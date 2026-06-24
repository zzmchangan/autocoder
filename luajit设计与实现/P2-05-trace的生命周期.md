# P2-05 trace 的生命周期

> **本书主线**:把动态执行安全变成机器码。**二分法**:解释器侧 ↔ JIT 侧。本章是 **JIT 侧的总纲**——一个 trace 从热点触发,到录制、优化、生成机器码、安装、运行、退出、链接,直到最终销毁的完整一生。**★对照**:官方 Lua + JVM/V8(method 状态机对照 trace 状态机)。**源码**:LuaJIT 2.1.ROLLING。**基调**:纯直球,不用比喻;从第一性原理一步步推导。

---

## 引子:从 P0-01 的七阶段旅程,到源码

P0-01 §12 用一个 `for i = 1, 1000000 do x = x + i end` 循环,把 JIT 的完整一生拆成了七个阶段:

```
解释器先跑 → 热点被发现 → 录制 trace → 优化 IR → 生成机器码 → 安装接管 → 失败则退回
```

那是一个**概念图**,讲清了"会发生什么"。但它留下了一个更硬的问题没回答:**这七个阶段,在源码里是怎么被串起来的?谁负责让流程从一个阶段走到下一个阶段?中间出错了怎么办?机器码跑起来之后,谁在管它?它最后是怎么死的?**

这一章就来回答这些问题。它的定位很明确——**JIT 侧的总纲**。这一章把七阶段旅程铺成一条**状态机**,然后逐行指给你看:每个状态切换在哪一行源码、调了哪个模块。讲完这一章,你就拿到了后面 P2-06 到 P5 全部章节的地图:

- P2-06(`lj_record`)是这里 RECORD 状态的深入;
- P2-07(IR/SSA)是 RECORD 产出物的深入;
- P3 全篇(fold/mem/sink/loop)是 OPT 状态的深入;
- P4 全篇(线性扫描、汇编生成)是 ASM 状态的深入;
- P5 全篇(guard/snapshot/side trace/链接)是"运行 + 退出"阶段的深入。

所以这一章不钻任何一个模块的细节(那是后面十几章的事),它只做一件事:**把这条生命周期,连同它的状态机、它的管理者、它存储的对象、它的失败处理和它的死亡,完整地、有源码佐证地讲清楚。**

---

## §1 第一性原理:一个热点,要变成机器码,得经过哪些步骤

在贴源码之前,我们先纯靠第一性原理,把"一条热点路径变成可运行机器码"这件事**必须**经历的步骤推导出来。这是本章最重要的一节——它解释了为什么 LuaJIT(以及任何 trace JIT)的状态机**长成这个样子**。

### 1.1 为什么不能一步到位

设想一个最朴素的方案:解释器发现某条回边变热了,**立刻**当场把它编译成机器码,装上去。这行不行?

不行。让我们一步一步地撞墙。

**第一堵墙:编译需要时间,而程序还在跑。**

编译(把字节码翻成 IR、做优化、做寄存器分配、吐机器码)是一个有相当工作量的过程。一段热点 trace 的编译可能要几百微秒到几毫秒。而解释器是在执行用户程序的——用户的循环此刻正等着下一次迭代的结果。如果解释器在发现热点的瞬间就地开始编译,那这次循环迭代就会被冻结在编译里,用户感受到一次明显的卡顿。

更要命的是:编译需要**完整的**信息。你要编译一条 trace,得先把这条路径从头到尾录下来——光看到回边那一条字节码不够,你得知道"从循环入口到回边,这条路径上依次执行了哪些字节码、每一步的类型是什么、哪里有分支需要插 guard"。这些信息,**只有让解释器实际从头到尾再跑一遍这条路径,边跑边记**,才能拿到。

所以编译不可能"在发现热点的那一瞬间就地完成"。它必须被**拆成两步**:先把热点路径**录制**下来(边执行边记,得到完整的 IR),再在录完之后**离线**做优化和代码生成(这时候解释器可以暂停一下,或者编译在另一个上下文里完成)。

这就推出了第一个状态:**RECORD(录制)**。它的本质是"解释器一边正常执行,一边把执行的字节码翻译成 IR,追加到正在构建的 trace 里"。录制结束,才有一份完整的 IR 可以拿去编译。

**第二堵墙:录制完的 IR,不能直接变机器码。**

录下来的 IR 是"诚实但啰嗦"的——它忠实地记录了每一次取、每一次算、每一次存,包括很多冗余。比如 `x = x + 0`,IR 里会老老实实地有"加 0"这条指令;但实际上加 0 可以消掉。又比如循环不变量,循环体里某次计算只依赖循环外的不变量,那它每次循环算出同样的结果,完全可以提到循环外算一次。

如果不做这些优化就直接生成机器码,机器码会又长又慢,白瞎了 JIT 的意义。所以录制完之后,**必须有一个优化阶段**,在 IR 上做常量折叠、别名分析、死代码消除等等,把 IR 收紧。

这就推出了第二个状态:**OPT(优化)**。它在前一个状态产出的 IR 上跑若干个 pass。

**第三堵墙:优化后的 IR,还得翻译成机器码。**

IR 是一种与具体 CPU 无关的中间表示(后面 P2-07 会讲它是 SSA 形式)。但 CPU 只认机器码。所以优化完,还差最后一步:把优化后的 IR,**为具体的目标架构**(x86-64 / ARM64 / ...)**分配寄存器**、**生成汇编**、**写入一块可执行内存**。

这又是一个独立的状态:**ASM(汇编生成 / 代码生成)**。它依赖优化后的 IR 作为输入,产出可执行的机器码。

**第四堵墙:机器码生成了,但解释器还不知道要跳进来。**

机器码躺在内存里,本身不会被执行。要让它真正接管热点,还差一步**安装**:把解释器里那条"被认定为热点"的字节码**改写**(patch)一下——改成"遇到这条字节码,别解释了,直接跳到这段机器码去"。这一步在 LuaJIT 里叫**字节码打补丁**,把普通循环字节码 `BC_FORL` 改写成带 JIT 跳转的 `BC_JFORL` 之类。

不安装,机器码就是死的;安装了,下一次解释器跑到这里,就直接跳进机器码飞奔。这一步属于"收尾",在 LuaJIT 里它和 ASM 的尾巴合在一起(ASM 状态结束时一并安装)。

### 1.2 四个核心状态,串成一条流水线

把上面四堵墙撞完,我们其实已经推出了 trace 编译期的核心状态:

```
        发现热点
           │
           ▼
       ┌───────┐
START──►│ RECORD │  边执行边记,产出 IR
       └───┬───┘
           │ 录完
           ▼
       ┌─────┐
       │ OPT │     在 IR 上做优化 pass
       └──┬──┘
          │ 优化完
          ▼
       ┌─────┐
       │ ASM │     寄存器分配 + 生成机器码 + 安装
       └──┬──┘
          │ 装好
          ▼
       IDLE        回解释器,等下次热点
```

这条流水线有四个关键性质,每一个都值得单独点明,因为它们决定了后面源码的形状:

**性质一:顺序依赖,不能乱跳。** RECORD 的产出(IR)是 OPT 的输入;OPT 的产出(优化后的 IR)是 ASM 的输入;ASM 的产出(机器码)是安装的前提。每个阶段都必须等上一个阶段完成。这就是为什么它是一条**状态机**而不是一团并行任务——阶段之间有严格的先后数据依赖。

**性质二:每个阶段都可能失败,且失败后必须安全退回。** 录制时碰到不支持的字节码、优化时发现循环类型不稳定、生成机器码时发现可执行内存不够——任何一个阶段都可能让这条 trace 编不下去。失败时不能崩、不能算错,必须**放弃这条 trace**,让程序继续用解释器跑(解释器永远是正确的保底)。这就要求状态机里必须有一条**错误出口**:任何状态出错,都跳到一个统一的错误处理点,作废当前 trace,回 IDLE。

**性质三:整个过程对解释器是"半透明"的。** RECORD 阶段比较特殊——它**不是**纯粹离线编译,而是"让解释器再跑一遍热点路径,边跑边记"。也就是说,RECORD 期间程序其实在正常前进(循环又跑了一圈),只不过这一圈的副作用除了更新程序状态,还多了一份"IR 记录"。而 OPT/ASM 期间,解释器是停下来的(编译占着 CPU)。这个区别在源码里体现为:RECORD 是"每执行一条字节码就回调一次录制器",OPT/ASM 是"一次性跑完"。

**性质四:需要一个"管理者"贯穿全程。** 这条流水线跨好几个模块(录制器在 `lj_record.c`、优化在 `lj_opt_*.c`、生成在 `lj_asm.c`),它们之间要共享同一份正在构建的 trace 数据。所以必须有一个**中心对象**,持有当前 trace 的 IR、snapshot、机器码,持有当前状态,并且负责在状态之间做切换。这个对象就是 `jit_State`(下一节细看)。

### 1.3 再加上两个状态:启动与退出

上面四个是编译期的核心。但一条 trace 的完整生命周期,还包含**编译之前**和**编译之后**:

- **编译之前**:解释器发现热点的那一刻。这是一个"从无到有"的瞬间——之前没有 trace,现在要创建一个。这个瞬间需要一个独立的 **START(启动)** 状态:分配 trace 编号、初始化各种录制状态、把起始位置记下来(起始 pc、起始字节码),然后才进入 RECORD。

- **编译之后**:机器码装上去了,它要在程序运行期间**被执行**。执行时绝大多数情况没问题(guard 不触发,一路跑到底);但偶尔 guard 会触发(乐观假设破了),这时机器码不能继续,必须**退出**:把当前寄存器状态恢复成解释器的样子(靠 snapshot),退回解释器继续。这个"运行期退出"是生命周期的另一个环节,在源码里它走的是另一条入口(`lj_trace_exit`),和编译期状态机不在同一条调用链上。

而且,退出之后还有可能**再编译**:如果某个退出点退得太频繁(说明那条没被编译的分支其实也很热),就从那个退出点重新录一条新 trace,叫 side trace。这等于又启动了一轮 START→RECORD→OPT→ASM,只不过这次的起点不是循环回边,而是某条已存在 trace 的退出点。

把这些都加上,trace 的完整生命周期就是:

```
                 ┌──────── 编译期 (一次) ────────┐
                 │                                 │
  热点触发 ──► START ──► RECORD ──► OPT ──► ASM ──► 安装
                 │                                 │
                 └──── (失败则 ERR ──► 作废回解释器)
                                                          │
                                                          ▼
                                                   ┌── 运行期 (多次) ──┐
                                                   │                   │
                                                   机器码飞跑 ──► guard 触发?
                                                   │     否             │ 是
                                                   │     │              │
                                                   │     ▼              ▼
                                                   │   循环/链接      退出(lj_trace_exit)
                                                   │                   │ 恢复+回解释器
                                                   │                   │
                                                   │                   └─► 退出点变热?
                                                   │                          是 ──► 再录 side trace (回到 START)
                                                   └──────────────────────────┘

                                                           ...
                                                     函数被回收 / trace 失效
                                                           │
                                                           ▼
                                                        销毁 (GC)
```

这就是一个 trace 从生到死的全图。后面所有的源码讲解,都是把这张图里的每一个箭头,对应到 `lj_trace.c` 里的一行代码。

### 1.4 用 P0-01 的 for 循环,跟踪它在生命周期里的流转

为了不让上面的抽象图悬在半空,我们把 P0-01 §12 那个 `for i = 1, 1000000 do x = x + i end`,在生命周期里完整走一遍。这次我们带上状态名:

```lua
local x = 0
for i = 1, 1000000 do
  x = x + i
end
```

1. **IDLE 阶段(程序启动)**:JIT 状态机处于 `LJ_TRACE_IDLE`。循环还没开始。
2. **解释器跑前若干次**:循环开始,解释器正常执行 `x = x + i`。每循环一次,经过循环回边字节码 `BC_FORL`,解释器里手写汇编的 hotcount 逻辑就把这个 pc 对应的计数器减去 `HOTCOUNT_LOOP`(=2)。
3. **热点触发(进入 START)**:计数器减到 0,解释器跳到 `lj_trace_hot`(vm_x64.dasc:2296 那条 `call extern lj_trace_hot`)。`lj_trace_hot` 把状态置为 `LJ_TRACE_START`,进入状态机。
4. **START**:状态机调用 `trace_start`——分配一个 trace 编号,初始化录制环境,记录起始 pc 和起始字节码(`BC_FORL`)。然后状态推进到 `LJ_TRACE_RECORD`。
5. **RECORD**:这是最长的一段。解释器**再跑一遍循环体**,但这次每执行一条字节码,就回调 `lj_trace_ins` → `lj_record_ins`,把这条字节码翻译成 IR 追加进去。录制器观察到 `x`、`i` 都是整数,于是:
   - 把 `x + i` 录成整数加法的 IR;
   - 在合适的位置插上 guard IR("检查 x 是整数""检查 i 是整数""检查循环没越界")。
   录到循环回边(即将回到循环开头),录制器认为"一条线性路径录完了",调用 `lj_trace_end` 把状态置为 `LJ_TRACE_END`。
6. **END → OPT**:状态机进入 `LJ_TRACE_END` 分支,先做循环优化(`lj_opt_loop`)、死代码消除(`lj_opt_dce`)、指令拆分(`lj_opt_split`)、分配消除(`lj_opt_sink`)。这些就是 P3 各章的内容。优化完,状态推进到 `LJ_TRACE_ASM`。
7. **ASM**:状态机进入 `LJ_TRACE_ASM` 分支,调用 `lj_asm_trace`——做寄存器分配、吐汇编、写进可执行内存,生成最终的 `mcode`。同时每个 guard 对应的 snapshot 也固化下来。
8. **安装(ASM 的尾巴)**:`lj_asm_trace` 返回后,`trace_state` 调 `trace_stop`——把起始字节码 `BC_FORL` 改写成 `BC_JFORL`(带 trace 编号),从此解释器跑到这里就跳机器码。`trace_save` 把这份 `GCtrace` 存进 `J->trace[]` 数组。状态回到 `LJ_TRACE_IDLE`。
9. **运行期**:剩下 99 万多次循环,每次跑到 `BC_JFORL`,直接跳进 `mcode` 飞奔。guard 都不触发,一路跑到底,回循环头再来。
10. **退出(这个例子不会发生,但概念上)**:假设某次循环 `x` 被外部赋成字符串——guard "检查 x 是整数"触发,机器码跳到退出 stub,最终调到 `lj_trace_exit`,靠 snapshot 把状态恢复成解释器的样子,退回解释器按真实类型继续。这次慢一下,但结果对。
11. **销毁(程序结束或函数回收)**:这个 `for` 所在的函数(prototype)如果被 GC 回收,挂在它名下的 trace 也会被 flush 并释放机器码内存。

这个例子贯穿全章,后面讲到每个状态,我们都会回头指给它看。

---

## §2 源码印证:TraceState 状态枚举

第一性原理把状态机推出来了。现在看 LuaJIT 的源码是怎么定义这套状态的。状态枚举在 `lj_jit.h:144`:

```c
/* Trace compiler state. */
typedef enum {
  LJ_TRACE_IDLE,        /* Trace compiler idle. */
  LJ_TRACE_ACTIVE = 0x10,
  LJ_TRACE_RECORD,      /* Bytecode recording active. */
  LJ_TRACE_RECORD_1ST,  /* Record 1st instruction, too. */
  LJ_TRACE_START,       /* New trace started. */
  LJ_TRACE_END,         /* End of trace. */
  LJ_TRACE_ASM,         /* Assemble trace. */
  LJ_TRACE_ERR          /* Trace aborted with error. */
} TraceState;
```

(`lj_jit.h:144`)

逐个对应我们推导出来的状态:

| 枚举值 | 对应 §1 的状态 | 含义 |
|---|---|---|
| `LJ_TRACE_IDLE` (0) | IDLE | 状态机空闲,没在编译任何 trace。绝大多数时间停在这里。 |
| `LJ_TRACE_START` | START | 刚发现热点,准备开始一条新 trace。分配编号、初始化环境。 |
| `LJ_TRACE_RECORD` | RECORD | 录制中:解释器边执行边记,把字节码翻成 IR。 |
| `LJ_TRACE_RECORD_1ST` | RECORD 的特殊首指令 | 录制 side trace 时,第一条指令也要录的中间态(见后文)。 |
| `LJ_TRACE_END` | OPT | 录制结束,进入优化阶段(命名上叫 END,实际承载的是优化逻辑)。 |
| `LJ_TRACE_ASM` | ASM | 汇编生成 + 安装。 |
| `LJ_TRACE_ERR` | 失败出口 | 任何阶段出错,统一跳到这里作废 trace。 |

注意三件不太显然的事:

**第一,`LJ_TRACE_ACTIVE = 0x10` 是位标志,不是序列状态。**

它的值是 16(`0x10`),而其他状态是 0、1、2、3……连续的小整数。这说明 `ACTIVE` 不是流水线上的一个阶段,而是一个**叠加位**:表示"当前有一条 trace 处于活动状态(正在编译或正在跑)"。它被单独定义,是因为退出处理和异步中止要用它。看 `lj_trace.h:45`:

```c
/* Signal asynchronous abort of trace or end of trace. */
#define lj_trace_abort(g)   (G2J(g)->state &= ~LJ_TRACE_ACTIVE)
#define lj_trace_end(J)     (J->state = LJ_TRACE_END)
```

`lj_trace_abort` 的实现就是"把 ACTIVE 位清掉"——这是一种"异步告诉状态机:当前 trace 别管了"的信号。而正常结束(`lj_trace_end`)是把整个 `state` 字段设成 `END`。两者粒度不同:结束是完整的状态切换,中止是位清除(保留其余位,让状态机自己判断)。

**第二,为什么有个 `LJ_TRACE_RECORD_1ST`?**

这是录制 side trace(从某个退出点录新 trace)时的一个中间态。root trace(从循环回边录)的起始位置明确,直接进 RECORD 就行;但 side trace 的起点是某条已存在 trace 的退出点,录制的第一条指令需要特殊处理(要把退出时的栈状态"重放"进 IR)。所以状态机用 `RECORD_1ST` 标记"正在录第一条指令",录完才进正常 `RECORD`。看 `trace_state` 里的处理(`lj_trace.c:687`):

```c
case LJ_TRACE_START:
  J->state = LJ_TRACE_RECORD;  /* trace_start() may change state. */
  trace_start(J);
  lj_dispatch_update(J2G(J), 0);
  if (J->state != LJ_TRACE_RECORD_1ST)
    break;
  /* fallthrough */

case LJ_TRACE_RECORD_1ST:
  J->state = LJ_TRACE_RECORD;
  /* fallthrough */
case LJ_TRACE_RECORD:
  ...
```

`trace_start` 内部如果发现是 side trace 且需要录首指令,会把状态改成 `RECORD_1ST`;然后这里用 `fallthrough` 无缝接到 `RECORD_1ST` → `RECORD`。设计得很紧凑。

**第三,`LJ_TRACE_END` 这个名字有误导性。**

从名字看,`END` 像是"结束",但看状态机的 `case LJ_TRACE_END` 分支(`lj_trace.c:720`),它干的其实是**优化**:

```c
case LJ_TRACE_END:
  trace_pendpatch(J, 1);
  J->loopref = 0;
  if ((J->flags & JIT_F_OPT_LOOP) &&
      J->cur.link == J->cur.traceno && J->framedepth + J->retdepth == 0) {
    setvmstate(J2G(J), OPT);
    lj_opt_dce(J);
    if (lj_opt_loop(J)) {  /* Loop optimization failed? */
      ...
      J->state = LJ_TRACE_RECORD;  /* Try to continue recording. */
      break;
    }
    J->loopref = J->chain[IR_LOOP];
  }
  lj_opt_split(J);
  lj_opt_sink(J);
  ...
  J->state = LJ_TRACE_ASM;
  break;
```

所以 `LJ_TRACE_END` 的语义其实是"**录制结束,进入编译收尾(优化+准备生成)**"。它在这里跑 DCE、loop 优化、split、sink 这一串 P3 的优化 pass,然后推进到 ASM。叫 `END` 是因为"录制到此结束",但从 trace 整个生命周期看,它才走到一半。读源码时要记住这个命名陷阱。

另外注意这里一个**精妙的设计**:如果 `lj_opt_loop` 返回非零(循环优化失败,通常是发现循环体类型不稳定),状态机**不报错**,而是把状态退回 `LJ_TRACE_RECORD`——"继续录制",试图通过多展开几圈来消除类型不稳定。这是 trace JIT 一个重要的容错手段:循环优化失败不等于整条 trace 作废,而是回到录制器再试。看 `lj_opt_loop.c:415` 的返回值约定:返回 0 表示优化成功,返回 1 表示失败(需要退回录制)。这种"优化失败→退回录制"的状态回退,是状态机里少见的**逆向**跳转,专门为了挽救那些差一点就能成的 trace。

---

## §3 源码印证:trace_state——那个大 switch

状态枚举有了。真正驱动状态机的是 `trace_state`(`lj_trace.c:680`)。这是整个 JIT 编译期的**心脏**——一个 protected callback,被包在 `lj_vm_cpcall` 里(这样可以捕获录制/优化/生成过程中抛出的错误)。它的主体就是那个大 `switch`:

```c
/* State machine for the trace compiler. Protected callback. */
static TValue *trace_state(lua_State *L, lua_CFunction dummy, void *ud)
{
  jit_State *J = (jit_State *)ud;
  UNUSED(dummy);
  do {
  retry:
    switch (J->state) {
    case LJ_TRACE_START:
      J->state = LJ_TRACE_RECORD;  /* trace_start() may change state. */
      trace_start(J);
      lj_dispatch_update(J2G(J), 0);
      if (J->state != LJ_TRACE_RECORD_1ST)
        break;
      /* fallthrough */

    case LJ_TRACE_RECORD_1ST:
      J->state = LJ_TRACE_RECORD;
      /* fallthrough */
    case LJ_TRACE_RECORD:
      trace_pendpatch(J, 0);
      setvmstate(J2G(J), RECORD);
      ...
      lj_record_ins(J);
      break;

    case LJ_TRACE_END:
      ...优化 pass...
      J->state = LJ_TRACE_ASM;
      break;

    case LJ_TRACE_ASM:
      setvmstate(J2G(J), ASM);
      lj_asm_trace(J, &J->cur);
      trace_stop(J);
      setvmstate(J2G(J), INTERP);
      J->state = LJ_TRACE_IDLE;
      lj_dispatch_update(J2G(J), 0);
      return NULL;

    default:  /* Trace aborted asynchronously. */
      setintV(L->top++, (int32_t)LJ_TRERR_RECERR);
      /* fallthrough */
    case LJ_TRACE_ERR:
      trace_pendpatch(J, 1);
      if (trace_abort(J))
        goto retry;
      setvmstate(J2G(J), INTERP);
      J->state = LJ_TRACE_IDLE;
      lj_dispatch_update(J2G(J), 0);
      return NULL;
    }
  } while (J->state > LJ_TRACE_RECORD);
  return NULL;
}
```

(`lj_trace.c:680`,有删节)

这个函数的设计有几个要点,每一个都对应我们 §1 推导出来的性质:

### 3.1 它是"录制一条、推进一段"的循环,不是"一口气编完"

注意最外层的 `do { ... } while (J->state > LJ_TRACE_RECORD)`。这个循环条件很关键:`LJ_TRACE_RECORD` 的值是 2,`LJ_TRACE_IDLE` 是 0。循环条件是"状态值 > 2 时继续转"。

这背后的逻辑是:**RECORD 状态每转一圈,只录一条字节码**(`lj_record_ins(J)` 录一条,然后 `break` 出 switch,回到 `while` 判断)。因为录制是"解释器执行一条,回调录一条",所以状态机不能一口气把整条 trace 录完——它必须录一条、退出来、让解释器去执行下一条、再被回调进来录下一条。

所以 RECORD 阶段的状态机表现是:**进 switch → 录一条字节码 → break → while 判断状态还是 RECORD(值=2,不 > 2)→ 退出循环,返回 NULL → 控制权交还解释器 → 解释器执行下一条字节码 → 又触发回调 → 再进 trace_state → 又录一条……** 如此反复,直到录制器认为路径录完了(调 `lj_trace_end` 把状态改成 END,值变大,下一轮 while 继续,直接进 END 分支跑优化)。

而 START/END/ASM 这几个状态(值都 > 2),一旦进入就是**一口气跑完**——它们不会被"录一条就退出",而是在循环里连续推进:START 完 fallthrough 到 RECORD(录第一条),END 完推进到 ASM,ASM 完 return。所以这几个状态的转换是原子的、不交还解释器的。

这个 `while (J->state > LJ_TRACE_RECORD)` 的设计,精确地编码了"录制是细粒度的、其他阶段是粗粒度的"这个区别。非常优雅。

### 3.2 每个状态切换都调一个明确的模块

把 switch 里每个 case 调用的模块列出来,正好是后面各章的地图:

| 状态 | 调用的模块 | 对应后续章节 |
|---|---|---|
| START | `trace_start` → `lj_record_setup`(`lj_record.c:2823`) | P2-06 |
| RECORD | `lj_record_ins`(`lj_record.c:2226`) | P2-06 |
| END (OPT) | `lj_opt_dce` / `lj_opt_loop`(`lj_opt_loop.c:415`)/ `lj_opt_split` / `lj_opt_sink` | P3-09~12 |
| ASM | `lj_asm_trace`(`lj_asm.c:2471`) | P4-13~15 |
| 安装 | `trace_stop`(`lj_trace.c:499`) | 本章 + P5-19 |
| ERR | `trace_abort`(`lj_trace.c:585`) | 本章 |

所以这一章的作用就是"指路牌"——你看完这个 switch,就知道每一章该去哪里看深入的实现。

### 3.3 ERR 是统一的失败出口,且会重试

看 `case LJ_TRACE_ERR` 和上面的 `default`。任何阶段出错(录制器调用 `lj_trace_err` 抛出 `LUA_ERRRUN`,被 `lj_vm_cpcall` 捕获后重定向回 `trace_state`,状态被设成 `LJ_TRACE_ERR`),都会落到这里。这里调 `trace_abort`:

```c
case LJ_TRACE_ERR:
  trace_pendpatch(J, 1);
  if (trace_abort(J))
    goto retry;
  ...
```

`trace_abort` 返回非零表示"要重试"(比如机器码区域不够大,换个区域重新 ASM;见 `lj_trace.c:599` 那个 `LJ_TRERR_MCODELM` 分支),就用 `goto retry` 重进 switch。返回零表示真的放弃了,就回 IDLE。这个"失败可重试"的机制,让 trace 编译对 transient 错误(可执行内存一时紧张)有韧性,不会因为一次偶发失败就放弃热点。

---

## §4 源码印证:三个触发入口

状态机定义清楚了。现在看**谁把状态机从 IDLE 推进到 START**。LuaJIT 有三个触发入口,对应三种"开始一条新 trace"的场景。

### 4.1 lj_trace_hot:循环/调用热点(最常见)

这是最主要的入口。解释器执行循环回边或函数调用时,hotcount 计数器减到 0,跳到这里。

先看解释器那边怎么触发的。hotcount 的自减逻辑在手写汇编 VM 里。以 x64 为例(`vm_x64.dasc:335`):

```asm
|  and reg, HOTCOUNT_PCMASK
|  sub word [DISPATCH+reg+GG_DISP2HOT], HOTCOUNT_LOOP
```

(`vm_x64.dasc:335-336`,循环回边;`:343-344` 是调用,减 `HOTCOUNT_CALL`)

这两行汇编做的事:把当前 pc 哈希(`pc & HOTCOUNT_PCMASK`,掩码是 63×2 字节),定位到 hotcount 数组(`GG_DISP2HOT` 是 hotcount 数组相对于 dispatch 表的偏移,`lj_dispatch.h:122`)的某个槽,然后**减去 `HOTCOUNT_LOOP`(=2)**。

> **★一个容易误读的点(也是对 P0-01 §6 的精确化)**:P0-01 §6 把 `HOTCOUNT_LOOP=2`、`HOTCOUNT_CALL=1` 描述为"阈值——计数到这个值就触发"。这个说法抓住了大意(值小=很快热),但严格讲不精确。源码里 `HOTCOUNT_LOOP` 是**每次回边自减的量**,不是阈值本身。真正的"初始计数"由参数 `hotloop`(默认 56,`lj_jit.h:116`)乘以 `HOTCOUNT_LOOP` 算出,初始化时写进 hotcount 槽(`lj_dispatch.c:86` 的 `lj_dispatch_init_hotcount`)。每次回边减 2,减到 0 触发。所以一个循环要跑大约 `hotloop`=56 次才会触发热点。本章把这个关系讲精确:`HOTCOUNT_LOOP/CALL` 是步长,`hotloop/hotcall` 是阈值。这是 2.1 ROLLING 的真实语义,老资料常混为一谈。

减到 0(或更小)后,汇编跳到 `lj_trace_hot`(`vm_x64.dasc:2296`):

```asm
|  lea CARG1, [DISPATCH+GG_DISP2J]
|  call extern lj_trace_hot        // (jit_State *J, const BCIns *pc)
```

看 `lj_trace_hot` 本体(`lj_trace.c:781`):

```c
/* A hotcount triggered. Start recording a root trace. */
void LJ_FASTCALL lj_trace_hot(jit_State *J, const BCIns *pc)
{
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

(`lj_trace.c:781`)

逐行看它做的事,每一件都对应 §1 推导出的需求:

1. **`hotcount_set(..., pc, hotloop*HOTCOUNT_LOOP)`**:把这次触发的 hotcount 槽**重置**回初始值。为什么?因为这条 trace 一旦录成功装上去,字节码会被改写成 `BC_JFORL`,以后不再走 hotcount;但如果录制**失败**了(回 IDLE),字节码没改,解释器还会继续走这条回边——如果不重置 hotcount,下一圈又立刻触发,陷入"反复触发反复失败"的死循环。重置成 `hotloop*HOTCOUNT_LOOP`,让失败后要再跑 `hotloop` 圈才会再次触发,给惩罚机制(后文 §7)留出起作用的时间。
2. **`if (J->state == LJ_TRACE_IDLE && ...)`**:只有当前空闲、且不在 GC hook/vmevent 里,才开始新 trace。这保证不会"录制中又开一条新录制"(状态机是单条的,同时只能编一条 trace)。
3. **`J->parent = 0; J->exitno = 0`**:标记这是 root trace(没有父 trace,没有退出号)。这两个字段在 side trace 时非零。
4. **`J->state = LJ_TRACE_START`**:推进状态机。
5. **`lj_trace_ins(J, pc-1)`**:进入状态机。注意 `pc-1`——解释器的 pc 是"指向下一条要执行的指令",所以当前要录的是 `pc-1`。

`lj_trace_ins` 是个薄包装(`lj_trace.c:770`):

```c
/* A bytecode instruction is about to be executed. Record it. */
void lj_trace_ins(jit_State *J, const BCIns *pc)
{
  J->pc = pc;
  J->fn = curr_func(J->L);
  J->pt = isluafunc(J->fn) ? funcproto(J->fn) : NULL;
  while (lj_vm_cpcall(J->L, NULL, (void *)J, trace_state) != 0)
    J->state = LJ_TRACE_ERR;
}
```

(`lj_trace.c:770`)

关键在 `lj_vm_cpcall(..., trace_state)`——它把 `trace_state` 这个状态机函数包在一个**受保护的调用**里执行。为什么需要保护?因为录制器/优化器/生成器在干活时可能抛错误(碰到不支持的字节码、类型不稳定、内存不够),这些错误用 Lua 的异常机制(`lj_err_throw`)抛出。`lj_vm_cpcall` 能捕获这些异常,把它们转化成返回码,而不是让异常一路向上传播搞崩整个 VM。捕获到错误后,`while` 循环把状态置成 `LJ_TRACE_ERR`,再进一次 `trace_state` 走错误处理。这个"用 cpcall 包状态机"的设计,是 trace 编译**失败安全**的关键一环(§6 详讲)。

### 4.2 trace_hotside:退出点变热(录 side trace)

第二个入口不在编译期,而在**运行期退出之后**。当一条已编译的 trace 跑到某个 guard 触发退出,如果这个退出点退得太频繁(说明那条没编译的分支也热了),就从这里录一条 side trace。

看 `lj_trace.c:799`:

```c
/* Check for a hot side exit. If yes, start recording a side trace. */
static void trace_hotside(jit_State *J, const BCIns *pc)
{
  SnapShot *snap = &traceref(J, J->parent)->snap[J->exitno];
  if (!(J2G(J)->hookmask & (HOOK_GC|HOOK_VMEVENT)) &&
      isluafunc(curr_func(J->L)) &&
      snap->count != SNAPCOUNT_DONE &&
      ++snap->count >= J->param[JIT_P_hotexit]) {
    lj_assertJ(J->state == LJ_TRACE_IDLE, "hot side exit while recording");
    J->state = LJ_TRACE_START;
    lj_trace_ins(J, pc);
  }
}
```

(`lj_trace.c:799`)

注意几个细节:

- 它查的是父 trace 的 snapshot 的 `count` 字段(`SnapShot.count`,`lj_jit.h:187`)。每次从这个退出点退出,`count` 加一;到 `hotexit`(默认 10,`lj_jit.h:117`)就认为够热。
- `snap->count != SNAPCOUNT_DONE`:`SNAPCOUNT_DONE`=255(`lj_jit.h:190`),表示这个退出点**已经录过 side trace 并链接好了**,不再重复录。
- 这次 `J->parent` 非零(指向父 trace),`J->exitno` 是退出号。`trace_start` 里会根据这两个字段,把新 trace 标记为 side trace,挂在父 trace 名下。

`trace_hotside` 在哪里被调?在 `lj_trace_exit` 的尾巴(`lj_trace.c:946`):

```c
} else if ((J->flags & JIT_F_ON)) {
  trace_hotside(J, pc);
}
```

所以流程是:trace 退出 → `lj_trace_exit` 恢复状态回解释器 → 顺手检查这个退出点够不够热 → 够热就启动 side trace 的录制。退出和"再录制"被巧妙地串在了同一次回调里。

### 4.3 lj_trace_stitch:trace 缝合(调用链跨函数)

第三个入口 `lj_trace_stitch`(`lj_trace.c:814`)处理一种特殊情况:一条 trace 录到一半,需要调用一个**没被 JIT 编译**的函数(比如标准库函数或还没热的用户函数)。这时不能直接把调用录进当前 trace(被调函数没有机器码可跳),也不想就此结束 trace。LuaJIT 的办法是:**把当前 trace 缝合(stitch)到被调函数返回处**——让当前 trace 跑到调用点就退出,等被调函数在解释器里跑完返回,再缝合回来继续。

```c
/* Stitch a new trace to the previous trace. */
void LJ_FASTCALL lj_trace_stitch(jit_State *J, const BCIns *pc)
{
  if (J->state == LJ_TRACE_IDLE &&
      !(J2G(J)->hookmask & (HOOK_GC|HOOK_VMEVENT))) {
    J->parent = 0;  /* Have to treat it like a root trace. */
    /* J->exitno is set to the invoking trace. */
    J->state = LJ_TRACE_START;
    lj_trace_ins(J, pc);
  }
}
```

(`lj_trace.c:814`)

注意它把 `J->parent = 0`(当 root trace 对待),但 `J->exitno` 设成"发起缝合的那条 trace 的编号"——这是个复用字段,语义和 side trace 不同。缝合出来的 trace,在 `trace_stop` 里走的是 `BC_CALL/CALLM/ITERC` 分支(`lj_trace.c:546`),把前一条 trace 的 `link` 字段指向新 trace,实现"缝合"。

缝合是个相对少见的机制(默认 `minstitch`=0,`lj_jit.h:114`),但在某些跨函数调用场景下能让 trace 跑得更久。它的存在说明:trace 之间不只是"主+侧"的树关系,还可以是"前后缝合"的链关系。P5-19 讲 TraceLink 9 种时会专门讲 `LJ_TRLINK_STITCH`。

---

## §5 源码印证:trace_start 和 trace_stop——一头一尾

状态机的 START 和 ASM 尾巴各有一个重量级函数:`trace_start`(开)和 `trace_stop`(收)。它们分别负责"把一条新 trace 拉起来"和"把一条编完的 trace 装上去"。这是 trace 生命周期里最关键的两个动作。

### 5.1 trace_start:给一条新 trace 接生

`trace_start`(`lj_trace.c:419`)很长,但逻辑清晰。我们挑关键段看:

**第一段:拒绝不该编译的情况。**

```c
static void trace_start(jit_State *J)
{
  TraceNo traceno;

  if ((J->pt->flags & PROTO_NOJIT)) {  /* JIT disabled for this proto? */
    if (J->parent == 0 && J->exitno == 0 && bc_op(*J->pc) != BC_ITERN) {
      /* Lazy bytecode patching to disable hotcount events. */
      ...
      setbc_op(J->pc, (int)bc_op(*J->pc)+(int)BC_ILOOP-(int)BC_LOOP);
      J->pt->flags |= PROTO_ILOOP;
    }
    J->state = LJ_TRACE_IDLE;  /* Silently ignored. */
    return;
  }

  /* Ensuring forward progress for BC_ITERN can trigger hotcount again. */
  if (!J->parent && bc_op(*J->pc) == BC_JLOOP) {  /* Already compiled. */
    J->state = LJ_TRACE_IDLE;  /* Silently ignored. */
    return;
  }
  ...
```

(`lj_trace.c:419-440`)

两种情况直接回 IDLE:prototype 标记了 `PROTO_NOJIT`(这个函数不允许 JIT,比如它用了 `jit.off`),或者这个 pc 已经被编译过了(`BC_JLOOP` 说明已经装了机器码)。第一种情况还会做一件聪明事:**惰性把字节码改成 `ILOOP` 变体**(`BC_LOOP`→`BC_ILOOP`),让以后这条字节码不再触发 hotcount——既然这个函数不让 JIT,就别再来烦状态机了。这是"省"的体现:不浪费编译开销在注定编不了的代码上。

**第二段:分配 trace 编号。**

```c
  /* Get a new trace number. */
  traceno = trace_findfree(J);
  if (LJ_UNLIKELY(traceno == 0)) {  /* No free trace? */
    ...
    lj_trace_flushall(J->L);
    J->state = LJ_TRACE_IDLE;
    return;
  }
  setgcrefp(J->trace[traceno], &J->cur);
```

(`lj_trace.c:442-451`)

`trace_findfree`(`lj_trace.c:61`)在 `J->trace[]` 数组里找一个空位。如果满了(达到 `maxtrace`=1000,`lj_jit.h:109`),它**清空所有 trace**(`lj_trace_flushall`)然后回 IDLE——这是个激进但合理的策略:trace 缓存满了,说明热点变了或者太多,干脆全部重来。`setgcrefp(J->trace[traceno], &J->cur)` 这一步很关键:它把当前正在构建的 `J->cur`(一个临时的 GCtrace,实际是 jit_State 内嵌的那个)登记到 trace 数组里,这样录制过程中其他地方可以通过 `traceref(J, n)` 找到它。

**第三段:初始化录制环境。**

```c
  /* Setup enough of the current trace to be able to send the vmevent. */
  memset(&J->cur, 0, sizeof(GCtrace));
  J->cur.traceno = traceno;
  J->cur.nins = J->cur.nk = REF_BASE;
  J->cur.ir = J->irbuf;
  J->cur.snap = J->snapbuf;
  J->cur.snapmap = J->snapmapbuf;
  J->mergesnap = 0;
  J->needsnap = 0;
  J->bcskip = 0;
  J->guardemit.irt = 0;
  J->postproc = LJ_POST_NONE;
  lj_resetsplit(J);
  J->retryrec = 0;
  J->ktrace = 0;
  setgcref(J->cur.startpt, obj2gco(J->pt));
  ...
  lj_record_setup(J);
}
```

(`lj_trace.c:454-496`)

这里有一个**非常重要的设计要点**:`J->cur` 不是一个独立分配的 GCtrace,而是 `jit_State` 结构体里**内嵌**的一个 `GCtrace cur` 字段(`lj_jit.h:418`)。录制期间,IR/snapshot 都往 `J->cur.ir`、`J->cur.snap` 里填——但这两个指针指向的是**共享的可增长缓冲区**(`J->irbuf`、`J->snapbuf`、`J->snapmapbuf`,见 `lj_jit.h:457-465`),不是 `J->cur` 自己的内存。

为什么这样设计?因为录制过程中 IR 会一条条追加、snapshot 会一个个加,大小事先不知道,用可增长缓冲区最方便。但**最终的 GCtrace 必须是紧凑的、独立的内存块**(因为 GC 要管理它,而且要长期存活)。所以录制期间用临时缓冲区,录制结束后(在 `lj_asm_trace` 里调 `lj_trace_alloc`)把数据**拷贝压缩**到一个新分配的紧凑 GCtrace 里。源码注释 `lj_trace.c:54` 说得很清楚:

```c
/*
** The current trace is first assembled in J->cur. The variable length
** arrays point to shared, growable buffers (J->irbuf etc.). When trace
** recording ends successfully, the current trace and its data structures
** are copied to a new (compact) GCtrace object.
*/
```

这个"临时缓冲区 → 最终紧凑拷贝"的两段式,是 trace 生命周期里一个容易被忽略但很重要的细节。它解释了为什么 `GCtrace` 的 `ir`/`snap`/`snapmap` 都是指针——它们在最终对象里指向"紧跟在 GCtrace 结构体后面的连续内存",由 `lj_trace_alloc` 一次性分配好(`lj_trace.c:123`)。

最后 `lj_record_setup(J)`(`lj_record.c:2823`)做录制专用的初始化:清空 slot 映射、设置 frame base、初始化循环展开计数器、发射固定引用(IR 的 `BASE`/`NIL`/`FALSE`/`TRUE` 这几个固定槽)。录制器正式就绪,状态机回到 `trace_state` 进入 RECORD 状态开始录第一条字节码。

### 5.2 trace_stop:把编完的 trace 装上机器

`trace_stop`(`lj_trace.c:499`)是 trace 生命周期的"成人礼"——它把编好的机器码正式安装到解释器里。核心是一个大 switch,按**起始字节码的类型**决定怎么打补丁:

```c
/* Stop tracing. */
static void trace_stop(jit_State *J)
{
  BCIns *pc = mref(J->cur.startpc, BCIns);
  BCOp op = bc_op(J->cur.startins);
  GCproto *pt = &gcref(J->cur.startpt)->pt;
  TraceNo traceno = J->cur.traceno;
  GCtrace *T = J->curfinal;

  switch (op) {
  case BC_FORL:
    setbc_op(pc+bc_j(J->cur.startins), BC_JFORI);  /* Patch FORI, too. */
    /* fallthrough */
  case BC_LOOP:
  case BC_ITERL:
  case BC_FUNCF:
    /* Patch bytecode of starting instruction in root trace. */
    setbc_op(pc, (int)op+(int)BC_JLOOP-(int)BC_LOOP);
    setbc_d(pc, traceno);
  addroot:
    /* Add to root trace chain in prototype. */
    J->cur.nextroot = pt->trace;
    pt->trace = (TraceNo1)traceno;
    break;
  ...
  case BC_JMP:
    /* Patch exit branch in parent to side trace entry. */
    lj_assertJ(J->parent != 0 && J->cur.root != 0, "not a side trace");
    lj_asm_patchexit(J, traceref(J, J->parent), J->exitno, J->cur.mcode);
    ...
    /* Add to side trace chain in root trace. */
    {
      GCtrace *root = traceref(J, J->cur.root);
      root->nchild++;
      J->cur.nextside = root->nextside;
      root->nextside = (TraceNo1)traceno;
    }
    break;
  ...
  }

  /* Commit new mcode only after all patching is done. */
  lj_mcode_commit(J, J->cur.mcode);
  J->postproc = LJ_POST_NONE;
  trace_save(J, T);
  ...
}
```

(`lj_trace.c:499-567`,有删节)

几个要点:

**安装的本质是改写字节码。** 看 `case BC_FORL/LOOP/ITERL/FUNCF` 那段:`setbc_op(pc, op + BC_JLOOP - BC_LOOP)`——把起始字节码的操作码从普通循环指令(`BC_FORL` 等)改成 JIT 循环指令(`BC_JFORL` 等),并把 trace 编号塞进字节码的 D 字段(`setbc_d(pc, traceno)`)。从此解释器 dispatch 到 `BC_JFORL` 时,会查 D 字段的 trace 编号,直接跳进那段 trace 的 `mcode`。这就是"安装"的真相:**不是装一段代码到某个地方等它运行,而是把解释器里的"入口字节码"改成一个跳转指令**。

**FORL 比较特殊,还要改 FORI。** 数值 for 循环的字节码是 `FORI`(循环头)和 `FORL`(回边)一对。装 trace 时不仅改 `FORL`,还把对应的 `FORI` 改成 `JFORI`(`setbc_op(pc+bc_j(...), BC_JFORI)`)。为什么?因为 trace 的机器码包含了整个循环体,从 `FORI` 之后开始。改了 `JFORI`,让循环**进入**时也能直接跳进机器码,而不只是回边时。这是个细节优化。

**root trace 和 side trace 的安装方式不同。** root trace 是改字节码(`setbc_op`);side trace(起始字节码是 `BC_JMP`,从父 trace 的退出点录的)不是改字节码,而是 `lj_asm_patchexit`——**直接改父 trace 的机器码**,把那个退出点的跳转目标从"退出 stub"改成"新 side trace 的入口"。这是机器码层面的"热补丁"。同时把新 trace 挂到 root trace 的 `nextside` 链上,`nchild++`。

**所有 patch 完才 commit mcode。** `lj_mcode_commit` 把机器码内存的页权限从可写改成可执行(W^X,见 P4-15)。必须在所有改写(字节码 patch、机器码 patch)完成之后才 commit——因为 commit 之后这块内存就只读可执行了,不能再改。这个顺序保证 patch 不会被半途锁死。

**最后 trace_save 把 trace 存档。** `trace_save`(`lj_trace.c:145`)做几件事:把 `J->cur` 的内容拷到最终紧凑的 `GCtrace`(由 `lj_asm_trace` 里的 `lj_trace_alloc` 分配的那个 `J->curfinal`),挂进 GC 根集(`setgcrefr(T->nextgc, J2G(J)->gc.root)`,让 GC 能扫到),登记到 `J->trace[traceno]`,调 `lj_gc_barriertrace` 通知 GC。从此这条 trace 是一个正式的 GC 对象,有了户口。

---

## §6 为什么 sound:失败安全与阶段交接

讲完了正向流程,这一节专门讲"为什么这套设计不会算错"。这是 §1 性质二(每个阶段都可能失败,且失败后必须安全退回)的源码兑现。LuaJIT 用三层机制保证:任何阶段失败,程序结果都和纯解释器一致。

### 6.1 第一层:任何阶段出错,统一走 ERR

录制器、优化器、生成器在任何地方发现"这条 trace 编不下去了",都调 `lj_trace_err` 或 `lj_trace_err_info`(`lj_trace.c:38`):

```c
/* Synchronous abort with error message. */
void lj_trace_err(jit_State *J, TraceError e)
{
  setnilV(&J->errinfo);
  setintV(J->L->top++, (int32_t)e);
  lj_err_throw(J->L, LUA_ERRRUN);
}
```

它把错误码(一个 `TraceError` 枚举值,定义在 `lj_traceerr.h`,有几十种:类型不符、循环不稳定、机器码超限……)压栈,然后 `lj_err_throw` 抛出 `LUA_ERRRUN`。这个异常被外层 `lj_vm_cpcall` 捕获,`lj_trace_ins` 的 `while` 循环把状态置成 `LJ_TRACE_ERR`,重进 `trace_state` 走 `case LJ_TRACE_ERR` → `trace_abort`。

`trace_abort`(`lj_trace.c:585`)做三件事:释放这条 trace 占的临时资源(`lj_mcode_abort` 丢弃半成品的机器码,`lj_trace_free` 释放 `curfinal`)、惩罚导致失败的起始字节码(让以后更难再触发,见 §7)、清空 `J->cur.traceno`。然后状态回 IDLE,控制权交还解释器。

**关键点:失败发生在 trace 还没装上去之前。** 机器码还没 commit、字节码还没 patch,解释器对这条 trace 一无所知。所以失败之后,解释器就当什么都没发生过,继续按它原本的方式(纯解释)执行下一条字节码。**程序的正确性完全由解释器保底,trace 的失败不会污染任何已执行的副作用。** 这就是 P0-01 §9 讲的"机器码要么和解释器一样,要么退回解释器,永远不会更错"的根基——而这里讲的是它在生命周期层面的兑现:**失败发生在安装之前,所以根本没有"错误的机器码被执行"的机会**。

### 6.2 第二层:惩罚机制,避免反复失败

如果某条字节码反复触发热点、反复录制失败(比如那段代码类型太混乱,怎么窄化都窄不了),每次都要走一遍 START→RECORD→ERR 的开销,很浪费。LuaJIT 用**惩罚(penalty)机制**避免:

```c
/* Penalize a bytecode instruction. */
static void penalty_pc(jit_State *J, GCproto *pt, BCIns *pc, TraceError e)
{
  uint32_t i, val = PENALTY_MIN;
  for (i = 0; i < PENALTY_SLOTS; i++)
    if (mref(J->penalty[i].pc, const BCIns) == pc) {  /* Cache slot found? */
      val = ((uint32_t)J->penalty[i].val << 1) + (...);  /* Double penalty. */
      if (val > PENALTY_MAX) {
        blacklist_pc(pt, pc);  /* Blacklist it, if that didn't help. */
        return;
      }
      goto setpenalty;
    }
  ...
setpenalty:
  J->penalty[i].val = (uint16_t)val;
  J->penalty[i].reason = e;
  hotcount_set(J2GG(J), pc+1, val);
}
```

(`lj_trace.c:392`,有删节)

逻辑:每条导致失败的起始字节码,在 `J->penalty[]`(64 个槽的轮转缓存,`lj_jit.h:309`)里记一笔。每次再失败,**惩罚值翻倍**(`val << 1`)。惩罚值就是下次这条字节码的 hotcount 初始值——值越大,要跑越多圈才再触发。这样,反复失败的代码,触发频率指数下降。

如果翻倍超过 `PENALTY_MAX`(60000,`lj_jit.h:311`),就**拉黑**(`blacklist_pc`,`lj_trace.c:380`):把字节码永久改成 `ILOOP` 变体(加 `BC_ILOOP-BC_LOOP` 偏移),让它**永远不再触发热点**。这是终极保险——一段注定编不出的代码,直接踢出 JIT 候选,专心用解释器跑。

惩罚机制是"省"在生命周期里的体现:把编译开销花在"有希望成功"的代码上,对反复失败的果断放弃。

### 6.3 第三层:阶段交接的数据完整性

状态机保证每个阶段的输入是上一个阶段完整的产出。具体看 `trace_state` 里阶段切换的顺序:

- **RECORD → END**:录制器把 IR 一条条追加到 `J->cur.ir`(指向 `J->irbuf`)。调 `lj_trace_end` 时,IR 已经是完整的(这条线性路径从头到尾都录完了)。END 分支拿这份完整 IR 跑优化。
- **END → ASM**:优化 pass(`lj_opt_*`)原地修改 `J->cur.ir`——折叠、消除、替换。优化完,IR 还是同一份缓冲区,只是内容更紧凑。ASM 拿这份优化后的 IR 生成机器码。
- **ASM → 安装**:`lj_asm_trace`(`lj_asm.c:2471`)读 `J->cur` 的优化后 IR,做寄存器分配,吐机器码到 `J->cur.mcode`(实际是新分配的可执行内存)。同时它调 `lj_trace_alloc` 把 IR/snapshot 拷贝到紧凑的最终 GCtrace(`J->curfinal`)。`trace_stop` 再把这份最终对象存档。

每一级的数据依赖都满足:**后一个阶段只读前一个阶段已完成的产出**。没有任何阶段会读到半成品(比如优化器不会在录制器还没录完时就动 IR)。这个严格的数据流,是状态机顺序推进(`while (J->state > LJ_TRACE_RECORD)` 加 fallthrough)天然保证的。

### 6.4 一个边界:运行期失败靠 snapshot,不是状态机

上面三层讲的都是**编译期**的失败安全。但 trace 生命周期还有**运行期**的失败——guard 触发。运行期失败不是状态机的事(那时状态机在 IDLE),它走的是另一条机制:snapshot 恢复。

这部分是 P5-16/17 的核心,本章只在生命周期层面点一句:运行期 guard 触发,机器码跳到退出 stub,最终调 `lj_trace_exit`(`lj_trace.c:886`),它靠 snapshot 把寄存器状态恢复成解释器期望的栈状态,然后回解释器。解释器从恢复出的 pc 继续执行,结果和"这条 trace 从来没被编译过"完全一样。**运行期失败的正确性,由 snapshot 的完整性 + 解释器的正确性共同保证。** §8 会从生命周期角度再点一下 `lj_trace_exit`。

---

## §7 GCtrace:trace 在生命周期里怎么被填充

P0-01 §11 已经让读者见过 `GCtrace` 的三件套(IR / snapshot / 机器码)。这里我们换一个角度看它:**在整个生命周期里,GCtrace 的各个字段是分阶段被填进去的**。把填充的时机讲清楚,GCtrace 就不再是一堆字段,而是一条"trace 从空到满"的时间线。

先回顾结构(`lj_jit.h:250`,P0-01 已贴,这里只点关键):

```c
typedef struct GCtrace {
  GCHeader;
  uint16_t nsnap;      /* Number of snapshots. */
  IRRef nins;          /* Next IR instruction. */
  GCRef gclist;
  IRIns *ir;           /* IR instructions/constants. */
  IRRef nk;            /* Lowest IR constant. */
  uint32_t nsnapmap;   /* Number of snapshot map elements. */
  SnapShot *snap;      /* Snapshot array. */
  SnapEntry *snapmap;  /* Snapshot map. */
  GCRef startpt;       /* Starting prototype. */
  MRef startpc;        /* Bytecode PC of starting instruction. */
  BCIns startins;      /* Original bytecode of starting instruction. */
  MSize szmcode;       /* Size of machine code. */
  MCode *mcode;        /* Start of machine code. */
  MSize mcloop;        /* Offset of loop start in machine code. */
  uint16_t nchild;     /* Number of child traces (root trace only). */
  uint16_t spadjust;   /* Stack pointer adjustment. */
  TraceNo1 traceno;    /* Trace number. */
  TraceNo1 link;       /* Linked trace. */
  TraceNo1 root;       /* Root trace of side trace. */
  TraceNo1 nextroot;   /* Next root trace for same prototype. */
  TraceNo1 nextside;   /* Next side trace of same root trace. */
  uint8_t sinktags;
  uint8_t topslot;
  uint8_t linktype;
  ...
} GCtrace;
```

(`lj_jit.h:250`)

按生命周期阶段,填充时机如下:

| 字段 | 何时填充 | 谁填的 |
|---|---|---|
| `traceno` | START | `trace_start`(`lj_trace.c:455`) |
| `startpt`, `startpc`, `startins` | START / RECORD 初 | `trace_start` + `lj_record_setup`(`lj_record.c:2862-2863`) |
| `ir` 各条指令, `nins`, `nk` | RECORD(逐条) | `lj_record_ins` 每次 `emitir` |
| `snap` 各项, `snapmap`, `nsnap`, `nsnapmap` | RECORD(按需)+ END | `lj_snap_add`(录制时每个关键点)+ 优化时调整 |
| `root`, `nextside`(side trace) | START | `lj_record_setup`(`lj_record.c:2867`) |
| `link`, `linktype` | END / 安装 | `lj_record_stop` 设 linktype,`trace_stop` 设 link |
| `mcode`, `szmcode`, `mcloop`, `spadjust` | ASM | `lj_asm_trace`(`lj_asm.c:2471`) |
| `nchild`, `nextroot` | 安装 | `trace_stop`(`lj_trace.c:519,542`) |

几个值得展开的点:

**`ir` 是边录边长的。** RECORD 阶段,录制器每翻译一条字节码,就调一次 `emitir`(以及内部的折叠逻辑),往 `J->irbuf` 里追加一条 `IRIns`。`nins` 字段是"下一条 IR 指令的引用号"(带 REF_BIAS 偏移,见 P2-07),每追加一条就加一。到 RECORD 结束,`ir[REF_BASE+1 .. nins-1]` 就是这条 trace 完整的 IR 指令序列;`ir[nk .. REF_BASE-1]` 是常量区(从下往长)。

**`snap` 是录制时按需加的,不是录完统一生成。** snapshot 不是每条 IR 都有,而是在"可能退出的点"(每个 guard、循环回边、调用边界)加。录制器在合适的时机调 `lj_snap_add`,把当前栈状态压缩成一组 `SnapEntry`,追加到 `snapmap`,`snap` 数组加一项记录这组 entry 的偏移和元数据。这意味着 snapshot 和 IR 是**交错生成**的——录到哪个 guard,就当场记下"如果这个 guard 退出,状态该怎么恢复"。

**`mcode` 只在 ASM 阶段才出现。** 录制和优化期间,`J->cur.mcode` 是 NULL(没有机器码)。只有进入 `lj_asm_trace`,它才 `lj_mcode_reserve` 申请一块可执行内存,从后往前吐汇编,最后 `mcode` 指向这块内存的入口。这印证了 §1 的"顺序依赖":没有优化完的 IR,就没有机器码。

**`link` 和 `linktype` 决定 trace 跑完去哪。** `linktype` 是 `TraceLink` 枚举(`lj_jit.h:237`,P0-01 §13 贴过 9 种),`link` 是目标 trace 编号。这两个字段在录制结束(`lj_record_stop`)时设:循环 trace 设 `LJ_TRLINK_LOOP` 且 `link=自己`;接到别的 trace 设 `LJ_TRLINK_ROOT`;回解释器设 `LJ_TRLINK_INTERP`。机器码跑完,按这两个字段决定跳转目标(P5-19 详讲)。

**`root`/`nextroot`/`nextside`/`nchild` 管理 trace 树。** root trace 的 `root`=0;side trace 的 `root`=它的根 trace 编号。同一个 prototype 下的多个 root trace 用 `nextroot` 串成链(挂在 `pt->trace` 字段上);同一个 root 下的多个 side trace 用 `nextside` 串成链(挂在 root trace 的 `nextside` 上)。`nchild` 记录这个 root 有多少个 side trace。这四个字段把所有 trace 组织成一棵(或一片)树,GC 和 trace 链接都靠它遍历。

理解了填充时机,GCtrace 就活了起来:它不是静态的数据结构,而是**一条 trace 一生所有阶段的产出物的汇集**。每个字段背后,都对应生命周期的某个动作。

---

## §8 运行期环节:lj_trace_exit 怎么把控制权交还解释器

前面 §1-§7 讲的都是编译期。这一节简短地点一下**运行期**——trace 装好之后,机器码跑起来,guard 触发退出时,生命周期怎么继续。这是 P5-16/17 的预览,这里只从生命周期角度看清入口和出口。

退出发生在机器码内部:某个 guard 检查失败,机器码跳到一个**退出 stub**(exit stub)。退出 stub 是 ASM 阶段为每个 snapshot 生成的的一小段代码,它把所有寄存器存到一块叫 `ExitState` 的内存里,然后跳到平台相关的退出处理器(如 x64 的 `lj_vm_exit_handler`),最终调到 C 函数 `lj_trace_exit`(`lj_trace.c:886`)。

```c
/* A trace exited. Restore interpreter state. */
int LJ_FASTCALL lj_trace_exit(jit_State *J, void *exptr)
{
  ERRNO_SAVE
  lua_State *L = J->L;
  ExitState *ex = (ExitState *)exptr;
  ExitDataCP exd;
  int errcode, exitcode = J->exitcode;
  ...
  T = traceref(J, J->parent); UNUSED(T);
  ...
  exd.J = J;
  exd.exptr = exptr;
  errcode = lj_vm_cpcall(L, NULL, &exd, trace_exit_cp);
  if (errcode)
    return -errcode;
  ...
  pc = exd.pc;
  cf = cframe_raw(L->cframe);
  setcframe_pc(cf, pc);
  ...
  } else if ((J->flags & JIT_F_ON)) {
    trace_hotside(J, pc);   /* 检查退出点够不够热 */
  }
  ...
}
```

(`lj_trace.c:886`,有大幅删节)

关键在 `trace_exit_cp`(`lj_trace.c:835`)调的 `lj_snap_restore`(`lj_snap.c:940`):

```c
static TValue *trace_exit_cp(lua_State *L, lua_CFunction dummy, void *ud)
{
  ExitDataCP *exd = (ExitDataCP *)ud;
  ...
  exd->pc = lj_snap_restore(exd->J, exd->exptr);
  ...
}
```

`lj_snap_restore` 做的事:拿父 trace 的 snapshot(`traceref(J, J->parent)->snap[J->exitno]`),照着它把 `ExitState` 里寄存器的值,搬运、翻译成解释器值栈上的样子,并算出解释器应该从哪条字节码继续(`pc`)。恢复完,`lj_trace_exit` 把这个 pc 塞回解释器的 cframe(`setcframe_pc(cf, pc)`),返回。控制权回到解释器的 dispatch 循环,解释器从恢复出的 pc 继续执行——就像这条 trace 从来没跑过一样。

注意 `lj_trace_exit` 的尾巴调了 `trace_hotside(J, pc)`——这正是 §4.2 讲的 side trace 触发点。退出处理和"再录制"被串在了同一次回调里:退出恢复完,顺手看看这个退出点够不够热,够热就启动 side trace 录制。这个串联是 trace 生命周期"运行期 → 编译期"的自然回流。

`lj_trace_exit` 的返回值也有讲究:正常退出返回 0 或 MULTRES(对于 CALLM/RETM/TSETM 这种变长返回);带错误退出返回负的错误码(`-exitcode`);特殊地,遇到 `BC_JLOOP` 且需要保证前向进度时返回 `-17`(`LUA_YIELD` 的值,表示"让解释器重新 dispatch")。这些返回值由汇编退出处理器解释,决定解释器接下来怎么走。细节留给 P5。

---

## §9 trace 的存储与管理:jit_State 与 tracemap

现在从单条 trace 拉远,看**所有 trace 是怎么被管理的**。管理者是 `jit_State`(类型定义在 `lj_jit.h:417`),它是整个 JIT 编译器的中心对象,一个 Lua VM 只有一个(通过 `G2J(g)` 宏访问)。我们已经零散见过它的字段,这里集中看和 trace 生命周期管理相关的。

### 9.1 jit_State 里和 trace 管理相关的字段

```c
typedef struct jit_State {
  GCtrace cur;          /* 当前正在构建的 trace(内嵌) */
  GCtrace *curfinal;    /* 当前 trace 的最终地址(asm 期间设) */
  ...
  TraceState state;     /* 状态机当前状态 */
  ...
  GCRef *trace;         /* 所有 trace 的数组(按编号索引) */
  TraceNo freetrace;    /* 下一个空闲 trace 编号扫描起点 */
  MSize sizetrace;      /* trace 数组大小 */
  ...
  TraceNo parent;       /* 当前 side trace 的父(0=root) */
  ExitNo exitno;        /* 当前 side trace 在父的退出号 */
  ...
} jit_State;
```

(`lj_jit.h:417`,有删节)

三个核心:

**`cur` 是正在构建的 trace。** 前面讲过,它是内嵌的 GCtrace,录制期间所有数据往这里填。同一时刻只有一条 trace 在构建(状态机是单条的),所以一个 `cur` 就够。

**`trace[]` 是所有已完成 trace 的数组。** `trace[n]` 指向编号为 n 的 GCtrace(或 NULL,如果该编号空闲)。`traceref(J, n)` 宏(`lj_jit.h:289`)就是带边界检查地取这个数组元素。trace 编号从 1 开始(0 是无效),上限 `maxtrace`=1000。编号一旦分配,在 trace 存活期间不变——机器码 patch、trace 链接都用编号引用,所以编号必须稳定。

**`parent`/`exitno` 区分 root 和 side。** root trace 的 `parent`=0;side trace 的 `parent`=父 trace 编号,`exitno`=它从父的哪个退出点录的。这两个字段在 START 时设,贯穿整条 trace 的构建和安装(安装 side trace 时要用它们找到父 trace 做 `lj_asm_patchexit`)。

### 9.2 tracemap:按起始 pc 查 trace

一个关键需求:解释器跑到某条字节码,怎么知道这里有没有装过 trace?有两层:

**第一层:字节码 patch 自带。** 安装 root trace 时,起始字节码被改写成 `BC_JFORL`/`BC_JLOOP` 等,且 D 字段存了 trace 编号。解释器 dispatch 到这些 JIT 变体字节码时,直接读 D 字段跳机器码。这是最直接的"按 pc 查 trace"——查的就是字节码本身。

**第二层:prototype 的 trace 链。** 每个 prototype(函数对象)有个 `trace` 字段(`GCproto.trace`),指向挂在它名下的第一条 root trace 编号。`trace_stop` 的 `addroot` 标签(`lj_trace.c:517`)就在维护这条链:

```c
  J->cur.nextroot = pt->trace;
  pt->trace = (TraceNo1)traceno;
```

新 trace 头插进链表。这样从一个 prototype 能遍历到它名下所有 root trace(`nextroot` 链),再从每个 root 遍历到它的 side trace(`nextside` 链)。**为什么一个函数可以有多个 trace?** 因为同一个函数可能有多个热点入口:不同的循环、不同的调用路径。每个热点入口录一条 root trace,它们都挂在同一个 prototype 名下,用 `nextroot` 串起来。

这两层加起来,就是 LuaJIT 的"trace 查找":字节码 patch 处理"当前 pc 有没有机器码可跳"的快路径;prototype 的 trace 链处理"这个函数有哪些 trace"的慢路径(用于 flush、GC、调试)。

### 9.3 trace 的容量限制与淘汰

`maxtrace`=1000(`lj_jit.h:109`)是 trace 总数上限。`maxside`=100(`lj_jit.h:112`)是一个 root 最多多少个 side trace。`maxsnap`=500(`lj_jit.h:113`)是一条 trace 最多多少个 snapshot。这些上限都是"省"的体现——防止 trace 失控膨胀。

满了怎么办?`trace_findfree` 找不到空位时(`lj_trace.c:74`),`trace_start` 调 `lj_trace_flushall` 全部清空重来。这是个粗暴但有效的策略:trace 缓存是 LRU 的一种近似(虽然不是严格的 LRU),满了就全清,让新的热点重新录。另外用户也可以主动调 `jit.flush()` 触发清空。

---

## §10 trace 的销毁:GC 与 trace 的关系

trace 是 GC 对象(`GCtrace` 的 `GCHeader` 让它有 `gct` 类型标记 `~LJ_TTRACE`,`lj_trace_alloc` 里 `T2->gct = ~LJ_TTRACE`,`lj_trace.c:132`)。这意味着它的生命周期最终由 GC 管理——trace 会死。这一节讲 trace 怎么死。

### 10.1 trace 挂在 GC 根集上

`trace_save`(`lj_trace.c:151`)在存档时,把新 trace 挂进 GC 的 main chain:

```c
  setgcrefr(T->nextgc, J2G(J)->gc.root);
  setgcrefp(J2G(J)->gc.root, T);
  newwhite(J2G(J), T);
```

从此 GC 的标记阶段能扫到它。trace 的可达性,主要通过两条路径维护:

**路径一:prototype → trace 链。** `GCproto.trace` 字段指向 root trace,root 的 `nextroot` 指向下一个 root,root 的 `nextside` 指向它的 side trace。只要 prototype 活着,挂在它名下的 trace 都可达。GC 标记 prototype 时,会顺着这条链标记所有相关 trace。看 `lj_gc.c:287`:

```c
  if (pt->trace) gc_marktrace(g, pt->trace);
```

**路径二:trace 之间的 link。** `gc_marktrace`(`lj_gc.c:244`)顺着 trace 的 `link`/`nextroot`/`nextside` 把整棵 trace 树标记上:

```c
static void gc_marktrace(global_State *g, TraceNo traceno)
{
  GCobj *o = obj2gco(traceref(G2J(g), traceno));
  ...
  if (T->link) gc_marktrace(g, T->link);
  if (T->nextroot) gc_marktrace(g, T->nextroot);
  if (T->nextside) gc_marktrace(g, T->nextside);
  ...
}
```

(`lj_gc.c:244-269`,有删节)

所以即使 prototype 被回收了(它的字节码要释放),只要还有别的活对象引用着某条 trace(比如机器码 patch 里存了 trace 编号),这条 trace 也不会被误回收。GC 把 trace 树当成一个整体来标记。

### 10.2 flush:主动让 trace 死

除了 GC 回收,trace 还会被**主动 flush**。三种 flush:

**`lj_trace_flush`(单条,`lj_trace.c:259`)**:flush 一条 root trace。先从 prototype 的 trace 链里摘下来(`trace_flushroot`),再 `trace_unpatch` 把字节码改回原样(`BC_JFORL`→`BC_FORL`)。注意它只 unpatch 字节码,trace 对象本身的释放交给 GC(因为可能有 side trace 或 link 还引用它)。

**`lj_trace_flushproto`(按函数,`lj_trace.c:269`)**:flush 一个 prototype 名下的所有 root trace。函数被重新加载(比如热更新)时调。

**`lj_trace_flushall`(全部,`lj_trace.c:276`)**:flush 所有 trace。遍历 `J->trace[]`,flush 每个 root,清空 penalty 缓存,**释放所有机器码内存**(`lj_mcode_free`),清空 exit stub 组。这个最彻底——`trace_findfree` 满了、`trace_abort` 遇到 `LJ_TRERR_MCODEAL`(机器码区域分配失败)时都会调。

### 10.3 trace 死的时候,机器码怎么处理

trace 对象被 GC 回收时,调 `lj_trace_free`(`lj_trace.c:172`):

```c
void LJ_FASTCALL lj_trace_free(global_State *g, GCtrace *T)
{
  jit_State *J = G2J(g);
  if (T->traceno) {
    lj_gdbjit_deltrace(J, T);
    if (T->traceno < J->freetrace)
      J->freetrace = T->traceno;
    setgcrefnull(J->trace[T->traceno]);
  }
  lj_mem_free(g, T, ...);
}
```

注意一个**重要的细节**:`lj_trace_free` 只释放 GCtrace 对象本身(包括紧跟其后的 IR/snapshot 内存,它们是一次性分配的连续块,**lj_trace.c:181-183**),但**不释放机器码**!机器码内存由 `jit_State` 的 `mcarea` 链统一管理(`lj_jit.h:506`),它的释放走另一条路:`lj_mcode_free`(在 `lj_trace_flushall` 或 `lj_trace_freestate` 时整体释放)。

为什么机器码和 trace 对象分开管理?因为机器码内存有特殊要求(必须可执行,W^X,见 P4-15),而且多个 trace 的机器码可能共享同一块大的 mcode area(为了减少 mprotect 调用)。所以机器码内存按 area 管理,不跟单个 trace 绑定。trace 对象死了,只是 `J->trace[n]` 被置 NULL,字节码被 unpatch;机器码内存要等整个 area 被 flush 才释放。

这个分离意味着:trace 死后,它的机器码可能还躺在 mcode area 里一段时间(直到 area 被 flush 或 VM 退出)。这是无害的——字节码已经 unpatch 回普通指令,不会跳进这段死机器码;它只是占点内存,等下次 flush 回收。

### 10.4 VM 退出时彻底清理

`lj_trace_freestate`(`lj_trace.c:359`)在 VM 销毁时调,释放所有 JIT 相关内存:所有 mcode area、snapshot 缓冲区、IR 缓冲区、trace 数组。这是 trace 生命周期的终点——VM 都没了,trace 自然全死。

至此,trace 的完整一生走完了:从 hotcount 触发接生(`trace_start`),经录制/优化/生成长大(`trace_state` 的 START→RECORD→END→ASM),通过安装成年(`trace_stop`),在运行期飞奔或退出(`lj_trace_exit`),最终被 GC 或 flush 收尸(`lj_trace_free`/`lj_trace_flushall`)。这就是 JIT 侧的总纲。

---

## §11 ★对照:官方 Lua 与 JVM/V8

把 LuaJIT 的 trace 生命周期和两个对象对照,取舍会更清晰。

### 11.1 对照一:官方 Lua(切"有没有生命周期")

官方 Lua 是纯解释器,**根本没有 trace 这个概念,也就没有 trace 的生命周期**。它只有字节码和 prototype:字节码编译一次(前端 `lj_parse.c` 那一步,官方 Lua 也有),之后永远解释执行,不编译、不优化、不退出、不销毁机器码(因为没有机器码)。

所以对照官方 Lua,LuaJIT 多出来的**全部**是这条生命周期:热点检测、状态机、录制、优化、生成、安装、运行期退出、trace 树管理、GC 回收。官方 Lua 的字节码对象一旦创建,生命周期极其简单(跟着 prototype 活,prototype 死它死);LuaJIT 在字节码对象之上,又叠了一整套"运行时编译产物"的生命周期管理。这是 JIT 相对解释器的**全部复杂度来源**——快几十倍的代价,就是实现并维护这套生命周期。

一个具体的对照点:**失败处理**。官方 Lua 解释器每条字节码都正确执行,不存在"编译失败"这回事;LuaJIT 的 trace 可能在任何阶段失败(§6),必须有一整套 ERR 处理 + 惩罚 + 黑名单机制保证失败安全。这套机制官方 Lua 完全不需要。

### 11.2 对照二:JVM/V8(切"trace 状态机 vs method 状态机")

JVM 和 V8 也有 JIT,它们的编译产物(机器码)也有生命周期。但它们的编译单位是 **method(整个方法)**,不是 trace。这个根本区别,让两者的"生命周期状态机"形状不同。

**JVM/V8 的 method 状态机**(以 HotSpot 为例,概念上):

```
  方法被调用多次
       │
       ▼
  解释器(C1/解释层)计数到阈值
       │
       ▼
  ┌──────────┐
  │ 编译整个  │  C1(client)或 C2(server)编译器
  │  方法    │  把整个方法的字节码→IR→优化→机器码
  └────┬─────┘
       │
       ▼
  安装(method 的入口表指向新机器码)
       │
       ▼
  运行机器码
       │
       ▼
  假设失效?(类型 profile 变了)
       │ 是
       ▼
  deoptimization(去优化)
       │ 机器码废弃,退回解释器,可能重新编译
```

和 LuaJIT 的 trace 状态机比,几个根本不同:

**第一,编译单位不同 → 状态机粒度不同。** JVM 编译整个方法,一次编译覆盖方法内所有分支;LuaJIT 只录一条线性路径,一次编译只覆盖一条路径。所以 JVM 的状态机是"方法级"的(一个方法一个状态),LuaJIT 是"trace 级"的(一个方法可能有十几条 trace,每条独立的状态)。这导致 LuaJIT 的 trace 之间有复杂的链接关系(TraceLink 9 种、side trace 树),而 JVM 的 method 之间相对独立(主要是去优化后重编译)。

**第二,假设失效的处理不同。** JVM 用 **deoptimization**:假设失效时,把方法从"已编译"状态退回"解释"状态,废弃机器码,以后可能基于新的类型 profile 重新编译。LuaJIT 用 **side exit + snapshot**:假设失效时,不废弃整条 trace,只是从失效的那个 guard 退出,退回解释器继续;如果那个退出点够热,再录一条 side trace 覆盖那条没编译的分支。**JVM 是"全有或全无"(整个方法要么编译要么不编译),LuaJIT 是"渐进覆盖"(一条条 trace 把路径空间逐渐填满)**。LuaJIT 的方式对动态语言更友好——它不要求一次性推断对整个方法的所有类型,而是边跑边补。

**第三,编译时机的触发器不同。** JVM 用 method invocation counter(方法调用计数)和 backedge counter(回边计数),阈值较高(默认几千次);LuaJIT 用 hotcount,阈值很低(`hotloop`=56 次回边)。这是因为 trace 短、编译快,可以更早触发;method 长、编译慢,必须更谨慎。这也反映在生命周期的"启动成本"上:LuaJIT 一条 trace 从触发到装上可能只要微秒级;JVM 的 C2 编译一个方法可能要毫秒级。

**第四,失败安全的形式不同。** 两者都保证"失败回解释器,结果正确",但机制不同。JVM 的 deoptimization 靠**转移栈(deoptimization frame)**——把机器码的执行状态映射回解释器/字节码栈,和 LuaJIT 的 snapshot 异曲同工(都是"机器码状态 → 解释器状态"的翻译表)。但 JVM 因为编译整个方法,snapshot 粒度是方法的每个安全点;LuaJIT 因为只编译线性路径,snapshot 粒度是每个 guard。LuaJIT 的 snapshot 更密(几乎每条可能失败的 IR 都有),但因为 trace 短,总数可控。

下面这个表把生命周期维度的对照列清楚:

| 维度 | LuaJIT(trace JIT) | 官方 Lua | JVM/V8(method JIT) |
|---|---|---|---|
| 编译单位 | 一条线性热路径(trace) | 不编译 | 整个方法(method) |
| 触发器 | hotcount(回边/调用计数) | — | method/backedge counter |
| 状态机粒度 | 每条 trace 一个状态 | — | 每个方法一个状态 |
| 一次编译覆盖 | 一条路径 | — | 方法内所有分支 |
| 假设失效处理 | side exit + snapshot(局部退出) | — | deoptimization(整个方法退回) |
| 失败后恢复 | 退回解释器,退出点热则录 side trace | — | 退回解释器,可能基于新 profile 重编译整个方法 |
| trace/method 间关系 | TraceLink 9 种 + side trace 树 | — | 相对独立(去优化后重编译) |
| 生命周期终态 | GC 回收 / flush | — | method 卸载 / code cache 满 LRU |

trace 状态机 vs method 状态机,是 JIT 两大流派在"生命周期"维度的根本分歧。LuaJIT 选 trace,换来更快的编译、更激进的假设、更细粒度的渐进覆盖;代价是更复杂的 trace 间链接和退出处理(整个 P5 篇)。这些复杂性,正是从本章这条生命周期状态机里长出来的。

---

## §12 回扣主线 + 后续地图

回到本书的主线:**把动态执行安全变成机器码**。

这一章讲的是这条主线在 **JIT 侧的总纲**。一个 trace 从无到有到死的一生,就是"动态执行"被"安全变成机器码"的完整过程在时间轴上的展开:

- **"变成机器码"** 体现在 START→RECORD→END→ASM 这条编译流水线——字节码边执行边被录成 IR,IR 被优化,优化后的 IR 被生成机器码并安装。
- **"安全"** 体现在三层失败安全——任何阶段出错走 ERR 作废(编译期)、guard 触发靠 snapshot 退回解释器(运行期)、惩罚与黑名单避免反复失败。加上 GCtrace 把每条 trace 的 IR/snapshot/机器码完整封装,保证阶段交接的数据完整。
- **"动态"** 体现在 trace 是运行时录制、运行时安装、运行时退出、运行时可能再录 side trace——整个生命周期是数据驱动的,不依赖任何静态分析。

而这条生命周期的每一个阶段,都是后面某一章的深入。把地图列在这里,带着这张图读后面,不会迷路:

| 生命周期阶段 | 后续章节 | 讲什么 |
|---|---|---|
| **START 的录制初始化** | [P2-06 录制:字节码变 IR](P2-06-录制字节码变IR.md) | `lj_record_setup`/`lj_record_ins` 怎么把一条字节码翻成 IR,怎么处理栈帧、怎么插 guard |
| **RECORD 的产出物 IR** | P2-07 IR 与 SSA | IR 的 SSA 形式、REF_BIAS 偏移、指令格式 |
| **RECORD 的类型决策** | P2-08 类型窄化 narrowing | 怎么从动态类型推断出具体类型(整数/浮点),决定生成哪种机器指令 |
| **END 的优化 pass** | P3-09 常量折叠 fold | `lj_opt_fold`(最大的优化 pass) |
| | P3-10 内存优化与别名分析 | `lj_opt_mem`,store/load 的冗余消除 |
| | P3-11 分配消除 sink + 死代码 dce | `lj_opt_sink`/`lj_opt_dce` |
| | P3-12 循环优化 + 指令拆分 | `lj_opt_loop`/`lj_opt_split` |
| **ASM 的寄存器分配** | P4-13 线性扫描寄存器分配 | 线性扫描算法,ra 分配 |
| **ASM 的汇编生成** | P4-14 汇编生成 asm | `lj_asm_trace` 怎么把 IR 翻成 x86/arm 汇编 |
| | P4-15 后端目标与机器码内存 | 可执行内存 W^X、mcode area 管理 |
| **运行期 guard 触发** | P5-16 guard 运行时检查与 side exit | guard 在机器码里长什么样,怎么跳到 exit stub |
| **运行期 snapshot 恢复** | P5-17 snapshot 恢复解释器状态 | `lj_snap_restore` 怎么把寄存器翻译回栈 |
| **退出点再录 side trace** | P5-18 side trace:从退出点录制新 trace | side trace 的录制、`lj_asm_patchexit` 机器码热补丁 |
| **trace 之间链接** | P5-19 trace 链接(TraceLink 9 种) | 机器码跑完去哪:LOOP/ROOT/STITCH/... |

读完这一章,你应该能在脑子里画出那张"trace 一生"的状态机图,并且知道每个箭头对应 `lj_trace.c` 的哪一行、调了哪个模块、是后面哪一章的内容。这就是 JIT 侧的总纲——后面所有的深入,都是在这条生命周期的某一个阶段上钻下去。

trace 没有魔法,它只是把"动态执行变成机器码"这件事,拆成了一系列**有严格顺序、有失败兜底、有数据依赖**的状态,然后一个状态一个状态地推进。状态机的优雅之处在于:它把一件本质上很复杂的事(运行时编译动态语言),规整成了一条清晰可控的流水线。而这条流水线的第一个真实动作——把一条字节码翻译成 IR——就是下一章的主题。

---

*下一章 [P2-06 录制:字节码变 IR](P2-06-录制字节码变IR.md):状态机的 RECORD 阶段到底怎么工作。我们跟着 `lj_record_ins` 的大 switch,看解释器执行的每一条字节码,是怎么被翻译成 IR 指令、怎么插上 guard、怎么处理动态类型的。*
