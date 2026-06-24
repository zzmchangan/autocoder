# P1-04 值·Table·状态:LuaJIT 的数据表示

> **本书主线**:把动态执行安全变成机器码。**二分法**:解释器侧 ↔ JIT 侧。**★对照**:官方 Lua(16 字节 tagged TValue)对照 LuaJIT(8 字节 NaN-boxing)。**源码**:LuaJIT 2.1.ROLLING。**基调**:纯直球,不用比喻;从第一性原理一步步推导。

---

## 引子:JIT 的速度,从数据的"形状"开始

P0-01 讲清了 JIT 的全貌:解释器先跑,发现热点,录一条 trace,乐观假设加 guard,编译成机器码。这本后续几章,本来要讲解释器本身(P1-02 的字节码、P1-03 的热点检测)。

但在进入解释器之前,有一个更底层的问题必须先回答:**解释器跑的是什么?是数据。** 每一条字节码指令,本质都是在搬运、加工一个个"值"——一个数加另一个数、查一张表里的字段、调用一个函数。这些值,在内存里长什么样?

这个问题听起来琐碎,但对 JIT 是性命攸关的。原因有两层:

第一,**机器码是直接操作内存和寄存器的二进制位**。如果值的表示里藏了一个间接的指针跳转、一个 union 里的分支判断,那么编译出来的机器码,就要替这些跳转买单。值的表示越紧凑、越规整,机器码就越短、越快、寄存器里塞得下越多。

第二,**guard 检查的是类型**。每条机器码里插的 guard,本质就是"读出这个值的类型 tag,跟假设的 tag 比一下"。如果类型 tag 的位置是固定的几位,guard 就是单条 `cmp` + 条件跳;如果 tag 散落在内存里需要 deref 一次才能读到,guard 就慢一倍。整个 JIT 的快,有一部分就直接押在"类型 tag 怎么编码"这种看起来很琐碎的细节上。

所以这一章要讲清楚:LuaJIT 怎么在内存里表示一个 Lua 值、一张表、一个执行状态。重点不在"它怎么实现的"(那只是源码),而在"它**为什么**这么实现"——为什么放弃官方 Lua 的朴素方案、改用一种看起来很 trick 的位编码?这个 trick 解决了什么、又付出了什么?

讲完这章,你会看到:**JIT 的速度,不是只靠编译器那一段。从最底层的数据表示开始,LuaJIT 就在为机器码的效率铺路。** 这是主线"把动态执行安全变成机器码"里"快"字在数据层的体现。

---

## §1 第一性原理:动态语言的值,怎么用 C 装下

我们从最朴素的问题开始。

Lua 是动态类型语言。一个变量,这一秒可能是整数 `42`,下一秒可能是字符串 `"hello"`,再下一秒可能是一张表、一个函数。但 C 是静态类型,C 里的每个变量都有固定类型——`int`、`double`、`char *`、`struct Foo *`。

现在问题来了:**解释器是用 C 写的。它要在 C 的类型系统里,用一个统一的类型,装下 Lua 里所有可能的值。** 装得下 number,也装得下 string、table、function、nil……怎么装?

### 1.1 朴素方案:tag + union

最直接的思路是给每个值配一个**类型标记**(tag),再用一个 union 装下值本身:

```c
struct Value {
  union {
    double n;        /* 是数 */
    char *s;         /* 是字符串指针 */
    struct Table *t; /* 是表指针 */
    struct Func *f;  /* 是函数指针 */
    /* ... 其他类型 ... */
  };
};

struct TValue {
  int tag;           /* 类型标记:0=nil, 1=bool, 2=number, 3=string, ... */
  struct Value v;    /* 值本身 */
};
```

这是 C 程序员最自然的写法:**一个 tag 字段说明"现在装的是什么",一个 union 把所有可能的值的存储叠在一起**。读出一个值时,先看 tag,再决定怎么解释 union 里的位。

这种方案叫 **tagged value**(带类型标记的值)。它正确、清晰、好懂。**官方 Lua 5.x 正是这么做的。** 在官方 Lua 的源码里(`lobject.h`),`TValue` 就是一个结构体,长这样(简化):

```c
/* 官方 Lua 5.1 的 TValue,简化 */
typedef struct TValue {
  TValuefields;  /* 宏展开:Value value; int tt */
} TValue;
/* TValuefields 展开:union { ... } value; int tt; */
```

字段顺序、union 内容会随版本调整,但本质就是"一个 `int tt` 存类型 + 一个 `union Value` 存值"。

### 1.2 这个方案的开销:16 字节,以及一次额外访存

朴素方案的问题,是它**太大、太慢**。

先看大小。在 64 位平台上,`union Value` 至少要装下一个 `double`(8 字节)或一个指针(8 字节),所以 union 占 8 字节。再加上前面的 `int tag`(占 4 字节,但因对齐要补齐到 8 字节边界),整个 `TValue` 结构体**占 16 字节**。

16 字节是什么概念?CPU 缓存行(cache line)通常是 64 字节。一个 16 字节的 `TValue`,占掉缓存行的 1/4。同样的缓存行,LuaJIT 的紧凑表示能塞进 8 个值,官方 Lua 只能塞进 4 个。函数调用、表查找、循环遍历,这些操作全都是成片地访问 TValue 数组(栈、表的数组部分)。**值越大,缓存行装得越少,缓存命中率越低,访存就越慢。** 在现代 CPU 上,一次缓存未命中代价几十上百纳秒——比一次加法慢上百倍。

更隐蔽的开销在**取类型**这件事本身。看官方 Lua 怎么读类型:它要去访问 `o->tt`,也就是 TValue 结构体里**偏移 8 字节**的那个字段。如果 TValue 是从一个数组里取出来,这个字段访问需要算一次地址加法。而对于解释器/JIT 来说,"读类型"是每条字节码都要做的动作,密度极高。

官方 Lua 接受这个开销,因为它是纯解释器,本来每条指令就要花几十纳秒"理解",多 4 字节、多一次访存,不那么显眼。但 **LuaJIT 要把这些值交给机器码直接操作**——机器码的指令是纳秒级的,任何多余的访存都会被放大成显著的性能损失。

那能不能更紧凑?

### 1.3 关键洞察:浮点数里有"不要"的位

要省到 8 字节(一个 64 位字),就得想办法把"类型 tag"和"值本身"塞进同一个 64 位字里,而不是分两个字段。

乍看不可能:8 字节塞下一个 `double` 就满了,哪有地方塞 tag?

这里有一个看起来像魔法、其实非常朴素的洞察。它来自 IEEE 754 浮点数标准的一个细节。

IEEE 754 规定,一个 64 位 double,有 1 位符号、11 位指数、52 位尾数。当指数位 11 位**全为 1**、尾数最高位为 1、其余尾数不全为 0 时,这个位模式表示 **NaN**(Not-a-Number,非数)。NaN 用来表示"出错的浮点运算结果",比如 `0.0 / 0.0`。

关键在于:**IEEE 754 留下了大量的 NaN 位模式没用到。** 具体说,指数全 1(即 0x7FF 或 0xFFF 开头)的位模式,只要尾数不为 0,就**全部**是 NaN。这有 2^52 - 1 种之多。而硬件 FPU 实际只会产生**一种**特定的 NaN(Quiet NaN,通常位模式是 `0xFFF8000000000000`),其余那些 NaN 位模式,从浮点运算的角度看,**永远不会被生成、也用不到**。

这些"用不到"的 NaN 位模式,就是空出来的地皮。

LuaJIT 的核心 trick 是:**把类型 tag 藏进 NaN 的位里,把值(指针、整数)放进 NaN 剩下的位。** 这样,同一个 64 位字,在内存里看起来像一个 NaN(对浮点硬件而言),但其实里面藏了类型和值。这种技术社区叫 **NaN-boxing**(NaN 装箱)。

为什么这是 sound 的?因为只要保证:**真的浮点数永远不会落进我们占用那些 NaN 位模式**,就永远不会混淆。具体怎么保证,后面 §3 详讲。先看实现。

---

## §2 源码印证:TValue 与 NaN-boxing 的位编码

我们来看 LuaJIT 2.1.ROLLING 的 `lj_obj.h`。TValue 的定义在 `lj_obj.h:174`:

```c
/* Tagged value. */
typedef LJ_ALIGN(8) union TValue {
  uint64_t u64;		/* 64 bit pattern overlaps number. */
  lua_Number n;		/* Number object overlaps split tag/value object. */
#if LJ_GC64
  GCRef gcr;		/* GCobj reference with tag. */
  int64_t it64;
  struct {
    LJ_ENDIAN_LOHI(
      int32_t i;	/* Integer value. */
    , uint32_t it;	/* Internal object tag. Must overlap MSW of number. */
    )
  };
#else
  struct {
    LJ_ENDIAN_LOHI(
      union {
	GCRef gcr;	/* GCobj reference (if any). */
	int32_t i;	/* Integer value. */
      };
    , uint32_t it;	/* Internal object tag. Must overlap MSW of number. */
    )
  };
#endif
  ...
} TValue;
```

几个关键事实,一个一个拆。

### 2.1 这是一个 union,不是 struct

第一个反直觉点:**TValue 是 `union`,不是 `struct`**(`union TValue`)。union 的特点是**所有成员共享同一段内存**。也就是说,`u64`、`n`、`gcr`、`it64`、`{ i, it }` 这些名字,指向的是**同一个 8 字节**的不同解释方式:

- 用 `o->u64` 读,这 8 字节被当成一个 64 位无符号整数。
- 用 `o->n` 读,这 8 字节被当成一个 double。
- 用 `o->gcr` 读,这 8 字节被当成一个 GC 对象引用(类型 + 指针打包在一起)。
- 用 `o->it` 读,只读其中 4 字节(MSW,高位字),当作类型 tag。
- 用 `o->i` 读,只读另外 4 字节(LSW,低位字),当作整数值。

为什么是 union?因为我们要的就是"同一段 8 字节,能用多种方式解释"。如果用 struct,字段就会各占各的内存,就不紧凑了。

`LJ_ALIGN(8)` 保证这个 union 在内存里**按 8 字节对齐**。这很重要:8 字节对齐意味着读写它用一条 64 位 load/store 指令就够,不会跨越缓存行边界。

### 2.2 LJ_ENDIAN_LOHI:处理大端小端

你注意到 `LJ_ENDIAN_LOHI(int32_t i; , uint32_t it;)` 这种怪写法。这是处理 CPU 字节序的。在小端 CPU(x86、ARM 小端)上,union 低 4 字节先出现,所以 `int32_t i`(值)在低字、`uint32_t it`(类型)在高字;在大端 CPU 上反过来。`LJ_ENDIAN_LOHI(lo, hi)` 这个宏在 `lj_arch.h:663-668` 按平台展开成正确的顺序。

为什么类型 tag 必须在高字(MSW,Most Significant Word)?这跟 NaN 的位模式有关——IEEE 754 的指数位在高位,要让 TValue 看起来像 NaN,高位的位模式必须长成指数全 1 的样子。所以 tag 必须放在高位字。下面细看。

### 2.3 非 GC64 模式:tag 在高位字

先看最常见的 32 位 GC 模式(`!LJ_GC64`,即 32 位平台或显式关闭 GC64)。这种模式下,TValue 的 8 字节布局是这样的(逐字摘录自 `lj_obj.h:226-241` 作者注释):

```
                  ---MSW---.---LSW---
primitive types |  itype  |         |
lightuserdata   |  itype  |  void * |  (32 bit platforms)
GC objects      |  itype  |  GCRef  |
int (LJ_DUALNUM)|  itype  |   int   |
number           -------double------
```

读法:**高 4 字节(MSW)永远是类型 tag `itype`,低 4 字节(LSW)是值**(指针、整数,或者没有意义)。

**真的浮点数**走最后一行:整个 8 字节就是 double 的位模式,此时没有独立的 itype 字段——但**只有 double 的位模式不是 NaN**,其他类型的位模式都被刻意构造成 NaN,以区分。

来走三个具体例子。

#### 例子一:一个 number,值 3.14

直接把 3.14 的 IEEE 754 位模式塞进 8 字节。3.14 的 double 位模式大致是 `0x40091EB851EB851F`。这 8 字节就是 TValue,高字是 `0x40091EB8`(不是全 1,所以不是 NaN,所以是合法 double)。`o->n` 读出来就是 3.14,`o->u64` 读出来是那个长整数,但没人会用 `o->itype` 去读它的类型——它是 number,类型由"位模式不是 NaN"隐式表示。

LuaJIT 把 number 设值就一句:`#define setnumV(o, x) ((o)->n = (x))`(`lj_obj.h:947`)。直接写 double。

#### 例子二:一个 string,指向某 GCstr 对象(地址 0x12345678)

string 是 GC 对象。值是一个指针(32 位模式下 4 字节)。LuaJIT 把指针放在低字,把**类型 tag** 放在高字。string 的 tag 是 `LJ_TSTR`。

`LJ_TSTR` 是多少?看 `lj_obj.h:263`:

```c
#define LJ_TSTR			(~4u)
```

`~4u` = `0xFFFFFFFB`。这个数的特点是:**作为 32 位有符号整数是负数**(-5),**作为 IEEE 754 double 的高 32 位,会让整个 double 落在 NaN 区间**(`0xFFFFFFFB_xxxxxxxx` 开头,指数 11 位全 1)。

所以一个 string TValue,内存里 8 字节是这样:

```
MSW: 0xFFFFFFFB   (= LJ_TSTR,作为 tag,同时也是 NaN 高位)
LSW: 0x12345678   (GCstr 指针)
```

整体作为 double 看,是 `0xFFFFFFFB12345678`,落在 NaN 区间。**这就是 NaN-boxing 的精髓:类型 tag 选的数值,恰好让 8 字节整体成为一个 NaN,与真的 double 永远不会撞车。**

#### 例子三:一个 nil

nil 是原始类型,它没有值,只需要表示"我是 nil"。tag 是 `LJ_TNIL = ~0u = 0xFFFFFFFF`(`lj_obj.h:260`)。

非 GC64 模式下,设 nil 就是把高字写成这个值:`#define setnilV(o) ((o)->it = LJ_TNIL)`(`lj_obj.h:885`)。低字随便(没人读)。整体 `0xFFFFFFFF_xxxxxxxx` 是 NaN。

### 2.4 类型常量全表:为什么是 `~n` 而不是 `n`

来看完整的类型常量表(`lj_obj.h:260-274`):

```c
#define LJ_TNIL			(~0u)
#define LJ_TFALSE		(~1u)
#define LJ_TTRUE		(~2u)
#define LJ_TLIGHTUD		(~3u)
#define LJ_TSTR			(~4u)
#define LJ_TUPVAL		(~5u)
#define LJ_TTHREAD		(~6u)
#define LJ_TPROTO		(~7u)
#define LJ_TFUNC		(~8u)
#define LJ_TTRACE		(~9u)
#define LJ_TCDATA		(~10u)
#define LJ_TTAB			(~11u)
#define LJ_TUDATA		(~12u)
/* This is just the canonical number type used in some places. */
#define LJ_TNUMX		(~13u)
```

注意一个反直觉的细节:**类型 tag 不是从 0 开始递增(0、1、2、3……),而是从 `~0`、`~1`、`~2` 开始递减**(也就是 0xFFFFFFFF、0xFFFFFFFE、0xFFFFFFFD……)。

为什么这样选?两个理由。

**第一个理由:让 tag 落在 NaN 区间。** `~n` 作为 32 位模式,最高字节是 0xFF。把这样一个 32 位模式塞进 double 的高字,整个 double 的指数位(11 位)就是全 1,落进 NaN 区间。如果用 `0、1、2` 这种小数,高字会是 `0x00000000`,double 的指数位就不是全 1,会和真的浮点数混淆。这是 NaN-boxing 的硬约束。

**第二个理由:让类型比较变成"小于等于"。** Lua 类型有一个自然的层次:nil/false/true 是原始类型(primitive),number 是数,其余是 GC 对象(string/table/func...)。LuaJIT 刻意把原始类型排在前面(`~0u`、`~1u`、`~2u`,数值最大),GC 对象排在后面(`~4u` 到 `~12u`,数值较小)。这样判断"是不是数"这种分类,可以用单条整数比较指令完成,而不是一串分支。看:

```c
#define tvisnumber(o)	(itype(o) <= LJ_TISNUM)
#define tvispri(o)	(itype(o) >= LJ_TISPRI)   /* LJ_TISPRI = LJ_TTRUE = ~2u */
#define tvistabud(o)	(itype(o) <= LJ_TISTABUD) /* LJ_TISTABUD = LJ_TTAB = ~11u */
#define tvisgcv(o)	((itype(o) - LJ_TISGCV) > (LJ_TNUMX - LJ_TISGCV))
```

(`lj_obj.h:808-811`,`LJ_TISGCV = LJ_TSTR+1`,`lj_obj.h:284`)

"是不是 GC 对象"、"是不是原始类型"、"是不是数",全都是**单条 `cmp` 加条件跳**,机器码里没有一连串 if-else。这对解释器(每条字节码都要判类型)和 JIT(guard 也是判类型)都是直接的速度红利。作者在 `lj_obj.h:255-258` 写了排序约束:

```
** ORDER LJ_T
** Primitive types nil/false/true must be first, lightuserdata next.
** GC objects are at the end, table/userdata must be lowest.
```

这段话翻译过来就是:类型常量的**数值顺序**不是随便排的,它编码了类型的分类层次,**让类型判断能用整数比较而不是分支**。

### 2.5 LJ_GC64 模式:把 64 位指针也塞进去

到现在讲的都是 32 位 GC 模式(tag 占高字 4 字节,值占低字 4 字节)。到了 64 位平台,问题来了:**指针是 8 字节,低字 4 字节装不下**。

LuaJIT 在 64 位平台提供 `LJ_GC64` 模式(默认在 x64、ARM64 上开启,见 `lj_arch.h:596-602`)。这个模式下,TValue 依然是 8 字节,但位编码变了。作者注释 `lj_obj.h:242-254`:

```
** Format for 64 bit GC references (LJ_GC64):
**
** The upper 13 bits must be 1 (0xfff8...) for a special NaN. The next
** 4 bits hold the internal tag. The lowest 47 bits either hold a pointer,
** a zero-extended 32 bit integer or all bits set to 1 for primitive types.
**
**                     ------MSW------.------LSW------
** primitive types    |1..1|itype|1..................1|
** GC objects         |1..1|itype|-------GCRef--------|
** lightuserdata      |1..1|itype|seg|------ofs-------|
** int (LJ_DUALNUM)   |1..1|itype|0..0|-----int-------|
** number              ------------double-------------
```

读法:**最高 13 位全 1**(构成 NaN 的高位模式 `0xFFF8...`)、**接下来 4 位是类型 tag**、**最低 47 位是值**(指针、整数,或全 1 表示原始类型)。

为什么是 47 位指针?这是 x86-64 的一个事实约束:x86-64 的虚拟地址空间虽然理论 64 位,但实际只用了低 48 位(用户态更低,通常 47 位),高位是符号扩展。所以一个真实的指针,**47 位就够装**。剩下 13 位给 NaN 标记、4 位给 tag,正好拼成 64 位。

来看相关宏:

```c
/* lj_obj.h:290 */
#if LJ_GC64
#define LJ_GCVMASK	(((uint64_t)1 << 47) - 1)
#endif

/* lj_obj.h:782 */
#if LJ_GC64
#define itype(o)	((uint32_t)((o)->it64 >> 47))
#define tvisnil(o)	((o)->it64 == -1)
#else
#define itype(o)	((o)->it)
#define tvisnil(o)	(itype(o) == LJ_TNIL)
#endif
```

`itype(o)` 把整个 64 位 `it64` 右移 47 位,把低 47 位的值丢掉,高 17 位(13 位 NaN + 4 位 tag)右移到最低,再取低 32 位——结果正好是那 4 位 tag。**一条移位指令就取出类型**,非常便宜。

`LJ_GCVMASK = (1 << 47) - 1` 是低 47 位的掩码。取 GC 指针时用它把 tag 抹掉:

```c
/* lj_obj.h:834 */
#if LJ_GC64
#define gcval(o)	((GCobj *)(gcrefu((o)->gcr) & LJ_GCVMASK))
#else
#define gcval(o)	(gcref((o)->gcr))
#endif
```

设值时,类型 tag 左移 47 位拼进去:

```c
/* lj_obj.h:872 */
#if LJ_GC64
#define setitype(o, i)		((o)->it = ((i) << 15))
#define setnilV(o)		((o)->it64 = -1)
#define setpriV(o, x)		((o)->it64 = (int64_t)~((uint64_t)~(x)<<47))
...
```

注意 GC64 下 nil 是 `it64 == -1`(全 64 位全 1),这正好对应"低 47 位全 1 表示原始类型"的规则——nil 的低 47 位全 1,高 17 位也全 1,整体是全 1。

为什么 `setitype` 是 `<< 15` 而不是 `<< 47`?因为 `o->it` 是 `uint32_t`,只占 32 位。把 tag `i`(4 位有效)左移 15 位后落在 32 位字的高位段,再经由小端字节序和 `it64` 的高位对齐。这是位编码的细节,领会"tag 被打包进高位"即可。

### 2.6 三个例子再走一遍(GC64 模式)

为了让你看清两种模式的差别,我们把 §2.3 的三个例子在 GC64 下再走一遍。

**number 3.14**:和 32 位模式一样,8 字节就是 double 位模式 `0x40091EB851EB851F`,不是 NaN,所以是 number。setnumV 一句 `o->n = 3.14`。

**string,GCstr 在地址 0x7FFE_ABCD_0000**:
```
bit 63..51: 全 1(13 位,NaN 标记)         = 0xFFF8
bit 50..47: LJ_TSTR (~4u 取低 4 位 = 0xB)  = 0xB
bit 46..0:  GCstr 指针 0x7FFEABCD0000 的低 47 位
```
整体作为一个 64 位字,前 16 位是 `0xFFFB`,落在 NaN 区间。`itype(o)` 右移 47 位得到 tag,`gcval(o)` 用 `LJ_GCVMASK` 屏蔽掉 tag 得到指针。

**nil**:`it64 = -1`,即 64 位全 1。`itype(o)` 右移 47 位得到 `0x1FFFF`,但 tvisnil 直接判 `it64 == -1`,更快。

### 2.7 取值的内联函数

最后看一下 LuaJIT 提供的取/设值 API。这些都在 `lj_obj.h`,大多是 `static LJ_AINLINE`(强制内联),目的是让编译器把它们直接嵌进调用点,没有函数调用开销。

设值宏(`lj_obj.h:872-967`,摘关键):

```c
#define setnilV(o)		((o)->it = LJ_TNIL)   /* 非 GC64 */
#define setnumV(o, x)		((o)->n = (x))
#define setnanV(o)		((o)->u64 = U64x(fff80000,00000000))  /* 写一个标准 quiet NaN */

static LJ_AINLINE void setintV(TValue *o, int32_t i)
{
#if LJ_DUALNUM
  o->i = (uint32_t)i; setitype(o, LJ_TISNUM);
#else
  o->n = (lua_Number)i;
#endif
}
```

`LJ_DUALNUM` 是另一个开关:开启时,LuaJIT 区分整数和浮点(整数值用 `i` 字段存,tag 是 `LJ_TISNUM`),不开时所有数都当 double 存(整数也转 double)。开了 DUALNUM 整数运算更快(走 CPU 整数指令,不走浮点单元),但类型系统更复杂。这是另一个"用复杂度换速度"的取舍。

设 GC 对象值,有一组宏(`lj_obj.h:934-945`):

```c
#define define_setV(name, type, tag) \
static LJ_AINLINE void name(lua_State *L, TValue *o, const type *v) \
{ \
  setgcV(L, o, obj2gco(v), tag); \
}
define_setV(setstrV, GCstr, LJ_TSTR)
define_setV(setthreadV, lua_State, LJ_TTHREAD)
define_setV(setprotoV, GCproto, LJ_TPROTO)
define_setV(setfuncV, GCfunc, LJ_TFUNC)
define_setV(setcdataV, GCcdata, LJ_TCDATA)
define_setV(settabV, GCtab, LJ_TTAB)
define_setV(setudataV, GCudata, LJ_TUDATA)
```

`define_setV` 是个宏生成宏,批量为每种 GC 类型生成一个 `setstrV`/`settabV`/`setfuncV`。它们内部都调 `setgcV`(`lj_obj.h:928`),后者把 GCobj 指针和 tag 一起打包进 TValue:

```c
static LJ_AINLINE void setgcVraw(TValue *o, GCobj *v, uint32_t itype)
{
#if LJ_GC64
  setgcreft(o->gcr, v, itype);
#else
  setgcref(o->gcr, v); setitype(o, itype);
#endif
}
```

非 GC64 下两步:先写指针(`setgcref`),再写 tag(`setitype`)。GC64 下一步合并:用 `setgcreft`(`lj_obj.h:71`)把指针和 tag 用一次 64 位写一起塞进去。

**所有这些 API 都被设计成可内联的、零额外访存的**。这正是为 JIT 友好铺路:解释器和机器码操作值时,就是几条内存写指令,没有任何函数调用或间接跳转。

---

## §3 为什么 sound:NaN-boxing 不会出错

这一节专门回答一个必然冒出来的疑问:**这个 trick 看起来太聪明了,它真的不会出错吗?** 具体三个子问题:

### 3.1 真的浮点数,会不会碰巧长成一个 tag?

不会。IEEE 754 的 NaN 定义是:指数位 11 位全 1、尾数最高位为 1(quiet NaN)或为 0 但尾数其余位不为 0(signaling NaN)。LuaJIT 占用的 tag 区域 `~0u`(0xFFFFFFFF)到 `~13u`(0xFFFFFFF3),作为 32 位字都是 `0xFFFFFFxx`,放到 double 高字后,指数全 1、尾数最高位 1——确实都是 NaN。

反过来,所有**非 NaN 的 double**(包括所有有限数、正负无穷),其高位字都不可能是 `0xFFFFFFxx` 这种模式。具体地:

- 有限 double,指数位不全 1。
- 正负无穷,指数全 1 但尾数全 0(也就是高字是 `0x7FF00000` 或 `0xFFF00000`),跟 `0xFFFFFFFB` 这种差很远。

所以**真的 double 和 NaN-boxing 出来的 tag,在位模式上永远不会撞车**。这是 IEEE 754 标准保证的硬约束,LuaJIT 依赖它。

### 3.2 FPU 自己产生的 NaN,会不会被误判成某个类型?

FPU 实际只会产生一种 NaN(quiet NaN),位模式是 `0xFFF8000000000000`。这个位模式对应的高字是 `0xFFF80000`,跟 LuaJIT 的 tag `0xFFFFFFFB` 等都不一样(`0xFFF8xxxx` vs `0xFFFFFxxx`)。所以即便程序里某个运算产生了 NaN,它也不会被误认成 Lua 的某个类型——LuaJIT 会把它当成一个合法的 double NaN 来对待(运行时如果需要,用 `tvisnan` 判断,见 `lj_obj.h:814`: `#define tvisnan(o) ((o)->n != (o)->n)`,用 `x != x` 判 NaN)。

### 3.3 GC64 的 47 位指针,会不会不够?

x86-64 当前实现只用了 48 位虚拟地址,而且第 48 位是符号扩展位,所以用户态指针的实际有效位是 47 位(内核态是高 16 位为 1)。LuaJIT 假设所有 GC 对象都分配在用户态地址空间(`mmap` 出来的内存在低地址),47 位足够装下指针。

这是当前 x86-64 硬件的事实约束。Intel 后续如果扩展地址空间到 57 位(5 级页表),这个假设会被打破。但 LuaJIT 的做法是显式依赖这个约束并在文档里说明——如果哪天地址扩展,需要重新设计 GC64 编码。这种"在硬约束上榨取性能"的取舍,在系统编程里很常见。

### 3.4 小结:sound 的根源

NaN-boxing 之所以 sound,根源在于:**它依赖的不是某种巧合,而是 IEEE 754 标准明确留出的、永远不会被合法 double 占用的位模式空间**。这个空间本来就空着不用,LuaJIT 把类型 tag 搬进去住,既不挤占 double 的精度,也不会被 FPU 生成的 NaN 干扰。

代价是两个:**位编码的复杂度**(代码里到处是移位、掩码、对齐,可读性差),以及**平台假设**(47 位指针、IEEE 754 兼容)。换来的是:**每个值压缩到 8 字节,类型 tag 可用一条移位指令取出,整套操作可内联、零间接跳转**。对于一个把性能当命脉的 JIT,这个代价完全值得。

---

## §4 GCobj:所有可回收对象的统一头部

讲完了单个值的表示,我们看值的"内容"。除了 number/bool/nil 这种原始类型,Lua 的大部分值是**可被垃圾回收的对象**(GC object):string、table、function、upvalue、thread(协程)、proto(字节码原型)、cdata(FFI 类型)、udata(userdata)。这些对象都分配在堆上,需要 GC 来回收。

LuaJIT 给所有 GC 对象一个**统一头部**,让 GC 可以用同一种方式遍历、标记、回收它们,而不关心具体是什么对象。这个头部就是 `GCHeader`(`lj_obj.h:63`):

```c
#define GCHeader	GCRef nextgc; uint8_t marked; uint8_t gct
```

三个字段:

- **`nextgc`**(类型 `GCRef`):指向下一个 GC 对象。所有 GC 对象用这个字段串成一条单向链表,GC 沿着这条链遍历所有对象。
- **`marked`**(uint8_t):GC 标记位。记录这个对象的"颜色"(白/灰/黑,用于三色标记式增量 GC)、是否被固定(不可回收)等。GC 在标记阶段读写这个字段。
- **`gct`**(uint8_t):对象的具体类型。值是 `~LJ_TSTR`、`~LJ_TTAB` 这种(`LJ_T*` 的按位取反——注意和 TValue 里的 tag 关系:GCobj 头里存的是 `~LJ_T*`,因为这里用单字节,而 `~LJ_TSTR = ~(~4u) = 4`,正好放进一个字节)。

作者在 `lj_obj.h:64` 注释:"This occupies 6 bytes, so use the next 2 bytes for non-32 bit fields."——GCHeader 占 6 字节(`GCRef` 4 或 8 字节 + 两个 `uint8_t`)。在 32 位模式下 GCRef 是 4 字节,GCHeader 共 6 字节,后面 2 字节留给具体类型的字段。

### 4.1 GCRef 的两副面孔

`GCRef` 在 `lj_obj.h:54-60`:

```c
typedef struct GCRef {
#if LJ_GC64
  uint64_t gcptr64;	/* True 64 bit pointer. */
#else
  uint32_t gcptr32;	/* Pseudo 32 bit pointer. */
#endif
} GCRef;
```

这是个**包装类型**,里面就一个指针(64 位或 32 位)。为什么不直接用 `GCobj *`?包装一层是为了**类型安全**——`GCRef` 和裸指针在 C 类型系统里区分开,避免误把别的指针当 GC 指针。配套有一组宏来读写(`lj_obj.h:66-88`):

```c
#if LJ_GC64
#define gcref(r)	((GCobj *)(r).gcptr64)
#define setgcref(r, gc)	((r).gcptr64 = (uint64_t)&(gc)->gch)
#define setgcreft(r, gc, it) \
  (r).gcptr64 = (uint64_t)&(gc)->gch | (((uint64_t)(it)) << 47)
...
#else
#define gcref(r)	((GCobj *)(uintptr_t)(r).gcptr32)
#define setgcref(r, gc)	((r).gcptr32 = (uint32_t)(uintptr_t)&(gc)->gch)
...
#endif

#define gcnext(gc)	(gcref((gc)->gch.nextgc))   /* lj_obj.h:90 */
```

`gcnext(gc)` 沿着 nextgc 字段取下一个对象,GC 遍历主链就用它。

### 4.2 GCobj:一个 union 把所有 GC 类型叠在一起

所有具体 GC 类型(string、table、function...)都有 GCHeader 在最前面。LuaJIT 用一个 union 把它们叠起来,这就是 `GCobj`(`lj_obj.h:754`):

```c
typedef union GCobj {
  GChead gch;
  GCstr str;
  GCupval uv;
  lua_State th;
  GCproto pt;
  GCfunc fn;
  GCcdata cd;
  GCtab tab;
  GCudata ud;
} GCobj;
```

union 的特性是所有成员共享同一段内存。既然所有成员都以 GCHeader 开头(它们都把 `GCHeader` 放在结构体最前),那么 `gcobj->gch`、`gcobj->str`、`gcobj->tab` 读出来的 GCHeader 部分**是同一片内存**。这样:

- GC 想遍历、标记一个对象时,不管它是什么类型,直接用 `gcobj->gch.nextgc`、`gcobj->gch.marked` 操作即可(把任意 GCobj 当 `GChead` 看)。
- 业务代码想用具体类型时,再用转换宏取出具体字段:`gco2tab(o)`(`lj_obj.h:773`)把 GCobj 当成 GCtab,`gco2str(o)` 当成 GCstr,等等。

```c
#define gco2str(o)	check_exp((o)->gch.gct == ~LJ_TSTR, &(o)->str)
#define gco2tab(o)	check_exp((o)->gch.gct == ~LJ_TTAB, &(o)->tab)
#define gco2func(o)	check_exp((o)->gch.gct == ~LJ_TFUNC, &(o)->fn)
... /* lj_obj.h:767-774 */
```

`check_exp` 在调试构建里断言类型对得上,发布构建是空操作。这是 C 里实现"带运行时类型检查的 downcast"的常见手法。

### 4.3 GChead:统一头部之外的额外字段

GCobj 除了 GCHeader 这 6 字节,还有一些**几乎所有 GC 类型都需要的字段**(环境表、gclist、metatable)。这些被收进 `GChead`(`lj_obj.h:731`):

```c
typedef struct GChead {
  GCHeader;
  uint8_t unused1;
  uint8_t unused2;
  GCRef env;
  GCRef gclist;
  GCRef metatable;
} GChead;
```

LuaJIT 用 `STATIC_ASSERT` 保证所有具体 GC 类型(string/tab/func/udata 等)的 `env`、`gclist`、`metatable` 字段偏移都和 `GChead` 一致。这样 GC 处理 metatable、traverse gclist 时,可以不区分类型直接操作这些字段。

### 4.4 注意:`gct` 里存的是 `~LJ_T*`,不是 `LJ_T*`

一个容易混淆的点:GCobj 头里的 `gct` 字段存的是 `~LJ_TSTR`(=4)、`~LJ_TTAB`(=11)这种**正数**(单字节),而 TValue 里 itype 字段存的是 `LJ_TSTR`(=0xFFFFFFFB)这种**负数**(4 字节)。

为什么反过来?因为 GCobj 头里 gct 是单字节,只有 256 种取值,要存 14 个类型,直接存类型编号(`~LJ_TSTR = 4`,落在 0..255)即可;而 TValue 的 itype 是 4 字节,要构成 NaN 高位模式,必须存 `~n` 形式的负数。**两套编码,一正一负,因为字宽不同选择了不同的位模式**。读代码时要小心分清。

gco2str 里的断言 `(o)->gch.gct == ~LJ_TSTR`——这里 `~LJ_TSTR` 中 `LJ_TSTR = ~4u`,所以 `~LJ_TSTR = ~(~4u) = 4u`,正好匹配 gct 里存的单字节正数 4。

### 4.5 主链的建立

主线程(state)是 GC 主链上的第一个对象。在 `lj_state.c:305` 的 `lua_newstate` 里:

```c
setgcref(g->gc.root, obj2gco(L));
```

之后每创建一个 GC 对象(Table/String/...),它都被插到这条链上(`g->gc.root` 是链头)。关闭 state 时,`lj_state.c:219` 有断言:

```c
lj_assertG(gcref(g->gc.root) == obj2gco(L), "main thread is not first GC object");
```

GC 全量遍历就从 `g->gc.root` 开始,沿 `nextgc` 走完所有对象。这个机制是 GC 能"不漏对象"的基础——任何分配的 GC 对象都在链上,GC 一定能扫到。

注意字段名是 `g->gc.root`,不是 PUC-Lua 的 `g->rootgc`。LuaJIT 把 GC 状态收在 `global_State` 里的一个嵌套结构 `GCState gc`(`lj_obj.h:593`)里,所以是 `g->gc.root`、`g->gc.total`、`g->gc.state` 这种写法。GC 状态枚举在 `lj_gc.h:11`:

```c
enum {
  GCSpause, GCSpropagate, GCSatomic, GCSsweepstring, GCSsweep, GCSfinalize
};
```

增量 GC 在这些状态间推进:暂停 → 传播标记 → 原子阶段 → 扫字符串 → 扫其他对象 → 终结。这些细节是 P6-21 的内容,这里只先建立"所有 GC 对象串在一条链上,统一头部让 GC 一视同仁"的概念。

---

## §5 Table:数组 + 哈希的混合表

Lua 的 table 是核心数据结构。它既是数组(用整数下标访问 `t[1]`、`t[2]`),又是哈希表(用任意 key 访问 `t.name`、`t["key"]`)。LuaJIT 的实现把这两部分合并到一个结构里。

### 5.1 GCtab 结构

Table 的结构体叫 `GCtab`(注意不是 `Table`,文件叫 `lj_tab.c/lj_tab.h`,但结构体名带 GC 前缀),定义在 `lj_obj.h:498`:

```c
typedef struct GCtab {
  GCHeader;
  uint8_t nomm;		/* Negative cache for fast metamethods. */
  int8_t colo;		/* Array colocation. */
  MRef array;		/* Array part. */
  GCRef gclist;
  GCRef metatable;	/* Must be at same offset in GCudata. */
  MRef node;		/* Hash part. */
  uint32_t asize;	/* Size of array part (keys [0, asize-1]). */
  uint32_t hmask;	/* Hash part mask (size of hash part - 1). */
#if LJ_GC64
  MRef freetop;		/* Top of free elements. */
#endif
} GCtab;
```

字段一个一个看,每个都有它的理由:

**`GCHeader`**:和其他 GC 对象一样,Table 也是 GC 对象,要能被 GC 遍历。所以头部是统一的 GCHeader。

**`nomm`**(negative cache for fast metamethods):元方法的负缓存。Lua 的 table 可以挂 metatable,metatable 里可以定义 `__index`、`__add` 等元方法(metamethod)。每次访问 table 字段如果没命中,都要去查 metatable——很慢。`nomm` 是个位图,记录"这个 table **没有**哪些元方法"(所以叫 negative cache——缓存的是"没有")。如果某次查找确认这个 table 没有 `__index`,`nomm` 的对应位被置上,以后再查 `__index` 直接看位图就知道没有,跳过 metatable 查找。这是为热路径优化。

**`colo`**(array colocation):数组同置标记。LuaJIT 有个小优化——如果表很小(数组部分 ≤ 16 个元素),数组部分和表头**一起分配**(colocate),省一次 malloc。`colo` 记录"数组部分跟在表头后面有多远"(0 表示没有同置)。`LJ_MAX_COLOSIZE = 16`(`lj_def.h:62`)是同置上限。

**`array`**(`MRef`,内存引用):指向数组部分。数组部分是一段连续的 TValue 数组,存整数下标 0 到 asize-1 的值。注意是 `MRef`(普通内存引用)不是 `GCRef`(GC 引用)——数组部分本身不是单独的 GC 对象,它是 Table 的一部分。

**`gclist`**:GC 遍历用(挂灰对象链等)。

**`metatable`**:指向元表。注释说"Must be at same offset in GCudata"——userdata 也有 metatable 字段,且偏移必须和 Table 一致,这样 GC 处理 metatable 时可以不区分 Table 还是 userdata。

**`node`**(`MRef`):指向哈希部分。哈希部分是一个 Node 数组(每个 Node 存一个 key-value 对)。

**`asize`**(uint32_t):数组部分大小。注释明确"keys [0, asize-1]"——整数 key `k` 满足 `0 <= k < asize` 落在数组部分,否则落哈希部分。

**`hmask`**(uint32_t):哈希部分掩码。注释"size of hash part - 1"——哈希桶数是 `hmask + 1`,必须是 2 的幂(这样 `hash & hmask` 就是取模)。

**`freetop`**(仅 GC64):哈希部分空闲节点栈顶。非 GC64 时这个字段存在 Node 数组的开头(`node[0].freetop`,见下),GC64 时挪到 Table 结构里(因为 Node 结构在 GC64 下变大了,塞 freetop 不划算)。

### 5.2 Node:哈希节点

哈希部分的元素叫 Node(`lj_obj.h:487`):

```c
typedef struct Node {
  TValue val;		/* Value object. Must be first field. */
  TValue key;		/* Key object. */
  MRef next;		/* Hash chain. */
#if !LJ_GC64
  MRef freetop;		/* Top of free elements (stored in t->node[0]). */
#endif
} Node;

LJ_STATIC_ASSERT(offsetof(Node, val) == 0);
```

一个 Node 装三样东西:

- **`val`**:这个 key 对应的值(TValue)。
- **`key`**:这个节点的 key(也是 TValue,因为 key 可以是任意 Lua 值,除了 nil)。
- **`next`**:哈希冲突链。如果两个 key 哈希到同一个桶,它们用 next 串成链表,查找时沿链比较 key。

注意一个反直觉的细节:**val 在前,key 在后**。一般直觉是先 key 后 value,这里反过来。`STATIC_ASSERT(offsetof(Node, val) == 0)` 强制 val 必须在偏移 0。为什么?为了让 Node 的 val 字段和 TValue 数组里某个元素的地址一致——这样 JIT 生成访问 table 字段的机器码时,可以统一用"取 Node 第 0 字节开始的 TValue"的逻辑,不用区分"这个值在数组部分还是哈希部分"。一个看似无关紧要的字段顺序,背后是为机器码的简洁性服务。

### 5.3 数组 vs 哈希:谁存什么

LuaJIT Table 的核心决策是:**整数 key 优先存数组部分,其他 key 存哈希部分**。

具体规则(见 `lj_tab.h:79` 的 `inarray` 宏):

```c
#define inarray(t, key)		((MSize)(key) < (MSize)(t)->asize)
#define arrayslot(t, i)		(&tvref((t)->array)[(i)])
```

整数 key `k`:**`0 <= k < asize` 就在数组部分**,直接取 `array[k]`(O(1),一次访存);否则落哈希部分,走哈希查找。

为什么这样分?两个理由:

**第一,数组部分更快**。整数下标访问是直接索引 `array[k]`,没有任何哈希计算、没有冲突链遍历,一次内存读就够。Lua 程序里大量用 table 当数组(`local t = {}; for i=1,n do t[i] = ... end`),这种访问模式走数组部分,极快。

**第二,节省内存**。如果用纯哈希表存数组,每个元素除了值还要存 key(也是 TValue),加上 next 指针,一个元素占 24+ 字节。用 TValue 数组,每个元素就 8 字节(一个 TValue),省 2/3。

那 asize 怎么定?LuaJIT 在表第一次插入整数 key 时,统计这个表里所有整数 key 的分布,算出一个"最佳 asize"——让尽量多的整数 key 落进数组部分(避免它们去哈希),同时不让数组部分太空(避免浪费)。这个逻辑在 `lj_tab.c` 的 `countint`(`:294`)、`countarray`(`:308`)、`counthash`(`:330`)、`bestasize`(`:345`)、`rehashtab`(`:357`)几个函数里,核心是:**根据已知的整数 key 分布,选一个 asize 让数组的密度最优**。具体算法不展开,领会"asize 是动态计算的、为了让数组部分尽可能装下整数 key"即可。

### 5.4 哈希函数

哈希部分怎么把 key 映射到桶?核心是 `lj_tab.h:42-51` 的一组宏:

```c
#define hashstr(t, s)		hashmask(t, (s)->sid)
#define hashlohi(t, lo, hi)	hashmask((t), hashrot((lo), (hi)))
#define hashnum(t, o)		hashlohi((t), (o)->u32.lo, ((o)->u32.hi << 1))
#if LJ_GC64
#define hashgcref(t, r) \
  hashlohi((t), (uint32_t)gcrefu(r), (uint32_t)(gcrefu(r) >> 32))
#else
#define hashgcref(t, r)		hashlohi((t), gcrefu(r), gcrefu(r) + HASH_BIAS)
#endif
```

四种 key 四种哈希:

- **string**:用字符串的 `sid`(intern 时分配的短 ID,见 P1-02)直接做哈希。**字符串的哈希在它被创建(intern)那一刻就算好了,sid 就是哈希结果**,查表时直接拿来用,不再算。这是 LuaJIT 字符串查表快的原因。
- **number**:用 double 的 lo 和 hi(左移 1 位避免高位丢失)喂进 `hashrot`。
- **GC 对象(table/function/...)**:用指针的位喂进 `hashrot`。GC64 用指针的高低 32 位分别喂,非 GC64 用 `gcrefu + HASH_BIAS`(`HASH_BIAS = -0x04c11db7`,`lj_tab.h:12`,CRC 多项式常数,用来打乱指针高位)。
- **bool**:直接用值(0 或 1)索引。

`hashrot`(`lj_tab.h:18`)是个旋转哈希,把 lo 和 hi 两个 32 位混合成一个 32 位哈希值:

```c
static LJ_AINLINE uint32_t hashrot(uint32_t lo, uint32_t hi)
{
#if LJ_TARGET_X86ORX64
  lo ^= hi; hi = lj_rol(hi, HASH_ROT1);
  lo -= hi; hi = lj_rol(hi, HASH_ROT2);
  hi ^= lo; hi -= lj_rol(lo, HASH_ROT3);
#else
  ...
#endif
  return hi;
}
```

`HASH_ROT1=14`、`HASH_ROT2=5`、`HASH_ROT3=13`(`lj_tab.h:13-15`)。这是 Knuth/Multiplicative 哈希的变体,位旋转+加减混合,分布均匀且运算极快(几条移位指令)。x86 版本刻意写成"两操作数友好"的形式(`lo ^= hi; hi = rol(hi, n); ...`),让 GCC 编出来的指令更紧凑。

哈希常量都用 2 的幂或小常数,因为编译成移位指令比乘除快得多。这是系统编程里"为编译器友好"的典型例子。

### 5.5 查找:get 路径

通用查找 `lj_tab_get` 在 `lj_tab.c:401`:

```c
cTValue * LJ_FASTCALL lj_tab_get(const GCtab *t, cTValue *key)
{
  if (tvisstr(key)) {
    return lj_tab_getstr(t, strV(key));
  } else if (tvisint(key)) {
    return lj_tab_getint(t, intV(key));
  } else if (tvisnum(key)) {
    /* ... 转成整数看能不能走数组 ... */
  } else if (tvisnil(key)) {
    return niltv(L);
  } else {
    return genlookup(t, key);  /* 走 hashkey */
  }
}
```

整数 key 走 `lj_tab_getint`(`lj_tab.h:81`,内联宏):

```c
#define lj_tab_getint(t, key) \
  (inarray((t), (key)) ? arrayslot((t), (key)) : lj_tab_getinth((t), (key)))
```

**先判 inarray**:在数组范围里直接返回数组槽(O(1));不在才走 `lj_tab_getinth` 查哈希(O(1) 平均,O(n) 最坏冲突)。

字符串 key 走 `lj_tab_getstr`(`lj_tab.c:391`),用 sid 直接哈希,沿冲突链比较字符串指针(LuaJIT 字符串 intern 过,指针相等即字符串相等,不用 memcmp)。

### 5.6 插入新 key:newkey

插入一个表里还没有的新 key,走 `lj_tab_newkey`(`lj_tab.c:436`)。这个函数用的是 Brent 哈希变体——一种在插入时通过移动已有节点来减少冲突链长度的算法,比朴素的链地址法查找更快。细节不展开(P2 trace 录制时再细看),只要知道:**新 key 插入可能触发 rehash**(哈希部分满了要扩容),那时表的所有 key 重新哈希、重新分布。

对外扩容的入口是 `lj_tab_reasize`(`lj_tab.c:371`,注意 Mike Pall 拼成 `reasize` 少一个 i,这是源码事实不是错别字),实际逻辑在 static 函数 `rehashtab`(`lj_tab.c:357`)。统计逻辑在前面提到的 `countint`/`bestasize` 系列。

### 5.7 next 遍历与 LJ_KEYINDEX(2.1 新增)

Lua 的 `next(t, k)` 函数返回表里 k 之后的下一个 key-value 对,用于 `for k, v in pairs(t)`。这个遍历要保证每个元素被访问一次且仅一次。

LuaJIT 2.1 加了一个优化:用一种叫 **LJ_KEYINDEX** 的特殊 TValue tag,把"遍历位置"编码成一个线性索引,而不是真实的 key。定义在 `lj_obj.h:287`:

```c
/* Type marker for slot holding a traversal index. Must be lightuserdata. */
#define LJ_KEYINDEX		0xfffe7fffu
```

这个 tag 表现为 lightuserdata 类型,但低 32 位存的是一个**线性索引**:

- 索引 `[0, asize-1]`:对应数组部分的第几个元素。
- 索引 `[asize, asize+hmask]`:对应哈希部分的第几个 Node。
- 索引 `~0u`:无效 key。

函数 `lj_tab_keyindex`(`lj_tab.c:573`)把任意 key 转成这个线性索引,`lj_tab_next`(`lj_tab.c:602`)消费索引返回下一对。这样,`pairs` 遍历在 JIT 录制时,可以把"取下一个 key"编译成对线性索引的简单递增和边界比较,避免每次都重新哈希查 key。这是 2.1 ROLLING 相对老版的一个性能改进点(P2-06 录制时会用到)。

注意:**LJ_KEYINDEX 是 TValue 的 tag,不改 Table 结构**。Table 没有因为 LJ_KEYINDEX 多任何字段(`hmask2` 这种东西不存在)。索引存在 TValue 的低字里,跟着控制流走,不挂在表上。

### 5.8 表长度:#t

`#t` 运算符返回表的"长度"。Lua 5.x 的语义有歧义(任何 nil 边界都算合法),LuaJIT 的实现在 `lj_tab_len`(`lj_tab.c:655`),快路径 + 慢路径(`tab_len_slow`,`:630`)。快路径用二分查找找一个边界 n,使得 `t[n]` 非 nil、`t[n+1]` 是 nil。慢路径处理有空洞的情况。这个细节 P2 录制时如果碰到再展开,这里建立"表长度是个非平凡计算"的概念即可。

### 5.9 对照官方 Lua 的 Table

官方 Lua 的 Table 也是数组+哈希,字段类似(`array`、`node`、`asize`、`lsizenode` 是哈希大小的对数)。区别在:

- 官方 Lua 用 `lsizenode`(log2 大小)而非 `hmask`(大小-1)。LuaJIT 用 hmask 是因为 `hash & hmask` 比 `hash >> lsizenode` 在某些架构上编译更友好(虽然现代编译器两者都能优化)。
- 官方 Lua 的 Node 里 key 和 val 是分开的 key/value 字段;LuaJIT 强制 val 在偏移 0,为 JIT 生成统一访问代码。
- LuaJIT 有 `nomm` 负缓存和 `colo` 同置优化,官方 Lua 没有。
- LJ_KEYINDEX 是 LuaJIT 2.1 独有,加速 pairs 遍历的 JIT 录制。

这些差异单看不大,累积起来让 LuaJIT 的 Table 操作比官方 Lua 快不少。更关键的是,**LuaJIT 的 Table 字段布局是为 JIT 生成代码服务的**(val 在偏移 0、inarray 是单条比较),官方 Lua 没有这个约束。

---

## §6 lua_State:执行状态

值和表都讲了,最后看**执行状态**——一个 Lua 协程(线程)运行时持有的全部上下文。

### 6.1 lua_State 结构

`lua_State` 在 `lj_obj.h:691`(注意是 `struct lua_State`,不是 GCState):

```c
struct lua_State {
  GCHeader;
  uint8_t dummy_ffid;	/* Fake FF_C for curr_funcisL() on dummy frames. */
  uint8_t status;	/* Thread status. */
  MRef glref;		/* Link to global state. */
  GCRef gclist;		/* GC chain. */
  TValue *base;		/* Base of currently executing function. */
  TValue *top;		/* First free slot in the stack. */
  MRef maxstack;	/* Last free slot in the stack. */
  MRef stack;		/* Stack base. */
  GCRef openupval;	/* List of open upvalues in the stack. */
  GCRef env;		/* Thread environment (table of globals). */
  void *cframe;		/* End of C stack frame chain. */
  MSize stacksize;	/* True stack size (incl. LJ_STACK_EXTRA). */
};
```

字段读法:

**`GCHeader`**:lua_State 自己也是 GC 对象(协程可以被回收),所以头部是 GCHeader。它的 `gct` 是 `~LJ_TTHREAD`。

**`dummy_ffid`**:dummy frame 用的伪函数 ID,细节和 FF_C 相关,不影响主线理解。

**`status`**:线程状态(运行/挂起/正常/死)。

**`glref`**(`MRef`):指向 `global_State`。每个 lua_State 关联一个全局状态。`G(L)` 宏(`lj_obj.h:707`): `#define G(L) (mref(L->glref, global_State))` 解出 global_State 指针。

**`gclist`**:GC 灰链。

**`base`** / **`top`** / **`maxstack`** / **`stack`**:这四个字段定义了**值栈**。这是 lua_State 最核心的部分,下一节专门讲。

**`openupval`**:开 upvalue 链。Lua 的闭包捕获外层局部变量,被捕获的变量如果还活在栈上,就由一个 upvalue 对象"包装"住,串在 openupval 链上。当外层函数返回、变量要离开栈时,upvalue 把值"关"到堆上(变成 closed upvalue)。这是 Lua 闭包的实现机制,细节不在本章。

**`env`**:线程的环境表(全局变量表)。每个协程可以有独立的全局环境。

**`cframe`**(void *):C 栈帧链尾。Lua 调 C 函数、C 又回调 Lua,这种嵌套调用的 C 栈帧链,由 cframe 串起来,用于错误处理时正确回退。

**`stacksize`**:栈的真实大小(含 `LJ_STACK_EXTRA` 个额外槽,留作 VM 内部临时空间)。

### 6.2 值栈:base/top/maxstack/stack

LuaJIT 的字节码 VM 是**栈式**的(P1-02 会详讲),指令操作的是栈上的 TValue。栈就是一段连续的 TValue 数组。四个指针划出栈的不同区域:

```
低地址                                            高地址
|___stack___|...|_base____|___当前函数的寄存器/局部变量___|_top_|___空闲___|___maxstack___|
```

- **`stack`**(MRef):整个栈数组的起点(最底)。
- **`base`**(TValue *):**当前执行函数的帧起点**。函数的参数和局部变量从 base 开始编号——`base[0]` 是第一个寄存器,`base[1]` 是第二个,以此类推。每调用一个新函数,base 就被新函数的帧起点覆盖。
- **`top`**(TValue *):栈顶,**第一个空闲槽**。半开区间 `[base, top)` 是当前活跃值。函数往栈上压值,top 往上移;弹出,top 往下移。
- **`maxstack`**(MRef):栈上限。栈不能无限增长,超过 maxstack 要扩容(或者报栈溢出错误)。

栈布局的初始化在 `lj_state.c:180`(`stack_init` 函数):

```c
setthreadV(L1, st++, L1);  /* Needed for curr_funcisL() on empty stack. */
if (LJ_FR2) setnilV(st++);
L1->base = L1->top = st;
```

栈最底槽 `[0]` 存 lua_State 自己(用于空栈时 `curr_funcisL()` 仍能工作)。LJ_FR2 模式下 `[1]` 存 nil(占位第二帧槽),然后 base 和 top 都指向 `[1+LJ_FR2]`。

### 6.3 LJ_FR2:两槽帧信息

`LJ_FR2` 是 2.1 引入的编译开关(`lj_arch.h:604`):GC64 模式下自动开启(`LJ_GC64 ⇒ LJ_FR2`)。它改变了栈帧的布局。

非 FR2(单槽帧),每个函数调用的栈帧头部占**一个 TValue**,这个 TValue 把"函数引用"和"帧类型 + 上一帧大小"打包在一起:

```
              base-1              |  base  base+1 ...
              lo     hi           |
             [func | PC/delta/ft] | [slots ...]
```
(`lj_frame.h:53` 注释)

FR2(两槽帧),栈帧头部占**两个 TValue**,一个存函数,一个存 PC/帧类型:

```
                   base-2  base-1      |  base  base+1 ...
                  [func   PC/delta/ft] | [slots ...]
                  ^-- frame            | ^-- base   ^-- top
```
(`lj_frame.h:33` 注释)

为什么 GC64 要两槽?因为单槽帧把 PC(返回地址)和帧大小打包进一个 32 位字段(`ftsz`),但在 64 位平台上 PC 是 64 位,塞不下。所以 GC64 必须用两个 TValue(共 16 字节,够装 64 位 PC + 64 位帧信息)。这是 GC64 带来的连锁改动之一。

帧类型用 `ftsz` 字段的低位标记(`lj_frame.h:24`):

```c
enum {
  FRAME_LUA, FRAME_C, FRAME_CONT, FRAME_VARG,
  FRAME_LUAP, FRAME_CP, FRAME_PCALL, FRAME_PCALLH
};
```

`FRAME_LUA=0`(Lua 函数帧)、`FRAME_C=1`(C 函数帧)、`FRAME_CONT=2`(continuation 帧,用于 pcall 等)、`FRAME_VARG=3`(可变参数帧)等。因为 `FRAME_LUA=0` 且 Lua 函数的 PC 总是 4 字节对齐(低位 00),所以"是 Lua 帧"可以单条 `and` 判断。这种"利用对齐低位编码类型"是经典的低位 tag 技巧,和 NaN-boxing 是同一思路。

### 6.4 global_State:全局状态

`global_State` 在 `lj_obj.h:634`,字段较多,核心的几类:

```c
typedef struct global_State {
  lua_Alloc allocf;	/* Memory allocator. */
  void *allocd;		/* Memory allocator data. */
  GCState gc;		/* Garbage collector. */
  ...
  StrInternState str;	/* String interning. */
  volatile int32_t vmstate;  /* VM state or current JIT code trace number. */
  GCRef mainthref;	/* Link to main thread. */
  ...
  GCRef cur_L;		/* Currently executing lua_State. */
  MRef jit_base;	/* Current JIT code L->base or NULL. */
  ...
  PRNGState prng;	/* Global PRNG state. */
  GCRef gcroot[GCROOT_MAX];  /* GC roots. */
} global_State;
```

关键几项:

- **`allocf` / `allocd`**:内存分配器函数指针和数据。LuaJIT 默认用自己的分配器(`lj_alloc.c`),也允许用户自定义。所有内存分配走这个。
- **`gc`**(`GCState`):GC 全部状态——已分配内存总量、阈值、当前白位、GC 阶段、灰链、弱表链等等。前面 §4.5 提过。
- **`str`**(`StrInternState`):字符串 intern 表。所有 Lua 字符串都 intern(相同内容的字符串只有一份),这里存哈希表用于查找。intern 是 string 比较能用指针相等判等的原因,也是 sid 的来源。
- **`vmstate`**:VM 当前状态(跑解释器/跑 C 代码/跑第 N 号 trace)。side exit 时用来判断"刚才在干什么"。
- **`mainthref`**:主线程引用。每个 lua_State universe 有一个主线程,协程是从主线程派生的。
- **`cur_L`**:当前正在执行的 lua_State。协程切换时改这个。
- **`jit_base`**:JIT 代码运行时的 L->base。机器码跑起来后,解释器的 base 失效,用 jit_base 记录当前 base 给 side exit 恢复用。
- **`prng`**(`PRNGState`):全局伪随机数状态(2.1 新增,用于哈希种子等)。
- **`gcroot[GCROOT_MAX]`**:GC 根数组。包括注册表、各基础类型的 metatable(`GCROOT_BASEMT` 等,见 `lj_obj.h:575-586`)。这些是 GC 遍历的起点。

### 6.5 GG_State:三位一体

到这里你可能注意到一个问题:`global_State` 里没有 `jit_State *J` 字段。JIT 状态去哪了?

答案在 `GG_State`(`lj_dispatch.h:89`):

```c
typedef struct GG_State {
  lua_State L;				/* Main thread. */
  global_State g;			/* Global state. */
#if LJ_TARGET_ARM && !LJ_TARGET_NX
  uint8_t align1[(16-sizeof(global_State))&15];
#endif
#if LJ_TARGET_MIPS
  ASMFunction got[LJ_GOT__MAX];
#endif
#if LJ_HASJIT
  jit_State J;				/* JIT state. */
  HotCount hotcount[HOTCOUNT_SIZE];	/* Hot counters. */
  ...
#endif
  ASMFunction dispatch[GG_LEN_DISP];	/* Instruction dispatch tables. */
  BCIns bcff[GG_NUM_ASMFF];		/* Bytecode for ASM fast functions. */
} GG_State;
```

`GG_State` 是 **lua_State + global_State + (jit_State + hotcount) + dispatch 表** 的合体结构。**它们被一次性分配在同一块连续内存里**。注释(`lj_dispatch.h:88`):"Global state, main thread and extra fields are allocated together."

为什么合体?一个关键优化:**让 dispatch 表和 g 之间的偏移在编译期固定**。解释器每条字节码都要查 dispatch 表(P1-03 会讲),如果 dispatch 表的地址能从一个固定基址+固定偏移算出,就不用每次加载基址。ARM 上还有更精细的对齐(`align1`、`align2`)让 dispatch 表落在 K12 寻址范围(单条指令能寻址的 4KB 偏移)内,这样查 dispatch 表只要一条 `ldr` 指令——这是为 ARM 解释器速度做的微优化。

转换宏(`lj_dispatch.h:111-118`):

```c
#define GG_OFS(field)	((int)offsetof(GG_State, field))
#define G2GG(gl)	((GG_State *)((char *)(gl) - GG_OFS(g)))
#define J2GG(j)		((GG_State *)((char *)(j) - GG_OFS(J)))
#define L2GG(L)		(G2GG(G(L)))
#define J2G(J)		(&J2GG(J)->g)
#define G2J(gl)		(&G2GG(gl)->J)
#define L2J(L)		(&L2GG(L)->J)
```

任意一个 L/g/J 指针,通过固定偏移加减就能得到另外两个。这就是为什么 global_State 不需要显式存 J 指针——它在 GG_State 里的位置是编译期固定的。

`lua_newstate` 里一次性分配(`lj_state.c:269`):

```c
GG = (GG_State *)allocf(allocd, NULL, 0, sizeof(GG_State));
...
L = &GG->L;
g = &GG->g;
```

然后 `setmref(L->glref, g)`(`lj_state.c:281`)把 L 和 g 关联起来。整个 universe 的根就是这一个 GG_State 分配。

### 6.6 对照官方 Lua 的 lua_State

官方 Lua 也有 `lua_State`(在 `lstate.h`),字段大体相似:`base`、`top`、`stack`、`stacksize`、`ci`(调用信息链,对应 LuaJIT 的 cframe+帧)、`openupval` 等。几个关键差异:

- 官方 Lua 用 `CallInfo *ci` 维护调用栈(LuaJIT 把调用信息编码进值栈的帧槽,即 base-1 或 base-2 那个 TValue,省一个独立的数据结构)。
- 官方 Lua 的 lua_State 没有为 JIT 优化的字段(`jit_base` 等),因为没 JIT。
- 官方 Lua 不区分 FR1/FR2,栈帧布局固定。
- LuaJIT 的 GG_State 合体分配是官方 Lua 没有的(`global_State` + `lua_State` 在官方 Lua 是分开分配,通过 `g->mainthread` 互指)。

这些差异都指向同一个方向:**LuaJIT 在 lua_State 层就把"为 JIT 友好"考虑进去了**——jit_base 字段、合体分配的 dispatch 表、帧信息编码进栈,都是为了机器码运行时能最快地访问到所需状态。

---

## §7 为什么这么表示:JIT 友好在数据层的体现

讲到这里,值、Table、State 三块都过了一遍。现在回到这一章的核心问题:**LuaJIT 为什么不厌其烦地搞 NaN-boxing、统一 GC 头、紧凑帧布局?** 答案是主线里的"快"字。我们把代价和收益摆出来看。

### 7.1 紧凑 = 缓存友好

最直接的收益是**缓存命中率**。值从 16 字节压到 8 字节,意味着:

- 函数的局部变量数组(栈上一段 TValue)更小,更多变量能塞进同一个缓存行。
- 表的数组部分更紧凑,遍历数组时缓存命中率高。
- 一个 64 字节缓存行,LuaJIT 能装 8 个 TValue,官方 Lua 装 4 个。

现代 CPU 上,一次 L1 缓存命中约 1 纳秒,L2 是 4 纳秒,L3 是十几纳秒,主存是几十上百纳秒。**缓存命中率每提高 1%,整体性能可能提升几个百分点**——这是 JIT 之外、纯靠数据表示拿到的速度。

### 7.2 类型 tag 位置 = guard 便宜

guard 是 JIT 的灵魂(P0-01 讲过)。每条机器码都可能插 guard,而 guard 的核心动作是"读出值的类型 tag,跟假设的类型比较"。

NaN-boxing 把 tag 放在固定的位(TValue 的 MSW 或 GC64 下的高 17 位),让 guard 编译成:**一条 load(读 TValue)、一条 shift+mask(取 tag)、一条 cmp、一条条件跳**。四条指令,纳秒级。

如果用官方 Lua 的表示,tag 在 `o->tt`,虽然也是一条 load,但 TValue 本身是 16 字节(可能跨缓存行),而且判断类型往往要多次比较(因为没有 §2.4 那种"用 `~n` 让分类变成小于等于"的设计)。两相比较,guard 的开销差几倍。**guard 是每条机器码都要做的,差几倍就是整体差几倍**。

### 7.3 内联 = 无函数调用开销

LuaJIT 的所有值操作宏(`setnumV`、`tvisstr`、`gcval` 等)都标 `LJ_AINLINE` 强制内联。编译后它们就是几条内存读写指令嵌在调用点,没有函数调用的开销(压栈、跳转、返回)。

这之所以能做到,是因为值的表示足够简单——一个 union + 几个位运算,内联后代码膨胀小。如果表示复杂(比如要 deref 多层指针、查类型表),内联会让代码膨胀到不可接受。**紧凑的表示是内联的前提,内联又是无开销的前提**。

### 7.4 字段布局 = 机器码统一

Table 的 Node 把 val 放在偏移 0、`inarray` 是单条比较、`hashstr` 用预计算的 sid——每一个这种"看起来无关紧要的字段顺序选择",背后都是为了让 JIT 生成的机器码尽可能短、尽可能通用。

举例:JIT 录制 `t[i]` 这种整数下标访问时,生成的机器码大致是:

```asm
  mov  rax, [t + offsetof(GCtab, asize)]   ; 读 asize
  cmp  i, rax                              ; i < asize?
  jae  .hash_part                          ; 不在数组,跳哈希
  mov  rax, [t + offsetof(GCtab, array)]   ; 读 array 指针
  mov  result, [rax + i*8]                 ; 取 array[i](TValue 是 8 字节,i*8 直接索引)
  jmp  .done
.hash_part:
  ...
```

(示意,真实机器码见 P4-14)

注意 `i*8` 这个乘法——因为 TValue 是 8 字节,数组索引是 `i * sizeof(TValue) = i * 8`,CPU 用一条 `lea` 或 `mov` 的 scale 字段就完成,几乎免费。如果 TValue 是 16 字节,就是 `i*16`,虽然也能算,但缓存压力大一倍。

**整个访问路径,从 inarray 判断到取值,机器码就 4-5 条指令,全内联,没有间接跳**。这是 LuaJIT 的 table 访问快的原因——不是某个魔法优化,而是从最底层的字段布局开始,每一步都为机器码效率服务。

### 7.5 GCobj 统一头 = GC 与 JIT 解耦

所有 GC 对象共享 GCHeader,意味着 GC 不需要知道对象具体是什么类型就能遍历、标记。这对 JIT 有两个好处:

- **JIT 生成的代码不需要在 GC 触发时做特殊处理**。GC 走自己的统一链,JIT 代码继续跑,两者解耦。
- **guard 检查对象类型用 `gct` 单字节**,统一接口。检查一个值是不是表,就是 `gcval(o)->gch.gct == ~LJ_TTAB`,一条 load + 一条 cmp。

### 7.6 取舍:代价是什么

代价我们也诚实说清楚:

- **可读性差**。NaN-boxing 的位编码、`~n` 形式的类型常量、各种移位掩码,读源码时不熟悉的人完全看不懂。Mike Pall 自己在 `lj_obj.h:224-258` 写了一大段注释解释位布局,正是因为不解释没人看得懂。
- **平台假设**。47 位指针假设、IEEE 754 兼容假设。如果未来硬件改了(5 级页表、非 IEEE 浮点),需要重新设计。
- **调试困难**。用调试器看一个 TValue,默认显示的是 union 第一个成员(u64),看不出它到底是数还是字符串,要手动解释位模式。
- **GC64 vs 非 GC64 两套代码**。很多宏有 `#if LJ_GC64` 两个分支,维护成本高。

这些代价,对于一个把性能当命脉的 JIT 运行时,完全是值得的。Mike Pall(LuaJIT 唯一的作者)选择了**用实现复杂度换运行时速度**——这正是 LuaJIT 整个设计的基调,也是它能比官方 Lua 快几十倍的原因之一。

---

## §8 ★对照:官方 Lua 与 JVM/V8

把这章学的和两个对照对象放在一起看。

### 8.1 对照一:官方 Lua(16 字节 tagged TValue 对照 8 字节 NaN-boxing)

| 维度 | LuaJIT(2.1 ROLLING) | 官方 Lua 5.x |
|---|---|---|
| TValue 大小 | 8 字节(一个 64 位字) | 16 字节(tag + Value union,带对齐) |
| 类型 tag 编码 | NaN-boxing,tag 藏在 NaN 位里 | 独立 `int tt` 字段 |
| 取类型 | 一条移位指令(GC64)或读 MSW(非 GC64) | 读 `o->tt` 字段 |
| 类型判断 | 单条整数比较(得益于 `~n` 排序) | 往往需要多次比较或 switch |
| GC 对象统一头 | GCHeader(nextgc/marked/gct,6 字节) | `CommonHeader`(next/tt,类似) |
| Table 字段 | nomm 负缓存、colo 同置、val 偏移 0 | 无 nomm/colo |
| 栈帧 | 编码进值栈(FR2 两槽/FR1 单槽) | 独立 CallInfo 链 |
| 全局状态 | GG_State 合体分配(L/g/J/dispatch) | global_State 独立分配 |

最核心的差异是 **TValue 大小**:8 vs 16。这一项差异,通过缓存命中率的杠杆,放大成整体性能的显著差距。这是 LuaJIT 数据表示上**最关键的一个决策**,也是这章的主线。

其次是**类型判断的方式**。官方 Lua 因为 tag 是独立字段、且类型常量没有排序设计,判断"是不是数"这种分类,往往是一串 if-else 或 switch。LuaJIT 用 `~n` 排序,让分类变成单条整数比较,这对每条字节码都要判类型的解释器、对每条机器码都可能插 guard 的 JIT,都是直接的红利。

### 8.2 对照二:JVM / V8(method JIT)

JVM 和 V8 也面临"动态类型怎么表示"的问题,但它们的解法和 LuaJIT 不同。

**JVM** 的基础值表示不是 NaN-boxing,而是**类型特殊的压缩**。Java 虽然是静态类型,但对象引用是 32 位或 64 位指针,基本类型(int/long/double)有专门的指令。JVM 不需要在一个统一的 TValue 里塞所有类型——它的字节码指令本身就带类型信息(`iload` 是 int、`dload` 是 double)。所以 JVM 没有 NaN-boxing 这种 trick 的需求。代价是 Java 不能像 Lua 那样动态改变变量类型(这本来就不是 Java 的目标)。

**V8(JavaScript)** 倒是和 Lua 一样面对动态类型。V8 早期的表示也有类似 NaN-boxing 的设计(叫 NaN-boxing 或 pointer tagging),但后来演进到**隐藏类(hidden class)+ 字段盒(boxing)**的组合:每个对象有个隐藏类描述它的字段布局,值按隐藏类预测的类型存储。这比 LuaJIT 的 NaN-boxing 复杂得多,因为 JS 的对象模型比 Lua 富裕(原型链、属性描述符等)。

共同点:**所有高性能动态语言运行时,都在值表示上做了大量优化**。LuaJIT 选 NaN-boxing 是因为 Lua 的类型相对简单(就那么十几种),一个 64 位字装得下;V8 选隐藏类是因为 JS 对象复杂,简单的 NaN-boxing 不够。

差异点在于**为 JIT 服务的程度**。LuaJIT 的每一个表示决策(紧凑 TValue、统一 GC 头、字段偏移、内联宏)都明确指向"让 JIT 生成的机器码更短更快"。JVM/V8 因为有更复杂的优化 pipeline(方法级编译、多层 JIT、profile-guided),数据表示只是其中一个环节;LuaJIT 因为追求极简,把数据表示的优化做到了极致。

| 维度 | LuaJIT(trace JIT) | JVM(method JIT) | V8(method JIT) |
|---|---|---|---|
| 值表示 | NaN-boxing(8B) | 类型化指令,无统一 TValue | NaN-boxing + 隐藏类 |
| 动态类型 | tag 藏在 NaN 位 | 字节码带类型 | 隐藏类预测字段类型 |
| guard 开销 | 极低(单条移位+cmp) | 较低(类型 profile) | 较低(隐藏类检查) |
| 对象统一头 | GCHeader(6B) | 对象头(mark word+klass) | 对象头(map+properties) |

### 8.3 NaN-boxing 不是 LuaJIT 发明

顺带说清一个事实:**NaN-boxing 不是 LuaJIT 发明的技术**。它最早见于 SpiderMonkey(Mozilla 的 JS 引擎)和 V8 早期,用来表示 JS 的动态值。LuaJIT 借鉴了这个技术,并按 Lua 的类型集合做了调整(用 `~n` 排序、设计 LJ_GC64 的 47 位指针布局)。

所以 NaN-boxing 是**动态语言运行时圈子里成熟的、被多个项目验证过的技术**。LuaJIT 的贡献不在于发明它,而在于**把它和一个极简的 trace JIT 配合得极其紧密**——从值的位编码到 GCobj 统一头到 Table 字段偏移,层层为机器码效率服务。这是 LuaJIT 整体设计哲学的体现:**每一层都为上一层铺路,数据表示为解释器和 JIT 铺路**。

---

## §9 回扣主线

这一章我们没讲 trace、没讲 guard、没讲 IR,讲的全是看起来很底层的东西:一个值在内存里怎么摆、一张表有哪些字段、一个状态怎么组织。但这些"底层的东西",是主线"把动态执行安全变成机器码"里**"快"字的根基**。

回看主线三股张力:

- **快**:NaN-boxing 把值压到 8 字节,缓存友好;类型 tag 在固定位,guard 便宜;字段布局为机器码统一;内联无开销。这些是"快"在数据层的兑现。
- **安全**:统一 GC 头让 GC 能不漏对象地遍历;值的类型 tag 永远不会和 double 混淆(IEEE 754 保证);GC64 的 47 位指针是显式声明的平台假设,不是隐式 bug。这些是"安全"在数据层的兑现。
- **省**:紧凑表示省内存(8B vs 16B,直接省一半);GG_State 合体分配省一次 malloc;Table 的 colo 同置省数组部分的独立分配。这些是"省"在数据层的兑现。

更关键的是,**这些数据表示的决策,是为后面 P2-P5 的 JIT 流水线服务的**:

- P2 trace 录制时,每读一个值都要判类型——得益于 NaN-boxing,这个判断极快。
- P3 优化 pass 处理 IR 时,IR 里也用类似的类型标记系统(见 P2-07),思想相通。
- P4 后端生成机器码时,Table 字段偏移、TValue 大小这些事实直接决定生成的指令。
- P5 guard 和 snapshot 恢复时,要把机器码寄存器状态翻译回解释器的 TValue 栈——栈的布局(base/top/FR2 帧)是恢复的依据。

所以这章不是孤立的"数据结构介绍",而是**为整本书后面的 JIT 实现铺地基**。理解了值的位编码、Table 的字段、State 的栈布局,后面看 trace 录制、IR 生成、机器码输出时,才不会在"这个值怎么来的""那个字段在哪"上卡壳。

数据表示是 JIT 的地基。地基打得越牢、越紧凑,上面盖的 JIT 楼就越快越稳。LuaJIT 把这个地基做到了极致——这就是这章要讲清的全部。

---

*下一章 [P2-05 trace 的生命周期](P2-05-trace的生命周期.md):数据表示讲完了,我们从解释器侧正式跨入 JIT 侧。看一条 trace 从被发现、被录制、被优化、被编译成机器码,到被安装、被运行、被退出、被链接的完整一生。这是全书核心的开始。*
