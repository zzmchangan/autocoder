# 第 5 篇 · 第 16 章 · PD 的角色:TSO + 调度 + ID 分配

> **核心问题**:前面四章我们一直在讲 Region 内部的 Raft、Region 之间的 Percolator 事务。但有一个角色始终在背景里晃——**PD(Placement Driver)**。事务要拿 `start_ts`/`commit_ts`,谁给?Region 想分裂,新 Region 的 id 谁分配?机器加进来、热点 Region 要搬走,谁决定搬哪、搬到哪?这一章,我们正面拆 PD 这个"集群大脑"到底管什么、怎么管,以及为什么它必须是一个**中心化**的组件。

> **读完本章你会明白**:
> 1. PD 为什么管的是三件事——**分配全局时间戳(TSO)、调度 Region(balance)、分配 Region/Store ID**——而不是两件或四件,这三件事为什么无法下沉到 TiKV 各自去做。
> 2. PD 自己怎么保证高可用:**PD 集群自身也跑 Raft**(承接《etcd》),3 个 PD 节点挂一个不影响;为什么不用 Gossip 去中心化,而坚持中心化。
> 3. TiKV 侧怎么看 PD:`components/pd_client/` 是 TiKV 内嵌的 PD 客户端,heartbeat 怎么上报、TSO 怎么批量取、ID 怎么申请——以及为什么取 TSO 走的是一条**专用的 gRPC 流**而非一问一答。
> 4. PD 服务端(TSO 分配算法、balance 调度决策)**不在本地 clone 的 tikv 仓**,而在独立的 `tikv/pd` 仓(Go 写);本章诚实标注边界,只讲本地能看到的 tikv 侧协作。

> **如果一读觉得太难**:先只记住三件事——① PD 是中心化大脑,自己也是 Raft 集群(挂一个没事);② 它管三件事:发时间戳(TSO)、搬 Region(balance)、发 id(谁要谁申请);③ TiKV 这边通过 `pd_client` 跟它说话,Region 心跳定期上报、TSO 是批量预申请。

---

## 〇、一句话点破

> **PD 是 TiKV 集群里唯一一个"看得见全局"的角色——所有 Region 把自己的状态心跳给它,它据此发时间戳、发 id、决定谁搬到哪。中心化换来全局视图,自身 Raft 换来高可用,这是 TiKV 在"全局调度能力"和"单点风险"之间做的取舍。**

这是结论,不是理由。本章倒过来拆:先讲为什么这三件事必须有一个中心化角色来做(去中心化会撞什么墙),再讲 PD 自己怎么用 Raft 解决"中心化就=单点"的悖论,然后逐个拆三件职责在 tikv 侧的源码长什么样,最后交代诚实边界(PD 服务端在另一个仓)。

---

## 一、为什么必须有 PD:三件无法下沉的事

读到这一篇,你已经知道 TiKV 的核心机制:**百万个 Raft 组(multi-raft)各管一个 Region,跨 Region 用 Percolator 拼事务**。这套机制里,每个 Region 是高度自治的——它有自己的 Raft leader、自己的日志、自己的 apply 流程。那么问题来了:

- 事务的 `start_ts` 从哪来?MVCC 要给每个 key 标版本号,Percolator 要用 `start_ts`/`commit_ts` 判定先后,**这个时间戳必须全局唯一、全局单调递增**——否则两个事务各自拿到一样的 `start_ts`,版本号撞了,隔离性就破了。可时间戳这件事,你能让每个 Region 自己产生吗?
- Region 想分裂成两个,新 Region 的 id 怎么定?如果两个 Region 同时分裂,各自拍一个 id 出来,**撞了怎么办**?id 必须全局唯一。
- 集群里某台机器磁盘快满了,或者某几个 Region 是热点(被疯狂读写),需要把一部分 Region 挪到别的机器——**这个"挪"的决策谁做**?如果让每台机器各自决定,大家都觉得"我太忙了该把别人挪走",就没人真挪。

这三件事——**全局时间戳、全局 id、全局调度**——有个共同特征:**它们都需要一个"全局视图"**。任何一个 Region、任何一台 TiKV 单独看,都只能看到自己那一亩三分地,做不了全局决策。这就是 PD 存在的根本理由。

> **不这样会怎样**:假设没有 PD,让 TiKV 节点之间用 Gossip 互相传播状态、各自做决策。问题立刻冒出来:
> - **时间戳没法全局单调**:Gossip 是最终一致的,两个节点各自推自己的时间戳计数器,消息还没传到对方,两边就可能产生重叠的 ts。MVCC 一撞版本号,事务隔离就废了。
> - **id 没法全局唯一**:同理,Gossip 传播有延迟,两个节点同时申请一个"还没被用过的"id,撞了。
> - **调度没法全局最优**:Gossip 只能让节点看到邻居状态,看不到整个集群哪里最空、哪里最热。要做"把 leader 从最忙的机器挪到最闲的机器"这种**全局均衡**,必须有全局视图。
>
> 这就是为什么 TiKV 选了中心化的 PD——**它用"一个中心点"换"全局视图"**,这个全局视图是发时间戳、发 id、做调度这三件事的共同前提。

### 三个职责为什么是这三个(不多不少)

仔细想,PD 管的这三件事,恰好覆盖了 TiKV 集群运转的三个全局性需求:

1. **TSO(时间戳)** —— 事务层的全局序。MVCC 的版本、Percolator 的 `start_ts`/`commit_ts`,都从这里来。**没有全局序,事务层就不 ACID**。
2. **调度(balance/hot region)** —— 复制层的全局均衡。Region 怎么放、leader 怎么打散、热点怎么分散,都从这里决策。**没有全局调度,集群就退化为"Region 各自为政、负载严重不均"**。
3. **ID 分配** —— 元数据的全局唯一。新 Region 的 region_id、新 Store 的 store_id、新 Peer 的 peer_id,都从这里发。**没有全局 id,分裂/扩容就 id 撞车**。

注意这三件事的归属:TSO 服务事务层(第 4 篇 Percolator 的支柱)、调度服务复制层(第 1~2 篇 Region 的再平衡)、ID 分配则是两边都用的元数据基础设施。所以 PD 是个**横跨复制层和事务层的衔接角色**——这也是本章在二分法里标"事务层/衔接"的原因。

> **钉死这件事**:PD 不是一个"可选的辅助组件",它是 TiKV 区别于"一群互相不认识的 Region"的核心。没有 PD,百万个 Raft 组就像一百万个互不相干的小数据库,既没有全局事务(没 TSO)、也没有全局均衡(没调度)、连扩容都做不到(id 撞车)。PD 把它们拧成了一个**集群**。

---

## 二、PD 自己怎么高可用:再跑一个 Raft

中心化的 PD 解决了"全局视图"问题,但立刻引出一个更要命的担忧——**单点**。如果 PD 这一台机器挂了,整个集群就拿不到时间戳、做不了调度、发不了 id,**所有事务全部卡死**。这比"一台 TiKV 挂了"严重得多——一台 TiKV 挂了只影响它上面的 Region(还有别的副本顶上),PD 挂了影响**全集群**。

### 答案:PD 自己也是个 Raft 集群

这个担忧,TiKV 用一个干脆的答案解决:**PD 自己也跑 Raft**。一个生产部署的 PD 是 3 个(或更多奇数个)节点,这 3 个节点之间用 Raft 复制状态(承接《etcd》那本,Raft 的选主、日志、提交这些不重复讲)。任意时刻只有一个是 leader(真正的"工作中的 PD"),所有 TSO 请求、调度决策都由 leader 做;另外两个是 follower,默默同步日志。挂一个?剩下两个里选出新 leader,继续干。

```
   PD 集群(3 节点,自己跑 Raft)
   ┌─────────────────────────────────────┐
   │  PD leader(工作中)                  │
   │   ├─ 发 TSO                          │
   │   ├─ 做调度决策                      │
   │   └─ 发 id                           │
   │  PD follower 1   PD follower 2       │
   │   (同步日志)      (同步日志)         │
   └─────────────────────────────────────┘
        leader 挂了 → 剩下两个 Raft 选主 → 新 leader 继续
```

> **承接《etcd》**:PD 自己就是个 etcd(实际上,早期 PD 就是直接嵌入了 etcd 作为存储和共识层;现在的 PD 用 etcd 的 Raft 库做共识,自己再加 TSO/调度逻辑)。Raft 怎么选主、怎么保证多数派一致、leader 切换时日志怎么衔接——这些**全是《etcd》那本拆透的**,本章不重复。你只需要记住:**PD 集群的高可用,和 etcd 集群的高可用,是同一种机制**(Raft 多数派)。

### 为什么不用 Gossip 去中心化(一个经典的取舍)

读到这你可能会问:既然 PD 这么关键,为什么不让所有 TiKV 节点用 Gossip 互相传播状态,去中心化地做调度?这样就没有单点了。

这是个值得严肃回答的问题,因为它折射了分布式系统里一个根本张力:**全局视图 vs 去中心化**。

- **Gossip(去中心化)**:每个节点把自己知道的状态告诉邻居,邻居再告诉邻居,最终所有节点"大致"都知道了全貌。优点是**没有单点**,任何节点挂了不影响。致命缺点是**最终一致**——某一刻,A 节点以为"B 很闲",但其实 B 早就被别人塞满了,消息还没传到;A 据此做了错误调度。而且 Gossip 只能做局部贪心决策,**做不了全局最优**。
- **PD(中心化 + Raft 高可用)**:所有 Region 把状态汇报给一个中心,中心有**完整、强一致**的全局视图(挂的 follower 通过 Raft 日志追平),据此做全局最优调度。代价是**有一个 leader**,但这个 leader 用 Raft 保证高可用,挂了秒级切换。

TiKV 的取舍是后者,理由很实在:**对一个要扛几十 TB、百万 QPS、做精细调度的数据库,全局视图比"去单点"更重要**。而且 Raft 已经把"中心化=单点"这个悖论破解了——3 个 PD 节点挂一个不影响、挂两个才完蛋(概率极低)。用"PD 集群 Raft 高可用"换"全局调度能力",这笔账划算。

> **钉死这件事**:**PD 用中心化换全局视图,用 Raft 换高可用**。这两个选择叠加,既拿到了 Gossip 拿不到的全局最优调度能力,又规避了"中心化=单点"的致命风险。这是 TiKV 在 CAP 取舍里一个相当干净的选择——不是"中心化 vs 去中心化"的二选一,而是"中心化但自身高可用"的第三条路。

---

## 三、tikv 侧的 PD 客户端:`components/pd_client/`

讲完 PD 是什么、为什么,我们落到本地能看到的源码。**重要边界声明**:PD 服务端(TSO 分配算法、balance 调度决策、id 计数器)**不在本地 clone 的 tikv 仓**,它在独立的 `tikv/pd` 仓(Go 写)。本地 tikv 仓里能看到的,是 **TiKV 侧的 PD 客户端 `components/pd_client/`**——它负责和 PD 服务端通信。本章以及 P5-17、P5-18,都以这个客户端为锚点讲 TiKV 侧怎么协作;涉及 PD 服务端逻辑时,会诚实标注"在 pd 仓,不在本地"。

`pd_client` 的对外入口是一个 trait —— `PdClient`,它定义了"TiKV 需要问 PD 的所有事":

```rust
// components/pd_client/src/lib.rs#L284(简化,只列三职责相关方法)
pub trait PdClient: Send + Sync {
    // —— 职责一:ID 分配 ——
    /// Allocates a unique positive id.
    fn alloc_id(&self) -> Result<u64> { ... }

    // —— 职责二:TSO(全局时间戳)——
    /// Gets a timestamp from PD.
    fn get_tso(&self) -> PdFuture<TimeStamp> {
        self.batch_get_tso(1)
    }
    /// Gets a batch of timestamps from PD.
    fn batch_get_tso(&self, _count: u32) -> PdFuture<TimeStamp> { ... }

    // —— 职责三:调度协作 ——
    /// Region's Leader uses this to heartbeat PD.
    fn region_heartbeat(
        &self, term: u64, region: metapb::Region, leader: metapb::Peer,
        region_stat: RegionStat, replication_status: Option<RegionReplicationStatus>,
    ) -> PdFuture<()> { ... }

    /// Sends store statistics regularly.
    fn store_heartbeat(
        &self, stats: pdpb::StoreStats, report: Option<pdpb::StoreReport>,
        status: Option<StoreDrAutoSyncStatus>,
    ) -> PdFuture<pdpb::StoreHeartbeatResponse> { ... }

    /// Asks PD for batch split. PD returns the newly split Region ids.
    fn ask_batch_split(
        &self, region: metapb::Region, count: usize, reason: pdpb::SplitReason,
    ) -> PdFuture<pdpb::AskBatchSplitResponse> { ... }

    // —— 路由(也是 PD 管)——
    /// Gets Region which the key belongs to.
    fn get_region(&self, key: &[u8]) -> Result<metapb::Region> { ... }
    fn get_store(&self, store_id: u64) -> Result<metapb::Store> { ... }
    ...
}
```

把这个 trait 的方法分一下类,正好对应 PD 的三职责 + 路由:

| 职责 | trait 方法 | 方向 | 谁主动 |
|------|------------|------|--------|
| **TSO** | `get_tso` / `batch_get_tso` | TiKV → PD | TiKV 要时间戳时主动拉 |
| **调度协作** | `region_heartbeat` / `store_heartbeat` | TiKV → PD | TiKV 定期主动上报 |
| **调度协作** | `ask_batch_split` | TiKV → PD | TiKV 想分裂时问 PD 要 id |
| **ID 分配** | `alloc_id` / (split 里的新 region_id) | TiKV → PD | TiKV 需要新 id 时申请 |
| **路由** | `get_region` / `get_store` | TiKV → PD | TiKV(或 TiDB)找 Region 时查 |

> **钉死这件事**:`PdClient` trait 是 TiKV 和 PD 之间**唯一**的接口边界。所有"问 PD 的事"——拿时间戳、上报心跳、要 id、查路由——都走这几个方法。看懂这个 trait,就看懂了 TiKV 侧"怎么和 PD 协作"的全貌。

### `RpcClient`:真实的实现

`PdClient` 是 trait(为了测试时能 mock),真实实现是 `RpcClient`(走 gRPC):

```rust
// components/pd_client/src/client.rs#L52
#[derive(Clone)]
pub struct RpcClient {
    cluster_id: u64,
    pd_client: Arc<Client>,        // 底层 gRPC 客户端,封装重连/leader 切换
    monitor: Arc<ThreadPool<TaskCell>>,
}
```

`RpcClient::new` 里有一段关键逻辑(简化示意):

```rust
// components/pd_client/src/client.rs#L60-L100(简化)
pub async fn new_async(cfg: &Config, ...) -> Result<RpcClient> {
    let pd_connector = PdConnector::new(env.clone(), security_mgr.clone());
    for i in 0..retries {
        match pd_connector.validate_endpoints(cfg, true).await {
            Ok((client, target, members, tso)) => {
                let cluster_id = members.get_header().get_cluster_id();
                let rpc_client = RpcClient {
                    cluster_id,
                    pd_client: Arc::new(Client::new(
                        ..., tso.unwrap(), cfg.enable_forwarding, cfg.retry_interval.0,
                    )),
                    monitor: monitor.clone(),
                };
                // spawn 一个后台 future,定期重连 PD 更新信息
                let duration = cfg.update_interval.0;
                ... // 循环里调 cli.reconnect(false)
                return Ok(rpc_client);
            }
            ...
        }
    }
}
```

注意两个细节:

1. **`validate_endpoints` 会校验所有 PD 节点,并拿到当前 leader 的地址**。PD 是 3 节点 Raft,客户端必须连到 leader 才能正常工作(发请求给 follower 会被转发,慢)。所以初始化时先问一遍 PD members,找到 leader。
2. **`tso` 字段被单独传进 `Client::new`**——这是因为 TSO 走的不是普通的一问一答 RPC,而是一条**专用的 gRPC 双向流**,需要单独建一条连接。这个细节是 P5-17 的重点,这里先记住:**TSO 有独立的通道**。

> **承接《gRPC》**:pd_client 和 PD 之间的通信走的是 gRPC(普通的 unary RPC + TSO 用的双向流)。HTTP/2、流、调用选项这些**承接《gRPC》那本**,本章不重复。这里只讲"TiKV 在 gRPC 之上定义了哪些 PD 调用"。

---

## 四、职责一(预热):ID 分配——最简单的一件

三件职责里,ID 分配最简单,我们先讲它(它的源码也最短)。

### 为什么 id 必须全局唯一

TiKV 集群里,每个 Region 有一个 `region_id`,每个 Store(一台 TiKV 节点)有一个 `store_id`,每个 Peer(Region 的一个副本)有一个 `peer_id`。这些 id 必须**全局唯一**,否则出大问题:

- **region_id 撞了**:两个不同的 Region 有同一个 id,PD 的路由表就乱了——查一个 key,到底路由到哪个 Region?
- **store_id 撞了**:两台 TiKV 节点用同一个 store_id,heartbeat 上报时 PD 分不清谁是谁。
- **peer_id 撞了**:同一个 Region 的两个副本 peer_id 一样,Raft 协议就跑不动(Raft 用 peer_id 区分副本)。

所以这些 id **必须有一个全局唯一的来源**。最朴素的做法是让 PD 维护一个全局计数器,每次有人要 id,PD 自增一下返回——这就保证了全局唯一(因为只有一个计数器,且 PD 是 Raft leader,强一致)。

### 源码:`alloc_id` 就是一次普通 RPC

```rust
// components/pd_client/src/client.rs#L420
fn alloc_id(&self) -> Result<u64> {
    let _timer = PD_REQUEST_HISTOGRAM_VEC.alloc_id.start_coarse_timer();

    let mut req = pdpb::AllocIdRequest::default();
    req.set_header(self.header());

    let resp = sync_request(&self.pd_client, LEADER_CHANGE_RETRY, |client, option| {
        client.alloc_id_opt(&req, option)
    })?;
    check_resp_header(resp.get_header())?;

    let id = resp.get_id();
    if id == 0 {
        return Err(box_err!("pd alloc weird id 0"));
    }
    Ok(id)
}
```

就这么短——构造一个 `AllocIdRequest`,发给 PD leader,等响应,返回 id。没有任何花哨的东西。`sync_request` 是个辅助函数,内部会处理"PD leader 切换时重试"(`LEADER_CHANGE_RETRY`),这正是前面说的"PD Raft 高可用"在客户端这侧的体现——**leader 挂了换新的,客户端自动重连重试,对调用方透明**。

> **PD 服务端的 id 计数器逻辑不在本地**:PD 那边怎么维护这个全局计数器(怎么持久化、怎么在 leader 切换时不丢不重),在 `tikv/pd` 仓的 `server/id/` 等,不在本地 clone 的 tikv 仓。本章不展开,但你心里要有数:**它就是个 Raft 复制的全局自增计数器,保证唯一性靠"只有一个 leader 在发"**。

### Region 分裂时也要 id(split 顺带讲一句)

`alloc_id` 是显式申请 id 的入口。但 Region 分裂时,新 Region 的 id 是通过 `ask_batch_split` 一次性拿到的(PD 在响应里直接返回新 region_id 和新 peer_id 列表):

```rust
// components/pd_client/src/client.rs#L718(简化)
fn ask_batch_split(
    &self, region: metapb::Region, count: usize, reason: pdpb::SplitReason,
) -> PdFuture<pdpb::AskBatchSplitResponse> {
    let mut req = pdpb::AskBatchSplitRequest::default();
    req.set_header(self.header());
    req.set_region(region);
    req.set_split_count(count as u32);     // 我想分裂成 count+1 个
    req.set_reason(reason);
    // ... 发给 PD,PD 返回 count 个新 region_id + 各自的 peer_id
}
```

这一步在 P2-08(Region 分裂)里拆过细节——TiKV 决定要分裂,问 PD 要新 id,PD 给了 id 后 TiKV 才真的执行分裂。这里只强调一点:**分裂这个动作是 TiKV 发起、PD 批准(id)+ TiKV 执行**——典型的"决策权分离"(谁有数据谁执行,谁有全局视图谁发 id)。

> **钉死这件事**:ID 分配的源码极简(一次 RPC),但它折射的设计很关键——**全局唯一的 id 必须有一个中心化的来源**。TiKV 不自己拍 id,全去问 PD 要,这是"中心化换唯一性"的最直接体现。下一节讲 TSO,那才是 PD 三职责里最复杂、最精巧的一件。

---

## 五、职责二(引子):TSO——全局时间戳的入口

TSO 是 PD 三职责里最硬核的一件(全书招牌章 P5-17 专门拆),这里只讲它在 `pd_client` 里的入口长什么样,为下一章铺垫。

### 为什么时间戳必须全局单调递增

事务的 `start_ts`(事务开始时拿的时间戳)、`commit_ts`(提交时的时间戳),都来自 TSO。MVCC 用 `start_ts` 决定一个版本对当前事务是否可见(只看 `commit_ts ≤ start_ts` 的版本),Percolator 用 `commit_ts` 判定两个事务谁先谁后。**如果两个事务拿到一样的时间戳,或者后拿的事务拿到的时间戳比先拿的小**,整个 MVCC 的版本序就乱了——可能出现"A 提交了,B 后开始却读到 A 提交前的状态"这种隔离性破坏。

所以 TSO 的承诺是:**全局唯一、全局单调递增**。这个承诺,只能由一个中心点(PD leader)集中分配来保证——如果有两个节点各自发时间戳,谁也保证不了"我发的比你发的大"。

### 源码入口:`get_tso` 就是 `batch_get_tso(1)`

```rust
// components/pd_client/src/lib.rs(在 PdClient trait 里)
/// Gets a timestamp from PD.
fn get_tso(&self) -> PdFuture<TimeStamp> {
    self.batch_get_tso(1)        // 取 1 个时间戳
}

/// Gets a batch of timestamps from PD.
/// 返回一个 (physical, logical) 时间戳,表示分配的范围是
/// [Timestamp(physical, logical - count + 1), Timestamp(physical, logical)]
fn batch_get_tso(&self, _count: u32) -> PdFuture<TimeStamp> { ... }
```

注意 `batch_get_tso` 的注释——**PD 一次返回 count 个连续的时间戳,范围是 `[logical - count + 1, logical]`**。也就是说,PD 不是每次只发一个时间戳,而是**批量发**:你一次要 N 个,PD 给你一个范围,你自己分。这个"批量"是 TSO 性能的关键(单条事务就要一次 RPC 拿时间戳,延迟扛不住),P5-17 会拆透。

真实的 `batch_get_tso` 实现走的是 `client.inner.rl().tso.get_timestamp(count)`,这个 `tso` 字段就是前面提到的 `TimestampOracle`(TSO 后台线程)。它的核心机制在 `components/pd_client/src/tso.rs`,下一章 P5-17 会逐行拆——这里只点破:**TSO 不是简单的一问一答,而是一条专用的 gRPC 流 + 一个后台线程批量攒请求**。

```rust
// components/pd_client/src/client.rs#L914(简化)
fn batch_get_tso(&self, count: u32) -> PdFuture<TimeStamp> {
    let executor = move |client: &Client, _| {
        let ts_fut = Compat::new(Box::pin(client.inner.rl().tso.get_timestamp(count)));
        let with_timeout = GLOBAL_TIMER_HANDLE.timeout(
            ts_fut, std::time::Instant::now() + Duration::from_secs(REQUEST_TIMEOUT),
        ).compat();
        Box::pin(async move {
            let ts = with_timeout.await.map_err(...)?;
            PD_REQUEST_HISTOGRAM_VEC.tso.observe(timer.saturating_elapsed_secs());
            Ok(ts)
        })
    };
    self.pd_client.request((), executor, LEADER_CHANGE_RETRY).execute()
}
```

注意 `REQUEST_TIMEOUT` 是 2 秒(`const REQUEST_TIMEOUT: u64 = 2;`,在 lib.rs 末尾)——TSO 请求 2 秒不回就超时报错。这也说明 TSO 是延迟敏感的(事务要等时间戳才能开始),不能让它卡太久。

> **PD 服务端的 TSO 分配算法不在本地**:PD 那边怎么用"物理时钟 + 逻辑计数器"混编出全局单调递增的时间戳、怎么在 leader 切换时保证时间戳不倒退(TLA+ 级别的正确性论证),在 `tikv/pd` 仓的 `server/tso/`,**不在本地 clone 的 tikv 仓**。P5-17 会讲清原理(可对照 PD 官方源码),并重点拆本地能看到的 `resolved_ts`(`components/resolved_ts/`,tikv 侧算安全点)和 `pd_client/tso.rs`(tikv 侧怎么取)。**诚实标注:TOS 分配本体在 pd 仓**。

---

## 六、职责三:调度协作——Region 心跳怎么上报

第三件职责是调度(balance/hot region,下一章 P5-18 专拆)。这里先讲 TiKV 侧怎么把"自己当前的状态"汇报给 PD——因为调度决策的前提是 PD 得知道每个 Region、每个 Store 当前什么状况,这靠**心跳(heartbeat)**。

### Region 心跳:leader 定期把状态告诉 PD

每个 Region 的 leader,会定期发一个 `RegionHeartbeatRequest` 给 PD,内容是:"我是 Region X 的 leader,任期是 term,我的 Region 范围是 [start_key, end_key),最近写了多少字节、读了多少字节,我的副本都有谁、谁 down 了":

```rust
// components/pd_client/src/client.rs#L584(简化)
fn region_heartbeat(
    &self, term: u64, region: metapb::Region, leader: metapb::Peer,
    region_stat: RegionStat, replication_status: Option<RegionReplicationStatus>,
) -> PdFuture<()> {
    PD_HEARTBEAT_COUNTER_VEC.with_label_values(&["send"]).inc();

    let mut req = pdpb::RegionHeartbeatRequest::default();
    req.set_term(term);
    req.set_header(self.header());
    req.set_region(region);
    req.set_leader(leader);
    req.set_down_peers(region_stat.down_peers.into());
    req.set_pending_peers(region_stat.pending_peers.into());
    req.set_bytes_written(region_stat.written_bytes);
    req.set_keys_written(region_stat.written_keys);
    req.set_bytes_read(region_stat.read_bytes);
    req.set_keys_read(region_stat.read_keys);
    req.set_query_stats(region_stat.query_stats);
    req.set_approximate_size(region_stat.approximate_size);
    req.set_approximate_keys(region_stat.approximate_keys);
    req.set_cpu_usage(region_stat.cpu_usage);
    req.set_cpu_stats(region_stat.cpu_stats);
    ...
}
```

注意 `RegionStat` 这个结构(`components/pd_client/src/lib.rs`),它就是心跳里上报的"Region 当前状态":

```rust
// components/pd_client/src/lib.rs(简化)
#[derive(Default, Clone, Debug)]
pub struct RegionStat {
    pub down_peers: Vec<pdpb::PeerStats>,    // 哪些副本挂了
    pub pending_peers: Vec<metapb::Peer>,    // 哪些副本日志落后(还没追上)
    pub written_bytes: u64,                  // 最近写了多少字节
    pub written_keys: u64,
    pub read_bytes: u64,                     // 最近读了多少字节
    pub read_keys: u64,
    pub query_stats: QueryStats,             // 查询分类统计(读/写/Coprocessor)
    pub approximate_size: u64,               // Region 大概多大
    pub approximate_keys: u64,               // 大概多少个 key
    pub cpu_usage: u64,                      // CPU 占用(老字段)
    pub cpu_stats: pdpb::CpuStats,           // CPU 详细统计(新字段,9.x)
    ...
}
```

这些字段就是 PD 做调度的依据:**`written_bytes`/`read_bytes`/`cpu_usage` 高 → PD 判定这是热点,考虑搬走或分裂;`approximate_size` 大 → 考虑分裂;`down_peers` 非空 → 考虑补副本**。没有这些心跳,PD 就是个瞎子,啥决策也做不了。

> **钉死这件事**:Region 心跳是 **TiKV → PD** 的单向信息流,目的是让 PD 拿到全局视图。**leader 才发心跳**(follower 不发——follower 不知道自己是不是稳定的 leader,发了也是无效信息)。心跳间隔默认 60 秒左右(Region 大或忙时会更频繁),这是个带宽和时效性的权衡——太频繁浪费网络,太稀疏 PD 看到的状态过时。

### Store 心跳:整台 TiKV 节点的状态

除了 Region 心跳,还有 **Store 心跳**——整台 TiKV 节点把自己的状态(磁盘用了多少、CPU 多忙、总读写量)汇报给 PD:

```rust
// components/pd_client/src/client.rs#L758(简化)
fn store_heartbeat(
    &self, mut stats: pdpb::StoreStats, report: Option<pdpb::StoreReport>,
    dr_autosync_status: Option<StoreDrAutoSyncStatus>,
) -> PdFuture<pdpb::StoreHeartbeatResponse> {
    let mut req = pdpb::StoreHeartbeatRequest::default();
    req.set_header(self.header());
    stats.mut_interval().set_end_timestamp(UnixSecs::now().into_inner());
    req.set_stats(stats);
    ...
}
```

Store 心跳和 Region 心跳的区别:Region 心跳是 per-Region 的(每个 Region 一个),Store 心跳是 per-Store 的(每台 TiKV 一个,聚合所有 Region 的统计)。PD 做调度时两者都用——Region 心跳定位热点,Store 心跳定位空闲机器。

### 心跳响应:PD 通过心跳响应下发调度指令

一个容易被忽略的细节:**Region 心跳不是单纯的上报,PD 会在心跳响应里下发调度指令**。比如 PD 看到 Region X 的 leader 在很忙的机器 A 上,它就在下一次心跳响应里告诉 leader:"你应该把 leader 转移到机器 B 的那个 peer"(transfer leader),或者"你应该把自己分裂成两个"(split),或者"你应该在机器 C 上加一个副本"(add peer)。

TiKV 侧收心跳响应的入口是 `handle_region_heartbeat_response`(在 `PdClient` trait 里),它注册一个回调,PD 每发一个响应,回调被触发,把调度指令转成 raftstore 内部的 Task 去执行。这部分细节在 P5-18(调度)拆,这里只点破:**心跳通道是双向的——上行 Region 状态,下行调度指令**。

```
   TiKV(Region leader)                  PD
        │                                │
        │── RegionHeartbeatRequest ────▶│  (上报:我很忙,size=300MB)
        │                                │  (PD 决策:该分裂了)
        │◀── RegionHeartbeatResponse ───│  (下发:split!新 region_id=42)
        │                                │
        │   (TiKV 执行 split)            │
        │── RegionHeartbeatRequest ────▶│  (上报:我分裂了,现在是两个 Region)
```

> **钉死这件事**:PD 的调度不是它自己直接动手——**它只能"建议",TiKV 执行**。PD 在心跳响应里下发指令(transfer leader / split / add peer),TiKV 的 raftstore 收到后转成内部的 Raft 提议去执行。这种"决策权在 PD、执行权在 TiKV"的分离,是因为 PD 没有直接操作 TiKV 数据的能力(它连 Raft 日志都写不了),只能通过 Region leader 间接驱动。

---

## 七、技巧精解:为什么 TSO 走专用 gRPC 流(而非一问一答)

本章挑一个最值得单独拆透的技巧:**TSO 为什么不走普通的 unary RPC(一问一答),而要走一条专用的 gRPC 双向流(bidirectional stream)**。这个设计直接决定了 TiKV 在高并发事务下的 TSO 延迟和吞吐,是 PD 客户端最精巧的地方之一。

### 朴素做法会撞什么墙

假设我们朴素地实现 TSO:每个事务要时间戳,就发一个 `GetTsoRequest` 给 PD,等 PD 返回 `GetTsoResponse`。一问一答,简单直接。看起来没问题——直到并发上来:

- 每秒 10 万个事务,就是 10 万次 RPC。每次 RPC 都有**网络往返延迟**(就算 PD 在内网,也是 0.5~1ms),加上 gRPC 的协议开销。
- PD leader 那边要处理 10 万次请求,每次都得走 Raft 状态机确认自己还是 leader、读时间戳计数器、自增、返回。**单位时间能处理的事务数,被 TSO 这个单点卡死**。

这就是"TSO 单点瓶颈"的经典担忧——TiKV 所有事务都得过 PD 这一个时间戳发放点,PD 处理不过来,整个集群的事务吞吐就被限住了。

### TiKV 的答案:专用流 + 后台线程批量攒请求

打开 `components/pd_client/src/tso.rs`,看 `TimestampOracle` 怎么做的:

```rust
// components/pd_client/src/tso.rs#L55-L95(简化)
pub struct TimestampOracle {
    /// 一个 bounded channel,把"要时间戳"的请求送到后台 TSO 工作线程
    request_tx: mpsc::Sender<TimestampRequest>,
    close_rx: watch::Receiver<()>,
}

impl TimestampOracle {
    pub(crate) fn new(
        cluster_id: u64, pd_client: &PdClient, call_option: CallOption,
    ) -> Result<TimestampOracle> {
        let (request_tx, request_rx) = mpsc::channel(MAX_BATCH_SIZE);   // 容量 64
        let (rpc_sender, rpc_receiver) = pd_client.tso_opt(call_option)?;  // 一条专用 gRPC 流
        ...
        // 起一个后台线程,跑两个并发 future
        thread::Builder::new()
            .name(TSO_WORKER_THREAD.to_string())
            .spawn_wrapper(move || {
                block_on(run_tso(cluster_id, rpc_sender, rpc_receiver, request_rx, close_tx))
            })
            .expect("unable to create tso worker thread");
        ...
    }
}
```

关键在 `run_tso` 里**两个并发的 future**:

```rust
// components/pd_client/src/tso.rs#L119-L160(简化)
async fn run_tso(
    cluster_id: u64,
    mut rpc_sender: impl Sink<(TsoRequest, WriteFlags), Error = Error> + Unpin,
    mut rpc_receiver: impl Stream<Item = Result<TsoResponse>> + Unpin,
    mut request_rx: mpsc::Receiver<TimestampRequest>,
    close_tx: watch::Sender<()>,
) {
    // pending: 等待 PD 响应的请求组(每组是一次批量请求)
    let pending_requests = Rc::new(RefCell::new(VecDeque::with_capacity(MAX_PENDING_COUNT)));

    // future 1: 从 channel 攒请求,攒够一批就发一个 TsoRequest 给 PD
    let send_requests = async move {
        rpc_sender.send_all(&mut TsoRequestStream { ... }).await?;
        rpc_sender.close().await?;
        Ok(())
    };

    // future 2: 从 PD 收 TsoResponse,把时间戳分发给等待的请求
    let receive_and_handle_responses = async move {
        while let Some(Ok(resp)) = rpc_receiver.next().await {
            allocate_timestamps(&resp, &mut pending_requests.borrow_mut())?;
        }
        Ok(())
    };

    // 两个 future 并发跑——发送和接收解耦
    let (send_res, recv_res) = join!(send_requests, receive_and_handle_responses);
    ...
}
```

这两个 future 解耦,是精髓所在:

- **future 1(发送)**:不停从 `request_rx`(本地 channel)捞"要时间戳"的请求,**攒够一批(最多 `MAX_BATCH_SIZE = 64` 个)就发一个 `TsoRequest` 给 PD**。这一个 `TsoRequest` 里 `count` 字段 = 这一批请求要的时间戳总数。也就是说,**64 个 TiKV 内部事务的时间戳请求,被合并成了 1 次发给 PD 的 RPC**。
- **future 2(接收)**:从 PD 收 `TsoResponse`(一条流上的多个响应),每个响应带一个 `(physical, logical, count)`,然后 `allocate_timestamps` 把这 `count` 个时间戳分发给等待的那批请求。

而本地 channel 和 PD 之间,用的是**一条专用的 gRPC 双向流**(`pd_client.tso_opt()` 建立的)——不是每次 RPC 新建连接,而是一条**长连接**,TiKV 往里塞 `TsoRequest`,PD 往回塞 `TsoResponse`,双方都不用等对方。

### 这个设计妙在哪

把这三层叠起来看:

1. **本地 channel 攒批**:TiKV 内部 N 个事务同时要时间戳,它们不各自发 RPC,而是都往 `request_tx` 这个 channel 塞一个 `TimestampRequest`(本质是个 oneshot channel 的 sender),然后阻塞等待。
2. **后台线程批量发**:TSO worker 线程从 channel 攒一批(最多 64 个),合成一个 `TsoRequest`(count = 总数)发给 PD。**1 次 RPC 拿回 N 个时间戳**。
3. **PD 分配一段连续范围**:PD 收到 `TsoRequest(count=N)`,一次性分配 N 个连续时间戳 `[logical - N + 1, logical]`,返回一个 `TsoResponse`。
4. **本地分发**:TSO worker 收到响应,按请求顺序把时间戳分发给每个等待的事务(通过之前那个 oneshot channel 的 sender)。

效果:**每秒 10 万次时间戳请求,可能只需要 ~1500 次 PD RPC**(每次拿 64 个)。网络往返次数降两个数量级,PD 的处理压力也大幅降低。这就是为什么 TiKV 能扛住高并发事务而不被 TSO 单点卡死——**客户端侧的批量合并**。

> **不这么写会怎样**:如果朴素地一问一答,10 万 QPS 就是 10 万次 RPC,PD 单点直接被打爆。这个专用流 + 后台攒批的设计,把"每事务一次 RPC"降成了"每 64 事务一次 RPC",是 TSO 在高并发下能撑住的关键。**这个技巧的本质是"用 batch 摊薄单点开销"**——和 raftstore 的 batch-system(P1-04)用一个线程池批量驱动百万 Peer,是同一种思想。

### 反面对比:如果 PD 自己批量但客户端不批量

有意思的是,即使 PD 服务端支持批量分配(它确实支持,`TsoResponse` 带 count),如果客户端不主动攒批,效果也大打折扣——因为客户端每个事务来了就发一个 count=1 的请求,PD 一次也只发一个时间戳。**批量必须在客户端发起**——这正解释了为什么 `tso.rs` 要在 TiKV 侧起一个后台线程专门攒请求,而不是让事务直接调 PD。这个设计,是 TiKV 工程经验的体现。

---

## 八、架构演进:9.x 的 PD 相关新东西

最后交代几个 8.x/9.x 和 PD 相关的演进(诚实标注:这些主要在 PD 仓,tikv 侧只是适配):

1. **`resource_control`(资源管控)**:8.x 新增。PD 维护一个资源组(resource group)的概念,不同业务/租户分到不同组,每组有 RU(Resource Unit,综合 CPU/IO/内存的配额)上限。tikv 侧 `pd_client` 加了 `report_ru_metrics` 上报 RU 消耗(见 `PdClient` trait 最后)。这本质上是**把"调度"从"搬数据"扩展到了"限流量"**——不只是均衡数据分布,还要均衡资源消耗。

2. **`causal_ts`(因果时间戳)**:`components/causal_ts/`。这是给 CDC(Change Data Capture)和某些场景用的"因果序"时间戳,和 TSO 不同——TSO 是全局线性序(强一致但慢),causal_ts 是因果序(弱一点但更快,不必每次都过 PD)。它内部用 BatchTso 批量预取再本地推算。本章不展开,知道有这么个东西即可,它是"在 TSO 之外给某些场景开的一条更快的时间戳通道"。

3. **`cpu_stats` 字段(9.x)**:注意前面 `RegionStat` 里有两个 CPU 字段——老的 `cpu_usage`(已 deprecated 注释标了)和新的 `cpu_stats`(详细 CPU 统计)。这是 PD 做"基于 CPU 的热点调度"的依据(老的字段粒度太粗)。这个演进说明 PD 的调度从"看流量(字节/请求)"进化到了"看 CPU"——因为现代 TiKV 瓶颈常常在 Coprocessor 计算而非 IO。

4. **`report_min_resolved_ts`**:tikv 侧把每个 Store 最小的 resolved_ts 上报给 PD(P5-17 拆),PD 据此推进全局 GC safe point。这是事务层和 GC 的衔接。

> **这些演进的趋势**:PD 从"管 Region 分布"进化到"管资源(CPU/RU)、管时间戳变体(causal_ts)、管安全点(resolved_ts)"——职责在扩张。但核心机制没变:**中心化 + Raft 高可用 + 客户端批量协作**。

---

## 九、章末小结

### 回扣主线

本章是第 5 篇(协调)的开篇。回到二分法:**PD 是横跨复制层和事务层的衔接角色**——TSO 服务事务层(Percolator 的全局序),调度服务复制层(Region 均衡),ID 分配是两边都用的元数据基础设施。没有 PD,multi-raft 就是一百万个互不相干的小数据库,既没全局事务也没全局均衡。

PD 的设计可以浓缩成一句话:**中心化换全局视图,Raft 换高可用,客户端批量协作换性能**。

### 五个为什么

1. **为什么 TiKV 必须有 PD?**——三件事(全局时间戳、全局 id、全局调度)都需要一个"看得见全局"的中心点;Gossip 去中心化只能做局部贪心,做不了全局最优,且时间戳/id 没法保证全局唯一。
2. **为什么 PD 中心化却不是单点?**——PD 自己跑 Raft(承接《etcd》),3 节点挂一个不影响;客户端 `pd_client` 自动处理 leader 切换重试(`LEADER_CHANGE_RETRY`)。
3. **为什么 PD 管三件事,不多不少?**——TSO 是事务层的全局序、调度是复制层的全局均衡、ID 是元数据的全局唯一——恰好覆盖 TiKV 集群的三个全局性需求。
4. **为什么 TSO 走专用 gRPC 流而非一问一答?**——高并发下每事务一次 RPC 会打爆 PD 单点;客户端用 `TimestampOracle` 后台线程批量攒请求(最多 64 个一批),把 RPC 次数降两个数量级。
5. **为什么 Region 心跳是 leader 发、且 PD 在响应里下发指令?**——leader 才是稳定的工作节点(follower 可能正在选举);PD 通过心跳响应下发 transfer leader/split/add peer 指令,因为 PD 只能"建议"、TiKV 执行(决策权与执行权分离)。

### 想继续深入往哪钻

- **PD 服务端源码**:在独立的 `tikv/pd` 仓(Go 写),TSO 分配算法在 `server/tso/`、balance 调度在 `server/schedulers/`、id 分配在 `server/id/`。**不在本地 clone 的 tikv 仓**,可对照官方源码。
- **TSO 的"全局单调递增"到底怎么保证**:下一章 P5-17 拆透(物理时钟 + 逻辑计数器混编 + leader 切换不倒退),并拆本地 `components/resolved_ts/`。
- **balance/hot region 调度决策**:P5-18 拆本地 `store/worker/{pd,split_controller}.rs`(tikv 侧上报 + 执行 + 本地热点识别)。
- **承接《etcd》**:PD 自身的 Raft 高可用,和 etcd 集群是同一种机制——选主、日志复制、多数派,详见《etcd》那本。
- **承接《gRPC》**:pd_client 走 gRPC,普通 RPC + TSO 的双向流——HTTP/2 流的细节见《gRPC》。

### 引出下一章

我们搞清了 PD 是什么、管什么、怎么和 TiKV 协作。但还有一个最硬核的问题没拆透:**TSO 凭什么能"全局单调递增"?** PD leader 怎么用物理时钟和逻辑计数器混编出时间戳?leader 切换时怎么保证新 leader 发的时间戳不比老 leader 小?以及,tikv 侧的 `resolved_ts` 是怎么算出"这个时间点之前的数据都稳定了"的安全点(给 GC 和事务读用)?下一章 P5-17,全书招牌章,我们拆到源码级。

> **下一章**:[P5-17 · TSO:全局单调递增的时间戳](P5-17-TSO-全局单调递增的时间戳.md)
