# 附录 A · InnoDB 源码全景路线图

> 这份附录是给"读完正文二十三章、想自己下到源码里走一遍"的读者一张**导航图**。它不重复讲机制(那是正文二十三章干的事),只回答三件事:**读哪一章 → 看哪部分源码**、**每个模块干什么 / 关键文件在哪 / 从哪个函数进**、**一条 UPDATE 该按什么顺序顺着源码读下去**。
>
> 全书源码以 `mysql/mysql-server @ 845d525 (9.7.0 LTS)` 为准,InnoDB 源码在 [`storage/innobase/`](../mysql-server/storage/innobase/),server 层在 [`sql/`](../mysql-server/sql/)。本地相对路径都可点击跳转。
>
> 一句话总览:**server 层(`sql/`)→ handler 桥(`storage/innobase/handler/ha_innodb.cc`)→ InnoDB 引擎(`storage/innobase/{btr,buf,lock,log,trx,mtr,ibuf,read,row,page,dict,ddl,lob,ha,sync,...}`)。**

---

## 一、总览:三层调用关系

一条 `UPDATE` 从用户连接进来,到最终落盘,要穿过**三层**:

```
┌──────────────────────────────────────────────────────────────────────┐
│  第 1 层:server 层 (sql/)                                            │
│  连接 → 解析 (sql_parse) → 预处理 → 优化 (sql_optimizer/sql_executor) │
│  → 执行 (sql_update/sql_insert/sql_delete/sql_select)                │
│  ↓ 通过 handler 接口 (handlerton / class handler)                     │
├──────────────────────────────────────────────────────────────────────┤
│  第 2 层:handler 桥 (storage/innobase/handler/)                       │
│  ha_innodb.cc —— 把 server 层的虚函数调用翻译成 InnoDB 内部调用       │
│  ha_innobase::write_row / update_row / delete_row / index_read ...    │
│  handler0alter.cc —— DDL 入口 (in-place / online / instant)           │
│  i_s.cc / p_s.cc —— information_schema / performance_schema 接口      │
│  ↓                                                                     │
├──────────────────────────────────────────────────────────────────────┤
│  第 3 层:InnoDB 引擎 (storage/innobase/)                              │
│                                                                        │
│  ┌─ row 行操作  row0upd/row0ins/row0sel/row0mysql —— 旅行开始         │
│  │     │                                                              │
│  │     ├─ dict 数据字典 dict0dict/dict0mem/dict0dd —— 表/索引元信息    │
│  │     │                                                              │
│  │     ├─ btr B+树  btr0btr/btr0cur/btr0sea —— 找页/定位记录/AHI      │
│  │     │     │                                                        │
│  │     │     ├─ page 页结构 page0page/page0cur —— 页内记录操作         │
│  │     │     ├─ rem 记录格式 rem0rec/rem0cmp                           │
│  │     │     └─ lob 大字段 lob0lob/lob0ins —— off-page 行溢出          │
│  │     │                                                              │
│  │     ├─ buf buffer pool  buf0buf/buf0lru/buf0flu/buf0rea —— 页缓存  │
│  │     │     └─ buf0dblwr —— doublewrite buffer 防页撕裂              │
│  │     │                                                              │
│  │     ├─ lock 锁  lock0lock/lock0wait —— 行锁/间隙锁/临键锁/死锁      │
│  │     │                                                              │
│  │     ├─ trx 事务  trx0trx/trx0undo/trx0rec/trx0roll/trx0sys         │
│  │     │     ├─ undo log 写 undo (回滚 + MVCC 版本链)                  │
│  │     │     └─ trx0purge —— purge 清理旧版本                          │
│  │     │                                                              │
│  │     ├─ mtr mini-transaction  mtr0mtr/mtr0log —— redo 的生成单位    │
│  │     │                                                              │
│  │     ├─ log redo (WAL)  log0write/log0buf/log0files_*/log0chkp      │
│  │     │     └─ log0recv —— crash recovery 重放                       │
│  │     │                                                              │
│  │     ├─ read MVCC  read0read —— ReadView 可见性判断                 │
│  │     │                                                              │
│  │     ├─ ibuf change buffer  ibuf0ibuf —— 二级索引写合并             │
│  │     │                                                              │
│  │     └─ fil/fsp 文件空间 fil0fil/fsp0fsp —— 表空间/段/区/页         │
│  │                                                                     │
│  └─ sync 同步原语  sync0rw/sync0sync —— rw-latch/mutex                │
│     os OS 抽象  os0file/os0event —— 文件 IO/事件/线程                  │
│     srv 启动  srv0start/srv0srv —— innobase 启动与后台线程             │
└──────────────────────────────────────────────────────────────────────┘
```

理解这张图,你就理解了 InnoDB 源码的**物理布局**:**`handler/ha_innodb.cc` 是唯一的门**,server 层只认 handler 接口,InnoDB 所有内部模块都藏在它后面。这条边界也是 P6-20(一条 SQL 的完整旅程)要讲的核心。

---

## 二、按篇/章的源码地图表

下面这张表把全书 23 章(P0~P7)和源码目录/文件一一对应。每行给:**章号 → 源码目录/文件 → 核心函数/结构体 → 一句话作用**。函数名都经源码核实(见末节"核实方式")。

### 第 0 篇 · 开篇

| 章 | 标题 | 源码位置 | 核心函数 / 结构体 | 作用 |
|---|---|---|---|---|
| P0-01 | 第一性原理:为什么需要 InnoDB | [`storage/innobase/`](../mysql-server/storage/innobase/) (总览) + [`sql/handler.h`](../mysql-server/sql/handler.h) | `class handler` / `handlerton` | 可插拔存储引擎架构的接口骨架;InnoDB 注册到 server 层的入口 |

### 第 1 篇 · 地基:表就是 B+树(聚簇索引 / 二级索引 / 页)

| 章 | 标题 | 源码位置 | 核心函数 / 结构体 | 作用 |
|---|---|---|---|---|
| P1-02 | 聚簇索引:表的数据就在主键 B+树里 | [`btr/btr0btr.cc`](../mysql-server/storage/innobase/btr/btr0btr.cc) | `btr_root_get` / `btr_root_block_get` / `btr_page_create` / `btr_page_alloc_low` | B+树的根、页分配、节点结构;索引组织表(IOT)的根 |
| P1-02 | (字典侧) | [`dict/dict0dict.cc`](../mysql-server/storage/innobase/dict/dict0dict.cc) / [`dict0mem.cc`](../mysql-server/storage/innobase/dict/dict0mem.cc) | `dict_index_t` / `dict_table_t` | 表和索引的内存表示(聚簇/二级标志在这) |
| P1-03 | 二级索引与回表 | [`btr/btr0cur.cc`](../mysql-server/storage/innobase/btr/btr0cur.cc) / [`row/row0sel.cc`](../mysql-server/storage/innobase/row/row0sel.cc) | `btr_cur_search_to_nth_level` / `row_sel_store_row_id_to_prebuilt` | 二级索引树定位 + 回聚簇索引取行;覆盖索引在此判断 |
| P1-04 | B+树页与记录:16KB 的页怎么组织 | [`page/page0page.cc`](../mysql-server/storage/innobase/page/page0page.cc) / [`page0cur.cc`](../mysql-server/storage/innobase/page/page0cur.cc) | `page_dir_get_nth_slot` / `page_dir_find_owner_slot` / `page_cur_*` | 页结构(File/Page Header + 记录 + Page Directory)、页内游标 |
| P1-04 | (记录格式) | [`rem/rem0rec.cc`](../mysql-server/storage/innobase/rem/rem0rec.cc) / [`rem0cmp.cc`](../mysql-server/storage/innobase/rem/rem0cmp.cc) | `rec_get_offsets` / `cmp_data_data` | Compact/Redundant/Dynamic 记录头解析、记录比较 |
| P1-04 | (大字段 off-page) | [`lob/lob0lob.cc`](../mysql-server/storage/innobase/lob/lob0lob.cc) / [`lob0ins.cc`](../mysql-server/storage/innobase/lob/lob0ins.cc) | `lob::insert` / `lob::z_insert` | 行溢出大字段存到 lob 页(9.x 还有 zlib 压缩 zlob) |

### 第 2 篇 · 内存:Buffer Pool

| 章 | 标题 | 源码位置 | 核心函数 / 结构体 | 作用 |
|---|---|---|---|---|
| P2-05 | Buffer Pool:页面缓存 | [`buf/buf0buf.cc`](../mysql-server/storage/innobase/buf/buf0buf.cc) | `buf_pool_init` / `buf_block_init` / `buf_page_get_gen` / `buf_LRU_get_free_block` | buffer pool 初始化、页获取(free/LRU/flush 三链表) |
| P2-05 | (预读) | [`buf/buf0rea.cc`](../mysql-server/storage/innobase/buf/buf0rea.cc) | `buf_read_page` / `buf_read_page_low` | 缓存未命中时的磁盘预读 |
| P2-06 | 改进的 LRU:midpoint insertion | [`buf/buf0lru.cc`](../mysql-server/storage/innobase/buf/buf0lru.cc) | `buf_LRU_*` 系列 | midpoint insertion(young/old 分区)、LRU 淘汰 |
| P2-06 | (刷脏页) | [`buf/buf0flu.cc`](../mysql-server/storage/innobase/buf/buf0flu.cc) | `buf_flush_*` 系列 | 脏页异步刷盘(flush list / LRU 两条路径) |
| P2-07 | 自适应哈希索引(AHI)与 change buffer | [`btr/btr0sea.cc`](../mysql-server/storage/innobase/btr/btr0sea.cc) / [`ha/ha0ha.cc`](../mysql-server/storage/innobase/ha/ha0ha.cc) | `btr_search_sys_create` / `ha_search_and_update_if_found_func` | AHI 热点路径哈希索引 |
| P2-07 | (change buffer) | [`ibuf/ibuf0ibuf.cc`](../mysql-server/storage/innobase/ibuf/ibuf0ibuf.cc) | `ibuf_insert` / `ibuf_merge` / `ibuf_merge_pages` | 二级索引写先缓存、后台合并 |

### 第 3 篇 · 事务·崩溃恢复:WAL(redo / undo / 2PC)—— InnoDB 最核心

| 章 | 标题 | 源码位置 | 核心函数 / 结构体 | 作用 |
|---|---|---|---|---|
| P3-08 | WAL 与 redo log | [`log/log0write.cc`](../mysql-server/storage/innobase/log/log0write.cc) | `log_write_buffer` / `log_writer_*` 系列 / `log_buffer_ready_for_write_lsn` | **8.0.30 重构后的 redo 写入**(多线程:writer / write_notifier / flusher / flush_notifier) |
| P3-08 | (redo buffer / files) | [`log/log0buf.cc`](../mysql-server/storage/innobase/log/log0buf.cc) / [`log0files_*.cc`](../mysql-server/storage/innobase/log/) (capacity/dict/finder/governor/io) | `log_t` 结构体、`log_files_*` 系列 | log buffer 管理、redo 文件容量/治理(老 `log0log.cc` 单文件模型已拆) |
| P3-08 | (checkpoint) | [`log/log0chkp.cc`](../mysql-server/storage/innobase/log/log0chkp.cc) | checkpoint 推进 | redo 回收的起点 |
| P3-08 | (8.0.30 前兼容) | [`log/log0pre_8_0_30.cc`](../mysql-server/storage/innobase/log/log0pre_8_0_30.cc) | —— | 兼容 8.0.30 之前 redo 格式(读老版本),**老资料讲的 log0log 单文件模型大片过时** |
| P3-09 | mini-transaction(mtr):redo 的生成单位 | [`mtr/mtr0mtr.cc`](../mysql-server/storage/innobase/mtr/mtr0mtr.cc) / [`mtr0log.cc`](../mysql-server/storage/innobase/mtr/mtr0log.cc) | `mtr_t` 类 / `mtr_memo_slot_t` / `mtr_start` / `mtr_commit` | 一组页修改打包成原子单位,提交时把 redo 写进 log buffer |
| P3-10 | undo log:回滚 + MVCC 版本链 | [`trx/trx0undo.cc`](../mysql-server/storage/innobase/trx/trx0undo.cc) / [`trx0rec.cc`](../mysql-server/storage/innobase/trx/trx0rec.cc) | `trx_undo_insert_recs` / `trx_undo_update_rec` / `trx_undo_get_prev_rec` | undo(逻辑日志)记录怎么改回去;undo 段 / undo tablespace |
| P3-10 | (回滚段 / undo 系统) | [`trx/trx0rseg.cc`](../mysql-server/storage/innobase/trx/trx0rseg.cc) / [`trx0sys.cc`](../mysql-server/storage/innobase/trx/trx0sys.cc) | `trx_assign_rseg_durable` / `trx_assign_rseg_temp` | 回滚段分配、事务系统全局状态 |
| P3-11 | 两阶段提交(2PC):redo 与 binlog 一致 | [`trx/trx0trx.cc`](../mysql-server/storage/innobase/trx/trx0trx.cc) / [`handler/ha_innodb.cc`](../mysql-server/storage/innobase/handler/ha_innodb.cc) | `trx_commit_low` / `trx_commit` / `trx_commit_in_memory` / `trx_flush_logs` / `innobase_commit` (L1355) / `innobase_commit_by_xid` (L1548) | 事务提交路径:prepare → 写 binlog → commit;`ha_innodb.cc` 把 hton->commit 接到 server |
| P3-12 | crash recovery 与 doublewrite | [`log/log0recv.cc`](../mysql-server/storage/innobase/log/log0recv.cc) | `recv_recovery_from_checkpoint_start` (L3766) / `recv_recovery_from_checkpoint_finish` (L3950) / `recv_apply_log_rec` (L1118) / `recv_apply_hashed_log_recs` (L1173) | 崩溃后扫 redo checkpoint → 重放 redo → 回滚未提交事务 |
| P3-12 | (doublewrite buffer) | [`buf/buf0dblwr.cc`](../mysql-server/storage/innobase/buf/buf0dblwr.cc) | `dblwr` 命名空间内的 `File` 等 | 先顺序写一份页到双写区、再随机写数据文件,防 partial page write(页撕裂) |

### 第 4 篇 · 并发控制:MVCC

| 章 | 标题 | 源码位置 | 核心函数 / 结构体 | 作用 |
|---|---|---|---|---|
| P4-13 | MVCC 全貌:多版本并发控制 | [`read/read0read.cc`](../mysql-server/storage/innobase/read/read0read.cc) | `class ReadView` / `class MVCC` | 一行多版本的基础设施 |
| P4-14 | Read View 与可见性判断 | [`read/read0read.cc`](../mysql-server/storage/innobase/read/read0read.cc) | `MVCC::view_open` (L499) / `MVCC::get_view` (L476) / `ReadView::prepare` (L446) / `ReadView::changes_visible` | 建/关 read view、可见性算法(min/max/活跃列表三段判断) |
| P4-14 | (顺着 undo 找版本) | [`row/row0vers.cc`](../mysql-server/storage/innobase/row/row0vers.cc) | `row_vers_build_for_consistent_read` 等 | 沿 undo 版本链向前找可见版本 |
| P4-15 | undo 版本链与 purge | [`trx/trx0purge.cc`](../mysql-server/storage/innobase/trx/trx0purge.cc) / [`row/row0purge.cc`](../mysql-server/storage/innobase/row/row0purge.cc) | `trx_purge_run` (L2555) / `trx_purge_add_update_undo_to_history` (L352) / `row_purge_node_create` | 后台 purge 清"没人再需要"的旧版本(对照 TiKV GC) |

### 第 5 篇 · 并发控制:锁

| 章 | 标题 | 源码位置 | 核心函数 / 结构体 | 作用 |
|---|---|---|---|---|
| P5-16 | 锁全貌:行锁 + 表锁,两阶段锁协议 | [`lock/lock0lock.cc`](../mysql-server/storage/innobase/lock/lock0lock.cc) | `lock_rec_*` 系列 / `lock_table_*` / `lock_t` 结构 | 记录锁 S/X、表锁 IX/IS;两阶段(随时加、commit 释放) |
| P5-17 | 间隙锁与临键锁:解决幻读 | [`lock/lock0lock.cc`](../mysql-server/storage/innobase/lock/lock0lock.cc) | `lock_rec_reset_and_inherit_gap_locks` (L3086) / gap lock 标志位 | 锁"不存在的间隙"、next-key = 记录锁 + 前间隙 |
| P5-18 | 死锁检测与锁等待 | [`lock/lock0lock.cc`](../mysql-server/storage/innobase/lock/lock0lock.cc) / [`lock0wait.cc`](../mysql-server/storage/innobase/lock/lock0wait.cc) | `lock_wait_request_check_for_cycles` / `lock_deadlock_found` / `lock_wait_table_reserve_slot` | wait-for graph 检测环 = 死锁、回滚 undo 量小的事务 |
| P5-19 | 隔离级别:RR/RC/RU/Serializable | [`trx/trx0trx.cc`](../mysql-server/storage/innobase/trx/trx0trx.cc) + [`read0read.cc`](../mysql-server/storage/innobase/read/read0read.cc) + [`lock0lock.cc`](../mysql-server/storage/innobase/lock/lock0lock.cc) | read view 建立时机(事务级 vs 语句级)+ 间隙锁开关 | RR 事务级 view + 间隙锁;RC 语句级 view 无间隙锁 |

### 第 6 篇 · 实践与进阶

| 章 | 标题 | 源码位置 | 核心函数 / 结构体 | 作用 |
|---|---|---|---|---|
| P6-20 | 一条 SQL 的完整旅程 | [`sql/sql_parse.cc`](../mysql-server/sql/sql_parse.cc) → [`sql_optimizer.cc`](../mysql-server/sql/sql_optimizer.cc) → [`sql_executor.cc`](../mysql-server/sql/sql_executor.cc) → [`sql_update.cc`](../mysql-server/sql/sql_update.cc) / [`sql_insert.cc`](../mysql-server/sql/sql_insert.cc) → [`handler/ha_innodb.cc`](../mysql-server/storage/innobase/handler/ha_innodb.cc) | server 层 4 段 + `ha_innobase::write_row` (L9249) / `update_row` (L10003) / `delete_row` (L10161) / `index_read` (L10423) / `index_first` (L10893) / `rnd_next` (L11072) | server 层 SQL 旅程 + handler 接口进 InnoDB |
| P6-21 | 索引调优与 explain | [`handler/ha_innodb.cc`](../mysql-server/storage/innobase/handler/ha_innodb.cc) (records_in_range) + server 优化器 | explain 输出对应 | 索引选择、回表/覆盖判断 |
| P6-22 | 在线 DDL:加字段/索引怎么不锁表 | [`handler/handler0alter.cc`](../mysql-server/storage/innobase/handler/handler0alter.cc) / [`ddl/`](../mysql-server/storage/innobase/ddl/) (ddl0ddl/ddl0builder/ddl0loader/ddl0bulk) / [`dict/dict0dd.cc`](../mysql-server/storage/innobase/dict/dict0dd.cc) | `innobase_support_instant` (L829) / `ha_innobase::prepare_inplace_alter_table` / `ha_innobase::inplace_alter_table` / `ha_innobase::commit_inplace_alter_table` / `dd_open_table` (dict0dd L428) | in-place / online rebuild / **instant DDL(8.0+ 秒级,只改数据字典)** |

### 第 7 篇 · 收束

| 章 | 标题 | 源码位置 | 核心函数 / 结构体 | 作用 |
|---|---|---|---|---|
| P7-23 | 全书收束:InnoDB 与 OLTP 引擎的演进 | 全书回顾 + 对照表 | —— | B+树 vs LSM、单机 vs 分布式事务的总对照 |

---

## 三、各模块职责速查

InnoDB 把 `storage/innobase/` 切成三十来个子目录,每个目录一个职责。下面是**按字母序的速查表**,给每个模块一句话定位 + 关键文件 + 推荐阅读入口(从哪个函数进最自然)。

| 模块 | 目录 | 关键文件 | 职责 | 推荐入口函数 |
|---|---|---|---|---|
| **btr** B+树 | [`btr/`](../mysql-server/storage/innobase/btr/) | `btr0btr.cc` / `btr0cur.cc` / `btr0sea.cc` / `btr0pcur.cc` / `btr0load.cc` | B+树的根/页分配/分裂/合并、游标定位、AHI 入口 | `btr_cur_search_to_nth_level`(从根定位到叶) |
| **buf** buffer pool | [`buf/`](../mysql-server/storage/innobase/buf/) | `buf0buf.cc` / `buf0lru.cc` / `buf0flu.cc` / `buf0rea.cc` / `buf0dblwr.cc` | 页缓存池、改进 LRU、刷脏页、预读、doublewrite | `buf_page_get_gen`(取一页) |
| **ddl** DDL | [`ddl/`](../mysql-server/storage/innobase/ddl/) | `ddl0ddl.cc` / `ddl0builder.cc` / `ddl0loader.cc` / `ddl0bulk.cc` / `ddl0merge.cc` | 在线建表/建索引的并发 builder/loader(8.0 重构,从 handler0alter 调入) | `ddl::Builder` 类 |
| **dict** 数据字典 | [`dict/`](../mysql-server/storage/innobase/dict/) | `dict0dict.cc` / `dict0mem.cc` / `dict0dd.cc` / `dict0load.cc` / `dict0crea.cc` | 表/索引/列的内存表示 + **8.0 新 DD**(transactional data dictionary,存 InnoDB 表里,替 `.frm`) | `dict_table_get_low` / `dd_open_table` |
| **fil** 文件 | [`fil/`](../mysql-server/storage/innobase/fil/) | `fil0fil.cc` | 文件/表空间的逻辑 IO 抽象层(在 os0file 之上、buf0buf 之下) | `fil_io` |
| **fsp** 表空间 | [`fsp/`](../mysql-server/storage/innobase/fsp/) | `fsp0fsp.cc` / `fsp0file.cc` / `fsp0space.cc` | 表空间/段/区/页的分配(bitmap 管理) | `fseg_create` / `fsp_alloc_free_page` |
| **ha** AHI 哈希 | [`ha/`](../mysql-server/storage/innobase/ha/) | `ha0ha.cc` / `hash0hash.cc` | 自适应哈希索引(AHI)用的哈希表原语 | `ha_search_and_update_if_found_func` |
| **handler** 桥 | [`handler/`](../mysql-server/storage/innobase/handler/) | `ha_innodb.cc` / `handler0alter.cc` / `i_s.cc` / `p_s.cc` | **server 层 ↔ InnoDB 的唯一接口**;DDL、I_S/P_S 也在这 | `ha_innobase::write_row` / `update_row` |
| **ibuf** change buffer | [`ibuf/`](../mysql-server/storage/innobase/ibuf/) | `ibuf0ibuf.cc` | 二级索引写先缓存、后台合并(承 LevelDB 写缓冲思想) | `ibuf_insert` / `ibuf_merge` |
| **lob** 大字段 | [`lob/`](../mysql-server/storage/innobase/lob/) | `lob0lob.cc` / `lob0ins.cc` / `lob0del.cc` / `lob0update.cc` / `zlob0*.cc` | 行溢出大字段(BLOB/TEXT)off-page 存储;9.x 有 zlib 压缩 zlob | `lob::insert` |
| **lock** 锁 | [`lock/`](../mysql-server/storage/innobase/lock/) | `lock0lock.cc` / `lock0wait.cc` / `lock0iter.cc` / `lock0guards.cc` | 行锁/间隙锁/临键锁/表锁、锁等待、死锁检测 | `lock_rec_lock` |
| **log** redo (WAL) | [`log/`](../mysql-server/storage/innobase/log/) | `log0write.cc` / `log0buf.cc` / `log0chkp.cc` / `log0recv.cc` / `log0files_*.cc` / `log0pre_8_0_30.cc` | **redo 物理日志(WAL)** 写入/恢复;**8.0.30 大重构**(拆十几个文件,老 log0log 单文件过时) | `log_write_buffer` / `recv_recovery_from_checkpoint_start` |
| **mem** 内存 | [`mem/`](../mysql-server/storage/innobase/mem/) | `memory.cc` | 内存分配器封装(mem_heap_t 等) | `mem_heap_create` |
| **mtr** mini-transaction | [`mtr/`](../mysql-server/storage/innobase/mtr/) | `mtr0mtr.cc` / `mtr0log.cc` | redo 的生成单位:一组页修改打包原子提交 | `mtr_t::commit` |
| **os** OS 抽象 | [`os/`](../mysql-server/storage/innobase/os/) | `os0file.cc` / `os0event.cc` / `os0thread.cc` | 文件 IO、事件、线程的跨平台封装 | `os_file_read` / `os_file_write` |
| **page** 页 | [`page/`](../mysql-server/storage/innobase/page/) | `page0page.cc` / `page0cur.cc` / `page0zip.cc` | 页结构操作(Header/Directory/记录)、页内游标、压缩页 | `page_cur_search_with_match` |
| **read** MVCC | [`read/`](../mysql-server/storage/innobase/read/) | `read0read.cc` | **ReadView + 可见性判断**(MVCC 的核心) | `MVCC::view_open` / `ReadView::changes_visible` |
| **rem** 记录 | [`rem/`](../mysql-server/storage/innobase/rem/) | `rem0rec.cc` / `rem0cmp.cc` | 记录格式(Compact/Redundant/Dynamic)解析与比较 | `rec_get_offsets` |
| **row** 行操作 | [`row/`](../mysql-server/storage/innobase/row/) | `row0ins.cc` / `row0upd.cc` / `row0sel.cc` / `row0mysql.cc` / `row0purge.cc` / `row0vers.cc` / `row0undo.cc` / `row0umod.cc` / `row0uins.cc` | **行级 CRUD 的总入口**(从 ha_innodb 进来的第一站) | `row_insert_for_mysql` / `row_update_for_mysql` / `row_search_for_mysql` |
| **srv** 启动 | [`srv/`](../mysql-server/storage/innobase/srv/) | `srv0start.cc` / `srv0srv.cc` / `srv0conc.cc` | innobase 启动、后台线程(purge/flush/cleaner/master) | `srv_start` |
| **sync** 同步 | [`sync/`](../mysql-server/storage/innobase/sync/) | `sync0rw.cc` / `sync0sync.cc` / `sync0arr.cc` / `sync0debug.cc` | rw-latch / mutex / 信号量(对照 Linux 同步原语那本) | `rw_lock_s_lock` / `mutex_enter` |
| **trx** 事务 | [`trx/`](../mysql-server/storage/innobase/trx/) | `trx0trx.cc` / `trx0undo.cc` / `trx0rec.cc` / `trx0roll.cc` / `trx0rseg.cc` / `trx0sys.cc` / `trx0purge.cc` | **事务对象 + undo + 提交 + purge**(事务与并发这一面的中枢) | `trx_commit_low` / `trx_allocate_for_mysql` |
| **ut** 工具 | [`ut/`](../mysql-server/storage/innobase/ut/) | (utility) | 各类小工具(链表/位图/数学) | —— |

> **两个不在子目录但极关键的文件**:[`include/`](../mysql-server/storage/innobase/include/) 下放所有头文件(`*.h` / `*.ic`),是各模块的"接口契约";[`handler/ha_innodb.cc`](../mysql-server/storage/innobase/handler/ha_innodb.cc) 是全书最常被引用的单文件(两万多行)。

---

## 四、推荐源码阅读顺序:顺着一条 UPDATE 的旅程

最自然的读法不是按目录字母序,而是**顺着一条 `UPDATE` 的旅程**读——这正是本书主线的源码版。建议按下面七段顺序,每段配主章节,先读懂主路径函数、再横向扩展。

### 旅程 0:启动 — srv0start

读 InnoDB 源码,先搞清**它怎么起来的**。从 [`srv/srv0start.cc`](../mysql-server/storage/innobase/srv/srv0start.cc) 的 `srv_start` 进去,看它怎么依次初始化 buf pool / log / lock sys / trx sys / dict,然后起后台线程。这一步帮你建立"全局对象都在哪"的地图。

### 旅程 1:进 engine — ha_innodb → row0*

```
ha_innobase::update_row (ha_innodb.cc:10003)
   ↓
row_update_for_mysql (row/row0mysql.cc)
   ↓
row_upd_step (row/row0upd.cc)
```

server 层调 `handler->update_row`,InnoDB 这边的实现把它翻译成 `row0mysql` 的入口。这一段是 P6-20 的核心。**读完这一段,你就跨过了 server↔engine 的边界**。

### 旅程 2:找页 — row → btr → page

```
row_upd → btr_cur_search_to_nth_level (btr/btr0cur.cc)
            ↓ 沿 B+树从根到叶定位
         buf_page_get_gen (buf/buf0buf.cc)   ← 页没在 buffer pool 就 buf_read_page 读盘
            ↓
         page_cur_search_with_match (page/page0cur.cc)   ← 页内二分找记录
```

这一段对应 P1-02 / P1-04 / P2-05。读懂 `btr_cur_search_to_nth_level` 是理解 B+树的钥匙。

### 旅程 3:加锁 — lock0lock

```
row_upd → lock_rec_lock (lock/lock0lock.cc)
            ↓ 行锁 / 间隙锁 / 临键锁
         lock_wait_table_reserve_slot (lock0wait.cc)   ← 等不到就挂起
            ↓
         lock_wait_request_check_for_cycles   ← 顺带检测死锁
```

这一段对应 P5-16 / P5-17 / P5-18。重点看 `lock_rec_lock` 里**怎么根据隔离级别和 SQL 决定锁哪种**(记录/间隙/临键)。

### 旅程 4:写 undo — trx0undo

```
row_upd → trx_undo_report_row_update (trx/trx0undo.cc)
            ↓
         trx_undo_update_rec / trx_undo_get_prev_rec (trx0rec.cc)
```

UPDATE 改之前先把旧值写 undo——这步既是**为了能回滚**,也是**为了 MVCC 留旧版本**(P3-10 / P4-13)。两条目的同一个 undo log。

### 旅程 5:改页 + 生成 redo — page0page + mtr0mtr

```
row_upd → row_upd_rec_in_place (row/row0upd.cc:469)   ← 就地改记录
            ↓
         mtr_start (mtr/mtr0mtr.cc)   ← 开 mtr
            ↓  改页的过程中 mtr 记 redo(物理日志)
         mtr_t::commit   ← 提交 mtr,把 redo 写进 log buffer
```

**这一段是 InnoDB 最精妙的设计之一**:对页的每一次修改都裹在一个 mtr 里,mtr 提交时把"哪页哪偏移改成什么"的物理 redo 写进 log buffer。对应 P3-09。

### 旅程 6:写 redo + 提交 — log0write + trx0trx

```
mtr_t::commit → log_write_buffer (log/log0write.cc:698)
                  ↓ 8.0.30 重构后的多线程写入
               log_writer / log_write_notifier / log_flusher / log_flush_notifier
                  ↓
trx_commit_low (trx/trx0trx.cc:2137)   ← 两阶段提交
   ↓
innobase_commit (ha_innodb.cc:1355)   ← server 层的 hton->commit 入口
```

这一段对应 P3-08 / P3-11,是 InnoDB 的事务心脏。重点理解**8.0.30 重构后 redo 写入是四个专用线程流水线**(writer / write_notifier / flusher / flush_notifier),老资料讲的单线程模型过时。

### 旅程 7:刷盘 + 恢复 — buf0flu + buf0dblwr + log0recv

```
脏页刷盘(异步):
   buf_flush_* (buf/buf0flu.cc)
      → buf0dblwr.cc   (先写双写区防页撕裂)
      → os_file_write (os/os0file.cc)

崩溃重启:
   recv_recovery_from_checkpoint_start (log/log0recv.cc:3766)
      → recv_apply_hashed_log_recs (log0recv.cc:1173)   ← 按 page 聚合后重放 redo
      → recv_recovery_from_checkpoint_finish (log0recv.cc:3950)
      → 顺 undo 回滚未提交事务 (trx0roll)
```

这一段对应 P3-12。读完这步,**一条 UPDATE 从生到死、到 crash 复活的完整闭环就合上了**。

> **横向扩展建议**:主路径读顺后,再按兴趣横扫:喜欢并发就读 `sync/` + `lock/`,喜欢存储引擎就读 `btr/` + `page/` + `fsp/`,喜欢崩溃恢复就深挖 `log/` 整个目录。各模块的"推荐入口函数"见第三节速查表。

---

## 五、架构演进标注:哪些是新的、哪些过时了

InnoDB 处于持续演进,源码里有几处**新老并存**,读的时候务必分辨:

| 演进点 | 现状(9.7.0 LTS) | 老版本 / 过时资料 | 涉及文件 |
|---|---|---|---|
| **redo log 8.0.30 大重构** | 拆成 `log0write.cc` / `log0buf.cc` / `log0files_*.cc` / `log0chkp.cc` 等十几个文件,多线程并发写 | 老资料讲 `log0log.cc` 单文件模型,大片过时;`log0pre_8_0_30.cc` 是读老格式的兼容代码 | [`log/`](../mysql-server/storage/innobase/log/) |
| **数据字典 8.0 重构** | 新的 transactional data dictionary(DD),表/索引元信息存 InnoDB 表里,`dict0dd.cc` 是桥梁 | 老的 `.frm` 文件**已废弃**;`dict0upgrade.cc` 是升级用 | [`dict/`](../mysql-server/storage/innobase/dict/) |
| **instant DDL(8.0+)** | 加列等只改数据字典、**秒级完成**,`innobase_support_instant` (handler0alter.cc:829) 判断 | 老"DDL 必重建表"过时 | [`handler0alter.cc`](../mysql-server/storage/innobase/handler/handler0alter.cc) |
| **DDL 并行 builder/loader(8.0+)** | `ddl/` 目录独立的 `Builder` / `Loader` / `Bulk` 体系,并发建索引/建表 | 老的串行 inplace alter | [`ddl/`](../mysql-server/storage/innobase/ddl/) |
| **clone 插件(8.0+)** | 原生克隆备份,见 [`clone/`](../mysql-server/storage/innobase/clone/) | 替代部分外部工具(xtrabackup 等) | [`clone/`](../mysql-server/storage/innobase/clone/) |
| **9.x 新特性** | 向量类型 `VECTOR`、JavaScript 存储过程等 | 8.x 没有 | 散布在 `dict/` / `sql/` |

> **一句话提醒**:凡是 2019 年前(redo 重构前)、或讲 `.frm` / 5.7 redo / 无 instant DDL 的博客,**都不能当唯一依据**,务必对照 9.7.0 源码核。本书所有引用都以 9.7.0 LTS 为准。

---

## 六、源码核实方式与说明

- **目录结构**:逐个 `ls` 了 [`storage/innobase/`](../mysql-server/storage/innobase/) 下全部子目录(btr/buf/lock/log/trx/mtr/ibuf/read/row/page/dict/ddl/lob/ha/sync/os/mem/fil/fsp/rem/srv/handler/include 等)和 [`sql/`](../mysql-server/sql/) 关键文件,确认本附录引用的每个文件名都真实存在。
- **函数名 / 行号**:用 `grep -nE "^(返回类型).*函数名"` 在对应文件里核实了 `btr_root_get` / `btr_page_create` / `buf_pool_init` / `buf_page_get_gen` / `log_write_buffer` / `trx_commit_low` / `trx_commit` / `MVCC::view_open` / `ReadView::prepare` / `recv_recovery_from_checkpoint_start` / `innobase_commit` / `ha_innobase::write_row/update_row/delete_row/index_read` / `ibuf_insert` / `btr_search_sys_create` / `ha_search_and_update_if_found_func` / `page_dir_*` 等关键符号,行号均为 9.7.0 LTS 真实行号。
- **需说明处**:
  1. `buf/buf0dblwr.cc` 内部用了 `dblwr` 命名空间,旧的全局函数名(如 `buf_dblwr_write`)在新版里已重构进命名空间,只标目录/文件、不深标内部函数。
  2. `log0write.cc` 顶部有大量 doxygen 文档块描述四个专用线程(writer/write_notifier/flusher/flush_notifier),这是 8.0.30 重构的产物,函数本身分散在文件中,建议结合文档块读。
  3. `handler0alter.cc` 的 instant DDL 判断在 L829(`innobase_support_instant`),但真正的 inplace/rebuild 分发逻辑在 `ha_innobase::prepare_inplace_alter_table` / `inplace_alter_table` / `commit_inplace_alter_table` 这一组,跨 `ha_innodb.cc` 与 `handler0alter.cc` 两文件。
  4. server 层 `sql/` 只在 P6-20 旅程略带(本书聚焦 InnoDB,server 层 SQL 旅程承《PostgreSQL 数据库内核》那本),本附录对 `sql/` 只点到 `sql_parse.cc` / `sql_optimizer.cc` / `sql_executor.cc` / `sql_update.cc` 等关键文件,不展开。

> 读完这份路线图,你应该能:**(1)** 知道读完哪一章该去翻哪个目录;**(2)** 拿到一个模块名能立刻说出它干啥、关键文件在哪、从哪个函数进;**(3)** 顺着一条 UPDATE 的旅程,从 `ha_innodb.cc` 一路读到 `log0recv.cc`,把 InnoDB 的源码走成一个闭环。剩下的,就是打开源码、按图索骥。
