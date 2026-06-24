# P3-11 分配消除 sink + 死代码 dce

> **本书主线**:把动态执行安全变成机器码。**二分法**:解释器侧 ↔ JIT 侧。本章在 **JIT 侧(P3 优化 pass)**。**★对照**:官方 Lua(无优化,临时 table 照分配)+ JVM/V8(逃逸分析 + 标量替换 vs LuaJIT 的 allocation sinking)。**源码**:LuaJIT 2.1.ROLLING。**基调**:纯直球,不用比喻;从第一性原理一步步推导。

---

## 引子:两条 IR 上发生的"清场"

经过 P3 前面几章,我们已经看到一段 trace 的 IR 是怎么被一步步打磨的:常量折叠把 `x+0` 折成 `x`(P3-09),前向替换把重复的 load 合并、把"先存后读"折成"直接用值"(P3-10),别名分析判断两个指针会不会指向同一块内存。每一步都让 IR 更紧凑,最终生成的机器码更短、更快。

这一章是 P3 优化篇的收尾。它讲两个看起来不同、但精神一致的优化——它们都遵循同一条朴素原则:

> **没用的东西,就不要留着。**

"没用的东西"分两种:

第一种,是 **死代码**(dead code)。某些 IR 指令算出来的结果,从头到尾没有任何指令再用到它。它算了个寂寞。这种指令,删掉它,机器码就少几条,运行就快一点,而且不会改变任何结果。识别并删除这种指令的优化,叫 **DCE**(Dead Code Elimination,死代码消除)。

第二种,是 **没必要的内存分配**。Lua 程序里有一个极其常见的模式:

```lua
local t = {}
t[1] = 1
t[2] = 2
return t[1] + t[2]
```

`local t = {}` 创建了一个 table,往里写了两个值,最后只是把里面的值读出来用。这个 table 从生到死,没有被传给别的函数,没有被任何外部代码引用——它是一个 **短命的、局部的临时对象**。

可即便如此,解释器执行这段代码时,还是老老实实:先 `malloc` 一块内存造出 table,写两个槽,读两个槽,然后等这个 table 没人引用了,GC 再来回收。分配一次,回收一次,两次内存操作的开销,就为了一个用完即扔的临时容器。

这值得吗?这个临时 table 根本没逃出当前这点代码,能不能干脆 **别分配它**,直接把 `t[1]`、`t[2]` 换成它们存的值来用?

能。这种优化叫 **allocation sinking**(分配消除),或者更准确地说,是 "把分配和相关的存取操作沉掉(sink)"。LuaJIT 用它干掉那些不逃逸的临时 table 分配,直接在寄存器里传递值,省掉分配和 GC 压力。

DCE 和 sink,一个删"算出来没人用的",一个删"造出来没必要的"。它们是优化流水线的最后两道清理。这一章就讲清楚:它们怎么识别"没用"、怎么保证删了之后程序还正确(sound)、以及 LuaJIT 的源码是怎么用极短的篇幅把它们实现的——`lj_opt_dce.c` 只有 75 行,`lj_opt_sink.c` 也才 258 行。

---

## §1 第一性原理:什么样的代码是"死"的

先从 DCE 讲起,因为它概念更简单,而且 sink 的实现里其实借用了 DCE 的标记机制——两个优化的工程手法是相通的。

### 1.1 一条没人用的指令

回到那个最基础的问题:一条 IR 指令,什么时候是"死的"?

考虑这段经窄化后的 IR(伪示意,数值是编的):

```
%1 = ADD %x, 1        -- 算 x+1
%2 = MUL %y, 2        -- 算 y*2
%3 = ADD %1, %z       -- 算 (x+1)+z, 用到了 %1
return %3             -- 返回 %3
```

这里 `%2 = MUL %y, 2` 算出了一个结果,但后面没有任何指令引用 `%2`。它算了个寂寞。这就是死代码——它执行了,但它的结果对程序的最终输出毫无贡献。

为什么会出现死代码?有几个来源:

1. **前面优化留下的残骸**。常量折叠时,可能把某条指令折成了一个常量,但它原本产生值的指令还在 IR 里没人引用了。比如 `x * 1` 折成 `x`,那条 MUL 就没人要了。
2. **前向替换(前一章 P3-10)留下的**。前向替换把 `store(t, k, v); load(t, k)` 折成 `v`,那条 load 原本如果还被别的指令引用,现在不引用了,可能就变死。
3. **录制时多录的**。trace 录制是边跑边录,有时为了拿到类型信息或 guard,会录下一些 IR,但录完发现最终没用上。

这些死指令如果留着,后端 asm 会老老实实给它们分配寄存器、生成机器码——白白占地方。删掉它们,机器码更小,寄存器压力更小,运行更快。

### 1.2 但"没人用"还不够——有副作用的不能删

到这里你可能会想:DCE 不就是"看一条指令的结果有没有被引用,没被引用就删"吗?这么简单?

没那么简单。看这两条:

```
%1 = STORE [t+8], 42    -- 往内存写 42
%2 = ADD %a, %b         -- 算 a+b,没人用
```

`%2 = ADD` 确实没人用,删掉没问题——加法只是算个值,删了不影响内存、不影响别的状态。

但 `%1 = STORE` 呢?它的"结果"也几乎没人引用(store 通常没有返回值)。但它 **写了一块内存**。如果这块内存后面被别人读,或者这块内存要被外部看到(比如传给了别的函数),那这个 store 是 **有副作用的**(side effect)——删了它会改变程序行为。

所以 DCE 的真正判据不是"结果没人用",而是更精确的:**这条指令有没有副作用**。

一条指令有副作用,当且仅当它满足以下任意一条:

1. **它写内存**(store 类):改了内存状态,删掉可能让后续读拿到错值。
2. **它是个 guard**:guard 是运行时检查,删掉等于撤掉一道假设验证,机器码可能算错(我们在 §3 详细讲为什么 guard 绝不能被 DCE 删)。
3. **它调用了可能修改状态的函数**(call 类)。
4. **它分配了内存**(alloc 类):分配本身改变堆状态。

只有 **既没人引用、又没副作用** 的指令,才能安全删除。DCE 的全部难点,就是精确判定"这条指令到底有没有副作用"。

### 1.3 反向标记:从"有用的"出发

那么算法怎么实现?一个朴素的思路是:正向扫一遍,每条指令查"我被谁引用了",没被引用就删。但这有两个麻烦:一是要建一张"被谁引用"的反查表,占内存;二是 store 这种没返回值的指令,本来就没"引用者",靠这个判不出。

LuaJIT 用的是反过来的思路,叫 **反向标记传播**(backward mark propagation)。它的逻辑很优雅:

> **与其找"谁是死的",不如找"谁是活的",然后把活的全标记下来——没被标记的,就是死的。**

那"谁是活的"?活的指令来自三类 **根**(root):

1. **被 snapshot 引用的**。snapshot 记录的是 guard 退出时要恢复的状态(下一章 P5-17 详讲)。如果一条 IR 指令的值要写进 snapshot,那它在 side exit 时必须能恢复出来,所以它必须活——asm 必须为它算出值。
2. **有副作用的**(store/guard/call/alloc)。这些指令就算没人引用它的"结果",它本身的副作用不能丢。
3. **被任何"活的"指令当作操作数引用的**。如果 `%3 = ADD %1, %z` 是活的,那它的两个操作数 `%1` 和 `%z` 也必须活——因为要算 `%3` 就得先有它们。

第 3 条是关键:它让"活"从根开始,沿着数据流 **反向传播**。一条指令一旦被标成活的,它的操作数就被标活;操作数的操作数又被标活……一路传回去。最后,所有直接或间接支撑着"根"的指令,都被标成活的;那些标不到的,就是真的没人要、又没副作用的死代码。

这个算法只扫一遍 IR(从后往前),不需要建反查表,极其高效。LuaJIT 的 DCE 就是这么干的,75 行搞定。

### 1.4 用最小例子走一遍

把这个算法在我们的例子上走一遍:

```
%1 = ADD %x, 1
%2 = MUL %y, 2
%3 = ADD %1, %z
return %3
```

第一步,确定根。`return %3` 是出口,`%3` 必须活(它要被返回,相当于要进 snapshot)。`%1`、`%2`、`%3` 都不是 store/guard/call,没有副作用。

第二步,反向传播。`%3` 活,标它的操作数 `%1`、`%z` 活。`%1` 活,标它的操作数 `%x` 和常量 `1` 活(常量本来就活,这里忽略)。

第三步,扫一遍看谁没被标。`%2 = MUL %y, 2` 没被任何活指令引用,它自己也没副作用——它是死的。删掉(替换成 NOP)。

结果:

```
%1 = ADD %x, 1
%3 = ADD %1, %z
return %3
```

`%2` 消失了。机器码少一条 MUL。完美。

这就是 DCE 的全部第一性原理。它不复杂,但它必须做对一件事:**副作用的判定必须准确**。判错了,要么删了不该删的(程序算错),要么留着该删的(优化没做透)。下一节我们看 LuaJIT 怎么用一个精巧的位编码,把"有没有副作用"变成一个 O(1) 的查表。

---

## §2 源码印证:lj_opt_dce.c 为什么只有 75 行

现在读源码。`lj_opt_dce.c` 全文 75 行,是整个 LuaJIT 优化器里最短的文件之一。我们逐函数看,重点理解它怎么把上面的"反向标记"落地的。

### 2.1 副作用的编码:IR 模式表 lj_ir_mode

DCE 能这么短,秘密不在算法本身(算法上节讲完了),而在一个**早就编码好的、全局共享的数据**——IR 模式表。LuaJIT 在定义每一条 IR 操作码时,就同时声明了它的"模式",这个模式里就包含了"有没有副作用"。

打开 `lj_ir.h:284`,模式位的定义:

```c
/* Mode bits: Commutative, {Normal/Ref, Alloc, Load, Store}, Non-weak guard. */
#define IRM_C			0x10

#define IRM_N			0x00
#define IRM_R			IRM_N
#define IRM_A			0x20
#define IRM_L			0x40
#define IRM_S			0x60

#define IRM_W			0x80
```

抓住这几个位:

- `IRM_C`(0x10):可交换(commutative),如 `a+b == b+a`,给折叠优化用,跟 DCE 关系不大。
- 低 3 位(bit 1-2 经过 `&IRM_S` 后是 0x60 掩码)表示 **指令类别**:`IRM_N`(0x00 普通算术)、`IRM_A`(0x20 分配)、`IRM_L`(0x40 读)、`IRM_S`(0x60 写)。注意 `IRM_S = 0x60`,而 `irm_kind(m) = (m) & IRM_S`(`lj_ir.h:303`),也就是说 **只要类别 ≥ 0x60,就是写类**。
- `IRM_W`(0x80):**非弱 guard**(non-weak guard)位。这一位是关键。

每条 IR 操作码的模式,是把这几个标志 **或** 起来。看操作码定义(`lj_ir.h` 的 `IRDEF` 宏),举几个对照:

```c
_(ADD,    C , ref, ref)    // IRM_C | IRM_N | (异或IRM_W)  → 普通可交换算术,无副作用
_(ASTORE, S , ref, ref)    // IRM_S | (异或IRM_W)          → 写,有副作用
_(TNEW,   AW, lit, lit)    // IRM_A | IRM_W | (异或IRM_W) → 分配,有副作用(分配即副作用)
_(CALLS,  S , ref, lit)    // IRM_S | (异或IRM_W)          → 调用,有副作用
_(LT,     N , ref, ref)    // IRM_N | (异或IRM_W)          → guard(比较),有副作用(它是检查!)
```

注意 `IRMODE` 宏最后那个 `^IRM_W`(`lj_ir.h:305`):

```c
#define IRMODE(name, m, m1, m2)	(((IRM##m1)|((IRM##m2)<<2)|(IRM_##m))^IRM_W),
```

它把"是否非弱 guard"那一位 **默认翻转**了。这个设计很巧:大多数普通算术指令(ADD/MUL)在定义里没标 `W`,经过 `^IRM_W` 后反而 **有了** `IRM_W` 位;而真正标了 `W` 的(ASTORE/TNEW)翻转后 **没了**。但关键在于:**guard 类指令**(LT/GE/LE… 比较)在运行时被设了 `IRT_GUARD` 类型标记(因为它们录制时挂了 guard),于是判定副作用时要 **把类型里的 guard 位和模式表的 W 位一起算**。

这就是 `ir_sideeff` 那一行魔法的来历(`lj_ir.h:600`):

```c
/* A store or any other op with a non-weak guard has a side-effect. */
static LJ_AINLINE int ir_sideeff(IRIns *ir)
{
  return (((ir->t.irt | ~IRT_GUARD) & lj_ir_mode[ir->o]) >= IRM_S);
}
```

这一行看起来吓人,拆开看就清楚。它判的是"这条指令的模式位,在考虑了它是否真的是 guard 之后,是否 ≥ `IRM_S`(0x60)"。`>= IRM_S` 意味着类别是 Store 或更高。具体:

- 如果指令本身模式是 `IRM_S`(写类,如 ASTORE),模式位 ≥ 0x60,直接判有副作用。
- 如果指令是 guard 比较类(LT 等),它的类型 `ir->t.irt` 里被设了 `IRT_GUARD`(0x80)。`ir->t.irt | ~IRT_GUARD` 把高位全置 1,再和模式表 `&`——由于 `LJ_STATIC_ASSERT((int)IRT_GUARD == (int)IRM_W)`(`lj_ir.h:605`,0x80==0x80),guard 位的置位会让结果跨过 0x60 的门槛,判有副作用。
- 普通算术(ADD),既不是 S 类、又没被设 guard 位,结果 < 0x60,无副作用。

**一句话:`ir_sideeff` 用一次位运算 + 一次查表,把"这条指令写内存 / 是 guard / 是分配 / 是调用"统一定义成"有副作用",O(1)。** 这是 DCE 能写得这么短的地基——副作用判定这个最难的点,被前移到了 IR 设计阶段,编码进了一张静态表。

### 2.2 dce_marksnap:把 snapshot 引用的标活

有了副作用判定,DCE 的两个阶段就清晰了。第一阶段,标根里的第一类:snapshot 引用。`lj_opt_dce.c:21`:

```c
/* Scan through all snapshots and mark all referenced instructions. */
static void dce_marksnap(jit_State *J)
{
  SnapNo i, nsnap = J->cur.nsnap;
  for (i = 0; i < nsnap; i++) {
    SnapShot *snap = &J->cur.snap[i];
    SnapEntry *map = &J->cur.snapmap[snap->mapofs];
    MSize n, nent = snap->nent;
    for (n = 0; n < nent; n++) {
      IRRef ref = snap_ref(map[n]);
      if (ref >= REF_FIRST)
	irt_setmark(IR(ref)->t);
    }
  }
}
```

这个函数做的事:遍历这个 trace 的 **所有** snapshot(`J->cur.snap` 数组,每个 guard 一个),把每个 snapshot 里记录的每个条目(`snap_ref(map[n])` 取出它对应的 IR 引用)标上 mark。

`irt_setmark` 是什么?看 `lj_ir.h:446`:

```c
#define irt_setmark(t)		((t).irt |= IRT_MARK)
```

`IRT_MARK` 是 `0x20`(`lj_ir.h:344`),一个专门留给优化 pass 用的"杂项标记位"——注释写的是 "Marker for misc. purposes"。它和类型本身无关,纯粹是个临时标记,DCE 用它表示"这条指令是活的,别删"。注意这里有个 `ref >= REF_FIRST` 的判断:`REF_FIRST = REF_BIAS+1 = 0x8001`(`lj_ir.h:469`),常量引用(`< REF_BIAS`)不需要标(常量本来就常驻,谈不上死活)。

走完 `dce_marksnap`,所有 snapshot 引用到的 IR 指令,类型里都带上了 `IRT_MARK`。这是第一批"活的根"。

### 2.3 dce_propagate:反向传播 + 删死代码

第二阶段是核心。`lj_opt_dce.c:37`:

```c
/* Backwards propagate marks. Replace unused instructions with NOPs. */
static void dce_propagate(jit_State *J)
{
  IRRef1 *pchain[IR__MAX];
  IRRef ins;
  uint32_t i;
  for (i = 0; i < IR__MAX; i++) pchain[i] = &J->chain[i];
  for (ins = J->cur.nins-1; ins >= REF_FIRST; ins--) {
    IRIns *ir = IR(ins);
    if (irt_ismarked(ir->t)) {
      irt_clearmark(ir->t);
    } else if (!ir_sideeff(ir)) {
      *pchain[ir->o] = ir->prev;  /* Reroute original instruction chain. */
      lj_ir_nop(ir);
      continue;
    }
    pchain[ir->o] = &ir->prev;
    if (ir->op1 >= REF_FIRST) irt_setmark(IR(ir->op1)->t);
    if (ir->op2 >= REF_FIRST) irt_setmark(IR(ir->op2)->t);
  }
}
```

这个函数把"反向标记"和"删除死代码"合并在 **同一次反向扫描** 里完成,这是它最漂亮的地方。逐行拆:

**第 42 行**:`pchain` 是个指针数组,指向每条 IR 操作码链的"当前尾指针"。LuaJIT 的 IR 是 SSA,同一类操作码用 `ir->prev` 字段串成一条链(`J->chain[op]` 指向链头),便于 CSE(公共子表达式消除)等优化快速查找。删一条 IR 时,要把它从链里摘掉,这就是 `pchain` 的作用。

**第 43 行主循环**:`for (ins = J->cur.nins-1; ins >= REF_FIRST; ins--)`。从最后一条 IR 指令往前扫,扫到 `REF_FIRST`(第一条非常量指令)为止。为什么反向?因为"活"是从出口往回传的——后面的指令决定前面的指令是否被需要。

**循环体三岔路口**,每条指令遇到三种情况:

**情况 A:已标记(活的)**。第 45-46 行:

```c
if (irt_ismarked(ir->t)) {
  irt_clearmark(ir->t);
}
```

这条指令之前被标了 mark(要么被 snapshot 标,要么被后面的活指令标)。它是活的。清掉 mark(因为标记使命已完成,要还回去给后面的 pass 用),然后落到第 52-54 行——把它自己的操作数也标活(反向传播的"传"就在这里)。

**情况 B:没标记,且无副作用(死的)**。第 47-50 行:

```c
} else if (!ir_sideeff(ir)) {
  *pchain[ir->o] = ir->prev;  /* Reroute original instruction chain. */
  lj_ir_nop(ir);
  continue;
}
```

`ir_sideeff` 返回假——这条指令既没人引用它的结果,又没副作用。它就是死代码。删!具体两步:

1. `*pchain[ir->o] = ir->prev`:把这条指令从它所在的操作码链里摘掉(前一个节点的 prev 指针跳过它,指向它的 prev)。这样后续的 CSE 等优化再查链时,不会撞到这条已死的指令。
2. `lj_ir_nop(ir)`:把这条指令的 opcode 改成 `IR_NOP`,操作数清零。看 `lj_ir_nop`(`lj_ir.h:608`):

```c
static LJ_AINLINE void lj_ir_nop(IRIns *ir)
{
  ir->ot = IRT(IR_NOP, IRT_NIL);
  ir->op1 = ir->op2 = 0;
  ir->prev = 0;
}
```

注意,LuaJIT 的 DCE **不物理删除** IR(不移动数组、不压缩)。它只是把死指令改成 NOP。为什么?看文件头注释(`lj_opt_dce.c:62`):

```c
/* Note that compressing the IR to eliminate the NOPs does not pay off. */
```

压缩 IR(把 NOP 抹掉、后面的指令往前挪)要 O(n) 移动 + 重新分配所有 ref——得不偿失。NOP 在后端 asm 阶段会被自然跳过(asm 不为 NOP 生成机器码),留着它占的那一格 IR 空间,几乎没成本。这是 LuaJIT "省事但不省正确性"的一贯风格。

`continue` 跳过下面的操作数标记——死指令的操作数不用标(它都不活了,标它的操作数干嘛)。

**情况 C:没标记,但有副作用**。落到第 52-54 行:

```c
pchain[ir->o] = &ir->prev;
if (ir->op1 >= REF_FIRST) irt_setmark(IR(ir->op1)->t);
if (ir->op2 >= REF_FIRST) irt_setmark(IR(ir->op2)->t);
```

这条指令虽然没人引用它的"结果",但它有副作用(store/guard/call/alloc),不能删。它算"活的根"之一。于是:更新链指针,然后把它的两个操作数标活——因为要执行这条有副作用的指令,就得先有它的操作数。

**这三种情况合起来,就把"反向标记传播"和"删死代码"在一次扫描里做完了。** 没有"先全标一遍、再全扫一遍删"的两遍法,一遍搞定。这是它短的原因之一。

### 2.4 lj_opt_dce:入口 + 失效缓存

入口函数 `lj_opt_dce.c:64`:

```c
void lj_opt_dce(jit_State *J)
{
  if ((J->flags & JIT_F_OPT_DCE)) {
    dce_marksnap(J);
    dce_propagate(J);
    memset(J->bpropcache, 0, sizeof(J->bpropcache));  /* Invalidate cache. */
  }
}
```

三步:

1. 检查 `JIT_F_OPT_DCE` 标志位——用户可以用 `-O-dce` 关掉这个优化(虽然几乎没人关)。默认开。
2. `dce_marksnap` + `dce_propagate`,两阶段跑完。
3. `memset(J->bpropcache, 0, ...)`:**失效化 backpropagation 缓存**。`J->bpropcache`(`lj_jit.h:492`)是前向替换等优化用的一块缓存(记录"某个 ref 上次查到的传播结果")。DCE 删了一些指令、改了一些链,这些缓存的依据变了,必须清空,否则后续 pass 会拿到过期数据。这是个容易被忽略但极重要的细节——优化 pass 之间共享 IR 状态,改了 IR 就要负责通知别的 pass 缓存失效。

### 2.5 为什么 DCE 在循环优化之前跑

看一下 DCE 在整个 trace 编译流程里的位置。`lj_trace.c:720`(LJ_TRACE_END 分支):

```c
case LJ_TRACE_END:
  trace_pendpatch(J, 1);
  J->loopref = 0;
  if ((J->flags & JIT_F_OPT_LOOP) &&
      J->cur.link == J->cur.traceno && J->framedepth + J->retdepth == 0) {
    setvmstate(J2G(J), OPT);
    lj_opt_dce(J);                              // ← DCE 在这里
    if (lj_opt_loop(J)) { ... }                 // ← 循环优化
    J->loopref = J->chain[IR_LOOP];
  }
  lj_opt_split(J);
  lj_opt_sink(J);                               // ← sink 在这里
  ...
```

注意 DCE 在 `lj_opt_loop`(循环优化,下一章 P3-12)**之前** 跑。为什么?因为循环优化要把循环体首尾对接、做循环不变量外提,对接前先清掉死代码,能让循环优化看到更干净的 IR、做更准的分析。循环优化本身又会产生新的死代码(外提后原位置的指令可能没用了),但那些会在后续 pass 或 asm 阶段处理。

还有一点值得注意:`lj_opt_dce.c` 文件头注释第一行(`lj_opt_dce.c:3`):

```c
** DCE: Dead Code Elimination. Pre-LOOP only -- ASM already performs DCE.
```

"Pre-LOOP only"——这个独立的 DCE pass 只在循环优化前跑一次。"ASM already performs DCE"——后端汇编生成阶段(`lj_asm.c`)本身还会再做一次轻量 DCE(在分配寄存器时,asm 会跳过结果没人用的指令)。所以 LuaJIT 的死代码消除其实是 **两段式**:这个 pass 做主力的全局 DCE,asm 阶段做收尾的局部 DCE。本章讲的是主力那个。

DCE 讲完了。75 行,核心就是"反向标记 + 副作用判定 + NOP 替换"。下面进入本章的另一个重头戏——sink,它比 DCE 复杂得多(258 行),但精神一脉相承:也是"标记 + 反向传播 + 决定保留还是消除",只是要处理的对象(逃逸分析)更微妙。

---

## §3 第一性原理(续):什么样的分配能"沉掉"

### 3.1 逃逸:对象能不能被 sink 的分水岭

回到开头的例子:

```lua
local t = {}
t[1] = 1
t[2] = 2
return t[1] + t[2]
```

我们说这个 `t` 是"短命的临时对象",可以不真分配。但要凭什么下这个判断?凭的是一个概念:**逃逸**(escape)。

一个对象 **逃逸**,意思是它 **可能被当前这点代码之外的代码看到**。一旦逃逸,它就必须真实存在于内存里——因为外面的代码会用指针去找它、读写它。这时你不能把它"沉掉"变成寄存器里的几个值,因为外面拿不到寄存器。

对象不逃逸,意思是它 **只活在当前这点代码内部**——创建它的地方、用它的地方,都在一个封闭的范围里。这种对象,你完全可以用几个寄存器/栈槽来"模拟"它,根本不用在堆上分配。

逃逸的判据,落实到 LuaJIT 的 trace IR 里,就是看这个 table(或 cdata)对象的 **引用** 有没有被用在"危险的地方"。哪些地方危险?

1. **传给函数调用**(CALL 类)。函数可能把它存起来、返回出去、传给别的协程——一旦进了函数,就当它逃了。
2. **被 store 到别的对象里**。比如 `obj.field = t`,t 被存进另一个 table,那 obj 持有了 t 的引用,t 的生命超出当前范围。
3. **被 snapshot 引用**。这是 trace JIT 特有的逃逸路径,非常重要,我们单独用 §3.3 讲。
4. **被 upvalue 捕获、被全局变量持有**等等。

反过来,如果这个 table 的引用只出现在这几处:

- 创建它的指令(`TNEW`/`TDUP`)。
- 往它的某个槽 **写** 的 store 指令(`ASTORE`/`HSTORE`/`NEWREF`),而且写的槽是 **编译期常量下标**(`t[1]`、`t.k`)。
- 从它的某个槽 **读** 的 load 指令(`ALOAD`/`HLOAD`/`FLOAD`),而且也是常量下标。

那么这个 table 没逃逸,可以 sink。

### 3.2 sink 消除的是什么:sink 之后的执行模型

确认一个 table 没逃逸后,sink 会怎么改 IR?这里有个关键的认知调整:

> **LuaJIT 的 sink,并不是把 IR 真的改写成"用寄存器替代 table"的等价 IR。它只是给这些指令打上标记,告诉后端 asm "这个分配别真生成、这个 store 别真写内存"。**

具体说,sink 在 `TNEW`/`TDUP`(以及 FFI 的 `CNEW`/`CNEWI`)和相关的 `ASTORE`/`HSTORE`/`FSTORE`/`XSTORE`/`NEWREF` 指令的 `ir->prev` 字段里,写一个特殊值 `REGSP(RID_SINK, ...)`,意思大致是"这条指令的结果不分配实际存储,标记为 sunk"。同时把 `TNEW`/`TDUP` 上的 guard 位清掉(`ir->t.irt &= ~IRT_GUARD`)——因为分配都被消除了,原本挂在分配上的"分配成功"guard 也就没意义了。

后端 asm 扫到带 `RID_SINK` 标记的 TNEW,就 **不生成调用 `lj_tab_new` 的机器码**;扫到带 `RID_SINK` 的 ASTORE,就 **不生成往 table 写内存的机器码**,而是把要写的值"记下来"——如果后面有 load 读这个槽,asm 通过 store-to-load forwarding(前一章 P3-10 的机制)直接把这个值喂给 load;如果这个值要进 snapshot,asm 把它当作一个普通的 spill 值处理。

这样,sink 之后 `local t = {}; t[1]=1; t[2]=2; return t[1]+t[2]` 的机器码大致是:

```
mov  eax, 1           ; t[1] 的值,直接放寄存器
mov  edx, 2           ; t[2] 的值,直接放寄存器
add  eax, edx         ; t[1]+t[2],直接加
                     ; 完全没有调用 lj_tab_new、没有写内存、没有读内存
ret
```

一次 table 分配、两次内存写、两次内存读,全部省掉。这就是 sink 对 Lua 性能的意义——Lua 代码里到处都是临时 table(`{}` 构造、累加器、临时返回值),sink 把那些用完即扔的 table 全部变成寄存器里的几个值,GC 压力骤降。

### 3.3 snapshot 约束:trace JIT 特有的逃逸

现在讲那个 trace JIT 特有的、最容易踩坑的逃逸路径——**snapshot 引用**。

回忆主线(P0-01 §8):trace 是"一条线性热路径 + 若干 guard"。每个 guard 失败时要 side exit,side exit 时要靠 snapshot 把机器码状态恢复成解释器能认的样子,退回解释器继续跑。

现在考虑这个情形:trace 里 sink 掉了一个 table `t`(没真分配),但某个 guard 退出时,snapshot 里记录了"这时 t 的值应该是……"。问题来了:t 没真分配,snapshot 怎么恢复它?解释器拿到一个"应该存在但实际没分配"的 table,会崩溃。

所以:**如果一个对象的引用出现在某个 snapshot 里,这个对象就必须真实存在(至少在 side exit 那一刻),不能被完全 sink 掉。**

这是 trace JIT 区别于 method JIT 的一个独特约束。method JIT(V8/JVM)的 deoptimization 是把整个方法退回解释器,恢复的是栈帧级别的状态,处理逃逸对象靠的是"反优化时在堆上重建对象"。而 LuaJIT 的 side exit 是 **每个 guard 各自的小型退出**,snapshot 是细粒度的状态记录,sink 必须尊重它——snapshot 要的对象,就是逃逸的对象。

具体到源码,sink 的标记阶段会把所有 snapshot 引用的 IR 都标 mark(就是 §2.2 看到的 `irt_setmark`),被标 mark 的分配就是"snapshot 要的,不能 sink"。

但 LuaJIT 在这里做了一个 **精妙的折中**:sink 不是"全或无"的。一个对象即使进了 snapshot(部分逃逸),它身上那些 **没进 snapshot 的 store** 还是能被 sink——只要 snapshot 那一刻能重建出对象的状态。这就是为什么 `lj_asm.c:953` 的 `asm_snap_alloc1` 会处理 `RID_SINK`→`RID_SUNK` 的转换:如果一个对象进了 snapshot,asm 会把它从"完全 sink"降级成"sunk"——分配还是要做(为了 snapshot 能恢复),但相关 store 可以用特殊方式生成。这个细节我们在 §5.2 看 asm 消费时再讲。

### 3.4 循环里的 sink:PHI 约束

还有一类约束,只在 **循环 trace** 里出现,叫 PHI 约束。

循环 trace 的特征是:循环体首尾对接,循环开始时的某个变量(比如累加器 table)和循环结束时的它,是"同一个变量的两次出现"——这在 SSA 里用 **PHI 节点** 表示("这个变量在这条边上来的是值 A,在那条边上来的是值 B,合并一下")。

如果一个 table 在循环里被 sink,那循环回边时,这个 table 的"值"也要能传过去。但 sink 的 table 没有实体,只有"它存的若干个槽的值"。这些值要么:

- 是循环不变量(每次循环值都一样),那 sink 它没问题——回边时值不变。
- 是个 PHI(每次循环值会变),那就要小心:这些变化的值必须能在寄存器里正确地经过 PHI 传递。如果某个值依赖循环变量、又是个复杂表达式,sink 它可能让 PHI 处理不过来。

LuaJIT 用 `sink_checkphi`(`lj_opt_sink.c:51`)处理这个:检查 store 进 sink 对象的值,是不是个"可 sink 的 PHI"或"循环不变量"。是,才允许 sink;不是(循环变量、且无法简单表达),就拒绝 sink 这个分配。这个我们看源码时细讲。

### 3.5 为什么 sink 是 sound 的:三道闸

把上面几节合起来,sink 的 soundness(正确性)靠三道闸保证:

1. **逃逸分析闸**:只有确认不逃逸(没传给函数、没被外部 store、没进危险路径)的分配,才考虑 sink。逃逸的不动。
2. **snapshot 闸**:进了 snapshot 的对象,要么完全不 sink(它得能在 side exit 时恢复),要么降级处理(asm 时 `RID_SINK`→`RID_SUNK`,该分配还得做)。
3. **PHI 闸**:循环里 sink 的值,必须是可 sink 的 PHI 或循环不变量,保证回边传递正确。

三道闸都过,才 sink。任何一道没过,就当这个分配不能 sink,老老实实分配。**宁可漏 sink(性能损失),不可错 sink(正确性破坏)。** 这是所有优化的共同准则:sound 优先于性能。

---

## §4 源码印证:lj_opt_sink.c 的标记-传播-sweep

现在读 sink 的源码。258 行,比 DCE 长三倍多,但结构清晰,分四个阶段:**前置检查 → 标记(mark,找不 sink 的)→ (循环时)迭代重标 → 扫描落标(sweep,给可 sink 的打 RID_SINK)**。我们逐个看。

### 4.1 入口 lj_opt_sink:前置条件

`lj_opt_sink.c:240`:

```c
/* Allocation sinking and store sinking.
**
** 1. Mark all non-sinkable allocations.
** 2. Then sink all remaining allocations and the related stores.
*/
void lj_opt_sink(jit_State *J)
{
  const uint32_t need = (JIT_F_OPT_SINK|JIT_F_OPT_FWD|
			 JIT_F_OPT_DCE|JIT_F_OPT_CSE|JIT_F_OPT_FOLD);
  if ((J->flags & need) == need &&
      (J->chain[IR_TNEW] || J->chain[IR_TDUP] ||
       (LJ_HASFFI && (J->chain[IR_CNEW] || J->chain[IR_CNEWI])))) {
    if (!J->loopref)
      sink_mark_snap(J, &J->cur.snap[J->cur.nsnap-1]);
    sink_mark_ins(J);
    if (J->loopref)
      sink_remark_phi(J);
    sink_sweep_ins(J);
  }
}
```

抓住入口的几个要点:

**第一,前置优化标志(`need`)**。sink 不是单独跑的,它依赖前面一堆优化先跑过:`SINK`(自己)、`FWD`(前向替换,P3-10)、`DCE`(死代码消除)、`CSE`(公共子表达式)、`FOLD`(常量折叠)。为什么?

- 依赖 `FWD`:前向替换把"先 store 后 load"折成"直接用值",这是 sink 能起作用的前提——load 都被消掉了,sink 才能彻底不分配。如果 load 还在,sink 也只能消除分配,load 还是会去读那块不存在的内存。
- 依赖 `DCE`:死代码先清掉,IR 更干净,sink 的逃逸分析更准(不会被死引用误导)。
- 依赖 `CSE`/`FOLD`:把重复的分配、常量折叠后的指令合并,减少 sink 要处理的对象数。

只有这一串优化全开(默认全开),sink 才跑。少一个,sink 直接放弃——因为没它们,sink 做不 sound。

**第二,前置对象检查**。`J->chain[IR_TNEW] || J->chain[IR_TDUP] || ...`:这个 trace 里 **必须存在至少一条分配指令**(TNEW/TDUP,FFI 还有 CNEW/CNEWI)。如果一个分配都没有,sink 跑了也是空转,直接跳过。这是个聪明的短路——绝大多数 trace 不创建 table,这步检查让它们零开销。

**第三,分循环 vs 非循环**。`J->loopref` 是"循环引用"(循环 trace 才有,非循环 trace 为 0)。

- 非循环 trace(`!J->loopref`):只需要标 **最后一个** snapshot(`sink_mark_snap(J, &J->cur.snap[J->cur.nsnap-1])`)。为什么只标最后一个?因为非循环 trace 只有一个出口(末尾),只有那个 snapshot 的对象会逃到 trace 之外。中间的 snapshot 是 guard 退出用的,但 guard 退出退回解释器后,那个对象如果只在 trace 内部用,解释器也用不到它——所以中间 snapshot 引用的内部对象,不一定逃逸。这是个细节,但很关键:**只有 trace 末尾会真正"传出"对象**。
- 循环 trace(`J->loopref`):对象可能沿循环回边逃逸,要额外跑 `sink_remark_phi` 处理 PHI。

之后不管循环与否,都跑 `sink_mark_ins`(标记)和 `sink_sweep_ins`(落标)。

### 4.2 sink_checkalloc:识别"指向可 sink 分配的 store"

标记阶段反复用到一个辅助函数 `sink_checkalloc`(`lj_opt_sink.c:22`):

```c
/* Check whether the store ref points to an eligible allocation. */
static IRIns *sink_checkalloc(jit_State *J, IRIns *irs)
{
  IRIns *ir = IR(irs->op1);
  if (!irref_isk(ir->op2))
    return NULL;  /* Non-constant key. */
  if (ir->o == IR_HREFK || ir->o == IR_AREF)
    ir = IR(ir->op1);
  else if (!(ir->o == IR_HREF || ir->o == IR_NEWREF ||
	     ir->o == IR_FREF || ir->o == IR_ADD))
    return NULL;  /* Unhandled reference type (for XSTORE). */
  ir = IR(ir->op1);
  if (!(ir->o == IR_TNEW || ir->o == IR_TDUP || ir->o == IR_CNEW))
    return NULL;  /* Not an allocation. */
  return ir;  /* Return allocation. */
}
```

这个函数回答一个问题:**这条 store 指令(传进来的 `irs`),它写到的目标对象,是不是一个"有资格被 sink"的分配?** 如果是,返回那个分配指令的指针;不是,返回 NULL。

逐行看它怎么判断:

**第 25-26 行**:`irs->op1` 是 store 的"目标引用"(写到哪)。先看这个引用的 `op2`(下标/键)。`irref_isk` 判断是不是常量(`lj_ir.h:485`:`(ref) < REF_BIAS`)。如果键不是常量(比如 `t[i]`,i 是变量),**直接返回 NULL**——非定长下标的 store,sink 处理不了。为什么?因为 sink 要把"写的值"和"槽"对应起来做 store-to-load forwarding,槽必须是编译期已知的(槽 1、槽 "foo"),变量下标对应哪个槽编译时定不了。

**第 27-28 行**:如果目标是 `HREFK`(hash 表常量键引用)或 `AREF`(数组常量下标引用),这是 LuaJIR 的"定长引用"指令(已经把常量键/下标编进去了),顺着它的 `op1` 找到它引用的 table。

**第 29-31 行**:否则,目标必须是 `HREF`/`NEWREF`/`FREF`/`ADD` 这几种引用之一。`XSTORE`(往外部内存写,FFI 场景)的目标可能是 `ADD`(算出来的地址),特殊处理。如果不是这些已知类型,返回 NULL。

**第 32-34 行**:最后,顺着 `op1` 找到的那个指令,必须是 `TNEW`/`TDUP`/`CNEW`(分配指令)。如果不是分配(比如 store 到一个预先存在的全局 table),返回 NULL——只能 sink 自己 trace 里新建的对象,store 到老对象的不能动。

**一句话:`sink_checkalloc` 是 sink 的"资格初审"——它判定一条 store 是否写给了一个"自己造的、定长下标的"对象。只有初审通过的 store,才进入后续的 sink 决策。**

### 4.3 sink_mark_ins:反向标记"不能 sink 的"

重头戏来了。`sink_mark_ins`(`lj_opt_sink.c:82`)做的是"找所有 **不能** sink 的,标 mark"。它的思路和 DCE 的 `dce_propagate` 一脉相承:反向扫,从"根"开始标,传播。

函数顶端的注释(`lj_opt_sink.c:72-81`)列出了所有的"根":

```c
/* Mark non-sinkable allocations using single-pass backward propagation.
**
** Roots for the marking process are:
** - Some PHIs or snapshots (see below).
** - Non-PHI, non-constant values stored to PHI allocations.
** - All guards.
** - Any remaining loads not eliminated by store-to-load forwarding.
** - Stores with non-constant keys.
** - All stored values.
*/
```

这些根,每一个都对应一种"不能 sink"的情形。我们边看代码边对照。

```c
static void sink_mark_ins(jit_State *J)
{
  IRIns *ir, *irlast = IR(J->cur.nins-1);
  for (ir = irlast ; ; ir--) {
    switch (ir->o) {
    case IR_BASE:
      return;  /* Finished. */
```

主循环从最后一条 IR 反向扫,遇到 `IR_BASE`(IR 数组和常量数组的分界,`REF_BIAS` 位置)就结束。`IR_BASE` 之后的都是常量,不参与 sink。

```c
    case IR_ALOAD: case IR_HLOAD: case IR_XLOAD: case IR_TBAR: case IR_ALEN:
      irt_setmark(IR(ir->op1)->t);  /* Mark ref for remaining loads. */
      break;
```

**根之一:残留的 load**。如果前向替换没把这个 load 消掉(load 还在),说明这个对象的内存真的被读了——这时对象不能 sink(因为 sink 要求所有访问都通过 store-to-load forwarding 折成值)。把这个 load 的目标(`op1`)标 mark。注意 `TBAR`(table barrier,写屏障)和 `ALEN`(取数组长度)也算"读了对象"——它们操作对象的内部状态,sink 不了。

```c
    case IR_FLOAD:
      if (irt_ismarked(ir->t) || ir->op2 == IRFL_TAB_META)
	irt_setmark(IR(ir->op1)->t);  /* Mark table for remaining loads. */
      break;
```

**FLOAD**(读对象字段)特殊处理。两种情况要把它的目标 table 标 mark:

1. `irt_ismarked(ir->t)`:这条 FLOAD 自己被标了(说明它的结果被需要,那它读的 table 不能 sink)。
2. `ir->op2 == IRFL_TAB_META`:读的是 table 的 **metatable** 字段(`IRFL_TAB_META`,`lj_ir.h:199`)。metatable 影响语义(运算、查找都查它),读 metatable 的对象不能 sink——因为 sink 后对象没了,metatable 关系也丢了。

```c
    case IR_ASTORE: case IR_HSTORE: case IR_FSTORE: case IR_XSTORE: {
      IRIns *ira = sink_checkalloc(J, ir);
      if (!ira || (irt_isphi(ira->t) && !sink_checkphi(J, ira, ir->op2)))
	irt_setmark(IR(ir->op1)->t);  /* Mark ineligible ref. */
      irt_setmark(IR(ir->op2)->t);  /* Mark stored value. */
      break;
      }
```

**store 类指令**(ASTORE/HSTORE/FSTORE/XSTORE)。这里两条 mark:

第一条(行 98-99):**判定这条 store 的目标分配是否有资格 sink**。调 `sink_checkalloc` 初审,如果初审不过(`!ira`),或者初审过了但是个 PHI 分配且 `sink_checkphi` 判定存入的值不可 sink(`irt_isphi(ira->t) && !sink_checkphi(...)`),就把 store 的目标引用标 mark——这个分配不能 sink。注意这里 `irt_isphi(ira->t)`:`irt_isphi`(`lj_ir.h:448`)判断这个分配是否是循环里的 PHI(每次循环可能是不同的分配),PHI 分配的 sink 要额外检查存入的值。

第二条(行 100):**存入的值(`op2`)永远标 mark**。为什么?因为存入的值本身需要被"使用"(要写进对象),它的活跃性必须保证——asm 阶段要为它生成代码。这条 mark 不是说值"不能 sink",而是说"值必须被保留供 sink 后的 store-to-load forwarding 用"。

```c
#if LJ_HASFFI
    case IR_CNEWI:
      if (irt_isphi(ir->t) &&
	  (!sink_checkphi(J, ir, ir->op2) ||
	   (LJ_32 && ir+1 < irlast && (ir+1)->o == IR_HIOP &&
	    !sink_checkphi(J, ir, (ir+1)->op2))))
	irt_setmark(ir->t);  /* Mark ineligible allocation. */
#endif
      /* fallthrough */
    case IR_USTORE:
      irt_setmark(IR(ir->op2)->t);  /* Mark stored value. */
      break;
```

**FFI 专属**(`LJ_HASFFI`)。`CNEWI`(创建 cdata 并立即初始化)如果是 PHI,要检查它的初始值(`op2`,32 位下还有 HIOP 的高位部分)能不能 sink。不能的话,标 mark。然后落到 `USTORE`(往未类型化内存写)分支,把存入值标 mark。

```c
#if LJ_HASFFI
    case IR_CALLXS:
#endif
    case IR_CALLS:
      irt_setmark(IR(ir->op1)->t);  /* Mark (potentially) stored values. */
      break;
```

**根之一:调用指令**(`CALLS`/`CALLXS`)。调用是"黑盒",可能修改任何对象。这里把它的 `op1`(可能是被调用函数或目标对象)标 mark——保守起见,调用涉及的对象不能 sink。这对应 §3.1 说的"传给函数就当逃逸"。

```c
    case IR_PHI: {
      IRIns *irl = IR(ir->op1), *irr = IR(ir->op2);
      irl->prev = irr->prev = 0;  /* Clear PHI value counts. */
      if (irl->o == irr->o &&
	  (irl->o == IR_TNEW || irl->o == IR_TDUP ||
	   (LJ_HASFFI && (irl->o == IR_CNEW || irl->o == IR_CNEWI))))
	break;
      irt_setmark(irl->t);
      irt_setmark(irr->t);
      break;
      }
```

**PHI 节点**,循环 trace 特有。PHI 有两个操作数(`op1`=循环前值,`op2`=回边值)。这里:

- 先把两个操作数的 `prev` 字段清零(`irl->prev = irr->prev = 0`)。`prev` 字段在 sink 里被临时征用,记录"这个 PHI 分配上有几个可 sink 的 store"——`sink_checkphi` 里 `ira->prev++` 就是给它加一。清零是为了开始计数。
- 如果两个操作数 **都是同类分配**(都是 TNEW,或都是 TDUP 等),这是个"每轮循环都新建一个同型 table"的 PHI,**不标 mark**——这种 PHI 是 sink 的理想目标(每轮的 table 都不逃逸,可以分别 sink)。
- 否则(PHI 两边不是同型分配,比如一边是 table 一边是别的),两边都标 mark——这种 PHI 不能 sink。

```c
    default:
      if (irt_ismarked(ir->t) || irt_isguard(ir->t)) {  /* Propagate mark. */
	if (ir->op1 >= REF_FIRST) irt_setmark(IR(ir->op1)->t);
	if (ir->op2 >= REF_FIRST) irt_setmark(IR(ir->op2)->t);
      }
      break;
    }
  }
}
```

**默认分支:传播 mark**。所有其他指令(算术、比较等),如果它自己被标了 mark,或者它是个 guard(`irt_isguard`),就把它的两个操作数也标 mark。这就是反向传播——和 DCE 的 `dce_propagate` 同样的手法。注意 **guard 一定是根**(注释里的"All guards"):guard 是运行时检查,它"用"了它的操作数(要检查它们),所以 guard 的操作数都不能 sink。

注意这里有个微妙点:`irt_isguard(ir->t)` 让所有 guard 成为根。但 guard 本身不是"分配",为什么要标?因为 guard 可能在检查一个值,而这个值如果来自某个 sink 的对象……其实更准确地说,标 guard 是为了把 guard 涉及的对象引用传播出去,确保 sink 不会消除掉 guard 要用的对象。这是 sound 保障的一部分。

走完 `sink_mark_ins`,所有"不能 sink"的分配,类型里都带上了 `IRT_MARK`。剩下的(没标 mark 的)分配,就是 sink 的目标。

### 4.4 sink_mark_snap 和 sink_remark_phi:snapshot 与 PHI 的细化

`sink_mark_snap`(`lj_opt_sink.c:143`)很简单,就是把一个 snapshot 里引用的所有 IR 标 mark(和 DCE 的 `dce_marksnap` 几乎一样,只是这里只标一个指定的 snapshot):

```c
static void sink_mark_snap(jit_State *J, SnapShot *snap)
{
  SnapEntry *map = &J->cur.snapmap[snap->mapofs];
  MSize n, nent = snap->nent;
  for (n = 0; n < nent; n++) {
    IRRef ref = snap_ref(map[n]);
    if (!irref_isk(ref))
      irt_setmark(IR(ref)->t);
  }
}
```

入口 `lj_opt_sink` 里,只在 **非循环 trace** 时调它一次,标最后一个 snapshot。这对应 §3.3 的 snapshot 闸——非循环 trace 只有末尾会传出对象,中间 snapshot 的对象不外逃。

循环 trace 不调 `sink_mark_snap`,改调 `sink_remark_phi`(`lj_opt_sink.c:155`):

```c
/* Iteratively remark PHI refs with differing marks or PHI value counts. */
static void sink_remark_phi(jit_State *J)
{
  IRIns *ir;
  int remark;
  do {
    remark = 0;
    for (ir = IR(J->cur.nins-1); ir->o == IR_PHI; ir--) {
      IRIns *irl = IR(ir->op1), *irr = IR(ir->op2);
      if (!((irl->t.irt ^ irr->t.irt) & IRT_MARK) && irl->prev == irr->prev)
	continue;
      remark |= (~(irl->t.irt & irr->t.irt) & IRT_MARK);
      irt_setmark(IR(ir->op1)->t);
      irt_setmark(IR(ir->op2)->t);
    }
  } while (remark);
}
```

这个函数处理循环 trace 的 PHI 一致性问题。逻辑是:扫描所有 PHI 节点,如果一个 PHI 的两边(循环前值 / 回边值)mark 状态不一致,或它们的"可 sink store 计数"(`prev`)不一致,就 **把两边都标 mark**——保守地认为这个 PHI 不能 sink。

为什么要迭代(`do { ... } while (remark)`)?因为标一个 PHI 的两边 mark 后,可能让别的 PHI 的 mark 状态又变得不一致(连锁反应),所以要反复标到稳定。这是个不动点(fixpoint)算法——保证最终所有 PHI 的 mark 状态自洽。

这个迭代是 sink 比 DCE 长的一个原因:循环 trace 的 PHI 一致性需要反复求不动点,而非循环 trace 一遍就够。

### 4.5 sink_checkphi 和 sink_phidep:PHI 依赖检查

辅助函数 `sink_checkphi`(`lj_opt_sink.c:51`)和 `sink_phidep`(`lj_opt_sink.c:39`)处理"存入 PHI 分配的值,是不是可 sink 的"。`sink_checkphi`:

```c
static int sink_checkphi(jit_State *J, IRIns *ira, IRRef ref)
{
  if (ref >= REF_FIRST) {
    IRIns *ir = IR(ref);
    if (irt_isphi(ir->t) || (ir->o == IR_CONV && ir->op2 == IRCONV_NUM_INT &&
			     irt_isphi(IR(ir->op1)->t))) {
      ira->prev++;
      return 1;  /* Sinkable PHI. */
    }
    /* Otherwise the value must be loop-invariant. */
    if (ref < J->loopref) {
      /* Check for PHI dependencies, but give up after reasonable effort. */
      int work = 64;
      return !sink_phidep(J, ref, &work);
    } else {
      return 0;  /* Loop-variant. */
    }
  }
  return 1;  /* Constant (non-PHI). */
}
```

这个函数回答:**存进一个 PHI 分配的值 `ref`,能被 sink 吗?**

- 如果值本身就是个 PHI(`irt_isphi`),或者是个 int→num 转换且其输入是 PHI(`IR_CONV` + `IRCONV_NUM_INT`),那它是"可 sink 的 PHI"——返回 1,并给分配的 `prev` 计数加一(记录"这个分配有几个可 sink 的 PHI store")。
- 否则,值必须 **循环不变量**(loop-invariant)才能 sink。怎么判不变量?看它的定义位置 `ref` 是不是在循环之前(`ref < J->loopref`,`loopref` 是循环起点的 IR 引用)。循环之前定义的值,在循环里不变,可 sink。
- 但即使定义在循环之前,还要检查它 **不依赖任何 PHI**(否则它其实是循环变量的衍生)。`sink_phidep` 递归检查这个依赖,最多查 64 层(`work = 64`)——超过就放弃,当它不可 sink(保守)。这是个性能保护:防止极端情况下的深度递归。
- 定义在循环之内(`ref >= J->loopref`)且不是 PHI,那它是"循环变量",**不可 sink**——返回 0。

`irt_isphi`(`lj_ir.h:448`)是 `IRT_ISPHI`(0x40)位,标"这条指令是某个 PHI 的操作数"。这个位在循环优化(`lj_opt_loop`)阶段被设上,sink 在循环 trace 里就能查到。

### 4.6 sink_sweep_ins:落标,给可 sink 的打 RID_SINK

标记都做完后,最后一步 `sink_sweep_ins`(`lj_opt_sink.c:173`)扫一遍,把没标 mark(可 sink)的分配和 store,打上 `RID_SINK` 标记:

```c
static void sink_sweep_ins(jit_State *J)
{
  IRIns *ir, *irbase = IR(REF_BASE);
  for (ir = IR(J->cur.nins-1) ; ir >= irbase; ir--) {
    switch (ir->o) {
    case IR_ASTORE: case IR_HSTORE: case IR_FSTORE: case IR_XSTORE: {
      IRIns *ira = sink_checkalloc(J, ir);
      if (ira && !irt_ismarked(ira->t)) {
	int delta = (int)(ir - ira);
	ir->prev = REGSP(RID_SINK, delta > 255 ? 255 : delta);
      } else {
	ir->prev = REGSP_INIT;
      }
      break;
      }
```

对每条 store:再调一次 `sink_checkalloc` 找它的目标分配,如果分配存在(`ira`)且 **没被标 mark**(`!irt_ismarked(ira->t)`,说明可 sink),就给这条 store 的 `prev` 写 `REGSP(RID_SINK, delta)`。

`REGSP(r, s)` 是 `lj_target.h:44` 的 `((r) + ((s) << 8))`,把寄存器号 `r` 和 spill 槽 `s` 编码进一个 16 位值(存在 `ir->prev` 里)。这里 `r = RID_SINK`(`lj_target.h:24` = `RID_INIT-1`,一个特殊值,告诉 asm"这条 store 是 sunk 的")。`s = delta`(store 指令和它的分配指令之间隔了多少条 IR,封顶 255)——这个 delta 是给 asm 用的:asm 扫到 sunk store 时,要找到它属于哪个分配,用 delta 能快速定位(`ira + delta == irs`)。我们在 §5.2 看 asm 时会用到这个。

如果 store 不可 sink,`prev = REGSP_INIT`(`lj_target.h:46`,`REGSP(RID_INIT, 0)`),即"正常的、需要分配寄存器/spill 的指令"。

```c
    case IR_NEWREF:
      if (!irt_ismarked(IR(ir->op1)->t)) {
	ir->prev = REGSP(RID_SINK, 0);
      } else {
	irt_clearmark(ir->t);
	ir->prev = REGSP_INIT;
      }
      break;
```

**`NEWREF`** 是"往 hash 表插入新键"的指令(`lj_ir.h:94`,模式 `S`,有副作用——它真的改 table 结构)。它的特殊性在于:它既是个 store(插入键值),又可能触发 table rehash。如果它操作的 table 可 sink,标 `RID_SINK`;否则正常。

```c
#if LJ_HASFFI
    case IR_CNEW: case IR_CNEWI:
#endif
    case IR_TNEW: case IR_TDUP:
      if (!irt_ismarked(ir->t)) {
	ir->t.irt &= ~IRT_GUARD;
	ir->prev = REGSP(RID_SINK, 0);
	J->cur.sinktags = 1;  /* Signal present SINK tags to assembler. */
      } else {
	irt_clearmark(ir->t);
	ir->prev = REGSP_INIT;
      }
      break;
```

**分配指令本体**(`TNEW`/`TDUP`/`CNEW`/`CNEWI`)。这是 sink 的核心目标。如果没标 mark(可 sink):

1. `ir->t.irt &= ~IRT_GUARD`:**清掉 guard 位**。分配指令原本挂了 guard(分配可能失败——OOM),guard 是副作用。但既然要 sink(根本不分配了),这个"分配成功"的 guard 也就没意义了,清掉。这一步是 sink 能减少机器码的关键之一——少一个 guard 就少一段检查代码、少一个 snapshot。
2. `ir->prev = REGSP(RID_SINK, 0)`:标 sink。
3. `J->cur.sinktags = 1`:**置 trace 的 sinktags 标志**(`lj_jit.h:279`)。这个标志告诉后端 asm"这个 trace 里有 sunk 指令,扫描时要处理 RID_SINK"。

如果分配不可 sink,清 mark、恢复 `REGSP_INIT`。

```c
    case IR_PHI: {
      IRIns *ira = IR(ir->op2);
      if (!irt_ismarked(ira->t) &&
	  (ira->o == IR_TNEW || ira->o == IR_TDUP ||
	   (LJ_HASFFI && (ira->o == IR_CNEW || ira->o == IR_CNEWI)))) {
	ir->prev = REGSP(RID_SINK, 0);
      } else {
	ir->prev = REGSP_INIT;
      }
      break;
      }
```

**PHI 节点**。如果一个 PHI 的回边值(`op2`)是个没标 mark 的分配(TNEW/TDUP/...),这个 PHI 也标 `RID_SINK`——意思是"循环回边上的这个新分配,sink 掉"。这处理"每轮循环都 `local t = {}`"的情形:每轮的 t 都不逃逸,PHI 把它们都 sink。

```c
    default:
      irt_clearmark(ir->t);
      ir->prev = REGSP_INIT;
      break;
    }
  }
  for (ir = IR(J->cur.nk); ir < irbase; ir++) {
    irt_clearmark(ir->t);
    ir->prev = REGSP_INIT;
    /* The false-positive of irt_is64() for ASMREF_L (REF_NIL) is OK here. */
    if (irt_is64(ir->t) && ir->o != IR_KNULL)
      ir++;
  }
}
```

**默认分支**:其他指令,清 mark、恢复 `REGSP_INIT`。最后的循环处理常量区(`nk` 到 `REF_BASE` 之间),把所有常量的 mark 也清掉——因为标记是临时的,跑完 sink 必须全部清干净,还回去给后续 pass/asm 用。

至此 sink 完成。被标 `RID_SINK` 的分配和 store,在后端 asm 阶段会被特殊处理:不生成真正的分配/写内存代码。

---

## §5 为什么这样设计是 sound 的

讲完了源码,我们回头审视 sink 和 DCE 的 soundness。这一节是"为什么不会算错"的论证。

### 5.1 DCE 的 soundness:副作用表是完备的

DCE 的正确性,完全押在"副作用判定是否完备"上。我们已经在 §2.1 看到,LuaJIT 把每条 IR 操作码的副作用编码进 `lj_ir_mode` 表,`ir_sideeff` 用一次位运算查出。这个表是 **完备的**:

- 所有写类(store)标了 `IRM_S`。
- 所有 guard 在运行时被设了 `IRT_GUARD` 位(录制时挂 guard 的指令才有,§2.1 的位运算把它纳入)。
- 所有分配标了 `IRM_A`(TNEW/TDUP)。
- 所有 call 标了 `IRM_S`(CALLS/CALLXS)。

判据 `>= IRM_S` 一刀切:模式位 ≥ 0x60 的,全有副作用。这个判据 **保守**(宁可多判有副作用,留着不删),所以绝不会删掉有副作用的指令。而真正"算个值没人用"的纯计算指令(ADD/MUL/CONV),既不在写类、又没挂 guard,模式位 < 0x60,无副作用,可安全删。

DCE 唯一可能"漏"的,是删除了一条"理论上可删但被保守保留"的指令——那是性能损失,不是正确性损失。**DCE 永远不会删错。**

还有一道更底层的安全网:**DCE 删的指令,改成了 NOP,但 IR 数组结构不变(ref 编号不变)**。这意味着即便 DCE 判定有 bug(假设),误删了一条指令,它也只是变成 NOP——后续 asm 看到 NOP 不生成代码,但 IR 的 ref 链路、snapshot 引用都还在(只是那条 NOP 不产生值)。这种情况下程序会在 asm 阶段或运行时立刻暴露问题(assert 或崩溃),不会"悄悄算错"。这是 SSA + NOP 替换设计带来的"显式失败"特性,优于物理删除。

### 5.2 sink 的 soundness:三道闸的逐条论证

sink 的 soundness 比 DCE 复杂,因为它要保证的是"删掉一个分配后,程序行为不变"。我们逐条论证 §3.5 的三道闸。

**第一道闸:逃逸分析完备**。sink 的标记阶段(`sink_mark_ins`)把所有可能的逃逸路径都标了 mark:

- 残留的 load(`ALOAD`/`HLOAD`/`XLOAD`/`TBAR`/`ALEN`):对象被读了内存,逃。标。
- FLOAD 读 metatable(`IRFL_TAB_META`):metatable 影响语义,逃。标。
- store 到非常量键(`sink_checkalloc` 返回 NULL):下标不定,无法做 store-to-load forwarding,逃。标。
- 调用(`CALLS`/`CALLXS`):可能修改任意对象,逃。标。
- guard 的操作数(默认分支里 `irt_isguard`):guard 用了这个值,逃。标。

这些根覆盖了所有"对象可能被外部感知"的情形。**没有被这些根标记到的分配,就是确认只在内部定长访问的——可以 sink。** 这是完备的逃逸分析(在 trace 这个线性范围内)。

**第二道闸:snapshot 约束**。非循环 trace 标最后一个 snapshot(`sink_mark_snap`),循环 trace 通过 `sink_remark_phi` 的 PHI 一致性间接覆盖。被 snapshot 引用的对象标 mark,sink 不动它。

但这道闸有个精妙的"部分 sink"机制,值得单独讲——看后端 asm 怎么消费 `RID_SINK`。`lj_asm.c:953` 的 `asm_snap_alloc1`:

```c
/* Allocate register or spill slot for a ref that escapes to a snapshot. */
static void asm_snap_alloc1(ASMState *as, IRRef ref)
{
  IRIns *ir = IR(ref);
  if (!irref_isk(ref)) {
    bloomset(as->snapfilt1, ref);
    bloomset(as->snapfilt2, hashrot(ref, ref + HASH_BIAS));
    if (ra_used(ir)) return;
    if (ir->r == RID_SINK || ir->r == RID_SUNK) {
      ir->r = RID_SUNK;
#if LJ_HASFFI
      if (ir->o == IR_CNEWI) {  /* Allocate CNEWI value. */
	asm_snap_alloc1(as, ir->op2);
	...
      } else
#endif
      {  /* Allocate stored values for TNEW, TDUP and CNEW. */
	IRIns *irs;
	lj_assertA(ir->o == IR_TNEW || ir->o == IR_TDUP || ir->o == IR_CNEW, ...);
	for (irs = IR(as->snapref-1); irs > ir; irs--)
	  if (irs->r == RID_SINK && asm_sunk_store(as, ir, irs)) {
	    ...
	    asm_snap_alloc1(as, irs->op2);
	    ...
	  }
      }
    } else {
      ...
```

这段逻辑:当一个 ref 要进 snapshot(逃逸到 side exit),asm 检查它是不是 `RID_SINK`。如果是,**把它降级成 `RID_SUNK`**(`ir->r = RID_SUNK`,`lj_target.h:25`,`RID_INIT-2`)。`RID_SUNK` 的意思是"这个对象本想 sink,但因为进了 snapshot,不得不分配——但分配用 sunk 方式生成"。

"用 sunk 方式生成"是什么意思?看 `asm_sunk_store`(`lj_asm.c:936`):

```c
static int asm_sunk_store(ASMState *as, IRIns *ira, IRIns *irs)
{
  if (irs->s == 255) {
    ...
    return (IR(irk->op1) == ira);
  } else {
    return (ira + irs->s == irs);  /* Quick check. */
  }
}
```

asm 用 store 的 `s` 字段(就是 sink_sweep 里存的 delta)快速判断"这条 store 是不是属于这个 sunk 分配"。是的话,asm 为这个 sunk 分配生成一段特殊代码:在 side exit 那一刻,临时分配一个 table,把 sunk 的 store 值填进去,再放进 snapshot。

**这就是 sink 的 snapshot 折中:sink 不生成日常运行的分配代码(快),但为可能的 side exit 准备了一份"按需重建"的代码(正确)。** 日常运行对象不存在(寄存器里几个值),side exit 时才现场造一个出来交给解释器。造的代价只在 side exit 那一刻付,绝大多数时候(不 side exit)完全不付。这是 trace JIT 把"快"和"安全"分离的又一例证。

**第三道闸:PHI 约束**。循环 trace 里,sink 的值必须能正确经过回边。`sink_checkphi` 判定的"可 sink PHI 或循环不变量"保证了这点:

- 可 sink PHI(`irt_isphi`):值是循环变量,但它的 PHI 处理就是寄存器到寄存器的传递,sink 后这个传递还在(只是不经过内存),正确。
- 循环不变量(`ref < loopref` 且不依赖 PHI):值每轮不变,sink 它每轮用同一个寄存器值,正确。

不可 sink 的(循环变量且非 PHI、或深度依赖 PHI),`sink_checkphi` 返回 0,标 mark,不 sink。**所以循环里 sink 的值,要么正确传递、要么根本没 sink。**

三道闸合起来,sink 的 soundness 有保证:**逃逸的不动、snapshot 要的降级处理、PHI 处理不了的放弃。** 任何不确定的情形,一律不 sink。这是"保守优先"的体现。

### 5.3 sink 与 DCE 的工程同构

讲到这里,你应该看到一个有意思的工程同构:sink 和 DCE,**算法骨架几乎一样**。都是:

1. **标记阶段**:反向扫 IR,从一组"根"开始,用 `IRT_MARK` 位标"不能动"的,传播到操作数。
2. **决定阶段**:扫一遍,标了 mark 的保留,没标的处理(DCE 删、sink 标 RID_SINK)。

差别只在"根"的定义和"处理"的方式:

- DCE 的根是"snapshot 引用 + 有副作用",处理是 NOP 替换。
- sink 的根是"逃逸路径(snapshot/call/残留 load/非定长 store/guard/PHI 不一致)",处理是 RID_SINK 标记 + 清 guard 位。

这种同构不是巧合——它是 **反向数据流分析**(backward dataflow analysis)的标准框架。无论删死代码还是消分配,本质上都是"从出口往回推,确定哪些是必要的,删掉不必要的"。LuaJIT 把这个框架用得很纯熟,DCE 75 行、sink 258 行,都是这个框架的精炼实例。

理解了这个同构,你就理解了编译器优化的一大类——它们看着名字不同(DCE/sink/死存储消除/部分冗余消除……),底层都是数据流分析。这是本章想传递给你的、超越 LuaJIT 本身的认知。

---

## §6 ★对照:官方 Lua + JVM/V8

### 6.1 对照一:官方 Lua(切"有没有优化")

官方 Lua 是纯解释器,**没有任何 IR、没有任何优化 pass**。`local t = {}; t[1]=1; return t[1]` 这种代码,官方 Lua 会:

1. `{}`:调用 `luaH_new` 真实分配一个 GCtab 结构(`lj_tab.c` / `ltab.c` 对应物),挂在 GC 链上。
2. `t[1]=1`:调用 `luaH_set` 真实写 table 的数组槽。
3. `return t[1]`:调用 `luaH_get` 真实读 table 的数组槽。
4. 函数返回后,t 没人引用,等下一轮 GC 增量扫描时回收。

全程一次分配、一次写、一次读、一次回收,四次内存相关操作,都在解释器里慢吞吞地走。官方 Lua 没有"这个 table 没逃逸,别分配"的概念——它是解释器,不做任何静态分析,每个操作按字面执行。

LuaJIT 加上 sink 后,同样的代码:分配被消除(寄存器里几个值),写读被 store-to-load forwarding 折成直接用值,**零次内存操作**。这就是 JIT 优化相对解释器的本质优势之一——不光是"机器码比解释快",更是"机器码可以做解释器做不了的静态分析,消除解释器必须做的冗余操作"。

但要注意:官方 Lua 的"不做优化"也是一种 sound 的选择。它永远按字面语义执行,简单、正确、可预测。LuaJIT 的 sink 是"在保证语义不变的前提下消除"——一旦分析出错(理论上),就会破坏语义。LuaJIT 用三道闸和保守策略把出错概率压到零,但代价是实现复杂度(258 行 + asm 配合)。**官方 Lua 用简单换 sound,LuaJIT 用复杂换性能。** 各有所长。

### 6.2 对照二:JVM / V8(逃逸分析 + 标量替换 vs allocation sinking)

JVM 和 V8 也有消除临时对象分配的优化,叫 **逃逸分析**(Escape Analysis, EA)+ **标量替换**(Scalar Replacement)。它的思路和 LuaJIT 的 sink 神似但实现路径不同,值得一对照。

**JVM/V8 的标量替换**:对一个确认不逃逸的对象,不把它存在堆/寄存器里作为一个"对象",而是 **把它的每个字段拆成独立的标量(单独的变量/寄存器)**。比如对象 `Point{x=1, y=2}`,标量替换后变成两个独立变量 `x=1`、`y=2`,各自走寄存器分配。对象的内存表示完全消失。

**LuaJIT 的 allocation sinking**:思路类似(都是"不真分配,用寄存器里的值替代"),但实现是 **标记 + asm 时跳过生成**,而不是"把对象拆成字段变量"。LuaJIT 不在 IR 层把 table 拆成若干独立 SSA 变量(那要重写 IR 结构),而是给 TNEW/ASTORE 打 RID_SINK 标记,让 asm 跳过分配代码、靠 store-to-load forwarding 喂值。对象在 IR 里还是个 TNEW,只是"不生成代码"。

两种实现的差别,根源在于 **IR 的粒度和编译单位**:

- JVM/V8 是 method JIT,IR 是整个方法的,可以做精细的字段级分析(把对象拆字段、做字段级 CSE/forwarding),标量替换是自然的。
- LuaJIT 是 trace JIT,IR 是一条线性路径,对象访问通常就是"造→定长写→定长读"的简单模式,用 sink + forwarding 足够,不需要拆字段。trace 的线性性反而让逃逸分析更简单(没有方法里的复杂控制流干扰)。

另一个差别是 **deoptimization 的处理**:

- JVM/V8 的 deoptimization 是方法级的,逃逸分析消除的对象在 deopt 时要在堆上 **重建**(用记录的字段值造一个真对象)。这个重建逻辑复杂。
- LuaJIT 的 side exit 是 guard 级的,sink 的对象如果进了 snapshot,用 `RID_SUNK` 降级(§5.2)——在 side exit 那一刻按需生成分配代码,把 sunk 的 store 值填进去。这个机制更轻量(只为确实会 side exit 的对象付代价)。

| 维度 | LuaJIT(allocation sinking) | 官方 Lua | JVM/V8(逃逸分析+标量替换) |
|---|---|---|---|
| 有无优化 | 有(标记 RID_SINK,asm 跳过) | 无(按字面执行) | 有(对象拆成字段标量) |
| 分析粒度 | trace 内,定长下标的 table/cdata | — | 方法内,任意对象的字段 |
| 消除方式 | asm 不生成分配代码 + store-to-load forwarding | — | IR 层拆字段,各自寄存器分配 |
| deopt 处理 | RID_SUNK 降级,side exit 时按需分配 | — | deopt 时堆上重建对象 |
| 复杂度 | 中(258 行 + asm 配合) | 无 | 高(方法级 EA 是大工程) |

这个对照凸显了 LuaJIT 的 trace 路线特色:**用更简单的分析(线性 trace + 定长访问),达到 method JIT 要花大力气才达到的效果(消除临时分配)**。当然代价是覆盖面窄(只覆盖 trace 内的简单模式,复杂场景 sink 不动),但对 Lua 的高频临时 table 模式,这个覆盖已经足够有效。

### 6.3 回扣主线

这一章讲的是两个收尾优化:删死代码(DCE)、消除不逃逸的分配(sink)。它们在主线上落在哪?

回到主线的三股张力——**快、安全、省**。DCE 和 sink 主要服务于"快"和"省":

- **快**:删掉死代码、消除临时分配,机器码更短、内存操作更少,CPU 跑得更快。
- **省**:消除临时分配,直接省掉 `malloc`/GC 开销——这是"省"的极致(连分配都省了)。Lua 程序里海量的临时 table,正是 sink 的用武之地。

而"安全"呢?DCE 和 sink 本身不增加 guard、不改变 snapshot,它们 **在保证语义不变的前提下**做优化。DCE 只删纯计算,sink 只动确认不逃逸的分配——它们是"安全的优化"。它们的存在,恰恰证明了 LuaJIT 的"安全"不仅仅是"假设失败能回退"(那是 guard 的事),还包括"优化不能破坏语义"这一层——这一层靠的是优化的 soundness 设计(完备的副作用表、三道闸)。

所以,这一章是"把动态执行 **安全** 变成机器码"里"安全"二字在优化阶段的体现:**优化可以激进(消除分配),但必须 sound(确认不逃逸才消除)**。LuaJIT 用 75 行 DCE 和 258 行 sink,把这条原则落到了源码里。

讲完 DCE 和 sink,P3 优化 pass 的主力已经讲完(折叠 P3-09、内存/别名 P3-10、DCE/sink 本章)。下一章是 P3 的最后一篇——循环优化与指令拆分,讲 trace 作为循环的特殊处理,以及 32 位平台上把 64 位运算拆成两条 32 位的 split pass。那里会把循环 trace 的 PHI、LOOP 标记、以及 sink 在循环里的协作(本章 §4.4 提到的 `sink_remark_phi`)串成一个完整的图景。

---

*下一章 [P3-12 循环优化与指令拆分](P3-12-循环优化与指令拆分.md):循环 trace 怎么把循环体首尾对接、做循环不变量外提;32 位平台上 64 位运算怎么拆成两条 32 位。这是 P3 优化篇的收尾,也是从优化过渡到后端(寄存器分配、汇编生成)的桥梁。*
