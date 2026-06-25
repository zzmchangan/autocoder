# 第 1 篇 · 第 2 章 · Region:把海量 key 切成一段段

> **核心问题**:上一章说到,TiKV 要把 etcd 的"一个 Raft 组管全量 KV"放大成"百万个 Raft 组各管一片"。可这一片一片到底怎么切?凭什么按 key 切、而不是按 hash 切?切出来的每一段(Region)用什么数据结构描述、怎么编号、存进 RocksDB 时 key 长什么样?以及——P0-01 里提到的 256MB 默认大小(8.3.0 之前是 96MB),这个数字是怎么定下来的?这一章,把"被复制的单位"——Region——从 key 编码到元数据存储,一次性立起来。

> **读完本章你会明白**:
> 1. 为什么 Region 按 **key range(连续区间)** 切,而不是按 hash 切——前者保序、利于范围扫描和分裂,后者一	hash 就打散了,范围查变全表扫。
> 2. TiKV 在 RocksDB 里存的 key,跟用户写的 key 不是同一个东西:用户 key 前面要加一个 `z` 前缀(`DATA_PREFIX`),为什么。
> 3. 一个 Region 到底由哪些字段描述(`metapb::Region` + `RegionEpoch`),为什么需要 `conf_ver` 和 `version` 两个版本号。
> 4. Region 大小为什么从 96MB 涨到了 **256MB**(8.3.0+),以及 raftstore-v2 为什么直接干到 10GB——这背后是"调度开销 vs 单 Region 处理效率"的权衡,不是拍脑袋。

> **如果一读觉得太难**:先只记住三件事——① Region = 一段连续 key range + 一组副本(Peers),是这个集群里"被复制、被调度、被分裂"的最小单位;② 用户 key 进 TiKV 时会被加上 `z` 前缀,把"用户数据"和"系统内部数据(Region 元数据等)"在 RocksDB 里隔开;③ Region 大小是个甜点,小了开销爆炸、大了调度笨重,新版默认 256MB。

---

## 〇、一句话点破

> **Region 是一段连续的 key range,外加一组副本(Peers)和两个版本号——它是 TiKV 里"被复制、被调度、被分裂"的最小单位;百万个 Raft 组的"百万",数的正是 Region 的个数。**

这是结论。本章倒过来拆:先讲为什么不按 hash 切、非要按区间切;再讲用户 key 怎么被编码进 RocksDB;接着讲 Region 的元数据结构和那两个版本号是干嘛的;最后把"Region 多大合适"这个工程甜点掰开揉碎,并顺手纠正一个流传甚广的旧数字。

---

## 一、为什么不按 hash 切,要按 key range 切

数据要分片,第一反应通常是 hash:对 key 算个哈希,模上节点数,落到哪台就哪台。Redis Cluster、Cassandra、很多 KV 存储都这么干。hash 分片有个公认的好处——数据天然均匀,加节点只要 rehash 一部分。可 TiKV 偏偏不这么干,它选了**按 key 的范围(range)切**。这不是任性,是被一个硬需求逼出来的。

### 那个硬需求:范围扫描

数据库不只是点查(`get(k)`)。`SELECT * FROM orders WHERE id BETWEEN 1000 AND 2000`、`SELECT * FROM users ORDER BY age`、索引的 range scan——这些都是**范围查询**。如果数据按 hash 打散到各个节点,一次范围查询要**同时去所有节点各取一段再归并**,节点越多扇出越大、网络往返越多,范围扫的性能随集群规模劣化。

按 key range 切就不一样了:一段连续的 key 物理上聚在一个 Region 里(进而聚在一台机器的 RocksDB 里相邻位置),范围查询只要定位到起点 Region,顺着 key 顺序往后扫即可——**扫得越远,跨的 Region 越多,但任一时刻都只在少数几个 Region 上**。RocksDB 作为 LSM-tree,本身就靠 key 有序来高效做范围迭代(这点《LevelDB》那本拆透过);Region 按 range 切,恰好把"有序"这个性质从单机延伸到了分布式。

> **不这样会怎样**:如果按 hash 分片,一次 `WHERE id BETWEEN 1000 AND 2000` 会被打成几千个针对不同节点的点查,聚合代价爆炸。更重要的是,TiDB 的索引、二级索引的扫描、`SELECT ... ORDER BY` 全都要靠 key 有序——hash 分片会让这些操作从"扫一段连续数据"退化成"全集群 gather",数据库就没法用了。所以 TiKV 的分片必须是 **range(保序)**,这是被"数据库要支持高效范围查询"这个根本需求定死的。

### range 分片的代价:热点

range 分片不是没毛病。它的天然短板是**热点**:如果 key 是自增 id(订单号、时间戳),所有新写入都堆在 key 空间的最右端,也就是最后一个 Region 上,这个 Region 所在的机器被打爆,别的机器闲着。这是 hash 分片不存在的问题(hash 会把自增 id 均匀打散)。

TiKV 怎么对付热点?不是改回 hash,而是**在 range 的框架里做调度**:PD 识别出热点 Region(通过心跳统计读写流量),把它分裂、把副本/leader 挪到空闲机器(`split_controller`、`hot region scheduling`,P5-18 详拆)。换句话说,TiKV 接受了 range 分片的"热点倾向",用**动态再调度**来治——而不是放弃 range 的"保序"红利。这是个典型的工程取舍:**用一个动态可治的毛病,换一个静态不可治的毛病的命**。

> **钉死这件事**:Region 按 key range 切,不是为了均匀(那是 hash 的强项),而是为了**保序**——为了让范围扫描、索引顺序访问、排序这些数据库的核心操作,在分布式下还能高效。代价是热点要靠 PD 调度治。这个选择,从根上决定了 TiKV 是个"数据库的存储层",而不是个"通用 KV 缓存"。

---

## 二、用户 key 进了 TiKV,变成了什么样

切 Region 得先有个 key 空间。但读者马上会遇到一个困惑:**TiKV 收到的 key,和最终存进 RocksDB 的 key,不是同一个东西**。它们之间差一个前缀。

### 为什么要加前缀:把用户数据和系统数据隔开

一个 TiKV 进程里只有(很少的)几个 RocksDB 实例,所有数据都往里塞。这里面不止有用户的 KV 数据,还有大量**系统自己用的内部数据**:每个 Region 的状态(`RegionLocalState`)、每个 Peer 的 Raft 状态(`RaftLocalState`)、Apply 进度(`RaftApplyState`)、store 自身的标识(`StoreIdent`)……这些内部数据如果跟用户数据混在一个扁平的 key 空间里,后果是灾难性的:扫描用户数据会扫到一堆系统记录、range 边界没法清晰界定、万一 key 撞了更是一锅粥。

TiKV 的做法简单粗暴且有效:**给所有 key 加一个字节的前缀,按前缀分片整个 key 空间**。打开 `components/keys/src/lib.rs`,前缀的定义一目了然:

```rust
// local is in (0x01, 0x02);
pub const LOCAL_PREFIX: u8 = 0x01;            // keys/src/lib.rs#L24
// ...
pub const DATA_PREFIX: u8 = b'z';             // keys/src/lib.rs#L28
pub const DATA_PREFIX_KEY: &[u8] = &[DATA_PREFIX];
pub const DATA_MIN_KEY: &[u8] = &[DATA_PREFIX];
pub const DATA_MAX_KEY: &[u8] = &[DATA_PREFIX + 1];   // L31
```

几个关键常量:

| 前缀 | 值 | 含义 |
|------|----|------|
| `LOCAL_PREFIX` | `0x01` | 所有"系统内部数据"的开头,占 `(0x01, 0x02)` |
| `DATA_PREFIX` | `b'z'` (= `0x7A`) | 所有"用户数据"的开头 |
| `DATA_MIN_KEY` | `[0x7A]` | 用户数据的下界 |
| `DATA_MAX_KEY` | `[0x7B]` | 用户数据的上界(注意是 `DATA_PREFIX + 1`,开区间) |

为什么用户数据选 `z`(`0x7A`)而不是 `0x02`?因为 `[0x00, 0x7A)` 这一大段留给了各种内部前缀,用户数据被顶到后面,跟系统数据(在 `0x01` 段)远远隔开,扫用户数据时根本不会碰到系统数据。而 `0xFF` 又被留作全局上界 `MAX_KEY`(`keys/src/lib.rs#L19`),所以 `z` 是个精心挑的、在字节序里位置靠后但不顶到 `0xFF` 的字符。

把用户 key 变成 RocksDB 里的 key,就是加这个前缀:

```rust
pub fn data_key(key: &[u8]) -> Vec<u8> {              // keys/src/lib.rs#L206
    let mut v = Vec::with_capacity(DATA_PREFIX_KEY.len() + key.len());
    v.extend_from_slice(DATA_PREFIX_KEY);
    v.extend_from_slice(key);
    v
}

pub fn origin_key(key: &[u8]) -> &[u8] {              // keys/src/lib.rs#L219
    assert!(validate_data_key(key), "invalid data key {}", ...);
    &key[DATA_PREFIX_KEY.len()..]
}
```

`data_key` 把用户 key 前面贴一个 `z`,`origin_key` 反过来剥掉。每个用户 key 进 TiKV,第一件事就是被 `data_key` 转成内部表示。

> **不这样会怎样**:如果不加前缀,把用户数据和 Region 元数据、Raft 状态、Apply 状态混在一个扁平空间,那么一次"扫描某个 Region 的用户数据"会扫到一堆跟用户无关的内部记录,而且**没法用 key range 边界干净地卡住一个 Region 的范围**——因为内部数据可能穿插在用户 key 中间。前缀隔离让"用户数据全在 `z` 开头"成为一个不变式,后续所有按 Region 范围扫描的逻辑都建立在这个不变式上。

### 字节序:让 key 在 RocksDB 里"天然有序"

加了前缀还不够。RocksDB(像所有 LSM/B+ 树)按 key 的字节序排列,而**字节序比较**和**业务想要的比较**不一定一致。最典型的:用户 key 里如果嵌入了整数(比如表 `_tidb_rowid`、或 index 的 handle),用 little-endian 存的话,字节序里 `255` 反而排在 `1` 前面——这会破坏"按 id 顺序扫描"的语义。

TiKV 在 `components/codec/src/` 里有一套编码工具,核心是两类:

1. **`MemComparableByteCodec`**(`codec/src/byte.rs`):保证"编码后的字节序比较 = 原始 bytes 的语义比较"。它的做法借鉴自 MyRocks——把原始 bytes 每 8 字节一组,每组末尾追加一个 **padding marker**,标记最后一组补了多少个 `0`:

```rust
const MEMCMP_GROUP_SIZE: usize = 8;                   // codec/src/byte.rs#L11
const MEMCMP_PAD_BYTE: u8 = 0;                        // codec/src/byte.rs#L12
// ...
/// Encodes all bytes in the `src` into `dest` in ascending memory-comparable format.
// Refer: https://github.com/facebook/mysql-5.6/wiki/MyRocks-record-format#memcomparable-format
pub fn encode_all(src: &[u8], dest: &mut [u8]) -> usize {     // codec/src/byte.rs#L67
    // 每 8 字节一组,最后一组不足 8 用 0 补齐,末尾写一个 marker = !(padding_size)
}
```

为什么要 8 字节一组还加 marker?因为 **`[u8]` 的字节序比较,在长度不同时会出错**:`"ab"` vs `"ab\0"`——前者短,但语义上它俩"差不多大"。MemComparable 编码保证:**任意两个不同长度的 key,编码后比较结果和它们的字典序一致**——靠的就是 marker 标出"我在第几组补了几个 0",让比较器知道原始长度边界。这是范围扫描正确性的基石。

2. **NumberCodec**(`codec/src/number.rs`):把 `u64`/`i64`/`f64` 编成"字节序即数值序"的形式——大端(big-endian)是基本,对有符号数还要翻转符号位(让负数排在正数前)。

> **钉死这件事**:TiKV 里"用户 key"到"RocksDB 内部 key"经过两层处理:① 加 `z` 前缀(隔离用户数据 vs 系统数据);② 必要时做 memcomparable 编码(保证字节序即语义序)。这两步合起来,使得"按 key range 切 Region、按 Region 范围扫描"在 RocksDB 里变成了简单的 `[start_key, end_key)` 字节区间扫描——底层 LSM 的有序性被原封不动地利用。**没有这两步,range 分片的前提(保序)就落不了地**。

> **小提醒**:在 6.x 之前,TiKV 的 user key 还要再做一层 memcomparable 编码(`encode_bytes`)。但**新版 TiDB 端已经直接发"memcomparable 编码后的 key"给 TiKV**(TiDB 层做了编码,TiKV 这层大多只加 `z` 前缀)。所以你会在 `keys` crate 里看到大量 `data_key` 的调用,却较少看到 `encode_bytes` 的调用——编码责任被上推到了 TiDB。这点读源码时别被老博客带偏。

---

## 三、Region 的元数据:它到底长什么样

key 空间有了,现在切 Region。一个 Region 在 TiKV 里是用 protobuf 描述的——具体是 `kvproto::metapb::Region`(kvproto 是 TiDB/TiKV/PD 共享的 protobuf 定义,本地仓没 vendored kvproto 源码,但它的字段被 TiKV 大量使用,结构稳定且公开)。一个 Region 的核心字段是:

```
metapb::Region {
    id: u64,                  // Region 全局唯一 ID(PD 分配)
    start_key: Vec<u8>,       // Region 的 key 下界(包含,内部编码后的 key)
    end_key:   Vec<u8>,       // Region 的 key 上界(不包含;空表示"到最后")
    region_epoch: RegionEpoch {  // 版本号,见下一节
        conf_ver: u64,
        version:  u64,
    },
    peers: Vec<metapb::Peer>, // 这个 Region 的所有副本
}
metapb::Peer {
    id: u64,           // Peer 全局唯一 ID(PD 分配)
    store_id: u64,     // 这个 Peer 落在哪台 store
    role: PeerRole,    // Voter / Learner ...(Raft 角色)
}
```

几个要点:

- **`id` 和 `peers[].id` 都是 PD 全局分配的**(P5-16 详拆)。Region id 不会复用,Peer id 也不会。
- **`start_key` / `end_key` 是内部编码后的 key**(带 `z` 前缀的那套)。这一点很关键——在 TiKV 内部比对"某个 key 属于哪个 Region"时,比的就是编码后的字节。`keys::enc_start_key(region)` / `enc_end_key(region)` 这两个 helper(`keys/src/lib.rs#L229, L237`)就是把 Region 的边界 key 转成内部表示用的。
- **`end_key` 为空表示"到 key 空间末尾"**。这是最后一个 Region 的标志。注意 `enc_end_key` 里专门处理了这点:

```rust
pub fn enc_end_key(region: &Region) -> Vec<u8> {       // keys/src/lib.rs#L237
    assert!(!region.get_peers().is_empty());
    data_end_key(region.get_end_key())
}

#[inline]
pub fn data_end_key(key: &[u8]) -> Vec<u8> {           // keys/src/lib.rs#L245
    if key.is_empty() {
        DATA_MAX_KEY.to_vec()       // 空 end_key → 0x7B,即用户数据上界
    } else {
        data_key(key)
    }
}
```

空 `end_key` 被翻译成 `DATA_MAX_KEY`(`0x7B`),这样所有"判断 key 是否落在 `[start, end)`"的逻辑都能用统一的字节比较,不用为"最后一个 Region"特判。

### 不相交且覆盖:Region 集合的两个不变式

切完所有 Region,整个集群的 Region 集合必须满足两条:

1. **两两不相交**:任意两个 Region 的 `[start_key, end_key)` 不能重叠。否则同一个 key 落在两个 Region 里,数据就乱了。
2. **覆盖整个 key 空间**:把所有 Region 的区间并起来,等于 `[DATA_MIN_KEY, DATA_MAX_KEY)` 即 `[0x7A, 0x7B)`。否则有些 key 不属于任何 Region,没法路由。

这两条是 TiKV 路由正确性的基石。分裂(split)产生两个相邻新区间、合并(merge)把两个相邻区间合一,都必须维持这两条不变式。Region 的 `start_key` 就是上一个 Region 的 `end_key`,首尾相接像一条被切成段的绳子。

### RegionEpoch:两个版本号,各管一摊

`region_epoch` 里的两个版本号,是 TiKV 区分"新旧 Region"的关键:

- **`version`**:只要 Region 的 **key range 变了**(分裂、合并),`version` 就 +1。
- **`conf_ver`**:只要 Region 的 **副本配置变了**(加减副本、Peer 角色变更),`conf_ver` 就 +1。

为什么要两个?因为这是两类**完全不同**的变化,混在一个版本号里会丢失信息:

> **不这样会怎样**:假设只有一个版本号。一个 Region 分裂了(version 涨),分裂出来的新 Region 的副本配置可能也和原来不一样(比如加了个 Learner 准备换副本)。如果用同一个版本号,下游节点收到一条带着"老 conf_ver、新 version"的消息时,分不清"这是分裂前的合法消息"还是"配置变更后的过期消息"。两个版本号各管一摊,**任意一个变了就说明 Region"代际"变了**,而下游能用"我看到的 version/conf_ver 是否和消息里的一致"精确判断消息是否过期。

这是 TiKV 防止**陈旧消息(stale message)**的核心机制。比如一个 Peer 收到一条 Raft 消息,它会先比 `from_epoch` 和自己的 `region_epoch`——如果消息里的 epoch 旧,说明这是发给"已经不存在的旧 Region"的消息,直接丢弃或回退(`store/fsm/store.rs` 里的 `check_msg`,P2-05 详拆)。没有 epoch,陈旧消息会把已经分裂/合并过的新 Region 搞乱。

> **钉死这件事**:Region 不是一段"死的 key range",而是一段"会演变"的 key range——它会分裂、会合并、副本会增减。`RegionEpoch(version, conf_ver)` 就是这段演变的"代际号"。TiKV 里几乎所有跨 Peer 的消息,都随身带着 epoch,接收方靠它判断"这条消息是不是过期的"。这是分布式系统里**用版本号对抗时序混乱**的标准做法(和 MVCC 用 ts 对抗并发读写是同一种思路,P3-10)。

---

## 四、Region 在 RocksDB 里怎么存:local key 的两套前缀

Region 本身是个 protobuf 结构,它最终也要落盘(进程重启后得恢复)。落盘的 key,用的还是前缀隔离那套思路,只不过这次落在 `LOCAL_PREFIX`(`0x01`)段里。看 `keys/src/lib.rs` 的 41-58 行:

```rust
pub const REGION_RAFT_PREFIX: u8 = 0x02;               // keys/src/lib.rs#L41
pub const REGION_RAFT_PREFIX_KEY: &[u8] = &[LOCAL_PREFIX, REGION_RAFT_PREFIX];
pub const REGION_RAFT_MIN_KEY: &[u8] = &[LOCAL_PREFIX, REGION_RAFT_PREFIX];
pub const REGION_RAFT_MAX_KEY: &[u8] = &[LOCAL_PREFIX, REGION_RAFT_PREFIX + 1];

pub const REGION_META_PREFIX: u8 = 0x03;               // keys/src/lib.rs#L45
pub const REGION_META_PREFIX_KEY: &[u8] = &[LOCAL_PREFIX, REGION_META_PREFIX];
pub const REGION_META_MIN_KEY: &[u8] = &[LOCAL_PREFIX, REGION_META_PREFIX];
pub const REGION_META_MAX_KEY: &[u8] = &[LOCAL_PREFIX, REGION_META_PREFIX + 1];

// Following are the suffix after the local prefix.
// For region id
pub const RAFT_LOG_SUFFIX: u8 = 0x01;                   // keys/src/lib.rs#L52
pub const RAFT_STATE_SUFFIX: u8 = 0x02;
pub const APPLY_STATE_SUFFIX: u8 = 0x03;
pub const SNAPSHOT_RAFT_STATE_SUFFIX: u8 = 0x04;

// For region meta
pub const REGION_STATE_SUFFIX: u8 = 0x01;
```

系统内部数据被进一步分成**两个段**:

- `[LOCAL_PREFIX, REGION_RAFT_PREFIX]` = `[0x01, 0x02]` 段:存 Raft 相关——Raft 日志(`RAFT_LOG_SUFFIX`)、Raft 状态机状态(`RAFT_STATE_SUFFIX`)、Apply 进度(`APPLY_STATE_SUFFIX`)、Snapshot 时的 Raft 状态(`SNAPSHOT_RAFT_STATE_SUFFIX`)。
- `[LOCAL_PREFIX, REGION_META_PREFIX]` = `[0x01, 0x03]` 段:存 Region 元数据——`REGION_STATE_SUFFIX` 就是 `RegionLocalState`(Region 当前的 protobuf 描述)。

注释把动机说得很直白:

```rust
// We save two types region data in DB, for raft and other meta data.
// When the store starts, we should iterate all region meta data to
// construct peer, no need to travel large raft data, so we separate them
// with different prefixes.
```

**启动时遍历元数据建 Peer,不想顺带遍历巨大的 Raft 日志**——所以把它们用不同前缀隔开,扫描时只扫 `0x03` 段就好。这是个为**冷启动性能**做的设计。

具体到一条 key 怎么拼,看 `make_region_key`(`keys/src/lib.rs#L70`):

```rust
#[inline]
fn make_region_key(region_id: u64, suffix: u8, sub_id: u64) -> [u8; 19] {
    let mut key = [0; 19];
    key[..2].copy_from_slice(REGION_RAFT_PREFIX_KEY);   // [0x01, 0x02]
    BigEndian::write_u64(&mut key[2..10], region_id);   // region_id 大端 8 字节
    key[10] = suffix;                                    // 区分 log/state/apply
    BigEndian::write_u64(&mut key[11..19], sub_id);     // 比如 log index
    key
}

pub fn raft_log_key(region_id: u64, log_index: u64) -> [u8; 19] {     // L91
    make_region_key(region_id, RAFT_LOG_SUFFIX, log_index)
}

pub fn region_state_key(region_id: u64) -> [u8; 11] {     // L198,走 meta 前缀
    make_region_meta_key(region_id, REGION_STATE_SUFFIX)
}
```

几个细节值得品:

1. **`region_id` 用大端(`BigEndian::write_u64`)存**。为什么?因为 RocksDB 按 key 字节序排,大端编码下 `region_id` 数值大的排在后面——同一 Region 的所有内部 key(`region_id` 相同)聚在一起,而且**按 `suffix` 和 `sub_id` 有序**。`raft_log_key` 把 `log_index` 也大端编码,于是同一 Region 的 Raft 日志在 RocksDB 里**按 index 递增排成一队**,range scan 日志、找某个 index 的日志,全是 O(范围)的。

2. **key 长度固定(19 或 11 字节)**。返回的是 `[u8; N]` 数组而不是 `Vec<u8>`,这意味着这些 key 在栈上构造、零分配,热路径上省一次 heap alloc。这种"高频小 key 用定长数组"是 TiKV 源码里反复出现的优化手段。

> **钉死这件事**:TiKV 的 key 空间,本质上是被**两级前缀**组织起来的:`0x01` 段是系统内部(`0x02` Raft 相关 / `0x03` 元数据),`0x7A`(`z`)段是用户数据。这种隔离让"扫描用户数据"和"扫描系统数据"互不干扰,而且每个 Region 的所有相关记录(日志、状态、元数据)都靠 `region_id` 大端编码聚成一个连续块,方便批量加载和管理。**这是 TiKV 在单机引擎上"组织百万 Region"的地基**。

> **架构演进提醒**:这里讲的"Raft 日志 key = `raft_log_key(region_id, log_index)` 存进 RocksDB",是**老版本**的做法。新版 TiKV 已经把 Raft 日志挪到了专用的 **RaftEngine**(`components/raft_log_engine/`),不再写进 RocksDB。但 Region 元数据(`RegionLocalState`)仍然存在 RocksDB 的 `LOCAL_PREFIX` 段里,这套 key 编码没变。RaftEngine 为什么单独存、怎么存,是 P2-06 的招牌内容,这里先记一笔:**本章这套 `region_raft_prefix` key 设计仍在用,但承载 Raft 日志的引擎换了**。

---

## 五、Region 多大合适:从 96MB 到 256MB 的演进

P0-01 里我们说"Region 默认约 256MB(8.3.0 之前是 96MB)"。这个数字不是从来如此——打开 `components/raftstore/src/coprocessor/config.rs`,看它怎么演变的:

```rust
/// Default region split size. In version < 8.3.0, the default split size is
/// 96MB. In version >= 8.3.0, the default split size is increased to 256MB to
/// allow for larger region size in TiKV.
pub const SPLIT_SIZE: ReadableSize = ReadableSize::mb(256);    // config.rs#L75
pub const RAFTSTORE_V2_SPLIT_SIZE: ReadableSize = ReadableSize::gb(10);   // L76
```

注释把这件事讲得清清楚楚:**8.3.0 之前默认 96MB,8.3.0 及之后默认 256MB**。而 raftstore-v2(新版多线程 raftstore)更激进,直接 **10GB**。这是写作时 Grep 源码**核实**的事实——8.3.0 之前的资料都写 96MB,新版已是 256MB,老博客大片过时。

那么,为什么是 256MB?为什么曾经是 96MB?为什么 v2 敢到 10GB?这三个数字背后是同一个权衡的三个取值点。

### 权衡的两端:开销 vs 灵活

Region 大小的选择,是在两个相反的力之间找甜点:

**Region 越小,代价越大(开销爆炸)**:
- 每个 Region 是一个独立的 Raft 组,要维护 Leader、选举、心跳、日志。Region 数量 = Raft 组数量 = 状态机数量。同样的数据量,Region 越小,Raft 组越多。
- PD 要维护一张"Region → 机器"路由表。Region 越多,路由表越大,PD 内存和心跳开销越高。
- 跨 Region 的事务(Percolator)概率随 Region 数量上升——你写一批 key,跨的 Region 越多,两阶段提交的协调开销越大。
- 算个账:100TB 数据 ÷ 96MB ≈ **100 万个 Region**;÷ 1MB ≈ **1 亿个 Region**。后者光是 Raft 状态机的常驻内存就能压垮集群。

**Region 越大,代价也越大(调度笨重)**:
- 分裂和迁移要把整个 Region 的数据挪一遍。1GB 的 Region 挪一次,网络/磁盘要传 1GB,期间服务受影响。
- 热点无法精细分散。如果某个热点 key 在一个 1GB 的 Region 里,这个热点就绑死在那一台机器上,PD 没法只挪"热点那一小撮 key"。
- 单个 Raft 日志变长,追平新副本(回放日志或传 Snapshot)更慢。

### 96MB → 256MB:TiDB 团队的生产经验

96MB 是早期 TiDB 团队调出来的甜点,它**倾向于"灵活"**——那时候集群规模不大、热点是主要矛盾,96MB 让 PD 能精细地切分和迁移。但随着 TiDB 部署规模越来越大(几十 TB、上百 TB),96MB 暴露出了"Region 数量爆炸"的代价:百万级 Region 的 PD 路由表、跨 Region 事务开销、Raft 状态机常驻内存,都成了新的瓶颈。

8.3.0 把默认值提到 256MB,是个**向"减少开销"方向拨一格**的决定:同样 100TB 数据,Region 数量从 ~100 万降到 ~40 万,PD 路由表缩 60%,跨 Region 事务概率下降。代价是分裂/迁移单次成本上升——但生产实践证明,256MB 单次迁移在现代万兆网络 + NVMe 磁盘下仍然可接受,而 Region 数量下降带来的整体收益更大。

### v2 的 10GB:换骨架后规则变了

raftstore-v2 为什么敢直接 10GB?因为 v2 重写了 raftstore 的核心,引入了**多线程 Raft** + **region bucket**(Region 内部再细分 bucket,粒度比 Region 更细)。在 v2 里:

- 多线程让单个大 Region 不会卡住整个 store(老版 v1 是单线程批量调度,大 Region 占用调度时间长会影响别的 Region);
- region bucket 让"热点分散"不再依赖"Region 足够小"——可以在大 Region 内部按 bucket 调度,既保住了大 Region 的处理效率,又能精细分散热点。

于是 v2 敢把 Region 放大到 10GB。但**本书以经典 raftstore(v1)为主线**,所以你会看到 v1 的默认 256MB;v2 的 10GB 作为演进对照(P1-04 会讲清 v1 单线程批量调度的约束,正是 v2 要打破的)。

### 还有一对:region_max_size 和 region_split_size

光有 `split_size` 不够,还有个 `max_size`。看 config.rs 的 getter:

```rust
pub fn region_split_size(&self) -> ReadableSize {        // config.rs#L110
    self.region_split_size.unwrap_or(SPLIT_SIZE)
}

pub fn region_max_size(&self) -> ReadableSize {          // config.rs#L120
    self.region_max_size
        .unwrap_or(self.region_split_size() / 2 * 3)    // = split * 1.5
}
```

为什么 `max_size = split_size * 1.5`?这俩分工不同:

- `region_max_size`(默认 256 × 1.5 = 384MB):**触发分裂的阈值**。一个 Region 长到这么大,split_check worker 会启动,准备把它切了。
- `region_split_size`(默认 256MB):**切完之后,每个新 Region 的大小目标**。

两者不重合是有意的:触发阈值(384MB)比目标大小(256MB)大,留了缓冲——split_check 是异步的,从"决定切"到"真的切完"有延迟,这段时间 Region 还在涨。如果触发阈值 = 目标大小,那切完的瞬间新 Region 又快超标了,会频繁触发分裂。**触发线 > 目标线,给分裂留出执行时间,避免抖动**。`region_max_keys` 和 `region_split_keys` 也是同样的 1.5 倍关系(`config.rs#L114, L125`)。

> **钉死这件事**:Region 大小不是个常数,是个**被反复调参的甜点**。它平衡着"Raft 组开销"(希望 Region 大)和"调度灵活度"(希望 Region 小)。96MB(老)→ 256MB(新 v1)→ 10GB(v2)的演进,折射的是"集群规模越来越大"和"raftstore 越来越并行"两个趋势。**这个数字会继续变,但权衡的两端不会变**——理解了这两端,你就理解了为什么 Region 是这个量级。

---

## 六、技巧精解:大端编码与"key 即索引"

本章最值得钉死的技巧,是 TiKV 怎么用 **key 的字节序**来当"天然索引"。这不是花架子,是 Region 这套设计能跑起来的底层支撑。

### 朴素做法:用一张表存"Region 的所有日志 index"

假设你要存每个 Region 的 Raft 日志——`(region_id, log_index) → log_entry`。最朴素的实现是:开一个 RocksDB,key 用 `format!("{}_{}", region_id, log_index)`。看起来没问题,实际上有个致命伤:**字符串字典序和数值序不一致**。`"10_5"` 在字典序里排在 `"2_100"` 前面(因为字符 `'1' < '2'`)。于是同一 Region 的日志在 RocksDB 里**不按 index 排**——你想 range scan `[index=5, index=20)` 的日志,扫出来的是乱序的,得自己再排一次。

### TiKV 的做法:大端定长编码

TiKV 把 `region_id` 和 `log_index` 都用 **8 字节大端**编码,定长拼起来(`keys/src/lib.rs#L70-77` 的 `make_region_key`)。大端编码的好处是:**字节序比较 = 数值序比较**。`u64` 大端的字节序里,数值大的排后面;同 `region_id` 下,`log_index` 小的排前面。于是:

- 同一 Region 的所有日志,在 RocksDB 里**按 index 升序聚成一段连续区间**;
- range scan `[raft_log_key(rid, 5), raft_log_key(rid, 20))` 直接拿到 `[5, 20)` 这段日志,**天然有序、零额外排序**;
- 不同 Region 的日志按 `region_id` 分块(因为 `region_id` 在 key 前面),互不穿插。

这一招把"RocksDB 的有序性"从"存储层"延伸到了"业务层"——**key 本身就是索引**,不用再建一张专门的 index 表。

### 为什么不省那 8 字节:定长 vs 变长

有人会问:`log_index` 实际值通常不大(几十万以内),用变长编码(varint)不更省空间?省是省了,但**变长编码的字节序不再等于数值序**。`varint(5)` 是 1 字节,`varint(128)` 是 2 字节,字典序里 `varint(128)` 反而排在 `varint(5)` 前面(因为它第一个字节 `0x80` > `0x05`)。于是 range scan 又坏了。

TiKV 选了**定长大端**:多花几字节,换来"key 即有序索引"。在 Raft 日志、Region 元数据、MVCC key(P3-10)这些需要 range scan 的场景里,这笔交易极其划算。而且这些 key 都是定长数组 `[u8; 19]` / `[u8; 11]`,栈上构造零分配——热路径优化也顺手做了。

> **不这么写会怎样**:如果用变长或字符串拼接 key,同一 Region 的日志在 RocksDB 里乱序,所有"扫描某个 index 区间的日志""找第一条 ≥ N 的日志""截断日志到某个 index"这些 Raft 核心操作(截断、回放、复制都要 range scan 日志)全都得自己排序或建二级索引,代码复杂度和性能开销双双爆炸。**用大端定长 key,把有序性"外包"给 RocksDB 的字节序,是 TiKV 源码里最朴素也最高频的技巧之一**——《LevelDB》那本讲的 LSM 有序性,在这里被吃干榨净。

---

## 七、章末小结

### 回扣主线

本章是全书第 1 篇地基章,服务**复制层**:它立起了"被复制的单位"——Region。后续所有"复制层"的内容(multi-Raft、raftstore、RaftEngine、Apply、分裂迁移)都围绕 Region 展开;而事务层(P4 Percolator)也要先知道"key 落在哪个 Region"才能发起跨组事务。**Region 是复制层和事务层共同的"地理坐标"**。

一句话总结:Region 是一段连续的 key range(保序,支撑范围扫描),加上一组 Peer(副本)和两个版本号(防陈旧消息);它在 RocksDB 里靠两级前缀隔离(系统数据 `0x01` / 用户数据 `z`)和大端定长 key(字节序即索引)被高效组织;它的大小是个甜点(v1 默认 256MB,v2 默认 10GB),平衡着 Raft 组开销和调度灵活度。

### 五个为什么

1. **为什么按 key range 切,不按 hash 切?**——数据库要支持高效范围扫描和有序访问,range 切保序,hash 切打散序;热点问题用 PD 调度治,而不是放弃保序。
2. **为什么用户 key 要加 `z` 前缀?**——把用户数据和系统内部数据(Region 元数据、Raft 状态等)在 RocksDB 里隔离,扫用户数据时不会碰到系统数据,range 边界能干净卡住。
3. **为什么 RegionEpoch 有两个版本号?**——key range 变化(version)和副本配置变化(conf_ver)是两类不同的事,分两个号才能精确判断跨 Peer 消息是否过期,防陈旧消息搞乱新 Region。
4. **为什么 Region 默认 256MB 而不是 96MB 了?**——8.3.0+ 集群规模变大,96MB 导致 Region 数量爆炸(PD 路由表、跨组事务开销、Raft 状态机常驻内存都是压力),256MB 在"减少开销"方向拨一格,代价是单次分裂迁移变重但可接受。
5. **为什么 key 用大端定长编码?**——让 RocksDB 的字节序直接等于数值序,同一 Region 的日志/元数据天然有序聚成一段,range scan 零额外排序;变长编码虽省空间但破坏有序性,得不偿失。

### 想继续深入往哪钻

- **key 编码全貌**:读 `components/keys/src/lib.rs`(本章大量引用)和 `components/codec/src/{byte,number}.rs`(memcomparable + 数值编码)。
- **Region 元数据落盘**:读 `components/raftstore/src/store/region_meta.rs`(`RegionLocalState` 的序列化视图 `RegionMeta`)和 `store/util.rs`(`RegionReadProgress`,P4-15 读时会用到)。
- **Region 大小调参**:读 `components/raftstore/src/coprocessor/config.rs`(`SPLIT_SIZE`、`region_max_size = split*1.5`)和 `coprocessor/split_check/`(P2-08 详拆分裂判定)。
- **承接前作**:本章用到的"LSM-tree 按 key 有序"的性质,以及 SST/MemTable 的范围扫描能力,在《LevelDB》那本拆透;本书 P3-09 会讲 TiKV 怎么用 RocksDB 的 Column Family。
- **kvproto 的 Region 定义**:kvproto 是外部 crate(`pingcap/kvproto`),`metapb.proto` 里 Region/Peer/RegionEpoch 的字段定义公开可查;本地 TiKV 仓没 vendored 它的源,但通过 TiKV 的使用点(如 `region_meta.rs`)能看到全部字段。

### 引出下一章

Region 立起来了——每段 key range 是一个独立单位,有自己的副本、自己的版本号。下一个问题立刻冒出来:**一个 TiKV 进程里有几十万个 Region,也就是几十万个独立的 Raft 组,怎么让它们在一个进程里同时跑起来?** 最朴素的念头是"每个 Raft 组开一个线程定时推进",可几十万个线程会撑爆操作系统。这正是下一章 P1-03 要拆的——**Raft 库回顾与 multi-Raft 的挑战**。

> **下一章**:[P1-03 · Raft 库回顾与 multi-Raft 的挑战](P1-03-Raft库回顾与multi-Raft的挑战.md)
