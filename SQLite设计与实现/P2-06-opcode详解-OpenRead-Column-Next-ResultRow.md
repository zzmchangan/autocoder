# 第 2 篇 · 第 6 章 · opcode 详解:OpenRead / Column / Next / ResultRow

> **核心问题**:上一章我们把 `sqlite3VdbeExec` 那个 9456 行的巨型函数拆透了——`for(pOp=&aOp[p->pc]; 1; pOp++)` 这个主循环,`switch(pOp->opcode)` 把控制权交给每个 `case OP_xxx`。但循环里那一百多个 `case` **各自到底在干什么**?一条真实的 `SELECT` 会被编译成什么样的 opcode 串?那个神奇的 `EXPLAIN` 命令输出的 `addr / opcode / p1 / p2 / p3 / p4 / p5 / comment` 八列表格怎么读?更关键的:`OpenRead`、`Column`、`Next`、`ResultRow` 这四个出现频率最高的 opcode,各自的 case 体是怎么写的、四个操作数怎么用、它们之间怎么配合把"B-tree 里的一行数据"变成"返回给应用的一行结果"?

> **读完本章你会明白**:
> 1. **opcode 的操作数模型**——一条 VDBE 指令有 `p1/p2/p3/p4` 四个操作数(外加 `p5` 标志位),其中 `p1/p2/p3` 是定长 `int`(绝大多数是寄存器号/页号/常量/跳转目标),`p4` 是个 tagged union(`P4_INT32`/`P4_KEYINFO`/`P4_MEM`/`P4_FUNCDEF`/...),它怎么让一条指令既能装一个整数、又能装一个 `KeyInfo` 结构、又能装一个 SQL 函数指针——这是"一条 opcode 携带复杂信息"的关键。
> 2. **怎么读 EXPLAIN 的输出**——`EXPLAIN SELECT ...` 把 prepared statement 的 opcode 流原样打出来,八列里每一列是什么意思、`p2` 在跳转 opcode 里是目标地址、在 `ResultRow` 里是结果列数、在 `Column` 里是列号——同一个字段在不同 opcode 里语义不同,本章会逐行讲一张真实 EXPLAIN 表。
> 3. **`OpenRead` / `Column` / `Next` / `ResultRow` 四个核心 opcode 的真实 case 体**——`OpenRead` 怎么把"游标号 p1"绑到一个 B-tree 根页 p2、怎么用 `sqlite3BtreeCursor` 在 VDBE 游标里塞一个 `BtCursor`;`Column` 怎么从一条二进制 record 里解码出第 N 列写进寄存器、为什么它有 300 行(行缓存 + 增量解析 + 零拷贝三件套);`Next` 怎么推进游标、怎么用 `p2` 跳回循环顶;`ResultRow` 怎么把一段寄存器范围标记成"结果行"、怎么靠 `p->pc` 记住下一条指令实现"一次 step 返一行"。
> 4. **opcode 怎么分类**——按功能分成游标类、寄存器类、取值类、结果类、控制流类、聚合类六大类,以及写路径(INSERT/UPDATE)用到的 `OpenWrite`/`NewRowid`/`MakeRecord`/`Insert` 这一组,你会看到一条 INSERT 的完整 EXPLAIN。

> **逃生阀(这章 opcode 很多,一读觉得晕,先记住这五件事)**:
> ① 一条 opcode 就是 `struct VdbeOp`,有 `p1/p2/p3`(三个 `int`) + `p4`(union,按 `p4type` 解释) + `p5`(标志位),`p2` 在跳转类 opcode 里是目标地址;② `OpenRead` 建 cursor 连 B-tree,`Column` 从 cursor 当前行的 record 解码第 N 列,`Next` 推进 cursor,`ResultRow` 把寄存器段标记成结果行——这四个串起来就是一条 SELECT 的骨架;③ EXPLAIN 的输出就是 opcode 流本身,`addr` 是指令地址、`p2` 在 `Rewind`/`Next`/`Goto` 里是跳转目标;④ opcode 总共约 192 个(对照 Lua 的 47 条,SQLite 因 SQL 复杂度指令更多),但常用的就二三十个,不用背全部;⑤ 写路径多了 `OpenWrite`(带写标志)、`MakeRecord`(把寄存器编码成 record)、`NewRowid`(算下一个 rowid)、`Insert`(写进 B-tree)这组。记住这五点,后面每一节都是在展开它们。

---

## 〇、一句话点破

> **VDBE 的 opcode 是"一个原子动作 + 至多四个操作数"的极简执行模型:每条 opcode 只干一件事(打开一个游标、取一列、推进游标、返回一行),操作数用寄存器号/页号/常量/跳转目标就够了,装不下的复杂信息(索引键信息、SQL 函数指针、字符串)塞进 `p4` 这个 tagged union。`EXPLAIN` 把这串 opcode 原样打出来,读懂它就读懂了 SQLite 怎么执行你的 SQL。**

这是结论,不是理由。本章倒过来拆:先讲 opcode 的操作数模型(贴 `struct VdbeOp` 和 `P4_xxx` 类型表),再讲怎么读 EXPLAIN(贴一张真实的 `EXPLAIN SELECT` 输出逐行讲),然后把 `OpenRead`/`Column`/`Next`/`ResultRow` 四个最核心 opcode 的 case 体逐段拆透,接着给一个 opcode 分类总表,最后用一个 INSERT 的 EXPLAIN 对照讲写路径 opcode,并以"Column 的 record 变长 header 解析"和"Next 的循环回跳 + 性能埋点"两个最硬核的技巧收尾。

---

## 一、从主循环接过来:每个 `case` 就是一个 opcode 的实现

上一章(P2-05)我们把 VDBE 的心脏拆透了:

```c
/* vdbe.c:881 */
for(pOp=&aOp[p->pc]; 1; pOp++){        /* fetch + 自增 */
  ...
  switch( pOp->opcode ){                /* decode */
    case OP_Goto:      ...
    case OP_Integer:   ...
    case OP_OpenRead:  ...
    case OP_Column:    ...
    case OP_Next:      ...
    case OP_ResultRow: ...
    case OP_Halt:      ...
    ...
  }
}
```

那个 `switch` 里有**一百多个 `case`**,每个 `case` 对应一个 opcode 的实现。上一章我们只挑了几个最核心的(`OpenRead`/`Column`/`SeekRowid`/`Next`/`ResultRow`/`Halt`/`Goto`/`Gosub`)在讲游标/寄存器/控制流时顺带拆了,但那是"为了讲 VDBE 骨架而拆",**没有把每个 opcode 讲透**。本章反过来——**以 opcode 为主线**,把出现频率最高的那十几个 opcode,从操作数语义、case 体实现、为什么这么设计三个层面逐一拆透。

> **钉死这件事(承接 P2-05)**:上一章讲的是 VDBE 这个"虚拟机"的骨架——主循环怎么 fetch-decode-execute、`Mem` 寄存器怎么动态类型、`VdbeCursor` 游标怎么连 B-tree。本章不再讲骨架,**只讲循环里那一个个 `case` 具体在干什么**。如果你忘了 VDBE 主循环怎么跑,先回 P2-05 复习那个 `for(pOp=&aOp[pc]; 1; pOp++)` 再往下读。两章的关系是:P2-05 讲"虚拟机怎么执行 opcode",P2-06 讲"opcode 本身长什么样、各自干什么"。

在拆单个 opcode 之前,先看清"一条 opcode"这个数据结构长什么样——这是理解所有 case 体的前提。

---

## 二、一条 opcode 长什么样:`struct VdbeOp` 和它的四个操作数

### `VdbeOp` 结构:opcode + p1/p2/p3 + p4 union + p5

VDBE 的"指令"是 `struct VdbeOp`([`vdbe.h:55`](../sqlite/src/vdbe.h#L55)),上一章贴过,这里重新看清它的每一个字段,因为本章所有 opcode 都在摆弄这几个字段:

```c
struct VdbeOp {
  u8 opcode;          /* 操作码,最多 256 种(实际用约 192 种) */
  signed char p4type; /* p4 的类型,取 P4_xxx 常量之一 */
  u16 p5;             /* 第五参数,16 位无符号,常作标志位 */
  int p1;             /* 第一操作数 */
  int p2;             /* 第二操作数(跳转类 opcode 时是目标地址) */
  int p3;             /* 第三操作数 */
  union p4union {     /* 第四参数,tagged union */
    int i;                 /* P4_INT32:32 位整数 */
    void *p;               /* 通用指针 */
    char *z;               /* P4_STATIC/P4_DYNAMIC:字符串 */
    i64 *pI64;             /* P4_INT64:64 位整数 */
    double *pReal;         /* P4_REAL:浮点数 */
    FuncDef *pFunc;        /* P4_FUNCDEF:SQL 函数指针 */
    sqlite3_context *pCtx; /* P4_FUNCCTX:函数上下文 */
    CollSeq *pColl;        /* P4_COLLSEQ:排序规则 */
    Mem *pMem;             /* P4_MEM:一个完整的值 */
    VTable *pVtab;         /* P4_VTAB:虚拟表 */
    KeyInfo *pKeyInfo;     /* P4_KEYINFO:索引键信息 */
    u32 *ai;               /* P4_INTARRAY:整数数组 */
    SubProgram *pProgram;  /* P4_SUBPROGRAM:子程序(触发器用) */
    Table *pTab;           /* P4_TABLE:表结构 */
    SubrtnSig *pSubrtnSig; /* P4_SUBRTNSIG:子程序签名 */
    Index *pIdx;           /* P4_INDEX:索引结构 */
  } p4;
#ifdef SQLITE_ENABLE_EXPLAIN_COMMENTS
  char *zComment;          /* EXPLAIN 注释(本章那些 ; users 之类) */
#endif
#if defined(SQLITE_ENABLE_STMT_SCANSTATUS) || defined(VDBE_PROFILE)
  u64 nExec;               /* 这条 opcode 被执行了多少次(profiler) */
  u64 nCycle;              /* 累计花了多少 CPU 周期 */
#endif
};
```

注意几件事,这是本章所有 opcode 拆解的基础:

- **`p1/p2/p3` 是定长 `int`**——三个 32 位整数操作数,够表达 90% 的 opcode 语义。绝大多数 opcode 只用到其中两三个。比如 `OP_Column p1 p2 p3` 是"游标 p1 的第 p2 列 → 寄存器 p3",三个 `int` 全用上,语义完整。
- **`p2` 的双重身份**——在非跳转 opcode 里它是普通操作数(列号、寄存器号、结果列数),**在跳转 opcode 里它是目标指令地址**(`OP_Goto`/`OP_Rewind`/`OP_Next`/`OP_If`/... 都是 `jump to p2`)。这是 VDBE 操作数复用的关键——同一个字段在不同 opcode 里语义不同,靠 `OPFLG_JUMP` 属性位区分(下面 OPFLG 会讲)。
- **`p4` 是 tagged union**——当三个 `int` 装不下时(比如要带一个 `KeyInfo` 结构、一个 SQL 函数指针、一个字符串字面量、一个 64 位整数),就用 `p4` 加 `p4type` 标签来装。`p4type` 决定怎么解释 `p4` 这个 union,这是 C 里实现"动态类型参数"的标准手法。
- **`p5` 当标志位**——16 位无符号,塞位标志。比如 `OP_OpenWrite` 的 `p5` 里可能塞了 `OPFLAG_FORDELETE`(这个游标用于删除)、`OPFLAG_P2ISREG`(p2 是寄存器号不是页号);`OP_Insert` 的 `p5` 塞 `OPFLAG_NCHANGE`(累加变更计数)、`OPFLAG_LASTROWID`(记录 last rowid)、`OPFLAG_APPEND`(提示是追加);`OP_Next` 的 `p5` 塞性能统计 counter 的下标。
- **`zComment` 只在 `SQLITE_ENABLE_EXPLAIN_COMMENTS` 编译时存在**——它就是你在 EXPLAIN 输出里看到的 `; users`、`r[2]= cursor 0 column 1` 那些人类可读注释,由 code generator 在编译时填,生产构建可以裁掉省内存。
- **`nExec`/`nCycle` 只在 `SQLITE_ENABLE_STMT_SCANSTATUS` 或 `VDBE_PROFILE` 编译时存在**——它们让 `sqlite3_stmt_scanstatus()` 能告诉你"哪条 opcode 跑了几次、花了多少 CPU 周期",这是 SQLite 自带的 profiler。

> **钉死这件事**:`struct VdbeOp` 是"opcode + 三个定长 int 操作数 + 一个 tagged union + 一组标志位"的极简指令格式。一条指令典型 16 字节(opcode 1 + p4type 1 + p5 2 + p1/p2/p3 各 4 = 16,不含 p4 union),整个 prepared statement 的 opcode 流就是这玩意儿排成数组 `aOp[]`,被主循环逐条消费。**理解了这四个操作数怎么用,你就理解了所有 opcode 的语义框架。**

### p4 的十八般武艺:`P4_xxx` 类型表

`p4` 这个 union 是 VDBE 操作数模型的精髓——它让一条 opcode 既能带一个 64 位整数(`OP_Int64`/`OP_Real`),又能带一个索引键信息(`OP_OpenRead` 打开索引游标时带 `KeyInfo`),又能带一个 SQL 函数(`OP_AggStep` 带聚合函数的 `FuncDef`),又能带一个字符串字面量(`OP_String8`)。看 `vdbe.h:126` 的 `P4_xxx` 全表:

```c
/* vdbe.h:126 */
#define P4_NOTUSED      0   /* 不用 p4 */
#define P4_TRANSIENT    0   /* p4 指向临时字符串(需拷贝) */
#define P4_STATIC     (-1)  /* 指向静态字符串(不能释放) */
#define P4_COLLSEQ    (-2)  /* 指向 CollSeq 排序规则结构 */
#define P4_INT32      (-3)  /* 32 位有符号整数 */
#define P4_SUBPROGRAM (-4)  /* 指向 SubProgram(触发器子程序) */
#define P4_TABLE      (-5)  /* 指向 Table 结构 */
#define P4_INDEX      (-6)  /* 指向 Index 结构 */
#define P4_DYNAMIC    (-7)  /* 指向 sqliteMalloc() 分配的内存(用完要释放) */
#define P4_FUNCDEF    (-8)  /* 指向 FuncDef(SQL 函数定义) */
#define P4_KEYINFO    (-9)  /* 指向 KeyInfo(索引键信息) */
#define P4_EXPR       (-10) /* 指向 Expr 树(游标提示) */
#define P4_MEM        (-11) /* 指向 Mem(一个完整值,如默认值) */
#define P4_VTAB       (-12) /* 指向 sqlite3_vtab(虚拟表) */
#define P4_REAL       (-13) /* 64 位浮点数 */
#define P4_INT64      (-14) /* 64 位有符号整数 */
#define P4_INTARRAY   (-15) /* 32 位整数数组 */
#define P4_FUNCCTX    (-16) /* 指向 sqlite3_context(函数执行上下文) */
#define P4_TABLEREF   (-17) /* 像 P4_TABLE 但引用计数 */
#define P4_SUBRTNSIG  (-18) /* 指向 SubrtnSig(子程序签名) */
```

这套 `P4_xxx` 是"一条 opcode 携带复杂信息"的全部手段。我们会在具体 opcode 里反复遇到它们,先记住几个高频的:

- **`P4_INT32`**——最常见的 p4 类型,`OP_OpenRead` 打开表游标时,p4 装这张表的列数(`nField`);`OP_Affinity` 装 affinity 字符串指针(其实走 P4_STATIC/P4_DYNAMIC)。
- **`P4_KEYINFO`**——`OP_OpenRead` 打开**索引**游标时带这个,装索引的键信息(每列的排序规则、是否有 collation),决定 B-tree 怎么排序键。
- **`P4_FUNCDEF` / `P4_FUNCCTX`**——`OP_AggStep`(聚合步进)带这个,装聚合函数的指针;第一次执行后自改成 `P4_FUNCCTX`(带执行上下文)。
- **`P4_MEM`**——`OP_Column` 在访问一行里"不存在的列"(表新增列但旧行没数据)时,带这个列的默认值;也用于一些边界场景。
- **`P4_INT64` / `P4_REAL`**——`OP_Int64`/`OP_Real` 用,装 32 位装不下的整数和浮点数。
- **`P4_TABLE`**——`OP_Insert`/`OP_Delete` 带,装目标 `Table` 结构,用于触发 update hook。
- **`P4_COLLSEQ`**——比较类 opcode 带,装排序规则,决定字符串怎么比大小。

> **不这样会怎样(如果不用 tagged union)**:如果不让 p4 是 union,VDBE 要么把每个 opcode 写成不同结构体(`OpRead`/`OpColumn`/`OpAgg` 各一种,主循环 switch 里处理不同结构体——又乱又难维护),要么把 `Op` 结构撑得很大(把 `KeyInfo*`/`FuncDef*`/`i64`/`double` 全塞进每个 opcode——一条指令浪费几十字节,百万条指令就是几十 MB)。tagged union 让所有 opcode 共用同一种 `VdbeOp` 结构(16 字节起步),只在需要时才填 `p4`,既统一又紧凑。**这是 C 里实现"统一指令格式 + 灵活携带复杂参数"的标准手法,Lua 的 `TValue`、CPython 的 `PyObject` 都是同一个套路。**

### OPFLG 属性位图:opcode 的"元数据"

为了让主循环和 EXPLAIN 能快速判断一条 opcode 的"类型属性"(是不是跳转、读哪个寄存器、写哪个寄存器),SQLite 在构建时由 `mkopcodeh.tcl` 脚本扫描 `vdbe.c` 的 `case` 注释,生成一张 `sqlite3OpcodeProperty[]` 数组(构建产物 `opcodes.h`,在仓库根目录而非 `src/`;OPFLG 定义见 [`opcodes.h:198`](../sqlite/opcodes.h#L198))。每个 opcode 对应一个 `u8` 属性位图:

```c
/* opcodes.h:198 */
#define OPFLG_JUMP        0x01  /* jump:  P2 holds jmp target(P2 是跳转目标) */
#define OPFLG_IN1         0x02  /* in1:   P1 is an input(P1 作输入寄存器) */
#define OPFLG_IN2         0x04  /* in2:   P2 is an input(P2 作输入寄存器) */
#define OPFLG_IN3         0x08  /* in3:   P3 is an input(P3 作输入寄存器) */
#define OPFLG_OUT2        0x10  /* out2:  P2 is an output(P2 作输出寄存器) */
#define OPFLG_OUT3        0x20  /* out3:  P3 is an output(P3 作输出寄存器) */
#define OPFLG_NCYCLE      0x40  /* ncycle:Cycles count against P1(计入性能统计) */
#define OPFLG_JUMP0       0x80  /* jump0: P2 might be zero(P2 可能为 0) */
```

这些属性怎么用?看主循环里的 debug 检查([`vdbe.c:1009`](../sqlite/src/vdbe.c#L1009)):

```c
#ifdef SQLITE_DEBUG
  {
    u8 opProperty = sqlite3OpcodeProperty[pOp->opcode];
    if( (opProperty & OPFLG_IN1)!=0 ){
      assert( pOp->p1>0 );
      assert( memIsValid(&aMem[pOp->p1]) );   /* 读 p1,得是有效寄存器 */
    }
    if( (opProperty & OPFLG_IN2)!=0 ){ ... }
    if( (opProperty & OPFLG_IN3)!=0 ){ ... }
    if( (opProperty & OPFLG_OUT2)!=0 ){ memAboutToChange(p, &aMem[pOp->p2]); }
    if( (opProperty & OPFLG_OUT3)!=0 ){ memAboutToChange(p, &aMem[pOp->p3]); }
  }
#endif
```

debug 模式下,主循环每条 opcode 都查属性位图,确认"这条 opcode 声称读 p1,那 p1 寄存器必须有效""声称写 p2,那要通知 p2 的浅拷贝依赖失效"。生产构建里这些 assert 没了,但属性位图还在——它还有别的用途,比如 `OPFLG_NCYCLE` 标记要不要把这条 opcode 计入 `nCycle` 性能统计,`OPFLG_JUMP` 让 EXPLAIN 知道 p2 是跳转目标。

更重要的是(上一章技巧精解讲过):**这张属性位图本身,加上 opcode 值是连续小整数(0~约 192),让编译器能放心地把那个巨型 switch 优化成 jump table**——一个地址数组,索引是 opcode,值是 case 入口地址。这是 SQLite 心脏能快起来的根基之一。

> **钉死这件事(承接 P2-05 技巧精解)**:`OPFLG_xxx` 这 8 个位标志,是每条 opcode 的"自我描述"——它声明自己读哪些寄存器、写哪些寄存器、是不是跳转、要不要计入性能统计。debug 模式下主循环用它们做 sanity check(声称读 p1 就得是有效寄存器),生产模式它们还服务于性能统计和编译器优化。**理解了 OPFLG,你就理解了为什么 VDBE 的操作数模型能既统一又安全——每条 opcode 都带着自己的"类型契约"。**

---

## 三、怎么读 EXPLAIN:把 opcode 流原样打出来

理解了操作数模型,现在可以读 EXPLAIN 了。EXPLAIN 是 SQLite 最强的调试工具之一——它**不做任何执行**,只是把 code generator 产出的 opcode 流原样打成一个表格。读懂这张表,就读懂了 SQLite 怎么执行你的 SQL。

### EXPLAIN 的输出格式:八列

`EXPLAIN SELECT ...` 的输出是这样一个八列表(列名是 SQLite 固定的):

| 列名 | 含义 |
|------|------|
| `addr` | opcode 在 `aOp[]` 数组里的下标(指令地址) |
| `opcode` | 操作码名(如 `OpenRead`、`Column`) |
| `p1` | 第一操作数 |
| `p2` | 第二操作数(跳转类 opcode 时是目标 `addr`) |
| `p3` | 第三操作数 |
| `p4` | 第四操作数(按 `p4type` 解释:整数/字符串/KeyInfo 名/...) |
| `p5` | 标志位(常以十进制显示) |
| `comment` | 人类可读注释(需 `SQLITE_ENABLE_EXPLAIN_COMMENTS` 编译) |

注意 `addr` 是 0 起的整数,它就是 `pOp - aOp`。跳转 opcode 的 `p2` 就是目标 `addr`——比如 `Rewind 0 8 0` 意思是"游标 0 到第一条;如果表空跳到 addr=8",这个 8 就是 `aOp[8]` 的下标。

### 一条真实 SELECT 的 EXPLAIN:逐行讲

下面是一张**真实的** EXPLAIN 输出,来自 SQLite 3.54.0(我用 `EXPLAIN SELECT name,age FROM users WHERE id>=2` 在一张 `users(id INTEGER PRIMARY KEY, name TEXT, age INTEGER)` 表上跑出来的,表里有 3 行数据):

```
addr  opcode       p1  p2  p3  p4    p5  comment
----  -----------  --  --  --  ----  --  -----------------------------
0     Init         0   8   0         0   Start at 8
1     OpenRead     0   2   0   3     0   root=2 iDb=0; users
2     SeekGE       0   7   1         0   key=r[1]; pk
3     Column       0   1   2         0   r[2]= cursor 0 column 1
4     Column       0   2   3         0   r[3]= cursor 0 column 2
5     ResultRow    2   2   0         0   output=r[2..3]
6     Next         0   3   0         0
7     Halt         0   0   0         0
8     Transaction  0   0   1   0     1   usesStmtJournal=0
9     Integer      2   1   0         0   r[1]=2
10    Goto         0   1   0         0
```

逐行讲(这一段是本章的核心,把每条 opcode 的操作数语义讲透):

**addr=0 `Init 0 8 0`** ——程序入口。`Init` 干两件事:① 设 `p2=8` 为"真正的执行起点"(它自己是个 jump,跳到 addr=8,跳过下面那段"初始化代码",让初始化代码只在程序末尾被走到一次);② 记录一些元信息。这里的 8 就是 `Transaction` 那条。**`Init` 的 p2 永远指向"主程序体的第一条 opcode"**,这是 VDBE 的固定约定——主程序体夹在 `Init` 和 `Halt` 之间,初始化代码(Transaction/Integer/Goto 那组)排在末尾,由 `Init` 跳过去执行一次,执行完 `Goto` 跳回主程序体。

**addr=1 `OpenRead 0 2 0 3`** ——打开 users 表的读游标。`p1=0` 是游标号(后续 `Column`/`Next` 通过 0 找到它);`p2=2` 是 users 表 B-tree 的根页号(在 schema 里查到的);`p3=0` 是数据库序号(0=main 主库,1=temp);`p4=3`(P4_INT32)是这张表的列数(3 列);`p5=0` 无标志。注释 `root=2 iDb=0; users` 是 code generator 填的人类可读说明。

**addr=2 `SeekGE 0 7 1`** ——`WHERE id>=2` 的定位。`p1=0` 游标 0;`p2=7` 是"没找到时的跳转目标"(这里指向 `Halt`,意思是范围扫完就停);`p3=1` 是装查找键的寄存器号(寄存器 1 在 addr=9 被填成 2);注释 `key=r[1]; pk` 说"用寄存器 1 的值作主键查找键"。`SeekGE` 把游标定位到第一个 `rowid >= 2` 的位置。

**addr=3 `Column 0 1 2`** ——取第 1 列。`p1=0` 游标 0;`p2=1` 是列号(注意 0 是 rowid/INTEGER PRIMARY KEY,1 是 name,2 是 age);`p3=2` 是目标寄存器(把 name 写进寄存器 2)。注释 `r[2]= cursor 0 column 1`。

**addr=4 `Column 0 2 3`** ——取第 2 列(age),写进寄存器 3。

**addr=5 `ResultRow 2 2`** ——把寄存器 2..3(从 p1=2 起 p2=2 个寄存器)作为一行结果返回。`p1=2` 是起始寄存器,`p2=2` 是结果列数(不是跳转目标!注意 `ResultRow` 不是跳转 opcode)。注释 `output=r[2..3]`。

**addr=6 `Next 0 3`** ——推进游标。`p1=0` 游标 0;`p2=3` 是"还有数据时跳回的目标"(这里是 addr=3,就是循环体顶——`Column`/`ResultRow` 那组);`p3=0` 是 btree 提示。**`Next` 是循环的发动机**:游标推进成功(还有数据),跳回 addr=3 重新取列、出结果;游标耗尽,fall through 到 addr=7。

**addr=7 `Halt`** ——结束执行。

**addr=8 `Transaction 0 0 1`** ——开始事务。`p1=0` 数据库序号;`p2=0` 是读事务标志(0=读,1=写);`p3=1` 是 statement journal 标志。**这条是 `Init` 跳过来执行的初始化代码**。

**addr=9 `Integer 2 1`** ——把常量 2 写进寄存器 1(供 `SeekGE` 的 p3=1 用)。`p1=2` 是常量值,`p2=1` 是目标寄存器。注释 `r[1]=2`。

**addr=10 `Goto 0 1`** ——跳到 addr=1(`OpenRead`),开始主程序体。

把这张表串起来,执行流是:`0 Init →(跳)8 Transaction → 9 Integer → 10 Goto →(跳)1 OpenRead → 2 SeekGE → 3 Column → 4 Column → 5 ResultRow →(返一行)6 Next →(跳)3 Column → 4 Column → 5 ResultRow → 6 Next →(耗尽,fall through)7 Halt`。这就是一条 `SELECT name,age FROM users WHERE id>=2` 的完整执行。

> **钉死这件事**:EXPLAIN 的输出就是 opcode 流本身,**读懂这张表你就读懂了 SQLite 怎么执行你的 SQL**。关键规则:① `addr` 是指令下标;② 跳转 opcode(`Init`/`Goto`/`Rewind`/`Next`/`SeekGE`/`If`/...)的 `p2` 是目标 addr;③ 非跳转 opcode 的 `p1/p2/p3` 各有各的语义(`Column` 的 p2 是列号、`ResultRow` 的 p2 是列数、`OpenRead` 的 p2 是根页号),要按 opcode 区分;④ `Init` 的 p2 指向"主程序体起点",初始化代码(Transaction/常量赋值)排在末尾由 `Init` 跳过去执行一次。**EXPLAIN 是你调试 SQL 性能的第一工具——慢查询先 EXPLAIN,看它有没有走索引(SeekGE vs Rewind 全表扫)、有没有多余的 opcode。**

### 全表扫的 EXPLAIN:Rewind + Next 的循环模式

把上面那条改成 `SELECT * FROM users`(不带 WHERE,全表扫),EXPLAIN 长这样:

```
addr  opcode       p1  p2  p3  p4    p5  comment
----  -----------  --  --  --  ----  --  -----------------------------
0     Init         0   9   0         0   Start at 9
1     OpenRead     0   2   0   3     0   root=2 iDb=0; users
2     Rewind       0   8   0         0
3     Rowid        0   1   0         0   r[1]=users.rowid
4     Column       0   1   2         0   r[2]= cursor 0 column 1
5     Column       0   2   3         0   r[3]= cursor 0 column 2
6     ResultRow    1   3   0         0   output=r[1..3]
7     Next         0   3   0         1
8     Halt         0   0   0         0
9     Transaction  0   0   1   0     1   usesStmtJournal=0
10    Goto         0   1   0         0
```

和上一张对比,关键区别是 `SeekGE` 换成了 **`Rewind`**:`Rewind 0 8` 把游标定位到表的第一条,如果表空就跳到 addr=8(`Halt`)。然后 `Rowid`/`Column`/`Column`/`ResultRow` 是循环体,`Next 0 3` 的 p2=3 是循环体顶(addr=3),p5=1 是性能 counter 下标(累加全表扫步数到 `aCounter[1]`)。

注意这里 `Rowid` 是新增的——因为 `SELECT *` 包含 `id`(INTEGER PRIMARY KEY 就是 rowid),所以多一条 `Rowid 0 1` 把 rowid 取到寄存器 1。`Rowid` 是 `Column` 的特化版,专门取 rowid(比通用 `Column` 快,不用解 record)。

> **对比读写两条 EXPLAIN**:`WHERE id>=2`(走主键索引)用 `SeekGE` 精确定位起点;`SELECT *`(全表扫)用 `Rewind` 回到表头。**这是 SQLite 决定走索引还是全表扫的直接体现——EXPLAIN 里出现 `SeekGE/SeekGT/SeekRowid` 说明走了索引,出现 `Rewind`+`Next` 说明全表扫。** 慢查询优化第一招:EXPLAIN 看它是 Seek 还是 Rewind。

---

## 四、OpenRead:打开一个游标,连上 B-tree

现在把 EXPLAIN 读会了,我们逐个拆那四个最核心的 opcode。从 `OpenRead` 开始——它是"编译与执行"通往"存储与事务"的真正起点,上一章讲游标时已经贴过它的 case 体,这里从"操作数怎么用"的角度再拆透。

### `OpenRead` 的操作数语义

`OpenRead p1 p2 p3 p4 p5`:

- **`p1`**——游标号。这条 opcode 在 `p->apCsr[p1]` 这个槽位分配一个新的 `VdbeCursor`,后续 `Column`/`Next`/`SeekRowid` 通过这个编号找到游标。
- **`p2`**——B-tree 根页号(这就是 EXPLAIN 里 `root=2` 的来源)。code generator 在编译时从 schema 查到这张表/索引的根页号,塞进 p2。临时表场景下 p2 可以是寄存器号(配 `OPFLAG_P2ISREG` 标志)。
- **`p3`**——数据库序号。0=main 主库,1=temp 临时库,2+=ATTACH 进来的附加库。多数据库时用它区分。
- **`p4`**——表游标时是 `P4_INT32`(列数 `nField`);索引游标时是 `P4_KEYINFO`(键信息结构)。**这个 p4 的类型决定了游标是表游标还是索引游标**。
- **`p5`**——标志位。`OPFLAG_SEEKEQ`(只做等值查找,可优化)、`OPFLAG_FORDELETE`(配合 `OpenWrite`,游标用于删除)、`OPFLAG_P2ISREG`(p2 是寄存器号)。

### case 体逐段拆

`OP_OpenRead` 和 `OP_OpenWrite` 共享同一个 case 体([`vdbe.c:4421`](../sqlite/src/vdbe.c#L4421)),只在"是否设写标志"那一点分叉。这是 SQLite 用 fallthrough 共享代码的一个典型(上一章讲过这个技巧):

```c
case OP_OpenRead:            /* ncycle */
case OP_OpenWrite:
  assert( pOp->opcode==OP_OpenWrite || pOp->p5==0 || pOp->p5==OPFLAG_SEEKEQ );
  assert( p->bIsReader );
  assert( pOp->opcode==OP_OpenRead || pOp->opcode==OP_ReopenIdx
          || p->readOnly==0 );

  if( p->expired==1 ){
    rc = SQLITE_ABORT_ROLLBACK;
    goto abort_due_to_error;
  }

  nField = 0;
  pKeyInfo = 0;
  p2 = (u32)pOp->p2;          /* ★ 根页号 */
  iDb = pOp->p3;              /* ★ 数据库序号(0=main) */
  assert( iDb>=0 && iDb<db->nDb );
  assert( DbMaskTest(p->btreeMask, iDb) );
  pDb = &db->aDb[iDb];
  pX = pDb->pBt;              /* ★ 拿到这个 db 的 B-tree 句柄 */
  assert( pX!=0 );
  if( pOp->opcode==OP_OpenWrite ){
    /* 写游标:设 BTREE_WRCSR(可写)标志 */
    assert( OPFLAG_FORDELETE==BTREE_FORDELETE );
    wrFlag = BTREE_WRCSR | (pOp->p5 & OPFLAG_FORDELETE);
    assert( sqlite3SchemaMutexHeld(db, iDb, 0) );
    if( pDb->pSchema->file_format < p->minWriteFileFormat ){
      p->minWriteFileFormat = pDb->pSchema->file_format;
    }
    if( pOp->p5 & OPFLAG_P2ISREG ){
      /* p2 是寄存器号,根页号从那个寄存器取(临时表场景) */
      assert( p2>0 );
      assert( p2<=(u32)(p->nMem+1 - p->nCursor) );
      pIn2 = &aMem[p2];
      assert( memIsValid(pIn2) );
      assert( (pIn2->flags & MEM_Int)!=0 );
      sqlite3VdbeMemIntegerify(pIn2);
      p2 = (int)pIn2->u.i;   /* ★ 从寄存器拿真正的根页号 */
      assert( p2>=2 );
    }
  }else{
    wrFlag = 0;               /* 读游标:不可写 */
    assert( (pOp->p5 & OPFLAG_P2ISREG)==0 );
  }
  if( pOp->p4type==P4_KEYINFO ){
    pKeyInfo = pOp->p4.pKeyInfo;          /* 索引游标:键信息 */
    assert( pKeyInfo->enc==ENC(db) );
    assert( pKeyInfo->db==db );
    nField = pKeyInfo->nAllField;
  }else if( pOp->p4type==P4_INT32 ){
    nField = pOp->p4.i;                   /* 表游标:列数 */
  }
  assert( pOp->p1>=0 );
  assert( nField>=0 );
  testcase( nField==0 );  /* Table with INTEGER PRIMARY KEY and nothing else */
  pCur = allocateCursor(p, pOp->p1, nField, CURTYPE_BTREE);  /* ★ 分配游标 */
  if( pCur==0 ) goto no_mem;
  pCur->iDb = iDb;
  pCur->nullRow = 1;
  pCur->isOrdered = 1;
  pCur->pgnoRoot = p2;
#ifdef SQLITE_DEBUG
  pCur->wrFlag = wrFlag;
#endif
  rc = sqlite3BtreeCursor(pX, p2, wrFlag, pKeyInfo, pCur->uc.pCursor); /* ★ 桥 */
  pCur->pKeyInfo = pKeyInfo;
  pCur->isTable = pOp->p4type!=P4_KEYINFO;   /* p4 是 KeyInfo → 索引;否则表 */
  ...
  break;
```

这条 opcode 干三件事:

**第一件:从指令参数取出"开哪棵树"**。`p2` 是根页号(或 `OPFLAG_P2ISREG` 时从寄存器取),`p3` 是数据库序号,`p4` 是 KeyInfo(索引)或列数(表)。这些是 code generator 在编译时从 schema 里查好塞进 opcode 的。`pDb = &db->aDb[iDb]; pX = pDb->pBt;` 拿到这个数据库的 B-tree 句柄 `pX`。

**第二件:`allocateCursor(p, pOp->p1, nField, CURTYPE_BTREE)`**——在 `p->apCsr[p1]` 这个槽位分配一个新的 `VdbeCursor`(柔性数组按 `nField` 分配,上一章讲过 `aType[FLEXARRAY]` 那个紧凑布局)。`p1` 是游标编号,后续 `OP_Column`/`OP_Next` 通过这个编号找到游标。

**第三件:`sqlite3BtreeCursor(pX, p2, wrFlag, pKeyInfo, pCur->uc.pCursor)`**——**这一步是"桥"!**调 `btree.c` 的接口,真正打开一个底层 `BtCursor`,存进 `pCur->uc.pCursor`。从这一刻起,这个 VDBE 游标就连上了一棵具体的 B-tree,后续的 `OP_SeekRowid`/`OP_Next`/`OP_Column` 都能通过这个 `BtCursor` 操作 B-tree。

注意那行决定性的 `pCur->isTable = pOp->p4type!=P4_KEYINFO`——**游标是表游标还是索引游标,由 p4 的类型决定**:p4 是 `P4_KEYINFO`(键信息)说明这是索引游标(索引的键要按 KeyInfo 排序),否则是表游标(按 rowid 排序)。这个 `isTable` 标志会影响后续 `OP_SeekRowid`(只能用于表游标,因为它按 rowid 找)和 `OP_Next` 的行为。

> **钉死这件事(承接 P2-05)**:`OpenRead` 是"编译执行通往存储"的真正起点——它把一个抽象的"游标编号 p1"绑定到一个具体的 B-tree 根页号 p2,通过 `sqlite3BtreeCursor` 在 VDBE 游标里塞进一个 `BtCursor` 指针。**从这一条 opcode 开始,VDBE 不再是个纯计算器,它接上了底下的存储。** P3-08 章(B-tree)会从桥的另一头讲 `sqlite3BtreeCursor` 在 B-tree 侧干了什么(在根页上建一个游标位置、加载页头等)。

> **不这样会怎样(为什么游标要编号,不直接传指针)**:如果游标用指针而不是编号,VDBE 指令里要塞 64 位指针(在 32 位系统也是 32 位),而且指针不能跨进程/不能持久化。用编号(`p1` 是个 `int`),code generator 只管"我用游标 0、游标 1",执行时 `apCsr[p1]` 找到实际游标——解耦了"编译期符号"和"运行期对象"。这是编译器/虚拟机的通用设计(Lua 的 upvalue/寄存器也是编号,不是指针)。

---

## 五、Column:从游标取一列,VDBE 最复杂的 opcode

`OpenRead` 把游标连上 B-tree 了,但真正取数据是 `Column` 干的。这是 VDBE 里**最复杂、最长**的 opcode(从 [`vdbe.c:3010`](../sqlite/src/vdbe.c#L3010) 到 3303 行,近 300 行)。它的复杂全在性能优化——每一行都在堵一个性能黑洞。

### `Column` 的操作数语义

`Column p1 p2 p3 p4 p5`:

- **`p1`**——游标号。从 `p->apCsr[p1]` 拿游标。
- **`p2`**——列号(0 起)。0 通常是 rowid/INTEGER PRIMARY KEY,1 是第一列,以此类推。注意:`Column` 取的是记录里的第 N 列,不是表定义里的列名——code generator 在编译时把列名翻译成列号。
- **`p3`**——目标寄存器号。把取出的值写进 `aMem[p3]`。
- **`p4`**——可选的 `P4_MEM`(列的默认值)。当一行里某列不存在(表 ALTER 加了列,旧行没数据)时,用这个默认值。
- **`p5`**——标志位。`OPFLAG_BYTELENARG`(只算长度,如 `length(X)`)、`OPFLAG_TYPEOFARG`(只算类型,如 `typeof(X)`)——这两个让 `Column` 在某些场景下根本不读列数据,直接返常量。

### case 体:四步走

`Column` 的逻辑分四步,我们看简化后的核心(完整版见 [`vdbe.c:3010`](../sqlite/src/vdbe.c#L3010)):

**步骤 1:行缓存检查——游标移到新行了吗?**

```c
case OP_Column: {            /* ncycle */
  ...
  pC = p->apCsr[pOp->p1];    /* 拿游标 */
  p2 = (u32)pOp->p2;         /* 拿列号 */
  ...
  aOffset = pC->aOffset;

  if( pC->cacheStatus!=p->cacheCtr ){      /* ★ 缓存失效:游标移到新行了 */
    if( pC->nullRow ){                     /* 空行:写 NULL 走人 */
      ...
      sqlite3VdbeMemSetNull(pDest);
      goto op_column_out;
    }
    pCrsr = pC->uc.pCursor;
    ...
    /* ★ 步骤 2:从 B-tree 拿到当前行的 record 指针 */
    pC->payloadSize = sqlite3BtreePayloadSize(pCrsr);
    pC->aRow = sqlite3BtreePayloadFetch(pCrsr, &pC->szRow);  /* 零拷贝 */
    ...
    pC->cacheStatus = p->cacheCtr;         /* 标记缓存有效 */
    /* 解 record 头第一个字节(可能整个头就一字节) */
    if( (aOffset[0] = pC->aRow[0])<0x80 ){
      pC->iHdrOffset = 1;
    }else{
      pC->iHdrOffset = sqlite3GetVarint32(pC->aRow, aOffset);
    }
    pC->nHdrParsed = 0;
  }
```

`cacheStatus != cacheCtr` 是缓存失效信号——`cacheCtr` 是全局代际号,每次游标移到新行(`Next`/`Rewind`/`SeekRowid` 都会设 `pC->cacheStatus = CACHE_STALE`),第一次访问这行的任何列,都要重新从 B-tree 拿 record。`sqlite3BtreePayloadFetch` 是**零拷贝**——它返回 B-tree 页在内存(pager 缓存)里的指针,不拷贝数据。

**步骤 3:增量解析 record 头到 p2 列(只解析不够的部分)**

```c
  if( pC->nHdrParsed<=p2 ){
    ...
    do {
      /* 从 iHdrOffset 处逐字节读 varint 的 serial type */
      if( (pC->aType[i] = t = zHdr[0])<0x80 ){
        zHdr++;
        offset64 += sqlite3VdbeOneByteSerialTypeLen(t);
      }else{
        zHdr += sqlite3GetVarint32(zHdr, &t);
        pC->aType[i] = t;
        offset64 += sqlite3VdbeSerialTypeLen(t);
      }
      aOffset[++i] = (u32)(offset64 & 0xffffffff);
    } while( (u32)i<=p2 && zHdr<zEndHdr );
    pC->nHdrParsed = i;
    ...
  } else {
    t = pC->aType[p2];       /* 缓存命中,直接拿 serial type */
  }
```

record 头是变长 varint 序列(每列一个 serial type),解析它要逐字节。SQLite **不一次解析完整个头**,而是**解析到你要的那一列就停**(`nHdrParsed<=p2`),下次访问更后面的列,从 `nHdrParsed` 续上。`SELECT a FROM t`(只取第一列)根本不会去解析 b、c 的 serial type。解析结果(每列的 serial type、每列的偏移)存进 `aType[]`/`aOffset[]`。

**步骤 4:按 serial type 从 aRow+offset 提取列值写进 pDest 寄存器**

```c
  pDest = &aMem[pOp->p3];
  ...
  if( pC->szRow>=aOffset[p2+1] ){
    zData = pC->aRow + aOffset[p2];    /* 列数据在页内,直接读 */
    if( t<12 ){
      sqlite3VdbeSerialGet(zData, t, pDest);   /* NULL/INT/REAL:小数据 */
    }else{
      /* 字符串/BLOB 快路径 */
      static const u16 aFlag[] = { MEM_Blob, MEM_Str|MEM_Term };
      pDest->n = len = (t-12)/2;       /* serial type 算长度 */
      pDest->enc = encoding;
      memcpy(pDest->z, zData, len);    /* 拷贝到寄存器 */
      ...
    }
  }else{
    /* 列数据跨页(溢出页),走 vdbeColumnFromOverflow */
    rc = vdbeColumnFromOverflow(pC, p2, t, aOffset[p2], ...);
  }
```

按 serial type(一个 varint,决定这列是什么类型、多长)从 `aRow+offset` 读数据。`t<12` 是 NULL/INT/REAL 等小数据(走 `sqlite3VdbeSerialGet`),`t>=12` 是字符串/BLOB(`(t-12)/2` 算长度)。大多数行列数据全在一个 B-tree 页里(`szRow>=aOffset[p2+1]`),走快路径直接 memcpy;只有 TEXT/BLOB 太大跨页了(溢出页),才走慢路径 `vdbeColumnFromOverflow`。

### 为什么 Column 写到 300 行

`Column` 的精髓全在**四个性能优化叠加**:

1. **行级缓存(`cacheStatus`)**——游标移到新行后,第一次访问这行的任何列触发"从 B-tree 拿 record + 解析 record 头",解析结果存进 `aType[]`/`aOffset[]`,`cacheStatus` 设成当前 `cacheCtr`。**接下来这行上的其他列访问,直接命中缓存,不用再碰 B-tree、不用再解析 record 头**。这是 `SELECT a, b, c FROM t`(一行取多列)能快起来的根本。
2. **零拷贝拿 record(`sqlite3BtreePayloadFetch`)**——返回 B-tree 页在内存里的指针,不拷贝数据。pager 把页缓存在内存,`PayloadFetch` 直接给你这个缓存页的指针。
3. **增量解析(`nHdrParsed`)**——只解析到目标列,不解析整个头。访问第一列不解析后面的列。
4. **快路径 vs 溢出页分流**——大多数行列数据全在一个页里走快路径,只有跨页(溢出页)走慢路径。

> **不这样会怎样(朴素写法的三个性能黑洞)**:朴素写法是每次 `Column` 都"从 B-tree 读整条 record → 解析整个 record 头 → 提取目标列"。这有三个黑洞:① 每次都读 B-tree(行没变,白读);② 每次都解析整个头(只想要一列);③ 每次都拷贝整条 record(列数据就在页里,直接用指针就行)。SQLite 用"行缓存 + 增量解析 + 零拷贝"三个手段把这三个黑洞全堵了。**这是 `Column` 写到 300 行的原因——每一行都是性能优化。** 这一个 opcode 的复杂度,就体现了 SQLite 把每个常见路径都做成快路径的极致工程。

> **钉死这件事**:`Column p1 p2 p3` 是"游标 p1 的第 p2 列 → 寄存器 p3",但它的实现是 VDBE 里最复杂的——靠行级缓存(`cacheStatus`)让一行只解析一次 record 头、靠零拷贝(`PayloadFetch` 拿页内指针)省 memcpy、靠增量解析(`nHdrParsed`)只解析到目标列、靠快路径/溢出页分流处理大列。**`Column` 是 VDBE 性能优化的集大成者,读懂它就读懂了 SQLite 的"快"从哪来。** record 格式本身(变长 header + serial type)是 P3-09 章的主题,这里只讲 `Column` 怎么解码它。

---

## 六、Next:循环的发动机,带性能埋点

`Column` 是"取当前行的列",`Next` 是"推进游标到下一行,有数据就跳回循环顶"。它是 VDBE 用跳转 opcode 表达循环的核心。

### `Next` 的操作数语义

`Next p1 p2 p3 p5`:

- **`p1`**——游标号。推进这个游标。
- **`p2`**——跳转目标。**游标推进成功(还有数据),跳到 addr=p2**(通常是循环体顶);耗尽则 fall through。
- **`p3`**——btree 提示。0=普通,1=这是索引游标且这次推进本可省略(如果索引唯一)。优化用。
- **`p5`**——性能 counter 下标。`SQLITE_STMTSTATUS_FULLSCAN_STEP`(全表扫步数)或 `SQLITE_STMTSTATUS_AUTOINDEX`(自动索引步数),推进成功时 `aCounter[p5]++`。

### case 体:Prev/Next 共享 next_tail

`OP_Prev` 和 `OP_Next` 共享一个 `next_tail` 尾部([`vdbe.c:6530`](../sqlite/src/vdbe.c#L6530)),又是 fallthrough 共享代码的例子:

```c
case OP_SorterNext: {  /* jump */
  VdbeCursor *pC;
  pC = p->apCsr[pOp->p1];
  assert( isSorter(pC) );
  rc = sqlite3VdbeSorterNext(db, pC);
  goto next_tail;

case OP_Prev:          /* jump, ncycle */
  assert( pOp->p1>=0 && pOp->p1<p->nCursor );
  assert( pOp->p5==0
       || pOp->p5==SQLITE_STMTSTATUS_FULLSCAN_STEP
       || pOp->p5==SQLITE_STMTSTATUS_AUTOINDEX);
  pC = p->apCsr[pOp->p1];
  assert( pC!=0 );
  assert( pC->deferredMoveto==0 );
  assert( pC->eCurType==CURTYPE_BTREE );
  assert( pC->seekOp==OP_SeekLT || pC->seekOp==OP_SeekLE
       || pC->seekOp==OP_Last   || pC->seekOp==OP_IfNoHope
       || pC->seekOp==OP_NullRow);
  rc = sqlite3BtreePrevious(pC->uc.pCursor, pOp->p3);   /* ★ 往前一条 */
  goto next_tail;

case OP_Next:          /* jump, ncycle */
  assert( pOp->p1>=0 && pOp->p1<p->nCursor );
  assert( pOp->p5==0
       || pOp->p5==SQLITE_STMTSTATUS_FULLSCAN_STEP
       || pOp->p5==SQLITE_STMTSTATUS_AUTOINDEX);
  pC = p->apCsr[pOp->p1];
  assert( pC!=0 );
  assert( pC->deferredMoveto==0 );
  assert( pC->eCurType==CURTYPE_BTREE );
  assert( pC->seekOp==OP_SeekGT || pC->seekOp==OP_SeekGE
       || pC->seekOp==OP_Rewind || pC->seekOp==OP_Found
       || pC->seekOp==OP_NullRow|| pC->seekOp==OP_SeekRowid
       || pC->seekOp==OP_IfNoHope);
  rc = sqlite3BtreeNext(pC->uc.pCursor, pOp->p3);       /* ★ 往后一条 */

next_tail:
  pC->cacheStatus = CACHE_STALE;            /* ★ 行变了,Column 缓存失效 */
  VdbeBranchTaken(rc==SQLITE_OK,2);
  if( rc==SQLITE_OK ){
    pC->nullRow = 0;
    p->aCounter[pOp->p5]++;                 /* ★ 累加性能统计 */
#ifdef SQLITE_TEST
    sqlite3_search_count++;
#endif
    goto jump_to_p2_and_check_for_interrupt;/* ★ 有数据 → 跳回循环顶 p2 */
  }
  if( rc!=SQLITE_DONE ) goto abort_due_to_error;
  rc = SQLITE_OK;
  pC->nullRow = 1;
  goto check_for_interrupt;                 /* 游标耗尽 → fall through */
}
```

注意几件事:

- **`OP_SorterNext`/`OP_Prev`/`OP_Next` 三个 opcode 共享 `next_tail`**——SorterNext 用 `sqlite3VdbeSorterNext`(排序游标),Prev 用 `sqlite3BtreePrevious`(B-tree 往前),Next 用 `sqlite3BtreeNext`(B-tree 往后),但它们处理推进结果(成功跳 p2、耗尽 fall through、缓存失效)的逻辑完全一样,合并到 `next_tail`。这是 fallthrough 共享代码的典型。
- **`sqlite3BtreeNext(pC->uc.pCursor, pOp->p3)` 是真正调 B-tree 的那一行**——让底层 `BtCursor` 在 B-tree 上走到下一个键。`p3` 是个提示(0=普通,1=索引且本可省略),B-tree 用它做优化。
- **`pC->cacheStatus = CACHE_STALE` 是关键**——游标移到新行了,`Column` 之前缓存的 `aType[]`/`aOffset[]` 失效了,下次 `Column` 必须重新从 B-tree 拿 record 重解析。这个"行变了就废缓存"的约定,是 `Column` 缓存机制正确性的保证。
- **`goto jump_to_p2_and_check_for_interrupt`**——推进成功,跳到 p2(循环体顶),并顺带做中断/进度回调检查(上一章讲过,这个 label 在 `OP_Goto` case 内,是所有循环底部的统一检查点,只在这里查中断而不在每条 opcode 查,省 1.5% 性能)。
- **`p->aCounter[pOp->p5]++` 是性能埋点**——`p5` 选 counter 下标,推进一次累加一次。`sqlite3_stmt_status()` 能读这些 counter,告诉你这条 SQL 是不是做了全表扫(p5=`SQLITE_STMTSTATUS_FULLSCAN_STEP`)或自动索引(p5=`SQLITE_STMTSTATUS_AUTOINDEX`)。**这是 SQLite 把性能埋点塞进 opcode 的方式——一个 `Next` 既是循环发动机,又是性能计数器。**

### `Next` 和 `Rewind` 怎么配合表达循环

把 `Rewind` + 循环体 + `Next` 拼起来,一个全表扫的 opcode 模式长这样(就是上面那张 `SELECT * FROM users` 的 EXPLAIN):

```
addr  opcode      p1  p2  ...   含义
----  ----------  --  --        ------------------------
2     Rewind      0   8         游标 0 到第一条;空表跳 8(Halt)
3     Rowid       0   1         ┐
4     Column      0   1   2     │ 循环体:取列、出结果
5     Column      0   2   3     │
6     ResultRow   1   3         ┘
7     Next        0   3         游标 0 下一条;有数据跳回 3(循环体顶)
8     Halt                       (空表出口 / 循环结束都到这)
```

`Rewind` 的 p2=8 是"空表跳到循环后",`Next` 的 p2=3 是"有数据跳回循环顶",这是 VDBE 用跳转 opcode 表达循环的标准模式。**`Rewind` 负责初始化(定位第一条 + 空表判断),`Next` 负责步进 + 终止判断,中间夹着循环体。**

> **钉死这件事**:`Next p1 p2` 是循环的发动机——推进游标 p1,成功跳回 p2(循环体顶),耗尽 fall through。它和 `Rewind`(或 `SeekGE`)配合表达循环:`Rewind` 定位起点 + 空表判断,`Next` 步进 + 终止判断。**`Next` 的 p5 还是性能埋点,累加全表扫/自动索引步数到 `aCounter`——一个 opcode 同时是循环控制和性能观测点。** 注意 `Next` 必须跟 `Rewind`/`SeekGE`(向前扫)配,`Prev` 必须跟 `Last`/`SeekLE`(向后扫)配,源码里那些 `assert(pC->seekOp==OP_SeekGT || ...)` 就在保证这个配对正确。

---

## 七、ResultRow:把寄存器段标记成结果行

`Column` 取出的列值存在寄存器里,`ResultRow` 把一段连续的寄存器标记成"一行结果",然后暂停执行,把控制权还给应用——这就是"一次 `sqlite3_step` 返一行"的实现。

### `ResultRow` 的操作数语义

`ResultRow p1 p2`:

- **`p1`**——起始寄存器号。从 `aMem[p1]` 开始。
- **`p2`**——结果列数。寄存器 `p1 .. p1+p2-1` 这一段就是一行结果。

**注意 `p2` 在这里不是跳转目标!** `ResultRow` 不是跳转 opcode(它没有 `OPFLG_JUMP` 位)。它只是标记一段寄存器,然后通过 `goto vdbe_return` 退出主循环(不是跳转 opcode 的 jump)。这是 VDBE 操作数语义复用的一个例子——同一个 `p2` 字段,在 `Goto`/`Next` 里是地址,在 `ResultRow` 里是列数,在 `Column` 里是列号。

### case 体:短短几行,干了件大事

`ResultRow` 的 case 体很短([`vdbe.c:1781`](../sqlite/src/vdbe.c#L1781)),但干的事很关键:

```c
case OP_ResultRow: {
  assert( p->nResColumn==pOp->p2 );    /* 结果列数对得上 */
  assert( pOp->p1>0 || CORRUPT_DB );
  assert( pOp->p1+pOp->p2<=(p->nMem+1 - p->nCursor)+1 );

  p->cacheCtr = (p->cacheCtr + 2)|1;   /* ★ 翻新缓存代际 */
  p->pResultRow = &aMem[pOp->p1];      /* ★ 记录结果行起点 */
#ifdef SQLITE_DEBUG
  {
    Mem *pMem = p->pResultRow;
    int i;
    for(i=0; i<pOp->p2; i++){
      assert( memIsValid(&pMem[i]) );
      REGISTER_TRACE(pOp->p1+i, &pMem[i]);
      /* 结果寄存器重置时会断掉 SCopy 依赖 */
      pMem[i].pScopyFrom = 0;
    }
  }
#endif
  if( db->mallocFailed ) goto no_mem;
  if( db->mTrace & SQLITE_TRACE_ROW ){
    db->trace.xV2(SQLITE_TRACE_ROW, db->pTraceArg, p, 0);  /* trace 回调 */
  }
  p->pc = (int)(pOp - aOp) + 1;        /* ★ 记下一条指令地址 */
  rc = SQLITE_ROW;                      /* ★ 返回码 SQLITE_ROW */
  goto vdbe_return;                     /* ★ 退出主循环,回 sqlite3_step */
}
```

这几行干了四件事:

1. **`p->cacheCtr = (p->cacheCtr + 2)|1`**——翻新缓存代际号。这是个巧妙的写法:`cacheCtr` 是个奇数(每次 `+2` 保持奇数,`|1` 保证低位是 1),`Column` 的 `cacheStatus` 比较的是"是否等于当前 `cacheCtr`"。翻新它,让下次 `Column` 必然发现缓存失效——因为结果行返回后,应用可能调 `sqlite3_column_*` 改寄存器内容,缓存不能再信。
2. **`p->pResultRow = &aMem[pOp->p1]`**——记录结果行起点。`sqlite3_column_text()`/`sqlite3_column_int()` 这些应用侧 API,就是通过 `p->pResultRow[i]` 读第 i 列的值。
3. **`p->pc = (int)(pOp - aOp) + 1`**——**记下一条指令地址**!这是"一次 step 返一行"的关键——`pc` 记成"当前 ResultRow 的下一条",下次 `sqlite3_step` 从这个 `pc` 续上,自然就跳过了已执行的 `ResultRow`,继续跑后面的 `Next` → 循环体。
4. **`rc = SQLITE_ROW; goto vdbe_return`**——设返回码为 `SQLITE_ROW`,退出主循环。`sqlite3_step` 看到 `SQLITE_ROW` 就返给应用,应用拿一行数据,下次再调 `sqlite3_step` 从 `p->pc` 续上。

### "一次 step 返一行"的完整机制

把 `ResultRow` 和 `sqlite3_step` 串起来(上一章讲过状态机,这里从 opcode 角度再看一遍):

```
应用调 sqlite3_step():
  → sqlite3VdbeExec(p) 进主循环
  → ...跑到 ResultRow
  → 设 p->pc = (ResultRow 的 addr + 1)
  → 设 rc = SQLITE_ROW
  → goto vdbe_return 退出主循环
  → sqlite3_step 返 SQLITE_ROW 给应用
应用读一行数据(sqlite3_column_* 读 p->pResultRow)
应用再调 sqlite3_step():
  → sqlite3VdbeExec(p) 进主循环
  → 主循环 for(pOp=&aOp[p->pc]; ...) 从 p->pc 续上
  → 第一条是 ResultRow 的下一条(通常是 Next)
  → Next 推进游标,有数据跳回循环体顶
  → ...跑到下一个 ResultRow,重复
```

**`p->pc` 是跨 `step` 调用的"书签"**——每返一行,`pc` 记下条指令,下次 `step` 从那续上。这套"一次 step 一行"的协议,是 SQLite 流式返回结果集的基础,也是 prepared statement 能"执行到一半暂停、下次续上"的根基。

> **钉死这件事**:`ResultRow p1 p2` 把寄存器 `p1..p1+p2-1` 标记成结果行,记 `p->pc = 下一条指令`,返 `SQLITE_ROW` 退出主循环。**应用通过 `sqlite3_column_*` 读 `p->pResultRow[i]`,下次 `sqlite3_step` 从 `p->pc` 续上——这就是"一次 step 返一行"的全部实现。** 注意 `p2` 在这里是结果列数(不是跳转目标),这是 VDBE 操作数语义复用的体现。`ResultRow` 不结束执行(那是 `Halt` 的事),它只是"暂停返一行",执行真正结束靠 `Halt`(返 `SQLITE_DONE`)。

---

## 八、opcode 分类总表:192 个 opcode,按功能分六大类

把前面拆的几个 opcode 放回全局看。SQLite 总共约 **192 个 opcode**(`grep -c "^#define OP_" opcodes.h` = 192),对照 Lua 5.5 的 47 条指令——**SQLite 因 SQL 复杂度(查询/插入/更新/删除/事务/触发器/聚合/虚拟表/...)指令更多**,但常用的就二三十个。按功能分六大类:

### opcode 分类总表

| 类别 | 代表 opcode | 操作数语义 | 干什么 |
|------|-------------|------------|--------|
| **① 游标类** | `OpenRead` / `OpenWrite` / `OpenEphemeral` / `OpenAutoindex` / `OpenPseudo` / `Close` / `ColumnsUsed` | p1=游标号, p2=根页号, p3=db序号, p4=KeyInfo/列数 | 打开/关闭游标,连上 B-tree(或临时表/伪表) |
| **② 定位/遍历类** | `Rewind` / `Last` / `SeekGE`/`GT`/`LE`/`LT` / `SeekRowid` / `NotExists` / `Next` / `Prev` / `SorterNext` / `NullRow` | p1=游标, p2=跳转目标(或键寄存器), p3=键寄存器 | 把游标移到某个位置(表头/表尾/指定键/下一条) |
| **③ 寄存器类** | `Integer` / `Int64` / `Real` / `String8`/`String` / `Null` / `SoftNull` / `Variable` / `SCopy` / `Copy` / `IntCopy` / `Concat` | p1=常量/源寄存器, p2=目标寄存器, p4=值(64位/字符串/...) | 把常量/参数/别的寄存器的值写进寄存器 |
| **④ 取值/编码类** | `Column` / `Rowid` / `MakeRecord` / `Affinity` / `TypeCheck` / `Sequence` | p1=游标, p2=列号/源, p3=目标寄存器 | 从游标取列(`Column`),或把寄存器编码成 record(`MakeRecord`) |
| **⑤ 结果类** | `ResultRow` | p1=起始寄存器, p2=列数 | 把寄存器段标记成结果行,返 `SQLITE_ROW` |
| **⑥ 控制流类** | `Goto` / `Gosub` / `Return` / `Init` / `InitCoroutine` / `Yield` / `EndCoroutine` / `If` / `IfNot` / `IfNullRow` / `IfPos` / `IfNotZero` / `IfEmpty` / `Halt` / `HaltIfNull` / `Transaction` | p2=跳转目标(Goto/If 类), p1=寄存器(If 类) | 跳转、子程序、条件分支、停止 |
| **⑦ DML/写类** | `OpenWrite` / `NewRowid` / `Insert` / `IdxInsert` / `Delete` / `IdxDelete` / `RowCell` / `FkCheck` | 见各 opcode | 写路径:打开写游标、算 rowid、写 record、删行 |
| **⑧ 聚合类** | `AggStep` / `AggStep1` / `AggInverse` / `AggFinal` / `AggValue` | p4=FuncDef/FuncCtx, p3=累加器寄存器, p2=参数寄存器 | 聚合函数步进(每行调一次)和收尾 |
| **⑨ schema/其他** | `CreateBtree` / `DropBtree` / `ParseSchema` / `ReadCookie` / `SetCookie` / `VerifyCookie` / `JournalMode` / `Vacuum` / `Expire` | 各种 | DDL、schema 维护、VACUUM、cookie 校验 |

这个表不用背——记住几条主线就够:**游标类开/关游标,定位/遍历类移动游标,寄存器类装值,`Column`取列,`ResultRow`出结果,控制流类跳转,写类那组做 INSERT/UPDATE/DELETE,聚合类做 GROUP BY**。其他 opcode 都是这些的变体或辅助。

> **钉死这件事**:SQLite 的 192 个 opcode 不是杂乱无章的,它们围绕"游标 + 寄存器 + 控制流"三件套组织——游标类连 B-tree,定位类移动游标,寄存器类装值,`Column`/`MakeRecord` 在寄存器和 record 之间转换,`ResultRow`/`Halt` 出结果,控制流类跳转。**理解了这个分类框架,遇到没见过的 opcode(EXPLAIN 里冒出来的),你也能从它的 p1/p2/p3 语义猜出它是哪一类、干什么。**

---

## 九、写路径 opcode:INSERT 的 EXPLAIN 对照

前面讲的都是读路径(SELECT)。写路径(INSERT/UPDATE/DELETE)多了一组专门 opcode:`OpenWrite`(带写标志)、`NewRowid`(算下一个 rowid)、`MakeRecord`(把寄存器编码成 record)、`Insert`(写进 B-tree)。看一条真实 INSERT 的 EXPLAIN 就懂了。

### 一条 INSERT 的 EXPLAIN

`EXPLAIN INSERT INTO users VALUES(5,'Dave',40)`(同样的 users 表)的真实输出:

```
addr  opcode       p1  p2  p3  p4      p5  comment
----  -----------  --  --  --  ------  --  -----------------------------
0     Init         0   15  0           0   Start at 15
1     OpenWrite    0   2   0   3       0   root=2 iDb=0; users
2     SoftNull     2   0   0           0   r[2]=NULL
3     String8      0   3   0   Dave     0   r[3]='Dave'
4     Integer      40  4   0           0   r[4]=40
5     Integer      5   1   0           0   r[1]=5
6     NotNull      1   8   0           0   if r[1]!=NULL goto 8
7     NewRowid     0   1   0           0   r[1]=rowid
8     MustBeInt    1   0   0           0
9     Noop         0   0   0           0   uniqueness check for ROWID
10    NotExists    0   12  1           0   intkey=r[1]
11    Halt         1555 2  0   users.id 2
12    MakeRecord   2   3   5   DBD     0   r[5]=mkrec(r[2..4])
13    Insert       0   5   1   users   49  intkey=r[1] data=r[5]
14    Halt         0   0   0           0
15    Transaction  0   1   1   0       1   usesStmtJournal=0
16    Goto         0   1   0           0
```

逐段讲(只讲写路径特有的那些):

**addr=1 `OpenWrite 0 2 0 3`**——和 `OpenRead` 几乎一样,只是 opcode 名是 `OpenWrite`(它和 `OpenRead` 共享 case 体,设 `BTREE_WRCSR` 写标志)。`Transaction` 的 p2=1 也改成写事务了(addr=15)。

**addr=2-5(装值进寄存器)**——`SoftNull 2`(寄存器 2 设成 NULL,这是 id 列,因为 INTEGER PRIMARY KEY 用 rowid)、`String8 0 3 Dave`(寄存器 3 = 'Dave')、`Integer 40 4`(寄存器 4 = 40)、`Integer 5 1`(寄存器 1 = 5,这是显式给的 rowid)。

**addr=6-7(`NotNull`/`NewRowid`,rowid 分配)**——`NotNull 1 8` 检查寄存器 1(id 值)是不是 NULL,不是就跳到 8;是(没显式给 rowid)就走到 `NewRowid 0 1`,算下一个 rowid 写进寄存器 1。**`NewRowid` 的算法很有意思(下面单独拆)**:它先 `sqlite3BtreeLast` 找最大 rowid +1,如果到 MAX_ROWID 了就随机选一个不冲突的 rowid。

**addr=8-11(`MustBeInt`/`Noop`/`NotExists`,rowid 校验)**——`MustBeInt 1` 确保寄存器 1 是整数;`Noop` 那条注释 `uniqueness check for ROWID` 是 SQLite 在 debug 模式插的 rowid 唯一性检查(生产是 no-op);`NotExists 0 12 1` 检查这个 rowid 是不是已存在,**不存在跳到 12(继续插入)**,存在就 fall through 到 `Halt 1555 2`(1555=`SQLITE_CONSTRAINT_PRIMARYKEY`,2=Rollback,报主键冲突)。

**addr=12 `MakeRecord 2 3 5 DBD`**——**把寄存器 2..4(3 个)编码成一条 record,写进寄存器 5**。`p1=2` 起始寄存器,`p2=3` 寄存器个数,`p3=5` 目标寄存器,`p4=DBD` 是 affinity 字符串(D=BLOB affinity 给 SoftNull 那列,B=BLOB... 实际是 code generator 算的每列 affinity)。**`MakeRecord` 是 `Column` 的逆操作**——`Column` 从 record 解码出列,`MakeRecord` 把列编码成 record。

**addr=13 `Insert 0 5 1 users 49`**——**把 record(寄存器 5)以 rowid(寄存器 1)为键写进游标 0 的 B-tree**。`p1=0` 游标,`p2=5` 数据寄存器,`p3=1` 键(rowid)寄存器,`p4=users`(P4_TABLE,触发 update hook 用),`p5=49` 是 `OPFLAG_NCHANGE|OPFLAG_LASTROWID` 的位组合(累加变更计数 + 记 last rowid)。**这一条调 `sqlite3BtreeInsert` 真正写进 B-tree。**

### `NewRowid`:rowid 怎么分配

`NewRowid`([`vdbe.c:5624`](../sqlite/src/vdbe.c#L5624))的算法分两步(源码注释明说):

1. **常规路径**:`sqlite3BtreeLast` 找最大 rowid,+1。如果还没到 `MAX_ROWID`(`0x7fffffffffffffff`),这就是新 rowid。
2. **随机路径**(`useRandomRowid`):如果最大 rowid 已经是 `MAX_ROWID`,SQLite **随机选一个 rowid**,试 100 次看有没有冲突:

```c
    if( pC->useRandomRowid ){
      cnt = 0;
      do{
        sqlite3_randomness(sizeof(v), &v);
        v &= (MAX_ROWID>>1); v++;  /* 确保正数 */
      }while(  ((rc = sqlite3BtreeTableMoveto(pC->uc.pCursor, (u64)v,
                                                 0, &res))==SQLITE_OK)
            && (res==0)              /* res==0 表示已存在 */
            && (++cnt<100));
      if( rc ) goto abort_due_to_error;
      if( res==0 ){
        rc = SQLITE_FULL;   /* 100 次都冲突,放弃 */
        goto abort_due_to_error;
      }
    }
```

这是个"概率上几乎总能成功"的兜底方案——当顺序 rowid 耗尽时,随机选一个。100 次尝试里总有一个不冲突(除非表真的满到 MAX_ROWID 量级,那就 `SQLITE_FULL`)。

### `MakeRecord` 和 `Insert`:写 record 进 B-tree

`MakeRecord`([`vdbe.c:3504`](../sqlite/src/vdbe.c#L3504))是 `Column` 的逆操作。它遍历 `p1..p1+p2-1` 这段寄存器,为每个值算 serial type(根据值的类型和大小),累计 header 长度和 data 长度,然后一次性把 header + data 写进目标寄存器(`p3`)。源码里那段 `if( uu<=127 ){ ... }else if( uu<=32767 ){ ... }` 就是在为整数选最省字节的 serial type(1/2/4/6/8 字节)——和 `Column` 解码时用的 `sqlite3SmallTypeSizes[]` 表一一对应。

`Insert`([`vdbe.c:5783`](../sqlite/src/vdbe.c#L5783))把 `MakeRecord` 产出的 record(`pData`,寄存器 p2)以 rowid(`pKey`,寄存器 p3)为键写进游标 p1 的 B-tree:

```c
case OP_Insert: {
  Mem *pData;       /* record 数据 */
  Mem *pKey;        /* rowid 键 */
  ...
  pData = &aMem[pOp->p2];
  pKey = &aMem[pOp->p3];
  ...
  x.nKey = pKey->u.i;                    /* rowid */
  ...
  x.pData = pData->z;
  x.nData = pData->n;
  seekResult = ((pOp->p5 & OPFLAG_USESEEKRESULT) ? pC->seekResult : 0);
  ...
  rc = sqlite3BtreeInsert(pC->uc.pCursor, &x,
      (pOp->p5 & (OPFLAG_APPEND|OPFLAG_SAVEPOSITION|OPFLAG_PREFORMAT)),
      seekResult
  );
  pC->deferredMoveto = 0;
  pC->cacheStatus = CACHE_STALE;         /* 写了新行,缓存失效 */
  ...
  if( pTab ){
    db->xUpdateCallback(db->pUpdateArg,
           (pOp->p5 & OPFLAG_ISUPDATE) ? SQLITE_UPDATE : SQLITE_INSERT,
           zDb, pTab->zName, x.nKey);    /* update hook */
  }
  break;
}
```

核心是 `sqlite3BtreeInsert(pC->uc.pCursor, &x, ...)`——调 B-tree 接口真正写。`p5` 的 `OPFLAG_APPEND` 提示"这是追加(rowid 比所有现存都大),可优化",`OPFLAG_SAVEPOSITION` 提示"写完保持游标位置",`OPFLAG_PREFORMAT` 提示"record 已经是最终格式,不用重新编码"。写完如果配了 update hook(`db->xUpdateCallback`),触发它——这是 `sqlite3_update_hook` API 的实现。

> **钉死这件事(读路径 vs 写路径对照)**:读路径(SELECT)用 `OpenRead` + `Rewind`/`Seek` + `Column` + `ResultRow` + `Next`;写路径(INSERT)用 `OpenWrite` + (装值) + `NewRowid` + `MakeRecord` + `Insert`。**关键对照:读用 `Column` 解码 record,写用 `MakeRecord` 编码 record(互为逆操作);读用 `OpenRead`(只读游标),写用 `OpenWrite`(可写游标,设 BTREE_WRCSR);写多一组 rowid 管理(`NewRowid` 算 rowid、`NotExists` 查冲突、`MustBeInt` 校验)。** UPDATE 和 DELETE 在这基础上加一层(先读旧行、改、再写或删),核心 opcode 还是这组。P3-08(B-tree)会讲 `sqlite3BtreeInsert` 在 B-tree 侧怎么改页(分裂、平衡),P4-12(rollback journal)/P4-13(WAL)讲这些改怎么保 ACID。

---

## 十、技巧精解:Column 的 record 变长 header 解析 + Next 的循环回跳与性能埋点

正文讲完了,现在挑两个最硬核的技巧单独拆透。

### 技巧一:Column 的 record 变长 header 解析(增量 + 查表双优化)

`Column` 解码 record 时,要解析 record header(变长 varint 序列,每列一个 serial type)。SQLite 这里用了两个叠加优化,让它快得惊人。

**record 格式回顾**(P3-09 会详讲,这里只说 `Column` 怎么解):一条 record = `[header长度 varint][serial_type_1 varint][serial_type_2 varint]...[data_1][data_2]...`。header 第一个字节是 header 总长,后面每列一个 serial type(varint,决定这列类型和长度)。serial type 的语义:

- 0 = NULL
- 1~6 = 整数(分别 1/2/3/4/6/8 字节)
- 7 = IEEE 754 浮点(8 字节)
- 8/9 = 整数常量 0/1(0 字节,值藏在 type 里)
- N>=12 且偶 = BLOB,长度 `(N-12)/2`
- N>=13 且奇 = TEXT,长度 `(N-13)/2`

**优化一:增量解析,只解析到目标列**。`Column` 用 `nHdrParsed` 记"已经解析到第几列",只解析到 `p2`(目标列)就停。`SELECT a FROM t` 只解析第 0 列的 serial type,不碰后面的。下次访问更后面的列,从 `nHdrParsed` 续上。这避免了解析整个头的浪费。

**优化二:小 serial type 查表,大 serial type 算术**。serial type 的"数据长度"函数 `sqlite3VdbeSerialTypeLen`([`vdbeaux.c:3995`](../sqlite/src/vdbeaux.c#L3995)):

```c
/* vdbeaux.c:3975 */
const u8 sqlite3SmallTypeSizes[128] = {
        /*  0   1   2   3   4   5   6   7   8   9 */
/*   0 */   0,  1,  2,  3,  4,  6,  8,  8,  0,  0,
/*  10 */   0,  0,  0,  0,  1,  1,  2,  2,  3,  3,
/*  20 */   4,  4,  5,  5,  6,  6,  7,  7,  8,  8,
/*  30 */   9,  9, 10, 10, 11, 11, 12, 12, 13, 13,
... /* 一直到 127 */
};
u32 sqlite3VdbeSerialTypeLen(u32 serial_type){
  if( serial_type>=128 ){
    return (serial_type-12)/2;       /* 大 type:算术 */
  }else{
    assert( serial_type<12
            || sqlite3SmallTypeSizes[serial_type]==(serial_type - 12)/2 );
    return sqlite3SmallTypeSizes[serial_type];  /* 小 type:查表 */
  }
}
u8 sqlite3VdbeOneByteSerialTypeLen(u8 serial_type){
  assert( serial_type<128 );
  return sqlite3SmallTypeSizes[serial_type];   /* 单字节快路径 */
}
```

**小 serial type(<128,用一个字节 varint)查 128 字节的 `sqlite3SmallTypeSizes[]` 表**——一次数组读,没有分支。`Column` 的内层循环里那段 `if( (pC->aType[i] = t = zHdr[0])<0x80 ){ ... sqlite3VdbeOneByteSerialTypeLen(t) }` 就是这个快路径:serial type < 128(单字节 varint),走查表,极快。只有 serial type >= 128(多字节 varint,大 TEXT/BLOB)才走 `sqlite3GetVarint32` + 算术。

这个 128 字节的表是常量数组,放在 `.rodata`,缓存友好。绝大多数列的 serial type 都 < 128(整数、短字符串),走查表快路径。

> **不这样会怎样(朴素写法)**:朴素写法是 `len = (serial_type>=12) ? (serial_type-12)/2 : small_len_table[serial_type]`,每个 serial type 都要分支判断 + 算术。SQLite 把"小 type 查表"做成无分支的 `sqlite3SmallTypeSizes[t]`(t 已经从 `zHdr[0]` 读出来是个 `u8`,必然 < 128),编译器能把它优化成一条 `movzx` 指令。**这是 SQLite 把"解析 record"这个热点路径优化到极致的体现——128 字节查表 vs 分支算术,差的是每个列一次分支预测的开销,百万行累加起来很可观。**

### 技巧二:Next 的循环回跳 + 中断检查点 + 性能埋点三合一

`Next` 看起来只是"推进游标 + 跳回循环顶",但它巧妙地把三件事合并到一个 opcode 里。

**第一件:循环回跳**。`Next` 成功(还有数据)`goto jump_to_p2_and_check_for_interrupt`,这个 label 在 `OP_Goto` case 内([`vdbe.c:1113`](../sqlite/src/vdbe.c#L1113)),它做 `pOp = &aOp[pOp->p2 - 1]`(减 1 是因为循环末尾会 `pOp++`),然后落到 `check_for_interrupt`。

**第二件:中断检查点**。`check_for_interrupt` label 也在 `OP_Goto` case 内,它检查 `db->u1.isInterrupted`(应用是否调了 `sqlite3_interrupt`)和进度回调。**SQLite 不在每条 opcode 都查中断**(那会让每条 opcode 多几条指令),而是只在循环底部(`Next`/`Prev`/`VNext`/`SorterNext` 跳到 `jump_to_p2_and_check_for_interrupt`)查一次。源码注释明说([`vdbe.c:1116`](../sqlite/src/vdbe.c#L1116)):

> This code uses unstructured "goto" statements and does not look clean. But that is not due to sloppy coding habits. The code is written this way for performance, to avoid having to run the interrupt and progress checks on every opcode. **This helps sqlite3_step() to run about 1.5% faster according to "valgrind --tool=cachegrind"**.

SQLite 为了 1.5% 的性能,宁愿写"不干净"的 `goto`——把中断检查挂在循环底部,非循环代码不查。这是嵌入式数据库对性能极致追求的缩影。

**第三件:性能埋点**。`p->aCounter[pOp->p5]++` 累加性能 counter。`p5` 选哪个 counter:`SQLITE_STMTSTATUS_FULLSCAN_STEP`(全表扫步数)还是 `SQLITE_STMTSTATUS_AUTOINDEX`(自动索引步数)。code generator 在生成 `Next` 时,如果这是个全表扫,就设 `p5=SQLITE_STMTSTATUS_FULLSCAN_STEP`;如果是自动索引扫,设 `p5=SQLITE_STMTSTATUS_AUTOINDEX`。应用调 `sqlite3_stmt_status(stmt, SQLITE_STMTSTATUS_FULLSCAN_STEP, 0)` 能读到这个 counter,判断这条 SQL 是不是做了全表扫(性能诊断)。

**三合一的意义**:`Next` 一个 opcode,同时是循环发动机(回跳)、中断检查点(只在这里查中断省性能)、性能观测点(埋 counter)。这是 SQLite "一个 opcode 干多件事"的设计哲学——把相关的副作用合并,减少 opcode 数量,减少主循环开销。

> **钉死这件事**:`Next` 不是简单的"游标 +1",它是"循环回跳 + 中断检查 + 性能埋点"三合一。① 回跳靠 `goto jump_to_p2_and_check_for_interrupt`(共享 `OP_Goto` 的 label);② 中断检查只在循环底部做(非循环代码不查,省 1.5%);③ 性能 counter 用 `p5` 选下标(`aCounter[p5]++`),让 `sqlite3_stmt_status` 能诊断全表扫。**这是 SQLite 把循环控制、中断响应、性能观测合并到一个 opcode 的工程艺术——一个 `Next` 顶三个独立功能。**

---

## 十一、章末小结

### 回扣主线

本章是全书"编译与执行"这一面的展开——上一章(P2-05)讲 VDBE 这个虚拟机**怎么执行** opcode(主循环、寄存器、游标),本章讲 **opcode 本身长什么样、各自干什么**。一条 opcode 是 `struct VdbeOp`(opcode + p1/p2/p3 三个 int 操作数 + p4 tagged union + p5 标志位),192 个 opcode 按功能分六大类(游标/定位/寄存器/取值/结果/控制流 + 写路径 + 聚合)。`OpenRead`/`Column`/`Next`/`ResultRow` 四个核心 opcode 串起来就是一条 SELECT 的骨架:`OpenRead` 建游标连 B-tree → `Column` 从 record 解码列进寄存器 → `ResultRow` 把寄存器段标成结果行返一行 → `Next` 推进游标跳回循环顶。写路径(INSERT)多一组 `OpenWrite`/`NewRowid`/`MakeRecord`/`Insert`。

**opcode 是"编译与执行"通往"存储与事务"的接口**——`OpenRead`/`Column`/`Next`/`Insert` 这些 opcode 经游标调 B-tree 接口(`sqlite3BtreeCursor`/`PayloadFetch`/`Next`/`Insert`),从这一章开始,我们就要从"opcode 怎么执行"过渡到"B-tree 怎么存数据"。下一章 P2-07 会把整条 SELECT 的编译+执行串起来(从 SQL 字符串到 opcode 流到结果),作为"编译与执行"这一半的收尾;然后 P3-08(B-tree)从 opcode 调的那些 B-tree 接口的另一头讲存储。

### 五个为什么

1. **为什么 opcode 用 p1/p2/p3 三个定长 int 操作数 + p4 tagged union,而不是变长参数列表?**——定长让指令格式统一(16 字节起步,缓存友好)、主循环 switch 分发高效(编译器能优化成 jump table)、操作数访问是 `pOp->p1` 一条 `mov`。变长参数列表要解析、不缓存友好。三个 int 够 90% 的 opcode,p4 union 装不下的复杂信息(KeyInfo/FuncDef/字符串)。**这是"统一格式 + 灵活携带"的平衡。**

2. **为什么 `p2` 在不同 opcode 里语义不同(跳转目标/列号/列数/根页号)?**——操作数复用,省字段。靠 `OPFLG_JUMP` 属性位区分:有 `OPFLG_JUMP` 的 opcode(`Goto`/`Next`/`Rewind`/`SeekGE`/`If`/...),p2 是跳转目标;没有的,p2 按各自语义(`Column` 的列号、`ResultRow` 的列数、`OpenRead` 的根页号)。这让 4 个操作数能表达远超 4 种语义。**这是 VDBE 操作数模型的精妙——一个字段,多种语义,靠属性位区分。**

3. **为什么 `Column` 写到 300 行?**——全是性能优化:行级缓存(`cacheStatus`)让一行只解析一次 record 头、零拷贝(`PayloadFetch` 拿页内指针)省 memcpy、增量解析(`nHdrParsed`)只解析到目标列、小 serial type 查 128 字节表(`sqlite3SmallTypeSizes`)无分支、快路径/溢出页分流。**每行都在堵一个性能黑洞,这是 `Column` 是 VDBE 最复杂 opcode 的原因。**

4. **为什么 `Next` 的 `p5` 用来选性能 counter,而不是新增一个 opcode?**——三合一哲学:`Next` 既是循环发动机(回跳),又是中断检查点(只在这里查中断省 1.5%),又是性能埋点(`aCounter[p5]++`)。合并相关的副作用到一个 opcode,减少 opcode 数量和主循环开销。`p5` 本来就是标志位,复用它选 counter 不增加指令长度。**这是 SQLite "一个 opcode 干多件事"的设计哲学。**

5. **为什么 INSERT 用 `MakeRecord` + `Insert` 两条 opcode,不合成一条?**——分离关注点:`MakeRecord` 只管"把寄存器编码成 record 格式"(纯计算,不碰 B-tree),`Insert` 只管"把 record 写进 B-tree"(调 `sqlite3BtreeInsert`)。分离让 `MakeRecord` 可以被复用(索引也用 record 格式,`IdxInsert` 前面也有 `MakeRecord`),让 `Insert` 的 case 体聚焦于 B-tree 写 + hook 触发。**这是"一个 opcode 一个原子动作"的原则——编码和写盘是两件事,分开。**

### 想继续深入往哪钻

- **想看全部 192 个 opcode 的语义**:读 `vdbe.c` 里每个 `/* Opcode: ... */` 文档注释(SQLite 源码里每个 opcode 都有文档注释,由 `mkopcodeh.tcl` 提取生成 `opcodes.h` 和官方 opcode.html)。或访问 SQLite 官方文档的 "Opcode" 页面。
- **想自己玩 EXPLAIN**:起一个 `sqlite3` CLI,建张表插点数据,`EXPLAIN SELECT ...` 看它的 opcode 流。改 WHERE 条件(加索引、加 ORDER BY、加 JOIN),对比 EXPLAIN 变化——这是学 SQL 性能优化最快的方法。
- **想看性能埋点怎么读**:调 `sqlite3_stmt_status(stmt, SQLITE_STMTSTATUS_FULLSCAN_STEP, 0)` 和 `SQLITE_STMTSTATUS_AUTOINDEX`,看你的 SQL 做了多少全表扫步数和自动索引步数。需 `SQLITE_ENABLE_STMT_SCANSTATUS` 编译。
- **想看 record 格式细节**:P3-09 章(记录格式 + 动态类型)会详讲 record 的变长 header + serial type + type affinity。本章只讲 `Column`/`MakeRecord` 怎么编解码。
- **想看 B-tree 接口的另一头**:P3-08 章(B-tree 存储)从 `sqlite3BtreeCursor`/`First`/`Next`/`PayloadFetch`/`Insert` 这些 opcode 调的接口的另一头讲——它们在 B-tree 侧怎么移动游标、怎么读页、怎么写页。
- **想对照其他 VM 的指令集**:对照《Lua 虚拟机深入浅出》的指令集章节——Lua 5.5 是 47 条定长 32 位指令(极致紧凑),SQLite 是 192 条不定长 struct 指令(够宽够灵活)。两种取舍反映了"嵌入式脚本 VM"和"数据库 VM"的不同需求。

### 引出下一章

本章把 VDBE 的核心 opcode(`OpenRead`/`Column`/`Next`/`ResultRow` + 写路径 `OpenWrite`/`NewRowid`/`MakeRecord`/`Insert`)逐一拆透了,你会读 EXPLAIN、知道每个 opcode 干什么。但一条真实的 SELECT 往往比 `SELECT * FROM users` 复杂得多——有 WHERE(已看到 SeekGE)、有 JOIN(多游标 + 嵌套循环)、有 ORDER BY(Sorter 游标)、有 GROUP BY(AggStep 聚合)、有子查询(子程序 opcode)。**下一章 P2-07《一条 SELECT 怎么执行》,会把整条 SELECT 的编译 + 执行串起来**——从 SQL 字符串怎么一步步变成 opcode 流(code generator 怎么为每个 SQL 节点生成 opcode),到这些 opcode 怎么在 VDBE 里串成完整执行流,特别会拆 WHERE 怎么变成 Seek/filter、JOIN 怎么变成嵌套循环、ORDER BY 怎么变成 Sorter。这是"编译与执行"这一半的收尾,之后 P3-08 起进入"存储与事务"。

> **下一章**:[P2-07 · 一条 SELECT 怎么执行](P2-07-一条SELECT怎么执行.md)

---

> **承接索引**:本章承接 P2-05(VDBE 虚拟机)——P2-05 讲 VDBE 主循环骨架(怎么 fetch-decode-execute),本章讲循环里那一个个 `case` 具体干什么(opcode 本身)。本章涉及的 opcode 经游标调 B-tree 接口(`sqlite3BtreeCursor`/`PayloadFetch`/`Next`/`Insert`),这些接口的另一头是 P3-08(B-tree 存储);写路径 opcode(`Insert`)调的 `sqlite3BtreeInsert` 涉及改页,改页的 ACID 保证是 P4-12(rollback journal)/P4-13(WAL)。本章不重复《Lua》VM 基础(opcode 循环、寄存器模型《Lua》讲过),只讲 SQLite opcode 独有部分(游标连 B-tree、record 编解码、写路径 rowid 管理)。
