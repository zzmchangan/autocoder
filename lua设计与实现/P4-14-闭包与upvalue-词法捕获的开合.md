# P4-14 闭包与 upvalue:词法捕获的开合

> **本书主线**:统一与精简换小而快。**二分法**:编译侧(源码→字节码)↔ 执行侧(VM 执行 + 管值生命周期)。本章在**执行侧**,接 [P4-13 调用栈与调用约定](P4-13-调用栈与调用约定luaD_call.md)。**★对照**:CPython 的 `cell` 对象。**源码**:lua-5.5.0。**基调**:纯直球,不用比喻。

---

## 一、这章解决什么问题

Lua 的函数是一等公民:可以把函数当值传来传去、当 Table 的字段存、当另一个函数的返回值交出去。一等公民必然带出一个问题——**内层函数引用了外层函数的局部变量,外层函数返回以后,这个引用凭什么还不失效?**

看一段再普通不过的 Lua 代码:

```lua
local function counter()
  local n = 0
  return function ()
    n = n + 1
    return n
  end
end

local c = counter()
print(c())   -- 1
print(c())   -- 2
print(c())   -- 3
```

`counter()` 调完、返回了那个内层函数,`counter` 的调用帧就从调用栈上撤销了,它栈上的局部变量 `n` 所在的那个槽位,按理也该随之作废。可是后面三次调 `c()`,`n` 居然还在,还累加。

这件事在 C 里写不出来:C 的局部变量在函数返回后,栈帧就交还了,任何指向它的指针都成了悬空指针。Lua 要让"内层函数引用外层局部变量"这件事在语义上成立,就必须解决两件事:

1. **引用怎么登记**:内层函数在编译期就知道自己要引用外层的哪些局部变量(P2-08 已讲编译期怎么静态登记 upvalue 的索引)。运行期得有人按这个登记,把"内层函数"和"外层那个栈槽"真正连起来。
2. **引用怎么不悬空**:外层函数返回、栈帧撤销的那一刻,得有人把这些引用"转移"到一个能在栈外存活的地方,保证之后内层函数再用到它,读到的还是原来那个值,而不是一块被别的函数覆盖过的废内存。

Lua 的解法是 **upvalue**:一个专门的小对象,装在闭包里,代表"对某个外层局部变量的引用"。它有两个状态——

- **open(开)**:外层函数还活着,upvalue 指向外层栈上的那个槽位,读写都直接打到栈上。
- **closed(合)**:外层函数要返回了,把栈上那个槽位里的值**拷一份**进 upvalue 自己的存储,从此 upvalue 不再依赖栈,独立存活。

开合之间的转换发生在外层函数返回的那一刻,由 VM 自动完成,程序员看不见。这一章就把这套机制从数据结构到字节码到关闭逻辑,用 5.5.0 的源码逐行讲清楚,并回答:这个设计为什么 sound——为什么转换之后引用不会悬空、不会重复、不会和 GC 冲突。

这一章和 [P2-08 语法分析(下):作用域与 upvalue 的静态收集](P2-08-语法分析下-作用域与upvalue的静态收集.md) 是一条线的两头:**P2-08 在编译期静态登记 upvalue 的索引(`instack`/`idx`),本章在运行期按这个登记把 upvalue 连起来、并在外层返回时把 open 转成 closed。** 编译期登记,运行期连。

---

## 二、源码怎么实现

### 2.1 两类闭包:LClosure 与 CClosure

Lua 的函数值有两种来源:Lua 源码定义的函数、宿主用 C 写的函数。闭包也分两类,定义在 `lobject.h:699` 和 `lobject.h:706`:

```c
/* lobject.h:696 */
#define ClosureHeader \
	CommonHeader; lu_byte nupvalues; GCObject *gclist

/* lobject.h:699 */
typedef struct CClosure {
  ClosureHeader;
  lua_CFunction f;
  TValue upvalue[1];  /* list of upvalues */
} CClosure;

/* lobject.h:706 */
typedef struct LClosure {
  ClosureHeader;
  struct Proto *p;
  UpVal *upvals[1];  /* list of upvalues */
} LClosure;
```

两者的公共头部是 `ClosureHeader`:GC 头(`CommonHeader`)、upvalue 个数 `nupvalues`、GC 链表指针 `gclist`。区别在主体:

- **`CClosure`(C 闭包)**:持有一个 C 函数指针 `f`,以及一组**直接内嵌**的 `TValue upvalue[]`。C 闭包的 upvalue 就是普通值,没有 open/closed 之分——因为 C 函数没有"调用帧上的局部变量被内层函数引用"这回事,C 闭包的 upvalue 在 `lua_pushcclosure` 创建时就一次性填好、都是 closed 的。
- **`LClosure`(Lua 闭包)**:持有一个 `Proto *p`(函数原型的指针,即字节码、常量、调试信息等),以及一组 `UpVal *upvals[]`——每个元素是一个**指向 `UpVal` 对象的指针**。Lua 闭包的 upvalue 才有 open/closed 两态,因为只有 Lua 函数才有"外层函数的栈槽被内层函数引用"。

这里有个贯穿全书主线的小细节:Lua 用 `union Closure { CClosure c; LClosure l; }`(`lobject.h:713`)把两种闭包并到一个 union,VM 根据 tag 分发。这是"统一"的又一例——一套 GC、一套 TValue 表示,服务两种闭包。

### 2.2 一个函数值在 TValue 里怎么存

`TValue` 是 Lua 万物皆值的统一外壳(P1-02 详讲)。函数值装进 `TValue` 时,tag 有三种变体(`lobject.h:640`):

```c
#define LUA_VLCL	makevariant(LUA_TFUNCTION, 0)  /* Lua closure */
#define LUA_VLCF	makevariant(LUA_TFUNCTION, 1)  /* light C function */
#define LUA_VCCL	makevariant(LUA_TFUNCTION, 2)  /* C closure */
```

- `LUA_VLCL`:Lua 闭包,`TValue` 的 `.gc` 指向一个 `LClosure`。
- `LUA_VCCL`:C 闭包,`.gc` 指向一个 `CClosure`。
- `LUA_VLCF`:**轻量 C 函数**——这是一个为"省"而生的特殊编码,`TValue` 里不存对象指针,而是直接把 C 函数指针塞进 `TValue.f` 字段(`lobject.h:668` 的 `setfvalue` 宏:`val_(io).f=(x); settt_(io, LUA_VLCF);`)。轻量 C 函数没有 upvalue、不是 GC 对象,就是一个裸的函数指针。大量不带状态的 C 库函数(如 `math.sin`)用这种编码,连 `CClosure` 都不必分配——这是"精简换小而快"在函数表示上的直接体现。

本章的主角是 `LClosure` 和它的 `UpVal`。下文凡说"闭包",默认指 Lua 闭包。

### 2.3 UpVal 结构:开合两态的物理基础

`UpVal` 是这一切的核心,定义在 `lobject.h:679`:

```c
/* lobject.h:679 */
typedef struct UpVal {
  CommonHeader;
  union {
    TValue *p;  /* points to stack or to its own value */
    ptrdiff_t offset;  /* used while the stack is being reallocated */
  } v;
  union {
    struct {  /* (when open) */
      struct UpVal *next;  /* linked list */
      struct UpVal **previous;
    } open;
    TValue value;  /* the value (when closed) */
  } u;
} UpVal;
```

这是 5.5 最值得讲的一个结构。它有两个 union,分别装"当前值在哪"和"链表/值的存储":

**第一个 union `v`**:回答"这个 upvalue 的值,现在住在哪里"。

- `v.p`:一个 `TValue *` 指针。**open 态**,它指向**外层函数栈上的那个槽位**;**closed 态**,它指向**自己 `u.value`**。换句话说,读写 upvalue 永远走 `*v.p`,而 `v.p` 在 open/closed 两态下指向不同地方——这是"一式两用"的精妙处,后面 GETUPVAL/SETUPVAL 的代码会直接体现。
- `v.offset`:一个 `ptrdiff_t`。**这个字段平时不用,只在值栈扩容(realloc 搬家)的那个瞬间用**。

这里必须插一段 5.5 vs 老资料的硬差异。**Lua 5.3/5.4 的 `UpVal` 没有 `offset` 这个 union 成员**。老版本的 `UpVal.v` 就是一个单纯的 `TValue *p`,指向栈槽。问题在于:Lua 的值栈会动态扩容(`luaD_reallocstack`),`realloc` 可能把整块栈搬到新地址,于是所有指向旧栈的 `TValue *` 指针都失效了。老版本(5.4)靠在扩容时遍历 `openupval` 链表、逐个修正 `uv->v.p` 的偏移来补救。**5.5 改了路子**:引入和 `lua_State` 栈指针同源的 `StkIdRel`(相对表示,P1-02 已详讲),在栈搬家的瞬间,`UpVal` 临时把 `v.p` 换算成相对基地址的 `v.offset`(一个整数,不受搬家影响),搬完再换算回新的 `v.p`。这就是 `v` 为什么是 `union { TValue *p; ptrdiff_t offset; }`——同一个存储位置,搬家那一刻存 offset,其余时间存指针。

这个机制和 `StkIdRel` 是同源的:`StkIdRel` 让 `lua_State` 的 `top`/`stack`/`tbclist` 等指针在栈搬家时不失效,`UpVal.v.offset` 让 upvalue 的栈指针在同一时刻不失效。一处设计,两处受益——这是"用更少的机制换更多的能力"在内存布局上的又一个落点。

**第二个 union `u`**:回答"open 态的链表结点 / closed 态的值存储,共享同一块内存"。

- `u.open`:open 态时,这块内存是一个双向链表结点(`next` 和 `previous`),把所有 open upvalue 串在 `lua_State.openupval` 链表上。
- `u.value`:closed 态时,这块内存是一个 `TValue`,装拷过来的值。

注意 `u.open` 和 `u.value` **共用同一块内存**(union)。这没问题,因为:open 态时值在栈上(由 `v.p` 指着),`u` 这块内存只需要当链表结点;closed 态时 upvalue 已经从链表上摘下来,`u` 这块内存正好拿来装值。一个 union 同时承担"链表结点"和"值存储"两种角色,省掉一份内存——又是"精简"。

### 2.4 open 与 closed 的判定

怎么知道一个 upvalue 处于哪个态?看 `lfunc.h:32`:

```c
/* lfunc.h:32 */
#define upisopen(up)	((up)->v.p != &(up)->u.value)
/* lfunc.h:35 */
#define uplevel(up)	check_exp(upisopen(up), cast(StkId, (up)->v.p))
```

- `upisopen(up)`:`v.p` 不等于"自身 `u.value` 的地址",就是 open。因为 closed 态时 `v.p = &u.value`(指向自己),open 态时 `v.p` 指向栈。一个比较,判定状态。
- `uplevel(up)`:open 态时把 `v.p` 当 `StkId`(栈指针)用,返回这个 upvalue 指向的栈槽地址。`check_exp` 是个断言,保证只在 open 态调用。

有了这两个,open/closed 的语义就钉死了:**open = 指着栈,closed = 指着自己**。

### 2.5 创建与复用 open upvalue:luaF_findupval

内层函数在运行期要引用外层某个栈槽时,调 `luaF_findupval`(`lfunc.c:87`)。这个函数做两件事:**在已有的 open upvalue 链表里找有没有指向同一栈槽的,有就复用;没有就新建一个并插进去**。

```c
/* lfunc.c:87 */
UpVal *luaF_findupval (lua_State *L, StkId level) {
  UpVal **pp = &L->openupval;
  UpVal *p;
  lua_assert(isintwups(L) || L->openupval == NULL);
  while ((p = *pp) != NULL && uplevel(p) >= level) {  /* search for it */
    lua_assert(!isdead(G(L), p));
    if (uplevel(p) == level)  /* corresponding upvalue? */
      return p;  /* return it */
    pp = &p->u.open.next;
  }
  /* not found: create a new upvalue after 'pp' */
  return newupval(L, level, pp);
}
```

几个要点:

1. **链表是有序的**。`lua_State.openupval`(`lstate.h:294`)是 open upvalue 的链表头,按**栈地址从高到低**排(即从栈顶往栈底)。`while` 循环里 `uplevel(p) >= level` 是降序遍历:只要还没遍到比 `level` 更低的栈地址就继续。这个有序性是 `luaF_closeupval` 能高效工作的前提(下面讲)。
2. **找到就复用**。如果 `uplevel(p) == level`,说明已经有一个 open upvalue 指向 `level` 这个栈槽,直接返回它。这保证:**同一栈槽,全局只有一个 open upvalue**。多个内层函数引用外层同一个局部变量,它们共享同一个 `UpVal` 对象——这是为什么后面 SETUPVAL 改了值,所有引用方都能看到(它们读的是同一个 `v.p`)。
3. **没找到就新建**。调 `newupval`(`lfunc.c:65`):

```c
/* lfunc.c:65 */
static UpVal *newupval (lua_State *L, StkId level, UpVal **prev) {
  GCObject *o = luaC_newobj(L, LUA_VUPVAL, sizeof(UpVal));
  UpVal *uv = gco2upv(o);
  UpVal *next = *prev;
  uv->v.p = s2v(level);  /* current value lives in the stack */
  uv->u.open.next = next;  /* link it to list of open upvalues */
  uv->u.open.previous = prev;
  if (next)
    next->u.open.previous = &uv->u.open.next;
  *prev = uv;
  if (!isintwups(L)) {  /* thread not in list of threads with upvalues? */
    L->twups = G(L)->twups;  /* link it to the list */
    G(L)->twups = L;
  }
  return uv;
}
```

`newupval` 分配一个新的 `UpVal` GC 对象(`LUA_VUPVAL`),设 `v.p = s2v(level)`(指向栈槽,即 open 态),然后把它插进 `prev` 指向的链表位置(保持降序),并维护双向链表的 `previous`/`next`。

这里还有一处全局维护:如果当前线程 `L` 还不在"有 open upvalue 的线程"链表(`twups`,`lstate.h:297` 和 `global_State.twups` `lstate.h:363`)上,就把它链进去。为什么要有这个链表?因为 **GC 在某些阶段需要遍历所有线程的 open upvalue**(open upvalue 的值还在别的线程的栈上,GC 走不到栈以外的指针,得靠这个链表找到它们)。线程一旦有了 open upvalue,就把自己挂到 `twups` 上,GC 才不会漏。这是 sound 的一个保证(下文第 3 节展开)。

### 2.6 OP_CLOSURE:创建闭包并填 upvalue

字节码层面,`function ... end` 求值成一个闭包值,由 `OP_CLOSURE` 指令完成(`lvm.c:1929`)。它不内联展开,而是调 `pushclosure`:

```c
/* lvm.c:1929 */
vmcase(OP_CLOSURE) {
  StkId ra = RA(i);
  Proto *p = cl->p->p[GETARG_Bx(i)];
  halfProtect(pushclosure(L, p, cl->upvals, base, ra));
  checkGC(L, ra + 1);
  vmbreak;
}
```

`GETARG_Bx(i)` 是这个内层函数的原型在外层 `Proto` 的 `p[]` 数组(嵌套函数原型表)里的下标。`cl->upvals` 是**当前(外层)闭包的 upvalue 表**,传给 `pushclosure` 当 `encup`(enclosing upvalues)用。`pushclosure` 在 `lvm.c:834`:

```c
/* lvm.c:834 */
static void pushclosure (lua_State *L, Proto *p, UpVal **encup, StkId base,
                         StkId ra) {
  int nup = p->sizeupvalues;
  Upvaldesc *uv = p->upvalues;
  int i;
  LClosure *ncl = luaF_newLclosure(L, nup);
  ncl->p = p;
  setclLvalue2s(L, ra, ncl);  /* anchor new closure in stack */
  for (i = 0; i < nup; i++) {  /* fill in its upvalues */
    if (uv[i].instack)  /* upvalue refers to local variable? */
      ncl->upvals[i] = luaF_findupval(L, base + uv[i].idx);
    else  /* get upvalue from enclosing function */
      ncl->upvals[i] = encup[uv[i].idx];
    luaC_objbarrier(L, ncl, ncl->upvals[i]);
  }
}
```

这里是编译期与运行期的接头。`p->upvalues` 是 `Upvaldesc` 数组(P2-08 编译期登记的每个 upvalue 的元信息),`Upvaldesc`(`lobject.h:548`)长这样:

```c
/* lobject.h:548 */
typedef struct Upvaldesc {
  TString *name;  /* upvalue name (for debug information) */
  lu_byte instack;  /* whether it is in stack (register) */
  lu_byte idx;  /* index of upvalue (in stack or in outer function's list) */
  lu_byte kind;  /* kind of corresponding variable */
} Upvaldesc;
```

核心是 `instack` 和 `idx` 两个字段。填 upvalue 的循环按它们分两路:

- **`instack == 1`**:这个 upvalue 引用的是**当前函数(外层)栈上的某个局部变量**。`idx` 是那个局部变量相对于 `base`(当前函数栈底)的寄存器号。于是调 `luaF_findupval(L, base + uv[i].idx)`——这就是运行期"连线":按编译期登记的栈偏移,找到或新建一个指向那个栈槽的 open upvalue。
- **`instack == 0`**:这个 upvalue 引用的不是当前栈上的局部变量,而是**外层函数自己的某个 upvalue**(即"隔层引用",内层函数引用了外层函数从更外层捕获来的变量)。这时 `idx` 是外层闭包 `upvals[]` 数组的下标。于是 `ncl->upvals[i] = encup[uv[i].idx]`——直接把外层那个 `UpVal*` 指针拿过来共用,不新建、不 findupval。

这两路的区分是 Lua 作用域规则在字节码层的体现:**直接引用本函数可见的局部变量走 `instack=1`,通过外层闭包间接引用走 `instack=0`**。P2-08 在编译期决定每个 upvalue 走哪路、`idx` 填多少;本章在运行期按这个决定执行。编译期登记,运行期连——一条线贯穿。

填完 upvalue 后,`luaC_objbarrier(L, ncl, ncl->upvals[i])` 是写屏障:新闭包 `ncl` 还可能是白色的(刚分配),而它引用的 upvalue 可能已经存活很久(灰色/黑色),屏障保证 GC 的三色不变式不被破坏(P5-16 详讲)。

### 2.7 GETUPVAL 与 SETUPVAL:读写 upvalue

内层函数体里读写那个被捕获的变量,走 `OP_GETUPVAL`(`lvm.c:1287`)和 `OP_SETUPVAL`(`lvm.c:1293`):

```c
/* lvm.c:1287 */
vmcase(OP_GETUPVAL) {
  StkId ra = RA(i);
  int b = GETARG_B(i);
  setobj2s(L, ra, cl->upvals[b]->v.p);
  vmbreak;
}
/* lvm.c:1293 */
vmcase(OP_SETUPVAL) {
  StkId ra = RA(i);
  UpVal *uv = cl->upvals[GETARG_B(i)];
  setobj(L, uv->v.p, s2v(ra));
  luaC_barrier(L, uv, s2v(ra));
  vmbreak;
}
```

两条指令都通过 `cl->upvals[b]->v.p` 访问那个值。注意这里**完全没有判断 open 还是 closed**——因为不需要:`v.p` 在 open 态指向栈槽,closed 态指向自身 `u.value`,无论哪种,`*v.p` 都给出"当前应该读写的那个值"。**两态共用一套访问代码**,这是 `UpVal.v` union 设计的直接红利。

- `GETUPVAL`:把 upvalue 当前值(`*v.p`)拷到目标寄存器 `ra`。
- `SETUPVAL`:把寄存器 `ra` 的值写到 `*v.p`。如果 upvalue 是 open 的,写的就是栈槽——于是**外层函数自己**也能看到这个改动(它读那个局部变量也是读同一个栈槽);如果有别的闭包共享这个 upvalue(回忆 findupval 的复用),它们也立刻看到新值。如果 upvalue 是 closed 的,写的就是 `u.value`,同样所有共享者都看到。**这就是 counter 例子里 `n` 能跨调用累加的物理基础。**

`SETUPVAL` 末尾的 `luaC_barrier(L, uv, s2v(ra))` 是写屏障:closed upvalue 持有的值是个 GC 对象,写一个新值进去可能改变可达性,屏障通知 GC。注意 open 态的 upvalue 不需要屏障——因为 open 态的值在栈上,栈是 GC 的根,GC 直接扫得到(下文第 3 节细讲)。

### 2.8 关闭 upvalue:luaF_closeupval 与 luaF_close

现在到了开合转换的关键。外层函数返回时,它栈上的局部变量所在槽位要被回收,这时必须把所有指向这些槽位的 open upvalue **关闭**:把值从栈拷进 upvalue 自身,让 upvalue 脱离栈独立存活。

真正干这活的是 `luaF_closeupval`(`lfunc.c:197`):

```c
/* lfunc.c:197 */
void luaF_closeupval (lua_State *L, StkId level) {
  UpVal *uv;
  while ((uv = L->openupval) != NULL && uplevel(uv) >= level) {
    TValue *slot = &uv->u.value;  /* new position for value */
    lua_assert(uplevel(uv) < L->top.p);
    luaF_unlinkupval(uv);  /* remove upvalue from 'openupval' list */
    setobj(L, slot, uv->v.p);  /* move value to upvalue slot */
    uv->v.p = slot;  /* now current value lives here */
    if (!iswhite(uv)) {  /* neither white nor dead? */
      nw2black(uv);  /* closed upvalues cannot be gray */
      luaC_barrier(L, uv, slot);
    }
  }
}
```

循环条件 `uplevel(uv) >= level`:因为 open upvalue 链表按栈地址降序排(回忆 findupval),从链表头(`L->openupval`)开始,只要还指向"不低于 `level` 的栈槽"就关闭它。`level` 是调用方给的"关闭到这个栈地址为止"的边界。

循环体里四步:

1. `slot = &uv->u.value`:取 upvalue 自身 `u.value` 的地址,作为值的新归宿。
2. `luaF_unlinkupval(uv)`(`lfunc.c:186`):把这个 upvalue 从 open 链表上摘下来(维护双向链表的 `previous`/`next`)。摘下来之后,`u.open` 那块内存就不再当链表结点用了,正好可以当 `u.value` 用——union 复用在这里兑现。
3. `setobj(L, slot, uv->v.p)`:把 `v.p` 当前指向的值(也就是栈槽里的值)拷到 `slot`(也就是 `u.value`)。**这一步是 open→closed 的实质:值从栈搬进了 upvalue 自身。**
4. `uv->v.p = slot`:把 `v.p` 改指向 `u.value`。从此 `upisopen(uv)` 返回假(因为 `v.p == &u.value`),upvalue 进入 closed 态。

最后那一段 GC 处理:closed upvalue 持有一个值,这个值可能是 GC 对象。把 upvalue 染黑(`nw2black`)并加屏障,是因为 closed upvalue 现在是这个值的**唯一**可达路径持有者之一(栈上的原槽位马上要被覆盖),它必须被 GC 当作一个确定的根来追踪,不能处于"灰色"(待扫描)的中间态——否则万一 GC 在这个中间态暂停、栈槽又被覆盖,就可能漏标这个值。染黑 + 屏障,把这条可达路径钉死。这是 sound 的又一保证。

`luaF_closeupval` 是纯 upvalue 关闭。上层还有个包装 `luaF_close`(`lfunc.c:230`),除了关 upvalue,还要关 to-be-closed 变量(P4-15 详讲):

```c
/* lfunc.c:230 */
StkId luaF_close (lua_State *L, StkId level, TStatus status, int yy) {
  ptrdiff_t levelrel = savestack(L, level);
  luaF_closeupval(L, level);  /* first, close the upvalues */
  while (L->tbclist.p >= level) {  /* traverse tbc's down to that level */
    StkId tbc = L->tbclist.p;  /* get variable index */
    /* ... 关闭 to-be-closed 变量,调 __close 元方法 ... */
  }
  restorestack(L, levelrel);  /* level 可能因 close 方法调用而变,恢复 */
}
```

注意 `luaF_close` 开头 `savestack(L, level)` 把 `level` 存成相对偏移,结尾 `restorestack` 恢复——因为关闭 to-be-closed 变量可能调用 `__close` 元方法,元方法可能扩容栈、导致栈搬家,`level` 这个绝对指针会失效,必须用相对偏移保护。这又是一个和 `UpVal.v.offset`、`StkIdRel` 同源的"抗栈搬家"设计。

### 2.9 谁来触发关闭:函数返回

那关闭操作在什么时候被触发?关键在 `OP_RETURN` 指令自己。先看一个容易被老资料带偏的点:Lua 5.5 有 `OP_RETURN0`/`OP_RETURN1`/`OP_RETURN` 三种返回指令(`lcode.c:208` 的 `luaK_ret` 按返回值个数选)。编译器在收尾阶段(`lcode.c:1940` 附近)还做了一件关键的事:

```c
/* lcode.c:1940 */
case OP_RETURN0: case OP_RETURN1: {
  if (!(fs->needclose || (p->flag & PF_VAHID)))
    break;  /* no extra work */
  /* else use OP_RETURN to do the extra work */
  SET_OPCODE(*pc, OP_RETURN);
}  /* FALLTHROUGH */
case OP_RETURN: case OP_TAILCALL: {
  if (fs->needclose)
    SETARG_k(*pc, 1);  /* signal that it needs to close */
  ...
}
```

`fs->needclose` 是编译期就知道的:这个函数(或它的内层函数)建了 upvalue,返回时可能要关。一旦 `needclose` 为真,编译器就把 `RETURN0`/`RETURN1` **改写成 `RETURN`**,并在指令的 `k` 位上置 1——这个 `k` 位就是"返回时要关 upvalue"的信号(`lopcodes.h:402` 注释:"In OP_RETURN/OP_TAILCALL, 'k' specifies that the function builds upvalues, which may need to be closed")。

运行期 `OP_RETURN`(`lvm.c:1763`)读到这个 `k` 位,就调 `luaF_close`:

```c
/* lvm.c:1763 */
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
    luaF_close(L, base, CLOSEKTOP, 1);   /* <-- 这里关闭 */
    updatetrap(ci);
    updatestack(ci);
  }
  if (nparams1)  /* vararg function? */
    ci->func.p -= ci->u.l.nextraargs + nparams1;
  L->top.p = ra + n;  /* set call for 'luaD_poscall' */
  luaD_poscall(L, ci, n);
  ...
}
```

`if (TESTARG_k(i))` 成立时,调 `luaF_close(L, base, CLOSEKTOP, 1)`——`base` 是当前函数栈底,意思是"关闭从 base 到栈顶的所有 open upvalue"(也就是本函数建的那些)。`CLOSEKTOP` 是状态码,表示这是正常返回路径的关闭;最后的 `1` 表示这个过程允许 yield(协程场景)。

这就是 counter 返回时关闭 upvalue 的真正路径:**不是某个外部调度函数来关,而是 `OP_RETURN` 指令自己,凭编译期设的 `k` 位,在返回前主动关。** 这个设计把"是否需要关 upvalue"的判断推回了编译期(编译器知道 `needclose`),运行期只用一条 `if (TESTARG_k(i))` 就分流——不需要关的函数(`RETURN0`/`RETURN1` 不改写)连这个 `if` 都不进,返回路径零开销。这又是"把代价集中到真正需要的场合"的体现。

尾调用走的是另一条相近的路:`OP_TAILCALL`(`lvm.c:1737`)里同样 `if (TESTARG_k(i))`,但它调的是 `luaF_closeupval(L, base)`(`lvm.c:1750`)——只关 upvalue,不关 to-be-closed(紧接着的断言 `L->tbclist.p < base` 保证尾调用的函数没有 to-be-closed 变量,这是尾调用的语义前提)。

除了返回,还有几个场合也会关 upvalue:`ldo.c:811` 协程出错回卷时 `luaF_close(L, func, status, 1)`、`ldo.c:1049`/`ldo.c:1065` 显式关闭变量(`lua_close`,走受保护模式,经 `closepaux`→`luaD_rawrunprotected`)时、以及 `lstate.c:302` 线程销毁时 `luaF_closeupval(L1, L1->stack.p)` 关掉所有 upvalue。所有"某段栈要失效"的场合,都得先把它上面的 open upvalue 关掉,这是硬规则。

### 2.10 把 counter 走一遍

把上面的零件串起来,走一遍开头的 counter 例子。

**编译期**(P2-08):编译 `counter` 时,编译器看到内层 `function() ... end` 引用了外层局部变量 `n`。它在内层函数的 `Proto.upvalues[0]` 里登记:`instack=1`(引用的是当前外层栈上的局部变量)、`idx=<n 的寄存器号>`、`name="n"`。

**运行期·调用 `counter()`**:进入 `counter` 的调用帧,`n` 落在它的某个栈槽(设为 `base+k`)。执行到 `return function() ... end` 时,VM 执行 `OP_CLOSURE`(`lvm.c:1929`),它调 `pushclosure`(`lvm.c:834`):

- `luaF_newLclosure` 创建一个 `LClosure`,挂到结果寄存器。
- 填 upvalue 循环:`uv[0].instack == 1`,所以调 `luaF_findupval(L, base + k)`(`lfunc.c:87`)。链表里还没有指向 `base+k` 的 open upvalue,于是 `newupval`(`lfunc.c:65`)建一个,设 `v.p = s2v(base+k)`(指向 `n` 的栈槽,即 open 态),插进 `L->openupval` 链表,返回。

此时内层闭包的 `upvals[0]` 指向一个 open upvalue,后者又指向 `counter` 栈上的 `n`。`counter` 把这个闭包作为返回值交出去。

**运行期·`counter` 返回**:执行到 `OP_RETURN`(`lvm.c:1763`)。因为编译期 `counter` 的 `needclose` 为真,这条指令的 `k` 位是 1,于是 `if (TESTARG_k(i))` 成立,调 `luaF_close(L, base, CLOSEKTOP, 1)`(`lvm.c:1774`),进而 `luaF_closeupval(L, level)`(`lfunc.c:197`)。循环发现链表头那个 open upvalue 的 `uplevel >= level`,于是关闭它:

- `luaF_unlinkupval` 摘链。
- `setobj(L, &uv->u.value, uv->v.p)`:把栈上 `n` 的当前值(0)拷进 `uv->u.value`。
- `uv->v.p = &uv->u.value`:转 closed。

从此这个 upvalue 不再指向 `counter` 的栈(那块栈马上要被 `c()` 的新调用帧覆盖),而是指向自己肚子里的 `u.value`,里面存着 0。`counter` 的调用帧撤销,但 `n` 的值已经被"救"进了 upvalue。

**运行期·第一次 `c()`**:进入内层闭包的调用帧。函数体 `n = n + 1` 编译成大致:`GETUPVAL R0 0`(把 upvalue[0] 的值读到 R0)、`ADDI R0 R0 1`、`SETUPVAL R0 0`(把 R0 写回 upvalue[0])、`RETURN1 R0`。

- `GETUPVAL`(`lvm.c:1287`):`cl->upvals[0]->v.p` 现在 closed 态,指向 `u.value`,读到 0。
- `SETUPVAL`(`lvm.c:1293`):把 1 写回 `cl->upvals[0]->v.p`,即写进 `u.value`。下次再调,读到 1,加成 2。

如此三次,`n` 在 closed upvalue 里从 0 累加到 3。整个过程里 open/closed 的转换对外完全透明,程序员只看到"n 还在、还在涨"。

---

## 三、为什么这样设计是 sound 的

### 3.1 open→closed 转换保证不悬空

最核心的正确性问题:外层函数返回、栈帧撤销后,内层闭包里的 upvalue 凭什么不悬空?

答案就在 `luaF_closeupval` 的那一步 `setobj(L, slot, uv->v.p)` + `uv->v.p = slot`。在栈帧撤销**之前**(`OP_RETURN` 指令 `lvm.c:1774` 先 `luaF_close` 关 upvalue、`luaD_poscall` 才真正退栈),VM 把所有指向即将失效栈槽的 open upvalue 逐一关闭:值从栈拷进 upvalue 自身的 `u.value`,`v.p` 改指自身。从此 upvalue 不再引用任何栈内存,它自己是个独立的 GC 对象,挂在持有它的闭包上(闭包又挂在调用它的栈或更上层的闭包/全局上)。栈怎么被覆盖都和它无关。

反过来,如果不做这个转换,upvalue 还指着那块栈,`counter` 一返回、栈被下一个调用复用,`n` 的旧位置就被新调用的局部变量盖掉了——`c()` 再读 upvalue,读到的是别的函数的局部变量,语义彻底崩坏。open→closed 转换是这个语义崩塌的唯一防线。

### 3.2 findupval 复用保证"同一栈槽一个 upvalue"

`luaF_findupval` 的复用逻辑(`lfunc.c:91` 的 `if (uplevel(p) == level) return p`)保证:对同一个栈槽,无论多少个内层闭包引用它,全局只有一个 open upvalue 对象。

这个保证的必要性体现在 SETUPVAL 的语义上。counter 的变种:

```lua
local function make()
  local n = 0
  local function get() return n end
  local function inc() n = n + 1 end
  return get, inc
end
local g, i = make()
print(g())  -- 0
i()
print(g())  -- 1
```

`get` 和 `inc` 都引用 `n`。如果它们各持有一个独立的 upvalue 对象,`inc` 改自己的那份,`get` 看不到——语义就错了。正因为 findupval 复用,`get` 和 `inc` 的 `upvals[0]` 指向**同一个** `UpVal` 对象(都指向 `n` 的栈槽),`inc` 的 SETUPVAL 改的就是 `get` 的 GETUPVAL 读的那个值。counter 能累加、get/inc 能配合,根都在这里。

### 3.3 链表有序保证关闭的高效与正确

open upvalue 链表按栈地址降序排,这不是随便选的,是 `luaF_closeupval` 正确工作的前提。

`luaF_closeupval` 从链表头开始,只要 `uplevel(uv) >= level` 就关。因为降序,链表头总是指向最高(最靠近栈顶)的栈槽。函数返回时,要关闭的是"从某个 `level` 到栈顶"这段栈上的所有 upvalue——正好是链表头部连续的一段。降序保证这段在链表里是连续的前缀,循环一次扫完,不必遍历整条链。

如果链表无序,关闭操作就得遍历整条链找所有 `>= level` 的,复杂度退化;更糟的是,关闭过程中要摘链、要维护 `previous`/`next`,无序链表上的批量摘除既慢又容易出错。降序把关闭变成"从头摘到某点为止"的线性扫描,O(关闭数量)。

### 3.4 offset union 保证栈搬家时不失效

前面提过,5.5 的 `UpVal.v` 是 `union { TValue *p; ptrdiff_t offset; }`。这个 `offset` 字段在栈扩容的瞬间承担"指针不失效"的任务。

Lua 的值栈会动态扩容(`luaD_reallocstack`),`realloc` 可能把整块栈搬到新地址。open upvalue 的 `v.p` 指向旧栈上的某个槽,搬家后这个地址就错了。5.5 的做法:搬家前,遍历 open upvalue 链表,把每个 `v.p` 换算成 `v.offset`(相对于栈基地址的偏移,是个整数,不受搬家影响);搬完,再遍历一次,把 `v.offset` 换算回基于新栈基地址的 `v.p`。两次遍历,O(open upvalue 数量),远比老版本"修指针"的做法干净。

这个机制和 `lua_State` 里的 `StkIdRel`(`top`/`stack`/`tbclist` 等)同源:都是"把绝对指针改成相对偏移,搬家时只动基地址"。Lua 5.5 把这套相对表示推广到了所有"可能因栈搬家而失效的栈指针"上——`StkIdRel` 管线程自己的栈指针,`UpVal.v.offset` 管 upvalue 的栈指针。一处设计思路,两处落地,这是"统一与精简"在内存管理上的又一个实例。

**5.5 vs 老资料硬差异**:讲 5.3/5.4 的资料里,`UpVal` 的 `v` 字段就是一个单纯的 `TValue *p`,没有 `offset` union 成员,栈搬家靠别的补救手段。本书以 5.5.0 源码为准,`offset` union 是 5.5 的演进,和 P1-02 讲的 `StkIdRel` 是同一波改动。

### 3.5 GC 不漏:twups 链表与 closed 染黑

upvalue 是 GC 对象(`LUA_VUPVAL`),它的值可达性分两态:

- **open 态**:值在别的线程(或本线程)的栈槽上。栈是 GC 的根,GC 扫栈时能扫到这个值——但前提是 GC 知道这个 upvalue 存在、且它指向哪里。open upvalue 的 `v.p` 指向栈,GC 扫栈本来就会扫到那个值,**所以 open upvalue 本身不需要额外持有值的可达性**。但 upvalue 对象自己得被 GC 追踪(它挂在闭包上,闭包扫到它)。难点是:GC 的某些阶段需要知道"哪些线程有 open upvalue",才能正确处理——这就是 `twups` 链表(`newupval` 里 `if (!isintwups(L))` 那段)的作用:有线程一旦有了 open upvalue,就挂到 `global_State.twups` 上,GC 遍历这个链表就能找到所有 open upvalue,不漏。
- **closed 态**:值在 `u.value` 里,不在栈上。这时 upvalue 是这个值的唯一持有者之一。`luaF_closeupval` 关闭时把 upvalue 染黑(`nw2black`)并加屏障,保证 GC 把它当作确定的持有者,值不会被漏标。

两态下 GC 都不漏,这是 upvalue 机制和增量 GC 共存的基础。如果 open upvalue 不挂 twups、或 closed 不染黑,都可能让一个还被闭包引用的值被 GC 回收——下次 GETUPVAL 读到的是已回收内存,崩。

---

## 四、★对照 CPython + 回扣主线

CPython 同样支持闭包,同样有"内层函数引用外层局部变量、外层返回后引用不失效"的需求。它的解法和 Lua 形成鲜明对照。

### 4.1 CPython 的 cell 对象

Python 里,被内层函数引用的外层局部变量,在编译期就被识别出来,放进代码对象的 `co_cellvars`(被内层引用的局部变量名集合)和 `co_freevars`(本函数引用的外层变量名集合)。运行期,这些变量**不直接存在帧的 `fast locals` 数组里,而是存在一个 `cell` 对象里**,帧数组里放的是对 cell 的引用。

`cell` 对象(Python 3.12 的 `PyCellObject`)大致是:

```c
struct PyCellObject {
    PyObject_HEAD
    PyObject *ob_ref;   /* 指向真正的值 */
    ...
};
```

读写被捕获的变量,总是经过一层 `cell.ob_ref` 的间接:`LOAD_DEREF`/`STORE_DEREF` 指令操作的是 cell,不是直接操作值。外层函数返回时,**不需要**像 Lua 那样做 open→closed 转换——因为 cell 对象从一开始就是个独立的堆对象,外层帧的 fast locals 数组里放的是 cell 的引用,帧撤销时撤销的是这个引用,cell 本身(只要还被内层闭包引用着)继续存活,`ob_ref` 里的值不动。

### 4.2 两种路线的取舍

两种做法都能让"外层返回后引用不失效",但走的路完全不同:

| 维度 | Lua 5.5 | CPython |
|---|---|---|
| **被捕获变量的存储** | open 态直接在外层**栈槽**上,closed 才搬进 upvalue 自身 | 一律在堆上的 `cell` 对象里,栈帧只存 cell 引用 |
| **访问代价** | open 态 GETUPVAL 一次解引用(`*v.p` 直达栈) | 总是多一层间接(先取 cell,再取 `ob_ref`) |
| **外层返回的处理** | 必须在返回时 `luaF_close` 把值搬进 upvalue | 不需要,cell 本来就在堆上 |
| **同一变量多引用者** | findupval 复用,共享同一个 `UpVal` | 编译期多个 cell 引用指向同一个 cell 对象,天然共享 |
| **栈搬家** | 5.5 用 `offset` union 抗搬家 | Python 帧的 fast locals 是堆上数组,无栈搬家问题 |

Lua 的路线用一个"延迟到返回才搬"的设计,换取了 **open 态下访问的极简**——直接读栈槽,一次解引用,连 cell 的间接都省了。被捕获的变量在它"还活着"的大部分时间里(外层函数还没返回),就和普通局部变量一样住在栈上,访问代价和普通局部变量无异。只有在外层返回的那一刻,才付出一次拷贝的代价把它搬进 upvalue。

这是 Lua 主线"用精简换快"的一个微观体现:**不为还没发生的事提前付代价**。Python 的 cell 一开始就上堆,换来的是实现简单(没有 open/closed 两态、没有 close 逻辑、没有栈搬家问题),代价是被捕获变量访问永远多一层间接。Lua 选择把复杂度压在"返回那一刻的关闭逻辑"上,换取平时访问的快——这和它"寄存器式换指令少""增量 GC 换不卡宿主"是同一种取舍哲学:**把代价集中到少数关键点,让常见路径尽量短。**

两种路线的对照,也折射出两个 VM 对"栈"的不同定位。Lua 的值栈是一个紧密的、会扩容搬家的、和调用帧高度耦合的结构,upvalue 必须想办法和它共存(开合两态 + offset union)。CPython 的帧 fast locals 是堆上的数组,天然适合放 cell 引用,不必区分开合。Lua 的"省"和"紧"逼出了 upvalue 的开合机制,CPython 的"松"和"堆优先"则用 cell 直接消解了这个问题——两个 VM 的整体气质,在这个小机制上看得一清二楚。

### 4.3 回扣主线

闭包和 upvalue 看似只是个语言特性,实则集中体现了 Lua 的几条主线:

- **精简**:`UpVal` 用两个 union(`v` 和 `u`)一物多用——`v` 在 open/closed/搬家三态下分别装栈指针/自身指针/偏移;`u` 在 open/closed 两态下分别装链表结点/值。一个不到 40 字节的小结构,扛下了词法捕获的全部机制。
- **快**:open 态 GETUPVAL 一次解引用直达栈槽,被捕获变量在它活跃的大部分时间里和普通局部变量一样快;只有返回那一刻付一次拷贝。
- **统一**:两类闭包(`LClosure`/`CClosure`)并进一个 `union Closure`,一套 GC、一套 TValue 表示服务两者;`UpVal.v.offset` 和 `StkIdRel` 同源,一套相对表示思路管所有"栈搬家失效"问题。
- **编译期登记、运行期连**:`Upvaldesc` 的 `instack`/`idx` 把 P2-08 的静态分析和本章的运行期连线缝起来——这正是 Lua"编译侧 ↔ 执行侧"二分法在一个具体机制上的交汇。

upvalue 的开合,是 Lua 用一个精巧的小结构同时满足"一等公民函数"和"不悬空引用"的解。它不靠把一切堆化(Python 的路),而靠"平时住栈、返回时搬"的延迟策略,把快和正确同时拿下。这是"统一与精简换小而快"在词法捕获上的具体落地。

---

*下一章 [P4-15 尾调用、可变参数与 to-be-closed](P4-15-尾调用可变参数与to-be-closed.md):讲尾调用怎么复用当前调用帧、可变参数 `...` 怎么传递,以及 `to-be-closed` 变量——后者正是本章 `luaF_close` 里那个 `tbclist` 循环要做的事。*
