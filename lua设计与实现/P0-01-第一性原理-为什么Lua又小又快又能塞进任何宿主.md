# P0-01 第一性原理:为什么 Lua 又小又快又能塞进任何宿主

> **本书主线**:统一与精简换小而快。**二分法**:编译侧(源码→字节码)↔ 执行侧(VM 执行 + 管值生命周期)。**★对照**:CPython。**源码**:lua-5.5.0。**基调**:纯直球,不用比喻。

---

## 引子:这本书要讲什么

Lua 是一门用 C 写的、为嵌入而生的脚本语言。1993 年起在巴西 PUC-Rio 维护,到本书依据的 5.5.0(2025 年发布),整个官方实现的 C 源码只有 **32106 行**。它被嵌进过游戏引擎(魔兽世界、愤怒的小鸟)、基础设施(Redis、nginx、Wireshark)、桌面应用(Lightroom)和各种嵌入式设备。

这本书不讲 Lua 语法,也不教怎么用 Lua 写脚本。它回答一个问题:

**一门能塞进任何宿主、又快得不像脚本的虚拟机,内核到底是怎么搭起来的。**

要回答它,得先看清 Lua 到底在解决一个什么矛盾。

---

## 一、Lua 解决的核心张力:小 ↔ 全/快

脚本语言通常面临一个两难。

一种走法是大而全,以 Python、Ruby 为代表:语言能力强、标准库丰富,但运行时庞大。要嵌入一个宿主,得带上整套解释器、类型系统、标准库和一堆依赖,宿主为"拥有脚本能力"付出很高的体积和复杂度代价。

另一种走法是小而弱,以各种配置用的领域语言为代表:容易嵌入,但语言能力贫乏,写不出像样的逻辑。

Lua 的选择很特别。它要同时满足两个看起来对立的目标:

- **小**:内核极简,内存占用低,对外依赖几乎为零;一个 `lua_State` 结构就是一个完整且互相隔离的 VM 实例,宿主链接几个 `.c` 文件、调一个函数就能用上。
- **全/快**:它是一门完整的语言——有闭包、协程、元表、垃圾回收;而且相当快,在纯解释执行的层面,性能已经接近手写 C 的循环。

小和快/全通常打架:功能多就大,要快就得为每种情况写专用代码(又变大)。Lua 却用一套统一的设计把这对矛盾同时化解。这套设计浓缩成一句话,也是贯穿全书的主线:

> **用"统一"和"精简",同时拿下"小"和"快"。**

具体是三招。下面用 lua-5.5.0 的源码逐一落地。

---

## 二、第一招·统一:一切复合数据都是 Table

先看 Lua 唯一的复合数据结构 `Table`,定义在 `lobject.h:776`:

```c
typedef struct Table {
  CommonHeader;
  lu_byte flags;       /* 1<<p means tagmethod(p) is not present */
  lu_byte lsizenode;   /* log2 of number of slots of 'node' array */
  unsigned int asize;  /* number of slots in 'array' array */
  Value *array;        /* array part */
  Node *node;          /* hash part */
  struct Table *metatable;
  GCObject *gclist;
} Table;
```

注意 `array` 和 `node` 并存在同一个结构里:前者是数组部分,后者是哈希部分。这就是"统一"的根基——一个 `Table` 同时承担:

- **数组/列表**:连续的整数键 `1,2,3...` 走 `array` 部分,直接下标访问,O(1)。
- **哈希/字典**:离散的或字符串的键走 `node` 部分。
- **对象**:Lua 没有类,面向对象靠 `Table` + `metatable` 实现(详见 P6-19)。
- **全局环境/模块**:Lua 的全局变量表、模块、注册表,本质都是 `Table`。

换句话说,Lua 砍掉了"为每种复合数据各造一个类型"的复杂度。源码里没有 `list`、`dict`、`tuple`、`set` 分立——只有 `Table` 一个。一套创建、遍历、扩容、GC 的代码,服务所有复合数据。这正是"小"的根源之一:数据结构层面就少了一大半代码。

而它并没有因此变慢:整数键命中 `array` 部分时,访问代价和一个 C 数组完全一样。统一的代价(都可能落进哈希)只发生在离散键上,且哈希部分本身设计得相当精细(P1-05 详讲)。

---

## 三、第二招·精简①:寄存器式字节码

Lua 的字节码是**寄存器式**的,不是 Python、JVM 那种**栈式**的。区别在于操作数怎么给:

- 栈式:指令没有显式操作数,靠往一个共享栈上压、取。要做 `a + b`,得先把 `a`、`b` 压栈,再发一条加法指令从栈顶取两个 operand、把结果压回栈。
- 寄存器式:指令的操作数直接是"寄存器号"。要做 `a + b`,一条指令说"`R_c = R_a + R_b`"就完事,中间不经过栈来回。

Lua 里每个函数有一组自己的寄存器(其实就是它那段值栈的槽位,P4-13 详讲)。看一个具体对照。Lua 代码:

```lua
local a = 1
local b = a + 2
```

Lua 5.5 大致编译成(操作码名见 `lopnames.h`,精确反编译留到 P3-12):

```
LOADI   R0  1      -- R0 = 1          (a)
ADDI    R1  R0  2  -- R1 = R0 + 2     (b)
RETURN0
```

两条核心指令,结果直接落在寄存器 `R1`。而同一段代码,CPython 3.11+ 的栈式字节码大致是:

```
RESUME        0
LOAD_CONST    1        ; 压入 1
STORE_FAST    0 (a)    ; 弹出存入 a
LOAD_FAST     0 (a)    ; 压入 a
LOAD_CONST    2        ; 压入 2
BINARY_OP     0 (+)    ; 弹出两个、相加、压回
STORE_FAST    1 (b)    ; 弹出存入 b
RETURN_CONST
```

同样是"两个局部变量相加",Lua 两条、CPython 七八条。差距来自哪里?栈式必须把每个值先搬上栈、用完再搬走,每次 `LOAD`/`STORE` 都是一条指令;寄存器式直接用寄存器号指代操作数,省掉了这些搬运。

指令少,意味着**取指、译码、分发的次数都少**——这是寄存器式"快"的来源。同时指令更紧凑(一条指令干更多事),字节码体积也更小——这也是"小"。

代价是编译器必须做寄存器分配(决定哪个值放哪个寄存器),但这只在编译期付一次,执行期一直省。P2-09 会讲 Lua 怎么用简单的线性分配拿下它。

---

## 四、第三招·精简②:增量式 GC,可中断

第三招看垃圾回收。Lua 的 GC 是**增量式三色标记清除**,相关的全局状态在 `global_State`(`lstate.h:327`)里:

```c
typedef struct global_State {
  lua_Alloc frealloc;     /* function to reallocate memory */
  void *ud;
  l_mem GCtotalbytes;     /* bytes allocated + debt */
  l_mem GCdebt;           /* bytes counted but not yet allocated */
  ...
  lu_byte gcstate;        /* state of garbage collector */
  lu_byte gckind;         /* kind of GC running */
  ...
  GCObject *allgc;        /* list of all collectable objects */
  GCObject *gray;         /* list of gray objects */
  GCObject *grayagain;    /* objects to be traversed atomically */
  GCObject *weak;         /* tables with weak values */
  GCObject *ephemeron;    /* ephemeron tables (weak keys) */
  ...
} global_State;
```

这里的 `gray`/`grayagain`/`weak`/`ephemeron` 各种链表,是三色标记的工作队列;`gcstate` 是 GC 的状态机;`GCdebt` 是控制"这一步做多少 GC 工作"的债务计数。

关键是**增量**:Lua 的 GC 不是一次性把整个堆扫完(那样会卡住宿主几百毫秒),而是把一次完整的回收切成无数小步,每执行一小段字节码,就顺手做一小步 GC,由 `GCdebt` 控制每步的工作量。

这一招对"嵌入"几乎生死攸关。一个游戏引擎每帧只有 16 毫秒,绝不能接受某帧里 Lua 的 GC 停顿 200 毫秒;Redis 用 Lua 跑事务脚本,更不能在单线程里被 GC 堵住。增量让 GC 的停顿被切细、摊平到每次几微秒,宿主几乎感受不到。

可中断的代价是更复杂的状态机(三色不变式、各种再扫描队列,P5-16 详讲),但换来的是"GC 永远不成为宿主的负担"——这恰好是"适合嵌入"的核心要求。

---

## 五、一个 lua_State 就是一个 VM:嵌入性的根基

把三招放在一起看,会发现它们都落在同一个东西上:`lua_State`。Lua 的所有运行时状态——值栈、调用帧、upvalue、错误恢复点——全装在 `struct lua_State`(`lstate.h:285`)这一个结构里:

```c
struct lua_State {
  CommonHeader;
  lu_byte allowhook;
  TStatus status;
  StkIdRel top;            /* first free slot in the stack */
  struct global_State *l_G;
  CallInfo *ci;            /* call info for current function */
  StkIdRel stack_last;
  StkIdRel stack;          /* stack base */
  UpVal *openupval;        /* list of open upvalues in this stack */
  StkIdRel tbclist;        /* list of to-be-closed variables */
  ...
  struct lua_longjmp *errorJmp;  /* current error recover point */
  CallInfo base_ci;        /* CallInfo for first level (C host) */
  ...
};
```

而所有线程共享的全局状态(内存分配器、字符串表、GC、注册表)在 `global_State` 里,由 `l_G` 指针指向。

这个布局直接决定了 Lua 的嵌入方式:

- 宿主链接几个 `.c` 文件,调 `luaL_newstate()` 就得到一个全新、独立的 VM 实例——上面这个结构体就是它的全部。
- 可以在一个进程里创建多个 `lua_State`:它们可以共享同一个 `global_State`(共用字符串驻留表和 GC),但各自的值栈 `stack`、调用链 `ci` 完全隔离。这是"在一个进程里跑多个互不干扰的 Lua 世界"的能力,也是协程(P6-20)能够基于"切栈"实现的物理基础。

顺带记一个 5.5 的演进点:上面 `lua_State` 里的栈指针是 `StkIdRel` 类型——**相对表示**,而不是老版本(5.3/5.4)资料里写的 `StkId`(也就是 `TValue *` 绝对指针)。改成相对表示后,值栈扩容 `realloc` 搬家时,不必逐个更新所有指向栈的指针。这是 5.5 相对老资料的一个硬差异,细节留到 P1-02。

---

## 六、为什么这三招能同时拿下小和快

把三招放在一起,关键在于它们不是各管一头,而是相互成全:

- **统一的 Table**:一套数据结构代码服务所有复合数据 → 内核代码量小(小);同时整数键走数组部分 O(1) → 访问快(快)。
- **寄存器式字节码**:指令少 → 取指/译码少 → 执行快(快);指令紧凑 → 字节码体积小(小)。
- **增量 GC**:可中断 → 不卡宿主 → 适合嵌入(小/友好);按需推进 → 不必每次全停顿(快/平滑)。

三招的共同指向是同一件事:**砍掉一切可砍的机制,让剩下每一种机制都同时服务多个目的。** Table 既是数组又是哈希又是对象;一条 `ADDI` 既取操作数又算又存结果;增量 GC 既回收又不阻塞。这就是"用更少的机制换更多的能力",也是全书主线在源码层面的具体含义。

---

## 七、★对照 CPython

CPython 是 Lua 最直接的对照对象:同属嵌入式脚本语言 VM 这个生态位,但几乎每一个架构选择都走到 Lua 的反面。

| 维度 | Lua 5.5 | CPython |
|---|---|---|
| **数据模型** | 只有 `Table` 一个复合结构,数组/哈希/对象/模块全是它 | `list`/`dict`/`tuple`/`set`/`frozenset`/`bytes` 等多种内建类型,各自一套 C 实现 |
| **指令模型** | 寄存器式(`ADDI R1 R0 2`,操作数即寄存器) | 栈式(`LOAD_FAST`/`BINARY_OP`/`STORE_FAST`,来回压栈弹栈) |
| **GC** | 增量式三色标记,可中断、按步推进 | 引用计数为主(引用归零即回收)+ 分代标记(周期性处理环引用) |

每一个对照都揭示一种取舍:

- 数据模型上,Lua 用统一换来了源码小、学习曲线平;CPython 用专用换来了每种类型的极致优化,代价是更多代码。
- 指令模型上,Lua 寄存器式指令少、执行快,代价是编译器要做寄存器分配;CPython 栈式实现简单、编译器轻松,代价是同一段逻辑指令更多。
- GC 上,CPython 的引用计数回收非常及时(对象一不可达就回收),但每次指针赋值都有增减计数的开销,且处理循环引用需要单独的分代周期(那是一次完整停顿);Lua 的增量标记对宿主延迟更友好,但回收不那么即时,且需要更复杂的三色状态机维持正确性。

全书每一章的 ★对照 CPython 栏都会回到这张表,把 Lua 当下的每一个设计放到和 CPython 的相对位置上看清楚——这是理解"Lua 为什么这么选"最快的方式。

---

## 八、全书怎么读

本书按"编译侧 ↔ 执行侧"的二分法组织,数据是两边共用的根基:

- **数据根基(P1)**:一个 Lua 值在 C 里长什么样(`TValue`)、字符串怎么存、`Table` 怎么造。这是编译器和 VM 共用的基础语言。
- **编译侧(P2)**:源码怎么一步步变成寄存器式字节码——词法分析、语法分析、代码生成与寄存器分配。
- **执行侧(P3–P6)**:VM 怎么执行字节码(P3)、函数怎么被调起来(P4)、值的生命周期怎么由 GC 管(P5)、元表/协程/C API 怎么让 Lua 成为一门可被宿主嵌入的完整语言(P6)。

建议的读法:P0-01 到 P1 先顺序读,把"值"的表示吃透——后面所有章节都建立在这个基础上。之后编译侧(P2)和执行侧(P3)可以并行读,它们正好对应"源码怎么进来"和"字节码怎么跑出去"两条线。GC(P5)和协程/C API(P6)相对独立,可按兴趣选读。

读完全书,会得到一个清晰的结论:Lua 的每一个设计,都在实践它的主线——**用更少的机制,换更多的能力**。这正是它又小、又快、又能塞进任何宿主的根本原因,也是这本书要带你一行行看清的东西。

---

*下一章 [P1-02 lua_State 与 TValue](P1-02-lua_State与TValue-万物皆值与tagged编码.md):从一个 `TValue` 到底装了什么开始,进入 Lua 的数据根基。*
