# P5-17 snapshot:把机器码状态翻译回解释器

> **本书主线**:把动态执行安全变成机器码。**二分法**:解释器侧 ↔ JIT 侧。本章落在 **JIT 侧·运行时**,承接 [P5-16 guard 运行时检查与 side exit](P5-16-guard运行时检查与side-exit.md):guard 在机器码里发现假设破裂后,接下来要做的不是"继续跑机器码"(那会算错),也不是"程序崩溃",而是**把当前状态完整、正确地交给解释器**,让解释器接管。这件"状态交接"的活,由 snapshot 完成。
>
> **本章回答的核心问题**:guard 失败的那一刻,机器码运行的数据全在 CPU 寄存器和它自己的 spill 区里,而解释器要数据在它自己的值栈上——两边对不上。怎么把前者**无损地**翻译成后者?
>
> **★对照**:JVM/V8 的 deoptimization(frame reconstruction)vs LuaJIT 的 snapshot restore。
> **源码**:LuaJIT 2.1.ROLLING,`lj_snap.c`(1034 行)、`lj_jit.h`(SnapShot/SnapEntry 编码)、`lj_record.c`(录制时交错生成)、`lj_trace.c`(lj_trace_exit 调 lj_snap_restore)、`lj_target.h`(regsp/RegSP)、`lj_target_x86.h`(ExitState)、`vm_x64.dasc`(exit stub 保存寄存器)。
> **基调**:纯直球,不用比喻;从第一性原理推导。

---

## 第一部分:这章解决什么问题(第一性原理推导)

### §1 把场景再钉死一次

P5-16 讲完了 guard。在 trace 的机器码里,每一个乐观假设(比如"这个值一定是整数""这个表一定没有 metatable")都对应一条或多条运行时检查指令。检查通过,机器码继续飞奔;检查不通过,机器码不能继续往下执行——因为后面所有指令都是按"假设成立"生成的,假设破了再往下走就会算错,甚至破坏内存。

那不通过之后去哪?答案是:**退回解释器**。解释器是完整的、正确的 Lua 实现,它不做任何乐观假设,任何类型都能按真实情况处理。只要能把"当前这一刻程序的状态"交给解释器,解释器就能从这条字节码接着往下跑,给出和"假设从一开始就没做、全程解释器跑"完全一致的结果。

这句话里有三个字是本章的全部难点:**交给它**。

"状态"听起来抽象,落到机器里就是一堆具体的数字:此刻每一个活跃变量等于几、是整数还是浮点、是 nil 还是一个表对象的指针。解释器要的,是这些数字摆成它能认的样子;而机器码此刻手里拿着的,是这些数字摆成的另一种样子。两种"样子"之间差着一条鸿沟。snapshot 就是跨过这条鸿沟的桥。

要讲清这座桥为什么必要,先得把鸿沟本身看清。下面两节分别看"机器码这一刻数据在哪""解释器要数据在哪",然后你看两边对不上,桥的必要性就自己冒出来了。

### §2 机器码这一刻,数据在哪

trace 编译完、装上以后,热点循环就跑在 `GCtrace->mcode` 指向的那段机器码里(P0-01 §11 讲过 GCtrace 三件套)。机器码是给 CPU 直接执行的,所以它操作数据的方式,完全是 CPU 那一套。

CPU 算术不能直接在内存里做,它得先把数据搬到 CPU 内部那些极快的存储单元里——**寄存器**。所以 trace 的机器码运行时,绝大多数活跃变量的当前值,**就在寄存器里**。一个 `x + y`,机器码大概是"把 rax 里的 x 加上 rbx 里的 y,结果放 rax"——x 和 y 此刻物理上在 rax、rbx 里,不在任何"栈"上。

但 CPU 的寄存器数量很少。x86-64 一共 16 个通用寄存器(rax、rbx、……、r15),arm64 也只有 30 来个。trace 里同时活跃的变量可能比这多。寄存器分配器(P4-13 讲过的线性扫描)分配不过来时,就得把一部分变量**临时赶出寄存器,存到内存里**,这叫 **spill**(溢出)。被 spill 的变量,此刻的值在内存的某个 spill 槽里。

所以机器码运行到任意一刻,一个活跃变量的值,只可能在三个地方之一:

1. **某个 CPU 寄存器里**(最常见,分配器给它分了家)。
2. **某个 spill 槽里**(寄存器不够时被赶出去的)。
3. **是一个常量**(编译期就定死的,比如字面量 `2`,机器码里直接 `mov rax, 2`)。

注意第三个:常量根本不需要"存放",机器码里用到时直接硬编码进指令。但解释器看到这个变量时,它要的是一个放在栈槽里的 TValue。常量也得恢复成一个值。

关键:**每个变量此刻"在哪",是寄存器分配器在编译 trace 时逐个决定的,而且中途可能变**——同一个变量在 trace 前半段可能在 rax,到后半段因为被 spill 又跑到 spill 槽 5,再后来又被 rematerialize(重新具体化)成常量。所以"此刻在哪个寄存器/spill 槽"这个信息,是**逐点、逐变量**的,没有简单规律。

这就是机器码这一侧的真相:数据散落在寄存器和 spill 槽里,每个变量在哪由分配器决定,且随位置变。

### §3 解释器这一刻,要数据在哪

解释器是栈式的(P1-02 讲过)。它眼里,每个活跃变量就老老实实待在一个叫**值栈**的内存数组里,每个变量占一个 **slot**(槽),slot 里是一个 `TValue`(带类型 tag 的值)。

解释器要执行 `x + y` 时,它的逻辑是:去 slot 里取出 x 的 TValue、取出 y 的 TValue、看两者的 tag、做对应类型的加法。它**完全不关心**什么寄存器——寄存器是解释器自己内部用的事(解释器自己也会用几个寄存器放 base 指针之类的),跟"Lua 变量的值在哪"无关。在解释器的世界观里,变量的值就一定在值栈的某个 slot 里。

所以当机器码要退回解释器时,解释器期待的初始状态是:**值栈上那些该有值的 slot,都得装好正确的 TValue**。只有满足这个前提,解释器才能从下一条字节码接着跑。

### §4 鸿沟:两种世界观对不上

把 §2 和 §3 并排放:

| | 机器码这一刻 | 解释器要的 |
|---|---|---|
| x 的值 | 在 rax 寄存器 | 在值栈 slot[2] |
| y 的值 | 在 spill 槽 5 | 在值栈 slot[3] |
| 常量 2 | 硬编码在指令里 | (可能也需要一个 slot) |
| 当前 PC | 机器码里的某个偏移 | 字节码里的某条指令 |

两边**完全对不上**。你不能直接把控制权丢给解释器——它一上手去读 slot[2],那里可能还是上一次函数调用留下的垃圾数据,x 根本不在那。

而且这个"对不上"还更深一层:机器码里此刻到底有哪些变量是活跃的?这本身就是编译器在 trace 录制时记录下来的信息。机器码自己没有"变量表",它只有一串指令。要知道"退出点应该恢复哪些 slot",必须有人在编译时把这个信息记下来。

**snapshot 就是干这件事的。** 它是编译 trace 时,在每一个可能退出的点(guard 处),逐个记录下:

> 在这个退出点,解释器的每一个 slot,其值应该从 trace 的哪条 IR 指令("IR ref")取来。

注意它记的不是"值本身",而是"值从哪来"(一个 IR ref)。因为退出是运行时才发生的事,值的具体数字要等运行时才知道;但"这个 slot 对应哪个 IR ref"是编译时就能定的。snapshot 记的是这个**映射关系**,运行时再根据映射,顺着 IR ref 去把真正的值算出来、搬过来。

P0-01 §8 把它叫"状态翻译表",现在我们把这张表到底长什么样、怎么用,一层层拆开。

### §5 snapshot 记什么:三个最小例子

光说"记 slot→ref 映射"还是抽象。用三个最小场景把这件事钉死。

**场景 A:值在寄存器里。**

假设 trace 录制时,变量 x 对应 IR 指令 `ref 003`(比如一条 `ADD`)。寄存器分配给 ref 003 分了寄存器 rax。那么退出点上,x 的值此刻在 rax 里。snapshot 记:slot[2] → ref 003。运行时退出,顺着 ref 003 去查它的分配信息("在 rax"),于是从退出时保存下来的 rax 副本里取出值,写到 slot[2]。

**场景 B:值被 spill 了。**

假设变量 y 对应 ref 005,分配器因为寄存器紧张把它 spill 到了 spill 槽 3。snapshot 记:slot[3] → ref 005。运行时退出,查 ref 005 的分配信息("spill 槽 3"),于是从退出时保存下来的 spill 区的第 3 槽取出值,写到 slot[3]。

**场景 C:值是常量。**

假设变量 z 永远等于整数 2,编译期就是常量 IR 指令 `KINT 2`(ref 在常量区)。snapshot 记:slot[4] → 这个常量 ref。运行时退出,查到它是常量,直接从 IR 里取出常量值 2,写到 slot[4]。寄存器/spill 都不用碰。

三个场景合起来,snapshot 的本质就清楚了:**它是一张"slot → IR ref"的表;每个 IR ref 自带"我此刻在哪个寄存器/spill 槽/我是常量"的分配信息;退出时按图索骥,把值从各处捞出来摆到对应的 slot 上。**

这里有个关键的连环:退出那一刻,寄存器的当前值得先被**保存**到一块内存里,snapshot 的恢复逻辑才能去读。这件事是机器码里的 exit stub 干的(P5-16 讲过 side exit 跳到 exit stub)。我们等下看源码时会贴 x64 的 exit stub 汇编,它干的就是"把所有寄存器倒进一个叫 ExitState 的结构体"。snapshot 恢复时读的"寄存器副本"和"spill 区副本",都在这个 ExitState 里。

所以完整的数据流是:

```
guard 失败
  → 跳到 exit stub
  → exit stub 把所有寄存器倒进 ExitState(含 gpr[]/fpr[]/spill[])
  → 调 lj_trace_exit
  → lj_trace_exit 调 lj_snap_restore
  → lj_snap_restore 按 snapshot 表,从 ExitState 各处捞值,写到解释器值栈
  → 解释器接管,从恢复好的 pc 继续跑
```

snapshot 就是这条链上最核心的"翻译步骤"。下面我们正式进源码,把这张表的编码、生成、恢复,逐行看清。

---

## 第二部分:源码怎么实现

### §6 snapshot 的两层数组:SnapShot + SnapEntry

一个 trace 可能有几十上百个 guard,每个 guard 一个退出点,每个退出点一张 snapshot 表。这些表怎么存?

直觉做法:每个退出点存一个完整数组,每个 slot 一项。但这样浪费:很多退出点之间只差一两个 slot 变化,大部分 slot 是一样的(同一个变量贯穿好几个 guard)。全存一遍,内存爆炸。

LuaJIT 的做法是**两层数组**,把"表头"和"表内容"分开:

- `GCtrace.snap`:一个 `SnapShot` 数组,每个元素是一个 snapshot 的**头部**(元信息:这张表的内容从哪开始、有几项、对应哪个机器码位置)。
- `GCtrace.snapmap`:一个 `SnapEntry` 数组,把**所有** snapshot 的实际条目内容,首尾相连地摊平存在一个大数组里。每个 `SnapShot` 用一个 `mapofs` 字段指向自己在 snapmap 里的起始偏移。

源码在 `lj_jit.h:180`:

```c
/* Stack snapshot header. */
typedef struct SnapShot {
  uint32_t mapofs;	/* Offset into snapshot map. */
  IRRef1 ref;		/* First IR ref for this snapshot. */
  uint16_t mcofs;	/* Offset into machine code in MCode units. */
  uint8_t nslots;	/* Number of valid slots. */
  uint8_t topslot;	/* Maximum frame extent. */
  uint8_t nent;		/* Number of compressed entries. */
  uint8_t count;	/* Count of taken exits for this snapshot. */
} SnapShot;
```

逐字段讲为什么这样设计:

- **`mapofs`**:这张 snapshot 的条目内容,在 `snapmap` 大数组里的起始下标。靠它定位"我的表内容在哪"。
- **`ref`**:这张 snapshot 对应的**第一条 IR 指令的 ref**。语义是"snapshot 拍在 IR 的哪个位置"——退出恢复时,只能用到这条 ref **之前**已经算出来的 IR 值(之后的还没执行,值不存在)。这个字段划定了"哪些 IR 值此刻是可用的"。
- **`mcofs`**:对应机器码里的偏移(以 MCode 单元计)。退出发生时,exit stub 知道自己从哪条机器指令跳出来的,靠二分 `mcofs` 数组反查到是第几个 snapshot(后面 §14 讲 lj_trace_unwind 会用到)。
- **`nslots`**:此刻值栈上一共有多少个有效 slot(从 base 到 top)。恢复时要保证栈有这么多 slot。
- **`topslot`**:栈可能要扩展到的最大深度(跨函数帧)。恢复前要确保值栈够大,不够要先扩容,否则写 slot 会越界。
- **`nent`**:这张 snapshot 实际**压缩后**的条目数。注意是"压缩后"——原始可能几十个 slot,但很多 slot 不需要恢复(见 §8 的 SNAP_NORESTORE),压缩完只剩真正要写的几个。
- **`count`**:这个退出点被触发过多少次。P5-18 讲 side trace 会用到:一个退出点被触发够多次(`hotexit` 阈值),就从它这里长出一条 side trace。

注意 `count` 还有个特殊值 `SNAPCOUNT_DONE = 255`(`lj_jit.h:190`),表示这个退出点已经编译并链接了 side trace,下次再退出直接跳 side trace,不用再退解释器。

**为什么分两层?** 因为 snapmap 是摊平的大数组,相邻 snapshot 可以**共享尾部**(后面的 snapshot 如果某个 slot 和前面一样,理论上能省)。更实际的好处是:`SnapShot` 头部固定 12 字节,可以紧凑排布、CPU cache 友好;而 snapmap 的条目按需增长,伸缩灵活。两层数组是"头部定长 + 内容变长"的经典编码。

接下来看 snapmap 里每个条目长什么样。`lj_jit.h:193`:

```c
/* Compressed snapshot entry. */
typedef uint32_t SnapEntry;

#define SNAP_FRAME		0x010000	/* Frame slot. */
#define SNAP_CONT		0x020000	/* Continuation slot. */
#define SNAP_NORESTORE		0x040000	/* No need to restore slot. */
#define SNAP_SOFTFPNUM		0x080000	/* Soft-float number. */
#define SNAP_KEYINDEX		0x100000	/* Traversal key index. */
...
#define SNAP(slot, flags, ref)	(((SnapEntry)(slot) << 24) + (flags) + (ref))
#define SNAP_TR(slot, tr) \
  (((SnapEntry)(slot) << 24) + \
   ((tr) & (TREF_KEYINDEX|TREF_CONT|TREF_FRAME|TREF_REFMASK)))
...
#define snap_ref(sn)		((sn) & 0xffff)
#define snap_slot(sn)		((sn) >> 24)
```

一个 `SnapEntry` 是 32 位,塞了三样东西:

- **高 8 位(bit 24–31)**:`slot` 编号(值栈第几个槽)。`snap_slot(sn) = sn >> 24`。
- **低 16 位(bit 0–15)**:`ref`,即"这个 slot 的值从哪条 IR 指令取"。`snap_ref(sn) = sn & 0xffff`。
- **中间 8 位(bit 16–23)**:一组标志位(`SNAP_FRAME` 等)。

低 16 位放 ref,是因为 LuaJIT 的 IR ref 用 16 位带偏移表示(`REF_BIAS = 0x8000`,`lj_ir.h:464`)。常量 ref < REF_BIAS,普通指令 ref ≥ REF_BIAS,都能塞进 16 位。一个 trace 的 IR 指令数有上限(`maxrecord = 4000`,`lj_jit.h:110`),16 位够用。

**三个最重要的标志位:**

- **`SNAP_FRAME`(0x010000)**:这个 slot 是一个**函数帧的边界**。Lua 的值栈上,一次函数调用会压入一个帧,帧底有个特殊的 slot 标记帧的类型和大小(ftsz)。恢复时这种 slot 不能简单写值,要写成帧链接。`LJ_STATIC_ASSERT(SNAP_FRAME == TREF_FRAME)`(`lj_jit.h:200`)保证它和录制时的 `TREF_FRAME` 位对齐,录制时直接把 TRef 的标志位原样搬进 SnapEntry。
- **`SNAP_CONT`(0x020000)**:continuation slot,跟 C 调 Lua 再回调的延续帧有关。
- **`SNAP_NORESTORE`(0x040000)**:**这个 slot 退出时不用恢复**。为什么会有"不用恢复"的 slot?因为有些 slot 是只读的、或者值没变过(下面 §8 详讲)。标了这个位的,snapshot 恢复时直接跳过,省时间。

`SNAP_TR` 宏把录制时 `J->slot[s]` 里的 TRef(一个 32 位数,低 16 位是 ref、中间是标志位)直接转成 SnapEntry——说明 snapshot 条目和录制时的 slot 表是**同构**的,录制时 slot 长啥样,snapshot 就记啥样,只是换了层皮。这是设计的简洁之处。

### §7 snapshot 怎么生成:lj_snap_add(录制时交错)

snapshot 不是录完整个 trace 再统一拍的。它是**录制过程中,每遇到一个需要退出点的位置,当场拍一张**。这叫"交错生成"(P2-06 提过)。

为什么要交错?因为 snapshot 记的是"此刻 slot→ref 的映射",而这个映射是**随录制演进的**:录制每往前走一条字节码,就可能新算出一个值、改变某个 slot 对应的 ref。如果在退出点不立刻拍,等到录制结束再补拍,拍出来的是录制结束时的映射,不是退出那一刻的——恢复出来就错了。

所以正确的做法是:**录制走到退出点这一步,立刻把当前 `J->slot[]` 表的状态固化下来**。这就是 `lj_snap_add`。

`lj_snap.c:181`:

```c
/* Add or merge a snapshot. */
void lj_snap_add(jit_State *J)
{
  MSize nsnap = J->cur.nsnap;
  MSize nsnapmap = J->cur.nsnapmap;
  /* Merge if no ins. inbetween or if requested and no guard inbetween. */
  if ((nsnap > 0 && J->cur.snap[nsnap-1].ref == J->cur.nins) ||
      (J->mergesnap && !irt_isguard(J->guardemit))) {
    if (nsnap == 1) {  /* But preserve snap #0 PC. */
      emitir_raw(IRT(IR_NOP, IRT_NIL), 0, 0);
      goto nomerge;
    }
    nsnapmap = J->cur.snap[--nsnap].mapofs;
  } else {
  nomerge:
    lj_snap_grow_buf(J, nsnap+1);
    J->cur.nsnap = (uint16_t)(nsnap+1);
  }
  J->mergesnap = 0;
  J->guardemit.irt = 0;
  snapshot_stack(J, &J->cur.snap[nsnap], nsnapmap);
}
```

逐行读:

1. 拿当前的 snapshot 数 `nsnap` 和 snapmap 占用 `nsnapmap`。
2. **合并判断**:有两种情况会"合并"而不是新建一张 snapshot:
   - `J->cur.snap[nsnap-1].ref == J->cur.nins`:上一张 snapshot 拍在和当前**完全相同**的 IR 位置(中间没新增 IR 指令)。那再拍一张是重复,直接覆盖上一张。
   - `J->mergesnap && !irt_isguard(J->guardemit)`:`mergesnap` 标志位表示"允许合并",且自上次以来**没发射过 guard 指令**(`guardemit` 为空)。没发射 guard 意味着没新的退出点需求,可以合并。
3. 合并的特例:`nsnap == 1`(只有第 0 张 snapshot)时,**不合并**,而是故意发射一条 `NOP` 指令把当前 IR 位置往后推一格,然后走 `nomerge` 新建。注释说"But preserve snap #0 PC"——第 0 张 snapshot 的 PC 有特殊用途(它标记 trace 的入口 PC),不能被覆盖掉。
4. 不合并的路径(`nomerge`):扩容 snapshot 缓冲区(`lj_snap_grow_buf`),`nsnap++`,准备写新的一张。
5. 清掉 `mergesnap` 和 `guardemit` 两个标志——它们是"自上次 lj_snap_add 以来的累积状态",拍完就清零,等下一次累积。
6. 调 `snapshot_stack` 把当前栈状态真正写进新 snapshot。

`mergesnap` 和 `guardemit` 这对标志是理解 snapshot 生成节奏的钥匙。录制过程中,有些操作会设 `mergesnap = 1`(表示"接下来如果没新 guard,可以和上一张合并"),有些会设 `guardemit`(发射了一条 guard IR)。`lj_snap_add` 根据这两个标志决定是新建还是合并。这套机制保证了:**每个真正需要退出点的位置都有且只有一张 snapshot,没有冗余**。

举个例子。录制一段循环体,顺序可能是:

```
record 字节码 1 (普通运算)        → guardemit 空
record 字节码 2 (类型检查,发 guard) → guardemit 置位
  → 调 lj_snap_add:guardemit 非空,不合并,新建 snapshot #1
record 字节码 3 (普通运算)        → guardemit 空
record 字节码 4 (循环回边)
  → 调 lj_snap_add(循环末尾必须拍):guardemit 空,mergesnap 看情况
```

注意循环末尾(`rec_forl` 里的 `lj_snap_add(J)`,`lj_record.c:574`)一定会调一次,因为循环回边处是 trace 的衔接点,必须有 snapshot 记录"循环跑完一圈、准备回开头"时的状态——万一下一圈某个 guard 失败,得能退到这个点。

### §8 拍栈:snapshot_stack 和差量压缩

`lj_snap_add` 把活儿派给 `snapshot_stack`(`lj_snap.c:161`),后者真正遍历栈、写条目:

```c
/* Take a snapshot of the current stack. */
static void snapshot_stack(jit_State *J, SnapShot *snap, MSize nsnapmap)
{
  BCReg nslots = J->baseslot + J->maxslot;
  MSize nent;
  SnapEntry *p;
  /* Conservative estimate. */
  lj_snap_grow_map(J, nsnapmap + nslots + (MSize)(LJ_FR2?2:J->framedepth+1));
  p = &J->cur.snapmap[nsnapmap];
  nent = snapshot_slots(J, p, nslots);
  snap->nent = (uint8_t)nent;
  nent += snapshot_framelinks(J, p + nent, &snap->topslot);
  snap->mapofs = (uint32_t)nsnapmap;
  snap->ref = (IRRef1)J->cur.nins;
  snap->mcofs = 0;
  snap->nslots = (uint8_t)nslots;
  snap->count = 0;
  J->cur.nsnapmap = (uint32_t)(nsnapmap + nent);
}
```

`nslots = baseslot + maxslot` 是此刻值栈的总深度(从栈底到当前帧顶)。先按 `nslots` 保守地扩容 snapmap(可能实际写得少,但先留够),然后:

- `snapshot_slots`:遍历所有 slot,把需要记的写成 SnapEntry,返回**实际写了多少条**(压缩后)。
- `snapshot_framelinks`:在条目尾部追加**帧链接信息**(PC、各层帧的 ftsz),返回追加了几个。

最后填好 SnapShot 头部:mapofs 指向起点、ref 是当前 IR 位置、nslots 是栈深、count 清零。

注意 `snap->mcofs = 0`:**录制阶段不知道机器码偏移**(机器码还没生成呢!)。mcofs 是后端 `lj_asm` 生成机器码时回填的,后面 §14 会看到 lj_trace_unwind 用它二分查 exit 号。

压缩的核心在 `snapshot_slots`(`lj_snap.c:63`):

```c
/* Add all modified slots to the snapshot. */
static MSize snapshot_slots(jit_State *J, SnapEntry *map, BCReg nslots)
{
  IRRef retf = J->chain[IR_RETF];  /* Limits SLOAD restore elimination. */
  BCReg s;
  MSize n = 0;
  for (s = 0; s < nslots; s++) {
    TRef tr = J->slot[s];
    IRRef ref = tref_ref(tr);
    ...
    if (ref) {
      SnapEntry sn = SNAP_TR(s, tr);
      IRIns *ir = &J->cur.ir[ref];
      if ((LJ_FR2 || !(sn & (SNAP_CONT|SNAP_FRAME))) &&
	  ir->o == IR_SLOAD && ir->op1 == s && ref > retf) {
	/*
	** No need to snapshot unmodified non-inherited slots.
	** But always snapshot the function below a frame in LJ_FR2 mode.
	*/
	if (!(ir->op2 & IRSLOAD_INHERIT) &&
	    (!LJ_FR2 || s == 0 || s+1 == nslots ||
	     !(J->slot[s+1] & (TREF_CONT|TREF_FRAME))))
	  continue;
	/* No need to restore readonly slots and unmodified non-parent slots. */
	if (!(LJ_DUALNUM && (ir->op2 & IRSLOAD_CONVERT)) &&
	    (ir->op2 & (IRSLOAD_READONLY|IRSLOAD_PARENT)) != IRSLOAD_PARENT)
	  sn |= SNAP_NORESTORE;
      }
      if (LJ_SOFTFP32 && irt_isnum(ir->t))
	sn |= SNAP_SOFTFPNUM;
      map[n++] = sn;
    }
  }
  return n;
}
```

这段是 snapshot 紧凑性的关键。逐层拆:

**第一层:只记有 ref 的 slot。** `TRef tr = J->slot[s]`,如果 `tref_ref(tr)` 是 0,说明这个 slot 在录制时**没有对应的 IR**(可能是个从未被这次 trace 碰过的死 slot),直接跳过,不写进 snapshot。这是第一道省:死的、不相关的 slot 不记。

**第二层:SLOAD 优化。** 这是最精妙的一层。`IR_SLOAD` 是"从栈加载"的 IR 指令(P2-07 SSA 构建时,trace 入口的变量都是从父解释器栈 load 进来的)。如果一个 slot 对应的 IR 是 SLOAD,且 `s == ir->op1`(这个 SLOAD 加载的就是 slot s 自己),说明这个变量的值**就是它进入 trace 时从解释器栈读进来的那个原值,trace 里从没改过它**。

这种 slot 有两种情况:

- **不是继承的**(`!(op2 & IRSLOAD_INHERIT)`):trace 里没改它,它进入 trace 时是多少、退出时还是多少。退出时**解释器栈上这个 slot 本来就还是那个值**(因为 trace 是机器码在跑,没动解释器栈)——所以根本不用恢复,`continue` 直接跳过,连条目都不写。
- **是继承的但只读**(`IRSLOAD_READONLY`):值虽然从父 trace 继承,但本 trace 声明只读不写。这种情况要写条目(因为 side trace 录制时要能重放出这个 slot),但标 `SNAP_NORESTORE`,退出恢复时跳过它,省时间。

注释里的 `retf`(`IR_RETF` 链)是个边界:RETF 指令之后的 SLOAD 不能做这个优化,因为 RETF 改变了帧结构,slot 语义变了。

**第三层:soft-float 标记。** `LJ_SOFTFP32` 平台(没有硬件浮点的 32 位机)上,浮点数占两个 slot。`SNAP_SOFTFPNUM` 标记这个 slot 是个 soft-float 数,恢复时要特殊处理(读两个 slot)。

这三层合起来,snapshot 从"全量记每个 slot"压缩到"只记 trace 真正碰过、且需要恢复的 slot"。一个有 30 个 slot 的栈,snapshot 实际可能只记 5–10 条。这就是为什么 snapshot 多(每个 guard 一个)却不会爆内存的根源。

### §9 帧链接:snapshot_framelinks

Lua 是有函数调用的,值栈上叠着多层帧。退出恢复时,光恢复值不够,还得把**帧的结构**恢复出来,否则解释器不知道当前在哪个函数、调用栈深度多少。

这件事 `snapshot_framelinks`(`lj_snap.c:110`)干。它在 snapmap 条目尾部追加:当前 PC、以及每层帧的 ftsz(frame type and size,帧类型和大小)。源码较长,核心是这段:

```c
/* Add frame links at the end of the snapshot. */
static MSize snapshot_framelinks(jit_State *J, SnapEntry *map, uint8_t *topslot)
{
  cTValue *frame = J->L->base - 1;
  cTValue *lim = J->L->base - J->baseslot + LJ_FR2;
  GCfunc *fn = frame_func(frame);
  cTValue *ftop = isluafunc(fn) ? (frame+funcproto(fn)->framesize) : J->L->top;
#if LJ_FR2
  uint64_t pcbase = (u64ptr(J->pc) << 8) | (J->baseslot - 2);
  ...
  memcpy(map, &pcbase, sizeof(uint64_t));
#else
  MSize f = 0;
  map[f++] = SNAP_MKPC(J->pc);  /* The current PC is always the first entry. */
#endif
  ...
  while (frame > lim) {  /* Backwards traversal of all frames above base. */
    if (frame_islua(frame)) {
      ...
      map[f++] = SNAP_MKPC(frame_pc(frame));
      frame = frame_prevl(frame);
    } else if (frame_iscont(frame)) {
      ...
      frame = frame_prevd(frame);
    } else {
      ...
      map[f++] = SNAP_MKFTSZ(frame_ftsz(frame));
      frame = frame_prevd(frame);
      continue;
    }
    ...
  }
  *topslot = (uint8_t)(ftop - lim);
  ...
  return f;
}
```

关键信息:

- **当前 PC 永远是第一条**。snapshot 条目里,所有 slot 条目之后,紧跟的第一条帧链接就是退出后解释器该接着执行的 PC。恢复时 `pc = snap_pc(&map[nent])`(`lj_snap_restore` 里这么取)。
- **帧链向后遍历**。从当前帧底 `frame = L->base - 1` 往栈底 `lim` 走,每经过一层帧,记下它的 PC 或 ftsz。这样 snapshot 完整记录了"退出时调用栈长什么样"。
- **LJ_FR2 模式**(`lj_jit.h` 顶部条件编译,2.1 ROLLING 在 64 位 GC64 下默认开 FR2):帧信息编码成一个 64 位 `pcbase`(PC 左移 8 位 | baseslot-2),占 2 个 SnapEntry。FR2 是 2.1 相对 2.0 的重要变化(老资料讲 FR1 多)。

`topslot` 算的是栈可能用到的最大深度(跨所有帧的 framesize),恢复前用它检查值栈够不够大。

### §10 退出发生:exit stub 先存寄存器

snapshot 是给"退出后"用的。但退出发生的那一瞬间,谁来把机器码运行时的寄存器值**存下来**?snapshot 恢复时读的"寄存器副本"从哪来?

答案在汇编写的 exit stub 里。x64 的实现在 `vm_x64.dasc:2408`(`->vm_exit_handler`):

```asm
|// Called from an exit stub with the exit number on the stack.
|// The 16 bit exit number is stored with two (sign-extended) push imm8.
|->vm_exit_handler:
|.if JIT
|  endbr
|  push r13; push r12
|  push r11; push r10; push r9; push r8
|  push rdi; push rsi; push rbp; lea rbp, [rsp+88]; push rbp
|  push rbx; push rdx; push rcx; push rax
|  movzx RCd, byte [rbp-8]   // Reconstruct exit number.
|  mov RCH, byte [rbp-16]
|  mov [rbp-8], r15; mov [rbp-16], r14
|  ...
|  set_vmstate EXIT
|  mov [DISPATCH+DISPATCH_J(exitno)], RCd
|  mov [DISPATCH+DISPATCH_J(parent)], RAd
|  sub rsp, 16*8              // Room for SSE regs.
|  movsd qword [rbp-8], xmm15; movsd qword [rbp-16], xmm14
|  ...                        // 存 xmm0..xmm15
|  mov L:RB, [DISPATCH+DISPATCH_GL(cur_L)]
|  mov BASE, [DISPATCH+DISPATCH_GL(jit_base)]
|  mov CARG2, rsp             // ExitState 指针 = 栈顶
|  lea CARG1, [DISPATCH+GG_DISP2J]   // jit_State 指针
|  call extern lj_trace_exit  // (jit_State *J, ExitState *ex)
```

逐段:

1. **push 所有通用寄存器**。r13/r12/r11/.../rax 一条条压栈。注意 r14、r15 是后存的(用 `mov [rbp-8], r15` 这种直接写到栈上对应位置),因为它们在 push 序列里位置特殊。这一堆 push 之后,栈上就摆好了所有 GPR 的当前值——这就是 ExitState 的 `gpr[]` 数组的物理来源。
2. **重建 exit 号**。exit stub 跳过来时,用两条 `push imm8` 把 16 位 exit 号编码进栈了(P5-16 讲过 side exit 用这种编码)。这里从栈上读出来放进 RC。
3. **存 SSE 寄存器**。`sub rsp, 16*8` 给 16 个 xmm 寄存器腾空间,然后 `movsd` 逐个存进去。这是 ExitState 的 `fpr[]`。
4. **设 vmstate = EXIT**,把 exit 号和父 trace 号写到 dispatch 全局状态。
5. **准备参数,调 lj_trace_exit**。CARG2(rsi)指向栈顶——这就是 ExitState 结构体的地址(C 里看就是从 gpr 数组开始的那块连续内存)。CARG1(rdi)指向 jit_State。

注意 `ExitState` 的定义(`lj_target_x86.h:157`):

```c
typedef struct {
  lua_Number fpr[RID_NUM_FPR];	/* Floating-point registers. */
  intptr_t gpr[RID_NUM_GPR];	/* General-purpose registers. */
  int32_t spill[256];		/* Spill slots. */
} ExitState;
```

spill 槽也在 ExitState 里。trace 的机器码运行时,被 spill 的变量就写在 `ex->spill[N]` 里(N 是分配器给的 spill 槽号)。退出时这些值原封不动留在那,snapshot 恢复时按 spill 槽号去读。

所以 exit stub 干的活就是:**把"机器码这一刻所有寄存器的值"原样倒进 ExitState 这个内存块**。snapshot 恢复时,所有值(无论原本在寄存器还是 spill 槽)都能从这个块里读到。这就是 §5 场景 A/B 里"从保存下来的副本取值"的物理实现。

### §11 恢复总入口:lj_trace_exit → lj_snap_restore

exit stub 调进 C 的 `lj_trace_exit`(`lj_trace.c:886`):

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
#ifdef EXITSTATE_CHECKEXIT
  if (J->exitno == T->nsnap) {  /* Treat stack check like a parent exit. */
    lj_assertJ(T->root != 0, "stack check in root trace");
    J->exitno = T->ir[REF_BASE].op2;
    J->parent = T->ir[REF_BASE].op1;
    T = traceref(J, J->parent);
  }
#endif
  lj_assertJ(T != NULL && J->exitno < T->nsnap, "bad trace or exit number");
  exd.J = J;
  exd.exptr = exptr;
  errcode = lj_vm_cpcall(L, NULL, &exd, trace_exit_cp);
  ...
}
```

几个要点:

- `J->exitno` 是 exit stub 传进来的退出号。注释 `/* For now, snapno == exitno. */`(在 lj_snap_restore 里)说明:退出号和 snapshot 号**一一对应**。第 N 个 guard 失败,就是第 N 个 exit,用第 N 张 snapshot 恢复。
- `EXITSTATE_CHECKEXIT` 分支:栈检查失败这种特殊退出(每个 trace 末尾都有个栈溢出检查 guard),它的 exit 号等于 `T->nsnap`(超出正常 snapshot 范围),特殊处理成"用父 trace 的某个 snapshot"。
- 关键是 `lj_vm_cpcall(... trace_exit_cp)`:**用一个受保护的调用包装 lj_snap_restore**。注释 `/* Need to protect lj_snap_restore because it may throw. */`(`lj_trace.c:834`)解释了为什么——lj_snap_restore 里可能触发栈扩容、可能 unsink 分配表(调 `lj_tab_new`),这些操作可能抛 Lua 错误(内存不足等)。如果直接调,错误会从 C 栈里乱蹦;用 cpcall 包一层,错误被规范地转成错误码返回。

`trace_exit_cp`(`lj_trace.c:835`)就是那个被保护的包装:

```c
static TValue *trace_exit_cp(lua_State *L, lua_CFunction dummy, void *ud)
{
  ExitDataCP *exd = (ExitDataCP *)ud;
  cframe_errfunc(L->cframe) = 0;
  cframe_nres(L->cframe) = -2*LUAI_MAXSTACK*(int)sizeof(TValue);
  exd->pc = lj_snap_restore(exd->J, exd->exptr);
  UNUSED(dummy);
  return NULL;
}
```

核心就一句:`exd->pc = lj_snap_restore(...)`。恢复完,把解释器接下来该执行的 PC 带回来。

### §12 真正的恢复:lj_snap_restore

这是本章的重头戏。`lj_snap.c:940`:

```c
/* Restore interpreter state from exit state with the help of a snapshot. */
const BCIns *lj_snap_restore(jit_State *J, void *exptr)
{
  ExitState *ex = (ExitState *)exptr;
  SnapNo snapno = J->exitno;  /* For now, snapno == exitno. */
  GCtrace *T = traceref(J, J->parent);
  SnapShot *snap = &T->snap[snapno];
  MSize n, nent = snap->nent;
  SnapEntry *map = &T->snapmap[snap->mapofs];
  ...
  TValue *frame;
  BloomFilter rfilt = snap_renamefilter(T, snapno);
  const BCIns *pc = snap_pc(&map[nent]);
  lua_State *L = J->L;

  setcframe_pc(L->cframe, bc_isret_or_tail(bc_op(*pc)) ? pc : pc+1);
  setcframe_pc(cframe_raw(cframe_prev(L->cframe)), pc);

  /* Make sure the stack is big enough for the slots from the snapshot. */
  if (LJ_UNLIKELY(L->base + snap->topslot >= tvref(L->maxstack))) {
    L->top = curr_topL(L);
    lj_state_growstack(L, snap->topslot - curr_proto(L)->framesize);
  }

  /* Fill stack slots with data from the registers and spill slots. */
  frame = L->base-1-LJ_FR2;
  ...
  for (n = 0; n < nent; n++) {
    SnapEntry sn = map[n];
    if (!(sn & SNAP_NORESTORE)) {
      TValue *o = &frame[snap_slot(sn)];
      IRRef ref = snap_ref(sn);
      IRIns *ir = &T->ir[ref];
      if (ir->r == RID_SUNK) {
        ...snap_unsink...
        continue;
      }
      snap_restoreval(J, T, ex, snapno, rfilt, ref, o);
      ...
    }
  }
  ...
  return pc;
}
```

逐段拆这个算法:

**第 0 步:定位 snapshot 和 PC。**

- `snap = &T->snap[snapno]`:用退出号当 snapshot 下标,取出那张表。
- `map = &T->snapmap[snap->mapofs]`:按 mapofs 找到这张表的条目内容起点。
- `pc = snap_pc(&map[nent])`:条目里前 `nent` 条是 slot,紧接着的(`map[nent]`)就是帧链接里的第一条——当前 PC(§9 讲过)。这是退出后解释器该接着跑的字节码地址。

**第 1 步:设好 cframe 的 PC。** 把解释器控制帧(cframe)里的 PC 字段设好,这样万一恢复过程中抛错误,错误信息能指向正确的位置。注意对 return/tailcall 指令特殊处理(pc 不 +1,因为 pc+1 可能越界)。

**第 2 步:确保值栈够大。** `topslot` 是 snapshot 记的栈最大深度。如果当前值栈顶 + topslot 超过了 maxstack,得先扩容(`lj_state_growstack`)。不扩容直接写 slot 会越界崩溃。

**第 3 步:遍历条目,逐个恢复。** 这是核心循环。对每条 SnapEntry:

- 跳过 `SNAP_NORESTORE` 的(§8 讲过,只读/未改的 slot)。
- 取出目标 slot 地址 `o = &frame[snap_slot(sn)]`、源 IR ref。
- **特判 sunk 分配**(`ir->r == RID_SUNK`):这个 slot 的值不是一个普通标量,而是一个被"下沉"(sink 优化,P3-11)分配出来的对象(table/cdata)。这种对象在 trace 里根本没真正分配(sink 优化把它推迟了),退出时要现场把它"捞回来"(unsink)。下面 §13 单独讲。
- **普通值**:调 `snap_restoreval`,从 ExitState 里把值捞出来写到 slot。

`rfilt` 是个 Bloom filter(`snap_renamefilter`),用来快速判断"这个 ref 在退出点之前有没有被寄存器重命名"。寄存器分配时,一个变量的"家"可能在中途被搬到另一个寄存器(`IR_RENAME` 指令记录这种搬运)。如果搬过,snapshot 里记的 ref 对应的"当前寄存器"得按 RENAME 链回溯到退出那一刻的真实寄存器。rfilt 用 Bloom filter 避免每条都查(大多数 ref 没被 rename,Bloom filter 快速排除)。

### §13 取值的细节:snap_restoreval

这是把"一个 IR ref 的值"从 ExitState 捞出来的核心函数。`lj_snap.c:698`:

```c
/* Restore a value from the trace exit state. */
static void snap_restoreval(jit_State *J, GCtrace *T, ExitState *ex,
			    SnapNo snapno, BloomFilter rfilt,
			    IRRef ref, TValue *o)
{
  IRIns *ir = &T->ir[ref];
  IRType1 t = ir->t;
  RegSP rs = ir->prev;
  if (irref_isk(ref)) {  /* Restore constant slot. */
    if (ir->o == IR_KPTR) {
      o->u64 = (uint64_t)(uintptr_t)ir_kptr(ir);
    } else {
      ...
      lj_ir_kvalue(J->L, o, ir);
    }
    return;
  }
  if (LJ_UNLIKELY(bloomtest(rfilt, ref)))
    rs = snap_renameref(T, snapno, ref, rs);
  if (ra_hasspill(regsp_spill(rs))) {  /* Restore from spill slot. */
    int32_t *sps = &ex->spill[regsp_spill(rs)];
    if (irt_isinteger(t)) {
      setintV(o, *sps);
    } else if (irt_isnum(t)) {
      o->u64 = *(uint64_t *)sps;
    ...
    } else {
      ...
      setgcV(J->L, o, (GCobj *)(uintptr_t)*(GCSize *)sps, irt_toitype(t));
    }
  } else {  /* Restore from register. */
    Reg r = regsp_reg(rs);
    if (ra_noreg(r)) {
      ... /* CONV NUM_INT 特殊处理 */
    } else if (irt_isinteger(t)) {
      setintV(o, (int32_t)ex->gpr[r-RID_MIN_GPR]);
    } else if (irt_isnum(t)) {
      setnumV(o, ex->fpr[r-RID_MIN_FPR]);
    ...
    } else {
      setgcV(J->L, o, (GCobj *)ex->gpr[r-RID_MIN_GPR], irt_toitype(t));
    }
  }
}
```

这里出现了本章最关键的一个数据结构:**`ir->prev` 存的是 RegSP**。

`RegSP` 是"register and spill slot"的合体编码(`lj_target.h:42`):

```c
/* Combined register and spill slot (uint16_t in ir->prev). */
typedef uint32_t RegSP;

#define REGSP(r, s)	((r) + ((s) << 8))
#define regsp_reg(rs)	((rs) & 255)
#define regsp_spill(rs)	((rs) >> 8)
#define regsp_used(rs) \
  (((rs) & ~REGSP(RID_MASK, 0)) != REGSP(RID_NONE, 0))
```

低 8 位是寄存器号(`regsp_reg`),高 24 位是 spill 槽号(`regsp_spill`)。后端寄存器分配器跑完,把"这条 IR 指令的值最终落在哪"塞进 `ir->prev`(这个字段在 IR 构建阶段用于别的,后端复用)。所以恢复时,一条 IR ref 一查 `ir->prev` 就知道值在哪。

`snap_restoreval` 的三分支:

**分支 1:常量。** `irref_isk(ref)`(ref 小于 REF_BIAS 说明是常量)。直接从 IR 里取常量值(`lj_ir_kvalue`),根本不用碰 ExitState。KPTR 特殊处理(存的是指针)。这就是 §5 场景 C。

**分支 2:有 spill 槽。** `ra_hasspill(regsp_spill(rs))`(spill 槽号非 0)。从 `ex->spill[N]` 取值。按类型解释:整数取 4 字节、浮点取 8 字节、GC 对象当指针。这就是 §5 场景 B。

**分支 3:在寄存器里。** 没 spill,看 `regsp_reg(rs)` 寄存器号。从 `ex->gpr[N]` 或 `ex->fpr[N]` 取(GPR 还是 FPR 看类型)。这就是 §5 场景 A。

注意 `regsp_used` 的判定(`lj_target.h:50`):当 RegSP 等于 `REGSP(RID_NONE, 0)`(寄存器号 0 且 spill 槽 0)时,这条 IR"没被分配任何位置"——它可能是个被死代码消除的、或者纯中间结果。`snap_restoreval` 里对 `ra_noreg(r)` 的特判就是处理一种特殊情况:`IR_CONV`(类型转换)的中间结果没分配位置时,递归去取它的操作数再现场转换(`snap_restoreval(... ir->op1, o)` + `setnumV`)。这是 DCE 和 snapshot 协作的边角。

这个三分支结构,正是 §5 三个场景的源码落地。snapshot 记的是"slot → ref",RegSP 记的是"ref → 物理位置(寄存器/spill/常量)",ExitState 存的是"物理位置 → 当前值"。三者串起来,就把值从机器码的世界搬到了解释器的值栈。

### §14 sunk 分配的恢复:snap_unsink

§12 循环里有个特判:`ir->r == RID_SUNK`。这值得单独讲,因为它是 snapshot 恢复里最绕的一部分,也最能体现 snapshot 机制的威力。

背景:sink 优化(P3-11 讲过分配消除)。trace 里如果有个 `t = {}` 然后只往里写不读出来,这个 table 分配是死的——但 sink 优化不是删掉它,而是把它"下沉":不在 trace 里真正分配,只在退出时(如果真的退出了)才现场分配出来。

怎么标记?后端给这种"被下沉的分配"的 IR 打上 `r = RID_SUNK`(`lj_target.h:25`,`RID_SUNK = RID_INIT - 2`,是个特殊寄存器号)。snapshot 恢复时碰到 `ir->r == RID_SUNK`,就知道"这个 slot 不是普通值,是个被推迟的分配,得现场造出来"。

这就是 `snap_unsink`(`lj_snap.c:833`)。核心逻辑(简化):

```c
static void snap_unsink(jit_State *J, GCtrace *T, ExitState *ex,
			SnapNo snapno, BloomFilter rfilt,
			IRIns *ir, TValue *o)
{
  lj_assertJ(ir->o == IR_TNEW || ir->o == IR_TDUP ||
	     ir->o == IR_CNEW || ir->o == IR_CNEWI, ...);
  ...
  {
    IRIns *irs, *irlast;
    GCtab *t = ir->o == IR_TNEW ? lj_tab_new(J->L, ir->op1, ir->op2) :
				  lj_tab_dup(J->L, ir_ktab(&T->ir[ir->op1]));
    settabV(J->L, o, t);
    irlast = &T->ir[T->snap[snapno].ref];
    for (irs = ir+1; irs < irlast; irs++)
      if (irs->r == RID_SINK && snap_sunk_store(T, ir, irs)) {
        ... /* 重建每一次 sunk store */
      }
  }
}
```

它干三件事:

1. **现场分配对象**。`IR_TNEW` → `lj_tab_new`,`IR_TDUP` → `lj_tab_dup`(从常量表模板复制),FFI 的 `IR_CNEW`/`CNEWI` → `lj_cdata_newx`。这就把 sink 推迟掉的分配,在退出时补回来。
2. **重放所有 sunk store**。从这条分配 IR 往后扫,凡是标了 `RID_SINK` 且确认是属于这次分配的 store(`snap_sunk_store` 判断),都重新执行一遍:取出 key、取出 value、往新分配的对象里写。`irlast = &T->ir[T->snap[snapno].ref]` 限定扫描范围——只扫到这张 snapshot 的 ref 为止(snapshot 之后的 store 不属于这个退出点)。
3. **值从哪来?** sunk store 的 value 又是一个 IR ref,用 `snap_restoreval` 把它从 ExitState 捞出来(递归)。所以 unsink 可能递归调用 restoreval。

注意 lj_snap_restore 主循环里还有个去重(`lj_snap.c:984`):

```c
if (ir->r == RID_SUNK) {
  MSize j;
  for (j = 0; j < n; j++)
    if (snap_ref(map[j]) == ref) {  /* De-duplicate sunk allocations. */
      copyTV(L, o, &frame[snap_slot(map[j])]);
      goto dupslot;
    }
  snap_unsink(J, T, ex, snapno, rfilt, ir, o);
dupslot:
continue;
}
```

如果两个 slot 指向同一个 sunk 分配(同一个 ref),只 unsink 一次,第二个直接 copyTV 复制第一个的结果。避免重复分配同一个对象。

unsink 是 snapshot 机制的高阶能力:它不只搬运现成的值,**还能根据 IR 指令现场重建对象**。这让 sink 优化(省掉 trace 内的分配)成为可能,而退出时语义照样正确。

### §15 寄存器重命名:RENAME 链

§12 提到 `rfilt = snap_renamefilter(T, snapno)`。展开讲。

线性扫描寄存器分配(P4-13)是按 IR 顺序一遍扫过去的。扫的过程中,一个变量的"家"可能换:在 IR 第 100 条时它在 rax,到第 200 条时寄存器紧张被搬到 rbx。这种搬运叫**寄存器重命名**,后端用 `IR_RENAME` 指令记录(`lj_asm.c:724` 的 `ra_addrename` 发射)。

`IR_RENAME` 的语义:`op1` 是被搬的 IR ref,`op2` 是从哪个 snapshot 开始生效(`snapno`),`prev` 是搬之前的老位置。RENAME 指令**追加在 IR 末尾**,所以它们在 `T->ir[nins-1]` 往前连续排。

这带来一个问题:snapshot 记的是"slot → ref",ref 是 IR 下标。但同一个 ref,在 trace 的不同位置,它的物理位置(寄存器)可能不一样。退出发生在 snapshot N 这个点,得查"ref 在 snapshot N 那一刻的真实物理位置",而不是它最后的位置。

`snap_renamefilter`(`lj_snap.c:387`)就是干这个:

```c
static BloomFilter snap_renamefilter(GCtrace *T, SnapNo lim)
{
  BloomFilter rfilt = 0;
  IRIns *ir;
  for (ir = &T->ir[T->nins-1]; ir->o == IR_RENAME; ir--)
    if (ir->op2 <= lim)
      bloomset(rfilt, ir->op1);
  return rfilt;
}
```

从 IR 末尾往前扫所有 RENAME,凡 `op2 <= lim`(在目标 snapshot 之前生效的),把它的 `op1`(被搬的 ref)塞进 Bloom filter。Bloom filter 是个快速"可能命中"判断——大多数 ref 不在过滤器里,直接确定没被 rename;在过滤器里的再走慢路径 `snap_renameref` 精确查:

```c
static RegSP snap_renameref(GCtrace *T, SnapNo lim, IRRef ref, RegSP rs)
{
  IRIns *ir;
  for (ir = &T->ir[T->nins-1]; ir->o == IR_RENAME; ir--)
    if (ir->op1 == ref && ir->op2 <= lim)
      rs = ir->prev;
  return rs;
}
```

往前扫,找到**最后一个**匹配的 RENAME(取它的 `prev` 作为老位置)。注意是"最后一个"——因为同一个 ref 可能被搬好几次,退出点生效的是最后一次 ≤ lim 的搬运后的位置。

这套机制保证:**不管寄存器分配中途怎么搬,snapshot 恢复时总能算出退出那一刻每个值的真实物理位置**。

### §16 恢复后的收尾:PC、top、MULTRES

lj_snap_restore 主循环跑完,slot 都填好了,还有几件收尾(`lj_snap.c:1016`):

```c
  /* Compute current stack top. */
  switch (bc_op(*pc)) {
  default:
    if (bc_op(*pc) < BC_FUNCF) {
      L->top = curr_topL(L);
      break;
    }
    /* fallthrough */
  case BC_CALLM: case BC_CALLMT: case BC_RETM: case BC_TSETM:
    L->top = frame + snap->nslots;
    break;
  }
  return pc;
```

`L->top`(值栈当前栈顶)要根据下一条字节码来定:

- 大多数指令:用 `curr_topL` 算(根据帧信息推断)。
- `BC_CALLM`/`CALLMT`/`RETM`/`TSETM`:这些指令用到了 MULTRES(变长返回值数量),栈顶必须精确到 snapshot 记的 `nslots`。LJ_FR2 模式下还有个 `L->base += (map[nent+LJ_BE] & 0xff)` 调整 base。

最后 `return pc` 把"解释器该接着跑的字节码地址"带回去。`lj_trace_exit` 拿到这个 pc,设到 cframe,然后返回一个整数(给汇编侧用:MULTRES 或错误码)。汇编侧(`vm_x64.dasc:2449` 之后)拿到返回值,恢复 BASE、L 等解释器寄存器,跳回解释器 dispatch 循环。整个 side exit 完成。

### §17 snapshot 的另一用途:side trace 录制(P5-18 预告)

snapshot 不只用于退出恢复。**side trace 录制也从父 trace 的 snapshot 起步**。这件事 `lj_snap_replay`(`lj_snap.c:508`)干,详细在 P5-18 讲,这里点一下关联:

当一个退出点被触发够多次(`count` 达到 `hotexit` 阈值),LuaJIT 决定从这长出一条 side trace。新 trace 录制的**起点状态**(各个 slot 的初值)从哪来?——就是父 trace 在这个退出点的 snapshot。`lj_snap_replay` 遍历父 trace 的 snapshot 条目,为每个 slot 发射对应的 IR(常量重放、PVAL 引用、SLOAD 继承),把"退出那一刻的状态"作为新 trace 的 IR 起点。然后新 trace 从这往下录新的路径。

所以 snapshot 一物两用:

- **运行时退出恢复**:把 ExitState 的值搬到解释器栈(`lj_snap_restore`)。
- **编译时 side trace 起点**:把 snapshot 的 ref 关系重放成新 trace 的 IR(`lj_snap_replay`)。

两者读的是同一张 snapshot 表,只是消费者不同。这体现了 snapshot 作为"状态翻译依据"的通用性:它完整描述了退出点状态,谁能用谁用。

---

## 第三部分:为什么这样设计是 sound 的

讲完了实现,现在回答最关键的问题:**凭什么 snapshot 恢复出来的状态,和"假设从一开始就没做、全程解释器跑"的状态一致?** 换句话说,为什么这套机制不会让程序算错?

这是"把动态执行**安全**变成机器码"里"安全"两个字的命门。我们分几层论证。

### §18 不变式一:每个退出点都有 snapshot

第一个保证:**机器码里每一个可能跳出去的位置(guard),都对应一张 snapshot**。

这由 §7 的交错生成机制保证。录制时,每发射一条 guard IR(`irt_isguard`),就标记 `guardemit`;下次 `lj_snap_add` 看到 guardemit 非空,必然新建一张 snapshot(不能合并)。循环回边、trace 衔接点,也强制 `lj_snap_add`。所以**没有"裸 guard"——每个 guard 都有据可查**。

如果一个 guard 跳出去却没有对应的 snapshot,恢复时就不知道该把哪些值搬回栈,结果必错。交错生成从机制上杜绝了这种遗漏。

### §19 不变式二:snapshot 记全了"解释器需要的状态"

第二个保证:**snapshot 记录的字段集合,恰好覆盖解释器接管所需的一切**。

解释器要什么?三类信息:

1. **每个活跃 slot 的值**(slot → IR ref)。`snapshot_slots` 记。
2. **调用栈结构**(每层帧的 PC、ftsz)。`snapshot_framelinks` 记。
3. **下一条要执行的字节码 PC**。帧链接的第一条记。

snapshot 这三类全记了,没有遗漏。特别注意 §8 的差量压缩——它**只省掉不需要恢复的 slot**(只读的、值没变过的),对"解释器需要但 trace 没改"的 slot,要么不记(解释器栈上本来就有原值)、要么记但标 NORESTORE(逻辑等价于不恢复)。压缩只是省空间省时间,**不丢信息**——被省掉的 slot,其值在退出时确实不需要重写(因为 trace 没碰它,解释器栈上还是进入 trace 时的原值,正好就是正确的值)。

这是个精妙的不变式:**snapshot 看似只记了部分 slot,但记下的部分 + 解释器栈上 trace 没碰过的部分 = 完整正确的状态**。

### §20 不变式三:值的物理位置永远能算出来

第三个保证:**对每个 IR ref,snapshot 恢复时总能算出它在退出那一刻的真实物理位置(寄存器/spill/常量)**。

这由三层机制保证:

1. **常量**:`irref_isk(ref)` 直接判定,值在 IR 里,永远能取。
2. **寄存器/spill**:`ir->prev` 存的 RegSP 告诉位置。后端寄存器分配器保证:每条非死 IR 都有确定的 RegSP(要么分了寄存器、要么分了 spill 槽、要么标记 SUNK)。
3. **重命名**:`IR_RENAME` 链 + `snap_renamefilter`/`snap_renameref` 保证:即使中途寄存器搬过,也能回溯到退出那一刻的真实位置。

合起来,任何 IR ref 的值,恢复时都能从 ExitState(寄存器副本 + spill 区)或 IR 常量区里精确取出。不存在"找不到值"的情况。

ExitState 本身由 exit stub 保证完整——它无差别地把**所有** GPR、FPR、spill 槽都存下来(`vm_x64.dasc` 的 exit handler 把 16 个 xmm + 所有 GPR 全 push/spill)。所以"值在哪个寄存器"无论答案是哪个,ExitState 里都有它的副本。

### §21 不变式四:类型/语义一致

第四个保证:**恢复出来的值,类型和语义与"全程解释器跑"一致**。

这体现在 `snap_restoreval` 按类型解释物理值。同一个 8 字节,按整数读和按浮点读结果不同。snapshot 通过 IR ref 知道这个值**应该是什么类型**(`ir->t`),然后按对应类型从 ExitState 取(`irt_isinteger` → 4 字节、`irt_isnum` → 8 字节、GC 对象 → 指针)。类型对齐保证了语义一致。

边角情况也覆盖:

- **soft-float**(LJ_SOFTFP32):浮点占两个 slot,`SNAP_SOFTFPNUM` 标记,恢复时读两个 4 字节拼成 8 字节。
- **key index**(SNAP_KEYINDEX):表遍历用的特殊整数 slot,恢复成带特殊 tag 的数(`o->u32.hi = LJ_KEYINDEX`)。
- **frame slot**(SNAP_FRAME):slot 是帧底,不能写普通值,要写成帧链接 ftsz。
- **CONV NUM_INT**:中间转换结果没分配位置时,递归取操作数现场转。

每个特判都是为了保证:恢复出来的值,和"如果没做 JIT、全程解释器在那个点看到的状态"逐字节一致。

### §22 不变式五:sunk 分配也能正确重建

第五个保证,是 sink 优化下仍 sound。

sink 优化让 trace 里的 `t = {}; t.x = 1` 这种代码不真正分配 table(省时间)。但退出时如果解释器需要这个 t,它必须是个真实存在的 table 对象。`snap_unsink`(§14)现场 `lj_tab_new` 出来,并重放所有 sunk store。重放用的是 snapshot 记的 value ref,值从 ExitState 取——所以重建出来的 table,**内容和"如果 trace 真的执行了分配和赋值"完全一样**。

这个保证让 sink 优化(一个激进的性能优化)不破坏正确性:trace 内省了分配,退出时补回来,语义等价。

### §23 五个不变式合起来

把这五条合起来,snapshot 恢复的 sound 性就完整了:

1. 每个 guard 有 snapshot(不遗漏退出点)。
2. snapshot 记全解释器需要的状态(不丢信息)。
3. 每个 IR ref 的值能物理取到(寄存器/spill/常量都覆盖)。
4. 类型语义对齐(按 IR 类型解释物理值)。
5. sunk 分配能重建(sink 优化不破坏正确性)。

所以结论:**guard 触发后,snapshot 恢复出来的解释器状态,和"假设这个 trace 从来没编译过、全程解释器跑"在退出那一刻的状态,逐字节一致。** 解释器从这个状态接着跑,结果必然正确。这就是"机器码要么和解释器一样、要么退回解释器"(P0-01 §9 的关键不变式)在 snapshot 这一环的落地。

而且这套保证是**静态可验证**的——不需要运行时测试,从代码结构和不变式就能推导出来。这就是为什么 LuaJIT 敢用这么激进的优化(乐观假设 + sink + 寄存器重命名),却几乎不出正确性 bug:sound 性写在设计里,不是靠运气。

---

## 第四部分:★对照 + 回扣主线

### §24 ★对照一:官方 Lua(切"有没有退出恢复")

官方 Lua 是纯解释器,**根本没有 snapshot 这回事**。它不需要,因为它从来没编译出过机器码——每个变量永远在值栈上,从来不存在"数据在寄存器、要搬回栈"的问题。

所以 snapshot 是 JIT 引入的**纯额外成本**。官方 Lua 的每一纳秒都花在执行字节码上;LuaJIT 要额外付出:录制时拍 snapshot(`lj_snap_add` 在每个 guard 调一次)、存储 snapshot 数组(每个 trace 占 `snap` + `snapmap` 两块内存)、退出时恢复(`lj_snap_restore` 遍历条目)。这些是 JIT 为了"快"付出的"安全税"。

但这个税很划算:snapshot 的差量压缩(§8)让它内存占用很小(一个 trace 通常几十到几百字节),恢复只在实际退出时才发生(绝大多数 guard 不触发,不付出恢复成本)。所以**平均下来,snapshot 的开销远小于机器码省下的时间**——这正是 JIT 划算的根源之一。

对照还点出一个设计取舍:官方 Lua 的"简单"(无 snapshot)来自它不优化;LuaJIT 的"快"来自它优化,而优化带来的复杂性(snapshot/side exit/寄存器恢复)被精心封装在 `lj_snap.c` 这一个文件里(1034 行),不泄漏到解释器。解释器照常跑,不知道 snapshot 存在;snapshot 只在退出时被唤醒。两个世界干净分离。

### §25 ★对照二:JVM/V8 的 deoptimization(frame reconstruction)

JVM 和 V8 也有"假设失败要退回解释器"的问题,它们叫 **deoptimization**(去优化)。但做法和 LuaJIT 截然不同,对照能看清 trace JIT 的取舍。

**JVM/V8 的做法:frame reconstruction(帧重建)。**

JVM/V8 是 method JIT,编译时以整个方法为单位。它们编译出的机器码,把方法的局部变量表、操作数栈,**编译成了寄存器分配后的物理位置**(和 LuaJIT 类似)。但它们的机器码里**不预存"每个退出点该恢复什么"的表**(没有 snapshot)。

deopt 发生时(比如某个类型假设破了),JVM/V8 的机器码跳到一个 deopt handler。这个 handler 拿着**当前的寄存器值 + 编译期记录的调试信息**(局部变量到寄存器的映射表、字节码位置映射表 / Bytecode to PC map),**现场反推**出"如果用解释器跑,此刻每个局部变量和操作数栈该是什么"。这个过程叫 frame reconstruction——重建一个解释器帧。

对比 LuaJIT:

| 维度 | LuaJIT(snapshot restore) | JVM/V8(frame reconstruction) |
|---|---|---|
| 信息存哪 | 每个 guard 一张预编译的 snapshot 表 | 一份全局的 bytecode-PC 映射 + 调试信息 |
| 恢复怎么做 | 遍历 snapshot 条目,按 RegSP 取值 | 现场反推:遍历寄存器,按映射表塞回解释器帧 |
| 信息何时生成 | 录制时交错生成(每 guard 一张) | 编译时统一生成(整方法一份) |
| 退出快不快 | 快(snapshot 精简、直接查表) | 较慢(要反推整个帧) |
| 内存 | 每 guard 一张(差量压缩) | 每方法一份映射表(较固定) |
| 编译单位 | trace(线性路径) | method(整个方法) |

核心差异是**"预计算"vs"现场算"**:

- LuaJIT 在录制时就**预计算**好了每个退出点的恢复表(snapshot),退出时只是查表执行,快。
- JVM/V8 存的是"通用的位置映射",退出时**现场**反推出完整帧,慢一点但灵活(同一个映射表能给方法里任意位置用)。

为什么 LuaJIT 能预计算?因为 trace 是**线性**的——每条 IR 指令的位置确定,录制时就能精确知道"此刻每个 slot 是哪个 ref"。method JIT 的控制流是图(有 if/else/循环各种分叉),同一个方法内不同退出点的状态差很多,预计算每条都存代价大,不如存一份映射现场算。

这恰好印证了 trace vs method 的根本分歧(P0-01 §10):trace 的线性性让它适合**预计算 snapshot**(快恢复),method 的复杂性让它只能**现场 reconstruction**(慢但通用)。两种 JIT 各自的退出策略,是它们编译单位选择的直接后果。

还有一个对照点:**side trace 的起点**。LuaJIT 的 snapshot 一物两用(退出恢复 + side trace 录制起点,§17),因为 snapshot 完整描述了退出点状态。JVM/V8 没有等价物——它们 deopt 后直接回解释器,不会"从 deopt 点长出一条新的编译路径"(JVM/V8 重新编译是基于新的 profile,不是从 deopt 点衔接)。这是 trace JIT 独有的"渐进优化"能力(P5-18 讲 side trace),根源正是 snapshot 把退出点状态完整留了下来。

### §26 回扣主线

把本章放回全书主线:**把动态执行安全变成机器码**。

snapshot 是"安全"两个字的运行时命脉。机器码的快(P2–P4 全书讲怎么生成最快机器码)依赖一个前提:**假设破了能全身而退**。没有 snapshot,guard 触发后机器码就卡死——要么继续跑算错,要么崩溃。snapshot 让"退"这个动作变得可行且正确:它把机器码世界的状态(寄存器/spill),无损翻译成解释器世界的状态(值栈),让解释器无缝接管。

具体到本章覆盖的几个点:

- **snapshot 记什么**:slot → IR ref 的映射(§5–§6),是"状态翻译表"的精确定义。
- **怎么编码**:两层 SnapShot + SnapEntry 数组,差量压缩(§6、§8),让 snapshot 在内存上划算。
- **怎么生成**:录制时交错拍,每个 guard 一张(§7),保证不遗漏。
- **怎么恢复**:lj_snap_restore 遍历条目,snap_restoreval 按 RegSP 从 ExitState 取值(§12–§13),寄存器重命名靠 RENAME 链回溯(§15),sunk 分配现场重建(§14)。
- **为什么 sound**:五个不变式合起来,保证恢复状态与"全程解释器"逐字节一致(§18–§23)。

snapshot 还把 P5 这一篇的三章串起来:**guard**(P5-16,机器码里插检查)→ **snapshot**(P5-17,失败时恢复状态)→ **side trace**(P5-18,失败够多次就从 snapshot 起点长新 trace)。三章合起来,就是 trace JIT 完整的"运行时安全网":检查、退避、再优化。一张 snapshot 同时服务前两者(退出恢复)和第三者(side trace 起点),是这套机制的枢纽。

而 snapshot 的设计哲学——**预计算 + 差量压缩 + 与解释器世界干净分离**——也是整个 LuaJIT 的哲学:把复杂性和性能优化封装在 JIT 侧,解释器侧保持简单正确;两者靠 snapshot 这种"翻译层"协作。这种分层让 LuaJIT 既能极快(JIT 侧尽情优化),又能极稳(解释器侧永远正确保底)。

下一章我们看 snapshot 的第二个用途:[P5-18 side trace:从退出点录制新 trace](P5-18-side-trace从退出点录制新trace.md)——当一个 guard 经常失败,失败到够热,LuaJIT 就从那张 snapshot 出发,录制一条新的 trace,让那个"经常走的冷分支"也变成机器码。这是 JIT"越跑越快"的引擎,而它的起点,正是本章讲透的这张 snapshot。

---

*上一章 [P5-16 guard 运行时检查与 side exit](P5-16-guard运行时检查与side-exit.md):guard 在机器码里怎么检查、失败怎么跳到 exit stub。下一章 [P5-18 side trace:从退出点录制新 trace](P5-18-side-trace从退出点录制新trace.md):snapshot 的第二个用途——从退出点长出新 trace。*
