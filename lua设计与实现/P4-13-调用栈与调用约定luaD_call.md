# P4-13 调用栈与调用约定 luaD_call

> **本书主线**:统一与精简换小而快。**二分法**:编译侧(源码→字节码)↔ 执行侧(VM 执行 + 管值生命周期)。**本章位置**:执行侧·函数、闭包与调用(P4)。**对照栏**:CPython 的调用(_PyEval 建 frame / PyCFunction 走 C)。**源码**:lua-5.5.0,`ldo.c`、`lstate.h`、`lvm.c`、`lfunc.h`。**基调**:纯直球,不用比喻。

---

## 一、这章解决什么问题

P3-11 把 `luaV_execute` 的主循环拆完了:取指、译码、执行、分发。但那里刻意绕开了一个问题——**一条 `OP_CALL` 指令执行下去,被调函数到底是怎么"跑起来"的?参数是怎么进到被调函数手里的?返回值又是怎么回到调用处的?**

这个问题不像它看上去那么简单。一门脚本语言的"函数调用",在底层至少要同时回答四件事:

1. **控制流转移**:当前执行到一半的函数要被挂起,被调函数要从它的第一条指令开始跑。挂起的状态(执行到哪条指令、局部变量在哪)得有个地方存。
2. **参数传递**:调用者手上有函数值和一组实参,它们在值栈上是一段连续的槽位。被调函数怎么"看到"这些参数?
3. **栈帧隔离**:被调函数自己也会用值栈(放它自己的局部变量、临时值)。它用的那段栈,不能和调用者用的那段冲突。
4. **返回值回流**:被调函数跑完,可能返回零个、一个、多个值,甚至返回"调用者收到几个值由调用者决定"的多返回值(`LUA_MULTRET`)。这些值要被搬到调用者期望的位置,调用者的栈顶要被调整回正确状态。

这四件事,在 Lua 里由一个叫 **`CallInfo`** 的结构 + 一组 `luaD_*` 函数(`ldo.c`)共同完成。一个 `CallInfo` 就是栈上的一帧,对应一次函数调用。这一章把这些数据结构和函数逐行拆开,讲清楚一次 `f(a, b)` 从调用到返回,在值栈和 `CallInfo` 链上到底发生了什么。

讲透它,顺带就把 Lua 执行侧最关键的一个设计优势落到了源码上:**Lua 调 Lua 不吃 C 栈深度**。这是 Lua 能扛深递归、能塞进栈容量有限的嵌入式宿主的根基之一,也是 5.5 相对老资料在调用机制上的若干演进的集中体现。

---

## 二、源码怎么实现

### 2.1 一帧调用长什么样:`CallInfo` 结构

调用信息全部装在 `struct CallInfo` 里,定义在 `lstate.h:187`:

```c
struct CallInfo {
  StkIdRel func;  /* function index in the stack */
  StkIdRel top;  /* top for this function */
  struct CallInfo *previous, *next;  /* dynamic call link */
  union {
    struct {  /* only for Lua functions */
      const Instruction *savedpc;
      volatile l_signalT trap;  /* function is tracing lines/counts */
      int nextraargs;  /* # of extra arguments in vararg functions */
    } l;
    struct {  /* only for C functions */
      lua_KFunction k;  /* continuation in case of yields */
      ptrdiff_t old_errfunc;
      lua_KContext ctx;  /* context info. in case of yields */
    } c;
  } u;
  union {
    int funcidx;  /* called-function index */
    int nyield;  /* number of values yielded */
    int nres;  /* number of values returned */
  } u2;
  l_uint32 callstatus;
};
```

逐字段拆。

**`func`**(`StkIdRel`,相对表示,P1-02 讲过):被调函数在值栈上的位置。注意它指的不是"函数本身",而是"函数值所在那个栈槽"。`func + 1` 就是这个栈帧的 `base`——被调函数的寄存器从这里开始编号(R0 = `func + 1`)。

**`top`**(`StkIdRel`):这个函数用到的栈顶上限。它和 `L->top`(全局栈顶,当前实际用到哪里)不是一回事。`ci->top` 是"这个函数的寄存器空间能用到哪",在分配 CI 时就被设成 `func + 1 + maxstacksize`。它是一个**预留的**上限,不是当前实际栈顶。`L->top` 才是当前实际栈顶。这个区分很关键,后面讲。

**`previous` / `next`**:把所有 `CallInfo` 串成双向链表。`previous` 指调用者,`next` 指被调者。`L->ci`(`lua_State` 的字段,`lstate.h:291`)永远指向**当前**正在执行的那一帧。

**`u` 联合**:这是 Lua 函数和 C 函数的本质差异落点。同一个 `CallInfo`,要么在跑 Lua 函数、要么在跑 C 函数,两者需要的状态不同:

- **Lua 函数用 `u.l`**:
  - `savedpc`(`const Instruction *`):**指令指针**。Lua 函数跑到哪条字节码,就记在这里。函数被挂起(去调别的函数)时,当前 `pc` 存回 `savedpc`;回来时从 `savedpc` 续上。这就是 P3-11 里 `pc = ci->u.l.savedpc;`(`lvm.c:1212`)的来源。
  - `trap`(`volatile l_signalT`):这一帧是否打开了调试钩子/遇到栈重分配的标记。**注意它在这里,在 `u.l` 里,是 Lua 函数专属。** 这是 5.5(以及 5.4)相对老资料的一个硬差异——讲 5.3 的资料会把 `trap` 写在 `lua_State` 层;5.4 起它下沉到每个 `CallInfo`,让每个调用帧独立跟踪自己的 trap 状态(P3-11 第 8 条已标注)。`volatile` 是因为它可能被信号异步改。
  - `nextraargs`:`vararg` 函数的额外参数数。和可变参数机制相关(P4-15 详讲),这里只需知道它属于 Lua 帧的状态。

- **C 函数用 `u.c`**:
  - `k`(`lua_KFunction`):**续体(continuation)**。C 函数如果中途 yield(协程场景),恢复时要靠这个续体函数续上。纯同步的 C 函数它就是 NULL。
  - `old_errfunc`:`pcall` 嵌套时保存的外层错误函数。
  - `ctx`(`lua_KContext`):续体的上下文,跨 yield 传给续体函数。

注意 **C 函数没有 `savedpc`**——因为 C 函数不跑字节码,它的"执行位置"就是 C 程序计数器,由 C 栈自己管。这是 Lua 函数和 C 函数在调用机制上的根本分水岭:Lua 函数的执行状态在 `CallInfo` 里(数据结构化),C 函数的执行状态在 C 栈里(宿主原生)。

**`u2` 联合**:三种互斥用途复用同一个 `int`:
- `funcidx`:做 protected call 时记录被调函数在栈上的索引(C 函数用)。
- `nyield`:协程 yield 时存"yield 了多少个值"。
- `nres`:关闭 to-be-closed 变量时存"返回了多少个值"。

**`callstatus`**(`l_uint32`):一个 32 位状态字,塞了一大堆标志位。看 `lstate.h:222` 起的宏定义:

```c
/* bits 0-7 are the expected number of results from this function + 1 */
#define CIST_NRESULTS	0xffu
/* bits 8-11 count call metamethods (and their extra arguments) */
#define CIST_CCMT	8
/* Bits 12-14 are used for CIST_RECST (recover status) */
#define CIST_RECST	12
/* call is running a C function */
#define CIST_C		(1u << (CIST_RECST + 3))
/* call is on a fresh "luaV_execute" frame */
#define CIST_FRESH	(cast(l_uint32, CIST_C) << 1)
...
```

最关键的几位:

- **bit 0–7(`CIST_NRESULTS`)**:这帧调用**期望多少个返回值 + 1**(加 1 是为了把"0 个返回值"和"未设置"区分开)。这是调用约定的核心——调用者通过这个字段告诉被调函数"我要几个返回值"。多返回值 `LUA_MULTRET`(`lua.h:35`,`-1`)编码进来就是 0。读取用 `get_nresults(cs)`(`lstate.h:254`):`(cs & CIST_NRESULTS) - 1`。
- **`CIST_C`**(bit 15):这帧是不是在跑 C 函数。`isLua(ci)` 宏(`lstate.h:270`)就是 `!((ci)->callstatus & CIST_C)`。
- **`CIST_FRESH`**:这帧是不是"专门为它新开了一个 `luaV_execute` 的 C 帧"。这个标志是 `goto startfunc`/`returning` 复用 C 帧机制的关键,2.5 节详讲。
- **`CIST_TAIL`**:这帧是不是尾调用过来的。
- **`CIST_TBC`**:这帧有没有需要关闭的 to-be-closed 变量。
- **`CIST_YPCALL`**:这帧是不是 yieldable 的 protected call。
- **`CIST_CCMT`**(bit 8–11):这一帧累计调了多少层 `__call` 元方法(非函数值被当函数调时,Lua 会去找它的 `__call` 元方法,见 2.4 的 `tryfuncTM`)。

一个 `CallInfo` 就是这么一个紧凑结构,把一次调用需要的全部状态(Lua 还是 C、跑到哪、要几个返回值、各种标志)全装下。整条调用链就是一串 `CallInfo` 串成的双向链表。

### 2.2 调用总入口:`luaD_call`

对外暴露的调用入口在 `ldo.c:775`:

```c
void luaD_call (lua_State *L, StkId func, int nResults) {
  ccall(L, func, nResults, 1);
}
```

它只是 `ccall` 的薄包装。真正干活的是 `ccall`(`ldo.c:757`):

```c
l_sinline void ccall (lua_State *L, StkId func, int nResults, l_uint32 inc) {
  CallInfo *ci;
  L->nCcalls += inc;
  if (l_unlikely(getCcalls(L) >= LUAI_MAXCCALLS)) {
    checkstackp(L, 0, func);  /* free any use of EXTRA_STACK */
    luaE_checkcstack(L);
  }
  if ((ci = luaD_precall(L, func, nResults)) != NULL) {  /* Lua function? */
    ci->callstatus |= CIST_FRESH;  /* mark that it is a "fresh" execute */
    luaV_execute(L, ci);  /* call it */
  }
  L->nCcalls -= inc;
}
```

三个参数:`func` 是被调函数在值栈上的位置;`nResults` 是调用者期望的返回值数(`LUA_MULTRET` 表示"全要");`inc` 是 C 栈深度计数器要加多少(普通调用加 1,不可 yield 的调用加 `nyci = 0x10000 | 1`,见下)。

`ccall` 自己只做三件事:

1. **维护 C 栈深度计数**:`L->nCcalls += inc`。`nCcalls` 是个 32 位计数器,低 16 位是 C 栈递归深度,高 16 位是非 yieldable 调用数(`lstate.h:95` 的注释讲了这个编码)。`getCcalls(L)`(`lstate.h:107`)取低 16 位。这里检查它有没有超过 `LUAI_MAXCCALLS`(`ldo.h:63`,默认 **200**)。超过就调 `luaE_checkcstack`(`lstate.c:131`)抛 "C stack overflow"。这是 Lua 防止 C 栈爆掉的**第一道**防线——后面 2.5 会讲为什么还需要第二道(`goto startfunc`)。
2. **调 `luaD_precall` 准备新帧**:它返回 `CallInfo *`。如果返回非 NULL,说明被调是 **Lua 函数**,需要 VM 去跑它的字节码,于是 `luaV_execute(L, ci)` 进主循环;如果返回 NULL,说明被调是 **C 函数**——`precall` 内部已经把它**调完**了(C 函数不需要 VM 跑字节码),栈上结果也搬好了,直接返回。
3. **打 `CIST_FRESH` 标记**:只有 Lua 函数这一支会走到这里。这个标记告诉 VM "这个 `luaV_execute` 的 C 帧是专门为这次调用新开的"。`goto returning` 时会查它(2.5 节)。

注意 `luaD_callnoyield`(`ldo.c:783`)和 `luaD_call` 唯一的区别就是 `inc` 传 `nyci` 而不是 1——它额外把高 16 位(非 yieldable 计数)也加 1,标记"这次调用不能被 yield"。元方法调用、`__close` 调用走这条,因为它们不能让协程从中间 yield 出去。

### 2.3 调用前准备:`luaD_precall`

这是调用机制的核心。`ldo.c:715`:

```c
CallInfo *luaD_precall (lua_State *L, StkId func, int nResults) {
  unsigned status = cast_uint(nresults + 1);
  lua_assert(status <= MAXRESULTS + 1);
 retry:
  switch (ttypetag(s2v(func))) {
    case LUA_VCCL:  /* C closure */
      precallC(L, func, status, clCvalue(s2v(func))->f);
      return NULL;
    case LUA_VLCF:  /* light C function */
      precallC(L, func, status, fvalue(s2v(func)));
      return NULL;
    case LUA_VLCL: {  /* Lua function */
      CallInfo *ci;
      Proto *p = clLvalue(s2v(func))->p;
      int narg = cast_int(L->top.p - func) - 1;  /* number of real arguments */
      int nfixparams = p->numparams;
      int fsize = p->maxstacksize;  /* frame size */
      checkstackp(L, fsize, func);
      L->ci = ci = prepCallInfo(L, func, status, func + 1 + fsize);
      ci->u.l.savedpc = p->code;  /* starting point */
      for (; narg < nfixparams; narg++)
        setnilvalue(s2v(L->top.p++));  /* complete missing arguments */
      lua_assert(ci->top.p <= L->stack_last.p);
      return ci;
    }
    default: {  /* not a function */
      checkstackp(L, 1, func);  /* space for metamethod */
      status = tryfuncTM(L, func, status);  /* try '__call' metamethod */
      goto retry;  /* try again with metamethod */
    }
  }
}
```

`status` 把期望返回值数 + 1 编码进 `callstatus` 的低 8 位(`CIST_NRESULTS`)。`+1` 是为了能表示"期望 0 个返回值"——`get_nresults` 取的时候再减 1。

`switch` 按被调值的类型 tag 分流。Lua 的函数值有三种 tag(见 `lobject.h:639` 附近):

- **`LUA_VCCL`**:C 闭包(带 upvalue 的 C 函数,`CClosure` with `CIST_C`)。它的 C 函数指针在 `clCvalue(s2v(func))->f`。
- **`LUA_VLCF`**:轻量 C 函数(纯指针,不带 upvalue,`lua_CFunction` 直接装在 `TValue` 里)。函数指针在 `fvalue(s2v(func))`。
- **`LUA_VLCL`**:Lua 闭包(`LClosure`,带 `Proto *p`)。这是要进 VM 跑字节码的那种。

前两种都走 `precallC`(下一节),返回 NULL(表示 C 函数已经在 `precallC` 里调完了)。第三种 Lua 闭包,是这一节的重点。

#### Lua 函数的 precall

```c
Proto *p = clLvalue(s2v(func))->p;
int narg = cast_int(L->top.p - func) - 1;  /* number of real arguments */
int nfixparams = p->numparams;
int fsize = p->maxstacksize;  /* frame size */
checkstackp(L, fsize, func);
L->ci = ci = prepCallInfo(L, func, status, func + 1 + fsize);
ci->u.l.savedpc = p->code;  /* starting point */
for (; narg < nfixparams; narg++)
  setnilvalue(s2v(L->top.p++));  /* complete missing arguments */
```

逐行。

**`narg`** = `L->top.p - func - 1`。`L->top.p` 是当前全局栈顶,`func` 是函数值的位置。中间隔了 `func` 自己(1 个槽)+ 实参们。所以 `L->top.p - func - 1` 就是实参数。**这就是 Lua 的参数传递机制的全部**——调用者把函数和参数**连续压在值栈上**,被调函数靠 `L->top` 减 `func` 算出有几个参数。没有任何专门的"参数区"或"参数对象"。

> 谁来压这些参数?是 `OP_CALL` 指令自己。看 `lvm.c:1720`:
> ```c
> vmcase(OP_CALL) {
>   StkId ra = RA(i);
>   ...
>   int b = GETARG_B(i);
>   int nresults = GETARG_C(i) - 1;
>   if (b != 0)  /* fixed number of arguments? */
>     L->top.p = ra + b;  /* top signals number of arguments */
>   /* else previous instruction set top */
>   ...
> ```
> `OP_CALL A B C` 里,A 是函数所在寄存器(`R[A] = func`),B 编码参数个数 + 1(B=0 是 `LUA_MULTRET` 的特殊情况,表示"参数个数由上一条 `OP_VARARGPREP`/`OP_CALL` 设的 `L->top` 决定")。`ra + b` 就是函数 + 实参这段的栈顶,`L->top.p = ra + b` 把全局栈顶设到这,`precall` 才能算出 `narg`。**参数不是"传"进去的,是已经在栈上,`OP_CALL` 只是标记一下"到这里为止是参数"。**

**`nfixparams`** = `p->numparams`(`Proto` 的字段,`lobject.h:604`)。被调函数的**固定形参数**——编译期就知道的、有名字的参数个数(比如 `function f(a, b)` 的 `numparams = 2`)。

**`fsize`** = `p->maxstacksize`(`lobject.h:606`)。这个函数需要的**寄存器总数**(局部变量 + 临时槽)。编译器在 `lcode.c` 里算好的,P2-09 讲过。

**`checkstackp(L, fsize, func)`**(`ldo.h:50`):**栈空间检查 + 扩容**。如果 `L->stack_last.p - L->top.p <= fsize`(栈顶到栈底余量不够 `fsize` 个槽),就调 `luaD_growstack`(`ldo.c:353`)扩容。`checkstackp` 里有个细节:它用 `savestack(L, p)` 把 `func` 这个栈指针**先存成偏移**,扩容完再用 `restorestack(L, t__)` 转回指针——因为扩容可能 `realloc` 搬家,搬家期间绝对指针会失效。这是 `StkIdRel` 相对表示在调用路径上的直接体现(P1-02 详讲了 `relstack`/`correctstack` 机制,这里 `checkstackp` 是同款思路的微缩版,只保护一个指针)。

**`prepCallInfo(L, func, status, func + 1 + fsize)`**(`ldo.c:628`):**分配并初始化新 CI**:

```c
l_sinline CallInfo *prepCallInfo (lua_State *L, StkId func, unsigned status,
                                                StkId top) {
  CallInfo *ci = L->ci = next_ci(L);  /* new frame */
  ci->func.p = func;
  lua_assert((status & ~(CIST_NRESULTS | CIST_C | MAX_CCMT)) == 0);
  ci->callstatus = status;
  ci->top.p = top;
  return ci;
}
```

`next_ci(L)`(`ldo.c:619`)是 `L->ci->next ? L->ci->next : luaE_extendCI(L)`——优先复用链表上已有的空闲 CI(`luaE_shrinkCI` 会留一些),没有才调 `luaE_extendCI`(`lstate.c:71`)**新 malloc** 一个 `CallInfo`。新 CI 串到链表尾,`L->nci++`。

然后填三个字段:
- `ci->func.p = func`:函数位置。
- `ci->callstatus = status`:期望返回值数 + 标志(此时只有 `CIST_NRESULTS`,Lua 函数还没打 `CIST_C`)。
- `ci->top.p = func + 1 + fsize`:**栈帧上限** = 函数值 + 1(跳过函数槽)+ 寄存器总数。这个 `top` 是"这个函数的寄存器空间能用到哪"的预留上限。

回到 `precall`:

**`ci->u.l.savedpc = p->code`**:指令指针指向函数字节码的第一条(`p->code` 是 `Instruction *`,指向指令数组头)。这就是 Lua 函数执行的起点——下一条要跑的指令是函数的第一条。

**`for (; narg < nfixparams; narg++) setnilvalue(s2v(L->top.p++))`**:**补 nil**。如果实参数 `narg` 小于形参数 `nfixparams`(调用时少传了参数),把缺的形参位置全填 nil。比如 `function f(a,b,c)` 用 `f(1)` 调,`narg=1`、`nfixparams=3`,这个循环会把 R1(b)、R2(c) 填成 nil。**这就是 Lua "少传参数自动补 nil" 的实现。** 注意它推进了 `L->top.p`——补的 nil 占用的是被调函数自己的寄存器槽。

到这里,新 CI 建好、`func`/`top`/`savedpc`/`callstatus` 全填好、参数已经在栈上(就是 `func+1` 开始的那段,它们既是实参又是被调函数的 R0/R1/... 寄存器)。`precall` 返回这个 `ci`。

回到 `ccall`:`ci = luaD_precall(...)` 非 NULL,于是 `ci->callstatus |= CIST_FRESH; luaV_execute(L, ci);`——进 VM 主循环,从 `ci->u.l.savedpc`(函数第一条指令)开始跑。

> **关键点:参数 = 寄存器。** 在 Lua 里,被调函数的固定形参 R0..R(numparams-1) **就是**调用者压在栈上的实参本身,没有拷贝、没有专门的参数传递步骤。`OP_CALL` 之前调用者把实参压在 `func+1, func+2, ...`,`precall` 设新帧后这些槽**就地变成**被调函数的寄存器。这是寄存器式 VM 调用约定的精髓——参数传递零成本。对照 CPython 栈式 VM,参数要从调用者的栈搬到被调者的 `fastlocals` 数组,有一次显式拷贝。

### 2.4 C 函数的调用:`precallC`

C 函数走 `precallC`(`ldo.c:642`):

```c
l_sinline int precallC (lua_State *L, StkId func, unsigned status,
                                            lua_CFunction f) {
  int n;  /* number of returns */
  CallInfo *ci;
  checkstackp(L, LUA_MINSTACK, func);  /* ensure minimum stack size */
  L->ci = ci = prepCallInfo(L, func, status | CIST_C,
                               L->top.p + LUA_MINSTACK);
  lua_assert(ci->top.p <= L->stack_last.p);
  if (l_unlikely(L->hookmask & LUA_MASKCALL)) {
    int narg = cast_int(L->top.p - func) - 1;
    luaD_hook(L, LUA_HOOKCALL, -1, 1, narg);
  }
  lua_unlock(L);
  n = (*f)(L);  /* do the actual call */
  lua_lock(L);
  api_checknelems(L, n);
  luaD_poscall(L, ci, n);
  return n;
}
```

和 Lua 函数的 precall 几个本质差异:

1. **`checkstackp(L, LUA_MINSTACK, func)`**:`LUA_MINSTACK`(`lua.h:79`,**20**)。C 函数不像 Lua 函数有编译期算好的 `maxstacksize`,所以只保证最少 20 个槽的栈空间(`lua.h` 文档要求宿主 C 函数假设自己至少有 `LUA_MINSTACK` 个可用栈槽)。C 函数自己用 `lua_push*` API 时,API 内部会再检查。

2. **`status | CIST_C`**:打上 `CIST_C` 标记——这一帧是 C 函数。这是 `isLua(ci)` 判断的依据。

3. **`ci->top.p = L->top.p + LUA_MINSTACK`**:C 函数的栈帧上限设成"当前栈顶 + 20"。注意是 `L->top.p`(当前实际栈顶)不是 `func`——因为 C 函数的参数已经压在栈上了,C 函数会用 `lua_to*` 通过栈索引去取(从 `func` 算起的相对索引或负索引),它需要的是"往后还能 push 多少"的空间。

4. **`n = (*f)(L)`**:真正调那个 C 函数。`f` 是从 `lua_CFunction` 指针取出来的。C 函数用 `lua_State *L` 的栈式 API(`lapi.c`)接收参数、压返回值,返回一个 `int` 表示"我压了几个返回值"。注意这里的 `lua_unlock`/`lua_lock`——这是多线程锁的空宏(单线程 Lua 里是 no-op),解锁是为了让 C 函数里可能 yield 的协程能正确切换。

5. **`luaD_poscall(L, ci, n)`**:C 函数返回后,立刻 poscall 把返回值搬到调用者期望的位置。**所以 C 函数在 `precallC` 里就从头到尾跑完了**,`luaD_precall` 对 C 函数返回 NULL,`ccall` 不再调 `luaV_execute`。

C 函数调用约定的参数传递和 Lua 函数**完全一样**——调用者把函数 + 参数压栈,C 函数靠栈索引(`luaL_checkint(L, 1)` 等,索引 1 是第一个参数)去取。统一到了极致。

#### 非函数值:`__call` 元方法

`precall` 的 `default` 分支处理"被调值根本不是函数"的情况(比如 `t()` 其中 `t` 是个 table):

```c
default: {  /* not a function */
  checkstackp(L, 1, func);  /* space for metamethod */
  status = tryfuncTM(L, func, status);  /* try '__call' metamethod */
  goto retry;  /* try again with metamethod */
}
```

`tryfuncTM`(`ldo.c:523`)查这个值的元表里有没有 `__call` 元方法。有就把 `__call` 函数**插到 `func` 下面**(把原来的 `func` 和参数整体上移一格,空出的位置放 `__call` 函数,原来的 `func` 变成 `__call` 的第一个参数),然后 `goto retry` 用新的"函数"(其实是 `__call` 元方法)重新走 `switch`。`status` 累加 `CIST_CCMT` 计数(bit 8–11),防止 `__call` 链无限套娃(上限 `MAX_CCMT` = 15 层,满了抛 "'__call' chain too long")。

这个机制让任何值都能"被当函数调"——只要它的元表里有 `__call`。又是 Lua 用 Table + 元表统一行为的体现(P6-19 详讲)。

### 2.5 返回:`luaD_poscall` 与多返回值

函数跑完要返回。Lua 函数的返回由 `OP_RETURN`/`OP_RETURN0`/`OP_RETURN1` 三条指令处理(`lvm.c:1763`/`1785`/`1802`),它们最后都汇到 `luaD_poscall`。C 函数在 `precallC` 里也调它。

`luaD_poscall`(`ldo.c:605`):

```c
void luaD_poscall (lua_State *L, CallInfo *ci, int nres) {
  l_uint32 fwanted = ci->callstatus & (CIST_TBC | CIST_NRESULTS);
  if (l_unlikely(L->hookmask) && !(fwanted & CIST_TBC))
    rethook(L, ci, nres);
  /* move results to proper place */
  moveresults(L, ci->func.p, nres, fwanted);
  /* function cannot be in any of these cases when returning */
  lua_assert(!(ci->callstatus &
        (CIST_HOOKED | CIST_YPCALL | CIST_FIN | CIST_CLSRET)));
  L->ci = ci->previous;  /* back to caller (after closing variables) */
}
```

三个动作:

1. **`fwanted = ci->callstatus & (CIST_TBC | CIST_NRESULTS)`**:从这一帧的 `callstatus` 里抠出"期望返回值数 + 是否有 to-be-closed 变量"。`CIST_NRESULTS` 是低 8 位(期望数 + 1),`CIST_TBC` 是有无 to-be-closed 标志。这两位合起来是 `moveresults` 的全部输入。

2. **调返回钩子**(开了 `LUA_MASKRET` 钩子时):`rethook`(`ldo.c:494`)。注意它只在**没有** to-be-closed 变量时调——如果有 to-be-closed,钩子要等到那些变量关闭之后才调(在 `moveresults` 的 `default` 分支里)。

3. **`moveresults(L, ci->func.p, nres, fwanted)`**:**搬返回值**。这是核心,下面细讲。

4. **`L->ci = ci->previous`**:**弹出当前帧**,回到调用者的 CI。这一步是"控制流返回调用者"的实质——`L->ci` 指回调用者,VM 接下来跑调用者的字节码。

#### 多返回值的搬运:`moveresults`

被调函数实际返回了 `nres` 个值(在栈上 `L->top.p - nres` 到 `L->top.p` 这段),调用者期望 `wanted = get_nresults(fwanted)` 个值(在 `ci->func.p` 开始的位置放)。两边的数可能不等:

- 被调返回多了 → 多余的丢掉。
- 被调返回少了 → 缺的补 nil。
- 期望是 `LUA_MULTRET` → 全要,一个不丢。

`moveresults`(`ldo.c:561`)按 `fwanted` 分流,把最常见的几种情况特化掉:

```c
l_sinline void moveresults (lua_State *L, StkId res, int nres,
                                          l_uint32 fwanted) {
  switch (fwanted) {
    case 0 + 1:  /* no values needed */
      L->top.p = res;
      return;
    case 1 + 1:  /* one value needed */
      if (nres == 0)   /* no results? */
        setnilvalue(s2v(res));  /* adjust with nil */
      else
        setobjs2s(L, res, L->top.p - nres);  /* move it to proper place */
      L->top.p = res + 1;
      return;
    case LUA_MULTRET + 1:
      genmoveresults(L, res, nres, nres);  /* we want all results */
      break;
    default: {  /* two/more results and/or to-be-closed variables */
      ...
    }
  }
}
```

`fwanted` 是"期望数 + 1",所以:

- **`case 0+1`**:期望 0 个返回值(比如 `f()` 当语句用)。直接 `L->top.p = res`——把栈顶收回到函数位置,返回值全丢弃。
- **`case 1+1`**:期望 1 个返回值(比如 `local x = f()`)。如果被调真返回了至少 1 个,把第一个搬到 `res`;如果返回 0 个,补 nil。`L->top.p = res + 1`。
- **`case LUA_MULTRET + 1`**:多返回值(比如 `return f()` 这种把被调的返回值原样向上返回,或者 `t = {f()}` 把多返回值全展开进表)。走 `genmoveresults` 全搬。
- **`default`**:期望 2 个及以上,或者有 to-be-closed 变量要关。走通用路径 `genmoveresults`,中间可能夹一段 `luaF_close`(关 to-be-closed,P4-14/P4-15 讲)。

`genmoveresults`(`ldo.c:540`)是通用搬运:

```c
l_sinline void genmoveresults (lua_State *L, StkId res, int nres,
                                             int wanted) {
  StkId firstresult = L->top.p - nres;  /* index of first result */
  int i;
  if (nres > wanted)
    nres = wanted;  /* don't need them */
  for (i = 0; i < nres; i++)  /* move all results to correct place */
    setobjs2s(L, res + i, firstresult + i);
  for (; i < wanted; i++)  /* complete wanted number of results */
    setnilvalue(s2v(res + i));
  L->top.p = res + wanted;  /* top points after the last result */
}
```

`res` 是调用者期望返回值落下的位置(就是被调函数 `func` 那个槽——返回值要覆盖掉原来函数值的位置,因为函数值已经不需要了)。`firstresult` 是被调实际返回值的起点。搬 `min(nres, wanted)` 个,缺的补 nil,最后 `L->top.p = res + wanted` 调用者栈顶。

注意 `res` 和 `firstresult` 可能**重叠**——当被调函数返回的值就紧挨着 `func` 时(常见情况:`OP_RETURN1` 把返回值放在某个寄存器,寄存器在 `func` 上面不远)。`setobjs2s` 是 `memmove` 语义的重叠安全拷贝(`lobject.h` 里定义),所以重叠不会出错。

> **多返回值的源头**。Lua 函数怎么产生"多个返回值"?靠 `OP_RETURN` 的 B 字段。`lvm.c:1763`:
> ```c
> vmcase(OP_RETURN) {
>   StkId ra = RA(i);
>   int n = GETARG_B(i) - 1;  /* number of results */
>   ...
>   if (n < 0)  /* not fixed? */
>     n = cast_int(L->top.p - ra);  /* get what is available */
>   ...
>   L->top.p = ra + n;  /* set call for 'luaD_poscall' */
>   luaD_poscall(L, ci, n);
> ```
> B=0(`LUA_MULTRET`)时,返回值数由 `L->top.p - ra` 算——即"栈上有多少就算多少"。这通常发生在 `return f()` 的场景:上一层的 `OP_CALL` 用 `LUA_MULTRET` 调 `f`,把 `f` 的返回值原样留在栈上,本函数 `OP_RETURN` 也用 `LUA_MULTRET`,把这些值再原样向上传。整条链上多返回值**不需要逐层拷贝**,就靠 `LUA_MULTRET` 编码 + `L->top` 自然衔接。

> 另外两条返回指令是优化:`OP_RETURN0`(`lvm.c:1785`)是"返回 0 个值",`OP_RETURN1`(`lvm.c:1802`)是"返回 1 个值"。它们在没开钩子时**内联了 poscall 逻辑**(直接搬值 + `L->ci = ci->previous`),不走 `luaD_poscall`,省一次函数调用。这是把最常见的返回情况特化加速。

### 2.6 一次完整调用的全程

把上面的零件串起来,走一遍 `local x = f(1, 2)` 在底层发生了什么。假设 `f` 是 Lua 函数 `function f(a, b) return a + b end`,它的 `numparams=2`、`maxstacksize=2`。

**调用前(调用者 `OP_CALL` 执行中)**:
- 调用者已经把 `f` 加载到某寄存器 `R[A]`,把 `1` 加载到 `R[A+1]`、`2` 加载到 `R[A+2]`。
- `OP_CALL A 3 2`(B=3 表示函数 + 2 个参数;C=2 表示期望 1 个返回值,因为 `local x =` 只接一个)。
- `OP_CALL` 执行:`L->top.p = ra + 3`(标出参数边界);`savepc(ci)`(存当前 pc 以防出错);`luaD_precall(L, ra, 1)`。

**`luaD_precall` Lua 分支**:
- `narg = L->top.p - func - 1 = 2`。
- `nfixparams = 2`,`fsize = 2`。
- `checkstackp(L, 2, func)`:确保栈还够 2 个槽。
- `prepCallInfo`:新 CI,`ci->func.p = func`,`ci->callstatus = 2`(期望 1 个返回值 + 1 = 2),`ci->top.p = func + 1 + 2 = func + 3`,`ci->u.l.savedpc = p->code`。
- `narg < nfixparams` 不成立(2 不小于 2),不补 nil。
- 返回 `ci`。

**回到 `ccall`**:
- `ci->callstatus |= CIST_FRESH`。
- `luaV_execute(L, ci)` 进主循环。

**`luaV_execute`**:
- `startfunc`:`trap = L->hookmask`(假设没开钩子,trap=0);`cl = ci_func(ci)`(取被调闭包);`k = cl->p->k`;`pc = ci->u.l.savedpc`(函数第一条指令);`base = ci->func.p + 1`。
- 主循环跑 `f` 的字节码:`ADD` 算 `a + b` 放进某寄存器,然后 `OP_RETURN1` 返回那个寄存器。

**`OP_RETURN1`**:
- 没开钩子,走内联:`nres = get_nresults(ci->callstatus) = 1`;`L->ci = ci->previous`(弹回调用者);把返回值搬到 `base - 1`(就是 `func` 那个槽);`L->top.p = base`(栈顶到 `func + 1`)。
- `goto ret`:`ci->callstatus & CIST_FRESH` 为真(这帧是 `ccall` 专门开的),`return`——退出 `luaV_execute` 这个 C 函数。

**回到 `ccall`**:
- `L->nCcalls -= 1`。
- 返回到 `OP_CALL` 的 `vmbreak`,VM 继续跑调用者下一条指令(`local x =` 的 `OP_MOVE` 把 `func` 槽的返回值搬到 `x` 的寄存器)。

全程结束。值栈上 `f`、`1`、`2` 的位置被复用——`func` 槽现在装返回值,`func+1`、`func+2` 已经是"调用者栈顶之上"(被弹出)。新 CI 在链表上但 `L->ci` 已指回调用者。一次调用在栈上的足迹干净利落。

### 2.7 错误与恢复:`luaD_pcall` / setjmp / longjmp

调用可能被错误中断(`luaG_runerror` 之类)。Lua 的错误机制是 `setjmp`/`longjmp`(`ldo.c:60` 起):

```c
typedef struct lua_longjmp {
  struct lua_longjmp *previous;
  jmp_buf b;
  volatile TStatus status;  /* error code */
} lua_longjmp;
```

`luaD_rawrunprotected`(`ldo.c:160`)是基础原语:

```c
TStatus luaD_rawrunprotected (lua_State *L, Pfunc f, void *ud) {
  l_uint32 oldnCcalls = L->nCcalls;
  lua_longjmp lj;
  lj.status = LUA_OK;
  lj.previous = L->errorJmp;  /* chain new error handler */
  L->errorJmp = &lj;
  LUAI_TRY(L, &lj, f, ud);  /* call 'f' catching errors */
  L->errorJmp = lj.previous;  /* restore old error handler */
  L->nCcalls = oldnCcalls;
  return lj.status;
}
```

它链上一个 `lua_longjmp` 到 `L->errorJmp`(嵌套 pcall 的链),`LUAI_TRY`(POSIX 下是 `_setjmp`/`_longjmp`,ISO C 下是 `setjmp`/`longjmp`)包住 `f`。`f` 里如果出错,`luaD_throw`(`ldo.c:125`)调 `LUAI_THROW`(即 `longjmp`)跳回最近的 `lua_longjmp`,把错误码塞进 `lj.status`。

抛错的 `luaD_throw`:

```c
l_noret luaD_throw (lua_State *L, TStatus errcode) {
  if (L->errorJmp) {  /* thread has an error handler? */
    L->errorJmp->status = errcode;
    LUAI_THROW(L, L->errorJmp);  /* jump to it */
  }
  else {  /* thread has no error handler */
    ...
    abort();
  }
}
```

`longjmp` 会**直接撕开 C 栈**,回到 `luaD_rawrunprotected` 的 `setjmp` 点。这意味着被错误打断的调用链上所有 C 函数帧(`luaD_call`/`ccall`/`luaV_execute`/被调的 C 函数/...)全部瞬间废弃。`CallInfo` 链不会自动清理——这要靠外层的 `luaD_pcall` 收拾。

`luaD_pcall`(`ldo.c:1081`)是带状态恢复的保护调用:

```c
TStatus luaD_pcall (lua_State *L, Pfunc func, void *u, ptrdiff_t old_top,
                                  ptrdiff_t ef) {
  TStatus status;
  CallInfo *old_ci = L->ci;
  lu_byte old_allowhooks = L->allowhook;
  ptrdiff_t old_errfunc = L->errfunc;
  L->errfunc = ef;
  status = luaD_rawrunprotected(L, func, u);
  if (l_unlikely(status != LUA_OK)) {  /* an error occurred? */
    L->ci = old_ci;  /* restore caller's CI */
    L->allowhook = old_allowhooks;
    status = luaD_closeprotected(L, old_top, status);
    luaD_seterrorobj(L, status, restorestack(L, old_top));
    luaD_shrinkstack(L);   /* restore stack size in case of overflow */
  }
  L->errfunc = old_errfunc;
  return status;
}
```

出错时三件恢复:
1. **`L->ci = old_ci`**:`longjmp` 撕开了 C 栈,但 `L->ci` 还指向出错时最内层的 CI。把它**强制拨回** pcall 调用前的 CI,等于把整条 `CallInfo` 链"逻辑上"截断回 pcall 之前(那些更内层的 CI 还挂在链上,但 `L->ci` 不再指向它们,后续会被 `luaE_shrinkCI` 回收)。
2. **`luaD_closeprotected`**:关闭从 `old_top` 开始的 open upvalue 和 to-be-closed 变量(它自己再套一层 `luaD_rawrunprotected`,因为 `__close` 也可能出错)。
3. **`luaD_seterrorobj` + `luaD_shrinkstack`**:把错误对象搬到 `old_top` 位置;如果栈因为错误处理扩到了 `ERRORSTACKSIZE`,缩回去。

`lua_pcall`(C API 的 protected call,P6-21 详讲)就是基于 `luaD_pcall`。`luaL_loadstring` + `lua_pcall` 是宿主跑一段 Lua 脚本的标准姿势,错误不会撕开宿主的 C 栈——这是 Lua "可嵌入"的关键安全网之一。

---

## 三、为什么这样设计是 sound 的

### 3.1 CallInfo 帧复用:Lua 调 Lua 不爆 C 栈

这是整个调用机制最精妙的一招,也是 5.5 相对老资料在性能上最有量的设计。P3-11 提过 `goto startfunc`/`returning`,这里把它和 `CallInfo` 串起来讲透。

先看问题。如果 Lua 调 Lua 每次都递归调 `luaV_execute`(像很多 VM 那样),那么 Lua 调用链深度 = C 栈深度。一个递归 10000 层的 Lua 函数,会在 C 栈上叠 10000 层 `luaV_execute` 的帧。C 栈通常只有几 MB,每帧 `luaV_execute` 的局部变量(`cl`/`k`/`base`/`pc`/`trap` + 编译器寄存器溢出)算几十到几百字节,十几万层就把 C 栈耗干,`stack overflow`。

Lua 的解法:**Lua 调 Lua 不新开 C 帧**。看 `OP_CALL`(`lvm.c:1729`):

```c
if ((newci = luaD_precall(L, ra, nresults)) == NULL)
  updatetrap(ci);  /* C call; nothing else to be done */
else {  /* Lua call: run function in this same C frame */
  ci = newci;
  goto startfunc;
}
```

`newci != NULL`(Lua 调 Lua)时,**不调 `luaV_execute`**,而是 `ci = newci; goto startfunc;`——在**同一个** `luaV_execute` 的 C 帧里,把局部变量 `ci` 换成新 CI,然后跳回 `startfunc` 标签重载 `cl`/`k`/`base`/`pc`:

```c
startfunc:
  trap = L->hookmask;
returning:  /* trap already set */
  cl = ci_func(ci);
  k = cl->p->k;
  pc = ci->u.l.savedpc;
  if (l_unlikely(trap))
    trap = luaG_tracecall(L);
  base = ci->func.p + 1;
```

被调函数的 `cl`/`k`/`pc`/`base` 全部从新 `ci` 重新加载,主循环继续跑被调的字节码。**这一跳没有进 C 栈**——它是个 `goto`,不是函数调用。

返回时对称。`OP_RETURN1`(`lvm.c:1823`)的 `ret:` 标签:

```c
ret:  /* return from a Lua function */
  if (ci->callstatus & CIST_FRESH)
    return;  /* end this frame */
  else {
    ci = ci->previous;
    goto returning;  /* continue running caller in this frame */
  }
```

如果当前 CI 是 `CIST_FRESH`(专门为它开了 C 帧,由 `ccall` 打的标记),`return` 退出 `luaV_execute`。否则(当前是借上层 C 帧跑的内层 Lua 调用),`ci = ci->previous` 切回调用者 CI,`goto returning` 在同一个 C 帧里把 `cl`/`k`/`pc`/`base` 换回调用者的,继续跑调用者。

这样,一条 Lua→Lua→Lua→... 的调用链,只要中间没有夹 C 函数调用,整条链**只占一个 `luaV_execute` 的 C 帧**。Lua 调用链深度和 C 栈深度解耦——Lua 调用链只受 `LUAI_MAXSTACK`(`ldo.c:192`,默认 **1000000**)限制,这是值栈深度的上限,和 C 栈容量无关。

> **C 函数怎么破这个复用**。当调用链里夹了 C 函数,情况不同。C 函数在 `precallC` 里被**真正调**了(`n = (*f)(L)`),这一步进 C 栈。如果这个 C 函数内部又调 Lua(`lua_call`/`lua_pcall`),那时才会再开一个新的 `luaV_execute` C 帧(走 `ccall` → `luaV_execute`),并在那个新 CI 上打 `CIST_FRESH`。所以 Lua 的 C 栈深度 = **C 函数调用的嵌套深度**,不是 Lua 函数调用的嵌套深度。两层限制:
> - `LUAI_MAXSTACK`(1000000):值栈深度,限 Lua 调用链。
> - `LUAI_MAXCCALLS`(200):C 栈递归深度,限 C 函数嵌套(以及 Lua 调用中夹的 C 函数层数)。
>
> `ccall` 里 `getCcalls(L) >= LUAI_MAXCCALLS` 的检查就是第二道防线。

这个设计的 sound 性在于:**`CIST_FRESH` 准确标记了"哪些 CI 对应一个真实的 C 帧"**。`luaV_execute` 进来时打(由 `ccall`),`return` 时查。`goto startfunc` 创建的内层 CI 不打,所以 `goto returning` 时不会误退出。一个布尔位精确区分了"逻辑调用帧"(`CallInfo`)和"物理 C 帧",让两者数量解耦。这是把递归调用从"压 C 栈"变成"遍历数据结构"的经典手法,和 P6-20 协程的"把栈切成数据"一脉相承。

### 3.2 参数即寄存器:零拷贝调用约定

P3-12 讲过寄存器式比栈式指令少。这里看调用约定层面的延伸收益。

栈式 VM(CPython)的调用约定:调用者把实参压到求值栈,调用指令(`CALL_FUNCTION`)在内部**把实参从求值栈搬到被调函数的 `fastlocals` 数组**(一个独立的局部变量数组,挂在被调的 frame 对象上)。这次搬运是 O(参数数) 的显式拷贝。

Lua 的调用约定:调用者把实参压在 `func+1, func+2, ...`,`precall` 设新帧后,**这些槽就地成为被调函数的 R0, R1, ... 寄存器**。没有搬运。被调函数的第一条指令 `GETTABUP`/`ADD`/... 直接用 `RA(i)`(=`base + GETARG_A(i)`,`base = ci->func.p + 1`)访问这些寄存器,而它们正好就是实参所在位置。

为什么 sound?因为 Lua 的"值栈"在物理上是一个**连续的 `StackValue` 数组**,所有调用帧**共享同一段数组**,只是各帧占用不同的区间(`func` 到 `ci->top`)。新帧的 `base` 紧接调用者压的实参,实参自然就是新帧的最低几个寄存器。没有"调用者栈"和"被调者栈"两个独立数组,所以不存在跨数组搬运。

代价是栈帧不能独立增长——一个函数的栈帧(`func+1` 到 `ci->top`)是连续数组里的一段,如果它用超了(虽然 `maxstacksize` 编译期算好一般不会),或者深递归把整段数组用满,就要 `luaD_growstack` 扩容**整个数组**(下一节)。但这个代价摊到"零拷贝调用"的收益上,Lua 判断划算——绝大多数调用参数很少,搬运的节省累积起来很可观。

### 3.3 栈扩容与 `StkIdRel` 相对表示

`luaD_growstack`(`ldo.c:353`)负责值栈扩容:

```c
int luaD_growstack (lua_State *L, int n, int raiseerror) {
  int size = stacksize(L);
  if (l_unlikely(size > MAXSTACK)) {
    ...
    return 0;
  }
  else if (n < MAXSTACK) {
    int newsize = size + (size >> 1);  /* tentative new size (size * 1.5) */
    int needed = cast_int(L->top.p - L->stack.p) + n;
    if (newsize > MAXSTACK)
      newsize = MAXSTACK;
    if (newsize < needed)
      newsize = needed;
    if (l_likely(newsize <= MAXSTACK))
      return luaD_reallocstack(L, newsize, raiseerror);
  }
  luaD_reallocstack(L, ERRORSTACKSIZE, raiseerror);
  if (raiseerror)
    luaG_runerror(L, "stack overflow");
  return 0;
}
```

扩容策略:**1.5 倍增长**(`size + (size >> 1)`),但不低于"当前用量 + 需求",且不超过 `MAXSTACK`。超过 `MAXSTACK` 就报 stack overflow,但扩到 `ERRORSTACKSIZE`(`MAXSTACK + 200`)留出处理错误消息的空间。

核心是 `luaD_reallocstack`(`ldo.c:322`),P1-02 详讲过。这里只重申它和调用机制的关系:扩容时 `realloc` 可能把整个 `StackValue` 数组搬到新地址,所有指向旧栈的指针(`ci->func`、`ci->top`、`L->top`、open upvalue 的 `v.p` 等)全部失效。5.5(及 5.4)用 `StkIdRel`(`lobject.h:165`,union 既能是 `StkId` 指针又能是 `ptrdiff_t` 偏移)解决这个问题:

```c
static void relstack (lua_State *L) {
  ...
  for (ci = L->ci; ci != NULL; ci = ci->previous) {
    ci->top.offset = savestack(L, ci->top.p);
    ci->func.offset = savestack(L, ci->func.p);
  }
}
...
static void correctstack (lua_State *L, StkId oldstack) {
  ...
  for (ci = L->ci; ci != NULL; ci = ci->previous) {
    ci->top.p = restorestack(L, ci->top.offset);
    ci->func.p = restorestack(L, ci->func.offset);
    if (isLua(ci))
      ci->u.l.trap = 1;  /* signal to update 'trap' in 'luaV_execute' */
  }
}
```

扩容前 `relstack` 把**整条 `CallInfo` 链**上每个 CI 的 `func`/`top` 转成相对栈底的字节偏移,`realloc` 搬家,搬完 `correctstack` 用新栈基地址把偏移转回指针。注意 `correctstack` 里有个细节:对 Lua CI,它把 `ci->u.l.trap = 1`——这是因为扩容后 `base = ci->func.p + 1` 这个本地缓存的 `base` 失效了,要让 `vmfetch` 里的 `if (l_unlikely(trap))` 路径重新 `updatebase(ci)` 修正。这是 `trap` 字段除了"调试钩子"之外的第二个用途:**栈搬家的信号**。

> **5.5 vs 老资料差异**。讲 5.3 的资料,`CallInfo` 的 `func`/`top` 字段类型写的是 `StkId`(即 `TValue *` 绝对指针),`relstack`/`correctstack` 的逻辑完全不同(老版本靠"搬家后用旧地址算偏移"的非严格 ISO C 写法,或遍历修正)。5.4 引入、5.5 沿用的 `StkIdRel` union 把这层做成显式的"偏移态/指针态"切换。另外,5.5 的 `correctstack` 在 `LUAI_STRICT_ADDRESS`(默认 1)下走 `relstack`/`offset` 路径;在 `LUAI_STRICT_ADDRESS=0` 时退化成老的"旧地址算偏移"路径(`ldo.c:285` 起)。这是兼容性后备。

### 3.4 C 栈溢出的双保险

前面提到 `LUAI_MAXCCALLS`(200)是防 C 栈溢出的限制。它由 `luaE_checkcstack`(`lstate.c:131`)执行:

```c
void luaE_checkcstack (lua_State *L) {
  if (getCcalls(L) == LUAI_MAXCCALLS)
    luaG_runerror(L, "C stack overflow");
  else if (getCcalls(L) >= (LUAI_MAXCCALLS / 10 * 11))
    luaD_errerr(L);  /* error while handling stack error */
}
```

两段判定:`getCcalls == 200` 报普通 C 栈溢出错误(可被 pcall 接住);`>= 220`(`200/10*11`)说明**处理溢出错误的过程中又溢出了**(比如错误消息处理函数里又深递归),走 `luaD_errerr` 报 "error in error handling"——这是不可恢复的致命错误。两段判定的间隙(200~219)留给错误处理路径本身使用,避免它一动就又触发溢出。

这个设计 sound 在哪?它把 C 栈溢出做成**可被 pcall 捕获的普通 Lua 错误**(在 200 那一档),而不是直接 SIGSEGV。只有当错误处理本身又溢出(220 那一档),才升级成不可恢复的致命错误。这是把"宿主进程不会被 Lua 的深递归搞崩"作为底线的体现——嵌入场景下,宿主绝不能因为一段脚本递归太深而崩溃。

---

## 四、★对照 CPython + 回扣主线

### 4.1 调用机制对照

| 维度 | Lua 5.5 | CPython |
|---|---|---|
| **调用帧载体** | `CallInfo` 结构(`lstate.h:187`),链表串成调用链,共享同一段值栈数组 | `struct _interpreter_frame` 对象(3.11+),也是链表,但每个 frame 有独立的 `stacktop`/`stackbase` 指向一段 `locals` 数组 |
| **参数传递** | 调用者把实参压在值栈 `func+1..`,`precall` 设新帧后实参**就地成为**被调的 R0..R(n-1) 寄存器,**零拷贝** | `CALL` 指令把实参从求值栈**拷贝**到被调 frame 的 `fastlocals` 数组(`_PyEval_EvalFrameDefault` 里 `POP` + 写 `locals`) |
| **Lua/C 函数分流** | 一个 `luaD_precall` 内 `switch` 三种 tag(Lua 闭包 / C 闭包 / 轻 C 函数),C 函数在 `precallC` 里调完返回 NULL,Lua 函数返回 CI 交 VM 跑 | Python 函数走 `_PyEval_EvalFrameDefault`(建 frame 跑字节码);C 函数走 `PyCFunction_Call`(直接调 C 指针,不建字节码 frame)。两条路径分开 |
| **深递归** | Lua→Lua 用 `goto startfunc`/`returning` 复用一个 C 帧,调用链深度不吃 C 栈,上限 `LUAI_MAXSTACK`(1000000,值栈深度) | 每次函数调用都递归进 `_PyEval_EvalFrameDefault` 的 C 栈,Python 递归深度上限 `sys.setrecursionlimit`(默认 1000),受 C 栈容量约束 |
| **错误机制** | `setjmp`/`longjmp`(`luaD_rawrunprotected`),错误撕开 C 栈回到 pcall 点 | C 异常(`PyErr_SetString` 设异常对象,函数返回 -1 逐层传播),不撕 C 栈,靠返回码逐层回退 |
| **返回值搬运** | `moveresults` 按 `fwanted` 分流(0/1/MULTRET/多值),特化加速常见情况 | `UNPACK_SEQUENCE`/`RETURN_VALUE` 把返回值压回求值栈,多返回值靠 tuple 解包 |

几个关键对照点:

**调用帧的"重量"**。Lua 的 `CallInfo` 是个**很小的固定大小结构**(几个指针 + 一个 `l_uint32`),`next_ci` 还优先复用 `luaE_shrinkCI` 留的空闲 CI,大多数调用连 `malloc` 都不用。CPython 的 frame 对象(3.11 前)是个完整的堆分配对象,有 GC 开销;3.11 后改成栈上分配的轻量 frame(`_PyInterpreterFrame`),但仍比 `CallInfo` 重——它要带 `locals`/`globals`/`builtins`/`code` 等一堆指针。Lua 的精简在这里又一次体现。

**参数传递零拷贝 vs 显式搬运**。这是寄存器式 vs 栈式在调用约定上的直接后果。Lua 的"实参即寄存器"省掉了一次 O(n) 的搬运;CPython 的"求值栈到 fastlocals"是必须的——因为 CPython 的求值栈和被调函数的局部变量数组是两个独立的数据结构。这个差异和 P3-12 讲的"寄存器式指令更少"是同一个根:寄存器式把"操作数位置"和"局部变量位置"统一了。

**深递归的处理哲学**。Lua 用 `goto` 把 Lua 调用链做成纯数据结构(`CallInfo` 链),C 栈深度和 Lua 调用深度解耦,所以 Lua 的默认递归上限是 100 万(值栈深度),实际还能调。CPython 每次调用都进 C 栈,受 C 栈容量物理约束,默认上限只能给 1000(调高了会 SIGSEGV)。这是"把递归变成遍历"和"老老实实递归"的对照——Lua 用 `goto startfunc` 这个不那么"正经"的 C 招数,换来了深递归能力。

**错误的实现策略**。Lua 用 `setjmp`/`longjmp`——错误发生时直接撕开 C 栈,快但暴力(C 栈上的析构不会跑,所以 Lua 在 `luaD_pcall` 里要手动 `luaF_close` 关 upvalue/to-be-closed)。CPython 用返回码 + 异常对象——每个可能出错的 C 函数都要检查返回码逐层传播,慢但干净(每个 C 栈帧有机会清理自己的资源)。Lua 选了快,C Python 选了稳。这又是"小而快" vs "大而全"的一个侧写。

### 4.2 回扣主线

把这一章的几个设计放回 "统一与精简换小而快" 的主线:

- **精简①·一个 `CallInfo` 服务 Lua 和 C 两种函数**:`u` 联合让同一个结构既能跑 Lua 函数(用 `savedpc`/`trap`)又能跑 C 函数(用 `k`/`ctx`),靠 `CIST_C` 一位区分。一套调用机制(`luaD_precall`/`luaD_poscall`)服务两种函数,没有为 C 函数另造一套调用栈。
- **精简②·参数即寄存器,零拷贝**:实参压在 `func+1..`,新帧的 `base = func+1`,实参就地成为被调的 R0..。没有专门的参数传递步骤,没有跨数组搬运。这是寄存器式 VM 在调用约定上的红利。
- **精简③·`goto startfunc`/`returning` 把调用栈做成数据结构**:Lua→Lua 调用不压 C 栈,深递归便宜,调用链深度只受值栈容量限制。一个 `CIST_FRESH` 布尔位精确区分逻辑帧和物理 C 帧。
- **统一·`lua_State` 装下一切**:整条 `CallInfo` 链、值栈、错误恢复点(`errorJmp`)、C 栈深度计数(`nCcalls`),全在 `lua_State` 这一个结构里。一个 `lua_State` 就是一个完整的、可独立嵌进任何宿主的 VM 实例。

调用机制是执行侧的控制流命脉——P3-11 讲了"一条指令怎么跑",这一章讲了"一次调用怎么发生、参数怎么流、返回怎么回"。讲透它,P4-14 才能讲清楚闭包和 upvalue(因为 upvalue 就是指向调用栈某段的引用,调用返回时 upvalue 要"合"),P4-15 才能讲尾调用(尾调用复用当前 CI,是 `goto startfunc` 的极端形态)和可变参数(可变参数打破"实参数 = 形参数"的假设,需要 `nextraargs` 和 `PF_VAHID`)。

Lua 的调用约定,每一处都把"用更少的机制换更多的能力"做到了底:`CallInfo` 一个结构吃两种函数、实参就地变寄存器、`goto` 把递归拍平成遍历。这正是它又小又快、又能扛深递归塞进任何宿主的执行侧根基。

---

*下一章 [P4-14 闭包与 upvalue:词法捕获的开合](P4-14-闭包与upvalue-词法捕获的开合.md):upvalue 是内层函数对外层局部变量的引用。它在 open(指向值栈)和 closed(拷出成独立值)两种状态间切换,靠的就是这一章讲的 `CallInfo` 帧与值栈的关系——函数返回时 open upvalue 要被 `luaF_close` 关掉。*
