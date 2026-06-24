# 第 11 章 · readiness 模型与 AsyncFd:把裸 fd 包成可 await 的 Future

> **核心问题**:第 10 章讲透了 epoll——reactor 蹲在 `epoll_wait` 上,事件来了拿到一堆 `Event`,每个带一个 `Token(usize)`。可问题是:拿到 `Token(0x7f4a3c001200)`(一个看着像内存地址的怪数字),**tokio 怎么知道"是哪个 task 在等这个 fd"?** 十万个 fd、十万个等待中的 task,这张"fd → 等待者"的映射表,凭什么能做到 O(1) 查回、还能扛住海量并发注册/注销?以及更根本的:用户手里明明只有一个**裸 fd**(一个 `RawFd` 整数),tokio 怎么把它包成一个可以 `socket.readable().await?` 的 async API——那个 `readable()` 返回的 Future,内部在干什么?
>
> 这一章拆 tokio 在 mio 之上那层精巧封装:从 `ScheduledIo`(每个被监听 fd 的"等待者档案")、到 token 怎么高效映射回它、再到 `AsyncFd` 怎么把"等 fd 就绪"暴露成 async fn,以及 edge-triggered 那条"读到 EAGAIN"的铁律怎么被封进类型。
>
> **读完本章你会明白**:
> - "readiness 模型"到底是个什么模型——为什么 tokio 不直接"异步读",而是"先等可读、再同步读到 EAGAIN"。这套模型的代价和红利各是什么。
> - 十万 fd 怎么用一个 token 高效映射回等待中的 task:tokio 用的不是朴素的 `HashMap<fd, task>`,而是把 **ScheduledIo 的指针编码进 token**——拿到 token,直接 `token as *const ScheduledIo`,O(1) 拿到等待者档案,零查表、零锁。
> - `ScheduledIo` 这个"每个 fd 一份"的小结构,怎么同时容纳"readiness 状态(打包进一个 AtomicUsize)"+"reader/writer 两个保留 waker 槽"+"一个任意数量的 waiter 链表",用缓存行对齐避免 false sharing。
> - `AsyncFd::readable().await` 这个用户写的 async 调用,展开后是个什么状态机:乐观查 readiness → 没就绪就入队挂起 → 被 reactor 的 `wake` 唤醒 → 返回一个 `AsyncFdReadyGuard`,**这个 guard 强制你必须明确"清不清 readiness"**,把 edge-triggered 的"读到 EAGAIN"铁律封进了类型。
>
> **如果一读觉得太难**:先只记住三件事——① tokio 给每个被监听的 fd 配一份 `ScheduledIo`(一个小结构,记着"谁在等它就绪");② token 不是下标而是**指针编码**,reactor 拿到 token 直接还原成 `ScheduledIo` 指针,O(1) 找到等待者;③ 用户调 `socket.readable().await`,等的是"内核说这个 fd 可读了",拿到一个 guard 后用户去同步 read,读到 `EAGAIN` 就清 readiness 重新等——这一套把 edge-triggered 的坑堵死了。

---

## 章首·一句话点破

> **tokio 给每个被监听的 fd 配一份"档案"(`ScheduledIo`),档案地址直接编码进 mio 的 token;内核事件一来,reactor 拿着 token 当指针用,一步跳到档案,把"哪个 task 在等"全捞出来唤醒。而用户侧,tokio 把"等 fd 就绪"包成 `AsyncFd::readable().await`,用一个 `#[must_use]` 的 guard 强制你"读完或没读完都得说一声",把 edge-triggered 那条最容易踩坑的铁律,封进了类型系统。**

这是**结论**。这一章倒过来拆:先讲 readiness 模型到底是什么、为什么是它(而不是"异步 read");再拆 token 怎么映射回等待者(主角技巧:指针编码 vs HashMap 的反面对比);然后落到 `ScheduledIo` 的内存布局,看一个 fd 的等待者档案长什么样;最后跟到 `AsyncFd::readable` 的状态机,看 edge-triggered 那条铁律怎么被封进 guard 类型。

第 10 章结尾留了个钩子:"拿到 `Token(42)`,tokio 怎么知道是哪个 task 在等?"这一章一口气回答。

---

## 一、先看清 readiness 模型:为什么不"直接异步读",而要"等可读、再同步读"

要理解 tokio 在 mio 上面的封装,得先看清 tokio 选的 I/O 模型——**readiness(就绪)模型**——为什么是它,以及它的反面是什么。

### 两种 I/O 模型的根本分野

异步 I/O 在工业界有两个截然不同的流派:

#### 流派 A:completion(完成)模型——"你提交 read,我做完了连数据一起还你"

代表:**Windows IOCP**、**Linux io_uring**(新)、Linux 的 `aio`、FreeBSD 的 `aio`。

模型:用户向内核**提交一个完整的 I/O 请求**(包含"读哪个 fd、读到哪个 buffer、读多少"),然后干别的;内核做完了(数据已经搬进 buffer),**通知用户"这个请求完成了,数据在这"**。

```c
// 简化示意,非源码原文:completion 模型(IOCP 风格)
OVERLAPPED ov;
ReadFile(handle, buf, len, NULL, &ov);   // 提交请求,立刻返回
// ... 干别的 ...
GetQueuedCompletionStatus(...);          // 等内核通知"读完了"
// 此时 buf 里已经有数据了
```

**关键特征**:用户**提交时就给出 buffer**;内核完成时,**数据已经在 buffer 里**,用户直接用。

#### 流派 B:readiness(就绪)模型——"我只告诉你'可以读了',你自己去读"

代表:**Linux epoll**、**BSD/macOS kqueue**(classic 模式)。

模型:用户问内核"这堆 fd 里哪些**可以读**了",内核只回答"fd=42 可读了"——**不替你读**;用户自己拿着 fd 去 `read`,读到数据或读到 `EAGAIN`(其实暂时没数据,误报)。

```c
// 简化示意,非源码原文:readiness 模型(epoll 风格)
epoll_wait(epfd, events, max, -1);       // 等就绪事件
// events 里告诉你"fd=42 可读了"
int n = read(42, buf, sizeof buf);       // 自己去读
// n 可能 > 0(读到数据),也可能 == -1 且 errno==EAGAIN(误报)
```

**关键特征**:用户**不预提交 buffer**;内核只给"就绪"这个**信号**;用户自己 `read`/`write`。

### 为什么 tokio(和 mio)选 readiness

第 10 章那张表已经点过:Linux epoll、BSD/macOS kqueue 是 readiness 模型,Windows IOCP 是 completion 模型。tokio/mio **跨平台**,它要选一个**能在所有主流 OS 上落地**的统一抽象。

> **不这样会怎样(如果 tokio 硬选 completion)**:Linux 的 classic epoll 根本不提供 completion 接口。要在 Linux 上做 completion,tokio 得① 自己在用户态维护一个 buffer 池,② 把"读到 buffer"包成一个用户态的 read 请求队列,③ 内核通知就绪时,自己代用户执行 read。这是一层**完整的 completion 适配层**,复杂、有 buffer 拷贝开销,而且和 epoll 的 edge-triggered 语义配合时极易出错(读不完怎么办、buffer 复用怎么管)。Windows 的 IOCP 是天然 completion,但 Linux 上硬套 completion 就是削足适履。

> **所以这样设计**:mio 选了**就低不就高**——以 readiness 为统一模型。在 Linux/macOS/BSD 上,readiness 是 syscall 原生支持的(epoll/kqueue 直接就是 readiness);在 Windows 上,mio 用一层适配把 IOCP 的 completion 翻译成 readiness 的样子(代价是有 buffer 中转,但 Windows 不是 tokio 的主战场)。这样 tokio 上层只需要对着一个 readiness 抽象写代码,跨平台一致。

### readiness 模型的代价:你必须"等就绪 + 自己读"

选了 readiness,意味着 tokio 的异步 I/O API 长这样(**不是**"我帮你异步 read",而是):

1. **先等就绪**:`socket.readable().await?` ——等到内核说"这个 fd 可以读了";
2. **拿到一个 guard**:这个 guard 是 `#[must_use]` 的,你必须明确告诉它"读完了"还是"没读到";
3. **自己同步 read**:在 guard 的范围内,直接 `socket.read(&mut buf)`——这是同步 syscall,但因为你刚确认过"可读",它**几乎不会阻塞**;
4. **如果读到 EAGAIN**:说明"就绪"是误报(或者你读了一部分后内核缓冲区暂时空了),清掉 readiness 标志,回头再去 `readable().await` 等;
5. **如果读到了**:处理数据,guard drop。

```rust
// 简化示意,非源码原文:readiness 模型下读一个 fd 的典型代码
loop {
    let mut guard = async_fd.readable().await?;   // 1. 等就绪
    let buf: &mut [u8] = ...;
    match guard.try_io(|| inner.read(buf)) {       // 3. 自己同步 read
        Ok(Ok(n)) => { /* 读到 n 字节,处理 */ break; }
        Ok(Err(e)) => return Err(e),
        Err(_would_block) => { /* 4. EAGAIN,guard 自动清 readiness,继续等 */ }
    }
}
```

> **钉死这件事(readiness 的根本约定)**:tokio 不替你"异步 read",它只替你"异步等可读"。读,还是你自己用同步 syscall 读——但因为你刚确认过"可读",这次 read 几乎不会阻塞。**这是 readiness 模型和 completion 模型的本质区别**,也是为什么 tokio 的 I/O API 有 `readable()` / `writable()` 这种"等就绪"的方法,而不是 `read_async()` 这种"帮你读"的方法。第 6 篇会拆 `AsyncRead`/`AsyncWrite` trait 怎么把这套"等就绪 + 同步读"包成 `async fn read()` 的样子,本章先立住 readiness 这个底层模型。

---

## 二、token 怎么映射回等待者:主角技巧——指针编码,不是 HashMap

readiness 模型下,reactor 拿到一批就绪事件,每个事件带一个 `Token`。现在最关键的问题:**这个 token,怎么映射回"哪个 task 在等这个 fd"?**

### 反面:朴素的 HashMap<fd, 等待者>

最直觉的设计,是维护一张全局表,key 是 fd,value 是"等待者信息"。

```rust
// 简化示意,非源码原文:朴素的 HashMap 注册表
static WAITERS: Mutex<HashMap<RawFd, WaiterInfo>> = Mutex::new(HashMap::new());

// 注册时
WAITERS.lock().insert(fd, waiter_info);

// 事件来时
let token = event.token();   // 假设 token == fd
let info = WAITERS.lock().get(&token).cloned();
```

> **不这样会怎样(三个致命问题)**:
> - **每次事件来都要查表**:reactor 主循环里,每来一个事件就要 `HashMap::get`,每次 `get` 都要算 hash + 探测桶 + 可能的链表遍历,还要**加锁**(全局表是共享的)。一万个事件同时来,就是一万个 hash + 一万次锁竞争。**这是 reactor 热路径上的纯负担**。
> - **fd 会复用,有 ABA 风险**:fd 是个小整数,内核会复用。fd=42 关掉后,新开的 socket 可能又拿到 fd=42。如果你的注册表还留着旧的 fd=42 的等待者,事件来了你唤醒了**错误的 task**。要避免就得给每个 fd 配一个"generation"号,复杂度上升。
> - **生命周期管理噩梦**:HashMap 持有 `WaiterInfo`,task 取消了你怎么从表里删?fd 关闭了怎么同步删?这一套生命周期协调,在第 4 章拆 Waker 时讲过——**全局共享表是 async 世界里最痛的设计**,锁竞争 + 生命周期 + 组合性差三连。

这条路死在**"token 是个不透明乘客,我非要拿它去查一张全局表"**——把一件本来可以 O(1) 直接拿到的事,变成了 O(hash) + 加锁。

### 正解:把 ScheduledIo 的指针,直接编码进 token

tokio 用了一个极其巧妙的设计:**不要表,把指针当 token**。

具体来说:

1. 每个被监听的 fd,tokio 给它配一份 `ScheduledIo`(下节拆它的内部),这是一块**堆上**的、`Arc` 引用计数的内存;
2. **注册到 mio 时,token 不是个小整数下标,而是 `ScheduledIo` 这块内存的地址**——`ScheduledIo::token()` 把自己的 `&self` 指针转成 `usize`,塞进 mio 的 `Token`;
3. 事件一来,reactor 拿到 token,**直接把它当指针**:`token.0 as *const ScheduledIo`,一步拿到那块档案内存,O(1),无查表、无锁。

看 tokio 源码,这套机制干净到只有两行:

```rust
// tokio/src/runtime/io/scheduled_io.rs(摘录)
impl ScheduledIo {
    pub(crate) fn token(&self) -> mio::Token {
        mio::Token(super::EXPOSE_IO.expose_provenance(self))
    }
}
```

([tokio/src/runtime/io/scheduled_io.rs:189-191](../tokio/tokio/src/runtime/io/scheduled_io.rs#L189-L191))

`EXPOSE_IO` 是个全局静态([tokio/src/runtime/io/mod.rs:22](../tokio/tokio/src/runtime/io/mod.rs#L22)),它的 `expose_provenance` 在正常构建里就是 `ptr as usize`:

```rust
// tokio/src/util/ptr_expose.rs(摘录)
#[inline]
pub(crate) fn expose_provenance(&self, ptr: *const T) -> usize {
    #[cfg(not(miri))]
    { ptr as usize }
    // ...
}
```

([tokio/src/util/ptr_expose.rs:30-42](../tokio/tokio/src/util/ptr_expose.rs#L30-L42))

而 reactor 拿到事件后,把它还原成指针:

```rust
// tokio/src/runtime/io/driver.rs(摘录,turn 方法里)
for event in events.iter() {
    let token = event.token();
    if token == TOKEN_WAKEUP {
        // ...
    } else if token == TOKEN_SIGNAL {
        // ...
    } else {
        let ready = Ready::from_mio(event);
        let ptr = super::EXPOSE_IO.from_exposed_addr(token.0);   // ← token 还原成指针!

        // Safety: we ensure that the pointers used as tokens are not freed
        // until they are both deregistered from mio **and** we know the I/O
        // driver is not concurrently polling. The I/O driver holds ownership of
        // an `Arc<ScheduledIo>` so we can safely cast this to a ref.
        let io: &ScheduledIo = unsafe { &*ptr };

        io.set_readiness(Tick::Set, |curr| curr | ready);
        io.wake(ready);
    }
}
```

([tokio/src/runtime/io/driver.rs:199-223](../tokio/tokio/src/runtime/io/driver.rs#L199-L223))

**`let io: &ScheduledIo = unsafe { &*ptr };`**——这一行是本章的灵魂。token 直接当指针解引用,一步拿到 `ScheduledIo`。没有任何 HashMap,没有 hash 计算,没有锁。

> **钉死这件事(token = 指针的全部妙处)**:tokio 把"fd → 等待者"的映射,**从一张表,变成了一次指针解引用**。注册时,指针塞进 token;事件来时,token 还原成指针。整个映射 O(1)、无锁、无查表。十万个 fd 和一个 fd,代价完全一样——都是一次 `usize as *const T`。这是把 mio 那个"token 是不透明 u64 乘客"的自由度,用到了极致。

### 为什么 sound:这块内存什么时候释放

指针编码的命门,显然是**"这块 ScheduledIo 内存,什么时候释放"**。如果 reactor 正在用 token 当指针解引用,而这块内存已经被释放——就是 use-after-free,UB。

tokio 的解法在源码注释里写得明明白白([tokio/src/runtime/io/driver.rs:212-215](../tokio/tokio/src/runtime/io/driver.rs#L212-L215)):

> we ensure that the pointers used as tokens are not freed until they are both deregistered from mio **and** we know the I/O driver is not concurrently polling. The I/O driver holds ownership of an `Arc<ScheduledIo>` so we can safely cast this to a ref.

拆成两条 sound 性保证:

1. **reactor 自己持有一份 `Arc<ScheduledIo>`**:看 driver.rs 的 `Handle` 结构,`RegistrationSet` 持有所有 `ScheduledIo` 的 `Arc`。只要 reactor 还活着,这些 `ScheduledIo` 就不会被释放——指针始终有效。
2. **释放要走"先 deregister、再释放"的两步**:当用户 drop 一个 `AsyncFd`,它先调 `deregister`(从 mio 注销 fd、token 再也不会出现在事件里),然后把 `Arc<ScheduledIo>` 放进 `pending_release` 列表,**等 reactor 下一次 turn 之前**才真正 drop。这保证了:**当 reactor 在 turn 中解引用指针时,这块内存要么还在 reactor 的 registrations 列表里(有效),要么已经被 deregister(token 不会再出现)**。两者不会并发。

看 deregister 的源码佐证:

```rust
// tokio/src/runtime/io/registration_set.rs(摘录)
// Returns `true` if the caller should unblock the I/O driver to purge
// registrations pending release.
pub(super) fn deregister(&self, synced: &mut Synced, registration: &Arc<ScheduledIo>) -> bool {
    synced.pending_release.push(registration.clone());

    let len = synced.pending_release.len();
    self.num_pending_release.store(len, Release);

    len == NOTIFY_AFTER
}
```

([tokio/src/runtime/io/registration_set.rs:75-84](../tokio/tokio/src/runtime/io/registration_set.rs#L75-L84))

注意它**没有立刻 drop** `ScheduledIo`,而是 push 进 `pending_release`,等 reactor 在下一个 turn 开头 `release_pending_registrations`([tokio/src/runtime/io/driver.rs:182](../tokio/tokio/src/runtime/io/driver.rs#L182))才批量释放。这就是注释里那句"we know the I/O driver is not concurrently polling"的物理实现——释放发生在 poll **之间**,不是 poll **之中**,所以指针不会悬空。

> **钉死这件事(指针编码的 sound 性)**:`token as *const ScheduledIo` 这一行 unsafe 能成立,靠的是两条不变量:① reactor 持有 `Arc<ScheduledIo>`,内存不会在 reactor 用它时被释放;② 释放路径是"先 deregister(token 失效)→ 再延迟到 poll 之间释放"。这两条把 use-after-free 的可能性堵死。这是 Rust 用 `Arc` + 延迟释放,把"裸指针当 token"这种激进设计做 sound 的范例。

### 一个 miri 的细节:PtrExposeDomain 为什么存在

你可能注意到 `EXPOSE_IO` 不是普通的 `ptr as usize`,而是包了一层 `PtrExposeDomain`。看它的实现:

```rust
// tokio/src/util/ptr_expose.rs(摘录)
pub(crate) struct PtrExposeDomain<T> {
    #[cfg(miri)]
    map: Mutex<BTreeMap<usize, *const T>>,
    _phantom: PhantomData<T>,
}

impl<T> PtrExposeDomain<T> {
    pub(crate) fn expose_provenance(&self, ptr: *const T) -> usize {
        #[cfg(miri)]
        { let addr: usize = ptr.addr(); self.map.lock().insert(addr, ptr); addr }

        #[cfg(not(miri))]
        { ptr as usize }
    }
    // from_exposed_addr 同理
}
```

([tokio/src/util/ptr_expose.rs:11-60](../tokio/tokio/src/util/ptr_expose.rs#L11-L60))

在**正常构建**(`#[cfg(not(miri))]`)里,它就是 `ptr as usize` / `addr as *const T`——零开销。但在 **miri**(Rust 的 UB 检测工具)下,它换成了一个 `BTreeMap<addr, ptr>` 的查表。

为什么?因为 `ptr as usize` 再 `usize as ptr` 在 Rust 的 **strict provenance** 模型下,会丢失指针的"来源信息(provenance)"。miri 严格检查 provenance——一个 usize 直接 cast 成的指针,miri 认为它"没有合法的来源",会报 UB。`PtrExposeDomain` 在 miri 下用一张表保存"哪个 addr 对应哪个有 provenance 的指针",还原时从表里查回带 provenance 的指针。这让 tokio 在 miri 下能跑通严格 provenance 检查,**而正常构建零开销**。

> **钉死这件事(PtrExposeDomain 的设计)**:这是 Rust 生态"用 cfg 把开发期严格性、运行期零开销分开"的范例。正常用户跑 tokio,token 就是裸指针 cast,零开销;tokio 开发者跑 miri 验证,token 走一张表保留 provenance,能通过严格检查。**两套实现,同一个源文件,靠 cfg 切换**——既不牺牲性能,也不牺牲 sound 性验证能力。

---

## 三、ScheduledIo:一个 fd 的"等待者档案"长什么样

token 指向的 `ScheduledIo`,是每个被监听 fd 的"等待者档案"。这一节拆它的内存布局——你会看到一个极紧凑、极度优化的小结构。

### 一张图看清 ScheduledIo 的字段

```
   一个 ScheduledIo(每个被监听 fd 一份,堆上,Arc 引用计数)
   ┌─────────────────────────────────────────────────────────────┐
   │  #[repr(align(128))]  ← 缓存行对齐,避免 false sharing         │
   ├─────────────────────────────────────────────────────────────┤
   │  linked_list_pointers: UnsafeCell<Pointers<Self>>            │ ← driver 用,串成链表
   │                                                               │
   │  readiness: AtomicUsize  ← 打包!                             │
   │  ┌─────────┬───────────────┬────────────────────┬─────────┐  │
   │  │shutdown │  driver tick  │     readiness      │ (低位)  │  │
   │  │ 1 bit   │   15 bits     │     16 bits        │         │  │
   │  └─────────┴───────────────┴────────────────────┴─────────┘  │
   │   └─ READABLE/WRITABLE/READ_CLOSED/WRITE_CLOSED/PRIORITY/ERR  │
   │                                                               │
   │  waiters: Mutex<Waiters>                                      │
   │  ┌──────────────────────────────────────────────────────┐    │
   │  │  list: LinkedList<Waiter>   ← 任意数量的 async 等待者 │    │
   │  │  reader: Option<Waker>      ← AsyncRead 专用槽        │    │
   │  │  writer: Option<Waker>      ← AsyncWrite 专用槽       │    │
   │  └──────────────────────────────────────────────────────┘    │
   └─────────────────────────────────────────────────────────────┘
```

逐个字段拆。

### 字段一:`readiness: AtomicUsize`——状态位打包

这是最巧的字段。一个 `AtomicUsize`(64 位机器上 8 字节),**打包了三件事**:

```rust
// tokio/src/runtime/io/scheduled_io.rs(摘录)
// The `ScheduledIo::readiness` (`AtomicUsize`) is packed full of goodness.
//
// | shutdown | driver tick | readiness |
// |----------+-------------+-----------|
// |   1 bit  |   15 bits   |  16 bits  |

const READINESS: bit::Pack = bit::Pack::least_significant(16);
const TICK: bit::Pack = READINESS.then(15);
const SHUTDOWN: bit::Pack = TICK.then(1);
```

([tokio/src/runtime/io/scheduled_io.rs:164-173](../tokio/tokio/src/runtime/io/scheduled_io.rs#L164-L173))

- **低 16 位:readiness**(就绪位图)。每一位对应一种就绪状态:`READABLE`(0b001)、`WRITABLE`(0b010)、`READ_CLOSED`、`WRITE_CLOSED`、`PRIORITY`、`ERROR`(位定义见 [tokio/src/io/ready.rs:8-13](../tokio/tokio/src/io/ready.rs#L8-L13))。这个 fd 现在可读?低位第 0 位是 1;可写?第 1 位是 1。所有就绪状态共用 16 位,**一次原子读就把全部就绪状态捞出来**。
- **中间 15 位:driver tick**(驱动的"轮次"号)。这是个**单调递增**(模 32768)的计数,reactor 每次处理完一个事件就 `tick + 1`。它的作用下面"clear_readiness + tick"那一节详拆——简短地说,它是用来区分"这个 readiness 是这一轮设置的、还是上一轮残留的",防止 edge-triggered 下"清 readiness 时清错了"。
- **最高 1 位:shutdown**。reactor 关闭时置 1,所有等这个 fd 的 task 立刻被唤醒并拿到 `gone()` 错误。

> **钉死这件事(readiness 位打包的妙处)**:一个 fd 的"就绪状态 + tick + shutdown"三件事,塞进**一个 8 字节的 AtomicUsize**。读写它**一次原子操作**,无锁。这是个延续了第 5 章 task 状态位打包的同一套哲学——**Rust 系统级代码用位打包把多个并发状态塞进一个原子字**,既省内存(每个 fd 少占几个字节),又省同步(一次原子操作搞定,不用多个原子变量之间的协调)。在十万 fd 量级,这种"省一个原子字"的优化累积起来是实打实的缓存命中提升。

### 字段二:`waiters: Mutex<Waiters>`——等待者队列

readiness 位打包的是"fd 的状态",但"哪些 task 在等这个 fd"是另一回事——这些信息存在 `waiters` 字段里:

```rust
// tokio/src/runtime/io/scheduled_io.rs(摘录)
waiters: Mutex<Waiters>,

type WaitList = LinkedList<Waiter, <Waiter as linked_list::Link>::Target>;

#[derive(Debug, Default)]
struct Waiters {
    /// List of all current waiters.
    list: WaitList,

    /// Waker used for `AsyncRead`.
    reader: Option<Waker>,

    /// Waker used for `AsyncWrite`.
    writer: Option<Waker>,
}

#[derive(Debug)]
struct Waiter {
    pointers: linked_list::Pointers<Waiter>,
    waker: Option<Waker>,      // 这个等待者的 Waker
    interest: Interest,        // 等的是什么(READABLE / WRITABLE / ...)
    is_ready: bool,
    _p: PhantomPinned,
}
```

([tokio/src/runtime/io/scheduled_io.rs:107-140](../tokio/tokio/src/runtime/io/scheduled_io.rs#L107-L140))

这里有个**精妙的分层**——`Waiters` 里有**三种**等待者存储:

1. **`reader: Option<Waker>` / `writer: Option<Waker>`**——两个"保留槽",分别给 `AsyncRead` 和 `AsyncWrite` trait 的 poll 方法用。它们是 `Option<Waker>`(单个),因为 trait 方法的契约是"一个方向最多一个等待者"。这两个槽是**快路径**——大多数 I/O 走 `AsyncRead`/`AsyncWrite`,直接命中这两个槽,不用动链表。
2. **`list: WaitList`**——一个**侵入式链表**,存任意数量的 `Waiter`。这是给 `readable()` / `writable()` 这种 async fn 用的——多个 task 可以同时 `select!` 等同一个 fd,链表容纳任意多个。每个 `Waiter` 内嵌一个 `Waker` 和它等的 `Interest`。

为什么这么分层?**快路径优化**。`AsyncRead::poll_read` 是热路径(每次读都走),它只需要一个 reader 槽,O(1) 存取;链表是慢路径(`readable().await` 通常等少数 task),即便要遍历,也是少量。这种"快路径单槽 + 慢路径链表"的分层,是高性能 I/O 框架的标准设计。

注意 `Waiter` 有 `_p: PhantomPinned`——它**不可移动**。因为 `Waiter` 通过侵入式链表(`Pointers<Waiter>`)被串起来,链表节点存的是 `NonNull<Waiter>`(裸指针),`Waiter` 一旦入了链表就**不能被移动**(移动会让链表里的指针失效)。`PhantomPinned` 在类型层把这件事标出来,逼使用者在 `Pin` 下持有它。这是第 3 章 `Pin` 在实战中的真实落地。

### 字段三:`linked_list_pointers` + 缓存行对齐

剩下两个字段值得点一下:

- **`linked_list_pointers: UnsafeCell<Pointers<Self>>`**——这又是侵入式链表的节点指针,但这次是**让 `ScheduledIo` 自己被串成链表**(reactor 维护一张"所有注册的 ScheduledIo"链表,用 `RegistrationSet::Synced::registrations`,见 [registration_set.rs:20-21](../tokio/tokio/src/runtime/io/registration_set.rs#L20-L21))。这样 reactor 关闭时能遍历所有 `ScheduledIo` 把它们 shutdown。
- **`#[repr(align(128))]`**(在 x86_64/aarch64/powerpc64 上,见 [scheduled_io.rs:23-44](../tokio/tokio/src/runtime/io/scheduled_io.rs#L23-L44))——**128 字节缓存行对齐**。为什么 128 不是 64?源码注释引了 Intel/ARM 的资料:从 Intel Sandy Bridge 起,空间预取器(spatial prefetcher)**一次拉两条 64 字节缓存行**,所以为了彻底避免 false sharing,得对齐到 128。ARM big.LITTLE 的"大核"也是 128 字节缓存行。

> **钉死这件事(缓存行对齐的反 false sharing)**:十万 fd 各有一份 `ScheduledIo`,它们在堆上相邻排列。如果两个 `ScheduledIo` 共用一条缓存行,一个 CPU 改 `ScheduledIo_A.readiness`(原子写),会让另一个 CPU 上持有的 `ScheduledIo_B` 所在的**整条缓存行失效**——即使 B 根本没被碰。这就是 **false sharing**(伪共享),会让多核 reactor 的性能塌方。`#[repr(align(128))]` 强制每个 `ScheduledIo` 独占至少一条(在 64 字节核上甚至两条)缓存行,**根除 false sharing**。这是 Rust 系统级代码用 `repr(align)` 做缓存优化的范例,代价是每个 `ScheduledIo` 多占几十字节 padding,但换来的多核扩展性远超这点内存。

---

## 四、AsyncFd:把"等就绪"包成 async API

讲透了底层(`ScheduledIo` + token 指针编码),现在看用户侧的 API——`AsyncFd`。这是 tokio 暴露给用户的、把一个裸 fd 包成可 await 对象的入口。

### AsyncFd 是什么:一层包着 Registration 的壳

```rust
// tokio/src/io/async_fd.rs(摘录)
pub struct AsyncFd<T: AsRawFd> {
    registration: Registration,
    // The inner value is always present. the Option is required for `drop` and `into_inner`.
    // In all other methods `unwrap` is valid, and will never panic.
    inner: Option<T>,
}
```

([tokio/src/io/async_fd.rs:181-186](../tokio/tokio/src/io/async_fd.rs#L181-L186))

就俩字段:`registration`(底层的 `Registration`,持有 `Arc<ScheduledIo>`)+ `inner: Option<T>`(用户传进来的那个实现了 `AsRawFd` 的东西,比如 `std::net::TcpStream`)。`AsyncFd` 自己极薄——它就是把"一个 fd + 一份 reactor 注册"打包。

构造时,它做两件事:

```rust
// tokio/src/io/async_fd.rs(摘录,简化展示构造路径)
pub(crate) fn new_with_handle_and_interest(
    inner: T,
    handle: scheduler::Handle,
    interest: Interest,
) -> io::Result<Self> {
    // ... 内部:
    //   1. handle.io().add_source(&mut source, interest)
    //      → registrations.allocate() 分配 Arc<ScheduledIo>
    //      → registry.register(source, scheduled_io.token(), interest)
    //        把 fd + ScheduledIo 指针编码的 token 注册进 mio
    //   2. 把 Arc<ScheduledIo> 存进 Registration
}
```

([tokio/src/io/async_fd.rs:250-257](../tokio/tokio/src/io/async_fd.rs#L250-L257),内部委托 [tokio/src/runtime/io/driver.rs:266-289](../tokio/tokio/src/runtime/io/driver.rs#L266-L289) 的 `Handle::add_source`)

看 `Handle::add_source` 的真实代码,把"分配 ScheduledIo + 注册到 mio"绑在了一起:

```rust
// tokio/src/runtime/io/driver.rs(摘录)
pub(super) fn add_source(
    &self,
    source: &mut impl mio::event::Source,
    interest: Interest,
) -> io::Result<Arc<ScheduledIo>> {
    let scheduled_io = self.registrations.allocate(&mut self.synced.lock())?;
    let token = scheduled_io.token();     // ← ScheduledIo 指针编码成 token

    if let Err(e) = self.registry.register(source, token, interest.to_mio()) {
        // 注册失败,回收 ScheduledIo
        unsafe { self.registrations.remove(&mut self.synced.lock(), &scheduled_io) };
        return Err(e);
    }

    self.metrics.incr_fd_count();
    Ok(scheduled_io)
}
```

([tokio/src/runtime/io/driver.rs:266-289](../tokio/tokio/src/runtime/io/driver.rs#L266-L289))

> **钉死这件事(AsyncFd 构造的两件事)**:`AsyncFd::new(fd)` 干两件事:① 给这个 fd 配一份 `Arc<ScheduledIo>`(reactor 维护它的引用,保证 token 指针有效);② 把 fd + `ScheduledIo::token()`(指针编码)注册进 mio 的 epoll。从这一刻起,这个 fd 的事件,会带着"指向它档案的指针"流回 reactor。

### `readable().await` 是个什么状态机

用户写 `socket.readable().await?`,这是个 async fn。它返回一个 `AsyncFdReadyGuard`。展开后,内部是 `Registration::readiness(interest).await`,最终是 `ScheduledIo::readiness(interest).await`,内部是手写的 `Readiness` Future。看它的状态机:

```rust
// tokio/src/runtime/io/scheduled_io.rs(摘录)
struct Readiness<'a> {
    scheduled_io: &'a ScheduledIo,
    state: State,
    waiter: UnsafeCell<Waiter>,
}

enum State {
    Init,
    Waiting,
    Done,
}

impl Future for Readiness<'_> {
    type Output = ReadyEvent;

    fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        // ...
        loop {
            match *state {
                State::Init => {
                    // 乐观查现有 readiness(无锁)
                    let curr = scheduled_io.readiness.load(SeqCst);
                    let ready = Ready::from_usize(READINESS.unpack(curr)).intersection(interest);

                    if !ready.is_empty() || is_shutdown {
                        // 已经就绪!直接返回(快路径,不入队)
                        *state = State::Done;
                        return Poll::Ready(ReadyEvent { ... });
                    }

                    // 没就绪,加锁,再查一次(防 TOCTOU)
                    let mut waiters = scheduled_io.waiters.lock();
                    let curr = scheduled_io.readiness.load(SeqCst);
                    let mut ready = Ready::from_usize(READINESS.unpack(curr));
                    // ... 再次检查,若就绪则返回 ...

                    // 仍没就绪 → 把自己的 Waker 入队
                    *state = State::Waiting;
                    waiter.waker = Some(cx.waker().clone());   // ← 留 Waker
                    waiters.list.push_back(waiter_ptr);        // ← 入链表
                    return Poll::Pending;                      // ← 挂起
                }
                State::Waiting => {
                    // ... 被唤醒后再查,就绪则 Done,否则继续等 ...
                }
                State::Done => panic!("polled after ready"),
            }
        }
    }
}
```

([tokio/src/runtime/io/scheduled_io.rs:430-510](../tokio/tokio/src/runtime/io/scheduled_io.rs#L430-L510),简化展示)

这是个标准的"乐观查 → 加锁复查 → 入队挂起"的状态机,符合第 2 章 poll 契约。几个细节值得拆:

**① 乐观查 readiness(无锁快路径)**:`State::Init` 第一件事是 `readiness.load(SeqCst)`——**不加锁**地查现有 readiness。如果 fd 已经就绪(比如之前的 `clear_readiness` 没清干净,或者 reactor 已经 wake 过了),直接返回 `Ready`,**根本不进 waiters 链表**。这是巨大的优化——大多数 `readable().await` 在数据已经到了之后被调,命中这条快路径,零锁。

**② 加锁复查(防 TOCTOU)**:乐观查说没就绪,但**在你 load 之后、加锁之前**,reactor 可能正好 wake 了(置了 readiness)。所以加锁后**再 load 一次** readiness,确认真的没就绪才入队。这是经典的 **double-checked locking** 模式,防止"乐观查和入队之间的事件竞态"。

**③ 入队 + 留 Waker**:把 `cx.waker().clone()` 塞进 `Waiter.waker`,把 Waiter 推进 `waiters.list` 链表。这一步就是第 4 章讲的"返回 Pending 前留 Waker"——这里留的 Waker,就是 reactor 在 `ScheduledIo::wake` 时要按的对象。

> **钉死这件事(readiness future 的三态)**:Init(乐观查)→ Waiting(入队挂起)→ Done(就绪返回)。这套状态机把第 4 章的 Waker 契约、第 2 章的 poll 契约、和本章的 readiness 模型三者缝了起来——用户写 `readable().await`,内部是这套状态机在反复 poll,挂起时 Waker 留在 `ScheduledIo.waiters` 里,reactor wake 时按响它。

---

## 技巧精解:edge-triggered 那条"读到 EAGAIN"的铁律,怎么被封进 guard 类型

这一节是本章的硬核,把第 10 章讲的 edge-triggered 铁律——"收到事件后必须循环读到 EAGAIN,否则漏事件"——和 tokio 怎么用**类型系统**把它强制执行,拆透。这是本章总纲钦定的"注册表 + 映射"主角技巧的延伸:不仅映射要 O(1),还要把容易踩的坑**用类型堵死**。

### 问题的根源:edge-triggered 下,readiness 是"一次性消费"

回忆第 10 章:edge-triggered 下,fd 从"不可读"变"可读"只触发一次事件。tokio 收到事件,把 `ScheduledIo.readiness` 置位(表示"这个 fd 现在可读")。**用户必须把数据读完**,读完之后调"清 readiness",这样下次数据来(状态再从"空"变"非空"),才会触发新事件。

如果用户**不清 readiness**:tokio 的 `ScheduledIo.readiness` 一直显示"可读",但内核已经不会再触发事件了(因为状态没变化)——于是**用户调 `readable().await` 会立刻返回(tokio 那边的 readiness 还亮着),但内核其实没新数据,读到的是上次的旧数据或者 EAGAIN**——逻辑全乱。

如果用户**清 readiness 清早了**:tokio 把 readiness 清了,但用户其实没读完内核缓冲区,而内核**不会再触发事件**(edge 只在状态变化时触发,但状态一直是"非空")——**剩下的数据永久卡在内核里**,连接"假死"。

所以 edge-triggered 的铁律是:**先读到 EAGAIN(确认内核缓冲区空了),再清 readiness**。顺序不能反,不能漏。

### 朴素 API 的陷阱:让用户自己记得

最朴素的 API 设计,是给用户两个方法:`wait_readable().await` 和 `clear_readable()`,让用户自己保证"读完再 clear":

```rust
// 简化示意,非源码原文:朴素的 API,让用户自己保证顺序
loop {
    reactor.wait_readable(fd).await?;        // 等就绪
    let n = read(fd, buf)?;                  // 读
    if n == 0 { break; }
    process(buf);
    // 用户必须记得:读完后再 clear
    reactor.clear_readable(fd);              // ← 漏了这行就死,顺序错了也死
}
```

> **不这样会怎样(朴素 API 的反面)**:
> - **忘了 clear**:readiness 一直亮着,下次 `wait_readable` 立刻返回(其实内核没新事件),CPU 进入忙循环(死循环调 wait → read → EAGAIN → wait → ...)。**用户被 edge 的坑直接咬中**。
> - **clear 早了**:还没读到 EAGAIN 就 clear,内核缓冲区剩的数据永远等不到事件。**连接假死**,极难排查的 bug。
> - **每个用 AsyncFd 的用户,都得记住这条铁律**——百万用户里必然有人记不住。库的设计哲学是"把容易踩的坑用类型堵死",这个 API 显然不合格。

### tokio 的解法:`#[must_use]` 的 guard,强制你"说一声"

tokio 用一个**编译期强制**的设计堵死这个坑——`AsyncFdReadyGuard`。看 `readable()` 返回什么:

```rust
// tokio/src/io/async_fd.rs(摘录)
pub async fn readable<'a>(&'a self) -> io::Result<AsyncFdReadyGuard<'a, T>> {
    self.ready(Interest::READABLE).await
}
```

([tokio/src/io/async_fd.rs:713-715](../tokio/tokio/src/io/async_fd.rs#L713-L715))

它返回的不是一个普通的"就绪信号",而是一个 **guard**:`AsyncFdReadyGuard`。看这个类型的定义:

```rust
// tokio/src/io/async_fd.rs(摘录)
#[must_use = "You must explicitly choose whether to clear the readiness state by calling a method on ReadyGuard"]
pub struct AsyncFdReadyGuard<'a, T: AsRawFd> {
    async_fd: &'a AsyncFd<T>,
    event: Option<ReadyEvent>,
}
```

([tokio/src/io/async_fd.rs:193-197](../tokio/tokio/src/io/async_fd.rs#L193-L197))

注意那个 `#[must_use = "..."]`——**如果你拿到一个 guard 却不调用它的任何方法就让它 drop,编译器会警告**。这是 Rust 类型系统在喊:"你拿到了一个就绪信号,你必须明确告诉我怎么处理它!"

guard 提供几个方法,分别对应"读完了"和"没读完"的不同意图:

```rust
// tokio/src/io/async_fd.rs(摘录,简化)
impl<'a, T: AsRawFd> AsyncFdReadyGuard<'a, T> {
    /// "我试着读了,读到 EAGAIN 了"——清掉对应的 readiness
    pub fn clear_ready_matching(&mut self, ready: Ready) {
        if let Some(mut event) = self.event.take() {
            self.async_fd.registration.clear_readiness(event.with_ready(ready));
            // ...
        }
    }

    /// "我故意保留 readiness"(比如读了但没读到 EAGAIN,下次还想直接读)
    pub fn retain_ready(&mut self) {
        // no-op,只是为了满足 #[must_use]
    }

    /// "帮我执行一次 IO,如果 WouldBlock 就自动 clear"
    pub fn try_io<R>(&mut self, f: impl FnOnce(&T) -> io::Result<R>) -> Result<io::Result<R>, TryIoError> {
        // ... 调 f,如果 WouldBlock 就 clear_readiness ...
    }
}
```

([tokio/src/io/async_fd.rs:1059-1100, 1151+](../tokio/tokio/src/io/async_fd.rs#L1059-L1100))

`try_io` 是最常用的——它把"执行 IO → 如果 WouldBlock 就 clear"这个标准循环**封进一个方法**。用户拿到 guard 后调 `guard.try_io(|| inner.read(buf))`,内部自动处理 EAGAIN 和 clear,**用户根本不用想 edge-triggered 的铁律**。

> **钉死这件事(用类型堵坑的范例)**:tokio 把 edge-triggered 那条最容易踩的"读到 EAGAIN 再 clear"铁律,**用 `#[must_use]` 的 guard 类型强制执行**。用户**无法**忘记 clear——编译器会警告;用户**也无法**误用 clear——guard 的方法明确区分了"clear(clear_ready_matching)"、"保留(retain_ready)"、"自动循环(try_io)"。**整个 edge-triggered 的复杂度,被吞进了 guard 的类型设计,用户拿到的是一个不会用错的 API**。这是 Rust 用类型系统把不安全/易错的语义"关进笼子"的范例,和第 3 章的 `Pin`、第 4 章的 `Waker::from_raw` 同源——**把正确性义务,从用户的脑子里,挪到编译器里**。

### clear_readiness + tick:防"清错了"

最后拆一个微妙的 sound 性细节——`clear_readiness` 怎么保证"清的是这一轮的 readiness,不是上一轮残留的"。看 ScheduledIo 的 readiness 字段那个 15 位的 tick:

```rust
// tokio/src/runtime/io/scheduled_io.rs(摘录)
pub(super) fn set_readiness(&self, tick_op: Tick, f: impl Fn(Ready) -> Ready) {
    let _ = self.readiness.fetch_update(AcqRel, Acquire, |curr| {
        // ...
        let tick = TICK.unpack(curr);

        let new_tick = match tick_op {
            // Trying to clear readiness with an old event!
            Tick::Clear(t) if tick as u8 != t => return None,   // ← tick 不匹配,拒绝清!
            Tick::Clear(t) => t as usize,
            Tick::Set => tick.wrapping_add(1) % MAX_TICK,
        };
        let ready = Ready::from_usize(READINESS.unpack(curr));
        Some(TICK.pack(new_tick, f(ready).as_usize()))
    });
}
```

([tokio/src/runtime/io/scheduled_io.rs:209-228](../tokio/tokio/src/runtime/io/scheduled_io.rs#L209-L228))

这段是个**精细的并发协调**。场景:用户 task A 在 tick=5 时拿到一个 ready 事件(可读),然后被切走;切回来后,reactor 在 tick=6 又给这个 fd 设了一次 ready。A 现在 `clear_readiness(tick=5)`,**应该清的是 tick=5 的那个 readiness**,而不是 tick=6 的新 readiness——否则 A 把 reactor 刚设置的新事件清掉了,新数据就漏了。

`Tick::Clear(t) if tick as u8 != t => return None` 这一行就是干这个的——**clear 时带 tick,只有当前 readiness 的 tick 等于这个 tick 才允许清**。tick 不匹配,fetch_update 返回 None(不修改),保留新 readiness。这是 edge-triggered + 异步取消组合下,防止"清错 readiness 漏事件"的标准技巧。

> **钉死这件事(tick 防漏事件)**:readiness 字段里的 15 位 tick,不是为了好玩——它是 edge-triggered 下"clear_readiness 别清错了"的并发保护。每次 set readiness,tick 加 1;clear 时带原 tick,只清同 tick 的。这样即使 task 被切走、期间 reactor 又设了新 readiness,也不会被旧 task 的 clear 误清。这是 tokio 把"并发正确性"做到位的一个微小但关键的细节。

---

## 章末小结

### 用"餐厅服务员"比喻回顾本章

1. **tokio 选了 readiness 模型——"我只告诉你菜好了,你自己去端"**——而不是 completion 模型的"我帮你端好端到桌"。因为 epoll/kqueue 是 readiness,Windows IOCP 才是 completion,tokio 跨平台选了就低不就高。代价是用户得自己 read,但好处是跨平台一致、零拷贝。
2. **厨房喊"3 号菜好了"时,喊的不是一个桌号,而是"3 号菜档案柜的地址"**——这就是 token 指针编码。每个被监听的 fd 配一份档案(`ScheduledIo`,堆上一块内存),档案的内存地址直接当 token。服务员(reactor)拿到 token 当指针用,一步跳到档案柜,知道"哪些服务员在等这单"。**没有 HashMap 查表,没有锁**。
3. **每份档案(ScheduledIo)有三个抽屉**——① 一个 `AtomicUsize` 打包了"菜好没好(readiness)+ 这是第几轮(tick)+ 厨房关没关(shutdown)",一次原子读全知道;② 两个保留 waker 槽(reader/writer)给 AsyncRead/AsyncWrite 热路径用;③ 一个任意长的 waiter 链表给 `readable().await` 这种慢路径用。整个档案 128 字节对齐,避免多核 false sharing。
4. **用户调 `socket.readable().await`,等的是"内核说菜好了"**——内部是个状态机:乐观查 readiness(快路径,菜已经好直接端)→ 没好就入队挂起(留 Waker)→ 被 reactor wake → 返回。这套把第 4 章的 Waker 契约、第 2 章的 poll 契约、本章的 readiness 缝了起来。
5. **服务员端菜时,餐厅强制他"要么说端完了、要么说没端完"——`#[must_use]` 的 guard**——这是 tokio 把 edge-triggered"读到 EAGAIN 才能 stop"的铁律封进类型的办法。用户拿到 guard 不能无视,必须调 `clear_ready_matching` / `retain_ready` / `try_io` 之一,编译器盯着。**edge 的坑,被类型堵死了**。

### 本章在全书主线中的位置

记住全书的二分法:**调度执行(让就绪的任务跑) vs 事件唤醒(让等待的任务不空耗、就绪了再叫)**。

这一章,把"事件唤醒"那一面的**中间层**点亮了:

- 第 10 章拆了底座——mio/epoll 怎么提供"批量等事件";
- **这一章拆了中间层**——tokio 怎么在 mio 上,用 ScheduledIo + 指针编码 token,把"海量 fd ↔ 等待中的 task"对应起来,并用 AsyncFd 把它暴露成 async API;
- 第 12 章拆最上层——reactor 拿到事件、wake 了 task 之后,**怎么把 task 批量塞回调度队列**,把 reactor 和第 2 篇的 scheduler 缝起来。

本章服务的明显是"事件唤醒"那一面——它就是"等待者怎么挂上、怎么被精确找到"的全部机制。

### 五个"为什么"清单

1. **为什么 tokio 选 readiness 模型而不是 completion?**:因为 epoll/kqueue(Linux/macOS/BSD 的主流)是 readiness,IOCP(Windows)才是 completion。tokio 跨平台选了"就低不就高"——以 readiness 为统一抽象,Linux/macOS 原生支持,Windows 用适配层。代价是用户得自己 read,但换来跨平台一致和零拷贝。readiness 的根本约定是"先等就绪、再同步读到 EAGAIN"。
2. **token 怎么映射回等待者,凭什么 O(1)?**:tokio 把 `ScheduledIo` 的**指针编码进 token**——注册时指针塞 token,事件来时 token 还原成指针,直接解引用拿 `ScheduledIo`。没有 HashMap,没有锁,O(1)。这是把 mio"token 是不透明 u64"的自由度用到了极致。
3. **指针当 token 为什么 sound(不会 use-after-free)?**:两条保证:① reactor 持有 `Arc<ScheduledIo>`,内存不会在 reactor 用它时释放;② 释放走"先 deregister(token 失效)→ 再延迟到 poll 之间释放"的两步。这样 reactor 在 poll 中解引用指针时,内存要么还在(有效),要么 token 已经失效(不会出现)。延迟释放由 `pending_release` 列表 + reactor 在 turn 开头批量清理实现。
4. **ScheduledIo 为什么用位打包 + 缓存行对齐?**:① 一个 `AtomicUsize` 打包 readiness(16 位就绪位图)+ tick(15 位轮次)+ shutdown(1 位),一次原子读写搞定,省内存省同步;② `#[repr(align(128))]` 让每个 ScheduledIo 独占缓存行,避免十万 fd 之间的 false sharing,多核扩展性远超 padding 的内存代价。
5. **edge-triggered"读到 EAGAIN"的铁律,tokio 怎么强制用户遵守?**:用 `#[must_use]` 的 `AsyncFdReadyGuard`——用户拿到 guard 必须显式调 `clear_ready_matching` / `retain_ready` / `try_io` 之一,否则编译警告。`try_io` 把"执行 IO → WouldBlock 自动 clear"封进一个方法,用户根本不用想 edge 铁律。整个 edge 的复杂度被吞进 guard 类型,用户拿到一个不会用错的 API。

### 想继续深入,该往哪钻

- **tokio 源码(本章引用的核心)**:
  - [tokio/src/runtime/io/scheduled_io.rs](../tokio/tokio/src/runtime/io/scheduled_io.rs) —— `ScheduledIo` 的全部:结构定义(L101-110)、`readiness` 位打包常量(L164-173)、`token()`(L189-191,指针编码)、`set_readiness` + tick(L209-228)、`wake`(L235-295,唤醒等待者)、`Readiness` Future(L388-510)。**这一份文件,是 tokio reactor 中间层的全部**。
  - [tokio/src/runtime/io/driver.rs](../tokio/tokio/src/runtime/io/driver.rs) —— `Driver::turn`(L179-239,token 还原成指针 + wake)、`Handle::add_source`(L266-289,注册 + 指针 token)。第 10 章和本章都引这份,是 reactor 的心脏。
  - [tokio/src/runtime/io/registration_set.rs](../tokio/tokio/src/runtime/io/registration_set.rs) —— `RegistrationSet` + `Synced`,`allocate`(分配 Arc<ScheduledIo>)、`deregister`(L75-84,延迟释放到 pending_release)。这是"指针 token 为什么 sound"的物理保证。
  - [tokio/src/runtime/io/registration.rs](../tokio/tokio/src/runtime/io/registration.rs) —— `Registration`(AsyncFd 持有的下层),`readiness` async 方法、`clear_readiness`、`try_io`。
  - [tokio/src/io/async_fd.rs](../tokio/tokio/src/io/async_fd.rs) —— `AsyncFd` 公共 API:`new`/`with_interest`(L226-257)、`readable`/`writable`(L713-771)、`AsyncFdReadyGuard`(`#[must_use]` L193-197、`clear_ready_matching` L1059、`try_io`)。
  - [tokio/src/util/ptr_expose.rs](../tokio/tokio/src/util/ptr_expose.rs) —— `PtrExposeDomain`(指针编码 + miri 严格 provenance 适配),整个文件就这一件事。
- **`loom` 测试**:tokio 在 `tests/` 下有用 loom 验证 `ScheduledIo` 并发正确性的测试(尤其 `set_readiness` 的 tick + 多 waiter 的 wake 顺序)。想深入"指针编码 + 延迟释放 + 多 waiter"为什么在所有线程交错下都 sound,看 tokio 的 loom 测试。
- **io_uring 这条支线**(可选进阶):tokio 有个 `tokio_unstable + io-uring` feature,在 Linux 上用 io_uring 做 completion 模型的 I/O(见 [tokio/src/runtime/io/driver/uring.rs](../tokio/tokio/src/runtime/io/driver/uring.rs))。这是 readiness 之外的另一条路,目前还是 unstable,但能让你看清"completion 模型在 tokio 里长什么样"。
- **下一站**:本章讲透了"等待者怎么挂上 ScheduledIo、事件来了怎么 wake 它"。可 `wake` 之后——`waker.wake()` 经 vtable 调到 `scheduler.schedule(Notified(task))`,把 task 塞回调度队列——**reactor 一次 `epoll_wait` 拿到一批事件,会批量 wake 一堆 task,这批 wake 怎么高效地回灌调度器?reactor 和第 2 篇的 scheduler 怎么缝起来?** 翻开 **第 12 章 · reactor 与 scheduler 的握手**——本章的 `wake` 调用,会一路追到 scheduler 的本地队列 / 全局队列,把第 3 篇 reactor 和第 2 篇 scheduler 缝合成一个完整的事件驱动循环。

---

> readiness 模型和 AsyncFd 把"等待者怎么挂、怎么被精确找到"讲透了。可 `ScheduledIo::wake` 里那一行 `waker.wake()`,按响呼叫器之后,task 怎么回到调度队列?reactor 一次 `epoll_wait` 拿到一批事件,会同时 wake 一堆 task——这批 wake 怎么高效地回灌第 2 篇的 scheduler?翻开 **第 12 章 · reactor 与 scheduler 的握手**——把第 3 篇 reactor 和第 2 篇 scheduler 缝成一个完整的事件驱动循环。
