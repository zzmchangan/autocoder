# P3-11 解释器循环 luaV_execute:取指-译码-执行-分发

> **本书主线**:统一与精简换小而快。**二分法**:编译侧(源码→字节码)↔ 执行侧(VM 执行 + 管值生命周期)。**★对照**:CPython。**源码**:lua-5.5.0。**基调**:纯直球,不用比喻。
>
> **本章落点**:执行侧核心。编译器产出的字节码是一条条 32 位指令,P3-10 已经讲了它怎么编码;这一章回答的是 VM 怎么把它们一条条跑起来。Lua 给出的答案极简:**一个 `for(;;)` 循环 + 一张操作码表,解释全部指令**。没有 switch 分发器、没有指令线程化、没有 JIT——所有的"统一与精简"在执行侧集中体现为这一个循环。这一章把 `luaV_execute` 的骨架、取指、分发、快慢分流、代表性 case、GC 穿插逐一拆开,讲透这颗 VM 的心脏是怎么跳的。

---

## 一、这章解决什么问题

P3-10 讲清楚了 Lua 的字节码长什么样:一条 32 位指令,低 7 位是操作码,其余位是 A/B/C 三个操作数域。编译器把 Lua 源码翻译成一串这样的指令,存进 `Proto *` 的 `code` 数组。现在的问题是:**这串指令谁来跑,怎么跑。**

最朴素的答案是:写一个函数,里面一个无限循环,每次取一条指令,按操作码跳到对应的处理代码,处理完取下一条。这正是 Lua 的做法,也是几乎所有解释型 VM 的做法。问题在于,这个朴素做法在每个细节上都有取舍,而 Lua 在每个取舍上都选了"更精简、更省"的那一边:

- **取指怎么取**:从一个指针读 32 位字,指针自增。要不要把 pc 单独存在一个寄存器变量里?(要。`const Instruction *pc`。)
- **分发怎么分发**:用 `switch` 还是 computed goto?(默认 GCC 上用 computed goto。)
- **操作数怎么拿**:指令里存的是寄存器号,怎么换算成栈上的 `TValue *`?(加一个基址 `base`。)
- **会出错、会分配、会触发 GC 的指令怎么办**:每条指令都包一层 `setjmp` 太重,不包又怕长程跳转后状态不一致。(快慢分流:`Protect` 宏只包慢路径。)
- **GC 怎么和执行交错**:一次性扫完会卡死宿主,完全不扫内存会涨爆。(在分配点用 `checkGC` 步进一小步。)

这五个问题合起来,就是"一个循环怎么解释所有字节码"的工程学。Lua 的解法是:**把能内联的快路径写成宏直接展开,把慢路径集中到少数 `Protect` 点,把 GC 推进挂在分配点上。** 结果是一个不到 800 行(`luaV_execute` 函数本体 `lvm.c:1198–1970`)的主循环,却解释了全部 83 个操作码(`ljumptab.h` 里 83 个 `&&L_OP_*` 标签)。这一章把这套机制逐块拆开。

一个贯穿全章的视角:**这个循环本身没有任何"智能",它的快全部来自"提前筛掉慢情况"。** 算术指令先假设两边是整数,直接算;表访问先假设是普通表、键命中,直接取;调用先假设是 Lua 函数,直接进新帧。只有假设不成立时,才退到慢路径,退到慢路径的开销(可能调元方法、可能 GC、可能切栈)才是真正"贵"的。这种"快路径在循环里、慢路径在循环外"的分层,是 Lua VM 在纯解释执行下仍能接近手写 C 循环的根本原因。

---

## 二、源码怎么实现

### 2.1 主循环骨架

主循环在 `luaV_execute`(`lvm.c:1198`),函数签名:

```c
void luaV_execute (lua_State *L, CallInfo *ci) {
  LClosure *cl;
  TValue *k;
  StkId base;
  const Instruction *pc;
  int trap;
#if LUA_USE_JUMPTABLE
#include "ljumptab.h"
#endif
 startfunc:
  trap = L->hookmask;
  returning:  /* trap already set */
  cl = ci_func(ci);
  k = cl->p->k;
  pc = ci->u.l.savedpc;
  if (l_unlikely(trap))
    trap = luaG_tracecall(L);
  base = ci->func.p + 1;
  /* main loop of interpreter */
  for (;;) {
    Instruction i;  /* instruction being executed */
    vmfetch();
    ...
    vmdispatch (GET_OPCODE(i)) {
      vmcase(OP_MOVE) { ... vmbreak; }
      vmcase(OP_LOADI) { ... vmbreak; }
      ...
    }
  }
}
```

骨架就这几部分:

1. **局部变量缓存**:`cl` 是当前正在执行的 Lua 闭包(`LClosure *`),`k` 指向它的常量表(`cl->p->k`,一个 `TValue` 数组),`base` 是当前函数栈帧的起点(值栈上 `ci->func.p + 1` 的位置,即函数值下面一个槽),`pc` 是指令指针,`trap` 是"当前是否处于被钩子/信号打断的状态"。
2. **`startfunc:` / `returning:` 标签**:不是普通的循环入口。当一次 Lua 调用 Lua 时,**不新开一个 C 栈帧**去递归调用 `luaV_execute`,而是 `ci = newci; goto startfunc;`——在同一个 C 帧里把当前的 `cl/k/base/pc` 换成被调用者的,继续跑。返回时 `goto returning;` 把 `ci` 换回调用者。这是 Lua 控制深递归不爆 C 栈的关键,P4-13 详讲。这里只需记住:**主循环的"入口"和"返回点"是两个标签,不是函数边界。**
3. **`#if LUA_USE_JUMPTABLE #include "ljumptab.h"`**:条件包含跳转表,详见 2.3。
4. **`for (;;)` 循环体**:取指(`vmfetch()`)→ 分发(`vmdispatch`)→ 各 `vmcase` 分支 → `vmbreak`。

注意三个局部变量 `cl/k/base/pc` 都是**缓存在 C 局部变量里**,而不是每次都从 `ci` 上现读。寄存器分配上编译器会把它们放进硬件寄存器,取指译码是每条指令都做的高频动作,必须压到最便宜。`base` 的更新只在可能改栈的地方(`updatebase`/`updatestack`)发生,绝大多数指令不动栈,`base` 一直有效。

`base = ci->func.p + 1` 这一行要特别说明:`ci->func` 是当前调用帧里"函数本身"所在槽(`StkIdRel`,5.5 相对表示,P1-02),函数值的下一个槽就是寄存器 `R0`。所以**寄存器号 + base = 该寄存器在值栈上的地址**。这正是寄存器式 VM 在硬件上落地的方式——寄存器不是独立的寄存器文件,而是值栈上的一段连续槽位,靠一个基址指针寻址。

### 2.2 取指 vmfetch 宏

`vmfetch` 定义在 `lvm.c:1185`:

```c
#define vmfetch()	{ \
  if (l_unlikely(trap)) {  /* stack reallocation or hooks? */ \
    trap = luaG_traceexec(L, pc);  /* handle hooks */ \
    updatebase(ci);  /* correct stack */ \
  } \
  i = *(pc++); \
}
```

核心就一行 `i = *(pc++);`:从 `pc` 读一条 32 位指令到局部变量 `i`,然后 `pc` 自增。这是任何解释器的标准取指。

但前面有一段 `trap` 判断,这才是 5.5 的精髓。`trap` 是"本帧是否打开了调试钩子或遇到了栈重分配标记"的本地缓存,初值在 `startfunc:` 处取自 `L->hookmask`。它为真的情况有两种:

- **调试钩子开启**(行钩子/调用钩子/计数钩子):每条指令都得检查是否该触发钩子。
- **栈被 realloc 搬家了**:5.5 用 `StkIdRel` 相对表示后,栈搬家不再需要改所有指向栈的指针——但 `base` 这个 C 局部变量里缓存的绝对地址会失效,必须用 `updatebase(ci)`(`base = ci->func.p + 1`)重算。

`trap` 为假时,取指就是裸的 `*(pc++)`,没有任何额外开销。这是把调试开销完全隔离到"开了钩子时"的设计——平时跑生产代码,钩子是关的,`trap` 一直是 0,`vmfetch` 退化成一条内存读。`l_unlikely(trap)`(`llimits.h:329`,映射到编译器的分支预测提示)告诉硬件这条分支几乎不取,流水线不会被污染。

`pc` 自增后,`pc` 指向**下一条**指令。指令本身的译码用的是已读出的 `i`,不是 `*pc`。多字长指令(如 `OP_LOADKX`、`OP_NEWTABLE` 带 `EXTRAARG`)会在 case 里再 `pc++` 把扩展字吃掉,见 2.5。

### 2.3 分发:switch vs computed goto

`vmdispatch` 和 `vmcase`/`vmbreak` 是分发机制的三件套。它们有两套定义,看编译器:

**默认(GCC/Clang)——computed goto**,定义在 `ljumptab.h:8-16`:

```c
#undef vmdispatch
#undef vmcase
#undef vmbreak

#define vmdispatch(x)     goto *disptab[x];

#define vmcase(l)     L_##l:

#define vmbreak		vmfetch(); vmdispatch(GET_OPCODE(i));
```

后面跟一个 `static const void *const disptab[NUM_OPCODES] = { &&L_OP_MOVE, &&L_OP_LOADI, ... }`(`ljumptab.h:19`),把 83 个操作码一一映射到 83 个标签地址。于是 `vmdispatch(GET_OPCODE(i))` 展开成 `goto *disptab[opcode]`——**间接跳转,直接跳到目标标签**,不经过 switch 的比较。

**其他编译器(MSVC 等)——switch**,定义在 `lvm.c:1193-1195`:

```c
#define vmdispatch(o)	switch(o)
#define vmcase(l)	case l:
#define vmbreak		break
```

注意两套定义里 `vmbreak` 不一样:computed goto 版是 `vmfetch(); vmdispatch(GET_OPCODE(i));`,即**每个 case 自己负责重新取指并跳到下一条**;switch 版是 `break`,跳出 switch 后由外层 `for(;;)` 下一轮取指。两者语义等价,但 computed goto 把"取下一条并分发"内联进每个 case 尾部,省一次循环跳转。

`LUA_USE_JUMPTABLE` 的开关在 `lvm.c:39-45`:

```c
#if !defined(LUA_USE_JUMPTABLE)
#if defined(__GNUC__)
#define LUA_USE_JUMPTABLE	1
#else
#define LUA_USE_JUMPTABLE	0
#endif
#endif
```

GCC 系默认开,其他默认关。可以用 `-DLUA_USE_JUMPTABLE=0` 强制关掉。

**为什么 computed goto 快**——这是 VM 经典话题,核心是分支预测。`switch` 被编译成一串比较跳转(或一个跳转表+边界检查),CPU 的分支预测器看到的是"同一条间接跳转指令,历史目标在多个 case 之间漂移",预测准确率低,预测失败要清流水线。computed goto 把分发变成"每个 case 尾部各自一条间接跳转",分支预测器看到的是**N 条不同的跳转指令**,每条都有自己的历史缓冲,预测准确率高得多——尤其是 `OP_MOVE` 之后大概率还是 `OP_MOVE` 或别的常见指令这种局部性。Eli Bendersky 和 CPython issue #42804 都专门讨论过这个机制,CPython 自己也用同一招(见第四节对照)。

代价是:**computed goto 是 GCC 扩展**(`&&label` 取标签地址、`goto *ptr` 间接跳转),不是标准 C。所以 Lua 用条件编译给非 GCC 编译器保留了 switch 退路,`ljumptab.h` 只在 `LUA_USE_JUMPTABLE` 时被 `#include`。这体现了 Lua 的一个一贯取舍——**默认走快的路,但不放弃可移植性**。

### 2.4 Protect 宏:快慢分流

这是理解整个 `luaV_execute` 的钥匙。Lua 把每条指令的执行路径分成两层:

- **快路径(fast path)**:纯计算,不抛错、不分配、不改栈布局、不触发钩子。直接内联在 case 里,不带任何保护。典型:`OP_MOVE`、`OP_LOADI`、算术的整数分支。
- **慢路径(slow path)**:可能 `luaG_runerror`(用 `longjmp` 跳到错误恢复点)、可能 `luaC_step`(推进 GC,可能触发栈扩容)、可能 `luaD_call`(进入新调用帧)。这些动作要求**进入前 VM 的全局状态是一致的**,这样一旦跳走,错误处理代码能正确还原现场。

保护慢路径的就是 `Protect` 家族宏,在 `lvm.c:1144-1167`:

```c
#define savepc(ci)	(ci->u.l.savedpc = pc)

/* Whenever code can raise errors, the global 'pc' and the global
   'top' must be correct to report occasional errors. */
#define savestate(L,ci)		(savepc(ci), L->top.p = ci->top.p)

/* Protect code that, in general, can raise errors, reallocate the
   stack, and change the hooks. */
#define Protect(exp)  (savestate(L,ci), (exp), updatetrap(ci))

/* special version that does not change the top */
#define ProtectNT(exp)  (savepc(ci), (exp), updatetrap(ci))

/* Protect code that can only raise errors. (That is, it cannot change
   the stack or hooks.) */
#define halfProtect(exp)  (savestate(L,ci), (exp))
```

三个动作:

1. **`savestate`**:先把当前 `pc` 写回 `ci->u.l.savedpc`,把 `L->top.p` 校正到 `ci->top.p`。这一步是"对外的真相"——`savedpc` 是错误信息和调试器读的"当前执行到第几条指令",`L->top` 是栈有效边界。平时循环里这两个值可能是过期的(为了省一次写),只有可能跳走前才必须刷新。
2. **`exp`**:执行真正的慢操作。
3. **`updatetrap(ci)`**:把 `trap` 从 `ci->u.l.trap` 重新读回局部变量。因为 `exp` 里可能触发钩子或栈扩容(它们会改 `ci->u.l.trap`),本地缓存必须跟着更新。

`Protect` 是最重的(可能改栈、改钩子),`ProtectNT` 不动 `top`(用于 `luaD_call` 这种自己管 top 的调用),`halfProtect` 只保错误(用于 `luaF_newtbcupval` 这种只可能 `longjmp` 不会改栈的)。**三个 Protect 是按"慢操作到底能改什么"分级的**,能轻则轻——这本身就是"精简"在错误处理上的投影。

关键洞察:**绝大多数指令根本不碰 Protect**。看 `OP_MOVE`:

```c
vmcase(OP_MOVE) {
  StkId ra = RA(i);
  setobjs2s(L, ra, RB(i));
  vmbreak;
}
```

两条赋值,完事。没有 `savepc`,没有 `savestate`,没有任何检查。为什么安全?因为 `OP_MOVE` 只是栈内两个槽之间拷贝 `TValue`,不可能抛错、不可能扩栈、不可能调钩子——`base` 和 `pc` 在这之后依然有效。**保护开销被严格隔离到真正需要的指令上**,这就是 Lua VM 快的工程学根基:不为一百万次安全操作付一次错误检查的税。

反过来,凡是 case 里出现 `Protect(...)` 的地方,就是这条指令有慢路径。下一节逐个看。

### 2.5 操作数取出:RA/RB/RC/RKC

操作数域怎么从 32 位指令里拿出来变成 `TValue *`,靠一组宏(`lvm.c:1102-1110`):

```c
#define RA(i)	(base+GETARG_A(i))
#define vRA(i)	s2v(RA(i))
#define RB(i)	(base+GETARG_B(i))
#define vRB(i)	s2v(RB(i))
#define KB(i)	(k+GETARG_B(i))
#define RC(i)	(base+GETARG_C(i))
#define vRC(i)	s2v(RC(i))
#define KC(i)	(k+GETARG_C(i))
#define RKC(i)	((TESTARG_k(i)) ? k + GETARG_C(i) : s2v(base + GETARG_C(i)))
```

理解这一组宏的关键是分清三个地址空间:

- **寄存器空间**(R):值栈上 `[base, base+maxstacksize)` 这一段。`RA/RB/RC` 取出的就是 `StkId`(`StackValue *`,`lobject.h:158`),要拿 `TValue *` 得 `s2v()`(`lobject.h:172`,展开成 `&(o)->val`)。`vRA/vRB/vRC` 是"取寄存器并转 `TValue *`"的一步宏。
- **常量表空间**(K):`cl->p->k` 数组,编译期就确定的常量(数字、字符串字面量等)。`KB/KC` 直接取 `k + 偏移`。
- **混合空间**(RKC):某条指令的 C 操作数既可能是寄存器、也可能是常量——由指令里的 `k` 标志位(`TESTARG_k`)决定。这是 5.4 起 Lua 引入的"操作数复用":一个 C 域加上一位 k 位,既够 8 位寄存器号,也够常量表索引。`RKC` 读 k 位分流。

所以一个算术 case 里 `vRB(i)` 取左操作数(一定在寄存器),`RKC(i)` 取右操作数(可能是寄存器也可能是常量)。这套复用让 Lua 的算术指令种类翻倍(每个算术操作都有寄存器-寄存器版 `OP_ADD` 和寄存器-常量版 `OP_ADDK`),换来的是减少一条 `LOADK` 指令——又是"精简换少"的一招。

多字长指令的处理也在这层。`OP_LOADKX`(`lvm.c:1256`)的常量索引太大放不进 Bx,就额外占一条 `OP_EXTRAARG`:

```c
vmcase(OP_LOADKX) {
  StkId ra = RA(i);
  TValue *rb;
  rb = k + GETARG_Ax(*pc); pc++;
  setobj2s(L, ra, rb);
  vmbreak;
}
```

`*pc` 读下一条指令(即 `EXTRAARG`),取出它的 Ax 域(全 25 位当大立即数),`pc++` 把它消费掉。`OP_NEWTABLE`(`lvm.c:1407`)同理,数组大小超界时也挂一条 `EXTRAARG`。这种"基础指令 + 可选 EXTRAARG"的变长编码,让 32 位定长指令既能紧凑表示常见情况、又能逃逸到扩展字表示罕见的大数。

### 2.6 核心 case 逐个走

下面挑几类有代表性的 case 讲透,覆盖取数、表访问、算术、调用、返回、跳转、循环。全部来自 `lvm.c` 主循环,行号已逐字核对。

#### 2.6.1 取数类:OP_MOVE / LOADI / LOADK / GETUPVAL

最简单的寄存器复制:

```c
vmcase(OP_MOVE) {
  StkId ra = RA(i);
  setobjs2s(L, ra, RB(i));
  vmbreak;
}
```

`setobjs2s` 是栈槽到栈槽拷贝一个 `TValue`(连同 tag),无 Protect。Lua 里 `local b = a` 编译出来就是一条 `OP_MOVE`。

整数立即数加载:

```c
vmcase(OP_LOADI) {
  StkId ra = RA(i);
  lua_Integer b = GETARG_sBx(i);
  setivalue(s2v(ra), b);
  vmbreak;
}
```

`sBx` 是有符号 Bx(18 位),够装常见的小整数。浮点立即数 `OP_LOADF` 同构,只是 `setfltvalue`。大常量(放不进 sBx 的)走 `OP_LOADK`(从 `k` 表取)或 `OP_LOADKX`(带 EXTRAARG)。**这一族全是纯赋值,无 Protect。**

读 upvalue:

```c
vmcase(OP_GETUPVAL) {
  StkId ra = RA(i);
  int b = GETARG_B(i);
  setobj2s(L, ra, cl->upvals[b]->v.p);
  vmbreak;
}
```

`cl->upvals[b]` 是当前闭包的第 b 个 upvalue(P4-14 详讲 upvalue 的开合),`->v.p` 指向它当前捕获的值(可能是栈上一个槽,也可能是 closed 后堆上的一个 `TValue`)。这里拷贝出来即可,无 Protect——upvalue 的读语义是原子的(写才需要 `luaC_barrier` 屏障,P5-16)。

#### 2.6.2 表访问类:GETTABLE / GETI / GETFIELD —— 快路径 + __index 慢路径

表访问是"快慢分流"最典型的展示。看 `OP_GETTABLE`(`lvm.c:1311`):

```c
vmcase(OP_GETTABLE) {
  StkId ra = RA(i);
  TValue *rb = vRB(i);   /* 表 */
  TValue *rc = vRC(i);   /* 键 */
  lu_byte tag;
  if (ttisinteger(rc)) {  /* 整数键,走数组快路径 */
    luaV_fastgeti(rb, ivalue(rc), s2v(ra), tag);
  }
  else
    luaV_fastget(rb, rc, s2v(ra), luaH_get, tag);
  if (tagisempty(tag))
    Protect(luaV_finishget(L, rb, rc, ra, tag));
  vmbreak;
}
```

两段式:

1. **快路径** `luaV_fastgeti` / `luaV_fastget`(`lvm.h:89`/`81`):展开后调 `luaH_fastgeti`/`luaH_get`(P1-04/05 的 table 实现),直接在表里查。查到返回结果的 tag(非空),查不到返回空 tag。**这一段不抛错、不分配**,所以不进 Protect。
2. **慢路径** `luaV_finishget`:只有快路径返回空(没查到)时才走,而且被 `Protect(...)` 包住。它做的事是查 `__index` 元方法链。

`luaV_finishget`(`lvm.c:291`)的逻辑:

```c
lu_byte luaV_finishget (lua_State *L, const TValue *t, TValue *key,
                                      StkId val, lu_byte tag) {
  int loop;
  const TValue *tm;
  for (loop = 0; loop < MAXTAGLOOP; loop++) {
    if (tag == LUA_VNOTABLE) {  /* t 根本不是表 */
      tm = luaT_gettmbyobj(L, t, TM_INDEX);
      if (l_unlikely(notm(tm)))
        luaG_typeerror(L, t, "index");
    }
    else {  /* t 是表,但键没命中 */
      tm = fasttm(L, hvalue(t)->metatable, TM_INDEX);
      if (tm == NULL) {  /* 没有 __index,结果是 nil */
        setnilvalue(s2v(val));
        return LUA_VNIL;
      }
    }
    if (ttisfunction(tm)) {  /* __index 是函数,调它 */
      tag = luaT_callTMres(L, tm, t, key, val);
      return tag;
    }
    t = tm;  /* __index 是另一个表,递归在它上面再查 */
    luaV_fastget(t, key, s2v(val), luaH_get, tag);
    if (!tagisempty(tag))
      return tag;
  }
  luaG_runerror(L, "'__index' chain too long; possible loop");
  return 0;
}
```

三个关键点:

- **`MAXTAGLOOP`(`lvm.c:50`,值为 2000)**:`__index` 链可能递归(`__index` 又指向一个有 `__index` 的表),为了防止用户写出无限递归的元表链把 VM 挂死,用计数器封顶,超过 2000 报错。这是"统一 + 元方法"这个灵活性的代价——必须有兜底防死循环。
- **`fasttm`**:从表的 metatable 里取某个元方法。`fasttm`(`ltm.h:68`)会先查 `Table` 的 `flags` 位图(`lobject.h:778`,1<<p 表示第 p 个元方法"不存在"),命中就直接返回 NULL,不查哈希。这是把"绝大多数表没有元方法"这个统计事实编码进一个位图,把元方法查找的代价摊到几乎为零。
- **`__index` 是函数时直接调**:`luaT_callTMres` 会真去跑一个 Lua/C 函数,这一步可能触发任意副作用(包括再次进入 `luaV_execute`)。这就是为什么整个 `luaV_finishget` 必须 `Protect`——它内部可能改栈、可能 GC、可能递归调用。

`OP_GETI`(`lvm.c:1325`)和 `OP_GETFIELD`(`lvm.c:1338`)结构一样,区别只在键从哪个域取:`GETI` 的整数键直接从指令 C 域取(常见循环下标),`GETFIELD` 的字符串键从常量表 `KC(i)` 取(常见 `t.name`)。两者也都走 `luaV_fastgeti`/`luaV_fastget` 快路径 + `luaV_finishget` 慢路径。

`OP_GETTABUP`(`lvm.c:1300`)是"从 upvalue 取表再取键"——全局变量访问 `g.x` 编译成它(upvalue 指向 `_ENV` 表)。同样 fastget + finishget 两段。

表写入(`OP_SETTABLE`/`SETI`/`SETFIELD`/`SETTABUP`)镜像对称,快路径 `luaV_fastset` + 慢路径 `luaV_finishset`(`lvm.c:334`),慢路径走 `__newindex`。写入成功后还多一步 `luaV_finishfastset`(`lvm.h:105`),展开成 `luaC_barrierback`——**把新写入的对象纳入 GC 屏障**(P5-16:写屏障保证"黑对象不能指向白对象"的三色不变式)。这是 GC 与 VM 在表写入点的交汇,见 2.8。

#### 2.6.3 算术类:OP_ADD / ADDI / ADDK —— 整数快速路径 + MMBIN 回退

算术是另一处"提前筛慢情况"的典范。先看通用算术宏 `op_arith`(`lvm.c:1004`):

```c
#define op_arith(L,iop,fop) {  \
  TValue *v1 = vRB(i);  \
  TValue *v2 = vRC(i);  \
  op_arith_aux(L, v1, v2, iop, fop); }

#define op_arith_aux(L,v1,v2,iop,fop) {  \
  if (ttisinteger(v1) && ttisinteger(v2)) {  \
    StkId ra = RA(i); \
    lua_Integer i1 = ivalue(v1); lua_Integer i2 = ivalue(v2);  \
    pc++; setivalue(s2v(ra), iop(L, i1, i2));  \
  }  \
  else op_arithf_aux(L, v1, v2, fop); }
```

`OP_ADD`(`lvm.c:1506`)就一行 `op_arith(L, l_addi, luai_numadd);`。展开后:

1. 先判两边是不是都是整数。是——直接 `intop(+, i1, i2)`(`lvm.h:73`,用无符号运算再转回有符号,C 的有符号溢出是 UB,Lua 用 `l_castS2U`/`l_castU2S` 走无符号绕开)算出结果,`pc++` 跳过紧跟的 `OP_MMBIN`,写回。**全程无 Protect,纯计算。**
2. 不是都整数——`op_arithf_aux` 试转两边为浮点(`tonumberns`),成功就浮点算。也无 Protect。
3. **两边都不是数(比如字符串、表)**——`op_arithf_aux` 什么都不做(宏体里那个 `if` 不成立,什么都不写),case 结束。注意这里**没有** `pc++`!所以紧接着的 `OP_MMBIN` 会被取出来执行。

这就是"算术 + 元方法回退"的机制。编译器对每个算术表达式都生成两条指令:`OP_ADD` + `OP_MMBIN`(或 `OP_MMBINI`/`OP_MMBINK`,看右操作数是寄存器、立即数还是常量)。`OP_ADD` 成功时 `pc++` 跳过 `OP_MMBIN`;`OP_ADD` 失败(两边不都是数)时不跳,让 `OP_MMBIN` 接管去查 `__add` 元方法。看 `OP_MMBIN`(`lvm.c:1556`):

```c
vmcase(OP_MMBIN) {
  StkId ra = RA(i);
  Instruction pi = *(pc - 2);  /* 原算术指令 */
  TValue *rb = vRB(i);
  TMS tm = (TMS)GETARG_C(i);   /* 元方法编号 */
  StkId result = RA(pi);
  lua_assert(OP_ADD <= GET_OPCODE(pi) && GET_OPCODE(pi) <= OP_SHR);
  Protect(luaT_trybinTM(L, s2v(ra), rb, result, tm));
  vmbreak;
}
```

`pi = *(pc - 2)` 回头取算术指令本身(因为算术指令 + MMBIN 各占一条,`pc-2` 指向算术那条),`RA(pi)` 是结果该写回的寄存器。`tm` 从指令 C 域取(编译期就定好是 `TM_ADD` 还是 `TM_SUB` 等)。`luaT_trybinTM` 找到元方法并调用。**这一步必然进 Protect**——元方法可能抛错(没 `__add` 时 `luaG_typeerror`)、可能递归调用。

这套"算术指令 + MMBIN 指令对"是 5.4 引入、5.5 沿用的设计。它把**快路径(算术本身)和慢路径(元方法回退)解耦到两条独立指令**。快路径成功时一条指令搞定且跳过第二条;慢路径才付第二条的代价。比老版本(5.3 之前算术指令内部自己判元方法)更清晰,也让快路径更短、更利于流水线。

立即数算术 `OP_ADDI`(`lvm.c:1440`)用 `op_arithI`(`lvm.c:944`),右操作数是指令里的 `sC` 立即数,省一次取寄存器。常量算术 `OP_ADDK` 用 `op_arithK`,右操作数从 `KC(i)` 取。两者都是 `OP_ADD` 的特化版,失败时分别回退到 `OP_MMBINI`/`OP_MMBINK`。

除法/取模(`OP_DIV`/`OP_IDIV`/`OP_MOD`)多一步 `savestate(L, ci)`,因为除零可能抛错(浮点除零得到 inf 不抛,但整数除零 `luaG_runerror`)。除法之外还把"浮点结果"和"整数结果"分开:`OP_DIV` 永远是浮点(`op_arithf`),`OP_IDIV` 是整数向下取整(`op_arith` + `luaV_idiv`)。这套细分让每种算术都走自己最紧的快路径。

#### 2.6.4 调用类:OP_CALL —— 转交 luaD_call

`OP_CALL`(`lvm.c:1720`)是 VM 和调用栈子系统的接口:

```c
vmcase(OP_CALL) {
  StkId ra = RA(i);
  CallInfo *newci;
  int b = GETARG_B(i);
  int nresults = GETARG_C(i) - 1;
  if (b != 0)
    L->top.p = ra + b;  /* 固定参数个数,top 标出参数末尾 */
  savepc(ci);  /* 以防出错 */
  if ((newci = luaD_precall(L, ra, nresults)) == NULL)
    updatetrap(ci);  /* C 调用,没别的事 */
  else {  /* Lua 调用:在同一个 C 帧里跑 */
    ci = newci;
    goto startfunc;
  }
  vmbreak;
}
```

要点:

- `ra` 指向被调函数(在栈上),后面跟着参数。`b` 是"参数个数+1"(0 表示参数个数由上一条指令设过的 `L->top` 决定,用于可变参数调用)。`nresults` 是期望的返回值个数(-1 表示全收)。
- `luaD_precall` 是 P4-13 的主角。它做两件事:① 把被调函数抬起来设好新 `CallInfo`;② 如果是 C 函数,直接调它并返回 NULL(VM 这一侧无需再做什么);如果是 Lua 函数,返回新建的 `newci`。
- **关键**:`newci != NULL`(Lua 调 Lua)时,**不递归调用 `luaV_execute`**,而是 `ci = newci; goto startfunc;`。这把调用栈深度从"C 栈深度"解耦成"Lua 调用链长度",深递归不爆 C 栈。返回时 `OP_RETURN` 里 `goto returning;` 回到调用者的上下文(因为 `cl/k/base/pc` 这些局部变量会从 `ci` 重新加载)。

注意 `savepc(ci)` 出现在 `luaD_precall` 前——因为 precall 可能抛错(被调函数不是可调用对象)、可能触发栈扩容、可能跑钩子。这些都属于慢路径。

尾调用 `OP_TAILCALL`(`lvm.c:1737`)更进一步:复用当前栈帧,不新开 `CallInfo`,把被调函数抬到当前 `ra` 上后 `goto startfunc`。这是 Lua 实现"尾递归不爆栈"的机制(只有符合尾调用形式的 `return f()` 才编出 `OP_TAILCALL`)。它涉及关闭当前帧的 upvalue(`luaF_closeupval`),所以更复杂,P4-15 详讲。

#### 2.6.5 返回类:OP_RETURN / RETURN0 / RETURN1

`OP_RETURN`(`lvm.c:1763`)是通用返回:

```c
vmcase(OP_RETURN) {
  StkId ra = RA(i);
  int n = GETARG_B(i) - 1;  /* 返回值个数 */
  int nparams1 = GETARG_C(i);
  if (n < 0)  /* 不固定? */
    n = cast_int(L->top.p - ra);
  savepc(ci);
  if (TESTARG_k(i)) {  /* 可能有 open upvalue? */
    ci->u2.nres = n;
    if (L->top.p < ci->top.p)
      L->top.p = ci->top.p;
    luaF_close(L, base, CLOSEKTOP, 1);  /* 关闭 upvalue */
    updatetrap(ci);
    updatestack(ci);
  }
  if (nparams1)  /* 可变参数函数? */
    ci->func.p -= ci->u.l.nextraargs + nparams1;
  L->top.p = ra + n;
  luaD_poscall(L, ci, n);  /* 把返回值搬到调用者 */
  updatetrap(ci);
  goto ret;
}
```

`TESTARG_k(i)` 在这里复用作"本帧是否有需要关闭的 upvalue"标志。有就先 `luaF_close` 把指向当前栈的 open upvalue 全部关闭(P4-14)。`luaD_poscall` 把返回值搬到调用者期望的位置,然后 `goto ret`。

`ret` 标签在 `OP_RETURN1` 末尾(`lvm.c:1823`):

```c
   ret:  /* return from a Lua function */
    if (ci->callstatus & CIST_FRESH)
      return;  /* 结束这个 C 帧 */
    else {
      ci = ci->previous;
      goto returning;  /* 在本帧里继续跑调用者 */
    }
```

`CIST_FRESH` 表示这个 `luaV_execute` 的 C 帧是专门为当前调用开的(由 `luaD_call` 新调进来),返回就 `return` 退出 C 函数。否则(当前是借上层 C 帧跑的内层 Lua 调用),`ci = ci->previous; goto returning;` 切回调用者的 `ci`,在同一个 C 帧继续。这就是 `goto startfunc` 配对的返回半边。

`OP_RETURN0`/`OP_RETURN1`(`lvm.c:1785`/`1802`)是 0/1 个返回值的特化版,**有快路径**:没开钩子时不调 `luaD_poscall`,直接在 VM 里手写搬运(`L->ci = ci->previous`、把那一个返回值搬到 `base-1` 等)。这是因为 `return`、`return nil`、`return x` 太常见,值得专门优化掉一次 `luaD_poscall` 的函数调用开销。开了钩子时退到通用路径,保证钩子能看到每次返回。

#### 2.6.6 跳转与循环:OP_JMP / FORPREP / FORLOOP

`OP_JMP`(`lvm.c:1646`):

```c
vmcase(OP_JMP) {
  dojump(ci, i, 0);
  vmbreak;
}
```

`dojump`(`lvm.c:1127`):

```c
#define dojump(ci,i,e)	{ pc += GETARG_sJ(i) + e; updatetrap(ci); }
```

`sJ` 是 5.5 新引入的跳转专用域(无符号偏移,值见 lopcodes.h),`pc` 直接加上偏移就完成跳转。`updatetrap(ci)` 在跳转后重读 `trap`——这是允许行钩子打断紧循环的钩子:没有它,本地 `trap` 永远不会变,`for i=1,1e9 do end` 这种紧循环里钩子永远触发不了。

条件跳转(`OP_EQ`/`LT`/`LE`/`TEST`/`TESTSET`)不直接跳,而是计算条件后用 `docondjump`(`lvm.c:1138`):条件满足才执行下一条 `OP_JMP`,不满足 `pc++` 跳过。所以 Lua 的条件跳转是"比较指令 + JMP"两条一对,和算术+MMBIN 是同样的"指令对"模式。

数值 for 循环最有意思。Lua 代码 `for i = 1, 10, 2 do ... end` 编译成 `OP_FORPREP` + 循环体 + `OP_FORLOOP`。栈上预留 4 个槽:`ra`(初始→后改成计数器)、`ra+1`(limit→后改成 step)、`ra+2`(step→后改成控制变量)、`ra+3`(循环体里用的 `i`)。

`OP_FORPREP`(`lvm.c:1849`)调 `forprep`(`lvm.c:214`)。`forprep` 的关键技巧是**把三个值重新编码成另一种等价但更快的表示**:整数循环时,`ra` 改成"还剩多少次迭代"(无符号计数,避免每次比较带符号溢出问题),`ra+1` 改成 step,`ra+2` 改成当前控制变量值。这样 `OP_FORLOOP` 每次只需 `count > 0` 判一次、`count--`、`idx += step`。整数循环代码内联在 `OP_FORLOOP`(`lvm.c:1831`)里:

```c
vmcase(OP_FORLOOP) {
  StkId ra = RA(i);
  if (ttisinteger(s2v(ra + 1))) {  /* 整数循环? */
    lua_Unsigned count = l_castS2U(ivalue(s2v(ra)));
    if (count > 0) {  /* 还有迭代? */
      lua_Integer step = ivalue(s2v(ra + 1));
      lua_Integer idx = ivalue(s2v(ra + 2));
      chgivalue(s2v(ra), l_castU2S(count - 1));  /* 计数器减一 */
      idx = intop(+, idx, step);
      chgivalue(s2v(ra + 2), idx);  /* 更新控制变量 */
      pc -= GETARG_Bx(i);  /* 跳回循环体开头 */
    }
  }
  else if (floatforloop(ra))
    pc -= GETARG_Bx(i);
  updatetrap(ci);  /* 允许信号打断循环 */
  vmbreak;
}
```

几个点:

- **用无符号计数器 `count`**:整数迭代总数用无符号算,避免了"`init` 接近 `MAXINT` 时 `init + step` 溢出导致比较出错"的坑。这是 for 循环正确性的一个非显然细节。
- **浮点循环走 `floatforloop`**(`lvm.c:273`):整数 case 内联、浮点 case 调函数,因为整数 for 远比浮点 for 常见,值得专门内联省一次函数调用。
- **`updatetrap(ci)` 在循环回跳点**:和 `OP_JMP` 一样,让钩子能打断紧循环。

#### 2.6.7 其他常见 case

`OP_CONCAT` 字符串拼接:把 `ra..ra+n-1` 一段值交给 `luaV_concat`,可能分配新字符串(GC 点),所以 `ProtectNT` + `checkGC`。`OP_LEN` 取长度:`luaV_objlen` 对表是 `luaH_getn`、对字符串是 `len`,可能调 `__len` 元方法,`Protect`。`OP_NEWTABLE` 建表:`luaH_new` 分配,`checkGC`。`OP_CLOSURE` 造闭包:`pushclosure`(`lvm.c:834`)填 upvalue,`halfProtect` + `checkGC`。这些都遵循同一模式:**纯计算无 Protect,可能分配/抛错/调元方法的进 Protect 并挂 checkGC**。

### 2.7 GC 穿插:checkGC 与 luaC_condGC

VM 循环里 GC 怎么步进?看 `checkGC` 宏(`lvm.c:1178`):

```c
#define checkGC(L,c)  \
	{ luaC_condGC(L, (savepc(ci), L->top.p = (c)), \
	                         updatetrap(ci)); \
	   luai_threadyield(L); }
```

`luaC_condGC` 在 `lgc.h:233`:

```c
#define luaC_condGC(L,pre,pos) \
	{ if (G(L)->GCdebt <= 0) { pre; luaC_step(L); pos;}; \
	  condchangemem(L,pre,pos,0); }
```

机制是:**检查 `G(L)->GCdebt`**(全局分配债务,P0-01 讲过)。分配内存时 `GCdebt` 增加,被 GC 回收时减少。当 `GCdebt <= 0`(债务到期,该还了)时,在 `pre`(刷新 `savedpc` 和 `L->top`)之后调 `luaC_step`——**推进一步增量 GC**(三色标记的一小步,P5-16),再 `pos`(`updatetrap`,因为 step 里可能开了钩子或改了栈)。

`luaC_step` 不是扫完整堆,而是按 `GCtotalbytes` 和 `pause`/`stepmul` 参数算出"这一步做多少工作",通常几微秒到几十微秒就返回。这就是"增量"在 VM 里的落地:**GC 不是单独的 phase,而是挂在分配点上、每次分配可能顺手做一小步**。

`checkGC` 只出现在**可能分配**的指令上:`OP_NEWTABLE`(`lvm.c:1425`)、`OP_CONCAT`(`lvm.c:1631`)、`OP_CLOSURE`(`lvm.c:1933`)、`OP_DUP`(部分字符串操作)等。纯计算指令(`OP_MOVE`/`OP_ADD` 等)不分配,不挂 `checkGC`——它们连 GC 检查都不做,跑得最快。

这套设计的 sound 之处在于:**GC 推进的频率自动和分配频率成正比**。分配越频繁(临时对象越多),GC 步进越频繁;不分配的纯计算循环,GC 完全不动它。这正好是"该回收的时候才回收"——既不会卡住宿主(每步都很小),也不会让内存涨爆(分配多就推进多)。P5-16 会详讲三色状态机怎么保证这种可中断的正确性。

`luai_threadyield(L)`(`lvm.c:1173`,默认展开成 `lua_unlock(L); lua_lock(L);`)是给多线程嵌入留的让出点,单线程宿主是空操作。

### 2.8 错误定位:savedpc 的双重身份

`ci->u.l.savedpc`(`lstate.h:193`)在 `CallInfo` 里只占一个指针大小,却身兼两职:

1. **帧间断点**:每次进入 `luaV_execute`(`lvm.c:1212`),从 `ci->u.l.savedpc` 读 pc;`luaD_call` 建新帧时,旧帧的 `savedpc` 记住它执行到哪了,新帧从自己的 `savedpc`(被调函数 code 起点)开始。这样调用栈每一层都知道自己执行到哪。
2. **错误时的"现场"**:`luaG_runerror` 用 `longjmp` 跳到 `errorJmp`,跳走前调试系统从当前 `ci->u.l.savedpc` 反查 `Proto *` 的 `lineinfo` 数组(P2 编译期生成,记录每条指令对应的源码行),把错误信息定位到"哪个文件第几行"。这就是为什么所有 `Protect` 都要先 `savepc(ci)`——**跳走前必须把 savedpc 刷新到"当前这条可能出错的指令"**,否则错误信息会指到上一条 save 过的指令,误导调试。

注意平时循环里 `pc` 是局部变量,和 `savedpc` 是不同步的(`savedpc` 只在 Protect/checkGC 点刷新)。这是刻意的:**写 `savedpc` 是一次内存写,百万次循环里这是可测的开销,只在该写时才写**。

`vmfetch` 里 `trap` 为真时还会调 `luaG_traceexec(L, pc)`(`lvm.c:1187`),它负责行钩子/计数钩子的触发判定——也依赖 `pc` 当前值对照 `lineinfo` 判断"是否跨行了"。这是另一个 pc 用途:调试观测。

---

## 三、为什么这样设计是 sound 的

### 3.1 computed goto sound:为什么快而不破坏正确性

computed goto 用 GCC 扩展(`&&label`/`goto *ptr`),乍看不标准。但它的 sound 体现在两点:

- **语义等价于 switch**:每个 case 仍然是一个标签,跳转目标由操作码(0..NUM_OPCODES-1)索引一张编译期常量表得到。表是 `static const void *const disptab[NUM_OPCODES]`,内容在编译期就定死,和操作码枚举一一对应(`ljumptab.h` 顶部的 sed 命令注释就是自动生成这个对应关系的脚本)。不会跳到野地址。
- **可移植性有退路**:`LUA_USE_JUMPTABLE` 可由 `-D` 关掉,退回标准 `switch`。MSVC 等不支持扩展的编译器自动用 switch。两种模式下 case 体完全一样(都由 `vmcase`/`vmbreak` 宏包装),只是分发和循环边界不同。

快的来源(第一节已述)是分支预测友好,**但 sound 的来源是分发表和操作码枚举同源**。`NUM_OPCODES`(`lopcodes.h`)和 `disptab[]` 数组大小必须一致,这一点由 `#include "ljumptab.h"` 强制——表里 83 项,枚举也是 83 个,任何加新操作码的改动都会让两边同时更新。

### 3.2 快慢分流 sound:为什么 Protect 不会漏

快路径不进 Protect 的前提是"它真的不会抛错/分配/改栈/触发钩子"。这个前提由 case 实现保证,且 `lua_assert` 在调试构建里会查。看主循环开头(`lvm.c:1228-1231`):

```c
lua_assert(base == ci->func.p + 1);
lua_assert(base <= L->top.p && L->top.p <= L->stack_last.p);
/* for tests, invalidate top for instructions not expecting it */
lua_assert(luaP_isIT(i) || (cast_void(L->top.p = base), 1));
```

第三条特别有意思:对于"不读 top"的指令(纯寄存器操作,`luaP_isIT` 判定),测试构建里故意把 `L->top.p` 设成 `base`——这样任何慢路径里若忘了刷新 `top` 就会被后续断言抓到。**这是用断言强制"快路径不得依赖 top 正确"的纪律**。

`Protect` 家族之所以分级(`Protect`/`ProtectNT`/`halfProtect`),是因为不同慢操作改的状态不同。用错级别要么浪费(该轻用了重)、要么漏(该重用了轻,跳走后状态不一致)。三级的边界由注释明确(`lvm.c:1155-1167`),写新 case 时按"这个慢操作会改什么"选级别。

### 3.3 整数快速路径 sound:溢出与 UB

`intop(op,v1,v2)`(`lvm.h:73`)用 `l_castU2S(l_castS2U(v1) op l_castS2U(v2))` 把有符号运算转成无符号再做。为什么绕这一圈?因为 C 标准里有符号整数溢出是未定义行为(UB),编译器在 UB 上可以做令人吃惊的优化(比如假设不溢出,删掉溢出检查)。Lua 把运算转成无符号(无符号溢出是良定义的环绕),再转回有符号读结果,绕开了 UB——**语义在所有合规 C 编译器上一致**。

这保证了"`MAXINT + 1`"这种边界在 Lua 层面行为确定(得到 `MININT`,环绕),不会因编译器优化而不可预测。代价是算术结果可能"错"(环绕了),但 Lua 的整数语义本来就是环绕(不像 Python 的大整数自动扩展),用户预期如此。

### 3.4 GC 增量穿插 sound:为什么可中断不丢不重

`luaC_step` 推进一步后立即返回,中间三色状态机的"灰色"队列(`global_State` 的 `gray` 链)保留了"还没扫完的对象"。下一次 `checkGC` 触发时从灰色队列继续。这要求三色不变式在每次 step 后仍然成立(P5-16 详讲):

- 黑对象(已扫描完)不能指向白对象(未扫)——靠写屏障 `luaC_barrierback` 保证,表写入点(`OP_SET*` 的 `luaV_finishfastset`)就是屏障的挂载点之一。
- 灰对象(在队列里)最终都会被扫到——靠 `gray` 链表不丢。

VM 循环的每个写屏障挂载点(`luaV_finishfastset`、`OP_SETUPVAL` 的 `luaC_barrier`、`OP_CLOSURE` 的 `luaC_objbarrier`)就是 GC 与执行的交汇。这些点都是"对象图可能新增一条边"的地方——新增边时若破坏不变式,屏障立刻把目标对象重新标灰。**GC 的可中断性,最终落地到 VM 里少数写屏障调用**。这是"增量 GC 必须可中断"在执行侧的具体投影。

### 3.5 goto startfunc/returning sound:为什么不爆 C 栈

Lua 调 Lua 用 `goto startfunc` 复用 C 帧,意味着不论 Lua 调用链多深,`luaV_execute` 这个 C 函数只开一次。但 `luaD_call` 调 C 函数时是真正递归进 C 栈的(C 函数可能再调 Lua,那时再开新 `luaV_execute` C 帧)。所以 Lua 的栈深度限制其实是两层:

- Lua→Lua 调用深度:受 `LUAI_MAXCCALLS`(`lvm.c` 附近,默认 200)限制,不占 C 栈。
- Lua→C→Lua 嵌套深度:占 C 栈,受宿主线程栈大小限制。

这个分层让"Lua 内部递归"非常便宜(只换 `ci`,不换 C 帧),而"Lua 调 C 再调 Lua"才付 C 栈代价——后者是嵌入宿主的边界,本就该谨慎。这是 Lua 为嵌入做的又一个精简:**把语言内部的调用栈做成数据结构,把跨语言边界才留给 C 栈**。

---

## 四、★对照 CPython + 回扣主线

CPython 的主循环是 `_PyEval_EvalFrameDefault`(`Python/ceval.c`),和 `luaV_execute` 是同生态位的两套实现。把它们放在一起,正好照出 Lua 每一个执行侧取舍的相对位置。

### 4.1 分发机制:都用 computed goto,但 Lua 更纯

两者都用 computed goto(CPython 3.11+ 用 `TARGET(op)` 宏 + `goto *opcode_targets[op]`,`Python/ceval.c`;Lua 用 `disptab[]` + `goto *disptab[x]`,`ljumptab.h:12`)。Eli Bendersky 的经典文章和 CPython issue #42804 都指出,这一招对解释器循环是标配优化,两者在这一点上打平。

差异在退路:CPython 强制要求编译器支持 computed goto(否则 `#error`),Lua 保留了 switch 退路。代价是 Lua 可移植性更好,收益是 CPython 少维护一套 switch 分支。

### 4.2 指令模型:寄存器式 vs 栈式(P3-12 详讲)

这一栏是 P3-12 的主场,这里先点出执行侧的差异。CPython 的主循环每条指令都在压栈/弹栈,典型的 `BINARY_OP` 是"弹两个、算、压一个",每条 `LOAD_FAST`/`STORE_FAST` 都是一次值栈访问。Lua 的 `OP_ADD` 一条指令同时取两个寄存器、算、写回,中间无栈操作。

后果在主循环里直接可见:**同一段逻辑,Lua 的 `vmdispatch` 次数远少于 CPython**。每次分发都是一次间接跳转 + 分支预测,少一半分发就是实打实的快。P3-12 会用具体反汇编对照量化这个差距。

### 4.3 GC 与引用计数:增量步进 vs 每次指针操作

这是执行侧最深的对照。

- **Lua 的 GC 推进**只发生在 `checkGC` 点(`OP_NEWTABLE`/`OP_CONCAT`/`OP_CLOSURE` 等分配指令)。纯计算指令(`OP_MOVE`/`OP_ADD`/`OP_JMP` 等)完全不做 GC 工作。GC 的单位是"一次 step",由 `GCdebt` 控制,和分配量成正比。
- **CPython 的引用计数**是每次指针赋值都做的。`LOAD_FAST` 要 `Py_INCREF`,`STORE_FAST` 要 `Py_DECREF` 旧值 `Py_INCREF` 新值,`BINARY_OP` 结果产生一次 INCREF。**没有一条指令能逃掉引用计数**,因为每条指令都在搬值。

这意味着 CPython 主循环里,每条指令都额外背着 2-4 次 `Py_INCREF`/`Py_DECREF`(原子读改写或非原子,看 `Py_GIL_DISABLED`)。Lua 把这个开销完全消灭——值的生命周期由 GC 统一管,指令只管搬值。代价是 Lua 的回收不那么即时(要等 GC step 扫到),且需要复杂的三色状态机维持可中断的正确性(第三节已述)。但**在"嵌入宿主不能卡顿"这个目标下,Lua 的增量明显更合适**:宿主可以预测每次 `luaC_step` 的耗时上限(由 `stepmul` 控制),而 CPython 的分代标记周期是一次完整 STW,对游戏帧率不友好。

### 4.4 错误模型:longjmp vs 异常传播

Lua 用 `setjmp`/`longjmp`(`luaD_rawrunprotected` 包 `setjmp`,`luaG_runerror` 调 `longjmp` 跳回最近的 protect 点),`Protect` 宏就是显式包 `setjmp`。CPython 用 C 异常(`_PyErr_SetString` 设错误状态,主循环里检查 `_PyErr_Occurred` 决定是否跳到错误处理)。两者都有效,Lua 的方式更轻(一次 `setjmp` 保护的代码块可以很大,只要其中任意点 `longjmp` 都能回到 protect 点),CPython 的方式更显式(每条指令后都可能检查错误标志)。Lua 把 `setjmp` 的成本(一次 protect 一次 `setjmp`,后者在主流架构上不便宜)集中到慢路径,快路径完全不付。

### 4.5 专门化:CPython 的 adaptive interpreter

5.5 的 Lua 没有指令专门化(specialization)——`OP_GETTABLE` 就是 `OP_GETTABLE`,不区分键是整数还是字符串(在 case 内部 `if` 分流)。CPython 3.11+ 引入了 adaptive specializing interpreter(PEP 659):`LOAD_ATTR` 会在运行时观察目标类型,把自己改写成 `LOAD_ATTR_INSTANCE_VALUE`、`LOAD_ATTR_MODULE` 等专门化指令,带 inline cache 跳过类型检查。这让 CPython 在热点路径上能接近 JIT 的效果,但代价是主循环复杂度暴涨(`Python/generated_cases.c.h` 里几百条专门化 case)。

Lua 选择了相反的方向:**保持操作码集合小而通用,靠 case 内部的 `if` 快速分流**。这让 VM 源码简单(`lvm.c` 才 1972 行,CPython `ceval.c` + `generated_cases.c.h` 合计几万行),但也意味着 Lua 没有 CPython 那种"运行时学习"的优化空间。这是"小而精简"和"复杂而快"的经典取舍——Lua 选了前者,因为它首要目标是嵌入而不是极致性能(LuaJIT 才是奔着极致性能的另一条路)。

### 4.6 回扣主线

把这一章的所有机制收束回主线:**统一与精简换小而快**。

- **统一**:所有字节码共用一个循环、一张操作码表、一套 RA/RB/RC 寻址。没有按指令类别分发的多层 switch,没有为某类指令单独的执行路径。一个 `for(;;)` + `vmdispatch` 解释 83 个操作码,这是执行侧的"统一"。
- **精简①**:computed goto 把分发压到一次间接跳转;`vmfetch` 裸读指令;`RA`/`RB`/`RC` 宏内联展开。每个环节都砍掉了可砍的检查。
- **精简②**:快慢分流让 99% 的指令不付错误处理和 GC 的税;整数快速路径、表访问快速路径、for 循环整数内联,把最常见的几类操作压到最少指令数。
- **精简③**:`goto startfunc`/`returning` 把 Lua 调用栈做成数据结构,Lua→Lua 调用不占 C 栈,深递归便宜。
- **小**:`luaV_execute` 本体不到 800 行,全 `lvm.c` 1972 行。对比 CPython 主循环(`ceval.c` + `generated_cases.c.h`)的规模,这个"心脏"小得惊人,却解释了一门完整的语言。

这个循环的每一个设计——从 `trap` 的本地缓存到 `Protect` 的三级分级,从 `intop` 绕开 UB 到 `luaC_condGC` 的债务驱动——都在实践同一件事:**用更少的机制,换更多的能力**。这正是 Lua 又小又快的根源,也是这一章一行行拆开 `luaV_execute` 想让你看清的东西。

---

## 附录:5.5 vs 老资料差异

写这一章时 Grep/Read 核实发现的、与讲 5.3/5.4 的老资料冲突之处,以 5.5.0 源码为准:

1. **跳转域 `sJ`**:5.5 新增 `OP_JMP` 专用的 `sJ` 无符号跳转域(`dojump` 里 `GETARG_sJ(i)`,`lvm.c:1127`),老版本(5.4)用 `sBx`。这是 5.5 指令编码重排的一部分,P3-10 详讲。
2. **`vmbreak` 在 computed goto 下不是 `break`**:`ljumptab.h:16` 把它定义成 `vmfetch(); vmdispatch(GET_OPCODE(i));`,即每个 case 自己重新取指并跳转,**不是跳出 switch 后由外层 for 循环取指**。老资料讲 5.3 的 switch 实现时 `vmbreak` 就是 `break`,两者语义不同。
3. **算术+MMBIN 指令对**:`OP_MMBIN`/`MMBINI`/`MMBINK` 三种是 5.4 引入、5.5 沿用。5.3 的算术指令内部自己判元方法,没有单独的 MMBIN 指令。讲 5.3 的资料里看不到 `pi = *(pc - 2)` 这种"回看上一条指令"的写法。
4. **`OP_RETURN0`/`RETURN1` 内联快路径**:`lvm.c:1785`/`1802` 在没开钩子时直接手写 `L->ci = ci->previous` 搬运返回值,不调 `luaD_poscall`。这是 5.4 后期加入的优化,5.3 资料里 `OP_RETURN` 是统一走 `luaD_poscall`。
5. **`OP_VARARGPREP`/`OP_VARARG`/`OP_GETVARG` 并存**:5.5 既有老的 `OP_VARARGPREP`(进可变参数函数时调一次)+ `OP_VARARG`(取可变参数),又新增 `OP_GETVARG`(`lvm.c:1943`,直接取单个可变参数)。讲 5.4 的资料通常没有 `OP_GETVARG`。
6. **`OP_ERRNNIL`**:5.5 新增的"非 nil 即报错"指令(`lvm.c:1949`),用于优化 `local x = assert(...)` 这种模式。老资料没有。
7. **`StkIdRel` 相对表示**:`CallInfo.func`/`top` 都是 `StkIdRel`(`lstate.h:188-189`),不是老资料的 `StkId`(`TValue *` 绝对指针)。这是 5.5 为栈搬家优化做的改动,`vmfetch` 里 `updatebase` 依赖这个。
8. **`trap` 字段在 `CallInfo.u.l`**:`lstate.h:194`,`volatile l_signalT trap`,5.5 把它从 `lua_State` 层下沉到 `CallInfo` 层,让每个调用帧独立跟踪 trap 状态。老资料讲 5.3/5.4 时 trap 相关逻辑在 `lua_State` 上。

主控核验要点:`luaV_execute@lvm.c:1198`、主循环 `for(;;)@1217`、`vmfetch@1185`、`vmdispatch@1193`(switch)/`ljumptab.h:12`(goto)、`Protect@1158`/`ProtectNT@1161`/`halfProtect@1167`、`RA@1102`/`RKC@1110`、`OP_MOVE@1233`、`OP_GETTABLE@1311`、`luaV_finishget@291`、`op_arith@1004`/`op_arith_aux@992`、`OP_MMBIN@1556`、`OP_CALL@1720`、`OP_RETURN@1763`/`RETURN0@1785`/`RETURN1@1802`、`OP_JMP@1646`/`dojump@1127`、`OP_FORLOOP@1831`/`forprep@214`、`checkGC@1178`/`luaC_condGC@lgc.h:233`、`savedpc@lstate.h:193`。

---

*下一章 [P3-12 寄存器式精解:为什么比栈式指令少](P3-12-寄存器式精解-为什么比栈式指令少.md):把本章反复提到的"寄存器式 vs 栈式"量化——同一段 Lua 代码编译成寄存器式字节码 vs CPython 编译成栈式字节码,逐条对照指令数,讲透寄存器式到底省在哪里、代价是什么。*
