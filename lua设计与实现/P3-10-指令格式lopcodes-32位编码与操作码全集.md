# P3-10 指令格式 lopcodes:32 位编码与操作码全集

> **本书主线**:统一与精简换小而快。**二分法**:编译侧(源码→字节码)↔ 执行侧(VM 执行 + 管值生命周期)。**本章位置**:执行侧入口，承接 P2 编译器产出的字节码，为 P3-11 解释器循环铺路。**★对照**:CPython 的栈式 wordcode。**源码**:lua-5.5.0。**基调**:纯直球，不用比喻。

---

## 一、这章解决什么问题

P2 用四篇把一段 Lua 源码走完了词法、语法、代码生成，最后吐出来的，是一串 **32 位整数**。编译器 `lcode.c` 里每条指令都装在一个叫 `Instruction` 的变量里，函数 `Proto *f` 的 `f->code[]` 数组就是一段函数的完整字节码。执行侧 P3 要做的事，就是反复地从 `f->code[]` 里取出一条 `Instruction`，拆开它，搞清楚它是哪个操作码、操作数是哪几个寄存器或常量，然后跳到对应的执行分支。

这一步是执行侧的第一道关口，它的物理形态直接决定了整条执行链的形态。本章要回答一个非常具体的问题：

**一条 Lua 指令只有 32 位，操作码、若干个操作数(寄存器号、常量索引、跳转偏移)是怎么挤进这 32 位的？挤进去之后，VM 怎么无损地取出来？**

这个问题不解决，P3-11 的 `luaV_execute` 大 switch 就成了空中楼阁——所有 `vmcase(OP_XXX)` 分支里第一件事都是 `GETARG_A`/`GETARG_B`/`GETARG_C`，这些宏展开成什么、取的是哪几位，正是本章的内容。

为什么必须用固定 32 位？因为 Lua 要嵌进任何宿主，字节码要能跨平台 dump/load(`luac`、`string.dump`)。固定 32 位让指令长度恒定，取指就是一次 `*pc++`，无须解码长度；也让 dump 出来的字节码字节序确定、可移植。代价是每个字段的位宽是死的，必须在"操作码够不够多""寄存器号够不够大""常量表索引够不够深"之间精打细算地分配这 32 位。Lua 5.5 的分配方案，就是 `lopcodes.h` 这一个文件的全部内容。

本章四段：①讲清楚问题(已完成);②逐字段、逐格式、逐宏、逐操作码拆开 `lopcodes.h`/`lopcodes.c`/`lopnames.h` 三个文件，把一条真实指令的 32 位逐位展开；③论证这套编码为什么 sound(位宽够用、RK 约定省指令、EXTRAARG 突破限制);④对照 CPython 的 16 位栈式 wordcode，回扣主线。

一个贯穿全章的事实要先点明：**Lua 5.5 的指令格式与老资料(讲 5.3/5.4 的书和博客)有实质差异**。老资料普遍讲"只有四种格式 iABC/iABx/iAsBx/iAx",RK 约定是"B 或 C 的高位为 1 表示常量"。5.5 不是这样：格式增加到 **六种**(新增 `ivABC` 和 `isJ`),RK 约定改成 **独立的 k 位**。这些差异本章会逐处标出。源码是唯一依据。

---

## 二、源码怎么实现：把 32 位拆开

### 2.1 Instruction 类型：32 位无符号

先看指令本身的类型。`lopcodes.h` 开头的注释直接定调(`lopcodes.h:14-17`):

```c
/*===========================================================================
  We assume that instructions are unsigned 32-bit integers.
  All instructions have an opcode in the first 7 bits.
```

`Instruction` 这个类型的定义不在 `lopcodes.h`，而在 `llimits.h`。它依赖 `l_uint32`(`llimits.h:225`):

```c
/*
** An unsigned with (at least) 4 bytes
*/
#if LUAI_IS32INT
typedef unsigned int l_uint32;
#else
typedef unsigned long l_uint32;
#endif
```

`l_uint32` 是"至少 4 字节的无符号整数"。在 32 位 int 的平台上就是 `unsigned int`(4 字节，正好 32 位)；在 int 非 32 位的平台上退化为 `unsigned long`。`Instruction` 本身的 typedef 在 `lobject.h`(本章不展开)，底层就是这个 `l_uint32`。后面所有位运算都建立在"它是 32 位无符号"这个假设上。

代码里频繁出现的两个转换宏(`llimits.h:134`、`llimits.h:141`):

```c
#define cast_uint(i)	cast(unsigned int, (i))
#define cast_Inst(i)	cast(Instruction, (i))
```

`cast_Inst` 在所有 `CREATE_*` 宏里把操作码、操作数强制成 `Instruction` 再移位拼装。

### 2.2 六种指令格式：位分配全景

整张格式表在 `lopcodes.h:14-32`，这是全章最重要的一段注释，逐字贴出：

```
        3 3 2 2 2 2 2 2 2 2 2 2 1 1 1 1 1 1 1 1 1 1 0 0 0 0 0 0 0 0 0 0
        1 0 9 8 7 6 5 4 3 2 1 0 9 8 7 6 5 4 3 2 1 0 9 8 7 6 5 4 3 2 1 0
iABC          C(8)     |      B(8)     |k|     A(8)      |   Op(7)     |
ivABC         vC(10)    |     vB(6)   |k|     A(8)      |   Op(7)     |
iABx                Bx(17)               |     A(8)      |   Op(7)     |
iAsBx              sBx (signed)(17)      |     A(8)      |   Op(7)     |
iAx                           Ax(25)                     |   Op(7)     |
isJ                           sJ (signed)(25)            |   Op(7)     |
```

把位号从右(最低位 0)往左数，读法如下。

**所有格式共有的部分：最低 7 位(位 0–6)是操作码 Op。** 7 位能编码 128 个操作码，Lua 5.5 实际只用了 83 个(`lopnames.h`，后文详列)，够用且留了余量。

**几乎所有格式都有的部分：位 7–14(8 位)是操作数 A。** 8 位 → 最大寄存器号 255。这直接决定了"一个 Lua 函数最多有多少个寄存器"——`MAXARG_A = (1<<8)-1 = 255`。`MAX_FSTACK` 就是它(`lopcodes.h:210`),`NO_REG = MAX_FSTACK`(`lopcodes.h:215`)标记"无效寄存器"。

六种格式的差异在 A 之上那 17 位(位 15–31)怎么用：

- **iABC**:把高 17 位切成三段——1 位 k(`POS_k`，位 15)、B(8 位，位 16–23)、C(8 位，位 24–31)。这是最常用的格式，绝大多数算术、表访问、比较用 iABC。
- **ivABC**:同样 1 位 k + A，但把高 16 位切成 vB(6 位)+ vC(10 位)。vB/vC 位宽与 B/C 不同。这是个**新格式**(5.5 新增，老资料没有)，只有 `NEWTABLE` 和 `SETLIST` 两条指令用——因为表的哈希大小和数组大小需要更宽的字段。
- **iABx**:高 17 位合成一个 Bx(17 位)。用于需要一个较大常量索引或子原型索引的指令：`LOADK`、`CLOSURE`、`FORPREP`/`FORLOOP` 等。
- **iAsBx**:布局和 iABx 完全一样(Bx 17 位)，但 Bx 被当成**有符号**解释(sBx = Bx − OFFSET_sBx)。只有 `LOADI`/`LOADF` 用它(直接把一个整数/浮点立即数编进指令)。
- **iAx**:位 7–31 合成一个 Ax(25 位)。**只用于 `EXTRAARG` 一条指令**，作为前一条指令的扩展参数，承载超大的常量/原型索引。
- **isJ**:位 7–31 合成一个 sJ(25 位有符号)。**只用于 `JMP` 一条指令**(5.5 新增格式，老资料讲 5.3/5.4 时 JMP 走的是 iAsBx)。

`enum OpMode` 把这六种格式枚举出来(`lopcodes.h:36`):

```c
enum OpMode {iABC, ivABC, iABx, iAsBx, iAx, isJ};
```

注释里还解释了命名('v' = variant,'s' = signed,'x' = extended)和有符号数的表示法("excess K" 偏移编码):一个有符号参数用无符号写出，真值 = 写出值 − K，其中 K 是该无符号参数最大值的一半向下取整。下一小节用宏落地。

### 2.3 位宽与位置：SIZE_* 和 POS_* 宏

把上面那些位分配转成可计算的宏(`lopcodes.h:42-66`):

```c
#define SIZE_C		8
#define SIZE_vC		10
#define SIZE_B		8
#define SIZE_vB		6
#define SIZE_Bx		(SIZE_C + SIZE_B + 1)
#define SIZE_A		8
#define SIZE_Ax		(SIZE_Bx + SIZE_A)
#define SIZE_sJ		(SIZE_Bx + SIZE_A)

#define SIZE_OP		7

#define POS_OP		0

#define POS_A		(POS_OP + SIZE_OP)
#define POS_k		(POS_A + SIZE_A)
#define POS_B		(POS_k + 1)
#define POS_vB		(POS_k + 1)
#define POS_C		(POS_B + SIZE_B)
#define POS_vC		(POS_vB + SIZE_vB)

#define POS_Bx		POS_k

#define POS_Ax		POS_A

#define POS_sJ		POS_A
```

把这套宏算清楚：

- `SIZE_OP=7`，`POS_OP=0` → Op 占位 0–6。
- `SIZE_A=8`，`POS_A=POS_OP+SIZE_OP=7` → A 占位 7–14。
- `POS_k=POS_A+SIZE_A=15` → k 位是位 15(单 bit)。
- `POS_B=POS_k+1=16`，`SIZE_B=8` → B 占位 16–23。`POS_C=POS_B+SIZE_B=24`，`SIZE_C=8` → C 占位 24–31。
- 变体：`POS_vB=16`，`SIZE_vB=6` → vB 占位 16–21;`POS_vC=22`，`SIZE_vC=10` → vC 占位 22–31。
- `SIZE_Bx = SIZE_C+SIZE_B+1 = 8+8+1 = 17`，`POS_Bx=POS_k=15` → Bx 占位 15–31(17 位)。
- `SIZE_Ax = SIZE_Bx+SIZE_A = 17+8 = 25`，`POS_Ax=POS_A=7` → Ax 占位 7–31(25 位)。
- `SIZE_sJ = SIZE_Bx+SIZE_A = 25`，`POS_sJ=POS_A=7` → sJ 占位 7–31(25 位)。

注意一个关键点：**Bx/Ax/sJ 的位置都从 `POS_k` 或 `POS_A` 开始，把 k 位和 A 位"吞"进了自己的大字段**。也就是说同一条 32 位指令，是 iABC 还是 iABx，完全取决于操作码——操作码决定了它后面的位怎么切分。这正是为什么需要 opmode 表来记录每个操作码的格式(见 2.6)。

最大值宏(`lopcodes.h:82-114`):

```c
#if L_INTHASBITS(SIZE_Bx)
#define MAXARG_Bx	((1<<SIZE_Bx)-1)
#else
#define MAXARG_Bx	INT_MAX
#endif

#define OFFSET_sBx	(MAXARG_Bx>>1)         /* 'sBx' is signed */
...
#define MAXARG_A	((1<<SIZE_A)-1)
#define MAXARG_B	((1<<SIZE_B)-1)
#define MAXARG_vB	((1<<SIZE_vB)-1)
#define MAXARG_C	((1<<SIZE_C)-1)
#define MAXARG_vC	((1<<SIZE_vC)-1)
#define OFFSET_sC	(MAXARG_C >> 1)

#define int2sC(i)	((i) + OFFSET_sC)
#define sC2int(i)	((i) - OFFSET_sC)
```

把这些数字算出来贴给读者：

| 字段 | 位宽 | 最大值(无符号) | 有符号偏移 |
|---|---|---|---|
| Op | 7 | 127(实际用 83) | — |
| A | 8 | 255(`MAXARG_A`) | — |
| k | 1 | 1 | — |
| B | 8 | 255(`MAXARG_B`) | — |
| C | 8 | 255(`MAXARG_C`) | `OFFSET_sC = 127`，`int2sC`/`sC2int` 转换 |
| vB | 6 | 63(`MAXARG_vB`) | — |
| vC | 10 | 1023(`MAXARG_vC`) | — |
| Bx | 17 | 131071(`MAXARG_Bx`) | `OFFSET_sBx = 65535` |
| Ax | 25 | 33554431(`MAXARG_Ax`) | — |
| sJ | 25 | 33554431(`MAXARG_sJ`) | `OFFSET_sJ = 16777215` |

两个有符号字段值得点出：`sBx`(用于 LOADI/LOADF)和 `sJ`(用于 JMP)。它们的真值范围都是 `[−16777216, +16777215]` 量级。对 JMP 而言，这意味着一条跳转指令可以在 ±16M 条指令范围内跳，远远超过任何实际函数的体量，后文讲 EXTRAARG 时还会回来看这个"够不够"的问题。

### 2.4 参数宏：GETARG_*/SETARG_*/CREATE_*

位宽和位置定下来后，取/存操作数就是位移加掩码。底层的两个原语(`lopcodes.h:134-136`):

```c
#define getarg(i,pos,size)	(cast_int(((i)>>(pos)) & MASK1(size,0)))
#define setarg(i,v,pos,size)	((i) = (((i)&MASK0(size,pos)) | \
                ((cast_Inst(v)<<pos)&MASK1(size,pos))))
```

`MASK1` 和 `MASK0`(`lopcodes.h:118-121`)造掩码：

```c
/* creates a mask with 'n' 1 bits at position 'p' */
#define MASK1(n,p)	((~((~(Instruction)0)<<(n)))<<(p))

/* creates a mask with 'n' 0 bits at position 'p' */
#define MASK0(n,p)	(~MASK1(n,p))
```

`MASK1(8, 16)` 就是"位 16–23 全 1，其余全 0"的掩码。`getarg` 先右移把目标位移到最低位，再 `& MASK1` 取出来；`setarg` 先把目标位清零(`& MASK0`)，再把新值移位或进去。这是位域操作的标准套路。

操作码的取/存(`lopcodes.h:127-129`):

```c
#define GET_OPCODE(i)	(cast(OpCode, ((i)>>POS_OP) & MASK1(SIZE_OP,0)))
#define SET_OPCODE(i,o)	((i) = (((i)&MASK0(SIZE_OP,POS_OP)) | \
		((cast_Inst(o)<<POS_OP)&MASK1(SIZE_OP,POS_OP))))
```

各字段的取/存宏就建立在 `getarg`/`setarg` 之上。A 字段最直接(`lopcodes.h:138-139`):

```c
#define GETARG_A(i)	getarg(i, POS_A, SIZE_A)
#define SETARG_A(i,v)	setarg(i, v, POS_A, SIZE_A)
```

B、C 字段带一个 `check_exp` 断言，保证只在格式匹配时才取(`lopcodes.h:141-155`):

```c
#define GETARG_B(i)  \
	check_exp(checkopm(i, iABC), getarg(i, POS_B, SIZE_B))
#define GETARG_vB(i)  \
	check_exp(checkopm(i, ivABC), getarg(i, POS_vB, SIZE_vB))
#define GETARG_sB(i)	sC2int(GETARG_B(i))
#define SETARG_B(i,v)	setarg(i, v, POS_B, SIZE_B)
#define SETARG_vB(i,v)	setarg(i, v, POS_vB, SIZE_vB)

#define GETARG_C(i)  \
	check_exp(checkopm(i, iABC), getarg(i, POS_C, SIZE_C))
#define GETARG_vC(i)  \
	check_exp(checkopm(i, ivABC), getarg(i, POS_vC, SIZE_vC))
#define GETARG_sC(i)	sC2int(GETARG_C(i))
#define SETARG_C(i,v)	setarg(i, v, POS_C, SIZE_C)
#define SETARG_vC(i,v)	setarg(i, v, POS_vC, SIZE_vC)
```

`check_exp` 在调试构建里检查"这条指令的格式确实是 iABC"，防止对一条 iABx 指令调 `GETARG_B`。`GETARG_sB`/`GETARG_sC` 把 B/C 当有符号解释(走 `sC2int`，减去 `OFFSET_sC`)，用于 `ADDI R A B sC` 这类"操作数是一个有符号立即数"的指令。

k 位(`lopcodes.h:157-159`):

```c
#define TESTARG_k(i)	(cast_int(((i) & (1u << POS_k))))
#define GETARG_k(i)	getarg(i, POS_k, 1)
#define SETARG_k(i,v)	setarg(i, v, POS_k, 1)
```

`TESTARG_k` 只测非零(返回 0 或非零值，用于 `if`),`GETARG_k` 取 0/1。这个 k 位是 5.5 的核心机制之一，下一节专门讲。

Bx/Ax/sBx/sJ(`lopcodes.h:161-174`):

```c
#define GETARG_Bx(i)	check_exp(checkopm(i, iABx), getarg(i, POS_Bx, SIZE_Bx))
#define SETARG_Bx(i,v)	setarg(i, v, POS_Bx, SIZE_Bx)

#define GETARG_Ax(i)	check_exp(checkopm(i, iAx), getarg(i, POS_Ax, SIZE_Ax))
#define SETARG_Ax(i,v)	setarg(i, v, POS_Ax, SIZE_Ax)

#define GETARG_sBx(i)  \
	check_exp(checkopm(i, iAsBx), getarg(i, POS_Bx, SIZE_Bx) - OFFSET_sBx)
#define SETARG_sBx(i,b)	SETARG_Bx((i),cast_uint((b)+OFFSET_sBx))

#define GETARG_sJ(i)  \
	check_exp(checkopm(i, isJ), getarg(i, POS_sJ, SIZE_sJ) - OFFSET_sJ)
#define SETARG_sJ(i,j) \
	setarg(i, cast_uint((j)+OFFSET_sJ), POS_sJ, SIZE_sJ)
```

注意 `GETARG_sBx` 和 `GETARG_sJ` 是怎么把无符号转有符号的：取出无符号值，减去偏移量(`OFFSET_sBx`/`OFFSET_sJ`)。对应地，`SETARG_sBx`/`SETARG_sJ` 在写入时加上偏移量。这就是"excess K"编码的完整往返：`sJ 真值 + OFFSET_sJ` 存进指令位，`指令位 − OFFSET_sJ` 还原成真值。负偏移能表示出来，是因为加了偏移量后存的是无符号(不会下溢)。

最后是 CREATE 系列宏——编译器发射指令时用它们从零拼出一条指令(`lopcodes.h:177-198`):

```c
#define CREATE_ABCk(o,a,b,c,k)	((cast_Inst(o)<<POS_OP) \
			| (cast_Inst(a)<<POS_A) \
			| (cast_Inst(b)<<POS_B) \
			| (cast_Inst(c)<<POS_C) \
			| (cast_Inst(k)<<POS_k))

#define CREATE_vABCk(o,a,b,c,k)	((cast_Inst(o)<<POS_OP) \
			| (cast_Inst(a)<<POS_A) \
			| (cast_Inst(b)<<POS_vB) \
			| (cast_Inst(c)<<POS_vC) \
			| (cast_Inst(k)<<POS_k))

#define CREATE_ABx(o,a,bc)	((cast_Inst(o)<<POS_OP) \
			| (cast_Inst(a)<<POS_A) \
			| (cast_Inst(bc)<<POS_Bx))

#define CREATE_Ax(o,a)		((cast_Inst(o)<<POS_OP) \
			| (cast_Inst(a)<<POS_Ax))

#define CREATE_sJ(o,j,k)	((cast_Inst(o) << POS_OP) \
			| (cast_Inst(j) << POS_sJ) \
			| (cast_Inst(k) << POS_k))
```

注意几件事：①所有 CREATE 都以 `cast_Inst(o)<<POS_OP` 开头，把操作码放最低 7 位；②`CREATE_ABCk`/`CREATE_vABCk` 带末尾的 `k`，把 k 位一起拼进去；③没有 `CREATE_iAsBx`，因为 iAsBx 的位布局和 iABx 一模一样，只是解释时有符号——编译器侧(`lcode.c:432`)直接用 `CREATE_ABx` 发射，写进去的是 `Bc + OFFSET_sBx`；④`CREATE_sJ` 带一个 k 参数，因为 5.5 的 JMP 也有 k 位(虽然 opmode 表里 JMP 的 k 位用途较少，主要是为条件跳转留口子)。

编译器侧的发射函数包了一层断言(`lcode.c:399-453`)，只贴签名和关键断言：

```c
int luaK_codeABCk (FuncState *fs, OpCode o, int A, int B, int C, int k) {
  lua_assert(getOpMode(o) == iABC);
  lua_assert(A <= MAXARG_A && B <= MAXARG_B &&
             C <= MAXARG_C && (k & ~1) == 0);
  return luaK_code(fs, CREATE_ABCk(o, A, B, C, k));
}


int luaK_codevABCk (FuncState *fs, OpCode o, int A, int B, int C, int k) {
  lua_assert(getOpMode(o) == ivABC);
  lua_assert(A <= MAXARG_A && B <= MAXARG_vB &&
             C <= MAXARG_vC && (k & ~1) == 0);
  return luaK_code(fs, CREATE_vABCk(o, A, B, C, k));
}
...
static int codesJ (FuncState *fs, OpCode o, int sj, int k) {
  int j = sj + OFFSET_sJ;
  lua_assert(getOpMode(o) == isJ);
  lua_assert(j <= MAXARG_sJ && (k & ~1) == 0);
  return luaK_code(fs, CREATE_sJ(o, j, k));
}
```

`(k & ~1) == 0` 保证 k 只能是 0 或 1。`getOpMode(o) == iABC` 保证不会拿一个 iABx 的操作码塞进 `CREATE_ABCk`。这些断言在调试构建里守护着编码的一致性——这也是"为什么 sound"那一节的内容。

### 2.5 一条真实指令的 32 位逐位拆开：GETTABLE

光看宏太抽象，把一条真实指令拆开看。Lua 代码 `local x = t[k]`(t 在 R0,k 在 R1,x 落在 R2)编译出来大致是：

```
GETTABLE R2 R0 R1      -- R2 := R[B][R[C]]  即 R2 := R0[R1]
```

`GETTABLE` 是 iABC 格式(见 `lopcodes.h:248` 和 opmode 表)，操作数：Op=`OP_GETTABLE`、A=2、B=0、C=1、k=0。

从 `lopnames.h` 数过去，`OP_GETTABLE` 是第 13 个操作码(从 0 开始，MOVE=0, LOADI=1, ..., GETTABLE=12)。所以 Op=12。逐位写出来(位 31 在左，位 0 在右):

```
位号:  31       24 23       16 15  14       7 6      0
字段:  |  C(8)  |  |  B(8)  | k |  | A(8)  | | Op(7) |
值:       1            0      0       2        12
二进制:  00000001   00000000  0   00000010   0001100
```

把 32 位拼成两个 16 进制字：`00000001 00000000 0 00000010 0001100` = `0x010008C`(十进制 16779660)。这条指令在 `f->code[]` 里就存成这个 32 位整数。

VM 执行时(`lvm.c:1311` 的 `vmcase(OP_GETTABLE)`)做的第一件事，就是用本章这些宏把它拆回去：

```c
vmcase(OP_GETTABLE) {
  ...
  TValue *rb = vRB(i);   /* R[B] = R0,即表 t */
  TValue *rc = RC(i);    /* R[C] 的地址 = base+1,即 k */
  ...
}
```

其中 `vRB(i)` 和 `RC(i)` 是 `lvm.c` 里基于本章宏再包一层的快捷宏(`lvm.c:1100-1108`，稍后贴)。`vRB(i)` 内部展开成 `s2v(RB(i))`，`RB(i)` 展开成 `base+GETARG_B(i)`，而 `GETARG_B(i)` 就是把位 16–23 取出来得到 0。一条指令从编码到执行，走的就是这条闭环：`CREATE_ABCk` 把 0/2/0/1/0 拼成 32 位 → 存进 code[] → `*pc` 取出 → `GETARG_*` 拆回 0/2/0/1 → 执行。

### 2.6 opmode 表：每个操作码一个字节的属性

光知道怎么拆还不够，VM 在分发前还要知道"这条指令是什么格式、有哪些副作用"。这些信息集中在 `luaP_opmodes` 数组里——每个操作码一个字节，编码它的所有属性(`lopcodes.h:415-432`):

```c
/*
** masks for instruction properties. The format is:
** bits 0-2: op mode
** bit 3: instruction set register A
** bit 4: operator is a test (next instruction must be a jump)
** bit 5: instruction uses 'L->top' set by previous instruction (when B == 0)
** bit 6: instruction sets 'L->top' for next instruction (when C == 0)
** bit 7: instruction is an MM instruction (call a metamethod)
*/

LUAI_DDEC(const lu_byte luaP_opmodes[NUM_OPCODES];)

#define getOpMode(m)	(cast(enum OpMode, luaP_opmodes[m] & 7))
#define testAMode(m)	(luaP_opmodes[m] & (1 << 3))
#define testTMode(m)	(luaP_opmodes[m] & (1 << 4))
#define testITMode(m)	(luaP_opmodes[m] & (1 << 5))
#define testOTMode(m)	(luaP_opmodes[m] & (1 << 6))
#define testMMMode(m)	(luaP_opmodes[m] & (1 << 7))
```

8 位的含义：

- 位 0–2(`& 7`):指令格式，值 0–5 对应 `enum OpMode` 的 iABC/ivABC/iABx/iAsBx/iAx/isJ。
- 位 3(`testAMode`):这条指令是否设置寄存器 A(即 A 是"目的"而非"源")。区分这个是为了寄存器活跃性分析、调试器、JIT 等。
- 位 4(`testTMode`):是否是 test 指令——紧跟它后面必须是一条 JMP。EQ/LT/LE/TEST/TESTSET/EQK/EQI/... 这些条件判断置位。
- 位 5(`testITMode`):input-top，是否读取上一条指令设的 `L->top`(用于多返回值场景)。CALL/RETURN/SETLIST 这类用。
- 位 6(`testOTMode`):output-top，是否为下一条指令设 `L->top`。CALL/TAILCALL 用。
- 位 7(`testMMMode`):是否是元方法指令(MMBIN/MMBINI/MMBINK)。

表的生成靠一个宏 `opmode`(`lopcodes.c:16-17`)，把六个参数按位排好：

```c
#define opmode(mm,ot,it,t,a,m)  \
    (((mm) << 7) | ((ot) << 6) | ((it) << 5) | ((t) << 4) | ((a) << 3) | (m))
```

注意参数顺序和位顺序：调用时写成 `opmode(mm, ot, it, t, a, m)`，宏里 mm 在最高位(7),m 在最低位(0–2)。完整表(`lopcodes.c:22-109`)摘几行看：

```c
LUAI_DDEF const lu_byte luaP_opmodes[NUM_OPCODES] = {
/*       MM OT IT T  A  mode		   opcode  */
  opmode(0, 0, 0, 0, 1, iABC)		/* OP_MOVE */
 ,opmode(0, 0, 0, 0, 1, iAsBx)		/* OP_LOADI */
 ,opmode(0, 0, 0, 0, 1, iABC)		/* OP_GETTABLE */
 ,opmode(0, 0, 0, 0, 1, ivABC)		/* OP_NEWTABLE */
 ,opmode(0, 1, 1, 0, 1, iABC)		/* OP_CALL */
 ,opmode(0, 0, 0, 0, 0, isJ)		/* OP_JMP */
 ,opmode(0, 0, 0, 1, 0, iABC)		/* OP_EQ */
 ,opmode(1, 0, 0, 0, 0, iABC)		/* OP_MMBIN */
 ,opmode(0, 0, 0, 0, 0, iAx)		/* OP_EXTRAARG */
 ...
};
```

逐行读几个有代表性的：

- `OP_MOVE`：`opmode(0,0,0,0,1,iABC)` → 格式 iABC,A 是目的(setA=1)。最朴素的寄存器间搬动。
- `OP_LOADI`：`opmode(0,0,0,0,1,iAsBx)` → 格式 iAsBx，带符号立即数 sBx。
- `OP_GETTABLE`：iABC,setA，从表里取值放进 R[A]。
- `OP_NEWTABLE`：`opmode(0,0,0,0,1,ivABC)` → **ivABC** 格式(5.5 新格式之一)，因为要同时塞哈希大小(vB)和数组大小(vC)。
- `OP_CALL`：`opmode(0,1,1,0,1,iABC)` → OT=1(设 top 给下一条)、IT=1(用上一条的 top)、setA。多返回值的核心。
- `OP_JMP`：`opmode(0,0,0,0,0,isJ)` → **isJ** 格式(5.5 新格式)，只有 sJ 一个操作数，无 A。
- `OP_EQ`：`opmode(0,0,0,1,0,iABC)` → testTMode=1，后面必须跟 JMP。比较类指令的标志。
- `OP_MMBIN`：`opmode(1,0,0,0,0,iABC)` → MM=1，元方法指令。后文讲算术时会回来。
- `OP_EXTRAARG`：`opmode(0,0,0,0,0,iAx)` → iAx 格式，纯扩展参数，无 A、无副作用。

表的顺序与 `enum OpCode`(`lopcodes.h:231-348`)完全一致，注释里写 `/* ORDER OP */` 提醒：改了枚举顺序就要同步改这张表和 `lopnames.h` 的名字表。`NUM_OPCODES = (int)(OP_EXTRAARG) + 1`(lopcodes.h:351)，因为 EXTRAARG 是最后一个枚举值。

`lopcodes.c` 还暴露两个判定函数(`lopcodes.c:117-139`)，供 VM 优化路径用：

```c
/*
** Check whether instruction sets top for next instruction, that is,
** it results in multiple values.
*/
int luaP_isOT (Instruction i) {
  OpCode op = GET_OPCODE(i);
  switch (op) {
    case OP_TAILCALL: return 1;
    default:
      return testOTMode(op) && GETARG_C(i) == 0;
  }
}


/*
** Check whether instruction uses top from previous instruction, that is,
** it accepts multiple results.
*/
int luaP_isIT (Instruction i) {
  OpCode op = GET_OPCODE(i);
  switch (op) {
    case OP_SETLIST:
      return testITMode(GET_OPCODE(i)) && GETARG_vB(i) == 0;
    default:
      return testITMode(GET_OPCODE(i)) && GETARG_B(i) == 0;
  }
}
```

`luaP_isOT` 判断"这条指令会不会产生不定个数的结果"(C==0 时 top 接管返回值个数),`luaP_isIT` 判断"这条指令会不会消费不定个数的结果"(B==0)。两者配合，VM 才能在 `f(a, b())` 这种"可变个数实参"场景下正确调整 `top`。注意 `SETLIST` 是 ivABC 格式所以查 `vB`，其他 iABC 查 `B`——这正是 ivABC 引入后必须特判的地方。

### 2.7 RK 约定：k 位替代了老版的 ISK/INDEXK

现在讲一个 5.5 与老资料**最显著的差异**:RK 约定。

很多运算的某个操作数既可能是寄存器，也可能是常量表里的一个常量。比如 `t.x = v`，v 可能已经在一个寄存器里，也可能是个常量(数字字面量、短字符串)。如果为"常量操作数"和"寄存器操作数"各发一条独立指令，指令集会膨胀。Lua 的做法是**同一条指令的同一个操作数位置，既能编码寄存器也能编码常量**，用一个标志位区分。

老版(5.3/5.4)的做法是：把寄存器号和常量索引共用一个 8 位字段，规定**常量索引 = 寄存器最大值 + 1 起算**(即 `BITRK = MAXARG_C+1`，C 字段最高位为 1 表示常量)。老资料里的 `ISK(x)`、`INDEXK(rk)`、`RKASK(r)` 三个宏就是干这个的。

**5.5 彻底改了这个机制**。我去 `lua-5.5.0/src` 全目录 Grep `ISK`、`INDEXK`、`RKASK`、`BITRK`，**一个都找不到**(只有 `luac.c` 里有个打印用的局部宏 `#define ISK`，与编码无关)。5.5 的做法是：用**独立的 k 位**(`POS_k`，位 15)来标记"这条指令的 C(或 B)操作数是常量索引而非寄存器"。

`lopcodes.h:219-223` 的注释把约定讲清楚了：

```c
/*
** R[x] - register
** K[x] - constant (in constant table)
** RK(x) == if k(i) then K[x] else R[x]
*/
```

`RK(x)` 的定义是"如果 k 位为 1，则 x 是常量索引 K[x]，否则是寄存器 R[x]"。注意这里的 `k(i)` 指的就是 `GETARG_k(i)`——位 15 那个 k。

执行侧的落地在 `lvm.c` 的指令操作数宏(`lvm.c:1100-1110`，完整贴出):

```c
#define RA(i)	(base+GETARG_A(i))
#define RB(i)	(base+GETARG_B(i))
#define vRB(i)	s2v(RB(i))
#define KB(i)	(k+GETARG_B(i))
#define RC(i)	(base+GETARG_C(i))
#define vRC(i)	s2v(RC(i))
#define KC(i)	(k+GETARG_C(i))
#define RKC(i)	((TESTARG_k(i)) ? k + GETARG_C(i) : s2v(base + GETARG_C(i)))
```

`RA`/`RB`/`RC` 取寄存器地址(`base` 是当前函数值栈底，`k` 是当前函数的常量表指针 `f->k`)。`KB`/`KC` 取常量地址。**`RKC(i)` 就是 RK 约定的执行侧实现**:k 位为 1 走 `k + GETARG_C(i)`(常量表)，否则走 `s2v(base + GETARG_C(i))`(寄存器)。一条 `SETTABLE R[A][R[B]] := RK(C)` 在执行时，C 字段到底是寄存器还是常量，完全由 k 位决定——无须第二条指令、无须不同的操作码。

看 SETTABLE 的执行就能确认这个分支(`lvm.c` 里 `vmcase(OP_SETTABLE)` 一带而过，核心是 RKC):

```c
vmcase(OP_SETTABLE) {
  ...
  TValue *rb = vRB(i);   /* R[B]:键 */
  TValue *rc = RKC(i);   /* RK(C):值——k 位决定是寄存器还是常量 */
  ...
}
```

编译器侧怎么决定把一个操作数编成常量还是寄存器？看 `lcode.c` 的 `exp2RK`(`lcode.c:1085`):

```c
/*
** Ensures final expression result is in a valid R/K index
** (that is, it is either in a register or in 'k' with an index
** in the range of R/K indices).
** Returns 1 iff expression is K.
*/
static int exp2RK (FuncState *fs, expdesc *e) {
  if (luaK_exp2K(fs, e))
    return 1;
  else {  /* not a constant in the right range: put it in a register */
    luaK_exp2anyreg(fs, e);
    ...
```

它调 `luaK_exp2K` 尝试把表达式变成常量索引，成功(返回 1)就用 K 模式；失败就老老实实分配一个寄存器。`luaK_exp2K` 内部判断常量索引是否 `<= MAXINDEXRK`(`lcode.c:1068`),`MAXINDEXRK = MAXARG_B`(lopcodes.h:202)，也就是 255。常量索引超过 255 就放不下，只能落到寄存器。

发射 RK 指令的入口是 `codeABRK`(`lcode.c:1095`)，它根据 `exp2RK` 的返回值决定 k 位：

```c
static void codeABRK (FuncState *fs, OpCode o, int A, int B,
                      expdesc *ec) {
  int k = exp2RK(fs, ec);
  ...
  luaK_codeABCk(fs, o, A, B, ec->u.info, k);
}
```

所有用到 RK 的赋值(`SETTABLE`/`SETI`/`SETFIELD`/`SETTABUP`)都走这个函数(`lcode.c:1118` 起)。

**5.5 这个改动带来的后果**:①k 位是全局共享的——同一条指令只能有一个操作数是 RK(5.5 里 RK 作用在 C 上);②因为 k 位是独立的 1 位，不再占用 C 字段的最高位，所以寄存器号/常量索引都能用满完整的 8 位(0–255)，不像老版要留一位给标志。这其实是用 1 个全局 bit 换回了每个 RK 字段 1 个 bit，是个净收益。

### 2.8 操作码全集：83 个，分九类

最后把 `lopnames.h` 的 83 个操作码分门别类过一遍。每个操作码在 `lopcodes.h:231-348` 的枚举里都带一行注释说明语义，这里按功能归类，贴出关键注释。

**①寄存器/常量加载(9 个)**:

```c
OP_MOVE,/*	A B	R[A] := R[B]					*/
OP_LOADI,/*	A sBx	R[A] := sBx					*/
OP_LOADF,/*	A sBx	R[A] := (lua_Number)sBx				*/
OP_LOADK,/*	A Bx	R[A] := K[Bx]					*/
OP_LOADKX,/*	A	R[A] := K[extra arg]				*/
OP_LOADFALSE,/*	A	R[A] := false					*/
OP_LFALSESKIP,/*A	R[A] := false; pc++				*/
OP_LOADTRUE,/*	A	R[A] := true					*/
OP_LOADNIL,/*	A B	R[A], R[A+1], ..., R[A+B] := nil		*/
```

`LOADI`/`LOADF` 直接把整数/浮点立即数编进 sBx(±16M 范围)，无须进常量表，省一次访存。`LFALSESKIP` 是个特殊指令(注释 `lopcodes.h:358` 解释)，用于把条件转成布尔值时跳过下一条，配合 `LOADTRUE` 实现 `not cond ? false : true`。`LOADKX` 是常量索引超过 Bx(17 位)时的逃生通道，下一小节讲。

**②upvalue(4 个)**:

```c
OP_GETUPVAL,/*	A B	R[A] := UpValue[B]				*/
OP_SETUPVAL,/*	A B	UpValue[B] := R[A]				*/
OP_GETTABUP,/*	A B C	R[A] := UpValue[B][K[C]:shortstring]		*/
OP_SETTABUP,/*	A B C	UpValue[A][K[B]:shortstring] := RK(C)		*/
```

`GETTABUP` 是"从 upvalue(通常是 `_ENV`)按字符串键取值"——Lua 全局变量访问 `x` 就编成这条。`K[C]：shortstring` 表示 C 指向常量表里的一个短字符串。upvalue 的开合机制留到 P4-14。

**③表访问(4 读 + 4 写)**:

```c
OP_GETTABLE,/*	A B C	R[A] := R[B][R[C]]				*/
OP_GETI,/*	A B C	R[A] := R[B][C]					*/
OP_GETFIELD,/*	A B C	R[A] := R[B][K[C]:shortstring]			*/
...
OP_SETTABLE,/*	A B C	R[A][R[B]] := RK(C)				*/
OP_SETI,/*	A B C	R[A][B] := RK(C)				*/
OP_SETFIELD,/*	A B C	R[A][K[B]:shortstring] := RK(C)			*/
```

三种键分别对应：寄存器里的任意键(`GETTABLE`/`SETTABLE`，键是 R[C] 或 R[B])、整数立即数键(`GETI`/`SETI`，键是 C 本身)、字符串常量键(`GETFIELD`/`SETFIELD`，键在常量表)。**为每种键单独造一条指令**，执行时少一次类型判断和取值，这是用指令条数换执行速度的典型取舍。`SET*` 的值操作数都是 RK(C)，即值可能是寄存器也可能是常量。

**④表构造(1 个，ivABC 格式)**:

```c
OP_NEWTABLE,/*	A vB vC k	R[A] := {}				*/
```

ivABC 格式：vB 是哈希大小的 log2(加 1，或 0 表示空),vC 是数组大小。k=1 时数组大小超过 vC 范围，用下一条 EXTRAARG 扩展(见 2.9)。这是 5.5 新格式 ivABC 的主要使用者。

**⑤算术与位运算(24 个，分三族)**:

立即数族(操作数是立即数或常量):

```c
OP_ADDI,/*	A B sC	R[A] := R[B] + sC				*/  /* 整数立即数加 */
OP_ADDK,/*	A B C	R[A] := R[B] + K[C]:number			*/  /* 常量加 */
OP_SUBK, OP_MULK, OP_MODK, OP_POWK, OP_DIVK, OP_IDIVK,           /* 常量算术族 */
OP_BANDK, OP_BORK, OP_BXORK,                                     /* 常量位运算族 */
OP_SHLI,/*	A B sC	R[A] := sC << R[B]				*/  /* 移位立即数 */
OP_SHRI,/*	A B sC	R[A] := R[B] >> sC				*/
```

寄存器族(两个操作数都是寄存器):

```c
OP_ADD, OP_SUB, OP_MUL, OP_MOD, OP_POW, OP_DIV, OP_IDIV,         /* 算术 */
OP_BAND, OP_BOR, OP_BXOR, OP_SHL, OP_SHR,                        /* 位运算 */
```

元方法回退族(紧跟在算术/位运算后，操作失败时调用):

```c
OP_MMBIN,/*	A B C	call C metamethod over R[A] and R[B]		*/
OP_MMBINI,/*	A sB C k	call C metamethod over R[A] and sB	*/
OP_MMBINK,/*	A B C k		call C metamethod over R[A] and K[B]	*/
```

**这里有一组 5.5 相对老资料的显著差异**。老资料(5.3/5.4)讲 Lua 算术指令时通常只讲 `ADD`/`SUB`/... 一个族，常量操作数靠 RK 约定复用同一条指令。5.5 砍掉了算术指令上的 RK 约定，**为"另一个操作数是常量/立即数"的情况单独造了一整套指令**:`ADDK`/`SUBK`/`MULK`/... (常量在 K 表)、`ADDI`(整数立即数在 sC)、`SHLI`/`SHRI`(移位的立即数版本)。原因：算术指令在 5.5 里去掉了 k 位(看 opmode 表里 `OP_ADD` 是普通 iABC，无 k)，腾出来的编码空间用来做这些专用变体。每条专用指令在执行时少一次"k 位判断 + 常量表寻址"，热路径上的算术运算因此更快。代价是指令集膨胀(算术从 11 条扩到 24 条)，但 7 位操作码空间(128)绰绰有余。

`MMBIN`/`MMBINI`/`MMBINK` 是配套的元方法回退。Lua 的算术有"操作数不是数字时调元方法"的语义(比如 table 上的 `__add`)。编译器在每条算术指令后面都跟一条对应的 `MMBIN*`，执行时如果算术成功就 `pc++` 跳过它，失败才执行它去调元方法(注释 `lopcodes.h:362-365`)。三种 MMBIN 对应三种场景：两个寄存器(MMBIN)、寄存器和立即数(MMBINI)、寄存器和常量(MMBINK)。`MMBINI`/`MMBINK` 的 k 位表示"操作数翻转"(常量在左)，注释 `lopcodes.h:396-397`：`k` means the arguments were flipped。

**⑥一元与连接(5 个)**:

```c
OP_UNM,/*	A B	R[A] := -R[B]					*/
OP_BNOT,/*	A B	R[A] := ~R[B]					*/
OP_NOT,/*	A B	R[A] := not R[B]				*/
OP_LEN,/*	A B	R[A] := #R[B] (length operator)			*/
OP_CONCAT,/*	A B	R[A] := R[A].. ... ..R[A + B - 1]		*/
```

`CONCAT` 一条指令把 R[A] 到 R[A+B-1] 连续若干个值串成一个字符串，避免一条条 `..` 发指令。

**⑦upvalue 生命周期(2 个)**:

```c
OP_CLOSE,/*	A	close all upvalues >= R[A]			*/
OP_TBC,/*	A	mark variable A "to be closed"			*/
```

`CLOSE` 在作用域结束时关闭所有 open upvalue(P4-14 详讲)。`TBC` 标记"退出作用域时要调用 `__close` 元方法"的变量(to-be-closed,P4-15 详讲)。

**⑧控制流(13 个)**:

```c
OP_JMP,/*	sJ	pc += sJ					*/
OP_EQ,/*	A B k	if ((R[A] == R[B]) ~= k) then pc++		*/
OP_LT,/*	A B k	if ((R[A] <  R[B]) ~= k) then pc++		*/
OP_LE,/*	A B k	if ((R[A] <= R[B]) ~= k) then pc++		*/
OP_EQK,/*	A B k	if ((R[A] == K[B]) ~= k) then pc++		*/
OP_EQI,/*	A sB k	if ((R[A] == sB) ~= k) then pc++		*/
OP_LTI, OP_LEI, OP_GTI, OP_GEI,                                 /* 整数立即数比较族 */
OP_TEST,/*	A k	if (not R[A] == k) then pc++			*/
OP_TESTSET,/*	A B k	if (not R[B] == k) then pc++ else R[A] := R[B]  */
```

`JMP` 是 5.5 新格式 isJ 的唯一使用者，操作数只有 sJ(跳转偏移，±16M)。`EQ`/`LT`/`LE` 是寄存器-寄存器比较；`EQK` 是寄存器-常量比较；`EQI`/`LTI`/`LEI`/`GTI`/`GEI` 是寄存器-整数立即数比较(`GTI`/`GEI` 用"翻转的大于"避免在比较器里特判方向)。这一组同样体现 5.5 的思路：**为每种操作数类型造专用指令**，而不是靠 RK 复用。`TEST` 测单个寄存器的真假，`TESTSET` 在短路求值 `a = b or c` 这类场景里既测试又搬运(注释 `lopcodes.h:366-367`)。

所有比较和 test 指令的 opmode 都置了 testTMode(位 4)，含义是"紧跟我的下一条必须是 JMP"——比较本身不跳转，只决定要不要跳过紧跟的 JMP。注释 `lopcodes.h:399-401`：`All comparison and test instructions assume that the instruction being skipped (pc++) is a jump`。

**⑨调用、返回、循环、收尾(21 个)**:

```c
OP_CALL,/*	A B C	R[A], ... ,R[A+C-2] := R[A](R[A+1], ... , R[A+B-1]) */
OP_TAILCALL,/*	A B C k	return R[A](R[A+1], ... , R[A+B-1])		*/
OP_RETURN,/*	A B C k	return R[A], ... ,R[A+B-2]			*/
OP_RETURN0,/*		return						*/
OP_RETURN1,/*	A	return R[A]					*/
OP_FORLOOP,/*	A Bx	update counters; if loop continues then pc-=Bx; */
OP_FORPREP,/*	A Bx	<check values and prepare counters>;
                        if not to run then pc+=Bx+1;			*/
OP_TFORPREP,/*	A Bx	create upvalue for R[A + 3]; pc+=Bx		*/
OP_TFORCALL,/*	A C	R[A+4], ... ,R[A+3+C] := R[A](R[A+1], R[A+2]);	*/
OP_TFORLOOP,/*	A Bx	if R[A+2] ~= nil then { R[A]=R[A+2]; pc -= Bx }	*/
OP_SETLIST,/*	A vB vC k	R[A][vC+i] := R[A+i], 1 <= i <= vB	*/
OP_CLOSURE,/*	A Bx	R[A] := closure(KPROTO[Bx])			*/
OP_VARARG,/*	A B C k	R[A], ..., R[A+C-2] = varargs  			*/
OP_GETVARG, /* A B C	R[A] := R[B][R[C]], R[B] is vararg parameter    */
OP_ERRNNIL,/*	A Bx	raise error if R[A] ~= nil (K[Bx - 1] is global name)*/
OP_VARARGPREP,/* 	(adjust varargs)				*/
OP_EXTRAARG/*	Ax	extra (larger) argument for previous opcode	*/
```

几个要点：

- `CALL` 的 B 和 C 是"参数个数/返回值个数"的编码，B==0 表示"用 top"(实参个数等于函数实际收到个数，用于 `f(...)`),C==0 表示"把 top 设给下一条"(接收多返回值，用于 `a, b = f()`)。opmode 表里 CALL 的 OT=1、IT=1 就是这个意思。
- `RETURN0`/`RETURN1` 是 5.5 的专用变体——返回 0 个值或 1 个值是最高频的返回场景，单独造指令避免 RETURN 的通用开销。`RETURN0` 连 A 操作数都没有(无操作数返回)。
- 数值 for 循环用 `FORPREP`+`FORLOOP` 一对(`Bx` 是跳转回loop头的偏移)；泛型 for 用 `TFORPREP`+`TFORCALL`+`TFORLOOP` 三条。
- `CLOSURE` 从原型表 `KPROTO` 取第 Bx 个子函数原型，造一个闭包放进 R[A]。
- `SETLIST` 是 ivABC 格式(vB 元素、vC 起始下标)，用于 `{...}` 表构造里批量塞元素。
- **`GETVARG`** 是 5.5 新指令(老资料没有)，注释 `R[A] := R[B][R[C]]， R[B] is vararg parameter`——从可变参数表里按下标取值。
- **`ERRNNIL`** 是 5.5 新指令，注释 `raise error if R[A] ~= nil`——用于 `x = x or error(...)` 之类断言某变量非 nil 的场景，`K[Bx-1]` 存全局名字供错误信息用。`Bx == 0` 表示名字索引也放不下(注释 `lopcodes.h:390-391`)。
- `VARARGPREP` 是函数入口固定第一条指令，调整可变参数。它没有常规操作数。

最后是 `EXTRAARG`，它是整个编码体系的逃生通道，下一节专门讲。

### 2.9 EXTRAARG:突破位宽限制

所有字段位宽都是固定的，总有不够用的时候。最典型的是常量索引：`LOADK` 用 iABx,Bx 是 17 位，能编 0–131071。一个 Lua 函数的常量表如果超过 131072 个常量(虽然罕见，但语法上可能——比如一个巨大的表字面量),`LOADK` 就放不下。

5.5 的解法是 `LOADKX` + `EXTRAARG` 这一对(`lopcodes.h:239`、`lopcodes.h:347`):

```c
OP_LOADKX,/*	A	R[A] := K[extra arg]				*/
...
OP_EXTRAARG/*	Ax	extra (larger) argument for previous opcode	*/
```

编译器侧的发射逻辑(`lcode.c:461-469`):

```c
static int luaK_codek (FuncState *fs, int reg, int k) {
  if (k <= MAXARG_Bx)
    return luaK_codeABx(fs, OP_LOADK, reg, k);
  else {
    int p = luaK_codeABx(fs, OP_LOADKX, reg, 0);
    codeextraarg(fs, k);
    return p;
  }
}
```

常量索引 `k` 能塞进 Bx(17 位)就用 `LOADK`；塞不下就改发 `LOADKX`(A 还是寄存器号，Bx 位置空着不用)，紧跟一条 `EXTRAARG`(iAx 格式，Ax 25 位，能编 0–33554431)。执行时遇到 `LOADKX`，就去读下一条指令的 Ax 当作真实常量索引。注释 `lopcodes.h:379-380`：`In OP_LOADKX and OP_NEWTABLE, the next instruction is always OP_EXTRAARG`。

`NEWTABLE` 也是同样的扩展机制(`lcode.c:1874-1882`):

```c
void luaK_settablesize (FuncState *fs, int pc, int ra, int asize, int hsize) {
  Instruction *inst = &fs->f->code[pc];
  int extra = asize / (MAXARG_vC + 1);  /* higher bits of array size */
  int rc = asize % (MAXARG_vC + 1);  /* lower bits of array size */
  int k = (extra > 0);  /* true iff needs extra argument */
  hsize = (hsize != 0) ? luaO_ceillog2(cast_uint(hsize)) + 1 : 0;
  *inst = CREATE_vABCk(OP_NEWTABLE, ra, hsize, rc, k);
  *(inst + 1) = CREATE_Ax(OP_EXTRAARG, extra);
}
```

数组大小 `asize` 拆成低 10 位(vC，最大 1023)和高位(extra)。低位放 vC，高位放紧跟的 EXTRAARG 的 Ax。k 位标记"有没有 extra"——执行时如果 k=1，就把 EXTRAARG 的值拼回来(注释 `lopcodes.h:385-388`：`if k, then real C = EXTRAARG _ C`，这里 `_` 表示拼接)。一个超过 1023 元素的表字面量就会触发这个扩展。

`SETLIST` 也有类似的拼接(注释 `lopcodes.h:382-384`)，这里不展开了。

EXTRAARG 这套机制的关键在于：**主指令格式不动，只在"不够"时追加一条扩展指令**。绝大多数指令永远用不到 EXTRAARG，不为罕见的超大索引付任何代价；一旦真的碰到，又能正确处理。这是"精简但不封顶"的设计——把 32 位编码的能力榨干，用 2 条指令的代价换 25 位以上的扩展空间。

---

## 三、为什么这套编码是 sound 的

讲完实现，回到设计层面：这套 32 位编码为什么不丢正确性、不浪费空间、能覆盖所有场景。

### 3.1 位宽够用：每个字段的上限都不卡实际业务

逐字段核算：

- **Op 7 位**:128 个槽位，实际 83 个操作码，占用 65%，留了 45 个给未来扩展。Lua 从 5.0 到 5.5 二十多年才涨到现在这个数，余量充足。
- **A 8 位**:255 个寄存器。Lua 函数的寄存器数等于"局部变量 + 临时值"的数量，255 对应一段相当大的函数体(几百个局部变量)。`MAX_FSTACK = MAXARG_A`(lopcodes.h:210)，编译器在 `lcode.c` 里会检查 `freereg` 不超过这个值，超了报"too many local variables"错。实践中极少触发。
- **B/C 8 位**:RK 操作数最大索引 255。一个函数的常量表超过 256 个常量时，部分常量就无法走 RK(只能 `LOADK` 到寄存器再用)，但程序仍能正确编译——`exp2RK`(`lcode.c:1085`)在常量索引超 `MAXINDEXRK` 时自动退化为分配寄存器。正确性不丢，只是稍微多一条指令。
- **vB/vC**:vC 10 位(1023)给 NEWTABLE 的数组大小、SETLIST 的元素数。配合 EXTRAARG 能扩展到 25 位以上。
- **Bx 17 位**:常量索引/原型索引上限 131071。一个函数的常量或内嵌函数原型超过这个数，LOADKX+EXTRAARG 兜底。
- **sBx/sJ 25 位**:LOADI 立即数 ±16M,JMP 跳转偏移 ±16M 条指令。单函数不会有这么大，JMP 永远够。

每个字段都留了扩展机制(EXTRAARG 或退化路径)，保证"任何合法的 Lua 程序都能编码成这 32 位序列"。

### 3.2 RK 约定省指令而不丢信息

RK 约定的 sound 在于：**k 位是个无损开关**。

- k=0 时，C 字段是寄存器号，值在 `base + C`，取值路径确定。
- k=1 时，C 字段是常量索引，值在 `k + C`，取值路径也确定。

两条路径互斥(一个 C 值不可能同时是寄存器号和常量索引),k 位无歧义地选一条。执行侧 `RKC(i)` 是一个三元表达式(`lvm.c:1110`)，一次 `TESTARG_k` 决定走哪边，正确性等价于"两条独立的指令"。

省下的是什么？假设没有 RK,`SETTABLE` 要拆成 `SETTABLE_R`(值是寄存器)和 `SETTABLE_K`(值是常量)两条指令，操作码多一个，编译器要判断发哪条，VM 的 switch 多一个 case。RK 把这两条合并成一条，用 k 位区分。代价是每次执行多一次 k 位测试(一次 `&` 和分支预测友好的条件)——这比多一条指令的取指/译码开销小得多。

5.5 把 k 位从"C 字段最高位"提到独立位，还有个额外好处：C 字段恢复完整 8 位(0–255)。老版里 C 最高位要做标志，实际有效值只有 0–127。5.5 的常量索引能用到 255，常量多的函数触发"退化为寄存器"的概率更低。

### 3.3 opmode 表保证译码一致性

`luaP_opmodes` 表的 sound 在于：**它是操作码到格式的唯一真源**。

编译器发射时(`luaK_codeABCk` 等),`lua_assert(getOpMode(o) == iABC)` 守住"操作码和格式匹配"；执行时，虽然 `luaV_execute` 的大 switch 是按操作码分发(每个 case 已经隐式知道格式)，但调试器、反汇编器(`luac -l`)、JIT 前端都依赖 opmode 表来正确译码。只要表和枚举顺序一致(`/* ORDER OP */` 注释提醒)，整条链上对"这条指令是什么格式"的判断就永远一致。

`check_exp(checkopm(i, iABC), ...)` 这类断言在调试构建里把"对 iABx 指令取 GETARG_B"这种错误当场抓住。生产构建里 `check_exp` 展开为空，零开销。

### 3.4 EXTRAARG 是有界的逃生通道

EXTRAARG 的 sound 在于：**它永远是某条主指令的固定后继，不引入歧义**。

`LOADKX` 后必跟 EXTRAARG(注释 `lopcodes.h:379`),`NEWTABLE` 在 k=1 时后必跟 EXTRAARG,`SETLIST` 同理。主指令自己声明"我需不需要 extra",VM 按声明决定要不要多取一条。EXTRAARG 的 Ax 25 位能编 3355 万，任何实际的常量/原型/数组大小索引都覆盖。主指令格式不被污染(还是 32 位)，只在"不够"时多一条——这是"用最少的机制覆盖最极端的情况"。

---

## 四、★对照 CPython:16 位栈式 wordcode vs 32 位寄存器式

把 Lua 的指令格式放到 CPython 旁边看，差异是结构性的。

**CPython 3.6+ 的指令格式叫 wordcode**。每条指令固定 **2 字节 = 16 位**:1 字节操作码 + 1 字节操作数。操作数是一个 0–255 的 `arg`，含义由操作码决定——通常是栈上的位置、局部变量号、名字索引、常量索引。需要更大操作数的指令(如 `LOAD_CONST` 引用超过 255 的常量，或跳转目标超出范围)用 **`EXTENDED_ARG`** 前缀：它把自己的 arg 左移 8 位，和下一条指令的 arg 拼成 16 位(再不够还可以连续多个 `EXTENDED_ARG`，拼成 32/40 位)。

对照表：

| 维度 | Lua 5.5 | CPython 3.6+ |
|---|---|---|
| **指令宽度** | 32 位(4 字节) | 16 位(2 字节),`EXTENDED_ARG` 扩展 |
| **操作码位宽** | 7 位(83 个) | 8 位(约 120 个) |
| **指令模型** | 寄存器式：操作数直接是寄存器号/常量索引 | 栈式：操作数多是栈位置或索引，运算靠压栈弹栈 |
| **格式种类** | 6 种(iABC/ivABC/iABx/iAsBx/iAx/isJ) | 基本一种(opcode+arg),`EXTENDED_ARG` 算半个变体 |
| **常量操作数** | RK 约定(k 位)/专用 K 变体(ADDK 等) | 无 RK，常量必须先 `LOAD_CONST` 压栈 |
| **立即数** | LOADI/LOADF 直接编进 sBx;ADDI 的 sC | 几乎无，立即数先进常量表再 `LOAD_CONST` |
| **扩展机制** | EXTRAARG(iAx,25 位) | EXTENDED_ARG(每次扩 8 位) |
| **跳转** | JMP 用 isJ,±16M | `JUMP_FORWARD`/`POP_JUMP_*`，需 EXTENDED_ARG 扩大范围 |

几个关键差异点：

**宽度与取指**。Lua 一条指令 4 字节，CPython 2 字节。单条指令 CPython 更紧凑，但**同一段逻辑 CPython 需要更多条指令**。回看 P0-01 的例子：`local a = 1; local b = a + 2`，Lua 两条(LOADI、ADDI),CPython 七八条(RESUME、LOAD_CONST、STORE_FAST、LOAD_FAST、LOAD_CONST、BINARY_OP、STORE_FAST、RETURN_CONST)。指令多，取指/译码/分发的总开销就大。Lua 用更宽的单条指令，把"两个操作数 + 一个操作"塞进一条，换来了更少的取指次数。这是寄存器式"快"的物理基础——也是本章格式设计直接服务的主线。

**操作数语义**。Lua 的操作数直接就是寄存器号(R[A])或常量索引(K[Bx])，拿到就能用。CPython 的操作数对运算指令来说大多是"无操作数"——`BINARY_OP` 从栈顶弹两个、压一个，操作数只编码"哪种运算"。要把操作数搬上栈，得额外的 `LOAD_FAST`/`LOAD_CONST`。寄存器式省掉了这些搬运指令。

**立即数**。Lua 有 `LOADI`/`LOADF`(整数/浮点直接进指令)、`ADDI`/`SHRI`(运算带立即数)，常见的 `x + 1`、`x >> 2` 一条指令搞定，无须进常量表。CPython 几乎没有立即数指令，`1`、`2` 都要先放常量表再 `LOAD_CONST`——多一次常量表访问、多一条指令。

**常量操作数**。Lua 的 RK 约定让 `t.x = 5` 里的 `5` 可以直接编进 SETFIELD 的 C(常量)，一条指令完成。CPython 必须先 `LOAD_CONST 5` 压栈，再 `STORE_ATTR`，两条。5.5 进一步把算术的常量操作数拆成专用 ADDK/SUBK 族，连 RK 的 k 位测试都省了。

**扩展机制**。Lua 的 EXTRAARG 一次给 25 位，常量索引上限 3355 万，实际永远用不完。CPython 的 EXTENDED_ARG 一次只扩 8 位，超大常量池要连续多个 EXTENDED_ARG(每多一个加一条指令)。Lua 的扩展更稀疏(只有 LOADKX/NEWTABLE/SETLIST 用),CPython 的扩展更频繁(任何超过 255 的常量/跳转都要)。

**为什么两者这么选**。CPython 选 16 位栈式，好处是实现极简——编译器无须寄存器分配，指令种类少，VM 的 switch 简洁；坏处是同一段逻辑指令多。Lua 选 32 位寄存器式，好处是指令少、操作数直接、立即数友好；坏处是编译器要做寄存器分配(P2-09)，指令格式复杂(本章这一大篇)。两者的取舍，正是"简单 vs 紧凑"的经典对立——Lua 用编译期的复杂换执行期的紧凑，CPython 用执行期的指令多换编译期的简单。

这个对照直接落到本书主线上。Lua 的 32 位寄存器式编码，是"用更少的机制换更多的能力"的又一处体现：**一条指令同时编码操作码 + 多个操作数 + 操作数类型标志**，把栈式语言里需要多条指令才能完成的搬运、类型判断、立即数加载，都吸收进单条 32 位里。指令少，取指少，执行快；指令紧凑，字节码体积小；寄存器号直接编码，无须运行时栈维护。小和快，在这 32 位的位分配里同时拿到。这就是本章对"统一与精简换小而快"的具体落地。

---

*下一章 [P3-11 解释器循环 luaV_execute](P3-11-解释器循环luaV_execute-取指译码执行与分发.md):本章把指令怎么编码讲完了，下一章进入 VM 怎么循环地取指、译码、执行、分发——`luaV_execute` 的大 switch 里每个 `vmcase(OP_XXX)` 分支，正是用本章的 `GETARG_*` 宏拆开指令、走完执行的。*

---

## 附：本章核实的 lua-5.5.0 源码行号速查

| 引用 | 位置 |
|---|---|
| `Instruction` 注释(32 位无符号) | `lopcodes.h:14-17` |
| 六种格式图 | `lopcodes.h:14-32` |
| `enum OpMode {iABC, ivABC, iABx, iAsBx, iAx, isJ}` | `lopcodes.h:36` |
| `SIZE_C/vC/B/vB/Bx/A/Ax/sJ/OP` | `lopcodes.h:42-51` |
| `POS_OP/A/k/B/vB/C/vC/Bx/Ax/sJ` | `lopcodes.h:53-66` |
| `MAXARG_*` / `OFFSET_sBx/sJ/sC` | `lopcodes.h:82-114` |
| `MASK1/MASK0` | `lopcodes.h:118-121` |
| `GET_OPCODE/SET_OPCODE` | `lopcodes.h:127-129` |
| `getarg/setarg` | `lopcodes.h:134-136` |
| `GETARG_A/B/vB/sB/C/vC/sC/k/Bx/Ax/sBx/sJ` | `lopcodes.h:138-174` |
| `CREATE_ABCk/vABCk/ABx/Ax/sJ` | `lopcodes.h:177-198` |
| `MAX_FSTACK`/`NO_REG` | `lopcodes.h:210, 215` |
| RK 约定注释 `R[x]/K[x]/RK(x)` | `lopcodes.h:219-223` |
| `enum OpCode`(83 个) | `lopcodes.h:231-348` |
| `NUM_OPCODES` | `lopcodes.h:351` |
| Notes(B==0/C==0/EXTRAARG 等) | `lopcodes.h:355-412` |
| opmode 位布局注释 | `lopcodes.h:415-423` |
| `luaP_opmodes` 声明 | `lopcodes.h:425` |
| `getOpMode/testAMode/testTMode/testITMode/testOTMode/testMMMode` | `lopcodes.h:427-432` |
| `luaP_isOT`/`luaP_isIT` 声明 | `lopcodes.h:435-436` |
| `opmode` 宏 | `lopcodes.c:16-17` |
| `luaP_opmodes[]` 表 | `lopcodes.c:22-109` |
| `luaP_isOT` 实现 | `lopcodes.c:117-124` |
| `luaP_isIT` 实现 | `lopcodes.c:131-139` |
| `opnames[]`(83 个名) | `lopnames.h:15-101` |
| `l_uint32` | `llimits.h:225` |
| `cast_uint`/`cast_Inst` | `llimits.h:134, 141` |
| `luaK_codeABCk/codevABCk/codeABx/codeAsBx/codesJ/codeextraarg` | `lcode.c:399-453` |
| `luaK_codek`(LOADK/LOADKX 分支) | `lcode.c:461-469` |
| `MAXINDEXRK` 判定 | `lcode.c:1068` |
| `exp2RK` | `lcode.c:1085` |
| `codeABRK` | `lcode.c:1095` |
| `luaK_settablesize`(NEWTABLE+EXTRAARG) | `lcode.c:1874-1882` |
| `RA/RB/vRB/KB/RC/vRC/KC/RKC` | `lvm.c:1100-1110` |
| `dojump`/`donextjump`/`docondjump` | `lvm.c:1127, 1131, 1138` |
| `vmcase(OP_MOVE)` | `lvm.c:1233` |
| `vmcase(OP_LOADK)` | `lvm.c:1250` |
| `vmcase(OP_GETTABLE)` | `lvm.c:1311` |
| `vmcase(OP_GETI)` | `lvm.c:1325` |
| `vmcase(OP_ADDI)` | `lvm.c:1440` |
| `vmcase(OP_JMP)` | `lvm.c:1646` |
