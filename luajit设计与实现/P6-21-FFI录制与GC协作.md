# P6-21 FFI 录制 + GC 与 JIT 协作

> **本书主线**:把动态执行安全变成机器码。**二分法**:解释器侧 ↔ JIT 侧。**本章位置**:JIT 侧(P6)压轴。**★对照**:官方 Lua(纯解释器 + 增量 GC,无 JIT)+ JVM(JNI 不能被 JIT 内联,GC 分代/并发)。**源码**:LuaJIT 2.1.ROLLING。**基调**:纯直球,不用比喻;从第一性原理一步步推导。

---

## 引子:压轴两件事

上一章(P6-20)讲清楚了 FFI 在解释器侧怎么工作:你写 `ffi.cdef` 声明 C 函数签名,LuaJIT 用 `lj_ctype` 建模类型、用 `lj_ccall` 按调用约定真正去调那个 C 函数。那套机制管用,但有一个前提——它走的是解释器路径:每次调用都进 `lj_ccall_func`(`lj_ccall.c:1225`),走 `ccall_set_args` 把 Lua 栈上的参数一个个翻译成 `CCallState` 里的寄存器副本,再走手写汇编 `lj_vm_ffi_call` 真正调过去。这条路径比传统 Lua C API 快很多,但它**不是机器码**——每一次调用,都要重新走一遍这套 C 函数。

而本书从头到尾讲的就是一件事:**把热点代码变成机器码**。那么问题来了:

> 如果一段热循环里,恰好调了一个 C 函数(比如 `C.printf`、`C.write`、或者你自己用 C 写的高性能内核),这次 FFI 调用,能也变成机器码、被 JIT 编译进 trace 吗?

这是本章的第一根支柱,也是全书性能拼图里最后缺的一块:**FFI 录制**。如果能,那么"把动态执行变成机器码"这条主线,就**延伸到了 C 边界之外**——不仅 Lua 内部的算术、循环能跑机器码,连"Lua 调 C 函数"这个跨界动作,也能跑成接近原生 C 调 C 的机器码。trace 不会在调 C 时断裂。

第二根支柱,是压轴必须回答的一个一直被悬置的问题:**GC 和 JIT 怎么协作**。

前面 P1-04 讲过,LuaJIT 的对象(字符串、table、函数、cdata……)都挂在 GC 上,GC 用增量三色标记清扫来回收它们。但 JIT 引入了一类全新的对象——**trace**。一条 trace 不是普通的数据,它内部装着 IR 指令数组、snapshot 数组、还有一段**可执行的机器码**(`mcode`)。这些都要占用内存,而且 trace 本身可能引用别的 GC 对象(比如 IR 里嵌着指向某个 GCstr 的常量 `IR_KGC`)。那么:

- trace 是 GC 对象吗?GC 会回收它吗?
- trace 里引用的那些 GC 对象(字符串、cdata),GC 标记时会不会漏掉?
- JIT 编译出来的机器码内存(`mcode`),谁来释放?
- 一个 trace 正在跑(机器码在 CPU 上执行),这时候 GC 想移动或者回收对象,会不会出事?

这些问题听起来琐碎,但每一个都关系到正确性:**GC 漏标一个被 trace 引用的对象,就会把还在用的对象回收掉,机器码下一次执行就踩到已释放内存,崩溃**。所以"GC 与 JIT 协作"不是性能优化,而是 soundness 的硬要求。

本章就讲透这两根支柱:①FFI 调用怎么被录制成 IR、生成机器码(`lj_crecord.c`);②GC 怎么追踪 trace、怎么和正在运行的 JIT 代码和平共处(`lj_gc.c` + `lj_jit.h` 的 GCtrace)。讲完之后,我们会在最后一节把全书所有章节串起来,回扣那条贯穿始末的主线。

---

## §1 第一性原理:热点里的 FFI 调用,能编译吗

我们从最基础的地方开始,一步步把"FFI 录制为什么可能、为什么必要"推导出来。

### 1.1 回忆:trace 录制到底在录什么

P2-06 讲过 trace 录制(`lj_record.c`)。LuaJIT 发现一个循环够热之后,进入"录制模式":让解释器把这段循环体**再跑一遍**,但这一次,解释器每执行一条字节码,LuaJIT 就**同时**把它翻译成一条 IR 指令,追加到正在构建的 trace 的 IR 数组里。

这里有一个关键概念:录制时,解释器是在**真实地执行**这条字节码——它会真的去取栈上的值、真的做加法、真的把结果写回。但与此同时,录制器(`lj_record.c`)在旁边**影子般地**把这些操作翻译成 IR。比如解释器执行 `ADD` 字节码,`lj_record` 就 emit 一条 `IR_ADD`。

那遇到"调用"呢?Lua 里调一个函数,字节码是 `CALL`。如果调的是 Lua 函数,录制器可以选择**内联**这条调用(把被调函数的体也录进同一条 trace);如果调的是 C 函数(通过 Lua C API 注册的),录制器通常**没法内联**——因为 C 函数的实现是个黑盒,录制器看不见它内部干了什么,只能让解释器真的去调它,然后录一个"这里调了个 C 函数"的标记,trace 在这里要么终止,要么 stitch(缝补)。

这就引出一个根本的差别。

### 1.2 普通 C 函数和 FFI C 函数的本质差别

普通 Lua C API 注册的 C 函数(比如 `print`、`table.insert`),它的签名是固定的:`int (*)(lua_State *L)`。它接收一个 Lua 状态,从 Lua 栈上取参数,处理后把结果压回栈。Lua 不知道这个函数内部要取几个参数、是什么类型——这些信息藏在 C 函数的实现里,对 Lua 是黑盒。所以录制器遇到这种调用,顶多能录"调用了这个 C 函数指针",参数怎么从 Lua 值栈搬到 C 函数,全靠 C 函数自己用 `luaL_check*` 系列去取。这条搬运路径是固定的、有开销的,JIT 没法优化掉。

但 **FFI 的 C 函数不一样**。P6-20 §2 讲过,`ffi.cdef` 在解析时,已经把这个 C 函数的**完整签名**存进了类型表:返回类型是 `int`、第一个参数是 `const char *`、第二个参数是 `double`、是不是变长参数、调用约定是 cdecl 还是 stdcall……**全部已知,而且是静态的**(在 `ffi.cdef` 那一刻就定死了)。

这就打开了一扇门:**既然录制时能从类型表查到这个 C 函数的完整签名,那录制器就能精确地知道——第一个参数该转成什么 C 类型、放哪个寄存器;第二个参数该转成什么、放哪个寄存器;返回值从哪个寄存器取、转成什么 Lua 类型。** 它完全可以生成确定的 IR:一组类型转换 IR(把 Lua 值转成 C 调用约定要求的形状)+ 一条"调用"IR(指定函数地址和调用约定)。后端拿到这些 IR,直接生成"装载寄存器 + call 那个地址"的机器码——和 C 编译器为等价的 C 代码生成的机器码,几乎一模一样。

这就是 FFI 录制的核心洞察:

> **普通 C 函数是黑盒(签名对 Lua 隐藏),所以 JIT 看不进去;FFI 的 C 函数签名在 `ffi.cdef` 时已完全确定,所以录制器能把它精确地录成 IR,JIT 能把它编译成接近原生的机器码。**

类型已知,是 FFI 能被 JIT 的根本前提。这和 P0-01 §6 讲的热点检测、P2-08 讲的类型窄化一脉相承:LuaJIT 的 JIT 处处依赖"运行时观察到的类型信息",而 FFI 的类型信息是 `ffi.cdef` 直接给的,比运行时观察还确定。

### 1.3 把一次 FFI 调用拆成 IR

我们用一个最小例子把这件事看清楚。假设:

```lua
ffi.cdef("double sqrt(double x);")   -- 声明 C 标准库 sqrt
local C = ffi.C

local s = 0
for i = 1, 1000000 do
  s = s + C.sqrt(s + 1.0)            -- 热循环里调 sqrt
end
```

`C.sqrt` 出现在跑 100 万次的循环里,是热点。LuaJIT 会录制这个循环体。循环体里有两件事:`s + 1.0`(普通 Lua 加法)、`C.sqrt(...)`(FFI 调用)。前者 P2-06 讲过,录成 `IR_ADD`。后者就是本章主角。

录制器在录 `C.sqrt(...)` 时,从类型表查出:`sqrt` 是 `double (double)`,返回 double、一个 double 参数、POSIX x64 上这个参数走 `xmm0`、返回值也在 `xmm0`。于是它生成这样的 IR(概念示意,实际 IR 操作码稍后看源码):

```
; 假设 s+1.0 的结果已经在某个 IR ref,记作 %1
; 录制 C.sqrt:
  %2 = CONV  %1  (num → double, 通常已经是 double,可能 no-op)
  %3 = CALLXS <double>  args=(%2)  func=<sqrt 地址>
; %3 就是 sqrt 的返回值(double),后续 s = s + %3 录成 IR_ADD
```

`CALLXS` 是 LuaJIT IR 里专门给"调用一个运行时才知道地址的函数"用的操作码(`lj_ir.h:147`)。它的第一个操作数是一棵由 `CARG` 串起来的参数树,第二个操作数是函数地址(一个 IR 常量,或者从某个 cdata 里 FLOAD 出来的指针)。

后端(`lj_asm.c`)看到 `CALLXS`,就生成对应的机器码:把参数装进调用约定要求的寄存器(x64 POSIX 下 double 进 `xmm0`)、`call` 那个函数地址、从 `xmm0` 取返回值。**整个过程不再经过 `lj_ccall_func` / `ccall_set_args` / `lj_vm_ffi_call` 那套 C 代码**——机器码直接就是几条 `movsd` + 一条 `call`。这就是 FFI 调用被 JIT 编译后的样子,和 C 里直接写 `sqrt(s+1.0)` 编译出来的机器码,几乎没有差别。

### 1.4 为什么这样极致地快

把解释器路径和 JIT 路径摆在一起对比,就明白为什么"FFI 能 JIT"是几十倍性能差距的关键一环。

**解释器路径(P6-20 讲的,每次调用):**
1. 进 `lj_ccall_func`,取函数类型、取函数地址。
2. `ccall_set_args` 遍历每个参数:查参数类型、决定进 GPR 还是 FPR 还是栈、调 `lj_cconv_ct_tv` 做类型转换、写进 `CCallState` 的对应槽。
3. 调 `lj_vm_ffi_call` 汇编:把 `CCallState` 里的副本搬进物理寄存器、`call`。
4. `ccall_get_results`:把返回值从 `CCallState` 转回 Lua 值压栈。
5. 触发 `lj_gc_check`。

每一步都是 C 函数调用,加起来一次 `sqrt` 调用可能几十上百纳秒,其中相当部分是"准备参数"的开销。

**JIT 路径(被编译后,每次循环):**
1. 机器码里直接是 `movsd xmm0, [s 所在寄存器]`(参数已经在寄存器里,因为寄存器分配把它留在那了)。
2. `call <sqrt 地址>`。
3. `movsd [s], xmm0`(返回值直接用)。

没有 `lj_ccall_func`,没有 `ccall_set_args`,没有 `CCallState`,没有 `lj_vm_ffi_call`。寄存器分配把参数和返回值都安排在了调用约定要求的寄存器里,机器码就是赤裸的几条指令。开销逼近一次原生 C 函数调用(几个纳秒)。在跑 100 万次的循环里,这是几十倍的差距。

这就是 FFI 录制的价值。但要实现它,录制器要做两件不平凡的事:**(a)正确地把每个参数从 Lua 值的 IR 形式,转换成 C 调用约定要求的 IR 形式;(b)生成正确的 CALLXS IR,带上正确的函数地址和调用约定信息**。下一节我们就钻进 `lj_crecord.c`,看源码怎么做到这两件事,以及为什么它生成的机器码遵守 C ABI(soundness)。

---

## §2 lj_crecord.c:把 FFI 调用录成 IR

`lj_crecord.c` 是 FFI 录制的全部所在,2007 行。它和 `lj_record.c`(普通字节码录制)、`lj_ffrecord.c`(快速函数录制)并列,是 trace 录制器的三个分舵。本节聚焦它最核心的一个函数:`crec_call`——录制一次 FFI 函数调用。

### 2.1 入口:recff_cdata_call

Lua 里调 `C.sqrt(...)` 或 `cd:method(...)`,在字节码层面都是"对一个 cdata 做 CALL 操作"。LuaJIT 把这种调用分发到 `recff_cdata_call`(`lj_crecord.c:1335`):

```c
void LJ_FASTCALL recff_cdata_call(jit_State *J, RecordFFData *rd)
{
  CTState *cts = ctype_ctsG(J2G(J));
  GCcdata *cd = argv2cdata(J, J->base[0], &rd->argv[0]);
  CTypeID id = cd->ctypeid;
  CType *ct;
  cTValue *tv;
  MMS mm = MM_call;
  if (id == CTID_CTYPEID) {
    id = crec_constructor(J, cd, J->base[0]);
    mm = MM_new;
  } else if (crec_call(J, rd, cd)) {
    return;
  }
  /* Record ctype __call/__new metamethod. */
  ct = ctype_raw(cts, id);
  tv = lj_ctype_meta(cts, ctype_isptr(ct->info) ? ctype_cid(ct->info) : id, mm);
  ...
}
```

抓住主脉络。第一步 `argv2cdata(J, J->base[0], ...)` 把"被调用的东西"识别成一个 cdata(P6-20 §3 讲过,cdata 是 C 值在 Lua 里的化身)。注意 `argv2cdata` 内部做了一件 JIT 特有的事:

```c
static GCcdata *argv2cdata(jit_State *J, TRef tr, cTValue *o)
{
  GCcdata *cd;
  TRef trtypeid;
  if (!tref_iscdata(tr))
    lj_trace_err(J, LJ_TRERR_BADTYPE);
  cd = cdataV(o);
  /* Specialize to the CTypeID. */
  trtypeid = emitir(IRT(IR_FLOAD, IRT_U16), tr, IRFL_CDATA_CTYPEID);
  emitir(IRTG(IR_EQ, IRT_INT), trtypeid, lj_ir_kint(J, (int32_t)cd->ctypeid));
  return cd;
}
```

它 emit 了两条 IR:一条 `IR_FLOAD`(从 cdata 对象里读出 `ctypeid` 字段,即"这个 cdata 是哪种 C 类型"),一条 `IR_EQ` guard(`lj_crecord.c:57`)——**检查运行时这个 cdata 的 ctypeid 等于录制时观察到的那个值**。这就是 P0-01 §7 讲的 guard 在 FFI 录制里的具体应用:录制时观察到"这次调的是 sqrt 函数指针 cdata,ctypeid 是某某",就乐观假设"以后每次调的都是这个 ctypeid 的 cdata",并在机器码里插检查;一旦某次 ctypeid 变了(比如你把 `cd` 换成了另一个函数指针),guard 触发,side exit 退回解释器。这保证了 FFI 录制的乐观假设是**被检查的**,不会偷偷失效。

`argv2cdata` 之后,`recff_cdata_call` 分情况:如果这个 cdata 是 `CTID_CTYPEID`(即它是个"类型对象",比如 `ffi.new` 的第一个参数 `"int"`),走 `crec_constructor` 录制构造(下一节 `crec_alloc`);否则调 `crec_call(J, rd, cd)` 试着把它当函数指针调。`crec_call` 返回 1 表示成功录制成了一次 C 调用,直接 return。

### 2.2 crec_call:录制一次 C 函数调用的核心

`crec_call`(`lj_crecord.c:1264`)是本章最关键的函数。我们逐段读:

```c
static int crec_call(jit_State *J, RecordFFData *rd, GCcdata *cd)
{
  CTState *cts = ctype_ctsG(J2G(J));
  CType *ct = ctype_raw(cts, cd->ctypeid);
  CTInfo info;
  IRType tp = IRT_PTR;
  if (ctype_isptr(ct->info)) {                 /* 如果 cdata 是个指针 */
    tp = (LJ_64 && ct->size == 8) ? IRT_P64 : IRT_P32;
    ct = ctype_rawchild(cts, ct);              /* 解引用拿指向的函数类型 */
  }
  info = ct->info;  /* crec_call_args may invalidate ct pointer. */
  if (ctype_isfunc(info)) {
    TRef func = emitir(IRT(IR_FLOAD, tp), J->base[0], IRFL_CDATA_PTR);
    CType *ctr = ctype_rawchild(cts, ct);      /* 返回类型 */
    CTInfo ctr_info = ctr->info;  /* crec_call_args may invalidate ctr. */
    IRType t = crec_ct2irt(cts, ctr);
    TRef tr;
    TValue tv;
    /* Check for blacklisted C functions that might call a callback. */
    tv.u64 = ((uintptr_t)cdata_getptr(cdataptr(cd), ...) | U64x(800000000, 00000000));
    if (tvistrue(lj_tab_get(J->L, cts->miscmap, &tv)))
      lj_trace_err(J, LJ_TRERR_BLACKL);
    ...
```

逐字段读。第一段处理"指针包装":FFI 函数指针 cdata,通常是一个 `CT_PTR` 指向 `CT_FUNC`。所以先用 `ctype_rawchild` 解一层,拿到真正的函数类型 `ct`。`info = ct->info` 在解引用**之后**取,而且源码特意注释"crec_call_args may invalidate ct pointer"——这是因为 `crec_call_args` 里会调 `lj_ccall_ctid_vararg`,后者会 `lj_ctype_intern` 新建类型,可能让类型表 realloc,从而 `ct` 这个**指针**失效(但 `info` 是值拷贝,没事)。这是 P6-20 §5.1 讲过的"用 ID 而非指针引用内部对象"在录制侧的体现。

接下来是关键的三步:

**第一步:取函数地址。** `func = emitir(IRT(IR_FLOAD, tp), J->base[0], IRFL_CDATA_PTR)`。这是从 cdata 对象里 FLOAD 出那个函数指针(P6-20 讲过,cdata 的 payload 紧跟在 GCcdata 头后面,`IRFL_CDATA_PTR` 就是 payload 里那个指针)。这个 `func` 是一个 IR ref,运行时它持有真正的 C 函数地址。

**第二步:取返回类型,转成 IRType。** `ctr = ctype_rawchild(cts, ct)` 拿返回类型,`crec_ct2irt(cts, ctr)` 把 C 类型转成 IR 的类型枚举(`int` → `IRT_INT`、`double` → `IRT_NUM`、`void *` → `IRT_PTR`……)。`crec_ct2irt` 在 `lj_crecord.c:99`,逻辑直白:枚举当整数、浮点按位宽分 float/double、指针按 32/64 位分 P32/P64。这个 IRType 决定了 `CALLXS` 这条 IR 的返回类型,后端据此知道从哪个寄存器取返回值(整数从 `rax`、浮点从 `xmm0`)。

**第三步:黑名单检查。** 这是一个精妙的安全机制。注释写"Check for blacklisted C functions that might call a callback"。什么意思?如果一个 C 函数内部会回调 Lua(比如它接受一个函数指针参数,LuaJIT 把一个 Lua 函数包装成 C 回调传进去),那么这个回调可能触发 GC、可能修改 Lua 栈状态——而 trace 在跑机器码时,这些状态是**假设不变**的。如果让这种"会回调"的 C 函数进 trace,trace 里的假设就可能被回调偷偷打破,soundness 没了。

所以 LuaJIT 在解释器侧 `lj_ccall_func` 里(`lj_ccall.c:596`),一旦发现某个 C 函数真的触发了回调(`cts->cb.slot != ~0u`),就把这个函数**拉黑**——记进 `cts->miscmap` 这个表。录制时 `crec_call` 这里查这个黑名单,发现是黑名单函数就 `lj_trace_err(J, LJ_TRERR_BLACKL)` 终止录制。这是 FFI 录制 soundness 的第一道防线:**会回调的 C 函数,不让进 trace**。

继续读 `crec_call` 的中段:

```c
    if (ctype_isvoid(ctr_info)) {
      t = IRT_NIL;
      rd->nres = 0;
    } else if (!(ctype_isnum(ctr_info) || ctype_isptr(ctr_info) ||
                 ctype_isenum(ctr_info)) || t == IRT_CDATA) {
      lj_trace_err(J, LJ_TRERR_NYICALL);     /* 返回 struct/union 等:还不支持 */
    }
    if ((info & CTF_VARARG)
#if LJ_TARGET_X86
        || ctype_cconv(info) != CTCC_CDECL
#endif
        )
      func = emitir(IRT(IR_CARG, IRT_NIL), func,
                    lj_ir_kint(J, ctype_typeid(cts, ct)));
```

这段处理两件事。第一,**返回类型必须是简单类型**(number/pointer/enum/void)。如果返回 struct/union(`t == IRT_CDATA` 表示没法转成 IR 类型),直接 `LJ_TRERR_NYICALL` 终止——LuaJIT 的 FFI 录制**不录制返回聚合类型的 C 调用**(这种情况退回解释器走 `lj_ccall_func` 的完整路径)。这是个有意的限制:struct 返回的 ABI 规则太复杂(各架构不同,见 P6-20 §5.3),录制它生成正确 IR 的工程量大、收益小,所以 LuaJIT 选择不录。

第二,**变长参数或非默认调用约定,要把类型信息塞进函数 IR**。看那个 `if ((info & CTF_VARARG) ...)` 分支:如果是变长参数函数(像 `printf`),或者(x86 上)用了 fastcall/thiscall/stdcall 这些非 cdecl 调用约定,录制器会把函数指针 `func` 用 `IR_CARG` 和一个"类型 ID 常量"绑在一起。为什么?因为后端生成机器码时,需要知道这个调用的调用约定(决定寄存器分配、栈清理)和是不是变长参数(决定变长参数的默认提升)。普通 cdecl 非变长参数函数,这些信息是默认的,不用额外带;但变长参数和非 cdecl 必须显式带上类型 ID,后端 `asm_callx_flags` 会从这个 ID 反查信息(`lj_asm.c:1423`,下一节看)。

接着是最关键的两行——真正 emit CALLXS:

```c
    tr = emitir(IRT(IR_CALLXS, t), crec_call_args(J, rd, cts, ct), func);
```

`crec_call_args` 返回一棵由 `IR_CARG` 串起来的参数树(下一小节细讲),作为 `CALLXS` 的 op1;`func`(函数地址,可能带类型 ID)作为 op2;`t` 是返回类型。这一条 IR 就是"调用这个函数、传这些参数、返回类型是 t"的完整表达。后端会把它编译成"装载寄存器 + call"的机器码。

最后一段处理返回值的后加工:

```c
    if (ctype_isbool(ctr_info)) {
      ...  /* bool 返回:emit guard 检查 != 0 */
      crec_snap_caller(J);
      ...
      J->postproc = LJ_POST_FIXGUARDSNAP;
      tr = TREF_TRUE;
    } else if (t == IRT_PTR || (LJ_64 && t == IRT_P32) ||
               t == IRT_I64 || t == IRT_U64 || ctype_isenum(ctr_info)) {
      TRef trid = lj_ir_kint(J, ctype_cid(info));
      tr = emitir(IRTG(IR_CNEWI, IRT_CDATA), trid, tr);   /* 装回 cdata */
      if (t == IRT_I64 || t == IRT_U64) lj_needsplit(J);
    } else if (t == IRT_FLOAT || t == IRT_U32) {
      tr = emitconv(tr, IRT_NUM, t, 0);                   /* 提升成 Lua number */
    } else if (t == IRT_I8 || t == IRT_I16) {
      tr = emitconv(tr, IRT_INT, t, IRCONV_SEXT);         /* 符号扩展成 int */
    } else if (t == IRT_U8 || t == IRT_U16) {
      tr = emitconv(tr, IRT_INT, t, 0);                   /* 零扩展成 int */
    }
    J->base[0] = tr;
    J->needsnap = 1;
    return 1;
```

C 函数返回的原始值,是按 C 类型存的(可能是 1 字节 bool、4 字节 int、8 字节 double、8 字节指针)。但 Lua 值栈上要的是 Lua 类型(LuaJIT 内部 `intV`/`numV`/cdata)。所以返回值要做一道"从 C 类型 → Lua 类型"的转换,这正是 P6-20 §6 `lj_cconv_tv_ct` 的 IR 版本:

- **bool 返回**:C 函数返回 0/1 字节,Lua 里要的是 `true`/`false`。这里 emit 一个 `IR_NE` guard(检查返回值 != 0),并 `crec_snap_caller` 给调用者加个 snapshot——因为如果调用者忽略了返回值(比如 `C.foo()` 而不是 `x = C.foo()`),就不用检查;反之要正确转成 bool。`LJ_POST_FIXGUARDSNAP` 是个后处理标记,等 trace 录完后回头补全这个 guard 的 snapshot。
- **指针/64 位整数/枚举返回**:这些在 Lua 里没法用普通 number 装(64 位整数会丢精度,指针需要带类型),所以 emit `IR_CNEWI`——**新建一个 cdata 把这个值包起来**。`IR_CNEWI` 是"创建 cdata 并立即初始化一个值"的 IR(`lj_ir.h:124`),它的 op1 是 ctypeid(决定 cdata 是什么 C 类型),op2 是要装的值。这就是 FFI 录制里"返回值重新装箱成 cdata"的机制。注意 `lj_needsplit(J)`——64 位值在 32 位平台上要拆成高低两半(LuaJIT 的 split 优化,P3-12),这里标记一下。
- **float/u32 返回**:emit `IR_CONV` 转成 `IRT_NUM`(Lua 的 number 是 double,float 要提升,double 已经是 num 就 no-op,u32 在 LuaJIT 里也按 num 存避免装箱)。
- **i8/i16/u8/u16 返回**:emit `IR_CONV` 带符号扩展或零扩展,转成 `IRT_INT`(LuaJIT 内部小整数用 intV)。

这段代码逐分支对应 C 的整型提升规则——和 P6-20 §6.2 `lj_cconv_ct_ct` 的 `case CCX(I, I)` 用同一套语义(符号扩展 vs 零扩展)。**这种"解释器路径和 JIT 路径用完全相同的转换语义"是 soundness 的硬要求**(P6-20 §7.2 讲过):guard 失败退回解释器时,两边算出的值必须一致。

`J->needsnap = 1` 这一行也值得说:C 调用是有副作用的(它可能改全局状态、可能分配内存触发 GC),所以这一条 IR 之后**必须有一个 snapshot**——万一这条调用之后某个 guard 失败,要能退回解释器,而退回时必须假定这次调用**已经发生**了(因为机器码里它确实执行了)。`needsnap = 1` 就是告诉录制器"下一条 IR 之前补一个 snapshot"。这是 FFI 调用作为"有副作用操作"在 snapshot 策略上的体现。

### 2.3 crec_call_args:把参数摆成 CARG 树

`crec_call_args`(`lj_crecord.c:1121`)负责把 Lua 这边传进来的参数,逐个转换成 C 调用约定要求的 IR,然后用 `IR_CARG` 串成一棵树。这是 FFI 录制里最长最细的函数之一,我们抓主干。

```c
static TRef crec_call_args(jit_State *J, RecordFFData *rd,
                           CTState *cts, CType *ct)
{
  TRef args[CCI_NARGS_MAX];
  CTypeID fid;
  CTInfo info = ct->info;  /* lj_ccall_ctid_vararg may invalidate ct pointer. */
  MSize i, n;
  TRef tr, *base;
  cTValue *o;
  ...
  /* Skip initial attributes. */
  fid = ct->sib;
  while (fid) {
    CType *ctf = ctype_get(cts, fid);
    if (!ctype_isattrib(ctf->info)) break;
    fid = ctf->sib;
  }
  args[0] = TREF_NIL;
  for (n = 0, base = J->base+1, o = rd->argv+1; *base; n++, base++, o++) {
    CTypeID did;
    CType *d;

    if (n >= CCI_NARGS_MAX)
      lj_trace_err(J, LJ_TRERR_NYICALL);

    if (fid) {  /* Get argument type from field. */
      CType *ctf = ctype_get(cts, fid);
      fid = ctf->sib;
      lj_assertJ(ctype_isfield(ctf->info), "field expected");
      did = ctype_cid(ctf->info);
    } else {
      if (!(info & CTF_VARARG))
        lj_trace_err(J, LJ_TRERR_NYICALL);  /* Too many arguments. */
      ...
      did = lj_ccall_ctid_vararg(cts, o);  /* Infer vararg type. */
    }
    d = ctype_raw(cts, did);
    if (!(ctype_isnum(d->info) || ctype_isptr(d->info) ||
          ctype_isenum(d->info)))
      lj_trace_err(J, LJ_TRERR_NYICALL);
    tr = crec_ct_tv(J, d, 0, *base, o);
    ...
    args[n] = tr;
  }
  tr = args[0];
  for (i = 1; i < n; i++)
    tr = emitir(IRT(IR_CARG, IRT_NIL), tr, args[i]);
  return tr;
}
```

抓住核心循环。对每个 Lua 参数:

1. **查参数的 C 类型**。函数签名在类型表里是一个 `CT_FUNC`,它的 `sib` 链串着每个参数(每个参数是一个 `CT_FIELD`,`cid` 指向参数类型)。`fid` 沿这条链走,每走一步拿到一个参数的类型 ID `did`。这和 P6-20 §5.3 `ccall_set_args` 在解释器侧做的事**完全对应**——只是这里不摆寄存器,只生成 IR。
2. **变长参数的特殊处理**。如果签名里的固定参数用完了(`fid == 0`)但还有 Lua 参数,而且这个函数是变长参数(`CTF_VARARG`),调 `lj_ccall_ctid_vararg` 推断这个 Lua 参数该当什么 C 类型(数字 → double、字符串 → const char *……,见 P6-20 §5.3 的默认参数提升)。如果不是变长参数却参数多了,`LJ_TRERR_NYICALL` 终止。
3. **类型限制**:`if (!(ctype_isnum || ctype_isptr || ctype_isenum))`——只录制参数是 number/pointer/enum 的调用。struct 参数不录(`NYICALL`)。又是有意限制:struct 参数的 ABI 太复杂。
4. **转换**:`tr = crec_ct_tv(J, d, 0, *base, o)`。这是关键——把 Lua 参数的 IR(`*base`),转换成 C 类型 `d` 要求的 IR。`crec_ct_tv` 是 `lj_cconv_ct_tv` 的 IR 版本(P6-20 §6.1),它根据 Lua 值的类型和目标 C 类型,emit 一串 `IR_CONV`(整数↔浮点、符号/零扩展)、`IR_FLOAD`(从 cdata 取指针)等。注意循环里还有一段 `if (ctype_isinteger_or_bool(d->info))` 的处理(`lj_crecord.c:1181`),把小于 4 字节的整数提升成 `IRT_INT`——对应 C 的整型提升,保证 ABI 正确。

最后,所有参数 IR 用 `IR_CARG` 两两串起来:`tr = CARG(CARG(CARG(args[0], args[1]), args[2]), args[3])`……这是一棵左倾的树。为什么是树而不是数组?因为 IR 是 SSA 形式,每条 IR 只能有固定个数操作数(`IR_CARG` 是两个 op);多个参数就嵌套成一棵树。后端 `asm_collectargs` 会把这棵树解开还原成参数列表(下一节看)。

注意 `crec_call_args` 里那些 `#if LJ_TARGET_X86` / `LJ_TARGET_ARM64 && LJ_TARGET_OSX` 的分支(`lj_crecord.c:1130-1232`)——这些是处理各架构调用约定的特殊情形:x86 上 fastcall/thiscall 前几个参数走寄存器(要在参数列表里占位);Windows/x86 允许 64 位参数跨过重排;ARM64 macOS 变长参数要插一个标记。这些代码精确定位各 ABI 的边角规则,和 P6-20 §5.3 `CCALL_HANDLE_REGARG` 的宏是**同一套 ABI 知识的 IR 化**。换句话说,FFI 录制器把 `lj_ccall.c` 里那 1263 行调用约定知识,重新在 IR 层面实现了一遍——这样后端生成的机器码,才和 C 编译器生成的机器码遵守同样的 ABI。

### 2.4 CARG / CALLXS / CALLN:IR 里的调用家族

录完之后,看一眼 IR 层面这几个调用操作码的精确定义(`lj_ir.h:142`):

```c
  /* Calls. */
  _(CALLN,	NW, ref, lit) \
  _(CALLA,	AW, ref, lit) \
  _(CALLL,	LW, ref, lit) \
  _(CALLS,	S , ref, lit) \
  _(CALLXS,	S , ref, ref) \
  _(CARG,	N , ref, ref) \
```

逐个区分。这一族操作码看起来多,其实只有两个维度:

- **CALLN / CALLA / CALLL / CALLS**:这四个 op2 是**字面量**(lit,一个 IRCallID,索引 `lj_ir_callinfo[]` 表)。意思是"调一个**编译时已知**的 C 函数"。`lj_ir_callinfo[]`(`lj_ir.c:61`)是一张预先建好的表,登记了 LuaJIT 内部那一堆 C 辅助函数(`lj_str_new`、`lj_carith_divi64`、`lj_buf_putstr`……),录制时通过 `lj_ir_call(J, id, ...)`(`lj_ir.c:132`)生成 CALLN。后面三个字母 N/A/L/S 是优化属性标记(N=可 CSE、A=分配、L=可 load、S=有副作用),决定这条调用能不能被常量折叠等优化处理。**这族 IR 是"调 LuaJIT 自己的内部 C 函数"用的**——比如 64 位除法在 32 位平台上没有原生指令,LuaJIT 录一条 `CALLN lj_carith_divi64` 让后端生成调这个 C 辅助函数的机器码。
- **CALLXS**:op2 是**ref**(一个 IR 引用),意思是"调一个**运行时才知道地址**的函数"。这就是 FFI 调用用的——函数地址是 FLOAD 出来的 cdata payload,录制时不知道具体值,运行时才从 cdata 里读。`X` 表示"扩展"(extra),`S` 表示有副作用(必然,因为外部 C 函数什么都能干)。`crec_call` 里 `emitir(IRT(IR_CALLXS, t), args, func)` 就是这一族。

`CARG` 是参数打包:`N` 表示"无副作用纯计算"(打包参数本身不调函数),两个 ref 分别是"已有参数树"和"新参数"。它本身不生成机器码(后端 `asm_carg` 是 `break`,`lj_asm.c:1909`),只是个容器,把多个参数组织成一棵树方便 CALL* 引用。

`asm_collectargs`(`lj_asm.c:1397`)是后端解这棵树的函数:

```c
static void asm_collectargs(ASMState *as, IRIns *ir,
                            const CCallInfo *ci, IRRef *args)
{
  uint32_t n = CCI_XNARGS(ci);
  ...
  while (n-- > 1) {
    ir = IR(ir->op1);                              /* 沿 op1 往左走 */
    lj_assertA(ir->o == IR_CARG, "malformed CALL arg tree");
    args[n] = ir->op2 == REF_NIL ? 0 : ir->op2;   /* 取右儿子 */
  }
  args[0] = ir->op1 == REF_NIL ? 0 : ir->op1;     /* 最左叶子是 args[0] */
  lj_assertA(IR(ir->op1)->o != IR_CARG, "malformed CALL arg tree");
}
```

它沿 `op1` 一路往左走(CARG 树是左倾的),每一步把右儿子 `op2` 存进 `args[n]`,最后 `args[0]` 是最左叶子。注意 `REF_NIL` 的处理——`crec_call_args` 里那些 x86/ARM64 占位参数会 emit `TREF_NIL`(`args[n++] = TREF_NIL`),对应 `REF_NIL`,这里 `args[n] = ... ? 0 : ...` 把它变成 0,后端就知道"这个位置是占位的,不用真的传值"。这是占位参数的 IR 编码方式。

---

## §3 后端:CALLXS 怎么变成机器码

录制器生成了 `CALLXS` IR,后端(`lj_asm.c` + 各架构的 `lj_asm_*.h`)要把它翻译成机器码。这是"FFI 录制"落到地上的最后一公里。本节用 x86/x64 后端(`lj_asm_x86.h`)讲,因为它最常见,也最能体现"机器码遵守 C ABI"这件事。

### 3.1 asm_callx_flags:从 IR 反推调用约定

后端要为 CALLXS 重构出一份等价于 `CCallInfo` 的调用约定信息(寄存器分配、参数个数、调用约定)。这由 `asm_callx_flags`(`lj_asm.c:1414`)完成:

```c
static uint32_t asm_callx_flags(ASMState *as, IRIns *ir)
{
  uint32_t nargs = 0;
  if (ir->op1 != REF_NIL) {  /* Count number of arguments first. */
    IRIns *ira = IR(ir->op1);
    nargs++;
    while (ira->o == IR_CARG) { nargs++; ira = IR(ira->op1); }
  }
#if LJ_HASFFI
  if (IR(ir->op2)->o == IR_CARG) {  /* Copy calling convention info. */
    CTypeID id = (CTypeID)IR(IR(ir->op2)->op2)->i;
    CType *ct = ctype_get(ctype_ctsG(J2G(as->J)), id);
    nargs |= ((ct->info & CTF_VARARG) ? CCI_VARARG : 0);
#if LJ_TARGET_X86
    nargs |= (ctype_cconv(ct->info) << CCI_CC_SHIFT);
#endif
  }
#endif
  return (nargs | (ir->t.irt << CCI_OTSHIFT));
}
```

它做两件事。第一,**数参数个数**:沿 CALLXS 的 op1(那棵 CARG 树)走一遍,数出有多少个参数。第二,**提取调用约定信息**:回忆 `crec_call` 里,如果是变长参数或非 cdecl 调用约定,会把"函数指针 + 类型 ID"用 CARG 绑在一起(`lj_crecord.c:1298`)。这里后端检查 `IR(ir->op2)->o == IR_CARG`,如果是,就从那个 CARG 的 op2 里读出类型 ID,反查类型表拿到 `CTF_VARARG` 标志和(x86 上)调用约定(`ctype_cconv`),塞进 flags。

这个设计很巧妙:**录制时把"调用约定"这种编译期信息,通过 CARG 嵌进 IR,让后端能在不查运行时状态的情况下重构出完整的 ABI 信息**。这是 FFI 录制能把复杂调用约定正确传到后端的关键。

### 3.2 asm_callx:生成 call 机器码

`asm_callx`(`lj_asm_x86.h:757`)是把 CALLXS 翻成机器码的总控:

```c
static void asm_callx(ASMState *as, IRIns *ir)
{
  IRRef args[CCI_NARGS_MAX*2];
  CCallInfo ci;
  IRRef func;
  IRIns *irf;
  int32_t spadj = 0;
  ci.flags = asm_callx_flags(as, ir);
  asm_collectargs(as, ir, &ci, args);
  asm_setupresult(as, ir, &ci);
#if LJ_32
  /* Have to readjust stack after non-cdecl calls due to callee cleanup. */
  if ((ci.flags & CCI_CC_MASK) != CCI_CC_CDECL)
    spadj = 4 * asm_count_call_slots(as, &ci, args);
#endif
  func = ir->op2; irf = IR(func);
  if (irf->o == IR_CARG) { func = irf->op1; irf = IR(func); }
  ci.func = (ASMFunction)asm_callx_func(as, irf, func);
  if (!(void *)ci.func) {
    /* Use a (hoistable) non-scratch register for indirect calls. */
    RegSet allow = (RSET_GPR & ~RSET_SCRATCH);
    Reg r = ra_alloc1(as, func, allow);
    if (LJ_32) emit_spsub(as, spadj);
    emit_rr(as, XO_GROUP5, XOg_CALL, r);
  } else if (LJ_32) {
    emit_spsub(as, spadj);
  }
  asm_gencall(as, &ci, args);
}
```

逐段。先 `asm_callx_flags` 重建 flags,`asm_collectargs` 解开参数树,`asm_setupresult` 安排返回值寄存器(`lj_asm_x86.h:681`,整数返回 `rax`、浮点返回 `xmm0`)。

然后是函数地址处理,这是 FFI 调用和普通 CALLN 最大的区别。`func = ir->op2; irf = IR(func)`,如果 op2 是 CARG(带了类型 ID),再剥一层拿到真正的函数 ref(`irf->op1`)。然后调 `asm_callx_func`(`lj_asm_x86.h:736`):

```c
static void *asm_callx_func(ASMState *as, IRIns *irf, IRRef func)
{
#if LJ_32
  if (irref_isk(func))
    return (void *)irf->i;
#else
  if (irref_isk(func)) {
    MCode *p;
    if (irf->o == IR_KINT64)
      p = (MCode *)(void *)ir_k64(irf)->u64;
    else
      p = (MCode *)(void *)(uintptr_t)(uint32_t)irf->i;
    if (jmprel_ok(p, as->mcp))
      return p;  /* Call target is still in +-2GB range. */
  }
#endif
  return NULL;
}
```

这个函数决定**能不能用直接 call,还是必须间接 call**。如果函数地址是个编译期常量(`irref_isk`)——比如 `ffi.C.sqrt` 这种静态链接的函数,地址在编译 trace 时就能从 cdata 里 FLOAD 出来,而且是个常量——后端会尝试用**直接 call**(call 一个 immediate 地址,相对跳转)。x64 上还有个额外约束:`jmprel_ok` 检查目标地址在 `±2GB` 范围内(因为 x64 的相对 call 指令只有 32 位偏移);超出就放弃直接 call。

如果地址不是常量(`asm_callx_func` 返回 NULL),就是**间接 call**:`ra_alloc1` 把函数地址分配到一个非 scratch 寄存器,然后 `emit_rr(as, XO_GROUP5, XOg_CALL, r)`——这就是 x86 的 `call r/m64` 间接调用指令。FFI 函数指针大多是这种情况:地址从 cdata FLOAD 出来,是个运行时值。

最后无论直接还是间接,都 `asm_gencall(as, &ci, args)` 把参数按调用约定摆进寄存器/栈(`lj_asm_x86.h:574`)。`asm_gencall` 内部对每个参数:看它的类型(整数还是浮点),按 POSIX x64 的 `rdi/rsi/...` + `xmm0-7` 顺序或 Windows x64 的 `rcx/rdx/...` 顺序,分配寄存器或栈槽。如果参数已经在某个寄存器里(寄存器分配的结果),`emit_movrr` 移过去;如果在内存里,`ra_allocref` 加载;栈参数 `emit_movtomro` 写到栈上。

`asm_gencall` 的最后一行 `emit_call(as, ci->func)`(`lj_asm_x86.h:594`)——这就是那条 `call` 指令。对直接调用是 `call rel32`,对间接调用是 `call r64`(刚才 `asm_callx` 已经 emit 过了)。

### 3.3 生成出来的机器码长什么样

把整条链路合起来,`C.sqrt(s+1.0)` 在 x64 POSIX 上录完、编完,机器码大致是(概念示意):

```asm
; 假设 s+1.0 的结果在 xmm1(IR_ADD 的结果,寄存器分配留下的)
  movsd xmm0, xmm1          ; 参数:sqrt 的第一个 double 参数进 xmm0
                             ; (寄存器分配常常直接把结果分到 xmm0,这条 mov 可能被消除)
  call <sqrt 地址>            ; 直接 call(sqrt 是常量地址)或 call rax(间接)
  movsd xmm2, xmm0           ; 返回值:xmm0 → xmm2(后续 s = s + 返回值 用)
```

四条机器码。对比解释器路径(P6-20 §5):`lj_ccall_func` → `ccall_set_args`(遍历参数、查类型、转值、摆 CCallState)→ `lj_vm_ffi_call`(把 CCallState 搬进物理寄存器、call、回收返回值)→ `ccall_get_results`(返回值转 Lua 值)。解释器路径是几百纳秒的 C 函数链,JIT 路径是几纳秒的机器码。**这就是 FFI 录制带来的性能差距来源**。

而更关键的是:**这段机器码严格遵守 x64 POSIX ABI**(第一个 double 参数进 xmm0、返回值在 xmm0),因为它是由 `asm_gencall` 按 ABI 规则生成的。它和 C 编译器编译 `sqrt(s+1.0)` 生成的机器码,在 ABI 层面**完全等价**。这是 FFI 录制 soundness 的根基:不是"碰巧能调通",而是"按 C ABI 精确生成"。

---

## §4 lj_ffrecord.c:对照——内建函数的快速录制

讲 FFI 录制时,值得把它和另一个"录制函数调用"的机制对照:LuaJIT 标准库函数的**快速录制**(`lj_ffrecord.c`)。这两个机制本质上都是"把一次调用录成 IR",但服务的对象不同,做法也不同。理解它们的差别,能更清楚地看到 FFI 录制的独特价值。

### 4.1 什么函数能被快速录制

LuaJIT 把自己的标准库函数(`math.sin`、`string.format`、`table.insert`、`bit.band`……)实现成了一批特殊的 C 函数,叫**快速函数**(fast functions)。这些函数和普通 Lua C API 函数的差别在于:它们的签名是**固定的、类型已知的**,不经过 Lua 栈取参数,而是直接接收已经类型窄化好的值。

举例,`math.sin` 在解释器侧是个 C 函数,接收一个 Lua 栈上的 number。但 LuaJIT 知道它只接受一个 number 参数、返回一个 number。所以在录制时,`lj_ffrecord.c` 不必把 `math.sin` 当黑盒——它可以**内联**这次调用:把参数的 IR 直接传给一个已知的 C 辅助函数(`sin`),生成一条 `CALLN lj_vm_sin` 的 IR。

`recff_math_call`(`lj_ffrecord.c:646`)就是这个逻辑:

```c
static void LJ_FASTCALL recff_math_call(jit_State *J, RecordFFData *rd)
{
  TRef tr = lj_ir_tostr(J, J->base[0]) ? : J->base[0];
  J->base[0] = emitir(IRTN(IR_CALLN), tr, rd->data);
}
```

极其简洁:拿参数 IR,emit 一条 `IR_CALLN`,op2 是 `rd->data`(一个 IRCallID,索引 `lj_ir_callinfo[]`,指向对应的 C 函数 `sin`/`cos`/`exp`/……)。这条 CALLN 后端会生成一条 `call <lj_vm_sin>` 的机器码。

注意这和 FFI 录制的 `CALLXS` 不同:CALLN 的 op2 是**字面量** IRCallID,函数地址在编译期就完全确定(是 LuaJIT 内部的一个固定 C 函数);CALLXS 的 op2 是 **ref**,函数地址运行时才知道(从 cdata FLOAD 出来)。这两个操作码精确反映了"调用对象是否在编译期已知":内建函数已知 → CALLN;FFI 函数未知 → CALLXS。

### 4.2 快速函数录制的价值与边界

快速录制让 `math.sin`、`string.format` 这种高频标准库函数,在热循环里也能变成机器码(一条 `call` 到对应的 C 实现),不用每次走完整的 Lua C API 路径。这是 LuaJIT 性能的另一个来源。

但它的边界也很明显:**只有 LuaJIT 自己内建的那些函数能这样录**。因为录制器必须**事先知道**这个函数的签名(几个参数、什么类型、返回什么),才能生成正确的 CALLN。这个"事先知道"的信息,写死在 `lj_ffrecord.c` 的 `recff_*` 函数里(每个内建函数一个 handler),以及 `lj_ircall.h` 的 `IRCALLDEF` 表里(每个 C 辅助函数登记签名)。

用户自己写的 Lua C API 函数(用 `lua_register` 注册的)?**没法快速录制**。因为 LuaJIT 不知道它的签名——它只知道"这是个 `int (*)(lua_State *)`",内部要取几个参数、什么类型、怎么处理,全是黑盒。所以这种函数,录制器只能让解释器真的去调它,然后录一个"这里调了个外部 C 函数"的标记(通常 trace 在这里 stitch 或终止)。

**FFI 的 C 函数正好补上了这个缺口。** 用户用 `ffi.cdef` 声明的任意 C 函数,虽然不是 LuaJIT 内建的,但**签名在 `ffi.cdef` 时已经存进类型表了**——录制器能查到。所以 FFI 函数能像内建函数一样被精确录制(只是用 CALLXS 而非 CALLN,因为地址运行时才从 cdata 取)。这是 FFI 相对 Lua C API 的根本性能优势所在:**FFI 把"用户提供的 C 函数"也纳入了 JIT 的优化范围**,而 Lua C API 函数永远停在外面。

合起来看 LuaJIT 三种"调 C"的机制,从快到慢、从可 JIT 到不可 JIT:

| 机制 | 签名何时已知 | 能否 JIT 内联 | IR 操作码 | 例子 |
|---|---|---|---|---|
| 内建快速函数 | 编译期(写死在 LuaJIT) | 能 | CALLN | `math.sin` |
| FFI C 函数 | `ffi.cdef` 时(类型表) | 能 | CALLXS | `C.sqrt` |
| Lua C API 函数 | 永远不知(黑盒) | 不能 | (stitch/终止) | `print`、用户注册的 |

FFI 居中:不如内建函数那么"亲"(地址运行时才知),但远胜 Lua C API(签名已知,能 JIT)。这个中间位置,正是 FFI 成为 LuaJIT 招牌特性的根源——它让"调任意 C 库"也能享受 JIT 的性能。

---

## §5 GC 与 JIT 协作(一):trace 是 GC 对象

讲完了 FFI 录制,我们进入本章第二大块:GC 和 JIT 怎么协作。这是全书压轴必须回答的——前面所有章节都在讲 trace 怎么生成、怎么跑,但 trace 本身占内存,它引用别的 GC 对象,GC 必须正确处理这些关系,否则要么内存泄漏、要么误回收导致崩溃。

我们从最根本的事实开始:trace 是不是 GC 对象?

### 5.1 GCtrace:trace 是一个 GCobj

P0-01 §11 贴过 `GCtrace` 结构(`lj_jit.h:250`),我们重新审视它,这次带着 GC 的视角:

```c
typedef struct GCtrace {
  GCHeader;              /* 所有 GC 对象的公共头:tag + marked(GC 颜色) */
  uint16_t nsnap;
  IRRef nins;
#if LJ_GC64
  uint32_t unused_gc64;
#endif
  GCRef gclist;          /* GC 灰色链表指针 */
  IRIns *ir;             /* IR 数组 */
  ...
  GCRef startpt;         /* 起始 prototype(指向 GCproto) */
  ...
  MSize szmcode;
  MCode *mcode;          /* 机器码指针 */
  ...
} GCtrace;
```

注意两个 GC 相关的字段。第一,`GCHeader`——所有 GC 对象都有这个头,里面有 `gct`(类型 tag,对 trace 是 `~LJ_TTRACE`)和 `marked`(GC 颜色位,白/灰/黑)。第二,`gclist`——GC 灰色链表的串联指针,GC 标记阶段用它把所有灰色对象串起来排队传播。

这两个字段的存在,意味着 **trace 完完全全是一个 GC 对象**。它和 GCtab、GCstr、GCcdata 一样,被分配在 GC 堆上(`lj_trace.c:trace_save` 里 `newwhite(J2G(J), T)` 把它作为白色新对象挂进 `gc.root` 链),被 GC 标记和回收。

这一点是 GC 与 JIT 协作的根基:**trace 不是一个游离的、GC 看不见的东西,它是 GC 的正式公民**。GC 知道它的存在,会标记它、回收它。这保证了 trace 不会内存泄漏(不用了 GC 会回收),也不会被误回收(GC 标记时会追踪它引用的对象)。

### 5.2 gc_traverse_trace:GC 怎么遍历一条 trace

既然 trace 是 GC 对象,GC 标记它时要遍历它的子引用(它"指着"哪些别的 GC 对象)。这是 `gc_traverse_trace`(`lj_gc.c:256`):

```c
static void gc_traverse_trace(global_State *g, GCtrace *T)
{
  IRRef ref;
  if (T->traceno == 0) return;
  for (ref = T->nk; ref < REF_TRUE; ref++) {      /* 遍历 IR 常量区 */
    IRIns *ir = &T->ir[ref];
    if (ir->o == IR_KGC)
      gc_markobj(g, ir_kgc(ir));                  /* 标记 IR 常量里的 GC 对象 */
    if (irt_is64(ir->t) && ir->o != IR_KNULL)
      ref++;                                      /* 64 位常量占两条 */
  }
  if (T->link) gc_marktrace(g, T->link);          /* 标记链接的 trace */
  if (T->nextroot) gc_marktrace(g, T->nextroot);  /* 标记同 prototype 的下一条 root */
  if (T->nextside) gc_marktrace(g, T->nextside);  /* 标记下一条 side trace */
  gc_markobj(g, gcref(T->startpt));               /* 标记起始 prototype */
}
```

这个函数是"GC 照顾 trace"的核心。它做三件事:

**第一,遍历 IR 常量区,标记所有 `IR_KGC` 常量。** 这是最关键的。trace 的 IR 里,有些常量是指向 GC 对象的——比如 `lj_ir_kstr`(一个字符串常量)、`lj_ir_kfunc`(一个函数常量)、`lj_ir_ktab`(一个 table 常量)、`lj_ir_kcdata`(一个 cdata 常量)。这些在 IR 里以 `IR_KGC` 操作码存储(`lj_snap.c:452` 的 `snap_replay_const` 也处理 KGC/KPTR 等)。如果 GC 不遍历这些,就可能发生:trace 的机器码里用到了一个 GCstr 常量,但 GC 不知道 trace 还指着它,把它回收了——机器码下次执行就读到已释放内存,崩溃。

`gc_traverse_trace` 这个循环就是堵这个漏洞:把 trace 的 IR 常量区(`T->nk` 到 `REF_TRUE`)扫一遍,每个 `IR_KGC` 都 `gc_markobj`,告诉 GC"这个对象还被这条 trace 引着,别回收"。注意循环里 `if (irt_is64(ir->t) && ir->o != IR_KNULL) ref++`——这是因为 64 位常量(`IR_KNUM`/`IR_KINT64`)在 32 位平台上占两个 IR 槽,遍历时要跳一格。这种细节是 GC 正确性的边角。

**第二,标记 trace 之间的链接关系。** `link`/`nextroot`/`nextside` 是 P0-01 §13 讲的 trace 树结构。一条 trace 跑完可能跳到另一条 trace(`link`),root trace 串成链(`nextroot`),side trace 串成链(`nextside`)。GC 标记一条 trace 时,要把它链接的这些 trace 也标记上——因为如果 trace A 还活着、链向 trace B,那 B 显然也不能回收(A 的机器码会跳到 B)。这三行 `gc_marktrace` 就是保证 trace 树作为一个整体被正确标记。

**第三,标记起始 prototype。** `startpt` 指向这条 trace 录制时所在的那段 Lua 字节码(`GCproto`)。trace 依赖这个 prototype(比如 side exit 退回解释器时要回到这个 prototype 的某条字节码),所以 prototype 不能比 trace 先死。

这三个标记合起来,保证:**只要一条 trace 还活着,它引用的所有 GC 对象(IR 常量、链接的 trace、起始 prototype)都不会被回收**。这是 GC 与 JIT 协作的第一道 soundness 保障——不漏标。

### 5.3 propagatemark:trace 在标记传播里的位置

`gc_traverse_trace` 是被谁调用的?在增量 GC 的标记传播阶段,GC 从灰色队列里取出一个对象,调 `propagatemark`(`lj_gc.c:324`)遍历它。`propagatemark` 是个巨大的 if-else 链,按对象类型分发。trace 是其中一支:

```c
static size_t propagatemark(global_State *g)
{
  GCobj *o = gcref(g->gc.gray);
  int gct = o->gch.gct;
  ...
  gray2black(o);
  setgcrefr(g->gc.gray, o->gch.gclist);  /* 从灰色队列摘除 */
  if (LJ_LIKELY(gct == ~LJ_TTAB)) {
    ...
  } else if (LJ_LIKELY(gct == ~LJ_TFUNC)) {
    ...
  } else if (LJ_LIKELY(gct == ~LJ_TPROTO)) {
    ...
  } else if (LJ_LIKELY(gct == ~LJ_TTHREAD)) {
    ...
  } else {
#if LJ_HASJIT
    GCtrace *T = gco2trace(o);
    gc_traverse_trace(g, T);
    return ((sizeof(GCtrace)+7)&~7) + (T->nins-T->nk)*sizeof(IRIns) +
           T->nsnap*sizeof(SnapShot) + T->nsnapmap*sizeof(SnapEntry);
#else
    ...
#endif
  }
}
```

trace 是 else 分支(前面那些是高频类型,用 `LJ_LIKELY` 提示分支预测;trace 相对低频)。注意它返回的大小估计:

```c
((sizeof(GCtrace)+7)&~7)              /* trace 结构体本身(8 字节对齐) */
  + (T->nins-T->nk)*sizeof(IRIns)      /* IR 数组 */
  + T->nsnap*sizeof(SnapShot)          /* snapshot 数组 */
  + T->nsnapmap*sizeof(SnapEntry)      /* snapshot map */
```

这正是 trace 占用的全部内存(P0-01 §11 讲的 IR + snapshot)。GC 用这个估计来调整增量 GC 的步长(下一节细讲)——大对象标记一次花的时间多,GC 推进的"预算"也多。注意 **mcode 不在这个估计里**——因为 mcode 是单独管理的(下一节讲),它有自己的内存池。

### 5.4 gc_freefunc:trace 怎么被回收

trace 标记完,清扫阶段要回收白色的(没人引用的)trace。GC 的清扫表 `gc_freefunc[]`(`lj_gc.c:381`)登记了每种 GC 对象的释放函数:

```c
static const GCFreeFunc gc_freefunc[] = {
  (GCFreeFunc)lj_str_free,
  ...
#if LJ_HASJIT
  (GCFreeFunc)lj_trace_free,
#else
  (GCFreeFunc)0,
#endif
  ...
};
```

trace 的释放函数是 `lj_trace_free`(`lj_trace.c:172`):

```c
void LJ_FASTCALL lj_trace_free(global_State *g, GCtrace *T)
{
  jit_State *J = G2J(g);
  if (T->traceno) {
    lj_gdbjit_deltrace(J, T);
    if (T->traceno < J->freetrace)
      J->freetrace = T->traceno;
    setgcrefnull(J->trace[T->traceno]);   /* 从 trace 数组摘除 */
  }
  lj_mem_free(g, T,
    ((sizeof(GCtrace)+7)&~7) + (T->nins-T->nk)*sizeof(IRIns) +
    T->nsnap*sizeof(SnapShot) + T->nsnapmap*sizeof(SnapEntry));
}
```

它释放 trace 结构体 + IR + snapshot + snapshotmap——和 `propagatemark` 估计的大小一致。注意一个细节:**它不释放 mcode**。

为什么不释放 mcode?这是 LuaJIT 的一个有意设计。mcode(机器码)不是一条 trace 独占一块内存,而是所有 trace 共享一个**机器码内存池**。看 `lj_mcode.c` 的 `mcode_allocarea`(`lj_mcode.c:357` 附近):

```c
((MCLink *)J->mcarea)->next = oldarea;   /* 旧 area 串进链表 */
((MCLink *)J->mcarea)->size = sz;
J->szallmcarea += sz;
```

每个 mcode area 开头有个 `MCLink` 头,记录这块的大小和指向下一块的指针,所有 area 串成链表。新 trace 的机器码从当前 area 的 `mctop` 往下分配(`lj_mcode_reserve`),不够了就 `mcode_allocarea` 申请新 area。**单条 trace 不拥有自己的 mcode area**,所以 `lj_trace_free` 没法单独释放某条 trace 的机器码。

那 mcode 什么时候释放?答案在 `lj_mcode_free`(`lj_mcode.c:374`):

```c
void lj_mcode_free(jit_State *J)
{
  MCode *mc = J->mcarea;
  J->mcarea = NULL;
  J->szallmcarea = 0;
  while (mc) {
    MCode *next = ((MCLink *)mc)->next;
    size_t sz = ((MCLink *)mc)->size;
    lj_err_deregister_mcode(mc, sz, (uint8_t *)mc + sizeof(MCLink));
    mcode_free(mc, sz);
    mc = next;
  }
}
```

它遍历整个 mcode area 链表,**全部释放**。这个函数只在 `lj_trace_freestate`(`lj_trace.c:359`,关闭 Lua state 时)和 trace abort 的清理路径(`lj_trace.c:297`)调用。也就是说:**mcode 是批量管理的,GC 单条 trace 回收时不释放 mcode**。

这是个权衡。代价是:被回收的 trace 的机器码,会留在 mcode area 里"悬空"着(没人跳进去了,但内存还占着),直到整个 area 满了被新代码覆盖,或者 state 关闭时统一释放。好处是:**mcode 的分配/释放是 W^X 友好的批量操作**——mcode 所在的内存需要 `mprotect` 切换可写/可执行(见 P4-15),频繁地单条释放再分配会反复触发 `mprotect`,开销大。批量管理让 mcode 的内存操作集中在少数几次。

所以 GC 与 JIT 协作里 mcode 的处理是:**GC 回收 trace 的 IR/snapshot(单条精确),但 mcode 由 JIT 自己批量管理**。这是个"GC 管对象、JIT 管机器码内存"的职责划分。

### 5.5 当前正在录制的 trace:atomic 阶段的特殊处理

还有一类特殊情况:正在录制、还没保存的 trace(在 `J->cur` 里)。这条 trace 还没挂进 `gc.root`(因为还没 `trace_save`),但它已经引用了一些 GC 对象(录制过程中 emit 的 IR_KGC 常量)。如果 GC 在录制过程中跑起来,会不会漏标 `J->cur` 引用的对象?

`atomic` 函数(`lj_gc.c:621`)专门处理这个:

```c
static void atomic(global_State *g, lua_State *L)
{
  ...
  gc_mark_uv(g);
  gc_propagate_gray(g);
  ...
  gc_markobj(g, L);                       /* 标记运行中的线程 */
  gc_traverse_curtrace(g);                /* 遍历当前正在录制的 trace */
  gc_mark_gcroot(g);
  gc_propagate_gray(g);
  ...
}
```

`gc_traverse_curtrace` 宏(`lj_gc.c:274`)在 `LJ_HASJIT` 时展开成 `gc_traverse_trace(g, &G2J(g)->cur)`——直接遍历 `J->cur`(当前 trace)。源码注释说得很清楚:"The current trace is a GC root while not anchored in the prototype (yet)"。意思是:当前 trace 还没挂进 prototype 的 trace 链(没保存),所以 GC 标记阶段不会从 prototype 找到它;为了不漏标,atomic 阶段把它当作一个**临时 GC 根**显式遍历一次。

`atomic` 是增量 GC 从标记阶段切换到清扫阶段的"原子点"——在这个点,GC 会一次性做完所有残留的标记工作,保证不漏。把 `gc_traverse_curtrace` 放在这里,正是为了保证:**无论 GC 在录制过程中何时跑到 atomic,当前正在录制的 trace 引用的对象都会被标记到**。这是录音中(未保存)trace 的 GC 保障。

---

## §6 GC 与 JIT 协作(二):增量、写屏障、trace 运行时

上一节讲的是"trace 作为 GC 对象怎么被标记回收"。这一节讲更动态的一面:**GC 是增量的**(分多次小步跑,不一次停全场),而 **trace 的机器码正在 CPU 上跑**——这两个事实叠加,会产生一系列协作问题。

### 6.1 增量 GC:为什么要分步

先回忆 LuaJIT 的 GC 是什么样的。它是一个**增量三色标记清扫** GC(P1-04 提过 GCobj)。三色:

- **白**:还没标记的(候选回收)。
- **灰**:已标记,但它引用的对象还没全部标记(传播队列里)。
- **黑**:已标记,且它引用的对象也都标记了(确定存活)。

回收时,清扫阶段把仍是白色的对象回收掉。三色标记的不变式是:**黑色对象不能指向白色对象**(否则黑色对象引用的白色对象会被误回收)。所有从黑指向白的写操作,都要通过**写屏障**把那个白色对象变灰,维持不变式。

为什么要**增量**?如果 GC 一次性标记清扫完,在对象多的程序里会卡很久(STW, stop-the-world)。这对交互式应用(游戏、GUI)是不可接受的。增量 GC 把这个工作切成很多小步,每步只做一点(`gc_onestep`,`lj_gc.c:657`),夹在普通 Lua 执行之间,让停顿分散成很多次极短的卡顿。

LuaJIT 的 GC 状态机有这些阶段(`lj_gc.c:660` 起):`GCSpause`(开始)→ `GCSpropagate`(增量标记)→ `GCSatomic`(原子点,一次性收尾标记)→ `GCSsweepstring`(扫字符串表)→ `GCSsweep`(扫主对象链)→ `GCSfinalize`(跑 `__gc` 终结器)→ 回 `GCSpause`。

`lj_gc_step`(`lj_gc.c:724`)是增量 GC 的主入口,每次做若干步 `gc_onestep`,直到预算(`GCSTEPSIZE * stepmul`)用完或一个 GC 周期跑完:

```c
int LJ_FASTCALL lj_gc_step(lua_State *L)
{
  global_State *g = G(L);
  GCSize lim;
  ...
  lim = (GCSTEPSIZE/100) * g->gc.stepmul;       /* 预算 */
  ...
  if (g->gc.total > g->gc.threshold)
    g->gc.debt += g->gc.total - g->gc.threshold;
  do {
    lim -= (GCSize)gc_onestep(L);
    if (g->gc.state == GCSpause) { ... return 1; }
  } while (... lim > 0 ...);
  ...
}
```

`lj_gc_check` 宏(`lj_gc.h:65`)是触发点:在分配内存后检查,如果总内存超过阈值,就跑一次 `lj_gc_step`:

```c
#define lj_gc_check(L) \
  { if (LJ_UNLIKELY(G(L)->gc.threshold >= G(L)->gc.total)) \
      lj_gc_step(L); }
```

理解了增量 GC,我们来看它和 JIT 的两个协作问题。

### 6.2 问题一:trace 跑的时候,GC 不能进 atomic

第一个问题最严重。GC 的 `atomic` 阶段是一次性做完的(虽然是增量 GC 的例外),它会重置白色(`g->gc.currentwhite = otherwhite(g)`,`lj_gc.c:650`)、清空弱表、分离 userdata——这些都是**全局状态突变**。如果这些发生时 trace 的机器码正在 CPU 上跑,机器码可能正拿着一个"旧白色"的对象引用,atomic 一过,这个对象在新一轮里变成"该回收的白色",被清扫掉——机器码下一次循环就踩空。

LuaJIT 的处理在 `gc_onestep` 里(`lj_gc.c:669`):

```c
  case GCSatomic:
    if (tvref(g->jit_base))  /* Don't run atomic phase on trace. */
      return LJ_MAX_MEM;
    atomic(g, L);
    ...
```

`tvref(g->jit_base)` 检查"现在是不是在 trace 上跑"——`jit_base` 非 NULL 表示当前线程正在执行 JIT 编译出来的机器码(P0-01 §12 讲的,trace 接管时设这个字段)。如果是在 trace 上,`atomic` 直接 `return LJ_MAX_MEM`——**拒绝执行,推迟到 trace 退出**。

`LJ_MAX_MEM` 这个返回值是个信号。配合 `lj_gc_step_jit`(`lj_gc.c:764`):

```c
int LJ_FASTCALL lj_gc_step_jit(global_State *g, MSize steps)
{
  lua_State *L = gco2th(gcref(g->cur_L));
  L->base = tvref(G(L)->jit_base);
  L->top = curr_topL(L);
  while (steps-- > 0 && lj_gc_step(L) == 0)
    ;
  /* Return 1 to force a trace exit. */
  return (G(L)->gc.state == GCSatomic || G(L)->gc.state == GCSfinalize);
}
```

`lj_gc_step_jit` 是**从 trace 机器码内部**调用的(GC 在 trace 里跑若干步,因为 trace 跑很久,GC 也得有机会推进)。它跑若干步 `lj_gc_step`,**如果发现 GC 卡在 atomic 或 finalize 阶段无法继续**(因为上面那个检查),就 `return 1`——**强制 trace 退出**。trace 退出后回到解释器,`jit_base` 清空,这时 GC 才能安全地跑 atomic。

这是一个精妙的握手:**trace 跑的时候允许 GC 做增量标记(不动全局状态的部分),但 GC 想做 atomic(全局突变)时必须等 trace 退出**。这样既不让 GC 长时间停顿 trace(trace 跑得久时 GC 仍能推进标记),又保证 atomic 不在 trace 中途发生(避免状态突变踩坑)。

### 6.3 问题二:trace 引用对象的写屏障

第二个问题是写屏障。回忆三色不变式:黑不能指白。trace 录制时,会 emit `IR_KGC` 常量(比如字符串常量),这些常量是 trace 持有的 GC 对象引用。如果一条已存的(黑色)trace,因为某种原因需要新增一个 KGC 引用——比如 side trace 录制时引用了一个 root trace 没引用的字符串——这就破坏了不变式。

`lj_gc_barriertrace`(`lj_gc.c:855`)就是为这个准备的:

```c
#if LJ_HASJIT
void lj_gc_barriertrace(global_State *g, uint32_t traceno)
{
  if (g->gc.state == GCSpropagate || g->gc.state == GCSatomic)
    gc_marktrace(g, traceno);
}
#endif
```

它被 `trace_save` 调用(`lj_trace.c:165`):一条新 trace 保存好(变白、挂进 gc.root)后,立刻调 `lj_gc_barriertrace`。如果 GC 此时在标记阶段(`GCSpropagate` 或 `GCSatomic`),就把这条新 trace 标记成灰色(`gc_marktrace`),挂进灰色队列——这样 GC 后续传播时会遍历它,标记它引用的所有 KGC,维持不变式。

为什么是标记 trace 自己(而不是它引用的对象)?因为写屏障的本质是"把破坏不变式的那个对象重新拉回传播队列"。新 trace 出现,意味着"有个新对象(trace)可能引用了没标记的对象"——把 trace 标灰,让 GC 重新遍历它(`gc_traverse_trace`),就能标记到它新引用的所有对象。这是写屏障"前推传播前沿"思想(`lj_gc_barrierf` 注释:"Move the GC propagation frontier forward")在 trace 上的特化。

对照普通写屏障 `lj_gc_barrierf`(`lj_gc.c:804`):

```c
void lj_gc_barrierf(global_State *g, GCobj *o, GCobj *v)
{
  ...
  if (g->gc.state == GCSpropagate || g->gc.state == GCSatomic)
    gc_mark(g, v);  /* Move frontier forward. */
  else
    makewhite(g, o);  /* Make it white to avoid the following barrier. */
}
```

普通写屏障是"黑对象 o 写入了白对象 v"时触发:在标记阶段,直接标 v(`gc_mark(v)`);在清扫阶段,把 o 染白(这样下一轮 GC 重新标记它,简单粗暴)。`lj_gc_barriertrace` 是它的 trace 版特化——因为 trace 的"写入"(新增引用)发生在 trace 保存那一刻,直接标 trace 自己最方便。

### 6.4 snapshot 恢复时的 GC 处理

还有一个 GC 与 JIT 协作的细节:snapshot 恢复(P5-17)。当一个 guard 失败,side exit 要靠 snapshot 把机器码状态恢复成解释器能继续的样子。这时如果 trace 里有些"已分配但还没在解释器栈上"的 cdata(比如 trace 里 emit 过 `IR_CNEW` 新建了个 cdata,但还没"提交"到栈上),snapshot 恢复要正确处理它们。

看 `lj_snap.c` 的 snapshot 恢复代码(`lj_snap.c:841` 附近):

```c
  if (ir->o == IR_CNEW || ir->o == IR_CNEWI) {
    ...
    GCcdata *cd = lj_cdata_newx(cts, id, sz, info);
    setcdataV(J->L, o, cd);
    if (ir->o == IR_CNEWI) {
      uint8_t *p = (uint8_t *)cdataptr(cd);
      ...
      snap_restoredata(J, T, ex, snapno, rfilt, ir->op2, p, sz);
    }
    ...
  }
```

这是 **snapshot 恢复时重新分配 cdata** 的逻辑。当 snapshot 里有个 slot 对应一条 `IR_CNEW`/`IR_CNEWI`(P3-11 讲的分配消除 sink 后还没"物化"的 cdata),恢复时调 `lj_cdata_newx` 真的分配一个新的 cdata,然后把 trace 里算好的值(`snap_restoredata` 从 ExitState 寄存器里取)填进去。

为什么恢复时要重新分配,而不是直接用 trace 里那个 cdata?因为 sink 优化(P3-11)把那些"分配了但没逃逸"的 cdata **消除**了——trace 的机器码里根本没真的分配,只是把 cdata 的字段值放在寄存器里算。这些 cdata 只在 trace 内部"虚拟"存在。一旦要 side exit 退回解释器,解释器需要看到一个真实的 cdata 对象(在 Lua 栈上),所以 snapshot 恢复时要把这个虚拟 cdata **物化**——真的 `lj_cdata_newx` 分配一个,填上 trace 里算的值。这个分配会触发 `lj_gc_check`(P6-20 §5.1 讲过 cdata 是 GC 对象,分配可能触发 GC),所以恢复是 GC 安全的。

这个机制是 sink 优化和 snapshot 的接口:**sink 让 trace 跑得快(省掉分配),snapshot 恢复在退出时把省掉的分配补回来**。两边都对 GC 友好——sink 期间没分配(GC 没压力),恢复时分配并触发 GC(对象被正确纳入 GC)。

### 6.5 gc_mark 里 cdata 的特殊待遇

最后讲一个 `gc_mark`(`lj_gc.c:58`)里的小细节,它体现了 GC 对 cdata 的特殊处理:

```c
static void gc_mark(global_State *g, GCobj *o)
{
  int gct = o->gch.gct;
  ...
  white2gray(o);
  if (LJ_UNLIKELY(gct == ~LJ_TUDATA)) {
    ...
    gray2black(o);  /* Userdata are never gray. */
    ...
  } else if (LJ_UNLIKELY(gct == ~LJ_TUPVAL)) {
    ...
  } else if (gct != ~LJ_TSTR && gct != ~LJ_TCDATA) {
    /* 不是 string 也不是 cdata:挂进灰色队列等传播 */
    lj_assertG(gct == ~LJ_TFUNC || gct == ~LJ_TTAB ||
               gct == ~LJ_TTHREAD || gct == ~LJ_TPROTO || gct == ~LJ_TTRACE,
               "bad GC type %d", gct);
    setgcrefr(o->gch.gclist, g->gc.gray);
    setgcref(g->gc.gray, o);
  }
}
```

注意那个 `gct != ~LJ_TSTR && gct != ~LJ_TCDATA` 的条件:**string 和 cdata 不进灰色队列,直接从白变黑**(`white2gray` 之后没挂队列,但后续清扫时它们已经是灰的,实际上 LuaJIT 的设计是 string/cdata 没有子引用,等价于黑色叶子)。

为什么?因为 string 和 cdata 是**叶子对象**——它们不引用别的 GC 对象(string 的字符数据不是 GC 对象;cdata 的 payload 是裸 C 数据,除非是 `__gc` 终结器引用的函数,但那个走单独路径)。叶子对象不需要"传播"——标记它们就是直接涂黑。所以 GC 跳过把它们挂灰色队列这一步,直接处理。这是个优化:**省一次入队/出队,标记更快**。

而 trace **不是**叶子对象(它引用 KGC 常量、链接的 trace、prototype),所以 trace 走的是正常的"灰→挂队列→传播→黑"路径。这个对比说明:GC 对不同类型的对象,根据它们"有没有子引用"采取不同的标记策略,既正确又快。

---

## §7 为什么 sound:FFI ABI 正确、GC 不漏不重

讲完了所有机制,这一节集中论证本章两个主题的 soundness:①FFI 录制生成的机器码为什么不犯错;②GC 与 JIT 协作为什么不漏标不误回收。

### 7.1 FFI 录制的 soundness:机器码遵守 C ABI

FFI 录制把一次 C 调用变成机器码,这个机器码必须和 C 编译器编译等价 C 代码生成的机器码**行为完全一致**——参数传递对、返回值处理对、调用约定对。任何一处不对,轻则拿到错误参数,重则栈错乱、崩溃。

LuaJIT 通过四层保障堵住这个风险:

**第一层:类型信息来自完整的类型表。** `crec_call` 从 `ctype_raw(cts, cd->ctypeid)` 拿函数类型,这个类型是 `ffi.cdef` 时由完整的 C 解析器(`lj_cparse`)建立的,覆盖了 C 类型的所有维度(P6-20 §2.3)。录制器基于这个类型决定每个参数怎么转换、返回值怎么处理——类型信息从源头就是完整正确的。

**第二层:每个乐观假设都有 guard。** `argv2cdata` 里 emit 的 `IR_EQ` guard(`lj_crecord.c:57`)检查 ctypeid 没变;`crec_constructor` 同理。这意味着"这次调的确实是 sqrt 这个 ctypeid 的函数指针"这个假设,在机器码里被运行时检查。一旦某次 ctypeid 变了(比如 cdata 被换成了另一个函数指针),guard 触发 side exit,退回解释器走完整 `lj_ccall_func` 路径。**FFI 录制的乐观假设,和普通 trace 的 guard 一样,是被检查的、可回退的。**

**第三层:转换遵守 C 语义。** `crec_ct_tv` 的转换(整数提升、符号/零扩展)和 `crec_call` 返回值后加工的 `IR_CONV`,都精确对应 P6-20 §6 `lj_cconv_ct_ct` 的语义——而那套语义是为了和 C 编译器一致设计的(符号扩展按 `CTF_UNSIGNED`、浮点↔整数用 `lj_num2i64` 跨平台一致)。**解释器路径(`lj_cconv_ct_ct`)和 JIT 路径(`crec_ct_tv` emit 的 CONV)用同一套转换规则**,这保证了 guard 失败退回解释器时,两边算出的值完全一致——本书主线那个核心不变式("机器码结果要么和解释器一样,要么退回解释器")在 FFI 这里依然成立。

**第四层:机器码遵守 C ABI。** `asm_callx_flags` 从 IR 重构调用约定(`asm_callx_flags`),`asm_gencall` 按各架构 ABI 摆参数进寄存器/栈。x64 POSIX 的 double 进 xmm0、整数进 rdi/rsi/...;Windows x64 的位置共享;ARM64 的 x0-x7……这些和 P6-20 §5 `lj_ccall.c` 的 `CCALL_HANDLE_REGARG` 是同一套 ABI 知识,只是在 IR/后端层面重新实现。变长参数的默认提升(`crec_call_args` 里 4 字节以下整数提升成 int)、非 cdecl 调用约定的栈清理(`asm_callx` 里 x86 的 `spadj`)、struct 返回不录制(`crec_call` 的 NYICALL)——这些边角规则都精确处理或诚实拒绝。

四层合起来,FFI 录制生成的机器码,**在它选择录制的那些情形(参数是 num/ptr/enum、返回是 num/ptr/enum/void/bool)下,和 C 编译器生成的机器码行为一致**。对于它不录制的情形(返回 struct、struct 参数、会回调的函数),`LJ_TRERR_NYICALL`/`LJ_TRERR_BLACKL` 诚实终止,退回解释器——**永远不生成错误的机器码**。

### 7.2 GC 与 JIT 协作的 soundness:不漏标、不误回收

GC 与 JIT 协作的 soundness,核心是两个"不":**不漏标**(trace 引用的对象不被误回收)、**不误回收**(还在用的对象不被清扫)。

**不漏标**靠三个机制:

1. **trace 是 GC 对象,被正常标记。** `gc_marktrace`(`lj_gc.c:244`)把 trace 挂进灰色队列,`propagatemark` 的 trace 分支(`lj_gc.c:353`)调 `gc_traverse_trace` 遍历它的所有子引用。这条路径和普通 GC 对象(table、func)的标记路径是平行的——trace 不会被遗忘。
2. **IR_KGC 常量被显式标记。** `gc_traverse_trace` 扫 IR 常量区,每个 `IR_KGC` 都 `gc_markobj`(`lj_gc.c:262`)。这保证 trace 的机器码引用的字符串、cdata 等,都被 GC 看到。
3. **trace 之间的链接被追踪。** `link`/`nextroot`/`nextside` 都 `gc_marktrace`,trace 树作为一个整体被标记。
4. **当前未保存的 trace 在 atomic 阶段被显式遍历。** `gc_traverse_curtrace`(`lj_gc.c:274` 宏,在 `atomic` 调用)处理 `J->cur`,保证录制中的 trace 引用的对象也不漏。
5. **trace 保存时的写屏障。** `lj_gc_barriertrace`(`lj_gc.c:855`)在标记阶段把新 trace 标灰,让 GC 重新遍历。

这五个机制覆盖了 trace 引用 GC 对象的所有路径——已存的、录制中的、新保存的——保证 GC 永远知道"这个对象还被某条 trace 引着"。

**不误回收**靠三个机制:

1. **atomic 不在 trace 上跑。** `gc_onestep` 的 atomic 分支检查 `jit_base`(`lj_gc.c:670`),在 trace 上时拒绝执行;`lj_gc_step_jit` 检测到卡 atomic 就强制 trace 退出(`lj_gc.c:772`)。这避免了 GC 全局状态突变时 trace 还在跑的险境。
2. **mcode 由 JIT 批量管理,不参与单条 trace 回收。** `lj_trace_free` 不释放 mcode,避免误释放还在被别的 trace 用的机器码内存。
3. **snapshot 恢复时正确物化 sunk cdata。** `lj_snap.c:841` 的恢复逻辑保证 side exit 退回解释器时,sink 优化的 cdata 被正确分配并纳入 GC。

合起来,GC 与 JIT 协作是 sound 的:**GC 完整地追踪 trace 这个新对象类型**(标记它的 IR、snapshot、链接关系、KGC 引用),**同时尊重 JIT 的运行时特性**(atomic 不打扰 trace、mcode 批量管理、sink 物化)。没有"trace 持有的对象被误回收"或"trace 跑时 GC 状态突变"这两种灾难。

---

## §8 ★对照:官方 Lua、JNI 与分代 GC

把 LuaJIT 的 FFI 录制 + GC 协作,和几个对象放在一起对照,更能看清它的取舍。

### 8.1 对照官方 Lua:没有 JIT,GC 是怎么做的

官方 Lua 没有 JIT,所以根本没有"trace 是 GC 对象""GC 与 JIT 协作"这回事——它的 GC 只管 table/string/function/userdata/upvalue/thread 这些。官方 Lua 的 GC 也是增量三色标记清扫(和 LuaJIT 同源,LuaJIT 的 GC 是从官方 Lua 演化来的),但它**不需要处理 trace**:

| 维度 | LuaJIT | 官方 Lua |
|---|---|---|
| 是否有 trace | 有(GCtrace) | 无 |
| GC 对象类型 | 含 trace、cdata | 不含这两类 |
| GC 增量 | 是 | 是(同源) |
| 是否有"GC 不能在 trace 上跑 atomic"问题 | 有,需 `jit_base` 检查 | 无此问题 |
| FFI 调用 | 能被 JIT 编译(CALLXS) | 没有 FFI,只有 Lua C API |

这个对照点出:**JIT 给 GC 带来了新的负担**——trace 这个新对象类型必须被 GC 追踪,trace 运行时 GC 必须避让 atomic。官方 Lua 没有这些负担,但代价是没有 JIT 的性能。LuaJIT 选择了"加 JIT + 让 GC 照顾 trace",用 GC 代码的复杂度(`gc_traverse_trace`、`lj_gc_barriertrace`、`lj_gc_step_jit` 等)换性能。

### 8.2 对照 JVM:JNI 不能被 JIT,GC 分代并发

JVM 的对照更尖锐,分两方面。

**JNI 不能被 JIT 内联。** JVM 调 C 走 JNI(Java Native Interface,P6-20 §9.2 讲过)。JNI 的 C 函数是黑盒——签名虽然能从 `javac -h` 生成的头文件看出来,但 JVM 的 JIT 看不到 JNI 函数内部。所以 JVM 的 JIT(C1/C2/Graal)**无法把 JNI 调用内联进编译产物**——每次 JNI 调用都要从 managed code 切到 native code,经过 JNI 桥(切换栈帧、pin 对象、可能触发 safepoint),开销大且无法优化掉。

更糟的是,JIT 的很多优化(逃逸分析、内联、常量折叠)在 JNI 边界**断裂**——一旦代码走到 JNI 调用,JIT 之前的优化假设可能失效,需要 deoptimize。所以 JVM 生态里 JNI 是"最后手段",能用纯 Java 实现就不用。

LuaJIT 的 FFI 录制正好相反:**FFI 调用能被 JIT 内联**(`crec_call` 生成 CALLXS,后端生成直接 `call` 机器码)。这是因为 FFI 在 `ffi.cdef` 时就把签名存进了类型表,JIT 能查到、能基于它生成精确 IR。代价是 LuaJIT 实现了完整的 C 类型系统 + 多架构 ABI 支持(几大千行源码),以及 FFI 录制器(`lj_crecord.c` 2007 行)。但收益是巨大的:**Lua 调 C 库的开销,在热路径上逼近原生 C 调 C**,这是 JVM 永远做不到的。

**GC:增量 vs 分代并发。** LuaJIT 的 GC 是**增量**三色标记清扫——把一次 GC 切成很多小步,夹在执行之间,但本质还是标记-清扫(不分代、不移动对象)。JVM 的 GC(G1/ZGC/Shenandoah)**分代** + **并发** + 部分实现**移动**对象(compacting):

| 维度 | LuaJIT GC | JVM GC(G1/ZGC 等) |
|---|---|---|
| 算法 | 增量标记-清扫 | 分代 + 并发标记 + 压缩 |
| 是否分代 | 否(全量扫) | 是(新生代/老年代) |
| 是否移动对象 | 否(对象地址稳定) | 是(对象可能被移动) |
| 与 JIT 的特殊协作 | atomic 不在 trace 上跑 | safepoint:JIT 代码主动停下来让 GC |
| 暂停时间 | 短(增量小步) | 极短(ZGC 亚毫秒) |

这里有个有意思的对比。LuaJIT 因为**不移动对象**,所以 trace 的机器码可以放心地持有 GC 对象的地址(指针稳定,不会被搬走)——这是 LuaJIT 选择非移动 GC 的一个隐含理由(机器码里嵌的指针不能失效)。JVM 的 GC 会移动对象,所以 JIT 编译的代码里持有的对象引用必须是**句柄(handle)或能在 safepoint 重定位的**,这增加了 JIT 的复杂度。

而"atomic 不在 trace 上跑"(`lj_gc.c:670`)对应 JVM 的 safepoint 机制:JVM 的 JIT 编译产物里会插入 safepoint 检查点,GC 想做全局操作时,等所有线程到达 safepoint(从 JIT 代码退出)。两者本质都是**让 JIT 代码避开 GC 的全局突变**——LuaJIT 是"atomic 推迟到 trace 退出",JVM 是"等线程到达 safepoint"。

### 8.3 对照 CPython:ctypes/cffi 没有 JIT

P6-20 §9.2 提过,CPython 的 ctypes/cffi 也能让 Python 调 C(思路类似 LuaJIT FFI:声明签名、直接调)。但 CPython 是纯解释器(没有 JIT),所以 ctypes/cffi 的调用**永远走完整的 C 代码路径**(参数装箱/拆箱 + 调用约定准备),无法被编译成机器码。这等价于 LuaJIT FFI 的"解释器路径"(P6-20),但**永远到不了 JIT 路径**(本章)。

| 维度 | LuaJIT FFI | CPython ctypes/cffi | JVM JNI |
|---|---|---|---|
| 声明签名 | `ffi.cdef` | ctypes/cffi 声明 | `javac -h` + 手写 |
| 写 C 代码 | 否 | 否 | 是 |
| 解释器路径开销 | 小(直接 ABI) | 中(直接 ABI) | 大(JNI 桥) |
| **能被 JIT 内联** | **能** | **不能(无 JIT)** | 不能(JIT 看不进 JNI) |

这一栏是 LuaJIT FFI 的**独特价值**:**唯一一个"直接 ABI 调用 + trace JIT 内联"结合的实现**。ctypes/cffi 卡在第一层(直接 ABI,但无 JIT);JNI 卡在第二层(有 JIT,但 JNI 黑盒进不去)。LuaJIT 把两层都占了,这是它在跨语言调用性能上独占鳌头的根源。

---

## §9 全书压轴:trace + guard + side exit + FFI + GC,共同实现"把动态执行安全变成机器码"

这是全书最后一章正文。我们把前面所有章节串起来,回扣那条贯穿始末的主线:

> **用乐观假设 + 运行时检查(guard)+ 失败可回退(side exit/snapshot),把一段动态执行的脚本,安全地变成机器码。**

### 9.1 主线的五个支柱

LuaJIT 把这条主线落地,靠五个支柱,每一个都是前面某一篇的主题:

**支柱一:热点检测(P1-03)。** 解释器先跑,hotcount 数循环/调用次数,谁跑得多就编译谁。这回答了"什么时候编译"——数据驱动,只编译值得编译的。省。

**支柱二:trace 录制(P2-05~P2-08)。** 把运行时实际走的那条线性路径录成 IR(SSA),用类型窄化(narrowing)把动态类型乐观地定成具体类型。这回答了"编译什么"——一条实际的热路径,不是整个函数。聚焦,假设乐观。

**支柱三:guard(P5-16)。** 每个乐观假设都在机器码里配一条运行时检查。假设"x 是整数"就 emit 一条检查 x 类型的指令。这回答了"假设错了怎么办"——guard 当场发现,不会偷偷算错。安全。

**支柱四:snapshot + side exit(P5-17、P5-18)。** guard 失败时,照着 snapshot 把机器码状态恢复成解释器能继续的样子,退回解释器;经常失败的退出点还能再录一条 side trace,把那条"另一条路"也编译了。这回答了"失败之后呢"——退回永远正确的解释器,结果照样对;而且越跑越多的路径被编译,整体越来越快。安全 + 渐进优化。

**支柱五:FFI 录制 + GC 协作(P6-20、P6-21,本章)。** FFI 让"调 C"也能被 JIT 编译(CALLXS → 机器码),把 JIT 的覆盖范围延伸到 C 边界之外;GC 把 trace 当作正式 GC 对象追踪,保证 JIT 引入的新对象类型不破坏内存安全。这回答了"怎么和外部世界交互 + 怎么不出内存问题"——性能延伸到跨界,内存安全不破。

这五个支柱合起来,就是"把动态执行安全变成机器码"的完整工程实现。

### 9.2 安全和快的统一

全书反复出现一个张力:**快(乐观假设、机器码)和 安全(类型未知、不能算错)怎么共存?** LuaJIT 的答案在五个支柱里层层体现:

- **快**来自乐观假设(trace 只录一条路、类型窄化到具体类型、FFI 调用直接编成 call 机器码)。
- **安全**来自运行时检查(guard 兜底每个假设)+ 完整保底(失败退回永远正确的解释器)+ 类型系统严谨(FFI 的类型在 `ffi.cdef` 时就完整建模)+ GC 完整追踪(trace、cdata、所有引用)。

安全不是用慢换来的——guard 是几条机器码指令(检查 + 条件跳转),绝大多数时候不触发,几乎零开销。安全是用**"检查 + 失败可回退"的架构**换来的:不追求一次性把所有可能都覆盖(method JIT 那样),而是大胆假设、每次检查、失败就退。这个架构让动态语言既享受机器码的速度,又保留动态类型的灵活和正确。

本章的 FFI 录制和 GC 协作,是这个统一在全书最后两个落点:

- **FFI 录制是"快"的极致**:连跨界调 C 都能编成机器码,trace 不断在 C 边界。
- **GC 协作是"安全"的兜底**:JIT 引入的新对象(trace、cdata、mcode)被 GC 完整追踪,内存安全不破。

两者合起来,完成了"把动态执行安全变成机器码"的最后一块拼图。

### 9.3 LuaJIT 的设计哲学

回看全书,LuaJIT 的设计哲学可以浓缩成几条:

1. **数据驱动,而非静态分析。** 不试图在编译时推断所有类型(method JIT 的路),而是在运行时观察实际类型,基于观察做乐观假设。这让动态语言的 JIT 变得可行——不需要复杂的类型推断,只需要"看了什么就假设什么"。
2. **聚焦热点,而非全覆盖。** trace 只录一条热路径,不编译整个函数。这让编译开销小、编译快——省下的时间用来跑机器码。
3. **乐观 + 检查 + 回退,而非保守全覆盖。** 假设大胆(生成最快机器码),但每个假设都配 guard,失败可回退解释器。这让机器码极快,同时安全有保底。
4. **类型严谨是性能的前提。** FFI 能 JIT,是因为 `ffi.cdef` 时类型就完整建模了——没有类型系统的严谨,就没有"放心地编成机器码"的底气。安全(类型对)和快(能 JIT)在这里统一。
5. **GC 照顾所有对象,包括 JIT 自己造的。** trace、cdata、mcode 都是 JIT 引入的新东西,GC 必须完整追踪它们。这是"加 JIT 不能破坏内存安全"的硬要求。

这些哲学,贯穿了从 P0-01 的第一性原理推导,到本章的 FFI 录制 + GC 协作。每一章都是其中一条或多条的具体落地。把它们合起来,就是一个完整的 trace JIT 编译器——LuaJIT——如何被造出来的全貌。

### 9.4 没有魔法

全书最后一句话,和第一章遥相呼应:**JIT 没有魔法**。

trace 不是"自动找到最优路径",而是"录制实际跑的那条"。guard 不是"预测假设对不对",而是"每次都检查"。snapshot 不是"凭空恢复状态",而是"事先记录好映射表"。FFI 录制不是"AI 理解 C 函数",而是"基于完整的类型表生成精确 IR"。GC 与 JIT 协作不是"自动协调",而是"显式标记 trace、显式避开 atomic、显式处理 sink"。

每一步都有明确的理由、精确的源码、严谨的不变式。LuaJIT 比 V8/HotSpot 小一两个量级,正因为它选择的 trace 路线足够简单——简单到能被一个程序员(Mike Pall)独立实现,简单到能被一本书一行行讲清。但这个"简单"的设计,却能跑出比纯解释器快几十倍的速度——这是工程上的优雅,也是这本书想带你看到的:**好的设计,是把复杂的权衡(快 vs 安全 vs 省)用简单的机制(trace + guard + side exit)解决**。

写到这里,你应该已经看清了 LuaJIT 的全部:从 CPU 只懂机器码这个最基础的事实,到 trace JIT 的完整流水线,再到 FFI 录制和 GC 协作这两个压轴机制。每一行源码都有它的位置,每一个设计都有它的理由。剩下的,是把这些理解,变成你自己写 JIT、读运行时源码、做性能优化的底子。

全书正文到此结束。附录 A 会把这条主线再串一遍,给出全景脉络;附录 B 给出一份源码阅读路线,带你按顺序读 LuaJIT 的源码。

---

*全书正文完。下一步:[附录A 全景脉络](附录A-全景脉络.md)——把 21 章串成一张图,回看 trace JIT 的完整骨架。*
