# P3-09 常量折叠 fold

> **本书主线**:把动态执行安全变成机器码。**二分法**:解释器侧 ↔ JIT 侧。本章在 **JIT 侧·优化 pass 之首**。**★对照**:官方 Lua(无优化 pass)+ JVM/V8(C2/GC 的方法级优化 pass)。**源码**:LuaJIT 2.1.ROLLING,`lj_opt_fold.c`(2655 行,LuaJIT 最大的优化 pass)。**基调**:纯直球,不用比喻;从第一性原理一步步推导。

---

## 引子:录出来的 IR,离"最优"还差得远

上一章(P2-07)讲完 IR 与 SSA 之后,一条 trace 的录制已经能产出一份合法的中间表示了。录制的逻辑是"字节码逐条翻译成 IR":看到 `ADD`,就发一条 `IR_ADD`;看到 `MUL`,就发一条 `IR_MUL`。这种翻译忠实地保留了原始语义,但它有一个特点——它是**字面翻译**。

字面翻译带来一个直接后果:**录出来的 IR,几乎从来不是最优的**。它里面有大量的、肉眼可见的冗余和可化简之处。举几个最朴素的例子:

- 某条加法里,两个操作数都是常量。`a + 1 + 2` 这条式子,如果当时 `a` 未知但 `1`、`2` 已知,那 `1 + 2` 这部分**编译期就能算出来**等于 3,根本不必等到运行时让 CPU 去做这次加法。可录制器不做这件事,它老老实实发了一条 `IR_ADD` 把 `1` 和 `2` 加起来。
- 某条乘法乘的是 1,`x * 1`。乘 1 的结果恒等于 `x`,这次乘法**完全是多余的**。可录制器不知道,它照发了一条 `IR_MUL`。
- 某条乘法乘的是 2,`x * 2`。CPU 上整数乘法要好几拍,而左移一位 `x << 1` 通常只要一拍,两者结果一模一样。可录制器发的还是 `IR_MUL`。
- 某条减法是自己减自己,`i - i`。结果必然是 0,这条减法**根本不该存在**。

这些冗余不是录制器的错——录制阶段的核心任务是"把动态语义如实变成 IR",它要处理类型窄化、guard 插入、snapshot 准备等一大堆复杂工作,在**每发一条 IR 的当下**去判断"这条能不能化简"既分心又重复。于是录制器选择把这件事**推迟**:先把忠实的 IR 发出来,化简的工作,交给后面一个专门的阶段。

这个专门负责"把 IR 化简、让机器码更快更小"的阶段,就是**优化 pass**。而其中最大、最早、几乎每发一条 IR 都要过一遍的那个 pass,就是本章的主角:**常量折叠 fold**。

fold 的源码在 `lj_opt_fold.c`,整整 2655 行——是 LuaJIT 所有优化文件里最长的一个,比负责汇编生成的 `lj_asm.c`(2643 行)还要略长一筹。一个看似简单的"化简 IR"为什么需要这么多代码?它到底在做什么?这一章,我们就从"为什么录出来的 IR 有冗余"这个第一性问题开始,一步步把 fold 的机制、它的规则引擎、它做的几大类优化,以及它为什么永远不改变程序结果(sound),全部讲透。

---

## §1 第一性原理:为什么要优化 IR

在直接进入"怎么 fold"之前,我们先把最根本的问题摆清楚:**优化这一步,到底解决什么问题?不优化行不行?**

### 1.1 不优化的代价:IR 有冗余,机器码跟着冗余

先看一个事实链条,它是优化之所以必要的根基:

**第一,IR 是机器码的直接来源。** 后端的汇编生成器(P4 篇)是逐条读 IR、逐条发机器码的。你给 IR 里留了一条多余的 `IR_MUL`(`x * 1`),后端就会老老实实生成一条乘法指令;你给 IR 留一条 `x * 2`,后端就生成一条乘法指令而不是更快的左移指令。IR 有多少冗余,最终的机器码就有多少冗余,**二者一一对应**。

**第二,冗余指令是有实际代价的。** 一条多余的乘法指令,占用一个指令缓存槽,消耗几个时钟周期,还可能多占一个寄存器。在跑一百万次的循环里,这一条多余指令就是一百万次无谓的开销。把"算出来的结果恒等于输入"或"用更便宜的指令能算出相同结果"的冗余消掉,直接换来更小、更快的机器码。

**第三,冗余是普遍的,不是个例。** 程序里到处是 `i + 0`、`x * 1`、`1 + 2`、`i - i`、`(a + b) - a` 这种结构。这不是程序员写得多烂,而是**程序的真实形态**:循环里下标从 0 开始累加、偏移量是编译期已知的常量、连续两次同类型的运算可以合并……录制器忠实地把这些都翻成 IR,就必然带出大量可化简的模式。

把这三点合起来:**因为 IR 直接决定机器码,而冗余 IR 又普遍存在,所以在 IR 上做化简,是提升机器码质量最低成本、最高杠杆的一步。** 改一处 IR,省掉循环里上百万次多余运算——这就是优化 pass 存在的根本理由。

### 1.2 为什么在 IR 上优化,而不是在别的层

那么,优化该在哪一层做?有三个候选:Lua 源码层、字节码层、IR 层。

**Lua 源码层不行。** LuaJIT 不参与解析,它从字节码开始工作。而且源码层做优化要面对语法树的复杂结构,远没有 IR 干净。

**字节码层也不合适。** 字节码是解释器直接执行的,它的格式是给解释器 dispatch 用的,不是给优化用的。更重要的是,**字节码层不知道运行时的类型信息**。而 JIT 的全部优势,恰恰来自运行时观察到的真实类型(比如"这个循环里 x 一直是整数")。这些信息在录制时才进入 IR(成为 IR 的类型标记和 guard),脱离了 IR 谈优化,等于丢了 JIT 最核心的那张牌。

**IR 层最合适。** IR 是 SSA 形式(上一章讲的,每个引用单赋值、依赖关系清晰),带有完整的类型信息,结构规整(每条指令就是 `op + op1 + op2 + 类型`),而且它正好位于"录制完成"和"生成机器码"之间——是做化简的天然位置。所有现代编译器(LLVM 的 IR、JVM 的 Sea-of-Nodes、V8 的 TurboFan IR)都选择在 IR 上做优化,不是巧合,而是 IR 这一层在表达力和可分析性上恰到好处。

### 1.3 fold 的定位:优化 pass 之首,而且是"在线"的

LuaJIT 的优化不止 fold 一个。后面几章会讲到内存优化(`lj_opt_mem`,做别名分析和加载转发)、分配消除(`lj_opt_sink`)、死代码消除(`lj_opt_dce`)、循环优化和指令拆分(`lj_opt_loop`/`lj_opt_split`)。但 fold 是其中**最早执行、也是最大**的一个,而且它有一个独特的性质:**它是"在线"(on-the-fly)的**。

什么叫在线?意思是 fold **不是一个独立的、录制完之后才跑一遍的 pass**,而是**被嵌进每一条 IR 的发射路径里**的。录制器每要发一条 IR,它调用的不是直接 `lj_ir_emit`,而是一个叫 `emitir` 的宏。这个宏在 `lj_opt_fold.c:145` 定义:

```c
/* Pass IR on to next optimization in chain (FOLD). */
#define emitir(ot, a, b)	(lj_ir_set(J, (ot), (a), (b)), lj_opt_fold(J))
```

读懂这一行,就抓住了 fold 的运行模型。`emitir(ot, a, b)` 做两件事:

1. `lj_ir_set(J, ot, a, b)`——把这条待发的指令的字段(opcode+type `ot`、操作数 `a`、操作数 `b`)写进 `J->fold.ins`(当前正在处理的指令)。注意,**它只是写到 fold 状态里,还没有真正写进 trace 的 IR 数组**。
2. `lj_opt_fold(J)`——把它交给 fold 引擎。fold 引擎来决定这条指令的最终命运:被化简掉?被改成别的东西?还是原样写进 IR?

也就是说,**录制器发出的每一条 IR,都先过 fold 一遍**。fold 能当场化简的(比如 `x * 1`),就在这一刻化简掉,录制器甚至感知不到发生了化简;化简不了的,fold 才让它落进 IR 数组。

这个"在线化简"的设计有一个巨大的好处:**化简产生的连锁效应能立刻被处理**。比如 `x * 2` 被 fold 成 `x + x`,这个新的 `x + x` 会**立刻再走一遍 fold**(也许又能触发别的规则),直到达到一个不动点(fixed point)。如果是"录完再优化",这种连锁要等到整个 pass 跑完才能发现;而在线 fold 在发每一条指令时就把它彻底化简干净了。

理解了这一点,我们就理解了 fold 为什么是"pass 之首"——它不是被动地等 IR 都录好,而是**主动地站在每一条 IR 的必经之路上**,边录边化简。这种"在线、即时、到不动点"的工作方式,是 fold 设计的灵魂。

---

## §2 什么是常量折叠:编译期就算出答案

现在我们进入 fold 的第一大类优化,也是最经典的一类:**常量折叠(constant folding)**。这个名字的字面意思就是"把常量折叠掉"——更准确地说,是**当一条运算的所有操作数都是编译期已知的常量时,直接在编译期把这次运算算出来,用算出的常量结果替换掉整条运算指令**。

### 2.1 最简单的例子:1 + 2 → 3

回到引子里的例子。考虑这段 Lua:

```lua
local a = something()      -- a 运行时才知道
local y = a + 1 + 2        -- 但 1 和 2 是写死的常量
```

录制时,`a` 是一个 IR 引用(运行时值),`1` 和 `2` 是常量指令(`IR_KINT`)。录制器会忠实地发出两条加法:

```text
%3 = ADD %1, KINT(1)      ; a + 1
%4 = ADD %3, KINT(2)      ; (a + 1) + 2
```

第一条 `ADD` 的操作数有一个是变量(`a`),化简不了。但第二条 `ADD` 不同——它的两个操作数是 `%3`(上一步结果)和常量 `2`。这看起来也不能化简,因为 `%3` 不是常量。

但等等,现在让我们把表达式重写一下:`a + 1 + 2` 等价于 `a + (1 + 2)`,而 `1 + 2` 是两个常量相加,编译期就能算出 `3`。所以理想情况下,我们应该得到:

```text
%4 = ADD %1, KINT(3)      ; a + 3,只需一次加法
```

常量折叠要做的,就是识别出"两个常量参与的运算"并把它算掉。但上面的例子暴露出一个细节:**纯粹按"两个操作数都是常量"来匹配,只能抓住 `1 + 2` 这种紧挨着的常量;要抓住 `a + 1 + 2` 里的 `1 + 2`,还需要配合另一类优化(重新结合 reassociation,后面 §6 会讲)**。这一节我们先聚焦最纯粹的常量折叠:**两个操作数都是常量时,编译期算出结果**。

### 2.2 常量折叠为什么是 sound 的

在讲它怎么实现之前,必须先回答一个贯穿全章的问题:**化简凭什么不改变程序结果?**

这个问题对常量折叠尤其重要,因为它最直白。答案也很直白:**因为在所有可能的运行时取值下,常量运算的结果都和编译期算出来的一样**。

以 `1 + 2` 为例。`1` 这个 `IR_KINT` 指令,它的值在编译期就被固定写死(就是 1),运行时绝不会变成别的;`2` 同理。两个写死的值做整数加法,结果是 `3`——这是整数加法这条数学规律保证的,对任何 CPU、任何运行环境都成立。所以用常量指令 `KINT(3)` 替换掉 `ADD(KINT 1, KINT 2)`,**在任何一次实际运行中,这个替换产生的值都和原来分毫不差**。这就是常量折叠的 soundness 所在。

推广到一般情形:任何一条 IR 运算,如果它的所有操作数都是编译期常量(`IR_KINT`/`IR_KNUM`/`IR_KINT64`/`IR_KGC` 等),那么这条运算的结果就是一个确定的常量值——因为这条 IR 运算的语义是固定的(加法就是加法、按位与就是按位与),输入固定、语义固定,输出就唯一固定。编译期算出这个唯一固定的输出,替换原指令,结果必然一致。

注意一个细节:**常量折叠只对"结果是纯函数的运算"成立**。所谓纯函数,就是"同样的输入永远产生同样的输出,且不产生副作用"。算术、位运算、比较都是纯函数;但像"从内存读一个值"(`IR_ALOAD`)就不是——它的结果取决于内存里当下是什么,编译期不知道,所以不能简单折叠(内存读的化简靠的是别名分析,见下一章)。fold 里凡是能常量折叠的指令,都是纯运算。

### 2.3 常量折叠在 IR 里的实现:一条规则配一个算函数

LuaJIT 实现常量折叠的方式非常直接:**对每一种"全是常量的运算",写一条专门的规则,规则体里直接把结果算出来,返回一个新的常量引用**。

拿整数加法 `ADD KINT KINT` 举例。源码在 `lj_opt_fold.c:286-304`:

```c
LJFOLD(ADD KINT KINT)
LJFOLD(SUB KINT KINT)
LJFOLD(MUL KINT KINT)
LJFOLD(MOD KINT KINT)
LJFOLD(NEG KINT KINT)
LJFOLD(BAND KINT KINT)
LJFOLD(BOR KINT KINT)
LJFOLD(BXOR KINT KINT)
LJFOLD(BSHL KINT KINT)
LJFOLD(BSHR KINT KINT)
LJFOLD(BSAR KINT KINT)
LJFOLD(BROL KINT KINT)
LJFOLD(BROR KINT KINT)
LJFOLD(MIN KINT KINT)
LJFOLD(MAX KINT KINT)
LJFOLDF(kfold_intarith)
{
  return INTFOLD(kfold_intop(fleft->i, fright->i, (IROp)fins->o));
}
```

这段代码看起来陌生,但概念极简。`LJFOLD(ADD KINT KINT)` 这一行是在向 fold 引擎**声明一条规则**:当指令是 `ADD`、左操作数是 `KINT`(整数常量)、右操作数也是 `KINT` 时,匹配这条规则。连续多个 `LJFOLD` 叠在一起,表示它们**共用同一个处理函数**——这里 `ADD/SUB/MUL/MOD/NEG/BAND/BOR/BXOR/BSHL/...` 这一大串整数运算,全都交给同一个 `kfold_intarith` 函数处理。

`LJFOLDF(kfold_intarith)` 定义这个函数本身,它的函数体只有一行:

```c
return INTFOLD(kfold_intop(fleft->i, fright->i, (IROp)fins->o));
```

逐字拆开:

- `fleft` 是左操作数指令(已经由 fold 引擎从 IR 里取出来,缓存在 `J->fold.left`)。`fleft->i` 是这条 `KINT` 指令携带的整数值(回忆上一章:`IRIns` 是个 union,`i` 字段重叠存着 32 位整数常量)。
- `fright->i` 同理,是右操作数的整数值。
- `fins->o` 是当前指令的 opcode(`IR_ADD` 之类)。`kfold_intop` 是个 switch,根据 opcode 选对应的运算,把两个整数算出一个结果(下面马上看它)。
- `INTFOLD(...)` 是个宏(`lj_iropt.h:115`),它的作用是"把这个整数结果登记成一个 `KINT` 常量,返回它的引用"。它的定义是 `((J->fold.ins.i = (k)), (TRef)KINTFOLD)`——把结果写进 `fins->i`,然后返回特殊值 `KINTFOLD`;fold 引擎看到 `KINTFOLD` 这个返回值,就知道去 `lj_ir_kint` 申请一个常量槽存这个值。

再看 `kfold_intop` 这个真正做算术的函数,`lj_opt_fold.c:263-284`:

```c
static int32_t kfold_intop(int32_t k1, int32_t k2, IROp op)
{
  switch (op) {
  case IR_ADD: k1 += k2; break;
  case IR_SUB: k1 -= k2; break;
  case IR_MUL: k1 *= k2; break;
  case IR_MOD: k1 = lj_vm_modi(k1, k2); break;
  case IR_NEG: k1 = (int32_t)(~(uint32_t)k1+1u); break;
  case IR_BAND: k1 &= k2; break;
  case IR_BOR: k1 |= k2; break;
  case IR_BXOR: k1 ^= k2; break;
  case IR_BSHL: k1 <<= (k2 & 31); break;
  case IR_BSHR: k1 = (int32_t)((uint32_t)k1 >> (k2 & 31)); break;
  case IR_BSAR: k1 >>= (k2 & 31); break;
  case IR_BROL: k1 = (int32_t)lj_rol((uint32_t)k1, (k2 & 31)); break;
  case IR_BROR: k1 = (int32_t)lj_ror((uint32_t)k1, (k2 & 31)); break;
  case IR_MIN: k1 = k1 < k2 ? k1 : k2; break;
  case IR_MAX: k1 = k1 > k2 ? k1 : k2; break;
  default: lj_assertX(0, "bad IR op %d", op); break;
  }
  return k1;
}
```

这就是一个朴素的、一个 case 对一种运算的 switch。它和 CPU 实际执行的运算在语义上完全一致——而且正因为一致,折叠才是 sound 的。注意几个细节,它们体现了实现的精确性:

- 移位运算的位移量都做了 `& 31`(32 位整数移位掩码到 5 位),这和 x86 的 `SHL/SHR/SAR` 指令行为一致。
- `NEG`(取负)实现成 `~k1 + 1`,这是补码取负的标准位运算写法,避免了 `-k1` 在 `INT_MIN` 时的未定义行为。
- `MOD`(取模)调了 `lj_vm_modi`,因为 Lua 的取模语义(结果符号跟除数走)和 C 的 `%`(符号跟被除数走)不同,不能用 C 的 `%` 直接折。

这些细节说明一件事:**常量折叠算出来的结果,必须和运行时 IR 执行出来的结果逐位相同**。任何一处语义偏差(比如用 C 的 `%` 代替 Lua 的 mod),都会让 fold 改变程序结果,是不可接受的。所以这里要专门调 Lua 语义的 mod 实现。

浮点数的常量折叠走的是另一条路(`lj_opt_fold.c:172-185`),因为浮点运算不能简单地用 C 运算符——它要保证和运行时 IR 的浮点指令逐位一致(包括 NaN、正负零、舍入模式),所以交给一个专门的函数 `lj_vm_foldarith` 去算:

```c
LJFOLD(ADD KNUM KNUM)
LJFOLD(SUB KNUM KNUM)
LJFOLD(MUL KNUM KNUM)
LJFOLD(DIV KNUM KNUM)
LJFOLD(LDEXP KNUM KNUM)
LJFOLD(MIN KNUM KNUM)
LJFOLD(MAX KNUM KNUM)
LJFOLDF(kfold_numarith)
{
  lua_Number a = knumleft;
  lua_Number b = knumright;
  lua_Number y = lj_vm_foldarith(a, b, fins->o - IR_ADD);
  return lj_ir_knum(J, y);
}
```

`lj_vm_foldarith` 是手写汇编实现的(在 `lj_vm.s` 里),它精确复现运行时浮点指令的行为。这种"编译期计算必须和运行时计算逐位一致"的要求,是 fold 所有规则共有的纪律——下一节讲规则引擎时会反复看到。

---

## §3 fold 的引擎:一张规则表驱动的模式匹配

常量折叠只是 fold 做的事情之一。fold 真正的核心,是一个**基于规则表的、迭代到不动点的模式匹配引擎**。这个引擎的设计极其精巧,值得单独、完整地讲一遍——因为理解了它,后面所有具体的优化规则(代数化简、强度削减、重新结合)就都只是"往这张表里添一行规则"的事了。

### 3.1 关键事实:规则表不是单独的 .def 文件

先把一个容易弄错的事实澄清。很多人(包括讲 LuaJIT 的资料)会提到一个叫 `lj_fold.def` 的文件,说"LuaJIT 的折叠规则表定义在这个文件里"。**在 LuaJIT 2.1.ROLLING 里,这个文件不存在**。

事实是:所有的折叠规则,**直接以 `LJFOLD(...)` 宏的形式内嵌在 `lj_opt_fold.c` 源文件里**。我们在 §2.3 已经看到了 `LJFOLD(ADD KINT KINT)` 这样的行——它们就是规则定义本身。`LJFOLD` 这个宏在编译期被展开成空(`lj_opt_fold.c:151`):

```c
#define LJFOLD(x)
#define LJFOLDX(x)
#define LJFOLDF(name)	static TRef LJ_FASTCALL fold_##name(jit_State *J)
```

也就是说,对 C 编译器而言,`LJFOLD(ADD KINT KINT)` 这一行展开成什么都没有,纯粹是个注释般的空语句;`LJFOLDF(kfold_intarith)` 展开成一个普通的静态函数声明 `static TRef LJ_FASTCALL fold_kfold_intarith(jit_State *J)`。

那这些 `LJFOLD` 规则**是怎么变成可查询的规则表**的?答案是 LuaJIT 的构建工具 **buildvm**。构建时,buildvm 会**以文本方式扫描 `lj_opt_fold.c`**(逐行读,匹配行首的 `LJFOLD` 前缀),把每一条 `LJFOLD(op left right)` 解析成一个 24 位的键,把紧跟其后的 `LJFOLDF(name)` 或 `LJFOLDX(name)` 解析成对应的处理函数,然后生成一个叫 `lj_folddef.h` 的头文件,里面包含两张表:

- `fold_func[]`:一个函数指针数组,每个元素是一条规则对应的处理函数(`fold_kfold_intarith` 等)。
- `fold_hash[]`:一张**半完美哈希表(semi-perfect hash table)**,把 24 位键映射到 `fold_func[]` 的下标,外加一段 `#define fold_hashkey(k) ...` 宏定义哈希函数本身。

`lj_opt_fold.c:2521` 一行 `#include "lj_folddef.h"` 把这两张表引进来。运行时 fold 引擎靠这两张表查规则。

> **2.1 ROLLING 差异标注**:有些老资料(讲 2.0 时代的)描述 LuaJIT 有独立的 `lj_fold.def`。在 2.1.ROLLING 源码树里没有这个文件——规则内嵌在 `lj_opt_fold.c` 里,由 `src/host/buildvm_fold.c`(`emit_fold` 函数,`buildvm_fold.c:169`)在构建时扫描生成 `lj_folddef.h`。这是同一个机制的不同表述:机制没变(规则→半完美哈希表),只是规则的物理位置在源文件里内联,而不是单独成文件。

### 3.2 24 位键:怎么把一条规则编码成一个整数

理解了规则表怎么来的,接下来看**一条规则是怎么编码成一个可以查的键**的。这是 fold 引擎最巧妙的设计之一。

回忆 §2.3:`LJFOLD(ADD KINT KINT)` 声明了一条规则,它有三个部分——指令 opcode(`ADD`)、左操作数的 opcode(`KINT`)、右操作数的 opcode(`KINT`)。buildvm 把这三个部分拼成一个 24 位整数,编码方式在 `lj_opt_fold.c:2511-2519` 的注释里写得很清楚:

```text
xxxxxxxx iiiiiii lllllll rrrrrrrrrr

  xxxxxxxx = 8 bit index into fold function table
   iiiiiii = 7 bit folded instruction opcode
   lllllll = 7 bit left instruction opcode
rrrrrrrrrr = 8 bit right instruction opcode or 10 bits from literal field
```

具体到 `buildvm_fold.c:143-148` 的 `foldrule` 函数,组装逻辑是:

```c
uint32_t op = nexttoken(&p, 0, 0);          /* 指令 opcode, 7 bit */
uint32_t left = nexttoken(&p, 0, 0x7f);     /* 左操作数 opcode 或 any 标记 */
uint32_t right = nexttoken(&p, 1, 0x3ff);   /* 右操作数 opcode 或字面量或 any */
uint32_t key = (funcidx << 24) | (op << 17) | (left << 10) | right;
```

拼出来的键的低位 24 位是 `op << 17 | left << 10 | right`,高 8 位是这条规则对应的处理函数在 `fold_func[]` 里的下标(运行时查表用)。

这个编码有几个值得注意的设计点:

**第一,为什么左操作数只有 7 位、右操作数最多 10 位?** 因为左操作数的位置只放指令 opcode(最多 7 位够表示所有 IR 指令);而右操作数的位置除了放指令 opcode,**还可以放字面量**——比如 `LJFOLD(CONV KINT IRCONV_NUM_INT)` 这条规则,右边的 `IRCONV_NUM_INT` 不是一个指令而是一个字面量(转换模式),buildvm 把它编码成 10 位存进右操作数槽(`buildvm_fold.c:147` 的 `allowany=0x3ff` 和字面量解析逻辑)。

**第二,有一个特殊的记号 `any`,表示"通配符"**。`LJFOLD(MUL any KNUM)` 表示"不管左操作数是什么指令,只要右操作数是 `KNUM` 就匹配"。`any` 在左操作数槽里编码成 `0x7f`、在右操作数槽里编码成 `0x3ff`——这俩值是各自槽的全 1,正好用来表示"不关心这一位"。

这个 24 位键的设计,让"一条规则"变成了"一个整数",于是规则匹配就变成了**整数比较**——极快。

### 3.3 半完美哈希表:怎么用一张小表查规则

规则变成整数了,但还有个问题:fold 有几百条规则(粗略一数 `LJFOLD` 行有近三百条),总不能每收到一条指令就去遍历这三百条规则找匹配——那太慢了。

LuaJIT 的解法是**半完美哈希表(semi-perfect hash table)**。这里的"半完美"是个术语:完美哈希(perfect hash)是每个键恰好落到一个唯一的槽、无冲突;**半完美**则放宽一点,**允许冲突,但每个槽最多放两个键**(主槽 + 备槽)。

这张表是怎么构造的?在 `buildvm_fold.c:62-91` 的 `makehash` 函数里,buildvm 用**穷举搜索**找一个能让所有规则无冲突(或冲突能用备槽化解)的哈希函数:

```c
/* Exhaustive search for the shortest semi-perfect hash table. */
static void makehash(BuildCtx *ctx)
{
  uint32_t htab[BUILD_MAX_FOLD*2+1];
  uint32_t sz, r;
  /* Search for the smallest hash table with an odd size. */
  for (sz = (nkeys|1); sz < BUILD_MAX_FOLD*2; sz += 2) {
    /* First try all shift hash combinations. */
    for (r = 0; r < 32*32; r++) {
      if (tryhash(htab, sz, r, 0)) {
        printhash(ctx, htab, sz);
        fprintf(ctx->fp,
                "#define fold_hashkey(k)\t(((((k)<<%u)-(k))<<%u)%%%u)\n\n",
                r>>5, r&31, sz);
        return;
      }
    }
    /* Then try all rotate hash combinations. */
    for (r = 0; r < 32*32; r++) {
      if (tryhash(htab, sz, r, 1)) {
        ...
      }
    }
  }
  ...
}
```

这段代码做的事情,翻译成白话就是:**"找一个尽量小的奇数大小的表,再找一个哈希函数(形如 `((k<<a)-k)<<b` 或用循环移位的变体),使得把所有规则键哈希进表后,任何冲突都能用主槽+备槽两个位置化解掉。"** 找到了就把这个哈希函数和填好的表打印进 `lj_folddef.h`。

`tryhash`(`buildvm_fold.c:18-48`)是具体的尝试函数:它把每个键哈希进主槽;主槽被占就进备槽;主槽备槽都被占,就尝试把原来在备槽的键挪到下一个槽(如果它的哈希值允许)。如果挪不动,这次尝试失败,换参数重来。这是一个构造半完美哈希的经典算法(类似 Cichelli 算法的思路)。

这个构造是**离线**做的(构建 buildvm 时一次),所以可以花时间穷搜最优解;运行时只需要一次哈希计算 + 最多两次比较(查主槽和备槽),极快。这正是 fold 能做到"在线、每条 IR 都过一遍"而不拖慢录制的根本原因——查规则几乎是常数时间。

### 3.4 引擎主体:从最具体到最一般的逐步放宽匹配

有了规则表和哈希函数,fold 引擎的主体在 `lj_opt_fold.c:2526-2598`。这段代码不长,但它是整个 fold 的心脏,我们逐段读。

先看入口和"开关检查":

```c
/* Fold IR instruction. */
TRef LJ_FASTCALL lj_opt_fold(jit_State *J)
{
  uint32_t key, any;
  IRRef ref;

  if (LJ_UNLIKELY((J->flags & JIT_F_OPT_MASK) != JIT_F_OPT_DEFAULT)) {
    ...
    /* Folding disabled? Chain to CSE, but not for loads/stores/allocs. */
    if (!(J->flags & JIT_F_OPT_FOLD) && irm_kind(lj_ir_mode[fins->o]) == IRM_N)
      return lj_opt_cse(J);
    ...
  }
```

这段是给"用户关掉了某些优化"的情况兜底的。LuaJIT 的优化可以按 flag 单独开关(`JIT_F_OPT_FOLD`/`JIT_F_OPT_CSE`/`JIT_F_OPT_FWD`/`JIT_F_OPT_DSE` 等,定义在 `lj_jit.h:80-90`),默认全开(`JIT_F_OPT_DEFAULT = JIT_F_OPT_3`,`lj_jit.h:102`)。如果用户关了 fold,那对纯计算指令(`IRM_N` kind)就直接跳到 CSE;对加载指令(`IRM_L`)直接发射;对存储指令(`IRM_S`)直接发射。这是性能兜底,不是常态路径。

关键的匹配逻辑在下面。先构造键:

```c
  /* Fold engine start/retry point. */
retry:
  /* Construct key from opcode and operand opcodes (unless literal/none). */
  key = ((uint32_t)fins->o << 17);
  if (fins->op1 >= J->cur.nk) {
    key += (uint32_t)IR(fins->op1)->o << 10;
    *fleft = *IR(fins->op1);
    if (fins->op1 < REF_TRUE)
      fleft[1] = IR(fins->op1)[1];
  }
  if (fins->op2 >= J->cur.nk) {
    key += (uint32_t)IR(fins->op2)->o;
    *fright = *IR(fins->op2);
    if (fins->op2 < REF_TRUE)
      fright[1] = IR(fins->op2)[1];
  } else {
    key += (fins->op2 & 0x3ffu);  /* Literal mask. Must include IRCONV_*MASK. */
  }
```

这段把当前指令 `fins` 编码成一个 24 位的运行时键,逻辑和 buildvm 编码规则键完全对称:

- 指令自己的 opcode 放到 `<< 17` 位。
- 左操作数:如果它是个 IR 引用(`>= J->cur.nk`,即落在指令区而不是常量区),就把它的 opcode 放到 `<< 10` 位,**同时把这条左操作数指令整个拷贝到 `fleft`**(供规则函数直接读字段,不用再解引用);如果左操作数是字面量/空,这一段就是 0。
- 右操作数同理,放到最低位;如果是字面量(`< J->cur.nk`,比如 `IRCONV_NUM_INT` 这个转换模式),就把字面量值的低 10 位直接放进键里(注释强调"Must include IRCONV_*MASK")。

注意一个细节:`fleft[1] = IR(fins->op1)[1]`。这拷贝的是左操作数指令的**第二个 IR 槽**——因为有些指令(比如 `KNUM`/`KINT64`)是双槽指令,真正的常量值存在第二个槽里。规则函数要用到这个值时(比如 `knumleft` 宏读 `ir_knum(fleft)->n`),必须把这个槽也一起拷过来。

键构造好之后,是最关键的**逐步放宽匹配**循环:

```c
  /* Check for a match in order from most specific to least specific. */
  any = 0;
  for (;;) {
    uint32_t k = key | (any & 0x1ffff);
    uint32_t h = fold_hashkey(k);
    uint32_t fh = fold_hash[h];  /* Lookup key in semi-perfect hash table. */
    if ((fh & 0xffffff) == k || (fh = fold_hash[h+1], (fh & 0xffffff) == k)) {
      ref = (IRRef)tref_ref(fold_func[fh >> 24](J));
      if (ref != NEXTFOLD)
        break;
    }
    if (any == 0xfffff)  /* Exhausted folding. Pass on to CSE. */
      return lj_opt_cse(J);
    any = (any | (any >> 10)) ^ 0xffc00;
  }
```

这段是 fold 引擎的灵魂,值得一句一句读。

`any` 是一个"通配掩码",初始为 0,表示"用最精确的键去查"。循环每一轮,都把 `any` 的某些位置 1,把这些位和运行时键 `key` 做**或**运算——等价于"把这些位的值抹掉,改成不关心"。

第一轮 `any = 0`,`k = key`,即 `ins left right` 三段全精确。如果命中的规则函数返回了非 `NEXTFOLD` 的值(意味着化简成功),`break` 跳出;如果返回 `NEXTFOLD`(这条规则虽然在表里,但函数内部检查后发现当前情况不适用,比如 `LJFOLD(SUB any KNUM)` 命中了但右边不是 +0),就继续放宽。

`any` 的演化 `any = (any | (any >> 10)) ^ 0xffc00` 这一行,是在按一个固定的顺序把键的左、右两段逐步抹成 `any`。这个顺序在文件头的注释(`lj_opt_fold.c:44-50`)里写得明明白白:

```text
ins left right
ins any  right
ins left any
ins any  any
```

也就是:**先用最精确的 `ins left right` 查;查不到(或查到但规则说 NEXTFOLD)就把左操作数放宽成 any,查 `ins any right`;再不行把右放宽成 any,查 `ins left any`;最后两边都放宽,查 `ins any any`。**

这个"从最具体到最一般"的顺序非常关键。考虑一条 `ADD x KINT(0)` 指令(左是变量 `x`,右是常量 0)。最精确的键 `ADD <x的op> KINT` 在表里大概率没有(因为 `x` 的具体 opcode 千变万化,不可能为每种都写规则)。于是放宽到 `ADD any KINT`,这一查就命中了规则 `LJFOLD(ADD any KINT)`(§5.1 会看到),它的函数 `simplify_intadd_k` 检查右边是不是 0,是 0 就返回 `LEFTFOLD`(化简成左操作数,即 `x + 0 → x`)。

**为什么必须从最具体开始放宽?** 因为规则的优先级。比如同时存在 `LJFOLD(SUB any KINT)`(通用的"整数减常量"规则,会做 `i - k → i + (-k)` 的规范化)和更具体的 `LJFOLD(SUB KINT any)`(左边是常量的特殊情形,`0 - i → -i`)。如果上来就查最宽的 `SUB any any`,可能命中错误的规则。从最具体开始查,保证更专门的规则优先。

如果四轮都查不到(`any` 演化到 `0xfffff`,注释说"Exhausted folding"),那这条指令确实没有任何折叠规则可用了,fold 把它**交给 CSE**(公共子表达式消除,§7 讲),由 CSE 决定是复用已有的等价指令,还是老老实实发射。

最后是返回值处理:

```c
  /* Return value processing, ordered by frequency. */
  if (LJ_LIKELY(ref >= MAX_FOLD))
    return TREF(ref, irt_t(IR(ref)->t));
  if (ref == RETRYFOLD)
    goto retry;
  if (ref == KINTFOLD)
    return lj_ir_kint(J, fins->i);
  if (ref == FAILFOLD)
    lj_trace_err(J, LJ_TRERR_GFAIL);
  lj_assertJ(ref == DROPFOLD, "bad fold result");
  return REF_DROP;
}
```

规则函数能返回的值有几种(`lj_iropt.h:106-121`):

- 一个**正常的 IR 引用**(>= `MAX_FOLD`):最常见,比如 `LEFTFOLD` 返回的就是左操作数的引用。直接包装成 `TRef` 返回给调用者。
- `RETRYFOLD`:规则修改了 `fins` 的字段(比如把 `MUL` 改成了 `ADD`),要求"假装这条指令是刚收到的,从头再 fold 一遍"。`goto retry` 跳回键构造处,这就是"迭代到不动点"的实现——一条指令可能被连续化简好几次,每次 `RETRYFOLD` 都让它再走一遍引擎。
- `KINTFOLD`:常量折叠的结果(§2.3 见过),去申请一个 `KINT` 常量槽。
- `FAILFOLD`:某个 guard 假设恒失败,意味着这条 trace 没意义(比如编译期就知道某个类型检查必然失败),报错中止录制。
- `DROPFOLD`:某个 guard 恒成立(比如 `UGT(asize, k)` 里 `k` 是 0,无符号比较恒成立),直接丢弃这条指令。

把整个引擎串起来,它的工作流是:**收到一条指令 → 编码成键 → 按从具体到一般的顺序查半完美哈希表 → 命中规则就执行规则函数 → 看返回值(成功就返回、要重试就 goto retry、查完所有规则没命中就交 CSE)**。这是一个极其紧凑、高效的模式匹配循环,正是它支撑了 fold 的"在线、到不动点"工作方式。

---

## §4 代数化简:靠恒等式消掉多余运算

讲完了引擎,我们看 fold 做的几大类具体优化。本节讲第二大类:**代数化简(algebraic simplification)**。它的精神是:利用代数恒等式,把一条运算替换成更简单的形式,甚至整个消掉。

### 4.1 加零、乘一:恒等元化简

最朴素的代数化简,是利用运算的**恒等元(identity element)**:加法的恒等元是 0(`x + 0 = x`),乘法的恒等元是 1(`x * 1 = x`),按位与的恒等元是...不是 0(0 是吸收元),按位与的恒等元是全 1(`x & -1 = x`)。

LuaJIT 对这些的处理在 `lj_opt_fold.c:1345-1353`:

```c
LJFOLD(ADD any KINT)
LJFOLD(ADDOV any KINT)
LJFOLD(SUBOV any KINT)
LJFOLDF(simplify_intadd_k)
{
  if (fright->i == 0)  /* i o 0 ==> i */
    return LEFTFOLD;
  return NEXTFOLD;
}
```

`ADD any KINT` 匹配"整数加一个常量"。函数体只检查一件事:右边的常量是不是 0。如果是 0,返回 `LEFTFOLD`——`LEFTFOLD` 的定义是 `(J->fold.ins.op1)`(`lj_iropt.h:118`),即"化简成左操作数"。`x + 0` 整条 `ADD` 指令被丢弃,直接用 `x` 替换,机器码里就少了一条加法。

乘一的规则类似,在 `lj_opt_fold.c:1355-1368`:

```c
LJFOLD(MULOV any KINT)
LJFOLDF(simplify_intmul_k)
{
  if (fright->i == 0)  /* i * 0 ==> 0 */
    return RIGHTFOLD;
  if (fright->i == 1)  /* i * 1 ==> i */
    return LEFTFOLD;
  if (fright->i == 2) {  /* i * 2 ==> i + i */
    fins->o = IR_ADDOV;
    fins->op2 = fins->op1;
    return RETRYFOLD;
  }
  return NEXTFOLD;
}
```

注意这条规则同时处理了三种情况:`i * 0 → 0`(返回 `RIGHTFOLD`,化简成右操作数即常量 0)、`i * 1 → i`(`LEFTFOLD`)、`i * 2 → i + i`(把 `MULOV` 改成 `ADDOV`、右操作数改成左操作数,然后 `RETRYFOLD`)。最后这个 `i * 2 → i + i` 是强度削减的萌芽(乘法变加法),我们下一节细讲。

按位运算的恒等元化简在 `lj_opt_fold.c:1566-1606`:

```c
LJFOLD(BAND any KINT)
LJFOLD(BAND any KINT64)
LJFOLDF(simplify_band_k)
{
  int64_t k = ...;
  if (k == 0)  /* i & 0 ==> 0 */
    return RIGHTFOLD;
  if (k == -1)  /* i & -1 ==> i */
    return LEFTFOLD;
  return NEXTFOLD;
}

LJFOLD(BOR any KINT)
LJFOLD(BOR any KINT64)
LJFOLDF(simplify_bor_k)
{
  int64_t k = ...;
  if (k == 0)  /* i | 0 ==> i */
    return LEFTFOLD;
  if (k == -1)  /* i | -1 ==> -1 */
    return RIGHTFOLD;
  return NEXTFOLD;
}

LJFOLD(BXOR any KINT)
LJFOLD(BXOR any KINT64)
LJFOLDF(simplify_bxor_k)
{
  int64_t k = ...;
  if (k == 0)  /* i xor 0 ==> i */
    return LEFTFOLD;
  if (k == -1) {  /* i xor -1 ==> ~i */
    fins->o = IR_BNOT;
    fins->op2 = 0;
    return RETRYFOLD;
  }
  return NEXTFOLD;
}
```

这里能看出恒等元和吸收元的区别:`&` 的吸收元是 0(`x & 0 = 0`)、恒等元是 -1(`x & -1 = x`);`|` 恰好反过来,吸收元是 -1、恒等元是 0。`xor` 没有传统意义的吸收元,但 `xor -1` 等于取反(`~x`),所以化简成 `BNOT`。每一条都是基于一个具体的位运算恒等式,而每个恒等式都是数学上证明过的、对所有整数取值都成立的——这就是 sound 的来源。

### 4.2 自己减自己、自己异或自己:结果必然已知

还有一类代数化简,针对"操作数相同"的特殊情形,结果是个已知的常量:

```c
LJFOLD(SUB any any)
LJFOLD(SUBOV any any)
LJFOLDF(simplify_intsub)
{
  if (fins->op1 == fins->op2 && !irt_isnum(fins->t))  /* i - i ==> 0 */
    return irt_is64(fins->t) ? INT64FOLD(0) : INTFOLD(0);
  return NEXTFOLD;
}
```

`SUB any any` 命中后,函数检查左右操作数是不是同一个引用(`fins->op1 == fins->op2`)。如果是,`i - i` 恒为 0,直接折叠成常量 0。注意那个 `!irt_isnum(fins->t)` 的保护——**浮点数不能这么折**!原因是浮点的 `x - x` 在 `x` 是 NaN 时不等于 0(NaN - NaN = NaN,而 NaN != 0)。这是浮点语义和整数语义的分野,fold 必须尊重它。这种"看似显然、其实要小心浮点"的细节,在 fold 里到处都是。

异或的规则在 `lj_opt_fold.c:2032-2038`:

```c
LJFOLD(BXOR any any)
LJFOLDF(comm_bxor)
{
  if (fins->op1 == fins->op2)  /* i xor i ==> 0 */
    return irt_is64(fins->t) ? INT64FOLD(0) : INTFOLD(0);
  return fold_comm_swap(J);
}
```

`i xor i` 对所有整数恒为 0(每个位自己和自己异或都是 0),所以折叠成 0。这里没有浮点问题,因为 `BXOR` 本来就只作用于整数。

### 4.3 复合表达式的抵消:`(a + b) - a → b`

更进一阶的代数化简,是识别**复合表达式里的抵消**。比如 `(a + b) - a`,减法把加法加进去的 `a` 又减掉了,结果就是 `b`。这种模式在循环下标计算里极其常见(比如 `i` 先加了一个偏移、后来又减回去),fold 专门写了规则抓它,`lj_opt_fold.c:1479-1490`:

```c
LJFOLD(SUB ADD any)
LJFOLDF(simplify_intsubadd_leftcancel)
{
  if (!irt_isnum(fins->t)) {
    PHIBARRIER(fleft);
    if (fins->op2 == fleft->op1)  /* (i + j) - i ==> j */
      return fleft->op2;
    if (fins->op2 == fleft->op2)  /* (i + j) - j ==> i */
      return fleft->op1;
  }
  return NEXTFOLD;
}
```

`SUB ADD any` 匹配"左操作数是一条 ADD 指令"的减法。函数检查:当前减法的右操作数(`fins->op2`),是不是等于那条 ADD 的某一个操作数。如果是,说明减法抵消了加法的一项——`(i + j) - i` 抵消 `i` 剩 `j`,`(i + j) - j` 抵消 `j` 剩 `i`。返回剩下的那一项(`fleft->op2` 或 `fleft->op1`),整条减法和那条加法都省了。

这里出现了一个重要的宏 `PHIBARRIER(fleft)`,我们在 §8 会专门讲。先记住它的作用:**如果 `fleft` 是个 PHI 节点(循环变量在循环头汇合的地方),就不做这次折叠**——因为跨 PHI 折叠会破坏循环的 SSA 结构。

类似的抵消规则还有一串:`(i - j) - i → 0 - j`(`simplify_intsubsub_leftcancel`)、`i - (i - j) → j`(`simplify_intsubsub_rightcancel`)、`i - (i + j) → 0 - j`(`simplify_intsubadd_rightcancel`)、`(i + j1) - (i + j2) → j1 - j2`(`simplify_intsubaddadd_cancel`,这个函数体里穷举了四种子情形)。这些规则都是同一个思路:**识别出复合表达式里能互相抵消的部分,把整条运算约简成一个更简单的运算或一个已知的操作数**。

### 4.4 浮点代数化的陷阱:为什么有些"显然"的规则不能写

讲到代数化简,必须专门提一个反直觉的点:**浮点数的代数化简要极其小心,很多看起来天经地义的规则其实是错的**。LuaJIT 在 `lj_opt_fold.c:998-1003` 专门留了一段警告注释:

```c
/* FP arithmetic is tricky -- there's not much to simplify.
** Please note the following common pitfalls before sending "improvements":
**   x+0 ==> x  is INVALID for x=-0
**   0-x ==> -x is INVALID for x=+0
**   x*0 ==> 0  is INVALID for x=-0, x=+-Inf or x=NaN
*/
```

这三条"陷阱"值得逐条理解,因为它们是浮点语义和数学常识分道扬镳的典型:

**`x + 0 → x` 对浮点是错的。** 当 `x = -0.0`(负零)时,`-0.0 + 0.0 = +0.0`(IEEE 754 规定正零加负零得正零),而 `x` 本身是 `-0.0`。两者在 IEEE 754 里是不同的位模式(虽然 `==` 比较相等),如果有代码靠 `1/x` 的正负无穷来区分正负零,这个化简就改变了结果。所以浮点不能套用整数的 `x + 0 → x`。

**`0 - x → -x` 对浮点是错的。** 当 `x = +0.0` 时,`0.0 - 0.0 = +0.0`(不是 `-0.0`),而 `-x` 是 `-0.0`。又对不上。

**`x * 0 → 0` 对浮点是错的。** 当 `x = -0.0`、`x = ±Inf`、或 `x = NaN` 时,`x * 0` 分别是 `-0.0`、`NaN`、`NaN`,都不是 `+0.0`。

正因为这些陷阱,LuaJIT 对浮点只保留了少数**确实安全**的化简。比如 `SUB any KNUM` 里只处理 `x - (+0) → x`(因为减 `+0` 对任何浮点 `x` 都等于 `x`,这条是安全的),`lj_opt_fold.c:1024-1030`:

```c
LJFOLD(SUB any KNUM)
LJFOLDF(simplify_numsub_k)
{
  if (ir_knum(fright)->u64 == 0)  /* x - (+0) ==> x */
    return LEFTFOLD;
  return NEXTFOLD;
}
```

注意它显式检查的是 `u64 == 0`,即位模式恰好是正零(`+0.0` 的 IEEE 754 编码是全 0)——只有正零才安全,负零(`u64 == 0x8000000000000000`)不行。这种**精确到位模式的判断**,是浮点 fold 规则的标配。

这个例子把 fold 的 sound 纪律体现得淋漓尽致:**每一条规则,都必须是对所有可能取值(包括 NaN、正负零、无穷)都成立的恒等式**。任何一条"在大多数情况下成立"的规则,都会在边界情况(那些古怪的浮点特殊值)上改变程序结果,是不可接受的。LuaJIT 的 fold 用大量精确的位模式检查和保守的规则选择,守住了这条纪律。

---

## §5 强度削减:把贵的运算换成便宜的

第三大类优化是**强度削减(strength reduction)**:把一条"贵"的运算(执行时间长),替换成一条语义等价但"便宜"的运算(执行时间短)。最经典的例子就是**乘法变移位**。

### 5.1 乘 2 的幂:乘法变左移

考虑 `x * 8`。CPU 上整数乘法指令(`IMUL` on x86)通常要 3 个周期左右;而左移 3 位 `x << 3` 只要 1 个周期,两者结果完全一样(对补码整数,左移 k 位等于乘 2^k)。所以把 `x * 8` 换成 `x << 3`,白赚两个周期。

LuaJIT 抓这个模式的规则在 `lj_opt_fold.c:1411-1435`。先看一个辅助函数:

```c
static TRef simplify_intmul_k(jit_State *J, int32_t k)
{
  /* Note: many more simplifications are possible, e.g. 2^k1 +- 2^k2.
  ** But this is mainly intended for simple address arithmetic.
  ** Also it's easier for the backend to optimize the original multiplies.
  */
  if (k == 0) {  /* i * 0 ==> 0 */
    return RIGHTFOLD;
  } else if (k == 1) {  /* i * 1 ==> i */
    return LEFTFOLD;
  } else if ((k & (k-1)) == 0) {  /* i * 2^k ==> i << k */
    fins->o = IR_BSHL;
    fins->op2 = lj_ir_kint(J, lj_fls((uint32_t)k));
    return RETRYFOLD;
  }
  return NEXTFOLD;
}
```

核心是 `(k & (k-1)) == 0` 这个判断——这是经典的"判断一个数是不是 2 的幂"的位运算技巧:`k` 是 2 的幂时,它的二进制只有一个 1,`k-1` 就是把这个 1 借位变成后面一串 1,两者按位与为 0。命中后,把 `MUL` 改成 `BSHL`(左移),移位量是 `lj_fls(k)`(找 `k` 最高位的 1 在第几位,比如 `8 = 1000₂`,`fls` 返回 3)。

调用它的入口规则:

```c
LJFOLD(MUL any KINT)
LJFOLDF(simplify_intmul_k32)
{
  if (fright->i >= 0)
    return simplify_intmul_k(J, fright->i);
  return NEXTFOLD;
}
```

注意只处理 `fright->i >= 0`(非负常量)。负常量乘法为什么不折成移位?因为补码的负数移位语义比正数复杂,而且负号可以单独处理(配合 `MUL any KNUM` 里的 `x * -1 → -x` 规则)。

强度削减在 `MUL any KNUM` 这条浮点规则里也有一个有意思的变体,`lj_opt_fold.c:1063-1066`:

```c
  } else if (fins->o == IR_MUL && n == 2.0) {  /* x * 2 ==> x + x */
    fins->o = IR_ADD;
    fins->op2 = fins->op1;
    return RETRYFOLD;
  }
```

浮点乘 2 被换成浮点加自身(`x + x`)。为什么浮点不换成左移?因为浮点的位模式和整数不同,左移会把浮点的位当整数移,完全破坏值。但 `x + x` 在浮点语义下等于 `x * 2`(对正常值),而且浮点加法通常比浮点乘法快一点(尤其在不支持 FMA 的硬件上),所以这是个温和的强度削减。这条规则后面跟着的还有 `x / 2^k → x * 2^-k`(把除法换成乘以倒数,因为除法比乘法慢得多),`lj_opt_fold.c:1067-1076`:

```c
  } else if (fins->o == IR_DIV) {  /* x / 2^k ==> x * 2^-k */
    uint64_t u = ir_knum(fright)->u64;
    uint32_t ex = ((uint32_t)(u >> 52) & 0x7ff);
    if ((u & U64x(000fffff,ffffffff)) == 0 && ex - 1 < 0x7fd) {
      u = (u & ((uint64_t)1 << 63)) | ((uint64_t)(0x7fe - ex) << 52);
      fins->o = IR_MUL;  /* Multiply by exact reciprocal. */
      fins->op2 = lj_ir_knum_u64(J, u);
      return RETRYFOLD;
    }
  }
```

这段在直接操作 IEEE 754 双精度的位模式:检查除数是不是 2 的幂(尾数全 0、指数在合法范围),如果是,就计算出它的精确倒数(还是 2 的幂,指数取负),把除法换成乘法。**注意"精确"二字**——只有当倒数能精确表示(即除数是 2 的幂)时才能换,否则乘倒数和直接除会有舍入差异,不 sound。这种对 IEEE 754 位级别的精确操作,是 fold 处理浮点的常态。

### 5.2 移位自身的强度削减:移 1 位变加法

有意思的是,反向的强度削减也存在:**左移 1 位 `x << 1` 会被换回 `x + x`**,`lj_opt_fold.c:1619-1623`:

```c
  if (k == 1 && fins->o == IR_BSHL) {  /* i << 1 ==> i + i */
    fins->o = IR_ADD;
    fins->op2 = fins->op1;
    return RETRYFOLD;
  }
```

为什么移位反而要变加法?这不是倒退吗?这体现了 fold 和后端的分工:**fold 做的是"规范化",有些后端(尤其 x86)的 LEA 指令能把 `x + x` 计算和地址运算合并,比单独的移位指令更高效**。fold 不知道目标架构的细节,但它知道"加法是个更基础、后端优化空间更大的形式",所以把 `<< 1` 规范化成 `+`。这背后的哲学是:**fold 不追求生成"看起来最快"的 IR,而是追求生成"后端最容易优化"的 IR**。

这个例子也解释了为什么 fold 注释(§3.4 引用的那段)强调规则要**单调(monotonic)**——`MUL *2` → `BSHL 1`、`BSHL 1` → `ADD x x`,这两个规则如果同时存在,会不会无限循环(`ADD x x` 又被某条规则变回 `MUL *2`...)?不会,因为**没有 `ADD (x,x) → MUL x,2` 这条反向规则**。fold 的规则集合被设计成只会朝"更简化"的方向走,不会有反向规则,从而保证终止(`lj_opt_fold.c:120-133` 的 Requirement #3 说的就是这个)。

### 5.3 取模变按位与:`i % 8 → i & 7`

还有一个非常实用的强度削减:**对 2 的幂取模,换成按位与**。`i % 8` 等于 `i & 7`(因为 8 = 2^3,取模 2^k 等于保留低 k 位,也就是和 2^k - 1 做按位与)。取模指令(`IDIV` 系)要几十个周期,按位与只要 1 个周期,差几十倍。规则在 `lj_opt_fold.c:1449-1460`:

```c
LJFOLD(MOD any KINT)
LJFOLDF(simplify_intmod_k)
{
  int32_t k = fright->i;
  lj_assertJ(k != 0, "integer mod 0");
  if (k > 0 && (k & (k-1)) == 0) {  /* i % (2^k) ==> i & (2^k-1) */
    fins->o = IR_BAND;
    fins->op2 = lj_ir_kint(J, k-1);
    return RETRYFOLD;
  }
  return NEXTFOLD;
}
```

同样的 `(k & (k-1)) == 0` 判断 2 的幂。命中后 `MOD` 变 `BAND`,操作数从 `k` 变 `k-1`(8 变 7、16 变 15)。这条规则在哈希表查找(`hash & mask`)、环形缓冲区下标(`idx & (size-1)`)等场景里极其常见,能带来巨大的加速。

---

## §6 重新结合:把分散的常量聚到一起

第四大类优化叫**重新结合(reassociation)**,它解决的是一个更隐蔽的冗余:**常量分散在不同的子表达式里,没法直接常量折叠**。回忆 §2.1 的例子 `a + 1 + 2`:纯粹按"两个操作数都是常量"折叠,抓不到 `1 + 2`,因为它们被 `a` 隔开了。重新结合要做的事,就是把这种分散的常量**挪到一起**,让常量折叠能下手。

### 6.1 `(i + k1) + k2 → i + (k1 + k2)`

核心规则在 `lj_opt_fold.c:1769-1787`:

```c
LJFOLD(ADD ADD KINT)
LJFOLD(MUL MUL KINT)
LJFOLD(BAND BAND KINT)
LJFOLD(BOR BOR KINT)
LJFOLD(BXOR BXOR KINT)
LJFOLDF(reassoc_intarith_k)
{
  IRIns *irk = IR(fleft->op2);
  if (irk->o == IR_KINT) {
    int32_t k = kfold_intop(irk->i, fright->i, (IROp)fins->o);
    if (k == irk->i)  /* (i o k1) o k2 ==> i o k1, if (k1 o k2) == k1. */
      return LEFTFOLD;
    PHIBARRIER(fleft);
    fins->op1 = fleft->op1;
    fins->op2 = (IRRef1)lj_ir_kint(J, k);
    return RETRYFOLD;  /* (i o k1) o k2 ==> i o (k1 o k2) */
  }
  return NEXTFOLD;
}
```

`ADD ADD KINT` 匹配的模式是:当前是加法,左操作数也是加法,右操作数是常量。也就是 `(something + k1) + k2` 这种形状。函数检查那条内层加法的右操作数(`fleft->op2`,即 `k1`)是不是也是常量。如果是,那就有两个常量 `k1`、`k2` 参与同一个加法链,可以把它们先加起来:`(i + k1) + k2 → i + (k1 + k2)`。

具体做法很巧妙:它**不新建一条加法**,而是**修改当前指令**:把当前指令的左操作数从 `fleft`(那条内层 `(i + k1)`)改成 `fleft->op1`(即 `i`),右操作数从 `k2` 改成"两个常量的和 `k1 + k2`"(用 `kfold_intop` 算出来,申请成新的 `KINT`)。然后 `RETRYFOLD`——这条修改后的 `ADD i (k1+k2)` 再走一遍引擎。

这一步有个特别漂亮的副作用:如果 `k1 + k2` 算出来恰好等于 `k1`(比如 `k2 = 0`),那 `if (k == irk->i) return LEFTFOLD` 这一支直接命中,化简成 `LEFTFOLD`(整条运算退化成内层的 `i + k1`,外层加 0 被消掉)。这就是为什么重新结合和恒等元化简能协同:重新结合把常量聚拢,聚拢后如果触发了恒等元规则,就再消一层。

回到 §2.1 的 `a + 1 + 2`。现在我们能看到完整的化简链了:

1. 录制器发出 `ADD a 1`(假设是 `%3`)和 `ADD %3 2`。
2. 第二条 `ADD %3 2` 进 fold。它的左操作数 `%3` 是 `ADD`,右操作数是 `KINT 2`。匹配 `ADD ADD KINT`。
3. 内层 `%3` 的右操作数是 `KINT 1`。于是 `k = 1 + 2 = 3`,把当前指令改成 `ADD a KINT(3)`,`RETRYFOLD`。
4. 重试:现在是 `ADD a KINT(3)`,左是变量 `a`,右是常量 3。匹配 `ADD any KINT`,函数 `simplify_intadd_k` 检查右边是不是 0,不是,`NEXTFOLD`。
5. 没有更具体的规则,最终交 CSE,发射成 `ADD a, 3`。

一次加法代替了两次。这就是重新结合 + 常量折叠协同的效果。

### 6.2 重复操作数的化简:`(a & b) & a → a & b`

除了常量聚拢,重新结合还处理一类**操作数重复**的情形。`lj_opt_fold.c:1812-1819`:

```c
LJFOLD(BAND BAND any)
LJFOLD(BOR BOR any)
LJFOLDF(reassoc_dup)
{
  if (fins->op2 == fleft->op1 || fins->op2 == fleft->op2)
    return LEFTFOLD;  /* (a o b) o a ==> a o b; (a o b) o b ==> a o b */
  return NEXTFOLD;
}
```

`(a & b) & a`——外层又拿 `a` 去和 `(a & b)` 做 `&`。因为 `&` 满足幂等性(`x & x = x`)和吸收性,`(a & b) & a = a & b & a = a & a & b = a & b`,结果就是内层。所以直接返回 `LEFTFOLD`(内层的 `(a & b)`)。这个规则对 `&` 和 `|` 都成立(两者都幂等),但对 `xor` 不成立(`xor` 不幂等),`xor` 有它自己的规则,`lj_opt_fold.c:1830-1839`:

```c
LJFOLD(BXOR BXOR any)
LJFOLDF(reassoc_bxor)
{
  PHIBARRIER(fleft);
  if (fins->op2 == fleft->op1)  /* (a xor b) xor a ==> b */
    return fleft->op2;
  if (fins->op2 == fleft->op2)  /* (a xor b) xor b ==> a */
    return fleft->op1;
  return NEXTFOLD;
}
```

`(a xor b) xor a = b`——异或两次同一个值等于没异或,这是异或的自反性。fold 抓住它,直接返回内层的另一个操作数。这在加密、哈希、状态翻转等大量使用 xor 的代码里很有用。

### 6.3 移位的重新结合:`(i << k1) << k2 → i << (k1+k2)`

连续两次移位也可以合并,`lj_opt_fold.c:1841-1866`:

```c
LJFOLD(BSHL BSHL KINT)
LJFOLD(BSHR BSHR KINT)
LJFOLD(BSAR BSAR KINT)
LJFOLD(BROL BROL KINT)
LJFOLD(BROR BROR KINT)
LJFOLDF(reassoc_shift)
{
  IRIns *irk = IR(fleft->op2);
  PHIBARRIER(fleft);
  if (irk->o == IR_KINT) {  /* (i o k1) o k2 ==> i o (k1 + k2) */
    int32_t mask = irt_is64(fins->t) ? 63 : 31;
    int32_t k = (irk->i & mask) + (fright->i & mask);
    if (k > mask) {  /* Combined shift too wide? */
      if (fins->o == IR_BSHL || fins->o == IR_BSHR)
        return mask == 31 ? INTFOLD(0) : INT64FOLD(0);
      else if (fins->o == IR_BSAR)
        k = mask;
      else
        k &= mask;
    }
    fins->op1 = fleft->op1;
    fins->op2 = (IRRef1)lj_ir_kint(J, k);
    return RETRYFOLD;
  }
  return NEXTFOLD;
}
```

`(i << 3) << 2 → i << 5`。注意它还处理了"移位量加起来超过位宽"的边界情况:左移/右移超过位宽,32 位整数结果是 0(所有位都移出去了);算术右移(`BSAR`)超过位宽,等价于移满(全是符号位);循环移位(`BROL`/`BROR`)取模。这些边界处理再次体现 fold 的精确性——**化简必须对所有移位量都正确,包括极端的移位量**。

---

## §7 公共子表达式消除与存储消除:fold 引擎的搭档

讲完了四大类算术优化,我们要补充一个重要的事实:**fold 引擎不只是做算术化简,它还是其他几类优化的入口**。具体说,fold 引擎在"所有规则都没命中"时,默认会把指令交给一个叫 **CSE(Common-Subexpression Elimination,公共子表达式消除)** 的优化;而对于加载、存储、分配这些有副作用的指令,fold 表里专门列了规则,把它们转交给**别名分析/转发(forwarding)**和**死存储消除(DSE)**。这一节我们简要讲 CSE,DSE 和转发留到下一章(P3-10)细讲。

### 7.1 CSE:相同的运算只算一次

公共子表达式消除的思想很朴素:**如果同一个运算在 trace 里出现了两次,且两次的操作数完全相同,那么第二次的结果必然和第一次相同,直接复用第一次的结果,不必再算**。

比如 `x + y` 算了一次得到 `%5`,后面又遇到一次 `x + y`(操作数还是那两个引用),那第二次完全不用发新指令,直接用 `%5`。机器码里少一条加法。

CSE 的实现利用了 SSA 的一个性质。上一章讲过,LuaJIT 的 IR 维护着**按 opcode 分的链表**(`J->chain[IR_ADD]` 是所有 `ADD` 指令组成的链表,靠 `IRIns.prev` 字段串起来)。要查"这条 `ADD op1 op2` 之前有没有出现过",只要顺着 `J->chain[IR_ADD]` 往前找,看有没有哪条的 `op12`(`op1` 和 `op2` 拼成的 32 位)和当前相同。代码在 `lj_opt_fold.c:2603-2630`:

```c
/* CSE an IR instruction. This is very fast due to the skip-list chains. */
TRef LJ_FASTCALL lj_opt_cse(jit_State *J)
{
  /* Avoid narrow to wide store-to-load forwarding stall */
  IRRef2 op12 = (IRRef2)fins->op1 + ((IRRef2)fins->op2 << 16);
  IROp op = fins->o;
  if (LJ_LIKELY(J->flags & JIT_F_OPT_CSE)) {
    /* Limited search for same operands in per-opcode chain. */
    IRRef ref = J->chain[op];
    IRRef lim = fins->op1;
    if (fins->op2 > lim) lim = fins->op2;  /* Relies on lit < REF_BIAS. */
    while (ref > lim) {
      if (IR(ref)->op12 == op12)
        return TREF(ref, irt_t(IR(ref)->t));  /* Common subexpression found. */
      ref = IR(ref)->prev;
    }
  }
  /* Otherwise emit IR (inlined for speed). */
  {
    IRRef ref = lj_ir_nextins(J);
    IRIns *ir = IR(ref);
    ir->prev = J->chain[op];
    ir->op12 = op12;
    J->chain[op] = (IRRef1)ref;
    ir->o = fins->o;
    J->guardemit.irt |= fins->t.irt;
    return TREF(ref, irt_t((ir->t = fins->t)));
  }
}
```

读懂这段,就理解了 CSE 的全部机制:

- `op12` 把两个操作数拼成一个 32 位整数,作为"这条运算的指纹"。
- 沿着 `J->chain[op]` 链表往前找(`ref = IR(ref)->prev`),找有没有 `op12` 相同的。搜索有个下界 `lim`(取两个操作数引用的较大值)——意思是**只找定义在这条指令之前、且定义点比两个操作数都晚的指令**。这个界限保证:被复用的那条指令,它的操作数此时都已经定义好了(SSA 的支配关系)。
- 找到了就返回那个旧引用(`TREF(ref, ...)`),不发射新指令。这就是 CSE 命中。
- 找不到就 `lj_ir_nextins` 申请一个新 IR 槽,把这条指令真正写进 IR 数组,并把它**插到 `J->chain[op]` 链表头**(`ir->prev = J->chain[op]; J->chain[op] = ref`)——这样后面的同 opcode 指令就能查到它。

注释说"very fast due to the skip-list chains"——这个 `chain` 虽然代码上看着像单链表,但 LuaJIT 的实现里它有跳表(skip-list)的优化(在某些场景下加速查找),所以即便 trace 很长,CSE 的查找也很快。

### 7.2 fold 与 CSE 的接力

把 fold 引擎(§3.4)和 CSE(§7.1)合起来,一条 IR 指令的完整命运是这样的:

1. 录制器调 `emitir`,进 `lj_opt_fold`。
2. fold 引擎按从具体到一般的顺序查规则表。
3. 如果命中某条化简规则(`kfold_intarith`、`simplify_intadd_k`、`reassoc_intarith_k` 等),执行化简,可能 `RETRYFOLD` 再来一轮,直到化简干净,返回最终结果。
4. 如果所有规则都没命中(返回 `NEXTFOLD` 用尽了),`lj_opt_fold.c:2582-2583` 那一行 `return lj_opt_cse(J)` 把指令交给 CSE。
5. CSE 查有没有等价的旧指令可复用;有就复用,没有就把这条指令真正发射进 IR 数组。

所以**fold 是"能化简就化简",CSE 是"化简不了就看看能不能复用",最后才真正发射**。这两个接力,把"录出来的冗余 IR"压缩到尽可能精简。这就是为什么录出来的 IR 虽然字面翻译、冗余很多,但最终落到 IR 数组里的,已经是经过层层化简和去重的"干净" IR。

### 7.3 加载、存储、分配:交给专门的 pass

fold 表里还有一大类规则,处理的是有副作用的指令——加载(`ALOAD`/`HLOAD`/`ULOAD`/`FLOAD`/`XLOAD`/`VLOAD`)、存储(`ASTORE`/`HSTORE`/`USTORE`/`FSTORE`/`XSTORE`)、分配(`TNEW`/`TDUP`/`CNEW`)。这些指令不能简单地常量折叠或 CSE:

- 加载的结果取决于内存当下是什么,不是纯函数,不能折叠成常量(除非内存确实是只读的,见下)。
- 存储和分配有副作用,不能随意删除。

但它们可以**转发(forwarding)**和**死存储消除(DSE)**:

- **转发**:如果一次存储之后紧接着一次同地址的加载,加载可以直接用刚才存的值,不必真去读内存。这需要别名分析判断两次访问是不是同一地址、中间有没有别的存储改过它。
- **死存储消除**:如果一次存储的值从来没被读过(后面又被覆盖了,或者根本没人用),这次存储是"死"的,可以删掉。

这些优化的入口就在 fold 表里。比如 `lj_opt_fold.c:2124-2125`:

```c
LJFOLD(ALOAD any)
LJFOLDX(lj_opt_fwd_aload)
```

`LJFOLDX` 和 `LJFOLD` 不同——它不是定义一个新函数,而是**把这条指令直接转交给一个已存在的函数**(`lj_opt_fwd_aload`,实现在 `lj_opt_mem.c`)。所以 fold 引擎处理 `ALOAD` 时,会调别名分析的代码,看能不能转发。

存储的入口类似,`lj_opt_fold.c:2471-2482`:

```c
LJFOLD(ASTORE any any)
LJFOLD(HSTORE any any)
LJFOLDX(lj_opt_dse_ahstore)

LJFOLD(USTORE any any)
LJFOLDX(lj_opt_dse_ustore)

LJFOLD(FSTORE any any)
LJFOLDX(lj_opt_dse_fstore)

LJFOLD(XSTORE any any)
LJFOLDX(lj_opt_dse_xstore)
```

每种存储都交给对应的 DSE 函数。而分配指令(`TNEW`/`TDUP`/`CNEW` 等)和有副作用的调用(`CALLA`/`CALLL`/`CALLS`/`CALLXS`)则直接交给 `lj_ir_emit` 原样发射,`lj_opt_fold.c:2484-2496`:

```c
LJFOLD(NEWREF any any)  /* Treated like a store. */
LJFOLD(TMPREF any any)
LJFOLD(CALLA any any)
LJFOLD(CALLL any any)  /* Safeguard fallback. */
LJFOLD(CALLS any any)
LJFOLD(CALLXS any any)
LJFOLD(XBAR)
LJFOLD(RETF any any)  /* Modifies BASE. */
LJFOLD(TNEW any any)
LJFOLD(TDUP any)
LJFOLD(CNEW any any)
LJFOLD(XSNEW any any)
LJFOLDX(lj_ir_emit)
```

文件头注释(`lj_opt_fold.c:88-90`)专门强调了这一点:"all loads, stores and allocations must have an any/any rule to avoid being passed on to CSE"。意思是:**所有加载、存储、分配指令,都必须在 fold 表里有一条 `any/any` 的兜底规则**,否则它们会漏到 CSE 那里被错误地当纯运算去重——那会改变语义(比如两次存储不能合并成一次)。这些 `LJFOLDX(lj_ir_emit)` 和 `LJFOLDX(lj_opt_dse_xstore)` 就是兜底,确保副作用指令走正确的路径。

转发和 DSE 的具体算法(别名分析怎么判断两个引用是否指向同一地址)是下一章 P3-10 的主题,这里只点出**它们和 fold 的关系**:fold 是所有这些优化的统一入口,通过规则表把不同类型的指令分派到对应的处理逻辑。

### 7.4 数组越界检查消除(ABC):一类特殊的化简

还有一个值得专门提的优化,也归 fold 管:**数组越界检查消除(Array Bounds Check elimination,ABC)**。Lua 里访问 `t[i]`,IR 里会发一条 `ABC` 指令做越界检查(是个 guard,保证 `i` 在数组范围内)。但很多时候这个检查是多余的——比如 `i` 是循环变量、范围已知不会越界,或者之前已经查过同样的范围。

fold 里有专门消除冗余 ABC 的规则,`lj_opt_fold.c:1919-1939`:

```c
/* Eliminate ABC for constants.
** ABC(asize, k1), ABC(asize k2) ==> ABC(asize, max(k1, k2))
** Drop second ABC if k2 is lower. Otherwise patch first ABC with k2.
*/
LJFOLD(ABC any KINT)
LJFOLDF(abc_k)
{
  PHIBARRIER(fleft);
  if (LJ_LIKELY(J->flags & JIT_F_OPT_ABC)) {
    IRRef ref = J->chain[IR_ABC];
    IRRef asize = fins->op1;
    while (ref > asize) {
      IRIns *ir = IR(ref);
      if (ir->op1 == asize && irref_isk(ir->op2)) {
        uint32_t k = (uint32_t)IR(ir->op2)->i;
        if ((uint32_t)fright->i > k)
          ir->op2 = fins->op2;
        return DROPFOLD;
      }
      ref = ir->prev;
    }
    return EMITFOLD;  /* Already performed CSE. */
  }
  return NEXTFOLD;
}
```

这个规则做的事:如果对同一个数组(`asize` 相同)已经有一个常量下标的 ABC 检查,新的 ABC 检查可以和它合并——保留较大的那个下标(因为较大的下标过了检查,较小的必然也过)。函数找到旧的 ABC,比较下标,把旧的下标更新成较大的,然后新的 ABC 直接 `DROPFOLD`(丢弃)。这一条规则在循环里访问数组时特别有用:每次循环都可能发新的 ABC,但它们往往可以合并成循环开始时的一次检查。

ABC 消除由 `JIT_F_OPT_ABC` 这个 flag 控制(默认开),用户可以关掉它。这体现了 fold 的一个设计:**每类优化都可以单独开关**,给调试和性能权衡留了余地。

---

## §8 为什么 fold 是 sound 的:三条铁律

讲完了 fold 做的所有事,我们回到全章的核心问题之一:**凭什么相信 fold 不会改变程序结果?** 这一节把 fold 的 soundness 讲透。

### 8.1 铁律一:每条规则都是数学恒等式

这是最根本的一条。我们在前面每一节都反复强调了:**fold 的每一条规则,都建立在一个对所有取值都成立的数学恒等式之上**。

- `1 + 2 → 3`,因为整数加法是确定的函数。
- `x + 0 → x`,因为 0 是加法的恒等元(对**整数**;浮点不行,§4.4 讲过)。
- `x * 8 → x << 3`,因为补码整数左移 k 位等于乘 2^k。
- `(a + b) - a → b`,因为加法满足结合律和消去律。
- `i % 8 → i & 7`,因为模 2^k 等于保留低 k 位。

每一个都是可以被数学证明的恒等式——**不是"在大多数情况下成立",而是"在所有情况下成立"**。fold 的规则函数,本质上就是把这些恒等式逐条翻译成代码。只要恒等式成立,化简就不改变结果。

这条铁律的反面是:**任何不构成恒等式的"化简",都不能写成规则**。§4.4 那三条浮点陷阱(`x+0`、`0-x`、`x*0`)就是典型的"看似成立、实则不然"的例子,LuaJIT 的 fold **没有**把这些写成规则——因为它们不是恒等式。这种克制是 soundness 的保障。

### 8.2 铁律二:类型必须保持(Requirement #1)

文件头注释(`lj_opt_fold.c:95-98`)列了写规则必须遵守的三条要求,第一条是:

> **Requirement #1: All fold rules must preserve their destination type.**

意思是:**一条规则化简前后的结果类型必须一致**。如果原指令结果是整数(`KINT`),化简后也必须是整数;如果是浮点(`KNUM`),化简后也必须是浮点。

为什么这条重要?因为 IR 的类型信息贯穿整个后端——寄存器分配按类型选寄存器(整数用通用寄存器、浮点用 XMM)、汇编生成按类型选指令。如果一条产生浮点结果的指令被错误地化简成产生整数结果,后端会用错误的寄存器/指令,结果全错。

这条要求在实现上的体现是:做整数常量折叠用 `INTFOLD`(产生 `KINT`),做浮点常量折叠用 `lj_ir_knum`(产生 `KNUM`),**不能混用**。注释特别警告:"Never use `lj_ir_knumint()` which can have either a KINT or KNUM result"——`lj_ir_knumint` 这个函数会根据值大小自动选整数或浮点,看似方便,但破坏了类型确定性,所以在 fold 里禁用。

类型保持的一个有趣例子是 `simplify_numpow_k` 里的 `x ^ 0 → 1`,`lj_opt_fold.c:1104-1105`:

```c
  if (knumright == 0.0)  /* x ^ 0 ==> 1 */
    return lj_ir_knum_one(J);  /* Result must be a number, not an int. */
```

注释强调"Result must be a number, not an int"。哪怕 `x ^ 0` 的值是整数 1,结果也必须用 `lj_ir_knum_one`(产生浮点 1.0),因为原指令 `POW` 的结果类型是浮点。如果偷懒返回 `INTFOLD(1)`,类型就错了。这种对类型一致性的坚持,是 fold sound 的第二道闸。

### 8.3 铁律三:不跨 PHI 折叠(Requirement #2)

第三条要求是最微妙的,它关系到 fold 和循环 SSA 的交互:

> **Requirement #2: Fold rules should not create *new* instructions which reference operands *across* PHIs.**

要理解这条,得先回忆上一章(P2-07)讲的 PHI 节点。在循环的 SSA 里,循环变量在循环头会有一个 PHI 节点,把"循环前的初值"和"上一轮迭代后的值"汇合起来。PHI 是循环 SSA 的边界——**跨过 PHI 就意味着从循环内引用了循环外的东西**。

考虑一个折叠机会:`((i + c1) + c2)`,其中 `(i + c1)` 是个 PHI 的某个输入。如果我们像 §6.1 那样重新结合成 `i + (c1+c2)`,新建的加法会引用 `i`——但 `i` 在 PHI 的另一侧(循环前),这就"跨过了 PHI 边界"。后果是后端要么得给 `i` 加一个新 PHI(让它能在循环内被引用),要么得做复杂的寄存器搬运,反而把代码搞得更差。

LuaJIT 的处理是:**用 `PHIBARRIER` 宏显式禁止跨 PHI 的折叠**。这个宏定义在 `lj_opt_fold.c:157`:

```c
/* Barrier to prevent using operands across PHIs. */
#define PHIBARRIER(ir)	if (irt_isphi((ir)->t)) return NEXTFOLD
```

它的用法是:在一个规则函数里,如果要访问左操作数的操作数(即 `fleft->op1`),就先 `PHIBARRIER(fleft)`——如果 `fleft` 是个 PHI,直接返回 `NEXTFOLD`,放弃这次折叠。我们在前面很多规则里都见过它:`shortcut_dropleft`、`shortcut_leftleft`、各种 `simplify_*` 和 `reassoc_*` 函数,凡是会"穿透"到操作数的操作数的规则,都加了这道屏障。

注释(`lj_opt_fold.c:108-115`)解释了为什么这样做:**返回已存在的指令(如 `LEFTFOLD`)是安全的,因为该指令本来就在那;但新建一条引用跨 PHI 操作数的指令是有害的**。所以 `PHIBARRIER` 只在"要新建跨 PHI 引用"时才挡,简单的返回已有引用不受影响。

有少数例外:某些循环里频繁出现的高代价模式(比如反复的 int↔num 转换),即便跨 PHI 也值得折。`simplify_conv_int_num`(`lj_opt_fold.c:1127-1134`)就是一例:

```c
LJFOLD(CONV CONV IRCONV_INT_NUM)  /* _INT */
LJFOLDF(simplify_conv_int_num)
{
  /* Fold even across PHI to avoid expensive num->int conversions in loop. */
  if ((fleft->op2 & IRCONV_SRCMASK) ==
      ((fins->op2 & IRCONV_DSTMASK) >> IRCONV_DSH))
    return fleft->op1;
  return NEXTFOLD;
}
```

注意它**没有** `PHIBARRIER`,注释明确写了"Fold even across PHI to avoid expensive num->int conversions in loop"。这是个权衡:循环里反复的 num→int 转换(涉及 `cvttss2si` 之类指令)很贵,即便引入一点寄存器搬运,也比留着这个转换划算。这种例外是经过性能验证的,不是随手写的——注释里 "should be avoided" 和这个例外配合,体现的是 fold 在 soundness 和性能之间的精细拿捏。

### 8.4 铁律的保障:规则集合的单调性(Requirement #3)

最后,文件头还列了第三条要求:

> **Requirement #3: The set of all fold rules must be monotonic to guarantee termination.**

这条不是关于单条规则的正确性,而是关于**整个 fold 引擎的终止性**。因为 fold 有 `RETRYFOLD` 机制——一条指令被化简后可能再走一遍引擎,如果不小心,可能陷入无限循环(规则 A 把 X 变成 Y,规则 B 又把 Y 变回 X...)。

`lj_opt_fold.c:120-133` 的注释解释了如何避免:

```text
The goal is optimization, so one primarily wants to add strength-reducing
rules. This means eliminating an instruction or replacing an instruction
with one or more simpler instructions. Don't add fold rules which point
into the other direction.

Some rules (like commutativity) do not directly reduce the strength of
an instruction, but enable other fold rules (e.g. by moving constants
to the right operand). These rules must be made unidirectional to avoid
cycles.
```

核心思想:**规则只能朝"更简化"的方向走,不能反向**。强度削减规则天然满足(乘法→移位→加法,每一步都更便宜);不直接简化的规则(如交换律,把常量挪到右边)必须做成**单向**的——比如 `comm_swap` 只把较小的引用挪到右边(`lj_opt_fold.c:1969`: `if (fins->op1 < fins->op2) swap`),绝不会把已经在右边的常量挪回左边,从而避免来回交换。

这套"单调性 + 单向规则"的设计,保证了 fold 引擎对任何输入都会在有限步内终止——要么化简到无法再化简,要么交 CSE。这是 fold 能"在线、每条指令都跑"的前提:如果 fold 可能不终止,它就不能放在每条 IR 的发射路径上。

把三条铁律(恒等式 + 类型保持 + 不跨 PHI)加上单调性保障合起来,fold 的 soundness 和终止性都有了坚实的保证。这就是为什么 LuaJIT 敢让 fold 自动改写每一条 IR,而不用担心算错或卡死。

---

## §9 fold 与 SSA:为什么 SSA 让 fold 如此简单

讲完 soundness,我们要回头呼应一下上一章(P2-07)的主题:SSA。**fold 之所以能用这么简洁的规则表实现,根本原因在于 IR 是 SSA 形式**。这一节把两者的关系讲透。

### 9.1 单赋值让"查依赖"变成"看一眼"

考虑 fold 要做的一件典型事:判断"当前指令的左操作数,是不是个常量"。在非 SSA 的 IR 里(比如 LLVM 之前的、用变量名的形式),这要查"这个变量最后一次被赋值是什么"——可能要回溯一串赋值,还要处理别名(同一个值可能被多个变量名引用)。

但在 SSA 里,这件事简单到不需要查:**每个引用(ref)唯一对应一条定义指令,且这条指令永远不会变**。`fleft->o == IR_KINT` 这个判断就足够了——左操作数指向的那条指令,如果 opcode 是 `KINT`,它就是常量,而且永远是那个常量。没有"后来被改了"的可能,因为 SSA 禁止重新赋值。

§3.4 引擎主体里那几行 `*fleft = *IR(fins->op1)`——直接把左操作数指令拷贝过来缓存——之所以可行,也是因为 SSA:这条指令的字段不会变,拷贝一份和原地读完全等价。在非 SSA 里这种拷贝是危险的(拷贝之后原指令可能被改)。

### 9.2 依赖关系清晰,化简才敢放手

更深一层,SSA 让 fold 敢于做"穿透操作数"的化简——比如 §4.3 的 `(a + b) - a → b`,§6.1 的 `(i + k1) + k2 → i + (k1+k2)`。这些规则都要访问"操作数的操作数"(`fleft->op1`)。

在非 SSA 里,这种穿透是危险的:`fleft->op1` 指向的值,可能在那条加法执行之后被别的地方改过,用它来做化简可能引用了一个错误的、被覆盖的值。

但在 SSA 里,`fleft->op1` 指向的指令**就是那个值的唯一来源,从它被定义到被使用之间不会被改**。所以 fold 可以放心地穿透多层操作数(`fleft->op1`、`IR(fleft->op1)->op2`...),只要这些指令都是支配当前指令的(SSA 的支配性质保证了它们已经定义好)。

唯一要小心的是 PHI 边界(§8.3),因为 PHI 是 SSA 里唯一一个"值来自多个地方"的节点,跨过它就脱离了单赋值的清晰性——这就是为什么需要 `PHIBARRIER`。除了 PHI 这个边界,fold 在 SSA 的其余部分都可以大胆穿透化简。

### 9.3 CSE 完全依赖 SSA 的"相同引用即相同值"

§7.1 的 CSE,判断"两条运算是否相同"用的是 `IR(ref)->op12 == op12`——比较操作数引用是否相同。这个判断**只有在 SSA 里才等价于"运算结果相同"**:

- SSA 里,相同的引用必然指向相同的值(单赋值),所以操作数引用相同 → 操作数值相同 → 运算结果相同。CSE 复用是安全的。
- 非 SSA 里,相同的引用(比如同一个变量名)可能在两次运算之间被改过,操作数引用相同不代表值相同,CSE 就错了。

LuaJIT 的 CSE 能做到"very fast"且正确,完全得益于 SSA。`J->chain[op]` 这个按 opcode 分的链表,本质上就是利用了"相同 opcode + 相同操作数引用 = 相同运算"这个 SSA 性质,把所有等价运算快速归并。

所以 P2-07 讲的 SSA 不只是个 IR 形式的选择,它是**整个 fold 优化能简洁、正确、高效运行的基石**。没有 SSA,LuaJIT 的 fold 不可能用 2655 行就覆盖几百条规则、在线处理每一条 IR。fold 和 SSA 的这种共生关系,是 trace JIT 优化层设计的精髓。

---

## §10 ★对照:官方 Lua 与 JVM/V8

讲完了 fold 的全部机制,我们把它和两个对象放在一起对照,看清 LuaJIT 的取舍。

### 10.1 对照一:官方 Lua(切"有没有优化 pass")

官方 Lua 是纯解释器,它**没有任何优化 pass**。Lua 代码被解析成字节码后,字节码就直接交给解释器逐条执行,中间没有 IR、没有 fold、没有任何化简。

这意味着什么?意味着官方 Lua 每执行一次 `x * 1`、每执行一次 `i % 8`,解释器都要老老实实地走一遍完整的"取指→译码→判断类型→选运算→执行"流程,把那个本可以省掉/换掉的运算实打实地算一遍。在跑一百万次的循环里,这一百万次多余运算,一次都跑不掉。

对比之下,LuaJIT 的 fold 在编译期就把这些多余运算消掉了——`x * 1` 在 trace 里根本不存在(被 `LEFTFOLD` 消了),`i % 8` 变成了一条便宜的 `i & 7`。机器码里只有必要的、最便宜的运算。这就是 JIT 加上优化 pass 之后,比纯解释器快的来源之一(当然,fold 只是众多加速因素之一,更根本的是摆脱了解释器的取指/译码开销,见 P0-01)。

所以这条对照的结论是:**官方 Lua 没有 fold,因为它没有可优化的 IR;LuaJIT 有 fold,因为它有 IR,而 fold 让这份 IR 在变成机器码之前先被压到最精简**。优化 pass 是 JIT 相对解释器的一项独立增益,和"机器码比解释快"这件事本身是叠加的。

### 10.2 对照二:JVM / V8(切"trace 级 fold vs method 级海量 pass")

更有意思的对照是 LuaJIT 的 fold 和 JVM/V8 的优化 pass。两者都是 JIT、都做优化,但**优化的规模、范围、哲学截然不同**。

**规模对照**。LuaJIT 的 fold 是一个 2655 行的文件,做常量折叠、代数化简、强度削减、重新结合、CSE、ABC 消除,以及作为转发/DSE 的入口——**这些都是"局部"优化,针对单条指令或少数几条指令的组合**。而 JVM 的 C2 编译器和 V8 的 TurboFan,优化 pass 是一个庞大的列表:内联(inlining,把被调用函数体展开进调用点)、逃逸分析(escape analysis,判断对象会不会逃出方法,不逃就可以在栈上分配甚至标量化)、循环展开(loop unrolling)、向量化(auto-vectorization,把标量运算合并成 SIMD)、死代码消除、公共子表达式消除、常量折叠、range check elimination、devirtualization(虚调用去虚化)……C2 光是优化 pass 就有几十个,代码量是 LuaJIT fold 的几十倍。

**作用范围对照**。这是最根本的区别,直接源于 trace vs method(见 P0-01 §10)。JVM/V8 以**整个方法**为编译单位,IR 覆盖方法的全部控制流(所有 if/else 分支、所有循环),所以它的优化可以跨分支、跨循环做全局分析——比如逃逸分析要判断一个对象会不会通过任何分支逃出方法,这需要看遍整个方法的控制流图。而 LuaJIT 以**一条 trace**为编译单位,trace 是线性的(没有分叉,分叉靠 guard + side exit),所以 fold 的优化**只在这条线性路径内有效**。LuaJIT 不做逃逸分析(trace 太窄,对象生命周期多半超出单条 trace),不做方法级的内联展开(虽然有 trace 间的 tail-recursion/up/down-recursion 链接,见 P5-19,但那是 trace 级的、不是方法级的)。

**哲学对照**。这个对照最能说明问题:

- **JVM/V8 的哲学是"重剑无锋"**:构建庞大复杂的优化管线,用海量的分析和变换,把方法级的 IR 压榨到极致。代价是编译慢、内存占用大、实现复杂(C2 有几十万行代码)。但它服务的是 Java/JS 这种长期运行的服务端应用,编译开销能被超长的运行时间摊薄。
- **LuaJIT 的哲学是"轻灵精准"**:只做 trace 级、局部、高频生效的优化(fold 就是典型),规则简洁、在线执行、到不动点。代价是优化的"深度"不如 C2(没有逃逸分析、没有向量化),但好处是编译极快、内存占用小、适合嵌入到宿主程序里(游戏脚本、nginx 模块等编译预算紧张的场景)。

用一个具体的对照点收尾:**常量折叠本身两者都做,做法也类似**(都是编译期算常量运算)。但 LuaJIT 的 fold 把它和代数化简、强度削减、CSE、ABC 消除全塞进一个文件、一个引擎、一条在线路径,靠半完美哈希表做到极快;而 C2 的常量折叠只是它几十个 pass 里的一个,夹在内联和逃逸分析之间,服务于一个庞大的、多阶段的优化管线。**同样是"折叠常量",一个是最重要的优化、一个是大海里的一滴水**——这背后的取舍,就是 trace JIT 和 method JIT 的取舍。

### 10.3 对照表

| 维度 | LuaJIT fold(trace 级) | 官方 Lua | JVM/V8(method 级) |
|---|---|---|---|
| 是否有优化 pass | 有(fold 为首) | 无 | 有(几十个 pass) |
| 优化单位 | 单条 trace(线性路径) | — | 整个方法(含全部分支) |
| 常量折叠 | 有(规则表驱动,在线) | — | 有(C2 的 IdealKit 等) |
| 代数化简/强度削减 | 有(fold 内) | — | 有 |
| CSE | 有(fold 引擎内置) | — | 有 |
| 内联 | trace 级链接(非方法级展开) | — | 方法级内联(核心 pass) |
| 逃逸分析/标量化 | 无(trace 太窄) | — | 有(C2 / TurboFan 重头戏) |
| 向量化 | 无 | — | 有(C2 SuperWord) |
| 实现规模 | fold 2655 行 | — | C2 数十万行 |
| 编译速度 | 极快(在线哈希查表) | — | 较慢(多 pass 全局分析) |
| 适用场景 | 嵌入式/游戏/脚本(编译预算紧) | 教学/嵌入 | 长期运行服务端 |

---

## §11 回扣主线:fold 让"变成机器码"变得更值

回到全书的主线:**把动态执行安全变成机器码**。fold 在这条主线里扮演什么角色?

我们要看清:**安全变成机器码,不只是"变"出来就行,还要变得"值"**。如果录出来的 IR 带着满身冗余就变成机器码,那机器码虽然有"机器码的速度"(摆脱了解释器的取指/译码),但背着一堆多余的乘法、多余的越界检查、重复的运算——这个"快"就大打折扣。fold 的工作,就是**在变成机器码之前,把 IR 压到最精简**,让最终生成的机器码每一拍都用在刀刃上。

具体地,fold 通过四类优化贡献于"快":

- **常量折叠**:编译期算掉常量运算,运行时少算。
- **代数化简**:消掉恒等运算(`x + 0`、`i - i`),运行时不算。
- **强度削减**:贵的运算换便宜的(`*8 → <<3`、`%8 → &7`),运行时算得更快。
- **重新结合 + CSE**:聚拢常量、复用已有结果,运行时少发指令。

而 fold 通过三条铁律贡献于"安全":

- **每条规则都是数学恒等式**,保证化简不改变结果。
- **类型保持**,保证后端用对的寄存器和指令。
- **不跨 PHI**,保证循环 SSA 结构不被破坏。

再加上 fold 是**在线**的——站在每条 IR 发射的必经之路上,边录边化简,到不动点——这让优化几乎不增加额外的编译延迟(和"录完再跑一遍 pass"相比)。这种"低开销、高频次、到不动点"的设计,正是 trace JIT 在编译预算紧张时也能产出高质量机器码的关键。

所以 fold 完美地体现了主线的张力平衡:**它追求快(激进地化简每一处可化简的地方),但用 sound 的规则和 SSA 的清晰依赖保障安全(化简绝不改变结果),同时用在线引擎和哈希查表做到省(优化开销极小)**。快、安全、省,在 fold 这一个 pass 里达成了统一。

讲到这里,优化篇的开头就立住了。下一章我们讲 fold 把指令分派出去的两个搭档之一——**内存优化与别名分析**(`lj_opt_mem.c`):fold 处理不了加载和存储(它们有副作用、结果取决于内存状态),这些靠别名分析判断"两次内存访问是不是同一地址",从而做加载转发(Store→Load forwarding)和死存储消除(DSE)。如果说 fold 是"算术层的精简",那么别名分析就是"内存层的精简"——两者合起来,才把 trace 的 IR 从各个维度压到了最紧。

---

*下一章 [P3-10 内存优化与别名分析](P3-10-内存优化与别名分析.md):fold 把加载和存储指令转交给 `lj_opt_mem.c` 的别名分析。我们看 LuaJIT 怎么判断两个内存引用是否别名、怎么做 Store→Load 转发和死存储消除——把 IR 里对内存的访问也压到最精简。*
