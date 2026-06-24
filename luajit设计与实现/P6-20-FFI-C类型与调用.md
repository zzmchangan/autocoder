# P6-20 FFI:C 类型与调用

> **本书主线**:把动态执行安全变成机器码。**二分法**:解释器侧 ↔ JIT 侧。**本章位置**:JIT 侧(P6),LuaJIT 与外部(C 世界)的交互。**★对照**:官方 Lua(没有 FFI,只有 Lua C API)+ JVM/JNI、CPython ctypes/cffi。**源码**:LuaJIT 2.1.ROLLING。**基调**:纯直球,不用比喻;从第一性原理一步步推导。

---

## 引子:Lua 想直接调 C,凭什么

前面五章(P2–P5)讲的都是"Lua 自己的代码怎么被 JIT 编译"。但现实程序很少是纯 Lua——它要调系统的 `write`、要调图形库的 `glDrawArrays`、要调自己用 C 写的某个高性能内核。这些函数都是 C 函数,编译好的机器码,躺在 `.so`/`.dll`/`.dylib` 里,或者干脆就在可执行文件自己里面。

Lua 怎么调它们?这个问题,在不同语言实现里答案天差地别,而 LuaJIT 给出的答案——FFI(外部函数接口,Foreign Function Interface)——是它最出名的招牌特性之一。这一章和下一章,就是讲清楚 FFI 到底是怎么做到的。本章讲两个支柱:**C 的类型怎么在 LuaJIT 内部被表示出来**,**一次 C 调用到底是怎么发生的**。下一章 P6-21 讲更狠的:这种 C 调用还能被 JIT 编译进 trace,几乎零开销。

我们从最基础的地方开始,一步步把"为什么需要 FFI、FFI 长什么样、它怎么工作"推导出来。等你看清整条链路,再去看源码,就会发现没有任何魔法——每一步都有不得不如此的理由。

---

## §1 第一性原理:Lua 想调 C,传统方式为什么麻烦

先把问题摆清楚。假设你在 Lua 里想调 C 标准库的 `printf`:

```c
int printf(const char *format, ...);
```

这是 C 世界里一个再普通不过的函数。但 Lua 是另一门语言,它不能凭空"认识"`printf`。要让它能调,必须有人**在 Lua 和 C 之间架一座桥**。

### 1.1 传统方式:写一个 C 包装,注册给 Lua

官方 Lua(以及任何带 C API 的脚本语言)用的传统方式是这样的:你得**亲手用 C 写一个包装函数**,把 Lua 的调用约定翻译成 C 的调用约定,再把这个包装**注册**给 Lua。大概是这个样子(伪代码,概念示意):

```c
/* 第一步:用 C 写包装。这个函数符合 Lua C API 的签名 */
static int luaB_printf(lua_State *L) {
    const char *fmt = luaL_checkstring(L, 1);   /* 从 Lua 栈取出第 1 个参数 */
    /* ...还要手动把剩下的参数一个个取出来、转好类型... */
    int n = printf(fmt, /* ... */);              /* 真正调 C 的 printf */
    lua_pushinteger(L, n);                        /* 把返回值压回 Lua 栈 */
    return 1;                                     /* 告诉 Lua 返回了几个值 */
}

/* 第二步:把这个包装注册给 Lua,起个名字 */
lua_register(L, "printf", luaB_printf);
```

做完这两步,Lua 里就能写 `printf("hello %d", 42)` 了。

这条路能走通,但它有三个让人很难受的地方:

**第一,你必须为每一个想调的 C 函数,都手写这样一个包装。** 调 10 个 C 函数,写 10 个包装;调 100 个,写 100 个。每个包装的套路都差不多(取参数、转类型、调 C、压返回值),但又不能省——因为每个函数的参数个数、类型、返回类型都不一样。

**第二,包装本身是 C 代码,得单独编译、单独链接。** 你不能光靠 Lua 脚本就完成这件事,必须维护一个 C 扩展模块(动态库)。对纯脚本开发者来说,这是额外的工具链负担。

**第三,也是最要命的:每一次调用,都要在 Lua 栈和 C 调用之间来回搬运。** 看上面那个 `luaB_printf`:Lua 把参数放在它自己的"值栈"上(一种 LuaJIT 内部的数据结构,后面会看到),包装函数要用 `luaL_checkstring` 一个个从栈上取出来、转成 C 的 `const char *`;返回时又要 `lua_pushinteger` 把结果压回栈。这种"取一个参数就要调一次 API 函数、做一次类型检查、搬一次数据"的开销,在频繁调用时是实打实的性能损失。

这条路叫 **Lua C API**(或更一般的说法,叫 *栈式 FFI*)。官方 Lua 只有这一条路。它管用,但笨重。

### 1.2 那能不能让 Lua 直接调 C,不用写包装

自然会想:既然我都知道 `printf` 长什么样(`int printf(const char *, ...)`),也知道它编好的机器码在哪(动态库里某个符号),那能不能让 Lua **直接**准备好参数、直接跳过去执行那段机器码,根本不经过任何 C 包装?

这就是 LuaJIT FFI 的核心想法。它让你在 Lua 里**写一段 C 声明**,告诉 LuaJIT "有这么个 C 函数,签名是这样",然后 LuaJIT 就能**直接调用**它,不写一行 C 包装代码:

```lua
local ffi = require("ffi")

ffi.cdef [[
    int printf(const char *fmt, ...);     -- 声明 C 函数签名
]]

local C = ffi.C                            -- C = 默认命名空间(进程里的符号)
C.printf("hello %d\n", 42)                 -- 直接调用!
```

这段 Lua 代码,**没有**任何 C 包装函数,却成功调用了 C 标准库的 `printf`。这是怎么做到的?

要回答这个问题,得先解决两个更根本的子问题。这两个子问题,就是本章的两根支柱:

**子问题 A:LuaJIT 怎么"认识"`int printf(const char *, ...)`这串字符?** 也就是说,LuaJIT 内部必须有一套**能表示 C 类型**的东西——`int` 是什么、`const char *` 是什么、函数指针是什么、struct 是什么。C 的类型系统可不简单(有指针、数组、struct、union、enum、位域、变长参数……),LuaJIT 得用某种内部表示把这些都建模出来。这就是 **C 类型系统**(`lj_ctype`)。

**子问题 B:就算 LuaJIT 知道了 `printf` 的签名,它怎么"真的去调"那个编译好的 C 函数?** 调一个 C 函数,不是"跳到那个地址"那么简单——CPU 调用函数要遵守一套**调用约定(calling convention)**:前几个参数放哪些寄存器、剩下的放栈的哪里、浮点参数是不是要走另一组寄存器、返回值放哪、谁来清理栈……这些规则因 CPU 架构(x86/x64/ARM/ARM64/MIPS/PPC)和操作系统(Linux/Windows/macOS)而异,加起来有几十种组合。LuaJIT 必须把这些规则**逐个实现**对,才能真正"像 C 编译器生成的那样"去调一个 C 函数。这就是 **C 调用机制**(`lj_ccall`)。

本章就是讲透这两根支柱。我们先把它们合起来看一次完整流程,再分别钻进去。

### 1.3 一次完整调用:从声明到执行

用 `C.printf("hello %d\n", 42)` 这一行,把整条链路走一遍。它要经历四个阶段:

**阶段 1:解析声明,建立类型(`ffi.cdef` 时发生一次)。** 你写 `ffi.cdef("int printf(const char *fmt, ...);")`。LuaJIT 拿到这串字符,用一个**内置的 C 解析器**(`lj_cparse.c`)去分析它:识别出 `int`(返回类型)、`printf`(函数名)、`const char *`(第一个参数类型)、`...`(变长参数)。分析完,它把这些信息**存进内部的类型表**,生成若干个 `CType` 条目(类型表的一个元素),并给 `printf` 这个名字挂一个 `CT_EXTERN`(外部引用)的标记——意思是"这是外部 C 世界的一个东西,我这边只记个名字,真正的地址运行时再找"。这一步只发生一次,以后再调 `printf` 不用重新解析。

**阶段 2:找到函数地址(`ffi.C.printf` 时发生)。** 你写 `C.printf`。LuaJIT 去默认符号表(Linux/macOS 是进程自身的全局符号,Windows 是几个默认库)里查找 `printf` 这个名字,找到它编译好的机器码地址。然后造一个 **cdata 对象**(后面细讲)把这个地址包起来,挂在 `printf` 这个 key 上缓存起来。下次再 `C.printf` 就直接用缓存的 cdata,不用再查符号。

**阶段 3:准备参数,按调用约定摆好(每次调用发生)。** 你写 `C.printf("hello %d\n", 42)`。LuaJIT 现在**已经知道**这个函数的签名(阶段 1 存好的)和地址(阶段 2 找好的)。它要做的是:把 Lua 这边传的两个参数(`"hello %d\n"` 这个字符串、`42` 这个数字),**翻译成 C 调用约定要求的形式**。在 x64 Linux 上,这大致意味着:第一个参数(指针)放进 `rdi` 寄存器,第二个参数(整数)放进 `esi` 寄存器(它是变长参数,按整数规则走)。这些参数被摆进一个叫 `CCallState` 的结构里——你可以理解成一个"装着所有寄存器副本和栈槽的托盘"。

**阶段 4:真正跳过去执行,拿回返回值。** 一切就绪,LuaJIT 执行一条手写的汇编函数 `lj_vm_ffi_call`,它做的事很直白:把"托盘"里那些值**真的搬进 CPU 的物理寄存器**(比如 `mov rdi, [托盘.gpr[0]]`),然后 `call 那个函数地址`。C 函数执行完,返回值在 `rax`(整数)或 `xmm0`(浮点)里,汇编再把它**搬回托盘**。最后,LuaJIT 从托盘里取出返回值,**转回 Lua 的值**(比如 `int` 转成 Lua number),压回 Lua 栈,这一次调用就完成了。

这四个阶段,就是 FFI 一次调用的全貌。听起来很直接,但每一步都藏着精巧的设计。我们一个一个钻进去,从源码里印证。

---

## §2 C 类型系统:lj_ctype,怎么把 C 的类型装进 LuaJIT

先解决子问题 A:LuaJIT 怎么表示 C 的类型。

### 2.1 为什么需要一套专门的类型系统

你可能会想:Lua 自己不是有类型吗?number、string、table、function……为什么还要另搞一套?

因为 **C 的类型和 Lua 的类型,根本不是一回事**。Lua 是动态类型,运行时一个变量是什么类型,靠"值标签"区分(LuaJIT 用 `LJ_TCDATA` 这种 tag,后面会看到)。但 C 是**静态类型**,编译时每个变量的类型就定死了,而且 C 的类型远比 Lua 复杂:

- C 有 **不同位宽的整数**:`char`(1 字节)、`short`(2)、`int`(4)、`long long`(8),还有 `int128`(16)。Lua 只有一个 number(虽然 LuaJIT 内部区分整数/浮点,但那是实现细节)。
- C 有 **指针**:`int *`、`char *`、`void (*)(int)`(函数指针)。Lua 没有"指向内存地址"这种一等公民(除了 lightuserdata,但那很弱)。
- C 有 **聚合类型**:`struct`、`union`、`enum`、数组、位域(bitfield)。
- C 有 **类型限定符**:`const`、`volatile`。
- C 有 **调用约定**:`__cdecl`、`__stdcall`、`__fastcall`、`__thiscall`(x86 上)。

要让 Lua 能"声明一个 C 函数并调用它",LuaJIT **必须**能在内部精确地表示出上面所有这些 C 类型信息。否则它怎么知道调用时第一个参数该放寄存器还是栈?该按整数还是浮点?返回值该取几个字节?

所以 LuaJIT 专门做了一套 **C 类型系统**,代码主要在 `lj_ctype.c` / `lj_ctype.h`。这套系统的核心,是一个**类型表(type table)**,以及表里每个元素——`CType`。

### 2.2 类型表与 CType:用一个数组装下所有 C 类型

先看类型表本身。它在 `CTState` 结构里(`lj_ctype.h:174`):

```c
/* C type state. */
typedef struct CTState {
  CType *tab;       /* C type table. */
  CTypeID top;      /* Current top of C type table. */
  MSize sizetab;    /* Size of C type table. */
  lua_State *L;     /* Lua state (needed for errors and allocations). */
  global_State *g;  /* Global state. */
  GCtab *miscmap;   /* Map of -CTypeID to metatable and cb slot to func. */
  CCallback cb;     /* Temporary callback state. */
  CTypeID1 hash[CTHASH_SIZE];  /* Hash anchors for C type table. */
} CTState;
```

抓住三个字段:

- `tab`:这是一个 `CType` 数组,**所有** C 类型(不管是预定义的 `int`、`double`,还是你 `ffi.cdef` 声明的 `struct foo`)都作为这个数组里的一个元素存在。每个元素用它的下标——**CTypeID**(就是数组索引)——来引用。
- `top`:当前表里有多少个类型(下一个可用的 ID)。
- `hash`:一个哈希表,用来做**类型去重**(interning)。声明了两个一样的 `int`,不会建两个类型,而是复用同一个 ID。

再看表里的元素 `CType`(`lj_ctype.h:143`):

```c
/* C type table element. */
typedef struct CType {
  CTInfo info;    /* Type info. */
  CTSize size;    /* Type size or other info. */
  CTypeID1 sib;   /* Sibling element. */
  CTypeID1 next;  /* Next element in hash chain. */
  GCRef name;     /* Element name (GCstr). */
} CType;
```

这个结构体是整个类型系统的原子单位。我们逐字段看它为什么这么设计。

- **`info`(CTInfo,就是 `uint32_t`)**:这是**最关键**的字段,一个 32 位整数,**编码了这个类型的全部本质信息**——它是什么种类(int?指针?struct?)、带什么限定符(const?volatile?)、对齐是多少、如果是函数的话调用约定是什么、如果是指针的话指向哪个子类型……全部塞进这 32 位里。下一节专门讲它怎么编码。
- **`size`(CTSize,也是 `uint32_t`)**:这个类型占多少字节。`int` 是 4,`double` 是 8,指针是 4 或 8(看架构)。注意,对某些类型(如 `CT_FIELD` 字段),这个字段**复用**存别的东西(字段的偏移量);对 `CT_CONSTVAL`,存的是枚举常量的值。设计上不浪费字段。
- **`sib`(sibling,兄弟)**:指向**同一个父类型下的下一个成员**。比如一个 struct 有三个字段,这三个字段各是一个 `CType`,它们通过 `sib` 串成一条链,链头挂在 struct 这个 `CType` 上。函数的参数也这么串。这是 C 类型树形结构的"横向连接"。
- **`next`**:哈希链。用于类型去重查找时,把哈希到同一个桶的类型串起来。
- **`name`(GCRef,指向 GCstr)**:这个类型的名字(如果有)。比如 typedef 名字、struct 的 tag 名、字段名。

这里有个**精妙的设计**:为什么用一个扁平数组 + `sib`/`cid`(child id,编码在 info 里)来表示 C 的类型树,而不是用真正的指针链接(像 `struct CType *child`)?

两个理由。第一,**省内存**:一个 `CType` 才 16 字节,数组紧凑排布,缓存友好;用指针的话每个节点要多存指针、还要单独 malloc。第二,**稳定**:`cts->tab` 可能会因为类型变多而 realloc(地址变),如果用指针链接,realloc 后所有指针都得更新;用 ID(数组下标)引用,realloc 后 ID 不变,安全。这种"用 ID 而非指针引用内部对象"的思路,在 LuaJIT 里反复出现(IR 指令用 `IRRef`、trace 用 `TraceNo`,都是这个路子)。

### 2.3 CTInfo:32 位怎么装下一个类型的全部信息

现在钻进 `info` 这个 32 位整数。它是类型系统的灵魂。`lj_ctype.h:41` 有一张图,把 32 位的布局画了出来:

```
**  ---------- info ------------
** |type      flags...  A   cid | size   |  sib  | next  | name  |
```

最关键的是高 4 位(`CTMASK_NUM = 0xf0000000`,`CTSHIFT_NUM = 28`),存的是**类型种类**(`lj_ctype.h:17` 的 enum):

```c
enum {
  CT_NUM,       /* Integer or floating-point numbers. */
  CT_STRUCT,    /* Struct or union. */
  CT_PTR,       /* Pointer or reference. */
  CT_ARRAY,     /* Array or complex type. */
  CT_VOID,      /* Void type. */
  CT_ENUM,      /* Enumeration. */
  CT_FUNC,      /* Function. */
  CT_TYPEDEF,   /* Typedef. */
  CT_ATTRIB,    /* Miscellaneous attributes. */
  CT_FIELD,     /* Struct/union field or function parameter. */
  CT_BITFIELD,  /* Struct/union bitfield. */
  CT_CONSTVAL,  /* Constant value. */
  CT_EXTERN,    /* External reference. */
  CT_KW         /* Keyword. */
};
```

高 4 位能表示 16 种,够装下上面所有种类(还留了余量)。提取种类用宏 `ctype_type(info)`(`lj_ctype.h:189`):

```c
#define ctype_type(info)	((info) >> CTSHIFT_NUM)
```

为什么把种类放最高 4 位?因为这样**判断类型可以用一次掩码**,极快。比如判断是不是整数(`lj_ctype.h:217`):

```c
#define ctype_isinteger(info) \
  (((info) & (CTMASK_NUM|CTF_BOOL|CTF_FP)) == CTINFO(CT_NUM, 0))
```

一次 AND,一次比较,搞定。这种"把最常判断的信息放在高位、用位运算快速分类"的做法,是数据布局为性能服务的典型。

中间那些位(`flags`),放的是**限定符和对齐**(`lj_ctype.h:63` 起):

```c
#define CTF_BOOL     0x08000000u  /* Boolean: NUM, BITFIELD. */
#define CTF_FP       0x04000000u  /* Floating-point: NUM. */
#define CTF_CONST    0x02000000u  /* Const qualifier. */
#define CTF_VOLATILE 0x01000000u  /* Volatile qualifier. */
#define CTF_UNSIGNED 0x00800000u  /* Unsigned: NUM, BITFIELD. */
#define CTF_LONG     0x00400000u  /* Long: NUM. */
#define CTF_VLA      0x00100000u  /* Variable-length: ARRAY, STRUCT. */
...
#define CTF_ALIGN    (CTMASK_ALIGN<<CTSHIFT_ALIGN)  /* bits 16-19 */
```

注意一个**复用的细节**:同一个位,在不同类型种类下含义不同。比如 `0x08000000` 这个位,在 `CT_NUM` 下是 `CTF_BOOL`(布尔),在 `CT_ARRAY` 下是 `CTF_VECTOR`(向量),在 `CT_STRUCT` 下是 `CTF_UNION`(联合体)。这是因为不同种类需要的标志互不重叠,复用位能省空间。`lj_ctype.h:38` 那条 `LJ_STATIC_ASSERT` 就是在校验这种复用不会冲突:

```c
LJ_STATIC_ASSERT(((int)CT_PTR & (int)CT_ARRAY) == CT_PTR);
```

它保证 `CT_PTR` 和 `CT_ARRAY` 在二进制表示上有特定的位关系,从而 `ctype_ispointer` 这个判断可以一次掩码同时覆盖指针和数组(`lj_ctype.h:226`):

```c
#define ctype_ispointer(info) \
  ((ctype_type(info) >> 1) == (CT_PTR >> 1))  /* Pointer or array. */
```

这种位运算的花样,看着炫,本质是为了**让每一次类型判断都尽可能快**——因为 FFI 调用、转换、索引时,要无数次地判断"这个值是不是指针""那个字段是不是位域",慢一点累积起来就很可观。

最后是**低 16 位(`CTMASK_CID = 0x0000ffff`)**,存的是 **child id**——子类型的 CTypeID。一个指针 `int *`,它本身是一个 `CT_PTR` 类型的 CType,它的 `cid` 指向 `int` 那个 CType 的 ID。一个 struct 字段,它的 `cid` 指向字段本身的类型。这就是类型树的"纵向连接"。提取用 `ctype_cid`(`lj_ctype.h:190`):

```c
#define ctype_cid(info)	((CTypeID)((info) & CTMASK_CID))
```

把 `info` 这 32 位合起来看,它就是一个**自描述的类型标签**:高 4 位说我是什么种类,中间几位说我的限定符和对齐,低 16 位说我的子类型是谁。一个 32 位整数,装下了一个 C 类型节点的全部结构性信息。这是极致紧凑的数据设计。

### 2.4 预定义类型:开箱即用的基础类型

C 类型表不是空的——LuaJIT 启动时就**预先建好**一批基础类型(`int`、`char`、`double`、`void *`……),免得每次 `ffi.cdef` 都得从零造。这批预定义类型列在 `CTTYDEF` 宏里(`lj_ctype.h:281`):

```c
#define CTTYDEF(_) \
  _(NONE,       0,  CT_ATTRIB, CTATTRIB(CTA_BAD)) \
  _(VOID,      -1,  CT_VOID, CTALIGN(0)) \
  _(CVOID,     -1,  CT_VOID, CTF_CONST|CTALIGN(0)) \
  _(BOOL,       1,  CT_NUM, CTF_BOOL|CTF_UNSIGNED|CTALIGN(0)) \
  _(CCHAR,      1,  CT_NUM, CTF_CONST|CTF_UCHAR|CTALIGN(0)) \
  _(INT8,       1,  CT_NUM, CTALIGN(0)) \
  _(UINT8,      1,  CT_NUM, CTF_UNSIGNED|CTALIGN(0)) \
  _(INT16,      2,  CT_NUM, CTALIGN(1)) \
  _(UINT16,     2,  CT_NUM, CTF_UNSIGNED|CTALIGN(1)) \
  _(INT32,      4,  CT_NUM, CTALIGN(2)) \
  _(UINT32,     4,  CT_NUM, CTF_UNSIGNED|CTALIGN(2)) \
  _(INT64,      8,  CT_NUM, CTF_LONG_IF8|CTALIGN(3)) \
  _(UINT64,     8,  CT_NUM, CTF_UNSIGNED|CTF_LONG_IF8|CTALIGN(3)) \
  ...
  _(FLOAT,      4,  CT_NUM, CTF_FP|CTALIGN(2)) \
  _(DOUBLE,     8,  CT_NUM, CTF_FP|CTALIGN(3)) \
  ...
  _(P_VOID,  CTSIZE_PTR, CT_PTR, CTALIGN_PTR|CTID_VOID) \
  ...
```

每一行是一个预定义类型:名字、大小、种类+标志。比如 `INT32` 是大小为 4 的 `CT_NUM`,`DOUBLE` 是大小为 8 带 `CTF_FP` 标志的 `CT_NUM`。这些宏展开后生成一个数组 `lj_ctype_typeinfo`(`lj_ctype.c:118`),启动时填进类型表。

这些预定义类型有固定的 ID,枚举在 `lj_ctype.h:311`:

```c
enum {
#define CTTYIDDEF(id, sz, ct, info)	CTID_##id,
CTTYDEF(CTTYIDDEF)
#undef CTTYIDDEF
  CTID_MAX = 65536
};
```

所以代码里可以直接写 `CTID_INT32`、`CTID_DOUBLE`、`CTID_P_VOID`,不用查表——它们的 ID 在编译时就定死了。注意一个**架构相关**的点:`CTSIZE_PTR` 在 64 位下是 8,32 位下是 4(`lj_ctype.h:249`),所以 `P_VOID`(void 指针)的大小跟着架构走。同样,`CTF_LONG_IF8`(`lj_ctype.h:278`)只在 `sizeof(long)==8` 的系统(典型是 64 位 Linux)上才置 `CTF_LONG`——因为 Linux 64 位下 `long` 是 8 字节(LP64),而 Windows 64 位下 `long` 仍是 4 字节(LLP64)。这种平台差异,被一个宏干净地处理掉。

### 2.5 创建新类型:lj_ctype_new 与 lj_ctype_intern

`ffi.cdef` 解析出一个新类型(比如你声明的 `struct foo { int a; }`)后,要把它**加进类型表**。两个函数负责这事。

第一个,`lj_ctype_new`(`lj_ctype.c:155`),用于**创建带名字的、用户可见的类型**(struct、typedef、extern):

```c
CTypeID lj_ctype_new(CTState *cts, CType **ctp)
{
  CTypeID id = cts->top;
  CType *ct;
  if (LJ_UNLIKELY(id >= cts->sizetab)) {    /* 表满了,扩容 */
    if (id >= CTID_MAX) lj_err_msg(cts->L, LJ_ERR_TABOV);
    lj_mem_growvec(cts->L, cts->tab, cts->sizetab, CTID_MAX, CType);
  }
  cts->top = id+1;
  *ctp = ct = &cts->tab[id];
  ct->info = 0; ct->size = 0; ct->sib = 0; ct->next = 0;
  setgcrefnull(ct->name);
  return id;
}
```

它做的事很直白:取下一个可用 ID(必要时扩容),返回一个**清零的**新 `CType` 给调用者填。调用者填好 `info`、`size`、`name` 后,再调 `ctype_addname` 把它挂进哈希表。注意它返回的是 **ID**,而通过 `*ctp` 输出参数给调用者一个**指针**——这是个细节但重要:返回指针是让调用者方便填字段,返回 ID 是因为 ID 才是稳定的引用(realloc 后指针会失效,但 ID 不会)。

第二个,`lj_ctype_intern`(`lj_ctype.c:184`),用于**内部类型去重**(interning):

```c
CTypeID lj_ctype_intern(CTState *cts, CTInfo info, CTSize size)
{
  uint32_t h = ct_hashtype(info, size);
  CTypeID id = cts->hash[h];
  while (id) {                              /* 查哈希链 */
    CType *ct = ctype_get(cts, id);
    if (ct->info == info && ct->size == size)
      return id;                            /* 已存在,复用 */
    id = ct->next;
  }
  /* 不存在,新建一个并挂进哈希链 */
  ...
}
```

它的逻辑是:**先查哈希表,看有没有 `info` 和 `size` 都相同的类型**;有就直接返回那个 ID,没有才新建。为什么需要它?因为解析 C 声明时,会产生大量**匿名内部类型**。比如你声明两个函数都是 `int (*)(double)`,它们指向的函数类型是一样的,LuaJIT 不需要建两个——建一个,复用即可。这种"结构相同的类型只存一份"的做法叫**类型驻留**(type interning),和字符串 intern 是同一个思想:省内存、方便比较(两个 ID 相等就说明类型完全一样)。

这两个函数配合,类型表既能为用户类型(struct/typedef)建独立条目,又能对内部类型自动去重。这是类型系统能高效运作的基础。

---

## §3 cdata:C 的值在 Lua 世界里的化身

讲完了类型,讲**值**。C 的类型是静态的"图纸",但运行时你要有具体的"实例"——一个 `int` 变量、一个 `struct foo` 实例、一个指向某处的指针。这些 C 的**值**,在 LuaJIT 里用什么表示?

答案是 **cdata**(C data)。它是一种 LuaJIT 特有的**值类型**,专门用来承载 C 的数据。

### 3.1 GCcdata:一个挂在 GC 上的对象

cdata 对象的结构,在 `lj_obj.h:348`:

```c
/* C data object. Payload follows. */
typedef struct GCcdata {
  GCHeader;
  uint16_t ctypeid;	/* C type ID. */
} GCcdata;
```

极其简洁:一个 `GCHeader`(所有 GC 对象都有的头部,含类型 tag 和 GC 标记),加一个 `ctypeid`(说"这个 cdata 是哪种 C 类型")。就这两样?

注意那行注释 `/* Payload follows. */`——**真正的数据紧贴在结构体后面**。也就是说,一个 `GCcdata` 在内存里是这样布局的:

```
[ GCHeader | ctypeid | <payload: 真正的 C 数据> ]
```

访问 payload 用宏 `cdataptr`(`lj_obj.h:360`):

```c
#define cdataptr(cd)	((void *)((cd)+1))
```

`(cd)+1` 就是跳过整个 `GCcdata` 头,指向紧跟其后的 payload。这种"头部 + 变长 payload"的布局,和 `GCstr`(字符串头 + 字符数据)、`GCudata`(用户数据头 + 数据)是同一个套路——紧凑、一次分配、缓存友好。

这个 cdata 在 Lua 值的类型 tag 上,挂的是 `LJ_TCDATA`(`lj_obj.h:270`):

```c
#define LJ_TCDATA		(~10u)
```

所以判断一个 Lua 值是不是 cdata,就是看它的 tag 是不是 `LJ_TCDATA`(`lj_obj.h:801`):

```c
#define tviscdata(o)	(itype(o) == LJ_TCDATA)
```

这一点很重要:**cdata 是 Lua 的一等值**。你可以把它赋给变量、放进 table、当参数传——和 number、string 一样。只不过它的 tag 是 `LJ_TCDATA`,内部还带着一个 ctypeid 说明它是哪种 C 数据。这种"给 C 数据一个 Lua 值身份"的设计,是 FFI 能和 Lua 无缝衔接的关键:cdata 不需要特殊的"外部"表示,它就是 Lua 值栈上一个普通的格子。

### 3.2 创建 cdata:固定大小与变长两种

创建 cdata,看 `lj_cdata.h:38`:

```c
static LJ_AINLINE GCcdata *lj_cdata_new(CTState *cts, CTypeID id, CTSize sz)
{
  GCcdata *cd;
  cd = (GCcdata *)lj_mem_newgco(cts->L, sizeof(GCcdata) + sz);
  cd->gct = ~LJ_TCDATA;
  cd->ctypeid = ctype_check(cts, id);
  return cd;
}
```

分配 `sizeof(GCcdata) + sz` 字节(头 + payload),设好 tag 和 ctypeid,返回。这是**固定大小**的 cdata——payload 大小在创建时就知道(比如 `int` 是 4,`double` 是 8)。

但有些 cdata **大小运行时才知道**:变长数组(VLA)、`char[n]` 里 n 是变量、或者需要特殊对齐的(比如 16 字节对齐的 `__m128`)。这些用 `lj_cdata_newv`(`lj_cdata.c:29`),它多了一个 `GCcdataVar` 前缀(`lj_obj.h:354`)记录实际大小和对齐:

```c
/* Prepended to variable-sized or realigned C data objects. */
typedef struct GCcdataVar {
  uint16_t offset;	/* Offset to allocated memory (relative to GCcdata). */
  uint16_t extra;	/* Extra space allocated (incl. GCcdata + GCcdatav). */
  MSize len;		/* Size of payload. */
} GCcdataVar;
```

注意 `offset` 字段——因为对齐要求,真正的 `GCcdata` 头可能不在分配内存的开头,而是往后偏移了一点(为了对齐 payload)。`offset` 记录这个偏移,这样释放时能算回真正的分配起点(`memcdatav` 宏,`lj_obj.h:365`)。

判断一个 cdata 是不是变长的,看它的 `marked` 字段的 0x80 位(`lj_obj.h:361`):

```c
#define cdataisv(cd)	((cd)->marked & 0x80)
```

这是个**复用 GC 标记位**的技巧——`marked` 本来是 GC 用的(标记白/灰/黑),LuaJIT 借了其中一位(`0x80`)来区分"这个 cdata 是不是变长的"。因为 GC 的三色标记只用低几位,这位空闲,就拿来复用。

### 3.3 cdata 怎么被 Lua 访问:索引与元方法

cdata 是 Lua 值,但 C 的数据结构(struct 的字段、指针指向的内容、数组的元素)怎么被 Lua 代码访问?比如:

```lua
ffi.cdef("struct Point { int x; int y; };")
local p = ffi.new("struct Point")   -- p 是一个 cdata
p.x = 10                            -- 怎么做到的?
print(p.y)                          -- 怎么读?
```

这靠 **元方法(metamethod)**。每种 cdata 类型可以挂一个 metatable,里面定义 `__index`(读)、`__newindex`(写)、`__add`(加)等。`ffi.metatype` 就是干这个的(`lib_ffi.c:773`)。

但底层的实际索引逻辑,在 `lj_cdata_index`(`lj_cdata.c:109`)。这个函数是 FFI 数据访问的核心:给它一个 cdata 和一个 key(字符串或数字),它算出**这个 key 对应的内存地址和类型**。逻辑大致是:

- 先看 key 是数字还是字符串。
- 数字 key:说明在索引数组/指针。算出元素大小,`地址 + idx * 元素大小` 就是目标。
- 字符串 key:说明在访问 struct 字段。在 struct 的字段链(`sib`)里找名字匹配的,找到后用它的 `size` 字段(这里存的是**字段偏移**)算出地址。
- 如果是指针类型的 cdata,还要先**解引用**(`cdata_getptr`),拿到它指向的真正数据再索引。

找到地址和类型后,真正的读写交由 `lj_cdata_get` / `lj_cdata_set`(`lj_cdata.c:221` / `256`),它们再调 `lj_cconv`(下一节)在 C 值和 Lua 值之间转换。

这一整套机制,让 Lua 能**像访问 table 一样访问 C 结构体**——`p.x` 背后发生了一大堆事(算偏移、查类型、转值),但对 Lua 用户透明。这是 FFI 易用性的根源。

---

## §4 lj_cparse:把 C 声明解析成类型

现在解决"子问题 A 的入口":那段 `ffi.cdef("int printf(...)")` 的字符串,是怎么变成类型表里的条目的?

答案是一个**内置的 C 解析器**,在 `lj_cparse.c`(1934 行)。LuaJIT 自己实现了一个能解析 C 声明语法的解析器,不依赖外部工具。

### 4.1 入口:ffi.cdef → lj_cparse

从 Lua 侧看,`ffi.cdef` 是个 Lua 函数,实现在 `lib_ffi.c:476`:

```c
LJLIB_CF(ffi_cdef)
{
  GCstr *s = lj_lib_checkstr(L, 1);
  CPState cp;
  int errcode;
  cp.L = L;
  cp.cts = ctype_cts(L);
  cp.srcname = strdata(s);
  cp.p = strdata(s);
  cp.param = L->base+1;
  cp.mode = CPARSE_MODE_MULTI|CPARSE_MODE_DIRECT;
  errcode = lj_cparse(&cp);
  if (errcode) lj_err_throw(L, errcode);  /* Propagate errors. */
  lj_gc_check(L);
  return 0;
}
```

很直白:取出字符串参数,初始化一个 `CPState`(解析器状态),调 `lj_cparse`。`lj_cparse` 本身(`lj_cparse.c:1924`)是个薄包装:

```c
int lj_cparse(CPState *cp)
{
  LJ_CTYPE_SAVE(cp->cts);
  int errcode = lj_vm_cpcall(cp->L, NULL, cp, cpcparser);
  if (errcode)
    LJ_CTYPE_RESTORE(cp->cts);
  cp_cleanup(cp);
  return errcode;
}
```

注意两个细节。第一,`LJ_CTYPE_SAVE` / `LJ_CTYPE_RESTORE`(`lj_ctype.h:407`):解析前**保存**类型表的状态(当前 `top` 和 `hash`),解析失败时**回滚**。为什么?因为解析是**试探性**的——可能解析到一半发现语法错误,这时已经往类型表里塞了些半成品类型。回滚保证类型表不会留下垃圾。第二,`lj_vm_cpcall`:这是一个 LuaJIT 内部的"受保护调用"机制,保证解析器抛错时能被 Lua 的错误处理接住(不会直接 crash)。这两个机制合起来,让 `ffi.cdef` 即使写了非法的 C 声明,也只是抛个 Lua 错误,不会搞坏类型表或崩溃。

### 4.2 解析的主体:词法 + 声明语法

真正的解析在 `cpcparser`(`lj_cparse.c:1907`),它根据 mode 调 `cp_decl_multi`(多条声明)或 `cp_decl_single`(单条类型表达式)。`ffi.cdef` 用的是 multi 模式,所以走 `cp_decl_multi`(`lj_cparse.c:1813`)。

`cp_decl_multi` 的主循环,逻辑就是教科书式的 C 声明解析:

```c
static void cp_decl_multi(CPState *cp)
{
  int first = 1;
  while (cp->tok != CTOK_EOF) {
    CPDecl decl;
    CPscl scl;
    if (cp_opt(cp, ';')) { first = 0; continue; }   /* 跳过空语句 */
    if (cp->tok == '#') { /* 处理 #line/#pragma */ ... }
    scl = cp_decl_spec(cp, &decl, CDF_TYPEDEF|CDF_EXTERN|CDF_STATIC);
    /* ... 处理 struct/enum 空声明 ... */
    for (;;) {
      CTypeID ctypeid;
      cp_declarator(cp, &decl);
      ctypeid = cp_decl_intern(cp, &decl);
      if (decl.name && !decl.nameid) {
        CType *ct; CTypeID id;
        if ((scl & CDF_TYPEDEF)) {            /* typedef */
          id = lj_ctype_new(cp->cts, &ct);
          ct->info = CTINFO(CT_TYPEDEF, ctypeid);
          goto noredir;
        } else if (ctype_isfunc(ctype_get(cp->cts, ctypeid)->info)) {
          /* static/extern 函数声明都当 extern 处理 */
          ct = ctype_get(cp->cts, ctypeid);
          id = ctypeid;
        } else if ((scl & CDF_STATIC)) {       /* static 常量 */
          id = cp_decl_constinit(cp, &ct, ctypeid);
          goto noredir;
        } else {                                /* extern 或无存储类 */
          id = lj_ctype_new(cp->cts, &ct);
          ct->info = CTINFO(CT_EXTERN, ctypeid);
        }
        /* ... 处理符号重定向属性 ... */
      noredir:
        ctype_setname(ct, decl.name);
        lj_ctype_addname(cp->cts, ct, id);
      }
      if (!cp_opt(cp, ',')) break;
      cp_decl_reset(&decl);
    }
    ...
  }
}
```

抓住主脉络。C 声明的语法(去掉各种边角)是:`声明说明符 声明符 [, 声明符]* ;`。比如 `int a, b, c;` 是一个说明符 `int` 带三个声明符。这段代码精确对应这个结构:

1. `cp_decl_spec`:解析**声明说明符**(类型说明符 `int`/`struct foo`/限定符 `const`/存储类 `extern`)。它返回 `scl`,告诉你这次声明是 typedef 还是 extern 还是 static。这是 C 语法的"左半边"。
2. `cp_declarator`:解析**声明符**(变量名、指针的 `*`、数组的 `[]`、函数的 `()`)。C 声明符的语法出了名的绕(`int (*fp)(double)` 这种),LuaJIT 用一个栈式的方法处理它(声明符的修饰从外到内压栈,再 `cp_decl_intern` 从内到外组合成类型)。
3. `cp_decl_intern`:把声明符栈和说明符组合成**最终的 CTypeID**。这是"把语法树拍平成类型"的关键一步。
4. 根据存储类,把名字挂到类型表:`typedef` 建 `CT_TYPEDEF`,函数直接命名,`extern`/无存储类建 `CT_EXTERN`,`static` 建常量。

这里有一个**对 FFI 调用至关重要**的细节,看这段代码第 1858-1870 行:对于**函数声明**,无论是 `static` 还是 `extern`,LuaJIT 都**当成 extern 处理**(`/* Treat both static and extern function declarations as extern. */`)。为什么?因为 FFI 调用的本质是"按符号去找地址",而 `static` 在 C 里是"文件内可见",对 LuaJIT 来说没有"文件"概念,只能去进程符号表找——所以统统当 extern。这是 FFI 语义对 C 语义的一个**有意简化**。

再看 `printf` 这个具体例子。你写 `ffi.cdef("int printf(const char *fmt, ...);")`,解析过程是:

- `cp_decl_spec`:读到 `int`,说明符是 `int`(CT_NUM, 4 字节),存储类无(默认 extern)。
- `cp_declarator`:读到 `printf`,然后 `(`,进入函数声明符解析。参数列表里:`const char *fmt` 解析成一个 `CT_PTR → CT_NUM(char, const)` 的字段类型;`...` 标记变长参数(`CTF_VARARG`)。函数声明符压栈。
- `cp_decl_intern`:组合出一个 `CT_FUNC` 类型,info 里带 `CTF_VARARG`,参数字段通过 `sib` 串起来,`cid` 指向返回类型 `int`。
- 存储类是"函数且无 typedef",走 `CT_EXTERN` 分支:建一个 `CT_EXTERN` 的 CType,name 设为 `"printf"`,挂进哈希表。

至此,`printf` 这个名字就在类型表里有了一个条目,记录着"这是一个 extern 的 C 函数,签名是 `int (const char *, ...)`"。后续调用全靠这个条目。

---

## §5 lj_ccall:真正去调那个 C 函数

类型有了,地址怎么找、参数怎么摆、函数怎么调——这是子问题 B,本章的另一半重头戏。代码在 `lj_ccall.c`(1263 行)。

### 5.1 入口:lj_ccall_func

从 Lua 侧调 `C.printf(...)` 时,最终走到 `lj_ccall_func`(`lj_ccall.c:1225`):

```c
int lj_ccall_func(lua_State *L, GCcdata *cd)
{
  CTState *cts = ctype_cts(L);
  CType *ct = ctype_raw(cts, cd->ctypeid);
  CTSize sz = CTSIZE_PTR;
  if (ctype_isptr(ct->info)) {
    sz = ct->size;
    ct = ctype_rawchild(cts, ct);
  }
  if (ctype_isfunc(ct->info)) {
    CTypeID id = ctype_typeid(cts, ct);
    CCallState cc;
    int gcsteps, ret;
    cc.func = (void (*)(void))cdata_getptr(cdataptr(cd), sz);
    gcsteps = ccall_set_args(L, cts, ct, &cc);
    cts->cb.slot = ~0u;
    lj_vm_ffi_call(&cc);
    if (cts->cb.slot != ~0u) {  /* Blacklist function that called a callback. */
      ...
    }
    ct = ctype_get(cts, id);  /* Table may have been reallocated. */
    gcsteps += ccall_get_results(L, cts, ct, &cc, &ret);
    ...
    while (gcsteps-- > 0)
      lj_gc_check(L);
    return ret;
  }
  return -1;  /* Not a function. */
}
```

这个函数是 FFI 调用的总调度。它的逻辑清晰对应我们 §1.3 说的四个阶段:

1. **拿函数类型**:`ctype_raw(cts, cd->ctypeid)` 拿到这个 cdata 的原始类型。如果是个指针(函数指针通常包装成指针),`ctype_rawchild` 解引用拿到它指向的函数类型。
2. **拿函数地址**:`cc.func = cdata_getptr(cdataptr(cd), sz)`——从 cdata 的 payload 里取出那个指针值(就是函数地址),存进 `cc.func`。
3. **准备参数**:`ccall_set_args`(下一节细讲)——把 Lua 栈上的参数,按调用约定摆进 `CCallState`。
4. **真正调用**:`lj_vm_ffi_call(&cc)`——这是手写汇编,把 `cc` 里的值搬进物理寄存器,`call` 那个函数地址。
5. **取回返回值**:`ccall_get_results`——把 C 函数的返回值(在 `cc` 的寄存器副本里)转回 Lua 值。

注意第 1247 行那条注释 `/* Table may have been reallocated. */` 和它前面的 `ct = ctype_get(cts, id);`。这是一个**容易踩的坑**:`ccall_set_args` 里会调 `lj_cconv_ct_tv`,而后者可能创建新的 cdata(比如参数要 pass-by-reference),创建 cdata 可能触发 GC,GC 可能触发类型表的某些操作……更直接的是,`lj_ccall_ctid_vararg`(`lj_ccall.c:942`)会调 `lj_ctype_intern` **新建类型**,这可能让 `cts->tab` 扩容(realloc),从而**之前拿到的 `ct` 指针失效**!所以调用后必须用 ID **重新取**一遍 `ct` 指针。这正是 §2.2 说的"用 ID 而非指针引用内部对象"的价值——ID 稳定,指针不稳。

### 5.2 CCallState:装着调用所需的一切

真正执行调用的汇编,需要知道:函数地址多少、前几个整数参数放哪、前几个浮点参数放哪、栈上放多少、返回值怎么取。这些信息全装在 `CCallState` 里(`lj_ccall.h:166`):

```c
typedef LJ_ALIGN(CCALL_ALIGN_CALLSTATE) struct CCallState {
  void (*func)(void);		/* Pointer to called function. */
  uint32_t spadj;		/* Stack pointer adjustment. */
  uint8_t nsp;			/* Number of bytes on stack. */
  uint8_t retref;		/* Return value by reference. */
#if LJ_TARGET_X64
  uint8_t ngpr;			/* Number of arguments in GPRs. */
  uint8_t nfpr;			/* Number of arguments in FPRs. */
#elif LJ_TARGET_X86
  uint8_t resx87;		/* Result on x87 stack: 1:float, 2:double. */
#elif LJ_TARGET_ARM64
  void *retp;			/* Aggregate return pointer in x8. */
...
#elif LJ_TARGET_PPC
  uint8_t nfpr;
#endif
#if CCALL_NUM_FPR
  FPRArg fpr[CCALL_NUM_FPR];	/* Arguments/results in FPRs. */
#endif
  GPRArg gpr[CCALL_NUM_GPR];	/* Arguments/results in GPRs. */
  GPRArg stack[CCALL_NUM_STACK];	/* Stack slots. */
} CCallState;
```

把它想成一个"调用托盘",装着:

- `func`:要调的函数地址。
- `gpr[]`:整数/通用寄存器的副本数组。x64 Linux 是 6 个(`CCALL_NARG_GPR=6`,`lj_ccall.h:31`),Windows x64 是 4 个,ARM64 是 8 个。
- `fpr[]`:浮点/SSE 寄存器的副本数组。x64 Linux 是 8 个,Windows x64 是 4 个,ARM64 是 8 个。
- `stack[]`:栈槽数组。放不进寄存器的参数,放这。最多 31 个槽(`CCALL_NUM_STACK=31`,`lj_ccall.h:161`)。
- `nsp`:实际用了多少字节栈。
- 各种架构特有的小字段(`resx87` 给 x86 的浮点返回、`retp` 给 ARM64 的聚合返回……)。

注意这个结构体**有对齐要求**(`LJ_ALIGN(CCALL_ALIGN_CALLSTATE)`,通常 16 字节)。为什么?因为 `fpr[]` 里要放 16 字节的 SSE 寄存器值(`movaps` 指令要求 16 字节对齐),不对齐会触发对齐错误。这种"为了某条汇编指令的对齐要求而强制结构体对齐"的细节,是系统编程里典型的"为机器服务"的设计。

各架构的寄存器数量(`CCALL_NARG_GPR` / `CCALL_NARG_FPR`)在 `lj_ccall.h:16` 起的 `#if` 链里定义,完全对应各 ABI 文档:

```c
#elif LJ_ABI_WIN       /* Windows/x64 */
#define CCALL_NARG_GPR		4   /* rcx,rdx,r8,r9 */
#define CCALL_NARG_FPR		4   /* xmm0-xmm3 */
...
#else                   /* POSIX/x64 (Linux/macOS/SysV) */
#define CCALL_NARG_GPR		6   /* rdi,rsi,rdx,rcx,r8,r9 */
#define CCALL_NARG_FPR		8   /* xmm0-xmm7 */
```

这些数字不是 LuaJIT 拍脑袋定的,是 **x64 ABI 规范**写死的:POSIX x64 前 6 个整数参数走 rdi/rsi/rdx/rcx/r8/r9,前 8 个浮点走 xmm0-7;Windows x64 前 4 个走 rcx/rdx/r8/r9(不分整数浮点,位置占用)。LuaJIT 必须严格遵守,否则调 C 函数会拿到乱码参数。

### 5.3 ccall_set_args:按调用约定摆参数

最复杂的逻辑在 `ccall_set_args`(`lj_ccall.c:972`),它负责把 Lua 栈上的参数,**逐个**按调用约定摆进 `CCallState`。这是个 200 多行的大函数,但主干逻辑可以拆清楚。

它做的事,一句话:**遍历每个参数,决定它该进哪个寄存器还是栈,然后转换并摆好**。

```c
static int ccall_set_args(lua_State *L, CTState *cts, CType *ct, CCallState *cc)
{
  ...
  /* 遍历每个参数 */
  for (o = L->base+1, narg = 1; o < top; o++, narg++) {
    CTypeID did; CType *d; CTSize sz;
    MSize n, isfp = 0, isva = 0;
    void *dp, *rp = NULL;
    ...
    if (fid) {            /* 从函数签名拿参数类型 */
      CType *ctf = ctype_get(cts, fid);
      fid = ctf->sib;
      did = ctype_cid(ctf->info);
    } else {              /* 变长参数:推断类型 */
      if (!(info & CTF_VARARG))
        lj_err_caller(L, LJ_ERR_FFI_NUMARG);
      did = lj_ccall_ctid_vararg(cts, o);
      isva = 1;
    }
    d = ctype_raw(cts, did);
    sz = d->size;
    ...
    /* 决定怎么传这个参数 */
    if (ctype_isnum(d->info)) {
      if ((d->info & CTF_FP)) isfp = 1;     /* 浮点 → FPR */
    } else if (ctype_isstruct(d->info)) {
      CCALL_HANDLE_STRUCTARG             /* struct 传递(各 ABI 不同) */
    } else if (ctype_iscomplex(d->info)) {
      CCALL_HANDLE_COMPLEXARG
    } ...
    n = (sz + CTSIZE_PTR-1) / CTSIZE_PTR;   /* 占几个寄存器/槽 */
    
    CCALL_HANDLE_REGARG  /* 核心宏:尝试放进寄存器 */
    
    /* 放不进寄存器,放栈 */
    dp = ((uint8_t *)cc->stack) + nsp;
    nsp += ...;
    ...
    lj_cconv_ct_tv(cts, d, (uint8_t *)dp, o, CCF_ARG(narg));  /* 真正转换 */
    ...
  }
  ...
}
```

抓住三个关键点。

**第一,参数类型从哪来。** 函数签名(`ct` 的 `sib` 链)记录了每个参数的类型。`fid` 沿着这条链走,每走一步拿到一个参数的类型 ID。如果是变长参数(`...`),签名里没有,就用 `lj_ccall_ctid_vararg`(`lj_ccall.c:942`)**推断**:

```c
CTypeID lj_ccall_ctid_vararg(CTState *cts, cTValue *o)
{
  if (tvisnumber(o)) {
    return CTID_DOUBLE;                    /* 数字 → double(C 默认提升规则) */
  } else if (tviscdata(o)) {
    ...
  } else if (tvisstr(o)) {
    return CTID_P_CCHAR;                   /* 字符串 → const char * */
  } else if (tvisbool(o)) {
    return CTID_BOOL;
  } else {
    return CTID_P_VOID;
  }
}
```

这对应 C 的**默认参数提升**(default argument promotions):变长参数里的 `float` 会提升成 `double`,`char`/`short` 提升成 `int`。所以 Lua 里的数字传给 `printf` 的 `%d`,被当成 `double`(8 字节)传——这也是为什么 `printf("%d", 42)` 能工作,虽然 42 看起来是 int。这个细节,是 FFI 遵守 C ABI 的体现。

**第二,参数往哪放。** 这是 `CCALL_HANDLE_REGARG` 宏干的活。这个宏**每个架构都不同**,定义在 `lj_ccall.c` 开头那一大堆 `#if LJ_TARGET_X64` 里(`lj_ccall.c:20-582`)。看 POSIX x64 的(`lj_ccall.c:178`):

```c
#define CCALL_HANDLE_REGARG \
  if (isfp) {  /* 浮点:尝试进 FPR */ \
    int n2 = ctype_isvector(d->info) ? 1 : n; \
    if (nfpr + n2 <= CCALL_NARG_FPR) { \
      dp = &cc->fpr[nfpr]; \
      nfpr += n2; \
      goto done; \
    } \
  } else {  /* 整数:尝试进 GPR */ \
    if (!onstack && n <= 2 && ngpr + n <= maxgpr) { \
      dp = &cc->gpr[ngpr]; \
      ngpr += n; \
      goto done; \
    } \
  }
```

它做的事:浮点参数尝试塞进 `fpr[]`,整数参数尝试塞进 `gpr[]`,塞得下就用 `goto done` 跳过栈分配。塞不下(寄存器用完,或者参数太大)就落到后面,放 `stack[]`。

注意 POSIX x64 那行注释 `/* Note that reordering is explicitly allowed in the x64 ABI. */`(`lj_ccall.c:187`)。这是个 **ABI 细节**:POSIX x64 允许**整数参数和浮点参数重新排序**——比如 `f(int, double, int)`,整数参数可以"跳过"中间的 double 继续用下一个 GPR。而 Windows x64 **不允许**重排序,整数和浮点共享同一组位置(`lj_ccall.c:128`)。这种 ABI 差异,全靠 `CCALL_HANDLE_REGARG` 在不同架构下的不同实现来吸收。

**第三,struct/complex 的特殊处理。** struct 怎么传,是各 ABI 最头疼的部分。x64 POSIX 会把小 struct(<=16 字节)按字段**分类**(整数类/SSE 类/内存类),拆进寄存器;大 struct 走引用。Windows x64 把 <=8 字节的 struct 放一个 GPR,大的走引用。ARM64 有 HFA(Homogeneous Float Aggregate)规则。这些规则,全在 `CCALL_HANDLE_STRUCTRET` / `CCALL_HANDLE_STRUCTARG` / `ccall_classify_struct` 这些宏和函数里(`lj_ccall.c:625-857`)。代码很长,但本质就是**把 C 编译器在处理 struct 参数时做的那些 ABI 规定,原样复刻一遍**。这是 FFI"遵守 C ABI"最难的部分,也是为什么 `lj_ccall.c` 有 1263 行——大半都在处理各架构 struct/complex 的传递规则。

### 5.4 真正的调用:lj_vm_ffi_call 的汇编

参数都摆进 `CCallState` 后,真正去调 C 函数,是 `lj_vm_ffi_call`(`lj_ccall.h:194` 声明)。它是一个**手写汇编函数**,各架构在 `vm_x64.dasc` / `vm_x86.dasc` / `vm_arm64.dasc` 等里。

看 POSIX x64 的实现(`vm_x64.dasc:2823`,这里把 dynasm 记法转写成等价的 x64 汇编形式来读):

```asm
->vm_ffi_call:                    // 入口,参数(CCallState*)在 rdi
  push rbp; mov rbp, rsp
  push rbx
  mov rbx, rdi                     // rbx = &CCallState (保存到被调用者保存寄存器)

  // 1. 调整栈:按 spadj 预留参数空间
  mov eax, [rbx + offsetof(CCallState, spadj)]
  sub rsp, rax

  // 2. 把 stack[] 里的参数复制到真正的栈上
  movzx ecx, byte [rbx + offsetof(CCallState, nsp)]
  sub ecx, 8
  js >2                            // nsp <= 8(都在寄存器)就跳过
1:
  mov rax, [rbx + rcx + offsetof(CCallState, stack)]
  mov [rsp + rcx + CCALL_SPS_EXTRA*8], rax
  sub ecx, 8
  jns <1
2:

  // 3. 装载整数参数寄存器
  movzx eax, byte [rbx + offsetof(CCallState, nfpr)]
  mov rdi, [rbx + offsetof(CCallState, gpr) + 0]   // CARG1
  mov rsi, [rbx + offsetof(CCallState, gpr) + 8]   // CARG2
  mov rdx, [rbx + offsetof(CCallState, gpr) + 16]  // CARG3
  mov rcx, [rbx + offsetof(CCallState, gpr) + 24]  // CARG4
  mov r8,  [rbx + offsetof(CCallState, gpr) + 32]  // CARG5
  mov r9,  [rbx + offsetof(CCallState, gpr) + 40]  // CARG6

  // 4. 装载浮点参数寄存器(如果有)
  test eax, eax; jz >5
  movaps xmm0, [rbx + offsetof(CCallState, fpr) + 0]
  movaps xmm1, [rbx + offsetof(CCallState, fpr) + 16]
  movaps xmm2, [rbx + offsetof(CCallState, fpr) + 32]
  movaps xmm3, [rbx + offsetof(CCallState, fpr) + 48]
  cmp eax, 4; jbe >5
  movaps xmm4, [rbx + offsetof(CCallState, fpr) + 64]
  movaps xmm5, [rbx + offsetof(CCallState, fpr) + 80]
  movaps xmm6, [rbx + offsetof(CCallState, fpr) + 96]
  movaps xmm7, [rbx + offsetof(CCallState, fpr) + 112]
5:

  // 5. 调用!
  call [rbx + offsetof(CCallState, func)]

  // 6. 回收返回值(从 rax/xmm0 搬回 CCallState)
  mov [rbx + offsetof(CCallState, gpr) + 0], rax
  movaps [rbx + offsetof(CCallState, fpr) + 0], xmm0
  mov [rbx + offsetof(CCallState, gpr) + 8], rdx    // POSIX x64 第二个返回寄存器
  movaps [rbx + offsetof(CCallState, fpr) + 16], xmm1

  mov rbx, [rbp-8]; leave; ret
```

这段汇编是 FFI 的"临门一脚"。它做的事,**精确对应 x64 ABI**:

- **栈调整**:按 `spadj` 预留栈空间。POSIX x64 要求调用方在栈上给被调函数预留至少 32 字节(`CCALL_SPS_FREE=1` 个槽 + ABI 要求的 shadow space),`spadj` 就是算好的总调整量。
- **复制栈参数**:寄存器装不下的参数,从 `stack[]` 复制到真正的栈指针位置。
- **装载寄存器**:`gpr[0..5]` → rdi/rsi/rdx/rcx/r8/r9,`fpr[0..7]` → xmm0-7。注意它**先判断 `nfpr` 是否为 0**,如果是(没有浮点参数),直接跳过所有 `movaps`——这是优化,避免无谓的 SSE 操作。
- **`call`**:`call [rbx + func]` 跳到那个函数地址执行。C 函数在这里跑起来。
- **回收返回值**:C 函数返回后,整数返回值在 `rax`(POSIX 第二个在 `rdx`),浮点返回值在 `xmm0`(POSIX 第二个在 `xmm1`)。汇编把它们搬回 `CCallState`,这样 C 代码(`ccall_get_results`)就能接着处理。

为什么要用汇编写这个,而不是 C?因为 **C 没法做到"从一个数组按位置装载到指定的寄存器再调用"**。C 函数的参数,编译时就绑定了寄存器(C 编译器自己分配),你不能在运行时动态地决定"把这个值放 rdi 还是 rsi"。唯一能精确控制寄存器的,是汇编。所以 LuaJIT 用一小段手写汇编,完成了"把 CCallState 这个托盘里的值,原样灌进物理寄存器并调用"这个 C 做不到的动作。

这段汇编在 `vm_x64.dasc:2823`(POSIX 与 Windows 共用,Windows 少装载 r8/r9 之后的 GPR 和 xmm4-7),ARM64 在 `vm_arm64.dasc`,x86 在 `vm_x86.dasc`。每个架构一份,各自精确实现该架构的调用约定。`lj_ccall.c:582` 那条 `#error "Missing calling convention definitions for this architecture"` 就是在编译期保证:只要 LuaJIT 移植到一个新架构,这套调用约定的实现必须补齐,否则编译不过。

### 5.5 取回返回值:ccall_get_results

C 函数执行完,返回值在 `CCallState` 里(汇编搬回去的)。`ccall_get_results`(`lj_ccall.c:1184`)把它转回 Lua 值:

```c
static int ccall_get_results(lua_State *L, CTState *cts, CType *ct,
                             CCallState *cc, int *ret)
{
  CType *ctr = ctype_rawchild(cts, ct);     /* 返回类型 */
  uint8_t *sp = (uint8_t *)&cc->gpr[0];      /* 默认从 GPR 取 */
  if (ctype_isvoid(ctr->info)) {
    *ret = 0;  return 0;                     /* void:无返回值 */
  }
  *ret = 1;
  if (ctype_isstruct(ctr->info)) {
    /* struct 返回:用预分配的 cdata,按 ABI 规则重组 */
    if (!cc->retref) {
      void *dp = cdataptr(cdataV(L->top-1));
      CCALL_HANDLE_STRUCTRET2
    }
    return 1;
  }
  if (ctype_iscomplex(ctr->info)) { ... }
  ...
#if CCALL_NUM_FPR
  if (ctype_isfp(ctr->info) || ctype_isvector(ctr->info))
    sp = (uint8_t *)&cc->fpr[0];            /* 浮点/向量:从 FPR 取 */
#endif
  ...
  return lj_cconv_tv_ct(cts, ctr, 0, L->top-1, sp);  /* C→Lua 转换 */
}
```

关键在**返回值从哪取**:整数返回值在 `gpr[0]`(对应 `rax`),浮点返回值在 `fpr[0]`(对应 `xmm0`)。这段代码根据返回类型,选对来源,然后调 `lj_cconv_tv_ct`(下一节)把 C 值转成 Lua 值压栈。

struct 返回特别麻烦:小 struct 可能拆在寄存器里返回(`CCALL_HANDLE_STRUCTRET2` 重组),大 struct 是 pass-by-reference(调用前预分配了 cdata,函数直接往那个地址写)。`cc->retref` 标志区分这两种情况。这些规则,同样是各 ABI 规定的,LuaJIT 必须照做。

---

## §6 lj_cconv:C 值与 Lua 值的转换

调用前后,还有一道工序:**Lua 值 ↔ C 值的转换**。Lua 的 number 是双精度浮点(64 位),LuaJIT 内部还区分整数;但 C 的参数可能是 `int`(4 字节)、`char`(1 字节)、`float`(4 字节浮点)、指针……转换必须精确。这是 `lj_cconv.c`(768 行)的职责。

### 6.1 三个方向的转换

`lj_cconv.c` 提供三个核心函数,对应三个方向:

- **`lj_cconv_ct_tv`(`lj_cconv.c:544`)**:**TValue → C 类型**。Lua 传参给 C 函数时用这个。比如 `C.printf("%d", 42)`,要把 Lua 的 `42`(可能是 `intV` 也可能是 `numV`)转成 `int`。
- **`lj_cconv_tv_ct`(`lj_cconv.c:378`)**:**C 类型 → TValue**。C 函数返回、读取 cdata 时用这个。比如把 C 的 `int` 返回值转回 Lua number。
- **`lj_cconv_ct_ct`(`lj_cconv.c:119`)**:**C 类型 → C 类型**。纯 C 侧转换,比如 `int` 赋给 `long`,指针赋给 `intptr_t`。

看 `lj_cconv_ct_tv` 怎么把 Lua 值转成 C 值(`lj_cconv.c:544`):

```c
void lj_cconv_ct_tv(CTState *cts, CType *d, uint8_t *dp, TValue *o, CTInfo flags)
{
  CTypeID sid = CTID_P_VOID;
  CType *s;
  void *tmpptr;
  uint8_t tmpbool, *sp = (uint8_t *)&tmpptr;
  if (LJ_LIKELY(tvisint(o))) {              /* Lua 整数 */
    sp = (uint8_t *)&o->i;
    sid = CTID_INT32;
    flags |= CCF_FROMTV;
  } else if (LJ_LIKELY(tvisnum(o))) {       /* Lua 浮点 */
    sp = (uint8_t *)&o->n;
    sid = CTID_DOUBLE;
    flags |= CCF_FROMTV;
  } else if (tviscdata(o)) {                /* 已是 cdata */
    sp = cdataptr(cdataV(o));
    sid = cdataV(o)->ctypeid;
    ...
  } else if (tvisstr(o)) {                  /* 字符串 */
    ...
    sp = (uint8_t *)strdata(str);           /* → const char * */
    sid = CTID_A_CCHAR;
    ...
  } else if (tvistab(o)) {                  /* table → array/struct 初始化 */
    ...
  } else if (tvisbool(o)) {                 /* bool */
    ...
  } else if (tvisnil(o)) {                  /* nil → NULL 指针 */
    tmpptr = (void *)0;
  } else if (tvislightud(o)) {              /* lightuserdata → 指针 */
    tmpptr = lightudV(cts->g, o);
  } else if (tvisfunc(o)) {                 /* Lua 函数 → 回调指针 */
    void *p = lj_ccallback_new(cts, d, funcV(o));
    ...
  } else {
    cconv_err_convtv(cts, d, o, flags);     /* 不能转,报错 */
  }
  s = ctype_get(cts, sid);
  ...
  lj_cconv_ct_ct(cts, d, s, dp, sp, flags);  /* 归结到 C→C 转换 */
}
```

精妙之处在最后:不管 Lua 值原本是什么(整数、浮点、字符串、nil……),先**确定它对应的 C 类型**(整数→`int32`,浮点→`double`,字符串→`const char *`,nil→`void *`),拿到一个"源 C 类型和源数据指针",然后**统一交给 `lj_cconv_ct_ct` 做真正的 C→C 转换**。这样,所有 Lua→C 的转换,都化简成"先识别成某个 C 类型,再做 C→C 转换"两步。这种"统一到一个核心函数"的设计,避免了每种 Lua 类型 × 每个 C 类型都写一遍转换逻辑的组合爆炸。

### 6.2 lj_cconv_ct_ct:C 到 C 的核心转换

真正的转换逻辑在 `lj_cconv_ct_ct`(`lj_cconv.c:119`)。它用一个**巨大的 switch**,按"目标类型 × 源类型"分派:

```c
void lj_cconv_ct_ct(CTState *cts, CType *d, CType *s,
                    uint8_t *dp, uint8_t *sp, CTInfo flags)
{
  CTSize dsize = d->size, ssize = s->size;
  CTInfo dinfo = d->info, sinfo = s->info;
  ...
  switch (cconv_idx2(dinfo, sinfo)) {
  case CCX(I, I):     /* 整数 → 整数 */
  conv_I_I:
    if (dsize > ssize) {  /* 零扩展或符号扩展 */
      uint8_t fill = (!(sinfo & CTF_UNSIGNED) && (sp[ssize-1]&0x80)) ? 0xff : 0;
      memcpy(dp, sp, ssize);
      memset(dp + ssize, fill, dsize-ssize);
    } else {
      memcpy(dp, sp, dsize);
    }
    break;
  case CCX(I, F): {   /* 浮点 → 整数 */
    double n;
    if (ssize == sizeof(double)) n = *(double *)sp;
    else if (ssize == sizeof(float)) n = (double)*(float *)sp;
    ...
    if (dsize < 8) {
      int64_t i = lj_num2i64(n);
      if (dsize == 4) *(int32_t *)dp = i;
      ...
    } else if (dsize == 8) {
      if ((dinfo & CTF_UNSIGNED)) *(uint64_t *)dp = lj_num2u64(n);
      else *(int64_t *)dp = lj_num2i64(n);
    }
    break;
  }
  case CCX(F, I): {   /* 整数 → 浮点 */
    double n;
    ...
    *(double *)dp = n;  /* 或 float */
    break;
  }
  ...
  case CCX(P, P):     /* 指针 → 指针 */
    if (!lj_cconv_compatptr(cts, d, s, flags)) goto err_conv;
    cdata_setptr(dp, dsize, cdata_getptr(sp, ssize));
    break;
  ...
  }
}
```

`cconv_idx2(dinfo, sinfo)`(`lj_cconv.h:41`)是一个压缩索引:它把 C 类型按大类(Bool/Integer/Float/Complex/Vector/Pointer/Array/Struct)编号,然后用 `(dst<<3)+src` 算出一个 6 位的索引。这样 `CCX(I, F)`(整数目标、浮点源)就是一个常量,switch 高效分派。8 大类两两组合最多 64 种,覆盖所有常见转换,代码紧凑。

抓几个关键转换看 LuaJIT 怎么保证正确:

**整数→整数的符号扩展。** `case CCX(I, I)` 里,如果目标比源宽(比如 `int`→`long long`),要决定高位填 0 还是 1。规则:源是有符号整数且最高位是 1(负数),填 `0xff`(符号扩展);否则填 `0x00`(零扩展)。这段代码(`lj_cconv.c:173`)精确实现 C 的整型提升规则——有符号数符号扩展,无符号数零扩展。**少做这一步,负数就会变成大正数,结果全错**。

**浮点→整数。** `case CCX(I, F)` 的注释特别醒目(`lj_cconv.c:199`):

```c
/* The conversion must exactly match the semantics of JIT-compiled code! */
```

为什么这么强调?因为浮点转整数有一个**平台相关的坑**:C 标准说浮点超出整数范围时行为未定义,但不同硬件/编译器实际行为不一(LuaJIT 必须保证解释器路径和 JIT 路径给出**完全一样**的结果,否则 trace 退出后会得到不同值——这违反了本书主线那个不变式"机器码结果要么和解释器一样,要么退回解释器")。所以这里用 `lj_num2i64` / `lj_num2u64`(`lj_cconv.c:201-207`)——这是 LuaJIT 自己实现的、跨平台一致的转换函数,**保证解释器和 JIT 用同一套语义**。这是 FFI 转换的 soundness 保障之一。

**指针兼容性检查。** `case CCX(P, P)` 调 `lj_cconv_compatptr`(`lj_cconv.c:77`)检查两个指针类型是否兼容。C 对指针转换有严格规则(比如 `int *` 不能随便赋给 `char *` 除非显式 cast),LuaJIT 在 `lj_cconv_compatptr` 里实现了这套规则。`flags` 里的 `CCF_CAST`(显式转型)和 `CCF_IGNQUAL`(忽略限定符)控制检查的严格程度。这保证 FFI 的指针转换和 C 编译器一样安全——不会让你把 `int *` 偷偷当 `float *` 用导致数据损坏。

### 6.3 一个完整的转换例子:42 怎么变成 int

把 `C.printf("%d", 42)` 里那个 `42` 的旅程走完。假设 LuaJIT 内部 42 是个 `intV`(LuaJIT 的 DUALNUM 优化下,小整数存成整数而非浮点):

1. `ccall_set_args` 遍历到这个参数,Lua 栈上是 `42`(`tvisint`)。
2. 参数类型从函数签名拿——但 `printf` 是变长参数,签名里没这个参数的类型。调 `lj_ccall_ctid_vararg`(`lj_ccall.c:944`):`tvisnumber(o)` 为真,返回 `CTID_DOUBLE`。所以这个 42 被当成 `double` 传(符合 C 的默认提升)。
3. `CCALL_HANDLE_REGARG`:这是 POSIX x64,`double` 是浮点(`isfp=1`),`nfpr + 1 <= 8`,塞进 `fpr[1]`(第二个浮点参数槽,因为第一个是 format 字符串……实际上 format 是字符串转成 `const char *`,是整数参数走 GPR,所以这个 double 是第一个浮点,进 `fpr[0]`)。等等,这里要注意 x64 POSIX 的 ABI:变长参数函数,浮点参数**也**进 xmm 寄存器,但被调函数(`printf`)知道从 xmm 读——这是 ABI 规定的。
4. 调 `lj_cconv_ct_tv(cts, ctype_get(CTID_DOUBLE), dp, o, ...)`:Lua 整数 42,源类型 `CTID_INT32`,调 `lj_cconv_ct_ct` 的 `CCX(F, I)` 分支,整数 42 转成 double 42.0,写入 `dp`(指向 `fpr[0]`)。
5. 汇编 `lj_vm_ffi_call` 把 `fpr[0]` 装进 `xmm0`,`call printf`。

`printf` 内部按 ABI 规则从 xmm0 读出这个 double,按 `%d` 格式当成 int 截断,得到 42,打印。整个链路严丝合缝,每一步都遵守 C ABI。

---

## §7 为什么 sound:类型正确、转换不丢、遵守 ABI

讲完了机制,这一节专门论证:这套设计为什么不会出错。FFI 涉及"跨越语言边界",最容易出问题的地方就是类型表示错、转换丢精度、调用约定不符——任何一个错了,轻则结果错,重则内存损坏、崩溃。LuaJIT 用三层保障堵住这三类错误。

### 7.1 类型表示:CTInfo 完整且无歧义

第一层:C 类型必须被**完整且无歧义**地表示。CTInfo 那 32 位的编码,覆盖了 C 类型的所有维度:

- **种类**(高 4 位):14 种 CT_ 枚举,涵盖 C 所有类型种类。
- **限定符**(中段):`const`/`volatile`/`unsigned`/`long`/`bool`/`fp`/`vector`/`complex`/`union`/`vararg`/`ref`/`vla`,C 类型上能挂的限定符全有。
- **对齐**(bits 16-19):最多 2^15 字节对齐,够装下 SIMD 的大对齐要求。
- **子类型**(低 16 位):指向子类型的 ID,构成类型树。
- **调用约定**(bits 16-17,仅 FUNC):cdecl/thiscall/fastcall/stdcall。

任何 C 类型,都能被这 32 位 + 一个 size + sib 链 + cid,精确表达。解析器(`lj_cparse`)只认标准 C 语法,不会造出语义错误的类型。类型表通过 interning 保证"相同的 C 声明得到相同的类型 ID",不会出现两个语义相同但表示不同的类型——这避免了比较时的歧义。

一个具体的 soundness 体现:`ctype_isinteger` 这种判断宏(`lj_ctype.h:217`),用 `(info & (CTMASK_NUM|CTF_BOOL|CTF_FP)) == CTINFO(CT_NUM, 0)` 精确区分"整数"和"布尔"和"浮点"——三者都是 `CT_NUM`,但通过标志位区分开。这种精确区分保证后续转换不会把布尔当整数、把浮点当整数——那会导致灾难。

### 7.2 转换不丢精度:严格遵守 C 语义

第二层:C↔Lua 转换必须**不丢精度、不改变语义**。`lj_cconv.c` 的每个 case 都精确对应 C 标准的转换规则:

- **整数扩展**:零扩展 vs 符号扩展,按 `CTF_UNSIGNED` 正确选择(`lj_cconv.c:173-189`)。
- **浮点↔整数**:用 LuaJIT 自己的 `lj_num2i64` 等函数,保证跨平台一致(`lj_cconv.c:199` 那条注释"must exactly match JIT"是铁律)。
- **指针兼容**:`lj_cconv_compatptr` 实现 C 的指针兼容性规则,不兼容的转换(除非显式 cast)报错而非默默执行(`lj_cconv.c:77`)。
- **范围检查**:赋值超出目标范围时,`cconv_err_initov` 报"initializer overflow"(`lj_cconv.c:50`)。

特别值得说的是**浮点↔整数转换的一致性**。C 标准把"浮点超范围转整数"留给实现定义(implementation-defined),不同硬件行为不同。如果 LuaJIT 的解释器路径用一种语义、JIT 路径(下一章)用另一种,那么一个 trace 在 guard 失败退回解释器后,可能算出和机器码不同的结果——这直接破坏本书主线那个核心不变式("机器码结果要么和解释器一样,要么退回解释器")。所以 `lj_cconv.c` 这里用 LuaJIT 统一的 `lj_num2i64` / `lj_num2u64`,而 JIT 侧(`lj_crecord.c`)的录制也必须生成调用同样语义的 IR——两边对齐,保证一致。这是 FFI 转换的 soundness 根基。

### 7.3 遵守 ABI:调用约定逐字实现

第三层:实际调用必须**逐字遵守目标平台的 ABI**。这是 `lj_ccall.c` 1263 行的绝大部分所在。

LuaJIT 把每个支持架构的 ABI 规则,都**完整复刻**进了代码:

- **寄存器分配规则**:x64 POSIX 的 rdi/rsi/rdx/rcx/r8/r9 + xmm0-7,x64 Windows 的 rcx/rdx/r8/r9 + xmm0-3(共享位置),ARM 的 r0-r3 + d0-d7,ARM64 的 x0-x7 + v0-v7……每种架构的 `CCALL_HANDLE_REGARG` 精确实现。
- **栈对齐**:POSIX x64 调用方维持 16 字节栈对齐(`cc->spadj` 的计算,`lj_ccall.c:1177`)。Windows x64 的 shadow space。
- **struct 传递与返回**:各架构的 struct 分类规则(`ccall_classify_struct`)。x64 POSIX 把 <=16 字节 struct 按 INT/SSE/MEM 分类拆进寄存器(`lj_ccall.c:635-672`);ARM 的 HFA(Homogeneous Float Aggregate,`lj_ccall.c:738`);ARM64 的 HFA 变体(`lj_ccall.c:810`);MIPS64 的浮点字段分类(`lj_ccall.c:867`)。struct 返回有 by-value(小 struct 拆寄存器)和 by-reference(大 struct,调用方预分配,函数写进去)两种,`CCALL_HANDLE_STRUCTRET` 各架构实现不同。
- **变长参数**:默认参数提升(`lj_ccall_ctid_vararg`),Windows x64 的 vararg 镜像规则(`lj_ccall.c:1143`,把变长参数同时写进 GPR 和 FPR 两套)。
- **调用约定变体**:x86 的 cdecl/thiscall/fastcall/stdcall(`ctype_cconv`,`lj_ccall.c:997`)。

任何一个规则没实现或实现错,调用就会拿到错误参数或返回错误结果。LuaJIT 通过 `#if LJ_TARGET_*` 的条件编译,**保证每个架构只编译进它需要的 ABI 实现**,而且 `lj_ccall.c:581` 那条 `#error` 保证不支持的新架构编译时就报错——不会默默生成错误的调用代码。

汇编侧 `lj_vm_ffi_call` 同样精确:它装载寄存器的顺序和数量,严格匹配 `CCallState` 里 `ccall_set_args` 摆放的方式,也匹配 ABI 规定的物理寄存器。`vm_x64.dasc:2823` 那段汇编里,装载 rdi/rsi/rdx/rcx/r8/r9 和 xmm0-7 的顺序,就是 POSIX x64 ABI 的参数寄存器顺序。

这三层——类型表示完整、转换守语义、调用遵 ABI——合起来,保证 FFI 调用的结果,和用 C 编译器编译等价 C 代码的结果**完全一致**。这就是 FFI 的 soundness。它不是靠运行时检查兜底(那是 guard 的活),而是靠**构造时就保证正确**:类型系统建模对、转换规则对、调用约定对。从根上就不会出错。

---

## §8 为什么 FFI 比 Lua C API 快

讲到这里,可以正面回答 §1 那个问题了:FFI 为什么比传统的 Lua C API 包装快?

快在不走栈式 API。看传统方式每一次调用要做什么:每取一个参数,要调 `luaL_checkstring`/`luaL_checkinteger` 这种 API——这些 API 内部要去访问 Lua 栈、做类型检查、可能还要做字符串转换,每一次都是函数调用 + 多重判断。返回时同理。N 个参数就是 N 次这种开销。

而 FFI 的路径是:`ccall_set_args` **一次性**遍历所有参数,直接按调用约定摆进 `CCallState` 的寄存器/栈槽(一次 `lj_cconv_ct_tv` 转换 + 一次内存写),然后一段极简的汇编把寄存器装好就 `call`。没有"每参数一次 API 调用"的开销,参数直接进寄存器副本,汇编直接搬进物理寄存器。整个调用路径上,除了必要的类型转换,**没有多余的中转**。

更关键的是——这是下一章 P6-21 的重头——**FFI 调用还能被 JIT 编译**。当 `C.printf` 出现在一个热循环里,LuaJIT 会把这次 FFI 调用**录制成 IR**,后端直接生成"装载寄存器 + call"的机器码,连 `lj_ccall_func` / `ccall_set_args` 这些 C 函数都不走了——机器码里直接就是几条 `mov` + 一条 `call`。这几乎和你在 C 里直接调 `printf` 一样快。这是 Lua C API 永远做不到的:C API 包装函数本身是 C 代码,可以被调用,但调用它的开销(栈操作、类型检查)是固定的,JIT 没法消除。

所以 FFI 的快,有两层:**第一层**(本章),省掉了栈式 API 的中转,直接按调用约定摆参数;**第二层**(下一章),整个调用被 JIT 内联进 trace,连 CCallState 的准备开销都被优化掉。这两层叠加,让 LuaJIT 调 C 函数的开销,逼近原生 C 调 C——这是 FFI 性能的真正来源。

---

## §9 ★对照:JNI、ctypes/cffi 与官方 Lua

把 LuaJIT FFI 和几个同类机制放在一起,更能看清它的取舍。

### 9.1 对照官方 Lua:只有 Lua C API

官方 Lua **没有** FFI。它只有 §1 讲的那条路:**Lua C API + 手写 C 包装**。要调任何 C 函数,必须写一个 `luaB_printf` 这样的包装,编译成动态库,`require` 进来。

| 维度 | LuaJIT FFI | 官方 Lua |
|---|---|---|
| 调 C 的方式 | 声明签名,直接调用 | 手写 C 包装,注册 |
| 是否需要 C 代码 | 否(纯 Lua 脚本) | 是(必须写 + 编译 C) |
| 参数传递 | 直接进调用约定(寄存器/栈) | 经 Lua 栈 + API 函数 |
| 类型表示 | 内建 C 类型系统 | 无(C 侧自行处理) |
| 能否被 JIT | 能(下一章) | 不能(C 包装是黑盒) |
| 性能 | 接近原生 C 调用 | 每次调用有栈开销 |

这个对照点很尖锐:**FFI 让"调 C"从一件需要 C 工具链的工程,变成了一件纯 Lua 脚本就能做的事**,而且更快。这是 LuaJIT 相对官方 Lua 的一个巨大易用性 + 性能优势。代价是 LuaJIT 实现了一整套 C 类型系统 + 多架构 ABI 支持(本章这几千行源码),复杂度远超官方 Lua。

### 9.2 对照 JVM/JNI、CPython ctypes/cffi

跨语言调 C 是个普遍需求,各大运行时都有自己的方案。LuaJIT FFI 的设计,在这个生态里位置很特别。

**JVM 的 JNI(Java Native Interface)。** JNI 走的也是"写包装"的路子:用 `javac -h` 生成头文件,手写 C 的 `Java_包名_方法名` 函数,在里面用 JNI API(`GetEnv`、`FindClass`、`GetFieldID`、`CallStaticVoidMethod`……)操作 Java 对象。比 Lua C API 还重——因为 Java 的对象模型比 Lua 复杂,JNI 要处理类、字段、方法签名、异常、GC 引用(global/local reference)。

JNI 的痛点:1)必须写 C 代码 + 编译;2)JNI API 极其啰嗦,一次简单调用要写一堆;3)每次跨越 JNI 边界开销大(从 managed 切到 native,要切换栈帧、pin 对象、可能触发 GC safepoint);4)JIT 优化很难穿透 JNI 边界(JIT 看不到 native 函数内部)。所以 JVM 生态里,JNI 被当作"最后手段",能用纯 Java 实现就不用 JNI。

对比之下,LuaJIT FFI:**不写 C 代码**(纯 Lua 声明)、**调用开销小**(直接 ABI)、**能被 JIT 内联**(下一章)。每一项都是 JNI 做不到的。代价是 LuaJIT FFI 只能调"标准 ABI 的 C 函数"(不能像 JNI 那样深度操作宿主语言的对象模型),但这个限制对大多数场景(调系统库、调数学库、调自己写的 C 内核)完全够用。

**CPython 的 ctypes 与 cffi。** CPython 生态有两个类似 FFI 的东西。`ctypes`(标准库)让你在 Python 里声明 C 函数签名并调用,思路和 LuaJIT FFI 类似。`cffi`(第三方)更进一步,直接接受 C 源码声明。

它们和 LuaJIT FFI 的关键差别在**性能**。CPython 是解释器(没有 JIT),所以 ctypes/cffi 的调用,每次都要走完整的"参数装箱/拆箱 + 调用约定准备"的 C 代码路径——和 LuaJIT FFI 的第一层(本章)类似的开销。但 **LuaJIT FFI 有第二层**:调用被 JIT 编译进 trace,机器码直接装载寄存器并 call,几乎零开销。ctypes/cffi 因为没有 JIT,永远停在第一层。

所以一个微妙但重要的对比:

| 维度 | LuaJIT FFI | CPython ctypes/cffi | JVM JNI |
|---|---|---|---|
| 是否写 C 代码 | 否 | 否(ctypes/cffi) | 是 |
| 调用机制 | 直接 ABI | 直接 ABI | JNI 桥 + 包装 |
| 单次开销 | 小 | 中 | 大 |
| 能否被 JIT 内联 | **能** | 否(无 JIT) | 难(native 黑盒) |
| 热循环里调 C | 接近原生 | 每次完整开销 | 每次完整开销 + 边界开销 |

LuaJIT FFI 的独特价值,在于它把"直接 ABI 调用"和"trace JIT 内联"结合了起来——既得了 ctypes/cffi 的易用,又得了 JIT 的性能。这是它成为"招牌特性"的根源。

### 9.3 回扣主线:FFI 是 JIT 与外部世界的桥梁

回到本书主线——**把动态执行安全变成机器码**。前面五章讲的,都是"Lua 自己的代码怎么变成机器码"。但现实程序要和外部世界(C 库、系统调用、自写内核)交互。如果这种交互不能被 JIT 优化,那么无论 Lua 内部跑得多快,一遇到调 C 就慢下来——热点就断了。

FFI 解决的正是这个问题。它做了两件事:**第一**(本章),让 Lua 能**正确地**调 C——用完整的 C 类型系统建模类型、用精确的 ABI 实现调用、用守语义的转换保证不丢正确性。这是"安全"的一半(类型对、转换对、调用对)。**第二**(下一章),让这种 C 调用**能被 JIT 编译**——把 `lj_ccall_func` 的逻辑录制成 IR,后端直接生成"装载寄存器 + call"的机器码。这是"快"的一半。

合起来,FFI 让"把动态执行变成机器码"这条主线,**延伸到了 C 边界之外**:不仅 Lua 内部的算术、循环能跑机器码,连"Lua 调 C 函数"这个跨界动作,也能跑成接近原生的机器码。trace 不会在调 C 时断裂,热点能跨越语言边界延续——这是 LuaJIT 性能优势能覆盖"调库"场景的根本原因。

而这一切的根基,是本章讲的那套**类型系统的正确性**和**调用约定的精确性**。没有 `lj_ctype` 把 C 类型建模对,没有 `lj_ccall` 把 ABI 实现对,JIT 编译 C 调用就是空中楼阁——你没法把一个调用优化成机器码,如果你连这个调用的参数类型、传递方式都说不清楚。FFI 的性能,建立在类型系统的严谨之上。这是"安全"和"快"在本章的具体统一:因为类型对、调用对(安全),所以能放心地把它编译成机器码(快)。

下一章,我们就看这"第二层"是怎么实现的:FFI 调用怎么被录制进 trace、被 JIT 编译成几乎零开销的机器码,以及这中间 GC 怎么和 JIT 协作(因为 cdata 是 GC 对象,JIT 代码里引用 cdata 要处理 GC 移动问题)。

---

*下一章 [P6-21 FFI 录制与 GC 协作](P6-21-FFI录制与GC协作.md):本章讲了 FFI 怎么在解释器侧正确调用 C。下一章讲这次调用怎么被 JIT 编译器录制成 IR、生成机器码,以及 cdata 作为 GC 对象怎么在 JIT 代码里被安全引用。*
