# P1-05 Table(下):哈希探测与 rehash

> **本书主线**:统一与精简换小而快。**二分法**:编译侧 ↔ 执行侧(本章在数据根基 P1)。**★对照**:CPython 的 `dict`。**源码**:lua-5.5.0,核心文件 `ltable.c`(1355 行)。**基调**:纯直球,不用比喻。

---

## 一、这章解决什么问题

上一章(P1-04)把 `Table` 的两半摆出来了:连续整数键进数组部分 `array`,其余进哈希部分 `node`。那章留下两个问题悬而未决,正是本章要回答的:

1. **哈希部分满了怎么办?** 一个 `Table` 的哈希部分大小是固定的(`2^lsizenode` 个 `Node`),而往里塞 key 的操作随时发生。当所有槽位都占满,下一次 `t[新key] = v` 该怎么落位?显然得扩容。但 Lua 不能只做"哈希扩容"——因为它的数组部分和哈希部分是**联动**的:原来在哈希里的整数键 `5`,等数组部分长大到能容下它时,就该搬进数组。扩容必须重新决定两半的切分。

2. **既快又不浪费,怎么同时拿到?** "快"要求装填因子不能太低(否则内存浪费)、冲突不能太严重(否则探测链长);"不浪费"又要求不能预留太多空槽。这两个目标在开放寻址里天然打架。Lua 用一套精巧的算法——**统计每个 `2^k` 区间内的整数键数量,贪心地选出"数组部分利用率最高"的切分点**——同时压住两端。这是 Lua Table 最精妙的一招,也是本章重头戏。

还有一个绕不开的细节:被 `t[k] = nil` 删掉的 key,能不能立刻从哈希表里物理抹掉?不能。因为 Lua 的 `next` 函数要遍历表,而遍历可能正好停在某个被删的 key 上——如果那个 key 已经物理消失,`next` 就找不到下一个了。所以 Lua 引入 `DEADKEY` 标记,让"逻辑删除"和"物理回收"分离。

本章讲完这些,P1 数据根基就闭环了:从一个 `TValue` 是什么(P1-02),到字符串怎么存(P1-03),到 `Table` 怎么既当数组又当字典(P1-04),再到哈希怎么扩容收敛(P1-05)——之后 P2 转入编译侧时,所有"值长什么样"的基础都已铺好。

**一个贯穿全章的提醒**:本章依据 lua-5.5.0 的 `ltable.c` 实际源码。5.5 相对老资料(讲 5.3/5.4 的书和博客)有几处实质改动,凡是与老资料冲突的地方,本章都会显式标注差异,以 5.5 源码为准。

---

## 二、源码怎么实现

先回到 `Table` 结构(`lobject.h:776`),把上一章没讲透的几个字段补上:

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

`lsizenode` 是哈希部分大小的 log2——也就是说哈希部分永远是 `2^n` 个 `Node`(n=`lsizenode`)。这是个刻意的选择:大小是 2 的幂,取模就能用位运算,比 `%` 快得多。马上会看到这个选择怎么落地。

`Node` 是哈希表的一个槽位,定义在 `lobject.h:751`:

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

注意 `Node` 是个 union:既能当完整的 `TValue`(取值时用 `i_val`,通过 `gval(n)` 宏)看,又把 key 拆成 `key_tt` + `key_val` 两个独立字段存。这种拆法让 `Node` 在 4 字节和 8 字节对齐下都比"直接塞两个 `TValue`"更省空间。每个 `Node` 还带一个 `int next` 字段——这是开放寻址链的"偏移量",不是指针(下面会讲为什么用偏移量)。

### 2.1 哈希函数:每种键类型各算各的

Lua 的 key 可以是整数、浮点、字符串(短/长)、布尔、lightuserdata、C 函数、full userdata。每种算哈希的方式不一样,集中在 `mainpositionTV`(`ltable.c:188`):

```c
static Node *mainpositionTV (const Table *t, const TValue *key) {
  switch (ttypetag(key)) {
    case LUA_VNUMINT: {
      lua_Integer i = ivalue(key);
      return hashint(t, i);
    }
    case LUA_VNUMFLT: {
      lua_Number n = fltvalue(key);
      return hashmod(t, l_hashfloat(n));
    }
    case LUA_VSHRSTR: {
      TString *ts = tsvalue(key);
      return hashstr(t, ts);
    }
    case LUA_VLNGSTR: {
      TString *ts = tsvalue(key);
      return hashpow2(t, luaS_hashlongstr(ts));
    }
    case LUA_VFALSE:
      return hashboolean(t, 0);
    case LUA_VTRUE:
      return hashboolean(t, 1);
    case LUA_VLIGHTUSERDATA: {
      void *p = pvalue(key);
      return hashpointer(t, p);
    }
    case LUA_VLCF: {
      lua_CFunction f = fvalue(key);
      return hashpointer(t, f);
    }
    default: {
      GCObject *o = gcvalue(key);
      return hashpointer(t, o);
    }
  }
}
```

`mainposition` 就是"键的哈希值落到哈希表的第几个槽位"——后面所有探测都从它开始。这里的分歧在于**用哪种取模方式**。看 `ltable.c:106` 起的几个宏:

```c
/* When the original hash value is good, hashing by a power of 2
   avoids the cost of '%'. */
#define hashpow2(t,n)		(gnode(t, lmod((n), sizenode(t))))

/* for other types, it is better to avoid modulo by power of 2, as
   they can have many 2 factors. */
#define hashmod(t,n)	(gnode(t, ((n) % ((sizenode(t)-1u)|1u))))

#define hashstr(t,str)		hashpow2(t, (str)->hash)
#define hashboolean(t,p)	hashpow2(t, p)
#define hashpointer(t,p)	hashmod(t, point2uint(p))
```

这里有个很关键的取舍,老资料经常一带而过:

- **`hashpow2`**:直接 `h & (size-1)`(`lmod` 宏,`lobject.h:824`,且带 `check_exp` 断言 size 是 2 的幂)。只有当哈希值本身已经"分布足够均匀"时才安全——否则低位相同的键全挤进同一个槽。
- **`hashmod`**:用 `h % ((size-1)|1)`。注意是 `size-1` 再或上 1,保证除数是个**奇数**(不一定是 2 的幂的约数),避免"键的低位全相同导致取模后全落同槽"的问题。代价是 `%` 比 `&` 慢。

Lua 的判断是:字符串哈希(短串)和布尔值的哈希分布已经足够好,用快的 `hashpow2`;而**整数键、浮点键、指针键**用 `hashmod`。为什么整数键也用 `hashmod`?因为整数键太可能"低位有规律"——比如 `1, 2, 3, ...` 这种连续整数,如果直接 `& (size-1)` 且 size=8,那 `1,9,17,25...` 全落槽 1。`hashmod` 用奇数除数把这个规律打散。

看 `hashint`(`ltable.c:145`)的真实实现:

```c
static Node *hashint (const Table *t, lua_Integer i) {
  lua_Unsigned ui = l_castS2U(i);
  if (ui <= cast_uint(INT_MAX))
    return gnode(t, cast_int(ui) % cast_int((sizenode(t)-1) | 1));
  else
    return hashmod(t, ui);
}
```

这里有个**性能优化分支**:如果整数小到能塞进 `int`,就用 `int` 的 `%`(在很多 CPU 上比 `unsigned` 的 `%` 略快,因为编译器知道符号);否则用 `hashmod` 的无符号路径。这是个针对"绝大多数整数键都是小整数"这个统计事实的微优化。

**字符串哈希**则分两路。短串(`LUA_VSHRSTR`)的哈希在驻留时就算好存在 `TString.hash` 字段里(P1-03 讲过短串驻留),用 `hashstr` 直接 `hashpow2`。长串(`LUA_VLNGSTR`)用 `luaS_hashlongstr`(`lstring.c:61`)——它是**惰性**的:第一次访问才算哈希,算完置 `extra=1`,之后直接用缓存:

```c
unsigned luaS_hashlongstr (TString *ts) {
  lua_assert(ts->tt == LUA_VLNGSTR);
  if (ts->extra == 0) {  /* no hash? */
    size_t len = ts->u.lnglen;
    ts->hash = luaS_hash(getlngstr(ts), len, ts->hash);
    ts->extra = 1;  /* now it has its hash */
  }
  return ts->hash;
}
```

底层的 `luaS_hash`(`lstring.c:53`)长这样:

```c
static unsigned luaS_hash (const char *str, size_t l, unsigned seed) {
  unsigned int h = seed ^ cast_uint(l);
  for (; l > 0; l--)
    h ^= ((h<<5) + (h>>2) + cast_byte(str[l - 1]));
  return h;
}
```

`seed` 来自全局 `G(L)->seed`,在 `lua_State` 创建时随机化。**这是个安全相关的设计**:哈希里混入了进程级随机种子,使得外部攻击者难以构造大量哈希碰撞的 key 来把 Table 退化成 O(n) 探测(哈希碰撞攻击)。注意 `h ^= (h<<5)+(h>>2)+byte` 这一步是从右往左扫字符串——这是个经典的、 Avalanche 性质不错的简单哈希(JS 引擎的字符串哈希也用类似形式)。

**浮点键哈希** `l_hashfloat`(`ltable.c:168`)有一段注释解释的数值细节,核心思想是把浮点拆成"尾数 + 指数",用 `frexp` 把 `n` 规整到 `[0.5, 1.0)` 然后乘以 `-INT_MIN`(在补码下 `-INT_MIN` 是个能精确表示的大正数),再和指数相加。inf/NaN 返回 0。

到这里,所有键类型的哈希都覆盖了。下一步是冲突时怎么探测。

### 2.2 开放寻址探测:链式散列 + Brent 变体

这是本章最容易讲错、老资料最容易过时的一节。先把结论摆出来:

> **5.5 的 Table 哈希用开放寻址 + 链式散列表(chained scatter table)+ Brent 变体。冲突元素之间通过 `Node.next` 字段(存的是相对偏移量,不是指针)链成一条链。Brent 变体的核心是:插入新 key 时,如果它和某个"不在自己 main position 的 key"冲突,会移动那个 key 而不是新 key。**

先看查询路径,它最清楚展示了链是怎么走的。`getgeneric`(`ltable.c:291`):

```c
static const TValue *getgeneric (Table *t, const TValue *key, int deadok) {
  Node *n = mainpositionTV(t, key);
  for (;;) {  /* check whether 'key' is somewhere in the chain */
    if (equalkey(key, n, deadok))
      return gval(n);  /* that's it */
    else {
      int nx = gnext(n);
      if (nx == 0)
        return &absentkey;  /* not found */
      n += nx;
    }
  }
}
```

逻辑很简单:从 `mainposition` 出发,逐个比对 key;不匹配就 `n += gnext(n)` 走到链上的下一个 `Node`;`gnext` 返回 0 表示链到头了,没找到。

**`gnext(n)` 返回的是 `int` 类型的偏移量**(`Node.u.next` 字段),不是指针。`n += nx` 把指针加上这个偏移。为什么用偏移而不是指针?省内存:一个 `int` 偏移在 32/64 位系统都只要 4 字节,而指针在 64 位要 8 字节。对每个 `Node` 都省下 4 字节,大表累积可观。而且 `next=0` 天然表示"链尾",不需要额外的标志位。

整数键的查询路径 `getintfromhash`(`ltable.c:929`)走的是同一条链,只是比对方式从通用的 `equalkey` 换成了"直接比整数":

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

短串查询 `luaH_Hgetshortstr`(`ltable.c:975`)同理,只是比对换成 `eqshrstr`(短串相等就是指针相等,因为短串驻留)。

这套链式散列最关键的不变式,`ltable.c:18` 的文件头注释说得很清楚:

> A main invariant of these tables is that, if an element is not in its main position (i.e. the 'original' position that its hash gives to it), then the colliding element is in its own main position.

也就是说:**链的"头"永远是 main position 的占有者;链上后续的元素,要么它自己的 main position 就是这个槽(同一个哈希值),要么它是被挤过来的——但即便被挤过来,挤它的那个元素一定在自己 main position。** 这个不变式是 Brent 变体的精髓,它保证了即使装填因子到 100%,平均探测长度也不会失控。

下面看插入逻辑怎么维持这个不变式。这是 `insertkey`(`ltable.c:859`),全章最硬核的函数:

```c
static int insertkey (Table *t, const TValue *key, TValue *value) {
  Node *mp = mainpositionTV(t, key);
  /* table cannot already contain the key */
  lua_assert(isabstkey(getgeneric(t, key, 0)));
  if (!isempty(gval(mp)) || isdummy(t)) {  /* main position is taken? */
    Node *othern;
    Node *f = getfreepos(t);  /* get a free place */
    if (f == NULL)  /* cannot find a free place? */
      return 0;
    lua_assert(!isdummy(t));
    othern = mainpositionfromnode(t, mp);
    if (othern != mp) {  /* is colliding node out of its main position? */
      /* yes; move colliding node into free position */
      while (othern + gnext(othern) != mp)  /* find previous */
        othern += gnext(othern);
      gnext(othern) = cast_int(f - othern);  /* rechain to point to 'f' */
      *f = *mp;  /* copy colliding node into free pos. (mp->next also goes) */
      if (gnext(mp) != 0) {
        gnext(f) += cast_int(mp - f);  /* correct 'next' */
        gnext(mp) = 0;  /* now 'mp' is free */
      }
      setempty(gval(mp));
    }
    else {  /* colliding node is in its own main position */
      /* new node will go into free position */
      if (gnext(mp) != 0)
        gnext(f) = cast_int((mp + gnext(mp)) - f);  /* chain new position */
      else lua_assert(gnext(f) == 0);
      gnext(mp) = cast_int(f - mp);
      mp = f;
    }
  }
  setnodekey(mp, key);
  lua_assert(isempty(gval(mp)));
  setobj2t(cast(lua_State *, 0), gval(mp), value);
  return 1;
}
```

走一遍两种情况:

**情况 A:main position 是空的。** 直接 `setnodekey(mp, key)` 把 key 写进去,`next` 保持 0(没有链)。这是无冲突的快路径。

**情况 B:main position 被占了。** 这时分两种子情况:

- **B1:占住 mp 的那个节点,它自己的 main position 不是 mp**(说明它是被更早的冲突挤过来的)。这是 Brent 变体的触发条件。此时 Lua **不动新 key,而是把占住 mp 的旧节点挪走**到空位 `f`,把 mp 让给新 key(因为新 key 的 main position 就是 mp,让它"归位"能让查询路径最短)。挪动要做三件事:
  1. 从 mp 所属链的链头 `othern` 出发,顺着 `next` 走到 mp 的前驱(`while (othern + gnext(othern) != mp)`)。
  2. 把前驱的 `next` 改为指向 `f`(`gnext(othern) = f - othern`)。
  3. 把 mp 的内容(key/value/next)整块拷贝到 `f`(`*f = *mp`),然后修正 `f` 的 `next`:如果 mp 原来有后续链,要把那条链的相对偏移从"以 mp 为基准"换算成"以 f 为基准"(`gnext(f) += mp - f`),最后把 mp 的 `next` 清零、`gval(mp)` 置空,让 mp 变回空槽给新 key 用。

- **B2:占住 mp 的节点,它自己的 main position 就是 mp**(它和 new key 哈希冲突,但都在自己的 main position 上)。这时不能移动旧节点(它已经在自己的归位点),只能让**新 key 去空位 `f`**,并把 mp 的链延伸到 f(`gnext(mp) = f - mp`)。如果 mp 原来已经有链,要把 f 插到链的第二个位置(`gnext(f) = (mp + gnext(mp)) - f`)。

这套逻辑维持的不变式就是上面注释说的那条:**任何时刻,一个槽位要么被"main position 就是它的 key"占据,要么这条链的头是这样的 key。** 查询时,从 main position 出发顺着链走,最多走"链长"步就能找到——而链长被装填因子和哈希质量共同约束,平均很小。

`getfreepos`(`ltable.c:829`)负责找一个空槽。这里有个 5.5 的优化:

```c
static Node *getfreepos (Table *t) {
  if (haslastfree(t)) {  /* does it have 'lastfree' information? */
    /* look for a spot before 'lastfree', updating 'lastfree' */
    while (getlastfree(t) > t->node) {
      Node *free = --getlastfree(t);
      if (keyisnil(free))
        return free;
    }
  }
  else {  /* no 'lastfree' information */
    unsigned i = sizenode(t);
    while (i--) {  /* do a linear search */
      Node *free = gnode(t, i);
      if (keyisnil(free))
        return free;
    }
  }
  return NULL;  /* could not find a free place */
}
```

**⚠️ 5.5 与老资料的一个差异**:`lastfree` 字段在 5.5 是**有条件存在**的——只有当 `lsizenode >= LIMFORLAST`(即 `LIMFORLAST=3`,哈希表大小 >= 8)时,才会在 Node 数组前面额外分配一个 `Limbox` 来存 `lastfree` 指针(`ltable.c:49`、`ltable.c:62`)。小表(大小 1/2/4)不存 `lastfree`,直接线性扫描全表找空槽。这是个"小表不值得为 `lastfree` 多分配一次内存"的权衡。老资料(讲 5.3/5.4)经常笼统地说"Table 有个 lastfree 字段",在 5.5 不准确——它存在 Node 数组前的 Limbox 里,且只有大表才有。

`haslastfree` 和 `getlastfree` 宏(`ltable.c:62`、`ltable.c:63`):

```c
#define haslastfree(t)     ((t)->lsizenode >= LIMFORLAST)
#define getlastfree(t)     ((cast(Limbox *, (t)->node) - 1)->lastfree)
```

`lastfree` 从 Node 数组末尾往前找空位,这是个单调递减的游标——因为插入倾向于把空位消耗在前面,后面早被占满,从后往前扫能更快命中空位。

### 2.3 哈希表大小与装填因子

哈希部分大小总是 `2^lsizenode`(`sizenode(t)` 宏,`lobject.h:829`)。最小是 1(`lsizenode=0`),但 `lsizenode=0` 的表实际上用 `dummynode`——见 2.5 节。

**装填因子与 rehash 触发**:Lua 的哈希部分**允许装填到 100%**——这正是 Brent 变体存在的理由(文件头注释说 "even when the load factor reaches 100%, performance remains good")。rehash 的触发点在 `luaH_newkey`(`ltable.c:914`):

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

逻辑是:先尝试 `insertkey`,它返回 0 表示"找不到空位了"(`getfreepos` 返回 NULL,即哈希部分 100% 占满)。这时才调 `rehash` 扩容,扩容完用 `newcheckedkey` 把 key 重新插进去。

注意 `t[k] = nil` 不走这条路径——`if (!ttisnil(value))` 直接挡掉。给一个 key 赋 nil 不是"插入",而是"删除"(走另一条路径把 value 置空、key 标 DEADKEY,见 2.6)。

**和 CPython 的对比预告**:CPython 的 dict 在装填到 2/3 就触发扩容,Lua 容忍到 100%。这是两种哲学的对照,第四节详讲。

### 2.4 rehash:数组/哈希的自动切分算法

这是 Lua Table 设计的精华。`rehash`(`ltable.c:762`)本身不长,但它调用的 `numusehash`/`numusearray`/`computesizes` 三个函数合起来构成了"自动切分"算法:

```c
static void rehash (lua_State *L, Table *t, const TValue *ek) {
  unsigned asize;  /* optimal size for array part */
  Counters ct;
  unsigned i;
  unsigned nsize;  /* size for the hash part */
  /* reset counts */
  for (i = 0; i <= MAXABITS; i++) ct.nums[i] = 0;
  ct.na = 0;
  ct.deleted = 0;
  ct.total = 1;  /* count extra key */
  if (ttisinteger(ek))
    countint(ivalue(ek), &ct);  /* extra key may go to array */
  numusehash(t, &ct);  /* count keys in hash part */
  if (ct.na == 0) {
    /* no new keys to enter array part; keep it with the same size */
    asize = t->asize;
  }
  else {  /* compute best size for array part */
    numusearray(t, &ct);  /* count keys in array part */
    asize = computesizes(&ct);  /* compute new size for array part */
  }
  /* all keys not in the array part go to the hash part */
  nsize = ct.total - ct.na;
  if (ct.deleted) {  /* table has deleted entries? */
    /* insertion-deletion-insertion: give hash some extra size to
       avoid repeated resizings */
    nsize += nsize >> 2;
  }
  /* resize the table to new computed sizes */
  luaH_resize(L, t, asize, nsize);
}
```

先看它统计的是什么。`Counters` 结构(`ltable.c:421`):

```c
typedef struct {
  unsigned total;
  unsigned na;
  int deleted;
  unsigned nums[MAXABITS + 1];
} Counters;
```

- `total`:表里所有 key 的总数(包括即将插入的 ek)。
- `na`:能当数组下标的整数 key 数量(即 `1 <= k <= MAXASIZE` 的整数)。
- `deleted`:哈希部分是否有被删的节点(有就给哈希多留 25% 余量,避免"插入-删除-插入"反复触发 rehash)。
- `nums[i]`:落在区间 `(2^(i-1), 2^i]` 内的整数 key 数量。比如 `nums[3]` 是 key 在 `(4, 8]` 即 5/6/7/8 的个数。这是选数组大小的核心数据。

`countint`(`ltable.c:470`)把一个整数 key 计入对应的桶:

```c
static void countint (lua_Integer key, Counters *ct) {
  unsigned int k = arrayindex(key);
  if (k != 0) {  /* is 'key' an array index? */
    ct->nums[luaO_ceillog2(k)]++;  /* count as such */
    ct->na++;
  }
}
```

`arrayindex` 宏(`ltable.c:319`)检查 key 是否在 `[1, MAXASIZE]` 范围内,是就返回它,否则返回 0。`luaO_ceillog2(k)`(`lobject.c:37`)算 `ceil(log2(k))`——正好把 key 映射到对应的 `nums` 下标。比如 `k=5`, `ceil(log2(5))=3`,计入 `nums[3]`。

`numusehash`(`ltable.c:521`)遍历哈希部分的每个 Node,对每个非空节点:`total++`,如果是整数 key 就 `countint`。同时检测 DEADKEY(`isempty(gval(n))` 但 key 不为 nil):

```c
static void numusehash (const Table *t, Counters *ct) {
  unsigned i = sizenode(t);
  unsigned total = 0;
  while (i--) {
    Node *n = &t->node[i];
    if (isempty(gval(n))) {
      lua_assert(!keyisnil(n));  /* entry was deleted; key cannot be nil */
      ct->deleted = 1;
    }
    else {
      total++;
      if (keyisinteger(n))
        countint(keyival(n), ct);
    }
  }
  ct->total += total;
}
```

`numusearray`(`ltable.c:488`)遍历数组部分,同样按 `2^lg` 区间统计:

```c
static void numusearray (const Table *t, Counters *ct) {
  int lg;
  unsigned int ttlg;  /* 2^lg */
  unsigned int ause = 0;
  unsigned int i = 1;
  unsigned int asize = t->asize;
  for (lg = 0, ttlg = 1; lg <= MAXABITS; lg++, ttlg *= 2) {
    unsigned int lc = 0;
    unsigned int lim = ttlg;
    if (lim > asize) {
      lim = asize;
      if (i > lim)
        break;
    }
    for (; i <= lim; i++) {
      if (!arraykeyisempty(t, i))
        lc++;
    }
    ct->nums[lg] += lc;
    ause += lc;
  }
  ct->total += ause;
  ct->na += ause;
}
```

这一通统计下来,`ct.nums[i]` 里就有了"全表(数组 + 哈希 + 新 key)中,落在 `(2^(i-1), 2^i]` 区间的整数 key 数量"。接下来 `computesizes`(`ltable.c:446`)用这套数据选数组大小:

```c
static unsigned computesizes (Counters *ct) {
  int i;
  unsigned int twotoi;  /* 2^i (candidate for optimal size) */
  unsigned int a = 0;  /* number of elements smaller than 2^i */
  unsigned int na = 0;  /* number of elements to go to array part */
  unsigned int optimal = 0;
  for (i = 0, twotoi = 1;
       twotoi > 0 && arrayXhash(twotoi, ct->na);
       i++, twotoi *= 2) {
    unsigned nums = ct->nums[i];
    a += nums;
    if (nums > 0 &&  /* grows array only if it gets more elements... */
        arrayXhash(twotoi, a)) {  /* ...while using "less memory" */
      optimal = twotoi;
      na = a;
    }
  }
  ct->na = na;
  return optimal;
}
```

这是核心。算法从 `i=0`(候选大小 1)开始倍增,每步:

1. `a += nums[i]`:累加到目前为止能进数组的整数 key 总数。
2. 判断"如果数组大小是 `2^i`,会不会比把这些 key 全放进哈希更省内存"。判断用 `arrayXhash` 宏(`ltable.c:435`):

```c
/* Check whether it is worth to use 'na' array entries instead of 'nh'
   hash nodes. (A hash node uses ~3 times more memory than an array
   entry: Two values plus 'next' versus one value.) Evaluate with size_t
   to avoid overflows. */
#define arrayXhash(na,nh)	(cast_sizet(na) <= cast_sizet(nh) * 3)
```

注释解释了 3 倍这个数字的来源:一个 `Node` 装"两个 `Value`(key 和 value)+ next 字段",而一个数组元素只装"一个 `Value`"(tag 单独存在 tag 数组里,见 2.7),所以 Node 大约是数组元素的 3 倍。`arrayXhash(na, nh)` 为真,意味着"用 na 个数组槽位装这些 key,比用 nh 个哈希 Node 装它们**省内存**"。

3. 如果 `nums[i] > 0`(这一段确实有整数 key)且 `arrayXhash(twotoi, a)` 成立(数组大小 twotoi 装下 a 个元素是省的),就更新 `optimal = twotoi`, `na = a`。

这个贪心算法的效果:**选出最大的那个 `2^i`,使得"数组部分装下的整数 key 数 a"相对于"数组大小 2^i"达到了"比放哈希更省"的门槛,同时每一级都是"装得越多越优"**。结果就是文件头注释(`ltable.c:17-19`)那条规则——"数组大小是最大的 n,使得 `[1, n]` 内超过一半的槽位在被使用"。

**举个具体例子**。假设一个表当前的整数 key 是 `{1, 2, 3, 5, 7, 100}`,外加一个字符串 key `"x"`。`nums` 数组大概这样(只列关键的几级):

- `nums[0]`(key=1):1
- `nums[1]`(key 2):1
- `nums[2]`(key 3-4):1(key=3)
- `nums[3]`(key 5-8):2(key=5,7)
- `nums[4]`(key 9-16):0
- ...
- `nums[7]`(key 65-128):1(key=100)

走 `computesizes`:`i=0, twotoi=1, a=1, arrayXhash(1, na=6)`:1<=18 真;`arrayXhash(1, 1)`:1<=3 真,`optimal=1, na=1`。`i=1, twotoi=2, a=2`, `arrayXhash(2,2)`:2<=6 真,`optimal=2, na=2`。`i=2, twotoi=4, a=3`, `arrayXhash(4,3)`:4<=9 真,`optimal=4, na=3`。`i=3, twotoi=8, a=5`, `arrayXhash(8,5)`:8<=15 真,`optimal=8, na=5`。`i=4, twotoi=16, a=5`, `nums[4]=0` 跳过更新。`i=5,6` 同理 a 不增。`i=7, twotoi=128, a=6`, `arrayXhash(128,6)`:128<=18 **假**,循环退出。

最终 `optimal=8, na=5`。也就是说数组部分定 8 个槽,装下 key `1,2,3,5,7`(这 5 个都在 `[1,8]` 内);key `100` 和字符串 `"x"` 进哈希。这个切分让数组部分利用率 5/8 > 50%,且没为了一个游离的 key=100 把数组撑到 128(那样浪费 122 个槽)。

**这正是 Lua "当数组用就快、当字典用也不慢"的实现根基**:它自动识别出"哪段整数键密集到值得用数组",其余的进哈希。程序员不用区分 `list` 和 `dict`,运行时根据实际数据形态动态切分。

### 2.5 rehash 的执行:luaH_resize

`computesizes` 给出新的 `asize` 和 `nsize` 后,`rehash` 调 `luaH_resize`(`ltable.c:716`)真正搬数据:

```c
void luaH_resize (lua_State *L, Table *t, unsigned newasize,
                                          unsigned nhsize) {
  Table newt;  /* to keep the new hash part */
  unsigned oldasize = t->asize;
  Value *newarray;
  if (newasize > MAXASIZE)
    luaG_runerror(L, "table overflow");
  newt.flags = 0;
  setnodevector(L, &newt, nhsize);
  if (newasize < oldasize) {  /* will array shrink? */
    exchangehashpart(t, &newt);
    reinsertOldSlice(t, oldasize, newasize);
    exchangehashpart(t, &newt);
  }
  newarray = resizearray(L, t, oldasize, newasize);
  if (l_unlikely(newarray == NULL && newasize > 0)) {
    freehash(L, &newt);
    luaM_error(L);
  }
  exchangehashpart(t, &newt);
  t->array = newarray;
  t->asize = newasize;
  if (newarray != NULL)
    *lenhint(t) = newasize / 2u;
  clearNewSlice(t, oldasize, newasize);
  reinserthash(L, &newt, t);
  freehash(L, &newt);
}
```

这个函数有个精巧的**两阶段分配 + 错误恢复**设计,注释(`ltable.c:702`)说得很清楚:哈希部分和数组部分两次分配都可能失败。如果第一次(哈希)失败,直接抛错;如果第二次(数组)失败,要把已经分配的哈希部分释放掉再抛错,让表回到原始状态——不能让表处于"哈希换了但数组没换"的半成品状态。

具体流程:

1. `setnodevector(L, &newt, nhsize)`:在临时 `Table newt` 里建好新的哈希部分(分配 Node 数组、清空、设置 `lsizenode`)。
2. 如果数组在**收缩**(`newasize < oldasize`),被砍掉的那段数组里的整数 key 要先搬进(临时挂在 t 上的)新哈希——`reinsertOldSlice`(`ltable.c:676`)。这里有个 `exchangehashpart` 的来回交换技巧:先把新哈希挂到 t 上(让 `insertkey` 能往里写),搬完再换回来,保证出错时 t 还是原样。
3. `resizearray` 分配新数组。
4. `exchangehashpart(t, &newt)`:把新哈希正式装到 t 上,旧哈希临时挂到 `newt`。
5. 设置新数组、新 asize、初始化 lenhint。
6. `clearNewSlice`:把数组扩张新增的槽位填上"空"标记(`LUA_VEMPTY`)。
7. `reinserthash(L, &newt, t)`:把旧哈希(`newt` 上)里所有还活着的 key 重新插进新表(t)。这里用 `newcheckedkey`(`ltable.c:902`),它假设一定能插成功(因为新表刚按需扩容过):

```c
static void newcheckedkey (Table *t, const TValue *key, TValue *value) {
  unsigned i = keyinarray(t, key);
  if (i > 0)
    obj2arr(t, i - 1, value);
  else {
    int done = insertkey(t, key, value);
    lua_assert(done);
    cast(void, done);
  }
}
```

注意这一步的"重新切分"发生了:旧哈希里的整数 key,如果新 asize 能容下它,这次就走 `obj2arr` 进数组;否则继续留在哈希。这就是"数组长大了把整数 key 从哈希搬进数组"的发生点。

8. `freehash(L, &newt)`:释放旧哈希。

`setnodevector`(`ltable.c:602`)里还有个 5.5 的细节值得说:

```c
static void setnodevector (lua_State *L, Table *t, unsigned size) {
  if (size == 0) {
    t->node = cast(Node *, dummynode);
    t->lsizenode = 0;
    setdummy(t);
  }
  else {
    int i;
    int lsize = luaO_ceillog2(size);
    if (lsize > MAXHBITS || (1 << lsize) > MAXHSIZE)
      luaG_runerror(L, "table overflow");
    size = twoto(lsize);
    if (lsize < LIMFORLAST)
      t->node = luaM_newvector(L, size, Node);
    else {
      size_t bsize = size * sizeof(Node) + sizeof(Limbox);
      char *node = luaM_newblock(L, bsize);
      t->node = cast(Node *, node + sizeof(Limbox));
      getlastfree(t) = gnode(t, size);
    }
    t->lsizenode = cast_byte(lsize);
    setnodummy(t);
    for (i = 0; i < cast_int(size); i++) {
      Node *n = gnode(t, i);
      gnext(n) = 0;
      setnilkey(n);
      setempty(gval(n));
    }
  }
}
```

请求的 size 会被向上取整到 2 的幂(`luaO_ceillog2(size)` 得到 `lsize`,然后 `size = twoto(lsize)`)。如果请求的 size 是 0,表用 `dummynode`(下一节)。如果 `lsize < 3`(即 size 是 1/2/4),直接分配 Node 数组,不附带 `lastfree`;否则多分配一个 `Limbox` 放在数组前面存 `lastfree`。

### 2.6 空表优化:dummynode

一个刚创建的表,哈希部分是空的。如果每次 `luaH_new` 都分配一个 Node,绝大多数小表会浪费这个分配。Lua 的解法是 `dummynode`(`ltable.c:130`):

```c
static const Node dummynode_ = {
  {{NULL}, LUA_VEMPTY,  /* value's value and type */
   LUA_TDEADKEY, 0, {NULL}}  /* key type, next, and key value */
};
```

一个全局共享的、只读的 `Node`,它的 key 是 `LUA_TDEADKEY`(任何合法 key 都不会和它 `equalkey` 成功),value 是空。`luaH_new`(`ltable.c:799`)创建表时调 `setnodevector(L, t, 0)`,size=0 走 `t->node = dummynode` 分支,`setdummy(t)` 在 `flags` 里置 `BITDUMMY` 位(`ltable.h:31`)。之后所有访问哈希的代码看到 `isdummy(t)` 为真,就知道这是个共享的 dummy,不会去改写它,也不会去释放它。

`isdummy`(`ltable.h:33`)、`setdummy`/`setnodummy`(`ltable.h:35-36`):

```c
#define BITDUMMY		(1 << 6)
#define NOTBITDUMMY		cast_byte(~BITDUMMY)
#define isdummy(t)		((t)->flags & BITDUMMY)
#define setnodummy(t)		((t)->flags &= NOTBITDUMMY)
#define setdummy(t)		((t)->flags |= BITDUMMY)
```

`BITDUMMY` 用了 `flags` 的第 6 位——前 5 位(0-4)被 `maskflags`(`ltm.h:54`,对应 TM_EQ 等元方法快速探测位)占用,第 7 位预留给 `isrealasize`(老字段,5.5 已无实际使用)。所以哈希相关的位塞在第 6 位。

`insertkey` 里那一行 `if (!isempty(gval(mp)) || isdummy(t))` 就是为 dummynode 准备的——dummy 的 mainposition 永远"被占"(虽然占的是个永远匹配不上的 DEADKEY),所以插入必然走冲突分支,必然调 `getfreepos` 返回 NULL(`isdummy` 表没有真正的 Node 数组),从而触发 rehash 分配真正的哈希部分。第一次往空表插非整数 key 时,就会从 dummy 升级为真 Node 数组。

`freehash`(`ltable.c:393`)也检查 `isdummy`,dummy 表不释放:

```c
static void freehash (lua_State *L, Table *t) {
  if (!isdummy(t)) {
    char *arr = cast_charp(t->node) - extraLastfree(t);
    luaM_freearray(L, arr, sizehash(t));
  }
}
```

### 2.7 数组部分的紧凑布局:tag array

这一节是 5.5 相对老资料**最大的变化之一**,讲 Table 必须讲。老资料(讲 5.3/5.4)描述 Lua 的数组部分是"`TValue` 数组"——每个元素是一个完整的 `TValue`(16 字节:8 字节 Value + 8 字节 tag,因为要和 `TValue` 对齐)。

**5.5 完全重写了数组部分的内存布局**。看 `ltable.h:95` 的注释和图:

```
             Values                              Tags
  --------------------------------------------------------
  ...  |   Value 1     |   Value 0     |unsigned|0|1|...
  --------------------------------------------------------
                                       ^ t->array
```

数组部分被拆成**两段**:左半是 Value 数组(每个 8 字节),右半是 tag 数组(每个 1 字节,`lu_byte`),中间夹一个 `unsigned`(用作 `lenhint`,即 `#t` 的提示)。`t->array` 指针指向两段中间。

访问宏(`ltable.h:113`、`ltable.h:116`):

```c
/* Computes the address of the tag for the abstract C-index 'k' */
#define getArrTag(t,k)	(cast(lu_byte*, (t)->array) + sizeof(unsigned) + (k))

/* Computes the address of the value for the abstract C-index 'k' */
#define getArrVal(t,k)	((t)->array - 1 - (k))
```

注意 Value 是**反向**索引(`array - 1 - k`,即 Value 0 在最靠近 `array` 指针的位置,Value 1 在它左边),tag 是**正向**索引。这种"反向 Value + 正向 tag,指针在中间"的布局有个好处:**两种访问都不需要存两个指针**——一个 `t->array` 指针同时服务两段,靠正负偏移区分。而中间的 `unsigned` 是本来就要存的 `lenhint`,塞在两段之间不浪费空间。

省了多少内存?假设数组大小 n:

- 老布局:`n * sizeof(TValue) = n * 16` 字节(64 位)。
- 5.5 新布局:`n * sizeof(Value) + n * sizeof(lu_byte) + sizeof(unsigned) = n * 8 + n * 1 + 4 ≈ 9n + 4` 字节。

省了约 44%。对一个大量用数组(`{1,2,3,...}`)的 Lua 程序,这是实打实的内存节省,正是"精简"主线在数据结构内部的落地。

`concretesize`(`ltable.c:544`)给出了精确的字节数:

```c
static size_t concretesize (unsigned int size) {
  if (size == 0)
    return 0;
  else
    return size * (sizeof(Value) + 1) + sizeof(unsigned);
}
```

`resizearray`(`ltable.c:563`)负责数组的扩缩容。它的注释解释了一个反直觉的选择——**不直接用 `realloc`,而是新分配 + memcpy + 释放旧块**:

```c
/* We could reallocate the array, but we still would need to move the
   elements to their new position, so the copy implicit in realloc is a
   waste. Moreover, most allocators will move the array anyway when the
   new size is double the old one (the most common case). */
```

因为布局是"反向 Value + 正向 tag",扩容时两段都要移位——`realloc` 那次隐式拷贝帮不上忙,反而多一次无用的拷贝。不如直接新分配、把两段各自拷到新位置、释放旧块。

注意 `np += newasize` 这一行(`ltable.c:579`)——新分配的 `np` 指针先指向块的开头,然后**前移 newasize 个 Value**,正好落在"两段中间"的位置,这就是 `t->array` 应该指向的地方。

### 2.8 DEADKEY:逻辑删除与 next 遍历

最后一个核心知识点。当 `t[k] = nil` 删除一个 key 时,Lua 不能立即从哈希表里物理抹掉那个 Node。原因:`next` 函数遍历表时,用户手里拿着的"当前 key"可能正好是被删的那个。如果物理删了,`next` 就找不到"当前 key 在哪个槽",也就没法继续往后遍历。

所以 Lua 把删除做成两步:

1. **逻辑删除**:把 value 置空(`setempty(gval(n))`),key 保留在 Node 里。此时这个 Node 在 `isempty(gval(n))` 判定下是"空"的(查询不会命中它),但 key 还在,`findindex`(`ltable.c:343`,`next` 用的)还能定位到它。
2. **物理回收**:等到下一次 rehash,`numusehash` 把所有"空 value 但非 nil key"的节点识别为 `deleted`,rehash 时这些节点的 key 不被搬进新表,自然消失。

但这里有个 GC 交互的陷阱:被逻辑删除的 key 如果是个 GC 对象(比如字符串),它在 Node 里还存着一个引用。如果直接把 key 字段清空,GC 就会回收那个对象——但 `next` 此时如果正好停在这个 key 上,它手里的 key 指针就悬空了。

Lua 的解法是 **DEADKEY**。当 GC 在某个时机决定真正释放被删 key 指向的对象时(典型是该对象除了这个死引用外已无其他引用),它不把 Node 的 key 字段清空,而是把 key 的 tag 改成 `LUA_TDEADKEY`(`lobject.h:814`):

```c
#define setdeadkey(node)	(keytt(node) = LUA_TDEADKEY)
#define keyisdead(node)		(keytt(node) == LUA_TDEADKEY)
```

DEADKEY 节点保留 key 原来的 `gcvalue`(指针值),但 tag 标成"死"。这样:

- 普通查询(`getgeneric` with `deadok=0`)永远不会匹配 DEADKEY 节点——`equalkey` 里 `rawtt(k1) != keytt(n2)` 直接返回不等。
- `next` 遍历(`findindex` 调 `getgeneric(..., deadok=1)`)允许 DEADKEY 匹配——`equalkey`(`ltable.c:252`)里:

```c
else if (deadok && keyisdead(n2) && iscollectable(k1)) {
  /* a collectable value can be equal to a dead key */
  return gcvalue(k1) == gcvalueraw(keyval(n2));
}
```

也就是说,`next` 拿用户给的 key(可能是 GC 已回收又重新分配到同一地址的对象,注释 `ltable.c:242` 解释了这个边界)和 DEADKEY 节点比指针。注释诚实地说明这可能产生假阳性("false positive"),但保证不会破坏什么——最坏情况是 `next` 返回另一个合法项或 nil。

这就是为什么 `ltable.c:527` 那行 `lua_assert(!keyisnil(n))` 成立:哈希部分里一个 value 为空的节点,它的 key 一定不是 nil(nil key 的节点根本不会被创建),要么是活的被删 key,要么是 DEADKEY。

---

## 三、为什么这样设计是 sound 的

把上面八节的设计理由收拢成几条不变式和权衡。

### 3.1 Brent 变体的不变式保证了最坏情况下的探测长度

开放寻址最怕的是"聚簇"(clustering)——一长串连续被占的槽位,任何落进这段的 key 都要顺延,链越来越长。线性探测(Linear Probing)有一阶聚簇问题,二次探测(Quadratic Probing)有二阶聚簇问题。

Lua 的链式散列表 + Brent 变体走了第三条路:**不靠探测序列找空位,而是显式建链**。链头永远是 main position 的合法占有者,链上每个元素要么哈希相同、要么是被挤过来的——但被挤过来的元素,挤它的那个一定在自己 main position。这个递归性质保证了:

- 查询任意 key,最多走"它 main position 那条链"的长度。
- 一条链的长度上界是该 main position 上哈希冲突的 key 数,加上被"借位"挤过来的 key 数。Brent 变体把"借位"减到最少——只有当新 key 的 main position 被一个"不在自己 main position 的 key"占着时,才发生借位移动;否则新 key 自己去空位。

文件头注释说 "even when the load factor reaches 100%, performance remains good"——这正是这套机制换来的:它不依赖"装填因子低"来保证性能,而是靠"main position 归位"维持短链。代价是 `insertkey` 比线性探测复杂(可能要改两个 `next` 字段、移动节点),但插入是一次性的、查询是高频的,这个权衡划算。

### 3.2 数组/哈希自动切分让"统一 Table"不付性能税

Lua 砍掉 `list` 和 `dict` 的区分,统一成 `Table`。这听起来会让"当数组用"或"当字典用"都慢——因为运行时不知道程序员意图。`computesizes` 算法就是这个担忧的反驳:

- 当数据形态是密集整数键(`{1,2,3,...,1000}`),`computesizes` 会算出 asize=1024,几乎所有 key 进数组,访问代价等同 C 数组下标——和专用 list 一样快。
- 当数据形态是稀疏字符串键(`{name=..., age=..., addr=...}`),`nums` 几乎全 0,`computesizes` 走到 `ct->na==0` 分支,数组保持原大小(通常 0),所有 key 进哈希——和专用 dict 一样。
- 混合形态(`{1, 2, "x"=...}`),整数进数组、字符串进哈希,各走各的快路径。

切分是**每次 rehash 重新算的**,所以一个表从"当数组用"逐渐变成"当字典用",切分会自适应调整。程序员完全无感。这正是"统一"不付性能税的实现根基——也是老资料经常讲不透的一点,很多人误以为 Lua 的统一 Table "肯定比专用结构慢",实际上对密集整数键它是 C 数组速度。

### 3.3 `arrayXhash` 的 3 倍门槛是经验性的内存权衡

`computesizes` 里"是否值得把某段整数键放进数组"的判断标准是 `arrayXhash(na, nh)`:数组槽位数 <= 哈希 Node 数 × 3。这个 3 来自 Node 是数组元素 3 倍大小的事实。

但这个判断是**关于内存的,不是关于时间的**。注释(`ltable.c:431`)明说 "(A hash node uses ~3 times more memory than an array entry)"。也就是说:即使把某些整数 key 放进数组会让访问略快,Lua 也只在"内存上更省"时才这么做。这是个"宁可慢一点访问、也要省内存"的取向——和 Lua "小"的基调一致。

### 3.4 rehash 是 O(n) 但摊还 O(1)

每次 rehash 要遍历全表(数组 + 哈希)统计、重新分配、重新插入所有 key,代价是 O(n)。但 rehash 的触发条件是"哈希 100% 满"(或数组需要扩容),而每次 rehash 至少把容量翻倍(`setnodevector` 向上取整到 2 的幂)。所以经过一次 rehash 后,下一次 rehash 之前至少还能再插入 n 个 key(把新表填满)。

把 O(n) 的 rehash 代价摊到 n 次插入上,每次插入的摊还代价是 O(n)/n = O(1)。这就是"动态数组/哈希表摊还 O(1)"的经典论证,Lua 也遵循。

一个值得指出的细节:`rehash` 里 `if (ct.deleted) nsize += nsize >> 2;`——如果检测到有删除痕迹,给哈希多留 25% 余量。这是针对"反复插入-删除-插入"工作负载的反振荡措施:不留余量的话,每次插入到满就 rehash,删除几个又恰好不够下次插入,会频繁 rehash。25% 余量让删除后再插入能命中空槽,不必每次都扩容。

### 3.5 DEADKEY 不破坏三色不变式

DEADKEY 的存在让 GC 的处理稍微复杂:`numusehash` 要能把 DEADKEY 节点识别为 deleted(它们的 value 是空的);rehash 时不把它们搬进新表,自然消失。但 rehash 发生在用户线程(GC 之外),rehash 时所有 DEADKEY 节点的 key 指针还指向(可能已被回收的)GC 对象——这没问题,因为 rehash 不解引用 key 内容,只看 tag。

从三色 GC 的角度(P5-16 详讲),一个 Table 在 rehash 时被置为 barrierback(`luaH_newkey` 里的 `luaC_barrierback`),通知 GC "这个表变了,重新扫"。DEADKEY 节点在 GC 扫描 Table 时会被识别为"不保持引用"——它们的 tag 是 `LUA_TDEADKEY`,GC 不会顺着这个 tag 去标记 key 指向的对象。所以 DEADKEY 不会让本该回收的对象活下来,三色不变式不破。

### 3.6 dummy + Limbox + tag array:三处"按需分配"

把三处节省内存的设计放一起看,会发现它们遵循同一个原则——**按需分配,不为空状态付费**:

- **dummynode**:空哈希表不分配 Node 数组,共享一个全局 dummy。第一次插入才升级。
- **Limbox / lastfree**:小哈希表(size < 8)不存 lastfree 字段,直接线性扫描。只有大表才为这个优化付一个 Limbox 的内存。
- **tag array**:数组部分不为每个元素的 tag 付 8 字节对齐代价,拆成 1 字节 tag 数组。空数组(asize=0)连 `unsigned` 都不分配(`concretesize(0)=0`)。

这三处共同体现"精简"——不为可能用到、但当前没用到的状态预留内存。这是 Lua 能塞进嵌入式设备、内存占用低的微观根基。

---

## 四、★对照 CPython + 回扣主线

把 Lua 的 Table 哈希部分和 CPython 的 `dict` 摆在一起,能清楚看到两种哲学。

| 维度 | Lua 5.5 Table 哈希部分 | CPython dict |
|---|---|---|
| **复合结构** | 数组 + 哈希同在一个 `Table`,自动切分 | 只有哈希(`list` 是另一个独立类型) |
| **冲突策略** | 开放寻址 + 链式散列表(`Node.next` 偏移链)+ Brent 变体 | 开放寻址 + 伪随机探测(perturbation probe) |
| **装填阈值** | 容忍到 100% 才 rehash | 2/3 满就 rehash(`dk_usable` 耗尽) |
| **删除处理** | 逻辑删除(value 置空)+ DEADKEY(下次 rehash 回收) | 墓碑 tombstone(dummy marker,下次 rehash 回收) |
| **布局** | `Node` 数组(每 Node ~24B) | 紧凑双数组:稀疏 index 数组 + 稠密 entries 数组(Hettinger,3.6+) |
| **哈希随机化** | 全局 seed 混入字符串哈希 | 每进程随机化 + `PYTHONHASHSEED` |
| **大小** | 总是 2 的幂,`& (size-1)` 取模(对 `hashpow2`) | 总是 2 的幂,初始 8 |

逐条看取舍:

**冲突策略**:Lua 用"显式建链 + Brent 变体",CPython 用"伪随机探测"。伪随机探测的好处是无需额外字段(链偏移),探测序列由哈希值本身派生;坏处是探测序列更长(每个被占槽位都要算下一步)。Lua 的链式散列表用 `Node.next` 4 字节换来了"链上每一步是 O(1) 跳转",且 Brent 变体保证链短。两者在装填因子低时性能接近;Lua 的优势是能容忍高装填因子(100% 仍可用),CPython 必须在 2/3 就扩容。

**装填阈值**:这反映了不同的内存取向。CPython 在 2/3 扩容,意味着 dict 永远有 1/3 的空槽——这是用空间换查询时间(探测序列短)。Lua 容忍到 100%,意味着同样数据量下 Lua 的哈希表更紧凑(空槽少)——这是用 Brent 变体的复杂逻辑换内存节省。两者的取舍和各自的主基调一致:CPython 不惜内存换速度,Lua 不惜代码复杂度换内存。

**删除处理**:Lua 的 DEADKEY 和 CPython 的 tombstone 本质相同——都是"标记删除,等下次 rehash 物理回收",都是为了让遍历器(`next` / Python 的 dict 迭代器)能找到被删 key 的位置。区别在于 Lua 的 DEADKEY 是个独立的 tag(`LUA_TDEADKEY`),和 key 的 GC 状态解耦;CPython 的 tombstone 是 entry 的一个状态。Lua 的设计额外考虑了"死 key 的 GC 对象可能被回收导致指针复用"的边界(`equalkey` 的 false positive 注释),这是个更细致的处理。

**布局**:5.5 的 Lua 用 tag array 拆分数组部分(9n 字节 vs 老 16n 字节);CPython 用 Hettinger 的紧凑布局(稀疏 index + 稠密 entries)把 dict 从 ~24n 字节压到 ~12n 字节(只存一份稀疏索引)。两者思路相似——都是"把 tag/index 和 value 分开存,避免 padding 浪费"。Lua 的 tag 是 1 字节(因为 Lua 的类型 tag 本来就 1 字节),CPython 的 index 是 1/2/4/8 字节(按 dict 大小选)。Lua 的数组部分天然紧凑,CPython 的 dict 是把原本稀疏的哈希表强行紧凑化。

**哈希随机化**:两者都做了,都是对抗哈希碰撞攻击。Lua 混在字符串哈希里(seed 是全局的),CPython 每进程随机化且可通过 `PYTHONHASHSEED` 关闭(方便某些场景的可重现性)。

把对照收拢成一句话:**Lua 的 Table 哈希部分用更复杂的探测逻辑(Brent)和更紧凑的布局(tag array),在"内存省"和"高装填因子可用"之间取了和 CPython 不同的平衡点——这和 Lua 整体"小而快"的主线一致。**

---

## 五、本章小结与主线回扣

这一章把 Table 的哈希部分从"查询"到"插入"到"扩容切分"到"删除"完整走了一遍。核心是三个机制:

1. **链式散列表 + Brent 变体**:用 4 字节的 `Node.next` 偏移换"main position 归位 + 短链",让装填因子到 100% 仍可用——这是"精简"在探测策略上的体现。
2. **数组/哈希自动切分**(`computesizes`):用 `2^k` 区间统计 + `arrayXhash` 3 倍内存权衡,动态决定哪些整数键进数组、哪些进哈希——这是"统一 Table"不付性能税的实现根基。
3. **按需分配的内存布局**(dummynode / Limbox / tag array):空表不分配、小表不存 lastfree、数组拆 tag 数组——三处都遵循"不为空状态付费",把"小"落到字节级。

这三处合起来,正是 Lua 主线"统一与精简换小而快"在 Table 内部的完整落地。一个 `Table` 既是数组又是哈希,既快(整数键 C 数组速度、哈希键 Brent 短链)又省(紧凑布局、按需分配、容忍高装填),还能在数据形态变化时自动重新切分。没有这套设计,"一切复合数据都是 Table"就会变成"一切复合数据都慢"——是哈希探测和 rehash 这套算法,让统一真正可行。

至此,P1 数据根基闭环。从 `TValue`(P1-02)到字符串(P1-03)到 Table 两半(P1-04、P1-05),Lua 运行时的"值"层面已经讲透。接下来的 P2 转入编译侧——这些值在源码里是怎么被识别、解析、生成出来的。

---

*下一章 [P2-06 词法分析 llex:源码到 token](P2-06-词法分析llex-源码到token.md):从数据根基转入编译侧,看 Lua 的词法分析器怎么把字符流变成 token。*
