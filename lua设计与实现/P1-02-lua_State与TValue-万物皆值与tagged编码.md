# P1-02 lua_State 与 TValue：万物皆值与 tagged 编码

> **本书主线**：统一与精简换小而快。**本章落点**:"统一"的数据根基——所有 Lua 值用同一种 C 表示(`TValue`)，所有 VM 状态装进同一个结构(`lua_State`)。它同时服务"小"(值内联，小整数/布尔/浮点不必堆分配)和"快"(访问值少一次解引用)。**二分法位置**：数据根基(P1)，编译侧和执行侧共用的基础语言。**★对照**:CPython 的 `PyObject` 堆 box + `ob_refcnt` 引用计数 + `ob_type`，对比 Lua 的内联 tagged `TValue`。**源码**:lua-5.5.0,`lobject.h` / `lstate.h` / `lstate.c` / `ldo.c`。**基调**：纯直球，不用比喻。全角中文标点。

---

## 一、这章解决什么问题

P0-01 把 Lua 的三招摆了出来：统一的 Table、寄存器式字节码、增量 GC。但要真正读懂那三招的源码，先得回答两个更底层的问题：

1. **一个 Lua 值，在 C 代码里到底长什么样？** Lua 里有 nil、布尔、整数、浮点、字符串、表、函数、userdata、协程(thread)，九种基本类型。编译器生成字节码时要往里塞字面量，VM 执行时要从一个寄存器读出值、判断它是什么类型、对它做运算，GC 要遍历所有存活值——所有这些环节，都得用同一种 C 数据结构去装值。这个结构是什么？为什么是它？

2. **整个 VM 的状态装在哪？** Lua 号称"一个 `lua_State` 就是一个完整 VM 实例"，宿主调 `luaL_newstate()` 就拿到一个隔离的世界。这个"世界"由哪些字段构成？值栈、调用链、错误恢复点、GC、字符串表、内存分配器——它们怎么塞进一个结构？多个 `lua_State` 之间什么共享、什么隔离？

这两个问题指向 Lua 设计的同一个取向：**砍掉"每个值各是一个堆对象"的常规脚本语言做法**，改成把值内联进一个固定大小的 tagged 结构。这个取向直接决定了后面 Table 怎么存、字节码怎么用寄存器、GC 怎么扫——它是全书的物理地基。

这一章把这个地基拆开。先讲值(`TValue`、`Value`、类型标签、设值取值宏)，再讲 VM 状态(`lua_State`、`global_State`、值栈、`StkIdRel`)，最后讲清楚这套设计为什么不破坏正确性、和 CPython 比取舍在哪。顺带把几个 5.5 相对老资料(讲 5.3/5.4)的硬差异点出来——尤其 `StkIdRel` 这个 5.4 引入、5.5 沿用的栈相对表示。

---

## 二、源码怎么实现

### 2.1 TValue：一个 Lua 值的物理表示

一切从 `lobject.h` 的两段定义开始。先是值的联合体 `Value`(`lobject.h:49`)：

```c
typedef union Value {
  struct GCObject *gc;    /* collectable objects */
  void *p;         /* light userdata */
  lua_CFunction f; /* light C functions */
  lua_Integer i;   /* integer numbers */
  lua_Number n;    /* float numbers */
  /* not used, but may avoid warnings for uninitialized value */
  lu_byte ub;
} Value;
```

紧跟着是 `TValue`(`lobject.h:67`)：

```c
#define TValuefields	Value value_; lu_byte tt_

typedef struct TValue {
  TValuefields;
} TValue;
```

把宏展开，`TValue` 就是两个字段：`Value value_`(值本体)和 `lu_byte tt_`(类型标签，一个字节)。`lu_byte` 是 `unsigned char`(`llimits.h:42`)，`lua_Integer` 默认是 `long long`(64 位)，`lua_Number` 默认是 `double`(64 位)——这俩默认值在 `luaconf.h` 里(`LUA_INT_DEFAULT = LUA_INT_LONGLONG`,`luaconf.h:134`;`LUA_FLOAT_DEFAULT = LUA_FLOAT_DOUBLE`,`luaconf.h:135`，对应 `LUA_INTEGER = long long`@`luaconf.h:520`、`LUA_NUMBER = double`@`luaconf.h:446`)。

为什么用 `union` 装所有类型？因为一个值在同一时刻只能是其中一种。`union` 的所有成员共享同一段内存，大小等于最大成员。在 64 位平台上，指针、`lua_Integer`、`lua_Number` 都是 8 字节，所以 `sizeof(Value) = 8`。加上一个字节的 `tt_`，再加上对齐填充，`sizeof(TValue)` 在 64 位平台上正好是 **16 字节**。

这就是 Lua 的"万物皆值":nil、`true`、`42`、`3.14`、`"hello"`、`{}`、`print`、一个协程——每一样都装在一个 16 字节的 `TValue` 里。类型靠 `tt_` 区分，值本体靠 `union Value` 承载。不存在"布尔是一种小对象、整数是另一种小对象、字符串又是一种"的多套表示——只有这一种。

`gc` 成员值得单独说一句：所有需要 GC 的对象(字符串、表、闭包、userdata、thread、proto、upvalue)在 C 里都以 `GCObject *` 指针的形式进 `Value`。`GCObject` 本身(`lobject.h:305`)只有一个 `CommonHeader`:

```c
typedef struct GCObject {
  CommonHeader;
} GCObject;
```

而 `CommonHeader`(`lobject.h:301`)是：

```c
#define CommonHeader	struct GCObject *next; lu_byte tt; lu_byte marked
```

三个字段：指向下一个 GC 对象的 `next`(所有 GC 对象串成链表，GC 靠它遍历)、自己的类型标签 `tt`、GC 三色标记 `marked`。所有 GC 对象(表、闭包、字符串……)的结构体第一行都是 `CommonHeader;`，所以它们的指针都能安全地转成 `GCObject *` 来访问这三个公共字段。这同一个 header 既服务于"所有对象串成一条 allgc 链"，也服务于三色标记。**这套设计的精妙之处在于：`CommonHeader` 让异质对象(表、字符串、闭包)共享同一种链表节点和 GC 接口，GC 不必为每种类型各写一套遍历代码。** 这也是 P5 要展开讲的东西。

注意 `lua_State` 自己也有 `CommonHeader`——这意味着一个 `lua_State`(thread)本身就是一个 GC 对象，会被挂在 `allgc` 链上。这点下面会用到。

### 2.2 类型标签系统：8 位编码九种类型加变体

`tt_` 虽然只有一个字节，但信息密度很高。先看基本类型，定义在公开头 `lua.h:62`:

```c
#define LUA_TNIL		0
#define LUA_TBOOLEAN		1
#define LUA_TLIGHTUSERDATA	2
#define LUA_TNUMBER		3
#define LUA_TSTRING		4
#define LUA_TTABLE		5
#define LUA_TFUNCTION		6
#define LUA_TUSERDATA		7
#define LUA_TTHREAD		8

#define LUA_NUMTYPES		9
```

九种基本类型(`LUA_NUMTYPES = 9`)。但这九种不够细——光是 `LUA_TNUMBER` 就得区分整数和浮点(两者在 Lua 5.x 是不同子类型)，`LUA_TSTRING` 要区分短串和长串(短串驻留、长串不驻留，P1-03 详讲)，`LUA_TFUNCTION` 要区分 Lua 闭包和 C 闭包。所以 `tt_` 的 8 位被切成四段，在 `lobject.h:34` 的注释里讲得清清楚楚：

```c
/*
** tags for Tagged Values have the following use of bits:
** bits 0-3: actual tag (a LUA_T* constant)
** bits 4-5: variant bits
** bit 6: whether value is collectable
*/
```

- **bits 0-3(低 4 位)**：基本类型，对应 `LUA_TNIL` 到 `LUA_TTHREAD`，能装下 0-8 共 9 种，还有空余。
- **bits 4-5(中间 2 位)**：变体(variant)，4 种取值。把同一基本类型再细分。
- **bit 6**：是否是可回收对象(GC 对象)。

变体用宏 `makevariant`(`lobject.h:42`)合成：`#define makevariant(t,v) ((t) | ((v) << 4))`——基本类型 `t` 放低 4 位，变体号 `v` 左移 4 位放进去。于是所有具体变体标签的定义都长这样(分布在 `lobject.h` 各类型段落)：

```c
/* nil 的内部变体(lobject.h:183) */
#define LUA_VNIL        makevariant(LUA_TNIL, 0)
#define LUA_VEMPTY      makevariant(LUA_TNIL, 1)   /* 空栈槽 */
#define LUA_VABSTKEY    makevariant(LUA_TNIL, 2)   /* 表里查不到的键 */
#define LUA_VNOTABLE    makevariant(LUA_TNIL, 3)   /* 快速 get 碰到非表 */

/* 布尔两种(lobject.h:250) */
#define LUA_VFALSE      makevariant(LUA_TBOOLEAN, 0)
#define LUA_VTRUE       makevariant(LUA_TBOOLEAN, 1)

/* 数字的整/浮(lobject.h:336) */
#define LUA_VNUMINT     makevariant(LUA_TNUMBER, 0)  /* integer numbers */
#define LUA_VNUMFLT     makevariant(LUA_TNUMBER, 1)  /* float numbers */

/* 字符串的短/长(lobject.h:373) */
#define LUA_VSHRSTR     makevariant(LUA_TSTRING, 0)  /* short strings */
#define LUA_VLNGSTR     makevariant(LUA_TSTRING, 1)  /* long strings */

/* 函数的 Lua 闭包 / C 闭包(lobject.h:640) */
#define LUA_VLCL        makevariant(LUA_TFUNCTION, 0)  /* Lua closure */
#define LUA_VCCL        makevariant(LUA_TFUNCTION, 2)  /* C closure */
```

注意布尔和数字的区分方式不同：布尔没有 payload,`true`/`false` 全靠 `tt_` 区分；数字有 payload(`Value.i` 或 `Value.n`)，所以变体只用来标记"读哪个字段"。

bit 6 的"可回收"位是关键。它用一个宏标记(`lobject.h:311`)：

```c
#define BIT_ISCOLLECTABLE	(1 << 6)

#define iscollectable(o)	(rawtt(o) & BIT_ISCOLLECTABLE)

/* mark a tag as collectable */
#define ctb(t)			((t) | BIT_ISCOLLECTABLE)
```

凡是指向 GC 对象的值(字符串、表、闭包、userdata、thread)，其 `tt_` 的 bit 6 必须是 1。比如 `LUA_VTABLE` 是 `makevariant(LUA_TTABLE, 0)`，但实际写入一个"表值"时，`tt_` 被设成 `ctb(LUA_VTABLE)`——带上了 bit 6。而整数、浮点、布尔、light userdata、light C function 这些不需要 GC 的，bit 6 是 0。这一位让 GC 在扫值栈时，一眼就能判断"这个值要不要顺着指针追下去"——一个位运算 `iscollectable(o)` 就够了，不必查表。

取类型的一组宏(`lobject.h:72-87`)层层封装，从粗到细：

```c
#define val_(o)     ((o)->value_)
#define rawtt(o)    ((o)->tt_)

/* tag with no variants (bits 0-3) */
#define novariant(t)  ((t) & 0x0F)

/* type tag of a TValue (bits 0-3 for tags + variant bits 4-5) */
#define withvariant(t)  ((t) & 0x3F)
#define ttypetag(o)   withvariant(rawtt(o))

/* type of a TValue */
#define ttype(o)    (novariant(rawtt(o)))
```

三个粒度：`rawtt(o)` 拿原始字节；`novariant` 屏蔽掉变体只留基本类型(判断"是不是 number"用这个)；`withvariant` 保留变体(判断"是不是短串"用这个)。对外 API `lua_type` 返回的是 `novariant` 那一级(基本类型)，VM 内部判断具体变体用 `ttypetag`。

### 2.3 设值与取值宏：tagged value 的写入协议

光有结构不够，得有一套一致的"如何往一个 `TValue` 里写值"的协议。Lua 用一组宏，每个类型一对。挑关键的看。

**设类型标签的底层宏**(`lobject.h:114`)：

```c
/* set a value's tag */
#define settt_(o,t) ((o)->tt_=(t))
```

**通用拷贝**(`lobject.h:118`)——把一个 `TValue` 整体复制到另一个，值和标签一起搬：

```c
#define setobj(L,obj1,obj2) \
	{ TValue *io1=(obj1); const TValue *io2=(obj2); \
          io1->value_ = io2->value_; settt_(io1, io2->tt_); \
	  checkliveness(L,io1); lua_assert(!isnonstrictnil(io1)); }
```

注意它先拷 `value_` 再设 `tt_`，顺序在并发 GC 场景下有意义(避免 GC 读到一个"新标签+旧值"的撕裂态)，但单线程内更重要的一点是：`setobj` 是把 16 字节整体搬，没有分支、没有类型判断——这是寄存器式 VM 里 `MOVE` 指令、参数传递、返回值回填的底层原语，热得不能再热。

**各类专门的设值宏**(`lobject.h` 各类型段)：

```c
/* 设 nil(只动标签,payload 不用清,因为读时只看标签) */
#define setnilvalue(obj) settt_(obj, LUA_VNIL)              /* lobject.h:211 */

/* 设布尔(用标签区分真假,payload 同样不动) */
#define setbfvalue(obj)  settt_(obj, LUA_VFALSE)             /* lobject.h:263 */
#define setbtvalue(obj)  settt_(obj, LUA_VTRUE)              /* lobject.h:264 */

/* 设浮点 */
#define setfltvalue(obj,x) \
  { TValue *io=(obj); val_(io).n=(x); settt_(io, LUA_VNUMFLT); }   /* lobject.h:351 */

/* 设整数 */
#define setivalue(obj,x) \
  { TValue *io=(obj); val_(io).i=(x); settt_(io, LUA_VNUMINT); }   /* lobject.h:357 */

/* 设 light userdata(无 GC) */
#define setpvalue(obj,x) \
  { TValue *io=(obj); val_(io).p=(x); settt_(io, LUA_VLIGHTUSERDATA); } /* ~lobject.h:471 */

/* 设表(GC 对象,用 ctb 加可回收位) */
#define sethvalue(L,obj,x) ... \
    val_(io).gc = obj2gco(x_); settt_(io, ctb(LUA_VTABLE)); ...     /* lobject.h:736 */

/* 设字符串(GC 对象) */
#define setsvalue(L,obj,x) ... \
    val_(io).gc = obj2gco(x_); settt_(io, ctb(x_->tt)); ...          /* lobject.h:384 */

/* 设 Lua 闭包 */
#define setclLvalue(L,obj,x) ... \
    val_(io).gc = obj2gco(x_); settt_(io, ctb(LUA_VLCL)); ...        /* lobject.h:660 */
```

两个规律一眼可见：

1. **非 GC 类型**(`nil`/布尔/数字/light userdata/light C function)的宏不带 `ctb`,bit 6 是 0;**GC 类型**(字符串/表/闭包/userdata/thread/proto)的宏一律用 `ctb(...)` 设标签，bit 6 是 1。这条线和 `iscollectable` 完美对齐。

2. 设 `nil` 和布尔根本不动 payload——`setnilvalue` 只写 `tt_`。这是 sound 的：读取代码看到 `LUA_VNIL` 就当 nil 用，不会去读 `value_` 的内容。所以一个刚 `malloc` 出来、payload 是垃圾的 `TValue`，只要把 `tt_` 设成 `LUA_VNIL`，它就是个合法的 nil。值栈初始化时就靠这条(`lstate.c:163-164` 把整个栈 `setnilvalue` 一遍)。

取值宏对应：布尔/数字用 `checktag` 断言类型后直接读字段(`ivalue`/`fltvalue`/`nvalue` 见 `lobject.h:343-346`)，GC 对象用 `gcvalue(o)` 拿 `val_(o).gc`(`lobject.h:318`)。这套宏统一了"怎么读写一个值"——VM 解释器、Table 操作、API 层全用同一套，没有第二套路径。

### 2.4 lua_State：一个线程的全部状态

值讲完了，看承载这些值的 VM。`struct lua_State` 在 `lstate.h:285`，逐字段贴出来：

```c
struct lua_State {
  CommonHeader;
  lu_byte allowhook;
  TStatus status;
  StkIdRel top;  /* first free slot in the stack */
  struct global_State *l_G;
  CallInfo *ci;  /* call info for current function */
  StkIdRel stack_last;  /* end of stack (last element + 1) */
  StkIdRel stack;  /* stack base */
  UpVal *openupval;  /* list of open upvalues in this stack */
  StkIdRel tbclist;  /* list of to-be-closed variables */
  GCObject *gclist;
  struct lua_State *twups;  /* list of threads with open upvalues */
  struct lua_longjmp *errorJmp;  /* current error recover point */
  CallInfo base_ci;  /* CallInfo for first level (C host) */
  volatile lua_Hook hook;
  ptrdiff_t errfunc;  /* current error handling function (stack index) */
  l_uint32 nCcalls;  /* number of nested non-yieldable or C calls */
  int oldpc;  /* last pc traced */
  int nci;  /* number of items in 'ci' list */
  int basehookcount;
  int hookcount;
  volatile l_signalT hookmask;
  struct {  /* info about transferred values (for call/return hooks) */
    int ftransfer;  /* offset of first value transferred */
    int ntransfer;  /* number of values transferred */
  } transferinfo;
};
```

逐块讲。先讲和"值栈"直接相关的字段——这是 `lua_State` 的心脏。

- **`CommonHeader`**(展开成 `next; tt; marked`)：`lua_State` 自己是 GC 对象。它的 `tt` 是 `LUA_VTHREAD`(`lstate.c:343` 在 `lua_newstate` 里就设好 `L->tt = LUA_VTHREAD`)。这意味着所有 thread 都挂在 `allgc` 链上，GC 会扫到它们。
- **`stack` / `stack_last` / `top`**：值栈的三件套。`stack` 指向栈底(`StackValue` 数组起点)，`stack_last` 指向栈顶上限(最后一个可用槽 + 1，即越界哨兵)，`top` 指向第一个空闲槽。`stack_last - stack` 是栈容量，`top - stack` 是当前有效值数。
- **`StkIdRel` 类型**(而不是 `StkId`/`TValue *`)：这是 5.5(以及 5.4)相对老资料(讲 5.3)的硬差异，下面 2.6 单独讲透。

值栈的元素不是 `TValue`，而是 `StackValue`(`lobject.h:148`)：

```c
typedef union StackValue {
  TValue val;
  struct {
    TValuefields;
    unsigned short delta;
  } tbclist;
} StackValue;
```

它是个 union：正常情况下就是一个 `TValue`(走 `val` 成员)；但要支持 to-be-closed 变量(`tbclist` 成员多一个 `delta`，用来把栈上所有 to-be-closed 变量串成链)。`StkId` 就是 `StackValue *`(`lobject.h:158`)：

```c
/* index to stack elements */
typedef StackValue *StkId;
```

VM 里所谓"寄存器"，其实是某个函数栈帧里 `StackValue` 数组的一段——每条字节码指令的操作数(寄存器号)最终都被翻译成"相对当前函数栈底的偏移"。这是 P3、P4 的核心，这里只埋一个锚：寄存器不是独立存储，就是值栈的一段。

`top` 标记有效值边界：栈里 `stack` 到 `top` 之间的槽是当前活跃的值，`top` 到 `stack_last` 是空闲但可用的槽，`stack_last` 之外还有 `EXTRA_STACK = 5`(`lstate.h:142`)的紧急缓冲——这 5 个槽不计入 `stack_last`，专门留给元方法调用等"马上要 push 但很快会检查栈"的情况，免得每次都查栈溢出。

接着讲控制和生命周期字段。

- **`l_G`**(`struct global_State *`)：指向共享的全局状态。所有协程(同一次 `luaL_newstate` 创建出的 thread)共用一个 `global_State`；不同 `lua_State`(不同 `luaL_newstate`)各自独立。这是"多世界"和"协程切栈"的物理基础(2.5 详讲)。宏 `G(L)` 就是 `L->l_G`(`lstate.h:375`)。
- **`ci` / `base_ci` / `nci`**：调用链。`ci` 指向当前调用帧(`CallInfo`,`lstate.h:187`)，`base_ci` 是内嵌的第一帧(C 宿主那一层，不用 malloc)，`nci` 是链上 `CallInfo` 总数。每个 `CallInfo` 记录一次函数调用的上下文：`func`(函数在栈上的位置)、`top`(这个函数的栈帧上限)、Lua 函数的 `savedpc`(执行到哪条字节码)、C 函数的 continuation `k`、调用状态 `callstatus`。`ci` 串成双向链表(`previous`/`next`)。
- **`openupval` / `tbclist`**：两条栈内链表。`openupval` 是当前栈上所有"开放 upvalue"(P4-14 详讲，指还没闭合、仍指向栈槽的 upvalue)链；`tbclist` 是所有 to-be-closed 变量链。这俩都用 `delta` 偏移串，不额外分配。
- **`errorJmp`**(`struct lua_longjmp *`)：错误恢复点。Lua 的错误处理是 `setjmp`/`longjmp` 实现的——调用 `luaD_pcall` 时压一个 `lua_longjmp` 上栈，出 `error`(`luaG_errormsg` → `luaD_throw`)时 `longjmp` 回到这里。`struct lua_longjmp` 定义在 `ldo.c`(不是头文件，因为只有 ldo 用)。协程 yield/resume 的栈切换也走这套机制。
- **`nCcalls`**(`l_uint32`)：C 调用深度 + 不可 yield 调用深度，两个 16 位拼一个 32 位(`lstate.h:94-99` 注释说得很清楚：低 16 位数 C 栈递归次数，高 16 位数不可 yield 调用次数，合在一起是为了"一条指令同时改和存")。防 C 栈溢出：`LUAI_MAXCCALLS = 200`(`ldo.h:63`)，超了 `luaE_checkcstack` 报 "C stack overflow"。`yieldable(L)` 判断高 16 位是否为 0(`lstate.h:104`)——非零说明栈上有不可 yield 的调用，此时 yield 是非法的。
- **`status`**(`TStatus`，就是 `lu_byte`)：线程状态(`LUA_OK`/`LUA_YIELD`/各种错误码)。
- **`hook` / `hookmask` / `hookcount` / `basehookcount` / `allowhook` / `oldpc`**：调试钩子全套。`hook` 是回调函数，`hookmask` 是位掩码(哪类事件触发：call/return/line/count)，`hookcount`/`basehookcount` 是 count hook 的计数，`allowhook` 是"当前是否允许 hook 重入",`oldpc` 记上次触发的 pc(防同一行重复触发 line hook)。
- **`gclist`**:GC 对象链表节点。thread 本身在某些时刻会进 gray 链(被标记时)。
- **`twups`**:"threads with open upvalues" 链。一个 thread 一旦有了开放 upvalue，就被挂到 `g->twups` 链上，GC 要扫这些栈(因为栈槽里可能持有别对象活引用)。
- **`errfunc`**(`ptrdiff_t`)：当前 `pcall` 的错误处理函数在栈上的偏移(用偏移而非指针，同样是抗栈搬家的设计)。
- **`transferinfo`**:call/return hook 用的"这次调用传了几个值、从哪开始"。

这一大堆字段全装在一个 `struct lua_State` 里。宿主拿到的 `lua_State *` 指针，指向的就是这整个结构——值栈、调用链、错误恢复、调试钩子，全在里面。这就是"一个 `lua_State` 是一个完整 VM 实例"在 C 层面的字面含义。

还有一层包装：`LX`(`lstate.h:318`)：

```c
typedef struct LX {
  lu_byte extra_[LUA_EXTRASPACE];
  lua_State l;
} LX;
```

`lua_State` 实际是嵌在 `LX` 里的，前面留了 `LUA_EXTRASPACE` 字节(默认 0，可配)给宿主藏私有数据。宿主调 `lua_getextraspace(L)` 拿到的就是这块。`lua_newstate` 分配的是 `LX` 大小，主线程就内嵌在 `global_State.mainth` 里(`g->mainth` 是 `LX` 类型，`lstate.h:371`)，所以主 thread 不单独 malloc——`L = &g->mainth.l`(`lstate.c:342`)。

### 2.5 global_State：所有线程共享的世界

`lua_State` 是 per-thread,`global_State` 是 per-state(一个 `luaL_newstate` 一份)。`struct global_State` 在 `lstate.h:327`:

```c
typedef struct global_State {
  lua_Alloc frealloc;  /* function to reallocate memory */
  void *ud;         /* auxiliary data to 'frealloc' */
  l_mem GCtotalbytes;  /* number of bytes currently allocated + debt */
  l_mem GCdebt;  /* bytes counted but not yet allocated */
  l_mem GCmarked;  /* number of objects marked in a GC cycle */
  l_mem GCmajorminor;  /* auxiliary counter to control major-minor shifts */
  stringtable strt;  /* hash table for strings */
  TValue l_registry;
  TValue nilvalue;  /* a nil value */
  unsigned int seed;  /* randomized seed for hashes */
  lu_byte gcparams[LUA_GCPN];
  lu_byte currentwhite;
  lu_byte gcstate;  /* state of garbage collector */
  lu_byte gckind;  /* kind of GC running */
  lu_byte gcstopem;  /* stops emergency collections */
  lu_byte gcstp;  /* control whether GC is running */
  lu_byte gcemergency;  /* true if this is an emergency collection */
  GCObject *allgc;  /* list of all collectable objects */
  GCObject **sweepgc;  /* current position of sweep in list */
  GCObject *finobj;  /* list of collectable objects with finalizers */
  GCObject *gray;  /* list of gray objects */
  GCObject *grayagain;  /* list of objects to be traversed atomically */
  GCObject *weak;  /* list of tables with weak values */
  GCObject *ephemeron;  /* list of ephemeron tables (weak keys) */
  GCObject *allweak;  /* list of all-weak tables */
  GCObject *tobefnz;  /* list of userdata to be GC */
  GCObject *fixedgc;  /* list of objects not to be collected */
  /* fields for generational collector */
  GCObject *survival;  /* start of objects that survived one GC cycle */
  GCObject *old1;  /* start of old1 objects */
  GCObject *reallyold;  /* objects more than one cycle old ("really old") */
  GCObject *firstold1;  /* first OLD1 object in the list (if any) */
  GCObject *finobjsur;  /* list of survival objects with finalizers */
  GCObject *finobjold1;  /* list of old1 objects with finalizers */
  GCObject *finobjrold;  /* list of really old objects with finalizers */
  struct lua_State *twups;  /* list of threads with open upvalues */
  lua_CFunction panic;  /* to be called in unprotected errors */
  TString *memerrmsg;  /* message for memory-allocation errors */
  TString *tmname[TM_N];  /* array with tag-method names */
  struct Table *mt[LUA_NUMTYPES];  /* metatables for basic types */
  TString *strcache[STRCACHE_N][STRCACHE_M];  /* cache for strings in API */
  lua_WarnFunction warnf;  /* warning function */
  void *ud_warn;         /* auxiliary data to 'warnf' */
  LX mainth;  /* main thread of this state */
} global_State;
```

这个结构是"一个 Lua 世界"的全局面。挑核心讲。

**嵌入接口：`frealloc` + `ud`**。这是 Lua 对接宿主内存管理的唯一通道。`frealloc` 是宿主提供的分配器函数指针，签名是 `lua_Alloc`(`void *(*)(void *ud, void *ptr, size_t osize, size_t nsize)`)。Lua 所有内存分配(`luaM_new`/`luaM_realloc`/`luaM_free`)最终都走 `(*g->frealloc)(g->ud, ptr, osize, nsize)`。宿主可以塞自己的分配器(游戏引擎的定长池、Redis 的 zmalloc、嵌入式设备的静态堆)，Lua 完全不假设 `malloc` 存在。`luaL_newstate`(`lauxlib.c:1184`)默认用 `luaL_alloc`(就是带 `realloc` 直通的包装，`lauxlib.c:1049`)：

```c
LUALIB_API lua_State *(luaL_newstate) (void) {
  lua_State *L = lua_newstate(luaL_alloc, NULL, luaL_makeseed(NULL));
  if (l_likely(L)) {
    lua_atpanic(L, &panic);
    lua_setwarnf(L, warnfon, L);
  }
  return L;
}
```

注意 5.5 的硬差异：**`lua_newstate` 在 5.5 是三参数 `lua_newstate(f, ud, seed)`**(`lua.h:163`)，第三个 `seed` 是哈希随机种子；老资料(5.4 及更早)写的是两参数 `lua_newstate(f, ud)`，种子在内部生成。5.5 把种子提到 API，允许宿主控制随机化(防哈希碰撞攻击)。`luaL_makeseed`(`lauxlib.c:1174`)默认用时间、地址等凑一个。

**GC 全家桶**。`currentwhite`/`gcstate`/`gckind`/`gcstopem`/`gcstp`/`gcemergency` 是 GC 状态机；`GCtotalbytes`/`GCdebt`/`GCmarked`/`GCmajorminor` 是 GC 统计与步进控制；`gcparams[LUA_GCPN]` 是可调参数(paudebt/mul/stepsize 等)。然后是一堆 GC 对象链表：

- `allgc`：所有 GC 对象主链(创建时就挂上，`luaC_newobj` 干这事)。
- `finobj`：带 finalizer(`__gc`)的对象链。
- `tobefnz`：马上要跑 finalizer 的对象链。
- `fixedgc`：不可回收对象链(目前主要是保留字短串，如 `if`/`then`)。
- `gray`/`grayagain`：三色标记的工作队列。
- `weak`/`ephemeron`/`allweak`：三种弱表的清理队列。
- 分代 collector 的分代边界指针(`survival`/`old1`/`reallyold`/`firstold1` 以及对应的 finobj 变体)。

这些链表是 P5-16/17/18 的主角，这里只认下它们都在 `global_State` 里——所有 thread 共享一份 GC，所以一个 thread 创建的对象，另一个 thread(同 state)也能引用，GC 统一扫。

**字符串表 `strt`**(`stringtable`@`lstate.h:167`)：所有驻留短串的全局哈希表。`hash`(桶数组)、`nuse`(元素数)、`size`(桶数)。这是"同样的短串在整个 state 里只有一份"的物理保证，P1-03 详讲。

**`l_registry`**(`TValue`)：注册表，一个全局表，存 `_G`(全局环境)、主 thread 引用、C 库注册的引用。`init_registry`(`lstate.c:186`)在新建 state 时填好它的三个预置键。

**`mt[LUA_NUMTYPES]`**：九种基本类型各自的元表(只有 table/userdata 两种是用户可设的，但数组留了全部槽位)。

**`strcache[STRCACHE_N][STRCACHE_M]`**(`STRCACHE_N=53`、`STRCACHE_M=2`,`lstate.h:151`)：C API 里 `lua_pushstring` 的快速缓存，避免每次都查 `strt`。

**`mainth`**(`LX`)：主线程内嵌在这里。`mainthread(G)` 宏(`lstate.h:376`)返回 `&G->mainth.l`。

**"多 lua_State 共享一个 global_State"的含义**:`lua_newstate` 建的是主 thread，它和 `global_State` 一一对应；`lua_newthread`(`lstate.c:273`)建的是协程级 thread，它**复用**调用者所在 state 的 `global_State`(`preinit_thread` 里 `G(L1) = g`，共享)，但自己有独立的 `stack`/`ci`。所以：

- 同一 state 内的多个 thread(协程)：共享 `strt`、共享 GC、共享 `l_registry`，但各自有独立值栈和调用链。协程切换的本质就是换 `lua_State`(换栈和 `ci`)，`global_State` 不动。
- 不同 state(`luaL_newstate` 各调一次)：完全隔离的两套 `global_State`，互不可见。一个进程里跑多个互不干扰的 Lua 世界就靠这个。

### 2.6 StkIdRel:5.5(5.4)的相对栈表示

现在讲本章最值得标注的一处 5.5 vs 老资料差异。看 `lua_State` 里那几个栈指针：

```c
StkIdRel top;
StkIdRel stack_last;
StkIdRel stack;
StkIdRel tbclist;
```

类型是 `StkIdRel`，不是 `StkId`(也就是 `StackValue *`)。`StkIdRel` 定义在 `lobject.h:165`:

```c
/*
** When reallocating the stack, change all pointers to the stack into
** proper offsets.
*/
typedef union {
  StkId p;  /* actual pointer */
  ptrdiff_t offset;  /* used while the stack is being reallocated */
} StkIdRel;
```

这是个 union：正常工作时 `p` 是真实指针(绝对地址)；栈扩容搬家的那一瞬，字段被重解释成 `offset`(相对栈底的字节偏移)。为什么这么设计？因为值栈是个会 `realloc` 的 `StackValue` 数组——当栈不够用、`luaD_reallocstack`(`ldo.c:322`)调用 `luaM_reallocvector` 扩容时，数组可能被搬到新地址，于是所有指向栈的指针(`top`、`stack_last`、各个 `ci->func`/`ci->top`、各个 open upvalue 的 `v.p`、`tbclist`)全部失效。

如果是绝对指针(老资料讲的 5.3 就是 `StkId = TValue *`)，`realloc` 搬家后必须**逐个**找出所有指向旧栈的指针、加上地址差、改成新栈地址。这要求代码维护一份"所有指向栈的指针清单"，容易漏、容易错。5.4 引入(5.5 沿用)的 `StkIdRel` 改成相对偏移，搬家的核心代码就干净了。看 `ldo.c` 的 `luaD_reallocstack`:

```c
int luaD_reallocstack (lua_State *L, int newsize, int raiseerror) {
  int oldsize = stacksize(L);
  int i;
  StkId newstack;
  StkId oldstack = L->stack.p;
  lu_byte oldgcstop = G(L)->gcstopem;
  lua_assert(newsize <= MAXSTACK || newsize == ERRORSTACKSIZE);
  relstack(L);  /* change pointers to offsets */                       /* ldo.c:329 */
  G(L)->gcstopem = 1;  /* stop emergency collection */
  newstack = luaM_reallocvector(L, oldstack, oldsize + EXTRA_STACK,
                                   newsize + EXTRA_STACK, StackValue);
  G(L)->gcstopem = oldgcstop;  /* restore emergency collection */
  if (l_unlikely(newstack == NULL)) {  /* reallocation failed? */
    correctstack(L, oldstack);  /* change offsets back to pointers */
    if (raiseerror)
      luaM_error(L);
    else return 0;  /* do not raise an error */
  }
  L->stack.p = newstack;
  correctstack(L, oldstack);  /* change offsets back to pointers */    /* ldo.c:341 */
  ...
}
```

三步走：`relstack`(把所有指针转成偏移)→ `realloc` → `correctstack`(把所有偏移转回指针，但这次指针指向新栈)。`relstack`(`ldo.c:252`)的实现是枚举所有需要修正的位置：

```c
static void relstack (lua_State *L) {
  CallInfo *ci;
  UpVal *up;
  L->top.offset = savestack(L, L->top.p);
  L->tbclist.offset = savestack(L, L->tbclist.p);
  for (up = L->openupval; up != NULL; up = up->u.open.next)
    up->v.offset = savestack(L, uplevel(up));
  for (ci = L->ci; ci != NULL; ci = ci->previous) {
    ci->top.offset = savestack(L, ci->top.p);
    ci->func.offset = savestack(L, ci->func.p);
  }
}
```

`savestack`/`restorestack` 在 `ldo.h:45`:

```c
#define savestack(L,pt)     (cast_charp(pt) - cast_charp(L->stack.p))
#define restorestack(L,n)   cast(StkId, cast_charp(L->stack.p) + (n))
```

就是字节级偏移。`correctstack`(`ldo.c:269`)把同样的位置用 `restorestack` 转回去——因为此时 `L->stack.p` 已经是新地址，偏移加新基地址就是新指针，自动正确。

注意 open upvalue 也参与这套(`up->v.offset` 在 `relstack` 里被写)——`UpVal` 的 `v` 字段(`lobject.h:681`)本身也是 `union { TValue *p; ptrdiff_t offset; }`，和 `StkIdRel` 一模一样的把戏。这就是为什么 open upvalue 也能扛住栈搬家：它指向栈槽的引用，在搬家瞬间被临时转成偏移，搬完再转回指针。

这套设计 sound 在哪？关键在于：**搬家是一个同步的、原子的过程**。`relstack` 把所有指针变偏移后，`realloc` 才动手；`realloc` 完成后 `correctstack` 立刻把偏移变回指针。中间没有任何代码会去读这些 `StkIdRel` 字段(`relstack` 之后到 `correctstack` 之前，栈本身处于"搬家"状态，VM 不执行字节码)。所以"先偏移化、搬完再指针化"在单线程里是无缝的——这是 2.7 节要展开的 sound 论证。

补充一个边角：5.5 默认走 `LUAI_STRICT_ADDRESS = 1`(`ldo.h` 附近的配置，`ldo.c:244`)分支，即上面这种"转偏移再转回来"的严格版本。代码里还有一个 `#else` 分支(`ldo.c:285`)做同样的事但假设"free 后的地址还能用"——非严格平台走那条。默认严格版更可移植。

`StkIdRel` 是 5.4 引入、5.5 沿用的设计。**凡是讲 5.3 或更早的资料，`lua_State` 里的栈指针写的是 `StkId`(即 `TValue *` 绝对指针)，与 5.5 源码不符。** 这是本章必须显式标注的硬差异之一。

### 2.7 生命周期：newstate 与 close

最后看一个 `lua_State` 怎么生、怎么死。生在 `lua_newstate`(`lstate.c:336`)：

```c
LUA_API lua_State *lua_newstate (lua_Alloc f, void *ud, unsigned seed) {
  int i;
  lua_State *L;
  global_State *g = cast(global_State*,
                       (*f)(ud, NULL, LUA_TTHREAD, sizeof(global_State)));   /* lstate.c:339 */
  if (g == NULL) return NULL;
  L = &g->mainth.l;                                                          /* lstate.c:342 */
  L->tt = LUA_VTHREAD;
  g->currentwhite = bitmask(WHITE0BIT);
  L->marked = luaC_white(g);
  preinit_thread(L, g);
  g->allgc = obj2gco(L);  /* by now, only object is the main thread */
  ...
  g->frealloc = f;
  g->ud = ud;
  ...
  g->seed = seed;
  g->gcstp = GCSTPGC;  /* no GC while building state */
  ...
  if (luaD_rawrunprotected(L, f_luaopen, NULL) != LUA_OK) {
    /* memory allocation error: free partial state */
    close_state(L);
    L = NULL;
  }
  return L;
}
```

关键步骤：用宿主的 `f` 分配一个 `global_State` 大小的块(`lstate.c:339`)→ 主线程 `L` 就嵌在这个块的 `mainth.l` 里(`lstate.c:342`，不单独分配)→ 给主线程设类型 `LUA_VTHREAD`、GC 白色标记 → `preinit_thread` 把基本字段清零(`lstate.c:225`)→ 把主线程挂上 `allgc`(此时世界唯一对象就是主线程)→ 初始化 GC 参数和各链表 → 用 `luaD_rawrunprotected`(底层 `setjmp` 保护)跑 `f_luaopen`(`lstate.c:207`)完成后续初始化：`stack_init`(建值栈)、`init_registry`(建注册表)、`luaS_init`(建字符串表)、`luaT_init`(建元方法名表)、`luaX_init`(建保留字短串)。任何一步分配失败，`rawrunprotected` 捕获错误，`close_state` 回滚。

`g->gcstp = GCSTPGC` 这行很关键：**建 state 期间 GC 是停的**(`GCSTPGC` 是"GC 自己停自己"的位，`lgc.h:214`)，避免还没建好的半成品被 GC 扫到。`f_luaopen` 最后才 `g->gcstp = 0`(`lstate.c:215`)放开 GC。而 `completestate(g)` 宏(`lstate.h:382`)用 `g->nilvalue` 是不是 nil 来判断 state 是否建完——`lua_newstate` 里故意把 `nilvalue` 设成整数 0(`lstate.c:374`:`setivalue(&g->nilvalue, 0)`)，`f_luaopen` 最后才 `setnilvalue(&g->nilvalue)`(`lstate.c:216`)把它变成真正的 nil，标记"state 完工"。

死在 `lua_close`(`lstate.c:391`)：

```c
LUA_API void lua_close (lua_State *L) {
  ...
  close_state(L);
}
```

`close_state`(`lstate.c:255`)的步骤：关所有 upvalue(`luaD_closeprotected`)→ 清空栈顶准备跑 finalizer → `luaC_freeallobjects` 把所有 GC 对象回收一遍(包括跑 `__gc`)→ 释放字符串表哈希桶 → `freestack` 释放值栈和 `ci` 链 → 最后用 `(*g->frealloc)(g->ud, g, sizeof(global_State), 0)`(`lstate.c:269`)把 `global_State` 块本身还给宿主。断言 `gettotalbytes(g) == sizeof(global_State)`(`lstate.c:268`)确保此时一个字节的多余分配都不剩——干净到底。

`luaL_newstate` / `lua_close` 这一对，就是一个 Lua 世界从无到有、从有到无的完整边界。中间所有操作，都作用在那个 `lua_State *` 上。

---

## 三、为什么这样设计是 sound 的

### 3.1 tagged value 为什么 sound：类型标签和 payload 永远同步

`TValue` 的 sound 性质是：**任何时候读一个 `TValue`,`tt_` 准确反映 `value_` 当前装的是什么**。这由设值宏的协议保证——每个 `set*value` 宏都在写 payload 之后(或同时)写 `tt_`，没有任何一条代码路径会"改了 payload 不改标签"或"改了标签不改 payload"。唯一的例外是 nil/布尔：它们的 payload 不被读，所以只设标签就够。

这套协议的后果是：类型判断永远是 O(1) 的一个字节读加一次比较(`ttype`/`ttisinteger`/`ttisstring`...)，没有任何运行时类型查找。VM 执行 `ADD` 指令时，先 `ttisinteger(Ra)`、`ttisinteger(Rb)` 两个宏一比就知道走整数加还是浮点加或元方法——没有虚函数表、没有 `dynamic_cast`、没有类型对象解引用。这是 Lua 解释器快的一个底层来源。

而 GC 的 sound 性靠 bit 6:`iscollectable(o)` 一个位运算就告诉 GC"这个值要不要顺着 `gc` 指针追"。如果标签和"是不是 GC 对象"对不上(比如把一个整数标成可回收)，GC 会去解引用一个根本不是指针的 `i` 字段，直接崩。所以 bit 6 的正确性由 `ctb` 宏强制——所有设 GC 对象的宏都走 `ctb(...)`，不给人手写忘加 bit 6 的机会。

### 3.2 值内联为什么 sound：不用区分"值在栈上还是堆上"

Lua 的值栈是一段连续的 `StackValue` 数组，每个元素 16 字节左右(union，可能因为 `tbclist` 的 `delta` 略大)。一个整数 `42` 就直接躺在某个栈槽的 `value_.i` 里，不经过任何堆分配。一个布尔 `true` 就是一个 `tt_ = LUA_VTRUE` 的栈槽。访问它们：`val_(o).i`、看 `tt_`，完事，零解引用(相对于"值是指针，指针指向堆上的 box,box 里才是真值"的模型)。

sound 的点在于：**所有值用同一种表示(都是 `TValue`)，不管它是不是堆对象**。VM 不需要写两套代码("如果是指针走 A 路径，如果是内联值走 B 路径")。`setobj`、Table 的 `get`/`set`、参数传递，统统按 16 字节拷贝处理，拷完类型标签自动带上。GC 扫栈时，对每个槽 `iscollectable` 一下，是 GC 对象才追指针，不是就跳过——一个统一循环搞定。

代价是每个值占满 16 字节，哪怕一个布尔(实际有效信息 1 bit + 1 字节标签)也占 16 字节。这是"用空间换统一和速度"的取舍——后面 ★对照 CPython 一栏会看清这个取舍的相对位置。

### 3.3 StkIdRel 为什么 sound：搬家是个原子区间

2.6 讲了 `relstack` → `realloc` → `correctstack` 三步。sound 的关键论证：

1. **`relstack` 之后、`correctstack` 之前，这段代码区间内没有任何字节码执行**。`luaD_reallocstack` 是同步函数调用，中间不 yield、不跑 hook、不让 GC 扫栈(`gcstopem = 1` 临时停紧急 GC)。所以"栈指针处于偏移态"这个中间状态对 VM 不可见。
2. **所有需要修正的位置都被 `relstack` 枚举到**。看 `relstack` 的循环：`L->top`、`L->tbclist`、所有 open upvalue(`L->openupval` 链)、所有 `ci`(沿 `ci->previous` 链)的 `top`/`func`。这几类就是全部指向值栈的指针来源。`StkIdRel` 把它们统一成"可重解释为偏移"的 union，所以 `relstack` 能原地改它们。
3. **偏移是相对栈底的字节差，与栈的实际地址无关**。`realloc` 把栈搬到任何新地址，`restorestack(L, offset) = L->stack.p + offset` 都给出正确的新指针，因为 `L->stack.p` 已经被更新成新地址(`luaD_reallocstack` 在 `correctstack` 之前就 `L->stack.p = newstack`,`ldo.c:340`)。

这三条合起来，`StkIdRel` 保证了"值栈可动态扩容，且扩容时不丢任何指针"。这比老版本(5.3 及更早)用绝对指针 + 手动遍历修正更不容易漏(漏一个就悬空指针)，代码也更短。这是 5.5 相对老资料在正确性维护性上的实质改进。

### 3.4 多 thread 共享 global_State 为什么 sound:GC 视图全局唯一

协程(同 state 的多个 thread)共享一个 `global_State`。这意味着所有 GC 对象(不管哪个 thread 创建的)都在同一个 `allgc` 链上，GC 扫描时看到的是全局唯一的对象图。一个 thread 持有的对象引用，另一个 thread 也能看到——只要通过 `l_registry` 或共享 Table 暴露出来。

sound 的点：GC 不需要"先扫 A 线程的堆、再扫 B 线程的堆、再合并"——只有一个堆(`allgc`)、一套三色不变式、一个 gray 队列。增量 GC 的步进也是全局的(由 `GCdebt` 控制)，所有线程共同推进。各 thread 的值栈是各自私有的(物理隔离)，但栈里持有的 GC 对象引用被 GC 统一追踪。这种"栈私有、堆共享"的布局，是协程能低成本共享数据的根基，也是 Lua 不需要 GIL(不像 CPython)的物理前提——当然，Lua 的协程是协作式非抢占的，不存在真并发，所以也不需要 GIL。

---

## 四、★对照 CPython + 回扣主线

### 4.1 ★对照 CPython:PyObject 堆 box vs 内联 tagged TValue

CPython 里每一个值——哪怕一个整数 `42`、一个布尔 `True`——都是一个堆上的 `PyObject`。`PyObject` 的核心是这三个字段(简化)：

```c
typedef struct _object {
    Py_ssize_t ob_refcnt;     /* 引用计数 */
    PyTypeObject *ob_type;    /* 类型对象指针 */
    ...
} PyObject;
```

具体类型(`PyLongObject`、`PyFloatObject`、`PyUnicodeObject`、`PyDictObject`...)都在 `PyObject` 头之后接自己的 payload。一个 `42` 在 CPython 里是：分配一个 `PyLongObject`(至少 28 字节，含 ob_refcnt/ob_type/ob_size/数字位)，`ob_refcnt` 初始为 1,`ob_type` 指向 `PyLong_Type`。每一次变量赋值，引用计数 +1；每一次离开作用域，引用计数 -1；减到 0 立刻释放。

把两套表示放到一起对比：

| 维度 | Lua 5.5 `TValue` | CPython `PyObject` |
|---|---|---|
| **值在哪里** | 直接内联在值栈槽里(16 字节，含标签) | 堆上一个 box，变量存的是指向 box 的指针 |
| **小整数/布尔** | 整数/布尔直接躺栈槽，零堆分配 | 仍是堆对象(CPython 有小整数缓存 -5..256，但仍是 `PyLongObject` 指针) |
| **类型信息** | 1 字节 `tt_`(标签内联，零解引用) | `ob_type` 指针，要解引用才知道类型 |
| **生命周期** | GC 统一回收(增量标记) | 引用计数即时回收 + 分代标记处理环 |
| **一次值访问** | 读 `tt_` 判类型 + 读 `value_` 字段(都在同一缓存行) | 先解引用指针拿到 box，再读 `ob_type` 判类型，再读 payload(至少两次解引用) |
| **创建一个临时值** | 写栈槽(两条赋值) | malloc 一个堆对象 + 初始化 + 管引用计数 |
| **空间代价** | 每个值固定 16 字节(布尔也 16) | 每个值至少 16-28 字节 + 一个指针位的存储 |

这个对照揭示的取舍：

- **CPython 的 box 模型**让值可以独立存在、跨栈帧传递只需拷贝指针(8 字节)、引用计数让回收非常即时(对象一不可达就 free)。代价是：每个值至少一次堆分配(慢)、每次访问至少两次解引用(慢，缓存不友好)、引用计数的增减散布在每条赋值里(开销)，且循环引用要靠周期性的分代标记清理(那是一次 STW)。
- **Lua 的内联 tagged 模型**让小整数/布尔/浮点零堆分配(快)、访问值零解引用(快，值和标签在同一缓存行)、统一交给增量 GC(对宿主延迟友好)。代价是：每个值固定占满 16 字节(布尔也 16，空间有浪费)、值在栈上时不能"跨栈帧共享存储"(得整体拷贝 16 字节，不过 `setobj` 是无分支的)。

两套都 sound，但服务的目标不同。CPython 的模型适合"值生命周期复杂、跨结构共享多"的通用脚本(它的 dict/list/tuple 各自高度优化)；Lua 的模型适合"值大量是临时局部量、要嵌入宿主、不能容忍每值一次 malloc"的场景(游戏每帧创建海量临时向量、Redis 每条 EVAL 产生一堆临时值)。**Lua 的选择是它"小而快"主线的直接体现：砍掉每值的堆 box，换来零分配的临时值和零解引用的访问。** 这正是本章在全书主线里的落点。

### 4.2 回扣主线

把这一章的三个核心设计放回"统一与精简换小而快"的主线：

- **统一的 `TValue`**：九种类型一种 C 表示，VM/编译器/GC/API 全用同一套读写宏。源码不必为每种类型各写一套值操作 → 内核代码量小(小)；类型判断是 1 字节比较，零解引用(快)。
- **内联的值存储**：小整数/布尔/浮点零堆分配，直接躺值栈。临时值不 malloc(快)；值和标签同缓存行(快)；代价是布尔也占 16 字节(空间换速度)。
- **`lua_State` 装下整个 VM**：一个结构体装下值栈、调用链、错误恢复、调试钩子，共享一份 `global_State`(GC/字符串表/注册表)。宿主一个指针就拿到一个完整隔离的世界(适合嵌入)；协程只是另一个 `lua_State`，切栈不切全局状态(协程便宜)。
- **`StkIdRel` 相对栈表示**：值栈可动态扩容且搬家时不丢指针。代码更短更不容易漏(精简)；正确性由"搬家是原子区间 + 全部指针来源被枚举"保证(sound)。

这四点共同回答了开篇的两个问题：一个 Lua 值在 C 里是一个 16 字节的 tagged `TValue`；整个 VM 装在一个 `lua_State` 加一个 `global_State` 里。它们是后面所有章节的物理地基——Table 用 `TValue` 存键值(P1-04)、字节码用寄存器号索引值栈槽(P3)、闭包用 `UpVal` 指向栈槽(P4-14)、GC 扫值栈靠 `iscollectable`(P5)、C API 在值栈顶推取 `TValue`(P6-21)。每一章都会回到这两个结构。

---

*下一章 [P1-03 字符串：短串驻留与长串惰性哈希](P1-03-字符串-短串驻留与长串惰性哈希.md):`TValue` 里的 `gc` 指针指向的第一个具体 GC 对象——`TString`，看 Lua 怎么用一张全局哈希表让所有相同的短串在内存里只存在一份。*
