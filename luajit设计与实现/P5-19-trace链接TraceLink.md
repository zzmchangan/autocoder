# P5-19 trace 链接(TraceLink 9 种)

> **本书主线**:把动态执行安全变成机器码。**二分法**:解释器侧 ↔ JIT 侧。本章落在 **JIT 侧·运行时收尾**,承接 [P5-18 side trace:从退出点录制新 trace](P5-18-side-trace从退出点录制新trace.md):一条 trace 录好、编译完、装上机器码之后,它跑到最后一条机器码,接下来该跳到哪里?
>
> **本章回答的核心问题**:一条 trace 不是孤立的一段机器码——它跑到底必须有去向。去向只有三类:回到自己(循环)、跳到别的 trace、退回解释器。LuaJIT 把这些去向分成 **9 种链接方式(TraceLink)**,并据此生成 trace 的结尾跳转机器码,让多条 trace 织成一张"机器码跳转网",程序尽量一直在机器码里跑、少回解释器。
>
> **★对照**:JVM/V8 的方法间内联与调用链接 vs LuaJIT 的 trace 链接。
> **源码**:LuaJIT 2.1.ROLLING,`lj_jit.h`(`TraceLink` 枚举@237、`GCtrace` 的 `link`/`linktype`/`mcloop` 字段@271/275/281)、`lj_record.c`(`lj_record_stop` 设 link@299,各种 linktype 的录制决定)、`lj_trace.c`(`trace_stop` 安装 link@499)、`lj_asm.c`(`asm_tail_link` 退解释器前导@2131、`asm_loop` 记 mcloop@1686)、`lj_asm_x86.h`(`asm_tail_fixup` 生成跳转@2942、`asm_tail_prep` 留退出空间@2974、`asm_loop_fixup` LOOP 回跳@2851)、`lj_ffrecord.c`(`rec_stop_stitch_cp` STITCH 录制@101)。
> **基调**:纯直球,不用比喻;从第一性原理推导。

---

## 第一部分:这章解决什么问题(第一性原理推导)

### §1 一条 trace 跑到底,机器码停在哪

前四章(P5-16 到 P5-18)把 trace JIT 运行时的"内部控制流"讲完了:guard 检查假设、snapshot 恢复状态、side trace 把失败路径也变成机器码。但还有一个最朴素的问题没回答,它关系到 trace 作为一段机器码的**完整性**。

这个问题是:**一条 trace 编译出来,是一段有起点、有终点的机器码。CPU 执行到这条机器码的最后一条,接下来该去哪?**

听起来简单,细想却是个必须正面回答的设计问题。因为机器码不像解释器那样有"dispatch 循环"——解释器执行完一条字节码,自动跳到下一条(dispatch 表分发),这是解释器的固有结构。而一段 trace 机器码,它就是一段直线代码,CPU 跑完最后一条指令,如果后面什么都没有,程序就崩了(执行流掉进未定义内存)。所以**trace 的机器码末尾,必须显式地有一条"去向指令"**——一条跳转,告诉 CPU 接下来跳到哪。

这条"去向指令"跳的目标,就是 trace 链接的核心。它跳到哪里,决定了这段 trace 跑完之后的命运。

P0-01 §13 简要列过三种去向:回到自己(循环)、链到别的 trace、退回解释器。本章把这件事彻底讲透——LuaJIT 不是只分这粗粗三种,而是分成了 **9 种链接方式**,每一种对应一种特定的控制流场景。为什么要分这么细?因为不同场景下,"trace 跑完后程序的真实状态"和"目标 trace 期望的入口状态"对齐方式不同,需要不同的机器码处理。这是把"去向"做对、做快所必需的精度。

### §2 为什么不能"一律退回解释器"

先问一个反问题:trace 跑完,统一退回解释器不就行了?反正解释器是完整的 Lua 实现,从任何点都能接着跑。这样链接方式就一种,多简单。

不行,原因正是前几章反复强调的那个张力——**快**。退回解释器意味着:走 side exit 的完整流程(exit stub 存寄存器 → `lj_snap_restore` 恢复栈 → 解释器 dispatch 重新理解字节码)。这套流程 P5-16/P5-17 讲过,每一次都要付出退出恢复开销 + 解释执行开销。

对于一个跑 100 万次的循环,如果每次循环体跑完都退回解释器、再由解释器重新进 trace,那 100 万次退出恢复 + 100 万次解释 dispatch,几乎把 trace JIT 的加速全部吃光。**trace 的价值,恰恰在于"尽量不退回解释器"**。

所以理想情况是:trace 跑完后,**直接跳到下一段机器码继续跑**。这段机器码可能是:

- 自己(如果是循环 trace,跑完一圈再跑一圈);
- 另一条已编译的 trace(如果本 trace 的出口正好接上另一条 trace 的入口);
- 一段"缝合"代码(本 trace 中间有没法录的一小段,切解释器跑一小段再回来)。

只有实在接不上的时候,才退回解释器。9 种链接方式,就是把这几种"接得上"和"接不上"的情况,精确地枚举出来,各自生成对应的结尾跳转机器码。

### §3 第一性原理:trace 是线性代码,链接决定它的"出口形状"

要从第一性原理推导 9 种链接,先得看清 trace 机器码的**形状**。

trace 是一条线性路径(P0-01 §10):没有内部分支(分支都变成了 guard,失败就 side exit)。所以一段 trace 的机器码,从入口到出口,是一条直线——入口处做些准备(从 ExitState 取值,如果是 side trace),中间是各种计算和 guard,出口处是"去向指令"。

这条直线的**出口形状**,由两件事决定:

1. **这条 trace 是不是循环**。如果是循环 trace(录的是某个循环回边的一圈),那它跑完一圈后,逻辑上应该回到循环开头再跑一圈——出口要跳回自己的入口附近。如果不是循环(比如录的是一段直来直去的代码、或一个函数调用链),跑完就该去别处。
2. **跑完后接得上谁**。循环 trace 接自己;非循环 trace 看出口能不能接上另一条已编译的 trace;都接不上就退解释器。

这两件事组合,产生了几种典型出口形状。LuaJIT 用 `TraceLink` 枚举把这几种形状编号。在讲 9 种之前,先用最常见的两种——LOOP 和"退解释器"——把出口形状看清楚。

### §4 LOOP:循环 trace 跑完跳回自己

最常见的链接是 LOOP。一个循环 trace(比如录了 `for i=1,N do x=x+i end` 的一圈循环体),它跑完一圈,`i` 加了 1,`x` 加了 `i`,逻辑上该回到循环开头判断 `i<=N` 并跑下一圈。如果每圈都成立(大部分时候),那就一直跑下去。

这种 trace 的机器码形状是**首尾接环**:出口的跳转指令,目标指向自己入口附近的某个点(不是真正的入口,是循环体开始处,叫 mcloop)。这样 CPU 跑完一圈,跳回 mcloop,再跑一圈,再跳回……形成一个闭环,可以无限跑下去,根本不碰解释器。**这是 trace JIT 最爽的情况——整段循环都在机器码里飞奔,解释器完全旁路。**

LOOP 链接的关键点是:trace 机器码的**开头**和**结尾**是配合设计的。开头在 mcloop 处(循环体起点),结尾是一条跳转指令跳回 mcloop。两头一接,环就闭上了。这要求生成机器码时知道"循环体从哪开始",这个位置记在 `GCtrace.mcloop` 字段(`lj_jit.h:271`)。

注意 LOOP 的一个细节:trace 跑完一圈回到 mcloop 时,它不是无条件回——循环本身也有 guard(比如 `i<=N` 的检查,如果 `i` 超了就 side exit)。所以 LOOP trace 的机器码里,既有"跑完一圈跳回开头"的闭环跳转,也有中间各处"假设失败就 side exit"的 guard 跳转。两种跳转共存,各管各的。

### §5 退解释器:trace 跑完接不上谁

另一种常见情况是退回解释器。一条非循环的 trace(比如录了一段从不回边的直线代码),跑完最后一条机器码,后面没有别的 trace 可接(或者要接的 trace 还没编译),这时候只能退回解释器。

退回解释器的出口形状是:结尾跳转指令的目标,是解释器的一个入口函数(汇编里叫 `lj_vm_exit_interp`)。跳进去之前,要把 trace 此刻的状态(寄存器里的值)摆成解释器期望的样子——这就是 snapshot 恢复(P5-17)。snapshot 恢复好之后,跳进 `lj_vm_exit_interp`,解释器从 trace 退出点对应的字节码接着跑。

这种"退解释器"的链接,对应 `TraceLink` 里的两种:

- **`LJ_TRLINK_INTERP`**:fallback 到解释器。最常见——trace 跑完了,老老实实回解释器。
- **`LJ_TRLINK_RETURN`**:返回到解释器。专门用于函数返回场景——trace 录的是一个函数的某段,跑完后函数要返回,返回点在解释器侧。

两者的机器码出口形状几乎一样(都跳 `lj_vm_exit_interp`),区别在于录制时的语义:`INTERP` 是通用的"接不上就退",`RETURN` 是明确的"这是一次函数返回"。分开编号是为了让 trace_stop 安装时知道 trace 的语义,做对应的处理(比如 RETURN 类型的 trace,后续如果调用者也被录进 trace,可能改链接)。

### §6 现在正式看 9 种 TraceLink

有了 LOOP 和"退解释器"垫底,现在把 9 种链接一次性摆出来。它们在 `lj_jit.h:237`:

```c
236	/* Type of link. ORDER LJ_TRLINK */
237	typedef enum {
238	  LJ_TRLINK_NONE,		/* Incomplete trace. No link, yet. */
239	  LJ_TRLINK_ROOT,		/* Link to other root trace. */
240	  LJ_TRLINK_LOOP,		/* Loop to same trace. */
241	  LJ_TRLINK_TAILREC,		/* Tail-recursion. */
242	  LJ_TRLINK_UPREC,		/* Up-recursion. */
243	  LJ_TRLINK_DOWNREC,		/* Down-recursion. */
244	  LJ_TRLINK_INTERP,		/* Fallback to interpreter. */
245	  LJ_TRLINK_RETURN,		/* Return to interpreter. */
246	  LJ_TRLINK_STITCH		/* Trace stitching. */
247	} TraceLink;
```

注释里有个 `ORDER LJ_TRLINK`——枚举顺序有意义,后面会看到 `trace_stop` 之类的地方偶尔按值分类。逐个讲每种链接的场景、动机、出口形状。

**`LJ_TRLINK_NONE`(0):未完成的链接。** trace 还在录制中,链接类型还没定。录制结束调 `lj_record_stop` 时才赋上具体的 linktype。所以 NONE 是初始值,不是真正的"链接方式"。

**`LJ_TRLINK_LOOP`(2):循环回自己。** §4 讲过,最常见。trace 录的是某个循环回边的一圈,跑完跳回自己的 mcloop。`link` 字段等于自己的 traceno。

**`LJ_TRLINK_ROOT`(1):链到另一条 root trace。** 这条 trace 跑完,正好接上另一条已编译的 root trace 的入口,直接跳过去。`link` 字段存目标 root trace 的 traceno。场景:本 trace 录到某个点,发现前面正好是另一个已编译循环的入口(比如一个函数调用进了一个已编译的循环),那就链过去,不用退解释器。

**`LJ_TRLINK_INTERP`(6):退回解释器。** §5 讲过,通用 fallback。

**`LJ_TRLINK_RETURN`(7):返回到解释器。** §5 讲过,函数返回专用。

**`LJ_TRLINK_TAILREC`(3):尾递归。** 录的是尾递归调用链(函数 f 在末尾调用自己 f)。trace 把"调用 f → f 又调用 f → ..."这条尾递归链录成一条 trace,跑完跳回自己的入口(类似 LOOP,但语义是尾递归归约)。`link` 等于自己的 traceno。

**`LJ_TRLINK_UPREC`(4):上递归。** 录的是递归下降(f 调用 f 调用 f,层层深入),trace 跑完跳回自己,表示"还要再深一层"。

**`LJ_TRLINK_DOWNREC`(5):下递归。** 和 UPREC 相反,录的是递归回升(深层 f 返回到浅层 f)。也是跳回自己。

这三种(TAILREC/UPREC/DOWNREC)是递归相关的链接。它们在机器码层都表现为"跳回自己的入口"(像 LOOP),但语义不同:LOOP 是普通循环,TAILREC 是尾递归归约,UPREC/DOWNREC 是递归的深入/回升。分开编号是因为录制时的处理不同(递归需要特殊的栈帧处理),也为了调试和统计能区分。对运行时性能来说,它们都让递归代码在机器码里跑而不退解释器,是 trace JIT 对递归的优化。

**`LJ_TRLINK_STITCH`(8):缝合。** 这是最特殊的一种,§7 专门讲。

把这 9 种按"出口去向"归类,其实只有三大类:

| 去向 | 包含的 linktype | 机器码出口 |
|---|---|---|
| 跳回自己 | LOOP, TAILREC, UPREC, DOWNREC | 跳转目标 = 自己的 mcloop |
| 跳到别的 trace | ROOT | 跳转目标 = 目标 trace 的 mcode |
| 退回解释器 | INTERP, RETURN | 跳转目标 = lj_vm_exit_interp |
| 缝合 | STITCH | 特殊:切解释器跑一小段再回来 |
| 未定 | NONE | 录制中,还没定 |

所以 9 种看起来多,本质就是"回自己 / 找别人 / 退解释器 / 缝合"四种去向,递归相关的三种是"回自己"的细分,INTERP/RETURN 是"退解释器"的细分。理解了四大类,9 种就记住了。

### §7 STITCH:缝合——trace 中间有没法录的一小段

STITCH 是最值得专门讲的一种,因为它体现了 LuaJIT"不放弃整条 trace"的设计哲学。

考虑这个场景:正在录一条 trace,录到一半,遇到一个**没法 JIT 录制**的字节码——比如某个还没有 JIT 实现的库函数(FFI 之外的某些 C 函数),或者某个暂时 NYI(Not Yet Implemented)的操作。这时候有两条路:

**路一:放弃整条 trace。** 遇到录不进的字节码,直接 abort,这条 trace 不编了。代价是:前面已经录的好好的那一大段,全部白费,程序继续在解释器跑,这段热代码得不到加速。

**路二:缝合(STITCH)。** 不放弃整条 trace。把"录不进的那一小段"标记出来,trace 机器码跑到那里时,**切到解释器执行这一小段**,执行完再**回到 trace 机器码**继续往下跑。这样整条 trace 保住了,只有那一小段走解释器,其余全是机器码。

显然路二更优——它把"局部不可录"和"整体可加速"分开了,不让一个老鼠屎坏一锅汤。这就是 STITCH 的动机。

STITCH 的出口形状和别的都不同:它不是"trace 跑完后跳哪",而是"trace 跑到中途,切出去再切回来"。具体实现是用一个**续体(continuation)**:trace 机器码跑到缝合点,不是 side exit 退解释器(那会结束 trace),而是设置一个特殊的续体帧,让解释器执行那一小段后,**主动跳回 trace 机器码的缝合点之后**继续跑。

这个续体在源码里是 `lj_cont_stitch`(`lj_vm.h:114`,一段手写汇编)。缝合发生时,录制器在 Lua 栈上插入一个 continuation frame(`recff_stitch`,`lj_ffrecord.c:110`),记录"执行完那一小段后,回到 trace 的哪里"。运行时,解释器跑完那一小段,碰到 continuation frame,就跳进 `lj_cont_stitch`,它再把控制权交回 trace 机器码。

STITCH 的代价是:缝合的那一小段走解释器(慢),且进出缝合有切换开销。但它换来了"整条 trace 保住",大多数时候是净赚。这是 trace JIT 在"乐观"和"实用"之间的平衡——能录的尽量录,录不进的缝合过去,不轻易放弃。

需要说明的是,STITCH 主要用在**快速函数(builtin fast functions)**的录制里。当录制器遇到一个 builtin(比如 `string.byte` 的某个变体)但它暂时没实现 JIT 录制,就用 STITCH 缝合。普通字节码如果录不进,通常是 NYI,直接 abort 更干净(因为普通字节码没法"切出去跑一小段")。所以 STITCH 的使用场景是受限的,但在这个场景里它很有价值。

### §8 link 字段和 linktype 字段:trace 怎么记住自己的链接

讲完了 9 种链接的语义,现在看 trace 怎么把自己的链接信息存下来。`GCtrace` 有两个字段(`lj_jit.h:275/281`):

```c
275	  TraceNo1 link;	/* Linked trace (or self for loops). */
...
281	  uint8_t linktype;	/* Type of link. */
```

- **`linktype`**:一个字节,存 `TraceLink` 枚举值(0–8)。告诉后端"这条 trace 的链接是哪种"。
- **`link`**:存链接目标的 traceno。对 LOOP/TAILREC/UPREC/DOWNREC,`link == 自己的 traceno`(回自己);对 ROOT,`link == 目标 trace 的 traceno`;对 INTERP/RETURN/STITCH,`link == 0`(没有具体目标 trace,退解释器或缝合)。

这两个字段在**录制结束时**由 `lj_record_stop` 设置(下一节看源码)。设置好后,后端代码生成时读这两个字段,据此生成结尾跳转机器码。

有个关键点:`link` 字段不仅录制时定,**运行时还可能被改**。最典型的场景是 STITCH——`trace_stop` 里,新 trace 装上时,会把**前一条 trace**(通过 STITCH 接到本 trace 的那条)的 `link` 字段改成指向本 trace(`lj_trace.c:550`)。这样前一条 trace 跑到缝合出口,就知道"缝到新 trace"。这是链接的动态更新,让 trace 网随编译进展越来越密。

### §9 把 9 种链接在录制侧的来源看清楚

9 种 linktype 不是凭空选的,每一种都对应录制时遇到的一种字节码场景。录制器(`lj_record.c`)在录到特定场景时,调 `lj_record_stop(J, linktype, lnk)` 结束录制并设好链接。下面把每种 linktype 在录制侧的触发场景列出来(对应源码行号在第二部分逐一印证):

- **LOOP**:录到循环回边(BC_FORL/BC_ITERL/BC_LOOP),且回到本 trace 的起点。`rec_loop_interp` `lj_record.c:637`。
- **ROOT**:本 trace 跑到某个点,接上一个已编译的 root trace。`rec_loop_jit` `lj_record.c:670`、`rec_func_jit` `lj_record.c:1967`。
- **TAILREC**:尾递归归约。录到一个函数的入口,而本 trace 正好也是从这个函数起录的(尾递归自己调自己)。`rec_func_jit` `lj_record.c:1965`。
- **UPREC**:上递归。`rec_call` 里检测到递归深入。`lj_record.c:1888`。
- **DOWNREC**:下递归。`rec_ret` 里检测到递归回升。`lj_record.c:977`。
- **INTERP**:通用 fallback。录制遇到无法继续的情况(超 maxside、类型不稳定等),放弃但优雅退出。`lj_record.c:2891`。
- **RETURN**:函数返回到解释器。`rec_ret` `lj_record.c:952`、`lj_ffrecord.c:186`。
- **STITCH**:缝合。`rec_stop_stitch_cp` `lj_ffrecord.c:104`。

每一种都对应一个明确的录制场景。这印证了 §6 的归纳:9 种链接是 9 种"trace 跑完后程序真实去向"的精确枚举,不是拍脑袋分的。

### §10 用一个例子演示:多条 trace 织成网

讲清了 9 种链接,现在用一个稍复杂的例子,看 trace 怎么织成网。还是 P0-01/P5-18 那个循环,但加点东西:

```lua
local x = 0
for i = 1, 1000000 do
  if i < 900000 then
    x = x + 1
  else
    x = math.abs(x - 1)   -- 假设 math.abs 被 JIT 录制支持
  end
end
```

这个程序跑起来,会形成几条 trace:

1. **root trace R**(LOOP):录主流路径 `i<900000 → x=x+1 → 回循环`。link = R 自己(LOOP),跑完跳回 mcloop。
2. **side trace S1**(从 R 的 `i<900000` guard 退出点录):录失败路径 `i>=900000 → x=math.abs(x-1) → 回循环`。如果它录完能接回 R 的循环开头,link = R(ROOT,链到 root trace);如果它自己形成闭环(回到自己的起点),link = S1 自己(LOOP)。

这样 R 和 S1 就织成了一张网:R 跑一圈,`i<900000` 成立,LOOP 回 R 自己再跑一圈;某次 `i>=900000`,R 的 guard 失败,patchexit 让它跳进 S1(P5-18 讲的热补丁),S1 跑完 `math.abs(x-1)`,link 到 R(ROOT),跳回 R 的 mcode 继续循环。整张网全是机器码跳机器码,不退解释器。

这就是 trace 链接的价值:**把多条 trace 接成一张跳转图,让程序在图里转圈,而不是频繁进出解释器**。LOOP 让单条循环 trace 自闭环;ROOT/SIDE-TO-ROOT 让 root 和 side 互跳;退解释器只在真的接不上时发生。

把第一部分总结一下:trace 的结尾必须有去向指令,去向分四大类(回自己/找别人/退解释器/缝合),LuaJIT 精确枚举成 9 种 linktype,每种对应一种录制场景。链接让多条 trace 织成机器码跳转网,这是 trace JIT"让程序尽量在机器码里跑"的关键一步。下面进源码,把这套机制逐行印证。

---

## 第二部分:源码怎么实现

### §11 录制结束设链接:lj_record_stop

trace 的链接信息,在录制结束时由 `lj_record_stop` 设上。这个函数是链接的"决策点"——录制器在各种场景下调用它,传入 linktype 和 lnk,它负责把这两个值存进 `J->cur`。源码在 `lj_record.c:299`:

```c
299	void lj_record_stop(jit_State *J, TraceLink linktype, TraceNo lnk)
300	{
301	#ifdef LUAJIT_ENABLE_TABLE_BUMP
302	  if (J->retryrec)
303	    lj_trace_err(J, LJ_TRERR_RETRY);
304	#endif
305	  lj_trace_end(J);
306	  J->cur.linktype = (uint8_t)linktype;
307	  J->cur.link = (uint16_t)lnk;
308	  /* Looping back at the same stack level? */
309	  if (lnk == J->cur.traceno && J->framedepth + J->retdepth == 0) {
310	    if ((J->flags & JIT_F_OPT_LOOP))  /* Shall we try to create a loop? */
311	      goto nocanon;  /* Do not canonicalize or we lose the narrowing. */
312	    if (J->cur.root)  /* Otherwise ensure we always link to the root trace. */
313	      J->cur.link = J->cur.root;
314	  }
315	  canonicalize_slots(J);
316	nocanon:
317	  /* Note: all loop ops must set J->pc to the following instruction! */
318	  lj_snap_add(J);  /* Add loop snapshot. */
319	  J->needsnap = 0;
320	  J->mergesnap = 1;  /* In case recording continues. */
321	}
```

逐行读:

**第 305 行**:`lj_trace_end(J)`。把 JIT 状态机从 RECORD 推到 END,表示录制结束,后面进优化和汇编。

**第 306–307 行**:核心。把传入的 `linktype` 和 `lnk` 存进 `J->cur.linktype` 和 `J->cur.link`。这就是 §8 讲的两个字段。录制器在调用 `lj_record_stop` 时已经决定了用哪种链接(比如循环就用 `LJ_TRLINK_LOOP`,目标是自己就用 `J->cur.traceno`),这里只是存下来。

**第 308–314 行**:一个针对循环的重要修正。条件 `lnk == J->cur.traceno && J->framedepth + J->retdepth == 0` 的意思是"链接目标是自己在同一栈层"(典型的循环)。这种情况下:

- 如果开了循环优化(`JIT_F_OPT_LOOP`),`goto nocanon` 跳过 `canonicalize_slots`。为什么跳过?注释说"Do not canonicalize or we lose the narrowing"——规范化栈槽会丢失类型窄化信息(P2-08 讲窄化)。循环优化需要保留窄化,所以不规范化。
- 如果没开循环优化,且这是条 side trace(`J->cur.root` 非 0),把 `link` 改成 `J->cur.root`——确保 side trace 的循环链接总是指向 root trace,而不是 side 自己。这是个保守的 fallback:没开循环优化时,不让 side trace 自闭环,统一链回 root。

这一段处理了"循环链接的精细化":开循环优化时保留窄化让 loop optimization 发挥;没开时确保链接语义清晰(回 root)。

**第 315 行**:`canonicalize_slots(J)`。规范化栈槽(非循环情况)。把 trace 里的栈槽整理成标准形式,方便后端处理。

**第 318 行**:`lj_snap_add(J)`。拍最后一张 snapshot——循环/链接 snapshot。这张 snapshot 记录"trace 跑到链接出口时的状态",如果这条 trace 后来被 side exit(虽然它主要走 link,但也可能有别的 guard 失败),靠这张 snapshot 恢复。注释"all loop ops must set J->pc to the following instruction"提醒:循环类字节码录制完,`J->pc` 要指向下一条字节码(因为循环回边回到循环头,但录制视角是"已执行完这条回边")。

`lj_record_stop` 本身不复杂,它是个"存值 + 规范化 + 拍末 snapshot"的收尾函数。真正的链接决策在调用它的各处(§9 列的那些场景)。下面看几个典型的调用场景。

### §12 录制侧的链接决策:循环

最常见的循环链接。录制器录到一个循环回边字节码(BC_FORL/BC_ITERL/BC_LOOP),调 `rec_loop_interp`(`lj_record.c:628`)处理:

```c
628	/* Handle the case when an interpreted loop op is hit. */
629	static void rec_loop_interp(jit_State *J, const BCIns *pc, LoopEvent ev)
630	{
631	  if (J->parent == 0 && J->exitno == 0) {
632	    if (pc == J->startpc && J->framedepth + J->retdepth == 0) {
633	      if (bc_op(J->cur.startins) == BC_ITERN) return;  /* See rec_itern(). */
634	      /* Same loop? */
635	      if (ev == LOOPEV_LEAVE)  /* Must loop back to form a root trace. */
636		lj_trace_err(J, LJ_TRERR_LLEAVE);
637	      lj_record_stop(J, LJ_TRLINK_LOOP, J->cur.traceno);  /* Looping trace. */
638	    } else if (ev != LOOPEV_LEAVE) {  /* Entering inner loop? */
...
646		lj_trace_err(J, LJ_TRERR_LINNER);  /* Root trace hit an inner loop. */
...
656	  }  /* Side trace continues across a loop that's left or not entered. */
657	}
```

**第 631 行**:`J->parent == 0 && J->exitno == 0`——这是 root trace(parent 为 0)。side trace 走别的分支。

**第 632 行**:`pc == J->startpc && J->framedepth + J->retdepth == 0`——当前字节码地址等于 trace 的起始地址,且栈深度和返回深度都是 0(在同一栈层)。这表示**转回到 trace 起点了**——形成一个闭环。

**第 635–636 行**:如果这是个 `LOOPEV_LEAVE`(离开循环)事件却回到了起点,矛盾,报错 `LJ_TRERR_LLEAVE`。要形成 root trace,必须是真正地 loop back。

**第 637 行**:`lj_record_stop(J, LJ_TRLINK_LOOP, J->cur.traceno)`。**这就是 LOOP 链接的诞生**。linktype = LOOP,目标 = 自己的 traceno。一条循环 root trace 就此定型:它跑完一圈,跳回自己。

**第 638 行往后**:处理内层循环(root trace 遇到嵌套循环)。这种情况通常报错 `LJ_TRERR_LINNER`(让内层循环自己 spawn 一条 side trace),或者有限地展开(unroll)。这是边角情况,不影响主流程。

注意一个细节:`lj_record_stop` 的第二个参数传 `J->cur.traceno`——trace 此时还没正式分配最终 traceno(在 `trace_start` 里分配),但 `J->cur.traceno` 已经在 `trace_start` 设好了,所以这里能用。这是 LOOP 链接"回自己"的实现基础:link 字段存自己的 traceno。

### §13 录制侧的链接决策:链到别的 trace(ROOT)

除了回自己,trace 还能链到别的已编译 trace。这发生在录制器遇到一个**已编译的循环回边**或**已编译的函数入口**时。看 `rec_loop_jit`(`lj_record.c:659`):

```c
659	/* Handle the case when an already compiled loop op is hit. */
660	static void rec_loop_jit(jit_State *J, TraceNo lnk, LoopEvent ev)
661	{
662	  if (J->parent == 0 && J->exitno == 0) {  /* Root trace hit an inner loop. */
663	    /* Better let the inner loop spawn a side trace back here. */
664	    lj_trace_err(J, LJ_TRERR_LINNER);
665	  } else if (ev != LOOPEV_LEAVE) {  /* Side trace enters a compiled loop. */
666	    J->instunroll = 0;  /* Cannot continue across a compiled loop op. */
667	    if (J->pc == J->startpc && J->framedepth + J->retdepth == 0)
668	      lj_record_stop(J, LJ_TRLINK_LOOP, J->cur.traceno);  /* Form extra loop. */
669	    else
670	      lj_record_stop(J, LJ_TRLINK_ROOT, lnk);  /* Link to the loop. */
671	    ...
676	  }  /* Side trace continues across a loop that's left or not entered. */
677	}
```

这是 side trace(因为 root trace 遇到已编译循环直接报错 `LJ_TRERR_LINNER`)。side trace 录到一个已编译的循环回边:

**第 667–668 行**:如果正好回到自己的起点(`J->pc == J->startpc`),形成 LOOP(自己闭环)。

**第 670 行**:否则,`lj_record_stop(J, LJ_TRLINK_ROOT, lnk)`——**链到那个已编译的循环 trace**(lnk 是它的 traceno)。这就是 ROOT 链接:side trace 跑完,跳到另一条 root trace 的 mcode,接着跑。

函数调用也有类似的:`rec_func_jit`(`lj_record.c:1949`):

```c
1949	/* Record entry to an already compiled function. */
1950	static void rec_func_jit(jit_State *J, TraceNo lnk)
1951	{
...
1963	  J->instunroll = 0;  /* Cannot continue across a compiled function. */
1964	  if (J->pc == J->startpc && J->framedepth + J->retdepth == 0)
1965	    lj_record_stop(J, LJ_TRLINK_TAILREC, J->cur.traceno);  /* Extra tail-rec. */
1966	  else
1967	    lj_record_stop(J, LJ_TRLINK_ROOT, lnk);  /* Link to the function. */
1968	}
```

完全对称的结构:回到自己起点就 TAILREC(尾递归归约),否则 ROOT 链到那个已编译函数的 trace。

这两个函数印证了 §9 的分类:ROOT 链接用于"接上别的 trace",TAILREC 用于"尾递归归约"。它们都是录制器在遇到已编译目标时的链接决策。

### §14 录制侧的链接决策:退解释器与缝合

退解释器的链接决策分散在多处。最典型的是 `rec_ret`(`lj_record.c:952`)的函数返回:

```c
952	    lj_record_stop(J, LJ_TRLINK_RETURN, 0);  /* Return to interpreter. */
```

函数返回时,如果返回点接不上已编译的 trace,就 RETURN 链接退解释器。`link = 0`(没有目标 trace)。

通用的 fallback 在 `lj_record_setup` 的 sidecheck 里(P5-18 §12 讲过):

```c
2891	      lj_record_stop(J, LJ_TRLINK_INTERP, 0);
```

side trace 超限或试太多次,放弃录制,INTERP 退解释器。

缝合(STITCH)的录制在 `lj_ffrecord.c:101`:

```c
101	static TValue *rec_stop_stitch_cp(lua_State *L, lua_CFunction dummy, void *ud)
102	{
103	  jit_State *J = (jit_State *)ud;
104	  lj_record_stop(J, LJ_TRLINK_STITCH, 0);
105	  UNUSED(L); UNUSED(dummy);
106	  return NULL;
107	}
```

这是个 continuation 函数(`_cp` 后缀),通过 `lj_vm_cpcall` 调用。它结束当前录制,设 STITCH 链接。触发它的 `recff_stitch`(`lj_ffrecord.c:110`)负责在 Lua 栈上插入 continuation frame:

```c
109	/* Trace stitching: add continuation below frame to start a new trace. */
110	static void recff_stitch(jit_State *J)
111	{
112	  ASMFunction cont = lj_cont_stitch;
113	  lua_State *L = J->L;
114	  TValue *base = L->base;
115	  BCReg nslot = J->maxslot + 1 + LJ_FR2;
116	  TValue *nframe = base + 1 + LJ_FR2;
117	  const BCIns *pc = frame_pc(base-1);
118	  TValue *pframe = frame_prevl(base-1);
119	  int errcode;
120
121	  /* Move func + args up in Lua stack and insert continuation. */
122	  memmove(&base[1], &base[-1-LJ_FR2], sizeof(TValue)*nslot);
123	  setframe_ftsz(nframe, ((char *)nframe - (char *)pframe) + FRAME_CONT);
124	  setcont(base-LJ_FR2, cont);
```

逐行看这个缝合设置:

**第 112 行**:`cont = lj_cont_stitch`。这是缝合的续体函数(`lj_vm.h:114`,手写汇编)。它的工作是:解释器执行完那一小段后,被调用,把控制权交回 trace 机器码。

**第 114–118 行**:取出当前 Lua 栈状态。`base` 是当前栈基址,`nslot` 是参数个数,`pc` 是调用者字节码位置,`pframe` 是上一帧。

**第 122 行**:`memmove` 把函数和参数在栈上往上挪一位,腾出位置插续体帧。这是为了在解释器栈里插一个 FRAME_CONT(continuation frame)。

**第 123–124 行**:设新帧为 `FRAME_CONT` 类型,并在帧头放上 `cont`(即 `lj_cont_stitch`)。这样解释器执行完那一小段,遇到这个 FRAME_CONT,就会调用 `lj_cont_stitch`,后者跳回 trace 机器码。

这就是 STITCH 的录制侧:它不是简单设个 linktype 就完事,还要在解释器栈里埋一个续体,让"切出去跑一小段再回来"成为可能。这是 9 种链接里机制最复杂的一种。

### §15 trace_stop:按起点字节码分流安装

录制结束、机器码生成完,就到 `trace_stop`(`lj_trace.c:499`)安装 trace。trace_stop 按 trace 的**起始字节码**(`J->cur.startins`)分流,不同起点对应不同的安装方式。先看完整的 switch:

```c
499	static void trace_stop(jit_State *J)
500	{
501	  BCIns *pc = mref(J->cur.startpc, BCIns);
502	  BCOp op = bc_op(J->cur.startins);
503	  GCproto *pt = &gcref(J->cur.startpt)->pt;
504	  TraceNo traceno = J->cur.traceno;
505	  GCtrace *T = J->curfinal;
506
507	  switch (op) {
508	  case BC_FORL:
509	    setbc_op(pc+bc_j(J->cur.startins), BC_JFORI);  /* Patch FORI, too. */
510	    /* fallthrough */
511	  case BC_LOOP:
512	  case BC_ITERL:
513	  case BC_FUNCF:
514	    /* Patch bytecode of starting instruction in root trace. */
515	    setbc_op(pc, (int)op+(int)BC_JLOOP-(int)BC_LOOP);
516	    setbc_d(pc, traceno);
517	  addroot:
518	    /* Add to root trace chain in prototype. */
519	    J->cur.nextroot = pt->trace;
520	    pt->trace = (TraceNo1)traceno;
521	    break;
522	  case BC_ITERN:
523	  case BC_RET:
524	  case BC_RET0:
525	  case BC_RET1:
526	    *pc = BCINS_AD(BC_JLOOP, J->cur.snap[0].nslots, traceno);
527	    goto addroot;
528	  case BC_JMP:
529	    /* Patch exit branch in parent to side trace entry. */
530	    lj_assertJ(J->parent != 0 && J->cur.root != 0, "not a side trace");
531	    lj_asm_patchexit(J, traceref(J, J->parent), J->exitno, J->cur.mcode);
532	    /* Avoid compiling a side trace twice (stack resizing uses parent exit). */
533	    {
534	      SnapShot *snap = &traceref(J, J->parent)->snap[J->exitno];
535	      snap->count = SNAPCOUNT_DONE;
536	      if (J->cur.topslot > snap->topslot) snap->topslot = J->cur.topslot;
537	    }
538	    /* Add to side trace chain in root trace. */
539	    {
540	      GCtrace *root = traceref(J, J->cur.root);
541	      root->nchild++;
542	      J->cur.nextside = root->nextside;
543	      root->nextside = (TraceNo1)traceno;
544	    }
545	    break;
546	  case BC_CALLM:
547	  case BC_CALL:
548	  case BC_ITERC:
549	    /* Trace stitching: patch link of previous trace. */
550	    traceref(J, J->exitno)->link = traceno;
551	    break;
552	  default:
553	    lj_assertJ(0, "bad stop bytecode %d", op);
554	    break;
555	  }
556
557	  /* Commit new mcode only after all patching is done. */
558	  lj_mcode_commit(J, J->cur.mcode);
559	  J->postproc = LJ_POST_NONE;
560	  trace_save(J, T);
...
567	}
```

这个 switch 把 trace 按起点分成四类安装方式。注意它**不是按 linktype 分**,而是按**起始字节码**分——因为安装方式取决于"trace 从哪起步",这决定了它怎么被触发、怎么接入已有的 trace 网。逐个看:

**root trace(BC_FORL/BC_LOOP/BC_ITERL/BC_FUNCF,511–520 行)**:循环类 root trace。安装方式是**改字节码**:把循环回边指令(BC_FORL/BC_LOOP/BC_ITERL)原地改成 `BC_JLOOP`(515 行,`op + (BC_JLOOP - BC_LOOP)` 算出 J 对应的 opcode),并在 d 字段存 traceno(516 行)。这样以后解释器跑到这条回边,看到是 BC_JLOOP,就直接跳进 trace 的 mcode。同时挂到 prototype 的 `pt->trace` 链(519–520)。BC_FORL 特殊一点,还要 patch 前面的 FORI 为 JFORI(509 行),让循环入口也走 JIT 路径。

这是 root trace 进入 trace 网的方式:**字节码被改写**,解释器 dispatch 时自然跳进 mcode。

**返回类(BC_ITERN/BC_RET/BC_RET0/BC_RET1,522–527 行)**:这些起点的 trace 也改字节码为 BC_JLOOP,挂到 prototype 链。和循环类不同的是,它们用的 BCINS_AD 直接构造,因为返回类字节码的字段布局和循环类略有不同(`J->cur.snap[0].nslots` 作为 a 字段)。

**side trace(BC_JMP,528–545 行)**:P5-18 §17 详细讲过。side trace 不改字节码(它从退出点起步,不经过字节码),而是**热补丁父 trace 的机器码**:`lj_asm_patchexit`(531 行)把父 trace 第 exitno 个 guard 的跳转目标改成 side trace 入口。同时把父 snapshot 的 count 置 SNAPCOUNT_DONE(535 行),把 side 挂到 root 的 nextside 链(541–543)。

**STITCH(BC_CALL/CALLM/ITERC,546–551 行)**:这是缝合 trace 的安装。关键就一行(550):`traceref(J, J->exitno)->link = traceno`。它**改前一条 trace 的 link 字段**,让前一条 trace 跑到缝合出口时,跳到这条新 trace。注意这里 `J->exitno` 不是退出号,而是被复用为"前一条 trace 的 traceno"(STITCH 场景下语义复用,见录制侧 `recff_stitch` 的设置)。这是 STITCH 进入 trace 网的方式:**动态改前一条 trace 的 link**,把缝合接上。

**第 557–560 行**:所有 patching 做完,才 `lj_mcode_commit` 提交新 trace 的机器码(设保护、登记),然后 `trace_save` 把 trace 存进全局 trace 表。注释"Commit new mcode only after all patching is done"强调顺序:先 patch 完所有相关的地方(字节码/父机器码/前一条 trace 的 link),再提交,保证一致性。

合起来,trace_stop 的逻辑是:**根据 trace 从哪起步,选择合适的"接入 trace 网"的方式**。root trace 改字节码,side trace 热补丁父机器码,STITCH 改前一条 trace 的 link。三种方式各自让 trace 接入已有的跳转图,使得程序运行时能在 trace 之间跳转。

### §16 后端:asm_tail_link(架构无关前导)

trace 的链接信息(linktype/link)设好后,由后端代码生成读这些信息,生成结尾跳转机器码。这里有个重要的结构分工:

- **架构无关的链接前导**:`asm_tail_link`,在 `lj_asm.c:2131`。
- **架构相关的跳转生成**:`asm_tail_fixup`/`asm_tail_prep`,在各后端(`lj_asm_x86.h`/`lj_asm_arm.h` 等)。

先看架构无关的 `asm_tail_link`(`lj_asm.c:2131`):

```c
2131	static void asm_tail_link(ASMState *as)
2132	{
2133	  SnapNo snapno = as->T->nsnap-1;  /* Last snapshot. */
2134	  SnapShot *snap = &as->T->snap[snapno];
2135	  int gotframe = 0;
2136	  BCReg baseslot = asm_baseslot(as, snap, &gotframe);
2137
2138	  as->topslot = snap->topslot;
2139	  checkmclim(as);
2140	  ra_allocref(as, REF_BASE, RID2RSET(RID_BASE));
2141
2142	  if (as->T->link == 0) {
2143	    /* Setup fixed registers for exit to interpreter. */
2144	    const BCIns *pc = snap_pc(&as->T->snapmap[snap->mapofs + snap->nent]);
2145	    int32_t mres;
2146	    if (bc_op(*pc) == BC_JLOOP) {  /* NYI: find a better way to do this. */
2147	      BCIns *retpc = &traceref(as->J, bc_d(*pc))->startins;
2148	      if (bc_isret(bc_op(*retpc)))
2149		pc = retpc;
2150	    }
2151	#if LJ_GC64
2152	    emit_loadu64(as, RID_LPC, u64ptr(pc));
2153	#else
2154	    ra_allockreg(as, i32ptr(J2GG(as->J)->dispatch), RID_DISPATCH);
2155	    ra_allockreg(as, i32ptr(pc), RID_LPC);
2156	#endif
2157	    mres = (int32_t)(snap->nslots - baseslot - LJ_FR2);
2158	    switch (bc_op(*pc)) {
2159	    case BC_CALLM: case BC_CALLMT:
2160	      mres -= (int32_t)(1 + LJ_FR2 + bc_a(*pc) + bc_c(*pc)); break;
2161	    case BC_RETM: mres -= (int32_t)(bc_a(*pc) + bc_d(*pc)); break;
2162	    case BC_TSETM: mres -= (int32_t)(int32_t)bc_a(*pc); break;
2163	    default: if (bc_op(*pc) < BC_FUNCF) mres = 0; break;
2164	    }
2165	    ra_allockreg(as, mres, RID_RET);  /* Return MULTRES or 0. */
2166	  } else if (baseslot) {
2167	    /* Save modified BASE for linking to trace with higher start frame. */
2168	    emit_setgl(as, RID_BASE, jit_base);
2169  }
2170	  emit_addptr(as, RID_BASE, 8*(int32_t)baseslot);
2171
2172	  if (as->J->ktrace) {  /* Patch ktrace slot with the final GCtrace pointer. */
2173	    setgcref(IR(as->J->ktrace)[LJ_GC64].gcr, obj2gco(as->J->curfinal));
2174	    IR(as->J->ktrace)->o = IR_KGC;
2175	  }
2176
2177	  /* Sync the interpreter state with the on-trace state. */
2178	  asm_stack_restore(as, snap);
2179
2180	  /* Root traces that add frames need to check the stack at the end. */
2181	  if (!as->parent && gotframe)
2182	    asm_stack_check(as, as->topslot, NULL, as->freeset & RSET_GPR, snapno);
2183	}
```

这个函数处理的是**退解释器时的寄存器准备**(以及栈同步)。注意它只在**非循环 trace**(`!as->loopref`)时被调用——看 `lj_asm.c:2561`:

```c
2561	  if (!as->loopref)
2562	    asm_tail_link(as);
```

循环 trace 有自己的 loop fixup(§17),不走这里。`asm_tail_link` 处理的是那些"跑完要退解释器或跳别的 trace"的非循环 trace。逐段看:

**第 2142 行**:`if (as->T->link == 0)`。这是关键判断——`link == 0` 表示退解释器(linktype 是 INTERP/RETURN/或没链上)。这种情况下,要为"退解释器"准备固定的寄存器。

**第 2144 行**:从最后一张 snapshot 取出"退到哪个 PC"(`snap_pc`)。退解释器时,解释器要从这个 PC 接着跑。

**第 2146–2150 行**:一个特殊处理。如果退到的字节码是 BC_JLOOP(说明退出点是某个循环 trace 的回边),且对应 trace 的起始指令是返回类,就把 PC 调整成那个返回指令。注释"NYI: find a better way"说明这是个 workaround。

**第 2151–2156 行**:**设退解释器要的寄存器**。GC64 模式下,把 PC 载入 `RID_LPC`(Lua PC 寄存器);非 GC64 模式下,载入 `RID_DISPATCH`(dispatch 表指针)和 `RID_LPC`。这两个寄存器是解释器的"上下文寄存器"——解释器入口 `lj_vm_exit_interp` 期望 dispatch 在 RID_DISPATCH、当前 PC 在 RID_LPC。这里提前把它们设好,等结尾跳进 `lj_vm_exit_interp` 就能直接用。

**第 2157–2165 行**:算 MULTRES(多返回值数量)放进 `RID_RET`。这是解释器期望的另一个寄存器——某些字节码(BC_CALLM/RETM/TSETM)需要知道"前面操作产生了几个值",这个数放在 RID_RET。根据退到的字节码类型算 mres(默认 0 或根据 a/c/d 字段算)。

**第 2166–2169 行**:如果 `link != 0`(链到别的 trace)但 `baseslot` 非 0(本 trace 起点帧比目标高),要把修改过的 BASE 存到全局(`emit_setgl ... jit_base`),让目标 trace 能拿到正确的栈基址。

**第 2170 行**:`emit_addptr(as, RID_BASE, 8*(int32_t)baseslot)`。调整 BASE 寄存器(栈指针),加上 baseslot 偏移。退解释器或跳别的 trace 时,栈基址要对齐到目标期望的位置。

**第 2178 行**:`asm_stack_restore(as, snap)`。**同步解释器栈**。把 trace 运行时的栈状态(寄存器里的值),按最后一张 snapshot 恢复成解释器期望的样子。这是 P5-17 讲的 snapshot 恢复在汇编生成侧的体现——生成"把寄存器值写回栈槽"的机器码。

**第 2181–2182 行**:root trace 如果加了帧(`gotframe`),结尾要检查栈溢出(`asm_stack_check`)。因为加了帧意味着栈用了更多,得确保不溢出。

合起来,`asm_tail_link` 做的是"退解释器或跳别的 trace 之前,把寄存器和栈准备好"。它架构无关(逻辑都一样:设 LPC/DISPATCH/RET、调整 BASE、恢复栈),所以放在 `lj_asm.c`。但**它不生成结尾跳转指令本身**——跳转指令是架构相关的(不同架构跳转指令不同),由 `asm_tail_fixup` 生成。这就是分工。

(注:提示词提到"asm_tail_link 实现在 lj_asm_x86.h",但 2.1.ROLLING 源码显示 `asm_tail_link` 实际在 `lj_asm.c:2131`,是架构无关的。真正在 `lj_asm_x86.h` 的是 `asm_tail_fixup`(2942)和 `asm_tail_prep`(2974),它们生成架构相关的跳转指令。这是源码与某些资料印象的差异,以源码为准。)

### §17 后端:asm_tail_fixup 与 asm_tail_prep(x86,真正的跳转生成)

`asm_tail_link` 准备好寄存器和栈,接下来 `asm_tail_fixup`(`lj_asm_x86.h:2942`)生成真正的结尾跳转指令。这是链接在机器码层的最终落实:

```c
2939	/* -- Tail of trace ------------------------------------------------------- */
2940
2941	/* Fixup the tail code. */
2942	static void asm_tail_fixup(ASMState *as, TraceNo lnk)
2943	{
2944	  /* Note: don't use as->mcp swap + emit_*: emit_op overwrites more bytes. */
2945	  MCode *mcp = as->mctail;
2946	  MCode *target;
2947	  int32_t spadj = as->T->spadjust;
2948	  if (spadj) {  /* Emit stack adjustment. */
2949	    if (LJ_64) *mcp++ = 0x48;
2950	    if (checki8(spadj)) {
2951	      *mcp++ = XI_ARITHi8;
2952	      *mcp++ = MODRM(XM_REG, XOg_ADD, RID_ESP);
2953	      *mcp++ = (MCode)spadj;
2954	    } else {
2955	      *mcp++ = XI_ARITHi;
2956	      *mcp++ = MODRM(XM_REG, XOg_ADD, RID_ESP);
2957	      *(int32_t *)mcp = spadj; mcp += 4;
2958	    }
2959	  }
2960	  /* Emit exit branch. */
2961	  target = lnk ? traceref(as->J, lnk)->mcode : (MCode *)(void *)lj_vm_exit_interp;
2962	  if (lnk || jmprel_ok(mcp + 5, target)) {  /* Direct jump. */
2963	    *mcp++ = XI_JMP; mcp += 4;
2964	    *(int32_t *)(mcp-4) = jmprel(as->J, mcp, target);
2965	  } else {  /* RIP-relative indirect jump. */
2966	    *mcp++ = XI_GROUP5; *mcp++ = XM_OFS0 + (XOg_JMP<<3) + RID_EBP; mcp += 4;
2967	    *((int32_t *)(mcp-4)) = (int32_t)(as->J->exitstubgroup[0] - 16 - mcp);
2968	  }
2969	  /* Drop unused mcode tail. Fill with NOPs to make the prefetcher happy. */
2970	  while (as->mctop > mcp) *--as->mctop = XI_NOP;
2971	}
```

这个函数是**9 种链接在机器码层的最终收敛点**。不管 linktype 是 LOOP/ROOT/INTERP/RETURN 还是别的,只要不是循环 trace(循环走 loop fixup),结尾跳转都由这里生成。逐行读:

**第 2945 行**:`mcp = as->mctail`。`mctail` 是机器码尾部预留的位置(`asm_tail_prep` 留好的空间,见下文)。结尾代码从这里开始往前填(x86 后端是从后往前生成机器码的)。

**第 2947–2959 行**:栈指针调整。如果 trace 运行时改过栈指针(`spadjust` 非 0),结尾要调回来——`add esp, spadj`(或 `add rsp, spadj` 在 64 位)。因为退解释器或跳别的 trace 时,栈指针要恢复成它们期望的值。注释里 `checki8` 判断能不能用短的 imm8 形式(省字节),否则用长的 imm32。

**第 2961 行**:**这是链接的核心**。`target = lnk ? traceref(as->J, lnk)->mcode : (MCode *)(void *)lj_vm_exit_interp`。一句话决定了 trace 跑完跳哪:

- 如果 `lnk` 非 0(有链接目标),target = 目标 trace 的 mcode 起点。这是 LOOP(目标是自己)、ROOT(目标是别的 root trace)、以及 side-to-root 的情况。注意 LOOP 的结尾其实主要走 `asm_loop_fixup`(§18),但这里也是兼容路径。
- 如果 `lnk == 0`(退解释器,INTERP/RETURN),target = `lj_vm_exit_interp`。这是解释器的入口函数(汇编,`lj_vm.h:64`),跳进去就从 trace 退出点接着跑解释器。

这一行把 9 种 linktype 收敛成两类机器码目标:**有 link 就跳 trace mcode,没 link 就跳解释器入口**。前面 §6 讲的四大类去向(回自己/找别人/退解释器/缝合),在机器码层被这一行精炼成"跳 trace 还是跳解释器"。STITCH 稍特殊(它的"缝合"语义在录制侧用 continuation 实现,但结尾机器码也是跳某个目标)。

**第 2962–2968 行**:生成跳转指令。两种情况:

- **直接跳转**(2963–2964):`XI_JMP`(0xE9,5 字节无条件跳转)+ 4 字节相对偏移。`jmprel(as->J, mcp, target)` 算出从当前位置到 target 的相对偏移。这是常见情况——target 在 ±2GB 范围内(32 位偏移能表达)。
- **RIP 相对间接跳转**(2966–2967):当直接跳转够不着(target 太远,`jmprel_ok` 返回 false)时,用间接跳转。把 target 地址存在一个固定的"跳板表"里(`as->J->exitstubgroup[0] - 16`),用 RIP 相对寻址取出来再跳。这是处理"trace 之间距离超过 32 位偏移"的情况——在地址空间稀疏时(比如 64 位且 mcode 区域分散),需要这种间接跳转。

**第 2970 行**:把没用到的尾部空间填 NOP。注释"make the prefetcher happy"——CPU 预取指令时,连续的 NOP 比垃圾字节友好(不会预取到错误路径)。这是性能微优化。

现在看 `asm_tail_prep`(`lj_asm_x86.h:2974`),它在生成机器码**之前**预留尾部空间:

```c
2973	/* Prepare tail of code. */
2974	static void asm_tail_prep(ASMState *as, TraceNo lnk)
2975	{
2976	  MCode *p = as->mctop;
2977	  /* Realign and leave room for backwards loop branch or exit branch. */
2978	  if (as->realign) {
2979	    int i = ((int)(intptr_t)as->realign) & 15;
2980	    /* Fill unused mcode tail with NOPs to make the prefetcher happy. */
2981	    while (i-- > 0)
2982	      *--p = XI_NOP;
2983	    as->mctop = p;
2984	    p -= (as->loopinv ? 5 : 2);  /* Space for short/near jmp. */
2985	  } else {
2986	    p -= (LJ_64 && !lnk) ? 6 : 5;  /* Space for exit branch. */
2987	  }
2988	  if (as->loopref) {
2989	    as->invmcp = as->mcp = p;
2990	  } else {
2991	    /* Leave room for ESP adjustment: add esp, imm */
2992	    p -= LJ_64 ? 7 : 6;
2993	    as->mcp = p;
2994	    as->invmcp = NULL;
2995	  }
2996	  as->mctail = p;
2997	}
```

这个函数在机器码区域**顶部**(mctop)预留空间,给结尾的跳转指令和栈调整用。逐行:

**第 2978–2984 行**:如果需要循环对齐(`as->realign` 非 0),先填 NOP 对齐到 16 字节边界(让循环开头地址对齐,提升指令预取效率),然后留 5 字节(长跳)或 2 字节(短跳)给循环回跳。这是循环 trace 的特殊处理。

**第 2985–2987 行**:非对齐情况,留 5 或 6 字节给退出分支。注意 `LJ_64 && !lnk` 时留 6 字节——64 位且退解释器时,可能需要 REX 前缀(0x48)多一个字节。

**第 2988–2995 行**:如果是循环(`as->loopref`),设 `invmcp = mcp = p`(循环回跳的位置);否则(非循环),再多留 6–7 字节给 ESP 调整指令(`add esp, imm`),设 `invmcp = NULL`(没有循环回跳)。

**第 2996 行**:`as->mctail = p`。记住尾部位置,后面 `asm_tail_fixup` 从这里填结尾代码。

`asm_tail_prep` 和 `asm_tail_fixup` 配合:prep 先预留空间,fixup 后填指令。这是 x86 后端"从后往前生成机器码"的标准模式(因为不知道前面会生成多少指令,所以先在尾部留够,再往前填)。

合起来,非循环 trace 的结尾机器码生成是:`asm_tail_link`(准备寄存器/栈)→ `asm_tail_prep`(留空间)→ ... 中间各种 IR 生成 ... → `asm_tail_fixup`(填跳转指令,目标由 link 决定)。最终 trace 机器码尾部有一条跳转,跳到 link 指向的 trace mcode 或 `lj_vm_exit_interp`。

### §18 循环 trace 的特殊处理:asm_loop_fixup

循环 trace(LOOP 链接)的结尾不是 `asm_tail_fixup` 生成的普通跳转,而是 `asm_loop_fixup`(`lj_asm_x86.h:2851`)生成的**回跳到 mcloop**。先看 mcloop 怎么定。

`asm_loop`(`lj_asm.c:1686`)处理 IR 里的 `IR_LOOP` 指令(标志循环体起点):

```c
1686	static void asm_loop(ASMState *as)
1687	{
1688	  /* HINT: muinf = unrolled loop, realign = curtailed loop. */
...
1701	  as->mcloop = as->mcp;  /* Mark start of loop body in machine code. */
...
1703	  if (!as->realign) RA_DBG_FLUSH();
...
```

第 1701 行:`as->mcloop = as->mcp`。**记录循环体在机器码里的起点位置**。这个 `mcloop` 最后存进 `GCtrace.mcloop`(`lj_asm.c:2628`):

```c
2628	  T->mcloop = as->mcloop ? (MSize)((char *)as->mcloop - (char *)as->mcp) : 0;
```

存的是偏移量(相对 mcode 起点)。这样别的 trace 链到这条循环 trace 时,可以选择跳到 mcode 开头(从头跑)或跳到 mcloop(从循环体跑)。side trace 链回 root 循环 trace 时,通常跳 mcloop——直接进循环体,跳过 trace 的 prolog。

现在看 `asm_loop_fixup`(`lj_asm_x86.h:2851`),它生成循环的回跳:

```c
2850	/* Fixup the loop branch. */
2851	static void asm_loop_fixup(ASMState *as)
2852	{
2853	  MCode *p = as->mctop;
2854	  MCode *target = as->mcp;
2855	  if (as->realign) {  /* Realigned loops use short jumps. */
2856	    as->realign = NULL;  /* Stop another retry. */
2857	    lj_assertA(((intptr_t)target & 15) == 0, "loop realign failed");
2858	    if (as->loopinv) {  /* Inverted loop branch? */
2859	      p -= 5;
2860	      p[0] = XI_JMP;
2861	      lj_assertA(target - p >= -128, "loop realign failed");
2862	      p[-1] = (MCode)(target - p);  /* Patch sjcc. */
2863	      if (as->loopinv == 2)
2864	        p[-3] = (MCode)(target - p + 2);  /* Patch opt. short jp. */
2865	    } else {
2866	      lj_assertA(target - p >= -128, "loop realign failed");
2867	      p[-1] = (MCode)(int8_t)(target - p);  /* Patch short jmp. */
2868	      p[-2] = XI_JMPs;
2869	    }
2870	  } else {
2871	    MCode *newloop;
2872	    p[-5] = XI_JMP;
2873	    if (as->loopinv) {  /* Inverted loop branch? */
2874	      /* asm_guardcc already inverted the jcc and patched the jmp. */
2875	      p -= 5;
2876	      newloop = target+4;
2877	      *(int32_t *)(p-4) = (int32_t)(target - p);  /* Patch jcc. */
2878	      if (as->loopinv == 2) {
2879	        *(int32_t *)(p-10) = (int32_t)(target - p + 6);  /* Patch opt. jp. */
2880	        newloop = target+8;
2881	      }
2882	    } else {  /* Otherwise just patch jmp. */
2883	      *(int32_t *)(p-4) = (int32_t)(target - p);
2884	      newloop = target+3;
2885	    }
2886	    /* Realign small loops and shorten the loop branch. */
2887	    if (newloop >= p - 128) {
2888	      as->realign = newloop;  /* Force a retry and remember alignment. */
2889	      as->curins = as->stopins;  /* Abort asm_trace now. */
2890	      as->T->nins = as->orignins;  /* Remove any added renames. */
2891	    }
2892	  }
2893	}
```

这个函数生成的是**循环 trace 机器码最末尾的"跳回循环开头"指令**。它处理两种情况:

**对齐的循环(2855–2869 行)**:`as->realign` 非 0,说明循环开头被对齐到了 16 字节边界(为了性能)。这种情况下用**短跳转**(2 字节,`XI_JMPs` 即 0xEB + 1 字节偏移),因为对齐后循环开头离结尾很近(在 -128 字节内)。短跳转省空间、省指令缓存。`loopinv` 表示循环分支是否反转(某些情况下循环条件被反转生成),反转时用 5 字节(`XI_JMP` + 条件跳转)。

**未对齐的循环(2870–2891 行)**:用 5 字节长跳转(`XI_JMP` 即 0xE9 + 4 字节偏移)。`*(int32_t *)(p-4) = (int32_t)(target - p)` 填入从跳转指令到 target(mcloop,循环开头)的相对偏移。target 就是 `as->mcp`(§17 讲过,循环时 `invmcp = mcp = p`,指向循环开头)。

**第 2886–2891 行**:一个优化。如果算出循环体足够小(`newloop >= p - 128`,即回跳距离在短跳范围内),强制重试并对齐(`as->realign = newloop`),下次用短跳转。这是 Mike Pall 的性能执着——小循环用短跳,既省空间又快。

注意 `asm_loop_fixup` 生成的回跳,目标 `target = as->mcp`,而这个 `as->mcp` 在循环 trace 里指向**循环体的开头**(即 mcloop 附近)。所以循环 trace 跑完一圈,这条回跳指令让它跳回循环体开头,再跑一圈。这就是 LOOP 链接在机器码层的实现:**机器码首尾用一条跳转接环**。

合起来,循环 trace 的机器码形状是:

```
[prolog: 设 vmstate, 栈检查]   <- mcode 起点
[循环体开头: mcloop]            <- asm_loop 标记, side trace 链到这里
  ... 各种计算和 guard ...
[循环体末尾]
[回跳指令: jmp mcloop]          <- asm_loop_fixup 生成, 跳回循环体开头
```

CPU 跑进 mcloop,执行循环体,到末尾跳回 mcloop,再执行循环体......无限循环,除非某个 guard 失败 side exit。这就是"trace JIT 最爽的情况"的机器码真相。

### §19 STITCH 的运行时:lj_cont_stitch 续体

§14 讲了 STITCH 的录制侧(插 continuation frame)。现在看运行时缝合怎么发生。

STITCH trace 的机器码跑到缝合点(那个没法录的 builtin 调用),它不 side exit(那会结束 trace),而是走一个特殊的退出路径——这个路径保留了"执行完那一小段后回来"的信息。具体地:

1. trace 机器码跑到缝合点,设置好续体帧(录制时埋的 FRAME_CONT + `lj_cont_stitch`),然后**退到解释器**。
2. 解释器执行那个没法 JIT 的 builtin(比如某个 string 操作),执行完,碰到续体帧 FRAME_CONT。
3. 解释器调用续体帧里存的 `lj_cont_stitch`(`lj_vm.h:114`,手写汇编)。
4. `lj_cont_stitch` 检查"是否还在 JIT 模式、是否该回到 trace",如果是,它**跳回 trace 机器码的缝合点之后**,继续跑 trace 的剩余部分。

这个机制的关键是续体帧——它在 Lua 栈里埋了一个"回来后干什么"的标记,让解释器执行完那一小段后,不是无目的地继续解释,而是被续体引导回 trace 机器码。

`lj_cont_stitch` 是手写汇编(在 `lj_vm.s` 里),它的工作简化说是:检查退出状态、恢复 trace 需要的寄存器、跳回 trace mcode 的对应位置。因为涉及具体的寄存器约定和栈布局,必须手写汇编。

STITCH 的代价是:缝合的那一小段走解释器(慢),且进出缝合有切换开销。但相比"放弃整条 trace",它让大部分代码保持机器码执行,是净赚。这是 LuaJIT 在"全录或全弃"之外找到的第三条路——**局部退让,整体保住**。

需要强调的是,STITCH 是 9 种链接里最复杂、使用场景最受限的一种。它主要用于快速函数(builtin)的缝合,普通字节码 NYI 通常直接 abort。但理解它对理解"trace JIT 如何处理部分可录代码"很重要——它展示了 trace JIT 的灵活性边界。

### §20 把链接在源码层的完整流程串起来

把本章讲的链接机制,在源码层串成一条完整的链:

```
录制阶段:
  录制器录到特定场景(循环回边/函数返回/遇到已编译 trace/STITCH 等)
    → 调 lj_record_stop(J, linktype, lnk) (lj_record.c:299)
      → 设 J->cur.linktype = linktype, J->cur.link = lnk (lj_record.c:306-307)
      → 循环修正:开 OPT_LOOP 保留窄化,否则 side trace 链回 root (lj_record.c:309-314)
      → 拍末 snapshot (lj_record.c:318)
      → lj_trace_end 推状态机到 END

优化阶段:
  LJ_TRACE_END 状态 (lj_trace.c:720)
    → 如果是循环(link==traceno 且栈层为 0):lj_opt_loop 做循环优化 (lj_trace.c:727)
      → 失败则 link=0, linktype=NONE, 继续录制 (lj_trace.c:728-729)
    → lj_opt_split, lj_opt_sink

汇编生成阶段:
  LJ_TRACE_ASM 状态 (lj_trace.c:742)
    → lj_asm_trace (lj_asm.c)
      → asm_tail_prep(as, T->link) (lj_asm.c:2553, lj_asm_x86.h:2974)
        → 在 mctop 预留结尾空间(循环留回跳位,非循环留退出分支位)
      → 如果非循环(!loopref):asm_tail_link(as) (lj_asm.c:2561→2131)
        → link==0: 设 RID_LPC/DISPATCH/RET 准备退解释器 (lj_asm.c:2142-2165)
        → asm_stack_restore 同步栈 (lj_asm.c:2178)
      → 逐条 IR 生成机器码 (从后往前)
      → 遇 IR_LOOP: asm_loop 记 mcloop (lj_asm.c:1686→1701)
      → asm_tail_fixup(as, T->link) (lj_asm.c:2632, lj_asm_x86.h:2942)
        → target = lnk ? traceref(lnk)->mcode : lj_vm_exit_interp (lj_asm_x86.h:2961)
        → 生成 jmp target (直接或间接)
      → 如果循环: asm_loop_fixup(as) (lj_asm_x86.h:2851)
        → 生成回跳 jmp mcloop (短跳或长跳)
      → T->mcloop = as->mcloop 偏移 (lj_asm.c:2628)

安装阶段:
  trace_stop(J) (lj_trace.c:499)
    → 按 startins 分流:
      → root(BC_FORL/LOOP/ITERL/FUNCF): 改字节码为 BC_JLOOP, 挂 pt->trace (511-520)
      → 返回类(BC_RET*): 改字节码为 BC_JLOOP, 挂 pt->trace (522-527)
      → side(BC_JMP): lj_asm_patchexit 热补丁父机器码 (528-545)
      → STITCH(BC_CALL/CALLM/ITERC): 改前一条 trace 的 link = traceno (546-551)
    → lj_mcode_commit 提交 (558)
    → trace_save 存进 J->trace[] (560)

运行时:
  trace 跑到结尾:
    → 循环 trace: 回跳 jmp mcloop, 跑下一圈
    → 非循环有 link: jmp 目标 trace mcode, 继续跑机器码
    → 非循环无 link: jmp lj_vm_exit_interp, 退解释器
  STITCH 缝合点: 退解释器跑一小段, lj_cont_stitch 引导回 trace
```

这条链上,链接信息从录制(`lj_record_stop`)流向汇编(`asm_tail_*`/`asm_loop_fixup`)再流向安装(`trace_stop`),最终在运行时变成具体的跳转执行。每一个环节,本章都讲了"为什么"和"怎么做"。

---

## 第三部分:为什么这样设计是 sound 的

### §21 不变式一:跳转目标永远合法

第一个保证:**trace 结尾跳转的目标,永远是合法的执行入口**。

这个保证由 `asm_tail_fixup` 的核心一行支撑(`lj_asm_x86.h:2961`):

```c
target = lnk ? traceref(as->J, lnk)->mcode : (MCode *)(void *)lj_vm_exit_interp;
```

target 只有两种取值:

- **某条 trace 的 mcode 起点**(lnk 非 0 时)。这条 trace 是已编译、已安装的(`trace_stop` 里 `trace_save` 进了 `J->trace[]`),它的 mcode 是一段合法的可执行机器码。跳进去能正确执行。
- **`lj_vm_exit_interp`**(lnk 为 0 时)。这是解释器的标准入口(`lj_vm.h:64`),一段手写汇编,负责从 trace 退出状态恢复成解释器状态并继续 dispatch。跳进去能正确退到解释器。

没有第三种可能。所以 trace 跑完跳转,要么进另一段机器码(合法),要么进解释器入口(合法),**永远不会跳到未定义内存或错误的入口**。这是链接 sound 性的最底层保证。

### §22 不变式二:跳转时状态对齐

第二个保证:**跳到目标时,寄存器和栈状态是目标期望的样子**。

这个保证分两种情况:

**退解释器(lnk==0)**:`asm_tail_link`(`lj_asm.c:2142–2178`)负责把状态摆成解释器期望的样子——设 RID_LPC(退出 PC)、RID_DISPATCH(dispatch 表)、RID_RET(MULTRES),调 BASE 到正确位置,`asm_stack_restore` 按最后一张 snapshot 把栈恢复。这些做完,跳进 `lj_vm_exit_interp`,解释器拿到的状态就和"它自己跑到这个 PC"完全一致,能正确接着跑。这正是 P5-17 讲的 snapshot 恢复在汇编生成侧的体现。

**跳别的 trace(lnk!=0)**:这种情况下,目标 trace 期望的入口状态,和本 trace 结尾的状态,要对齐。对齐靠几件事:

- **栈基址对齐**:`asm_tail_link` 的 `emit_addptr(as, RID_BASE, 8*baseslot)`(`lj_asm.c:2170`)调整 BASE,以及 `baseslot` 非 0 时存全局(`2166–2168`),保证目标 trace 拿到的 BASE 在它期望的位置。
- **目标 trace 的入口设计**:目标 trace 的 mcode 开头有自己的 prolog(设 vmstate、栈检查、从入口状态取值),它期望的入口约定是固定的(比如 BASE 在 RID_BASE、某些值在特定寄存器)。本 trace 结尾跳过去时,这些约定要满足。这在 trace 编译时由后端统一保证——所有 trace 遵循相同的入口/出口寄存器约定(比如 BASE 永远在 RID_BASE),所以 trace 之间能直接跳。

这里有个精妙点:**LOOP(回自己)的状态对齐是天然的**。循环 trace 跑完一圈回到 mcloop,这一圈里修改的寄存器(BASE、各种值)在循环体里是自洽的——循环体开头期望的状态,正好是循环体末尾留下的状态(因为它们是同一段代码的衔接)。所以 LOOP 不需要特别的状态对齐,首尾自然接上。这是循环 trace 能"无限自闭环"的基础。

### §23 不变式三:LOOP 闭环不会跑飞

第三个保证,专门针对 LOOP:**循环 trace 的首尾接环,不会因为长时间运行而跑飞**。

LOOP trace 的机器码是一个闭环:跑完一圈跳回 mcloop,再跑一圈。这个闭环里,每一圈执行相同的机器码,产生相同类型的状态变化(比如 i 加 1、x 加 i)。只要每圈的 guard 都不失败(假设成立),这个闭环可以无限跑下去,正确性不变。

为什么不会跑飞?因为:

1. **每圈的机器码完全相同**。不是"动态生成新代码",是同一段静态机器码反复执行。所以行为可预测——第 N 圈和第 1 圈执行的是字节级别相同的指令。
2. **状态变化是确定性的**。i 加 1、x 加 i,这些操作在 guard 保护下是确定性的(guard 保证 i 还是整数、x 还是整数)。所以每一圈后,状态按确定的方式变化,不会随机漂移。
3. **guard 兜底**。如果某圈某个假设破了(比如 i 溢出整数范围、或类型变了),guard 触发 side exit,退出闭环。所以闭环只在"所有假设持续成立"时才持续,一旦有变就跳出。

合起来,LOOP 闭环是一个"在 guard 保护下的确定性循环"。它要么持续正确执行(假设成立),要么 side exit 退出(假设失败)。永远不会"跑着跑着跑飞了"。这是循环 trace 能放心让 CPU 在里面转圈的根本原因。

有个细节:循环里可能有 GC step 检查(P5-18 §18 提过)。闭环里如果触发 GC 检查退出,会走 side exit 退解释器做 GC,做完再回来。这不破坏正确性,只是偶发的性能开销(保证 GC 能推进)。

### §24 不变式四:STITCH 缝合语义等价

第四个保证,针对最复杂的 STITCH:**缝合执行的代码,和全程解释器执行,语义等价**。

STITCH 把 trace 中间一段(没法录的 builtin)切给解释器跑。这一段的执行,完全是解释器的标准行为——它按真实类型、真实语义执行那个 builtin,结果和"全程解释器跑到这里"完全一致。执行完,续体 `lj_cont_stitch` 把控制权交回 trace 机器码。

这里要论证的是:trace 机器码在缝合点之前和之后的部分,它们的状态衔接是否正确?

- **缝合点之前**:trace 机器码跑,状态(寄存器/栈)在变化。到缝合点时,状态需要交给解释器。这一步靠 snapshot——缝合点也有一张 snapshot,记录"此刻状态怎么恢复成解释器的样子"。退到解释器时按这张 snapshot 恢复,解释器拿到正确状态。
- **缝合点之后**:解释器执行完那段 builtin,结果在某些栈槽/寄存器里。续体引导回 trace 机器码时,trace 期望的入口状态(缝合点之后的寄存器约定)要和解释器留下的状态对齐。这一步靠续体帧记录的信息和 `lj_cont_stitch` 的寄存器恢复逻辑。

只要这两步对齐正确(由 snapshot + 续体设计保证),缝合就语义等价:trace 机器码段 + 解释器段 + trace 机器码段,串联起来,和"全程解释器"产生相同结果。

STITCH 的 sound 性,本质上还是 P0-01 §9 的不变式:**机器码要么和解释器一样(假设成立时),要么退回解释器(缝合点就是主动退一小段)**。缝合只是把"退回"局部化、可控化,不让一个不可录点毁掉整条 trace。

### §25 不变式五:链接动态更新的一致性

第五个保证,针对 STITCH 的动态 link 更新:**改前一条 trace 的 link 字段,不会破坏它**。

§15 讲过,STITCH trace 安装时,`trace_stop` 会改前一条 trace 的 link(`lj_trace.c:550`):`traceref(J, J->exitno)->link = traceno`。这是"改已存在 trace 的字段",听起来要小心。

为什么 sound？因为:

1. **改的是 link 字段(一个数据),不是机器码**。前一条 trace 的机器码一个字节都不动(不像 side trace 的 patchexit 改机器码)。改的只是 `GCtrace.link` 这个 uint16 字段。机器码运行时不读这个字段(它读的是生成时固化进去的跳转目标),所以改 link 不影响已经在跑的机器码。
2. **改完后,前一条 trace 新的执行会用新 link**。等等——如果机器码里固化了跳转目标,改 link 字段有什么用？这里有个细节:STITCH 的链接不是"机器码里固化的跳转",而是通过运行时查 link 字段决定的。具体地,STITCH trace 的出口走的是 `lj_vm_exit_interp` 类的路径(退解释器),退到解释器后,解释器发现"这里其实该缝到新 trace",于是查 link 字段跳过去。所以改 link 字段能影响后续的缝合去向。
3. **改的时机安全**。改发生在 `trace_stop`,此时新 trace 已编译完、即将提交。前一条 trace 此刻没在跑(控制权在 C 状态机里)。改完,下次前一条 trace 跑到缝合点,看到的就是新 link。

所以 STITCH 的动态 link 更新,改的是数据(不影响机器码),改的时机安全,改的效果在下次执行时生效。这是"链接随编译进展而织密"的实现,且不破坏已存在的 trace。

### §26 五个不变式合起来

合起来,trace 链接的 sound 性就完整了:

1. **跳转目标合法**:target 只能是 trace mcode 或 `lj_vm_exit_interp`,无第三种(`asm_tail_fixup` 保证)。
2. **状态对齐**:退解释器靠 `asm_tail_link` 设寄存器+恢复栈;跳 trace 靠统一入口约定;LOOP 天然自洽(§22)。
3. **LOOP 闭环不跑飞**:静态机器码+确定性变化+guard 兜底(§23)。
4. **STITCH 语义等价**:snapshot 恢复 + 续体对齐,局部退让整体保住(§24)。
5. **动态 link 更新一致**:改数据不改机器码,时机安全(§25)。

所以结论:**trace 链接产生的控制流,和"全程解释器"语义一致**。trace 之间跳转、LOOP 自闭环、STITCH 缝合,都只是"让程序在机器码里多跑一会儿"的优化手段,不改变计算结果。这是"把动态执行安全变成机器码"里"安全"在链接这一环的延续——链接让 trace 织成网,但网里的每一条路径,都和解释器等价。

和 side trace 一样,这套保证也是**静态可推导**的:从代码结构和不变式就能论证,不需要运行时测试。这就是 LuaJIT 敢让 trace 之间直接机器码跳机器码(不经过解释器中转)却不出正确性 bug 的根源。

---

## 第四部分:★对照 + 回扣主线

### §27 ★对照一:官方 Lua(切"没有 trace 网")

官方 Lua 是纯解释器,**根本没有 trace、没有 trace 链接**。它的执行模型是:每条字节码都现场解释,一个循环跑 100 万次就解释 100 万次。解释器之间不存在"跳转"——它只有 dispatch 表分发,从一条字节码到下一条。

所以在官方 Lua 里,不存在"trace 跑完跳哪"这个问题。每条字节码执行完,自动 dispatch 到下一条(dispatch 表查找),这是解释器的固有结构,不需要额外的"链接"机制。

LuaJIT 引入 trace 后,才有了"trace 跑完跳哪"的问题——因为 trace 是一段独立的机器码,它不在解释器的 dispatch 循环里,必须有显式的去向指令。9 种 TraceLink,就是 LuaJIT 为解决这个问题设计的。这是"加上 JIT"带来的额外复杂度:不仅要编译 trace,还要把 trace 接成网,让程序在网里流转。

对照能看清:trace 链接是 trace JIT 特有的机制,纯解释器没有也不需要。它的价值在于"让程序尽量在机器码里跑",这是 JIT 相对解释器的性能优势的具体来源之一——不仅是"单条 trace 快",更是"trace 之间不用回解释器中转"。

### §28 ★对照二:JVM/V8(方法内联 vs trace 链接)

JVM 和 V8 也有 JIT,也有"让程序在机器码里多跑"的机制,但它们的方式和 trace 链接**本质不同**。这是 trace vs method 分歧在"代码衔接"这一环的体现。

**JVM/V8 的做法:方法内联 + 调用链接**。

JVM/V8 是 method JIT,以整个方法为单位编译。它们让程序在机器码里多跑的主要手段是**方法内联(inlining)**:把被调用方法的机器码,直接复制(inlined)到调用方法的机器码里。这样调用方跑完调用点,不需要跳转,直接继续执行被内联的代码——被调用方法成了调用方法的一部分。

方法内联的好处是:消除调用开销(不需要跳转、不需要保存/恢复寄存器)、且让优化跨方法进行(内联后被调用方法的代码能和调用方一起优化)。代价是机器码膨胀(每个调用点都复制一份被调用方法),所以 JVM/V8 用 inline heuristic 控制内联深度和大小。

对于没法内联的方法(太大、或虚方法动态分发),JVM/V8 用**调用链接(ic call / inline cache)**:调用点缓存上次调用的目标方法的机器码入口,下次直接跳过去(快速路径);如果目标变了(多态),回退到慢速查找。这是它们处理"方法间跳转"的方式。

**LuaJIT 的做法:trace 链接**。

LuaJIT 不内联方法(它根本不以方法为单位)。它让程序在机器码里多跑的方式是 **trace 链接**:一条 trace 跑完,直接跳到另一条 trace 的 mcode,或者 LOOP 自闭环。trace 之间是平等的"跳转"关系,不是"包含"关系。

关键区别:

| 维度 | LuaJIT(trace 链接) | JVM/V8(方法内联/调用链接) |
|---|---|---|
| 衔接单位 | trace(线性热路径)之间 | method(整个函数)之间 |
| 衔接方式 | 跳转(jmp mcode) | 内联(复制代码)/ 调用链接(缓存入口) |
| 跨"方法" | trace 本身可跨方法(录一条调用链) | 内联把方法合并;不内联则调用链接 |
| 代码膨胀 | 低(trace 短,只跳转不复制) | 高(内联复制机器码) |
| 动态分发处理 | guard 失败 side exit/长 side trace | inline cache 去优化/重新编译 |
| 循环处理 | LOOP 自闭环(trace 即一圈循环) | 方法内循环(back branch,整个方法机器码) |

最根本的差异是 **"跳转" vs "内联"**:

- **LuaJIT 用跳转**。trace A 跑完跳 trace B,两者机器码各自独立,只是首尾用 jmp 连。好处是不复制代码(省空间)、trace 短(编译快);代价是每次跳转有一点开销(虽然远小于回解释器)。
- **JVM/V8 用内联**。把 B 的代码复制进 A,A 跑到调用点直接继续执行 B 的代码(零跳转开销)。好处是无跳转开销、可跨方法优化;代价是代码膨胀(每个调用点复制)、编译慢(要分析内联收益)。

为什么 LuaJIT 选跳转?因为 trace 本身就是"跨方法的线性路径"——一条 trace 可能已经录了"函数 A 调用函数 B 调用函数 C"这条链(P2-06 讲过 trace 可跨调用边界)。所以 trace 已经天然"内联"了调用链(录的时候就把调用链展平成一条线性 IR),不需要额外的内联机制。trace 之间的链接,处理的是"trace 级别"的衔接(一个 trace 跑完接另一个),而不是"方法级别"的衔接。

这又回到 P0-01 §10 的根本分歧:**trace 的线性性,让它用跳转链接就够;method 的整体性,让它倾向内联**。两种 JIT 的代码衔接策略,是它们编译单位选择的直接后果。

还有一个对照点:**LOOP vs method 内的 back branch**。LuaJIT 的循环是一个 trace 自闭环(LOOP),机器码首尾接环,这是 trace 级的循环。JVM/V8 的循环是方法机器码内的 back branch(向后跳转指令),循环在方法机器码内部,不需要"链接"——它就是方法控制流图里的一个回边。所以 JVM/V8 不需要"LOOP 链接"这种东西,循环天然在方法内;LuaJIT 因为以 trace(一圈循环)为单位,才需要 LOOP 把 trace 首尾接起来。这是 trace vs method 在循环处理上的镜像差异。

### §29 回扣主线

把本章放回全书主线:**把动态执行安全变成机器码**。

trace 链接是"快"这一股张力的收尾。前几章把单条 trace 的生命周期讲完了:录制(P2)→ 优化(P3)→ 代码生成(P4)→ 运行时 guard/snapshot/side trace(P5-16/17/18)。但这些讲的都是"一条 trace 怎么编出来、怎么安全跑"。一条 trace 跑完去哪?这个问题不解决,trace 就是一堆孤立的机器码段,每跑完一条就得回解释器,加速效果大打折扣。

trace 链接把这个缺口补上:**让 trace 之间织成网,程序在网里流转,尽量不回解释器**。9 种 TraceLink 把"trace 跑完的去向"精确枚举:

- **LOOP/TAILREC/UPREC/DOWNREC**:回到自己(循环、尾递归、递归),机器码首尾接环,无限跑。
- **ROOT**:跳到别的 trace,trace 之间直接机器码跳机器码。
- **INTERP/RETURN**:退回解释器,接不上时的保底。
- **STITCH**:缝合,局部退让整体保住,处理部分可录代码。

这些链接在源码层的实现,合起来是一套完整的"trace 接入网"机制:

- **录制决策**(`lj_record_stop`,`lj_record.c:299`):录制器在各种场景下决定用哪种 linktype,存进 `J->cur.linktype/link`。
- **后端生成**(`asm_tail_fixup`,`lj_asm_x86.h:2942`):核心一行 `target = lnk ? traceref(lnk)->mcode : lj_vm_exit_interp`,把 9 种 linktype 收敛成"跳 trace 或跳解释器"两类机器码目标;LOOP 特殊,由 `asm_loop_fixup`(`lj_asm_x86.h:2851`)生成回跳 mcloop。
- **架构无关前导**(`asm_tail_link`,`lj_asm.c:2131`):退解释器时准备寄存器(LPC/DISPATCH/RET)和栈(snapshot 恢复)。
- **安装接入**(`trace_stop`,`lj_trace.c:499`):root trace 改字节码为 BC_JLOOP,side trace 热补丁父机器码(P5-18),STITCH 改前一条 trace 的 link。
- **mcloop 标记**(`asm_loop`/`T->mcloop`,`lj_asm.c:1701/2628`):循环体起点,LOOP 回跳目标和 side-to-root 链接的跳转目标。
- **STITCH 续体**(`lj_cont_stitch`,`lj_vm.h:114`):缝合的"切出去再回来"机制。

trace 链接把 P5 这一篇的五章串起来,也把整棵 trace 树接成了一张机器码跳转图:**guard**(P5-16,机器码里插检查)→ **snapshot**(P5-17,失败时恢复状态)→ **side trace**(P5-18,失败够多次就从退出点长新 trace)→ **trace 链接**(P5-19,trace 之间互相跳转)。五章合起来,就是 trace JIT 完整的"运行时自适应":检查、退避、再优化、互相链接,最终让程序在一张由 root + side + 各种链接织成的机器码网里高速流转,解释器只在冷代码和偶发失败时被唤醒。

而 trace 链接的设计哲学——**精确分类去向 + 跳转目标永远合法 + 状态对齐 + 局部退让(STITCH)而不整体放弃**——也是整个 LuaJIT 的哲学:不追求一次编译到位,而是让 trace 在运行中渐进地接成网,每次编译都让网更密一点,失败可回退,正确性由 guard 和解释器保底。这种"渐进、自适应、永远 sound"的风格,让 LuaJIT 在动态语言 JIT 里独树一帜——它不像 JVM/V8 那样追求"方法级的最优内联",而是"trace 级的灵活跳转",用轻量的链接代替重型的内联,用 trace 的线性性换取编译的敏捷。这就是它"越跑越快、网越织越密"的引擎,也是 trace JIT 路线在运行时收尾这一环的精髓。

trace 链接讲完,P5 篇(运行时:guard、snapshot、side trace、trace 链接)就完整了。trace JIT 的核心机制——从录制到优化到代码生成到运行时自适应——已经全部讲清。下一章我们转向 JIT 与外部的协作:[P6-20 FFI:C 类型与调用]——LuaJIT 不仅能让 Lua 跑成机器码,还能让 Lua 直接调用 C 函数(甚至 C 数据结构),这也是 JIT 编译的。FFI 是 LuaJIT 的招牌特性之一,它把"JIT 能调 C"也纳入了"安全变成机器码"的版图。

---

*上一章 [P5-18 side trace:从退出点录制新 trace](P5-18-side-trace从退出点录制新trace.md):失败够多次就从退出点长新 trace,这是 trace 树生长的机制。本章 [P5-19 trace 链接] 把整棵 trace 树接成机器码跳转网。下一章 [P6-20 FFI:C 类型与调用](P6-20-FFI-C类型与调用.md):JIT 如何安全地调用 C 函数。*
