# 第 6 章 · TransportConnector:L4/TLS 连接与复用

> 第 2 篇 · 转发设施·upstream 连接池 · 数据面招牌章

## 章首 · 核心问题

这一章只回答一个问题:

> **到 upstream 的 TCP/TLS 连接怎么建起来、用完怎么复用、复用前怎么确认它还没死?**

这听起来是三个问题,其实是同一个问题的三个面。一条代理请求从 downstream 进来,经过钩子链(`ProxyHttp` 的 `request_filter`/`upstream_peer`/...),最终一定要把字节送到 upstream——而送字节的前提,是手里有一条**活的、到目标 peer 的连接**。这条连接从哪来?最朴素的答案是"每次都新建一条":对每个请求 `connect()` 到 upstream,发完请求收完响应,`close()`。这能跑,但跑不快——TCP 三次握手 + TLS 握手(尤其 TLS 1.2/1.3 的若干 RTT)是每个请求都要付出的固定代价,在内网这是毫秒级,在跨地域的长肥管道上是几十毫秒甚至上百毫秒。一个每秒百万请求的代理,如果每请求都新建连接,光握手就要烧掉惊人的 CPU 和带宽。

所以代理都必须**复用连接**:用完不关,放回池子,下次同一个 peer 的请求直接从池子里捞一条出来接着用。这就是连接池(connection pool)。可一旦复用,立刻冒出一堆新问题:

1. **池子长什么样?** 一个全局大池?按 peer 分桶?桶里是 LRU 还是 FIFO?
2. **怎么知道哪条连接是"同一个 peer"的?** TCP 连接是四元组(本地 addr, 本地 port, 远端 addr, 远端 port),可代理要复用的是"到同一个 upstream 服务"的连接,这个"同一个"按什么 key 判定?
3. **从池子里捞出来的连接,怎么确认还活着?** 这是最阴险的问题。TCP 是一个**沉默的协议**——对端崩溃、链路中断、NAT 表项过期,本地 socket 在很长一段时间内**毫不知情**,`write()` 以为成功(本地内核缓冲区收下了),`read()` 一直阻塞等着永远不来的数据。你把这样一条"半开"(half-open)的连接从池子里捞出来发给请求,第一个 `write` 可能成功(骗过你),然后 `read` 就卡死,请求超时,用户看到 502。**连接池最大的坑,不是池满了,是把死连接当活连接复用了**。
4. **空闲太久要不要关?** 一条连接放了 10 分钟没人用,服务器可能早就因为它空闲而关了(或者中间的防火墙 NAT 表项过期了),再复用就是死连接。
5. **建连这种 CPU 密集活要不要在请求线程做?** TLS 握手是非对称运算,ClientHello 之后的证书验证、密钥派生是实打实的 CPU 开销,几百条并发握手能瞬间把 reactor 线程打满,挤掉别的请求的处理时间。

这一章就把 `TransportConnector`(`pingora-core/src/connectors/mod.rs`)怎么回答这五个问题拆透。它是 Pingora 数据面第一个真正承重的组件——`upstream_peer` 钩子选完 peer 之后,框架调 `get_stream(peer)`,这一步要么从池子里复用、要么新建,返回一条 `Stream`(就是 `Box<dyn AsyncRead + AsyncWrite + ...>`),后续 HTTP 协议层(P2-07/P4-12~14)就在这条 Stream 之上跑。

读完本章你会明白:

1. **`TransportConnector` 的池是怎么组织的**——它不是一把大锁守一个大 HashMap,而是 `RwLock<HashMap<GroupKey, Arc<PoolNode>>>` + 每个 PoolNode 内部一个 `crossbeam::ArrayQueue` 无锁"热队列"(只有 16 槽)+ 一个 `Mutex<HashMap>` 兜底,外加一个 thread-local 的 LRU 做全局容量限制。这套结构是为了"高并发、高 RPS 下连接被极频繁地借还"专门设计的,锁争用是头号敌人。
2. **`reuse_hash` 这个 64 位 key 把哪些字段哈希进去了**——地址、SNI、scheme、proxy、客户端证书、`max_h2_streams`……所有"影响这条连接能不能被另一个请求复用"的字段都在。理解了这个,你就理解了"什么时候两条连接算同一条"。
3. **`test_reusable_stream` 怎么用 1 字节非阻塞读探测连接死活**——这是连接池最招牌的技巧。它不真读数据(那会消费掉真实响应),它只想知道"对端有没有悄悄发点什么、或者干脆关了"。非阻塞读 1 字节:读到 0 字节 = EOF 连接已关;读到 1 字节 = "对端主动发了数据"(对 idle 连接这是异常,服务器不该先说话);什么都没立刻返回 = 连接还活着。这一招对照 hyper 的 keepalive 探测、Nginx 的 `proxy_next_upstream off`,你会看到不同代理对"死连接问题"的取舍。
4. **`offload_threadpool` 为什么把建连/TLS 握手 offload 到独立线程池**——建连和 TLS 握手是 CPU 密集 + 可能阻塞的活,如果在 reactor 线程(也就是处理别的请求的线程)上做,会拖慢整个线程上的所有 task。Pingora 的解法是 spawn 一组专用的 `current_thread` runtime(不做 work stealing),把建连 future 投递过去执行,执行完再把 Stream 拿回来。这承接了 Tokio `spawn_blocking` 的思想(承《Tokio》一句带过指路 `[[tokio-source-facts]]`),但有自己的取舍。
5. **`PreferredHttpVersion` 为什么要在连接池层记住"这个 peer 该用 h1 还是 h2"**——HTTP/2 协商靠 ALPN,可有些服务器 ALPN 协商行为古怪(声称支持 h2 但握手后表现不正常),Pingora 允许业务在尝过一次后"标记"这个 peer 以后永远走 h1(`prefer_h1`),避免每次复用都踩同一个坑。

> **逃生阀**(这章并发细节密集,先读这一段)。
>
> 如果你对 Tokio 的 `AsyncRead`/`AsyncWrite`/reactor/mio、`spawn_blocking`、`Arc<Mutex>` 完全陌生,这一章会很吃力。本章假设你读过《Tokio》(IO 模型/任务调度/`spawn_blocking` 语义)和本书 P1-02~05(`ProxyHttp` 钩子链,尤其 `upstream_peer` 返回 `HttpPeer`)。涉及 Tokio 内部机制(reactor 的 epoll edge-triggered、`AsyncRead::poll_read`、budget=128 让出),一律一句带过指路 `[[tokio-source-facts]]`,篇幅全留 Pingora 独有。如果你只想抓住一句话:**`TransportConnector` = 一个按 peer 分桶的连接池,复用前用 1 字节非阻塞读探活,建连这种重活可以 offload 到专用线程池**。

## 章首 · 一句话点破

> **`TransportConnector` 的全部秘密:同一个 peer 用过的连接别扔,放回池子(`Arc<Mutex<Stream>>`),下次同 peer 再来先去池子里捞;捞出来别直接用,先 `test_reusable_stream` 用 1 字节非阻塞读戳一下——戳出 EOF/异常数据就丢掉重建,戳不出(连接还沉默着)就接着用。建连/TLS 握手这种 CPU 密集活,可选地 offload 到一组不做 work stealing 的 `current_thread` runtime 里跑,别让它在 reactor 线程上拖慢别人。**

这是结论。本章倒过来拆:先看朴素方案为什么会把死连接复用出去、为什么建连会拖慢 reactor,再看 hyper/Nginx 怎么处理这两个问题,最后看 Pingora 的池结构、`test_reusable_stream`、`offload_threadpool` 怎么各自落地。

---

## 正文

### 6.1 痛点:为什么连接复用不是"放个 HashMap 那么简单"

#### 6.1.1 一个真实场景:反向代理到一万个后端

设想你用 Pingora 写一个网关,后面挂了一万个后端(可以是物理机、容器、也可以是上游的另一个代理)。每秒来了 100 万个请求,`upstream_peer` 钩子根据负载均衡把它们分散到这一万个后端。最朴素的实现:

```rust
// (示意,朴素写法,反例)
async fn proxy(req: Request) -> Response {
    let peer = load_balancer.select();
    let stream = TcpStream::connect(peer.address()).await?;   // 每次都新建
    let resp = send_req_over(stream, req).await?;
    stream.shutdown();   // 用完就关
    resp
}
```

这个能跑。但每个请求付出:① TCP 三次握手(1 个 RTT,跨地域几十毫秒);② 如果是 HTTPS,TLS 握手(TLS 1.2 是 2 个 RTT,TLS 1.3 是 1 个 RTT,加上非对称运算的 CPU 时间);③ 拥塞控制冷启动(新连接的 cwnd 从很小开始,头几个包慢)。100 万 QPS × 每请求新建连接 = 每秒 100 万次握手,这在任何合理的硬件上都跑不动。

所以代理都必须复用连接:**用完不关,放回池子,下次同 peer 直接捞出来接着用**。TCP/TLS 握手分摊到几百个请求上,每个请求只付"已经在用的连接发一笔数据"的代价。

#### 6.1.2 朴素池:为什么简单的 HashMap 会出问题

最朴素的池长这样:

```rust
// (示意,朴素写法,反例)
struct NaivePool {
    conns: Mutex<HashMap<u64 /* peer hash */, Vec<Stream>>>,
}
```

借:`lock()`,`pop()` 一条;还:`lock()`,`push()` 回去。这看起来对,实际跑起来全是问题。

**问题一:锁争用。** 100 万 QPS,每个请求都要借一条、还一条,意味着每秒 200 万次 `lock()`。一把全局 `Mutex` 守一个 `HashMap`,所有请求在锁这里排队,把并发打回了串行。这是连接池设计的头号敌人。

**问题二:死连接。** 这是更阴险的问题。设想这个时序:

```text
T0: 请求 A 借连接 C,发请求,收响应,还连接 C 进池子。
T1: 连接 C 在池子里空闲。
T2: 服务器端崩溃/重启,或者中间的 NAT 表项过期,或者链路被防火墙 reset。
     ——关键:本地 socket 完全不知道。TCP 不会主动告诉你对端没了。
T3: 请求 B 借连接 C(从池子里捞出来),发请求。
     write() 成功(内核缓冲区收下了,你以为发出去了)。
T4: 请求 B 等 read() 等响应——永远不会来。
T5: 请求 B 超时,用户看到 502。
```

这就是**半开连接(half-open connection)**问题。TCP 的设计哲学是"沉默的可靠":它不主动报告链路状态,只在你 `write`/`read` 失败时(比如收到 RST、或者 keepalive 探测超时)才发现连接断了。可一条 idle 连接放在池子里,没人 `write` 也没人 `read`,对端在 T2 时刻没了它毫不知情。等你 T3 复用它,`write` 可能成功(本地内核收下了),`read` 就卡死——因为对端的 RST 可能要等下次轮询才到、或者干脆永远到不了(链路中断没发 RST)。

朴素池没有任何机制检测这个。结果就是:复用率越高,踩中死连接的概率越大。这在生产环境是个高频故障——Cloudflare 的博客专门讲过,他们用 Pingora 替换掉原来的代理后,死连接复用造成的 502 大幅下降,核心改进就在 `test_reusable_stream`。

**问题三:空闲连接堆积。** 服务器为了自保,通常会对 idle 连接设超时(比如 60 秒不发数据就关)。如果你池子里的连接放太久没被借走,服务器那边早关了,你这边还当宝贝存着,捞出来还是死连接。同理,中间路径上的防火墙/NAT 也有 idle 超时,表现一样。

**问题四:建连阻塞 reactor。** 这是个独立但同样重要的问题。`TcpStream::connect` 在内核里可能要等一个 RTT 才完成(异步,但 future 要被反复 poll);TLS 握手更重——非对称运算(ECDHE 的椭圆曲线运算、证书链验证)是实打实的 CPU 时间。如果你的 reactor 线程(也就是处理别的请求的线程)同时要跑 1000 个 TLS 握手,这些 CPU 密集的握手计算会挤占 reactor poll 别的 task 的时间,导致本来已经 ready 的别的连接的 `read`/`write` 被延迟,产生连锁的超时。

> **钉死这件事**:连接池的四大痛点——① 锁争用(全局锁把并发打回串行);② 死连接复用(TCP 沉默,对端断了本地不知情);③ 空闲堆积(服务器/中间设备 idle 超时);④ 建连阻塞 reactor(TLS 握手 CPU 密集)。这四个问题决定了连接池的设计不是"放个 HashMap"那么简单。`TransportConnector` 的全部设计,都是冲着这四个问题去的。

### 6.2 承接方怎么做:hyper 连接池、Nginx、Tokio spawn_blocking

Pingora 不是第一个解决这些问题的。先看承接方/对照方怎么做,这能让 Pingora 的取舍显形。

#### 6.2.1 hyper 的连接池:per-host, idle 探测靠"读就 EOF"

hyper(`hyper-util` 里)的连接池和 Pingora 思路同源,但细节不同。hyper 的池按 host 分桶,每条 idle 连接关联一个 idle watcher。hyper 复用前的探活,本质和 Pingora 类似——它假设"服务器不会主动发数据",所以一旦 idle 连接上能 `poll_read` 出 0 字节(EOF)或出错,就认为连接死了,丢弃。

但 hyper 的池和 Pingora 有几个关键差异:

- **hyper 的池是 HTTP client 层的**——它服务的是 `hyper::Client`,一个发请求的角色。Pingora 的 `TransportConnector` 服务的是**代理**,代理要复用的不是"自己作为 client 的连接"那么简单(还要考虑 proxy 链路、TLS 后端可插换、SNI 多变等)。
- **hyper 的池上层是 Service 模型**(`SendRequest::poll_ready` 管背压),Pingora 的池上层是 `ProxyHttp` 钩子链(代理一个请求,不在 Service trait 框架里)。这是同级对照(承《hyper》,一句带过指路)。
- **hyper 没有 offload threadpool**——hyper 是 HTTP 库,建连/TLS 都在调用方的 runtime 上跑。Pingora 是个完整的代理框架,自己控制运行时,所以能决定是否 offload。

#### 6.2.2 Nginx:连接复用 + `proxy_next_upstream` 兜底

Nginx 的 upstream keepalive(`proxy_http_version 1.1; proxy_set_header Connection "";`)开启后,到 upstream 的连接也会复用。Nginx 的探活策略和 Pingora 不同:

- **Nginx 不主动探活**,它的策略是"复用失败就让重试兜底"。一条死连接复用出去,发请求失败(写失败或读超时),Nginx 会按 `proxy_next_upstream` 的配置决定是否换一个 upstream 重试。这把"死连接"问题从"预防"挪到了"事后补救"。
- **代价**:每次踩中死连接都要付出一次失败 + 重试的代价(用户感知到延迟抖动)。Pingora 走的是"预防"路线——复用前先用 `test_reusable_stream` 戳一下,大概率把死连接过滤掉,避免请求真的发出去再失败。

这两种策略各有取舍。Nginx 的好处是简单(不探活,省一次系统调用),坏处是踩坑的请求要付失败代价。Pingora 的好处是请求大概率不被死连接坑到,坏处是每条复用前都要付一次非阻塞 read 的代价(一个 syscall,但极便宜)。

> **钉死这件事**:连接池的"死连接"问题有两条路——① **预防**(复用前探活,Pingora 的 `test_reusable_stream`);② **兜底**(踩坑了重试,Nginx 的 `proxy_next_upstream`)。Pingora 选了预防,因为代理场景下"踩坑"的代价是用户可见的 502 或延迟抖动,而探活的代价是一次极便宜的非阻塞 read。这是 Cloudflare 在 40M+ req/s 规模下验证过的取舍。

#### 6.2.3 Tokio `spawn_blocking`:CPU 密集活不要在 reactor 线程做

建连/TLS 握手这种 CPU 密集活,通用解法是 Tokio 的 `spawn_blocking`——把一个同步的、CPU 密集的、或者可能阻塞的 future 投递到一个独立的 blocking 线程池里跑,reactor 线程不被它阻塞。这个思想承接《Tokio》(一句带过指路 `[[tokio-source-facts]]`),Pingora 的 `offload_threadpool` 是它的特化版本——但 Pingora 没有用 `spawn_blocking`(那是给**同步**阻塞代码用的),而是 spawn 一组**独立的 `current_thread` runtime**,把**异步**的建连 future 投递过去。区别和动机后面 6.5 详讲。

### 6.3 所以 Pingora 这么设计:TransportConnector 全貌

现在看 `TransportConnector` 的真实结构。先看类型定义(`pingora-core/src/connectors/mod.rs#L142-L149`):

```rust
/// [TransportConnector] provides APIs to connect to servers via TCP or TLS with connection reuse
pub struct TransportConnector {
    tls_ctx: tls::Connector,
    connection_pool: Arc<ConnectionPool<Arc<Mutex<Stream>>>>,
    offload: Option<OffloadRuntime>,
    bind_to_v4: Vec<SocketAddr>,
    bind_to_v6: Vec<SocketAddr>,
    preferred_http_version: PreferredHttpVersion,
}
```

六个字段,每个对应一个职责:

- **`tls_ctx: tls::Connector`**——TLS 上下文。`tls::Connector` 在 boringssl/openssl 后端就是 `SslConnector`(OpenSSL 的 connector builder),它持有 CA、客户端证书、调试用的 SSLKEYLOG 设置等。所有到 TLS upstream 的连接共用这一个 connector(它内部是 `Arc`,clone 廉价)。
- **`connection_pool: Arc<ConnectionPool<Arc<Mutex<Stream>>>>`**——连接池本体。注意池里存的不是裸 `Stream`,而是 `Arc<Mutex<Stream>>`。为什么用 `Arc<Mutex>` 包一层?因为池子要给 idle watcher 一份引用(它要在后台监视这条连接),同时借出去给请求用一份,**两边要互斥**(同一个时刻只能一边操作这条连接)。`Arc<Mutex<Stream>>` 让"池子持有 + 后台 idle 监视 + 借出给请求"三方共享同一条连接的所有权,且用 `Mutex` 保证同一时刻只有一方在读写。这是连接池能正确工作的关键设计,6.4 详讲。
- **`offload: Option<OffloadRuntime>`**——可选的 offload 线程池。`None` 表示建连/TLS 握手就在当前 reactor 线程做;`Some` 表示 offload 到专用线程池。这是 `ConnectorOptions::offload_threadpool` 配置项控制的。
- **`bind_to_v4: Vec<SocketAddr>` / `bind_to_v6: Vec<SocketAddr>`**——本端绑定的源地址列表。代理经常要指定"从哪个本地 IP 出去"(比如为了走特定的网络接口、或者绕开某些路由),`bind_to_random`(`l4.rs#L213`)会从列表里随机挑一个。
- **`preferred_http_version: PreferredHttpVersion`**——记住"某个 peer 该用 h1 还是 h2",6.6 详讲。

构造函数 `new`(`mod.rs#L155-L176`):

```rust
pub fn new(mut options: Option<ConnectorOptions>) -> Self {
    let pool_size = options
        .as_ref()
        .map_or(DEFAULT_POOL_SIZE, |c| c.keepalive_pool_size);
    // Take the offloading setting there because this layer has implement offloading,
    // so no need for stacks at lower layer to offload again.
    let offload = options.as_mut().and_then(|o| o.offload_threadpool.take());
    let bind_to_v4 = options
        .as_ref()
        .map_or_else(Vec::new, |o| o.bind_to_v4.clone());
    let bind_to_v6 = options
        .as_ref()
        .map_or_else(Vec::new, |o| o.bind_to_v6.clone());
    TransportConnector {
        tls_ctx: tls::Connector::new(options),
        connection_pool: Arc::new(ConnectionPool::new(pool_size)),
        offload: offload.map(|v| OffloadRuntime::new(v.0, v.1)),
        bind_to_v4,
        bind_to_v6,
        preferred_http_version: PreferredHttpVersion::new(),
    }
}
```

注意几个细节:

- **`DEFAULT_POOL_SIZE = 128`**(`mod.rs#L151`)。不配置时,池子容量 128 条 idle 连接(全局,跨所有 peer 共享这个上限)。
- **`offload_threadpool.take()`**——`new` 把 `offload_threadpool` 从 `options` 里**拿走**了。注释专门解释:"this layer has implement offloading, so no need for stacks at lower layer to offload again"。意思是:offload 在 `TransportConnector` 这一层做了,底下 TLS 层就不需要再 offload 一次(避免双重 offload)。所以把这个配置项从 options 里 take 出来,底下 TLS connector 拿到的 options 里就没有这个字段了。
- **`OffloadRuntime::new(v.0, v.1)`**——`offload_threadpool` 的类型是 `Option<(usize, usize)>`(`ConnectorOptions#L77`),元组 `(shards, thread_per_shard)`。`OffloadRuntime::new` 接收这两个参数,构造一个 `shards × thread_per_shard` 个线程的池子。6.5 详讲。

`ConnectorOptions`(`mod.rs#L46-L82`)的全貌:

```rust
pub struct ConnectorOptions {
    pub ca_file: Option<String>,
    #[cfg(feature = "s2n")]
    pub s2n_config_cache_size: Option<usize>,
    pub cert_key_file: Option<(String, String)>,
    pub debug_ssl_keylog: bool,
    pub keepalive_pool_size: usize,
    pub offload_threadpool: Option<(usize, usize)>,
    pub bind_to_v4: Vec<SocketAddr>,
    pub bind_to_v6: Vec<SocketAddr>,
}
```

值得注意的字段:

- **`keepalive_pool_size`**——池子最多存多少条 idle 连接(全局 LRU 上限)。超过就 LRU 淘汰最久没用的。
- **`offload_threadpool: Option<(usize, usize)>`**——`(#pools, #thread in each pool)`,即 `(shards, thread_per_shard)`。注释解释得很清楚(`mod.rs#L69-L77`):

  > "TCP and TLS connection establishment can be CPU intensive. Sometimes such tasks can slow down the entire service, which causes timeouts which leads to more connections which snowballs the issue. Use this option to isolate these CPU intensive tasks from impacting other traffic."

  这段注释点出一个生产事故模式:**雪球效应(snowball)**——建连慢 → 整个服务变慢 → 大量请求超时 → 客户端重试 → 连接更多 → 更慢 → ……。offload 的目的就是切断这个雪球,把建连这种 CPU 密集活隔离到专用线程,不让它拖慢本来已经 ready 的请求的处理。
- **`s2n_config_cache_size`**——只在使用 s2n TLS 后端时有意义。s2n 的 config 创建很贵(注释原话:"Creating a new s2n config is an expensive operation"),所以要缓存。这个字段是 s2n 后端独有的,本书 P5-16 详讲 TLS 后端时回扣。

### 6.4 连接池底层:`ConnectionPool` 与 `PoolNode`

`TransportConnector` 的池本体是 `ConnectionPool<Arc<Mutex<Stream>>>`,定义在 `pingora-pool/src/connection.rs#L164-L168`:

```rust
pub struct ConnectionPool<S> {
    // TODO: n-way pools to reduce lock contention
    pool: RwLock<HashMap<GroupKey, Arc<PoolNode<PoolConnection<S>>>>>,
    lru: Lru<ID, ConnectionMeta>,
}
```

两个字段:

- **`pool: RwLock<HashMap<GroupKey, Arc<PoolNode<...>>>>`**——按 `GroupKey`(就是 `u64`,`reuse_hash` 出来的)分桶的池子。每个桶是一个 `Arc<PoolNode<PoolConnection<S>>>`。读写锁:平时查询桶用读锁(多读并发),新建桶用写锁(很少,只有第一次见到这个 peer 时)。
- **`lru: Lru<ID, ConnectionMeta>`**——全局 LRU,记录所有 idle 连接的"最近使用"顺序,容量满时淘汰最久没用的。注意 key 是 `ID`(连接的唯一 id,Unix 上是 fd,Windows 上是 socket handle),不是 `GroupKey`——因为同一个 GroupKey 下可能有多条连接,LRU 要在单条连接粒度上淘汰。

`S` 在 `TransportConnector` 里被实例化为 `Arc<Mutex<Stream>>`。`PoolConnection<S>`(`connection.rs#L51-L70`)是在 `S` 上又包了一层,加了一个 `notify_use: oneshot::Sender<bool>`——这是 idle watcher 和"借出去"之间的通知通道,6.4.3 详讲。

#### 6.4.1 PoolNode:无锁热队列 + 兜底 HashMap

最精巧的设计在 `PoolNode`(`connection.rs#L75-L158`)。它不是简单的 `Mutex<Vec<Stream>>`,而是两层:

```rust
pub struct PoolNode<T> {
    connections: Mutex<HashMap<ID, T>>,
    // a small lock free queue to avoid lock contention
    hot_queue: ArrayQueue<(ID, T)>,
    // to avoid race between 2 evictions on the queue
    hot_queue_remove_lock: Mutex<()>,
}

const HOT_QUEUE_SIZE: usize = 16;
```

- **`hot_queue: ArrayQueue<(ID, T)>`**——一个固定大小(16 槽)的**无锁**环形队列,来自 `crossbeam-queue`。这是"快路径":借(`get_any`)和还(`insert`)都优先走 hot_queue,**完全不加锁**。
- **`connections: Mutex<HashMap<ID, T>>`**——兜底的 HashMap,只有 hot_queue 满了(还的时候)或者空了(借的时候)才走这里,加一把 `Mutex`。这是"慢路径"。

为什么这么设计?模块文档原话(`pingora-pool/src/lib.rs#L17-L20`):

> "The pool is optimized for high concurrency, high RPS use cases. Each connection group has a lock free hot pool to reduce the lock contention when some connections are reused and released very frequently."

翻译:在高 RPS 场景下,同一个 peer 的连接被**极频繁地**借还(每秒几千上万次)。如果每次借还都抢一把 `Mutex`,锁争用会成为瓶颈。所以加一个小的(16 槽)无锁队列做快路径——大部分借还都在这里完成,无锁;只有少量溢出到 HashMap(慢路径,加锁)。

看 `get_any`(`connection.rs#L98-L116`,借的逻辑):

```rust
pub fn get_any(&self) -> Option<(ID, T)> {
    let hot_conn = self.hot_queue.pop();
    if hot_conn.is_some() {
        return hot_conn;
    }
    let mut connections = self.connections.lock();
    // find one connection, any connection will do
    let id = match connections.iter().next() {
        Some((k, _)) => *k,
        None => return None,
    };
    let connection = connections.remove(&id);
    Some((id, connection.unwrap()))
}
```

逻辑很直白:① 先 pop hot_queue,pop 到就返回(无锁快路径);② hot_queue 空,锁 `connections`,从 HashMap 里随便挑一个(任意一个都行,`iter().next()`),拿出来返回(加锁慢路径)。

注意"**随便挑一个**"——这里不挑 LRU 顺序(全局 LRU 在 `ConnectionPool::lru` 那层管),`PoolNode` 这层只关心"快速给一条"。这是性能取舍:`PoolNode` 追求借的速度,LRU 顺序由上层统一管理。

`insert`(`connection.rs#L119-L125`,还的逻辑):

```rust
pub fn insert(&self, id: ID, conn: T) {
    if let Err(node) = self.hot_queue.push((id, conn)) {
        // hot queue is full
        let mut connections = self.connections.lock();
        connections.insert(node.0, node.1);
    }
}
```

① 先 push hot_queue,push 成功就返回(无锁);② hot_queue 满(16 条都闲着),锁 `connections`,塞进 HashMap(加锁)。

`HOT_QUEUE_SIZE = 16` 是个有意思的常数。为什么是 16?注释解释:"Keep the queue size small because eviction is O(n) in the queue"。淘汰(remove)操作要遍历整个 hot_queue 找目标 id(`connection.rs#L131-L157`),所以队列越长淘汰越慢。16 是个折中:大到能覆盖大部分热点(peer 的并发 idle 连接通常不超过 16),小到让淘汰的 O(n) 可接受。

> **钉死这件事**:`PoolNode` 的两层结构(无锁 hot_queue + 兜底 HashMap)是 Pingora 连接池**抗锁争用**的核心。它承认了一个现实:在高 RPS 下,锁是头号敌人,而连接的借还又有强烈的局部性(同一个 peer 的连接被密集借还)。所以用一个小无锁队列做快路径,让大部分借都不撞锁;少部分溢出才走加锁的 HashMap。这是"用空间换时间、用复杂度换吞吐"的典型取舍——一个 PoolNode 现在有两个数据结构、一把锁、一个无锁队列,但换来的是高并发下接近无锁的借还性能。

#### 6.4.2 LRU:thread-local 的"软全局"上限

`ConnectionPool` 还有一个全局 LRU(`lru: Lru<ID, ConnectionMeta>`),它管的是"整个池子最多存多少条 idle 连接"。超过 `keepalive_pool_size`(默认 128)就淘汰最久没用的。

这个 `Lru` 的实现很有意思(`pingora-pool/src/lru.rs#L42-L50`):

```rust
pub struct Lru<K, T>
where K: Send, T: Send {
    lru: RwLock<ThreadLocal<RefCell<LruCache<K, Node<T>>>>>,
    size: usize,
    drain: AtomicBool,
}
```

注意 `ThreadLocal<RefCell<LruCache<...>>>`——它不是一把大锁守一个全局 LruCache,而是**每个线程一个** LruCache(`ThreadLocal` 来自 `thread_local` crate)。每个线程往自己的 thread-local LruCache 里塞,只有自己的 LruCache 超过 `size` 才淘汰。

这又是一个抗锁争用的设计:如果全局一个 LruCache,每次 put/pop 都要抢锁;现在每个线程独立,put/pop 完全无锁(只对自己的 RefCell 借用,不跨线程)。

代价是:全局容量上限变成"每线程上限 × 线程数"——不是严格的"全局 128 条"。但这是可接受的近似:Pingora 用 NoStealRuntime(P5-15),线程数固定且不多,每线程的池子大小相对可控。注释里的 TODO(`connection.rs#L165` "n-way pools to reduce lock contention")暗示这是个有意识的取舍——把严格的全局上限换成抗锁争用的 thread-local 近似。

LRU 的 `add`(`lru.rs#L90-L95`)返回两个东西:

```rust
pub fn add(&self, key: K, meta: T) -> (Arc<Notify>, Option<T>) {
    let node = Node::new(meta);
    let notifier = node.close_notifier.clone();
    (notifier, self.put(key, node))
}
```

- **`Arc<Notify>`**——一个"被淘汰时通知"的信号量。idle watcher 会等这个 Notify,一旦这条连接被 LRU 淘汰,Notify 被 `notify_one()`,idle watcher 知道"这条连接不用再监视了,它要被回收了",退出。
- **`Option<T>`**——返回被淘汰的那条连接的 meta(如果有)。`put`(`lru.rs#L66-L88`)在塞入新连接后,如果当前线程的 LruCache 超过 `size`,就 `pop_lru()` 把最老的踢掉,返回它的 meta。

这两个返回值是 `ConnectionPool::put` 把连接进池的关键(`connection.rs#L249-L263`):

```rust
pub fn put(
    &self,
    meta: &ConnectionMeta,
    connection: S,
) -> (Arc<Notify>, oneshot::Receiver<bool>) {
    let (notify_close, replaced) = self.lru.add(meta.id, meta.clone());
    if let Some(meta) = replaced {
        self.pop_evicted(&meta);
    };
    let pool_node = self.get_pool_node(meta.key);
    let (notify_use, watch_use) = oneshot::channel();
    let connection = PoolConnection::new(notify_use, connection);
    pool_node.insert(meta.id, connection);
    (notify_close, watch_use)
}
```

`put` 干三件事:① 进 LRU(可能淘汰掉一条老的,如果淘汰了就把那条从 PoolNode 里 `pop_evicted` 移除);② 拿到(或新建)这个 GroupKey 对应的 PoolNode;③ 把连接(包成 `PoolConnection`,附带一个"被借走时通知"的 oneshot)insert 进 PoolNode。返回 `(notify_close, watch_use)`:

- **`notify_close: Arc<Notify>`**——"这条连接被 LRU 淘汰时通知"(给 idle watcher 用)。
- **`watch_use: oneshot::Receiver<bool>`**——"这条连接被借走时通知"(给 idle watcher 用)。

idle watcher 同时等这两个信号 + 实际的 read 事件(`idle_poll`,6.4.3 详讲)。这三个信号任意一个触发,idle watcher 都要采取行动。

#### 6.4.3 idle_poll:后台监视 idle 连接

`release_stream`(`mod.rs#L258-L279`)是"把用完的连接还回池子"的入口:

```rust
pub fn release_stream(
    &self,
    mut stream: Stream,
    key: u64, // usually peer.reuse_hash()
    idle_timeout: Option<std::time::Duration>,
) {
    if !test_reusable_stream(&mut stream) {
        return;
    }
    let id = stream.id();
    let meta = ConnectionMeta::new(key, id);
    debug!("Try to keepalive client session");
    let stream = Arc::new(Mutex::new(stream));
    let locked_stream = stream.clone().try_lock_owned().unwrap(); // safe as we just created it
    let (notify_close, watch_use) = self.connection_pool.put(&meta, stream);
    let pool = self.connection_pool.clone(); //clone the arc
    let rt = pingora_runtime::current_handle();
    rt.spawn(async move {
        pool.idle_poll(locked_stream, &meta, idle_timeout, notify_close, watch_use)
            .await;
    });
}
```

`release_stream` 干五件事,顺序非常讲究:

1. **`test_reusable_stream(&mut stream)`**——先探活!如果连接已经死了(读到 EOF 或异常数据),直接 return(不进池子,连接被 drop 时关闭)。这是**第一道防线**,把死连接挡在池子外。6.7 技巧精解详讲。
2. **`stream.id()`**——拿连接的唯一 id(Unix 上是 fd)。这个 id 是 LRU 的 key,也是 PoolNode 里 HashMap 的 key。
3. **`Arc::new(Mutex::new(stream))`**——把 `Stream` 包成 `Arc<Mutex<Stream>>`。注意:从这一刻起,这条连接的所有权被 `Arc` 共享——池子(`PoolNode`)持一份,idle watcher 持一份(`locked_stream`)。借出去的时候,请求方也是从这个 `Arc` 里 `try_unwrap` 出来用。
4. **`pool.put(&meta, stream)`**——进池子。返回 `notify_close`(被淘汰通知)和 `watch_use`(被借走通知)。
5. **`rt.spawn(idle_poll(...))`**——在当前 runtime 上 spawn 一个后台 task,跑 `idle_poll`。这个 task 持有 `locked_stream`(连接的独占锁),在后台监视这条连接。

注意第 5 步用的是 `pingora_runtime::current_handle().spawn(...)`,不是 `tokio::spawn`。这承接 Pingora 自研的 NoStealRuntime(P5-15)——`current_handle` 拿到当前 runtime 的 handle,在这个 runtime 上 spawn,idle_poll task 跑在和请求同一个 runtime 上(但因为是异步的,不阻塞请求)。

`locked_stream` 是关键。`stream.clone().try_lock_owned().unwrap()`——`try_lock_owned` 是 `tokio::sync::Mutex` 的方法,尝试**非阻塞**地拿一个 owned 锁(`OwnedMutexGuard`)。这里能 `unwrap` 是因为注释说的 "safe as we just created it"——我们刚把这条 stream 包进 `Arc::new(Mutex::new(...))`,只有我们一个 owner,锁没人抢,`try_lock_owned` 必然成功。

这个 owned 锁被 move 进 idle_poll task。它的意义是:**idle watcher 持有这条连接的锁**。当有请求来借这条连接时(`reused_stream`,6.4.4),它要先 `s.lock().await` 拿锁——可锁在 idle watcher 手里,idle watcher 正卡在 `read` 上(等连接上的事件)。怎么解?

答案在 `idle_poll`(`connection.rs#L271-L310`):

```rust
pub async fn idle_poll<Stream>(
    &self,
    connection: OwnedMutexGuard<Stream>,
    meta: &ConnectionMeta,
    timeout: Option<Duration>,
    notify_evicted: Arc<Notify>,
    watch_use: oneshot::Receiver<bool>,
) where
    Stream: AsyncRead + Unpin + Send,
{
    let read_result = tokio::select! {
        biased;
        _ = watch_use => {
            debug!("idle connection is being picked up");
            return
        },
        _ = notify_evicted.notified() => {
            debug!("idle connection is being evicted");
            return
        }
        read_result = read_with_timeout(connection , timeout) => read_result
    };

    match read_result {
        Ok(n) => {
            if n > 0 {
                warn!("Data received on idle client connection, close it")
            } else {
                debug!("Peer closed the idle connection or timeout")
            }
        }
        Err(e) => {
            debug!("error with the idle connection, close it {:?}", e);
        }
    }
    // connection terminated from either peer or timer
    self.pop_closed(meta);
}
```

`idle_poll` 是个 `tokio::select!`,三个分支同时等,`biased` 表示按顺序优先匹配:

1. **`watch_use`**——连接被借走了(请求方调 `pool.get()` 时,`PoolConnection::release` 会 `notify_use.send(true)`)。idle watcher 立刻退出,**交出锁**(owned guard 随 task 结束被 drop,锁释放)。请求方那边 `s.lock().await` 拿到锁,继续用。
2. **`notify_evicted.notified()`**——连接被 LRU 淘汰了。idle watcher 退出,连接被 drop(关闭)。
3. **`read_with_timeout(connection, timeout)`**——在连接上 read。read 返回意味着:① 读到 EOF(对端关了);② 读到数据(对端主动发了东西,对 idle 连接是异常);③ timeout 到了(idle 太久)。这三种情况都说明连接不该继续留着,idle watcher 调 `pop_closed` 把它从池子里移除,然后退出(连接被 drop 关闭)。

注意第 3 分支把 `connection`(owned guard)move 进了 `read_with_timeout`——这是关键:**read 操作持有锁**。read 期间,如果有请求来借(走 `reused_stream` 的 `s.lock().await`),它会等锁——等 idle watcher 退出(被 `watch_use` 唤醒)才拿到。

但等等,这里有个时序问题:`reused_stream` 是先从池子里取出 `Arc<Mutex<Stream>>`,再 `s.lock().await`。可 idle watcher 持着锁做 read,read 在阻塞(没数据来),怎么触发 `watch_use`?

答案:`reused_stream` 在 lock 之前先 `pool.get()`——`get`(`connection.rs#L228-L243`)从 PoolNode 里取出连接,这一步同时调 `PoolConnection::release`(`connection.rs#L64-L69`):

```rust
pub fn release(self) -> S {
    // notify the idle watcher to release the connection
    let _ = self.notify_use.send(true);
    // wait for the watcher to release
    self.connection
}
```

`release` 发了 `notify_use.send(true)`——这正是 `watch_use` 那个 oneshot 收到的东西。所以时序是:

```text
请求方: pool.get(peer_hash)
         → PoolNode::get_any() 取出 (id, PoolConnection)
         → PoolConnection::release() 发 notify_use
              ↓ (oneshot 信号)
idle watcher: select! 的 watch_use 分支触发, return (退出 task)
              ↓ (owned guard drop, 锁释放)
请求方: s.lock().await 拿到锁
        → test_reusable_stream
        → 用连接发请求
```

`release` 的注释点出这个交接:"notify the idle watcher to release the connection" + "wait for the watcher to release"——虽然代码上没有显式 wait(因为 `release` 直接返回 connection,owned guard 的释放由 task 结束保证),但语义上就是请求方等 idle watcher 退出。

> **钉死这件事**:`Arc<Mutex<Stream>>` + `idle_poll` 持锁的设计,实现了**连接在"后台监视"和"借给请求"之间的安全交接**。idle watcher 持锁监视(防止它监视期间请求方也来读写,造成数据混乱);请求方借的时候,先从池子取(触发 `notify_use`),idle watcher 收到信号退出并释放锁,请求方拿到锁继续用。整个交接过程靠 `tokio::sync::Mutex` 的 await(请求方)和 owned guard 的 drop(idle watcher)协调,无锁竞争。这是连接池"为什么 sound"的核心一条:同一条连接同一时刻只有一方在操作。

`read_with_timeout`(`connection.rs#L347-L366`)就是 idle watcher 的"探活 read":

```rust
async fn read_with_timeout<S>(
    mut connection: OwnedMutexGuard<S>,
    timeout_duration: Option<Duration>,
) -> io::Result<usize>
where
    S: AsyncRead + Unpin + Send,
{
    let mut buf = [0; 1];
    let read_event = connection.read(&mut buf[..]);
    match timeout_duration {
        Some(d) => match timeout(d, read_event).await {
            Ok(res) => res,
            Err(e) => {
                debug!("keepalive timeout {:?} reached, {:?}", d, e);
                Ok(0)
            }
        },
        _ => read_event.await,
    }
}
```

注意它也读 **1 字节**(`let mut buf = [0; 1];`)。idle watcher 在连接上挂一个 1 字节的 read,等三个结果之一:① EOF(0 字节,对端关了);② 异常数据(1 字节,对端主动发东西);③ timeout(idle 太久)。这和 `test_reusable_stream` 的 1 字节读是**同一个思路的两次应用**——一次在借的时候(非阻塞探活),一次在 idle 期间(阻塞监听 + timeout)。1 字节是因为:读到东西就够判断"连接异常"了,不需要读更多(读更多反而会消费掉可能的真实响应数据,虽然对 idle 连接来说不应该有真实响应)。

#### 6.4.4 reused_stream:从池子里借一条

现在看借的逻辑 `reused_stream`(`mod.rs#L202-L249`):

```rust
pub async fn reused_stream<P: Peer + Send + Sync>(&self, peer: &P) -> Option<Stream> {
    match self.connection_pool.get(&peer.reuse_hash()) {
        Some(s) => {
            debug!("find reusable stream, trying to acquire it");
            {
                let _ = s.lock().await;
            } // wait for the idle poll to release it
            match Arc::try_unwrap(s) {
                Ok(l) => {
                    let mut stream = l.into_inner();
                    // test_reusable_stream: we assume server would never actively send data
                    // first on an idle stream.
                    #[cfg(unix)]
                    if peer.matches_fd(stream.id()) && test_reusable_stream(&mut stream) {
                        Some(stream)
                    } else {
                        None
                    }
                    // ... (windows 分支类似, 用 matches_sock)
                }
                Err(_) => {
                    error!("failed to acquire reusable stream");
                    None
                }
            }
        }
        None => {
            debug!("No reusable connection found for {peer}");
            None
        }
    }
}
```

`reused_stream` 干四件事:

1. **`connection_pool.get(&peer.reuse_hash())`**——按 peer 的 `reuse_hash` 从池子里取一条 `Arc<Mutex<Stream>>`。池子那层(`ConnectionPool::get`,`connection.rs#L228-L243`)会从对应 GroupKey 的 PoolNode 取一条,触发 `PoolConnection::release`(发 `notify_use`,唤醒 idle watcher)。
2. **`s.lock().await`**——等 idle watcher 释放锁(idle watcher 收到 `notify_use` 退出,owned guard drop)。注释专门解释:"wait for the idle poll to release it"。注意这个 `let _ = s.lock().await;` 拿到锁后立刻 drop(空 block 结束)——它的目的只是**等 idle watcher 退出**,不是真的要用锁。drop 之后,锁重新可用。
3. **`Arc::try_unwrap(s)`**——尝试把 `Arc<Mutex<Stream>>` 拆回 `Mutex<Stream>`(再 `into_inner()` 拿到 `Stream`)。`try_unwrap` 只有在 `Arc` 的 strong count = 1 时才成功(只有我们一个 owner)。如果失败(`Err`),说明还有别的 `Arc` 持有这条连接(idle watcher 还没退出,或者别的什么),`error!("failed to acquire reusable stream")` 返回 None。
4. **`peer.matches_fd(stream.id()) && test_reusable_stream(&mut stream)`**——两个检查:① `matches_fd` 确认这条连接的 fd 确实属于这个 peer(防止 fd 复用造成的误判——fd 在连接关闭后会被内核回收复用,可能 PoolNode 里还存着老的 fd 记录);② `test_reusable_stream` 再做一次非阻塞探活(从借出到使用之间可能又过了几个微秒,再探一次更稳)。两个都过,返回 `Some(stream)`;否则 None。

注意第 3 步的 `Arc::try_unwrap` 是个有意思的设计。为什么不用 `Arc::clone` 共享,而要 `try_unwrap` 独占?因为请求要**独占**这条连接发请求(代理要对连接做 read/write,不能和别人共享)。`try_unwrap` 成功 = 没有别的 owner = 我独占。如果 idle watcher 还持着一份 Arc(还没退出),`try_unwrap` 失败——这是个保护:宁可借失败(走 `new_stream` 新建),也不能在还有别的 owner 的情况下用这条连接。

第 4 步的 `matches_fd` 是个细节但重要的防护。设想:连接 A(fd=10)进池子,后来连接 A 关闭了(fd=10 被回收),新的连接 B 复用了 fd=10(连到了**另一个** peer)。如果池子里还存着 A 的记录(key=peer_A_hash, id=10),现在来个请求要连 peer_A,从池子里捞出了这个记录——可 fd=10 现在是连到 peer_B 的连接!如果不检查 `matches_fd`,就会把连到 peer_B 的连接当成连到 peer_A 的复用了,发错请求。`matches_fd` 通过对比 fd 当前的对端地址和 peer 期望的地址,确保 fd 还是我们认为的那条连接。

#### 6.4.5 get_stream:复用优先,新建兜底

`get_stream`(`mod.rs#L287-L298`)是上层(HTTP connector、代理逻辑)调的入口,把"先复用,失败再新建"打包成一个 API:

```rust
pub async fn get_stream<P: Peer + Send + Sync + 'static>(
    &self,
    peer: &P,
) -> Result<(Stream, bool)> {
    let reused_stream = self.reused_stream(peer).await;
    if let Some(s) = reused_stream {
        Ok((s, true))
    } else {
        let s = self.new_stream(peer).await?;
        Ok((s, false))
    }
}
```

返回 `(Stream, bool)`——bool 表示"是否复用"。上层根据这个 bool 决定要不要走 keepalive 逻辑(复用的连接已经握过手,新建的要重新协商)。

逻辑很简单:① 先 `reused_stream` 试复用;② 有就返回 `(stream, true)`;③ 没有就 `new_stream` 新建,返回 `(stream, false)`。

注意 `new_stream` 的 `P: 'static` bound——`reused_stream` 只要 `Send + Sync`,而 `new_stream` 多一个 `'static`。这是因为 `new_stream` 可能要 offload 到另一个 runtime(把 peer clone 过去),需要 `'static`。`get_stream` 取两者的并集,也要 `'static`。

### 6.5 new_stream 与 offload_threadpool:把建连 offload

`new_stream`(`mod.rs#L181-L199`)是"新建一条连接"的入口:

```rust
pub async fn new_stream<P: Peer + Send + Sync + 'static>(&self, peer: &P) -> Result<Stream> {
    let rt = self
        .offload
        .as_ref()
        .map(|o| o.get_runtime(peer.reuse_hash()));
    let bind_to = l4::bind_to_random(peer, &self.bind_to_v4, &self.bind_to_v6);
    let alpn_override = self.preferred_http_version.get(peer);
    let stream = if let Some(rt) = rt {
        let peer = peer.clone();
        let tls_ctx = self.tls_ctx.clone();
        rt.spawn(async move { do_connect(&peer, bind_to, alpn_override, &tls_ctx.ctx).await })
            .await
            .or_err(InternalError, "offload runtime failure")??
    } else {
        do_connect(peer, bind_to, alpn_override, &self.tls_ctx.ctx).await?
    };

    Ok(stream)
}
```

`new_stream` 干四件事:

1. **`self.offload.as_ref().map(|o| o.get_runtime(peer.reuse_hash()))`**——如果配了 offload,按 peer 的 hash 选一个 offload runtime handle(`OffloadRuntime::get_runtime`,6.5.1 详讲)。否则 `rt = None`。
2. **`bind_to_random`**——从配置的源地址列表里随机挑一个(`l4.rs#L213-L256`),为了多源 IP 出口做负载分散。
3. **`preferred_http_version.get(peer)`**——查这个 peer 之前有没有被标记过"该用 h1 还是 h2"(6.6 详讲)。返回 `Option<ALPN>`,作为 TLS ALPN 协商的 override。
4. **建连**:① 有 offload runtime,把 `do_connect` 这个 future spawn 到 offload runtime 上,`await` 它的结果(clone peer 和 tls_ctx 进 future,因为跨 runtime);② 没有 offload,直接在当前 runtime 上 `do_connect`。

`do_connect`(`mod.rs#L308-L328`)→ `do_connect_inner`(`mod.rs#L331-L344`)是真正的建连:

```rust
async fn do_connect_inner<P: Peer + Send + Sync>(
    peer: &P,
    bind_to: Option<BindTo>,
    alpn_override: Option<ALPN>,
    tls_ctx: &TlsConnector,
) -> Result<Stream> {
    let stream = l4_connect(peer, bind_to).await?;
    if peer.tls() {
        let tls_stream = tls::connect(stream, peer, alpn_override, tls_ctx).await?;
        Ok(Box::new(tls_stream))
    } else {
        Ok(Box::new(stream))
    }
}
```

两步:① `l4_connect`(就是 `connectors::l4::connect`)建 TCP/UDS 连接;② 如果 peer 要 TLS(`peer.tls()`),`tls::connect` 做 TLS 握手,返回 `SslStream<Stream>`,Box 起来;否则直接 Box TCP/UDS stream。返回的 `Stream` 就是 `Box<dyn IO>`(IO trait 见 `protocols/mod.rs#L88-L107`,是 `AsyncRead + AsyncWrite + Shutdown + UniqueID + Ssl + ...` 的一堆 trait 组合)。

`do_connect` 还会处理 `total_connection_timeout`(`mod.rs#L318-L327`)——整个建连(包括 TCP + TLS)的总超时,用 `pingora_timeout::timeout` 包住 `do_connect_inner`。`pingora_timeout` 是 Pingora 自研的无锁 timeout(比 `tokio::time::timeout` 轻,底层仍是 tokio 时间轮,承《Tokio》一句带过指路)。

#### 6.5.1 OffloadRuntime:一组不做 work stealing 的 current_thread runtime

`OffloadRuntime`(`offload.rs#L23-L77`)是 Pingora 自研的 offload 池:

```rust
pub(crate) struct OffloadRuntime {
    shards: usize,
    thread_per_shard: usize,
    // Lazily init the runtimes so that they are created after pingora
    // daemonize itself. Otherwise the runtime threads are lost.
    pools: OnceCell<Box<[(Handle, Sender<()>)]>>,
}
```

字段:

- **`shards` / `thread_per_shard`**——配置的 `(#pools, #thread in each pool)`。
- **`pools: OnceCell<Box<[(Handle, Sender<()>)]>>`**——一组 `(Handle, Sender<()>)`。`Handle` 是 tokio runtime 的 handle(用来 spawn future);`Sender<()>` 是个关闭信号(drop 它或者 send 时让 runtime 退出)。`OnceCell` 表示**懒初始化**——直到第一次用时才创建这些 runtime。

`init_pools`(`offload.rs#L42-L64`)是关键:

```rust
fn init_pools(&self) -> Box<[(Handle, Sender<()>)]> {
    let threads = self.shards * self.thread_per_shard;
    let mut pools = Vec::with_capacity(threads);
    for _ in 0..threads {
        // We use single thread runtimes to reduce the scheduling overhead of multithread
        // tokio runtime, which can be 50% of the on CPU time of the runtimes
        let rt = Builder::new_current_thread().enable_all().build().unwrap();
        let handler = rt.handle().clone();
        let (tx, rx) = channel::<()>();
        std::thread::Builder::new()
            .name("Offload thread".to_string())
            .spawn(move || {
                debug!("Offload thread started");
                // the thread that calls block_on() will drive the runtime
                // rx will return when tx is dropped so this runtime and thread will exit
                rt.block_on(rx)
            })
            .unwrap();
        pools.push((handler, tx));
    }

    pools.into_boxed_slice()
}
```

注意几个细节:

- **每个线程一个 `current_thread` runtime**——不是 multi_thread runtime!注释解释:"We use single thread runtimes to reduce the scheduling overhead of multithread tokio runtime, which can be 50% of the on CPU time of the runtimes"。意思是:多线程 tokio runtime 的 work-stealing 调度器本身有开销(线程间偷 task、同步调度队列),在 Pingora 的 offload 场景下,这个开销能占到 runtime 总 on-CPU 时间的 50%。用 `current_thread` runtime(单线程,无 work stealing,无调度同步)能省掉这 50%。
  
  这承接了 Pingora NoStealRuntime 的核心思想(P5-15 详讲)——Pingora 全程偏爱 `current_thread` runtime,认为 work stealing 在它的负载模式下是净开销。OffloadRuntime 是这个思想在连接池层的又一次应用。
  
- **`std::thread::spawn` + `rt.block_on(rx)`**——每个 offload 线程是一个**原生 OS 线程**,线程里 `block_on` 一个 `current_thread` runtime(驱动 reactor),`block_on` 的 future 是 `rx`(一个 oneshot receiver)。`rx` 在 `tx` 被 drop 时返回,所以这个线程/runtime 的生命周期由 `tx` 控制——`OffloadRuntime` drop 时,所有 `tx` drop,所有线程的 `block_on` 返回,线程退出。**干净的关闭语义**。
  
- **"the thread that calls block_on() will drive the runtime"**——`current_thread` runtime 的特点是:`block_on` 的调用线程就是 driver 线程(它跑 reactor、跑调度)。所以这个 OS 线程既跑 reactor 也跑 future。

`get_runtime`(`offload.rs#L66-L76`)选哪个线程:

```rust
pub fn get_runtime(&self, hash: u64) -> &Handle {
    let mut rng = rand::thread_rng();
    // choose a shard based on hash and a random thread with in that shard
    let shard = hash as usize % self.shards;
    let thread_in_shard = rng.gen_range(0..self.thread_per_shard);
    let pools = self.pools.get_or_init(|| self.init_pools());
    &pools[shard * self.thread_per_shard + thread_in_shard].0
}
```

选法:**shard 按 hash,shard 内的 thread 随机**。同一个 peer 的建连总是落在同一个 shard(按 `reuse_hash` 取模),但 shard 内的多个线程随机挑一个。这样:

- **同 peer 的建连集中在同一 shard**——可能有利于缓存局部性(同一个后端的连接复用 TCP fast open cookie 等内核状态)。
- **shard 内随机分散**——避免单线程成为热点。

`pools.get_or_init` 是 `OnceCell` 的懒初始化——第一次 `get_runtime` 时才真正创建所有 runtime。注释解释了为什么要懒:"Lazily init the runtimes so that they are created after pingora daemonize itself. Otherwise the runtime threads are lost."——如果 Pingora 要 daemonize(后台化,`fork` 出子进程),`fork` 之前创建的线程会丢失(只有 `fork` 的调用线程存活到子进程)。所以必须等 daemonize 完成后再创建这些 offload 线程。`OnceCell` 的懒初始化保证了这一点——daemonize 之后第一次用时才 init。

#### 6.5.2 为什么不用 tokio::spawn_blocking?

一个自然的疑问:Tokio 已经有 `spawn_blocking`(把同步阻塞代码投递到 blocking 线程池),Pingora 为什么不用,而是自己搭一组 `current_thread` runtime?

关键差异:`spawn_blocking` 是给**同步阻塞**代码用的——它的签名是 `spawn_blocking<F: FnOnce() -> R + Send + 'static)`,接收一个**普通函数**(返回值,不是 Future)。这个函数在线程池的某个线程上**同步**执行,阻塞没关系。

可建连是**异步**的——`do_connect` 是个 `async fn`,内部 `l4_connect` 和 `tls::connect` 都是异步的(基于 Tokio 的 AsyncRead/AsyncWrite + reactor)。`spawn_blocking` 不能直接跑 async future(它在同步线程上跑,没有 reactor 驱动)。

理论上可以用 `tokio::task::block_in_place` + 在 blocking 线程里 `block_on`,但那是给"已经在 multi_thread runtime 里、要嵌套阻塞"用的,语义复杂,而且 `block_in_place` 要 multi_thread runtime(和 Pingora 的 NoSteal 取舍冲突)。

所以 Pingora 的解法是:**给每个 offload 线程配一个 `current_thread` runtime**,这样这个线程有自己的 reactor,能跑 async future。把 `do_connect` 这个 async future `spawn` 到这个 runtime 的 handle 上,`await` 它——future 在 offload 线程上跑(包括 TCP connect 的等待、TLS 握手的非对称运算),reactor 也在那个线程上驱动,**完全不占请求所在的 reactor 线程**。

这就达成了 offload 的目的:**建连的 CPU 密集部分(TLS 握手)在 offload 线程上算,不阻塞请求 reactor**。请求 reactor 在 `rt.spawn(...).await` 这里挂起(让出,去跑别的 task),等 offload 线程算完把 Stream 送回来。

> **钉死这件事**:`OffloadRuntime` 是 Pingora 对"`spawn_blocking` 不够用"的回答。`spawn_blocking` 给同步阻塞代码用,可建连是异步的(要 reactor)。Pingora 给每个 offload 线程配一个 `current_thread` runtime,让 async future 能在专用线程上跑。同时,`current_thread` 不做 work stealing,省掉 multi_thread runtime 调度开销(注释说能省 50% 的 on-CPU 时间)。这承接了 NoStealRuntime 的思想(P5-15),是 Pingora 全程偏爱单线程 runtime 的又一次体现。代价是:offload 线程间不偷 task,如果某个线程卡在慢建连上,排队的建连要等(但因为是按 shard 分配 + shard 内随机,热点被分散)。

#### 6.5.3 雪球效应:offload 为什么重要

回到 `ConnectorOptions::offload_threadpool` 的注释(`mod.rs#L69-L77`),它点出了 offload 要防的"雪球效应":

> "TCP and TLS connection establishment can be CPU intensive. Sometimes such tasks can slow down the entire service, which causes timeouts which leads to more connections which snowballs the issue."

这个雪球的链条是:

```text
某次网络抖动 / 后端变慢
    → 建连变慢(更多连接在握手阶段积压)
    → reactor 线程被 TLS 握手的 CPU 计算占满
    → 别的请求的处理被延迟(reactor 没空 poll 它们)
    → 这些请求也超时
    → 客户端重试,产生更多请求
    → 更多连接需要建
    → TLS 握手更多,reactor 更满
    → ... (雪球)
```

offload 切断这个雪球的点是"reactor 线程被 TLS 握手的 CPU 计算占满"——把 TLS 握手挪到 offload 线程,reactor 线程只负责"派发握手任务 + 接收握手完成的 Stream",不被 CPU 计算占满,别的请求的处理不受影响。即使建连变慢(在 offload 线程上排队),已经建好的连接上的请求照常处理,不会连锁超时。

这是生产事故驱动的优化——Cloudflare 在大规模部署中踩过这个雪球,所以 `offload_threadpool` 是个可选但推荐的配置。

### 6.6 PreferredHttpVersion:记住"这个 peer 该用什么协议"

`PreferredHttpVersion`(`mod.rs#L346-L373`)是个小但精巧的设计:

```rust
struct PreferredHttpVersion {
    // TODO: shard to avoid the global lock
    versions: RwLock<HashMap<u64, u8>>, // <hash of peer, version>
}

impl PreferredHttpVersion {
    pub fn new() -> Self {
        PreferredHttpVersion {
            versions: RwLock::default(),
        }
    }

    pub fn add(&self, peer: &impl Peer, version: u8) {
        let key = peer.reuse_hash();
        let mut v = self.versions.write();
        v.insert(key, version);
    }

    pub fn get(&self, peer: &impl Peer) -> Option<ALPN> {
        let key = peer.reuse_hash();
        let v = self.versions.read();
        v.get(&key)
            .copied()
            .map(|v| if v == 1 { ALPN::H1 } else { ALPN::H2H1 })
    }
}
```

它就是一个 `RwLock<HashMap<u64, u8>>`——按 peer 的 hash 记一个版本号(`1` = h1,其他 = h2h1)。`add` 写入,`get` 读出。

为什么需要这个?HTTP/2 的协商靠 ALPN——TLS 握手时客户端在 ClientHello 里告诉服务器"我支持 h2 和 h1.1",服务器选一个。正常情况下,服务器支持 h2 就选 h2(更高性能),不支持就选 h1.1。

但现实没那么干净:有些服务器**声称支持 h2 但实际有问题**(比如 h2 实现有 bug、或者中间有设备不识别 h2 帧)。Pingora 第一次连这个 peer 时按正常 ALPN 协商(可能选了 h2),发现"哦这个 peer 用 h2 不靠谱",就可以调 `prefer_h1(peer)`(`mod.rs#L301-L303`):

```rust
/// Tell the connector to always send h1 for ALPN for the given peer in the future.
pub fn prefer_h1(&self, peer: &impl Peer) {
    self.preferred_http_version.add(peer, 1);
}
```

`prefer_h1` 把这个 peer 标记为"以后永远用 h1"。下次 `new_stream` 时,`preferred_http_version.get(peer)` 返回 `Some(ALPN::H1)`,作为 `alpn_override` 传给 `do_connect`——TLS 握手时**只宣告 h1**(不宣告 h2),服务器被迫选 h1。

注意 `alpn_override` 在 TLS connect 里的位置(`connectors/tls/boringssl_openssl/mod.rs#L244-L246`):

```rust
if let Some(alpn) = alpn_override.as_ref().or(peer.get_alpn()) {
    ssl_conf.set_alpn_protos(alpn.to_wire_preference()).unwrap();
}
```

`alpn_override.as_ref().or(peer.get_alpn())`——override 优先,没有才用 peer 自己的 alpn 设置。所以 `PreferredHttpVersion` 的 override 高于一切。

这是个"学习"机制——代理根据历史经验调整未来行为,避免反复踩同一个坑。注释里的 TODO("shard to avoid the global lock")暗示这个 `RwLock<HashMap>` 在极高并发下可能成为热点,未来可能要分片。当前版本是一个简单可用的实现。

> **钉死这件事**:`PreferredHttpVersion` 解决的是"ALPN 协商失败的善后"——服务器声称支持 h2 但实际不靠谱时,业务可以 `prefer_h1(peer)` 标记它,以后永远走 h1。这是个学习机制,让代理从失败中调整。对照 Nginx:Nginx 没有"记住 peer 该用什么"的机制,只能靠 `proxy_http_version` 全局配置(全局 h1 或 h2,不能 per-peer)。Pingora 的 per-peer 学习更灵活,代价是一把全局 `RwLock<HashMap>`(TODO 要分片)。

---

## 技巧精解

这一节挑两个最硬核的技巧单独拆透:**(一)`test_reusable_stream` 的 1 字节非阻塞读探活**;(二)`offload_threadpool` 把建连 offload 到 `current_thread` runtime**。每个技巧配真实源码 + 反面对比。

### 技巧一:`test_reusable_stream` —— 1 字节非阻塞读探活

这是 Pingora 连接池最招牌的技巧,也是 Cloudflare 在大规模生产中过滤死连接的核心手段。理解它,等于理解"为什么 Pingora 的复用率可以做得这么高,同时 502 率又这么低"。

#### 6.7.1 问题:TCP 的沉默与半开连接

回顾 6.1.2 提出的半开连接问题。TCP 是个**沉默**的协议:

- 对端崩溃(机器挂了、进程被 kill -9)、链路中断(网线拔了、路由断了)、NAT 表项过期、防火墙静默 drop——这些情况下,**对端不会发任何东西**(没机会发 RST,或者 RST 被中间设备丢了)。
- 本地 socket 在很长一段时间内**毫不知情**。内核的 TCP keepalive(默认 2 小时后才探)太慢,根本覆盖不了代理场景的复用周期(秒级)。
- 你 `write()` 到这条连接,本地内核**收下**(塞进发送缓冲区),`write` 立刻返回成功——你以为发出去了,其实数据可能永远到不了对端。
- 你 `read()` 等响应,等到天荒地老(或者你的应用层超时介入)。

把这样一条连接从池子里捞出来发给请求,就是经典的生产故障:第一个 `write` 骗过你(本地成功),`read` 卡死,请求超时,用户看到 502。在高 RPS 下,这种故障会密集出现——因为复用率高,踩中死连接的概率也高。

#### 6.7.2 朴素方案:为什么不工作

**朴素方案一:啥都不做,踩坑了重试。** 这是 Nginx 的路线(`proxy_next_upstream`)。简单,但每次踩坑付出一次失败 + 重试的代价,用户感知到延迟抖动。在长尾延迟敏感的场景(P99、P999),这是不可接受的。

**朴素方案二:阻塞 read 探活。** 复用前 `read()` 等一下,看有没有 EOF。问题是:正常 idle 连接 `read` 会**阻塞**(没数据来,服务器在等请求),你要等多久?等 1ms?连接还活着但服务器慢;等 1s?请求延迟 +1s。阻塞 read 探活根本不可行。

**朴素方案三:非阻塞 read 探活。** 把 socket 设成非阻塞,`read` 立刻返回——要么有数据(读到东西)、要么 EAGAIN(没数据,连接还沉默着)、要么 EOF(对端关了)、要么 error(连接断了)。这听起来对,但有微妙的问题:Tokio 的 `AsyncRead::poll_read` 是为异步 reactor 设计的,它的"非阻塞"语义不完全是"立刻返回 EAGAIN"——它可能返回 `Poll::Pending`(挂起 waker,等 reactor 通知),而 `Pending` 不等于"立刻能判断连接死活"。

#### 6.7.3 Pingora 的解法:`now_or_never` + 1 字节非阻塞 read

Pingora 的 `test_reusable_stream`(`mod.rs#L379-L400`)用了一个巧妙的组合:

```rust
use futures::future::FutureExt;
use tokio::io::AsyncReadExt;

/// Test whether a stream is already closed or not reusable (server sent unexpected data)
fn test_reusable_stream(stream: &mut Stream) -> bool {
    let mut buf = [0; 1];
    // tokio::task::unconstrained because now_or_never may yield None when the future is ready
    let result = tokio::task::unconstrained(stream.read(&mut buf[..])).now_or_never();
    if let Some(data_result) = result {
        match data_result {
            Ok(n) => {
                if n == 0 {
                    debug!("Idle connection is closed");
                } else {
                    warn!("Unexpected data read in idle connection");
                }
            }
            Err(e) => {
                debug!("Idle connection is broken: {e:?}");
            }
        }
        false
    } else {
        true
    }
}
```

逐行拆:

1. **`let mut buf = [0; 1];`**——1 字节缓冲区。只读 1 字节,因为目的不是读数据,是探"有没有数据/EOF/error"。读到 1 字节就够了判断"对端主动发了东西"(对 idle 连接是异常)。
   
2. **`tokio::task::unconstrained(stream.read(&mut buf[..]))`**——`unconstrained` 是 Tokio 的一个工具(`tokio::task::unconstrained`),它把一个 future 包起来,**屏蔽 cooperative yielding**(协作式让出)。正常情况下,Tokio 的 future 每 poll 若干次会自动 yield(让出 reactor 给别的 task,这是 budget 机制,承《Tokio》一句带过指路 `[[tokio-source-facts]]` budget=128)。`unconstrained` 关掉这个 yield——future 一直 poll 到完成,不让出。注释解释为什么要 `unconstrained`:"because now_or_never may yield None when the future is ready"——意思是,如果不屏蔽 yield,`stream.read` 可能在 ready 时仍然 yield 一次(返回 `Pending`),导致 `now_or_never` 误判成"还没 ready"。

3. **`.now_or_never()`**——`futures::future::FutureExt::now_or_never`,它**立刻 poll 一次** future:如果 ready,返回 `Some(结果)`;如果 `Pending`,返回 `None`(**不挂 waker,不等**)。这是个"非阻塞试一下"的语义——poll 一次,成不成都不等。

4. **结果判断**:
   - `Some(Ok(0))`——读到 0 字节 = EOF,对端关了连接 → `false`(不可复用)。
   - `Some(Ok(1))`——读到 1 字节 = 异常数据(idle 连接上服务器不该主动发东西)→ `false`(不可复用)。注释里的 assumption:"we assume server would never actively send data first on an idle stream"(mod.rs#L212)。这是个合理的假设——HTTP 是请求-响应模型,服务器绝不会在没收到请求时主动发数据。如果 idle 连接上来了数据,要么是协议错误(服务器实现坏了)、要么是中间人注入、要么是连接被复用到了错误的会话——无论哪种,都不该复用这条连接。
   - `Some(Err(e))`——read 出错(连接断、RST 等)→ `false`(不可复用)。
   - `None`——`now_or_never` poll 一次返回 Pending,意味着 read 没立刻 ready——**连接还沉默着**(没数据、没 EOF、没 error),这就是"连接还活着"的信号 → `true`(可复用)。

#### 6.7.4 为什么 1 字节非阻塞 read 能探活

关键洞察:**对一条 idle 连接,任何"立刻发生"的 read 事件都意味着连接异常,而"没有立刻发生的 read 事件"意味着连接还正常沉默着**。

展开讲:

- **正常 idle 连接**:`read` 会阻塞(异步:`Pending`)——服务器没发东西,内核接收缓冲区空,read 等数据。`now_or_never` poll 一次,`Pending`,返回 `None` → 连接活着。
- **对端关了连接**(FIN):内核收到 FIN,`read` 立刻返回 0(EOF)。`now_or_never` 返回 `Some(Ok(0))` → 连接死了。
- **对端发了 RST**:`read` 立刻返回 `Err(ConnectionReset)`。`now_or_never` 返回 `Some(Err(...))` → 连接死了。
- **对端主动发数据**(异常):`read` 立刻返回数据。`now_or_never` 返回 `Some(Ok(n>0))` → 连接异常,不复用。

唯一不能探出来的情况:**对端崩溃但本地完全没收到任何信号**(没 FIN、没 RST,链路静默中断)。这种情况下,`read` 也是 `Pending`(本地以为还在等数据),`test_reusable_stream` 误判成"活着"。这是这个技巧的**根本局限**——它只能探出"本地已经知道"的连接死亡,探不出"本地还不知道"的。

如何弥补这个局限?两道补充防线:① **idle watcher 的 `idle_poll`**(`connection.rs#L271`)——连接进池子后,后台持续 read 监视,一旦后续收到 FIN/RST/数据,立刻 pop 掉;② **应用层超时**——请求用这条连接发出去后,如果响应迟迟不来(read 阻塞),应用层的 read timeout 会兜底。这两道防线加上 `test_reusable_stream`,把死连接复用的概率压到极低(但不是零——"本地永远不知道对端没了"的情况仍要靠应用层超时兜底)。

#### 6.7.5 反面对比:hyper 和 Nginx 怎么做

**hyper 的 idle 探测**:`hyper-util` 的连接池有类似的 idle 探测机制——它假设"服务器不会主动发数据",所以 idle 连接上 read 出 0 字节或异常,就丢弃连接。和 Pingora 思路同源。区别在于 hyper 的池是 HTTP client 层的,探活时机和 Pingora 略有不同(hyper 在复用时和 idle 期间都探)。

**Nginx 的策略**:Nginx 不主动探活(至少在开源版里没有等价的 1 字节非阻塞 read)。它的死连接处理走"踩坑重试"路线——`proxy_next_upstream error timeout http_502 http_503 http_504;` 配置,踩中死连接(发请求失败)就换一个 upstream 重试。优点是简单(不探活,省一次 syscall),缺点是每次踩坑都要付失败代价(用户感知延迟抖动)。

对照表:

| 维度 | Pingora `test_reusable_stream` | hyper `hyper-util` 池 | Nginx upstream |
|------|--------------------------------|----------------------|----------------|
| 探活时机 | 复用前 + idle 期间 | 复用前 + idle 期间 | 不主动探活 |
| 探活手段 | 1 字节非阻塞 read + now_or_never | 类似(read 0 字节判定) | 无(靠发请求失败) |
| 死连接处理 | 丢弃,走 new_stream 重建 | 丢弃,重建 | 踩坑后 proxy_next_upstream 重试 |
| 用户感知 | 大概率无感(预防) | 大概率无感(预防) | 有延迟抖动(踩坑时) |
| 代价 | 每次复用一次极便宜的非阻塞 read | 类似 | 每次 syscall 的失败 + 重试代价 |

Pingora 选了"预防"路线,因为代理场景下用户可见的 502/延迟抖动代价,远大于一次非阻塞 read 的代价。这是 Cloudflare 在 40M+ req/s 规模下验证过的取舍。

#### 6.7.6 为什么用 `now_or_never` 而不是 `try_read` / `poll_read`

一个自然的疑问:Tokio 的 `AsyncReadExt` 有 `try_read`(对应 `read` 但不阻塞),为什么不用它,而是绕一圈用 `now_or_never(stream.read(...))`?

答案微妙。Tokio 的 IO 类型(TcpStream 等)的 `poll_read` 在底层是 `mio` 的一次 `epoll_ctl`/`epoll_wait` 配合——非阻塞 IO + reactor 注册 waker。`try_read` 在异步上下文里是个语义模糊的东西(它要做非阻塞 read,但 AsyncRead 的接口是 `poll_read`,没有"非阻塞 try"的对应)。

`now_or_never(stream.read(...))` 的精妙在于:`stream.read(...)` 返回一个 future(`AsyncReadExt::read` 返回 `Read<'_, Self>`),`now_or_never` 对这个 future **poll 一次**——这一次 poll 内部会调 `poll_read`,如果 `poll_read` 返回 `Ready`,future 完成;如果返回 `Pending`,future 不完成,`now_or_never` 返回 `None`。这精确地实现了"非阻塞试一次 read"的语义,而且用的是标准 `AsyncRead` 接口,不依赖额外的 `try_read` API。

但有一个坑:`now_or_never` poll 的 future,如果它在内部 yield 了(cooperative budget 让出),会立刻被 `now_or_never` 当成 `Pending`(返回 None),即使 future 其实在内核层面已经 ready。这就是为什么要 `tokio::task::unconstrained`——它屏蔽 yield,保证 `stream.read` 在内核 ready 时一定返回 `Ready`(不被 budget 让出干扰),`now_or_never` 的判断才准确。注释里专门解释了这一点。

> **钉死这件事(`test_reusable_stream` 的精髓)**:1 字节非阻塞 read 是探活的最小代价——一次 syscall(`epoll_wait` + `recv`),不消费真实数据(只读 1 字节,且对 idle 连接这 1 字节要么没有要么是异常)。`now_or_never` + `unconstrained` 的组合精确实现了"poll 一次 read,ready 就用,Pending 就认为活着"的语义,绕开了 Tokio budget 让出的干扰。它能探出"本地已知道"的连接死亡(EOF/RST/异常数据),探不出"本地还不知道"的(链路静默中断)——后者由 idle watcher 的持续监听 + 应用层超时兜底。对照 Nginx 的"踩坑重试",这是预防 vs 补救的取舍,Pingora 选预防,因为代理场景下用户可见故障的代价更高。

### 技巧二:`offload_threadpool` —— 把建连 offload 到 `current_thread` runtime

第二个硬核技巧:`OffloadRuntime` 为什么用一组 `current_thread` runtime 而不是别的方案。理解它,等于理解 Pingora 对"async 代码的 CPU 密集活怎么不阻塞 reactor"的回答。

#### 6.7.7 问题:TLS 握手是 CPU 密集的 async 代码

建连有两段:① TCP connect(主要是等一个 RTT,IO 密集,不占 CPU);② TLS 握手(包括非对称运算:ECDHE 椭圆曲线运算、证书链验证、密钥派生——CPU 密集)。

TLS 握手在 Pingora 里是 `async fn`(`tls::connect`),它内部用 boringssl/openssl 的异步适配(`boring_tokio.rs` 把同步的 SSL_connect 包成 async,基于 SSL_get_error + retry)。握手过程中,非对称运算是**同步**的(在调用 SSL_connect 的线程上算),只是 IO 等待(等 ServerHello 等回包)是异步的。

如果这个握手在请求所在的 reactor 线程上跑,问题就来了:非对称运算那几百微秒到几毫秒的 CPU 时间,reactor 线程**完全占用**在算数学题上,没空 poll 别的 task。本来 ready 的别的连接的 read/write 被推迟,产生连锁延迟。

#### 6.7.8 朴素方案:为什么都不够好

**朴素方案一:`tokio::spawn`(在当前 multi_thread runtime 上跑)。** 握手 future spawn 到当前 runtime,Tokio 调度器把它分配到某个 worker 线程上跑。问题是:这个 worker 线程同时也要跑别的 task(别的请求的处理),握手 CPU 占用它,别的 task 还是受影响。`spawn` 只是让出当前 task 的执行权,没隔离 CPU。

**朴素方案二:`tokio::task::spawn_blocking`。** 这是 Tokio 给"阻塞代码"用的——把一个同步 `FnOnce() -> R` 投递到 blocking 线程池(一组专门跑阻塞代码的 OS 线程)。可建连是**异步**的(`async fn`),`spawn_blocking` 的签名是同步函数,跑不了 async future(没有 reactor 驱动)。理论上可以在 blocking 线程里 `block_on`(同步等 async future),但要新建 runtime 或者用当前 runtime 的 handle,语义复杂,而且 blocking 线程池的容量是有限的(默认 512),被握手占满会影响别的 blocking 操作。

**朴素方案三:multi_thread runtime 做隔离。** 给 offload 单独建一个 multi_thread runtime(N 个 worker 线程做 work stealing)。这样握手在这个隔离 runtime 上跑,不占请求 reactor。问题是:multi_thread runtime 的 work-stealing 调度器本身有开销(线程间偷 task、同步调度队列)。Pingora 的 OffloadRuntime 注释明确说:"the scheduling overhead of multithread tokio runtime, which can be 50% of the on CPU time of the runtimes"——work-stealing 的开销能占到 runtime on-CPU 时间的 50%。在 offload 这种 CPU 密集场景下,这个开销是净损失(没换来对应的吞吐提升)。

#### 6.7.9 Pingora 的解法:每个线程一个 `current_thread` runtime

Pingora 的解法(`offload.rs#L42-L64`)是给每个 offload 线程配一个**独立的 `current_thread` runtime**:

- 每个 OS 线程一个 runtime,线程内只有一个 reactor,一个调度队列,**无 work stealing**(就一个线程,没谁可偷)。
- runtime 之间完全隔离——一个 runtime 上的握手慢,不影响别的 runtime。
- `current_thread` 的调度开销极低(无偷 task 同步),正好契合 offload 这种 CPU 密集场景。

`get_runtime`(`offload.rs#L66-L76`)按 peer hash 选 shard,shard 内随机选线程,把负载分散到所有 offload 线程上。

`new_stream` 用 `rt.spawn(do_connect_future).await` 把建连投递到选中的 offload runtime,然后 await:

- `rt.spawn(...)`——在 offload runtime 上 spawn future,future 在那个 runtime 的线程上跑(包括 TCP connect 的等待、TLS 握手的非对称运算)。
- `.await`——请求 task 在自己的 reactor 上 await 这个 JoinHandle,等 offload 线程把 Stream 算完送回来。await 期间请求 reactor 让出(去跑别的 task),不阻塞。

这就达成了 offload 的目的:**建连的 CPU 密集部分在 offload 线程上算,请求 reactor 不被占用**。

#### 6.7.10 为什么是 `current_thread` 不是 multi_thread

核心是 Pingora 对 work stealing 的取舍。Work stealing 在以下场景划算:

- task 多、task 轻均衡。
- 某个线程空了,从别的线程偷 task 来跑,提高利用率。

但在 Pingora 的 offload 场景:

- task 数量 = 并发建连数(可能很多,但每个 task 都很重——TLS 握手是 CPU 密集的)。
- 偷 task 涉及跨线程同步(原子操作、锁),开销不小。
- Pingora 的观察是:在 offload 这种 CPU 密集负载下,work stealing 的同步开销占比极高(50%)。因为每个 task 都在拼命算 CPU,调度器偷 task 的同步操作叠加起来很显著。

`current_thread` runtime 无 work stealing,无调度同步——每个线程独立跑自己的 reactor 和 task,开销最低。代价是:线程间不偷 task,如果某个线程上排队的建连多(建连慢),它后面的建连要排队等(不会偷到别的空闲线程)。Pingora 用 `shard 内随机` 缓解这一点——同一个 shard 内多个线程随机分散,大概率不会全堆在一个线程上。

这个取舍承接了 Pingora NoStealRuntime 的核心思想(P5-15 详讲)——Pingora 全程偏爱 `current_thread` runtime,认为 work stealing 在它的负载模式下是净开销。OffloadRuntime 是这个思想在连接池层的应用。

#### 6.7.11 反面对比:Tokio `spawn_blocking` 和 Go runtime

**Tokio `spawn_blocking`**:设计目标是同步阻塞代码(FnOnce),不跑 async future。Pingora 的建连是 async,不能直接用。这是设计目标不匹配,不是 `spawn_blocking` 不好。

**Go runtime**:Go 的 goroutine 调度器天然把 IO 阻塞和 CPU 计算统一处理——一个 goroutine 阻塞在 IO 时,运行时把它挂起,把 M(系统线程)让给别的 goroutine;CPU 计算密集的 goroutine 占着 M 跑,但 Go 的 GOMAXPROCS 限制了同时跑 CPU 的 goroutine 数(默认 = CPU 核数),不会让 CPU 密集 goroutine 把所有 M 都占满。Go 的这个模型对"IO + CPU 混合"负载很自然,不需要手动 offload。代价是 Go runtime 的复杂度(GMP 模型)和 GC 开销。Pingora 用 Rust + Tokio,要自己处理"CPU 密集不阻塞 reactor",OffloadRuntime 是这个取舍的产物。

对照表:

| 维度 | Pingora OffloadRuntime | tokio spawn_blocking | tokio spawn(multi_thread) | Go goroutine |
|------|------------------------|----------------------|---------------------------|--------------|
| 目标负载 | async CPU 密集(建连/TLS) | 同步阻塞代码 | 通用 async | 一切(统一) |
| 隔离性 | 完全隔离(独立 runtime) | 部分(blocking 池) | 无(同 runtime) | 部分(GOMAXPROCS) |
| work stealing | 无(current_thread) | 无(独立线程) | 有(multi_thread) | 有(GMP) |
| 调度开销 | 极低 | 低 | 较高(50% 注释) | 中(Go runtime 复杂) |
| 何时用 | async CPU 密集要隔离 | 同步阻塞要隔离 | 通用 async | 一切 |

Pingora 的 OffloadRuntime 是个特化方案——针对"async + CPU 密集 + 要隔离"这个特定场景,用 `current_thread` runtime + OS 线程的组合,避开了 multi_thread 的 work stealing 开销。这是 NoStealRuntime 思想在连接池层的应用。

> **钉死这件事(OffloadRuntime 的精髓)**:offload 解决的是"async CPU 密集代码不阻塞请求 reactor"。Tokio 的 `spawn_blocking` 给同步阻塞代码用,跑不了 async;`spawn` 不隔离 CPU。Pingora 给每个 offload 线程配一个 `current_thread` runtime,既能让 async future 跑(有 reactor),又无 work stealing 开销(单线程无偷)。代价是线程间不偷 task,慢建连要排队(用 shard 内随机缓解)。这是 Pingora 对 NoSteal 思想的连贯应用——全书从 NoStealRuntime 到 OffloadRuntime,一致的取舍:work stealing 在 Pingora 的负载下是净开销,用多个单线程 runtime 替代。

---

## 章末小结

### 回扣主线

本章属于**转发设施**这一面(数据面),而且是第 2 篇(转发·连接池)的招牌章。`TransportConnector` 做的事,本质是把"到 upstream 的连接"这个资源管起来:

- **建连**:`new_stream` 调 `do_connect`(TCP + TLS),可选 offload 到专用线程池。
- **复用**:`reused_stream` 从池子里按 `reuse_hash` 取一条,`test_reusable_stream` 探活。
- **池子**:`ConnectionPool` 按 GroupKey 分桶,每桶一个 `PoolNode`(无锁 hot_queue + 兜底 HashMap),全局 thread-local LRU 限上限。
- **idle 监视**:`idle_poll` 后台持锁监听,被借走/被淘汰/连接异常时退出。

这一层是数据面的承重墙——`upstream_peer` 钩子选完 peer,框架调 `get_stream(peer)` 拿到一条 Stream,后续 HTTP 协议层(P2-07/P4-12~14)就在这条 Stream 上跑。`TransportConnector` 把"连接怎么建/怎么复用/怎么探活/怎么 offload"这堆复杂度全包了,上层(HTTP connector、代理逻辑)只看到一个"给我 peer,还我 Stream"的简单 API。

承接方面:本章强承接《Tokio》——`AsyncRead`/`AsyncWrite` 是 Stream 的基础(reactor/mio/edge-triggered 一句带过指路 `[[tokio-source-facts]]`),`spawn_blocking` 思想启发了 OffloadRuntime(但 Pingora 用 `current_thread` runtime 跑 async,不用 `spawn_blocking` 跑同步),`tokio::sync::Mutex` 实现"idle watcher 和请求方的安全交接"。同级对照《hyper》——hyper-util 的连接池思路同源(1 字节探活、per-host 分桶),但 hyper 是 HTTP client 层的池,Pingora 是代理层的池(要考虑 proxy 链路、TLS 多后端、SNI 多变)。强对照《Envoy》——Envoy 的 upstream 连接池在 HCM 和 router filter 之间,设计更复杂(per-cluster pool,thread-local),Pingora 的池更简洁(一个全局 `ConnectionPool`,thread-local 在 LRU 这层)。对照 Nginx——Nginx 的 upstream keepalive 不主动探活(踩坑重试),Pingora 选了 `test_reusable_stream` 预防路线。

### 五个为什么

1. **为什么连接池里存的是 `Arc<Mutex<Stream>>` 而不是裸 `Stream`?** 因为同一条连接要被三方共享所有权(池子、idle watcher、借出去的请求),且同一时刻只能一方操作它(read/write 不能并发)。`Arc` 共享所有权,`Mutex` 保证互斥。idle watcher 持 owned guard 监视,请求方借的时候先 `notify_use` 唤醒 idle watcher 退出(释放锁),自己再 `lock` 拿到。

2. **为什么 `PoolNode` 要分无锁 hot_queue 和加锁 HashMap 两层?** 高 RPS 下同一个 peer 的连接被极频繁借还,一把锁守一个 HashMap 会成为锁争用瓶颈。16 槽的 `crossbeam::ArrayQueue` 无锁队列做快路径(大部分借还不撞锁),溢出的走加锁 HashMap 慢路径。这是为高并发场景专门设计的两层结构。

3. **为什么 `test_reusable_stream` 用 1 字节非阻塞 read 探活?** 半开连接是连接池最大的坑(TCP 沉默,对端断了本地不知情)。1 字节非阻塞 read 用一次 syscall 探出"本地已知道"的连接死亡(EOF/RST/异常数据),不消费真实数据(只读 1 字节)。`now_or_never` + `unconstrained` 精确实现"poll 一次 read"语义。探不出的(本地还不知道对端没了)由 idle watcher 持续监听 + 应用层超时兜底。

4. **为什么 offload 用 `current_thread` runtime 而不是 `spawn_blocking` 或 multi_thread runtime?** 建连是 async CPU 密集(TLS 握手),`spawn_blocking` 跑同步阻塞代码跑不了 async;multi_thread runtime 的 work-stealing 调度开销能占 50% on-CPU 时间。每个 offload 线程一个 `current_thread` runtime,既能跑 async(有 reactor),又无 work stealing 开销。代价是线程间不偷 task,用 shard 内随机缓解。这是 NoSteal 思想在连接池层的应用。

5. **为什么需要 `PreferredHttpVersion`?** 有些服务器声称支持 h2 但实际不靠谱(握手后表现异常)。业务尝过一次后可以 `prefer_h1(peer)` 标记它,以后永远走 h1(ALPN override)。这是个学习机制,让代理从失败中调整,避免反复踩坑。对照 Nginx 只能全局配 `proxy_http_version`,Pingora 的 per-peer 学习更灵活。

### 想继续深入往哪钻

- **源码**:把 `pingora-core/src/connectors/` 四个文件(`mod.rs`/`l4.rs`/`offload.rs`/`tls/`)和 `pingora-pool/src/` 三个文件(`lib.rs`/`connection.rs`/`lru.rs`)逐个对照本章读一遍。重点看 `mod.rs` 的 `TransportConnector` 五个 API(`new_stream`/`reused_stream`/`release_stream`/`get_stream`/`prefer_h1`)、`connection.rs` 的 `PoolNode`(无锁 hot_queue)和 `idle_poll`(select! 三分支)、`offload.rs` 的 `init_pools`(`current_thread` runtime)。
- **Tokio spawn_blocking 和 current_thread runtime**:本章一句带过的 `spawn_blocking` 内部(blocking 线程池)、`current_thread` runtime 的 block_on 驱动模型、《Tokio》详讲。理解了它们的差异,再看 OffloadRuntime 会觉得每个选择都顺理成章。详见 `[[tokio-source-facts]]`。
- **hyper-util 连接池**:hyper 的连接池在 `hyper-util` crate(不在 hyper 主仓),思路和 Pingora 同源(1 字节探活、per-host 分桶)。读它的 `pool.rs`/`client/Pool` 实现,对比 Pingora 的 `ConnectionPool`,看两个库怎么落地同一套思想。
- **crossbeam ArrayQueue**:`crossbeam-queue::ArrayQueue` 是无锁环形队列,`PoolNode` 的 hot_queue 用的就是它。读它的实现(CAS + 环形缓冲),理解为什么"无锁"在单生产者-单消费者或多生产者-多消费者场景下能比 Mutex 快。
- **NoStealRuntime**:本章 OffloadRuntime 用 `current_thread` runtime 的思想,在 P5-15 的 `pingora-runtime`/`NoStealRuntime` 里有更完整的应用(整个 Pingora 的请求处理 runtime 也是多 `current_thread` 池)。读完 P5-15 再回看本章,会发现 OffloadRuntime 是 NoSteal 思想的小规模预演。

### 引出下一章

`TransportConnector` 解决了"到 upstream 的 L4/TLS 连接怎么建/复用",返回的是一条 `Stream`(`Box<dyn AsyncRead + AsyncWrite + ...>`)。可 HTTP 代理要的不是"一条 TCP 字节流",而是"一条 HTTP 会话"——要在 Stream 之上建 HTTP/1 状态机(发请求行、收响应头、管 keep-alive)或 HTTP/2 会话(多路复用、流控、ALPN 协商 h2)。

这就是下一章 **P2-07 HTTP connector:L7 连接与 h1/h2 会话** 要讲的。HTTP connector 在 `TransportConnector` 给的 Stream 之上,建立 L7 的 HTTP 会话:ALPN 协商出 h1 还是 h2,h1 起 keep-alive 循环(一条连接发一个请求,等响应,继续发下一个),h2 起多 stream(一条连接并发多个请求)。HTTP connector 是连接池(L4)和协议层(h1/h2,第 4 篇)之间的桥梁——L4 管字节流,L7 管 HTTP 语义。

---

> **本章源码引用**(pingora @ v0.8.1, commit `719ef6cd`):
> - `pingora-core/src/connectors/mod.rs#L46-L82`(`ConnectorOptions`)、`#L142-L149`(`TransportConnector` 结构)、`#L151`(`DEFAULT_POOL_SIZE=128`)、`#L155-L176`(`new`)、`#L181-L199`(`new_stream`)、`#L202-L249`(`reused_stream`)、`#L258-L279`(`release_stream`)、`#L287-L298`(`get_stream`)、`#L301-L303`(`prefer_h1`)、`#L308-L328`(`do_connect`)、`#L331-L344`(`do_connect_inner`)、`#L346-L373`(`PreferredHttpVersion`)、`#L379-L400`(`test_reusable_stream`)
> - `pingora-core/src/connectors/offload.rs#L23-L29`(`OffloadRuntime` 结构)、`#L42-L64`(`init_pools`,`current_thread` runtime)、`#L66-L76`(`get_runtime`,shard 内随机)
> - `pingora-core/src/connectors/l4.rs#L92-L211`(`connect`,TCP/UDS 建连)、`#L213-L256`(`bind_to_random`)
> - `pingora-core/src/connectors/tls/boringssl_openssl/mod.rs#L153-L265`(`connect`,TLS 握手 + ALPN)、`#L244-L246`(`alpn_override` 优先于 `peer.get_alpn()`)
> - `pingora-pool/src/connection.rs#L51-L70`(`PoolConnection`)、`#L75-L158`(`PoolNode`,无锁 hot_queue + 兜底 HashMap,`HOT_QUEUE_SIZE=16`)、`#L164-L168`(`ConnectionPool` 结构)、`#L228-L263`(`get`/`put`)、`#L271-L310`(`idle_poll`,select! 三分支)、`#L347-L366`(`read_with_timeout`,1 字节读)
> - `pingora-pool/src/lru.rs#L42-L50`(`Lru`,`ThreadLocal<RefCell<LruCache>>`)、`#L66-L95`(`put`/`add`,返回淘汰 meta)
> - `pingora-core/src/upstreams/peer.rs#L366-L370`(`BasicPeer::reuse_hash`)、`#L664-L689`(`HttpPeer::peer_hash`/`Hash`)、`#L707-L755`(`HttpPeer::Peer` impl,`reuse_hash`/`matches_fd`)
> - `pingora-core/src/protocols/tls/mod.rs#L50-L166`(`ALPN` enum,`to_wire_preference`)
> - `pingora-core/src/protocols/mod.rs#L48-L107`(`UniqueID`/`IO` trait,`Stream = Box<dyn IO>`)
>
> **承接**:
> - 《Tokio》`AsyncRead`/`AsyncWrite`/reactor(mio epoll)/`spawn_blocking`/`current_thread` runtime/budget=128——一句带过指路 `[[tokio-source-facts]]`,本章只用其 API 和概念,不重讲内部;
> - 《hyper》hyper-util 连接池——同级对照(都基于 1 字节探活思想),一句带过指路;
> - 《Envoy》upstream 连接池(per-cluster pool, thread-local)——强对照,filter chain 一句带过指路;
> - NoStealRuntime(本书 P5-15)——OffloadRuntime 是 NoSteal 思想的小规模预演,完整应用在 P5-15;
> - bytes/零拷贝——留 P2-08(HttpTask 零拷贝透传)。
>
> **本章源码印象修正**(写时核实并明确的、易被老资料带偏的事实):
> - **池的真实结构是 `RwLock<HashMap<GroupKey, Arc<PoolNode>>>` + 每个 PoolNode 内部 `crossbeam::ArrayQueue`(无锁热队列,16 槽)+ `Mutex<HashMap>`(兜底)**,不是凭记忆的"一把大锁守一个大 HashMap"。无锁热队列是抗锁争用的核心设计(`HOT_QUEUE_SIZE=16`)。
> - **全局 LRU 是 `ThreadLocal<RefCell<LruCache>>`**(每线程一个 LruCache),不是一把锁守一个全局 LruCache。这是抗锁争用的另一处设计,代价是全局容量上限变成"每线程上限 × 线程数"的近似。
> - **`test_reusable_stream` 用 `tokio::task::unconstrained(stream.read(...)).now_or_never()`**,不是裸的 `try_read` 或 `poll_read`。`unconstrained` 屏蔽 cooperative budget 让出,保证 `now_or_never` 在内核 ready 时准确判断(注释专门解释)。读 1 字节(`[0; 1]`),不是 0 字节。
> - **`idle_poll` 持有连接的 owned guard(`OwnedMutexGuard<Stream>`)**,在后台 read 监视,被借走时(`watch_use` oneshot)或被淘汰时(`notify_evicted`)或读到 EOF/异常数据时退出。这是"连接在 idle watcher 和请求方之间安全交接"的关键。
> - **`OffloadRuntime` 用 `current_thread` runtime(不是 multi_thread)**,注释原话:multi_thread 的 work-stealing 调度开销能占 50% on-CPU 时间。每个 offload 线程一个独立 runtime,无 work stealing。这是 NoSteal 思想在连接池层的应用。
> - **`release_stream` 里 `try_lock_owned().unwrap()` 安全**,因为刚 `Arc::new(Mutex::new(stream))` 创建,只有自己一个 owner,锁没人抢(注释 "safe as we just created it")。
> - **`reuse_hash` 的内容因 peer 类型而异**:`BasicPeer` 只哈希 address(`peer.rs#L366-L370`);`HttpPeer` 哈希 address + scheme + proxy + sni + client cert + verify_cert + verify_hostname + alternative_cn + psk(s2n)+ group_key + max_h2_streams(`peer.rs#L671-L688`)。所以"两条连接算同一条"的判定远比"同一个 addr"复杂——所有影响 TLS/HTTP 复用的字段都参与。
> - **`prefer_h1` 的实现是 `PreferredHttpVersion` 的 `RwLock<HashMap<u64, u8>>`**(peer hash → version),不是 per-peer 配置对象。注释 TODO 要分片以避免全局锁。
> - **`alpn_override` 在 TLS connect 里优先于 `peer.get_alpn()`**(`boringssl_openssl/mod.rs#L244`),即 `PreferredHttpVersion` 标记的 override 高于 peer 自己的 ALPN 配置。
