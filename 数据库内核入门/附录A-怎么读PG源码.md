# 附录 A · 怎么读 PostgreSQL 源码

> 全书正文带你跟着一条 SQL 走完了它的一生,引用了 PG 17.0 里几十处源码。但那些引用只是**路标**——你迟早会想自己钻进去:这条 SQL 真的走到这里了吗?优化器究竟在哪一行挑出了那条路径?Buffer Pool 命中时到底跳过了哪几步?
>
> 这篇附录,是给你一本"自驾手册":怎么在 PG 这片 6000+ 个 `.c/.h` 文件的森林里不迷路、怎么搭工具、怎么读、从哪里读起。

---

## 一、为什么单独写一篇"怎么读源码"

数据库内核源码的体量,是劝退新人的第一道墙。PG 17.0 仅 `src/` 下的 `.c` 和 `.h` 文件就有**上千个**,你拿到的常常是这样的体验:

- 想看"一句 SQL 怎么被解析",搜 `parse`,跳出来 30 个文件,不知道哪个是入口;
- 想跟"一次插入到底改了哪些页",从 `heap_insert` 进去,五次跳转后到了 `ReadBuffer`,再看不懂和谁的关系;
- 想看 Buffer Pool,打开 `bufmgr.c`——1900 多行,从哪行读起?

**这不是你的问题,是缺少一条路线的问题。**

本书正文已经给了你那条路线的**骨架**——"一条 SQL 的旅程":连接 → 解析 → 优化 → 执行 → 存储 → 事务 → 日志。这条骨架不只是讲故事的顺序,**它就是读源码的最佳顺序**:PG 的目录结构、函数调用链,几乎是为这条旅程量身铺设的。你只要知道"我现在在旅程的第几站、这一站住在哪个目录",就不会迷路。

所以这一篇做的事情很简单:把旅程的每一站,**精确映射到 PG 的源码目录和入口函数**,再给你三件工具(建索引、断点调试、grep 找符号)。带着这张地图回到正文,你会发现引用的行号不再是抽象坐标,而是能真正打开看的代码。

---

## 二、PG 源码目录结构(基于真实 `postgresql-17.0/src/`)

下面这张表,是 `ls` 出来的真实结构(不是凭记忆),左侧是 PG 17.0 `src/` 下的顶层目录,右侧是它装的东西:

| 目录 | 装什么 | 你什么时候会进来看 |
|---|---|---|
| `src/backend/` | **数据库服务端(后端)的全部实现**。整本书 90% 的源码引用都在这里 | 几乎每一章 |
| `src/include/` | **头文件**。所有结构体(`BufferDesc`、`HeapTupleHeaderData`、`PageHeaderData`...)、宏、函数原型都在这里 | 想看"这个对象长什么样"时 |
| `src/common/` | 前后端**共用**的代码(日志、加密、协议解析)。不是核心,但偶尔会被引用 | 少量 |
| `src/port/` | 平台兼容层(把不同 OS 的差异抹平) | 几乎不进 |
| `src/interfaces/` | 客户端库(libpq 等)——客户端怎么连数据库 | P1 第 2 章 连接 |
| `src/bin/` | **命令行工具的源码**:`psql`、`pg_ctl`、`initdb`、`pg_waldump`、`pg_dump`... | 附录 B 的工具 |
| `src/fe_utils/` | 上述工具共用的前端工具函数 | 少量 |
| `src/pl/` | 过程语言(plpgsql 等) | 几乎不进 |
| `src/test/` | 回归测试、隔离测试、TAP 测试——**想看某个机制"应该怎么表现",读它的测试用例最直接** | 验证理解时 |

### `src/backend/` 内部:按子系统分目录

`src/backend/` 下进一步按子系统拆成 **20+ 个子目录**(用 `ls` 真实确认),下面这张表是其中**与本书直接相关**的部分,按旅程顺序排列:

| 子系统目录 | 装什么 | 对应本书 |
|---|---|---|
| `src/backend/postmaster/` | 进程模型:`postmaster`(监听 fork)、`bgwriter`、`checkpointer`、`autovacuum`、`walwriter` 等后台进程 | P1 第 2 章 连接;P5 第 19 章;P6 第 21 章 |
| `src/backend/main/` | `main()` 程序入口,分发到 postmaster/standalone | P1 第 2 章 |
| `src/backend/tcop/` | **Traffic Cop**——一条 SQL 的总调度:解析→分析→重写→规划→执行的串联全在这里 | P1 全篇的"主干道" |
| `src/backend/parser/` | 词法/语法分析(lex/yacc)+ 语义分析 → 生成解析树 | P1 第 3 章 |
| `src/backend/optimizer/` | 查询优化器(分 `path/`、`plan/`、`prep/`、`util/`、`geqo/` 五个子目录) | P1 第 4 章 |
| `src/backend/executor/` | 执行器(火山模型、各种算子) | P1 第 5 章 |
| `src/backend/commands/` | 各种 `CREATE/ALTER/DROP/VACUUM/...` 命令的实现 | P6 第 21 章 等 |
| `src/backend/catalog/` | 系统目录(`pg_class`、`pg_proc`...)的管理 | 少量 |
| `src/backend/rewrite/` | 视图重写规则系统 | 少量 |
| `src/backend/nodes/` | 解析树/计划树节点的创建、拷贝、序列化 | P1 第 3、4 章 |
| `src/backend/utils/` | 杂项工具大筐:内存上下文、缓存、快照、统计、时间、排序、类型转换... | 全书零散引用 |
| `src/backend/storage/buffer/` | **Buffer Pool**(`bufmgr.c`、`freelist.c`、`localbuf.c`、`buf_init.c`) | P2 第 8 章 |
| `src/backend/storage/smgr/` | 存储管理器抽象(在 Buffer 和真实文件之间) | P2 |
| `src/backend/storage/lmgr/` | **锁管理器**:常规锁 `lock.c`、轻量锁 `lwlock.c`、死锁检测 `deadlock.c`、SSI `README-SSI` | P4 第 16、17 章 |
| `src/backend/storage/ipc/` | 进程间共享内存、信号量 | P1 第 2 章、P4 |
| `src/backend/storage/page/` | 页面帮助函数(item pointer 操作等) | P2 第 6 章 |
| `src/backend/storage/freespace/` | FSM(空闲空间映射) | P2 第 9 章 |
| `src/backend/access/heap/` | **堆表存取方法**:`heapam.c`、`heap_insert`、可见性判断 | P2 第 7 章、P4 第 17 章 |
| `src/backend/access/nbtree/` | **B 树**(默认索引)实现 | P3 第 11 章 |
| `src/backend/access/{brin,gin,gist,hash,spgist}/` | 其他索引家族 | P3 第 12 章 |
| `src/backend/access/transam/` | **事务与持久性**:WAL(`xlog.c`)、事务状态(`xact.c`、`clog.c`、`varsup.c`)、快照、SLRU、两阶段提交 | **P4 第 14 章 + P5 全篇** |
| `src/backend/replication/` | 流复制、WAL sender/receiver | P6 第 22 章 |

> **一个反直觉点**:`xlog.c`(WAL 的总入口,4000+ 行)不在 `storage/` 下,而在 `access/transam/` 里;`xact.c`(事务)也在那里。**事务子系统和持久性子系统住在同一个目录**——这恰恰印证了全书主线:两者都是"让数据不丢不乱"那一侧,本来就是一家。

### `src/include/`:所有结构体的家

正文里几乎所有"先看这个对象长什么样"的引用,都指向 `src/include/`。它的子目录布局**和 `src/backend/` 一一对应**(`include/storage/`、`include/access/`、`include/executor/`...)。几个全书反复出现的结构体真实位置:

| 结构体 | 定义文件 | 行号 |
|---|---|---|
| `PageHeaderData`(8KB 页的头) | `src/include/storage/bufpage.h` | L155 |
| `HeapTupleHeaderData`(一行 tuple 的头) | `src/include/access/htup_details.h` | L153 |
| `BufferDesc`(缓冲槽的描述符) | `src/include/storage/buf_internals.h` | L245 |

记住这个习惯:**想看一个对象长什么样,先去 `src/include/` 里找它的 `typedef struct`**,再看操作它的函数。这是读 PG 源码最重要的方法论之一(见第五节)。

---

## 三、阅读路线:对应"一条 SQL 旅程"的源码路径

这条路线,是本书 P1→P5 的旅程在源码里的精确落地。每一步都给入口函数和真实行号——你可以打开 `postgresql-17.0/` 真的跟着走一遍。

### 第 0 步:进程起来

```
main()                                   src/backend/main/main.c           L58
 └─ PostgresMain()                       src/backend/tcop/postgres.c       L4239
```

`main()` 根据 `argv[0]` 的名字分发:如果叫 `postgres`,就走 `PostgresMain`(单用户 standalone 模式);正常生产是 postmaster fork 出子进程后,子进程也跳到 `PostgresMain` 进入命令循环。

### 第 1 站:连接与会话(P1 第 2 章)

```
postmaster 监听 → fork 一个 backend 子进程
ServerLoop()                             src/backend/postmaster/postmaster.c   L1624
 └─ 子进程进入 PostgresMain()            src/backend/tcop/postgres.c           L4239
```

`postmaster/postmaster.c` 里有完整的进程模型:`ServerLoop` 是主进程的 accept 循环;`BackendStartup`(同文件)负责 fork。其他后台进程(`bgwriter.c`、`checkpointer.c`、`walwriter.c`、`autovacuum.c`)也都在 `postmaster/` 目录里——它们都是 postmaster 在启动时拉起来的兄弟进程。

### 第 2 站:解析(P1 第 3 章)

进入 backend 命令循环后,一条简单 SQL 走 `exec_simple_query`,这是**整条旅程的总调度函数**,后面几站都从它分支出去:

```
exec_simple_query()                      src/backend/tcop/postgres.c       L1017
 ├─ pg_parse_query()                     src/backend/tcop/postgres.c       L615   → 词法/语法 (parser/gram.y)
 ├─ pg_analyze_and_rewrite_fixedparams() src/backend/tcop/postgres.c       L675   → 语义分析 (parser/analyze.c)
```

`pg_parse_query` 实际调到 `parser/parser.c` 里的 `raw_parser`,后者由 `gram.y`(yacc 文法)生成。想看"SELECT 语法是怎么定义的",直接打开 `src/backend/parser/gram.y` 搜 `SelectStmt`。

### 第 3 站:优化(P1 第 4 章)

```
exec_simple_query() 续
 ├─ pg_plan_queries()                    src/backend/tcop/postgres.c       L976
     └─ planner()                        src/backend/optimizer/plan/planner.c
```

优化器是 `src/backend/optimizer/` 下分五个子目录的大模块:`path/`(生成等价路径)、`plan/`(路径落成计划)、`prep/`(预处理,如子查询上拉)、`util/`(代价模型、属性等价类)、`geqo/`(遗传算法,处理多表 join 顺序)。每个子目录都有 `README`,从那里读起最快。

### 第 4 站:执行(P1 第 5 章)

```
exec_simple_query() 续
 ├─ PortalStart()                        src/backend/tcop/pquery.c         (经由 portal)
 └─ PortalRun()                          src/backend/tcop/pquery.c         L684
     └─ ExecutorRun()                    src/backend/executor/execMain.c
```

`PortalRun` 在 `pquery.c`(同目录 L684)里,它把执行分派到 `ExecutorRun`(执行器总入口,在 `executor/execMain.c`)。从 `execMain.c` 出发,你就能看到火山模型的根节点如何调用 `ExecProcNode`,再一路向下到各种 `nodeSeqscan.c`、`nodeHashjoin.c`、`nodeAgg.c` 等算子。

### 第 5 站:存储与缓冲(P2)

```
执行器要读一页 →
ReadBuffer()                             src/backend/storage/buffer/bufmgr.c   L745
 └─ ReadBuffer_common()                  src/backend/storage/buffer/bufmgr.c   L1198   (命中/淘汰/IO 都在这)
```

`bufmgr.c` 是 Buffer Pool 的中枢,命中、淘汰(Clock-Sweep,在 `freelist.c`)、脏页标记全在这里。`storage/buffer/README` 是必读——它就是 PG 缓冲管理的官方设计文档。

往底层是 `storage/smgr/`(smgr 抽象,屏蔽不同存储后端),再往上是 `access/heap/heapam.c` 的 `heap_insert`(L1994)等——它把"插入一行"翻译成"在一个页里加一个 tuple,可能触发页分裂"。

### 第 6 站:事务与并发(P4)

```
heap_insert 的调用栈里夹着可见性与锁判断 →
xmin/xmax 设置、快照获取                src/backend/access/transam/xact.c
行锁                                    src/backend/access/heap/heapam_visibility.c
表/常规锁                               src/backend/storage/lmgr/lock.c
轻量锁(保护共享内存结构)               src/backend/storage/lmgr/lwlock.c
死锁检测                                src/backend/storage/lmgr/deadlock.c
SSI(可串行化隔离)                      见 src/backend/storage/lmgr/README-SSI
```

### 第 7 站:持久性(P5)

```
改数据前先写 WAL →
xlog.c(4000+ 行,WAL 的总入口)         src/backend/access/transam/xlog.c
检查点                                  src/backend/access/transam/xlog.c  (CheckPointGuts 等)
崩溃恢复(redo)                          src/backend/access/transam/xlog.c  (StartupXLOG 等)
SLRU(clog/multixact 这种辅助日志的存储) src/backend/access/transam/slru.c
```

`xlog.c` 很大,**不要从头读**。先读 `access/transam/README` 整体了解 WAL 的设计,再按本书 P5 各章的引用跳到具体函数。

---

## 四、工具搭建

光靠编辑器和 grep 也能读,但搭好工具后效率差一个数量级。三件必备:索引、调试器、grep。

### 4.1 建索引:ctags / cscope

**PG 官方自带一个建索引脚本**(这是真实存在的,不是我自己编的命令):

```bash
# 在 postgresql-17.0/ 根目录下执行
src/tools/make_ctags          # 生成 vi 风格的 tags 文件
src/tools/make_ctags -e       # 生成 emacs 风格的 TAGS
```

脚本内部用 `find ... -name "*.[chly]"` 扫全部 C 源码,再调系统的 `ctags` 生成索引,并把 `tags` 文件**软链到每个子目录**(这样你在任意子目录里 vim 跳转都生效)。它会自动识别 Exuberant Ctags 并加上 `--c-kinds=+dfmstuv`(把宏定义、函数、结构体等都纳入)。

如果你想要**跨文件调用关系**的索引(ctags 只能跳定义,不能反查"谁调了我"),用 cscope——PG 没自带脚本,但自己建很简单:

```bash
cd postgresql-17.0
find . -name "*.[chly]" -o -name "*.cpp" > cscope.files
cscope -b -q -k        # -b 只建库不进交互界面,-q 加速符号查找,-k 不查系统头
```

生成 `cscope.out` 后,在 vim 里 `:cs add cscope.out`,就能用 `:cs find g 符号`(找定义)、`:cs find c 符号`(找谁调用了它)、`:cs find s 符号`(找所有出现)。

> **小提示**:`cscope.files` 生成后别删,放仓库根目录,以后 cscope 启动会自动认。重新 `cscope -b` 即可增量更新。

### 4.2 gdb:attach 一个 backend 断点跟一条 SQL

PG 是"每连接一进程",这点对调试是**天大的好消息**:你可以精确地 attach 到那条 SQL 所在的进程。

```bash
# 1. 编译 PG 时带调试信息(默认 configure 就带 -g,但确认一下)
./configure CFLAGS="-g -O0" --enable-debug   # -O0 防止变量被优化掉
make && make install

# 2. 启动数据库,用 psql 连上
pg_ctl -D data start
psql -d mydb

# 3. 另一个终端,找出这个 psql 对应的 backend PID
#    (在 psql 里: SELECT pg_backend_pid(); )
psql -c "SELECT pg_backend_pid();"

# 4. gdb attach 这个 PID
gdb -p <PID>

# 5. 打断点——比如想看一条 SQL 走 exec_simple_query 时的栈
(gdb) break exec_simple_query
(gdb) break heap_insert
(gdb) break ReadBuffer_common
(gdb) continue

# 6. 回到 psql 敲一句 SQL,gdb 就会断下来
#    看栈:        (gdb) bt
#    看某层变量:   (gdb) frame 3   (gdb) info args   (gdb) print *query
#    单步:        (gdb) next      (gdb) step
```

几个实战技巧:

- **断点要打在 `_common` / `_internal` 这类后缀的内部函数上**,别打在薄包装上。比如打 `ReadBuffer_common` 而不是 `ReadBuffer`(后者只是个转发)。
- **想理解"一条 SQL 究竟走了哪些路径"**,在 `exec_simple_query` 或 `ExecutorRun` 上断下来,然后 `bt` 看调用栈——这本书里画的那些调用图,你能亲眼看一遍。
- **PG 大量用函数指针**(尤其是执行器节点的 `ExecProcNode`),纯断点跟不动。配合 cscope 的"找引用"(`:cs find c`)才能补上函数指针那一跳。

### 4.3 grep / ripgrep:最快的"它在哪"

工具再好,`grep` 永远是最快定位的。用 `ripgrep`(`rg`)更快、更友好:

```bash
# 找一个函数的定义
rg "^heap_insert" src/backend/access/heap/
# 找一个结构体的定义
rg "^typedef struct BufferDesc" src/include/
# 找"谁调用了 heap_insert"(全仓库)
rg "\bheap_insert\(" src/backend/
# 找一个错误信息的出处(调试时常用:用报错文本反查代码位置)
rg "could not serialize access" src/backend/
```

> **命名规律**(读 PG 源码事半功倍):PG 的函数命名高度规律——
> - 解析树节点创建:`makeNode(...)`、`makeTypeName(...)`;
> - 执行器算子:`nodeXxx.c` 里 `ExecInitXxx` / `ExecXxx` / `ExecEndXxx` 三件套;
> - 系统目录访问:`heap_open` / `SearchSysCache`;
> - 锁:`LockRelationOid` / `LockTuple`;
> - 写 WAL:`log_xxx`(如 `log_heap_insert`)。
>
> 看到一个陌生函数名,从命名就能猜出它属于哪个子系统、是 init/run/end 哪个阶段。

---

## 五、读源码的方法论

工具搭好,只是"能读";真要读懂,有四条经验。

### 5.1 从数据结构入手,再看操作它的函数

**这是读一切系统软件源码的总诀,PG 尤其如此。** PG 的代码组织有一个鲜明特点:操作某对象的函数,其行为完全由该对象的内存布局决定。你不懂 `HeapTupleHeaderData` 那 23 字节头是怎么排的,`heap_insert` 里那一堆位运算就看天书;反过来,你把 `PageHeaderData` 的布局画在纸上,`PageAddItem` 几乎能默写出来。

所以本书每个机制都是**先画结构体、再看函数**——这不是写作技巧,是阅读顺序。读源码时同样:看到一个新对象,先 `src/include/` 里 `typedef struct` 看完它的字段,再去看操作它的函数。

### 5.2 顺着一条 SQL 的执行栈读,而不是按目录平铺

最容易犯的错:打开 `src/backend/storage/buffer/bufmgr.c`,从第 1 行往下读——读到第 500 行你已经忘了为什么开始读它。

正确做法:**永远跟着一条 SQL 的调用链读**。你想懂 Buffer Pool,不要平铺读 `bufmgr.c`;而是从 `ReadBuffer`(L745)进,看它怎么查 hash 表找命中、怎么在没命中时调 `BufferAlloc`、怎么触发 Clock-Sweep 淘汰——这条**故事线**上的函数一个一个读,每个都回到"这一步解决了什么"。本书正文就是这条故事线,附录第三节的路线图就是它的源码索引。

> 平铺读法只适合一种情况:**你已经懂了大框架,只是来查某个函数的细节**。学习阶段,一定走调用链。

### 5.3 善用 PG 源码里的 README——它是世界上最好的内核文档

PG 的源码里散落着一批高质量的 `README` 文件,**这些不是开发者随手写的备忘,而是正式的设计文档**。它们比大多数教科书都写得更清楚。下面这张清单,是 `ls` 真实确认存在的:

| README | 在哪 | 讲什么 |
|---|---|---|
| `src/backend/optimizer/README` | 优化器 | 优化器的整体设计与代价模型 |
| `src/backend/executor/README` | 执行器 | 火山模型、表达式求值 |
| `src/backend/storage/buffer/README` | Buffer Pool | 缓冲管理的设计、淘汰、脏页 |
| `src/backend/storage/lmgr/README` | 锁 | 常规锁、轻量锁的层级与使用场景 |
| `src/backend/storage/lmgr/README-SSI` | SSI | 可串行化快照隔离的实现 |
| `src/backend/access/transam/README` | WAL/事务 | WAL 与 MVCC 的整体设计 |
| `src/backend/access/transam/README.parallel` | 并行查询 | 并行框架的协议 |
| `src/backend/access/nbtree/README` | B 树 | B 树(默认索引)的页面布局与算法 |
| `src/backend/access/heap/README.HOT` | HOT | 仅索引更新的 HOT 优化 |

读每篇正文之前,先把对应目录的 README 扫一遍,正文会好懂一倍——你会发现书里很多"为什么这么设计"的解释,根源就在这些 README 里。

### 5.4 用本书引用的行号定位,别从零开始找

正文里每处源码引用都标了文件路径和行号(`#L起始-L结束`)。这是**经过 grep 核对的真实坐标**,不是凭记忆写的(见总纲第五节的源码引用约定)。你的阅读可以这样进行:

1. 读正文某节,看到一段引用的源码;
2. 打开 `postgresql-17.0/` 下对应文件,跳到那个行号区间;
3. 不只读那一段,把它的**前后 30 行也扫一遍**——上下文往往比引用本身信息量更大;
4. 用 cscope/grep 跳到它调用的下一个函数,继续往下挖。

这样积累下来,你会自然在脑子里建起一张"函数 → 文件 → 行号"的地图,而不是面对 6000 个文件发懵。

---

## 六、全书各篇 → 主源码目录速查表

把本书六篇和对应的主源码目录、入口文件一次对齐,撕下来贴墙上:

| 本书篇/章 | 子系统 | 主源码目录 | 关键入口文件/函数 |
|---|---|---|---|
| P0 · 开篇 | (无,概念铺垫) | — | — |
| P1 第 2 章 · 连接与会话 | 进程模型 | `src/backend/postmaster/`、`main/` | `postmaster.c: ServerLoop`(L1624)、`main/main.c: main`(L58)、`tcop/postgres.c: PostgresMain`(L4239) |
| P1 第 3 章 · 解析器 | 解析 | `src/backend/parser/` | `gram.y`、`analyze.c`;经由 `tcop/postgres.c: pg_parse_query`(L615) |
| P1 第 4 章 · 优化器 | 优化 | `src/backend/optimizer/{path,plan,prep,util,geqo}/` | `plan/planner.c`;经由 `tcop/postgres.c: pg_plan_queries`(L976) |
| P1 第 5 章 · 执行器 | 执行 | `src/backend/executor/` | `execMain.c: ExecutorRun`;经由 `tcop/pquery.c: PortalRun`(L684) |
| P2 第 6 章 · 页面 | 页布局 | `src/backend/storage/page/`、`src/include/storage/bufpage.h` | `PageHeaderData`(L155) |
| P2 第 7 章 · 堆表与元组 | 堆存取 | `src/backend/access/heap/` | `heapam.c: heap_insert`(L1994);`include/access/htup_details.h: HeapTupleHeaderData`(L153) |
| P2 第 8 章 · Buffer Pool | 缓冲 | `src/backend/storage/buffer/` | `bufmgr.c: ReadBuffer`(L745)、`ReadBuffer_common`(L1198)、`freelist.c` |
| P2 第 9 章 · FSM/VM | 辅助映射 | `src/backend/storage/freespace/` | `freespace.c` |
| P3 · 索引(全篇) | 索引 | `src/backend/access/{nbtree,hash,brin,gin,gist,spgist}/` | `nbtree/nbtinsert.c` 等;`access/nbtree/README` |
| P4 第 14 章 · ACID | 事务 | `src/backend/access/transam/` | `xact.c` |
| P4 第 16 章 · 锁 | 锁 | `src/backend/storage/lmgr/` | `lock.c`、`lwlock.c`、`deadlock.c`;`README` |
| P4 第 17 章 · MVCC | 多版本 | `src/backend/access/heap/`、`access/transam/` | `heapam_visibility.c`、`xact.c`、`varsup.c` |
| P4 · 可串行化 | SSI | `src/backend/storage/lmgr/` | `README-SSI` |
| P5 第 18 章 · WAL | 预写日志 | `src/backend/access/transam/` | `xlog.c`;`README` |
| P5 第 19 章 · Checkpoint | 检查点 | `src/backend/postmaster/checkpointer.c`、`access/transam/xlog.c` | `CheckPointGuts` |
| P5 第 20 章 · 崩溃恢复 | redo | `src/backend/access/transam/xlog.c` | `StartupXLOG`;`slru.c`(SLRU 子系统) |
| P6 第 21 章 · VACUUM | 死元组清理 | `src/backend/commands/vacuum.c`、`postmaster/autovacuum.c` | — |
| P6 第 22 章 · 复制 | 流复制 | `src/backend/replication/` | `walsender.c`、`walsender.h` |
| P6 第 23 章 · 并行查询 | 并行 | `src/backend/executor/`、`access/transam/README.parallel` | — |
| 附录 B · 观测工具 | 工具 | `src/bin/{psql,pg_waldump,pg_controldata,pg_ctl,...}/` | 见附录 B |

---

## 七、最后一句话

读源码这件事,新人最缺的不是聪明,是**路线**。这篇附录给了你路线、地图、工具,但真正让你钻进去的,是正文里那些"**为什么这么设计**"的好奇心——当你读到 Buffer Pool 的 Clock-Sweep 时忍不住想"PG 真的这么淘汰吗",读到 WAL 时想"那句 `INSERT` 的日志到底长什么样",那时打开 `postgresql-17.0/` 顺着本书的行号跳进去,你会比读任何二手资料都快。

> 本书所有源码引用,都指向同一个固定版本 **PostgreSQL 17.0**。如果你手上的源码不是 17.0,行号可能略有偏移,但函数名、调用关系完全一致——回到 17.0 即可对齐。
