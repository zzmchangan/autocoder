# 第 3 篇 · 第 10 章 · 索引与查询:怎么用 B-tree 加速

> **核心问题**:P3-08 讲清楚了 SQLite 用 B-tree(不是 B+树)存一张表,表的 key 是 rowid、数据(整行)挂在 rowid 上;P3-09 讲清楚了挂在 rowid 上的那一"行"在页里长什么样(record 变长格式 + type affinity)。但你写 SQL 的时候,几乎从不用 rowid 去查——你查的是 `WHERE name='张三'`、`WHERE age>18 AND city='北京'`。这些**非 rowid 列**怎么加速?这就是索引(index)的全部意义:**再种一棵 B-tree,把要查的列当 key,把 rowid 当 value**——查到 key 就拿到 rowid,再回表取整行。可这"再种一棵树"背后有一连串硬问题:索引 B-tree 和表 B-tree 在源码层面到底是不是同一套代码?(是。)为什么表用 `BTREE_INTKEY`、索引用 `BTREE_BLOBKEY`?覆盖索引(covering index)凭什么能省掉一次回表?复合索引 `(a,b,c)` 的"最左前缀"原则是规则还是算法后果?`WITHOUT ROWID` 表的主键索引为什么是"聚簇"的、它和 InnoDB 的聚簇索引什么关系?最关键的——**每加一个索引,写一次数据要同步维护几棵 B-tree**(写放大从哪来),这个代价凭什么值得?这一章把第 3 篇收尾,讲清"索引怎么用 B-tree 加速",为第 4 篇的 pager/WAL 把"存储"这半本接上。

> **读完本章你会明白**:
> 1. **索引也是 B-tree**,但它是 `BTREE_BLOBKEY`(key 即内容、无 data),而表是 `BTREE_INTKEY`(key 是 64 位整数 rowid、data 是整行)——同一套 `sqlite3BtreeInsert`/`sqlite3BtreeCreateTable` 代码,靠 P3 flag 切换两种形态。`sqlite3CreateIndex`(build.c:3960)负责建索引。
> 2. **回表(lookup)是两次 B-tree 查找**:先在索引 B-tree 按 `name` 定位拿到 rowid,再用 rowid 回到表 B-tree `SeekRowid` 取整行。`OP_SeekRowid`(vdbe.c:5530)就是回表那一下。
> 3. **覆盖索引**(covering index)省的是回表——索引里直接含要查的列,`WHERE_IDX_ONLY` 标志(where.c:3820)一立,codegen 就不发 `SeekRowid`,直接从索引游标 `Column` 取列,省掉一整次 B-tree 查找。性能差距常常是一个数量级。
> 4. **复合索引最左前缀**不是规则,是 B-tree 按 key 前缀排序的**几何后果**——`(a,b,c)` 索引的 B-tree 里,记录按 a→b→c 排好序,所以能服务 `a=?`(在 a 上二分)、`a=? AND b=?`(在 a,b 上二分),但**不能跳过 a**去查 `b=?`(b 在 a 内部无全局序)。
> 5. **WITHOUT ROWID 表把主键做成聚簇 B-tree**——`convertToWithoutRowidTable`(build.c:2373)把表的 `OP_CreateBtree` 的 P3 从 `BTREE_INTKEY` 改成 `BTREE_BLOBKEY`,整张表的主键索引**就是数据本身**,不用回表。这和 InnoDB 的聚簇索引是同一个思想,SQLite 用一句"主键即 B-tree key"做掉。
> 6. **索引维护代价**:一次 insert/update/delete,表 B-tree 要改一遍,**每个相关索引 B-tree 都要同步改一遍**——`sqlite3RowInsert`(insert.c)循环对每个索引发 `OP_IdxInsert`,delete.c:929 发 `OP_IdxDelete`。索引越多、写越慢,这是索引换查询速度的**写放大**根本。

> **逃生阀(这章很长,一读觉得晕,先记住这五件事)**:
> ① 索引就是一棵**额外的 B-tree**,key 是索引列编码成的 record、value 是 rowid;② 查到 rowid 后**回表**取整行,这叫 lookup,是第二次 B-tree 查找;③ 覆盖索引把要查的列塞进索引,省掉回表;④ 复合索引最左前缀是 B-tree 排序的几何后果,不是规则;⑤ `WITHOUT ROWID` 把主键做成聚簇 B-tree(像 InnoDB),不用回表。代价是每多一个索引,写数据就多改一棵 B-tree。记住这五点,后面每一节都是在展开它们。

> **承接提示**:这一章讲**索引的存储侧**——索引 B-tree 怎么存、怎么用、怎么维护。索引**选择**(查询规划器为什么挑这个索引不挑那个、`WhereLoop` 代价模型怎么算)在 **P2-07《一条 SELECT 怎么执行》**已经讲透了(System-R 风格动态规划、`(rCost,nRow,rUnsort)` 三元代价、LogEst 对数存储),本章**只指路、不重讲**。本章末尾的"想继续深入"也会指向 P2-07。

---

## 〇、一句话点破

> **索引是 SQLite 给你开的一个后门——你在哪一列上查询慢,就在哪一列种一棵 B-tree,把那列当 key、rowid 当 value,查询时先在这棵索引树里二分定位、拿到 rowid 再回表取整行。索引 B-tree 和表 B-tree 在源码里是同一套代码,靠 `BTREE_INTKEY`(表)/`BTREE_BLOBKEY`(索引)这一个 flag 区分;覆盖索引省的是回表,WITHOUT ROWID 聚簇省的也是回表,复合索引最左前缀是 B-tree 排序的几何后果,索引维护代价是写放大——一句话,索引的全部技巧都围绕"rowid 这把回表的钥匙"展开。**

这是结论,不是理由。本章倒过来拆:先讲索引为什么要额外种一棵 B-tree、这棵树和表树什么关系;再讲回表这个动作(两次 B-tree 查找);然后讲覆盖索引凭什么省回表、复合索引最左前缀的几何来源;接着拆 WITHOUT ROWID 聚簇 B-tree 和 InnoDB 的对照;再讲索引维护的写放大代价;最后是技巧精解,把"索引 B-tree 的 key 到底长什么样"和"覆盖索引 vs 非覆盖的 EXPLAIN 对照"两个最硬的点拆透。

---

## 一、为什么要再种一棵 B-tree:索引的本质动机

### 提出问题:没有索引会怎样

假设你有一张 `users(id, name, age, city)` 表,存了 1000 万行。你执行:

```sql
SELECT name FROM users WHERE age = 25;
```

没有索引时,SQLite 怎么找 `age=25` 的行?**只有一条路:从头到尾把表 B-tree 扫一遍**。在 VDBE 的 opcode 流里,这条 SQL 会被编译成(用 `EXPLAIN` 能看到):

```
   addr  opcode      p1    p2    p3    注释
   ----  ----------  ----  ----  ----  -------------------------
   0     OpenRead    0     2     0     打开表 users(根页 2)
   1     Rewind      0     9     0     游标定位到表的第一行(末尾跳 9)
   2     Column      0     2     1     取第 2 列(age)放进寄存器 1
   3     Ne          1     8     2     寄存器1 不等于寄存器2(=25)就跳 8
   4     Column      0     1     3     取第 1 列(name)放进寄存器 3
   5     ResultRow   3     1     0     把寄存器 3 作为结果行返回
   6     ...
   8     Next        0     2     0     游标前进到下一行,跳回 2
   9     Close       0     0     0
```

注意 `Rewind → Column → Ne(跳过) → ... → Next` 这个循环——它**把 1000 万行全过一遍**,每一行都 `Column` 取出 age、和 25 比较、不匹配就跳过。如果 `age=25` 的行只有 100 行,你为了找这 100 行,**读了 1000 万次的 age 列、走了 1000 万次的 Next**。这就是**全表扫(full table scan)**,复杂度 `O(N)`,N 是表的行数。

> **不这样会怎样**:全表扫的代价是线性的。表越大越慢——1000 万行的表,哪怕你只查一行,也得把这 1000 万行全读一遍。如果每次查询都这样,SQLite 在稍大数据量上就根本不可用。这就是索引要解决的问题:**让"按某列查找"从 `O(N)` 降成 `O(log N)`**。

### 所以这样设计:再种一棵 B-tree,把要查的列当 key

索引的核心想法极其朴素:**既然表 B-tree 是按 rowid 排序的(所以按 rowid 查是 `O(log N)`),那我再种一棵 B-tree,按 `age` 排序,不就能按 age 也 `O(log N)` 查了吗?** 这棵按 age 排序的 B-tree,就是 age 列上的索引。

```
   表 B-tree(key=rowid, data=整行):
   ┌──────────────────────────────────────────┐
   │  rowid=1 │ (1,'张三',30,'上海')           │
   │  rowid=2 │ (2,'李四',25,'北京')           │
   │  rowid=3 │ (3,'王五',25,'广州')           │
   │  rowid=4 │ (4,'赵六',40,'深圳')           │
   │  ...                                     │
   └──────────────────────────────────────────┘
   按 rowid 排序,所以 rowid 查询是 O(log N)。

   age 列上的索引 B-tree(key=age 编码, value=rowid):
   ┌──────────────────────────────────────────┐
   │  age=25  → rowid=2                       │
   │  age=25  → rowid=3                       │
   │  age=30  → rowid=1                       │
   │  age=40  → rowid=4                       │
   │  ...                                     │
   └──────────────────────────────────────────┘
   按 age 排序,所以按 age 查询也是 O(log N)。
```

现在你执行 `SELECT name FROM users WHERE age = 25`,SQLite 的查询规划器(在 P2-07 讲过的 `wherePathSolver`)看到 age 上有索引,就会**改用索引扫**:先在索引 B-tree 里 `SeekGE age=25`(`O(log N)` 定位),然后顺着索引往后扫(`IdxGE` 边扫边判断 key 还是不是 25),每扫到一个,拿到它携带的 rowid,**再用这个 rowid 回到表 B-tree 去取整行**(`SeekRowid`,又是 `O(log N)`)。

这就是索引加速的本质:**用一棵额外 B-tree 的 `O(log N)` 定位,换掉全表扫的 `O(N)` 线性扫描**。

> **所以这样设计**:索引不是魔法,它就是"换一个排序键再种一棵 B-tree"。B-tree 的查询效率来自"按 key 排序后能二分定位",这个性质对任何 key 都成立——你想要按哪列快查,就把那列当 key 种一棵树。一张表可以有任意多棵索引 B-tree(每列一棵、甚至多列复合一棵),它们和表 B-tree 共存于同一个 .db 文件里,**用根页号区分**(P3-08 讲过的"单文件多 B-tree")。

### 源码佐证:索引在 schema 里怎么记

你 `CREATE INDEX idx_age ON users(age)`,SQLite 干的第一件事,是把这条索引作为一个 schema 条目写进 `sqlite_master`(SQLite 里叫 `sqlite_schema`)表。这一切发生在 [`sqlite3CreateIndex`](../sqlite/src/build.c#L3960-L3972) 这个函数里:

```c
void sqlite3CreateIndex(
  Parse *pParse,     /* All information about this parse */
  Token *pName1,     /* First part of index name. May be NULL */
  Token *pName2,     /* Second part of index name. May be NULL */
  SrcList *pTblName, /* Table to index. Use pParse->pNewTable if 0 */
  ExprList *pList,   /* A list of columns to be indexed */
  int onError,       /* OE_Abort, OE_Ignore, OE_Replace, or OE_None */
  Token *pStart,     /* The CREATE token that begins this statement */
  Expr *pPIWhere,    /* WHERE clause for partial indices */
  int sortOrder,     /* Sort order of primary key when pList==NULL */
  int ifNotExist,    /* Omit error if index already exists */
  u8 idxType         /* The index type */
){
```

注意几个参数:`pList` 是要索引的列(`ExprList`)、`pPIWhere` 是 partial index 的 WHERE 限定(后面讲)、`onError` 决定是不是 UNIQUE 索引(`OE_Abort` 表示唯一约束冲突时回滚)、`idxType` 区分普通索引/UNIQUE/PRIMARY KEY。函数末尾,SQLite 把这条索引的 schema 记录用一条 `INSERT INTO sqlite_schema ...` 写进去:

```c
      /* Add an entry in sqlite_schema for this index
      */
      sqlite3NestedParse(pParse,
         "INSERT INTO %Q." LEGACY_SCHEMA_TABLE " VALUES('index',%Q,%Q,#%d,%Q);",
         db->aDb[iDb].zDbSName,
         pIndex->zName,
         pTab->zName,
         iMem,
         zStmt
      );
```

(见 [`build.c`](../sqlite/src/build.c#L4477-L4486))这条 `INSERT` 的五个字段对应 `sqlite_schema(type, name, tbl_name, rootpage, sql)`:`type='index'`、`name` 是索引名、`tbl_name` 是它索引的表、`rootpage` 是这棵索引 B-tree 的根页号、`sql` 是原始的 `CREATE INDEX` 文本。**下次打开这个 .db 文件,SQLite 读 `sqlite_schema` 就能重建所有索引的元信息**,包括每棵索引 B-tree 的根页号——这就是为什么"单文件多 B-tree"能持久化:每棵树的根页号都记在 schema 里。

> **钉死这件事**:索引在物理上就是一棵额外的 B-tree,和表 B-tree 平起平坐地住在同一个 .db 文件里。它在 schema 里有一条记录(type='index'),记录了自己的根页号。你 `CREATE INDEX` 的那一刻,SQLite 干了三件事:① 在 schema 里插一条记录、② 用 `sqlite3BtreeCreateTable` 分配一个新根页(给索引 B-tree 用)、③ 把表里现有数据按索引列排序填进这棵新树(`sqlite3RefillIndex`)。从此这棵树就和表树同步维护。

---

## 二、索引 B-tree 和表 B-tree 是同一套代码:`BTREE_INTKEY` vs `BTREE_BLOBKEY`

### 提出问题:表和索引都用 B-tree,它们长得一样吗?

这是初学 SQLite 最容易困惑的点。表 B-tree 的 key 是 rowid(整数),data 是整行 record;索引 B-tree 的 key 是索引列编码成的 record,**value 是什么**?如果是 rowid,那 rowid 放哪?是和 key 一起,还是单独存?

答案藏在 SQLite B-tree 模块对"表"和"索引"的**二分定义**里。打开 [`btree.h`](../sqlite/src/btree.h#L112-L123),这段注释白纸黑字:

```c
/* The flags parameter to sqlite3BtreeCreateTable can be the bitwise OR
** of the flags shown below.
**
** Every SQLite table must have either BTREE_INTKEY or BTREE_BLOBKEY set.
** With BTREE_INTKEY, the table key is a 64-bit integer and arbitrary data
** is stored in the leaves.  (BTREE_INTKEY is used for SQL tables.)  With
** BTREE_BLOBKEY, the key is an arbitrary BLOB and no content is stored
** anywhere - the key is the content.  (BTREE_BLOBKEY is used for SQL
** indices.)
*/
#define BTREE_INTKEY     1    /* Table has only 64-bit signed integer keys */
#define BTREE_BLOBKEY    2    /* Table has keys only - no data */
```

这段注释信息量极大,逐句拆:

- **`BTREE_INTKEY`(表用的)**:key 是 64 位有符号整数(就是 rowid),**任意数据(data)存在叶子页里**。注释明说"This is used for SQL tables"——表 B-tree,key 是 rowid、data 是整行 record。
- **`BTREE_BLOBKEY`(索引用的)**:key 是任意 BLOB,**不存任何 data——key 就是内容本身**。注释明说"This is used for SQL indices"——索引 B-tree,key 是索引列编码的 record,**没有 data**。

> **钉死这件事**:这是理解 SQLite 索引最关键的一句——**索引 B-tree 的 key 即内容、无 data**。那 rowid 怎么办?**rowid 编进 key 里**。具体说,索引 record 的格式是"索引列的值 + rowid",作为一个整体当 key。所以索引 B-tree 里每一项的 key 是 `(age=25, rowid=2)`、`(age=25, rowid=3)`、`(age=30, rowid=1)`……排序时先按 age、age 相同再按 rowid。你查 `age=25`,二分定位到 `(25,2)` 这一项,**这一项的 key 里就含 rowid=2**,SQLite 从 key 里把 rowid 抠出来,拿去回表。

### 不这样会怎样:为什么索引把 rowid 编进 key

如果索引 B-tree 单独有个 data 字段存 rowid(像 InnoDB 二级索引那样 data 存主键),会怎样?也能工作,但 SQLite 不这么干,原因是**紧凑**。SQLite 的设计哲学是嵌入式、单文件、页要小(默认 4KB),一个页能塞越多记录越好。把 rowid 编进 key、不单独留 data 字段,索引页的每条 cell 就只有"一个变长 key",格式极其规整(和表 record 的"key+payload"二段式相比,索引是纯 key 一段式)。

而且这个设计有一个**意外的好处**:既然索引 B-tree 的 key 末尾总是 rowid,那么**索引天然按 rowid 排了第二序**。当你查 `age=25` 有多行时,它们在索引里已经按 rowid 排好了——回表时按 rowid 顺序访问表 B-tree,**局部性更好**(rowid 相近的行大概率在同一个或相邻页,缓存命中率高)。这是 SQLite 把 rowid 编进 key 的隐藏红利。

> **承接《MySQL·InnoDB》**:InnoDB 的二级索引 data 字段存的是**主键值**(不是 rowid,因为 InnoDB 主键可能是复合的),回表时拿主键去聚簇索引找。SQLite 因为有 rowid 这个整数主键(或 INTEGER PRIMARY KEY 别名),直接把 rowid 编进索引 key。两者思想同源(二级索引指向主键),实现细节因主键模型不同而异。

### 源码佐证:表和索引共用 `sqlite3BtreeInsert`

更妙的来了——表 B-tree 和索引 B-tree,**插入用的是同一个函数** [`sqlite3BtreeInsert`](../sqlite/src/btree.c#L9434)。这个函数不关心你是表还是索引,它只看游标(`BtCursor`)上挂的 `pKeyInfo`:

- 表的游标 `pKeyInfo == NULL`(因为 key 是整数,整数比较不用 collation);
- 索引的游标 `pKeyInfo != NULL`(因为 key 是 record,record 比较要按列的 collation,比如 TEXT 列要按 BINARY 或 NOCASE 排序)。

btree.c 里有一行关键的断言,把这件事钉死了:

```c
  assert( (flags & BTREE_PREFORMAT) || (pX->pKey==0)==(pCur->pKeyInfo==0) );
```

(见 [`btree.c`](../sqlite/src/btree.c#L9497))意思是:要么走 PREFORMAT 路径(预格式化的插入,优化用),否则"有 pKey 等价于有 pKeyInfo"——也就是**索引插入带 pKey(变长 record)+ pKeyInfo(比较规则),表插入两者都为空(用整数 key)**。同一个 `sqlite3BtreeInsert`,通过这个二分,既插表也插索引。

> **所以这样设计**:SQLite 的 B-tree 引擎被设计成"key 比较规则可插拔"——表用整数 key(隐含的整数比较)、索引用 record key(带 collation 的逐列比较)。一套 B-tree 代码、两种用法,靠 `pKeyInfo` 这个指针的有无切换。这是 SQLite 极致复用代码的典范:没有"TableBtree"和"IndexBtree"两个类,只有一棵 B-tree,你给它什么 key 比较规则,它就按什么排序。

---

## 三、回表(lookup):索引到整行的两次 B-tree 查找

### 提出问题:索引拿到 rowid 之后呢?

回到 `SELECT name FROM users WHERE age = 25`。走索引后,SQLite 在索引 B-tree 里定位到 `(25, rowid=2)`、`(25, rowid=3)`……但你要的 `name` 列在**表**里,不在索引里(索引只索引了 age)。怎么办?**拿 rowid 回表 B-tree 去取整行**。

这个动作在 VDBE 里是 `OP_SeekRowid`:

```c
case OP_SeekRowid: {        /* jump0, in3, ncycle */
```

(见 [`vdbe.c`](../sqlite/src/vdbe.c#L5530))`SeekRowid` 的语义是:**在表游标(P1)里,按 P3 寄存器里的 rowid 值,二分定位到那一行**。这就是"回表"——索引给了一个 rowid,`SeekRowid` 拿这个 rowid 回到表 B-tree,`O(log N)` 找到那一整行,然后 `Column` 才能从表游标取出 name 列。

完整的"索引扫 + 回表"opcode 流长这样:

```
   addr  opcode      p1    p2    p3    注释
   ----  ----------  ----  ----  ----  -------------------------
   0     OpenRead    0     2     0     打开表 users(根页 2)
   1     OpenRead    1     5     0     打开索引 idx_age(根页 5)
   2     Integer     25    2     0     把常量 25 放进寄存器 2
   3     SeekGE      1     5     2     在索引游标(1)里 SeekGE 寄存器2(=25)
   4     IdxGT       1     8     2     索引游标当前 key 的 age 部分 > 25?跳 8
   5     IdxRowid    1     3     0     从索引游标取出当前项的 rowid → 寄存器3
   6     SeekRowid   0     8     3     用寄存器3 的 rowid 回表游标(0)定位
   7     Column      0     1     4     从表游标取第1列(name)→ 寄存器4
   8     ResultRow   4     1     0     返回结果行
   9     Next        1     4     0     索引游标前进,跳回 4
   10    Close       0     0     0
```

注意这条流的精妙之处:

1. **两个游标**:P1=0 是表游标、P1=1 是索引游标。`SeekGE`/`IdxGT`/`IdxRowid`/`Next` 都作用在索引游标(1)上,只有 `SeekRowid` 和 `Column` 作用在表游标(0)上。
2. **`SeekGE`(addr 3)`:在索引里二分定位到第一个 `age>=25` 的项,`O(log N)`。
3. **`IdxGT`(addr 4)`:取出当前索引项的 age 部分,如果 >25 就说明扫出界了(因为我们要的是 age=25,age 升序,>25 就该停),跳到 8 结束。
4. **`IdxRowid`(addr 5)`:从当前索引项里抠出 rowid,放进寄存器 3。**这一步揭示了"索引 key 含 rowid"——rowid 就在 key 里,`IdxRowid` 把它取出来**。
5. **`SeekRowid`(addr 6)`:这就是回表!拿寄存器 3 的 rowid,去表游标(0)里二分定位整行,`O(log N)`。
6. **`Column`(addr 7)`:从表游标取 name 列。**注意是从表游标取,不是从索引游标——因为 name 不在索引里**。
7. **`Next`(addr 9)`:索引游标前进一格,跳回 4 继续判断下一个索引项。

> **钉死这件事**:非覆盖索引的查询,对每一行结果都做了**两次 B-tree 查找**——一次在索引里(SeekGE/Next)、一次在表里(SeekRowid)。这就是"回表"的全部代价。如果 age=25 有 100 行,你就做了 100 次独立的 `SeekRowid`(每次 `O(log N)`)。**这就是为什么覆盖索引(下一节)那么重要——它能省掉这 100 次回表**。

### 不这样会怎样:为什么不在索引里存整行

你可能想:索引 B-tree 干脆把整行也存进去(像表那样),不就不用回表了吗?能,但**代价是索引变得和表一样大**。你每建一个索引,就多存一份整行数据,空间爆炸。更糟的是,**每次 update 任何一列,所有索引都得跟着改整行**(因为整行都冗余存了),写放大更恐怖。

SQLite 的取舍是:**索引只存索引列 + rowid,保持索引瘦小**(一个 age 索引,每条记录就 age(几字节) + rowid(8 字节),十几字节;对比整行动辄上百字节)。默认情况下需要回表(一次 `O(log N)`),但绝大多数查询这样已经够快了。**当你确实需要省掉回表,SQLite 给你一个开关:覆盖索引**(下一节)。

> **承接《LevelDB》**:这种"索引指向主数据"的二层结构,LevelDB 那本的 SSTable 索引块也是同一种思想——索引块存 key 和 offset(指向数据块的位置),不存数据本身。SQLite 的索引指向 rowid(指向表里的行),LevelDB 的索引指向 offset(指向数据块)。都是"瘦索引指向胖数据"的复用。

---

## 四、覆盖索引:省掉回表的杀手锏

### 提出问题:如果索引里已经含要查的列呢?

看这条查询:

```sql
SELECT age FROM users WHERE age = 25;
```

你要查的是 age,WHERE 条件也是 age。age 索引里**已经有 age**(它是 key)。那还需要回表吗?**不需要**——age 就在索引项里,`Column` 直接从索引游标取就行。这就是**覆盖索引(covering index)**:索引的列**覆盖**了查询要的所有列,不用回表。

再看一条:

```sql
SELECT id, age FROM users WHERE age = 25;
```

id 是 rowid(INTEGER PRIMARY KEY 时 id 就是 rowid 的别名),age 在索引里——**两个要查的列都在索引里**(rowid 是索引 key 的固有部分,age 是索引列),所以这条也是覆盖索引,不用回表。

### 源码佐证:`isCovering` 标志和 `WHERE_IDX_ONLY`

SQLite 怎么知道一个索引是不是覆盖的?在 [`sqlite3CreateIndex`](../sqlite/src/build.c#L4319-L4332) 里,建索引时会检查这个索引是否包含表的所有列:

```c
  /* If this index contains every column of its table, then mark
  ** it as a covering index */
  assert( HasRowid(pTab)
      || pTab->iPKey<0 || sqlite3TableColumnToIndex(pIndex, pTab->iPKey)>=0 );
  recomputeColumnsNotIndexed(pIndex);
  if( pTblName!=0 && pIndex->nColumn>=pTab->nCol ){
    pIndex->isCovering = 1;
    for(j=0; j<pTab->nCol; j++){
      if( j==pTab->iPKey ) continue;
      if( sqlite3TableColumnToIndex(pIndex,j)>=0 ) continue;
      pIndex->isCovering = 0;
      break;
    }
  }
```

这是"索引包含全表所有列"的极端情况(`isCovering=1`)。但更常见的是**部分覆盖**——查询只要几列,这几列恰好都在索引里。这种判断在查询规划阶段做,看的是 `WHERE_IDX_ONLY` 标志。打开 [`where.c`](../sqlite/src/where.c#L3810-L3830):

```c
** pIdx is an index that covers all of the low-number columns used by
** pWInfo->pSelect (columns from 0 through 62) or an index that has
** expressions terms.  Hence, we cannot determine whether or not it is
** a covering index by using the colUsed bitmasks.  We have to do a search
** to see if the index is covering.  This routine does that search.
**
** The return value is one of these:
**
**      0                The index is definitely not a covering index
**
**      WHERE_IDX_ONLY   The index is definitely a covering index
```

查询规划器(P2-07 讲的 `wherePathSolver`)为每个候选索引算一遍:这条 SELECT 用到的所有列,是不是都在这个索引里?如果是,就给这个 WhereLoop 打上 `WHERE_IDX_ONLY` 标志。codegen 看到 `WHERE_IDX_ONLY`,**就不发 `SeekRowid` 回表**,直接从索引游标取所有列。

### 不这样会怎样:覆盖索引 vs 非覆盖的 EXPLAIN 对照

这是最能说明问题的一组对比。建一张表:

```sql
CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT, age INT, city TEXT);
CREATE INDEX idx_age ON users(age);
CREATE INDEX idx_age_city ON users(age, city);   -- 复合索引,覆盖 age+city
```

**查询 1(非覆盖,只要 age 也要 name)**:

```sql
EXPLAIN SELECT name FROM users WHERE age = 25;
-- 走 idx_age,但 name 不在索引里,要回表
```

opcode 流里会有 `SeekRowid`(回表)+ `Column`(从表取 name)。

**查询 2(覆盖,只要 age)**:

```sql
EXPLAIN SELECT age FROM users WHERE age = 25;
-- 走 idx_age,age 在索引里,不回表
```

opcode 流里**没有 SeekRowid**,`Column` 直接从索引游标取 age。

**查询 3(覆盖,复合索引 idx_age_city)**:

```sql
EXPLAIN SELECT city FROM users WHERE age = 25;
-- 走 idx_age_city,age 和 city 都在索引里,不回表
```

同样没有 `SeekRowid`。**注意查询 3 比"在 idx_age 上查 name"快得多**——不是因为查询 3 用了更好的索引选择算法,而是因为 idx_age_city 这棵索引物理上包含了 city 列,省掉了回表。这就是 DBA 常说的"为高频查询设计覆盖索引"。

> **钉死这件事**:覆盖索引省的是**整个回表动作**——一次 B-tree 查找。对于"查出 1000 行、每行回表一次"的查询,覆盖索引把这 1000 次 `SeekRowid` 全省了。性能差距常常是一个数量级(回表的随机 IO 是数据库最贵的操作之一)。设计索引时,**把高频查询要 SELECT 的列、加上 WHERE 的列,一起塞进一个复合索引**,让它变成覆盖索引,是 SQLite 性能调优的头号手段。

### 反例:覆盖索引不是免费的

但覆盖索引也有代价——**索引变胖了**。idx_age 每条记录十几字节,加了 city 的 idx_age_city 每条记录多几十字节(city 是 TEXT)。索引页能塞的记录变少,树可能变高一点、缓存命中率降一点。而且 **update city 时,idx_age_city 也要跟着改**(city 是它的一部分),而 idx_age 不用(city 不在它里面)。所以覆盖索引是**用索引变胖 + 写放大,换查询省回表**——对读多写少的列值得,对频繁写的列要权衡。

---

## 五、复合索引与最左前缀:B-tree 排序的几何后果

### 提出问题:复合索引 `(a,b,c)` 能服务哪些查询?

你建了 `CREATE INDEX idx_abc ON t(a, b, c)`。这棵索引 B-tree 的 key 是 `(a, b, c)` 三元组,按 a→b→c 的字典序排好。那么以下查询哪些能用上这棵索引?

| 查询 | 能用 idx_abc 吗? | 为什么 |
|------|------------------|--------|
| `WHERE a = 1` | **能** | a 是最左列,在 a 上有全局序,二分定位 |
| `WHERE a = 1 AND b = 2` | **能** | a 定位后,b 在 a=1 的范围内有序 |
| `WHERE a = 1 AND b = 2 AND c = 3` | **能** | 三列全等值,精确二分 |
| `WHERE b = 2` | **不能** | b 不是最左列,在 b 上无全局序 |
| `WHERE c = 3` | **不能** | c 最右,无全局序 |
| `WHERE a = 1 AND c = 3` | **部分能** | 能用 a 定位,但 c 在 b 跳过后无序(c 要靠逐行过滤) |
| `WHERE a > 1 AND b = 2` | **部分能** | a 是范围,b 在 a>1 的范围里无序 |

这就是著名的**最左前缀原则(leftmost prefix rule)**:复合索引能服务的查询,必须从最左列开始、连续地用等值(或范围)谓词,**一旦中间断一列或换成范围,后面的列就用不上索引的二分了**。

### 这不是规则,是 B-tree 排序的几何后果

很多教程把最左前缀讲成"规则",让人死记硬背。其实它根本不是规则,是 **B-tree 按 key 字典序排序**这件事的**几何后果**。看这个示意:

```
   idx_abc 的 B-tree 里,key 按字典序排好的样子:

   (1,1,1) (1,1,2) (1,2,1) (1,2,3) (1,5,2) (2,1,1) (2,1,3) (2,3,1) (3,1,1) ...
    ↑                   ↑                       ↑             ↑
    a=1 区间开始        仍在 a=1 内              a=2 开始       a=3 开始

   注意:b 和 c 的值在 a 区间内是有序的,
        但跨 a 区间看 b、c,完全是乱的(1,2,1 后面 b 突然跳到 5)。
```

你 `WHERE a = 1`:在 a 这一层二分,定位到 a=1 的区间起点 `O(log N)`,然后顺序扫到 a 变 2 为止。**能用索引**。

你 `WHERE a = 1 AND b = 2`:先二分到 a=1 区间,在 a=1 这个小区间内,b 是有序的(因为字典序里 a 固定后按 b 排),所以能在 b 上再二分。**能用索引**。

你 `WHERE b = 2`(没有 a 条件):b 在全局没有序(看上图,b 的值是 1,1,2,2,5,1,1,3,1——跨 a 区间完全乱)。你没法在 b 上二分,只能全索引扫。**用不上索引**。

你 `WHERE a = 1 AND c = 3`(跳过 b):a 能用,但 c 在"a 固定、b 不固定"的情况下是乱的(看 a=1 区间内,c 是 1,2,1,3,2——因为 b 在变,c 跟着 b 内部乱序)。所以 c 用不上二分,只能 a 定位后逐行过滤 c。**部分用索引**。

你 `WHERE a > 1 AND b = 2`:a 是范围,定位到 a>1 的区间后,a 在变(2,3,4……),b 跨 a 区间无序,没法在 b 上二分。**部分用索引**。

> **钉死这件事**:最左前缀不是 SQLite 的规则,是任何按字典序排序的数据结构(B-tree、跳表、有序数组)的**几何必然**。你只要记住一件事:**复合索引的 key 是按字典序排的,你能二分的只有"连续的最左前缀"**。一旦中断(跳列、换范围),后面的列就退化成顺序扫。这个理解一旦建立,所有"能不能用索引"的判断都能现场推出来,不用背规则。

### 源码佐证:索引列匹配在 where.c 的 `aStartOp[]`

查询规划器怎么把"WHERE 谓词能匹配索引的前几列"这件事翻译成 opcode?在 P2-07 讲过的 `wherecode.c` 里,有一个 `aStartOp[]` 决策表,根据匹配的列数和谓词类型(等值/范围),选不同的起始 opcode(`SeekGE`/`SeekGT`/`SeekLE` 等)。这块的逻辑(P2-07 已拆透代价模型)本质上就是"数 WHERE 谓词能从最左列连续匹配几列",匹配几列就在那几列上二分。

本章不重复 P2-07 的代价模型细节,**只钉死一件事**:最左前缀的"能匹配几列"不是查询规划器的发明,是 B-tree 字典序的几何后果在 codegen 里的直接投影——能二分几列,就在那几列上发 `SeekGE`,剩下的列靠 `IdxGT`/`IdxLE` 逐行过滤。

> **承接 P2-07**:复合索引"能不能用、用几列"的**代价计算**(每个 WhereLoop 的 `nEq`/`nLt`/`nDot` 字段、LogEst 对数代价、`wherePathSolver` 的动态规划)在 **P2-07《一条 SELECT 怎么执行》**第 5 节"查询规划器"里讲透了,本章只讲存储侧"索引 B-tree 为什么有最左前缀这个几何性质",不重讲代价模型。

---

## 六、WITHOUT ROWID:主键即聚簇 B-tree(对照 InnoDB)

### 提出问题:rowid 表的"二级索引回表"能不能彻底省掉?

到目前为止,所有索引都是"二级索引"(secondary index)——它们指向 rowid,查到 rowid 要回表。有没有办法让**表本身就是按主键排的、主键索引就是数据本身**,这样按主键查就不用回表?

这就是 `WITHOUT ROWID` 表。看这两张表的对比:

```sql
-- 普通 rowid 表(默认)
CREATE TABLE users(
  id INTEGER PRIMARY KEY,   -- INTEGER PRIMARY KEY 是 rowid 的别名
  name TEXT,
  age INT
);
-- 这张表的 B-tree 是 BTREE_INTKEY,key=id(rowid)、data=(name,age) 整行。
-- 如果你建 CREATE INDEX idx_name ON users(name),idx_name 是二级索引,
-- 查 name 要回表用 id 去找。

-- WITHOUT ROWID 表
CREATE TABLE users(
  id INTEGER PRIMARY KEY,
  name TEXT,
  age INT,
  UNIQUE(name)              -- 假设 name 唯一
) WITHOUT ROWID;
-- 这张表的 B-tree 是 BTREE_BLOBKEY,key=(id)、data=(name,age),
-- **主键索引就是数据本身**,按 id 查不用回表(它就是表)。
-- 但 idx_name 这种二级索引,指向的是 id(主键值),查 name 仍要"回表"到主键树。
```

关键区别:**WITHOUT ROWID 表的主键 B-tree 就是聚簇索引(clustered index)**——数据和主键索引合一,没有独立的"表树 + 主键索引树"两份。这和 InnoDB 的聚簇索引是**同一个思想**。

### 源码佐证:`convertToWithoutRowidTable` 把 INTKEY 改成 BLOBKEY

WITHOUT ROWID 在源码里怎么实现的?打开 [`build.c`](../sqlite/src/build.c#L2373-L2402):

```c
static void convertToWithoutRowidTable(Parse *pParse, Table *pTab){
  Index *pIdx;
  Index *pPk;
  int nPk;
  int nExtra;
  int i, j;
  sqlite3 *db = pParse->db;
  Vdbe *v = pParse->pVdbe;

  /* Mark every PRIMARY KEY column as NOT NULL (except for imposter tables)
  */
  if( !db->init.imposterTable ){
    for(i=0; i<pTab->nCol; i++){
      if( (pTab->aCol[i].colFlags & COLFLAG_PRIMKEY)!=0
       && (pTab->aCol[i].notNull==OE_None)
      ){
        pTab->aCol[i].notNull = OE_Abort;
      }
    }
    pTab->tabFlags |= TF_HasNotNull;
  }

  /* Convert the P3 operand of the OP_CreateBtree opcode from BTREE_INTKEY
  ** into BTREE_BLOBKEY.
  */
  assert( !pParse->bReturning );
  if( pParse->u1.cr.addrCrTab ){
    assert( v );
    sqlite3VdbeChangeP3(v, pParse->u1.cr.addrCrTab, BTREE_BLOBKEY);
  }
```

注意最后这几行——这是 WITHOUT ROWID 实现的**全部精髓**:`CREATE TABLE` 在 codegen 阶段先发了一条 `OP_CreateBtree`,它的 P3 参数本来是 `BTREE_INTKEY`(默认 rowid 表);`convertToWithoutRowidTable` 用 `sqlite3VdbeChangeP3` **把这条 opcode 的 P3 原地改成 `BTREE_BLOBKEY`**。就这么一行改动,这张表就从"整数 key 表"变成了"BLOB key 表",也就是**主键 record 当 key、整行当 data**——主键索引和数据合体了。

`TF_WithoutRowid` 标志定义在 [`sqliteInt.h`](../sqlite/src/sqliteInt.h#L2496):

```c
#define TF_WithoutRowid   0x00000080 /* No rowid.  PRIMARY KEY is the key */
```

注释白纸黑字:"No rowid. PRIMARY KEY is the key"——没有 rowid,主键就是 B-tree 的 key。

### 不这样会怎样:WITHOUT ROWID 什么时候该用

WITHOUT ROWID 不是银弹。它的核心红利是**按主键访问不用回表**(聚簇),代价是:

1. **主键必须是合适的 key**。如果你的主键很长(比如 TEXT UUID),整棵表 B-tree 的 key 都很长,页能塞的行变少,树变高,缓存变差。
2. **二级索引指向主键值(不是 rowid 整数)**。rowid 表的二级索引指向 8 字节整数,WITHOUT ROWID 表的二级索引指向主键值(可能几十字节),二级索引更胖。
3. **没有 rowid 这把万能钥匙**。rowid 表里,所有二级索引都指向同一个 8 字节 rowid,回表是固定的 `O(log N)` 整数查找;WITHOUT ROWID 表里,二级索引指向变长主键,回表要按变长 key 查找,略慢。

所以 WITHOUT ROWID 最适合的场景是:**主键短(整数或短字符串)、且绝大多数查询按主键访问**。比如 `CREATE TABLE kv(k INTEGER PRIMARY KEY, v BLOB) WITHOUT ROWID`——这是一个纯 KV 表,按 k 查 v 是 `O(log N)` 且不用回表,比 rowid 表的等价方案更紧凑。

> **承接《MySQL·InnoDB》**:InnoDB 的表**天生就是聚簇索引**(主键索引即数据,所有表都是"WITHOUT ROWID"的语义),二级索引指向主键值。SQLite 默认是 rowid 表(堆表 + rowid),WITHOUT ROWID 是可选的聚簇模式。**InnoDB 是"默认聚簇、可选堆表(没有)",SQLite 是"默认堆表、可选聚簇"**——两种默认值反映了不同的设计哲学:InnoDB 服务 C/S 高并发(聚簇对范围扫和主键查询友好)、SQLite 服务嵌入式(堆表对单行增删简单、rowid 通用)。本书 P3-08 已对照过两种 B-tree 模型,这里从索引角度再钉一次。

---

## 七、索引维护代价:一次写要改几棵 B-tree

### 提出问题:索引不是免费的——写数据时怎么办?

到目前为止讲的都是索引怎么**加速查询**。但索引有代价——**每次 insert/update/delete,所有相关索引都要同步维护**。这是索引换查询速度的**写放大**根本。

看一次 `INSERT INTO users(id, name, age, city) VALUES(...)`。假设 users 上有 `idx_age`、`idx_age_city` 两个索引。这次 insert 要改几棵 B-tree?

- **表 B-tree**:插入新行(新 rowid + 整行 record)。1 棵。
- **idx_age**:插入 (age, rowid)。1 棵。
- **idx_age_city**:插入 (age, city, rowid)。1 棵。

**总共 3 棵 B-tree 的插入**。如果你有 10 个索引,一次 insert 就是 11 棵 B-tree 的插入(1 表 + 10 索引)。

### 源码佐证:`sqlite3RowInsert` 循环发 `OP_IdxInsert`

这件事在 [`insert.c`](../sqlite/src/insert.c#L2825-L2855) 里看得清清楚楚。`sqlite3RowInsert`(产 insert opcode 的函数)的核心逻辑是:先对所有索引循环发 `OP_IdxInsert`,再对表发 `OP_Insert`:

```c
    pik_flags = (useSeekResult ? OPFLAG_USESEEKRESULT : 0);
    if( IsPrimaryKeyIndex(pIdx) && !HasRowid(pTab) ){
      pik_flags |= OPFLAG_NCHANGE;
      pik_flags |= (update_flags & OPFLAG_SAVEPOSITION);
      if( update_flags==0 ){
        codeWithoutRowidPreupdate(pParse, pTab, iIdxCur+i, aRegIdx[i]);
      }
    }
    sqlite3VdbeAddOp4Int(v, OP_IdxInsert, iIdxCur+i, aRegIdx[i],
                         aRegIdx[i]+1,
                         pIdx->uniqNotNull ? pIdx->nKeyCol: pIdx->nColumn);
    sqlite3VdbeChangeP5(v, pik_flags);
  }
  if( !HasRowid(pTab) ) return;
  ...
  sqlite3VdbeAddOp3(v, OP_Insert, iDataCur, aRegIdx[i], regNewData);
```

(见 [`insert.c`](../sqlite/src/insert.c#L2825-L2855))注意 `OP_IdxInsert` 那行——它在**一个 for 循环里**,对表的每个索引都发一条。循环结束后,才发针对表本身的 `OP_Insert`(rowid 表)或者直接 return(WITHOUT ROWID 表的主键索引插入已经在循环里做了,因为主键索引就是表)。

delete 同理,在 [`delete.c`](../sqlite/src/delete.c#L929) 里:

```c
    sqlite3VdbeAddOp3(v, OP_IdxDelete, iIdxCur+i, r1, p3);
```

每个索引发一条 `OP_IdxDelete`。update 呢?update 的本质是"删旧值 + 插新值",所以**每个索引要发 `OP_IdxDelete`(删旧 key)+ `OP_IdxInsert`(插新 key)**,代价是 insert 的两倍(对索引列而言;如果 update 没改某索引涉及的列,该索引可以跳过,这是优化)。

### 不这样会怎样:为什么索引必须同步维护

如果索引不同步维护会怎样?**索引和表数据不一致**——你 insert 了一行 age=25,但 idx_age 没加这条,下次 `WHERE age=25` 走索引就查不到这行(数据丢了,从索引视角)。这是**正确性灾难**。所以索引必须和表**强一致同步**,每次写都改,不能事后补。

这个强一致同步的代价是:**索引越多,写越慢**。一张 10 个索引的表,insert 比 0 个索引的表慢约 10 倍(粗略,实际还要看 B-tree 是否触发页分裂)。这就是 DBA 说的"索引是双刃剑——查询快了、写入慢了"。SQLite 作为嵌入式、读多写中等的数据库,这个权衡尤其要心里有数:**不要为了"以防万一查询慢"就乱建索引,每个索引都在默默吃你的写性能**。

> **钉死这件事**:索引的代价 = 写放大。一次写要改 (1 + 索引数) 棵 B-tree。这是物理事实,源码里白纸黑字(insert.c 的 `OP_IdxInsert` 循环)。设计索引时,问自己:**这个索引换来的查询加速,值不值得每次写多改一棵 B-tree?** 读多写少的列值得,频繁写的列要慎重。

---

## 八、partial index 和表达式索引:把索引做精

### partial index:只索引符合条件的行

SQLite 支持**部分索引(partial index)**:`CREATE INDEX idx_active_users ON users(age) WHERE active = 1`。这个索引**只索引 active=1 的行**,active=0 的行不进索引。

好处显而易见:**索引更小**(只装部分行)、**写放大更小**(改 active=0 的行不用动这个索引)。代价是只能服务 `WHERE active=1 AND age=?` 这种带 active=1 条件的查询。

源码上,partial index 的 WHERE 子句存在 [`Index.pPartIdxWhere`](../sqlite/src/sqliteInt.h#L2807):

```c
  Expr *pPartIdxWhere;     /* WHERE clause for partial indices */
```

(见 [`sqliteInt.h`](../sqlite/src/sqliteInt.h#L2797-L2836) 的 Index 结构体)建索引时这个 WHERE 传给 `sqlite3CreateIndex` 的 `pPIWhere` 参数(build.c:3968),查询规划器用它判断"这个索引能不能服务当前查询"(查询的 WHERE 必须逻辑蕴含索引的 pPartIdxWhere,才能用这个索引)。

### 表达式索引:把表达式当 key

SQLite 3.9+ 支持**表达式索引**:`CREATE INDEX idx_lower_name ON users(lower(name))`。这个索引的 key 是 `lower(name)` 的值,不是 name 本身。服务 `WHERE lower(name) = 'zhang'` 这种查询(否则 `WHERE lower(name)=?` 没法用普通 name 索引,因为函数破坏了字典序)。

源码上,表达式索引的列表达式存在 [`Index.aColExpr`](../sqlite/src/sqliteInt.h#L2808):

```c
  ExprList *aColExpr;      /* Column expressions */
```

`aiColumn` 数组里,表达式列用特殊值 `XN_EXPR = -2` 标记(见 [`sqliteInt.h`](../sqlite/src/sqliteInt.h#L2855-L2856)):

```c
#define XN_ROWID     (-1)     /* Indexed column is the rowid */
#define XN_EXPR      (-2)     /* Indexed column is an expression */
```

这两个特性(3.9+,2015)是 SQLite 索引能力的现代化补充,体现了"索引 key 可以是任意可比较的东西"这个设计弹性。

---

## 九、技巧精解:索引 B-tree 的 key 到底长什么样 + 覆盖索引省回表的 EXPLAIN 实证

这一节挑两个最硬核的点拆透。

### 技巧一:索引 record 的二进制编码——为什么"key 即内容"能工作

前面反复说"索引 B-tree 的 key 是索引列编码的 record + rowid,无 data"。这个 record 在二进制层面长什么样?它用的是和表 record **完全相同的 record 格式**(P3-09 讲过的变长 record:header + body,header 里是 serial type 串,body 里是各列值)。

区别只在"key 还是 key+data":

- **表 record**:整行是一个 record,作为 B-tree 的 **payload(data)**;key 是单独的 rowid 整数,存在 cell 头里(不是 record 的一部分)。
- **索引 record**:索引列的值 + rowid,编码成一个 record,作为 B-tree 的 **key**;没有 data(payload 为空)。

这就是 btree.h 注释说的"With BTREE_BLOBKEY, the key is an arbitrary BLOB and no content is stored anywhere - the key is the content"——**key 就是那个 record BLOB,没有额外 content**。

> **不这么编码会怎样**:如果索引 key 不用 record 格式、而用某种"age:4字节 + rowid:8字节"的定长格式,会怎样?也能工作,但**丧失 collation 灵活性**。TEXT 列的索引,要按 BINARY/NOASE/RTRIM 不同 collation 排序,定长格式没法表达"这个字段的比较规则"。用 record 格式(和表一样),索引能复用表的 `sqlite3VdbeRecordCompare` 比较函数,按列的 collation 逐列比较。这是 SQLite "表和索引共用一套 B-tree 代码"在 key 层面的延伸——连 key 的编码格式都共用。

这个共用带来的一个精妙后果:**索引 key 的比较,和表 record 的比较,走的是同一段 `sqlite3VdbeRecordCompare` 代码**(vdbeaux.c 里)。给定一个 key record 和一个待比较 record,这段代码逐列读 serial type、按列 collation 比较,直到分出大小。表和索引、覆盖和非覆盖、单列和复合,全用这一个比较器。这是 SQLite 极致代码复用的另一个典范。

### 技巧二:覆盖索引 vs 非覆盖的 EXPLAIN 实证(反面对比)

光说"覆盖索引省回表"不够直观,我们用真实的 EXPLAIN 对照看。建表:

```sql
CREATE TABLE t(id INTEGER PRIMARY KEY, a INT, b INT, c TEXT);
CREATE INDEX idx_a ON t(a);           -- 单列索引,只覆盖 a
CREATE INDEX idx_ab ON t(a, b);       -- 复合索引,覆盖 a+b(+id)
INSERT INTO t VALUES(1,10,100,'x'),(2,10,200,'y'),(3,20,100,'z');
```

**对照 A:非覆盖(查 c,idx_a 不含 c)**

```sql
EXPLAIN QUERY PLAN SELECT c FROM t WHERE a = 10;
-- 输出:SEARCH t USING INDEX idx_a (a=?)
```

注意 `EXPLAIN QUERY PLAN` 用的是 `SEARCH ... USING INDEX`,没说"covering"。opcode 流(用 `EXPLAIN` 不带 QUERY PLAN)里一定有 `SeekRowid`(回表取 c)+ `Column`(从表取 c)。

**对照 B:覆盖(查 a+b,idx_ab 含 a 和 b)**

```sql
EXPLAIN QUERY PLAN SELECT b FROM t WHERE a = 10;
-- 输出:SEARCH t USING COVERING INDEX idx_ab (a=?)
```

**关键字 `COVERING`** 出现了!`EXPLAIN QUERY PLAN` 明确告诉你这个查询用了**覆盖索引**,不会回表。opcode 流里**没有 SeekRowid**,`Column` 直接从索引游标(idx_ab)取 b。

这两个对照是验证你脑中"覆盖索引省回表"模型的最快工具——**自己建个表、跑两条 EXPLAIN QUERY PLAN,看有没有 `COVERING` 这个词**。这是 DBA 调优 SQLite 的日常动作。

> **钉死这件事**:`EXPLAIN QUERY PLAN` 输出里的 `COVERING` 这个词,是覆盖索引的官方认证。看到它,说明这条查询不用回表;没看到(只有 `USING INDEX`),说明要回表。设计高频查询的索引时,目标是让尽可能多的关键查询命中 `COVERING`——把 SELECT 的列 + WHERE 的列塞进一个复合索引。

### 反面对比:朴素建索引的陷阱

很多初学者朴素地"每列建一个单列索引",以为这样所有查询都快。大错。看 `SELECT b FROM t WHERE a=10`:

- 朴素方案(idx_a 单列):非覆盖,要回表取 b。
- 优化方案(idx_ab 复合):覆盖,不回表。

**单列索引几乎永远非覆盖**(除非你只查那一列)。复合索引才是覆盖索引的主力。所以"为高频查询设计复合索引、让它覆盖"远比"每列建单列索引"有效。这是 SQLite 索引调优的第一课。

---

## 十、索引类型总表

把本章涉及的索引类型汇总成一张表:

| 索引类型 | 怎么建 | 特点 | 适用场景 |
|----------|--------|------|----------|
| 普通索引 | `CREATE INDEX idx ON t(col)` | 二级索引,指向 rowid,需回表 | 通用,加速 `WHERE col=?` |
| UNIQUE 索引 | `CREATE UNIQUE INDEX idx ON t(col)` | key 唯一,插入重复报错 | 唯一约束 + 加速 |
| 复合索引 | `CREATE INDEX idx ON t(a,b,c)` | 多列 key,最左前缀 | 多条件 WHERE,可做覆盖 |
| 覆盖索引 | (隐式)索引含查询所有列 | 不回表,`COVERING` | 高频 SELECT 的列 |
| partial index | `CREATE INDEX idx ON t(a) WHERE cond` | 只索引部分行,索引小 | active=1 这种过滤 |
| 表达式索引 | `CREATE INDEX idx ON t(f(col))` | key 是表达式值 | `WHERE lower(name)=?` |
| 主键索引(rowid 表) | `INTEGER PRIMARY KEY` | rowid 别名,表本身按 rowid 排 | 主键点查 |
| 聚簇主键(WITHOUT ROWID) | `PRIMARY KEY ... WITHOUT ROWID` | 主键即 B-tree key,聚簇,不回表 | 短主键、主键查询为主 |

注意一个易混点:**rowid 表的 INTEGER PRIMARY KEY 不是"额外的索引"**,它就是 rowid 的别名,表 B-tree 本身就按它排序,不需要额外的索引树(查 rowid 是 `SeekRowid` 直接在表上)。而 WITHOUT ROWID 表的主键,是表 B-tree 的 key 本身(聚簇),也不需要额外索引树。**额外的索引树只在非主键列上才建**。

---

## 十一、章末小结

### 回扣主线

本章服务"**存储与事务**"这一面——讲的是索引(一种特殊的 B-tree)怎么存、怎么用、怎么维护。索引的全部技巧,都围绕"rowid 这把回表的钥匙"展开:

- 索引 B-tree 的 key = 索引列 + rowid(`BTREE_BLOBKEY`),value 为空(回表靠 key 里的 rowid)。
- 表 B-tree 的 key = rowid(`BTREE_INTKEY`),value = 整行 record。
- 同一套 `sqlite3BtreeInsert` 代码,靠 `pKeyInfo` 有无切换两种形态。
- 覆盖索引省回表、WITHOUT ROWID 聚簇省回表、复合索引最左前缀是字典序的几何后果、索引维护是写放大。

第 3 篇(B-tree 存储)到这里收尾:P3-08 讲了表和索引都是 B-tree、P3-09 讲了 record 格式和 type affinity、本章讲了索引怎么用 B-tree 加速。这三章一起,把"数据在单文件里怎么存、怎么查"讲完了。下一章进入第 4 篇——**B-tree 页在磁盘上,pager 怎么缓存、WAL/journal 怎么保证 ACID**。

### 五个为什么

1. **为什么索引是"额外的 B-tree"而不是别的数据结构?**——因为 B-tree 的查询效率来自"按 key 排序后能二分定位",这个性质对任何 key 都成立。你想按哪列快查,就把那列当 key 种一棵 B-tree。一张表可以有任意多棵索引 B-tree,用根页号区分。

2. **为什么索引把 rowid 编进 key,而不是单独存 data?**——紧凑(索引页塞更多记录)+ 索引天然按 rowid 排第二序(回表局部性好)。代价是 key 变长,但 SQLite 的 record 格式本就是变长的,没有额外开销。

3. **为什么复合索引有最左前缀原则?**——不是规则,是 B-tree 按 key 字典序排序的几何后果。你能二分的只有"连续的最左前缀",一旦跳列或换范围,后面的列就失去全局序,退化成顺序扫。

4. **为什么 WITHOUT ROWID 表的主键查询不用回表?**——因为 `convertToWithoutRowidTable` 把表的 `OP_CreateBtree` P3 从 `BTREE_INTKEY` 改成 `BTREE_BLOBKEY`,主键成了 B-tree 的 key、整行成了 data,**主键索引就是数据本身**(聚簇),没有独立的"表树"可回。对照 InnoDB 默认就是聚簇。

5. **为什么索引越多写越慢?**——一次 insert/update/delete,表 B-tree 要改一遍,**每个相关索引 B-tree 都要同步改一遍**(insert.c 的 `OP_IdxInsert` 循环、delete.c 的 `OP_IdxDelete`)。索引数 = 写放大倍数。这是索引换查询速度的物理代价。

### 想继续深入往哪钻

- **索引选择(为什么用这个索引不用那个)**:本书 **P2-07《一条 SELECT 怎么执行》**第 5 节"查询规划器",讲透了 `wherePathSolver` 的 System-R 动态规划、`(rCost,nRow,rUnsort)` 三元代价、LogEst 对数存储、WhereLoop 代价模型。本章只讲存储侧,代价模型不重复。
- **索引 B-tree 的页结构 / cell 布局**:本书 **P3-08《B-tree 存储》**,讲了 B-tree 页的 header/cell pointer array/freeblock、表和索引页的同构。
- **record 的二进制编码**:本书 **P3-09《记录格式与动态类型》**,讲了 record 的 header+body、serial type、type affinity。
- **SQLite 官方文档**:读 "Query Planner"(查询规划器)、"The SQLite Indexing System"(索引系统概述)、"WITHOUT ROWID"(WITHOUT ROWID 的设计权衡,官方有专门一页讲什么时候该用)。
- **动手感受**:`sqlite3` CLI 建个表,加几个索引,用 `EXPLAIN QUERY PLAN` 看不同查询的索引选择和 `COVERING` 标志;用 `EXPLAIN`(不带 QUERY PLAN)看 opcode 流里有没有 `SeekRowid`(回表的标志)。

### 引出下一章

索引讲完了,但所有这些 B-tree(表的、索引的)的**页都在磁盘文件上**。SQLite 执行 `SeekRowid`、`IdxInsert` 时,不可能每次都去磁盘读页——太慢了。**页必须缓存进内存**,这就是 pager 的事。而且写一页时,如果 crash 了怎么办?**必须先记日志(rollback journal / WAL),才能 crash 不丢**。下一章 P4-11,我们从"索引 B-tree 加速查询"接过来,进入第 4 篇——**Pager:页怎么缓存、怎么读写**,把"存储与事务"这半本的"事务"那面接上。

> **下一章**:[P4-11 · Pager:页缓存](P4-11-Pager-页缓存.md)
