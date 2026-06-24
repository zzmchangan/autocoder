# P2-09 代码生成 lcode:寄存器分配与指令发射

> **本章主线**:统一与精简换小而快——代码生成是编译侧的收尾,也是"寄存器式字节码"为什么紧凑的最直接体现。Lua 用线性寄存器分配 + 延迟表达式表示,把任何一条表达式压成尽可能少、尽可能紧凑的 32 位指令。**二分法**:编译侧(P2)收尾,接 P2-07/08。**★对照**:CPython 栈式编译器(只压栈弹栈,不做寄存器分配)。**源码**:lua-5.5.0,`lcode.h`/`lcode.c`/`lopcodes.h`/`lparser.h`。**基调**:纯直球,不用比喻。

---

## 一、这章解决什么问题

语法分析(P2-07/08)把一段 Lua 源码递归下降地拆成一棵抽象语法树,但 Lua **边解析边生成**:每认出一个表达式,立刻把它"落"成字节码。本章要回答的是:**一个表达式求值的中间结果和最终结果,到底放到哪里去?用什么形式存?最后怎么变成那 32 位的指令?**

这是编译侧的收尾,也是全书主线"统一与精简换小而快"里**精简二字最直接的落脚点**。回顾 P0-01 的结论:Lua 的字节码是寄存器式的,操作数直接是寄存器号,不像 CPython 那样靠共享栈来回压弹。寄存器式"指令少、紧凑"的好处是在执行期兑现的,但代价是编译器必须做一件事——**寄存器分配**:决定哪个值放哪个寄存器。这件事做不好,要么寄存器用爆,要么生成的指令反而更多。Lua 的选择很特别:

- **不做图着色分配**。教科书式的寄存器分配是 NP 难(图着色、活跃变量分析),Lua 完全不碰。它用一种**线性分配**:局部变量按声明顺序占低槽位,临时表达式结果从 `freereg` 往后顺序占用,用完即释放。
- **延迟表示(expdesc)**。一个表达式在被真正"消费"之前,不急着落进寄存器,而是用一个小结构记录"它现在是什么形态"——是常量、是已在某寄存器里、是可重定位的指令结果,还是一张表的下标访问。等真正要用时,才一次性 discharge 成具体指令。这样很多表达式可以就地优化掉,例如 `a > 0` 直接生成 `GTI` 一条指令,不必先 `LOADI 0` 再 `LT`。

这两个选择合在一起,让 Lua 的代码生成器又小(整个 `lcode.c` 在 5.5.0 只有 1971 行)又快(几乎是一次线性扫描表达式树),生成的字节码又少又紧凑。本章要逐函数讲清:

1. 寄存器栈的线性分配是怎么跟踪的(`freereg`/`luaK_reserveregs`/`freereg`)。
2. 表达式的延迟表示 `expdesc`(`lparser.h:78`)有哪些形态,为什么需要它。
3. 把任意形态落到具体寄存器的枢纽 `luaK_dischargevars`(`lcode.c:819`)。
4. 常量表 `Proto->k` 怎么去重登记(`addk`/`k2proto`/`stringK`/`luaK_intK`/`luaK_numberK`)。
5. 32 位指令怎么发射(`luaK_codeABCk`/`luaK_codevABCk`/`luaK_codeABx`/`codeAsBx`/`codesJ`)。
6. 跳转占位与回填(`luaK_jump`/`luaK_patchlist`/`fixjump`),以及多个跳转共享目标时的跳转链表。
7. 表达式优化的几个典型例子(常量折叠、立即数变体 `ADDI`/`SHRI`、常量变体 `ADDK`、比较立即数 `EQI`/`LTI`)。
8. 函数收尾 `luaK_finish`(`lcode.c:1929`)和 `maxstacksize` 的意义。

读完这一章,会看清 Lua 的编译器如何用极简的机制,生成出全书主线反复强调的那种"又少又紧凑"的字节码。

---

## 二、源码怎么实现

### 2.1 寄存器栈与线性分配

先建立"寄存器"的物理含义。Lua 里每个函数有一段自己的**值栈**(其实是 `lua_State::stack` 数组的一段连续槽位,见 P4-13)。函数的"寄存器"就是这段槽位的低若干个:`R0`、`R1`、`R2`……它们既是局部变量的家,也是表达式求值的临时容器。固定参数和 `local` 变量从 `R0` 开始顺序占,剩下的高位槽位是临时区,由代码生成器按需取用。

编译期用一个 `FuncState` 结构(`lparser.h:166`)跟踪当前函数的代码生成状态,关键的几个字段:

```c
typedef struct FuncState {
  ...
  Table *kcache;  /* cache for reusing constants */
  ...
  int nk;  /* number of elements in 'k' */
  ...
  short nactvar;  /* number of active variable declarations */
  lu_byte nups;  /* number of upvalues */
  lu_byte freereg;  /* first free register */
  ...
  lu_byte needclose;  /* function needs to close upvalues when returning */
} FuncState;
```

`freereg` 是线性分配的核心计数器:它指向**下一个可用的临时寄存器号**。所有临时表达式的结果都从 `freereg` 开始往后占。`nactvar`(活跃局部变量数,与寄存器占用一一对应)决定了局部变量占到哪里,临时区从那里再往后排。

线性分配的两条规则:

- **占**:`luaK_reserveregs`(`lcode.c:488`)把 `freereg` 往后推 `n` 位,同时更新 `maxstacksize`:

```c
void luaK_reserveregs (FuncState *fs, int n) {
  luaK_checkstack(fs, n);
  fs->freereg =  cast_byte(fs->freereg + n);
}
```

`luaK_checkstack`(`lcode.c:476`)负责跟踪本函数用到的最大寄存器数:

```c
void luaK_checkstack (FuncState *fs, int n) {
  int newstack = fs->freereg + n;
  if (newstack > fs->f->maxstacksize) {
    luaY_checklimit(fs, newstack, MAX_FSTACK, "registers");
    fs->f->maxstacksize = cast_byte(newstack);
  }
}
```

这里 `MAX_FSTACK`(`lopcodes.h:210`)是 `MAXARG_A = 255`,因为寄存器号必须装进 8 位的 A 操作数。所以一个 Lua 函数最多用 255 个寄存器,超了编译报错。

- **释放**:某个临时表达式用完后,要把它占的寄存器还回去。`freereg`(`lcode.c:499`)只释放**高于局部变量区**的临时寄存器:

```c
static void freereg (FuncState *fs, int reg) {
  if (reg >= luaY_nvarstack(fs)) {
    fs->freereg--;
    lua_assert(reg == fs->freereg);
  }
}
```

关键判断 `reg >= luaY_nvarstack(fs)`:如果 `reg` 落在局部变量区内(即它其实是个局部变量的家),不释放——因为局部变量的生命周期由作用域管,不受临时表达式影响。只有临时寄存器才参与 LIFO 式的退栈。`lua_assert(reg == fs->freereg)` 是一个强约束:**临时寄存器必须严格从高到低释放**,中间不能有空洞**。这是线性分配的不变式。

为什么这种线性栈式分配就够?因为 Lua 的表达式求值是树形的,递归下降解析时,子表达式一定先于父表达式完成、用完即弃。任何一棵表达式树的后序遍历,临时寄存器的占用都是一串嵌套的 push/pop,完全可以线性模拟。Lua 没有"表达式的值要在两个不相关的地方同时存活很久"的情况(那种才需要图着色),所以不需要活跃变量分析。

两个临时寄存器的释放还有顺序问题:`freeregs`(`lcode.c:510`)先释放高的再释放低的,保证断言不破:

```c
static void freeregs (FuncState *fs, int r1, int r2) {
  if (r1 > r2) {
    freereg(fs, r1);
    freereg(fs, r2);
  }
  else {
    freereg(fs, r2);
    freereg(fs, r1);
  }
}
```

二元运算的两个操作数正好需要这种成对释放(见 2.5)。

### 2.2 expdesc:表达式的延迟表示

线性分配回答了"临时结果放哪",但还有一个更关键的问题:**一个表达式在被消费之前,要不要立刻算出来?** Lua 的答案是:**不要**。能用更省的方式表示,就尽量延迟。

`expdesc`(`lparser.h:78`)是表达式的延迟描述符:

```c
typedef struct expdesc {
  expkind k;
  union {
    lua_Integer ival;    /* for VKINT */
    lua_Number nval;  /* for VKFLT */
    TString *strval;  /* for VKSTR */
    int info;  /* for generic use */
    struct {  /* for indexed variables */
      short idx;  /* index (R or "long" K) */
      lu_byte t;  /* table (register or upvalue) */
      lu_byte ro;  /* true if variable is read-only */
      int keystr;  /* index in 'k' of string key, or -1 if not a string */
    } ind;
    struct {  /* for local variables */
      lu_byte ridx;  /* register holding the variable */
      short vidx;  /* index in 'actvar.arr' */
    } var;
  } u;
  int t;  /* patch list of 'exit when true' */
  int f;  /* patch list of 'exit when false' */
} expdesc;
```

`k` 字段是 `expkind` 枚举(`lparser.h:25`),记录这个表达式当前以什么形态存在。5.5.0 的完整形态:

```c
typedef enum {
  VVOID,    /* 空列表(表达式列表的最后一个) */
  VNIL,     /* 常量 nil */
  VTRUE,    /* 常量 true */
  VFALSE,   /* 常量 false */
  VK,       /* 常量表 k 中的项;info = 常量索引 */
  VKFLT,    /* 浮点常量;nval = 数值 */
  VKINT,    /* 整数常量;ival = 数值 */
  VKSTR,    /* 字符串常量;strval = TString 指针 */
  VNONRELOC,/* 已落在固定寄存器;info = 结果寄存器 */
  VLOCAL,   /* 局部变量;var.ridx = 寄存器 */
  VVARGVAR, /* 可变参数(vararg parameter);var.ridx = 寄存器 */
  VGLOBAL,  /* 全局变量;info = actvar.arr 中的相对索引 */
  VUPVAL,   /* upvalue;info = upvalue 索引 */
  VCONST,   /* 编译期 <const> 变量;info = actvar.arr 绝对索引 */
  VINDEXED, /* 下标访问;t[idx],t 是寄存器,idx 是寄存器里的键 */
  VVARGIND, /* 可变参数的下标访问 */
  VINDEXUP, /* upvalue 的下标访问(键必须是字面短字符串) */
  VINDEXI,  /* 整数常量下标;t[k],k 是小整数 */
  VINDEXSTR,/* 字面短字符串下标;t.k */
  VJMP,     /* 比较/测试;info = 对应 JMP 的 pc */
  VRELOC,   /* 可重定位(指令结果可放任意寄存器);info = 指令 pc */
  VCALL,    /* 函数调用;info = CALL 指令 pc */
  VVARARG   /* 可变参数表达式;info = VARARG 指令 pc */
} expkind;
```

为什么需要这么多形态?因为**每种形态对应一种可以延迟发射指令的优化机会**。举几个关键例子:

- **`VINDEXI` / `VINDEXSTR`**:遇到 `t[3]` 或 `t.name` 时,不立刻发射 `GETTABLE`,而是记下"这是 `t` 的某个小整数/短字符串下标"。等到真正要值时,根据情况选 `GETI` 或 `GETFIELD`(见 2.3)。如果这个表达式只是出现在比较的左操作数,可能根本不用单独落寄存器。
- **`VRELOC`**:一条结果可放任意寄存器的指令(如 `ADD`、`GETUPVAL`)。它的目标寄存器字段(A 操作数)在生成时先填 0,等消费者指定寄存器后,再用 `SETARG_A` 改写那条已生成的指令(`discharge2reg` 的 `VRELOC` 分支)。这就避免了"先生成到 R0,再 MOVE 到目标"的浪费。
- **`VJMP`**:一个比较表达式的结果是布尔值,但 Lua 不会立刻把它变成 `true`/`false` 装进寄存器,而是保留成"一条比较指令 + 一条跳转"。`t` 和 `f` 两个跳转链表(`expdesc::t`/`f`)分别记录"条件为真时跳到哪"和"条件为假时跳到哪"。这样 `if a > b then ... end` 根本不用把 `a > b` 的布尔结果落进寄存器,直接用跳转控制流。
- **`VKINT` / `VKFLT`**:常量先不进常量表,保留成数值本身。等到要做运算时,可能直接折叠(`2 + 3` 编译期算成 `5`),或者作为立即数编进 `ADDI`/`LTI` 等"立即数变体"指令。

这一整套设计的精神是:**表达式在被消费之前,只是"一张尚未兑现的欠条"**。谁要用它,谁通过 `luaK_dischargevars` 把欠条兑换成真金白银(具体寄存器或具体指令)。消费者可以选择最优的兑换方式。

> **5.5 vs 老资料差异(重要)**:讲 5.3/5.4 的资料里,`expkind` 枚举要少得多。5.5 新增了 `VCONST`(编译期 `<const>` 局部变量,见 5.4 引入的 `<const>` 修饰)、`VGLOBAL`(显式全局变量,5.5 把全局变量的处理重写,引入 `OP_ERRNNIL` 在访问未声明全局时报错)、`VVARGVAR`/`VVARGIND`(5.5 新引入的"可变参数表"机制,见 `PF_VATAB` 标志)、`VINDEXI`/`VINDEXSTR`(把整数和短字符串下标单独分出来,配 `GETI`/`GETFIELD` 指令)。老资料讲 `VINDEXED` 一把抓的写法在 5.5 已经过时。

### 2.3 luaK_dischargevars:表达式落地的枢纽

`luaK_dischargevars`(`lcode.c:819`)是表达式求值的核心枢纽。它把任意形态的 `expdesc` 转换到一个"已经有值在某处"的形态(通常是 `VNONRELOC` 或 `VRELOC`),过程中发射必要的指令。几乎所有消费者在用表达式之前都先调它:

```c
void luaK_dischargevars (FuncState *fs, expdesc *e) {
  switch (e->k) {
    case VCONST: {
      const2exp(const2val(fs, e), e);
      break;
    }
    case VVARGVAR: {
      luaK_vapar2local(fs, e);  /* turn it into a local variable */
    }  /* FALLTHROUGH */
    case VLOCAL: {  /* already in a register */
      int temp = e->u.var.ridx;
      e->u.info = temp;
      e->k = VNONRELOC;  /* becomes a non-relocatable value */
      break;
    }
    case VUPVAL: {  /* move value to some (pending) register */
      e->u.info = luaK_codeABC(fs, OP_GETUPVAL, 0, e->u.info, 0);
      e->k = VRELOC;
      break;
    }
    case VINDEXUP: {
      e->u.info = luaK_codeABC(fs, OP_GETTABUP, 0, e->u.ind.t, e->u.ind.idx);
      e->k = VRELOC;
      break;
    }
    case VINDEXI: {
      freereg(fs, e->u.ind.t);
      e->u.info = luaK_codeABC(fs, OP_GETI, 0, e->u.ind.t, e->u.ind.idx);
      e->k = VRELOC;
      break;
    }
    case VINDEXSTR: {
      freereg(fs, e->u.ind.t);
      e->u.info = luaK_codeABC(fs, OP_GETFIELD, 0, e->u.ind.t, e->u.ind.idx);
      e->k = VRELOC;
      break;
    }
    case VINDEXED: {
      freeregs(fs, e->u.ind.t, e->u.ind.idx);
      e->u.info = luaK_codeABC(fs, OP_GETTABLE, 0, e->u.ind.t, e->u.ind.idx);
      e->k = VRELOC;
      break;
    }
    case VVARGIND: {
      freeregs(fs, e->u.ind.t, e->u.ind.idx);
      e->u.info = luaK_codeABC(fs, OP_GETVARG, 0, e->u.ind.t, e->u.ind.idx);
      e->k = VRELOC;
      break;
    }
    case VVARARG: case VCALL: {
      luaK_setoneret(fs, e);
      break;
    }
    default: break;  /* there is one value available (somewhere) */
  }
}
```

逐分支看 discharge 的逻辑:

- **`VCONST`**:`const2exp`(`lcode.c:728`)把编译期常量(存在 `actvar.arr` 里,见 P2-08 的 `<const>` 局部)翻译成对应的常量形态(`VKINT`/`VKFLT`/`VKSTR`/`VNIL`/`VTRUE`/`VFALSE`)。这一步不发任何指令。
- **`VLOCAL` / `VVARGVAR`**:局部变量本来就在寄存器里,把 `var.ridx` 复制到 `info`,`k` 改成 `VNONRELOC`。注意源码注释里的 `(can't do a direct assignment; values overlap)`:因为 `info` 和 `var.ridx` 共享同一个 union,不能直接写 `e->u.info = e->u.var.ridx`,要先存到临时变量 `temp` 再赋值。这是 C 语言 union 的细节,但很真实。
- **`VUPVAL`**:发射 `GETUPVAL`,目标寄存器先填 0(等消费者定),`k` 改 `VRELOC`。
- **`VINDEXUP`** / **`VINDEXSTR`** / **`VINDEXI`** / **`VINDEXED`** / **`VVARGIND`**:分别发射对应的取值指令。这里正好体现 5.5 的下标变体优化:`t.x`(短字符串键)用 `GETFIELD`、`t[3]`(小整数键)用 `GETI`、`t[k]`(任意键)才用通用的 `GETTABLE`。每种都比老版本"统一用 GETTABLE"省——短字符串键不用先进常量表再下标,小整数键不用先 LOADK 再下标。注意发射前会先 `freereg`/`freeregs` 释放表寄存器(因为下标访问之后,表本身的临时寄存器就不再需要了,除非它是个局部变量)。`VVARGIND` 配 5.5 新指令 `GETVARG`(`lopcodes.h:341`),用于访问可变参数表的元素。
- **`VVARARG` / `VCALL`**:这两种是"多值表达式"(函数调用或 `...`)。`luaK_setoneret`(`lcode.c:792`)把它们的 C 操作数改成 2(只要一个返回值),`VCALL` 变 `VNONRELOC`(结果固定在 CALL 的 A 寄存器),`VVARARG` 变 `VRELOC`。

`dischargevars` 之后,表达式要么是常量形态(`VKINT`/`VKFLT`/`VKSTR`/`VK`/`VNIL`/`VTRUE`/`VFALSE`),要么是 `VRELOC`(待定寄存器的指令),要么是 `VNONRELOC`(已落定寄存器),要么是 `VJMP`(比较,见 2.6)。这时再进一步,`discharge2reg`(`lcode.c:882`)把它落进**指定的**寄存器:

```c
static void discharge2reg (FuncState *fs, expdesc *e, int reg) {
  luaK_dischargevars(fs, e);
  switch (e->k) {
    case VNIL: {
      luaK_nil(fs, reg, 1);
      break;
    }
    case VFALSE: {
      luaK_codeABC(fs, OP_LOADFALSE, reg, 0, 0);
      break;
    }
    case VTRUE: {
      luaK_codeABC(fs, OP_LOADTRUE, reg, 0, 0);
      break;
    }
    case VKSTR: {
      str2K(fs, e);
    }  /* FALLTHROUGH */
    case VK: {
      luaK_codek(fs, reg, e->u.info);
      break;
    }
    case VKFLT: {
      luaK_float(fs, reg, e->u.nval);
      break;
    }
    case VKINT: {
      luaK_int(fs, reg, e->u.ival);
      break;
    }
    case VRELOC: {
      Instruction *pc = &getinstruction(fs, e);
      SETARG_A(*pc, reg);  /* instruction will put result in 'reg' */
      break;
    }
    case VNONRELOC: {
      if (reg != e->u.info)
        luaK_codeABC(fs, OP_MOVE, reg, e->u.info, 0);
      break;
    }
    default: {
      lua_assert(e->k == VJMP);
      return;  /* nothing to do... */
    }
  }
  e->u.info = reg;
  e->k = VNONRELOC;
}
```

两个细节值得注意:

1. **`VRELOC` 的改写**:不重新发指令,而是直接 `SETARG_A(*pc, reg)` 改写已生成那条指令的目标寄存器字段。这是延迟表示最直接的收益:指令的目标寄存器可以在指令生成之后才确定,不必为了改目标而先 `MOVE`。
2. **`VNONRELOC` 的省略**:`if (reg != e->u.info)`——如果目标寄存器就是表达式现在所在的寄存器,什么都不做。这避免了无谓的 `MOVE R0 R0`。
3. **`VKINT` / `VKFLT` 的特殊路径**:`luaK_int`(`lcode.c:692`)和 `luaK_float`(`lcode.c:700`)会先尝试用 `LOADI`/`LOADF`(立即数编码在 sBx 里,不进常量表),只有数值太大装不下时才走 `luaK_codek` 进常量表。这是又一层优化:小整数连常量表都不用进。

消费者层面,常用的入口有三个:

- `luaK_exp2nextreg`(`lcode.c:999`):把表达式落到**下一个空闲寄存器**(最常用,例如函数调用的实参、表构造器的元素)。
- `luaK_exp2anyreg`(`lcode.c:1011`):把表达式落到**任意一个**寄存器(已经是 `VNONRELOC` 且无跳转就直接返回,否则走 `exp2nextreg`)。
- `luaK_exp2val`(`lcode.c:1043`):把表达式落成"一个值"——有跳转的(`VJMP` 或 `hasjumps`)必须落寄存器,否则只 `dischargevars`。

### 2.4 常量表 Proto->k 与去重

数值、字符串这些字面量不会直接编进指令(Lua 没有那种"指令里嵌一个 64 位 double"的格式)。它们进**常量表** `Proto->k`(`lobject.h` 里 `Proto` 结构的字段),指令通过一个索引引用。`Proto` 里相关的字段(`lobject.h:602`):

```c
typedef struct Proto {
  CommonHeader;
  lu_byte numparams;  /* number of fixed (named) parameters */
  lu_byte flag;
  lu_byte maxstacksize;  /* number of registers needed by this function */
  ...
  TValue *k;  /* constants used by the function */
  ...
} Proto;
```

每个函数(每个 `Proto`)有自己的常量表。常量登记的核心是 `addk`(`lcode.c:545`):

```c
static int addk (FuncState *fs, Proto *f, TValue *v) {
  lua_State *L = fs->ls->L;
  int oldsize = f->sizek;
  int k = fs->nk;
  luaM_growvector(L, f->k, k, f->sizek, TValue, MAXARG_Ax, "constants");
  while (oldsize < f->sizek)
    setnilvalue(&f->k[oldsize++]);
  setobj(L, &f->k[k], v);
  fs->nk++;
  luaC_barrier(L, f, v);
  return k;
}
```

`addk` 只管"加到表尾",不做去重。去重由 `k2proto`(`lcode.c:565`)负责——这是 5.5 的关键改进:

```c
static int k2proto (FuncState *fs, TValue *key, TValue *v) {
  TValue val;
  Proto *f = fs->f;
  int tag = luaH_get(fs->kcache, key, &val);  /* query scanner table */
  if (!tagisempty(tag)) {  /* is there an index there? */
    int k = cast_int(ivalue(&val));
    /* collisions can happen only for float keys */
    lua_assert(ttisfloat(key) || luaV_rawequalobj(&f->k[k], v));
    return k;  /* reuse index */
  }
  else {  /* constant not found; create a new entry */
    int k = addk(fs, f, v);
    setivalue(&val, k);
    luaH_set(fs->ls->L, fs->kcache, key, &val);
    return k;
  }
}
```

`fs->kcache` 是一张 `Table`(就是 P1-04/05 讲的那个万能 Table),用作**常量去重的缓存**。每来一个常量,先以它本身(或它的某种规范化键)查 `kcache`,命中就复用已有的索引,没命中才 `addk` 加到表尾,并在 `kcache` 里登记。

> **5.5 vs 老资料差异(重要)**:讲 5.3/5.4 的资料常说 Lua 的常量去重是"线性扫描整个 `k` 表比对",复杂度 O(n)。5.5 改用一张 `Table`(`fs->kcache`)做哈希缓存,去重变成 O(1)。这是一个不小的性能改进,老资料需要更新。

各种类型的常量入口:

```c
static int stringK (FuncState *fs, TString *s) {
  TValue o;
  setsvalue(fs->ls->L, &o, s);
  return k2proto(fs, &o, &o);  /* use string itself as key */
}

static int luaK_intK (FuncState *fs, lua_Integer n) {
  TValue o;
  setivalue(&o, n);
  return k2proto(fs, &o, &o);  /* use integer itself as key */
}
```

字符串和整数都以**自身**作为 `kcache` 的键,因为它们都是合法的 Table 键。

浮点数麻烦一些,因为存在 `2`(整数)和 `2.0`(浮点)在 Table 里会塌缩成同一个键的问题。`luaK_numberK`(`lcode.c:617`)用一个巧妙的规范化键来避免碰撞:

```c
static int luaK_numberK (FuncState *fs, lua_Number r) {
  TValue o, kv;
  setfltvalue(&o, r);  /* value as a TValue */
  if (r == 0) {  /* handle zero as a special case */
    setpvalue(&kv, fs);  /* use FuncState as index */
    return k2proto(fs, &kv, &o);  /* cannot collide */
  }
  else {
    const int nbm = l_floatatt(MANT_DIG);
    const lua_Number q = l_mathop(ldexp)(l_mathop(1.0), -nbm + 1);
    const lua_Number k =  r * (1 + q);  /* key */
    lua_Integer ik;
    setfltvalue(&kv, k);  /* key as a TValue */
    if (!luaV_flttointeger(k, &ik, F2Ieq)) {  /* not an integer value? */
      int n = k2proto(fs, &kv, &o);  /* use key */
      if (luaV_rawequalobj(&fs->f->k[n], &o))  /* correct value? */
        return n;
    }
    /* else, ... do not try to reuse constant; instead, create a new one */
    return addk(fs, fs->f, &o);
  }
}
```

思路:把浮点数 `r` 乘以 `1 + 2^-52`(double 的最小精度),得到一个和 `r` 数值上"几乎相等但不塌缩成整数"的键 `k`。这个键用来查 `kcache`,实际存的值还是原始 `r`。如果键恰好塌缩成整数(说明 `r` 本身就是整数级的大数,可能和整数常量碰撞),就放弃去重,直接 `addk` 加一条新的(最坏只是浪费一个表项,不影响正确性)。`r == 0` 单独处理,用 `FuncState` 指针作为 lightuserdata 键,完全不会碰撞。

布尔和 nil 也类似:`boolT`/`boolF`(`lcode.c:655`/`645`)、`nilK`(`lcode.c:665`)。注意 nil 不能作 Table 键,所以 `nilK` 用 `kcache` 表自身作为键(`sethvalue(..., &k, fs->kcache)`)。

常量索引装不下时怎么办?普通指令的 Bx 操作数只有 17 位(`MAXARG_Bx`),如果常量表超过这个大小,`luaK_codek`(`lcode.c:461`)走 `LOADKX` + `EXTRAARG` 的扩展路径:

```c
static int luaK_codek (FuncState *fs, int reg, int k) {
  if (k <= MAXARG_Bx)
    return luaK_codeABx(fs, OP_LOADK, reg, k);
  else {
    int p = luaK_codeABx(fs, OP_LOADKX, reg, 0);
    codeextraarg(fs, k);
    return p;
  }
}
```

`EXTRAARG` 是 25 位的 Ax 格式(`lopcodes.h:25`),所以常量表最大可以到 `MAXARG_Ax = 2^25 - 1`(超过一百万元素,实际上几乎不可能)。

但更常见的是,常量并不通过 `LOADK` 加载,而是直接编进运算指令——这就是 5.5 的"常量变体"指令族(`ADDK`/`SUBK`/`MULK`/`BANDK` 等,见 2.5)。

### 2.5 指令发射:32 位的六种格式

Lua 的每条指令是一个 32 位无符号整数(`Instruction` 类型)。低 7 位是操作码,剩下 25 位按操作码的"参数模式"分成不同布局。`lopcodes.h:14` 给出六种格式:

```
        3 3 2 2 2 2 2 2 2 2 2 2 1 1 1 1 1 1 1 1 1 1 0 0 0 0 0 0 0 0 0 0
        1 0 9 8 7 6 5 4 3 2 1 0 9 8 7 6 5 4 3 2 1 0 9 8 7 6 5 4 3 2 1 0
iABC          C(8)     |      B(8)     |k|     A(8)      |   Op(7)     |
ivABC         vC(10)     |     vB(6)   |k|     A(8)      |   Op(7)     |
iABx                Bx(17)               |     A(8)      |   Op(7)     |
iAsBx              sBx (signed)(17)      |     A(8)      |   Op(7)     |
iAx                           Ax(25)                     |   Op(7)     |
isJ                           sJ (signed)(25)            |   Op(7)     |
```

`enum OpMode {iABC, ivABC, iABx, iAsBx, iAx, isJ};`(`lopcodes.h:36`)。每种模式对应一个发射函数(`lcode.c`):

```c
int luaK_codeABCk (FuncState *fs, OpCode o, int A, int B, int C, int k) {
  lua_assert(getOpMode(o) == iABC);
  lua_assert(A <= MAXARG_A && B <= MAXARG_B &&
             C <= MAXARG_C && (k & ~1) == 0);
  return luaK_code(fs, CREATE_ABCk(o, A, B, C, k));
}

int luaK_codevABCk (FuncState *fs, OpCode o, int A, int B, int C, int k) {
  lua_assert(getOpMode(o) == ivABC);
  lua_assert(A <= MAXARG_A && B <= MAXARG_vB &&
             C <= MAXARG_vC && (k & ~1) == 0);
  return luaK_code(fs, CREATE_vABCk(o, A, B, C, k));
}

int luaK_codeABx (FuncState *fs, OpCode o, int A, int Bc) {
  lua_assert(getOpMode(o) == iABx);
  lua_assert(A <= MAXARG_A && Bc <= MAXARG_Bx);
  return luaK_code(fs, CREATE_ABx(o, A, Bc));
}

static int codeAsBx (FuncState *fs, OpCode o, int A, int Bc) {
  int b = Bc + OFFSET_sBx;
  lua_assert(getOpMode(o) == iAsBx);
  lua_assert(A <= MAXARG_A && b <= MAXARG_Bx);
  return luaK_code(fs, CREATE_ABx(o, A, b));
}

static int codesJ (FuncState *fs, OpCode o, int sj, int k) {
  int j = sj + OFFSET_sJ;
  lua_assert(getOpMode(o) == isJ);
  lua_assert(j <= MAXARG_sJ && (k & ~1) == 0);
  return luaK_code(fs, CREATE_sJ(o, j, k));
}
```

注意几点:

1. **`iABC` 和 `ivABC` 各有一个 k 位**(位于 A 和 B 之间,`POS_k`)。这个 k 位是 5.4 引入、5.5 沿用的关键:它让一条指令的某个操作数既可以是寄存器号(R),也可以是常量表索引(K),由 k 位区分。这就是为什么 `ADDK` 和 `ADD` 其实是同一种运算,只是 k 位不同——但 5.5 为了元方法处理方便,把它们拆成了两个独立操作码(`OP_ADD`/`OP_ADDK`,见 `lopcodes.h:263`/`278`)。`ivABC` 是 5.5 新增的变体格式,把 B 收窄到 6 位、C 扩展到 10 位,给 `NEWTABLE`/`SETLIST` 这种需要更大数组尺寸的指令用。
2. **有符号操作数用 excess-K 编码**:`sBx`、`sC`、`sJ` 都是无符号值减去一个偏移得到真实值。`OFFSET_sBx = MAXARG_Bx >> 1`(`lopcodes.h:88`)、`OFFSET_sJ = MAXARG_sJ >> 1`(`lopcodes.h:103`)。所以 `JMP` 的 `sJ` 字段能表示 `[-OFFSET_sJ, MAXARG_sJ - OFFSET_sJ]` 范围的相对跳转偏移,大约正负一千六百万条指令——Lua 函数永远到不了这么长。
3. **`codeAsBx` 在内部复用 `CREATE_ABx`**:`sBx` 和 `Bx` 共用同一组位,只是解释时减偏移。所以发射时也是 `CREATE_ABx(o, A, b + OFFSET_sBx)`。

所有发射函数最终汇到 `luaK_code`(`lcode.c:384`):

```c
int luaK_code (FuncState *fs, Instruction i) {
  Proto *f = fs->f;
  luaM_growvector(fs->ls->L, f->code, fs->pc, f->sizecode, Instruction,
                  INT_MAX, "opcodes");
  f->code[fs->pc++] = i;
  savelineinfo(fs, f, fs->ls->lastline);
  return fs->pc - 1;  /* index of new instruction */
}
```

它把指令追加到 `Proto->code` 数组,记下行号信息(`savelineinfo` 做相对行号压缩,出错时能反查源码行),返回新指令的 pc。后续如果要改这条指令(例如回填跳转目标、改 TESTSET 的目标寄存器),就拿着这个 pc 操作 `f->code[pc]`。

### 2.6 跳转与回填:JMP 的占位与跳转链

控制流(if/while/and/or)的核心是跳转。Lua 的跳转指令只有一种:`OP_JMP`(`lopcodes.h:305`),格式 `isJ`,带一个 25 位有符号偏移 `sJ`。

但编译时,跳转目标往往是**还没生成**的代码位置(前向跳转,如 `if cond then ... end` 跳过 then 块到 end 之后)。Lua 的处理是**先占位,后回填**。

`luaK_jump`(`lcode.c:200`)发射一条 JMP 占位:

```c
int luaK_jump (FuncState *fs) {
  return codesJ(fs, OP_JMP, NO_JUMP, 0);
}
```

`NO_JUMP`(`lcode.h:20`)是 `-1`,作为"目标未知"的占位。注意 `codesJ` 里 `j = sj + OFFSET_sJ`,所以 `-1` 编进 `sJ` 字段后是一个特定的无符号值;`fixjump` 解读时能识别它。

等到目标位置确定了,`luaK_patchlist`(`lcode.c:308`)/`luaK_patchtohere`(`lcode.c:314`)负责回填:

```c
void luaK_patchlist (FuncState *fs, int list, int target) {
  lua_assert(target <= fs->pc);
  patchlistaux(fs, list, target, NO_REG, target);
}

void luaK_patchtohere (FuncState *fs, int list) {
  int hr = luaK_getlabel(fs);  /* mark "here" as a jump target */
  luaK_patchlist(fs, list, hr);
}
```

`luaK_getlabel`(`lcode.c:234`)把当前 pc 标记成一个跳转目标(更新 `fs->lasttarget`,这样后续的 peephole 优化不会跨越基本块边界误合并):

```c
int luaK_getlabel (FuncState *fs) {
  fs->lasttarget = fs->pc;
  return fs->pc;
}
```

真正写跳转字段的是 `fixjump`(`lcode.c:168`):

```c
static void fixjump (FuncState *fs, int pc, int dest) {
  Instruction *jmp = &fs->f->code[pc];
  int offset = dest - (pc + 1);
  lua_assert(dest != NO_JUMP);
  if (!(-OFFSET_sJ <= offset && offset <= MAXARG_sJ - OFFSET_sJ))
    luaX_syntaxerror(fs->ls, "control structure too long");
  lua_assert(GET_OPCODE(*jmp) == OP_JMP);
  SETARG_sJ(*jmp, offset);
}
```

偏移是相对于"JMP 的下一条指令"的(`dest - (pc + 1)`),所以 `pc += sJ` 这种 VM 执行语义成立(VM 取指后 pc 已经自增)。偏移超范围就报 "control structure too long"。

> **5.5 vs 老资料差异(重要)**:讲 5.3/5.4 的资料会说跳转用 `sBx` 字段、`OP_JMP` 是 `iAsBx` 格式。5.5 把 JMP 改成了独立的 `isJ` 格式,字段是 `sJ`(25 位有符号),不再占 A 字段。老资料里 `GETARG_sBx` 解读 JMP 的写法在 5.5 已经过时,要用 `GETARG_sJ`。这是 5.5 指令格式的一个硬变化,直接影响了跳转链的实现。

**跳转链**是 Lua 处理"多个跳转指向同一目标"的精巧设计。考虑 `if a and b then ... end`:`a` 为假要跳过 then 块,`b` 为假也要跳过 then 块。这两个跳转最终都指向 end 之后。如果每个跳转都单独回填一次目标,没问题,但 Lua 用了更省的方式——**把所有指向同一目标的跳转串成链表**,链表的"next"指针就藏在跳转指令自身的 `sJ` 字段里。

具体看 `getjump`(`lcode.c:155`):

```c
static int getjump (FuncState *fs, int pc) {
  int offset = GETARG_sJ(fs->f->code[pc]);
  if (offset == NO_JUMP)  /* point to itself represents end of list */
    return NO_JUMP;  /* end of list */
  else
    return (pc+1)+offset;  /* turn offset into absolute position */
}
```

一条已经回填目标的 JMP,`sJ` 是真实偏移,`getjump` 返回它的目标。但一条**还没回填目标、在链表里**的 JMP,`sJ` 编码的是"链表中下一条 JMP 的位置"——用 `(pc+1)+offset` 算出来。这样从链头出发,顺着每条 JMP 的 `sJ` 字段,就能遍历整条链。链尾的标志是 `sJ == NO_JUMP`(`getjump` 返回 `NO_JUMP`)。

`luaK_concat`(`lcode.c:182`)把两条链拼起来:

```c
void luaK_concat (FuncState *fs, int *l1, int l2) {
  if (l2 == NO_JUMP) return;
  else if (*l1 == NO_JUMP)
    *l1 = l2;
  else {
    int list = *l1;
    int next;
    while ((next = getjump(fs, list)) != NO_JUMP)  /* find last element */
      list = next;
    fixjump(fs, list, l2);  /* last element links to 'l2' */
  }
}
```

注意这里的妙处:拼链时,用 `fixjump` 把 l1 链尾那条 JMP 的 `sJ` 写成"指向 l2 链头"的偏移。这时 `sJ` 字段同时承担两个角色——**对已回填目标的 JMP,它是真实跳转偏移;对链表中间的 JMP,它是 next 指针**。两种角色共用一个字段,因为一条 JMP 在任一时刻要么在链里(等回填),要么已经有确定目标(不在链里),不会冲突。

等到目标确定,`patchlistaux`(`lcode.c:290`)遍历整条链,把每条 JMP 的 `sJ` 都改成真实目标偏移:

```c
static void patchlistaux (FuncState *fs, int list, int vtarget, int reg,
                          int dtarget) {
  while (list != NO_JUMP) {
    int next = getjump(fs, list);
    if (patchtestreg(fs, list, reg))
      fixjump(fs, list, vtarget);
    else
      fixjump(fs, list, dtarget);  /* jump to default target */
    list = next;
  }
}
```

`patchtestreg`(`lcode.c:261`)还有一个优化:跳转前的比较指令如果是 `TESTSET`(既要测试又要产出值),回填时可以顺便指定它的目标寄存器;如果不需要值,就把它降级成 `TEST`(只测试不产出):

```c
static int patchtestreg (FuncState *fs, int node, int reg) {
  Instruction *i = getjumpcontrol(fs, node);
  if (GET_OPCODE(*i) != OP_TESTSET)
    return 0;
  if (reg != NO_REG && reg != GETARG_B(*i))
    SETARG_A(*i, reg);
  else {
    *i = CREATE_ABCk(OP_TEST, GETARG_B(*i), 0, 0, GETARG_k(*i));
  }
  return 1;
}
```

`getjumpcontrol`(`lcode.c:245`)处理"测试指令 + JMP"的配对:Lua 的比较(`EQ`/`LT`/`TEST` 等)后面一定紧跟一条 JMP,作为"条件不满足则跳过":

```c
static Instruction *getjumpcontrol (FuncState *fs, int pc) {
  Instruction *pi = &fs->f->code[pc];
  if (pc >= 1 && testTMode(GET_OPCODE(*(pi-1))))
    return pi-1;
  else
    return pi;
}
```

`testTMode`(`lopcodes.h:429`)检查操作码的 opmode 表里 T 位是否置位——置位的指令(所有比较和测试)后面必须跟一条 JMP。opmode 表(`lopcodes.c:22`)里 `OP_EQ`/`OP_LT`/`OP_LE`/`OP_EQK`/`OP_EQI`/`OP_LTI`/`OP_LEI`/`OP_GTI`/`OP_GEI`/`OP_TEST`/`OP_TESTSET` 的 T 位都是 1。

### 2.7 表达式优化举例

把上面的机制串起来,看几个真实的表达式怎么生成代码。

**例 1:局部赋值 `local a = b + c`**

`b`、`c` 都是局部变量,假设 `b` 在 `R0`、`c` 在 `R1`。`local a` 声明一个新局部,它会占 `R2`(下一个寄存器)。表达式 `b + c` 的求值(`codebinexpval`,`lcode.c:1506`):

```c
static void codebinexpval (FuncState *fs, BinOpr opr,
                           expdesc *e1, expdesc *e2, int line) {
  OpCode op = binopr2op(opr, OPR_ADD, OP_ADD);
  int v2 = luaK_exp2anyreg(fs, e2);  /* make sure 'e2' is in a register */
  ...
  finishbinexpval(fs, e1, e2, op, v2, 0, line, OP_MMBIN, binopr2TM(opr));
}
```

`binopr2op`(`lcode.c:1442`)利用枚举顺序的技巧:算术运算符 `OPR_ADD..OPR_SHR` 和算术指令 `OP_ADD..OPR_SHR` 顺序对齐,所以 `OP_ADD + (OPR_SUB - OPR_ADD) = OP_SUB`。这种"ORDER OPR - ORDER OP"的对齐贯穿整个文件(`lcode.h:23` 的注释提醒改枚举时要 grep)。

`finishbinexpval`(`lcode.c:1488`)是二元运算的统一收尾:

```c
static void finishbinexpval (FuncState *fs, expdesc *e1, expdesc *e2,
                             OpCode op, int v2, int flip, int line,
                             OpCode mmop, TMS event) {
  int v1 = luaK_exp2anyreg(fs, e1);
  int pc = luaK_codeABCk(fs, op, 0, v1, v2, 0);
  freeexps(fs, e1, e2);
  e1->u.info = pc;
  e1->k = VRELOC;  /* all those operations are relocatable */
  luaK_fixline(fs, line);
  luaK_codeABCk(fs, mmop, v1, v2, cast_int(event), flip);  /* metamethod */
  luaK_fixline(fs, line);
}
```

注意它生成**两条**指令:一条 `ADD`(结果待定寄存器,先填 0),紧跟一条 `MMBIN`。`MMBIN` 是元方法兜底:VM 执行 `ADD` 时,如果操作数没有 `__add` 元方法,直接算完跳过 `MMBIN`;如果有,则执行 `MMBIN` 调元方法。这样正常情况(无元方法)只多取一条无害的 `MMBIN` 指令,异常情况(有元方法)不用复杂的回退逻辑。`flip` 参数标记操作数是否交换过(因为 `a - b` 编成 `a + (-b)` 时,元方法需要原始顺序)。

回到 `local a = b + c`:生成的指令大致是

```
ADD   R2 R0 R1     -- R2 = R0 + R1
MMBIN R0 R1 TM_ADD -- 若有 __add 则调用
```

`ADD` 的 A 操作数原本填 0,等到 `local a` 把表达式落到 `R2` 时(`exp2reg` 的 `VRELOC` 分支),`SETARG_A` 把它改成 2。所以最终就是 `ADD R2 R0 R1`。结果直接落在 `a` 的寄存器,没有 `MOVE`。

**例 2:常量运算 `local a = b + 2`**

`2` 是整数常量。`codecommutative`(`lcode.c:1598`)看到第二操作数是小整数常量,直接用立即数变体 `ADDI`:

```c
static void codecommutative (FuncState *fs, BinOpr op,
                             expdesc *e1, expdesc *e2, int line) {
  int flip = 0;
  if (tonumeral(e1, NULL)) {  /* is first operand a numeric constant? */
    swapexps(e1, e2);  /* change order */
    flip = 1;
  }
  if (op == OPR_ADD && isSCint(e2))  /* immediate operand? */
    codebini(fs, OP_ADDI, e1, e2, flip, line, TM_ADD);
  else
    codearith(fs, op, e1, e2, flip, line);
}
```

`isSCint`(`lcode.c:1291`)检查常量能否装进 `sC`(8 位有符号,范围约 `[-128, 127]`)。能装下就用 `ADDI`,常量直接编进指令的 C 字段,**不进常量表、不需要 LOADK**:

```
ADDI  R_a R_b 2    -- R_a = R_b + 2
MMBINI R_b 2 TM_ADD 0
```

如果常量超出 `sC` 范围但能进常量表(`codearith` -> `codebinK`,`lcode.c:1533`),用 `ADDK`,常量在常量表里占一项,但仍然一条指令搞定运算:

```
ADDK  R_a R_b Kindex
MMBINK R_b Kindex TM_ADD 0
```

这三种变体(`ADD`/`ADDI`/`ADDK`)分别对应"两个寄存器"、"寄存器+小立即数"、"寄存器+常量索引"。**老资料(讲 5.3)通常只讲 `ADD` 一种**,5.4/5.5 把常量运算专门拆出 `*I`/`*K` 变体,显著减少了 `LOADK` 的发射次数——这是 5.5 字节码比老版本更紧凑的直接原因之一。

**例 3:比较 `if a == b then ... end`**

`codeeq`(`lcode.c:1666`)处理相等比较。如果 `b` 是小数字常量,用 `EQI`(立即数比较);如果 `b` 是常量表项,用 `EQK`;否则用通用 `EQ`:

```c
static void codeeq (FuncState *fs, BinOpr opr, expdesc *e1, expdesc *e2) {
  ...
  r1 = luaK_exp2anyreg(fs, e1);  /* 1st expression must be in register */
  if (isSCnumber(e2, &im, &isfloat)) {
    op = OP_EQI;
    r2 = im;  /* immediate operand */
  }
  else if (exp2RK(fs, e2)) {  /* 2nd expression is constant? */
    op = OP_EQK;
    r2 = e2->u.info;  /* constant index */
  }
  else {
    op = OP_EQ;  /* will compare two registers */
    r2 = luaK_exp2anyreg(fs, e2);
  }
  freeexps(fs, e1, e2);
  e1->u.info = condjump(fs, op, r1, r2, isfloat, (opr == OPR_EQ));
  e1->k = VJMP;
}
```

`condjump`(`lcode.c:224`)生成"比较 + JMP"的组合:

```c
static int condjump (FuncState *fs, OpCode op, int A, int B, int C, int k) {
  luaK_codeABCk(fs, op, A, B, C, k);
  return luaK_jump(fs);
}
```

所以 `if a == b then ... end` 大致生成:

```
EQ    R_a R_b 1    -- if (a == b) ~= 1 then pc++  (即不等则跳过下一条JMP)
JMP   ...           -- 跳过 then 块(目标待回填)
... then 块 ...
```

`EQ` 的 k 位是 1(条件取真分支),操作语义是"如果 `(R[A]==R[B]) != k` 则 pc++"——也就是"不等则跳过下一条指令"。下一条是 JMP,跳过它就进入 then 块;相等则执行 JMP,跳过 then 块。这种"比较 + JMP"的配对让布尔比较的结果**根本不进寄存器**,直接驱动控制流。

**例 4:短路求值 `local a = b or c`**

`and`/`or` 走的是另一条路(`luaK_posfix` 的 `OPR_OR` 分支,`lcode.c:1799`):

```c
case OPR_OR: {
  lua_assert(e1->f == NO_JUMP);
  luaK_concat(fs, &e2->t, e1->t);
  *e1 = *e2;
  break;
}
```

短路求值的核心是:`b or c` 中,如果 `b` 为真,直接取 `b`,不计算 `c`;如果 `b` 为假,取 `c`。这在 `luaK_infix`(`lcode.c:1718`)里前置处理:

```c
case OPR_OR: {
  luaK_goiffalse(fs, v);  /* go ahead only if 'v' is false */
  break;
}
```

`luaK_goiffalse`(`lcode.c:1205`)给 `b` 生成一条"如果为真则跳转"的指令,把跳转挂到 `e->t`(true list);为假则继续往下算 `c`。最终 `e1` 的 `t` 链上挂着"b 为真时应跳到的位置",回填时这些跳转指向"短路结果就是 b 的值"的地方。整个过程**不生成任何把布尔值装进寄存器的指令**,纯靠跳转链。这是 `expdesc` 的 `t`/`f` 跳转链最典型的应用。

### 2.8 函数收尾 luaK_finish 与 maxstacksize

函数体编译完,`luaK_finish`(`lcode.c:1929`)做最后一遍扫描:

```c
void luaK_finish (FuncState *fs) {
  int i;
  Proto *p = fs->f;
  if (p->flag & PF_VATAB)  /* will it use a vararg table? */
    p->flag &= cast_byte(~PF_VAHID);  /* then it will not use hidden args. */
  for (i = 0; i < fs->pc; i++) {
    Instruction *pc = &p->code[i];
    ...
    switch (GET_OPCODE(*pc)) {
      case OP_RETURN0: case OP_RETURN1: {
        if (!(fs->needclose || (p->flag & PF_VAHID)))
          break;
        SET_OPCODE(*pc, OP_RETURN);
      }  /* FALLTHROUGH */
      case OP_RETURN: case OP_TAILCALL: {
        if (fs->needclose)
          SETARG_k(*pc, 1);  /* signal that it needs to close */
        if (p->flag & PF_VAHID)
          SETARG_C(*pc, p->numparams + 1);
        break;
      }
      case OP_GETVARG: {
        if (p->flag & PF_VATAB)
          SET_OPCODE(*pc, OP_GETTABLE);
        break;
      }
      case OP_VARARG: {
        if (p->flag & PF_VATAB)
          SETARG_k(*pc, 1);
        break;
      }
      case OP_JMP: {  /* to optimize jumps to jumps */
        int target = finaltarget(p->code, i);
        fixjump(fs, i, target);
        break;
      }
      default: break;
    }
  }
}
```

它做的事:

1. **跳转链全部回填**:每个 `OP_JMP` 都通过 `finaltarget`(`lcode.c:1911`)找到最终目标(跳过"跳转到跳转"的中间跳),再 `fixjump` 回填。`finaltarget` 限制了 100 次跳转链追踪(`for (count = 0; count < 100; count++)`)以防死循环。
2. **RETURN 系列指令的修补**:如果函数需要关闭 upvalue(`fs->needclose`,例如有 to-be-closed 变量或逃逸的 upvalue),把 `RETURN0`/`RETURN1` 升级成通用的 `RETURN`,并设 k 位告诉 VM "返回前要调 `OP_CLOSE`"。如果函数有隐藏的可变参数(`PF_VAHID`),设 C 操作数告诉 VM 修正 `func` 指针。
3. **可变参数表机制**(`PF_VATAB`):5.5 新引入。如果函数把可变参数存进了一张表(而不是直接用隐藏参数),把 `GETVARG` 改写成 `GETTABLE`、给 `VARARG` 设 k 位。这是 5.5 处理可变参数的新方式,详见 P4-15。

`maxstacksize` 的确定贯穿整个编译过程,不只在 `luaK_finish`。每次 `luaK_reserveregs` -> `luaK_checkstack`(`lcode.c:476`)都会更新 `fs->f->maxstacksize` 为"到目前为止用到的最大寄存器数"。编译结束时,这个值就是函数需要的栈大小。它存在 `Proto->maxstacksize`(`lobject.h:606`,`lu_byte` 类型,最大 255):

```c
lu_byte maxstacksize;  /* number of registers needed by this function */
```

VM 调用这个函数时(`luaD_call`,见 P4-13),根据 `maxstacksize` 一次性分配够用的栈槽位,运行期间不必再动态扩。这是**编译器指导 VM** 的关键信息:编译器算出"这个函数最多用几个寄存器",VM 据此预分配,避免运行时频繁检查栈溢出。

---

## 三、为什么这样设计是 sound 的

讲完实现,回到设计动机。Lua 的代码生成有三个非平凡的选择,每个都有明确的"为什么"。

### 3.1 线性寄存器分配为什么够

教科书式的寄存器分配(图着色、线性扫描)需要做活跃变量分析,复杂度高、编译慢。Lua 完全不做,只用一个 `freereg` 计数器加 LIFO 退栈。这能工作,是因为 Lua 的表达式语义有一个强约束:**子表达式的值只在一个父表达式求值期间需要,求值完即弃**。

考虑 `a + b * c`。语法树是 `+(a, *(b, c))`。后序遍历:先算 `b * c`(占一个临时寄存器),再算 `a + 临时`(再占一个),完成后释放。整棵树的临时寄存器占用是一串严格嵌套的 push/pop,对应一个栈——这正是 `freereg` 线性退栈所模拟的。

Lua 没有"一个表达式的值要在多个不相邻的地方同时存活"的场景(那种才需要图着色)。变量要么是命名的局部(占固定的低寄存器,生命周期由作用域管),要么是匿名的临时(严格嵌套,可线性退栈)。两类互不干扰(`freereg` 判断 `reg >= luaY_nvarstack` 区分)。所以线性分配对 Lua 的语义是 sound 的:它不会让两个活着的临时值抢同一个寄存器。

### 3.2 延迟表示 expdesc 为什么省

延迟表示的核心收益是**消费侧优化**。一个表达式在被消费之前,不发射任何指令,只记录"它现在是什么形态"。消费者拿到这张欠条,可以选择最优的兑现方式:

- `b + 2` 里,`2` 先保持 `VKINT` 形态。`codecommutative` 看到它是小整数,直接编进 `ADDI`,省掉一条 `LOADK`。
- `a > 0` 里,`0` 先保持 `VKINT`,`codeorder` 看到它是小数,直接编进 `GTI`,省掉 `LOADI`。
- `t.x` 里,下标访问先保持 `VINDEXSTR`,等到要值时才发射 `GETFIELD`,而 `GETFIELD` 直接用常量表里的短字符串索引,省掉 `LOADK` + `GETTABLE`。
- `a == b then ...` 里,比较结果保持 `VJMP`(一条比较 + 一条 JMP),根本不落进寄存器,因为 `if` 只关心控制流,不要布尔值。
- `b or c` 里,短路求值用 `expdesc` 的 `t`/`f` 跳转链,纯靠跳转实现"取 b 或 c",不生成任何布尔装载指令。

如果用急切求值(每个表达式一算完就落进寄存器),这些优化全没了:`b + 2` 要先 `LOADI 2`、`a > 0` 要先 `LOADI 0`、`t.x` 要先 `LOADK "x"`、比较要先落布尔。延迟表示让 Lua 在每种具体语境下选最省的指令序列,这是它的字节码紧凑的根本原因之一。

### 3.3 跳转链为什么 sound

跳转链的精妙在于**一个 `sJ` 字段同时扮演"目标偏移"和"链表 next"两个角色**。这能工作,因为一条 JMP 在任一时刻只处于两种状态之一:

- **在链里**:还没回填目标。`sJ` 编码的是链中下一条 JMP 的位置。
- **已回填**:`sJ` 编码的是真实跳转目标。

两种状态互斥,所以共用一个字段不会冲突。回填过程(`patchlistaux`)遍历链表,把每条 JMP 从"在链里"状态切换到"已回填"状态,就是用真实目标偏移覆盖掉 next 指针。

sound 性的关键在 `getjump`(`lcode.c:155`)对 `NO_JUMP` 的识别:`sJ == NO_JUMP` 时返回 `NO_JUMP` 表示链尾。`NO_JUMP = -1`(`lcode.h:20`),编进 `sJ` 后是一个特定的无符号值,刚好也是"指向自己"的无效地址(注释 `lcode.h:18` 说它作为绝对地址和链表自环都无效)。所以遍历链表不会无限循环,也不会误把已回填的 JMP 当成链中节点。

`finaltarget` 在 `luaK_finish` 里还做了"跳转到跳转"的优化:如果一条 JMP 的目标又是 JMP,直接跳到最终目标,省掉中间一跳。这进一步压缩了跳转的执行开销。

---

## 四、★对照 CPython:栈式不做分配 vs 寄存器线性分配

CPython 是 Lua 最直接的对照对象。在代码生成这一层,两者走的是完全不同的路。

**CPython 的编译器**(`compile.c`/`ceval.c`)生成的是**栈式字节码**。它的代码生成几乎不做寄存器分配——因为根本没有寄存器。每个值要么在值栈顶,要么在一个具名槽位(`LOAD_FAST`/`STORE_FAST` 操作的局部变量数组,有点像 Lua 的局部变量寄存器,但语义不同)。表达式的求值就是一串压栈弹栈:

以 `local a = b + c`(对应 Python `a = b + c`)为例,CPython 3.11+ 大致生成:

```
LOAD_FAST   b       ; 把 b 压栈
LOAD_FAST   c       ; 把 c 压栈
BINARY_OP   +       ; 弹出 c、b,相加,结果压栈
STORE_FAST  a       ; 弹出存入 a
```

每条 `LOAD_FAST` 都是一次压栈,`BINARY_OP` 弹两个压一个,`STORE_FAST` 弹一个。中间值全走栈,没有"这个临时值放哪个寄存器"的问题——编译器省了事,但指令数量上去了。

Lua 的版本(假设 `b` 在 R0、`c` 在 R1、`a` 落 R2):

```
ADD   R2 R0 R1     ; R2 = R0 + R1,一条指令
MMBIN R0 R1 TM_ADD
```

(如果其中一个是常量,还能用 `ADDI`/`ADDK` 更省。)同样的逻辑,Lua 两条核心指令(算 + 元方法兜底),CPython 四条。差距来自:**寄存器式把操作数直接编码进指令,省掉了栈式来回搬运的 `LOAD`/`STORE`**。

两者的设计取舍一目了然:

| 维度 | Lua 5.5 | CPython |
|---|---|---|
| **指令模型** | 寄存器式,操作数是寄存器号 | 栈式,操作数隐含在栈顶 |
| **代码生成** | 线性寄存器分配(`freereg` LIFO)+ 延迟表示(`expdesc`) | 几乎不做分配,直接压栈弹栈 |
| **临时值** | 占临时寄存器,用完退栈 | 压值栈,用完弹出 |
| **常量处理** | 常量表去重(`kcache` 哈希)+ `*K`/`*I` 变体指令直接编码 | 常量表(`LOAD_CONST`)+ 栈 |
| **比较与控制流** | `EQ`/`LT` + `JMP` 配对,布尔不落寄存器 | `COMPARE_OP` 把布尔压栈,再 `POP_JUMP_IF_*` |
| **编译器复杂度** | `lcode.c` 1971 行,线性扫描 | `compile.c` 更长,但逻辑直白(不用分析寄存器) |
| **生成的指令数** | 少(每条干更多事) | 多(每条只压/弹/算一件事) |

CPython 的栈式让编译器简单——它不需要决定"这个值放哪",压栈就完事。但代价是执行期指令多,取指/译码/分发的次数也多,每个 `LOAD`/`STORE` 都是一次开销。Lua 反过来:编译期付一次寄存器分配的代价(`lcode.c` 那 1971 行),执行期一直省。对于一个目标是"嵌入宿主、被频繁调用"的语言,执行期的省比编译期的省更重要——脚本一旦加载,字节码可能跑成千上万次,而编译只发生一次。

更深一层,Lua 的延迟表示(`expdesc`)让它的代码生成**比 CPython 更会优化**。CPython 的编译器相对"老实":看到 `b + 2`,老老实实 `LOAD_CONST 2` 再 `BINARY_OP`。Lua 看到 `b + 2`,认出 `2` 是小整数常量,直接编进 `ADDI`,省掉一条 `LOADK`。这种"消费侧优化"是栈式天然做不到的——栈式必须把每个值先压上栈,没有"等消费者决定怎么用"的余地。

回扣全书主线:**统一与精简换小而快**。代码生成这一章是"精简"二字的集中展示。Lua 用线性分配(精简的分配策略)+ 延迟表示(精简的表达式描述)+ 紧凑的 32 位指令格式(精简的编码),换来了又少又紧凑的字节码。这种字节码在执行期表现为更少的取指、更少的译码、更紧凑的代码缓存——正是 P0-01 说的"寄存器式快"的具体落地。而这一切的源头,只是 `lcode.c` 那 1971 行 C 代码,小到能完整读懂,正是 Lua 作为"最佳教学样本"的魅力所在。

---

*本章讲完了编译侧的收尾:源码怎么变成紧凑的寄存器式字节码。下一章 [P3-10 指令格式 lopcodes:32 位编码与操作码全集](P3-10-指令格式lopcodes-32位编码与操作码全集.md)从编译侧跨入执行侧,逐一展开那 32 位指令的每一种格式和每一个操作码,看 VM 怎么取指、译码、执行这些由本章发射出来的指令。*
