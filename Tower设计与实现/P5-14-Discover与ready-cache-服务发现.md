# 第 14 章 · Discover 与 ready_cache:服务发现

> 第 5 篇 · 路由与负载均衡类中间件 · 组合单元

## 章首 · 核心问题

这一章只回答一个问题:

> **后端服务列表是动态的(实例不断地上线、下线),客户端怎么实时感知这个变化,把"一群随时在变的后端"变成 Tower 中间件能消费的东西?**

写过任何微服务 client 的人,都撞过这个问题。你以为你的 gRPC client 后面挂了 3 个 backend pod,实际在生产里,这个数字每一分钟都在变:K8s 把 Deployment 从 3 副本扩到 5 副本(上线)、某个 pod OOM 被 evict(下线)、滚动更新时新 pod 起来一个老 pod 又被杀一个、健康检查把一个慢 pod 临时剔除、上云时多可用区调度抖动……你拿到的那张"后端 IP:端口列表"从来不是静态的,**它是一条随时间流动的流**。

客户端怎么感知这个流动?如果让你自己写,你大概率会写出下面三种里的一种:

1. **启动时拿一次列表,写死。** client 启动时去 etcd/Consul/K8s API server 拉一次后端列表,存进一个 `Vec`,之后再也不刷新。后果:扩容的实例永远接不到流量,下线的实例每次请求都连接失败,client 看到的"世界"停在了启动那一刻;
2. **定时轮询(poll)。** client 起一个后台 task,每 30 秒重新拉一次完整列表,把旧的整个替换掉。后果:30 秒粒度太粗(扩容的实例要等 30 秒才被用),粒度细了又给注册中心压力(每秒拉一次完整列表,集群一大就扛不住);而且"整列表替换"意味着每次轮询都要 diff 一下哪些新增哪些删除,逻辑容易写错;
3. **手动维护一个 `Mutex<HashMap<Key, Service>>`,每个事件手动增删。** 注册中心推一个 "add backend A" 事件,你 `map.lock().insert(A, svc)`,推一个 "remove backend A",你 `map.lock().remove(A)`。后果:`Mutex` 把所有访问串行化(承 P2-05 讲过的 `Arc<Mutex<Service>>` 三重罪);而且你 lock 住 map 的时候,别的 task 想读某个后端也得排队;更糟的是,你存的 `Service` 可能没就绪(`poll_ready` 还没跑),发请求时才发现"这个后端其实连不上"。

这三个反例都漏在同一个地方:**它们把"后端列表"当成一个静态数据结构(`Vec`/`HashMap`),而不是一个动态事件流**。后端列表的本质是"随时间产生 Insert/Remove 事件",这是异步流的形状,不是集合的形状。

Tower 给出的答案干净利落:

> **把"动态后端列表"建模成一个 `Discover`(一个 sealed trait alias for `TryStream<Item = Change>`),`Change` 只有两个变体 `Insert(Key, Service)` / `Remove(Key)`。Discover 承 Tokio Stream 机制(后端列表天然是异步变化的流),Balance(P5-15)消费这个流。中间用 `ReadyCache` 缓存"已就绪"的服务,用 `indexmap` 维护有序映射——既能在 `poll_discover` 产 Remove 时 O(1) 删除,又能让 Balance 的 P2C 算法按下标随机采样(O(1) 索引 + 可迭代)。**

这是把"服务发现"这件云原生系统里的核心难题,落进 Rust 异步生态的样子。Discover 是"服务列表变化"的抽象,ReadyCache 是"已就绪服务集合"的缓存,Change 是"增删"的最小事件单元。读完本章你会明白:

1. **为什么 Discover 是 `TryStream<Item = Change>` 的 trait alias,而不是新 trait**——后端列表的变化天然是异步流(承 Tokio Stream),不需要重新发明,Discover 只是给"`Item = Change` 的 TryStream"起个名字,sealed + blanket impl 保证零运行时开销、外部 crate 没法乱实现;
2. **`Change::Insert(Key, Service)` / `Change::Remove(Key)` 为什么这样设计**——`Insert` 带上 Key + Service(增的时候要同时给身份和实物),`Remove` 只带 Key(删的时候调用方已经持有过 Service,凭身份删即可),Key 必须 `Eq`(身份相等才能匹配增删);
3. **`ReadyCache` 为什么把服务分成 pending / ready 两套集合**——刚 `push` 进来的服务未必 `poll_ready` 过(后端实例刚起,TCP 握手还没好),要先放进 pending 集,异步 poll 到就绪再迁到 ready 集;ready 集才是 Balance 能直接用的"立即可发请求"的服务;
4. **为什么 ready 集用 `indexmap::IndexMap` 而不是 `HashMap`/`BTreeMap`**——P2C 负载均衡要按下标随机抽两个(`rng.gen_range(0..len)` 取下标),`HashMap` 没有按下标访问的能力,`BTreeMap` 按下标是 O(log n),`IndexMap` 既保留插入顺序可按下标 O(1) 访问、又能 O(1) 按键查找删除;
5. **Discover 怎么和 Balance(P5-15)组合**——Balance 在 `poll_ready` 里先 `poll_discover` 拉变更(Insert 就 `push` 进 cache、Remove 就 `evict`),再 `poll_pending` 推进就绪,最后在 ready 集上做 P2C 选一个发请求。Discover 是 Balance 的"上游数据源"。

> **逃生阀**(这章概念密度大,先读这一段)。
>
> 如果你只想要一句话:**Discover = 把"动态后端列表"做成一个异步流(`TryStream<Item = Change>`),流里产出 `Insert(key, svc)` / `Remove(key)` 事件;ReadyCache = 把 Discover 流出的服务缓存下来,分成 pending(还没就绪,异步 poll)和 ready(已就绪,可直接 call)两套集合,用 indexmap 维护让 Balance 能按下标随机采样**。如果你对 Tokio 的 `Stream`/`TryStream`/`poll_next` 不熟,先读《Tokio》(本章一句带过指路);对 Service trait 的 `poll_ready`/`call` 不熟,先读 P1-02;对"`&mut self` 取走就绪状态"不熟,先读 P2-05(Buffer 章),本章的 `call_ready_index` 用了同样的"取走再放回 pending"惯用法。

## 章首 · 一句话点破

> **Discover 把"后端列表的变化"建模成一个 `TryStream<Item = Change>`,`Change` 只有 `Insert`/`Remove` 两个变体——这是云原生里"服务发现"的最小完整抽象,承 Tokio Stream 的异步流机制(后端列表天然是异步变化的,不是静态集合)。ReadyCache 把 Discover 流出的服务按"已就绪/未就绪"分两套集合缓存,pending 集异步 poll 到就绪迁到 ready 集,ready 集用 indexmap 维护保证 Balance 的 P2C 能 O(1) 按下标采样、O(1) 按键删除。这套 Discover + ReadyCache 是 Balance(P5-15)和 Steer(P5-16)的标准上游——所有"在多个动态后端之间分发"的中间件,都从它俩取数据。**

这是结论,不是理由。本章倒过来拆:先看动态后端为什么是个难题(静态列表/轮询/手撸 map 的坑),再看别的系统怎么处理(Envoy EDS/K8s watch/Consul),然后看 Tower 为什么用 Discover 流 + ReadyCache 缓存这种结构,最后逐行拆源码,讲清 `indexmap` 的有序映射、pending/ready 分离、cancellation 机制为什么 sound。

---

## 正文

### 14.1 痛点:后端列表是动态的,把它当静态集合会撞墙

#### 14.1.1 一个真实场景:微服务 client 后面挂着一群 pod

设想你在写一个简化版的微服务 client,内部持有一组后端连接(每个后端一个 `Service<Request>`,可能是 P4-13 讲的 `Reconnect` 包过的连接):

```rust
// (示意, 非源码原文)
struct MicroServiceClient {
    backends: Vec<BoxService>,   // 一组后端
    // ...
}

impl Service<Request> for MicroServiceClient {
    fn call(&mut self, req: Request) -> Self::Future {
        // 随便挑一个后端发请求
        let idx = rand::random::<usize>() % self.backends.len();
        self.backends[idx].call(req)
    }
}
```

这个 client 用得好好的,直到运维同学把后端 Deployment 从 3 副本扩到 5 副本。K8s 起了两个新 pod,把它们的状态写进了 etcd,所有 watch etcd 的客户端都收到了 "add pod D"、"add pod E" 两个事件。但**你的 client 不 watch**,它的 `backends: Vec` 在启动时就定死了 3 个,新增的 D、E 永远接不到流量——3 个老 pod 扛着全部流量,2 个新 pod 闲着。这还不算最糟。

更糟的是反向场景:某个老 pod A 被 OOM kill 了,K8s 把它从 etcd 删掉。你的 client 的 `Vec` 里还留着 A,下次 `call` 时随机选到 A,`A.call(req)` 立刻拿到一个 `Connection refused`(进程已经死了,pod IP 可能还在但端口不通),请求失败。你的调用方看到一个莫名其妙的 500,而其实 B、C、D、E 都还活着,本可以把请求转过去。

这就是服务发现的第一个痛点:**后端列表是动态的,但客户端把它当静态集合,两边世界观对不上**。

#### 14.1.2 朴素方案一:启动时拿一次列表,写死

最朴素的反应:启动时去注册中心拿一次完整列表,存起来。

```rust
// (示意, 朴素写法, 反例)
async fn build_client() -> MicroServiceClient {
    let backends: Vec<String> = registry.list_all().await;   // 启动时拉一次
    let services: Vec<_> = backends.iter()
        .map(|addr| connect(addr))   // 每个地址建一条连接
        .collect::<Vec<_>>().await;
    MicroServiceClient { backends: services }
}
```

这个写法撞三堵墙:

**墙一:扩容感知不到。** 启动后注册中心新增的实例,client 永远不知道。高峰扩容的收益完全拿不到,新加的机器闲着。

**墙二:下线没剔除。** 启动后挂掉的实例,client 还在用。每次请求都可能命中死实例,失败率随时间累积上升。重试能掩盖一部分,但重试放大流量(P4-11 讲过),治标不治本。

**墙三:启动慢且脆。** 一次性建所有连接,启动时间长(几十个实例 = 几十个 TCP 握手串行/并行);启动时正好某个实例不可用,整个 client 起不来。

> **钉死这件事**:把动态列表写死,等于把客户端的世界观冻结在启动瞬间。云原生系统的本质是"一切都在变"(扩缩容、滚动更新、健康检查、故障转移),静态列表根本无法表达这种变化。

#### 14.1.3 朴素方案二:定时轮询完整列表

第二反应:那定时刷新总行了吧?

```rust
// (示意, 朴素写法, 反例)
struct PollingClient {
    backends: Arc<RwLock<Vec<BoxService>>>,   // 读写锁保护的列表
}

// 后台 task 每 30 秒重新拉一次
async fn refresh_loop(client: PollingClient) {
    loop {
        tokio::time::sleep(Duration::from_secs(30)).await;
        let new_list: Vec<String> = registry.list_all().await;
        let mut backends = client.backends.write().await;
        *backends = new_list.into_iter()
            .map(|addr| connect(addr))
            .collect::<Vec<_>>().await;
    }
}
```

轮询(`poll`)比静态列表好——至少它感知到了变化。但它仍然撞三堵墙:

**墙一:粒度困境。** 轮询间隔太长(30 秒),扩容的实例要等 30 秒才被用,这 30 秒高峰流量压垮老实例;间隔太短(1 秒),给注册中心巨大压力——你一个 client 每秒拉一次完整列表,几千个 client 就是每秒几千次完整列表拉取,注册中心扛不住。这个两难是轮询的固有缺陷。

**墙二:整列表替换的开销。** 每次 `*backends = new_list` 把整个 `Vec` 替换掉——即使这次只有 1 个实例变化(比如新增了 D),你也要重建整个列表,旧的 A、B、C 连接被 drop 掉重建。连接重建是昂贵的(TCP 握手 + TLS 握手 + 可能的鉴权),根本没必要因为"加了 D"就把 A、B、C 也重建。

**墙三:diff 逻辑分散且易错。** 严格的轮询实现应该 diff(对比新旧列表,只增删差异部分),但 diff 逻辑要你自己写——`new` 里有 `old` 没有的就 insert,`old` 里有 `new` 里没有的就 remove,这个集合差运算如果手写,容易漏边界(比如"同一个 key 但服务实例变了"怎么处理)。逻辑散在业务里,改一次漏一处。

> **钉死这件事**:轮询的根本问题是"拉(pull)模式 + 整列表语义"。它把注册中心当成"被反复查询的数据库",把列表变化当成"快照差异",而注册中心其实是个"事件流"(实例上线发一个事件、下线发一个事件),快照只是事件流的瞬时投影。用错语义,粒度和开销两难无解。

#### 14.1.4 朴素方案三:手动 `Mutex<HashMap<Key, Service>>` 维护

第三反应:用事件流(注册中心推事件,我增删 map),用一个 `Mutex<HashMap>` 维护:

```rust
// (示意, 朴素写法, 反例)
struct ManualClient {
    backends: Arc<Mutex<HashMap<String, BoxService>>>,
}

// 注册中心推 add 事件
async fn on_add(client: ManualClient, key: String, addr: String) {
    let svc = connect(addr).await;
    client.backends.lock().await.insert(key, svc);
}

// 注册中心推 remove 事件
async fn on_remove(client: ManualClient, key: String) {
    client.backends.lock().await.remove(&key);
}

// 发请求时随机选一个
async fn call(client: ManualClient, req: Request) -> Response {
    let map = client.backends.lock().await;
    let keys: Vec<_> = map.keys().collect();
    let key = keys[rand::random::<usize>() % keys.len()];
    map[key].call(req).await     // ← map 锁还没释放, call 又要 &mut self, 死锁
}
```

事件流(`on_add`/`on_remove`)思路对了,但用 `Mutex<HashMap>` 装载仍然撞四堵墙:

**墙一:`Mutex` 全串行。** 所有访问(增、删、读、call)都要抢同一把锁。一个 `call` 在持锁期间跑(连接很慢),其他 task 全部阻塞,退回 P2-05 讲过的 "`Arc<Mutex<Service>>` 三重罪":全串行、阻塞 async 线程、背压破坏。

**墙二:`HashMap` 不能按下标访问。** 负载均衡要"随机抽一个",`HashMap` 没有按下标能力,你得先 `keys().collect::<Vec>()` 再随机——每次发请求都遍历整个 map 收集 key,代价 O(n)。集群一大,n 上千,这个开销不可忽略。

**墙三:存的 Service 没保证就绪。** 你 `insert(key, svc)` 时,`svc` 的 `poll_ready` 跑过吗?没有。发请求 `call` 时才发现"这个 svc 其实连不上",请求失败。你需要额外的"就绪检查"层,但这层得自己写,又散回业务。

**墙四:删和增的竞争没处理。** 同一个 key,先来一个 `on_add`(insert),马上又来一个 `on_remove`(remove),如果你不按顺序处理,可能删了新的留了旧的;如果 key 的相等语义不对(比如 key 是地址字符串但实例换了),匹配错乱。

> **钉死这件事**:`Mutex<HashMap>` 把"事件流"装对了(增删是对的语义),但容器选错了。`HashMap` 不能按下标访问,`Mutex` 全串行,存的 Service 不保证就绪——三个工程硬伤,在性能和正确性上都不 sound。Tower 的 ReadyCache 把这三个全修了:`IndexMap` 替代 `HashMap`(可按下标)、无锁的单 task 所有权模型替代 `Mutex`(承 P2-05 的 Buffer 模型)、pending/ready 分离替代"插入即就绪"。

#### 14.1.5 把问题摆清楚:后端列表是异步事件流

把三个反例的教训收束一下,我们要的东西是:

> **一个能消费"后端列表变化事件流"的抽象,它对每个事件做出反应(Insert 就加,Remove 就删),并维护一个"已就绪服务集合",让负载均衡器能高效地(随机采样)从中选一个发请求。这个抽象要承 Tokio Stream(因为列表变化天然是异步流),要避免 `Mutex` 串行(用单 task 所有权),要保证就绪(pending/ready 分离),要支持高效采样(indexmap)。**

这其实是把"服务发现"这件云原生核心能力,落进 Rust 异步生态。在 Go/Java 里这通常是 "watch + listener 模式 + 本地缓存",在 Tower 里它落成了 Discover + ReadyCache。

### 14.2 别的系统怎么做服务发现

在讲 Tower 的方案之前,先看几个真实系统怎么处理"动态后端列表",这能帮我们看清 Tower 的取舍是站在哪条线上。

#### 14.2.1 K8s:List + Watch(版本化事件流)

K8s 的服务发现是教科书级的 "List + Watch" 模式:

- **List**:client 启动时,先调 API server 的 `List` 接口拿一次完整资源列表(Endpoints/EndpointSlice),附带一个 `resourceVersion`(资源版本号);
- **Watch**:拿到初始版本号后,client 切到 `Watch` 接口,从那个版本号开始持续接收增量事件流——`ADDED`(新增)、`MODIFIED`(修改)、`DELETED`(删除)三个事件类型,每个事件带资源的当前状态;
- **重连**:Watch 连接断了,client 用最后收到的版本号重新 List + Watch,保证不丢事件。

K8s 的关键设计是"事件流 + 版本号"——事件不是孤立的,每个事件都在一条"版本号递增"的流上,断了能续。这避免了朴素轮询的粒度困境(Watch 是推送,实时),也避免了事件丢失(版本号 + 重连重 List)。

K8s 的事件类型其实有 3 个(ADDED/MODIFIED/DELETED),而 Tower 的 `Change` 只有 2 个(Insert/Remove)。为什么 Tower 不要 `Modify`?因为 Tower 抽象的是"服务的增删",一个服务"变了"(比如地址变了、TLS 配置变了)在 Tower 看来就是"旧的 Remove + 新的 Insert"——抽象到这个粒度,MODIFY 是冗余的。这是更瘦的抽象。

> **钉死这件事(K8s vs Tower 的抽象层级)**:K8s 的 Endpoints/EndpointSlice 是"资源对象",它有完整的 metadata/labels/spec,status 有就绪状态,事件能 MODIFY;Tower 的 `Change` 是"服务实例的增删",没有 metadata 概念,Key 是用户自己定义的(可以是字符串地址、可以是整数 ID),Service 是用户自己给的(可以是 Reconnect、可以是任何 `Service<Request>`)。Tower 比 K8s 低一层——Tower 不做"资源管理",只做"对一组服务的增删事件做出反应"。你可以用 K8s client 拿到 Endpoints 变化,转换成 `Change::Insert`/`Change::Remove` 喂给 Tower(很多生产代码就是这么干的)。

#### 14.2.2 Consul / etcd:长轮询或 Watch(键值事件流)

Consul 和 etcd 是更通用的 KV 注册中心,它们的服务发现机制类似:

- **Consul**:支持阻塞查询(blocking query,长轮询——client 发请求,server 持有直到数据变化才返回)+ Watch(基于 KV 的 watch plan);
- **etcd**:支持 Watch 接口,从某个 revision 开始订阅 KV 变化事件流(`PUT` / `DELETE`)。

两者都把"键值变化"建模成事件流,client 订阅流,增量接收 `PUT key=value`(对应 Insert)和 `DELETE key`(对应 Remove)。这和 Tower 的 `Change` 模型几乎一一对应——`PUT` → `Change::Insert`,`DELETE` → `Change::Remove`。

差别同样在抽象层级:Consul/etcd 流的是"键值对"(value 是任意字节,需要 client 自己反序列化、自己连接后端),Tower 流的是"Service 实例"(调用方自己决定怎么造 Service,比如用一个 `MakeService` 工厂把地址造成 `Reconnect<Connection>`,再把 `Reconnect` 喂进 Discover 流)。

> **钉死这件事**:Consul/etcd 流的是"数据"(地址字符串),Tower 流的是"对象"(`Service<Request>`)。这看起来是个小差别,实际很关键——Tower 让你流的是已经构造好的 Service,Discover 的消费方(Balance)直接拿到能 `call` 的 Service,不需要再做"地址 → 连接"的转换。这意味着"造 Service"这一步发生在 Discover 之前(由调用方或 Discover 的实现负责),Discover 本身不做 IO,只做事件分发。

#### 14.2.3 Envoy:EDS(Endpoint Discovery Service,xDS 协议族)

Envoy(C++ 服务网格)有完整的服务发现子系统,叫 EDS(Endpoint Discovery Service),是 xDS 协议族的一员:

- Envoy 通过 xDS gRPC stream 订阅 cluster 的 endpoint 刘表(EDS),控制面推送 `ClusterLoadAssignment` 资源,里面有完整的 endpoint 列表(host:port + health status + weight + locality);
- Envoy 内部维护一个 `EndpointManager`,处理 endpoint 的增删、健康检查、locality 优先级、panic threshold;
- 配合 active health check(主动探测)和 outlier detection(被动剔除),Envoy 有一套完整的"哪些 endpoint 能用"的判断逻辑。

Envoy 的设计哲学是"重运行期配置 + 重控制面",endpoint 列表是控制面推下来的"配置",Envoy 把它当成数据,跑自己的健康检查和负载均衡算法。

> **对照《Envoy》**:Envoy 的 EDS/cluster 是 cluster manager + thread local cluster + 一整套 endpoint 管理机制,《Envoy》P3 已拆透,一句带过指路 `[[envoy-source-facts]]`。Tower 的 Discover/ReadyCache 是最小组件、编译期组合——它不做健康检查(那是调用方的事,比如用 Reconnect 检测连接死活)、不做 locality 优先级(那是更高层 LB 策略)、不做 panic threshold。两者不是替代关系,是不同抽象层——Envoy 是"完整的服务网格数据面",Tower 是"服务发现 + 负载均衡的中间件积木"。Envoy 内部的 endpoint 管理在概念上和 Tower 的 Discover/ReadyCache 同构(订阅 + 缓存 + 就绪管理),但 Envoy 把它做成了一个完整子系统,Tower 把它做成了两个可组合的小组件。

#### 14.2.4 gRPC:resolver + balancer(参照)

gRPC(C++/Go)的客户端也内置了服务发现,模型叫 "resolver + balancer":

- **resolver**:负责把"服务名"(比如 `dns:///my-service.default.svc.cluster.local`)解析成一串地址(从 DNS / xDS / 自定义解析器拿),地址变化时通知 balancer;
- **balancer**:负责在地址间做负载均衡(round-robin / pick-first / grpclb / xDS),接收 resolver 的地址变化,维护一组子通道(subchannel),每个子通道有自己的连接状态和就绪状态。

gRPC 的 resolver 通知 balancer 用的也是"地址列表变化"的模型——但 gRPC 的设计是"完整列表 + 状态",不是纯粹的增量事件流。它的 `ResolverResult` 通常带完整的新地址列表,balancer 内部 diff。

Tower 的 Discover 比这更瘦:纯增量事件流(`Change::Insert`/`Remove`),不带状态(就绪状态由 `ReadyCache` 内部 poll 出来,不靠 resolver 推)。这是两种不同的取舍——gRPC 让 resolver 提供更多信息(包括连接状态提示),Tower 让 resolver 只管增删、就绪自己 poll。

> **钉死这件事(三种模型对比)**:K8s/Consul/etcd = 事件流(增删事件,带版本/revision);gRPC resolver = 完整列表(每次推完整新列表,balancer 内部 diff);Envoy EDS = 完整列表配置(控制面推完整 endpoint 配置,Envoy 内部 diff + 健康检查);Tower Discover = 事件流(`Change::Insert`/`Remove`)。Tower 选了"事件流"模型(承 K8s/Consul/etcd 的传统),因为这模型最瘦、最组合友好——消费方(Balance)只需要对每个事件做出反应,不需要 diff,不需要管理版本号。这是最小抽象的选择。

### 14.3 所以 Tower 这么设计:Discover 流 + ReadyCache 缓存

把痛点(14.1)和别家做法(14.2)摆清楚后,Tower 的方案就呼之欲出了:**用 `Discover`(一个 TryStream alias)把动态后端建模成 Change 事件流,用 `ReadyCache` 把流出的服务缓存成 pending/ready 两套集合**。

#### 14.3.1 第一步:把"后端列表变化"建模成 `Change` 枚举

后端列表的所有变化,本质就两类:**新增了一个服务**、**移除了一个服务**。Tower 用一个两变体的枚举表达(`tower/src/discover/mod.rs#L99-L106`):

```rust
/// A change in the service set.
#[derive(Debug, Clone)]
pub enum Change<K, V> {
    /// A new service identified by key `K` was identified.
    Insert(K, V),
    /// The service identified by key `K` disappeared.
    Remove(K),
}
```

注意几件事:

1. **`Change<K, V>` 是泛型**——`K` 是 key 的类型(身份,比如 `String` 地址、`usize` 索引、`SocketAddr`),`V` 是 service 的类型(被发现的那个 `Service<Request>`)。Tower 不假设 key 和 service 是什么具体类型,只要求 `K: Eq`(身份能比较,见下文);
2. **`Insert(K, V)` 带上 key 和 service**——增的时候要同时给"身份"(这个服务叫什么)和"实物"(这个服务本身)。Discover 的消费方需要 key 来后续 Remove 时匹配,需要 service 来发请求;
3. **`Remove(K)` 只带 key**——删的时候调用方已经持有过这个 service(在 Insert 时拿到了),凭 key 就能定位要删的。Remove 不带 service,因为 service 在消费方的缓存里(ReadyCache),删的时候从缓存里取出来 drop;
4. **没有 `Modify` 变体**——一个服务"变了"(地址换了、配置换了)在 Tower 看来是"先 Remove 旧的 + 再 Insert 新的"两个事件。这把抽象压到最瘦(承 14.2.1 讲过的"MODIFY 是冗余的")。

`Change` 的 `Key: Eq` 约束很关键——它是增删匹配的基础。Insert(A, svc) 进来,后续 Remove(A) 才能匹配上同一个 A;如果 key 不 `Eq`,就没办法判断"这个 Remove 要删的是哪个"。`Eq` 比 `PartialEq` 更强(承 Rust 标准: `Eq` 表示"对所有值,== 都满足等价关系",`PartialEq` 允许 `NaN != NaN` 这种破缺),用于 key 是合理要求——key 不能像 `f64::NAN` 那样自己不等于自己,否则增删全乱套。

> **钉死这件事(Change 的最小性)**:`Change` 只有 Insert/Remove 两个变体,这是"服务列表变化"的最小完整抽象。任何更复杂的语义(Modify、Update、HealthCheck)都能用 Insert/Remove 组合表达(Update = Remove + Insert)。这种"最小抽象"是 Tower 一贯的风格——发现/缓存只管"增删",就绪/健康交给 ReadyCache 内部 poll、交给 Reconnect 检测连接死活、交给外层 LoadShed 处理满载。每个中间件单一职责。

#### 14.3.2 第二步:Discover 是 `TryStream<Item = Change>` 的 trait alias

有了 `Change`,怎么表达"一个会源源不断产出 Change 的东西"?这正是异步流(Stream)的形状。Tower 直接复用 Tokio 生态的 Stream 抽象,把 Discover 定义成一个 trait alias(`tower/src/discover/mod.rs#L54-L97`):

```rust
/// A dynamically changing set of related services.
///
/// As new services arrive and old services are retired,
/// [`Change`]s are returned which provide unique identifiers
/// for the services.
pub trait Discover: Sealed<Change<(), ()>> {
    /// A unique identifier for each active service.
    type Key: Eq;

    /// The type of [`Service`] yielded by this [`Discover`].
    type Service;

    /// Error produced during discovery
    type Error;

    /// Yields the next discovery change set.
    fn poll_discover(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Change<Self::Key, Self::Service>, Self::Error>>>;
}

impl<K, S, E, D: ?Sized> Sealed<Change<(), ()>> for D
where
    D: TryStream<Ok = Change<K, S>, Error = E>,
    K: Eq,
{
}

impl<K, S, E, D: ?Sized> Discover for D
where
    D: TryStream<Ok = Change<K, S>, Error = E>,
    K: Eq,
{
    type Key = K;
    type Service = S;
    type Error = E;

    fn poll_discover(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<D::Ok, D::Error>>> {
        TryStream::try_poll_next(self, cx)
    }
}
```

这段是本章最关键的一段代码。逐行读:

1. **`pub trait Discover: Sealed<Change<(), ()>>`**——Discover 有一个 supertrait `Sealed<Change<(), ()>>`。`Sealed` 是 Tower 内部的 sealed 模式(`tower/src/lib.rs#L221-L225`):

   ```rust
   #[allow(unreachable_pub)]
   #[cfg(any(feature = "balance", feature = "discover", feature = "make"))]
   mod sealed {
       pub trait Sealed<T> {}
   }
   ```

   `Sealed` 是个**私有模块里的空 trait**,外部 crate 没法 impl 它(因为它在 `mod sealed` 里,虽然是 `pub trait` 但模块是私有的)。这导致 Discover 也是"外部 crate 没法直接 impl"——你只能靠下面的 blanket impl 自动获得。这是 Rust 的 sealed trait 模式,承 P4-13 讲 MakeService 时的同一手法;

2. **关联类型 `Key`/`Service`/`Error`**——分别对应 Change 的 key 类型、service 类型、错误类型。`Key: Eq`(承 14.3.1);

3. **唯一的方法 `poll_discover`**——签名和 `Stream::poll_next` 几乎一模一样:`Pin<&mut Self> + Context -> Poll<Option<Result<Change, Error>>>`。`Poll<Option<Result<...>>>` 这个三层嵌套是异步流的标准形状(承 Tokio Stream):
   - 外层 `Poll`:`Ready`(有事件 / 流结束)还是 `Pending`(暂时没事件,等);
   - 中层 `Option`:`Some`(还有事件)还是 `None`(流结束了,Discover 不会再产出新事件);
   - 内层 `Result`:`Ok(Change)`(成功事件)还是 `Err`(发现过程出错,比如注册中心连接断了);

4. **两个 blanket impl**——这是 trait alias 的实现方式。第一个 impl 给所有符合条件的类型实现 `Sealed`(让它们"有资格"当 Discover),第二个 impl 给所有符合条件的类型实现 `Discover` 本身:

   ```rust
   impl<K, S, E, D: ?Sized> Discover for D
   where
       D: TryStream<Ok = Change<K, S>, Error = E>,
       K: Eq,
   { ... }
   ```

   读这段:**任何 `D: TryStream<Ok = Change<K, S>, Error = E>` 其中 `K: Eq`,都自动实现 `Discover`**。`poll_discover` 就是 `TryStream::try_poll_next`(TryStream 的标准方法,从 `futures-core` 来)。Discover 没有引入任何新行为,它只是给"`Ok = Change` 的 TryStream"起个名字。

> **钉死件事(Discover 的零开销 trait alias)**:Discover 不是新 trait,它是 `TryStream<Item = Result<Change, _>>` 的 trait alias。你不需要"实现 Discover",你只要写一个 `TryStream`(Item 是 `Result<Change<K, S>, E>`),它自动就是 Discover。这意味着:
> - **零运行时开销**:`poll_discover` 直接调 `try_poll_next`,没有 vtable 跳转(泛型单态化后是静态调用);
> - **零样板代码**:不用写 `impl Discover for MyType`,只要你的类型已经是 TryStream;
> - **自动获得所有 Stream 组合子**:`take`/`filter`/`map`/`chain` 这些 `futures-util` 的 Stream 适配器都能用在 Discover 上,你可以 `discover.filter(|change| ...)` 过滤事件。
>
> 这承 P4-13 讲的 MakeService 是同一个 Rust 设计模式(sealed + blanket impl 把一个语义约束做成 trait alias)。MakeService alias 的是 `Service<Target>`,Discover alias 的是 `TryStream<Ok = Change>`。

#### 14.3.3 第三步:Discover 承 Tokio Stream(为什么这么设计 sound)

Discover 选择"复用 TryStream"而不是"发明新 trait",背后是 Tower 一贯的设计哲学:**Tokio 生态已经把异步流讲透了,Tower 不重新发明**。

承接铁律:《Tokio》已经把 `Stream`/`TryStream`/`poll_next`/`Pin`/`Context`/`Waker` 这些异步流的机制拆透了——Stream 是 "`poll_next` 不断产出 Item,直到返回 `None`" 的异步迭代器,TryStream 是 "Item 是 `Result` 的 Stream",`Pin` 保证 self-referential 的 Future/Stream 能被安全 poll,`Waker` + `Context` 是唤醒机制。这些《Tokio》讲透的,本章一句带过指路 `[[tokio-source-facts]]`,篇幅全留 Tower 独有(Discover 怎么把服务发现建模成 Change 流、ReadyCache 怎么缓存就绪)。

Discover 复用 TryStream 的好处是显而易见的:

1. **异步性天然表达**——后端列表的变化是异步的(K8s watch 是异步推送、Consul long-polling 是异步等待、DNS 解析是异步 IO),用 TryStream 表达,`poll_discover` 返回 `Pending` 就是"暂时没新事件,等",返回 `Ready(Some(Ok(Change)))` 就是"有新事件",完美契合;
2. **取消语义天然**——Stream 的 `drop` = 取消订阅。Discover 的消费方(Balance)被 drop,Discover 也跟着 drop,内部的事件源(watch 连接、订阅句柄)被释放。这不需要额外的"取消订阅"逻辑,纯靠 Rust 所有权;
3. **背压天然**——Stream 的 pull 模型(poll_next)天然背压——消费方不 poll,生产方不推。Discover 的消费方按自己的节奏 `poll_discover`,不会被打爆。如果 Discover 内部的事件源推得太快(比如 K8s 短时间内推了几百个事件),消费方不 poll,事件在 Stream 内部缓冲,缓冲满了 Stream 自然 backpressure(承 Tokio mpsc 的容量限制)。

> **承接《Tokio》**:Discover 的异步流机制(`poll_next`/`Pin`/`Waker`/`Context`/Stream 取消 = drop/TryStream 的 try_poll_next)都是《Tokio》已拆透的(详见 `[[tokio-source-facts]]` 的 Stream 章节)。本章不重讲 Tokio Stream 内部,只讲 Tower 怎么把"服务发现"建模成 Change 流、ReadyCache 怎么消费这个流——这是 Tower 独有的设计,承 Tokio 但不重复 Tokio。

#### 14.3.4 第四步:`ServiceList`:Discover 的最简实现(静态列表)

讲 Discover 不能不讲它的最简实现 `ServiceList`,它是"Discover 怎么写"的最小范例(`tower/src/discover/list.rs#L12-L53`):

```rust
pin_project! {
    /// Static service discovery based on a predetermined list of services.
    ///
    /// [`ServiceList`] is created with an initial list of services. The discovery
    /// process will yield this list once and do nothing after.
    #[derive(Debug)]
    pub struct ServiceList<T>
    where
        T: IntoIterator,
    {
        inner: Enumerate<T::IntoIter>,
    }
}

impl<T, U> ServiceList<T>
where
    T: IntoIterator<Item = U>,
{
    pub fn new<Request>(services: T) -> ServiceList<T>
    where
        U: Service<Request>,
    {
        ServiceList {
            inner: services.into_iter().enumerate(),
        }
    }
}

impl<T, U> Stream for ServiceList<T>
where
    T: IntoIterator<Item = U>,
{
    type Item = Result<Change<usize, U>, Infallible>;

    fn poll_next(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        match self.project().inner.next() {
            Some((i, service)) => Poll::Ready(Some(Ok(Change::Insert(i, service)))),
            None => Poll::Ready(None),
        }
    }
}
```

`ServiceList` 把一个静态的迭代器(`IntoIterator<Item = Service>`)包装成一个 Stream,实现 Discover。读它的几个要点:

1. **`inner: Enumerate<T::IntoIter>`**——把迭代器的每个元素配一个索引(`enumerate()`),这个索引就是 `Change::Insert` 的 key。所以 `ServiceList` 的 key 类型是 `usize`(索引),不同服务用 `0, 1, 2, ...` 区分;
2. **`Stream::Item = Result<Change<usize, U>, Infallible>`**——Item 是 `Result<Change, Infallible>`,错误类型是 `Infallible`(不可能出错,因为静态列表不会失败)。注意它直接 impl 的是 `Stream`(不是 `TryStream`,但 `Stream<Item = Result<..>>` 自动是 TryStream,承 `futures-core` 的 blanket impl);
3. **`poll_next` 极简**——每次 poll,迭代器 `next()` 拿一个,有就 `Change::Insert(i, service)` 返回,没有就 `None`(流结束)。因为静态列表本来就没有 Remove(列表不会变),所以 ServiceList 只产 Insert;
4. **`pin_project!`**——用 `pin-project-lite` 宏把 `inner` 字段 pin 住(承 P2-05/P3-08 都讲过 pin-project-lite 手写状态机的手法),因为 Stream 要求 `poll_next(self: Pin<&mut Self>)`。

`ServiceList` 的用途是**测试**和**静态后端场景**——你的后端是固定的(比如配置文件里写死的 3 个 IP),不需要动态发现,用 `ServiceList::new(vec![svc1, svc2, svc3])` 就够了。它产一次 Insert 流(3 个事件)就结束,Discover 语义上"完成"了。

> **钉死件事(ServiceList 是 Discover 的参考实现)**:要看"怎么写一个 Discover",`ServiceList` 是最简范例——只要写一个 `Stream<Item = Result<Change, _>>`,自动就是 Discover。生产里的 Discover 实现通常是 `K8sWatch`/`ConsulWatch`/`DnsResolver` 之类,它们内部跑 K8s/Consul/DNS 的 watch,把收到的地址转换成 `Change::Insert(addr, make_service.make_service(addr))` / `Change::Remove(addr)` 喂出来。这些生产实现不在 Tower 仓里(它们在 linkerd/kube-rs/tonic 等项目里),Tower 只提供 Discover 抽象 + ReadyCache 缓存,具体的事件源由用户接。

#### 14.3.5 第五步:ReadyCache 把 Discover 流出的服务缓存成 pending/ready 两套集合

Discover 流出的是"事件"(Insert/Remove),但 Balance 要的是"一组已经就绪的服务"(能直接 call 的)。中间需要一个缓存层,把事件转换成就绪服务集合——这就是 `ReadyCache`(`tower/src/ready_cache/cache.rs#L58-L74`):

```rust
/// Drives readiness over a set of services.
///
/// The cache maintains two internal data structures:
///
/// * a set of _pending_ services that have not yet become ready; and
/// * a set of _ready_ services that have previously polled ready.
pub struct ReadyCache<K, S, Req>
where
    K: Eq + Hash,
{
    /// A stream of services that are not yet ready.
    pending: FuturesUnordered<Pending<K, S, Req>>,
    /// An index of cancelation handles for pending streams.
    pending_cancel_txs: IndexMap<K, CancelTx>,

    /// Services that have previously become ready. Readiness can become stale,
    /// so a given service should be polled immediately before use.
    ///
    /// The cancelation oneshot is preserved (though unused) while the service is
    /// ready so that it need not be reallocated each time a request is
    /// dispatched.
    ready: IndexMap<K, (S, CancelPair)>,
}
```

`ReadyCache` 内部有三个字段,但语义上是"两套集合"——pending 集(没就绪)+ ready 集(已就绪):

1. **`pending: FuturesUnordered<Pending<K, S, Req>>`**——pending 集,用 `FuturesUnordered`(承 `futures-util`)装一堆 `Pending` Future。每个 `Pending` 是一个"等待某个 service `poll_ready` 就绪"的 Future(下文详述)。`FuturesUnordered` 是一个可以并发 poll 多个 Future 的容器,poll 它返回"哪个 Future 就绪了"——这是 ReadyCache 异步推进 pending 集的关键;
2. **`pending_cancel_txs: IndexMap<K, CancelTx>`**——pending 集的"取消句柄"索引。每个 pending service 都有一个 `CancelTx`(取消发送端),存这个索引是为了在 `evict` 时能通过 key 找到 CancelTx,取消那个还在等待的 Pending Future;
3. **`ready: IndexMap<K, (S, CancelPair)>`**——ready 集,用 `IndexMap` 装已就绪的 service。每个 entry 是 `(S, CancelPair)`:S 是 service 本身,CancelPair 是(取消发送端,取消接收端)——这个取消对在 service 还没进 pending 之前就创建好了,在 ready 集里"保留"(虽然没用),下次 service 用完回到 pending 集时复用,省得重新分配。

> **钉死件事(pending/ready 分离的精髓)**:为什么 ReadyCache 要分两套集合?因为 `poll_ready` 是异步的(承 P1-02)。一个 service 刚 `push` 进来,它的 `poll_ready` 可能返回 `Pending`(连接还在握手、连接池满了、限流到顶),不能立刻用。如果只有一套集合,要么"插入即就绪"(错的,会发请求到没就绪的 service 上),要么"插入时阻塞 poll_ready"(错的,会阻塞当前 task)。ReadyCache 的解法:插入时进 pending 集(异步等待),`poll_pending` 反复推进 pending 集,谁 ready 了谁迁到 ready 集。ready 集里的 service 才是"立即可发请求"的。这承 P2-05/P2-06 的 SpawnReady 思路——把"就绪"这件事从请求路径剥离,异步推进。

#### 14.3.6 第六步:为什么用 `indexmap::IndexMap`,不是 `HashMap`/`BTreeMap`

ReadyCache 的两个字段(`pending_cancel_txs` 和 `ready`)都用 `indexmap::IndexMap`,这不是随便选的。看 ReadyCache 对外暴露的接口,有好几个"按下标访问"的方法(`tower/src/ready_cache/cache.rs#L190-L208`):

```rust
/// Obtains a reference to a service in the ready set by index.
pub fn get_ready_index(&self, idx: usize) -> Option<(&K, &S)> {
    self.ready.get_index(idx).map(|(k, v)| (k, &v.0))
}

/// Obtains a mutable reference to a service in the ready set by index.
pub fn get_ready_index_mut(&mut self, idx: usize) -> Option<(&K, &mut S)> {
    self.ready.get_index_mut(idx).map(|(k, v)| (k, &mut v.0))
}

/// Returns an iterator over the ready keys and services.
pub fn iter_ready(&self) -> impl Iterator<Item = (&K, &S)> {
    self.ready.iter().map(|(k, s)| (k, &s.0))
}
```

这些 `get_ready_index` 方法存在的唯一目的,是支持 **P2C 负载均衡的随机采样**。看 P5-15 的 Balance 怎么用 ReadyCache(`tower/src/balance/p2c/service.rs#L159-L184`):

```rust
/// Performs P2C on inner services to find a suitable endpoint.
fn p2c_ready_index(&mut self) -> Option<usize> {
    match self.services.ready_len() {
        0 => None,
        1 => Some(0),
        len => {
            // Get two distinct random indexes (in a random order) and
            // compare the loads of the service at each index.
            let [aidx, bidx] = sample_floyd2(&mut self.rng, len as u64);
            debug_assert_ne!(aidx, bidx, "random indices must be distinct");

            let aload = self.ready_index_load(aidx as usize);
            let bload = self.ready_index_load(bidx as usize);
            let chosen = if aload <= bload { aidx } else { bidx };
            // ...
            Some(chosen as usize)
        }
    }
}

/// Accesses a ready endpoint by index and returns its current load.
fn ready_index_load(&self, index: usize) -> <D::Service as Load>::Metric {
    let (_, svc) = self.services.get_ready_index(index).expect("invalid index");
    svc.load()
}
```

P2C(Power-of-2-Choices)算法的核心是"在 ready 集里**随机抽两个下标**,比较负载,选负载小的"(P5-15 详述)。这个"随机抽两个下标"要求容器能按下标 O(1) 访问——`sample_floyd2(rng, len)` 返回两个 `[0, len)` 范围内的随机下标,然后 `get_ready_index(aidx)` 拿那个下标对应的 service。

这就是为什么必须是 `IndexMap`:

- **`HashMap` 不行**——`HashMap` 没有按下标访问的能力(`HashMap` 的内部布局是桶数组,但"下标"对应桶位置不是"第几个插入的元素",没有 `get_index` 方法)。要随机抽,只能 `keys().collect::<Vec>()` 再随机,每次 O(n);
- **`BTreeMap` 不行**——`BTreeMap` 按下标是 O(log n)(要中序遍历到第 i 个),虽然 `BTreeMap` 也提供迭代,但没有 O(1) 的 `get_index`;
- **`Vec<(K, V)>` 也不行**——`Vec` 能按下标 O(1),但按 key 查找/删除是 O(n)(要线性扫描)。ReadyCache 的 `evict` 要按 key 删(`Change::Remove(key)` 进来,要找到那个 key 删掉),`Vec` 删除是 O(n);
- **`IndexMap` 都行**——`IndexMap` 是 `indexmap` crate 提供的"有序哈希映射",内部维护一个"`Vec<K>`(插入顺序) + `HashMap<K, V>`(快速查找)"的双索引结构:按 key 查找/删除是 O(1)(用 HashMap),按下标访问是 O(1)(用 Vec),按插入顺序迭代是 O(n)(顺序遍历 Vec)。

`indexmap::IndexMap` 的双索引让它同时满足"按 key 操作"(增删查)和"按下标操作"(随机采样)两个需求,这是 ReadyCache 选它的根本原因。文档注释里专门提到这一点(`tower/src/ready_cache/cache.rs#L52-L57`):

```rust
/// Note that the by-index accessors are provided to support use cases (like
/// power-of-two-choices load balancing) where the caller does not care to keep
/// track of each service's key. Instead, it needs only to access _some_ ready
/// service. In such a case, it should be noted that calls to
/// [`ReadyCache::poll_pending`] and [`ReadyCache::evict`] may perturb the order of
/// the ready set, so any cached indexes should be discarded after such a call.
```

文档直接点明"by-index accessors 是给 P2C 负载均衡用的"——按下标访问存在的全部理由就是 P2C。同时文档提醒:"`poll_pending` 和 `evict` 可能扰动 ready 集的顺序,所以缓存的下标在这些操作后要丢弃"——这是因为 `IndexMap::swap_remove_index` 用的是"交换删除"(把要删的元素和最后一个交换再删,保持 O(1) 但改变了顺序),所以下标会变。

> **钉死件事(IndexMap 的双索引是 P2C 的前提)**:没有 `IndexMap` 的"按下标 O(1) 访问",P2C 算法没法高效实现——每次随机采样都要 O(n) 遍历,集群一大性能崩盘。`IndexMap` 让"按 key 增删"(Discover 的 Insert/Remove)和"按下标采样"(Balance 的 P2C)都 O(1),这是 ReadyCache 选它的工程理由。这是 Tower 把数据结构选型和算法需求绑定的典型例子——数据结构服务于算法,不是随便选的。

### 14.4 ReadyCache 的核心操作:push / poll_pending / check_ready / call_ready

ReadyCache 把 Discover 流出的服务管理起来,核心操作就四个:`push`(加新服务)、`poll_pending`(推进 pending 集到 ready 集)、`check_ready`/`check_ready_index`(检查某服务还就绪吗)、`call_ready`/`call_ready_index`(在就绪服务上发请求)。逐个拆。

#### 14.4.1 `push`:新服务进 pending 集

当 Discover 产出 `Change::Insert(key, svc)` 时,消费方调 `push` 把 svc 加进 cache(`tower/src/ready_cache/cache.rs#L249-L265`):

```rust
/// Pushes a new service onto the pending set.
///
/// The service will be promoted to the ready set as [`poll_pending`] is invoked.
///
/// Note that this does **not** remove services from the ready set. Once the
/// old service is used, it will be dropped instead of being added back to
/// the pending set; OR, when the new service becomes ready, it will replace
/// the prior service in the ready set.
pub fn push(&mut self, key: K, svc: S) {
    let cancel = cancelable();
    self.push_pending(key, svc, cancel);
}

fn push_pending(&mut self, key: K, svc: S, (cancel_tx, cancel_rx): CancelPair) {
    if let Some(c) = self.pending_cancel_txs.insert(key.clone(), cancel_tx) {
        // If there is already a service for this key, cancel it.
        c.cancel();
    }
    self.pending.push(Pending {
        key: Some(key),
        cancel: Some(cancel_rx),
        ready: Some(svc),
        _pd: std::marker::PhantomData,
    });
}
```

`push` 做三件事:

1. **`cancelable()` 创建取消对**——`cancelable()`(`tower/src/ready_cache/cache.rs#L418-L424`)创建一个 `(CancelTx, CancelRx)` 对。这个取消对是基于 `AtomicWaker` + `AtomicBool` 实现的(下文 14.5 详述),不是 `tokio::sync::oneshot`(注释专门说明这点):
   
   ```rust
   /// Creates a cancelation sender and receiver.
   ///
   /// A `tokio::sync::oneshot` is NOT used, as a `Receiver` is not guaranteed to
   /// observe results as soon as a `Sender` fires. Using an `AtomicBool` allows
   /// the state to be observed as soon as the cancelation is triggered.
   fn cancelable() -> CancelPair {
       let cx = Arc::new(Cancel {
           waker: AtomicWaker::new(),
           canceled: AtomicBool::new(false),
       });
       (CancelTx(cx.clone()), CancelRx(cx))
   }
   ```

   为什么不用 oneshot?注释说"oneshot 的 Receiver 不保证立即观察到 Sender 发送的信号"。oneshot 内部用 `AtomicUsize` 状态机,Receiver poll 时如果 Sender 还没发,会注册 waker 等;Sender 发的时候唤醒 waker,但 Receiver 下次 poll 才能读到——这有个"窗口",在这窗口里 Sender 已经 cancel 但 Receiver 不知道。ReadyCache 要的是"cancel 立刻可见"(在 Pending Future 的 poll 里第一件事就检查 canceled 标志),所以用 `AtomicBool`——`store(true, SeqCst)` 立刻对所有线程可见,`load(SeqCst)` 立刻读到。这是无锁同步的精妙(承 P2-05 的无锁思路);

2. **`pending_cancel_txs.insert(key, cancel_tx)`**——把 cancel_tx 存进索引,如果这个 key 之前已经有 cancel_tx(说明之前 push 过同一个 key 但还没就绪),`insert` 返回旧的 cancel_tx,接着 `c.cancel()` 取消旧的 Pending Future(避免同一个 key 有两个 Pending 在跑);

3. **`pending.push(Pending { key, cancel: cancel_rx, ready: svc, ... })`**——把 svc 包成 `Pending` Future,push 进 `FuturesUnordered`。这个 Pending Future 后续会被 `poll_pending` 反复 poll,直到 svc 的 `poll_ready` 返回 Ready。

注意 `push` 的文档说"this does not remove services from the ready set"——如果同一个 key 已经在 ready 集里,push 不会立刻删掉旧的 ready service。文档解释了两种后续:"once the old service is used, it will be dropped instead of being added back to the pending set"(旧 service 被用过后直接 drop,不回 pending,因为新的已经在 pending 等了);"when the new service becomes ready, it will replace the prior service in the ready set"(新 service 就绪后,insert 进 ready 集会覆盖旧的)。这个设计避免了"用旧 service 中途被新 service 顶替"的尴尬——旧 service 还能用完,新 service 就绪后接管。

> **钉死件事(push 的 key 冲突处理)**:同一个 key 重复 push,Tower 的策略是"取消旧的 pending + 让新的就绪后覆盖旧的 ready"。这个策略假设"后来的 service 是更新的版本"(比如 K8s 滚动更新,新 pod 起来了,同一个 key 对应的 service 应该换成新 pod 的连接)。如果你的语义是"同一个 key 永远不变"(比如 key 是固定地址,service 是 Reconnect),那 push 重复 key 触发的 cancel 是无害的(旧的 Pending 被 cancel,新的 Pending 跑同样的 Reconnect,poll_ready 后覆盖)。

#### 14.4.2 `poll_pending`:推进 pending 集到 ready 集

`poll_pending` 是 ReadyCache 的"发动机"——它反复 poll pending 集,把就绪的 service 迁到 ready 集(`tower/src/ready_cache/cache.rs#L281-L315`):

```rust
/// Polls services pending readiness, adding ready services to the ready set.
///
/// Returns [`Poll::Ready`] when there are no remaining unready services.
/// [`poll_pending`] should be called again after [`push`] or
/// [`call_ready_index`] are invoked.
pub fn poll_pending(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), error::Failed<K>>> {
    loop {
        match Pin::new(&mut self.pending).poll_next(cx) {
            Poll::Pending => return Poll::Pending,
            Poll::Ready(None) => return Poll::Ready(Ok(())),
            Poll::Ready(Some(Ok((key, svc, cancel_rx)))) => {
                trace!("endpoint ready");
                let cancel_tx = self.pending_cancel_txs.swap_remove(&key);
                if let Some(cancel_tx) = cancel_tx {
                    // Keep track of the cancelation so that it need not be
                    // recreated after the service is used.
                    self.ready.insert(key, (svc, (cancel_tx, cancel_rx)));
                } else {
                    assert!(
                        cancel_tx.is_some(),
                        "services that become ready must have a pending cancelation"
                    );
                }
            }
            Poll::Ready(Some(Err(PendingError::Canceled(_)))) => {
                debug!("endpoint canceled");
                // The cancellation for this service was removed in order to
                // cause this cancellation.
            }
            Poll::Ready(Some(Err(PendingError::Inner(key, e)))) => {
                let cancel_tx = self.pending_cancel_txs.swap_remove(&key);
                assert!(
                    cancel_tx.is_some(),
                    "services that return an error must have a pending cancelation"
                );
                return Err(error::Failed(key, e.into())).into();
            }
        }
    }
}
```

`poll_pending` 的核心是 `Pin::new(&mut self.pending).poll_next(cx)`——poll 那个 `FuturesUnordered`。`FuturesUnordered::poll_next` 内部并发 poll 所有 Pending Future,谁先就绪返回谁(承 `futures-util`)。这个 poll 有四种结果,对应四种处理:

1. **`Pending`**——所有 Pending 都还没就绪,返回 `Poll::Pending`,等下次唤醒;
2. **`Ready(None)`**——pending 集空了(所有 Pending 都迁到 ready 了),返回 `Poll::Ready(Ok(()))`,表示"暂时没有 pending 了";
3. **`Ready(Some(Ok((key, svc, cancel_rx))))`**——某个 Pending 就绪了!拿到 key/svc/cancel_rx 三件套:
   - `pending_cancel_txs.swap_remove(&key)` 把对应的 cancel_tx 取出来(从索引里删);
   - `ready.insert(key, (svc, (cancel_tx, cancel_rx)))` 把 service + 重组的 CancelPair 插进 ready 集。注意 cancel_tx 从索引取出后,和 cancel_rx 重新配对存进 ready——这样下次 service 用完回 pending 时,可以复用这对 CancelPair,不用重新分配(承 ready 字段注释的"cancelation is preserved so that it need not be reallocated");
   - `continue` 继续轮询(可能还有别的 Pending 也就绪了);
4. **`Ready(Some(Err(PendingError::Canceled(_))))`**——某个 Pending 被 cancel 了(因为 `evict` 调用了它的 cancel_tx)。文档注释说"the cancellation for this service was removed in order to cause this cancellation"——意思是 cancel_tx 在 `evict` 时已经被 `swap_remove` 取出并 `c.cancel()` 了,所以这里 PendingError::Canceled 进来时,pending_cancel_txs 里已经没这个 key 了,什么都不用做,继续轮询;
5. **`Ready(Some(Err(PendingError::Inner(key, e))))`**——某个 Pending 的 service `poll_ready` 失败了(承 P1-02 的"Ready(Err) = Service 死透")。这时:
   - `pending_cancel_txs.swap_remove(&key)` 清理索引;
   - 返回 `Err(error::Failed(key, e.into()))`——把失败的 key 和错误包成 `Failed`,告诉调用方"这个 service 死了"。调用方(Balance)通常会记录日志、丢弃这个 service,继续 poll_pending 处理别的。

注意 `poll_pending` 是个 `loop`——一次调用可能处理多个就绪的 Pending(每就绪一个就 continue,继续 poll),直到 `Pending` 或 `Ready(None)` 或 `Ready(Err)` 才退出。这避免了"一次 poll 只推进一个 Pending"的低效(承 P4-13 Reconnect 的 poll_ready 也是 loop 多态推进)。

> **钉死件事(poll_pending 的双状态语义)**:`poll_pending` 返回 `Poll::Ready(Ok(()))` 不是"所有 pending 都就绪了",而是"pending 集暂时空了"——可能所有都迁到 ready 了,也可能从来没 pending 过。它返回 `Poll::Pending` 表示"还有 pending 但暂时都没就绪,等"。这个语义很重要——Balance(P5-15)的 poll_ready 会反复调 poll_pending 直到它返回 Ready(Ok),表示"该迁的都迁了,可以开始 P2C 选 ready 集里的 service 了"。

#### 14.4.3 `check_ready_index`:ready 状态可能过期,发请求前再 poll 一次

ready 集里的 service 虽然之前 `poll_ready` 过,但**就绪状态可能过期**——比如一个 HTTP keep-alive 连接,在 ready 集里放了一会儿,这中间连接可能被对端关了(keepalive timeout 触发),实际已经不能用了。文档专门强调这一点(`tower/src/ready_cache/cache.rs#L34-L39`):

```rust
/// The ready set can hold services for an arbitrarily long time. During this
/// time, the runtime may process events that invalidate that ready state (for
/// instance, if a keepalive detects a lost connection). In such cases, callers
/// should use [`ReadyCache::check_ready`] (or [`ReadyCache::check_ready_index`])
/// immediately before dispatching a request to ensure that the service has not
/// become unavailable.
```

所以 ReadyCache 提供了 `check_ready` / `check_ready_index`,**在发请求前再 poll 一次**(`tower/src/ready_cache/cache.rs#L338-L373`):

```rust
/// Checks whether the referenced endpoint is ready.
///
/// If the service is no longer ready, it is moved back into the pending set
/// and `false` is returned.
///
/// If the service errors, it is removed and dropped and the error is returned.
pub fn check_ready_index(
    &mut self,
    cx: &mut Context<'_>,
    index: usize,
) -> Result<bool, error::Failed<K>> {
    let svc = match self.ready.get_index_mut(index) {
        None => return Ok(false),
        Some((_, (svc, _))) => svc,
    };
    match svc.poll_ready(cx) {
        Poll::Ready(Ok(())) => Ok(true),
        Poll::Pending => {
            // became unready; so move it back there.
            let (key, (svc, cancel)) = self
                .ready
                .swap_remove_index(index)
                .expect("invalid ready index");

            // If a new version of this service has been added to the
            // unready set, don't overwrite it.
            if !self.pending_contains(&key) {
                self.push_pending(key, svc, cancel);
            }

            Ok(false)
        }
        Poll::Ready(Err(e)) => {
            // failed, so drop it.
            let (key, _) = self
                .ready
                .swap_remove_index(index)
                .expect("invalid ready index");
            Err(error::Failed(key, e.into()))
        }
    }
}
```

`check_ready_index` 拿到 ready 集第 index 个 service,`poll_ready` 它,三种结果:

1. **`Ready(Ok(()))`——还就绪**。返回 `Ok(true)`,调用方可以接着 `call_ready_index`;
2. **`Pending`——不再就绪了**(比如连接刚才被关,正在重连)。这时 service 从 ready 集挪回 pending 集(`swap_remove_index` 从 ready 删,`push_pending` 加回 pending),返回 `Ok(false)`。注意那个 `if !self.pending_contains(&key)` 守卫——如果这个 key 已经在 pending 集了(说明 push 过新版本),不要覆盖,直接丢弃这个旧 service;
3. **`Ready(Err(e))`——service 死透了**。从 ready 集删掉(`swap_remove_index`),返回 `Err(Failed(key, e))`。调用方拿到错误,知道这个 service 死了,可以记录、上报。

这个"再 poll 一次"的设计是 ReadyCache 区别于"普通缓存"的关键——普通缓存假设"缓存的数据一直有效",ReadyCache 知道"就绪状态可能过期",所以发请求前必须验证。这就是为什么 `call_ready_index` 的文档强调 "panics if the specified service is not in the ready set"——它假设你刚 `check_ready_index` 过,如果不检查直接 call,可能 call 到一个不再就绪的 service 上。

> **钉死件事(就绪状态可能过期)**:ready 集里的 service 之前 poll_ready 过,但这不代表"现在还就绪"。连接可能在这期间死掉、限流可能到顶、负载可能满载。ReadyCache 强制"call 之前 check"——`check_ready_index` 再 poll 一次,确认就绪状态没过期。这是把"就绪状态的时效性"显式建模——ReadyCache 不保证 ready 集里的 service 永远就绪,只保证"刚才 check 过的瞬间是就绪的"。

#### 14.4.4 `call_ready_index`:取走就绪 service,用完回 pending

终于到发请求这一步。`call_ready_index` 在就绪 service 上调 `call`,**取走 service**(承 P1-02 的 `&mut self` + mem::replace 惯用法),用完之后把它放回 pending 集(因为它需要重新 poll_ready 才能再次就绪)(`tower/src/ready_cache/cache.rs#L393-L408`):

```rust
/// Calls a ready service by index.
///
/// # Panics
///
/// If the specified index is out of range.
pub fn call_ready_index(&mut self, index: usize, req: Req) -> S::Future {
    let (key, (mut svc, cancel)) = self
        .ready
        .swap_remove_index(index)
        .expect("check_ready_index was not called");

    let fut = svc.call(req);

    // If a new version of this service has been added to the
    // unready set, don't overwrite it.
    if !self.pending_contains(&key) {
        self.push_pending(key, svc, cancel);
    }

    fut
}
```

`call_ready_index` 做三件事,顺序非常讲究:

1. **`ready.swap_remove_index(index)`**——从 ready 集取走第 index 个 service。`swap_remove_index` 是 `IndexMap` 的方法,把目标元素和最后一个交换再删,O(1)。这一步**取走**了 service 的所有权(承 P1-02:`call` 是 `&mut self`,调用后 service 的"就绪状态"被消费了,需要重新 poll_ready);
2. **`svc.call(req)`**——在取走的 service 上发请求,拿到 Future `fut`。注意这里 `svc` 是 `mut`(因为 `Service::call(&mut self, req)`),所以前面 `let (key, (mut svc, cancel))` 的 `mut` 是必要的;
3. **`push_pending(key, svc, cancel)`(如果 key 不在 pending 集)**——把用过的 service 放回 pending 集(复用 cancel 对,省得重新分配)。它需要重新 `poll_ready` 才能再次进 ready 集——因为 `call` 之后 service 可能又"忙"了(比如 in-flight 请求数到达上限),需要重新等待。

为什么"用完放回 pending"?这承 P1-02 的核心:**`call` 消费就绪状态**。一个 service `poll_ready` 返回 Ready(Ok) 后,`call` 一次,它的就绪状态就被"用掉"了——下次想 call,得重新 `poll_ready`。如果 call 完不回 pending,这个 service 就永远卡在"刚 call 过"的状态,没法再次进入 ready 集被 P2C 选中。回 pending 让它能重新走 `poll_pending → poll_ready → 进 ready` 的循环。

那个 `if !self.pending_contains(&key)` 守卫和 check_ready_index 里的一样——如果 key 已经在 pending 集(有新版本在等),不要覆盖,丢弃旧 service。这是处理"同一个 key 在 call 中途又被 push 了新版本"的边界情况。

> **钉死件事(call_ready_index 的取走-放回)**:`call_ready_index` 严格遵循 Service trait 的 `&mut self` + `poll_ready` + `call` 契约(承 P1-02)。call 之前 service 在 ready 集(check 过),call 取走 service 的所有权,call 之后 service 回 pending 集(等下次 poll_ready)。这是"取走就绪状态再重新就绪"的惯用法(承 P2-05 Buffer 里 worker 也是 poll_ready + call + 重新 poll_ready 的循环)。ReadyCache 把这个循环做成 pending/ready 两集之间的迁移,优雅且 sound。

#### 14.4.5 `evict`:Remove 事件触发,从缓存里删服务

当 Discover 产出 `Change::Remove(key)` 时,消费方调 `evict` 把那个 key 对应的 service 从缓存里删(`tower/src/ready_cache/cache.rs#L217-L229`):

```rust
/// Evicts an item from the cache.
///
/// Returns true if a service was marked for eviction.
///
/// Services are dropped from the ready set immediately. Services in the
/// pending set are marked for cancellation, but [`ReadyCache::poll_pending`]
/// must be called to cause the service to be dropped.
pub fn evict<Q: Hash + Equivalent<K>>(&mut self, key: &Q) -> bool {
    let canceled = if let Some(c) = self.pending_cancel_txs.swap_remove(key) {
        c.cancel();
        true
    } else {
        false
    };

    self.ready
        .swap_remove_full(key)
        .map(|_| true)
        .unwrap_or(canceled)
}
```

`evict` 分两步删:

1. **`pending_cancel_txs.swap_remove(key)`**——如果这个 key 在 pending 集,取出它的 cancel_tx,`c.cancel()` 取消那个 Pending Future。**注意:这一步不立即 drop service**——Pending Future 还在 `FuturesUnordered` 里,要等下次 `poll_pending` 被 poll 到时,它读到 `canceled = true`,返回 `PendingError::Canceled`,才被 `FuturesUnordered` 移除,service 才真正 drop。文档注释明确说"Services in the pending set are marked for cancellation, but poll_pending must be called to cause the service to be dropped";
2. **`ready.swap_remove_full(key)`**——如果这个 key 在 ready 集,直接 `swap_remove_full` 删(IndexMap 的方法,O(1),返回被删的元素)。ready 集的删除是立即的(service 立刻 drop)。

为什么 pending 集不立即删?因为 `FuturesUnordered` 不支持"按 key 删除某个 Future"——它内部是一个 slab(槽位数组),Future 按插入顺序存在槽里,没有"按 key 索引"。要删某个 Future,要么遍历找(慢),要么用 cancel 信号让 Future 自己返回(让它从内部移除)。ReadyCache 选了后者——用 cancel 信号,下次 poll_pending 时 Pending 自己返回 Canceled,被 FuturesUnordered 移除。这是 `FuturesUnordered` + cancel 信号的标准用法。

> **钉死件事(evict 的延迟删除)**:pending 集的 evict 是"标记取消",不是"立即删除"。service 真正被 drop 是在下次 `poll_pending` 时。这意味着:evict 之后,pending 集里那个 service 还占着内存(短暂地),直到下次 poll_pending。调用方(Balance)的 poll_ready 会先调 evict(处理 Remove 事件),再调 poll_pending(推进 pending),所以这个延迟很短(一次 poll_ready 内就清理了)。但如果调用方 evict 后不调 poll_pending(理论上不该这样),那个 service 会一直占着,直到下次 poll_pending。这是 ReadyCache 的一个隐含契约——evict 后必须 poll_pending 才能真正清理。

### 14.5 Pending Future 与 cancellation 机制:为什么不用 oneshot

ReadyCache 最精妙的实现细节,是它的 Pending Future 和 cancellation(取消)机制。这一节单独拆,因为这是 ReadyCache 区别于朴素缓存的核心技巧。

#### 14.5.1 Pending Future:一个"等待 service 就绪"的 Future

每个 push 进来的 service,都被包成一个 `Pending` Future(`tower/src/ready_cache/cache.rs#L99-L109`):

```rust
pin_project_lite::pin_project! {
    /// A [`Future`] that becomes satisfied when an `S`-typed service is ready.
    ///
    /// May fail due to cancelation, i.e. if the service is evicted from the balancer.
    struct Pending<K, S, Req> {
        key: Option<K>,
        cancel: Option<CancelRx>,
        ready: Option<S>,
        _pd: std::marker::PhantomData<Req>,
    }
}
```

`Pending` 是个手写的 Future(用 `pin_project_lite` 把字段 pin 住),它持有四样东西:

- `key: Option<K>`——service 的身份。Option 是因为 Future 完成时要 take 走(承 P1-02 的 mem::replace 惯用法);
- `cancel: Option<CancelRx>`——取消接收端。poll 时第一件事检查它;
- `ready: Option<S>`——被等待的 service 本身。Option 同理;
- `_pd: PhantomData<Req>`——phantom,标记 Req 类型(因为 Pending 不直接持有 Req,但 Future 的 Output 涉及 S::Error,而 S: Service<Req>)。

Pending 的 Future 实现(`tower/src/ready_cache/cache.rs#L435-L483`):

```rust
impl<K, S, Req> Future for Pending<K, S, Req>
where
    S: Service<Req>,
{
    type Output = Result<(K, S, CancelRx), PendingError<K, S::Error>>;

    fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        let this = self.project();
        // Before checking whether the service is ready, check to see whether
        // readiness has been canceled.
        let CancelRx(cancel) = this.cancel.as_mut().expect("polled after complete");
        if cancel.canceled.load(Ordering::SeqCst) {
            let key = this.key.take().expect("polled after complete");
            return Err(PendingError::Canceled(key)).into();
        }

        match this
            .ready
            .as_mut()
            .expect("polled after ready")
            .poll_ready(cx)
        {
            Poll::Pending => {
                // Before returning Pending, register interest in cancelation so
                // that this future is polled again if the state changes.
                let CancelRx(cancel) = this.cancel.as_mut().expect("polled after complete");
                cancel.waker.register(cx.waker());
                // Because both the cancel receiver and cancel sender are held
                // by the `ReadyCache` (i.e., on a single task), then it must
                // not be possible for the cancelation state to change while
                // polling a `Pending` service.
                assert!(
                    !cancel.canceled.load(Ordering::SeqCst),
                    "cancelation cannot be notified while polling a pending service"
                );
                Poll::Pending
            }
            Poll::Ready(Ok(())) => {
                let key = this.key.take().expect("polled after complete");
                let cancel = this.cancel.take().expect("polled after complete");
                Ok((key, this.ready.take().expect("polled after ready"), cancel)).into()
            }
            Poll::Ready(Err(e)) => {
                let key = this.key.take().expect("polled after compete");
                Err(PendingError::Inner(key, e)).into()
            }
        }
    }
}
```

Pending::poll 的逻辑非常讲究,分三步:

1. **先检查 cancel**——`if cancel.canceled.load(SeqCst)` 如果已经被取消,立刻返回 `Err(PendingError::Canceled(key))`,不再 poll service。注释强调"Before checking whether the service is ready, check cancelation"——cancel 优先级最高,即使 service 这一刻也 ready 了,只要被 cancel 就直接 Canceled(因为 evict 的语义是"这个 service 不要了");
2. **`poll_ready(cx)` service**——三种结果:
   - `Pending`:service 还没就绪。**关键技巧**:在返回 Pending 之前,`cancel.waker.register(cx.waker())` 注册当前 waker 到 AtomicWaker。这样如果之后 cancel 被触发(`CancelTx::cancel` 会 `waker.wake()`),这个 Pending Future 会被重新唤醒 poll,下次 poll 第一件事就检查到 canceled,返回 Canceled。这是"cancel 能中断 pending 等待"的机制;
   - `Ready(Ok(()))`:service 就绪了。take 走 key/cancel/ready,返回 `Ok((key, svc, cancel_rx))`——三件套交给 poll_pending,后者把它们存进 ready 集;
   - `Ready(Err(e))`:service 死透了。返回 `Err(PendingError::Inner(key, e))`。

那个 `assert!(!cancel.canceled.load(SeqCst), "cancelation cannot be notified while polling a pending service")` 是个 debug 断言——它断言"在 poll 一个 Pending service 的过程中,cancel 状态不可能变化"。注释解释:因为 cancel 的发送端(CancelTx)和接收端(CancelRx)都被 ReadyCache 持有(都在同一个 task 里),而 poll 是同步的(在 poll 期间没有别的代码能跑),所以 poll 期间 cancel 状态不可能被改。如果这个断言失败,说明有并发 bug(比如 CancelTx 被别的线程拿到了)。

#### 14.5.2 Cancel:为什么用 AtomicWaker + AtomicBool,不用 oneshot

cancelable() 的注释专门解释了为什么不用 `tokio::sync::oneshot`(`tower/src/ready_cache/cache.rs#L413-L417`):

```rust
/// Creates a cancelation sender and receiver.
///
/// A `tokio::sync::oneshot` is NOT used, as a `Receiver` is not guaranteed to
/// observe results as soon as a `Sender` fires. Using an `AtomicBool` allows
/// the state to be observed as soon as the cancelation is triggered.
```

这是一个非常 subtle 的点。`tokio::sync::oneshot` 是个单值通道,Sender 发一个值,Receiver 接收。它的实现内部用 `AtomicUsize` 状态机:`Empty` / `Sent` / `Closed` / `ReceiverRegistered` 等状态。当 Sender 调 `send(value)` 时,把值存进 `Some(value)`(用 `Box` 指针),状态从 `Empty` 改成 `Closed`(带值),然后唤醒 Receiver 注册的 waker。Receiver 下次 poll 时,看到状态是 `Closed`,读出值返回。

问题在哪?**Receiver 只有在"下次 poll"时才能读到发送的值**。如果 Sender 已经 send 但 Receiver 还没被 poll,Receiver 内部状态已经是 Closed,但 Pending Future 的 poll 循环没跑到——它要等 waker 触发调度,被重新 poll 时才能看到。在单 task 内(ReadyCache 的场景,CancelTx 和 CancelRx 都在同一个 task 里),这个延迟通常不存在(因为 send 和 poll 在同一个 task 调度循环里),但语义上 oneshot 不保证"立刻可见"。

ReadyCache 的 Pending::poll 需要的是"在 poll 期间立刻检查 cancel 状态"——poll 第一件事 `cancel.canceled.load(SeqCst)` 必须能立刻反映"刚才 evict 调了 cancel"。用 `AtomicBool::load` 是纯原子读,没有任何调度延迟——cancel 调了 `store(true, SeqCst)`,下次任何 load 都立刻看到 true。这是无锁同步的"立刻可见性"。

所以 ReadyCache 自己实现了一个 `Cancel`(`tower/src/ready_cache/cache.rs#L79-L91`):

```rust
#[derive(Debug)]
struct Cancel {
    waker: AtomicWaker,
    canceled: AtomicBool,
}

#[derive(Debug)]
struct CancelRx(Arc<Cancel>);

#[derive(Debug)]
struct CancelTx(Arc<Cancel>);

type CancelPair = (CancelTx, CancelRx);
```

`Cancel` 内部两个字段:

- `waker: AtomicWaker`——来自 `futures-util`,一个可以"原子地注册/唤醒 waker"的工具(承 P2-05 讲过 AtomicWaker 在跨线程唤醒场景的用途)。`register(waker)` 注册当前 waker,`wake()` 唤醒;
- `canceled: AtomicBool`——取消标志。`store(true, SeqCst)` 设置,`load(SeqCst)` 读取。

CancelTx 和 CancelRx 都包一个 `Arc<Cancel>`,共享同一份状态。CancelTx 的 cancel 方法(`tower/src/ready_cache/cache.rs#L426-L431`):

```rust
impl CancelTx {
    fn cancel(self) {
        self.0.canceled.store(true, Ordering::SeqCst);
        self.0.waker.wake();
    }
}
```

`cancel` 做两件事:① 把 canceled 标志设成 true(SeqCst 保证所有线程立刻可见);② 唤醒 Pending Future 注册的 waker(让 PendingFuture 重新 poll,下次 poll 第一件事 load 到 canceled=true,返回 Canceled)。

为什么 Ordering::SeqCst?因为 cancel 是"跨字段"的同步——Pending Future 的 poll 里既要 load canceled,又要 register waker,又要 poll service 的 poll_ready;CancelTx 的 cancel 既要 store canceled,又要 wake。这些操作之间需要严格的内存序保证,SeqCst 是最强的(顺序一致性),虽然性能稍差但绝对正确。在 ReadyCache 这个场景(单 task,CancelTx 和 CancelRx 都在同一 task),Acquire/Release 也够用,但 Tower 选了 SeqCst 保守。

> **钉死件事(为什么不用 oneshot)**:oneshot 的 Receiver "下次 poll 才能看到发送的值",这在多线程异步场景下问题不大(异步本来就有调度延迟),但在 ReadyCache 的 Pending Future 场景里,要"poll 第一件事立刻检查 cancel 状态",必须用立刻可见的同步原语。AtomicBool 的 load 是纯原子读,store 立刻可见,完美契合。这是无锁同步比 channel 更适合"状态广播"场景的典型例子——channel 是"传值",AtomicBool 是"广播状态",语义不同。

### 14.6 ReadyCache 为什么 sound:不泄漏、不双跑、不丢事件

讲完源码,这一节专门回答 soundness——ReadyCache 是个并发数据结构(虽然是单 task 内,但涉及 Future 调度),最容易让人担心的几个点。

#### 14.6.1 不泄漏:service 一定被 drop

ReadyCache 内部的 service 生命周期严格由 ready/pending 两个集合的所有权管理。service 被 drop 的时机:

- **`check_ready_index` 里 service `poll_ready` 返回 `Ready(Err)`**——service 死透,`swap_remove_index` 从 ready 集删,service drop;
- **`poll_pending` 里 Pending 返回 `Err(PendingError::Inner)`**——pending 集里的 service `poll_ready` 失败,被 `FuturesUnordered` 移除,service drop;
- **`evict` 删 ready 集**——`swap_remove_full` 立刻删,service 立刻 drop;
- **`evict` 删 pending 集**——标记 cancel,下次 `poll_pending` 时 Pending 返回 Canceled,被 `FuturesUnordered` 移除,service drop;
- **ReadyCache 自己被 drop**——整个结构 drop,ready 和 pending 都 drop,所有 service drop。

边界情况:evict pending 后没 poll_pending 就 drop ReadyCache。这时 ReadyCache 整个 drop,FuturesUnordered 也 drop,所有 Pending Future drop,Pending 持有的 service(`ready: Option<S>`)也 drop——所以即使 evict 标记了 cancel 但没 poll,service 在 ReadyCache drop 时仍然被正确清理。这是 Rust 所有权保证的。

`Pending` 的 Future 实现里,`key`/`cancel`/`ready` 都是 `Option<T>`,poll 完成时 take 走。如果 Future 还没完成就被 drop(比如 ReadyCache drop),Option 里的值(Some)跟着 drop,资源释放。这是 Future 配合 Option 的标准资源管理(承 P1-02 的 mem::replace + Option 惯用法)。

> **钉死件事(不泄漏)**:ReadyCache 的 service 生命周期完全由 Rust 所有权管理,没有手动 close、没有 Arc 引用计数泄漏。无论走哪条路径(check_ready 失败、poll_pending 失败、evict、ReadyCache drop),service 最终都被 drop,连接被关闭、文件描述符被释放。这是"为什么 sound" 的资源安全保证。

#### 14.6.2 不双跑:同一个 service 不会被并发 poll_ready

并发问题最大的担忧:同一个 service 会不会被两个地方同时 poll_ready(导致 service 内部状态错乱)?ReadyCache 严格保证不会,因为:

1. **ReadyCache 是 `&mut self`**——所有方法(`push`/`poll_pending`/`check_ready`/`call_ready`/`evict`)都要求 `&mut self`,Rust 借用检查保证同一时刻只有一个调用方。这意味着同一时刻只有一个地方在 poll service 的 poll_ready;
2. **service 在任一时刻只在一个集合里**——pending 集或 ready 集,不会同时在两处。`push_pending` 把 service 加进 pending,`poll_pending` 把它从 pending 取出放进 ready,`check_ready_index`/`call_ready_index` 把它从 ready 取出(可能放回 pending)。每次迁移都是先取走(`swap_remove`)再放,中间没有"两个集合都有"的窗口;
3. **Pending Future 是 owned 的**——Pending 持有 service(`ready: Option<S>`),FuturesUnordered 持有 Pending,只有 FuturesUnordered 的 poll_next 能 poll Pending,poll_next 内部是串行的(承 `futures-util` 实现)。

唯一的"看似并发"是 Pending 在 FuturesUnordered 里——但 FuturesUnordered 内部 poll Future 是串行的(它的 poll_next 一次只 poll 一个 Future,或者用某种轮询策略,但绝不同时 poll 两个),所以同一时刻只有一个 Pending 被 poll,只有一个 service 的 poll_ready 被调。

> **钉死件事(单 task 所有权模型)**:ReadyCache 选择"单 task 所有权 + &mut self"而不是"多 task + Mutex",承 P2-05 Buffer 的同一思路——把并发访问收束到一个 task 里,所有修改都是 `&mut self`,Rust 借用检查保证无数据竞争。Balance(P5-15)持有 ReadyCache,所有对 ReadyCache 的访问都在 Balance 的 poll_ready 里(Balance 自己也是 `&mut self`),整个调用链是单 task 的。这避免了 Mutex 串行(承 P2-05 三重罪),又保证了正确性。

#### 14.6.3 不丢事件:Discover 流和 ReadyCache 缓存的同步

Discover 流和 ReadyCache 缓存要保持同步——Discover 产出的每个事件,ReadyCache 都要做出反应。Balance(P5-15)的 poll_ready 里做这件事(`tower/src/balance/p2c/service.rs#L106-L129`):

```rust
/// Polls `discover` for updates, adding new items to `not_ready`.
///
/// Removals may alter the order of either `ready` or `not_ready`.
fn update_pending_from_discover(
    &mut self,
    cx: &mut Context<'_>,
) -> Poll<Option<Result<(), error::Discover>>> {
    debug!("updating from discover");
    loop {
        match ready!(Pin::new(&mut self.discover).poll_discover(cx))
            .transpose()
            .map_err(|e| error::Discover(e.into()))?
        {
            None => return Poll::Ready(None),
            Some(Change::Remove(key)) => {
                trace!("remove");
                self.services.evict(&key);
            }
            Some(Change::Insert(key, svc)) => {
                trace!("insert");
                // If this service already existed in the set, it will be
                // replaced as the new one becomes ready.
                self.services.push(key, svc);
            }
        }
    }
}
```

`update_pending_from_discover` 反复 `poll_discover`,每个 Change 都处理:Insert → `services.push(key, svc)`,Remove → `services.evict(&key)`。这是个 `loop` + `ready!`(`ready!` 宏来自 `futures-core`,把 `Poll::Pending` 提前 return,只保留 `Poll::Ready(x)` 的 x),所以它会一直 poll 直到 Discover 返回 Pending(暂时没新事件)或 None(流结束)。

这个 loop 保证:Discover 产出的事件**全部**被处理,不会丢。每次 Balance 的 poll_ready 第一步就是 `update_pending_from_discover`,把 Discover 累积的事件全部 drain 到 ReadyCache。这承 Stream 的 pull 模型——消费方主动 poll,生产方不会"丢"事件(只要消费方还在 poll)。

边界情况:Discover 返回 Err(发现过程出错,比如 K8s watch 连接断)。Balance 用 `map_err(|e| error::Discover(e.into()))?` 把错误透出——这时 Balance 自己的 poll_ready 返回 Err,调用方知道"服务发现挂了"。但 ReadyCache 里已经缓存的服务**不会丢**——它们还在 ready/pending 集,只是 Discover 暂时不能产出新事件。下次 Discover 恢复(连接重连),继续产出事件,Balance 继续 drain。这是 Discover 错误的"可恢复"语义——错误是临时的,缓存是持久的。

> **钉死件事(不丢事件)**:Discover 流和 ReadyCache 缓存的同步靠 Balance 的 `update_pending_from_discover`——每次 poll_ready 都 drain Discover 的所有事件到 ReadyCache。承 Stream pull 模型,事件不会丢(只要消费方还在 poll)。Discover 错误是可恢复的(缓存还在),不是致命的(整个 Balance 死)。

### 14.7 Discover 与 ReadyCache 的组合:Balance 怎么消费

Discover 和 ReadyCache 单独用得不多——它们是 Balance(P5-15)和 Steer(P5-16)的"上游数据源"。这一节讲 Balance 怎么把 Discover + ReadyCache 组合起来,做完整的"动态发现 + 就绪缓存 + P2C 选择"。

#### 14.7.1 Balance 的结构:Discover + ReadyCache

Balance 的结构体(`tower/src/balance/p2c/service.rs#L29-L42`):

```rust
pub struct Balance<D, Req>
where
    D: Discover,
    D::Key: Hash,
{
    discover: D,

    services: ReadyCache<D::Key, D::Service, Req>,
    ready_index: Option<usize>,

    rng: Box<dyn Rng + Send + Sync>,

    _req: PhantomData<Req>,
}
```

Balance 持有四样东西:

- `discover: D`——Discover 流(数据源);
- `services: ReadyCache<...>`——ReadyCache 缓存(就绪服务集合);
- `ready_index: Option<usize>`——当前 P2C 选中的 service 在 ready 集的下标。None 表示还没选;
- `rng: Box<dyn Rng + Send + Sync>`——随机数生成器(P2C 抽样用)。

这是"数据源 + 缓存 + 选中状态 + 随机源"的标准组合。Discover 提供事件流,ReadyCache 缓存就绪服务,ready_index 记住"上次选了谁"(下次 poll_ready 复用,避免每次都重新 P2C),rng 提供 P2C 的随机性。

#### 14.7.2 Balance::poll_ready 的完整流程

Balance 的 poll_ready(`tower/src/balance/p2c/service.rs#L209-L251`)是 Discover + ReadyCache 组合的全貌:

```rust
fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
    // `ready_index` may have already been set by a prior invocation. These
    // updates cannot disturb the order of existing ready services.
    let _ = self.update_pending_from_discover(cx)?;
    self.promote_pending_to_ready(cx);

    loop {
        // If a service has already been selected, ensure that it is ready.
        if let Some(index) = self.ready_index.take() {
            match self.services.check_ready_index(cx, index) {
                Ok(true) => {
                    self.ready_index = Some(index);
                    return Poll::Ready(Ok(()));
                }
                Ok(false) => {
                    trace!("ready service became unavailable");
                }
                Err(Failed(_, error)) => {
                    debug!(%error, "endpoint failed");
                }
            }
        }

        // Select a new service by comparing two at random and using the
        // lesser-loaded service.
        self.ready_index = self.p2c_ready_index();
        if self.ready_index.is_none() {
            debug_assert_eq!(self.services.ready_len(), 0);
            return Poll::Pending;
        }
    }
}
```

poll_ready 做三件事,顺序非常讲究:

1. **`update_pending_from_discover`**——drain Discover 的事件流,把 Insert/Remove 同步到 ReadyCache;
2. **`promote_pending_to_ready`**——推进 ReadyCache 的 pending 集到 ready 集(调 `poll_pending` loop);
3. **loop**:检查上次选中的 ready_index 还就绪吗(`check_ready_index`),就绪就返回 Ready(Ok),不就绪就 P2C 重新选(`p2c_ready_index`)。

这三步对应 Discover + ReadyCache 的三个层次:**事件同步(Discover → ReadyCache)、就绪推进(pending → ready)、负载均衡选择(P2C)**。每一步都依赖前一步——P2C 要在 ready 集上跑,ready 集要靠 poll_pending 推进,pending 集要靠 update 从 Discover 拿数据。

详细的 P2C 算法和 Balance 的 soundness 分析是 P5-15 的主角,本章只点出组合关系:**Discover 提供事件流,ReadyCache 缓存就绪服务,Balance 在 ready 集上做 P2C 选择**。这三层是 Tower 第 5 篇(路由负载均衡)的核心架构。

> **钉死件事(Balance 是 Discover + ReadyCache 的消费者)**:Balance 不直接持有"后端列表",它持有的是 Discover 流 + ReadyCache 缓存。Discover 流是"动态"的(实时反映后端变化),ReadyCache 缓存是"瞬时投影"(当前哪些后端就绪)。Balance 的所有决策(P2C 选谁)都基于 ReadyCache 的 ready 集,而 ready 集由 Discover 的事件驱动更新。这是"事件驱动 + 状态缓存"的典型架构——事件流是真相源(source of truth),缓存是查询接口(query interface)。

#### 14.7.3 完整时序:Discover Change 流入 + ReadyCache 更新 + Balance 选择

把 Discover、ReadyCache、Balance 的交互画成一张时序图:

```mermaid
sequenceDiagram
    autonumber
    participant Reg as 注册中心<br/>(K8s/Consul)
    participant D as Discover<br/>(TryStream&lt;Item=Change&gt;)
    participant B as Balance
    participant RC as ReadyCache
    participant Svc as 某后端 Service

    Note over D: D 内部 watch 注册中心
    Reg->>D: 推送 add backend A
    Reg->>D: 推送 add backend B
    Note over B: 调用方调 poll_ready
    B->>B: poll_ready
    B->>D: poll_discover (drain 所有事件)
    D-->>B: Ready(Some(Ok(Insert(A, svcA))))
    B->>RC: push(A, svcA) → 进 pending 集
    B->>D: poll_discover
    D-->>B: Ready(Some(Ok(Insert(B, svcB))))
    B->>RC: push(B, svcB) → 进 pending 集
    B->>D: poll_discover
    D-->>B: Pending(暂时没新事件)
    B->>RC: poll_pending (推进 pending)
    RC->>Svc: svcA.poll_ready
    alt svcA Ready(Ok)
        Svc-->>RC: Ready(Ok)
        Note over RC: svcA 迁到 ready 集
    else svcA Pending
        Svc-->>RC: Pending
        Note over RC: svcA 留在 pending 集
    end
    RC->>Svc: svcB.poll_ready
    Svc-->>RC: Ready(Ok)
    Note over RC: svcB 迁到 ready 集
    B->>RC: poll_pending → Ready(Ok) pending 空
    B->>B: p2c_ready_index<br/>在 ready 集随机抽两个比负载
    B-->>B: ready_index = Some(idx)
    B-->>调用方: Ready(Ok) 可以 call 了

    Note over B: 后端 A 下线
    Reg->>D: 推送 remove backend A
    B->>B: 下次 poll_ready
    B->>D: poll_discover
    D-->>B: Ready(Some(Ok(Remove(A))))
    B->>RC: evict(A)
    alt A 在 ready 集
        Note over RC: A 立即从 ready 删除 drop
    else A 在 pending 集
        Note over RC: 标记 cancel<br/>下次 poll_pending 时 drop
    end
    Note over B: ready 集只剩 B<br/>P2C 重新选, ready_index 更新
```

这张图是本章的总图。三个关键瞬间:

- **第 4-10 步(Insert 流入)**:Discover drain 出 Insert 事件,ReadyCache.push 把 service 加进 pending 集——这一步不阻塞(pending 集是异步推进的);
- **第 13-21 步(poll_pending 推进)**:ReadyCache 反复 poll pending 集的 service.poll_ready,谁 ready 谁迁到 ready 集。这一步是异步的(service.poll_ready 可能 Pending);
- **第 26 步后(Remove 流入)**:Discover drain 出 Remove 事件,ReadyCache.evict 删 service。如果 service 在 ready 集,立即 drop;如果在 pending 集,标记 cancel,下次 poll_pending 清理。Balance 的 P2C 在新的 ready 集上重新选。

整张图展示 Discover(事件流)+ ReadyCache(状态缓存)+ Balance(决策)的三层分工,清晰且 sound。

---

## 技巧精解

这一节挑 ReadyCache 两个最硬核的技巧单独拆透:**(一)Pending Future + AtomicWaker/AtomicBool 的 cancellation 机制——为什么不用 oneshot,这个机制怎么实现"cancel 立刻可见"且 sound;(二)pending/ready 双集合 + indexmap 的设计——为什么这样分,为什么 indexmap 是 P2C 的前提,对照朴素实现会撞什么墙**。每个技巧配真实源码 + 反面对比。

### 技巧一:cancellation 用 AtomicWaker + AtomicBool,不用 oneshot

这是 ReadyCache 最 subtle 的技巧,也是最容易看错的地方。先看 cancelable() 的注释(`tower/src/ready_cache/cache.rs#L413-L417`):

```rust
/// Creates a cancelation sender and receiver.
///
/// A `tokio::sync::oneshot` is NOT used, as a `Receiver` is not guaranteed to
/// observe results as soon as a `Sender` fires. Using an `AtomicBool` allows
/// the state to be observed as soon as the cancelation is triggered.
```

#### 为什么 oneshot 不行

`tokio::sync::oneshot` 是个"发送单值"的通道,看起来挺适合做 cancellation——Sender 调 `send(())` 表示"取消",Receiver 调 `poll` 看是否收到 `()`。但 oneshot 有个微妙的问题:**Receiver 只在"下次 poll"时才能看到 Sender 发送的值**。

oneshot 的内部状态机是:`Empty` → `Closed`(有值,等 Receiver 取)→ Receiver poll 时取走。当 Sender 调 `send`,它把值存进 channel,改状态为 `Closed`,唤醒 Receiver 注册的 waker。但这个"唤醒"是异步的——waker 触发后,Receiver 要等调度器调度到它的 task,才能重新 poll,才能读到 `Closed` 状态,才能拿到值。中间有个"调度窗口"。

在多线程异步场景下,这个调度窗口无所谓(异步本来就有调度延迟)。但在 ReadyCache 的 Pending Future 场景里,问题来了——Pending::poll 第一件事是检查 cancel 状态(`tower/src/ready_cache/cache.rs#L441-L449`):

```rust
fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
    let this = self.project();
    // Before checking whether the service is ready, check to see whether
    // readiness has been canceled.
    let CancelRx(cancel) = this.cancel.as_mut().expect("polled after complete");
    if cancel.canceled.load(Ordering::SeqCst) {
        let key = this.key.take().expect("polled after complete");
        return Err(PendingError::Canceled(key)).into();
    }
    // ...
}
```

这一步要求"cancel 状态立刻可见"——如果 evict 调了 cancel,Pending 下次 poll(可能就在 evict 后的同一个 poll_ready 循环里)必须立刻读到 canceled=true。用 oneshot 的话,send 之后 Receiver 还要等调度才能看到,这违背"立刻检查"的设计意图。

更具体的场景:Balance 的 poll_ready 里,先 `update_pending_from_discover`(里面可能 evict),再 `promote_pending_to_ready`(里面 poll_pending,poll Pending Future)。evict 标记 cancel,poll_pending 接着 poll Pending——这两个步骤在同一个 poll_ready 调用里,中间没有 task 调度。Pending poll 时必须立刻看到 evict 设置的 canceled,才能正确返回 Canceled。用 oneshot,这个"立刻可见"不保证。

#### AtomicWaker + AtomicBool 怎么解决

ReadyCache 自己实现的 Cancel 用了两个原子原语(`tower/src/ready_cache/cache.rs#L79-L91`):

```rust
#[derive(Debug)]
struct Cancel {
    waker: AtomicWaker,
    canceled: AtomicBool,
}
```

- `AtomicBool` 提供"立刻可见的状态"——`store(true, SeqCst)` 立刻对所有线程可见,`load(SeqCst)` 立刻读到。Pending::poll 第一件事 `cancel.canceled.load(SeqCst)` 是纯原子读,没有任何调度延迟;
- `AtomicWaker` 提供"原子地注册/唤醒 waker"——Pending 返回 Pending 之前 `cancel.waker.register(cx.waker())` 注册当前 waker,CancelTx.cancel 调 `cancel.waker.wake()` 唤醒。AtomicWaker 来自 `futures-util`,它保证"register 和 wake 的并发安全"(承 P2-05 讲过 AtomicWaker)。

CancelTx.cancel 的实现(`tower/src/ready_cache/cache.rs#L426-L431`):

```rust
impl CancelTx {
    fn cancel(self) {
        self.0.canceled.store(true, Ordering::SeqCst);
        self.0.waker.wake();
    }
}
```

cancel 做两件事:① 设置 canceled 标志(立刻可见);② 唤醒 Pending Future 的 waker(让它重新 poll)。

这两步配合,实现了"cancel 立刻可见 + Pending 会被重新唤醒"的完整语义:

- 如果 Pending 正在被 poll(在 poll_pending 里),它 poll 第一件事 load canceled,看到 true,返回 Canceled;
- 如果 Pending 没在被 poll(在 FuturesUnordered 里等其他 Future),cancel 调 wake,AtomicWaker 唤醒 Pending 所在 task,task 重新 poll FuturesUnordered,FuturesUnordered poll 这个 Pending,Pending 第一件事 load canceled,看到 true,返回 Canceled。

无论哪种情况,cancel 都能"穿透"到 Pending,让它返回 Canceled,从而被 FuturesUnordered 移除。

#### Pending::poll 里的双重检查

Pending::poll 里的 cancel 检查有两处,顺序很讲究(`tower/src/ready_cache/cache.rs#L441-L481`):

```rust
fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
    let this = self.project();
    // 第一处: poll 开始前检查 cancel
    let CancelRx(cancel) = this.cancel.as_mut().expect("polled after complete");
    if cancel.canceled.load(Ordering::SeqCst) {
        let key = this.key.take().expect("polled after complete");
        return Err(PendingError::Canceled(key)).into();
    }

    match this.ready.as_mut().expect("polled after ready").poll_ready(cx) {
        Poll::Pending => {
            // 第二处: poll_ready 返回 Pending 时, 注册 waker + 再次断言
            let CancelRx(cancel) = this.cancel.as_mut().expect("polled after complete");
            cancel.waker.register(cx.waker());
            assert!(
                !cancel.canceled.load(Ordering::SeqCst),
                "cancelation cannot be notified while polling a pending service"
            );
            Poll::Pending
        }
        // ...
    }
}
```

- **第一处(poll 开始前)**:防止"cancel 已经触发但 Pending 还没被 poll"的情况。如果 evict 已经 cancel 了,Pending 这次 poll 立刻返回 Canceled,不再 poll service.poll_ready;
- **第二处(poll_ready 返回 Pending 时)**:防止"poll_ready 期间 cancel 被触发"的窗口。注释解释"cancel 的发送端和接收端都在 ReadyCache(同一 task),poll 期间没有别的代码能跑",所以 poll 期间 cancel 不可能变化。但 register 之后到返回 Pending 之间,如果有 wake 触发(wake 是同步的,会立刻调用 cx.waker().wake()),Pending 会被重新调度 poll——下次 poll 第一处检查会捕获 cancel。这个 assert 是个 invariant 检查,如果它失败说明并发模型被破坏了。

这个双重检查 + register + assert 的组合,是 cancellation 机制 sound 的核心。它保证:

1. cancel 在 poll 前可见(第一处检查);
2. cancel 在 poll 期间不会发生(单 task,assert 保护);
3. cancel 在 Pending 期间能唤醒(register waker,CancelTx.cancel 调 wake);
4. 唤醒后 cancel 在下次 poll 前可见(下次 poll 第一处检查)。

#### 反面对比:如果用 oneshot 会怎样

设想一个用 oneshot 的朴素实现:

```rust
// (反例, 朴素写法, 非源码)
struct NaivePending<K, S> {
    key: K,
    cancel_rx: oneshot::Receiver<()>,
    svc: S,
}

impl<K, S: Service<Req>, Req> Future for NaivePending<K, S, Req> {
    type Output = Result<(K, S), PendingError<K, S::Error>>;

    fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        // 先检查 cancel: poll oneshot
        match Pin::new(&mut self.cancel_rx).poll(cx) {
            Poll::Ready(Ok(())) => return Poll::Ready(Err(PendingError::Canceled(self.key))),  // 取消
            Poll::Ready(Err(_)) => return Poll::Ready(Err(PendingError::Canceled(self.key))),   // sender drop
            Poll::Pending => {}  // 没取消, 继续
        }
        // 再 poll service
        match self.svc.poll_ready(cx) {
            Poll::Ready(Ok(())) => Poll::Ready(Ok((self.key, self.svc))),
            Poll::Pending => Poll::Pending,
            Poll::Ready(Err(e)) => Poll::Ready(Err(PendingError::Inner(self.key, e))),
        }
    }
}
```

这个朴素写法在功能上"看起来对",但有个微妙问题:**oneshot 的 poll 注册了 waker,service 的 poll_ready 也注册了 waker,两个 waker 都要被唤醒,pending 才能被重新 poll**。具体地:

- service.poll_ready Pending 时,它注册了自己的 waker(在 cx 里);
- oneshot.poll Pending 时,它也注册了自己的 waker(在同一个 cx 里)——但 oneshot 内部用的是自己的 waker 存储,会**覆盖** service 注册的 waker(或者反过来,取决于实现顺序)。

结果:只有"service 就绪"或"cancel 触发"中的某一个能唤醒 Pending,另一个被丢失。比如 cancel 先 poll_pending 注册 waker,然后 service.poll_ready 也注册,覆盖了 cancel 的 waker——这时 service 就绪能唤醒 Pending,但 cancel 不能(因为 cancel 的 waker 被覆盖了)。

要解决这个,需要"合并 waker"——把多个唤醒源汇合到一个 waker。AtomicWaker 就是干这个的:Pending 自己持有一个 AtomicWaker,在返回 Pending 前 register 当前的 cx.waker();service.poll_ready 用 cx 的 waker(被 Pending 转发);CancelTx.cancel 调 AtomicWaker.wake() 唤醒。这样无论"service 就绪"还是"cancel 触发"都能唤醒 Pending。

实际上,`futures-util` 有个 `fuse()` 组合子可以合并 Future,但 ReadyCache 选择手写 Pending(而不是用 `select` 或 `fuse`),是为了:

1. **效率**:手写 Future 比 `select`/`fuse` 组合的 Future 更省(没有额外的 enum 包装、状态机);
2. **控制**:手写可以精确控制"先检查 cancel 再 poll service"的顺序,以及 register 的时机;
3. **Ownership**:Pending 持有 service 的所有权,手写能精确管理 take/drop(承 P1-02 的 Option + mem::replace)。

> **钉死件事(为什么不用 oneshot + fuse/select)**:oneshot 的 Receiver 在多 waker 源场景下会覆盖 waker,导致 cancel 或 service 就绪其中一个唤醒丢失。ReadyCache 自己实现 Cancel(AtomicWaker + AtomicBool)+ 手写 Pending Future,精确控制 cancel 检查时机和 waker register,避免 waker 覆盖。这是无锁同步 + 手写状态机的典型应用——通用组合子(select/fuse)不够用时,手写更精确。

### 技巧二:pending/ready 双集合 + indexmap 的设计

第二个硬核技巧是 ReadyCache 的"双集合 + indexmap"数据结构设计。这一节拆为什么这样设计,以及对照朴素实现会撞什么墙。

#### 为什么分 pending/ready 两套集合

前面(14.3.5)提过 pending/ready 分离是为了异步 poll_ready,这里再深入一层——它解决了什么具体问题。

朴素实现(单集合)会撞的墙:

```rust
// (反例, 朴素写法, 非源码)
struct NaiveCache<K, S> {
    services: HashMap<K, S>,   // 单集合
}

impl<K, S: Service<Req>, Req> NaiveCache<K, S, Req> {
    fn push(&mut self, key: K, svc: S) {
        self.services.insert(key, svc);
    }

    fn call_random(&mut self, cx: &mut Context<'_>, req: Req) -> Option<S::Future> {
        let keys: Vec<_> = self.services.keys().collect();
        if keys.is_empty() { return None; }
        let key = keys[rand::random::<usize>() % keys.len()];
        let svc = self.services.get_mut(key)?;
        match svc.poll_ready(cx) {
            Poll::Ready(Ok(())) => Some(svc.call(req)),
            Poll::Pending => None,  // ← 没就绪, 返回 None
            Poll::Ready(Err(_)) => { self.services.remove(key); None }
        }
    }
}
```

这个朴素实现的问题:

1. **每次 call 都要遍历所有 key 收集成 Vec**——`keys().collect()`,O(n)。集群 1000 个后端,每次发请求都 O(1000),性能崩盘;
2. **没就绪的 service 阻塞调用方**——随机选到一个没就绪的 service,返回 None,调用方不知道该 retry 还是 wait。每次 call 都要"赌运气"——可能选 100 次都选到没就绪的;
3. **没法批量推进就绪**——你只能一个一个 poll_ready,不能并发 poll 所有 service 看谁先就绪;
4. **没有"哪些就绪了"的快速查询**——调用方不知道当前有几个就绪的,不知道 P2C 要从多少个里抽。

ReadyCache 的双集合设计解决所有这些问题:

- **pending 集**:存所有"还没就绪"的 service,用 FuturesUnordered 并发 poll,谁先就绪谁迁到 ready;
- **ready 集**:存所有"已就绪"的 service,Balance 直接在 ready 集上 P2C 选择,选中的肯定就绪(可能要 check 一下,因为就绪可能过期,但 check 是 O(1));
- **O(1) 采样**:ready 集用 IndexMap,P2C 按下标随机抽,O(1);
- **批量推进就绪**:poll_pending 一次 poll 整个 pending 集,所有就绪的 service 批量迁到 ready,效率高;
- **快速查询**:`ready_len()` 立刻知道有多少就绪的,P2C 决策有依据。

#### 为什么 ready 集用 IndexMap

前面(14.3.6)讲过 P2C 需要"按下标 O(1) 访问",这里再展开为什么不是别的容器。

考虑四种容器,对照 ReadyCache 的两个核心操作:

| 容器 | 按 key 查/删 | 按下标访问 | 迭代 | 备注 |
|------|------------|----------|------|------|
| `HashMap<K, V>` | O(1) 平均 | 不支持 | 无序 | 不能 P2C 采样 |
| `BTreeMap<K, V>` | O(log n) | O(log n)(中序到第 i) | 有序(按 key) | 采样慢 |
| `Vec<(K, V)>` | O(n)(线性扫) | O(1) | 按插入顺序 | 按 key 删慢 |
| `IndexMap<K, V>` | O(1) 平均 | O(1) | 按插入顺序 | 完美 |

ReadyCache 的两个核心操作:

1. **`evict(key)`(来自 Discover 的 Remove 事件)**:按 key 删,要求 O(1)。`HashMap`/`IndexMap` 满足,`BTreeMap` 是 O(log n),`Vec` 是 O(n);
2. **P2C 的 `get_ready_index(idx)`(随机采样)**:按下标访问,要求 O(1)。`IndexMap`/`Vec` 满足,`BTreeMap` 是 O(log n),`HashMap` 不支持。

只有 `IndexMap` 同时满足两个操作的 O(1)。这是工程上的硬性约束——不是随便选的。

`IndexMap` 的内部结构是"`Vec<K>`(插入顺序的 key 数组)+ `HashMap<K, (usize, V)>`(key 到"在 Vec 里的下标 + value"的映射)"。按 key 查:HashMap 找到 (idx, value),O(1)。按下标访问:Vec 直接索引,O(1)。删除:`swap_remove`(把要删的和最后一个交换,更新 HashMap 里那个被交换的 key 的 idx),O(1)。代价是删除会"扰动顺序"(被交换的元素下标变了),所以文档提醒"poll_pending 和 evict 后,缓存的下标要丢弃"。

#### 反面对比:如果用 HashMap + keys().collect() 会怎样

设想用 HashMap 的朴素实现:

```rust
// (反例, 朴素写法, 非源码)
fn p2c_with_hashmap<R: Rng>(
    rng: &mut R,
    services: &HashMap<K, S>,
) -> Option<&K> {
    if services.is_empty() { return None; }
    if services.len() == 1 { return services.keys().next(); }
    let keys: Vec<_> = services.keys().collect();   // ← O(n) 每次都做
    let aidx = rng.gen_range(0..keys.len());
    let bidx = rng.gen_range(0..keys.len());
    // 假设两个下标不同
    Some(if load(keys[aidx]) <= load(keys[bidx]) { keys[aidx] } else { keys[bidx] })
}
```

这个朴素实现每次 P2C 都 `keys().collect()`,O(n)。集群 1000 个后端,每次发请求都 O(1000),成千上万的 QPS 下,CPU 大量浪费在 collect 上。更糟的是,HashMap 的 keys() 顺序不稳定(取决于内部桶布局),每次 collect 出来的 Vec 顺序可能不同,虽然不影响 P2C 正确性(随机采样不需要稳定顺序),但 collect 本身的 O(n) 是实打实的开销。

ReadyCache 的 IndexMap 解决方案:`get_ready_index(idx)` 是 O(1),P2C 采样 `sample_floyd2(rng, len)` 生成两个随机下标,然后两次 O(1) 的 get_ready_index——总开销 O(1),和集群大小无关。这是"数据结构选型决定性能上限"的典型例子。

#### 双集合 + IndexMap 的协作

把双集合 + IndexMap 合起来看,ReadyCache 的完整数据流:

```text
push(key, svc)     ──→  pending 集 (FuturesUnordered<Pending>)
                          │
                          │ poll_pending
                          │ (并发 poll 所有 Pending.poll_ready)
                          ▼
                       ready 集 (IndexMap<K, (S, CancelPair)>)
                          │
                          │ get_ready_index(idx)  ← P2C 采样
                          │ check_ready_index     ← 发请求前验证
                          │ call_ready_index      ← 发请求
                          ▼
                       (service 用完回 pending 重新 poll_ready)
```

- **pending 集**:用 FuturesUnordered,因为它要"并发 poll 多个 Future",FuturesUnordered 是这个用途的标准容器(承 futures-util);
- **pending_cancel_txs**:用 IndexMap,因为它要"按 key 找 cancel_tx"用于 evict,O(1)。这里其实用 HashMap 也行(不需要按下标,因为 cancel_txs 不被采样),但 Tower 选了 IndexMap 保持一致性(可能也因为 IndexMap 的 swap_remove 比 HashMap 的 remove 在某些场景更友好);
- **ready 集**:用 IndexMap,因为它既要"按 key 操作"(evict 来自 ready 集)又要"按下标采样"(P2C),必须 IndexMap。

这三个数据结构各司其职,FuturesUnordered 管并发 poll,IndexMap 管"按 key + 按下标"双索引。它们组合起来,实现了"高效发现 + 高效采样"的完整功能。

> **钉死件事(双集合 + IndexMap 是工程选型的胜利)**:ReadyCache 的数据结构不是随便选的,每个选择都对应一个具体需求——FuturesUnordered 对应"并发 poll pending",IndexMap 对应"按 key 操作 + 按下标采样双需求",pending/ready 分离对应"异步 poll_ready"。这些选择对照朴素实现(HashMap 单集合、Vec 单集合)的优势在性能和正确性上都明显。这是 Tower 把抽象和数据结构绑定的典型——抽象(Discover 流)决定接口,数据结构(ReadyCache 内部)决定性能。

---

## 章末小结

### 回扣主线

本章属于**组合单元**(Layer/ServiceBuilder 组合那一面)的延伸——虽然 Discover 不是 Layer,ReadyCache 不是 Layer,但它们是"可组合中间件"生态的关键组件。Discover 和 ReadyCache 做的事,本质是把"动态后端列表"建模成可消费的抽象,让上游的中间件(Balance/Steer)能基于动态后端工作:

- **老视角**(静态列表):后端列表是 `Vec`/`HashMap`,启动时定死,运维要扩容缩容得重启客户端;
- **Tower 视角**:后端列表是 `Discover`(一个 TryStream<Item=Change> 流),Discover 流出 Change::Insert/Remove 事件,ReadyCache 缓存就绪服务,Balance 在 ready 集上做负载均衡。整个链路是事件驱动的,实时感知后端变化。

这是"服务发现"这件云原生核心能力,落进 Rust 异步生态的样子。Discover 承 Tokio Stream(后端列表天然是异步变化的流,`poll_next`/`Pin`/`Waker` 都是 Tokio 已拆透的,本章一句带过指路),用 sealed + blanket impl 做成 TryStream 的 trait alias(零开销,承 P4-13 MakeService 同一手法)。ReadyCache 用 pending/ready 双集合 + IndexMap,把"异步就绪 + 高效采样"两个需求都满足。

承接方面:本章的异步流机制(Discover 的 poll_discover 就是 TryStream::try_poll_next)承《Tokio》的 Stream 章节,FuturesUnordered 的并发 poll、AtomicWaker 的跨线程唤醒、Future/Poll/Waker 协作都是《Tokio》讲透的,本章一句带过指路 `[[tokio-source-facts]]`,篇幅全留 Tower 独有(Discover 怎么建模 Change 流、ReadyCache 怎么缓存就绪、cancellation 机制为什么不用 oneshot)。对照 Envoy:Envoy 的 EDS 是完整的服务发现子系统(cluster manager + endpoint manager + health check + outlier detection),《Envoy》P3 已拆透,一句带过指路 `[[envoy-source-facts]]`;Tower 的 Discover/ReadyCache 是最小组件,不做健康检查(交给 Reconnect/调用方)、不做 locality 优先级(交给更高层 LB),只管"事件流 + 就绪缓存"。两者是不同抽象层——Envoy 是完整数据面,Tower 是中间件积木。

### 五个为什么

1. **为什么 Discover 是 TryStream 的 trait alias,而不是新 trait?** 因为后端列表的变化天然是异步流(承 Tokio Stream),不需要重新发明。trait alias + sealed + blanket impl 让任何 `TryStream<Ok = Change>` 自动是 Discover,零运行时开销、零样板代码,还自动获得所有 Stream 组合子(take/filter/map/chain)。这是 Rust 用类型系统表达"语义约束"的典型(承 P4-13 MakeService 同一手法)。

2. **为什么 `Change` 只有 Insert/Remove 两个变体?** 因为这是"服务列表变化"的最小完整抽象。任何更复杂的语义(Modify、Update、HealthCheck)都能用 Insert/Remove 组合表达(Update = Remove + Insert)。最小抽象的好处是消费方(Balance)的逻辑简单——只对两种事件做出反应,不需要处理各种边界。健康检查、就绪判断交给 ReadyCache 内部 poll 和外层 Reconnect,职责清晰。

3. **为什么 ReadyCache 分 pending/ready 两套集合?** 因为 `poll_ready` 是异步的(承 P1-02)。service 刚 push 进来未必就绪,要先放 pending 集异步 poll,就绪了迁到 ready 集。这避免了"插入即就绪"的错误(发请求到没就绪的 service 上)和"插入时阻塞 poll_ready"的错误(阻塞当前 task)。pending/ready 分离让"就绪推进"和"请求分发"解耦,前者异步后台跑,后者基于 ready 集做决策。

4. **为什么 ready 集用 IndexMap 而不是 HashMap/BTreeMap?** 因为 Balance 的 P2C 负载均衡要"按下标随机采样",要求 O(1) 按下标访问;同时 Discover 的 Remove 事件要"按 key 删除",要求 O(1) 按 key 操作。HashMap 不能按下标,BTreeMap 按下标是 O(log n),Vec 按 key 删除是 O(n),只有 IndexMap 的双索引(Vec + HashMap)同时满足两个 O(1)。这是数据结构选型服务于算法需求的典型例子。

5. **为什么 cancellation 用 AtomicWaker + AtomicBool,不用 tokio::sync::oneshot?** 因为 oneshot 的 Receiver"下次 poll 才能看到 Sender 发送的值",有调度窗口;而 Pending Future 要求"poll 第一件事立刻检查 cancel 状态",必须用立刻可见的同步原语。AtomicBool 的 load 是纯原子读,store 立刻可见,完美契合。AtomicWaker 提供跨线程安全的 waker 注册/唤醒,配合 AtomicBool 实现"cancel 立刻可见 + Pending 会被重新唤醒"的完整语义。这是无锁同步比 channel 更适合"状态广播"的典型场景。

### 想继续深入往哪钻

- **源码**:把 `tower/src/discover/{mod,list}.rs` 和 `tower/src/ready_cache/{mod,cache,error}.rs` 四个文件逐行对照本章读一遍。重点看 `discover/mod.rs#L54-L106` 的 Discover trait + Change 枚举 + blanket impl,`ready_cache/cache.rs#L58-L74` 的 ReadyCache 结构,`cache.rs#L281-L315` 的 poll_pending,`cache.rs#L435-L483` 的 Pending::poll(双重 cancel 检查)。
- **Tokio Stream**:本章一句带过的 Stream/TryStream/poll_next/Pin/Waker/FuturesUnordered/AtomicWaker,详见《Tokio》Stream 章节(`[[tokio-source-facts]]`)。理解了 Stream 的 pull 模型、FuturesUnordered 的并发 poll、AtomicWaker 的跨线程唤醒,再看 Discover/ReadyCache 就顺理成章。
- **Envoy EDS**:本章 14.2.3 对照的 Envoy EDS/cluster manager,《Envoy》P3 已拆透(`[[envoy-source-facts]]`)。Envoy 的 endpoint 管理在概念上和 Tower 的 Discover/ReadyCache 同构(订阅 + 缓存 + 就绪),但 Envoy 把它做成完整子系统,Tower 做成最小可组合组件。
- **K8s List+Watch**:本章 14.2.1 对照的 K8s 服务发现(List + Watch 模式),详见 K8s 官方文档或 client-go 实现。生产里 Tower 的 Discover 通常接 K8s/Consul/etcd 的 watch,把地址变化转换成 Change 事件喂进来。
- **Balance/P2C(P5-15)**:本章的 Discover + ReadyCache 是 Balance 的"上游数据源",P5-15 详细拆 Balance 怎么在 ready 集上做 P2C 选择、Load trait 怎么衡量负载、Balance 的 poll_ready 三步流程。本章是 P5-15 的前置,把两者对照读,看完整的服务发现 + 负载均衡栈。
- **Reconnect 配合(P4-13)**:每个 Discover 流出的 service 通常是 Reconnect 包装的(P4-13 讲过),这样单个后端连接死了自动重连。Discover 提供动态后端列表,Reconnect 保证每个后端的连接活性,两者组合才是完整的"动态后端 + 自动重连"方案。

### 引出下一章

Discover 和 ReadyCache 解决了"后端列表怎么动态更新 + 已就绪服务怎么缓存"两件事,但还差最关键的一步——**在多个就绪后端之间,选哪个发请求?**。ReadyCache 的 ready 集里有 N 个就绪 service,P2C 算法要从里头挑一个,挑谁?

这就是下一章 **P5-15 Balance 与 P2C:负载均衡** 要讲的。Balance 消费 Discover 流(P5-14),把就绪 service 维护在 ReadyCache 里,在 ready 集上做 Power-of-2-Choices(P2C)——随机抽两个 service,比较负载(`Load` trait 的 `peak_ewma`/`pending_requests`/`constant` 三种度量),选负载小的。P2C 为什么比 round-robin/随机好?PEWMA(峰值指数加权移动平均)怎么估计后端延迟?Balance 的 `poll_ready` 三步流程(update_pending_from_discover / promote_pending_to_ready / P2C 选择)怎么协作?这些 P5-15 详细拆。从 Discover/ReadyCache(数据源 + 缓存)到 Balance(决策),是从"知道有哪些后端"到"在这些后端之间做选择"的跃迁,这是 Tower 第 5 篇(路由负载均衡)的核心——完整的服务发现 + 负载均衡栈。

---

> **本章源码引用**(tower @ tower-0.5.2, commit `7dc533e`):
> - [discover 模块文档 + Discover trait + Change 枚举 + blanket impl](../tower/tower/src/discover/mod.rs#L1-L106)
> - [ServiceList: Discover 的最简静态实现](../tower/tower/src/discover/list.rs#L12-L53)
> - [ReadyCache 结构(pending/ready 双集合)](../tower/tower/src/ready_cache/cache.rs#L58-L74)
> - [Cancel / CancelTx / CancelRx / CancelPair](../tower/tower/src/ready_cache/cache.rs#L79-L91)
> - [Pending Future 定义](../tower/tower/src/ready_cache/cache.rs#L99-L109)
> - [push / push_pending](../tower/tower/src/ready_cache/cache.rs#L249-L265)
> - [poll_pending(推进 pending 到 ready)](../tower/tower/src/ready_cache/cache.rs#L281-L315)
> - [check_ready_index(就绪可能过期)](../tower/tower/src/ready_cache/cache.rs#L338-L373)
> - [call_ready_index(取走 service 用完回 pending)](../tower/tower/src/ready_cache/cache.rs#L393-L408)
> - [evict(Remove 事件触发删除)](../tower/tower/src/ready_cache/cache.rs#L217-L229)
> - [cancelable + CancelTx::cancel(为什么不用 oneshot)](../tower/tower/src/ready_cache/cache.rs#L413-L431)
> - [Pending::poll(双重 cancel 检查 + AtomicWaker register)](../tower/tower/src/ready_cache/cache.rs#L435-L483)
> - [ready_cache::error::Failed](../tower/tower/src/ready_cache/error.rs#L5-L28)
> - [ready_cache 模块导出](../tower/tower/src/ready_cache/mod.rs#L1-L7)
> - [Balance 怎么消费 Discover(update_pending_from_discover)](../tower/tower/src/balance/p2c/service.rs#L106-L129)
> - [Balance 的 p2c_ready_index(P2C 算法)](../tower/tower/src/balance/p2c/service.rs#L159-L184)
> - [Balance::poll_ready(Discover+ReadyCache+P2C 三步)](../tower/tower/src/balance/p2c/service.rs#L209-L251)
> - [sealed 模块(Sealed trait alias 机制)](../tower/tower/src/lib.rs#L221-L225)
> - [discover/ready_cache 模块声明 + feature flag](../tower/tower/src/lib.rs#L169-L185)
>
> **承接**:
> - 《Tokio》Stream/TryStream/poll_next/Pin/Waker/Context/FuturesUnordered 并发 poll/AtomicWaker 跨线程唤醒/Future 取消 = drop/Stream pull 模型背压——一句带过指路 `[[tokio-source-facts]]`,本章只用 Stream 抽象,不重讲 Tokio 内部;
> - 《Envoy》P3 的 EDS/cluster manager/endpoint manager/outlier detection/active health check——14.2.3 对照点过指路 `[[envoy-source-facts]]`,Envoy 是完整数据面,Tower 是最小组件;
> - P1-02 的 Service trait `&mut self`/poll_ready/call 契约/mem::replace 惯用法/Ready(Err) = 死透——本章 call_ready_index 严格遵循(取走 service 用完回 pending);
> - P2-05 的 Buffer worker + mpsc 单 task 所有权模型(对照 ReadyCache 单 task 所有权)+ AtomicWaker——本章 ReadyCache 选择单 task `&mut self` 而非 `Mutex`,承同一思路;
> - P4-13 的 MakeService(同是 sealed + blanket impl trait alias)+ Reconnect(Discover 流出的每个 service 通常是 Reconnect 包装的);
> - P5-15 的 Balance/P2C(本章的下游消费者,详细 P2C 算法和 Load trait 在 P5-15)。
>
> **本章源码印象修正**(写时核实并明确的、易被老资料带偏的事实):
> - **Discover 不是新 trait,是 `TryStream<Ok = Change>` 的 sealed trait alias**。`poll_discover` 直接调 `TryStream::try_poll_next`,零开销。老资料如果说"实现 Discover trait"是不准确的——你只能写一个 `TryStream<Ok = Change>`,它自动获得 Discover(blanket impl)。这承 P4-13 的 MakeService 同一手法。
> - **`Change` 只有 Insert(K, V)/Remove(K) 两个变体,没有 Modify**。Update 语义靠 Remove + Insert 实现。K 必须 `Eq`(Discover trait 的关联类型约束),用于增删匹配。
> - **ReadyCache 是 pending/ready 双集合**,不是单一 HashMap。pending 集用 `FuturesUnordered<Pending>`,ready 集用 `IndexMap<K, (S, CancelPair)>`。这是为异步 poll_ready(承 P1-02)+ P2C 按下标采样(承 P5-15)的双重需求。
> - **ready 集用 `indexmap::IndexMap`,不是 `HashMap`/`BTreeMap`/`Vec`**。原因是 P2C 要 O(1) 按下标采样 + Discover Remove 要 O(1) 按 key 删除,只有 IndexMap 的双索引(Vec + HashMap)同时满足。这是数据结构选型服务于算法的硬约束。
> - **cancellation 用 `AtomicWaker + AtomicBool`,不用 `tokio::sync::oneshot`**。原因注释专门写明:oneshot 的 Receiver"下次 poll 才能看到 Sender 发送的值",有调度窗口;AtomicBool 的 load/store 立刻可见,完美契合"Pending::poll 第一件事立刻检查 cancel"。这是无锁同步比 channel 更适合"状态广播"的典型场景。
> - **Pending::poll 有双重 cancel 检查**(poll 开始前 + poll_ready 返回 Pending 时),配合 `assert!(cancelation cannot be notified while polling a pending service)` 断言。这是 cancellation 机制 sound 的核心——保证 cancel 在 poll 前、poll 期间(单 task 不可能)、Pending 期间(AtomicWaker 唤醒)都能被捕获。
> - **`call_ready_index` 严格遵循 Service trait 的"取走就绪状态"惯用法**:call 前在 ready 集(check 过),call 取走 service 所有权(`swap_remove_index`),call 后 service 回 pending 集(重新 poll_ready)。这承 P1-02 的 `&mut self` + mem::replace,P2-05 Buffer worker 的 poll_ready + call + 重新 poll_ready 循环。
> - **`evict` 对 pending 集是"标记取消",不是"立即删除"**。pending 集的 service 真正 drop 要等下次 `poll_pending`(Pending 返回 Canceled 被 FuturesUnordered 移除)。ready 集的 evict 是立即删除(`swap_remove_full`)。这是 FuturesUnordered 不支持按 key 删除的限制导致的设计。
> - **没有废弃的 `tower-discover` / `tower-ready-cache` crate**。自 0.4.0 起所有独立子 crate 已合并进 `tower`(承总纲第五节),真实路径是 `tower/src/discover/`、`tower/src/ready_cache/`(feature flag 分别是 `discover` 和 `ready-cache`)。老博客里的 `tower-discover`/`tower-ready-cache` crate 名已过时。
> - **ServiceList 的 key 类型是 `usize`(迭代器 enumerate 的索引),错误类型是 `Infallible`**(静态列表不会失败)。ServiceList 只产 Insert 不产 Remove(静态列表不会变)。它是 Discover 的最简参考实现,主要用于测试和静态后端场景。
