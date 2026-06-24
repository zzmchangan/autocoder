# P1-04 Table(上):数组与哈希的合体

> **本书主线**:统一与精简换小而快。**二分法位置**:数据根基(P1)。**本章主线落点**:统一的皇冠——一个 `Table` 结构里数组部分和哈希部分并存,兼任数组/字典/对象/命名空间。这是"小"(一套结构代码)和"快"(整数键走数组 O(1))同时拿下的核心证据。**★对照**:CPython 的 `list`(纯指针数组)与 `dict`(纯哈希表)分立成两个类型。**源码**:lua-5.5.0,`lobject.h` / `ltable.h` / `ltable.c`。**基调**:纯直球,不用比喻。

---

## 一、这章解决什么问题

用 Lua 写脚本,几乎每一行都离不开 `{}`。一句 `local t = {}` 之后,既可以 `t[1] = "a"; t[2] = "b"` 把它当数组用,也可以 `t["name"] = "lua"; t["version"] = "5.5"` 把它当字典用,还可以两种用法混在一个表里:

```lua
local t = {}
t[1] = "a"           -- 当数组
t[2] = "b"
t["name"] = "lua"    -- 当字典
t[3] = "c"           -- 又回到数组
```

这件事在很多语言里是要付出"分立"代价的。CPython 里,连续整数下标的数据放进 `list`(底层是一段指针数组),字符串键的数据放进 `dict`(底层是一张哈希表),二者是两套完全独立的 C 类型、两套创建/扩容/遍历/GC 代码。一个对象不能既是 `list` 又是 `dict`。

Lua 的答案是不分。源码里没有 `list` 这个类型,也没有 `dict`,只有一个 `Table`。上面那段 Lua 代码里,`t[1]`、`t[2]`、`t[3]` 落在 `Table` 的**数组部分**(`array`),`t["name"]` 落在 `Table` 的**哈希部分**(`node`),两部分同住一个结构、互不干扰。

这带来两个直接的好处,正好落在本书主线的两端:

- **小**:整个内核只有一套创建、遍历、扩容、GC 的代码服务所有复合数据。砍掉 `list`/`dict`/`tuple`/`set` 的分立,源码量直接少一大块。
- **快**:`t[i]` 命中数组部分时,访问代价和 C 数组下标一样,是 O(1) 的直接寻址,缓存友好。统一的代价(落进哈希)只发生在离散键上。

本章要回答的问题就是:这个"兼任数组和字典"的 `Table`,在 lua-5.5.0 的源码里到底长什么样?一个 `{}` 创建出来后,`t[1]` 和 `t["x"]` 各自走哪条路、为什么都快?本章只讲结构、创建和基础访问两件事;哈希怎么探测、什么时候 rehash,留到 [P1-05](P1-05-Table下-哈希探测与rehash.md)。

> **5.5 vs 老资料(5.3/5.4)差异提示**:本章涉及的大半个 `ltable.c` 在 5.5 里被重写(相对 5.4 增加约 360 行)。最显著的变化有三处:① 数组部分的物理布局从"一段连续 `TValue`"改成了"值数组 + 标签数组分离、且 `array` 指针落在两者中间"的倒排结构;② `luaH_new` 的签名从老的 `luaH_new(L, narray, nhash)` 简化成无参的 `luaH_new(L)`,新表一律从空开始;③ 新增了一套 `luaH_pset*` + `luaH_finishset` 的"预置"机制,把"能就地改的就直接改、改不了的才走慢路径"这件事结构化了。下面凡与老资料冲突处,均以 5.5 源码为准并显式标注。

---

## 二、源码怎么实现

### 2.1 Table 结构(lobject.h:776)

`Table` 定义在 `lobject.h:776`,逐字段如下:

```c
typedef struct Table {
  CommonHeader;
  lu_byte flags;  /* 1<<p means tagmethod(p) is not present */
  lu_byte lsizenode;  /* log2 of number of slots of 'node' array */
  unsigned int asize;  /* number of slots in 'array' array */
  Value *array;  /* array part */
  Node *node;
  struct Table *metatable;
  GCObject *gclist;
} Table;
```

(源码 `lobject.h:776`–`785`。)

逐字段讲:

- **`CommonHeader`**:这是所有 GC 对象共有的头(`lu_byte tt` + `lu_byte marked`,见 P5-16),让 GC 能用统一代码遍历所有可回收对象。`Table` 是 GC 对象,所以带这个头。
- **`flags`**:一个字节,同时干两件事。低 6 位(位 0..5)是**元方法缓存位图**——`1<<p` 表示"这个表(或它的元表)里不存在第 `p` 号元方法",用来加速元方法查找(见 2.7)。位 6(`BITDUMMY`)表示"哈希部分用的是全局共享的 `dummynode`"(见 2.4)。注释里还提到"位 7 is used for 'isrealasize'",但 5.5 实际源码里已经不再使用这个标志(`isrealasize` 已被移除),这是老注释残留,以源码为准。
- **`lsizenode`**:哈希部分槽数的 log2。哈希部分大小永远是 2 的幂,存 log2 而不是真值,既省空间(一个字节够表示到 2^128),又把"取模"变成"按位与"(见 2.6 的 `lmod`)。
- **`asize`**:数组部分的槽数。整数键 `1..asize` 走数组部分,直接下标。
- **`array`**:指向数组部分。注意 5.5 里它不是普通的 `TValue *`(见 2.3),它指向"值段和标签段之间"。
- **`node`**:指向哈希部分的 `Node` 数组。空表时指向全局共享的 `dummynode`。
- **`metatable`**:元表,本身也是一个 `Table`。挂元方法(详见 P6-19)。`NULL` 表示没有元表。
- **`gclist`**:GC 链表指针,让增量 GC 能把这个 `Table` 串进灰色/弱表等队列(P5-16/P5-17)。

一句话:`Table` 用 `array` + `node` 两个指针把两种存储捏在一个结构里,其余字段(flags/lsizenode/asize)是这两部分的元信息,metatable/gclist 是它"当对象"和"被 GC 管"的接口。

### 2.2 Node 与 TKey(lobject.h:751)

哈希部分的基本单元是 `Node`,定义在 `lobject.h:751`:

```c
typedef union Node {
  struct NodeKey {
    TValuefields;  /* fields for value */
    lu_byte key_tt;  /* key type */
    int next;  /* for chaining */
    Value key_val;  /* key value */
  } u;
  TValue i_val;  /* direct access to node's value as a proper 'TValue' */
} Node;
```

(源码 `lobject.h:751`–`759`。)

这里有个值得讲的布局技巧。`Node` 是个 `union`,有两个视图:

- **`u` 视图**:完整的"值 + 键 + next 指针"。其中 `TValuefields` 展开就是 `Value value_; lu_byte tt_`,是节点存放的那个值(注意是值在前);`key_tt`/`key_val` 是这个节点的键的类型和值(键被拆开存放);`next` 是冲突链表的下一个节点的偏移(开放寻址的链,详见 P1-05)。
- **`i_val` 视图**:直接把节点的"值"那一块当成一个标准的 `TValue` 来访问。

为什么要拆?源码注释(`lobject.h:744`–`750`)说得很直白:把键的字段(`key_tt` 和 `key_val`)打散、不组成一个完整的 `TValue`,是为了让 `Node` 在 4 字节对齐和 8 字节对齐两种机器上都更省空间。键的 tag 单独存成 `key_tt`(一个字节),不必像 `TValue` 那样为对齐补 padding。这是"小"的又一个细节。

`TValue` 本身是最朴素的 tagged value(`lobject.h:49`–`69`):

```c
typedef union Value {
  struct GCObject *gc;    /* collectable objects */
  void *p;         /* light userdata */
  lua_CFunction f; /* light C functions */
  lua_Integer i;   /* integer numbers */
  lua_Number n;    /* float numbers */
  lu_byte ub;
} Value;

#define TValuefields	Value value_; lu_byte tt_

typedef struct TValue {
  TValuefields;
} TValue;
```

(源码 `lobject.h:49`–`69`。)`Value` 是所有 Lua 值的载体(整数/浮点/GC 对象指针/light userdata/light C 函数共用一个 union),`tt_` 是类型 tag。一个 `TValue` = 一个值 + 一个类型。整个 VM 里所有的值——栈上的、upvalue 里的、表里的——底层都是这个 16 字节(64 位下)的结构。

围绕 `Node` 的键,Lua 定义了一组判断宏(`lobject.h:791`–`805`):

```c
#define keytt(node)		((node)->u.key_tt)
#define keyval(node)		((node)->u.key_val)

#define keyisnil(node)		(keytt(node) == LUA_TNIL)
#define keyisinteger(node)	(keytt(node) == LUA_VNUMINT)
#define keyival(node)		(keyval(node).i)
#define keyisshrstr(node)	(keytt(node) == ctb(LUA_VSHRSTR))
#define keystrval(node)		(gco2ts(keyval(node).gc))
```

这些宏是访问键的统一入口。注意几个细节:键的 tag 比较用的是 `LUA_TNIL`(不带 variant)还是 `LUA_VNUMINT`/`ctb(LUA_VSHRSTR)`(带 variant),分情况——nil 不区分 variant,数字和字符串要区分。`keyisshrstr` 判断"短串键",它和 `keystrval` 配合,让短串键的比较退化成指针比较(见 2.6)。还有一组"死键"宏(`setdeadkey`/`keyisdead`,`lobject.h:814`–`815`),是表在遍历中被删键时为了不破坏 `next` 语义用的,留到 P1-05/P5-17 讲。

### 2.3 数组部分的物理布局(关键 5.5 改动)

这是 5.5 相对老资料最大的变化。老资料(讲 5.3/5.4 的书/博客)里,数组部分就是一段连续的 `TValue`,`t->array[k]` 直接取值。**5.5 不是这样。**

看 `ltable.h:95`–`134` 的一整段注释和宏:

```c
/*
** The array part of a table is represented by an inverted array of
** values followed by an array of tags, to avoid wasting space with
** padding. In between them there is an unsigned int, explained later.
** The 'array' pointer points between the two arrays, so that values are
** indexed with negative indices and tags with non-negative indices.

             Values                              Tags
  --------------------------------------------------------
  ...  |   Value 1     |   Value 0     |unsigned|0|1|...
  --------------------------------------------------------
                                       ^ t->array
*/

/* Computes the address of the tag for the abstract C-index 'k' */
#define getArrTag(t,k)	(cast(lu_byte*, (t)->array) + sizeof(unsigned) + (k))

/* Computes the address of the value for the abstract C-index 'k' */
#define getArrVal(t,k)	((t)->array - 1 - (k))
```

(源码 `ltable.h:95`–`116`。)

5.5 把数组部分拆成了**两段分离的数组**:一段存所有的 `Value`(值),一段存所有的 `lu_byte` tag(类型),中间夹一个 `unsigned int`(是 `#t` 的长度提示 `lenhint`,见 2.8)。`t->array` 指针指向这个 `unsigned` 的位置,也就是两段的交界:

- **值**用**负向**下标访问:`getArrVal(t,k) = t->array - 1 - k`,即 `Value 0` 在 `array-1`,`Value 1` 在 `array-2`,往低地址走。
- **标签**用**非负**下标访问:`getArrTag(t,k) = (lu_byte*)array + sizeof(unsigned) + k`,往高地址走。

为什么要把值和标签分开存?注释一句话点破:**"to avoid wasting space with padding"**。如果还按老办法存一段 `TValue`(值 8 字节 + tag 1 字节 + 7 字节 padding 对齐到 16 字节),每个槽要浪费 7 字节。拆开后,值段是紧凑的 `Value` 数组(每项 8 字节,天然对齐),标签段是紧凑的 `lu_byte` 数组(每项 1 字节),padding 浪费被消灭。对一个存 100 万个小整数的数组,这能省下约 7 MB。这是"小"在数据布局层面的兑现。

读写值的宏(`ltable.h:130`–`146`):

```c
#define arr2obj(h,k,val)  \
  ((val)->tt_ = *getArrTag(h,(k)), (val)->value_ = *getArrVal(h,(k)))

#define obj2arr(h,k,val)  \
  (*getArrTag(h,(k)) = (val)->tt_, *getArrVal(h,(k)) = (val)->value_)

#define farr2val(h,k,tag,res)  \
  ((res)->tt_ = tag, (res)->value_ = *getArrVal(h,(k)))

#define fval2arr(h,k,tag,val)  \
  (*tag = (val)->tt_, *getArrVal(h,(k)) = (val)->value_)
```

`arr2obj`/`obj2arr` 是完整的"标签 + 值"一起搬;`farr2val`/`fval2arr`(`f` = fast)是已经预先拿到了 tag 或 tag 地址,只搬另一半——这是给 VM 的快速路径用的(见 2.6)。

数组部分还有一个容量上的细节:`concretesize`(`ltable.c:544`)算出 asize 个槽实际要分配多少字节,公式是 `size * (sizeof(Value) + 1) + sizeof(unsigned)`,正好对应"值段 + 标签段 + 中间那个 unsigned"。

### 2.4 创建:luaH_new 与 dummynode

`Table` 的创建入口是 `luaH_new`(`ltable.c:799`):

```c
Table *luaH_new (lua_State *L) {
  GCObject *o = luaC_newobj(L, LUA_VTABLE, sizeof(Table));
  Table *t = gco2t(o);
  t->metatable = NULL;
  t->flags = maskflags;  /* table has no metamethod fields */
  t->array = NULL;
  t->asize = 0;
  setnodevector(L, t, 0);
  return t;
}
```

(源码 `ltable.c:799`–`808`。)

几个点:

- `luaC_newobj` 先从 GC 那边分配一个 `sizeof(Table)` 的对象并打上 `LUA_VTABLE` 标记,把它挂进 GC 的对象链表(P5-16)。
- **`flags = maskflags`**:`maskflags`(`ltm.h:54`)是 `cast_byte(~(~0u << (TM_EQ + 1)))`,即低 6 位全 1。意思是"这个表刚创建,假定它没有任何快速元方法"(每个快速元方法位都是 1 = 不存在)。这是缓存位的初始状态,见 2.7。
- `array = NULL`,`asize = 0`:空表没有数组部分。
- `setnodevector(L, t, 0)`:传 size=0,走 `setnodevector` 的空表分支(`ltable.c:602`–`607`):

```c
static void setnodevector (lua_State *L, Table *t, unsigned size) {
  if (size == 0) {  /* no elements to hash part? */
    t->node = cast(Node *, dummynode);  /* use common 'dummynode' */
    t->lsizenode = 0;
    setdummy(t);  /* signal that it is using dummy node */
  }
  else {
    ...
  }
}
```

空表的哈希部分不分配任何 `Node`,而是让 `t->node` 指向一个全局共享的 `dummynode`,并把 `flags` 的 `BITDUMMY` 位置 1。`dummynode` 定义在 `ltable.c:130`:

```c
static const Node dummynode_ = {
  {{NULL}, LUA_VEMPTY,  /* value's value and type */
   LUA_TDEADKEY, 0, {NULL}}  /* key type, next, and key value */
};
```

(源码 `ltable.c:122`–`133`。注意 `#define dummynode (&dummynode_)` 在 `ltable.c:122`。)

这个 `dummynode` 的设计意图,源码注释(`ltable.c:124`–`129`)讲得很清楚:

> Common hash part for tables with empty hash parts. That allows all tables to have a hash part, avoiding an extra check ("is there a hash part?") when indexing. Its sole node has an empty value and a key (DEADKEY, NULL) that is different from any valid TValue.

也就是说:空表**不存 NULL 指针**,而是指向一个"看起来像哈希部分、但永远是空"的共享节点。好处是访问表时不必每次都判断"这个表有没有哈希部分"——所有表都有哈希部分,空表的哈希部分就是那个永远查不到东西的 `dummynode`。这把一个条件分支从热路径上拿掉了。`isdummy`/`setdummy`/`setnodummy` 三个宏(`ltable.h:31`–`36`)管理 `BITDUMMY` 这一位,`allocsizenode(t)`(`ltable.h:41`)在 dummy 时返回 0(不需要为哈希部分分配内存),非 dummy 时返回真实槽数。

> **5.5 vs 老资料差异**:老资料(5.3/5.4)里 `luaH_new` 的签名是 `Table *luaH_new (lua_State *L, int narray, int nhash)`,创建时可以一次指定数组部分和哈希部分的初始大小,常配合 `luaH_resize` 使用。**5.5 把这个签名简化成 `luaH_new(L)`**,新表一律从空(asize=0 + dummynode)开始,需要预分配时另外调 `luaH_resize`。`luaL_newstate`/构造器等调用点相应调整。Grep 5.5 全仓,`luaH_new` 只有无参这一个定义。

### 2.5 访问 API:四个分入口

读一个表里的值,Lua 提供了一组分入口,而不是一个万能函数。这一组入口在 `ltable.h:149`–`155` 声明:

```c
LUAI_FUNC lu_byte luaH_get (Table *t, const TValue *key, TValue *res);
LUAI_FUNC lu_byte luaH_getshortstr (Table *t, TString *key, TValue *res);
LUAI_FUNC lu_byte luaH_getstr (Table *t, TString *key, TValue *res);
LUAI_FUNC lu_byte luaH_getint (Table *t, lua_Integer key, TValue *res);

/* Special get for metamethods */
LUAI_FUNC const TValue *luaH_Hgetshortstr (Table *t, TString *key);
```

为什么分这么多入口?因为 VM 里读表有好几条不同的字节码指令,它们的 key 类型在编译期就定了:`GETI` 的 key 一定是整数(操作数直接编码在指令里),`GETFIELD`/`GETTABUP` 的 key 一定是短串(编译期常量池里的 `TString`),只有 `GETTABLE` 的 key 是运行时的任意值。为每种 key 提供专用入口,就能跳过类型分派、走最快的路径。下面逐个看。

**整数键:`luaH_getint`**(`ltable.c:959`):

```c
lu_byte luaH_getint (Table *t, lua_Integer key, TValue *res) {
  unsigned k = ikeyinarray(t, key);
  if (k > 0) {
    lu_byte tag = *getArrTag(t, k - 1);
    if (!tagisempty(tag))
      farr2val(t, k - 1, tag, res);
    return tag;
  }
  else
    return finishnodeget(getintfromhash(t, key), res);
}
```

(源码 `ltable.c:959`–`969`。)

逻辑就两步:

1. `ikeyinarray(t, key)`(`ltable.c:326`,展开是 `checkrange(key, t->asize)`)判断这个整数键 `1 <= key <= asize` 是否落在数组部分。是,就走 `getArrTag`/`farr2val` 直接下标,读数组部分的第 `k-1` 个槽(抽象下标,内部由 2.3 的倒排宏翻译成真实地址)。
2. 否则(`key > asize` 或 `key <= 0`),走 `getintfromhash`(`ltable.c:929`),到哈希部分按 `hashint` 算出的主位置开始沿 `next` 链找。

注意返回值是个 `lu_byte` tag,不是 `TValue*`。这是 5.5 的一个统一约定:**所有 `luaH_get*` 都返回 tag、把值写进出参 `res`**。这样调用方能用 `tagisempty(tag)` 一个判断就知道"找到没有",而值已经在 `res` 里了。这种"返回 tag + 出参写值"的风格,是 5.5 配合 VM 快速路径的重设计,老资料里 `luaH_get` 返回 `const TValue*` 的写法已经过时。

`getintfromhash` 的查找(`ltable.c:929`–`942`):

```c
static const TValue *getintfromhash (Table *t, lua_Integer key) {
  Node *n = hashint(t, key);
  lua_assert(!ikeyinarray(t, key));
  for (;;) {  /* check whether 'key' is somewhere in the chain */
    if (keyisinteger(n) && keyival(n) == key)
      return gval(n);  /* that's it */
    else {
      int nx = gnext(n);
      if (nx == 0) break;
      n += nx;
    }
  }
  return &absentkey;
}
```

这是开放寻址 + 链表的经典走法:从主位置 `hashint(t,key)` 出发,沿着 `next` 偏移一个一个比,`next==0` 表示链尾。整数键的相等判断是 `keyisinteger(n) && keyival(n) == key`,直接比整数值。找不到返回全局的 `absentkey`(`ltable.c:136`,`{NULL}, LUA_VABSTKEY`)。哈希函数 `hashint` 和探测的细节(为什么用 `%`、Brent 变体怎么处理冲突)是 P1-05 的重头戏,这里只看结构。

**短串键:`luaH_Hgetshortstr` / `luaH_getshortstr`**(`ltable.c:975`–`993`):

```c
const TValue *luaH_Hgetshortstr (Table *t, TString *key) {
  Node *n = hashstr(t, key);
  lua_assert(strisshr(key));
  for (;;) {  /* check whether 'key' is somewhere in the chain */
    if (keyisshrstr(n) && eqshrstr(keystrval(n), key))
      return gval(n);  /* that's it */
    else {
      int nx = gnext(n);
      if (nx == 0)
        return &absentkey;  /* not found */
      n += nx;
    }
  }
}

lu_byte luaH_getshortstr (Table *t, TString *key, TValue *res) {
  return finishnodeget(luaH_Hgetshortstr(t, key), res);
}
```

(源码 `ltable.c:975`–`993`。)

关键在 `eqshrstr(keystrval(n), key)` 这一行。短串在 Lua 里是**驻留**(intern)的——内容相同的短串全局只有一份(P1-03 详讲),所以两个短串相等当且仅当指针相等。`eqshrstr` 实际上就是指针比较(源码注释 `ltable.c:237`–`238` 明说"It is assumed that 'eqshrstr' is simply pointer equality")。这让短串键的查找不必逐字节比字符串,一次指针比较搞定,和整数键一样快。

注意还有一个 `luaH_Hgetshortstr`(大写 H 开头),它返回 `const TValue*` 而不是 tag——注释说是 "Special get for metamethods"。这是给 GC/元方法查找用的内部入口,因为它需要拿到槽位指针(而不仅是值)。

**长串键与通用入口:`luaH_getstr` / `luaH_get`**(`ltable.c:1004`–`1042`):

```c
static const TValue *Hgetstr (Table *t, TString *key) {
  if (strisshr(key))
    return luaH_Hgetshortstr(t, key);
  else
    return Hgetlongstr(t, key);   /* 走 getgeneric */
}

lu_byte luaH_getstr (Table *t, TString *key, TValue *res) {
  return finishnodeget(Hgetstr(t, key), res);
}

lu_byte luaH_get (Table *t, const TValue *key, TValue *res) {
  const TValue *slot;
  switch (ttypetag(key)) {
    case LUA_VSHRSTR:
      slot = luaH_Hgetshortstr(t, tsvalue(key));
      break;
    case LUA_VNUMINT:
      return luaH_getint(t, ivalue(key), res);
    case LUA_VNIL:
      slot = &absentkey;
      break;
    case LUA_VNUMFLT: {
      lua_Integer k;
      if (luaV_flttointeger(fltvalue(key), &k, F2Ieq)) /* integral index? */
        return luaH_getint(t, k, res);  /* use specialized version */
      /* else... */
    }  /* FALLTHROUGH */
    default:
      slot = getgeneric(t, key, 0);
      break;
  }
  return finishnodeget(slot, res);
}
```

(源码 `ltable.c:1004`–`1042`。)

`luaH_get` 是万能入口,内部按 key 的 tag 分派:短串走 `Hgetshortstr`,整数走 `getint`,nil 直接判 absent,浮点数若是整数值(`1.0`)归一化成整数走 `getint`(否则进 `getgeneric`),其余类型(长串、布尔、light userdata、闭包等)走通用的 `getgeneric`(`ltable.c:291`)。`getgeneric` 是最慢的一条路:它要先 `mainpositionTV` 算主位置,再沿链比键,键相等判断 `equalkey`(`ltable.c:252`)要按 tag 分多种情况逐个比(浮点要 `luai_numeq`,长串要 `luaS_eqstr`,对象要指针比)。所以分入口的意义就是把最常见的几种 key(整数、短串)从这条慢路上摘出来,直奔专用路径。

### 2.6 VM 怎么调这些入口:GETI / GETFIELD / GETTABLE

光看 `luaH_get*` 还看不出为什么分这么多入口。要看 VM 的字节码处理,才明白这些分入口是专门为 VM 的快速路径设计的。

读表相关的字节码有四条:`GETI`(整数下标,`R[A] = R[B][C]`,C 是指令里的立即数)、`GETFIELD`(短串下标,`R[A] = R[B][K[C]]`,K[C] 是常量池里的短串)、`GETTABLE`(任意下标,`R[A] = R[B][R[C]]`)、`GETTABUP`(从 upvalue 取表再短串下标)。它们在 `lvm.c` 里的处理(`lvm.c:1300`–`1348`):

```c
vmcase(OP_GETTABUP) {
  StkId ra = RA(i);
  TValue *upval = cl->upvals[GETARG_B(i)]->v.p;
  TValue *rc = KC(i);
  TString *key = tsvalue(rc);  /* key must be a short string */
  lu_byte tag;
  luaV_fastget(upval, key, s2v(ra), luaH_getshortstr, tag);
  if (tagisempty(tag))
    Protect(luaV_finishget(L, upval, rc, ra, tag));
  vmbreak;
}
vmcase(OP_GETTABLE) {
  StkId ra = RA(i);
  TValue *rb = vRB(i);
  TValue *rc = vRC(i);
  lu_byte tag;
  if (ttisinteger(rc)) {  /* fast track for integers? */
    luaV_fastgeti(rb, ivalue(rc), s2v(ra), tag);
  }
  else
    luaV_fastget(rb, rc, s2v(ra), luaH_get, tag);
  if (tagisempty(tag))
    Protect(luaV_finishget(L, rb, rc, ra, tag));
  vmbreak;
}
vmcase(OP_GETI) {
  StkId ra = RA(i);
  StkId rb = vRB(i);
  int c = GETARG_C(i);
  lu_byte tag;
  luaV_fastgeti(rb, c, s2v(ra), tag);
  if (tagisempty(tag)) {
    TValue key;
    setivalue(&key, c);
    Protect(luaV_finishget(L, rb, &key, ra, tag));
  }
  vmbreak;
}
vmcase(OP_GETFIELD) {
  StkId ra = RA(i);
  StkId rb = vRB(i);
  TValue *rc = KC(i);
  TString *key = tsvalue(rc);  /* key must be a short string */
  lu_byte tag;
  luaV_fastget(rb, key, s2v(ra), luaH_getshortstr, tag);
  if (tagisempty(tag))
    Protect(luaV_finishget(L, rb, rc, ra, tag));
  vmbreak;
}
```

(源码 `lvm.c:1300`–`1348`。)

看清楚这里的两层快速路径:

1. **`luaV_fastgeti` / `luaV_fastget` / `luaV_fastseti` / `luaV_fastset`**(`lvm.h:81`–`99`)是 VM 专用的内联宏,它们把"被访问的是不是 Table""是不是命中数组部分"这些判断直接展开进字节码处理的代码里,不走函数调用。比如 `luaH_fastgeti`(`ltable.h:49`–`54`):

```c
#define luaH_fastgeti(t,k,res,tag) \
  { Table *h = t; lua_Unsigned u = l_castS2U(k) - 1u; \
    if ((u < h->asize)) { \
      tag = *getArrTag(h, u); \
      if (!tagisempty(tag)) { farr2val(h, u, tag, res); }} \
    else { tag = luaH_getint(h, (k), res); }}
```

它先做一次 `u = k-1; u < asize` 的无符号比较——这一步同时处理了 `k >= 1` 和 `k <= asize` 两件事(因为 `k-1` 转无符号后,`k <= 0` 会变成超大正数,自然 `>= asize`)。命中就直接读数组部分的 tag,非空就用 `farr2val` 把值搬进 `res`;不命中才退化到 `luaH_getint`。`luaV_fastgeti` 外层(`lvm.h:89`)还先判 `ttistable(t)`,不是表就直接返回 `LUA_VNOTABLE`。

2. 只有当快速路径返回的 tag 是"空"(`tagisempty`,即任意 nil 变体)时,才 `Protect(luaV_finishget(...))` 走慢路径。慢路径里要处理元方法(`__index`)、非表类型的索引(字符串索引、带 `__index` 的 userdata 等)、key 归一化(把 `1.0` 当 `1`)这些边角情况(P3-11/P6-19 详讲)。

这就是为什么 `luaH_get*` 要分那么多入口:`GETI` 直接喂 `luaH_fastgeti`(数组优先)、`GETFIELD` 直接喂 `luaH_getshortstr`(短串指针比)、`GETTABLE` 在 key 是整数时也走 `fastgeti`、否则才走通用的 `luaH_get`。每条字节码都配一条最短的查找路径,中间的类型分派和函数调用开销能省则省。这是"快"在 VM 和表访问接口之间的一层精心对齐。

### 2.7 写入 API:pset + finishset + luaH_newkey

写入比读取复杂,因为写入可能触发 rehash(表满了要扩容),rehash 是可能分配内存、可能抛错的操作,不能放在 VM 的快速路径里(VM 快速路径用 `vmcase` 展开,不能 `Protect` 之外分配)。5.5 把写入拆成了**"预置(pset)+ 完成(finishset)"**两段,正是为了把这个限制结构化。

`ltable.h` 里写入相关的声明(`ltable.h:157`–`168`):

```c
LUAI_FUNC int luaH_psetint (Table *t, lua_Integer key, TValue *val);
LUAI_FUNC int luaH_psetshortstr (Table *t, TString *key, TValue *val);
LUAI_FUNC int luaH_psetstr (Table *t, TString *key, TValue *val);
LUAI_FUNC int luaH_pset (Table *t, const TValue *key, TValue *val);

LUAI_FUNC void luaH_setint (lua_State *L, Table *t, lua_Integer key,
                                                    TValue *value);
LUAI_FUNC void luaH_set (lua_State *L, Table *t, const TValue *key,
                                                 TValue *value);

LUAI_FUNC void luaH_finishset (lua_State *L, Table *t, const TValue *key,
                                              TValue *value, int hres);
```

两套:`luaH_set*` 是完整的一次性写入(内部就是 pset + 必要时 finishset),给 C API 和不需要快速路径的地方用;`luaH_pset*` 是给 VM 快速路径用的"预置"。

`pset` 的返回值是个 `int` 编码(`ltable.h:67`–`92` 有一大段注释讲它),三种情况:

- **`HOK`**(0):成功就地写入了(key 已存在,只是改值)。
- **`HNOTFOUND`**(1):key 不在表里,需要新建。
- **一个编码了位置的正数或负数**:key 对应的槽存在但当前是空(典型情况是哈希链上的主位置被占了),需要去那个位置写入。正数 `HFIRSTNODE + hash_index` 表示在哈希部分,负数 `~array_index` 表示在数组部分。

看 `luaH_psetshortstr`(`ltable.c:1098`–`1121`)体会这套设计:

```c
int luaH_psetshortstr (Table *t, TString *key, TValue *val) {
  const TValue *slot = luaH_Hgetshortstr(t, key);
  if (!ttisnil(slot)) {  /* key already has a value? (all too common) */
    setobj(((lua_State*)NULL), cast(TValue*, slot), val);  /* update it */
    return HOK;  /* done */
  }
  else if (checknoTM(t->metatable, TM_NEWINDEX)) {  /* no metamethod? */
    if (ttisnil(val))  /* new value is nil? */
      return HOK;  /* done (value is already nil/absent) */
    if (isabstkey(slot) &&  /* key is absent? */
       !(isblack(t) && iswhite(key))) {  /* and don't need barrier? */
      TValue tk;  /* key as a TValue */
      setsvalue(cast(lua_State *, NULL), &tk, key);
      if (insertkey(t, &tk, val)) {  /* insert key, if there is space */
        invalidateTMcache(t);
        return HOK;
      }
    }
  }
  /* Else, either table has new-index metamethod, or it needs barrier,
     or it needs to rehash for the new key. In any of these cases, the
     operation cannot be completed here. Return a code for the caller. */
  return retpsetcode(t, slot);
}
```

注释点破了这个函数优化的正是构造器最常见的场景:`{x=1, y=2}`。这种场景下表没有元表(`checknoTM` 为真,见 2.8)、key 不存在、有空位、不需要 GC barrier——`insertkey` 一次性把新键塞进哈希,返回 `HOK`。任何一步不满足(有 `__newindex` 元方法、需要 barrier、需要 rehash、value 是 nil),就 `retpsetcode` 返回位置编码,交给调用方的 `luaH_finishset` 处理。

`luaH_finishset`(`ltable.c:1154`–`1188`)根据返回码分派:`HNOTFOUND` 时校验 key 合法性(nil/NaN 报错,浮点归一化,外部串内部化),然后调 `luaH_newkey`;正数编码去哈希部分对应槽写;负数编码去数组部分对应槽写。`luaH_newkey`(`ltable.c:914`–`926`)是真正"插入一个全新 key"的入口:

```c
static void luaH_newkey (lua_State *L, Table *t, const TValue *key,
                                                 TValue *value) {
  if (!ttisnil(value)) {  /* do not insert nil values */
    int done = insertkey(t, key, value);
    if (!done) {  /* could not find a free place? */
      rehash(L, t, key);  /* grow table */
      newcheckedkey(t, key, value);  /* insert key in grown table */
    }
    luaC_barrierback(L, obj2gco(t), key);
    condchangemem(L, (void)0, (void)0, 1);
  }
}
```

`insertkey` 找不到空位就 `rehash`(扩容 + 重新切分数组/哈希,P1-05 详讲),扩完再 `newcheckedkey` 插入。注意两点:一是 **nil 值根本不插入**——Lua 里 `t[k] = nil` 的语义是"删除 k"(如果 k 存在的话),不是"插入 nil",所以 `newkey` 直接对 nil value 啥也不做;二是 `luaC_barrierback` 是 GC 写屏障(P5-16),通知 GC"这个表引用了一个可能新生的对象",保证增量三色标记的正确性。

`luaH_set`(`ltable.c:1195`–`1199`)和 `luaH_setint`(`ltable.c:1206`–`1218`)是给 C API 用的完整入口,逻辑和上面 pset+finishset 一致,只是合在一个函数里、不区分快速路径。`luaH_setint` 对数组部分的 key 直接 `obj2arr` 写,对哈希部分的 key 先 `rawfinishnodeset` 试改既有槽,不行再 `luaH_newkey`。

VM 的 SETI/SETFIELD/SETTABLE/SETTABUP 用的就是 `luaH_fastseti`/`luaH_fastset` + `luaH_pset*` 这套快速路径(`lvm.c:1349`–`1393`),返回 `HOK` 就 `luaV_finishfastset`(只补一个 GC barrier),否则 `Protect(luaH_finishset(...))` 走慢路径。和读取完全对称。

### 2.8 flags:元方法缓存位图

回头看 `flags` 字段的低 6 位。它是"该元方法不存在"的缓存。机制在 `ltm.h`:

```c
#define maskflags	cast_byte(~(~0u << (TM_EQ + 1)))
#define notm(tm)	ttisnil(tm)
#define checknoTM(mt,e)	((mt) == NULL || (mt)->flags & (1u<<(e)))
#define gfasttm(g,mt,e)  \
  (checknoTM(mt, e) ? NULL : luaT_gettm(mt, e, (g)->tmname[e]))
#define fasttm(l,mt,e)	gfasttm(G(l), mt, e)
```

(源码 `ltm.h:54`–`68`。)

`TM_EQ` 是 `TMS` 枚举(`ltm.h:18`–`45`)里最后一个"快速元方法":

```c
typedef enum {
  TM_INDEX,
  TM_NEWINDEX,
  TM_GC,
  TM_MODE,
  TM_LEN,
  TM_EQ,  /* last tag method with fast access */
  TM_ADD, ... TM_CLOSE,
  TM_N
} TMS;
```

也就是说 `TM_INDEX`(0)、`TM_NEWINDEX`(1)、`TM_GC`(2)、`TM_MODE`(3)、`TM_LEN`(4)、`TM_EQ`(5)这 6 个元方法是走"快速访问"路径,它们的"是否存在"被缓存在 `flags` 的低 6 位里。后面的算术/比较/concat/call 等(`TM_ADD`..`TM_CLOSE`)不走快速缓存。

`maskflags`(`~(~0u << 6)` = `0x3F`)正好是低 6 位全 1。新表 `flags = maskflags` 表示"假定这 6 个元方法都不存在"(每个位都是 1 = 不存在)。

机制是这样:

- 要查一个表 `t` 有没有 `__index` 元方法,不直接去翻 `t->metatable`,而是先 `checknoTM(t->metatable, TM_INDEX)`。如果 `t->metatable == NULL`(没元表)或者 `flags` 的第 0 位是 1(缓存说"没有"),直接判定"没有",返回 `NULL`,完全不进哈希查找。
- 只有缓存位是 0("可能有")时,才 `luaT_gettm` 去元表里按短串键 `__index` 真正查一次。查到了就用;查不到(元表里确实没这个字段),就把 `flags` 的对应位置 1("记住,没有了"),下次就不用再查了。

为什么缓存"不存在"而不是"存在"?因为绝大多数表压根没有元表、或者元表里只有少数几个元方法。缓存"不存在"能让 99% 的访问在 `checknoTM` 这一步就短路掉,根本不进元表查找。这是把"表通常没有元方法"这个经验事实压进了一个字节的状态里。

`invalidateTMcache(t)`(`ltable.h:23`,`(t)->flags &= cast_byte(~maskflags)`)把低 6 位全清 0,表示"缓存作废,可能有什么元方法"。凡是对元表做了可能改变元方法集合的操作(给表设新元表、往可能是元方法的字段写值),都要调一次 `invalidateTMcache` 把缓存作废。前面 `luaH_psetshortstr` 里 `insertkey` 成功后就调了它——新插入一个短串键,万一这个键就是 `__index` 呢?作废缓存,下次重新查。

注意位 6 是 `BITDUMMY`(哈希部分是不是 dummynode,见 2.4),它和元方法缓存共用 `flags` 这个字节,但语义无关,靠 `maskflags` 只覆盖低 6 位来隔离。位 7 老资料里说是 `isrealasize`,5.5 已不用。

### 2.9 #t:边界与长度提示

数组和哈希合体后,有个绕不开的问题:`#t` 取的是什么?Lua 的定义是"一个边界 n,使得 `t[n]` 非 nil 而 `t[n+1]` 为 nil"。当表有空洞(`t[1] t[2] t[4]` 有值、`t[3]` 为 nil)时,边界不唯一(`#t` 可能返回 2 或 4)。

5.5 在数组部分那个 `unsigned` 中间槽(`lenhint`,见 2.3)存的就是"上次找到的边界的提示"。`luaH_getn`(`ltable.c:1301`–`1343`)的逻辑:先读 `lenhint`,在它附近 4 格内(`maxvicinity = 4`)找边界;找不到再退化为二分搜索(`binsearch`)。数组部分最后一个元素非空时,边界可能延伸到哈希部分,再走 `hash_search` 二分。每次找到一个边界都 `newhint` 更新提示,让下一次 `#t`(以及 `for i=1,#t` 这种循环)大概率在常数步内命中。

这个 `lenhint` 的位置选得巧:它塞在值段和标签段之间那个本来就要为对齐留的 `unsigned` 空位里(见 2.3 的 `concretesize`),不占 `Table` 结构体本身的字段——注释(`ltable.h:119`–`123`)说"It is stored there to avoid wasting space in the structure Table for tables with no array part."没有数组部分的表(纯字典)就不分配这个 unsigned,也就不存 hint,零开销。这又是一个"小"的细节。

---

## 三、为什么这样设计是 sound 的

### 3.1 双部分的切分策略:为什么整数键进数组

源码注释(`ltable.c:14`–`18`)一句话给出切分准则:

> Non-negative integer keys are all candidates to be kept in the array part. The actual size of the array is the largest 'n' such that more than half the slots between 1 and n are in use.

也就是说,整数键 `1, 2, 3, ...` 是数组部分的候选;数组部分的大小 `asize` 取"最大的 n,使得 `1..n` 里超过一半的槽在被用"。这个"超过一半"的阈值(`computesizes` 里的 `arrayXhash(na,nh)`,即 `na <= nh*3`,见 `ltable.c:435`)是内存代价权衡:一个哈希 `Node` 占的字节数大约是一个数组槽的 3 倍(两个 `Value` + next vs 一个 `Value`),所以当某段整数键里"实际在用的数量 × 3"已经大于或等于"哈希槽数 × 3",把它们挪进数组更省内存。

这个切分是动态的:每次 rehash(`luaH_resize`,`ltable.c:716`)都会重新 `numusearray` + `numusehash`(`ltable.c:488`/`521`)统计当前所有整数键的分布,重新算出最优 `asize`,然后把超出新 `asize` 的整数键从数组挪进哈希(`reinsertOldSlice`,`ltable.c:676`),或把原来在哈希里的连续整数键挪进数组(`reinserthash` 走 `newcheckedkey`,会按新 `asize` 自动分流)。所以一个表用着用着,它的数组/哈希边界是会自动调整的——`t[1]=1; t[2]=2; t[3]=3` 一开始可能 `t[3]` 在哈希里(因为还没触发 rehash),扩容时被收进数组。

切分对正确性的保证是:任意一个整数 key,要么 `1 <= key <= asize` 在数组部分(`ikeyinarray` 返回非 0),要么在哈希部分,**绝不会两边都有**(数组部分只装 `1..asize`,哈希部分只装 `asize` 以外的整数键和其他类型键)。这是 `luaH_getint` 那个两分支 `if` 的不变式。rehash 重新切分时,`computesizes` 算出新 `asize`,所有 `<= newasize` 的整数键收进数组,`> newasize` 的挪进哈希,切分不变式始终成立。

### 3.2 dummynode:消灭空表的条件分支

2.4 讲过空表的哈希部分指向全局共享的 `dummynode`。这个设计的 sound 在于:它让"表有没有哈希部分"这件事对所有表都恒为真。访问代码(`luaH_getint` 的 `getintfromhash`、`luaH_Hgetshortstr` 等)不必在入口判 `t->node == NULL`,直接走 `hashint`/`hashstr` 算主位置、沿链找。`dummynode` 的键是 `(DEADKEY, NULL)`,和任何合法 `TValue` 都不相等(`equalkey` 永远返回假),`next` 是 0(链尾),所以任何查找都会立即走到"链尾,next==0,返回 absentkey"——逻辑上等价于"哈希部分是空的",但省掉了一次 `if`。

代价是一个全局静态对象(几十字节),换来所有表访问热路径上少一次分支预测失败的潜在开销。这是典型的"用一点点静态内存换热路径上的可预测性"。

### 3.3 值/标签分离:padding 不丢正确性

2.3 的倒排数组结构,正确性靠 `getArrTag`/`getArrVal`/`arr2obj`/`obj2arr` 这套宏保证:任何一个抽象下标 `k`,它的 tag 永远在 `getArrTag(t,k)`、值永远在 `getArrVal(t,k)`,读写都成对进行(`arr2obj` 同时读 tag 和 value,`obj2arr` 同时写)。`tagisempty(tag)` 用 `novariant(tag) == LUA_TNIL`(`lobject.h:204`)判断空——数组部分空槽的 tag 是 `LUA_VEMPTY`(`lobject.h:186`,`makevariant(LUA_TNIL, 1)`),它和标准 nil(`LUA_VNIL`)、absent(`LUA_VABSTKEY`)、notable(`LUA_VNOTABLE`)都是 nil 的 variant,统一被 `tagisempty` 判空。所以"这个槽有没有值"只看 tag 一个字节,不必读 value。

`clearNewSlice`(`ltable.c:694`)在数组扩容时把新槽的 tag 全置 `LUA_VEMPTY`,保证新分配的槽一律被视为空,不会读到上一次分配残留的值——这是扩容时不丢正确性的关键。`resizearray`(`ltable.c:563`)搬元素时用 `memcpy` 整段搬值段和标签段(因为新旧的 `concretesize` 不同,但公共前缀的布局是一致的),搬完释放老块,地址计算 `np += newasize` 把指针重新定位到新块的交界处,保证 `getArrVal`/`getArrTag` 在新地址上仍然正确。

### 3.4 pset/finishset 拆分:让快速路径不抛错

2.7 讲了 5.5 把写入拆成 pset + finishset。这个拆分的 sound 在于:**VM 的快速路径不能抛错、不能分配内存**(`vmcase` 里的代码如果不 `Protect`,就不在 `setjmp` 保护下,一旦内存分配失败会直接 longjmp 跳过解释器的状态维护)。rehash 会分配内存(新的 Node 数组、新的 array),可能失败、可能触发紧急 GC,这些都不能在快速路径里发生。

pset 的契约就是:**只在能就地完成时返回 `HOK`,但凡需要 rehash、需要 barrier、需要查元方法,一律返回非 `HOK` 编码**。VM 拿到非 `HOK` 才 `Protect(luaH_finishset(...))`,这时已经在保护下,可以安全地 rehash、抛错。这个拆分把"可能出错的慢操作"干净地从"必须无错的快路径"里隔离出来,是 5.5 配合 VM 解释器循环的一次结构化重设计。老资料里 `luaH_set` 一个函数搞定、内部直接 rehash 的写法,在 5.5 的 VM 快速路径模型下是行不通的。

### 3.5 元方法缓存:不漏不误报

`flags` 缓存"该元方法不存在",正确性靠两点保证:

1. **初始全 1**(新表 `flags = maskflags`)是保守的——假定啥元方法都没有。这只会让第一次真正有元方法的查找多走一次 `luaT_gettm`(查到后会把对应位清 0),不会漏报"有元方法"。
2. **凡可能改变元方法集合的操作,都 `invalidateTMcache`**。设新元表(`lua_setmetatable`)、往可能是元方法名字的字段写值(`luaH_psetshortstr` 里 `insertkey` 成功后)都会清缓存。清了之后位变 0("可能有"),下次访问重新查,查不到再置 1。所以缓存只会"临时乐观",不会"永久漏报"。

这套机制对 GC 也有意义:`TM_GC`(`__gc` 元方法)和 `TM_MODE`(`__mode`,弱表)都走快速缓存。一个表有没有 `__gc` 决定它要不要被 GC 特殊对待(P5-17),这个判断在 GC 遍历时是热路径,缓存让它在一次 `checknoTM` 内完成。

---

## 四、★对照 CPython + 回扣主线

### 4.1 数据模型:list/dict 分立 vs 一个 Table

CPython 的内建复合类型是一组专用类型,各自一套 C 实现:

- **`list`**:底层是 `PyListObject`,核心是一段 `PyObject **` 指针数组(`ob_item`),只能用连续整数下标 `0..size-1` 访问。扩容是 `realloc` 这段指针数组。
- **`dict`**:底层是 `PyDictObject`,核心是一张哈希表(5.3+ 是"分离的键数组 + 值数组"的紧凑布局,带空槽索引),key 可以是任意可哈希对象。
- 还有 `tuple`(不可变序列)、`set`(哈希集合)、`frozenset`、`bytes`(不可变字节序列)、`bytearray` 等,每种都是一个独立的类型对象、一套独立的创建/遍历/GC 代码。

一个 Python 对象不能既是 `list` 又是 `dict`。要在同一个结构里既存有序数据又存命名属性,常见做法是嵌套(一个 `dict` 里某个 key 指向一个 `list`),两种类型各自管理自己的存储。

Lua 的 `Table` 是另一种取舍。源码里只有一个 `Table` 结构(`lobject.h:776`),它的 `array` + `node` 并存:连续整数键 `1..asize` 进 `array`,其余键(离散整数、字符串、布尔、对象等)进 `node`。一个 `Table` 可以同时是:

- **数组**:`t[1] t[2] t[3]` 走 `array` 部分。
- **字典**:`t["name"] = "lua"` 走 `node` 部分。
- **对象**:`t` 挂一个 `metatable`(也是 `Table`),靠 `__index`/`__newindex` 等元方法实现面向对象(P6-19)。对象的"字段"就是 `Table` 里的短串键。
- **命名空间/模块/全局环境**:Lua 的全局变量表、`require` 进来的模块、C API 的注册表,本质都是 `Table`。看 `lstate.c:186`–`200` 的 `init_registry`:

```c
static void init_registry (lua_State *L, global_State *g) {
  /* create registry */
  Table *registry = luaH_new(L);
  sethvalue(L, &g->l_registry, registry);
  luaH_resize(L, registry, LUA_RIDX_LAST, 0);
  ...
  /* registry[LUA_RIDX_GLOBALS] = new table (table of globals) */
  sethvalue(L, &aux, luaH_new(L));
  luaH_setint(L, registry, LUA_RIDX_GLOBALS, &aux);
  ...
}
```

(源码 `lstate.c:186`–`200`。)

注册表本身是一个 `Table`(`registry`),它的第 `LUA_RIDX_GLOBALS` 个整数槽又指向另一个 `Table`——全局变量表。一个 Lua 程序里所有的全局变量(`print`、`string`、`math`、用户自己写的 `foo = function() ... end`),全住在这个"全局 Table"里,以短串为键。模块也一样:`require "math"` 拿到的 `math`,是一个以短串为键、以函数为值的 `Table`。整个 Lua 的命名体系,从全局到模块到对象字段,清一色是 `Table`。

这是"统一"最彻底的兑现:**一种数据结构,通吃四种用法**。代价是——一个 `Table` 在任何单一维度上都不是最优的:当数组用,它比 CPython `list` 多了 `node` 指针和 `lsizenode`/`flags` 等字段的几个字节开销(虽然空表时 `node` 指向共享 dummynode,几乎零开销);当字典用,它的哈希部分是链表式开放寻址,在高负载下不如 CPython 5.3+ 的紧凑分离布局缓存友好。但换回来的是:**内核只需要一套创建、遍历、GC 代码服务所有复合数据**,源码量、二进制体积、学习成本全都下来。这正是"小"的根源之一。

而它没有因此变慢:整数键命中数组部分时是 O(1) 直接下标(2.6 的 `luaH_fastgeti`),短串键靠驻留退化成指针比较(2.5 的 `eqshrstr`),这两条最常见的路径都做到了接近 C 数组和 C 字符串比较的性能。统一的代价只发生在少数离散键和长串键上。

### 4.2 这套设计换来了什么:小结

把这一章的所有机制对到主线上:

| 机制 | 服务"小" | 服务"快" |
|---|---|---|
| 一个 `Table` 兼任数组/字典/对象/命名空间 | 一套创建/遍历/GC 代码,源码量小 | — |
| 数组部分 + 哈希部分并存 | — | 整数键 O(1) 直接下标,缓存友好 |
| Node 键字段拆开存放 | `Node` 在 4/8 字节对齐下都更小 | — |
| 5.5 值/标签分离的倒排数组 | 消灭 padding,大数组省内存 | tag 单字节判空 |
| dummynode 共享空表哈希 | 空表零哈希内存 | 热路径少一次分支 |
| flags 元方法缓存 | 一个字节管 6 个元方法的"存在性" | 99% 的访问 `checknoTM` 一步短路 |
| 分入口 `getint`/`getshortstr`/`get` | — | 每条 VM 字节码配最短查找路径 |
| pset/finishset 拆分 | — | 快速路径不抛错,慢路径才 rehash |
| `lenhint` 藏在数组中缝 | 不占 `Table` 字段 | `#t` 常数步命中 |

每一行都是一个具体的设计决定,每一个决定都同时落在"小"或"快"至少一端。把它们合起来看,`Table` 这一个结构之所以能用一套代码同时拿下数组和字典两种用法、又不在这两种用法上明显吃亏,靠的就是这些细节层层叠加:布局上消灭 padding、空表上消灭分配、热路径上消灭分支和函数调用、元方法上消灭重复查找。

这就是"统一的皇冠"在源码层面的具体含义。Lua 砍掉 `list`/`dict`/`tuple`/`set` 的分立,不是靠"写一个能干所有事但都干得马虎的通用结构",而是靠"一个结构里精心切分出数组部分和哈希部分,各自做到接近专用类型的性能"。统一的代价(哈希部分不如专用 dict 紧凑)被控制在少数场景,而统一的红利(一套代码、一致的对象模型、到处可用的 `:` 语法和元表机制)渗透到语言的每一个角落。

本章只讲了结构和基础访问。哈希函数怎么选、冲突怎么用 Brent 变体处理、rehash 时数组/哈希怎么重新切分、`next` 遍历为什么不会因为 rehash 而崩——这些是 [P1-05 Table(下):哈希探测与 rehash](P1-05-Table下-哈希探测与rehash.md) 的内容。

---

*下一章 [P1-05 Table(下):哈希探测与 rehash](P1-05-Table下-哈希探测与rehash.md):深入 `mainpositionTV`、`insertkey` 的 Brent 变体冲突解决,以及 `rehash` 时如何用 `computesizes` 重新切分数组和哈希。*
