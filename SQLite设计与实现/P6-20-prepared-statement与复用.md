# 第 6 篇 · 第 20 章 · prepared statement 与复用

> **核心问题**:前 19 章我们把一条 `SELECT` 怎么编译成 VDBE opcode、怎么用虚拟机执行、怎么存进单文件 B-tree、怎么用 pager/WAL 保 ACID,完整地走了一遍。可有一个性能问题始终悬在头上——**SQL 每执行一次,都要把"字符串 → tokenize → parse → codegen → opcode"这一整条编译链路跑一遍吗?** 同一个查询在循环里跑一万次(比如 `WHERE id=?`,绑定不同的 id),难道要 tokenize 一万次、parse 一万次、生成 opcode 一万次?那 token 表、LALR(1) parser 状态机、Code Generator 遍历 AST 的开销,每一项都要付一万遍。prepared statement 就是 SQLite 对这个问题的回答:**把"字符串 → opcode"这一整段编译只做一次,把产物(一段 opcode 流 + 它的寄存器骨架)缓存下来;之后绑定不同参数、反复执行,执行的只是那段已经编好的 opcode,编译一次都不用再做**。这一章拆的就是这个"一次编译、多次执行"的复用机制——它怎么落到源码里、为什么这么快、bind/step/reset 三个动作各自在干什么。
>
> **读完本章你会明白**:
> 1. ★ **prepared statement 为什么快**:`sqlite3_prepare_v2` 把 SQL **一次性**编译成 opcode 流存进 `sqlite3_stmt`(其实就是 `Vdbe` 结构体),之后 `bind → step → reset → 再 bind → 再 step` 反复执行时,**编译只发生过一次**,tokenize/parse/codegen 的开销被摊薄到海量执行上——这是 SQLite 高性能最直接的一个来源。
> 2. ★ **参数绑定(?/ ?N / :name / @name / $name)怎么实现**:四种占位符在 parse 阶段都被翻译成一个**变量编号**,绑定时值进 `aVar[]` 数组,执行时一条 `OP_Variable` opcode 把绑定值拷进工作寄存器——绑定与编译彻底解耦(编译不知道值是多少,执行才知道)。
> 3. ★ **reset 的精妙**:`sqlite3_reset` **不重新编译 opcode**,它只做三件事——重置 `pc`(程序计数器)、释放游标和非持久寄存器、把状态机推回 `VDBE_READY_STATE`。opcode 流原封不动,所以 reset+再 step 比重新 prepare 快几个数量级。
> 4. **finalize vs reset 的边界**:reset 让语句"回到起跑线"准备再跑一次(保留 opcode);finalize 是"销毁 Vdbe、释放 opcode 数组本身"。一个复用、一个回收,边界在"opcode 还要不要"。
>
> **和 P1-02 的分工**(重要,先说清):P1-02《架构全景八层流水线》第 2 章已经讲过 prepared statement 的**对象生命周期总论**——`sqlite3_stmt` 就是 `Vdbe` 的别名、prepare/step/finalize 三段式分别在八层流水线的哪几层干活、为什么是 `VDBE_INIT/READY/RUN/HALT` 四态状态机。**本章不重复生命周期总论**,而是从"**复用机制和性能 why**"的角度深挖:bind 怎么把值送进寄存器、`OP_Variable` 怎么把它取出来、reset 凭什么"不重编译就能再跑"、`sqlite3_exec` 每次 prepare+finalize 为什么慢。如果你还没读过 P1-02 的八层流水线,先翻回去扫一眼那张图,本章所有函数调用都落在那八层的某一层上。

> **如果一读觉得太密**:先抓住四句话——① prepare 把 SQL 编译成 opcode 缓存进 `Vdbe`;② bind 把值填进 `aVar[]` 数组;③ step 跑 opcode,跑到 `OP_Variable` 时从 `aVar[]` 取值进工作寄存器;④ reset 把 `pc` 归零、清游标和寄存器,**opcode 不动**,于是能再 bind/step。本章主线就是这句:**编译只发生一次,bind/step/reset 在已经编好的 opcode 上反复跑**。

---

## 〇、一句话点破

> **prepared statement 是 SQLite 把"SQL 字符串 → opcode 流"这条编译链路的代价一次性付清、之后反复执行的机制:prepare 做一次完整编译,产出 opcode + 寄存器骨架存进 `Vdbe`;bind 把参数值填进 `aVar[]` 绑定数组;step 跑 opcode,跑到 `OP_Variable` 时把绑定值取出来用;reset 把 `pc` 归零、清掉游标和非持久寄存器,opcode 原封不动。于是同一句 SQL 反复执行,编译只付一次,bind/step/reset 全在已经编好的 opcode 上玩——这就是"快"的全部秘密。**

这是结论,不是理由。本章倒过来拆:先看"每次重新编译"的朴素模型有多贵,把 prepared 的动机立起来;再把 prepare 一次编译做了什么、bind/step/reset 三步各自做什么,逐层落到源码函数和 opcode 上;然后用一张时序图把 `prepare → bind → step → reset → 再 bind → 再 step → finalize` 的完整生命周期串起来;最后用一个性能对照表和一节"技巧精解",钉死"为什么 reset 不重编译"这个最容易被讲错的关键点。

> **本章归属二分法的哪一面**:**编译与执行**。prepared statement 服务的本质是把"编译"和"执行"切成两个时间不重叠的阶段——prepare 全在编译前端(Tokenizer/Parser/Code Generator)干活,bind/step/reset 全在执行器(VDBE)干活。这一章是"编译与执行"这一面的收束章:它讲清了"编译"的产物(opcode + 寄存器骨架)怎么被"执行"反复消费,正好把 P6-19(触发器/视图/FTS 都被编成 opcode)的"opcode 复用"主题接到 P7-21 全书收束。

---

## 一、先看"每次重新编译"有多贵:prepared 的动机

要理解 prepared statement 为什么值得专门拿一章讲,得先看**没有它会怎样**。SQLite 提供一个最简单的执行接口叫 `sqlite3_exec`——传一句 SQL 字符串、给一个回调、它就把结果一行行喂给你的回调。它的全部源码只有一百多行([sqlite3_exec](../sqlite/src/legacy.c#L30)),核心就是一个 `while` 循环:

```c
while( rc==SQLITE_OK && zSql[0] ){
    rc = sqlite3_prepare_v2(db, zSql, -1, &pStmt, &zLeftover);   // 编译
    while( 1 ){
      rc = sqlite3_step(pStmt);                                   // 执行
      /* ... 回调 ... */
      if( rc!=SQLITE_ROW ){
        rc = sqlite3VdbeFinalize((Vdbe *)pStmt);                  // 销毁
        break;
      }
    }
}
```

注意看这三个动作的顺序:**prepare(编译)→ step(执行)→ finalize(销毁)**——`sqlite3_exec` 把这三步捏死在一条语句的生命周期里,跑完就销毁。`legacy.c` 第 111 行那句 `sqlite3VdbeFinalize` 尤其关键:它把刚编译出来的 opcode 数组整个释放掉。换句话说,`sqlite3_exec` 每跑一次,这段 opcode 就被造出来一次、用一次、扔掉。

> **不这样会怎样(把 `sqlite3_exec` 用在循环里)**:想象一个最朴素的高频场景——批量按 id 查用户名:

```c
for( int i=0; i<10000; i++ ){
  char sql[128];
  sqlite3_snprintf(sizeof(sql), sql, "SELECT name FROM users WHERE id=%d", ids[i]);
  sqlite3_exec(db, sql, callback, 0, 0);  // 每次都 prepare + step + finalize
}
```

每一轮循环都要付**一整条编译链路的开销**:

| 阶段 | 干的事 | 每轮付出的代价 |
|------|--------|----------------|
| Tokenizer | 把 SQL 字符串切成 token(`sqlite3GetToken`,逐字符状态机) | O(SQL 长度)次字符扫描 + token 分类 |
| Parser | LALR(1) 状态机吃 token、建 AST(Lemon 生成的 `sqlite3Parser` 循环) | 每个 token 一次状态转移 + 归约时建 Expr/Select 节点(堆分配) |
| Code Generator | 遍历 AST、产出 opcode(`sqlite3Select`/`sqlite3Insert`/…) | 遍历整棵 AST + `sqlite3VdbeAddOp` 往 opcode 数组里塞指令(可能 realloc) |
| 内存分配 | Parse 上下文、Expr 节点、opcode 数组、Vdbe 结构体本身 | 若干次 `sqlite3Malloc`(走 lookaside 或 heap) |
| schema 锁检查 | `sqlite3BtreeSchemaLocked` 确认 schema 没被改 | 每库一次 mutex + 检查 |
| `sqlite3VdbeMakeReady` | 分配寄存器数组 `aMem[]`、绑定数组 `aVar[]`、游标数组 `apCsr[]` | 一次批量内存分配 |

这六项每一项都是**纯编译开销**,和真正"读数据"半点关系没有。1 万次循环就是 1 万遍这六项——这些 CPU 周期全花在"重复翻译同一句(只是参数不同的)SQL"上,根本没有去碰 B-tree。

> **所以这样设计**:prepared statement 把这六项编译开销**只付一次**,之后 1 万次执行只跑 opcode,再不过 Tokenizer/Parser/Code Generator。改成 prepared 写法:

```c
sqlite3_stmt *stmt;
sqlite3_prepare_v2(db, "SELECT name FROM users WHERE id=?", -1, &stmt, 0);  // 编译只 1 次
for( int i=0; i<10000; i++ ){
  sqlite3_bind_int(stmt, 1, ids[i]);   // 填参数
  while( sqlite3_step(stmt)==SQLITE_ROW ){ /* 取行 */ }
  sqlite3_reset(stmt);                  // 重置,准备下一轮
}
sqlite3_finalize(stmt);                 // 循环结束才销毁
```

编译只在 `prepare` 那一刻发生一次,循环里只剩 `bind/step/reset`——这三步全是在**已经编好的 opcode** 上操作,代价是 reset 那一下清寄存器/游标(常数级),加上 step 跑 opcode(只和 B-tree 的页数有关)。SQLite 官方文档的原话就是:**"prepare the SQL statement once, then execute it many times with different parameter values"**——这就是 prepared statement 的全部价值主张。

> **钉死这件事**:prepared statement 不是"语法糖"也不是"缓存层",它是把**编译链路的代价从 O(执行次数) 降到 O(1)** 的机制。1 万次循环里,朴素写法编译 1 万次、执行 1 万次;prepared 写法编译 1 次、执行 1 万次。节省的是那 9999 遍 tokenize/parse/codegen——而这些恰是 SQL 执行里最贵的几步。

---

## 二、prepare 一次编译做了什么:把 SQL 变成 opcode + 寄存器骨架

`sqlite3_prepare_v2` 是 prepare 的公开入口。我们跟着源码链路走一遍,看它一次编译到底产出了什么。

### 2.1 调用链:`sqlite3_prepare_v2` → `sqlite3LockAndPrepare` → `sqlite3Prepare`

公开 API `sqlite3_prepare_v2` 只有一行实质逻辑([sqlite3_prepare_v2](../sqlite/src/prepare.c#L943)):

```c
int sqlite3_prepare_v2(
  sqlite3 *db, const char *zSql, int nBytes,
  sqlite3_stmt **ppStmt, const char **pzTail
){
  rc = sqlite3LockAndPrepare(db, zSql, nBytes, SQLITE_PREPARE_SAVESQL, 0,
                             ppStmt, pzTail);
  return rc;
}
```

注意它传了一个 `SQLITE_PREPARE_SAVESQL` 标志——这个标志要求**把原始 SQL 文本存进 Vdbe**,这是后面 schema 变更时能自动 recompile 的前提(`prepare.c` 第 956 行,注释里说 "the original SQL text is retained")。`sqlite3_prepare_v3` 比它多一个 `prepFlags` 参数,本质上也是调 `sqlite3LockAndPrepare`(`prepare.c` 第 977 行)。

`sqlite3LockAndPrepare`([sqlite3LockAndPrepare](../sqlite/src/prepare.c#L842))做两件事:**加锁**(锁住所有 B-tree 的 mutex,因为编译期要读 schema),然后调 `sqlite3Prepare` 做真正的编译。它还有一个**重试循环**:

```c
do{
  rc = sqlite3Prepare(db, zSql, nBytes, prepFlags, pOld, ppStmt, pzTail);
  ...
}while( (rc==SQLITE_ERROR_RETRY ...)
     || (rc==SQLITE_SCHEMA && (sqlite3ResetOneSchema(db,-1), cnt)==1) );
```

这个循环是为了处理**编译期 schema 被改**的极端情况——如果编译到一半发现 schema cookie 不对(`SQLITE_SCHEMA`),就重置 schema 缓存、再编译一次,最多重试一次。正常情况下这个循环只跑一遍。

真正的编译在 `sqlite3Prepare`([sqlite3Prepare](../sqlite/src/prepare.c#L688))。它做的事情按顺序:

1. **初始化 Parse 上下文**(`sParse`),这是编译期的"工作台",tokenizer/parser/codegen 都往这里塞中间产物(`prepare.c` 第 701-707 行)。
2. **检查 schema 锁**——遍历所有 database(主库 + ATTACH 的库),对每个 B-tree 调 `sqlite3BtreeSchemaLocked` 确认没有别的连接正在改 schema(`prepare.c` 第 753-767 行)。这一步保证编译用的是**一致**的 schema 快照。
3. **调 `sqlite3RunParser`**——这是编译链路的主入口([sqlite3RunParser](../sqlite/src/tokenize.c#L600)),它里面就是那个著名的 `while(1)` 循环:`sqlite3GetToken` 切一个 token,喂给 Lemon 生成的 `sqlite3Parser(pEngine, tokenType, …)` 状态机。状态机按 SQL 文法归约时,**调 Code Generator**(`sqlite3Select`/`sqlite3Insert`/…)产出 opcode,opcode 被 `sqlite3VdbeAddOp` 塞进 Vdbe 的 `aOp[]` 数组。整个 tokenize+parse+codegen 全在这一趟循环里完成。
4. **把 SQL 文本存进 Vdbe**——`sqlite3VdbeSetSql(sParse.pVdbe, zSql, …, prepFlags)`(`prepare.c` 第 801 行),这一步是 recompile 能力的基础。
5. **`sqlite3VdbeMakeReady`**——这一步把 Vdbe 从"编译期"切到"可执行期",下面单说。
6. **返回 `*ppStmt = (sqlite3_stmt*)sParse.pVdbe`**(`prepare.c` 第 824 行)——注意这个 cast,`sqlite3_stmt` 和 `Vdbe` 是同一个东西的两个名字。

### 2.2 `sqlite3VdbeMakeReady`:把 opcode 流"打包"成可执行 Vdbe

prepare 最后一步 `sqlite3VdbeMakeReady` 是个关键函数([sqlite3VdbeMakeReady](../sqlite/src/vdbeaux.c#L2654))。它在编译产物(Vdbe,此刻 `eVdbeState==VDBE_INIT_STATE`,opcode 已经填好但寄存器/游标还没分配)的基础上,**一次性把执行所需的所有数据结构分配好**:

```c
nVar = pParse->nVar;          // 绑定参数个数(由 parser 数 ? 和 :name 得来)
nMem = pParse->nMem;          // 工作寄存器个数(由 codegen 累加)
nCursor = pParse->nTab;       // 游标个数
nArg = pParse->nMaxArg;       // 虚拟表 xFilter/xUpdate 最大参数
...
p->aMem = allocSpace(&x, 0, nMem*sizeof(Mem));       // 工作寄存器数组
p->aVar = allocSpace(&x, 0, nVar*sizeof(Mem));       // ★ 绑定参数数组
p->apArg = allocSpace(&x, 0, nArg*sizeof(Mem*));     // 虚拟表参数数组
p->apCsr = allocSpace(&x, 0, nCursor*sizeof(VdbeCursor*));  // 游标指针数组
...
initMemArray(p->aVar, nVar, db, MEM_Null);           // 绑定数组初始化成 NULL
initMemArray(p->aMem, nMem, db, MEM_Undefined);      // 工作寄存器初始化成 Undefined
```

这里有三个细节特别值得讲:

> **不这样会怎样(分配策略)**:`MakeReady` 的内存分配有一个**两趟复用**的小技巧(`vdbeaux.c` 第 2719-2733 行)。第一趟先看 opcode 数组尾部有没有空闲内存(`x.pSpace = &((u8*)p->aOp)[n]`,opcode 数组分配时通常会多留一些),能复用就复用,复用不完才在第二趟整体 `sqlite3DbMallocRawNN` 再分配一块。注释里明说:**"This two-pass approach that reuses as much memory as possible from the leftover memory at the end of the opcode array. This can significantly reduce the amount of memory held by a prepared statement."** 这是个为嵌入式场景省内存的精打细算——同一个 Vdbe 的 opcode 数组、寄存器数组、绑定数组、游标数组**共用一块连续内存**,而不是各 malloc 各的。嵌入式场景(V8 isolate、iOS app)每个 prepared statement 都省一点内存,加起来很可观。

> **钉死这件事**:`aVar[]` 这个数组的长度 `nVar`,是 **parser 在编译期数出来的**——SQL 里有几个 `?`/`:name`/`@name`/`$name`,parser 就把 `pParse->nVar` 加几次(下面 §3 讲细节)。也就是说,**prepare 一返回,绑定数组的大小就固定了**,后面 bind 多了报 `SQLITE_RANGE`、bind 少了那些位置保持 NULL。这是"编译与执行解耦"的物理体现:编译期决定绑定数组有多大,执行期只往里填值。

`MakeReady` 最后还会把状态机推到 `VDBE_READY_STATE`(通过紧接着的 `sqlite3VdbeRewind`,见 `vdbeaux.c` 第 2750 行),`pc` 设成 -1。从此这个 Vdbe 就处于"准备就绪、随时可 step"的状态。

> **状态机小复习(承 P1-02)**:`Vdbe.eVdbeState` 有四态——`VDBE_INIT_STATE`(0,编译期)、`VDBE_READY_STATE`(1,就绪)、`VDBE_RUN_STATE`(2,执行中)、`VDBE_HALT_STATE`(3,执行完待 reset/finalize),定义在 `vdbeInt.h` 第 534-537 行。prepare 完成后停在 READY;step 把它推到 RUN,执行完推到 HALT;reset 把它从 HALT 推回 READY;finalize 把它整个销毁。**整个 prepared statement 的复用,就是这个状态机在 READY↔RUN↔HALT 之间反复跳**,opcode 数组从头到尾不动。P1-02 已讲过状态机全貌,这里只点出"复用靠状态机跳、opcode 不动"这一句。

---

## 三、参数绑定:从 `?`、`?N`、`:name` 到 `OP_Variable`

参数绑定是 prepared 复用的灵魂——如果一条 SQL 只能跑固定的字面量,那就没法反复执行(每次值都不同)。SQLite 支持四种占位符:

| 占位符 | 例子 | 编号规则 |
|--------|------|----------|
| `?` | `WHERE id=?` | 按出现顺序自动编号 `?1 ?2 ?3 …` |
| `?NNN` | `WHERE id=?5 AND age>?2` | 显式编号(NNN 是数字) |
| `:name` | `WHERE id=:uid` | 按名字映射,同名复用同编号 |
| `@name` / `$name` | `WHERE id=@uid` | 同上(只为兼容不同语言习惯) |

这四种占位符在 SQLite 内部**最后都被归一成一个正整数编号**(变量号),绑定值就按这个编号进 `aVar[]` 数组。归一逻辑在 parser 那一侧,具体在 `sqlite3ExprAssignVarNumber`([sqlite3ExprAssignVarNumber](../sqlite/src/expr.c#L1319)):

```c
if( z[1]==0 ){
  /* Wildcard of the form "?".  Assign the next variable number */
  assert( z[0]=='?' );
  x = (ynVar)(++pParse->nVar);                  // "?" 按出现顺序自动加
}else{
  if( z[0]=='?' ){
    /* Wildcard of the form "?nnn".  Convert "nnn" to an integer */
    i64 i; ...
    bOk = 0==sqlite3Atoi64(&z[1], &i, n-1, SQLITE_UTF8);  // ?NNN 解析数字
    ...
    x = (ynVar)i;
    if( x>pParse->nVar ){
      pParse->nVar = (int)x;                    // ?NNN 直接用 NNN,且抬高 nVar 上限
      doAdd = 1;
    }else if( sqlite3VListNumToName(pParse->pVList, x)==0 ){
      doAdd = 1;
    }
  }else{
    /* Wildcards like ":aaa", "$aaa" or "@aaa".  Reuse the same variable
    ** number as the prior appearance of the same name, or if the name
    ** has never appeared before, reuse the same variable number */
    x = (ynVar)sqlite3VListNameToNum(pParse->pVList, z, n);  // 先查名字表
    if( x==0 ){
      x = (ynVar)(++pParse->nVar);               // 名字第一次出现,新编号
      doAdd = 1;
    }
    /* 否则 x 沿用同名变量的编号——这是 ":name 多次出现复用同编号" 的实现 */
  }
  if( doAdd ){
    pParse->pVList = sqlite3VListAdd(db, pParse->pVList, z, n, x);  // 记进名字表
  }
}
pExpr->iColumn = x;   // ★ 编号记进 AST 节点的 iColumn
```

> **钉死这件事(四种占位符的归一)**:这段代码把四种占位符**在 parse 期全部归一成一个 `i64` 编号**,记进 AST 节点的 `pExpr->iColumn` 字段。从此往后,`:uid`、`@uid`、`$uid` 这种"有名字的"占位符,名字只存在一张叫 `pVList` 的小表里(`util.c` 的 `sqlite3VList*` 系列函数,本质是个 `[name, number]` 对的数组),给 `sqlite3_bind_parameter_index("uid")` 反查名字→编号用。AST 上**只剩编号**,名字不再参与执行。这就是为什么 `sqlite3_bind_int(stmt, idx, val)` 的 `idx` 永远是个正整数——不管占位符原本是 `?` 还是 `:name`,bind 时都用编号。

### 3.1 Code Generator 为每个占位符产一条 `OP_Variable`

占位符在 AST 里是 `TK_VARIABLE` 节点(`tokenize.c` 把 `?`/`:name` 等都 tokenize 成 `TK_VARIABLE`)。Code Generator 遇到这种节点,只发一条 opcode([expr.c](../sqlite/src/expr.c#L5165)):

```c
case TK_VARIABLE: {
  assert( !ExprHasProperty(pExpr, EP_IntValue) );
  assert( pExpr->u.zToken!=0 );
  assert( pExpr->u.zToken[0]!=0 );
  sqlite3VdbeAddOp2(v, OP_Variable, pExpr->iColumn, target);   // ★ 发 OP_Variable
  return target;
}
```

`sqlite3VdbeAddOp2(v, OP_Variable, pExpr->iColumn, target)` 意思是:**发一条 `OP_Variable` opcode,它的 P1 = 占位符编号(`iColumn`),P2 = 目标寄存器(`target`)**。所以一句 `SELECT name FROM users WHERE id=?` 编出来的 opcode(简化)大概是:

```
addr  opcode         p1    p2    p3    comment
----  -----------    ----  ----  ----  -----------------------------
0     Init           0     11    0     -- 启动,跳到主程序
1     OpenRead       0     2     0     -- 打开表 users(root page 2)
2     Variable       1     3     0     -- ★ 把 ?1 的绑定值拷进寄存器 3
3     Rewind         0     9     0     -- 游标到表头
4       Column         0     1     4     -- 读 users.id 进寄存器 4
5       Ne             3     4     9     -- 寄存器 3(?1 的值)!= 寄存器 4(id)?跳 9
6       Column         0     2     5     -- 读 users.name 进寄存器 5
7       ResultRow      5     1     0     -- 吐一行结果(寄存器 5 起 1 列)
8     Next           0     4     0     -- 下一行,跳回 4
9     Halt           0     0     0
...
```

`addr 2` 那条 `Variable 1 3` 就是占位符 `?` 编译出的指令。它的语义是:**"把第 1 号绑定参数的值,拷进第 3 号工作寄存器"**。

### 3.2 `OP_Variable` 执行:从 `aVar[]` 取值进工作寄存器

执行期跑到 `OP_Variable` 时,VDBE 主循环跳到这条 case([OP_Variable](../sqlite/src/vdbe.c#L1610)):

```c
case OP_Variable: {            /* out2 */
  Mem *pVar;       /* Value being transferred */

  assert( pOp->p1>0 && pOp->p1<=p->nVar );
  pVar = &p->aVar[pOp->p1 - 1];                  // ★ 从 aVar[] 取出绑定值
  if( sqlite3VdbeMemTooBig(pVar) ){
    goto too_big;
  }
  pOut = &aMem[pOp->p2];                          // 目标工作寄存器
  if( VdbeMemDynamic(pOut) ) sqlite3VdbeMemSetNull(pOut);  // 清掉目标寄存器原值
  memcpy(pOut, pVar, MEMCELLSIZE);                // ★ 浅拷贝整个 Mem 单元
  pOut->flags &= ~(MEM_Dyn|MEM_Ephem);
  pOut->flags |= MEM_Static|MEM_FromBind;          // ★ 标记"值来自绑定"
  UPDATE_MAX_BLOBSIZE(pOut);
  break;
}
```

这三行是整个 bind 机制的执行侧实现:

1. `pVar = &p->aVar[pOp->p1 - 1]`——按 opcode 的 P1(占位符编号,从 1 开始)找到对应的绑定值。注意这里 `-1`:对外接口 `sqlite3_bind_*(stmt, i, …)` 的 `i` 是从 1 开始的(`bind.c` 第 1841 行 assert `i>0`),而 `aVar[]` 是 C 数组从 0 开始,所以 `aVar[i-1]`。
2. `memcpy(pOut, pVar, MEMCELLSIZE)`——**直接 memcpy 整个 `Mem` 单元**到工作寄存器。注意这是浅拷贝,`Mem.z`(指向字符串/blob 的指针)被原样搬过去,`pVar` 自己仍持有指针。
3. `pOut->flags |= MEM_Static|MEM_FromBind`——把目标寄存器标记成 **"静态,不可释放"**(`MEM_Static`)且**"值来自绑定"**(`MEM_FromBind`)。这两个标志是后面 reset 安全性的关键(见 §4.4)。

> **钉死这件事(绑定与编译解耦)**:看这条 opcode 就懂了为什么"绑定与编译彻底解耦"。`OP_Variable` 在**编译期**就被编进 opcode 流,但那时候它只知道自己要"取第 1 号绑定值",**完全不知道那个值是多少**。值要到执行期(而且要在 step 之前被 `bind_*` 填进 `aVar[0]`)才现身。换句话说,opcode 流里每个 `OP_Variable` 都是一个**"等值的占位符指令"**,值由执行期的 `aVar[]` 内容决定。这就是为什么同一份 opcode 反复执行、每次绑定不同参数,得到不同结果——**opcode 不变,变的只是 `aVar[]` 的内容**。

### 3.3 `sqlite3_bind_*`:把值填进 `aVar[]`

bind 的全部工作就是往 `aVar[]` 数组里写值。一组 `sqlite3_bind_*` 函数(`vdbeapi.c` 第 1814 行起),每个都长得几乎一样,以 `sqlite3_bind_int64` 为例([sqlite3_bind_int64](../sqlite/src/vdbeapi.c#L1850)):

```c
int sqlite3_bind_int64(sqlite3_stmt *pStmt, int i, sqlite_int64 iValue){
  int rc;
  Vdbe *p = (Vdbe *)pStmt;
  rc = vdbeUnbind(p, (u32)(i-1));                       // 先解开旧绑定
  if( rc==SQLITE_OK ){
    assert( p!=0 && p->aVar!=0 && i>0 && i<=p->nVar );  // tag-20240917-01
    sqlite3VdbeMemSetInt64(&p->aVar[i-1], iValue);      // ★ 写值进 aVar[i-1]
    sqlite3_mutex_leave(p->db->mutex);
  }
  return rc;
}
```

两步:**先 `vdbeUnbind` 解开旧绑定**(把 `aVar[i-1]` 释放并设成 NULL),再 `sqlite3VdbeMemSetInt64` 把新值写进去。`sqlite3_bind_double`/`_text`/`_blob`/`_null`/`_zeroblob` 全是这个套路(`vdbeapi.c` 第 1836-1973 行),只是写值的 `sqlite3VdbeMemSet*` 调用不同。

`vdbeUnbind` 本身([vdbeUnbind](../sqlite/src/vdbeapi.c#L1719))有几个细节值得说:

```c
static int vdbeUnbind(Vdbe *p, unsigned int i){
  ...
  sqlite3_mutex_enter(p->db->mutex);
  if( p->eVdbeState!=VDBE_READY_STATE ){                       // ★ 只能在 READY 态 bind
    sqlite3Error(p->db, SQLITE_MISUSE_BKPT);
    ...
    return SQLITE_MISUSE_BKPT;                                  // 执行中 bind 报 MISUSE
  }
  if( i>=(unsigned int)p->nVar ){                              // 越界
    ...
    return SQLITE_RANGE;
  }
  pVar = &p->aVar[i];
  sqlite3VdbeMemRelease(pVar);                                  // 释放旧值(比如旧字符串)
  pVar->flags = MEM_Null;                                       // 置 NULL
  ...
  /* If the bit corresponding to this variable in Vdbe.expmask is set, then
  ** binding a new value to this variable invalidates the current query plan. */
  if( p->expmask!=0 && (p->expmask & (i>=31 ? 0x80000000 : (u32)1<<i))!=0 ){
    p->expired = 1;                                            // ★ 可能让计划失效
  }
  return SQLITE_OK;
}
```

> **钉死这件事(bind 只能在 READY 态)**:`vdbeUnbind` 第一道检查是 `if( p->eVdbeState!=VDBE_READY_STATE )`——**bind 只允许在 READY 态做**。也就是说,你不能在 `step` 执行到一半(状态是 RUN)的时候改绑定值,SQLite 会直接返回 `SQLITE_MISUSE`(`vdbeapi.c` 第 1728-1730 行还会 `sqlite3_log` 一条 "bind on a busy prepared statement" 再返回)。这是"绑定与执行串行化"的硬约束——同一个 Vdbe 同一时刻要么在 bind、要么在 step,不能交错。这也意味着,想并行跑同一个查询的不同参数,必须 prepare 出**多个** Vdbe 各自 bind/step(或者用 `SQLITE_OPEN_NOMUTEX` 配合应用层串行化)。

> **不这样会怎样(`expmask` 让计划失效)**:`vdbeUnbind` 末尾那段 `expmask` 检查是个高级特性——某些查询的**计划依赖绑定值**(比如 `WHERE x LIKE ? ESCAPE ?`,escape 字符影响能不能走索引),Code Generator 会在编译时把这种参数的位标记进 `Vdbe.expmask`(`vdbeInt.h` 第 522 行)。之后只要这些参数被重新 bind,`vdbeUnbind` 就把 `p->expired` 设成 1,下次 `sqlite3_step` 进来会发现 `expired`,触发 `SQLITE_SCHEMA` 走 recompile 路径(`sqlite3Reprepare` 重新编译)。这是 prepared 复用的一个"逃生阀":**绝大多数 bind 不触发重编译,但少数值敏感的参数会**。注释里明说(`vdbeapi.c` 第 1742-1750 行):"If the specific value bound to a host parameter in the WHERE clause might influence the choice of query plan for a statement, then the statement will be automatically recompiled."

### 3.4 绑定的"四形态"完整图景

把上面三步拼起来,参数绑定在 SQLite 内部其实是这么一条链:

```
   SQL: "SELECT name FROM users WHERE id=:uid AND age>:a"
                              │              │           │
            ┌─────────────────┘              │           └────────────┐
            ▼                                ▼                        ▼
   tokenizer 产出 TK_VARIABLE            TK_VARIABLE               TK_VARIABLE
            │                                │                        │
   sqlite3ExprAssignVarNumber:               │                        │
     :uid → 查 pVList,没有 → nVar=1, x=1    │                        │
                                       :a → 查 pVList,没有 → nVar=2, x=2
   :uid(再出现)→ 查 pVList,有 → x=1 (复用同编号)                       │
            │                                                                       │
            ▼                                                                       ▼
   AST 节点 iColumn=1              AST 节点 iColumn=2               AST 节点 iColumn=1
            │                                                                       │
            ▼                                                                       ▼
   Code Generator 发 OP_Variable 1, target_r1    OP_Variable 2, target_r2  OP_Variable 1, target_r3
            │                                                                       │
            └─────────────────────┐                       ┌──────────────────┘
                                  ▼                       ▼
                            aVar[0]  ◄─────────────────────┘  (第 1 号绑定,:uid)
                            aVar[1]                              (第 2 号绑定,:a)

   执行期:
     sqlite3_bind_int(stmt, 1, uid_val);   → aVar[0] = uid_val
     sqlite3_bind_int(stmt, 2, age_val);   → aVar[1] = age_val
     OP_Variable 1, r1  → memcpy r1 = aVar[0]   (MEM_FromBind)
     OP_Variable 1, r3  → memcpy r3 = aVar[0]   (同一个绑定值, 进两个不同寄存器)
     OP_Variable 2, r2  → memcpy r2 = aVar[1]
```

关键观察:**同一个绑定值(如 `:uid` 出现两次)只占 `aVar[]` 一个槽位**,但执行期 `OP_Variable` 可以把它拷进多个工作寄存器。绑定数组的大小只和**不同的占位符个数**有关,和占位符在 SQL 里出现几次无关——这是 `sqlite3_bind_parameter_count()` 返回 `nVar`(不同参数数)而不是"占位符出现次数"的原因。

---

## 四、step 与 reset:跑一遍,再重置回起跑线

bind 完了就 step。`sqlite3_step` 是执行的入口,但真正干活的不是它,是它调的 `sqlite3VdbeExec`。

### 4.1 `sqlite3_step` → `sqlite3Step` → `sqlite3VdbeExec`

`sqlite3_step`([sqlite3_step](../sqlite/src/vdbeapi.c#L978))本身很薄,主要是**处理 schema 变更导致的 recompile**:

```c
int sqlite3_step(sqlite3_stmt *pStmt){
  ...
  db = v->db;
  sqlite3_mutex_enter(db->mutex);
  while( (rc = sqlite3Step(v))==SQLITE_SCHEMA                  // ★ schema 变更触发重编译
         && cnt++ < SQLITE_MAX_SCHEMA_RETRY ){
    int savedPc = v->pc;
    rc = sqlite3Reprepare(v);                                   // 重新编译
    ...
    sqlite3_reset(pStmt);                                       // 重置后再来
    ...
  }
  sqlite3_mutex_leave(db->mutex);
  return rc;
}
```

正常的执行(`SQLITE_SCHEMA` 不触发时)直接走 `sqlite3Step`([sqlite3Step](../sqlite/src/vdbeapi.c#L836))。`sqlite3Step` 做两件事:**状态机迁移**(从 READY 推到 RUN),然后调 `sqlite3VdbeExec` 跑 opcode。

状态机迁移那段(`vdbeapi.c` 第 844-884 行)是 prepared 复用的执行侧入口:

```c
if( p->eVdbeState==VDBE_READY_STATE ){
  ...
  db->nVdbeActive++;
  if( p->readOnly==0 ) db->nVdbeWrite++;
  if( p->bIsReader ) db->nVdbeRead++;
  p->pc = 0;                          // ★ 程序计数器归零, 从 opcode 0 开始
  p->eVdbeState = VDBE_RUN_STATE;     // ★ 推到 RUN 态
}
```

注意那句 `p->pc = 0`——**每次从 READY 进 RUN,程序计数器都从 0 开始**。这意味着每次 step(在 reset 之后)都从 opcode 流的第一条(`OP_Init`)跑起。这就是 prepared 复用执行侧的本质:**重置 `pc` = 从头再跑一遍已经编好的 opcode**。

真正的执行是 `sqlite3VdbeExec`(`vdbeapi.c` 第 928 行调)——这是那个几万行的 `switch` 主循环(承 P2-05),按 opcode 逐条执行。返回 `SQLITE_ROW` 表示吐了一行、`SQLITE_DONE` 表示跑完。每返回一次 `SQLITE_ROW`,`sqlite3_step` 就把这一行交给调用方(`sqlite3_column_*` 系列从这里读);调用方再 step,VDBE 从上次停的 `pc` 接着跑,直到下一条 `OP_ResultRow` 或 `OP_Halt`。

> **钉死这件事(step 与 pc)**:`sqlite3_step` 的语义是"跑到下一行结果或跑完",不是"跑完整条 SQL"。这是 prepared statement 在循环里一行行取结果的基础——VDBE 用 `pc` 记住自己跑到哪了,每次 step 从 `pc` 接着跑。所以同一句 `SELECT` 在循环里 step 100 次(假设有 100 行结果),`pc` 是连续推进的 100 段,而不是从头跑 100 遍。这是 prepared 复用的另一个维度:**结果集的多行也是一次执行里 pc 顺序推进出来的**,不是 prepare 100 次。

### 4.2 跑完:`OP_Halt` 把状态推到 HALT

一条 SQL 跑完(读到表尾、或 UPDATE/INSERT 改完所有行),Code Generator 在最后会发 `OP_Halt`。`OP_Halt` 执行时调 `sqlite3VdbeHalt`([sqlite3VdbeHalt](../sqlite/src/vdbeaux.c#L3322)),它做几件事:

1. **`closeAllCursors(p)`**——关掉所有打开的游标(`vdbeaux.c` 第 3346 行)。游标是 VDBE 持有的 B-tree 读取位置,关掉就是释放这些 B-tree cursor(把页引用还给 pager)。
2. **事务善后**——如果这是 auto-commit 模式且是唯一的活跃写语句,触发 commit/rollback(`vdbeaux.c` 第 3412-3449 行);如果出错且是语句级错误,回滚到 savepoint。这部分是事务机制(P4 已讲),本章不展开。
3. **把状态机推到 `VDBE_HALT_STATE`**。

> **钉死这件事(HALT ≠ 销毁)**:执行完停在 HALT,但 **opcode 数组一个字节都没动**,游标关了、寄存器也由 `closeAllCursors` 顺带 `releaseMemArray` 清了(`vdbeaux.c` 第 2855 行)。Vdbe 这个对象本身完整地存在着,随时可以被 reset 推回 READY 再跑一遍。这就是 prepared statement 能反复执行的物理基础——**HALT 是"机器停在原地",不是"机器被拆了"**。

`closeAllCursors` 里面那条 `releaseMemArray(p->aMem, p->nMem)`([releaseMemArray 调用](../sqlite/src/vdbeaux.c#L2855))值得专门看一眼,因为它干了一件非常关键的事——清工作寄存器(`aMem[]`),但**不清绑定寄存器**(`aVar[]`)。这就是 reset 之后绑定值还在的原因。

### 4.3 `sqlite3_reset`:不重编译,只重置 pc + 清寄存器/游标

到了 prepared 复用最关键的函数 `sqlite3_reset`。它的全部源码([sqlite3_reset](../sqlite/src/vdbeapi.c#L128))只有十几行:

```c
int sqlite3_reset(sqlite3_stmt *pStmt){
  int rc;
  if( pStmt==0 ){
    rc = SQLITE_OK;
  }else{
    Vdbe *v = (Vdbe*)pStmt;
    sqlite3 *db = v->db;
    sqlite3_mutex_enter(db->mutex);
    checkProfileCallback(db, v);
    rc = sqlite3VdbeReset(v);        // ★ 重置(关游标、清寄存器、处理事务)
    sqlite3VdbeRewind(v);            // ★ 倒带(pc 归零、状态回 READY)
    ...
    rc = sqlite3ApiExit(db, rc);
    sqlite3_mutex_leave(db->mutex);
  }
  return rc;
}
```

两个核心调用:`sqlite3VdbeReset` 和 `sqlite3VdbeRewind`。一个管"善后",一个管"倒带"。我们分别看。

#### `sqlite3VdbeReset`:善后

`sqlite3VdbeReset`([sqlite3VdbeReset](../sqlite/src/vdbeaux.c#L3593))做这几件事:

```c
int sqlite3VdbeReset(Vdbe *p){
  sqlite3 *db = p->db;

  /* 如果上次没正常跑完(出错或中断),现在强制 halt */
  if( p->eVdbeState==VDBE_RUN_STATE ) sqlite3VdbeHalt(p);

  /* 把 VDBE 的错误码/错误消息传给 db 句柄(供 sqlite3_errcode/errmsg 读) */
  if( p->pc>=0 ){
    vdbeInvokeSqllog(p);
    if( db->pErr || p->zErrMsg ){
      sqlite3VdbeTransferError(p);
    }else{
      db->errCode = p->rc;
    }
  }

  /* DEBUG assert: 游标都已关, 寄存器都已清 */
#ifdef SQLITE_DEBUG
  if( p->apCsr ) for(i=0; i<p->nCursor; i++) assert( p->apCsr[i]==0 );
  if( p->aMem ){
    for(i=0; i<p->nMem; i++) assert( p->aMem[i].flags==MEM_Undefined );
  }
#endif
  if( p->zErrMsg ){
    sqlite3DbFree(db, p->zErrMsg);   // 清错误消息
    p->zErrMsg = 0;
  }
  p->pResultRow = 0;
  ...
}
```

注意中间那段 DEBUG assert——**"游标都已关、寄存器都已清"** 是 `sqlite3VdbeReset` 的不变量。这是谁干的?是 `sqlite3VdbeHalt` 里 `closeAllCursors` → `releaseMemArray(p->aMem, p->nMem)` 干的(`vdbeaux.c` 第 2855 行)。也就是说,reset 时清寄存器/游标的工作,在 `sqlite3VdbeHalt`(被 `sqlite3VdbeReset` 调用)里就完成了。

> **钉死这件事(清的是什么,不清的是什么)**:reset 清的是**工作寄存器**(`aMem[]`)和**游标**(`apCsr[]`),它**不动绑定寄存器**(`aVar[]`)。所以 reset 之后,绑定值还在——你可以不重新 bind 直接 step(用上次的参数再跑一遍),也可以 bind 新值再 step。绑定值是 prepared statement 的"持久状态",reset 不碰它;只有 `sqlite3_clear_bindings` 才专门清绑定值。这个区分非常重要,下面 §4.5 单讲。

#### `sqlite3VdbeRewind`:倒带

`sqlite3VdbeReset` 之后调的是 `sqlite3VdbeRewind`([sqlite3VdbeRewind](../sqlite/src/vdbeaux.c#L2600)),它才是把 Vdbe"推回起跑线"的核心:

```c
void sqlite3VdbeRewind(Vdbe *p){
  ...
  assert( p->eVdbeState==VDBE_INIT_STATE
       || p->eVdbeState==VDBE_READY_STATE
       || p->eVdbeState==VDBE_HALT_STATE );

  assert( p->nOp>0 );   // ★ opcode 一条都没少

  p->eVdbeState = VDBE_READY_STATE;     // ★ 状态回 READY

  ...
  p->pc = -1;                           // ★ 程序计数器设成 -1
  p->rc = SQLITE_OK;
  p->errorAction = OE_Abort;
  p->nChange = 0;
  p->cacheCtr = 1;                       // ★ 行缓存计数器归位
  p->minWriteFileFormat = 255;
  p->iStatement = 0;
  p->nFkConstraint = 0;
  ...
}
```

`sqlite3VdbeRewind` 做的事可以用一张表说清:

| 字段 | reset 后的值 | 含义 |
|------|-------------|------|
| `eVdbeState` | `VDBE_READY_STATE` | 状态机回到"就绪" |
| `pc` | `-1` | 程序计数器归零前哨(`OP_Init` 会把它设成 0) |
| `rc` | `SQLITE_OK` | 清掉上次执行的错误码 |
| `nChange` | `0` | 这次执行修改的行数清零 |
| `cacheCtr` | `1` | 游标行缓存计数器归位 |
| `minWriteFileFormat` | `255` | 写文件格式下限重置 |
| `aOp[]`(opcode 数组) | **原封不动** | ★ **编译产物一字未改** |
| `aVar[]`(绑定数组) | **原封不动** | ★ **绑定值不丢** |

注意最后两行——**opcode 数组和绑定数组 reset 一字不动**。这就是 reset 凭什么"不重编译就能再跑"的物理基础:**机器的"程序存储器"和"参数存储器"都没动,只是"程序计数器"和"临时寄存器"被清零了**。下次 step 进来,从 `pc=0` 开始,按一样的 opcode、用一样(或新 bind)的参数,再走一遍。

> **钉死这件事(pc = -1 不是 pc = 0)**:细看会发现 `Rewind` 把 `pc` 设成 `-1` 而不是 `0`。这是因为 opcode 数组的第 0 条永远是 `OP_Init`(`vdbe.c` 第 9125 行 assert `pOp==p->aOp`),`OP_Init` 是个特殊的"启动"指令——它做 trace 回调、设置 `minWriteFileFormat` 标记等。VDBE 主循环从 `pc=0` 开始跑 `OP_Init`,然后跳到主程序的起始地址。`Rewind` 设 `-1` 是为了配合 `sqlite3Step` 里 `p->pc = 0`(`vdbeapi.c` 第 883 行)——`Step` 把 `-1` 改成 `0`,VDBE 从 opcode 0 的 `OP_Init` 开始跑。这个细节不重要,但它说明 reset 后的第一次 step **总是从 `OP_Init` 开始**,不会"接着上次跑到的地方"——上次跑到哪里已经被 reset 抹掉了。

### 4.4 复用的安全前提:`MEM_FromBind` 标志

到这里有个隐患必须解决——`OP_Variable` 是 `memcpy(pOut, pVar, MEMCELLSIZE)` 把绑定值**浅拷贝**进工作寄存器(`vdbe.c` 第 1620 行),那么工作寄存器 `pOut` 和绑定寄存器 `pVar` 此时**共享同一个 `Mem.z` 指针**(指向字符串或 blob)。如果 reset 时 `releaseMemArray` 把工作寄存器释放了,会不会把绑定值里那个共享的字符串也释放掉?

SQLite 用一个标志位解决这个问题——回看 `OP_Variable` 那条代码(`vdbe.c` 第 1622 行):

```c
pOut->flags &= ~(MEM_Dyn|MEM_Ephem);
pOut->flags |= MEM_Static|MEM_FromBind;   // ★
```

`MEM_Static` 标志告诉 `releaseMemArray`:**这个寄存器的 `.z` 指针不属于它,不要 `free`**。`releaseMemArray` 里那段 inlined `sqlite3VdbeMemRelease` 只对 `MEM_Agg|MEM_Dyn` 的寄存器调 `sqlite3VdbeMemRelease`(`vdbeaux.c` 第 2215 行),`MEM_Static` 的直接跳过——因为它的 `.z` 由 `aVar[]` 那边的真正持有者管理。

> **不这样会怎样(没有 `MEM_Static`/`MEM_FromBind`)**:假设 reset 时 `releaseMemArray` 不看标志、无差别地释放所有工作寄存器——那它就会把"工作寄存器 3"那个 `Mem.z` 指针(其实是 `aVar[0].z` 的同一个字符串)`free` 掉。下一次 step 时,`OP_Variable` 又 `memcpy` 把 `aVar[0]` 拷进工作寄存器,这次拷过去的是一个**已经被 free 的悬垂指针**——`use-after-free`,经典内存破坏。SQLite 用 `MEM_Static|MEM_FromBind` 这个标志,让"工作寄存器持有绑定值的浅拷贝但不负责释放",从根上堵死了这个 bug。`MEM_FromBind` 这个名字本身就是为这个用途而设(`vdbeInt.h` 第 320 行注释 "Value originates from sqlite3_bind()")。

`MEM_Static` 这套标志机制是整个 prepared 复用安全性的支点:它让"绑定值只存一份(在 `aVar[]`),工作寄存器持有浅拷贝"成为可能——既省内存(不重复拷贝字符串),又保证 reset 不误伤绑定值。

### 4.5 `sqlite3_clear_bindings`:专门清绑定值

bind 值既然 reset 不清,那想清怎么办?用 `sqlite3_clear_bindings`([sqlite3_clear_bindings](../sqlite/src/vdbeapi.c#L149)):

```c
int sqlite3_clear_bindings(sqlite3_stmt *pStmt){
  ...
  Vdbe *p = (Vdbe*)pStmt;
  ...
  sqlite3_mutex_enter(mutex);
  for(i=0; i<p->nVar; i++){
    sqlite3VdbeMemRelease(&p->aVar[i]);     // ★ 释放每个绑定值
    p->aVar[i].flags = MEM_Null;             // ★ 置 NULL
  }
  ...
}
```

它就是把 `aVar[]` 整个数组遍历一遍,每个都 `sqlite3VdbeMemRelease`(释放字符串/blob 内存)再设 `MEM_Null`。注意它**只清绑定数组**,不动工作寄存器(那些由 reset 管)。

> **钉死这件事(clear_bindings vs reset 的边界)**:reset 和 clear_bindings 是两个正交的操作:
> - **reset** 清"执行期临时状态"——`pc`、工作寄存器 `aMem[]`、游标 `apCsr[]`。**不动绑定值**。
> - **clear_bindings** 清"绑定值"——`aVar[]`。**不动执行期状态**。
>
> 这两个互不干扰。典型的复用循环是 `bind → step → reset → bind(覆盖旧值) → step → reset → …`,绝大多数情况下根本不用 clear_bindings——因为 `bind_*` 内部的 `vdbeUnbind` 已经先把旧绑定值释放了(`vdbeapi.c` 第 1738 行)。clear_bindings 只在一个场景下有用:**你想把所有绑定值都显式置 NULL**(比如某个参数这轮不传,语义上等价于 NULL),而不靠重新 bind 覆盖。SQLite 官方文档明确说 clear_bindings 是"optional"——绝大多数应用从不调它。

---

## 五、prepared statement 的完整生命周期:一张时序图

把前四节拼起来,prepared statement 从生到死的完整生命周期是这样:

```mermaid
sequenceDiagram
    autonumber
    participant App as 应用代码
    interface  as sqlite3_prepare_v2
    participant V as Vdbe (sqlite3_stmt)
    participant CG as 编译链路 (Tokenizer/Parser/CodeGen)
    participant VM as VDBE 执行器

    Note over App,VM: ━━━ 阶段一: prepare (编译, 只发生一次) ━━━

    App->>interface: sqlite3_prepare_v2(db, "SELECT name FROM users WHERE id=?", ...)
    interface->>CG: sqlite3RunParser(&sParse, zSql)
    Note right of CG: tokenize + parse + codegen<br/>产出 opcode 流存进 Vdbe.aOp[]
    CG-->>interface: 编译完成
    interface->>V: sqlite3VdbeMakeReady(p, pParse)
    Note right of V: 分配 aMem[]/aVar[]/apCsr[]<br/>状态: INIT → READY<br/>pc = -1
    interface-->>App: 返回 sqlite3_stmt (Vdbe*)

    Note over App,VM: ━━━ 阶段二: 循环 bind/step/reset (执行, 可重复 N 次) ━━━

    rect rgb(245, 245, 255)
    Note over App,VM: 第 1 轮
    App->>interface: sqlite3_bind_int(stmt, 1, 42)
    interface->>V: vdbeUnbind(p, 0) + aVar[0] = 42
    App->>interface: sqlite3_step(stmt)
    interface->>V: sqlite3Step(p)
    Note right of V: 状态: READY → RUN, pc = 0
    V->>VM: sqlite3VdbeExec(p)
    Note right of VM: 跑 opcode: OP_Init → OpenRead →<br/>OP_Variable 1, r1 (从 aVar[0] 取 42) →<br/>Rewind → Column → Ne → ResultRow
    VM-->>interface: SQLITE_ROW (吐一行)
    interface-->>App: SQLITE_ROW
    App->>interface: sqlite3_column_text(stmt, 0)
    interface-->>App: "alice"
    App->>interface: sqlite3_step(stmt)
    V->>VM: sqlite3VdbeExec(p) (从上次 pc 接着跑)
    VM-->>interface: SQLITE_DONE (读完所有行)
    interface-->>App: SQLITE_DONE
    App->>interface: sqlite3_reset(stmt)
    interface->>V: sqlite3VdbeReset(p) + sqlite3VdbeRewind(p)
    Note right of V: 关游标, 清 aMem[], <br/>★ aVar[] 不动 (42 还在)<br/>状态: HALT → READY, pc = -1
    end

    rect rgb(245, 255, 245)
    Note over App,VM: 第 2 轮 (★ 不再编译)
    App->>interface: sqlite3_bind_int(stmt, 1, 43)
    interface->>V: vdbeUnbind 释放旧的 42, aVar[0] = 43
    App->>interface: sqlite3_step(stmt)
    Note right of V: 状态: READY → RUN, pc = 0 (从 OP_Init 重跑)
    V->>VM: sqlite3VdbeExec(p) (用同一份 opcode, 新参数 43)
    VM-->>interface: SQLITE_ROW / SQLITE_DONE
    App->>interface: sqlite3_reset(stmt)
    Note right of V: 回 READY
    end

    Note over App,VM: ━━━ 阶段三: finalize (销毁, 一次) ━━━

    App->>interface: sqlite3_finalize(stmt)
    interface->>V: sqlite3VdbeReset(v) + sqlite3VdbeDelete(v)
    Note right of V: ★ 释放 aOp[] (opcode 数组)<br/>释放 aMem[]/aVar[]/apCsr[]<br/>释放 Vdbe 结构体本身
    interface-->>App: SQLITE_OK
```

这张图把三件事钉死了:

1. **编译只在阶段一发生一次**——阶段二的每一轮循环(从第 2 轮起)都直接用阶段一编译好的 opcode,不再过 Tokenizer/Parser/Code Generator。
2. **reset 把状态从 HALT 推回 READY,`aVar[]` 不动**——所以下一轮可以 bind 新值(覆盖旧的),也可以不 bind 直接 step(用上次的参数)。
3. **finalize 才释放 opcode 数组本身**——reset 永远不碰 opcode,只有 finalize 才"拆机器"。

---

## 六、技巧精解:为什么 reset 不重编译这么快

这一节专门拆 prepared 复用最容易被讲错的两个硬核技巧——**reset 的复用机制**和 **`OP_Variable` 的绑定解耦**——配真实源码,做反面对比。

### 6.1 技巧一:reset 只重置 `pc`/寄存器/游标,不重编译 opcode

这是 prepared statement 性能的核心。我们用一个**反面对比**讲清楚它有多重要。

> **反例:朴素实现(每次重新 prepare)**

假设 SQLite 没有 reset,每次想再跑一遍都得重新 `prepare_v2`。代码长这样:

```c
for( int i=0; i<10000; i++ ){
  sqlite3_stmt *stmt;
  sqlite3_prepare_v2(db, "SELECT name FROM users WHERE id=?", -1, &stmt, 0);  // 每次编译
  sqlite3_bind_int(stmt, 1, ids[i]);
  sqlite3_step(stmt);
  sqlite3_finalize(stmt);  // 每次销毁
}
```

每一轮循环要付的开销:

| 开销项 | 单次代价 | 10000 轮总代价 |
|--------|----------|-----------------|
| Tokenizer | O(SQL 长度) 字符扫描 | 10000 × O(39 字符) |
| Parser | 每个 token 一次 LALR 状态转移 + 归约建 AST 节点(堆分配) | 10000 × O(token 数) + 10000 次 Expr 节点 malloc |
| Code Generator | 遍历 AST 产 opcode + `sqlite3VdbeAddOp`(可能 realloc opcode 数组) | 10000 × O(AST 节点) + 10000 次 opcode 数组分配 |
| `MakeReady` | 分配 aMem/aVar/apCsr(至少一次 malloc) | 10000 次内存分配 |
| `finalize` | 释放 opcode 数组、aMem、aVar、apCsr、Vdbe 本身 | 10000 次内存释放 |
| schema 锁检查 | 每库一次 `sqlite3BtreeSchemaLocked` | 10000 × (库数) |

这六项加起来,假设单次约 50-200 微秒(取决于 SQL 复杂度),10000 轮就是 **0.5-2 秒纯编译开销**——根本没碰 B-tree。

> **正解:prepared 复用(reset + 再 step)**

```c
sqlite3_stmt *stmt;
sqlite3_prepare_v2(db, "SELECT name FROM users WHERE id=?", -1, &stmt, 0);  // ★ 只编译一次
for( int i=0; i<10000; i++ ){
  sqlite3_bind_int(stmt, 1, ids[i]);    // 写 aVar[0]
  sqlite3_step(stmt);
  sqlite3_reset(stmt);                   // ★ 只 reset
}
sqlite3_finalize(stmt);                  // 循环外销毁一次
```

每一轮循环现在只付:

| 开销项 | 单次代价 | 10000 轮总代价 |
|--------|----------|-----------------|
| `bind_int` | `vdbeUnbind`(常数)+ `sqlite3VdbeMemSetInt64`(常数) | 10000 × O(1) |
| `step` | 跑 opcode(只和 B-tree 页数有关) | 10000 × O(查询计划) |
| `reset` | `sqlite3VdbeHalt`(关游标 + `releaseMemArray`)+ `sqlite3VdbeRewind`(常数) | 10000 × O(游标数 + 寄存器数) |

注意:**Tokenizer/Parser/Code Generator/`MakeReady`/`finalize` 这五项的总代价,从 10000 倍降到 1 倍**。这五项里,Parser 的 AST 节点 malloc 和 Code Generator 的 opcode 数组分配是最贵的(都走 heap),prepared 复用把它们一次性付清,之后 9999 次执行一分钱不花。

> **源码佐证:reset 不碰 opcode**

回看 `sqlite3VdbeRewind` 的源码(`vdbeaux.c` 第 2600-2634 行),它改的字段是 `eVdbeState`/`pc`/`rc`/`errorAction`/`nChange`/`cacheCtr`/`minWriteFileFormat`/`iStatement`/`nFkConstraint`——**全是 Vdbe 结构体里的小字段,没有一个涉及 `aOp[]`**(opcode 数组)。甚至 DEBUG 模式下它对 opcode 做的事只有一句:`for(i=0; i<p->nOp; i++){ p->aOp[i].nExec = 0; p->aOp[i].nCycle = 0; }`(只在 `VDBE_PROFILE` 编译选项下,清 profile 计数器)——这进一步证明:reset 对 opcode 是**只读**的(最多在 profile 模式下清计数器),绝不动指令内容。

> **钉死这件事**:reset 快,是因为它的工作量是 O(游标数 + 寄存器数 + 几个标量字段),而 prepare 的工作量是 O(SQL 长度 + AST 节点数 + opcode 数 + 若干次堆分配)。两者差几个数量级。SQLite 官方文档明说:**"Re-using a prepared statement is much faster than re-preparing it. The sqlite3_reset() routine resets the prepared statement but does not recompile it."** 这就是 prepared statement 是"SQLite 高性能关键之一"的全部理由。

### 6.2 技巧二:`OP_Variable` 把绑定与编译彻底解耦

第二个硬核技巧是 `OP_Variable` opcode 本身的设计——它实现了"编译期不知道值、执行期才取值"的解耦。我们看为什么这个设计 sound(正确)。

> **反例:朴素设计(编译期把字面量直接嵌进 opcode)**

如果 SQLite 没有 `OP_Variable`,而是要求 SQL 里只能写字面量(`WHERE id=42`),Code Generator 就会发 `OP_Integer 42, target`(把 42 直接编进 opcode 的 P1)。这样 opcode 自带值,执行期不用查绑定数组。但这有两个硬伤:

1. **值变了就要重编译**——想查 `id=43`,SQL 不一样,必须重新 prepare。这正是 `sqlite3_exec` 的处境,§一已说。
2. **没法做参数化查询**—— bind API 整个不存在。

> **正解:`OP_Variable` 作为"等值的占位符指令"**

`OP_Variable` 的设计是:**opcode 里只记"取第 N 号绑定值",不记值本身**。值存在 `aVar[N-1]` 里,由执行期的 `bind_*` 填。这样:

- **opcode 不依赖具体值**——同一份 opcode 配不同 `aVar[]` 内容,跑出不同结果。
- **绑定值可以反复换**——bind 100 次不同值,opcode 一字不改,每次 step 都从 `aVar[]` 取最新值。
- **多个占位符复用同一份 opcode**——1000 行的批量查询只用一份 opcode,而不是 1000 份。

> **为什么这个设计 sound(正确)**

`OP_Variable` 的 `memcpy(pOut, pVar, MEMCELLSIZE)` 浅拷贝 + `MEM_Static|MEM_FromBind` 标志的组合,**保证了绑定值只存一份、工作寄存器持有不负责释放的引用**。这个组合的 sound 性体现在三个不变量:

1. **绑定值的所有权清晰**:`aVar[N]` 是绑定值的唯一持有者,它的 `Mem.z`/`Mem.zMalloc` 是真正的内存;工作寄存器的 `Mem.z` 只是个 alias。
2. **reset 不会误释放绑定值**:`releaseMemArray` 只对 `MEM_Dyn`/`MEM_Agg` 的寄存器调 `sqlite3VdbeMemRelease`(`vdbeaux.c` 第 2215 行),`MEM_Static` 的直接跳过——工作寄存器因为标了 `MEM_Static`,reset 时不会被 free,绑定值安全。
3. **`bind_*` 重新填值时旧值被释放**:`vdbeUnbind` 里的 `sqlite3VdbeMemRelease(pVar)`(`vdbeapi.c` 第 1738 行)是绑定值释放的唯一入口,旧值在这里被正确释放,新值随后填入——不会内存泄漏,也不会悬垂指针。

这三个不变量共同保证了"绑定值浅拷贝进工作寄存器"这个设计在 reset/再 bind/再 step 的循环里**永远 sound**。

> **钉死这件事**:`OP_Variable` 的精妙不在"取一个值"(那只是 `memcpy`),而在**它把"编译产物"和"运行时数据"分到了两个存储区**——opcode 在 `aOp[]`(不可变),值在 `aVar[]`(每次 bind 改)。这个分离让 opcode 可以被反复执行而不失效,值可以反复换而 opcode 不动。这是 prepared statement "一次编译、多次执行"在执行器侧的物理实现,和 Lua VM 的 `OP_LOADK`(把常量编进字节码)对照鲜明——Lua 的常量在编译期就定死,SQLite 的"变量"在执行期才取。这条对照是 SQLite VDBE 承接《Lua》VM 的一个微妙差异(P2-05 已立 Lua VM 基础,这里只点出 SQLite 的扩展)。

---

## 七、与《MySQL·InnoDB》prepared statement 的对照(承接)

prepared statement 不是 SQLite 独有,MySQL/PG 都有。但实现形态完全不同——SQLite 是**同进程函数调用**,MySQL 是**网络协议二进制**。一句话对照:

| 维度 | SQLite prepared | MySQL/PG prepared |
|------|----------------|---------------------|
| 调用形态 | 同进程函数调用(`sqlite3_prepare_v2` → `sqlite3_step`) | 网络协议二进制(`COM_STMT_PREPARE` → `COM_STMT_EXECUTE`,MySQL 协议包) |
| 编译产物存在哪 | 客户端进程内存(Vdbe 结构体,`aOp[]` 数组) | 服务端进程内存(MySQL 是 Prepared_statement 对象,PG 是 unnamed/named prepared statement) |
| 绑定值怎么传 | 直接写 Vdbe 的 `aVar[]`(同进程内存) | `COM_STMT_EXECUTE` 包里编码(二进制 row format,逐字段类型 + 值) |
| 反复执行的网络往返 | 零(同进程,直接函数调用) | 每次 `COM_STMT_EXECUTE` 一个网络往返(值小但仍有 RTT) |
| 编译开销摊薄对象 | 单条 SQL 反复执行 | 单条 SQL 反复执行 + **省网络带宽**(只传参数,不传 SQL 文本) |

SQLite 因为是嵌入式(链接进应用、单进程),prepared statement 没有"网络协议"那一层——`sqlite3_prepare_v2` 是个直接的 C 函数调用,编译产物直接留在应用进程内存里,绑定值直接写进程内存里的 `aVar[]`。MySQL/PG 因为是 C/S,prepared statement 是**协议层特性**——客户端发 `COM_STMT_PREPARE` 给服务端、服务端编译好返回一个 statement id,之后客户端用 `COM_STMT_EXECUTE` 带 statement id + 参数值反复执行,省的是"每次重发整个 SQL 文本"的网络带宽。

> **钉死这件事(承接差异)**:两者的"为什么快"不一样。SQLite prepared 快,是因为省了**编译开销**(tokenize/parse/codegen)。MySQL/PG prepared 快,是因为省了**网络往返传 SQL 文本**(虽然也省编译,但 C/S 场景下网络往往更贵)。这是嵌入式 vs C/S 的本质差异在 prepared statement 这个特性上的投影——SQLite 没有"网络"这个变量,所以它的 prepared 纯粹是"省编译";MySQL/PG 的 prepared 还兼着"省网络"。

PG 还有个 SQLite 没有的特性:**`PREPARE ... AS SELECT ...` 可以把 prepared statement 命名、存在服务端、跨会话复用**(部分场景);SQLite 的 prepared statement 是**连接私有**的(`Vdbe` 挂在 `sqlite3*` 这个 db 句柄上),不跨连接共享。这又是嵌入式(单连接)vs C/S(多连接)的差异。

---

## 八、章末小结

> **本章服务二分法的哪一面**:**编译与执行**。prepared statement 的全部意义是把"编译"和"执行"切成两个时间上不重叠的阶段——prepare 把 tokenize/parse/codegen 这一整条编译链路跑一遍、把产物 opcode + 寄存器骨架缓存进 Vdbe;bind/step/reset 全在执行器(VDBE)上反复操作。这一章是"编译与执行"这一面的收束:它把"编译的产物怎么被反复消费"讲到底,正好把 P6-19(触发器/视图/FTS 都是 opcode 复用)的主题接到 P7-21 全书收束。

### 五个为什么

1. **为什么 prepared statement 这么快?**——因为编译(tokenize/parse/codegen/MakeReady/内存分配)只在 prepare 那一次发生,之后 N 次执行只跑 opcode,编译开销从 O(N) 降到 O(1)。reset 只重置 `pc`/寄存器/游标(常数级),不重编译 opcode(那是 finalize 才干的)。

2. **为什么 reset 不重编译就能再跑?**——因为 opcode 数组 `aOp[]` 是 reset 的**只读**对象,reset 只改 Vdbe 结构体里的几个标量字段(`pc`/`eVdbeState`/`rc` 等)和清工作寄存器/游标。机器的"程序存储器"一字未动,只是"程序计数器"和"临时寄存器"被清零。下次 step 从 `pc=0` 的 `OP_Init` 重跑同一份 opcode。

3. **为什么 `?`/`?N`/`:name`/`@name`/`$name` 最后都归一成编号?**——因为执行器只关心"取第 N 号绑定值"(由 `OP_Variable` 的 P1 指定),不关心占位符原本是啥样。四种占位符在 parse 期由 `sqlite3ExprAssignVarNumber` 全部归一成 `i64` 编号,名字只存 `pVList` 表给 `bind_parameter_index` 反查用。归一后执行侧统一、简单、快。

4. **为什么 `OP_Variable` 是浅拷贝 + `MEM_Static|MEM_FromBind` 标志,而不是深拷贝?**——深拷贝大字符串/blob 太贵(每次 step 都拷)。浅拷贝让绑定值只存一份(在 `aVar[]`),工作寄存器持有不负责释放的引用,靠 `MEM_Static` 标志让 `releaseMemArray` 跳过它。这套设计既省内存(不重复拷贝),又保证 reset 不误伤绑定值。

5. **为什么 SQLite 的 prepared 是函数调用、MySQL 的 prepared 是协议包?**——因为 SQLite 是嵌入式(同进程),prepared statement 没有"网络"那一层,编译产物直接留进程内存,绑定值直接写 `aVar[]`。MySQL/PG 是 C/S,prepared statement 是协议层特性,省的是网络往返传 SQL 文本。两者的"为什么快"不一样:SQLite 纯省编译,MySQL/PG 还兼省网络。

### 想继续深入往哪钻

- **prepare 主链路**:`src/prepare.c` 整个文件——[`sqlite3_prepare_v2`](../sqlite/src/prepare.c#L943)、[`sqlite3LockAndPrepare`](../sqlite/src/prepare.c#L842)(锁 + 重试循环)、[`sqlite3Prepare`](../sqlite/src/prepare.c#L688)(真正的编译,调 `sqlite3RunParser`)、[`sqlite3Reprepare`](../sqlite/src/prepare.c#L892)(schema 变更后重新编译,schema cookie 触发)。
- **bind/step/reset/finalize API**:`src/vdbeapi.c`——[vdbeUnbind](../sqlite/src/vdbeapi.c#L1719)、[`sqlite3_bind_*` 系列](../sqlite/src/vdbeapi.c#L1814)(第 1814-1990 行)、[`sqlite3_clear_bindings`](../sqlite/src/vdbeapi.c#L149)、[`sqlite3_reset`](../sqlite/src/vdbeapi.c#L128)、[`sqlite3_finalize`](../sqlite/src/vdbeapi.c#L99)、[`sqlite3_step`](../sqlite/src/vdbeapi.c#L978) → [`sqlite3Step`](../sqlite/src/vdbeapi.c#L836)。
- **`OP_Variable` 与重置机制**:[`OP_Variable` case](../sqlite/src/vdbe.c#L1610)(绑定值进工作寄存器)、[`sqlite3VdbeMakeReady`](../sqlite/src/vdbeaux.c#L2654)(分配寄存器骨架)、[`sqlite3VdbeRewind`](../sqlite/src/vdbeaux.c#L2600)(reset 倒带,pc=-1)、[`sqlite3VdbeReset`](../sqlite/src/vdbeaux.c#L3593)(善后)、[`sqlite3VdbeHalt`](../sqlite/src/vdbeaux.c#L3322)(关游标/清寄存器/事务善后)、[`closeAllCursors`](../sqlite/src/vdbeaux.c#L2845)、[`releaseMemArray`](../sqlite/src/vdbeaux.c#L2186)(清工作寄存器那段 inlined 优化注释尤其值得读)。
- **占位符编号与 VList**:[`sqlite3ExprAssignVarNumber`](../sqlite/src/expr.c#L1319)(四种占位符归一)、`src/util.c` 的 `sqlite3VListAdd`/`sqlite3VListNameToNum`/`sqlite3VListNumToName`(命名占位符的 name→number 表)。
- **Vdbe 结构体与状态机**:`src/vdbeInt.h` 第 458-529 行的 `struct Vdbe`(字段全列)、第 534-537 行的 `VDBE_INIT/READY/RUN/HALT_STATE` 四态定义。
- **`sqlite3_exec`(反例)**:`src/legacy.c` 第 30 行 `sqlite3_exec`——prepare+step+finalize 全捏在一条语句里,是"不复用"的对照组。
- **官方文档**:SQLite C Interface "Prepared Statements"、"Binding Values To Prepared Statements"、"One-Step Query Execution Interface"(sqlite3_exec);SQLite Architecture 的 "Theprepared Statement Object" 一节。

### 引出下一章

本章是全书"编译与执行"这一面的收束——prepared statement 把"编译一次、执行多次"这个主题讲到了底:编译的产物是 opcode + 寄存器骨架,执行是反复 bind/step/reset,reset 不重编译、`OP_Variable` 把绑定与编译解耦、`MEM_Static|MEM_FromBind` 保证浅拷贝安全。走完前 20 章,你已经能在脑子里放映一条 `SELECT` 从字符串到结果的完整旅程——SQL → Tokenizer → Parser → Code Generator → opcode → VDBE 执行 → B-tree 读页 → pager 缓存 → WAL/journal 保 ACID。下一章 P7-21 是全书收束,把 SQLite 摆到 MySQL/PG(C/S)、LevelDB(KV)旁边横向比一比,正面回答"VDBE + B-tree + 单文件这套设计到底换来了什么、付出了什么",以及嵌入式数据库这条路在端侧 AI、os_kv KV 后端、浏览器+WASM 时代的生命力。

> **下一章**:P7-21 · 全书收束:SQLite vs MySQL/PG vs LevelDB。
