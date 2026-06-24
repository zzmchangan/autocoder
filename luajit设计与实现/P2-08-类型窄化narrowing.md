# P2-08 类型窄化 narrowing:把动态的 number 拆成 int 和 double

> **本书主线**:把动态执行安全变成机器码。**二分法**:解释器侧 ↔ JIT 侧。本章在 **JIT 侧**(trace 录制时的类型推断)。**★对照**:官方 Lua(只有一种 number) + V8(hidden class 推断对象形状 vs LuaJIT narrow 推断数值类型)。**源码**:LuaJIT 2.1.ROLLING,`lj_opt_narrow.c`(614 行,本章核心)。**基调**:纯直球,不用比喻;从第一性原理一步步推导。

---

## §0 本章要解决什么问题

读到这里,你已经知道 LuaJIT 的整套流程了:P0-01 讲过,先解释器跑,发现热点,录一条线性 trace,做乐观假设 + guard,生成机器码。trace 录制把字节码翻成 IR,P2-07 讲过 IR 是 SSA 形式。这一章,我们盯着 IR 里的**一个细节**看——这个细节是 LuaJIT 最招牌、最有特色的设计之一,也是它"动态语言跑出接近 C 速度"的关键所在。

这个细节是:**IR 里,数字到底用什么类型表示?**

听起来像个琐碎的小问题。但它直接决定了 LuaJIT 生成的机器码里,算术用整数指令还是浮点指令——而这两者性能差很大。这一章,我们就从这个问题出发,一步步推导出 LuaJIT 的"类型窄化"(narrowing)机制。

---

## §1 第一性原理:Lua 的 number,和 CPU 的两套算术

### 1.1 Lua 只有一种 number,而且是 double

先把最基础的事实摆清楚。Lua 5.1 的语言规范规定:

> Lua 里所有的数,都是 number 类型。而 number,在标准实现里就是一个双精度浮点数(IEEE 754 double)。

也就是说,你在 Lua 里写:

```lua
local a = 1
local b = 2
local c = a + b
```

`a`、`b`、`c` 三个变量,**没有一个是"整数"**。它们都是 double——一个 64 位的浮点数。`1` 在 Lua 里不是整型的 1,而是浮点的 1.0(只不过打印出来不显示小数点)。`a + b` 不是整数加法,而是浮点加法。

这是 Lua(以及 JavaScript、Python 2 的 int/long 区分之前)这类动态语言的标准选择:**统一用 double 表示所有数,省掉了"这个数到底是整数还是浮点"的类型判断。** 实现简单,语义清晰,数和数之间没有隐式转换的开销。

(注:LuaJIT 还有一个 DUALNUM 模式,允许 number 在内部区分 int 和 double 两种 tag。但语言层面看到的仍然是统一的 number,这只是为了在 ARM 这类浮点慢的机器上提速。我们先按标准单 number 模式讲,最后单独讲 DUALNUM。)

### 1.2 但 CPU 有两套完全独立的算术单元

问题来了。CPU 这块硅片上,有两套**完全独立**的算术执行单元:

1. **整数单元(integer ALU)**:做 32 位/64 位整数运算。`add eax, ebx` 这种。
2. **浮点单元(FPU,现在叫 SSE/AVX)**:做 IEEE 754 浮点运算。`addsd xmm0, xmm1` 这种。

这两套单元用不同的寄存器(整数用 eax/ebx/rdi 这种,浮点用 xmm0/xmm1 这种)、不同的指令、不同的执行端口(port)。在现代超标量 CPU 上,它们**并行**执行——整数单元和浮点单元可以同时干活,互不干扰。

现在,看 Lua 的算术 `a + b`。因为 a、b 都是 double,所以解释器做的肯定是**浮点加法**(`addsd`)。这没问题,结果一定对。

但是——这里有个**潜在的性能损失**:每次算术都走浮点单元,整数单元就闲着。CPU 的总执行带宽是整数单元和浮点单元带宽之和,不利用整数单元等于浪费一半带宽。更糟的是,在紧凑的 JIT 循环里,**浮点运算的延迟比整数运算长**(浮点加法 3-4 周期,整数加法 1 周期),这些延迟会暴露出来,拖慢循环。

### 1.3 一个观察:很多 number,实际值就是整数

但是程序员写循环,绝大多数时候,变量虽然名义上是 number(double),**实际值却是个整数**。看这个最经典的例子:

```lua
local x = 0
for i = 1, 1000000 do
  x = x + i
end
```

这里 `x` 和 `i` 都是 number(double)。但仔细想:

- `i` 从 1 涨到 1000000,**每一步都是整数**。
- `x` 从 0 开始,每次加一个整数,**永远也是整数**(而且远没到 int32 的 ±21 亿上限)。

如果能让这段循环走**整数加法**(`add eax, ebx`)而不是浮点加法(`addsd xmm0, xmm1`),就快得多——整数加法 1 周期、走整数单元不挤浮点端口、循环里的归纳变量更新延迟更低。

那能不能?能不能让 JIT 观察到"i 和 x 实际一直是整数",然后生成整数加法的机器码?

**这就是类型窄化要解决的问题。**

> **类型窄化(narrowing):把名义上是 number(可能是 int 或 double)的值,在 trace 里乐观地假设成"确定是 int",生成整数运算的 IR 和机器码。这是动态类型 → 具体类型的推断。**

这一章的全部内容,就是讲清楚:

1. 窄化怎么决定一个值能不能假设成 int?(条件)
2. 假设成 int 之后,万一运行时它不是 int(变成 double、或溢出了 int 范围),怎么保证不出错?(guard)
3. 这套机制在源码里怎么实现的?(`lj_opt_narrow.c`)

我们一步步来。

---

## §2 窄化前后:同一段循环,两套机器码

在讲机制之前,先用那段 `for i=1,1000000` 循环,把"窄化前"和"窄化后"的差别看清楚。这能让你直观理解为什么窄化这么重要。

### 2.1 不窄化:全程浮点

如果不做窄化,trace 录制会把 `x = x + i` 翻成这样的 IR(概念性,简化):

```
%1 = ADD x, i     ; 类型 IRT_NUM,double
```

后端生成 x86 机器码,大概长这样:

```asm
        ; x 在 xmm0,i 在 xmm1
    addsd  xmm0, xmm1     ; 浮点加法,3-4 周期
```

每次循环迭代,都做一次浮点加法。100 万次迭代,100 万次浮点加法。慢。

### 2.2 窄化后:全程整数

如果做了窄化,trace 录制观察到"x 和 i 实际值一直是整数,而且不会溢出 int32",于是把 IR 改成:

```
%1 = ADDOV x_int, i_int   ; 类型 IRT_INT,32 位整数,带溢出检查
```

后端生成的机器码:

```asm
        ; x 在 eax,i 在 ecx
    add    eax, ecx        ; 整数加法,1 周期
    jo     .side_exit      ; 溢出则跳到退出点(检查 OF 标志位)
```

每次循环,一次整数加法 + 一次溢出检查(`jo` 跳转指令,几乎永远不跳,分支预测器轻松搞定)。100 万次,100 万次整数加法。比浮点版本快得多。

而且——这还没完。整数加法走整数单元,把浮点单元彻底空出来给别的事用(如果循环里还有别的浮点计算,两者可以并行)。在超标量 CPU 上,这是双倍收益。

### 2.3 为什么安全:溢出就 side exit

窄化版本机器码里那条 `jo .side_exit` 是关键。它是 P0-01 §7 讲过的 **guard**:

- 大部分时候,加法不溢出,`jo` 不跳,机器码全速跑。快。
- 万一某次加法真的溢出了(比如 x 涨到了 21 亿再加一次),`jo` 发现 OF 标志位置位,跳到 `.side_exit`,靠 snapshot 恢复状态,退回解释器用浮点重新算。**结果一定对,只是慢一下。**

这就是窄化的核心契约:

> **乐观假设是 int → 生成最快的整数机器码 → 配一个 overflow guard 兜底 → 假设破了就 side exit 回解释器。** 和 P0-01 §7 完全一致的套路,只不过这里的 guard 不是"检查类型",而是"检查结果是否还在 int 范围"。

你看,**窄化是"乐观假设 + 运行时检查"这条主线在数值类型上的具体应用**。它不是一个新的设计哲学,而是你已经熟悉的 guard 套路在 int/double 上的落地。

---

## §3 窄化的难点:不只是"看到 int 就假设 int"

现在你可能会想:那窄化很简单啊?录制时看一眼操作数的运行时类型,如果是 int,就发射整数 IR,完事。

没那么简单。LuaJIT 的窄化要回答三个难题,这三个难题决定了 `lj_opt_narrow.c` 为什么有 600 多行。我们一个一个看。

### 3.1 难题一:普通的算术,**根本不该**窄化

先看一个反直觉的事实:**在单 number 模式下,LuaJIT 的普通算术(`x + y` 这种)默认是不窄化的。**

为什么?因为这往往**更慢**,不是更快。原因写在 `lj_opt_narrow.c` 文件开头那段长长的注释里(`lj_opt_narrow.c:22-90`),我们逐段看。

注释开头先说**解释器**的情况:

> Lua has only a single number type and this is a FP double by default. Narrowing doubles to integers does not pay off for the interpreter on a current-generation x86/x64 machine. Most FP operations need the same amount of execution resources as their integer counterparts, except with slightly longer latencies. Longer latencies are a non-issue for the interpreter, since they are usually hidden by other overhead.

大意:在解释器里,浮点和整数运算占的执行资源差不多,只是浮点延迟稍长一点。但解释器的其他开销太大(取指/译码/分发),这点延迟差被淹没了。所以解释器里不值得做整数窄化。

然后说一个更重要的原理——**整数和浮点单元是并行的,不利用浮点单元就是浪费**:

> The total CPU execution bandwidth is the sum of the bandwidth of the FP and the integer units, because they execute in parallel. The FP units have an equal or higher bandwidth than the integer units. Not using them means losing execution bandwidth. Moving work away from them to the already quite busy integer units is a losing proposition.

CPU 的总带宽 = 整数单元带宽 + 浮点单元带宽(因为并行)。浮点单元带宽往往等于甚至大于整数单元。**不利用浮点单元 = 浪费带宽。** 把工作从浮点单元挪到已经挺忙的整数单元,**是个亏本买卖**。

那为什么 JIT 又要窄化?注释接着说 JIT 的情况不同:

> The situation for JIT-compiled code is a bit different: the higher code density makes the extra latencies much more visible. Tight loops expose the latencies for updating the induction variables. Array indexing requires narrowing conversions with high latencies and additional guards (to check that the index is really an integer). And many common optimizations only work on integers.

JIT 代码密度高(没有解释器的取指译码开销遮挡),所以浮点的**额外延迟**就暴露出来了。具体三个场景:**紧凑循环里更新归纳变量(i++)的延迟看得见**、**数组下标(必须先 double→int 转换,延迟高)**、**很多优化只在整数上做**。

然后是一个关键设计判断:

> One solution would be speculative, eager narrowing of all number loads. This causes many problems, like losing -0 or the need to resolve type mismatches between traces. It also effectively forces the integer type to have overflow-checking semantics.

一种方案是"对所有 number load 都激进窄化"。问题一堆:丢 -0、trace 之间类型不匹配、被迫给整数加 overflow 检查。

> A better solution is to keep all numbers as FP values and only narrow when it's beneficial to do so. LuaJIT uses predictive narrowing for induction variables and demand-driven narrowing for index expressions, integer arguments and bit operations. Additionally it can eliminate or hoist most of the resulting overflow checks. **Regular arithmetic computations are never narrowed to integers.**

更好的方案:**保持所有 number 为浮点,只在有利可图时才窄化。** LuaJIT 的窄化分两类:

1. **归纳变量的预测式窄化(predictive)**:`for i=1,n` 的 i,窄化成 int。
2. **数组下标、位运算参数的需求驱动式窄化(demand-driven)**:这两个地方**必须**是 int(数组下标要 int 才能寻址、位运算只在 int 上定义),所以从需求出发,把相关的表达式都拉成 int。

**而普通的算术运算,从不窄化**——保持浮点,让浮点单元有事干。

这一段注释是整个 `lj_opt_narrow.c` 的设计纲领。你读到这里,就已经抓住了窄化的第一原则:**窄化不是越多越好,而是只在该用的地方用**。下面我们会看到,这两个类别(预测式 / 需求驱动式)分别对应源码里的两套函数。

### 3.2 难题二:溢出怎么处理

假设我们把归纳变量 `i` 窄化成了 int,每步 `i = i + step`(step 也是 int)。问题:**int 加法可能溢出**。`i` 涨到 `0x7fffffff` 再加 1,数学上应该是 `0x80000000`(21 亿多),但 32 位 int 装不下,会回绕(wrap-around)成负数 `-0x80000000`。

这会破坏语义。因为 Lua 的 number 是 double,double 能精确表示到 ±2^53,根本不存在"21 亿就溢出"这回事——按 Lua 语义,`i` 应该继续涨成 21 亿多、22 亿多……一直到 double 的精度极限。

所以,如果窄化成 int 做加法,必须**检测溢出**:一旦 `i + step` 超出 int32 范围,就不能用这个 int 结果(它错了),得退回 double(或者退回解释器)重新算。

LuaJIT 的做法是:**给整数加法配一个 overflow guard**。具体地,它有一组专门的 IR opcode(`lj_ir.h:84-86`):

```c
  /* Overflow-checking arithmetic ops. */
  _(ADDOV,	CW, ref, ref) \
  _(SUBOV,	NW, ref, ref) \
  _(MULOV,	CW, ref, ref) \
```

注意这三条 opcode 的命名规律:`ADDOV` = ADD + O(overflow)。它们和普通的 `IR_ADD`/`IR_SUB`/`IR_MUL`(`lj_ir.h:69-71`)**是不同的 opcode**。这是个常被误传的点:

> **常见误解**:窄化产生的整数加法,"加法"是普通的 `IR_ADD`,然后在后面插一条单独的"检查溢出"的 IR。
>
> **真相**:`ADDOV` 是一条**独立的 IR opcode**,它语义上等于"做加法,同时检查是否溢出,溢出则 guard 失败"。后端汇编生成时,一条 `add` 指令同时既算结果又设置 OF(overflow flag)标志位,紧跟一条 `jo`(jump if overflow)指令完成 guard。**不是两条 IR,是一条**。

为什么这样设计?文件头注释解释了(`lj_opt_narrow.c:67-74`):

> The integer type in the IR has convenient wrap-around semantics and ignores overflow. Extra operations have been added for overflow-checking arithmetic (ADDOV/SUBOV) instead of an extra type. Apart from reducing overall complexity of the compiler, this also nicely solves the problem where you want to apply algebraic simplifications to ADD, but not to ADDOV. And the x86/x64 assembler can use lea instead of an add for integer ADD, but not for ADDOV (lea does not affect the flags, but it helps to avoid register moves).

设计精妙处:

1. **普通的 `IR_ADD` 有 wrap-around 语义,故意忽略溢出**(因为很多时候我们知道不会溢出,比如数组下标加常量,不需要检查,这样能用更激进的代数化简)。
2. **需要检查溢出时,用 `IR_ADDOV`**,这是一条不同的指令。
3. 这样分开,**代数化简可以只对 `ADD` 做、不对 `ADDOV` 做**——因为 `ADDOV` 的语义和 `ADD` 不同(它要检查溢出),化简规则不通用。
4. 而且 x86 汇编生成时,`ADD` 可以用 `lea` 指令实现(`lea` 不影响标志位,还能省一次寄存器搬运),但 `ADDOV` 不能用 `lea`(它需要 `add` 来设置 OF 标志)。

所以,窄化产生的整数加法,IR 长这样:

```
%1 = ADDOV x_int, i_int   ; 做整数加法,溢出则 guard 失败
```

不是:

```
%1 = ADD x_int, i_int     ; 错误!这会忽略溢出,语义不对
%2 = GUARD_OVERFLOW %1    ; 不存在这种独立 IR
```

这一点务必记牢,它是理解后面窄化代码的钥匙。

### 3.3 难题三:数组下标的窄化要回溯

第三个难题是最微妙的,也是 `lj_opt_narrow.c` 大部分代码在处理的事。

看这段代码:

```lua
local t = {}
local x = 5
for i = 1, 100 do
  t[i + x] = i          -- 注意下标是 i + x
end
```

这里 `i` 是归纳变量,窄化成 int 了。`x` 是循环外常量 5,也是 int。**下标 `i + x` 数学上是整数,应该能窄化成整数加法,直接用于数组寻址。**

但是——录制器在录制 `t[i + x]` 这条字节码时,它会怎么做?它先看 `i + x` 这个表达式。问题来了:

- `i` 和 `x` 在 SSA 里是什么类型?如果 trace 用单 number 模式,它们是 `IRT_NUM`(double)。
- 那 `i + x` 录制出来,是 `IR_ADD` 类型 `IRT_NUM`,即浮点加法。
- 然后数组下标需要一个 int(数组在内存里是连续的,寻址要用整数偏移),所以录制器会发射一个**类型转换**:`CONV.int.num(i + x)`,把那个浮点结果转成 int。

合起来 IR 是:

```
%sum = ADD i_num, x_num          ; 浮点加法
%idx = CONV.int.num %sum         ; double → int 转换
```

这能跑,但是**非常浪费**:明明 i 和 x 实际都是 int,却先做浮点加法再转回 int。多了一次浮点加法(3-4 周期)+ 一次类型转换(还要检查转换没丢精度,又是一个 guard),纯粹是脱裤子放屁。

理想情况应该是:

```
%idx = ADDOV i_int, x_int        ; 整数加法,直接得到 int 下标
```

一次整数加法搞定。怎么从前面那种"先浮点加再转"优化成后面这种"直接整数加"?

这就需要**回溯(backpropagation)**:从那个"需要 int"的需求点(`CONV.int.num`)出发,**往回追溯**它的操作数来源,把路上的浮点运算**改写**成对应的整数运算。

具体地:

1. 看到 `CONV.int.num(%sum)`,需求是"把 %sum 转成 int"。
2. 往回看,%sum 是 `ADD(i_num, x_num)`。浮点加法。
3. 能不能把这个 ADD 改成整数 ADDOV?**只有当两个操作数都能安全地变成 int 时才行**。
4. 继续往回看 i 和 x:i 是归纳变量(已知是 int),x 是常量 5(能窄化成 int)。两者都能变 int。
5. 于是改写:把 `ADD(i_num, x_num)` 换成 `ADDOV(i_int, x_int)`,那个外层 `CONV` 就不需要了(因为 ADDOV 结果已经是 int)。

这就是**需求驱动的窄化**:不是看到 int 就窄化,而是**有"需要 int"的需求(数组下标、位运算)时,才从需求点往回追溯,把相关表达式拉成整数运算**。

注意这里的微妙之处:

- 如果 `i + x` 的某个操作数**不是整数**(比如 `i + 3.5`,3.5 无法窄化成 int),那回溯到这个操作数时必须**停下来**——不能把 `i + 3.5` 改成整数加法(3.5 没法表示成 int)。这时只能保留浮点加法 + 转换。
- 如果整个表达式树都不能转(比如 `a + b` 里 a、b 都是浮点),回溯失败,保留原来的浮点版本。
- 回溯还要**控制改写的规模**:不能无限制地往回追溯把整个程序都拉成整数(那样 IR 会爆炸)。`lj_opt_narrow.c` 限制了回溯深度和引入的类型转换数量。

这一套回溯逻辑,就是 `lj_opt_narrow.c` 里那几个 `narrow_conv_backprop` / `narrow_stripov_backprop` / `narrow_conv_emit` 函数干的事。是本章源码部分的重头戏。

---

## §4 小结:窄化的两类、三个难题

在进入源码之前,把概念理清楚。

**窄化分两类**(对应文件头注释的 predictive / demand-driven):

| 类别 | 触发场景 | 实现函数 | 例子 |
|---|---|---|---|
| 预测式(predictive) | for 循环的归纳变量 | `lj_opt_narrow_forl` | `for i=1,n` 的 i |
| 需求驱动式(demand-driven) | 数组下标、位运算参数、整数参数 | `lj_opt_narrow_index` / `lj_opt_narrow_tobit` / `lj_opt_narrow_toint` + 回溯 | `t[i+1]`、`bit.band(x, 0xff)` |

**普通算术 `x + y` 不窄化**(单 number 模式),保持浮点。

**三个难题**:

1. **不该处处窄化**:普通算术窄化反而慢(浪费浮点单元)。只有归纳变量和"必须 int"的需求点才窄化。
2. **溢出处理**:窄化成 int 后,加法可能溢出。用 `IR_ADDOV`/`SUBOV`/`MULOV` 这组**独立的 opcode**,语义是"做运算 + 检查溢出 + 溢出则 guard 失败"。
3. **数组下标要回溯**:从"需要 int"的需求点,往回追溯表达式树,把浮点运算改写成整数运算。

带着这三条,我们进源码。

---

## §5 源码印证:四组窄化函数

`lj_opt_narrow.c` 的导出函数(`lj_iropt.h:143-154` 声明,实现在 `lj_opt_narrow.c`)分成四组,正好对应上面的两类窄化 + 几个特化场景。我们一组一组看。

### 5.1 第一组:归纳变量窄化(预测式)

入口:`lj_opt_narrow_forl`(`lj_opt_narrow.c:590`)。它在录制 `FORL`(for 循环回边)字节码时被调用,调用点在 `lj_record.c:486` 和 `lj_record.c:551`。

```c
/* Narrow the FORL index type by looking at the runtime values. */
IRType lj_opt_narrow_forl(jit_State *J, cTValue *tv)
{
  lj_assertJ(tvisnumber(&tv[FORL_IDX]) &&
	     tvisnumber(&tv[FORL_STOP]) &&
	     tvisnumber(&tv[FORL_STEP]),
	     "expected number types");
  /* Narrow only if the runtime values of start/stop/step are all integers. */
  if (narrow_forl(J, &tv[FORL_IDX]) &&
      narrow_forl(J, &tv[FORL_STOP]) &&
      narrow_forl(J, &tv[FORL_STEP])) {
    /* And if the loop index can't possibly overflow. */
    lua_Number step = numberVnum(&tv[FORL_STEP]);
    lua_Number sum = numberVnum(&tv[FORL_STOP]) + step;
    if (0 <= step ? (sum <= 2147483647.0) : (sum >= -2147483648.0))
      return IRT_INT;
  }
  return IRT_NUM;
}
```

这段代码决定一个 for 循环的归纳变量到底用 int 还是 double。逻辑非常清晰,三步:

**第一步:看运行时值,判断 start/stop/step 三个数是不是都是整数。** 调 `narrow_forl`(`lj_opt_narrow.c:582`):

```c
/* Narrow a single runtime value. */
static int narrow_forl(jit_State *J, cTValue *o)
{
  if (tvisint(o)) return 1;
  if (LJ_DUALNUM || (J->flags & JIT_F_OPT_NARROW)) return lj_num2int_ok(numV(o));
  return 0;
}
```

它检查一个运行时值:

- 如果它已经是 int tag(`tvisint`,DUALNUM 模式才有),直接返回 1。
- 否则(它是 double),在 DUALNUM 模式或开了 `JIT_F_OPT_NARROW` 优化时,调 `lj_num2int_ok` 检查这个 double **能不能精确转成 int**(`lj_obj.h:1018`,底层是汇编 `lj_vm_num2int_check`,返回值 < 0 表示会丢精度)。

注意:`FORL_IDX`、`FORL_STOP`、`FORL_STEP` 是 for 循环的三个控制变量(索引、终止、步长),在 `tv` 数组里连续存放。这三个**必须都是整数**,循环才能窄化成 int。任何一个不是整数(比如步长是 0.5),整个循环退回 double。

**第二步:判断循环索引会不会溢出 int32。** 这是个静态的、保守的检查:

```c
lua_Number step = numberVnum(&tv[FORL_STEP]);
lua_Number sum = numberVnum(&tv[FORL_STOP]) + step;
if (0 <= step ? (sum <= 2147483647.0) : (sum >= -2147483648.0))
  return IRT_INT;
```

意思是:循环索引的最大值(或最小值)是 `stop + step`(最后一次迭代后的值)。如果这个值在 int32 范围内(`±2147483647`),那索引永远不会溢出,可以安全窄化。

为什么检查 `stop + step` 而不是 `stop`?因为循环是"先比较 idx ≤ stop,再 idx += step",所以最后一次实际更新后,idx 会短暂地等于 `stop + step`(在退出判断之前)。这个峰值必须不溢出。

**第三步:返回类型。** 都满足,返回 `IRT_INT`;否则 `IRT_NUM`。

注意,`lj_opt_narrow_forl` 返回的是个**类型**,不是 TRef。录制器拿到这个类型后,用它来决定循环变量、step、stop 在 IR 里用 int 还是 double(`lj_record.c:489-490` 的 `fori_arg` 调用,会把这三个变量都按这个类型加载/转换)。

这一组是"预测式"窄化的全部——**简单、保守、只看运行时值**。它不做表达式回溯,因为 for 循环的控制变量就是三个数,直接判断就行。

> **关键点**:归纳变量的窄化是"预测式"的——它不要求 trace 里这个变量一定用在"必须 int"的地方,而是**主动**把它窄化成 int,因为经验告诉我们 for 循环的归纳变量几乎总是整数、几乎总是用在循环计数和数组下标上。窄化它收益最大。

### 5.2 第二组:数组下标窄化(需求驱动式)

入口:`lj_opt_narrow_index`(`lj_opt_narrow.c:451`)。调用点在 `lj_record.c:1457`(`TRef ikey = lj_opt_narrow_index(J, key);`)。

```c
/* Narrow array index. */
TRef LJ_FASTCALL lj_opt_narrow_index(jit_State *J, TRef tr)
{
  IRIns *ir;
  lj_assertJ(tref_isnumber(tr), "expected number type");
  if (tref_isnum(tr))  /* Conversion may be narrowed, too. See above. */
    return emitir(IRTGI(IR_CONV), tr, IRCONV_INT_NUM|IRCONV_INDEX);
  /* Omit some overflow checks for array indexing. See comments above. */
  ir = IR(tref_ref(tr));
  if ((ir->o == IR_ADDOV || ir->o == IR_SUBOV) && irref_isk(ir->op2) &&
      (uint32_t)IR(ir->op2)->i + 0x40000000u < 0x80000000u)
    return emitir(IRTI(ir->o - IR_ADDOV + IR_ADD), ir->op1, ir->op2);
  return tr;
}
```

这段处理数组下标。逻辑:

**如果下标是 double**(`tref_isnum(tr)`):发射一个 `IR_CONV` 把它转成 int,模式是 `IRCONV_INT_NUM|IRCONV_INDEX`(`INT_NUM` 表示 double→int,`INDEX` 表示"这是数组下标,用特殊的回溯规则")。注意这个 CONV 是带 guard 的(`IRTGI`,`I` = INT,`G` = GUARD)——因为 double→int 转换可能丢精度(比如 3.5 转不成 int),丢了就 side exit。

**如果下标已经是 int**(走过了 else 分支):它可能是个 `ADDOV`/`SUBOV`(被前面的算术窄化产生的)。这里有个**优化**:如果这个 ADDOV/SUBOV 的第二个操作数是常量,且常量值的绝对值 ≤ 2^30(那个魔数 `0x40000000u` 的判断:`k + 0x40000000u < 0x80000000u` 等价于 `-0x40000000 ≤ k < 0x40000000`,即 ±2^30),那可以**把 ADDOV 降级成普通 ADD**——因为数组访问后面本来就有 bounds check(`IR_ABC`,unsigned 比较),溢出回绕成的负数会被 bounds check 抓住,不需要单独的 overflow guard。

这就是文件头注释(`lj_opt_narrow.c:161-173`)讲的那个优化:

> There's another optimization opportunity for array indexing: it's always accompanied by an array bounds-check. The outermost overflow check may be delegated to the ABC operation. This works because ABC is an unsigned comparison and wrap-around due to overflow creates negative numbers.
>
> But this optimization is only valid for constants that cannot overflow an int32_t into the range of valid array indexes [0..2^27+1). A check for +-2^30 is safe since -2^31 - 2^30 wraps to 2^30 and 2^31-1 + 2^30 wraps to -2^30-1.

数组下标后面必然跟着 bounds check(检查下标 < 数组长度)。如果下标因为溢出回绕成负数,unsigned 比较会把负数看成一个巨大的正数,大于数组长度,bounds check 失败。所以**外层的 overflow check 可以让 ABC 来做**,省一次显式 overflow guard。但只在偏移是常量且 ≤ 2^30 时安全(避免溢出后正好落到合法下标范围里)。

> 例子:`t[i+1]`、`t[i-10]` 这种最常见的情况,优化后就是 `ADD(i, 1)`,没有 ADDOV,没有 overflow guard,只有后面的 ABC bounds check。最终汇编可以把整个 `ADD` 融合进 load 指令的寻址模式(`lea` + 内存操作数),极致精简。这就是 LuaJIT 数组访问飞快的原因之一。

### 5.3 第三组:位运算参数和整数参数窄化(需求驱动 + 回溯剥除 overflow 检查)

入口有两个:`lj_opt_narrow_tobit`(`lj_opt_narrow.c:482`,位运算参数)和 `lj_opt_narrow_toint`(`lj_opt_narrow.c:466`,普通整数参数)。这两个长得很像,差别在**overflow 语义**。

先看 `lj_opt_narrow_tobit`(位运算参数):

```c
/* Narrow conversion to bitop operand (overflow wrapped). */
TRef LJ_FASTCALL lj_opt_narrow_tobit(jit_State *J, TRef tr)
{
  if (tref_isstr(tr))
    tr = emitir(IRTG(IR_STRTO, IRT_NUM), tr, 0);
  if (tref_isnum(tr))  /* Conversion may be narrowed, too. See above. */
    return emitir(IRTI(IR_TOBIT), tr, lj_ir_knum_tobit(J));
  if (!tref_isinteger(tr))
    lj_trace_err(J, LJ_TRERR_BADTYPE);
  /*
  ** Wrapped overflow semantics allow stripping of ADDOV and SUBOV.
  ** MULOV cannot be stripped due to precision widening.
  */
  return narrow_stripov(J, tr, IR_SUBOV, (IRT_INT<<5)|IRT_INT|IRCONV_TOBIT);
}
```

位运算(`bit.band`、`bit.bor` 等)的参数,在 Lua 语义下是"把 number 当成 32 位无符号整数做位运算"。位运算**不在乎 overflow**(它就是按位操作,溢出回绕是正常的)。所以:

- 如果参数是 double,发射 `IR_TOBIT`(`lj_ir.h:138`,这是把 double 转成 32 位整数用于位运算的专用 opcode,语义是"加 2^52 魔数取低 32 位",不丢精度也不检查)。
- 如果参数已经是 int(比如前面算术产生的 `ADDOV` 结果),调 `narrow_stripov` 把 overflow 检查**剥掉**——因为位运算不关心溢出,那个 `ADDOV` 可以降级成普通 `ADD`(wrap-around 语义)。

`narrow_stripov`(`lj_opt_narrow.c:427`)是剥 overflow 检查的核心:

```c
/* Recursively strip overflow checks. */
static TRef narrow_stripov(jit_State *J, TRef tr, int lastop, IRRef mode)
{
  IRRef ref = tref_ref(tr);
  IRIns *ir = IR(ref);
  int op = ir->o;
  if (op >= IR_ADDOV && op <= lastop) {
    BPropEntry *bp = narrow_bpc_get(J, ref, mode);
    if (bp) {
      return TREF(bp->val, irt_t(IR(bp->val)->t));
    } else {
      IRRef op1 = ir->op1, op2 = ir->op2;  /* The IR may be reallocated. */
      op1 = narrow_stripov(J, op1, lastop, mode);
      op2 = narrow_stripov(J, op2, lastop, mode);
      tr = emitir(IRT(op - IR_ADDOV + IR_ADD,
		      ((mode & IRCONV_DSTMASK) >> IRCONV_DSH)), op1, op2);
      narrow_bpc_set(J, ref, tref_ref(tr), mode);
    }
  } else if (LJ_64 && (mode & IRCONV_SEXT) && !irt_is64(ir->t)) {
    tr = emitir(IRT(IR_CONV, IRT_INTP), tr, mode);
  }
  return tr;
}
```

它递归地把 `ADDOV`/`SUBOV`/(可能)`MULOV` 替换成对应的 `ADD`/`SUB`/`MUL`。关键那行:

```c
tr = emitir(IRT(op - IR_ADDOV + IR_ADD, ...), op1, op2);
```

`op - IR_ADDOV + IR_ADD` 这个表达式,把 opcode 从"overflow 版"映射到"普通版":`ADDOV → ADD`、`SUBOV → SUB`、`MULOV → MUL`。利用的是 IRDEF 里这两组 opcode 排列相邻且顺序一致(`lj_ir.h:69-71` 的 ADD/SUB/MUL,`lj_ir.h:84-86` 的 ADDOV/SUBOV/MULOV)。

注意 `lastop` 参数:位运算传 `IR_SUBOV`(只剥 ADDOV 和 SUBOV,**不剥 MULOV**,因为乘法剥了会丢精度——注释 `MULOV cannot be stripped due to precision widening`);整数参数传 `IR_MULOV`(三个都剥)。

`lj_opt_narrow_toint`(整数参数,`lj_opt_narrow.c:466`)和它几乎一样,区别在 `lastop` 传 `IR_MULOV`(全剥),因为整数参数的 overflow 是 undefined(可以 wrap)。

### 5.4 第四组:普通算术窄化(条件性)

入口:`lj_opt_narrow_arith`(`lj_opt_narrow.c:526`)。这是处理 `+`、`-`、`*`、`/` 这些算术字节码的主入口,调用点在 `lj_record.c:2486`。

```c
/* Narrowing of arithmetic operations. */
TRef lj_opt_narrow_arith(jit_State *J, TRef rb, TRef rc,
			 TValue *vb, TValue *vc, IROp op)
{
  rb = conv_str_tonum(J, rb, vb);
  rc = conv_str_tonum(J, rc, vc);
  /* Must not narrow MUL in non-DUALNUM variant, because it loses -0. */
  if ((op >= IR_ADD && op <= (LJ_DUALNUM ? IR_MUL : IR_SUB)) &&
      tref_isinteger(rb) && tref_isinteger(rc) &&
      lj_num2int_ok(lj_vm_foldarith(numberVnum(vb), numberVnum(vc),
				    (int)op - (int)IR_ADD)))
    return emitir(IRTGI((int)op - (int)IR_ADD + (int)IR_ADDOV), rb, rc);
  if (!tref_isnum(rb)) rb = emitir(IRTN(IR_CONV), rb, IRCONV_NUM_INT);
  if (!tref_isnum(rc)) rc = emitir(IRTN(IR_CONV), rc, IRCONV_NUM_INT);
  return emitir(IRTN(op), rb, rc);
}
```

这段是关键。逻辑:

1. 先把可能的字符串操作数转成 number(`conv_str_tonum`)。
2. **窄化条件**(那个大 `if`):
   - `op` 在 ADD 到 (DUALNUM ? MUL : SUB) 之间——即单 number 模式下只考虑 ADD/SUB,**不考虑 MUL**(因为乘法窄化会丢 `-0`,而 Lua 语义里 `0 * -1 = -0.0` 是有意义的)。
   - 两个操作数**在 IR 里已经是 int 类型**(`tref_isinteger(rb) && tref_isinteger(rc)`)。注意:这是说操作数来自归纳变量窄化或 DUALNUM,不是任意 double。
   - **折叠预演证明结果也是整数**:`lj_num2int_ok(lj_vm_foldarith(...))`——用运行时的值 vb/vc 实际算一遍(调 `lj_vm_foldarith`,这是常量折叠用的那个算术函数),看结果能不能精确转成 int。
3. **三个条件都满足**,发射 `ADDOV`/`SUBOV`/`MULOV`(`IRTGI`,带 guard)。
4. **否则**:把操作数转成 double(`IRTN(IR_CONV)`),发射普通浮点 `ADD`/`SUB`/`MUL`(`IRTN(op)`)。

这一段印证了 §3.1 那条原则:**普通算术默认走 double,只有当操作数运行时已知都是整数、且结果也是整数时,才窄化成 overflow 检查的整数运算**。窄化是机会主义的,不是强制的。

注意那个映射:`(int)op - (int)IR_ADD + (int)IR_ADDOV`,把 ADD/SUB/MUL 映射到 ADDOV/SUBOV/MULOV,同样是利用 opcode 相邻排列。

还有两个类似的特化函数:

- `lj_opt_narrow_unm`(`lj_opt_narrow.c:543`,一元负号):对 `-x`,如果 x 是 int 且不是 0(避免 `-0`),也不是 `0x80000000`(避免 `-INT_MIN` 溢出),发射 `SUBOV(0, x)`;否则转 double 走 `IR_NEG`。
- `lj_opt_narrow_mod`(`lj_opt_narrow.c:559`,取模 `%`):如果两个操作数都是 int 且除数非 0,发射整数 `IR_MOD`;否则退回 `b - floor(b/c)*c` 的浮点序列。

这几个函数的共同模式:**先检查能不能窄化(操作数类型 + 运行时值),能就发射整数 IR 配 guard,不能就老老实实走 double**。

---

## §6 源码印证:回溯式窄化的算法

现在看本章最复杂的部分:**数组下标的需求驱动式窄化,怎么从那个 `IR_CONV` 往回追溯,把表达式树拉成整数运算**。这是 §3.3 讲的那个回溯算法的源码。

入口是 `lj_opt_narrow_convert`(`lj_opt_narrow.c:405`),它在常量折叠 pass(`lj_opt_fold.c`)处理 `IR_CONV` 指令时被调用:

```c
/* Narrow a type conversion of an arithmetic operation. */
TRef LJ_FASTCALL lj_opt_narrow_convert(jit_State *J)
{
  if ((J->flags & JIT_F_OPT_NARROW)) {
    NarrowConv nc;
    nc.J = J;
    nc.sp = nc.stack;
    nc.maxsp = &nc.stack[NARROW_MAX_STACK-4];
    nc.t = irt_type(fins->t);
    if (fins->o == IR_TOBIT) {
      nc.mode = IRCONV_TOBIT;  /* Used only in the backpropagation cache. */
    } else {
      nc.mode = fins->op2;
    }
    if (narrow_conv_backprop(&nc, fins->op1, 0) <= 1)
      return narrow_conv_emit(J, &nc);
  }
  return NEXTFOLD;
}
```

`fins` 是当前正在优化的指令(这里是某个 `IR_CONV` 或 `IR_TOBIT`)。`fins->op1` 是要转换的那个值(可能是个 `ADD`、`SUB` 表达式)。

这个函数做两件事:

1. **回溯收集**:`narrow_conv_backprop` 从 `fins->op1` 出发,递归往回追溯,把表达式树"翻译"成一串**栈机指令**(存到 `nc.stack` 里)。返回值是"需要引入多少个额外的类型转换"(>1 就放弃,保留原来的浮点版本)。
2. **发射**:`narrow_conv_emit` 执行那串栈机指令,真正生成 IR。

为什么用栈机?文件头注释(`lj_opt_narrow.c:148-159`)解释了:

> Using on-the-fly backpropagation of an expression tree doesn't work because it's unknown whether the transform is correct until the end. This either requires IR rollback and cache invalidation for every subtree or a two-pass algorithm. The former didn't work out too well, so the code now combines a recursive collector with a stack-based emitter.

一边回溯一边发射 IR 不行,因为**到回溯结束才知道这个变换对不对**(可能某个子表达式不能转,整个就得放弃)。如果边走边发射,发现不对时要回滚已经发射的 IR,很麻烦。所以分两遍:**先回溯收集(递归),把"要做什么"记成栈机指令序列;收集完了确认没问题,再用一个栈机统一执行(发射 IR)**。

栈机指令是 32 位格式(`lj_opt_narrow.c:186-195`):

```c
/* The stack machine has a 32 bit instruction format: [IROpT | IRRef1]
** The lower 16 bits hold a reference (or 0). The upper 16 bits hold
** the IR opcode + type or one of the following special opcodes:
*/
enum {
  NARROW_REF,		/* Push ref. */
  NARROW_CONV,		/* Push conversion of ref. */
  NARROW_SEXT,		/* Push sign-extension of ref. */
  NARROW_INT		/* Push KINT ref. The next code holds an int32_t. */
};
```

四种"指令":

- `NARROW_REF`:把一个 IR 引用压栈(这个值原样使用)。
- `NARROW_CONV`:对一个引用做类型转换(发射 `IR_CONV`),结果压栈。
- `NARROW_SEXT`:符号扩展(用于 int → int64)。
- `NARROW_INT`:压一个整数常量(`KINT`)。

加上普通的 IROpT(上 16 位是 opcode+type,下 16 位是引用),表示"弹出栈顶两个,做这个运算,结果压栈"。

### 6.1 回溯收集:narrow_conv_backprop

核心函数 `narrow_conv_backprop`(`lj_opt_narrow.c:265`),逐段看:

```c
/* Backpropagate narrowing conversion. Return number of needed conversions. */
static int narrow_conv_backprop(NarrowConv *nc, IRRef ref, int depth)
{
  jit_State *J = nc->J;
  IRIns *ir = IR(ref);
  IRRef cref;

  if (nc->sp >= nc->maxsp) return 10;  /* Path too deep. */
```

先看栈满没满(`NARROW_MAX_STACK = 256`,`lj_opt_narrow.c:184`)。满了返回 10(一个大于 1 的数,表示"放弃",因为外层只接受 ≤ 1 的返回值)。

```c
  /* Check the easy cases first. */
  if (ir->o == IR_CONV && (ir->op2 & IRCONV_SRCMASK) == IRT_INT) {
    if ((nc->mode & IRCONV_CONVMASK) <= IRCONV_ANY)
      narrow_stripov_backprop(nc, ir->op1, depth+1);
    else
      *nc->sp++ = NARROWINS(NARROW_REF, ir->op1);  /* Undo conversion. */
    if (nc->t == IRT_I64)
      *nc->sp++ = NARROWINS(NARROW_SEXT, 0);  /* Sign-extend integer. */
    return 0;
  }
```

**情况一:当前节点是个 `CONV(int → ...)`**(源类型是 int)。说明这里**已经有一个 int 了**(可能是归纳变量窄化产生的)。那直接用这个 int,不需要再转:

- 如果当前模式是 `IRCONV_ANY` 或更弱(`<= IRCONV_ANY`),进一步往回追溯它的源(`narrow_stripov_backprop`,可能剥掉 overflow 检查)。
- 否则,直接压一个 `NARROW_REF`(用这个 int 值,撤销外层的转换)。
- 如果目标是 int64,加一个符号扩展。
- 返回 0(不需要额外转换)。

这就是"撤销冗余转换"——`CONV(int → num)` 后面又 `CONV(num → int)`,两个抵消。

```c
  } else if (ir->o == IR_KNUM) {  /* Narrow FP constant. */
    lua_Number n = ir_knum(ir)->n;
    int64_t i64;
    int32_t k;
    if ((nc->mode & IRCONV_CONVMASK) == IRCONV_TOBIT) {
      /* Allows a wider range of constants, if const doesn't lose precision. */
      if (lj_num2int_check(n, i64, k)) {
	*nc->sp++ = NARROWINS(NARROW_INT, 0);
	*nc->sp++ = (NarrowIns)k;
	return 0;
      }
    } else if (lj_num2int_cond(n, i64, k, checki16((int32_t)i64))) {
      /* Only if constant is a small integer. */
      *nc->sp++ = NARROWINS(NARROW_INT, 0);
	*nc->sp++ = (NarrowIns)k;
	return 0;
    }
    return 10;  /* Never narrow other FP constants (this is rare). */
  }
```

**情况二:当前节点是个浮点常量**(`IR_KNUM`)。尝试把这个 double 常量窄化成 int 常量:

- 如果是 `IRCONV_TOBIT` 模式(位运算),允许更宽的范围,只要不丢精度(`lj_num2int_check`)。
- 否则(下标等),**只在常量是小整数时**才窄化(`checki16`,16 位范围)。
- 窄化成功,压一个 `NARROW_INT` + 整数值。
- 否则返回 10(放弃,浮点常量不窄化,这种情况少见)。

这一段处理 `t[i + 1]` 里的那个 `1`——它是个 double 常量 1.0,能窄化成 int 常量 1。

```c
  /* Try to CSE the conversion. Stronger checks are ok, too. */
  cref = J->chain[fins->o];
  while (cref > ref) {
    IRIns *cr = IR(cref);
    if (cr->op1 == ref &&
	(fins->o == IR_TOBIT ||
	 ((cr->op2 & IRCONV_MODEMASK) == (nc->mode & IRCONV_MODEMASK) &&
	  irt_isguard(cr->t) >= irt_isguard(fins->t)))) {
      *nc->sp++ = NARROWINS(NARROW_REF, cref);
      return 0;  /* Already there, no additional conversion needed. */
    }
    cref = cr->prev;
  }
```

**情况三:尝试 CSE**(公共子表达式消除)。看之前有没有对同一个 ref 做过同样的转换,有就复用,不用重新转换。这是为了避免在循环里重复生成相同的转换 IR。

```c
  /* Backpropagate across ADD/SUB. */
  if (ir->o == IR_ADD || ir->o == IR_SUB) {
    /* Try cache lookup first. */
    IRRef mode = nc->mode;
    BPropEntry *bp;
    /* Inner conversions need a stronger check. */
    if ((mode & IRCONV_CONVMASK) == IRCONV_INDEX && depth > 0)
      mode += IRCONV_CHECK-IRCONV_INDEX;
    bp = narrow_bpc_get(nc->J, (IRRef1)ref, mode);
    if (bp) {
      *nc->sp++ = NARROWINS(NARROW_REF, bp->val);
      return 0;
    } else if (nc->t == IRT_I64) {
      /* Try sign-extending from an existing (checked) conversion to int. */
      mode = (IRT_INT<<5)|IRT_NUM|IRCONV_INDEX;
      bp = narrow_bpc_get(nc->J, (IRRef1)ref, mode);
      if (bp) {
	*nc->sp++ = NARROWINS(NARROW_REF, bp->val);
	*nc->sp++ = NARROWINS(NARROW_SEXT, 0);
	return 0;
      }
    }
```

**情况四:当前节点是 `ADD` 或 `SUB`**——这是回溯的核心。先查回溯缓存(`bpropcache`,见 §6.3),看这个 ADD/SUB 之前有没有被窄化过,有就复用结果。缓存 miss 才继续往下走。

```c
    if (++depth < NARROW_MAX_BACKPROP && nc->sp < nc->maxsp) {
      NarrowIns *savesp = nc->sp;
      int count = narrow_conv_backprop(nc, ir->op1, depth);
      count += narrow_conv_backprop(nc, ir->op2, depth);
      /* Limit total number of conversions. */
      if (count <= 1 && nc->sp < nc->maxsp) {
	*nc->sp++ = NARROWINS(IRT(ir->o, nc->t), ref);
	return count;
      }
      nc->sp = savesp;  /* Too many conversions, need to backtrack. */
    }
  }
```

真正递归回溯:**对 ADD 的两个操作数,各自递归回溯**。累计两边需要的额外转换数(`count`)。如果总数 ≤ 1(至多引入一个额外转换,保证不爆炸),压一个普通的 IROpT 指令(表示"做这个 ADD,结果类型是 nc->t"),返回 count。

如果 count > 1(引入太多转换,不划算),**回滚栈指针**(`nc->sp = savesp`),放弃这条路径。这保证了窄化不会无限制地扩展表达式树。

```c
  /* Otherwise add a conversion. */
  *nc->sp++ = NARROWINS(NARROW_CONV, ref);
  return 1;
}
```

**默认情况**:这个节点没法继续回溯(不是 CONV、不是常量、不是 ADD/SUB,或者是 MUL/DIV 这种不支持的),那就**老老实实给它加一个类型转换**——压一个 `NARROW_CONV`,表示"对这个 ref 发射一个 IR_CONV"。返回 1(引入了一个转换)。

这个返回值很重要:外层 ADD 在判断 `count <= 1` 时,如果两个操作数都需要转换(count = 2),就放弃整条路径——因为那样会引入两个 CONV,不如保留原来的单个 CONV 划算。

### 6.2 发射:narrow_conv_emit

回溯收集完,`narrow_conv_emit`(`lj_opt_narrow.c:357`)执行那串栈机指令,真正生成 IR:

```c
/* Emit the conversions collected during backpropagation. */
static IRRef narrow_conv_emit(jit_State *J, NarrowConv *nc)
{
  /* The fins fields must be saved now -- emitir() overwrites them. */
  IROpT guardot = irt_isguard(fins->t) ? IRTG(IR_ADDOV-IR_ADD, 0) : 0;
  IROpT convot = fins->ot;
  IRRef1 convop2 = fins->op2;
  NarrowIns *next = nc->stack;  /* List of instructions from backpropagation. */
  NarrowIns *last = nc->sp;
  NarrowIns *sp = nc->stack;  /* Recycle the stack to store operands. */
  while (next < last) {  /* Simple stack machine to process the ins. list. */
    NarrowIns ref = *next++;
    IROpT op = narrow_op(ref);
    if (op == NARROW_REF) {
      *sp++ = ref;
    } else if (op == NARROW_CONV) {
      *sp++ = emitir_raw(convot, ref, convop2);  /* Raw emit avoids a loop. */
    } else if (op == NARROW_SEXT) {
      lj_assertJ(sp >= nc->stack+1, "stack underflow");
      sp[-1] = emitir(IRT(IR_CONV, IRT_I64), sp[-1],
		      (IRT_I64<<5)|IRT_INT|IRCONV_SEXT);
    } else if (op == NARROW_INT) {
      lj_assertJ(next < last, "missing arg to NARROW_INT");
      *sp++ = nc->t == IRT_I64 ?
	      lj_ir_kint64(J, (int64_t)(int32_t)*next++) :
	      lj_ir_kint(J, *next++);
    } else {  /* Regular IROpT. Pops two operands and pushes one result. */
      IRRef mode = nc->mode;
      lj_assertJ(sp >= nc->stack+2, "stack underflow");
      sp--;
      /* Omit some overflow checks for array indexing. See comments above. */
      if ((mode & IRCONV_CONVMASK) == IRCONV_INDEX) {
	if (next == last && irref_isk(narrow_ref(sp[0])) &&
	  (uint32_t)IR(narrow_ref(sp[0]))->i + 0x40000000u < 0x80000000u)
	  guardot = 0;
	else  /* Otherwise cache a stronger check. */
	  mode += IRCONV_CHECK-IRCONV_INDEX;
      }
      sp[-1] = emitir(op+guardot, sp[-1], sp[0]);
      /* Add to cache. */
      if (narrow_ref(ref))
	narrow_bpc_set(J, narrow_ref(ref), narrow_ref(sp[-1]), mode);
    }
  }
  lj_assertJ(sp == nc->stack+1, "stack misalignment");
  return nc->stack[0];
}
```

这是一个标准的栈机解释器:

- `NARROW_REF`:压栈。
- `NARROW_CONV`:发射一个 `IR_CONV`,结果压栈。
- `NARROW_SEXT`:弹出栈顶,做符号扩展,压回。
- `NARROW_INT`:压一个整数常量。
- 普通 IROpT(ADD/SUB):弹出两个,做运算,**加上 guardot**(如果原 CONV 是 guard 的,这里发射的就是 ADDOV 而不是 ADD),结果压栈。

注意那个 `guardot` 的计算(`lj_opt_narrow.c:360`):

```c
IROpT guardot = irt_isguard(fins->t) ? IRTG(IR_ADDOV-IR_ADD, 0) : 0;
```

如果原来的 CONV 指令是带 guard 的(`irt_isguard`,即 `IRCONV_INDEX` 模式下的下标转换),那 `guardot` 是一个"加到 opcode 上让它变成 overflow 版"的偏移量(`IR_ADDOV - IR_ADD`)。发射 `ADD` 时,`op + guardot` 就变成 `ADDOV`。**这一步完成了"浮点 ADD → 整数 ADDOV"的转换,overflow 检查自动带上。**

还有那个数组下标的 overflow 优化(`lj_opt_narrow.c:387-393`):

```c
if ((mode & IRCONV_CONVMASK) == IRCONV_INDEX) {
  if (next == last && irref_isk(narrow_ref(sp[0])) &&
    (uint32_t)IR(narrow_ref(sp[0]))->i + 0x40000000u < 0x80000000u)
    guardot = 0;
  else  /* Otherwise cache a stronger check. */
    mode += IRCONV_CHECK-IRCONV_INDEX;
}
```

如果这是最外层的 ADD(数组下标),且一个操作数是 ≤ 2^30 的常量,那 `guardot = 0`——**不发 ADDOV,发普通 ADD**,让后面的 ABC bounds check 来兜底(§5.2 讲过的优化)。否则,缓存里记一个更强的检查模式(`IRCONV_CHECK`),因为内层的转换需要更严格的检查。

最后,把这次窄化的结果存进回溯缓存(`narrow_bpc_set`),下次回溯到同一个 ref 直接命中。

### 6.3 回溯缓存:bpropcache

回溯可能很深,而且循环里同一个表达式会被反复处理。为了避免重复计算,有个回溯缓存 `bpropcache`(`lj_jit.h:492`):

```c
BPropEntry bpropcache[BPROP_SLOTS];  /* Backpropagation cache slots. */
uint32_t bpropslot;	/* Round-robin index into bpropcache slots. */
```

`BPropEntry`(`lj_jit.h:315`)和 `BPROP_SLOTS`(`lj_jit.h:322`):

```c
typedef struct BPropEntry {
  IRRef1 key;     /* Key: original IRRef. */
  IRRef1 val;     /* Value: narrowed IRRef. */
  IRRef mode;     /* Mode (IRCONV_*). */
} BPropEntry;

#define BPROP_SLOTS	16
```

16 个槽,轮转(round-robin)替换。查询 `narrow_bpc_get`(`lj_opt_narrow.c:214`):

```c
static BPropEntry *narrow_bpc_get(jit_State *J, IRRef1 key, IRRef mode)
{
  ptrdiff_t i;
  for (i = 0; i < BPROP_SLOTS; i++) {
    BPropEntry *bp = &J->bpropcache[i];
    /* Stronger checks are ok, too. */
    if (bp->key == key && bp->mode >= mode &&
	((bp->mode ^ mode) & IRCONV_MODEMASK) == 0)
      return bp;
  }
  return NULL;
}
```

线性扫描 16 个槽,匹配 key(原 IR 引用)+ 模式(允许更强的模式命中,`bp->mode >= mode`,因为更强的检查包含更弱的)。这是个很小的缓存,因为回溯的局部性很强——同一段表达式树在窄化过程中会被多次访问。

> **设计要点**:回溯缓存是"更强检查可以替代更弱检查"的。比如缓存里存了一个 `IRCONV_CHECK`(带检查的窄化结果),查询 `IRCONV_INDEX`(下标,需要检查)时也能命中——因为带检查的结果满足下标的要求。反过来不行。这避免了为每种模式都算一遍。

---

## §7 为什么这样设计是 sound 的

讲完了机制,我们回到第一性原理,问一个根本问题:**窄化,凭什么保证不改变程序语义?**

这是整个设计最核心的不变式。窄化把 double 改成了 int,这听起来很危险——int 会溢出、int 没有 -0、int 的精度只有 32 位。LuaJIT 怎么保证改完之后,程序结果和用 double 算的一模一样?

答案分三层。

### 7.1 第一层:窄化的条件本身就保证了"运行时确实是整数"

窄化**不是**无脑地把所有 number 当 int。它有严格的前提:

- **归纳变量窄化**(`lj_opt_narrow_forl`):要求 start/stop/step 三个运行时值**都是整数**,且 `stop + step` 不溢出 int32。这保证了整个循环期间,索引一直是整数、不会溢出。
- **普通算术窄化**(`lj_opt_narrow_arith`):要求两个操作数**在 IR 里已经是 int 类型**(来自归纳变量窄化或 DUALNUM),**且**折叠预演证明结果也是整数。这保证了这次运算的输入和输出都是精确的整数。
- **数组下标/位运算的需求驱动窄化**:从一个"需要 int"的需求点出发,回溯时只在**所有子表达式都能安全转成 int** 时才改写(回溯中遇到不能转的就停下来,保留浮点版本)。

也就是说,**窄化只在"有充分证据表明这段计算全程都是整数"时才发生**。它不是盲目乐观,是基于运行时证据的条件性乐观。

### 7.2 第二层:就算证据不足,guard 兜底

但"证据"是基于**当前观察**的。万一运行时情况变了——比如某次循环 i 突然不是整数了,或者加法结果溢出了 int32?

这就是 guard 的作用。每一种窄化,都配了对应的 guard:

- **整数加法溢出**:`ADDOV` opcode 自带 overflow 检查,汇编生成时 `add` + `jo`(溢出跳转)。溢出了 side exit。
- **double → int 转换丢精度**:`CONV` 带 guard(`IRTGI`),转换后发现 double 值和 int 值对不上(丢精度了),side exit。
- **归纳变量运行时类型变化**:`SLOAD`(栈加载)带 typecheck,加载后发现类型不对(本来是 int,变成了 double),side exit。
- **for 循环方向 / 步长变化**:`rec_for_check`(`lj_record.c:445`)发射 step 的方向 guard(`IR_GE`/`IR_LT`),步长正负变了就 side exit。

**每一个窄化假设,都有对应的 guard 兜底。** 这正是 P0-01 §7 讲的那条主线在数值类型上的全面落地。guard 触发就 side exit 回解释器,解释器用 double 重新算,**结果一定对**。

### 7.3 第三层:特殊值的处理

有几个特殊值需要单独保证:

- **-0(负零)**:Lua 语义里 `0 * -1 = -0.0`(IEEE 754 的负零)。如果窄化成 int,会丢掉这个信息(int 没有负零)。所以 `lj_opt_narrow_arith` 在单 number 模式下**不窄化 MUL**(`lj_opt_narrow.c:532` 那个 `LJ_DUALNUM ? IR_MUL : IR_SUB`),`lj_opt_narrow_unm` 对 0 也不窄化(`if (k != 0 && k != 0x80000000u)`)。
- **INT_MIN(0x80000000)**:`-INT_MIN` 数学上是 `0x80000000` 取反 = `0x7FFFFFFF + 1`,溢出。所以 `lj_opt_narrow_unm` 单独排除这个值(`k != 0x80000000u`),对 INT_MIN 退回 double。
- **NaN / Inf**:for 循环的 start/stop/step 如果是 NaN 或 -0,直接 abort 录制(`lj_record.c:530-534`,这些值会导致"语义不匹配或永远失败的 guard")。
- **除零**:`lj_opt_narrow_mod` 在除数为 0 时不窄化(退回浮点的 `b - floor(b/c)*c`,浮点除零是 inf/NaN,不崩溃)。

这些边界情况的处理,体现在源码里那些看似奇怪的 `if` 条件上。每一个 `if` 都是在挡住一个会让窄化改变语义的特例。

**所以,窄化是 sound 的,因为它满足 P0-01 §9 那条不变式**:

> 窄化产生的机器码结果,要么和用 double 算的完全一样(所有 guard 不触发),要么 side exit 回解释器用 double 重算(某个 guard 触发)。它永远不会比 double 算法更错。

---

## §8 完整例子:把 for 循环走一遍

把概念和源码串起来,用开头的例子完整走一遍:

```lua
local x = 0
for i = 1, 1000000 do
  x = x + i
end
```

假设单 number 模式(`LJ_DUALNUM=0`,x86 默认配置可能如此,取决于 `LUAJIT_NUMMODE`)。

**步骤 1:解释器跑,热点触发。** 循环回边的 hotcount 到阈值,JIT 启动录制。

**步骤 2:录制 FORL(for 循环回边)。** 进入 `rec_for_loop`(`lj_record.c:479`)。它调 `lj_opt_narrow_forl`(`lj_record.c:486`):

```c
TRef idx = J->base[ra+FORL_IDX];
IRType t = idx ? tref_type(idx) :
	   (init || LJ_DUALNUM) ? lj_opt_narrow_forl(J, tv) : IRT_NUM;
```

第一次录制时 `idx` 还没建立,且 `LJ_DUALNUM=0`、`init=0`,所以走 `IRT_NUM` 分支?不——`idx` 在循环 trace 里通常已存在(上一轮录的)。假设是首次,`lj_opt_narrow_forl` 检查运行时值:i=1、stop=1000000、step=1,都是整数,且 `1000000 + 1 = 1000001 ≤ 2147483647`,**返回 `IRT_INT`**。

于是 i、stop、step 都按 int 类型加载(`fori_arg` with `t = IRT_INT`)。归纳变量 i 窄化成 int。`rec_for_check` 检查溢出(这里 stop+step 远小于 int32 上限,静态检查通过,不需要额外 overflow guard)。

**步骤 3:录制循环体 `x = x + i`。** 进入 `lj_opt_narrow_arith`(`lj_record.c:2486`):

```c
rc = lj_opt_narrow_arith(J, rb, rc, rbv, rcv, IR_ADD);
```

- rb 是 x,rc 是 i。两者运行时都是整数(int tag 或能精确转 int 的 double)。
- `tref_isinteger(rb) && tref_isinteger(rc)`?这里 x 是从外面来的(可能 SLOAD 加载,double 类型),i 是归纳变量(int 类型)。**如果 x 是 double 类型,这个条件不满足,走 double 分支**。
- 但 x 的运行时值是 0(整数),`lj_num2int_ok(lj_vm_foldarith(0, i, ADD))` 能通过(0+i=i,整数)。然而 `tref_isinteger(rb)` 检查的是 **IR 类型**,不是运行时值。如果 x 在 IR 里是 IRT_NUM,这一关过不了。

这里有个细节:**普通算术的窄化,要求操作数在 IR 里已经是 int 类型**。x 如果是从循环外加载的、IR 类型是 double,那 `x + i` 不会窄化成 ADDOV,而是走 double 的 ADD。i 虽然是 int,会被转成 double 参与运算。

那这个例子岂不是窄化不了 x?**是的,在单 number 模式下,如果 x 来自循环外且没被窄化,`x + i` 走 double。** 这是 §3.1 讲的"普通算术不主动窄化"的体现。

要想让 x 也窄化,有两种情况:

1. **DUALNUM 模式**:x 加载时如果运行时是 int,加载成 int 类型,那 `tref_isinteger(x)` 成立,`x + i` 窄化成 ADDOV。
2. **x 是归纳变量本身的衍生物**:比如 `x = i * 2`,i 是 int,2 是 int 常量(DUALNUM 下或被窄化),那 `i * 2` 可能窄化。

所以这个经典例子,**在 DUALNUM 模式下能全程整数化**;在单 number 模式下,只有 i 窄化成 int,但 `x + i` 里的 x 如果是 double,加法走 double。这是单 number 模式的固有局限,也是 DUALNUM 存在的理由之一。

(注:实际中 LuaJIT 的 x86 默认编译通常 DUALNUM=1,见 `lj_arch.h:580-583`,因为 x86 属于 `LJ_NUMMODE_DUAL_SINGLE` 或 `DUAL`。所以上面这个例子在大多数 x86 LuaJIT 上,x 会被加载成 int,`x + i` 走 ADDOV,全程整数。具体取决于 `LUAJIT_NUMMODE` 编译选项。)

**步骤 4:优化 IR。** 常量折叠 pass 处理 ADDOV。如果 i 和 step 是常量,可能折叠。

**步骤 5:生成机器码。** ADDOV 在 x86 上生成:

```asm
    add  eax, ecx        ; x += i (eax=x, ecx=i)
    jo   .Lside_exit     ; 溢出则跳
```

一条 add + 一条 jo。100 万次循环,100 万次这个序列。整数单元执行,1 周期延迟,极快。

**步骤 6:运行。** 大部分时候,加法不溢出,jo 不跳,机器码全速循环。假设第 100 万次后循环正常结束——期间没有任何 side exit。

**步骤 7:万一溢出。** 假设 x 涨到接近 int32 上限(这个例子里 x 最大是 1000000*1000001/2 ≈ 5×10^11,远超 int32 2.1×10^9——**实际会溢出**)。那到某次迭代,`add eax, ecx` 设置 OF 标志,`jo .Lside_exit` 跳转,side exit。snapshot 恢复 x 和 i 的当前值(此时它们是真值,因为 add 算了一半,但 OF 置位说明结果错了——实际上 side exit 时会用 snapshot 里记录的、从机器码状态推导出的"正确"值)。退回解释器,解释器用 double 继续 `x = x + i`,给出正确的大数结果。

> **修正**:上面这个例子,如果 i 上限 1000000,求和 x ≈ 5×10^11,确实会溢出 int32。所以**实际的 LuaJIT 在录制时会发现这个问题**——`lj_opt_narrow_arith` 的 `lj_num2int_ok(lj_vm_foldarith(...))` 预演会发现某次加法结果超 int32,从而不窄化那次加法。或者更早,如果 x 的类型推断能预见到溢出,干脆整个走 double。这正印证了 §7.1 的"窄化条件本身就保证了运行时是整数"——预演不通过就不窄化。

把这个例子的各个分支想清楚,你就理解了窄化的全部边界。

---

## §9 DUALNUM 模式:让窄化更激进

前面多次提到 DUALNUM。现在单独讲清楚。

**DUALNUM(双数模式)** 是 LuaJIT 的一个编译选项(`lj_arch.h:575-586`),让 TValue( Lua 的值表示)在内部区分 int 和 double 两种 tag。也就是说,一个 number 变量,在 DUALNUM 下,运行时**真的可能是 int 类型**(`tvisint`,`lj_obj.h:805`),而不一定是 double。

这有什么影响?

- **更多窄化机会**:普通算术 `lj_opt_narrow_arith` 在 DUALNUM 下,操作数更可能是 int 类型(`tref_isinteger` 更容易成立),所以更多算术能窄化成 ADDOV。包括 MUL(DUALNUM 下 `op <= IR_MUL`,允许乘法窄化)。
- **归纳变量更自然是 int**:`narrow_forl` 里 `if (tvisint(o)) return 1` 直接命中,不需要 `lj_num2int_ok` 检查。
- **代价**:TValue 多一种 tag,解释器要处理 int/double 的转换(比如 `int + double` 要提升),实现复杂度高一点。

文件头注释(`lj_opt_narrow.c:77-89`)讲 DUALNUM 的设计动机:

> All of the above has to be reconsidered for architectures with slow FP operations or without a hardware FPU. The dual-number mode of LuaJIT addresses this issue. Arithmetic operations are performed on integers as far as possible and overflow checks are added as needed.

DUALNUM 最初是为**浮点慢或没有硬件 FPU 的架构**(比如早期 ARM)设计的——那里浮点运算太贵,值得尽量用整数。但后来在 x86 上也常用,因为现代 CPU 整数运算确实更快、循环归纳变量用 int 收益明显。

DUALNUM 下还有一个变化:**位运算参数和整数参数的窄化,会剥除 overflow 检查**(`lj_opt_narrow.c:82-89`):

> This implies that narrowing for integer arguments and bit operations should also strip overflow checks, e.g. replace ADDOV with ADD.

因为 DUALNUM 下,这些值的 overflow 语义是 wrap-around(回绕),不需要检查。`narrow_stripov` 函数(`lj_opt_narrow.c:427`)就是干这个的。

**单 number 模式 vs DUALNUM 的窄化差异总结**:

| 场景 | 单 number 模式 | DUALNUM |
|---|---|---|
| for 归纳变量 | 需要 `JIT_F_OPT_NARROW` 开关 | 默认窄化 |
| 普通算术 ADD/SUB | 仅当操作数已是 int | 更多机会(操作数常是 int) |
| 普通算术 MUL | **不窄化**(丢 -0) | 窄化 |
| 位运算参数 overflow | 不剥(保留 ADDOV) | 剥(降级 ADD) |

---

## §10 ★对照:官方 Lua、V8 与 LuaJIT

### 10.1 对照官方 Lua:有没有窄化

官方 Lua 是纯解释器。它的 number **永远是 double**,没有任何窄化。每次算术都是浮点运算。

为什么官方 Lua 不做窄化?两个原因:

1. **解释器里窄化不划算**。如 §3.1 引用的注释:解释器的取指/译码开销太大,浮点多出来的那点延迟被淹没,整数窄化在解释器层面收益不明显。
2. **窄化需要 guard + side exit 机制**,那是 JIT 的配套。解释器没有机器码、没有 side exit,做了窄化也没地方放 guard。

所以官方 Lua 老老实实用 double。简单、正确、慢。LuaJIT 的窄化是 **JIT 专属优化**——只有把代码变成机器码、能在机器码里插 guard、能 side exit 回解释器,窄化才有意义。

**这个对照说明:窄化是"JIT 加了什么"的一个具体例子。** 同样是 Lua,官方 Lua 用 double 算,LuaJIT 在热点上用 int 算。这中间的性能差,有一部分就来自窄化。

### 10.2 对照 V8:hidden class vs narrowing

V8(Chrome 的 JavaScript 引擎)也做类型推断优化,但它的重点和 LuaJIT 完全不同。

**V8 的招牌是 hidden class(隐藏类)**。JavaScript 里对象是动态的,可以随时加属性、删属性。V8 给每个对象关联一个"隐藏类"(类似 Java 的 class),记录它的属性布局(哪些属性、在哪个偏移)。相同属性布局的对象共享同一个隐藏类。这样属性访问就能编译成"按隐藏类记录的偏移直接读内存",而不是每次都哈希查找。

V8 也做数值类型推断(它会推断一个变量是 Smi——小整数,还是 HeapNumber——堆上的浮点),但**它的核心类型推断投资在对象形状上**,因为 JavaScript 的性能瓶颈在对象属性访问,不在算术。

**LuaJIT 的招牌是 narrowing(数值类型窄化)**。Lua 的对象模型简单(table + 元表),属性访问的优化空间不如 JavaScript 大;但 Lua 大量用于数值计算(游戏脚本、科学计算),算术性能很关键。所以 LuaJIT 把类型推断的精力投在**数值类型**上——把 number 拆成 int 和 double。

两者是**不同维度的类型推断**:

| 维度 | V8 hidden class | LuaJIT narrowing |
|---|---|---|
| 推断对象 | 对象的形状(属性布局) | 数值的具体类型(int/double) |
| 优化目标 | 属性访问(按偏移读内存) | 算术(用整数指令) |
| 假设失效处理 | deoptimization(去优化,重建解释器帧) | side exit + snapshot(恢复状态退回解释器) |
| 推断粒度 | per-object-shape | per-value |
| 适用场景 | JavaScript(对象密集) | Lua(数值密集) |

还有一层对照:**V8 是 method JIT**,它以整个函数为单位编译,在函数范围内做类型推断(基于类型反馈 profile),假设相对稳健,deoptimization 代价高(要重建整个函数的解释器帧)。**LuaJIT 是 trace JIT**,以一条线性路径为单位,假设更激进、更局部,side exit 代价低(只需恢复 trace 退出点的状态)。所以 LuaJIT 敢于更激进地窄化(看到一次整数就假设整数),而 V8 相对保守(要 profile 累积足够证据)。

这个对照说明:**类型推断的策略,和 JIT 的流派(trace vs method)、语言的特点(对象密集 vs 数值密集)紧密相关。** LuaJIT 的窄化,是 trace JIT + Lua 数值密集这两个条件下的最优解。

---

## §11 回扣主线 + 衔接下一章

这一章我们讲了类型窄化(narrowing)。回到全书主线:

> **把动态执行安全变成机器码。**

窄化在这条主线里扮演什么角色?它是**"乐观假设"在数值类型上的具体落地**。

P0-01 讲过,LuaJIT 的核心是"乐观假设 + 运行时检查 + 失败可回退"。窄化把这个原则用在了 number 类型上:

- **乐观假设**:观察到某个 number 实际一直是整数,就假设它一直是整数,生成整数运算的机器码(快)。
- **运行时检查**:在机器码里插 overflow guard(`ADDOV` + `jo`),万一溢出或类型变了,立刻发现(安全)。
- **失败可回退**:guard 触发就 side exit,靠 snapshot 恢复状态,退回解释器用 double 重算(正确)。

这正是主线里"快"和"安全"在数值计算上的完美平衡。没有窄化,LuaJIT 的算术性能会退回到浮点水平,几十倍的加速比会大打折扣。**窄化是 LuaJIT 动态语言跑出接近 C 整数速度的关键。**

而且,窄化还体现了一个更深的设计智慧:**类型推断不必非此即彼**。LuaJIT 没有像静态语言那样把类型定死,也没有像纯解释器那样完全放弃类型信息。它走了一条中间路线——**保持 number 的动态性(运行时仍是 double 或 int tag),但在 trace 编译时,基于运行时证据,乐观地把具体类型编进机器码,用 guard 兜底**。这既享受了静态类型的性能,又保留了动态类型的灵活性。

这种"乐观 + 兜底"的智慧,会贯穿后面所有的优化 pass。下一章,我们看 LuaJIT 最大的优化 pass——**常量折叠(fold)**。窄化产生的 `ADDOV`、`CONV` 这些 IR,会进入 fold pass,被进一步化简(`ADDOV(x, 0) → x`、`CONV(CONV(x)) → x` 等)。fold 是从"录制产生的 IR"转入"优化后的 IR"的关键一步,也是 LuaJIT 优化能力的核心。

---

*下一章 [P3-09 常量折叠 fold](P3-09-常量折叠fold.md):窄化产生的 IR 进入优化流水线。我们看 LuaJIT 最大的优化 pass 怎么把冗余的运算、转换、比较一一消掉,让 IR 更精简、机器码更快。*
