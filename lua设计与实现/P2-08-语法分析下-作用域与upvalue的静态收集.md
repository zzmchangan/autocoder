# P2-08 语法分析(下):作用域与 upvalue 的静态收集

> **本书主线**:统一与精简换小而快。**二分法**:编译侧(源码→字节码)↔ 执行侧(VM 执行 + 管值生命周期)。**★对照**:CPython。**源码**:lua-5.5.0。**基调**:纯直球,不用比喻。
>
> 本章落点:**把作用域分析与 upvalue 登记前移到编译期**——这是 Lua"精简"在设计上的又一个大动作,运行时不再做任何动态名字查找,闭包引用外层变量只连一次。承接 [P2-07 语法分析上](P2-07-语法分析上-递归下降与算符优先.md),呼应 [P4-14 闭包与 upvalue 的开合](P4-14-闭包与upvalue-词法捕获的开合.md)。

---

## 一、这章解决什么问题

上一章讲了递归下降怎么把表达式喂给算符优先,但刻意绕开了一类最关键的变量:一个内层函数里出现的名字,它到底指谁?

Lua 的作用域是**词法作用域**(lexical scope,也叫静态作用域)。这意味着一个名字在源码里的"指向什么",完全由它在源码文本里的嵌套位置决定,与运行时调用顺序无关。看一段典型代码:

```lua
local function counter()
  local n = 0           -- 外层局部变量
  return function ()    -- 内层闭包
    n = n + 1
    return n
  end
end
```

内层那个匿名函数里出现的 `n`,指向的是外层 `counter` 里的 `n`。注意:内层函数被调用的时候,`counter` 可能早就返回了,`n` 所在的那段值栈可能已经被别的调用复用。可内层函数仍然要正确地读写那个 `n`。

这是闭包要解决的核心问题。很多动态语言的解法是在**运行时**动态查:沿调用链一层层去找这个名字在哪,要么每次访问都遍历,要么用一个间接的"cell"对象包一层再运行时跳。这些都能工作,但每次访问都要付出查找或间接寻址的代价。

Lua 选了另一条路。它的关键洞察是:**既然作用域是词法的,那"一个名字指向哪个外层变量"在编译期就完全确定**——函数怎么嵌套、每一层有哪些局部变量,都是源码写死的静态事实。Lua 把这个分析在编译期一次性做完:

- 每个局部变量在编译期就被分配一个固定的**寄存器槽位**(就是它运行时所在的值栈位置)。
- 内层函数引用一个外层局部变量时,编译期就在内层函数的 **upvalue 表**里登记一条记录:`这个 upvalue 来自外层第几个寄存器,还是来自外层的第几个 upvalue`。
- 同一个外层变量在内层被引用多次,只登记一次。
- 全局变量也统一进 upvalue 机制:它们通过一个特殊的名为 `_ENV` 的 upvalue 访问。

编译完之后,字节码里读写外层变量就是两条直接索引指令:`GETUPVAL A B`(`R[A] := UpValue[B]`)和 `GETTABUP A B C`(`R[A] := UpValue[B][K[C]]`,用于全局)。运行时 VM 拿到指令,按编译期定好的索引直接取,没有任何名字查找、没有沿调用链回溯。这一章要讲清楚的就是:**Lua 是怎么在编译期把这些静态分析出来的。**

为什么这是"精简"?因为它把一个本来可以扔给运行时反复做的事情(查作用域),前移到了只做一次的编译期。运行时机制因此可以做到极简:一个 upvalue 表,一条索引指令,完事。这是本章要回答的核心问题。

---

## 二、源码怎么实现

源码全部在 `lparser.c`(编译器)和 `lobject.h`/`lparser.h`(数据结构)。下面按"局部变量分配 → 作用域 block → 变量查找 → upvalue 登记 → _ENV → 嵌套函数与 CLOSURE → goto/label"的顺序,把机制一行行走通。

### 2.1 局部变量 = 编译期分配的寄存器槽位

Lua 的局部变量在运行时就是函数值栈的一个槽位(P4-13 详讲值栈)。编译期负责决定:**这个局部变量占第几个槽位**。这件事由一个全局的 `actvar` 数组和几条小函数协作完成。

先看 `actvar` 是什么。它是 `Dyndata` 里的一个数组(`lparser.h:150`):

```c
typedef struct Dyndata {
  struct {  /* list of all active local variables */
    Vardesc *arr;
    int n;
    int size;
  } actvar;
  Labellist gt;  /* list of pending gotos */
  Labellist label;   /* list of active labels */
} Dyndata;
```

`actvar.arr` 是一个**所有正在编译的函数共用的扁平数组**,每个槽位是一个 `Vardesc`(`lparser.h:118`):

```c
typedef union Vardesc {
  struct {
    TValuefields;  /* constant value (if it is a compile-time constant) */
    lu_byte kind;
    lu_byte ridx;  /* register holding the variable */
    short pidx;  /* index of the variable in the Proto's 'locvars' array */
    TString *name;  /* variable name */
  } vd;
  TValue k;  /* constant value (if any) */
} Vardesc;
```

重点字段:`vd.kind`(变量种类)、`vd.ridx`(寄存器号)、`vd.name`(名字)。一个函数的局部变量,就是这个数组里从 `fs->firstlocal` 开始的一段。注意是**相对索引**——每个 `FuncState` 记一个 `firstlocal`(`lparser.h:178`)表示"我的局部变量从 `actvar` 数组的第几个开始",这样多个嵌套函数可以共用同一个扁平数组而互不踩踏。

声明一个新局部变量的入口是 `new_localvar`(`lparser.c:211`),它调 `new_varkind`(`lparser.c:194`):

```c
static int new_varkind (LexState *ls, TString *name, lu_byte kind) {
  lua_State *L = ls->L;
  FuncState *fs = ls->fs;
  Dyndata *dyd = ls->dyd;
  Vardesc *var;
  luaM_growvector(L, dyd->actvar.arr, dyd->actvar.n + 1,
             dyd->actvar.size, Vardesc, SHRT_MAX, "variable declarations");
  var = &dyd->actvar.arr[dyd->actvar.n++];
  var->vd.kind = kind;  /* default */
  var->vd.name = name;
  return dyd->actvar.n - 1 - fs->firstlocal;
}
```

到这里只做了两件事:在 `actvar` 尾部追加一项,记下名字和种类。注意它**还没有分配寄存器**——`ridx` 还没填。寄存器分配发生在稍后。

真正分配寄存器的是 `adjustlocalvars`(`lparser.c:328`):

```c
static void adjustlocalvars (LexState *ls, int nvars) {
  FuncState *fs = ls->fs;
  int reglevel = luaY_nvarstack(fs);
  int i;
  for (i = 0; i < nvars; i++) {
    int vidx = fs->nactvar++;
    Vardesc *var = getlocalvardesc(fs, vidx);
    var->vd.ridx = cast_byte(reglevel++);
    var->vd.pidx = registerlocalvar(ls, fs, var->vd.name);
    luaY_checklimit(fs, reglevel, MAXVARS, "local variables");
  }
}
```

逻辑是直白的线性分配:从当前已经占用的最高寄存器 `reglevel`(`luaY_nvarstack` 算出,见下)开始,把这批刚声明的变量**依次塞进连续的寄存器号** `reglevel, reglevel+1, ...`。变量甲占 R0,变量乙占 R1,变量丙占 R2,顺序就是声明顺序。Lua 没有复杂的寄存器分配算法(没有图着色、没有活跃变量分析),就是这种朴素的线性栈式分配——每个作用域进来时在栈顶摞一层,出去时弹掉。这是它编译器能这么小的原因之一。

`registerlocalvar`(`lparser.c:175`)是顺带往 `Proto->locvars` 里写一条**调试信息**(名字 + 起始 pc),跟运行时无关,纯粹给调试器/错误信息用:

```c
static short registerlocalvar (LexState *ls, FuncState *fs,
                               TString *varname) {
  Proto *f = fs->f;
  int oldsize = f->sizelocvars;
  luaM_growvector(ls->L, f->locvars, fs->ndebugvars, f->sizelocvars,
                  LocVar, SHRT_MAX, "local variables");
  while (oldsize < f->sizelocvars)
    f->locvars[oldsize++].varname = NULL;
  f->locvars[fs->ndebugvars].varname = varname;
  f->locvars[fs->ndebugvars].startpc = fs->pc;
  luaC_objbarrier(ls->L, f, varname);
  return fs->ndebugvars++;
}
```

`LocVar` 结构(`lobject.h:560`)只有 `varname`、`startpc`、`endpc` 三个字段,纯调试用。

寄存器槽位能被复用的关键在 `removevars`(`lparser.c:346`):

```c
static void removevars (FuncState *fs, int tolevel) {
  fs->ls->dyd->actvar.n -= (fs->nactvar - tolevel);
  while (fs->nactvar > tolevel) {
    LocVar *var = localdebuginfo(fs, --fs->nactvar);
    if (var)  /* does it have debug information? */
      var->endpc = fs->pc;
  }
}
```

出作用域时,把当前函数的活跃变量数 `nactvar` 缩回到 `tolevel`,同时从全局 `actvar` 数组尾部弹出这些项。注意它**没有清寄存器**——寄存器槽位是否还在用,由 `freereg`(下一章 P2-09 讲)和 `reglevel` 共同管理;`removevars` 只负责让这些变量从"名字→槽位"的映射里消失,于是运行时同一段寄存器槽位可以被后续新声明的变量重新占用。这就是局部变量寄存器复用的全部机制。

一个细节:`reglevel` / `luaY_nvarstack`(`lparser.c:236`、`250`)要处理"有些局部变量不在寄存器里"(比如 5.5 的编译期常量 `const`,见下)的情况,所以它不是简单地等于 `nactvar`,而是**往前找最近的在寄存器里的变量**取它的 `ridx+1`:

```c
static lu_byte reglevel (FuncState *fs, int nvar) {
  while (nvar-- > 0) {
    Vardesc *vd = getlocalvardesc(fs, nvar);  /* get previous variable */
    if (varinreg(vd))  /* is in a register? */
      return cast_byte(vd->vd.ridx + 1);
  }
  return 0;  /* no variables in registers */
}

lu_byte luaY_nvarstack (FuncState *fs) {
  return reglevel(fs, fs->nactvar);
}
```

`varinreg`(`lparser.h:111`)的定义是 `((v)->vd.kind <= RDKTOCLOSE)`,即 kind 在 0~3 之间的变量占寄存器;kind 为 4(`RDKCTC` 编译期常量)和 5/6(全局声明)的不占寄存器。这是 5.5 新增 `const`/编译期常量机制后必须做的修正——老资料(讲 5.3/5.4)里的"局部变量数 == 寄存器数"在 5.5 不再成立,需要用 `reglevel` 而不是 `nactvar`。

### 2.2 作用域 block:enterblock / leaveblock

局部变量"进作用域分配,出作用域释放"这件事,挂在**块(block)**这个结构上。Lua 的 block 是一个链表,每进入一个语法块(`do...end`、`while...end`、函数体等)就 push 一个 `BlockCnt`,出去时 pop。

`BlockCnt` 定义在 `lparser.c:49`:

```c
typedef struct BlockCnt {
  struct BlockCnt *previous;  /* chain */
  int firstlabel;  /* index of first label in this block */
  int firstgoto;  /* index of first pending goto in this block */
  short nactvar;  /* number of active declarations at block entry */
  lu_byte upval;  /* true if some variable in the block is an upvalue */
  lu_byte isloop;  /* 1 if 'block' is a loop; 2 if it has pending breaks */
  lu_byte insidetbc;  /* true if inside the scope of a to-be-closed var. */
} BlockCnt;
```

注意三个关键字段:

- `nactvar`:进这个 block 时当前函数已有多少个活跃局部变量。出 block 时要把变量数缩回这个值。
- `upval`:这个 block 里有没有变量被内层函数当成 upvalue 引用(或者有 to-be-closed 变量)。如果是,出 block 时不能直接弹寄存器,要先发一条 `OP_CLOSE` 指令把 upvalue 关闭(详见 2.4 和 P4-14)。
- `firstlabel`/`firstgoto`:goto/label 机制用,见 2.7。

进 block(`lparser.c:720`):

```c
static void enterblock (FuncState *fs, BlockCnt *bl, lu_byte isloop) {
  bl->isloop = isloop;
  bl->nactvar = fs->nactvar;
  bl->firstlabel = fs->ls->dyd->label.n;
  bl->firstgoto = fs->ls->dyd->gt.n;
  bl->upval = 0;
  /* inherit 'insidetbc' from enclosing block */
  bl->insidetbc = (fs->bl != NULL && fs->bl->insidetbc);
  bl->previous = fs->bl;  /* link block in function's block list */
  fs->bl = bl;
  lua_assert(fs->freereg == luaY_nvarstack(fs));
}
```

把当前 `nactvar` 记进 `bl->nactvar`(留作出 block 时回退用),记下 label/goto 列表的当前水位(同样的目的),清零 `upval`,然后把这个 block 挂到 `fs->bl` 链表头。

出 block(`lparser.c:745`):

```c
static void leaveblock (FuncState *fs) {
  BlockCnt *bl = fs->bl;
  LexState *ls = fs->ls;
  lu_byte stklevel = reglevel(fs, bl->nactvar);  /* level outside block */
  if (bl->previous && bl->upval)  /* need a 'close'? */
    luaK_codeABC(fs, OP_CLOSE, stklevel, 0, 0);
  fs->freereg = stklevel;  /* free registers */
  removevars(fs, bl->nactvar);  /* remove block locals */
  lua_assert(bl->nactvar == fs->nactvar);  /* back to level on entry */
  if (bl->isloop == 2)  /* has to fix pending breaks? */
    createlabel(ls, ls->brkn, 0, 0);
  solvegotos(fs, bl);
  if (bl->previous == NULL) {  /* was it the last block? */
    if (bl->firstgoto < ls->dyd->gt.n)  /* still pending gotos? */
      undefgoto(ls, &ls->dyd->gt.arr[bl->firstgoto]);  /* error */
  }
  fs->bl = bl->previous;  /* current block now is previous one */
}
```

关键三步:

1. 如果 `bl->upval` 为真(有变量被内层当 upvalue,或有 to-be-closed),发一条 `OP_CLOSE stklevel`——这条指令运行时会把所有 `>= stklevel` 的 upvalue 关闭成 closed 状态(P4-14)。这就是"作用域出口要关 upvalue"在编译期的落点。
2. `removevars(fs, bl->nactvar)`:把本 block 声明的局部变量从名字表弹出。
3. `solvegotos`:处理本 block 里还没匹配上的 goto(见 2.7)。

注意条件 `bl->previous`——只有不是函数最外层 block 时才发 `OP_CLOSE`。函数最外层 block 的 upvalue 关闭由 `OP_RETURN` 自己负责(`lvm.c` 里 `luaF_close`),不需要单独 `OP_CLOSE`。

`isloop == 2` 是 break 的处理:循环 block 进入时 `isloop=1`,如果其中有 `break`,会把 `isloop` 改成 2(详见 `breakstat`),出 block 时用 `createlabel` 造一个名为 `brkn`(默认 `*break`)的隐式 label,所有 break 跳到这里。

### 2.3 变量查找:searchvar

编译期遇到一个名字(比如 `n`),要确定它是局部变量、upvalue 还是全局。这件事分两层:先在当前函数里查(`searchvar`),查不到就递归到外层函数查(`singlevaraux`)。

`searchvar`(`lparser.c:414`)在当前函数的活跃局部变量里**从新到老**倒序找:

```c
static int searchvar (FuncState *fs, TString *n, expdesc *var) {
  int i;
  for (i = cast_int(fs->nactvar) - 1; i >= 0; i--) {
    Vardesc *vd = getlocalvardesc(fs, i);
    if (varglobal(vd)) {  /* global declaration? */
      if (vd->vd.name == NULL) {  /* collective declaration? */
        if (var->u.info < 0)  /* no previous collective declaration? */
          var->u.info = fs->firstlocal + i;  /* this is the first one */
      }
      else {  /* global name */
        if (eqstr(n, vd->vd.name)) {  /* found? */
          init_exp(var, VGLOBAL, fs->firstlocal + i);
          return VGLOBAL;
        }
        else if (var->u.info == -1)  /* active preambular declaration? */
          var->u.info = -2;  /* invalidate preambular declaration */
      }
    }
    else if (eqstr(n, vd->vd.name)) {  /* found? */
      if (vd->vd.kind == RDKCTC)  /* compile-time constant? */
        init_exp(var, VCONST, fs->firstlocal + i);
      else {  /* local variable */
        init_var(fs, var, i);
        if (vd->vd.kind == RDKVAVAR)  /* vararg parameter? */
          var->k = VVARGVAR;
      }
      return cast_int(var->k);
    }
  }
  return -1;  /* not found */
}
```

倒序是为了让内层同名变量遮蔽外层(后声明的先被找到,直接 return)。`eqstr` 用指针比较(`lparser.c:43`),因为词法分析器对所有字符串都做了驻留(P1-03),同一个名字一定是同一个 `TString *`。

这里要特别注意 5.5 的一个**重大新特性**:`global` 声明。5.5 引入了显式的变量声明语法(`global`/`local`、带 `const`),所以在 `searchvar` 里多了一整段对"全局声明"(`varglobal(vd)`,即 `kind >= GDKREG`)的处理。它的语义是:函数体里如果显式写了 `global x`(单个名字声明)或者 `global` 不带名字(集体声明,意味着"后面所有未声明的名字都默认全局"),那么查不到局部变量时,这个名字直接被认定为全局,不需要再走 `_ENV` 那套间接路径之外的查找。`var->u.info` 的几个特殊值(`-1` 表示"目前只有默认的前导声明",`-2` 表示"已经失效",`>=0` 指向具体的集体声明项)就是为这套语义服务的。

老资料(讲 5.4 及之前)的 `searchvar` 没有这段——5.4 之前所有"查不到的局部变量"一律是全局,走 `_ENV`。这是 5.5 相对老资料的一个硬差异,凡引用 `searchvar` 必须按 5.5 实况讲。

返回值的几种情况(对应 `expkind` 枚举,`lparser.h:25`):

- `VLOCAL`:普通局部变量(寄存器),`var->u.var.vidx` 存相对索引,`var->u.var.ridx` 存寄存器号。
- `VVARGVAR`:可变参数(`...`)那个特殊变量,5.5 把它也建模成一个特殊的局部变量,kind 为 `RDKVAVAR`。
- `VCONST`:编译期常量(`const x = 1`),不占寄存器,值直接内联到常量表。
- `VGLOBAL`:全局变量(显式 `global` 声明的,或默认全局走 `_ENV`,见 2.5)。
- `-1`:当前函数里找不到,要往上查 upvalue。

`init_var`(`lparser.c:273`)把一个局部变量装进 `expdesc`:

```c
static void init_var (FuncState *fs, expdesc *e, int vidx) {
  e->f = e->t = NO_JUMP;
  e->k = VLOCAL;
  e->u.var.vidx = cast_short(vidx);
  e->u.var.ridx = getlocalvardesc(fs, vidx)->vd.ridx;
}
```

注意它把寄存器号 `ridx` 也抄进了 `expdesc`——代码生成阶段(`lcode.c`)直接用这个号发指令,不再回查 `actvar`。

### 2.4 upvalue 登记:singlevaraux / newupvalue / markupval

`searchvar` 在当前函数找不到,就轮到 `singlevaraux`(`lparser.c:476`)递归向外层查:

```c
static void singlevaraux (FuncState *fs, TString *n, expdesc *var, int base) {
  int v = searchvar(fs, n, var);  /* look up variables at current level */
  if (v >= 0) {  /* found? */
    if (!base) {
      if (var->k == VVARGVAR)  /* vararg parameter? */
        luaK_vapar2local(fs, var);  /* change it to a regular local */
      if (var->k == VLOCAL)
        markupval(fs, var->u.var.vidx);  /* will be used as an upvalue */
    }
    /* else nothing else to be done */
  }
  else {  /* not found at current level; try upvalues */
    int idx = searchupvalue(fs, n);  /* try existing upvalues */
    if (idx < 0) {  /* not found? */
      if (fs->prev != NULL)  /* more levels? */
        singlevaraux(fs->prev, n, var, 0);  /* try upper levels */
      if (var->k == VLOCAL || var->k == VUPVAL)  /* local or upvalue? */
        idx  = newupvalue(fs, n, var);  /* will be a new upvalue */
      else  /* it is a global or a constant */
        return;  /* don't need to do anything at this level */
    }
    init_exp(var, VUPVAL, idx);  /* new or old upvalue */
  }
}
```

这是 upvalue 静态收集的核心。它由最内层发起(`base=1`),逐层向上(`base=0`)。逻辑分两种情况:

**情况一:在某一层找到了(是这一层的局部变量)。** 如果不是发起层(`!base`,即中间层),就要做一件关键事:`markupval(fs, var->u.var.vidx)`——标记"这一层有个变量被内层当 upvalue 用了"。这个标记会让包含这个变量的 block 在出口时发 `OP_CLOSE` 指令(因为 upvalue 需要在变量离开寄存器时被"关闭",P4-14 详讲开合)。如果是发起层(`base==1`),啥也不用做,因为这个变量就是当前函数的直接外层局部,直接当 upvalue 用。

**情况二:这一层没找到。** 先查这一层已有的 upvalue(`searchupvalue`)。如果也没有,就继续递归到更外层 `fs->prev`,外层返回后,如果变量确实在某层被找到(变成了 `VLOCAL` 或 `VUPVAL`),就在当前层登记一个新的 upvalue(`newupvalue`);如果一路查到最外层都找不到,变量是全局(`VGLOBAL`)或常量(`VCONST`),当前层不登记 upvalue(全局走 `_ENV` 这个特殊 upvalue,见 2.5;常量直接内联)。

递归回溯的过程中,每一层中间函数都会 `newupvalue` 把这个变量登记进自己的 upvalue 表——这是必须的:upvalue 是一条"链",从最内层到真正定义那个变量的层,中间每一层都得有对应的 upvalue 槽位,运行时闭包创建时(P4-14)才能逐层把这条链接起来。

`searchupvalue`(`lparser.c:360`)是 upvalue 去重的关键:

```c
static int searchupvalue (FuncState *fs, TString *name) {
  int i;
  Upvaldesc *up = fs->f->upvalues;
  for (i = 0; i < fs->nups; i++) {
    if (eqstr(up[i].name, name)) return i;
  }
  return -1;  /* not found */
}
```

同一个名字在同一个函数里只登记一次 upvalue。这是 upvalue 去重——内层函数里多次引用同一个外层变量(比如 `n = n + 1` 里 `n` 出现两次),编译期只产生一个 upvalue 槽位,字节码里两次都引用同一个索引。

`newupvalue`(`lparser.c:382`)是登记新 upvalue 的地方:

```c
static int newupvalue (FuncState *fs, TString *name, expdesc *v) {
  Upvaldesc *up = allocupvalue(fs);
  FuncState *prev = fs->prev;
  if (v->k == VLOCAL) {
    up->instack = 1;
    up->idx = v->u.var.ridx;
    up->kind = getlocalvardesc(prev, v->u.var.vidx)->vd.kind;
    lua_assert(eqstr(name, getlocalvardesc(prev, v->u.var.vidx)->vd.name));
  }
  else {
    up->instack = 0;
    up->idx = cast_byte(v->u.info);
    up->kind = prev->f->upvalues[v->u.info].kind;
    lua_assert(eqstr(name, prev->f->upvalues[v->u.info].name));
  }
  up->name = name;
  luaC_objbarrier(fs->ls->L, fs->f, name);
  return fs->nups - 1;
}
```

这里正是 upvalue 两种来源的分水岭,由 `instack` 字段编码:

- **`instack=1`**:这个 upvalue 来自**外层的局部变量(在寄存器栈上)**。`idx` 存的是那个变量在外层函数的寄存器号 `ridx`。
- **`instack=0`**:这个 upvalue 来自**外层已有的 upvalue(不在栈上,在外层的 upvalue 表里)**。`idx` 存的是外层 upvalue 表里的索引 `v->u.info`。

`Upvaldesc` 结构(`lobject.h:548`)就这四个字段:

```c
typedef struct Upvaldesc {
  TString *name;  /* upvalue name (for debug information) */
  lu_byte instack;  /* whether it is in stack (register) */
  lu_byte idx;  /* index of upvalue (in stack or in outer function's list) */
  lu_byte kind;  /* kind of corresponding variable */
} Upvaldesc;
```

`instack` + `idx` 这两个字段就是**编译期静态分析的全部产物**:运行时闭包创建时,VM 拿着这两个字节就能正确地把这条 upvalue 链接起来——要么从外层栈上抓一个值槽(`instack=1`,这时 P4-14 会把这个栈槽升级成一个 open upvalue 对象),要么从外层闭包的 upvalue 数组里复制一个引用(`instack=0`)。不需要名字、不需要查找、不需要遍历调用链。

`markupval`(`lparser.c:451`)把"某个 level 的变量被当 upvalue"这件事标记到正确的 block 上:

```c
static void markupval (FuncState *fs, int level) {
  BlockCnt *bl = fs->bl;
  while (bl->nactvar > level)
    bl = bl->previous;
  bl->upval = 1;
  fs->needclose = 1;
}
```

沿着 block 链往回找,找到"这个变量是在哪个 block 进来时声明的"(`bl->nactvar <= level` 的那个 block),把它的 `upval` 标记为 1。这样那个 block 出口时就会发 `OP_CLOSE`。同时把整个函数的 `fs->needclose` 标记为 1——函数返回时也要关 upvalue。

### 2.5 _ENV:全局变量也统一进 upvalue

Lua 5.1 之后,全局变量访问本质上是访问一个叫 `_ENV` 的 upvalue。`_ENV` 指向全局表(默认是 `_G`)。所有"裸名字"`x`(不是局部、不是 upvalue)都被编译成 `_ENV.x`。

这件事的入口是 `buildglobal`(`lparser.c:502`):

```c
static void buildglobal (LexState *ls, TString *varname, expdesc *var) {
  FuncState *fs = ls->fs;
  expdesc key;
  init_exp(var, VGLOBAL, -1);  /* global by default */
  singlevaraux(fs, ls->envn, var, 1);  /* get environment variable */
  if (var->k == VGLOBAL)
    luaK_semerror(ls, "%s is global when accessing variable '%s'",
                      LUA_ENV, getstr(varname));
  luaK_exp2anyregup(fs, var);  /* _ENV could be a constant */
  codestring(&key, varname);  /* key is variable name */
  luaK_indexed(fs, var, &key);  /* 'var' represents _ENV[varname] */
}
```

`ls->envn` 就是字符串 `_ENV`(`llex.h:24` 定义 `LUA_ENV` 为 `"_ENV"`)。它把 `_ENV` 当一个普通名字去 `singlevaraux` 查——结果一定是 upvalue(因为 `_ENV` 在最外层 main 函数里被预先注入成一个指向全局表的 upvalue)。拿到 `_ENV` 这个 upvalue 后,把变量名作为字符串 key,编译成 `_ENV[varname]` 这种 indexed 访问,最终落到 `GETTABUP` 指令(`R[A] := UpValue[B][K[C]:string]`,`lopcodes.h:247`)。

这个设计非常关键:它把"全局变量"这种本来需要单独机制的东西,统一进了 upvalue 体系。`_ENV` 本身就是一个 upvalue,可以像普通 upvalue 一样被内层函数捕获、被重新赋值(改变全局环境)。Lua 的"全局"不是 VM 的特殊概念,而是一个能被源码操纵的普通 upvalue。这是"统一"主线在编译器里的一个具体落地。

`buildvar`(`lparser.c:520`)是名字查找的总入口,处理"是全局还是局部/upvalue"的分流:

```c
static void buildvar (LexState *ls, TString *varname, expdesc *var) {
  FuncState *fs = ls->fs;
  init_exp(var, VGLOBAL, -1);  /* global by default */
  singlevaraux(fs, varname, var, 1);
  if (var->k == VGLOBAL) {  /* global name? */
    int info = var->u.info;
    /* global by default in the scope of a global declaration? */
    if (info == -2)
      luaK_semerror(ls, "variable '%s' not declared", getstr(varname));
    buildglobal(ls, varname, var);
    ...
  }
}
```

注意它先把 `var` 默认初始化成 `VGLOBAL`(`info=-1`),再调 `singlevaraux`。如果 `singlevaraux` 在局部或 upvalue 里找到了,`var->k` 会被改成 `VLOCAL`/`VUPVAL`/`VCONST`;如果没找到,保持 `VGLOBAL`,走 `buildglobal` 走 `_ENV` 那条路。`info == -2` 是 5.5 显式全局声明机制下的"未声明"错误(在 `global` 块里用了没声明的名字)。

### 2.6 嵌套函数编译与 CLOSURE 指令

内层函数(比如 `counter` 里的那个匿名函数)是独立编译的——它有自己的 `FuncState`、自己的 `Proto`。编译入口是 `body`(`lparser.c:1103`):

```c
static void body (LexState *ls, expdesc *e, int ismethod, int line) {
  /* body ->  '(' parlist ')' block END */
  FuncState new_fs;
  BlockCnt bl;
  new_fs.f = addprototype(ls);
  new_fs.f->linedefined = line;
  open_func(ls, &new_fs, &bl);
  checknext(ls, '(');
  if (ismethod) {
    new_localvarliteral(ls, "self");  /* create 'self' parameter */
    adjustlocalvars(ls, 1);
  }
  parlist(ls);
  checknext(ls, ')');
  statlist(ls);
  new_fs.f->lastlinedefined = ls->linenumber;
  check_match(ls, TK_END, TK_FUNCTION, line);
  codeclosure(ls, e);
  close_func(ls);
}
```

几个要点:

- `addprototype`(`lparser.c:768`):在外层函数的 `Proto->p` 数组里**追加一个新 Proto 指针**。内层函数的 Proto 是外层 Proto 的子项,运行时通过索引访问。
- `open_func` / `close_func`(`lparser.c:799` / `830`):压栈/弹栈 `FuncState`。`open_func` 把新 `FuncState` 链到 `ls->fs`(`fs->prev = ls->fs; ls->fs = fs`),并初始化 `firstlocal = ls->dyd->actvar.n`(记下"我的局部变量从全局 actvar 数组哪里开始")。`close_func` 收尾:发最终 `RETURN`、`leaveblock`、`luaK_finish`(跳转回填)、shrink 各个数组、`ls->fs = fs->prev`(恢复外层 FuncState)。
- `codeclosure`(`lparser.c:792`):在外层函数里发一条 `OP_CLOSURE` 指令。注意它操作的是 `ls->fs->prev`(外层 FuncState),因为此时 `ls->fs` 还是刚 close 的内层——`codeclosure` 在 `close_func` 之前调用,但发指令用的是外层 fs:

```c
static void codeclosure (LexState *ls, expdesc *v) {
  FuncState *fs = ls->fs->prev;
  init_exp(v, VRELOC, luaK_codeABx(fs, OP_CLOSURE, 0, fs->np - 1));
  luaK_exp2nextreg(fs, v);  /* fix it at the last register */
}
```

`OP_CLOSURE A Bx`(`lopcodes.h:337`):`R[A] := closure(KPROTO[Bx])`。`Bx` 是外层 Proto 常量区里"Proto 指针表"的索引——其实 `Proto->p` 数组的索引就是 `fs->np - 1`(刚 append 进去的那个)。运行时(`lvm.c:1929`)VM 执行 `OP_CLOSURE` 时,会创建一个新的 `LClosure`,按内层 Proto 的 `upvalues` 表(那些 `Upvaldesc`)逐个把 upvalue 连上:对每个 `instack=1` 的,调 `luaF_findupval(L, base + uv[i].idx)` 在当前栈上找一个 open upvalue;对 `instack=0` 的,直接复制外层闭包的 `upvals[uv[i].idx]`。这条链路的运行时细节留到 P4-14,这里只要记住:**编译期产出的 `Upvaldesc` 表,就是运行时连 upvalue 的全部依据。**

### 2.7 goto 与 label

5.x 的 goto 是一个延迟匹配机制:goto 先发一条跳转指令占位,label 后面出现时回填跳转目标。这要求编译期跟踪所有"还没匹配上的 goto"。

数据结构在 `lparser.h`:

```c
typedef struct Labeldesc {
  TString *name;  /* label identifier */
  int pc;  /* position in code */
  int line;  /* line where it appeared */
  short nactvar;  /* number of active variables in that position */
  lu_byte close;  /* true for goto that escapes upvalues */
} Labeldesc;

typedef struct Labellist {
  Labeldesc *arr;
  int n;
  int size;
} Labellist;
```

`Dyndata` 里挂两条 `Labellist`:`gt`(pending gotos)和 `label`(active labels)。每个 block 进来时记下当时的 `gt.n` 和 `label.n`(在 `BlockCnt` 的 `firstgoto`/`firstlabel`),出去时处理这期间新增的 goto/label。

label 的登记:`createlabel`(`lparser.c:678`)调 `newlabelentry` 往 `label` 列表加一项,`pc` 是当前指令位置(`luaK_getlabel` 取的)。

goto 的登记:`newgotoentry`(`lparser.c:663`):

```c
static int newgotoentry (LexState *ls, TString *name, int line) {
  FuncState *fs = ls->fs;
  int pc = luaK_jump(fs);  /* create jump */
  luaK_codeABC(fs, OP_CLOSE, 0, 1, 0);  /* spaceholder, marked as dead */
  return newlabelentry(ls, &ls->dyd->gt, name, line, pc);
}
```

注意一个精妙设计:goto 发的是**两条指令**——一条 `JMP`(跳转,目标待填)后面跟一条占位的 `OP_CLOSE`。这个占位的 CLOSE 是"死指令"(操作数第 1 参数 `B=1` 标记为占位),正常执行不会跑到。它的作用是:如果后面发现这个 goto 跳出的是一个有 upvalue 的 block(需要关 upvalue),匹配 label 时就把这两条指令**对调**(CLOSE 挪到 JMP 前面),让跳转前先关 upvalue。这就是 `closegoto`(`lparser.c:597`)里那段看起来奇怪的"交换指令"在做的事:

```c
  if (gt->close ||
      (label->nactvar < gt->nactvar && bup)) {  /* needs close? */
    lu_byte stklevel = reglevel(fs, label->nactvar);
    /* move jump to CLOSE position */
    fs->f->code[gt->pc + 1] = fs->f->code[gt->pc];
    /* put CLOSE instruction at original position */
    fs->f->code[gt->pc] = CREATE_ABCk(OP_CLOSE, stklevel, 0, 0, 0);
    gt->pc++;  /* must point to jump instruction */
  }
```

为什么不一开始就发对的顺序?因为发 goto 时还不知道目标 label 在哪个作用域、需不需要关 upvalue——这是 goto/label 延迟匹配的本质。占位+对调是一个简洁的解法。

goto 的匹配在 `solvegotos`(`lparser.c:696`,出 block 时调用)和 `findlabel`(`lparser.c:626`):

```c
static Labeldesc *findlabel (LexState *ls, TString *name, int ilb) {
  Dyndata *dyd = ls->dyd;
  for (; ilb < dyd->label.n; ilb++) {
    Labeldesc *lb = &dyd->label.arr[ilb];
    if (eqstr(lb->name, name))  /* correct label? */
      return lb;
  }
  return NULL;  /* label not found */
}
```

`findlabel` 只在**当前函数的可见 label**里找(从 `ilb` 开始),这强制了 goto 不能跨函数跳。`solvegotos` 处理"出 block 时还没匹配的 goto":把它们**上移到外层 block**(`gt->nactvar = bl->nactvar`,因为内层变量已经出作用域了),留给外层 block 去匹配。如果到函数最外层 block 出去时还有 pending goto,`leaveblock` 里那段会报 "no visible label" 错误:

```c
  if (bl->previous == NULL) {  /* was it the last block? */
    if (bl->firstgoto < ls->dyd->gt.n)  /* still pending gotos? */
      undefgoto(ls, &ls->dyd->gt.arr[bl->firstgoto]);  /* error */
  }
```

goto 的作用域约束还有一条:`closegoto` 里 `if (gt->nactvar < label->nactvar) jumpscopeerror(...)`——goto 不能跳进一个比它当前作用域变量更多的位置(即不能跳进某个局部变量的作用域中间),否则那个局部变量的寄存器槽位状态不对。这是词法作用域的硬约束,编译期强制。

### 2.8 把一个闭包的编译过程走一遍

把上面的机制串起来,看开头那个 `counter` 例子怎么编译。

1. 编译 `counter` 函数体,`open_func` 建一个 `FuncState`(记为 FS1)。`parlist` 声明参数(无),进 `counter` 的 block。
2. `local n = 0`:`new_localvar("n")` 在 actvar 加一项,`adjustlocalvars` 给 `n` 分配寄存器 R0(`n` 的 `ridx=0`)。
3. `return function() ... end`:遇到 `function`,调 `body`。
   - `addprototype`:在 FS1 的 `Proto->p` 加一个新 Proto(记为 P2)。
   - `open_func`:建 FS2,`fs->prev = FS1`,`firstlocal = actvar.n`(FS2 的局部变量从 actvar 当前末尾开始)。
   - 编译内层函数体。遇到 `n = n + 1` 里的 `n`:
     - `singlevar` → `buildvar` → `singlevaraux(FS2, "n", ...)`。
     - `searchvar(FS2, "n")`:FS2 没有叫 `n` 的局部变量,返回 -1。
     - `searchupvalue(FS2, "n")`:FS2 还没有 upvalue,返回 -1。
     - `fs->prev != NULL`(FS1 存在),递归 `singlevaraux(FS1, "n", ..., base=0)`。
     - `searchvar(FS1, "n")`:找到,是 VLOCAL,`ridx=0`,`vidx=0`。
     - `!base`,所以 `markupval(FS1, 0)`:标记 FS1 里包含 `n` 的 block 的 `upval=1`,FS1 的 `needclose=1`。
     - 递归返回,`var->k` 是 `VLOCAL`,在 FS2 里 `newupvalue(FS2, "n", var)`:`instack=1`(来自外层局部)、`idx=0`(外层寄存器 R0)、登记进 FS2 的 upvalue 表,返回索引 0。
     - `var->k = VUPVAL`,`info=0`。
   - 于是内层函数体里两次 `n`(读和写)都解析成 upvalue 0。读:`GETUPVAL`,`lcode.c:835` 发 `OP_GETUPVAL`。写:`luaK_storevar` 的 `VUPVAL` 分支(`lcode.c:1112`)发 `OP_SETUPVAL`。
   - `statlist` 编译完,`codeclosure` 在 FS1 发 `OP_CLOSURE A Bx`(Bx 指向 P2),`close_func` 收尾 FS2。
4. `counter` 函数体编译完,`close_func` 收尾 FS1,发最终 `RETURN`。

编译完之后:FS2 的 Proto 的 `upvalues` 表里有一条记录 `{name="n", instack=1, idx=0, kind=0}`。FS1 的字节码里有一条 `OP_CLOSURE`。FS1 里包含 `n` 的 block 出口(也就是 `counter` 函数返回时)会触发 upvalue 关闭——但这里 `counter` 是函数最外层 block,关闭由 `OP_RETURN` 负责,不发单独 `OP_CLOSE`。

运行时执行 `OP_CLOSURE`:VM 读 P2 的 `upvalues[0]`,看到 `instack=1, idx=0`,调 `luaF_findupval(L, base + 0)` 把 FS1 当时栈上 R0 的位置升级成一个 open upvalue 对象,挂进新闭包的 `upvals[0]`。从此内层闭包每次 `GETUPVAL 0` 就直接拿到这个 upvalue 对象里的值——这就是运行时开合的起点,P4-14 会接着讲这个 upvalue 对象怎么在被捕获变量离开栈时变成 closed。

整个过程的关键:编译期就静态确定了"内层的 upvalue 0 来自外层的 R0 寄存器"。运行时没有任何名字查找。

---

## 三、为什么这样设计是 sound 的

这一节回答"为什么这套机制是对的",分四个点。

### 3.1 编译期静态分析的正确性根基:词法作用域

整套 upvalue 机制能前移到编译期,根上靠的是**词法作用域**这一前提。词法作用域意味着:一个名字指向哪个变量,由它在源码里的嵌套位置静态决定,不随运行时调用顺序变化。

这一点保证了:函数的嵌套结构(`FuncState->prev` 链)、每层有哪些局部变量、变量在哪个寄存器——这些都是编译期能完全遍历的静态事实。`singlevaraux` 顺着 `fs->prev` 链向上递归,就是在遍历这条静态的嵌套链。它一定能找到答案(要么是某一层的局部,要么是全局),因为词法作用域下名字的绑定在编译期就闭合。

如果是动态作用域(Lua 不是),这套就完全不成立——运行时才知道名字指向谁,upvalue 没法静态登记。Lua 把作用域做成词法的,正是为了让这一整套静态分析成立。

### 3.2 upvalue 去重的 sound:同一个外层变量只一份

`searchupvalue` 在登记新 upvalue 前先查已有的,保证同一个外层变量在一个函数里只占一个 upvalue 槽位。这件事的 sound 体现在两个层面:

- **正确性**:upvalue 索引是字节码里硬编码的(`GETUPVAL A B` 的 `B`)。如果同一个变量被登记两次,就会出现"两个索引指向同一个外层变量"的歧义,代码生成和运行时链接都会混乱。去重保证索引与变量一一对应。
- **效率**:内层函数里多次引用同一个外层变量(很常见,比如循环计数器),只占一个 upvalue 槽位,字节码里所有引用共用一个索引,运行时也只连一次。

### 3.3 instack/idx 二元编码的完备性

upvalue 只有两个来源:要么是外层栈上的局部变量(`instack=1`),要么是外层的某个 upvalue(`instack=0`)。这覆盖了所有可能——因为顺着 `fs->prev` 链向上,任何一个被捕获的变量,要么在某一层是个寄存器里的局部(到那一层 `instack=1`),要么从那一层再往上本身就是 upvalue(中间层 `instack=0`)。没有第三种情况。

`newupvalue` 里两个分支(`v->k == VLOCAL` 和 else)正好对应这两种,`instack` + `idx` 两个字节就能无歧义地编码 upvalue 的来源。运行时 VM 拿到这两个字节,要么 `luaF_findupval(base + idx)`(栈上),要么 `外层闭包->upvals[idx]`(外层 upvalue 表),二选一,没有歧义路径。这是一个极简但完备的编码。

### 3.4 _ENV 统一的 sound:没有特例

把全局变量统一成 `_ENV[name]`,意味着 VM 不需要为"全局变量访问"单独设计一套机制。全局变量访问就是一个 indexed 操作(`GETTABUP`,因为 `_ENV` 是 upvalue,`GetTable` 优化成 `GETTABUP`),跟普通 `t[k]` 走同一条代码路径。这消除了一个本来要特殊处理的特例,是"精简"的典型体现。

同时 `_ENV` 本身是普通 upvalue 这件事,让 Lua 获得了一个能力:可以用 `local _ENV = {...}` 改变一段代码的全局环境(沙箱、模块隔离的基础)。如果 `_ENV` 是 VM 写死的特殊变量,这种用法就不可能。统一换来了灵活性。

### 3.5 OP_CLOSE 的 sound:upvalue 不悬空

`markupval` 标记 + `leaveblock` 发 `OP_CLOSE`,保证了一个 block 出口时,所有在这个 block 里声明且被内层捕获的变量,其对应的 open upvalue 会被正确关闭(P4-14 讲 closed 状态)。如果没有这一步,变量离开寄存器后(upvalue 还指向那个栈槽),下一次调用复用这段栈,upvalue 就会读到错误的数据。`OP_CLOSE` 在出口处把 open upvalue 的值复制到一个独立的堆上对象(closed upvalue),从此与栈脱钩。这是 upvalue 机制不悬空的编译期保障——编译器负责在正确的位置发 `OP_CLOSE`,运行时负责执行关闭。

---

## 四、★对照 CPython + 回扣主线

### ★对照 CPython:cell 对象的运行时间接 vs 编译期索引

CPython 的闭包走的是另一条路。Python 的局部变量在 `PyCodeObject` 里有三个名字表:`co_varnames`(局部)、`co_cellvars`(被内层捕获的局部)、`co_freevars`(本函数的 upvalue)。被捕获的局部变量在运行时**不直接存在帧的 `fastlocals` 数组里,而是被包进一个 `cell` 对象**,对这个变量的读写都变成"读 cell.value / 写 cell.value"的间接访问。

具体来说,CPython 编译期做的事是:

1. 标记哪些变量是 cell(`co_cellvars`)、哪些是 free(`co_freevars`)。
2. 给它们分配索引(在 fastlocals 里的位置)。

但运行时访问这些变量,字节码是 `LOAD_DEREF i` / `STORE_DEREF i`——这里的 `i` 索引的是帧里的一个 cell 指针数组,每次访问都要先取 cell 指针、再解引用 cell 拿到真正的值。也就是说:**Python 的闭包变量访问是两次间接(帧→cell→值)**。

对照 Lua:

| 维度 | Lua 5.5 | CPython |
|---|---|---|
| **upvalue 索引确定时机** | 编译期完全确定(`Upvaldesc.instack/idx`) | 编译期确定 cell/freevar 索引,但值要通过 cell 间接取 |
| **运行时访问指令** | `GETUPVAL A B`:直接 `R[A] := UpValue[B]`,一次取 | `LOAD_DEREF i`:取 cell 指针再取 `.ob_ref`,两次间接 |
| **被捕获变量的存储** | 变量本来在寄存器(栈槽),被捕获时按需升级成 open upvalue 对象 | 被捕获变量从一开始就装在 cell 里(即使没被内层访问,只要在 cellvars 里就是 cell) |
| **全局变量** | 统一成 `_ENV` 这个 upvalue 的 indexed 访问(`GETTABUP`) | 单独的 `LOAD_NAME`/`STORE_NAME`(走帧的 `f_locals` 字典或 globals 字典) |
| **开合状态** | 区分 open(指向栈)/ closed(独立对象),按需关闭 | 无 open/closed 之分,被捕获变量始终在 cell 对象里 |

核心差异:**Lua 的 upvalue 是"按需升级"**——变量在被捕获之前,就是寄存器里一个普通值,访问是零开销的寄存器读写(`GETUPVAL` 只在内层访问时才用);只有当它真的被内层函数捕获且外层函数即将返回时,才通过 `OP_CLOSE` 升级成 closed 对象。**CPython 的 cell 是"预先装箱"**——只要一个变量在 `co_cellvars` 里(编译期标记),它运行时就是个 cell,即使外层函数还没返回、即使这个 cell 从未被内层真正访问,每次读写都要过一层 cell 间接。

Lua 这套设计的代价是更复杂的运行时状态(open/closed 两种形态、`luaF_findupval` 的 open 链表维护、`OP_CLOSE` 的关闭逻辑,P4-14 详讲),换来的是:**外层函数访问自己的局部变量零间接(就是寄存器读写),内层访问 upvalue 一次间接(`GETUPVAL` 直接取 upvalue 对象里的值)**。这比 CPython 的两次间接少一层,也是 Lua 闭包访问更快的原因之一。

而全局变量的对照更鲜明:Lua 把全局统一进 upvalue(`_ENV`),CPython 给全局单独一套 `LOAD_NAME`/`STORE_NAME`(还要查 `f_locals` 字典,miss 了才查 globals)。Lua 的统一换来了机制更少,代价是全局访问也要走一次 table indexed(`GETTABUP`,即一次哈希查找)——但这件事两个语言都得做(全局本来就在一个表/字典里),Lua 只是把它统一进了已有的 indexed 机制,没有额外造轮子。

### 回扣主线

这一章讲的是 Lua"精简"主线在编译器里的一个大动作:**把作用域分析和 upvalue 登记前移到编译期**。运行时不查名字、不沿调用链回溯、不为全局变量单独造机制——这些省下来的运行时复杂度,都是编译期一次性付清的。

具体落到主线:

- **统一**:全局变量(`_ENV` 这个 upvalue 的 indexed 访问)、外层局部变量(`GETUPVAL`)、外层 upvalue(`instack=0`)——三种"非当前函数的变量",统一收进 upvalue 一套机制,一个 `Upvaldesc` 结构、一条索引指令搞定。
- **精简**:upvalue 静态收集(`singlevaraux` 递归 + 去重)让运行时闭包链接只需要两个字节(`instack`/`idx`);`instack/idx` 的二元编码是极简但完备的;goto 的占位+对调用一个机制解决了"跳转前要不要关 upvalue"这个延迟决策问题。

用更少的机制换更多的能力——这正是 Lua 又小又快的根。编译期多分析一遍,运行时就少做无数遍。

---

*下一章 [P2-09 代码生成 lcode:寄存器分配与指令发射](P2-09-代码生成lcode-寄存器分配与指令发射.md):本章留下的 `expdesc`(VLOCAL/VUPVAL/VINDEXED...)怎么落成具体的字节码,寄存器怎么线性分配,跳转怎么回填——进入代码生成阶段。*
