# P6-21 C API 与嵌入:宿主与脚本的双向桥

> **本书主线**:统一与精简换小而快。**二分法**:编译侧(源码→字节码)↔ 执行侧(VM 执行 + 管值生命周期)。**★对照**:CPython。**源码**:lua-5.5.0。**基调**:纯直球,不用比喻。
>
> **本章主线落点**:全书"嵌入性"的总落地。Lua 之所以能塞进任何宿主(游戏引擎、Redis、nginx、Wireshark),最终靠的就是 C API。一个 `lua_State` 加一套栈式 API,就是宿主(C)与脚本(Lua)之间的双向桥——宿主能往 VM 里塞值、调 Lua 函数、跑脚本取结果;Lua 也能反过来调宿主注册的 C 函数。本章把这条桥的每一块桥板逐行铺开,并在结尾把前面所有章节(值的表示、Table、闭包、调用栈、GC、协程、元表)汇合到这一个"嵌入完整闭环"上。

---

## 一、这章解决什么问题

前二十章,我们一直在 VM 内部走:一个值在 `TValue` 里怎么装(P1-02),一个 `Table` 怎么承载数组与哈希(P1-04/05),源码怎么被编译成寄存器式字节码(P2),VM 的解释器循环怎么取指译码分发(P3-11),函数怎么被调起来、闭包怎么捕获 upvalue(P4-13/14),增量三色 GC 怎么按步推进(P5),元表怎么让 Table 充当对象(P6-19),协程怎么靠切栈实现协作式多任务(P6-20)。这些讲完,Lua 作为一门语言 VM 的内部已经完整。

但这门语言要真正"被宿主用起来",还差最后一层:**宿主(C 代码)和 VM 之间怎么交换数据、怎么互相调用**。这一层就是 C API,源码主要在 `lapi.c`(1473 行)、`lauxlib.c`(1202 行)和 `linit.c`(只有 60 多行)。

这一章要回答三个具体问题:

1. **宿主 C 怎么操作 Lua 的值?** Lua 的值(`TValue`、`Table`、字符串、闭包)在 VM 内部有复杂的 tagged 编码和 GC 链。宿主是外部 C 代码,绝不能直接去碰 `TValue` 的内部字段——否则 GC 一动宿主就错。Lua 的答案是:**栈式 API**。宿主永远通过操作一个属于当前 `lua_State` 的虚拟栈,来读写 Lua 的值;宿主从不持有 Lua 对象的裸指针(除了 light userdata 这种刻意逃逸的口子)。
2. **怎么让 Lua 调 C?** Lua 脚本里写 `host.compute(x)` 时,`compute` 的实现是宿主的 C 函数。要把这个 C 函数挂到 Lua 的世界里,需要一套"注册 C 函数"的机制:C 函数有固定签名 `int (*)(lua_State *)`,内部从栈读参数、把结果压栈、返回结果个数。
3. **一个完整的嵌入闭环长什么样?** 从 `luaL_newstate()` 建一个全新 VM,到 `luaL_setfuncs` 把宿主函数注册进去,到 `luaL_dostring` 跑一段脚本,到 `lua_pcall` 调脚本里的函数并受保护地拿到结果,到 `lua_close` 销毁。这条链把前面每一章都串起来——`newstate` 建的 `lua_State` 就是 P1-02 讲的那个结构;`pcall` 的错误恢复靠 P4-13 讲的调用栈 + `setjmp`;注册的 C 闭包走的是 P4-14 讲的 `CClosure`;跑脚本触发的 GC 是 P5 讲的增量三色;调 Lua 函数时如果它 yield,续体机制接的是 P6-20 讲的协程。

本章的对照栏会把这套设计与 CPython C API 正面对比:CPython 让宿主直接操作 `PyObject *`、手动 `Py_INCREF`/`Py_DECREF` 管引用计数,Lua 让宿主只碰虚拟栈、值由 GC 管、不手动管引用计数。两种设计是两种根本不同的取舍——直接暴露换灵活但易错,栈式解耦换安全但多一次拷贝。

---

## 二、栈式 API 的思想:宿主为什么不直接碰 TValue

先看清 Lua 的 C API 为什么是"栈式"。这决定了一切后续设计。

宿主 C 代码要往 Lua 世界里传一个值(比如一个数字 `42`),最直觉的做法是:宿主直接构造一个 `TValue`,塞到 Lua 的某个表里。但这在 Lua 里行不通,原因有三:

1. **`TValue` 的内部表示是 VM 的实现细节**。5.5 的 `TValue`(`lobject.h:67`)用 tagged union 编码,整数、浮点、布尔、nil、GC 对象各有各的 tag 和位布局(P1-02 详讲)。宿主如果手搓 `TValue`,等于和 VM 的内部表示强耦合;VM 一升级(比如 5.4→5.5 改了 `StkId` 为 `StkIdRel`),宿主代码就废了。
2. **GC 问题**。如果宿主直接持有指向 Lua 对象(比如一个 `Table *`)的 C 指针,而 GC 在某个时刻回收或搬动了这个对象,宿主的指针就悬空。Lua 的增量 GC 是会动的(P5-16),宿主绝不能假定自己持有的指针在两次 API 调用之间还有效。
3. **类型安全**。宿主随手构造的 `TValue` 可能 tag 和 value 对不上(比如 tag 写成字符串、value 却是 NULL),VM 一解释就崩。

Lua 的解法是:**在 `lua_State` 上挂一段值栈,作为宿主和 VM 之间唯一的交换缓冲区**。这段栈在 `lua_State` 里就是 `stack`/`top`/`ci` 这几个字段(`lstate.h:289-299`)。宿主的所有 API 调用,本质都是往这段栈上压值、从这段栈上读值、或者操作这段栈。宿主从不直接构造 `TValue`,而是调 `lua_pushinteger(L, 42)`——这个 API 内部替宿主把 `42` 编码成正确的 `setivalue(s2v(L->top.p), n)`(`lapi.c:532`),并推进栈顶。

看 `lua_pushinteger` 的完整实现(`lapi.c:530-535`):

```c
LUA_API void lua_pushinteger (lua_State *L, lua_Integer n) {
  lua_lock(L);
  setivalue(s2v(L->top.p), n);
  api_incr_top(L);
  lua_unlock(L);
}
```

三步:锁(`lua_lock`,在单线程构建下是空宏,多线程构建下可选挂锁)、把值写进当前栈顶槽位(`setivalue` 展开成设置 tag 为 `LUA_VNUMINT` 并写整数值)、推进栈顶(`api_incr_top` 检查不越界后 `L->top.p++`)。宿主看到的只是一个 `void` 函数,内部所有 VM 细节都被封装掉了。

这个设计的妙处在于:**栈既是数据交换的缓冲区,又是宿主与 VM 之间的隔离层**。宿主只看到"一个会涨会缩的值数组",不需要知道 `TValue` 的位长什么样、不需要知道 GC 在干什么、不需要知道当前在哪个 `CallInfo` 帧。值一旦压进栈,它的生命周期就由 VM(具体说是 GC)管,宿主不需要、也不应该手动释放它。

这与 CPython 形成根本对照,本章第四节会展开。

---

## 三、栈索引:正数、负数、伪索引

栈式 API 操作栈,需要一个寻址方式。Lua 用整数索引,分三类。核心逻辑全在 `lapi.c:58` 的 `index2value`:

```c
static TValue *index2value (lua_State *L, int idx) {
  CallInfo *ci = L->ci;
  if (idx > 0) {
    StkId o = ci->func.p + idx;
    api_check(L, idx <= ci->top.p - (ci->func.p + 1), "unacceptable index");
    if (o >= L->top.p) return &G(L)->nilvalue;
    else return s2v(o);
  }
  else if (!ispseudo(idx)) {  /* negative index */
    api_check(L, idx != 0 && -idx <= L->top.p - (ci->func.p + 1),
                 "invalid index");
    return s2v(L->top.p + idx);
  }
  else if (idx == LUA_REGISTRYINDEX)
    return &G(L)->l_registry;
  else {  /* upvalues */
    idx = LUA_REGISTRYINDEX - idx;
    api_check(L, idx <= MAXUPVAL + 1, "upvalue index too large");
    if (ttisCclosure(s2v(ci->func.p))) {  /* C closure? */
      CClosure *func = clCvalue(s2v(ci->func.p));
      return (idx <= func->nupvalues) ? &func->upvalue[idx-1]
                                      : &G(L)->nilvalue;
    }
    else {  /* light C function or Lua function (through a hook)?) */
      api_check(L, ttislcf(s2v(ci->func.p)), "caller not a C function");
      return &G(L)->nilvalue;  /* no upvalues */
    }
  }
}
```

三类索引:

1. **正数索引(绝对索引)**:`idx > 0`,从栈底数起,`1` 是栈底。注意基准是 `ci->func.p + idx`——索引 `1` 不是 `stack[0]`,而是"当前调用帧的函数之上的第一个槽位"。这是 Lua 调用约定的体现(P4-13):每个 `CallInfo` 帧的栈底是它自己的函数,函数之上才是参数和局部变量。所以宿主从 C 函数里看栈,索引 `1` 就是第一个传入参数。如果索引指向的槽位在 `top` 之上(即没压过值),返回全局的 `nilvalue`(一个共享的 nil 哨兵,见 `lstate.c:374`),这样越界读取不会崩、只会得到 nil。
2. **负数索引(相对索引)**:`idx < 0` 且不是伪索引。从栈顶数起,`-1` 是栈顶,`-2` 是次顶。实现就是 `L->top.p + idx`。负数索引的好处是:宿主经常不知道栈里压了多少东西(尤其调用一个返回多值的 Lua 函数后),用 `-1` 直接拿栈顶最方便。
3. **伪索引(pseudo-index)**:`idx <= LUA_REGISTRYINDEX`。这是给"不在值栈上、但需要像栈值一样访问"的东西用的。`LUA_REGISTRYINDEX`(`lua.h:43`)定义为 `-(INT_MAX/2 + 1000)`,是个绝对值很大的负数,确保不和任何合法的真实索引冲突。它指向注册表 `G(L)->l_registry`(一个全局 Table)。比它还小的索引 `LUA_REGISTRYINDEX - i` 指向当前 C 函数的第 `i` 个 upvalue(下文 C 闭包详讲)。判断是否伪索引用宏 `ispseudo(i)`(`(i) <= LUA_REGISTRYINDEX`,`lapi.c:48`),判断是否 upvalue 伪索引用 `isupvalue(i)`(`(i) < LUA_REGISTRYINDEX`,`lapi.c:51`)。

`lua_absindex`(`lapi.c:167`)把负索引转成正索引,但伪索引原样返回——伪索引没有对应的栈绝对位置:

```c
LUA_API int lua_absindex (lua_State *L, int idx) {
  return (idx > 0 || ispseudo(idx))
         ? idx
         : cast_int(L->top.p - L->ci->func.p) + idx;
}
```

这套索引设计有一个关键的 sound 保证:**所有读值 API 都先过 `index2value`,越界或类型不对时返回 nil 或哨兵,绝不解引用野指针**。这是栈式 API 安全性的第一道闸。

---

## 四、压栈:把 C 的值送进 Lua

压栈是最基础的方向:C → Lua。Lua 为每一种基本类型提供了一个 `lua_push*`。看几个典型实现,它们结构高度一致:锁 → 把值写进 `s2v(L->top.p)` → `api_incr_top` → 解锁。

`lua_pushnil`(`lapi.c:514`):

```c
LUA_API void lua_pushnil (lua_State *L) {
  lua_lock(L);
  setnilvalue(s2v(L->top.p));
  api_incr_top(L);
  lua_unlock(L);
}
```

`lua_pushnumber`(`lapi.c:522`):

```c
LUA_API void lua_pushnumber (lua_State *L, lua_Number n) {
  lua_lock(L);
  setfltvalue(s2v(L->top.p), n);
  api_incr_top(L);
  lua_unlock(L);
}
```

`lua_pushinteger` 已贴过(第三节)。`lua_pushboolean`(`lapi.c:636`)稍特殊,因为 Lua 5.5 把 `true`/`false` 编成两个不同的类型 tag,不是用一个 bool 位:

```c
LUA_API void lua_pushboolean (lua_State *L, int b) {
  lua_lock(L);
  if (b)
    setbtvalue(s2v(L->top.p));
  else
    setbfvalue(s2v(L->top.p));
  api_incr_top(L);
  lua_unlock(L);
}
```

`lua_pushlightuserdata`(`lapi.c:647`)压的是一个裸 C 指针,VM 不参与它的生命周期管理:

```c
LUA_API void lua_pushlightuserdata (lua_State *L, void *p) {
  lua_lock(L);
  setpvalue(s2v(L->top.p), p);
  api_incr_top(L);
  lua_unlock(L);
}
```

`lua_pushstring`(`lapi.c:570`)是重量级的——它要把 C 字符串变成 Lua 的驻留字符串(P1-03 讲的短串 intern),所以涉及分配和 GC:

```c
LUA_API const char *lua_pushstring (lua_State *L, const char *s) {
  lua_lock(L);
  if (s == NULL)
    setnilvalue(s2v(L->top.p));
  else {
    TString *ts;
    ts = luaS_new(L, s);
    setsvalue2s(L, L->top.p, ts);
    s = getstr(ts);  /* internal copy's address */
  }
  api_incr_top(L);
  luaC_checkGC(L);
  lua_unlock(L);
  return s;
}
```

注意三件事:一是 `s == NULL` 时压 nil(方便宿主把"拿不到字符串"直接映射成 nil);二是 `luaS_new` 内部走驻留表,相同内容只存一份;三是压完调 `luaC_checkGC(L)`——因为可能分配了新字符串,要按 P5 的增量步进规则顺手做一小步 GC。返回值是 Lua 内部那份拷贝的地址,宿主可以临时读它,但不能跨 API 调用持有(GC 可能回收或搬家)。

`lua_pushlstring`(`lapi.c:543`)和 `lua_pushstring` 的区别是带长度,能压含 `\0` 的二进制串;长度为 0 时走 `luaS_new(L, "")` 拿到共享的空串。

所有压栈函数,宿主都不需要操心 GC barrier——因为新压的值要么是栈上原值(`TValue` 不需要 barrier)、要么是刚创建的新对象(新对象是白色,`lua_assert(iswhite(cl))`,P5-16 讲过白色对象不需要屏障)。这是 GC sound 的一个具体落点。

---

## 五、读值:把 Lua 的值取回 C

反方向 Lua → C 的是 `lua_to*`。这一类要小心类型:Lua 是动态类型,栈上某个槽位当前可能是任何类型。`lua_to*` 的约定是:**能转就转,不能转就返回 0/NULL,不抛错**。要严格检查就配套用 `lua_is*` 或直接用 `luaL_check*`(下文)。

`lua_toboolean`(`lapi.c:409`)最简单:

```c
LUA_API int lua_toboolean (lua_State *L, int idx) {
  const TValue *o = index2value(L, idx);
  return !l_isfalse(o);
}
```

`l_isfalse` 判定只有 `nil` 和 `false` 为假,其他全为真——符合 Lua 的真值语义。

`lua_tonumberx`(`lapi.c:389`)和 `lua_tointegerx`(`lapi.c:399`)带一个 `isnum` 出参,告诉宿主这次转换成不成功:

```c
LUA_API lua_Number lua_tonumberx (lua_State *L, int idx, int *pisnum) {
  lua_Number n = 0;
  const TValue *o = index2value(L, idx);
  int isnum = tonumber(o, &n);
  if (pisnum)
    *pisnum = isnum;
  return n;
}
```

`tonumber` 是个宏,会处理"字符串能不能解析成数字""整数能不能无损转成浮点"等情况(P1-02 详讲)。`lua_tointegerx` 同理用 `tointeger`。不传 `isnum`(传 NULL)就是盲取,转不成功都返回 0——这是 `lua_tonumber`/`lua_tointeger` 这两个不带 `x` 的宏的行为(它们是 `lua.h` 里的简写宏)。

`lua_tolstring`(`lapi.c:415`)有意思,它会做"隐式转换":

```c
LUA_API const char *lua_tolstring (lua_State *L, int idx, size_t *len) {
  TValue *o;
  lua_lock(L);
  o = index2value(L, idx);
  if (!ttisstring(o)) {
    if (!cvt2str(o)) {  /* not convertible? */
      if (len != NULL) *len = 0;
      lua_unlock(L);
      return NULL;
    }
    luaO_tostring(L, o);
    luaC_checkGC(L);
    o = index2value(L, idx);  /* previous call may reallocate the stack */
  }
  lua_unlock(L);
  if (len != NULL)
    return getlstr(tsvalue(o), *len);
  else
    return getstr(tsvalue(o));
}
```

如果栈上是数字,`lua_tolstring` 会把它转成字符串再返回——这意味着**调用会修改栈上的值**(把数字原地变成字符串)。这是个 footgun:你以为是只读,实际改了。Lua 文档明确警告不要把 `lua_tolstring` 的结果当作 key 去操作同一个表。注意 `luaO_tostring` 后重新 `index2value` 一次,因为转换可能触发栈 realloc,旧地址作废——这是 5.5 用 `StkIdRel` 相对表示后必须小心的事(P0-01 已点过)。

`lua_rawlen`(`lapi.c:437`)按原始长度返回(字符串的字节数、Table 不走 `__len` 元方法的数组长度),`lua_type`(`lapi.c:282`)返回类型常量,`lua_typename`(`lapi.c:288`)把类型常量翻成字符串("nil"/"boolean"/"lightuserdata"/"number"/"string"/"table"/"function"/"userdata"/"thread")。

整套 `lua_to*`/`lua_is*` 的 sound 保证在 `index2value`:越界返回 `nilvalue`,类型不符返回 0/NULL,**绝不让宿主拿到一个被错误解释的 `TValue`**。

---

## 六、栈管理:gettop/settop/rotate/copy

宿主在栈上来回搬值,需要一组栈管理 API。

`lua_gettop`(`lapi.c:174`)返回栈里值的个数(以当前帧函数之上的槽位计):

```c
LUA_API int lua_gettop (lua_State *L) {
  return cast_int(L->top.p - (L->ci->func.p + 1));
}
```

`lua_settop`(`lapi.c:179`)设栈顶。正参数是绝对设、负参数是相对减;设低时若跨过了 to-be-closed 变量(P4-15),要先关 upvalue:

```c
LUA_API void lua_settop (lua_State *L, int idx) {
  CallInfo *ci;
  StkId func, newtop;
  ptrdiff_t diff;
  lua_lock(L);
  ci = L->ci;
  func = ci->func.p;
  if (idx >= 0) {
    api_check(L, idx <= ci->top.p - (func + 1), "new top too large");
    diff = ((func + 1) + idx) - L->top.p;
    for (; diff > 0; diff--)
      setnilvalue(s2v(L->top.p++));  /* clear new slots */
  }
  else {
    api_check(L, -(idx+1) <= (L->top.p - (func + 1)), "invalid new top");
    diff = idx + 1;  /* will "subtract" index (as it is negative) */
  }
  newtop = L->top.p + diff;
  if (diff < 0 && L->tbclist.p >= newtop) {
    lua_assert(ci->callstatus & CIST_TBC);
    newtop = luaF_close(L, newtop, CLOSEKTOP, 0);
  }
  L->top.p = newtop;  /* correct top only after closing any upvalue */
  lua_unlock(L);
}
```

`lua_pop(L, n)` 是 `lua.h` 里的宏,等于 `lua_settop(L, -(n)-1)`。注意这个负号——宿主常写错。

`lua_rotate`(`lapi.c:238`)用三次反转实现旋转(经典手法的注释写得很清楚:"Let x = AB, where A is a prefix of length 'n'. Then, rotate x n == BA. But BA == (A^r . B^r)^r."):

```c
LUA_API void lua_rotate (lua_State *L, int idx, int n) {
  StkId p, t, m;
  lua_lock(L);
  t = L->top.p - 1;  /* end of stack segment being rotated */
  p = index2stack(L, idx);  /* start of segment */
  api_check(L, L->tbclist.p < p, "moving a to-be-closed slot");
  api_check(L, (n >= 0 ? n : -n) <= (t - p + 1), "invalid 'n'");
  m = (n >= 0 ? t - n : p - n - 1);  /* end of prefix */
  reverse(L, p, m);  /* reverse the prefix with length 'n' */
  reverse(L, m + 1, t);  /* reverse the suffix */
  reverse(L, p, t);  /* reverse the entire segment */
  lua_unlock(L);
}
```

基于 `lua_rotate`,`lua_insert`、`lua_remove`、`lua_replace` 都是宏(`lua.h` 里),组合一下旋转和设顶就实现了插入、删除、替换。注意 `api_check(L, L->tbclist.p < p, "moving a to-be-closed slot")`——绝不允许把一个待关闭变量旋转走,否则 `__close` 会找错对象,这是 P4-15 讲的 to-be-closed 机制的 sound 保证。

`lua_copy`(`lapi.c:253`)直接把一个索引的值复制到另一个索引,**原地覆盖**:

```c
LUA_API void lua_copy (lua_State *L, int fromidx, int toidx) {
  TValue *fr, *to;
  lua_lock(L);
  fr = index2value(L, fromidx);
  to = index2value(L, toidx);
  api_check(L, isvalid(L, to), "invalid index");
  setobj(L, to, fr);
  if (isupvalue(toidx))  /* function upvalue? */
    luaC_barrier(L, clCvalue(s2v(L->ci->func.p)), fr);
  /* LUA_REGISTRYINDEX does not need gc barrier
     (collector revisits it before finishing collection) */
  lua_unlock(L);
}
```

注释点出一个 sound 细节:写到 C 闭包的 upvalue 要打 GC barrier(因为旧值可能被覆盖,引用关系变了);但写注册表不需要 barrier——因为 GC 在完成收集前会重访注册表(`lgc.c` 里的特殊处理)。这种"哪里需要 barrier、哪里不需要"的精确判断,是增量 GC 能正确的关键(P5-16/17)。

`lua_pushvalue`(`lapi.c:268`)是把某索引的值复制一份压到栈顶,是 `lua_copy` 的"压栈版"。

---

## 七、表操作:让 Table 从 C 端可用

Table 是 Lua 唯一的复合数据(P1-04)。C API 必须让宿主能创建表、读写表。这一组 API 分"走元方法"和"不走元方法(raw)"两套。

`lua_createtable`(`lapi.c:792`)建一个空表并压栈,可提示数组部分和哈希部分的初始大小:

```c
LUA_API void lua_createtable (lua_State *L, int narray, int nrec) {
  Table *t;
  lua_lock(L);
  t = luaH_new(L);
  sethvalue2s(L, L->top.p, t);
  api_incr_top(L);
  if (narray > 0 || nrec > 0)
    luaH_resize(L, t, cast_uint(narray), cast_uint(nrec));
  luaC_checkGC(L);
  lua_unlock(L);
}
```

`luaH_new` 建空表(P1-04),`luaH_resize` 按提示预分配,避免后续插入时频繁 rehash(P1-05)。预分配是性能优化点:宿主知道要塞多少东西时,给提示能省一串 rehash。

读表:`lua_gettable`(`lapi.c:707`)走元方法,键是栈顶值;`lua_getfield`(`lapi.c:721`)键是字符串字面量(更常用);`lua_geti`(`lapi.c:727`)键是整数。它们内部都先走 `luaV_fastget`(P3-11 讲过的快速路径,直接命中哈希槽),没命中再走 `luaV_finishget`(可能触发 `__index` 元方法,P6-19):

```c
LUA_API int lua_gettable (lua_State *L, int idx) {
  lu_byte tag;
  TValue *t;
  lua_lock(L);
  api_checkpop(L, 1);
  t = index2value(L, idx);
  luaV_fastget(t, s2v(L->top.p - 1), s2v(L->top.p - 1), luaH_get, tag);
  if (tagisempty(tag))
    tag = luaV_finishget(L, t, s2v(L->top.p - 1), L->top.p - 1, tag);
  lua_unlock(L);
  return novariant(tag);
}
```

返回值的类型(压到栈顶的值是什么类型),宿主可以据此分支处理。

`lua_rawget`(`lapi.c:760`)、`lua_rawgeti`(`lapi.c:772`)、`lua_rawgetp`(`lapi.c:782`)是 raw 版本——**绕开元方法**,直接走 `luaH_get`/`luaH_fastgeti`。raw 版用于:实现元方法本身时(否则会无限递归)、或宿主明确不想触发 `__index` 的场景。看 `lua_rawget`:

```c
LUA_API int lua_rawget (lua_State *L, int idx) {
  Table *t;
  lu_byte tag;
  lua_lock(L);
  api_checkpop(L, 1);
  t = gettable(L, idx);
  tag = luaH_get(t, s2v(L->top.p - 1), s2v(L->top.p - 1));
  L->top.p--;  /* pop key */
  return finishrawget(L, tag);
}
```

写表是对称的:`lua_settable`(`lapi.c:886`)、`lua_setfield`(`lapi.c:902`)、`lua_seti`(`lapi.c:908`)走元方法,raw 版 `lua_rawset`/`lua_rawseti`/`lua_rawsetp` 不走。`lua_settable` 的实现展示 fast/slow 双路径:

```c
LUA_API void lua_settable (lua_State *L, int idx) {
  TValue *t;
  int hres;
  lua_lock(L);
  api_checkpop(L, 2);
  t = index2value(L, idx);
  luaV_fastset(t, s2v(L->top.p - 2), s2v(L->top.p - 1), hres, luaH_pset);
  if (hres == HOK)
    luaV_finishfastset(L, t, s2v(L->top.p - 1));
  else
    luaV_finishset(L, t, s2v(L->top.p - 2), s2v(L->top.p - 1), hres);
  L->top.p -= 2;  /* pop index and value */
  lua_unlock(L);
}
```

`luaV_fastset` 先试直接写入已有哈希槽(`HOK` 表示命中);没命中(键不存在,或需要 rehash,或要触发 `__newindex`)走 `luaV_finishset`。这套 fast/slow 双路径是 VM 内部解释器循环的同一套机制(P3-11),C API 直接复用。

`lua_setglobal`/`lua_getglobal`(`lapi.c:699/878`)是"读写全局变量"的便捷封装,内部先从注册表取出全局表(`getGlobalTable`,`lapi.c:691`),再对全局表做 `getfield`/`setfield`。全局表存在注册表的 `LUA_RIDX_GLOBALS`(`lua.h:84`)槽位里。

---

## 八、调用:lua_call / lua_pcall / 续体

宿主把函数和参数压栈后,要能调用它。Lua 提供两类调用 API:**不保护**的 `lua_call` 和**受保护**的 `lua_pcall`。

`lua_callk`(`lapi.c:1037`,普通 `lua_call` 是它 `k=NULL` 的简写宏):

```c
LUA_API void lua_callk (lua_State *L, int nargs, int nresults,
                        lua_KContext ctx, lua_KFunction k) {
  StkId func;
  lua_lock(L);
  api_check(L, k == NULL || !isLua(L->ci),
    "cannot use continuations inside hooks");
  api_checkpop(L, nargs + 1);
  api_check(L, L->status == LUA_OK, "cannot do calls on non-normal thread");
  checkresults(L, nargs, nresults);
  func = L->top.p - (nargs+1);
  if (k != NULL && yieldable(L)) {  /* need to prepare continuation? */
    L->ci->u.c.k = k;  /* save continuation */
    L->ci->u.c.ctx = ctx;  /* save context */
    luaD_call(L, func, nresults);  /* do the call */
  }
  else  /* no continuation or no yieldable */
    luaD_callnoyield(L, func, nresults);  /* just do the call */
  adjustresults(L, nresults);
  lua_unlock(L);
}
```

约定是:函数压在 `-(nargs+1)`,参数跟在后面,`nresults` 是期望返回值个数(`LUA_MULTRET` 表示全要)。调用约定本身在 P4-13 已详讲。这里的关键是 `luaD_call`(P4-13)——它推进 `CallInfo`,真正进 VM 的解释器循环执行被调函数;执行完返回时,`adjustresults` 把多退少补的返回值规整成 `nresults` 个。

`lua_call` **不保护**:被调函数里如果抛错(比如 `error()`、或除零、或类型错),错误会一路 `luaG_errormsg` → `luaD_throw` 跳到最近的 recover point。如果栈上没有 recover point(没有 pcall 包着),错误会跳出 VM,直接进 panic 函数(`luaL_newstate` 注册的那个 `panic`,`lauxlib.c`),通常 `exit` 进程。所以 `lua_call` 只用在宿主有把握不出错、或宿主自己在外层包了 pcall 的场景。

`lua_pcallk`(`lapi.c:1076`,普通 `lua_pcall` 是 `k=NULL` 的简写)是受保护版,这是嵌入宿主最常用的调用 API:

```c
LUA_API int lua_pcallk (lua_State *L, int nargs, int nresults, int errfunc,
                        lua_KContext ctx, lua_KFunction k) {
  struct CallS c;
  TStatus status;
  ptrdiff_t func;
  lua_lock(L);
  ...
  if (errfunc == 0)
    func = 0;
  else {
    StkId o = index2stack(L, errfunc);
    api_check(L, ttisfunction(s2v(o)), "error handler must be a function");
    func = savestack(L, o);
  }
  c.func = L->top.p - (nargs+1);  /* function to be called */
  if (k == NULL || !yieldable(L)) {  /* no continuation or no yieldable? */
    c.nresults = nresults;  /* do a 'conventional' protected call */
    status = luaD_pcall(L, f_call, &c, savestack(L, c.func), func);
  }
  else {  /* prepare continuation (call is already protected by 'resume') */
    CallInfo *ci = L->ci;
    ci->u.c.k = k;  /* save continuation */
    ci->u.c.ctx = ctx;  /* save context */
    ci->u2.funcidx = cast_int(savestack(L, c.func));
    ci->u.c.old_errfunc = L->errfunc;
    L->errfunc = func;
    setoah(ci, L->allowhook);
    ci->callstatus |= CIST_YPCALL;  /* function can do error recovery */
    luaD_call(L, c.func, nresults);
    ci->callstatus &= ~CIST_YPCALL;
    L->errfunc = ci->u.c.old_errfunc;
    status = LUA_OK;
  }
  adjustresults(L, nresults);
  lua_unlock(L);
  return APIstatus(status);
}
```

保护机制的核心在 `luaD_pcall`(`ldo.c:1081`):

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
    L->ci = old_ci;
    L->allowhook = old_allowhooks;
    status = luaD_closeprotected(L, old_top, status);
    luaD_seterrorobj(L, status, restorestack(L, old_top));
    luaD_shrinkstack(L);   /* restore stack size in case of overflow */
  }
  L->errfunc = old_errfunc;
  return status;
}
```

`luaD_rawrunprotected`(`ldo.c:160`)用 `setjmp`(POSIX 下 `_setjmp`,更省)埋一个恢复点:

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

错误发生时,`luaD_throw`(`ldo.c` 里)调 `LUAI_THROW`(`longjmp`)跳回这个 `setjmp` 点,`status` 被设成错误码。`luaD_pcall` 捕获到错误后做三件清理:**恢复 `ci` 到调用前**(丢弃所有因错误而半途的帧)、**恢复 `allowhooks`**(防止 hook 在错误状态下还挂着)、**关掉栈上从 `old_top` 到 `top` 之间打开的 to-be-closed upvalue**(`luaD_closeprotected`,P4-15)、**把错误对象压到 `old_top`**(`luaD_seterrorobj`,运行时错误压 message、内存错误压固定 message、其他压原对象)、**缩栈**(`luaD_shrinkstack`,如果错误是栈溢出,把扩过的栈缩回去)。

`errfunc` 参数是错误处理函数的索引(0 表示不用);非 0 时,发生运行时错误会先调这个函数(典型用途:加 traceback,即 `luaL_traceback`)。它的位置用 `savestack` 存成偏移量——因为整个 pcall 期间栈可能 realloc,绝对地址不可靠,只能存偏移。

`lua_pcallk` 返回状态码:`LUA_OK`(0,成功)、`LUA_ERRRUN`(运行时错误)、`LUA_ERRMEM`(内存错误)、`LUA_ERRERR`(错误处理函数自己又错了)。宿主据此分支。

续体机制(`k`/`ctx`)是为协程服务的:如果被调的 Lua 函数中途 `coroutine.yield`,而当前 `lua_State` 又是可 yield 的(协程线程),普通的 C 调用栈没法跨 yield 续上(yield 会切走整个栈)。Lua 的解法是:让宿主提供一个续体函数 `k`,VM 在 yield 后恢复时,不回原来的 C 调用点(那已经无效了),而是调 `k(L, status, ctx)`——宿主在 `k` 里继续后续逻辑。`ci->u.c.k`/`ci->u.c.ctx` 就是存续体和上下文的地方。这套机制和 P6-20 协程的栈切换是配套的。当 `k == NULL` 或当前线程不可 yield 时,走 `f_call`+`luaD_pcall` 的常规保护路径(`luaD_callnoyield`,显式禁止 yield,保证 C 调用栈能直接返回)。

---

## 九、注册 C 函数给 Lua:CClosure 与 luaL_setfuncs

反向桥——让 Lua 调 C——是嵌入的另一根支柱。宿主有一个 C 函数,想暴露给 Lua 脚本当函数用。

Lua 规定所有可被 Lua 调用的 C 函数必须签名一致(`lua.h`):

```c
typedef int (*lua_CFunction) (lua_State *L);
```

约定:C 函数从 `L` 的栈读参数(参数从索引 1 开始),把返回值压栈,返回压了几个(`return n`)。`LUA_MULTRET` 不允许作返回值个数(C 函数必须明确告诉 VM 返回几个)。

注册单个函数最简的宏是 `lua_register(L, name, f)`(`lua.h:400`),展开是 `lua_pushcfunction(L, f)` + `lua_setglobal(L, name)`。`lua_pushcfunction` 又是 `lua_pushcclosure(L, f, 0)` 的宏(`lua.h:402`)——0 个 upvalue 的闭包。

`lua_pushcclosure`(`lapi.c:609`)的实现揭示了 Lua 怎么把 C 函数变成一个 Lua 值:

```c
LUA_API void lua_pushcclosure (lua_State *L, lua_CFunction fn, int n) {
  lua_lock(L);
  if (n == 0) {
    setfvalue(s2v(L->top.p), fn);
    api_incr_top(L);
  }
  else {
    int i;
    CClosure *cl;
    api_checkpop(L, n);
    api_check(L, n <= MAXUPVAL, "upvalue index too large");
    cl = luaF_newCclosure(L, n);
    cl->f = fn;
    for (i = 0; i < n; i++) {
      setobj2n(L, &cl->upvalue[i], s2v(L->top.p - n + i));
      /* does not need barrier because closure is white */
      lua_assert(iswhite(cl));
    }
    L->top.p -= n;
    setclCvalue(L, s2v(L->top.p), cl);
    api_incr_top(L);
    luaC_checkGC(L);
  }
  lua_unlock(L);
}
```

两类:**0 upvalue 时压的是 light C function**(`setfvalue`,直接把函数指针塞进 `TValue`,不分配对象,P1-02 讲过这种轻量编码);**有 upvalue 时分配一个 `CClosure` 对象**(`luaF_newCclosure`),把函数指针和栈顶 `n` 个值(作为 upvalue)拷进 `CClosure` 的 `upvalue` 数组,弹出这 `n` 个值,把新 `CClosure` 压栈。

`CClosure` 的定义在 `lobject.h:699`:

```c
#define ClosureHeader \
	CommonHeader; lu_byte nupvalues; GCObject *gclist

typedef struct CClosure {
  ClosureHeader;
  lua_CFunction f;
  TValue upvalue[1];  /* list of upvalues */
} CClosure;
```

注意 `upvalue[1]` 这个 C 的柔性数组技巧——实际分配时 `luaF_newCclosure`(`lfunc.h:15` 的宏)按 `offsetof(CClosure, upvalue) + sizeof(TValue) * n` 分配,所以 `upvalue` 数组实际有 `n` 个元素。`nupvalues` 记录个数,GC 遍历时据此知道要 trace 几个 upvalue。

C 闭包的 upvalue 在 C 函数内部用伪索引 `lua_upvalueindex(i)`(`lua.h:44`,等于 `LUA_REGISTRYINDEX - i`)访问——`index2value` 那段(`lapi.c:73-84`)已经看过:它从当前 `ci->func` 指向的 `CClosure` 的 `upvalue[i-1]` 取值。这样 C 函数就能拥有"创建时绑定的私有状态",实现有状态的 C 模块。

批量注册一组 C 函数用 `luaL_setfuncs`(`lauxlib.c:965`),这是写 C 模块的标准入口:

```c
LUALIB_API void luaL_setfuncs (lua_State *L, const luaL_Reg *l, int nup) {
  luaL_checkstack(L, nup, "too many upvalues");
  for (; l->name != NULL; l++) {  /* fill the table with given functions */
    if (l->func == NULL)  /* placeholder? */
      lua_pushboolean(L, 0);
    else {
      int i;
      for (i = 0; i < nup; i++)  /* copy upvalues to the top */
        lua_pushvalue(L, -nup);
      lua_pushcclosure(L, l->func, nup);  /* closure with those upvalues */
    }
    lua_setfield(L, -(nup + 2), l->name);
  }
  lua_pop(L, nup);  /* remove upvalues */
}
```

`luaL_Reg` 是 `{name, func}` 数组,以 `{NULL, NULL}` 结尾。用法是:宿主先 `luaL_newtable` 建一个表(模块表)、可选地压 `nup` 个共享 upvalue(所有函数都拿到同一份),然后调 `luaL_setfuncs` 把数组里每个函数注册成表的字段。`lua_setfield(L, -(nup + 2), l->name)` 那个 `-（nup + 2)` 是算出"模块表"在栈里的位置(下面 `nup` 个 upvalue + 1 个刚 push 的闭包,再往下 1 个就是表)。

一个典型 C 模块长这样:

```c
static int l_add (lua_State *L) {
  lua_Integer a = luaL_checkinteger(L, 1);
  lua_Integer b = luaL_checkinteger(L, 2);
  lua_pushinteger(L, a + b);
  return 1;
}

static int l_mul (lua_State *L) {
  lua_Integer a = luaL_checkinteger(L, 1);
  lua_Integer b = luaL_checkinteger(L, 2);
  lua_pushinteger(L, a * b);
  return 1;
}

static const luaL_Reg mylib[] = {
  {"add", l_add},
  {"mul", l_mul},
  {NULL, NULL}
};

int luaopen_mylib (lua_State *L) {
  luaL_newlib(L, mylib);  /* = luaL_newtable + luaL_setfuncs(nup=0) */
  return 1;
}
```

`luaL_newlib` 是 `lua.h`/`lualib.h` 里的宏,等于建表 + `luaL_setfuncs(L, reg, 0)`。`luaopen_mylib` 是模块入口,被 `require "mylib"` 时调到。

`luaL_check*`(`lauxlib.c:408/426/448` 等)是 C 函数读参数的标准姿势——既读又检查,类型不对直接抛错:

```c
LUALIB_API const char *luaL_checklstring (lua_State *L, int arg, size_t *len) {
  const char *s = lua_tolstring(L, arg, len);
  if (l_unlikely(!s)) tag_error(L, arg, LUA_TSTRING);
  return s;
}

LUALIB_API lua_Integer luaL_checkinteger (lua_State *L, int arg) {
  int isnum;
  lua_Integer d = lua_tointegerx(L, arg, &isnum);
  if (l_unlikely(!isnum))
    interror(L, arg);
  return d;
}
```

类型不符时,`tag_error` → `luaL_argerror` → `luaL_error`,后者把"bad argument #N (X expected, got Y)"格式化后 `lua_error` 抛出去。这个错误会被外层 `pcall` 捕获。所以 `luaL_check*` 的契约是:**要么返回正确类型的值,要么不返回(抛错)**——宿主代码可以写得非常直白,不用每次都判空。

---

## 十、注册表与长期引用:luaL_ref

注册表 `LUA_REGISTRYINDEX` 是一个全局的 Table(`G(L)->l_registry`),所有 C 代码共享。它的特殊之处是:**它的生命周期和 `lua_State` 一样长,且 GC 不会在正常周期里回收它持有的强引用**(除非显式设 nil)。所以宿主要把一个 Lua 值"长期持有"(跨多次 API 调用、跨多个 C 函数),注册表是唯一可靠的地方。

直接用 `lua_settable(L, LUA_REGISTRYINDEX, ...)` 可以,但要自己想 key。Lua 提供了 `luaL_ref`(`lauxlib.c:689`)做"分配整数引用号"这件事,把注册表当成一个自动管理的引用池:

```c
LUALIB_API int luaL_ref (lua_State *L, int t) {
  int ref;
  if (lua_isnil(L, -1)) {
    lua_pop(L, 1);  /* remove from stack */
    return LUA_REFNIL;  /* 'nil' has a unique fixed reference */
  }
  t = lua_absindex(L, t);
  if (lua_rawgeti(L, t, 1) == LUA_TNUMBER)  /* already initialized? */
    ref = (int)lua_tointeger(L, -1);  /* ref = t[1] */
  else {  /* first access */
    lua_assert(!lua_toboolean(L, -1));  /* must be nil or false */
    ref = 0;  /* list is empty */
    lua_pushinteger(L, 0);  /* initialize as an empty list */
    lua_rawseti(L, t, 1);  /* ref = t[1] = 0 */
  }
  lua_pop(L, 1);  /* remove element from stack */
  if (ref != 0) {  /* any free element? */
    lua_rawgeti(L, t, ref);  /* remove it from list */
    lua_rawseti(L, t, 1);  /* (t[1] = t[ref]) */
  }
  else  /* no free elements */
    ref = (int)lua_rawlen(L, t) + 1;  /* get a new reference */
  lua_rawseti(L, t, ref);
  return ref;
}
```

机制是一个**空闲链表**,复用已释放的引用号:`t[1]` 是链表头(下一个可用的空闲号或 0 表示空),`t[ref]` 在引用号 `ref` 被释放后,存的是"下一个空闲号"。`luaL_ref` 时:如果栈顶是 nil,返回特殊常量 `LUA_REFNIL`(表示"对 nil 的引用");否则从空闲链表摘一个号(没有就 `rawlen + 1` 新分配),把栈顶值存到 `t[ref]`,返回这个号。

`luaL_unref`(`lauxlib.c:716`)把号还回去,挂回空闲链表:

```c
LUALIB_API void luaL_unref (lua_State *L, int t, int ref) {
  if (ref >= 0) {
    t = lua_absindex(L, t);
    lua_rawgeti(L, t, 1);
    lua_assert(lua_isinteger(L, -1));
    lua_rawseti(L, t, ref);  /* t[ref] = t[1] */
    lua_pushinteger(L, ref);
    lua_rawseti(L, t, 1);  /* t[1] = ref */
  }
}
```

`rawgeti(L, t, ref)` 把引用的值取回栈顶。这套机制的 sound 之处:**引用号是整数,宿主可以安全存在 C 变量里(整数不会被 GC)**;而引用号指向的 Lua 值由注册表这个 Table 强引用着,GC 不会动它,直到宿主显式 `luaL_unref`。这给了宿主一种"在 C 侧持有 Lua 对象引用"的安全方式——不需要也不允许直接持有 `TValue *`。

注册表里还有几个固定的保留槽位:`LUA_RIDX_GLOBALS`(2,全局表 `_ENV` 的根)、`LUA_RIDX_MAINTHREAD`(3,主线程,协程恢复时用)。`getGlobalTable`(`lapi.c:691`)就是从注册表取 `LUA_RIDX_GLOBALS`。

---

## 十一、错误处理:lua_error 与 luaL_error

C 函数内部抛错有两条路。`lua_error`(`lapi.c:1252`)直接抛栈顶值作错误对象:

```c
LUA_API int lua_error (lua_State *L) {
  TValue *errobj;
  lua_lock(L);
  errobj = s2v(L->top.p - 1);
  api_checkpop(L, 1);
  /* error object is the memory error message? */
  if (ttisshrstring(errobj) && eqshrstr(tsvalue(errobj), G(L)->memerrmsg))
    luaM_error(L);  /* raise a memory error */
  else
    luaG_errormsg(L);  /* raise a regular error */
  /* code unreachable; will unlock when control actually leaves the kernel */
  return 0;  /* to avoid warnings */
}
```

注意 `return 0` 永远到不了——`luaG_errormsg` 会 `luaD_throw` 跳走。这个函数返回 int 只是为了让宿主写 `return lua_error(L);` 编译过(有些编译器警告"非 void 函数没返回值")。

特殊处理:如果错误对象恰好是全局的内存错误消息(`G(L)->memerrmsg`,一个预分配的字符串"not enough memory"),走 `luaM_error`——内存错误有专门的精简路径,因为此时可能连压错误对象的内存都没有了(P5 讲过 GC 的内存压力场景)。

`luaL_error`(`lauxlib.c:238`)是更常用的便捷版,带 `printf` 风格的格式化:

```c
LUALIB_API int luaL_error (lua_State *L, const char *fmt, ...) {
  va_list argp;
  va_start(argp, fmt);
  luaL_where(L, 1);  /* 压入 "source:line: " 前缀 */
  lua_pushvfstring(L, fmt, argp);
  va_end(argp);
  lua_concat(L, 2);
  return lua_error(L);
}
```

`luaL_where(L, 1)` 查当前调用层(第 1 层 = 调 `luaL_error` 的那个 C 函数的调用者)的调试信息,压一个 `"myfile.lua:42: "` 前缀,这样错误消息自带位置。`lua_pushvfstring` 用 `luaO_pushvfstring` 格式化,内部保证即使内存紧张也能工作(注释明说:"does not need reserved stack space when called. (At worst, it generates a memory error instead of the given message.)")。

所有这些错误,只要被调函数是在 `lua_pcall` 里跑的,都会被捕获,变成 `lua_pcall` 的返回状态码和栈顶的错误对象。**宿主用 `pcall` 包住一切不可信的脚本调用,是嵌入的基本纪律**。

---

## 十二、开标准库:luaL_openlibs 与选择性裁剪

一个新 `lua_State` 默认是"裸"的——没有任何标准库,连 `print`、`string.format`、`table.insert` 都没有。宿主要用,得显式开库。

5.5 的标准库开库逻辑全在 `linit.c`(整文件 60 多行):

```c
static const luaL_Reg stdlibs[] = {
  {LUA_GNAME, luaopen_base},
  {LUA_LOADLIBNAME, luaopen_package},
  {LUA_COLIBNAME, luaopen_coroutine},
  {LUA_DBLIBNAME, luaopen_debug},
  {LUA_IOLIBNAME, luaopen_io},
  {LUA_MATHLIBNAME, luaopen_math},
  {LUA_OSLIBNAME, luaopen_os},
  {LUA_STRLIBNAME, luaopen_string},
  {LUA_TABLIBNAME, luaopen_table},
  {LUA_UTF8LIBNAME, luaopen_utf8},
  {NULL, NULL}
};


LUALIB_API void luaL_openselectedlibs (lua_State *L, int load, int preload) {
  int mask;
  const luaL_Reg *lib;
  luaL_getsubtable(L, LUA_REGISTRYINDEX, LUA_PRELOAD_TABLE);
  for (lib = stdlibs, mask = 1; lib->name != NULL; lib++, mask <<= 1) {
    if (load & mask) {  /* selected? */
      luaL_requiref(L, lib->name, lib->func, 1);  /* require library */
      lua_pop(L, 1);  /* remove result from the stack */
    }
    else if (preload & mask) {  /* selected? */
      lua_pushcfunction(L, lib->func);
      lua_setfield(L, -2, lib->name);  /* add library to PRELOAD table */
    }
  }
  lua_assert((mask >> 1) == LUA_UTF8LIBK);
  lua_pop(L, 1);  /* remove PRELOAD table */
}
```

**这是 5.5 相对老资料(5.3/5.4)的一个硬演进点**。老版本的 `luaL_openlibs(L)` 是一个独立函数,要么全开要么不开。5.5 把它重写成 `luaL_openselectedlibs(L, load, preload)`,接受两个位掩码:`load` 的位表示"立即加载这个库",`preload` 的位表示"不立即加载,但把它注册到 `package.preload` 表,等脚本第一次 `require` 时才加载"(惰性加载)。`luaL_openlibs(L)` 在 `lualib.h:62` 被降级成一个调用 `luaL_openselectedlibs(L, ~0, 0)`(全立即加载、不 preload)的宏。

位掩码的位定义在 `lualib.h`:`LUA_BASELIBK`、`LUA_LOADLIBK`、`LUA_COLIBK`……每个库一个位,从 1 开始左移。注释明确"Must be listed in the same ORDER of their respective constants"——`stdlibs` 数组的顺序和位掩码的顺序必须严格对应,`lua_assert((mask >> 1) == LUA_UTF8LIBK)` 在收尾时校验这个不变式。

这个演进直接服务"小"的主线:**嵌入宿主可以按需裁剪标准库**。一个跑配置脚本的嵌入式设备,不需要 `io`(没有文件系统)、不需要 `os`(没有系统调用)、不需要 `debug`(危险),只开 `base`+`string`+`table`+`math` 就够。代码体积、攻击面、内存占用都随之降低。Redis 嵌入 Lua 时就是只开一部分库(并砍掉 `dofile`/`loadfile` 等危险函数),把 Lua 当成"受沙箱约束的表达式/事务脚本引擎"。这种"可裁剪的标准库"是 Lua 适合嵌入的又一个具体落点——内核和库严格分层,库完全是可选的。

`luaL_requiref`(`lauxlib.c:1006`)是开单个库的标准姿势:先查 `package.loaded` 表(防止重复开),没开过就调 `openf`(库的 `luaopen_*` 入口),把结果存进 `package.loaded`,可选地也存进全局表(`glb` 参数)。

---

## 十三、一个完整的嵌入闭环

把前面所有章节串起来,一个最小的嵌入宿主长这样。这是本章的"压轴串场":

```c
#include <lua.h>
#include <lualib.h>
#include <lauxlib.h>

/* 宿主提供的 C 函数,要暴露给 Lua */
static int l_greet (lua_State *L) {
  const char *name = luaL_checkstring(L, 1);        /* 读参数 */
  lua_pushfstring(L, "hello, %s!", name);            /* 压结果 */
  return 1;                                          /* 返回 1 个值 */
}

static const luaL_Reg host_lib[] = {
  {"greet", l_greet},
  {NULL, NULL}
};

int main (void) {
  lua_State *L = luaL_newstate();                    /* ① 建一个全新 VM */
  if (L == NULL) return 1;

  luaL_openlibs(L);                                  /* ② 开标准库 */

  /* ③ 把宿主函数注册成全局模块 host */
  luaL_newlib(L, host_lib);
  lua_setglobal(L, "host");

  /* ④ 跑一段脚本(里面会调宿主函数) */
  const char *script =
    "local msg = host.greet('world')\n"
    "return msg .. ' length=' .. #msg\n";

  if (luaL_dostring(L, script) != LUA_OK) {          /* dostring = load + pcall */
    fprintf(stderr, "error: %s\n", lua_tostring(L, -1));
    lua_close(L);
    return 1;
  }

  /* ⑤ 取返回值 */
  const char *result = lua_tostring(L, -1);
  printf("result: %s\n", result);                    /* hello, world! length=18 */

  lua_close(L);                                      /* ⑥ 销毁 VM */
  return 0;
}
```

每一步对应前面某一章:

- **① `luaL_newstate()`**(`lauxlib.c:1184`)内部调 `lua_newstate(luaL_alloc, NULL, luaL_makeseed(NULL))`(`lstate.c:336`)。注意 5.5 的 `lua_newstate` 是**三参数**`(lua_Alloc f, void *ud, unsigned seed)`——老资料讲的 5.4 是两参数,5.5 多了 `seed`(给字符串驻留哈希用的随机化种子,防哈希碰撞攻击,`luaL_makeseed` 用时间/地址等熵源生成)。`lua_newstate` 里做的事在 P0-01 / P1-02 已展开:分配 `global_State`、初始化主线程 `lua_State`、建注册表、初始化 GC 各链表和参数(`setgcparam` 设 PAUSE/STEPMUL/STEPSIZE 等,P5 详讲),最后 `luaD_rawrunprotected(L, f_luaopen, NULL)` 把栈和注册表搭起来——这一步本身就用 pcall 保护,防止初始化中途内存失败留下半截状态。`luaL_newstate` 还顺手 `lua_atpanic` 注册一个默认 panic 函数(未捕获错误时打印并 `exit`)、`lua_setwarnf` 打开警告。
- **② `luaL_openlibs(L)`** 上节已讲,5.5 是 `luaL_openselectedlibs(L, ~0, 0)` 的宏,全开。
- **③ 注册宿主函数**:`luaL_newlib` 建表 + `luaL_setfuncs`(第九节);`lua_setglobal` 把表挂到全局变量 `host`。注册的每个 C 函数会被包装成 light C function(0 upvalue,`setfvalue`)压进表的字段。从此 Lua 脚本写 `host.greet(...)` 就会进 `l_greet`。
- **④ `luaL_dostring`**(`lua.h` 宏)是 `luaL_loadstring` + `lua_pcall(..., 0, 0)` 两步。`load` 把源码编译成 `LClosure`(P2 全流程:词法→语法→代码生成→产出 Proto),压一个闭包到栈顶;`pcall` 调这个闭包,进 VM 解释器循环(P3-11),执行字节码。执行中遇到 `host.greet('world')`,VM 查全局表 `_ENV.host`(P4 讲过 `_ENV` 是第一个 upvalue,`lua_load` 在 `lapi.c:1133-1136` 把全局表设进闭包的第一个 upvalue),拿到 light C function,按 C 调用约定(P4-13)进 `l_greet`。`l_greet` 内部 `luaL_checkstring` 读参数、`lua_pushfstring` 压结果、`return 1` 告诉 VM 返回 1 个值。VM 把这 1 个值放回调用点,脚本继续。执行中任何 GC 工作(P5 增量三色)在字节码之间穿插进行,不打断脚本。如果脚本有错(语法错或运行时错),`pcall` 返回非 `LUA_OK`,错误对象压在栈顶——第十节讲过。
- **⑤ 取返回值**:脚本 `return msg .. ' length=' .. #msg` 返回一个字符串。`pcall` 成功后,栈顶就是返回值(`adjustresults` 把返回值规整到 `nresults=0`... 这里 `dostring` 实际期望 0 或多返回值,具体看宏版本;宿主 `lua_tostring(L, -1)` 取栈顶)。
- **⑥ `lua_close(L)`**(`lstate.c:391`)销毁整个 VM:跑一次最终 GC 回收所有对象、关闭所有还开着的 upvalue、调 `__gc` 终结器、释放 `global_State` 内存。从 `luaL_newstate` 到 `lua_close`,所有 Lua 占用的内存都经过 `luaL_alloc`(默认就是 `realloc`)这一根管子,宿主完全掌控内存。

这就是 Lua 嵌入闭环的全貌。注意它有多简洁:**一个 `lua_State *` 指针、十几个 API 调用,就是一个完整可用的脚本引擎实例**。没有进程、没有线程、没有共享库加载——宿主链接几个 `.c` 文件,调几个函数,完事。这正是 P0-01 讲的"一个 `lua_State` 就是一个 VM"在工程上的兑现。

---

## 十四、为什么这套设计是 sound 的

把 C API 的几个 sound 关键点收一下。

**栈式解耦保证类型安全**。所有读值过 `index2value`,越界返回 `nilvalue`、类型不符返回 0/NULL,宿主拿不到被错解释的 `TValue`。所有写值过 `set*value` 系列,由 VM 替宿主做正确的 tag 编码。宿主代码不可能因为"忘了初始化某个 `TValue` 字段"而让 VM 崩——因为宿主根本不构造 `TValue`。

**GC 管值,宿主不手动管引用计数**。栈上的值由 VM 拥有;压一个字符串,VM 驻留它;建一个表,VM 把它挂进 GC 链;`lua_pop` 弹掉一个值,它可能立刻被回收(如果没有别的引用),也可能在下一个 GC 周期回收。宿主从不需要写"释放这个 Lua 对象"的代码——这消除了 CPython 那种忘记 `Py_DECREF` 导致泄漏、或多 `DECREF` 导致 double-free 的整类 bug。代价是回收不如引用计数即时(对象不可达后不会立即释放,要等 GC 步进到它),但增量 GC 把停顿切得很细(P5-16),宿主感受不到。

**pcall 保护保证错误不逃逸出 VM**。任何脚本错误(运行时错、内存错、C 函数里 `luaL_error` 抛的错)都被 `luaD_pcall`+`setjmp` 捕获,变成一个状态码和一个错误对象压在栈上。宿主的 C 调用栈不会被打穿——`lua_pcall` 正常返回,宿主代码继续往下走(分支处理错误)。`luaD_pcall` 的清理(恢复 `ci`、关 to-be-closed、缩栈、压错误对象)保证错误发生后 VM 状态一致,可以继续用。唯一例外是 panic——当没有任何 pcall 包着时,错误进 panic 函数(默认 `exit`);但这是宿主的明确选择(没包 pcall 就是接受这个风险)。

**registry 提供安全的长期引用**。宿主要在 C 侧长期持有一个 Lua 值,用 `luaL_ref` 拿一个整数引用号。整数不会被 GC、不会失效,引用号指向的值被注册表强引用、GC 不动。这避免了"宿主持有 `TValue *`,GC 搬家后悬空"的整类问题。`luaL_ref` 的空闲链表复用机制保证引用号不无限增长。

**可裁剪库保证最小内核**。`luaL_openselectedlibs` 的位掩码设计(5.5 新增),让宿主精确控制开哪些标准库——内核和库严格分层,库完全是可选的。一个"只跑配置表达式"的设备可以只开 `base`+`math`,代码体积和攻击面都大幅缩小。这服务"小"的主线:Lua 的体积不是固定的,而是宿主按需选择的结果。

**C 闭包 upvalue 给 C 模块状态**。`CClosure` 的 `upvalue` 数组让 C 函数拥有创建时绑定的私有状态,不必靠全局变量(线程不安全)或 `static`(不可重入)。多个 C 函数共享同一组 upvalue(`luaL_setfuncs` 的 `nup` 参数),实现"模块级共享配置"。upvalue 由 GC 管,模块卸载时自动回收。

---

## 十五、★对照 CPython

把 Lua 的 C API 和 CPython 的 C API 放一起看,是理解 Lua 取舍的最佳角度。

**根本差异:栈式 vs 直接指针**。

Lua 的 API 一切操作经过虚拟栈:`lua_pushinteger(L, 42)` 把 42 压栈,`lua_tointeger(L, -1)` 从栈顶取。宿主从不直接持有 Lua 对象的 C 指针(除了 light userdata 这种逃逸口子)。值一旦进栈,生命周期由 GC 管。

CPython 的 C API 是直接指针风格:宿主拿到的是 `PyObject *`,直接操作这个指针。要建一个整数,调 `PyLong_FromLong(42)`,拿到一个 `PyObject *`;要把这个整数塞进一个 list,调 `PyList_Append(list, obj)`。每个返回 `PyObject *` 的 API 都会**增加这个对象的引用计数**(`Py_INCREF`),宿主用完后必须**配对减少**(`Py_DECREF`),否则就泄漏。

两种风格的直接后果:

| 维度 | Lua 5.5 栈式 API | CPython 直接指针 API |
|---|---|---|
| **值生命周期** | 由 GC 管,宿主不手动释放 | 引用计数,宿主必须配对 `Py_INCREF`/`Py_DECREF`,漏配就泄漏或 double-free |
| **GC** | 增量三色标记,可中断(P5) | 引用计数为主(对象不可达即回收)+ 分代标记(周期性处理环引用) |
| **类型安全** | `lua_to*` 越界/类型不符返回哨兵 | 类型不符通常 `segfault` 或 `assertion`(靠 `Py_TYPE(obj) == &PyLong_Type` 这类手动检查) |
| **栈/堆关系** | 宿主只操作虚拟栈,VM 内部栈/堆隔离 | 宿主持有的 `PyObject *` 就是堆上真实对象,GC 直接管它 |
| **典型 bug** | 忘了 `lua_pop` 导致栈堆积(易查,栈深度对不上) | 忘了 `Py_DECREF` 导致内存泄漏(难查,引用计数对不上)或 double-free(崩溃) |
| **错误传播** | `pcall`+`setjmp` 捕获,返回状态码 | 错误用"异常对象 + NULL 返回值"传播,宿主每个调用后查 `PyErr_Occurred()` |
| **性能** | 每次传值多一次栈拷贝 | 直接指针,零拷贝,但每次赋值有 `Py_INCREF`/`Py_DECREF` 的原子或非原子开销 |

CPython 的直接指针风格给了宿主最大的灵活性(可以直接构造任意 `PyObject` 树、可以和 C 库无缝互操作),代价是**引用计数管理是宿主的责任**。CPython 扩展模块的 bug,绝大多数是引用计数配对错误——C API 没法在编译期检查这件事,全靠开发者纪律和运行时调试工具(`python -X dev`、`PYTHONMALLOC=debug`)。而且引用计数处理不了循环引用,还得一套分代标记 GC 周期性跑(那是一次 STW),对"嵌入宿主不能长时间卡顿"不如 Lua 的增量 GC 友好(P0-01 对照表已点)。

Lua 的栈式风格牺牲了直接指针的灵活性(每次传值要压栈/读栈、栈深度有限),换来:**宿主不可能写出引用计数 bug**(根本就没有引用计数这回事)、**类型安全靠 VM 兜底**(哨兵而非崩溃)、**值生命周期完全由 GC 自动管理**(宿主零负担)。这套取舍对"嵌入"这个目标极其对路——宿主开发者不必成为 GC 专家,也能安全地用 Lua。

栈式 API 还有一个 CPython 没有的好处:**天然线程隔离**。一个 `lua_State` 的栈是这个线程私有的,多线程要跑多个 Lua,各自建 `lua_State`(可共享 `global_State`),互不干扰。CPython 的 `PyObject *` 是全局可见的,加上 GIL 的存在,多线程用 CPython 扩展要处处小心 GIL 的获取释放(`PyGILState_Ensure`)。Lua 没有 GIL,线程隔离在 `lua_State` 层面就做到了(P6-20 协程也是基于这套隔离)。

---

## 十六、回扣全书主线

本书开头讲 Lua 用三招化解"小 ↔ 全/快"的张力:**统一的 Table、寄存器式字节码、增量 GC**。现在走到 C API 这章,可以看到这三招最终都在"嵌入闭环"里汇合:

- **统一的 Table** 让注册表、全局表、模块表、对象元表都是同一个 `Table` 结构——C API 操作它们用的是同一套 `gettable`/`settable`/`rawget`/`rawset`,代码量极小。如果 Lua 像 CPython 那样有 `list`/`dict`/`tuple`/`set` 分立,C API 就得为每种类型一套操作函数,膨胀数倍。
- **寄存器式字节码**让 `lua_pcall` 调一个 Lua 函数时,VM 执行效率高(P3-12 对照过 CPython 栈式字节码的指令数劣势)——宿主调 Lua 不至于慢到不可用,这是"能嵌入实战"的前提。
- **增量 GC** 让宿主跑脚本时,GC 停顿被切细到几乎不可见——游戏引擎每帧 16ms、Redis 单线程事务,都能容忍。如果 GC 是 STW 的(像 CPython 的分代标记周期),宿主根本不敢在关键路径上嵌 Lua。

而 C API 本身又是第四招:**栈式解耦**。这一招把宿主和 VM 内部表示完全隔离——宿主不碰 `TValue`、不管引用计数、不直接持有 GC 对象。这一招让"嵌入"这件事变得安全到可以放心做:宿主开发者不需要读完这本书(P1 到 P5 的所有内部机制)就能用 Lua,因为 C API 把这些复杂性全封装在栈后面了。

这就是 Lua 的"统一与精简换小而快"在嵌入层面的兑现:**用更少的机制(一个 `lua_State` + 一套栈式 API),换到完整的双向互操作能力(宿主调 Lua、Lua 调宿主、错误受保护、值自动回收、库可裁剪)**。一个 `lua_State` 加一套栈式 API,就是宿主与脚本之间的一座完整双向桥——这是全书执行侧的压轴,也是 Lua 之所以能塞进任何宿主的最后一公里。

全书到此,编译侧(P2)、执行侧(P3–P6)的二分法已经走完一圈。把所有章节连起来,会得到一张完整的"Lua 虚拟机怎么造出来"的全景脉络——这正是[附录A 全景脉络](附录A-全景脉络.md)要做的事:把 P0 到 P6 的每一条线索(值的表示、Table、字节码、解释器、调用栈、闭包、GC、元表、协程、C API)再串一遍,看它们怎么共同支撑起"统一与精简换小而快"这条主线。

---

*全书正文到此结束。接下来读[附录A 全景脉络](附录A-全景脉络.md)看主线收束,或[附录B 源码阅读路线](附录B-源码阅读路线.md)按推荐顺序重读 lua-5.5.0 源码。*
