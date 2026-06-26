# 附录 B · RocksDB 工具链与调优实践

> **核心问题**:你读完了前 22 章,知道了 WriteGroup 怎么攒批、InlineSkipList 怎么并发插、三种 Compaction 各解决什么、WriteController 怎么反压、Block Cache 怎么多档 pin、Bloom/Ribbon 怎么早退、Column Family 怎么隔离。但真到线上,你面对的不是"讲清一个机制",而是"我的写延迟为什么突然从 0.5ms 飙到 50ms"、"我的磁盘为什么越用越满明明数据量没涨"、"我这个 key 到底在哪个文件、是哪个版本"、"我这套 workload 到底该选 Level 还是 Universal"。这一章不教你"Options 文档怎么读"(那是手册的事),而是给你一套**真刀真枪可操作的方法论**:怎么用 db_bench 压测验证假设、怎么用 ldb/sst_dump 现场定位、怎么按 workload 把旋钮拧到读写放大三角上的那个点、出问题怎么顺着决策树一路查到根因。

> **读完本附录你会明白**:
> 1. 怎么用 **db_bench** 把一个 workload 跑起来,fillrandom/overwrite/readrandom/mixgraph 各压什么、关键 flag 怎么传、怎么对比 Level vs Universal Compaction 看写放大和读延迟的变化。
> 2. 怎么用 **ldb** 和 **sst_dump** 在线上现场诊断:查一个 key 落在哪个文件、dump SST 看 properties、调 MANIFEST 还原版本演进、repair 一个坏掉的 DB。
> 3. **按 workload 调 Options 的方法论**:写多、读多、省盘、大 value 四类典型 workload,每类给一组推荐旋钮组合,讲清每个旋钮转一下读写放大三角上的点往哪移(回扣 P0-01)。
> 4. **Statistics 和 PerfContext 怎么读**:关键 ticker(BLOCK_CACHE 命中率、BLOOM_FILTER_USEFUL 负命中率、WRITE_AMPLIFICATION、STALL_MICROS、COMPACTION_TIME)怎么解读,PerfContext 怎么把一次 Get 的耗时拆到 block cache miss / bloom / key compare / block checksum。
> 5. **线上四大类故障的排查决策树**:写延迟突增、读放大变大、空间不回收、Compaction 跟不上,每类给一棵从现象到根因再到对应章节的决策树。
> 6. **与 TiKV/MySQL(MyRocks)集成的常见坑**:CF 怎么划、write_buffer_size 怎么调、rate_limiter 怎么不抢前台,诚实讲框架(讲 RocksDB 侧的旋钮,不编对方系统内部细节)。

> **如果只想快速上手**:先记住三件事——① 所有调优都是"在读写放大三角上挪点",先想清楚你的 workload 要优化哪一维,再查本附录第三节的对照表拧旋钮;② 出问题先跑 `rocksdb.levelstats` + `rocksdb.cfstats` + `rocksdb.actual-delayed-write-rate` 三个 GetProperty,80% 的故障一眼能定位;③ db_bench 是验证假设的唯一手段,任何线上改动先在 db_bench 复现再上线。

---

## 〇、一句话点破

> **RocksDB 的调优不是"背参数",而是"测——看——拧"的闭环:用 db_bench 把你的 workload 复现出来,用 Statistics/PerfContext/GetProperty 看清当前三角上的点在哪儿,再按 workload 对照表拧旋钮把它挪到你想去的位置。出问题,也是这个闭环——先看(levelstats/cfstats/stall stats)定位故障类型,再沿决策树查到根因。**

这是结论。本附录倒过来拆:先给工具(db_bench 压什么、ldb/sst_dump 怎么用),再给方法论(按 workload 选旋钮的对照表),再给可观测(Statistics/PerfContext 怎么读),最后给排查清单(四类故障的决策树)。

---

## 一、工具链全景:你手边有什么

先把 RocksDB 自带的工具摸清楚。源码都在 `tools/` 目录下,11.6.0 实测(`tools/` 目录,ls 核实):

| 工具 | 主程序源码 | 干什么 |
|------|-----------|--------|
| `db_bench` | `tools/db_bench_tool.cc`(40 万行,真正逻辑在这) + `tools/db_bench.cc`(780 字节薄壳) | 压测:跑 fillrandom/overwrite/readrandom/mixgraph 等 benchmark,输出吞吐/延迟/直方图 |
| `ldb` | `tools/ldb_cmd.cc`(20 万行) + `tools/ldb_tool.cc` + `tools/ldb.cc`(420 字节薄壳) | 命令行操作 DB:put/get/scan/dump/manifest_dump/compact/repair/backup/restore... |
| `sst_dump` | `tools/sst_dump_tool.cc`(3 万行) + `tools/sst_dump.cc`(420 字节薄壳) | 单个 SST 文件诊断:scan(打印 key)/raw(全 dump)/show_properties(看元数据)/verify(校验)/identify |
| `blob_dump` | `tools/blob_dump.cc`(3K) | BlobDB 的 blob 文件 dump |
| `block_cache_analyzer` | `tools/block_cache_analyzer/` | 分析 block cache 的 trace(用 trace_replay 抓的) |
| `io_tracer_parser` | `tools/io_tracer_parser_tool.cc` | 分析 IO trace(用 IOTracer 抓的) |
| `trace_analyzer` | `tools/trace_analyzer.cc` | 分析操作 trace(Get/Put/Seek 的访问序列) |

> **钉死这件事**:`db_bench.cc` 和 `ldb.cc` 这两个看起来"应该是主程序"的文件,**都只是薄壳**(几百字节,只调一行 `RunBench`/`LDBTool::Run`),真正的逻辑在 `db_bench_tool.cc`(40 万行)和 `ldb_cmd.cc`(20 万行)。你要看 db_bench 支持哪些 benchmark、ldb 支持哪些子命令,得去 `_tool.cc`/`_cmd.cc` 找,别在薄壳里白找。

这三个工具(`db_bench`/`ldb`/`sst_dump`)是 90% 的日常现场工具。下面三节分别拆。

---

## 二、db_bench:怎么把 workload 跑起来

db_bench 是 RocksDB 官方的压测工具,也是 TiKV/MyRocks 调优时复现 workload 的标准武器。它的核心思想是:用一组 `--benchmarks=a,b,c` 指定的 benchmark 序列,在一组 `--xxx=yyy` 的 flag 配置下,跑出一个 DB 的吞吐和延迟。

### 2.1 它支持哪些 benchmark(ls 核实)

源码在 `tools/db_bench_tool.cc` 第 115~273 行,`DEFINE_string(benchmarks, "...", "...")` 把所有支持的 benchmark 列了出来(默认值是一长串,真实使用时你只会挑几个)。挑线上最常用的几个讲清:

| benchmark | 干什么 | 压的是哪条路径 |
|-----------|--------|---------------|
| `fillseq` | 顺序写 N 个 key(async) | 纯写路径,无并发 |
| `fillrandom` | 随机写 N 个 key(async) | 纯写路径,随机覆盖 | 
| `overwrite` | 在已有数据上随机覆盖写 N 个 | 写路径 + Compaction 跟随(测稳态写放大) |
| `fillsync` | 随机写 N/1000 个,每个都 fsync | WAL sync 的代价(测 fsync 频率对写吞吐的影响) |
| `readrandom` | 随机读 N 次 | 纯读路径(测点查延迟和读放大) |
| `readseq` | 顺序读 | 顺序扫描(测 Iterator 吞吐) |
| `readwhilewriting` | 1 写 + N 读并发 | 读写混跑(测 Compaction/Flush 抢前台读) |
| `readwhilescanning` | 1 扫 + N 随机读 | 长扫描挤 cache 的效应 |
| `mixgraph` | 按 mix_graph 论文模型混合 Get/Put/Seek,带 skew | 最贴近真实业务的混合 workload |
| `seekrandom` | 随机 Seek + Next 若干次 | 范围扫描的开销 |
| `updaterandom` | 随机 read-modify-write | 读改写的放大 |
| `compact`/`compactall` | 全量 Compaction | 测一次全 compact 的耗时和空间收敛 |
| `waitforcompaction` | 等后台 compaction 跑完 | 隔离变量(让 compaction 跑完再测下一项) |
| `stats`/`levelstats`/`sstables` | 打印 DB 统计 | 配合其他 benchmark 看 snapshot |

> **钉死这件事**:`fillrandom` 和 `overwrite` 是两个最常被搞混的 benchmark。`fillrandom` 是"往空 DB 写",第一次写,Compaction 还没起来;`overwrite` 是"往已有数据的 DB 写",会持续触发 Flush 和 Compaction,**这才是测稳态写放大和写吞吐的正确姿势**。你要量 SSD 寿命相关的写放大,必须用 `overwrite`(或 `fillrandom` 写满后再 `overwrite`),不能只看 `fillrandom`。

### 2.2 一组典型的 fillrandom + readrandom 跑法

最经典的"先写满,再测读":

```bash
# 1. 先 fillseq 写满 1 亿个 key(顺序写,模拟冷数据导入)
./db_bench --benchmarks=fillseq \
    --num=100000000 --value_size=1024 \
    --compression_type=snappy \
    --use_existing_db=false \
    --statistics --histogram \
    --db=/tmp/rocksdb_bench

# 2. 用同一个 DB,测 readrandom(随机读 100 万次,32 线程)
./db_bench --benchmarks=readrandom \
    --num=100000000 --reads=1000000 \
    --threads=32 \
    --use_existing_db=true \
    --statistics --histogram \
    --db=/tmp/rocksdb_bench
```

几个关键 flag(ls 核实):

- `--num`:总共多少个 key(默认 1000000,即一百万)。
- `--reads`:读多少次(`readrandom` 用),默认 -1 表示等于 `--num`。
- `--threads`:并发线程数(默认 1)。读压测一定要调大,否则单线程压不出 cache miss。
- `--value_size`:value 多大字节(默认 100)。
- `--use_existing_db`:true 用已有 DB,false 新建(默认 false)。
- `--compression_type`:压缩算法(默认 snappy,可选 no/zstd/lz4 等)。
- `--statistics`/`--histogram`:打印 Statistics 和延迟直方图。
- `--db`:DB 路径。
- `--duration`:跑多久秒(和 `--num` 二选一,测稳态常用 duration)。

### 2.3 对比 Level vs Universal Compaction:看写放大和读延迟怎么变

这是 db_bench 最有教学价值的用法——亲手验证 P0-01 的"读写放大三角"。用同一个 workload,切 Compaction 策略,看写放大和读延迟的此消彼长:

```bash
# 方案 A:Level Compaction(默认),测稳态 overwrite 写放大
./db_bench --benchmarks=fillrandom,overwrite,stats \
    --num=50000000 --value_size=1024 \
    --comp_style=0 \
    --max_bytes_for_level_base=268435456 \
    --level0_file_num_compaction_trigger=4 \
    --use_existing_db=false \
    --statistics \
    --db=/tmp/rocksdb_level

# 方案 B:Universal Compaction,同样 workload
./db_bench --benchmarks=fillrandom,overwrite,stats \
    --num=50000000 --value_size=1024 \
    --comp_style=1 \
    --compaction_options_universal="{size_ratio=1;min_merge_width=2;max_size_amplification_percent=200;}" \
    --use_existing_db=false \
    --statistics \
    --db=/tmp/rocksdb_universal
```

跑完看输出里的两个数:

- **写放大**(`stats` 输出里的 `Write amplification` 行):Level 默认 10~30 倍,Universal 通常 3~8 倍。
- **读延迟**(readrandom 的 P50/P99 直方图):Universal 因为 L0 宽、层数不规整,读放大比 Level 大,P99 通常高 1.5~3 倍。

> **不这样会怎样**:如果你不亲手跑这一组对比,只看书上讲"Universal 用空间换写",你不会真的"信"。亲手跑出来看到 Level 写放大 25 倍、Universal 写放大 5 倍,而 Universal 的 P99 读延迟高了一截——这一刻 P0-01 的读写放大三角才在你脑子里"活"了。这也是为什么本附录反复强调"db_bench 是验证假设的唯一手段"。

### 2.4 mixgraph:最贴近真实业务的混合 workload

`fillrandom + readrandom` 是"先写满再只读",真实业务是"边写边读还有 skew"。db_bench 的 `mixgraph` 按 [the mix_graph paper](https://github.com/facebook/rocksdb/raw/main/tools/db_bench_tool.cc) 的模型混合 Get/Put/Seek,关键 flag(实测,`tools/db_bench_tool.cc` 1708~1766 行):

```bash
./db_bench --benchmarks=mixgraph \
    --num=100000000 \
    --threads=32 \
    --mix_get_ratio=0.85 --mix_put_ratio=0.14 --mix_seek_ratio=0.01 \
    --keyrange_dist_a=0.0 --keyrange_dist_b=0.0 \
    --keyrange_dist_c=0.0 --keyrange_dist_d=0.0 \
    --keyrange_num=1 \
    --key_dist_a=0.0 --key_dist_b=1.0 \
    --sine_mix_rate=true \
    --sine_mix_rate_noise=0.2 \
    --mix_max_scan_len=1000 \
    --mix_max_value_size=1024 \
    --duration=600 \
    --db=/tmp/rocksdb_mixgraph
```

参数解释:

- `--mix_get_ratio`/`--mix_put_ratio`/`--mix_seek_ratio`:Get/Put/Seek 的比例(三者加起来应该是 1.0)。
- `--keyrange_dist_a..d`:key range 访问分布的参数(`f(x)=a*exp(b*x)+c*exp(d*x)`),用来模拟"热点 key range"。
- `--key_dist_a..b`:单个 key range 内 key 的访问分布(`f(x)=a*x^b`)。
- `--sine_mix_rate`:开启正弦波 QPS 控制(模拟昼夜峰谷)。
- `--duration`:跑多少秒(混合 workload 用 duration 比 num 更直观)。

mixgraph 的参数复杂,但调对了能非常贴近线上。Facebook 自己调优 MyRocks 就是用 mixgraph 复现生产 workload。

### 2.5 db_bench 的局限(诚实讲)

db_bench 不是万能的:

- **它压的是单机 RocksDB**,不带网络。TiKV/etcd 那种"Raft 复制 + 网络往返"的延迟它压不出来。要看分布式延迟,得用对应系统的压测工具(TiKV 的 go-ycsb、etcd 的 bench)。
- **它的 key/value 生成是合成的**,真实业务的 key 分布(比如 TiKV 的 MVCC key 是 `key_commit_ts`)、value 结构(比如带 schema 的 row)它模拟不了,要靠 `keyrange_dist` 等参数尽量拟合。
- **它的 Statistics 是进程内的**,跨进程/跨机器的聚合它不做。生产监控要靠 Prometheus + RocksDB exporter。

但作为"单机引擎层的假设验证",db_bench 已经够用了。线上调优的绝大多数决策(选 Compaction、调 cache、定反压阈值),都能在 db_bench 上先验证再上线。

---

## 三、ldb 和 sst_dump:现场诊断的两把刀

线上出了问题——"这个 key 怎么读出来的值不对"、"磁盘怎么满了"、"DB 怎么打不开了"——这时候 db_bench 帮不上忙(它只测性能),要靠 ldb 和 sst_dump 现场看。

### 3.1 ldb:DB 级的瑞士军刀

`ldb` 是 RocksDB 的命令行工具,子命令分两类(ls `tools/ldb_tool.cc` 105~149 行核实):**Data Access**(put/get/scan/delete/batchput/approxsize/checkconsistency...)和 **Admin**(dump/load/manifest_dump/compact/reduce_levels/repair/backup/restore/checkpoint/list_column_families/get_property/write_external_sst/ingest_external_sst/unsafe_remove_database_file_dumper...)。

最常用的几个:

```bash
# 1. 查一个 key(最常用,排查"这个 key 到底是什么值")
./ldb --db=/path/to/db get abc
./ldb --db=/path/to/db --hex get 0x616263

# 2. 范围扫描(排查"这个区间的数据对不对")
./ldb --db=/path/to/db scan --from=aaa --to=zzz --max_scan_size=1000

# 3. dump 整个 DB 成一个 ASCII 文件(冷备份 / 排查 / 迁移)
./ldb --db=/path/to/db dump --from=... --to=... > /tmp/dump.txt

# 4. 看 MANIFEST(这是排查"版本怎么演进到这一步"的利器)
./ldb --db=/path/to/db manifest_dump
./ldb --db=/path/to/db manifest_dump --path=/path/to/db/MANIFEST-000123

# 5. 看每个 level 多少文件、多大(levelstats,排查 L0 堆积)
./ldb --db=/path/to/db get_property rocksdb.levelstats

# 6. 全量手动 compact(线上空间收敛的标准操作)
./ldb --db=/path/to/db compact

# 7. repair 一个坏掉的 DB(WAL/MANIFEST 损坏时救命)
./ldb --db=/path/to/db repair

# 8. 备份和恢复
./ldb --db=/path/to/db backup --backup_dir=/tmp/backup
./ldb --db=/path/to/db restore --backup_dir=/tmp/backup --db=/tmp/restored

# 9. 列出所有 Column Family(排查"我有哪些 CF")
./ldb --db=/path/to/db list_column_families
```

> **技巧精解·manifest_dump 是排查版本演进的利器**:线上最容易让人抓狂的故障之一是"磁盘上明明有这个文件,DB 却打不开"或者"这个 key 读出来是旧值"。根因往往在 MANIFEST——MANIFEST 记录了"哪个 SST 在哪一层、什么时候加进来的、什么时候被删掉的"(详见 P2-09)。`ldb manifest_dump` 把这个二进制文件翻译成人能读的文本,你能看到完整的 VersionEdit 序列:AddFile / DeleteFile / ColumnFamily / LogNumber... 排查"这个文件到底是不是当前 Version 的"一目了然。配合 `ldb get` 看 key 当前值、`sst_dump --show_properties` 看某个 SST 的元数据,三者联用就是"这个 key 在哪个文件、什么版本"的完整诊断链。

ldb 的一个重要 flag 是 `--try_load_options`(默认 true 当指定了 db 且不是新建):从 DB 目录下的 `OPTIONS-xxxxx` 文件加载真实 Options,而不是用默认 Options 打开。这意味着你用 ldb 操作 DB 时,它读到的 Options 和你业务进程一样,行为一致。如果 OPTIONS 文件坏了或丢了,加 `--ignore_unknown_options` 跳过未识别的字段。

### 3.2 sst_dump:单个 SST 文件的显微镜

`sst_dump` 用来诊断**单个 SST 文件**(ls `tools/sst_dump_tool.cc` 39~137 行核实命令)。线上"怀疑某个文件坏了"、"想看这个文件里有哪些 key"、"这个文件的 Bloom 多密、压缩用了哪种"——sst_dump 是唯一手段:

```bash
# 1. 看一个 SST 的元数据(properties:大小、key 数、compression、bloom bits...)
./sst_dump --file=/path/to/db/000123.sst --show_properties

# 2. 扫描打印所有 key(排查"这个文件里到底有什么")
./sst_dump --file=/path/to/db/000123.sst --command=scan

# 3. 只看某个 key 区间
./sst_dump --file=/path/to/db/000123.sst --command=scan --from=aaa --to=zzz

# 4. 全 dump 成一个 _dump.txt 文件(连 data block 内容都展开,排查 block 级问题)
./sst_dump --file=/path/to/db/000123.sst --command=raw

# 5. 校验 checksum(排查"这个文件有没有损坏")
./sst_dump --file=/path/to/db/000123.sst --command=verify

# 6. 只看这个文件是哪个 DB 的(identify,确认文件归属)
./sst_dump --file=/path/to/db/000123.sst --command=identify

# 7. 用不同压缩算法重压一遍,看每种算法的压缩率和速度
./sst_dump --file=/path/to/db/000123.sst --command=recompress --compression_type=zstd
```

`--show_properties` 是最常用的,它打印 SST 的 TableProperties,关键信息:

```
# data blocks
# entries
# deletions
# range deletions
# key range
# compression ratio
raw key size, raw value size
# bloom filter bits
filter policy name
index format, ...
```

从这里你能看到:这个 SST 有多少 key、有多少是墓碑(deletion)、压缩率多少、Bloom 多少位/key、index 是 binary 还是 partitioned。这一组数据就是判断"这个 SST 是否健康"的体检表。

> **钉死这件事**:`--command` 是 sst_dump 的核心 flag,它有 6 个值:`check`(默认,类似 scan 但带校验)、`scan`(打印 key)、`raw`(全 dump 成文件)、`verify`(只校验)、`identify`(只报文件归属)、`recompress`(用不同算法重压看压缩率)。不加 `--command` 时默认 `check`。`--show_properties` 是独立的 bool flag,可以和任何 command 组合。

### 3.3 一个完整的诊断链:这个 key 到底在哪个文件、什么版本

把 ldb 和 sst_dump 串起来,就是线上最常用的"定位一个 key 的完整生命周期"的诊断链:

```
1. ldb get <key>                    → 看 DB 视角下这个 key 当前是什么值
2. ldb manifest_dump                 → 看 MANIFEST,这个 key 涉及的 AddFile/DeleteFile 历史
3. sst_dump --command=scan          → 对怀疑的 SST 逐个 scan,看 key 在不在、什么 seq、是 Put 还是 Delete
4. sst_dump --show_properties       → 看这个 SST 的 properties(bloom 密度、compression、deletion 数)
```

> **不这样会怎样**:如果你只会 `ldb get`,你看到的是"当前值",但讲不清"为什么是这个值、它从哪来"。配合 manifest_dump 和 sst_dump,你能还原出"这个 key 最初在文件 A 写入(seq=100),后来在文件 B 被覆盖(seq=200),文件 A 因为被 snapshot 引用所以没被 compact 掉"——这一层理解,是"用 RocksDB"和"会用 RocksDB"的分水岭。

---

## 四、Options 调优方法论:按 workload 拧旋钮

工具讲完了,讲方法论。RocksDB 有几百个 Options,凭直觉调必错。本节给一套"按 workload 分类"的调优方法论:先判断你的 workload 属于哪一类,再查对照表拧对应的旋钮。

### 4.1 先判断你的 workload 属于哪一类

绝大多数生产 workload,可以归到下面四类之一(或它们的混合):

| workload 类型 | 典型场景 | 核心矛盾 | 优化目标(读写放大三角上要挪到的点) |
|--------------|---------|---------|---------------------------------|
| **写多读少** | 日志、消息队列、监控时序、TiKV 的 default CF | 写放大吃 SSD 寿命、Compaction 抢 IO | 写放大小、SSD 寿命长(牺牲空间放大、读放大) |
| **读多写少** | 在线服务的点查、缓存层、配置中心 | 点查延迟敏感、读放大拖慢 P99 | 读放大小、P99 低(牺牲写放大) |
| **省盘优先** | 冷数据、归档、成本敏感 | 空间放大、磁盘贵 | 空间放大小(牺牲写放大、Compaction 勤做) |
| **大 value** | 文档存储、图片元数据、消息体 | 大 value 撑爆 Compaction | value 分离,LSM 只存指针(开 BlobDB) |

判断完类型,查下面四张表,每张表给"推荐 Compaction + 推荐旋钮组合 + 每个旋钮在三角上挪点往哪移"。

> **回扣 P0-01**:每个旋钮都是读写放大三角上挪点的一个动作。下面表里"代价"列,就是"这个旋钮让点往三角的哪条边移"——选 workload 类型,本质是选你要优化三角的哪条边。

### 4.2 写多读少:写放大敏感场景

**推荐 Compaction**:**Universal Compaction**(`--comp_style=1`)。Universal 把大小相近的文件直接合并,不逐层下压,一个 key 被重写的次数远少于 Level(详见 P4-15)。

**推荐旋钮组合**(基于真实默认值,源码 Grep 核实):

| 旋钮 | 推荐值 | 默认值 | 在三角上怎么挪点 |
|------|--------|--------|-----------------|
| `compaction_style` | `kCompactionStyleUniversal`(1) | `kCompactionStyleLevel`(0) | 写放大↓ 空间放大↑ 读放大↑ |
| `write_buffer_size` | 128MB~512MB | 64MB(`options.h:191`) | MemTable 大,Flush 次数少,写放大↓ 内存↑ |
| `max_write_buffer_number` | 4~6 | 2(`advanced_options.h:271`) | 多 MemTable 攒更多写再 Flush,突发写扛得住 |
| `level0_file_num_compaction_trigger` | 4(或更大,Universal 下不敏感) | 4(`options.h:255`) | L0 文件攒多少才 compact |
| `compaction_options_universal.max_size_amplification_percent` | 200~500 | 200(`universal_compaction.h:48`) | 空间放大上限(放宽省写放大) |
| `compaction_options_universal.size_ratio` | 1~2 | 1(`universal_compaction.h:30`) | 多大差异算"相近",调大更激进合并 |
| `max_background_jobs` | 8~16 | 2(`options.h:895`) | 后台 Compaction/Flush 并发多 |
| `bytes_per_sync` | 1MB~2MB | 0(`options.h:1319`,关闭) | 后台刷盘更平滑,减少 fsync 风暴 |

**典型组合**:TiKV 的 default CF(存海量 KV)就是写多读少,TiKV 默认给 default CF 用较大的 `write_buffer_size`、较多的后台线程。详见本附录第六节的集成坑。

### 4.3 读多写少:读放大敏感场景

**推荐 Compaction**:**Level Compaction**(`--comp_style=0`,默认)。Level 层数规整、读放大小(详见 P4-14)。或者用 Level 但压层数(`num_levels` 调小到 4~5)。

**推荐旋钮组合**:

| 旋钮 | 推荐值 | 默认值 | 在三角上怎么挪点 |
|------|--------|--------|-----------------|
| `compaction_style` | `kCompactionStyleLevel`(0) | 0(`options.h:1867`) | 默认,读放大小 |
| `num_levels` | 4~6(数据量小可以压) | 7(`advanced_options.h:538`) | 层数少,读一次穿过的层数少,读放大↓ 空间放大↑ |
| `max_bytes_for_level_multiplier` | 8~10 | 10(`advanced_options.h:671`) | 倍数小,每层不大,读放大↓ 写放大↑ |
| `BlockBasedTableOptions.block_cache` | 大(2~8GB,看内存) | 32MB 内部 cache(`table.h:383`) | 热数据全进内存,读放大↓ 内存↑ |
| `BlockBasedTableOptions.cache_index_and_filter_blocks` | true | false(`table.h:238`) | index/filter 进 cache 可淘汰,大 DB 必开(否则 index 常驻内存爆) |
| `BlockBasedTableOptions.pin_l0_filter_and_index_blocks_in_cache` | true | false(`table.h:267`) | L0 的 index/filter 钉在 cache,L0 查得快 |
| `BlockBasedTableOptions.block_size` | 4KB~16KB | 4K | 小 block,读少浪费,适合点查 |
| `filter_policy` | Bloom 10~14 bits/key,或 Ribbon | 默认 10 bits Bloom | 密 Bloom,假阳性低,负命中率↑,读放大↓ |
| `BlockBasedTableOptions.partition_filters` + `index_type` | partitioned | binary index | 大索引分页,不一次载入,内存↓ |
| `row_cache`(DB 级) | 开 1GB | 关 | 整行缓存,点查命中率高时超有效 |
| `memtable_prefix_bloom_size_ratio` | 0.1 | 0.0(`advanced_options.h:436`) | MemTable 也有 Bloom,point lookup 早退 |
| `max_background_jobs` | 8 | 2 | 后台不拖前台 |

> **钉死这件事(读多的灵魂)**:**`cache_index_and_filter_blocks=true` 是大 DB 的必开项**。为什么?因为 RocksDB 每个 SST 打开时,默认会把它的 index block 和 filter block 常驻内存(不进 cache 不可淘汰)。一个 1TB 的 DB 可能有上万个 SST,每个 SST 的 index+filter 几 MB,常驻起来轻松几十 GB——内存爆。开了 `cache_index_and_filter_blocks`,index/filter 进 cache 可按 LRU 淘汰,内存可控。代价是冷 SST 的第一次查要重新载入 index/filter(一次额外的 IO)。这是 P2-07 讲的"index/filter 分离与分区"在生产里的落地,务必开。

### 4.4 省盘优先:空间放大敏感场景

**推荐 Compaction**:**Level Compaction + 勤 Compaction**,或 Level + 较低的 `max_bytes_for_level_multiplier`(让每层更紧凑)。

**推荐旋钮组合**:

| 旋钮 | 推荐值 | 默认值 | 在三角上怎么挪点 |
|------|--------|--------|-----------------|
| `compaction_style` | Level | Level | 勤 Compact,空间收敛 |
| `max_bytes_for_level_multiplier` | 5~8 | 10 | 倍数小,每层紧凑,空间↓ 写放大↑ |
| `level0_file_num_compaction_trigger` | 2~3 | 4 | L0 攒少点就 compact,空间↓ 写放大↑ |
| `max_write_buffer_number` | 2 | 2 | 不攒多 MemTable,Flush 勤 |
| `CompactionFilter`(自定义) | 删过期/旧版本 | 无 | 在 compaction 时过滤,空间↓ |
| `ttl`(FIFO) | 设 TTL | 无 | 时序数据自动过期,空间↓ |
| 手动 `CompactRange` 定期全量 | 定期跑 | — | 收敛到底层,空间↓ |
| `compression_type` | zstd(压缩率高) | snappy | 压缩率高,空间↓ CPU↑ |

省盘的代价是写放大——Compaction 勤做,每个 key 被重写的次数多。SSD 寿命和空间的权衡,看你硬件成本和数据成本哪个贵。

### 4.5 大 value:value 分离场景

**核心**:**开 BlobDB**(`enable_blob_files=true`)。BlobDB 把大 value 存到独立的 blob 文件,LSM 里只存指针(index + 小 metadata),Compaction 不再每次重写大 value(详见 P6-22)。

**推荐旋钮组合**:

| 旋钮 | 推荐值 | 默认值 | 在三角上怎么挪点 |
|------|--------|--------|-----------------|
| `enable_blob_files` | true | false | 开 value 分离 |
| `min_blob_size` | 2KB~256KB(看 value 分布) | 0 | 多大 value 才分离到 blob,小 value 还是留 LSM |
| `blob_file_size` | 256MB | 256MB | 单个 blob 文件大小 |
| `blob_compression_type` | zstd/lz4 | no | blob 文件压缩 |
| `enable_blob_garbage_collection` | true | false | blob GC,回收孤儿 blob(blob 文件的 Compaction) |
| `blob_garbage_collection_age_cutoff` | 0.25 | 0.25 | 多老的 blob 参与 GC |
| `blob_garbage_collection_force_threshold` | 0.75 | 0.75 | blob 空间放大到多少强制 GC |
| `blob_compaction_replay_cache_size` | 视内存 | 默认 | blob 重放的 cache |

> **不这样会怎样**:不开 BlobDB,一个 100KB 的 value,每次 Compaction 都被完整重写一遍。一个 key 从 L0 合到 L6 重写 7 次,实际写盘 700KB,这还只是一个 key。如果你的 value 是图片/文档/消息体,Compaction 的 IO 会被大 value 撑爆,SSD 寿命快速消耗——这就是为什么 P6-22 把 BlobDB 列为"扛大 value workload 的招牌"。开 BlobDB,LSM 里只存 ~30 字节的 blob 指针,Compaction 重写的是指针不是 value,写放大大幅下降。

### 4.6 调优方法论小结:测——看——拧的闭环

把四张表合起来,RocksDB 调优的闭环就是:

```
1. 测:用 db_bench 复现你的 workload(fillrandom/overwrite/readrandom/mixgraph)
2. 看:用 Statistics/PerfContext/GetProperty 看当前三角上的点在哪
   - 写放大多少?(看 stats 的 Write amplification)
   - 读放大多少?(看 PerfContext 的 block_read_count)
   - 空间放大多少?(看 SST 总大小 vs 逻辑数据量)
   - Bloom 负命中多少?(看 BLOOM_FILTER_USEFUL ticker)
3. 拧:按上面四张表查 workload 类型,拧对应旋钮
4. 再测:回到第 1 步,验证假设
```

> **钉死这件事**:这个闭环里,**"看"是最容易被跳过的一步**。很多人凭直觉拧旋钮("我加大 cache 应该会快"),不看数据,结果拧错了方向(比如 cache 加大了但 Bloom 没调,负命中还是多,读放大没降)。**任何调优,先看数据再拧旋钮,拧完再测验证**——这是和"凭经验拍脑袋"的根本区别。

---

## 五、可观测:Statistics 和 PerfContext 怎么读

"看"这一步,核心工具是 Statistics(进程级聚合指标)和 PerfContext(单次操作的细粒度耗时)。它们是上面"测——看——拧"闭环里"看"的两大件。

### 5.1 Statistics:进程级的 Ticker 和 Histogram

Statistics 在 `include/rocksdb/statistics.h`,定义了几百个 **Ticker**(计数器,单调递增)和几十个 **Histogram**(延迟分布)。开 Statistics:

```cpp
options.statistics = rocksdb::CreateDBStatistics();
options.statistics->set_stats_level(StatsLevel::kExceptDetailedTimers);
```

关键 ticker(源码 `statistics.h` 核实行号)和怎么解读:

| Ticker | 行号 | 解读 |
|--------|------|------|
| `NUMBER_KEYS_WRITTEN` | 169 | 逻辑写次数 |
| `NUMBER_KEYS_READ` | 171 | 逻辑读次数 |
| `BLOCK_CACHE_HIT` / `BLOCK_CACHE_MISS` | 41 / 36 | block cache 命中率 = HIT / (HIT+MISS),低于 90% 要扩 cache |
| `BLOCK_CACHE_INDEX_HIT/MISS` | (分角色) | index block 命中率,低说明 index 没进 cache 或 cache 太小 |
| `BLOCK_CACHE_FILTER_HIT/MISS` | (分角色) | filter block 命中率,低同上 |
| `BLOOM_FILTER_USEFUL` | 112 | Bloom 帮助避免的 block 读次数(Bloom 说"不在"而省的读) |
| `BLOOM_FILTER_FULL_POSITIVE` | (相关) | Bloom 说"在"且确实在的次数 |
| `WRITE_DONE_BY_OTHER` | 240 | follower 写被 leader 捎带完成的次数(写组效率) |
| `WRITE_WITH_WAL` | 241 | 带 WAL 的写次数 |
| `STALL_MICROS` | 205 | 写被 stall 的微秒数(WriteController 反压的总时间) |
| `COMPACTION_TIME` | 637 | Compaction 累计耗时 |

**最关键的三个解读**:

1. **Block cache 命中率** = `BLOCK_CACHE_HIT / (BLOCK_CACHE_HIT + BLOCK_CACHE_MISS)`。生产应该 >95%。低于 90%,要么 cache 太小,要么 workload 的 working set 太大(cache 装不下)。注意:这个命中率分角色看(data/index/filter),如果 index 命中率低但 data 高,说明 `cache_index_and_filter_blocks` 没开或 cache 太小被 data 挤掉了。

2. **Bloom 负命中率**。Bloom 的价值是"一个 Get 来了,先问 Bloom 这个 key 在不在,如果 Bloom 说不在就跳过这个 SST,省一次读"。`BLOOM_FILTER_USEFUL` 就是"被 Bloom 救下的读次数"。如果你发现 `BLOOM_FILTER_USEFUL` 很低(占总 read 比例小),说明 Bloom 没起作用——可能是 `filter_policy` 没设、bits/key 太低(Bloom 太稀疏假阳性高)、或 workload 是顺序扫描不用 Bloom。详见 P2-08 和 P3-11。

3. **STALL_MICROS 和 actual-delayed-write-rate**。`STALL_MICROS` 是 WriteController 把写拖慢的总时间(详见 P5-17)。配合 `rocksdb.actual-delayed-write-rate` 这个 GetProperty,你能看到"现在写被拖慢到什么速度"。如果 `STALL_MICROS` 在飙升,说明 L0 文件数 / MemTable 数 / pending compaction bytes 触发了 slowdown 或 stop——这就是本附录第七节"写延迟突增"故障的根因信号。

### 5.2 PerfContext:单次操作的细粒度耗时

Statistics 是聚合的,PerfContext 是**单次操作**(一次 Get/Put)的细粒度计数。源码 `include/rocksdb/perf_context.h`(核实行号):

```cpp
// 开启 PerfContext(在 Get 之前设 perf_level)
rocksdb::SetPerfLevel(rocksdb::PerfLevel::kEnableTimeAndCPUTimeExceptMutex);
auto* perf = rocksdb::get_perf_context();
perf->Reset();
db->Get(read_options, key, &value);
// 现在 perf 里有这次 Get 的细粒度数据
```

PerfContext 关键字段(核实行号):

| 字段 | 行号 | 解读 |
|------|------|------|
| `user_key_comparison_count` | 74 | user key 比较次数(高说明要扫很多 key) |
| `block_cache_hit_count` | 75 | block cache 命中次数 |
| `block_read_count` | 76 | block 实际从盘读的次数(读放大的直接量) |
| `block_read_byte` | 77 | block 从盘读的字节数 |
| `block_cache_index_hit_count` | 81 | index block 命中 |
| `block_cache_filter_hit_count` | 88 | filter block 命中 |
| `bloom_filter_useful` | 37 | 这次操作里 Bloom 救了几次 |
| `block_checksum_time` | 108 | block checksum 校验耗时 |
| `get_read_bytes` | 112 | 这次 Get 读出的 value 字节 |
| `write_wal_time` | 198 | 写 WAL 耗时(PerfContext for Put) |
| `key_lock_wait_time` | 244 | key 锁等待(Transaction 场景) |
| `get_cpu_nanos` | 271 | 这次 Get 的 CPU 纳秒(开 kEnableTimeAndCPUTime...) |

> **技巧精解·用 PerfContext 定位一次 Get 的耗时分布**:线上"某个 Get 为什么慢",Statistics 看不出(它是聚合的)。用 PerfContext 包住这一次 Get,然后看:`block_read_count` 高不高?高 → 读放大大 → 查层数/Bloom/cache;`bloom_filter_useful` 低不低?低 → Bloom 没救命 → 查 bits/key 和 Bloom 是否启用;`block_checksum_time` 高不高?高 → CPU 被 checksum 吃了(可能开了不必要的校验或压缩算法的 checksum 重);`user_key_comparison_count` 高不高?高 → 在某个 block 里扫了很多 key(可能是 `max_sequential_skip_in_iterations` 没生效)。这一套字段,把一次 Get 的耗时拆得明明白白——这是 P3-11"Get 路径与读放大"在生产里的落地。

PerfContext 还有一个变种 **IOStats**(perf_step_timer),专门测 IO:`io_read_bytes`、`io_read_count`。两者配合,你能区分一次 Get 的慢是"CPU 慢(扫 key/checksum)"还是"IO 慢(盘读)"——这是读性能优化的第一个分叉。

### 5.3 GetProperty:一行命令看 DB 健康度

不用开 Statistics 也能看 DB 状态,RocksDB 提供了一组 `GetProperty` 字符串(ls `include/rocksdb/db.h` 1120~1462 行核实),用 ldb 或代码都能查:

```bash
# 用 ldb 查(最方便)
./ldb --db=/path/to/db get_property rocksdb.levelstats
./ldb --db=/path/to/db get_property rocksdb.cfstats
./ldb --db=/path/to/db get_property rocksdb.actual-delayed-write-rate
./ldb --db=/path/to/db get_property rocksdb.is-write-stopped
./ldb --db=/path/to/db get_property rocksdb.compaction-pending
./ldb --db=/path/to/db get_property rocksdb.mem-table-flush-pending
./ldb --db=/path/to/db get_property rocksdb.num-files-at-level0
./ldb --db=/path/to/db get_property rocksdb.estimate-num-keys
./ldb --db=/path/to/db get_property rocksdb.estimate-live-data-size
./ldb --db=/path/to/db get_property rocksdb.num-entries-active-mem-table
```

关键的几个及其解读:

| GetProperty | 含义 | 出问题的信号 |
|------------|------|-------------|
| `rocksdb.levelstats` | 每层文件数和字节数 | L0 文件数 > `level0_slowdown_writes_trigger`(20)→ 写延迟要飙 |
| `rocksdb.cfstats` | 每 CF 详细统计(写入、压缩、cache...) | 综合 |
| `rocksdb.actual-delayed-write-rate` | 当前写被拖慢到的速率(bytes/s) | 非 0 → WriteController 在 delay |
| `rocksdb.is-write-stopped` | 写是否被 stop(1=是) | 1 → L0 文件数达 `level0_stop_writes_trigger`(36),写全卡 |
| `rocksdb.compaction-pending` | 是否有待 compact(1=是) | 持续 1 → Compaction 跟不上 |
| `rocksdb.mem-table-flush-pending` | 是否有待 flush(1=是) | 持续 1 → Flush 跟不上 |
| `rocksdb.num-files-at-level0` | L0 文件数 | 接近 20 → 要触发 slowdown |
| `rocksdb.estimate-live-data-size` | 估算活数据字节数 | 配合总文件大小算空间放大 |
| `rocksdb.num-entries-active-mem-table` | active memtable 的 entry 数 | 持续涨 → 写没 flush |

> **钉死这件事**:线上故障排查的**第一动作**永远是跑这三个:`rocksdb.levelstats` + `rocksdb.cfstats` + `rocksdb.actual-delayed-write-rate`。这三个能定位 80% 的故障——L0 堆积、Compaction 跟不上、写被 stall,在这三个数上一眼能看到。这是本附录第七节排查清单的起点。

---

## 六、与 TiKV/MySQL 集成的常见坑

RocksDB 很少裸用,绝大多数情况是被上层系统(TiKV、MySQL 的 MyRocks、Kafka Streams、Cassandra)包着用。本节讲集成时 RocksDB 侧的常见调优坑——**诚实讲框架**:讲 RocksDB 侧的旋钮怎么调(CF 划分、write_buffer_size、rate_limiter),不编对方系统的内部细节(那些细节看对应系统的文档)。

### 6.1 与 TiKV 集成:CF 划分是核心

TiKV 的单机引擎就是 RocksDB(在 TiKV 里叫 `engine_rocks`),一个 Region 一个 RocksDB 实例,每个实例有 3 个 Column Family:

| CF | 存什么 | workload 特点 | RocksDB 侧调优重点 |
|----|--------|--------------|-------------------|
| `default` | 真实 KV 数据(`key_value`) | 写多,海量 | write_buffer_size 调大、Universal 可选、max_background_jobs 调大 |
| `lock` | MVCC 的锁(Percolator) | 写读都少,小 | 小 write_buffer_size,不抢资源 |
| `write` | MVCC 的 commit 记录 | 读多(查 commit ts) | 适度 Block cache,密 Bloom |

> **回扣 P5-19(Column Family)**:CF 的核心是"**共享 WAL/MANIFEST,独立 MemTable/SST/Options**"。TiKV 把数据按角色拆到三个 CF,共享一个 WAL(写一次 WAL 三个 CF 都进),又各自有独立的 MemTable/SST/Options——default CF 用大 MemTable 扛写,lock CF 用小 MemTable 省资源,write CF 调密 Bloom 加速点查。这就是 CF 架构的价值:**一个引擎实例扛多种 workload 又互不挤兑**。如果 TiKV 用三个独立的 RocksDB 实例代替 CF,WAL 写三遍、Compaction 三套后台线程、内存三份——浪费且打架。

TiKV 集成 RocksDB 侧的常见坑:

- **CF 的 write_buffer_size 要按 workload 单独调**。default CF 写多调大(128MB+),lock CF 写少调小(64MB 默认),别一刀切。
- **rate_limiter 要给后台 compaction 限速**,不能让 compaction 的 IO 抢了前台 Raft 复制的网络和磁盘(详见 P5-18)。TiKV 默认会配 rate_limiter。
- **max_background_jobs 要调大**(8~16),否则多个 CF 的 compaction 排队。
- **blob DB**:TiKV 9.x 开始集成 RocksDB 的 BlobDB,大 value(value > 1KB)分离,默认 CF 的写放大显著下降。

### 6.2 与 MySQL(MyRocks)集成:write_buffer_size 和 level0_trigger

MyRocks 是 Facebook 把 RocksDB 做成 MySQL 存储引擎的方案(MySQL 8.x 起 RocksDB 引擎是 Meta 维护,非官方)。RocksDB 侧的调优坑(框架层面,讲旋钮不讲 MySQL 内部):

- **write_buffer_size 调大**(256MB~1GB)。MyRocks 单表写入并发高,MemTable 太小 Flush 频繁,写放大爆。TiKV 的 default CF 同理。
- **level0_file_num_compaction_trigger 调大**(8~20)。MyRocks 写吞吐高,L0 文件攒少就 compact 反而拖慢——适当放宽,让 L0 攒到 8~20 个再 compact。代价是读放大略增(L0 多文件),但配合密 Bloom 和 cache 压得住。注意别调到超过 `level0_slowdown_writes_trigger`(默认 20)。
- **rate_limiter 不抢前台**:MyRocks 的 SQL 查询是前台,Compaction 是后台,rate_limiter 要给后台限速给前台让路(详见 P5-18 的 IO 优先级)。
- **compression 用 zstd 或 lz4**:MyRocks 数据量大,snappy 压缩率不够,zstd 省 30~50% 空间(CPU 略高)。

### 6.3 通用集成原则

不管和谁集成,RocksDB 侧的几条通用原则:

1. **write_buffer_size 按写吞吐调**:写多的 CF/实例调大,写少的调小。一刀切默认(64MB)在写多的场景必爆。
2. **rate_limiter 总要配**:不配的话后台 compaction 的 IO 会把前台打爆。给 rate_bytes_per_sec 设到磁盘带宽的 50~70%,给前台读高优先级(详见 P5-18)。
3. **max_background_jobs 至少 8**:默认 2 在生产完全不够,后台排队,compaction 跟不上。
4. **cache_index_and_filter_blocks 大 DB 必开**:不开内存爆(详见 4.3 节)。
5. **CF 共享一个实例,别开多个实例**:多个独立 RocksDB 实例的 WAL/Compaction/内存各自为政,资源浪费——CF 架构就是解决这个的(P5-19)。

---

## 七、线上问题排查清单:四大类故障的决策树

这是本附录的核心实践——线上故障的排查决策树。RocksDB 生产故障,绝大多数归到这四类:**写延迟突增**、**读放大变大**、**空间不回收**、**Compaction 跟不上**。每类给一棵从现象到根因再到对应章节的决策树。

### 7.1 故障一:写延迟突增(P99 从 0.5ms 飙到几十 ms)

**现象**:业务 Put 的 P99 突然飙高,日志里 RocksDB 写变慢。

**决策树**(回扣 P5-17 Write Stall 与 Write Delay):

```
写延迟突增
  │
  ├─ 第 1 步:查 rocksdb.actual-delayed-write-rate
  │   ├─ 非 0(在 delay)→ WriteController 触发了 slowdown,继续查根因
  │   └─ 0(没 delay)→ 不是 RocksDB 反压,查别的(网络/磁盘满/CPU)
  │
  ├─ 第 2 步(delay 了):查 rocksdb.is-write-stopped
  │   ├─ 1(stop 了)→ L0 文件数达 level0_stop_writes_trigger(36)
  │   │              → 根因:L0 堆积,Flush 进 L0 速度 > Compaction 出 L0 速度
  │   │              → 见 P5-17 "stop_writes_trigger" 节
  │   └─ 0(没 stop,只 delay)→ L0 文件数达 level0_slowdown_writes_trigger(20)
  │                             或 pending compaction bytes 达 soft limit
  │                             → 见 P5-17 "slowdown_writes_trigger" 节
  │
  ├─ 第 3 步:查 rocksdb.num-files-at-level0 / rocksdb.levelstats
  │   看 L0 实际文件数:
  │   ├─ 接近/超过 20 → L0 堆积
  │   │   └─ 为什么 L0 堆积?→ 查 7.4 "Compaction 跟不上"
  │   └─ 没超 → 查 MemTable 数量
  │
  ├─ 第 4 步:查 active+immutable memtable 数量
  │   ├─ 接近 max_write_buffer_number(默认 2)→ MemTable 堆积
  │   │   └─ 根因:Flush 太慢
  │   │       → max_background_flushes 太小(调大)
  │   │       → 磁盘慢(硬件问题)
  │   │       → write_buffer_size 太小(MemTable 频繁满,调大)
  │   │       → 见 P1-05 "Flush" 节
  │   └─ 正常 → 查 pending compaction bytes
  │
  └─ 第 5 步:查 STALL_MICROS / rocksdb.cfstats 的 stall 详情
      定位是哪类 stall(L0/memtable/level),再对症
```

**处置**:调大 `max_background_jobs` / `max_background_flushes`(后台并发多)、`write_buffer_size` 大些(MemTable 攒更多再 Flush,减少 Flush 频率)、调高 `level0_slowdown_writes_trigger` 和 `level0_stop_writes_trigger`(给 L0 更多缓冲,但代价是读放大略增)、检查磁盘带宽(可能 rate_limiter 限太狠)。详见 P5-17。

### 7.2 故障二:读放大变大(Get P99 变高,read 放大上升)

**现象**:点查 P99 变高,但写正常。

**决策树**(回扣 P3-11 Get 路径与读放大):

```
读放大变大
  │
  ├─ 第 1 步:查 BLOCK_CACHE_HIT/(HIT+MISS) 命中率
  │   ├─ < 90%(低)→ Block cache 不够
  │   │   └─ 处置:加大 block_cache,或加 row_cache
  │   │       检查 cache_index_and_filter_blocks 是否开(不开 index 没进 cache)
  │   │       → 见 P3-10 "Block Cache" 节
  │   └─ > 95%(高)→ cache 够,继续查 Bloom
  │
  ├─ 第 2 步:查 BLOOM_FILTER_USEFUL / 总 read 比例
  │   ├─ 低(Bloom 没救命)→ Bloom 没起作用
  │   │   ├─ filter_policy 没设?→ 设 Bloom(10~14 bits/key)或 Ribbon
  │   │   ├─ bits/key 太低(假阳性高)?→ 调高 bits/key
  │   │   ├─ workload 是顺序扫描?(Bloom 只对 point lookup 有效)
  │   │   └─ → 见 P2-08 "Bloom/Ribbon" 和 P3-11 节
  │   └─ 高(Bloom 正常)→ 继续查层数
  │
  ├─ 第 3 步:用 PerfContext 看一次 Get 的 block_read_count
  │   ├─ 高(读了很多 block)→ 读放大大
  │   │   ├─ 层数多?(num_levels=7)→ 数据量小可压到 4~5 层
  │   │   ├─ L0 文件多?(L0 不做 level 假设,每个文件都要查)→ 触发了 Intra-L0 compaction?
  │   │   ├─ max_sequential_skip_in_iterations 没生效?
  │   │   └─ → 见 P3-11 "Get 路径" 节
  │   └─ 低 → 不是读放大,查 CPU
  │
  ├─ 第 4 步:查 PerfContext 的 block_checksum_time / get_cpu_nanos
  │   ├─ checksum_time 高 → 开了不必要的 checksum?compression 的 checksum 重?
  │   └─ cpu_nanos 高但 IO 低 → CPU 瓶颈(可能压缩解压)
  │
  └─ 第 5 步:查是否有长扫描挤 cache
      readwhilescanning 场景:一个长 Seek 把热 data block 挤出 cache
      → 处置:扫描用单独的 cache(no_block_cache 配置)或限制扫描频率
```

**处置**:加大 Block cache、设密 Bloom(或换 Ribbon)、压层数、开 `cache_index_and_filter_blocks`、必要时加 `row_cache`。详见 P3-10/P3-11/P2-08。

### 7.3 故障三:空间不回收(磁盘越用越满,数据量没涨)

**现象**:DB 的 SST 总大小持续增长,但逻辑数据量没变(空间放大变大)。

**决策树**(回扣 P4 Compaction、P6-22 BlobDB、P4-16 CompactionFilter):

```
空间不回收
  │
  ├─ 第 1 步:对比 rocksdb.estimate-live-data-size 和 磁盘实际占用
  │   ├─ 实际 >> live(空间放大大)→ 有大量旧版本/墓碑/blob 孤儿
  │   └─ 接近 → 不是空间放大,查写入了多少数据(可能业务真写多了)
  │
  ├─ 第 2 步(空间放大大):查 Compaction 是否跟上
  │   ├─ rocksdb.compaction-pending = 1 持续 → Compaction 没跟上(见 7.4)
  │   ├─ disable_auto_compactions 被关了?→ 打开自动 compaction
  │   └─ → 见 P4-13 "Compaction 框架" 节
  │
  ├─ 第 3 步:查是否有长 snapshot 阻止旧版本被 compact
  │   ├─ 有 snapshot 持续不释放?→ Compaction 不能丢被 snapshot 引用的旧版本
  │   │   → 见 P6-20 "Snapshot 与 MVCC" 节
  │   │   → 处置:释放 snapshot,或调小 snapshot TTL
  │   └─ 没有 → 继续查 TTL/CompactionFilter
  │
  ├─ 第 4 步:查是否有 TTL 数据没过期
  │   ├─ FIFO Compaction 的 TTL 没配?
  │   ├─ CompactionFilter 没设(过期数据没在 compaction 时删)?
  │   └─ → 见 P4-16 "FIFO + CompactionFilter" 节
  │
  ├─ 第 5 步:开了 BlobDB 的话,查 blob 空间放大
  │   ├─ blob 文件的孤儿(被 LSM 删了但 blob 还在)?→ 开 blob GC
  │   │   enable_blob_garbage_collection=true
  │   │   blob_garbage_collection_force_threshold 调低
  │   └─ → 见 P6-22 "BlobDB" 节
  │
  └─ 第 6 步:手动 CompactRange 全量收一次
      ldb --db=... compact  或  CompactRange(start, end)
      如果空间明显降 → 确认是 Compaction 不够勤;定期跑或调勤 Compaction
```

**处置**:确认 auto compaction 开启、释放长 snapshot、配 CompactionFilter/TTL、开 BlobDB 的 GC、定期 CompactRange、调勤 Compaction(降低 `level0_file_num_compaction_trigger`、降低 `max_bytes_for_level_multiplier`)。详见 P4 / P6-20 / P6-22 / P4-16。

### 7.4 故障四:Compaction 跟不上(pending bytes 持续涨)

**现象**:`rocksdb.compaction-pending=1` 持续,pending compaction bytes 持续涨,最终触发 `soft/hard_pending_compaction_bytes_limit` 引发 Write Stall。

**决策树**(回扣 P4-13 Compaction 框架、P5-18 Rate Limiter):

```
Compaction 跟不上
  │
  ├─ 第 1 步:查 max_background_jobs / max_background_compactions
  │   ├─ 太小(默认 2)→ 后台 compaction 线程不够,排队
  │   │   → 调大到 8~16
  │   │   → 见 P4-13 "subcompaction" 节
  │   └─ 够 → 继续查 rate_limiter
  │
  ├─ 第 2 步:查 rate_limiter 的 bytes_per_second
  │   ├─ 限太狠(后台 IO 被限到很低)→ Compaction 拿不到 IO,跟不上
  │   │   → 调大 rate_bytes_per_sec(磁盘带宽的 50~70%)
  │   │   → 或调 IO 优先级,给 compaction 更多份额
  │   │   → 见 P5-18 "Rate Limiter" 节
  │   └─ 没限或够 → 继续查 subcompaction
  │
  ├─ 第 3 步:查大 compaction 是否拆成 subcompaction 并发
  │   ├─ max_subcompactions = 1(没拆)→ 大 compaction 单线程跑,慢
  │   │   → 调大 max_subcompactions(2~4)
  │   │   → 见 P4-13 "subcompaction" 节
  │   └─ 拆了 → 继续查数据特征
  │
  ├─ 第 4 步:查是不是 Universal 下空间放大触发频繁合并
  │   ├─ max_size_amplification_percent 太低 → 频繁 full compaction
  │   │   → 调高(200→500)
  │   └─ → 见 P4-15 "Universal" 节
  │
  ├─ 第 5 步:查是不是 Level 下层数太多,每个 key 重写多遍
  │   ├─ num_levels=7 但数据量小 → 压到 4~5 层,写放大↓
  │   └─ → 见 P4-14 "Level" 节
  │
  └─ 第 6 步:查是不是 value 太大撑爆 compaction
      ├─ value 普遍大(>1KB)→ 开 BlobDB,value 分离
      └─ → 见 P6-22 "BlobDB" 节
```

**处置**:调大 `max_background_jobs`/`max_background_compactions`/`max_subcompactions`、调大 `rate_bytes_per_sec`、调高 Universal 的 `max_size_amplification_percent`、压 Level 的 `num_levels`、开 BlobDB。详见 P4-13/P5-18/P4-15/P6-22。

### 7.5 排查清单的通用闭环

四类故障,排查方法本质一样——**先 GetProperty 定位故障类型,再沿决策树查根因,根因指向对应章节**。这个闭环和第四节"调优方法论"的闭环是同一个骨架,只是方向相反:

```
调优:测 → 看(Statistics/PerfContext)→ 拧(workload 对照表)
排查:看(GetProperty 看现象)→ 决策树 → 根因 → 处置
```

> **钉死这件事**:RocksDB 的所有线上问题,都能在前面 22 章里找到根因。本附录的排查清单只是"把现象快速路由到对应章节"的索引——真正的根因和处置,在 P1(写路径)/P3(读路径)/P4(Compaction)/P5(调控)/P6(横切)里。这也是为什么本书要先讲透机制再讲实践——不懂数据流,排查清单只是机械执行;懂数据流,排查清单是验证假设的利器。

---

## 八、技巧精解:db_bench 复现 + PerfContext 定位的两个经典模式

本附录是实践章,挑两个最常用的"现场套路"单独拆透。

### 技巧一:用 db_bench 复现线上故障的"控制变量法"

线上写延迟飙了,你怀疑是"Universal Compaction 的 size_ratio 设错了"。怎么验证?**不能直接在线上调**(影响生产),要在 db_bench 上复现:

```bash
# 假设 A:线上当前配置(size_ratio=1)
./db_bench --benchmarks=fillrandom,overwrite \
    --num=100000000 --value_size=1024 \
    --comp_style=1 \
    --compaction_options_universal="{size_ratio=1;}" \
    --use_existing_db=false --db=/tmp/A --histogram --duration=300

# 假设 B:改成 size_ratio=4(更激进合并)
./db_bench --benchmarks=fillrandom,overwrite \
    --num=100000000 --value_size=1024 \
    --comp_style=1 \
    --compaction_options_universal="{size_ratio=4;}" \
    --use_existing_db=false --db=/tmp/B --histogram --duration=300
```

**控制变量法**:除了 `size_ratio` 这一个 flag,其他全一样(num、value_size、threads、duration、compression、硬件)。跑出来对比 A 和 B 的:

- 写吞吐(ops/s):size_ratio 大是否吞吐高(少做大 full compaction)?
- 写放大:size_ratio 大是否写放大小(合并更激进,但每 key 重写次数可能少)?
- 读延迟(P99):size_ratio 大是否读延迟高(L0 更宽)?

> **不这样会怎样**:如果你在线上直接改 size_ratio,改完发现延迟反而飙了——回滚已经造成了生产事故。db_bench 复现,就是把"在生产上赌"变成"在测试上验证"。这一套"控制变量 + 对比"是 db_bench 最核心的用法,所有 RocksDB 调优决策都应该先在 db_bench 上复现。

### 技巧二:PerfContext 包 Get 定位一次慢读的耗时分布

线上某个 Get P99=20ms(异常),怎么定位?用 PerfContext 包住这次 Get:

```cpp
// (示意,非源码原文)
SetPerfLevel(PerfLevel::kEnableTimeAndCPUTimeExceptMutex);
get_perf_context()->Reset();
Status s = db->Get(read_options, "problematic_key", &value);
auto* p = get_perf_context();
// 打印 p 的关键字段
printf("block_read_count=%lu block_read_byte=%lu\n",
       p->block_read_count, p->block_read_byte);
printf("block_cache_hit_count=%lu bloom_filter_useful=%lu\n",
       p->block_cache_hit_count, p->bloom_filter_useful);
printf("user_key_comparison_count=%lu block_checksum_time=%lu\n",
       p->user_key_comparison_count, p->block_checksum_time);
printf("get_cpu_nanos=%lu get_read_bytes=%lu\n",
       p->get_cpu_nanos, p->get_read_bytes);
SetPerfLevel(PerfLevel::kDisable);
```

读这一组字段:

- `block_read_count=15, block_read_byte=600KB` → 这次 Get 实际从盘读了 15 个 block 600KB——读放大巨大(正常一次 Get 应该 cache 命中,读 0~2 个 block)。
- `block_cache_hit_count=2` → 只命中 2 个(可能 index/filter 命中了但 data 没命中),cache 不够或 working set 大。
- `bloom_filter_useful=3` → Bloom 救了 3 次(避免了 3 个 SST 的读),但显然不够,还有 15 次实际读。
- `user_key_comparison_count=5000` → 扫了 5000 个 key 比较,可能是某个 block 里 key 太密或 `max_sequential_skip_in_iterations` 没生效。
- `block_checksum_time=2ms` → checksum 占了 2ms,可能是压缩算法的 checksum 重(换算法?)。
- `get_cpu_nanos=18ms, get_read_bytes=2KB` → CPU 18ms 但只读出 2KB value,CPU 是瓶颈(不是 IO)。

这一套字段拆完,这次慢 Get 的根因(读放大?CPU?checksum?Bloom?)一目了然——这就是"为什么 PerfContext 是读性能优化的第一工具"。

---

## 九、附录小结

### 回扣主线

本附录是全书的实践收尾。它把前 22 章讲的机制(WriteGroup/InlineSkipList/三种 Compaction/Write Stall/Block Cache/Bloom/CF/BlobDB)落到**真刀真枪的操作**:

1. **工具链**(db_bench/ldb/sst_dump)是验证假设和现场诊断的武器。
2. **调优方法论**(按 workload 选旋钮)是把读写放大三角上的点挪到目标位置的指南——回扣 P0-01 的"读写放大三角",每个旋钮都是挪点的一个动作。
3. **可观测**(Statistics/PerfContext/GetProperty)是"看"这一步的核心,从聚合指标到单次操作细粒度,三层可观测。
4. **排查清单**(四类故障决策树)是线上出问题时的快速路由,把现象指向根因章节。

> **全书一句话主线**:LevelDB 把读写放大钉死在一个固定点,RocksDB 把这个点上每个写死的决策都做成可调旋钮。本附录告诉你**怎么用工具把旋钮拧到对的位置,以及拧错了怎么查**。

### 五个为什么

1. **为什么调优要先 db_bench 复现再上线?**——RocksDB 几百个 Options 互相影响,凭直觉调必错;db_bench 用控制变量法隔离验证,把"在生产上赌"变成"在测试上验证"。
2. **为什么 ldb 和 sst_dump 是现场诊断的两把刀?**——ldb 操作 DB 级(get/scan/manifest_dump/compact/repair),sst_dump 显微镜看单个 SST(scan/raw/show_properties/verify),两者联用能定位"一个 key 在哪个文件、什么版本"。
3. **为什么按 workload 选旋钮比凭经验调好?**——因为读写放大三角上的点有无数个,每个 workload(写多/读多/省盘/大 value)要优化的边不同,对照表给出每类 workload 的旋钮组合和"代价"(点往哪移)。
4. **为什么 cache_index_and_filter_blocks 是大 DB 必开?**——不开的话每个打开的 SST 的 index+filter 常驻内存不可淘汰,上万 SST 轻松几十 GB 内存;开了进 cache 可 LRU 淘汰,内存可控(代价是冷 SST 第一次查多一次 IO)。
5. **为什么排查要先 GetProperty 再走决策树?**——`levelstats`/`cfstats`/`actual-delayed-write-rate` 三个数能一眼定位 80% 的故障类型(写延迟/读放大/空间/Compaction),再沿决策树查到根因——盲目调参是排查的大忌。

### 想继续深入往哪钻

- **想熟练 db_bench**:读 `tools/db_bench_tool.cc` 第 115~273 行的所有 benchmark 和 flag 定义,亲手跑 fillrandom/overwrite/readrandom/mixgraph,对比 Level vs Universal。
- **想熟练 ldb**:跑 `./ldb --help` 看所有子命令(源码 `tools/ldb_tool.cc` 105~149 行有完整列表),把 Data Access 和 Admin 命令各试一遍。
- **想熟练 sst_dump**:对线上一个 SST 跑 `--command=scan`/`raw`/`show_properties`/`verify`,理解每个输出字段的含义。
- **想看可观测的源码**:`include/rocksdb/statistics.h`(Ticker/Histogram 定义)、`include/rocksdb/perf_context.h`(PerfContext 字段)、`include/rocksdb/perf_level.h`(PerfLevel 控制)。
- **想看集成案例**:TiKV 的 `engine_rocks`(RocksDB 侧 CF 调优)、MyRocks(MySQL 的 RocksDB 引擎,Meta 维护)。
- **想看排查的根因章节**:本附录的决策树每条都指向 P1(写路径)/P3(读路径)/P4(Compaction)/P5(调控)/P6(横切)的具体章节,回看对应章节能理解根因的机制层细节。

### 全书收束

至此,《RocksDB 设计与实现深入浅出》正文 23 章 + 附录 A(源码全景)+ 附录 B(工具链与调优实践)全部完成。希望你现在能在脑子里放映出:

- 一次 `Put` 怎么进 WriteBatch,被 WriteGroup 攒成一批,leader 写 WAL,各 batch 并发无锁写进 InlineSkipList MemTable,MemTable 满 Flush 成 L0 SST,Compaction 三种策略把数据逐层(或 Universal 合并)收敛——以及这每一步 LevelDB 写死什么,RocksDB 打开成什么旋钮。
- 一次 `Get` 怎么穿透 active memtable(Bloom 早退)→ immutable → L0(多文件)→ L1..Ln(Index 二分 + Bloom 早退 + data block),Block Cache 多档 pin 让热数据留在内存——以及读放大怎么算、怎么压。
- 出问题怎么查:`levelstats` + `cfstats` + `actual-delayed-write-rate` 一眼定位,沿决策树查到根因,db_bench 复现验证,PerfContext 看细粒度,对应章节找机制层解释。

> 这本书讲的不是"RocksDB 的 Options 怎么调",而是"它凭什么把 LevelDB 写死的每个决策都做成旋钮、C++ 源码里那些 WriteGroup、InlineSkipList、三种 Compaction、Write Stall、Column Family、BlobDB 到底在干什么"。读完,你该能拿起 db_bench 和 ldb,在自己的 workload 上,把读写放大三角上的点,精确挪到你想要的位置——这就是 RocksDB "把每个旋钮都交给你"的真正含义。
