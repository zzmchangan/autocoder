# 第 11 章 · Retry:失败重试与 Policy

> 第 4 篇 · 韧性类中间件 · 执行单元(招牌章)

---

## 章首 · 核心问题

这一章只回答一个问题:

> **请求失败了,要不要重试?要重试几次?怎么避免"重试本身"把已经过载的下游彻底打死?**

这听起来像一个最朴素的工程问题——你打开浏览器加载一个网页,转圈转不出来,你会刷新一下,再不行刷新两下,最后骂一句放弃。重试这件事在日常生活里就是这么自然。可一旦把它放到一个**分布式系统**里,放到一个**每秒几万请求**的服务上,放到一个**上下游还各自带着重试**的链路里,它就立刻变成最危险的几个机制之一。

分布式系统里有一条几乎被所有 SRE 写进事故复盘的教训:**重试风暴(retry storm)是自找的拒绝服务攻击**。一个后端因为某种原因(机器抖动、GC 停顿、网络毛刺、依赖变慢)开始返回少量错误,客户端发现错误,启动重试。本来 100 个请求里只有 5 个失败,客户端给这 5 个重试一次,后端就要承受 105 个请求——可这额外的 5 个请求**让本就在挣扎的后端更慢**,失败率从 5% 涨到 15%,客户端重试的请求更多,后端更慢,失败率涨到 40%——几分钟之内,一个本来只是"轻微毛刺"的事件,被放大成了一次**全后端雪崩**。如果这个客户端自己也有上游、上游也配了重试,那么"重试 × 重试 × 重试"会以**乘法**而不是加法放大流量,Google SRE 那本名著把这种情形叫做"cascading failure"。

所以"重试"从来不是简单的 `for i in 0..3 { try_call() }`。它要在两件相反的事情之间找平衡:

1. **该重试的要重试**——网络抖动、临时故障、连接重置这类瞬态错误(transient error),重试一次通常就成功了,不重试等于把可以救活的请求白白丢给用户;
2. **不该重试的绝不能重试**——下游已经过载、请求本身不幂等、错误是业务级的(404、403),重试只会让事情更糟。

而 Tower 给出的答案,是把"该不该重试"这件事拆成**三个正交的决策点**,每个点都是一个可以独立替换的 trait:

- **`Policy` trait**:决定**单次**失败后要不要重试(以及如果要,等多久再试)。这是"该不该"这一面。
- **`Budget` trait**:决定**整个服务在一段时间窗口内**还能不能消耗一次重试的额度。这是"重试总量"这一面,防风暴的真正闸门。
- **`Backoff` trait**:决定**两次重试之间**怎么等待(指数退避 + 抖动)。这是"避免同步惊群"这一面。

这三个 trait 合起来,才是 Tower 对"请求失败了怎么办"的完整回答。其中 **0.5.0 把 `Budget` 从一个写死的结构体重构成一个 trait**,是这一章最值得拆的重大演进——它让用户可以把自己的预算策略(比如基于令牌桶、基于 TPS、基于熔断器状态)塞进 Retry,而不再被绑死在 Tower 内置的那一种实现上。

读完本章你会明白:

1. **为什么"固定重试 N 次"在分布式系统里是危险的**——它会以乘法放大流量,把毛刺放大成雪崩;Tower 用 `Budget`(重试预算)替代"最大重试次数",用一个会随时间过期的令牌桶把重试总量**和实际成功率挂钩**。
2. **`Policy` trait 的三个方法各管什么**——`retry(&mut self, req, result)` 决定单次要不要重试,`clone_request(&mut self, req)` 决定请求能不能被克隆以备重试,`Policy::Future` 是"重试前的等待 Future"(可以塞一个 `tokio::time::Sleep` 做退避)。★0.5.0 把 `retry` 从 `&self, &Req, Result<&Res, &E>` 改成 `&mut self, &mut Req, &mut Result<Res, E>`,让 policy 可以**改写**请求(比如塞一个 `x-retry-count` header)和**改写**结果(比如把"重试耗尽"翻译成特定错误)。
3. **`Budget` trait 0.5.0 重构的来龙去脉**——重构前 `Budget` 是一个具体的结构体,`withdraw` 返回 `Result<(), Overdrawn>`;重构后 `Budget` 是一个 trait(只两个方法 `deposit(&self)` / `withdraw(&self) -> bool`),内置实现改名 `TpsBudget`,用户可以实现自己的预算。这是 breaking change 但换来了"任意预算策略可插拔"。
4. **`Retry` 的 `call` 为什么"可能多次调用内层"**——它返回的 `ResponseFuture` 是一个三态状态机(`Called` → `Waiting` → `Retrying` → `Called` ...),在内层 Future resolve 出失败后,会**重新** `poll_ready` + `call` 内层 service,直到成功、Policy 拒绝重试、或 Budget 用完。
5. **为什么 `Retry` 要求内层 service 必须 `Clone`**——因为每次重试都要 `call` 一次,而 `call(&mut self)` 取走了 service 的就绪状态,重试时必须从一个新的 clone 上重新 `poll_ready`。这正好是 P2-05(Buffer)解决的那个问题的反面——`!Clone` 的 service 想用 Retry,得先套一层 Buffer 把它变成 `Clone`。

> **逃生阀**(这章概念点多,先读这一段)。
>
> 如果你只关心一句话:**Retry = Policy(决定单次要不要重试)+ Budget(限总重试量,防风暴)+ Backoff(两次重试之间指数退避 + 抖动)**。Policy 管"该不该",Budget 管"还能不能",Backoff 管"什么时候"。三者拼起来,既把该救的请求救活,又把不该重试的流量拦住。如果你想跳过最硬核的源码,看第 1、2、3 节(动机 + Envoy 对照 + Policy)+ 第 6 节(Budget 防风暴)+ 技巧精解即可;第 4、5 节是源码逐行拆 `ResponseFuture` 状态机,留给想真正读懂 retry 源码的人。
>
> 本章假设你读过 P1-02(`Service` trait 的 `&mut self` 与 `poll_ready` 背压)、P2-05(Buffer,理解 `!Clone` service 怎么共享)、P3-08/09/10(Timeout/ConcurrencyLimit/RateLimit,理解 Tower 中间件怎么用 `tokio::time`/`Semaphore`/令牌桶)。涉及 `tokio::time::Sleep` 内部机制,一句带过指路 [[tokio-source-facts]]。

## 章首 · 一句话点破

> **重试这件事的危险在于它是"放大器":本来的小故障被重试放大成大故障。Tower 的解法不是"重试 N 次",而是把"该不该重试"(Policy)、"还能不能重试"(Budget)、"什么时候重试"(Backoff)三件事拆成正交的三个 trait——Policy 基于单次结果做判断,Budget 基于一段时间的重试总量做闸门(防风暴),Backoff 用指数退避 + 抖动避免同步惊群。一次 `call` 返回的 Future 内部可能反复 `poll_ready + call` 内层,直到成功、Policy 停、或 Budget 耗尽。**

这是结论,不是理由。本章倒过来拆:先看朴素重试为什么会放大流量,再看 Envoy 的 retry policy 怎么做(横向对照),然后逐个拆 Tower 的三个 trait,最后落到 `ResponseFuture` 状态机的源码上,把"一次 call 多次执行内层"这件事钉死。

---

## 正文

### 11.1 重试的两面性:为什么这件事既必要又危险

#### 11.1.1 瞬态错误是真实存在的,重试能救活请求

先把"重试为什么必要"讲透,不然读者会以为这章只是讲"防风暴"。

分布式系统里,一个请求失败的**原因**可以粗略分成两类:

- **瞬态错误(transient error)**:网络毛刺、TCP 连接被 RST、瞬时丢包、对端 GC 停顿、对端刚重启还没就绪、临时限流(429)。这类错误的特征是:**马上再试一次,大概率成功**。一个典型的数字:Google SRE 书里给过经验,绝大多数瞬态错误在第一次重试时就恢复,重试 1 次能把成功率从 99% 提到 99.9%。
- **持久错误(permanent error)**:请求参数不合法(400)、没权限(403)、资源不存在(404)、下游真的挂了(503 持续)、请求不幂等且已经部分写入。这类错误重试**毫无意义**,甚至有害(比如不幂等的 POST 请求重试一次等于提交两次)。

> **钉死这件事**:**重试是只对瞬态错误有效的**。一个设计良好的重试机制,第一道闸门就是"区分错误类型"——`Policy::retry` 看到的是 `Result<Res, E>`,它的工作就是**判断这个错误是不是瞬态的**,瞬态就重试,持久就放弃。

对瞬态错误重试,收益是巨大的:它把"本来该失败"的请求救回来,直接提升了用户体验和 SLO。这就是为什么几乎所有 RPC 框架(gRPC、Envoy、Finagle、Dubbo)、几乎所有 HTTP client(reqwest、Go 的 net/http)、几乎所有数据库 client(JDBC、psycopg)都内置了重试。

#### 11.1.2 但盲目重试会放大流量,把毛刺放大成雪崩

可是——重试的本质是**多发请求**。多发请求意味着**给本就在挣扎的下游更多压力**。这就引出了分布式系统里最经典的事故模式之一:**重试风暴**。

`tower/src/retry/budget/mod.rs` 的模块文档([source](../tower/tower/src/retry/budget/mod.rs#L1-L25))把这件事讲得非常清楚,这段文档本身就是一篇小论文,值得逐句看:

> Systems configured this way are vulnerable to retry storms. A retry storm begins when one service starts to experience a larger than normal failure rate. This causes its clients to retry those failed requests. The extra load from the retries causes the service to slow down further and fail more requests, triggering more retries. If each client is configured to retry up to 3 times, **this can quadruple the number of requests being sent!** To make matters even worse, if any of the clients' clients are configured with retries, the number of retries **compounds multiplicatively** and can turn a small number of errors into a self-inflicted denial of service attack.

翻译过来就是:假设每个客户端配了"最多重试 3 次",那么最坏情况下,一次失败的请求会被放大成 4 次发送(原始 1 次 + 重试 3 次)。如果这个客户端自己也有上游、上游也配了重试,那么"4 × 4 = 16"——一个请求在最坏情况下能放大成 16 次发送。再往上串一层,64 次。这就是为什么 SRE 圈有一句行话:**"重试风暴不是网络问题,是配置问题"**。

更阴险的是,这种放大是**自我正反馈**的:

```
下游轻微故障 → 失败率 5% → 客户端重试 → 流量 +20%
            → 下游更慢 → 失败率 15% → 客户端重试更多 → 流量 +60%
            → 下游雪崩 → 失败率 50% → 全员重试 → 流量 ×3
            → 下游彻底打挂 → 100% 失败 → 客户端把重试次数打满后放弃
```

这条链一旦启动,几分钟之内就能把一个本来"扛得住"的服务打成"完全不可用"。Twitter、GitHub、AWS 都出过这种事故。

#### 11.1.3 所以"重试 N 次"这个配置本身就是问题

`budget/mod.rs` 文档紧接着点出了"最多重试 N 次"这种配置的两个根本问题([source](../tower/tower/src/retry/budget/mod.rs#L8-L25)):

> **Choosing the maximum number of retry attempts is a guessing game.** You need to pick a number that's high enough to make a difference when things are somewhat failing, but not so high that it generates extra load on the system when it's really failing. In practice, you usually pick a maximum retry attempts number out of a hat (e.g. 3) and hope for the best.

第一,"最多重试 N 次"这个 N 是**拍脑袋拍的**。N 太小,救不回该救的请求;N 太大,出事的时候放大效应更猛。3 这个数字几乎是个行业默认值,但几乎没人能说清为什么是 3 而不是 2 或 5。

> **Systems configured this way are vulnerable to retry storms.** ...

第二,也是更要命的:**"最多重试 N 次"这个机制本身,在"系统真出事"的时候会把事情变得更糟**。因为它是**基于单次请求**的判断——每个请求独立看自己的失败次数,看不到全局。当系统整体出事时,每个请求都独立地把自己的 N 次重试打满,没有任何"全局刹车"机制。

> **所以这样设计**:Tower 不用"最多重试 N 次"这个机制,改用一个**会随时间过期、和成功率挂钩的令牌桶**(Budget)。Budget 的核心思想是:**重试的额度不是凭空给的,而是从成功的请求里"赚"来的**——每个成功请求 `deposit` 一点额度,每个重试请求 `withdraw` 一点额度,额度耗尽就停止重试。这样,当系统健康(成功率高)时,Budget 里攒了足够多的额度,可以放手重试;当系统出事(成功率低)时,Budget 很快被 `withdraw` 光,后续的重试请求被自动拦下,**重试量自然收敛**。这就是 Budget 防风暴的本质——它让重试总量和系统健康度**负反馈**挂钩,而不是和失败次数正反馈挂钩。

这是这一章最核心的一句话,值得反复回扣。第 6 节会把 `TpsBudget` 的源码逐行拆,看这个"负反馈"是怎么用一个 10 槽位的滑动窗口令牌桶实现的。

---

### 11.2 横向对照:Envoy 的 retry policy 怎么做

在讲 Tower 之前,先看看"工业级服务网格"是怎么做重试的,这样对照之下能看清 Tower 的取舍。**承接《Envoy》[[envoy-source-facts]]**:Envoy 的 retry 在 HCM(HTTP Connection Manager)的 `retry_policy` 里配,典型配置长这样(简化示意):

```yaml
# (示意,Envoy 配置,非 Tower 源码)
retry_policy:
  retry_on: "5xx,gateway-error,connect-failure,refused-stream,reset"
  num_retries: 3
  per_try_timeout: 250ms
  retry_back_off:
    base_interval: 25ms
    max_interval: 250ms
  retry_host_predicate:
    - name: envoy.retry_host_predicates.previous_hosts
```

可以看到 Envoy 的重试策略由几个旋钮组成:

- **`retry_on`**:一个逗号分隔的字符串,列举**哪些错误条件**触发重试(5xx、gateway-error、connect-failure、refused-stream、reset)。这就是 Envoy 版的"Policy 决策",但是**配置驱动**的(写在 YAML 里),不是 trait。
- **`num_retries`**:最多重试次数。这就是前面说的"拍脑袋 3"。
- **`per_try_timeout`**:每次尝试的超时。
- **`retry_back_off`**:退避策略,指数 + 上限。
- **`retry_host_predicate`**:重试时排除上一次试过的主机(避免又把请求发到刚失败的那台)。

> **对照《Envoy》**:Envoy 的 retry policy 是**配置驱动、运行期生效**的——你写 YAML,Envoy 在运行期根据 YAML 决定重试行为。这适合服务网格场景(运维动态调整),但灵活性受限于 Envoy 内置的那些条件。Tower 走的是**代码驱动、trait 抽象**的路——你写一个 `impl Policy` 的 Rust 类型,在编译期就确定重试逻辑,可以写任意复杂的判断(看请求体、查外部状态、调一个 Consul 接口判断要不要重试)。这是"零成本抽象 + 编译期单态化"(Tower)和"运行期灵活 + 配置驱动"(Envoy)的典型取舍差异,和本书 P1-03(Layer 洋葱 vs Envoy filter chain)那个对照是同构的。Envoy 的 overload manager 用 LoadShedPoint 防过载(对照本书 P2-07 LoadShed),但 Envoy 的 retry 本身**没有 Budget 这种"和成功率挂钩的负反馈"**——它的防风暴主要靠 `num_retries` + circuit breaker(熔断器)外部配合。

> **钉死这件事**:Envoy 的重试防风暴是"重试次数上限 + 熔断器"两件**正交**的事拼出来的;Tower 的 Retry 把"重试次数决策"和"重试总量限制"**都内置**到中间件里了,前者用 `Policy`,后者用 `Budget`。换句话说,Tower 的 Budget 在某种意义上**把熔断器的"限重试"那一面融进了重试中间件本身**——你不需要再外挂一个熔断器,只要 Policy 配合 Budget,就能实现"健康时放手重试、出事时自动收敛"。

Envoy 还有 `retry_host_predicate` 这种"重试时换主机"的能力,这对应到 Tower 里要配合 P5-15(Balance/P2C 负载均衡)一起用——Retry 内层套 Balance,每次重试 Balance 会重新 P2C 选一个后端,自然就避开了刚才失败的那台。这是第 5 篇会展开的话题,本章先记住"Retry + Balance 是天生一对"。

---

### 11.3 所以 Tower 这么设计:三个正交的 trait

把上面两节的结论合起来,Tower 的 Retry 设计可以这样概括:

> **Retry = Policy(该不该重试,基于单次结果)+ Budget(还能不能重试,基于一段时间窗口的重试总量)+ Backoff(什么时候重试,指数退避 + 抖动)。三者正交,各管一面,组合起来既救得活该救的请求,又拦得住不该重试的流量。**

值得强调的是,**Policy 和 Budget 是两层独立的闸门**。一个重试请求要真正发出去,必须**同时**通过这两道闸门:

- Policy 说"这个错误是瞬态的,可以重试"——基于**这一次**的请求和结果;
- Budget 说"我们这段时间的重试额度还没用完"——基于**最近一段时间的全局**重试量。

任意一道闸门说"不",重试就停止,把错误返回给调用方。这两道闸门缺一不可:

- 只有 Policy 没有 Budget:就是"重试 N 次",前面讲过,出事时放大流量;
- 只有 Budget 没有 Policy:不知道哪些错误该重试,会对 404、403 这种永久错误也消耗额度重试,既浪费 Budget 又没用。

`Policy` 是必选的(Retry 的泛型参数 `P: Policy<...>` 是强制的),`Budget` 是可选的——Tower **没有**强制要求 Policy 内部必须用 Budget,你可以写一个只用 Policy、不用 Budget 的简单重试(像 `tower/src/retry/policy.rs` 文档示例的 `Attempts(usize)` 那样)。但**生产环境的重试必须有 Budget**,这是这一章反复强调的工程原则。Budget 是 policy **内部**持有的一个东西(`Arc<TpsBudget>` 作为 policy 结构体的字段),Policy 在 `retry` 方法里调 `budget.withdraw()`。

至于 Backoff,它是 Policy 的"等待策略"——`Policy::retry` 返回的不是一个简单的 bool,而是一个 `Option<Self::Future>`,这个 Future 就是"重试前要 await 的等待"。如果你想让重试之间有指数退避,就把这个 Future 设成一个 `tokio::time::Sleep`;如果你想让重试立刻执行(比如 `Attempts` 那个例子),就返回 `future::ready(())`。

下面三节分别拆这三个 trait,最后落到 `ResponseFuture` 状态机上看它们怎么串起来。

---

### 11.4 `Policy` trait:决定单次要不要重试

#### 11.4.1 trait 定义

`Policy` trait 的全部定义在 `tower/src/retry/policy.rs`([source](../tower/tower/src/retry/policy.rs#L46-L90)),就两个方法加一个关联类型:

```rust
// tower/src/retry/policy.rs#L46-L90
pub trait Policy<Req, Res, E> {
    /// The [`Future`] type returned by [`Policy::retry`].
    type Future: Future<Output = ()>;

    /// Check the policy if a certain request should be retried.
    ///
    /// This method is passed a reference to the original request, and either
    /// the [`Service::Response`] or [`Service::Error`] from the inner service.
    ///
    /// If the request should **not** be retried, return `None`.
    ///
    /// If the request *should* be retried, return `Some` future that will delay
    /// the next retry of the request. This can be used to sleep for a certain
    /// duration, to wait for some external condition to be met before retrying,
    /// or resolve right away, if the request should be retried immediately.
    ///
    /// ## Mutating Requests
    ///
    /// The policy MAY chose to mutate the `req`: if the request is mutated, the
    /// mutated request will be sent to the inner service in the next retry.
    /// This can be helpful for use cases like tracking the retry count in a
    /// header.
    ///
    /// ## Mutating Results
    ///
    /// The policy MAY chose to mutate the result. This enables the retry
    /// policy to convert a failure into a success and vice versa. ...
    fn retry(&mut self, req: &mut Req, result: &mut Result<Res, E>) -> Option<Self::Future>;

    /// Tries to clone a request before being passed to the inner service.
    ///
    /// If the request cannot be cloned, return [`None`]. Moreover, the retry
    /// function will not be called if the [`None`] is returned.
    fn clone_request(&mut self, req: &Req) -> Option<Req>;
}
```

逐个看:

**`retry(&mut self, req: &mut Req, result: &mut Result<Res, E>) -> Option<Self::Future>`** 是核心。它接收:

- `&mut self`:policy 自己,可以更新内部状态(比如剩余重试次数);
- `&mut Req`:这次失败的请求,**可变引用**——这是 0.5.0 的 breaking change,policy 可以修改请求;
- `&mut Result<Res, E>`:这次的结果(成功或失败),**可变引用**——policy 也可以修改结果(比如把"重试耗尽"翻译成特定错误);

返回 `Option<Self::Future>`:

- `None`:不重试,把当前结果返回给调用方;
- `Some(future)`:重试,但先 await 这个 future(通常是一个 `Sleep` 做退避,或 `ready(())` 立刻重试)。

注意 `result` 是 `&mut Result<Res, E>` 而不是 `Result<&Res, &E>`。这看起来是个小细节,其实是个关键设计——它让 policy 能**改写**结果。文档里给的例子([source](../tower/tower/src/retry/policy.rs#L69-L79)):

> The policy MAY chose to mutate the result. This enables the retry policy to convert a failure into a success and vice versa. For example, if the policy is used to poll while waiting for a state change, the policy can switch the result to emit a specific error when retries are exhausted.

这个能力用于"重试耗尽时,把错误翻译成另一种错误"。比如你定义一个 `ServiceUnavailable` 错误,正常失败返回的是 `Timeout`,但重试耗尽后你想把它翻译成 `ServiceUnavailable` 让上游更容易判断——policy 在最后一次 `retry` 返回 `None` 之前,把 `result` 改成 `Err(ServiceUnavailable)`。

**`clone_request(&mut self, req: &Req) -> Option<Req>`** 是另一个关键方法。它的作用是:**在请求第一次发给内层 service 之前**,先克隆一份留着,以备重试。如果返回 `None`,表示"这个请求不能克隆,所以无法重试",那么后续即便失败了 `retry` 方法也**不会被调用**——这是 `ResponseFuture` 状态机里一个重要的短路逻辑(见第 5 节源码)。这个设计的妙处在于:**重试不是无条件的**,有些请求(比如持有独占资源的 `&mut` 引用、流式 body)根本无法克隆,这时 Retry 自动降级成"不重试",而不是编译失败。这给了用户**运行期**决定"这个请求要不要支持重试"的能力。

**`type Future: Future<Output = ()>`** 是关联类型。它不要求 `Send`/`'static`,这些约束留给调用方按需加(和 `Service::Future` 一样的策略)。`Output = ()` 是因为它的语义是"等待",不返回任何值——等待结束就重试。

#### 11.4.2 ★0.5.0 的两个 breaking change:为什么 `retry` 改成 `&mut`

这是这一章的重点演进,值得单独讲。

`tower/CHANGELOG.md` 0.5.0 条目([source](../tower/tower/CHANGELOG.md#L33-L35))写得很清楚:

```
- **retry**: **Breaking Change** `retry::Policy::retry` now accepts `&mut Req` and
  `&mut Res` instead of the previous mutable versions. This increases the
  flexibility of the retry policy. To update, update your method signature to
  include `mut` for both parameters. ([#584])
- **retry**: **Breaking Change** Change Policy to accept &mut self ([#681])
```

往前翻 git 历史,看 0.5.0 之前的 `Policy::retry` 签名:

```rust
// 0.5.0 之前(简化示意,基于 git show 19c1a1d^:tower/src/retry/policy.rs)
pub trait Policy<Req, Res, E>: Sized {
    fn retry(&self, req: &Req, result: Result<&Res, &E>) -> Option<Self::Future>;
    fn clone_request(&self, req: &Req) -> Option<Req>;
}
```

对比 0.5.0 之后:

```rust
// 0.5.0 之后(简化示意)
pub trait Policy<Req, Res, E> {
    fn retry(&mut self, req: &mut Req, result: &mut Result<Res, E>) -> Option<Self::Future>;
    fn clone_request(&mut self, req: &Req) -> Option<Req>;
}
```

注意三处变化:

1. `&self` → `&mut self`;
2. `&Req` → `&mut Req`;
3. `Result<&Res, &E>` → `&mut Result<Res, E>`。

为什么要做这些 breaking change?**因为旧签名让 policy 写不出几个真实的需求**。

**理由一:policy 需要改写请求**。最常见的需求是"在重试的请求里塞一个 `x-retry-count: N` header",让下游知道这是第几次重试,方便排查。旧签名 `&Req` 是不可变引用,policy 改不了请求,这个需求做不到。新签名 `&mut Req` 让 policy 可以原地改请求(注意——这里的"改"是在 `clone_request` 克隆出来的副本上改,不是改原始请求)。

**理由二:policy 需要改写结果**。前面讲的"重试耗尽时把错误翻译成 `ServiceUnavailable`",旧签名 `Result<&Res, &E>` 是只读的,policy 改不了结果。新签名 `&mut Result<Res, E>` 让 policy 可以替换结果。

**理由三:policy 需要内部状态**。比如"指数退避"policy,每次重试要把"当前退避时间"翻倍,这需要 `&mut self`。旧签名 `&self` 让 policy 只能用 `Cell`/`Mutex` 这种内部可变性,既丑又有性能开销。新签名 `&mut self` 让 policy 可以直接持有可变状态。

> **不这样会怎样**:0.5.0 之前,如果你想写一个"重试时塞 header + 指数退避 + 重试耗尽翻译错误"的 policy,你做不到——你得在 policy 内部塞一个 `RefCell<Option<Req>>` 来"绕过"借用检查,丑且容易错。0.5.0 的 breaking change 把这三个 `&mut` 加上,让 policy 可以直接、高效、安全地修改这三样东西。代价是所有现有的 `impl Policy` 都要改签名(加 `mut`),这是一次"值得的"breaking change。

#### 11.4.3 一个完整的 Policy 示例:带 Budget 和退避

把 `Policy` 和 `Budget`、`Backoff` 拼起来,看一个真实可用的 policy 长什么样。这正是 `tower/src/retry/budget/mod.rs` 文档示例的扩展版本([source](../tower/tower/src/retry/budget/mod.rs#L28-L72)):

```rust
// (来自 budget/mod.rs 文档示例,简化)
use std::sync::Arc;
use futures_util::future;
use tower::retry::{budget::{Budget, TpsBudget}, Policy};

type Req = String;
type Res = String;

#[derive(Clone, Debug)]
struct RetryPolicy {
    budget: Arc<TpsBudget>,   // 共享预算
}

impl<E> Policy<Req, Res, E> for RetryPolicy {
    type Future = future::Ready<()>;   // 不等待,立刻重试(简化;真实用 Sleep 退避)

    fn retry(&mut self, req: &mut Req, result: &mut Result<Res, E>) -> Option<Self::Future> {
        match result {
            Ok(_) => {
                // 成功:deposit 一点预算(给未来的重试攒额度),不重试
                self.budget.deposit();
                None
            }
            Err(_) => {
                // 失败:withdraw 预算,余额不足就不重试
                let withdrew = self.budget.withdraw();
                if !withdrew {
                    return None;   // 预算耗尽,放弃重试(防风暴的关键!)
                }
                // 预算够,重试
                Some(future::ready(()))
            }
        }
    }

    fn clone_request(&mut self, req: &Req) -> Option<Req> {
        Some(req.clone())
    }
}
```

这个示例里有几个细节值得注意:

- `Arc<TpsBudget>`:Budget 用 `Arc` 共享——一个 service 的所有请求**共用同一个 Budget**,这样 Budget 才能反映"全局重试量"。如果每个请求各用一个 Budget,那 Budget 就退化成"每个请求最多重试 N 次",失去了防风暴能力。
- `Ok(_)` 分支也调了 `deposit`:这是 Budget 防风暴的关键——**每个成功请求都向 Budget 里存一点额度**,这样 Budget 的总额度就和系统的**健康度**正相关。系统健康(成功多)→ Budget 满 → 可以放手重试;系统出事(成功少)→ Budget 空 → 重试被拦下。
- `Err(_)` 分支先 `withdraw` 再决定:withdraw 失败(余额不足)就**不重试**,把错误返回。这就是 Budget 作为"防风暴闸门"的代码体现。
- `clone_request` 直接 `Some(req.clone())`:对于 `String` 这种 `Clone` 类型,总是允许重试。如果请求类型不能 clone(比如持有 `&mut` 引用),这里返回 `None`,Retry 自动降级成"不重试"。

> **钉死这件事**:这个示例展示了 Tower Retry 防风暴的全部秘密——**Budget 用 `deposit`/`withdraw` 把重试总量和成功率挂钩**。成功越多 → deposit 越多 → 能 withdraw 越多重试;成功越少 → deposit 越少 → 重试被自动限制。这是一个**负反馈**系统,和"重试 N 次"的正反馈(失败越多 → 重试越多 → 更失败)形成鲜明对比。第 6 节会拆 `TpsBudget` 怎么用 10 槽位滑动窗口实现这个负反馈。

---

### 11.5 ★源码拆解:`Retry` Service 和 `ResponseFuture` 状态机

理论讲够了,现在落到源码。这一节把 `Retry` 怎么实现"一次 `call` 多次执行内层"这件事逐行拆透。

#### 11.5.1 `Retry` Service:就两个方法

`Retry` 这个 Service 的定义在 `tower/src/retry/mod.rs`([source](../tower/tower/src/retry/mod.rs#L42-L94)),非常薄:

```rust
// tower/src/retry/mod.rs#L42-L46
#[derive(Clone, Debug)]
pub struct Retry<P, S> {
    policy: P,
    service: S,
}
```

就两个字段:`policy` 和内层 `service`。注意 `#[derive(Clone)]`——这要求 `P: Clone` 且 `S: Clone`,后者是 Retry 的硬性约束(`mod.rs` 文档明确写了,见 [source](../tower/tower/src/retry/mod.rs#L20-L40))。为什么 Retry 要求 `S: Clone`?因为重试时要重新 `call` 一次,而 `call(&mut self)` 取走了 service 的就绪状态,必须从一个新的 clone 上重新 `poll_ready`。

`Service` trait 的 impl 在 [source](../tower/tower/src/retry/mod.rs#L72-L94):

```rust
// tower/src/retry/mod.rs#L72-L94
impl<P, S, Request> Service<Request> for Retry<P, S>
where
    P: Policy<Request, S::Response, S::Error> + Clone,
    S: Service<Request> + Clone,
{
    type Response = S::Response;
    type Error = S::Error;
    type Future = ResponseFuture<P, S, Request>;

    fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        // NOTE: the Future::poll impl for ResponseFuture assumes that Retry::poll_ready is
        // equivalent to Ready.service.poll_ready. If this ever changes, that code must be updated
        // as well.
        self.service.poll_ready(cx)
    }

    fn call(&mut self, request: Request) -> Self::Future {
        let cloned = self.policy.clone_request(&request);
        let future = self.service.call(request);

        ResponseFuture::new(cloned, self.clone(), future)
    }
}
```

三个关键点:

**1. `poll_ready` 直接转发给内层 `service`。** 注意那段注释——它说"`ResponseFuture::poll` 假定 `Retry::poll_ready` 等价于 `Retry.service.poll_ready`",这是第 5.2 节状态机里那个优化(重试时不再走 `Retry::poll_ready` 而是直接走 `service.poll_ready`)的前提。为什么 Retry 的 `poll_ready` 不做任何额外的事(比如检查 Budget)?因为 Budget 是在 `retry` 方法里检查的(失败之后),不是在 `poll_ready` 里(发请求之前)。`poll_ready` 只关心"内层能不能接请求"。

**2. `call` 里先 `clone_request`,再 `call` 内层。** 注意顺序:先 `self.policy.clone_request(&request)` 拿到一个**克隆的请求**(放进 `Option<Request>`,以备重试),然后才把**原始** request `call` 给内层。`call` 会消费 `request`(move),所以必须先克隆。如果 `clone_request` 返回 `None`(请求不可克隆),那么 `Option<Request>` 是 `None`,后续即便内层失败,Retry 也**不会**尝试重试(因为没东西可重试)。这是 `ResponseFuture` 状态机里一个关键的短路条件。

**3. `ResponseFuture::new(cloned, self.clone(), future)`。** 注意第三个参数 `self.clone()`——Retry 把**自己整个 clone 一份**塞进 Future。为什么?因为重试时要重新 `poll_ready + call`,而 `call` 需要 `&mut self`,Future 在 poll 时拿到的就是这份 clone。这也是为什么 Retry 要求 `S: Clone`——它要把整个 Retry(含 service)clone 进 Future。这个 clone 的代价通常不大(service 一般是 `Arc` 包的小结构),但它是 Retry 的硬性开销。

> **钉死这件事**:`Retry::call` 干了三件事——① `clone_request`(拿重试用的副本,可能为 None);② `call` 内层(发起第一次请求);③ 把 (副本, self.clone(), future) 包成 `ResponseFuture` 返回。**真正的重试逻辑全在 `ResponseFuture::poll` 里**,Retry 这个 Service 本身只是个"启动器"。

#### 11.5.2 `ResponseFuture`:三态状态机

重试的真正核心在 `tower/src/retry/future.rs` 的 `ResponseFuture`([source](../tower/tower/src/retry/future.rs))。这是一个用 `pin_project_lite` 手写出来的 Future 状态机。先看结构:

```rust
// tower/src/retry/future.rs#L11-L44
pin_project! {
    pub struct ResponseFuture<P, S, Request>
    where
        P: Policy<Request, S::Response, S::Error>,
        S: Service<Request>,
    {
        request: Option<Request>,           // 克隆的请求(重试时用),None 表示不可重试
        #[pin]
        retry: Retry<P, S>,                  // 整个 Retry 的 clone(重试时用它 call 内层)
        #[pin]
        state: State<S::Future, P::Future>,  // 状态机当前态
    }
}

pin_project! {
    #[project = StateProj]
    enum State<F, P> {
        // 正在 poll 内层 Service::call 返回的 future
        Called { #[pin] future: F },
        // 正在 poll Policy::retry 返回的 future(通常是退避 Sleep)
        Waiting { #[pin] waiting: P },
        // 退避结束,正在 poll 内层 service.poll_ready 准备重试
        Retrying,
    }
}
```

三个字段:

- `request: Option<Request>`:从 `clone_request` 来的请求副本。如果是 `None`,后续不会重试。重试成功发起后会被 `take()` 设回 `None`(然后重新 `clone_request` 再填一份)。
- `retry: Retry<P, S>`:`Retry::call` 时 clone 进来的整个 Retry。重试时用它内部的 `service` 重新 `poll_ready + call`。
- `state`:三态状态机。

三个状态画成 mermaid 状态图:

```mermaid
stateDiagram-v2
    [*] --> Called
    Called --> Waiting: 内层 future 返回失败\n且 Policy::retry 返回 Some(等待)
    Called --> [*]: 内层 future 返回成功\n或 Policy::retry 返回 None\n或 request 为 None
    Waiting --> Retrying: Policy 的等待 future resolve
    Retrying --> Called: 内层 poll_ready Ready +\n重新 clone_request + call 内层
    Retrying --> [*]: poll_ready 失败(Error)
```

这是 Retry 的执行骨架:**请求在内层和 policy 之间来回跑,直到成功、policy 拒绝、或 poll_ready 失败**。注意循环——`Retrying → Called → Waiting → Retrying → ...` 可以反复多次,这就是"一次 `call` 多次执行内层"的机制。

#### 11.5.3 `poll` 实现:逐段拆

`ResponseFuture::poll` 的全部实现([source](../tower/tower/src/retry/future.rs#L64-L120)):

```rust
// tower/src/retry/future.rs#L64-L120
impl<P, S, Request> Future for ResponseFuture<P, S, Request>
where
    P: Policy<Request, S::Response, S::Error>,
    S: Service<Request>,
{
    type Output = Result<S::Response, S::Error>;

    fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        let mut this = self.project();

        loop {
            match this.state.as_mut().project() {
                StateProj::Called { future } => {
                    let mut result = ready!(future.poll(cx));
                    if let Some(req) = &mut this.request {
                        match this.retry.policy.retry(req, &mut result) {
                            Some(waiting) => {
                                this.state.set(State::Waiting { waiting });
                            }
                            None => return Poll::Ready(result),
                        }
                    } else {
                        // request wasn't cloned, so no way to retry it
                        return Poll::Ready(result);
                    }
                }
                StateProj::Waiting { waiting } => {
                    ready!(waiting.poll(cx));

                    this.state.set(State::Retrying);
                }
                StateProj::Retrying => {
                    // NOTE: we assume here that
                    //
                    //   this.retry.poll_ready()
                    //
                    // is equivalent to
                    //
                    //   this.retry.service.poll_ready()
                    //
                    // we need to make that assumption to avoid adding an Unpin bound to the Policy
                    // in Ready to make it Unpin so that we can get &mut Ready as needed to call
                    // poll_ready on it.
                    ready!(this.retry.as_mut().project().service.poll_ready(cx))?;
                    let req = this
                        .request
                        .take()
                        .expect("retrying requires cloned request");
                    *this.request = this.retry.policy.clone_request(&req);
                    this.state.set(State::Called {
                        future: this.retry.as_mut().project().service.call(req),
                    });
                }
            }
        }
    }
}
```

逐段拆:

**`Called` 分支——内层 future 跑完了,问 policy 要不要重试。**

```rust
StateProj::Called { future } => {
    let mut result = ready!(future.poll(cx));          // 等内层 future 出结果
    if let Some(req) = &mut this.request {             // 有克隆的请求?
        match this.retry.policy.retry(req, &mut result) {  // 问 policy
            Some(waiting) => {
                this.state.set(State::Waiting { waiting });  // 等退避
            }
            None => return Poll::Ready(result),        // policy 说不重试,返回结果
        }
    } else {
        // request wasn't cloned, so no way to retry it
        return Poll::Ready(result);                    // 没克隆请求,没法重试,直接返回
    }
}
```

注意三个细节:

1. `ready!(future.poll(cx))`:这是 `futures_core::ready` 宏,等内层 future 出 `Poll::Ready`,Pending 就直接 return Pending(标准 Future 组合)。内层 future 可能是 hyper 在发 HTTP 请求,可能是 reqwest 在查连接池,它什么时候 resolve 不关 Retry 的事。
2. `if let Some(req) = &mut this.request`:这是"短路"的关键——如果当初 `clone_request` 返回了 `None`(请求不可克隆),那 `this.request` 是 `None`,这里**直接返回结果,根本不调 `policy.retry`**。这就是 `clone_request` 文档说的"如果返回 None,retry 函数不会被调用"。
3. `policy.retry(req, &mut result)`:把**可变引用**传给 policy。policy 可以改 `req`(塞 header)和改 `result`(翻译错误)。返回 `Some` 就转 `Waiting`(等退避),返回 `None` 就把当前 result 返回给调用方。

> **钉死这件事**:这个 `if let Some(req)` 短路是 Retry 的一个**运行期降级**机制——同一个 Retry service,可以让某些请求(`clone_request` 返回 `Some`)支持重试,某些请求(`clone_request` 返回 `None`)不支持重试。决策在运行期做,不在编译期。这对流式 body 这种不可克隆的请求很有用——你想重试一个 POST,但 body 已经被消费了没法克隆,这时 `clone_request` 返回 `None`,Retry 自动放弃重试。

**`Waiting` 分支——退避等待。**

```rust
StateProj::Waiting { waiting } => {
    ready!(waiting.poll(cx));          // 等 policy 给的 future(通常是 Sleep)
    this.state.set(State::Retrying);   // 等完了,准备重试
}
```

很简单——poll policy 返回的 future(通常是 `tokio::time::Sleep` 做指数退避),等它 resolve 就转 `Retrying`。如果 policy 返回的是 `future::ready(())`(立刻重试),这个 `ready!` 立刻返回,几乎无开销。

> **承接《Tokio》**:`tokio::time::Sleep` 内部是 Tokio 的时间轮(`runtime/time/wheel`),它的 register/deregister/wake 机制在《Tokio》已拆透,这里一句带过——policy 返回的 `Sleep` future 在 `Waiting` 分支被 poll,Sleep 内部向 Tokio 时间轮注册一个唤醒,到点了 Tokio wake 这个 future,`ready!` 返回。详见 [[tokio-source-facts]]。本章不重复 Tokio 时间轮的内部。

**`Retrying` 分支——重试前最后一次 poll_ready,然后重新 call 内层。**

```rust
StateProj::Retrying => {
    // NOTE: we assume here that this.retry.poll_ready() is equivalent to
    // this.retry.service.poll_ready()
    ready!(this.retry.as_mut().project().service.poll_ready(cx))?;
    let req = this
        .request
        .take()
        .expect("retrying requires cloned request");
    *this.request = this.retry.policy.clone_request(&req);
    this.state.set(State::Called {
        future: this.retry.as_mut().project().service.call(req),
    });
}
```

这段是状态机里最密的四步:

1. **`ready!(this.retry.as_mut().project().service.poll_ready(cx))?`**:重试前**再 poll_ready 一次**。为什么?因为内层 service 在上一次 `call` 时取走了就绪状态(回顾 P1-02 的 `&mut self` 语义),重试前必须确认它又 ready 了。如果内层返回 `Pending`(比如连接池暂时没空闲连接),这个 future 就 Pending,等内层 ready。如果内层返回 `Error`,这个 `?` 直接把 error 返回给调用方(用 `Poll::Ready(Err)`)。注意那段 NOTE 注释——它**绕过** `Retry::poll_ready` 直接调 `Retry.service.poll_ready`,原因是避免给 Policy 加 `Unpin` bound。这是一个微优化,但注释明确警告了"`Retry::poll_ready` 等价于 `Retry.service.poll_ready`",和 `Retry::poll_ready` 那段注释呼应。
2. **`let req = this.request.take().expect(...)`**:把当初 `clone_request` 留下的请求副本 `take()` 出来(此时 `this.request` 变 `None`)。`expect` 是因为能进 `Retrying` 状态说明肯定经过了 `Called` 分支的 `if let Some(req)` 检查,`this.request` 必然非 `None`。
3. **`*this.request = this.retry.policy.clone_request(&req)`**:再次调 `clone_request`!这是为了**下一次**重试准备的——把这次要用的 `req` 取走了,得再克隆一份留着以备再失败。如果这次 `clone_request` 返回 `None`(可能因为 policy 状态变了),下一次进入 `Called` 分支就会走 `else` 分支直接返回,不再重试。
4. **`this.state.set(State::Called { future: ...service.call(req) })`**:用那个 `req`(可能是被 policy 改写过的,因为第 5.2 节讲过 `retry` 拿的是 `&mut Req`)重新 `call` 内层 service,把返回的 future 塞回 `Called` 状态。状态机回到起点,循环继续。

整个 `poll` 用一个 `loop {}` 包起来——为什么?因为一次 `poll` 调用可能跨过多个状态(`Waiting → Retrying → Called` 这种转移不需要 yield 给 executor)。比如 policy 返回 `future::ready(())`(立刻重试),那 `Waiting` 立刻 resolve,`Retrying` 立刻 poll_ready(假设 ready),立刻 `call`,状态变回 `Called`,这一次 poll 调用就跨过了三个状态。`loop` 让这种"无等待的状态转移"在同一次 poll 里完成,避免无谓的 wake。只有当某个 `ready!` 返回 Pending(真的在等内层 future 或 Sleep)时,`ready!` 才会从 poll 函数 return Pending。

> **钉死这件事**:`ResponseFuture::poll` 是 Retry 全部魔力的所在。它是一个三态状态机,在内层 future 失败时:① 问 policy 要不要重试(`Called → Waiting`);② 等 policy 给的退避 future(`Waiting → Retrying`);③ 重新 poll_ready 内层 + 重新 clone_request + 重新 call 内层(`Retrying → Called`)。这个循环可以反复多次,直到成功、policy 拒绝、或 poll_ready 失败。**一次 `Retry::call` 返回的 Future,内部可能多次 `poll_ready + call` 内层 service**——这就是"call 多次执行内层"的语义。

---

### 11.6 ★`Budget` trait 与 `TpsBudget`:防风暴的闸门

这一节拆 Budget——它是 Retry 防风暴的真正闸门,也是 0.5.0 重构的重头戏。

#### 11.6.1 0.5.0 之前:`Budget` 是个写死的结构体

先看历史。在 0.5.0 之前,`tower/src/retry/budget.rs`(注意是单文件,不是目录)里有一个**具体的 `Budget` 结构体**,长这样(基于 git show d27ba65^:tower/src/retry/budget.rs,简化):

```rust
// 0.5.0 之前(简化示意,基于 git 历史)
pub struct Budget {
    bucket: Bucket,
    deposit_amount: isize,
    withdraw_amount: isize,
}

pub struct Overdrawn { _inner: () }   // 余额不足的错误类型

impl Budget {
    pub fn deposit(&self) { /* ... */ }
    pub fn withdraw(&self) -> Result<(), Overdrawn> { /* ... */ }
}
```

用户只能用这一个 `Budget` 类型,policy 里持有 `Arc<Budget>`,`withdraw` 返回 `Result<(), Overdrawn>`。这个设计的问题:

- **不可扩展**:用户想换一种预算策略(比如基于熔断器状态的预算、基于自适应限流的预算),做不到——`Budget` 是个具体类型,不是 trait;
- **API 不优雅**:`withdraw` 返回 `Result<(), Overdrawn>`,而 `Overdrawn` 这个错误类型对用户来说没意义(用户只关心"够不够",不关心"为什么不够"),返回 `bool` 更合适。

#### 11.6.2 0.5.0 之后:`Budget` trait + `TpsBudget` 实现

0.5.0 的 PR #703(`d27ba65`)把这件事重构了([CHANGELOG](../tower/tower/CHANGELOG.md#L37) 原文:`retry: Add Budget trait. This allows end-users to implement their own budget and bucket implementations.`)。重构后:

- `tower/src/retry/budget.rs` 这个单文件被拆成 `tower/src/retry/budget/` 目录;
- 目录下 `mod.rs` 定义 `Budget` **trait**([source](../tower/tower/src/retry/budget/mod.rs#L81-L91));
- `tps_budget.rs` 定义 `TpsBudget` **结构体**([source](../tower/tower/src/retry/budget/tps_budget.rs#L26-L41)),是 `Budget` trait 的内置实现(原 `Budget` 结构体改名 `TpsBudget`)。

新的 `Budget` trait 极简,就两个方法:

```rust
// tower/src/retry/budget/mod.rs#L81-L91
pub trait Budget {
    /// Store a "deposit" in the budget, which will be used to permit future
    /// withdrawals.
    fn deposit(&self);

    /// Check whether there is enough "balance" in the budget to issue a new
    /// retry.
    ///
    /// If there is not enough, false is returned.
    fn withdraw(&self) -> bool;
}
```

注意几个关键变化:

1. **trait 而非 struct**:用户可以 `impl Budget for MyBudget`,实现任意预算策略;
2. **`withdraw` 返回 `bool`** 而非 `Result<(), Overdrawn>`:`Overdrawn` 错误类型被删了,用户只关心"够不够",`bool` 最直接;
3. **两个方法都是 `&self`** 而非 `&mut self`:这意味着 Budget 必须**内部可变性**(用 `AtomicIsize`/`Mutex`),这样才能被多个并发请求共享(`Arc<Budget>`)。这是个刻意的约束——Budget 是**跨请求共享**的全局状态,不能是 per-request 的可变状态。

> **不这样会怎样**:0.5.0 之前,如果你想写一个"基于熔断器状态"的 Budget(熔断器开时不允许重试),你做不到——你得自己在 policy 里绕过 Budget 自己写一套。0.5.0 之后,你 `impl Budget for CircuitBreakerBudget`,在 `withdraw` 里查熔断器状态,然后把这个 Budget 塞进 policy。这是"开放扩展"的经典 trait 设计——Tower 把"预算策略"这件事开放给了用户。

#### 11.6.3 `TpsBudget` 的实现:10 槽位滑动窗口令牌桶

现在看内置实现 `TpsBudget` 怎么工作。这是防风暴的核心机制。先看结构([source](../tower/tower/src/retry/budget/tps_budget.rs#L26-L41)):

```rust
// tower/src/retry/budget/tps_budget.rs#L26-L41
pub struct TpsBudget {
    generation: Mutex<Generation>,
    /// Initial budget allowed for every second.
    reserve: isize,
    /// Slots of a the TTL divided evenly.
    slots: Box<[AtomicIsize]>,
    /// The amount of time represented by each slot.
    window: Duration,
    /// The changers for the current slot to be committed
    /// after the slot expires.
    writer: AtomicIsize,
    /// Amount of tokens to deposit for each put().
    deposit_amount: isize,
    /// Amount of tokens to withdraw for each try_get().
    withdraw_amount: isize,
}

#[derive(Debug)]
struct Generation {
    /// Slot index of the last generation.
    index: usize,
    /// The timestamp since the last generation expired.
    time: Instant,
}
```

`TpsBudget` 是一个**滑动窗口令牌桶**。它的核心字段:

- **`slots: Box<[AtomicIsize]>`**:10 个槽位的数组(代码里 `windows = 10u32`),每个槽位是一个 `AtomicIsize`,存"这段时间内的令牌净增减"。这是滑动窗口的存储;
- **`window: Duration`**:每个槽位代表的时间长度(`ttl / 10`)。比如 `ttl = 10s`,那 `window = 1s`,每个槽位代表 1 秒;
- **`reserve: isize`**:基础储备——即使没有任何 deposit,也保证每秒允许一定数量的重试(给刚启动的、低流量的客户端用);
- **`writer: AtomicIsize`**:**当前槽位**的临时写入区。deposit/withdraw 先写到 `writer`,等当前槽位过期了再 commit 到 `slots[index]`;
- **`generation: Mutex<Generation>`**:当前是第几个槽位(`index`)+ 上次过期的时间戳(`time`);
- **`deposit_amount` / `withdraw_amount`**:每次 deposit/withdraw 加减多少令牌。这俩由 `retry_percent` 算出来(见下文)。

`new` 的构造函数([source](../tower/tower/src/retry/budget/tps_budget.rs#L69-L112))揭示了关键参数语义:

```rust
// tower/src/retry/budget/tps_budget.rs#L69-L112
pub fn new(ttl: Duration, min_per_sec: u32, retry_percent: f32) -> Self {
    // assertions taken from finagle
    assert!(ttl >= Duration::from_secs(1));
    assert!(ttl <= Duration::from_secs(60));
    assert!(retry_percent >= 0.0);
    assert!(retry_percent <= 1000.0);
    assert!(min_per_sec < ::std::i32::MAX as u32);

    let (deposit_amount, withdraw_amount) = if retry_percent == 0.0 {
        (0, 1)
    } else if retry_percent <= 1.0 {
        (1, (1.0 / retry_percent) as isize)
    } else {
        (1000, (1000.0 / retry_percent) as isize)
    };
    let reserve = (min_per_sec as isize)
        .saturating_mul(ttl.as_secs() as isize)
        .saturating_mul(withdraw_amount);
    // ...
}
```

三个参数:

- **`ttl`**:deposit 的有效期,1~60 秒。一个 deposit 在 ttl 内有效,过期作废——这是滑动窗口的时间跨度;
- **`min_per_sec`**:每秒最少允许的重试数(给低流量客户端的保底);
- **`retry_percent`**:重试比例——每单位 deposit 允许多少单位 withdraw。这是防风暴的核心旋钮。

`retry_percent` 的换算逻辑(注意这里 `deposit_amount`/`withdraw_amount` 的设计很巧妙):

- `retry_percent == 0.0`:`deposit_amount = 0`, `withdraw_amount = 1`。deposit 不增加余额,withdraw 只能消耗 `reserve`。这就是"完全禁用 deposit 模式,只靠保底"。
- `retry_percent <= 1.0`(比如 0.2):`deposit_amount = 1`, `withdraw_amount = (1.0/0.2) = 5`。每 deposit 1 个令牌,withdraw 要 5 个令牌——意味着每 5 次成功(deposit)才允许 1 次重试(withdraw)。这就是 retry_percent = 0.2 = "重试是成功数的 20%"的语义。
- `retry_percent > 1.0`(比如 2.0):`deposit_amount = 1000`, `withdraw_amount = 500`。每 deposit 1000,withdraw 500——即每 1 次成功允许 2 次重试。

为什么要这么奇怪的"deposit_amount=1000"的换算?因为 `retry_percent` 可以是小数(比如 0.2),但令牌是整数,需要放大避免精度损失。`retry_percent <= 1.0` 时用 `(1, 1/pct)`,`> 1.0` 时用 `(1000, 1000/pct)`,这是为了避免 `1.0/pct` 在 `pct > 1` 时下取整变 0。

> **钉死这件事**:`TpsBudget::new(ttl, min_per_sec, retry_percent)` 这三个参数定义了 Budget 的全部行为。**`retry_percent` 是防风暴的总开关**——它把"重试量"和"成功量"的比例**钉死**。比如配 0.2,意味着无论系统多健康,重试量永远不超过成功量的 20%;系统出事时(成功少),这 20% 也随之减少,自然收敛。这就是负反馈的量化体现。

#### 11.6.4 `deposit` / `withdraw` 的运行期行为

`Budget` trait 的两个方法在 `TpsBudget` 上的实现([source](../tower/tower/src/retry/budget/tps_budget.rs#L153-L181)):

```rust
// tower/src/retry/budget/tps_budget.rs#L153-L170
fn put(&self, amt: isize) {
    self.expire();
    self.writer.fetch_add(amt, Ordering::SeqCst);
}

fn try_get(&self, amt: isize) -> bool {
    debug_assert!(amt >= 0);

    self.expire();

    let sum = self.sum();
    if sum >= amt {
        self.writer.fetch_add(-amt, Ordering::SeqCst);
        true
    } else {
        false
    }
}

// tower/src/retry/budget/tps_budget.rs#L173-L181
impl Budget for TpsBudget {
    fn deposit(&self) {
        self.put(self.deposit_amount)
    }
    fn withdraw(&self) -> bool {
        self.try_get(self.withdraw_amount)
    }
}
```

逻辑很清楚:

- **`deposit`** = `put(deposit_amount)`:先 `expire`(见下),然后往 `writer` 加 `deposit_amount` 个令牌(用原子 `fetch_add`)。
- **`withdraw`** = `try_get(withdraw_amount)`:先 `expire`,然后 `sum()` 算总余额,够就 `fetch_add(-amt)` 扣掉并返回 `true`,不够返回 `false`。

`sum()` 的实现([source](../tower/tower/src/retry/budget/tps_budget.rs#L139-L151)):

```rust
// tower/src/retry/budget/tps_budget.rs#L139-L151
fn sum(&self) -> isize {
    let current = self.writer.load(Ordering::SeqCst);
    let windowed_sum: isize = self
        .slots
        .iter()
        .map(|slot| slot.load(Ordering::SeqCst))
        // fold() is used instead of sum() to determine overflow behavior
        .fold(0, isize::saturating_add);

    current
        .saturating_add(windowed_sum)
        .saturating_add(self.reserve)
}
```

总余额 = 当前 writer + 10 个 slots 的总和 + reserve(基础储备)。注意 `saturating_add`——防溢出,溢出就饱和到 `isize::MAX`。

`expire()` 是滑动窗口的核心([source](../tower/tower/src/retry/budget/tps_budget.rs#L114-L137)):

```rust
// tower/src/retry/budget/tps_budget.rs#L114-L137
fn expire(&self) {
    let mut gen = self.generation.lock().expect("generation lock");

    let now = Instant::now();
    let diff = now.saturating_duration_since(gen.time);
    if diff < self.window {
        // not expired yet
        return;
    }

    let to_commit = self.writer.swap(0, Ordering::SeqCst);
    self.slots[gen.index].store(to_commit, Ordering::SeqCst);

    let mut diff = diff;
    let mut idx = (gen.index + 1) % self.slots.len();
    while diff > self.window {
        self.slots[idx].store(0, Ordering::SeqCst);
        diff -= self.window;
        idx = (idx + 1) % self.slots.len();
    }

    gen.index = idx;
    gen.time = now;
}
```

`expire` 干两件事:

1. **把 `writer` 的累积量 commit 到当前 slot**:`self.writer.swap(0, ...)` 把 writer 清零,把累积值存到 `slots[gen.index]`;
2. **清掉过期的 slot**:从 `gen.index` 往后扫,每超过一个 `window` 就把对应 slot 清零。这是滑动窗口——超过 `ttl = 10 * window` 的老 deposit 自然被清掉,不再计入余额。

注意 `expire` 用 `Mutex` 保护(因为要同时改 `gen.index`、`gen.time`、`slots`)。这是个**短临界区**——只做原子读写,不 await,不阻塞。`deposit`/`withdraw` 用 `Ordering::SeqCst` 保证内存序正确(虽然 `SeqCst` 性能差点,但 Budget 操作不频繁,可接受)。

#### 11.6.5 把它画出来:滑动窗口令牌桶

用 ASCII 框图画 `TpsBudget` 的内存布局(`ttl=10s`, `window=1s`):

```
TpsBudget (Arc-shared across all requests of one service)
┌─────────────────────────────────────────────────────────────┐
│ reserve: isize           ← 基础储备(min_per_sec × ttl × withdraw_amount)│
│ deposit_amount: isize    ← 每次 deposit 加多少(retry_percent 决定)│
│ withdraw_amount: isize   ← 每次 withdraw 减多少(retry_percent 决定)│
│                                                             │
│ generation: Mutex<Generation>                               │
│   ├─ index: usize    ← 当前 slot 下标(0..9)               │
│   └─ time: Instant   ← 上次过期时间                         │
│                                                             │
│ writer: AtomicIsize  ← 当前 slot 的临时写入区(原子)        │
│                                                             │
│ slots: Box<[AtomicIsize; 10]>  ← 10 个槽位,每个代表 1 秒    │
│   ┌───┬───┬───┬───┬───┬───┬───┬───┬───┬───┐                 │
│   │ +5│ +3│ -2│  0│ +8│ +1│ -4│  0│  0│  0│  (net tokens)   │
│   └───┴───┴───┴───┴───┴───┴───┴───┴───┴───┘                 │
│     ↑                                                       │
│     当前 index=0(writer 累积满了会 commit 到这里)          │
│                                                             │
│ sum() = writer + Σ slots[i] + reserve                       │
│       = (live deposits - withdrawals in last 10s) + reserve │
└─────────────────────────────────────────────────────────────┘
        deposit(Ok): writer += deposit_amount   (原子)
        withdraw(retry): if sum() >= withdraw_amount
                          then writer -= withdraw_amount; true
                          else false
        expire(): 每 1s 触发一次,writer→slots[index],清过期 slot
```

**`deposit` 的语义**:成功请求调 `deposit` → 往 `writer` 加令牌 → 1 秒后 commit 到 slot → 在接下来 10 秒(`ttl`)内有效 → 10 秒后 slot 被清,作废。

**`withdraw` 的语义**:失败请求调 `withdraw` → 检查 `sum() >= withdraw_amount` → 够就扣 `writer`,返回 `true`(允许重试);不够返回 `false`(放弃重试,防风暴)。

**滑动窗口的语义**:只有**最近 10 秒**的 deposit 计入余额。这意味着:

- 系统刚启动:slot 全 0,reserve 提供"启动额度",允许少量重试;
- 系统健康运行 10 秒:slot 里攒满了 deposit,余额充足,可以放手重试;
- 系统出事(突然大量失败):deposit 停止增长(withdraw 多,deposit 少),10 秒内老 deposit 过期作废,余额快速耗尽,后续重试被拦下,**自然收敛**。

这就是 Budget 防风暴的全部机制——它是一个"和成功率挂钩的负反馈令牌桶"。和"最多重试 3 次"的"无脑放大"形成鲜明对比。

> **钉死这件事**:`TpsBudget` 的设计**借鉴自 Twitter Finagle**(代码注释里写了 `// assertions taken from finagle`)。Finagle 是 Scala 的 RPC 框架,它的 retry budget 是这个滑动窗口令牌桶设计的源头。Google SRE 书《SRE》第 22 章也推荐这种"基于成功率的重试预算"思路。Tower 把它实现成 `Budget` trait 的内置实现 `TpsBudget`,用户也可以实现自己的 Budget。

---

### 11.7 `Backoff` trait:指数退避 + 抖动

Budget 解决了"还能不能重试",这一节看"什么时候重试"——Backoff。

#### 11.7.1 为什么需要退避:同步惊群

先讲不讲退避会怎样。假设 1000 个客户端同时发请求,后端过载返回 503。如果所有客户端都"立刻重试",那么 1000 个重试请求会**在同一时刻**打到后端——这就是"同步惊群"(thundering herd)。后端刚被这波打一下,又迎来一波完全同步的重试,雪上加霜。

退避(backoff)的解法是:**让重试之间有间隔**。但单纯"等固定时间再重试"也不行——1000 个客户端还是会在固定时间后同步重试。所以需要**抖动(jitter)**:每个客户端等一个**随机**的时间再重试,把重试打散。

最常用的策略是**指数退避 + 抖动**(exponential backoff with jitter):每次重试的等待时间是 `base * 2^iterations` 再加一个随机抖动,`base` 是初始等待,`iterations` 是重试次数。

AWS 的架构博客有一篇经典文章《Exponential Backoff and Jitter》讲透了这件事(`backoff.rs` 文档里直接引了 [link](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/))。

#### 11.7.2 `Backoff` trait 和 `ExponentialBackoff`

`tower/src/retry/backoff.rs`([source](../tower/tower/src/retry/backoff.rs))定义了 `Backoff` trait 和内置的 `ExponentialBackoff`。这是 0.5.0 新加的(CHANGELOG #685: `retry: Add generic backoff utilities`)。

trait 定义([source](../tower/tower/src/retry/backoff.rs#L20-L39)):

```rust
// tower/src/retry/backoff.rs#L20-L39
pub trait MakeBackoff {
    type Backoff: Backoff;
    fn make_backoff(&mut self) -> Self::Backoff;
}

/// A backoff trait where a single mutable reference represents a single
/// backoff session. Implementors must also implement [`Clone`] which will
/// reset the backoff back to the default state for the next session.
pub trait Backoff {
    type Future: Future<Output = ()>;
    /// Initiate the next backoff in the sequence.
    fn next_backoff(&mut self) -> Self::Future;
}
```

两个 trait:`MakeBackoff`(工厂,每次请求 session 创建一个新的 `Backoff`)和 `Backoff`(单次 session 的退避策略)。`Backoff::next_backoff` 返回一个 `Future<Output = ()>`,这就是 `Policy::retry` 应该返回的那个"等待 future"。

内置实现 `ExponentialBackoff`([source](../tower/tower/src/retry/backoff.rs#L55-L70)):

```rust
// tower/src/retry/backoff.rs#L55-L70
/// A jittered [exponential backoff] strategy.
///
/// The backoff duration will increase exponentially for every subsequent
/// backoff, up to a maximum duration. A small amount of [random jitter] is
/// added to each backoff duration, in order to avoid retry spikes.
#[derive(Debug, Clone)]
pub struct ExponentialBackoff<R = HasherRng> {
    min: time::Duration,
    max: time::Duration,
    jitter: f64,
    rng: R,
    iterations: u32,
}
```

字段:

- `min`:初始(最小)等待时间;
- `max`:最大等待时间(指数增长的上限);
- `jitter`:抖动比例(0.0~100.0);
- `rng: R`:随机数生成器,默认 `HasherRng`;
- `iterations: u32`:当前重试次数(每次 `next_backoff` 自增)。

`next_backoff` 的实现([source](../tower/tower/src/retry/backoff.rs#L170-L184)):

```rust
// tower/src/retry/backoff.rs#L170-L184
impl<R> Backoff for ExponentialBackoff<R>
where
    R: Rng,
{
    type Future = tokio::time::Sleep;

    fn next_backoff(&mut self) -> Self::Future {
        let base = self.base();
        let next = base + self.jitter(base);

        self.iterations += 1;

        tokio::time::sleep(next)
    }
}
```

`base()` 是指数部分([source](../tower/tower/src/retry/backoff.rs#L134-L148)):

```rust
// tower/src/retry/backoff.rs#L134-L148
fn base(&self) -> time::Duration {
    // ...debug asserts...
    self.min
        .checked_mul(2_u32.saturating_pow(self.iterations))
        .unwrap_or(self.max)
        .min(self.max)
}
```

公式:`base = min(min * 2^iterations, max)`。注意 `checked_mul` + `unwrap_or(max)`——如果 `min * 2^iterations` 溢出(`checked_mul` 返回 `None`),直接用 `max`。这是一个防溢出的细节。

`jitter()` 是抖动部分([source](../tower/tower/src/retry/backoff.rs#L150-L167)):

```rust
// tower/src/retry/backoff.rs#L152-L167
fn jitter(&mut self, base: time::Duration) -> time::Duration {
    if self.jitter == 0.0 {
        time::Duration::default()
    } else {
        let jitter_factor = self.rng.next_f64();
        debug_assert!(
            jitter_factor > 0.0,
            "rng returns values between 0.0 and 1.0"
        );
        let rand_jitter = jitter_factor * self.jitter;
        let secs = (base.as_secs() as f64) * rand_jitter;
        let nanos = (base.subsec_nanos() as f64) * rand_jitter;
        let remaining = self.max - base;
        time::Duration::new(secs as u64, nanos as u32).min(remaining)
    }
}
```

抖动量是 `base * (rand_factor * jitter_ratio)`,即 `base` 乘一个随机比例(0 到 `jitter` 之间)。`remaining = max - base` 确保抖动后总时长不超过 `max`。

默认配置(`Default` impl,[source](../tower/tower/src/retry/backoff.rs#L186-L196)):

```rust
// tower/src/retry/backoff.rs#L186-L196
impl Default for ExponentialBackoffMaker {
    fn default() -> Self {
        ExponentialBackoffMaker::new(
            Duration::from_millis(50),       // min = 50ms
            Duration::from_millis(u64::MAX), // max = ~无限(实际是 u64::MAX 毫秒)
            0.99,                            // jitter = 99%
            HasherRng::default(),
        ).expect("Unable to create ExponentialBackoff")
    }
}
```

默认:min=50ms, max=u64::MAX(实际无上限), jitter=99%。jitter=99% 意味着抖动量可以接近 base 本身,把重试时间打散得非常彻底。

> **承接《Tokio》**:`tokio::time::sleep(next)` 内部创建一个 `tokio::time::Sleep` future,它向 Tokio 的时间轮注册一个定时器,到点了 wake。Sleep 的内部机制(register/deregister/`timer_entry`/时间轮的层级结构)在《Tokio》已拆透,这里一句带过——Backoff 的"等待"就是 sleep,和 P3-08 Timeout 用的 `tokio::time::sleep` 是同一个东西,详见 [[tokio-source-facts]]。

#### 11.7.3 把 Backoff 塞进 Policy

`Backoff` trait 是独立提供的,但 Policy 怎么用它?答案是 policy 持有一个 `MakeBackoff`(工厂),在 `retry` 方法里调 `next_backoff` 拿到一个 `Sleep` future,作为 `Some(waiting)` 返回。示意:

```rust
// (示意,非源码原文)
struct BackoffPolicy {
    budget: Arc<TpsBudget>,
    make_backoff: ExponentialBackoffMaker,   // 工厂
}

impl<Req, Res, E> Policy<Req, Res, E> for BackoffPolicy
where Req: Clone {
    type Future = ExponentialBackoff::Future;   // = tokio::time::Sleep

    fn retry(&mut self, req: &mut Req, result: &mut Result<Res, E>) -> Option<Self::Future> {
        match result {
            Ok(_) => { self.budget.deposit(); None }
            Err(_) => {
                if !self.budget.withdraw() { return None; }
                // 退避:返回一个 Sleep future
                let mut backoff = self.make_backoff.make_backoff();
                Some(backoff.next_backoff())
            }
        }
    }

    fn clone_request(&mut self, req: &Req) -> Option<Req> { Some(req.clone()) }
}
```

注意 `MakeBackoff`/`Backoff` 的分工——`MakeBackoff` 是 per-service 的工厂(可以 clone 给每个请求 session),`Backoff` 是 per-session 的(`iterations` 是这次请求的重试次数,session 结束就丢)。这个"工厂 + 实例"的两层结构,和 P5-15 的 `MakeBalance`/`Balance`、P4-13 的 `MakeService`/`Service` 是同一个模式——Rust 里构造"有状态的、可 clone 的策略"的标准手法。

> **钉死这件事**:Backoff 是 Policy 的"可选组件"。Policy 完全可以不用 Backoff,直接返回 `Some(future::ready(()))` 立刻重试(像 `Attempts(usize)` 那样)——但生产环境的重试**必须**有退避 + 抖动,否则就是同步惊群。Backoff trait 的存在让"退避策略"也可以替换——用户可以 `impl Backoff for MyBackoff`,实现自己的退避算法(比如基于失败类型的退避:503 退避长一点,网络错误退避短一点)。

---

## 技巧精解

这一节把本章最硬核的两个技巧单独拆透:① `ResponseFuture` 的三态状态机 + "call 多次执行内层"的语义;② 0.5.0 `Budget` 从 struct 重构成 trait 的 breaking 与好处。

### 技巧一:`call` 一次,内层 `call` 多次——状态机怎么做到的

这是 Retry 最绕的地方。读者第一次看 `Retry::call` 会困惑:`Service::call` 不是应该只发一次请求吗?为什么 Retry 的 `call` 返回的 Future 内部会反复 `call` 内层?

答案的核心在 `ResponseFuture` 的状态机。这里再用一张图把"一次外层 `call`,多次内层 `call`"画清楚:

```mermaid
sequenceDiagram
    participant Caller as 调用方
    participant Retry as Retry::call
    participant RF as ResponseFuture
    participant Inner as 内层 Service

    Caller->>Retry: call(req)
    Retry->>Retry: clone_request(req) → req_clone
    Retry->>Inner: call(req) [第 1 次]
    Retry->>RF: new(req_clone, self.clone(), inner_future)
    Retry-->>Caller: ResponseFuture

    Note over RF: 状态: Called

    Caller->>RF: poll
    RF->>Inner: poll inner_future
    Inner-->>RF: Ready(Err) 失败
    RF->>RF: policy.retry(req, Err) → Some(Sleep)
    Note over RF: 状态: Called → Waiting

    Caller->>RF: poll (重复)
    RF->>RF: poll Sleep (退避)
    Note over RF: Sleep Pending, RF return Pending

    Note over RF: (退避到点) 状态: Waiting → Retrying
    RF->>Inner: poll_ready [内层重新就绪]
    Inner-->>RF: Ready(Ok)
    RF->>RF: req_clone.take(); 再 clone_request
    RF->>Inner: call(req_clone) [第 2 次]
    Note over RF: 状态: Retrying → Called

    RF->>Inner: poll inner_future (第 2 次)
    Inner-->>RF: Ready(Ok) 成功!
    RF->>RF: policy.retry(req, Ok) → deposit; None
    RF-->>Caller: Poll::Ready(Ok(res))
```

这张图把"一次外层 call,内层被 call 多次"的时序画全了。关键技术点:

1. **`Retry::call` 只调内层一次**(第一次),把 inner_future 包进 `ResponseFuture` 返回;
2. **重试的发起在 `ResponseFuture::poll` 里**——内层 future 失败时,poll 调 `policy.retry`,如果返回 `Some`,状态机转入 `Waiting → Retrying → Called`,在 `Retrying` 状态里**重新** `poll_ready + call` 内层;
3. **整个循环在同一个 `ResponseFuture` 里**——对调用方来说,它只 poll 了一个 future,不知道内部重试了多少次。

> **不这样会怎样**:如果 Retry 不是这种状态机,而是"递归 future"(每次失败返回一个新的 RetryFuture),会有两个问题:① 每次 retry 都分配一个新 future,性能差;② 递归 future 容易爆栈(尤其重试次数多时)。Tower 用 `pin_project_lite` 手写状态机,把"重试循环"扁平化成一个 `loop {}` + 三态 enum,既零分配,又不会爆栈。这是 Rust 异步生态里"手写 Future 状态机"的典型范例,和 hyper 的 `SendRequest` future、Tokio 的 `mpsc::Receiver` future 是同一类技巧。

### 技巧二:0.5.0 `Budget` trait 化——breaking 与好处

这是这一章第二个值得单独拆的点。重构前后的对照:

| 维度 | 0.5.0 之前 | 0.5.0 之后 |
|------|-----------|-----------|
| `Budget` 是什么 | 一个具体 struct(`tower/src/retry/budget.rs`) | 一个 trait(`tower/src/retry/budget/mod.rs`) |
| 内置实现名 | `Budget` | `TpsBudget`(`tower/src/retry/budget/tps_budget.rs`) |
| `withdraw` 签名 | `fn withdraw(&self) -> Result<(), Overdrawn>` | `fn withdraw(&self) -> bool` |
| `Overdrawn` 类型 | 存在 | 删除 |
| 用户能自定义 Budget | 不能 | 能(`impl Budget for MyBudget`) |
| 文件结构 | 单文件 `budget.rs` | 目录 `budget/`(mod.rs + tps_budget.rs) |

**breaking 的代价**:

1. 所有持有 `Arc<Budget>` 的 policy 要改成 `Arc<TpsBudget>`(类型改名);
2. 所有调 `withdraw().is_ok()` 的代码要改成 `withdraw()`(返回值从 Result 变 bool);
3. `Overdrawn` 类型被删,引用它的代码编译失败。

**好处**:

1. **开放扩展**:用户可以 `impl Budget for MyBudget`,实现任意预算策略。比如:
   - 基于熔断器的 Budget(熔断器开时 `withdraw` 永远返回 false);
   - 基于自适应限流的 Budget(`withdraw` 查当前 RPS,超阈值返回 false);
   - 基于 Prometheus 指标的 Budget(查全局错误率,错误率高时收紧预算);
   - 基于时间的 Budget(白天宽松,夜间严格);
2. **API 更直接**:`withdraw(&self) -> bool` 比 `-> Result<(), Overdrawn>` 更符合"够不够"的语义,`Overdrawn` 这种"无意义的错误类型"被消除;
3. **trait 的对象安全**:`Budget` 只有两个 `&self` 方法,是对象安全的(可以用 `dyn Budget`)。policy 可以持有 `Arc<dyn Budget>`,运行期切换预算策略。

> **钉死这件事**:0.5.0 这次重构是 Tower 演进史上"trait 化"的典型一例——把"一个写死的实现"提升为"一个 trait + 一个内置实现",换来了"用户可扩展"的能力。代价是一次 breaking change(用户要改 import 和方法调用)。这种 tradeoff 在 Rust 库设计里很常见:`tokio` 的 `AsyncRead`/`AsyncWrite`(从具体类型到 trait)、`hyper` 的 `Body` trait(从 `hyper::Body` 到 `http_body::Body` trait)都是同一类演进。Tower 0.5.0 的 retry 模块这次重构,让 Retry 中间件从一个"用啥预算 Tower 说了算"的封闭组件,变成了一个"预算策略可插拔"的开放框架。

---

## 章末小结

### 回扣全局:Retry 服务"执行"这一面

Retry 是第 4 篇(韧性类)的第一章,它服务的毫无疑问是**执行单元**这一面——它是一个 Service,有 `poll_ready`(直接转发给内层)和 `call`(发起请求 + 包成 `ResponseFuture`)。它的 `ResponseFuture` 是一个状态机 Future,在执行过程中可能多次调用内层 service。

但 Retry 也有强烈的"组合"色彩——它的三个 trait(`Policy`/`Budget`/`Backoff`)都是**可替换的组件**,通过泛型参数注入 `Retry<P, S>`。从这个角度看,Retry 是"用 trait 把策略拼起来"的典范,和第 1 篇的 Layer(把 Service 装饰成新 Service)在精神上是一致的——只不过 Layer 是"组合 Service",而 Retry 是"组合 Policy"。这也是为什么 Retry 的灵活性这么高:换 Policy 就换重试逻辑,换 Budget 就换防风暴策略,换 Backoff 就换退避算法,三者正交。

### 五个"为什么"清单

1. **为什么 Retry 不用"最多重试 N 次"这种配置?**
   因为它会以乘法放大流量,把毛刺放大成雪崩。Tower 用 `Budget`(和成功率挂钩的负反馈令牌桶)替代,让重试总量随系统健康度自动收敛。
2. **为什么 0.5.0 把 `Policy::retry` 改成 `&mut self, &mut Req, &mut Result`?**
   让 policy 可以改写请求(塞 header)、改写结果(翻译错误)、维护内部状态(指数退避计数)。旧签名 `&self, &Req, Result<&Res, &E>` 做不到这些,用户得用 `RefCell` 绕。
3. **为什么 0.5.0 把 `Budget` 从 struct 重构成 trait?**
   开放扩展——让用户可以 `impl Budget for MyBudget`,实现自定义预算策略(熔断器、自适应限流、Prometheus 指标)。代价是一次 breaking change(类型改名、签名变化)。
4. **为什么 `Retry` 要求内层 `S: Clone`?**
   因为 `call(&mut self)` 取走了就绪状态,重试时要从一个新的 clone 上重新 `poll_ready`。Retry 在 `call` 里 `self.clone()` 一份塞进 `ResponseFuture`,重试时用这份 clone 的 `service` 重新发起。`!Clone` 的 service 要用 Retry,得先套一层 `Buffer`(P2-05)把它变成 `Clone`。
5. **为什么 `ResponseFuture` 是手写状态机而不是 async fn?**
   零分配 + 不爆栈。重试循环用 `loop {}` + 三态 enum 扁平化,每次 poll 跨多个状态都不分配新 future。这是 Rust 异步生态手写 Future 状态机的典型范例。

### 想继续深入往哪钻

- **源码**:
  - `tower/src/retry/mod.rs`(`Retry` Service,20 行核心);
  - `tower/src/retry/policy.rs`(`Policy` trait,90 行);
  - `tower/src/retry/future.rs`(`ResponseFuture` 状态机,120 行,本章最硬核);
  - `tower/src/retry/budget/mod.rs`(`Budget` trait + 防风暴文档);
  - `tower/src/retry/budget/tps_budget.rs`(`TpsBudget` 滑动窗口令牌桶);
  - `tower/src/retry/backoff.rs`(`Backoff`/`ExponentialBackoff`);
  - `tower/src/retry/layer.rs`(`RetryLayer`,28 行,极薄)。
- **承接 Tokio**:`tokio::time::sleep` / `Sleep`(Backoff 退避用)——见《Tokio》时间轮相关章 [[tokio-source-facts]]。
- **对照 Envoy**:Envoy retry policy(`retry_on`/`num_retries`/`retry_back_off`)配置驱动 vs Tower trait 抽象;Envoy overload manager 的 LoadShedPoint 对照本书 P2-07 LoadShed——见《Envoy》HCM/overload 相关章 [[envoy-source-facts]]。
- **对照 Finagle**:Twitter Finagle 的 retry budget 是 `TpsBudget` 的设计源头,Google SRE 书第 22 章也讲"retry budget"思路。
- **实践**:
  - Retry + Buffer(给 `!Clone` service 套 Buffer 再套 Retry);
  - Retry + Balance(每次重试 P2C 重新选后端,避开失败的那台)——第 5 篇展开;
  - Retry + Timeout(每个 per_try_timeout + 总 timeout,见附录 B)。

### 引出下一章

Retry 解决了"请求失败了重试"的问题,但有一种"失败"不是真的失败——**请求太慢**。p99 延迟里的长尾请求,可能不是后端挂了,只是某一次请求恰好被调度到了慢的机器、走了慢的网络路径。这时候 Retry 整个重发有点浪费(原来的请求其实快回来了),不重发又要等很久。下一章 **P4-12 Hedge(对冲请求)** 讲的是另一种解法:**等请求接近 p99 时,再发一个"对冲"请求,谁先回用谁**。Hedge 用 `rotating_histogram` 滚动估 p99,是降尾延迟的招牌技巧,和 Retry 形成"失败重试 vs 慢了对冲"的姊妹篇。我们下一章见。

---

> **本章核心**:Retry 不是"重试 N 次",而是 Policy(单次决策)+ Budget(总量闸门)+ Backoff(退避抖动)三个正交 trait。Policy 基于单次结果判断该不该重试,Budget 基于一段时间的成功率限制重试总量(防风暴的负反馈),Backoff 用指数退避 + 抖动避免同步惊群。一次 `call` 返回的 `ResponseFuture` 是三态状态机,内部可能多次 `poll_ready + call` 内层,直到成功、Policy 停、或 Budget 耗尽。0.5.0 把 `Budget` 从 struct 重构成 trait,把 Policy 改成 `&mut`,让 Retry 从"封闭组件"变成"开放框架"。
