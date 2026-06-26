# 第 6 篇 · 第 19 章 · 触发器 / 视图 / FTS

> **核心问题**:前 18 章,我们把一条 SELECT/INSERT/UPDATE/DELETE 怎么编译成 VDBE opcode、怎么执行、怎么存进 B-tree、怎么用 pager/WAL 保 ACID,全过了一遍。可 SQL 还有三个"高级特性"——`CREATE TRIGGER`、`CREATE VIEW`、全文索引(FULLTEXT TABLE)——它们的执行模型长什么样?触发器"在某个事件发生时自动跑一段代码",这"自动跑"在 VDBE 里到底怎么实现?视图"是一张虚拟的表",这"虚拟"在 opcode 层面意味着什么?FTS 全文搜索"分词建倒排索引",这倒排索引存在 SQLite 的单文件 B-tree 里长什么样?
>
> 这三个特性看起来毫无关系,本章会给你一个**统一答案**:**它们最后都被编译成 VDBE opcode**——触发器体被编成一段 sub-program(子程序),挂在表上,主语句执行到合适时机用 `OP_Program` opcode 调用它;视图在查询时被"宏展开"成子查询(根本没有任何独立存储);FTS 表是一张虚拟表(virtual table),它的插入/查询全是 VDBE 调到 C 实现的虚拟表方法。**不管 SQL 表面上多花哨,底下都是 opcode 流**——这是 SQLite "VDBE 统一执行模型"的最大胜利,也是本章的主线。

> **读完本章你会明白**:
> 1. 触发器不是运行时"事件回调",而是**编译期就把触发器体编成一段 sub-program,挂到表上;主语句执行时在 BEFORE/AFTER 时机用 `OP_Program` opcode 调用**——"触发器编译进 opcode 流"是字面意义的,不是比喻。
> 2. 视图**默认不物化**:CREATE VIEW 只把 SELECT 定义存进 `sqlite_schema`,查询用到视图时 parser 把视图的 SELECT **展开成子查询**(宏展开式),视图本身一行数据都不存。
> 3. FTS3/FTS5 是**虚拟表**:用 `sqlite3_create_module` 注册一个 C 实现的 module(22 个方法),表的读写全走 module 的 xCreate/xConnect/xFilter/xColumn/xUpdate 等回调;倒排索引(term → doclist)存成 segment,FTS3 用 `%_segments`+`%_segdir` 影子表、FTS5 用 `%_data`+`%_idx` 影子表。
> 4. 虚拟表机制是 SQLite 的"逃生舱":FTS、JSON 表、rtree、甚至序列化 KV,都靠同一套 `sqlite3_module` 方法表实现,把"自定义表行为"的复杂度完整外包给 C 扩展。
> 5. 这三件事**共用同一个结论**——再复杂的 SQL 特性,最后都坍缩成 VDBE opcode;这就是为什么 SQLite 的执行模型只有一个(VDBE),却能撑起完整 SQL 语法。

> **如果一读觉得太难**:先抓住三句结论——① 触发器体是一段 sub-program,主语句用 `OP_Program` opcode 调它;② 视图是"查询时展开的子查询",没有自己的数据;③ FTS 是虚拟表,倒排索引藏在几张影子表里。本章主线就是这句:**SQL 的高级特性,最后都被 VDBE 字节码统一掉**。

---

## 〇、一句话点破

> **触发器、视图、FTS 这三个看似不搭界的"高级 SQL 特性",在 SQLite 内部都被同一个事实统一:它们最后都是 VDBE opcode。触发器体是 sub-program、被 `OP_Program` 调用;视图是查询时展开的子查询(展开后还是 opcode);FTS 是虚拟表(读写走 C 回调,但回调产出的还是 opcode)。再花哨的语法,都逃不出"编译成字节码、用虚拟机执行"这个根。**

这是结论,不是理由。本章倒过来拆:先看触发器——它最直接地暴露了"高级特性 = opcode"这个事实;再看视图——它暴露了"存储 vs 计算"的边界(视图不存储);再看 FTS/虚拟表——它暴露了 SQLite 怎么用一套机制挂载任意自定义存储;最后把三者拧成一股,呼应全书主线。

> **本章归属二分法的哪一面**:三件事都落在 **编译与执行** 这一侧。触发器是"编译期把触发器体编进 opcode 流",视图是"编译期(parser)把视图展开成子查询",FTS 虚拟表是"执行期 VDBE 调用 module 回调产出 opcode"。它们都不改 B-tree/pager/WAL 的存储语义(FTS 影子表本身也只是普通 B-tree),改的是"opcode 怎么来、怎么执行"。所以本章服务"编译与执行"那一面。

---

## 一、触发器:把"事件回调"编译进 opcode 流

我们从触发器讲起,因为它最能直接暴露 SQLite 的设计哲学。

### 1.1 一个朴素设想:运行时事件回调

先用最朴素的方式设想触发器怎么实现。你写:

```sql
CREATE TRIGGER log_insert AFTER INSERT ON users
BEGIN
  INSERT INTO audit VALUES (new.id, 'insert', datetime('now'));
END;
```

意思是:**每次往 `users` 表 INSERT 一行之后,自动再 INSERT 一行到 `audit` 表**。

最直白的实现是"运行时事件回调":在执行 `INSERT INTO users` 的代码里,写一句"如果 users 表上有 AFTER INSERT 触发器,就调用这个触发器的处理函数"。这个处理函数内部再执行那条 `INSERT INTO audit`。这非常像 GUI 编程里的 `onClick` 回调——按钮被点了就调你注册的函数。

> **不这样会怎样**:这种"运行时回调"模型能跑,但有几个硬伤:
> 1. **执行模型分裂**:主 INSERT 是 VDBE opcode 在执行,触发器体里的 INSERT 又要单独走一遍"解析→编译→执行"。每个触发器体都是一次完整的"小 SQL 执行",如果主语句改 100 行触发器就跑 100 次,性能崩。
> 2. **优化机会丧失**:触发器体和主语句是分开编译的,优化器看不到它们一起的全貌,没法做跨语句的优化(比如触发器体里读的列可以复用主语句已经读出来的值)。
> 3. **语义难统一**:`OLD.*` 和 `NEW.*` 怎么传给"回调函数"?如果回调是 C 函数,得搞一套专门的 ABI;如果是再编译一次 SQL,得搞一套参数绑定。复杂度爆炸。

SQLite **不这么做**。它选了另一条路:**把触发器体在编译期就编成一段独立的 VDBE sub-program(子程序),挂到表上;主语句执行时,在 BEFORE/AFTER 时机,用一条 `OP_Program` opcode 调用这个 sub-program**。下面拆透。

### 1.2 CREATE TRIGGER:把触发器定义存进 sqlite_schema

先看 `CREATE TRIGGER` 这条 DDL 本身怎么处理。Parser(`parse.y`)里,触发器的语法规则是这样的([sqlite3FinishTrigger 调用](../sqlite/src/parse.y#L1740)、[sqlite3BeginTrigger 调用](../sqlite/src/parse.y#L1746)):

```
cmd ::= createkw trigger_decl(A) BEGIN trigger_cmd_list(S) END(Z). {
  sqlite3FinishTrigger(pParse, S, &all);
}
trigger_decl(A) ::= temp(T) TRIGGER ifnotexists(NOERR) nm(B) dbnm(Z)
                    trigger_time(C) trigger_event(D)
                    ON fullname(E) foreach_clause when_clause(G). {
  sqlite3BeginTrigger(pParse, &B, &Z, C, D.a, D.b, E, G, T, NOERR);
}
```

也就是说,Parser 看到 `CREATE TRIGGER log_insert AFTER INSERT ON users BEGIN ... END`,会做两件事:

1. 先调 [`sqlite3BeginTrigger`](../sqlite/src/trigger.c#L104):收集触发器的**元信息**(名字、时机 `BEFORE/AFTER/INSTEAD OF`、事件 `INSERT/UPDATE/DELETE`、挂在哪张表、WHEN 条件),把这些塞进 `Parse.pNewTrigger`(一个临时的 `Trigger` 结构体)。
2. `BEGIN ... END` 之间的每条语句(`trigger_cmd_list`)被 parser 解析成一条条 `TriggerStep`(链表,每种 INSERT/UPDATE/DELETE/SELECT 各一个 step 构造函数,见 [`sqlite3TriggerSelectStep`](../sqlite/src/trigger.c#L443) 等)。
3. 最后调 [`sqlite3FinishTrigger`](../sqlite/src/trigger.c#L323):把 `step_list` 挂到 `Trigger` 上,然后把**整个 CREATE TRIGGER 的原始文本**写进 `sqlite_schema` 表(就是 `sqlite_master`)。

关键在第三步。看 [`sqlite3FinishTrigger`](../sqlite/src/trigger.c#L323) 的核心片段(简化,非源码原文,只保留主线):

```c
void sqlite3FinishTrigger(
  Parse *pParse, TriggerStep *pStepList, Token *pAll
){
  Trigger *pTrig = pParse->pNewTrigger;
  ...
  pTrig->step_list = pStepList;          /* 触发器体挂在 Trigger 上 */
  ...
  if( !db->init.busy ){
    Vdbe *v = sqlite3GetVdbe(pParse);
    sqlite3BeginWriteOperation(pParse, 0, iDb);
    z = sqlite3DbStrNDup(db, (char*)pAll->z, pAll->n);   /* CREATE TRIGGER 整段文本 */
    sqlite3NestedParse(pParse,
       "INSERT INTO %Q." LEGACY_SCHEMA_TABLE
       " VALUES('trigger',%Q,%Q,0,'CREATE TRIGGER %q')",
       db->aDb[iDb].zDbSName, zName, pTrig->table, z);   /* 把原文存进 sqlite_schema */
    sqlite3ChangeCookie(pParse, iDb);
    sqlite3VdbeAddParseSchemaOp(v, iDb, ...);
  }
  ...
}
```

> **钉死这件事(第一钉)**:CREATE TRIGGER 这条 DDL **干的事就是把触发器的原始 SQL 文本存进 `sqlite_schema` 表**(那行 `type='trigger'` 的记录的最后一列就是完整的 `CREATE TRIGGER ... END` 文本)。**触发器体此时还没有被编译成 opcode**——它只是作为文本躺在 schema 表里。

为什么不立即编译?因为 CREATE TRIGGER 时,触发器体里可能引用的列、表、视图都还没解析完(尤其是触发器在别的表之前创建时)。SQLite 选择**延迟编译**:把原文存起来,等真正要执行用到它的 DML 时再编。

### 1.3 触发器被"激活":从 sqlite_schema 读出来挂到表上

下次打开数据库时,SQLite 会扫描 `sqlite_schema`,对每条 `type='trigger'` 的记录**重新 parse 一遍那段 CREATE TRIGGER 文本**(这次 `db->init.busy=1`,走"初始化"路径),把 `Trigger` 结构体建好,挂到对应表的 `pTrigger` 链表上。看 [`sqlite3FinishTrigger`](../sqlite/src/trigger.c#L323) 后半段(初始化路径):

```c
  if( db->init.busy ){
    Trigger *pLink = pTrig;
    Hash *pHash = &db->aDb[iDb].pSchema->trigHash;
    ...
    pTrig = sqlite3HashInsert(pHash, zName, pTrig);   /* 存进全局 trigger hash */
    ...
    if( pLink->pSchema==pLink->pTabSchema ){
      Table *pTab;
      pTab = sqlite3HashFind(&pLink->pTabSchema->tblHash, pLink->table);
      pLink->pNext = pTab->pTrigger;                  /* 挂到表的 pTrigger 链表头 */
      pTab->pTrigger = pLink;
    }
  }
```

所以执行期,每张 `Table` 结构体上挂着一条 `pTrigger` 链(所有挂在这张表上的触发器)。链上每个 `Trigger` 的 `step_list` 还只是 AST(`TriggerStep` 链表),**还没编译成 opcode**。

### 1.4 真正的编译:DML 执行时把触发器体编成 sub-program

现在主角登场。当你执行 `INSERT INTO users VALUES (...)` 且 `users` 上有触发器时,`insert.c` 的 codegen 会先调 [`sqlite3TriggersExist`](../sqlite/src/trigger.c#L869) 查"这表上有没有匹配当前事件的触发器":

```c
pTrigger = sqlite3TriggersExist(pParse, pTab, TK_INSERT, 0, &tmask);   /* insert.c:985 */
```

`tmask` 是个位掩码,告诉我们有 BEFORE 触发器(`TRIGGER_BEFORE`)还是 AFTER 触发器(`TRIGGER_AFTER`)。然后,insert.c 会在主 INSERT 的 opcode 流里,在**合适的时机**调用 [`sqlite3CodeRowTrigger`](../sqlite/src/trigger.c#L1468):

- 在主 INSERT 真正写 B-tree **之前**:`sqlite3CodeRowTrigger(pParse, pTrigger, TK_INSERT, 0, TRIGGER_BEFORE, ...)`([insert.c:1501](../sqlite/src/insert.c#L1501))
- 在主 INSERT 真正写 B-tree **之后**:`sqlite3CodeRowTrigger(pParse, pTrigger, TK_INSERT, 0, TRIGGER_AFTER, ...)`([insert.c:1612](../sqlite/src/insert.c#L1612))

`sqlite3CodeRowTrigger` 遍历触发器链,对每个匹配的触发器调 [`sqlite3CodeRowTriggerDirect`](../sqlite/src/trigger.c#L1396)。这个函数是真正的"触发器编译 + 调用"枢纽:

```c
void sqlite3CodeRowTriggerDirect(
  Parse *pParse, Trigger *p, Table *pTab, int reg, int orconf, int ignoreJump
){
  Vdbe *v = sqlite3GetVdbe(pParse);   /* 主语句的 VDBE */
  TriggerPrg *pPrg;
  pPrg = getRowTrigger(pParse, p, pTab, orconf);   /* 把触发器体编译成 sub-program */
  assert( pPrg || pParse->nErr );

  /* 在主 VDBE 里发一条 OP_Program,P4 指向那个 sub-program */
  if( pPrg ){
    int bRecursive = (p->zName && 0==(pParse->db->flags&SQLITE_RecTriggers));
    sqlite3VdbeAddOp4(v, OP_Program, reg, ignoreJump, ++pParse->nMem,
                      (const char *)pPrg->pProgram, P4_SUBPROGRAM);
    VdbeComment((v, "Call: %s.%s", p->zName, onErrorText(orconf)));
    sqlite3VdbeChangeP5(v, (u16)bRecursive);   /* P5: 是否禁止递归 */
  }
}
```

这里有两个关键动作:

1. **`getRowTrigger`**(就是 [`codeRowTrigger`](../sqlite/src/trigger.c#L1228) 那一族)做的是:**新开一个子 Parse 上下文**,把触发器的 `step_list`(那串 TriggerStep AST)当成一段独立的 SQL 程序编译,产出一个 `SubProgram`(一段独立的 opcode 流)。这个 sub-program 的输入是 `reg` 开始的一组寄存器(里面装着 `OLD.*` 和 `NEW.*` 的值,见后面 1.6 节)。
2. **`sqlite3VdbeAddOp4(v, OP_Program, ...)`**:在**主语句的 VDBE 流**里,发一条 `OP_Program` opcode,它的 `p4` 是个指针,指向刚编出来的 `SubProgram`。

> **钉死这件事(第二钉·本章最关键)**:触发器体**被编译成一段独立的 VDBE sub-program**;主语句的 VDBE 流里,在 BEFORE/AFTER 时机,**有一条 `OP_Program` opcode 去调用这个 sub-program**。这就是字面意义的"触发器编译进 opcode 流"——不是运行时回调,不是事件分发,就是一条普通的 opcode(`OP_Program`)调一段普通的 opcode(触发器体)。SQLite 的执行模型从头到尾只有 VDBE 一套。

### 1.5 用 EXPLAIN 看触发器怎么嵌进 opcode 流

讲这么多,不如亲眼看看。建一张带 BEFORE + AFTER 触发器的表:

```sql
CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE audit(id INT, action TEXT);
CREATE TRIGGER trg_before BEFORE INSERT ON users BEGIN
  INSERT INTO audit VALUES (new.id, 'before');
END;
CREATE TRIGGER trg_after AFTER INSERT ON users BEGIN
  INSERT INTO audit VALUES (new.id, 'after');
END;
INSERT INTO users VALUES (1, 'alice');
```

最后那条 `INSERT INTO users VALUES (1, 'alice')` 编出来的 opcode(用 `EXPLAIN` 看,简化示意,实际地址可能不同)长这样:

```
addr  opcode         p1   p2   p3   p4              comment
----  -----------    ---  ---  ---  --------------  -----------------------------
0     OpenWrite      0    2    0                    # 打开 users 表(根页 2)写
1     OpenWrite      1    3    0                    # 打开 audit 表(根页 3)写
2     Integer        1    1    0                    # 常量 1 → 寄存器 1 (id)
3     String8        0    2    0    alice           # 'alice' → 寄存器 2 (name)
4     OpenEphemeral  ...  ...  ...                  # 准备 new.* 寄存器组
5     Program        ...  ...  ...  SubProgram#0    # 调用 trg_before 子程序
6     NewRowid       0    3    0                    # 给 users 行分配 rowid
7     Insert         0    1    2                    # 真正写 users 表 ← 主 INSERT
8     Program        ...  ...  ...  SubProgram#1    # 调用 trg_after 子程序
9     Close          0    0    0
10    Close          1    0    0
11    Halt           0    0    0
```

(简化示意,非 EXPLAIN 原文,真实地址、寄存器号、辅助 opcode 会有差异)

注意三条关键 opcode:

- **addr 5 `Program`**:这是 `trg_before` 的 sub-program 调用,**在主 INSERT 之前**。VDBE 执行到这条 opcode,会跳进 `SubProgram#0` 的 opcode 流执行(里面就是那条 `INSERT INTO audit VALUES (new.id, 'before')` 编出来的 opcode),执行完再回来。
- **addr 7 `Insert`**:这才是**真正往 users 表写那一行**——它夹在 BEFORE 触发器和 AFTER 触发器中间,完美对应"BEFORE 在写之前跑、AFTER 在写之后跑"的语义。
- **addr 8 `Program`**:这是 `trg_after` 的 sub-program 调用,**在主 INSERT 之后**。

`OP_Program` 的 `p4` 是个 `P4_SUBPROGRAM`(常量 `-4`,见 [vdbe.h:131](../sqlite/src/vdbe.h#L131)),指向一个 [`SubProgram` 结构体](../sqlite/src/vdbe.h#L101):

```c
struct SubProgram {
  VdbeOp *aOp;       /* 该 sub-program 自己的 opcode 数组 */
  int nOp;           /* opcode 数 */
  int nMem;          /* 它用的寄存器数 */
  int nCsr;          /* 它用的游标数 */
  ...
  SubProgram *pNext; /* 链表 */
};
```

> **钉死这件事(第三钉)**:主语句的 opcode 流和触发器体的 opcode 流,**是两段独立的 opcode 数组**(`VdbeOp *aOp` 各自一份)。主 VDBE 执行到 `OP_Program`,**借用同一个 VDBE 虚拟机**(就是 `vdbe.c` 那个大 switch-case 循环,见 P2-05)切到 sub-program 的 opcode 数组执行,执行完切回来。**没有第二个虚拟机,没有第二个执行模型**——一切还是那个 VDBE。

### 1.6 OLD.* 和 NEW.*:寄存器传参

触发器体里能引用 `old.id`、`new.name`——这些"伪表的列"在 opcode 层面是什么?答案藏在 [`sqlite3CodeRowTriggerDirect`](../sqlite/src/trigger.c#L1396) 函数上面的注释里(那里写得清清楚楚):

```
   Register       Contains
   ------------------------------------------------------
   reg+0          OLD.rowid
   reg+1          OLD.* value of left-most column of pTab
   ...            ...
   reg+N          OLD.* value of right-most column of pTab
   reg+N+1        NEW.rowid
   reg+N+2        NEW.* value of left-most column of pTab
   ...            ...
   reg+N+N+1      NEW.* value of right-most column of pTab
```

> **钉死这件事(第四钉)**:`OLD.*` 和 `NEW.*` **不是真正的表**,而是**一组寄存器**。`OP_Program` 的 `p1`(那个 `reg` 参数)告诉 sub-program:"从寄存器 `reg` 开始,前 N+1 个是 OLD,后 N+1 个是 NEW"。sub-program 里的 opcode 引用 `old.id` 时,code generator 只是把它编成"读寄存器 `reg+某偏移`"。这又一次印证了:**触发器没有引入新执行机制,只是用了已有的寄存器 + opcode**。

> **为什么 OLD/NEW 用寄存器而不是真表**:如果用真表,每次触发都要建一张临时表、塞数据、删数据,开销大;用寄存器,主语句反正已经把行数据读进寄存器了,直接把寄存器地址传给 sub-program 即可,**零拷贝**。这是 SQLite 的典型取舍:能用寄存器解决的,绝不建临时表。

### 1.7 INSTEAD OF:视图上的触发器

`BEFORE` 和 `AFTER` 都好理解(写在主操作前/后)。`INSTEAD OF` 呢?它专门用在**视图**上(普通表不能用 INSTEAD OF)。语义是:"当对视图做 INSERT/UPDATE/DELETE 时,**不要做默认的(其实视图根本没法直接改)**,而是**改跑这段触发器体**"。

`INSTEAD OF` 在 parser 里被当成 `TK_INSTEAD`([parse.y:1756](../sqlite/src/parse.y#L1756)),在 codegen 里它的处理逻辑就是"不发出对视图本身的写 opcode(视图也没法写),只发 `OP_Program` 调用触发器体"。这是触发器机制反过来给视图"补上可写性"——视图只读,但挂个 INSTEAD OF 触发器就能把"对视图的写"翻译成"对底层表的写"。我们后面讲视图时会用到这一点。

---

## 二、视图:查询时"宏展开"成子查询

触发器讲完,视图就简单了——它甚至比触发器更"轻":**视图没有任何独立的执行机制,它纯粹是 parser/codegen 层面的一次"文本展开"**。

### 2.1 CREATE VIEW:只存 SELECT 定义,不存数据

```sql
CREATE VIEW active_users AS SELECT id, name FROM users WHERE active=1;
```

这条 DDL 干了什么?[`sqlite3CreateView`](../sqlite/src/build.c#L3009) 的核心逻辑(简化):

```c
void sqlite3CreateView(Parse *pParse, ... Select *pSelect, ...){
  ...
  sqlite3FixInit(&sFix, ...);
  if( sqlite3FixSelect(&sFix, pSelect) ) goto create_view_fail;
  pSelect->selFlags |= SF_View;            /* 标记:这是视图的 SELECT */
  ...
  p->u.view.pSelect = pSelect;             /* 把 SELECT 的 AST 挂到 Table 上 */
  ...
}
```

然后,跟触发器一样,这条 CREATE VIEW 的**原文文本**被写进 `sqlite_schema`(`type='view'` 那一行)。下次打开库时,重新 parse 这段文本,把 SELECT 的 AST 挂回 `Table.u.view.pSelect`(见 [build.c:3061](../sqlite/src/build.c#L3061) `p->u.view.pSelect = pSelect`)。

> **钉死这件事(第五钉)**:CREATE VIEW **不建任何 B-tree,不存任何数据**。视图在 `sqlite_schema` 里只有一行(存的是 SELECT 的文本),内存里有个 `Table` 结构体,它的 `u.view.pSelect` 字段挂着那段 SELECT 的 AST。**视图是"存了一条 SELECT 定义",不是"存了一份数据副本"**。

### 2.2 默认行为:不物化,展开成子查询

那么 `SELECT * FROM active_users WHERE name LIKE 'a%'` 怎么执行?答案藏在 [`select.c` 的 `sqlite3SrcListLookup`/视图处理那段](../sqlite/src/select.c#L6052)(简化):

```c
if( !IsOrdinaryTable(pTab) ){
  ...
  if( sqlite3ViewGetColumnNames(pParse, pTab) ) return WRC_Abort;   /* 算出视图列名 */
  assert( pFrom->fg.isSubquery==0 );
  if( IsView(pTab) ){
    ...
    sqlite3SrcItemAttachSubquery(pParse, pFrom, pTab->u.view.pSelect, 1);  /* 把视图的 SELECT 当子查询挂到 FROM 项 */
  }
  ...
}
```

注意 `sqlite3SrcItemAttachSubquery(pParse, pFrom, pTab->u.view.pSelect, 1)` 这行——它的意思是:**把视图的 SELECT(`pTab->u.view.pSelect`),当成一个子查询,挂到当前查询的 FROM 子句里那个 `pFrom` 项上**。

这就是**视图展开**。等价于:

```sql
-- 你写的:
SELECT * FROM active_users WHERE name LIKE 'a%';
-- 展开后(parser/codegen 层面)等价于:
SELECT * FROM (SELECT id, name FROM users WHERE active=1) AS active_users
WHERE name LIKE 'a%';
```

展开之后,这个"带子查询的 FROM"就跟一个普通子查询没有区别了——code generator 对它一视同仁地编 opcode(子查询产出的行进临时表,外层从临时表读)。所以**视图的执行就是子查询的执行,没有任何"视图专属"opcode**。

> **钉死这件事(第六钉)**:视图 = 查询时把视图名替换成它的 SELECT(作为子查询)。这是**宏展开(macro expansion)**式的实现——视图名像一个宏,展开后就不存在了。视图的查询性能,完全等价于"你手写展开后的子查询"的性能;优化器对两者一视同仁。

> **为什么不物化**:物化视图(MySQL 的某些视图、Oracle 的 materialized view)会真的存一份数据副本,查询快但更新慢、有一致性问题。SQLite **默认不物化**——这是嵌入式数据库的取舍:嵌入式场景数据量通常不大、读不算特别频繁,物化的更新成本和数据一致性复杂度不划算。SQLite 3.x 后期虽然有些情况下会把视图结果"物化"进临时表(比如视图出现在多个地方、或递归 CTE 里),但那是**查询时的临时物化**(查完就扔),**不是存储物化**——视图本身仍然不存数据。

### 2.3 用 EXPLAIN 看视图展开

```sql
EXPLAIN SELECT * FROM active_users WHERE name LIKE 'a%';
```

你会看到 opcode 长得跟"对一张普通表做带 WHERE 的查询"完全一样:打开 `users` 表、循环、Column 取 `name`、判断 `active=1` 和 `name LIKE 'a%'`、ResultRow。**没有任何"打开视图"的 opcode**——视图在编译期就已经被展开消失。

唯一的痕迹是:如果视图的 SELECT 比较复杂(比如有聚合、JOIN),展开后可能产出一个 ephemeral table(临时表)先装子查询结果,再让外层扫这个临时表。但这跟"子查询物化"是同一套机制(P2-07 讲过子查询),不是视图特有的。

### 2.4 视图上的写:INSTEAD OF 触发器接住

普通视图是只读的(你不能 `INSERT INTO active_users ...`,因为视图本身没 B-tree 可写)。但**挂了 INSTEAD OF 触发器**的视图可写——你写 `INSERT INTO active_users ...`,SQLite 不去改视图(改不了),而是触发那个 INSTEAD OF INSERT 触发器(用上一节的 `OP_Program` 机制),由触发器体去改底层 `users` 表。

这把两节连起来了:**视图的"可写"是触发器给的;触发器的"接视图写"是 `OP_Program` 实现的**。又是 opcode 统一一切。

---

## 三、虚拟表机制:FTS、JSON、rtree 的共同底座

视图讲完,我们离 FTS 还差一步——FTS 不是普通表(它的"表"背后不是 B-tree),它走的是**虚拟表(virtual table)**机制。这是 SQLite 留的"逃生舱":让 C 扩展能定义自己的"表",自定义怎么存、怎么查、怎么写。

### 3.1 sqlite3_module:一张 22 项的方法表

虚拟表的核心是 [`struct sqlite3_module`](../sqlite/src/sqlite.h.in#L7704)——一张函数指针表,扩展填写这张表,告诉 SQLite "这种虚拟表该怎么建、怎么连、怎么查、怎么写":

```c
struct sqlite3_module {
  int iVersion;
  int (*xCreate)(sqlite3*, void *pAux, int argc, const char *const*argv,
               sqlite3_vtab **ppVTab, char**);          /* CREATE VTAB 时调用 */
  int (*xConnect)(sqlite3*, void *pAux, int argc, const char *const*argv,
               sqlite3_vtab **ppVTab, char**);          /* 已存在的 VTAB 连接时调用 */
  int (*xBestIndex)(sqlite3_vtab*, sqlite3_index_info*);  /* 让 VTAB 自己选索引 */
  int (*xDisconnect)(sqlite3_vtab*);
  int (*xDestroy)(sqlite3_vtab*);
  int (*xOpen)(sqlite3_vtab*, sqlite3_vtab_cursor**);    /* 打开游标 */
  int (*xClose)(sqlite3_vtab_cursor*);
  int (*xFilter)(sqlite3_vtab_cursor*, int idxNum, ...);  /* 开始一次扫描(WHERE) */
  int (*xNext)(sqlite3_vtab_cursor*);                      /* 下一行 */
  int (*xEof)(sqlite3_vtab_cursor*);                       /* 扫完没 */
  int (*xColumn)(sqlite3_vtab_cursor*, sqlite3_context*, int);  /* 取第 N 列 */
  int (*xRowid)(sqlite3_vtab_cursor*, sqlite3_int64*);    /* 取 rowid */
  int (*xUpdate)(sqlite3_vtab*, int, sqlite3_value**, sqlite3_int64*);  /* 写 */
  int (*xBegin)(sqlite3_vtab*);    int (*xSync)(sqlite3_vtab*);   /* 事务 */
  int (*xCommit)(sqlite3_vtab*);   int (*xRollback)(sqlite3_vtab*);
  int (*xFindFunction)(...);        int (*xRename)(...);
  /* version 2+ */
  int (*xSavepoint)(...);  int (*xRelease)(...);  int (*xRollbackTo)(...);
  /* version 3+ */
  int (*xShadowName)(const char*);   /* 哪些表名是"影子表"(只读保护) */
  /* version 4+ */
  int (*xIntegrity)(...);
};
```

(简化,完整版见 [sqlite3.h.in:7704](../sqlite/src/sqlite.h.in#L7704))

这张表的设计动机是:**把"一张表该怎么行为"的复杂性,完整外包给 C 扩展**。SQLite 只负责调度——执行 `SELECT * FROM fts_table WHERE fts_table MATCH 'foo'` 时,VDBE 发的 opcode 是 `VOpen`/`VFilter`/`VNext`/`VColumn` 这些虚拟表专属 opcode,它们内部就是去调 module 的 `xOpen`/`xFilter`/`xNext`/`xColumn`。SQLite 不知道也不关心数据到底怎么存(可能在另一组 B-tree 里、可能在内存里、可能在网络上)。

### 3.2 注册一个虚拟表模块

扩展用 [`sqlite3_create_module`](../sqlite/src/vtab.c#L108)(或带析构回调的 [`sqlite3_create_module_v2`](../sqlite/src/vtab.c#L123))注册一个 module:

```c
int sqlite3_create_module(
  sqlite3 *db, const char *zName,
  const sqlite3_module *pModule, void *pAux
);
```

注册后,`zName`(比如 `"fts5"`)就成了一种"虚拟表类型"。然后用户写 `CREATE VIRTUAL TABLE t USING fts5(content)`,SQLite 的 parser 看到 `USING fts5`,就去查 `fts5` 这个 module,调它的 `xCreate`。

> **承接**:虚拟表机制在 P3-08(B-tree 存普通表)、P2-05(VDBE opcode 循环)讲过普通表怎么存怎么执行;虚拟表是"普通表机制 + C 回调"的混合——外表看是表(SQL 语法一致),内里是 C 扩展自己实现的存储。这是 SQLite 区别于 MySQL 的一个灵活性:MySQL 加新存储引擎要改 server,SQLite 加新"表类型"只要写个 C 扩展注册个 module。

---

## 四、FTS 全文索引:倒排索引藏在影子表里

现在主角 FTS 登场。FTS(Full-Text Search)是 SQLite 的全文索引扩展,让一列文本能高效地 `MATCH '关键词'`。我们重点拆三件事:**FTS 表怎么建出来、倒排索引长什么样、查询怎么跑**。

### 4.1 FTS3 和 FTS5:都还在,FTS5 是现代首选

先回答一个常见疑问:**3.54 用 FTS3 还是 FTS5?** 答案是**两个都在 `ext/` 下,都活着**——FTS3(`ext/fts3/`,有 `fts3.c`、`fts3_write.c` 等)和 FTS5(`ext/fts5/`,有 `fts5_main.c`、`fts5_index.c` 等)。两者是同一类功能的两代实现:

- **FTS3**(以及 FTS4,代码同一份):老版本,2007 年进入 SQLite,segment 存为"b+-tree in shadow tables"。
- **FTS5**:2015 年起的重写,架构更现代(segment 用扁平格式 + 增量合并、支持表达式更丰富、新增 detail 模式控制),**是官方文档明确推荐的新项目首选**(SQLite 官方文档原话:"FTS5 is recommended for new applications")。

哪个被默认编译进去,取决于编译开关:`SQLITE_ENABLE_FTS3` / `SQLITE_ENABLE_FTS5`([main.c:19-44](../sqlite/src/main.c#L19) 那两个 `#ifdef`)。你可以两个都开,也可以只开一个。本章后面以 FTS5 为主拆(它是现代首选),FTS3 作为对照。

### 4.2 建一张 FTS 表:产生一堆影子表

```sql
CREATE VIRTUAL TABLE docs USING fts5(title, body);   -- FTS5
-- 或:
CREATE VIRTUAL TABLE docs USING fts3(title, body);   -- FTS3
```

这条 DDL 看起来建了一张表 `docs`,但实际上 SQLite 会调用 fts5 module 的 `xCreate` 方法(`ext/fts5/fts5_main.c` 里那个 `fts5Mod`),`xCreate` 内部又会**建出一堆普通表**(叫**影子表 / shadow tables**)。看 FTS5 的 [`sqlite3Fts5CreateTable`](../sqlite/ext/fts5/fts5_storage.c#L308) 一族函数,会建这些影子表([fts5_storage.c:252-267](../sqlite/ext/fts5/fts5_storage.c#L252) 删表时也能看到名字):

| 影子表(FTS5) | 作用 |
|---|---|
| `docs_data` | 倒排索引本身(segment 的二进制页 + b-tree) |
| `docs_idx` | term → segment 页号的索引(辅助快速定位 term) |
| `docs_content` | 原始文档内容(rowid → 标题/正文),用于高亮/snippet |
| `docs_docsize` | 每篇文档各列的 token 数(用于 snippet 计算) |
| `docs_config` | 配置(key-value,存 schema 版本、detail 模式等) |

FTS3 的影子表略有不同([fts3_write.c:312-337](../sqlite/ext/fts3/fts3_write.c#L312) 能看到 FTS3 用 `%_content`/`%_segments`/`%_segdir`/`%_docsize`/`%_stat`):

| 影子表(FTS3) | 作用 |
|---|---|
| `docs_content` | 原始文档(rowid → 各列文本) |
| `docs_segments` | segment 的二进制 blob(倒排索引的页) |
| `docs_segdir` | segment 目录(level、根页号,描述每个 segment) |
| `docs_docsize` | 每行 token 数 |
| `docs_stat` | 全局统计(总行数、平均 token 数) |

> **钉死这件事(第七钉)**:FTS 表**本身没有数据**,它是一张虚拟表(走 fts5 module 的 C 回调)。**真正的数据藏在它名下的几张影子表里**——这些影子表是**普通 B-tree 表**(用我们 P3-08 讲的那套 B-tree 存),只不过 module 把它们当实现细节藏起来了。**影子表对外只读**(普通 SQL 不能直接改,见后面 xShadowName),只能通过 FTS 表的接口写。

> **为什么影子表是只读保护**:如果你绕过 FTS 接口,直接 `INSERT INTO docs_segments VALUES (...)`,会把倒排索引搞坏(索引和数据不一致)。3.26+ 起,SQLite 用 `xShadowName`([sqlite3.h.in:7704 那张表的 version 3 方法](../sqlite/src/sqlite.h.in#L7704))让扩展声明"哪些表名是影子表",然后这些表默认对外只读(除非开 `SQLITE_DBCONFIG_DEFENSIVE` 之类的开关)。这是 SQLite 的安全护栏——又一次体现"数据完整性靠约束,不靠程序员自觉"。

### 4.3 倒排索引长什么样:term → doclist

FTS 的核心数据结构是**倒排索引(inverted index)**:对每个 token(分词后的词),记录"这个词出现在哪些文档里"。看一张 ASCII 图:

```
   倒排索引(逻辑结构,存在 docs_data / docs_segments 里):

   term        doclist
   ----        ------------------------------------------------
   "apple"   → [ (docid=1, pos=[3,17]),  (docid=5, pos=[22]),  (docid=9, pos=[1,8,40]) ]
   "banana"  → [ (docid=2, pos=[10]),    (docid=5, pos=[5]) ]
   "cherry"  → [ (docid=1, pos=[8]),     (docid=9, pos=[12]) ]
   ...

   每个 doclist 是 (docid, [位置列表]) 的序列,docid 升序。
   MATCH 'apple' → 直接查 "apple" 的 doclist → 命中文档 1, 5, 9
   MATCH 'apple AND cherry' → 求两个 doclist 的交集 → 命中文档 1, 9
```

物理上,这些 doclist 不是一张大表,而是**分成若干 segment**,每个 segment 内部是**有序的(term 升序、docid 升序)**,方便合并和二分。FTS3 的 segment 存为"一或多个 b+-tree in shadow tables"([fts3Int.h:113](../sqlite/ext/fts3/fts3Int.h#L113) 注释原话:`"...stored as one or more b+-trees in the %_segments and %_segdir tables"`)——也就是 segment 的内容在 `docs_segments`(叶子 blob)+ `docs_segdir`(导航)里。FTS5 用了不同的扁平格式(`docs_data` 里分页 + `docs_idx` 辅助),但逻辑一样:term 有序,doclist 跟在 term 后面。

> **承接《LevelDB》**:FTS 的 segment + 合并,跟《LevelDB》讲的 LSM 的 segment + compaction **是同一种思想**——写时先攒到内存,满了 flush 成一个有序 segment;segment 多了就后台合并(level 升一级)。这就是为什么 FTS 写一篇文章不慢(增量写、定期合并)、查询要合并多个 segment(读放大)的原因。**倒排索引的存储,本质是一个 LSM-flavored 的有序字符串表**——又一个跨系统的承接。

### 4.4 写一篇文档:tokenize + 写倒排 + 写影子表

你执行 `INSERT INTO docs(title, body) VALUES ('Hello World', 'SQLite is fast')`。这条 INSERT 走的不是普通表的 `Insert` opcode(因为 FTS 表没 B-tree),而是 VDBE 发的 `VUpdate` opcode(虚拟表写),它调到 fts5 module 的 `xUpdate` 方法。`xUpdate` 内部做这几件事:

1. **拿原文**:从 `docs_content`(或内存)拿 `title`/`body` 的文本。
2. **tokenize(分词)**:调配置好的 tokenizer(默认 `unicode61`,按 Unicode 词边界切词;还有 `porter`(英文词干)、`trigram`、自定义等)。"Hello World" → `["hello", "world"]`(小写化),"SQLite is fast" → `["sqlite", "is", "fast"]`(停用词是否过滤看配置)。
3. **写倒排**:对每个 token,在内存里的 hash table(增量 buffer)追加 `(token, docid, col, pos)`。
4. **写影子表**:`docs_content` 插一行(存原文),`docs_docsize` 插一行(存 token 数),`docs_config`/`docs_stat` 更新统计。倒排数据先在内存攒着,**等事务提交时(或攒满了)再 flush 成一个新 segment**,写进 `docs_data`(FTS5)或 `docs_segments`+`docs_segdir`(FTS3)。

> **承接《LevelDB》《RocksDB》**:这个"先攒内存 hash,满了 flush 成有序 segment,segment 多了合并"——和 LevelDB 的 MemTable→SSTable、RocksDB 的 memtable→flush→compaction **完全是同一套机制**。FTS5 的 [`Fts5Hash`](../sqlite/ext/fts5/fts5_hash.c)(内存倒排)、`Fts5Structure`(segment 层级,[fts5Int.h:194](../sqlite/ext/fts5/fts5Int.h#L194) 的 `nCrisisMerge` 是合并阈值)、自动 merge,跟 LSM 的术语几乎一一对应。

### 4.5 查询:MATCH 走 xFilter,合并多个 segment

`SELECT title FROM docs WHERE docs MATCH 'sqlite' ORDER BY rank`:

1. parser 看到 `MATCH` + `docs`(虚拟表),codegen 产 `VFilter` opcode,把 `MATCH 'sqlite'` 这个约束传给 module 的 `xFilter`。
2. fts5 module 的 `xFilter`:解析 `MATCH` 表达式(`ext/fts5/fts5_expr.c` 有个专门的查询表达式 parser,支持 `AND`/`OR`/`NEAR`、短语、列限定),拿到要查的 term(`"sqlite"`),**到每个 segment 里查这个词的 doclist**(`ext/fts5/fts5_index.c`),然后把多个 segment 的 doclist **合并**(因为同一个词可能散在新老 segment 里,合并后才是完整命中列表)。
3. module 通过游标(`VOpen`/`VNext`/`VColumn`/`VRowid` 对应 `xOpen`/`xNext`/`xColumn`/`xRowid`)把命中文档一行行吐回 VDBE。
4. VDBE 拿到行后,继续执行外层的 `ORDER BY rank`(rank 是 fts5 提供的辅助函数 BM25 评分)和 `title` 列输出。

整个过程,**外层 SELECT 看到的是一张普通的表**(有游标、能 MATCH、能 ORDER BY),但底层完全是 fts5 module 的 C 代码在调度倒排索引。**虚拟表的精髓就是这种"接口是表,实现是 C"的隔离**。

### 4.6 FTS 的虚拟表注册:fts5 module

最后看一下 fts5 怎么把自己注册成一个 module([`ext/fts5/fts5_main.c:3814`](../sqlite/ext/fts5/fts5_main.c#L3814)):

```c
rc = sqlite3_create_module_v2(db, "fts5", &fts5Mod, p, fts5ModuleDestroy);
```

`fts5Mod` 是个 `sqlite3_module` 结构体,里面填满了 fts5 自己实现的 `xCreate`/`xConnect`/`xBestIndex`/`xUpdate`/`xFilter`/... 一长串方法。FTS3 类似([`ext/fts3/fts3.c:4190`](../sqlite/ext/fts3/fts3.c#L4190) 那一族 `sqlite3_create_module_v2`)。**FTS3 和 FTS5 就是两个各自实现了 `sqlite3_module` 的 C 扩展**——这是虚拟表机制的胜利:全文搜索引擎这么复杂的特性,SQLite 内核一行没改,纯靠"注册一个 module"就接进来了。

> **承接**:FTS 是 SQLite 里"用虚拟表挂载自定义存储"的最大例子。其他虚拟表扩展还有 **rtree**(R-树,空间索引,`ext/rtree/`)、**序列化 KV**、**JSON 表函数**等。它们共用同一套 `sqlite3_module` 机制——**SQLite 的可扩展性,靠的就是这个"逃生舱"**。

---

## 五、技巧精解:两个最硬核的设计

本章挑两个最硬核的技巧单独拆透。

### 技巧一:触发器编译成 sub-program + `OP_Program` 调用(本章最核心)

这是本章最值得钉死的技巧。我们再深挖一层:**为什么把触发器体编成 sub-program,而不是 inline 进主语句的 opcode 流?**

inline 的做法是:主 INSERT 的 opcode 流里,在 BEFORE 位置直接展开"触发器体的 opcode"(把 `INSERT INTO audit ...` 的 opcode 直接拼进主流),AFTER 位置再拼一份。看起来更简单(没有 `OP_Program`,没有 sub-program 切换),为什么不这么做?

> **不这么写会怎样(反面)**:
> 1. **递归触发器没法处理**:如果触发器体里又 INSERT 了同一张表(触发自己),inline 的 opcode 流会无限展开(编译期就炸)。sub-program 模型下,VDBE 执行 `OP_Program` 时有递归深度计数(就是 [`sqlite3CodeRowTriggerDirect`](../sqlite/src/trigger.c#L1396) 里那个 `bRecursive` 和 P5),到了深度就停(`SQLITE_RecTriggers` 开关控制)。
> 2. **触发器复用不了**:同一个触发器可能被多条不同的主语句触发(insert/update/delete/再嵌套),inline 要在每个主语句里都编一份触发器体;sub-program 模式下,触发器体只编一次(`getRowTrigger` 会缓存,见 [trigger.c:1356](../sqlite/src/trigger.c#L1356) 注释里"Return a pointer to a TriggerPrg object containing the sub-program for trigger p"),多处复用。
> 3. **`OLD.*`/`NEW.*` 语义不清**:inline 的话,`old.id`/`new.id` 引用的是哪一行的 OLD/NEW?如果主语句一次改 100 行,触发器体执行 100 次,每次的 OLD/NEW 都不同。sub-program 模式下,每次调用 `OP_Program` 把当前行的 OLD/NEW 装进约定的寄存器组([trigger.c:1396 上面的注释](../sqlite/src/trigger.c#L1396)那张表),sub-program 引用固定的"寄存器相对地址",清爽。

`OP_Program` 在 VDBE 里的实现([vdbe.c:7535](../sqlite/src/vdbe.c#L7535))是个精巧的设计:它**复用同一个 VDBE 虚拟机**(那个大 switch-case 循环),只是切换了"当前执行的 opcode 数组指针"和"寄存器帧"——本质上像一个函数调用(保存现场、跳到子程序、子程序执行完 `OP_Return` 回来、恢复现场)。没有第二个虚拟机,没有线程切换,开销极低(就是几次寄存器赋值)。

> **钉死这件事**:sub-program 设计 = **"用 opcode 的切换,模拟函数调用"**。SQLite 没有在 VDBE 里引入"函数"概念,而是用"切 opcode 数组 + 寄存器帧"这一招,既支持触发器,又支持递归触发器,还顺带支持了外键动作(foreign key cascade,也走 `OP_Program`——你看 [`sqlite3CodeRowTriggerDirect`](../sqlite/src/trigger.c#L1396) 注释里那句 `"Call: %s.%s"` 里 `p->zName` 可能是 NULL 表示 fkey)。**一个机制,三种用途**——这是"统一执行模型"的最大红利。

### 技巧二:视图"宏展开"——不存数据,只存定义

视图的技巧在于它的**轻**:不存数据、没有专属 opcode、没有专属执行路径,纯靠 parser/codegen 层面的一次"展开"。

> **不这么写会怎样(反面)**:
> 1. **物化视图**:像 Oracle materialized view 那样真存一份数据副本,查询快,但**更新要保持同步**(底层表改了,视图副本要刷新)——这套同步机制(增量刷新、定期刷新、查询时重写)极其复杂,嵌入式数据库扛不住。SQLite 选"不物化",**一致性免费**(每次查都从底层实时算)、**存储免费**(不占空间),代价是每次查询要重新算(但 SQLite 是嵌入式、数据量通常不大,这点计算开销可接受)。
> 2. **专属 opcode**:给视图设计一套 `OpenView`/`ViewFilter`/`ViewNext` opcode。但这违背 SQLite 的统一执行模型——视图展开后就是子查询,子查询有现成的执行机制,何必另搞一套?**展开成子查询,等于免费复用了 P2-07 讲的整套子查询执行机制**(包括子查询物化、相关子查询、CTE 递归)。

视图展开的位置在 [`select.c:6054`](../sqlite/src/select.c#L6054) 的 `sqlite3SrcItemAttachSubquery(pParse, pFrom, pTab->u.view.pSelect, 1)`——这一行就是"视图名 → 子查询"的**单点展开**。整个 SQLite 里,视图的处理就靠这一行(加上 [`sqlite3ViewGetColumnNames`](../sqlite/src/build.c#L3229) 算列名)。**简单到几乎看不见,却是整个视图机制的命门**。

> **钉死这件事**:视图是 SQLite 里"最省事的特性"——一行 `sqlite3SrcItemAttachSubquery` 把视图名替换成它的 SELECT,剩下全交给已有的子查询机制。**这是"统一执行模型"的另一个红利:新特性只要能翻译成已有机制,就免费**。对照 MySQL(视图有专门的 query rewrite 层、有 merge/temptable 两种算法),SQLite 的做法极简,但够用——这正是嵌入式哲学。

---

## 六、章末小结

### 回扣主线

本章三个特性,看似不搭界,其实都在呼应全书主线——**SQLite 把一切 SQL 特性,都编译成 VDBE opcode、用虚拟机执行**:

| 特性 | 怎么落到 opcode |
|---|---|
| **触发器** | 触发器体编成 sub-program,主语句在 BEFORE/AFTER 时机用 `OP_Program` opcode 调用 |
| **视图** | parser 把视图名展开成它的 SELECT(作为子查询),展开后按普通子查询编 opcode |
| **FTS/虚拟表** | VDBE 发 `VOpen`/`VFilter`/`VNext`/`VColumn`/`VUpdate` 等虚拟表 opcode,调 C 扩展的 module 方法 |

三件事**没有一个引入新的执行模型**。触发器没有"事件运行时",视图没有"视图专属 opcode",FTS 没有"FTS 虚拟机"。SQLite 的执行模型从头到尾只有一个:**VDBE opcode 循环**(承《Lua》虚拟机)。再复杂的 SQL 特性,最后都坍缩成这个循环里跳动的 opcode。**这就是 SQLite "VDBE 统一执行模型"的最大胜利**——它让 SQLite 的内核保持极简(一个执行引擎),却撑得起完整 SQL 语法 + 全文搜索 + 空间索引 + 任意自定义存储。

> 服务二分法的哪一面:三件事都在 **编译与执行** 这一侧——触发器是"编译期编 sub-program",视图是"编译期(parser)展开",FTS 虚拟表是"执行期 VDBE 调 module 回调"。它们的存储侧(影子表是普通 B-tree)用 P3-08/P4 已有的机制,本章没引入新存储。

### 五个为什么

1. **为什么触发器体要编成 sub-program,而不是 inline 进主语句?**——递归触发器需要深度控制、同一触发器要被多处复用、OLD/NEW 寄存器语义更清晰;sub-program + `OP_Program` 用"切 opcode 数组"模拟函数调用,一个机制同时支持触发器、递归触发器、外键 cascade。
2. **为什么视图不物化(默认)?**——一致性免费(每次实时算)、存储免费(不占空间);嵌入式场景数据量不大、计算开销可接受。物化的同步复杂度,嵌入式扛不住。
3. **为什么 FTS 用虚拟表而不是普通表?**——FTS 的存储是倒排索引(多 segment + 合并),根本不是 B-tree 一行一记录的模型;虚拟表让 FTS 用 C 实现"自己的存储",外表保持"是一张表"的 SQL 接口。这是 SQLite 的逃生舱。
4. **为什么 FTS 的影子表对外只读?**——直接改影子表会破坏倒排索引与原文的一致性(索引坏了查不出结果)。`xShadowName` 让 module 声明影子表名,SQLite 默认拒绝普通 SQL 改它们——数据完整性靠约束。
5. **为什么触发器/视图/FTS 最后都被 opcode 统一掉?**——SQLite 的设计哲学是"一个执行引擎(VDBE)+ 编译器把一切翻译成 opcode"。这让内核极简(不用为每个特性写执行运行时)、新特性只要能翻译成已有机制就免费(视图复用子查询)、跨特性组合自然(视图上挂触发器、FTS 表和普通表 JOIN)。

### 想继续深入往哪钻

- **触发器**:`src/trigger.c` 整个文件(本章主线)、[`getRowTrigger`/`codeRowTrigger`](../sqlite/src/trigger.c#L1228)(sub-program 编译)、`OP_Program` 在 [`vdbe.c:7535`](../sqlite/src/vdbe.c#L7535) 的实现;SQLite 官方文档 "SQLite Trigger Support"。
- **视图**:`src/select.c` 的 `sqlite3SrcListLookup` 一带(select.c:6052 视图展开单点)、`src/build.c` 的 [`sqlite3CreateView`](../sqlite/src/build.c#L3009) 和 [`sqlite3ViewGetColumnNames`](../sqlite/src/build.c#L3229);SQLite 官方文档 "CREATE VIEW" + "Query Planner"(视图怎么影响计划)。
- **FTS5(现代首选)**:`ext/fts5/fts5_main.c`(module 注册)、`ext/fts5/fts5_index.c`(segment 查询/合并)、`ext/fts5/fts5_expr.c`(MATCH 表达式 parser)、`ext/fts5/fts5_storage.c`(影子表);SQLite 官方文档 "FTS5 Extension"(最权威)。
- **FTS3**:`ext/fts3/fts3.c`(总入口)、`ext/fts3/fts3_write.c`(segment 写/合并)、`ext/fts3/fts3Int.h`(数据结构注释)。
- **虚拟表机制**:`src/vtab.c` 整个文件、`sqlite3.h.in:7704` 的 `sqlite3_module` 结构体;SQLite 官方文档 "Virtual Table Mechanism"。
- **动手感受**:`sqlite3` CLI 建一张带 BEFORE/AFTER 触发器的表,`EXPLAIN INSERT ...` 看 `OP_Program` 在主 INSERT 前后各有一条;`EXPLAIN SELECT * FROM 视图` 看里面没有"打开视图"的 opcode;`CREATE VIRTUAL TABLE t USING fts5(...)` 后 `.schema` 看一堆影子表。

### 引出下一章

我们讲完了触发器、视图、FTS——它们都印证了"SQL 特性最后都被 VDBE opcode 统一"。但还有一个"opcode 复用"的核心场景没讲:**SQL 编译一次、执行多次**——也就是 prepared statement。一条 SQL,编译成 opcode 后缓存起来,反复 bind 不同参数执行,极快。下一章 P6-20,我们拆 prepared statement 的编译/缓存/bind 机制,看 SQLite 怎么把"编译成 opcode"这一步的代价摊薄到多次执行上——这是 SQLite 高性能的关键之一,也直接呼应全书主线"编译成字节码 + 虚拟机执行"。

> **下一章**:P6-20 · prepared statement 与复用。
