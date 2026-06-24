# P4-14 汇编生成 asm:把 IR 一条条翻译成机器码

> **本书主线**:把动态执行安全变成机器码。**二分法**:解释器侧 ↔ JIT 侧。本章在 **JIT 侧·后端**——IR 到机器码的最后一跳。**★对照**:官方 Lua(根本没有"翻译成机器码"这一步)+ JVM/V8(C2/Graal 的机器码生成)。**源码**:LuaJIT 2.1.ROLLING,`lj_asm.c` 的 `lj_asm_trace`/`asm_ir`/`asm_*` 函数族、`lj_asm_x86.h` 的架构相关代码生成、`lj_emit_x86.h` 的底层发射函数、`lj_target.h`/`lj_target_x86.h` 的寄存器与操作码定义。**基调**:纯直球,不用比喻;从第一性原理一步步推导,贴真实 x86-64 汇编逐行解释。

---

## 引子:机器码到底从哪来

上一章(P4-13)我们拆了后端的第一道门槛:寄存器分配。结论是——LuaJIT 用**反向线性扫描**,把 IR 里那几百个自由的虚拟 ref,塞进了十几个物理寄存器(外加少量溢出到栈)。分配完之后,IR 里每一条指令,它用到的每个操作数,都已经有了一个明确的物理位置:要么在某个寄存器(比如 EAX),要么在栈上某个偏移(比如 `[ESP+16]`)。

但到这一步为止,我们手里仍然只有**数据结构**——IR 还是 IR,寄存器分配结果只是写在 `ir->r` 字段里的一个数字。CPU 一行这种东西都跑不了。CPU 要的是**机器码**:一串串二进制字节,每一段对应一条真实的指令。

把"已经分配好寄存器的 IR",变成"CPU 能直接执行的机器码字节"——这件事,就是这一章的主题。它由 `lj_asm.c` 里那个总控函数 `lj_asm_trace`,以及它驱动的 `asm_ir` 分发、`asm_*` 各路翻译函数、`emit_*` 各路发射函数,层层接力完成。这是后端流水线的最后一站:从这里出去的,就是成品机器码,马上要拷进可执行内存(P4-15 讲怎么拷、拷到哪)。

听起来像是"一对一翻译",有什么难的?难就难在:CPU 的指令系统(x86-64)是一套历史悠久、规则繁杂、特例成堆的东西;而 IR 是 LuaJIT 自己设计的、干净规整的中间表示。把干净的 IR 翻译成别扭的 x86,既要**忠实**(语义不丢一条),又要**聪明**(尽量用上 x86 的省指令技巧),还要**配合 guard 机制**(P0-01 讲的那个安全兜底,在这里要变成真实的 cmp+jmp 机器码)。这一章,我们把这套翻译机器拆开来看。

---

## §1 第一性原理:从 IR 到机器码这一步,为什么必须有

### 1.1 一条 IR 不是一条机器码:抽象层次的鸿沟

先把最基本的事实钉死:IR 和机器码,是**两个抽象层次完全不同**的东西。一条 IR 指令,通常不是对应一条机器码——而是对应**一条或多条机器码**,有时还要加额外的数据搬运。

为什么会差这么多?因为两边的设计目标完全不同。

**IR 的设计目标:简洁、规整、易优化。** LuaJIT 的 IR 是 SSA 形式(每个值定义一次)。它的指令大多是**三操作数**的:`%c = ADD %a, %b`,意思是"把 %a 和 %b 相加,结果存到新的值 %c"。这种三操作数形式,非常适合做优化——常量折叠、死代码消除都直接在 IR 上做,不用操心寄存器。三操作数的好处是表达力强、语义清晰。

**x86 机器码的设计目标:历史兼容、字节紧凑。** x86 的运算指令,绝大多数是**两操作数**的:`add eax, ecx` 真正的语义是 `eax = eax + ecx`——结果**写回第一个操作数**,第二个操作数不变。这是 1978 年 8086 留下来的遗产(那时候每个字节都金贵,两操作数比三操作数省一个寄存器字段的编码)。

光这一个差异,就制造了第一道鸿沟。看 IR:

```
%012 = ADD %008, %009     ; 期望:把 %008 和 %009 加起来,存到一个新地方 %012
```

如果 %008 在 EAX,%009 在 ECX,%012 想要的结果放 EDX。你不能写 `add edx, eax, ecx`——x86 没这种三操作数指令。你得这样:

```asm
mov edx, eax      ; 先把 %008 搬到 %012 的目的寄存器 edx
add edx, ecx      ; 再把 %009 加到 edx 上
```

两条机器码,对应一条 IR。多出来的那条 `mov`,纯粹是为了把三操作数的 IR 适配到两操作数的 x86。这是抽象层次差异最直接的代价。

那能不能避免这条 mov?有时能。如果 %008 这个值,在这条 IR 之后**再也不用了**(它在加法之后死亡),那 %012 就可以**直接占用** %008 的寄存器(EAX)——`add eax, ecx`,结果在 EAX,而 EAX 现在就是 %012(因为 %008 死了,EAX 让位给 %012)。这就省掉了 mov。这正是上一章 `ra_left` 那个函数干的事:它尽量让目的寄存器和左操作数寄存器是同一个,从而省掉搬运。这是寄存器分配和代码生成**紧密配合**的第一个例子。

所以你看,"IR 到机器码"这一步,远不是"查表对照"那么简单。它要处理:操作数个数差异、寻址方式差异、立即数范围、副作用、guard 检查……每一条都得有专门的处理逻辑。这套逻辑,就是 `asm_ir` 这个分发函数和它后面那一大堆 `asm_*` 函数要承担的工作。

### 1.2 CPU 的指令世界:寄存器、立即数、内存、寻址

要把 IR 翻成机器码,得先讲清楚 CPU 的指令世界长什么样。x86-64 的运算指令,操作数有**三种来源**:

1. **寄存器(register)**:EAX、ECX、XMM0 这些。CPU 内部最快的存储。
2. **立即数(immediate)**:直接编码在指令里的常数,比如 `add eax, 5` 里的 `5`。小立即数(能放进一个字节的)有更短的编码。
3. **内存(memory)**:`[ESP+16]`、`[RAX+RCX*4+8]` 这种。通过一个**寻址模式**算出地址,再去那个地址取数。

第三种尤其重要。x86 的算术指令,**大多数允许一个操作数是内存**。比如:

```asm
add eax, [rsp+16]    ; 把栈上 [rsp+16] 处的 4 字节,加到 eax 上
mov ecx, [rax+rdx*4] ; 把 [rax+rdx*4] 处的 4 字节取到 ecx
```

这给了代码生成器一个**重要的优化机会**:如果一个值在内存里(比如某个局部变量在栈上),没必要先 load 进寄存器再算,可以直接把那个内存地址塞进指令的操作数里——一条指令就把"取值 + 运算"全做了。

LuaJIT 的代码生成大量利用了这个特性。它叫**内存操作数融合**(memory operand fusion)。看 `asm_fuseload`(`lj_asm_x86.h:437`),这个函数负责"为一个 IR ref 准备操作数"。它的逻辑不是简单地"把 ref load 进寄存器",而是:

- 如果这个 ref 已经在寄存器里,直接用那个寄存器(`ir->r`)。
- 如果这个 ref 是一个**可融合的 load**(比如 SLOAD 从栈 load、FLOAD 从对象字段 load),并且满足"没有冲突指令夹在中间"(`noconflict`),那就**不真的生成 load 指令**,而是把这个 load 的地址信息记到一个叫 `mrm` 的结构里,返回一个特殊标记 `RID_MRM`。
- 调用方拿到 `RID_MRM` 之后,直接把这个内存操作数塞进下一条运算指令的操作数里——load 就这么被"融合"掉了,没有独立的 load 指令。

这套机制省掉了海量的冗余 load 指令。比如一个数组访问 `t[i]`:

```lua
local x = t[i] + 1     -- 先 load t[i],再加 1
```

朴素做法是 `mov reg, [t_addr]; add reg, 1`——load 加运算,两条指令。融合之后是 `add reg, [t_addr]`——一条指令把 load 和运算全干了。这是 trace JIT 性能的一大来源,我们后面会贴真实汇编对照。

### 1.3 反向代码生成:从尾巴往前长

LuaJIT 的机器码生成,有一个从外面看非常反直觉的特征:**机器码是从高地址往低地址生长的**。

意思是:预留一块内存区域 `[mcbot, mctop]`。一开始,代码生成指针 `mcp` 指在最顶上 `mctop`。每生成一条机器指令,`mcp` 就**往低地址减**(减去这条指令的字节数)。等所有指令都生成完,`mcp` 指向生成的机器码的最开头(最低地址),而 `mctop` 还是原来的顶。最终 `mcp` 到 `mctop` 之间,就是完整的机器码。

```text
mcbot                                      mcp            mctop
  |                                         |               |
  v                                         v               v
  [ exit stubs ...           ]    [   生成的机器码  →       ]
                                         (从右往左生成)
```

看 `lj_emit_x86.h:29-31` 这些最底层的发射宏:

```c
#define emit_i8(as, i)   (*--as->mcp = (MCode)(i))
#define emit_i32(as, i)  (*(int32_t *)(as->mcp-4) = (i), as->mcp -= 4)
#define emit_u32(as, u)  (*(uint32_t *)(as->mcp-4) = (u), as->mcp -= 4)
```

注意 `*--as->mcp`——先减指针,再写字节。这是反向生长的核心机制。每条机器指令,都是从它的**最后一个字节**开始往前写。

为什么要这样?这是为了配合上一章讲的**反向扫描 IR**。回忆 P4-13:`lj_asm_trace` 的主循环(`lj_asm.c:2565-2579`)是**从最后一条 IR 指令,往前扫,扫到第一条**:

```c
for (as->curins--; as->curins > as->stopins; as->curins--) {
  IRIns *ir = IR(as->curins);
  ...
  asm_ir(as, ir);    /* 生成这条 IR 的机器码 */
}
```

IR 是反向扫的(从后往前),那么机器码自然也跟着反向生成(从高地址往低地址)。这样**代码生成和寄存器分配是同一个扫描过程**:扫到哪条 IR,就同时分配它的寄存器、生成它的机器码。不需要"先扫一遍分配,再扫一遍生成"两个独立 pass。这是 LuaJIT 后端一个关键的设计整合。

为什么反向扫描?上一章详细讲过:反向扫描里,SSA 的 ref 编号天然就是活跃区间信息(ref 越小定义越早,反向扫到时它的活跃区间越远)。还有更深一层原因——和循环回边跳转有关。一个循环 trace,它的循环体机器码要能"跳回自己开头"形成闭环。如果正向生成(从前往后),生成到回边时,跳转目标(循环开头)早就生成完了、地址是已知的,这看起来挺好。但反向生成有个更妙的好处:**循环开头对应的机器码位置,是扫描全部完成之后才确定的(就是最终 `mcp` 指的位置)**——而回边的跳转指令,是在扫描到 LOOP 那条 IR 时生成的(那时还在扫描中途,目标位置未知,先占位)。

这就引出一个必须解决的问题:**前向跳转的回填**。我们用一个独立小节讲它,因为它贯穿整个代码生成。

### 1.4 前向跳转的难题:目标地址现在还不知道

x86 的跳转指令,编码成一个**相对偏移**:跳转指令本身有一个字段,存"目标地址 - 跳转指令末尾地址"。CPU 执行到跳转时,把 PC 加上这个偏移,就到了目标。

问题来了。在反向代码生成里,一个**向后跳转**(从机器码低地址跳到高地址,即 trace 里"往回看"的跳转,比如循环回边跳到循环开头),它的目标地址(循环开头,高地址方向)**在生成跳转指令时,还是未知的**——因为循环开头还没生成(我们要扫完所有 IR 才知道开头在哪)。

反过来,一个**向前跳转**(从高地址跳到低地址,比如 guard 失败跳到 exit stub),它的目标地址(exit stub,通常在更低地址)**是已知的**——因为 exit stub 是预先在 `mcbot` 那边生成好的(`asm_exitstub_setup`,`lj_asm_x86.h:51`)。

所以代码生成要处理两类跳转:

- **guard 失败跳到 exit stub**:目标已知,直接生成完整的跳转指令。
- **循环回边跳到循环开头**:目标未知,先占位,等扫完回填。

后者的处理,看 `asm_loop_fixup`(`lj_asm_x86.h:2851`):

```c
static void asm_loop_fixup(ASMState *as)
{
  MCode *p = as->mctop;          /* 之前在 asm_tail_prep 预留的跳转指令位置 */
  MCode *target = as->mcp;       /* 扫完之后的循环开头位置 */
  ...
  p[-5] = XI_JMP;                              /* 写入 jmp 操作码字节 */
  *(int32_t *)(p-4) = (int32_t)(target - p);   /* 回填偏移 */
  ...
}
```

`asm_tail_prep`(`lj_asm_x86.h:2974`)在最开始就**预留了 5 字节**给这条回边跳转(`p -= 5` 那行,或者是 2 字节的短跳)。扫描途中,这 5 字节是空着的。等扫完,`target = as->mcp` 就是循环开头的真实地址——这时回头把偏移算出来填进去。这就是**回填(patch/fixup)**。

guard 那边不用回填,因为 exit stub 在 `mcbot` 那边先建好了。`asm_guardcc`(`lj_asm_x86.h:75`)直接生成完整的条件跳转:

```c
static void asm_guardcc(ASMState *as, int cc)
{
  MCode *target = exitstub_addr(as->J, as->snapno);   /* exit stub 地址,已知 */
  MCode *p = as->mcp;
  ...
  emit_jcc(as, cc, target);   /* 直接生成完整的 jcc target */
}
```

`emit_jcc`(`lj_emit_x86.h:502`)把跳转指令的字节,从 `target - p` 算出偏移,直接写死:

```c
static void emit_jcc(ASMState *as, int cc, MCode *target)
{
  MCode *p = as->mcp;
  *(int32_t *)(p-4) = jmprel(as->J, p, target);   /* 偏移 */
  p[-5] = (MCode)(XI_JCCn+(cc&15));                /* 0f 8x 操作码 */
  p[-6] = 0x0f;
  as->mcp = p - 6;
}
```

注意 `0x0f 0x8x` 是 x86 的**长条件跳转**(6 字节:2 字节操作码 + 4 字节偏移)。LuaJIT 用长跳转而不是短跳转(`0x7x`,2 字节,偏移范围 ±127),是因为 trace 可能很长,guard 到 exit stub 的距离可能超过 127 字节——长跳转覆盖 32 位偏移,稳。

(还有个分支预测的细节:CPU 把"向后跳转"默认当作会跳(因为循环回边大概率跳),"向前跳转"默认当作不跳。guard 是向前跳(exit stub 在前面),失败才跳,默认不跳——正合适。循环回边是向后跳,默认跳——也正合适。LuaJIT 这套布局天然符合 CPU 的分支预测偏好。)

到这里,第一性原理讲完了:**IR 是抽象的、三操作数、易优化的;机器码是具体的、两操作数、寻址复杂的;两者之间有鸿沟,需要专门的翻译;翻译时利用 x86 的内存操作数融合省指令;反向生成配合反向扫描,前向跳转用回填解决。** 接下来进源码,看这套翻译机器怎么落地。

---

## §2 源码印证:从 lj_asm_trace 到 emit_* 的四层

LuaJIT 的汇编生成,是一个**四层结构**。从上到下:

1. **`lj_asm_trace`**(`lj_asm.c:2471`):总控。准备状态、反向扫 IR、最后回填头部和尾部。
2. **`asm_ir`**(`lj_asm.c:1785`):分发器。一个巨大的 switch,按 IR 的 opcode 调对应的 `asm_*`。
3. **`asm_*`**(各处):具体翻译。`asm_add` 把 ADD 翻成 add,`asm_sload` 把 SLOAD 翻成 mov,`asm_comp`+`asm_guardcc` 把比较 guard 翻成 cmp+jcc。这一层也调用上一章的 `ra_dest`/`ra_left` 做寄存器分配。
4. **`emit_*`**(`lj_emit_x86.h`):底层发射。把"逻辑上的指令"变成"具体的字节序列"。架构相关的最底层。

我们自上而下逐层看。

### 2.1 第一层:lj_asm_trace 总控

这是整个汇编生成的入口,任何一条 trace 编译成机器码,都从这里开始(`lj_asm.c:2471`)。我们分块看。

**块一:准备(`2476-2508`)**

```c
void lj_asm_trace(jit_State *J, GCtrace *T)
{
  ASMState as_;
  ASMState *as = &as_;
  /* Remove nops/renames left over from ASM restart ... */
  {
    IRRef nins = T->nins;
    IRIns *ir = &T->ir[nins-1];
    if (ir->o == IR_NOP || ir->o == IR_RENAME) {
      do { ir--; nins--; } while (ir->o == IR_NOP || ir->o == IR_RENAME);
      T->nins = nins;
    }
  }
  as->orignins = lj_ir_nextins(J);
  lj_ir_nop(&J->cur.ir[as->orignins]);
  as->J = J;
  as->T = T;
  J->curfinal = lj_trace_alloc(J->L, T);   /* 复制一份 IR,因为生成时可能要加 RENAME */
  ...
  as->mctop = as->mctoporig = lj_mcode_reserve(J, &as->mcbot);  /* 预留机器码内存 */
  as->mcp = as->mctop;                                            /* 指针放在顶部 */
  as->mclim = as->mcbot + MCLIM_REDZONE;
  asm_setup_target(as);    /* 预先生成 exit stub */
  ...
}
```

几个要点:

- `ASMState as_`:整个汇编生成的全局状态,栈上分配。它很大(有 cost 数组、各种 RegSet、parent 映射等),但放栈上是因为汇编生成是单线程、不可重入的,用完即毁。
- `lj_trace_alloc` 复制一份 IR 到 `J->curfinal`。为什么?因为汇编生成中途可能要往 IR 里**加 RENAME 指令**(上一章 PHI 那节讲的,循环回边的寄存器搬运靠 RENAME 记录)。原始 IR 不能动(还在别的流程用),所以在副本上操作。注释里写了大约 95% 的 trace 不加任何 RENAME,只有 5% 加——所以预分配了 1 个槽(`lj_ir_nextins`),只在不够时才重新分配重试。
- `lj_mcode_reserve` 预留一块可执行内存(具体怎么预留、W^X 怎么处理,P4-15 讲),返回顶 `mctop` 和底 `mcbot`。`mcp` 一开始放在顶上,准备往下生长。
- `asm_setup_target`(`lj_asm_x86.h:3018`)就一句话:`asm_exitstub_setup(as, as->T->nsnap)`——根据这个 trace 有多少个 snapshot,预生成相应数量的 exit stub(每个 guard 退出点对应一个)。这些 stub 放在 `mcbot` 那边(低地址),先于真正的代码生成。

**块二:那个 `for(;;)` 重试循环(`2537-2619`)**

```c
for (;;) {
  as->mcp = as->mctop;        /* 每次重试都重置指针 */
  ...
  as->ir = J->curfinal->ir;
  as->curins = J->cur.nins = as->orignins;
  ...
  asm_tail_prep(as, T->link);   /* 预留尾部空间 */
  ...
  asm_setup_regsp(as);          /* 第一遍:扫 IR 算寄存器提示和 spill 槽 */
  if (!as->loopref)
    asm_tail_link(as);          /* 非循环 trace:生成退到解释器的尾部 */

  /* 反向扫 IR 的主循环 */
  for (as->curins--; as->curins > as->stopins; as->curins--) {
    IRIns *ir = IR(as->curins);
    ...
    asm_snap_prev(as);
    if (!ra_used(ir) && !ir_sideeff(ir) && (as->flags & JIT_F_OPT_DCE))
      continue;                              /* 死代码消除 */
    if (irt_isguard(ir->t))
      asm_snap_prep(as);                     /* guard:准备 snapshot 分配 */
    ...
    asm_ir(as, ir);                          /* 真正生成 */
  }
  ...
}
```

重试循环的存在,是因为两件事可能要求重做:

1. **`realign`**:x86 上,小循环的循环体会被**对齐到 16 字节边界**(为了让循环体的指令落在少数几个缓存行里,提高指令预取效率)。第一次扫到一半发现循环够小、值得对齐,设 `as->realign` 让这里 `continue` 重来。
2. **IR 增长**:如果中途加了 RENAME 让 IR 超出预分配,`J->curfinal->nins >= T->nins` 判断失败(`2603`),重新分配更大的 IR 副本,重来。注释说只有约 2% 的 trace 触发重试。

主循环内部,每条 IR:
- `asm_snap_prev`:跨过 snapshot 边界时更新当前 snapshot(配合上一章 §4.3 的 snapshot 预分配)。
- 死代码消除:`!ra_used(ir) && !ir_sideeff(ir)`——结果没人用、又没副作用,直接跳过,连机器码都不生成。这是寄存器分配之后的最后清理。
- `irt_isguard(ir->t)`:这条 IR 带 guard 标记(乐观假设),调 `asm_snap_prep` 给它对应的 snapshot 准备好(预分配寄存器、spill,保证 guard 失败时能恢复)。
- `asm_ir(as, ir)`:核心。分发到具体的 `asm_*` 函数。

**块三:头部和尾部(`2584-2634`)**

```c
/* Emit head of trace. */
if (as->gcsteps > 0) {
  ...
  asm_gc_check(as);          /* 累计需要 GC,在头部插入 GC 检查 */
}
ra_evictk(as);               /* 把常量占用的寄存器都赶走(因为头部要用) */
if (as->parent)
  asm_head_side(as);         /* side trace:头部要恢复父 trace 的状态 */
else
  asm_head_root(as);         /* root trace:头部做栈调整 */

if (J->curfinal->nins >= T->nins) {  /* IR 没增长,成功 */
  ...
  break;
}
...
} /* end for(;;) */

/* 全部扫完 + 头部生成完 */
if (as->freeset != RSET_ALL)
  lj_trace_err(as->J, LJ_TRERR_BADRA);   /* 检查:所有寄存器应回归空闲 */

T->mcode = as->mcp;          /* 记录入口点(机器码最低地址) */
T->mcloop = as->mcloop ? (MSize)((char *)as->mcloop - (char *)as->mcp) : 0;
if (as->loopref)
  asm_loop_tail_fixup(as);   /* 循环:回填回边跳转 */
else
  asm_tail_fixup(as, T->link);  /* 非循环:回填尾部跳转(到下一条 trace 或解释器) */
T->szmcode = (MSize)((char *)as->mctop - (char *)as->mcp);  /* 总大小 */
asm_snap_fixup_mcofs(as);    /* 把每个 snapshot 的 mcofs 填上 */
lj_mcode_sync(T->mcode, as->mctoporig);   /* 同步指令缓存(必要时) */
```

注意整个流程的顺序:**先扫 IR 生成主体(从尾巴往前)→ 生成头部(还在往前)→ 最后回填所有跳转**。头部是最后生成的(因为它是 IR 序列里最靠前的,反向扫最后扫到)。回填在更最后(因为要等所有位置都定下来)。

`lj_mcode_sync` 是为了处理某些架构的**指令缓存一致性问题**——CPU 把刚写的机器码可能缓存在 D-cache,但执行时从 I-cache 取,两边可能不一致(尤其 ARM/MIPS)。x86 上硬件保证一致性,这个调用基本是空操作;但跨架构的代码不能省。P4-15 会专门讲。

### 2.2 第二层:asm_ir 分发器

`asm_ir`(`lj_asm.c:1785`)就是那个大 switch。我们看它怎么按 opcode 分发:

```c
static void asm_ir(ASMState *as, IRIns *ir)
{
  switch ((IROp)ir->o) {
  /* Miscellaneous ops. */
  case IR_LOOP: asm_loop(as); break;
  case IR_NOP: case IR_XBAR:
    lj_assertA(!ra_used(ir), ...);
    break;
  case IR_USE:
    ra_alloc1(as, ir->op1, irt_isfp(ir->t) ? RSET_FPR : RSET_GPR); break;
  case IR_PHI: asm_phi(as, ir); break;
  ...

  /* Guarded assertions. */
  case IR_LT: case IR_GE: case IR_LE: case IR_GT:
  case IR_ULT: case IR_UGE: case IR_ULE: case IR_UGT:
  case IR_ABC:
    asm_comp(as, ir);
    break;
  case IR_EQ: case IR_NE: asm_fuseequal(as, ir); break;

  /* Arithmetic ops. */
  case IR_ADD: asm_add(as, ir); break;
  case IR_SUB: asm_sub(as, ir); break;
  case IR_MUL: asm_mul(as, ir); break;
  ...
  case IR_CONV: asm_conv(as, ir); break;

  /* Loads and stores. */
  case IR_ALOAD: case IR_HLOAD: case IR_ULOAD: case IR_VLOAD:
    asm_ahuvload(as, ir);
    break;
  case IR_FLOAD: asm_fload(as, ir); break;
  case IR_SLOAD: asm_sload(as, ir); break;
  ...

  /* Calls. */
  case IR_CALLA:
    as->gcsteps++;
    /* fallthrough */
  case IR_CALLN: case IR_CALLL: case IR_CALLS: asm_call(as, ir); break;
  case IR_CALLXS: asm_callx(as, ir); break;
  case IR_CARG: break;

  default:
    setintV(&as->J->errinfo, ir->o);
    lj_trace_err_info(as->J, LJ_TRERR_NYIIR);
    break;
  }
}
```

分发逻辑非常直白:`switch (ir->o)`,每个 case 对应一个 `asm_xxx`。要注意几点:

**第一,有些 IR opcode 共用一个 `asm_xxx`**。比如 `IR_LT` 到 `IR_ABC` 这一系列比较指令,全都走 `asm_comp`——它们语义相似(都是"比较两个值,可能带 guard"),差异在比较方向和符号性。`asm_comp` 内部用一个**查表**(`asm_compmap`,后面讲)来区分。

**第二,有些 opcode 是复合翻译**。比如 `IR_EQ`/`IR_NE` 走 `asm_fuseequal`,它会检查"前一条 IR 是不是 HREF(哈希表查找)",如果是,把 HREF 和 EQ **合并成一条**指令翻译——这是又一个融合优化。`IR_CALLA` 在 `asm_call` 之前先 `as->gcsteps++`,因为 CALLA(C call with allocation)会触发 GC,要在头部插入 GC 检查。

**第三,default 报 NYI**(Not Yet Implemented)。意思是"这种 IR 我还不会翻"。出现这种情况说明 IR 流水线上游出了意外(比如优化 pass 产生了后端不认识的 IR),直接放弃这条 trace。这是健壮性的兜底。

注意这个 switch **没有用 IR 的 mode 表**(那个 `lj_ir_mode[ir->o]` 数组)来做分发。mode 表是给优化 pass 和寄存器分配用的(它编码了"这条 IR 是不是比较、是不是 guard、是不是 store"等元信息,见 `lj_ir.h:285-302`)。代码生成的分发是直接的 switch——因为每个 opcode 的翻译逻辑差异很大,塞进表里反而难读。mode 表在某些辅助判断里用,比如 `asm_swapops`(`lj_asm_x86.h:2058`)判断"这是不是可交换运算"用 `irm_iscomm(lj_ir_mode[ir->o])`——可交换的运算(ADD/MUL)允许交换两个操作数,不可交换的(SUB/DIV)不行。

接下来,我们挑几条代表性的 IR,看它们各自怎么翻成机器码。这是本章的核心内容——让你看见 IR 怎么变成真实的字节。

### 2.3 典型翻译之一:算术 ADD → add/lea 指令

`asm_add`(`lj_asm_x86.h:2226`)是最典型的算术翻译:

```c
static void asm_add(ASMState *as, IRIns *ir)
{
  if (irt_isnum(ir->t))
    asm_fparith(as, ir, XO_ADDSD);              /* 浮点:addsd xmm */
  else if (as->flagmcp == as->mcp || irt_is64(ir->t) || !asm_lea(as, ir))
    asm_intarith(as, ir, XOg_ADD);              /* 整数:add 或 lea */
}
```

分三条路:

**浮点加法**:`asm_fparith(as, ir, XO_ADDSD)`,发射 `addsd xmm0, xmm1` 这种 SSE2 浮点加法指令。`XO_ADDSD` 是 x86 浮点加法的操作码(`lj_target_x86.h:290`,`XO_f20f(58)` 展开成 `f2 0f 58` 三个字节)。

**整数加法**,优先用 `asm_lea` 尝试翻译成 `lea` 指令。`lea`(Load Effective Address)是 x86 的一个怪指令——它本来是"算地址"的,但因为它能在一个时钟周期内做 `reg = reg1 + reg2*scale + imm`,实际上是个**四操作数加法器**。LuaJIT 用它做加法,有两个好处:

1. **三操作数**:IR 的 `%c = ADD %a, %b` 是三操作数。x86 的 `add` 是两操作数(必须有一个 mov 配合)。但 `lea` 可以 `lea ecx, [eax+ebx]`——三个操作数(`ecx = eax + ebx`),不用 mov!这正好匹配 IR 的三操作数语义,省一条指令。
2. **能融合常量**:`lea ecx, [eax+8]` 把"加 8"融进去了。

看 `asm_lea`(`lj_asm_x86.h:2169`)的注释(写得非常清楚):

```c
/* LEA is really a 4-operand ADD with an independent destination register,
** up to two source registers and an immediate. One register can be scaled
** by 1, 2, 4 or 8. This can be used to avoid moves or to fuse several
** instructions.
**
** Currently only a few common cases are supported:
** - 3-operand ADD:    y = a+b; y = a+k   with a and b already allocated
** - Left ADD fusion:  y = (a+b)+k; y = (a+k)+b
** - Right ADD fusion: y = a+(b+k)
*/
```

`asm_lea` 返回 1 表示成功用 lea 翻译了,返回 0 表示这种情况 lea 处理不了,回退到 `asm_intarith`。如果 `as->flagmcp == as->mcp`(下一条指令要依赖 flags,lea 不改 flags,正好可以保留前面的 flags)或者 `irt_is64(ir->t)`(64 位加法,lea 配 REX 前缀也能干),也走 lea。

**普通整数加法**:回退到 `asm_intarith(as, ir, XOg_ADD)`。这才是"标准的"add 翻译。我们看它(`lj_asm_x86.h:2101`),这是算术翻译的模板,所有 `asm_band`/`asm_bor`/`asm_bxor`/`asm_sub` 都复用它:

```c
static void asm_intarith(ASMState *as, IRIns *ir, x86Arith xa)
{
  IRRef lref = ir->op1;
  IRRef rref = ir->op2;
  RegSet allow = RSET_GPR;
  Reg dest, right;
  int32_t k = 0;
  ...
  right = IR(rref)->r;
  if (ra_hasreg(right)) {
    rset_clear(allow, right);   /* 右操作数已有寄存器,把它从可分配集合排除 */
    ra_noweak(as, right);
  }
  dest = ra_dest(as, ir, allow);   /* ① 给目的(结果)分配寄存器 */
  if (lref == rref) {
    right = dest;                  /* a+a 特殊处理 */
  } else if (ra_noreg(right) && !asm_isk32(as, rref, &k)) {
    if (asm_swapops(as, ir)) { ... }   /* 可交换就交换,优化 */
    right = asm_fuseloadm(as, rref, rset_clear(allow, dest), irt_is64(ir->t));
  }
  if (irt_isguard(ir->t))          /* ADDOV/SUBOV/MULOV 这种带溢出检查的 */
    asm_guardcc(as, CC_O);
  if (xa != XOg_X_IMUL) {
    if (ra_hasreg(right))
      emit_mrm(as, XO_ARITH(xa), REX_64IR(ir, dest), right);   /* ② op reg, reg */
    else
      emit_gri(as, XG_ARITHi(xa), REX_64IR(ir, dest), k);      /* ② op reg, imm */
  } else { ... /* IMUL 特殊 */ }
  ra_left(as, dest, lref);          /* ③ 把左操作数搬到 dest */
}
```

这是算术翻译的标准三步:

1. **`ra_dest`**:给结果分配一个寄存器(上一章讲的,可能从 hint 来,可能新分配)。
2. **`emit_mrm` 或 `emit_gri`**:发射运算指令本身。`emit_mrm` 是"reg, reg/mem"形态(`op reg, reg`),`emit_gri` 是"reg, imm"形态(`op reg, 立即数`)。`XO_ARITH(xa)` 把算术 group 编码加上 `/r` 字段(`xa` 是 ADD/SUB/AND/OR/XOR 之一,编码在 ModRM 的 `/digit` 字段)。
3. **`ra_left`**:把左操作数搬到 dest 寄存器。因为 x86 两操作数,`op dest, right` 实际是 `dest = dest op right`,所以必须先把左操作数弄到 dest。如果左操作数已经有寄存器而且和 dest 一样(上一章 PHI 合并的结果),这里什么都不发;否则发一条 `mov dest, left`。

**注意反向顺序**:`emit_mrm`/`emit_gri` 在前,`ra_left` 在后。但反向代码生成里,**后调用的先出现在机器码里**(因为 mcp 往低地址长,后写的字节在低地址,执行时先执行)。所以最终机器码顺序是:

```asm
mov dest, left      ; ra_left 发的,后调用,但执行在前
op dest, right      ; emit_mrm 发的,先调用,但执行在后
```

这正是我们期望的:先搬左操作数,再做运算。**反向代码生成要求思考顺序反过来——先发后置的指令,后发前置的指令。** 这是 LuaJIT 后端代码的一个普遍特征,读源码时要时刻记住。

我们看一个具体的整数加法 IR `%012 = ADD %008, %009`,假设 %008 在 EAX,%009 在 ECX,%012 要放 EDX(分配结果)。最终机器码(反向 emit 出来的,正序看):

```asm
mov  edx, eax      ; ba?? 不,实际是 89 c2 (mov edx,eax 的编码) —— ra_left
add  edx, ecx      ; 01 ca (add edx,ecx) —— emit_mrm(XO_ARITH(XOg_ADD), edx, ecx)
```

`89 c2`:`mov edx, eax`。0x89 是 `mov r/m, r`(把寄存器写到 r/m),ModRM `c2` = 11 000 010 = `mode=reg, reg=eax(0), rm=edx(2)`。所以是 `mov edx, eax`。

`01 ca`:`add edx, ecx`。0x01 是 `add r/m, r`,ModRM `ca` = 11 001 010 = `mode=reg, reg=ecx(1), rm=edx(2)`。所以是 `add edx, ecx`(把 ecx 加到 edx 上)。

两条指令 4 字节,完成了一次加法。这就是 IR `%012 = ADD %008, %009` 翻译成机器码的全过程。

如果 %008 在 ADD 之后死亡(再也没人用),`ra_left` 会发现 %008 的寄存器(EAX)和 dest 不同,但因为它死了,可以让 %012 直接占用 EAX(让 ra_dest 把 EAX 分配给 %012),这样省掉 mov:

```asm
add  eax, ecx      ; 直接在 eax 上累加,没有 mov
```

一条指令 2 字节。这就是"省 mov"的优化在起作用。

如果右操作数是常量,比如 `%012 = ADD %008, KINT(5)`,走 `emit_gri` 那条路:

```asm
mov  edx, eax      ; ra_left
add  edx, 5        ; 83 c2 05 (add edx, imm8)
```

`83 c2 05`:0x83 是 `add r/m, imm8`(短立即数形态,因为 5 能放进一个字节),ModRM `c2` = reg 模式 + edx,立即数 0x05。

### 2.4 典型翻译之二:load SLOAD → mov 指令

load 类的 IR(SLOAD 从栈 load、FLOAD 从对象字段 load、ALOAD 从数组 load),翻译的核心是 `emit_rmro`——"reg, [base+offset]" 形态。看 `asm_sload`(`lj_asm_x86.h:1728`)的关键部分(简化):

```c
static void asm_sload(ASMState *as, IRIns *ir)
{
  int32_t ofs = 8*((int32_t)ir->op1-1-LJ_FR2) + ...;   /* 栈偏移 */
  IRType1 t = ir->t;
  Reg base;
  ...
  if (ra_used(ir)) {
    RegSet allow = irt_isnum(t) ? RSET_FPR : RSET_GPR;
    Reg dest = ra_dest(as, ir, allow);                  /* ① 目的寄存器 */
    base = ra_alloc1(as, REF_BASE, RSET_GPR);           /* ② BASE 寄存器(栈基址) */
    ...
    emit_rmro(as, irt_isnum(t) ? XO_MOVSD : XO_MOV, dest, base, ofs);  /* ③ mov reg, [base+ofs] */
  }
  ...
}
```

三步:

1. `ra_dest`:给 load 的结果分配寄存器(浮点用 XMM,整数用 GPR)。
2. `ra_alloc1(REF_BASE, ...)`:确保 BASE 寄存器(解释器的栈指针,固定是某个寄存器,通常是 R14 或 EDX)可用。
3. `emit_rmro(as, XO_MOV, dest, base, ofs)`:发射 `mov dest, [base+ofs]`。`XO_MOV` 是整数 mov,`XO_MOVSD` 是双精度浮点 mov。

`emit_rmro`(`lj_emit_x86.h:110`)是"reg, [base+offset]"形态的发射函数。它处理 x86 内存操作数的所有寻址细节:

```c
static void emit_rmro(ASMState *as, x86Op xo, Reg rr, Reg rb, int32_t ofs)
{
  MCode *p = as->mcp;
  x86Mode mode;
  if (ra_hasreg(rb)) {
    ...
    if (ofs == 0 && (rb&7) != RID_EBP) {
      mode = XM_OFS0;            /* 偏移为 0,最短编码 */
    } else if (checki8(ofs)) {
      *--p = (MCode)ofs;         /* 1 字节偏移 */
      mode = XM_OFS8;
    } else {
      p -= 4;
      *(int32_t *)p = ofs;       /* 4 字节偏移 */
      mode = XM_OFS32;
    }
    if ((rb&7) == RID_ESP)
      *--p = MODRM(XM_SCALE1, RID_ESP, RID_ESP);   /* ESP 特殊:要 SIB 字节 */
  } else { ... /* 没有 base,绝对地址 */ }
  as->mcp = emit_opm(xo, mode, rr, rb, p, 0);
}
```

x86 内存操作数的 ModRM 编码有三档偏移:`XM_OFS0`(没偏移)、`XM_OFS8`(1 字节偏移,范围 -128~127)、`XM_OFS32`(4 字节偏移)。`emit_rmro` 根据 `ofs` 的大小选最短的那档——这又是字节紧凑性的体现,小偏移用短编码省字节。

ESP 是特例:ModRM 里 `rm=ESP(4)` 的编码被借用来表示"后面有 SIB(Scale-Index-Base)字节",所以要用 ESP 当基址时,得补一个 SIB 字节(`MODRM(XM_SCALE1, RID_ESP, RID_ESP)`,意思是 scale=1、index=none、base=ESP)。这是 x86 编码的一个历史包袱,LuaJIT 老老实实处理。

假设 `SLOAD %010 : [base+16]` 加载到 EAX(整数),最终机器码:

```asm
mov  eax, [r14+16]    ; 或者具体看 BASE 是哪个寄存器
```

`41 8b 46 10`(假设 BASE 是 R14D,需要 REX 前缀):0x41 是 REX.B(扩展寄存器),0x8b 是 `mov r32, r/m32`,ModRM `46` = 01 000 110 = `mode=OFS8, reg=eax, rm=r14(借 REX.B)`,立即数 0x10 = 16。

这就是 SLOAD 怎么变成一条 mov 指令。

更精彩的是**融合**。看前面 `asm_fuseload`(`lj_asm_x86.h:437`)里 SLOAD 那一支:

```c
} else if (ir->o == IR_SLOAD) {
  if (!(ir->op2 & (IRSLOAD_PARENT|IRSLOAD_CONVERT)) &&
      noconflict(as, ref, IR_RETF, 2) &&
      !(LJ_GC64 && irt_isaddr(ir->t))) {
    as->mrm.base = (uint8_t)ra_alloc1(as, REF_BASE, xallow);
    as->mrm.ofs = 8*((int32_t)ir->op1-1-LJ_FR2) + ...;
    as->mrm.idx = RID_NONE;
    return RID_MRM;       /* 返回"用内存操作数"标记 */
  }
}
```

当一个 SLOAD 被用作别的运算的操作数(比如 `%012 = ADD %008, %009`,而 %009 是个 SLOAD),`asm_intarith` 调 `asm_fuseloadm` 准备右操作数,内部调 `asm_fuseload`,发现 %009 是个可融合的 SLOAD——**不生成独立的 mov 指令**,而是把 SLOAD 的地址信息记进 `as->mrm`,返回 `RID_MRM`。然后 `emit_mrm`(`lj_emit_x86.h:200`)检查操作数是不是 `RID_MRM`,如果是,直接把这个 mrm 内存操作数塞进运算指令:

```asm
add  eax, [r14+16]    ; 一条指令完成 load + add
```

而不是:

```asm
mov  ecx, [r14+16]    ; 先 load
add  eax, ecx         ; 再加
```

省了一条指令和一个寄存器(ECX 不用临时占用了)。这是 LuaJIT 代码生成质量的核心来源之一——**让 x86 的内存操作数特性发挥到极致**。

### 2.5 典型翻译之三:guard(比较 + 条件跳转)

这是 trace JIT 最有特色的一类翻译。回忆 P0-01:trace 里到处是 guard——乐观假设的运行时检查。比如"假设这个值是整数""假设 i < 900000"。这些假设,在机器码里要变成真实的检查指令,失败就跳到 exit stub 退回解释器。

LuaJIT 的 guard 由两部分组成:

- **比较指令**(cmp/test/ucomisd 等):设置 CPU 的 flags 寄存器(EFLAGS)。
- **条件跳转**(jcc):根据 flags 决定跳不跳。跳则失败,跳到 exit stub。

这两部分分别由 IR 的比较指令(LT/GE/LE/GT/ULT/.../EQ/NE/ABC)和 `asm_guardcc` 配合生成。看 `asm_comp`(`lj_asm_x86.h:2402`)的整数比较分支(简化):

```c
static void asm_comp(ASMState *as, IRIns *ir)
{
  uint32_t cc = asm_compmap[ir->o];    /* ① 查表得条件码 */
  ...
  else {  /* 整数比较 */
    IRRef lref = ir->op1, rref = ir->op2;
    ...
    /* 把常量和可融合 load 换到右边 */
    if (irref_isk(lref) || (!irref_isk(rref) && opisfusableload(leftop))) {
      if ((cc & 0xc) == 0xc) cc ^= 0x53;   /* 交换操作数要翻转条件码 */
      ...
      lref = ir->op2; rref = ir->op1;
    }
    if (asm_isk32(as, rref, &imm)) {
      ...
      asm_guardcc(as, cc);                          /* ② jcc 到 exit stub */
      ...
      emit_gmrni(as, XG_ARITHi(XOg_CMP), r64 + left, imm);  /* ③ cmp reg, imm */
    } else {
      Reg left = ra_alloc1(as, lref, RSET_GPR);
      Reg right = asm_fuseloadm(as, rref, rset_exclude(RSET_GPR, left), r64);
      asm_guardcc(as, cc);                          /* ② jcc */
      emit_mrm(as, XO_CMP, r64 + left, right);     /* ③ cmp reg, reg/mem */
    }
  }
}
```

三步:

1. **`asm_compmap[ir->o]`**:查表得条件码(cc,condition code)。`asm_compmap`(`lj_asm_x86.h:2386`)是一个静态数组,把每个比较 IR opcode 映射到一个打包的条件码:

```c
static const uint16_t asm_compmap[IR_ABC+1] = {
  /*                 signed non-eq unsigned flags */
  /* LT  */ COMPFLAGS(CC_GE, CC_G,  CC_AE, VCC_PS),   /* LT 失败:cc=GE(大于等于时跳) */
  /* GE  */ COMPFLAGS(CC_L,  CC_L,  CC_B,  0),         /* GE 失败:cc=L(小于时跳) */
  /* LE  */ COMPFLAGS(CC_G,  CC_G,  CC_A,  VCC_PS),
  /* GT  */ COMPFLAGS(CC_LE, CC_L,  CC_BE, 0),
  /* ULT */ COMPFLAGS(CC_AE, CC_A,  CC_AE, VCC_U),
  ...
  /* EQ  */ COMPFLAGS(CC_NE, CC_NE, CC_NE, VCC_P),
  /* NE  */ COMPFLAGS(CC_E,  CC_E,  CC_E,  VCC_U|VCC_P),
  ...
};
```

注意一个反直觉的地方:**表里存的是"guard 失败时跳"的条件**。`IR_LT` 是"小于"——它带 guard 表示"假设这里小于"。但表里 `LT` 对应的是 `CC_GE`(大于等于)——意思是**当条件不成立(大于等于)时跳到 exit**。这符合 guard 的语义:guard 是"如果假设不成立就退出"。所以 IR_LT 编译出的 jcc 是"如果 GE 就跳"。

这个反向有点绕,但符合逻辑。COMPFLAGS 宏(`lj_asm_x86.h:2385`)把多个信息打包进一个 16 位整数:有符号比较的失败条件、无符号比较的失败条件、用于非等比较的额外条件、以及浮点比较的 VCC 标志(是否需要额外的 CC_P 分支处理 NaN)。

2. **`asm_guardcc(as, cc)`**:发射条件跳转,失败跳到 exit stub。前面贴过它的代码——`emit_jcc(as, cc, target)`,target 是 `exitstub_addr(as->J, as->snapno)`。

3. **`emit_mrm(XO_CMP, ...)` 或 `emit_gmrni(XO_CMP, ...)`**:发射比较指令本身,设置 flags。

注意**反向顺序**:`asm_guardcc` 在前(后执行,机器码里在后),`emit_cmp` 在后(先执行,机器码里在前)。最终机器码正序:

```asm
cmp  reg, right      ; 先比较,设置 flags —— emit_mrm 发的,后调用
jcc  exit_stub       ; 再根据 flags 跳 —— asm_guardcc 发的,先调用
```

具体例子。假设 `LT %008, %009`("假设 %008 小于 %009",带 guard),%008 在 EAX,%009 在 ECX:

```asm
cmp   eax, ecx              ; 3b c1 (cmp eax, ecx:eax-ecx,设 flags)
jge   exit_stub_3           ; 0f 8d xx xx xx xx (大于等于则跳,snapno=3)
```

`3b c1`:0x3b 是 `cmp r32, r/m32`(把 r/m 减去 r 比较——注意 x86 cmp 的方向是 `r - r/m`,但这里不重要,只看 flags),ModRM `c1` = 11 000 001 = `reg=eax, rm=ecx`,所以是 `cmp eax, ecx`。

`0f 8d xx xx xx xx`:0x0f 0x8d 是 `jge`(jump if greater or equal,即 SF=OF,对应 CC_GE)的长跳转。后面 4 字节是偏移,指向 exit stub。

执行流程:EAX 和 ECX 比较。如果 EAX >= ECX(假设不成立),跳到 exit stub——exit stub 把退出号(snapno=3)压栈,跳到 `lj_vm_exit_handler`,handler 根据 snapshot 恢复状态,退回解释器。如果 EAX < ECX(假设成立),不跳,继续往下执行机器码——这正是 trace 想要的"快路径"。

这就是 guard 在机器码里的样子:**cmp 设置 flags,jcc 失败跳转**。每条 guard 都是这个模式。一个 trace 里可能有几十条 guard,意味着几十对 cmp+jcc——这是 trace JIT 机器码里最常见的模式之一。

**guard 的微妙之处**:为什么 `asm_guardcc` 必须在所有寄存器分配**之后**调用?注释写得很清楚(`lj_asm_x86.h:71-74`):

```c
/* Emit conditional branch to exit for guard.
** It's important to emit this *after* all registers have been allocated,
** because rematerializations may invalidate the flags.
*/
```

因为寄存器分配过程中,可能触发 **rematerialization**(常量重新 load)——比如把某个寄存器里占着的常量丢弃,需要时重新 `mov reg, imm`。而 `mov` 之类指令**可能修改 flags**(比如 `xor eax, eax` 这种清零写法就改 flags)。如果在 jcc 之后又做了 remat,jcc 依赖的 flags 就被破坏了——会跳错。所以必须等所有可能改 flags 的操作(寄存器分配)都完了,最后才发 jcc。这正是上面 `asm_comp` 里 `asm_guardcc` 在 `emit_cmp` 之前调用的原因(反向生成,后调用的先执行,jcc 在机器码里反而靠后,cmp 靠前——但 jcc 是寄存器分配之后才发的,保证 flags 不被后续 remat 破坏)。

### 2.6 exit stub:guard 失败的去处

每个 guard 跳转的目标,是一个 **exit stub**——一小段固定的机器码,负责"把退出号告诉运行时,跳到退出处理函数"。看 `asm_exitstub_gen`(`lj_asm_x86.h:9`):

```c
static MCode *asm_exitstub_gen(ASMState *as, ExitNo group)
{
  ExitNo i, groupofs = (group*EXITSTUBS_PER_GROUP) & 0xff;
  MCode *target = (MCode *)(void *)lj_vm_exit_handler;
  MCode *mxp = as->mcbot;
  MCode *mxpstart = mxp;
  ...
  /* Push low byte of exitno for each exit stub. */
  *mxp++ = XI_PUSHi8; *mxp++ = (MCode)groupofs;          /* push 退出号低字节 */
  for (i = 1; i < EXITSTUBS_PER_GROUP; i++) {
    *mxp++ = XI_JMPs; *mxp++ = (MCode)((2+2)*(EXITSTUBS_PER_GROUP - i) - 2);
    *mxp++ = XI_PUSHi8; *mxp++ = (MCode)(groupofs + i);  /* 跳过前面的 push */
  }
  /* Push the high byte of the exitno for each exit stub group. */
  *mxp++ = XI_PUSHi8; *mxp++ = (MCode)((group*EXITSTUBS_PER_GROUP)>>8);  /* 高字节 */
  ...
  /* Jump to exit handler which fills in the ExitState. */
  ...
  *mxp++ = XI_JMP; ...                                    /* jmp lj_vm_exit_handler */
  ...
}
```

每个 exit stub 干的事:`push exitno`(把这个 guard 的退出号压栈)、然后跳到 `lj_vm_exit_handler`(汇编写的退出处理函数,负责根据 snapshot 恢复状态,见 P5-16/P5-17)。

一个 trace 有多个 guard,就有多个 exit stub。它们被**分组管理**,每组 32 个(`EXITSTUBS_PER_GROUP`,`lj_target_x86.h:165`)。组内的 stub 共享高字节部分(退出号的高字节),只 push 自己的低字节,然后跳到组尾统一 push 高字节、跳 handler。这是一种空间优化——避免每个 stub 都重复编码高字节。

exit stub 的地址,通过 `exitstub_addr(J, exitno)`(`lj_target.h:160`)算出:

```c
#define exitstub_addr(J, exitno) \
  ((MCode *)exitstub_addr_((char **)((J)->exitstubgroup), (exitno)))

static LJ_AINLINE char *exitstub_addr_(char **group, uint32_t exitno)
{
  return (char *)group[exitno / EXITSTUBS_PER_GROUP] +
         EXITSTUB_SPACING*(exitno % EXITSTUBS_PER_GROUP);
}
```

每个组的起始地址存在 `J->exitstubgroup[group]` 数组里。算地址:组起始 + 组内偏移(每个 stub 占 `EXITSTUB_SPACING = 4` 字节,即 push 一条 + jmp short 一条)。所以 guard 的 jcc 直接跳到这个算出来的地址——一次性、不用回填。

`asm_guardcc` 里那句 `MCode *target = exitstub_addr(as->J, as->snapno)`——`snapno` 就是这个 guard 对应的 snapshot 编号(也就是 exit 编号),用它算出 exit stub 地址。

### 2.7 第三层和第四层:asm_* 调 emit_*

我们已经看到,`asm_*` 这一层负责"理解 IR 语义、做寄存器分配、决定发射什么形态的指令"。但它们不直接写字节,而是调 `emit_*` 函数。`emit_*` 是**架构相关的最后一层**——把"逻辑指令"变成"具体字节"。

`lj_emit_x86.h` 里全是这种 `emit_*` 函数:

- `emit_rr(as, xo, r1, r2)`:`op r1, r2`,两个寄存器操作数。
- `emit_rmro(as, xo, rr, rb, ofs)`:`op rr, [rb+ofs]`,寄存器 + 内存(base+offset)。
- `emit_rmrxo(as, xo, rr, rb, rx, scale, ofs)`:`op rr, [rb+rx*scale+ofs]`,带 index 的内存操作数(用于数组访问 `t[i]` 这种)。
- `emit_rma(as, xo, rr, addr)`:`op rr, [addr]`,绝对地址。
- `emit_gri(as, xg, rb, i)`:`op rb, imm`,寄存器 + 立即数(group 指令形态)。
- `emit_mrm(as, xo, rr, rb)`:最灵活,rb 可以是寄存器也可以是之前融合好的内存操作数(`RID_MRM`)。
- `emit_loadi(as, r, i)`:`mov r, imm`,加载立即数(有 xor 优化:加载 0 用 `xor r,r` 更短)。
- `emit_movrr(as, ir, dst, src)`:`mov dst, src`,寄存器间搬。
- `emit_jcc(as, cc, target)`:`jcc target`,条件跳转。
- `emit_jmp(as, target)`:`jmp target`,无条件跳转(回边用)。
- `emit_call(as, f)`:调用 C 函数。

每个 `emit_*` 函数,核心是构造 x86 指令的**字节编码**。x86 指令编码非常复杂(变长、多前缀、ModRM/SIB/Displacement/VEX 等),`emit_op`(`lj_emit_x86.h:37`)是处理这些的底层:

```c
static LJ_AINLINE MCode *emit_op(x86Op xo, Reg rr, Reg rb, Reg rx,
                                 MCode *p, int delta)
{
  int n = (int8_t)xo;
  if (n == -60) {  /* VEX-encoded instruction */
    ...
    *(uint32_t *)(p+delta-5) = (uint32_t)xo;
    return p+delta-5;
  }
  ...
  *(uint32_t *)(p+delta-5) = (uint32_t)xo;
  p += n + delta;
#if LJ_64
  {
    uint32_t rex = 0x40 + ((rr>>1)&(4+(FORCE_REX>>1)))+((rx>>2)&2)+((rb>>3)&1);
    if (rex != 0x40) {       /* 需要 REX 前缀 */
      rex |= (rr >> 16);     /* REX.W(64 位)等 */
      ...
      *--p = (MCode)rex;
    }
  }
#endif
  return p;
}
```

`xo`(x86Op)是一个 32 位数,把操作码字节和"长度信息"打包在一起(`lj_target_x86.h:188` 那些宏):

```c
#define XO_(o)   ((uint32_t)(0x0000fe + (0x##o<<24)))
#define XO_0f(o) ((uint32_t)(0x0f00fd + (0x##o<<24)))
#define XO_66(o) ((uint32_t)(0x6600fd + (0x##o<<24)))
```

低字节 `0xfe`/`0xfd`/`0xfc` 不是真的指令字节,而是编码"这条指令有几个前缀字节、操作码多长"的元信息。`(int8_t)xo` 取低字节,负数表示有前缀(比如 `0xfe` = -2 表示 1 字节操作码 + 1 字节前缀,`0xfd` = -3 表示 2 字节操作码 + 1 字节前缀)。`emit_op` 用这个信息算出操作码到底要写几个字节、写在哪里。

这种"把操作码和元信息塞进一个 32 位数"的设计,让所有 `emit_*` 都能用同一套 `emit_op` 底层,非常紧凑。

REX 前缀(`0x40`-`0x4f`)是 x86-64 的扩展前缀,用来访问扩展寄存器(R8-R15、XMM8-XMM15)和 64 位操作。`emit_op` 在 64 位模式下根据 `rr`/`rb`/`rx` 的寄存器编号,自动决定是否要加 REX 前缀、加什么样的 REX。比如用到 R8D 及以上的寄存器,REX.B 位置 1;64 位操作(IRT_I64/IRT_U64 等),REX.W 位置 1。

`emit_*` 是 LuaJIT 后端**和具体 CPU 架构绑定的最后一层**。换一个架构(ARM/ARM64/PPC/MIPS),就有对应的 `lj_emit_arm.h` 等等。`asm_*` 这一层(`lj_asm_x86.h`)虽然名字带 x86,但很多逻辑(寄存器分配配合、guard 模式、融合策略)是跨架构的;真正架构不可移植的,是 `emit_*` 这层字节编码。

### 2.8 跳转回填:patch

最后讲清楚"前向跳转的回填"。前面提到两类回填:

**回填一:循环回边(`asm_loop_fixup`)**

`asm_loop_fixup`(`lj_asm_x86.h:2851`)在最末尾被调(`lj_asm.c:2630`),这时所有机器码都生成完了。它做的事:

```c
static void asm_loop_fixup(ASMState *as)
{
  MCode *p = as->mctop;          /* asm_tail_prep 预留的回边跳转位置 */
  MCode *target = as->mcp;       /* 扫完之后的循环开头位置 */
  ...
  p[-5] = XI_JMP;                              /* 写入 jmp 操作码 0xe9 */
  *(int32_t *)(p-4) = (int32_t)(target - p);   /* 回填偏移 */
  ...
  /* Realign small loops and shorten the loop branch. */
  if (newloop >= p - 128) {
    as->realign = newloop;   /* 循环够小,触发重对齐重试 */
    ...
  }
}
```

`asm_tail_prep`(`lj_asm_x86.h:2974`)在最开始(`lj_asm.c:2553`)就为回边跳转预留了 5 字节(`p -= 5`),空着。扫到 IR_LOOP 时,`asm_loop`(`lj_asm.c:1686`)被调,它做一些循环相关处理(`asm_phi_shuffle` 处理循环回边的寄存器搬运,`asm_phi_copyspill` 同步 spill 槽),然后记录 `as->mcloop = as->mcp`(标记循环开头的位置)。但**不在这里发回边跳转**——回边跳转的位置在尾部预留区(`mctop` 那边),不在循环开头。

扫完所有 IR、生成完头部之后,`target = as->mcp` 就是循环开头的最终地址(因为 mcp 经过头部生成后,指向了机器码的最低地址,即循环开头)。这时回头填那 5 字节:`jmp target`。

`newloop >= p - 128` 那段是个优化:如果循环足够小(跳转距离在短跳转 ±127 范围内),用 2 字节短跳转代替 5 字节长跳转,并对齐循环体到 16 字节边界——这能省字节 + 提高指令预取效率。代价是要重试一次(`as->realign = newloop` 触发 `lj_asm_trace` 的 `for(;;)` 重来)。注释说重试只发生一次,且很快。

**回填二:尾部跳转(`asm_tail_fixup`)**

非循环 trace(link 不是 LOOP),扫完之后要回填尾部——决定"这条 trace 跑完去哪"。看 `asm_tail_fixup`(`lj_asm_x86.h:2942`):

```c
static void asm_tail_fixup(ASMState *as, TraceNo lnk)
{
  MCode *mcp = as->mctail;       /* asm_tail_prep 预留的尾部位置 */
  MCode *target;
  int32_t spadj = as->T->spadjust;
  if (spadj) {  /* Emit stack adjustment. */
    ...                                  /* add esp, spadj */
  }
  /* Emit exit branch. */
  target = lnk ? traceref(as->J, lnk)->mcode : (MCode *)(void *)lj_vm_exit_interp;
  if (lnk || jmprel_ok(mcp + 5, target)) {  /* Direct jump. */
    *mcp++ = XI_JMP; mcp += 4;
    *(int32_t *)(mcp-4) = jmprel(as->J, mcp, target);
  } else {  /* RIP-relative indirect jump. */
    *mcp++ = XI_GROUP5; *mcp++ = XM_OFS0 + (XOg_JMP<<3) + RID_EBP; mcp += 4;
    *((int32_t *)(mcp-4)) = (int32_t)(as->J->exitstubgroup[0] - 16 - mcp);
  }
  /* Drop unused mcode tail. Fill with NOPs to make the prefetcher happy. */
  while (as->mctop > mcp) *--as->mctop = XI_NOP;
}
```

尾部跳转的目标有两种:

- `lnk != 0`:链到另一条 trace,目标就是那条 trace 的 `mcode`(入口点)。这是 trace linking(P5-19 讲),让一条 trace 跑完直接跳到下一条,不回解释器。
- `lnk == 0`:不链,退回解释器,目标是 `lj_vm_exit_interp`。

如果目标地址距离超过 32 位能表达的范围(`jmprel_ok` 失败,在 64 位下可能发生,因为代码可能分散在很远的地址),用 **RIP-relative 间接跳转**:跳转指令引用 `exitstubgroup[0] - 16` 处存的一个指针(那里存着 `lj_vm_exit_interp` 和 `lj_vm_exit_handler` 的地址,见 `asm_exitstub_setup` 的 `57-64`),通过间接跳转到达。这是 64 位长距离跳转的兜底。

末尾 `while (as->mctop > mcp) *--as->mctop = XI_NOP` 把预留但没用上的尾部字节填成 NOP(0x90)。注释说"让 prefetcher happy"——CPU 的指令预取器不喜欢半填的缓存行,NOP 填充让它干净。

到这里,整个汇编生成的源码脉络讲完了。**`lj_asm_trace` 总控 → `asm_ir` 分发 → `asm_*` 翻译 + 寄存器分配 → `emit_*` 发字节 → 最后回填跳转**。机器码就这样一字一字地生成了。

---

## §3 为什么这样设计是 sound 的

讲了源码,现在回答关键问题:这套翻译机器,为什么是**正确**的?为什么 IR 经过它翻成机器码之后,运行结果和解释器一致(假设成立时),或者能正确退回(假设失败时)?这是"把动态执行安全变成机器码"里"安全"两个字的又一处落地。

汇编生成的 soundness,有三层保证。

### 3.1 第一层:每条 IR 都被忠实翻译,语义不丢

这是最基础的。`asm_ir` 的 switch 覆盖了**所有可能的 IR opcode**——要么有对应的 `asm_xxx` 翻译,要么走 default 报 NYI 错误(放弃这条 trace)。不存在"某种 IR 被默默忽略"的情况——如果 IR 流水线上游产生了一条后端不认识的 IR,整个 trace 编译失败,代码退回解释器跑,绝不会生成"少翻译一条"的残缺机器码。

每条 IR 的翻译,都必须保证机器码的语义和 IR 一致。这听起来显然,实际上很多细节容易出错。举几个例子:

**算术的操作数顺序**。SUB 是不可交换的(`a-b ≠ b-a`)。`asm_swapops`(`lj_asm_x86.h:2053`)检查 `irm_iscomm(lj_ir_mode[ir->o])`——只有可交换运算(ADD/MUL/BAND/BOR/BXOR)才允许交换操作数。SUB/DIV/BSHL 这些不可交换的,严格保持原顺序。这一条保证了减法、除法、移位不会被翻译反。

**整数 vs 浮点的指令选择**。`asm_add` 第一行就 `if (irt_isnum(ir->t))`——浮点走 `XO_ADDSD`,整数走 `XOg_ADD`。这两条指令在 CPU 里是完全不同的电路(整数 ALU vs 浮点 SIMD 单元),生成的机器码字节也完全不同(浮点是 `f2 0f 58`,整数是 `01`/`03`)。混用会算错——所以类型判断必须严格。LuaJIT 用 IR 的类型字段 `ir->t` 来决定,这个字段在录制和窄化阶段(P2-08)就已经定下来了,反映了"这条运算运行时实际是什么类型"。

**64 位操作的 REX.W 前缀**。`REX_64IR(ir, r)`(`lj_emit_x86.h:543`)这个宏,如果 IR 类型是 64 位(`irt_is64`),给寄存器编号加上 `REX_64` 标志,`emit_op` 会据此生成 REX.W 前缀(0x48),告诉 CPU"这条指令是 64 位操作"。少了这个前缀,CPU 会按 32 位算(高 32 位被截断),FFI 的 int64 运算就会错。所以凡是涉及 64 位类型(IRT_I64/IRT_U64/IRT_P64 等,GC64 模式下的指针)的运算,都严格走这个宏。

这些细节,每一条都是"翻译正确性"的钉子。少了任何一个,机器码就会算错。

### 3.2 第二层:guard 的语义在机器码里完整保留

这是 trace JIT 特有的正确性要求。回忆 P0-01 的核心不变式:**机器码的结果要么和解释器一致(假设全成立),要么退回解释器(某假设失败)**。这个不变式能不能成立,关键看 guard 翻译得对不对。

guard 翻译要保证两件事:

**一,每个乐观假设都有对应的运行时检查。** IR 里凡是带 `IRT_GUARD` 标记的指令(`irt_isguard(ir->t)`,见 `lj_ir.h:602`),都意味着"这条指令做了一个乐观假设,运行时要检查"。检查怎么落地?就是 `asm_*` 里调 `asm_guardcc`——生成一对 cmp+jcc,失败跳 exit stub。

举几个 guard 的来源:

- **类型检查**:SLOAD 加载一个值,假设它是整数。`asm_sload`(`lj_asm_x86.h:1804`)里 `if (ir->op2 & IRSLOAD_TYPECHECK)`——要做类型检查,生成 `cmp [base+ofs+4], itype; jne exit_stub`。如果运行时那个槽不是整数标记,jne 跳走。
- **比较假设**:`LT %a, %b`("假设 a 小于 b"),`asm_comp` 翻成 `cmp a, b; jge exit_stub`——大于等于就跳。
- **溢出检查**:`ADDOV %a, %b`("假设 a+b 不溢出"),`asm_intarith` 里 `if (irt_isguard(ir->t)) asm_guardcc(as, CC_O)`——溢出标志位 OF 置位则跳(`CC_O`)。
- **数组越界**:ABC(Above Boundary Check),`asm_compmap` 里和 UGT 同构,翻译成无符号比较——索引 >= 长度则跳。

每种 guard,都有专门的 cmp + jcc 模式。**没有任何一个 guard 在翻译时被"省略"**——因为 `irt_isguard` 标记在 IR 里,`asm_*` 看到就必然调 `asm_guardcc`,没有"可省略"的分支。这保证了:**只要 IR 里标记了 guard,机器码里就一定有对应的检查**。

**二,guard 失败跳到正确的 exit stub,对应正确的 snapshot。** 每个 guard 在 IR 里关联一个 snapshot 编号(`as->snapno`,在反向扫描时由 `asm_snap_prev` 维护)。`asm_guardcc` 用 `exitstub_addr(as->J, as->snapno)` 算出对应的 exit stub 地址——每个 guard 跳到它**专属**的 exit stub。exit stub 把这个 snapno 压栈,跳到 `lj_vm_exit_handler`,handler 用这个 snapno 找到对应的 snapshot,照 snapshot 恢复解释器状态(P5-17 讲)。

这就保证:**guard 失败时,退到正确的位置、恢复正确的状态**。snapshot 里记录的"这一刻每个对解释器有意义的值在哪个寄存器/栈槽",和机器码里 guard 跳转那一刻的实际状态,必须严格对应——这正是上一章 §4.3 snapshot 驱动预分配的工作。寄存器分配和 snapshot 紧密配合,保证 guard 失败时状态可恢复。

### 3.3 第三层:跳转的回填不会错位

前面讲了两类回填(循环回边、尾部跳转)。回填的本质是:**生成跳转指令时目标未知,先占位;等目标确定了,再填上偏移**。这一步如果填错偏移,跳转就跳到错误地址,机器码崩溃。

LuaJIT 怎么保证回填正确?

**一,占位的位置精确记录。** 循环回边占位的位置是 `asm_tail_prep` 预留的 `mctop - 5`(固定 5 字节);尾部跳转占位的位置是 `as->mctail`(也由 `asm_tail_prep` 设置)。这些位置存在 `ASMState` 的字段里(`mctop`、`mctail`),回填时直接拿来用,不会"找不到占位在哪"。

**二,偏移用相对地址计算,自动适应。** `jmprel(J, p, target)`(`lj_emit_x86.h:493`)计算 `target - p`(p 是跳转指令的末尾地址,x86 跳转偏移的语义是从指令末尾算的):

```c
static LJ_AINLINE int32_t jmprel(jit_State *J, MCode *p, MCode *target)
{
  ptrdiff_t delta = target - p;
  ...
  return (int32_t)delta;
}
```

因为机器码最后会被 `memcpy` 到另一块可执行内存(P4-15 讲,因为生成时在内部分配的 buffer,完成后拷到正式的可执行区域),绝对地址会变,但**相对偏移不变**——所以用相对偏移编码跳转,拷贝之后仍然正确。这是 JIT 编译器普遍采用的技巧:**代码内部跳转一律用相对偏移,绝对地址只在跳到外部(trace linking 到别的 trace、跳到 lj_vm_exit_handler)时用,而且要保证在 32 位偏移可达范围内**。

`asm_tail_fixup` 里那个 RIP-relative 间接跳转,就是为了处理"目标地址太远,32 位偏移都够不到"的情况(64 位下,代码可能散布在远超 4GB 的地址空间)——用间接跳转,跳转指令引用一个存绝对地址的槽,通过两次跳转到达。这是极端情况下的兜底,保证回填**永远能成功**,不会因为地址太远而失败。

**三,断言兜底。** `lj_asm_trace` 末尾(`lj_asm.c:2623`):

```c
if (as->freeset != RSET_ALL)
  lj_trace_err(as->J, LJ_TRERR_BADRA);   /* Ouch! Should never happen. */
```

扫完之后,所有寄存器应该都回归空闲(`freeset == RSET_ALL`)。如果有寄存器没释放,说明寄存器分配有 bug(某个值占着寄存器但没释放),直接报错放弃。这是机器码正确性的最后一道运行时检查——虽然它检查的是寄存器分配,但寄存器分配错乱必然导致机器码错乱(操作数取错寄存器),所以这个检查也间接保护了汇编生成的正确性。

三层保证合起来:**每条 IR 忠实翻译(语义不丢)→ 每个 guard 都生成检查(假设不漏)→ 每个跳转都正确回填(控制流不乱)**。机器码运行的结果,要么和解释器一致,要么能正确退回。这正是"把动态执行安全变成机器码"在后端这一环的落地。

---

## §4 ★对照:官方 Lua 与 JVM/V8

把 LuaJIT 的汇编生成放回两个对照对象里,取舍会更清晰。

### 4.1 对照一:官方 Lua——根本没有"翻译成机器码"这一步

官方 Lua 是纯解释器。它运行 Lua 代码的方式是:**有一个固定的循环(在 C 里写的 `luaV_execute`),这个循环读字节码、查表、调用对应的 C 函数处理**。这个循环本身是被 GCC 编译成机器码的(就和任何 C 程序一样),但**这段机器码是固定的、通用的——它处理所有的 Lua 字节码,不针对任何具体的一段 Lua 代码做定制**。

也就是说,官方 Lua 没有"把用户的 Lua 代码翻成机器码"这一步。用户的 Lua 代码永远以字节码形式存在,被那个固定的解释器循环一遍遍读、一遍遍理解、一遍遍执行。

所以,从官方 Lua 到 LuaJIT,后端这一整套——`asm_ir` 分发、`asm_*` 翻译、`emit_*` 发字节、跳转回填——**全部是 JIT 新加的**。这是"加 JIT 到底加了什么"最具体的一笔账:加了一整套机器码生成器。这套生成器的复杂度(几千行 C,处理几百种 IR opcode × 几十种 x86 寻址模式),是 JIT 比解释器复杂得多的根本原因。

还有一个对照点值得提:**官方 Lua 的解释器循环,它的"分发表"是一个 C 的 switch**(`switch (op) { case OP_ADD: ...; case OP_SUB: ...; }`)。LuaJIT 的 `asm_ir` 也是一个 switch,看起来形似——但两者本质完全不同。解释器的 switch 是**运行时**的:每执行一条字节码,都要走一遍 switch 来选 case。而 `asm_ir` 的 switch 是**编译时**的:每条 IR 在编译时走一遍 switch 选好对应的机器码,从此那条机器码就固定了,运行时不再走 switch。这就是"编译"和"解释"的根本区别:**编译把分发的代价前置到编译时,运行时就不用再分发**。

(顺带:LuaJIT 自己的解释器 `lj_vm.s`,是**手写汇编**的,不用 C switch——因为手写汇编可以用更紧凑的 dispatch 表(跳转表),比 C switch 编译出来的代码快。这是 P1-02 的内容。)

### 4.2 对照二:JVM/V8——机器码生成的不同流派

JVM 和 V8 也是 JIT,它们也有"IR 翻机器码"这一步。但它们的机器码生成,在好几个维度上和 LuaJIT 不同。

**维度一:IR 的形式。** LuaJIT 的 IR 是 SSA + 线性(trace 是一条直线),`asm_ir` 按 opcode switch 分发。JVM C2 的 IR 是 **Sea-of-Nodes**(节点图,带依赖边),不是线性序列——它的代码生成要先做**线性化**(把图排成线性的机器码序列,考虑调度),再发机器码。V8 TurboFan 的 IR 也是图形式(TurboFan 的 `Schedule` 阶段把图变成线性)。图形式的 IR 表达力更强(能精确建模数据依赖和控制依赖),但代码生成更复杂;线性 IR(LuaJIT)更简单,适合 trace 这种本来就线性的结构。

**维度二:指令选择(instruction selection)。** 这是机器码生成的经典子问题:**给定一个 IR 操作,选哪条机器指令最合适?** LuaJIT 的指令选择比较简单——`asm_add` 优先试 lea,失败回退到 add,就这两条路。JVM C2 和 V8 TurboFan 用更复杂的指令选择算法(基于 BURG 或者模式匹配的树状覆盖),能识别更多优化模式(比如把 `a*4` 选成 `shl a, 2` 而不是 `imul`,把 `a*0+ b` 折成 `mov b` 等等)。LuaJIT 这些大部分在 IR 优化阶段就做了(`lj_opt_fold.c` 的常量折叠),所以后端指令选择可以简单。这是分工——优化在前端做透,后端就轻装。

**维度三:寄存器分配和代码生成的耦合。** LuaJIT 把寄存器分配**融合**在代码生成里——`asm_*` 边分配边生成(上一章讲的反向线性扫描的特征)。JVM C2 是**分离**的:先做完图着色寄存器分配(产生一个分配方案),再根据方案生成机器码。V8 TurboFan 也是分离的(register allocator 是独立 pass)。融合的好处是省一遍扫描、紧凑;分离的好处是模块清晰、容易做复杂优化(比如 coalescing)。LuaJIT 选融合,是因为 trace 短、线性扫描足够简单,融合不增加复杂度;JVM/V8 选分离,是因为它们的 IR 复杂,分离更好管理。

**维度四:机器码生成的产物形态。** LuaJIT 把机器码直接发到一块裸内存(`mcp` 指针往前长),最后 memcpy 到可执行区域——**手写字节,不经过任何中间层**。JVM C2 和 V8 TurboFan 通常通过一个**宏汇编器**(macro assembler)层——C++ 代码调用 `masm->add(rax, rbx)` 这种高层接口,宏汇编器内部维护一个指令列表,最后统一编码成字节。宏汇编器的好处是抽象清晰、容易扩展;LuaJIT 手写字节的好处是**极致紧凑、零开销**——每个 `emit_*` 直接写字节,没有中间表示。这呼应了 LuaJIT 整体的"小而快"哲学。

**维度五:deoptimization 的机器码。** JVM/V8 的 deoptimization(去优化,假设失效退回解释器),对应 LuaJIT 的 side exit。两者的机器码形态不同:

- LuaJIT:每个 guard 是 cmp + jcc,失败跳 exit stub。**检查在快路径上**(每次都执行 cmp),但 jcc 不跳时开销极小(分支预测命中率高)。
- JVM C2:**uncommon trap**。在假设点放一个"陷阱"(一个调用 deoptimization 的指令),但**快路径上不检查**——C2 假设热点代码的假设几乎不会失效,所以连检查都省了,只在失效时跳到 uncommon trap 处理。代价是失效时退得更慢(没有 per-point 的 snapshot,要从全局 deopt 表算)。

V8 TurboFan 介于两者之间:有些 guard 像 LuaJIT 那样 inline 检查,有些像 C2 那样用 trap。

LuaJIT 选 inline 检查,是因为它的 trace 假设非常乐观非常细(每个 guard 都很具体),失效概率不低(尤其是 side trace 还没长起来的时候),inline 检查让失效处理更快。这是 trace JIT 和 method JIT 在 guard 设计上的取舍差异。

把这些维度列个表:

| 维度 | LuaJIT(trace JIT) | JVM C2(method JIT) | V8 TurboFan(method JIT) |
|---|---|---|---|
| IR 形式 | SSA + 线性(trace) | Sea-of-Nodes(图) | TurboFan 图 |
| 代码生成分发 | switch by opcode | 线性化后遍历 | Schedule 后遍历 |
| 指令选择 | 简单(lea/add 二选一) | BURG/树覆盖 | 模式匹配 |
| 寄存器分配 | 融合(反向线性扫描) | 分离(图着色) | 分离(图着色变种) |
| 机器码生成 | 手写字节(emit_*) | 宏汇编器 | 宏汇编器 |
| 假设失效 | inline cmp+jcc + exit stub | uncommon trap | 混合 |

这张表能看出一个规律:**编译单位越大、优化越重,代码生成越复杂、越分层(图 IR + 分离分配 + 宏汇编器);编译单位越小、越追求编译速度,代码生成越紧凑(线性 IR + 融合分配 + 手写字节)**。LuaJIT 是后者的极致——后端整个 `lj_asm.c` + `lj_asm_x86.h` + `lj_emit_x86.h` 加起来 6000 多行,就实现了一个完整的 x86-64 代码生成器;同样的功能在 JVM C2 里要分散在好几个文件、上万行。这是"小而美"的胜利,也是 trace JIT 哲学的体现:用最小的代码,解决恰好匹配的问题。

还有一个对照点:**架构可移植性**。LuaJIT 的 `emit_*` 层是架构绑定的(`lj_emit_x86.h`/`lj_emit_arm.h`/...),`asm_*` 层(`lj_asm_x86.h`/`lj_asm_arm.h`/...)也部分架构相关。但 LuaJIT 的设计是**每个架构一套独立的 `lj_asm_<arch>.h`**,而不是用一套跨架构的中间层。这意味着加一个新架构,要重写整个 `lj_asm_<arch>.h`——工作量大,但每个架构的代码可以做到极致优化(不用为跨架构抽象付代价)。JVM C2 和 V8 用跨架构的宏汇编器(共享更多代码),新架构接入更快,但每个架构的代码没那么极致。这是工程取舍:**LuaJIT 宁可每个架构独立实现,也不引入跨架构抽象层**——因为它要支持的架构少(x86/ARM/ARM64/PPC/MIPS),每个都值得手工打磨。

---

## §5 回扣主线

这一章我们拆了后端的最后一跳:汇编生成。

回到主线"把动态执行安全变成机器码"。寄存器分配(上一章)解决了"值放哪"的问题,但放到寄存器里的值,要参与**运算**、要做**检查**、要**跳转**——这些动作怎么变成机器码,就是本章的主题。从 IR 到机器码,是"半成品"到"成品"的最后一道工序。

这道工序的核心,是 `lj_asm_trace` 驱动的**反向扫描 + 四层翻译**:

- **`lj_asm_trace`** 总控,反向扫 IR,配合反向线性扫描寄存器分配。
- **`asm_ir`** 按 opcode 分发,每条 IR 找到对应的翻译函数。
- **`asm_*`** 具体翻译,理解 IR 语义、做寄存器分配、决定指令形态——ADD 翻成 add/lea,SLOAD 翻成 mov,guard 翻成 cmp+jcc。
- **`emit_*`** 发字节,把逻辑指令变成 x86 的变长字节序列,处理 ModRM/SIB/REX/VEX 所有编码细节。

这套设计的每一步,都呼应着主线的三股张力:

- **快**:利用 x86 的内存操作数融合(load 塞进运算指令)、lea 三操作数加法器、短立即数编码——能省一条指令就省一条,能短一个字节就短一个字节。这些是"快"在机器码层面的具体落地。
- **省**:反向扫描让寄存器分配和代码生成融合成一遍,不重复扫描;手写字节层(emit_*)零开销,没有宏汇编器的中间表示;小循环还能重对齐省缓存行。这些是"省"(编译开销小、生成代码紧凑)的体现。
- **安全**:每个 guard 都翻译成 cmp+jcc,失败跳 exit stub;跳转回填用相对偏移,memcpy 后仍正确;末尾有 `freeset == RSET_ALL` 的断言兜底。这些保证机器码要么算对(假设成立),要么正确退回(假设失败)——绝不比解释器更错。

特别值得强调的是第三点。汇编生成不是孤立地追求"指令选得漂亮",它要**和 guard 机制精密配合**。`asm_guardcc` 必须在所有寄存器分配之后调用(避免 remat 破坏 flags),每个 guard 跳到它专属的 exit stub(对应正确的 snapshot),跳转回填用相对偏移保证 memcpy 后仍正确——这些细节,都是为了让"乐观假设 + 运行时检查 + 失败可回退"这条主线,在机器码这一层严丝合缝地落地。guard 在 IR 里只是一个标记,在机器码里是一对真实的 cmp+jcc 字节——这是"假设"变成"机器里的检查"的临界点。

汇编生成是后端的终点,但不是 trace 生命周期的终点。生成的机器码,还要**拷到一块真正的可执行内存里**(生成时用的 buffer 不一定可执行,或者需要 W^X 切换),CPU 才能跳进去执行。这块可执行内存怎么管理、W^X 怎么处理、机器码怎么 patch(trace linking 时要改别的 trace 的跳转目标),是下一章的内容。

---

*下一章 [P4-15 后端目标与机器码内存](P4-15-后端目标与机器码内存.md):机器码生成完了,放在哪?怎么让一块普通的内存变成 CPU 能执行的"代码内存"?W^X(写时不可执行、执行时不可写)的安全约束怎么满足?trace 链接时要怎么 patch 已有机器码的跳转目标?这些是后端的最后一公里——让生成的字节真正跑起来。*
