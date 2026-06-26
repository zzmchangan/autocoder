# 第 1 篇 · 第 4 章 · Code Generator:AST → VDBE 字节码

> **核心问题**:上一章 Parser 把 SQL 字符串变成了一棵 AST。这棵 AST 怎么变成 VDBE 能执行的 opcode 流?更根本地问:**为什么 SQLite 要先把 AST "编译成字节码",而不是直接递归遍历 AST 执行**?这一道编译,是 SQLite 高性能(prepared statement 复用)和强表达力(优化器在 opcode 上分析)的根。本章拆透 code generator——它怎么遍历 AST、为每种 SQL 语句产出 opcode 序列,opcode 在内存里长什么样,寄存器怎么分配。

> **读完本章你会明白**:
> 1. **为什么"编译成字节码"而不是直接解释 AST**——prepared statement 复用(一次编译多次执行)、扁平指令流便于优化器分析、SELECT/INSERT/UPDATE/DELETE 都变 opcode 由同一个 VDBE 执行(统一执行模型)。这是 SQLite 和 Lua 共享的设计哲学。
> 2. **AST 怎么变成 opcode**——code generator 是一组递归遍历 AST 的函数,每种 SQL 语句(DDL/DML/SELECT)各有一个 codegen 主入口(`sqlite3Select` / `sqlite3Insert` / `sqlite3Update` / `sqlite3Delete` / `build.c` 的 DDL),内部为每种 AST 节点产出对应 opcode。
> 3. **一条 `SELECT a,b FROM t WHERE id=10` 怎么被编译成 `OpenRead/Integer/SeekRowid/Column/ResultRow/Close`**——逐条 opcode 从哪个 codegen 函数里冒出来,贴真实源码片段和行号。
> 4. **opcode 的内存表示**——`VdbeOp` 结构体的 `opcode + p1/p2/p3/p4 + p5` 五操作数模型,以及 `p4` 的多类型 union(整数 / 字符串 / `Mem` / `FuncDef` / `KeyInfo` ……)为什么这么设计。
> 5. **寄存器分配**——code generator 怎么编号寄存器、临时借用(`sqlite3GetTempReg` / `ReleaseTempReg` / `GetTempRange`)、`Parse.nMem` 这个单调水位线怎么在 `MakeReady` 时拍板成 `Vdbe.aMem[]` 的大小。

> **如果一读觉得太难**:先只记三件事——① AST 不是直接执行的,而是被 code generator 翻译成一串 opcode(扁平指令流);② 一条 SELECT 的 opcode 长这样:`OpenRead` 打开表 → `SeekRowid` 定位行 → `Column` 取列 → `ResultRow` 返回行 → `Close`,执行(下一章 P2-05)和编译(本章)是分离的;③ opcode 在内存里就是 `VdbeOp` 结构体数组,`Vdbe.aOp[]` 持有它。本章只讲"生成",不讲"执行"——执行是 VDBE 的事(P2-05)。

---

## 〇、一句话点破

> **code generator 是一组递归遍历 AST、为每种 SQL 节点往 `Vdbe.aOp[]` 里塞 opcode 的函数;`SELECT a,b FROM t WHERE id=10` 会变成一条 `OpenRead → Integer → SeekRowid → Column → Column → ResultRow → Close` 的 opcode 序列,这条序列由下一章的 VDBE 逐条执行。**

这是结论,不是理由。本章倒过来拆:先讲为什么非要"编译成字节码"而不是直接解释 AST,再讲 AST 节点怎么映射成 opcode,接着拆一条真实 SELECT 的 opcode 是从 codegen 的哪些函数里一行行生出来的,然后讲 opcode 的内存表示(`VdbeOp` 结构体)、寄存器分配,最后技巧精解拆"五操作数 + p4 多类型"和"临时寄存器借用"两个硬核技巧。

---

## 一、为什么"编译成字节码",而不是直接解释 AST

这是本章最根本的一个 why。搞清这个,后面所有 codegen 细节才有动机。

### 朴素做法:直接解释 AST

很多教学型数据库(MySQL 早期、Postgres 早期教材示例)的做法是:解析出 AST 后,直接写个递归函数遍历这棵树,走到 `SelectStmt` 节点就开表读,走到 `Where` 节点就过滤,走到 `Column` 节点就取列——边遍历边执行。这种"tree-walking interpreter"对教学够了,简单直观。

但 SQLite 偏不。它在 AST 和真实执行之间,**硬生生多塞了一层"编译"**:把 AST 翻译成一串扁平的 opcode 字节码,然后把这串字节码交给一个独立的虚拟机(VDBE)去执行。这个"多塞一层"的设计,带来三个朴素做法做不到的东西。

### 收益一:prepared statement——一次编译,多次执行

这是 SQLite(以及所有"编译成字节码"的数据库/虚拟机)高性能的头号理由。

`SELECT name FROM users WHERE id=?` 这种带占位符的 SQL,你的应用经常要反复执行(查 1000 次,每次 bind 不同的 id)。如果每次都从头解析 + 遍历 AST,你就得做 1000 次 tokenize、1000 次 parse、1000 次 AST 遍历——前面三步的产出(opcode 流)其实每次都一样,只有 `?` 那个 bind 值不同。

> **不这样会怎样**:朴素地直接解释 AST,每次执行都得重新遍历整棵 AST,Parse 的开销(切词 + 建树)无法摊销。而"编译成字节码"后,`sqlite3_prepare_v2()` 把 SQL 一次性编译成一个 prepared statement(里面就是一串 opcode,存在 `Vdbe.aOp[]`),之后 `sqlite3_bind_*` / `sqlite3_step` 反复用这串 opcode——parse 只做一次,执行 N 次。**opcode 流是"编译产物"也是"缓存单元"**。

```
   prepare 一次:
     "SELECT name FROM users WHERE id=?"
        │ tokenize → parse → codegen
        ▼
     Vdbe.aOp[] = [OpenRead, Variable(bind槽1)→reg, SeekRowid,
                   Column, ResultRow, Close, Halt]   ← 缓存住,不变

   step N 次(bind 不同 id):
     bind(1, 10);  step();   ← VDBE 拿 aOp[] 执行,只换 reg 里的值
     bind(1, 11);  step();
     bind(1, 12);  step();
     ...
```

这个设计直接对应 SQLite 的 API:`sqlite3_prepare_v2()`(编译) → `sqlite3_bind_*()`(传参)→ `sqlite3_step()`(执行一次,可重复)→ `sqlite3_finalize()`(释放)。这条 API 流程,本质就是"编译成字节码 + 缓存字节码 + 反复执行字节码"。

> **承接《Lua》**:**Lua 的 `lua_load` / `lua_pcall` 是同一个套路**——`lua_load` 把源码编译成字节码(`Proto` 结构体里挂着指令数组),`lua_pcall` 反复执行这份字节码。Lua 的 `luaK_` 系列(codegen)对应 SQLite 的 `sqlite3VdbeAddOp*` 系列;Lua 的 `Proto->code[]` 对应 SQLite 的 `Vdbe->aOp[]`。**两者都是"编译器 + 字节码虚拟机"架构**,只是输入不同(Lua 吃源码、SQLite 吃 SQL)。这个通则《Lua》那本讲过了,本书只讲 SQLite 独有的部分(Op 的四操作数模型、SQL 语句→opcode、寄存器编号)。

### 收益二:扁平指令流,优化器好下手

AST 是树状、嵌套、带作用域的。直接在 AST 上做优化(选索引、常量折叠、谓词下推、子查询展平)非常别扭——树的重写要处处小心父子关系和作用域。

opcode 是**扁平的指令数组**(`aOp[]`),一条一条排好,带跳转目标(很多 opcode 的 `p2` 就是跳转地址)。优化器在这种扁平流上做分析,就像在一段汇编上做 peephole optimize——比在 AST 树上好下手得多。

> **不这样会怎样**:直接解释 AST 的话,每次执行都得在 AST 树上临时判断"这条 WHERE 该走索引还是全表扫"——慢且难优化。而 SQLite 在 codegen 阶段就把这些决策**固化进 opcode**:比如 WHERE `id=10` 且 `id` 是 INTEGER PRIMARY KEY,codegen 直接产出 `SeekRowid`(按 rowid 直接定位,一次 B-tree 查找),根本不走 `Rewind/Next` 全表扫;WHERE `name='x'` 且有索引,codegen 产出 `OpenRead` 索引 + `SeekGE`/`IdxColumn`。**索引选择、循环顺序、谓词下推,这些优化决策在编译期就烧死进了 opcode**——执行期 VDBE 只管闷头执行,不需要边走边判断。

实际上 SQLite 的优化器就长在 codegen 里头——`src/where.c` 的 `sqlite3WhereBegin()`(WHERE 子句的 codegen 主函数,我们待会儿会看到)在产出 opcode 之前,会先跑一遍"WHERE 子句分析",决定每个表用哪个索引、怎么嵌套循环,然后**把决策编进 opcode**。这就是"编译成字节码"带来的优化空间:AST 是给人看的,opcode 是给优化器和机器执行的。

### 收益三:统一执行模型——所有语句最后都是 opcode

直接解释 AST 的话,`SELECT` / `INSERT` / `UPDATE` / `DELETE` / `CREATE TABLE` / `CREATE INDEX` / `DROP`……每种语句各写一套解释逻辑,执行入口分散。

SQLite 把所有这些语句统一编译成 opcode 后,**执行端只有一个东西——VDBE**。不管你跑的是 `SELECT` 还是 `INSERT`,最终都是 `Vdbe.aOp[]` 里一串 opcode,VDBE 一个大 switch-case 逐条执行。执行逻辑高度统一,bug 也集中在 VDBE 这一处。

> **钉死这件事**:这个"统一执行模型"的好处,在源码里直接体现为——`src/select.c` / `src/insert.c` / `src/update.c` / `src/delete.c` / `src/build.c` 这几个 codegen 主文件,**产出物全是同一种东西**(`VdbeOp` 数组),最后都喂给 `src/vdbe.c` 的 VDBE 执行。如果直接解释 AST,这几条路径会各自长一套解释器,改一处要动五处。"编译成字节码"把执行逻辑收敛到一处,VDBE 成了 SQLite 唯一的执行引擎。

### 收益四(承 Lua):执行和编译解耦,可独立演进

这是"编译器 + 虚拟机"架构的通用红利——《Lua》那本讲过。编译端可以换(`sqlite3_prepare` 可以编译成不同的 opcode 表示,SQLite 的 `prepare` 接口几十年来稳定),执行端也可以换(SQLite 的 VDBE 是稳定的,但 opcode 集合一直在演进,3.x 加了窗口函数 opcode、CTE opcode、生成列 opcode……)。两端解耦,各自演进。

> **不这样会怎样**:如果直接解释 AST,AST 的结构变了(加新 SQL 语法),解释器就得跟着改;执行逻辑和语法分析揉在一起,改动会牵一发动全身。"编译成字节码"让 AST→opcode 的翻译(codegen)和 opcode→执行(VDBE)各管一摊,SQL 语法演进(比如加窗口函数)只需要在 codegen 加几条新 opcode 的生成代码,VDBE 加对应 case,互不干扰。

---

## 二、AST 节点 → opcode:code generator 在干什么

搞清了 why,现在看 how。

### code generator = 一组递归遍历 AST、往 `aOp[]` 里塞 opcode 的函数

code generator 不是一个单独的函数,而是一**组**函数,分散在几个文件里:

| SQL 语句类型 | codegen 主入口 | 源文件 |
|---|---|---|
| `SELECT` | `sqlite3Select()` | [`src/select.c#L7590`](../sqlite/src/select.c#L7590) |
| `INSERT` | `sqlite3Insert()` | `src/insert.c#L900` |
| `UPDATE` | `sqlite3Update()` | `src/update.c` |
| `DELETE` | `sqlite3Delete()` | `src/delete.c` |
| `CREATE TABLE` / `CREATE INDEX` / DDL | `build.c` 一组函数 | `src/build.c` |
| `WHERE` 子句 + 索引选择 | `sqlite3WhereBegin()` | [`src/where.c#L6830`](../sqlite/src/where.c#L6830) |
| 表达式(列、常量、函数、运算) | `sqlite3ExprCode()` | `src/expr.c` |

这些函数的共同工作模式是:**拿一个 AST 节点 → 决定要产哪些 opcode → 调用 `sqlite3VdbeAddOp*()` 把 opcode 塞进 `Vdbe.aOp[]` → 递归处理子节点**。和 Lua 的 `luaK_expr()`(遍历表达式 AST 生成字节码)是同一个模式,只是 SQLite 的"表达式"是 SQL 表达式(列引用、比较、函数调用),Lua 的表达式是程序表达式。

### 五个塞 opcode 的函数:`sqlite3VdbeAddOp0~4`

往 `aOp[]` 里塞 opcode,有五个接口,按"带几个操作数"区分。它们都在 [`src/vdbe.h#L199-L208`](../sqlite/src/vdbe.h#L199-L208) 声明,在 `src/vdbeaux.c` 实现:

```c
int sqlite3VdbeAddOp0(Vdbe*,int);                              // 0 个操作数
int sqlite3VdbeAddOp1(Vdbe*,int,int);                          // p1
int sqlite3VdbeAddOp2(Vdbe*,int,int,int);                      // p1, p2
int sqlite3VdbeAddOp3(Vdbe*,int,int,int,int);                  // p1, p2, p3
int sqlite3VdbeAddOp4(Vdbe*,int,int,int,int,const char *zP4,int); // + p4(带类型)
int sqlite3VdbeAddOp4Int(Vdbe*,int,int,int,int,int);           // p4 是 int
```

真正干活的只有 `sqlite3VdbeAddOp3`(`src/vdbeaux.c#L270`),其余几个都是它和 `sqlite3VdbeChangeP4` 的薄包装:

```c
// src/vdbeaux.c:261-270  (简化,保留逐字一致)
int sqlite3VdbeAddOp0(Vdbe *p, int op){
  return sqlite3VdbeAddOp3(p, op, 0, 0, 0);
}
int sqlite3VdbeAddOp1(Vdbe *p, int op, int p1){
  return sqlite3VdbeAddOp3(p, op, p1, 0, 0);
}
int sqlite3VdbeAddOp2(Vdbe *p, int op, int p1, int p2){
  return sqlite3VdbeAddOp3(p, op, p1, p2, 0);
}
int sqlite3VdbeAddOp3(Vdbe *p, int op, int p1, int p2, int p3){
  int i;
  VdbeOp *pOp;
  i = p->nOp;                                 // ← 新 opcode 落在 aOp[] 末尾
  assert( p->eVdbeState==VDBE_INIT_STATE );   // ← 只能在"编译期"塞,运行期不能塞
  assert( op>=0 && op<0xff );
  if( p->nOpAlloc<=i ){
    return growOp3(p, op, p1, p2, p3);        // ← 容量不够,先扩容再重试
  }
  p->nOp++;
  pOp = &p->aOp[i];
  pOp->opcode = (u8)op;
  pOp->p5 = 0;
  pOp->p1 = p1;
  pOp->p2 = p2;
  pOp->p3 = p3;
  pOp->p4.p = 0;
  pOp->p4type = P4_NOTUSED;
  return i;                                   // ← 返回新 opcode 的地址(addr)
}
```

几个关键点(后面技巧精解会展开):

1. **`AddOp3` 是唯一的"塞 opcode"原语**。`AddOp0/1/2` 都是给它喂 0。返回值 `i` 是这条 opcode 在 `aOp[]` 里的下标,叫 **addr**(地址),后面 codegen 用这个 addr 做跳转目标。
2. **只能编译期塞**:`assert( p->eVdbeState==VDBE_INIT_STATE )`——VDBE 有四个状态(`VDBE_INIT_STATE` 编译中 / `VDBE_READY_STATE` 待执行 / `VDBE_RUN_STATE` 执行中 / `VDBE_HALT_STATE` 结束,见 `src/vdbeInt.h`),只有 `INIT` 状态能加 opcode。这把"编译"和"执行"在源码层面物理隔离了。
3. **`aOp[]` 容量不够自动扩容**:`growOp3`(`src/vdbeaux.c#L225`)是 `AddOp3` 的慢路径(被 `SQLITE_NOINLINE` 标注,避免污染热路径的 icache),它先调 `growOpArray` 把 `aOp[]` 扩容,再递归调一次 `AddOp3`。**热路径常见情况(`nOp < nOpAlloc`)不进函数调用,内联成一个数组写**——这是为 codegen 高频塞 opcode 优化的。

### 怎么把 p4 塞进去

`p4` 比较特殊——它是个 union(待会儿讲),可能是整数、字符串、`FuncDef*`、`KeyInfo*` 等等,所以"塞 p4"单独抽出来:

```c
// src/vdbeaux.c:412-422
int sqlite3VdbeAddOp4(
  Vdbe *p, int op, int p1, int p2, int p3,
  const char *zP4,    /* P4 操作数 */
  int p4type          /* P4 操作数类型(P4_INT32/P4_KEYINFO/P4_FUNCDEF/...) */
){
  int addr = sqlite3VdbeAddOp3(p, op, p1, p2, p3);
  sqlite3VdbeChangeP4(p, addr, zP4, p4type);
  return addr;
}
```

`AddOp4Int`(`src/vdbeaux.c#L315`)是 `p4` 为 `int` 的快路径(它不调 `ChangeP4`,直接写 `pOp->p4.i = p4; pOp->p4type = P4_INT32;`,内联在 `AddOp4Int` 里,比走 `ChangeP4` 的通用路径快)——这种"快路径内联 + 慢路径外提"的代码布局,我们在技巧精解里拆。

---

## 三、一条 SELECT 的 AST → opcode:逐条拆

现在用招牌例子把 codegen 跑一遍:`SELECT a,b FROM t WHERE id=10`,假设 `id` 是 `INTEGER PRIMARY KEY`(也就是 rowid 别名)。

### 这条 SQL 编译出来的 opcode

你可以在 `sqlite3` CLI 里跑 `EXPLAIN SELECT a,b FROM t WHERE id=10;`,会看到类似这样的 opcode 流(精简,真实输出会多几条 `Init` / `Goto` / `Halt`,我们后面对照真实输出):

```
addr  opcode      p1   p2   p3   p4          注释
----  ----------  ---  ---  ---  ----------  -------------------------
0     OpenRead    0    2    0    0           # 打开表 t(根页 2)的游标 0,只读
1     Integer     10   1    0                # 常量 10 放进寄存器 1
2     SeekRowid   0    4    1                # 在游标 0 按 rowid(=寄存器1)定位;没找到跳到 addr4
3     Column      0    1    2                # 游标 0 取列 a(列号 1)放进寄存器 2
4     Column      0    2    3                # 游标 0 取列 b(列号 2)放进寄存器 3
        ↑(注:这里 addr 是 EXPLAIN 重排的示意,真实是连续编号;真实 SeekRowid 的 p2 跳到 NotFound 分支)
5     ResultRow   2    2    0                # 把寄存器 2..3(共 2 列)作为一行结果返回
6     Close       0    0    0                # 关闭游标 0
```

(上面是为讲解精简、地址连续化的示意;真实 EXPLAIN 会多 `Init` / `Goto` / 跳转的 `NotFound` 处理 / `Halt`,但核心 6 条就是这 6 条。)

VDBE 下一章会拿着这串 opcode 一条条执行。本章只关心:**这 6 条 opcode 分别从 codegen 的哪个函数里生出来**。

### AST 节点 → opcode 的映射

先把映射铺出来,再逐条拆:

```
   SQL: SELECT a, b FROM t WHERE id=10
   AST(简化):
        SelectStmt
        ├── pEList(结果列): [a, b]
        ├── pSrc(表源):     [t]
        └── pWhere(WHERE):  id = 10

   codegen 遍历这棵 AST,产出 opcode:

   ┌─────────────────────────┬───────────────────────────────────────────┬───────────────────────────────┐
   │ AST 节点 / 阶段          │ 产出的 opcode                              │ 产出它的 codegen 函数          │
   ├─────────────────────────┼───────────────────────────────────────────┼───────────────────────────────┤
   │ 进入 sqlite3Select()      │ (准备 Vdbe,分配结果寄存器)                  │ sqlite3Select() select.c:7590 │
   │ 处理 FROM t              │ OpenRead 0,2,0                            │ sqlite3WhereBegin() where.c   │
   │                         │                                           │   → sqlite3OpenTable() insert.c:26 │
   │ 处理 WHERE id=10         │ Integer 10,1                              │ sqlite3ExprCode() expr.c      │
   │                         │   (常量 10 → 寄存器 1)                       │   → codeInteger() expr.c:4335 │
   │                         │ SeekRowid 0,NotFound,1                    │ codeAllEq() / wherecode.c     │
   │                         │   (按 rowid 在游标 0 定位)                   │   → sqlite3VdbeAddOp3 OP_SeekRowid │
   │ 处理 SELECT 列 a, b       │ Column 0,1,2   Column 0,2,3              │ sqlite3ExprCode() expr.c      │
   │                         │   (取 a,b 放进结果寄存器 2,3)                 │   → sqlite3ExprCodeGetColumn() │
   │ 处理结果输出              │ ResultRow 2,2                             │ selectInnerLoop() select.c:1864│
   │ 循环/收尾                 │ Close 0                                    │ sqlite3WhereEnd() where.c     │
   └─────────────────────────┴───────────────────────────────────────────┴───────────────────────────────┘
```

### codegen 主入口:`sqlite3Select()`

`SELECT` 的 codegen 主函数在 [`src/select.c#L7590`](../sqlite/src/select.c#L7590),签名:

```c
int sqlite3Select(
  Parse *pParse,         /* 解析上下文(持有正在编译的 Vdbe) */
  Select *p,             /* SELECT 的 AST 节点 */
  SelectDest *pDest      /* 结果往哪写(返回客户端 / 写临时表 / 喂给外层) */
);
```

它干的事情(只看主干,跳过窗口函数 / 聚合 / DISTINCT / 子查询这些复杂分支):

```c
// src/select.c:7590 起(简化,保留主干逐字)
int sqlite3Select(Parse *pParse, Select *p, SelectDest *pDest){
  ...
  v = sqlite3GetVdbe(pParse);              // ← 拿到/创建正在编译的 Vdbe
  ...
  sqlite3SelectPrep(pParse, p, 0);         // ← 名字解析、视图展开等预处理
  ...
  // 1. 处理 FROM(打开表)→ 由 sqlite3WhereBegin 完成
  // 2. 处理 WHERE + 选索引 + 决定循环结构 → 由 sqlite3WhereBegin 完成
  pWInfo = sqlite3WhereBegin(pParse, pTabList, pWhere, ...);
  //    ↑ 这一句内部产出了 OpenRead / Integer / SeekRowid
  
  // 3. 处理 SELECT 列(取列值放进结果寄存器)→ selectInnerLoop
  selectInnerLoop(pParse, p, pEList, ...);
  //    ↑ 这一句内部产出了 Column / ResultRow
  
  // 4. 收尾(Close 等)→ sqlite3WhereEnd
  sqlite3WhereEnd(pWInfo);
  //    ↑ 这一句产出 Close
  ...
}
```

注意 `sqlite3GetVdbe(pParse)`——它返回当前 `Parse` 正在编译的 `Vdbe`(没有就 `sqlite3VdbeCreate` 创建一个)。所有 codegen 函数都通过 `Parse->pVdbe` 拿到这个 Vdbe,然后往它的 `aOp[]` 里塞 opcode。**一个 prepared statement 对应一个 Vdbe**,这就是 `Vdbe` 和"一次编译"的对应关系。

### 阶段一:FROM + WHERE → OpenRead / Integer / SeekRowid

`sqlite3WhereBegin()`([`src/where.c#L6830`](../sqlite/src/where.c#L6830))是 WHERE 子句 + 循环结构的 codegen 主函数,它做了三件事:① 分析 WHERE,决定每个表用哪个索引、循环怎么嵌套;② 产出"打开表"的 opcode(`OpenRead`);③ 产出"循环开头 + 定位"的 opcode(`Rewind` / `SeekRowid` / `SeekGE` 等)。它返回一个 `WhereInfo*`,里面有循环的地址信息,后面 `sqlite3WhereEnd` 用它来产出"循环结尾"(`Next` / `Close`)。

打开表的 opcode(`OpenRead`)来自 [`sqlite3OpenTable()`](../sqlite/src/insert.c#L26):

```c
// src/insert.c:26-50  (简化)
void sqlite3OpenTable(
  Parse *pParse, int iCur, int iDb, Table *pTab, int opcode
){
  Vdbe *v = pParse->pVdbe;
  assert( opcode==OP_OpenWrite || opcode==OP_OpenRead );
  if( !pParse->db->noSharedCache ){
    sqlite3TableLock(pParse, iDb, pTab->tnum, (opcode==OP_OpenWrite)?1:0, pTab->zName);
  }
  if( HasRowid(pTab) ){
    sqlite3VdbeAddOp4Int(v, opcode, iCur, pTab->tnum, iDb, pTab->nNVCol);
    //                       ↑    ↑     ↑      ↑       ↑
    //                      游标号  根页号  库号   列数  (p4=列数,运行期检查用)
    VdbeComment((v, "%s", pTab->zName));
  }else{
    // WITHOUT ROWID 表:走索引(KeyInfo)
    Index *pPk = sqlite3PrimaryKeyIndex(pTab);
    sqlite3VdbeAddOp3(v, opcode, iCur, pPk->tnum, iDb);
    sqlite3VdbeSetP4KeyInfo(pParse, pPk);
  }
}
```

注意这条 `sqlite3VdbeAddOp4Int(v, opcode, iCur, pTab->tnum, iDb, pTab->nNVCol)` —— 它就是 `OpenRead` opcode 的诞生地。`p1=iCur`(游标号)、`p2=pTab->tnum`(表的根页号,B-tree 下一章讲)、`p3=iDb`(库号,主库通常是 0)、`p4=nNVCol`(列数,VDBE 执行时会用它做完整性检查)。**这就是 AST 节点 `FROM t` 翻译成 opcode 的全过程**:codegen 从 `Table` 结构体里掏出根页号、列数,塞进 `OpenRead` 的四个操作数。

WHERE `id=10` 的处理分两步:① 把常量 10 算出来放进寄存器(`Integer 10,1`);② 用这个寄存器按 rowid 定位(`SeekRowid`)。

常量 10 的产出在表达式 codegen 里——`sqlite3ExprCode()`(表达式 codegen 主函数,`src/expr.c`)遍历到 `id=10` 这个比较表达式时,先算右边的常量 10,走到 `codeInteger()`(`src/expr.c#L4335`):

```c
// src/expr.c:4335 起  (简化)
static void codeInteger(Parse *pParse, Expr *pExpr, int negFlag, int iMem){
  Vdbe *v = pParse->pVdbe;
  if( pExpr->flags & EP_IntValue ){
    int i = pExpr->u.iValue;
    if( negFlag ) i = -i;
    sqlite3VdbeAddOp2(v, OP_Integer, i, iMem);
    //                       ↑        ↑  ↑
    //                    opcode     常量  目标寄存器
  }else{
    // 大整数或带符号走 OP_Int64
    ...
  }
}
```

`sqlite3VdbeAddOp2(v, OP_Integer, i, iMem)` 这一行,就是 `Integer 10,1` 的诞生。`OP_Integer` 的 `p1` 是要写的常量值,`p2` 是目标寄存器号。

按 rowid 定位 `SeekRowid` 的产出,在 WHERE codegen 的"等值查找"分支里(`codeAllEq()` / `wherecode.c`)。当优化器发现 `WHERE id=10` 且 `id` 是 INTEGER PRIMARY KEY(rowid 别名)时,它直接产出 `SeekRowid`,而不是走全表扫的 `Rewind/Next`:

```c
// 简化示意:WHERE id=10 且 id 是 rowid 别名时
sqlite3VdbeAddOp3(v, OP_SeekRowid, iCur, destIfNotFound, regRowid);
//                              ↑    ↑                ↑
//                           游标号  找不到时跳转的目标    装 rowid 值的寄存器
```

真实代码在 `src/wherecode.c:1709` 和 `src/expr.c:4209` 都有 `OP_SeekRowid` 的产出(一个在 WHERE 范围扫描、一个在 `IN` 表达式求值)。

> **不这样会怎样**:如果优化器不做这个决策、直接走"朴素全表扫",这条 `WHERE id=10` 会变成 `OpenRead → Rewind → Column(id) → Ne(不等就跳 Next) → Next → ……`——一个完整的全表扫循环,B-tree 有多少行就扫多少行(可能几百万行)。而 `SeekRowid` 是一次 B-tree 查找(对数复杂度,几层 B-tree 节点就到)。**"编译成字节码"让优化器在 codegen 阶段就把"走索引还是全表扫"固化进 opcode**,这是它带来的最大执行效率红利之一。直接解释 AST 的话,这个决策要么每次执行临时做(慢),要么干脆不做(永远全表扫)。

### 阶段二:SELECT 列 → Column / ResultRow

WHERE 处理完,游标已经定位在目标行上了。接下来要取 `a, b` 两列,放进结果寄存器,然后 `ResultRow` 返回。

取列的 opcode 是 `Column`,表达式 codegen 在遍历到"列引用"(`TK_COLUMN`)AST 节点时产出:

```c
// src/expr.c:4488 起  (简化)
int sqlite3ExprCodeGetColumn(...){
  ...
  sqlite3ExprCodeGetColumnOfTable(v, pTab, iTable, iColumn, iReg);
  //  ↑ 内部产出:sqlite3VdbeAddOp3(v, OP_Column, iTabCur, iColumn, iReg);
  //                                      ↑         ↑        ↑
  //                                    游标号      列号      目标寄存器
}
```

`SELECT a, b` 会被 `selectInnerLoop()`([`src/select.c`](../sqlite/src/select.c))逐列处理:对 `a` 产出 `Column 0, 1, 2`(游标 0,列号 1,放进结果寄存器 2),对 `b` 产出 `Column 0, 2, 3`。

最后是 `ResultRow`,在 `selectInnerLoop` 的 `SRT_Output` 分支产出(我们前面读到的):

```c
// src/select.c:1451(简化,去掉排序分支)
case SRT_Output: {
  ...
  sqlite3VdbeAddOp2(v, OP_ResultRow, regResult, nResultCol);
  //                              ↑           ↑
  //                          第一个结果寄存器   列数
}
```

`OP_ResultRow` 的 `p1` 是第一个结果寄存器号,`p2` 是列数。VDBE 执行到这条时,就把 `aMem[p1..p1+p2-1]` 这几个寄存器当作一行结果返回给客户端(通过回调)。

### 阶段三:收尾 → Close

`sqlite3WhereEnd(pWInfo)`(`src/where.c`)产出循环结尾:对全表扫它产出 `Next`(回到循环开头扫下一行);对 `SeekRowid` 这种点查(只查一行),它产出 `Close`(关闭游标)。这就是 `Close 0` 的来源。

> **钉死这件事**:把上面三阶段拼起来,就是一条 `SELECT a,b FROM t WHERE id=10` 在 codegen 里的一生:**`sqlite3Select` 主入口 → `sqlite3WhereBegin`(产 OpenRead/Integer/SeekRowid)→ `selectInnerLoop`(产 Column/ResultRow)→ `sqlite3WhereEnd`(产 Close)**。每个 codegen 函数只往 `aOp[]` 里塞 opcode,不执行——执行是下一章 VDBE 的事。**编译和执行在 SQLite 里物理分离,这是它和"边解析边执行"数据库的根本区别**。

---

## 四、opcode 的内存表示:`VdbeOp` 结构体

现在看 opcode 在内存里到底长什么样。这是本章的硬核之一。

### `VdbeOp`:一条 opcode 的内存布局

每条 opcode 是一个 `VdbeOp` 结构体([`src/vdbe.h#L55-L95`](../sqlite/src/vdbe.h#L55-L95)):

```c
struct VdbeOp {
  u8 opcode;          /* 操作码:OP_OpenRead / OP_SeekRowid / ... */
  signed char p4type; /* p4 的类型(P4_INT32 / P4_KEYINFO / P4_FUNCDEF / ...) */
  u16 p5;             /* 第五个操作数,16 位无符号(标志位) */
  int p1;             /* 第一操作数 */
  int p2;             /* 第二操作数(常作跳转目标) */
  int p3;             /* 第三操作数 */
  union p4union {     /* 第四操作数(多类型) */
    int i;                 /* P4_INT32 */
    void *p;               /* 通用指针 */
    char *z;               /* 字符串 */
    i64 *pI64;             /* P4_INT64 */
    double *pReal;         /* P4_REAL */
    FuncDef *pFunc;        /* P4_FUNCDEF:函数定义对象 */
    sqlite3_context *pCtx; /* P4_FUNCCTX:函数上下文 */
    CollSeq *pColl;        /* P4_COLLSEQ:排序规则 */
    Mem *pMem;             /* P4_MEM:一个内存单元 */
    VTable *pVtab;         /* P4_VTAB */
    KeyInfo *pKeyInfo;     /* P4_KEYINFO:索引比较规则 */
    u32 *ai;               /* P4_INTARRAY */
    SubProgram *pProgram;  /* P4_SUBPROGRAM:子程序(触发器) */
    Table *pTab;           /* P4_TABLE */
    SubrtnSig *pSubrtnSig; /* P4_SUBRTNSIG */
    Index *pIdx;           /* P4_INDEX */
#ifdef SQLITE_ENABLE_CURSOR_HINTS
    Expr *pExpr;           /* P4_EXPR */
#endif
  } p4;
#ifdef SQLITE_ENABLE_EXPLAIN_COMMENTS
  char *zComment;          /* 注释(EXPLAIN 时显示,生产构建里不编译) */
#endif
  ...
};
```

一条 opcode 在内存里长这样(`SQLite` 默认构建,不带 EXPLAIN_COMMENTS):

```
   一条 VdbeOp(默认构建,不含可选字段):
   ┌────────────────────────────────────────────────────────────────┐
   │ opcode (u8)   │ p4type (s8)  │ p5 (u16) │ p1 (int) │ p2 (int) │
   │  操作码        │  p4 类型      │  标志位   │  操作数1 │  操作数2 │
   ├────────────────────────────────────────────────────────────────┤
   │ p3 (int)      │ p4 (union: 8 字节,放 i / 指针 / 双精度)          │
   │  操作数3       │                                              │
   └────────────────────────────────────────────────────────────────┘
   约 32 字节一条(SQLite 的 B-tree 页 / 内存对齐精心调过)
```

注意几个设计决策:

1. **五个操作数,但大多数 opcode 只用前三个(`p1/p2/p3`)**。`p4` 只在"需要附加数据"时用(比如 `OpenRead` 的列数、`Function` 的 `FuncDef*`、`IdxInsert` 的 `KeyInfo*`)。`p5` 是"标志位"小整数(比如 `OPFLAG_APPEND`、约束类型 `P5_ConstraintUnique` 等)。**五操作数的设计让绝大多数 opcode 用最少的字段表达,只有少数需要附加数据的 opcode 才付 p4 的成本**。
2. **`opcode` 是 `u8`,所以最多 256 种 opcode**。SQLite 实际用了约 150 种(opcode 编号在 `src/opcode.h`,由 `mkopcodeh.tcl` 从 `vdbe.c` 的 case 标签自动生成)。**用 u8 而不是 int,是为了让 `aOp[]` 紧凑**——一条 opcode 占的内存越小,`aOp[]` 整个数组在 CPU cache 里的命中率越高(VDBE 执行时是顺序扫 `aOp[]` 的,cache 友好)。
3. **`p4` 是 union,用 `p4type` 区分**。这是"同一段内存承载多种类型数据"的经典 C 技巧——不浪费空间,又能表达整数、指针、字符串等异质数据。

### 五个操作数各自的角色

把 `p1/p2/p3/p4/p5` 的典型角色汇总(每种 opcode 用法不同,这是最常见的):

| 操作数 | 类型 | 典型角色 | 例子 |
|---|---|---|---|
| `p1` | int | **游标号** 或 标志 | `OpenRead 0,...` 的 0 是游标号 |
| `p2` | int | **跳转目标**(JUMP opcode)或 第二参数 | `SeekRowid`,p2=没找到时跳哪 |
| `p3` | int | 第三参数(常是寄存器号或列号) | `Column` 的 p3=目标寄存器 |
| `p4` | union | **附加数据**(类型由 p4type 定) | `Function` 的 p4=`FuncDef*` |
| `p5` | u16 | **标志位**(小整数) | `Insert` 的 `OPFLAG_APPEND` |

这个设计有意为之:**让"最常用"的三个操作数(p1/p2/p3)是定长 int,放结构体最前面(对齐好);把"少数 opcode 才用"的 p4 用 union 收尾,p5 用 u16 塞缝**。整条 opcode 在内存里紧凑且 cache 友好。

### 跳转地址:opcode 流里的"控制流"

很多 opcode 是 JUMP(跳转)opcode——它们的 `p2` 是跳转目标(另一个 opcode 的 addr)。这让 opcode 流能表达 `if/else`、循环:`SeekRowid` 没找到就跳到 NotFound 分支、`Rewind` 空表就跳过整个循环、`Next` 没扫完就回到循环开头……

codegen 怎么处理这种"我还不知道跳转目标"的情况?用 label:

```c
// 简化示意(SQLite 内部机制)
int addrLabel = sqlite3VdbeMakeLabel(pParse);   // ← 申请一个尚未解析的 label
sqlite3VdbeAddOp2(v, OP_IfNot, regCond, addrLabel); // 先塞 opcode,p2=占位 label
...
// 中间塞其他 opcode
...
sqlite3VdbeResolveLabel(v, addrLabel);          // ← 现在 label 指向"这里",回填所有引用它的 p2
```

`MakeLabel` / `ResolveLabel` 是 codegen 表达"前向跳转"的标准手段——和汇编器里 label 一模一样,和 Lua `luaK_jump` / `luaK_patchlist` 也是同一个套路(承《Lua》编译器)。SQLite 在 `MakeReady` 阶段会跑一遍 `resolveP2Values()`([`src/vdbeaux.c#L879`](../sqlite/src/vdbeaux.c#L879)),把所有未解析的 label 回填成真实的 `aOp[]` 下标。**这是"扁平 opcode 流 + 跳转"实现控制流的核心机制**。

---

## 五、寄存器分配:中间值放哪

opcode 之间怎么传值?靠**寄存器**。这是 SQLite VDBE 和 Lua VM 共享的另一个概念(承《Lua》虚拟机:Lua VM 也是寄存器式,SQLite VDBE 大量用寄存器)。

### 寄存器是什么:就是 `Vdbe.aMem[]` 的一个槽

SQLite 的"寄存器"不是 CPU 寄存器,而是 **`Vdbe.aMem[]` 数组里的一个槽**——每个槽是一个 `Mem` 结构体(SQLite 的动态类型值,能装 int/real/text/blob/null,见 `src/vdbeInt.h`)。寄存器号就是 `aMem[]` 的下标。

```
   Vdbe 持有的寄存器堆 aMem[]:
   ┌─────┬─────┬─────┬─────┬─────┬─────┬─────────┐
   │  0  │  1  │  2  │  3  │  4  │  5  │  ......  │  ← 共 nMem 个槽
   │ (空) │ r1  │ r2  │ r3  │ r4  │ r5  │         │
   └─────┴─────┴─────┴─────┴─────┴─────┴─────────┘
              ↑     ↑     ↑
              Integer 10 放这(Column 取的 a 放这……
```

`OP_Integer 10, 1` 的意思是:`aMem[1] = 10`。`OP_Column 0, 1, 2` 的意思是:`aMem[2] = cursor[0].column[1]`。`OP_ResultRow 2, 2` 的意思是:把 `aMem[2..3]` 当一行结果返回。

寄存器号是 codegen 在编译期分配的(下一小节),VDBE 在执行期按号访问 `aMem[]`。**寄存器号是编译期常量,运行期不变**——这让 VDBE 取寄存器是 `O(1)` 数组访问,极快。

### 寄存器怎么分配:`Parse.nMem` 水位线 + 临时寄存器池

codegen 分配寄存器的核心机制在 `Parse` 结构体([`src/sqliteInt.h#L3882`](../sqlite/src/sqliteInt.h#L3882))里有几个字段:

```c
struct Parse {
  ...
  u8 nTempReg;         /* aTempReg[] 里当前有几个回收的临时寄存器 */
  int nRangeReg;       /* 临时连续寄存器块的大小 */
  int iRangeReg;       /* 临时连续寄存器块的起始号 */
  int nTab;            /* 已分配的游标数(也即游标号水位线) */
  int nMem;            /* 已用掉的内存单元(寄存器)数 ← 关键水位线 */
  ...
  int aTempReg[8];     /* 临时寄存器回收栈(最多 8 个) */
  ...
};
```

`Parse.nMem` 是**寄存器号水位线**——codegen 每需要一个"长期持有"的寄存器(比如装结果列、装 bind 值),就 `++pParse->nMem`,返回新的寄存器号。**这种"单调递增水位线"是最朴素的寄存器分配**——不回收,简单粗暴,但保证每个长期寄存器号唯一。

但 codegen 中大量需要的是**短期临时寄存器**——比如算 `a+b` 时,要先把 `a` 取到一个临时寄存器、`b` 取到另一个临时寄存器,再算加法,算完这两个临时寄存器就没用了。如果每次都 `++nMem`,寄存器号会爆炸(一条复杂表达式的 codegen 可能要几百个临时寄存器),`Vdbe.aMem[]` 会很大。

SQLite 用一个**临时寄存器池**解决这个问题——`aTempReg[8]`(单寄存器回收栈)和 `iRangeReg/nRangeReg`(连续寄存器块回收):

```c
// src/expr.c:7610-7615  分配一个临时寄存器
int sqlite3GetTempReg(Parse *pParse){
  if( pParse->nTempReg==0 ){
    return ++pParse->nMem;              // ← 池空了,从水位线分配新号
  }
  return pParse->aTempReg[--pParse->nTempReg];  // ← 池里有,复用回收的号
}

// src/expr.c:7621-7628  归还一个临时寄存器
void sqlite3ReleaseTempReg(Parse *pParse, int iReg){
  if( iReg ){
    sqlite3VdbeReleaseRegisters(pParse, iReg, 1, 0, 0);
    if( pParse->nTempReg<ArraySize(pParse->aTempReg) ){
      pParse->aTempReg[pParse->nTempReg++] = iReg;  // ← 压回栈,下次复用
    }
  }
}
```

```c
// src/expr.c:7633-7646  分配/归还连续寄存器块
int sqlite3GetTempRange(Parse *pParse, int nReg){
  int i, n;
  if( nReg==1 ) return sqlite3GetTempReg(pParse);
  i = pParse->iRangeReg;
  n = pParse->nRangeReg;
  if( nReg<=n ){
    pParse->iRangeReg += nReg;          // ← 从块尾切 nReg 个
    pParse->nRangeReg -= nReg;
  }else{
    i = pParse->nMem+1;                 // ← 块不够大,从水位线分配
    pParse->nMem += nReg;
  }
  return i;
}
void sqlite3ReleaseTempRange(Parse *pParse, int iReg, int nReg){
  if( nReg==1 ){ sqlite3ReleaseTempReg(pParse, iReg); return; }
  sqlite3VdbeReleaseRegisters(pParse, iReg, nReg, 0, 0);
  if( nReg>pParse->nRangeReg ){
    pParse->nRangeReg = nReg;           // ← 整块归还,下次复用
    pParse->iRangeReg = iReg;
  }
}
```

这段代码的设计很精妙(技巧精解里展开):

1. **`aTempReg[8]` 是一个定长栈**——最多回收 8 个单寄存器。多了就丢(`if( pParse->nTempReg < ArraySize(aTempReg) )`),因为 codegen 同时活跃的临时寄存器很少超过 8 个,栈够用。**定长数组 vs 链表**:定长数组无 malloc,访问快,这是嵌入式场景的取舍。
2. **`iRangeReg/nRangeReg` 是"整块回收"**——当 codegen 一次性借了一片连续寄存器(比如取一行所有列,要 N 个寄存器),归还时整片压回,下次同样需求直接复用整片。这避免了"逐个回收导致不连续"。
3. **`GetTempRange` 优先用块、不够再从水位线分配**——先看回收块够不够大,够就从块尾切;不够才动水位线 `nMem`。这让一片连续寄存器能反复复用。

### 寄存器号怎么拍板成 `Vdbe.aMem[]` 大小:`sqlite3VdbeMakeReady()`

codegen 跑完后,`Parse.nMem` 是寄存器号的最大值。但 `Vdbe` 还没分配真正的 `aMem[]` 数组——这要等 `sqlite3VdbeMakeReady()`([`src/vdbeaux.c#L2654`](../sqlite/src/vdbeaux.c#L2654)),它把 codegen 阶段累积的"需要多少寄存器 / 多少游标 / 多少 bind 参数"拍板成 `Vdbe` 的最终数组大小:

```c
// src/vdbeaux.c:2654-2700  (简化,保留逐字)
void sqlite3VdbeMakeReady(Vdbe *p, Parse *pParse){
  ...
  nVar = pParse->nVar;        // ← bind 参数个数
  nMem = pParse->nMem;        // ← 寄存器数(codegen 累积的水位线)
  nCursor = pParse->nTab;     // ← 游标数
  nArg = pParse->nMaxArg;
  
  /* 每个游标借用一个内存单元 */
  nMem += nCursor;
  if( nCursor==0 && nMem>0 ) nMem++;  /* aMem[0] 即便不用也留出来 */
  
  /* resolveP2Values 把 label 回填成真实地址,同时算 readOnly 标志 */
  resolveP2Values(p, &nArg);
  p->usesStmtJournal = (u8)(pParse->isMultiWrite && pParse->mayAbort);
  ...
  /* 真正分配 aMem[] / aVar[] / apCsr[] */
  p->aMem = allocSpace(&x, 0, nMem*sizeof(Mem));
  ...
}
```

注意 `nMem += nCursor`——**游标也借用 `aMem[]` 的尾部空间**(每个 `VdbeCursor` 占一个 `Mem` 槽,见注释 `Each cursor uses a memory cell`)。这是个紧凑布局:`aMem[]` 既装寄存器又装游标,减少一次内存分配。`allocSpace()` 还有一个精巧设计:**优先复用 `aOp[]` 数组尾巴上的空闲空间**(`MakeReady` 注释里写 "try to reuse unused memory at the end of the opcode array")——`aOp[]` 分配时一般会多分配一些(`nOpAlloc > nOp`),多出来的字节就拿来装 `aMem[]` / `aVar[]` / `apCsr[]`,**省一次 `malloc`**。这是嵌入式数据库省内存的典型手段。

> **钉死这件事**:寄存器分配在 SQLite 里是**编译期完成**的——codegen 走完,所有寄存器号都是常量,VDBE 执行期只做 `aMem[i]` 数组访问。**临时寄存器用池复用,长期寄存器用水位线单调分配,最后 `MakeReady` 拍板数组大小**。这套机制让 SQLite 的 opcode 既表达力强(寄存器够用),又紧凑(临时寄存器复用、`aMem/aOp` 共享分配)。

---

## 六、不同 SQL 语句的 codegen 入口对照

到目前为止我们用 SELECT 讲完了 codegen 的主干。其他 SQL 语句的 codegen 模式完全一样(遍历 AST、塞 opcode),只是入口不同、产的 opcode 不同。汇总对照:

| SQL 语句 | codegen 入口 | 典型 opcode 序列 |
|---|---|---|
| `SELECT` | `sqlite3Select()` `select.c:7590` | `OpenRead → Seek/Rewind → Column → ResultRow → Next → Close` |
| `INSERT` | `sqlite3Insert()` `insert.c:900` | `OpenWrite → (取值/计算默认值) → NewRowid → Insert → Close` |
| `UPDATE` | `sqlite3Update()` `update.c` | `OpenWrite → Seek → (取旧值/算新值) → Column → Insert(覆盖) → Next` |
| `DELETE` | `sqlite3Delete()` `delete.c` | `OpenWrite → Seek → Delete → Next` |
| `CREATE TABLE` | `build.c` 的 `sqlite3EndTable()` 等 | (写 `sqlite_master` 表的 opcode:`OpenWrite sqlite_master → Column(各列) → Insert`) |
| `CREATE INDEX` | `build.c` 的 `sqlite3CreateIndex()` | (扫表 + 建索引 B-tree 的 opcode) |

**关键洞察**:DDL(建表建索引)在 SQLite 里也是 opcode——`CREATE TABLE` 实际上被编译成"往 `sqlite_master` 系统表插一行"的 opcode 序列。**这是"统一执行模型"的极致体现:连 DDL 都走 VDBE**。这也是为什么 `CREATE TABLE` 能在事务里、能 rollback——它本质就是一组 `Insert` opcode,rollback 时这些 `Insert` 跟数据 `Insert` 一样被回滚。

> **承接《Lua》编译器**:Lua 的 `luaK_*` 系列(`luaK_expr` / `luaK_jump` / `luaK_patchlist`)是 Lua 的 codegen,它们遍历 Lua AST 产出 Lua 字节码;SQLite 的 `sqlite3VdbeAddOp*` 系列 + `sqlite3ExprCode` / `sqlite3Select` / `sqlite3Insert` …… 是 SQLite 的 codegen,遍历 SQL AST 产出 VDBE 字节码。**两者是完全同构的"AST → 字节码"翻译器**。《Lua》那本讲透了 codegen 的通则(遍历、回填跳转、寄存器编号),本书只讲 SQLite 独有的——SQL 语句特有的 opcode 种类、`VdbeOp` 的四操作数模型、临时寄存器池。

---

## 七、为什么 sound:codegen 的正确性从哪来

讲到这里你可能会担心:**codegen 怎么保证产出的 opcode 一定执行出正确结果**?这个担心是合理的——opcode 是手写的指令序列,一个 `p1` 写错、一个跳转目标错,结果就全错。

SQLite 用几个手段保证 codegen 的 sound:

1. **每种 SQL 语义对应固定的 opcode 模板**。比如"等值点查走 rowid"永远产出 `Integer + SeekRowid`,"全表扫"永远产出 `Rewind + Column + Next`。**codegen 是机械地把语义模板填进 opcode**,不是临时拼凑——模板是经过充分测试的(TH3 测试套件,100% 分支覆盖)。
2. **`assert` 大量用**。比如 `AddOp3` 里 `assert( op>=0 && op<0xff )`(opcode 编号合法)、`sqlite3OpenTable` 里 `assert( opcode==OP_OpenWrite || opcode==OP_OpenRead )`。这些 assert 在 debug 构建里把关,生产构建里编译掉。
3. **VDBE 执行时还会校验**。比如 `OpenRead` 的 `p4=列数`,VDBE 执行时会用它检查"游标打开的表是否真的有这么多列"(防 schema 变化)。这是编译期和执行期的双重保险。
4. **label 回填机制保证跳转目标有效**。`MakeReady` 跑 `resolveP2Values` 时,如果某个 label 没被 `ResolveLabel`,会触发 assert。**所有 JUMP opcode 的 p2 在执行前一定被回填成有效地址**——这是"扁平 opcode 流 + 跳转"能正确表达控制流的保证。

> **钉死这件事**:SQLite 的 codegen 不是"凭灵感拼 opcode",而是"一套经过充分测试的、从 SQL 语义到 opcode 模板的机械映射"。这种机械性是它 sound 的根——每条 SQL 语义对应固定的 opcode 模板,模板被 TH3 充分测试,加上 assert 和执行期校验,codegen 产出的 opcode 执行结果和 SQL 语义一致。

---

## 八、技巧精解

本章挑两个最硬核的技巧单独拆透:**① `VdbeOp` 的五操作数 + `p4` 多类型 union 设计**;② **临时寄存器池(`aTempReg` 栈 + `iRangeReg` 块)**。

### 技巧一:五操作数 + p4 多类型 union——一条 opcode 表达一切

#### 它解决什么问题

opcode 要表达的操作五花八门:`Integer`(写一个常量)、`OpenRead`(要知道根页号、库号、列数)、`Function`(要知道哪个函数、参数寄存器范围)、`IdxInsert`(要知道索引的比较规则 `KeyInfo*`)、`ResultRow`(要知道结果寄存器范围)……

如果每种 opcode 用一个**专用结构体**,`aOp[]` 就要存"操作码 + 各种结构体的 union",既浪费空间(union 取最大),又难统一处理(VDBE 执行时要 switch 出几十种结构体)。

#### 朴素设计会怎样

朴素的两种选择都有问题:

- **方案 A:每个 opcode 一个专用结构体**——`struct OpOpenRead { int iCur; int root; int db; int nCol; }`、`struct OpFunction { FuncDef *fn; int regBase; int nArg; int regOut; }`……VDBE 执行时按 opcode switch 出对应结构体。**问题**:几十种结构体大小不一,`aOp[]` 没法紧凑排布(union 取最大,VDBE 顺序扫时 cache 不友好);每种 opcode 加一个字段要改结构体,扩展难。
- **方案 B:全部用 int 数组**——`opcode + p1 + p2 + p3` 四个 int,要附加数据就再开一个旁路数组。**问题**:附加数据(字符串、`FuncDef*`)无法内联,要么开旁路数组(管理复杂),要么塞进 int(指针强转 int,可移植性差)。

#### SQLite 的巧妙手段:固定五操作数 + p4 用 union 收尾

SQLite 的设计:**前三个操作数 `p1/p2/p3` 永远是 int(定长、对齐好),第四个 `p4` 是 union(按需承载多类型),第五个 `p5` 用 u16 塞标志位**。

```c
struct VdbeOp {
  u8 opcode;           // ← 定长 1 字节
  signed char p4type;  // ← 定长 1 字节,标记 p4 是哪种类型
  u16 p5;              // ← 定长 2 字节
  int p1, p2, p3;      // ← 定长 12 字节
  union p4union { ... } p4;  // ← 8 字节(指针大小)
};
```

这套设计的妙处:

1. **绝大多数 opcode 只用 p1/p2/p3 就够了**——`Integer 10, 1`(p1=10, p2=1)、`Column 0, 1, 2`(p1/p2/p3)都是只用前三个,`p4` 留空(`p4type=P4_NOTUSED`)。**绝大多数 opcode 不付 p4 的成本**(union 不主动构造,只在需要时塞)。
2. **少数需要附加数据的 opcode,用 p4 union 表达**——`OpenRead` 的列数用 `P4_INT32`(`p4.i = nCol`)、`Function` 的函数定义用 `P4_FUNCDEF`(`p4.pFunc = pFunc`)、`IdxInsert` 的比较规则用 `P4_KEYINFO`(`p4.pKeyInfo = pKeyInfo`)。**union 让 p4 这 8 字节能承载 10 多种类型,按需选用**。
3. **`p4type` 用负数标记 ownership**——看 `vdbe.h`:`P4_COLLSEQ = -2`、`P4_INT32 = -3`、`P4_FUNCDEF = -8`、`P4_KEYINFO = -9`……注意 `P4_FREE_IF_LE = -7` 是分界线——**编号 ≤ -7 的 P4 类型 own 资源(析构时要 free),编号 > -7 的不 own(只是引用)**。比如 `P4_KEYINFO`(-9,own)析构时要 free 掉 `KeyInfo*`;`P4_COLLSEQ`(-2,不 own)只是引用,析构时不 free。**一个负数编号同时表达"类型 + 所有权",这是极致紧凑的设计**。
4. **内存对齐精心调过**——`opcode(u8) + p4type(s8) + p5(u16)` 正好 4 字节,接 `p1(int)` 4 字节对齐;`p1/p2/p3` 三个 int 12 字节;`p4` union 8 字节(64 位系统)。**整条 opcode 在 64 位系统上约 24-32 字节**,`aOp[]` 顺序扫时 cache 友好。

#### 反面对比:如果不用 union

如果 `p4` 拆成"int p4i + char *p4z + void *p4p + FuncDef *p4func + KeyInfo *p4key + ...",一条 opcode 会膨胀到 60+ 字节(每个可能类型都占一个字段),`aOp[]` 内存占用翻倍,VDBE 顺序扫 cache miss 频发。**union 把这些互斥的字段压成一个 8 字节槽,既表达力强又紧凑**——这是 C 的经典技巧,SQLite 把它用到了极致。

> **钉死这件事**:`VdbeOp` 的"五操作数 + p4 多类型 union"是 SQLite opcode 兼顾表达力和紧凑性的核心设计。**绝大多数 opcode 只用 p1/p2/p3,少数需要附加数据的用 p4 union + p4type 按需承载,ownership 用 p4type 的正负编号隐式表达**。这个设计让 `aOp[]` 既小(每条 24-32 字节)又灵活(能表达 10 多种附加数据类型)。

### 技巧二:临时寄存器池——单寄存器栈 + 连续块回收

#### 它解决什么问题

codegen 算表达式时要大量临时寄存器(`a+b` 要两个、`f(a,b,c)` 要三个……)。如果每次都从 `nMem` 水位线分配新号,寄存器号会爆炸——一条复杂 SELECT 的 codegen 可能要几百个临时寄存器,`Vdbe.aMem[]` 会很大(prepared statement 占的内存就大)。

#### 朴素设计会怎样

朴素的两种选择:

- **方案 A:不回收,全部从水位线分配**——简单,但 `nMem` 爆炸,`aMem[]` 几百个槽,内存浪费。prepared statement 缓存住这个大数组,长期占内存。
- **方案 B:用链表/动态数组做寄存器池**——每次 malloc/free 一个寄存器号槽。**问题**:malloc 慢,codegen 高频分配/回收会让 codegen 被内存分配拖累;而且 codegen 是单线程的,链表的保护开销纯属浪费。

#### SQLite 的巧妙手段:定长栈 + 单块回收

SQLite 的设计(`src/expr.c:7610-7657`):**单寄存器用定长 8 元素栈(`aTempReg[8]`),连续寄存器块用"单块回收"(`iRangeReg/nRangeReg`)**。

- **`aTempReg[8]` 定长栈**:
  - 分配:`nTempReg==0` 就 `++nMem`,否则弹栈 `aTempReg[--nTempReg]`。
  - 回收:压栈 `aTempReg[nTempReg++] = iReg`(满了就不压,直接丢)。
  - **妙处**:无 malloc(定长数组嵌在 `Parse` 里);codegen 同时活跃的临时寄存器极少超过 8 个(表达式嵌套深度有限),栈基本够用;满了就丢也无妨(丢掉的寄存器号只是没法复用,占 `aMem[]` 一个空槽,微小浪费)。

- **`iRangeReg/nRangeReg` 单块回收**:
  - 分配:要 N 个连续寄存器,先看回收块(`iRangeReg` 起的 `nRangeReg` 个)够不够,够就从块尾切 N 个;不够就 `nMem += N`。
  - 回收:整块压回 `iRangeReg/nRangeReg`(只保留最近归还的一整块)。
  - **妙处**:连续寄存器需求(取一行所有列)频繁出现,单块回收让"一行 N 列"的需求反复复用同一片寄存器,**N 个寄存器号在整条 codegen 中可能只占一次水位线**。

#### 反面对比:如果朴素地用一个寄存器池

假设用一个动态数组做寄存器池(每次 malloc/push/pop):

```c
// 朴素方案( SQLite 没这么干):
int *regPool = malloc(...);   // ← malloc 一次
push(regPool, iReg);          // ← 每次回收
iReg = pop(regPool);          // ← 每次分配
```

这个方案的代价:① malloc 一次(`Parse` 生命周期里至少一次);② 每次 push/pop 是函数调用 + 数组边界检查;③ 动态数组满了要 realloc。SQLite 的定长栈:**零 malloc(嵌在 `Parse` 里)、push/pop 是 `aTempReg[--nTempReg]` / `aTempReg[nTempReg++]=iReg` 两条数组写、永远不会 realloc**。在 codegen 这种高频路径上,这种"用定长数组换零分配"的取舍是嵌入式数据库的典型偏好。

而连续块的"单块回收"更是妙——它假设"连续寄存器需求高度重复"(确实如此,每取一行就要一片连续寄存器),所以只保留最近归还的一块,**用最小状态(`iRangeReg + nRangeReg` 两个 int)实现高效复用**。如果用更复杂的"空闲块链表"(像内存分配器的 free list),状态复杂、查找慢,而 SQLite 这种单块方案对实际 workload 命中率极高。

> **钉死这件事**:SQLite 的临时寄存器分配用"定长 8 元素栈(单寄存器)+ 单块回收(连续寄存器)"两个极简机制,**零 malloc、常数时间分配/回收、状态最小**。这是嵌入式场景"用定长/单状态换零分配"取舍的典范——不追求理论上的最优(寄存器分配是 NP-hard 的图着色问题),而是用对实际 workload 命中率最高的简单方案。

---

## 九、章末小结

### 回扣主线

本章讲的是"编译与执行"那一面的**编译下半场**:AST → opcode。它把上一章 Parser 产出的 AST,翻译成下一章 VDBE 要执行的 opcode 流。**code generator 是连接"语法分析"和"虚拟机执行"的桥**——它在 `Parse.pVdbe->aOp[]` 这个数组里,一行行写下 SQL 的"执行剧本"。

回到全书二分法:**编译与执行(SQL → opcode → VDBE 执行) vs 存储与事务(B-tree/Pager/WAL)**。本章是"编译与执行"那一面的核心一环,服务的是"SQL 怎么变成执行结果"这条主线。下一章 P2-05 接力,讲 VDBE 怎么执行本章产出的 opcode。

### 五个为什么

1. **为什么 SQLite 要先把 AST 编译成字节码,而不是直接解释 AST?**——四个收益:① prepared statement 复用(一次编译多次执行);② 扁平指令流便于优化器分析(选索引、谓词下推);③ 统一执行模型(所有 SQL 最后都是 opcode,一个 VDBE 执行);④ 编译和执行解耦,各自演进。这套思想《Lua》那本讲透了,SQLite 是同一个根。

2. **为什么 opcode 是扁平指令流,不是 AST 那样的树?**——扁平流好做 peephole 优化(优化器在 codegen 阶段就把决策烧死进 opcode)、好做跳转(label + resolveP2Values 回填)、VDBE 顺序扫 cache 友好。树状 AST 优化要小心父子关系和作用域,扁平流优化像汇编 peephole,直接。

3. **为什么 `VdbeOp` 用五操作数 + p4 union,而不是每个 opcode 一个专用结构体?**——五操作数(三个 int + 一个 union + 一个 u16)兼顾表达力和紧凑性。绝大多数 opcode 只用 p1/p2/p3,少数需要附加数据的用 p4 union + p4type 按需承载,ownership 用 p4type 正负编号隐式表达。这条 opcode 在 64 位系统上约 24-32 字节,cache 友好。

4. **为什么寄存器分配用"水位线 + 临时池",而不是图着色?**——SQLite 是单查询单 VDBE,寄存器分配不需要像编译器那样追求全局最优(图着色 NP-hard)。水位线保证长期寄存器号唯一,临时池(定长栈 + 单块回收)让短期寄存器高效复用、零 malloc。**用对实际 workload 命中率最高的简单方案,不追求理论最优**——嵌入式取舍。

5. **为什么 DDL(建表建索引)也走 VDBE,而不是直接改 schema 文件?**——统一执行模型:DDL 被编译成"往 `sqlite_master` 系统表插一行"的 opcode,跟数据 `Insert` 走同一套 VDBE。这让 DDL 能在事务里、能 rollback,执行逻辑高度统一。**"一切皆 opcode"是 SQLite 设计的核心美学**。

### 想继续深入往哪钻

- **想看 codegen 全貌**:读 `src/select.c` 的 `sqlite3Select()`(SELECT codegen 主函数,~9000 行重头)、`src/insert.c` 的 `sqlite3Insert()`、`src/where.c` 的 `sqlite3WhereBegin()`(WHERE codegen + 优化器)。
- **想看 opcode 种类**:在 `sqlite3` CLI 跑 `EXPLAIN SELECT/INSERT/UPDATE/DELETE ...`,看不同语句编译出的 opcode 流;或读 SQLite 官方文档 "Virtual Machine That Executes SQL"(列出所有 opcode)。
- **想理解优化器怎么选索引**:本书 P3-10(索引与查询)会拆 `sqlite3WhereBegin` 里头的索引选择逻辑;本章只讲了它产出 opcode,索引决策的细节留到那章。
- **想理解寄存器/游标的执行**:本书 P2-05(VDBE 虚拟机)会拆 VDBE 执行期怎么访问 `aMem[]` / `apCsr[]`;本章只讲 codegen 怎么分配它们。

### 引出下一章

我们搞清楚了 code generator 怎么把 AST 翻译成 opcode 流。但 opcode 流只是"剧本",**谁演这个剧本**?VDBE 虚拟机。下一章 P2-05,我们拆 VDBE 怎么逐条执行 opcode——那个巨大的 switch-case 循环长什么样、寄存器和游标在执行期怎么动、`OpenRead` / `Column` / `ResultRow` 这些 opcode 各自的 case 怎么写。这是 SQLite 灵魂的核心,承《Lua》虚拟机章节。

> **下一章**:[P2-05 · VDBE 虚拟机:执行字节码](P2-05-VDBE虚拟机-执行字节码.md)
