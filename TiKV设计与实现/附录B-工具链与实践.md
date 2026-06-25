# 附录 B · 工具链与实践

> 本附录帮读者把书里的知识落到**动手排查**上。内容分三块:① TiKV 生产工具链(`tikv-ctl` / `pd-ctl` / TiUP / Grafana);② 本书与四本前作的承接关系说明(想深入某块去读前作哪章);③ 常见线上问题排查清单(症状 → 根因 → 排查命令,每条对应本书某章的机制)。

> **版本说明**:本附录的子命令示例基于 `tikv @ 852b977 (9.0.0-beta.2)`。`tikv-ctl` / `pd-ctl` 的确切 flag 随版本演进,实际使用时**以你安装的版本的 `--help` 输出为准**——本附录只给典型用法和思路,不保证每个 flag 在你的版本完全一致。子命令名称来自 [`cmd/tikv-ctl/src/cmd.rs`](../tikv/cmd/tikv-ctl/src/cmd.rs) 的 `Cmd` 枚举(L111),供核对。

---

## 一、工具链速览

| 工具 | 干什么 | 典型场景 |
|------|--------|----------|
| `tikv-ctl` | 直接和单个 TiKV 节点 / RocksDB / Raft 状态交互 | 看 Region / MVCC / 锁 / RocksDB / 强制 compact / 闪回 |
| `pd-ctl` | 和 PD 集群交互(Region 分布 / 调度 / TSO / GC) | 看集群拓扑、调度任务、safe point、store 健康 |
| TiUP | 部署 / 升级 / 扩缩容 TiKV 集群 | 生产部署的标准方式 |
| Grafana | 看监控面板 | raftstore / scheduler / GC 延迟 / 锁等待 |

> 这四个工具配合用:`pd-ctl` 看全局(PD 视角),`tikv-ctl` 钻进单节点(RocksDB / Raft 细节),Grafana 看趋势,TiUP 管生命周期。排查问题时通常先用 Grafana / `pd-ctl` 定位是哪类问题(Region 不均 / 写延迟 / 锁 / GC),再用 `tikv-ctl` 钻进具体节点验证。

---

## 二、tikv-ctl 常用子命令

`tikv-ctl` 的子命令在 [`cmd/tikv-ctl/src/cmd.rs`](../tikv/cmd/tikv-ctl/src/cmd.rs#L111) 的 `Cmd` 枚举里定义。下面挑本书涉及的常用子命令讲典型用法。

> **两种执行模式**:`tikv-ctl` 可以① 直接连 PD(`--pd <addr>`,走 gRPC,只读为主);② 直接连单节点数据目录(`--data-dir <path>`,离线,能改数据,危险)。生产排查优先用 PD 模式(只读、安全);改数据(如强制 compact、tombstone)才用 data-dir 模式,且需停 TiKV。

### 2.1 Region 相关

**`size`**([L118](../tikv/cmd/tikv-ctl/src/cmd.rs#L118)):看一个 Region 的大小(各 CF)。
```
tikv-ctl --pd <pd-addr> size -r <region-id>
```
> 对应机制:本书 [P1-02](P1-02-Region-把海量key切成一段段.md)(Region 大小)、[P2-08](P2-08-Region分裂迁移与Snapshot.md)(split_check 用它判断该不该分裂)。默认 Region 256MB(v1)/ 10GB(v2),超过 `region_max_size`(默认 split_size × 1.5)触发分裂。

**`region`**([L813](../tikv/cmd/tikv-ctl/src/cmd.rs#L813)):看一个 Region 的元信息(范围 / 副本 / Leader / RegionEpoch)。
```
tikv-ctl --pd <pd-addr> region -r <region-id>
```

**`split-region`**([L516](../tikv/cmd/tikv-ctl/src/cmd.rs#L516)):手动分裂一个 Region(通常让 PD 自动做,排查热点时偶尔手动)。
```
tikv-ctl --pd <pd-addr> split-region -r <region-id> -k <split-key>
```

### 2.2 MVCC 和锁相关

**`mvcc`**([L218](../tikv/cmd/tikv-ctl/src/cmd.rs#L218)):看一个 key 的 MVCC 各版本(default / write / lock CF)。
```
tikv-ctl --pd <pd-addr> mvcc -k <key>         # 看单 key
tikv-ctl --pd <pd-addr> mvcc -k <key> --cf lock # 只看 lock CF
```
> 对应机制:本书 [P3-10](P3-10-MVCC编码-key加时间戳.md)(key+ts 编码、三 CF)、[P4-13](P4-13-Prewrite预写-选Primary加锁.md)(lock CF 里的 Primary 字节)、[P4-15](P4-15-MVCC读取与锁的解决.md)(读时遇锁)。**排查锁等待 / 事务卡住时,这是看锁状态的首选命令**——能看到 `Lock` 结构的 `primary`(Primary key 字节)、`lock_type`、`for_update_ts`(悲观锁)。

**`scan`**([L134](../tikv/cmd/tikv-ctl/src/cmd.rs#L134)):扫一个 key range 的 MVCC(可过滤 start_ts / commit_ts / cf)。
```
tikv-ctl --pd <pd-addr> scan --from <start-key> --to <end-key> --limit 100
```

**`raw-scan`** / **`raw-get`**([L172](../tikv/cmd/tikv-ctl/src/cmd.rs#L172)、[L202](../tikv/cmd/tikv-ctl/src/cmd.rs#L202)):看 RawKV(非事务)的原始 key。

### 2.3 RocksDB 相关

**`compact`**([L278](../tikv/cmd/tikv-ctl/src/cmd.rs#L278)):强制 compact 一个 CF 的指定 range(手动触发,排查 GC / 空间放大问题时用)。
```
tikv-ctl --data-dir <path> compact -c default  # compact default CF
tikv-ctl --data-dir <path> compact -r <region-id>  # compact 一个 Region
```
> 对应机制:本书 [P3-09](P3-09-RocksDB引擎-LSM-tree与Column-Family.md)(CF)、[P6-20](P6-20-GC与flashback-MVCC老版本回收.md)(compaction filter 在 compact 时清过期版本)。**承接《LevelDB》**:Compaction 原理在前作。

**`compact-cluster`**([L451](../tikv/cmd/tikv-ctl/src/cmd.rs#L451)):对整个集群的所有 TiKV 触发 compact。

**` RocksDB` 属性 / properties**:`region-properties`([L500](../tikv/cmd/tikv-ctl/src/cmd.rs#L500))、`range-properties`([L506](../tikv/cmd/tikv-ctl/src/cmd.rs#L506))看 SST 的 TableProperties(本书 [P6-20](P6-20-GC与flashback-MVCC老版本回收.md) 讲的 `check_need_gc` 用它判断该不该 GC)。

### 2.4 Raft 相关

**`raft`**([L113](../tikv/cmd/tikv-ctl/src/cmd.rs#L113)) / **`log`**([L793](../tikv/cmd/tikv-ctl/src/cmd.rs#L793)):看一条 Raft 日志 entry。
```
tikv-ctl --data-dir <path> raft log -r <region-id> -i <index>
```
> 对应机制:本书 [P2-06](P2-06-Raft日志存储-RaftEngine.md)(RaftEngine 存日志)。**承接《etcd》**:Raft 日志格式在前作。

**`raft-engine-ctl`**([L571](../tikv/cmd/tikv-ctl/src/cmd.rs#L571)):直接操作 RaftEngine 文件(9.x 默认日志引擎)。

**`consistency-check`**([L423](../tikv/cmd/tikv-ctl/src/cmd.rs#L423)):强制对一个 Region 做一致性检查(排查副本不一致)。

### 2.5 调试和恢复

**`bad-regions`**([L429](../tikv/cmd/tikv-ctl/src/cmd.rs#L429)):列出有问题的 Region(副本不一致 / 落后等)。

**`get-region-read-progress`**([L771](../tikv/cmd/tikv-ctl/src/cmd.rs#L771)):看一个 Region 的 read progress(对应本书 [P5-17](P5-17-TSO-全局单调递增的时间戳.md) 讲的 resolved_ts / RegionReadProgress)。

**`recover-mvcc`**([L351](../tikv/cmd/tikv-ctl/src/cmd.rs#L351)):删损坏的 MVCC key 恢复(危险操作,慎用)。

**`unsafe-recover`**([L387](../tikv/cmd/tikv-ctl/src/cmd.rs#L387)):TiKV 无法正常启动时的不安全恢复(如多数派永久丢失,强制用剩余副本)。**仅灾难恢复用**。

**`flashback`**([L616](../tikv/cmd/tikv-ctl/src/cmd.rs#L616)):闪回到某个 version(对应本书 [P6-20](P6-20-GC与flashback-MVCC老版本回收.md),用旧 version 当新数据走 prewrite+commit)。

> **重要提示**:`tikv-ctl` 的 `--help` 会列出你版本支持的全部子命令和 flag。本附录的子命令名来自源码枚举,实际 flag(如 `-r`/`--region-id`、`-k`/`--key`、`-c`/`--cf`)以 `--help` 为准。

---

## 三、pd-ctl 常用子命令

`pd-ctl` 的源码在 `tikv/pd` 仓(Go,本地 clone 不含)。这里给典型用法。

### 3.1 集群和 Region 分布

**`store`**:看所有 store(TiKV 节点)的状态、Region 数、Leader 数、磁盘占用。
```
pd-ctl store                # 列所有 store
pd-ctl store <store-id>     # 看单个 store 详情
```
> 排查 Region 不均时第一步(本书 [P5-18](P5-18-Region调度-balance与热点.md))。

**`region`**:看 Region 分布。
```
pd-ctl region                # 集群 Region 概览
pd-ctl region <region-id>    # 单个 Region 详情(范围/副本/Leader/Epoch)
pd-ctl region sibling <region-id>   # 看 Region 的兄弟(分裂后两边)
pd-ctl region store <store-id>      # 看某 store 上的所有 Region
pd-ctl region check miss-peer       # 找缺副本的 Region
pd-ctl region check extra-peer      # 找多副本的 Region
```

**`hot region`**:看热点 Region(PD 识别的读写热点)。
```
pd-ctl hot region            # 看热点
pd-ctl hot region read       # 只看读热点
pd-ctl hot region write      # 只看写热点
```
> 对应机制:本书 [P5-18](P5-18-Region调度-balance与热点.md)(PD 心跳上报流量,识别热点调度)。

### 3.2 调度

**`scheduler`**:看 / 控制 PD 调度器。
```
pd-ctl scheduler show                    # 列所有调度器
pd-ctl scheduler add balance-leader-scheduler   # 加 leader 均衡调度器
pd-ctl scheduler remove <scheduler-name>        # 移除调度器
```
> 对应机制:本书 [P5-18](P5-18-Region调度-balance与热点.md)。**边界**:调度决策算法在 `tikv/pd` 仓的 `server/schedulers/`,本地 tikv clone 不含。

**`operator`**:看正在执行的调度操作(change_peer / transfer_leader / split)。
```
pd-ctl operator show          # 看正在执行 / 排队的 operator
```

### 3.3 TSO 和 GC

**`tso`**:看 / 解析 TSO。
```
pd-ctl tso                    # 当前物理时间戳
pd-ctl tso <ts>               # 解析一个 TSO 成物理时间
```
> 对应机制:本书 [P5-17](P5-17-TSO-全局单调递增的时间戳.md)(TSO 物理时钟左移 18 位 + 逻辑计数)。

**`service gc`**:看 GC safe point。
```
pd-ctl service gc safepoint   # 看 GC safe point(对应本书 P6-20)
```
> 对应机制:本书 [P6-20](P6-20-GC与flashback-MVCC老版本回收.md)(safe point 是 GC 边界,取 GC TTL / CDC / BR / resolved_ts 下界)。

**`config`**:看 / 改 PD 配置(含 GC TTL、Region split size 等)。
```
pd-ctl config gc              # 看 GC 配置(如 gc-ttl)
pd-ctl config set gc-ttl 86400
```

---

## 四、TiUP 部署

TiUP 是 TiDB / TiKV / PD 集群的官方部署工具。典型操作(以版本为准):

```
tiup cluster deploy <cluster-name> <version> topology.yaml   # 部署
tiup cluster start <cluster-name>                            # 启动
tiup cluster display <cluster-name>                          # 看拓扑
tiup cluster upgrade <cluster-name> <new-version>            # 升级
tiup cluster scale-out <cluster-name> scale-out.yaml         # 扩容
tiup cluster scale-in <cluster-name> -N <node>:<port>        # 缩容
```

> 排查问题时常用 `tiup cluster display` 看哪个 TiKV 节点挂了 / 哪个 PD 是 leader。扩缩容会触发 Region 调度(本书 [P5-18](P5-18-Region调度-balance与热点.md))。

---

## 五、Grafana 监控关键面板

TiKV 集群的 Grafana 面板按模块组织。排查问题时最常看这几个(面板名称可能随版本略有差异,以你部署的 dashboard 为准):

### 5.1 Raft / raftstore 面板

- **Raft propose / ready 速率**:看 Raft 提议吞吐(本书 [P2-05](P2-05-raftstore全貌-一条写请求的旅程.md) 五步流水线)。propose 速率飙升 = 写压力大。
- **Raft log append / commit 延迟**:看日志写 / 提交延迟(本书 [P2-06](P2-06-Raft日志存储-RaftEngine.md))。延迟飙升可能是磁盘 IO 瓶颈。
- **Raftstore CPU**:batch-system 线程的 CPU(本书 [P1-04](P1-04-batch-system-FSM-一个线程池驱动百万Peer.md))。**这是百万 Peer 调度的核心指标**,长期打满说明 Region 数量过多或单线程瓶颈(考虑 v2)。
- **Hibernate region 数**:休眠的 Region 数(本书 [P2-07](P2-07-异步IO与hibernate-省CPU的两手.md)),空闲集群这个数应该高(省 CPU)。
- **Snapshot 速率 / 数量**:Snapshot 传输(本书 [P2-08](P2-08-Region分裂迁移与Snapshot.md)),飙升说明大量 Region 迁移(扩缩容 / 调度)。

### 5.2 Scheduler / 事务面板

- **Scheduler worker CPU**:SchedPool 的 CPU(本书 [P4-12](P4-12-事务模型全景-scheduler-latch-双引擎.md))。打满说明事务调度忙。
- **Scheduler latch wait duration**:latch 等待延迟(本书 [P4-12](P4-12-事务模型全景-scheduler-latch-双引擎.md))。**飙升 = 行锁冲突激烈**,是热点写冲突的信号。
- **Prewrite / Commit RPC 延迟**:事务两阶段延迟(本书 [P4-13](P4-13-Prewrite预写-选Primary加锁.md)、[P4-14](P4-14-Commit提交与Secondary清理.md))。
- **Store CPU / IO**:单节点 CPU / IO。

### 5.3 GC 面板

- **GC speed / progress**:GC 推进速度(本书 [P6-20](P6-20-GC与flashback-MVCC老版本回收.md))。
- **Safe point 推进**:safe point 是否在涨。**不涨 = GC 跟不上**,可能是 CDC / BR 消费者卡住、或 compaction filter 慢。
- **Lock CF 大小**:lock CF 的数据量(本书 [P4-13](P4-13-Prewrite预写-选Primary加锁.md))。膨胀 = 大量未清的 Secondary 锁。

### 5.4 PD 面板

- **Region 分布 / Leader 分布**:各 store 的 Region / Leader 数(本书 [P5-18](P5-18-Region调度-balance与热点.md))。不均是调度信号。
- **Balance / Hot region 调度速率**:调度执行速度。
- **TSO 延迟 / leader 切换**:TSO RPC 延迟、PD leader 切换次数(本书 [P5-17](P5-17-TSO-全局单调递增的时间戳.md))。**TSO 延迟飙升或频繁切 leader = PD 单点瓶颈**。

---

## 六、与四本前作的承接关系说明

本书是"源码精解大书"分布式存储线第二本,站在《etcd》《LevelDB》《gRPC》三本之上(三重承接)。想深入某块,去读前作对应章节。

| 你在本书哪章 | 想深入哪块 | 去读哪本前作 |
|--------------|------------|--------------|
| [P1-03](P1-03-Raft库回顾与multi-Raft的挑战.md)、[P2-05](P2-05-raftstore全貌-一条写请求的旅程.md)、[P2-07](P2-07-异步IO与hibernate-省CPU的两手.md)、[P2-08](P2-08-Region分裂迁移与Snapshot.md)、[P5-16](P5-16-PD的角色-TSO-调度-ID分配.md)、[P5-17](P5-17-TSO-全局单调递增的时间戳.md)、[P6-21](P6-21-悲观锁与CDC.md) | Raft 算法本体(选主 / 日志 / 提交 / 安全性) | **《etcd》**——TiKV 用的 `raft` crate 是 etcd Raft 的 Rust 移植,算法逻辑一致 |
| [P1-02](P1-02-Region-把海量key切成一段段.md)、[P2-06](P2-06-Raft日志存储-RaftEngine.md)、[P2-08](P2-08-Region分裂迁移与Snapshot.md)、[P3-09](P3-09-RocksDB引擎-LSM-tree与Column-Family.md)、[P3-10](P3-10-MVCC编码-key加时间戳.md)、[P3-11](P3-11-Apply流水线-Raft命令怎么落盘.md)、[P6-20](P6-20-GC与flashback-MVCC老版本回收.md) | LSM-tree / SST / Compaction / Bloom filter / WriteBatch | **《LevelDB》**——RocksDB 是 LevelDB 的工业级后代,核心机制同源 |
| [P2-05](P2-05-raftstore全貌-一条写请求的旅程.md)(batch_raft 流)、[P5-16](P5-16-PD的角色-TSO-调度-ID分配.md)(TSO 专用流)、[P6-19](P6-19-Coprocessor-把计算下推.md)(DAG 流) | HTTP/2 流 / HPACK / gRPC 流控 | **《gRPC》**——TiKV 在 gRPC 之上定义 RPC,协议层前作讲透 |

> **一句话**:本书不重复讲 Raft 算法、LSM-tree、HTTP/2——那是三本前作的活。本书的篇幅全留给 TiKV 独有的:multi-raft 怎么落地、Percolator 怎么跨组拼事务、scheduler/latch 怎么调度、RaftEngine 怎么存日志。读本书时某处觉得"这个机制眼熟",去翻前作,大概率有更深的拆解。

---

## 七、常见线上问题排查清单

下面是最常见的几类线上问题,每条按"症状 → 可能根因(对应本书哪章)→ 排查命令 / 面板"组织。

### 7.1 Region 不均(某些节点磁盘 / CPU 打满,其他空闲)

**症状**:某个 store 磁盘占用远高于其他,或 CPU 打满;Grafana Region 分布不均。

**可能根因**:
- PD balance 调度没跟上(本书 [P5-18](P5-18-Region调度-balance与热点.md)):balance 调度器被禁用 / 限流太严 / 调度速度跟不上数据增长。
- 大 Region 没分裂(本书 [P2-08](P2-08-Region分裂迁移与Snapshot.md)):Region 超过 `region_max_size` 但 split_check 没触发(配置问题 / split_check worker 卡)。
- 热点 Region 集中(本书 [P5-18](P5-18-Region调度-balance与热点.md)):某些 key 被频繁访问,PD 的 hot region 调度没把它们打散。

**排查**:
```
pd-ctl store                              # 看各 store 的 Region 数 / 磁盘 / Leader
pd-ctl region store <hot-store-id> --limit 20   # 看 hot store 上最大的 Region
pd-ctl hot region                         # 看热点
pd-ctl scheduler show                     # 看 balance / hot-region 调度器在不在
tikv-ctl --pd <addr> size -r <big-region-id>  # 看大 Region 实际多大
```
Grafana 看 "Region 分布" / "Balance 调度速率" 面板。

### 7.2 写延迟抖动(偶尔 P99 飙高)

**症状**:写延迟平时正常,偶尔抖动到几百 ms 甚至秒级;Grafana Prewrite / Commit RPC 延迟 P99 飙升。

**可能根因**:
- Raft propose / append 被磁盘 IO 卡住(本书 [P2-05](P2-05-raftstore全貌-一条写请求的旅程.md)、[P2-06](P2-06-Raft日志存储-RaftEngine.md)):fsync 慢,日志写阻塞 Raft 推进。看 Raft log append 延迟。
- async_io 线程卡(本书 [P2-07](P2-07-异步IO与hibernate-省CPU的两手.md)):写线程(store-writer-N)批量合并时自适应等待,偶发大 batch 拖延迟。
- scheduler latch 冲突(本书 [P4-12](P4-12-事务模型全景-scheduler-latch-双引擎.md)):同行并发写,latch 排队。看 latch wait duration。
- Compaction 抖动(本书 [P3-09](P3-09-RocksDB引擎-LSM-tree与Column-Family.md)):RocksDB Compaction 瞬间吃 IO / CPU。看 Compaction 面板。
- Leader 切换(本书 [P2-08](P2-08-Region分裂迁移与Snapshot.md)):Region leader 频繁切换,写短暂失败重试。看 Raft leader 变化次数。

**排查**:
```
Grafana:
  - Raft log append / commit 延迟(看磁盘 IO 瓶颈)
  - Scheduler latch wait duration(看锁冲突)
  - RocksDB Compaction 速率 / IO(看 Compaction 抖动)
  - Raft leader 变化次数(看 leader 抖动)
tikv-ctl --pd <addr> mvcc -k <hot-key>   # 看热点 key 是不是锁堆积
```

### 7.3 锁等待 / 死锁(事务长时间卡住)

**症状**:某些事务长时间不返回;Grafana 锁等待队列长;应用报 `TxnLockNotFound` 或死锁错误。

**可能根因**:
- 悲观锁冲突激烈(本书 [P6-21](P6-21-悲观锁与CDC.md)、[P4-15](P4-15-MVCC读取与锁的解决.md)):`acquire_pessimistic_lock` 互相等待,waiter_manager 队列堆积。
- 死锁(本书 [P4-15](P4-15-MVCC读取与锁的解决.md)、[P6-21](P6-21-悲观锁与CDC.md)):集中式 `DetectTable` wait-for 图 DFS 找环,返回 wait_chain 诊断。
- Primary 卡住(本书 [P4-13](P4-13-Prewrite预写-选Primary加锁.md)、[P4-14](P4-14-Commit提交与Secondary清理.md)):Primary 所在节点挂了 / 网络分区,Secondary 锁清不掉,读 Secondary 都要查 Primary(超时)。
- lock CF 膨胀(本书 [P4-13](P4-13-Prewrite预写-选Primary加锁.md)):大量未清的 Secondary 锁堆积。

**排查**:
```
tikv-ctl --pd <addr> mvcc -k <stuck-key> --cf lock   # 看锁状态(primary 字节 / lock_type / for_update_ts)
tikv-ctl --pd <addr> scan --from <range-start> --cf lock --limit 50  # 扫 lock CF 看哪些锁堆积
Grafana:
  - Scheduler latch wait duration
  - Lock manager waiter 数量 / 死锁检测次数
  - Lock CF 大小
```
死锁错误信息里通常带 `wait_chain`,能定位到是哪些事务互相等(本书 [P6-21](P6-21-悲观锁与CDC.md) 拆过 `pushed` 记前驱重建 wait_chain)。

### 7.4 GC 跟不上(磁盘持续膨胀)

**症状**:磁盘占用持续增长,即使删除了大量数据;`pd-ctl service gc safepoint` 看到 safe point 不推进。

**可能根因**:
- GC safe point 被 CDC / BR 卡住(本书 [P6-20](P6-20-GC与flashback-MVCC老版本回收.md)、[P6-21](P6-21-悲观锁与CDC.md)):safe point 取 GC TTL / CDC / BR / resolved_ts 下界,任一消费者卡住,safe point 就不推进。**这是最常见的 GC 跟不上的原因**。
- compaction filter 慢(本书 [P6-20](P6-20-GC与flashback-MVCC老版本回收.md)):RocksDB Compaction 不跑,compaction filter 没机会清过期版本。可能是 Compaction 限流太严 / SST 太多。
- resolved_ts 不推进(本书 [P5-17](P5-17-TSO-全局单调递增的时间戳.md)):悲观锁卡住 resolved_ts(`Resolver` 里有未提交事务),safe point 算不出来。

**排查**:
```
pd-ctl service gc safepoint              # 看 safe point 和各 service 的进度
pd-ctl service gc safepoint --detail     # 看 CDC / BR 等消费者的 safe point
Grafana:
  - GC speed / progress(safe point 在不在涨)
  - Lock CF 大小(锁堆积会卡 resolved_ts)
  - RocksDB Compaction 速率
tikv-ctl --pd <addr> compact -c default  # 手动触发 compact(排查时验证 compaction filter)
```

### 7.5 读延迟 / stale read

**症状**:读延迟高,或偶尔读到旧数据(stale read 问题)。

**可能根因**:
- MVCC scanner 慢(本书 [P4-15](P4-15-MVCC读取与锁的解决.md)):某个 key 版本太多(default CF 堆积),scan 要跳很多版本。根因通常是 GC 跟不上。
- 遇锁阻塞(本书 [P4-15](P4-15-MVCC读取与锁的解决.md)):读到 lock 要查 Primary 状态(CheckTxnStatus RPC),延迟增加。
- resolved_ts 落后(本书 [P5-17](P5-17-TSO-全局单调递增的时间戳.md)):follower read 用 resolved_ts,落后会让 follower 读不到新数据。
- Coprocessor 下推吃 CPU(本书 [P6-19](P6-19-Coprocessor-把计算下推.md)):大聚合下推把 TiKV CPU 吃满。

**排查**:
```
tikv-ctl --pd <addr> mvcc -k <slow-key>   # 看 key 版本数(版本多 = GC 跟不上)
tikv-ctl --pd <addr> get-region-read-progress -r <region-id>  # 看 resolved_ts 进度
Grafana:
  - Coprocessor 请求延迟 / CPU
  - resolved_ts 落后程度
  - Read pool CPU
```

### 7.6 TSO 单点瓶颈(PD leader 切换 / TSO 延迟飙升)

**症状**:整个集群事务短暂卡住;Grafana TSO 延迟飙升;PD leader 频繁切换。

**可能根因**(本书 [P5-17](P5-17-TSO-全局单调递增的时间戳.md)):
- PD leader 切换:新 leader 要等保护期(~3 秒)+ 校准物理时钟,这几秒拿不到新时间戳,事务阻塞。
- PD leader 节点 CPU / 网络瓶颈:TSO 单点,单节点扛不住。
- 网络抖动:PD 和 TiKV 之间网络问题。

**排查**:
```
pd-ctl tso                               # 看当前 TSO 能不能正常返回
Grafana:
  - PD 面板:TSO 延迟、leader 切换次数、PD 节点 CPU
  - TiKV 面板:get_tso RPC 延迟
```
> 缓解:客户端批量(64 个/批)、resolved_ts 让读少问 TSO、causal_ts(9.x,CDC 用)。这是 TiKV 事务层唯一的单点,本书 [P5-17](P5-17-TSO-全局单调递增的时间戳.md) 和收束章 [P7-22](P7-22-全书收束-从etcd到TiKV的跃迁.md) 都讨论过它。

---

## 八、排查问题的一般思路

最后给一个通用的排查思路,把上面散的点串起来:

1. **先用 Grafana 定位是哪类问题**:看 Raftstore CPU / Scheduler latch / GC safe point / Region 分布 / TSO 延迟哪个异常。
2. **再用 pd-ctl 看全局**:Region 分布、调度、safe point、TSO、store 健康。
3. **最后用 tikv-ctl 钻进具体节点**:看具体 Region 的大小、key 的 MVCC、锁状态、强制 compact 验证。
4. **对照本书机制理解**:每一步看到的指标,对应本书哪一章的机制(比如 latch wait 飙升对应 [P4-12](P4-12-事务模型全景-scheduler-latch-双引擎.md),safe point 不推进对应 [P6-20](P6-20-GC与flashback-MVCC老版本回收.md)),这样能从"现象"追到"根因"。

> **核心心法**:TiKV 的每个监控指标、每个工具命令,背后都是某一章讲的某个机制。**理解了机制,指标就不再是数字,而是机制的体检报告**。这就是本书 21 章源码拆解的价值——它让你排查问题时,知道每个数字在说什么。

---

> 配套 [附录 A · 源码全景路线图](附录A-源码全景路线图.md),那里把 22 章串成一条可走的源码阅读路线;读完源码再回来排查,会更得心应手。
