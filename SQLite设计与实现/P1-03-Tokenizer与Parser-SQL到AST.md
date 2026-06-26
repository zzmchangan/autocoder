# 第 1 篇 · 第 3 章 · Tokenizer 与 Parser:SQL → AST

> **核心问题**:上一章我们把 SQLite 切成了八层流水线,看到一条 `SELECT a+b*c FROM t WHERE x>0` 要先从字符串变成**语法树(AST)**,才能交给 Code Generator 编译成 opcode。但"从字符串到树"这一跳,内部到底怎么发生的?具体说有三件事:① **Tokenizer** 怎么把连续的字节流切成一个个 token(`SELECT`、`a`、`+`、`b`、`*`、`c`、`FROM`、`t`、`WHERE`、`x`、`>`、`0`),又怎么区分一个标识符到底是关键字 `SELECT` 还是列名 `select`?② **Parser** 怎么按文法把这些 token 拼成一棵树——`a+b*c` 凭什么树根是 `+` 而不是 `*`?③ SQLite 的 parser 不是手写的,是用一个叫 **Lemon** 的工具**从文法源 `parse.y` 生成**的——SQLite 为什么非要自研一个 parser generator,而不直接手写递归下降、或者用现成的 bison/yacc?

> **读完本章你会明白**:
> 1. Tokenizer 的核心是**一张字符分类表 + 一个字符类 switch**(`tokenize.c` 里 899 行的 `sqlite3GetToken`),为什么用查表不用 `if-else` 链——速度。
> 2. 关键字识别用的是 **`mkkeywordhash.c` 离线生成的最小完美哈希表**(`keywordhash.h`),运行时 O(1) 查;为什么不用普通哈希——嵌入式,代码体积要小、冲突链要短。
> 3. SQLite 的 parser 是 **LALR(1) 表驱动 parser**,**不是手写递归下降**,文法源是 `src/parse.y`(2163 行),产物是 `parse.c`;`parse.y` 是人写的"语法书",`parse.c` 是机器按这本书造出来的"执行机"。
> 4. 为什么 SQLite 自研 **Lemon** 而不用 bison/yacc——三点:线程安全(parser 引擎可在栈上分配)、可重入、**比 yacc 严格得多的冲突检测**(Lemon 遇到文法冲突直接报错,yacc 只警告),这是 SQLite 几十年零语法 bug 的根。
> 5. AST 的核心节点长什么样——`Expr`(表达式树)、`Select`(查询树)、`SrcList`(FROM 列表),以及一个贯穿全书的精妙技巧:**Expr 结构里的 `EP_Reduced` / `EP_TokenOnly` 截断标志**,让同一棵树在 schema 缓存里占的内存砍掉一半以上。

> **如果一读觉得太难**:先记住四件事——① Tokenizer 是按字符分类表切词的 switch;② Parser 是用 Lemon 从 `parse.y` 生成的 LALR(1) 表驱动机器,不是手写递归下降;③ `a+b*c` 树根是 `+`,是因为文法里"乘除"优先级比"加减"高(写成了不同的非终结符层);④ 词法/语法分析的**第一性原理**(正则切词 / LALR 表驱动 / 抽象语法树)《Lua》那本已经讲透,本章只讲 SQLite 独有的部分(Lemon、SQL 文法、AST 节点设计)。

---

## 〇、一句话点破

> **Tokenizer 是一张字符分类表驱动的逐字符扫描器,Parser 是 Lemon 从 `parse.y` 文法生成的 LALR(1) 表驱动状态机;两者的产物是一棵由 `Expr` / `Select` / `SrcList` 节点组成的 AST,这棵树是 Code Generator 的输入。**

这是结论,不是理由。本章倒过来拆:先讲 Tokenizer 为什么用字符分类表(而不是 `if-else`)、关键字哈希为什么离线生成(而不是运行时建);再讲 parser 为什么不手写、为什么是 Lemon 而非 bison;接着贴真实的 `parse.y` 文法片段,把"SELECT/WHERE/表达式优先级"怎么落地成 AST 讲透;最后用一个 `SELECT a+b*c FROM t WHERE x>0` 的完整例子把"字符串 → tokens → AST"三层串起来,收在两个最硬的技巧(关键字哈希的最小化、Expr 的内存截断)上。

---

## 一、词法/语法分析的第一性原理:见《Lua》,本章只补 SQLite 独有的

在动手前,先把"这本书和前作的分工"钉死,避免重复造轮子。

词法分析(把字符流切成 token)、语法分析(按文法把 token 序列归约成 AST)、LALR(1) 表驱动 parser 的工作原理(状态栈、移进-归约、预测分析表),这些**第一性原理**在《Lua 虚拟机深入浅出》那本的"编译前端"章已经讲透(Lua 的 `llex.c` 词法器、`lparser.c` 递归下降 parser,以及那本对照讲的 yacc/bison LALR 模型)。**读过那本的同学,这一节当复习;没读过的,把下面这几句记住就够支撑本章**:

- **词法分析**本质是"逐字符跑一个有限状态机,把一段段连续字节归类成 token"。手写通常用一个 `switch(首字符类)` + 一个吃同类的内层循环。
- **语法分析**有两种主流实现:**递归下降**(手写,一个非终结符对应一个 C 函数,用函数调用栈模拟文法嵌套,如 Lua、V8)、**表驱动 / LALR(1)**(用工具从文法源生成,运行时是一个"读 token、查表、移进或归约"的循环,如 SQLite、PostgreSQL、MySQL)。
- **AST(抽象语法树)**是 parser 的产物,后续阶段(语义分析、代码生成、优化)都在树上操作。树的设计直接决定后续代码好不好写——这就是为什么 SQLite 的 `Expr` 结构里塞了那么多 union 和 flag。

> **承接《Lua》**:Lua 的 parser 是**手写递归下降**(因为 Lua 文法小、且要求单遍编译就填好跳转地址),SQLite 的 parser 是**Lemon 生成的 LALR(1) 表驱动**(因为 SQL 文法大、规整、适合工具生成)。两者代表编译前端的两大流派,本章重点讲 SQLite 选 LALR+Lemon 的理由——这是 SQLite 区别于 Lua 的编译前端选型,也是读者最容易翻车的点(很多资料把 SQLite parser 讲成"手写",**错**,它是工具生成的)。

本章后面所有内容,都是 SQLite 独有、前作没讲过的:**Tokenizer 的字符分类表设计、关键字最小完美哈希、为什么自研 Lemon、`parse.y` 里真实的 SQL 文法长什么样、`Expr`/`Select`/`SrcList` 这些 AST 节点为什么这么设计**。

---

## 二、Tokenizer:一张字符分类表驱动的逐字符扫描器

### 提问:把 `"SELECT a+b"` 切成 token,朴素怎么做?

最朴素的想法:写一个 `while(*z)` 循环,里面用 `if-else` 判断当前字符。

```c
// 朴素写法(示意,非 SQLite 源码)
while( *z ){
  if( isspace(*z) ){ 跳过空白; }
  else if( isalpha(*z) ){ 读一个标识符; 查关键字表决定是关键字还是 ID; }
  else if( isdigit(*z) ){ 读一个数字; }
  else if( *z=='+' ){ 产出 TK_PLUS; }
  else if( *z=='*' ){ 产出 TK_STAR; }
  ... 几十个 else if ...
}
```

这种写法能跑,但在 SQL 这种**字符类多**(关键字字母、数字、十几种操作符 `<`/`<=`/`<>`/`<<`、引号 `'`/`"`/`` ` ``、注释 `-`/`/*`)、且**每条 SQL 要切几十到几百个 token**的场景下,有两个硬伤:

- **`if-else` 链太长,编译器生成的跳转是二分或线性比较,慢**。Tokenize 是编译期最热的循环之一,SQLite 对它的要求是"每条 SQL 切词的延迟要稳定地低"。
- **字符的分类逻辑分散在各处**。同一个 `<` 字符,既可能是 `TK_LT`(`<`),也可能是 `TK_LE`(`<=`)、`TK_NE`(`<>`)、`TK_LSHIFT`(`<<`)——这些"双字符操作符"的前瞻判断写在 `if-else` 里,又臭又长还容易漏。

### 不这样会怎样:为什么不能用 `if-else` 链

> **不这样会怎样**:如果 SQLite 的 tokenize 用 `if-else` 链,每切一个 token 平均要跳十几二十次分支;在嵌入式设备(手机、IoT)上,`sqlite3_prepare_v2` 的延迟会肉眼可见地高。更糟的是,字符分类逻辑散落,加一个新操作符(比如 `->`、`->>` 这种 JSON 操作符)就得改一堆地方,容易引 bug。SQLite 选择把"字符 → 类别"这一步**预算成一张查找表**,把"按类别分发"做成**一个 switch(small int)**——编译器会把小整数 switch 编成跳表(jump table),O(1)。

### 所以这样设计:字符分类表 + 字符类 switch

SQLite 的 tokenizer 全部在 [`src/tokenize.c`](../sqlite/src/tokenize.c) 里(899 行,核心是 `sqlite3GetToken`)。它的设计精髓是**两层查表**。

**第一层:字符 → 字符类**。SQLite 把所有可能出现在 SQL 里的字节,预先归成 31 个"字符类"(`CC_*`),用一张 256 项的查找表 `aiClass[]` 把每个字节映射到它的类: [`tokenize.c#L29-L59`](../sqlite/src/tokenize.c#L29-L59)

```c
#define CC_X          0    /* 字母 'x',BLOB 字面量开头 x'...' */
#define CC_KYWD0      1    /* 关键字首字母(A-Z, 除 x) */
#define CC_KYWD       2    /* 关键字后续字母 / 标识符 */
#define CC_DIGIT      3    /* 数字 */
#define CC_DOLLAR     4    /* '$' */
#define CC_VARALPHA   5    /* '@', '#', ':' —— 字母型变量 */
#define CC_VARNUM     6    /* '?' —— 数字型变量 */
#define CC_SPACE      7    /* 空白 */
#define CC_QUOTE      8    /* '"', '\'', '`' —— 字符串 / 引号 ID */
#define CC_QUOTE2     9    /* '[' —— [..] 风格引号 ID */
#define CC_PIPE      10    /* '|' —— 位或 / 连接 || */
#define CC_MINUS     11    /* '-' —— 减号 / -- 注释 / -> / ->> */
#define CC_LT        12    /* '<' */
#define CC_GT        13    /* '>' */
#define CC_EQ        14    /* '=' */
#define CC_BANG      15    /* '!' */
#define CC_SLASH     16    /* '/' —— 除号 / 注释 */
#define CC_LP        17    /* '(' */
#define CC_RP        18    /* ')' */
#define CC_SEMI      19    /* ';' */
#define CC_PLUS      20    /* '+' */
#define CC_STAR      21    /* '*' */
#define CC_PERCENT   22    /* '%' */
#define CC_COMMA     23    /* ',' */
#define CC_AND       24    /* '&' */
#define CC_TILDA     25    /* '~' */
#define CC_DOT       26    /* '.' */
#define CC_ID        27    /* Unicode 等可用作标识符的字符 */
#define CC_ILLEGAL   28    /* 非法字符 */
#define CC_NUL       29    /* 0x00 */
#define CC_BOM       30    /* UTF8 BOM 首字节 0xEF */
```

这张表本身是一个 256 项的 `unsigned char` 数组,布局是"按 ASCII 码做下标"——`aiClass['<']` 直接给出 `CC_LT(12)`,`aiClass['S']` 给出 `CC_KYWD0(1)`: [`tokenize.c#L61-L100`](../sqlite/src/tokenize.c#L61-L100)

```
/*         x0  x1  x2  x3  x4  x5  x6  x7  x8  x9  xa  xb  xc  xd  xe  xf */
/* 2x */    7, 15,  8,  5,  4, 22, 24,  8, 17, 18, 21, 20, 23, 11, 26, 16,
/*            SP  !   "   #   $   %   &   '   (   )   *   +   ,   -   .   /   */
/* 3x */    3,  3,  3,  3,  3,  3,  3,  3,  3,  3,  5, 19, 12, 14, 13,  6,
/*          0   1   2 ...                  ;   <   =   >   ?                */
/* 4x */    5,  1,  1,  1,  1,  1,  1,  1,  1,  1,  1,  1,  1,  1,  1,  1,
/*          @   A   B   C   D ...                                   O       */
```

这张表是 ASCII 版的(还有 EBCDIC 版,见下文"可移植性"小节)。一行 16 个数字对应 ASCII 一行的 16 个字符——`0x20` 是空格(`CC_SPACE=7`),`0x21` 是 `!`(`CC_BANG=15`),`0x2b` 是 `+`(`CC_PLUS=20`)。**一次数组下标,就完成"字符归类"**,没有任何分支。

**第二层:字符类 → 处理逻辑**。`sqlite3GetToken` 的入口就是一个 `switch( aiClass[*z] )`: [`tokenize.c#L273-L295`](../sqlite/src/tokenize.c#L273-L295)

```c
i64 sqlite3GetToken(const unsigned char *z, int *tokenType){
  i64 i;
  int c;
  switch( aiClass[*z] ){  /* 按首字符的类分发 */
    case CC_SPACE: {
      for(i=1; sqlite3Isspace(z[i]); i++){}
      *tokenType = TK_SPACE;
      return i;
    }
    case CC_MINUS: {
      if( z[1]=='-' ){
        for(i=2; (c=z[i])!=0 && c!='\n'; i++){}   /* -- 行注释 */
        *tokenType = TK_COMMENT;
        return i;
      }else if( z[1]=='>' ){
        *tokenType = TK_PTR;                       /* -> 或 ->> */
        return 2 + (z[2]=='>');
      }
      *tokenType = TK_MINUS;
      return 1;
    }
    ...
  }
}
```

因为 `aiClass[*z]` 是 0~30 的小整数,编译器会把整个 switch 编成**跳表**——`jmp [table + aiClass[*z]*8]`,O(1) 进到对应 case。每个 case 内部再用一两个 `if` 处理"多字符操作符"的前瞻(`-` 看 `z[1]` 是 `-` 还是 `>`),以及"读掉同类的后续字符"(`for(i=1; sqlite3Isspace(z[i]); i++){}` 把一串空白吃完)。

> **所以这样设计**:两层查表把"字符归类"这个最热的操作做成 O(1),把"按类分发"做成跳表 switch;每个 token 的产出成本基本就是"一次数组下标 + 一两次前瞻 + 一个吃同类循环"。这是嵌入式数据库榨 tokenize 性能的标准套路,和《MySQL·InnoDB》那种"服务端不在乎多几次分支"的取舍正好相反——**SQLite 必须在每条 prepare 上抠到极致**。

### 几个关键字符类的处理(逐个拆)

读 `sqlite3GetToken` 的 switch,几个有讲究的 case 值得单独看:

**① `CC_QUOTE`:三种引号一套逻辑**。`'`、`"`、`` ` `` 在 SQL 里都算引号(分别用于字符串字面量、引号标识符、MySQL 风格引号标识符),SQLite 用一个 case 统一处理——读到下一个同样的引号为止,并支持"双写转义"(`'it''s'` 表示 `it's`): [`tokenize.c#L396-L420`](../sqlite/src/tokenize.c#L396-L420)

```c
case CC_QUOTE: {
  int delim = z[0];
  testcase( delim=='`' );  testcase( delim=='\'' );  testcase( delim=='"' );
  for(i=1; (c=z[i])!=0; i++){
    if( c==delim ){
      if( z[i+1]==delim ){ i++; }   /* 双写转义: '' -> ' */
      else break;
    }
  }
  if( c=='\'' ){ *tokenType = TK_STRING;  return i+1; }   /* 单引号 = 字符串 */
  else if( c!=0 ){ *tokenType = TK_ID;     return i+1; }  /* 双引号/反引号 = 标识符 */
  else           { *tokenType = TK_ILLEGAL; return i; }   /* 没闭合 = 非法 */
}
```

注意最后那三行判断——**SQLite 用"闭合引号是什么字符"来反推这个 token 是字符串还是标识符**:`'...'` 闭合是 `'` → `TK_STRING`;`"..."` 或 `` `...` `` 闭合是 `"`/`` ` `` → `TK_ID`(引号标识符,允许用 `select` 当列名)。这是一个把"字符串字面量"和"引号标识符"塞进同一个扫描循环的小巧手段。

**② `CC_DIGIT` + `CC_DOT` 的 fall-through**:整数、浮点、十六进制、`.5` 这种小数,都从一个 case 起步。`CC_DOT` 在"后面跟数字"时,直接**贯穿(fall through)**进 `CC_DIGIT` 的处理,这是 C 里少见的合法 `deliberate_fall_through`(SQLite 自己加了宏标注,避免编译器警告): [`tokenize.c#L421-L498`](../sqlite/src/tokenize.c#L421-L498)。整段处理 `0x1F`、`3.14`、`1e10`、`1_000`(数字分隔符,`SQLITE_DIGIT_SEPARATOR`),逻辑虽长但平铺直叙——一个 `for` 吃整数部分,看有没有 `.`,看有没有 `e/E` 指数,最后扫一遍有没有非法尾字符(如 `123abc` 标 `TK_ILLEGAL`)。

**③ `CC_KYWD0` / `CC_KYWD`:关键字 vs 标识符的"贪婪读完再查"**。这是 tokenizer 最微妙的一环。一个 `select` 到底是关键字 `TK_SELECT` 还是列名 `TK_ID`,**光看第一个字符决定不了**——必须先把整个单词读完,再去关键字表里查。SQLite 的做法是: [`tokenize.c#L539-L551`](../sqlite/src/tokenize.c#L539-L551)

```c
case CC_KYWD0: {
  if( aiClass[z[1]]>CC_KYWD ){ i = 1; break; }   /* 后面不是关键字字符 → 单字符 ID */
  for(i=2; aiClass[z[i]]<=CC_KYWD; i++){}        /* 贪婪读完所有关键字可用字符 */
  if( IdChar(z[i]) ){
    /* 读到关键字字符表之外、但仍是合法 ID 字符(如 Unicode)→ 这是标识符 */
    i++;
    break;   /* 落到末尾的通用 "while(IdChar(z[i])) i++" 兜底,产 TK_ID */
  }
  *tokenType = TK_ID;
  return keywordCode((char*)z, i, tokenType);    /* 去关键字表查 */
}
```

注意那个 `keywordCode((char*)z, i, tokenType)`——这就是去**关键字哈希表**里查"这个单词是不是关键字、是哪个 token 码"。关键字哈希的玄机,我们放到本章"技巧精解"里单独拆。这里先记住结论:**关键字识别 = 贪婪读完一个单词 + 一次哈希查找**,前者保证 `select_all` 这种带下划线的标识符不被错切成 `select` + `_all`,后者保证 O(1)。

> **钉死这件事**:SQLite 的 tokenizer 不是 `if-else` 链,而是**字符分类表 + 字符类跳表 switch**。`aiClass[]` 把任意字节 O(1) 映射到 31 个类,`switch(类)` O(1) 跳到对应处理逻辑。这套"查表 + 跳表"是嵌入式 SQL tokenizer 的标准做法,把每条 SQL 的切词延迟压到最低。

---

## 三、关键字哈希:`mkkeywordhash.c` 离线生成的最小化哈希表

切到一个"像关键字的单词"后,要做一次查找:它到底是关键字、还是用户定义的标识符?SQL 关键字有上百个(`SELECT`/`FROM`/`WHERE`/`JOIN`/`WINDOW`/`OVER`/`FILTER`/...),每次切词都要查一遍,这个查找必须极快。

### 提问:怎么存这上百个关键字?

朴素做法:运行时建一个普通哈希表,关键字当 key、token 码当 value,每次切到单词就算个哈希、查表。但这有两个问题:

- **冲突链**。普通哈希表对短字符串(关键字都是 2~10 个字母)哈希分布不均,容易撞链。
- **体积**。运行时建的哈希表要存字符串、存桶、存 next 指针,在嵌入式里每多一个字节都是负担。

### 不这样会怎样

> **不这样会怎样**:如果 SQLite 运行时建一张普通关键字哈希表,每次启动多花几 KB 内存,切词时还要处理冲突链(最坏退化成线性查)。SQLite 要嵌入手机、IoT,启动开销和内存占用都被盯得很死。

### 所以这样设计:离线生成最小完美哈希

SQLite 的做法是:**不在运行时建哈希表,而是在编译期用 `tool/mkkeywordhash.c` 这个独立程序,生成一张"最小化"的哈希表 + 查找函数,写进 `keywordhash.h`,再 `#include` 进 `tokenize.c`**。生成的代码长这样(简化示意,真实是机器生成的): [`tokenize.c#L137-L148`](../sqlite/src/tokenize.c#L137-L148)

```c
/*
** The sqlite3KeywordCode function looks up an identifier to determine if
** it is a keyword.  If it is a keyword, the token code of that keyword is 
** returned.  If the input is not a keyword, TK_ID is returned.
**
** The implementation of this routine was generated by a program,
** mkkeywordhash.c, located in the tool subdirectory of the distribution.
** The output of the mkkeywordhash.c program is written into a file
** named keywordhash.h and then included into this source file by
** the #include below.
*/
#include "keywordhash.h"
```

`mkkeywordhash.c` 干的事(见 [`tool/mkkeywordhash.c`](../sqlite/src/../tool/mkkeywordhash.c) 的 `Keyword` 结构和主循环):

1. **把所有关键字带进一个数组**。每个关键字记录它适用的编译开关(如 `ALTER` 在 `SQLITE_OMIT_ALTERTABLE` 下不存在),这样裁剪编译时关键字表自动变小: [`tool/mkkeywordhash.c#L34-L50`](../sqlite/src/../tool/mkkeywordhash.c#L34-L50)

```c
struct Keyword {
  char *zName;         /* 关键字名 */
  char *zTokenType;    /* 这个关键字对应的 token 码 */
  int mask;            /* 编译掩码,非 0 才编进来 */
  int priority;        /* 哈希链里排前的优先级 */
  int id;              /* 唯一 ID */
  int hash;            /* 哈希值 */
  int offset;          /* 名字串里的偏移 */
  int len;             /* 长度 */
  int prefix;          /* 前缀长度 */
  int longestSuffix;   /* 也是别的关键字前缀的最长后缀 */
  int iNext;           /* 同哈希链的下一个 */
  int substrId;        /* 嵌在哪个更长的关键字里 */
  int substrOffset;    /* 嵌入偏移 */
  char zOrigName[20];  /* 处理前的原名 */
};
```

2. **尝试不同的哈希函数,找到一个让"总槽位数 / 关键字数"尽量小、且每个槽的链尽量短(理想是 1)的函数**。这就是"最小完美哈希"(minimal perfect hash)的思想——O(1) 查找、零冲突(或极少冲突)。
3. **把所有关键字名拼成一个大字符串、共用内存**(用 `offset` 指向大串里的某段),避免每个关键字一个 `malloc`。
4. **生成 `keywordhash.h`**:一个 `aKeywordTable[]` 数组 + 一个 `keywordCode()` 函数,运行时一次哈希 + 一次数组下标 + (运气差时)几次链上比较,就给出 token 码。

> **所以这样设计**:关键字哈希**完全在编译期离线算好**,运行时没有任何建表开销,体积压到最小(共用字符串、最小桶数),查找 O(1)。这是 SQLite 在"上百个关键字 × 每次 prepare 都要查"这个高频操作上榨性能的典型招——**把能预算的全预算**。对照 MySQL 的词法器(运行时查普通哈希表,反正服务端不在乎那点开销),这是嵌入式思维和服务端思维的分野。

`keywordCode()` 这个函数被 `sqlite3GetToken` 的 `CC_KYWD0` case 调用(前面贴过),它的返回值要么是某个关键字的 token 码(`TK_SELECT`、`TK_FROM`...),要么是 `TK_ID`(不是关键字,当普通标识符)。**这就是关键字和标识符的根本区分点**——不是看字符,是查表。

---

## 四、Parser:为什么 SQLite 不手写,而用 parser generator

Tokenizer 把字符串切成 token 流,接下来交给 parser。这里 SQLite 做了一个和《Lua》那本截然不同的选型:**Lua 的 parser 是手写递归下降,SQLite 的 parser 是用工具从文法源生成的表驱动 LALR(1)**。为什么 SQLite 不也手写?

### 提问:为什么 SQL parser 适合用工具生成,而不是手写?

这要从 SQL 文法的特点说起。SQL 的文法有两个特征:

- **大**。SQLite 支持的 SQL 子集,光 `parse.y` 就 2163 行,几百条产生式(`SELECT`、`CREATE TABLE`、`CREATE INDEX`、`INSERT`、`UPDATE`、`DELETE`、`TRIGGER`、`VIEW`、`WITH`、`WINDOW`、`UPSERT`、`PRAGMA`、`VACUUM`、`ATTACH`、`SAVEPOINT`...)。手写递归下降,意味着为每个非终结符写一个 C 函数,几百个函数,维护噩梦。
- **规整**。SQL 是 SQL 标准定义的、几十年没大变(相比 Lua 那种小语言)。一旦写成文法源(`.y` 文件),后续加新特性(如 `UPSERT`、`WINDOW`)就是改文法 + 重跑生成器,比手写函数清爽得多。

### 不这样会怎样

> **不这样会怎样**:如果手写 SQL parser,① 几百个非终结符 → 几百个 C 函数,代码膨胀、维护成本高;② 表达式优先级(`+`、`*`、`AND`、`OR`、比较、`BETWEEN`、`IN`、`LIKE`...)的层次关系,手写要靠"函数调用嵌套顺序"表达,极易写错;③ SQL 语法演进频繁(每年加新特性),手写改动面大、易引 bug。**用一个工具从文法源生成,把"语法书"和"执行机"分离,是 SQL parser 的标准工程实践**——PostgreSQL、MySQL 用的也是 bison/yacc,SQLite 用的是自研的 Lemon。

### 所以这样设计:LALR(1) 表驱动 parser,文法源是 `parse.y`

SQLite 的 parser 工程链是这样的:

```
   src/parse.y  (人写的文法源,Lemon 文法 + 嵌入的 C 动作)
        │
        ▼  (运行 tool/lemon 生成器,构建期一次)
   parse.c  + parse.h  (生成的 C parser + token 码 #define)
        │
        ▼  (编译进 sqlite)
   sqlite3Parser()  (运行时被 tokenize.c 的 sqlite3RunParser 调用)
```

**`parse.y` 是人写的"语法书",`parse.c` 是机器按这本书造出来的"执行机"**。运行时,parser 是一个"读 token、查表、移进或归约"的循环——它不在源码里,在生成的 `parse.c` 里。这就是"表驱动 LALR(1)"和"手写递归下降"的根本区别:**手写的控制流写在 C 函数调用栈里(嵌套即语法),表驱动的控制流写在一张分析表里(查表决定移进/归约)**。

> **钉死这件事**:SQLite 的 parser **不是手写递归下降**。它是 Lemon 从 `src/parse.y` 生成的 **LALR(1) 表驱动 parser**。`parse.y` 在源码树里(2163 行,文法源),`parse.c` 是构建产物(可能在 `src/` 里也可能不在,取决于构建配置),`tokenize.c` 调用生成出来的 `sqlite3Parser()`。**别被某些资料误导成"SQLite 手写 parser",这是最常见的错**。

LALR(1) 表驱动 parser 的第一性原理(状态栈、移进 shift、归约 reduce、预测分析表、lookahead token)在《Lua》那本对照 yacc/bison 讲过,这里不重复。本章只讲 SQLite 选 **Lemon** 而非 bison/yacc 的理由——这是编译前端的关键选型,也是 SQLite 工程上最容易被忽略的硬核决策。

---

## 五、为什么自研 Lemon,而不是 bison/yacc

这是本章最值得讲透的一个"为什么"。SQLite 有现成的 bison/yacc 可用(它们是 1970 年代以来的标准 parser generator,GPL/BSD),却偏偏自研了 **Lemon**(D. Richard Hipp 自己写的)。为什么?

### 提问:bison/yacc 有什么不好,值得 Hipp 自己写一个?

bison/yacc 用了几十年,生成无数 parser(PostgreSQL、MySQL、GCC 早期、Bash...),成熟稳定。但 Hipp 在设计 SQLite 时,看出了它在 SQLite 这种"嵌入式、要安全、要可维护"的场景下的几个硬伤。Lemon 就是为修这些硬伤而生的。

### Lemon 比 bison/yacc 强在哪(五点)

**① 线程安全 / 可重入**。yacc 生成的 parser 用全局变量(`yy*` 一堆全局),不能在多线程里同时跑两个 parser。Lemon 生成的 parser 把所有状态塞进一个 `yyParser` 结构体,**parser 引擎对象可以分配在栈上**(SQLite 在 amalgamation 模式下用 `sqlite3Parser_ENGINEALWAYSONSTACK` 把整个 parser 放栈上,零 malloc): [`parse.y#L90-L100`](../sqlite/src/parse.y#L90-L100)

```c
#ifdef SQLITE_AMALGAMATION
# define sqlite3Parser_ENGINEALWAYSONSTACK 1
#endif
```

这意味着 SQLite 可以在多线程里同时 prepare 多条 SQL,互不干扰——这是嵌入式(一个 App 多线程用 SQLite)必须的。

> **不这样会怎样**:如果用 yacc,要么每个连接 clone 一份 parser 全局(浪费)、要么加锁串行化 prepare(慢)。Lemon 的可重入设计,让 SQLite 的多线程 prepare 天然安全,零额外开销。

**② 严格的冲突检测(Lemon 遇到文法冲突直接报错,yacc 只警告)**。这是 Lemon 最硬核的一点。LALR(1) 文法可能有"移进-归约冲突"(shift-reduce conflict)或"归约-归约冲突"(reduce-reduce conflict)——同一个状态下,看到同一个 lookahead token,既可移进也可归约,parser 不知道该怎么办。yacc/bison 遇到冲突**默认只打警告**,然后按"默认移进"硬选一个,生成的 parser 照样跑(但在某些输入上会选错,产出错误的 AST)。**Lemon 遇到冲突直接报错、拒绝生成 parser**,逼文法作者把文法改成无冲突。

这是 SQLite 几十年几乎零语法 bug 的根本原因之一——**文法从源头就是无歧义的**。Hipp 在 Lemon 文档里明确说过:yacc 的"默认移进"是个陷阱,让语法 bug 潜伏几年才被发现,Lemon 不留这个后门。

**③ 比 yacc 友好得多的错误信息**。SQL 写错了(如 `SELEC * FROM t`),parser 要给用户一个有用的报错("near \"SELEC\": syntax error"),而不是"internal parser error"。Lemon 生成的 parser 内置了更好的错误恢复和定位,SQLite 的 `%syntax_error` 动作就挂在 `parse.y` 里: [`parse.y#L43-L55`](../sqlite/src/parse.y#L43-L55)

```c
%syntax_error {
  UNUSED_PARAMETER(yymajor);  /* 消编译警告 */
  if( TOKEN.z[0] ){
    parserSyntaxError(pParse, &TOKEN);
  }else{
    sqlite3ErrorMsg(pParse, "incomplete input");
  }
}
%stack_overflow {
  if( pParse->nErr==0 ) sqlite3ErrorMsg(pParse, "Recursion limit");
}
```

**④ 显式的可裁剪(`%ifndef` / `%endif`)**。SQLite 支持编译时裁剪(关掉 `SQLITE_OMIT_COMPOUND_SELECT` 就不要 UNION/INTERSECT),文法源里用 `%ifndef ... %endif` 把可裁剪的产生式包起来,Lemon 据此生成对应版本的 parser: [`parse.y#L280-L282`](../sqlite/src/parse.y#L280-L282)

```c
%ifdef SQLITE_OMIT_COMPOUND_SELECT
... (省略 UNION 相关产生式)
%endif SQLITE_OMIT_COMPOUND_SELECT
```

这让 SQLite 能裁成几百 KB 的精简版(只留 SELECT/INSERT/UPDATE/DELETE),嵌入 IoT。yacc 没有这套机制。

**⑤ 类型安全的语义值**。yacc 的语义栈是 `YYSTYPE` 联合体,所有非终结符共享一个类型,容易写错(把 `Expr*` 当 `Select*` 用)。Lemon 允许**每个非终结符声明自己的 C 类型**,生成器在动作代码里做类型检查: [`parse.y#L530-L535`](../sqlite/src/parse.y#L530-L535)

```c
%type select {Select*}
%destructor select {sqlite3SelectDelete(pParse->db, $$);}
%type selectnowith {Select*}
%destructor selectnowith {sqlite3SelectDelete(pParse->db, $$);}
%type oneselect {Select*}
%destructor oneselect {sqlite3SelectDelete(pParse->db, $$);}
```

注意每个 `%type` 还配一个 `%destructor`——如果 parser 因为语法错误丢弃某个语义值,Lemon 自动调 destructor 释放内存,**杜绝内存泄漏**。yacc 没这个。

> **所以这样设计**:Lemon 是 Hipp 专门为 SQLite 这种"嵌入式、多线程、零容忍语法 bug、要可裁剪"的场景设计的 parser generator。它修了 yacc 的线程不安全、冲突默认移进、类型弱、不可裁剪四大硬伤。**这是 SQLite 编译前端最关键的工程决策**——选对了工具,几十年的语法稳定性、多线程安全、可裁剪都有了根基。理解 Lemon,就理解了 SQLite 为什么这么稳。

> **钉死这件事**:SQLite 的 parser generator 是 **Lemon(LALR(1),自研)**,不是 bison/yacc。Lemon 三大杀器:**可重入(线程安全)**、**遇冲突直接报错而非默认移进(语法从源头无歧义)**、**每非终结符独立类型 + 自动 destructor(类型安全 + 防内存泄漏)**。这是 SQLite 编译前端的脊梁。

---

## 六、`parse.y` 里的真实文法:SELECT 语句怎么落地成 AST

抽象讲完 LALR 和 Lemon,现在贴真实的 `parse.y` 文法,看一条 `SELECT` 在文法里长什么样、parser 怎么按它把 token 拼成 AST。

### SELECT 的顶层产生式

一条 SELECT 在文法里,从顶层 `cmd`(命令)一层层归约下来: [`parse.y#L518-L528`](../sqlite/src/parse.y#L518-L528)

```c
//////////////////////// The SELECT statement /////////////////////////////////
cmd ::= select(X).  {
  SelectDest dest = {SRT_Output, 0, 0, 0, 0, 0, 0};
  if( (pParse->db->mDbFlags & DBFLAG_EncodingFixed)!=0
   || sqlite3ReadSchema(pParse)==SQLITE_OK
  ){
    sqlite3Select(pParse, X, &dest);   // ← 把 AST 交给 code generator(下一章)
  }
  sqlite3SelectDelete(pParse->db, X);
}
```

这就是顶层:一个 `cmd`(命令)可以是一个 `select`。归约到这里时,`select` 已经是一棵 `Select*` 树(存在语义值 `X` 里),`sqlite3Select(pParse, X, &dest)` 是把树交给 Code Generator 的入口(下一章 P1-04 详讲)。注意 `sqlite3SelectDelete`——AST 用完要释放,这就是 Lemon 的 `%destructor` 之外的显式释放点。

`select` 这个非终结符,本身又有几条产生式(WITH 子句、不带 WITH): [`parse.y#L608-L619`](../sqlite/src/parse.y#L608-L619)

```c
%ifndef SQLITE_OMIT_CTE
select(A) ::= WITH wqlist(W) selectnowith(X). {A = attachWithToSelect(pParse,X,W);}
select(A) ::= WITH RECURSIVE wqlist(W) selectnowith(X).
                                              {A = attachWithToSelect(pParse,X,W);}
%endif
select(A) ::= selectnowith(A). {
  Select *p = A;
  if( p ){ parserDoubleLinkSelect(pParse, p); }   // 把 compound SELECT 拉成双向链表
}
```

`selectnowith` 又分单条(`oneselect`)和复合(`UNION`/`INTERSECT`/`EXCEPT`): [`parse.y#L621-L649`](../sqlite/src/parse.y#L621-L649)

```c
selectnowith(A) ::= oneselect(A).
%ifndef SQLITE_OMIT_COMPOUND_SELECT
selectnowith(A) ::= selectnowith(A) multiselect_op(Y) oneselect(Z).  {
  // UNION/INTERSECT/EXCEPT 复合:把右边的 oneselect 挂到左边链表上
  Select *pRhs = Z;
  Select *pLhs = A;
  ...
  if( pRhs ){
    pRhs->op = (u8)Y;            // 记下是 UNION 还是 INTERSECT
    pRhs->pPrior = pLhs;         // 用 pPrior 串成链
    ...
  }
  A = pRhs;
}
multiselect_op(A) ::= UNION(OP).             {A = @OP;}
multiselect_op(A) ::= UNION ALL.             {A = TK_ALL;}
multiselect_op(A) ::= EXCEPT|INTERSECT(OP).  {A = @OP;}
%endif
```

注意 `pRhs->pPrior = pLhs` 这一行——**复合 SELECT 用 `Select.pPrior` 串成单向链表**(`parserDoubleLinkSelect` 再补一个 `pNext` 成双向),而不是嵌套成树。这是 `Select` 结构的一个设计点(下面讲 AST 节点时会回扣)。

最核心的一条——**单条 SELECT 的产生式**: [`parse.y#L651-L667`](../sqlite/src/parse.y#L651-L667)

```c
oneselect(A) ::= SELECT distinct(D) selcollist(W) from(X) where_opt(Y)
                 groupby_opt(P) having_opt(Q) 
                 orderby_opt(Z) limit_opt(L). {
  A = sqlite3SelectNew(pParse,W,X,Y,P,Q,Z,D,L);
}
```

这一行文法就是整条 SELECT 的骨架!读出来就是:`SELECT [DISTINCT|ALL] <列列表> FROM <表列表> [WHERE <表达式>] [GROUP BY <列表>] [HAVING <表达式>] [ORDER BY <列表>] [LIMIT <表达式>]`。归约时调 `sqlite3SelectNew`,把这八个部件拼成一个 `Select` 节点。注意所有可选部件(`where_opt`、`groupby_opt`、`orderby_opt`、`limit_opt`)在文法里都有"空产生式"——没有 WHERE 子句时 `where_opt` 归约成 `0`(NULL),`sqlite3SelectNew` 看到 NULL 就把 `pWhere` 置空。

`sqlite3SelectNew` 的实现在 `select.c`,就是把八个部件填进 `Select` 结构: [`src/select.c#L126-L176`](../sqlite/src/select.c#L126-L176)

```c
Select *sqlite3SelectNew(
  Parse *pParse, ExprList *pEList, SrcList *pSrc, Expr *pWhere,
  ExprList *pGroupBy, Expr *pHaving, ExprList *pOrderBy,
  u32 selFlags, Expr *pLimit
){
  Select *pNew, *pAllocated;
  Select standin;
  pAllocated = pNew = sqlite3DbMallocRawNN(pParse->db, sizeof(*pNew));
  if( pNew==0 ){ pNew = &standin; }              // OOM 时用栈上 standin 兜底
  if( pEList==0 ){
    pEList = sqlite3ExprListAppend(pParse, 0, sqlite3Expr(pParse->db,TK_ASTERISK,0));
  }                                              // 没有 SELECT 列表 → 默认 SELECT *
  pNew->pEList = pEList;
  pNew->op = TK_SELECT;
  pNew->selFlags = selFlags;
  pNew->pSrc = pSrc;
  pNew->pWhere = pWhere;
  pNew->pGroupBy = pGroupBy;
  pNew->pHaving = pHaving;
  pNew->pOrderBy = pOrderBy;
  pNew->pLimit = pLimit;
  ...
  return pAllocated;
}
```

注意那个 `if( pEList==0 ){ ... TK_ASTERISK ... }`——**`SELECT * FROM t` 里的 `*` 不是文法特殊符号,而是 `sqlite3SelectNew` 在没有列列表时,默认塞一个 `TK_ASTERISK` 类型的 `Expr`**。这就是为什么 `*` 在后续阶段能像普通表达式一样处理(展开成所有列)。

### 表达式优先级:`a+b*c` 凭什么树根是 `+`

这是读者最关心的点。SQL 表达式有十几层优先级(从低到高大致是 `OR`、`AND`、`NOT`、比较 `=`/`<`/`>`、`BETWEEN`/`IN`/`LIKE`、`+`/`-`、`*`/`/`、一元 `-`/`~`、字面量),LALR 文法怎么表达?

SQLite 的做法是**用多层非终结符 + 不同的产生式层**来编码优先级。看 `parse.y` 的表达式段: [`parse.y#L1175-L1359`](../sqlite/src/parse.y#L1175-L1359)

```c
expr(A) ::= term(A).                                       // term 提升为 expr
expr(A) ::= LP expr(X) RP.            {A = X;}             // 括号
expr(A) ::= idj(X).          {A=tokenExpr(pParse,TK_ID,X);} // 标识符
...
term(A) ::= NULL|FLOAT|BLOB(X). {A=tokenExpr(pParse,@X,X);} // 字面量
term(A) ::= STRING(X).          {A=tokenExpr(pParse,@X,X);}
term(A) ::= INTEGER(X). { ... }
...
expr(A) ::= expr(A) AND expr(Y).        {A=sqlite3ExprAnd(pParse,A,Y);}
expr(A) ::= expr(A) OR(OP) expr(Y).     {A=sqlite3PExpr(pParse,@OP,A,Y);}
expr(A) ::= expr(A) LT|GT|GE|LE(OP) expr(Y).  {A=sqlite3PExpr(pParse,@OP,A,Y);}
expr(A) ::= expr(A) EQ|NE(OP) expr(Y).  {A=sqlite3PExpr(pParse,@OP,A,Y);}
expr(A) ::= expr(A) BITAND|BITOR|LSHIFT|RSHIFT(OP) expr(Y). {A=sqlite3PExpr(pParse,@OP,A,Y);}
expr(A) ::= expr(A) PLUS|MINUS(OP) expr(Y).  {A=sqlite3PExpr(pParse,@OP,A,Y);}
expr(A) ::= expr(A) STAR|SLASH|REM(OP) expr(Y). {A=sqlite3PExpr(pParse,@OP,A,Y);}
```

看起来 `+` 和 `*` 用的是同一种产生式形式(`expr ::= expr OP expr`),那 LALR 怎么知道 `*` 比 `+` 紧?**靠的是 Lemon 的"优先级声明"**——Lemon 允许在文法里用 `[TOKEN]` 给产生式打优先级标记,以及在文件开头用 `%left`/`%right`/`%nonassoc` 声明 token 的结合性和优先级。看 `parse.y` 末尾(以及散布在产生式里的 `[BITNOT]`、`[IN]`、`[LIKE_KW]`、`[BETWEEN]` 标记): [`parse.y#L1452-L1452`](../sqlite/src/parse.y#L1452)

```c
expr(A) ::= PLUS|MINUS(B) expr(X). [BITNOT] {    // 一元 +/-,优先级标 [BITNOT]
  Expr *p = X;
  u8 op = @B + (TK_UPLUS-TK_PLUS);
  ...
}
```

那个 `[BITNOT]` 就是告诉 Lemon:"这条产生式的优先级等于 `BITNOT` 这个 token 的优先级"(高,因为一元运算符紧)。而 `expr(A) ::= expr(A) STAR|SLASH|REM(OP) expr(Y).` 这条没有显式标记,Lemon 用**这条产生式里最右边的终结符**(`STAR`/`SLASH`/`REM`)的优先级——这些在 `parse.y` 里通过 `%left STAR SLASH REM` 等声明,优先级比 `PLUS`/`MINUS` 高一级。于是 LALR 在遇到 `a + b * c` 时,看到 `*` 的 lookahead,因为 `*` 优先级高于正在归约的 `+`,选择**移进** `*`(先归约 `b*c`),最终树根是 `+`。

> **钉死这件事**:SQLite 表达式优先级不是手写"先乘除后加减"的 if 判断,而是**文法里多层产生式 + Lemon 的 `%left`/`%right` 优先级声明 + 产生式上的 `[TOKEN]` 优先级标记**,三者共同让 LALR 分析表自动算出"遇到 `*` 时该移进还是归约"。这是表驱动 parser 用文法编码优先级的标准做法——和手写递归下降里"先 parse 乘除、再 parse 加减"的函数嵌套是同一回事的两种表达。

### `a+b*c` 的 AST 长这样

`a+b*c` 归约完后,产物是一棵 `Expr` 树(`sqlite3PExpr` 每次新建一个内部节点,左右子树是 `pLeft`/`pRight`):

```
              Expr(TK_PLUS)              ← 树根(a+b*c 的 +)
             /            \
   Expr(TK_ID,"a")    Expr(TK_STAR)     ← b*c 的 *
                       /          \
             Expr(TK_ID,"b")   Expr(TK_ID,"c")
```

每个 `Expr(TK_ID,...)` 是叶子(变量引用),`TK_PLUS`/`TK_STAR` 是内部节点(二元运算)。**树根是 `TK_PLUS`,天然编码了"先算 `b*c` 再加 `a`"**——这是 AST 比中缀表达式字符串优越的地方:优先级已经在树的结构里了,后续 code generator 遍历树时不用再判断优先级。

`Expr` 结构的精妙,我们在讲 AST 节点那节单独拆(它有个全 SQLite 最省内存的技巧:`EP_Reduced`)。

---

## 七、AST 节点:`Token` / `Expr` / `Select` / `SrcList` 为什么这么设计

parser 的产物是 AST。SQLite 的核心 AST 节点有四个,全定义在 [`src/sqliteInt.h`](../sqlite/src/sqliteInt.h):

### `Token`:零拷贝的字符串切片

最底层的节点是 `Token`——一个 token 在内存里的样子: [`sqliteInt.h#L2887-L2890`](../sqlite/src/sqliteInt.h#L2887-L2890)

```c
struct Token {
  const char *z;     /* token 的文本。注意:不以 \0 结尾! */
  unsigned int n;    /* token 的字符数 */
};
```

就两个字段:一个指针、一个长度。**关键在注释:"z 指向的内存不归 Token 所有,z 通常指向原始 SQL 字符串中间的某个位置"**。这是零拷贝——tokenizer 切词时,不复制每个 token 的文本,而是让 `Token.z` 直接指向输入 SQL 字符串里那段字节,`Token.n` 记长度。parser 在归约时,把 `Token` 当语义值传来传去,全程不分配新字符串。

> **不这样会怎样**:如果每个 token 都 `malloc` 一份文本拷贝,一条 1KB 的 SQL 切出几百个 token,就要几百次 `malloc`/`free`——在嵌入式里这是灾难。零拷贝 `Token` 把这个开销压到零,只有真正需要"独立的、可修改的字符串"(如去引号后的标识符名)时,才在 `tokenExpr` 里 `sqlite3DbMallocRawNN(... sizeof(Expr)+t.n+1)` 一次性把 `Expr` 和它的 `zToken` 一起分配(见下文 Expr)。

### `Expr`:表达式树节点,带截断标志的省内存设计

表达式树的每个节点是 `Expr`。这是 SQLite 里**最复杂的 AST 节点**,也是省内存技巧最密集的地方: [`sqliteInt.h#L3039-L3101`](../sqlite/src/sqliteInt.h#L3039-L3101)

```c
struct Expr {
  u8 op;                 /* 节点的操作码,如 TK_PLUS / TK_ID / TK_SELECT */
  char affExpr;          /* affinity 或 RAISE 类型 */
  u8 op2;                /* 备用操作码(多用途,见注释) */
#ifdef SQLITE_DEBUG
  u8 vvaFlags;           /* 调试验证标志 */
#endif
  u32 flags;             /* 各种 EP_* 标志(见下) */
  union {
    char *zToken;          /* 字面量/变量/函数名:token 文本,已去引号,\0 结尾 */
    int iValue;            /* EP_IntValue 时:直接存整数值(省掉 zToken) */
  } u;

  /* --- EP_TokenOnly 标志置位时,下面这些字段不分配内存 --- */

  Expr *pLeft;           /* 左子树 */
  Expr *pRight;          /* 右子树 */
  union {
    ExprList *pList;     /* IN/EXISTS/SELECT/CASE/FUNCTION/BETWEEN:参数列表 */
    Select *pSelect;     /* EP_xIsSelect 时:子查询 */
  } x;

  /* --- EP_Reduced 标志置位时,下面这些字段也不分配内存 --- */

#if SQLITE_MAX_EXPR_DEPTH>0
  int nHeight;           /* 这棵子树的高度 */
#endif
  int iTable;            /* TK_COLUMN:游标号;TK_REGISTER:寄存器号;... */
  ynVar iColumn;         /* TK_COLUMN:列号(-1 是 rowid);TK_VARIABLE:变量号 */
  i16 iAgg;              /* 聚合信息索引 */
  union {
    int iJoin;             /* EP_OuterON/EP_InnerON:右表号 */
    int iOfst;             /* 否则:token 起始偏移 */
  } w;
  AggInfo *pAggInfo;     /* TK_AGG_COLUMN/TK_AGG_FUNCTION 的聚合信息 */
  union {
    Table *pTab;           /* TK_COLUMN:列所在的表 */
    Window *pWin;          /* EP_WinFunc:窗口/过滤定义 */
    int nReg;              /* TK_NULLS:要清零的寄存器数 */
    struct {               /* TK_IN/TK_SELECT/TK_EXISTS:子程序地址 */
      int iAddr;
      int regReturn;
    } sub;
  } y;
};
```

注意结构体中间那两行注释:`/* EP_TokenOnly 标志置位时,下面不分配 */` 和 `/* EP_Reduced 标志置位时,下面也不分配 */`。这是 Expr 最精妙的设计——**同一个 `Expr` 类型,根据 flag 占用不同大小的内存**:

- **全尺寸(`EP_Reduced` 和 `EP_TokenOnly` 都不设)**:完整结构体,约 80 字节。新建的表达式、code generator 工作时的表达式用这个。
- **`EP_Reduced`**:砍掉 `nHeight`、`iTable`、`iColumn`、`iAgg`、`w`、`pAggInfo`、`y` 这些"分析/执行阶段才用"的字段(`EXPR_REDUCEDSIZE` = `offsetof(Expr, iTable)`),约省 40%。存在 schema 里的 CHECK 约束、生成列表达式用这个(它们只需要 op/zToken/pLeft/pRight,不需要游标号那些)。
- **`EP_TokenOnly`**:再砍掉 `pLeft`、`pRight`、`x`(`EXPR_TOKENONLYSIZE` = `offsetof(Expr, pLeft)`),只剩 op/affExpr/op2/flags/u,约 16 字节。这是"列表里没接上的叶子节点"用的极致省内存形态。

> **不这样会怎样**:SQLite 的 schema 里可能存成千上万个 `Expr`(每张表的 CHECK 约束、每个生成列、每个索引上的表达式、每个视图的定义...)。如果每个 `Expr` 都占满 80 字节,一个有几百个表、每表几十个 CHECK 的 schema,光 Expr 就几 MB——嵌入式设备扛不住。`EP_Reduced`/`EP_TokenOnly` 让 schema 缓存里的 `Expr` 平均砍到 40~16 字节,**省下一半以上的内存**。这是 SQLite 在 AST 节点上做的极致省内存设计,值得单独放进"技巧精解"。

`Expr.op` 是节点的"操作码",**直接复用 tokenizer 的 token 码**(`TK_PLUS`、`TK_ID`、`TK_SELECT`...),这是 `parse.y` 注释里明确说的: [`sqliteInt.h#L2976-L2982`](../sqlite/src/sqliteInt.h#L2976-L2982)

> Expr.op is the opcode. The integer parser token codes are reused as opcodes here. For example, the parser defines TK_GE to be an integer code representing the ">=" operator. This same integer code is reused to represent the greater-than-or-equal-to operator in the expression tree.

所以"TK_PLUS"既是 tokenizer 产出的 token 类型,也是 `Expr` 树节点的操作码——一套码表,从字符到树到(后续的)opcode 一以贯之。

`Expr.u.zToken` 存文本。注意 `tokenExpr`(parser 里建叶子 `Expr` 的函数)是这样分配的: [`parse.y#L1140-L1171`](../sqlite/src/parse.y#L1140-L1171)

```c
static Expr *tokenExpr(Parse *pParse, int op, Token t){
  Expr *p = sqlite3DbMallocRawNN(pParse->db, sizeof(Expr)+t.n+1);  // Expr + 字符串一起分配
  if( p ){
    p->op = (u8)op;
    ...
    p->u.zToken = (char*)&p[1];          // zToken 指向 Expr 之后的内存
    memcpy(p->u.zToken, t.z, t.n);
    p->u.zToken[t.n] = 0;                 // \0 结尾
    ...
  }
  return p;
}
```

`sizeof(Expr)+t.n+1` 一次 `malloc`,把 `Expr` 结构体和它的 `zToken` 字符串**塞在同一块内存**里(`zToken` 指向 `&p[1]`)。这是 SQLite 普遍的省分配技巧——**一次 malloc 拿结构体 + 变长尾数据**,避免"malloc 结构体 + malloc 字符串"两次分配。对照 LevelDB 的 Slice 也是类似思路(指针 + 长度,不拥有内存),但 SQLite 更进一步:需要拥有内存时,把结构和字符串合并成一次分配。

### `Select`:查询树,用链表而非嵌套表达复合查询

一条 SELECT 的 AST 根节点是 `Select`: [`sqliteInt.h#L3597-L3617`](../sqlite/src/sqliteInt.h#L3597-L3617)

```c
struct Select {
  u8 op;                 /* TK_UNION / TK_ALL / TK_INTERSECT / TK_EXCEPT(复合时) */
  LogEst nSelectRow;     /* 估计的结果行数 */
  u32 selFlags;          /* SF_* 标志(SF_Distinct / SF_Aggregate / ...) */
  int iLimit, iOffset;   /* LIMIT/OFFSET 计数器的寄存器 */
  u32 selId;             /* 这个 SELECT 的唯一 ID */
  ExprList *pEList;      /* 结果列(SELECT a, b 里的 a, b) */
  SrcList *pSrc;         /* FROM 子句 */
  Expr *pWhere;          /* WHERE 子句 */
  ExprList *pGroupBy;    /* GROUP BY */
  Expr *pHaving;         /* HAVING */
  ExprList *pOrderBy;    /* ORDER BY */
  Select *pPrior;        /* 复合 SELECT 里的前一个(链表) */
  Select *pNext;         /* 复合 SELECT 里的后一个(双向链表) */
  Expr *pLimit;          /* LIMIT 表达式 */
  With *pWith;           /* WITH 子句(CTE) */
#ifndef SQLITE_OMIT_WINDOWFUNC
  Window *pWin;          /* 窗口函数列表 */
  Window *pWinDefn;      /* 命名窗口定义列表 */
#endif
};
```

每个字段直接对应 SQL 的一个子句——`pEList` 是 SELECT 列表、`pSrc` 是 FROM、`pWhere` 是 WHERE...这套"一个子句一个字段"的设计,让 code generator 处理 SELECT 时极其清爽:`SELECT` 子句遍历 `pEList`,`FROM` 子句遍历 `pSrc`,`WHERE` 子句求值 `pWhere`,各管各的。

注意 `op` 和 `pPrior`/`pNext`:**复合查询(`A UNION B UNION C`)不是嵌套成树,而是用 `pPrior`/`pNext` 串成双向链表**,每个 `Select` 节点的 `op` 标它和前一个的连接方式(`TK_UNION`/`TK_INTERSECT`...)。这是前面 `parse.y` 里 `parserDoubleLinkSelect` 干的事。为什么用链表不用树?因为复合查询的语义是"从左到右依次合并",链表遍历比树遍历直观,且 code generator 处理时可以线性走完。

### `SrcList`:FROM 子句的扁平数组

FROM 子句(`FROM t1 JOIN t2 ON ... JOIN t3 USING(...)`)在 AST 里是一个 `SrcList`: [`sqliteInt.h#L3425-L3429`](../sqlite/src/sqliteInt.h#L3425-L3429)

```c
struct SrcList {
  int nSrc;             /* FROM 里表/子查询的个数 */
  u32 nAlloc;           /* a[] 已分配的槽数 */
  SrcItem a[FLEXARRAY]; /* 每个表/子查询一项(柔性数组) */
};
```

**不是链表,是柔性数组**(`FLEXARRAY` 是 C 的灵活数组成员)。每个 `SrcItem` 描述一个表源: [`sqliteInt.h#L3361-L3407`](../sqlite/src/sqliteInt.h#L3361-L3407)

```c
struct SrcItem {
  char *zName;      /* 表名 */
  char *zAlias;     /* "A AS B" 里的别名 B */
  Table *pSTab;     /* zName 对应的 Table 对象 */
  struct {
    u8 jointype;             /* 和前一个表的连接类型 */
    unsigned notIndexed :1;  /* 有 NOT INDEXED */
    unsigned isIndexedBy :1; /* 有 INDEXED BY */
    unsigned isSubquery :1;  /* 这项是子查询 */
    unsigned isTabFunc :1;   /* 表值函数语法 */
    unsigned isCorrelated :1;
    unsigned isMaterialized :1;
    unsigned viaCoroutine :1;
    unsigned isRecursive :1; /* WITH RECURSIVE 的递归引用 */
    unsigned fromDDL :1;
    unsigned isCte :1;       /* 是 CTE */
    ... 十几个位域 ...
  } fg;
  int iCursor;      /* VDBE 访问这个表的游标号 */
  Bitmask colUsed;  /* 第 N 位列被引用时,第 N 位置 1 */
  union { char *zIndexedBy; ExprList *pFuncArg; u32 nRow; } u1;
  union { Index *pIBIndex; CteUse *pCteUse; } u2;
  union { Expr *pOn; IdList *pUsing; } u3;   /* ON 或 USING 子句 */
  union { Schema *pSchema; char *zDatabase; Subquery *pSubq; } u4;
};
```

为什么用柔性数组不用链表?因为 FROM 子句的表源数量在建好后基本不变,而且 code generator 要**频繁按下标随机访问**(JOIN 的嵌套循环要按顺序取每个表)。柔性数组既省了每个节点的 next 指针,又给了 O(1) 随机访问——这是 AST 节点选数据结构时的典型权衡(树/链表适合动态增删,数组适合静态遍历)。

注意 `SrcItem` 里那一大堆位域(`fg.xxx :1`)——SQLite 在 AST 节点上**疯狂用位域省内存**,一个 `SrcItem` 才几十字节,却塞进了十几个布尔标志。这是嵌入式数据库在 AST 设计上一贯的取舍:**宁可在结构体里塞位域和 union,也不浪费一个字节**。

> **钉死这件事**:SQLite 的四个核心 AST 节点各有省内存的招——`Token` 零拷贝(指针指向输入串)、`Expr` 三档截断(全尺寸/Reduced/TokenOnly)、`Select` 用链表表达复合(扁平易遍历)、`SrcList` 用柔性数组(FROM 静态适合数组)。这套设计是 SQLite 在"AST 节点数量巨大(schema 里成千上万)、嵌入式内存紧张"双重压力下的最优解,值得每个数据库内核工程师细读。

---

## 八、一个完整例子:`SELECT a+b*c FROM t WHERE x>0` 三层串起来

把前面讲的三层(tokenize → parse → AST)用一个完整例子串起来。输入 `SELECT a+b*c FROM t WHERE x>0`:

### 第一层:Tokenizer 切词

`sqlite3RunParser`(`tokenize.c` 的主循环)逐字符调 `sqlite3GetToken`,产出 token 流: [`tokenize.c#L645-L718`](../sqlite/src/tokenize.c#L645-L718)

```c
while( 1 ){
  n = sqlite3GetToken((u8*)zSql, &tokenType);   // 切一个 token
  mxSqlLen -= n;
  if( mxSqlLen<0 ){ pParse->rc = SQLITE_TOOBIG; break; }   // SQL 太长
  if( tokenType>=TK_SPACE ){                                  // 特殊 token 处理
    if( AtomicLoad(&db->u1.isInterrupted) ){ ... break; }    // 被中断
    if( tokenType==TK_SPACE ){ zSql += n; continue; }        // 跳过空白
    if( zSql[0]==0 ){ ... }                                   // 输入结束,补 TK_SEMI + 0
    ...
  }
  pParse->sLastToken.z = zSql;                                // 记 token 文本(零拷贝)
  pParse->sLastToken.n = (u32)n;
  sqlite3Parser(pEngine, tokenType, pParse->sLastToken);      // ★ 喂给 parser
  lastTokenParsed = tokenType;
  zSql += n;
  if( pParse->rc!=SQLITE_OK ) break;
}
```

注意 `sqlite3Parser(pEngine, tokenType, pParse->sLastToken)` 这一行——**tokenizer 和 parser 是"生产者-消费者"关系,tokenize 切一个就喂一个给 parser,parser 立刻按 LALR 表移进或归约**。不是先切完所有 token 再 parse,而是**边切边 parse**。这是 LALR 表驱动 parser 的标准用法,省去了存整个 token 流的内存。

切词结果(token 流):

```
SELECT  →  TK_SELECT    (关键字,keywordCode 查到)
a       →  TK_ID        (标识符,keywordCode 查不到)
+       →  TK_PLUS      (CC_PLUS 类)
b       →  TK_ID
*       →  TK_STAR      (CC_STAR 类)
c       →  TK_ID
FROM    →  TK_FROM
t       →  TK_ID
WHERE   →  TK_WHERE
x       →  TK_ID
>       →  TK_GT        (CC_GT 类,无 '=' 前瞻)
0       →  TK_INTEGER
(EOF)   →  补 TK_SEMI + 0
```

注意 `a`、`b`、`c`、`t`、`x` 都被 `keywordCode` 判成 `TK_ID`(不是关键字),`SELECT`/`FROM`/`WHERE` 被判成对应关键字 token 码。这就是"关键字 vs 标识符"的分水岭——**查哈希表**。

### 第二层:Parser 按 `parse.y` 归约

parser 拿到 token 流,按 LALR 表一步步移进(shift)/归约(reduce)。整个归约序列很长(每个 token 移进、每条文法归约都是一步),这里只画关键归约点:

1. `a` 移进 → `idj` → 归约成 `expr`(via `expr(A) ::= idj(X). {A=tokenExpr(pParse,TK_ID,X)}`),产出一个 `Expr(TK_ID, "a")`。
2. `+` 移进(等着右边的 `expr`)。
3. `b` 移进 → 归约成 `Expr(TK_ID,"b")`。
4. `*` 移进——**这里 LALR 看到后面是 `*`(优先级高于当前的 `+`),选择移进而非先把 `a + b` 归约**,所以 `+` 先挂起。
5. `c` 移进 → 归约成 `Expr(TK_ID,"c")`。
6. 看到 `FROM`(优先级低于 `*`),触发归约:`Expr(TK_ID,"b")` 和 `Expr(TK_ID,"c")` 按 `expr ::= expr STAR expr` 归约,调 `sqlite3PExpr(pParse, TK_STAR, b, c)`,产出 `Expr(TK_STAR, pLeft=b, pRight=c)`。
7. 继续归约:`a` 和上一步的 `b*c` 按 `expr ::= expr PLUS expr`,调 `sqlite3PExpr(pParse, TK_PLUS, a, b*c)`,产出 `Expr(TK_PLUS, pLeft=a, pRight=b*c)`。
8. `FROM` 移进 → `t` 移进 → 归约成 `seltablist`(via `seltablist(A) ::= stl_prefix(A) nm(Y) dbnm(D) as(Z) on_using(N).`),产出含一个 `SrcItem{zName="t"}` 的 `SrcList`。
9. `WHERE` 移进 → `x` 归约成 `expr` → `>` 移进 → `0` 归约成 `expr(TK_INTEGER,"0")` → 按 `expr ::= expr GT expr` 归约成 `Expr(TK_GT, pLeft=x, pRight=0)`。这就是 `where_opt`。
10. 最终按 `oneselect(A) ::= SELECT distinct(D) selcollist(W) from(X) where_opt(Y) ...`,调 `sqlite3SelectNew(pParse, W=[a+b*c], X=[t], Y=x>0, ...)`,产出一个 `Select` 节点。

### 第三层:产出的 AST

最终 AST(简化框图):

```
                       Select
                       ├── op = TK_SELECT
                       ├── pEList (SELECT 列):  ExprList
                       │     └── [0].pExpr:  Expr(TK_PLUS)
                       │                       ├── pLeft:  Expr(TK_ID, "a")
                       │                       └── pRight: Expr(TK_STAR)
                       │                                     ├── pLeft:  Expr(TK_ID, "b")
                       │                                     └── pRight: Expr(TK_ID, "c")
                       ├── pSrc (FROM):  SrcList
                       │     └── a[0]: SrcItem { zName = "t" }
                       ├── pWhere:  Expr(TK_GT)
                       │             ├── pLeft:  Expr(TK_ID, "x")
                       │             └── pRight: Expr(TK_INTEGER, "0")
                       ├── pGroupBy = NULL
                       ├── pHaving   = NULL
                       ├── pOrderBy  = NULL
                       └── pLimit    = NULL
```

这棵树就是 Code Generator 的输入。下一章 P1-04 会讲 code generator 怎么遍历这棵树,产出 `OpenRead`/`Column`/`ResultRow` 这样的 VDBE opcode。

> **钉死这件事**:`SELECT a+b*c FROM t WHERE x>0` 在 SQLite 编译前端的全过程是——**tokenizer 用字符分类表逐字符切出 13 个 token → parser 边收 token 边按 LALR 表移进/归约 → 产出一棵 `Select` 树,树的 `pEList` 里挂着 `a+(b*c)` 的 `Expr` 树,`pWhere` 里挂着 `x>0` 的 `Expr` 树**。这棵树是后续所有优化、代码生成、执行的基础。理解这棵树怎么长出来,就理解了 SQL 编译前端。

---

## 九、技巧精解:两个最硬核的细节

本章挑两个最硬核、最体现 SQLite 工程功底的技巧,单独拆透。

### 技巧一:Lemon 的"遇冲突直接报错"——SQL 文法零歧义的护城河

前面说过,Lemon 遇到 LALR 冲突直接报错、拒绝生成 parser,而 yacc/bison 默认只警告。这听起来只是"严格一点",但它的实际影响是巨大的——它是 SQLite 几十年几乎零语法 bug 的护城河。

**反面对比:yacc 的"默认移进"陷阱**。假设有个 SQL 文法片段长这样(假设性例子,非 SQLite 实际文法):

```
// 假想的、有歧义的文法(用 yacc 写法)
stmt ::= IF expr THEN stmt.        // if-then
stmt ::= IF expr THEN stmt ELSE stmt.  // if-then-else
```

这是经典的"悬空 else"歧义(`if a then if b then s1 else s2` 的 `else` 归谁)。yacc 遇到这个冲突,默认选"移进"(else 归内层 if),只打个 `1 shift/reduce conflict` 警告就过去了。结果就是:**文法作者可能根本没注意到有歧义,生成的 parser 在某些边界输入上会按 yacc 的默认规则解析,和文法作者的本意不符**。这种 bug 可以潜伏几年,直到某个用户写出触发边界的 SQL 才暴露。

**Lemon 的做法**。Lemon 遇到同样的冲突,**直接编译失败**,报:

```
parser grammar error: shift-reduce conflict ...
```

文法作者**必须**改文法(如把 `IF expr THEN stmt ELSE stmt` 拆得更明确),消除歧义,才能让 Lemon 生成 parser。这逼着 SQLite 的文法从源头就是无歧义的——**任何语法上的歧义,在构建期就被发现并修掉**,不可能带到运行时。

**为什么这很 sound**。SQL 是给用户写的一种语言,用户会写出各种千奇百怪的边界输入。如果 parser 文法本身有歧义,意味着存在某串 token 序列,parser 在两种解析之间摇摆——这对一个数据库是致命的(可能把 `DROP TABLE foo` 解析成别的东西)。Lemon 的"零冲突"策略,从源头消除了这个风险。Hipp 在 Lemon 文档里明确说:yacc 的默认移进是个历史包袱,它让一代 parser 带着潜伏的语法 bug 跑了几十年,Lemon 不重蹈覆辙。

**SQLite 文法实际的冲突数**。你可以自己验证:下载 SQLite 源码,跑 `make parse.c`,Lemon 在生成 `parse.c` 时如果遇到任何冲突会直接报错退出。SQLite 的 `parse.y` 经过几十年打磨,是**零冲突**的(这是 Lemon 能成功生成 `parse.c` 的前提)。这不是巧合,是 Lemon 的严格性 + Hipp 的精心设计共同保证的。

> **钉死这件事**:Lemon 的"遇冲突直接报错"不是洁癖,是**用工具的严格性,换语法的正确性**。它让 SQLite 的 SQL 文法从构建期就是无歧义的,把语法 bug 扼杀在摇篮里。这是 SQLite 选 Lemon 而非 yacc 的最根本理由——**正确性优先,方便性让位**。

### 技巧二:`Expr` 的三档截断——AST 节点的极致省内存

前面讲 `Expr` 结构时提过 `EP_Reduced` / `EP_TokenOnly`,这里拆透为什么这么设计、怎么实现。

**问题:AST 节点数量巨大**。一个中等规模的 SQLite schema,可能有:

- 每张表若干个 CHECK 约束表达式(存 schema)。
- 每张表若干个生成列(`AS expr`)。
- 每个索引上的表达式(`CREATE INDEX ... ON t(expr)`)。
- 每个视图的 `SELECT` 定义(整棵 `Select` 树)。
- 每个触发器的 `WHEN` 子句和动作语句。

这些 `Expr` / `Select` 在 schema 加载时就建好,常驻内存。一个有几百个表、每表十几个表达式的 schema,Expr 节点轻松上万。如果每个 `Expr` 都占满 `sizeof(Expr)`(约 80~96 字节,取决于编译开关),光这些静态 Expr 就要几百 KB 到 1 MB——在嵌入式(手机 App、IoT)里是不可接受的。

**SQLite 的招:同一类型,三档大小**。`Expr` 的内存布局由 `flags` 里的两个 bit 控制: [`sqliteInt.h#L3195-L3197`](../sqlite/src/sqliteInt.h#L3195-L3197)

```c
#define EXPR_FULLSIZE           sizeof(Expr)           /* 全尺寸 */
#define EXPR_REDUCEDSIZE        offsetof(Expr,iTable)  /* Reduced: 砍到 iTable 之前 */
#define EXPR_TOKENONLYSIZE      offsetof(Expr,pLeft)   /* TokenOnly: 砍到 pLeft 之前 */
```

也就是说:

```
   全尺寸 Expr 的内存布局:
   ┌──── EP_TokenOnly 截断线 (EXPR_TOKENONLYSIZE) ────┐
   │ op | affExpr | op2 | [vvaFlags] | flags | u      │  ← TokenOnly 只留这些(~16B)
   ├──── EP_Reduced 截断线 (EXPR_REDUCEDSIZE) ────────┤
   │ pLeft | pRight | x                                │  ← Reduced 加留这些(~40B)
   ├──────────────────────────────────────────────────┤
   │ [nHeight] | iTable | iColumn | iAgg | w | ...    │  ← 全尺寸才留(共~80B)
   │ pAggInfo | y                                      │
   └──────────────────────────────────────────────────┘
```

**何时用哪档**:

- **全尺寸**:parser 刚建出来的表达式、code generator 正在分析的表达式——需要所有字段(游标号、列号、聚合信息都在分析中填)。`sqlite3Expr()` / `sqlite3PExpr()` / `tokenExpr()` 建出来默认全尺寸。
- **`EP_Reduced`**:存进 schema 的表达式(CHECK 约束、生成列、索引表达式、视图定义)。这些表达式已经分析过、绑定过,但它们的"分析期字段"(iTable、iColumn、pAggInfo 等)在 schema 缓存里用不到(重新分析时会重算),所以砍掉。`sqlite3ExprDup(pParse->db, p, EXPRDUP_REDUCE)` 复制时用 `EXPRDUP_REDUCE` 标志,产出的副本就是 Reduced。
- **`EP_TokenOnly`**:更深层的省内存——当一个表达式作为列表项、且不需要左右子树时(如某些常量折叠后的叶子),连 `pLeft`/`pRight`/`x` 都不要,只留 op 和文本。

**实现的关键:C 的 `offsetof` + 手动控制分配大小**。SQLite 不是用三个不同的结构体,而是**一个 `Expr` 类型 + 两个截断宏**。分配时按需 `malloc`:

```c
// 全尺寸(简化示意)
Expr *p = sqlite3DbMallocRawNN(db, EXPR_FULLSIZE);

// Reduced(简化示意)
Expr *p = sqlite3DbMallocRawNN(db, EXPR_REDUCEDSIZE);
p->flags |= EP_Reduced;

// TokenOnly(简化示意)
Expr *p = sqlite3DbMallocRawNN(db, EXPR_TOKENONLYSIZE);
p->flags |= EP_TokenOnly;
```

访问字段前,用宏检查 flag,避免访问被截掉的字段(`ExprUse_UToken`、`ExprUseXList` 等宏,见 [`sqliteInt.h#L3161-L3169`](../sqlite/src/sqliteInt.h#L3161-L3169)):

```c
#define ExprUseUToken(E)    (((E)->flags&EP_IntValue)==0)   // u.zToken 有效(非 IntValue)
#define ExprUseUValue(E)    (((E)->flags&EP_IntValue)!=0)   // u.iValue 有效
#define ExprUseWOfst(E)     (((E)->flags&(EP_InnerON|EP_OuterON))==0)
#define ExprUseWJoin(E)     (((E)->flags&(EP_InnerON|EP_OuterON))!=0)
#define ExprUseXList(E)     (((E)->flags&EP_xIsSelect)==0)  // x.pList 有效
#define ExprUseXSelect(E)   (((E)->flags&EP_xIsSelect)!=0)  // x.pSelect 有效
#define ExprUseYTab(E)      (((E)->flags&(EP_WinFunc|EP_Subrtn))==0)
...
```

这些宏既保证类型安全(只在对应 flag 置位时访问对应 union 成员),又和截断标志联动——`EP_TokenOnly` 的 Expr 根本没分配 `pLeft`/`pRight`,代码里也绝不会去访问(访问会 segfault,但宏 + flag 保证不会走到那)。

**反面对比:如果不用截断**。假设 SQLite 的每个 Expr 都全尺寸分配(80 字节),一个 500 表、每表 20 个表达式、每表达式平均 5 个节点的 schema,Expr 节点数 = 500 × 20 × 5 = 50000,全尺寸占用 = 50000 × 80 = 4 MB。用 Reduced(40 字节)后 = 50000 × 40 = 2 MB,**省了一半**。在很多嵌入式设备(给 SQLite 的内存预算就几 MB)上,这一半就是"能不能跑起来"的区别。

**为什么 sound**。截断掉的字段(iTable、iColumn、pAggInfo 等)是**分析期 / 执行期的临时数据**,不是 schema 需要持久化的语义。schema 里的 CHECK 约束 `x > 0`,它的语义是"op=TK_GT, pLeft=列 x, pRight=0"——这些都在 TokenOnly/Reduced 保留的字段里。iTable(列 x 的游标号)是执行时才需要、且每次执行可能不同的临时绑定,不该也不能存在 schema 里。所以砍掉它们**既省内存,又语义正确**——Reduced Expr 保留的就是"语法树的本质结构",砍的是"分析过程的副产物"。

> **钉死这件事**:`Expr` 的三档截断(全尺寸/Reduced/TokenOnly)是 SQLite 在 AST 节点上最精妙的省内存设计。它用 `offsetof` 算截断点、用 flag 标记档位、用访问宏保证安全,把 schema 缓存里的 Expr 内存砍掉一半以上。这是嵌入式数据库"每个字节都要抠"的典型,也是 C 语言柔性布局技巧的教科书级示范。

---

## 十、章末小结

### 回扣主线

本章拆的是 SQLite 八层流水线的第 2、3 层——**Tokenizer 和 Parser**。它服务的是二分法的**编译与执行**这一面:把 SQL 字符串变成 AST,是"编译"的第一步(下一步 P1-04 是 Code Generator 把 AST 变成 opcode)。

本章的核心结论:

1. **Tokenizer = 字符分类表 + 字符类跳表 switch**。`tokenize.c` 的 `sqlite3GetToken` 用 `aiClass[]` 把任意字节 O(1) 映射到 31 个字符类,`switch(类)` 跳表分发,每切一个 token 就是"一次下标 + 一两次前瞻 + 一个吃同类循环"。关键字识别用 `mkkeywordhash.c` 离线生成的最小化哈希表(`keywordhash.h`),运行时 O(1) 查。
2. **Parser = Lemon 生成的 LALR(1) 表驱动**。不是手写递归下降!文法源 `src/parse.y`(2163 行),产物 `parse.c`。SQLite 自研 Lemon(而非用 bison/yacc)的三大理由:可重入(线程安全)、遇冲突直接报错(语法零歧义)、每非终结符独立类型 + 自动 destructor(类型安全 + 防泄漏)。
3. **AST 的四个核心节点**:`Token`(零拷贝切片)、`Expr`(表达式树,三档截断省内存)、`Select`(查询树,复合查询用链表)、`SrcList`(FROM 子句,柔性数组)。每个节点都有嵌入式特色的省内存招。

### 五个为什么

1. **为什么 Tokenizer 用字符分类表,不用 `if-else` 链?**——`aiClass[]` 把字符归类做成 O(1) 查表,`switch(小整数类)` 编成跳表;`if-else` 链在十几类字符下要线性跳,慢且维护差。嵌入式每条 prepare 都要 tokenize,必须榨到极致。
2. **为什么 SQLite 用 Lemon,不用 bison/yacc?**——Lemon 可重入(多线程 prepare 安全)、遇冲突直接报错(语法从源头无歧义,几十年零语法 bug 的护城河)、每非终结符独立类型 + destructor(类型安全 + 防内存泄漏)、支持 `%ifndef` 可裁剪。yacc 这四点都不如 Lemon。
3. **为什么 `a+b*c` 的 AST 树根是 `+` 不是 `*`?**——文法里 `*`/`/` 的优先级(通过 `%left STAR SLASH` 声明)高于 `+`/`-`,LALR 在看到 `*` 的 lookahead 时选择移进(先归约 `b*c`),最终树根是 `+`。优先级编码在文法里,不在手写 if 判断里。
4. **为什么 `Expr` 要三档截断(全尺寸/Reduced/TokenOnly)?**——schema 里常驻的 `Expr`(CHECK 约束、生成列、索引表达式、视图定义)数量巨大,全尺寸(80B)会让 schema 缓存占几 MB;Reduced(40B)/TokenOnly(16B)砍掉分析期临时字段(iTable、iColumn 等),省一半以上内存,且语义 sound(砍的是副产物不是本质)。
5. **为什么 `Token` 不复制字符串、用零拷贝?**——一条 SQL 切几百个 token,每个 `malloc` 一份文本是灾难;`Token.z` 直接指向输入 SQL 字符串中间,`Token.n` 记长度,零分配。只有需要独立可改字符串时(如去引号后的标识符),才在 `tokenExpr` 里 `sizeof(Expr)+t.n+1` 一次性分配 Expr+字符串。

### 想继续深入往哪钻

- **想看 SQLite 官方讲架构**:读 `https://www.sqlite.org/arch.html` 的 "Tokenizer" 和 "Parser" 两节,以及 `https://www.sqlite.org/lemon.html`(Lemon 的官方文档,Hipp 自己写的,讲 Lemon 的设计理念)。
- **想理解 LALR(1) 表驱动的第一性原理**:复习《Lua》那本的编译前端章(那里对照 yacc/bison 讲了状态栈、移进-归约、预测分析表),或读经典《编译原理》(龙书)LALR 那章。
- **想动手感受**:在 SQLite 源码树跑 `make parse.c`,看 Lemon 怎么从 `parse.y` 生成 `parse.c`;或在 `sqlite3` CLI 里 `EXPLAIN SELECT a+b*c FROM t WHERE x>0`,看这棵 AST 编译出来的 opcode(下一章会讲怎么从树到 opcode)。
- **想读 SQLite 关键字哈希的生成器**:`tool/mkkeywordhash.c`,编译运行它,看它怎么搜索最小完美哈希函数。

### 引出下一章

我们搞清楚了 SQL 怎么从字符串变成 AST。但 AST 只是一棵描述"用户想要什么"的树,SQLite 真正执行的是 **VDBE opcode**。下一章 P1-04,我们拆**Code Generator**:它怎么遍历这棵 AST、怎么把 `Select` 树变成 `OpenRead`/`Column`/`ResultRow` 这样的 opcode 流?为什么 SQLite 选"编译成字节码"而不是直接解释 AST?这是 SQLite 灵魂(VDBE)的入口,也是和《Lua》那本最强的承接点。

> **下一章**:[P1-04 · Code Generator:AST → VDBE 字节码](P1-04-CodeGenerator-AST到VDBE字节码.md)
