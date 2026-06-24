# P6-19 元表与元方法:Table 如何充当对象

> **本书主线**:统一与精简换小而快。**二分法**:编译侧(源码→字节码)↔ 执行侧(VM 执行 + 管值生命周期)。**★对照**:CPython。**源码**:lua-5.5.0。**基调**:纯直球,不用比喻。
>
> **本章落点**:"统一"主线的延伸——P0-01 讲过 Lua 只有一个复合结构 Table,但 Table 本身没有任何"类型行为":它不会做加法、不认识 `obj.field` 该回退到哪、不知道被当作函数调用时怎么办。这些行为全靠挂一张**元表**(metatable)、往里塞**元方法**(metamethod)获得。这就是"一个 Table 充当对象/类"的机制根源。**二分法位置**:执行侧(P6),元表/协程/C API 三件套之一。**★对照**:CPython 的 `class`/`type` 类型系统与双下划线方法。

---

## 一、这章解决什么问题

先回顾 P0-01 给出的 `Table` 定义(`lobject.h:776`):

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

注意 `metatable` 字段:它指向**另一张 Table**。这张"附属表"里装的不是数据,而是一组约定好名字的键值对——`__index`、`__add`、`__eq`、`__call` 等。VM 在某些"Table 自己处理不了"的时刻,会去翻这张附属表,看有没有对应的处理函数。

这一章要回答三个问题:

1. **Lua 没有 class,面向对象和运算符重载靠什么?** 答案是 metatable + metamethod。一个普通 Table 挂上 metatable 后,就"获得"了 `__index`(字段回退,即继承)、`__add`(加法重载)、`__call`(当函数调)、`__eq`(相等比较)等一系列行为。**类型不是语言内建的概念,而是一张约定好名字的 Table。**

2. **一个 Table 怎么变成"对象"?** Lua 的 OOP 范式是:`setmetatable(obj, {__index = Class})`。读 `obj.foo` 命中失败时,VM 沿 `__index` 找到 `Class.foo`——这就是原型链。再加一层 `__index = Base`,就形成继承。整个 OOP 语义建立在"读字段失败时回退查父表"这条规则上。

3. **这套机制为什么不会慢?** 答案是 `flags` 缓存。每次读字段都要先看 metatable 里有没有 `__index` 太贵了,Lua 用 `Table.flags` 这个 `lu_byte` 位图记住"这张表没有哪些元方法",下次直接跳过查找。

这一章的目标:把 metatable 从挂载(TMS 枚举、`setmetatable`)到触发(`luaV_finishget`/`finishset`、`OP_MMBIN`、`tryfuncTM`)再到缓存(`flags`/`fasttm`/`invalidateTMcache`)这一整条链走通,看清"Table 充当对象"在源码层面的全部代价。

---

## 二、源码怎么实现

### 2.1 元方法的全部清单:TMS 枚举

所有元方法的名字硬编码在一个枚举里(`ltm.h:18`):

```c
typedef enum {
  TM_INDEX,
  TM_NEWINDEX,
  TM_GC,
  TM_MODE,
  TM_LEN,
  TM_EQ,  /* last tag method with fast access */
  TM_ADD,
  TM_SUB,
  TM_MUL,
  TM_MOD,
  TM_POW,
  TM_DIV,
  TM_IDIV,
  TM_BAND,
  TM_BOR,
  TM_BXOR,
  TM_SHL,
  TM_SHR,
  TM_UNM,
  TM_BNOT,
  TM_LT,
  TM_LE,
  TM_CONCAT,
  TM_CALL,
  TM_CLOSE,
  TM_N		/* number of elements in the enum */
} TMS;
```

对应的字符串名字在 `luaT_init`(`ltm.c:38`)里登记到全局状态:

```c
void luaT_init (lua_State *L) {
  static const char *const luaT_eventname[] = {  /* ORDER TM */
    "__index", "__newindex",
    "__gc", "__mode", "__len", "__eq",
    "__add", "__sub", "__mul", "__mod", "__pow",
    "__div", "__idiv",
    "__band", "__bor", "__bxor", "__shl", "__shr",
    "__unm", "__bnot", "__lt", "__le",
    "__concat", "__call", "__close"
  };
  int i;
  for (i=0; i<TM_N; i++) {
    G(L)->tmname[i] = luaS_new(L, luaT_eventname[i]);
    luaC_fix(L, obj2gco(G(L)->tmname[i]));  /* never collect these names */
  }
}
```

注意枚举顺序和字符串数组顺序**必须严格一致**(`ORDER TM` 注释)。`G(L)->tmname[i]` 是 `global_State` 里的字符串数组(`lstate.h:366`),把这些 `__xxx` 名字驻留成短串(GC 永不回收),这样 VM 查元方法时直接用驻留串去匹配哈希表,不必每次构造字符串。

**这里必须做一个 5.5 vs 老资料的修正**:枚举里**只有 24 个元方法**(TM_INDEX 到 TM_CLOSE),`TM_N = 25`(含哨兵)。很多 Lua 教程和讲 5.3/5.4 的资料会提到 `__tostring`、`__pairs`、`__ipairs` 这些"元方法"——**它们在 5.5 的 TMS 枚举里根本不存在**。原因:

- `__tostring`:VM 层没有"转字符串"这个元方法。`tostring()` 是标准库函数(`lstrlib.c`),它内部用 `luaL_getmetafield` 去查 `__tostring` 字段(只是个普通字段查找,不是 VM 元方法)。`type()` 显示的类型名来自 `__name` 字段(见 `luaT_objtypename`,`ltm.c:91`),那也是普通 metafield,不是元方法。
- `__pairs`/`__ipairs`:在 `lbaselib.c:287` 用 `luaL_getmetafield(L, 1, "__pairs")` 查,纯库层实现,generic for 的 `OP_TFORPREP`/`OP_TFORCALL` 不认识它。

**判断一个机制是不是"VM 元方法"的唯一标准:它在不在这个 TMS 枚举里。** 不在的就是"约定字段",由库函数或宿主代码解释。这个区分很重要:VM 元方法由解释器循环在特定指令失败时自动触发,约定字段则要程序主动去查。下表汇总 5.5.0 真实的 24 个元方法:

| 元方法 | 触发场景 | 触发指令/路径 |
|---|---|---|
| `__index` | `t[k]` 且 k 不在 t 里 | `GETTABLE` 等 → `luaV_finishget` |
| `__newindex` | `t[k] = v` 且 k 不在 t 里 | `SETTABLE` 等 → `luaV_finishset` |
| `__gc` | 对象被 GC 回收 | `lgc.c` `GCTM` |
| `__mode` | 弱表(键/值的弱性) | GC 遍历弱表时读 |
| `__len` | `#t` | `OP_LEN` → `luaV_objlen` |
| `__eq` | `==`(同类型非原始相等) | `OP_EQ` → `luaV_equalobj` |
| `__add` ~ `__idiv` | 算术 `+ - * / // % ^` | `OP_ADD` 等 → `OP_MMBIN` |
| `__band` ~ `__shr` | 位运算 `& \| ~ << >>` | `OP_BAND` 等 → `OP_MMBIN` |
| `__unm` | 一元负 `-t` | `OP_UNM` |
| `__bnot` | 按位反 `~t` | `OP_BNOT` |
| `__lt` `__le` | `<` `<=` | `OP_LT`/`OP_LE` |
| `__concat` | `..` | `OP_CONCAT` → `luaV_concat` |
| `__call` | `t(...)` | `tryfuncTM`(`ldo.c:523`) |
| `__close` | to-be-closed 变量退出作用域 | `OP_CLOSE`/`luaF_close` |

### 2.2 metatable 怎么挂:setmetatable / getmetatable

C API 暴露两个函数。`lua_setmetatable`(`lapi.c:964`)从栈顶取一张 Table 当元表,挂到目标对象上:

```c
LUA_API int lua_setmetatable (lua_State *L, int objindex) {
  TValue *obj;
  Table *mt;
  lua_lock(L);
  api_checkpop(L, 1);
  obj = index2value(L, objindex);
  if (ttisnil(s2v(L->top.p - 1)))
    mt = NULL;
  else {
    api_check(L, ttistable(s2v(L->top.p - 1)), "table expected");
    mt = hvalue(s2v(L->top.p - 1));
  }
  switch (ttype(obj)) {
    case LUA_TTABLE: {
      hvalue(obj)->metatable = mt;
      if (mt) {
        luaC_objbarrier(L, gcvalue(obj), mt);
        luaC_checkfinalizer(L, gcvalue(obj), mt);
      }
      break;
    }
    case LUA_TUSERDATA: {
      uvalue(obj)->metatable = mt;
      if (mt) {
        luaC_objbarrier(L, uvalue(obj), mt);
        luaC_checkfinalizer(L, gcvalue(obj), mt);
      }
      break;
    }
    default: {
      G(L)->mt[ttype(obj)] = mt;
      break;
    }
  }
  L->top.p--;
  lua_unlock(L);
  return 1;
}
```

三个分支对应三类挂载对象:

- **Table**(`LUA_TTABLE`):直接写 `hvalue(obj)->metatable = mt`,就是 `Table` 结构里那个字段。
- **full userdata**(`LUA_TUSERDATA`):写 `Udata` 结构里的 `metatable` 字段(userdata 也有这个字段,这是 Lua 给 C 扩展做"带类型的对象"的入口)。
- **基本类型**(number/string/boolean/thread/function):没有专门字段,挂在全局数组 `G(L)->mt[ttype(obj)]`(`lstate.h:367`)里——所有同类型对象共享同一张元表。

两个细节值得注意:

1. `luaC_objbarrier`:写完元表后插一条 GC 屏障。因为 metatable 是 GC 对象,挂上去后它的可达性变了(本来在栈上,现在被另一个对象引用),必须告诉 GC 维护三色不变式(P5-16 详讲屏障)。
2. `luaC_checkfinalizer`:挂 metatable 时检查新元表里有没有 `__gc`。如果有,且这个对象之前没被标记为"有 finalizer",要把对象立刻登记进 finalizer 队列——否则已存在的对象将来回收时不会调 `__gc`。这是 `__gc` 元方法的一个特殊语义:它必须在对象挂 metatable 的那一刻就登记,而不是等回收时才发现。

`lua_getmetatable`(`lapi.c:805`)是逆操作,把对象的 metatable 压栈:

```c
LUA_API int lua_getmetatable (lua_State *L, int objindex) {
  const TValue *obj;
  Table *mt;
  int res = 0;
  lua_lock(L);
  obj = index2value(L, objindex);
  switch (ttype(obj)) {
    case LUA_TTABLE:
      mt = hvalue(obj)->metatable;
      break;
    case LUA_TUSERDATA:
      mt = uvalue(obj)->metatable;
      break;
    default:
      mt = G(L)->mt[ttype(obj)];
      break;
  }
  if (mt != NULL) {
    sethvalue2s(L, L->top.p, mt);
    api_incr_top(L);
    res = 1;
  }
  lua_unlock(L);
  return res;
}
```

读 metatable 的逻辑和写完全对称。Lua 层的 `setmetatable(t, mt)` / `getmetatable(t)` 就是这两个 C API 的薄包装。

### 2.3 flags 缓存:避免每次查 metatable

每个 Table 挂了 metatable 后,VM 在很多指令里都要查"这张表有没有某个元方法"。如果每次都去 metatable 里做一次哈希查找,代价不小。Lua 的优化是 `Table.flags` 这个 `lu_byte`。

先看 `ltm.h:54` 的关键定义:

```c
/*
** Mask with 1 in all fast-access methods. A 1 in any of these bits
** in the flag of a (meta)table means the metatable does not have the
** corresponding metamethod field. (Bit 6 of the flag indicates that the
** table is using the dummy node; bit 7 is used for 'isrealasize'.)
*/
#define maskflags	cast_byte(~(~0u << (TM_EQ + 1)))
```

`TM_EQ` 的枚举值是 5,所以 `TM_EQ + 1 = 6`,`maskflags = 0b00111111`(低 6 位)。这意味着 flags 的低 6 位才是元方法缓存位,bit 6 和 bit 7 被挪作他用(dummy node 标志和真实 asize 标志,见 P1-05)。

**只有前 6 个元方法是 fast-access**:`__index`、`__newindex`、`__gc`、`__mode`、`__len`、`__eq`。为什么是这 6 个?因为它们出现频率最高(读字段、写字段、长度、相等比较)。算术、位运算、`__call`、`__concat`、`__close` 不走缓存——它们要么触发频率低,要么本来就要走更复杂的路径。

缓存的核心是两个宏。先看检测(`ltm.h:63`):

```c
#define checknoTM(mt,e)	((mt) == NULL || (mt)->flags & (1u<<(e)))
```

它的语义:`mt == NULL`(没元表)或者 `mt->flags` 的第 e 位是 1(缓存了"这个元方法不存在")。任意一种成立,就认为没有这个元方法。

再看取元方法的快速路径(`ltm.h:65`):

```c
#define gfasttm(g,mt,e)  \
  (checknoTM(mt, e) ? NULL : luaT_gettm(mt, e, (g)->tmname[e]))

#define fasttm(l,mt,e)	gfasttm(G(l), mt, e)
```

逻辑:先看缓存位。缓存说"没有"就直接返回 NULL,根本不进哈希查找。只有缓存没说"没有"时,才真正调 `luaT_gettm` 去查哈希表。`luaT_gettm`(`ltm.c:60`)是慢路径:

```c
const TValue *luaT_gettm (Table *events, TMS event, TString *ename) {
  const TValue *tm = luaH_Hgetshortstr(events, ename);
  lua_assert(event <= TM_EQ);
  if (notm(tm)) {  /* no tag method? */
    events->flags |= cast_byte(1u<<event);  /* cache this fact */
    return NULL;
  }
  else return tm;
}
```

注意 `lua_assert(event <= TM_EQ)`——**这个函数只能被 fast-access 元方法调用**。它做一次哈希查找:如果找到就返回;如果找不到(`notm` 即 nil),**立刻把缓存位置 1**(`events->flags |= 1u<<event`)。下次再查这个元方法,`checknoTM` 就命中,直接返回 NULL。

这套缓存的正确性靠一个关键不变式维持:**只要 metatable 里那个字段还是 nil,缓存位就一直是 1**。一旦有人把字段从 nil 改成有值,必须清缓存。清缓存的动作是 `invalidateTMcache`(`ltable.h:23`):

```c
#define invalidateTMcache(t)	((t)->flags &= cast_byte(~maskflags))
```

把低 6 位全部清零(bit 6/7 保留)。这个宏在两个地方调用:

- `luaV_finishset`(`lvm.c:347`):往一张 Table 写入新值时,如果这张 Table 是某张表的 metatable,新写的字段可能是某个元方法,于是清掉自己(作为 metatable)的缓存位。
- `ltable.c:1112`:rehash 时(哈希表扩容/重排),也清缓存。
- `lapi.c:933`:`lua_settable` 这类 C API 写操作。

特别要强调:**清的是被写的表自己的 flags**,因为任何表都可能被别人当 metatable 用。只要它的内容变了,它作为 metatable 的缓存就可能失效。最保守的做法就是全清——代价是一个字节赋值,远比每次都查哈希便宜。

新表创建时 flags 的初值是 `maskflags`(`ltable.c:803`):

```c
Table *luaH_new (lua_State *L) {
  GCObject *o = luaC_newobj(L, LUA_VTABLE, sizeof(Table));
  Table *t = gco2t(o);
  t->metatable = NULL;
  t->flags = maskflags;  /* table has no metamethod fields */
  ...
}
```

`maskflags = 0b00111111`,即低 6 位全是 1——表示"这张空表没有任何 fast-access 元方法",完全正确(空表当然没有元方法)。这个初值让 `checknoTM` 在没有 metatable 时直接命中 fast path。

### 2.4 核心:__index / __newindex 的触发路径

这是 metamethod 机制的重头戏,也是 OOP 继承的物理基础。先看读路径 `luaV_finishget`(`lvm.c:291`):

```c
lu_byte luaV_finishget (lua_State *L, const TValue *t, TValue *key,
                                      StkId val, lu_byte tag) {
  int loop;  /* counter to avoid infinite loops */
  const TValue *tm;  /* metamethod */
  for (loop = 0; loop < MAXTAGLOOP; loop++) {
    if (tag == LUA_VNOTABLE) {  /* 't' is not a table? */
      lua_assert(!ttistable(t));
      tm = luaT_gettmbyobj(L, t, TM_INDEX);
      if (l_unlikely(notm(tm)))
        luaG_typeerror(L, t, "index");  /* no metamethod */
      /* else will try the metamethod */
    }
    else {  /* 't' is a table */
      tm = fasttm(L, hvalue(t)->metatable, TM_INDEX);  /* table's metamethod */
      if (tm == NULL) {  /* no metamethod? */
        setnilvalue(s2v(val));  /* result is nil */
        return LUA_VNIL;
      }
      /* else will try the metamethod */
    }
    if (ttisfunction(tm)) {  /* is metamethod a function? */
      tag = luaT_callTMres(L, tm, t, key, val);  /* call it */
      return tag;  /* return tag of the result */
    }
    t = tm;  /* else try to access 'tm[key]' */
    luaV_fastget(t, key, s2v(val), luaH_get, tag);
    if (!tagisempty(tag))
      return tag;  /* done */
    /* else repeat (tail call 'luaV_finishget') */
  }
  luaG_runerror(L, "'__index' chain too long; possible loop");
  return 0;  /* to avoid warnings */
}
```

这个函数在 `OP_GETTABLE`/`OP_GETFIELD`/`OP_GETI`/`OP_GETTABUP` 等所有读字段指令的快速路径失败后被调用。看 `OP_GETTABLE` 的主体(`lvm.c:1311`):

```c
vmcase(OP_GETTABLE) {
  StkId ra = RA(i);
  TValue *rb = vRB(i);
  TValue *rc = vRC(i);
  lu_byte tag;
  if (ttisinteger(rc)) {  /* fast track for integers? */
    luaV_fastgeti(rb, ivalue(rc), s2v(ra), tag);
  }
  else
    luaV_fastget(rb, rc, s2v(ra), luaH_get, tag);
  if (tagisempty(tag))
    Protect(luaV_finishget(L, rb, rc, ra, tag));
  vmbreak;
}
```

先走 `luaV_fastget`(`lvm.h:81`):

```c
#define luaV_fastget(t,k,res,f, tag) \
  (tag = (!ttistable(t) ? LUA_VNOTABLE : f(hvalue(t), k, res)))
```

如果 `t` 不是 Table,返回 `LUA_VNOTABLE`(直接交给 finishget 处理非 Table 的元方法);如果是 Table,调 `luaH_get`/`luaH_getshortstr` 做真正的哈希查找,返回一个 tag 表示命中、空、还是边界外。只有 `tagisempty(tag)`(key 不在表里)才进 `luaV_finishget`。

`luaV_finishget` 的两条分支对应 `__index` 的两种形态:

**分支 A:`t` 是 Table,但 key 不在里面。** 用 `fasttm` 查 `t` 的 metatable 有没有 `__index`。注意这里用的是 `fasttm`(走 flags 缓存),不是 `luaT_gettmbyobj`。如果没有 `__index`,直接返回 nil(`setnilvalue`),Lua 语义里读一个不存在的字段就是 nil。如果有 `__index`,继续往下。

**分支 B:`t` 不是 Table**(比如是 userdata 或基本类型)。调 `luaT_gettmbyobj`(`ltm.c:71`):

```c
const TValue *luaT_gettmbyobj (lua_State *L, const TValue *o, TMS event) {
  Table *mt;
  switch (ttype(o)) {
    case LUA_TTABLE:
      mt = hvalue(o)->metatable;
      break;
    case LUA_TUSERDATA:
      mt = uvalue(o)->metatable;
      break;
    default:
      mt = G(L)->mt[ttype(o)];
  }
  return (mt ? luaH_Hgetshortstr(mt, G(L)->tmname[event]) : &G(L)->nilvalue);
}
```

按对象类型取 metatable(Table/userdata/基本类型三路),然后在 metatable 里查 `__index` 字段。注意这里**不走 flags 缓存**——因为基本类型/userdata 的元方法查找频率低,不值得缓存;而且 `luaT_gettmbyobj` 是通用入口,缓存逻辑只在 `fasttm` 那条路径上。

拿到 `__index` 之后,看它是什么:

- **是函数**:调 `luaT_callTMres(L, tm, t, key, val)`,即 `tm(t, key)`,结果放回 `val`。这是 `__index = function(t, k) ... end` 的形态。
- **是 Table**:`t = tm`,把目标换成这张 `__index` 表,再 `luaV_fastget` 查一次。这就是 `__index = parent_table` 的形态——**原型链/继承就是靠这个递归查询实现的**。

循环用 `MAXTAGLOOP`(lvm.c:50 定义为 2000)兜底,防止 `__index` 链成环(比如 `a.__index = b, b.__index = a`)导致死循环。超过 2000 次就 `luaG_runerror`。

**OOP 继承示例**。Lua 代码:

```lua
local Animal = {legs = 4}
local Dog = {}
setmetatable(Dog, {__index = Animal})
print(Dog.legs)   -- 4
```

执行 `Dog.legs` 编译成 `OP_GETFIELD`(字段名是常量短串),fast path 查 `Dog` 表找不到 `legs` 字段(tag empty),进 `luaV_finishget`。`Dog` 是 Table,`fasttm` 查它的 metatable(刚 setmetatable 挂上的那张表)的 `__index`,找到 `Animal` 表。`Animal` 不是函数是 Table,于是 `t = Animal`,再 `luaV_fastget` 查 `Animal.legs`,命中返回 4。整条链路一次函数调用都没发生,全是哈希查找。多级继承就是 `__index` 指向另一张带 `__index` 的表,递归下去。

写路径 `luaV_finishset`(`lvm.c:334`)结构对称:

```c
void luaV_finishset (lua_State *L, const TValue *t, TValue *key,
                      TValue *val, int hres) {
  int loop;  /* counter to avoid infinite loops */
  for (loop = 0; loop < MAXTAGLOOP; loop++) {
    const TValue *tm;  /* '__newindex' metamethod */
    if (hres != HNOTATABLE) {  /* is 't' a table? */
      Table *h = hvalue(t);  /* save 't' table */
      tm = fasttm(L, h->metatable, TM_NEWINDEX);  /* get metamethod */
      if (tm == NULL) {  /* no metamethod? */
        sethvalue2s(L, L->top.p, h);  /* anchor 't' */
        L->top.p++;  /* assume EXTRA_STACK */
        luaH_finishset(L, h, key, val, hres);  /* set new value */
        L->top.p--;
        invalidateTMcache(h);
        luaC_barrierback(L, obj2gco(h), val);
        return;
      }
      /* else will try the metamethod */
    }
    else {  /* not a table; check metamethod */
      tm = luaT_gettmbyobj(L, t, TM_NEWINDEX);
      if (l_unlikely(notm(tm)))
        luaG_typeerror(L, t, "index");
    }
    /* try the metamethod */
    if (ttisfunction(tm)) {
      luaT_callTM(L, tm, t, key, val);
      return;
    }
    t = tm;  /* else repeat assignment over 'tm' */
    luaV_fastset(t, key, val, hres, luaH_pset);
    if (hres == HOK) {
      luaV_finishfastset(L, t, val);
      return;  /* done */
    }
    /* else 'return luaV_finishset(L, t, key, val, slot)' (loop) */
  }
  luaG_runerror(L, "'__newindex' chain too long; possible loop");
}
```

关键差异在"是 Table 且没有 `__newindex`"这条分支:这是最常见的写场景(普通赋值),它真正执行写入 `luaH_finishset`,写完后**调 `invalidateTMcache(h)` 清掉自己的 flags 缓存**——因为新写入的字段可能恰好是某个元方法名(虽然概率低,但必须保守清掉)。还要 `luaC_barrierback` 通知 GC:表里新引用了一个对象,如果那个对象是黑色的且本表也是黑色,需要把表退回灰色重扫(P5-16)。

注意源码注释特别提到一个细节:**写之前先把 `h` 锚定到栈顶**(`sethvalue2s(L, L->top.p, h)`)。原因写在函数头注释里:如果 loop > 0(即沿 `__newindex` 链下到某张 metatable 里的表),`luaH_finishset` 可能触发紧急 GC,而这张表可能处于一张弱 metatable 里、本身没被别处强引用,GC 可能在它正被更新时把它回收掉。锚定到栈顶就是给它一个临时的强引用,防止这个窗口。

### 2.5 算术元方法:OP_MMBIN 三件套

算术指令(`OP_ADD`/`OP_SUB`/...)的快速路径是纯整数或浮点直接算(P3-11/12 见过)。看 `OP_ADD`(`lvm.c:1506`):

```c
vmcase(OP_ADD) {
  op_arith(L, l_addi, luai_numadd);
  vmbreak;
}
```

`op_arith`(`lvm.c:1004`)展开:

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

#define op_arithf_aux(L,v1,v2,fop) {  \
  lua_Number n1; lua_Number n2;  \
  if (tonumberns(v1, n1) && tonumberns(v2, n2)) {  \
    StkId ra = RA(i);  \
    pc++; setfltvalue(s2v(ra), fop(L, n1, n2));  \
  }}
```

两级快速路径:两个整数直接 `intop(+, i1, i2)`(一条机器指令加法,`pc++` 跳过下一条 MMBIN);否则尝试两个浮点数直接算。**只有两条快速路径都失败**(操作数里有非数字,比如 Table),才不写结果,`pc` 不自增,继续执行下一条指令——那条指令就是 `OP_MMBIN`。

这是 Lua 寄存器式 VM 的一个经典套路:**算术指令和元方法指令成对发射**。编译器为每个算术操作生成两条指令:第一条 `OP_ADD` 试快速路径,第二条 `OP_MMBIN` 兜底。快速路径成功就 `pc++` 跳过 MMBIN;失败就顺序执行到 MMBIN,走元方法。

看 `OP_MMBIN`(`lvm.c:1556`):

```c
vmcase(OP_MMBIN) {
  StkId ra = RA(i);
  Instruction pi = *(pc - 2);  /* original arith. expression */
  TValue *rb = vRB(i);
  TMS tm = (TMS)GETARG_C(i);
  StkId result = RA(pi);
  lua_assert(OP_ADD <= GET_OPCODE(pi) && GET_OPCODE(pi) <= OP_SHR);
  Protect(luaT_trybinTM(L, s2v(ra), rb, result, tm));
  vmbreak;
}
```

注意 `pi = *(pc - 2)`:回看两条指令之前的那个算术指令(因为 ADD/MMBIN 之间还隔着一条……实际上 5.5 的布局是 ADD 紧跟 MMBIN,`pc - 2` 是因为 ADD 执行后 pc 已经前进到 MMBIN,再 -1 到 ADD;但实际 5.5 ADD 后 `pc` 指向 MMBIN,`pc-2` 是 ADD 之前的指令位置——精确细节以源码为准)。`GETARG_C(i)` 取出 MMBIN 的 C 操作数,那是个 TMS 枚举值,标识这是哪个算术元方法(`TM_ADD`/`TM_SUB`/...)。`result = RA(pi)` 算出原算术指令的目标寄存器,元方法的结果要写到这里。

然后调 `luaT_trybinTM`(`ltm.c:150`):

```c
void luaT_trybinTM (lua_State *L, const TValue *p1, const TValue *p2,
                    StkId res, TMS event) {
  if (l_unlikely(callbinTM(L, p1, p2, res, event) < 0)) {
    switch (event) {
      case TM_BAND: case TM_BOR: case TM_BXOR:
      case TM_SHL: case TM_SHR: case TM_BNOT: {
        if (ttisnumber(p1) && ttisnumber(p2))
          luaG_tointerror(L, p1, p2);
        else
          luaG_opinterror(L, p1, p2, "perform bitwise operation on");
      }
      /* calls never return, but to avoid warnings: *//* FALLTHROUGH */
      default:
        luaG_opinterror(L, p1, p2, "perform arithmetic on");
    }
  }
}
```

它先试 `callbinTM`(`ltm.c:138`):

```c
static int callbinTM (lua_State *L, const TValue *p1, const TValue *p2,
                      StkId res, TMS event) {
  const TValue *tm = luaT_gettmbyobj(L, p1, event);  /* try first operand */
  if (notm(tm))
    tm = luaT_gettmbyobj(L, p2, event);  /* try second operand */
  if (notm(tm))
    return -1;  /* tag method not found */
  else  /* call tag method and return the tag of the result */
    return luaT_callTMres(L, tm, p1, p2, res);
}
```

**先查第一个操作数的元方法,没有再查第二个**——这是 Lua 的规则:`a + b` 先看 `a` 有没有 `__add`,没有再看 `b`。两边都没有就返回 -1,由 `luaT_trybinTM` 报错(算术错就报 "perform arithmetic on",位运算错多一道 toint 检查)。

注意算术元方法**不走 flags 缓存**——前面说过 `__add` 等不在 fast-access 范围内。每次都走 `luaT_gettmbyobj` 做完整查找。这是合理的取舍:算术元方法本来就是冷路径(快速路径已经过滤掉 99% 的整数/浮点运算),不值得为它维护缓存位。

`MMBINI`/`MMBINK` 是 MMBIN 的变体,处理操作数是立即数或常量的情况(`lvm.c:1566`/`1576`),多一个 `flip` 参数表示参数顺序是否翻转(比如 `a < 3` 编译成 `MMBINI` 时,3 是立即数,但 `__lt` 的语义要求左边是对象,所以要 flip)。

### 2.6 比较元方法:__eq / __lt / __le

`==` 走 `luaV_equalobj`(`lvm.c` 片段,核心在 600-653):

```c
  /* (节选自 luaV_equalobj) */
  case LUA_VTABLE: {
    if (hvalue(t1) == hvalue(t2)) return 1;       /* 同一表对象直接相等 */
    else if (L == NULL) return 0;
    tm = fasttm(L, hvalue(t1)->metatable, TM_EQ);  /* 查 t1 的 __eq */
    if (tm == NULL)
      tm = fasttm(L, hvalue(t2)->metatable, TM_EQ);  /* 再查 t2 的 __eq */
    break;  /* will try TM */
  }
  ...
  if (tm == NULL)  /* no TM? */
    return 0;  /* objects are different */
  else {
    int tag = luaT_callTMres(L, tm, t1, t2, L->top.p);  /* call TM */
    return !tagisfalse(tag);
  }
```

比较元方法有两层快速路径:

1. **类型不同直接不等**(不同类型永远不会通过 `__eq` 相等,这是 Lua 语义)。
2. **同类型的原始相等**先判:两个 Table 是不是同一个对象(指针相等)、两个数字是不是数值相等、两个字符串是不是同一驻留串。

只有"同类型且原始比较不等"时,才查 `__eq`。这里 `__eq` 是 fast-access 元方法,走 `fasttm`/flags 缓存。结果用 `tagisfalse` 判断——只有 false 和 nil 算"不相等",其他都算相等(和 Lua 的真值规则一致)。

`<` 和 `<=` 类似,走 `luaT_callorderTM`(`ltm.c:200`):

```c
int luaT_callorderTM (lua_State *L, const TValue *p1, const TValue *p2,
                      TMS event) {
  int tag = callbinTM(L, p1, p2, L->top.p, event);  /* try original event */
  if (tag >= 0)  /* found tag method? */
    return !tagisfalse(tag);
  luaG_ordererror(L, p1, p2);  /* no metamethod found */
  return 0;  /* to avoid warnings */
}
```

注意 `__lt` 和 `__le` **不在 fast-access 范围**(枚举里 TM_LT=20、TM_LE=21,远超 TM_EQ=5),所以走 `callbinTM` → `luaT_gettmbyobj` 完整查找。一个有趣的细节:Lua 没有单独的 `__gt`/`__ge`,`a > b` 编译时翻转成 `b < a`,`a >= b` 翻转成 `b <= a`——所以只需要两个比较元方法。

### 2.7 __call:把对象当函数调

`__call` 的触发点不在 lvm.c 的某个 OP_CALL case 里,而在调用进入的入口 `ldo.c:523 tryfuncTM`:

```c
static unsigned tryfuncTM (lua_State *L, StkId func, unsigned status) {
  const TValue *tm;
  StkId p;
  tm = luaT_gettmbyobj(L, s2v(func), TM_CALL);
  if (l_unlikely(ttisnil(tm)))  /* no metamethod? */
    luaG_callerror(L, s2v(func));
  for (p = L->top.p; p > func; p--)  /* open space for metamethod */
    setobjs2s(L, p, p-1);
  L->top.p++;  /* stack space pre-allocated by the caller */
  setobj2s(L, func, tm);  /* metamethod is the new function to be called */
  if ((status & MAX_CCMT) == MAX_CCMT)  /* is counter full? */
    luaG_runerror(L, "'__call' chain too long");
  return status + (1u << CIST_CCMT);  /* increment counter */
}
```

这个函数在 `luaD_precall`(`ldo.c:699`/`742`)里被调,当被调对象不是函数时触发。它做三件事:

1. **查 `__call` 元方法**:`luaT_gettmbyobj(L, s2v(func), TM_CALL)`。注意 `__call` 不在 fast-access 范围,走完整查找。
2. **栈上腾位**:把 `func` 上面的所有参数往后挪一格,腾出的位置放元方法。这样元方法的第一个参数就是原来的"被调对象"本身,后面跟着原参数——`t(a, b)` 变成 `__call(t, a, b)`。
3. **替换被调对象**:`setobj2s(L, func, tm)`,把栈上那个位置从原来的 Table 换成元方法函数。然后正常走 `luaD_precall` 的调用流程。

`MAX_CCMT` 计数器防 `__call` 链成环(元方法返回值又有 `__call` 又触发,理论上能无限套)。和 `__index` 的 `MAXTAGLOOP` 是同一类保护。

### 2.8 luaT_callTM:受保护的元方法调用

元方法的实际调用统一走 `luaT_callTM`(`ltm.c:103`)或 `luaT_callTMres`(`ltm.c:119`):

```c
void luaT_callTM (lua_State *L, const TValue *f, const TValue *p1,
                  const TValue *p2, const TValue *p3) {
  StkId func = L->top.p;
  setobj2s(L, func, f);  /* push function (assume EXTRA_STACK) */
  setobj2s(L, func + 1, p1);  /* 1st argument */
  setobj2s(L, func + 2, p2);  /* 2nd argument */
  setobj2s(L, func + 3, p3);  /* 3rd argument */
  L->top.p = func + 4;
  /* metamethod may yield only when called from Lua code */
  if (isLuacode(L->ci))
    luaD_call(L, func, 0);
  else
    luaD_callnoyield(L, func, 0);
}
```

两个设计要点:

1. **参数手动压栈**:元方法的参数从 VM 内部的值(寄存器里的 TValue)拷到栈顶,再调 `luaD_call` 执行。注释 `assume EXTRA_STACK` 表示这里假定栈有预留空间(P4-13 讲 EXTRA_STACK),不必检查扩容——因为元方法最多 3 个参数,预留空间够用。
2. **是否允许 yield**:如果当前调用帧是 Lua 代码(`isLuacode(L->ci)`),用 `luaD_call`(允许协程 yield);否则用 `luaD_callnoyield`(禁止 yield)。这是 5.4+ 的语义:元方法在 Lua 代码上下文里可以 yield(配合协程),但在 C 函数上下文里不能(因为 C 栈没法挂起)。

### 2.9 __len 和 luaV_objlen

取长度 `#t` 走 `OP_LEN`(`lvm.c:1621`):

```c
vmcase(OP_LEN) {
  StkId ra = RA(i);
  Protect(luaV_objlen(L, ra, vRB(i)));
  vmbreak;
}
```

`luaV_objlen`(`lvm.c:731`):

```c
void luaV_objlen (lua_State *L, StkId ra, const TValue *rb) {
  const TValue *tm;
  switch (ttypetag(rb)) {
    case LUA_VTABLE: {
      Table *h = hvalue(rb);
      tm = fasttm(L, h->metatable, TM_LEN);
      if (tm) break;  /* metamethod? break switch to call it */
      setivalue(s2v(ra), l_castU2S(luaH_getn(L, h)));  /* else primitive len */
      return;
    }
    case LUA_VSHRSTR: {
      setivalue(s2v(ra), tsvalue(rb)->shrlen);
      return;
    }
    case LUA_VLNGSTR: {
      setivalue(s2v(ra), cast_st2S(tsvalue(rb)->u.lnglen));
      return;
    }
    default: {  /* try metamethod */
      tm = luaT_gettmbyobj(L, rb, TM_LEN);
      if (l_unlikely(notm(tm)))  /* no metamethod? */
        luaG_typeerror(L, rb, "get length of");
      break;
    }
  }
  luaT_callTMres(L, tm, rb, rb, ra);
}
```

Table 先试 `fasttm` 查 `__len`(走缓存,`__len` 是 fast-access);有就用元方法,没有就 `luaH_getn` 取原生长度(数组部分长度,或哈希部分边界)。字符串直接取长度字段(短串 `shrlen`、长串 `u.lnglen`,P1-03 见过)。其他类型(userdata、number 等)只能走 `__len` 元方法,没有就报错。

### 2.10 __gc 和 __close:析构

`__gc` 是 finalizer:对象被 GC 回收前调用。触发点在 `lgc.c` 的 `GCTM`(`lgc.c:968`):

```c
static void GCTM (lua_State *L) {
  ...
  tm = luaT_gettmbyobj(L, &v, TM_GC);
  ...
}
```

GC 把带 finalizer 的对象放进一个单独的队列(`global_State` 里的 `finobj`/`tobefnz`),在 GC 周期末尾逐个调 `__gc`。这里有个微妙的"对象复活"语义:`__gc` 调用时,如果它把 `self` 重新引用到别处,对象就"复活"了——本该回收的不回收。Lua 的处理是 `__gc` 只调一次,复活的对象下次不再调(避免一个对象反复析构)。

`__close` 是 5.4 引入的,配合 to-be-closed 变量(`local x <close> = obj`):变量退出作用域时调 `obj.__close`。触发在 `luaF_close`(由 `OP_CLOSE` 调)。和 `__gc` 的区别:`__close` 是作用域退出时立即调,`__gc` 是 GC 回收时调——前者确定时机,后者不确定。

---

## 三、为什么这样设计是 sound 的

### 3.1 flags 缓存的正确性

缓存的逻辑是:"如果某次查 `luaT_gettm` 发现 metatable 里没有 `__index`,就把 metatable 的 flags 对应位置 1,下次直接跳过查找。" 这个缓存放过的唯一正确性问题是:**缓存说"没有",但实际上有了。** 这要求任何时候 metatable 的内容变了,缓存必须失效。

Lua 的做法是在所有可能改写 Table 内容的路径上插 `invalidateTMcache`:

- `luaV_finishset` 写入新值后(本表可能被别人当 metatable)。
- `ltable.c` 的 rehash(哈希表结构变化)。
- `lapi.c` 的 `lua_settable`/`lua_rawset` 等 C API 写操作。

清的是"所有 fast-access 位"(`~maskflags`),最保守。代价是一个字节 AND,可忽略。这个设计的 sound 在于:**宁可多清(假阴性,缓存失效但本可命中,只是多查一次哈希),也绝不少清(假阳性,缓存说没有但实际有,导致漏触发元方法)。** 漏触发是语义错误,不可接受。

另外,新表 flags 初值是 `maskflags`(全 1,表示没有任何 fast-access 元方法)——空表的正确状态。第一次查某元方法时 `checknoTM` 命中,直接返回 NULL,根本不进 `luaT_gettm`,连那次哈希查找都省了。这把"没有 metatable 的普通表"的元方法查询代价降到一次位测试。

### 3.2 __index 防环

`__index` 可以指向另一张表,那张表的 `__index` 又可以指向更上层的表,形成原型链。如果链上有环(`a.__index = b, b.__index = a`),递归查询会死循环。Lua 用 `MAXTAGLOOP = 2000`(lvm.c:50)兜底:每查一层 `loop++`,超过 2000 就 `luaG_runerror("'__index' chain too long; possible loop'")`。

2000 这个数是经验值:正常的继承链深不过几十层(现实中的类层级很少超过 20 层),2000 给足余量又能在合理时间内检测到环。`__newindex` 用同样的机制。

`__call` 用 `MAX_CCMT` 计数器防"元方法返回值又有 `__call` 又触发"的无限套娃,原理相同。

### 3.3 元方法调用受保护

`luaT_callTM` 在调元方法前把参数压到栈顶,然后走 `luaD_call`(或 `luaD_callnoyield`)。`luaD_call` 内部是 `luaD_precall` + 执行,这套机制本身就在受保护的环境里运行(外层有 `errorJmp` 错误恢复点,P4-13 见过)。所以即使元方法内部 `error()`,错误也能被上层 `pcall` 捕获,不会让 VM 崩溃。

一个细节:`luaT_callTMres` 用 `savestack`/`restorestack` 保存结果寄存器的相对位置(`ltm.c:121`):

```c
lu_byte luaT_callTMres (lua_State *L, const TValue *f, const TValue *p1,
                        const TValue *p2, StkId res) {
  ptrdiff_t result = savestack(L, res);
  ...
  res = restorestack(L, result);
  setobjs2s(L, res, --L->top.p);  /* move result to its place */
  return ttypetag(s2v(res));  /* return tag of the result */
}
```

因为元方法调用过程中栈可能扩容(`realloc` 搬家),`res` 这个绝对指针会失效。用相对偏移 `savestack` 存下来,调完 `restorestack` 重算。这是 5.5 相对栈表示(StkIdRel,P0-01 提到过)在元方法路径上的具体体现。

---

## 四、★对照 CPython + 回扣主线

### 4.1 CPython 的类型系统

CPython 有真正的 `class`/`type`。每个对象有个 `ob_type` 指针指向一个 `PyTypeObject`,那是个完整的类型对象,装着这个类型的所有"特殊方法"——CPython 叫 dunder methods(double underscore),如 `__add__`、`__getattr__`、`__init__`、`__len__`、`__eq__` 等。

关键差异:

| 维度 | Lua 5.5 | CPython |
|---|---|---|
| **类型载体** | Table + metatable(也是 Table),约定字段名 | `PyTypeObject` 结构体,固定槽位 |
| **方法绑定** | 无绑定,`obj:method()` 是 `obj.method(obj)` 的语法糖 | 描述符协议,`obj.method` 自动绑定实例 |
| **运算符重载入口** | VM 在指令失败时查 metatable 的 `__xxx` | VM 直接调 `ob_type->tp_xxx` 函数指针 |
| **方法查找开销** | flags 缓存(6 个 fast-access)+ 哈希查找 | 类型对象直接槽位访问,O(1) |
| **继承实现** | `__index` 指向父表,递归查 | MRO(Method Resolution Order)线性化 |
| **是否内建** | 全靠约定,语言只认 `__index`/`__add` 这些字符串 | `type` 是一等对象,继承是语言内建 |

具体到"加法"。Lua 里 `a + b`:

1. `OP_ADD` 快速路径试整数/浮点,失败。
2. `OP_MMBIN` 调 `luaT_trybinTM`,查 `a` 的 metatable 的 `__add` 字段(哈希查找字符串 `"__add"`)。
3. 找到就调,找不到查 `b` 的 metatable,都没有报错。

CPython 里 `a + b`:

1. `BINARY_OP` 指令调 `PyNumber_Add`。
2. `PyNumber_Add` 查 `type(a)->tp_as_number->nb_add`(直接函数指针,不是哈希查找)。
3. 没有就查 `type(b)` 的,都没有报 `TypeError`。

CPython 的类型槽是**编译期/链接期就固定好的 C 结构体字段**,查找是一次指针解引用;Lua 是**运行期的哈希表查找**(虽然有 flags 缓存)。这是 Lua 为"统一"(类型也是 Table)付出的代价:类型行为查询比 CPython 慢一档。但换来的是——任何 Table 都能动态挂任意 metatable,类型系统完全可编程,不需要预先定义类型对象。

再具体到"继承"。Lua:

```lua
local Base = {greet = function(self) print("hi") end}
local Derived = {}
setmetatable(Derived, {__index = Base})
Derived:greet()  -- hi,沿 __index 找到 Base.greet
```

CPython:

```python
class Base:
    def greet(self): print("hi")
class Derived(Base):  # 显式继承声明,MRO 线性化
    pass
Derived().greet()  # hi,沿 MRO 查找
```

CPython 的继承是语言内建的,有 MRO 线性化算法(C3 线性化)解决多继承菱形问题;Lua 的继承是 `__index` 递归,单链式,多继承要手动组合(通常用一个 metatable 的 `__index` 是函数,在里面分发到多个父表)。Lua 的简单换来灵活但不够规整,CPython 的规整换来内建支持但更重。

### 4.2 为什么 Lua 选这条路

回到主线。Lua 没有 `class`/`type`,不是因为做不到,而是因为**统一**这条主线要求:复合数据只有一个结构(Table),类型行为也用这个结构表达。如果专门造一个 `Type` 结构体,就破坏了"一切复合数据都是 Table"的统一,内核要多一套类型系统的代码(类型对象的生命周期、MRO、描述符协议……)——这和"小"的目标冲突。

metatable 的精妙在于:**它复用了 Table 这一个结构,却让 Table 获得了类型系统的能力。** 元方法是约定好名字的字段(`__add` 等),VM 在固定时机去查——不需要新的数据结构,不需要新的生命周期管理(metatable 也是 Table,GC 照常回收),不需要新的 C API(`setmetatable` 就是普通的字段赋值)。

代价是:

- **类型行为查询比内建类型系统慢**(哈希查找 vs 指针解引),靠 flags 缓存部分弥补。
- **OOP 语义靠约定**:没有内建的 `class`、没有方法绑定、没有 MRO。要写出像样的 OOP 代码,得靠库(比如 `middleclass`)或程序员自己遵守约定。
- **元方法集合是封闭的**:不能自己发明 `__foo` 让 VM 认,只能用 TMS 枚举里那 24 个。

这三个代价是"统一"换小换快的必然结果。Lua 选择了最小机制:一个 Table + 一组约定字段 + 一条"指令失败时查约定字段"的规则。这套机制足够表达 OOP、运算符重载、原型继承——所有"类型行为"都建立在它之上。这正是主线的具体落地:**用更少的机制(一个 Table 当类型),换更多的能力(对象、继承、重载)。**

---

*下一章 [P6-20 协程:协作式多任务的栈切换](P6-20-协程-协作式多任务的栈切换.md):从 metatable 这种"语言层面的扩展点"转向另一个扩展点——协程,看 Lua 怎么用一个独立的 `lua_State` 栈实现协作式多任务。*
