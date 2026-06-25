# 第 5 篇 · 第 18 章 · Region 调度:balance 与热点

> **核心问题**:前面十七章,我们讲完了"数据怎么存(Region/multi-raft)、怎么不丢(Raft)、怎么跨 Region 拼事务(Percolator)、怎么定全局序(TSO)"。但还有一个工程问题没拆:**集群是动态的**——新机器加进来、旧机器磁盘满了、某个 key 突然变成热点(被疯狂读写)。这时怎么让数据自动再平衡、热点自动分散?这一章拆 PD 的第三件职责:**调度(balance/hot region)**,以及 TiKV 侧的 `split_controller` 怎么在本地识别热点 Region 并主动分裂。

> **读完本章你会明白**:
> 1. PD 的调度有两大类——**balance(把 Region/leader 均匀分布到所有 store)** 和 **hot region(识别热点 key,把它搬走或分裂)**——前者解决"长期不均",后者解决"突发热点"。两者都靠 Region 心跳上报的统计驱动。
> 2. PD 的调度指令怎么落到 TiKV 执行:心跳响应里下发 `change_peer`/`transfer_leader`/`split_region`,TiKV 收到后转成 Raft admin 命令。**PD 决策,TiKV 执行**(决策权与执行权分离,前章点过)。
> 3. TiKV 侧的 `split_controller`(`components/raftstore/src/store/worker/split_controller.rs`)怎么**在本地**识别热点 Region(基于 QPS/字节/CPU 阈值),并主动提议分裂——这是"load-base split",和 PD 下发的"size-base split"是两条不同的分裂触发路径。
> 4. 调度决策的"主体"在 PD 仓(Go),本地 tikv 仓只有"上报统计 + 执行指令 + 本地热点识别"三块——本章诚实标注边界。

> **如果一读觉得太难**:先只记住三件事——① PD 调度分两类:balance(均匀)和 hot region(热点);② 调度指令通过心跳响应下发,TiKV 执行;③ TiKV 本地有个 `split_controller` 看 QPS/CPU,识别热点 Region 自动分裂(load-base split)。

---

## 〇、一句话点破

> **PD 调度的本质是"用全局视图做贪心决策"——它看到所有 Region 的心跳统计,找出"最忙的 store 上的最热 Region",决定把它搬到"最闲的 store"或分裂成两个。决策在 PD(全局视图),执行在 TiKV(数据在本地)。而 load-base split 是个例外——TiKV 本地就能识别 QPS 热点并主动分裂,不必等 PD。**

这是结论,不是理由。本章倒过来拆:先讲为什么需要调度(集群是动态的)、两类调度(balance vs hot region)各自解决什么,再拆心跳响应怎么下发指令、TiKV 怎么执行,然后重点拆 TiKV 本地的 `split_controller`(load-base split 的算法),最后是架构演进和诚实边界。

---

## 一、为什么需要调度:集群是动态的

讲到这一章,我们已经知道:TiKV 把数据切成 Region(默认 256MB,8.3.0+ 的新默认,不再是老的 96MB),每个 Region 一个 Raft 组,副本分散在不同 store 上。**初始部署时**,这些 Region 是均匀分布的——PD 在创建 Region 时就会把副本打散到不同 store,避免单点。

但集群是**动态**的,均匀很快被打破:

1. **数据增长不均**:某些 Region 的 key 被频繁写入(比如订单表的自增主键段、监控指标的时间序列),它们长得比别的 Region 快,很快超出 256MB 需要分裂;分裂出来的新 Region 又可能挤在同一台机器上(因为分裂是 Region 内部行为,不改变副本所在 store)。
2. **负载不均**:某些 key 被疯狂读写(热点),比如配置表的某一行被所有请求读、计数器被并发写。这些热点 Region 所在的 store 会成为瓶颈,而其他 store 闲着。
3. **机器变动**:新机器加进来(扩容),但老数据不会自动搬过去;老机器要下线(缩容),它的 Region 要迁走;机器挂了(故障),它的副本要在别处补上。

这三种情况,都需要一个**主动的再平衡机制**——把数据从"忙/挤"的地方挪到"闲/空"的地方。这就是 PD 调度的根本任务。

> **不这样会怎样**:如果没有调度,集群会退化为"初始均匀,然后越来越歪"——热点 Region 持续压在一台机器上,这台机器 CPU/IO 打满,而别的机器闲着。**扩容也没意义**(新机器加进来,数据不会自己过去)。这正是 etcd 这种"一个 Raft 组"不会遇到的问题——etcd 数据量小、不分裂、无热点,所以不需要调度。**调度的必要性,是 multi-raft 的副产品**——Region 一多,分布就会不均,必须主动平衡。

---

## 二、两类调度:balance 与 hot region

PD 的调度(在 pd 仓的 `server/schedulers/`,不在本地)分两大类,各自解决不同问题:

### balance:把 Region/leader 均匀分布

**balance** 解决的是"长期不均"——副本数、leader 数、磁盘占用在 store 之间不均匀。它又分两种:

- **balance-region**:搬**整个 Region 的副本**。比如 store A 上有 1000 个 Region 副本,store B 上只有 500 个,PD 会把 A 上的某些 Region 副本搬到 B(通过 add learner → sync → remove old peer 的流程,后面拆)。
- **balance-leader**:搬**leader 角色**(不搬数据,只换 leader)。Raft 的 leader 是写入口,所有写都过 leader。如果某台机器上是太多 Region 的 leader,它就是写瓶颈。PD 会把 leader 转移到 leader 少的机器(通过 transfer leader,后面拆)。

两者的区别:**balance-region 搬数据(慢,要传 SST)**,**balance-leader 搢身份(快,只换 leader 不传数据)**。所以 PD 优先做 balance-leader(快),balance-region 慢慢做(避免影响业务)。

```
   balance-leader(快,换身份不搬数据)          balance-region(慢,搬副本)
   ┌─────────────┐                            ┌─────────────┐
   │  store A     │  leader 多                  │  store A     │  Region 副本多
   │  Region1★    │                            │  Region1(副本)│
   │  Region2★    │  ★=leader                  │  Region2(副本)│
   │  Region3★    │                            │  Region3(副本)│
   └──────┬──────┘                            └──────┬──────┘
          │ transfer leader                          │ add learner on B
          │ Region2 的 leader 转给 store B            │ sync 数据
          ▼                                          ▼
   ┌─────────────┐                            ┌─────────────┐
   │  store A     │  leader 少了                │  store A     │  少了 Region3
   │  Region1★    │                            │  Region1(副本)│
   │  Region3★    │                            │  Region2(副本)│
   └─────────────┘                            └─────────────┘
   ┌─────────────┐                            ┌─────────────┐
   │  store B     │  多了个 leader              │  store B     │  多了 Region3
   │  Region2★    │                            │  Region3(副本)│
   └─────────────┘                            └─────────────┘
```

### hot region:识别热点,搬走或分散

**hot region** 解决的是"突发热点"——某些 key 被疯狂读写(远超平均)。和 balance 不同,hot region 不是看"副本数/leader 数均不均",而是看**流量分布**(读字节、写字节、QPS、CPU)。

PD 通过心跳里的 `read_bytes`/`written_bytes`/`query_stats`/`cpu_stats`(前章拆过 `RegionStat`)识别热点。比如某个 Region 的 `read_bytes` 是平均值的 10 倍,PD 判定它是热点,会:

- **搬走热点 Region 的副本**:把热点 Region 挪到负载低的 store。
- **打散热点 leader**:如果热点是写,把 leader 转移到别的 store。
- **触发分裂**:如果热点集中在一小段 key range,分裂这段让它变成两个 Region,各放一个 store,流量减半。

hot region 调度比 balance 更精细——它不是简单地"均匀分布 Region 数",而是"按流量均匀分布负载"。一个集群可能 Region 数完全均匀,但某几个 Region 是热点,照样有瓶颈——这时只有 hot region 调度能解决。

> **钉死这两类的区别**:**balance 看"数量"(Region 数/leader 数均不均),hot region 看"流量"(字节/QPS/CPU 均不均)**。一个集群可能数量完全均匀但流量极不均(几个热点 Region),这时需要 hot region;也可能流量均匀但数量不均(扩容后老数据没搬),这时需要 balance。两类调度互补,PD 同时跑。

---

## 三、调度的依据:Region/Store 心跳上报的统计

PD 做调度决策,依据是心跳上报的统计(前章拆过心跳的源码)。这里重点讲 TiKV 侧怎么**收集和上报**这些统计——因为统计的质量直接决定调度的质量。

### PD Worker:`components/raftstore/src/store/worker/pd.rs`

TiKV 侧所有和 PD 相关的工作,都走一个叫 **PD Worker** 的后台线程。它的入口是 `Runner`(在 `pd.rs`),处理一系列 `Task`:

```rust
// components/raftstore/src/store/worker/pd.rs#L140-L200(简化,只列调度相关 Task)
pub enum Task<EK>
where
    EK: KvEngine,
{
    Heartbeat(HeartbeatTask),              // Region 心跳:把 Region 状态报给 PD
    StoreHeartbeat {                       // Store 心跳:把整台 TiKV 状态报给 PD
        stats: pdpb::StoreStats,
        report: Option<pdpb::StoreReport>,
        dr_autosync_status: Option<StoreDrAutoSyncStatus>,
    },
    ReadStats { read_stats: ReadStats },   // 读统计(用于热点识别)
    WriteStats { write_stats: WriteStats },// 写统计(用于热点识别)
    AutoSplit { split_infos: Vec<SplitInfo> },  // 本地识别的热点,触发自动分裂
    AskBatchSplit { ... },                 // Region 太大,问 PD 要新 id 分裂
    ReportBatchSplit { ... },              // 分裂完了,报告 PD
    ...
}
```

这个枚举列出了 PD Worker 干的所有事:**收 Region/Store 心跳并转发给 PD、收集读写统计、执行本地热点分裂、问 PD 要分裂 id**。它是个"TiKV ↔ PD 之间的桥梁"。

### Region 心跳的统计怎么累积

值得看一眼 `handle_heartbeat`——它不是简单地把数据转发给 PD,而是**先在本地累积差值**(两次心跳之间的增量),再发:

```rust
// components/raftstore/src/store/worker/pd.rs#L2581-L2620(简化)
Task::Heartbeat(hb_task) => {
    let approximate_size = match hb_task.approximate_size {
        Some(0) => 1,    // HACK: 0 表示未初始化, 1 表示空 Region
        Some(v) => v,
        None => 0,
    };
    ...
    let (read_bytes_delta, read_keys_delta, written_bytes_delta, ...) = {
        let region_id = hb_task.region.get_id();
        let mut region_peers = self.region_peers.write().unwrap();
        let peer_stat = region_peers.entry(region_id).or_default();
        peer_stat.approximate_size = approximate_size;
        peer_stat.approximate_keys = approximate_keys;

        // 算增量:本次上报 - 上次上报
        let read_bytes_delta = peer_stat.read_bytes - peer_stat.last_region_report_read_bytes;
        let written_bytes_delta = hb_task.written_bytes - peer_stat.last_region_report_written_bytes;
        ...
        // 更新"上次上报"基准
        peer_stat.last_region_report_read_bytes = peer_stat.read_bytes;
        ...
    };
    // 然后调 handle_heartbeat 把增量发给 PD
    ...
}
```

注意这里算的是 **delta(增量)**——不是把"从启动以来的总流量"发给 PD,而是"自上次心跳以来的增量"。这是个带宽优化:避免心跳包越来越大(总流量只会增不会减)。PD 拿到增量后,自己累加成"近期流量"用于调度决策。

> **钉死这件事**:Region 心跳上报的是**增量流量**(两次心跳之间新产生的读写),不是总量。这是个经典的"差值编码"优化——降低心跳带宽,代价是 PD 要自己累加(丢失一次心跳就要等下一次重新校准)。`region_peers` 这个 HashMap 缓存了每个 Region 的"上次上报基准",增量就是当前 - 基准。

---

## 四、PD 下发指令:心跳响应里的四种调度

前一章点过"PD 通过心跳响应下发调度指令",这里拆透。TiKV 侧收响应的入口是 `schedule_heartbeat_receiver`(在 `pd.rs`),它注册一个回调,PD 每发一个 `RegionHeartbeatResponse`,回调被触发:

```rust
// components/raftstore/src/store/worker/pd.rs#L1888-L1975(简化,保留四种调度分支)
fn schedule_heartbeat_receiver(&mut self) {
    let router = self.router.clone();
    let fut = self.pd_client
        .handle_region_heartbeat_response(self.store_id, move |mut resp| {
            let region_id = resp.get_region_id();
            let epoch = resp.take_region_epoch();
            let peer = resp.take_target_peer();

            // 分支一:change_peer(加减副本,做 balance-region)
            if resp.has_change_peer() {
                PD_HEARTBEAT_COUNTER_VEC.with_label_values(&["change peer"]).inc();
                let mut change_peer = resp.take_change_peer();
                let req = new_change_peer_request(
                    change_peer.get_change_type(),
                    change_peer.take_peer(),
                );
                send_admin_request(&router, region_id, epoch, peer, req, Callback::None, ...);
            }
            // 分支二:change_peer_v2(联合共识,一次性改多个副本)
            else if resp.has_change_peer_v2() {
                let mut change_peer_v2 = resp.take_change_peer_v2();
                let req = new_change_peer_v2_request(change_peer_v2.take_changes().into());
                send_admin_request(&router, region_id, epoch, peer, req, Callback::None, ...);
            }
            // 分支三:transfer_leader(换 leader,做 balance-leader)
            else if resp.has_transfer_leader() {
                PD_HEARTBEAT_COUNTER_VEC.with_label_values(&["transfer leader"]).inc();
                let mut transfer_leader = resp.take_transfer_leader();
                let req = new_transfer_leader_request(
                    transfer_leader.take_peer(),
                    transfer_leader.take_peers().into(),
                );
                send_admin_request(&router, region_id, epoch, peer, req, Callback::None, ...);
            }
            // 分支四:split_region(分裂,PD 主动让分裂)
            else if resp.has_split_region() {
                PD_HEARTBEAT_COUNTER_VEC.with_label_values(&["split region"]).inc();
                let mut split_region = resp.take_split_region();
                let msg = if split_region.get_policy() == pdpb::CheckPolicy::Usekey {
                    CasualMessage::SplitRegion { ... }    // 按指定 key 分裂
                } else {
                    CasualMessage::HalfSplitRegion { ... } // 从中间分裂
                };
                router.send(region_id, PeerMsg::CasualMessage(Box::new(msg)));
            }
        });
    ...
}
```

这四个分支,就是 PD 能下发的**四种调度指令**:

1. **`change_peer`(加减副本)**:做 balance-region。比如"在 store C 上给 Region X 加一个副本"(`ChangePeerType::AddNode`),或"把 store A 上的 Region X 副本删掉"(`ChangePeerType::RemoveNode`)。这通过 Raft 的联合共识(joint consensus)完成——加新副本 → 等新副本追平日志 → 切换配置 → 删老副本。承接《etcd》的 Raft 配置变更。
2. **`change_peer_v2`(联合共识,多步)**:一次性改多个副本(比如从 3 副本改 5 副本)。比 v1 更通用。
3. **`transfer_leader`(换 leader)**:做 balance-leader。把 leader 角色从当前 peer 转给另一个 peer。这是个轻量操作——不搬数据,只让老 leader 发一个 TimeoutNow 给目标 peer,目标 peer 立刻发起选举成为新 leader。承接《etcd》的 Raft leader 转移。
4. **`split_region`(分裂)**:PD 让 Region 分裂。有两种 policy——`Usekey`(按 PD 指定的 key 分裂,PD 想切哪就切哪)和 `HalfSplitRegion`(从中间切,适合 PD 不知道具体热点在哪、只想把 Region 一分为二)。

每个分支最后都调 `send_admin_request` 或 `router.send`,把指令转成 **Raft admin 命令**发给对应的 Region leader。这一点非常关键——**PD 的指令最终变成 Raft 提议**,走和普通写一样的 Raft 流程(Propose → Replicate → Commit → Apply)。这意味着:

- 调度指令也是 Raft 复制的(多数派确认才生效),不会因为某个副本挂了就不一致。
- 调度指令和业务写在同一条 Raft 日志里,按序 apply,不会冲突。

> **钉死这件事**:PD 的调度指令**不是直接操作数据**,而是变成 Raft admin 命令(`ChangePeer`/`TransferLeader`/`Split`)发给 Region leader,走标准 Raft 流程生效。这是"决策权(PD)和执行权(TiKV 的 Raft 组)分离"的精髓——PD 不知道数据细节,只发指令;TiKV 的 Raft 组执行指令,保证一致。**PD 和 TiKV 的边界,就在 `send_admin_request` 这一步**。

---

## 五、TiKV 本地的热点识别:`split_controller` 和 load-base split

前面讲的 balance 和 hot region,**决策都在 PD**(pd 仓)。但 TiKV 有一项调度是**本地决策**的——**load-base split**(基于负载的分裂)。这一节拆这个 TiKV 侧的精巧机制。

### 为什么需要本地决策的分裂

PD 的热点识别有个天然延迟——它依赖 Region 心跳(默认几秒一次),心跳到了 PD,PD 算热点,下发分裂指令,这中间至少几秒。对于突发热点(某个 key 突然被疯狂打),这几秒可能就让这台 TiKV 打满 CPU。

更关键的是,**PD 看到的统计是 Region 粒度**——它知道"Region X 流量很大",但不知道**热点在 Region 内部的哪段 key**。比如一个 256MB 的 Region,可能热点集中在头部 1MB(自增主键)。PD 想分裂,但不知道该从哪个 key 切——它没有 key 粒度的访问分布。

这就是 TiKV 本地 `split_controller` 解决的两个问题:**更快地识别热点(本地统计,不必等心跳)+ 知道热点在 Region 内部的具体 key 范围(用于精确切分)**。

### `AutoSplitController`:本地热点识别器

`components/raftstore/src/store/worker/split_controller.rs` 的 `AutoSplitController` 是核心。它周期性地(`flush` 方法,默认 10 秒一次)收集所有 Region 的读写统计,识别热点,提议分裂:

```rust
// components/raftstore/src/store/worker/split_controller.rs#L763-L870(简化,核心逻辑)
pub fn flush(
    &mut self,
    ctx: &mut AutoSplitControllerContext,
    read_stats_receiver: &Receiver<ReadStats>,
    cpu_stats_receiver: &Receiver<Arc<RawRecords>>,
    thread_stats: &mut ThreadInfoStatistics,
    split_validator: &SplitValidator,
) -> (Vec<usize>, Vec<SplitInfo>) {
    let mut top_qps = BinaryHeap::with_capacity(TOP_N);
    let region_infos_map = Self::collect_read_stats(ctx, read_stats_receiver);  // 收集读统计
    let region_cpu_map = self.collect_cpu_stats(ctx, cpu_stats_receiver);       // 收集 CPU 统计
    ...
    let mut split_infos = vec![];
    for (region_id, region_infos) in region_infos_map {
        if split_validator.is_disabled(region_id) { continue; }

        // 算这个 Region 的总 QPS 和字节数
        let qps_prefix_sum = prefix_sum(region_infos.iter(), RegionInfo::get_read_qps);
        let qps = *qps_prefix_sum.last().unwrap();
        let byte = region_infos.iter().fold(0, |flow, ri| flow + ri.flow.read_bytes);
        ...
        let (cpu_usage, hottest_key_range) = region_cpu_map.get(&region_id)...;

        // ★ 核心判断:QPS/字节/CPU 是否超阈值
        if qps < self.cfg.qps_threshold()
            && byte < self.cfg.byte_threshold()
            && (!is_unified_read_pool_busy || !is_region_busy)
        {
            self.recorders.remove_entry(&region_id);   // 不够热,清掉记录
            continue;
        }
        // 够热!记录这个 Region 的 key 访问分布
        LOAD_BASE_SPLIT_EVENT.load_fit.inc();
        let detect_times = self.cfg.detect_times;
        let recorder = self.recorders.entry(region_id)
            .or_insert_with(|| Recorder::new(detect_times));
        recorder.update_peer(&region_infos[0].peer);
        recorder.update_cpu_usage(cpu_usage);
        if let Some(hottest_key_range) = hottest_key_range {
            recorder.update_hottest_key_range(hottest_key_range);
        }

        // 采样 key range(水库采样)
        let key_ranges = sample(self.cfg.sample_num, region_infos, RegionInfo::get_key_ranges_mut);
        recorder.record(key_ranges);

        // 累计 detect_times 次(默认 10 秒)持续够热,才真的提议分裂
        if recorder.is_ready() {
            let key = recorder.collect(&self.cfg);   // 算出分裂 key
            if !key.is_empty() {
                info!("load base split region";
                    "region_id" => region_id, "qps" => qps, ...);
                split_infos.push(SplitInfo { region_id, split_key: Some(key), ... });
            }
        }
    }
    ...
}
```

这段代码很长,但逻辑清晰。逐步拆:

**第一步:收集统计。** `collect_read_stats` 从 `read_stats_receiver` 收集所有 Region 的读 QPS、读字节、key range 访问记录;`collect_cpu_stats` 收集 CPU 统计(来自线程级的 CPU 采样,能精确到"哪个 Region 占了多少 CPU")。

**第二步:判断是否够热。** 核心是三个阈值(默认值在 `split_config.rs`):

```rust
// components/raftstore/src/store/worker/split_config.rs#L17-L20
pub const DEFAULT_QPS_THRESHOLD: usize = 3000;          // QPS 超 3000 才算热
pub const DEFAULT_BIG_REGION_QPS_THRESHOLD: usize = 7000; // 大 Region 阈值更高
pub const DEFAULT_BYTE_THRESHOLD: usize = 30 * 1024 * 1024; // 30MB/s
pub const DEFAULT_BIG_REGION_BYTE_THRESHOLD: usize = 100 * 1024 * 1024; // 100MB/s
```

一个 Region 要被判定为"热点",要满足:**QPS 超 3000 或字节超 30MB/s,且 Unified Read Pool 或该 Region 自己 CPU 够忙**。这是个**双重确认**——不是只看流量,还要看 CPU 是否真的被这个 Region 压满了(防止误判:有些 Region 流量大但 CPU 不忙,比如纯 IO 读)。

**第三步:采样 key range。** 这是最精巧的部分。`split_controller` 不是简单地"把 Region 一分为二",而是想知道**热点集中在 Region 内部的哪段 key**。它通过**水库采样(reservoir sampling)**记录访问过的 key range:

```rust
// components/raftstore/src/store/worker/split_controller.rs#L368-L383(简化,add_key_ranges)
fn add_key_ranges(&mut self, key_ranges: Vec<KeyRange>) {
    for (i, key_range) in key_ranges.into_iter().enumerate() {
        let n = self.get_read_qps() + i;
        if n == 0 || self.key_ranges.len() < self.sample_num {
            self.key_ranges.push(key_range);        // 还没采够 sample_num 个,直接放
        } else {
            let j = rand::thread_rng().gen_range(0..n);
            if j < self.sample_num {
                self.key_ranges[j] = key_range;     // 水库采样:以 sample_num/n 概率替换
            }
        }
    }
}
```

水库采样的精妙:**它能从一个超长的 key range 流中,等概率地采样出固定数量(sample_num,默认 20)个样本**——不需要记住所有访问过的 key range(那会爆内存),只需要 sample_num 个槽位。采样结果是"访问分布的等概率代表",后续用它推断热点集中在哪里。

**第四步:累计 detect_times 次持续够热,才提议分裂。** 这是个**防抖动**机制——一次够热不够(可能是瞬时尖峰),要连续 `detect_times`(默认 10)次都够热,才真的提议分裂。`Recorder` 累积这 10 次的采样,然后 `collect` 算出"在哪个 key 切"。

### `Recorder::collect`:怎么算出分裂 key

`collect` 方法是 load-base split 的算法核心:

```rust
// components/raftstore/src/store/worker/split_controller.rs#L317-L366(简化)
fn collect(&self, config: &SplitConfig) -> Vec<u8> {
    // 对累积的 key_ranges 做二次采样
    let sampled_key_ranges = sample(config.sample_num, self.key_ranges.clone(), |x| x);
    let mut samples = Samples::from(sampled_key_ranges);
    ...
    // 评估每个采样的 key range,算出能"平衡"地切开的 key
    recorded_key_ranges.into_iter().for_each(|key_range| {
        samples.evaluate(key_range);
    });
    // 根据 split_balance_score 和 split_contained_score 决定分裂 key
    samples.split_key(config.split_balance_score, config.split_contained_score)
}
```

`split_key` 的目标是找一个分裂点,让**切出来的两半流量尽量均衡**(`split_balance_score` 默认 0.8,即左右两半的流量差不超过 20%)。同时避免"把一个连续的访问模式切断"(`split_contained_score` 默认 0.5)。这两个分数是平衡性和局部性的权衡——既要把热点切开,又不能切得太碎破坏访问局部性。

> **钉死这件事**:load-base split 的精妙在于**它知道热点在 Region 内部的具体 key 范围**(通过水库采样),而不只是"Region 流量大"。这让 TiKV 能精确地切在"热点的边界"上,把热点单独切出来变成新 Region,再由 PD 搬到别的 store。这是 TiKV 调度最精细的地方——**PD 只知道 Region 粒度,而 TiKV 本地知道 key 粒度**。

### 分裂提议怎么执行:走 PD 要 id

`split_controller` 算出分裂 key 后,生成 `SplitInfo` 发给 PD Worker:

```rust
// components/raftstore/src/store/worker/pd.rs#L581-L640(简化,StoreStatsReporter trait 方法)
impl<EK> StoreStatsReporter for WrappedScheduler<EK> {
    fn collect_metrics(&self, ...) {
        // 周期性触发,内部会调 AutoSplitController::flush
        ...
    }
    fn report_split_infos(&self, split_infos: ...) {
        let task = Task::AutoSplit { split_infos };
        self.0.schedule(task).ok();
    }
}
```

`Task::AutoSplit` 在 PD Worker 的 `run` 里被处理:

```rust
// components/raftstore/src/store/worker/pd.rs#L2520-L2560(简化)
Task::AutoSplit { split_infos } => {
    let f = async move {
        for split_info in split_infos {
            let Ok(Some(region)) = pd_client.get_region_by_id(split_info.region_id).await else { continue; };
            if let Some(split_key) = split_info.split_key {
                // 走标准的 ask_batch_split 流程(PD 给新 region_id)
                Self::handle_ask_batch_split(
                    ..., region, vec![split_key], ..., "auto_split", ..., pdpb::SplitReason::Load,
                );
            } else if split_info.start_key.is_some() && split_info.end_key.is_some() {
                // 没有明确 split_key,从 key range 中点切
                ...HalfSplitRegion...
            }
        }
    };
}
```

注意:**即使 load-base split 是 TiKV 本地识别的,执行分裂还是要走 PD 要新 region_id**。这是因为 Region 的 id 必须全局唯一(前章讲过),只有 PD 能发。所以流程是:**TiKV 本地识别热点 → 算出分裂 key → 问 PD 要新 id → PD 给 id → TiKV 执行分裂 → 报告 PD**。`SplitReason::Load` 这个标记告诉 PD"这是负载触发的分裂",PD 会据此做不同的统计和处理。

> **钉死这条链路**:**load-base split 是个"TiKV 识别 + PD 授权 + TiKV 执行"的混合流程**。TiKV 识别热点和算分裂 key(本地能做,因为有 key 粒度统计),但新 region_id 必须 PD 发(全局唯一)。这种分工把"局部智能"和"全局协调"结合——TiKV 能快速响应本地热点,PD 保证全局 id 唯一。

---

## 六、两类分裂:size-base vs load-base

讲到这,有必要把 TiKV 的两条分裂触发路径对清楚——这是容易混淆的点:

### size-base split:Region 太大就切(经典)

**触发**:Region 的 `approximate_size` 超过阈值(默认 256MB,8.3.0+ 新默认)。
**谁判断**:`split_check` worker(`store/worker/split_check.rs`)周期性扫描 Region 大小,超阈值就提议分裂。
**目的**:防止单个 Region 过大,导致迁移/Snapshot 开销大、Compaction 慢。
**切哪里**:通常从 Region 中点切(`HalfSplitRegion`),因为目的是"切小",不在意精确切在热点边界。

### load-base split:Region 流量大就切(本章重点)

**触发**:Region 的 QPS/字节/CPU 超阈值(默认 QPS 3000、字节 30MB/s)。
**谁判断**:`split_controller`(本章拆的)周期性收集统计,超阈值且持续够久就提议分裂。
**目的**:把热点 Region 切散,让流量能分布到多个 store。
**切哪里**:精确切在热点边界(通过水库采样算出),让热点单独成新 Region。

两者的关键区别:**size-base 看"大小"(切小),load-base 看"流量"(切散)**。一个 256MB 的 Region 可能流量很小(冷数据),不需要 load-base split;一个 50MB 的 Region 可能流量爆表(热点),不需要 size-base split 但急需 load-base split。两者互补,共同保证 Region 既不太大也不太热。

> **钉死这件事**:TiKV 有**两条分裂路径**——size-base(防大,中点切)和 load-base(防热,精确切)。前者是经典机制(P2-08 拆过),后者是 5.x 引入的针对热点的优化。**搞混这两条是初学者常犯的错**——它们由不同的 worker 触发、用不同的算法、服务不同的目的。

---

## 七、技巧精解:水库采样(load-base split 的核心算法)

本章挑一个最值得单独拆透的技巧:**水库采样(reservoir sampling)**——load-base split 识别热点 key range 的核心算法。这个算法看似简单,但它解决了一个"在无限流上等概率采样"的难题,是 TiKV 调度精细化的数学基石。

### 朴素做法会撞什么墙

`split_controller` 要识别"热点集中在 Region 内部的哪段 key"。最朴素的做法是**记录所有访问过的 key range**——每来一个请求,就把它的 key range 加到一个列表里,统计完看哪段最热。

这个朴素做法立刻撞墙:

- **内存爆炸**:一个热点 Region 每秒几万次访问,每次访问产生 1~N 个 key range。记录所有访问,内存每秒涨几 MB,几小时就 OOM。
- **统计失真**:如果你只记前 N 个(早期访问),会漏掉后续的热点转移(热点从 key A 段移到 key B 段)。

所以需要一种算法:**从无限流中,等概率地采样固定数量(比如 20 个)的样本,且每个访问被采到的概率相等**——这就是水库采样。

### 水库采样的算法

水库采样的经典算法(Algorithm R):

1. 前 `k` 个样本(`k = sample_num`,默认 20),直接放进水库。
2. 第 `i` 个样本(`i > k`),以 `k/i` 的概率替换水库里的一个随机样本。

数学上可以证明:流结束时,**每个样本出现在水库里的概率都是 `k/n`**(n 是总样本数)——不管它在流的哪个位置。这正是"等概率采样"。

看 TiKV 的实现(简化):

```rust
// components/raftstore/src/store/worker/split_controller.rs#L368-L383(简化)
fn add_key_ranges(&mut self, key_ranges: Vec<KeyRange>) {
    for (i, key_range) in key_ranges.into_iter().enumerate() {
        let n = self.get_read_qps() + i;       // n = 当前已处理的样本数
        if n == 0 || self.key_ranges.len() < self.sample_num {
            self.key_ranges.push(key_range);    // 前 sample_num 个,直接放
        } else {
            let j = rand::thread_rng().gen_range(0..n);   // 生成 [0, n) 的随机数
            if j < self.sample_num {
                self.key_ranges[j] = key_range;            // 以 sample_num/n 概率替换
            }
        }
    }
}
```

`rand::thread_rng().gen_range(0..n)` 生成 `[0, n)` 的随机整数,它 `< sample_num` 的概率正是 `sample_num / n`。所以这段代码就是经典水库采样的实现——**用固定 `sample_num` 个槽位,等概率代表无限流**。

### 为什么这个算法妙

这个算法解决了一个看似不可能的问题:**有限内存,无限流,等概率采样**。它的妙处在于:

1. **内存恒定**:不管流多长,水库永远只有 `sample_num` 个槽位。TiKV 跑几个月,内存占用不变。
2. **等概率**:早期访问和晚期访问被采到的概率相等(`k/n`),所以采样结果**无偏**地代表整个访问分布。如果热点在晚期出现,它和早期访问一样有机会被采到。
3. **在线**:不需要预先知道流的总长度(这正是 TiKV 的场景——它不知道接下来会有多少请求)。

> **不这么写会怎样**:朴素地记所有访问,内存爆;只记前 N 个,统计失真(漏掉热点转移);按时间窗口记,窗口边界附近丢失样本。水库采样是唯一"内存恒定 + 无偏 + 在线"的解法。这是分布式系统里处理"无限流采样"的标准答案,TiKV 用它来推断热点分布,是数学优雅和工程实用的完美结合。

### 采样结果怎么用:推断热点边界

有了水库采样的 `sample_num` 个 key range(等概率代表整个访问流),`collect` 方法(前面拆过)对它们做**二次评估**:统计每个 key 范围被访问的次数,找出"访问最密集的边界",作为分裂 key。这保证了:**分裂切在"访问模式的自然边界"上**,切出来的两半流量尽量均衡。

水库采样给的是"无偏样本",`collect` 用这些样本算"最优切分点"——两层算法叠加,完成了"从海量访问中找出该从哪切"的难题。

---

## 八、PD 服务端的调度算法(诚实标注:不在本地)

本章重点拆了本地能看到的:心跳上报、指令执行、`split_controller`。但**调度的决策主体在 PD 服务端**,在独立的 `tikv/pd` 仓(Go 写)。这里诚实标注边界,简单介绍 PD 那边在做什么(可对照官方源码):

- **balance scheduler**(`server/schedulers/balance.go`):实现 balance-region 和 balance-leader。算法是贪心——每次找"最忙的 store 上最适合搬的 Region",搬到"最闲的 store"。搬的"适合度"综合考虑 Region 大小、store 剩余空间、副本约束(同 Region 副本不能在同 store)。
- **hot region scheduler**(`server/schedulers/hot_region.go`):实现热点调度。它用热度排名(每个 Region 的流量 / 所有 Region 平均流量)找热点,然后搬走或打散。比 balance 更精细——考虑流量方向(读热 vs 写热要分别均衡)。
- **replica checker**(`server/cluster/replica_checker.go`):保证副本数符合配置(默认 3 副本)。某副本 down 了,它会触发补副本。
- **rule checker**(`server/cluster/rule_checker.go`):支持 placement rules(按 key range 配置副本放哪,比如热数据放 SSD、冷数据放 HDD)。

这些都不在本地 clone 的 tikv 仓。本章拆的是"tikv 侧怎么配合"——上报统计(`pd.rs` 的 heartbeat)、执行指令(`schedule_heartbeat_receiver` 转成 Raft 命令)、本地识别热点(`split_controller`)。

> **钉死边界**:PD 调度决策在 pd 仓,tikv 仓只有协作。**涉及"为什么搬这个 Region 到那个 store"的算法逻辑,看 pd 仓的 `server/schedulers/`**;涉及"tikv 怎么知道要搬、怎么执行"的源码,看本章拆的本地文件。

---

## 九、架构演进:9.x 的调度相关变化

最后交代几个 8.x/9.x 和调度相关的演进:

1. **基于 CPU 的热点调度(8.x/9.x 重点)**:老版本只看流量(字节/QPS)识别热点,但现代 TiKV 瓶颈常常在 Coprocessor 计算(CPU)而非 IO。9.x 引入了 `cpu_stats`(`RegionStat` 里新字段,前章讲过),`split_controller` 也加了 `collect_cpu_stats` 收集线程级 CPU 占用,精确到"哪个 Region 占了多少 CPU"。这让热点识别从"流量维度"进化到"CPU 维度"——更准地找到真正压垮机器的 Region。

2. **`split_validator`**:9.x 加的机制。PD 可以通过心跳响应的 `change_split.auto_split_enabled` 字段,**允许或禁止某个 Region 的 load-base split**(`split_controller.rs` 的 `SplitConfigChange::UpdateRegionCpuCollector`)。这给了 PD 一个"开关"——当集群在特殊状态(比如滚动升级、大范围迁移)时,PD 可以临时关掉 TiKV 的自动分裂,避免雪崩。

3. **bucket(子 Region 分片,8.x)**:Region 内部进一步切成 bucket(桶),每个 bucket 单独统计流量。PD 调度可以基于 bucket 粒度——比 Region 更细,但比 key 更粗。这是个粒度甜点:bucket 数量可控(一个 Region 几十个 bucket),不像 key 粒度爆炸,又能识别 Region 内部的局部热点。`RegionStat` 里有 `report_region_buckets` 方法上报 bucket 统计。

4. **resource_control 与调度联动(8.x)**:资源管控(resource group)和调度联动——PD 调度时考虑 resource group 的 RU 配额,避免把高 RU 消耗的 Region 都堆在一个 store。这是调度从"看流量"扩展到"看资源消耗"。

> **演进趋势**:调度从"看数量(balance)→ 看流量(hot region)→ 看 CPU/资源(resource_control)"层层精细化。每一层都让 TiKV 能更准确地找到"该搬什么、搬到哪"。但核心机制没变:**心跳上报统计 + PD 决策 + TiKV 执行**。

---

## 十、章末小结

### 回扣主线

本章是第 5 篇的收尾。回到二分法:**PD 调度主要服务复制层**——它决定 Region 的副本放哪、leader 谁当、热点怎么分散,这些都是"数据怎么存、怎么扩展"的复制层问题(虽然 PD 自己横跨两边)。load-base split 是个特殊存在——它在 TiKV 本地决策(部分复制层智能下沉),但执行还是要 PD 授权 id。

本章的设计可以浓缩成两句话:**PD 用全局视图做 balance/hot region 决策,通过心跳响应下发 Raft admin 命令让 TiKV 执行;load-base split 是个例外,TiKV 本地用水库采样识别热点 key range,主动提议分裂,但 id 仍从 PD 拿**。

### 五个为什么

1. **为什么 TiKV 需要调度?**——集群是动态的(数据增长不均、热点、机器变动),初始均匀很快被打破,必须主动再平衡。这是 multi-raft 的副产品——Region 一多,分布就会不均。
2. **balance 和 hot region 有什么区别?**——balance 看"数量"(Region/leader 数均不均),解决长期不均;hot region 看"流量"(字节/QPS/CPU 均不均),解决突发热点。一个集群可能数量均匀但流量极不均(几个热点),需要 hot region。
3. **PD 的调度指令怎么执行?**——通过心跳响应下发 `change_peer`/`transfer_leader`/`split_region`,TiKV 收到后转成 Raft admin 命令走标准 Raft 流程。决策权(PD)和执行权(TiKV 的 Raft 组)分离。
4. **为什么需要 load-base split(TiKV 本地决策)?**——PD 的心跳有几秒延迟,且只看 Region 粒度;TiKV 本地能更快识别热点,且通过水库采样知道热点在 Region 内部的具体 key 范围,能精确切开。
5. **水库采样为什么是 load-base split 的核心?**——它用恒定内存(`sample_num` 个槽位),从无限访问流中等概率采样,无偏地代表整个访问分布。这是"在有限内存下处理无限流"的标准数学解法。

### 想继续深入往哪钻

- **PD 服务端的调度算法**:在 `tikv/pd` 仓的 `server/schedulers/`(`balance.go`/`hot_region.go`)、`server/cluster/`(`replica_checker.go`/`rule_checker.go`)。**不在本地 clone 的 tikv 仓**。
- **本地 split_controller**:`components/raftstore/src/store/worker/split_controller.rs`,本章拆了 `flush`/`Recorder`/水库采样,可继续看 `collect_cpu_stats`(CPU 维度热点识别)。
- **PD Worker 全貌**:`components/raftstore/src/store/worker/pd.rs`,本章拆了心跳、指令执行、AutoSplit,可继续看 `handle_store_heartbeat`(Store 心跳怎么处理 PD 响应)。
- **承接《etcd》**:`change_peer`/`transfer_leader` 这些 Raft 配置变更和 leader 转移的算法本体,承接《etcd》那本的 Raft 章节(联合共识、leader 转移)。
- **bucket 机制**:8.x 引入的 Region 内子分片,可看 `kvproto` 的 `BucketStat` 和 tikv 的 `report_region_buckets`。

### 引出下一章

我们搞清了 PD 的三件职责(TSO、调度、ID 分配),拆透了 TSO 和 resolved_ts(P5-17)、balance 和热点(P5-18)。第 5 篇到这里就结束了。下一章我们进入第 6 篇——**生产特性**:Coprocessor(把 SQL 计算下推到 TiKV 执行,省网络)、GC(MVCC 老版本回收)、悲观锁与 CDC。这些都是 TiKV 在生产环境里"不光能跑、还要跑得好用"的关键。

> **下一章**:[P6-19 · Coprocessor:把计算下推](P6-19-Coprocessor-把计算下推.md)
