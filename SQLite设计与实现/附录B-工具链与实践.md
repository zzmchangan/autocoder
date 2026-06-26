# 附录 B · SQLite 工具链与实践

> **核心问题**:全书 21 章把 SQLite 的内部机制(VDBE、B-tree、pager、WAL、VFS…)从设计动机到源码技巧都拆透了,可当你合上书、想**亲眼看一下一条 SELECT 在自己机器上编译出什么 opcode、自己建的索引到底有没有被用上、自己的库切换到 WAL 后读写是不是真的并发了**——你该用什么工具、怎么操作、看到的东西怎么和书里讲的对应起来?这份附录给你一套**"动手感受 SQLite 内部"的工具链 + 真实可复现的 EXPLAIN 例子 + 实用 PRAGMA 速查表 + 性能调优要点 + 与前作的对照实验 + 常见场景的注意点**。
>
> **一句话定位**:这份附录不是 API 文档,是"读完正文、想动手验证"的人的**实操手册**。正文给你"为什么这么设计",附录 A 给你"去哪个源码文件看",这里给你"在自己的终端上看到它真的这么跑"。

> **⚠️ 版本基准**:本附录所有命令、PRAGMA、opcode、EQP 输出文本,均以本地 checkout `../sqlite/` 为准——**实际 checkout = `533e59b4`(`sqlite/sqlite` master),对应 SQLite 3.54.0**(`VERSION` 文件实测;`SQLITE_VERSION` 在源码树里是 `--VERS--` 占位,构建时替换)。CLI 行为以 `src/shell.c.in` 实际实现为准,PRAGMA 列表以 `tool/mkpragmatab.tcl` 生成的 `aPragmaName[]` 表为准(本附录每条都核过)。

> **和附录 A 的分工**:附录 A 是**源码地图**(去哪个 `.c` 文件看怎么写的),附录 B 是**工具与实践地图**(在终端上用什么命令看、怎么对照验证)。两者配合用——A 看源码,B 看运行时。

---

## 一、sqlite3 CLI:用 30 秒起一个库

全书讲 SQLite 内部,但读者最先接触的永远是 `sqlite3` 这个命令行工具(CLI)。它是 SQLite 官方维护的交互式前端(源码就在 `src/shell.c.in`,编译出来就是 `sqlite3` 可执行文件)。**它是"动手感受 SQLite 内部"的最便宜入口**——你不需要写一行 C 代码、不需要装 IDE,敲几条 `.命令` 就能看到 opcode、schema、页统计。

> **钉死这件事**:本附录所有"动手"操作,默认都用 `sqlite3` CLI 完成。它比任何 GUI 工具都更贴近 SQLite 真实行为(GUI 工具往往包了一层、隐藏了细节)。装它:Linux/macOS 多数自带(`sqlite3 --version` 看版本,3.7+ 才有完整 WAL,3.25+ 才有窗口函数,建议 3.40+);Windows 从 `sqlite.org/download.html` 下预编译的 `sqlite3.exe`。

### 1.1 起、连、关一个库

最常用的几个点命令,围绕"怎么起一个库、连进去、看它里面有什么、关掉"展开。下面这条链覆盖 90% 的日常:

```sql
-- 1. 启动并打开(或新建)一个数据库文件
$ sqlite3 mydb.sqlite
SQLite version 3.54.0 2025-...
Enter ".help" for usage hints.
sqlite>

-- 2. 看当前连了哪些库(默认 main,ATTACH 的也会列出来)
sqlite> .databases
main: /path/to/mydb.sqlite r/w

-- 3. 看库里有哪些表
sqlite> .tables
users  orders  idx_name

-- 4. 看某张表的建表语句(看 schema 最直接)
sqlite> .schema users
CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT, age INTEGER);
CREATE INDEX idx_name ON users(name);

-- 5. 看全库 schema(所有表/索引/视图/触发器)
sqlite> .schema

-- 6. 看库的元信息(页大小、页数、文本编码、用户版本……)
sqlite> .dbinfo
database page size:          4096
number of pages:             12
page count:                  12
text encoding:               1 (utf8)
database version:            3
...

-- 7. 退出
sqlite> .exit
```

这几个命令里,有几个值得单独点出(**对应正文哪章**标在后面):

| 点命令 | 作用 | 关键参数 | 对应正文 |
|--------|------|----------|----------|
| `.open ?OPTIONS? ?FILE?` | 关闭当前库、重新打开 FILE(可在会话中切换库) | `--readonly`/`--writable`/`:memory:` | P1-02 |
| `.databases` | 列出当前连接的所有库(main + ATTACH 的) | — | P6-18 |
| `.tables ?PATTERN?` | 列出匹配 PATTERN(LIKE 语法)的表 | — | P3-08 |
| `.schema ?PATTERN?` | 打印匹配对象的 CREATE 语句(表/索引/视图/触发器) | — | P3-08 / P6-19 |
| `.dbinfo ?DB?` | 显示库的元信息(页大小、页数、编码等) | — | P3-08 / P4-11 |
| `.show` | 显示当前所有 CLI 设置(mode、headers、timer 等) | — | — |
| `.exit ?CODE?` | 退出(可带返回码) | — | — |

> **怎么读 .dbinfo 的输出**:它的 `database page size` 就是 P3-08 讲的 B-tree 页大小(默认 4096);`number of pages` 是整个 db 文件的总页数;`text encoding`(1=utf8/2=utf16le/3=utf16be)决定 P3-09 记录里字符串怎么存。这张元信息表是"一眼看穿库物理形态"的窗口——你建完表、插完数据后跑一下 `.dbinfo`,能直观感受到"原来我这个库现在是 12 页、每页 4KB"。

### 1.2 导入导出:`.dump` / `.import` / `.save`

嵌入式场景里,库的导入导出很常见(备份、迁移、测试 fixture)。CLI 提供三个核心命令:

**`.dump`——把整个库导成 SQL 文本**(最常用的备份方式):

```sql
-- 导出全库(所有 CREATE + INSERT),输出到屏幕
sqlite> .dump
PRAGMA foreign_keys=OFF;
BEGIN TRANSACTION;
CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT, age INTEGER);
INSERT INTO users VALUES(1,'alice',30);
INSERT INTO users VALUES(2,'bob',25);
COMMIT;

-- 导出到文件(配合 .output)
sqlite> .output backup.sql
sqlite> .dump
sqlite> .output stdout

-- 命令行一行导出
$ sqlite3 mydb.sqlite .dump > backup.sql
```

`.dump` 的价值在于:**它产出的是纯 SQL 文本**——跨版本、跨平台、人可读,而且恢复时只要 `sqlite3 newdb < backup.sql` 即可。这是 SQLite 官方推荐的逻辑备份方式(物理拷贝 `.db` 文件也行,但跨大版本或架构时可能不兼容)。注意:`.dump` 走的是"逐行读出来重新生成 INSERT",对大库会慢,大库备份推荐 `.backup`(在线热备,见后)。

**`.import FILE TABLE`——把 CSV/文本文件导入表**:

```sql
-- 准备:先建表
sqlite> CREATE TABLE scores(id INTEGER, name TEXT, score REAL);

-- 导入 CSV(默认按分隔符切列)
sqlite> .mode csv
sqlite> .import data.csv scores

-- 导入后检查
sqlite> SELECT COUNT(*) FROM scores;
```

`.import` 的注意点:① 它**不会自动建表**(你得先 `CREATE TABLE`);② 默认分隔符由 `.mode`/`.separator` 控制(`.mode csv` 用逗号,`.mode tabs` 用制表符);③ 第一行如果是表头,导入后手动 `DELETE FROM scores WHERE rowid=1` 或导入前 `.import --skip 1 data.csv scores`(3.32+ 支持跳过)。大文件导入前务必 `BEGIN; ... COMMIT;` 包起来(见第四节性能调优),否则每行一个事务会慢到怀疑人生。

**`.save ?OPTIONS? FILE`(别名 `.backup`)——在线热备**:

```sql
-- 把当前库在线备份到新文件(源库可继续读写,基于 sqlite3_backup API)
sqlite> .save backup_copy.sqlite
```

`.save` 走的是 `sqlite3_backup_*` 接口(源码 `src/backup.c`),它是 SQLite 的**在线备份机制**——源库在被备份的同时还能正常读写(读不阻塞,写会在 page 层短暂协调)。这比"关闭服务 + 拷文件"优雅得多,也比 `.dump` 快(直接按页拷,不重新生成 SQL)。生产环境定时备份首选 `.save`(或 API 层的 `sqlite3_backup_init`)。

### 1.3 输出格式:`.mode` / `.headers` / `.width` / `.timer` / `.explain`

CLI 默认输出是"列表模式"(字段用 `|` 分隔),改输出格式用 `.mode`。这是看 EXPLAIN/调试时最常动的设置:

```sql
-- 切到列对齐模式(最适合人看)
sqlite> .mode column
sqlite> .headers on
sqlite> SELECT * FROM users;
id  name   age
--  -----  ---
1   alice  30
2   bob    25

-- 切到行模式(一行一个字段,长字段最清楚)
sqlite> .mode list
sqlite> SELECT * FROM users;
1|alice|30
2|bob|25

-- 切到 INSERT 模式(输出 INSERT 语句,导数据用)
sqlite> .mode insert
sqlite> SELECT * FROM users;
INSERT INTO "table"(id,name,age) VALUES(1,'alice',30);
INSERT INTO "table"(id,name,age) VALUES(2,'bob',25);

-- 看执行时间(开计时,之后每条 SQL 都打印 real/user/sys 时间)
sqlite> .timer on
sqlite> SELECT COUNT(*) FROM users;
2
Run Time: real 0.000 user 0.000000 sys 0.000000

-- 看 EXPLAIN 的专用格式(自动设好 column + 合适列宽)
sqlite> .explain on
```

`.mode` 支持的子模式(3.54 实测,见 `shell.c.in` 的 `.mode` 处理):`column`(列对齐)/`list`(默认,竖线分隔)/`csv`(逗号分隔)/`tabs`(制表符)/`insert`(INSERT 语句)/`line`(一行一字段)/`html`/`json`(3.33+)/`markdown`/`table`/`box`/`quote`/`ascii`。日常最常用 `column`(人看)+ `csv`/`insert`(导出)。

> **钉死 `.timer`**:这是性能排查的第一工具。开 `.timer on` 后,每条 SQL 都打印 `Run Time: real X user Y sys Z`。`real` 是墙钟时间(含等待),`user`/`sys` 是 CPU 时间。**对比两条写法、或开/关 WAL 前后的 real 时间**,是判断"我这步优化有没有用"的最直接证据。注意第一次执行的 real 时间会含编译(prepare)开销,要看纯执行时间用 prepared statement 多跑几次取稳定值。

### 1.4 执行系统命令与脚本:`.shell` / `.read`

CLI 不离开会话就能跑系统命令、读外部脚本:

```sql
-- 跑一条系统命令(.shell 是 .system 的别名)
sqlite> .shell ls -la mydb.sqlite
-rw-r--r-- 1 user group 49152 Jun 26 mydb.sqlite

-- 从文件读入并执行一连串 SQL/点命令(批量初始化库常用)
sqlite> .read init.sql

-- 也可以启动时直接读
$ sqlite3 mydb.sqlite < init.sql
```

`.read` 在"用一个脚本初始化几十张表 + 测试数据"时极方便(把所有 `CREATE`/`INSERT`/`PRAGMA` 写进 `init.sql`,一行 `.read init.sql` 全执行)。配合 `.dump` 形成"导出→改→导回"的工作流。

### 1.5 其他高频点命令速查

| 命令 | 作用 |
|------|------|
| `.nullvalue STRING` | 用 STRING 代替 NULL 显示(避免 NULL 和空串混淆) |
| `.echo on\|off` | 回显执行的 SQL(调试脚本用) |
| `.bail on\|off` | 出错后是否停止(脚本里建议 on) |
| `.changes on\|off` | 显示每条 SQL 改了几行 |
| `.trace ?OPTIONS?` | 打印每条执行的 SQL(调试用) |
| `.log FILE\|on\|off` | 把日志写文件/stderr(排查崩溃用) |
| `.vfsinfo ?AUX?` | 显示当前用的 VFS 栈(P5-15 讲的 VFS,这里能看真身) |
| `.load FILE ?ENTRY?` | 加载扩展库(如 FTS/RTREE 的运行时加载) |
| `.stats on\|off` | 显示引擎统计(内存、页缓存命中) |
| `.scanstats on\|off\|est` | 3.32+,显示每步扫描的行数(细到 opcode 级,排查慢查询神器) |

> **`.scanstats` 是 3.32+ 的新工具**(对应 `sqlite3_stmt_scanstatus` API),它能把"每个 opcode 实际循环了多少次"打印出来——比 EXPLAIN(只看计划)更深一层,能看到**实际执行**的代价。排查"为什么这条 SQL 慢"时,先 EXPLAIN 看计划、再 `.scanstats on` 看实际循环数,两步定位。

> **CLI 速查小结**:起库 `.open`/连库 → 看库 `.databases`/`.tables`/`.schema`/`.dbinfo` → 改格式 `.mode column`/`.headers on`/`.timer on` → 导入导出 `.dump`/`.import`/`.save` → 看执行 `EXPLAIN`/`.scanstats`。记住这条链,日常 90% 操作够用。

---

## 二、★EXPLAIN 与 EXPLAIN QUERY PLAN:亲眼看 opcode 和查询计划

这是**全书最该动手练**的一节。EXPLAIN 是"看 SQLite 内部"最直接的工具——正文 P2-06/P2-07 讲 VDBE 怎么执行 opcode、P3-10 讲索引怎么选,你读完可能仍半信半疑("真的编译成了 OpenRead/Column/Next 吗?""我的索引真的被用上了吗?")。EXPLAIN 把这些**直接打印到屏幕上**,让你亲眼验证。

> **承接正文**:P2-05(VDBE 虚拟机)、P2-06(opcode 详解)、P2-07(一条 SELECT 怎么执行)、P3-10(索引与查询)。这一节是那几章的"动手验证版"。

### 2.1 两条命令,看两种东西

很多人混用 `EXPLAIN` 和 `EXPLAIN QUERY PLAN`,其实它们**输出完全不同**、用途也不同:

| 命令 | 看什么 | 输出形态 | 源码真身 |
|------|--------|----------|----------|
| `EXPLAIN <SQL>` | **opcode 流**(VDBE 字节码) | 每条 opcode 一行:addr/opcode/p1/p2/p3/p4/p5/comment | `sqlite3VdbeList()`(`vdbeaux.c#L2413`),`p->explain==1` |
| `EXPLAIN QUERY PLAN <SQL>` | **查询计划**(人类可读) | 每个扫描节点一行:id/parent/notfrom/detail | 同函数,`p->explain==2`,文本由 `wherecode.c` 生成 |

> **钉死两者的区别**:`EXPLAIN` 是给**懂 VDBE 的人**看的(对应正文第 2 篇)——它打印的是机器要执行的 opcode 流,你得知道 `OpenRead`/`Column`/`Next` 是什么意思。`EXPLAIN QUERY PLAN`(简称 EQP)是给**只想知道"走了全表扫还是索引"的人**看的——它把 opcode 流"翻译"成人类可读的 `SCAN users`/`SEARCH users USING INDEX idx_name`,不需要懂 opcode。日常排查先用 EQP(快),深入定位再用 EXPLAIN(细)。

### 2.2 EXPLAIN:看 opcode 流

先建一个最小 schema,塞两条数据:

```sql
sqlite> .open test.sqlite
sqlite> CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT, age INTEGER);
sqlite> INSERT INTO users VALUES(1, 'alice', 30), (2, 'bob', 25);
sqlite> CREATE INDEX idx_name ON users(name);
```

现在 `EXPLAIN` 一条最简单的点查:

```sql
sqlite> .explain on
sqlite> EXPLAIN SELECT name FROM users WHERE id=1;
addr  opcode         p1    p2    p3    p4             p5  comment
----  -------------  ----  ----  ----  -------------  --  -------------
0     Init           0     9     0                    0   Start at 9
1     OpenRead       0     2     0     1              0   root=2 iDb=0 name=users
2     Integer        1     1     0                    0   r[1]=1
3     SeekRowid      0     5     1                    0   intkey=r[1] priokey
4     Column         0     1     2                    0   r[2]= cursor 0 column 1
5     ResultRow      2     1     0                    0   output=r[2]..r[2]
6     Close          0     0     0                    0
7     Halt           0     0     0                    0
8     Goto           0     1     0                    0
9     Goto           0     1     0                    0
```

这就是 P0-01/P2-07 反复说的"SQL 被编译成 opcode 流"——**亲眼看到了**。逐行解读(对应正文 P2-06 opcode 详解):

- **`Init 0 9`**:程序入口,跳到 addr 9 开始(实际 addr 9 又 Goto 回 1,这是 SQLite 的固定入口套路)。
- **`OpenRead 0 2 0 "1"`**:打开游标 0,读根页为 2 的 B-tree(就是 `users` 表),`p4=1` 表示只读 1 列(name)。**这就是 VDBE 调 B-tree 接口的入口**(正文 P2-05 讲的"opcode → B-tree"枢纽)。
- **`Integer 1 1`**:把常量 `1` 放进寄存器 1(`r[1]=1`)。
- **`SeekRowid 0 5 1`**:在游标 0 上,按 rowid(=`r[1]` 的值 1)定位。`INTEGER PRIMARY KEY` 的列就是 rowid,所以 `WHERE id=1` 直接按 rowid 在 B-tree 上做点查。命中就前进,否则跳到 addr 5。
- **`Column 0 1 2`**:从游标 0 取第 1 列(name),放进寄存器 2。
- **`ResultRow 2 1`**:把 `r[2]` 作为结果行输出(`r[2]..r[2]`,1 列)。
- **`Close` / `Halt`**:关闭游标、停机。

> **这一串 opcode 在源码里的真身**:每个 opcode 的实现就在 `vdbe.c` 的 `sqlite3VdbeExec` 大 switch 里。比如 `OP_Column` 的 case 在 [`vdbe.c#L3010`](../sqlite/src/vdbe.c#L3010)(`case OP_Column:`),`OP_OpenRead` 在 [`vdbe.c`](../sqlite/src/vdbe.c)(搜 `case OP_OpenRead`),`OP_ResultRow` 在 [`vdbe.c#L1781`](../sqlite/src/vdbe.c#L1781)。**EXPLAIN 输出的每一行,都能在 `vdbe.c` 里找到对应的 case 块**——这就是和 Lua VM(`luaV_execute`)完全相同的读法(承《Lua》VM 章节)。

**怎么读这 8 列**(列定义在 [`vdbeaux.c#L2413` 的 `sqlite3VdbeList`](../sqlite/src/vdbeaux.c#L2413),`p->explain==1` 分支):

| 列 | 含义 |
|----|------|
| `addr` | opcode 在程序里的地址(从 0 开始,执行靠它跳转) |
| `opcode` | 操作码名字(`OP_xxx` 去掉前缀) |
| `p1` | 第一操作数(常是游标号、表根页) |
| `p2` | 第二操作数(常是跳转目标 addr、寄存器号) |
| `p3` | 第三操作数(常是列号、寄存器号) |
| `p4` | 第四操作数(字符串/数字,如列名、表名、常量) |
| `p5` | 标志位( bitwise flags,进阶调试才看) |
| `comment` | 人类可读注释(需编译时开 `SQLITE_ENABLE_EXPLAIN_COMMENTS`,发行版默认开) |

> **EXPLAIN 的 8 列是 `sqlite3VdbeList` 写死的**:见 [`vdbeaux.c#L2471-L2488`](../sqlite/src/vdbeaux.c#L2471),它依次往 8 个结果寄存器写 addr/opcode-name/p1/p2/p3/p4/p5/comment。所以你看到的格式跨版本稳定,不会突然变。

### 2.3 EXPLAIN QUERY PLAN:看查询计划(人类可读)

EQP 把 opcode 流翻译成"扫描节点树",每行 4 列:`id`/`parent`/`notfrom`/`detail`。对同一条 SQL:

```sql
sqlite> EXPLAIN QUERY PLAN SELECT name FROM users WHERE id=1;
id  parent  notfrom  detail
--  ------  -------  ----------------------------
1   0       0        SEARCH users USING PRIMARY KEY (rowid=?)
```

只一行,说人话:**用主键(rowid)在 `users` 表上 SEARCH**(点查)。`SEARCH` 表示"按索引/rowid 定位少量行"(高效),区别于 `SCAN`(全表扫)。这是 P3-10 讲的"索引选择"在终端上的投影。

换一条会全表扫的:

```sql
sqlite> EXPLAIN QUERY PLAN SELECT name FROM users WHERE age > 20;
id  parent  notfrom  detail
--  ------  -------  --------------------
1   0       0        SCAN users
```

`SCAN users` = 全表扫(因为没有 `age` 上的索引,只能逐行扫)。这时给它建个索引再 EXPLAIN:

```sql
sqlite> CREATE INDEX idx_age ON users(age);
sqlite> EXPLAIN QUERY PLAN SELECT name FROM users WHERE age > 20;
id  parent  notfrom  detail
--  ------  -------  ----------------------------------------
1   0       0        SEARCH users USING INDEX idx_age (age>?)
```

变成 `SEARCH users USING INDEX idx_age (age>?)`——走索引了。**这就是"建索引有没有用"的最直接验证**:建索引前后各跑一次 EQP,看 `SCAN` 变没变 `SEARCH`。

### 2.4 ★三个真实例子:全表扫 vs 索引扫 vs JOIN

把 P3-10 讲的"索引与查询"在终端上验一遍。先扩展 schema:

```sql
sqlite> CREATE TABLE orders(
   ...>   oid INTEGER PRIMARY KEY,
   ...>   uid INTEGER,
   ...>   amount REAL,
   ...>   ts TEXT
   ...> );
sqlite> CREATE INDEX idx_orders_uid ON orders(uid);
sqlite> INSERT INTO orders VALUES
   ...>   (1, 1, 99.5, '2026-06-01'),
   ...>   (2, 1, 30.0, '2026-06-02'),
   ...>   (3, 2, 15.0, '2026-06-03');
```

**例 1:无索引的等值查询(全表扫)**:

```sql
sqlite> EXPLAIN QUERY PLAN SELECT * FROM orders WHERE amount = 99.5;
id  parent  notfrom  detail
--  ------  -------  -------------------
1   0       0        SCAN orders          -- amount 没索引,只能全扫
```

**例 2:有索引的等值查询(索引点查)**:

```sql
sqlite> EXPLAIN QUERY PLAN SELECT * FROM orders WHERE uid = 1;
id  parent  notfrom  detail
--  ------  -------  ---------------------------------------
1   0       0        SEARCH orders USING INDEX idx_orders_uid (uid=?)
```

**例 3:覆盖索引(不用回表)**:

```sql
-- 只查 uid(索引里有),不用回表
sqlite> EXPLAIN QUERY PLAN SELECT uid FROM orders WHERE uid = 1;
id  parent  notfrom  detail
--  ------  -------  ---------------------------------------
1   0       0        SEARCH orders USING COVERING INDEX idx_orders_uid (uid=?)
```

注意多了 `COVERING`——意思是"索引本身就有要查的所有列,不用再回主表取数据"(P3-10 讲的覆盖索引)。**这个 `COVERING` 字样来自源码 [`wherecode.c#L171`](../sqlite/src/wherecode.c#L171)** 的 `zFmt = "COVERING INDEX %s"`。

**例 4:JOIN(嵌套循环,两层节点)**:

```sql
sqlite> EXPLAIN QUERY PLAN
   ...> SELECT u.name, o.amount
   ...> FROM users u JOIN orders o ON u.id = o.uid
   ...> WHERE u.age > 20;
id  parent  notfrom  detail
--  ------  -------  ------------------------------------------------
2   0       0        SCAN u                                            -- 外层:全扫 users(因 age>20 没索引,假设 idx_age 没建)
3   2       1        SEARCH o USING INDEX idx_orders_uid (uid=?)       -- 内层:对每个 u,用索引点查 orders
```

`id` 和 `parent` 列表达**嵌套**:`id=3 parent=2` 表示第 3 行是第 2 行(`SCAN u`)的子节点——对外层 `users` 扫出的每一行,内层用 `idx_orders_uid` 做一次索引点查。这就是 P2-07 讲的"JOIN = 嵌套循环"在 EQP 上的体现。

**例 5:需要排序(临时 B-tree)**:

```sql
sqlite> EXPLAIN QUERY PLAN SELECT * FROM orders ORDER BY amount;
id  parent  notfrom  detail
--  ------  -------  ------------------------------------------
2   0       0        SCAN orders
3   0       0        USE TEMP B-TREE FOR ORDER BY               -- amount 无索引,得建临时 B-tree 排序
```

`USE TEMP B-TREE FOR ORDER BY`(源码 [`select.c#L1648`](../sqlite/src/select.c#L1648) 的 `zFmt`)——SQLite 看到没有可用索引支持 ORDER BY,就建一棵临时 B-tree 来排序,代价不小。**优化提示**:给 `amount` 建索引,这个 TEMP B-TREE 就消失了(索引本身有序)。

> **EQP 文本对照表**(全部源码核实,3.54):

| EQP detail 字样 | 含义 | 源码出处 |
|-----------------|------|----------|
| `SCAN <表>` | 全表/全索引扫 | `select.c#L6981`、`insert.c#L1139` |
| `SEARCH <表> USING INDEX <索引>` | 按索引定位(范围或点查) | `wherecode.c#L152` |
| `SEARCH <表> USING PRIMARY KEY (rowid=?)` | 按 rowid 点查 | `wherecode.c` |
| `SEARCH <表> USING COVERING INDEX <索引>` | 覆盖索引,不回表 | `wherecode.c#L171` |
| `SCAN <表> CONSTANT ROW` | 扫常量子查询(无真实表) | `select.c#L2883`、`where.c#L6956` |
| `USE TEMP B-TREE FOR ORDER BY` | 需临时 B-tree 排序 | `select.c#L1705` |
| `USE TEMP B-TREE FOR DISTINCT` | 需临时 B-tree 去重 | `select.c#L6691` |
| `USE TEMP B-TREE FOR GROUP BY` | 需临时 B-tree 聚合 | `select.c#L6723` |
| `COMPOUND QUERY` | 复合查询(UNION/INTERSECT) | `select.c#L3012` |
| `AUTOMATIC COVERING INDEX` | 自动建临时覆盖索引(无显式索引时) | `wherecode.c#L169` |

> **钉死 EQP 的用法**:① 看到大量 `SCAN` = 慢查询信号,考虑加索引;② 看到 `SEARCH ... COVERING INDEX` = 已最优(不用回表);③ 看到 `USE TEMP B-TREE` = 排序/去重的额外代价,可用索引消除;④ 多行的 JOIN,看 `parent` 列理解嵌套层次。**这是把 P3-10"索引与查询"落到手上的最快路径**。

### 2.5 EXPLAIN 的高级用法

**看 INSERT/UPDATE/DELETE 的 opcode**:不只 SELECT,所有 SQL 都能 EXPLAIN。比如看一条 INSERT 怎么编:

```sql
sqlite> EXPLAIN INSERT INTO users(name, age) VALUES('carol', 28);
addr  opcode         p1    p2    p3    p4             p5  comment
----  -------------  ----  ----  ----  -------------  --  -------
0     Init           0     14    0                    0
1     OpenWrite      0     2     0     3              0   iDb=0 name=users    -- OpenWrite=打开表写(区别于 OpenRead)
2     Integer        0     2     0                    0   r[2]=0
3     OpenWrite      1     3     0     0              0   iDb=0 name=sqlite_sequence   -- 维护自增序列
...
9     NewRowid       0     2     0                    0   r[2]=rowid           -- 申请一个新 rowid
10    Insert         0     3     2                    0   intkey=r[2] data=r[3]  -- 把记录插进 B-tree
...
```

能看到 `OpenWrite`(写模式打开)、`NewRowid`(分配新 rowid)、`Insert`(写 B-tree)——这对应 P1-04 讲的"Code Generator 为 INSERT 产出 opcode"。**注意 INSERT 不像 SELECT 有 EQP 等价物**(EQP 只对 SELECT/UPDATE/DELETE/INSERT 有效,但 INSERT 的 EQP 通常是空或简单提示),深入看 INSERT 必须用 EXPLAIN 看 opcode。

**配合 `.scanstats on` 看实际执行代价**(3.32+):

```sql
sqlite> .scanstats on
sqlite> SELECT COUNT(*) FROM users WHERE age > 20;
2
sqlite> .scanstats est
-- 输出每个 opcode 实际循环的次数(估计或精确)
```

`.scanstats` 比 EXPLAIN 更深一层——EXPLAIN 只告诉你"计划是 SCAN users",`.scanstats` 告诉你"实际 SCAN 循环了 1000 次"。**两步定位慢查询**:先 EQP 看计划合不合理,再 `.scanstats` 看实际代价。

> **承接附录 A**:EXPLAIN 看到的 opcode,正是附录 A 第三节列出的 `vdbe.c` 里 `case OP_xxx` 的实现。**EXPLAIN 是导航、`vdbe.c` 是地图**——你 EXPLAIN 一条 SQL 看到 `OP_Column`,就到 `vdbe.c` 搜 `case OP_Column` 看它怎么实现的。这条"EXPLAIN → vdbe.c"的链,是读 SQLite 执行层最高效的姿势。

---

## 三、PRAGMA 速查:每个开关对应正文章哪一章

PRAGMA 是 SQLite 的"运行时配置开关"——它不是标准 SQL(别的数据库没有 `PRAGMA` 关键字),是 SQLite 专属的引擎调优/诊断接口。正文每一篇都涉及几个 PRAGMA(WAL 用 `journal_mode`、缓存用 `cache_size`、隔离用 `locking_mode`…),这里把它们**集中成一张速查表**,每条标**作用 + 合法值 + 对应正文章**,让你"看完正文想动手验证某个机制"时,一查就知道用哪个 PRAGMA。

> **PRAGMA 的本质**:它改的是 `sqlite3` 连接句柄(正文 P1-02 讲的 `struct sqlite3`,定义在 `sqliteInt.h`)里的标志位/字段。比如 `PRAGMA journal_mode=WAL` 改的是 `Pager.journalMode`(从 `PAGER_JOURNALMODE_DELETE` 变 `PAGER_JOURNALMODE_WAL`),`PRAGMA synchronous=NORMAL` 改的是 `Pager.syncFlags`。**PRAGMA 的处理逻辑全在 [`src/pragma.c`](../sqlite/src/pragma.c)**(一个巨型 `switch`,每个 PRAGMA 一个 case),名字表由 [`tool/mkpragmatab.tcl`](../sqlite/tool/mkpragmatab.tcl) 生成(本附录的 PRAGMA 列表就是从它核出来的,3.54 共 80+ 条)。

### 3.1 事务与持久性类(对应第 4 篇:Pager / WAL)

| PRAGMA | 合法值 | 作用 | 对应正文 |
|--------|--------|------|----------|
| `journal_mode` | `delete`(默认)/`truncate`/`persist`/`memory`/`off`/`wal` | **选择原子提交机制**:`delete`=rollback journal 默认,`wal`=WAL 模式(读写并发)。改完持久化进 db 头(除 `memory`),下次打开仍是该模式 | **P4-12**(rollback journal)/ **P4-13**(WAL) |
| `synchronous` | `0`(OFF)/`1`(NORMAL)/`2`(FULL,默认)/`3`(EXTRA) | **fsync 强度**:OFF=从不 fsync(最快,crash 可能丢)、NORMAL=WAL 模式下 checkpoint 才 fsync、FULL=每次提交 fsync、EXTRA=FULL + 额外 fsync | **P4-13** / **P4-14** |
| `wal_autocheckpoint` | 整数(默认 1000,单位页) | WAL 累积到 N 页自动 checkpoint(0=禁用自动) | **P4-13** |
| `wal_checkpoint` | `PASSIVE`(默认)/`FULL`/`RESTART`/`TRUNCATE` | 手动触发一次 checkpoint,返回 busy/log/ckpt 三个值 | **P4-13** |
| `journal_size_limit` | 字节数(默认 -1) | rollback journal 文件大小上限(单次事务后截断到该值) | P4-12 |
| `locking_mode` | `normal`(默认)/`exclusive` | `exclusive`=连接一直持有排他锁(不释放,省反复加锁开销,但别人打不开) | **P5-17** |
| `auto_vacuum` | `none`(默认)/`full`/`incremental` | 自动回收碎片页:`none`=不自动(VACUUM 手动),`full`=删行自动回收,`incremental`=`full` 但分批(配合 `incremental_vacuum`) | P3-08 |
| `incremental_vacuum` | 整数 N | 配合 `auto_vacuum=incremental`,回收最多 N 页碎片 | P3-08 |
| `secure_delete` | `0`(默认)/`1`/`FAST` | 删行时是否用 0 覆盖原内容(`1`=覆盖,防恢复,慢;`FAST`=覆盖但不 fsync) | P3-08 |

> **最常用的两个**:`PRAGMA journal_mode=WAL`(现代应用 99% 该开,读写并发)+ `PRAGMA synchronous=NORMAL`(WAL 下兼顾安全与性能)。这两个是 P4-13 的核心,后面第四节性能调优会专门讲怎么配。

### 3.2 页与缓存类(对应第 4 篇:Pager / pcache)

| PRAGMA | 合法值 | 作用 | 对应正文 |
|--------|--------|------|----------|
| `page_size` | 字节(默认 4096,须 2 的幂) | **B-tree 页大小**。只能在创建空库前设(改已有库要先 VACUUM)。现代存储建议 4096 或 8192 | **P3-08** / **P4-11** |
| `cache_size` | 整数(默认 -2000) | **pager 页缓存大小**:负数=KB 数(-2000≈2MB),正数=页数。绝对值越大缓存越多、越吃内存 | **P4-11** |
| `default_cache_size` | 同上 | 持久化版(写进 db 头),`cache_size` 是会话级。3.7+ 后 `default_cache_size` 已不推荐,改用 `cache_size` | P4-11 |
| `mmap_size` | 字节数(默认 0=禁用) | 用 mmap 映射 db 文件(大库读优化,避免 read 系统调用)。0=不用 mmap | P4-11 / P5-15 |
| `temp_store` | `default`(默认)/`memory`/`file` | 临时表/索引存哪:`memory`=内存(快但大临时表 OOM),`file`=临时文件 | P4-11 |
| `cache_spill` | 整数(默认=cache_size) | WAL 模式下,dirty 页累积到 N 后 spill 到 WAL(防止内存暴涨) | P4-13 |

> **`page_size` 必须建库前设**:它写进 db 文件头(前 100 字节),一旦有数据就不能直接改(会破坏 B-tree)。要改老库的 page_size:`PRAGMA page_size=8192; VACUUM;`(VACUUM 重建文件,顺带应用新 page_size)。**为什么默认 4096**:与绝大多数 OS 的 page cache 对齐,避免跨页 IO,且 B-tree 节点容量适中(详见 P3-08 页结构)。

### 3.3 Schema 与类型类(对应第 3 篇 + 第 5 篇)

| PRAGMA | 合法值 | 作用 | 对应正文 |
|--------|--------|------|----------|
| `table_info(tbl)` | 查询 | 列出表的列(cid/name/type/notnull/dflt_value/pk),最常用 | P3-08 |
| `table_xinfo(tbl)` | 查询 | 同上,多 `hidden` 列(虚表的隐藏列) | P3-08 |
| `table_list` | 查询(3.37+) | 列出所有表/视图/虚表(schema/name/type/ncol/wr/strict) | P3-08 |
| `index_list(tbl)` | 查询 | 列出表的索引(seq/name/unique/origin/partial) | **P3-10** |
| `index_info(idx)` | 查询 | 列出索引的列(seqno/cid/name) | P3-10 |
| `foreign_key_list(tbl)` | 查询 | 列出表的外键 | P6-19 |
| `foreign_keys` | `0`(默认!)/`1` | **是否启用外键检查**。⚠️ SQLite 默认关!需显式 `PRAGMA foreign_keys=ON` | P6-19 |
| `defer_foreign_keys` | `0`/`1` | 事务内外键检查推迟到 COMMIT(批量导入时临时关) | P6-19 |
| `case_sensitive_like` | `0`(默认)/`1` | LIKE 是否大小写敏感(默认不敏感,改了影响性能) | P3-10 |
| `encoding` | `UTF-8`(默认)/`UTF-16le`/`UTF-16be` | db 文本编码。建库前设,改已有库要 VACUUM | P3-09 |
| `collation_list` | 查询 | 列出所有排序规则 | P3-10 |
| `cell_size_check` | `0`/`1`(默认) | 读 B-tree cell 时是否校验大小(关掉省一点 CPU,但损坏难发现) | P3-08 |

> **`foreign_keys` 默认是关的**——这是 SQLite 最反直觉的默认之一(很多新手以为建了 `FOREIGN KEY` 就生效,其实没有)。**原因**:历史兼容(SQLite 早期没外键,3.6.19 才加,默认关避免破坏老应用)。**生产建议**:连接后第一件事 `PRAGMA foreign_keys=ON;`。

### 3.4 诊断与检查类(对应第 4 篇 + 全书)

| PRAGMA | 合法值 | 作用 | 对应正文 |
|--------|--------|------|----------|
| `integrity_check` | 可选 N | **全库一致性检查**(B-tree 结构、索引与表一致、无孤儿页)。返回 `ok` 或一堆问题描述。排查"库是不是坏了"首选 | **P4-14** |
| `quick_check` | 可选 N | `integrity_check` 的快速版(不检查索引一致性,只查 B-tree 结构) | P4-14 |
| `compile_options` | 查询 | 列出编译时选项(`ENABLE_FTS5`/`ENABLE_JSON1`/`ENABLE_STAT4`…),看你的 SQLite 支持哪些特性 | P0-01 |
| `database_list` | 查询 | 列出当前连接的所有库(main/attached) | P6-18 |
| `freelist_count` | 查询 | 空闲页数(碎片,配合 `auto_vacuum` 回收) | P3-08 |
| `page_count` | 查询 | 总页数 | P3-08 |
| `function_list` | 查询 | 列出所有 SQL 函数(内置+自定义) | P2-06 |
| `module_list` | 查询 | 列出虚表模块(FTS/RTREE/dbstat…) | P6-19 |
| `pragma_list` | 查询 | 列出所有 PRAGMA(本表就是它 + mkpragmatab.tcl 核的) | — |
| `stats` | 查询 | 列出表/索引的行数和页数统计 | P3-10 |
| `lock_status` | 查询 | 列出当前持有的锁状态(unlocked/shared/reserved/pending/exclusive) | **P5-17** |
| `data_version` | 查询 | 库被修改次数计数器(检测"别的连接改了库没") | P5-17 |

> **`integrity_check` 是排查损坏的第一工具**:如果怀疑库被 crash 弄坏了(打开报错、查询结果不对),先跑 `PRAGMA integrity_check;`。返回 `ok` = 库结构完好;返回一堆 `row X is missing from index Y` = 索引坏了(可 `REINDEX` 修复);返回 `database disk image is malformed` = 严重损坏,要用 `.dump` 救数据。它走的是 P4-14 讲的"B-tree 遍历 + 索引交叉验证"逻辑。

### 3.5 版本与控制类

| PRAGMA | 合法值 | 作用 |
|--------|--------|------|
| `user_version` | 整数 | 用户自定义版本号(写进 db 头,做 schema 迁移版本管理常用) |
| `application_id` | 整数 | 应用标识(写进 db 头前 68 字节,让 `file` 命令识别"这是我的库") |
| `schema_version` | 整数(只读) | SQLite 内部 schema 版本(改了 schema 自增) |
| `legacy_file_format` | `0`/`1` | 是否用老文件格式(兼容 3.0-3.5,一般不用动) |
| `automatic_index` | `0`/`1`(默认开) | 是否允许自动建临时索引(无显式索引时)。默认开,关掉可省内存但查询变慢 |
| `recursive_triggers` | `0`(默认)/`1` | 是否允许递归触发器(默认关,防无限递归) |
| `query_only` | `0`/`1` | `1`=只允许读,禁止写(给只读连接加保险) |
| `writable_schema` | `0`/`1` | `1`=允许改 `sqlite_master`(危险!调试用) |

> **`user_version` 做 schema 迁移**:应用升级时,读 `PRAGMA user_version`,根据值决定要不要跑迁移脚本,跑完 `PRAGMA user_version = N+1`。比维护单独的迁移表简单(值就存在 db 头里,不会丢)。

> **PRAGMA 速查小结**:日常最常用 = `journal_mode`(WAL)+ `synchronous`(安全/性能)+ `cache_size`(内存)+ `foreign_keys`(外键)+ `integrity_check`(体检)+ `table_info`/`index_list`(看 schema)。把这 6 个记牢,覆盖 95% 场景。其他用到再查本表。

---

## 四、性能调优要点:把正文章落到手上

正文 P3-10(索引)、P4-13(WAL)、P6-20(prepared statement)讲了 SQLite 高性能的几个根。这一节把它们**收束成可操作的调优清单**,每条都标"动手怎么验"。

### 4.1 prepared statement 复用:别每条 SQL 都重新编译

**为什么**:正文 P6-20 讲过,SQLite 执行 SQL 是"编译(Tokenize/Parser/Code Generator 产出 opcode)+ 执行(VDBE)"两步。编译代价不小(尤其复杂 SQL),如果每执行一次都重新编译,等于每条 SQL 都付一次编译税。**prepared statement = 编译一次,缓存 opcode,反复 bind 不同参数执行**。

**反例(每条都重编译)**:

```python
# Python:每条 SQL 都 prepare 一次(慢)
for uid in range(1000):
    cur.execute("SELECT name FROM users WHERE id=?", (uid,))  # 每次都重新编译
```

**正例(复用 prepared statement)**:

```c
/* C API:prepare 一次,bind 1000 次(快)*/
sqlite3_stmt *stmt;
sqlite3_prepare_v2(db, "SELECT name FROM users WHERE id=?", -1, &stmt, 0);
for (int uid = 0; uid < 1000; uid++) {
    sqlite3_bind_int(stmt, 1, uid);   /* 绑定参数 */
    while (sqlite3_step(stmt) == SQLITE_ROW) { /* 取结果 */ }
    sqlite3_reset(stmt);               /* 复用,不释放 opcode */
}
sqlite3_finalize(stmt);
```

> **CLI 层的 prepared**:`sqlite3` CLI 对带 `?` 占位的 SQL 会内部复用 prepared statement。但如果你在 C 里直接用 `sqlite3_exec`(它内部 prepare+step+finalize 一条龙),每次都会重编译——**高性能场景用 `sqlite3_prepare_v2` + `sqlite3_bind_*` + `sqlite3_step` + `sqlite3_reset` 的四件套**(P6-20 详讲)。

**怎么验**:开 `.timer on`,跑循环 1000 次查询。复用 prepared 的 real 时间通常是重编译的 1/5~1/10。源码:`sqlite3_prepare_v2` 在 [`prepare.c#L943`](../sqlite/src/prepare.c#L943),真身 `sqlite3Prepare` 在 [`prepare.c#L688`](../sqlite/src/prepare.c#L688)。

### 4.2 合理建索引:SCAN 变 SEARCH

正文 P3-10 讲了索引怎么用 B-tree 加速。落到手上就一句话:**用 EQP 检查每条慢查询,看到 `SCAN` 就考虑加索引**。

**调优步骤**(对应第二节的 EQP):

```sql
-- 1. 找慢查询(开 .timer,挑 real 时间长的)
sqlite> .timer on
sqlite> SELECT * FROM orders WHERE amount > 100;   -- 假设 500ms

-- 2. EQP 看计划
sqlite> EXPLAIN QUERY PLAN SELECT * FROM orders WHERE amount > 100;
SCAN orders   -- 全扫,慢

-- 3. 建索引
sqlite> CREATE INDEX idx_amount ON orders(amount);

-- 4. 再 EQP + 计时
sqlite> EXPLAIN QUERY PLAN SELECT * FROM orders WHERE amount > 100;
SEARCH orders USING INDEX idx_amount (amount>?)   -- 走索引了
sqlite> SELECT * FROM orders WHERE amount > 100;   -- 假设 5ms,快 100 倍
```

**索引的坑**(P3-10 详讲,这里点到):

- **不是越多越好**:每个索引都是一棵额外 B-tree,写时要更新所有索引(写放大)。读写比高的场景才值得多建索引。
- **覆盖索引最香**:如果索引包含查询要的所有列,EQP 显示 `COVERING INDEX`,不用回表(见第二节例 3)。
- **WHERE 的列才有用**:索引只在 WHERE/JOIN/ORDER BY/GROUP BY 用到才生效。给 `SELECT` 的列建索引没用。
- **LIKE 前缀才走索引**:`LIKE 'abc%'` 走索引,`LIKE '%abc'` 不走(后缀匹配无法用 B-tree 有序性)。
- **ANALYZE 喂统计**:大表建完索引后跑一次 `ANALYZE;`(生成 `sqlite_stat1`),优化器选索引更准(3.7.17+ 支持 `STAT4` 更精细)。

### 4.3 WAL 模式:读写并发的根本

**为什么默认该开 WAL**:正文 P4-13 讲透——rollback journal 模式(默认)下,写时整个库被写锁独占,读也读不了;WAL 模式下,读读数据文件 + WAL、写只追加 WAL,**读不阻塞写、写不阻塞读**。对任何"有并发读"的应用(Web 后端、App 多线程),WAL 都是必开。

```sql
-- 一行搞定(持久化,改完下次打开还是 WAL)
sqlite> PRAGMA journal_mode=WAL;
wal

-- 验证
sqlite> PRAGMA journal_mode;
wal
```

**WAL 配套建议**:

- `PRAGMA synchronous=NORMAL`(WAL 下安全,NORMAL 已保证提交不丢,只有 checkpoint 时有极小风险)
- `PRAGMA wal_autocheckpoint=1000`(默认,够用;写极频繁可调大)
- WAL 会产生 `mydb.sqlite-wal` 和 `mydb.sqlite-shm` 两个辅助文件,**备份时要一起拷**(或先 `PRAGMA wal_checkpoint(TRUNCATE)` 合并回主库再拷)。

**WAL 的限制**(诚实交代):

- **仍是单写者**:WAL 解决的是"读写并发",**没解决"多写者并发"**——同时只能有一个写事务(P5-17 的 RESERVED 锁)。多线程高并发写仍要排队(用队列或连接池)。
- **网络文件系统不可靠**:WAL 依赖共享内存(`-shm`),NFS/SMB 上共享内存语义不保证,**不要把 WAL 库放网络盘**(用 rollback journal 模式或换 C/S 数据库)。

### 4.4 cache_size 与 synchronous 的权衡

这两个 PRAGMA 是"内存换性能"和"安全换性能"的两个旋钮,常一起调。

**`cache_size`(内存)**:pager 页缓存大小。默认 -2000(≈2MB)。大库读多写少,调到 -20000(≈20MB)甚至更大,缓存命中率上去,IO 大幅减少。

```sql
sqlite> PRAGMA cache_size=-20000;   -- 给 pager 20MB 缓存
sqlite> .timer on
sqlite> SELECT COUNT(*) FROM big_table;   -- 第二次跑明显变快(页都缓存了)
```

**`synchronous`(安全/性能)**:fsync 强度。WAL 下 `NORMAL` 是性价比最高的(比 `FULL` 快,提交仍安全);rollback journal 模式下 `NORMAL` 有极小丢数据风险(系统崩溃才丢,SQLite 自身崩溃不丢),追求极致安全用 `FULL`。`OFF`(0)**永远不要在生产用**(crash 一定丢数据)。

| 场景 | 推荐 |
|------|------|
| 生产 + WAL | `journal_mode=WAL` + `synchronous=NORMAL` + `cache_size=-20000` |
| 生产 + 极致安全(金融) | `journal_mode=WAL` + `synchronous=FULL` + `cache_size=-20000` |
| 临时/缓存库(丢了重建) | `journal_mode=MEMORY` + `synchronous=OFF` + `cache_size=-20000` |
| 只读分析库 | `journal_mode=DELETE` + `synchronous=NORMAL` + `cache_size=-50000`(多给内存) |

### 4.5 事务批处理:别让每条 INSERT 自成一个事务

**SQLite 最常见的性能陷阱**:默认情况下,每条 SQL 自动 BEGIN+COMMIT(autocommit)。这意味着**每条 INSERT 都触发一次完整事务**(写 journal/WAL + fsync)。1000 条 INSERT = 1000 次 fsync,慢到离谱。

**反例(慢)**:

```sql
sqlite> .timer on
sqlite> INSERT INTO t VALUES(1);   -- 每条自动一个事务
sqlite> INSERT INTO t VALUES(2);
sqlite> INSERT INTO t VALUES(1000);
-- 1000 次 fsync,可能要几十秒
```

**正例(快)**:用显式事务把 1000 条包起来,只触发一次 fsync。

```sql
sqlite> BEGIN;
sqlite> INSERT INTO t VALUES(1);
sqlite> INSERT INTO t VALUES(2);
sqlite> INSERT INTO t VALUES(1000);
sqlite> COMMIT;
-- 只 1 次 fsync,几百毫秒搞定
```

> **提速可达 100 倍以上**。导入大文件(`.import`)务必这么包。C API 里同理:用 `BEGIN`/`COMMIT` 显式控制事务边界,别让 autocommit 偷偷给每条加事务。这背后的机制是 P4-12/P4-13 讲的"每个事务 = 一次 journal/WAL 写 + fsync",fsync 是磁盘 IO 最贵的操作。

### 4.6 调优清单(速记)

1. **`PRAGMA journal_mode=WAL`**——99% 的应用该开(读写并发)。
2. **prepared statement 复用**——别每条 SQL 重编译(C API 用 prepare/bind/step/reset 四件套)。
3. **EQP 检查慢查询**——看到 `SCAN` 考虑加索引,`USE TEMP B-TREE` 用索引消除。
4. **事务批处理**——批量写用显式 `BEGIN`/`COMMIT`,别让 autocommit 加税。
5. **`cache_size` 调大**——大库读多,给 pager 更多内存。
6. **`synchronous=NORMAL`(WAL 下)**——安全与性能的平衡点。
7. **`PRAGMA foreign_keys=ON`**——默认关,记得开(如果用外键)。
8. **`PRAGMA integrity_check` 定期体检**——排查潜在损坏。

> **承接**:本节的每一条都对应正文某一章(WAL→P4-13、索引→P3-10、prepared→P6-20、事务→P4-12)。**正文章讲"为什么这么设计",这里讲"在你的库里怎么调"**。两者配合:先读正文理解机制,再用本节清单落地。

---

## 五、★与前作承接:动手做对照实验

本书最大的特色是"站在四本书之上"(P0-06 四重承接):VDBE 字节码虚拟机↔《Lua》VM、B-tree/WAL↔《MySQL·InnoDB》、嵌入式↔《LevelDB》、C/S↔《PG/MySQL》。前面正文章是"指路"(告诉你哪部分前作讲过、本书不重复),这一节是**动手版**——给你具体的对照实验,在自己机器上**亲眼看两套系统用同一个根思想做出不同的东西**。

> **为什么这一节值得单开**:读书时"承接到前作"往往只是一句话带过,读者未必真动手对照过。但"两边都跑一遍、放一起看"的冲击力是纯文字给不了的——你会真切感受到"原来字节码虚拟机是通用的思想"、"原来 B-tree 和 B+树差别就在这里"。这一节把每个承接落成一个可复现的小实验。

### 5.1 与《Lua》对照:VDBE ↔ Lua VM(最强承接)

**承接点**:SQLite 把 SQL 编译成 opcode、用 VDBE 执行;Lua 把源码编译成字节码、用 Lua VM 执行。**两者都是"编译器 + 字节码虚拟机"架构**(P0-01、P2-05 反复强调)。这个实验让你**同时看两边的字节码**。

**实验:同一段"循环加 1"逻辑,Lua 和 SQLite 各自的字节码长什么样**。

Lua 那边(《Lua》P3-10 指令格式、P3-11 解释器循环):

```lua
-- loop.lua
local sum = 0
for i = 1, 10 do
  sum = sum + i
end
print(sum)
```

用 `luac`(Lua 自带的字节码反汇编工具)看字节码:

```bash
$ luac -l -l loop.lua        # -l 打印字节码,-l -l 更详细

main <loop.lua:0,0> (6 instructions at 0x...)
0+ params, 3 slots, 1 upvalue, 1 local, 1 constant, 0 functions
        1       [1]     VARARGPREP      0
        2       [2]     LOADI     0 0          ; 0        -- sum = 0
        3       [3]     FORPREP1  2 4                    -- for 循环预处理
        4       [3]     ADD       0 0 1                  -- sum = sum + i
        5       [3]     FORLOOP   2 2      ; to 4        -- 循环回跳
        6       [5]     GETTABUP  1 0 1     ; _ENV "print"
        7       [5]     MOVE      2 0
        8       [5]     CALL      1 2 1     ; 1 in 0 out
        9       [5]     RETURN    1 1 0
```

SQLite 这边(把同样的循环用 SQL 写,然后用 EXPLAIN 看 opcode):

```sql
sqlite> .open /tmp/test.sqlite
sqlite> CREATE TABLE t(i INTEGER);
sqlite> EXPLAIN INSERT INTO t WITH RECURSIVE c(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM c WHERE i<10) SELECT i FROM c;
addr  opcode         p1    p2    p3    p4             p5  comment
----  -------------  ----  ----  ----  -------------  --  -------
0     Init           0     30    0                    0
1     OpenWrite      0     2     0     1              0   iDb=0 name=t
2     Integer        0     3     0                    0   r[3]=0    -- 计数器清零(类似 sum=0)
3     InitCoroutine  1     19    2                    0
...   (递归 CTE 的 opcode,含 YIELD/Goto 循环)
```

**对照看什么**:

| 维度 | Lua 字节码 | SQLite VDBE opcode |
|------|-----------|-------------------|
| 工具 | `luac -l`(离线反汇编) | `EXPLAIN`(运行时打印) |
| 指令格式 | 32 位定长(P3-10 讲的 `Instruction` 5 字段) | 变长 opcode(8 列:addr/opcode/p1/p2/p3/p4/p5/comment) |
| 算术 | `ADD rA rB rC`(寄存器式) | `Add r1 r2`(也寄存器式,VDBE 用 Mem 数组当寄存器) |
| 循环 | `FORPREP`/`FORLOOP`(专用 opcode) | `Goto`/条件跳转(通用跳转) |
| 分发 | `luaV_execute` 的 switch(P3-11) | `sqlite3VdbeExec` 的 switch(P2-05) |
| 入口 | `lua_load` 编译 + `lua_pcall` 执行 | `sqlite3_prepare_v2` 编译 + `sqlite3_step` 执行 |

> **钉死这个对照**:两边都是"**编译期产出指令流 + 执行期一个大 switch 逐条解释**"。Lua 的 `luaV_execute`(`ldo.c`/`lvm.c`)和 SQLite 的 `sqlite3VdbeExec`(`vdbe.c`)在结构上是**同构的**——都是一个 `for(;;){ switch(opcode){ case OP_XXX: ... } }`。**读 Lua VM 的姿势(带着 luac 输出搜 case),和读 SQLite VDBE 的姿势(带着 EXPLAIN 输出搜 case),是完全一样的**。这是本书最强的承接(《Lua》P3-11 解释器循环 ↔ 本书 P2-05 VDBE)。

**深入实验**:如果想看两边"虚拟机 dispatch"的差异,可以数一下"执行 100 万次空循环"两边各花多久——Lua 5.5 的 `luaV_execute` 用了直接线程化(direct threading,GCC computed goto)优化,SQLite 的 `sqlite3VdbeExec` 在大多数构建里是普通 switch(低编译开销但 dispatch 稍慢)。这是"同一个根思想,不同实现取舍"的活样本。

### 5.2 与《MySQL·InnoDB》对照:B-tree vs B+树、WAL vs redo

**承接点**:SQLite 用 B-tree(非 B+树)+ WAL/rollback journal;InnoDB 用 B+树 + redo/undo log。两者都解决"怎么存、怎么不丢",但结构不同(P3-08、P4-13 对照讲)。这个实验让你**看两边的存储结构和日志模式差异**。

**实验一:B-tree vs B+树——叶子页里有没有数据**。

SQLite 那边:用 `dbstat` 虚表看 SQLite 的 B-tree 页(正文 P3-08 讲过 `dbstat`)。

```sql
sqlite> CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT, payload TEXT);
sqlite> INSERT INTO users VALUES(1, 'alice', repeat('x', 100));
sqlite> INSERT INTO users VALUES(2, 'bob', repeat('y', 100));
-- ... 插入足够多行让 B-tree 分裂
sqlite> SELECT pageno, pagetype, ncell, payload FROM dbstat WHERE name='users';
pageno     pagetype  ncell  payload
---------  --------  -----  -------
2          leaf      50     5200    -- 叶子页,直接存数据行(payload 非零)
3          interior  20     160     -- 内部页,只存 key+子页指针(payload 小)
4          leaf      48     4996    -- 另一个叶子页
```

**关键观察**:SQLite 的 `leaf` 页 `payload` 非零(直接存数据行),`interior` 页 payload 很小(只存导航 key)。**这就是 B-tree 的特征——叶子页存数据**。

InnoDB 那边(《MySQL·InnoDB》P1-04 B+树页与记录):InnoDB 的聚簇索引也是叶子页存数据,但**二级索引的叶子页只存主键 key,不存数据**(要回表)。更根本的是,InnoDB 的内部页**绝对不存数据行**(只存 key + 子页号),数据全在叶子页,叶子页间有双向链表——这是 B+树的定义特征。

```sql
-- MySQL 里看(需要 information_schema,简化示意)
mysql> SELECT page_number, page_type, number_of_records 
       FROM information_schema.INNODB_BUFFER_PAGE 
       WHERE TABLE_NAME LIKE '%users%';
-- 内部页 page_type=index,records 少(只 key)
-- 叶子页 page_type=index,records 多(存完整行)
-- 叶子页之间通过 PAGE_NEXT/PAGE_PREV 链接(双向链表,B+树特征)
```

**对照看什么**:

| 维度 | SQLite B-tree | InnoDB B+树 |
|------|---------------|-------------|
| 内部页存什么 | key + **数据**(行) | key + 子页指针(不存数据) |
| 叶子页存什么 | 数据(行) | 数据(聚簇索引)或主键(二级索引) |
| 叶子页链表 | 无(靠内部页导航) | **有双向链表**(范围扫顺序读) |
| 表的存储 | 一棵 B-tree(rowid 当 key) | 聚簇索引就是数据,二级索引是额外 B+树 |
| 点查路径 | 定位到节点直接拿数据(少一跳) | 走到叶子页才拿数据(多一跳) |

> **钉死这个对照**:SQLite 选 B-tree 是为了"点查少一跳"(嵌入式读为主场景);InnoDB 选 B+树是为了"内部页更小、树更矮、范围扫顺序"(C/S 大数据量场景)。**两种数据结构没有谁更先进,是不同场景的最优解**。这是 P3-08 的核心结论,亲手查 `dbstat` 能让你直观看到 SQLite 叶子页确实存数据。

**实验二:WAL vs redo log——日志写在哪、怎么恢复**。

SQLite 的 WAL(P4-13):用 CLI 直接看 WAL 文件的产生。

```sql
sqlite> PRAGMA journal_mode=WAL;
sqlite> INSERT INTO users VALUES(100, 'carol', 'z');
-- 这时文件系统里多了两个文件:
-- mydb.sqlite-wal   (WAL 日志,追加写)
-- mydb.sqlite-shm   (共享内存索引)
sqlite> PRAGMA wal_checkpoint(TRUNCATE);   -- 手动 checkpoint,合并回主库
-- checkpoint 后 -wal 文件缩小到 0
```

InnoDB 的 redo log(《MySQL·InnoDB》P3-08 WAL 与 redo log):redo 是固定大小的环形文件(`ib_logfile`),WAL 是追加增长的文件。两者都"先写日志再改数据",但:

| 维度 | SQLite WAL | InnoDB redo log |
|------|-----------|----------------|
| 文件形态 | 追加增长(`-wal`),checkpoint 后截断 | 固定大小环形(覆盖写) |
| 共享索引 | 共享内存(`-shm`里的 wal-index) | 无额外索引(logic 在 log block 头) |
| 读时是否查日志 | 是(读数据文件 + WAL) | 否(buffer pool 命中即可,redo 只用于恢复) |
| 并发读不阻塞写 | 是(WAL 核心) | 是(MVCC + undo 实现,机制不同) |
| undo | **无**(靠 rollback journal 记原内容) | 有(undo log + MVCC 版本链,P3-10) |

> **钉死这个对照**:两者都是"Write-Ahead Logging"思想(改前先记日志,保 crash 不丢),但 SQLite 的 WAL 更简单(嵌入式、单文件、共享内存),InnoDB 的 redo + undo 更复杂(C/S、多事务并发、MVCC)。**SQLite 没有 undo log**——它靠 rollback journal 记"原内容"做回滚,而 InnoDB 的 undo 记"怎么改回去"(逻辑日志)+ MVCC 版本链(P3-10/P4-13)。这是嵌入式简化的典型取舍。

### 5.3 与《LevelDB》对照:嵌入式 B-tree vs LSM

**承接点**:SQLite 和 LevelDB 都是嵌入式(链接进应用、单文件/单目录),但存储结构完全不同——SQLite 用 B-tree(就地更新),LevelDB 用 LSM(只追加 + Compaction)。这个实验让你**看两种存储在"写"上的根本差异**。

**实验:同样写 10000 条数据,看两种引擎的写行为**。

SQLite(B-tree 就地更新):

```sql
sqlite> .timer on
sqlite> PRAGMA journal_mode=WAL;
sqlite> CREATE TABLE kv(k INTEGER PRIMARY KEY, v TEXT);
sqlite> BEGIN;
sqlite> INSERT INTO kv VALUES(1, 'a');   -- ... 10000 条
sqlite> COMMIT;
-- B-tree 的写:找到叶子页 → 改页 → 页写回(就地更新)
-- WAL 里追加记录这次改页
sqlite> SELECT COUNT(*) FROM kv;
10000
-- db 文件大小:约 1-2MB(数据 + B-tree 结构)
```

LevelDB(LSM 只追加,承《LevelDB》P1-04 SkipList/MemTable、P2-07 SSTable、P5-17 WAL):

```
-- LevelDB 的写路径(概念):
-- 1. 写 WAL(追加)
-- 2. 写 MemTable(SkipList,内存)
-- 3. MemTable 满了 → freeze → 转 SSTable(L0 文件,追加写)
-- 4. L0 文件多了 → Compaction → 归并到 L1/L2...(后台归并)
```

**对照看什么**:

| 维度 | SQLite(B-tree) | LevelDB(LSM) |
|------|----------------|---------------|
| 写路径 | 找页 → 就地改页 + WAL | WAL + MemTable(SkipList)→ SSTable |
| 写放大 | 1 次(改页)+ WAL | 多次(Compaction 反复读写) |
| 读放大 | 1 次(B-tree 点查) | 多次(查多层 SSTable,靠布隆过滤) |
| 空间放大 | 1(就地更新,无重复) | >1(多层重复数据,靠 Compaction 回收) |
| 随机写 | 慢(要找页改页) | **快**(只追加 MemTable) |
| 范围读 | 中(B-tree 叶子页顺序) | 快(SSTable 归并后顺序) |
| 接口 | SQL(丰富) | KV(Get/Put/Delete/Scan) |
| 适用场景 | 读为主、事务、复杂查询 | 写极多、KV、简单查询 |

> **钉死这个对照**:这是"三放大三角"(写放大/读放大/空间放大,P7-21 收束讲)的活样本。SQLite 选 B-tree 牺牲"随机写速度"换"读快 + 空间省 + 事务";LevelDB 选 LSM 牺牲"读 + 空间"换"随机写极快"。**SQLite 适合"读多写少 + 要 SQL"的端侧场景;LevelDB 适合"写极多 + KV"的存储引擎场景**(TiKV 的单机引擎就是 RocksDB,LSM 派)。亲手写一遍,能感受到 LSM"写快但 Compaction 后台忙"、B-tree"写慢但读即得"的差异。

**深入实验**:如果你装了 LevelDB,可以用它的 `ldb` 工具或写个小程序,写 10000 条后看 `.ldb` 目录——会看到多个 `.log`/`.ldb` 文件(LSM 多层结构);而 SQLite 始终是**一个 `.db` 文件**(单文件 B-tree)。这种"单文件 vs 多文件"的视觉差异,就是 B-tree 就地更新 vs LSM 只追加 + Compaction 的直接结果。

### 5.4 与《PG/MySQL》对照:嵌入式 vs C/S

**承接点**:SQLite 是嵌入式(库链接进 App),PG/MySQL 是 C/S(独立服务进程,客户端走网络)。这个对照在 P0-01 用"自带发动机小车 vs 车队+加油站"点睛过。

**实验:看"连接"这件事在两边的差异**。

SQLite:没有"连接服务器"这步,`sqlite3_open` 直接打开文件。

```sql
-- SQLite:打开就是 open 文件,没有网络往返
sqlite> .open mydb.sqlite
-- 进程内直接读写文件,微秒级
```

MySQL:每次连接要 TCP 握手 + 认证 + 线程分配。

```sql
-- MySQL:连接是网络操作,毫秒级
$ mysql -h 127.0.0.1 -u root -p mydb
-- 走 TCP(甚至跨机),server 端 fork 线程/进程
```

**对照看什么**:

| 维度 | SQLite(嵌入式) | MySQL/PG(C/S) |
|------|----------------|----------------|
| 部署 | 一个库 + 一个文件 | 独立服务进程 + 配置 + 运维 |
| 连接 | 进程内函数调用(无网络) | TCP 网络(有往返) |
| 并发写 | 单写者(文件锁) | 多写者(行锁 + MVCC) |
| 多应用共享 | 难(文件锁,单机) | 易(网络,跨机) |
| 运维 | 零(随 App 生灭) | 高(备份、监控、调优、DBA) |
| 单机性能 | 极高(无网络、进程内) | 受网络/序列化影响 |
| 分布式 | 不支持 | 支持(主从/分片) |

> **钉死这个对照**:SQLite 和 PG/MySQL 不是"谁先进",是"解决不同问题"。端侧(手机/浏览器/App)用 SQLite(零运维、随 App 部署);服务端(高并发写、多应用共享、分布式)用 PG/MySQL。**这就是 P0-01 说的"自带发动机小车 vs 车队+加油站"——没有谁更好,看你要不要那套"加油站基础设施"**。

### 5.5 承接实验小结

| 承接 | 对照实验 | 看什么 |
|------|----------|--------|
| ↔《Lua》 | `luac -l` vs `EXPLAIN` | 两边都是"编译成字节码 + VM 执行" |
| ↔《MySQL》 | `dbstat` 看 B-tree vs MySQL B+树 | 叶子页存不存数据(结构差异) |
| ↔《MySQL》 | WAL 文件 vs redo log | WAL 追加 + 共享内存 vs redo 环形 |
| ↔《LevelDB》 | 写 10000 条看文件数 | 单文件 B-tree vs 多文件 LSM |
| ↔《PG/MySQL》 | open vs connect | 进程内调用 vs 网络往返 |

> **钉死这一节的价值**:读书时"承接"是抽象的,亲手跑一遍对照实验,你才真正"看见"VDBE 和 Lua VM 同构、B-tree 叶子页存数据、WAL 和 redo 都是先写日志。**这是从"读懂"到"融会贯通"的一步**——你会发现数据库/虚拟机的核心思想是相通的,SQLite 只是用"嵌入式、单文件"的方式重做了一遍。

---

## 六、常见场景实践:端侧 / 测试 / 配置 / 嵌入式

SQLite 是"全球部署量最大的数据库",不是因为它跑在服务器上,而是因为它**嵌在每一个手机 App、浏览器、桌面软件里**。这一节覆盖四个最常见的落地场景,每个标**注意点**(容易踩的坑)。

### 6.1 端侧应用:手机 App / 浏览器 / 桌面

**场景**:App 要本地存数据(通讯录、聊天记录、离线缓存、用户偏好),浏览器存书签/历史/Cookie,桌面软件存配置/工程文件。这些都该用 SQLite(不要自己发明文件格式)。

**怎么用**:

- **Android**:`android.database.sqlite` 包(底层就是 SQLite),或用 Room(ORM,封装 SQLite)。
- **iOS**:`sqlite3` C API 直接调,或用 Core Data / GRDB.swift(封装)。
- **浏览器**:Chrome/Firefox/Safari 内部都用 SQLite 存历史/书签;Web 平台可用 `sql.js`(SQLite 编译成 WASM)或浏览器的 IndexedDB(非 SQLite)。
- **桌面**:Electron 内置 `better-sqlite3`;Qt 有 `QSqlDatabase`;Python 内置 `sqlite3`。

**注意点**:

- **开 WAL**:`PRAGMA journal_mode=WAL;`。App 多线程(UI 线程 + 后台线程)并发读写,WAL 让读不阻塞写,避免 ANR/UI 卡顿。
- **连接生命周期**:一个连接尽量在一个线程用(SQLite 连接不是线程安全的默认配置,`SQLITE_THREADSAFE` 编译选项决定,多数构建是 serialized 模式但建议每线程一连接)。Android 上推荐用单例 `SQLiteOpenHelper` + 连接池。
- **数据库迁移**:用 `PRAGMA user_version` 做 schema 版本管理(见第三节),App 升级时读版本号、跑迁移脚本。
- **崩溃恢复**:SQLite 的 ACID 保证 crash 不丢已提交事务(P4-14),但 App 要处理"打开报 `database disk image is malformed`"的极端情况——跑 `PRAGMA integrity_check`,坏了用备份 `.db` 文件恢复。
- **体积裁剪**:移动端如果只用基本 SQL,可裁掉 FTS/RTREE/JSON 等扩展编译,减小 SQLite 库体积(用 `SQLITE_OMIT_*` 编译选项)。

> **真实例子**:Chrome 浏览器用 SQLite 存历史、书签、Cookie;微信/WhatsApp 用 SQLite 存聊天记录;iOS 的很多系统服务(短信、备忘录)底层是 SQLite。**你手机里可能有几十个 SQLite 库在跑**,只是你不知道。

### 6.2 测试:mock / fixture / 内存库

**场景**:单元测试/集成测试需要一个"快、隔离、可重置"的数据库。SQLite 的 `:memory:` 模式(纯内存库,不落盘)是测试的完美选择——微秒级、测试完即销毁、不污染文件系统。

**怎么用**:

**Python 的内存库测试**(pytest fixture):

```python
import sqlite3
import pytest

@pytest.fixture
def db():
    """每个测试函数拿到一个全新的内存库,测试完自动销毁"""
    conn = sqlite3.connect(":memory:")   # 纯内存,进程退出即消失
    conn.executescript("""
        CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT);
        INSERT INTO users VALUES(1, 'alice');
    """)
    yield conn
    conn.close()   # 测试完关闭,内存自动释放

def test_count(db):
    cur = db.execute("SELECT COUNT(*) FROM users")
    assert cur.fetchone()[0] == 1
```

**注意点**:

- **`:memory:` 真的快**:没有文件 IO,所有操作在内存(VFS 用 `memdb.c`,P5-15 讲过),测试套件跑几千个用例也不慢。
- **每个测试一个新库**:用 fixture 保证隔离(`:memory:` 每个连接独立,不共享)。不要用一个共享文件库跑所有测试(会互相污染、且慢)。
- **mock 与真实库的取舍**:测"SQL 逻辑"(JOIN/WHERE/事务)用真实 SQLite;测"业务逻辑"(不碰 SQL)用 mock 对象。**不要 mock 数据库**(mock SQL 行为容易和真实行为不一致,导致测试通过但生产挂)。
- **与生产数据库的差异**:如果你的生产是 PostgreSQL/MySQL,用 SQLite 测试要小心 SQL 方言差异(如 `SERIAL`/`RETURNING`/窗口函数语法)。**推荐测试也用和生产同源的数据库**(Docker 起一个),SQLite 测试只用于"逻辑不依赖特定方言"的场景。

> **`:memory:` 的源码**:它走的是 [`memdb.c`](../sqlite/src/memdb.c) 的 `MemFile`(P5-15 VFS 抽象的体现)。`sqlite3_open(":memory:", &db)` 让 SQLite 用内存 VFS,所有"文件操作"实际在内存数组上——这是 VFS 抽象的威力(换个 VFS,同一套上层逻辑跑在内存里)。

### 6.3 配置存储与单文件数据

**场景**:一个应用/工具要存配置(不是简单的 key=value,而是有结构的配置),或要把"一组相关数据"打包成单文件分发(如一个 `.sqlite` 文件包含所有地图数据、所有字典词条)。

**怎么用**:用 SQLite 当"结构化配置文件"——比 JSON/XML 强的是:能查询、能索引、能事务。

```sql
-- 应用配置库
sqlite> CREATE TABLE config(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
sqlite> INSERT INTO config VALUES('theme', 'dark', datetime('now'));
sqlite> INSERT INTO config VALUES('language', 'zh-CN', datetime('now'));
sqlite> SELECT value FROM config WHERE key='theme';
dark

-- 应用启动时读配置
sqlite> .schema config
```

**注意点**:

- **比 JSON 强**:JSON 要全读进内存再解析,SQLite 按需读(B-tree 点查)。配置项多(上千)时,SQLite 明显更省内存。
- **事务保证**:改多个配置项用事务包起来,要么全成要么全不成(JSON 没有,改一半 crash 就坏了)。
- **单文件分发**:`.sqlite` 文件可以单独分发、拷贝、版本控制。比一堆散文件好管理。**但注意**:分发前 `PRAGMA wal_checkpoint(TRUNCATE)` 合并 WAL(否则接收方缺 `-wal`/`-shm` 文件数据不全),或用 rollback journal 模式生成。
- **`application_id` 标识**:`PRAGMA application_id = <你的ID>;`,让 `file` 命令能识别"这是我的库"(写入 db 头前 68 字节)。

### 6.4 嵌入式设备:固件 / 工业 / IoT

**场景**:路由器、智能音箱、工业控制器、传感器网关——设备资源有限(内存几 MB、存储几十 MB),需要一个数据库但不能跑 MySQL。

**怎么用**:SQLite 的可裁剪性在这里发挥到极致。

- **裁剪编译**:用 `SQLITE_OMIT_*` 关掉不需要的特性(FTS/RTREE/JSON/窗口函数),库体积从 1.5MB 降到几百 KB。
- **选内存分配器**:资源极紧张用 `mem1`(直接调系统 malloc,无统计开销);要可靠性用 `mem2`(debug 越界检测);高性能用 `mem5`(buddy,承《内存分配器》)。
- **调小缓存**:`PRAGMA cache_size=-500;`(500KB),省内存。
- **关 fsync 或用 NORMAL**:`PRAGMA synchronous=NORMAL;`(WAL 下安全且快);极端资源场景可考虑 `OFF`(但要接受 crash 丢数据)。

**注意点**:

- **掉电恢复**:嵌入式设备常掉电,SQLite 的 ACID 在这里至关重要。rollback journal 模式(默认)或 WAL 都保证已提交事务不丢。**但**:掉电时正在 fsync 的数据可能损坏扇区,建议配合硬件(掉电保护电容)或用 `PRAGMA fullfsync=1;`(macOS/iOS,见第三节)。
- **Flash 寿命**:嵌入式常用 Flash(写入次数有限)。SQLite 频繁写会损耗 Flash——**用 WAL + 合理 checkpoint 间隔**,减少小写入;或用 `auto_vacuum=incremental` 减少写放大。极端场景考虑 os_kv VFS(把 SQLite 存到 KV 后端,可能用更友好的 Flash 算法)。
- **单进程**:嵌入式设备通常单进程,文件锁不是问题(不像服务器多连接)。可考虑 `locking_mode=exclusive;` 省去反复加锁开销。
- **真实例子**:飞机航电系统(SQLite 最初就是为军舰开发的)、汽车娱乐系统、智能家电、卫星——很多都用 SQLite 存运行数据。

> **承接**:嵌入式场景是 SQLite 的"主场"(P0-01 讲过它诞生于军舰系统)。这里的注意点都是"资源受限 + 高可靠"双重约束下的取舍,和端侧 App(资源相对宽裕)不同。**SQLite 的可裁剪性(mem0-5/mutex/OMIT 选项)就是为这个场景设计的**(承《内存分配器》的可切换分配器思想)。

### 6.5 通用排错清单

不管哪个场景,遇到 SQLite 问题,按这个顺序排查:

1. **`PRAGMA integrity_check;`**——库坏没坏?返回 `ok` 排除损坏。
2. **`EXPLAIN QUERY PLAN <慢SQL>;`**——是不是全表扫?看 `SCAN` 还是 `SEARCH`。
3. **`.timer on`**——哪条 SQL 慢?对比优化前后。
4. **`PRAGMA journal_mode;`**——是不是没开 WAL(读写并发差)?
5. **`.scanstats on`(3.32+)**——opcode 级别看实际循环次数(比 EXPLAIN 更深)。
6. **`PRAGMA lock_status;`**——是不是锁等待(读写互相阻塞)?
7. **`.dbinfo`**——页大小/页数对不对?(大库该调大 page_size 或 cache_size)
8. **看 `-wal`/`-shm`/`-journal` 文件**——WAL 模式下它们在不在?大小是否异常暴涨(checkpoint 没跟上)?

> **最常见的三类问题**:① "慢"——多半是缺索引(SCAN)或没开 WAL(写阻塞读);② "锁错误 `database is locked`"——多半是多连接写争抢(WAL 下仍是单写者),或忘了 `COMMIT`/`CLOSE`;③ "库损坏"——多半是异常断电 + 文件系统不可靠(NFS/Flash),用 `.dump` 救数据 + 备份恢复。

---

## 七、附录小结:从"懂原理"到"能动手"

这份附录把全书 21 章的原理,**落到了一套可操作的工具链和场景实践上**:

- **sqlite3 CLI**(第一节):起库、看 schema、导出导入、计时——日常 90% 操作的入口。
- **★EXPLAIN / EQP**(第二节):看 opcode 流、看查询计划——"亲眼看 SQLite 内部"的最直接工具,呼应正文 P2-06/P2-07/P3-10。
- **PRAGMA 速查表**(第三节):80+ 个开关,每条标对应正文章——"想动手验证某个机制"时的查阅表。
- **性能调优**(第四节):WAL/索引/prepared/事务批处理——把 P3-10/P4-13/P6-20 收束成可操作清单。
- **★与前作承接实验**(第五节):Lua/MySQL/LevelDB/PG 的对照实验——把四重承接落到"亲手对照",从"读懂"到"融会贯通"。
- **常见场景**(第六节):端侧/测试/配置/嵌入式 + 排错清单——落地到真实工程的注意点。

> **钉死一句话**:**正文给你"为什么这么设计",附录 A 给你"去哪个源码文件看怎么写的",附录 B 给你"在自己的终端上看到它真的这么跑"**。三者配合,你才算真的"吃透"了 SQLite——不光知道 VDBE、B-tree、WAL 怎么工作,还能亲手 `EXPLAIN` 看到 opcode、亲手 `PRAGMA journal_mode=WAL` 切换模式、亲手用 `dbstat` 看到 B-tree 叶子页存数据。这是从"相信书上是这么写的"到"我验证过它就是这么跑的"的最后一步。

> **承接说明**:本附录是全书的实践篇,把正文 21 章的承接关系(Lua/MySQL/LevelDB/PG)落到"动手对照"。EXPLAIN 看 opcode 承接《Lua》VM(`luac` 看字节码)、`dbstat` 看 B-tree 承接《MySQL·InnoDB》B+树、嵌入式对照承接《LevelDB》LSM、C/S 对照承接《PG/MySQL》。读源码(附录 A)+ 跑工具(附录 B),是理解 SQLite 的两条腿。全书到此结束——合上书,你该能在脑子里放映出一条 SELECT 在 SQLite 里的全过程,并且亲手验证它的每一步。

