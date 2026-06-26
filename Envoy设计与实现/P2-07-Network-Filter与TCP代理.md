# 第 2 篇 · 第 7 章 · Network Filter 与 TCP 代理

> **核心问题**:上一章 P2-06 我们把流量推进到了 listener filter 的尽头——TCP 连接已经被 `SO_REUSEPORT` 分给某个 worker、TLS 探测/proxy_protocol 也在 `Connection` 对象建起来之前跑完了。可真正的字节流(`onData` 那一坨 TCP payload)还没动过——它要进一条新的链:**network filter 链**。这条链和 listener filter 链不一样,它跑在一条**已经存在的 `Connection` 上**,看到的是**真实的、与应用层协议无关的原始字节流**。问题是:这层"协议无关的字节流 filter"凭什么要存在?纯 TCP 流量(Redis、MySQL、Kafka、各种私有协议)在 Envoy 里怎么代理?最反直觉的——**HTTP Connection Manager(HCM)本身,居然就是这条链上的一个 filter**,和 `tcp_proxy` 这种"啥协议都不懂、纯字节透传"的家伙平起平坐,用同一套 `addReadFilter` 注册。这层统一性是怎么来的,又换来了什么代价?这一章就把这条字节流层的 filter 链拆透。

> **读完本章你会明白**:
> 1. **network filter 链为什么是"协议无关的字节流层"**——它在 `Connection` 建起来之后、任何应用层协议解码之前工作,看到的是裸字节。为什么需要这层(非 HTTP 流量也要代理:数据库、消息队列、私有协议),和 listener filter / http filter 各自的边界在哪。
> 2. **ReadFilter / WriteFilter 两条方向相反的链**——读向(downstream 进来的字节,`onData`)FIFO 推进,写向(downstream 发出去的字节,`onWrite`)因 `moveIntoList`/`moveIntoListBack` 的差异天然形成逆序,合起来就是字节流版的"洋葱模型"。network filter 也能拦双向字节流,不只是读。
> 3. **`FilterStatus::Continue` / `StopIteration` 怎么把异步性织进同步链**——一个 filter 想等外部结果(等 ratelimit 服务回包、等 upstream 连接建好),就返回 `StopIteration` 把链停住,事后通过 `continueReading()` 把链重新点着。这是 network filter 链能承载"异步 + 多步"治理的根本机制。
> 4. **`tcp_proxy` 是"不理解协议的纯字节透传"**——它 `onNewConnection` 里选 cluster→建 upstream 连接,`onData` 里把 downstream 字节原样 `encodeData` 给 upstream,upstream 回来的字节 `connection().write()` 回 downstream。它不解析任何协议,这是它和 HCM(理解 HTTP)的根本分野。
> 5. **HCM 是 network filter 的特例——这条统一性的代价与收益**——HCM 复用 network filter 的整套注册/连接管理/链推进机制,内部却把字节解码成 HTTP、再驱动一条独立的 http filter 链。朴素地"HTTP 和 TCP 代理各走一套独立机制"会撞什么墙(代码分叉、connection 管理无法统一、前置字节处理无法组合),Envoy 又为什么把这条统一性贯彻到底。

> **如果一读觉得太难**:先只记住四件事——① listener filter 跑完进 network filter 链,这层看的是**裸字节**,和协议无关(纯 TCP / 数据库 / 消息队列 / 私有协议都能在这里加工);② 这条链有读向(`onData`,FIFO)和写向(`onWrite`,逆序)两个方向,filter 想停就返回 `StopIteration`,事后 `continueReading()` 复活;③ `tcp_proxy` 是纯字节透传(不解析协议),`onData` 把 downstream 字节转发给 upstream 选出的连接;④ **HCM 就是个 network filter**,只不过它内部把字节解码成 HTTP、再驱动一条 http filter 链——这是 Envoy"一条连接的字节流先进 network filter 链、链上的 filter 决定走 HTTP 还是 TCP 透传"统一设计的根。这四条抓住,本章就拿到了 80%。

---

## 〇、一句话点破

> **network filter 链是字节流层的 filter——它在 `Connection` 对象建起来之后工作,看到的是协议无关的裸字节。一条连接的字节流先进 listener filter(连接握手前),再进 network filter 链(连接建好后的字节加工);链上的 filter 可以是 `tcp_proxy`(纯字节透传,不理解任何协议)、HCM(把字节解码成 HTTP 再驱动 http filter 链)、或任意私有协议代理。HCM 和 `tcp_proxy` 用同一套 `addReadFilter` 注册、同一套 `FilterStatus`/`continueReading` 机制推进——这是 Envoy 把"HTTP 处理"和"纯 TCP 代理"统一进同一个 connection 管理框架的根。**

这是结论,不是理由。本章倒过来拆:先讲 network filter 链在整条旅程里的位置和它"协议无关"的本质,再拆 ReadFilter/WriteFilter 两条方向相反的链与 `FilterStatus` 的异步推进语义,接着把 `tcp_proxy` 的纯字节透传彻底拆透(选 cluster、建 upstream、来回转发),最后回到那个最反直觉的设计——HCM 为什么是 network filter、这套统一性换来了什么。

---

## 一、network filter 链在整条旅程里的位置:字节流世界的大门

### 从 listener filter 的尽头接过来

回到全书那张旅程图,我们把目光聚焦在 P2-06 和 P3-08 之间这段空白:

```
   请求进来(TCP 连接)
     │
     ▼
   Listener(SO_REUSEPORT 把连接分给某 worker)         ── P2-05
     │
     ▼
   Listener Filter 链(连接握手前:TLS 探测 / proxy_protocol / original_dst)
     │   ↑ 这些 filter 看到的是"还没建起 Connection 对象的 socket",
     │     用 ListenerFilterBuffer 偷看前几个字节,在 Connection 诞生前做决策
     │   ── P2-06 的尽头:Connection 对象正式建起来,accept 流程结束
     ▼
   ┌─────────────────────────────────────────────────────────────┐
   │ Network Filter 链(连接建起来之后的字节流加工)             │ ◀── 本章
   │   读向 onData:ratelimit → ext_authz → HCM(或 tcp_proxy)  │
   │   写向 onWrite:同一批 filter 的逆序                       │
   └─────────────────────────────────────────────────────────────┘
     │
     ├── 如果链上是 HCM:字节被解码成 HTTP → 进 http filter 链 ── P3-08/10
     │
     └── 如果链上是 tcp_proxy:字节原样转发给 upstream cluster ── 本章后半
     │
     ▼
   Cluster → LB → Endpoint(连接池 → 后端)                    ── 第 4 篇
```

P2-06 讲清楚了:listener filter 跑在 `Connection` 对象诞生**之前**,它看到的是一个还没正式 accept 完的 socket,用 `ListenerFilterBuffer` 偷看头几个字节(TLS ClientHello、proxy_protocol 头)就做决策——TLS 该不该解、真实客户端 IP 是谁、原始目的地址是哪。这套跑完,`Connection` 对象才正式建起来,fd 被绑进这个 worker 的事件循环,真正的 TCP 字节流(`onData` 拿到的 payload)开始涌进来。

**这之后的字节加工,就是 network filter 链的事。** 它和 listener filter 有三个根本区别:

| 维度 | Listener Filter(P2-06) | **Network Filter(本章)** | HTTP Filter(P3-10) |
|------|------------------------|---------------------------|--------------------|
| 工作时机 | `Connection` 建起**之前** | `Connection` 建起**之后** | HTTP 解码**之后** |
| 看到的数据 | socket 前几个字节(`ListenerFilterBuffer`) | **裸字节流**(`Buffer::Instance`) | 结构化 HTTP 请求/响应 |
| 是否理解协议 | 完全不(只看 magic bytes) | **完全不(协议无关)** | 只懂 HTTP |
| 调用入口 | `onAccept(cb)` | `onNewConnection()` + `onData(data, end_stream)` | `decodeHeaders`/`decodeData`/... |
| 能拦写向吗 | 不能(socket 还没建) | **能(`onWrite`)** | 能(`encode*`) |
| 终结方式 | 拒绝就关 socket | 返回 `StopIteration`、关连接、或自己接管 | `sendLocalReply` / reset stream |

最关键的区别在第二、第三行:**network filter 看到的是裸字节流,且和协议无关**。它不知道你发的是 HTTP、Redis RESP、MySQL 协议、还是某个内部私有协议——它只看见 `Buffer::Instance` 里一坨字节,可以基于这坨字节做任何加工:计数、限速、鉴权、转发、或者把这坨字节当成某协议解出来再驱动一条更细的 filter 链。

> **钉死这件事**:network filter 链是"字节流世界的大门"——它一脚踩在 listener filter 的尽头(`Connection` 刚建好),另一脚决定这连接接下来去哪(被 HCM 解成 HTTP?被 tcp_proxy 原样转走?被 echo 回环?被 ratelimit 拦下?)。它的"协议无关"是它存在的根,也是它能统一代理 HTTP/数据库/消息队列/私有协议的根。

### 为什么需要一层"协议无关"的字节流 filter

朴素的问题:既然 HTTP 有自己的 http filter 链(P3-10),为什么还要在它前面再垫一层 network filter?为什么不直接"是 HTTP 就进 http filter 链,不是 HTTP 就……就怎么办"?

答案的第一层:**微服务世界里,流量不只有 HTTP**。Envoy 的定位是"通用数据面",它要代理的不止是 REST/gRPC,还有:

- **数据库**:MySQL(`mysql_proxy`)、Postgres(`postgres_proxy`)、Redis(`redis_proxy`)、MongoDB(`mongo_proxy`)、Cassandra、Redis 协议都要代理、要做读写分离、要做 SQL 审计。
- **消息队列**:Kafka(`kafka_broker`)、RocketMQ、各种 MQ 的协议都是私有 TCP 协议,要代理、要做流量治理。
- **RPC 框架**:Dubbo(`dubbo_proxy`)、Thrift(`thrift_proxy`)、TARS,这些都不是 HTTP,得有地方代理。
- **私有协议 / 通用代理**:有的协议 Envoy 没内置解析器,但只想做透传 + 限速 + 鉴权——`tcp_proxy` 纯字节透传、`generic_proxy` 框架让你插自己的 codec。

如果 Envoy 只会 HTTP,这些场景全得另找代理,或者每个协议造一套独立的 filter 机制——这正是 Nginx 早期的痛(Nginx 的 `stream {}` 模块后来才加,且和 `http {}` 是两套机制)。**Envoy 的回答是:把"协议无关的字节流 filter"做成一层,任何协议的代理都建在这层之上**——你想代理 Redis,就写个 redis_proxy network filter(内部解析 RESP);你想代理 HTTP,就写个 HCM network filter(内部解析 HTTP);你想啥都不解析只透传,就写个 tcp_proxy network filter。

答案的第二层更深:**即便你只代理 HTTP,字节流层也提供了 HTTP filter 链做不到的东西**。HTTP filter 链是"HTTP 解码之后"才跑的——它看到的已经是结构化的 `RequestHeaderMap`/`RequestTrailerMap`。可有些治理逻辑,根本不需要、也不应该等到 HTTP 解码完才做:

- **`ratelimit`(network 层)**:字节流一进来就限流,不用等解析完 HTTP 头才知道这连接要打多少请求。
- **`ext_authz`(network 层)**:连接级别鉴权(基于源 IP、SNI、mTLS 身份),不用看 HTTP 头。
- **`rbac`(network 层)**:基于连接四元组做访问控制,纯字节流层就能决策。
- **`connection_limit`**:限制并发连接数,这必须在字节流层、连接刚建时就做。
- **`tcp_bandwidth_limit`**:字节级限速,直接看 `onData` 的字节数。

> **不这样会怎样**:如果只有 HTTP filter 链、没有字节流层,这些治理逻辑要么硬塞进 HTTP filter(职责错位——HTTP filter 凭什么管"连接数"?),要么得另造一套和 HTTP filter 平行的机制(代码分叉、两套心智模型)。Envoy 把字节流层和 HTTP 层分开,字节流层做"连接级、协议无关"的治理,HTTP 层做"请求级、HTTP 特有"的治理——**各管各的边界,组合自然**。

### `well_known_names`:network filter 的全家福

打开 [`source/extensions/filters/network/well_known_names.h`](../envoy/source/extensions/filters/network/well_known_names.h),你会看到 Envoy 内置的全部 network filter 名字(`envoy.filters.network.*`)。把它们按"对字节做什么"分个类,这套设计的全貌就出来了:

```
   network filter 全家福(按职责分类,见 well_known_names.h)

   【纯字节透传】
     tcp_proxy           纯 TCP 透传,不理解协议(本章主角)
     sni_cluster         按 SNI 选 cluster(配 tcp_proxy 用)
     sni_dynamic_forward_proxy  按 SNI 动态查 cluster

   【协议代理(内置 codec,理解某种协议)】
     http_connection_manager   ★ HCM——理解 HTTP(承 P3-08)
     redis_proxy         理解 Redis RESP
     mongo_proxy         理解 MongoDB wire protocol
     mysql_proxy         理解 MySQL
     postgres_proxy      理解 PostgreSQL
     kafka_broker        理解 Kafka
     dubbo_proxy         理解 Dubbo
     thrift_proxy        理解 Thrift
     rocketmq_proxy      理解 RocketMQ
     zookeeper_proxy     理解 ZooKeeper
     generic_proxy       ★ 协议无关代理框架,插自己的 codec(较新)

   【连接级治理(协议无关)】
     ratelimit / local_ratelimit   连接级限流
     ext_authz                      外部鉴权
     rbac                           连接级访问控制
     connection_limit               并发连接数限制
     tcp_bandwidth_limit            字节级限速
     ext_proc                       旁路给外部进程处理
     wasm                           跑 Wasm 字节码

   【杂项】
     echo                字节回环(调试用)
     direct_response     直接回一段字节然后关连接
     set_filter_state    设置 filter state(给下游 filter 用)
     match_delegate      按 matcher 委托到子链
     geoip / reverse_tunnel / dynamic_modules / ...
```

这张表的精妙之处在于:**它们全部都是 network filter——都用同一套 `ReadFilter`/`WriteFilter` 接口、同一套 `addReadFilter`/`addWriteFilter` 注册、同一套 `FilterStatus`/`continueReading` 推进机制**。HCM 没有任何特权(它在 listener 配置里就是一个 filter 名字 `envoy.filters.network.http_connection_manager`),`tcp_proxy`、`redis_proxy`、`echo`、`ratelimit` 全都一样。这套统一性是本章反复要回扣的主线。

> **钉死这件事**:network filter 不是"HTTP filter 的前置",而是一个**独立的、协议无关的字节流加工层**。它有自己的全生态:透传(tcp_proxy)、各种协议代理(HCM/redis/mysql/...)、连接级治理(ratelimit/rbac/connection_limit/...)。HTTP 处理(HCM)只是这层上一个"碰巧理解 HTTP"的 filter,和 redis_proxy 是"碰巧理解 Redis"的 filter 是同一回事。

---

## 二、network filter 的接口:ReadFilter、WriteFilter、FilterManager

现在动手看接口。所有 network filter 都实现 [`envoy/network/filter.h`](../envoy/envoy/network/filter.h) 里的几个抽象类,这条链的运行机制全写在它们的签名和注释里。

### ReadFilter:读向(downstream 字节进来)

```cpp
// envoy/network/filter.h  (简化示意,非源码原文)
class ReadFilter {
public:
  virtual ~ReadFilter() = default;

  // 连接刚建起来时被调一次。filter 可以做"一次性初始化"——
  // 启动 timer、选 route、建 upstream 连接(tcp_proxy 就在这里建)。
  // 返回 StopIteration 可以拦住后续 filter 的 onNewConnection。
  virtual FilterStatus onNewConnection() PURE;

  // 每次从 socket 读到字节就被调。data 是裸字节 Buffer,end_stream 表示
  // 对端是否半关闭。filter 可以修改 data(增删改字节),返回 StopIteration
  // 就停链,返回 Continue 就交给下一个 filter。
  virtual FilterStatus onData(Buffer::Instance& data, bool end_stream) PURE;

  // filter manager 注册它时调一次,把 callbacks(含 connection、dispatcher、
  // continueReading、upstreamHost)塞进来。filter 把 callbacks 存好,日后用。
  virtual void initializeReadFilterCallbacks(ReadFilterCallbacks& callbacks) PURE;
};
```

见 [ReadFilter 接口定义](../envoy/envoy/network/filter.h#L254-L294)。三个方法的分工很清晰:

- **`initializeReadFilterCallbacks`**:filter 被 `addReadFilter` 注册时调一次([filter_manager_impl.cc:28](../envoy/source/common/network/filter_manager_impl.cc#L28)),把"和 filter manager 沟通的回调"塞进来。这套 callbacks 里最关键的是 [`ReadFilterCallbacks`](../envoy/envoy/network/filter.h#L167-L249)——它提供 `connection()`(拿到这条 `Connection` 的引用,filter 就能 `connection().write(...)`、`connection().close(...)`、`connection().dispatcher()` 拿到事件循环)、`continueReading()`(停链后复活链)、`upstreamHost()`/`upstreamHost(host)`(读写当前选定的 upstream host,filter 间共享这个信息,比如 `tcp_proxy` 选完 host 写进去,ratelimit filter 拿来打日志)。
- **`onNewConnection`**:连接刚建起来、字节还没来(或刚来)时被调一次。这是"一次性初始化"的入口——`tcp_proxy` 在这里选 route、建 upstream 连接;HCM 在这里(对 QUIC)造 codec;`connection_limit` 在这里查并发数。
- **`onData`**:字节来的回调,filter 的主战场。

### WriteFilter:写向(downstream 字节发出去)

```cpp
// envoy/network/filter.h  (简化示意,非源码原文)
class WriteFilter {
public:
  virtual ~WriteFilter() = default;

  // 每次要往 socket 写字节就被调。data 是待写字节,end_stream 表示是否写完。
  // filter 可以修改 data(比如压缩、加 proxy_protocol 头),返回 StopIteration
  // 停链,返回 Continue 让字节继续往下走到 socket。
  virtual FilterStatus onWrite(Buffer::Instance& data, bool end_stream) PURE;

  virtual void initializeWriteFilterCallbacks(WriteFilterCallbacks&) {}
};
```

见 [WriteFilter 接口定义](../envoy/envoy/network/filter.h#L136-L160)。和 `ReadFilter` 对称——只不过它拦的是"发出去的字节"。比如 `tcp_proxy` 的 upstream 回来字节、要写回 downstream 时,这批字节会先穿 downstream 这条连接的 write filter 链(注意:`tcp_proxy` 自己用 `read_callbacks_->connection().write(data, ...)`,这个 write 会触发 write 链,见 [`onUpstreamData`](../envoy/source/common/tcp_proxy/tcp_proxy.cc#L1276-L1285))。

### Filter:一个 filter 同时管读向和写向

很多 filter 只关心读向(比如 `tcp_proxy`、HCM——它们主要在 `onData` 里干活)。但有些 filter 两个方向都要管(比如字节级限速、proxy_protocol 加头),它们实现 [`Filter : public WriteFilter, public ReadFilter`](../envoy/envoy/network/filter.h#L302):

```cpp
// envoy/network/filter.h
class Filter : public WriteFilter, public ReadFilter {};
```

注册时用 `addFilter`(不是 `addReadFilter`),它同时把 filter 加进读链和写链([filter_manager_impl.cc:20-23](../envoy/source/common/network/filter_manager_impl.cc#L20-L23)):

```cpp
void FilterManagerImpl::addFilter(FilterSharedPtr filter) {
  addReadFilter(filter);
  addWriteFilter(filter);
}
```

> **钉死这件事**:network filter 有读向(`onData`,downstream 字节进来)和写向(`onWrite`,downstream 字节发出去)两条链。只读的 filter 实现 `ReadFilter`、用 `addReadFilter` 注册;双向的 filter 实现 `Filter`(同时继承 `ReadFilter`+`WriteFilter`)、用 `addFilter` 注册。这套两向语义和 HTTP filter 的 decoder/encoder 两向(P3-10)同构——只是 HTTP filter 拦的是结构化请求/响应,network filter 拦的是裸字节。

### FilterManager:链怎么存、怎么推

filter 都注册到一个 [`FilterManager`](../envoy/envoy/network/filter.h#L308-L348)(每个 `Connection` 自己一个),实现是 [`FilterManagerImpl`](../envoy/source/common/network/filter_manager_impl.h#L121)。它内部用两条 `std::list` 分别存读 filter 和写 filter:

```cpp
// source/common/network/filter_manager_impl.h  (简化示意,非源码原文)
class FilterManagerImpl {
private:
  std::list<ActiveReadFilterPtr> upstream_filters_;   // 读链(命名怪,但就是读)
  std::list<ActiveWriteFilterPtr> downstream_filters_; // 写链
};
```

注意命名有点反直觉——`upstream_filters_` 存的是**读 filter**(字节从 downstream 进来,往 upstream 方向流),`downstream_filters_` 存的是**写 filter**(字节往 downstream 发出去)。命名按"字节往哪个方向流"而不是"哪个方向来的",初次看会犯晕,记住就好。

更关键的是这两条 list 的**插入方向不同**,这是字节流版"洋葱模型"的物理基础:

```cpp
// source/common/network/filter_manager_impl.cc  (简化示意)
void FilterManagerImpl::addReadFilter(ReadFilterSharedPtr filter) {
  // moveIntoListBack —— 插到链尾
  LinkedList::moveIntoListBack(std::move(new_filter), upstream_filters_);  // L29
}

void FilterManagerImpl::addWriteFilter(WriteFilterSharedPtr filter) {
  // moveIntoList —— 插到链头
  LinkedList::moveIntoList(std::move(new_filter), downstream_filters_);    // L17
}
```

见 [addReadFilter 用 moveIntoListBack](../envoy/source/common/network/filter_manager_impl.cc#L25-L30) 和 [addWriteFilter 用 moveIntoList](../envoy/source/common/network/filter_manager_impl.cc#L13-L18)。读 filter 链尾插(FIFO),写 filter 链头插(LIFO)。读向迭代时从头(`begin()`)往后走,写向迭代也从头(`begin()`)往后走——但因为插入方向相反,**同一个双向 filter 在读链和写链里的相对位置是逆序的**。

```
   配置顺序:ratelimit → HCM(注册时 addReadFilter/addWriteFilter 的顺序)
   
   读链 upstream_filters_  (moveIntoListBack, 尾插 → FIFO)
     [ratelimit] → [HCM]            onData 顺序:ratelimit 先, HCM 后
     begin()────────────────────▶  end()
   
   写链 downstream_filters_  (moveIntoList, 头插 → LIFO)
     [HCM] → [ratelimit]            onWrite 顺序:HCM 先, ratelimit 后
     begin()────────────────────▶  end()
   
   ── 合起来就是字节流版的"洋葱":请求(downstream 字节)从 ratelimit 进、HCM 出,
      响应(upstream 回来要写回 downstream 的字节)从 HCM 进、ratelimit 出。两向逆序。
```

> **不这样会怎样**:如果读链和写链都 FIFO,那 ratelimit 在两个方向都会先于 HCM 执行——可 ratelimit 的逻辑可能是"只在请求方向限速,响应方向放过",强行让它在响应方向也跑一遍没意义、还可能误拦。LIFO 写链保证"最先注册的 filter 最靠近字节流的内核端(读:最先看到进来的字节;写:最后看到出去的字节)"——这样的 filter 是最外层的"洋葱皮",进出都包着它,自然适合做"连接级治理"(连接一进来就管、出去的字节也都过它)。这正是 ratelimit/rbac/ext_authz 这类连接级 filter 的语义。

---

## 三、`FilterStatus` + `continueReading`:把异步性织进同步链

光有两条 list 还不够,filter 链的真正威力在于**每个 filter 都能决定"接下来这条链怎么走"**。这就是 [`FilterStatus`](../envoy/envoy/network/filter.h#L42-L47) 的语义:

```cpp
// envoy/network/filter.h
enum class FilterStatus {
  Continue,        // 继续把数据交给下一个 filter
  StopIteration    // 停下,不再往后传(链在这里被掐住)
};
```

只有两个值,但威力巨大。看读向的迭代逻辑——[`onContinueReading`](../envoy/source/common/network/filter_manager_impl.cc#L62-L98):

```cpp
// source/common/network/filter_manager_impl.cc  (简化示意,非源码原文)
void FilterManagerImpl::onContinueReading(ActiveReadFilter* filter, ReadBufferSource& buffer_source) {
  if (connection_.state() != Connection::State::Open) return;

  std::list<ActiveReadFilterPtr>::iterator entry;
  if (!filter) {
    entry = upstream_filters_.begin();          // 从链头开始
  } else {
    entry = std::next(filter->entry());         // 从"上一次停下的 filter"的下一个开始
  }

  for (; entry != upstream_filters_.end(); entry++) {
    if (!(*entry)->initialized_) {
      (*entry)->initialized_ = true;
      FilterStatus status = (*entry)->filter_->onNewConnection();   // 懒触发 onNewConnection
      if (status == FilterStatus::StopIteration) return;            // ← 停链
    }
    StreamBuffer read_buffer = buffer_source.getReadBuffer();
    FilterStatus status = (*entry)->filter_->onData(read_buffer.buffer, read_buffer.end_stream);
    if (status == FilterStatus::StopIteration) return;              // ← 停链
  }
}
```

每次某个 filter 返回 `StopIteration`,for 循环就 `return`,后面的 filter 这一轮不会被调到。**但链没有被销毁、也没被忘掉**——iterator 的位置(filter->entry())记在 `ActiveReadFilter` 里。等条件满足了,filter 调 [`continueReading()`](../envoy/envoy/network/filter.h#L176):

```cpp
// ActiveReadFilter 实现 ReadFilterCallbacks
void continueReading() override { parent_.onContinueReading(this, parent_.connection_); }
```

见 [ActiveReadFilter::continueReading](../envoy/source/common/network/filter_manager_impl.h#L160)。`continueReading()` 重新进 `onContinueReading`,但这次传的是 `this`(当前 filter),函数里 `entry = std::next(filter->entry())`——**从当前 filter 的下一个继续**。

### 这套机制为什么是 network filter 链的灵魂

这套"返回 StopIteration + 事后 continueReading 复活"看似简单,但它解决了 filter 链里最难的问题:**怎么把异步操作织进一条同步推进的链**。

一个具体场景:`ratelimit` filter 调外部限流服务(gRPC 调用),这调用是异步的——发完请求不能阻塞 worker 线程(那是 epoll 单线程模型,阻塞就全卡死)。可 `onData` 是同步函数,它必须马上返回。怎么办?

```cpp
// 伪代码(简化示意,非源码原文)
FilterStatus RatelimitFilter::onData(Buffer::Instance& data, bool end_stream) {
  if (status_ == Status::Calling) {
    // 上次的限流请求还在飞,先把链停住
    return FilterStatus::StopIteration;
  }
  // 第一次见到这连接的字节,发起限流请求(异步)
  client_->limit(*this, descriptors_);
  status_ = Status::Calling;
  return FilterStatus::StopIteration;   // ← 把链停住,等异步回调
}

// 异步回调(在 worker 的事件循环里被调度)
void RatelimitFilter::onComplete(LimitResponse resp) {
  status_ = (resp.status == OK) ? Status::Ok : Status::Denied;
  if (resp.status == OK) {
    read_callbacks_->continueReading();   // ← 复活链!字节继续往后走
  } else {
    read_callbacks_->connection().close(...);  // ← 拒绝,直接关连接
  }
}
```

真实源码见 [ratelimit filter 的 onData/onNewConnection](../envoy/source/extensions/filters/network/ratelimit/ratelimit.cc#L70-L94)——`status_ == Status::Calling` 时返回 `StopIteration`,否则 `Continue`,正是这个模式。ratelimit 在 `onNewConnection`(或第一次 `onData`)里发 gRPC 限流请求(`client_->limit(*this, ...)`),把 `status_` 置 `Calling`、返回 `StopIteration`;外部限流服务回包后,worker 的事件循环调度它的 `onComplete` 回调,回调里根据结果决定 `continueReading()`(放行)还是 `connection().close(...)`(拒绝)。**整个等待期间,worker 事件循环不阻塞,其他几千个连接照常被处理**——这就是 network filter 链能扛高并发的根。

不只是 ratelimit。`ext_authz`(网络层外部鉴权)、`ext_proc`(旁路给外部进程)都是这套模式:发外部请求 → `StopIteration` → 外部回包 → `continueReading()`。这套"异步 stop/continue"是 network filter 链的**通用异步原语**——只要一个 filter 要等外部结果(网络、定时器、upstream),都用它。

`tcp_proxy` 也用同一招,而且用得更彻底——它 `onNewConnection` 里要建 upstream 连接,这连接建立是异步的(TCP 三次握手要时间,upstream 的 epoll 事件回调要等)。`tcp_proxy` 在 `establishUpstreamConnection()` 里发完建连请求就返回 `StopIteration`,等 upstream 连接建好、连接池回调 [`onGenericPoolReady`](../envoy/source/common/tcp_proxy/tcp_proxy.cc#L899-L937) 时,在里面调 [`read_callbacks_->continueReading()`](../envoy/source/common/tcp_proxy/tcp_proxy.cc#L933) 把链复活。**这就是 `tcp_proxy` 能"先停链等 upstream、upstream 好了再继续"的根**。

> **不这样会怎样**:如果 filter 链只能"要么全跑完、要么直接关连接",没有"先停着、事后复活"的能力,那所有异步治理(外部限流、外部鉴权、upstream 建连)都得阻塞 worker 线程——可 worker 是单线程 epoll 模型(P1-03),阻塞一个连接就阻塞这个 worker 上几千个连接。`StopIteration` + `continueReading` 让 filter 能"暂时把这条链挂起、不占线程、等异步事件回来再继续"——这是 network filter 链能承载复杂异步治理的根本机制,和 HTTP filter 链的 `continueDecoding()`/`continueEncoding()`(P3-10)完全同构。

### onNewConnection 的懒触发:细节里的魔鬼

注意 [`onContinueReading`](../envoy/source/common/network/filter_manager_impl.cc#L82-L88) 里有一段容易看漏但很关键的代码:

```cpp
if (!(*entry)->initialized_) {
  (*entry)->initialized_ = true;
  FilterStatus status = (*entry)->filter_->onNewConnection();   // ← 懒触发!
  if (status == FilterStatus::StopIteration) return;
}
```

`onNewConnection` 不是在连接建好的那一刻全部 filter 一次性调完——而是**每个 filter 在它前面所有 filter 都 `Continue`、轮到它第一次"被数据推到"时才调**。这听起来是个小细节,但它解决了一个微妙的顺序问题:

设想链是 `ratelimit → tcp_proxy`,ratelimit 在 `onNewConnection` 里发异步限流请求、返回 `StopIteration`。这时 `tcp_proxy` 的 `onNewConnection` 还没被调——它被卡在 ratelimit 后面。等 ratelimit 的异步回调回来、`continueReading()` 复活链,iterator 推进到 tcp_proxy,这时才调 tcp_proxy 的 `onNewConnection`(它去建 upstream 连接)。

**这个顺序保证了:前置 filter 没放行,后置 filter 就不会开始干活**。如果一次性把所有 `onNewConnection` 都调了,tcp_proxy 在 ratelimit 还没回包时就去建 upstream 连接——浪费资源(可能马上被拒)、还可能产生竞态(限流拒绝时 upstream 已经建了一半)。懒触发让顺序严格起来。

[`initializeReadFilters`](../envoy/source/common/network/filter_manager_impl.cc#L41-L60) 是连接刚 accept 完时的入口——它跑一遍链、调每个 filter 的 `onNewConnection`,遇到第一个 `StopIteration` 就 break:

```cpp
bool FilterManagerImpl::initializeReadFilters() {
  if (upstream_filters_.empty()) return false;
  for (auto& entry : upstream_filters_) {
    if (entry->filter_ && !entry->initialized_) {
      entry->initialized_ = true;
      FilterStatus status = entry->filter_->onNewConnection();
      if (status == FilterStatus::StopIteration || connection_.state() != Connection::State::Open) {
        break;   // ← 第一个 StopIteration 就停
      }
    }
  }
  return true;
}
```

---

## 四、典型 network filter 三连:echo、direct_response、connection_limit

接口讲完,看几个最简单的真实 filter,感受"链上每个 filter 干一件事"。这三个是 Envoy 内置最简洁的 network filter,把它们读一遍,network filter 的写法就通了。

### echo:字面意义的回环

[`source/extensions/filters/network/echo/echo.cc`](../envoy/source/extensions/filters/network/echo/echo.cc) 全文 23 行,核心就一个方法:

```cpp
// source/extensions/filters/network/echo/echo.cc
Network::FilterStatus EchoFilter::onData(Buffer::Instance& data, bool end_stream) {
  ENVOY_CONN_LOG(trace, "echo: got {} bytes", read_callbacks_->connection(), data.length());
  read_callbacks_->connection().write(data, end_stream);   // ← 把进来的字节写回去
  ASSERT(0 == data.length());
  return Network::FilterStatus::StopIteration;             // ← 停链(后面没 filter 了)
}
```

echo filter 做的事:收到字节→原样写回 downstream→停链。它没实现 `onNewConnection`(默认空),也没实现 `onWrite`(注册时只用 `addReadFilter`,不进写链)。这是"终结型 filter"的样板——它就是这条链的终点,字节到这里被它消费掉,不再往后传。注册见 [echo/config.cc](../envoy/source/extensions/filters/network/echo/config.cc#L28):`filter_manager.addReadFilter(std::make_shared<EchoFilter>())`,名字 `envoy.filters.network.echo`。

### direct_response:回一段字节然后关连接

[`source/extensions/filters/network/direct_response/filter.cc`](../envoy/source/extensions/filters/network/direct_response/filter.cc) 也很短,核心在 `onNewConnection`:

```cpp
// source/extensions/filters/network/direct_response/filter.cc
Network::FilterStatus DirectResponseFilter::onNewConnection() {
  auto& connection = read_callbacks_->connection();
  if (!response_.empty()) {
    Buffer::OwnedImpl data(response_);
    connection.write(data, true);                          // 写一段固定字节,end_stream=true
  }
  connection.streamInfo().setResponseCodeDetails(
      StreamInfo::ResponseCodeDetails::get().DirectResponse);
  connection.close(Network::ConnectionCloseType::FlushWrite);  // ← 关连接
  return Network::FilterStatus::StopIteration;
}
```

direct_response 在连接**刚建起来**(`onNewConnection`)时就写一段配置好的字节、然后把连接关掉。这是"banner"型场景的样板——比如某些协议要求服务端先发一段欢迎语、或者直接拒绝连接(配空响应就是"建连即关")。它和 HTTP 层的 `direct_response`(P3-11)不同——HTTP 的那个是返回结构化 HTTP 响应,这个是字节流层的、协议无关的"回一段字节就关"。

### connection_limit:并发连接数限流(门控型 filter)

[`source/extensions/filters/network/connection_limit/connection_limit.cc`](../envoy/source/extensions/filters/network/connection_limit/connection_limit.cc) 演示了"门控型 filter"——它本身不消费字节,只决定后续 filter 能不能跑:

```cpp
// source/extensions/filters/network/connection_limit/connection_limit.cc  (简化)
Network::FilterStatus Filter::onNewConnection() {
  if (!config_->enabled()) return Network::FilterStatus::Continue;

  config_->stats().active_connections_.inc();

  if (!config_->incrementConnectionWithinLimit()) {
    // 超过上限,拒绝
    config_->stats().limited_connections_.inc();
    is_rejected_ = true;
    config_->incrementConnection();
    // ... 起个 timer 延迟关连接(防 DoS),或者直接关
    read_callbacks_->connection().close(Network::ConnectionCloseType::NoFlush,
                                        "over_connection_limit");
    return Network::FilterStatus::StopIteration;   // ← 拒绝,停链
  }
  return Network::FilterStatus::Continue;          // ← 没超,放行
}

Network::FilterStatus Filter::onData(Buffer::Instance&, bool) {
  if (is_rejected_) return Network::FilterStatus::StopIteration;  // 拒绝期间,数据也别过
  return Network::FilterStatus::Continue;
}
```

`incrementConnectionWithinLimit` 用 `compare_exchange_weak` 做原子自增([connection_limit.cc:26-38](../envoy/source/extensions/filters/network/connection_limit/connection_limit.cc#L26-L38))——因为多个 worker 各自 accept 连接,这计数得跨 worker 共享(用 `std::atomic`)。`connection_limit` 配在链的最前面,它 `Continue` 了后面的 filter(tcp_proxy / HCM)才会跑;它 `StopIteration` + 关连接,这条连接就到此为止。**这是"前置门控 filter"的样板**——它不碰字节内容,只控制连接能否继续。

> **钉死这件事**:这三个 filter(echo / direct_response / connection_limit)演示了 network filter 的三种典型形态——**终结型**(echo,字节到这就消费完)、**一次性响应型**(direct_response,建连就回一段然后关)、**门控型**(connection_limit,只决定后续 filter 能不能跑)。它们加上后面要讲的**转发型**(tcp_proxy)、**协议解码型**(HCM/redis_proxy/...)合起来,构成了 network filter 的全部角色谱。每个都极专注地干一件事,串起来就是完整的字节流治理。

---

## 五、tcp_proxy:不理解任何协议的纯字节透传

现在到本章的另一半主角——`tcp_proxy`。它是"纯 TCP 代理"的样板:**不理解任何协议,把 downstream 字节原样转发给 upstream cluster 选出的连接,upstream 回来的字节原样写回 downstream**。这是代理 Redis/MySQL/Kafka/各种私有协议时的默认选择。

### tcp_proxy 的类定义:它是个 ReadFilter

先看它的身份——[`source/common/tcp_proxy/tcp_proxy.h`](../envoy/source/common/tcp_proxy/tcp_proxy.h#L467-L470):

```cpp
// source/common/tcp_proxy/tcp_proxy.h
class Filter : public Network::ReadFilter,                                   // ← network filter
               public Upstream::LoadBalancerContextBase,                     // ← 给 LB 提供上下文
               protected Logger::Loggable<Logger::Id::filter>,
               public GenericConnectionPoolCallbacks {                       // ← 连接池回调
public:
  // Network::ReadFilter
  Network::FilterStatus onData(Buffer::Instance& data, bool end_stream) override;
  Network::FilterStatus onNewConnection() override;
  void initializeReadFilterCallbacks(Network::ReadFilterCallbacks& callbacks) override;
  // ...
};
```

`TcpProxy::Filter` 继承了三个角色:`Network::ReadFilter`(network filter 的本分)、`LoadBalancerContextBase`(给负载均衡器提供 downstream 连接信息、hash key 等)、`GenericConnectionPoolCallbacks`(连接池 ready/失败时的回调)。它**只实现 ReadFilter,不实现 WriteFilter**——因为 tcp_proxy 不需要在 downstream 写向拦截字节(它直接通过 `connection().write()` 写,这条 write 会触发 downstream 的 write 链,但 tcp_proxy 自己不在那条链上)。

注册方式和 echo、HCM 一模一样——[`source/extensions/filters/network/tcp_proxy/config.cc`](../envoy/source/extensions/filters/network/tcp_proxy/config.cc#L14-L31):

```cpp
// source/extensions/filters/network/tcp_proxy/config.cc
Network::FilterFactoryCb ConfigFactory::createFilterFactoryFromProtoTyped(...) {
  Envoy::TcpProxy::ConfigSharedPtr filter_config(std::make_shared<Envoy::TcpProxy::Config>(...));
  return [filter_config, &context](Network::FilterManager& filter_manager) -> void {
    filter_manager.addReadFilter(std::make_shared<Envoy::TcpProxy::Filter>(
        filter_config, context.serverFactoryContext().clusterManager()));
  };
}
LEGACY_REGISTER_FACTORY(ConfigFactory, Server::Configuration::NamedNetworkFilterConfigFactory,
                        "envoy.tcp_proxy");
```

`addReadFilter`、`REGISTER_FACTORY`、名字 `envoy.tcp_proxy`——和 HCM(`envoy.http_connection_manager`)、echo(`envoy.filters.network.echo`)完全对称。**tcp_proxy 在注册层面没有任何特权**,它就是一个 network filter。

### 一条字节流穿过 tcp_proxy 的完整旅程

把 tcp_proxy 的几个关键方法串起来,看一条 downstream 字节流是怎么"穿过去"被转给 upstream、upstream 字节又怎么回来的。这是 tcp_proxy 的核心时序:

```mermaid
sequenceDiagram
    participant DS as Downstream 连接(客户端)
    participant TP as TcpProxy::Filter(network filter)
    participant CM as ClusterManager(LB)
    participant CP as TcpConnPool
    participant US as Upstream 连接(后端)

    Note over DS,US: ① 连接建立阶段(onNewConnection)
    DS->>TP: onNewConnection()
    TP->>TP: pickRoute() 选 route → cluster_name
    TP->>TP: establishUpstreamConnection()
    TP->>CM: getThreadLocalCluster(name).connPool()
    CM->>CP: newStream(callbacks)  (异步建连/复用)
    TP-->>DS: return StopIteration  (链暂停,等 upstream)
    CP->>US: (复用已有连接 或 新建 TCP)
    US-->>CP: 连接就绪
    CP->>TP: onGenericPoolReady(upstream, host)
    TP->>TP: upstream_ = move(upstream); onUpstreamConnection()
    TP->>TP: read_callbacks_->continueReading()  (★ 复活读链)
    Note over TP: upstream_ 已就绪,后续 onData 能直接转发

    Note over DS,US: ② downstream → upstream 字节转发(onData)
    DS->>TP: onData(downstream 字节)
    TP->>US: upstream_->encodeData(data, end_stream)
    Note over US: TcpUpstream::encodeData = upstream_conn.write(data)
    TP-->>DS: return StopIteration (每次都停,数据已转走)

    Note over DS,US: ③ upstream → downstream 字节回写(onUpstreamData)
    US->>CP: upstream 连接收到字节
    CP->>TP: onUpstreamData(data, end_stream)
    TP->>DS: read_callbacks_->connection().write(data, end_stream)
    Note over DS: 字节写回客户端(经过 downstream 的 write filter 链)
```

下面把这五步逐个拆开。

#### 第 1 步:`onNewConnection`——选 route、建 upstream

[`onNewConnection`](../envoy/source/common/tcp_proxy/tcp_proxy.cc#L1142-L1216) 干两件事:设 timer(空闲超时、连接时长上限)、选 route、然后建 upstream 连接。

```cpp
// source/common/tcp_proxy/tcp_proxy.cc  (简化示意,非源码原文)
Network::FilterStatus Filter::onNewConnection() {
  // 1. 设各种 timer(空闲超时、连接时长上限、access log flush)
  idle_timeout_ = config_->idleTimeout();
  if (idle_timeout_) {
    idle_timer_ = read_callbacks_->connection().dispatcher().createTimer(...);
    resetIdleTimer();
  }
  // 2. 给这条连接生成一个 UUID(给 access log/tracing 用)
  getStreamInfo().setStreamIdProvider(...);

  // 3. 默认 IMMEDIATE 模式:立即选 route + 建 upstream
  if (connect_mode_ == UpstreamConnectMode::IMMEDIATE) {
    route_ = pickRoute();                    // ← 选 route(从配置的 weighted_clusters 里挑)
    return establishUpstreamConnection();    // ← 建 upstream(异步,见下)
  }
  // (ON_DOWNSTREAM_DATA / ON_DOWNSTREAM_TLS_HANDSHAKE 模式:延迟建连,略)
  return receive_before_connect_ ? Network::FilterStatus::Continue
                                 : Network::FilterStatus::StopIteration;
}
```

`pickRoute` 从 `Config` 里配的 route 表(简单的 `cluster: "xxx"` 或 `weighted_clusters` 加权)里选出 cluster 名字——**注意这和 HTTP 的 router(P3-11)完全是两套**:tcp_proxy 的 route 是连接级的(按源 IP、SNI 等 filter state 选 cluster),HTTP router 的 route 是请求级的(按 Host、Path、Header 匹配)。tcp_proxy 不走 HTTP router,它直接在 `Config` 里维护自己的 route 表([`Config::getRouteFromEntries`](../envoy/source/common/tcp_proxy/tcp_proxy.h#L326))。

#### 第 1.5 步:建连期间先 `readDisable(true)`——把 downstream 字节挡在门外

在进入 `establishUpstreamConnection` 之前,`tcp_proxy` 在 `initialize` 阶段([tcp_proxy.cc:489-493](../envoy/source/common/tcp_proxy/tcp_proxy.cc#L489-L493))做了一个不起眼但极关键的动作:

```cpp
// source/common/tcp_proxy/tcp_proxy.cc  (简化示意,非源码原文)
void Filter::initialize(Network::ReadFilterCallbacks& callbacks, bool set_connection_stats) {
  // ... 上面是设 connect_mode_、检查 receive_before_connect_

  if (!receive_before_connect_) {
    // ★ upstream 还没建好,先把 downstream 的读禁掉!
    // 不然 onData 可能在 upstream_ 还为 nullptr 时进来,没法转发。
    read_callbacks_->connection().readDisable(true);
  }
  // ...
}
```

`readDisable(true)` 的物理含义是:对这个 fd 的 epoll 注册改成"不关心可读"——内核的接收队列还是会收字节(三次握手照常),但 worker 的事件循环不会因为 fd 可读而醒来调 `onData`。这是流量控制(traffic shaping)的根:upstream 没建好前,先把 downstream 字节流"暂停"在内核缓冲区,不让它涌进 Envoy 的用户态 buffer。

> **不这样会怎样**:如果不 `readDisable`,会发生一件尴尬的事——`onNewConnection` 触发 `establishUpstreamConnection`(异步,要等),返回 `StopIteration`;可这期间 downstream 客户端继续发字节,epoll 会因为这些字节再次醒来、推进到 `tcp_proxy` 的 `onData`,而此时 `upstream_ == nullptr`(连接还没建好),`onData` 没法转发字节,只能尴尬地返回 `StopIteration` 把字节丢在连接级 buffer 里越积越多——既浪费内存,又破坏了"upstream 建好才开始消费 downstream 字节"的语义。`readDisable` 从源头堵住这个问题:upstream 没好,drydown 字节根本不进 `onData`。建好后,[`onUpstreamConnection`](../envoy/source/common/tcp_proxy/tcp_proxy.cc#L1397-L1404) 里 `readDisable(false)` 恢复读,字节才开始流。

#### 第 2 步:`establishUpstreamConnection`——找 cluster、要连接池

[`establishUpstreamConnection`](../envoy/source/common/tcp_proxy/tcp_proxy.cc#L653-L760) 是建 upstream 的核心,逻辑分三段:

```cpp
// source/common/tcp_proxy/tcp_proxy.cc  (简化示意,非源码原文)
Network::FilterStatus Filter::establishUpstreamConnection() {
  const std::string& cluster_name = route_ ? route_->clusterName() : EMPTY_STRING;

  // ① 在本 worker 的 thread-local cluster 缓存里找这个 cluster
  Upstream::ThreadLocalCluster* thread_local_cluster =
      cluster_manager_.getThreadLocalCluster(cluster_name);

  if (!thread_local_cluster) {
    // cluster 不存在 —— 看配没配 on-demand cluster discovery(OdCDS)
    auto odcds = config_->onDemandCds();
    if (!odcds.has_value()) {
      onInitFailure(UpstreamFailureReason::NoRoute);   // 直接失败
    } else {
      // 异步去发现这个 cluster(承 P5-19)
      cluster_discovery_handle_ = odcds->requestOnDemandClusterDiscovery(...);
    }
    return Network::FilterStatus::StopIteration;
  }

  // ② 检查 cluster 的 connection 资源限制(circuit breaker,P4-15)
  const auto& cluster = thread_local_cluster->info();
  if (!cluster->resourceManager(Default).connections().canCreate()) {
    onInitFailure(UpstreamFailureReason::ResourceLimitExceeded);
    return Network::FilterStatus::StopIteration;
  }

  // ③ 向连接池要一个连接(异步!连接池可能已有空闲连接复用,也可能新建)
  //    (代码在后续段,会调 generic_conn_pool_->newStream(this))
  // ...
  return Network::FilterStatus::StopIteration;   // 不管怎样,这一步都先停链等回调
}
```

这一步的关键是 **`cluster_manager_.getThreadLocalCluster(cluster_name)`**——每个 worker 都有自己的 thread-local cluster 视图(P1-02 讲过 thread-local 无锁),这一步是无锁查找。cluster 找到后,检查 circuit breaker(connections 资源配额,承 P4-15),没满才向连接池要连接。

**向连接池要连接**这一步的真身在 [`TcpConnPool`](../envoy/source/common/tcp_proxy/upstream.cc#L319-L346)。它先用 cluster 的 thread-local 视图拿到一个连接池对象([`thread_local_cluster.tcpConnPool(...)`](../envoy/source/common/tcp_proxy/upstream.cc#L325-L326)),然后调 [`newConnection(*this)`](../envoy/source/common/tcp_proxy/upstream.cc#L341) 要连接——这是个**异步**调用:连接池可能立刻给你一个空闲连接(复用),也可能没有空闲的、需要新建(等 TCP 三次握手),还可能排队等别人释放。无论哪种,`newConnection` 都立刻返回一个 `Cancellable*`(可以拿来取消),真正的"连接就绪"通过 [`onPoolReady`](../envoy/source/common/tcp_proxy/upstream.cc#L355-L376) 回调通知:

```cpp
// source/common/tcp_proxy/upstream.cc  (简化示意,非源码原文)
void TcpConnPool::onPoolReady(ConnectionDataPtr&& conn_data, HostConstSharedPtr host) {
  // 给 downstream stream_info 记上 upstream connection id(给 access log 用)
  downstream_info_.upstreamInfo()->setUpstreamConnectionId(conn_data->connection().id());
  // 把 conn_data 包成 TcpUpstream,回调给 tcp_proxy::Filter
  auto upstream = std::make_unique<TcpUpstream>(std::move(conn_data), upstream_callbacks_);
  callbacks_->onGenericPoolReady(std::move(upstream), host, ...);
}
```

`onPoolReady` → 造 `TcpUpstream`(包装 conn_data)→ `onGenericPoolReady` → tcp_proxy 把 upstream 存好 + `continueReading()` 复活读链。这条回调链是"upstream 连接就绪"从连接池传到 tcp_proxy 的完整路径。

> **钉死这件事**:tcp_proxy 建 upstream 是**全异步**的——`establishUpstreamConnection` 发完 `newConnection` 就返回 `StopIteration`,连接池内部决定复用/新建/排队,准备好后通过 `onPoolReady` → `onGenericPoolReady` 回调,回调里 `continueReading()` 复活读链。整条路径没有任何线程阻塞——worker 的事件循环可以同时处理几千个连接各自的建连,互不干扰。这是 P1-03 讲的"epoll 单线程事件循环 + 回调驱动"在 network filter 里的实战。

### UpstreamConnectMode:tcp_proxy 的三种建连时机

`tcp_proxy` 在 1.39 引入了 [`UpstreamConnectMode`](../envoy/source/common/tcp_proxy/tcp_proxy.h#L438-L439),三种模式控制"什么时候建 upstream":

| 模式 | 行为 | 适用场景 |
|------|------|----------|
| **IMMEDIATE**(默认) | `onNewConnection` 立刻建 upstream | 大多数场景:连接一进来就知道往哪转 |
| **ON_DOWNSTREAM_DATA** | 等到 downstream 发来第一段字节才建 | 需要先看到 payload 才能选 cluster(比如按协议 magic bytes 分流) |
| **ON_DOWNSTREAM_TLS_HANDSHAKE** | 等 downstream TLS 握手完成才建 | 需要基于 TLS 握手结果(SNI/ALPN/mTLS 身份)选 cluster |

后两种模式配合 [`receive_before_connect_`](../envoy/source/common/tcp_proxy/tcp_proxy.cc#L451-L469) 机制——`receive_before_connect_` 为 true 时,`onNewConnection` 不 `readDisable` downstream,允许字节在建连期间进来、暂存在 `early_data_buffer_` 里。等建连时机到了(`ON_DOWNSTREAM_DATA` 看到字节、`ON_DOWNSTREAM_TLS_HANDSHAKE` 握手完成),`onData` 里 [`establishUpstreamConnection()`](../envoy/source/common/tcp_proxy/tcp_proxy.cc#L1104-L1115) 才被触发,连建好后 [`onUpstreamConnection`](../envoy/source/common/tcp_proxy/tcp_proxy.cc#L1381-L1390) 把 `early_data_buffer_` flush 给 upstream。

这套机制解决了"建 upstream 之前要先观察 downstream"的场景——比如一个端口复用代理,得等客户端发几个字节、嗅探出是 HTTP 还是数据库协议,才决定往哪个 cluster 转。`early_data_buffer_` 在这个窗口期缓存字节,有 `max_buffered_bytes_` 上限([tcp_proxy.cc:1123-1130](../envoy/source/common/tcp_proxy/tcp_proxy.cc#L1123-L1130))防 OOM——超了就 `readDisable(true)` 暂停 downstream 读,等 upstream 好了再恢复。这是流量控制的另一面:**双向 readDisable(downstream/upstream)合起来就是 tcp_proxy 的背压机制**。

**注意 cluster 找不到时的两条路**:`onInitFailure(NoRoute)`(失败,关连接)或走 OdCDS(On-Demand Cluster Discovery Service)——后者是"按需发现 cluster":配置里 cluster 还没下发时,tcp_proxy 可以临时去控制面要一下这个 cluster 的定义(承 P5-19)。这是个新特性,解决了"cluster 配置太多、不想全量下发"的问题。

#### 第 3 步:`onGenericPoolReady`——upstream 连接建好的回调

连接池异步建好(或复用)连接后,回调 [`onGenericPoolReady`](../envoy/source/common/tcp_proxy/tcp_proxy.cc#L899-L937):

```cpp
// source/common/tcp_proxy/tcp_proxy.cc  (简化示意,非源码原文)
void Filter::onGenericPoolReady(StreamInfo::StreamInfo* info,
                                std::unique_ptr<GenericUpstream>&& upstream,
                                Upstream::HostDescriptionConstSharedPtr& host, ...) {
  // 把 upstream 句柄存起来(从此 onData 能往它写)
  upstream_ = std::move(upstream);
  generic_conn_pool_.reset();
  read_callbacks_->upstreamHost(host);    // ← 把选中的 host 写进 ReadFilterCallbacks
                                          //   (其他 filter 如 access log 能拿到)

  // 设置各种 upstream 信息(地址、SSL 信息等)
  // ...

  onUpstreamConnection();                 // ← 把建连前缓存的"早数据"flush 出去
  read_callbacks_->continueReading();     // ★★★ 复活读链!之前 StopIteration 卡住的链
}                                         //   现在从 tcp_proxy 的下一个 filter(通常没有)继续
```

最关键的两行:**`upstream_ = std::move(upstream)`**——从此 `onData` 来了能直接往 upstream 转发;**`read_callbacks_->continueReading()`**——把 `onNewConnection` 时停掉的读链重新点着。这就是第三节讲的"异步织进同步链"在 tcp_proxy 里的实战:建连阶段停链、连建好了复活。

[`onUpstreamConnection`](../envoy/source/common/tcp_proxy/tcp_proxy.cc#L1376-L1430) 顺手做几件事:把建连前缓存的"早数据"(`early_data_buffer_`,`receive_before_connect` 模式下建连前收到的字节)flush 给 upstream、恢复 downstream 的读(`readDisable(false)`,建连期间 downstream 读是被禁掉的,防止字节堆积)、重置 idle timer。

#### 第 4 步:`onData`——downstream 字节转发给 upstream

upstream 连接建好之后,后续每次 downstream 来字节,都进 [`onData`](../envoy/source/common/tcp_proxy/tcp_proxy.cc#L1078-L1140):

```cpp
// source/common/tcp_proxy/tcp_proxy.cc  (简化示意,非源码原文)
Network::FilterStatus Filter::onData(Buffer::Instance& data, bool end_stream) {
  getStreamInfo().getDownstreamBytesMeter()->addWireBytesReceived(data.length());

  if (upstream_) {
    // ★ upstream 已就绪:直接把 downstream 字节转给 upstream
    getStreamInfo().getUpstreamBytesMeter()->addWireBytesSent(data.length());
    upstream_->encodeData(data, end_stream);    // ← 见下面的 TcpUpstream::encodeData
    resetIdleTimer();
  } else if (receive_before_connect_) {
    // upstream 还没建好,但开了"早数据":先把字节缓存起来
    early_data_buffer_.move(data);
    // ... 缓存满了就 readDisable downstream 防爆内存
  }
  ASSERT(0 == data.length());   // 不管哪条路,data 都被消费完了
  return Network::FilterStatus::StopIteration;  // 停链(后面也没 filter 了)
}
```

`upstream_->encodeData(data, end_stream)` 的真身在 [`TcpUpstream::encodeData`](../envoy/source/common/tcp_proxy/upstream.cc#L88-L90):

```cpp
// source/common/tcp_proxy/upstream.cc
void TcpUpstream::encodeData(Buffer::Instance& data, bool end_stream) {
  upstream_conn_data_->connection().write(data, end_stream);   // ← 纯字节 write,不解析任何东西
}
```

**这就是"纯字节透传"的根**——`TcpUpstream::encodeData` 就是 `upstream_conn_data_->connection().write(data, end_stream)`,把 `Buffer::Instance` 原样写进 upstream 连接。它不解析 HTTP、不解析 Redis、不解析任何协议——字节是啥它就是啥,转发就完事了。这是 tcp_proxy 和 HCM 的根本分野:HCM 的 `onData` 是 [`codec_->dispatch(data)`](../envoy/source/common/http/conn_manager_impl.cc#L546)(把字节解码成 HTTP 帧再处理),tcp_proxy 的 `onData` 是裸字节 write。

#### 第 5 步:`onUpstreamData`——upstream 字节回写 downstream

upstream 连接收到字节(后端响应)时,通过连接池回调 [`onUpstreamData`](../envoy/source/common/tcp_proxy/tcp_proxy.cc#L1276-L1285):

```cpp
// source/common/tcp_proxy/tcp_proxy.cc
void Filter::onUpstreamData(Buffer::Instance& data, bool end_stream) {
  ENVOY_CONN_LOG(trace, "upstream connection received {} bytes, end_stream={}",
                 read_callbacks_->connection(), data.length(), end_stream);
  getStreamInfo().getUpstreamBytesMeter()->addWireBytesReceived(data.length());
  getStreamInfo().getDownstreamBytesMeter()->addWireBytesSent(data.length());
  read_callbacks_->connection().write(data, end_stream);   // ★ 写回 downstream
  ASSERT(0 == data.length());
  resetIdleTimer();
  maybeCloseDownstreamForDrainClose();
}
```

**就一行核心:`read_callbacks_->connection().write(data, end_stream)`**——把 upstream 回来的字节,通过 downstream 连接写回客户端。注意这个 `write` 会触发 downstream 连接的 write filter 链(`onWrite` 那条),如果链上配了 write filter(比如 proxy_protocol 加头、字节级限速),字节会先穿一遍写链再落到 socket。

### tcp_proxy 的"不理解协议"换来了什么

把上面五步合起来,tcp_proxy 的本质就清楚了:**它就是一条字节管道——downstream 字节进,原样吐给 upstream;upstream 字节回,原样吐回 downstream**。它不区分你发的是 HTTP、Redis、MySQL 还是私有协议——字节就是字节。

这个"不理解协议"换来三个东西:

1. **协议无关**:同一份 tcp_proxy 代码,能代理 Redis、MySQL、Kafka、Postgres、SSH、任何私有 TCP 协议。你不用为每个协议写一遍代理逻辑。
2. **零解析开销**:不解协议,就没有 codec 解析的 CPU 开销,也没有解析 bug 的正确性风险。它只是 `write`,极快。
3. **透明**:客户端看到的协议,和后端看到的协议,完全一致——tcp_proxy 不改一个字节(除了可选的 proxy_protocol 加头)。这对那些 Envoy 不理解的协议尤其重要:你不用担心 tcp_proxy 把你的协议"解错了"。

代价也明显:**它做不了任何协议级治理**。它不能基于"这是 GET /api/users 请求"做路由(它根本不知道这是 HTTP),不能基于"这是 Redis 的 GET 命令"做读写分离,不能统计 QPS(它只看字节数)。**协议级治理需要协议级代理**——这就是为什么 Envoy 还有 HCM(理解 HTTP)、redis_proxy(理解 Redis)、mysql_proxy(理解 MySQL)这些专门的协议代理 network filter。

> **钉死这件事**:tcp_proxy 是"纯字节管道"——`onData` 把 downstream 字节 `encodeData` 给 upstream(本质是 `upstream_conn.write`),`onUpstreamData` 把 upstream 字节 `connection().write` 回 downstream。不理解协议换来协议无关 + 零解析开销 + 透明,代价是做不了协议级治理。代理 Envoy 不理解的协议(或不想花 CPU 解析的协议),用 tcp_proxy;要做协议级治理,用对应的协议代理 filter(HCM/redis_proxy/...)。

### 选 cluster 用的是 thread-local 视图(承接 P1-02、P4-12)

最后补一个和后续章节的衔接点。tcp_proxy 选 cluster 用的是 `cluster_manager_.getThreadLocalCluster(name)`——这是 thread-local 的 cluster 视图。回忆 P1-02:每个 worker 都有自己的 thread-local 副本,EDS 推新 endpoint 时,MainThread 算好后把结果通过 RCU 机制同步到每个 worker 的 thread-local slot,worker 读 cluster/host 列表全程无锁。

这意味着 tcp_proxy 在 `establishUpstreamConnection` 里查 cluster、选 host 的整条路径**没有锁**——这是 Envoy 能扛高并发的根之一(P1-02、P4-12/13 会拆透 cluster 和 LB 的细节)。本章只点到:tcp_proxy 选 cluster 是 thread-local 无锁查找,具体 cluster 类型(static/dns/eds)、LB 策略(round_robin/least_request/ring_hash/...)、连接池复用,是第 4 篇的事。

### 双向背压:tcp_proxy 怎么防止 buffer 爆内存

tcp_proxy 透传字节时,有一个绕不开的问题:**downstream 和 upstream 的速度可能不匹配**。downstream 发得快、upstream 处理慢(比如后端在做重计算),或者反过来(upstream 回包大、downstream 客户端读得慢)——不管哪种,慢的那一端会让快端发来的字节堆在 Envoy 的 buffer 里,越积越多,最后 OOM。

tcp_proxy 的解法是**双向 readDisable 背压**:两端都有 buffer,buffer 有高/低水位(high/low watermark,P1-04 讲过 Buffer 的 watermark)。一端的 buffer 超过高水位,就 `readDisable(true)` 暂停对端读(让对端别再发);buffer 排到低水位以下,再 `readDisable(false)` 恢复。这套由 [`DownstreamCallbacks::onAboveWriteBufferHighWatermark`](../envoy/source/common/tcp_proxy/tcp_proxy.h#L661-L671) 和 [`UpstreamCallbacks::onAboveWriteBufferHighWatermark`](../envoy/source/common/tcp_proxy/tcp_proxy.h#L521-L545) 协同驱动:

```
   downstream 发太快 → upstream buffer 涨过 high watermark
     ↓
     UpstreamCallbacks::onAboveWriteBufferHighWatermark()
       → readDisableDownstream(true)   ← 暂停 downstream 读,让它别再发
     ↓
     (upstream 慢慢消化,buffer 排到 low watermark 以下)
     ↓
     UpstreamCallbacks::onBelowWriteBufferLowWatermark()
       → readDisableDownstream(false)  ← 恢复 downstream 读

   反方向(upstream 回太快、downstream 读太慢)对称:
     DownstreamCallbacks::onAboveWriteBufferHighWatermark
       → readDisableUpstream(true)     ← 暂停 upstream 读
     ... readDisableUpstream(false)
```

这套背压是 **TCP 流控 + Envoy 用户态流控** 两层合起来:downstream 被 `readDisable` 后,Envoy 不再从 socket 读,内核接收 buffer 满了,TCP 协议栈自动向客户端发 zero window,客户端的 send 就被卡住——最终客户端感知到"对面慢了,我得等"。这是端到端的流控,从 application 层的 filter 一路传导到 TCP 层。**没有这套背压,一个慢后端能让 Envoy 被一个快客户端打爆内存;有了它,慢的那一端天然反压快的那一端,buffer 永远在水位之间**。

> **钉死这件事**:network filter 链不只有"过滤字节"的职责,还背着"流控"的职责——每个 filter 都可以 `readDisable`,任何一层发现"我下游消化不过来",都能反压到上游。tcp_proxy 作为字节管道,它的双向 readDisable + watermark 背压是防 OOM 的根。这套机制在 HTTP filter 链里同样存在(P3-10 的 `onDecoderFilterAboveWriteBufferHighWatermark`),底层都是 P1-04 的 Buffer watermark + connection 的 readDisable。

### Drainer:downstream 关了、upstream 还要 flush 怎么办

最后一个值得讲的细节。tcp_proxy 的 [`Drainer`](../envoy/source/common/tcp_proxy/tcp_proxy.h#L778-L803) 解决一个边界场景:**downstream 连接关了,但 upstream 还有字节没 flush 完**。

正常情况:downstream 和 upstream 双向透传,任一端关连接,tcp_proxy 把另一端也关掉,filter 析构。可如果 downstream 客户端发了 FIN(半关闭)或 RST,但这时 upstream 这边刚收到一批响应、还没写回 downstream(或者 downstream 那边的 socket buffer 还在 flush)——直接关 upstream 会丢字节。

Drainer 接管这种情况。tcp_proxy 在 [`onDownstreamEvent`](../envoy/source/common/tcp_proxy/tcp_proxy.cc#L1250-L1259) 里,如果 downstream 关了但 upstream 还有数据要 flush,把 upstream 连接和 callbacks 移交给 `UpstreamDrainManager`,后者把这条连接包成 `Drainer` 对象放进 deferred-delete 队列——Drainer 继续监听 upstream 字节、刷到 downstream(虽然 downstream 已关,但 socket buffer 可能还能 flush),或者等 idle timeout 到了强制收尾。**这是"优雅退出"的精细处理**:不丢在途字节、不让连接泄漏、不让 worker 事件循环被卡。

> **钉死这件事**:tcp_proxy 看似简单(就是个字节管道),但生产级代理的边界场景——背压、早数据、downstream 关了 upstream 还没 flush——每一个都得精心处理。`readDisable` 背压防 OOM、`early_data_buffer` 防建连期字节丢失、`Drainer` 防在途字节丢失。这些"看不见的细节"是 tcp_proxy 能用在生产代理 Redis/MySQL/Kafka 这些重协议的根——它们对字节丢失零容忍。

---

## 六、技巧精解(一):HCM 是 network filter 的特例——这条统一性的代价与收益

这是本章最值得单独钉死的设计决策。它也是 P3-08 的引子(那里拆 HCM 内部),本章只拆"为什么 HCM 要做成 network filter"这个统一性本身。

### 源码事实:HCM 真的是个 network filter

先看证据。HCM 的主类 `ConnectionManagerImpl`,直接继承 `Network::ReadFilter`(见 [conn_manager_impl.h](../envoy/source/common/http/conn_manager_impl.h))——和 tcp_proxy 的 `TcpProxy::Filter` 一模一样:

```cpp
// source/common/http/conn_manager_impl.h  (简化示意,非源码原文)
class ConnectionManagerImpl : Logger::Loggable<Logger::Id::http>,
                             public Network::ReadFilter,        // ← network filter!
                             public ServerConnectionCallbacks,  // codec 回调
                             public Network::ConnectionCallbacks,
                             public Http::ApiListener {
public:
  // Network::ReadFilter
  Network::FilterStatus onData(Buffer::Instance& data, bool end_stream) override;
  Network::FilterStatus onNewConnection() override;
  void initializeReadFilterCallbacks(Network::ReadFilterCallbacks& callbacks) override;
};
```

它的 `onData` 真身在 [conn_manager_impl.cc:515](../envoy/source/common/http/conn_manager_impl.cc#L515):

```cpp
// source/common/http/conn_manager_impl.cc  (简化示意,非源码原文)
Network::FilterStatus ConnectionManagerImpl::onData(Buffer::Instance& data, bool) {
  if (!codec_) {
    createCodec(data);    // ← 第一段字节来时,造 codec(按 H1/H2/H3 选实现)
  }
  // do-while:解出一帧就驱动一次 http filter 链,解完为止
  do {
    const Status status = codec_->dispatch(data);   // ★ 把字节解码成 HTTP 帧
    // ... 解出来的 headers/data/trailers 喂给 http filter 链
  } while (redispatch);
  return Network::FilterStatus::StopIteration;
}
```

HCM 的 `onData` 干的事和 tcp_proxy 截然不同——它 `codec_->dispatch(data)`,把裸字节**解码成结构化的 HTTP 帧**(RequestHeaderMap、body data、trailers),然后把解出来的结构化对象喂给 http filter 链(P3-10)。tcp_proxy 的 `onData` 是裸字节 `write`,HCM 的 `onData` 是字节解码成 HTTP 再驱动 http 链——但**对外,它们都是 `Network::ReadFilter::onData`**,被同一条 network filter 链按同一套机制调用。

HCM 注册进 listener 也用 `addReadFilter`,见 [HCM 配置工厂](../envoy/source/extensions/filters/network/http_connection_manager/config.cc#L330):

```cpp
// source/extensions/filters/network/http_connection_manager/config.cc  (简化)
return [...](Network::FilterManager& filter_manager) -> void {
  auto hcm = std::make_shared<Http::ConnectionManagerImpl>(...);
  filter_manager.addReadFilter(std::move(hcm));   // ★ 和 tcp_proxy 完全一样的注册
};
```

工厂注册名是 `envoy.http_connection_manager`(见 [HCM 工厂 LEGACY_REGISTER_FACTORY](../envoy/source/extensions/filters/network/http_connection_manager/config.cc#L346-L348))——和 `envoy.tcp_proxy` 完全对称。**HCM 在注册层面没有任何特权**。

### 朴素的反方案:"HTTP 和 TCP 各走一套独立机制"

如果不把 HCM 做成 network filter,会怎么设计?最朴素的反方案是:Nginx 那样,HTTP 和 TCP(stream)各走一套独立的处理框架——

```
   Nginx 的设计(对照):
   
   http {}   ← HTTP 处理框架(自己的 connection 管理、自己的 filter 链)
   stream {} ← TCP/UDP 处理框架(另一套 connection 管理、另一套 filter)
   
   两套框架相对独立,配置里平级,代码里也是两套机制。
```

Envoy 如果照搬,就是"HTTP listener"和"TCP listener"两种 listener,各自一套 connection 管理、各自的 filter 机制。**这套会撞三道墙**:

**墙一:前置字节处理无法组合**。假设你想在 HTTP 解码**之前**先做 proxy_protocol 解析(还原真实客户端 IP),或者先做字节级 ratelimit(在不知道是 HTTP 之前就限速),或者先做 mTLS 身份提取(基于证书做鉴权,这要在 HTTP 解码前)。如果 HTTP 是独立框架,这些前置逻辑要么硬塞进 HTTP 框架内部(职责错乱——HTTP 框架凭什么管 proxy_protocol?),要么另造一套机制(两套心智)。把 HCM 做成 network filter,前置逻辑就是 network filter 链上 HCM 之前的几个 filter——`ratelimit`、`ext_authz`、`rbac` 各自独立,自然组合在 HCM 前面。

**墙二:connection 管理无法统一**。Envoy 的 connection 管理是一套精细的机制——drain(优雅下线,P5-18)、idle timeout、watermark backpressure(缓冲区高水位暂停读,P1-04)、hot restart 时的连接交接(P6-21)、连接级 stats。如果 HTTP 和 TCP 各一套,这些全得重造两遍,行为还可能不一致(HTTP 连接 drain 的语义和 TCP 连接 drain 的语义对不上)。把 HCM 做成 network filter,所有 connection 管理逻辑在 network filter 这一层统一——HCM 和 tcp_proxy 共享同一套 drain、timeout、backpressure、stats。

**墙三:配置和组合的对称性丢失**。Envoy 的 listener 配置里,`filter_chains` 是一个数组,每条 chain 是一串 network filter。你想 HTTP 代理?chain 里配 HCM。你想 TCP 透传?chain 里配 tcp_proxy。你想根据 SNI 分流,有的走 HTTP 有的走 TCP 透传?配两条 filter_chain,匹配规则按 SNI,各自配 HCM 或 tcp_proxy。**这套机制对称、统一、可组合**。如果 HTTP 独立,你就得有"HTTP listener"和"TCP listener"两种 listener,跨协议分流(同一端口按 SNI 分 HTTP 和 TCP)极其别扭。

> **不这样会怎样**:一个具体场景——你有个 listener 监听 443,先做 TLS 终止(listener filter),然后想按 SNI 分流:`api.example.com` 走 HTTP(进 HCM → router 转后端),`db.example.com` 是数据库流量走 TCP 透传(进 tcp_proxy → 转数据库集群)。如果 HCM 是独立模块,你得为这两种情况写两套 listener、两套连接管理。把 HCM 做成 network filter,同一个 listener 两条 filter_chain 分支,一条配 `ratelimit → HCM`,一条配 `tcp_proxy`,统一在 listener/network filter 框架里——配置对称、行为一致、可组合。

### 所以这样设计:HCM 是"跨界 filter"

把 HCM 做成 network filter 的精髓,在于它是一个**"跨界 filter"**——

```
   HCM 在 network filter 链中的位置(一条 TCP 连接的字节流链)
   ┌──────────────────────────────────────────────────────────────┐
   │ 字节流世界(network filter,Buffer 传递)                       │
   │   ┌──────────┐   ┌──────────┐   ┌──────────────────────────┐ │
   │   │ratelimit │   │ ext_authz│   │ HCM(HTTP 入口)           │ │
   │   │(字节限流)│   │(连接鉴权)│   │  onData(data) {          │ │
   │   └──────────┘   └──────────┘   │    codec_->dispatch(data)│ │
   │                                  │      ↓ 解码              │ │
   │                                  │    RequestHeaderMap 等   │ │
   │                                  │      ↓                   │ │
   │                                  │    驱动 http filter 链   │ │ ◀── 跨界点
   │                                  │  }                       │ │
   │                                  └──────────────────────────┘ │
   └──────────────────────────────────────────────────────────────┘
                                                       │
                                                       ▼
                                          ┌──────────────────────────┐
                                          │ HTTP 世界(结构化请求/响应)│
                                          │ http filter 链(P3-10)    │
                                          └──────────────────────────┘
```

对外,HCM 是个 network filter——它和前置的 ratelimit/ext_authz 用同一套 `addReadFilter` 注册、同一套 `FilterStatus`/`continueReading` 推进、共享同一条 `Connection` 的所有管理逻辑(drain/timeout/stats)。对内,HCM 是 HTTP 引擎——它的 `onData` 把字节解码成 HTTP,为每条请求建一个 `ActiveStream`(P3-08),驱动一条独立的 http filter 链(decoder/encoder 两向,P3-10)。

这条统一性的收益,精炼成一句话:**Envoy 的 connection 管理在 network filter 这层统一,HTTP 只是这层上一个"碰巧理解 HTTP"的 filter**。和 redis_proxy 是"碰巧理解 Redis"的 filter、mysql_proxy 是"碰巧理解 MySQL"的 filter,本质上是同一回事——都是"协议解码型 network filter"。这条统一性让前置字节处理(ratelimit/ext_authz/rbac)能和 HTTP 处理自然组合、connection 管理一套机制管到底、跨协议分流配置对称。

### HCM 的 onNewConnection:一个有意思的细节

HCM 的 [`onNewConnection`](../envoy/source/common/http/conn_manager_impl.cc#L584-L596) 有个值得一讲的细节——它对绝大多数连接(TCP)返回 `Continue`,只对 QUIC 返回 `StopIteration`:

```cpp
// source/common/http/conn_manager_impl.cc
Network::FilterStatus ConnectionManagerImpl::onNewConnection() {
  if (!read_callbacks_->connection().streamInfo().protocol()) {
    // 非 QUIC:让字节继续往后流(onData 会处理)
    return Network::FilterStatus::Continue;
  }
  // 只有 QUIC 连接的 stream_info_ 才预设了 protocol
  Buffer::OwnedImpl dummy;
  createCodec(dummy);
  ASSERT(codec_->protocol() == Protocol::Http3);
  // QUIC 连接不走 onData 接口(QUICHE 自己做多路复用解复用)
  return Network::FilterStatus::StopIteration;
}
```

为什么?因为 HTTP/3 基于 QUIC(UDP),QUIC 的连接和 stream 多路复用由 QUICHE 库自己处理,不走 Envoy 的 `onData` 字节流接口。所以 HCM 对 QUIC 在 `onNewConnection` 就造好 codec、停链;对 HTTP/1.1 和 HTTP/2(TCP 承载),HCM 在 `onNewConnection` 放行,等字节来了在 `onData` 里造 codec + 解码。这个分支是 HTTP/3 引入后的妥协——P3-09 会拆透 HTTP/3 和 QUIC 的特殊路径。

> **钉死这件事**:HCM 是"跨界 filter"——对外是 network filter(享受 network filter 的注册、链推进、connection 管理全套),对内是 HTTP 引擎(字节解码成 HTTP、驱动 http filter 链)。这条统一性让 HTTP 处理和纯 TCP 代理、各种协议代理(redis/mysql/...)、连接级治理(ratelimit/ext_authz/rbac)全都在 network filter 这层统一——是 Envoy"通用数据面"定位的根。HCM 内部怎么解码、怎么驱动 http 链,是 P3-08 的事,本章只钉死"它是 network filter 特例"这条统一性。

---

## 七、技巧精解(二):读链 FIFO + 写链 LIFO——字节流版洋葱模型的物理基础

第二个值得单独钉死的技巧,是第二节点到的"读链 `moveIntoListBack`、写链 `moveIntoList`"那条插入方向差异。这看似是个小实现细节,实际是字节流版"洋葱模型"能成立的物理基础,搞不清它,filter 链的两向语义就总是糊的。

### 朴素的反方案:读链和写链都 FIFO

如果读链和写链都用 `moveIntoListBack`(都 FIFO),会发生什么?看一个具体例子。假设配置顺序是 `rbac → ext_authz → HCM`(rbac 最先注册):

```
   配置顺序:rbac → ext_authz → HCM  (addReadFilter/addWriteFilter 的顺序)
   
   方案 A(朴素):读链写链都 FIFO
   
     读链(FIFO):[rbac] → [ext_authz] → [HCM]
     写链(FIFO):[rbac] → [ext_authz] → [HCM]
     
     请求字节进来:onWrite 顺序 rbac → ext_authz → HCM
     响应字节出去:onWrite 顺序 rbac → ext_authz → HCM  ← rbac 先看到响应!
```

这有什么问题?rbac 是"基于连接四元组做访问控制"的 filter——它的语义是"连接一进来就审一次、决定放不放行"。它**只关心进来的请求字节,响应字节它根本不感兴趣**。可方案 A 里,rbac 的 `onWrite` 在响应路径上会先于 HCM 被调用——rbac 被迫参与响应处理,哪怕它的 `onWrite` 啥也不干就 `return Continue`,这也是不必要的调用开销,而且语义上很别扭(为什么 rbac 要先于 HCM 看到响应?)。

更糟的是 ext_authz——它是"外部鉴权"filter,典型的"只关心请求方向"。它的 `onWrite` 如果不小心改了字节(比如它想加个 response header),那它会在 HCM 还没构造好响应之前就先看到字节、动手脚——时序完全错乱。

### 所以这样设计:读链尾插、写链头插,天然逆序

Envoy 的方案是**读链 `moveIntoListBack`(尾插,FIFO)、写链 `moveIntoList`(头插,LIFO)**——同一个双向 filter(用 `addFilter` 注册)在两条链里的相对位置天然逆序:

```
   方案 B(Envoy 实际):读链 moveIntoListBack、写链 moveIntoList
   
   注意:每个 filter 注册时,addReadFilter 把它插到读链尾,addWriteFilter 把它插到写链头。
        所以同一个 filter 在两条链里出现两次(读链一份、写链一份),但相对顺序相反。
   
     注册顺序:rbac → ext_authz → HCM
     
     读链(FIFO, 尾插):[rbac] → [ext_authz] → [HCM]
       onData 顺序:rbac → ext_authz → HCM  (请求方向:rbac 最外层,先看到请求)
     
     写链(LIFO, 头插):[HCM] → [ext_authz] → [rbac]
       onWrite 顺序:HCM → ext_authz → rbac  (响应方向:rbac 最外层,最后看到响应)
     
     ── 合起来:rbac 是最外层"洋葱皮"
        请求:rbac 先看(决定放不放行)
        响应:rbac 最后看(如果它真要在响应上做事,在所有内层 filter 都处理完之后)
```

这正是"洋葱模型"的物理实现:**最先注册的 filter 是最外层,进出字节都包着它**;最后注册的 filter(HCM)是最内层。请求从外到内穿(rbac → ... → HCM),响应从内到外穿(HCM → ... → rbac),天然逆序——和 HTTP filter 的 decoder/encoder 两向(P3-10)语义完全同构,只不过 HTTP filter 的两向是显式分两条链(decoder_filters / encoder_filters),network filter 的两向是隐式靠插入方向造出来的。

### 为什么"先注册 = 最外层"是正确的语义

这套语义和几乎所有现代中间件框架(各种语言的"中间件"概念)吻合,不是巧合——它符合直觉:**最外层的 filter 应该最先看到进来的流量、最后看到出去的流量**。

- **rbac**(访问控制):最外层——任何流量进来先过它这道关,过不了直接拒,根本不让流量进到内层。响应出去它最后看一眼(如果它要记录或改 response)。
- **ext_authz**(外部鉴权):次外层——rbac 放行后,它做更细粒度的鉴权(调外部服务)。
- **ratelimit**(限流):也是外层——流量进来先过限流。
- **HCM / tcp_proxy**:最内层——前面所有 gatekeeper 都放行了,它才干活(解码 HTTP 或转发 TCP)。

这个"从外到内、按 gatekeeper 严格程度排序"的模型,让 filter 链的配置顺序天然就是治理优先级——你把最严格的(rbac)、最该早做的(ratelimit)放前面,最重的(HCM)放最后。读链 FIFO + 写链 LIFO 物理上保证了这套顺序。

> **不这样会怎样**:如果两条链都 FIFO,filter 的"层次感"就没了——所有 filter 在请求方向和响应方向都是同一个顺序,"洋葱"退化成"直筒",响应方向的 filter 顺序和请求方向一样,语义错乱(响应处理 filter 跑在它不该跑的位置)。Envoy 用读链尾插 + 写链头插,一行代码的差异(`moveIntoListBack` vs `moveIntoList`),就把两向逆序的洋葱模型物理实现了——这是 network filter 链两向语义的根,和 HTTP filter 显式分两条链(decoder/encoder)殊途同归。

### 验证:tcp_proxy 的 write 不走自己的 onWrite

一个容易混淆的点:tcp_proxy 只实现 `ReadFilter`(不实现 `WriteFilter`),所以它不在 downstream 连接的写链上。当 `onUpstreamData` 调 `read_callbacks_->connection().write(data, end_stream)` 时,这批字节会穿 downstream 连接的写链——但那条写链上没有 tcp_proxy 自己,只有其他配了 write filter 的家伙(比如配了 `proxy_protocol` filter 加头)。tcp_proxy 是"读向终结、写向不参与"的 filter,这是它的设计选择(它不需要改 downstream 出去的字节,upstream 来啥它就写啥)。

如果一个 filter 真的要在写向拦字节(比如字节级限速 `tcp_bandwidth_limit`,要在写回 downstream 时限速),它实现 `Filter`(同时读 + 写)、用 `addFilter` 注册——那它就同时出现在读链和写链上,请求方向 `onData` 拦、响应方向 `onWrite` 拦,两个方向都管。这正是字节级限速、proxy_protocol 加头这类 filter 的做法。

---

## 八、架构演进:generic_proxy 与 dynamic_modules——network filter 的新边界

Envoy 在持续演进,network filter 这层也有几个新特性值得点一下。

### generic_proxy:协议无关代理框架

传统的 network filter 模式是"每个协议一个 filter"——redis_proxy、mysql_proxy、mongo_proxy 各写一份,它们内部都要做"解析协议 → 驱动 filter 链 → 路由 → 转发"这套相似的事。代码重复多,加新协议成本高。

[`generic_proxy`](../envoy/source/extensions/filters/network/generic_proxy/) 是较新的特性(见 [工厂注册](../envoy/source/extensions/filters/network/generic_proxy/config.cc#L155-L159)),它把这套抽象成一个**协议无关的代理框架**:你只需要写一个 codec(把你的私有协议解成 generic 的 request/response 抽象),generic_proxy 框架就自动给你提供"协议 filter 链 + 路由 + 转发"。内置已经支持 Dubbo codec、HTTP1 codec(见 `generic_proxy/codecs/` 目录)。

generic_proxy 的 `Filter` 也是个 [`Network::ReadFilter`](../envoy/source/extensions/filters/network/generic_proxy/proxy.cc#L665),它 `onData` 里调 codec 解码、然后驱动自己内部的"协议 filter 链"(注意:这又是一条 filter 链,和 HCM 的 http filter 链是同构设计——network filter 里有协议 filter 链,协议 filter 链里有 router)。这进一步印证了"network filter 是字节流层、协议代理在它之上分层"的设计统一性:HCM 是"HTTP 协议代理 network filter",generic_proxy 是"任意协议代理 network filter 框架",redis_proxy 是"Redis 协议代理 network filter"——三个层次,同一套 network filter 基座。

### dynamic_modules:动态 C++ 模块

另一个新特性是 [`dynamic_modules`](../envoy/source/extensions/filters/network/dynamic_modules/),它允许运行时加载动态编译的 C++ 模块作为 network filter(不用重新编译 Envoy 二进制)。这是 Wasm(P6-22)之外的第二种运行时扩展方式——Wasm 是沙箱字节码(安全但慢),dynamic_modules 是原生 C++(快但信任模型不同)。两者对照在 P6-22 拆透,这里只点出"network filter 这层支持运行时扩展,不被内置 filter 限死"。

### 各种 proxy filter 的成熟度

顺便诚实交代:Envoy 内置的各种协议 proxy filter 成熟度不一。redis_proxy、mongo_proxy、thrift_proxy、dubbo_proxy 比较成熟,生产用得多;postgres_proxy、mysql_proxy、kafka_broker 功能相对有限(有些只做协议嗅探和基础统计,不做完整代理);generic_proxy 是较新的统一框架,还在演进。涉及具体协议代理时,以官方 docs 和源码为准,不要假设所有 proxy filter 都和 tcp_proxy 一样能做完整透传。

---

## 九、章末小结

### 回扣主线

本章服务**数据面**——具体是数据面 downstream 的字节流加工层。回到全书主线"一条流量穿过一串 filter",本章拆的是 listener filter 跑完之后、HTTP 解码(或纯 TCP 转发)之前的那一段:**network filter 链**。它和 listener filter(P2-06,连接握手前)的边界在于 `Connection` 对象是否建起来;和 HTTP filter 链(P3-10,HTTP 解码后)的边界在于是否已经把字节解成结构化协议。

本章立了四个东西:

1. **network filter 链是"协议无关的字节流层"**——它看裸字节,不解析任何协议。这层存在的根:微服务流量不只有 HTTP,数据库/消息队列/私有协议都要代理;即便只代理 HTTP,字节流层也提供了连接级治理(HTTP filter 做不到的)。
2. **ReadFilter/WriteFilter 两条方向相反的链**——读向 FIFO、写向 LIFO,天然形成字节流版"洋葱模型"。filter 能同时管读向(`onData`)和写向(`onWrite`),两向逆序。
3. **`FilterStatus::StopIteration` + `continueReading()`**——filter 想等异步结果(限流回包、upstream 建连)就停链,事后复活。这是 network filter 链承载异步治理的根本机制。
4. **HCM 是 network filter 的特例 + tcp_proxy 是纯字节透传**——两者用同一套 `addReadFilter` 注册、同一套 `FilterStatus`/`continueReading` 推进。这条统一性让 HTTP 处理、纯 TCP 代理、各种协议代理、连接级治理全都在 network filter 这层统一,是 Envoy"通用数据面"定位的根。

### 五个为什么

1. **为什么 network filter 链要做成"协议无关的字节流层"?**——微服务流量不只有 HTTP(数据库、消息队列、私有协议都要代理),需要一层"看裸字节、不绑定协议"的 filter 层;即便代理 HTTP,字节流层也提供了连接级治理(HTTP filter 做不到的,如 connection_limit、字节级限速、连接级 ext_authz)。
2. **为什么 network filter 有读向(`onData`)和写向(`onWrite`)两条链?**——和 HTTP filter 的 decoder/encoder 两向同构:有些 filter 只关心进来的字节(ratelimit),有些只关心出去的字节(压缩、加头),有些两个方向都管(字节级限速)。读链 FIFO + 写链 LIFO,天然形成"洋葱模型"(请求外到内、响应内到外逆序)。
3. **为什么 filter 链需要 `StopIteration` + `continueReading()`?**——异步操作(外部限流、外部鉴权、upstream 建连)不能阻塞 worker 单线程 epoll。filter 返回 `StopIteration` 把链挂起(不占线程),事后异步事件回来调 `continueReading()` 把链重新点着。这是 network filter 链承载异步治理的根本机制。
4. **为什么 tcp_proxy "不理解任何协议"?**——不理解协议换来协议无关(同一份代码代理 Redis/MySQL/Kafka/任何私有协议)、零解析开销(不解协议,只 `write`)、透明(不改一个字节)。代价是做不了协议级路由/读写分离/QPS 统计——要做这些,用对应的协议代理 filter(HCM/redis_proxy/...)。
5. **为什么 HCM 是一个 network filter,而不是独立模块?**——把 HCM 做成 network filter,前置字节处理(ratelimit/ext_authz/rbac)能自然组合在 HCM 前面、connection 管理(drain/timeout/stats/backpressure)一套机制管到底、跨协议分流(HTTP + TCP 透传在同一 listener)配置对称。朴素地"HTTP 和 TCP 各走一套独立机制"会撞组合性丢失、connection 管理分叉、配置不对称三道墙。这条统一性是 Envoy"通用数据面"的根。

### 想继续深入往哪钻

- **想看 network filter 接口全集**:读 [`envoy/network/filter.h`](../envoy/envoy/network/filter.h)——`ReadFilter`/`WriteFilter`/`Filter`/`FilterManager`/`ReadFilterCallbacks`/`WriteFilterCallbacks` 全在这一个文件,注释详尽。
- **想看链推进的运行机制**:读 [`source/common/network/filter_manager_impl.cc`](../envoy/source/common/network/filter_manager_impl.cc)——`onContinueReading`(读链 FIFO 迭代)、`onWrite`(写链 LIFO 迭代)、`initializeReadFilters`(onNewConnection 懒触发)、`addReadFilter`/`addWriteFilter`(插入方向差异)。
- **想看 tcp_proxy 完整实现**:读 [`source/common/tcp_proxy/tcp_proxy.cc`](../envoy/source/common/tcp_proxy/tcp_proxy.cc)(主 filter)+ [`upstream.cc`](../envoy/source/common/tcp_proxy/upstream.cc)(连接池 + `TcpUpstream::encodeData`)+ [`config.cc`](../envoy/source/extensions/filters/network/tcp_proxy/config.cc)(工厂注册)。重点方法:`onNewConnection`(L1142)、`establishUpstreamConnection`(L653)、`onGenericPoolReady`(L899)、`onData`(L1078)、`onUpstreamData`(L1276)、`onUpstreamConnection`(L1376)。
- **想看各种 network filter 的写法**:读 [`source/extensions/filters/network/`](../envoy/source/extensions/filters/network/) 下各子目录——echo(最简)、direct_response(一次性响应)、connection_limit(门控)、ratelimit(异步 stop/continue)、HCM(协议解码)、generic_proxy(协议无关框架)、redis_proxy/mysql_proxy/...(各协议代理)。[`well_known_names.h`](../envoy/source/extensions/filters/network/well_known_names.h) 列了全部内置 filter 名字。
- **想理解 HCM 内部**:读 P3-08——HCM 怎么用 codec 插件化把 HTTP/1.1/2/3 收进一个壳子、怎么用 `ActiveStream` 管多路复用下的每条请求、怎么驱动 http filter 链。本章只钉了"HCM 是 network filter"这条统一性,内部留给 P3-08。
- **想动手感受**:写一个最小 listener,filter_chain 里只配 `envoy.filters.network.tcp_proxy`,cluster 指向一个后端(比如本机 Redis),用 redis-cli 连 Envoy 端口,看透传;再换成 `envoy.filters.network.echo`,看字节回环;再换成 `envoy.filters.network.http_connection_manager`,看 HCM 解 HTTP。三种 filter 同一套注册机制,行为天差地别——这就是 network filter 链的威力。

### 引出下一章

本章把"字节流层的 filter 链"拆透了。可字节流层之后,如果这连接是 HTTP(绝大多数微服务场景),字节要被"读懂"成结构化的 HTTP 请求——这就是 HCM 的活。**下一章 P3-08,我们进 HTTP Connection Manager:HCM 怎么用一个统一的 codec 接口把 HTTP/1.1、HTTP/2、HTTP/3 三种协议收进同一个壳子,怎么为每条请求建一个 `ActiveStream`,怎么驱动一条独立的 http filter 链(decoder/encoder 两向)。** 本章钉了"HCM 是 network filter 特例"这条统一性,下一章拆 HCM 内部的 HTTP 引擎——这是从字节流世界跨进 HTTP 世界的桥。

> **下一章**:[P3-08 · HTTP Connection Manager(HCM)](P3-08-HTTP-Connection-Manager-HCM.md)
