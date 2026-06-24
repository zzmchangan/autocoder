# P4-15 尾调用、可变参数与 to-be-closed

> **本书主线**:统一与精简换小而快。**二分法**:编译侧(源码→字节码)↔ 执行侧(VM 执行 + 管值生命周期)。**本章位置**:执行侧(P4 函数、闭包与调用)收尾。**★对照**:CPython(无尾调用优化 / `*args` / `contextlib`)。**源码**:lua-5.5.0,`lvm.c` `ldo.c` `lfunc.c` `ltm.c` `lparser.c` `lcode.c` `lobject.h` `lstate.h` `lparser.h`。**基调**:纯直球,不用比喻。

---

## 一、这章解决什么问题

P4-13 讲了普通调用怎么压一个新 `CallInfo` 帧到值栈上,P4-14 讲了闭包怎么用 upvalue 捕获词法作用域。但还剩三类函数语义没讲,而且它们恰好都落在「return」这个作用域边界上:

1. **尾调用**。Lua 代码 `return f()` 里,`f()` 是函数调用的最后一个动作,它返回什么,当前函数就原样返回什么。一个朴实的实现会这样:`f()` 先压一个新调用帧执行,执行完把结果搬回当前帧,当前帧再把自己的结果搬回上一层。这样每深一层就多压一帧。如果这段代码写在递归里(状态机、循环用递归写),栈会一路涨下去,几万层就爆。但 `return f()` 在语义上有个特点:当前函数 `return f()` 之后**自己再没有任何动作**——它对 `f()` 的结果只是「原样传上去」。既然如此,当前帧在调用 `f` 之后就没用了,完全可以把 `f` 的帧**直接盖在当前帧的位置上**,复用当前帧那一块栈空间,而不是在它之上新压一块。这就是 proper tail call(真尾调用):尾调用不增长调用栈,可以无限递归。问题是:VM 怎么在源码层面做到这点?什么条件下能做、什么条件下做不了?

2. **可变参数**。Lua 函数可以声明 `function f(a, b, ...)`,固定参数 `a`、`b` 之外的多余实参被打包成 `...`。函数体里用 `...` 取出全部多余参数、用 `select(2, ...)` 取第几个、用 `{...}` 打包成表。这套语义背后,实参在值栈上是怎么放的?`...` 取值又是怎么定位的?而且 5.5 在这里有一个老资料完全没提到的硬变化——5.5 引入了**两种**可变参数机制:`PF_VAHID`(隐藏参数,实参留在栈上)和 `PF_VATAB`(可变参数表,实参打包成一个 Table),编译器根据函数体怎么用 `...` 自动二选一。还引入了 **5.5 全新的命名可变参数** `function f(...args)`(给 `...` 起个名字)和配套的新操作码 `OP_GETVARG`。讲老版本(5.3/5.4)的书根本没这些。

3. **to-be-closed 变量**。Lua 5.4 起加了 `<close>` 属性:`local f <close> = io.open(...)`。这个变量在离开作用域时(块结束、`return`、出错)**自动调用它的 `__close` 元方法**,参数是变量自己(出错时还多一个错误对象)。这是 RAII 风格的资源释放,用来保证文件句柄、锁、数据库连接一定被关掉。问题是:VM 怎么知道哪些变量是 to-be-closed?离开作用域时按什么顺序关?`__close` 自己又出错怎么办?它和尾调用、`return` 又是怎么咬合的——为什么有 to-be-closed 变量在作用域里时,`return f()` 就**不能**做尾调用?

这三件事看起来各管一头,但本质是同一个问题:**函数的边界上要做哪些收尾工作,以及这些收尾工作能不能让调用栈保持精简。** 尾调用是「精简」的正面典范——能不压帧就不压;to-be-closed 是「边界上的副作用」——它恰恰是阻止尾调用的那个收尾工作;可变参数是「实参在栈上的另一种布局」。讲清这三者,就把 P4「函数怎么被调起来」彻底收束了,也把执行侧从「调用/控制流」过渡到下一阶段「值的生命周期由 GC 管」。

---

## 二、源码怎么实现

### 2.1 尾调用:编译期先识别,执行期复用帧

先看编译期。Lua 什么时候把一个 `CALL` 编译成 `TAILCALL`?在 `lparser.c` 的 `retstat`(返回语句的语法分析,`lparser.c:2017`)里:

```c
static void retstat (LexState *ls) {
  /* stat -> RETURN [explist] [';'] */
  FuncState *fs = ls->fs;
  expdesc e;
  int nret;  /* number of values being returned */
  int first = luaY_nvarstack(fs);  /* first slot to be returned */
  if (block_follow(ls, 1) || ls->t.token == ';')
    nret = 0;  /* return no values */
  else {
    nret = explist(ls, &e);  /* optional return values */
    if (hasmultret(e.k)) {
      luaK_setmultret(fs, &e);
      if (e.k == VCALL && nret == 1 && !fs->bl->insidetbc) {  /* tail call? */
        SET_OPCODE(getinstruction(fs,&e), OP_TAILCALL);
        lua_assert(GETARG_A(getinstruction(fs,&e)) == luaY_nvarstack(fs));
      }
      nret = LUA_MULTRET;  /* return all values */
    }
    ...
  }
  luaK_ret(fs, first, nret);
  ...
}
```
(`lparser.c:2017-2046`)

关键判断在 `lparser.c:2029`:

```c
if (e.k == VCALL && nret == 1 && !fs->bl->insidetbc) {  /* tail call? */
  SET_OPCODE(getinstruction(fs,&e), OP_TAILCALL);
```

三个条件**同时**成立才编成 `TAILCALL`:

- `e.k == VCALL`:返回值表达式恰好是一个函数调用(`return f(...)`,且 `f(...)` 是返回值列表里唯一的一项)。
- `nret == 1`:返回值列表只有这一项(`return f()`,不是 `return f(), g()`)。
- `!fs->bl->insidetbc`:当前不在任何 to-be-closed 变量的作用域内。

第三个条件最容易被忽略,也最关键。`insidetbc` 是 `BlockCnt` 的字段(`lparser.c:56`),在一个块里只要声明了 to-be-closed 变量就会被置位(`marktobeclosed`,`lparser.c:463-468`,设 `bl->insidetbc = 1`),并且**向内层块继承**(`lparser.c:726-727`,`bl->insidetbc = (fs->bl != NULL && fs->bl->insidetbc)`)。也就是说,只要外层某个块里有 `<close>` 变量还活着,内层任何 `return f()` 都不能做尾调用。为什么?下面 2.3 讲 to-be-closed 时会落到源码:因为尾调用要复用当前帧,而当前帧的 to-be-closed 变量还没关——`__close` 必须在帧被覆盖**之前**触发,这就和「复用帧」冲突。所以编译器在 `return f()` 处遇到 `insidetbc` 就老老实实编成普通 `CALL` + `RETURN`,让执行期按正常路径先关变量、再返回。

普通情况下(没有 to-be-closed 变量),编译器把那个 `CALL` 指令的操作码直接改写成 `TAILCALL`,其余字段不动。注意这里 `e` 已经被 `luaK_setmultret` 处理过,`SET_OPCODE` 只是改操作码,参数布局和原 `CALL` 一致(函数在 `A` 寄存器、参数个数在 `B`、固定参数+1 在 `C`——`C` 字段在 5.5 用来传递可变参数函数的 `nparams1`,见 2.2)。

再看执行期。`OP_TAILCALL` 在 `lvm.c:1737`:

```c
vmcase(OP_TAILCALL) {
  StkId ra = RA(i);
  int b = GETARG_B(i);  /* number of arguments + 1 (function) */
  int n;  /* number of results when calling a C function */
  int nparams1 = GETARG_C(i);
  /* delta is virtual 'func' - real 'func' (vararg functions) */
  int delta = (nparams1) ? ci->u.l.nextraargs + nparams1 : 0;
  if (b != 0)
    L->top.p = ra + b;
  else  /* previous instruction set top */
    b = cast_int(L->top.p - ra);
  savepc(ci);  /* several calls here can raise errors */
  if (TESTARG_k(i)) {
    luaF_closeupval(L, base);  /* close upvalues from current call */
    lua_assert(L->tbclist.p < base);  /* no pending tbc variables */
    lua_assert(base == ci->func.p + 1);
  }
  if ((n = luaD_pretailcall(L, ci, ra, b, delta)) < 0)  /* Lua function? */
    goto startfunc;  /* execute the callee */
  else {  /* C function? */
    ci->func.p -= delta;  /* restore 'func' (if vararg) */
    luaD_poscall(L, ci, n);  /* finish caller */
    updatetrap(ci);  /* 'luaD_poscall' can change hooks */
    goto ret;  /* caller returns after the tail call */
  }
}
```
(`lvm.c:1737-1762`)

逐行看。`ra` 是被调函数在栈上的位置(也就是 `CALL`/`TAILCALL` 的 A 寄存器)。`b` 是参数个数+1(含函数本身)。`nparams1` 是 `C` 字段:5.5 里,如果当前函数是可变参数函数,编译器在 `luaK_finish`(`lcode.c:1949-1950`)里把 `C` 设成 `numparams + 1`,用来标记「我是可变参数函数,真正的 `func` 指针和栈上看到的 `func` 之间差了 `nextraargs + nparams1`」(因为可变参数函数用了隐藏参数机制,见 2.2)。`delta` 就是这个差值,用来在搬运时把 `func` 指针修正回真实位置。

`TESTARG_k(i)` 这一位是编译器在 `luaK_finish` 里设的(`lcode.c:1947-1948`,`if (fs->needclose) SETARG_k(*pc, 1)`):表示当前函数有 upvalue 需要关闭(比如内层闭包捕获了当前函数的局部变量)。如果置位,先 `luaF_closeupval(L, base)` 关掉当前帧的开放 upvalue——因为这些 upvalue 指向的栈槽马上要被被调函数覆盖,必须先把它们从「指向栈」改成「指向 upvalue 自己的值槽」(P4-14 讲过的开合)。紧接着两个 `lua_assert`:

```c
lua_assert(L->tbclist.p < base);  /* no pending tbc variables */
lua_assert(base == ci->func.p + 1);
```

第一个断言至关重要:**到 `OP_TAILCALL` 这里,当前帧不得有任何待关的 to-be-closed 变量**(`L->tbclist.p < base` 表示所有 to-be-closed 变量都在当前帧的 `base` 之下,即不属于当前帧)。这正是编译期 `!fs->bl->insidetbc` 那个条件在运行期的对应保证——编译器保证不会在有 to-be-closed 变量的作用域里发出 `TAILCALL`,所以运行期这里可以直接断言「没有」。如果有,尾调用复用帧就会把还没关的变量连同帧一起覆盖掉,`__close` 永远不会触发,资源泄漏。这个断言是「尾调用 sound」的运行期守门员。

然后是核心:`luaD_pretailcall`。它在 `ldo.c:669`:

```c
int luaD_pretailcall (lua_State *L, CallInfo *ci, StkId func,
                                    int narg1, int delta) {
  unsigned status = LUA_MULTRET + 1;
 retry:
  switch (ttypetag(s2v(func))) {
    case LUA_VCCL:  /* C closure */
      return precallC(L, func, status, clCvalue(s2v(func))->f);
    case LUA_VLCF:  /* light C function */
      return precallC(L, func, status, fvalue(s2v(func)));
    case LUA_VLCL: {  /* Lua function */
      Proto *p = clLvalue(s2v(func))->p;
      int fsize = p->maxstacksize;  /* frame size */
      int nfixparams = p->numparams;
      int i;
      checkstackp(L, fsize - delta, func);
      ci->func.p -= delta;  /* restore 'func' (if vararg) */
      for (i = 0; i < narg1; i++)  /* move down function and arguments */
        setobjs2s(L, ci->func.p + i, func + i);
      func = ci->func.p;  /* moved-down function */
      for (; narg1 <= nfixparams; narg1++)
        setnilvalue(s2v(func + narg1));  /* complete missing arguments */
      ci->top.p = func + 1 + fsize;  /* top for new function */
      lua_assert(ci->top.p <= L->stack_last.p);
      ci->u.l.savedpc = p->code;  /* starting point */
      ci->callstatus |= CIST_TAIL;
      L->top.p = func + narg1;  /* set top */
      return -1;
    }
    default: {  /* not a function */
      checkstackp(L, 1, func);  /* space for metamethod */
      status = tryfuncTM(L, func, status);  /* try '__call' metamethod */
      narg1++;
      goto retry;  /* try again */
    }
  }
}
```
(`ldo.c:669-704`)

这是尾调用的心脏。先看被调函数的类型分三种:

- **C 函数**(C closure `LUA_VCCL` 或 light C function `LUA_VLCF`):走 `precallC`。C 函数没法做「复用帧」——C 函数的调用是宿主直接调一个 C 函数指针,VM 控制不了它的栈布局。所以对 C 函数的「尾调用」其实是降级:它仍然会压一个新帧跑完 C 函数(`precallC` 里 `prepCallInfo` 建新 `ci`),跑完 `luaD_poscall` 收尾,然后回到调用者继续 `goto ret`。换句话说,**对 C 函数的尾调用不省帧**。这是 proper tail call 在 Lua 里的一个限制:C 函数不享受尾调用优化。但这是合理的——C 函数的栈由 C 调用约定管,VM 无权复用。
- **Lua 函数**(`LUA_VLCL`):这才是真尾调用。注意 `luaD_pretailcall` **没有调用 `next_ci`/`prepCallInfo` 建新帧**,而是直接操作当前 `ci`。
- **非函数**(default 分支):尝试 `__call` 元方法,把元方法找到后重试。

Lua 函数分支的几行是搬运的精髓:

```c
ci->func.p -= delta;  /* restore 'func' (if vararg) */
for (i = 0; i < narg1; i++)  /* move down function and arguments */
  setobjs2s(L, ci->func.p + i, func + i);
```

`func` 是被调函数当前在栈上的位置(`ra`,在当前帧的局部变量区里)。`ci->func.p` 是**当前帧**的 `func` 指针(当前正在执行的函数在栈上的位置,也就是当前帧的起点)。这一步做的事:把「被调函数 + 它的参数」这一串(`func[0], func[1], ..., func[narg1-1]`)整体**往下搬**,搬到**当前帧的 `func` 位置**——也就是覆盖掉当前函数自己。搬完后:

```c
ci->u.l.savedpc = p->code;  /* starting point */
ci->callstatus |= CIST_TAIL;
L->top.p = func + narg1;  /* set top */
return -1;
```

把当前 `ci` 的 `savedpc`(执行到的字节码位置)重置成被调函数 `Proto` 的代码起点 `p->code`。注意这里**没有创建新的 `CallInfo`**——`ci` 还是原来那个,只是它的 `func`、`savedpc`、`top` 全被改成了被调函数的。返回 `-1` 告诉 `OP_TAILCALL` 这是 Lua 函数。

回到 `OP_TAILCALL`:`n < 0`(Lua 函数)就 `goto startfunc`——直接跳到解释器循环开头,开始执行被调函数的字节码,**用的还是当前这个 C 栈帧、当前这个 `CallInfo`**。当前函数的字节码从此再也不会被执行(它的 `return` 已经在 `OP_TAILCALL` 里「完成」了),它的栈空间被被调函数复用。这就是「栈不增长」的物理实现:**没有新压 `CallInfo`,值栈没有新占一段,只是把当前帧的内容换成了被调函数**。

被调函数执行完,它的 `RETURN`/`RETURN0`/`RETURN1` 会走 `luaD_poscall`,而 `poscall` 里的 `L->ci = ci->previous` 回到的不是「当前函数的调用者」,而是「当前函数的调用者的调用者」——因为 `ci` 没换,`ci->previous` 一直指向更上一层。结果就是:被调函数的返回值**直接送到了当前函数的调用者手里**,中间没有当前函数这一层。这正是 proper tail call 的语义。

把这一段和普通 `OP_CALL`(`lvm.c:1720-1735`)对照看就格外清楚。普通调用:

```c
vmcase(OP_CALL) {
  StkId ra = RA(i);
  CallInfo *newci;
  ...
  if ((newci = luaD_precall(L, ra, nresults)) == NULL)
    updatetrap(ci);  /* C call; nothing else to be done */
  else {  /* Lua call: run function in this same C frame */
    ci = newci;
    goto startfunc;
  }
  vmbreak;
}
```

`luaD_precall`(`ldo.c:715`)里对 Lua 函数会调 `prepCallInfo`(`ldo.c:628`)→ `next_ci`(`ldo.c:619`)→ 拿一个**新的** `CallInfo`,挂在 `L->ci->next` 上,`ci->func.p = func` 指向新位置。然后回到 `OP_CALL`,`ci = newci` 切到新帧,`goto startfunc`。每深一层调用,`CallInfo` 链就长一个节点,值栈就多用一段。尾调用把这一整套都省了。

### 2.2 可变参数:5.5 的两套机制与命名可变参数

可变参数是 5.5 改动最大的地方之一,也是老资料最容易出错的地方。讲 5.3/5.4 的书里,可变参数只有一种实现:多余实参留在值栈上固定参数之后,`VARARG` 指令把它们拷到目标寄存器。5.5 在此基础上又加了**可变参数表**和**命名可变参数**两套新机制,源码里表现为两个 Proto 标志位和一组新操作码。

先看 Proto 的标志位,`lobject.h:586-597`:

```c
#define PF_VAHID	1  /* function has hidden vararg arguments */
#define PF_VATAB	2  /* function has vararg table */
#define PF_FIXED	4  /* prototype has parts in fixed memory */
...
#define isvararg(p)	((p)->flag & (PF_VAHID | PF_VATAB))
...
#define needvatab(p)	((p)->flag |= PF_VATAB)
```

`isvararg` 把两种都算可变参数函数。`PF_VAHID`(hidden,隐藏参数)和 `PF_VATAB`(vararg table,可变参数表)是互斥的两套实现。编译期怎么决定用哪套?

声明阶段。函数声明 `function f(a, b, ...)` 在 `lparser.c` 的 `parlist`(`lparser.c:1065`)里处理。遇到 `...`(`TK_DOTS`,`lparser.c:1079`):

```c
case TK_DOTS: {
  varargk = 1;
  luaX_next(ls);  /* skip '...' */
  if (ls->t.token == TK_NAME)
    new_varkind(ls, str_checkname(ls), RDKVAVAR);
  else
    new_localvarliteral(ls, "(vararg table)");
  break;
}
```
(`lparser.c:1079-1087`)

这里是 5.5 的关键分叉:

- 如果 `...` 后面**没跟名字**(老语法 `function f(...)`),声明一个匿名局部变量 `(vararg table)`。
- 如果 `...` 后面**跟了名字**(5.5 新语法 `function f(...args)`),调 `new_varkind(ls, name, RDKVAVAR)`(`lparser.c:194`)声明一个 `kind = RDKVAVAR` 的变量。`RDKVAVAR = 2`(`lparser.h:104`),专门标记「这是命名可变参数」。

不管走哪个分支,最后都调 `setvararg`(`lparser.c:1059`):

```c
static void setvararg (FuncState *fs) {
  fs->f->flag |= PF_VAHID;  /* by default, use hidden vararg arguments */
  luaK_codeABC(fs, OP_VARARGPREP, 0, 0, 0);
}
```

注意第一行:**默认先置 `PF_VAHID`**。也就是说,所有可变参数函数一开始都被标记成「用隐藏参数机制」。函数体第一句字节码是 `OP_VARARGPREP`,运行期它会根据函数到底用了哪种机制做相应的栈布局调整。

那么 `PF_VATAB` 什么时候置?在编译器最后那趟 `luaK_finish`(`lcode.c:1929`)里,看一个连带关系。只要函数体里把命名可变参数当变量用,`luaK_dischargevars`(`lcode.c:819`)遇到 `VVARGVAR` 表达式会调 `luaK_vapar2local`(`lcode.c:808`):

```c
void luaK_vapar2local (FuncState *fs, expdesc *var) {
  needvatab(fs->f);  /* function will need a vararg table */
  ...
}
```

第一行就 `needvatab(fs->f)`,把 `PF_VATAB` 置上。也就是说:**只要函数用了命名可变参数(`...args`),就强制走 `PF_VATAB` 表机制**。而 `luaK_finish` 开头(`lcode.c:1932-1933`)还有一句:

```c
if (p->flag & PF_VATAB)  /* will it use a vararg table? */
  p->flag &= cast_byte(~PF_VAHID);  /* then it will not use hidden args. */
```

两者互斥:用了表就关掉隐藏参数。所以 5.5 的实况是:

- **老语法 `function f(...)` 且函数体只用 `...`(不命名)**:默认 `PF_VAHID`,走隐藏参数机制。多余实参留在栈上。
- **新语法 `function f(...args)` 或函数体把可变参数当表索引**:置 `PF_VATAB`,走表机制。多余实参被打包成一个 Table 放在最后一个参数槽。

再看执行期怎么分别处理。`OP_VARARGPREP`(`lvm.c:1955`)调 `luaT_adjustvarargs`(`ltm.c:272`):

```c
void luaT_adjustvarargs (lua_State *L, CallInfo *ci, const Proto *p) {
  int totalargs = cast_int(L->top.p - ci->func.p) - 1;
  int nfixparams = p->numparams;
  int nextra = totalargs - nfixparams;  /* number of extra arguments */
  if (p->flag & PF_VATAB) {  /* does it need a vararg table? */
    lua_assert(!(p->flag & PF_VAHID));
    createvarargtab(L, ci->func.p + nfixparams + 1, nextra);
    /* move table to proper place (last parameter) */
    setobjs2s(L, ci->func.p + nfixparams + 1, L->top.p - 1);
  }
  else {  /* no table */
    lua_assert(p->flag & PF_VAHID);
    buildhiddenargs(L, ci, p, totalargs, nfixparams, nextra);
    /* set vararg parameter to nil */
    setnilvalue(s2v(ci->func.p + nfixparams + 1));
    lua_assert(L->top.p <= ci->top.p && ci->top.p <= L->stack_last.p);
  }
}
```
(`ltm.c:272-289`)

`totalargs` 是实际传入的参数总数(含固定参数),`nextra = totalargs - nfixparams` 是多余参数个数。两条路:

**PF_VATAB(表机制)**:调 `createvarargtab`(`ltm.c:231`),在栈顶 `new` 一个 `Table`,把 `nextra` 个多余实参 `luaH_setint` 塞进表的数组部分(键 `1..nextra`),再 `luaH_set(t, "n", nextra)` 存个数。然后把这张表搬到 `ci->func.p + nfixparams + 1`——也就是固定参数之后的那个槽位(对应编译期声明的 `(vararg table)` 局部变量或命名可变参数 `args`)。后续函数体里 `args[1]`、`args.n` 都是普通的表查找。

**PF_VAHID(隐藏参数机制)**:调 `buildhiddenargs`(`ltm.c:255`):

```c
static void buildhiddenargs (lua_State *L, CallInfo *ci, const Proto *p,
                             int totalargs, int nfixparams, int nextra) {
  int i;
  ci->u.l.nextraargs = nextra;
  luaD_checkstack(L, p->maxstacksize + 1);
  /* copy function to the top of the stack, after extra arguments */
  setobjs2s(L, L->top.p++, ci->func.p);
  /* move fixed parameters to after the copied function */
  for (i = 1; i <= nfixparams; i++) {
    setobjs2s(L, L->top.p++, ci->func.p + i);
    setnilvalue(s2v(ci->func.p + i));  /* erase original parameter (for GC) */
  }
  ci->func.p += totalargs + 1;  /* 'func' now lives after hidden arguments */
  ci->top.p += totalargs + 1;
}
```

注释把栈布局画得很清楚:

```text
initial stack:  func arg1 ... argn extra1 ...
                ^ ci->func                    ^ L->top
final stack: func nil ... nil extra1 ... func arg1 ... argn
                                         ^ ci->func
```

初始时多余实参 `extra1 ...` 紧跟在固定参数后面,夹在 `func` 和 `L->top` 之间。`buildhiddenargs` 做的事:把当前函数自己拷一份到栈顶,再把固定参数也拷到栈顶,然后把原来的固定参数槽清 nil(给 GC),最后把 `ci->func.p` 往后挪 `totalargs + 1`,让 `func` 指到「多余实参之后」的位置。这样,多余实参 `extra1 ...` 就被「留在了 `ci->func.p` **之前**」,通过 `ci->u.l.nextraargs` 记下个数。函数体执行时,它的「固定参数 + 局部变量」都在新的 `ci->func.p` 之后,和普通函数一样;而可变参数实体在 `ci->func.p - nextraargs` 到 `ci->func.p - 1` 这一段「隐藏」起来的栈区里。

为什么费这么大劲把实参挪到 `func` 之前?因为函数体的寄存器是从 `func + 1` 开始编号的(P4-13 讲过),固定参数和局部变量都要落在 `func` 之后;如果多余实参留在 `func` 之后,它们会占用函数体要用到的寄存器槽,冲突。把它们藏到 `func` 之前,既不占寄存器号,又能在需要时通过 `ci->func.p - nextraargs` 反向定位。

取可变参数有三条指令,对应三种用法:

**OP_VARARG(`lvm.c:1936`)**——取出全部或前 N 个可变参数到一段连续寄存器(用于 `local a, b, c = ...` 或 `{...}`):

```c
vmcase(OP_VARARG) {
  StkId ra = RA(i);
  int n = GETARG_C(i) - 1;  /* required results (-1 means all) */
  int vatab = GETARG_k(i) ? GETARG_B(i) : -1;
  Protect(luaT_getvarargs(L, ci, ra, n, vatab));
  vmbreak;
}
```

`n` 是要取的个数(`-1` 表示全取)。`vatab` 由 k 位和 B 字段共同决定:`k=1` 时 `vatab = B`(表所在的寄存器偏移),`k=0` 时 `vatab = -1`(没有表,走隐藏参数)。`luaT_getvarargs`(`ltm.c:338`)据此分流:

```c
void luaT_getvarargs (lua_State *L, CallInfo *ci, StkId where, int wanted,
                                    int vatab) {
  Table *h = (vatab < 0) ? NULL : hvalue(s2v(ci->func.p + vatab + 1));
  int nargs = getnumargs(L, ci, h);  /* number of available vararg args. */
  ...
  if (h == NULL) {  /* no vararg table? */
    for (i = 0; i < touse; i++)  /* get vararg values from the stack */
      setobjs2s(L, where + i, ci->func.p - nargs + i);
  }
  else {  /* get vararg values from vararg table */
    for (i = 0; i < touse; i++) {
      lu_byte tag = luaH_getint(h, i + 1, s2v(where + i));
      ...
    }
  }
  for (; i < wanted; i++)   /* complete required results with nil */
    setnilvalue(s2v(where + i));
}
```

`h == NULL`(PF_VAHID)就从栈上 `ci->func.p - nargs + i` 拷;`h != NULL`(PF_VATAB)就从表里 `luaH_getint` 取。取不够的用 nil 补齐。注意编译器 `luaK_finish` 里(`lcode.c:1958-1961`)还做了一个改写:如果函数是 `PF_VATAB`,把 `OP_VARARG` 的 k 位置 1(让它走 `vatab = B` 表路径);这一步保证运行期 `OP_VARARG` 能正确分流。换句话说,**同一个 `OP_VARARG` 指令,运行期根据 k 位决定从栈还是从表取**,编译器在 `luaK_finish` 里统一调整。

**OP_GETVARG(`lvm.c:1943`)**——这是 **5.5 全新**的操作码,老版本(5.3/5.4)没有。它用于命名可变参数的单个元素访问 `args[i]` 或 `args.n`:

```c
vmcase(OP_GETVARG) {
  StkId ra = RA(i);
  TValue *rc = vRC(i);
  luaT_getvararg(ci, ra, rc);
  vmbreak;
}
```

`luaT_getvararg`(`ltm.c:292`)只取单个值:

```c
void luaT_getvararg (CallInfo *ci, StkId ra, TValue *rc) {
  int nextra = ci->u.l.nextraargs;
  lua_Integer n;
  if (tointegerns(rc, &n)) {  /* integral value? */
    if (l_castS2U(n) - 1 < cast_uint(nextra)) {
      StkId slot = ci->func.p - nextra + cast_int(n) - 1;
      setobjs2s(((lua_State*)NULL), ra, slot);
      return;
    }
  }
  else if (ttisstring(rc)) {  /* string value? */
    size_t len;
    const char *s = getlstr(tsvalue(rc), len);
    if (len == 1 && s[0] == 'n') {  /* key is "n"? */
      setivalue(s2v(ra), nextra);
      return;
    }
  }
  setnilvalue(s2v(ra));  /* else produce nil */
}
```

键是整数 `n`:从 `ci->func.p - nextra + n - 1` 取第 `n` 个多余实参。键是字符串 `"n"`:返回多余实参个数 `nextra`。否则 nil。注意这里直接读 `ci->u.l.nextraargs`——它假定走的是隐藏参数机制。那如果函数是 `PF_VATAB` 呢?编译器在 `luaK_finish`(`lcode.c:1953-1956`)里把 `OP_GETVARG` **改写成 `OP_GETTABLE`**:

```c
case OP_GETVARG: {
  if (p->flag & PF_VATAB)  /* function has a vararg table? */
    SET_OPCODE(*pc, OP_GETTABLE);  /* must get vararg there */
  break;
}
```

因为命名可变参数在 `PF_VATAB` 下就是一张普通的 Table,`args[i]` 等价于普通的 `t[i]`,直接用 `OP_GETTABLE` 即可,不需要专门的 `OP_GETVARG`。所以 `OP_GETVARG` 只在 `PF_VAHID`(隐藏参数)路径下真正执行,`PF_VATAB` 路径下它在编译末期就被改写成 `OP_GETTABLE` 了。这是 5.5 设计上的精简:**一套前端语法(`args[i]`),编译器后端自动选用最合适的指令**,命名可变参数在表机制下零成本复用普通表查找。

`OP_GETVARG` 的编译期发射在 `luaK_dischargevars`(`lcode.c:862`):

```c
case VVARGIND: {
  freeregs(fs, e->u.ind.t, e->u.ind.idx);
  e->u.info = luaK_codeABC(fs, OP_GETVARG, 0, e->u.ind.t, e->u.ind.idx);
  e->k = VRELOC;
  break;
}
```

`VVARGIND` 是表达式 `args[k]` 的中间表示(命名可变参数 `args` 用 `k` 索引),由 `luaK_indexed`(`lcode.c:1372`)对 `VVARGVAR` 类型的表生成:

```c
else if (t->k == VVARGVAR) {  /* indexing the vararg parameter? */
  int kreg = luaK_exp2anyreg(fs, k);  /* put key in some register */
  lu_byte vreg = cast_byte(t->u.var.ridx);  /* register with vararg param. */
  ...
  fillidxk(t, kreg, VVARGIND);  /* 't' represents 'vararg[k]' */
}
```

而 `VVARGVAR` 这个表达式类型,是变量查找 `singlevar`(经 `searchvar`,`lparser.c:437`)在发现名字对应一个 `RDKVAVAR` 变量时设的:

```c
if (vd->vd.kind == RDKVAVAR)  /* vararg parameter? */
  var->k = VVARGVAR;
```

整条链串起来:**源码 `args[2]` → `singlevar` 找到 `args` 是 `RDKVAVAR` → 表达式标成 `VVARGVAR` → `luaK_indexed` 把 `args[2]` 标成 `VVARGIND` → `luaK_dischargevars` 发 `OP_GETVARG` → `luaK_finish` 在 `PF_VATAB` 下改写成 `OP_GETTABLE`**。命名可变参数在 5.5 里就是这样从前端语法一路落到字节码。

`OP_VARARGPREP`(`lvm.c:1955`)是入口指令,调 `luaT_adjustvarargs` 做上面说的栈布局调整。它后面跟着 `updatebase(ci)`,因为调整后 `base` 可能变了(可变参数函数的 `base` 要重新算)。

最后回看尾调用里那个 `delta`。`OP_TAILCALL`(`lvm.c:1743`):

```c
int nparams1 = GETARG_C(i);
/* delta is virtual 'func' - real 'func' (vararg functions) */
int delta = (nparams1) ? ci->u.l.nextraargs + nparams1 : 0;
```

如果当前函数是可变参数函数(`C` 字段非 0,`luaK_finish` 里设成 `numparams + 1`),`delta = nextraargs + nparams1`。这是因为可变参数函数用了隐藏参数机制后,真实的 `func`(`ci->func.p` 经过 `buildhiddenargs` 的 `+= totalargs + 1` 后的位置)和栈上「看起来」的函数位置差了一段。尾调用要把被调函数搬到当前帧的 `func` 位置,必须先把这个差值修正回来(`ci->func.p -= delta` 在 `luaD_pretailcall:684`)。这是可变参数和尾调用两个机制的接合点——可变参数改变了 `func` 指针的语义,尾调用必须感知这个改变才能正确搬运。

### 2.3 to-be-closed 变量:声明、关闭触发、错误隔离

to-be-closed 的生命周期分三步:声明时标记、出作用域时关闭、关闭出错时隔离。

**声明**。源码 `local x <close> = expr`。语法分析在 `localstat` 里识别 `<close>` 属性,把变量的 `kind` 设成 `RDKCTC` 之外的特殊值(实际 to-be-closed 用 `VDKREG` 但额外标记块)。真正在字节码层面把一个已存在的局部变量标记成 to-be-closed,是 `OP_TBC` 指令(`lvm.c:1640`):

```c
vmcase(OP_TBC) {
  StkId ra = RA(i);
  /* create new to-be-closed upvalue */
  halfProtect(luaF_newtbcupval(L, ra));
  vmbreak;
}
```

`luaF_newtbcupval`(`lfunc.c:172`)把变量挂进 `L->tbclist`(to-be-closed 链表):

```c
void luaF_newtbcupval (lua_State *L, StkId level) {
  lua_assert(level > L->tbclist.p);
  if (l_isfalse(s2v(level)))
    return;  /* false doesn't need to be closed */
  checkclosemth(L, level);  /* value must have a close method */
  while (cast_uint(level - L->tbclist.p) > MAXDELTA) {
    L->tbclist.p += MAXDELTA;  /* create a dummy node at maximum delta */
    L->tbclist.p->tbclist.delta = 0;
  }
  level->tbclist.delta = cast(unsigned short, level - L->tbclist.p);
  L->tbclist.p = level;
}
```

几个细节:

- `l_isfalse(s2v(level))` 为真(值是 `false` 或 `nil`)就直接返回,不挂链。所以 `local x <close> = false` 是合法的,只是不关。这给了「条件性关闭」一个写法。
- `checkclosemth`(`lfunc.c:127`)检查值有没有 `__close` 元方法,没有就报错 `variable '%s' got a non-closable value`。这就是为什么 `local x <close> = 42` 会运行报错——数字没有 `__close`。
- `tbclist` 是一个用「栈槽复用」实现的链表:每个 to-be-closed 变量所在的 `StkId` 里有一个 `tbclist.delta` 字段(和 upvalue 的 open 链表共用栈槽的复用技巧),存的是「到上一个 to-be-closed 变量的距离」。`L->tbclist.p` 指向链表头(最新的一个)。距离超过 `MAXDELTA`(`USHRT_MAX`,`lfunc.c:166`)就插「哑节点」(`delta = 0`)分段,保证 `delta` 能装进 `unsigned short`。

to-be-closed 变量本质上是一个**开放 upvalue**(它的值在栈上,但被一个 upvalue 结构包装,目的是让 `__close` 调用能找到它)。所以 `luaF_newtbcupval` 名字里有「upval」。

**关闭触发**。三个地方会触发关闭:

第一,`OP_CLOSE` 指令(`lvm.c:1634`),由编译器在块结束处(`leaveblock` 系列)发射,显式关闭到某个栈层级的所有 to-be-closed 变量和 upvalue:

```c
vmcase(OP_CLOSE) {
  StkId ra = RA(i);
  lua_assert(!GETARG_B(i));  /* 'close must be alive */
  Protect(luaF_close(L, ra, LUA_OK, 1));
  vmbreak;
}
```

第二,`OP_RETURN`(`lvm.c:1763`)在返回时,如果 k 位置位(表示当前函数有 `needclose`,即有 to-be-closed 变量或要关的 upvalue),先关再返回:

```c
vmcase(OP_RETURN) {
  StkId ra = RA(i);
  int n = GETARG_B(i) - 1;  /* number of results */
  int nparams1 = GETARG_C(i);
  if (n < 0)  /* not fixed? */
    n = cast_int(L->top.p - ra);  /* get what is available */
  savepc(ci);
  if (TESTARG_k(i)) {  /* may there be open upvalues? */
    ci->u2.nres = n;  /* save number of returns */
    if (L->top.p < ci->top.p)
      L->top.p = ci->top.p;
    luaF_close(L, base, CLOSEKTOP, 1);
    updatetrap(ci);
    updatestack(ci);
  }
  ...
  luaD_poscall(L, ci, n);
  ...
  goto ret;
}
```

`luaF_close(L, base, CLOSEKTOP, 1)` 把当前帧 `base` 以下的所有 to-be-closed 变量和 upvalue 全关掉,然后才 `luaD_poscall` 返回。注意 `OP_RETURN0`/`OP_RETURN1` 在 `luaK_finish`(`lcode.c:1940-1945`)里,如果 `fs->needclose || PF_VAHID`,会被改写成 `OP_RETURN`——因为 `OP_RETURN0`/`OP_RETURN1` 是没有关闭逻辑的快速路径,有 to-be-closed 变量时必须走完整的 `OP_RETURN`。

第三,被调函数返回到调用者时,如果调用者声明了「这个调用位置有 to-be-closed 变量」(`CIST_TBC` 标志,由 `lua_settop` 之类 C API 设置),`luaD_poscall` 里也会触发关闭。看 `moveresults`(`ldo.c:561`):

```c
l_sinline void moveresults (lua_State *L, StkId res, int nres,
                                          l_uint32 fwanted) {
  switch (fwanted) {
    ...
    default: {  /* two/more results and/or to-be-closed variables */
      int wanted = get_nresults(fwanted);
      if (fwanted & CIST_TBC) {  /* to-be-closed variables? */
        L->ci->u2.nres = nres;
        L->ci->callstatus |= CIST_CLSRET;  /* in case of yields */
        res = luaF_close(L, res, CLOSEKTOP, 1);
        L->ci->callstatus &= ~CIST_CLSRET;
        if (L->hookmask) {  /* if needed, call hook after '__close's */
          ...
          rethook(L, L->ci, nres);
          ...
        }
        ...
      }
      genmoveresults(L, res, nres, wanted);
      break;
    }
  }
}
```

`CIST_TBC`(`lstate.h:239`)是调用信息上的标志,表示「这次调用的结果位置上有 to-be-closed 变量需要关」。`fwanted = ci->callstatus & (CIST_TBC | CIST_NRESULTS)`(`ldo.c:606`)把这两个标志一起取出来。如果有 `CIST_TBC`,在搬结果之前先 `luaF_close`。`CIST_CLSRET`(`lstate.h:237`)是一个瞬态标志,标记「正在关闭 to-be-closed 变量期间」,用于协程 yield 恢复时能正确续上(下面讲)。

**核心函数 luaF_close**(`lfunc.c:230`):

```c
StkId luaF_close (lua_State *L, StkId level, TStatus status, int yy) {
  ptrdiff_t levelrel = savestack(L, level);
  luaF_closeupval(L, level);  /* first, close the upvalues */
  while (L->tbclist.p >= level) {  /* traverse tbc's down to that level */
    StkId tbc = L->tbclist.p;  /* get variable index */
    poptbclist(L);  /* remove it from list */
    prepcallclosemth(L, tbc, status, yy);  /* close variable */
    level = restorestack(L, levelrel);
  }
  return level;
}
```

两步:先 `luaF_closeupval`(`lfunc.c:197`)关掉所有 `level` 之下的开放 upvalue(把指向栈的 upvalue 改成指向自己的值槽,P4-14 讲过);再遍历 `tbclist`,把所有 `>= level` 的 to-be-closed 变量逐个 `prepcallclosemth` 关掉。`poptbclist`(`lfunc.c:216`)从链表头摘节点。注意每关一个就 `level = restorestack(L, levelrel)` 重新算 `level`——因为 `__close` 元方法是个 Lua/C 调用,可能扩值栈(`realloc` 搬家),`level` 这个指针可能失效,必须用 `savestack`/`restorestack`(存的是相对偏移)重新恢复。这是 5.5 用 `StkIdRel` 相对表示栈指针的收益之一(P0-01 提过):关闭期间栈可能搬家,相对偏移能扛得住。

**关闭方法调用**。`prepcallclosemth`(`lfunc.c:145`)准备参数,`callclosemethod`(`lfunc.c:107`)真正调:

```c
static void callclosemethod (lua_State *L, TValue *obj, TValue *err, int yy) {
  StkId top = L->top.p;
  StkId func = top;
  const TValue *tm = luaT_gettmbyobj(L, obj, TM_CLOSE);
  setobj2s(L, top++, tm);  /* will call metamethod... */
  setobj2s(L, top++, obj);  /* with 'self' as the 1st argument */
  if (err != NULL)  /* if there was an error... */
    setobj2s(L, top++, err);  /* then error object will be 2nd argument */
  L->top.p = top;  /* add function and arguments */
  if (yy)
    luaD_call(L, func, 0);
  else
    luaD_callnoyield(L, func, 0);
}
```

`__close` 元方法的调用约定:`__close(self, err)`。`self` 是被关的变量值。`err` 在正常关闭(`status == LUA_OK` 或 `CLOSEKTOP`)时是 `NULL`(不传第二个参数),在出错关闭(`status` 是 `LUA_ERRRUN` 等)时是错误对象。`yy` 参数决定这次调用能不能 yield:`yy=1` 用 `luaD_call`(可 yield,协程友好),`yy=0` 用 `luaD_callnoyield`(禁止 yield,用于某些不能被打断的关闭路径)。

`prepcallclosemth` 里 `status` 决定 `errobj`(`lfunc.c:149-160`):

```c
switch (status) {
  case LUA_OK:
    L->top.p = level + 1;  /* call will be at this level */
    /* FALLTHROUGH */
  case CLOSEKTOP:  /* don't need to change top */
    errobj = NULL;  /* no error object */
    break;
  default:  /* 'luaD_seterrorobj' will set top to level + 2 */
    errobj = s2v(level + 1);  /* error object goes after 'uv' */
    luaD_seterrorobj(L, status, level + 1);  /* set error object */
    break;
}
```

正常退出(`LUA_OK`/`CLOSEKTOP`)没有错误对象;异常退出把错误对象放在变量值后面那个槽,传给 `__close`。这让 to-be-closed 变量能在出错时做不同的清理(比如只在出错时回滚事务)。

**错误隔离**。`__close` 自己又出错怎么办?这是 to-be-closed 设计上必须解决的问题——多个 to-be-closed 变量按声明逆序关闭,如果前一个 `__close` 出错,后面的还得继续关,否则资源泄漏。`luaF_close` 里 `prepcallclosemth` → `callclosemethod` → `luaD_call` 这条链,`luaD_call` 走的是受保护调用路径(`ldo.c` 的 `luaD_rawrunprotected` 机制,P4-13 讲过错误恢复)。出错时,错误被捕获、错误对象记下,然后**继续循环关下一个**。多个错误会被串成一个错误链(`luaD_seterrorobj` 在 `LUA_ERRERR` 状态下会把原错误和新错误拼起来)。这正是 `luaF_close` 的 `while` 循环不因单个 `prepcallclosemth` 失败而 break 的含义:每个 to-be-closed 变量的关闭是独立的,一个失败不挡下一个。

`CIST_CLSRET` 标志(`lstate.h:237`)和 `ldo.c:839` 的 `finishpcallk` 配合,处理「关闭期间协程 yield 后恢复」的情况——`__close` 可能 yield(它是个 Lua 函数调用),yield 期间调用栈状态被冻结,恢复时要能续上关闭循环。`ldo.c:839`:

```c
if (ci->callstatus & CIST_CLSRET) {  /* was closing TBC variable? */
  lua_assert(ci->callstatus & CIST_TBC);
  ...
  /* don't need to reset CIST_CLSRET, as it will be set again anyway */
}
```

恢复时发现上次是在关闭 to-be-closed 变量期间 yield 的,就重新走关闭路径。这是 to-be-closed 和协程(P6-20)的接合点。

**generic for 的 to-be-closed**。`for ... in` 循环里,5.4+ 把迭代器的「关闭变量」(第三个返回值,如果有 `__close`)自动标记为 to-be-closed,循环正常结束时调它的 `__close`。这在 `OP_TFORPREP`(`lvm.c:1856`)里:

```c
vmcase(OP_TFORPREP) {
  ...
  StkId ra = RA(i);
  TValue temp;  /* to swap control and closing variables */
  setobj(L, &temp, s2v(ra + 3));
  setobjs2s(L, ra + 3, ra + 2);
  setobj2s(L, ra + 2, &temp);
  /* create to-be-closed upvalue (if closing var. is not nil) */
  halfProtect(luaF_newtbcupval(L, ra + 2));
  pc += GETARG_Bx(i);  /* go to end of the loop */
  ...
}
```

`ra+2` 是关闭变量,`luaF_newtbcupval` 把它挂进 `tbclist`。循环走完(`OP_TFORLOOP` 发现迭代器返回 nil)离开块时,块结束的 `OP_CLOSE` 会关掉它。这就是 `for line in io.lines() do ... end` 能保证文件句柄被关的原因。

### 2.4 三者的咬合点:return 上的收尾

把三个机制放回 `return` 这个点上,看它们怎么咬合。

一个有 to-be-closed 变量的函数 `function f() local x <close> = ...; return g() end`,编译期发生了什么:

1. `local x <close>` 触发 `marktobeclosed`(`lparser.c:463`),当前块 `bl->insidetbc = 1`、`fs->needclose = 1`。
2. `return g()` 进入 `retstat`(`lparser.c:2017`)。`explist` 解析 `g()`,得到 `e.k == VCALL`、`nret == 1`。
3. 尾调用判断(`lparser.c:2029`):`e.k == VCALL && nret == 1 && !fs->bl->insidetbc`。**第三个条件 `!fs->bl->insidetbc` 为假**(因为外层有 `x <close>`),所以**不编成 `TAILCALL`**,老老实实编成 `CALL` + 后续 `RETURN`。
4. `luaK_ret` 发 `OP_RETURN`。`luaK_finish` 里因为 `fs->needclose`,把 `OP_RETURN` 的 k 位置 1(`lcode.c:1947`)。

执行期:

1. `OP_CALL` 调 `g`,压新帧,执行,`g` 返回,`luaD_poscall` 把结果搬回 `f` 的栈。
2. `OP_RETURN`(k=1):`TESTARG_k(i)` 真,先 `luaF_close(L, base, CLOSEKTOP, 1)` 关掉 `x` 的 `__close`。然后 `luaD_poscall` 返回上层。

对比没有 to-be-closed 变量的 `function f() return g() end`:编译期第三个条件成立,编成 `TAILCALL`;执行期 `OP_TAILCALL` 直接复用帧,`g` 的返回值直达 `f` 的调用者,`f` 这一层帧被覆盖。**to-be-closed 变量的存在,迫使 `return f()` 退化成普通调用**——因为 `__close` 必须在帧被覆盖前执行,而尾调用的整个意义就是「立刻覆盖帧」,两者在时序上冲突。编译器在编译期就识别出这个冲突,主动放弃尾调用,这是「尾调用成立条件」在源码层面的精确落点。

upvalue 关闭则是另一条线:`OP_TAILCALL` 里 `TESTARG_k(i)` 真(当前函数有要关的 upvalue,即内层闭包捕获了当前函数的局部变量),先 `luaF_closeupval(L, base)` 关 upvalue,再搬运。upvalue 关闭和 to-be-closed 关闭的区别:upvalue 关闭只是把「指向栈」改成「指向 upvalue 自己的值槽」(`luaF_closeupval` 那几行,`lfunc.c:197`),不调任何元方法,不阻塞;而 to-be-closed 关闭要调 `__close`,是个完整的函数调用,可能 yield、可能出错。所以**有 upvalue 要关时,仍能尾调用**(只要没有 to-be-closed 变量),`OP_TAILCALL` 在搬运前先把 upvalue 关掉就行;**有 to-be-closed 变量时,不能尾调用**,编译期就挡掉了。

---

## 三、为什么这样设计是 sound 的

### 3.1 尾调用复用帧不增栈:sound 的两个不变式

proper tail call 的 sound 体现在两点:

**第一,被调函数和当前函数的调用者直接对接,语义不丢**。`return f()` 的语义是「当前函数返回 `f()` 的全部返回值」。普通调用实现这一点要两步:`f` 返回值搬到当前帧,当前帧再搬到调用者帧。尾调用砍掉中间一步:`f` 的返回值直接搬到调用者帧。为什么能砍?因为 `luaD_pretailcall` 把 `f` 的 `savedpc`、`func`、`top` 全装进了**当前 `ci`**(没建新 `ci`),`f` 执行完走 `luaD_poscall`,而 `poscall` 里 `L->ci = ci->previous`——`ci` 没换,`previous` 还是原来那个,指向上上层。结果值落到 `ci->func.p` 也就是上上层调用者期望的位置。语义完全等价,但少了一层 `CallInfo`、少占一段值栈。

**第二,当前函数在 `return f()` 之后确实没有任何动作**。这是尾调用能 sound 的前提。如果有动作(比如要关 to-be-closed 变量),复用帧就会丢掉那个动作。Lua 用两个机制保证这个前提:

- 编译期 `!fs->bl->insidetbc` 挡掉有 to-be-closed 变量的情况。
- 运行期 `lua_assert(L->tbclist.p < base)` 在 `OP_TAILCALL` 处断言当前帧没有待关变量。

upvalue 关闭不破坏这个前提,因为它在搬运**之前**就完成了(`luaF_closeupval` 在 `luaD_pretailcall` 之前调),关完之后当前帧的 upvalue 状态已经清理干净,被调函数覆盖帧不会丢任何 upvalue 信息。

C 函数不能享受尾调用,是因为 C 函数的栈由 C 调用约定管,VM 无权把 C 函数的栈帧和 Lua 帧互换。这不是设计的妥协,是物理约束——VM 控制不了 C 编译器生成的栈布局。所以 `luaD_pretailcall` 对 C 函数走 `precallC` 建新帧,是唯一 sound 的选择。

### 3.2 可变参数两套机制:sound 的取舍

5.5 为什么搞两套可变参数机制(`PF_VAHID` 和 `PF_VATAB`)?这是一个典型的「按使用模式选实现」的精简设计。

`PF_VAHID`(隐藏参数)的优势:多余实参直接留在栈上,`OP_VARARG`/`OP_GETVARG` 取值是纯栈拷贝,没有表查找开销,也没有创建 Table 的分配开销。劣势:`args.n` 这种「取个数」要在 `luaT_getvararg` 里特殊处理(读 `ci->u.l.nextraargs`),`args` 不能作为一等值传来传去(它不是个 Table,只是栈上一段)。

`PF_VATAB`(可变参数表)的优势:`args` 是个真正的 Table,能当一等值传递(`return args`、`somefunc(args)`),`args[i]`、`args.n`、`#args`、`pairs(args)` 全是普通表操作,无需特例。劣势:进入函数时要 `createvarargtab` 分配一张表、填数据,有分配和 GC 压力。

Lua 的选择:**默认走 `PF_VAHID`(快路径,适合大多数 `function(...)` 只是把 `...` 转发或解包的场景);一旦函数体把 `...` 当命名变量用(`...args` 或 `args[i]` 这种,需要把它当一等值),编译期 `luaK_vapar2local` 检测到,`needvatab` 置 `PF_VATAB`,切到表机制**。用户写代码时完全不感知这套切换,但得到的是「按需付出表分配代价」——只用 `...` 的函数不付表代价,需要命名可变参数的函数才付。这是「精简」主线在可变参数上的具体体现:一套前端语法,后端按实际使用选最省的实现。

`OP_GETVARG` 在 `PF_VATAB` 下被改写成 `OP_GETTABLE`(`lcode.c:1953-1956`),是这套设计的另一面:**没有为表机制单独发明一个操作码**,直接复用已有的 `OP_GETTABLE`。`OP_GETVARG` 只服务于隐藏参数路径(从栈上取单个值),表路径用通用表查找。这避免了操作码膨胀,也符合 Lua「精简」的一贯风格。

`buildhiddenargs` 把实参挪到 `func` 之前,是为了不和函数体的寄存器区冲突(P4-13 讲过函数寄存器从 `func+1` 开始)。这个布局让可变参数和固定参数/局部变量在栈上物理分离,sound 在于:函数体永远通过正偏移访问自己的寄存器,可变参数永远通过负偏移(`ci->func.p - nextra`)访问,两者不会撞。尾调用搬运时 `delta = nextraargs + nparams1` 把这个偏移修正回来,保证被调函数的 `func` 指到正确位置。

### 3.3 to-be-closed 的错误隔离:sound 的三层保证

to-be-closed 要 sound,必须保证:无论函数怎么退出(正常 return、出错、协程 yield-不恢复),所有 to-be-closed 变量的 `__close` 都被调用,且一个失败不挡其他。

**第一层:关闭触发点全覆盖**。`OP_CLOSE`(块结束)、`OP_RETURN`(return 时 k 位置位)、`luaD_poscall` 里的 `CIST_TBC` 分支(被调返回到有 to-be-closed 的调用位置)、`luaD_seterrorobj` 路径(出错 unwinding 时 `luaF_close` 带 `status` 参数)。无论从哪条路径退出作用域,都会经过 `luaF_close`。

**第二层:`luaF_close` 的循环不因单次失败 break**。`while (L->tbclist.p >= level)` 逐个关,每个 `prepcallclosemth` 是受保护调用。某个 `__close` 出错,错误被捕获记下,循环继续关下一个。多个错误通过 `luaD_seterrorobj` 串成错误链。

**第三层:`__close` 的 yield 友好**。`callclosemethod` 的 `yy` 参数控制能否 yield。`luaF_close` 从 `OP_RETURN`/`OP_CLOSE` 调时 `yy=1`(可 yield),从某些不能被打断的路径调时 `yy=0`。`CIST_CLSRET` 标志记录「正在关闭」,协程 yield 后恢复能续上关闭循环(`ldo.c:839`)。这让 to-be-closed 在协程里也 sound——`__close` 里 yield 不会导致后续变量漏关。

`luaF_newtbcupval` 对 `false`/`nil` 值直接跳过(`l_isfalse` 检查),是 sound 的一个细节:它允许 `local x <close> = maybe_nil()` 这种写法,值为假就不关,避免对假值调用 `__close`(没有意义)。`checkclosemth` 在声明时就检查有 `__close`,没有直接报错,而不是等到关闭时才发现——早失败比晚失败好。

`tbclist` 用栈槽复用(`StkId` 里的 `tbclist.delta` 字段)存链表,而不是单独分配链表节点,是「精简」的体现:不为 to-be-closed 单独分配内存,链表信息直接寄生在值栈槽里。哑节点机制(`delta = 0` 分段)保证 `delta` 装进 `unsigned short` 又不限制 to-be-closed 变量数量。

---

## 四、★对照 CPython + 回扣主线

这三个机制在 CPython 里有对照,但实现路径几乎完全不同。

**尾调用**。CPython **没有** proper tail call。Python 的 `return f()` 会老老实实压栈,递归深度受 `sys.setrecursionlimit()`(默认 1000)限制,超过抛 `RecursionError`。这不是 CPython 实现者的疏忽,是有意为之:Python 的函数调用涉及大量隐式状态(默认参数、`*args`/`**kwargs` 解包、装饰器、traceback 帧链),如果做尾调用复用帧,这些状态在调试时会丢失(traceback 看不到完整的调用链)。Guido van Rossum 明确拒绝过给 Python 加尾调用优化的提议,理由就是它会破坏调试体验。Lua 的取舍相反:Lua 把「能无限递归」视为函数式编程的基本能力,愿意为此放弃 traceback 里这一层帧(尾调用后调用栈确实少一层)。两种取舍都有道理,取决于语言定位——Lua 偏函数式/嵌入式状态机,Python 偏脚本/调试友好。

**可变参数**。Python 的 `*args`/`**kwargs`:多余位置参数打包成 `tuple`(不可变序列)放进 `args`,多余关键字参数打包成 `dict` 放进 `kwargs`。这套机制是**强制**的——只要函数声明了 `*args`,调用时就一定创建 tuple,没有「按需」的快路径。Lua 5.5 的 `PF_VAHID`/`PF_VATAB` 双机制,正是为了避免「每次都付打包代价」:只用 `...` 转发的函数(很常见,比如 wrapper)走 `PF_VAHID` 不建表;需要命名可变参数的才走 `PF_VATAB` 建表。Python 还区分 `*args`(位置可变)和 `**kwargs`(关键字可变)两种,Lua 只有位置可变参数一种(没有关键字可变参数,因为 Lua 没有关键字参数概念,函数调用全靠位置)。这又是一个「统一 vs 专用」的对照:Lua 用一套 `...` 概念覆盖可变参数场景,Python 用 `*args`/`**kwargs` 两套。

5.5 的命名可变参数 `function f(...args)` 在语义上接近 Python 的 `*args`(`args` 是个能索引、能取长度的容器),但实现上仍保留了 `PF_VAHID` 的快路径——只有真的把 `args` 当变量用了才建表,只 `print(...)` 转发的不建表。Python 没有这种按需优化。

**to-be-closed**。Python 的对应是 `contextlib.contextmanager` 和 `with` 语句:

```python
with open(path) as f:
    ...
```

`with` 块结束时调 `f.__exit__`。和 Lua 的 `<close>` 比:

- 作用域:Python 的 `with` 是一个**显式语句块**,资源只在块内有效;Lua 的 `<close>` 是**变量属性**,资源和变量绑定,变量出作用域(块结束、函数返回、出错)就关。Lua 的更灵活——一个 to-be-closed 变量可以跨多个块(只要它还活着)。
- 异常处理:Python 的 `__exit__` 接收异常信息(exc_type, exc_value, traceback),能吞掉异常(返回真值);Lua 的 `__close(self, err)` 接收错误对象,但 `__close` 自己出错会被隔离(不吞调用者的错误)。
- 多资源:Python 用 `ExitStack` 管理多个,按 LIFO 关;Lua 的 `tbclist` 天生按声明逆序关(`luaF_close` 的 while 循环从链表头,即最新声明的,往回关)。
- 错误隔离:Python 的 `with` 如果 `__exit__` 自己抛异常,会替换原异常(除非用 `ExitStack`);Lua 的 `luaF_close` 把多个 `__close` 错误串成链,不互相挡。

两者目标一样(RAII 资源释放),实现路径不同:Python 靠显式 `with` 块 + 协议方法,Lua 靠变量属性 + 元方法 + 受保护关闭链。Lua 的更轻(不引入新语句,只是变量属性),但作用域语义更隐式(变量出作用域才关,不像 `with` 块边界明显)。

**三者合起来,回扣主线**。尾调用、可变参数、to-be-closed 都是「作用域边界 + 控制流」的处理,且都体现了「精简换小而快」:

- 尾调用把 `return f()` 做成不增栈,让 Lua 能用递归写状态机而不爆栈——这是「精简」在控制流上的极致体现,一个判断(`!insidetbc`)+ 一个搬运(`luaD_pretailcall`)就省掉一整层调用帧。
- 可变参数两套机制按需切换,让常见场景(`...` 转发)不付表分配代价——这是「精简」在性能上的体现,一套语法两种实现,编译器自动选。
- to-be-closed 用变量属性(不引入新语句)+ 栈槽复用链表(不分配节点)+ 受保护关闭链(错误隔离),实现了完整的 RAII——这是「精简」在资源管理上的体现,用最少的新机制拿下「保证资源释放」这个重需求。

把 P4 的三章合起来看:P4-13 调用栈与调用约定(怎么压帧)、P4-14 闭包与 upvalue(怎么跨作用域捕获)、本章(怎么在边界上精简——尾调用省帧、可变参数按需、to-be-closed 受保护关闭),构成了「函数怎么被调起来」的完整图景。尾调用尤其重要——它是 Lua 函数式编程能力的物理基础,没有它,Lua 写递归下降解析器、状态机、协程风格代码都会受栈深度限制。

到本章为止,执行侧的「调用/控制流」这条线讲完了。函数被调起来、参数被传进来、闭包捕获了词法作用域、返回时该关的关该传的传——这些都是**值在栈上的生命周期**。但值还有另一层生命周期:它在堆上的对象(Table、字符串、闭包、upvalue)什么时候被回收。这是 GC 的事。函数执行过程中不断分配对象(可变参数表、闭包、临时 Table),这些对象不再被引用时必须被回收,否则内存只涨不落。Lua 的 GC 是增量式三色标记,核心约束是「必须可中断」——它不能一口气扫完整个堆(会卡住宿主),而是切成无数小步,每执行一段字节码顺手做一小步。这个「可中断」的设计,和本章尾调用的「可复用帧」、to-be-closed 的「错误隔离」一样,都是「精简换小而快」主线在执行侧不同维度的落地。

---

*下一章 [P5-16 三色标记与增量步进:GC 必须可中断](P5-16-三色标记与增量步进-GC必须可中断.md):从调用/控制流跨入值的生命周期,看 Lua 的增量式三色 GC 怎么在「必须回收干净」和「不能卡住宿主」之间走钢丝。*
