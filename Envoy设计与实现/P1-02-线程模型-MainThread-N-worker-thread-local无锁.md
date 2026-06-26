# 第 1 篇 · 第 2 章 · 线程模型:MainThread + N worker,thread-local 无锁

> **核心问题**:Envoy 一个进程,要同时处理几万到几十万个并发连接。它为什么不干脆"一个连接一个线程"?为什么不只用一个线程(像 Redis 那样)?又为什么不做成"一堆线程共享一个连接表、靠锁保护"?这一章拆 Envoy 的并发地基——**一个 MainThread 加 N 个 worker,每 worker 一个事件循环,连接一旦被某 worker accept 就永久绑定该 worker,跨线程共享的可变状态用 thread-local 副本避免锁**——这套设计为什么 sound,为什么扛得住海量连接。

> **读完本章你会明白**:
> 1. 为什么 Envoy 既不是"一个连接一个线程"(线程爆炸)、也不是"单线程"(单核跑不满),而是 **N 个 worker,每 worker 一个事件循环**——这是 epoll/事件循环这一范式在"多核 + 海量连接"下的自然延伸。
> 2. 为什么**连接一旦被某 worker accept,就永久绑定该 worker**,后续该连接上所有 filter chain 都在同一个线程跑——这条不变式是 thread-local 无锁成立的前提。
> 3. Envoy 怎么用 **SO_REUSEPORT 让内核做连接负载均衡**(每 worker 都 listen 同一端口),而不是"主线程 accept 再分发"——这避开了惊群与分发瓶颈。
> 4. 什么是 **thread-local slot 机制**(`source/common/thread_local/thread_local_impl.cc`),为什么主线程改了配置就能"广播"到所有 worker 的本地副本,而 worker 读自己的副本全程无锁。
> 5. (一条重要的诚实修正)Envoy 的 **counter/gauge stats 实际是 `std::atomic`,不是"每 worker 一个副本"**——本章会讲清两者各自的用武之地,以及为什么老资料里"stats 用 thread-local 归并"的说法是**对热数据无锁机制的简化叙述**。

> **如果一读觉得太难**:先只记住三件事——① Envoy 是 **N 个 worker,每 worker 一个事件循环**,MainThread 只管"下发配置"不跑流量;② **一条连接一生只待在一个 worker 上**,所以连接上的状态天然不需要锁;③ 跨线程要共享的**配置类**可变状态(Runtime、cluster manager 的 thread-local 视图等),走 **thread-local slot 机制**:主线程广播副本、worker 读本地副本,无锁;**热数据计数(stats)走 `std::atomic`**,也是无锁。一句话:**Envoy 把"会竞争的状态"要么绑死在一个线程上、要么广播副本、要么原子化,就是不让锁出现在数据面的热路径上**。

---

## 〇、一句话点破

> **Envoy 是一个 MainThread 加 N 个 worker 线程的进程:MainThread 负责生命周期、xDS 配置下发、admin,不碰流量;每个 worker 跑一个独立的 libevent 事件循环,扛一部分连接。连接一旦被某 worker accept,就永久绑定该 worker,后续这条连接上所有 filter chain 都在同一个 worker 线程里跑——所以连接内部状态天然无竞争。跨线程要共享的配置类状态,用 thread-local slot 机制广播副本;热路径的计数,用原子操作。Envoy 数据面的热路径上,几乎没有锁。**

这是结论,不是理由。本章倒过来拆:先讲"为什么不用一个线程、不用一个连接一个线程",再讲 MainThread + N worker 这个分工怎么落地、为什么连接必须绑定 worker,然后拆 SO_REUSEPORT 怎么让内核帮 Envoy 分连接,最后讲 thread-local slot 机制为什么 sound,以及一个常被讲错的细节(stats 到底是不是 thread-local 副本)。

---

## 一、为什么不是一个线程,也不是一个连接一个线程

这一章是全书"数据面"的地基第一章。上一章(P0-01)立起了主线——**一条流量穿过一串 filter,filter 由 xDS 动态热更新**。现在第一个问题是:**谁来跑这条 filter chain?** 一个进程要扛几十万连接,它怎么组织自己的线程?

我们先把两种朴素方案排除,再看 Envoy 的选择为什么自然。

### 朴素方案 A:一个线程,跑一个事件循环,处理所有连接

最简单的做法:整个 Envoy 进程只有一个线程,它跑一个 epoll 事件循环(关于 epoll 的事件机制,详见《Tokio》和《Linux 内核》那两本,本书 P1-03 会讲 Envoy 怎么把它封装成 dispatcher,这里不重复),所有连接都挂在这个线程上。

这套在 Redis、Nginx 单 worker 上都验证过——单线程、无锁、简洁。但它有一个绕不开的硬伤:**只用得起一个核**。现代 CPU 几十上百个核,你一个线程顶天跑满一个核,剩下的核全闲着。对 Envoy 这种"要扛海量连接、每个连接还要做 TLS 解密、HTTP 解码、filter chain 跑一串逻辑"的重活,单核根本喂不饱。

> **不这样会怎样**:单线程模型在"每个连接的处理都比较重(TLS + HTTP + 一串 filter)"的代理场景,会成为吞吐天花板。Redis 单线程能撑住是因为它每个操作很短、纯内存;Envoy 每条连接要干的事重得多,单线程远远不够。

### 朴素方案 B:一个连接一个线程(thread-per-connection)

另一种朴素做法:每来一个新连接,就 `pthread_create` 一个线程专门处理它。这是早期 Apache 的 prefork/worker 模型。

这套的问题更致命——**线程数和连接数一样多**。10 万连接就要 10 万线程。每个线程有栈(默认几 MB)、有内核调度开销、有上下文切换开销。10 万线程光栈就要上百 GB 内存,调度器在它们之间切换就能把 CPU 跑飞。而且绝大多数时候,连接是空闲的(等数据),线程却一直占着——纯浪费。

> **不这样会怎样**:thread-per-connection 在 C10K(万级连接)时代就被淘汰了,更别说 C100K、C1M。线程本身是昂贵的资源(栈、调度、内核状态),用它去"等一个空闲连接"是巨大浪费。这就是为什么 Nginx、Envoy、HAProxy 这些高性能代理全都转向了**事件驱动**:少量线程,每线程用 epoll 同时管几千几万个连接。

### 朴素方案 C:一堆线程共享一个连接表,靠锁保护

那能不能"多线程 + 共享所有连接"?比如搞 16 个线程,共享一个全局连接表,谁来事件了谁处理,访问连接表加锁。

这听起来"多核都能干活",但锁竞争会让多核的优势吃光。一个连接的事件触发后,线程要先抢"连接表锁"找到这个连接,再抢"这个连接的状态锁"去跑 filter chain。几十万连接、每秒几十万次事件,这些锁会成千上万线程挤在同一个 cacheline 上弹跳(false sharing + cacheline bouncing),多核反而比单核慢——这就是经典的"加锁扩不出来"。

> **不这样会怎样**:共享 + 锁的方案,在线程数一多时,锁竞争会让性能塌陷。更糟的是,filter chain 跑到一半要改连接状态(写 buffer、改 stream 状态),你不知道哪段代码会撞上别的线程,bug 极难查。这就是为什么现代高性能服务器几乎都避免"共享可变状态"。

### 所以 Envoy 的选择:N 个 worker,每 worker 一个事件循环,连接绑定 worker

Envoy(和 Nginx 一样)走的是**第三条路**:开 N 个 worker 线程(默认 N = CPU 核数,见 [`options_impl.cc:83`](../envoy/source/server/options_impl.cc#L83) 用 `std::thread::hardware_concurrency()`),**每 worker 跑一个独立的 libevent 事件循环**,连接**一旦被某 worker accept,就永久绑定该 worker**,该 worker 独占这条连接的所有处理。MainThread 单独一根线程,只管生命周期、配置下发、admin 接口,**不碰任何数据面流量**。

```
   Envoy 进程(一个进程, 1 + N + 1 线程)
   ┌──────────────────────────────────────────────────────────┐
   │                                                          │
   │  MainThread (1 个)                                       │
   │  ┌────────────────────────────┐                          │
   │  │ libevent dispatcher        │  ← xDS 订阅 / 配置广播   │
   │  │ - 收 xDS、算出 listener/   │  ← admin HTTP / stats 导出│
   │  │   cluster/route 配置        │  ← 生命周期 / hot restart│
   │  │ - 把配置通过 TLS 广播给 worker                       │
   │  └────────────────────────────┘                          │
   │            │ 广播配置副本 (thread-local slot)            │
   │            ▼                                             │
   │  Worker 0          Worker 1        ...   Worker N-1      │
   │  ┌──────────┐      ┌──────────┐         ┌──────────┐     │
   │  │libevent  │      │libevent  │         │libevent  │     │
   │  │dispatcher│      │dispatcher│         │dispatcher│     │
   │  │ 连接 1   │      │ 连接 A   │         │ 连接 X   │     │
   │  │ 连接 2   │      │ 连接 B   │         │ 连接 Y   │     │
   │  │ ...      │      │ ...      │         │ ...      │     │
   │  │ (独占)   │      │ (独占)   │         │ (独占)   │     │
   │  └──────────┘      └──────────┘         └──────────┘     │
   │   线程 0             线程 1                线程 N-1        │
   │                                                          │
   │  GuardDog 线程 (1 个, 看门狗, 不跑流量)                  │
   │  ┌──────────────────────────────────────────┐            │
   │  │ 周期性检查每个 worker/MainThread 是否 touch│            │
   │  │ 超时未 touch → 记 miss/megamiss/kill     │            │
   │  └──────────────────────────────────────────┘            │
   └──────────────────────────────────────────────────────────┘
        所有 worker 都用 SO_REUSEPORT listen 同一端口,
        内核把新连接分发给某个 worker (P2-05 详讲)。
```

这套设计的精妙在于:**它既拿到了多核(每 worker 一个核,各自事件循环),又消灭了数据面的锁竞争(连接绑定 worker,worker 之间不共享连接状态)**。MainThread 只负责"配什么、转发到哪",把这些决策通过 thread-local 副本广播给 worker;worker 只负责"按配置跑流量"。数据面热路径上,没有"两个线程抢同一个连接"的情形。

> **所以这样设计**:Envoy 把"多核扩得出"和"无锁"这两个看似矛盾的目标,用"按连接分片"统一起来——**每条连接是一个独立的王国,从 accept 到 close 都归一个 worker 独占**。worker 之间天然不需要协调"某条连接的状态"。这是后面一切(无锁 filter chain、thread-local 配置副本、零拷贝 buffer)能成立的地基。

> **钉死这件事**:Envoy 的线程模型不是"多线程共享",而是"**分片独占**"。一条连接一生只在一个线程上跑,这是 Envoy 数据面无锁的根。理解了这一点,后面所有"为什么 thread-local 能无锁""为什么 filter chain 不加锁"的问题都迎刃而解。

---

## 二、MainThread + N worker:谁干什么

明确了"分片独占"这个总思路,我们看 Envoy 具体怎么把线程分成 MainThread 和 worker 两类,各自负责什么。这一节回答:**为什么要有专门的 MainThread?它和 worker 怎么协作?**

### MainThread 不跑流量,只管"决策"

Envoy 的 MainThread 是一根特殊的线程。它也跑一个 libevent dispatcher(后面 P1-03 会讲 dispatcher 是什么),但它的 dispatcher 上挂的不是"连接",而是:

- **xDS 订阅**:和控制面(Istio)的 gRPC 双向流(LDS/RDS/CDS/EDS/SDS),收配置更新。这块是控制面,详见第 5 篇。
- **配置算出来后,广播给 worker**:收到一份新的 listener/cluster/route 配置,MainThread 算出"每个 worker 该看到的本地副本",然后通过 thread-local slot 广播下去(本章后面拆透)。
- **admin 接口**:`/stats`、`/config_dump`、`/listeners` 这些管理端点,跑在 MainThread 上(它自己也是一个 HTTP server)。
- **生命周期 / hot restart**:进程启动、优雅退出、hot restart 的 fd 传递(P6-21 讲)。

注意 MainThread **不碰任何数据面流量**:它不 accept 业务连接、不跑 filter chain、不做 TLS 解密。它只管"决定配置应该是什么"。这是一个清晰的职责切分:**决策(配置)在 MainThread,执行(跑流量)在 worker**。

启动时,MainThread 跑进自己的事件循环就不再出来,直到退出:

```cpp
// source/server/server.cc (InstanceBase::run, 简化示意,非源码原文)
void InstanceBase::run() {
  // ... 初始化 listener/cluster/xds 订阅 ...
  // cluster 全部初始化好后,回调里 startWorkers() 起所有 worker
  ENVOY_LOG(info, "starting main dispatch loop");
  dispatcher_->run(Event::Dispatcher::RunType::Block);  // 阻塞在这里
  ENVOY_LOG(info, "main dispatch loop exited");
  terminate();
}
```

MainThread 在 [`server.cc:1087`](../envoy/source/server/server.cc#L1087) 调 `dispatcher_->run(Block)` 阻塞在自己的事件循环里。注意,worker 是在这个 `run()` 之前的初始化阶段(`startWorkers`)就被起好了——worker 和 MainThread **并行运行**,各自跑各自的 dispatcher。

### worker 各跑一个事件循环,扛一部分连接

worker 是真正跑流量的线程。Envoy 启动时,按 `concurrency()`(默认 = CPU 核数)创建 N 个 worker,每 worker 一个独立的 libevent dispatcher、一个 `ConnectionHandler`(管这个 worker 上的所有连接):

```cpp
// source/server/worker_impl.cc (ProdWorkerFactory::createWorker,简化示意)
WorkerPtr ProdWorkerFactory::createWorker(uint32_t index, ...) {
  // 1. 给这个 worker 配一个独立的 libevent dispatcher
  Event::DispatcherPtr dispatcher(api_.allocateDispatcher(worker_name, ...));
  // 2. 配一个 ConnectionHandler,绑定到这个 dispatcher
  auto conn_handler = getHandler(*dispatcher, index, ...);
  return std::make_unique<WorkerImpl>(tls_, hooks_, std::move(dispatcher),
                                      std::move(conn_handler), ...);
}
```

每 worker 一个 `Event::DispatcherPtr`,这就是"每 worker 一个事件循环"的实体(下一章 P1-03 拆 dispatcher 内部)。然后 `WorkerImpl` 构造时,关键一步是**把自己注册到全局的 thread-local 表里**:

```cpp
// source/server/worker_impl.cc (WorkerImpl 构造函数,简化)
WorkerImpl::WorkerImpl(ThreadLocal::Instance& tls, ..., Event::DispatcherPtr&& dispatcher, ...)
    : tls_(tls), ..., dispatcher_(std::move(dispatcher)), handler_(...) {
  tls_.registerThread(*dispatcher_, false);   // ← 把这个 worker 的 dispatcher 注册进 TLS
  // ...
}
```

[`tls_.registerThread(*dispatcher_, false)`](../envoy/source/server/worker_impl.cc#L56) 这一行至关重要——它告诉全局 thread-local 系统:"这个 dispatcher 属于一个 worker 线程,以后 MainThread 要广播配置时,记得往它的 dispatcher 上 `post` 一份"。这就是 MainThread 和 worker 协作的桥梁(本章第四节拆透)。

worker 启动后,在自己的线程里跑 dispatcher 的阻塞循环:

```cpp
// source/server/worker_impl.cc (threadRoutine,简化)
void WorkerImpl::threadRoutine(OptRef<GuardDog> guard_dog, const std::function<void()>& cb) {
  ENVOY_LOG(debug, "worker entering dispatch loop");
  dispatcher_->post([this, &guard_dog, cb]() {
    cb();   // 通知 MainThread "我起来了"
    if (guard_dog.has_value()) {
      watch_dog_ = guard_dog->createWatchDog(...);  // 在 GuardDog 上注册自己
    }
  });
  dispatcher_->run(Event::Dispatcher::RunType::Block);  // ← 阻塞在这个 worker 的事件循环里
  // ... 退出后清理 ...
  handler_.reset();
  tls_.shutdownThread();   // 清理本线程的 TLS 数据
}
```

[`dispatcher_->run(Block)`](../envoy/source/server/worker_impl.cc#L177) 这一行就是"这个 worker 从此活在它的事件循环里"。epoll 一次拿一批事件,每个事件对应一个连接上的可读/可写,dispatcher 调对应的回调(读连接、跑 filter、写响应)。一个 worker 同时管几千几万个连接,但**同一时刻只在一个连接上跑 filter chain**——因为单线程,根本不存在"同一个 worker 上两个连接互相抢"。

worker 创建循环在 [`listener_manager_impl.cc:425`](../envoy/source/common/listener_manager/listener_manager_impl.cc#L425):

```cpp
for (uint32_t i = 0; i < server.options().concurrency(); i++) {
  workers_.emplace_back(worker_factory.createWorker(
      i, server.overloadManager(), server.nullOverloadManager(),
      absl::StrCat("worker_", i)));
}
```

### 为什么 MainThread 要和 worker 分开

一个自然的疑问:为什么不让某个 worker 兼任"管配置"?为什么非要单独一根 MainThread?

因为**配置下发(xDS)和跑流量,对延迟的诉求完全不同**。跑流量要的是极低尾延迟(p99),绝不能被别的事打断;而 xDS 订阅要做的可能是 CPU 密集的事(算一份新的路由表、解析一个大 proto、跑 outlier detection 的统计),这些事如果挤在 worker 上,会拖慢 worker 处理流量。把配置决策剥离到 MainThread,worker 就只管"按已经算好的本地副本忠实执行",不被配置计算打扰。

而且,xDS 订阅是个有状态的 gRPC 长连接、有 ACK/NACK 协商(第 5 篇),把它放在一个专门线程上,状态机清晰、不会被流量回调穿插。MainThread 就是个"专职调度员"。

> **不这样会怎样**:如果把配置下发也放在某个 worker 上,这个 worker 既要处理海量流量、又要做 CPU 密集的配置计算,会出现尾延迟毛刺——某次 EDS 推送算 endpoint 列表时,这个 worker 上的连接全都要等。在生产环境,p99 飙升是大忌。Main/worker 分离把"决策"和"执行"在物理上隔开,各自有自己的事件循环,互不打扰。

> **钉死这件事**:Envoy 的线程分工是 **MainThread 决策(配置)、worker 执行(流量)**,两类线程跑各自的事件循环,通过 thread-local slot 机制单向广播配置。这种分离是"低尾延迟 + 动态配置"能共存的前提。

---

## 三、连接绑定 worker:thread-local 无锁的前提

这一节是全章的关键。前面说"连接一旦被某 worker accept,就永久绑定该 worker",这条**不变式**是 Envoy 整个无锁设计的根。我们要讲清两件事:① 它是怎么实现的(accept 之后,连接的所有权就交给了这个 worker 的 dispatcher);② 为什么有了它,filter chain 上的状态就不需要锁。

### accept 之后,连接归这个 worker 独占

每个 worker 在初始化时,会拿到属于自己的 listener socket(每 worker 一个独立的 listen socket,靠 SO_REUSEPORT,下一节详讲)。当内核把一个新连接分给某 worker 的 listen socket,这个 worker 的 dispatcher 上就触发了"可读"事件,回调进 `ActiveTcpListener::onAccept`:

```cpp
// source/common/listener_manager/active_tcp_listener.cc (简化)
void ActiveTcpListener::onAccept(Network::ConnectionSocketPtr&& socket) {
  if (listenerConnectionLimitReached()) { socket->close(); return; }
  onAcceptWorker(std::move(socket), ...);   // 进 listener filter 链,最终 newConnection
}
```

`onAccept` 之后走到 `ActiveStreamListenerBase::newConnection`,这里关键的一步是用**本 worker 自己的 dispatcher**创建 server 连接对象:

```cpp
// source/common/listener_manager/active_stream_listener_base.cc (newConnection,简化)
void ActiveStreamListenerBase::newConnection(Network::ConnectionSocketPtr&& socket, ...) {
  const auto filter_chain = config_->filterChainManager().findFilterChain(*socket, *stream_info);
  // ...
  auto transport_socket = filter_chain->transportSocketFactory().createDownstreamTransportSocket();
  auto server_conn_ptr = dispatcher().createServerConnection(   // ← 用本 worker 的 dispatcher
      std::move(socket), std::move(transport_socket), *stream_info);
  // ... 给这条连接挂上 network filter chain ...
  config_->filterChainFactory().createNetworkFilterChain(*server_conn_ptr, ...);
  newActiveConnection(*filter_chain, std::move(server_conn_ptr), ...);
}
```

[`dispatcher().createServerConnection(...)`](../envoy/source/common/listener_manager/active_stream_listener_base.cc#L46) 里那个 `dispatcher()`,是**当前 worker 的 dispatcher**。这一步意味着:这个新连接的 fd 被注册到了**当前 worker 的 epoll**上,从此这个 fd 的所有"可读/可写"事件,只会被**这一个 worker 的 dispatcher**捕获并回调。

> **钉死这件事**:一条连接从 `onAccept` → `newConnection` → `createServerConnection` 开始,它的 fd 就绑死在了"accept 它的那个 worker"的 epoll 上。这个 fd 的事件永远不会被别的 worker 看到。**连接 = worker 上的一个回调集合,worker 独占它的一生**。

这条不变式带来一个直接后果:**这条连接上后续所有 filter chain 的执行,都在同一个 worker 线程里**。HCM 解 HTTP、http filter 链(鉴权/限流/router)、cluster 选 endpoint、连接池转发、encoder 链返回响应——所有这些代码,跑的都是"accept 它的那个 worker"的线程。这条连接的 `Connection` 对象、它的 stream 状态、它的 buffer,只被一个线程碰。

### 为什么有了这条不变式,filter chain 就不需要锁

想象 filter chain 上的某个 filter 要改连接状态——比如 HCM 要给当前 stream 加一个 header、router filter 要更新"已转发字节数"。这些状态都挂在**这条连接**上。而这条连接只被一个 worker 线程碰。所以:

- **同一个连接内部的并发 = 0**(单线程)。
- filter 之间传 buffer、改 stream 状态,都是同一个线程内的顺序操作,**天然无竞争**。

这就是为什么你在 Envoy 的 filter 代码里几乎看不到 `std::mutex`、`absl::Mutex`——**不是它们偷偷加了无锁数据结构,而是根本不需要锁,因为状态被连接绑定、连接被 worker 绑定、worker 是单线程**。

> **不这样会怎样**:如果连接不绑定 worker(比如"任何 worker 都能处理任何连接的事件"),那么同一个连接的 filter chain 可能被两个线程同时跑——HCM 正在改 header、另一个线程的 router filter 正在读 header,就要加锁保护连接状态。几万连接每秒几十万次事件,锁竞争会让多核优势蒸发。Envoy 用"连接绑定 worker"这条不变式,把锁消灭在了设计层面,而不是靠"精心加锁"。

### 唯一的例外:connection balancer(罕见的跨 worker 转移)

有一个小例外值得提:Envoy 支持 `connection_balance` 配置(默认不开),允许 worker 之间重新均衡连接数。比如 `ExactConnectionBalancerImpl` 会在 accept 后,如果发现别的 worker 连接数更少,把这条连接"转交"出去。但这个转移是**显式的、靠 post 跨线程投递**完成的,不是"两个线程同时碰一条连接"。而且它**牺牲 accept 吞吐换连接数精确均衡**,只适合"连接数少、长连接"(比如 sidecar egress 的 gRPC),默认不开。源码注释说得很清楚:

```cpp
// source/common/network/connection_balancer_impl.h (注释原文)
// This balancer sacrifices accept throughput for accuracy and should be used
// when there are a small number of connections that rarely cycle
// (e.g., service mesh gRPC egress).
```

所以默认情况下(NopConnectionBalancerImpl),连接**绝不跨 worker**。`ExactConnectionBalancer` 是个可选的、罕见场景的优化。

> **钉死这件事**:"连接绑定 worker"是 Envoy 数据面无锁的**物理基础**。它不是靠"无锁数据结构"做到的,而是靠**架构层面的分片**:每条连接是一个独立王国,从生到死只归一个 worker。filter chain 上的所有状态,因为只被一个线程碰,天然不需要锁。

---

## 四、SO_REUSEPORT:让内核帮 Envoy 分连接

连接要绑定 worker,那第一步——**新连接怎么决定分给哪个 worker?** 这里有两种朴素做法,Envoy 选了第三种。

### 朴素做法 A:主线程 accept,再分发

主线程(M或者某个专门的 acceptor 线程)单独 listen,accept 出一个连接,然后挑一个 worker,把 fd 通过某种 IPC(`sendmsg` 传 fd、或共享 epoll)转交过去。

这套的问题:① 主线程是**单点瓶颈**——所有连接都要经过它;② 转交 fd 有内核开销;③ 经典的**惊群(thundering herd)** 问题如果处理不好,一个连接的到来会唤醒所有等待的 worker。Linux 内核早期 `accept` 在多线程同时 `accept` 同一 fd 时确实会惊群。

### 朴素做法 B:所有 worker 共享一个 listen socket,都来 accept

所有 worker 都在同一 listen fd 上 `epoll_wait` + `accept`。早期 Linux 上这会惊群(一个连接到来,唤醒所有 worker,只有一个 accept 成功,其他白醒)。后来内核加了 `EPOLLEXCLUSIVE` 缓解,但仍有负载不均问题。

### Envoy 的选择:SO_REUSEPORT,每 worker 一个独立 listen socket

Envoy(以及 Nginx 1.9.1+)走的是 **SO_REUSEPORT** 这条路:**每个 worker 都用 SO_REUSEPORT 在同一个 `IP:port` 上各开一个独立的 listen socket**。内核知道这些 socket 监听同一地址,在连接到来时,**内核自己做负载均衡**,把每个新连接分发给某一个 worker 的 listen socket。这个 worker 的 epoll 上就触发可读,它 accept、绑定。

```cpp
// source/common/network/socket_option_factory.cc (简化)
std::unique_ptr<Socket::Options> SocketOptionFactory::buildReusePortOptions() {
  auto options = std::make_unique<Socket::Options>();
  options->push_back(std::make_shared<Network::SocketOptionImpl>(
      envoy::config::core::v3::SocketOption::STATE_PREBIND,
      ENVOY_SOCKET_SO_REUSEPORT, 1));   // ← 内核:这块 socket 上开 SO_REUSEPORT
  return options;
}
```

在 connection handler 里,每 worker 创建自己的 `ActiveTcpListener` 时,**传进去的是"本 worker 索引对应的那个 listen socket"**:

```cpp
// source/common/listener_manager/connection_handler_impl.cc (addListener,简化)
for (auto& socket_factory : config.listenSocketFactories()) {
  details->addActiveListener(
      config, address, ...,
      std::make_unique<ActiveTcpListener>(
          *this, config, runtime, random,
          socket_factory->getListenSocket(worker_index_.has_value() ? *worker_index_ : 0),
          //                          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
          //                          按 worker 索引拿对应的 listen socket(SO_REUSEPORT)
          address, config.connectionBalancer(*address), overload_state),
      ...);
}
```

[`socket_factory->getListenSocket(worker_index_ ...)`](../envoy/source/common/listener_manager/connection_handler_impl.cc#L98) 这一行就是关键:每个 worker 拿的是"自己的那个" listen socket。Envoy 的 listen socket factory 会按 worker index 给每个 worker 一个独立的 fd,它们都绑在同一 `IP:port` 上(因为开了 SO_REUSEPORT,内核允许这种重复绑定)。

```
                   客户端连接 (海量)
                         │
                         ▼
            ┌────────────────────────┐
            │   内核 (TCP 协议栈)     │
            │   收到 SYN,准备 accept │
            └────────────────────────┘
                         │
        SO_REUSEPORT: 内核在所有"监听同一地址"的
        listen socket 之间做 hash/RR 负载均衡
                         │
        ┌────────────────┼────────────────┐
        ▼                ▼                ▼
   worker 0 的       worker 1 的      worker N 的
   listen socket     listen socket    listen socket
   (fd #10)          (fd #11)         (fd #N)
   epoll 触发        epoll 触发       epoll 触发
   accept → 绑定     accept → 绑定    accept → 绑定
   (之后这条连接     (之后这条连接    (之后这条连接
    永远在 worker0)   永远在 worker1)   永远在 workerN)
```

SO_REUSEPORT 的好处是:**负载均衡在内核里做,零用户态开销;每 worker 只看自己的 listen socket,不惊群(因为每个连接只投递给一个 socket);worker accept 后连接天然绑定,不需要任何跨线程投递**。

> **不这样会怎样**:如果用"主线程 accept 再分发",主线程就是单点瓶颈——所有连接都要过它一遍,还要付出 fd 转交的内核开销。SO_REUSEPORT 把"分连接"这件事下沉到内核,主线程完全不用参与 accept,worker 各自独立 accept 自己的那份,完美贴合"连接绑定 worker"的模型。

> **钉死这件事**:Envoy 的连接分发**完全在内核完成**(SO_REUSEPORT)。这不是 Envoy 的发明(Nginx 早就在用),但它是"连接绑定 worker"能高效落地的关键。**SO_REUSEPORT 的内核负载均衡机制、Linux 内核版本差异、和惊群问题的关系,本书 P2-05(Listener 章)会重点拆透,本章只点出这个结论:连接一旦被某 worker accept,就永久绑定该 worker,不跨线程。**

---

## 五、跨线程共享的"配置类"状态:thread-local slot 机制

到这里,数据面的状态(连接、buffer、filter chain)都因为"连接绑定 worker"而天然无锁了。但有一类状态**必须跨线程共享**——**配置**。比如 Runtime feature flags、cluster manager 给每个 worker 看到的"本地 cluster 视图"、gRPC async client 的 per-thread 连接缓存。这些状态由 MainThread 维护,但每个 worker 都要读。如果每次 worker 读配置都去抢一个"全局配置锁",又回到锁竞争的老路。

Envoy 的解法是 **thread-local slot 机制**(`source/common/thread_local/`):MainThread 改了配置,把它"广播"成每个 worker 各自一份的本地副本,worker 之后只读自己的副本,**全程无锁**。这是 Envoy 最有代表性的技巧之一,我们拆透它。

### 朴素地用全局配置 + 锁会撞什么墙

想象不用 thread-local:全局一个 `ClusterManager`,所有 worker 读 cluster 列表时加锁。每次 router filter 要选 cluster(每条请求都要做),就要抢锁读一次。几万 QPS、N 个 worker,这把锁就成了热点——更糟的是,cluster 列表是会变的(EDS 推送 endpoint 上下线),写的时候要持锁遍历更新,把锁持有时间拉长,读侧阻塞更严重。cacheline 在 N 个核之间弹跳,性能塌陷。

### thread-local slot 怎么做:广播副本,worker 读本地

Envoy 的 `ThreadLocal::InstanceImpl` 维护一个全局的 **slot 表**(一个 `vector<Slot*>`),每个 slot 是一个"逻辑的 thread-local 变量"。每个注册过的线程(worker 和 main)都有一个 `thread_local` 的 `ThreadLocalData`,里面是一个 `vector<shared_ptr>`——按 slot index 存这个线程上该 slot 的本地副本。

核心数据结构([`thread_local_impl.h:72`](../envoy/source/common/thread_local/thread_local_impl.h#L72-L75)):

```cpp
struct ThreadLocalData {
  Event::Dispatcher* dispatcher_{};
  std::vector<ThreadLocalObjectSharedPtr> data_;   // ← 本线程各 slot 的本地副本
};
// ...
static thread_local ThreadLocalData thread_local_data_;   // ← 每线程一份
```

工作机制分三步:

**第一步:注册线程。** worker 起来时,`WorkerImpl` 构造函数调 `tls_.registerThread(*dispatcher_, false)`([`worker_impl.cc:56`](../envoy/source/server/worker_impl.cc#L56)),把这个 worker 的 dispatcher 加进全局的 `registered_threads_` 列表。从此这个 worker "在册",MainThread 广播时会带上它。

**第二步:分配 slot。** 某个模块(比如 Runtime、cluster manager)需要跨线程共享数据时,在 MainThread 上调 `tls_.allocateSlot()` 拿一个 slot index:

```cpp
// source/common/thread_local/thread_local_impl.cc
SlotPtr InstanceImpl::allocateSlot() {
  ASSERT_IS_MAIN_OR_TEST_THREAD();
  // ... 复用 free index 或新分配 ...
  return std::make_unique<SlotImpl>(*this, idx);
}
```

注意 `ASSERT_IS_MAIN_OR_TEST_THREAD()` —— slot 只能在 MainThread 上分配。这是为了简化生命周期管理:所有 slot 的创建/销毁都在一个线程上,没有 slot 表本身的竞争。

**第三步:广播(set) + worker 读(get)。** MainThread 算出新的配置后,调 `slot->set(cb)`:

```cpp
// source/common/thread_local/thread_local_impl.cc (SlotImpl::set,简化)
void InstanceImpl::SlotImpl::set(InitializeCb cb) {
  ASSERT_IS_MAIN_OR_TEST_THREAD();
  for (Event::Dispatcher& dispatcher : parent_.registered_threads_) {
    // 给每个 worker 的 dispatcher post 一个任务:算出这个 worker 的本地副本并塞进 TLS
    dispatcher.post(wrapCallback(
        [index = index_, cb, &dispatcher]() -> void {
          setThreadLocal(index, cb(dispatcher));   // cb 在 worker 线程上跑,生成本地副本
        }));
  }
  // MainThread 自己也来一份
  setThreadLocal(index_, cb(*parent_.main_thread_dispatcher_));
}
```

`set` 的精妙在于:**它不是"MainThread 算好副本,再发给 worker"**(那会有跨线程写 worker 的 TLS 的竞争),而是 **`post` 一个任务到每个 worker 的 dispatcher,让 worker 在自己的线程上跑 `cb` 生成本地副本**。`cb` 是 MainThread 传进来的工厂函数,它在 worker 线程上执行,产生一个 `ThreadLocalObject`(比如这个 worker 看到的 cluster 列表快照),然后 `setThreadLocal` 把它存进**本线程的** `thread_local_data_`。

worker 之后读副本,直接调 `slot->get()`,拿的是**本线程的** `thread_local_data_.data_[index]`:

```cpp
// source/common/thread_local/thread_local_impl.cc
ThreadLocalObjectSharedPtr InstanceImpl::SlotImpl::get() {
  return getWorker(index_);   // return thread_local_data_.data_[index_];
}
```

[`get()`](../envoy/source/common/thread_local/thread_local_impl.cc#L99) 就是访问本线程的 `thread_local` 变量——**纯本地内存访问,零锁、零原子**。worker 跑 filter 时,cluster manager 给它的就是这份本地副本,读它没有任何竞争。

### 为什么这套 sound:post 是关键

为什么这套机制是正确的(无数据竞争)?关键是 **`dispatcher.post()`** 这个原语。`post` 是"往某个 dispatcher 的事件队列里塞一个任务,这个任务会在**那个 dispatcher 所属的线程**上、按事件循环的顺序执行"。所以:

- MainThread 调 `set` 时,只是往每个 worker 的 dispatcher 队列里 `post` 一个 lambda——这个动作本身(MainThread 写 worker 的队列)是 dispatcher 内部用一把"队列锁"保护的(下一章 P1-03 拆),这是**唯一的锁,但它只保护队列 push,极短**。
- 真正的"写 worker 的 TLS 数据"(`setThreadLocal`)是 **worker 自己的线程**上跑的——同一个 worker 线程里,先 post 的任务先跑,顺序确定,没有两个线程同时写同一个 worker 的 TLS。

所以整套机制的并发安全,建立在"**每个 worker 的 TLS 数据,只由这个 worker 自己的线程写**"这条不变式上。MainThread 只是"投递任务",不直接碰 worker 的数据。

```
   MainThread                       Worker 0 的 dispatcher 队列         Worker 0 线程
   ──────────                       ──────────────────────────         ─────────────
   slot->set(cb) ───post(lambda1)──▶│ lambda1 │ ... │                    (正在跑别的连接)
                                    │          │     │
                                                                       事件循环转一圈
                                                                       取出 lambda1 执行:
                                                                         副本 = cb()
                                                                         thread_local_data_[idx] = 副本
                                                                         (本地写, 无竞争)
   
   Worker 0 之后 filter 读 slot->get()
     → return thread_local_data_.data_[idx]   (本地读, 无锁)
```

> **不这样会怎样**:如果 MainThread **直接写** worker 的 TLS 数据(而不是 post),就要面对"worker 正在读这个 slot,MainThread 在写"的竞争——必须加锁。而 post 把"写"这件事推迟到 worker 自己的线程上、按它的 event loop 顺序执行,消灭了这个竞争。这是 Envoy thread-local 机制之所以 sound 的核心技巧:**不直接共享数据,而是共享"产生数据的任务"**。

> **钉死这件事**:thread-local slot 机制 = **"主线程广播任务,worker 在自己线程上执行任务生成本地副本,之后 worker 只读本地副本"**。它把跨线程共享可变状态这件事,变成了"无共享"——每 worker 有自己的副本,MainThread 改了就再广播一次。这是 Envoy 数据面热路径无锁的另一半(连接内部状态靠"绑定 worker"无锁,跨线程配置靠"thread-local 副本"无锁)。

### runOnAllThreads:不只是 set,还能"通知"所有 worker

除了 `set`(广播副本),slot 还有 `runOnAllThreads(cb)`——给每个 worker 的 dispatcher `post` 同一个 lambda,让它们各自在自己线程上跑一次。这用于"通知所有 worker 做某事"(比如 flush、reload)。注意它**只能在 MainThread 上调**(`ASSERT_IS_MAIN_OR_TEST_THREAD()`),因为它要遍历 `registered_threads_`,这个列表是 MainThread 拥有的。

```cpp
// source/common/thread_local/thread_local_impl.cc
void InstanceImpl::runOnAllThreads(std::function<void()> cb) {
  ASSERT_IS_MAIN_OR_TEST_THREAD();
  for (Event::Dispatcher& dispatcher : registered_threads_) {
    dispatcher.post(cb);   // 给每个 worker post
  }
  cb();   // MainThread 自己也跑一次
}
```

这就是"MainThread 单向指挥 worker"的标准原语——配置类状态变更,都走这条路。

### 哪些东西真用了 thread-local slot

grep 一下 `allocateSlot()` 的用法,能看到这套机制的广泛使用:

- **Runtime feature flags**:`source/common/runtime/runtime_impl.cc:491` —— Runtime 配置(运行时开关)每个 worker 一份本地视图。
- **cluster manager 的 thread-local 视图**:cluster 上下线时,MainThread 算出新视图,broadcast 给每个 worker,worker 跑 LB 时读自己的本地视图。
- **gRPC async client 的 per-thread 连接**:`source/common/grpc/async_client_manager_impl.cc:71` —— 每 worker 一个 gRPC 客户端缓存,连接复用不跨线程。
- **tracing / access log 的 per-thread 状态**:各种 tracer(access_logger、opentelemetry、zipkin...)都拿一个 slot,缓存 per-thread 的批量数据。
- **tcp_proxy 的 drain manager**:`source/common/tcp_proxy/tcp_proxy.cc:220`。

这些都是"**配置类、读多写少、由 MainThread 维护、worker 高频读**"的状态——thread-local slot 的典型用武之地。

---

## 六、技巧精解:thread-local 无锁归并,以及一个常被讲错的细节

这一章最硬核的两个技巧,单独拆透。

### 技巧一:thread-local slot——"共享任务而非共享数据"

前面已经把机制讲清了,这里把它提炼成一个**可复用的设计模式**,并对比"朴素加锁"的代价。

**模式**:有一个可变状态 S(Runtime 配置 / cluster 视图 / ...),由 MainThread 维护,N 个 worker 高频读。朴素做法是全局 S + 读写锁;Envoy 做法是:

1. MainThread 上分配一个 TLS slot。
2. MainThread 改 S 后,调 `slot->set(factory)`,factory 是"在目标线程上生成本地副本"的函数。
3. `set` 内部对每个 worker 的 dispatcher `post(factory)`,factory 在 worker 线程上跑,产出一个只读副本,存进这个 worker 的 `thread_local_data_`。
4. worker 读时调 `slot->get()`,拿本地副本,零开销。

**为什么 sound**:每个 worker 的 TLS 副本,只由这个 worker 自己的线程写(post 的任务在 worker 上跑)、只由这个 worker 自己的线程读(filter 在 worker 上跑)。读和写发生在同一个线程上,顺序由 event loop 决定,**没有跨线程访问同一内存**,所以没有数据竞争,不需要锁、不需要原子。

**反面对比——朴素地全局加锁会撞什么墙**:

- 假设全局一个 `ClusterManager` + `absl::Mutex`,worker 每次 router filter 选 cluster 都要锁。N 个 worker 高频读,这把 mutex 的 cacheline 在 N 个核之间弹跳(cacheline bouncing)。CPU 缓存失效,内存总线压力飙升,多核扩不出性能。
- EDS 推送(写)时,持锁遍历更新 endpoint 列表,锁持有时间长,所有 worker 的读请求都阻塞,p99 飙升。
- 更阴险的:你想优化成读写锁(`absl::Mutex` + `ReaderLock`),但读写锁的"读"也要原子改 reader count,cacheline 依然在核间弹跳,只是没那么剧烈——治标不治本。

thread-local slot 把这个热点**彻底消解**:worker 读本地副本是纯本地内存访问,根本不碰共享内存;EDS 写在 MainThread 上算完,broadcast 出去,各 worker 异步更新本地副本,期间 worker 用旧副本继续跑流量,**旧副本和新副本不重叠**(各自独立的 `shared_ptr`),无竞争。

**一个细节:still_alive_guard 防止 slot 析构后的悬空调用**。看 `SlotImpl::wrapCallback`([`thread_local_impl.cc:73`](../envoy/source/common/thread_local/thread_local_impl.cc#L73-L84)):

```cpp
std::function<void()> InstanceImpl::SlotImpl::wrapCallback(const std::function<void()>& cb) {
  return [still_alive_guard = std::weak_ptr<bool>(still_alive_guard_), cb] {
    if (!still_alive_guard.expired()) {   // ← 执行前检查 slot 是否还活着
      cb();
    }
  };
}
```

每个 slot 持一个 `shared_ptr<bool> still_alive_guard_`,析构时它被释放。`post` 出去的 lambda 用 `weak_ptr` 捕获它,执行前 `expired()` 检查——这样即使 slot 在任务投递后、执行前被销毁,任务也不会访问悬空状态。这是"异步任务 + 对象生命周期"的经典处理,Envoy 在这里做得相当严谨。

### 技巧二(诚实修正):stats 到底是不是 thread-local 副本?

很多 Envoy 资料和本书的总纲/提示词里,都说"stats 用 thread-local 副本归并,每 worker 各存一份"。**这是一个简化叙述,和当前源码不符**,值得在这里讲清,因为它直接关系到你对"热数据无锁"的理解。

实际看源码,Envoy 的 counter/gauge 实现 [`source/common/stats/allocator.cc`](../envoy/source/common/stats/allocator.cc#L141-L169):

```cpp
class CounterImpl : public StatsSharedImpl<Counter> {
  // ...
  void add(uint64_t amount) override {
    value_ += amount;            // ← std::atomic<uint64_t> 的 +=,无锁原子操作
    pending_increment_ += amount;
    flags_ |= Flags::Used;
  }
  void inc() override { add(1); }
  uint64_t latch() override { return pending_increment_.exchange(0); }
  // ...
private:
  std::atomic<uint64_t> value_{0};
  std::atomic<uint64_t> pending_increment_{0};
};
```

**counter 是一个全局的、跨所有 worker 共享的 `std::atomic<uint64_t>`**,不是"每 worker 一个副本然后归并"。`inc()` 就是原子 `+=`,所有 worker 都 `fetch_add` 到同一个原子变量上。这是无锁的(atomic 是无锁的,在 x86 上是单条 `lock inc` 指令),但**不是 thread-local 副本**。

> **注:此处为 `std::atomic` 实现,非老资料 / 总纲简化叙述里所说的"每 worker thread-local 副本再归并"**。Envoy 早期版本可能有过 thread-local 攒批的探索,但当前 master(`df2c77d`,1.39.0-dev)的 counter/gauge 主路径就是原子操作。这是写作时 Grep 源码发现的与印象不符之处,以源码为准。

那为什么大家都说"stats 无锁"?因为 `std::atomic` **本身是无锁的**(lock-free)。所以结论("stats 是无锁的")对,但机制("靠 thread-local 副本归并")讲错了——靠的是原子操作。原子操作虽然无锁,但**多核 fetch_add 同一个原子变量,仍然会有 cacheline 弹跳**(原子写的 cacheline 在核间 invalidate 传递),所以高 QPS 下统计依然有跨核开销。这是为什么有些系统(Nginx、perf 工具)会用"per-cpu/per-thread 计数 + 归并"来彻底消灭这个弹跳。Envoy 没走这条路(为了简化、为了 latch/export 的实时性),而是直接用原子——**足够快,因为统计的写入频率远低于连接事件频率,且 atomic 在现代 CPU 上已经很快**。

那么"thread-local 归并"在 stats 里有没有用武之地?有,但在**别的层**:

- **`StatMerger`**(`source/common/stats/stat_merger.cc`):用于 **hot restart** 时,把**子进程**(新 Envoy)的 counter 和**父进程**(旧 Envoy)遗留的 counter 累加起来——因为 counter 是单调增的,新进程的 counter 从 0 开始,要继承父进程的存量才不丢统计。这里的 "merge" 是进程间的,不是线程间的。
- **per-worker listener 统计**:`PerHandlerListenerStats`(每 worker 各一份 `downstream_cx_total` 等,见 [`active_listener_base.h:21`](../envoy/source/server/active_listener_base.h#L21-L24)),这部分确实是 per-worker 的,但归并到 admin 视图时也是靠原子。
- **histogram**:`source/common/stats/histogram.cc` 用 thread-local 攒批(buffer 一批样本,定期 flush),这是为了减少全局 histogram 的写入频率——**这才是真正用 thread-local 攒批的地方**,counter/gauge 不是。

所以准确的图景是:

| 状态类型 | 机制 | 为什么 |
|---------|------|--------|
| 连接内部状态(buffer/stream/filter) | 绑定 worker(单线程独占) | 连接绑定 worker,天然无竞争 |
| 配置类共享(Runtime/cluster 视图) | thread-local slot 广播副本 | 读多写少,广播副本避免锁 |
| counter / gauge | `std::atomic`(共享原子变量) | 写入相对不频繁,原子足够 |
| histogram 样本 | thread-local 攒批 + 定期 flush | 高频写入,攒批减少全局竞争 |
| hot restart 时 counter 继承 | StatMerger(进程间 merge) | counter 单调,新进程要继承父进程存量 |

> **钉死这件事**:Envoy 数据面热路径无锁,靠的是**三套不同机制配合**——连接绑定 worker(消灭连接内竞争)、thread-local slot(消灭配置读竞争)、`std::atomic`(消灭 counter 竞争,虽然原子写有 cacheline 弹跳但可接受)。说"Envoy stats 是 thread-local 副本归并"是简化叙述,准确说法是"counter 用原子操作,histogram 才用 thread-local 攒批"。**这是 Grep 源码发现的与许多资料印象不符之处,以源码为准。**

---

## 七、GuardDog:worker 卡住了怎么办

讲完正常运行的线程模型,补一个"异常兜底":**GuardDog**。它是 Envoy 的看门狗机制,跑在一根单独的线程上,盯着所有 worker 和 MainThread——如果某根线程卡住(死循环、死锁、被信号阻塞),GuardDog 会发现并采取行动(记 metric、abort 进程触发重启)。

GuardDog 不是数据面的一部分,但它是 Envoy 线程模型的"安全网",所以放这里讲。

### 怎么看:每个 worker 周期性 touch,超时就报警

每个 worker 起来时,通过 `guard_dog->createWatchDog(...)` 注册自己([`worker_impl.cc:173`](../envoy/source/server/worker_impl.cc#L170-L176)):

```cpp
// source/server/worker_impl.cc (threadRoutine 里)
dispatcher_->post([this, &guard_dog, cb]() {
  cb();
  if (guard_dog.has_value()) {
    watch_dog_ = guard_dog->createWatchDog(
        api_.threadFactory().currentThreadId(), dispatcher_->name(), *dispatcher_);
  }
});
```

`createWatchDog` 会给这个 worker 起一个定时器,让 worker 在自己 event loop 里周期性地 `touch()`(表示"我还活着")。GuardDog 线程在它自己的 dispatcher 上跑一个周期 timer,每次到点 `step()`——遍历所有被监视的线程,看它们最后一次 touch 距今多久:

```cpp
// source/server/guarddog_impl.cc (step,简化)
void GuardDogImpl::step() {
  // ... for 每个 watched_dog:
  if (watched_dog->dog_->getTouchedAndReset()) {       // 这段时间 touch 过
    watched_dog->last_checkin_ = now;                  // 更新 checkin 时间
    continue;
  }
  auto delta = now - watched_dog->last_checkin_;
  if (delta > miss_timeout_)     { ... watchdog_miss_counter_.inc(); ... }      // 轻微超时
  if (delta > megamiss_timeout_) { ... watchdog_megamiss_counter_.inc(); ... }   // 严重超时
  if (killEnabled() && delta > kill_timeout_) {
    invokeGuardDogActions(WatchDogAction::KILL, {{tid, last_checkin}}, now);   // 直接 kill
  }
  // ... 多个线程同时卡 → MULTIKILL ...
}
```

如果某 worker 超过 `miss_timeout`(默认 200ms)没 touch,记一个 `watchdog_miss` counter;超过 `megamiss_timeout`(默认 1000ms)记 `watchdog_mega_miss`;超过 `kill_timeout`(默认 0=禁用)就触发 kill 动作(默认是 abort 进程,让外层 supervisor/systemd 重启)。多个线程同时卡到 multikill 阈值,触发 MULTIKILL——因为"多个线程一起卡"更可能是全局死锁,得重启。

### 为什么需要 GuardDog

Envoy 是个单进程多线程程序,一根线程卡住(filter 死循环、某个第三方扩展死锁、系统调用阻塞),不一定会让整个进程崩,但会让那部分流量卡死。没有看门狗,这种"半死不活"的状态能持续很久才被发现。GuardDog 让 Envoy **自检**:卡住就主动暴露(miss counter 暴涨)甚至主动重启,比"用户投诉才发现 envoy 卡了"强得多。

这也是为什么 Istio/Service Mesh 场景下,人们常把 `kill_timeout` 配上——让卡住的 Envoy sidecar 自动重启,而不是拖着半死的 sidecar 继续接流量。

> **钉死这件事**:GuardDog 是 Envoy 线程模型的**安全网**:worker/MainThread 周期性 touch,GuardDog 监督,超时就报警/kill。它不参与跑流量,但保证了"线程卡住能被发现"。这是生产级多线程服务器的标配。

---

## 八、架构演进:libevent 是默认,io_uring 在并行探索

诚实交代一下演进。Envoy 当前(`df2c77d`,1.39.0-dev)的事件引擎,**默认仍是 libevent**(底层 epoll)。看 dispatcher 的实现:

```
   source/common/event/
     dispatcher_impl.cc      ← DispatcherImpl,libevent 封装
     libevent.cc / libevent.h
     libevent_scheduler.cc   ← event_base_new() 创建 libevent event_base
     file_event_impl.cc      ← fd → libevent 事件回调
```

[`libevent_scheduler.cc:26`](../envoy/source/common/event/libevent_scheduler.cc#L26) 里 `event_base_new_with_config` / `event_base_new` 就是创建 libevent 的 event_base,所有 fd 注册、epoll_wait 都走 libevent。这是 Envoy 自诞生以来的事件引擎,稳定可靠。

**io_uring 的探索**:Envoy 在 `source/common/io/` 下有一套 io_uring 的实现(`io_uring_impl.cc`、`io_uring_worker_impl.cc`),用于实验性把某些 IO 操作(读、写、连接)走 io_uring 异步提交。但**它不是默认路径**——目前主要用于特定场景(比如文件 IO、某些 experimental feature),worker 的主事件循环仍是 libevent。io_uring 是 Linux 5.1+ 的新异步 IO 接口,理论性能优于 epoll(批量提交、内核态轮询),但 Envoy 的全面切换需要重写大量 IO 路径,目前仍在并行实验阶段。

> 本书 P1-03 会专门拆 libevent dispatcher(默认事件引擎),涉及 io_uring 处会标注"实验性,非默认"。**老资料如果说"Envoy 用 epoll"——对,但更准确说是"libevent 封装的 epoll";如果说"Envoy 已经切到 io_uring"——错,目前仍是实验。**

---

## 九、章末小结

### 回扣主线

本章是全书"数据面"地基的第一章。它回答了"**谁来跑 filter chain**"——

- **数据面这一面**:worker 线程 + 它的事件循环 + 绑定在它上面的连接。filter chain 跑在 worker 线程上。
- **控制面这一面**:MainThread 负责 xDS 订阅、配置广播。MainThread 把决策通过 thread-local slot 投递给 worker。

这正好接上 P0-01 末尾的悬念:"谁来跑这条 filter chain?"——答案是 **N 个 worker,每 worker 一个事件循环,连接绑定 worker,跨线程配置走 thread-local 副本,热路径无锁**。这套地基立起来之后,下一章(P1-03)我们钻进 worker 里那个 dispatcher,看一个 worker 怎么同时处理几千个连接。

### 五个为什么

1. **为什么 Envoy 不用一个线程跑所有连接?**——单线程只用得起一个核,Envoy 每条连接要做 TLS + HTTP + 一串 filter,单核喂不饱海量连接;N 个 worker 才能扩到多核。

2. **为什么不用一个连接一个线程?**——线程是昂贵资源(栈几 MB、调度开销),10 万连接 = 10 万线程,内存爆炸、调度瘫痪;事件驱动让少量线程管海量连接才是高性能代理的正道(Nginx/HAProxy 同理)。

3. **为什么连接一旦被某 worker accept 就永久绑定该 worker?**——这是数据面无锁的物理基础:连接内部状态(stream/buffer/filter)只被一个线程碰,天然无竞争,filter 代码不需要加锁。Envoy 把锁消灭在设计层面(分片独占),而不是实现层面(无锁数据结构)。

4. **为什么用 SO_REUSEPORT,而不是主线程 accept 再分发?**——主线程 accept 是单点瓶颈,fd 转交有内核开销,还可能惊群;SO_REUSEPORT 让内核在多个 listen socket 间做负载均衡,零用户态开销、不惊群、accept 后天然绑定 worker。(细节 P2-05 拆)

5. **为什么配置类状态用 thread-local slot,counter 用原子?**——配置读多写少,广播副本让 worker 读本地副本零开销;counter 写入相对不频繁,`std::atomic` 足够无锁(虽有 cacheline 弹跳但可接受),histogram 高频写入才用 thread-local 攒批。Envoy 对不同特性的状态,选了不同的无锁策略,不是一刀切。

### 想继续深入往哪钻

- **想看 thread-local slot 源码**:`source/common/thread_local/thread_local_impl.cc`(整套 slot 机制)、`include/envoy/thread_local/thread_local.h`(接口)。grep `allocateSlot` 看哪些模块在用。
- **想看 worker 怎么起**:`source/server/worker_impl.cc`(`threadRoutine` 是 worker 主循环)、`source/common/listener_manager/listener_manager_impl.cc:425`(worker 创建循环)。
- **想看 MainThread 主循环**:`source/server/server.cc` 的 `InstanceBase::run`(around L1066)。
- **想看 SO_REUSEPORT 怎么配**:`source/common/network/socket_option_factory.cc`、`source/common/listener_manager/listener_impl.cc`(buildConnectionBalancer / UDP listener factory);本书 P2-05 详讲。
- **想看 stats 真实实现**:`source/common/stats/allocator.cc`(CounterImpl/GaugeImpl 是 atomic)、`source/common/stats/thread_local_store.cc`(stats store)、`source/common/stats/stat_merger.cc`(hot restart merge)、`source/common/stats/histogram.cc`(histogram thread-local 攒批)。
- **想看 GuardDog**:`source/server/guarddog_impl.cc`、`source/server/watchdog_impl.cc`。
- **想看 io_uring 实验进度**:`source/common/io/io_uring_impl.cc`、`io_uring_worker_impl.cc`(注意是实验性,非默认)。

### 引出下一章

我们搞清楚了"**谁来跑 filter chain**"——N 个 worker,每 worker 一个事件循环,连接绑定 worker。但"事件循环"到底是什么?一个 worker 怎么同时盯几千个连接的 fd,谁的 fd 可读了就去读?这就是 **libevent dispatcher** 干的事——它把 epoll 封装成"注册 fd + 回调"的模型,把"被动等事件"变成"事件来了主动调你的代码"(反转控制)。下一章 P1-03,我们钻进 dispatcher,看一个 worker 的事件循环是怎么转起来的。

> **下一章**:[P1-03 · 事件引擎:libevent dispatcher](P1-03-事件引擎-libevent-dispatcher.md)
