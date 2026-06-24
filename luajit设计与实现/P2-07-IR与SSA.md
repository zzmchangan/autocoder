# P2-07 IR 与 SSA

> **本书主线**:把动态执行安全变成机器码。**二分法**:解释器侧 ↔ JIT 侧。本章是 **JIT 侧的数据根基**——讲 trace 的"半成品"IR(中间表示),以及它采用的 SSA(静态单赋值)形式。它是 P2-06 录制阶段的产出物,又是 P3 优化和 P4 代码生成的输入。**★对照**:官方 Lua(无中间层)+ JVM/V8(Sea-of-Nodes / 方法级 SSA vs LuaJIT 的线性 trace 级 SSA)。**源码**:LuaJIT 2.1.ROLLING。**基调**:纯直球,不用比喻;从第一性原理一步步推导。

---

## 引子:录制产出了什么

P2-05 §3 跟着 `trace_state` 的大 switch 走了一遍生命周期。我们看到 RECORD 状态每被回调一次,就调一次 `lj_record_ins`,把解释器刚执行的那条字节码"翻译成一条 IR,追加到正在构建的 trace 里"。P2-06(尚未写)会钻进 `lj_record_ins` 看每种字节码怎么翻译。但本章先退一步,问一个更基本的问题:

> **这条"翻译"翻出来的东西——IR——到底是什么?它为什么长成这个样子?**

这个问题必须先讲透,否则后面 P2-08 的类型窄化、P3 的常量折叠、P4 的寄存器分配,全都悬在半空。因为窄化是在 IR 上窄化、折叠是在 IR 上折叠、寄存器分配是给 IR 的操作数分配寄存器——它们全都是在 IR 这个中间形式上做的。不理解 IR,就理解不了 JIT 编译器到底在"什么东西"上做优化。

所以本章的目标只有一个:**讲清楚 LuaJIT 的 IR 是什么、它为什么是 SSA 形式、这个形式怎么从录制过程自然产生、它又凭什么支撑起后面所有的优化和代码生成。** 我们从最根本的问题开始:为什么不直接把字节码翻成机器码,非要中间多一层 IR?

---

## §1 第一性原理:为什么不直接 Lua → 机器码

### 1.1 一个朴素方案的失败

设想一个最直接的实现:录制阶段每看到一条字节码,直接吐出对应的机器码,一条对一条。比如看到 `x = x + i`(假定 x、i 是整数),就直接生成"取 x、取 i、整数加法、存结果"四条机器指令。录完整条 trace,机器码也就齐了。不需要 IR,不需要中间层。

听起来很省事。但只要你想在生成的机器码上做哪怕最简单的优化,这个方案立刻露馅。

举一个最常见的优化:**常量折叠**。假设循环体里有 `y = x + 0`。录制器忠实地把它翻成"取 x、取常量 0、整数加法、存结果"四条机器指令。但谁都看得出来,加 0 等于没加,这四条指令可以简化成"直接用 x 的值"。

如果只有机器码,你怎么发现"加 0 可以消掉"?机器码层面,加法就是一条 `ADD` 指令,它的两个操作数是两个寄存器或内存位置。你看到 `ADD r1, r2`,无法直接知道 r2 里装的是不是 0——你得追踪 r2 是怎么被赋值的,可能要回溯好几条指令,才能发现"哦,r2 是用常量 0 加载的"。这种在机器码上做反向数据流分析,极其繁琐、极其容易出错。

更要命的优化:**公共子表达式消除(CSE)**。假设循环体里有两处都算 `a[i]`。如果第一处已经把 i 对应的数组元素取出来了,第二处其实不用再取一次,直接用第一处的结果。但机器码层面,两次"取 a[i]"是两条独立的 `MOV` 指令,地址还可能长得不一样(经过不同的地址计算),你很难一眼看出它们取的是同一个东西。

这两个例子说明一个根本问题:

> **机器码是为 CPU 执行设计的,不是为编译器分析设计的。在机器码上做优化,就像在手写汇编上做代码审查——可行但极其痛苦,因为机器码丢失了"这条指令想表达什么"的高层信息。**

那怎么办?答案就是所有现代编译器(LLVM、GCC、JVM 的 C2、V8 的 TurboFan、LuaJIT)不约而同的选择:**在源代码和机器码之间,引入一层中间表示(IR,Intermediate Representation)。**

### 1.2 IR:为优化而生的中间形式

IR 的核心思想一句话:

> **不要直接从字节码翻到机器码,而是先翻到一种"既比字节码低级、又比机器码高级"的中间形式,在中间形式上做所有的优化,最后再把优化好的中间形式翻成机器码。**

为什么要多这一层?因为这一层的形状是**编译器自己设计的**,可以专门为优化和分析定制。它可以把"这条指令想表达什么"明确地写出来,把机器码里隐含的信息显式化。比如:

- 在 IR 里,"加法"是一个明确的操作 `IR_ADD`,它的两个操作数是两个**引用**(reference),引用指向产生这个值的另一条 IR 指令。你一眼就能看出这个加法用了哪些值。
- 在 IR 里,"常量 0"是一条专门的常量指令 `IR_KINT`,它显式地写着"我是 0"。你不用追踪寄存器赋值历史就知道某个操作数是不是常量。
- 在 IR 里,"加载 a[i]"是一条 `IR_ALOAD` 指令,它的操作数明确标注了"从哪个数组、哪个下标加载"。两条 `ALOAD` 是不是取同一个东西,比较操作数就能判断。

有了这些显式信息,常量折叠、公共子表达式消除、死代码消除等等优化,都变成了对 IR 的模式匹配和改写——直观、高效、正确性容易保证。

所以 IR 不是可有可无的装饰,它是整个优化流水线的**地基**。P3 整篇(常量折叠、内存优化、分配消除)都是"在 IR 上改写 IR"。P4 的代码生成是"把 IR 翻成机器码"。不理解 IR,就看不懂后面所有章节。

那 LuaJIT 的 IR 长什么样?它采用了一种叫 **SSA(Static Single Assignment,静态单赋值)** 的形式。SSA 是现代编译器的标配,但它对很多读者是陌生的。我们先放慢脚步,用一整个小节把 SSA 从第一性原理推导出来。

---

## §2 第一性原理:SSA 是什么,为什么它好优化

SSA 三个字听起来吓人,但它背后的动机极其朴素。这一节我们纯靠"为什么普通代码难优化",一步步把 SSA 推出来。

### 2.1 普通代码的根本麻烦:一个变量被赋值多次

看这段伪代码(用类似 Lua 的语法,但只是为了说明问题):

```
x = 1          // 第一次给 x 赋值
... 一些代码 ...
y = x + 2      // 用 x
x = 10         // 第二次给 x 赋值
... 一些代码 ...
z = x + 2      // 又用 x
```

注意:`x` 这个变量,被赋值了**两次**(第一次是 1,第二次是 10)。而且,`y = x + 2` 和 `z = x + 2` 表面上长得一样,但它们用的 `x` 不是同一个值——前者用第一次的 1,后者用第二次的 10。

现在编译器想做常量折叠/公共子表达式消除,它遇到一个大麻烦。假设它想问"`x + 2` 这个表达式,前面是不是已经算过、能不能复用?"它必须搞清楚:这里的 `x` 到底是哪一次赋值的 x?

为了回答这个问题,编译器要做"到达定义分析(reaching definition)":对每一个变量的使用点,追踪这个值是从哪条赋值语句流过来的。这件事在有分支的情况下尤其复杂——如果代码是:

```
if cond then
  x = 1
else
  x = 2
end
y = x + 2     // 这里的 x 可能是 1,也可能是 2
```

那 `y = x + 2` 里的 `x` 可能来自两个不同的赋值点,编译器必须维护一个"定义可达集合",记录所有可能流到这里的赋值。变量被赋值次数越多、分支越复杂,这个集合越大、分析越慢越容易出错。

这就是普通代码的根本麻烦:**一个变量被反复赋值,导致变量的每一次使用,都对应一个模糊的"值"——它可能是好几次赋值中的任何一次。** 这种模糊性让数据流分析变得复杂。

### 2.2 一个釜底抽薪的办法:每个变量只赋值一次

既然"反复赋值"是麻烦的根源,那有没有办法消除它?有,而且办法极其直接:

> **规定:每一个变量,在整个程序里只能被赋值一次。**

这就是 SSA——Static Single Assignment,静态单赋值。"静态"是说这是一个关于程序文本(静态结构)的性质,"单赋值"是说每个变量只被赋值一次。

一旦每个变量只被赋值一次,前面那个麻烦就消失了。看上面那段代码,变成 SSA 后:

```
x1 = 1            // 原来的 x = 1,改名为 x1
... 一些代码 ...
y1 = x1 + 2       // 原来的 y = x + 2,这里的 x 明确是 x1
x2 = 10           // 原来的 x = 10,改名为 x2(注意:不是赋值给同一个 x,而是一个新名字)
... 一些代码 ...
z1 = x2 + 2       // 原来的 z = x + 2,这里的 x 明确是 x2
```

现在 `y1 = x1 + 2` 和 `z1 = x2 + 2` 一眼就能看出不是一回事(用的是不同的 x1、x2)。而如果两处碰巧用的是同一个 `x1`,编译器立刻就知道可以复用第一次的结果——不用再做任何"到达定义分析",因为 `x1` 这个名字在全文里只对应一个值,没有歧义。

这就是 SSA 的全部精髓。它不是什么高深的数学,就是一个**命名约定**:用"每次赋值都换一个新名字"的代价,换来了"变量名和值一一对应、毫无歧义"的好处。这个好处让几乎所有的数据流优化算法都大幅简化。

### 2.3 分支怎么办:PHI 节点

但上面的转换有个绕不开的难点:分支。回到那个 if/else 的例子:

```
if cond then
  x1 = 1
else
  x2 = 2
end
y = ??? + 2     // 这里的 x,合并自 x1 和 x2
```

SSA 要求 `y` 的操作数是一个确定的名字。但 `x1`(走 then 分支)和 `x2`(走 else 分支)是两个不同的名字,到底用哪个?这取决于 `cond` 在运行时是真是假,静态分析时不知道。

SSA 的解法是引入一个特殊的指令,叫 **PHI**(φ,希腊字母,读作"phi")。PHI 是一个"合并"指令,它显式地列出"从不同前驱流过来的值":

```
if cond then
  x1 = 1
else
  x2 = 2
end
x3 = PHI(x1, x2)   // 如果走 then 分支,x3 = x1;如果走 else 分支,x3 = x2
y = x3 + 2
```

`PHI(x1, x2)` 的语义是:运行时,根据实际走的是哪个分支,从 x1 和 x2 里选一个作为 x3 的值。这样 x3 还是一个"只赋值一次"的 SSA 变量(PHI 就是它的赋值点),而它显式地记录了"我的值可能来自 x1 或 x2"。

PHI 节点是 SSA 处理控制流合并的唯一机制。有了它,SSA 才能完整地表达任何带分支的程序。

但是!这里有个关键的认知需要提前建立,因为它直接关系到后面理解 LuaJIT:**PHI 只在"控制流合并"的地方才需要**。一条直线的代码,没有任何 if/else、没有任何分支汇合,就根本不需要 PHI——因为不存在"一个值可能来自多个前驱"的情况。

### 2.4 为什么这对 trace compiler 特别有利

现在把 §2.3 的结论和 trace compiler 的特性(P0-01 §10 讲过)联系起来,你会看到 SSA 和 trace 天生契合:

> **trace 是一条线性的、没有分叉的路径。** 录制时只录"实际在走的那一条路",所有其他分支都变成了 guard(在退出点处理,不进入 trace 主体)。

这意味着什么?**一条 trace 的主体,从头到尾就是一条直线。** 它没有 if/else 的合并点(分支被 guard 挡在了 trace 外面)。所以,trace 的 SSA 形式,在绝大多数情况下,根本不需要 PHI 节点!

录制器每发射一条 IR,就分配一个新的引用号(下一节细看),天然满足"每个引用只赋值一次"。不需要重命名、不需要插入 PHI——SSA 是录制过程的自然产物。这是 trace JIT 相对 method JIT 的一个巨大简化:method JIT 要处理整个方法的所有分支,需要做完整的 SSA 构造(包括 PHI 插入);trace JIT 只录一条线,SSA 几乎是白送的。

**唯一需要 PHI 的地方,是循环的回边。** 这一点 §7 会专门讲。先记住:LuaJIT 的 IR 几乎全程是"无 PHI 的 SSA",只在循环优化时才引入少量 PHI。这是它简单而高效的根本原因之一。

### 2.5 一个最小 trace 的 IR 长什么样

在钻进源码之前,我们先在纸上画一个最小 trace 的 IR,让你对它有个直观印象。用 P0-01 §12 那个 `for i = 1, 1000000 do x = x + i end` 的循环体为例(假设 x、i 都是整数),录制成 IR 大致是这样:

```
引用   指令            操作数              含义
----   ----            ------              ----
k0001  IR_KINT         值=0                常量 0(x 的初值)
k0002  IR_KINT         值=1                常量 1(步长)
...
0001   IR_SLOAD        slot=x              从栈槽加载 x(带类型检查 guard)
0002   IR_SLOAD        slot=i              从栈槽加载 i(带类型检查 guard)
0003   IR_ADD          op1=0001, op2=0002  x + i
0004   IR_CONV/窄化     ...                 (可能的类型转换,P2-08 讲)
0005   IR_SSTORE       slot=x, val=0003    存回 x 的栈槽
0006   IR_LT/IR_LE     ...                 循环边界检查(guard)
...
0007   IR_LOOP                             循环回边标记
```

(这是示意,真实 IR 会多一些 guard、snapshot 锚点,但骨架就是这样。)

观察几件事:

1. **每条指令都有一个唯一的引用号**(左列)。别的指令引用它,就用这个号。比如 `IR_ADD` 的 op1=0001,意思是"我的第一个操作数是引用 0001 那条 SLOAD 的结果"。
2. **引用号分两段**:常量(`KINT` 等)用小的号(k0001、k0002……),普通指令用大的号(0001、0002……)。两段从中间往两头长——这是 LuaJIR 的招牌设计,下一节细讲。
3. **操作数都是引用**:ADD 的两个操作数是另外两条指令的引用,不是直接的变量名。这就是 SSA 的体现——每个值都用产生它的那条指令来标识。
4. **没有变量名**:你看不到"x"、"i"这种变量名。变量名是源代码层面的概念;在 IR 层面,一切都是"某个指令产生的值"。栈槽编号(slot)只是用来和解释器的值栈对接(SLOAD 从哪个槽读、SSTORE 写到哪个槽),不是 SSA 意义上的"变量"。

这个表就是 IR 的全貌。现在我们去源码里,看它怎么把这个结构落地。

---

## §3 源码印证:IRIns——一条 IR 指令长什么样

IR 指令在 LuaJIT 里是 `IRIns` 类型(`lj_ir.h:556`)。这是整个 JIT 编译器最核心的数据结构之一——P3 优化改写它,P4 代码生成读它,snapshot 靠它定位值。我们把它逐字段拆开。

```c
typedef union IRIns {
  struct {
    LJ_ENDIAN_LOHI(
      IRRef1 op1;	/* IR operand 1. */
    , IRRef1 op2;	/* IR operand 2. */
    )
    IROpT ot;		/* IR opcode and type (overlaps t and o). */
    IRRef1 prev;	/* Previous ins in same chain (overlaps r and s). */
  };
  struct {
    IRRef2 op12;	/* IR operand 1 and 2 (overlaps op1 and op2). */
    LJ_ENDIAN_LOHI(
      IRType1 t;	/* IR type. */
    , IROp1 o;		/* IR opcode. */
    )
    LJ_ENDIAN_LOHI(
      uint8_t r;	/* Register allocation (overlaps prev). */
    , uint8_t s;	/* Spill slot allocation (overlaps prev). */
    )
  };
  int32_t i;		/* 32 bit signed integer literal (overlaps op12). */
  GCRef gcr;		/* GCobj constant (overlaps op12 or entire slot). */
  MRef ptr;		/* Pointer constant (overlaps op12 or entire slot). */
  TValue tv;		/* TValue constant (overlaps entire slot). */
} IRIns;
```

(`lj_ir.h:556`)

这是一个 **union(联合体)**——几个 struct 共享同一块 64 位(8 字节)内存。为什么用 union?因为同一条 IR 指令,在不同的阶段、不同的用途下,需要被当作不同的字段组合来看。union 让这些视图共用一块内存,既省空间又便于 reinterpret。

文件顶部的注释把内存布局画得很清楚(`lj_ir.h:542`):

```c
/* IR instruction format (64 bit).
**
**    16      16     8   8   8   8
** +-------+-------+---+---+---+---+
** |  op1  |  op2  | t | o | r | s |
** +-------+-------+---+---+---+---+
** |  op12/i/gco32 |   ot  | prev  | (alternative fields in union)
** +-------+-------+---+---+---+---+
** |  TValue/gco64                 | (2nd IR slot for 64 bit constants)
** +---------------+-------+-------+
**        32           16      16
*/
```

逐个字段讲清楚:

### 3.1 op1 / op2:两个操作数引用

最常用的视图是第一个 struct:`op1`、`op2` 是两个 16 位的**操作数引用**(类型 `IRRef1`,`lj_ir.h:458` 定义 `typedef uint16_t IRRef1`)。绝大多数 IR 指令最多有两个操作数,每个操作数是另一条 IR 指令的引用号。

比如 `IR_ADD` 是加法,它的 `op1` 和 `op2` 分别指向被加数和加数那两条指令的引用。又比如 `IR_LT`(小于比较)的 `op1 < op2`。这是 SSA 的直接体现:**值之间通过引用互相连接,形成一张有向无环图(DAG)**——每条指令的值,由它的操作数(其他指令的值)计算而来。

为什么操作数是引用而不是直接的值?因为 SSA 的核心就是"值由产生它的指令唯一标识"。你不存具体数值(那会重复、会失控),而是存"去引用 X 那条指令拿值"。这样数据依赖关系是显式的:看 op1、op2 就知道这条指令依赖谁。

`LJ_ENDIAN_LOHI(...)` 是处理大小端的宏,保证 op1 在前、op2 在后(在小端机器 x86/arm64 上,op1 占低 16 位)。这保证 `IRRef2 op12`(把 op1、op2 打包成一个 32 位)能正确工作——优化器经常需要把两个操作数当一个 32 位数来比较(比如 CSE 判断"两条指令操作数是否完全相同")。

### 3.2 ot:操作码+类型合体

`ot` 字段(类型 `IROpT`,`lj_ir.h:453` 是 `uint16_t`)把**操作码**和**类型**合在一个 16 位里。注释说它"overlaps t and o"——意思是另一个视图里的 `t`(类型,8 位)和 `o`(操作码,8 位)拼起来就是 `ot`。

为什么把操作码和类型合体?因为**LuaJIT 的每条 IR 指令都带一个类型**。这是个关键设计:IR 不只有"做什么操作"(加法、加载、比较),还有"结果是什么类型"(整数、浮点、字符串、表……)。类型紧跟指令,不单独存。这有几个好处:

- 后端代码生成时,一眼就知道该用整数指令还是浮点指令(`IRT_INT` 用整数 `ADD`,`IRT_NUM` 用浮点 `ADDSD`)。
- 优化 pass 判断"这两个值能不能复用"时,除了操作码相同,还要求类型相同(`IRT_INT` 的 ADD 和 `IRT_NUM` 的 ADD 不能合并)。
- guard 检查(下一节)直接靠类型标记:如果一条指令的类型是 `IRT_INT`,运行时就检查它是不是真的整数。

`IRType` 枚举定义了所有类型(`lj_ir.h:320` 的 `IRTDEF`),挑代表性的:`IRT_NIL`/`IRT_FALSE`/`IRT_TRUE`(基础值)、`IRT_STR`(字符串)、`IRT_TAB`(表)、`IRT_FUNC`(函数)、`IRT_NUM`(双精度浮点)、`IRT_INT`(32 位整数)、`IRT_I64`/`IRT_U64`(64 位整数)、`IRT_P32`/`IRT_P64`(指针)等等。注意:**Lua 语言的值只有 nil/bool/lightud/num/str/tab/func/udata 这几种(`lj_obj.h` 的 `LJ_T`),但 IR 的类型比这细**——它把"数"细分成 NUM/INT/I8/U8/I16/U16/I32/U32/I64/U64。这是因为 JIT 在优化时,需要精确知道一个数是 32 位整数还是 64 位浮点,才能生成对的机器指令。这个"把动态类型细化成机器类型"的过程,就是 P2-08 类型窄化的主题。

`ot` 除了 5 位类型(`IRT_TYPE = 0x1f`),高 8 位还有几个标志位(`lj_ir.h:344`):`IRT_MARK`(临时标记,优化用)、`IRT_ISPHI`(这条指令是 PHI 的操作数,循环优化用)、**`IRT_GUARD`(这条指令是 guard)**。`IRT_GUARD` 这个标志极其重要——它标记"这条指令带运行时检查,失败就退出"。比如一个带类型检查的 SLOAD,它的 `ot` 里 `IRT_GUARD` 位是 1,意思是"加载完还要检查类型对不对,不对就 side exit"。这是 P0-01 §7 讲的 guard 机制在 IR 层面的直接体现。

### 3.3 prev:同链前驱(CSE/优化的命脉)

`prev` 字段(16 位 `IRRef1`)指向"同一种操作码链上的前一条指令"。这是 LuaJIT IR 最精巧的设计之一。

讲清楚它干嘛的,得先讲 CSE(公共子表达式消除)怎么工作。假设 IR 里有两条 `IR_ADD`:

```
0003  IR_ADD  op1=0001, op2=0002
...
0007  IR_ADD  op1=0001, op2=0002   // 和 0003 完全一样
```

第二条是冗余的,可以消除(直接用 0003 的结果)。但编译器怎么发现这两条一样?它不可能两两比较所有指令(那是 O(n²))。LuaJIT 的办法是:**为每一种操作码维护一条链表**。所有 `IR_ADD` 指令,用 `prev` 字段串成一条链;链头存在 `J->chain[IR_ADD]`(`lj_jit.h:478`,`IRRef1 chain[IR__MAX]`,每种 opcode 一个链头)。

发射一条新的 `IR_ADD` 时(`lj_ir_emit`,`lj_ir.c:117`),做这件事:

```c
TRef LJ_FASTCALL lj_ir_emit(jit_State *J)
{
  IRRef ref = lj_ir_nextins(J);
  IRIns *ir = IR(ref);
  IROp op = fins->o;
  ir->prev = J->chain[op];     // 把自己挂到同 opcode 链的链头之前
  J->chain[op] = (IRRef1)ref;  // 自己成为新链头
  ir->o = op;
  ir->op1 = fins->op1;
  ir->op2 = fins->op2;
  J->guardemit.irt |= fins->t.irt;
  return TREF(ref, irt_t((ir->t = fins->t)));
}
```

(`lj_ir.c:117`)

`ir->prev = J->chain[op]` 这一行,把当前指令的 `prev` 指向之前的链头;`J->chain[op] = ref` 把自己设成新链头。于是所有同 opcode 的指令,通过 `prev` 串成一条链,最新的在链头。

CSE 时(P3-09 详讲),要查"有没有和这条 ADD 一样的旧指令",只要顺着 `IR_ADD` 链往前走,逐个比较操作数。因为同 opcode 的指令聚集在一条链上,查找很快(通常只比几条就找到或确认没有)。这是 `prev` 字段的核心用途——**给优化器提供 O(链长) 而不是 O(n) 的查找**。

**注意注释的关键一句**(`lj_ir.h:553`):"`prev is only valid prior to register allocation and then reused for r + s.`"——`prev` 只在**寄存器分配之前**有效。寄存器分配阶段(P4-13)开始后,`prev` 这块内存被**复用**成 `r`(分配的寄存器号)和 `s`(溢出槽号)。为什么能复用?因为到寄存器分配时,优化都做完了,同 opcode 链不再需要(不会再做 CSE),`prev` 的使命结束,这块 16 位正好可以用来存分配结果(寄存器号 8 位 + 溢出槽 8 位)。

这种"一个字段在生命周期不同阶段承担不同职责"的内存复用,是 LuaJIT 把 IRIns 压到 64 位的手段之一。代价是阅读源码时要时刻注意"现在处于哪个阶段,这个字段当前是什么含义"。

### 3.4 第二个 struct:寄存器分配阶段的视图

第二个 struct 视图(`op12` / `t` / `o` / `r` / `s`)是为寄存器分配准备的。`r` 和 `s` 就是上面说的复用——寄存器分配结果存这里。`op12` 是 op1 和 op2 打包,方便整体比较。`t` 和 `o` 是 `ot` 拆开看。

### 3.5 常量视图:i / gcr / ptr / tv

最后几个 union 成员(`i`、`gcr`、`ptr`、`tv`)是给**常量指令**用的。当一条 IR 是常量(比如 `IR_KINT` 表示整数常量 42),它的"值"直接存在指令里——这时用 `i`(32 位整数)、`gcr`(GC 对象指针,用于 `IR_KGC` 字符串/表常量)、`ptr`(普通指针,用于 `IR_KPTR`)、`tv`(完整的 TValue,用于 64 位常量 `IR_KNUM`/`IR_KINT64`,占**两个** IRIns 槽,见注释的"2nd IR slot")。

注意 `ir_knum` 宏(`lj_ir.h:592`):`#define ir_knum(ir) check_exp((ir)->o == IR_KNUM, &(ir)[1].tv)`——它取的是**下一条** IRIns 槽的 `tv`。这是因为 64 位浮点数放不进一个 64 位 IRIns(操作码和类型占了位),所以占两个连续槽:第一个槽是 KNUM 指令头,第二个槽存 64 位值。这是 IR 格式的一个细节。

到这里,`IRIns` 的所有字段讲完了。记住三件事就抓住了本质:

1. **op1/op2 是操作数引用**——SSA 的骨架,值通过引用互联。
2. **ot 合并了操作码和类型(含 guard 标志)**——一条指令同时说明"做什么"和"结果什么类型"。
3. **prev 是同 opcode 链前驱**——给 CSE 等优化提供快速查找,寄存器分配阶段被复用为 r/s。

---

## §4 源码印证:IRRef 与 REF_BIAS——双向增长的 SSA

理解了 `IRIns`,下一个要讲清楚的是**引用(IRRef)**——指令之间怎么编号、怎么互相引用。这是 LuaJIT IR 最巧妙的设计,也是 SSA 形式的核心机制。

### 4.1 引用是什么

`IRRef` 就是 IR 指令的编号(`lj_ir.h:460`,`typedef uint32_t IRRef`,实际存储用 16 位的 `IRRef1`)。每条 IR 指令在 `J->cur.ir` 数组里有一个位置,这个位置的下标(经过偏移)就是它的引用。别的指令要用这条指令的值,就在自己的 op1/op2 里填这个引用。

`GCtrace` 的 `ir` 字段(`lj_jit.h`,P0-01 §11 贴过)指向这个数组。`nins` 字段(`lj_jit.h:253`,`/* Next IR instruction. Biased with REF_BIAS. */`)是"下一条要分配的普通指令的引用号"。`nk` 字段(`lj_jit.h:259`,`/* Lowest IR constant. Biased with REF_BIAS. */`)是"最低的常量引用号"。

### 4.2 双向增长:常量往下,指令往上

关键来了。LuaJIT 的 IR 数组**从中间开始,往两头长**:

- **常量指令**(`IR_KINT`、`IR_KNUM`、`IR_KGC` 等)从中间往下长:每加一个常量,`nk` 减一,常量放在数组中越来越低的位置。
- **普通指令**(`IR_ADD`、`IR_SLOAD` 等)从中间往上长:每加一条,`nins` 加一,放在数组中越来越高的位置。

中间的基准点是 `REF_BIAS`(`lj_ir.h:464`):

```c
enum {
  REF_BIAS =	0x8000,
  REF_TRUE =	REF_BIAS-3,
  REF_FALSE =	REF_BIAS-2,
  REF_NIL =	REF_BIAS-1,	/* \--- Constants grow downwards. */
  REF_BASE =	REF_BIAS,	/* /--- IR grows upwards. */
  REF_FIRST =	REF_BIAS+1,
  REF_DROP =	0xffff
};
```

(`lj_ir.h:463`)

`REF_BIAS = 0x8000 = 32768`。`REF_BASE = 32768` 是普通指令的起点,普通指令从 32769(`REF_FIRST`)开始往上:32769、32770、32771……常量从 32767(`REF_NIL`)开始往下:32767、32766、32765……(`REF_NIL`/`REF_FALSE`/`REF_TRUE` 这三个固定的常量占 32765~32767,P0-01 §11 没细讲,这里补上:它们是预定义的 nil/false/true 常量,录制初始化时由 `lj_record_setup` 发射,见后文)。

### 4.3 为什么这么设计:一眼区分常量和普通指令

这个"双向增长"看起来古怪,但它解决了一个非常实际的问题:**怎么快速判断一个引用是常量还是普通指令?**

如果常量和普通指令混在一起(都从 0 开始连续编号),判断"引用 X 是不是常量"就得去查那条指令的操作码(是不是 `IR_KINT` 之类),要访问内存。而双向增长的设计里,常量的引用号**永远小于 `REF_BIAS`**,普通指令的引用号**永远大于等于 `REF_BASE`(=`REF_BIAS`)**。判断就变成了一条极其简单的比较(`lj_ir.h:485`):

```c
#define irref_isk(ref)	((ref) < REF_BIAS)
```

`irref_isk(ref)`——"这个引用是不是常量"——就是一个 `< REF_BIAS` 比较,不用访问内存,几条机器指令搞定。这在优化器里被调用几百万次,省下的时间相当可观。

源码注释(`lj_ir.h:473`)解释了这种设计带来的连锁便利:

```
/* Note: IRMlit operands must be < REF_BIAS, too!
** This allows for fast and uniform manipulation of all operands
** without looking up the operand mode in lj_ir_mode:
** - CSE calculates the maximum reference of two operands.
**   This must work with mixed reference/literal operands, too.
** - DCE marking only checks for operand >= REF_BIAS.
** - LOOP needs to substitute reference operands.
**   Constant references and literals must not be modified.
*/
```

翻译过来:这种设计让"字面量(literal,直接写的小整数,比如 slot 编号)"也能用和引用一样的 `< REF_BIAS` 编码,于是优化器可以**不查操作码表**就统一处理所有操作数——CSE 算两个操作数的最大引用、DCE 判断操作数是不是常量、循环优化做替换,全都靠"和 REF_BIAS 比较"这一个动作。统一、快速、无分支。

### 4.4 引用怎么分配:lj_ir_nextins 和 ir_nextk

普通指令的引用分配在 `lj_ir_nextins`(`lj_iropt.h:31`):

```c
static LJ_AINLINE IRRef lj_ir_nextins(jit_State *J)
{
  IRRef ref = J->cur.nins;
  if (LJ_UNLIKELY(ref >= J->irtoplim)) lj_ir_growtop(J);
  J->cur.nins = ref + 1;
  return ref;
}
```

返回当前的 `nins`(下一条可用引用),然后 `nins` 加一。如果快到缓冲区上限(`irtoplim`),调 `lj_ir_growtop` 扩容。这就实现了"往上长"。

常量的引用分配在 `ir_nextk`(`lj_ir.c:173`,static inline):

```c
static LJ_AINLINE IRRef ir_nextk(jit_State *J)
{
  IRRef ref = J->cur.nk;
  if (LJ_UNLIKELY(ref <= J->irbotlim)) lj_ir_growbot(J);
  J->cur.nk = --ref;
  return ref;
}
```

返回当前的 `nk`,然后 `nk` 减一。快到下限(`irbotlim`)就 `lj_ir_growbot` 扩容(`lj_ir.c:90`,逻辑稍复杂,因为往下长可能需要把整个数组往上挪或重新分配)。这就实现了"往下长"。

**这里有一个 SSA 性质的关键点**:无论是 `lj_ir_nextins` 还是 `ir_nextk`,**每分配一次就给出一个全新的、从未用过的引用号**。这意味着每条 IR 指令的引用号都是独一无二的——这就是"单赋值"在引用层面的落实。录制器从来不复用一个引用号给两条不同的指令(常量去重除外,见下)。

### 4.5 常量去重:同一常量只存一份

注意 `ir_nextk` 只在"没找到相同常量"时才调用。看 `lj_ir_kint`(`lj_ir.c:200`,发射整数常量):

```c
TRef LJ_FASTCALL lj_ir_kint(jit_State *J, int32_t k)
{
  IRIns *ir, *cir = J->cur.ir;
  IRRef ref;
  for (ref = J->chain[IR_KINT]; ref; ref = cir[ref].prev)
    if (cir[ref].i == k)
      goto found;
  ref = ir_nextk(J);
  ir = IR(ref);
  ir->i = k;
  ir->t.irt = IRT_INT;
  ir->o = IR_KINT;
  ir->prev = J->chain[IR_KINT];
  J->chain[IR_KINT] = (IRRef1)ref;
found:
  return TREF(ref, IRT_INT);
}
```

逻辑:先顺着 `IR_KINT` 链(`prev` 串起来的)找,看有没有值等于 k 的;有就复用它的引用(`goto found`),没有才分配新的。这叫**常量留驻(interning)**——同一个常量在整个 trace 里只存一份,所有用到它的指令都引用同一个引用号。

这和字符串在 Lua VM 里的留驻一模一样(同一个字符串字面量全局只有一份)。好处:省空间,而且比较两个常量引用是否相等就是比较引用号(整数比较),飞快。注意这**不违反 SSA**——一个常量本来就只有一个值,所有引用指向它是正确的。

### 4.6 录制初始化:固定引用的发射

`lj_record_setup`(`lj_record.c:2823`)在录制开始时,发射几个固定的引用,让后面的指令能引用到基础的常量和栈基:

```c
  /* Emit instructions for fixed references. Also triggers initial IR alloc. */
  emitir_raw(IRT(IR_BASE, IRT_PGC), J->parent, J->exitno);
  for (i = 0; i <= 2; i++) {
    IRIns *ir = IR(REF_NIL-i);
    ir->i = 0;
    ir->t.irt = (uint8_t)(IRT_NIL+i);
    ir->o = IR_KPRI;
    ir->prev = 0;
  }
  J->cur.nk = REF_TRUE;
```

(`lj_record.c:2851-2860`)

- `emitir_raw(IR_BASE, ...)` 发射 `IR_BASE`,它是"栈基址"的引用——后面所有 SLOAD/SSTORE 都用它作为基准指针的来源。`IR_BASE` 占引用 `REF_BASE`(=32768),是第一条普通指令。
- 然后手动填三个常量槽 `REF_NIL`(=32767)、`REF_FALSE`(=32766)、`REF_TRUE`(=32765),分别对应 nil/false/true 三个基础值,操作码 `IR_KPRI`。
- 最后 `J->cur.nk = REF_TRUE`(=32765),意思是"下一个常量从 32764 开始往下分配"。

这一段初始化把 IR 数组的"中间地带"占好:普通指令从 32768 起往上,常量从 32765 起往下。后面录制的每条指令都接在这之后。

到这里,IR 的存储结构讲完了。SSA 在存储层面的落实就是:**每条指令一个独一无二的引用号(双向编号),操作数是引用,值通过引用互联成 DAG**。下一节看 IR 指令集——这些引用都"指向什么类型的指令"。

---

## §5 源码印证:IR 指令集(IROp)与类型(IRT)

LuaJIT 的 IR 指令集定义在 `lj_ir.h:14` 的巨大宏 `IRDEF(_)`。它用一种 X-macro 技巧:把所有指令列成一张表,每条指令一行 `_(name, mode, m1, m2)`,然后通过不同的宏展开,一次性生成枚举(`IROp`)、模式表(`lj_ir_mode`)等。

这张表很长(近 100 条指令),但可以按类别分成几组(注释里的分组):

### 5.1 Guarded assertions(带 guard 的比较)

```c
  _(LT,		N , ref, ref) \
  _(GE,		N , ref, ref) \
  _(LE,		N , ref, ref) \
  _(GT,		N , ref, ref) \
  ...
  _(EQ,		C , ref, ref) \
  _(NE,		C , ref, ref) \
```

这些是比较指令:`LT`(小于)、`GE`(大于等于)、`LE`、`GT`、`EQ`(等于)、`NE`(不等于),以及无符号版本 `ULT`/`UGE`/`ULE`/`UGT`。它们通常带 `IRT_GUARD` 标志发射(用 `IRTG` 宏,`lj_ir.h:361`),意思是"比较失败就退出 trace"。这就是 guard 在 IR 里的样子——一条比较指令,带 guard 标志,生成机器码时会插运行时检查。

注意注释(`lj_ir.h:15`):"Must be properly aligned to flip opposites (^1) and (un)ordered (^4)."——这些比较指令的枚举值特意安排过,`LT^1 == GE`(小于的反面是大于等于)、`LT^4 == ULT`(有符号变无符号),靠异或位翻转。这让"取反比较"变成一条异或指令,极快。`lj_ir.h:163-167` 有一串 `LJ_STATIC_ASSERT` 保证这个性质。

### 5.2 Miscellaneous ops(杂项,含 SSA 控制)

```c
  _(NOP,	N , ___, ___) \
  _(BASE,	N , lit, lit) \
  _(PVAL,	N , lit, ___) \
  _(GCSTEP,	S , ___, ___) \
  _(HIOP,	S , ref, ref) \
  _(LOOP,	S , ___, ___) \
  _(USE,	S , ref, ___) \
  _(PHI,	S , ref, ref) \
  _(RENAME,	S , ref, lit) \
  _(PROF,	S , ___, ___) \
```

这里有几个 SSA 相关的关键指令:

- **`IR_LOOP`**:循环回边标记。录制到循环回边时发射它,把"录制阶段(pre-roll)"和"循环展开后(loop body)"分开(P3-12 详讲)。它是循环优化的锚点。
- **`IR_PHI`**:PHI 节点!两个操作数 `op1`、`op2` 是循环回边处要合并的两个值(§7 详讲)。`IR_PHI` 在 LuaJIT 里只在循环优化时出现。
- **`IR_USE`**:标记"这个值被使用",防止 DCE 把它当死代码消掉。某些指令(如 `ADDOV` 溢出加法)的结果如果不显式 USE,DCE 会误删。
- **`IR_RENAME`**:寄存器分配阶段用,给一个值在不同位置换个寄存器(避免冲突)。
- **`IR_BASE`**:栈基址,§4.6 讲过。
- **`IR_HIOP`**:处理 64 位操作在 32 位平台上的高半部分(拆分指令用,P3-12)。

### 5.3 Constants(常量)

```c
  _(KPRI,	N , ___, ___) \
  _(KINT,	N , cst, ___) \
  _(KGC,	N , cst, ___) \
  _(KPTR,	N , cst, ___) \
  _(KKPTR,	N , cst, ___) \
  _(KNULL,	N , cst, ___) \
  _(KNUM,	N , cst, ___) \
  _(KINT64,	N , cst, ___) \
  _(KSLOT,	N , ref, lit) \
```

各种常量:`KINT`(32 位整数)、`KNUM`(双精度浮点,占两槽)、`KGC`(GC 对象,字符串/表/函数)、`KPTR`/`KKPTR`(指针,KK 表示常量指针不可变)、`KNULL`(类型化 NULL)、`KSLOT`(带 slot 的常量,用于 HREFK 的键)。它们的操作数模式是 `cst`(常量字面量),值直接存在指令的 `i`/`gcr`/`ptr`/`tv` 字段里(§3.5)。

### 5.4 Arithmetic ops(算术)

```c
  /* Arithmetic ops. ORDER ARITH */
  _(ADD,	C , ref, ref) \
  _(SUB,	N , ref, ref) \
  _(MUL,	C , ref, ref) \
  _(DIV,	N , ref, ref) \
  _(MOD,	N , ref, ref) \
  _(POW,	N , ref, ref) \
  _(NEG,	N , ref, ref) \
  ...
  /* Overflow-checking arithmetic ops. */
  _(ADDOV,	CW, ref, ref) \
  _(SUBOV,	NW, ref, ref) \
  _(MULOV,	CW, ref, ref) \
```

算术:`ADD`/`SUB`/`MUL`/`DIV`/`MOD`(取模)/`POW`(乘方)/`NEG`(取负)。带 `C` 标志的(ADD/MUL)是**可交换**的(a+b == b+a),CSE 时可以交换操作数匹配。`ADDOV`/`SUBOV`/`MULOV` 是**溢出检查**的整数运算——整数加法可能溢出,溢出时 Lua 要回退到浮点,所以这几个指令带 guard:溢出就退出 trace。它们带 `W`(weak guard)标志。

这些算术指令的类型决定用哪种机器指令:`IRT_INT` 的 ADD 生成整数 `ADD`,`IRT_NUM` 的 ADD 生成浮点 `ADDSD`(P4-14 详讲)。**类型(在 ot 字段里)和操作码共同决定机器指令**——这是 LuaJIT IR 的核心设计。

### 5.5 Memory ops(内存:加载/存储)

这是最大的一组,分三小类:

**内存引用计算**(算地址):

```c
  _(AREF,	R , ref, ref) \     // 数组下标寻址:op1=表, op2=下标
  _(HREFK,	R , ref, ref) \     // hash 表常量键寻址
  _(HREF,	L , ref, ref) \     // hash 表通用键寻址
  _(NEWREF,	S , ref, ref) \    // 新增表项
  _(UREFO,	LW, ref, lit) \    // upvalue 引用(开放)
  _(UREFC,	LW, ref, lit) \    // upvalue 引用(关闭)
  _(FREF,	R , ref, lit) \     // 字段寻址(op2 是 IRFieldID)
  _(TMPREF,	S , ref, lit) \    // 临时引用
  _(STRREF,	N , ref, ref) \    // 字符串拼接引用
  _(LREF,	L , ___, ___) \     // 加载 L(lua_State)
```

**加载**:

```c
  _(ALOAD,	L , ref, ___) \     // 从数组元素加载
  _(HLOAD,	L , ref, ___) \     // 从 hash 节点加载
  _(ULOAD,	L , ref, ___) \     // 从 upvalue 加载
  _(FLOAD,	L , ref, lit) \     // 从对象字段加载(op2 是字段 ID)
  _(XLOAD,	L , ref, lit) \     // 任意地址加载(FFI 用)
  _(SLOAD,	L , lit, lit) \     // 从栈槽加载(op1 是 slot 号!)
  _(VLOAD,	L , ref, lit) \     // 变长加载
  _(ALEN,	L , ref, ref) \     // 取数组长度
```

**存储**:

```c
  _(ASTORE,	S , ref, ref) \    // 存到数组元素
  _(HSTORE,	S , ref, ref) \    // 存到 hash 节点
  _(USTORE,	S , ref, ref) \    // 存到 upvalue
  _(FSTORE,	S , ref, lit) \    // 存到对象字段
  _(XSTORE,	S , ref, ref) \    // 存到任意地址
```

注意几个要点:

- **`SLOAD` 最特殊**。它是"从解释器值栈的某个槽加载值",`op1` 不是引用而是 **literal(字面量)**——槽号。因为栈槽是静态位置,不是某个 IR 指令产生的值。录制器看到读局部变量,就发一条 `SLOAD`,op1 填槽号。`SLOAD` 的 op2 是一组标志(`IRSLOAD_*`,`lj_ir.h:233`):是否要类型检查(`IRSLOAD_TYPECHECK`)、是否要从父 trace 继承(`IRSLOAD_PARENT`)、是否要整数转浮点(`IRSLOAD_CONVERT`)等。这是 SLOAD 的语义所在——它把"解释器栈槽的动态值"引入 IR,并决定如何检查/转换它。
- **load 和 store 严格配对**。注释(`lj_ir.h:102`):"Loads and Stores. These must be in the same order."——`ALOAD`/`ASTORE`、`HLOAD`/`HSTORE` 等,枚举值相差一个固定常量 `IRDELTA_L2S`(`lj_ir.h:170`)。这样 load↔store 互转就是加/减一个常量,优化器(P3-10 内存优化)用这个性质做 load/store 转发。
- **FLOAD 的 op2 是字段 ID**(`IRFieldID`,`lj_ir.h:220`),如 `IRFL_TAB_ARRAY`(表的 array 字段)、`IRFL_STR_LEN`(字符串长度)。它从 GC 对象的固定字段加载,用于内联对象字段访问(比如 `#s` 取字符串长度,直接 FLOAD `IRFL_STR_LEN`,不用调函数)。

这一组内存指令是 LuaJIT 把动态的表/upvalue/栈访问"静态化"的核心——录制时观测到具体的访问模式,就用具体的 ALOAD/HLOAD/FLOAD 表达,而不是通用的"表查找"。这是它能生成高效机器码的关键之一。

### 5.6 Allocations / Buffer / Barriers / Conversions / Calls

剩下的几组简略带过(完整列表在 `lj_ir.h`):

- **分配**:`TNEW`(新建表)、`TDUP`(复制表)、`SNEW`(新建字符串)、`CNEW`(新建 cdata,FFI)。带 `A`(alloc)标志,表示有副作用(分配内存)。
- **barrier**:`TBAR`(表写屏障,通知 GC)、`OBAR`(对象屏障)、`XBAR`(全屏障)。GC 协作相关,P6 讲。
- **类型转换**:`CONV`(转换,op2 编码源/目标类型,如 int→num)、`TOBIT`(转成位运算用的整数)、`TOSTR`(转字符串)、`STRTO`(字符串转数)。CONV 是 P2-08 类型窄化的产物。
- **调用**:`CALLN`/`CALLA`/`CALLL`/`CALLS`/`CALLXS`(各种 C 函数调用,FFI 和内置函数用)。op1 是函数指针或 IRRef,op2 是调用 ID。

### 5.7 操作数模式(IRM_*):ref / lit / cst / none

每条指令的两个操作数都有**模式**(`IRMode`,`lj_ir.h:276`):`IRMref`(引用)、`IRMlit`(16 位字面量)、`IRMcst`(常量字面量)、`IRMnone`(无)。看 `IRDEF` 表每行的第 3、4 列。这个模式表存在 `lj_ir_mode[IR__MAX]`(`lj_ir.c:47`),优化器和代码生成靠它判断"这个操作数是引用还是字面量"。

模式里还有几个标志位(`lj_ir.h:285`):`IRM_C`(可交换)、`IRM_A`(分配)、`IRM_L`(加载)、`IRM_S`(存储/有副作用)、`IRM_W`(weak guard)。这些标志告诉优化器"这条指令能不能消除、能不能重排、有没有副作用"。比如 `ir_sideeff` 函数(`lj_ir.h:600`)用模式判断一条指令有没有副作用(存储和带 guard 的指令有副作用,不能被 DCE 删掉)。

指令集讲完了。现在回头看录制过程,看 SSA 是怎么自然形成的。

---

## §6 SSA 怎么从录制过程自然形成

前面 §2.4 说,trace 是线性的,所以 SSA 几乎是白送的。这一节用源码把这件事讲清楚:录制器每翻译一条字节码,发生了什么,SSA 性质是怎么保证的。

### 6.1 录制的入口:emitir 宏

录制器翻译字节码时,绝大多数情况通过 `emitir` 宏发射 IR。这个宏在录制器里定义(`lj_record.c:42`):

```c
#define emitir(ot, a, b)	(lj_ir_set(J, (ot), (a), (b)), lj_opt_fold(J))
```

它做两件事:

1. `lj_ir_set(J, ot, a, b)`:把要发射的指令(操作码+类型 `ot`、操作数 `a`、`b`)暂存到 `J->fold.ins`(`lj_iropt.h:20` 的 `lj_ir_set_` 就是写 `J->fold.ins.ot/op1/op2`)。注意,这里**还没真正分配引用、还没写进 IR 数组**——只是"准备好"。
2. `lj_opt_fold(J)`:把这条准备好的指令送进**优化 pipeline**(P3-09 常量折叠)。折叠器可能当场优化它(比如发现是 `x+0`,直接返回 x 的引用,不真正发射这条 ADD);如果没法优化,才真正调 `lj_ir_emit` 把它写进 IR 数组。

所以 `emitir` 不是简单的"追加一条指令",而是"追加一条指令**并立刻尝试优化**"。这是 LuaJIT 的一个重要设计:**优化是即时(on-the-fly)的,边录边优化**,而不是"录完一整条 trace 再统一优化"。这样做的好处是,录制结束时,IR 已经是部分优化过的(常量折叠、CSE 已做),后面的 P3 pass 可以基于更紧凑的 IR 工作。

还有一个 `emitir_raw`(`lj_record.c:45`):

```c
#define emitir_raw(ot, a, b)	(lj_ir_set(J, (ot), (a), (b)), lj_ir_emit(J))
```

它跳过优化 pipeline,直接发射(`lj_ir_emit`,`lj_ir.c:117`,§3.3 贴过)。用于那些不该被折叠的指令(比如 SLOAD、guard)。录制器根据指令类型选择用哪个。

### 6.2 分配引用=保证单赋值

看 `lj_ir_emit`(`lj_ir.c:117`)的核心两行:

```c
  IRRef ref = lj_ir_nextins(J);   // 拿一个新的、唯一的引用
  IRIns *ir = IR(ref);
  ...
  ir->op1 = fins->op1;
  ir->op2 = fins->op2;
  ...
  return TREF(ref, irt_t((ir->t = fins->t)));
```

`lj_ir_nextins`(`lj_iropt.h:31`,§4.4 贴过)返回 `J->cur.nins` 然后 `nins+1`。这意味着:**每一次 `lj_ir_emit`,都拿到一个全新的、之前从未分配过的引用号**。这个引用号就唯一标识了这条新发射的指令。

这就是 SSA 的"单赋值"在录制器里的落实。录制器**永远不**把同一个引用号赋给两条不同的指令——因为 `nins` 只增不减,每次分配都是新值。所以每条 IR 指令的引用,就是一个独一无二的"SSA 名字"。

返回值是 `TREF(ref, type)`(`lj_ir.h:502`)——把引用和类型打包成一个 32 位的 `TRef`(tagged ref,§4.2 讲过 16 位 ref + 8 位类型在高位)。`TRef` 是录制器内部传递"一个值的引用+类型"的载体。录制器把字节码的栈槽映射到 `TRef`(`J->slot[]` 数组,`lj_jit.h`,slot 编号→TRef),后续字节码读这个槽就拿到对应的 TRef(也就是 IR 引用)。

### 6.3 栈槽到 TRef 的映射:slot[]

讲清楚录制器怎么把"Lua 变量"变成"IR 引用"。Lua 是栈式字节码,局部变量存在值栈的槽里。录制器维护一个映射 `J->slot[]`(`lj_record.c:106` 等处用到):**slot 编号 → 当前对应的 TRef**。

- 读局部变量(字节码如 `BC_MOV` 等):录制器查 `J->slot[s]`,拿到这个槽当前对应的 TRef(一个 IR 引用)。后续操作就用这个引用。
- 写局部变量:录制器算出新值的 TRef(发射若干 IR 得到),然后**更新** `J->slot[s] = 新TRef`。

注意!这里 `J->slot[s]` 可以被**多次更新**——同一个栈槽,在不同字节码处,对应不同的 TRef。这看起来像"变量被多次赋值"。但这**不违反 SSA**!因为:

> **被多次赋值的是"栈槽编号"(一个外部位置),而不是"IR 引用"。每一个 TRef(每一个 IR 引用)一旦产生,就永不改变——它就是它那条指令的结果,不会再被赋成别的值。**

栈槽 s 在字节码层面被反复赋值,但每次赋值,录制器都产生一个**新的 IR 引用**(新 TRef),记录到 `slot[s]`。所以 IR 层面,每个引用还是单赋值的;只是"栈槽 → 引用"的映射在变。这是 trace 录制把"命令式的栈操作"翻译成"SSA 形式的 IR"的核心机制。

举个具体例子。字节码 `x = x + 1`(假设 x 在 slot 5):

1. 录制器看到读 x:查 `slot[5]`,假设当前是 TRef 引用 0001。
2. 发射 `IR_ADD op1=0001, op2=KINT(1)`,得到新引用,假设 0007。
3. 更新 `slot[5] = TREF(0007, INT)`。

之后再读 x,拿到的是引用 0007,不是 0001。引用 0001 永远是"最初的 x",引用 0007 永远是"x+1 的结果"——两者都是单赋值的,各是各。这种"栈槽是窗口,引用是真相"的设计,让命令式的 Lua 代码自然映射成 SSA 的 IR。

### 6.4 录制为什么天然不需要 PHI

结合 §6.3 和 §2.4:一条 trace 是线性的,录制器从头到尾顺序执行字节码,顺序发射 IR。没有任何"控制流合并"的点(分支被 guard 挡在外面),所以**录制过程中根本不会出现"一个值可能来自两个前驱"的情况**。因此,录制产出的 IR(在不考虑循环时)是完全无 PHI 的 SSA——这是 trace 形式相对 method 形式的巨大简化。

唯一打破线性的是循环回边:录制到回边,要把"循环体"和"下一轮循环"接起来,这时循环变量在回边前后的值需要合并。这就是 PHI 出现的唯一场景,下一节专门讲。

---

## §7 循环的 SSA:PHI 怎么产生和处理

这一节是本章最难的部分,因为 PHI 本身就难。我们放慢,用一个具体例子把 LuaJIT 怎么处理循环的 SSA 讲透。

### 7.1 为什么循环需要 PHI

回到 P0-01 §12 的循环:

```lua
for i = 1, 1000000 do
  x = x + i
end
```

录制时,录制器录了一圈循环体,IR 大致是(简化):

```
(回边前的一圈)
0001  SLOAD slot=x        → x 的当前值
0002  SLOAD slot=i        → i 的当前值
0003  ADD 0001, 0002      → x+i
0004  SSTORE slot=x, val=0003   → 存回 x
0005  ADD i, step         → i+step(下一个 i)
0006  SSTORE slot=i, val=0005   → 存回 i
0007  LT/LE ...           → 边界检查 guard
0008  LOOP                → 循环回边标记
```

现在问题来了:**这条 trace 是个循环,机器码跑完 0008 要跳回开头再跑一圈**。但第二圈开始时,x 和 i 的值,不是 0001/0002 录制时的初值了——它们是 0003(新 x)和 0005(新 i)!

如果不处理这个,第二圈跑机器码时会用错值:它以为 x 是 0001 的结果,但实际 x 已经是 0003 的结果了。数据依赖断了。

这就是循环 SSA 的核心难题:**循环变量在循环入口的值,第一次循环和后续循环来自不同的地方**。第一次来自循环外(初值),后续来自上一轮的回边(更新值)。SSA 必须在循环入口显式合并这两者,这就是 PHI。

### 7.2 LuaJIT 的解法:复制替换 + PHI 收集

LuaJIT 处理循环 SSA 的方法在 `lj_opt_loop.c`(`lj_opt_loop` 函数,`lj_opt_loop.c:415`,P2-05 §3 提到它在 `LJ_TRACE_END` 状态被调用)。它的核心思路不是传统的 PHI 插入,而是一种**复制-替换(copy-substitution)结合冗余消除**的方法。文件开头那段长注释(`lj_opt_loop.c:22-90`)把动机讲得非常清楚,值得细读。

注释的核心论点(P2-05 §3 也提过):

> **传统的循环不变量外提(LICM)对动态语言基本没用**——因为 IR 里全是 guard,大部分指令控制依赖于这些 guard,第一个不能外提的 guard 就把后面所有指令都拖住了。所以 LuaJIT 不做 LICM,而是**把录制的指令流复制一遍,用替换表替换操作数,重新喂给优化 pipeline**。这等价于"展开两圈",但第二圈在折叠/CSE 后大部分消失,只剩循环体真正变化的指令。

具体看 `loop_unroll`(`lj_opt_loop.c:265`)。它做的事:

**第一步:发射 LOOP 标记,准备替换表。**

```c
  invar = J->cur.nins;                  // 录制段的最后一条指令的下一个
  lps->sizesubst = invar - REF_BIAS;
  lps->subst = lj_mem_newvec(J->L, lps->sizesubst, IRRef1);
  subst = lps->subst - REF_BIAS;
  subst[REF_BASE] = REF_BASE;           // BASE 映射到自身

  emitir_raw(IRTG(IR_LOOP, IRT_NIL), 0, 0);   // 发射 IR_LOOP
```

(`lj_opt_loop.c:279-286`)

`invar` 记录"录制段"的边界(回边前录了哪些指令)。`subst` 是替换表:`subst[旧引用] = 新引用`。初始化时 `subst[REF_BASE]=REF_BASE`(栈基不变)。

**第二步:逐条复制录制段的指令,替换操作数。**

```c
  for (ins = REF_FIRST; ins < invar; ins++) {
    IRIns *ir = IR(ins);
    IRRef op1, op2;
    ...
    /* Substitute instruction operands. */
    op1 = ir->op1;
    if (!irref_isk(op1)) op1 = subst[op1];   // 替换操作数 1
    op2 = ir->op2;
    if (!irref_isk(op2)) op2 = subst[op2];   // 替换操作数 2
    if (irm_kind(lj_ir_mode[ir->o]) == IRM_N &&
        op1 == ir->op1 && op2 == ir->op2) {  /* Regular invariant ins? */
      subst[ins] = (IRRef1)ins;  /* Shortcut. */
    } else {
      /* Re-emit substituted instruction to the FOLD/CSE/etc. pipeline. */
      IRType1 t = ir->t;
      IRRef ref = tref_ref(emitir(ir->ot & ~IRT_ISPHI, op1, op2));
      subst[ins] = (IRRef1)ref;
      ...
    }
  }
```

(`lj_opt_loop.c:311-373`,有删节)

对录制段的每一条指令:

- 用 `subst[]` 替换它的两个操作数(把旧引用换成"复制后"的新引用)。
- 如果替换后操作数没变(说明这条指令的操作数都是循环不变的),`subst[ins] = ins`——直接指向原指令,不复制(这就是不变量外提的效果:不变的指令不进循环体)。
- 如果操作数变了,重新 `emitir` 这条指令(用替换后的操作数),它会经过 FOLD/CSE pipeline。复制后的指令拿到一个新引用 `ref`,`subst[ins] = ref`。

**关键:重新 emit 时,操作数指向"复制后的新指令"。这就是把"回边后的值"接上"回边前的计算"。**

举个简化例子:录制段有 `0003: ADD 0001, 0002`。复制时:

- 替换 op1:0001 在复制段对应某条新指令 `0001'`,`subst[0001]=0001'`。
- 替换 op2:同理 `subst[0002]=0002'`。
- 如果 op1/op2 都变了,重新 emit:`0009: ADD 0001', 0002'`。`subst[0003]=0009`。

这样,复制段的 ADD 用的是复制段的操作数,数据依赖在复制段内自洽。经过 CSE,复制段里很多指令会和原段合并(相同操作的指令只留一份),最后只剩真正随循环变化的指令形成循环体。

**第三步:收集 PHI 候选。**

在复制过程中,如果发现一条指令的复制后引用 `ref` **小于 `invar`**(也就是 ref 在录制段范围内),说明这条指令的值在循环里被"回引"了——循环体的某个值依赖录制段的某个值,而录制段的这个值在循环里会被更新。这就是潜在的循环携带依赖(loop-carried dependency),需要 PHI 合并。

收集逻辑(`lj_opt_loop.c:332-340`):

```c
      if (ref != ins) {
        IRIns *irr = IR(ref);
        if (ref < invar) {  /* Loop-carried dependency? */
          /* Potential PHI? */
          if (!irref_isk(ref) && !irt_isphi(irr->t) && !irt_ispri(irr->t)) {
            irt_setphi(irr->t);                 // 标记这个 ref 是 PHI 操作数
            if (nphi >= LJ_MAX_PHI)
              lj_trace_err(J, LJ_TRERR_PHIOV);  // PHI 太多,放弃
            phi[nphi++] = (IRRef1)ref;
          }
          ...
```

被标记为 PHI 候选的引用,在它的 `t.irt` 里设 `IRT_ISPHI` 位(`lj_ir.h:345`)。这些是"循环入口需要 PHI 合并"的值。

**第四步:发射真正的 PHI 指令。**

复制循环结束后,调 `loop_emit_phi`(`lj_opt_loop.c:104`):

```c
static void loop_emit_phi(jit_State *J, IRRef1 *subst, IRRef1 *phi, IRRef nphi,
                          SnapNo onsnap)
{
  ...
  /* Pass #1: mark redundant and potentially redundant PHIs. */
  for (i = 0, j = 0; i < nphi; i++) {
    IRRef lref = phi[i];
    IRRef rref = subst[lref];
    if (lref == rref || rref == REF_DROP) {  /* Invariants are redundant. */
      irt_clearphi(IR(lref)->t);             // 不变量,不需要 PHI
    } else {
      phi[j++] = (IRRef1)lref;
      ...
```

(`lj_opt_loop.c:104-120`)

它做几趟扫描,去掉冗余的 PHI 候选(那些复制后引用和原引用相同的——说明是循环不变量,不需要合并),剩下的才真正发射 `IR_PHI` 指令。

`IR_PHI` 的形式(`lj_ir.h:41`):`_(PHI, S, ref, ref)`——两个操作数 `op1`、`op2`。语义:`op1` 是循环入口的旧值(来自循环外/上一轮),`op2` 是循环回边后的新值(来自这一轮的更新)。运行时(机器码层面),PHI 表示"第一次循环用 op1,后续循环用 op2"——后端(P4)会把这个翻译成"在循环外把 op1 移到某个寄存器,循环体内每轮用 op2 更新这个寄存器"。PHI 在这里不生成实际计算,只是告诉后端"这两个值要合并到同一个位置"。

回到 x+i 的例子,PHI 大致长这样(简化):

```
(循环外/pre-roll)
0001  SLOAD slot=x              → x 初值
...
0003  ADD 0001, 0002            → x+i
0004  SSTORE slot=x, 0003
...
0008  LOOP
(循环体,复制后经 CSE 收缩)
0009  PHI op1=0001, op2=0003    → 合并:x 第一圈是 0001,后续是 0003
0010  ADD 0009, ...             → 用 PHI 的结果
...
```

(实际 PHI 的两个操作数是具体的"前一轮值"和"本轮值",这里示意。)有了 PHI,循环体的 ADD 用 0009(PHI 结果),既覆盖第一圈(用 0001)又覆盖后续(用 0003),数据依赖闭合。

### 7.3 类型不稳定:PHI 的另一种失败

PHI 还有另一个难点:类型。如果同一个循环变量,在第一圈是整数、第二圈变成浮点(PHI 的两个操作数类型不同),怎么办?

`loop_unroll` 里有专门处理(`lj_opt_loop.c:343-356`):

```c
          /* Check all loop-carried dependencies for type instability. */
          if (!irt_sametype(t, irr->t)) {
            if (irt_isinteger(t) && irt_isinteger(irr->t))
              continue;
            else if (irt_isnum(t) && irt_isinteger(irr->t))  /* Fix int->num. */
              ref = tref_ref(emitir(IRTN(IR_CONV), ref, IRCONV_NUM_INT));
            else if (irt_isnum(irr->t) && irt_isinteger(t))  /* Fix num->int. */
              ref = tref_ref(emitir(IRTGI(IR_CONV), ref,
                                    IRCONV_INT_NUM|IRCONV_CHECK));
            else
              lj_trace_err(J, LJ_TRERR_TYPEINS);   // 类型不稳定,放弃
```

如果两边都是整数,OK(continue)。如果一边整数一边浮点,LuaJIT 会尝试插一个 `IR_CONV` 把它们统一(整数→浮点,或浮点→整数带检查)。如果类型差太远(比如一边是表一边是数),直接报 `LJ_TRERR_TYPEINS`(类型不稳定)。

报 `LJ_TRERR_TYPEINS` 时,P2-05 §2 讲过,状态机会**退回 RECORD 状态**(`J->instunroll` 计数,多展开几圈试试),试图通过多录几圈消除类型不稳定。如果展开次数用完(`--J->instunroll < 0`),才真正放弃这条 trace。这是 trace JIT 的容错——循环类型不稳定不等于整条 trace 作废,先试多展开。

`LJ_MAX_PHI`(`lj_def.h:86`)是 64——一条循环 trace 最多 64 个 PHI。超过就报 `LJ_TRERR_PHIOV`(PHI 溢出),放弃。这是"省"的体现:PHI 太多的循环优化代价太大,不值得。

### 7.4 这个方法的精妙之处

LuaJIT 的循环 SSA 处理,和教科书式的 SSA 构造(先建控制流图、插 PHI、再重命名)完全不同。它的做法是:**不显式构造控制流图,而是利用 trace 的线性 + 复制替换,让 SSA 和优化同时完成。** 文件注释(`lj_opt_loop.c:62-75`)总结了几条好处:

1. **控制依赖隐式保留**。复制段保留了录制段的所有 guard,循环体的 guard 通过 CSE 和录制段的 guard 合并,自然保证"循环体的指令只在 guard 成立时执行"。不用显式建模控制依赖。
2. **所有优化 pipeline 复用**。复制段重新喂给 FOLD/CSE/FWD,这些优化原本就写好了,循环优化白嫖它们。只需小限制(不在循环携带依赖间折叠)。
3. **snapshot 自然集成**。snapshot 也被复制替换,和 IR 同步。

这种"复制替换 + pipeline 复用 + 收集 PHI"的方法,是 LuaJIT 在 trace 框架下处理循环的独门技巧。它简单(不到 400 行 C)、高效(优化即时)、正确(SSA 性质由复制替换保证)。对比 method JIT 要做的完整 SSA 构造(动辄几千行),这是 trace 路线的又一次"以形式换简单"。

---

## §8 为什么 sound:SSA 保证优化不会算错依赖

讲完了 IR 结构、引用机制、指令集、SSA 形成、循环 PHI。这一节回答"为什么这样设计是 sound 的"——也就是,为什么在 SSA 形式的 IR 上做优化,不会改变程序的语义(算错结果)。

### 8.1 单赋值让数据依赖无歧义

这是最根本的一条。在 SSA 里,每个引用唯一对应一个值、一条产生它的指令。一条指令的操作数(op1、op2)是引用,**引用就是数据依赖**——op1 指向哪条指令,这条指令就直接依赖那条指令的结果,没有任何歧义。

这让优化的正确性论证极其简单。以 CSE 为例:两条 `IR_ADD op1=A, op2=B`(A、B 是引用)。在 SSA 里,只要 A、B 完全相同(同一个引用),这两条 ADD 的输入就完全相同,加法是确定性的,输出必然相同——所以可以安全地消除一条,用另一条的结果。这个论证不依赖任何"到达定义分析"、不依赖控制流——只依赖"引用相同 ⇔ 值相同"这个 SSA 不变式。

如果是非 SSA(变量被多次赋值),两条 `ADD x, y` 里 x、y 可能指向不同的赋值,CSE 就可能误消除,算错。SSA 从根本上杜绝了这种错误。

### 8.2 副作用靠指令模式标记

有些指令有副作用(存储、分配、调用),不能被 CSE 消除,即使操作数相同。比如两条 `ASTORE op1=同表, op2=同值`——第一条存完了,第二条还得存(可能有观察者,或者顺序敏感)。

LuaJIT 用指令模式(`IRM_S` 标志,§5.7)标记副作用。`ir_sideeff`(`lj_ir.h:600`):

```c
static LJ_AINLINE int ir_sideeff(IRIns *ir)
{
  return (((ir->t.irt | ~IRT_GUARD) & lj_ir_mode[ir->o]) >= IRM_S);
}
```

DCE(死代码消除,P3-11)只消除"没有副作用且结果没人用"的指令。有 `IRM_S`(store/alloc)或带 `IRT_GUARD` 的指令,都被视为有副作用,不会被删。这保证优化不会"删掉一条该执行的存储"。

guard 算副作用也很关键——guard 失败要退出 trace,不能被优化掉(否则假设失效就不被发现了,违反 P0-01 §9 的不变式)。所以带 `IRT_GUARD` 的指令,DCE 不碰。

### 8.3 PHI 正确合并循环

循环的 sound 性靠 PHI。§7 讲了 PHI 的两个操作数分别对应"循环入口值"和"回边值"。只要:

1. PHI 收集阶段正确识别了所有循环携带依赖(每一条循环体内被回引的录制段指令都标了 PHI);
2. 后端正确地把 PHI 翻译成"循环外初始化 + 循环内更新"的寄存器使用;

那么循环的每一轮,用的值都和"不优化、老老实实每轮重新算"完全一致。`loop_unroll` 通过"复制段重新过 pipeline"保证:复制段的每条指令都经过完整的 FOLD/CSE,它的语义和原录制段相同(只是操作数替换了),所以 PHI 收集到的 ref 都是真实的数据流。

类型不稳定的情况(§7.3),LuaJIT 要么插 CONV 强制统一(语义不变,只是多一次转换),要么报错退回(不冒险)。它不会生成"类型不对的 PHI"——那会导致机器码用错指令(整数指令处理浮点),算错。

### 8.4 优化失败的兜底:退回录制或放弃

即使 SSA 保证了单条优化的正确性,循环优化整体仍可能失败(PHI 太多、类型不稳定)。这时 P2-05 §2 讲的机制兜底:退回 RECORD 多展开,或彻底报错走 ERR 作废 trace。**失败时,这条 trace 根本不会被装上去**,解释器继续跑,结果完全正确。所以即使优化阶段"没做成",也不会"做错"——这是 trace JIT 失败安全的体现。

综上,SSA + 副作用标记 + 正确的 PHI + 失败兜底,共同保证了"在 IR 上做的所有优化,要么成功且语义不变,要么失败且不影响结果"。这就是 LuaJIT 优化 sound 的根基。

---

## §9 ★对照:官方 Lua 与 JVM/V8

把 LuaJIT 的 IR/SSA 和两个对象对照,取舍会更清晰。

### 9.1 对照一:官方 Lua(切"有没有中间层")

官方 Lua **完全没有 IR 这个概念**。它是纯解释器,字节码直接被解释器逐条执行,没有任何中间表示,没有任何优化 pass。

这意味着官方 Lua 里:

- **没有 SSA**。字节码层面的变量就是栈槽,被反复赋值,也没有引用、没有数据流图。解释器每条字节码都老老实实执行,不分析依赖、不消除冗余。
- **没有常量折叠**。`x + 0` 在官方 Lua 里永远老老实实做一次加法,即使加 0。
- **没有 CSE**。同一个表达式算两遍,就老老实实算两遍。
- **没有任何循环优化**。循环不变量不会外提,每一轮都重新算。

所以官方 Lua 慢,不只是慢在"每条字节码要解释",还慢在"它根本不做任何优化"——同样的计算,它可能比 JIT 多算很多遍。这是"没有 IR"的直接代价:没有中间层,就没有优化的舞台。

LuaJIT 引入 IR,本质上就是**为优化搭了一个舞台**。IR 把字节码的命令式执行,翻译成 SSA 的数据流图,让常量折叠、CSE、循环不变量外提这些静态优化成为可能。这是 JIT 相对解释器,除了"生成机器码"之外的另一大加速来源。

### 9.2 对照二:JVM/V8(切"Sea-of-Nodes vs 线性 SSA")

JVM(HotSpot 的 C2)和 V8(TurboFan)的 IR 也用 SSA,但它们的 SSA 形式和 LuaJIT 有根本不同。这一对照非常有讲头,因为它体现了 trace 和 method 两大流派的分歧。

**JVM C2 的 IR:Sea-of-Nodes(节点之海)**

HotSpot C2 的 IR 叫 **Sea-of-Nodes**,一种**图状 SSA**。它的特点:

- 每个值是一个**节点(node)**,节点之间用**边(edge)**连接,表示数据依赖和控制依赖。整个方法的 IR 是一张稠密的有向图。
- 控制流(基本块、分支)也显式建模成控制节点(control nodes),数据节点挂在控制节点上,表示"这个值在这个控制路径下有效"。
- 优化在图上做:GCM(全局代码移动)把节点在图上挪动(把不变量挪到循环外)、死代码消除删孤立节点。

Sea-of-Nodes 的好处是表达力强(能精确建模任何控制流),代价是构造和维护这张图很复杂(C2 的 IR 构造代码量巨大),优化 pass 需要遍历图、维护不变量。

**V8 TurboFan 的 IR:TurboFan 的 Sea-of-Nodes 变体**

V8 的 TurboFan 也用类似 Sea-of-Nodes 的图状 SSA(叫 TurboFan IR / sea-of-nodes),节点带类型(type feedback),优化在图上做。和 C2 思路一脉相承。

**LuaJIT 的 IR:线性 SSA(linear SSA)**

LuaJIT 的 IR 完全不同:

- IR 是一个**线性数组**(`J->cur.ir`),指令顺序排列,用引用(下标)互联。
- **不显式建模控制流**。trace 是线性的,没有分支(分支被 guard 挡在外面),所以不需要基本块、不需要控制节点。整条 trace 就是一个长长的指令序列,中间偶尔插一个 `IR_LOOP` 标记循环。
- 数据依赖靠 op1/op2 的引用,形成隐式的 DAG(因为是 SSA,无环)。
- 优化在数组上做:顺着同 opcode 链(prev 字段)找 CSE、常量折叠直接改写指令。不需要遍历图。

这种"线性 + 引用"的设计,比 Sea-of-Nodes 简单一个量级。LuaJIT 的 IR 相关代码(lj_ir.c 不到 500 行)相比 C2 的 IR 构造(几万行),是两个世界。

**为什么 LuaJIT 能这么简单?因为它只录一条 trace。** trace 的线性保证了"不需要建模控制流"——没有 if/else 合并、没有多前驱基本块,自然不需要图。而 method JIT 要处理整个方法的所有分支,必须用图来表达控制流,所以不得不用 Sea-of-Nodes 这种复杂结构。

这是 trace vs method 在 IR 层面的根本分歧:

| 维度 | LuaJIT(线性 SSA) | JVM C2 / V8 TurboFan(Sea-of-Nodes) |
|---|---|---|
| IR 形式 | 线性数组 + 引用 | 图(节点+边) |
| 控制流建模 | 不显式(trace 线性,分支靠 guard) | 显式(基本块、控制节点) |
| PHI 出现 | 只在循环回边(method 内无分支合并) | 凡有控制流合并处都有 |
| 优化遍历 | 顺同 opcode 链(prev) | 遍历图 |
| 复杂度 | 极低(~500 行 IR 代码) | 极高(几万行) |
| 表达力 | 单条线性路径 | 任意控制流 |
| 代价 | 一条路径外的分支要 side trace 单独录 | 整个方法一次编译 |

LuaJIT 选线性 SSA,换来的是极简的 IR 结构、飞快的编译、即时优化。代价是:每条 trace 只覆盖一条路径,一个方法的多条路径要分别录多条 trace(side trace 树,P5 讲)。method JIT 选 Sea-of-Nodes,一次编译覆盖整个方法,但编译慢、IR 复杂。

**PHI 密度也天差地别。** LuaJIT 的 PHI 只在循环回边出现,一条 trace 通常只有几个到十几个 PHI(`LJ_MAX_PHI`=64 封顶)。C2/V8 的 PHI 在每个分支合并点都有,一个方法可能几百上千个 PHI。PHI 少,后端寄存器分配(P4-13)就简单——这是 LuaJIT 线性扫描能搞定的原因之一,而 C2 要用更复杂的图着色。

### 9.3 对照小结

把两个对照合起来看:

- **vs 官方 Lua**:LuaJIT 引入 IR/SSA,是为优化搭舞台。官方 Lua 没有这一层,所以不做任何静态优化,同样的计算可能反复做。
- **vs JVM/V8**:LuaJIT 的线性 SSA 是 trace 形式的自然产物(线性→不需要图);JVM/V8 的 Sea-of-Nodes 是 method 形式的必然选择(多分支→必须用图)。线性 SSA 简单快,但只覆盖一条路径;Sea-of-Nodes 复杂慢,但覆盖整个方法。

IR 的形式,不是随便选的,它由"编译单位是 trace 还是 method"这个根本选择决定。LuaJIT 选 trace,于是有了线性 SSA 这个极简而强大的 IR——这是它能"小而快"的根基之一。

---

## §10 回扣主线

回到本书的主线:**把动态执行安全变成机器码。**

这一章讲的是这条主线在 **JIT 侧的数据根基**。IR/SSA 是 trace 录制的产出物,又是优化和代码生成的输入——它是整个 JIT 编译流水线的**中间枢纽**。

- **"变成机器码"** 体现在:IR 是字节码和机器码之间的中间层。字节码先翻成 IR,在 IR 上优化,优化后的 IR 再翻成机器码。多一层抽象,换来强大的优化能力。
- **"安全"** 体现在:SSA 的单赋值性质,让每条指令的数据依赖无歧义,优化不会"算错依赖"。guard 标志(`IRT_GUARD`)让带运行时检查的指令不被误删。PHI 正确合并循环,保证循环优化的语义不变。优化失败时退回录制或作废 trace,绝不生成"语义错误"的机器码。
- **"动态"** 体现在:IR 的类型(ot 字段里的 IRT_*)是录制时观测到的实际类型——动态语言运行时才有的类型信息,被"焊"进了 IR。窄化(P2-08)进一步把这个类型精确化。IR 不是静态分析出来的,是运行时录下来的。

LuaJIT 的 IR 还有一个贯穿全书的主题:**以形式换简单**。trace 的线性,让它不需要 Sea-of-Nodes 那种复杂图状 IR,线性 SSA 就够;线性 SSA 让它的 PHI 极少,后端能用简单的线性扫描;即时优化(emitir 直接进 FOLD pipeline)让它不用分阶段跑优化 pass。每一个简化,都源自"只录一条线性热路径"这个根本选择。

讲到这里,你已经知道 IR 是什么、它为什么是 SSA、引用怎么编号、指令有哪些、循环怎么处理。下一章 P2-08,我们看录制器在发射 IR 时,怎么把 Lua 的动态类型(只有"数")**窄化**成 IR 的精确类型(整数/浮点)——这是 IR 类型字段的来源,也是 LuaJIT 能生成高效整数/浮点机器指令的前提。

---

*下一章 [P2-08 类型窄化 narrowing](P2-08-类型窄化narrowing.md):Lua 的"数"是怎么被窄化成 IR 的 IRT_INT / IRT_NUM 的?窄化什么时候保守、什么时候激进?它和 guard 怎么协作,保证窄化错了能被发现?*
