# P5-18 side trace：从退出点录制新 trace

> **本书主线**：把动态执行安全变成机器码。**二分法**：解释器侧 ↔ JIT 侧。本章落在 **JIT 侧·运行时**，承接 [P5-17 snapshot 恢复解释器状态](P5-17-snapshot恢复解释器状态.md)：snapshot 把 guard 失败那一刻的状态，无损翻译回解释器能认的样子，让程序接着跑。但 snapshot 每次只解决"这一下退回去"的问题——如果一个 guard **反复**失败，反复退回解释器，那退回去的路径其实也很热，继续让解释器慢慢跑就太亏了。
>
> **本章回答的核心问题**：某个 guard 经常失败，与其每次都退回解释器、让那条失败后走的路径继续用解释器慢慢跑，能不能**从那个退出点出发，把失败后走的路径也录成一条新的 trace**，下次失败时直接跳进这条新 trace 继续跑机器码？
>
> 能。这就是 side trace(侧轨迹)。它是 LuaJIT"越跑越快"的根源——程序跑得越久，trace 树长得越完整，退回解释器的次数越来越少。
>
> **★对照**:JVM/V8 的"重新编译/分层编译" vs LuaJIT 的 side trace。
> **源码**:LuaJIT 2.1.ROLLING,`lj_trace.c`(`trace_hotside` 触发入口、`trace_stop` 热补丁安装)、`lj_snap.c`(`lj_snap_replay` 从父 snapshot 重放)、`lj_record.c`(`lj_record_setup` side trace 录制初始化)、`lj_asm_x86.h`(`lj_asm_patchexit` 机器码热补丁)、`lj_jit.h`(`GCtrace` 的 root/nextroot/nextside 字段)。
> **基调**：纯直球，不用比喻；从第一性原理推导。

---

## 第一部分：这章解决什么问题(第一性原理推导)

### §1 把场景再钉死一次：P0-01 那个 if i<900000

P0-01 §13 留了一个尾巴，本章就是来把它彻底讲透的。先把那个例子原样搬过来：

```lua
local x = 0
for i = 1, 1000000 do
  if i < 900000 then
    x = x + 1       -- 这条路,跑了 90 万次
  else
    x = x - 1       -- 这条路,只跑了 10 万次
  end
end
```

P0-01 §10 讲过，LuaJIT 是 trace JIT，只录实际在走的那条线性路径。这个循环跑起来，头 90 万次走的是 `x = x + 1` 这条路。所以 LuaJIT 录的第一条 trace(root trace)，录的是这条主流路径：

```
循环回边 → 检查 i<900000(这是 guard) → 若成立:x=x+1 → 回到循环开头
```

这条 root trace 编译成机器码后，头 90 万次循环都跑它，飞快。

但 `if i < 900000` 这个判断是 trace 里的一个 guard。第 90 万次往后，i 不再小于 900000,guard 失败。失败的瞬间，P5-16/P5-17 讲的整套机制启动：guard 跳到 exit stub → exit stub 把寄存器倒进 ExitState → `lj_snap_restore` 按 snapshot 把状态恢复成解释器的样子 → 解释器接管，从 `if` 失败的地方接着跑 `x = x - 1`。

到这里，P5-16/P5-17 已经把"这一次失败怎么办"讲清了。**问题是，这个循环还会失败 10 万次**。

### §2 反复失败才是真正的痛点

第 90 万次失败，退回解释器，跑了一轮 `x = x - 1`，回到循环开头，下一轮 i 还是大于等于 900000,guard **再次失败**，再退回解释器……如此往复 10 万次。

这 10 万次，每次都要走一遍 side exit 的完整流程：exit stub 存寄存器、`lj_snap_restore` 恢复栈、解释器 dispatch 重新理解 `x = x - 1` 这条字节码。每一次都付出退出恢复的开销 + 解释执行的开销。

而这 10 万次走的路径——`x = x - 1`——是**完全相同**的一条线性路径。它跑 10 万次，够不够热？够。它的所有变量(i、x)类型都很稳定(整数)，能不能编译？能。它和 root trace 录的那条路有什么本质区别？没有，只是另一条线性热路径。

那为什么不让它也变成一条 trace?

这正是 side trace 要解决的问题：**从那个反复失败的 guard 退出点出发，把"失败后走的路径"也录成一条新的 trace，下次再失败，直接跳进这条新 trace 继续跑机器码，不再退回解释器**。

### §3 为什么不能直接"再录一条 root trace"

你可能会想：既然失败后走的路径也是热路径，那等它热了，像录 root trace 那样从某个循环回边触发录制，再录一条不就行了？

不行。关键在于**起点的位置不一样**。

root trace 的起点是**循环回边**(或函数调用)。它录的是"从循环开头到循环末尾"这一整圈。hotcount 机制(P0-01 §6)就是配在循环回边上的——回边每跑一次，计数器加一，到了阈值就触发录制。

但 side trace 想要的起点不是循环开头，而是 **guard 退出点**。`if i < 900000` 失败的那一刻，程序的位置是"刚判定完条件、要走 else 分支"。从这里接着走，走到下一次循环回边，这才是失败后走的那条路。如果硬要从循环开头录，那录的又是一条"检查 i<900000，成立则 x=x+1"的路径——和原来的 root trace 一模一样，没解决任何问题。

所以 side trace 的录制，必须从 **guard 退出点**起步，而不是从循环回边起步。这是一个全新的起点，root trace 的触发机制(hotcount 配在回边上)管不到它。它需要一套自己的触发机制、自己的起点状态准备。这就是本章要讲的全部内容。

### §4 三个子问题

把"从退出点录一条新 trace"这件事拆开，有三个子问题必须回答：

1. **什么时候触发？** 怎么知道一个 guard 失败得够多、值得为它录一条 side trace?(不能失败一次就录，那太冲动；也不能失败太多次才录，那中间太亏。)
2. **起点状态从哪来？** root trace 录制时，起点状态(各个变量的初值)是从解释器栈 load 进来的。但 side trace 的起点是 guard 退出点，这个点的状态在机器码的寄存器里——它怎么变成新 trace 录制能用的 IR?
3. **录好的新 trace 怎么接上？** 录完一条 side trace，下次那个 guard 再失败，怎么让它"直接跳进 side trace"而不是又退回解释器？机器码已经生成了，要改它的跳转目标，这是**热补丁**——改正在执行的机器码。

这三个问题，分别对应源码里的三个机制：`trace_hotside`(触发)、`lj_snap_replay`(起点状态)、`lj_asm_patchexit`(热补丁)。本章按这个顺序讲下来。

在动源码之前，先用第一性原理把这三个机制各自**为什么必须这么做**想清楚。

### §5 子问题一：触发——为什么是"数退出次数"

第一个问题：什么时候该为某个 guard 录一条 side trace?

直觉上，一个 guard 失败了，我们想知道"它是不是经常失败"。最直接的办法：**给每个 guard 配一个计数器，每失败一次就加一，到了阈值就录**。

这和 root trace 的 hotcount 是一回事——都是"数次数，到了阈值就动手"。区别只在于：hotcount 数的是**循环回边**跑的次数(配在回边上)，而 side trace 数的是**某个 guard 退出**的次数(配在 guard 上)。

那这个计数器存哪？它得是"每个 guard 一个"。回忆 P5-17 §6 讲的 snapshot 结构：每个 guard 对应一张 snapshot,snapshot 头部 `SnapShot` 里有个字段 `count`(`lj_jit.h:187`):

```c
uint8_t count;	/* Count of taken exits for this snapshot. */
```

这个字段就是给 side trace 准备的。一个 guard 每失败一次(走一次 side exit)，它对应的 snapshot 的 `count` 就加一。等 `count` 到了阈值(默认 `hotexit = 10`,`lj_jit.h:117`)，就触发从这个 guard 录一条 side trace。

为什么阈值是 10 而不是 root trace 的 hotloop(默认 56)？因为 side trace 的录制成本更低(它不用从头分析循环结构，起点状态现成)，而且一旦失败过 10 次，基本能确定这条路径稳定地热。阈值低一点，早点动手，少亏几次。

这里还有个细节：什么时候去检查 `count`？显然不能在 guard 失败、刚跳到 exit stub 的时候(那会儿在汇编里，不好做复杂逻辑)。正确的时机是：**每次走完 side exit、`lj_snap_restore` 恢复完、控制权回到 C 里的 `lj_trace_exit` 时**，顺手检查一下刚才退出的那个 guard 的 `count`，够了就触发录制。这样检查发生在 C 里，逻辑好写，又不影响热路径(guard 成立时根本不走到这)。

### §6 子问题二：起点状态——为什么必须"重放 snapshot"

第二个问题是最绕的：side trace 录制的起点状态从哪来？

回忆 root trace 的录制(P2-06 讲过)。root trace 起步时，录制器会为循环里的每个变量发射一条 `IR_SLOAD`(从栈加载)指令，把解释器栈上的值"load"进 IR，作为后续运算的输入。这些 SLOAD 就是 root trace IR 的开头。

side trace 没法照搬这套。原因有两个：

**原因一：起点不同。** root trace 从循环开头录，循环开头的变量都在解释器栈上，SLOAD 能直接 load 到。但 side trace 从 guard 退出点录，退出点那一刻的变量状态，是 root trace 机器码运行到中途算出来的中间结果——这些值此刻在寄存器/spill 里，不在解释器栈上。SLOAD 加载不到。

**原因二：这些中间结果，有些是 root trace 优化出来的"虚拟值"。** 比如 root trace 做了 sink 优化(P3-11)，某个 `t = {}` 没有真正分配，snapshot 里记的是"这是个 sunk 分配"。side trace 如果要用这个 t，不能简单地 SLOAD，得知道它是个 sunk 分配、该现场重建。

所以 side trace 的起点状态，不能靠 SLOAD，只能靠**重放 root trace 在那个退出点的 snapshot**。

P5-17 已经把 snapshot 讲透了：snapshot 是一张"slot → IR ref"的表，完整描述了退出点那一刻的状态。它记的不是值本身(值要运行时才有)，而是"每个 slot 的值从哪条 IR ref 来"。这套信息，不仅退出恢复时用(`lj_snap_restore`),**side trace 录制时也能用**——只要把这张表"翻译"成新 trace 的 IR 起点就行。

怎么翻译？对 snapshot 里的每一条(slot → ref):

- 如果 ref 是常量：直接在新 trace 里重放这个常量(`snap_replay_const`)。
- 如果 ref 对应一条普通的 root trace IR(比如某个加法的结果)：新 trace 不能引用 root trace 的 IR(它们是两条独立的 trace)，但它可以发射一条**特殊指令** `IR_PVAL`(parent value，父值引用)，说"这个 slot 的值，等于父 trace 的第 N 条 IR 的结果"。后端代码生成时，PVAL 会变成"从 ExitState 里取这个寄存器/spill 的值"。
- 如果 ref 对应一个 sunk 分配：要在新 trace 里把这个分配**重新发射一遍**(TNEW + 所有 sunk store)，让 side trace 自己也持有一个(语义等价的)对象。

这就是 `lj_snap_replay` 干的事：**遍历父 trace 退出点的 snapshot，把每条 slot→ref 映射，翻译成新 trace 的 IR 起点指令**。重放完，新 trace 的 IR 开头就有了正确的初值，接下来就和 root trace 一样，继续往下录新的字节码路径。

这里有个精妙之处：snapshot 这张表，**一物两用**。退出恢复时，它是"翻译表"，帮 `lj_snap_restore` 把寄存器值搬到解释器栈；side trace 录制时，它是"起点蓝图"，帮 `lj_snap_replay` 把退出点状态搭成新 IR。同一张表，两种消费者，因为 snapshot 完整且精确地描述了退出点状态——谁能用谁用。P5-17 §17 已经预告过这一点，本章把它讲透。

### §7 子问题三：热补丁——为什么必须改机器码

第三个问题：side trace 录好了、编译成机器码了，下次那个 guard 再失败，怎么让它跳进 side trace?

最朴素的想法：guard 失败 → 退回解释器 → 解释器发现"哦这个退出点有 side trace" → 跳进 side trace。但这又绕了一圈 side exit 的开销(exit stub 存寄存器、`lj_snap_restore` 恢复栈、解释器 dispatch)，省下的时间被这套流程吃掉一半。

更好的办法：**直接改 root trace 的机器码，把那个 guard 失败时的跳转目标，从"退回解释器"改成"跳进 side trace"**。这样 guard 失败的瞬间，机器码自己就跳进 side trace，根本不经过解释器，也不走完整的 side exit 恢复流程(因为 side trace 的入口状态，正好就是退出点状态，机器码到机器码，无缝衔接)。

这就是**机器码热补丁**(hot patching)：一段已经生成、甚至可能正在被执行的机器码，被原地修改，改的是某条跳转指令的目标地址。

这件事听起来危险(改正在跑的代码？)，但其实是 sound 的，因为：

1. **只改跳转目标，不改指令逻辑。** root trace 的机器码本体(那些加法、表访问指令)一个字节都不动，只动 guard 失败时那条条件跳转指令的目标地址。
2. **改的时候，这段机器码不在跑。** 补丁发生在 side trace 编译完成的瞬间，此时控制权在 C 里(`trace_stop`),root trace 的机器码没在执行(解释器正停在 `lj_trace_ins` 的状态机里)。补丁打完，下次再跑到 root trace 的机器码，看到的就是新目标。
3. **目标合法。** 新目标(side trace 的 mcode 入口)期望的入口状态，正是退出点状态(由 snapshot 保证)。所以跳进去就能正确执行。

具体的补丁机制，源码里是 `lj_asm_patchexit`。它干的是：扫描 root trace 的机器码，找到所有跳向"这个 guard 的 exit stub"的条件跳转指令，把它们的跳转偏移量改成指向 side trace 入口。这是机器码级别的 pattern matching + 原地改写，本章第三部分会贴出 x86 后端的实现逐行讲。

### §8 把三个机制合起来：side trace 的完整一生

把 §5、§6、§7 合起来，side trace 从无到有到生效，完整的一生是这样的：

```
阶段 0:root trace 在跑,某个 guard 偶尔失败,每次退回解释器。
        每次失败,这个 guard 的 snapshot->count 加 1。

阶段 1(触发):某次失败后,count 达到 hotexit(10)。
        trace_hotside 决定:为这个 guard 录一条 side trace。

阶段 2(重放起点):lj_snap_replay 遍历父 trace 这个 guard 的 snapshot,
        把 slot→ref 映射翻译成新 trace 的 IR 起点(PVAL/常量/sunk 重建)。

阶段 3(录制):从重放的起点开始,像 root trace 一样,
        解释器每跑一条字节码,录制器就翻译一条 IR,直到遇到循环回边或退出。
        这条 side trace 自己也带 guard,自己也有 snapshot。

阶段 4(编译):优化 + 寄存器分配 + 汇编生成,产出 side trace 的 mcode。

阶段 5(热补丁安装):trace_stop 调 lj_asm_patchexit,
        改父 trace 的机器码,把那个 guard 的跳转目标改成 side trace 入口。
        父 trace 的 snapshot->count 置为 SNAPCOUNT_DONE(255),表示"已链接"。

阶段 6(生效):此后那个 guard 再失败,机器码直接跳进 side trace,
        跑 side trace 的机器码,不再退回解释器。
```

从阶段 0 到阶段 6，失败路径从"每次退回解释器"变成了"每次跳进 side trace 跑机器码"。这就是"越跑越快"的具体含义：**热路径越长成完整的树，退回解释器的次数越少**。

### §9 用那个例子演示：两棵 trace

回到 §1 的例子，看 side trace 装上后，程序实际怎么跑。

**没有 side trace 时(只有 root trace)**:

- 第 1–89 万次循环：走 root trace(机器码),guard `i<900000` 成立，飞快。
- 第 90 万次：guard 失败，side exit,snapshot 恢复，退回解释器，解释器跑 `x = x - 1`。
- 第 90 万零 1 次到 100 万次：每次循环都走 root trace → guard 失败 → 退解释器 → 跑 `x = x - 1` → 回循环。10 万次 side exit + 10 万次解释执行，慢。

**有了 side trace 后**:

- 第 1–89 万次：走 root trace，飞快。
- 第 90 万次：guard 失败，count 还没到 10，退回解释器(和没 side trace 时一样)。
- ……又失败了 9 次，count 到 10。第 10 次失败后，`trace_hotside` 触发，开始录制 side trace:`lj_snap_replay` 重放起点状态 → 录制 `x = x - 1` 这条路径 → 编译 → `lj_asm_patchexit` 热补丁。
- 第 11 次起：guard 失败，机器码**直接跳进 side trace**,side trace 跑完 `x = x - 1` 后……它自己也有个出口，如果出口接回 root trace 的循环开头(TraceLink,P5-19 讲)，那就 side trace → root trace → 又到 guard → 又跳 side trace，形成 root + side 两棵 trace 织成的环，全部机器码，不退解释器。

这就是 LuaJIT 对这个循环的最终形态：**一棵 root trace(主流)+ 若干 side trace(每个常失败的 guard 一条)，组成一棵 trace 树，树上所有路径都是机器码**。剩下 9999890 次循环，几乎全在机器码里飞奔。

讲清了原理，下面进源码，把这三个机制逐一印证。

---

## 第二部分：源码怎么实现

### §10 触发入口：trace_hotside

side trace 的触发，发生在 side exit 恢复之后。回忆 P5-17 §11:`lj_trace_exit` 是 exit stub 跳进 C 后的总入口，它调 `lj_snap_restore` 恢复状态，恢复完，解释器准备接管。就在"解释器接管之前"这个空档，`lj_trace_exit` 顺手检查一下：刚才退出的那个 guard，该不该为它录 side trace?

这个检查在 `lj_trace_exit` 的末尾(`lj_trace.c:940`):

```c
  } else if (LJ_HASPROFILE && (G(L)->hookmask & HOOK_PROFILE)) {
    /* Just exit to interpreter. */
  } else if (G(L)->gc.state == GCSatomic || G(L)->gc.state == GCSfinalize) {
    if (!(G(L)->hookmask & HOOK_GC))
      lj_gc_step(L);  /* Exited because of GC: drive GC forward. */
  } else if ((J->flags & JIT_F_ON)) {
    trace_hotside(J, pc);
  }
```

前面几个分支是"不录 side trace"的特殊情况：profile 模式下只退解释器不录；GC 在 atomic/finalize 阶段时优先推进 GC。正常情况(JIT 开着、没特殊状态)，就调 `trace_hotside`。

`trace_hotside` 是个 `static` 函数(`lj_trace.c:798`)，很短：

```c
798	/* Check for a hot side exit. If yes, start recording a side trace. */
799	static void trace_hotside(jit_State *J, const BCIns *pc)
800	{
801	  SnapShot *snap = &traceref(J, J->parent)->snap[J->exitno];
802	  if (!(J2G(J)->hookmask & (HOOK_GC|HOOK_VMEVENT)) &&
803	      isluafunc(curr_func(J->L)) &&
804	      snap->count != SNAPCOUNT_DONE &&
805	      ++snap->count >= J->param[JIT_P_hotexit]) {
806	    lj_assertJ(J->state == LJ_TRACE_IDLE, "hot side exit while recording");
807	    /* J->parent is non-zero for a side trace. */
808	    J->state = LJ_TRACE_START;
809	    lj_trace_ins(J, pc);
810	  }
811	}
```

逐行读，正好印证 §5 推导的每一点：

**第 801 行**:`SnapShot *snap = &traceref(J, J->parent)->snap[J->exitno]`。取出"刚才退出的那个 guard"对应的 snapshot。`J->parent` 是父 trace 号(由 `lj_trace_exit` 在前面设置，见 §16),`J->exitno` 是退出号——P5-17 §12 讲过，snapno == exitno，第 N 个 guard 退出就用第 N 张 snapshot。所以这一行定位到的，就是刚失败的那个 guard 的 snapshot。

**第 802–803 行**：前置检查。不能在 GC hook 或 vmevent 期间录(那些是全局状态，插不进录制)；当前必须是 Lua 函数(`isluafunc`)，不能是 C 函数(C 函数没法录)。

**第 804 行**:`snap->count != SNAPCOUNT_DONE`。这是关键的一道闸：`SNAPCOUNT_DONE`(255,`lj_jit.h:190`)表示这个退出点**已经**链接了一条 side trace(§13 讲 `trace_stop` 时会看到它怎么被置成 255)。已经链接过就不能再录——一个 guard 只配一条 side trace。这一道闸防止重复录制。

**第 805 行**:`++snap->count >= J->param[JIT_P_hotexit]`。这是核心：**先给 count 加一，再和阈值比**。`JIT_P_hotexit` 默认 10(`lj_jit.h:117`:_(`\007, hotexit, 10)`)。注意是 `++` 前缀自增，所以这次失败也算进去。等 count 累计到 10，条件成立。

**第 806 行**:`lj_assertJ(J->state == LJ_TRACE_IDLE, ...)`。断言当前 JIT 状态是 IDLE(没在录别的 trace)。注释"hot side exit while recording"说明：如果在录制中又触发热 side exit，是 bug(录制是单线程的，不能并发)。

**第 807–809 行**：条件全满足，启动录制。把 `J->state` 设成 `LJ_TRACE_START`，然后调 `lj_trace_ins(J, pc)` 进入录制状态机。注意 `J->parent` 这里**不重新设**——注释"J->parent is non-zero for a side trace"提醒：它已经在 `lj_trace_exit` 里被设好了(指向父 trace)，这里直接用。这就是 side trace 和 root trace 的根本区别：**root trace 的 `J->parent = 0`,side trace 的 `J->parent != 0`**，整个录制流程靠这一个字段区分两条路径(§12 会看到 `lj_record_setup` 怎么用它分流)。

`pc` 是传给 `lj_trace_ins` 的起点 PC。对 side trace，这个 pc 是退出后解释器要接着执行的字节码地址——正是 side trace 要录的起点。

### §11 JIT 参数：hotexit / tryside / maxside

§10 出现了三个参数，值得集中讲一下，因为它们直接决定 side trace 的行为边界。它们都在 `lj_jit.h:108` 的 `JIT_PARAMDEF` 宏里：

```c
108	#define JIT_PARAMDEF(_) \
109	  _(\010, maxtrace,	1000)	/* Max. # of traces in cache. */ \
110	  _(\011, maxrecord,	4000)	/* Max. # of recorded IR instructions. */ \
...
112	  _(\007, maxside,	100)	/* Max. # of side traces of a root trace. */ \
113	  _(\007, maxsnap,	500)	/* Max. # of snapshots for a trace. */ \
...
116	  _(\007, hotloop,	56)	/* # of iter. to detect a hot loop/call. */ \
117	  _(\007, hotexit,	10)	/* # of taken exits to start a side trace. */ \
118	  _(\007, tryside,	4)	/* # of attempts to compile a side trace. */ \
```

逐个解释：

- **`hotloop = 56`**:root trace 的 hotcount 阈值(循环/调用跑 56 次触发录制)。注意这是 root trace 的，P0-01 §6 讲过。
- **`hotexit = 10`**:side trace 的阈值——一个 guard 退出 10 次就为它录 side trace。比 hotloop 低，因为 side trace 录制更便宜、且失败 10 次基本能确定稳定。
- **`tryside = 4`**：为同一个 guard **尝试**录 side trace 的次数。为什么要"尝试"而不是"一定录成"？因为录制可能失败(比如碰到 NYI 字节码、类型不稳定)。失败一次，`penalty_pc` 给这个退出点加惩罚(提高下次触发的阈值)；但如果还在 tryside 次以内，允许再试。超过 tryside 次，这个 guard 被打入冷宫(`count` 锁死，不再尝试)。
- **`maxside = 100`**：一棵 trace 树最多挂 100 条 side trace。防止某个 root trace 的 guard 太多、side trace 爆炸式增长把内存吃光。到了 100，新退出点直接退解释器，不再录。

这四个参数合起来，是 side trace 的"开关与闸门"：什么算够热(hotexit)、允许试几次(tryside)、最多挂多少(maxside)、整体最多多少 trace(maxtrace)。它们都是可在运行时通过 `jit.opt` 调的，默认值是 Mike Pall 经验调出来的平衡点。

### §12 side trace 录制的分流：lj_record_setup

`trace_hotside` 把 `J->state` 设成 `LJ_TRACE_START` 后调 `lj_trace_ins`，后者进入录制状态机 `trace_state`。状态机在 `LJ_TRACE_START` 状态下调 `trace_start`(`lj_trace.c:419`),`trace_start` 做完 trace 号分配、`J->cur` 清零等初始化，最后调 `lj_record_setup`(`lj_record.c:2823`)——这才是真正"开始录"的地方。

`lj_record_setup` 的关键，是它靠 `J->parent` 分流 root 和 side 两条路径：

```c
2822	/* Setup for recording a new trace. */
2823	void lj_record_setup(jit_State *J)
2824	{
2825	  uint32_t i;
2826	
2827	  /* Initialize state related to current trace. */
2828	  memset(J->slot, 0, sizeof(J->slot));
2829	  memset(J->chain, 0, sizeof(J->chain));
2830	#ifdef LUAJIT_ENABLE_TABLE_BUMP
2831	  memset(J->rbchash, 0, sizeof(J->rbchash));
2832	#endif
2833	  memset(J->bpropcache, 0, sizeof(J->bpropcache));
2834	  J->scev.idx = REF_NIL;
2835	  setmref(J->scev.pc, NULL);
2836	
2837	  J->baseslot = 1+LJ_FR2;  /* Invoking function is at base[-1-LJ_FR2]. */
2838	  J->base = J->slot + J->baseslot;
2839	  J->maxslot = 0;
2840	  J->framedepth = 0;
2841	  J->retdepth = 0;
2842	...
2851	  /* Emit instructions for fixed references. Also triggers initial IR alloc. */
2852	  emitir_raw(IRT(IR_BASE, IRT_PGC), J->parent, J->exitno);
2853	...
2862	  J->startpc = J->pc;
2863	  setmref(J->cur.startpc, J->pc);
2864	  if (J->parent) {  /* Side trace. */
2865	    GCtrace *T = traceref(J, J->parent);
2866	    TraceNo root = T->root ? T->root : J->parent;
2867	    J->cur.root = (uint16_t)root;
2868	    J->cur.startins = BCINS_AD(BC_JMP, 0, 0);
2869	    /* Check whether we could at least potentially form an extra loop. */
2870	    if (J->exitno == 0 && T->snap[0].nent == 0) {
2871	      /* We can narrow a FORL for some side traces, too. */
2872	      if (J->pc > proto_bc(J->pt) && bc_op(J->pc[-1]) == BC_JFORI &&
2873		  bc_d(J->pc[bc_j(J->pc[-1])-1]) == root) {
2874		lj_snap_add(J);
2875		rec_for_loop(J, J->pc-1, &J->scev, 1);
2876		goto sidecheck;
2877	      }
2878	    } else {
2879	      J->startpc = NULL;  /* Prevent forming an extra loop. */
2880	    }
2881	    lj_snap_replay(J, T);
2882	  sidecheck:
2883	    if ((traceref(J, J->cur.root)->nchild >= J->param[JIT_P_maxside] ||
2884		 T->snap[J->exitno].count >= J->param[JIT_P_hotexit] +
2885					     J->param[JIT_P_tryside])) {
2886	      if (bc_op(*J->pc) == BC_JLOOP) {
2887		BCIns startins = traceref(J, bc_d(*J->pc))->startins;
2888		if (bc_op(startins) == BC_ITERN)
2889		  rec_itern(J, bc_a(startins), bc_b(startins));
2890	      }
2891	      lj_record_stop(J, LJ_TRLINK_INTERP, 0);
2892	    }
2893	  } else {  /* Root trace. */
2894	    J->cur.root = 0;
2895	    J->cur.startins = *J->pc;
2896	    J->pc = rec_setup_root(J);
2897	...
2901	    lj_snap_add(J);
2902	...
2907	  }
```

只看和 side trace 相关的部分(`if (J->parent)` 分支，2864–2892 行)，逐段：

**第 2852 行**(在分支之前，但和 side 强相关):`emitir_raw(IRT(IR_BASE, IRT_PGC), J->parent, J->exitno)`。发射的第一条 IR 是 `IR_BASE`，它的 op1 存 `J->parent`、op2 存 `J->exitno`。这条 IR 本身不生成机器码(它是个标记)，但后端用它知道"这条 trace 的父是谁、从哪个退出点来"——链接(P5-19)和 patchexit 都要靠它。

**第 2864–2867 行**：取出父 trace `T`。然后算 root:`TraceNo root = T->root ? T->root : J->parent`。这里处理的是**side trace 的 side trace**——如果父 trace T 自己也是一条 side trace(它的 `T->root` 非 0)，那新 trace 的 root 就继承爷爷的 root；如果 T 是 root trace(`T->root == 0`)，那 root 就是 T 自己。这样保证：不管 side 多深，`J->cur.root` 总是指向这棵 trace 树的根。

**第 2868 行**:`J->cur.startins = BCINS_AD(BC_JMP, 0, 0)`。side trace 的"起始字节码"记成一条 `BC_JMP`。这是个虚构的标记——root trace 的 startins 是真实的起始字节码(BC_FORL/BC_LOOP 等，用来后面 patch 字节码)，但 side trace 不从字节码起步，它的"起点"是父 trace 的退出点，所以用 BC_JMP 占位。这个占位至关重要：`trace_stop` 看到 startins 是 BC_JMP，就知道"这是 side trace，要走 patchexit 路径"(§13 会看到)。

**第 2870–2880 行**：一个优化尝试。某些情况下，side trace 也能形成自己的小循环(比如退出点正好在 FORL 附近)。这里检查能不能复用父 trace 的 FORL 窄化信息(`rec_for_loop`)。能就用，不能就把 `J->startpc = NULL` 防止后续误判。这是边角优化，不影响主流程。

**第 2881 行**:`lj_snap_replay(J, T)`。**这是 side trace 起点状态的核心**——下一节专门讲。它遍历父 trace 的 snapshot，把状态重放成新 IR，填进 `J->slot[]`。重放完，新 trace 的"初值"就齐了。

**第 2882–2892 行**(`sidecheck`)：一道安全阀。重放完，检查要不要**立即放弃录制**。条件是二选一：

- `traceref(J, J->cur.root)->nchild >= maxside`：这棵树已经挂够了 100 条 side trace。
- `T->snap[J->exitno].count >= hotexit + tryside`：这个退出点的 count 已经超过 `10 + 4 = 14`(默认)，说明前面已经试过几次都失败了(每次失败 penalty 会把 count 往上推)，别再试了。

任一条件成立，直接 `lj_record_stop(J, LJ_TRLINK_INTERP, 0)`——停止录制，链接类型是 `LJ_TRLINK_INTERP`(退回解释器)。这是一种"放弃但优雅退出"的机制：实在录不成的 side trace，不让它白费力气，直接落回解释器保底。

注意 `lj_record_stop` 的语义(`lj_record.c:299`)：它不直接结束录制流程，而是设好链接类型(`LJ_TRLINK_INTERP`)、调 `lj_snap_add` 拍最后一张 snapshot、设 `J->state = LJ_TRACE_END`，然后让状态机往下走到 ASM、再到 `trace_stop`。所以"放弃"的 side trace 也会走完编译流程，只是它的链接是退解释器而不是跳别的 trace。

### §13 起点 state 的重放：lj_snap_replay

这是本章最核心的源码。`lj_snap_replay`(`lj_snap.c:507`)把父 trace 退出点的 snapshot，翻译成新 trace 的 IR 起点：

```c
507	/* Replay snapshot state to setup side trace. */
508	void lj_snap_replay(jit_State *J, GCtrace *T)
509	{
510	  SnapShot *snap = &T->snap[J->exitno];
511	  SnapEntry *map = &T->snapmap[snap->mapofs];
512	  MSize n, nent = snap->nent;
513	  BloomFilter seen = 0;
514	  int pass23 = 0;
515	  J->framedepth = 0;
516	  /* Emit IR for slots inherited from parent snapshot. */
517	  for (n = 0; n < nent; n++) {
518	    SnapEntry sn = map[n];
519	    BCReg s = snap_slot(sn);
520	    IRRef ref = snap_ref(sn);
521	    IRIns *ir = &T->ir[ref];
522	    TRef tr;
523	    /* The bloom filter avoids O(nent^2) overhead for de-duping slots. */
524	    if (bloomtest(seen, ref) && (tr = snap_dedup(J, map, n, ref)) != 0)
525	      goto setslot;
526	    bloomset(seen, ref);
527	    if (irref_isk(ref)) {
528	      /* See special treatment of LJ_FR2 slot 1 in snapshot_slots() above. */
529	      if (LJ_FR2 && (sn == SNAP(1, SNAP_FRAME | SNAP_NORESTORE, REF_NIL)))
530		tr = 0;
531	      else
532		tr = snap_replay_const(J, ir);
533	    } else if (!regsp_used(ir->prev)) {
534	      pass23 = 1;
535	      lj_assertJ(s != 0, "unused slot 0 in snapshot");
536	      tr = s;
537	    } else {
538	      IRType t = irt_type(ir->t);
539	      uint32_t mode = IRSLOAD_INHERIT|IRSLOAD_PARENT;
540	      if (LJ_SOFTFP32 && (sn & SNAP_SOFTFPNUM)) t = IRT_NUM;
541	      if (ir->o == IR_SLOAD) mode |= (ir->op2 & IRSLOAD_READONLY);
542	      if ((sn & SNAP_KEYINDEX)) mode |= IRSLOAD_KEYINDEX;
543	      tr = emitir_raw(IRT(IR_SLOAD, t), s, mode);
544	    }
545	  setslot:
546	    /* Same as TREF_* flags. */
547	    J->slot[s] = tr | (sn&(SNAP_KEYINDEX|SNAP_CONT|SNAP_FRAME));
548	    J->framedepth += ((sn & (SNAP_CONT|SNAP_FRAME)) && (s != LJ_FR2));
548a	    if ((sn & SNAP_FRAME))
549	      J->baseslot = s+1;
550	  }
```

(为可读性，部分行号加了 a/b 后缀；实际源码连续。)逐段拆这个第一趟(pass 1):

**第 510–511 行**：定位父 trace 的退出 snapshot。`T->snap[J->exitno]` 是退出点那张，`snapmap[snap->mapofs]` 是它的条目起点——和 `lj_snap_restore` 里取的是**同一张表**(P5-17 §12)，印证"snapshot 一物两用"。

**第 517 行**：主循环，遍历 snapshot 的每一条 slot 条目。`nent` 是压缩后的条目数(P5-17 §8 讲过差量压缩)。

**第 518–522 行**：解出每条 SnapEntry 的三段：slot 号 `s`、IR ref `ref`、父 trace 里这条 ref 对应的 IR 指令 `ir`。注意 `ir = &T->ir[ref]`——它读的是**父 trace** 的 IR(因为 snapshot 记的 ref 是父 trace 的 IR 下标)。

**第 523–526 行**：去重。同一个 ref 可能在多个 slot 出现(比如两个 slot 都指向父 trace 的同一个常量)。Bloom filter `seen` 快速判断"这个 ref 之前见过没"；见过就走 `snap_dedup` 复用之前的结果。注释明确说："avoid O(nent^2) overhead"——朴素去重是平方复杂度，Bloom filter 把它降到线性。

**第 527–532 行**:**分支 A——常量**。`irref_isk(ref)` 判定 ref 是常量(常量 ref < REF_BIAS)。调 `snap_replay_const`(§14 详讲)在新 trace 里重放这个常量。FR2 模式下 slot 1 的特殊帧标记(REF_NIL + 特殊 flag)被单独处理成 `tr = 0`(空)。

**第 533–536 行**:**分支 B——父 trace 里这条 IR "没被分配位置"**。`!regsp_used(ir->prev)`(P5-17 §13 讲过 RegSP)说明父 trace 里这条 IR 是死的、或是个纯中间结果。这种情况下，先记下来(`pass23 = 1`，留给第二/三趟处理)，暂且把 `tr = s`(用 slot 号占位，后面会替换)。

**第 537–544 行**:**分支 C——普通值**。这是最常见的情况。发射一条 `IR_SLOAD` 指令，但 mode 带两个特殊标志：`IRSLOAD_INHERIT | IRSLOAD_PARENT`。这两个标志告诉后端："这个 SLOAD 不是真的去解释器栈 load，而是从父 trace 的退出状态(ExitState)继承这个值"。具体地：

- `IRSLOAD_PARENT`(0x01,`lj_ir.h:233`):"Coalesce with parent trace"——和父 trace 合并，值来自父的退出寄存器/spill。
- `IRSLOAD_INHERIT`(0x20,`lj_ir.h:238`):"Inherited by exits/side traces"——这个 slot 被后续退出/side trace 继承。

  后端代码生成时，带 PARENT 的 SLOAD 不发 load 指令，而是发"从 ExitState 的某个寄存器/spill 取值"的指令(具体位置由 `lj_snap_regspmap` 算，见 §15)。这就是 §6 说的"PVAL/从 ExitState 取"的实现——只不过对 slot 这种简单情况，LuaJIT 复用 SLOAD 指令加标志位，而不是单独搞个 PVAL。

  附带的 mode 位：`IRSLOAD_READONLY`(如果父 trace 里这条 SLOAD 是只读的，继承下来)、`IRSLOAD_KEYINDEX`(表遍历 key index，特殊 tag)。

**第 545–550 行**(`setslot`)：把算出的 `tr`(新 trace 的 IR ref)写进 `J->slot[s]`，带上原 snapshot 的标志位(KEYINDEX/CONT/FRAME)。同时维护 `framedepth`(帧深度)和 `baseslot`(当前帧底)。这样录制的"栈状态"就和退出点对齐了。

第一趟跑完，大多数 slot 都有了对应的新 IR(SLOAD_PARENT 或常量)。剩下两类"麻烦"的——父 trace 里没分配位置的(分支 B)和 sunk 分配——留给 pass2/pass3。

### §14 重放的边角：常量与 sunk 分配

§13 的 pass1 有两个分支没展开，这里补上。

**常量重放：`snap_replay_const`**(`lj_snap.c:445`):

```c
445	/* Replay constant from parent trace. */
446	static TRef snap_replay_const(jit_State *J, IRIns *ir)
447	{
448	  /* Only have to deal with constants that can occur in stack slots. */
449	  switch ((IROp)ir->o) {
450	  case IR_KPRI: return TREF_PRI(irt_type(ir->t));
451	  case IR_KINT: return lj_ir_kint(J, ir->i);
452	  case IR_KGC: return lj_ir_kgc(J, ir_kgc(ir), irt_t(ir->t));
453	  case IR_KNUM: case IR_KINT64:
454	    return lj_ir_k64(J, (IROp)ir->o, ir_k64(ir)->u64);
455	  case IR_KPTR: return lj_ir_kptr(J, ir_kptr(ir));  /* Continuation. */
456	  case IR_KNULL: return lj_ir_knull(J, irt_type(ir->t));
457	  default: lj_assertJ(0, "bad IR constant op %d", ir->o); return TREF_NIL;
458	  }
459	}
```

逻辑很直白：对父 trace 里的每种常量 IR(KPRI/KINT/KGC/KNUM/KINT64/KPTR/KNULL)，在新 trace 里**重新发射一个等价的常量**。新 trace 不能直接引用父 trace 的 IR(独立的两条 trace,IR 数组分开)，所以得自己造一份。造出来的常量值和父 trace 的完全一致(从父 IR 读出来再发射)，语义等价。

注意"Only have to deal with constants that can occur in stack slots"——只处理能出现在栈槽里的常量类型。其他常量(比如内部用的 K64)不会出现在 snapshot 里，不需要处理。

**Sunk 分配的重放：pass2/pass3**。这是最绕的部分，但原理在 P5-17 §14 已经讲过(sunk 分配的 unsink)。这里只是把"退出时 unsink"换成"录制时重放"。

回忆：sink 优化(P3-11)把 trace 里的 `t = {}` 推迟到退出时才真正分配。如果父 trace 有个 sunk 的 `t = {}`,side trace 想用这个 t，不能简单 SLOAD——它得在新 trace 里**重新发射这个分配 + 所有 sunk store**，让 side trace 自己持有一个(语义等价的)t。

这就是 `lj_snap_replay` 的 pass2(pass23=1 时执行)和 pass3(`lj_snap.c:552–682`)。核心逻辑：

```c
552	  if (pass23) {
553	    IRIns *irlast = &T->ir[snap->ref];
...
556	    /* Emit dependent PVALs. */
557	    for (n = 0; n < nent; n++) {
558	      SnapEntry sn = map[n];
559	      IRRef refp = snap_ref(sn);
560	      IRIns *ir = &T->ir[refp];
561	      if (regsp_reg(ir->r) == RID_SUNK) {
...
568	        if (irm_op1(m) == IRMref) snap_pref(J, T, map, nent, seen, ir->op1);
569	        if (irm_op2(m) == IRMref) snap_pref(J, T, map, nent, seen, ir->op2);
...
574	        IRIns *irs;
575	        for (irs = ir+1; irs < irlast; irs++)
576	          if (irs->r == RID_SINK && snap_sunk_store(T, ir, irs)) {
577	            if (snap_pref(J, T, map, nent, seen, irs->op2) == 0)
578	              snap_pref(J, T, map, nent, seen, T->ir[irs->op2].op1);
...
582	          }
583	      } else if (!irref_isk(refp) && !regsp_used(ir->prev)) {
584	        lj_assertJ(ir->o == IR_CONV && ir->op2 == IRCONV_NUM_INT, ...);
587	        J->slot[snap_slot(sn)] = snap_pref(J, T, map, nent, seen, ir->op1);
588	      }
589	    }
590	    /* Replay sunk instructions. */
591	    for (n = 0; pass23 && n < nent; n++) {
592	      SnapEntry sn = map[n];
593	      IRRef refp = snap_ref(sn);
594	      IRIns *ir = &T->ir[refp];
595	      if (regsp_reg(ir->r) == RID_SUNK) {
...
614	        } else {
615	          IRIns *irs;
616	          TRef tr = emitir(ir->ot, op1, op2);
617	          J->slot[snap_slot(sn)] = tr;
618	          for (irs = ir+1; irs < irlast; irs++)
619	            if (irs->r == RID_SINK && snap_sunk_store(T, ir, irs)) {
...
676	              tmp = emitir(irs->ot, tmp, val);
677	            }
678	        }
680	      }
681	    }
683	  }
```

pass2(557–589):**发射 sunk 分配依赖的 PVAL**。对每个 sunk 分配，先把它的操作数(分配大小、表模板等)和 sunk store 的值，用 `snap_pref` 发射成 `IR_PVAL`(父值引用)。

`snap_pref`(`lj_snap.c:471`)的逻辑：

```c
471	/* Emit parent reference with de-duplication. */
472	static TRef snap_pref(jit_State *J, GCtrace *T, SnapEntry *map, MSize nmax,
473			      BloomFilter seen, IRRef ref)
474	{
475	  IRIns *ir = &T->ir[ref];
476	  TRef tr;
477	  if (irref_isk(ref))
478	    tr = snap_replay_const(J, ir);
479	  else if (!regsp_used(ir->prev))
480	    tr = 0;
481	  else if (!bloomtest(seen, ref) || (tr = snap_dedup(J, map, nmax, ref)) == 0)
482	    tr = emitir(IRT(IR_PVAL, irt_type(ir->t)), ref - REF_BIAS, 0);
483	  return tr;
484	}
```

`IR_PVAL`(`lj_ir.h:36`:`_(PVAL, N , lit, ___)`)是"父值引用"指令——它的 op1 存"父 trace 的第 N 条 IR"(去偏移后)，告诉后端："这个值 = 父 trace 第 N 条 IR 在退出那一刻的值，运行时从 ExitState 取"。这就是 side trace 引用父 trace 中间结果的通用机制。PVAL 在后端会被 `lj_snap_regspmap` 翻译成具体的"从某寄存器/spill 取"的机器码(§15)。

pass3(591–682):**重放 sunk 分配本身**。对每个 sunk 分配(`regsp_reg(ir->r) == RID_SUNK`)，在新 trace 里重新发射：

- `IR_TNEW`/`IR_TDUP`：重新发射表分配(616 行 `emitir(ir->ot, op1, op2)`)。
- 然后扫所有属于这个分配的 sunk store(`snap_sunk_store` 判断)，重新发射这些 store(618–677 行)，把 sunk 的内容在新表上重建。
- `IR_CNEW`/`CNEWI`:FFI cdata 分配，类似处理，还涉及 HIOP(64 位在 32 位机的拆分)。

重放完，sunk 分配在 side trace 里就有了一个真实(语义等价)的对象。`irlast = &T->ir[snap->ref]`(553 行)限定扫描范围：只扫到退出 snapshot 的 ref 为止，之后的 store 不属于这个退出点。

这两个 pass 看起来复杂，本质就一句话：**把父 trace 在退出点的 sunk 状态，在 side trace 里照原样重建一遍**。重放的依据全是 snapshot + 父 IR，不依赖任何运行时值(值是后端从 ExitState 取的)。

`lj_snap_replay` 最后(684–688 行):

```c
684	  J->base = J->slot + J->baseslot;
685	  J->maxslot = snap->nslots - J->baseslot;
686	  lj_snap_add(J);
687	  if (pass23)  /* Need explicit GC step _after_ initial snapshot. */
688	    emitir_raw(IRTG(IR_GCSTEP, IRT_NIL), 0, 0);
```

设好 `J->base` 和 `J->maxslot`(从 snapshot 的 `nslots` 算出当前栈顶)，拍一张初始 snapshot(686 行 `lj_snap_add`)——这张 snapshot 是 side trace 的第 0 张，记录"side trace 起步时的状态"，后面如果 side trace 自己的 guard 失败，就靠它恢复。如果重放过 sunk 分配(有 GC 压力)，补一条 GCSTEP 让 GC 推进一下。

到这，side trace 的起点 IR 完全搭好了。接下来，录制器(从 `lj_record_ins` 开始)就和 root trace 一样，逐条字节码地往下录新路径，直到遇到循环回边或退出，调用 `lj_record_stop` 结束录制。

### §15 后端怎么落实 PVAL/SLOAD_PARENT:lj_snap_regspmap

§13/§14 反复提到"带 PARENT 的 SLOAD 和 PVAL，后端会从 ExitState 取值"。这个"取值"的依据是什么？答案在 `lj_snap_regspmap`(`lj_snap.c:407`):

```c
407	/* Copy RegSP from parent snapshot to the parent links of the IR. */
408	IRIns *lj_snap_regspmap(jit_State *J, GCtrace *T, SnapNo snapno, IRIns *ir)
409	{
410	  SnapShot *snap = &T->snap[snapno];
411	  SnapEntry *map = &T->snapmap[snap->mapofs];
412	  BloomFilter rfilt = snap_renamefilter(T, snapno);
413	  MSize n = 0;
414	  IRRef ref = 0;
415	  UNUSED(J);
416	  for ( ; ; ir++) {
417	    uint32_t rs;
418	    if (ir->o == IR_SLOAD) {
419	      if (!(ir->op2 & IRSLOAD_PARENT)) break;
420	      for ( ; ; n++) {
421	        lj_assertJ(n < snap->nent, "slot %d not found in snapshot", ir->op1);
422	        if (snap_slot(map[n]) == ir->op1) {
423	          ref = snap_ref(map[n++]);
424	          break;
425	        }
426	      }
427	    } else if (LJ_SOFTFP32 && ir->o == IR_HIOP) {
428	      ref++;
429	    } else if (ir->o == IR_PVAL) {
430	      ref = ir->op1 + REF_BIAS;
431	    } else {
432	      break;
433	    }
434	    rs = T->ir[ref].prev;
435	    if (bloomtest(rfilt, ref))
436	      rs = snap_renameref(T, snapno, ref, rs);
437	    ir->prev = (uint16_t)rs;
438	    lj_assertJ(regsp_used(rs), "unused IR %04d in snapshot", ref - REF_BIAS);
438a	  }
439	  return ir;
440	}
```

(为可读性加了 a 后缀。)这个函数在后端寄存器分配之前被调用，作用是：**把 side trace IR 开头的那些 PVAL/SLOAD_PARENT 指令，和父 trace 退出 snapshot 里的 IR ref 对上，然后把父 trace 那条 IR 的 RegSP(物理位置)拷到 side trace 这条 IR 的 `prev` 字段**。

逐段：

**第 416 行**：从头扫 side trace 的 IR。

**第 418–426 行**：遇到 `IR_SLOAD` 且带 `IRSLOAD_PARENT` 标志。这种 SLOAD 的 op1 是 slot 号。在父 trace 的 snapshot 里找到同一个 slot(`snap_slot(map[n]) == ir->op1`)，取出它对应的父 IR ref。

**第 427–428 行**:soft-float 32 位机上，HIOP 是个伴生指令(浮点拆两个 32 位),ref 递增。

**第 429–430 行**：遇到 `IR_PVAL`。它的 op1 存的就是父 ref(去偏移)，加回 REF_BIAS 还原。

**第 431–432 行**：遇到**别的** IR(不是 PVAL/SLOAD_PARENT)，说明 side trace 自己的 IR 开始了(`lj_snap_replay` 重放的起点 IR 走完，进入正常录制的 IR)，跳出循环。

**第 434–437 行**：核心。`rs = T->ir[ref].prev`——从父 trace 取出这条 IR 的 RegSP(寄存器分配结果，存 `prev` 字段，P5-17 §13 讲过)。然后处理重命名(`snap_renamefilter`/`snap_renameref`,P5-17 §15 讲过，回溯退出那一刻的真实位置)。最后把这个 RegSP 写到 side trace 这条 IR 的 `prev`:`ir->prev = (uint16_t)rs`。

**结果**:side trace 的这条 SLOAD_PARENT/PVAL，现在有了和父 trace 对应 IR 一样的 RegSP——也就是说，后端寄存器分配器看到"这个值在 rax/spill 槽 5"，就会生成"从 ExitState 的 rax 副本/spill 槽 5 取值"的机器码。而 ExitState 正是 side trace 入口时父 trace 退出存下来的(P5-17 §10 的 exit stub 存的)——所以这个值能正确取到。

这就是 side trace "从退出点状态无缝衔接"的后端落实：**父 trace 的 RegSP 映射到 side trace，机器码直接从 ExitState 取值，不用走解释器栈**。

### §16 J->parent 怎么定：lj_trace_exit 的前半段

§10 说 `trace_hotside` 里 `J->parent` "已经在 `lj_trace_exit` 里设好了"。展开看一下，`lj_trace_exit`(`lj_trace.c:886`)的前半段：

```c
886	/* A trace exited. Restore interpreter state. */
887	int LJ_FASTCALL lj_trace_exit(jit_State *J, void *exptr)
888	{
...
904	#ifdef EXITSTATE_PCREG
905	  J->parent = trace_exit_find(J, (MCode *)(intptr_t)ex->gpr[EXITSTATE_PCREG]);
906	#else
907	  UNUSED(ex);
908	#endif
909	  T = traceref(J, J->parent); UNUSED(T);
910	#ifdef EXITSTATE_CHECKEXIT
911	  if (J->exitno == T->nsnap) {  /* Treat stack check like a parent exit. */
912	    lj_assertJ(T->root != 0, "stack check in root trace");
913	    J->exitno = T->ir[REF_BASE].op2;
914	    J->parent = T->ir[REF_BASE].op1;
915	    T = traceref(J, J->parent);
916	  }
917	#endif
```

**第 905 行**：从 ExitState 里存的 PC(`ex->gpr[EXITSTATE_PCREG]`，某些平台 exit stub 把机器码 PC 存进一个寄存器)反查出是哪个 trace 退出的。这是 `EXITSTATE_PCREG` 平台的做法；不定义这个宏的平台，`J->parent` 在进入 `lj_trace_exit` 前就已经由汇编侧设好了。

**第 911–916 行**：栈检查退出的特殊处理。每个 trace 末尾都有个栈溢出检查 guard，它的 exit 号等于 `T->nsnap`(超出正常 snapshot 范围)。如果这种退出发生，说明是栈检查失败——它实际是要退到父 trace 的某个点(`T->ir[REF_BASE]` 存着栈检查的父信息)。这里把 `J->exitno` 和 `J->parent` 改写成"等价的父退出"，这样后面的 side trace 录制逻辑能统一处理。

这段代码的意义：不管退出是普通的 guard 失败，还是特殊的栈检查失败，`J->parent`/`J->exitno` 最终都指向"该为哪个 trace 的哪个退出点服务",`trace_hotside` 拿到的就是正确信息。

### §17 录制结束与编译：trace_stop

side trace 录制完(`lj_record_stop` 设 `LJ_TRACE_END`)，状态机走 `LJ_TRACE_ASM` → `lj_asm_trace` 生成机器码 → `trace_stop`。`trace_stop`(`lj_trace.c:499`)是 side trace **安装**的地方：

```c
498	/* Stop tracing. */
499	static void trace_stop(jit_State *J)
500	{
501	  BCIns *pc = mref(J->cur.startpc, BCIns);
502	  BCOp op = bc_op(J->cur.startins);
...
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
...
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
...
557	  /* Commit new mcode only after all patching is done. */
558	  lj_mcode_commit(J, J->cur.mcode);
559	  J->postproc = LJ_POST_NONE;
560	  trace_save(J, T);
...
567	}
```

`switch` 按 `J->cur.startins` 分流(回忆 §12:side trace 的 startins 被设成 `BC_JMP`)。所以 side trace 走 `case BC_JMP`(528–545 行)。逐段：

**第 530 行**:`lj_assertJ(J->parent != 0 && J->cur.root != 0, "not a side trace")`。断言这是 side trace(parent 非 0 且 root 非 0)。

**第 531 行**:`lj_asm_patchexit(J, traceref(J, J->parent), J->exitno, J->cur.mcode)`。**热补丁**——下一节 §18 详讲。它改父 trace 的机器码，把第 `J->exitno` 个 guard 的跳转目标，改成 side trace 的入口 `J->cur.mcode`。

**第 533–537 行**：补丁完，把父 trace 这个 snapshot 的 `count` 置为 `SNAPCOUNT_DONE`(255)。这就是 §10 `trace_hotside` 里 `snap->count != SNAPCOUNT_DONE` 那道闸的对应：已链接的退出点，count 锁死成 255，下次再退出也不会触发录制。同时，如果 side trace 用到更大的 `topslot`(栈深度)，把父的 topslot 也更新——这是为栈扩容考虑(下次再从这个退出点退，需要的栈可能更深)。

**第 539–544 行**：把 side trace 挂到 trace 树上。`root = traceref(J, J->cur.root)` 取出根 trace,`root->nchild++`(这棵树多了一条 side trace)，然后 `J->cur.nextside = root->nextside; root->nextside = traceno`——**头插法**把新 side trace 插到根的 nextside 链表头部。这就是 trace 树的组织(§20 详讲)。

对比 `case BC_FORL`/`BC_LOOP`/`BC_FUNCF`(511–520 行，root trace):root trace 是**改字节码**(把循环回边指令改成 BC_JLOOP，设 d 字段为 traceno)，并挂到 prototype 的 `pt->trace` 链。side trace 不改字节码(它不从字节码起步)，改的是**父 trace 的机器码**。这是 root 和 side 在安装阶段的根本区别。

**第 558 行**:`lj_mcode_commit(J, J->cur.mcode)`。所有 patching 做完，才提交 side trace 自己的机器码(设保护、登记)。注释"Commit new mcode only after all patching is done"强调顺序：必须先 patch 父，再提交子，保证一致性。

**第 560 行**:`trace_save(J, T)`。把 side trace 的 IR/snapshot/mcode 拷贝压缩到最终 GCtrace(`T`)里，挂进全局 trace 表(`J->trace[traceno]`)。这一步之后，side trace 正式可用。

### §18 热补丁：lj_asm_patchexit

这是本章第二个核心机制。`lj_asm_patchexit` 在 x86 后端(`lj_asm_x86.h:3126`,**注意不在 lj_asm.c**):

```c
3126	/* Patch exit jumps of existing machine code to a new target. */
3127	void lj_asm_patchexit(jit_State *J, GCtrace *T, ExitNo exitno, MCode *target)
3128	{
3129	  MCode *p = T->mcode;
3130	  MCode *mcarea = lj_mcode_patch(J, p, 0);
3131	  MCode *len = T->szmcode;
3132	  MCode *px = exitstub_addr(J, exitno) - 6;
3133	  MCode *pe = p+len-6;
3134	  MCode *pgc = NULL;
3135	#if LJ_GC64
3136	  uint32_t statei = (uint32_t)(GG_OFS(g.vmstate) - GG_OFS(dispatch));
3137	#else
3138	  uint32_t statei = u32ptr(&J2G(J)->vmstate);
3139	#endif
3140	  if (len > 5 && p[len-5] == XI_JMP && p+len-6 + *(int32_t *)(p+len-4) == px)
3141	    *(int32_t *)(p+len-4) = jmprel(J, p+len, target);
3142	  /* Do not patch parent exit for a stack check. Skip beyond vmstate update. */
3143	  for (; p < pe; p += asm_x86_inslen(p)) {
3144	    intptr_t ofs = LJ_GC64 ? (p[0] & 0xf0) == 0x40 : LJ_64;
3145	    if (*(uint32_t *)(p+2+ofs) == statei && p[ofs+LJ_GC64-LJ_64] == XI_MOVmi)
3146	      break;
3147	  }
3148	  lj_assertJ(p < pe, "instruction length decoder failed");
3149	  for (; p < pe; p += asm_x86_inslen(p)) {
3150	    if ((*(uint16_t *)p & 0xf0ff) == 0x800f && p + *(int32_t *)(p+2) == px &&
3151	        p != pgc) {
3152	      *(int32_t *)(p+2) = jmprel(J, p+6, target);
3153	    } else if (*p == XI_CALL &&
3154	              (void *)(p+5+*(int32_t *)(p+1)) == (void *)lj_gc_step_jit) {
3155	      pgc = p+7;  /* Do not patch GC check exit. */
3156	    } else if (LJ_64 && *p == 0xff &&
3157	                         p[1] == MODRM(XM_REG, XOg_CALL, RID_RET) &&
3158	                         p[2] == XI_NOP) {
3159	      pgc = p+5;  /* Do not patch GC check exit. */
3160	    }
3161	  }
3162	  lj_mcode_sync(T->mcode, T->mcode + T->szmcode);
3163	  lj_mcode_patch(J, mcarea, 1);
3164	}
3165	
```

逐段拆这个热补丁算法：

**第 3129 行**:`p = T->mcode`——父 trace 的机器码起点。

**第 3130 行**:`mcarea = lj_mcode_patch(J, p, 0)`——把父 trace 的机器码所在内存区域**改成可写**。机器码默认是只读可执行的(W^X 保护，P4-15 讲过)，要改它必须先翻转保护。`lj_mcode_patch(J, p, 0)` 的 `0` 表示"开始 patching"(查 `lj_mcode_patch` 实现，`lj_mcode.c:416`：第 424–440 行，找到 p 所在的 mcode area，调 `mcode_protect(J, MCPROT_GEN)` 翻成可写，返回 area 基址 `mcarea`)。返回值 `mcarea` 留着最后恢复保护用。

**第 3132 行**:`px = exitstub_addr(J, exitno) - 6`。算出"第 exitno 个 exit stub 的地址减 6"。为什么减 6？因为 x86 的长条件跳转指令(`0f 8x xx xx xx xx`)是 6 字节，guard 失败时跳向 exit stub 的那条指令，它的目标地址 = `px + 6`。所以"跳向这个 exit stub 的指令"，其目标地址等于 `px + 6`，即 `p + *(int32_t*)(p+2) == px`。`exitstub_addr` 宏在 `lj_target.h:160`。

**第 3133 行**:`pe = p + len - 6`。扫描上限，留 6 字节余量(避免越界读 6 字节跳转)。

**第 3140–3141 行**:**特判——trace 末尾的无条件跳转**。有些 trace 末尾有 条 `XI_JMP`(5 字节无条件跳转)直接跳 exit stub(比如非循环 trace 的结尾)。如果末尾这条 JMP 的目标正好是 px，改成跳 target。这是边角情况。

**第 3143–3148 行**:**第一趟扫描——跳过栈检查 prolog**。每个 trace 的机器码开头，有一段 prolog(设 vmstate、栈检查)。栈检查也是个 guard，它的 exit 不能被 patch(否则 side trace 会错误地接管栈检查失败)。这一趟用 `asm_x86_inslen`(指令长度解码器，逐条算指令长度)往前扫，直到找到写 vmstate 的那条 `MOV [vmstate], ...` 指令(`XI_MOVmi` + statei 立即数)，从这里之后才是真正的 trace body。`lj_assertJ(p < pe, "instruction length decoder failed")` 保证一定找得到。

**第 3149–3161 行**:**第二趟扫描——找并 patch 所有目标为 px 的条件跳转**。这是核心：

- **第 3150–3152 行**:`(*(uint16_t *)p & 0xf0ff) == 0x800f`——识别 x86 长条件跳转指令。`0f 8x`(x 是条件码：82=jb, 84=je, 8c=jl, ...)的低字节是条件，高字节固定 0f，中间字节是 8x。`& 0xf0ff` 把条件码位(低 4 位)抹掉，只要匹配 `0x800f` 就是长条件跳转。然后 `p + *(int32_t *)(p+2) == px`——这条跳转的目标(当前指令地址 + 6 字节指令长度 + 4 字节偏移)是不是正好等于 px？是，说明这就是"跳向第 exitno 个 exit stub"的那条 guard 指令。`*(int32_t *)(p+2) = jmprel(J, p+6, target)`——**算出跳向 side trace 入口的新偏移，原地写回**。这就是热补丁本身。

- **第 3153–3155 行 和 3156–3160 行**：识别 GC step 检查(`lj_gc_step_jit` 调用之后的退出检查)，记下它的位置 `pgc`。下一轮循环里，如果某条条件跳转 `p == pgc`(其实是 pgc 附近)，跳过不 patch——GC 检查的 exit 不能被 side trace 接管(它是运行时的 GC 触发，语义上要退解释器做 GC)。

**第 3162 行**:`lj_mcode_sync(T->mcode, T->mcode + T->szmcode)`——刷新指令缓存。x86/x64 上这是空操作(`lj_mcode.c:47`：硬件保证 cache coherency)；其他架构(arm/ppc/mips)上要真的 flush icache，因为修改了机器码后，icache 可能还存着旧指令。

**第 3163 行**:`lj_mcode_patch(J, mcarea, 1)`——把保护**翻回可执行**(MCPROT_RUN)。`1` 表示"结束 patching"。

合起来，这个算法的思路是：**扫父 trace 的机器码，逐条指令解码，找出所有"跳向目标 exit stub 的条件跳转"，把它们的跳转目标改成 side trace 入口**。难点在于：

1. 要跳过 prolog(栈检查不能 patch)。
2. 要避开 GC step 检查。
3. 要正确解码 x86 变长指令(`asm_x86_inslen`)。

为什么能放心改？因为 trace 的机器码是 LuaJIT 后端自己生成的，它**精确知道每条指令的边界和模式**——不需要通用的反汇编器，自家的 `asm_x86_inslen` 就够。而且后端生成时有意避免某些歧义指令模式(`lj_asm_x86.h` 注释提到故意插 nop)，保证 patchexit 的 pattern matcher 能无歧义地找到目标。这是生成器和解析器协同的设计。

### §19 patchexit 不止 patch 一个点

注意 §18 第二趟扫描是**遍历整个 trace body**，不是只 patch 一个点。为什么？因为同一个 exit号对应的 exit stub，在 trace 机器码里**可能被多条指令跳向**。

举个例子：一个 guard "检查 x 是整数"，后端可能生成多条检查指令(比如先检查 tag、再检查值范围)，它们失败时都跳同一个 exit stub(都对应这张 snapshot)。patchexit 要把这些**全部**改成跳 side trace，不能漏——漏了一条，那条路径上 guard 失败时还是退回解释器，side trace 就没生效。

所以 `for (; p < pe; ...)` 扫到底，把所有匹配的都 patch。这是"一个 snapshot 对应多条 guard 机器码"的体现。

### §20 trace 树的组织：GCtrace 的树字段

讲完了单个 side trace 的安装，现在看整体的 trace 树。一个 root trace 可能挂多个 side trace(每个常失败的 guard 一个),side trace 自己可能再有 side trace(它的 guard 也可能失败)。这就形成一棵树。这棵树靠 `GCtrace` 的几个字段组织(`lj_jit.h:272–278`):

```c
272	  uint16_t nchild;	/* Number of child traces (root trace only). */
274	  TraceNo1 traceno;	/* Trace number. */
275	  TraceNo1 link;	/* Linked trace (or self for loops). */
276	  TraceNo1 root;	/* Root trace of side trace (or 0 for root traces). */
277	  TraceNo1 nextroot;	/* Next root trace for same prototype. */
278	  TraceNo1 nextside;	/* Next side trace of same root trace. */
```

逐个讲这些字段怎么搭出树：

**`traceno`**：这条 trace 的全局编号(1 到 maxtrace)。所有 trace 存在 `J->trace[]` 数组里，用 traceno 索引。`traceref(J, n)` 宏(`lj_jit.h:289`)就是 `J->trace[n]` 取出 GCtrace。

**`root`**：这条 trace 所属的 root trace 号。对 root trace 自己，`root == 0`；对 side trace,`root` 指向它所在树的根(§12 讲过，即使 side 的 side,`root` 也指向最顶上的 root)。靠这个字段，任意一条 trace 都能 O(1) 找到它的根。

**`nchild`**：这棵树有多少条 side trace(**只在 root trace 上维护**)。每次 `trace_stop` 新挂一条 side,`root->nchild++`(`lj_trace.c:541`)。靠它判断是不是到了 `maxside` 上限(§12 sidecheck)。

**`nextside`**：同一棵树的 side trace 组成一条**单向链表**,**挂在 root trace 上**。新 side trace 头插：`J->cur.nextside = root->nextside; root->nextside = traceno`(`lj_trace.c:542–543`)。所以遍历一棵树的所有 side trace:`for (n = root->nextside; n != 0; n = traceref(J,n)->nextside)`。注意：这个链表是平的——不管 side 是 root 的直接子，还是更深的孙，都挂在 root 的 nextside 链上。这是设计简化：不维护真正的树形父子(除了 root 字段),side 之间用扁平链表管理。

**`nextroot`**：不同 root trace 之间，如果它们起点在同一个 prototype(Lua 函数)，用 `nextroot` 串起来，挂在 prototype 的 `pt->trace` 字段上。`trace_stop` 里 `J->cur.nextroot = pt->trace; pt->trace = traceno`(`lj_trace.c:519–520`)。所以一个 Lua 函数可能有多个 root trace(比如不同的循环回边各自热了)，它们靠 nextroot 串联。

**`link`**：这条 trace 跑完后**链到哪个 trace**(或自己)。这是 P5-19 要讲的 TraceLink。对循环 root trace,`link == traceno`(自己链自己)；对 side trace,link 可能是 root、是别的 side、或退解释器。

把这五个字段合起来，trace 树的组织是：

```
prototype.pt->trace  ──>  root1.nextroot ──> root2.nextroot ──> ...
                              |
                              v (nextside 链,平的)
                            side_a.nextside ──> side_b.nextside ──> ...
                            (每条 side 自己的 root 字段都指回 root1)
```

这种"扁平 nextside 链 + root 反向指针"的设计，让常用操作都很快：

- **找一个 trace 的根**:`T->root`,O(1)。
- **遍历一棵树所有 side**:`root->nextside` 链，O(nchild)。
- **判断是否超 maxside**:`root->nchild >= maxside`,O(1)。
- **加新 side**：头插 nextside + `nchild++`,O(1)。

不需要真正的树结构(父指针、子数组)，因为 LuaJIT 从不需要"找某个 side 的父 side"——side 录制时只要知道 root(算 `J->cur.root`)，不需要父 side;patchexit 时只要 `J->parent`(直接的父 trace 号)，存在 jit_State 里不存 GCtrace。这种"够用就好"的简化，是 LuaJIT 一贯的风格。

### §21 side trace 自己也会 side exit

§8 阶段 0 说"side trace 装上后，guard 失败直接跳 side trace 跑机器码"。但 side trace 自己也有 guard(它的机器码里也插了检查)，这些 guard 也可能失败。

side trace 的 guard 失败时，和 root trace 的 guard 失败一模一样：跳 exit stub → `lj_trace_exit` → `lj_snap_restore` 恢复 → 退解释器。而且，如果这个 side trace 的某个 guard 也经常失败(它的 snapshot->count 到 hotexit),`trace_hotside` 一样会为它录一条**新的 side trace**——也就是 side trace 的 side trace(孙 trace)。

这就是 trace 树**递归地生长**的机制：每个 trace(不管 root 还是 side)的 guard 都可能长出新的 side，树就越长越深。`maxside = 100` 限制一棵树的 side 总数(不管多少层)，防止爆炸。

注意 §12 里 `TraceNo root = T->root ? T->root : J->parent`——孙 trace 的 `J->cur.root` 继承自父 side 的 root，永远指向最顶上的 root trace。所以不管树多深，所有 trace 都归属同一个 root,nchild 都记在 root 上。这保证了 maxside 限制是"整棵树"的上限，不是某一层的。

### §22 把三个机制在源码层的衔接串起来

现在把本章讲的三个机制，在源码层串成一条完整的链：

```
运行时,某 trace 的第 N 个 guard 失败:
  机器码跳 exit stub (vm_x64.dasc 的 ->vm_exit_handler)
    → exit stub 存寄存器到 ExitState,调 lj_trace_exit (lj_trace.c:887)
      → lj_trace_exit 设 J->parent / J->exitno (lj_trace.c:905-916)
      → lj_trace_exit 调 lj_snap_restore 恢复解释器状态 (P5-17)
      → 恢复完,检查要不要录 side trace:
        → lj_trace_exit 末尾调 trace_hotside (lj_trace.c:946)
          → trace_hotside 看 snap->count:
            → 没到 hotexit:count++,退解释器,结束。
            → 到了 hotexit 且 != SNAPCOUNT_DONE:
              → J->state = LJ_TRACE_START,调 lj_trace_ins (lj_trace.c:808-809)
                → 状态机 LJ_TRACE_START → trace_start (lj_trace.c:419)
                  → 分配 trace 号,清 J->cur,调 lj_record_setup (lj_trace.c:495)
                    → lj_record_setup 看 J->parent != 0,走 side 分支 (lj_record.c:2864)
                      → 设 J->cur.root, startins=BC_JMP (lj_record.c:2867-2868)
                      → 调 lj_snap_replay(J, T) 重放起点 (lj_record.c:2881)
                        → 遍历父 snapshot,发射 SLOAD_PARENT / PVAL / sunk 重放 (lj_snap.c:507-689)
                      → sidecheck: 超限就 lj_record_stop(LJ_TRLINK_INTERP) 放弃 (lj_record.c:2883-2891)
                    → setup 完,状态机进 LJ_TRACE_RECORD
                      → 逐条字节码 lj_record_ins 录制 (P2-06)
                      → 遇循环回边/退出,调 lj_record_stop (lj_record.c:299)
                        → 设 linktype/link,拍末 snapshot,进 LJ_TRACE_END
                → 状态机 LJ_TRACE_END → 优化 → LJ_TRACE_ASM
                  → lj_asm_trace 生成机器码 (P4-14)
                    → 寄存器分配前,调 lj_snap_regspmap (lj_snap.c:407)
                      → 把 side 的 PVAL/SLOAD_PARENT 和父 IR 的 RegSP 对上
                    → 生成 side trace 的 mcode
                  → trace_stop (lj_trace.c:499)
                    → startins 是 BC_JMP,走 case BC_JMP (lj_trace.c:528)
                      → lj_asm_patchexit(J, 父trace, exitno, side的mcode) (lj_trace.c:531)
                        → 改父机器码:第 exitno 个 guard 的跳转目标改成 side 入口 (lj_asm_x86.h:3127)
                      → 父 snap->count = SNAPCOUNT_DONE (lj_trace.c:535)
                      → side 挂到 root->nextside,root->nchild++ (lj_trace.c:541-543)
                    → trace_save 提交 (lj_trace.c:560)
                  → 状态机回 LJ_TRACE_IDLE,JIT 继续跑
下次这个 guard 失败:机器码直接跳 side trace 入口,跑 side 的 mcode,不退解释器。
```

这条链上每个环节，本章都讲清了"为什么"和"怎么做"。三个机制各司其职：

- `trace_hotside`：决定**何时**录(数退出次数)。
- `lj_snap_replay`：决定**从什么状态**录(重放父 snapshot)。
- `lj_asm_patchexit`：决定录好**怎么生效**(热补丁父机器码)。

三者合起来，实现了"从退出点录制新 trace 并让它接管那条失败路径"。

---

## 第三部分：为什么这样设计是 sound 的

讲完了实现，现在回答最关键的问题：**凭什么 side trace 跑出来的结果，和"全程解释器跑"一致？凭什么热补丁不会把父 trace 改坏？**

这是"把动态执行**安全**变成机器码"里"安全"两个字在 side trace 这一环的命门。分几层论证。

### §23 不变式一：side trace 从正确的起点状态出发

第一个保证：**side trace 的起点状态，等于父 trace 在退出那一刻的真实状态**。

这个保证由 `lj_snap_replay` 的正确性支撑。`lj_snap_replay` 遍历的，是父 trace 第 `J->exitno` 张 snapshot——而这张 snapshot，正是父 trace 录制时在退出点拍的(P5-17 §7 讲过交错生成)。snapshot 完整且精确地记录了"退出点每个 slot 的值从哪条 IR ref 来"(P5-17 §19 论证过 snapshot 记全了解释器需要的状态)。

`lj_snap_replay` 把这张表翻译成新 trace 的 IR：常量重放成新常量、普通值发射成 SLOAD_PARENT(运行时从 ExitState 取)、sunk 分配现场重建。每一种翻译都**语义等价**:

- 常量：重放的常量值和父 trace 完全一致(`snap_replay_const` 从父 IR 读出来再发射)。
- SLOAD_PARENT：运行时取的是父 trace 那条 IR 在退出那一刻的值(由 `lj_snap_regspmap` 把父 RegSP 拷过来，后端从 ExitState 取)。
- sunk：重建的对象，和"父 trace 真的执行了那个分配 + 所有 sunk store"内容一致(重放用的是同一套 IR 指令和 snapshot 记的 value)。

所以 side trace 的起点状态，等价于"父 trace 跑到退出点、然后接着跑 side trace"——这正是我们想要的语义。从这点出发往下录，后续路径就是真实的字节码执行(和 root trace 录制一样)，正确性由字节码到 IR 的翻译保证(P2 篇讲)。

### §24 不变式二：side trace 自己的 guard 兜底

第二个保证：**side trace 不会比解释器更错**。

side trace 是一条 trace，它和 root trace 一样，录的是一条乐观假设的线性路径，机器码里插了 guard。它自己的乐观假设(比如"这个值在这条路上还是整数")如果失败，它自己的 guard 也会触发——走 side exit，退回解释器，或者再长一条更深的 side trace(§21)。

所以 side trace 继承了 P0-01 §9 的关键不变式：**它要么和解释器一样(假设成立)，要么退回解释器(假设失败)**。它永远不会比解释器更错。多层 side trace 嵌套也一样——每一层都有自己的 guard 兜底。

这点重要，因为 side trace 的假设可能比 root trace 更激进(它录的是"失败路径"，可能观察到 root 没观察到的类型变化，需要重新窄化)。但不管多激进，guard 保证安全。

### §25 不变式三：热补丁不破坏父 trace

第三个保证，是热补丁的 sound 性：**patchexit 只改跳转目标，不改父 trace 的执行逻辑**。

§18 讲过，patchexit 改的是"guard 失败时那条条件跳转指令的目标地址"——把"跳 exit stub"改成"跳 side trace 入口"。父 trace 的机器码本体(那些加法、表访问、其他 guard)一个字节都不动。

这为什么 sound？分两种情况看：

**情况 A：那个 guard 不失败**(大多数时候)。父 trace 的机器码跑到那条条件跳转，条件不成立(没跳)，继续往下执行父 trace 后续指令。这时**补丁完全没生效**——条件跳转不跳，目标地址是多少都无所谓。父 trace 行为和没补丁时一模一样。

**情况 B：那个 guard 失败**。条件成立，要跳。没补丁时跳 exit stub，走 side exit 退解释器；有补丁时跳 side trace 入口。两种情况哪个对？

- 跳 exit stub 退解释器：解释器从退出点接着跑，正确(P5-17 保证)。
- 跳 side trace:side trace 的入口状态(由不变式一)等于退出点状态，side trace 从这接着跑，跑的路径(由不变式二)要么正确执行、要么自己 guard 失败再退。

两种都对，而且 side trace 是机器码，更快。所以补丁**把"正确但慢"的退解释器，换成"正确且快"的跳 side trace"**，语义不变，性能提升。这就是补丁的 sound 性。

还要注意 patchexit **避开两类不能 patch 的指令**(§18):

- **栈检查 prolog**:trace 开头的栈溢出检查，它的 exit 必须退解释器(栈不够了，side trace 接管也会栈溢出)。第一趟扫描跳过它。
- **GC step 检查**：运行时 GC 触发的退出，语义上要退解释器做 GC。第二趟扫描记下 `pgc` 跳过它。

这两类是"语义上必须退解释器"的退出，patchexit 识别并保护它们，不让 side trace 错误接管。这是设计上的细致：补丁只接管"普通的乐观假设失败"，不接管"运行时系统事件"。

### §26 不变式四：一个 guard 只配一条 side trace

第四个保证：**防止重复编译**。

§10 讲过 `trace_hotside` 里有 `snap->count != SNAPCOUNT_DONE` 的闸。一旦为某个 guard 录了 side trace,`trace_stop` 把 `snap->count` 置成 `SNAPCOUNT_DONE`(255,`lj_trace.c:535`)。下次这个 guard 再失败(走 side trace 之后又退出来),`trace_hotside` 看到 255，不会再录第二条。

为什么要有这个保证？因为如果允许重复录，可能出现：guard 失败 → 录 side trace A → A 自己也经常失败 → 又录 side trace B(针对同一个原 guard)……但 A 和 B 录的是同一条路径(都是"原 guard 失败后的路径")，语义重复。而且 patchexit 已经把原 guard 的跳转改成跳 A,B 就没地方接(一个 guard 只有一个跳转目标)。所以"一个 guard 一条 side trace"是必要的。

那 side trace A 自己的 guard 经常失败怎么办？——为 A 的那个 guard 录一条新的 side trace(孙 trace)，挂在同一棵树上。这是递归地织网，不是对同一个 guard 重复录。

### §27 不变式五：补丁时父 trace 没在跑

第五个保证，是热补丁的**时序安全**。

§18 的 patchexit 发生在 `trace_stop` 里，此时控制权在 C 的状态机(`trace_state` → `lj_asm_trace` → `trace_stop`)。父 trace 的机器码此刻**没有在执行**——正在执行的是解释器(它通过 `lj_vm_cpcall` 调进状态机)。

所以"改正在执行的机器码"这个听着可怕的问题，在 LuaJIT 里不存在：补丁发生在 side trace 编译完成的瞬间，这时父 trace 静止。补丁打完，下次某个循环再跑到父 trace 的机器码，看到的就是新跳转目标。

即使考虑多线程(LuaJIT 的 JIT 是每线程一个 jit_State，不跨线程共享 trace)，也没有竞争：trace 是 thread-local 的，补丁不会和别的线程并发访问父 mcode。

唯一要小心的是 **icache 一致性**：改了机器码，CPU 的 icache 可能还存着旧指令。x86/x64 硬件保证 cache coherency(`lj_mcode_sync` 是空操作)，不用管；其他架构(arm/ppc/mips)上 `lj_mcode_sync` 真的 flush icache。patchexit 最后调 `lj_mcode_sync`(§18 第 3162 行)，保证所有架构上改完机器码立即可见。

### §28 五个不变式合起来

合起来，side trace 的 sound 性就完整了：

1. **起点状态正确**:`lj_snap_replay` 从父 snapshot 重放，初值等价于退出点状态(§23)。
2. **guard 兜底**:side trace 自己的 guard 保证不比解释器更错(§24)。
3. **补丁不改父逻辑**:patchexit 只改跳转目标，父 trace 本体不动，且避开栈检查/GC(§25)。
4. **不重复编译**：一个 guard 一条 side trace,SNAPCOUNT_DONE 锁死(§26)。
5. **补丁时序安全**：补丁时父没在跑，icache 正确刷新(§27)。

所以结论：**side trace 的执行结果，和"假设父 trace 的那个 guard 从一开始就走了另一条路、全程解释器跑"一致；热补丁不破坏父 trace 的正确性**。这是"机器码要么和解释器一样、要么退回解释器"(P0-01 §9)在 side trace 这一环的延续——只不过现在"退回解释器"被进一步优化成了"跳进 side trace 跑机器码"，但语义不变。

而且这套保证同样是**静态可推导**的：不需要运行时测试，从代码结构和不变式就能论证。这就是 LuaJIT 敢用"改正在生成的机器码"这么激进的机制，却几乎不出正确性 bug 的根源。

---

## 第四部分：★对照 + 回扣主线

### §29 ★对照一：官方 Lua(切"没有渐进优化")

官方 Lua 是纯解释器，**根本没有 trace、没有 side trace**。它的执行模型是：每条字节码都现场解释，一个循环跑 100 万次就解释 100 万次，每次都付出完整的"取指/译码/分发"开销。

更关键的是，官方 Lua**没有"渐进优化"的能力**。一段代码跑得再久，官方 Lua 对它的处理方式永远不变——永远是逐条解释。它不会观察到"这条路跑得多、值得优化"，也不会"为常走的分支专门加速"。程序的执行速度，从开始到结束是一条平线(不考虑 GC 抖动)。

LuaJIT 的 side trace 机制，引入了一种全新的执行演化模式：**程序会越跑越快**。一开始全解释器(慢),root trace 编译后热点快起来(快)，失败路径的 side trace 长出来后失败路径也快起来(更快)……程序运行的时间足够长，trace 树长得足够完整，几乎所有的热路径都变成了机器码，解释器只在冷代码和偶发失败时被唤醒。

这种"运行越久越快"的特性，是 trace JIT 独有的，官方 Lua 永远做不到。代价是 LuaJIT 实现了整整一套编译器(trace 录制/优化/后端/side trace/trace 链接)，复杂度远超官方 Lua 的解释器。

### §30 ★对照二：JVM/V8(重新编译 vs side trace)

JVM 和 V8 也是 JIT，也会"越跑越快"，但它们的机制和 side trace **本质不同**。对照能看清 trace JIT 的取舍。

**JVM/V8 的做法：重新编译 / 分层编译**。

JVM/V8 是 method JIT，以整个方法为单位编译。它们也会遇到"假设失效"——比如某个方法的参数类型变了，之前基于"参数总是 int"编译的机器码作废。它们的处理叫 **deoptimization**(去优化)：退回解释器，丢弃旧机器码。

那之后呢？JVM/V8 的做法是：**重新收集 profile(运行时类型统计)，基于新 profile 重新编译整个方法**。这次编译可能用了不同的假设(比如"参数有时是 int 有时是 long，生成带类型检查的代码")，或者升到更高的优化层级(C1 → C2,TurboFan 的分层)。

关键区别：

| 维度 | LuaJIT(side trace) | JVM/V8(重新编译) |
|---|---|---|
| 失败后优化单位 | 一条 side trace(从退出点起的新路径) | 重新编译整个方法 |
| 旧代码怎么办 | 保留，只补丁跳转目标 | 丢弃(deopt 后作废) |
| 优化粒度 | 精细(每个失败 guard 一条) | 粗(整个方法重来) |
| 编译开销 | 低(side trace 短，只录失败路径) | 高(整个方法重编) |
| 优化信息来源 | 父 snapshot(精确的退出点状态) | 重新收集 profile(统计性的) |
| 失败路径独立优化 | 是(side trace 专门优化失败路径) | 否(整个方法统一处理) |

最根本的差异是 **"增量优化" vs "重新优化"**:

- **LuaJIT 是增量优化**。root trace 不丢弃，失败路径单独长一条 side trace，两棵 trace 共存，各跑各的路径。优化是叠加的，越积越多。
- **JVM/V8 是重新优化**。旧机器码丢弃，基于新观察重新编译整个方法。优化是替换的，不是叠加。

为什么 LuaJIT 能增量？因为 trace 是**线性**的，一条 trace 只走一条路径，失败点明确(snapshot 精确记录退出状态)。从这个点接着录一条新 trace，语义清晰、衔接无缝。method JIT 不行——它的机器码覆盖整个方法的所有分支，某个分支失败不能"只重编那个分支"，只能整个方法重来(因为方法内的控制流是交织的图，分支之间有数据依赖)。

这又回到 P0-01 §10 的根本分歧：**trace 的线性性，让它支持增量优化(side trace);method 的复杂性，让它只能重新优化**。两种 JIT 的"失败后优化"策略，是它们编译单位选择的直接后果。

还有一个对照点：**snapshot 的角色**。LuaJIT 的 snapshot 一物两用——退出恢复 + side trace 起点(P5-17 §17、本章 §6)。JVM/V8 没有等价的"起点蓝图"：它们 deopt 时用 frame reconstruction 现场反推状态(P5-17 §25 对照过)，但反推出的状态只用来"交给解释器继续跑"，不会用来"从这点长出新编译路径"。JVM/V8 重新编译时，起点是方法的字节码开头(配合新 profile)，不是 deopt 点。所以 JVM/V8 的 deopt 是"终点"(交给解释器就完了),LuaJIT 的 side exit 是"中转站"(可能长出 side trace 继续优化)。这是 trace JIT 独有的渐进能力，根源正是 snapshot 把退出点状态完整、精确地留了下来。

### §31 回扣主线

把本章放回全书主线：**把动态执行安全变成机器码**。

side trace 是"快"这一股张力的极致发挥。P0-01 讲过 LuaJIT 平衡三股张力(快/安全/省)用三个机制：trace(只编热点，省)、guard(运行时检查，安全)、side exit + snapshot(失败回退，安全)。但这三个机制合起来，只解决了"主流路径快、失败能退"——失败后走的路径，还是解释器慢慢跑。

side trace 把"失败路径也快起来":**从反复失败的退出点，录制一条新 trace，让失败后走的路径也变成机器码**。这是 JIT 从"热点加速"走向"全网加速"的关键一步——程序跑得越久，trace 树长得越完整，越来越多的失败路径被 side trace 覆盖，退回解释器的次数越来越少。这就是 §1 开篇说的"越跑越快"的具体含义。

本章覆盖的几个点，合起来实现了这个能力：

- **触发**(`trace_hotside`,`lj_trace.c:798`)：数 guard 退出次数，到 `hotexit=10` 就录。一个 guard 一条 side trace,`SNAPCOUNT_DONE` 防重复。
- **起点重放**(`lj_snap_replay`,`lj_snap.c:507`)：遍历父 snapshot，把 slot→ref 映射翻译成新 IR 起点——常量重放、SLOAD_PARENT 从 ExitState 继承、sunk 分配现场重建。snapshot 一物两用(退出恢复 + side trace 起点)在这里落地。
- **后端落实**(`lj_snap_regspmap`,`lj_snap.c:407`)：把 side trace 的 PVAL/SLOAD_PARENT 和父 IR 的 RegSP 对上，机器码直接从 ExitState 取值，无缝衔接。
- **热补丁安装**(`lj_asm_patchexit`,`lj_asm_x86.h:3127`)：改父 trace 机器码，把 guard 的跳转目标从 exit stub 改成 side trace 入口。机器码级别的 pattern matching + 原地改写，但只改跳转目标、避开栈检查/GC、补丁时父没在跑。
- **trace 树**(`GCtrace` 的 root/nextroot/nextside/nchild,`lj_jit.h:272–278`)：扁平 nextside 链 + root 反向指针，让常用操作 O(1)，够用就好。
- **递归织网**:side trace 自己的 guard 也会失败，也会长孙 trace，树越深路径越完整。

side trace 把 P5 这一篇的四章串起来：**guard**(P5-16，机器码里插检查)→ **snapshot**(P5-17，失败时恢复状态)→ **side trace**(P5-18，失败够多次就从 snapshot 起点长新 trace)→ **trace 链接**(P5-19,trace 之间互相跳转)。四章合起来，就是 trace JIT 完整的"运行时自适应"：检查、退避、再优化、互相链接。一张 snapshot 同时服务前三章(退出恢复)和本章(side trace 起点)，是这套机制的枢纽；side trace 把 snapshot 留下的退出点状态，变成了"继续优化的跳板"，让 JIT 不会止步于"第一次编译"，而是持续地把更多路径变成机器码。

而 side trace 的设计哲学——**增量优化 + snapshot 重用 + 热补丁不改父逻辑**——也是整个 LuaJIT 的哲学：不追求一次编译到位(那是 method JIT 的路)，而是从实际运行中学习，渐进地把热点和它们的失败路径都编译成机器码，每次编译都小而快，失败可回退，正确性由 guard 和解释器保底。这种"渐进、自适应、永远 sound"的风格，让 LuaJIT 在动态语言 JIT 里独树一帜——它不像 JVM/V8 那样追求"尽量编译一次到最优"，而是"允许编译很多次，每次更接近完整"。这就是它"越跑越快"的引擎，也是 trace JIT 路线的精髓。

下一章我们看 trace 之间怎么互相跳转：[P5-19 trace 链接(TraceLink 9 种)](P5-19-trace链接TraceLink.md)——一条 trace 跑完，可以链到另一条 trace、回到自己(循环)、或退回解释器。这是把整棵 trace 树(包括 root + 所有 side trace)接成一张"机器码跳转图"的最后一步，从此 trace 之间不再需要退回解释器中转，直接机器码跳机器码，跑得更快。

---

*上一章 [P5-17 snapshot 恢复解释器状态](P5-17-snapshot恢复解释器状态.md)：snapshot 怎么把退出点状态无损翻译回解释器，这是 side trace 起点重放的基石。下一章 [P5-19 trace 链接(TraceLink 9 种)](P5-19-trace链接TraceLink.md)：trace 之间互相跳转，把整棵 trace 树接成机器码跳转图。*
