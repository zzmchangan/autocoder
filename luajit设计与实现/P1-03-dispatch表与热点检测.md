# P1-03 dispatch 表与热点检测

> **本书主线**:把动态执行安全变成机器码。**二分法**:解释器侧 ↔ JIT 侧——本章在**解释器侧**,承接 P1-02 的字节码与手写汇编 VM。**★对照**:官方 Lua(没有 JIT,也就没有热点检测)+ JVM/V8(method JIT 怎么发现热点)。**源码**:LuaJIT 2.1.ROLLING。**基调**:纯直球，不用比喻；从第一性原理一步步推导。
>
> **本章落点**:解释器与 JIT 之间的那道"开关"。讲清 LuaJIT 怎么在几乎零额外开销的情况下，一边飞速解释执行，一边数热点、在合适的时机触发编译。

---

## 引子:P1-02 留下的一个"怎么找"的问题

P1-02 我们看清了 LuaJIT 的字节码长什么样(32 位一条，操作码 + 操作数),也看清了执行这些字节码的 VM 是手写汇编。但有一个最基本的问题，当时为了讲清字节码格式而暂时搁下了:

> 解释器拿到一条字节码，怎么找到"执行这条字节码的代码"?

这句话听起来平淡，但它其实是整个解释器性能的命脉。因为解释器一辈子就在干一件事——反反复复"取一条字节码、找到它对应的执行代码、跳过去执行、再取下一条"。这个"找到并跳过去"的动作，叫**分发(dispatch)**。每条字节码都要分发一次，一个跑一百万次的循环就要分发一百万次。分发的效率，几乎就等于解释器的效率。

而本章还要回答一个更刁钻的问题:在这个已经快到极致的分发循环里,**怎么塞进去一个"数热点"的动作，又不让它变慢?** 因为 P0-01 §6 讲过，触发 JIT 的前提是发现热点——你得一边解释，一边数"这段循环跑了多少次"。可如果你为了数这个数，让每次解释都慢下来，那 JIT 还没开始，解释器就已经被拖垮了。

这两个问题——"怎么快速找到执行代码"和"怎么顺带数热点又不拖慢"——答案都在 LuaJIT 的一张表里:dispatch 表。这张表和它旁边紧挨着的 hotcount 数组，是 LuaJIT 解释器与 JIT 引擎之间的桥梁。这一章，我们就把它从零推导出来，再逐行落到源码上。

---

## §1 第一性原理:解释器怎么从一条字节码找到执行代码

### §1.1 最朴素的做法:一个大 switch

假设你来写解释器。字节码一条条摆在内存里，你维护一个程序计数器 PC(program counter),指向当前要执行的那条字节码。最直觉的写法是:

```c
for (;;) {
  uint8_t op = *pc;          // 取操作码
  switch (op) {              // 根据操作码跳到对应的执行代码
    case OP_ADD:  ...; break;
    case OP_SUB:  ...; break;
    case OP_CALL: ...; break;
    ...
    case OP_RETURN: return;
  }
}
```

这就是 C 写的解释器最常见的形态——一个巨大的 switch,每个 case 对应一种字节码操作码。绝大多数官方 Lua、CPython 的早期版本，都是这么做的。

它对不对?对。它快不快?这就值得仔细想了。

### §1.2 switch 为什么慢:分支预测的灾难

`switch(op)` 在 CPU 眼里是什么?是一条"条件跳转指令"。CPU 执行到这条跳转时，它并不知道下一次该跳到哪个 case——因为 `op` 的值要等到运行时才知道(下一条字节码是什么操作码，取决于程序具体在跑什么)。CPU 只能猜。

现代 CPU 靠**分支预测器**来猜跳转去向，猜对了流水线不停，猜错了要把流水线里已经预取的指令全部冲掉(流水线冲刷，几十个时钟周期的代价)。问题在于，解释器执行的字节码序列，操作码是高度多变的——这条是 ADD,下条是 CALL,再下条可能是 MOV。对分支预测器来说，这种"几乎每次都跳到不同地方"的跳转，是**最难猜**的一类:预测准确率常常低到 50% 出头，几乎等于瞎猜。每一次猜错，都是一次流水线冲刷。

更糟的是，很多 C 编译器会把一个大 switch 编译成一棵二分查找树(为了在 case 数量多时平衡),或者一张跳转表——但无论哪种，都要先做一次比较或一次内存读，才能确定跳转目标,**目标地址本身也是动态的**,分支预测器照样头疼。

所以 switch 解释器的核心瓶颈不是"算术慢",而是**这个决定下一条指令去哪的跳转，几乎每次都猜错**。这就是为什么手写解释器在性能敏感的场合被诟病。

### §1.3 更好的做法:一张函数指针表

能不能不猜?思路是这样的:与其用 switch 比较操作码、再去某个地方找跳转目标，不如**直接用操作码做下标，查一张预先填好的表**。这张表里，第 `i` 项存的就是"操作码为 `i` 的那条字节码的执行代码的地址"。

```c
typedef void (*Handler)(void);
Handler dispatch[OP__MAX] = {
  [OP_ADD]  = &do_add,
  [OP_SUB]  = &do_sub,
  [OP_CALL] = &do_call,
  ...
};

for (;;) {
  uint8_t op = *pc++;
  dispatch[op]();           // 用操作码直接做下标, O(1) 跳转
}
```

这里 `dispatch[op]()` 做的事情是:用 `op` 当下标，从 `dispatch` 数组里取出一个函数指针，然后跳过去执行。这是一次**数组下标访问 + 间接跳转**——没有比较、没有二分查找,**O(1)**。

它比 switch 好在哪?

1. **查找是 O(1) 且无分支**。`dispatch[op]` 就是一次基地址加偏移的内存读，不涉及任何"比较操作码"的条件分支。
2. **间接跳转仍有一次，但更规律**。是的,`jmp dispatch[op]` 还是一次间接跳转，分支预测器还是要猜目标。但是，这一步的"目标"虽然每次不同，却是从一张**固定的表**里取的；而且 LuaJIT 进一步用了一个技巧(下面 §1.5 讲),让这件事在它的手写汇编里做到极致。

这种"用一张函数指针表做分发"的做法，有个专门的名字:**token-threaded dispatch**(令牌线程化分发),或者更一般地叫**dispatch table**。LuaJIT 用的就是它。

### §1.4 把执行代码写成汇编:为什么不能用 C 函数

上面那个 `do_add`、`do_sub`,如果是普通的 C 函数，会有个问题:**函数调用本身有开销**。一次 C 函数调用要保存寄存器、压栈、建立栈帧、返回时再恢复——这些开销在"每条字节码都调用一次"的密度下，会积少成多，把 dispatch 表 O(1) 的优势抵消掉大半。

LuaJIT 的解法很激进:它把**每一条字节码的执行代码，都用手写汇编写**,而且写得像一段"会自己跳回 dispatch 循环"的代码片段——执行完一条字节码，最后一条指令就是 `jmp` 回 dispatch 表取下一条。这样不存在"函数调用返回"的开销:整条执行流就是一连串的跳转，寄存器全程在 VM 自己手里，不经过 C 调用约定。

这些手写汇编片段活在 `vm_x64.dasc`、`vm_x86.dasc` 等文件里(用 dynasm 这个工具从一种带宏的汇编 DSL 生成真正的 `.s`)。每条字节码操作码对应一个 case,比如 `case BC_FORL:`、`case BC_FUNCF:`。每个 case 的代码体，执行完后都接一个统一的"取下一条字节码、查 dispatch 表、跳过去"的动作。

### §1.5 这个"取下一条、跳过去"的动作长什么样

这是 LuaJIT 解释器的心脏，叫 `ins_NEXT`,在 `vm_x64.dasc:212`:

```asm
|// Instruction decode+dispatch. Carefully tuned (nope, lodsd is not faster).
|.macro ins_NEXT
|  mov RCd, [PC]              // 1. 取出当前 PC 指向的整条字节码(32 位)到 RC
|  movzx RAd, RCH             // 2. 从中拆出 A 操作数(第 8-15 位)
|  movzx OP, RCL              // 3. 拆出操作码 OP(第 0-7 位)
|  add PC, 4                  // 4. PC 前进一条(字节码 4 字节对齐)
|  shr RCd, 16                // 5. 把 RC 右移 16 位,得到 D 操作数(或 B/C 拼起来)
|  jmp aword [DISPATCH+OP*8]  // 6. 用 OP 做下标查 dispatch 表,跳过去
|.endmacro
```

(来源:`vm_x64.dasc:212-219`。`aword`、`RAd`、`RCL` 都是 dynasm 的写法,`aword` 是一个指针宽度的内存操作,`RAd` 表示 RB 寄存器的低 32 位等。)

我们一行行看，这 6 步在干什么:

1. `mov RCd, [PC]`——从 PC 指向的内存一次读 4 字节(整条字节码),放到寄存器 RC。一次内存读。
2. `movzx RAd, RCH`——字节码的格式是 `[B|C|A|OP]`(P1-02 讲过，见 `lj_bc.h:13`),A 在第 8-15 位。`RCH` 是 RC 的高 8 位(因为 x86 是小端，内存里的 `[A|OP]` 读进 32 位寄存器后,A 正好落在高 8 位)。这步把 A 拆出来，放到 RA 寄存器，后面执行代码要用。
3. `movzx OP, RCL`——同理，操作码 OP 在最低 8 位(`RCL`),拆出来放到 OP 寄存器。
4. `add PC, 4`——PC 加 4,指向下一条字节码。
5. `shr RCd, 16`——RC 右移 16 位，现在 RC 里是 `[B|C]` 拼起来的 16 位(也就是 D 操作数，因为 D 就是 C 和 B 拼成的 16 位数，见 `lj_bc.h:38` 的 `bc_d`)。
6. `jmp aword [DISPATCH+OP*8]`——**这是 dispatch 的关键一步**。`DISPATCH` 是一个寄存器，指向 dispatch 表的基地址；`OP*8` 是因为 x64 上一根指针 8 字节；`[DISPATCH+OP*8]` 取出表中第 OP 项那个函数指针,`jmp` 跳过去。

注意第 6 步:它**不是** `jmp DISPATCH+OP*8`(直接跳到某个地址),而是 `jmp [DISPATCH+OP*8]`(先读那个地址里的值，再跳到那个值指向的地方)。也就是说,dispatch 表里存的是**指针**,jmp 跳到指针指向的代码。这就是间接跳转。

这一整套(取指、译码、PC 前进、查表、跳转)只有 6 条指令，没有比较、没有分支。一个跑千万次的循环，这个序列要执行千万次，每次都是这固定 6 步——极其规整，正是 CPU 流水线最喜欢的形态。注释 `Carefully tuned` 不是虚言。

到这里，第一个问题"解释器怎么从一条字节码找到执行代码"已经答完:**用一张函数指针表，操作码做下标,O(1) 间接跳转**。这张表,LuaJIT 叫它 `GG->dispatch`。下一节我们就钻进这张表的真实结构。

---

## §2 dispatch 表的真身:GG_State

### §2.1 一张"全局状态 + dispatch 表 + hotcount"的大杂烩

现在来看这张表在源码里的真实长相。它不是孤立的一张表，而是嵌在一个更大的结构 `GG_State` 里。`GG` 是 "Global state + dispatch table" 的缩写。定义在 `lj_dispatch.h:89`:

```c
/* Global state, main thread and extra fields are allocated together. */
typedef struct GG_State {
  lua_State L;                          /* Main thread. */
  global_State g;                       /* Global state. */
#if LJ_TARGET_MIPS
  ASMFunction got[LJ_GOT__MAX];         /* Global offset table. */
#endif
#if LJ_HASJIT
  jit_State J;                          /* JIT state. */
  HotCount hotcount[HOTCOUNT_SIZE];     /* Hot counters. */
#endif
  ASMFunction dispatch[GG_LEN_DISP];    /* Instruction dispatch tables. */
  BCIns bcff[GG_NUM_ASMFF];             /* Bytecode for ASM fast functions. */
} GG_State;
```

(来源:`lj_dispatch.h:88-109`。)

这个结构体为什么长得这么怪?为什么 `lua_State`、`global_State`、`jit_State`、`hotcount`、`dispatch` 全挤在一个结构体里?这是 LuaJIT 一个极其重要的设计决策，我们一点点拆。

先看字段含义:

- `lua_State L`:主线程的 Lua 状态(栈、当前函数等)。P1-02 讲过。
- `global_State g`:全局状态(GC、字符串表、当前 hook 掩码等)。整个虚拟机一份。
- `jit_State J`:JIT 编译器的全部状态(当前在录制的 trace、trace 数组、参数等)。只有 `LJ_HASJIT` 打开时才有这个字段。
- `HotCount hotcount[HOTCOUNT_SIZE]`:热点计数器数组,64 个槽。同样只在 JIT 打开时存在。
- `ASMFunction dispatch[GG_LEN_DISP]`:**这就是 dispatch 表**。每个表项是一个 `ASMFunction`(指向一段汇编代码的指针)。
- `BCIns bcff[GG_NUM_ASMFF]`:汇编快速函数的字节码。

最关键的一点:**这个结构体是一次性分配的、连续的一大块内存**。看 `lj_state.c:269`:

```c
GG = (GG_State *)allocf(allocd, NULL, 0, sizeof(GG_State));
```

整个 `GG_State` 在 `lua_newstate` 时一次性 malloc 出来，所有字段在内存里**挨在一起**。这个"挨在一起"不是巧合，是 §3 要讲的零开销热点检测的物理基础。

### §2.2 dispatch 表有多长:为什么不是一个操作码一项

来看 `dispatch` 数组的长度 `GG_LEN_DISP`。定义在 `lj_dispatch.h:84-86`:

```c
#define GG_NUM_ASMFF	57
#define GG_LEN_DDISP	(BC__MAX + GG_NUM_ASMFF)   /* 动态表长度 */
#define GG_LEN_SDISP	BC_FUNCF                   /* 静态表长度 */
#define GG_LEN_DISP	(GG_LEN_DDISP + GG_LEN_SDISP)  /* 总长度 */
```

(来源:`lj_dispatch.h:82-86`。)

`dispatch` 表被分成两段,**总长度 = 动态段(DDISP)+ 静态段(SDISP)**。为什么会分两段?这要回到一个 §1 没讲透的点:dispatch 表里存的函数指针,**会变**。

回忆 §1.5 的 `jmp [DISPATCH+OP*8]`:CPU 跳到 dispatch 表第 OP 项指向的代码。但这个"指向的代码"是可换的——这正是 LuaJIT 切换模式(纯解释 / 录制 / JIT 打开)的手段。比如，同一个 `BC_FORL` 操作码:

- **JIT 关闭时**:dispatch[BC_FORL] 指向"纯解释的 FORL 处理"(不数热点)。
- **JIT 打开时**:dispatch[BC_FORL] 指向"会先减热点计数器的 FORL 处理"。
- **正在录制时**:dispatch[BC_FORL] 指向"会同时录制 IR 的 FORL 处理"。

所以 dispatch 表需要能整体替换。但有些操作码的处理逻辑是**永远不变**的(比如 ADD,它没有热点、也不参与录制逻辑的差异),为了不每次切换都重写整张表,LuaJIT 把它分成两段:

- **静态段 SDISP**(`dispatch[GG_LEN_DDISP ... GG_LEN_DISP-1]`,长度 `BC_FUNCF`):存那些"固定的、计数类操作码的基准处理"。这段很少改。
- **动态段 DDISP**(`dispatch[0 ... GG_LEN_DDISP-1]`):存那些"当前实际在用的处理"。模式切换时，主要改这段。

具体地,`GG_LEN_DDISP = BC__MAX + GG_NUM_ASMFF`。`BC__MAX` 是字节码操作码的总数(看 `lj_bc.h:204`,枚举到 `BC_FUNCCW` 结束，约 96 个),`GG_NUM_ASMFF=57` 是汇编快速函数(fast functions)的数量。所以动态段不仅覆盖所有字节码操作码，还额外覆盖 57 个快速函数入口。`GG_LEN_SDISP = BC_FUNCF`——静态段从操作码 0 到 `BC_FUNCF`(函数头操作码),覆盖的是那些"循环和调用类、需要热点计数"的操作码。

这背后的设计哲学是:**只对"可能成为热点"的操作码(循环回边 FORL/ITERL/LOOP、函数头 FUNCF/FUNCV)做计数和模式切换，其余操作码永远走同一段固定代码**。这是"省"的体现——不为不可能成为热点的指令付代价。

### §2.3 dispatch 表的一项指向什么:makeasmfunc

dispatch 表的每一项是 `ASMFunction`,也就是一个函数指针。这个指针指向哪?指向 `vm_x64.dasc` 里手写汇编中、对应操作码那段代码的开头。

但这里有个问题:`vm_x64.dasc` 经过 dynasm 处理后，会生成一个巨大的 `.s` 汇编文件，再汇编成一段连续的机器码。这段机器码里，每个操作码处理代码的**起始地址**是哪里?LuaJIT 用一个聪明的办法:**记录每个操作码处理代码相对于整段汇编起始地址的偏移**,运行时用"起始地址 + 偏移"得到绝对地址。

看 `lj_vm.h:117-120`:

```c
/* Start of the ASM code. */
LJ_ASMF char lj_vm_asm_begin[];

/* Bytecode offsets are relative to lj_vm_asm_begin. */
#define makeasmfunc(ofs) lj_ptr_sign((ASMFunction)(lj_vm_asm_asm_begin + (ofs)), 0)
```

(来源:`lj_vm.h:116-120`。注意 `lj_ptr_sign` 是 ARM64 指针认证(PAuth)用的，在 x64 上是空操作，核心就是 `lj_vm_asm_begin + ofs`。)

`lj_vm_asm_begin` 是整段汇编 VM 机器码的起始地址(一个符号)。`makeasmfunc(ofs)` 返回 `lj_vm_asm_begin + ofs`——也就是说，给定一个偏移 `ofs`,就得到那段机器码里偏移 `ofs` 处的地址，把它当作函数指针塞进 dispatch 表。

那 `ofs` 从哪来?从另一张表 `lj_bc_ofs[]`(`lj_bc.h:268` 声明,`buildvm` 生成)。这张表存的是每个操作码对应的汇编代码偏移。看 `lj_dispatch.c:60`,dispatch 表的初始化:

```c
/* Initialize instruction dispatch table and hot counters. */
void lj_dispatch_init(GG_State *GG)
{
  uint32_t i;
  ASMFunction *disp = GG->dispatch;
  for (i = 0; i < GG_LEN_SDISP; i++)
    disp[GG_LEN_DDISP+i] = disp[i] = makeasmfunc(lj_bc_ofs[i]);   /* ① */
  for (i = GG_LEN_SDISP; i < GG_LEN_DDISP; i++)
    disp[i] = makeasmfunc(lj_bc_ofs[i]);                           /* ② */
  /* The JIT engine is off by default. luaopen_jit() turns it on. */
  disp[BC_FORL] = disp[BC_IFORL];                                  /* ③ */
  disp[BC_ITERL] = disp[BC_IITERL];
  /* Workaround for stable v2.1 bytecode. TODO: Replace with BC_IITERN. */
  disp[BC_ITERN] = &lj_vm_IITERN;
  disp[BC_LOOP] = disp[BC_ILOOP];
  disp[BC_FUNCF] = disp[BC_IFUNCF];
  disp[BC_FUNCV] = disp[BC_IFUNCV];
  ...
}
```

(来源:`lj_dispatch.c:59-82`。)

逐行看:

- 步骤 ①:循环把动态段和静态段的前 `GG_LEN_SDISP` 项,**都**初始化为 `makeasmfunc(lj_bc_ofs[i])`。也就是说，默认情况下动态段和静态段是镜像，指向同一份汇编代码。
- 步骤 ②:动态段里 `GG_LEN_SDISP` 到 `GG_LEN_DDISP` 这一段(快速函数那段),只初始化动态段。
- 步骤 ③——**这是 JIT 关闭时的关键设置**:`disp[BC_FORL] = disp[BC_IFORL]`。

要理解步骤 ③,必须先讲清楚字节码操作码的"三件套"分组。

### §2.4 操作码的三件套:FORL / IFORL / JFORL

打开 `lj_bc.h:171`,字节码定义里有这么一段(节选):

```c
/* Loops and branches. I/J = interp/JIT, I/C/L = init/call/loop. */
_(FORI,	base,	___,	jump,	___) \
_(JFORI,	base,	___,	jump,	___) \
\
_(FORL,	base,	___,	jump,	___) \
_(IFORL,	base,	___,	jump,	___) \
_(JFORL,	base,	___,	lit,	___) \
\
_(ITERL,	base,	___,	jump,	___) \
_(IITERL,	base,	___,	jump,	___) \
_(JITERL,	base,	___,	lit,	___) \
\
_(LOOP,	rbase,	___,	jump,	___) \
_(ILOOP,	rbase,	___,	jump,	___) \
_(JLOOP,	rbase,	___,	lit,	___) \
\
_(JMP,	rbase,	___,	jump,	___) \
```

(来源:`lj_bc.h:171-187`。)

注意看:对每一个"可能成为热点的循环/迭代操作码",都有**三个变体**:

- **FORL / ITERL / LOOP / FUNCF / FUNCV**:带热点计数的版本。执行时会先减 hotcount。
- **IFORL / IITERL / ILOOP / IFUNCF / IFUNCV**:纯解释的版本(前缀 `I` 表示 interpreter)。不数热点。
- **JFORL / JITERL / JLOOP / JFUNCF / JFUNCV**:JIT 版本(前缀 `J`)。直接跳进已编译的 trace 机器码。

这三个变体的操作码值是**连续**的,FORL+0=FORL,FORL+1=IFORL,FORL+2=JFORL,以此类推。这点在 `lj_bc.h:219-228` 有一组静态断言盯死:

```c
LJ_STATIC_ASSERT((int)BC_FORL + 1 == (int)BC_IFORL);
LJ_STATIC_ASSERT((int)BC_FORL + 2 == (int)BC_JFORL);
LJ_STATIC_ASSERT((int)BC_ITERL + 1 == (int)BC_IITERL);
LJ_STATIC_ASSERT((int)BC_ITERL + 2 == (int)BC_JITERL);
LJ_STATIC_ASSERT((int)BC_LOOP + 1 == (int)BC_ILOOP);
LJ_STATIC_ASSERT((int)BC_LOOP + 2 == (int)BC_JLOOP);
LJ_STATIC_ASSERT((int)BC_FUNCF + 1 == (int)BC_IFUNCF);
LJ_STATIC_ASSERT((int)BC_FUNCF + 2 == (int)BC_JFUNCF);
LJ_STATIC_ASSERT((int)BC_FUNCV + 1 == (int)BC_IFUNCV);
LJ_STATIC_ASSERT((int)BC_FUNCV + 2 == (int)BC_JFUNCV);
```

(来源:`lj_bc.h:219-228`。)

为什么要这样连续排列?因为这样切换模式时，可以从一个操作码**算出**它的另一个变体:`FORL + 1 = IFORL`,`FORL + 2 = JFORL`,简单的整数加减就行，不用查表。后面 `lj_dispatch_update` 里就会看到这种用法。

现在回到 `lj_dispatch_init` 的步骤 ③:`disp[BC_FORL] = disp[BC_IFORL]`。它的意思是:**默认(JIT 关闭)时，把 FORL 的处理直接指向 IFORL 的处理**。也就是说，字节码里写的是 FORL(可能是热点的循环回边),但 JIT 没开，所以就当普通循环(IFORL)执行，不数热点。等 JIT 打开,`lj_dispatch_update` 会把 `disp[BC_FORL]` 改成指向真正会数热点的 FORL 汇编代码。

这就是 dispatch 表的精髓——**同一份字节码，执行哪段代码，完全由 dispatch 表当前的内容决定**。字节码本身不动，动的是表。这是 LuaJIT 模式切换的核心机制,§5 会展开。

---

## §3 零开销的精髓:dispatch 表与 hotcount 数组共享一块内存

前面两节铺垫了这么多，现在终于到了本章最精妙的设计:**dispatch 表和 hotcount 数组，故意挤在同一块连续内存里**。这个安排的唯一目的，就是让"一边解释一边数热点"这件事的开销趋近于零。

### §3.1 问题的提出:热点计数必须几乎不花时间

回忆 P0-01 §6 讲的:LuaJIT 要发现热点，就得给每条循环回边、每次函数调用配一个计数器，每执行一次就更新一下计数器，到阈值就触发编译。

现在请你想一个问题:**这个"更新计数器"的动作，放在哪里执行?**

最直觉的答案:在解释器的主循环里，每执行一条字节码之前，先查一下"这条字节码是不是循环回边?如果是，把对应的计数器加一"。但这立刻有两个问题:

1. **判断"是不是循环回边"本身就要花时间**——每条字节码都要判断一次，而绝大多数字节码根本不是循环回边。
2. **查计数器要一次额外的内存访问**——计数器在内存里，要读它、改它、写回，至少两次内存访问。

如果每条字节码都背这么一个"判断 + 两次访存"的包袱，那解释器就慢了。可是热点计数又是 JIT 必须的，不能不数。怎么办?

LuaJIT 的洞察是:**只对"可能成为热点的字节码"数，而且数的方式要榨干每一次内存访问的效率**。

"可能成为热点的字节码"是哪些?就是 §2.4 说的那几个循环回边和函数头:FORL、ITERL、ITERN、LOOP、FUNCF、FUNCV。LuaJIT 的做法是:**只有这几个操作码的汇编处理代码，会去碰计数器；其他操作码的汇编代码，完全不碰计数器**。这样，绝大多数字节码(ADD、MOV、CALL 的普通调用部分等)执行时，完全没有计数开销。

但即使只在 FORL 等少数操作码里数，计数器还是要访问内存。怎么让这次访问尽可能便宜?

### §3.2 关键洞察:让计数器"就在手边"

§1.5 讲了 dispatch 的最后一步是 `jmp [DISPATCH+OP*8]`。这里 `DISPATCH` 是一个寄存器，里面存着 dispatch 表的基地址。也就是说,**解释器在执行每条字节码时,DISPATCH 寄存器里始终握着 dispatch 表的地址**。

那 hotcount 数组放在哪?如果它和 dispatch 表在两块不相干的内存里，那访问 hotcount 就要用一个完全不同的地址——得先把这个地址搞到手(要么从某个全局变量读，要么算偏移),多一道工序。

LuaJIT 的解法简单到粗暴:**把 hotcount 数组，和 dispatch 表，放在同一个 `GG_State` 结构体里，挨在一起**。这样，既然 DISPATCH 寄存器已经握着 dispatch 表的基地址，而 hotcount 离它有个**编译期固定的偏移**,那访问 hotcount 就是:

```
地址 = DISPATCH + GG_DISP2HOT + (槽号 × 2)
```

`GG_DISP2HOT` 是 hotcount 数组相对于 dispatch 表的偏移，在编译期就能算出来(`offsetof(GG_State, hotcount) - offsetof(GG_State, dispatch)`)。所以汇编里访问某个 hotcount 槽，就是一条带偏移的内存指令，不需要任何额外寻址。

这个偏移定义在 `lj_dispatch.h:122`:

```c
#define GG_DISP2HOT	(GG_OFS(hotcount) - GG_OFS(dispatch))
```

(来源:`lj_dispatch.h:118-123`。`GG_OFS(field)` 就是 `offsetof(GG_State, field)`,见 `lj_dispatch.h:111`。)

类似的，还有从 dispatch 到 global_State(`g`)、到 jit_State(`J`)的偏移:

```c
#define GG_G2DISP	(GG_OFS(dispatch) - GG_OFS(g))
#define GG_DISP2G	(GG_OFS(g) - GG_OFS(dispatch))
#define GG_DISP2J	(GG_OFS(J) - GG_OFS(dispatch))
#define GG_DISP2HOT	(GG_OFS(hotcount) - GG_OFS(dispatch))
#define GG_DISP2STATIC	(GG_LEN_DDISP*(int)sizeof(ASMFunction))
```

(来源:`lj_dispatch.h:118-123`。)

这组偏移宏是整个 LuaJIT 解释器寻址的基石。解释器汇编代码里，要访问 `g` 的某个字段(比如 `g->vmstate`),就用 `DISPATCH_GL(vmstate)`,它展开成 `GG_DISP2G + offsetof(global_State, vmstate)`,于是 `mov [DISPATCH + DISPATCH_GL(vmstate)]` 就直接读写到那个字段。要访问 `J` 的某个字段，就用 `DISPATCH_J(...)`,同理。要访问 hotcount,就用 `GG_DISP2HOT`。**一切寻址，都从 DISPATCH 这个寄存器出发，加上编译期固定的偏移**。

这就是 §2.1 那个看起来奇怪的 `GG_State` 结构体布局的真正原因:`L`、`g`、`J`、`hotcount`、`dispatch` 全挤在一起，是为了让它们彼此之间的偏移都是编译期常量，从而解释器汇编代码能用 `[DISPATCH + 常量偏移]` 这种最快的寻址方式访问它们，一个乘法都不用(更不用说额外的内存读来获取基地址)。

### §3.3 DISPATCH 寄存器指向哪里:glref + GG_G2DISP

补一个细节:DISPATCH 寄存器到底指向 dispatch 表的基地址，还是 `g` 的基地址?

看汇编入口处(`vm_x64.dasc:524`):

```asm
|  mov DISPATCH, L:RB->glref		// Setup pointer to dispatch table.
|  add DISPATCH, GG_G2DISP
```

(来源:`vm_x64.dasc:524-525`,以及类似的入口在 `:584`、`:628`、`:665`。)

`L:RB->glref` 是 `lua_State` 里的 `glref` 字段，它指向对应的 `global_State g`。所以第一行让 DISPATCH 指向 `g`。第二行 `add DISPATCH, GG_G2DISP`,把 DISPATCH 加上"dispatch 相对 g 的偏移",于是 DISPATCH 现在指向 dispatch 表的基地址。

为什么要绕这一步?因为 `lua_State` 总是能拿到 `glref`(它是 Lua 状态机最基础的指针),而 dispatch 表的地址没法直接从一个全局符号拿到(它在堆上，每次 newstate 分配的地址不同)。所以从 `glref` 出发，加一个编译期常量偏移，就到了 dispatch 表。从那一刻起,DISPATCH 寄存器就稳定地指着 dispatch 表，直到解释器退出。

### §3.4 hotcount 槽怎么选:pc>>2 哈希

现在 dispatch 表和 hotcount 在一块儿了,DISPATCH 也指着 dispatch 表了。剩下的最后一个问题:**给定一条循环回边字节码，它对应 hotcount 数组里哪一个槽?**

hotcount 数组只有 64 个槽(`HOTCOUNT_SIZE=64`,见 `lj_dispatch.h:74`),而程序里可能有成千上万条循环回边字节码。显然不可能每条字节码独占一个槽。LuaJIT 用**哈希**把字节码地址(pc)映射到 64 个槽中的一个。

这个哈希极其简单，在 `lj_dispatch.h:125`:

```c
#define hotcount_get(gg, pc) \
  (gg)->hotcount[(u32ptr(pc)>>2) & (HOTCOUNT_SIZE-1)]
#define hotcount_set(gg, pc, val) \
  (hotcount_get((gg), (pc)) = (HotCount)(val))
```

(来源:`lj_dispatch.h:125-128`。)

槽号 = `(pc >> 2) & (HOTCOUNT_SIZE - 1)`。我们拆开看:

- **`pc >> 2`**:字节码是 32 位(4 字节)对齐的(P1-02 讲过，见 `lj_bc.h:13` 的指令格式和 §1.5 的 `add PC, 4`)。所以任何一条字节码的地址，低 2 位一定是 00。`pc >> 2` 把这无信息的低 2 位去掉，只保留有区分度的位。这样，相邻的字节码地址(相差 4)右移 2 后只差 1,分布均匀。
- **`& (HOTCOUNT_SIZE - 1)`**:`HOTCOUNT_SIZE=64`,`64-1=63`,二进制是 `0b111111`。`& 63` 就是取低 6 位，得到一个 0–63 的数，正好是 64 个槽的下标。因为 `HOTCOUNT_SIZE` 是 2 的幂,`& (size-1)` 等价于 `% size`,但 `&` 比 `%` 快。

为什么用这么简单的哈希?因为字节码地址已经天然分布得很散(每条相差 4,而 `>>2` 后相差 1),低 6 位足够随机。会不会两条不同的循环回边(pc 不同)哈希到同一个槽?会。这叫**哈希冲突**。冲突了会怎样?两条循环共用一个计数器，可能比真实热点更早或更晚触发编译。但这对正确性没影响(最坏只是触发时机略偏),而换来的好处是:槽数极少(64),整个 hotcount 数组只占 128 字节(64 × 2 字节),几乎能常驻 L1 cache,访问极快。这是个**用一点点不准，换巨大性能**的典型权衡。

注意汇编里的写法和 C 宏略有不同(`vm_x64.dasc:332`):

```asm
|.macro hotloop, reg
|  mov reg, PCd            // reg = PC 的低 32 位
|  shr reg, 1              // reg >>= 1
|  and reg, HOTCOUNT_PCMASK // reg &= (HOTCOUNT_SIZE-1)*sizeof(HotCount)
|  sub word [DISPATCH+reg+GG_DISP2HOT], HOTCOUNT_LOOP
|  jb ->vm_hotloop
|.endmacro
```

(来源:`vm_x64.dasc:331-338`。)

C 宏里是 `pc >> 2`,汇编里是 `PC >> 1`,看起来不一致?其实是一致的，原因在于**单位不同**:

- C 宏里 `pc` 是字节地址，字节码 4 字节，所以 `>>2`。
- 汇编里 `PC` 也是字节地址,`shr reg, 1` 后 `reg` 是"字节地址 / 2"。但接下来 `and reg, HOTCOUNT_PCMASK` 里的 `HOTCOUNT_PCMASK` 不是 63,而是 `(HOTCOUNT_SIZE-1)*sizeof(HotCount)`(见 `lj_dispatch.h:75`),即 `63 * 2 = 126`。所以 `reg &= 126` 后,reg 是一个 0, 2, 4, ..., 126 的偶数——正好是"字节地址 / 2 取低 6 位，再乘 2",也就是"hotcount 数组里的字节偏移"(因为每个 HotCount 是 2 字节，槽号乘 2 才是字节偏移)。

换句话说，汇编为了少做一次乘法(槽号 × 2 = 字节偏移),把 `>>2 & 63 <<1` 等价改写成了 `>>1 & 126`,一步到位得到字节偏移。这是手写汇编才有的、对寻址计算的极致打磨。C 宏 `hotcount_get` 写得易读，汇编写得高效，两者算的是同一个槽。

`HOTCOUNT_PCMASK` 定义在 `lj_dispatch.h:75`:

```c
#define HOTCOUNT_PCMASK		((HOTCOUNT_SIZE-1)*sizeof(HotCount))
```

### §3.5 计数器是递减的:从阈值减到 0

还有一个细节值得讲。hotcount 是**递减**的，不是递增的。初始化时，所有 64 个槽都被设成一个固定的起始值(阈值),每执行一次循环回边就**减**一个固定值，减到低于 0(无符号下溢)就触发。

看初始化函数 `lj_dispatch_init_hotcount`(`lj_dispatch.c:86`):

```c
#if LJ_HASJIT
/* Initialize hotcount table. */
void lj_dispatch_init_hotcount(global_State *g)
{
  int32_t hotloop = G2J(g)->param[JIT_P_hotloop];   /* 用户配置的循环数阈值,默认 56 */
  HotCount start = (HotCount)(hotloop*HOTCOUNT_LOOP - 1);  /* 起始值 */
  HotCount *hotcount = G2GG(g)->hotcount;
  uint32_t i;
  for (i = 0; i < HOTCOUNT_SIZE; i++)
    hotcount[i] = start;                              /* 所有槽都设成起始值 */
}
#endif
```

(来源:`lj_dispatch.c:84-95`。)

默认 `hotloop = 56`(见 `lj_jit.h:116` 的 `JIT_PARAMDEF` 里 `hotloop, 56`)。起始值 `= 56 * HOTCOUNT_LOOP - 1 = 56 * 2 - 1 = 111`。每次循环回边减 `HOTCOUNT_LOOP=2`,减 56 次到 -1(下溢),触发。`-1` 是为了让 `sub + jb` 的判定正好在第 56 次触发(细节:`sub` 产生借位当且仅当结果下溢,jb 是 jump-if-below,即借位时跳)。

为什么用递减而不是递增?因为**判定"到没到阈值"的代价不同**:

- 递增:每次要 `count++; if (count >= threshold)`,需要一次比较(`count` 和 `threshold` 比)。
- 递减:每次 `count -= step; if (count < 0)`,只需要判断减法本身有没有产生借位——而 x86 的 `sub` 指令天然会设置 CF(carry flag,借位标志),`jb` 就是测 CF。**不需要任何显式的比较指令**。

汇编里 `sub word [...], HOTCOUNT_LOOP; jb ->vm_hotloop` 就两条指令，第二条 `jb` 几乎是"白送"的(它只是读 sub 已经设好的标志位)。这就是递减的妙处:把"判定是否触发"这件事，折叠进了"递减"这个必须做的动作里，零额外开销。

### §3.6 把整个零开销计数看完整

现在把 §3 讲的所有零件组装起来，看一遍"解释器执行一条 FORL(循环回边)字节码时，数热点"的完整过程。汇编代码在 `vm_x64.dasc:4433`(BC_FORL 的处理):

```asm
case BC_FORL:
  |.if JIT
  |  hotloop RBd           // ← 这一行展开成 §3.4 那段宏
  |.endif
  | // Fall through. Assumes BC_IFORL follows and ins_AJ is a no-op.
  break;
```

(来源:`vm_x64.dasc:4433-4438`。`|.if JIT` 表示这段只在 JIT 编译时生成。)

`hotloop RBd` 展开后是:

```asm
mov  RBd, PCd                          // 1. 取 PC 低 32 位
shr  RBd, 1                            // 2. 右移 1
and  RBd, HOTCOUNT_PCMASK              // 3. 取低 7 位(& 126)
sub  word [DISPATCH+RBd+GG_DISP2HOT], HOTCOUNT_LOOP  // 4. hotcount 槽减 2
jb   ->vm_hotloop                      // 5. 借位则跳到 vm_hotloop
```

然后 fall through 到 BC_IFORL 的处理代码(真正执行循环迭代的那段汇编),正常把循环往前推一步。

整个过程:

- **第 1-3 步**:算出当前 PC 对应的 hotcount 槽的字节偏移，放到 RBd。3 条寄存器指令，无访存。
- **第 4 步**:一次内存读改写(`sub word [...]`,读 hotcount 槽、减 2、写回)。这是唯一一次访存，而且访问的是 64 个槽里某个槽，极大概率在 L1 cache。
- **第 5 步**:一次条件跳转，几乎一定不跳(只有 1/56 的概率跳)。分支预测器很快学会"几乎总是不跳",所以这条跳转的代价趋近于零。

也就是说,**每条循环回边字节码，只多付出了 3 条寄存器指令 + 1 次 L1 cache 访存 + 1 条几乎不跳的条件跳转**。这就是"零开销"的真正含义——不是真的零，而是小到可以忽略。而它换来的是:LuaJIT 能在不拖慢解释器的前提下，精确地知道哪个循环跑了多少次，在合适的时机触发 JIT 编译。

这一切的物理基础，就是 §2.1 那个"挤在一起"的 `GG_State`:**DISPATCH 寄存器握着 dispatch 表,hotcount 紧挨着 dispatch 表，偏移是编译期常量，所以访问 hotcount 就是一次 `[DISPATCH + 常量 + 寄存器]` 的内存操作**。这是 LuaJIT 全书最精巧的几处设计之一。

---

## §4 从热点到编译:触发流程

§3 讲了"怎么数",这一节讲"数到了之后怎么办"。也就是 hotcount 减到下溢之后，从"解释器正常跑"切换到"开始录制 trace"的全过程。

### §4.1 汇编侧的入口:vm_hotloop 和 vm_hotcall

hotcount 下溢后,§3.6 的第 5 步 `jb ->vm_hotloop` 会跳到 `vm_hotloop`。这是汇编里的一个标号，在 `vm_x64.dasc:2282`:

```asm
|->vm_hotloop:			// Hot loop counter underflow.
|.if JIT
|  mov LFUNC:RB, [BASE-16]		// Same as curr_topL(L).
|  cleartp LFUNC:RB
|  mov RB, LFUNC:RB->pc
|  movzx RDd, byte [RB+PC2PROTO(framesize)]
|  lea RD, [BASE+RD*8]
|  mov L:RB, SAVE_L
|  mov L:RB->base, BASE
|  mov L:RB->top, RD                  // 修一下 L->top(录制要用)
|  mov CARG2, PC                      // 第二个参数:pc
|  lea CARG1, [DISPATCH+GG_DISP2J]    // 第一个参数:jit_State *J
|  mov aword [DISPATCH+DISPATCH_J(L)], L:RB  // J->L = L
|  mov SAVE_PC, PC
|  call extern lj_trace_hot		// (jit_State *J, const BCIns *pc)
|  jmp <3                             // 跳回去继续
|.endif
```

(来源:`vm_x64.dasc:2282-2298`。)

逐行解读:

1. 前几行从栈帧(`[BASE-16]`)取出当前函数，算出 `L->top`。这是为进入 C 函数做准备(Lua 的栈顶要正确，否则录制时会出错)。
2. `mov CARG2, PC`:把 PC 放进第二个参数寄存器(x64 System V ABI 是 RSI,Windows ABI 是 RDX)。
3. **`lea CARG1, [DISPATCH+GG_DISP2J]`**:这是关键一行。`GG_DISP2J` 是 `jit_State J` 相对 dispatch 表的偏移(`lj_dispatch.h:121`)。所以 `DISPATCH + GG_DISP2J` 就是 `J` 的地址。一行 `lea` 拿到 `jit_State` 的指针，作为第一个参数传给 `lj_trace_hot`。**又是"从 DISPATCH 出发加常量偏移"这个套路**。
4. `mov [DISPATCH+DISPATCH_J(L)], L:RB`:把 `J->L = L` 写进去(让 J 知道自己在为哪个 Lua 线程录制)。
5. `call extern lj_trace_hot`:真正调用 C 函数 `lj_trace_hot`,进入 trace 录制。
6. `jmp <3`:跳回到 `->3` 标号(即 `vm_inshook` 的后半段，见 `vm_x64.dasc:2266`),重新从 dispatch 表取下一条字节码继续解释。

注意第 5 步是 `call`,意味着 `lj_trace_hot` 返回后，执行流回到 `jmp <3`,**解释器照常继续**。也就是说,hotcount 触发并不会"卡住"解释器——它只是"顺便"启动了 trace 录制，录制本身是个更复杂的过程(P2 详讲),但解释器在调用 `lj_trace_hot` 之后照样往前跑。

函数调用(BC_FUNCF/BC_FUNCV)的入口 `vm_hotcall` 类似，在 `vm_x64.dasc:2307`,逻辑结构相同，只是它要处理"函数调用"的语义(比如设置 `L->top` 到参数末尾)。还有一个小细节:函数热调用会给 PC 打个标记，看 `vm_x64.dasc:2310`:

```asm
|->vm_hotcall:			// Hot call counter underflow.
|.if JIT
|  mov SAVE_PC, PC
|  or PC, 1				// Marker for hot call.
|1:
|.endif
|  lea RD, [BASE+NARGS:RD*8-8]
...
|  call extern lj_dispatch_call	// (lua_State *L, const BCIns *pc)
```

(来源:`vm_x64.dasc:2307-2319`。)

`or PC, 1`——把 PC 的最低位置 1。这是个**标记位**:因为字节码地址一定是 4 字节对齐的，低 2 位一定是 00,所以把最低位或成 1 不会丢失真实地址信息(读的时候 `& ~1` 还原)。这个标记让下游的 `lj_dispatch_call` 知道"这次调用是 hotcall 触发的"(下面 §4.3 会看到它怎么用)。

### §4.2 C 侧的入口:lj_trace_hot

汇编跳到 C 函数 `lj_trace_hot`,在 `lj_trace.c:781`:

```c
/* A hotcount triggered. Start recording a root trace. */
void LJ_FASTCALL lj_trace_hot(jit_State *J, const BCIns *pc)
{
  /* Note: pc is the interpreter bytecode PC here. It's offset by 1. */
  ERRNO_SAVE
  /* Reset hotcount. */
  hotcount_set(J2GG(J), pc, J->param[JIT_P_hotloop]*HOTCOUNT_LOOP);
  /* Only start a new trace if not recording or inside __gc call or vmevent. */
  if (J->state == LJ_TRACE_IDLE &&
      !(J2G(J)->hookmask & (HOOK_GC|HOOK_VMEVENT))) {
    J->parent = 0;  /* Root trace. */
    J->exitno = 0;
    J->state = LJ_TRACE_START;
    lj_trace_ins(J, pc-1);
  }
  ERRNO_RESTORE
}
```

(来源:`lj_trace.c:781-796`。)

逐步看:

1. **`hotcount_set(J2GG(J), pc, J->param[JIT_P_hotloop]*HOTCOUNT_LOOP)`**:重置 hotcount。`J2GG(J)` 是从 `J` 反算出 `GG_State`(`lj_dispatch.h:113`),`hotcount_set` 把 pc 对应的槽重新设回阈值(`hotloop * HOTCOUNT_LOOP = 56 * 2 = 112`)。**为什么要重置?** 因为刚才这个槽已经减到下溢了，如果不重置，它就会一直是 0 附近，下一次循环回边立刻又触发——那样会在一个循环里反复触发录制，乱套。重置成阈值，让这个循环"冷静"下来，等这次录制完成、trace 编译好、字节码被 patch 成 JLOOP(§4.4 讲)之后，就不再走 hotcount 路径了。如果录制失败(比如 trace 太复杂),这个重置的阈值就是"下次再试"的等待期。

2. **`if (J->state == LJ_TRACE_IDLE && ...)`**:只有当 JIT 引擎当前空闲(`LJ_TRACE_IDLE`)、且不在 GC 钩子或 VM 事件中时，才启动新的录制。这一步防止"已经在录制又触发新的录制"(嵌套录制是不允许的,P2 会讲为什么)。如果条件不满足，这次 hotcount 触发就被"吞掉"了(不做事,hcount 已经重置，等下次)。

3. **`J->parent = 0; J->exitno = 0;`**:标记这次启动的是一个 **root trace**(根轨迹),不是 side trace。`parent=0` 表示没有父 trace。side trace 的概念 P0-01 §13 讲过，是从一个经常失败的 guard 退出点录制的新 trace,那个场景下 `parent` 非零。这里是 hotcount 直接触发的，一定是 root。

4. **`J->state = LJ_TRACE_START;`**:把 JIT 状态机置为"开始录制"。trace 的状态机是个枚举 `TraceState`(`lj_jit.h:144-153`):IDLE → START → RECORD → END → ASM → IDLE。这一步是 IDLE → START。

5. **`lj_trace_ins(J, pc-1)`**:这是真正驱动录制的入口。注意 `pc-1`——注释解释了:**解释器里的 PC 永远指向"下一条要执行的字节码"**(因为 §1.5 的 `add PC, 4` 已经把 PC 前进了),所以触发 hotcount 的那条字节码其实是 `pc-1`。`lj_trace_ins` 会让录制器从那条字节码开始录。

`lj_trace_ins` 在 `lj_trace.c:770`:

```c
/* A bytecode instruction is about to be executed. Record it. */
void lj_trace_ins(jit_State *J, const BCIns *pc)
{
  /* Note: J->L must already be set. pc is the true bytecode PC here. */
  J->pc = pc;
  J->fn = curr_func(J->L);
  J->pt = isluafunc(J->fn) ? funcproto(J->fn) : NULL;
  while (lj_vm_cpcall(J->L, NULL, (void *)J, trace_state) != 0)
    J->state = LJ_TRACE_ERR;
}
```

(来源:`lj_trace.c:770-778`。)

这里最关键的是 `lj_vm_cpcall(... trace_state)`:它在一个**受保护调用**(protected call,出错时不直接 throw,而是返回错误码)里运行 `trace_state`。`trace_state` 是 trace 编译的状态机(`lj_trace.c:680`),它根据 `J->state` 决定下一步:START 时调 `trace_start`,RECORD 时调 `lj_record_ins`(录制一条 IR),END 时做优化,ASM 时调 `lj_asm_trace` 生成机器码。整个录制-优化-汇编的过程，都在这个状态机里推进。具体每个状态做什么,P2-05 会逐行讲，本章只关心入口。

### §4.3 函数调用的入口:lj_dispatch_call

hotcall 那条路(§4.1 的 `vm_hotcall`)最后调的是 `lj_dispatch_call`,在 `lj_dispatch.c:475`:

```c
/* Call dispatch. Used by call hooks, hot calls or when recording. */
ASMFunction LJ_FASTCALL lj_dispatch_call(lua_State *L, const BCIns *pc)
{
  ERRNO_SAVE
  GCfunc *fn = curr_func(L);
  BCOp op;
  global_State *g = G(L);
#if LJ_HASJIT
  jit_State *J = G2J(g);
#endif
  int missing = call_init(L, fn);
#if LJ_HASJIT
  J->L = L;
  if ((uintptr_t)pc & 1) {  /* Marker for hot call. */
    ...
    pc = (const BCIns *)((uintptr_t)pc & ~(uintptr_t)1);
    lj_trace_hot(J, pc);
    ...
    goto out;
  } else if (J->state != LJ_TRACE_IDLE &&
             !(g->hookmask & (HOOK_GC|HOOK_VMEVENT))) {
    /* Record the FUNC* bytecodes, too. */
    lj_trace_ins(J, pc-1);
    ...
  }
#endif
  ...
  op = bc_op(pc[-1]);  /* Get FUNC* op. */
#if LJ_HASJIT
  /* Use the non-hotcounting variants if JIT is off or while recording. */
  if ((!(J->flags & JIT_F_ON) || J->state != LJ_TRACE_IDLE) &&
      (op == BC_FUNCF || op == BC_FUNCV))
    op = (BCOp)((int)op+(int)BC_IFUNCF-(int)BC_FUNCF);
#endif
  ERRNO_RESTORE
  return makeasmfunc(lj_bc_ofs[op]);  /* Return static dispatch target. */
}
```

(来源:`lj_dispatch.c:474-528`,省略了 assert 和部分细节。)

几个要点:

1. **`if ((uintptr_t)pc & 1)`**——这正是在检查 §4.1 那个 `or PC, 1` 打的标记。如果最低位是 1,说明这次调用是 hotcall 触发的。然后:
   - `pc = pc & ~1`:把标记位清掉，恢复真实 PC。
   - `lj_trace_hot(J, pc)`:和循环触发走同一个函数，启动 root trace 录制。
2. **`else if (J->state != LJ_TRACE_IDLE && ...)`**:如果不是 hotcall 标记，但 JIT 正在录制(状态非 IDLE),那也要 `lj_trace_ins` 录下这条 FUNC 字节码(因为录制时每条字节码都要录)。
3. **末尾的 `op = op + BC_IFUNCF - BC_FUNCF`**:这是关键的模式切换。如果 JIT 没开(`!(J->flags & JIT_F_ON)`)或正在录制(`J->state != LJ_TRACE_IDLE`),且这条是 FUNCF/FUNCV,那把操作码**改写成 IFUNCF/IFUNCV**。意思就是:既然不打算(或不能)触发新的 hotcall,就走那个"不数热点"的 IFUNCF 处理代码。这就是 §2.4 三件套的实际用法——**用操作码的整数加减，在三个变体间切换**。
4. **`return makeasmfunc(lj_bc_ofs[op])`**:`lj_dispatch_call` 是个**返回函数指针**的函数。它返回目标操作码对应的汇编代码地址，调用方(汇编里的 `vm_hotcall` 那段)拿到这个地址后 `jmp` 过去执行。这是个 ASMFunction-返回-ASMFunction 的中转设计。

### §4.4 录制完成后:字节码被 patch 成 JLOOP

录制、优化、汇编都完成后,trace 的机器码生成好了。但还有最后一步:**让解释器下次再跑到这个循环回边时，直接跳进已编译的 trace 机器码，而不再走解释执行**。

怎么做到?patch 字节码本身。看 `lj_trace.c:499` 的 `trace_stop`:

```c
/* Stop tracing. */
static void trace_stop(jit_State *J)
{
  BCIns *pc = mref(J->cur.startpc, BCIns);
  BCOp op = bc_op(J->cur.startins);
  ...
  switch (op) {
  case BC_FORL:
    setbc_op(pc+bc_j(J->cur.startins), BC_JFORI);  /* Patch FORI, too. */
    /* fallthrough */
  case BC_LOOP:
  case BC_ITERL:
  case BC_FUNCF:
    /* Patch bytecode of starting instruction in root trace. */
    setbc_op(pc, (int)op+(int)BC_JLOOP-(int)BC_LOOP);  // ← 关键
    setbc_d(pc, traceno);
  addroot:
    J->cur.nextroot = pt->trace;
    pt->trace = (TraceNo1)traceno;
    break;
  ...
```

(来源:`lj_trace.c:498-521`,节选。)

关键是 `setbc_op(pc, op + BC_JLOOP - BC_LOOP)`——把起始字节码(比如 BC_FORL)的操作码,**改写成 BC_JLOOP**(因为 FORL+某偏移=JLOOP,正是 §2.4 三件套的连续排列保证的)。同时 `setbc_d(pc, traceno)`,把这条字节码的 D 操作数改成 trace 编号。

这样，下次解释器再跑到这条字节码时:

1. `movzx OP, RCL` 取出操作码，现在是 BC_JLOOP 而不是 BC_FORL。
2. `jmp [DISPATCH+OP*8]` 跳到 dispatch[BC_JLOOP] 指向的代码。
3. 而 dispatch[BC_JLOOP] 指向的是"直接跳进 trace 机器码"的汇编(从 D 操作数里读出 traceno,找到 `J->trace[traceno]->mcode`,`jmp` 过去)。

从此，这个循环就跑在机器码里，飞快。**hotcount 再也不参与这个循环的执行**——因为字节码已经不是 FORL 了，根本不会走到 `hotloop` 那段代码。

这是个非常优雅的闭环:hotcount 发现热点 → 触发录制 → 生成机器码 → patch 字节码成 JLOOP → 下次直接跳机器码,hotcount 功成身退。整个机制里，字节码只在"被 patch 成 JLOOP"那一刻改了一次，其余时间它是只读的；真正在变的是 dispatch 表(决定哪段代码被执行)和字节码的操作码(patch 后)。

---

## §5 模式切换:改表，不改字节码

§4 讲的是 hotcount 触发后的单次流程。但还有一个更宏观的问题:**LuaJIT 怎么在"JIT 关闭"、"JIT 打开"、"正在录制"、"开了 hook"这些模式之间切换?**

答案贯穿整章:**改 dispatch 表**。字节码不动，改的是 dispatch 表里每一项指向的代码。

### §5.1 模式有哪些:dispatchmode 的位

LuaJIT 用一个字节 `g->dispatchmode` 记录当前模式，每一位是一种开关。这些位的定义在 `lj_dispatch.c:97-103`:

```c
/* Internal dispatch mode bits. */
#define DISPMODE_CALL	0x01	/* Override call dispatch. */
#define DISPMODE_RET	0x02	/* Override return dispatch. */
#define DISPMODE_INS	0x04	/* Override instruction dispatch. */
#define DISPMODE_JIT	0x10	/* JIT compiler on. */
#define DISPMODE_REC	0x20	/* Recording active. */
#define DISPMODE_PROF	0x40	/* Profiling active. */
```

(来源:`lj_dispatch.c:97-103`。)

含义:

- **DISPMODE_JIT**:JIT 引擎是否打开。开了，循环/调用操作码走"数热点"的版本；没开，走"不数热点"的 I 版本。
- **DISPMODE_REC**:是否正在录制 trace。录制时，所有指令的处理都要走一个特殊的"录制分发"(`lj_vm_record`),因为每条字节码都要被录成 IR。
- **DISPMODE_INS**:是否需要"指令级分发覆盖"。开了 line/count hook,或正在录制，或正在 profiling,都需要每条指令都调一次 C 回调(`lj_dispatch_ins`)。
- **DISPMODE_CALL**:是否需要"调用级分发覆盖"。开了 call hook,每次函数调用要调回调。
- **DISPMODE_RET**:类似,return hook。
- **DISPMODE_PROF**:profiling 模式。

这些位不是互斥的，可以叠加。比如"JIT 打开 + 正在录制"就是 `DISPMODE_JIT | DISPMODE_REC`。

### §5.2 切换的核心函数:lj_dispatch_update

切换模式的总入口是 `lj_dispatch_update`(`lj_dispatch.c:106`)。它根据当前的 JIT 状态、hook 掩码，算出新的 mode,如果和旧 mode 不同，就**重写 dispatch 表**。这是个挺长的函数(一百多行),我们抓核心看。

先是算新 mode:

```c
/* Update dispatch table depending on various flags. */
void LJ_FASTCALL lj_dispatch_update(global_State *g, int nolock)
{
  ...
  uint8_t oldmode = g->dispatchmode;
  uint8_t mode = 0;
#if LJ_HASJIT
  mode |= (G2J(g)->flags & JIT_F_ON) ? DISPMODE_JIT : 0;
  mode |= G2J(g)->state != LJ_TRACE_IDLE ?
	    (DISPMODE_REC|DISPMODE_INS|DISPMODE_CALL) : 0;
#endif
#if LJ_HASPROFILE
  mode |= (g->hookmask & HOOK_PROFILE) ? (DISPMODE_PROF|DISPMODE_INS) : 0;
#endif
  mode |= (g->hookmask & (LUA_MASKLINE|LUA_MASKCOUNT)) ? DISPMODE_INS : 0;
  mode |= (g->hookmask & LUA_MASKCALL) ? DISPMODE_CALL : 0;
  mode |= (g->hookmask & LUA_MASKRET) ? DISPMODE_RET : 0;
  if (oldmode != mode) {  /* Mode changed? */
    ...
```

(来源:`lj_dispatch.c:106-124`,节选。)

这就是把各种状态翻译成 mode 位的逻辑。然后，如果 mode 真的变了，进入重写 dispatch 表的部分。重写分几块，我们看最关键的两块。

**第一块:循环/调用操作码的 hotcount 版本切换**

```c
    /* Hotcount if JIT is on, but not while recording. */
    if ((mode & (DISPMODE_JIT|DISPMODE_REC)) == DISPMODE_JIT) {
      f_forl = makeasmfunc(lj_bc_ofs[BC_FORL]);
      f_iterl = makeasmfunc(lj_bc_ofs[BC_ITERL]);
      f_itern = makeasmfunc(lj_bc_ofs[BC_ITERN]);
      f_loop = makeasmfunc(lj_bc_ofs[BC_LOOP]);
      f_funcf = makeasmfunc(lj_bc_ofs[BC_FUNCF]);
      f_funcv = makeasmfunc(lj_bc_ofs[BC_FUNCV]);
    } else {  /* Otherwise use the non-hotcounting instructions. */
      f_forl = disp[GG_LEN_DDISP+BC_IFORL];
      f_iterl = disp[GG_LEN_DDISP+BC_IITERL];
      f_itern = &lj_vm_IITERN;
      f_loop = disp[GG_LEN_DDISP+BC_ILOOP];
      f_funcf = makeasmfunc(lj_bc_ofs[BC_IFUNCF]);
      f_funcv = makeasmfunc(lj_bc_ofs[BC_IFUNCV]);
    }
```

(来源:`lj_dispatch.c:130-144`。)

读懂这段:条件 `(mode & (DISPMODE_JIT|DISPMODE_REC)) == DISPMODE_JIT` 的意思是"JIT 打开 **且** 没在录制"。为什么录制时不走 hotcount?因为录制期间，所有指令都已经走 `lj_vm_record`(会调 `lj_dispatch_ins`/`lj_trace_ins`),不需要再重复数热点。

- **JIT 打开且不录制**:FORL、ITERL、ITERN、LOOP、FUNCF、FUNCV 都指向"会数热点"的版本(`makeasmfunc(lj_bc_ofs[BC_FORL])` 等)。
- **否则(JIT 关，或正在录制)**:都指向"不数热点"的 I 版本。

这六个函数指针(`f_forl` 等)随后被写进 dispatch 表的对应位置(动态段和静态段都写):

```c
    /* Init static counting instruction dispatch first (may be copied below). */
    disp[GG_LEN_DDISP+BC_FORL] = f_forl;
    disp[GG_LEN_DDISP+BC_ITERL] = f_iterl;
    disp[GG_LEN_DDISP+BC_ITERN] = f_itern;
    disp[GG_LEN_DDISP+BC_LOOP] = f_loop;
```

(来源:`lj_dispatch.c:146-149`。注意这里是写到静态段 `disp[GG_LEN_DDISP + ...]`,后面动态段会从静态段拷贝。)

**第二块:整张动态表的覆盖(INS 模式)**

当 INS 位(指令级覆盖)发生变化时，可能要把整张动态表都改了:

```c
    /* Set dynamic instruction dispatch. */
    if ((oldmode ^ mode) & (DISPMODE_PROF|DISPMODE_REC|DISPMODE_INS)) {
      /* Need to update the whole table. */
      if (!(mode & DISPMODE_INS)) {  /* No ins dispatch? */
	/* Copy static dispatch table to dynamic dispatch table. */
	memcpy(&disp[0], &disp[GG_LEN_DDISP], GG_LEN_SDISP*sizeof(ASMFunction));
	/* Overwrite with dynamic return dispatch. */
	if ((mode & DISPMODE_RET)) {
	  disp[BC_RETM] = lj_vm_rethook;
	  disp[BC_RET] = lj_vm_rethook;
	  disp[BC_RET0] = lj_vm_rethook;
	  disp[BC_RET1] = lj_vm_rethook;
	}
      } else {
	/* The recording dispatch also checks for hooks. */
	ASMFunction f = (mode & DISPMODE_PROF) ? lj_vm_profhook :
			(mode & DISPMODE_REC) ? lj_vm_record : lj_vm_inshook;
	uint32_t i;
	for (i = 0; i < GG_LEN_SDISP; i++)
	  disp[i] = f;
      }
    }
```

(来源:`lj_dispatch.c:152-171`。)

这段最戏剧化。两种情况:

- **不需要指令级覆盖(`!(mode & DISPMODE_INS)`)**:把静态段拷一份到动态段(`memcpy`),然后如果开了 return hook,把 RET 类操作码单独指向 `lj_vm_rethook`。这种情况下，绝大多数指令走的是"普通解释"的处理代码。
- **需要指令级覆盖(`mode & DISPMODE_INS`)**:**把动态段的前 `GG_LEN_SDISP` 项，全部指向同一个函数** `f`。这个 `f` 在三种情况里选一:
  - profiling:`lj_vm_profhook`
  - 正在录制:`lj_vm_record`
  - 开了 line/count hook:`lj_vm_inshook`

注意"全部指向同一个函数"——这是最暴力的一种覆盖。当正在录制时，解释器执行**任何**一条字节码,dispatch 都会先跳到 `lj_vm_record`,而 `lj_vm_record` 会调 `lj_dispatch_ins`(`lj_dispatch.c:411`),`lj_dispatch_ins` 里会调 `lj_trace_ins` 把这条字节码录成 IR。这就是为什么"录制时每条字节码都会被录下来"——不是录制器主动去扫字节码，而是**字节码执行时被动地触发录制**。

这是个极其重要的设计:**录制不是"解释器之外多跑一遍",而是"解释器照常跑，只是在每条指令的入口多调一个回调"**。这样录制得到的 IR,严格对应运行时实际执行的路径——这正是 trace compiler 要的"线性热路径"。

### §5.3 一次完整的模式切换:JIT 从关到开

把 §5.2 串起来，看一个具体场景:用户调用 `jit.on()`,JIT 从关闭变成打开。

1. `luaJIT_setmode(L, 0, LUAJIT_MODE_ENGINE|LUAJIT_MODE_ON)`(`lj_dispatch.c:263`),最终走到:
   ```c
   G2J(g)->flags |= (uint32_t)JIT_F_ON;   // 把 JIT_F_ON 位置 1
   lj_dispatch_update(g, 0);               // 触发 dispatch 表更新
   ```
2. `lj_dispatch_update` 算新 mode:`JIT_F_ON` 已设，所以 `mode |= DISPMODE_JIT`。假设没开 hook、没在录制，新 mode = `DISPMODE_JIT`,旧 mode = 0,不同，进入重写。
3. 重写时，因为 `(mode & (DISPMODE_JIT|DISPMODE_REC)) == DISPMODE_JIT` 成立，六个循环/调用操作码指向"数热点"版本(`makeasmfunc(lj_bc_ofs[BC_FORL])` 等)。
4. 写进静态段，再视情况拷到动态段。
5. **额外一步**(`lj_dispatch.c:209-211`):
   ```c
   #if LJ_HASJIT
   /* Reset hotcounts for JIT off to on transition. */
   if ((mode & DISPMODE_JIT) && !(oldmode & DISPMODE_JIT))
     lj_dispatch_init_hotcount(g);
   #endif
   ```
   从关到开，把 hotcount 数组重新初始化成阈值(`lj_dispatch_init_hotcount`,`lj_dispatch.c:86`)。这样之前(可能跑过一阵)积累的计数被清掉，从阈值重新开始数，避免"刚开 JIT 就因为旧计数立刻触发"。

从这一刻起，解释器再跑循环回边时,dispatch[BC_FORL] 指向的是带 hotcount 的汇编代码，开始数热点。等某个循环跑到阈值,§4 的流程启动,trace 编译，字节码 patch 成 JLOOP,机器码接管。

反过来,`jit.off()` 会把 `JIT_F_ON` 位清掉,`lj_dispatch_update` 把那六个操作码改回 I 版本(不数热点),但**已经编译好的 trace 不会立刻失效**——已经被 patch 成 JLOOP 的字节码还会跳进机器码跑，直到显式 `jit.flush()` 或者字节码被 unpatch(见 `lj_trace.c:204` 的 `trace_unpatch`)。这是个细节，但说明 dispatch 表切换和字节码 patch 是两套独立的机制:dispatch 表管"新执行的指令走哪",字节码 patch 管"这条指令本身变成什么"。

---

## §6 为什么这个设计是 sound 的

讲完了机制，这一节专门论证:为什么这套 dispatch + hotcount 的设计，在"快"和"对"之间没有偷工减料。三点。

### §6.1 热点检测的开销真的趋近于零

§3.6 算过:每条循环回边只多付 3 条寄存器指令 + 1 次 L1 cache 访存 + 1 条几乎不跳的条件跳转。但这个结论依赖几个前提，我们逐一验证它们成立:

**前提一:hotcount 槽访问一定快(在 L1 cache 里)。**

64 个槽，每个 2 字节，共 128 字节。现代 CPU 的 L1 cache line 是 64 字节，所以这 128 字节最多占 2 条 cache line。一旦解释器跑起来，这 2 条 cache line 几乎一定常驻 L1(因为每次循环回边都访问它们，访问频率极高)。所以"1 次 L1 访存"的假设成立，实际命中率接近 100%。

**前提二:哈希到同一个槽的冲突不会拖慢正确路径。**

冲突只会让"触发时机"略偏(两条循环共用一个槽，可能比单独计数更早或更晚触发)。但 §3.4 讲过，这不影响正确性，最坏只是 trace 编译的时机不精确。而且，如果一条循环真的很热(跑几百万次),哪怕它的槽和别的冲突，也会很快触发——只是触发次数比例略变。

**前提三:`jb ->vm_hotloop` 这条跳转的代价真的可以忽略。**

这条跳转只在 hotcount 下溢时跳，而下溢只在第 56 次循环回边时发生(默认阈值)。也就是说，每 56 次循环才有 1 次跳转被实际执行，其余 55 次 `jb` 都不跳。分支预测器极快地学会"这条几乎不跳",预测准确率趋近 56/56=98.2%,预测错误的代价分摊到 56 次循环上，微乎其微。

这三点合起来，印证了 §3 的论断:**hotcount 的开销趋近于零**。这是 LuaJIT 解释器即使开着 JIT,也比官方 Lua 快得多的一个重要原因——官方 Lua 没有这套机制，但也没有它的开销；LuaJIT 有这套机制，却几乎不为它付费。这就是工程上的"白吃午餐":靠的是 dispatch 表和 hotcount 共享内存、靠的是递减+借位判定、靠的是只对循环回边数。

### §6.2 模式切换不影响正确性

dispatch 表的切换会不会让程序算错?不会，因为有两道保障。

**第一道:切换是原子的、在安全点发生。**

`lj_dispatch_update` 改 dispatch 表时，解释器不会"半路"读到一半新一半旧的表。因为 `lj_dispatch_update` 总是在某个 C 函数里被调用(比如 `luaJIT_setmode`、`lua_sethook`、trace 状态机推进时),而这些调用点都是"解释器刚好不在 dispatch 循环里"的时刻——要么是外部 API 调用，要么是 trace 录制的状态切换。改完 dispatch 表返回后，解释器下一次 `jmp [DISPATCH+OP*8]` 读到的就是完整的新表。所以不存在"读到 FORL 的新指针、但 ITERL 还是旧指针"这种半新半旧状态。

**第二道:无论 dispatch 表指向哪段代码，执行结果都符合 Lua 语义。**

dispatch 表的三个版本(纯解释 I 版、数热点版、录制版)执行的语义都是一样的:都按 Lua 字节码的语义跑。数热点版只是在 I 版前面多了个"减计数器",减完之后 fall through 到 I 版的代码(§3.6 讲过 `hotloop` 之后 `Fall through`),执行的实际循环逻辑和 I 版完全一样。录制版(`lj_vm_record`)在录 IR 的同时，也会让解释器正常执行这条字节码(`lj_dispatch_ins` 里既调 `lj_trace_ins` 录制，又正常推进解释器)。所以无论 dispatch 表怎么切，用户看到的 Lua 程序行为不变，只是性能/录制状态不同。

### §6.3 触发时机不影响正确性

hotcount 用哈希、用递减、用 64 个槽——这些都会让"触发编译的时机"不那么精确。但这不影响正确性，因为:

- **早触发**:循环还没跑够次数就编译了。最坏只是编译开销花得早一点(可能编译了一段还不够热的代码),但 trace 编译出来后，要么假设成立跑得飞快，要么假设失败 side exit 退回解释器(§0-01 §7-9)。不会算错。
- **晚触发**:循环跑了很久才编译。最坏只是慢一点(多解释几轮),但最终还是会编译。
- **完全不触发**(哈希冲突太严重，或者循环次数本来就不到阈值):那就一直解释。慢，但完全正确。

所以 hotcount 的"不精确"只影响性能，不影响正确性。这是主线"安全"的体现:**正确性由解释器保底,hotcount 只决定什么时候尝试加速**。

---

## §7 ★对照:官方 Lua 与 JVM/V8

把本章的设计放进两个对照里，你会看得更清楚 LuaJIT 的取舍。

### §7.1 对照官方 Lua:没有 JIT,也就没有这套机制

官方 Lua 是纯解释器，它没有 §3 的 hotcount,没有 §2 的 dispatch 表(它用的是 §1.1 那种大 switch),没有 §4 的触发流程，没有 §5 的模式切换。

| 维度 | LuaJIT | 官方 Lua |
|---|---|---|
| 分发方式 | 函数指针表 dispatch(`jmp [DISPATCH+OP*8]`) | 大 switch(每个 case 一种操作码) |
| 执行代码 | 手写汇编(每条字节码一段) | C 函数(switch 的 case 体) |
| 热点检测 | hotcount(64 槽哈希，递减，只在循环/调用) | 无 |
| 模式切换 | 改 dispatch 表(不改字节码) | 无模式，永远解释 |
| 取指译码开销 | 6 条指令(ins_NEXT),无分支 | switch 的比较 + 跳转，分支预测差 |

官方 Lua 没有 dispatch 表，不是因为它不想快，而是因为它**不需要**——没有 JIT,就没有"切换执行模式"的需求,switch 够用了。dispatch 表的价值，恰恰在于它是"解释器与 JIT 之间的桥梁":它让同一份字节码，能在"纯解释 / 数热点 / 录制 / 跑机器码"之间无缝切换，而字节码本身一字不改。这是 JIT 带来的新需求,LuaJIT 用 dispatch 表满足它。

而 dispatch 表的函数指针，指向的是手写汇编——这又解释了 LuaJIT 解释器即使不开 JIT 也比官方 Lua 快的原因:手写汇编比 C switch 省掉了"函数调用开销 + switch 分支预测失败"两笔大头。这是 P1-02 讲过的"手写 VM"的价值，本章的 dispatch 表是它的载体。

### §7.2 对照 JVM/V8:method JIT 怎么发现热点

JVM 和 V8 也有 JIT,也要发现热点。但它们是 method JIT(以整个方法为单位编译),发现热点的方式和 LuaJIT 截然不同。

**JVM(HotSpot)的热点发现:方法计数器 + 回边计数器。**

JVM 给每个**方法**(Java 字节码里的一个 method)配两个计数器:

- **方法调用计数器**(method invocation counter):每次这个方法被调用，加一。
- **回边计数器**(backedge counter):每次这个方法里的循环回边执行，加一。

这两个计数器累加，超过阈值(`-XX:CompileThreshold`,默认 10000 左右),就触发该方法的一次编译(用 C1 或 C2 编译器)。编译以**整个方法**为单位:把整个方法的 Java 字节码翻译成 IR,做优化(逃逸分析、内联、循环展开等),生成覆盖整个方法的机器码。

和 LuaJIT 对比:

| 维度 | LuaJIT | JVM(HotSpot) |
|---|---|---|
| 计数对象 | 字节码指令级(pc 哈希到 64 槽) | 方法级(每个方法两个计数器) |
| 计数方式 | 递减(uint16,下溢触发) | 递减(JVM 也是递减，但每个方法独立) |
| 触发阈值 | hotloop=56(循环)/hotcall(调用) | CompileThreshold≈10000 |
| 计数开销 | 只在循环/调用操作码,3+1 条指令 | 每个方法入口/回边都计数 |
| 编译单位 | trace(一条线性热路径) | method(整个方法) |
| 计数器存储 | 全局 64 槽，共享 | 每个方法自带(存在方法元数据里) |

JVM 的方法计数器是"每个方法独立存储"的，这意味着每个方法都有自己的计数器，没有哈希冲突，计数精确。但代价是:每个方法元数据都要带计数器字段，内存开销大；而且方法数量多时，这些计数器分散在内存各处,cache 局部性差。LuaJIT 反其道:全局只有 64 个槽，所有循环共用，有冲突但 cache 极友好。这是个"精确 vs 紧凑"的取舍,LuaJIT 选了紧凑(因为它目标是嵌入式、轻量)。

**V8 的热点发现(早期):退出内联缓存 + 方法计数。**

V8 早期(2010 年前后)用类似的方法级计数。但 V8 后来(2017+ 的 SparkPlug + Maglev + TurboFan 流水线)演进得更复杂:SparkPlug(基线编译，快速生成未优化的字节码机器码)会为每个函数计数，到阈值交给 Maglev/TurboFan 做优化编译。本质上还是"方法级计数 + 多层编译"。

V8 还大量依赖 **IC(inline cache,内联缓存)**:它在运行时记录"这个操作(比如属性访问)历史上见过什么类型",把类型信息缓存在 IC entry 里，优化编译时用这些 IC 信息做类型推断。这和 LuaJIT 的 hotcount 不是一回事——IC 是"记录类型历史",hotcount 是"数执行次数"。

**根本分歧:为什么 LuaJIT 不学 JVM 用方法计数器?**

因为 trace compiler 不需要方法级信息。LuaJIT 要编译的是"一条线性热路径",它关心的不是"这个方法热不热",而是"这条具体的循环回边热不热"。方法热不热，和"方法里哪条路径热"是两回事——一个方法可能整体调用次数不多，但它内部某个循环跑了几百万次，这个循环就该编译。方法计数器会把这种情况漏掉(方法计数没到阈值),而指令级 hotcount 能精确抓到(那个具体循环的计数会到)。

反过来,JVM 要做 method-level 优化(整个方法的逃逸分析、内联),它需要方法级的计数来判断"这个方法值不值得整体编译"。这是 trace JIT 和 method JIT 在"发现热点"上的根本分歧，根源在于它们的编译单位不同。本章的 hotcount,正是 trace 路线在热点检测上的必然选择:**只有指令级、只数循环回边，才能精准定位"那条线性热路径"**。

---

## §8 回扣主线

这一章我们把 LuaJIT 解释器与 JIT 之间的那道"开关"看透了。

从第一性原理出发:解释器要从一条字节码找到执行代码，最直接的是大 switch,但 switch 慢在分支预测失败；更快的是一张函数指针表 dispatch,操作码做下标,O(1) 跳转。LuaJIT 的 dispatch 表(`GG->dispatch`)就是这张表，它指向手写汇编里每条字节码的处理代码。

但 dispatch 表的本事不止"快"。它最精妙的地方，是它和 hotcount 数组**共享一块连续内存**(`GG_State`),让解释器在执行每条循环回边字节码时，只需一次 `[DISPATCH + 常量偏移 + 哈希]` 的内存访问，就顺带把热点计数器减一——开销趋近于零。这是"在解释的同时数热点，又不拖慢解释"这个看似矛盾的需求的解。hotcount 用 64 个槽、pc>>2 哈希、递减+借位判定，每一处都是为了把这次访问压到最便宜。

hotcount 减到下溢，汇编跳到 `vm_hotloop`/`vm_hotcall`,一行 `lea CARG1, [DISPATCH+GG_DISP2J]` 拿到 jit_State 指针(又是"从 DISPATCH 出发加偏移"),调 `lj_trace_hot` 启动 root trace 录制。录制完成后，字节码被 patch 成 JLOOP,下次直接跳进机器码,hotcount 功成身退。

而这一切的模式切换——JIT 开关、录制开始结束、hook 开关——全靠**改 dispatch 表**这一个机制。字节码本身不动(除了最后 patch 成 JLOOP 那一下),动的是表里每一项指向的代码。这让 LuaJIT 能在"纯解释、数热点、录制、跑机器码"之间灵活切换，且每次切换都是 sound 的:不丢正确性(dispatch 表三版本语义一致),不破坏不变式(切换在安全点发生),触发时机不精确也不影响正确性(解释器保底)。

回到本书主线:**把动态执行安全变成机器码**。这一章讲的是"安全"和"快"在解释器侧如何共存:hotcount 让解释器几乎免费地知道哪段代码热(dispatch 表与 hotcount 共享内存的零开销设计),dispatch 表让模式切换不影响正确性(改表不改字节码)。而"省"——只编译热点、只对循环回边数计数——也贯穿全章。这是 trace JIT 在解释器侧的全部基础设施。

从下一章开始，我们要跨过二分法的另一侧，进入 JIT 侧:一条字节码怎么被录成 IR,IR 怎么被优化，怎么变成机器码。但请记住，这一切的起点，都是本章这道 dispatch 表与 hotcount 筑成的开关——没有它精准、廉价地发现热点,JIT 无从启动；没有它灵活、sound 地切换模式，录制与执行无法共存。**这道开关，是 trace JIT 整条流水线的第一个齿轮。**

---

*下一章 [P1-04 值·Table·状态](P1-04-值Table状态.md):在进入 JIT 录制之前，我们先把解释器侧最后一块拼图看清——LuaJIT 的值是什么长相、Table 怎么实现、lua_State 和 global_State 各管什么(对照官方 Lua,看看"加上 JIT"改了哪些表示)。*
