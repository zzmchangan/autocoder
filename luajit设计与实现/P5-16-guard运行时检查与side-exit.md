# P5-16 guard 运行时检查与 side exit

> **本书主线**:把动态执行安全变成机器码。**二分法**:解释器侧 ↔ JIT 侧。本章在 **JIT 侧·运行时**——机器码已经生成、正在 CPU 上飞奔的那一侧。**★对照**:官方 Lua(无 JIT,故无退出)+ JVM/V8(deoptimization,去优化)。**源码**:LuaJIT 2.1.ROLLING,`lj_asm_x86.h` / `vm_x86.dasc` / `lj_trace.c` / `lj_target*.h` / `lj_jit.h`。**基调**:纯直球,从第一性原理推导 guard 在 CPU 上长什么样、失败时怎么跳出来。

---

## §1 本章解决什么问题:P0-01 留下的那个"跳出去"

P0-01 的 §7 讲 guard,§8 讲 side exit。我们把那两节的结论再摆一遍,然后指出它们**留下了什么没讲**。

P0-01 §7 的结论是:LuaJIT 编译时基于观察做了乐观假设(比如"这个变量 x 在这条路径上永远是整数"),为了不让假设出错时把程序算崩,它在生成的机器码里**插了一条运行时检查**——这条检查叫 guard。guard 的语义就是机器码里的一句"如果假设不成立,就跳走,别往下执行了":

```text
(机器码,大致示意)
  检查 x 的类型标记   ← 这就是 guard
  如果不是整数,跳到 EXIT
  把 x 当整数,做加法  ← 乐观假设成立时执行
  ...
EXIT:
  (退出处理)
```

P0-01 §8 的结论是:guard 触发跳到 EXIT 之后,机器码不能继续(假设已经破产),于是要**退回解释器**。但退回前得把机器码此刻的状态(寄存器里的值)翻译成解释器期望的样子(栈上的值),这个翻译表叫 snapshot。完整动作——退出、靠 snapshot 恢复、退回解释器继续——叫 side exit。

P0-01 把**概念**讲清了。但它有意留下两个问题没有展开:

1. **guard 这条检查,在 CPU 上到底长什么样?** "检查 x 的类型标记"是哪条机器指令?"跳到 EXIT"又是哪条?这两条指令加起来开销多少?为什么我们敢说 guard "开销极低"?
2. **EXIT 那头是什么?** guard 跳过去之后落在哪段代码?那段代码做了什么,才能把控制权安稳地交还解释器?"退回解释器"这个动作,在源码里是哪个函数、走哪条路径?

这两个问题,就是本章的全部内容。它们都属于 **JIT 侧的运行时**:机器码已经在跑了,guard 是机器码里的几条指令,exit stub 是紧挨着机器码的一小段汇编,`lj_trace_exit` 是退出处理的 C 入口。P4-14 讲的是"怎么生成"这些机器码(汇编生成),本章讲的是"它们生成出来之后,运行时怎么执行、怎么跳"。

这一章的核心命题,可以用一句话概括,它也是"乐观假设 + 运行时检查 + 失败可回退"这条主线里**中间那一环**的源码展开:

> **guard 是机器码里的一条比较(cmp)+ 一条条件跳转(jcc);它失败时跳到一段叫 exit stub 的小汇编,exit stub 把退出号压栈后跳进 vm_exit_handler,handler 把所有寄存器保存成 ExitState 再调 C 函数 lj_trace_exit,lj_trace_exit 靠 snapshot 恢复状态、把 pc 设回解释器对应位置,控制权就此交还解释器。**

接下来我们一步步把这句话拆开,每一段都推导清楚、用源码印证。

---

## §2 第一性原理:guard 在 CPU 上必然是"比较 + 条件跳转"

### 2.1 从"假设"到"机器码检查"的推导

我们先不想 LuaJIT,从一个最朴素的问题开始:**给你一个假设"x 是整数",你怎么让 CPU 在运行时检查这个假设成立不成立?**

要检查一个假设,你得先有一件 CPU 能感知的事实,然后拿这个事实去和"假设为真时应满足的条件"对比。CPU 能直接感知的事实,无非两类:

- 寄存器或内存里存的某个**数值**。
- 上一条算术/逻辑指令留下的**条件标志**(x86 上是 RFLAGS 寄存器里的 ZF/CF/SF/OF/PF)。

而 CPU 提供的"对比"动作,本质只有一条:**比较(cmp)或算术逻辑运算**,它不直接产生分支,只负责更新条件标志。更新完标志之后,再用一条**条件跳转(jcc)**去读标志、决定跳不跳。

所以,任何"运行时检查一个条件、不满足就跳走"的逻辑,落到 x86 机器码上,必然是两步:

```text
  <某条更新标志的指令,通常是 cmp/test/算术运算>
  jcc  TARGET     ; 条件跳转:标志满足条件就跳到 TARGET
```

这是 CPU 指令集的物理约束,不是设计选择。cmp 负责算出"条件成不成立",jcc 负责根据"成不成立"决定流向。guard 也不例外——**guard 在机器码里就是一条 cmp(或能更新标志的等价指令)+ 一条 jcc。**

这条推导回答了"guard 在 CPU 上长什么样":它不是什么神秘的东西,就是你在 C 里写 `if (x 的类型 != 整数) goto EXIT;` 编译出来的那两条指令。guard 之所以"安全",是因为它物理上就是一次比较、一次分支,跑过了就说明假设成立。

### 2.2 那具体比较什么?LuaJIT 的值表示

要知道 cmp 比较什么,得先知道 LuaJIT 怎么表示一个 Lua 值。这决定了一个"整数"在内存里到底长什么样,guard 才知道去比哪个字节。

LuaJIT 的值是 **tagged value**(带标记的值):一个值占一个 `TValue` 结构(64 位或 48 位),里面一部分是**真正的数据**(整数本身、或指向对象的指针),另一部分是**类型标记(itype)**,标明这个值是整数、浮点、nil、字符串、表……中的哪一种。

非 GC64 模式(32 位、或 64 位的非 GC64 构建)下,`TValue` 是 8 字节:低 32 位是数据,高 32 位里低 4 字节是类型标记 `ittype`(实际用一个 32 位整数表示)。GC64 模式下,`TValue` 是 8 字节一个 64 位整数,类型标记塞在高 17 位里(用 tag-in-pointer 的方式,省得单独存标记)。

无论哪种,核心是:**类型信息就藏在值本身里,在内存某个固定的字节位置。** guard 要检查"x 是不是整数",就是去读那个位置的字节,拿它和"整数类型的标记值"比。

于是 guard 的具体形态就清楚了:先从值的类型字段所在地址做一次加载(或直接对内存做 cmp),再和"期望类型的标记"比较,再 jcc 跳走。我们等下在 §3 会贴真实的汇编。

### 2.3 为什么 guard 开销极低:分支预测站在乐观假设这边

现在回答一个关键问题:每条机器码里塞了一堆 cmp+jcc,难道不慢吗?

不慢,而且几乎免费。原因是 CPU 的**分支预测器**。

现代 CPU 流水线里,遇到条件跳转时,CPU 不会干等条件算出来才决定取哪条指令。它会**猜**:根据历史记录猜这个跳转大概率跳还是不跳,然后**投机地**沿着猜测的那条路往下取指、执行。等条件真正算出来:

- 猜对了:投机执行的结果直接用,毫无代价,就像那条 jcc 不存在一样。
- 猜错了:把投机执行的指令作废,回到正确的路。这叫**分支预测错误惩罚**,要花十几个到几十个周期。

关键在:guard 的跳转方向是**高度可预测的**。因为 guard 背后的假设是"这条路径上 x 永远是整数"——这是编译器**观察了很多次**才敢做的假设。运行时,假设绝大多数时候都成立,所以 guard 的 jcc 绝大多数时候**不跳**。历史告诉分支预测器:"这个跳转,从来不跳。"于是预测器次次猜"不跳",次次猜对,guard 那条 jcc 在流水线上几乎隐形。

把数字摆出来:一条 cmp 大约 1 个周期(如果操作数已经在寄存器里,且没有访存依赖),一条不跳的 jcc 在预测正确时大约 0 个周期(被流水线吸收)。也就是说,**guard 在假设成立时的真实开销,接近一条 cmp——大约 1 个周期,甚至更少。** 相比整数加法本身的零点几周期,这开销不大;相比解释器为了判类型花掉的几十个周期,这开销小到可以忽略。

这就是 guard 设计的精妙所在:**它把"安全保障"的成本,压缩到了几乎为零。** 代价不是每次都付,而是只在假设偶尔失败的那一次付——而那一次付的代价(退出恢复,见 §5-§7),是一次性的、且之后会触发 side trace(P5-18)把这条路也编译掉,让后续不再付。

### 2.4 一个最小例子:guard 触发到退出的完整旅程

我们把 guard 触发的全过程用一个最小例子演示,之后 §3-§7 会用源码逐段印证这个例子的每一步。

假设有一段 Lua:

```lua
local x = 1
for i = 1, 1000000 do
  x = x + i        -- 录制时观察到 x, i 都是整数
end
```

LuaJIT 录制这条循环时观察到 x、i 都是整数,于是乐观假设"这条 trace 上 x、i 永远是整数",编译成整数加法的机器码,并在前面插 guard 检查类型。机器码大致长这样(伪汇编):

```asm
  ; ---- guard: 检查 i 是整数 ----
  cmp  dword [base+i_ofs+4], LJ_TISNUM   ; 比较高 4 字节的类型标记
  jne  ->exit_stub_N                      ; 不是整数就跳到第 N 号 exit stub
  ; ---- 乐观路径:整数加法 ----
  mov  eax, [base+i_ofs]                  ; 取 i 的整数部分
  add  eax, [base+x_ofs]                  ; 加 x
  mov  [base+x_ofs], eax                  ; 存回 x
  ...                                     ; 循环回边
```

绝大多数循环(假设都成立),cmp 通过,jne 不跳,整数加法飞奔。假设某次 i 真的成了非整数(这个例子不会,换个有外部输入的程序就可能),那么:

1. **cmp 发现类型标记不等**,把 ZF 清零。
2. **jne 读到 ZF=0,跳转**——落在第 N 号 **exit stub** 上。
3. exit stub 是紧挨着 trace 机器码的一小段汇编:它**把退出号 N 压栈**(告诉后续处理"是从哪个 guard 出来的"),然后跳到 `vm_exit_handler`。
4. `vm_exit_handler` 是一段手写汇编(`vm_x86.dasc`):它**把所有通用寄存器、浮点寄存器、溢出槽保存到栈上一个叫 ExitState 的结构里**,然后**调 C 函数 `lj_trace_exit`**。
5. `lj_trace_exit`(`lj_trace.c`):根据退出号定位到是哪条 trace 的哪个 guard,调 `lj_snap_restore`(下章详)——这个函数照着 snapshot 把 ExitState 里的寄存器值搬运、翻译成解释器栈上的 TValue,并算出"解释器该从哪条字节码继续"。
6. `lj_trace_exit` 把那个 pc 写回 C frame,返回。
7. `vm_exit_handler` 的后半段据此**把栈拨回 C frame、恢复解释器现场**,跳进解释器主循环。
8. 解释器从 guard 对应的字节码位置继续——它是完整的 Lua 实现,按真实类型处理,结果正确。

这 8 步就是一次 side exit 的完整旅程。下面 §3-§7 逐步用源码印证。

---

## §3 guard 的机器码形态:`asm_guardcc` 与它生成的 cmp+jcc

### 3.1 所有 guard 共用的出口:`asm_guardcc`

LuaJIT 的后端是按目标 CPU 分文件的,x86/x64 的在 `lj_asm_x86.h`。后端生成机器码时,凡是遇到一个"需要 guard"的 IR 指令(类型检查、循环边界、溢出检查、表查找未命中……),最终都会调到同一个函数去**发射那条条件跳转**——`asm_guardcc`。

我们先看它的定义(`lj_asm_x86.h:75`):

```c
/* Emit conditional branch to exit for guard.
** It's important to emit this *after* all registers have been allocated,
** because rematerializations may invalidate the flags.
*/
static void asm_guardcc(ASMState *as, int cc)
{
  MCode *target = exitstub_addr(as->J, as->snapno);   // 75-1: 目标=本快照对应的 exit stub
  MCode *p = as->mcp;
  if (LJ_UNLIKELY(p == as->invmcp)) {                  // 75-2: 循环反转的特殊路径
    as->loopinv = 1;
    *(int32_t *)(p+1) = jmprel(as->J, p+5, target);
    target = p;
    cc ^= 1;                                           // 条件取反
    ...
    emit_sjcc(as, cc, target);
    return;
  }
  if (LJ_GC64 && LJ_UNLIKELY(as->mrm.base == RID_RIP))
    as->mrm.ofs += 6;
  emit_jcc(as, cc, target);                             // 75-3: 正常路径:发一条 jcc
}
```

这个函数只做一件事:**发射一条条件跳转,跳到当前 snapshot 对应的 exit stub。** 抓住三点:

**(1) 目标地址来自 `exitstub_addr(as->J, as->snapno)`(第 77 行)。** `as->snapno` 是"当前正在处理的 snapshot 编号"。前面 P4-14 讲过,每个 guard 归属一个 snapshot(一组共用同一个退出点的 guard 用同一个 snapshot,共享一个 exit stub)。所以这条 jcc 跳到的不是某个固定地址,而是"当前这个 guard 所属 snapshot 的 exit stub"。exit stub 怎么按编号找地址,见 §4。

**(2) 正常路径调 `emit_jcc(as, cc, target)`(第 93 行)。** `cc` 是条件码(`CC_NE` 不等、`CC_E` 相等、`CC_AE` 大于等于、`CC_O` 溢出……),由调用方根据"这个 guard 在检查什么"决定。`emit_jcc` 负责把这条 jcc 编码成机器码字节,我们 §3.2 马上看。

**(3) 注释里那句"It's important to emit this *after* all registers have been allocated, because rematerializations may invalidate the flags"非常关键。** 它揭示了一个时序约束:guard 的 cmp 和 jcc 必须在寄存器分配完成**之后**才发射。为什么?因为寄存器分配过程中,可能为了腾寄存器而插入"重新计算某个值"(rematerialization)的指令——比如把一个常量重新 mov 进寄存器——而有些 rematerialization 指令(比如 `xor reg,reg`)会**污染条件标志**。如果先发了 cmp、再分配寄存器、中间插了污染标志的指令,jcc 读到的就不是 cmp 留下的标志了,guard 就会错判。所以 asm_guardcc 一定在最后才发 jcc,保证 cmp 和 jcc 之间没有别的东西动过标志。这是 guard 之所以 sound 的一个实现细节(§8 会回到这点)。

### 3.2 jcc 的字节长什么样:`emit_jcc` 与 `emit_sjcc`

`emit_jcc` 在 `lj_emit_x86.h:502`:

```c
/* jcc target */
static void emit_jcc(ASMState *as, int cc, MCode *target)
{
  MCode *p = as->mcp;
  *(int32_t *)(p-4) = jmprel(as->J, p, target);
  p[-5] = (MCode)(XI_JCCn+(cc&15));
  as->mcp = p - 5;
}
```

(这里省略了后面的几行,核心如此。)它生成的是 **near jcc**(长条件跳转),编码是 **6 字节**:`0F 8x rel32`。其中:

- `0F 8x` 是操作码字节,`XI_JCCn=0x80`(`lj_target_x86.h:215`),加上条件码低 4 位拼成 `0F 80`(jo)、`0F 84`(je)、`0F 85`(jne)……
- `rel32` 是 4 字节的相对偏移,由 `jmprel` 算出(`lj_emit_x86.h:493`),即 `target - p`。

也就是说,asm_guardcc 正常路径生成的 guard,在机器码里长这样(以 jne 为例):

```asm
0F 85 [rel32]    ; jne  rel32 -> exit_stub_snapno
```

注意 LuaJIT 后端是**反向发射**(从后往前填字节,`as->mcp` 指针往前走),所以这里看到的是先填 rel32(`p-4`)、再填操作码(`p[-5]`)、再把 `mcp` 前移 5 字节。这是 LuaJIT 后端一个贯穿性的约定,本书 P4-14 详讲。

还有个短跳转版本 `emit_sjcc`(`lj_emit_x86.h:452`):

```c
/* jcc short target */
static void emit_sjcc(ASMState *as, int cc, MCLabel target)
{
  MCode *p = as->mcp;
  ptrdiff_t delta = target - p;
  lj_assertA(delta == (int8_t)delta, "short jump target out of range");
  p[-1] = (MCode)(int8_t)delta;
  p[-2] = (MCode)(XI_JCCs+(cc&15));
  as->mcp = p - 2;
}
```

它生成 **2 字节**的 short jcc:`7x rel8`(`XI_JCCs=0x70`,`lj_target_x86.h:214`)。只有目标在 ±127 字节内才用得了。guard 到 exit stub 的距离通常超过 127 字节,所以 guard 主路径用 `emit_jcc`(6 字节);`emit_sjcc` 主要用在循环反转(asm_guardcc 的 invmcp 分支)和 trace 内部的局部跳转。

### 3.3 cmp 从哪来:谁负责在 jcc 前发比较

注意 `asm_guardcc` **只发 jcc,不发 cmp**。那 cmp 是谁发的?是**调用 asm_guardcc 的那个具体 IR 的汇编函数**。每个 guard 类 IR,在生成自己专属的比较逻辑之后,调 asm_guardcc 把"失败就跳"那一步补上。我们看几个最典型的,理解 guard 的 cmp 千变万化、但 jcc 殊途同归。

**典型一:类型检查(SLOAD,最常见的 guard)。** 加载一个栈槽并检查类型。非 GC64 模式下(`lj_asm_x86.h:1804` 起):

```c
  if ((ir->op2 & IRSLOAD_TYPECHECK)) {
    /* Need type check, even if the load result is unused. */
    asm_guardcc(as, irt_isnum(t) ? CC_AE : CC_NE);          // 发 jcc:不等则跳(CC_NE)
    ...
    } else {
      emit_i8(as, irt_toitype(t));                           // 期望的类型标记(立即数)
      emit_rmro(as, XO_ARITHi8, XOg_CMP, base, ofs+4);      // cmp [base+ofs+4], itype
    }
  }
```

(见 `lj_asm_x86.h:1804-1842`,此处是非 GC64 分支。)这段生成的机器码是(以检查整数为例):

```asm
80 7F [ofs+4] [itype]    ; cmp byte [base+ofs+4], itype   (cmp r/m8, imm8)
0F 85 [rel32]            ; jne ->exit_stub_snapno          (asm_guardcc 发的)
```

也就是:从栈槽 `base+ofs` 往上偏 4 字节(类型标记所在),和"期望类型的标记值 `itype`"做一次 8 位比较;asm_guardcc 再发一条 jne,不等就跳。这就是 §2.4 例子里那个 guard 的真实样子。注意 `irt_isnum(t) ? CC_AE : CC_NE`:浮点检查用"无符号大于等于"(因为 LuaJIT 把数字类型的标记排在一段连续区间,小于某个边界就不是数字),整数/对象检查用"不等"。条件码的选择完全由值表示的编码决定,不是任意的。

GC64 模式下(`lj_asm_x86.h:1769` 起)形态不同——类型标记在 64 位值的高 17 位,所以是 `mov r64,[addr]; ror r64,47; cmp r16,itype; jne`(见源码注释 `lj_asm_x86.h:1769-1776`),但本质没变:把类型标记搬到一处、比较、jcc 跳。本书行号统一标到 2.1.ROLLING 源码,32 位非 GC64 是教学上最直观的形态,我们以它为主。

**典型二:整数运算溢出(ADDOV 等)。** 检查加法有没有溢出整数范围(`lj_asm_x86.h:2132`):

```c
  if (irt_isguard(ir->t))  /* For IR_ADDOV etc. */
    asm_guardcc(as, CC_O);   // 溢出标志置位则跳
```

前面发了真正的 `add` 指令,add 本身会更新 OF(溢出标志);这里 asm_guardcc 发一条 `jo`(jump if overflow,`CC_O`)。机器码:

```asm
01 ...            ; add ...
0F 80 [rel32]    ; jo ->exit_stub_snapno
```

这个 guard 连 cmp 都省了——直接复用加法自己更新的溢出标志。非常省:guard 的额外开销就是一条 jo(且预测正确时近乎免费)。

**典型三:栈溢出检查(asm_stack_check)。** 检查 Lua 栈有没有溢出(`lj_asm_x86.h:2707`):

```c
static void asm_stack_check(ASMState *as, BCReg topslot,
			    IRIns *irp, RegSet allow, ExitNo exitno)
{
  ...
  emit_jcc(as, CC_B, exitstub_addr(as->J, exitno));   // jb exitstub
  ...
  emit_gri(as, XG_ARITHi(XOg_CMP), r|REX_GC64, (int32_t)(8*topslot));  // cmp r, 8*topslot
  ...
}
```

注意这里它**没用 asm_guardcc,直接调 emit_jcc**——因为它检查的不是"假设成立否",而是"栈够不够",且它的 exitno 是单独传进来的(`exitstub_addr(as->J, exitno)`,见第 2713 行)。但形态完全一样:一条 cmp + 一条 jcc(这里是 `jb`,below 即无符号小于)。这印证了 §2.1 的推导:**一切运行时检查,落到机器码都是 cmp+jcc。** asm_guardcc 只是"检查 snapshot 对应的 guard"这条主路径的封装,本质和这里直发的 emit_jcc 是一回事。

### 3.4 一条 guard 的完整字节成本

把上面合起来,一个典型类型检查 guard,在 trace 机器码里占:

- cmp:2-4 字节(取决于是 cmp r/m8,imm8 还是 cmp r/m32,imm32)。
- jcc:6 字节(near jcc,`0F 8x rel32`)。

合计大约 8-10 字节,运行时约 1 个周期(假设成立、分支预测正确)。一个 trace 里可能有几十个 guard,加起来几百字节机器码、每条近 1 周期——但**这些周期在分支预测正确时大多被流水线吸收**,对吞吐的影响极小。这就是为什么 LuaJIT 敢在每条乐观假设上都插 guard:成本可控,收益(正确性)巨大。

---

## §4 exit stub:guard 失败跳到的"出口"

### 4.1 exit stub 是什么、放在哪

guard 的 jcc 跳到的目标,叫 **exit stub**(出口桩)。它是**一小段汇编**,紧跟在 trace 的机器码附近(在同一块可执行内存里)。

为什么需要这段单独的汇编,而不能让 jcc 直接跳进 C 函数 `lj_trace_exit`?两个原因:

**(1) 要告诉退出处理"我是从哪个 guard 出来的"。** 一条 trace 可能有几十个 guard,每个对应一个 snapshot。退出处理必须知道是哪个 guard 触发的,才能取对应的 snapshot 恢复状态。但 jcc 本身不带"我是第几号"的信息——它只是跳到一个地址。LuaJIT 的办法是:**给每个 guard 分配一个独立的、地址不同的小汇编(exit stub),这段汇编的唯一职责就是把"自己的编号"压到栈上**,然后再跳到统一的处理入口。这样处理入口一读栈顶,就知道是第几号 guard。

**(2) 寄存器此时还是机器码的布局,还没保存。** 直接跳进 C 函数会破坏这些寄存器(C 调用约定要用某些寄存器)。需要一段汇编把所有寄存器先保存到一个固定结构里(ExitState),才能安全调 C。这段保存工作放在 `vm_exit_handler`(§5),exit stub 只负责"传退出号 + 跳到 handler"。

### 4.2 exit stub 的生成:`asm_exitstub_gen`

exit stub 不是手写的,是后端在生成 trace 机器码时**一并生成**的。入口是 `asm_exitstub_setup`(`lj_asm_x86.h:51`),它会按需为每个 group 调 `asm_exitstub_gen` 生成一组 stub。我们先看核心的生成函数(`lj_asm_x86.h:9`):

```c
/* Generate an exit stub group at the bottom of the reserved MCode memory. */
static MCode *asm_exitstub_gen(ASMState *as, ExitNo group)
{
  ExitNo i, groupofs = (group*EXITSTUBS_PER_GROUP) & 0xff;
  MCode *target = (MCode *)(void *)lj_vm_exit_handler;        // 9-1: 统一跳到 vm_exit_handler
  MCode *mxp = as->mcbot;
  MCode *mxpstart = mxp;
  ...
  /* Push low byte of exitno for each exit stub. */            // 9-2: 压退出号低字节
  *mxp++ = XI_PUSHi8; *mxp++ = (MCode)groupofs;
  for (i = 1; i < EXITSTUBS_PER_GROUP; i++) {
    *mxp++ = XI_JMPs; *mxp++ = (MCode)((2+2)*(EXITSTUBS_PER_GROUP - i) - 2);  // 跳到组尾
    *mxp++ = XI_PUSHi8; *mxp++ = (MCode)(groupofs + i);
  }
  /* Push the high byte of the exitno for each exit stub group. */  // 9-3: 压退出号高字节
  *mxp++ = XI_PUSHi8; *mxp++ = (MCode)((group*EXITSTUBS_PER_GROUP)>>8);
#if !LJ_GC64
  /* Store DISPATCH at original stack slot 0. Account for the two push ops. */
  *mxp++ = XI_MOVmi;                                           // 9-4: 顺手存 DISPATCH(非 GC64)
  *mxp++ = MODRM(XM_OFS8, 0, RID_ESP);
  *mxp++ = MODRM(XM_SCALE1, RID_ESP, RID_ESP);
  *mxp++ = 2*sizeof(void *);
  *(int32_t *)mxp = ptr2addr(J2GG(as->J)->dispatch); mxp += 4;
#endif
  /* Jump to exit handler which fills in the ExitState. */     // 9-5: 跳 handler
  if (jmprel_ok(mxp + 5, target)) {  /* Direct jump. */
    *mxp++ = XI_JMP; mxp += 4;
    *((int32_t *)(mxp-4)) = jmprel(as->J, mxp, target);
  } else { /* RIP-relative indirect jump. */
    ...
  }
  ...
  return mxpstart;
}
```

抓住这段生成的结构。它生成的是**一组** exit stub(一个 group,共 `EXITSTUBS_PER_GROUP=32` 个,见 §4.3),布局是:

```asm
; ---- 第 0 号 stub(组首,groupofs+0)----
6A [groupofs]          ; push imm8  低字节 = 组内 0 + group*32
EB [jmp to tail]       ; jmp  组尾(高字节 push + 跳 handler)
; ---- 第 1 号 stub ----
6A [groupofs+1]        ; push imm8  低字节 = 组内 1
EB [jmp to tail]
; ... 共 32 个,每个 4 字节(push imm8 + jmp short)...
; ---- 组尾(所有 stub 殊途同归到这里)----
6A [group*32 >> 8]     ; push imm8  高字节
; (非 GC64 还有一条 mov [esp+...], DISPATCH)  ; 把 dispatch 指针存回栈槽 0
E9 [rel32]             ; jmp lj_vm_exit_handler
```

关键设计:

**(1) 退出号用两次 push imm8 编码成 16 位(9-2、9-3)。** 一个 trace 的 guard 可能超过 256 个(大 trace),所以退出号是 16 位。但 push imm8 只能压一个字节。LuaJIT 的办法:每个 stub 先压"组内偏移"(低字节),再 jmp 到组尾;组尾统一压"组号高字节"。这样两次 push 拼出完整 16 位退出号。`vm_exit_handler` 那边读栈上这两个字节就能还原(`vm_x86.dasc:2812-2813`,见 §5.2)。

**(2) 每个 stub 只占 4 字节(`6A xx EB xx`),极其紧凑。** 这是精心算过的:`push imm8`(2 字节)+ `jmp short`(2 字节)= 4 字节。组内 32 个 stub 共 128 字节,加上组尾十几个字节。一整组 exit stub 才约 140 字节机器码。这种"组尾共享高字节 push + 跳 handler"的设计,把每个 guard 的退出开销压到了 4 字节机器码。

**(3) 组尾那条 `jmp lj_vm_exit_handler`(9-5)。** 这是真正的"跳出机器码区、进入退出处理"的跳转。`lj_vm_exit_handler` 是 `vm_x86.dasc` 里手写的汇编符号(§5)。这里有个细节:LuaJIT 先试直接跳(`jmprel_ok`),如果 `lj_vm_exit_handler` 离得太远(超出 rel32 范围,只在 64 位某些内存布局下可能),就改成 RIP 间接跳(9-5 的 else 分支)。这是 2.1 ROLLING 在 64 位大地址空间下的稳健处理,老资料常略。

### 4.3 分组与寻址:`exitstub_addr`、`EXITSTUBS_PER_GROUP`、`LJ_MAX_EXITSTUBGR`

为什么 exit stub 要**分组**?因为单个 trace 的 guard 多到一定程度,组内 stub 用 jmp short(±127)够不到组尾了。LuaJIT 的限制是:

```c
/* lj_target_x86.h:163-165 */
/* Limited by the range of a short fwd jump (127): (2+2)*(32-1)-2 = 122. */
#define EXITSTUB_SPACING	(2+2)
#define EXITSTUBS_PER_GROUP	32
```

注释把数学讲清了:每个 stub 4 字节(`2+2`),组内 32 个,最后一个到组尾的距离是 `(2+2)*(32-1)-2 = 122`,刚好小于 jmp short 的 127 上限。所以**一组最多 32 个 stub**。一个 trace 的 guard 超过 32,就再开一组;组数有上限 `LJ_MAX_EXITSTUBGR=16`(`lj_def.h:87`):

```c
#define LJ_MAX_EXITSTUBGR	16	/* Max. # of exit stub groups. */
```

所以一条 trace 最多 `32*16 = 512` 个 guard(超出报 `LJ_TRERR_SNAPOV`,见 `asm_exitstub_setup` 第 55 行)。绝大多数 trace 远到不了这个数。

给定一个退出号 `exitno`,怎么找到它对应的 stub 地址?用 `exitstub_addr`(`lj_target.h:152`):

```c
/* Return the address of an exit stub. */
static LJ_AINLINE char *exitstub_addr_(char **group, uint32_t exitno)
{
  lj_assertX(group[exitno / EXITSTUBS_PER_GROUP] != NULL,
	     "exit stub group for exit %d uninitialized", exitno);
  return (char *)group[exitno / EXITSTUBS_PER_GROUP] +
	 EXITSTUB_SPACING*(exitno % EXITSTUBS_PER_GROUP);
}
#define exitstub_addr(J, exitno) \
  ((MCode *)exitstub_addr_((char **)((J)->exitstubgroup), (exitno)))
```

逻辑:`exitno / 32` 算出在第几组(查 `J->exitstubgroup[group]` 拿组首地址),`exitno % 32` 算出组内第几个,乘 `EXITSTUB_SPACING(=4)` 字节偏移得到 stub 地址。`J->exitstubgroup` 是 `jit_State` 里的数组(`lj_jit.h:483`):

```c
  MCode *exitstubgroup[LJ_MAX_EXITSTUBGR];  /* Exit stub group addresses. */
```

它**跨 trace 共享**——不是每条 trace 一组 stub,而是整个 JIT 状态(J)维护若干组 stub 供所有 trace 复用。这点要记一下,§6 会用到:`lj_trace_unwind` 就是靠这个共享数组从机器码地址反查退出号的。

asm_guardcc 第 77 行 `exitstub_addr(as->J, as->snapno)`,就是把"当前 snapshot 编号"当退出号,查到对应 stub 地址,作为 jcc 的目标。**snapshot 编号 = exit stub 编号**,这是一一对应的关键(下章 P5-17 会展开 snapshot)。

### 4.4 setup:何时生成这些 stub

`asm_exitstub_setup`(`lj_asm_x86.h:51`)在汇编一条 trace 的开头被调(`lj_asm_x86.h:3020`):

```c
  asm_exitstub_setup(as, as->T->nsnap);   // 按 snapshot 数(=guard 数上限)生成 stub
```

它按 trace 的 `nsnap`(snapshot 个数)生成够用的 stub 组。已存在的组复用(`as->J->exitstubgroup[i] == NULL` 才生成,见第 67 行),所以 stub 是**惰性生成、跨 trace 复用**的。

---

## §5 vm_exit_handler:把寄存器保存成 ExitState

exit stub 组尾跳到的 `lj_vm_exit_handler`,是 LuaJIT 手写汇编 VM(`vm_x86.dasc`)里的一段。它的职责:**把此刻所有寄存器的值,保存到栈上一个固定结构(ExitState)里,然后调 C 函数 lj_trace_exit。**

我们看 x64 非 Windows 调用约定下的主体(`vm_x86.dasc:2805` 起):

```asm
// Called from an exit stub with the exit number on the stack.
// The 16 bit exit number is stored with two (sign-extended) push imm8.
->vm_exit_handler:
.if JIT
.if X64
  push r13; push r12
  push r11; push r10; push r9; push r8
  push rdi; push rsi; push rbp; lea rbp, [rsp+88]; push rbp
  push rbx; push rdx; push rcx; push rax            // 5-1: 保存所有通用寄存器
  movzx RC, byte [rbp-8]            // 5-2: 还原退出号低字节(exit stub 压的)
  mov RCH, byte [rbp-16]            //      还原退出号高字节
  mov [rbp-8], r15; mov [rbp-16], r14   // 5-3: 把 r14/r15 也存进去(凑齐全部 GPR)
.else
  ...   // 32 位版本类似
.endif
  // Caveat: DISPATCH is ebx.
  mov DISPATCH, [ebp]                               // 5-4: 取回 dispatch(非 GC64 exit stub 存的)
  mov RA, [DISPATCH+DISPATCH_GL(vmstate)]  // 取当前 trace 号(vmstate 存的就是 traceno)
  set_vmstate EXIT                                  // 5-5: 把 vmstate 置为 EXIT
  mov [DISPATCH+DISPATCH_J(exitno)], RC   // 记下退出号到 J->exitno
  mov [DISPATCH+DISPATCH_J(parent)], RA   // 记下 trace 号到 J->parent
.if X64
  ...
  sub rsp, 16*8                 // 5-6: 腾地方存 16 个 xmm
  add rbp, -128
  movsd qword [rbp-8],   xmm15; movsd qword [rbp-16],  xmm14
  ...                                              //      保存所有 xmm(浮点寄存器)
  movsd qword [rbp-120], xmm1;  movsd qword [rbp-128], xmm0
.endif
  mov L:RB, [DISPATCH+DISPATCH_GL(cur_L)]
  mov BASE, [DISPATCH+DISPATCH_GL(jit_base)]
  mov aword [DISPATCH+DISPATCH_J(L)], L:RBa
  mov L:RB->base, BASE
  ...
  lea FCARG2, [rsp+16]            // 5-7: 第二个参数 = ExitState 的地址
  lea FCARG1, [DISPATCH+GG_DISP2J] // 5-8: 第一个参数 = jit_State* J
  mov dword [DISPATCH+DISPATCH_GL(jit_base)], 0    // 5-9: 清 jit_base(标记"已离开机器码")
  call extern lj_trace_exit@8    // (jit_State *J, ExitState *ex)   5-10: 调 C 入口
  ...
.endif
```

这段值得逐点讲清:

**(5-1、5-3、5-6)保存全部寄存器。** 机器码运行时,所有通用寄存器(rax...r15)和所有浮点寄存器(xmm0...xmm15)都可能装着有用的值(运算结果、临时值、REF_BASE 基址等)。退出处理要把这些全保存下来,因为 snapshot 恢复时要按"哪个寄存器对应哪个栈槽"把它们搬运到解释器栈上。保存的位置就是栈上即将成为 `ExitState` 的那块内存。`ExitState` 结构(`lj_target_x86.h:157`)长这样:

```c
typedef struct {
  lua_Number fpr[RID_NUM_FPR];	/* Floating-point registers. */
  intptr_t gpr[RID_NUM_GPR];	/* General-purpose registers. */
  int32_t spill[256];		/* Spill slots. */
} ExitState;
```

三个数组:浮点寄存器、通用寄存器、**溢出槽(spill slots)**。溢出槽是什么?寄存器不够用时,后端会把某些 IR 值"溢出"到 trace 自己的一小块栈区域里(`spill` 数组),退出时这些值也得能被 snapshot 找到,所以 ExitState 把 spill 区也装进来。vm_exit_handler 保存寄存器的顺序和位置,必须和这个结构对齐——所以源码注释 `lj_target_x86.h:156` 写"This definition must match with the *.dasc file(s)"。

**(5-2)还原退出号。** exit stub 压了两个字节(低字节在 `[rbp-8]`、高字节在 `[rbp-16]`,因为 5-1 的 push 顺序把 rbp 设在了它们之上)。这两条 `movzx` 把它们读出来拼到 RC(实际是 edx/eax)里,得到 16 位退出号。这是 §4.2"两次 push imm8 编码 16 位"的接收端。

**(5-4)取回 DISPATCH。** 非 GC64 模式下,exit stub 组尾还顺手把 dispatch 指针存到了栈槽 0(§4.2 的 9-4)。这里取回来。dispatch 是全局状态指针表,vm_exit_handler 后面要用它定位 J、L、vmstate 等。GC64 模式下不需要这步(dispatch 在专用寄存器里)。

**(5-5)set_vmstate EXIT。** 把全局 vmstate 从"正在跑 trace N"改成"EXIT"(退出中)。这是个调试/可观测标记,也用于 unwind(§6 的 lj_trace_unwind 在 EXITTRACE_VMSTATE 模式下会读 vmstate 定位 trace)。

**(5-7、5-8)准备 C 调用参数。** System V ABI 下,第一个参数(jit_State* J)放 rdi,第二个(ExitState*)放 rsi。这里 `lea FCARG1, [DISPATCH+GG_DISP2J]` 算出 J 的地址(dispatch 结构里偏移 GG_DISP2J 处就是 jit_State),`lea FCARG2, [rsp+16]` 算出刚保存的 ExitState 的栈地址。

**(5-9)清 jit_base。** `jit_base` 标记"当前是否在机器码里"(非 0 表示在跑 trace)。这里清 0,表示"我已经退出机器码、回到 C/解释器侧了"。这是个关键状态标记,解释器 dispatch 会查它。

**(5-10)call lj_trace_exit。** 真正进入 C 世界。约定是 `lj_trace_exit@8`(两个指针参数共 8 字节,32 位 STDCALL 命名;64 位实际是寄存器传参,这个符号名只是 dasc 里的统一写法)。下一节展开这个函数。

vm_exit_handler 的后半段(从 `mov RAa, L:RB->cframe` 开始)是 `lj_trace_exit` 返回**之后**的事:把栈拨回 C frame、恢复解释器需要的 BASE/PC/DISPATCH,然后跳进解释器主循环(`->vm_exit_interp`)。这部分 §7 讲。

---

## §6 lj_trace_exit:退出处理的 C 入口

### 6.1 函数签名与职责

`lj_trace_exit` 是退出处理的 C 入口(`lj_trace.c:886`):

```c
/* A trace exited. Restore interpreter state. */
int LJ_FASTCALL lj_trace_exit(jit_State *J, void *exptr)
{
  ERRNO_SAVE
  lua_State *L = J->L;
  ExitState *ex = (ExitState *)exptr;
  ExitDataCP exd;
  int errcode, exitcode = J->exitcode;
  TValue exiterr;
  const BCIns *pc, *retpc;
  void *cf;
  GCtrace *T;

  setnilV(&exiterr);
  if (exitcode) {  /* Trace unwound with error code. */   // 6-1: 错误退出特殊处理
    J->exitcode = 0;
    copyTV(L, &exiterr, L->top-1);
  }
  ...
  T = traceref(J, J->parent); UNUSED(T);
  ...
#ifdef EXITSTATE_CHECKEXIT
  if (J->exitno == T->nsnap) {  /* Treat stack check like a parent exit. */  // 6-2
    ...
    J->exitno = T->ir[REF_BASE].op2;
    J->parent = T->ir[REF_BASE].op1;
    T = traceref(J, J->parent);
  }
#endif
  lj_assertJ(T != NULL && J->exitno < T->nsnap, "bad trace or exit number");
  exd.J = J;
  exd.exptr = exptr;
  errcode = lj_vm_cpcall(L, NULL, &exd, trace_exit_cp);    // 6-3: 保护调用恢复
  if (errcode)
    return -errcode;

  ...
  pc = exd.pc;                                              // 6-4: 取回恢复后的 pc
  cf = cframe_raw(L->cframe);
  setcframe_pc(cf, pc);                                     // 6-5: 写回 C frame 的 pc
  ...
  if (J->flags & JIT_F_ON) {
    trace_hotside(J, pc);                                   // 6-6: 检测是否触发 side trace
  }
  ...
  switch (bc_op(*pc)) {                                     // 6-7: 按 pc 处的字节码返回 MULTRES
  case BC_CALLM: ...
  ...
  }
}
```

这个函数是退出处理的"总调度"。抓住几条主线:

**(6-3、下章详)保护调用 `trace_exit_cp`。** 真正的恢复工作在 `trace_exit_cp`(`lj_trace.c:835`)里,它调 `lj_snap_restore`(P5-17 详)把 ExitState 翻译成解释器栈、并算出"解释器该从哪条字节码继续"。为什么包一层 `lj_vm_cpcall`(protected call)?因为 `lj_snap_restore` **可能抛错**(比如恢复过程中发现表要 resize、触发 `__gc` 等)。一旦抛错,必须有正确的 Lua 异常机制接住,不能让异常穿过 C 代码(`lj_vm_cpcall` 就是干这个的,它 setjmp 了一层)。`trace_exit_cp` 的源码很短:

```c
/* Need to protect lj_snap_restore because it may throw. */
static TValue *trace_exit_cp(lua_State *L, lua_CFunction dummy, void *ud)
{
  ExitDataCP *exd = (ExitDataCP *)ud;
  cframe_errfunc(L->cframe) = 0;
  cframe_nres(L->cframe) = -2*LUAI_MAXSTACK*(int)sizeof(TValue);
  exd->pc = lj_snap_restore(exd->J, exd->exptr);   // 恢复,返回解释器该跑的 pc
  UNUSED(dummy);
  return NULL;
}
```

恢复完,`exd->pc` 装着"解释器继续位置"。

**(6-4、6-5)把 pc 写回 C frame。** `setcframe_pc(cf, pc)` 把恢复出的 pc 写到当前 C frame 的 SAVE_PC 字段。这样 vm_exit_handler 后半段(§7)从 C frame 读 pc 时,读到的就是"正确的继续位置"。

**(6-6)trace_hotside:可能触发 side trace。** 这一步是 side exit "越跑越快"的根源。`trace_hotside`(`lj_trace.c:799`):

```c
/* Check for a hot side exit. If yes, start recording a side trace. */
static void trace_hotside(Jit_State *J, const BCIns *pc)
{
  SnapShot *snap = &traceref(J, J->parent)->snap[J->exitno];
  if (!(J2G(J)->hookmask & (HOOK_GC|HOOK_VMEVENT)) &&
      isluafunc(curr_func(J->L)) &&
      snap->count != SNAPCOUNT_DONE &&
      ++snap->count >= J->param[JIT_P_hotexit]) {     // 累计退出次数到阈值
    ...
    J->state = LJ_TRACE_START;
    lj_trace_ins(J, pc);                               // 启动录制 side trace
  }
}
```

每个 snapshot 有个 `count` 字段(`lj_jit.h:187`),记"这个 guard 退出过几次"。每次 side exit 到这里,`++snap->count`。到了阈值 `JIT_P_hotexit`(默认 10,`lj_jit.h:117`),就认定这个退出点"够热"了,从它**重新录一条 trace**——这就是 side trace(P5-18 详)。`SNAPCOUNT_DONE=255`(`lj_jit.h:190`)表示"这个退出点已经编译过 side trace 并链上了,别再录",防止重复。

这一步是 side exit 和 side trace 的衔接点:大多数 guard 永不触发(假设稳定);偶尔触发的,先忍受几次解释器;触发达阈值,就把它也编译成机器码,以后这条路也不用退了。**这就是 JIT"越跑越快"的机制。**

**(6-7)返回 MULTRES。** 返回值的处理是为了照顾那些"返回多值"的字节码(BC_CALLM/BC_RETM/BC_TSETM 等)。解释器有些指令的语义是"把栈顶到某处的多个值一起处理",退出恢复后栈顶位置可能和机器码里不同,需要算清楚告诉解释器。这是和解释器协议的细节,不影响主线。

### 6.2 两种定位"哪条 trace"的方式

注意 `lj_trace_exit` 一进来就用 `J->parent` 和 `J->exitno`。这两个值是谁设的?是 vm_exit_handler 的 5-5 那段:

```asm
  mov RA, [DISPATCH+DISPATCH_GL(vmstate)]  // vmstate 里存着当前 traceno
  ...
  mov [DISPATCH+DISPATCH_J(exitno)], RC    // 写 J->exitno
  mov [DISPATCH+DISPATCH_J(parent)], RA    // 写 J->parent
```

也就是说,**vmstate 在跑 trace 时被设成 traceno**(trace 一进入就把 vmstate 设成自己的号),exit handler 读它就知道是哪条 trace。这是 `EXITTRACE_VMSTATE=1`(`lj_target_x86.h:167`)的方案,x86/x64 用这个。

但有些目标(或 unwind 路径)没有这个便利,得靠**机器码地址反查**。`trace_exit_find`(`lj_trace.c:872`)就是干这个的:

```c
/* Determine trace number from pc of exit instruction. */
static TraceNo trace_exit_find(jit_State *J, MCode *pc)
{
  TraceNo traceno;
  for (traceno = 1; traceno < J->sizetrace; traceno++) {
    GCtrace *T = traceref(J, traceno);
    if (T && pc >= T->mcode && pc < (MCode *)((char *)T->mcode + T->szmcode))
      return traceno;
  }
  lj_assertJ(0, "bad exit pc");
  return 0;
}
```

线性扫所有 trace,看 `pc` 落在谁的 `[mcode, mcode+szmcode)` 区间里。这是 unwind(`lj_trace_unwind`)在异常处理时的兜底路径,`EXITSTATE_PCREG` 模式下 `lj_trace_exit` 开头(905 行)也用它。

### 6.3 从机器码地址反查退出号:二分查找 snapshot

`lj_trace_unwind`(`lj_trace.c:978`,异常栈展开时用)展示了一个更精细的反查:已知退出的机器码地址,要算出是哪个 guard(退出号)。它靠 snapshot 的 `mcofs` 字段二分:

```c
uintptr_t LJ_FASTCALL lj_trace_unwind(jit_State *J, uintptr_t addr, ExitNo *ep)
{
  ...
  GCtrace *T = traceref(J, traceno);
  if (T ...) {
    SnapShot *snap = T->snap;
    SnapNo lo = 0, exitno = T->nsnap;
    uintptr_t ofs = (uintptr_t)((MCode *)addr - T->mcode);  // 退出地址在 mcode 里的偏移
    /* Rightmost binary search for mcode offset to determine exit number. */
    do {
      SnapNo mid = (lo+exitno) >> 1;
      if (ofs < snap[mid].mcofs) exitno = mid; else lo = mid + 1;
    } while (lo < exitno);
    exitno--;
    *ep = exitno;
    ...
  }
}
```

每个 snapshot 记一个 `mcofs`(`lj_jit.h:183`,"Offset into machine code in MCode units")——它对应的 guard 的 jcc 在 trace 机器码里的偏移。给定退出地址,算出偏移 `ofs`,在 `snap[].mcofs` 数组上二分,找到 `ofs` 落在第几个区间,就是第几号 guard。这印证了 §4.3 的"snapshot 编号 = exit stub 编号":这里用 mcofs 反推出来的 exitno,正是 exit stub 压栈的那个号。

这个反查平时不用(x86 用 vmstate 直接拿 traceno、用 exit stub 压栈的号直接拿 exitno),只在异常栈展开时用。但它揭示了 snapshot、机器码地址、退出号三者的一一对应关系,是 side exit 机制 sound 的一个几何保证。

---

## §7 退出后回解释器:vm_exit_interp

`lj_trace_exit` 返回后,控制权回到 `vm_exit_handler` 的后半段(`vm_x86.dasc:2866` 起):

```asm
  // lj_trace_exit 返回后,RD(eax) 里是 MULTRES 或负的错误码
  mov RAa, L:RB->cframe
  and RAa, CFRAME_RAWMASK            // 7-1: 拨回 C frame 的原始栈指针
  mov rsp, RAa                       // (64 位)重置栈到 C frame
  mov [RAa+CFRAME_OFS_L], L:RB       // 7-2: 设 SAVE_L
  mov BASE, L:RB->base               // 7-3: 取回解释器 base(恢复时设好的)
  mov PC, [RAa+CFRAME_OFS_PC]        // 7-4: 取 SAVE_PC(就是 lj_trace_exit 写的 pc)
  jmp >1
  ...
->vm_exit_interp:
  // RD = MULTRES or negated error code, BASE, PC and DISPATCH set.
.if JIT
  // 恢复解释器需要、但机器码借用的几个 callee-save 寄存器
  ...
.endif
```

抓住主线:

**(7-1)拨回栈。** 机器码运行时,栈指针可能已经被 trace 用得乱七八糟(trace 会在栈上分配自己的 spill 区)。退出处理要把栈**拨回 C frame 的位置**——也就是当初进入 trace 前、C 调用层的栈顶。`cframe_raw(L->cframe)` 给出这个原始栈地址,`mov rsp, RAa` 重置。这一步后,栈就回到了"好像从没进过机器码"的状态(只是栈上的值已经被 snapshot 恢复成了解释器的样子)。

**(7-3、7-4)取回 BASE、PC。** `BASE` 是解释器的栈基址(当前函数栈帧的起点),`PC` 是下一条要执行的字节码地址。这两个在 `lj_snap_restore` 里已经算好、`lj_trace_exit` 通过 `setcframe_pc` 写进了 C frame 的 SAVE_PC 字段。这里取出来装进解释器主循环用的寄存器。

**(跳进 vm_exit_interp)回解释器主循环。** `->vm_exit_interp` 完成最后几个 callee-save 寄存器的恢复(机器码借用了它们),然后落进解释器 dispatch 的下一条字节码。从此刻起,就是纯解释器在跑——它是完整的、正确的 Lua 实现,会按 `PC` 指向的字节码、按真实类型,老老实实执行。

**这就是 side exit 的终点**:从 guard 触发,经过 exit stub(压号)→ vm_exit_handler(存寄存器、调 C)→ lj_trace_exit(靠 snapshot 恢复、设 pc、可能触发 side trace)→ vm_exit_interp(拨栈、回解释器),控制权安稳地回到了解释器手里,程序继续跑出正确结果。

一次 side exit 的代价:一次预测错误的分支惩罚(十几到几十周期)+ 保存全部寄存器(几十周期)+ snapshot 恢复(搬运 spill/寄存器到栈,P5-17 详)+ 可能的 side trace 编译(一次性,但之后收益)。**这个代价是一次性的**,且只在假设偶尔失败时付。假设稳定时,trace 在机器码里飞奔,一个 guard 都不触发,这整套机制零开销(只有 §2.3 那条近免费的 cmp)。

---

## §8 为什么 sound:每个假设都有 guard,失败必回解释器

我们回到 P0-01 §9 那条核心不变式,用本章的源码印证它为什么成立:

> **机器码产生的结果,要么和解释器完全一样(假设成立),要么退回解释器(假设失败)。它永远不会比解释器更错。**

这道保险由两件事撑起来,本章源码逐一印证:

**第一,每一个乐观假设,都有一个 guard 兜底。** 看 §3 的调用点:类型检查(SLOAD)发 cmp+jne、整数溢出(ADDOV)发 jo、栈溢出(asm_stack_check)发 cmp+jb、表查找未命中(asm_guardcc CC_E/CC_NE 多处,见 `lj_asm_x86.h:1186`/`1198`/`2826`)……**只要一个 IR 指令带 `irt_isguard` 标记(`lj_asm.c:2574`),后端就一定为它发一条 jcc 到 exit stub。** 没有哪个乐观假设能"偷偷地"不配 guard——后端的代码生成是机械的:碰到 guard IR,必发 asm_guardcc 或等价的 emit_jcc。§3.1 还讲到 cmp 和 jcc 必须在寄存器分配之后发(注释 `lj_asm_x86.h:72-74`),保证中间没有 rematerialization 污染标志——这是 guard 不漏检的实现细节。

**第二,guard 一触发,就退回解释器;解释器是完整正确的 Lua 实现。** §4-§7 走了一遍这条路径:guard 的 jcc 跳到 exit stub → 压退出号 → 跳 vm_exit_handler → 存全部寄存器到 ExitState → 调 lj_trace_exit → 靠 snapshot 把寄存器/spill 翻译回解释器栈 → 设 pc → vm_exit_interp 拨栈回解释器。**这条路径上没有任何"继续按乐观假设跑"的余地**——一旦 guard 触发,机器码区就被彻底抛弃,控制权 100% 交给解释器。而解释器是 LuaJIT 自带的、不做任何假设的实现(P1-02 讲它怎么手写汇编、按真实类型 dispatch),它给出的结果和官方 Lua 语义一致。

两道保险合起来:**guard 保证"假设不成立必被发现",退出路径保证"发现后必回解释器"。** 所以无论动态类型怎么变化,LuaJIT 的输出永远不比解释器错。这就是主线"把动态执行**安全**变成机器码"里"安全"两个字的源码兑现。

还有一层 sound 值得点出:**snapshot 的完整性**。guard 触发后能不能正确恢复,取决于 snapshot 有没有记录所有该恢复的值。这包括寄存器里的、spill 槽里的、甚至被"分配消除(sink)"优化掉的临时分配(asm_sunk_store,`lj_asm.c:936`)。下章 P5-17 会详讲 snapshot 怎么保证一个值都不漏。这里只先记一句:**只要 snapshot 完整,恢复出的解释器状态就和"假设从没成立过、一直在解释器里跑"的状态逐位等价。** 这是 sound 的另一半根基,和 guard 各担一半。

---

## §9 退出的频率与代价:大多数 guard 永不触发

把 guard 的"成本结构"讲清楚,这关系到 LuaJIT 为什么敢这么乐观。

**绝大多数 guard 永不触发。** guard 背后的假设,是编译器**观察了多次实际运行**才敢做的(热点循环跑了千百次,看到的类型一致)。所以一旦编译,假设在运行时几乎总成立——trace 在机器码里飞奔,所有 guard 的 jcc 都不跳,分支预测器稳稳猜中,整条 trace 跑完一个 guard 都没触发。这是**常态**。

**偶尔触发的 guard,代价一次性。** 某个 guard 因为"少见的输入类型"或"冷分支"触发,代价是 §7 末尾算的那笔账:预测错误惩罚 + 寄存器保存 + snapshot 恢复 + 回解释器。大约几百到几千周期。听起来多,但这是一次性的——程序继续跑,下次到这个 guard,可能又不触发了。

**频繁触发的 guard,会被编译成 side trace。** §6.1 的 trace_hotside 盯着每个 snapshot 的退出计数。某个 guard 退出次数到 `JIT_P_hotexit`(默认 10)阈值,就从它**重新录一条 trace**(side trace,P5-18)。新 trace 把"退出后走的那条路"也编译成机器码,然后通过 **patch exit**(`lj_asm_patchexit`,`lj_asm_x86.h:3127`)把原来那个 guard 的 jcc 目标,从 exit stub 改成新 trace 的 mcode 入口:

```c
/* Patch exit jumps of existing machine code to a new target. */
void lj_asm_patchexit(jit_State *J, GCtrace *T, ExitNo exitno, MCode *target)
{
  MCode *p = T->mcode;
  ...
  MCode *px = exitstub_addr(J, exitno) - 6;    // 原 jcc 跳到的 exit stub 地址
  ...
  for (; p < pe; p += asm_x86_inslen(p)) {
    if ((*(uint16_t *)p & 0xf0ff) == 0x800f && p + *(int32_t *)(p+2) == px && p != pgc) {
      *(int32_t *)(p+2) = jmprel(J, p+6, target);   // 把 jcc 的 rel32 改指向新 trace
    }
    ...
  }
  ...
}
```

(见 `lj_asm_x86.h:3127-3164`。)它扫 trace 机器码,找所有"跳到这个 exit stub"的 jcc(`0F 8x` 操作码 + 目标等于 px),把它们的跳转目标改成新 side trace 的 mcode。从此那个 guard 触发时,不再退到解释器,而是**直接跳进 side trace 继续跑机器码**——退出代价被消掉了。

这就是 LuaJIT"越跑越快"的完整闭环:

1. 先乐观编译,guard 兜底(快且安全)。
2. guard 偶尔触发,忍受几次退出代价(正确,暂慢)。
3. 频繁触发到阈值,录 side trace + patch exit(把退出代价消掉)。
4. 越来越多的 guard 被这样"补上"side trace,退回解释器的次数越来越少。

这个闭环的起点是 guard 的 jcc,终点是 patch 后的 jcc(指向 side trace 而非 exit stub)。中间经过 exit stub、vm_exit_handler、lj_trace_exit、snapshot 恢复——全都是本章讲的机制。**guard 不是"检查完就完了",它还是 side trace 生长的挂载点。**

---

## §10 ★对照:官方 Lua、JVM/V8

### 10.1 对照一:官方 Lua(无 JIT,故无 guard、无 side exit)

官方 Lua 是纯解释器,**本章所有机制它都没有**。没有 guard——它每条字节码都按真实类型现场 dispatch(`OP_ADD` 里判类型、选加法),不需要"假设 + 检查"那一套,因为压根没有"假设"。没有 exit stub、没有 vm_exit_handler、没有 lj_trace_exit、没有 snapshot——因为根本没有机器码要退出,一直在解释器里。

所以官方 Lua 的特点是:**永远正确,永远慢得"均匀"。** 每条指令都付完整的"理解成本"(取指、译码、判类型、dispatch),没有快路径也没有退出代价。LuaJIT 加上 JIT 后,绝大多数指令在机器码里近乎免费地跑(快路径),只有假设失败的那极少数走退出(慢路径)。**guard 和 side exit 是"用复杂换速度"的代价:LuaJIT 多写了整整一套退出机制,换来了热点上几十倍的提速。**

### 10.2 对照二:JVM/V8(deoptimization,去优化)

JVM 和 V8 也有 JIT,也会做乐观假设(比如 `Add` 节点假设操作数是 int),假设失效时也要"退回去"。但它们用的机制叫 **deoptimization(去优化)**,和 LuaJIT 的 side exit 在**粒度**和**机制**上都不同:

**粒度:方法 vs trace。** JVM/V8 是 method JIT,以整个方法为单位编译。假设失效时,它**把整个方法从机器码退回到解释器**(或重新用一个更保守的版本跑)——这叫 deoptimization,粒度是"整个方法"。LuaJIT 是 trace JIT,以一条线性路径为单位;假设失效只退出**这一条 trace** 到对应的 guard,粒度是"一个 guard"。所以 LuaJIT 的退出更"外科手术":只退到出问题的那一点,其他还在跑的 trace 不受影响。

**机制:unpacking frame vs snapshot restore。** JVM/V8 的 deoptimization,要把被优化掉的栈帧"解包"成解释器期望的多个栈帧(一个方法里可能优化时合并了多个调用层),叫 frame unpacking 或 deopt info。它记录的是"每个被优化的栈帧该恢复成什么"。LuaJIT 的 snapshot 记录的是"trace 跑到这个 guard 时,寄存器/spill 怎么映射回解释器栈槽"。两者形式不同(snapshot 更紧凑、是 SSA ref 到栈槽的映射;deopt info 更结构化、是优化帧到解释器帧的展开),但**目的一样**:把优化时的状态翻译回解释器能继续的状态。

**复用:side trace vs re-profiling。** LuaJIT 频繁退出的 guard,直接 patch 成跳 side trace(§9),退出代价被消除。JVM/V8 频繁 deopt 的方法,通常会触发重新编译(用更保守的假设或更多 profile),不是"在退出点挂一段新机器码",而是"整个方法重编"。这反映了 trace vs method 的取舍:trace 短、退出点明确,适合"挂 side trace"这种外科手术;方法大、退出点散,适合"重编整个方法"。

**对照表(本书贯穿用法):**

| 维度 | LuaJIT(trace JIT) | 官方 Lua | JVM / V8(method JIT) |
|---|---|---|---|
| 退出机制 | side exit(snapshot 恢复) | 无 | deoptimization(frame unpacking) |
| 退出粒度 | 单个 guard(一条 trace) | — | 整个方法 |
| 失效后复用 | patch exit → side trace | — | 重新编译方法 |
| 状态恢复 | snapshot(寄存器/spill → 栈槽) | — | deopt info(优化帧 → 解释器帧) |
| 退出代价 | 小(只恢复到 guard 点) | — | 较大(整个方法回退) |

这张表是 trace vs method 在"退出"这一环的具体对照。LuaJIT 选 side exit + snapshot,代价是退出机制更精细(snapshot 编码、exit stub 分组、patch exit),收益是退出快、粒度小、能挂 side trace。这是 trace 路线的典型取舍,也是本章机制存在的理由。

---

## §11 回扣主线

回到本书主线:**把动态执行安全变成机器码。**

guard 和 side exit,是这条主线里**中间那一环**的源码展开。主线说"乐观假设 + 运行时检查 + 失败可回退",本章把后两个词逐行落到了 LuaJIT 2.1.ROLLING 的源码上:

- **运行时检查 = guard 的 cmp+jcc**。§2 从第一性原理推导出它在 CPU 上必然是比较 + 条件跳转;§3 用 `asm_guardcc`(`lj_asm_x86.h:75`)、`emit_jcc`(`lj_emit_x86.h:502`)、SLOAD 类型检查(`lj_asm_x86.h:1804`)等源码印证,讲清它为什么开销极低(分支预测站在乐观假设这边,一条 cmp 近乎免费)。
- **失败可回退 = exit stub → vm_exit_handler → lj_trace_exit → 回解释器**。§4 讲 exit stub 怎么用两次 push imm8 编码退出号、怎么分组复用(`asm_exitstub_gen`/`exitstub_addr`);§5 讲 vm_exit_handler 怎么把全部寄存器存进 ExitState 再调 C(`vm_x86.dasc:2805`);§6 讲 `lj_trace_exit`(`lj_trace.c:886`)怎么靠 snapshot 恢复、设 pc、触发 side trace;§7 讲 vm_exit_interp 怎么拨栈回解释器。
- **sound 的根基 = 每个 guard 都配 jcc + 失败必回解释器**。§8 把这两道保险用源码兑现了一遍。
- **越跑越快 = patch exit 挂 side trace**。§9 讲频繁触发的 guard 怎么被 `lj_asm_patchexit`(`lj_asm_x86.h:3127`)改成直接跳 side trace,把退出代价消掉。

guard 把"乐观"和"安全"分了家:机器码本体尽情乐观(整数加法直跑),guard 负责兜底(一条 cmp 检查),失败有 exit stub 接住(退回解释器)。**快由机器码提供,正确由解释器保底,两者各司其职。** 这是 LuaJIT 能把动态语言安全变成机器码的核心机巧,也是本章全部源码服务的目标。

但本章有意留下一个关键没展开:**snapshot 到底怎么把寄存器/spill 翻译回解释器栈?** §6.1 只是说"调 lj_snap_restore",没讲它内部怎么干。这正是下一章的主题。snapshot 是 side exit 能正确回退的"另一半根基"——guard 保证发现假设失效,snapshot 保证失效后状态能完整恢复。没有 snapshot,guard 触发了也只是"知道错了但回不去"。所以下一章我们深入 snapshot 的编码、分配、恢复,把 side exit 这套机制的另一半补全。

---

*下一章 [P5-17 snapshot 恢复解释器状态](P5-17-snapshot恢复解释器状态.md):guard 触发后,lj_snap_restore 怎么照着 snapshot 把寄存器和 spill 槽里的值,翻译、搬运成解释器栈上的 TValue,并算出"解释器该从哪条字节码继续"。snapshot 是 side exit 能正确回退的另一半根基。*
