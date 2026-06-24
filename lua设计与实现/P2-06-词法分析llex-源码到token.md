# P2-06 词法分析 llex：源码到 token

> **本书主线**：统一与精简换小而快。**二分法**：编译侧(源码→字节码)↔ 执行侧(VM 执行 + 管值生命周期)。**★对照**:CPython。**源码**:lua-5.5.0。**基调**：纯直球，不用比喻。
>
> **本章在二分法里的位置**：编译侧(P2)的入口。源码是字节流，语法分析器只认 token。这中间的切分，由 `llex.c` 完成。本章讲清楚 Lua 怎么用一张字符分类查找表 + 一个单字符大 switch + 一个 zio 输入抽象，把字符流切成 token 流，而且整个词法器不到六百行 C。

---

## 一、这章解决什么问题

编译器的输入，在物理上就是一段连续的字节。`luaL_loadfilex` 把文件按 `lua_Reader` 回调一块一块读进来(`lauxlib.c:808`)，最终汇成一条字节流。但语法分析器(P2-07)的工作单位不是字节，是 **token**——`local`、`a`、`=`、`1`、`end` 这种语义单元。一个字节流 `local a = 1\n`，语法分析器要拿到的是五个 token:`TK_LOCAL`、`TK_NAME("a")`、`'='`、`TK_INT(1)`、`TK_EOS`。中间这一步切分，就是词法分析。

这一步要做对、做快、还要做得小。Lua 的约束很硬：词法器是嵌入式的代码，它和宿主共享同一个进程的指令缓存，必须紧凑；它被语法分析器每读一个 token 就调用一次，是热点路径，必须快；它不能依赖任何重型库(连标准 `<ctype.h>` 都要尽量绕开)，因为宿主可能跑在奇怪的 locale 下。

这一章回答三个具体问题：

1. 一个 token 在 C 里怎么表示，才既能装下"一个字符"、又能装下"一整类保留字"、还能挂载语义值(数字、字符串)?
2. 主循环怎么写，才能用一个函数同时识别注释、长字符串、数字、操作符、标识符这十几类输入，而且分支尽量廉价？
3. 输入源(文件、内存、字符串)千差万别，词法器怎么做到对输入源完全无知？

Lua 的回答浓缩成三件东西，也是本章要逐函数讲透的：**一张字符分类查找表 `luai_ctype_[]`**、**一个对当前字符做 `switch` 的主循环 `llex`**、**一个把任意输入抽象成字符流的 `ZIO`**。三件东西加起来，体现的就是本书主线在编译侧的具体含义——用更少的机制，把字符流到 token 流这一步走得既对又快又小。

先看 token 的表示。

---

## 二、源码怎么实现

### 2.1 Token:`int` 一把抓

Lua 的 token 在内存里就是一个 `int`。`llex.h:56`:

```c
typedef struct Token {
  int token;
  SemInfo seminfo;
} Token;
```

关键是 `token` 字段的编码规则。单字符 token(如 `'='`、`'+'`、`'{'`)**就用字符本身的 ASCII 码**:`'='` 的 token 值就是 61。多字符 token(保留字、`==`、`...`、`<=` 这类)用一个单独的编码区间，从 `FIRST_RESERVED` 开始(`llex.h:20`):

```c
#define FIRST_RESERVED	(UCHAR_MAX + 1)
```

`UCHAR_MAX` 是 255，所以 `FIRST_RESERVED = 256`。这就保证：任何单字符 token 的值(0–255)和任何多字符 token 的值(256 及以上)**永远不会撞**。语法分析器拿到一个 token，只要 `token < FIRST_RESERVED`，就能直接当字符用；否则查保留字表。一个判断、一个分支，没有哈希、没有字符串比较。

多字符 token 的清单在 `llex.h:32` 的 `enum RESERVED`:

```c
enum RESERVED {
  /* terminal symbols denoted by reserved words */
  TK_AND = FIRST_RESERVED, TK_BREAK,
  TK_DO, TK_ELSE, TK_ELSEIF, TK_END, TK_FALSE, TK_FOR, TK_FUNCTION,
  TK_GLOBAL, TK_GOTO, TK_IF, TK_IN, TK_LOCAL, TK_NIL, TK_NOT, TK_OR,
  TK_REPEAT, TK_RETURN, TK_THEN, TK_TRUE, TK_UNTIL, TK_WHILE,
  /* other terminal symbols */
  TK_IDIV, TK_CONCAT, TK_DOTS, TK_EQ, TK_GE, TK_LE, TK_NE,
  TK_SHL, TK_SHR,
  TK_DBCOLON, TK_EOS,
  TK_FLT, TK_INT, TK_NAME, TK_STRING
};
```

枚举按 `FIRST_RESERVED` 起步，依次递增。前 23 个是保留字(`and` 到 `while`),`NUM_RESERVED` 宏算出这个数(`llex.h:46`):

```c
#define NUM_RESERVED	(cast_int(TK_WHILE-FIRST_RESERVED + 1))
```

`TK_WHILE - FIRST_RESERVED + 1 = 23`。保留字之后是操作符(`TK_IDIV` `//`、`TK_CONCAT` `..`、`TK_DOTS` `...`、`TK_EQ` `==`、`TK_GE` `>=`、`TK_LE` `<=`、`TK_NE` `~=`、`TK_SHL` `<<`、`TK_SHR` `>>`、`TK_DBCOLON` `::`)，再后是结构性 token(`TK_EOS` 文件结束)和带语义值的 token(`TK_FLT` 浮点、`TK_INT` 整数、`TK_NAME` 名字、`TK_STRING` 字符串)。

**一个 5.5 vs 老资料的重要差异**:`TK_GLOBAL` 是 lua-5.5.0 新增的保留字。讲 5.3/5.4 的资料里，保留字表到 `TK_GOTO` 之前只有 22 个，没有 `TK_GLOBAL`。5.5 引入了显式的 `global` 语法(对应 `lparser.c:2091` 的 `globalstatfunc`)，用来显式声明全局变量。但它受 `LUA_COMPAT_GLOBAL` 兼容开关控制——`luaconf.h:342` 默认 `#define LUA_COMPAT_GLOBAL`，此时 `global` **不**算保留字，可以当普通名字用；只有宿主显式 `#undef LUA_COMPAT_GLOBAL` 重新编译 Lua,`global` 才是保留字。这个开关怎么落到词法器里，下一小节看 `luaX_setinput`。

带语义值的 token，值就装在 `SemInfo` 里(`llex.h:49`):

```c
typedef union {
  lua_Number r;
  lua_Integer i;
  TString *ts;
} SemInfo;  /* semantics information */
```

`union` 三选一：`TK_INT` 用 `i`、`TK_FLT` 用 `r`、`TK_NAME`/`TK_STRING` 用 `ts`(指向一个 `TString`)。`union` 意味着同一个 `Token` 结构只占一份语义值空间，不浪费。一个 token 是 int 还是 float，不是词法器在文本里数有没有小数点决定的——是交给 `luaO_str2num` 转换后看结果类型决定的(2.5 节)。

### 2.2 LexState：词法器的全部状态

词法器的所有状态装在一个 `LexState` 里(`llex.h:64`):

```c
typedef struct LexState {
  int current;  /* current character (charint) */
  int linenumber;  /* input line counter */
  int lastline;  /* line of last token 'consumed' */
  Token t;  /* current token */
  Token lookahead;  /* look ahead token */
  struct FuncState *fs;  /* current function (parser) */
  struct lua_State *L;
  ZIO *z;  /* input stream */
  Mbuffer *buff;  /* buffer for tokens */
  Table *h;  /* to avoid collection/reuse strings */
  struct Dyndata *dyd;  /* dynamic structures used by the parser */
  TString *source;  /* current source name */
  TString *envn;  /* environment variable name */
  TString *brkn;  /* "break" name (used as a label) */
  TString *glbn;  /* "global" name (when not a reserved word) */
} LexState;
```

逐字段看它为什么要这些。

`current` 是当前字符。词法器是一个字符一个字符往前推进的，`current` 就是游标所指的那个字符。它用 `int` 而不是 `char`，因为要能表示 `EOZ = -1`(`lzio.h:16`，输入结束)。

`linenumber`/`lastline` 是行号。Lua 的错误信息要带行号(`malformed number near '0x'` 后面跟 `line 3`)，所以词法器必须边读边数行。`linenumber` 是当前读到第几行，`lastline` 是上一个被消费掉的 token 所在的行——两者区别在于 lookahead 的存在：当语法分析器还在处理上一个 token 时，词法器可能已经预读到了下一行，`linenumber` 已经前进了，但上一个 token 的行号得记在 `lastline` 里。

`t` 是当前 token,`lookahead` 是预读 token。Lua 的语法分析器只需要**至多一个 token 的 lookahead**(2.7 节会讲为什么)，所以这里就一个 `Token lookahead`，不是数组。

`fs` 指向语法分析器的当前函数状态(P2-07 详讲),`L` 是 VM 状态。词法器需要 `L` 是因为读字符串、查保留字要创建 `TString` 对象，这些都要经过 `L` 的内存分配和 GC。

`z` 是输入流，`ZIO` 类型(2.6 节)。`buff` 是 token 缓冲区，`Mbuffer` 类型——读标识符、数字、字符串时，字符先一个个塞进这个缓冲，等整个 token 读完了再一次性处理。`h` 是词法器私有的一个 `Table`，用来锚定读到的字符串，防止它们在编译结束前被 GC 回收掉(2.8 节的 `anchorstr`)。

`dyd` 是语法分析器的动态数据(活动变量表、goto 标签表)，词法器只是捎带持有。

最后三个 `TString *` 是 5.5 新增的优化。`envn` 是 `_ENV`(环境 upvalue 的名字),`brkn` 是 `"break"`(break 在 5.4 引入了可以带标签的语法，内部当成一个隐式 label 处理),`glbn` 是 `"global"`(配合 `LUA_COMPAT_GLOBAL`)。这三个字符串在编译过程中会被反复比较，预先 `luaS_newliteral` 出来缓存住，后面比较就是指针比较(`ts == ls->envn`)，不用字符串内容比较。

`luaX_setinput`(`llex.c:176`)初始化整个 `LexState`:

```c
void luaX_setinput (lua_State *L, LexState *ls, ZIO *z, TString *source,
                    int firstchar) {
  ls->t.token = 0;
  ls->L = L;
  ls->current = firstchar;
  ls->lookahead.token = TK_EOS;  /* no look-ahead token */
  ls->z = z;
  ls->fs = NULL;
  ls->linenumber = 1;
  ls->lastline = 1;
  ls->source = source;
  /* all three strings here ("_ENV", "break", "global") were fixed,
     so they cannot be collected */
  ls->envn = luaS_newliteral(L, LUA_ENV);  /* get env string */
  ls->brkn = luaS_newliteral(L, "break");  /* get "break" string */
#if defined(LUA_COMPAT_GLOBAL)
  /* compatibility mode: "global" is not a reserved word */
  ls->glbn = luaS_newliteral(L, "global");  /* get "global" string */
  ls->glbn->extra = 0;  /* mark it as not reserved */
#endif
  luaZ_resizebuffer(ls->L, ls->buff, LUA_MINBUFFER);  /* initialize buffer */
}
```

注意 `firstchar` 参数。Lua 在喂给词法器之前，先由 `lauxlib.c` 做了两件事：`skipBOM`(`lauxlib.c:779`)跳过 UTF-8 BOM,`skipcomment`(`lauxlib.c:795`)跳过 Unix shebang(`#!/usr/bin/lua` 那一行，因为 Lua 脚本常作为可执行文件)。跳完之后剩下的第一个字符，才作为 `firstchar` 传进来。所以词法器一进来，`ls->current` 已经是源码真正的第一个字符，不用自己处理 BOM 和 shebang。这个分工很关键：词法器只管 Lua 语法，BOM/shebang 这种"加载层"的事归 `lauxlib`。

注释里那句 `ls->glbn->extra = 0` 就是 `LUA_COMPAT_GLOBAL` 的落地点：`global` 这个 `TString` 的 `extra` 字段被清零，后面词法器用 `isreserved` 判断(2.4 节)时，`global` 就不会被当成保留字，而是当普通名字返回 `TK_NAME`。

### 2.3 字符分类：一张查找表 lctype

在讲主循环之前，先看一个底层基建。词法器要频繁判断当前字符"是不是字母"、"是不是数字"、"是不是空白"、"是不是十六进制数字"。最朴素的写法是调 `<ctype.h>` 的 `isalpha`/`isdigit`/`isspace`。Lua 不这么做，原因有二：一是 `ctype.h` 受 locale 影响(土耳其语 locale 下 `'I'` 的小写不是 `'i'`)，会破坏词法器的确定性；二是函数调用有开销。

Lua 自己造了一张 256+1 项的查找表，在 `lctype.c:28`:

```c
LUAI_DDEF const lu_byte luai_ctype_[UCHAR_MAX + 2] = {
  0x00,  /* EOZ */
  0x00,  0x00,  0x00,  0x00,  0x00,  0x00,  0x00,  0x00,	/* 0. */
  0x00,  0x08,  0x08,  0x08,  0x08,  0x08,  0x00,  0x00,
  /* ... 省略中间若干行 ... */
  0x04,  0x15,  0x15,  0x15,  0x15,  0x15,  0x15,  0x05,	/* 4. */  (A-F)
  0x05,  0x05,  0x05,  0x05,  0x05,  0x05,  0x05,  0x05,
  /* ... */
};
```

表长 `UCHAR_MAX + 2 = 257`。为什么多一项？为了让下标 -1(也就是 `EOZ`)也合法。访问宏在 `lctype.h:52`:

```c
#define testprop(c,p)	(luai_ctype_[(c)+1] & (p))
```

`(c)+1` 把 `EOZ = -1` 映射到下标 0，把字符 0–255 映射到下标 1–256。这样不管 `current` 是 `EOZ` 还是真实字符，一次数组访问就能判断属性，不需要特判。

每个字节是一个位掩码，5 个 bit 各代表一种属性(`lctype.h:39`):

```c
#define ALPHABIT	0
#define DIGITBIT	1
#define PRINTBIT	2
#define SPACEBIT	3
#define XDIGITBIT	4

#define MASK(B)		(1 << (B))
```

`ALPHABIT`(bit 0)是字母，`DIGITBIT`(bit 1)是数字，`PRINTBIT`(bit 2)是可打印，`SPACEBIT`(bit 3)是空白，`XDIGITBIT`(bit 4)是十六进制数字。

回头看表的值。`'0'`(ASCII 0x30)那一行是 `0x16`，二进制 `0001 0110`，即 DIGIT(0x02)+ PRINT(0x04)+ XDIGIT(0x10)——`'0'` 既是数字又是可打印又是十六进制数字，完全正确。`'A'`(ASCII 0x41)是 `0x15`,`0001 0101` = ALPHA(0x01)+ PRINT(0x04)+ XDIGIT(0x10)——字母、可打印、十六进制数字。`' '`(空格，0x20)是 `0x0c`,`0000 1100` = PRINT(0x04)+ SPACE(0x08)。`'\t'`(0x09)是 `0x08`，只 SPACE。

判定宏(`lctype.h:57`)是对 `testprop` 的封装：

```c
#define lislalpha(c)	testprop(c, MASK(ALPHABIT))
#define lislalnum(c)	testprop(c, (MASK(ALPHABIT) | MASK(DIGITBIT)))
#define lisdigit(c)	testprop(c, MASK(DIGITBIT))
#define lisspace(c)	testprop(c, MASK(SPACEBIT))
#define lisprint(c)	testprop(c, MASK(PRINTBIT))
#define lisxdigit(c)	testprop(c, MASK(XDIGITBIT))
```

注意名字里的 `l`——`lislalpha` 是 "Lua is alpha"，和 `<ctype.h>` 的 `isalpha` **不同**:Lua 的字母**包括下划线 `_`**(看表里 `_` 是 0x5F 那一行，值 `0x05` = ALPHA + PRINT)。Lua 的标识符允许下划线开头，所以下划线被并入 alpha 类。这是为 Lua 语法量身定做的，标准 `ctype.h` 做不到。

`ltolower`(`lctype.h:71`)走的是位运算，不是查表：

```c
#define ltolower(c)  \
  check_exp(('A' <= (c) && (c) <= 'Z') || (c) == ((c) | ('A' ^ 'a')),  \
            (c) | ('A' ^ 'a'))
```

`'A' ^ 'a'` 在 ASCII 里是 0x20。任何大写字母 `| 0x20` 就变成对应小写字母。`check_exp` 是个只在调试期生效的断言，确保这个变换对非大写字母是无害的(小写字母和 `'.'` `| 0x20` 不变)。注释说这个 `ltolower` 对字母和 `'.'` 都正确，而 Lua 词法器正好只需要这两类——读十六进制数字时大小写都要认(`0x1A` 和 `0x1a` 等价)，读数字时 `e`/`E` 指数也要大小写都认。

这套查找表的开关在 `lctype.h:19`:

```c
#if !defined(LUA_USE_CTYPE)

#if 'A' == 65 && '0' == 48
/* ASCII case: can use its own tables; faster and fixed */
#define LUA_USE_CTYPE	0
#else
/* must use standard C ctype */
#define LUA_USE_CTYPE	1
#endif

#endif
```

只有当字符编码不是 ASCII(EBCDIC 之类)时才回退到 `<ctype.h>`(`lctype.h:80` 那一支)。现代平台全是 ASCII,Lua 一律走自己的查找表。**5.5 vs 老资料差异**：讲 5.3/5.4 的资料说"Lua 默认用内置 ctype 表"，这点 5.5 没变，机制完全一致；但 5.5 在表里多预留了高位字节的处理逻辑(`NONA` 宏，默认 0,`LUA_UCID` 编译选项打开后所有非 ASCII 字节都算字母，用于支持 Unicode 标识符)。

这张表换来了什么？**每次字符判定是一次数组读 + 一次按位与，无函数调用，无 locale 依赖，O(1)**。词法器主循环里到处是 `lislalpha`/`lisdigit`/`lisspace`，这套宏是热点路径上的关键。

### 2.4 主循环 llex：一个 switch 走完所有情况

主循环在 `llex.c:467`。这是全章的核心，逐段拆。

```c
static int llex (LexState *ls, SemInfo *seminfo) {
  luaZ_resetbuffer(ls->buff);
  for (;;) {
    switch (ls->current) {
      case '\n': case '\r': {  /* line breaks */
        inclinenumber(ls);
        break;
      }
      case ' ': case '\f': case '\t': case '\v': {  /* spaces */
        next(ls);
        break;
      }
      ...
    }
  }
}
```

外层是 `for(;;)`，因为空白和注释不产生 token，要跳过，只有真正读到一个 token 才 `return`。每次进来先 `luaZ_resetbuffer` 把缓冲清空，准备装新 token 的字符。

`switch` 按当前字符分派。先处理"不产生 token 的情况"：换行和空白。换行走 `inclinenumber`(`llex.c:165`):

```c
static void inclinenumber (LexState *ls) {
  int old = ls->current;
  lua_assert(currIsNewline(ls));
  next(ls);  /* skip '\n' or '\r' */
  if (currIsNewline(ls) && ls->current != old)
    next(ls);  /* skip '\n\r' or '\r\n' */
  if (++ls->linenumber >= INT_MAX)
    lexerror(ls, "chunk has too many lines", 0);
}
```

跨平台换行(Unix `\n`、Mac `\r`、DOS `\r\n`)都正确处理：读完一个换行符，如果下一个还是不同的换行符，就一起吃掉，行号只加一。行号溢出 `INT_MAX` 直接报错。

空白就 `next(ls)` 吃掉。`next` 宏(`llex.c:32`):

```c
#define next(ls)	(ls->current = zgetc(ls->z))
```

`zgetc` 是从输入流取一个字符(2.6 节)。

接着看注释分支(`llex.c:479`):

```c
case '-': {  /* '-' or '--' (comment) */
  next(ls);
  if (ls->current != '-') return '-';
  /* else is a comment */
  next(ls);
  if (ls->current == '[') {  /* long comment? */
    size_t sep = skip_sep(ls);
    luaZ_resetbuffer(ls->buff);  /* 'skip_sep' may dirty the buffer */
    if (sep >= 2) {
      read_long_string(ls, NULL, sep);  /* skip long comment */
      luaZ_resetbuffer(ls->buff);  /* previous call may dirty the buff. */
      break;
    }
  }
  /* else short comment */
  while (!currIsNewline(ls) && ls->current != EOZ)
    next(ls);  /* skip until end of line (or end of file) */
  break;
}
```

`-` 单独出现就返回 `'-'` token(减号)。`--` 是注释：先看是不是 `--[[...]]` 长注释，如果是就走 `read_long_string(ls, NULL, sep)`(传 `NULL` 表示这是注释，不要语义值，只跳过)；否则是短注释，吃到行尾或文件尾。短注释不跨行，这是 Lua 的硬规则。

`skip_sep`(`llex.c:282`)读 `[=*[` 或 `]=*]` 序列，返回等号数加 2:

```c
static size_t skip_sep (LexState *ls) {
  size_t count = 0;
  int s = ls->current;
  lua_assert(s == '[' || s == ']');
  save_and_next(ls);
  while (ls->current == '=') {
    save_and_next(ls);
    count++;
  }
  return (ls->current == s) ? count + 2
         : (count == 0) ? 1
         : 0;
}
```

返回值含义：第二方括号匹配且 `count` 个等号，返回 `count + 2`(≥2)；只有单个 `[` 没有 `=`，返回 1;`[==...` 但第二个括号不匹配，返回 0(语法错误)。`count + 2` 这个值后面用来匹配结尾的 `]=*]`，等号数必须一致——`[[...]]`、`[=[...]=]`、`[==[...]==]` 都是合法的长字符串/长注释，各自独立。

长字符串处理 `read_long_string`(`llex.c:297`):

```c
static void read_long_string (LexState *ls, SemInfo *seminfo, size_t sep) {
  int line = ls->linenumber;  /* initial line (for error message) */
  save_and_next(ls);  /* skip 2nd '[' */
  if (currIsNewline(ls))  /* string starts with a newline? */
    inclinenumber(ls);  /* skip it */
  for (;;) {
    switch (ls->current) {
      case EOZ: {  /* error */
        const char *what = (seminfo ? "string" : "comment");
        const char *msg = luaO_pushfstring(ls->L,
                     "unfinished long %s (starting at line %d)", what, line);
        lexerror(ls, msg, TK_EOS);
        break;  /* to avoid warnings */
      }
      case ']': {
        if (skip_sep(ls) == sep) {
          save_and_next(ls);  /* skip 2nd ']' */
          goto endloop;
        }
        break;
      }
      case '\n': case '\r': {
        save(ls, '\n');
        inclinenumber(ls);
        if (!seminfo) luaZ_resetbuffer(ls->buff);  /* avoid wasting space */
        break;
      }
      default: {
        if (seminfo) save_and_next(ls);
        else next(ls);
      }
    }
  } endloop:
  if (seminfo)
    seminfo->ts = luaX_newstring(ls, luaZ_buffer(ls->buff) + sep,
                                     luaZ_bufflen(ls->buff) - 2 * sep);
}
```

注意三件事：

第一，**首字符是换行就跳过**(`if (currIsNewline(ls)) inclinenumber(ls)`)。这是 Lua 的语法：`[[\nhello]]` 和 `[[hello]]` 等价，开头的换行不计入字符串内容。

第二，**注释模式(`seminfo == NULL`)不存字符**，而且每遇到换行还 `luaZ_resetbuffer` 清空缓冲，避免长注释白占内存。字符串模式才把每个字符 `save_and_next` 进缓冲。

第三，字符串内容是从缓冲的 `sep` 偏移开始，长度减去 `2 * sep`——去掉两边的 `[=*[ ` 和 `]=*]`。`luaX_newstring` 把它变成 `TString` 挂到 `seminfo->ts`。

接下来是 `=`/`<`/`>`/`/`/`~`/`:` 这些"可能是单字符也可能是双字符"的操作符(`llex.c:508`):

```c
case '=': {
  next(ls);
  if (check_next1(ls, '=')) return TK_EQ;  /* '==' */
  else return '=';
}
case '<': {
  next(ls);
  if (check_next1(ls, '=')) return TK_LE;  /* '<=' */
  else if (check_next1(ls, '<')) return TK_SHL;  /* '<<' */
  else return '<';
}
case '>': {
  next(ls);
  if (check_next1(ls, '=')) return TK_GE;  /* '>=' */
  else if (check_next1(ls, '>')) return TK_SHR;  /* '>>' */
  else return '>';
}
case '/': {
  next(ls);
  if (check_next1(ls, '/')) return TK_IDIV;  /* '//' */
  else return '/';
}
case '~': {
  next(ls);
  if (check_next1(ls, '=')) return TK_NE;  /* '~=' */
  else return '~';
}
case ':': {
  next(ls);
  if (check_next1(ls, ':')) return TK_DBCOLON;  /* '::' */
  else return ':';
}
```

模式都一样：先吃掉第一个字符，再看当前字符是不是第二个字符，是就返回组合 token，不是就返回单字符 token。`check_next1`(`llex.c:208`)极简：

```c
static int check_next1 (LexState *ls, int c) {
  if (ls->current == c) {
    next(ls);
    return 1;
  }
  else return 0;
}
```

这里没有回溯的复杂度——因为单字符 token 和双字符 token 的第一个字符都已经吃掉了，差异只在第二个字符。这是确定性词法的典型：不需要正则、不需要 NFA，一个 switch 加 lookahead 一字符就够。

字符串字面量(`llex.c:540`)走 `read_string`:

```c
case '"': case '\'': {  /* short literal strings */
  read_string(ls, ls->current, seminfo);
  return TK_STRING;
}
```

`read_string`(`llex.c:404`)处理转义。先看主干：

```c
static void read_string (LexState *ls, int del, SemInfo *seminfo) {
  save_and_next(ls);  /* keep delimiter (for error messages) */
  while (ls->current != del) {
    switch (ls->current) {
      case EOZ:
        lexerror(ls, "unfinished string", TK_EOS);
        break;
      case '\n':
      case '\r':
        lexerror(ls, "unfinished string", TK_STRING);
        break;
      case '\\': {  /* escape sequences */
        ...
      }
      default:
        save_and_next(ls);
    }
  }
  save_and_next(ls);  /* skip delimiter */
  seminfo->ts = luaX_newstring(ls, luaZ_buffer(ls->buff) + 1,
                                   luaZ_bufflen(ls->buff) - 2);
}
```

短字符串不能跨行(遇到裸 `\n`/`\r` 报 `unfinished string`)，不能到 `EOZ`。开头先把定界符存进缓冲(`save_and_next`)，目的是错误信息里能显示这个 token；最后取出内容时 `+1` 跳过开头的定界符，长度 `-2` 去掉首尾两个定界符。

转义序列是 `case '\\'` 里的一坨。简化的：`\a\b\f\n\r\t\v` 直接映射成对应 ASCII(`llex.c:419`);`\x` 走 `readhexaesc` 读两个十六进制数字(`llex.c:426`);`\u` 走 `utf8esc` 读 `\u{XXXX}` 并编码成 UTF-8 字节序列(`llex.c:427`);`\\`、`\"`、`\'` 是字面转义；`\z` 是跳过后续空白(`llex.c:433`，用来在源码里折行字符串);`\<digit><digit><digit>` 是十进制字节转义(`readdecesc`,`llex.c:391`，最多三位)。每个转义都先把原始字符存进缓冲(为了错误信息能显示读到哪了)，成功后再用 `luaZ_buffremove` 把这些原始字符删掉、替换成真实值。

```c
case '\\': {  /* escape sequences */
  int c;  /* final character to be saved */
  save_and_next(ls);  /* keep '\\' for error messages */
  switch (ls->current) {
    case 'a': c = '\a'; goto read_save;
    case 'b': c = '\b'; goto read_save;
    /* ... \f \n \r \t \v 同理 ... */
    case 'x': c = readhexaesc(ls); goto read_save;
    case 'u': utf8esc(ls);  goto no_save;
    case '\n': case '\r':
      inclinenumber(ls); c = '\n'; goto only_save;
    case '\\': case '\"': case '\'':
      c = ls->current; goto read_save;
    case EOZ: goto no_save;
    case 'z': {  /* zap following span of spaces */
      luaZ_buffremove(ls->buff, 1);  /* remove '\\' */
      next(ls);  /* skip the 'z' */
      while (lisspace(ls->current)) {
        if (currIsNewline(ls)) inclinenumber(ls);
        else next(ls);
      }
      goto no_save;
    }
    default: {
      esccheck(ls, lisdigit(ls->current), "invalid escape sequence");
      c = readdecesc(ls);  /* digital escape '\ddd' */
      goto only_save;
    }
  }
 read_save:
   next(ls);
   /* go through */
 only_save:
   luaZ_buffremove(ls->buff, 1);  /* remove '\\' */
   save(ls, c);
   /* go through */
 no_save: break;
}
```

三个标号 `read_save`/`only_save`/`no_save` 控制善后：`read_save` 是"再吃一个字符(转义符后面的那个)、删掉反斜杠、存真实值";`only_save` 是"已经吃过了、删掉反斜杠、存真实值";`no_save` 是"自己处理完了，啥也不做"。用 `goto` 串起来避免重复代码。

数字字面量(`llex.c:544` 的 `.` 分支和 `:554` 的 `0-9` 分支)都汇到 `read_numeral`:

```c
case '.': {  /* '.', '..', '...', or number */
  save_and_next(ls);
  if (check_next1(ls, '.')) {
    if (check_next1(ls, '.'))
      return TK_DOTS;   /* '...' */
    else return TK_CONCAT;   /* '..' */
  }
  else if (!lisdigit(ls->current)) return '.';
  else return read_numeral(ls, seminfo);
}
case '0': case '1': ... case '9': {
  return read_numeral(ls, seminfo);
}
```

`.` 单独是字段访问(`.`);`..` 是连接(`TK_CONCAT`);`...` 是可变参数(`TK_DOTS`);`.5` 这种以点开头的数字，要靠 `lisdigit(ls->current)` 判断——点后面跟数字就是浮点字面量。这是 `.` 分支能产生四种结果的全部逻辑。

`read_numeral`(`llex.c:244`)注释里写得很诚实：

```c
/*
** This function is quite liberal in what it accepts, as 'luaO_str2num'
** will reject ill-formed numerals. Roughly, it accepts the following
** pattern:
**
**   %d(%x|%.|([Ee][+-]?))* | 0[Xx](%x|%.|([Pp][+-]?))*
**
** The only tricky part is to accept [+-] only after a valid exponent
** mark, to avoid reading '3-4' or '0xe+1' as a single number.
**
** The caller might have already read an initial dot.
*/
static int read_numeral (LexState *ls, SemInfo *seminfo) {
  TValue obj;
  const char *expo = "Ee";
  int first = ls->current;
  lua_assert(lisdigit(ls->current));
  save_and_next(ls);
  if (first == '0' && check_next2(ls, "xX"))  /* hexadecimal? */
    expo = "Pp";
  for (;;) {
    if (check_next2(ls, expo))  /* exponent mark? */
      check_next2(ls, "-+");  /* optional exponent sign */
    else if (lisxdigit(ls->current) || ls->current == '.')  /* '%x|%.' */
      save_and_next(ls);
    else break;
  }
  if (lislalpha(ls->current))  /* is numeral touching a letter? */
    save_and_next(ls);  /* force an error */
  save(ls, '\0');
  if (luaO_str2num(luaZ_buffer(ls->buff), &obj) == 0)  /* format error? */
    lexerror(ls, "malformed number", TK_FLT);
  if (ttisinteger(&obj)) {
    seminfo->i = ivalue(&obj);
    return TK_INT;
  }
  else {
    lua_assert(ttisfloat(&obj));
    seminfo->r = fltvalue(&obj);
    return TK_FLT;
  }
}
```

策略很巧妙：**词法器宽松地收字符，真正的合法性检查交给 `luaO_str2num`**。词法器只保证：遇到 `0x`/`0X` 切到十六进制模式(指数标记从 `Ee` 换成 `Pp`，因为十六进制浮点的指数是 `p`/`P`)；指数标记后可以跟一个 `+`/`-`；其他时候只要是十六进制数字或 `.` 就收下。

注释里那句"to avoid reading `3-4` or `0xe+1` as a single number"是关键约束：`+`/`-` 只在指数标记(`e`/`E`/`p`/`P`)之后才认，否则 `3-4` 会被当成一个数字。十六进制模式下 `0xe+1` 的 `+1` 同理不能混进数字——这就是为什么十六进制的指数标记是 `p` 而不是 `e`，把 `+` 语义歧义彻底隔开。

读完字符后，塞个 `\0` 结尾，调 `luaO_str2num`(`lobject.c:371`)把字符串转成数值：

```c
size_t luaO_str2num (const char *s, TValue *o) {
  lua_Integer i; lua_Number n;
  const char *e;
  if ((e = l_str2int(s, &i)) != NULL) {  /* try as an integer */
    setivalue(o, i);
  }
  else if ((e = l_str2d(s, &n)) != NULL) {  /* else try as a float */
    setfltvalue(o, n);
  }
  else
    return 0;  /* conversion failed */
  return ct_diff2sz(e - s) + 1;  /* success; return string size */
}
```

先试整数(`l_str2int`)，失败再试浮点(`l_str2d`)。所以 `0x1p2`、`1e3`、`3.14` 都会被识别成浮点(`l_str2int` 拒绝带 `.` 或指数标记的串)，而 `42`、`0xff` 会被识别成整数。**词法器自己不判断 int vs float，完全由 `luaO_str2num` 的转换结果决定**——这是职责分离：词法器管切分，数值库管解析。

转换失败返回 0，词法器报 `malformed number`。

最后看标识符和保留字(`llex.c:561` 的 `default` 分支):

```c
default: {
  if (lislalpha(ls->current)) {  /* identifier or reserved word? */
    TString *ts;
    do {
      save_and_next(ls);
    } while (lislalnum(ls->current));
    /* find or create string */
    ts = luaS_newlstr(ls->L, luaZ_buffer(ls->buff),
                             luaZ_bufflen(ls->buff));
    if (isreserved(ts))   /* reserved word? */
      return ts->extra - 1 + FIRST_RESERVED;
    else {
      seminfo->ts = anchorstr(ls, ts);
      return TK_NAME;
    }
  }
  else {  /* single-char tokens ('+', '*', '%', '{', '}', ...) */
    int c = ls->current;
    next(ls);
    return c;
  }
}
```

标识符规则：`lislalpha` 开头(字母或下划线),`lislalnum` 续接(字母、数字、下划线)。读到非续接字符为止，把缓冲里的内容 `luaS_newlstr` 成一个 `TString`。

然后判断是不是保留字：`isreserved` 宏(`lstring.h:48`):

```c
#define isreserved(s)	(strisshr(s) && (s)->extra > 0)
```

两个条件：**是短字符串**(`strisshr`，因为 Lua 字符串分短串驻留和长串不驻留，只有短串才可能被预留为保留字)且 **`extra > 0`**。`extra` 字段在 `luaX_init`(`llex.c:75`)里被设上：

```c
void luaX_init (lua_State *L) {
  int i;
  TString *e = luaS_newliteral(L, LUA_ENV);  /* create env name */
  luaC_fix(L, obj2gco(e));  /* never collect this name */
  for (i=0; i<NUM_RESERVED; i++) {
    TString *ts = luaS_new(L, luaX_tokens[i]);
    luaC_fix(L, obj2gco(ts));  /* reserved words are never collected */
    ts->extra = cast_byte(i+1);  /* reserved word */
  }
}
```

`luaX_init` 在 VM 启动时调用一次，把 23 个保留字(`luaX_tokens[]` 数组的前 23 个，`llex.c:45`)创建成短字符串，`luaC_fix` 把它们标记成"永不被 GC 回收"(固定对象),`ts->extra = i+1` 记下序号(从 1 开始，所以后面要 `-1`)。

识别保留字的巧妙之处：`luaS_newlstr` 创建短字符串时，如果这个串已经存在(驻留表里有)，就返回已有的那个指针。所以读到 `local` 时，`luaS_newlstr` 返回的就是 `luaX_init` 时创建的那个 `TString`，它的 `extra` 是非 0,`isreserved` 返回真。直接用 `extra - 1 + FIRST_RESERVED` 算出 token 值——`local` 是第 12 个保留字(`luaX_tokens[11]`,`i=11`,`extra=12`),token = `12 - 1 + 256 = 267 = TK_LOCAL`。**整个识别过程零字符串比较，就是一次 hash 查找 + 一次 `extra` 字段读**。

非保留字的标识符走 `anchorstr`(`llex.c:135`):

```c
static TString *anchorstr (LexState *ls, TString *ts) {
  lua_State *L = ls->L;
  TValue oldts;
  int tag = luaH_getstr(ls->h, ts, &oldts);
  if (!tagisempty(tag))  /* string already present? */
    return tsvalue(&oldts);  /* use stored value */
  else {  /* create a new entry */
    TValue *stv = s2v(L->top.p++);  /* reserve stack space for string */
    setsvalue(L, stv, ts);  /* push (anchor) the string on the stack */
    luaH_set(L, ls->h, stv, stv);  /* t[string] = string */
    /* table is not a metatable, so it does not need to invalidate cache */
    luaC_checkGC(L);
    L->top.p--;  /* remove string from stack */
    return ts;
  }
}
```

这是 5.5 重写的字符串锚定逻辑。**5.5 vs 老资料差异**:5.4 及之前用的是 `luaX_newstring`，直接 `luaS_new` 然后 `luaC_objbarrier` 锚到 `ls->h` 表；5.5 拆成两层——`luaX_newstring`(`llex.c:156`)只负责创建新串并锚定，`anchorstr` 负责查重：先在 `ls->h` 表里查这个串在不在，在就直接复用(同一个标识符出现多次只创建一个 `TString`)，不在才创建。这个去重对编译期内存友好，反复出现的变量名不会各占一份。

`default` 分支的 `else` 处理所有"单个字符的 token":`+`、`-`(已处理，这里到不了)、`*`、`%`、`(`、`)`、`{`、`}`、`[`、`]`、`,`、`;`、`^`、`#`。这些字符在 `llex` 里没有专门的 `case`，落进 `default`，直接返回字符 ASCII 码作为 token 值。这是"单字符 token 就用字符本身"这一编码规则的最终落地——这些字符不需要在枚举里列出，因为它们的值就是 ASCII。

`llex` 的最后一个 `case` 是 `EOZ`(`llex.c:558`)，返回 `TK_EOS`，标志输入结束。

### 2.5 走一遍 `local a = 1`

把上面拼起来，跟踪 `local a = 1\n` 这段源码的词法过程。

假设 `firstchar` 是 `'l'`(BOM/shebang 已被 `lauxlib` 跳过),`ls->current = 'l'`。语法分析器调 `luaX_next`，进入 `llex`:

1. 缓冲清空。`switch('l')` 落到 `default`。`lislalpha('l')` 为真。`do { save_and_next } while (lislalnum)`：存 `'l'`，读 `'o'`；存 `'o'`，读 `'c'`;...；存 `'l'`(第 5 个)，读 `' '`。`lislalnum(' ')` 为假，退出循环。缓冲里是 `"local"`。`luaS_newlstr` 查驻留表，返回 `luaX_init` 时创建的那个 `TString`,`extra = 12`,`isreserved` 真，返回 `12 - 1 + 256 = 267 = TK_LOCAL`。`ls->current` 现在是 `' '`。

2. `luaX_next` 把 `TK_LOCAL` 存进 `ls->t`，返回给语法分析器。语法分析器认出这是 `local` 语句开头，再调 `luaX_next`。

3. `llex` 进入。`switch(' ')` 命中空白分支，`next` 读 `'a'`，继续循环。`switch('a')` 落到 `default`,`lislalpha('a')` 为真，存 `'a'`，读 `' '`。`lislalnum(' ')` 为假。`luaS_newlstr("a")`——这是新串(第一次出现),`isreserved` 假(因为 `_ENV`、`break`、`global` 这些预设串的 `extra` 才非 0,`"a"` 不在其中)，走 `anchorstr`，在 `ls->h` 表里登记，返回 `TString`。`seminfo->ts` 指向它，返回 `TK_NAME`。`ls->current` 现在是 `' '`。

4. 语法分析器拿到 `TK_NAME("a")`，再调 `luaX_next`。`llex` 进入，`switch(' ')` 吃掉空白，读 `'='`。`switch('=')` 命中，`next` 读 `' '`(等号后面),`check_next1('=')` —— `' '` 不等于 `'='`，返回 0。返回 `'='`(ASCII 61)作为 token。

5. 语法分析器拿到 `'='`，再调 `luaX_next`。`llex` 进入，`switch(' ')` 吃空白，读 `'1'`。`switch('1')` 命中数字分支，调 `read_numeral`。`save_and_next`：存 `'1'`，读 `'\n'`。`lisxdigit('\n')` 假，`'.'` 也不是，退出循环。`lislalpha('\n')` 假。存 `'\0'`。`luaO_str2num("1")` → `l_str2int` 成功，`setivalue(obj, 1)`。`ttisinteger` 真，`seminfo->i = 1`，返回 `TK_INT`。`ls->current` 现在是 `'\n'`。

6. 语法分析器拿到 `TK_INT(1)`，再调 `luaX_next`。`llex` 进入，`switch('\n')` 命中，`inclinenumber`，行号变 2，读下一个字符。如果是文件尾，`EOZ`。`switch(EOZ)` 返回 `TK_EOS`。

整个过程五次 `luaX_next`，词法器状态机从一个字符推进到下一个字符，没有回溯、没有正则引擎、没有 DFA 状态表——就是一个 switch 加几个辅助函数。

### 2.6 zio：输入抽象

词法器对输入源一无所知。它只通过 `ZIO` 这个抽象拿字符。`ZIO` 定义在 `lzio.h:56`:

```c
struct Zio {
  size_t n;			/* bytes still unread */
  const char *p;		/* current position in buffer */
  lua_Reader reader;		/* reader function */
  void *data;			/* additional data */
  lua_State *L;			/* Lua state (for reader) */
};
```

`reader` 是宿主提供的回调，原型是 `lua_Reader`(`lua.h` 里：`const char *(*lua_Reader)(lua_State *, void *, size_t *)`)——给一个 `data`，返回一块字符的指针和长度，没数据了返回 `NULL`。文件输入、内存输入、网络流输入，只要实现这个回调，都能喂给 Lua 词法器。

`n` 是当前缓冲里还剩多少字节没读，`p` 是当前位置。取字符的宏 `zgetc`(`lzio.h:20`):

```c
#define zgetc(z)  (((z)->n--)>0 ?  cast_uchar(*(z)->p++) : luaZ_fill(z))
```

缓冲还有数据(`n > 0`)就直接从 `p` 取一个字节，`n--`、`p++`；缓冲空了调 `luaZ_fill`(`lzio.c:24`):

```c
int luaZ_fill (ZIO *z) {
  size_t size;
  lua_State *L = z->L;
  const char *buff;
  lua_unlock(L);
  buff = z->reader(L, z->data, &size);
  lua_lock(L);
  if (buff == NULL || size == 0)
    return EOZ;
  z->n = size - 1;  /* discount char being returned */
  z->p = buff;
  return cast_uchar(*(z->p++));
}
```

调 `reader` 要一块新缓冲。`reader` 返回 `NULL` 或长度 0 就是 `EOZ`。否则把 `n` 设为 `size - 1`(因为马上要返回第一个字节，算消耗掉了),`p` 指向新缓冲，返回第一个字节并 `p++`。

这个设计的关键是：**词法器永远只看一个字符，从不关心缓冲边界**。`zgetc` 的三目运算把"缓冲还有"和"缓冲空了去 refill"这两件事包成一个表达式，对调用方完全透明。词法器写 `next(ls)` 时，不需要知道这次取字符是命中了缓冲还是要触发一次 `reader` 回调。

初始化 `luaZ_init`(`lzio.c:39`):

```c
void luaZ_init (lua_State *L, ZIO *z, lua_Reader reader, void *data) {
  z->L = L;
  z->reader = reader;
  z->data = data;
  z->n = 0;
  z->p = NULL;
}
```

注意初始 `n = 0`、`p = NULL`——缓冲一开始是空的，第一次 `zgetc` 就会触发 `luaZ_fill` 去 `reader` 拿数据。这就是为什么 `luaX_setinput` 要单独传 `firstchar`：第一次取字符之前，词法器需要 `current` 已经有一个有效值，所以 `lauxlib` 在调用 `lua_load` 之前先 `getc` 出第一个字符(顺便做 BOM/shebang 处理)，作为 `firstchar` 直接塞进 `ls->current`，绕过 zio 的"第一次 fill"。

**5.5 vs 老资料差异**:`lzio.c` 在 5.5 新增了 `checkbuffer`(`lzio.c:50`)和 `luaZ_getaddr`(`lzio.c:79`)。这两个函数 5.4 没有。`luaZ_getaddr` 的作用是"如果剩余缓冲里有连续 n 字节，直接返回这 n 字节的起始地址，不拷贝"。这是给 `lundump.c`(预编译字节码加载)用的——加载二进制 chunk 时要读大块定长数据，直接拿地址比一个个 `zgetc` 快得多。词法器不用这两个函数，但它们的存在说明 5.5 把 zio 从"纯字符流"扩展成了"字符流 + 块寻址"的混合抽象，服务于二进制加载。

### 2.7 lookahead：为什么只预读一个

`LexState` 里有 `t`(当前 token)和 `lookahead`(预读 token)两个。`luaX_next`(`llex.c:588`):

```c
void luaX_next (LexState *ls) {
  ls->lastline = ls->linenumber;
  if (ls->lookahead.token != TK_EOS) {  /* is there a look-ahead token? */
    ls->t = ls->lookahead;  /* use this one */
    ls->lookahead.token = TK_EOS;  /* and discharge it */
  }
  else
    ls->t.token = llex(ls, &ls->t.seminfo);  /* read next token */
}
```

如果 `lookahead` 已经被预读过(`token != TK_EOS`)，就直接把它晋升为当前 token,`lookahead` 清空；否则才真正调 `llex` 读一个新 token。

预读由 `luaX_lookahead`(`llex.c:599`)触发：

```c
int luaX_lookahead (LexState *ls) {
  lua_assert(ls->lookahead.token == TK_EOS);
  ls->lookahead.token = llex(ls, &ls->lookahead.seminfo);
  return ls->lookahead.token;
}
```

为什么需要预读？因为 Lua 的语法在某些地方是** LL(1) 但不是 LL(0)**——光看当前 token 决定不了接下来是什么。典型例子：`a = b` 和 `a()` 都是合法语句，看到 `a` 之后，语法分析器要看下一个 token 是 `=` 还是 `(` 才知道这是赋值还是函数调用。再比如 `function f() end` 里，看到 `function` 后要看是 `function name` 还是 `function (args)`(匿名函数)。

但 Lua 的语法分析器**只需要一个 token 的 lookahead**。这是 Lua 语法刻意保持的性质——没有需要两个以上 lookahead 才能消解的歧义。所以 `lookahead` 就是一个 `Token`，不是数组。`luaX_lookahead` 里有断言 `ls->lookahead.token == TK_EOS`，保证不会重复预读。

这个"一个 token 的 lookahead"是 Lua 词法/语法分层的简洁性的来源：词法器只暴露两个操作(`luaX_next` 推进一步、`luaX_lookahead` 偷看一步)，语法分析器就能消解所有语法歧义，不需要更复杂的接口。

### 2.8 错误处理：带行号和位置

词法错误走 `lexerror`(`llex.c:116`):

```c
static l_noret lexerror (LexState *ls, const char *msg, int token) {
  msg = luaG_addinfo(ls->L, msg, ls->source, ls->linenumber);
  if (token)
    luaO_pushfstring(ls->L, "%s near %s", msg, txtToken(ls, token));
  luaD_throw(ls->L, LUA_ERRSYNTAX);
}
```

`luaG_addinfo`(`ldebug.c` 里的函数)把源文件名和行号拼进错误信息，格式是 `source:line: msg`。`txtToken`(`llex.c:104`)把出错的 token 转成可读形式，`near xxx` 指出错误附近是什么。

`luaX_token2str`(`llex.c:87`)负责 token 到字符串的转换：

```c
const char *luaX_token2str (LexState *ls, int token) {
  if (token < FIRST_RESERVED) {  /* single-byte symbols? */
    if (lisprint(token))
      return luaO_pushfstring(ls->L, "'%c'", token);
    else  /* control character */
      return luaO_pushfstring(ls->L, "'<\\%d>'", token);
  }
  else {
    const char *s = luaX_tokens[token - FIRST_RESERVED];
    if (token < TK_EOS)  /* fixed format (symbols and reserved words)? */
      return luaO_pushfstring(ls->L, "'%s'", s);
    else  /* names, strings, and numerals */
      return s;
  }
}
```

单字符 token 直接打印字符(`'='`)；不可打印的控制字符用 `'<\数字>'`；多字符 token 查 `luaX_tokens[]` 数组——保留字和操作符加引号(`'local'`、`'=='`)，名字/字符串/数字用尖括号格式(`<name>`、`<string>`)。

错误用 `luaD_throw` 抛 `LUA_ERRSYNTAX`。这是 Lua 的错误机制——不是返回错误码，而是 longjmp 回到 `luaD_pcall` 的保护点(`ldo.c:1155` 的 `luaD_protectedparser` 就是在 `luaD_pcall` 里跑的)。所以词法错误一抛出，整个编译过程终止，控制权回到宿主的 `lua_load` 调用点，返回 `LUA_ERRSYNTAX`。

---

## 三、为什么这样设计是 sound 的

### 3.1 查找表为什么 sound

`luai_ctype_[]` 查找表的正确性来自两点。

第一，**位掩码的正交性**。五个 bit 各代表一个独立属性，任何组合都不会互相干扰。一个字符可以同时是字母 + 数字(`_`？不，`_` 在表里是 0x05 = ALPHA + PRINT，不是数字:2但理论上 DIGIT 和 ALPHA 可以并存)。判定 `lislalnum` 就是 `ALPHA | DIGIT` 两个 bit 一起测，这是位运算的正确用法——两个 bit 的并集和"是字母或是数字"语义等价。

第二，**表与 locale 无关**。这张表在编译期就写死在 `lctype.c` 里，值是 ASCII 编码下的硬编码。土耳其语 locale 下 `isalpha('I')` 可能返回假，但 `lislalpha('I')` 永远返回真——因为表里 `'I'`(0x49)的 bit 0 是 1。Lua 词法器的行为在任何 locale 下都完全一致，这是嵌入式语言必须的性质(宿主的 locale 不应该影响脚本的解析)。

第三，**EOZ 的处理**。表多一项(257 而不是 256),`(c)+1` 把 `EOZ = -1` 映射到下标 0，值是 `0x00`(所有属性都假)。这意味着 `lislalpha(EOZ)`、`lisdigit(EOZ)` 等都返回假——词法器在输入结束时，任何属性判断都会失败，自然走到"结束"分支。不需要单独写 `if (c == EOZ)` 特判，这减少了主循环里的条件分支。

### 3.2 单字符 switch 为什么 sound

`llex` 用一个 `switch (ls->current)` 分派所有情况，看起来朴素，但它在几个意义上是对的。

第一，**所有分支都是确定性的**。每个字符唯一决定走哪个分支，分支内部用至多一个字符的 lookahead(`check_next1`)消解剩余歧义。没有需要回溯的情况——比如看到 `<`，要么是 `<`、要么是 `<=`、要么是 `<<`，吃完 `<` 后看下一个字符就够，不需要"试探 `<<=`"(Lua 没有 `<<=` 这种 token)然后回退。这种"每个前缀都唯一决定下一步"的性质，是手写词法器比正则引擎轻量的根本原因。

第二，**单字符 token 和多字符 token 共用一套编码**。单字符 token 用 ASCII 值(0–255)，多字符 token 用 256+,`FIRST_RESERVED` 这个边界保证了它们永不相撞。语法分析器拿到 token 后，`token < FIRST_RESERVED` 一个比较就知道是不是单字符 token。这是"一个 int 表示所有 token"的编码能成立的关键——如果没有这个边界，就需要额外的"token 类型"字段，`Token` 结构就变胖。

第三，**空白和注释的跳过内嵌在主循环里**。`for(;;)` 外层循环 + `switch` 内部分派，空白和注释命中后 `break` 回到循环顶部继续，只有真正读到 token 才 `return`。这避免了"先跳过空白再读 token"的两段式代码，把所有字符处理统一在一个函数里。

### 3.3 lookahead 为什么 sound

`lookahead` 只有一个槽，这之所以够用，是因为 Lua 语法是 LL(1) 的——任何语法决策点，看当前 token 和下一个 token 就能消解。

`luaX_next` 的"晋升 lookahead"逻辑是 sound 的关键。预读过的 token 不会丢失：`ls->t = ls->lookahead` 把它移到当前 token 的位置，`lookahead.token = TK_EOS` 清空预读槽。这样下一次 `luaX_next` 才会真正调 `llex` 读新 token。这个"预读即缓存"的机制保证：不管语法分析器是先 `luaX_lookahead` 再 `luaX_next`，还是直接 `luaX_next`，拿到的 token 序列完全一致。

不变式：**任意时刻 `lookahead` 要么是空(`TK_EOS`)，要么是 `t` 之后紧跟的下一个 token**。`luaX_lookahead` 的断言 `ls->lookahead.token == TK_EOS` 保证不会预读两次(那样会覆盖掉第一次预读的 token，造成 token 丢失)。

### 3.4 zio 抽象为什么 sound

zio 的 sound 性在于它**隐藏了输入的全部细节**。词法器只调 `zgetc`，从不关心：

- 数据来自文件、内存还是网络；
- 缓冲有多大(可能是 1 字节、可能是 4KB);
- `reader` 什么时候返回 `NULL`(文件读完了 vs 临时没数据)。

`luaZ_fill` 把"缓冲空了"这件事完全封装——它要么 refill 成功返回一个字符，要么返回 `EOZ`。对词法器来说，`zgetc` 是一个永远返回"下一个字符或 EOZ"的黑盒。

这个抽象的代价是**一次函数调用的间接性**(`reader` 回调)，但换来的是词法器对输入源的完全无知。这是 Lua 嵌入性的一个微观体现：宿主只要实现一个 `lua_Reader`(比如"从网络 socket 读一块"),Lua 就能编译网络流来的脚本，词法器不用改一行代码。

### 3.5 字符串锚定为什么 sound

词法器读到的字符串(`TK_NAME`、`TK_STRING`)要变成 `TString` 对象，这些对象在编译期间必须存活——否则 GC 可能在编译中途把它们回收掉，语法分析器拿着悬空指针就崩了。

`luaX_init` 把保留字 `luaC_fix` 成固定对象(永不被 GC)，这解决了保留字的存活问题。但用户写的标识符和字符串字面量不能 fix(那会内存泄漏)，所以用 `ls->h` 这个 `Table` 来锚定：`anchorstr` 把每个新串登记进 `ls->h`，只要编译没结束，`ls->h` 就持有这些串的引用，GC 不会动它们。编译结束(`luaY_parser` 返回后),`ls->h` 被释放，这些串如果没被其他地方引用，就会被 GC 正常回收。

`anchorstr` 的去重(查到已有就复用)是 sound 的：同一个标识符在源码里出现多次，编译后指向同一个 `TString`，这和 Lua 短字符串驻留的语义一致(短字符串在 VM 里全局唯一)。这也让语法分析器比较标识符时可以用指针比较(`ts == ts2`)，不用字符串内容比较。

---

## 四、★对照 CPython

把 Lua 的词法器和 CPython 的 tokenizer(`Parser/tokenizer.c`)对照，最能看出 Lua 主线"精简"的落点。

### 4.1 token 编码

Lua 的 token 是一个 `int`，单字符 token 用 ASCII 值，多字符 token 从 256 开始，共用一套编码。CPython 的 token 也是一个 `int`(枚举 `NAME`/`NUMBER`/`STRING`/`OP`/`NEWLINE`/`INDENT`/`DEDENT` 等)，但没有"单字符 token 用字符本身"这一招——所有 token 都是枚举值，连 `+`、`-` 这种单字符操作符都是 `OP` 类型，具体的操作符靠一个附属的字符串字段区分(`e->strings` 在 `Parser/tokenize.c`)。Lua 的编码更省内存：`Token` 结构里一个 `int` 就够，不需要附加字段。

### 4.2 缩进语义：核心分歧

这是 Lua 和 Python 词法层最大的区别。

**Python 把缩进当成语法的一部分**。一行开头的空格数决定了语句的嵌套层次。tokenizer 必须把这个信息显式地变成 token：进入一个缩进块，发射一个 `INDENT` token；退出，发射 `DEDENT` token；每行末尾发射 `NEWLINE`(语句内换行)或 `NL`(续行)。CPython 的 `Parser/tokenizer.c` 维护一个缩进栈(`indentstack`)，每次读到行首的空格，和栈顶比较：多了就 push 并发 `INDENT`，少了就 pop 并发 `DEDENT`(可能连续多个)。还要处理 tab vs space 的混用规则(`tidy` 函数)、续行符 `\`、括号内的隐式续行(括号内换行不发 `NEWLINE`)。这是一整套状态机，只为了把"看起来对齐的空格"变成显式的 token。

**Lua 没有缩进语义**。`llex` 的主循环里，空白(包括行首的空格)统一走 `case ' ': ... next(ls)` 吃掉，不产生任何 token。语句的边界由关键字(`do`/`end`/`then`/`while`)显式标记，不靠缩进。换行符 `case '\n'` 只触发 `inclinenumber`(更新行号)，不产生 `NEWLINE` token——Lua 的语法分析器不需要知道"这里换行了"，换行在词法层就和其他空白一样被吞掉。

这个差异的后果：

- Python 的源码格式是被语法约束的(混用 tab 和 space 会报 `TabError`，缩进不一致会报 `IndentationError`)。Lua 的源码格式是自由的，你可以把整个函数写在一行(用空格或 `;` 分隔)，也可以随意缩进。
- Python 的 tokenizer 必须维护跨行的状态(缩进栈、括号深度)。Lua 的 `llex` 是**无状态的字符级循环**——每次进来只看 `ls->current`，不记前一行缩进。
- Python 必须有 `NEWLINE`/`INDENT`/`DEDENT` 三种 token。Lua 一个都没有，token 种类比 Python 少三类。

Python 的 `;` 是可选的语句分隔符(很少用);Lua 的 `;` 也是可选的语句分隔符(也很少用)。但 Python 用缩进强制语句结构，Lua 用关键字——这是两种语言在"怎么表达程序结构"上的根本分歧，而这个分歧在词法层就已经显现：Python 的词法器要造出结构 token,Lua 的词法器只造出值 token。

### 4.3 字符分类

CPython 的 tokenizer 用标准 `<ctype.h>` 的 `isspace`/`isdigit`/`isalpha`(在 `Parser/tokenize.c` 里直接调)。这意味着 Python 的 tokenizer 受 locale 影响——虽然在 CPython 的实践中这个问题被规避了(因为 Python 源码默认是 UTF-8，且 Python 3 的标识符用 Unicode 分类，不走 `<ctype.h>`)，但 `<ctype.h>` 的 locale 依赖是一个已知的坑。

Lua 用自己的 `luai_ctype_[]` 查找表，locale 无关，且 O(1)。这是 Lua 为嵌入式场景做的选择——宿主可能跑在任何 locale 下，Lua 的行为必须确定。

### 4.4 数字识别

CPython 的 tokenizer(`Parser/tokenize.c`)对数字字面量有一套专门的识别逻辑，分整数、浮点、复数、十六进制、八进制、二进制、带下划线分隔(`1_000_000`)等多种形式，识别逻辑相当复杂(几百行代码)。识别完直接得出数值类型。

Lua 的 `read_numeral` 反其道而行——**宽松收字符，合法性交给 `luaO_str2num`**。词法器只管"这些字符看起来像数字"，真正的解析(整数 vs 浮点、十六进制、指数)由数值库统一处理。这是职责分离：词法器薄，数值库厚。Lua 不支持数字字面量里的下划线分隔(5.5 没有这个特性)，数字形式比 Python 少，所以词法器可以这么简化。

### 4.5 整体规模

Lua 5.5 的 `llex.c` 是 605 行，`lctype.c` 是 65 行，`lzio.c` 是 90 行——整个词法相关代码加起来约 760 行 C。CPython 的 `Parser/tokenizer.c`(3.12)是约 2200 行，`Parser/tokenize.c`(Python 实现的版本)另有约 1900 行。这个 3 倍以上的规模差距，大部分花在缩进处理(`INDENT`/`DEDENT` 状态机)、Python 丰富的字面量形式、f-string 的复杂解析(Python 3.12 把 f-string 的词法化做到了 tokenizer 层)、以及 Unicode 标识符的完整支持上。

Lua 的精简在词法层就兑现了：一个 switch、一张查找表、一个输入抽象，760 行 C 跑通一门完整语言的全部词法。

---

## 五、回扣主线

本章是编译侧(P2)的入口。把字符流切成 token 流这件事，Lua 用三件东西搞定：

- **`luai_ctype_[]` 查找表**——O(1)、locale 无关、把字符分类从函数调用降维成数组读 + 位与。这是"快"和"小"在微观层面的兑现。
- **`llex` 单字符 switch**——一个函数处理所有 token 类型，单字符 token 和多字符 token 共用一套 `int` 编码，空白和注释内嵌在主循环里跳过。这是"用更少的机制换更多的能力"——没有正则引擎、没有 DFA、没有状态机框架，一个 switch 够了。
- **`ZIO` 输入抽象**——词法器对输入源完全无知，宿主提供 `lua_Reader` 就能喂任何来源的脚本。这是"适合嵌入"在编译入口处的兑现。

对照 CPython,token 种类更少(没有 INDENT/DEDENT/NEWLINE)、词法器更小(760 行 vs 4000+ 行)、字符分类更快(查找表 vs locale 依赖的 `<ctype.h>`)。Lua 砍掉了 Python 词法器里最大的一块复杂度——缩进语义——换来的是一个可以完整读透的、680 行的词法器。这正是本书主线"统一与精简换小而快"在编译侧第一步的具体含义：**词法器本身就被设计得又小又快，后面的语法分析和代码生成才能建在一个轻量而确定的 token 流基础上**。

词法器吐出的 token 流，接下来交给语法分析器。语法分析器是递归下降 + 算符优先的混合，边解析边生成字节码——这是 P2-07 的内容。

---

*下一章 [P2-07 语法分析(上)](P2-07-语法分析上-递归下降与算符优先.md)：递归下降怎么吃下 token 流，算符优先怎么处理表达式，边解析怎么边生成寄存器式字节码。*
