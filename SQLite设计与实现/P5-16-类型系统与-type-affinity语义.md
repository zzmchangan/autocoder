# 第 5 篇 · 第 16 章 · 类型系统与 type affinity 语义

> **核心问题**:上一章 P5-15 拆了 VFS,把"SQLite 怎么读写一个文件、怎么跨平台"这一层讲透了。可这一章我们把视角拉回到 SQL 语义本身——**SQLite 的类型系统为什么这么"宽松"?** 你建表写 `CREATE TABLE t(a INTEGER, b TEXT)`,然后 `INSERT INTO t VALUES('xyz', 123)`——它不报错,而且 `typeof(a)` 还返回 `'text'`,而不是你声明的 `'integer'`。一个 `INTEGER` 列里居然能存文本!这在 MySQL/PG 里几乎不可想象。SQLite 凭什么敢这么做?它的类型系统在源码里到底是怎么定义的?值与值之间怎么比较、怎么排序?`COLLATE NOCASE` 那种"大小写不敏感"又是怎么实现的?以及——3.37 版以后新加的 `STRICT` 关键字,是不是 SQLite 终于"认输"也开始支持强类型了?
>
> 这一章聚焦**类型系统的语义侧**:affinity 怎么决定一个值"能不能存进这一列"、值与值怎么比、怎么排、collation 怎么改写比较规则、STRICT 模式怎么在动态类型之上补一层"可选强类型"。**注意分工**:字节级的存储格式(Record header + body、varint、serial type 怎么把一个值编成字节)已经在 P3-09 《记录格式与动态类型》那章拆透了,本章不再重复字节布局,只讲**类型语义**——affinity 规则、比较、排序、collation、STRICT。两章合起来才是 SQLite 类型系统的完整画像:P3-09 是"值在磁盘上长什么样",本章是"值在 SQL 语义上怎么被对待"。

> **读完本章你会明白**:
> 1. SQLite 的类型系统是**两套体系**:值有 5 种**存储类型**(NULL/INTEGER/REAL/TEXT/BLOB,由值本身决定),列只有 **affinity**(亲和性,7 种:`NONE/BLOB/TEXT/NUMERIC/INTEGER/REAL/FLEXNUM`,3.54 新增 FLEXNUM)——列类型名只是"柔性偏好",不强制。
> 2. **affinity 转换矩阵**:每种 affinity 收到不同类型的值,会"温柔地试转一次",转不成就保留原值(不报错)。本章给完整的转换矩阵 + 源码出处,并对照 MySQL 强类型的隐式转换。
> 3. **比较与排序的类型顺序**:`NULL < INTEGER/REAL < TEXT < BLOB`;两个数按数值比;数与文本按类型决定大小(不是数值)。SQLite 怎么在 `OP_Eq`/`OP_Lt`/`OP_Compare` 这几个 opcode 里实现这套规则。
> 4. **collation(比较序列)**:三个内置 `BINARY`(memcmp 字节比)/`NOCASE`(ASCII 大小写不敏感)/`RTRIM`(忽略尾部空格),以及为什么 SQLite 的 `NOCASE` 只懂 26 个英文字母(不懂 Unicode)。
> 5. **STRICT 表(3.37+,2021-11-27)**:新加的 `STRICT` 关键字给动态类型补一层"可选强类型"——列声明什么类型就必须存什么类型,违者 `SQLITE_CONSTRAINT_DATATYPE` 报错。这是 SQLite 在"动态类型"基础上加的、向后兼容的强类型开关,不是放弃动态类型。
> 6. **CAST(X AS type) vs typeof(X) vs affinity**:CAST 是"强制转换"(走 `OP_Cast`,失败给默认值),typeof 是"返回值真实类型",affinity 是"温柔试转"——三条独立路径,别混。

> **如果一读觉得太难**:先只记住三件事——① SQLite 列没有强类型,只有 **affinity**(7 种柔性偏好,值存什么由值本身定);② 比较排序的类型顺序是 `NULL < 数 < TEXT < BLOB`;③ 3.37+ 加了 `STRICT` 关键字,想要强类型就开它。其余细节(转换矩阵、collation、源码)都是这三件事的展开。

---

## 〇、一句话点破

> **SQLite 的类型系统把"类型"这件事拆成了两半:存什么类型由值自己决定(动态),列只负责温柔地推一把(affinity)——这套设计服务的是"嵌入式、数据来源杂、schema 要灵活"的场景。它在比较/排序/collation 上有一套独立于存储的语义规则,并在 3.37 用 STRICT 关键字给那些"我就要强类型"的场合补了一条可选的退路。**

这是结论,不是理由。本章倒过来拆:先讲清两套类型体系(存储类型 vs affinity)为什么这么分,再拆 affinity 转换矩阵怎么落地,接着讲比较/排序/collation 这套语义规则在源码里怎么实现,然后拆 3.37 的 STRICT 表是怎么"在动态类型上叠强类型"的,最后讲 CAST/typeof 这几个工具函数和"为什么动态类型对"。

---

## 一、两套类型体系:存储类型由值定,列只有亲和性

理解 SQLite 类型系统的第一步,是认清它有**两套"类型"概念**,它们经常被混为一谈,但根本不是一回事。

### 第一套:值的存储类型(5 种,由值本身决定)

一个值在 SQLite 里,客观上有 5 种**存储类型**(storage class):

| 存储类型 | 含义 | 例子 | 对应的 typeof() 返回 |
|---|---|---|---|
| **NULL** | 空值 | `NULL` | `'null'` |
| **INTEGER** | 有符号整数(1/2/3/4/6/8 字节,按值挑最省的) | `42`, `-1` | `'integer'` |
| **REAL** | IEEE 754 双精度浮点(8 字节) | `3.14` | `'real'` |
| **TEXT** | 字符串(按数据库编码 UTF-8/UTF-16le/UTF-16be) | `'alice'` | `'text'` |
| **BLOB** | 原始字节串(神圣不可侵犯) | `x'0102'` | `'blob'` |

**关键**:这 5 种类型是**值级别**的——一个值存什么类型,**完全由这个值本身决定**,跟你往哪一列插没关系。`typeof(X)` 函数返回的就是值本身的存储类型(不是列声明的类型)。源码里 `typeofFunc`([func.c:79-98 typeofFunc](../sqlite/src/func.c#L79-L98)) 就是查 `sqlite3_value_type()` 的结果(返回 1-5 的整数映射到 `"integer"/"real"/"text"/"blob"/"null"`),它根本不碰列定义:

```c
/* func.c:79  typeof 函数 —— 返回值本身的存储类型,不看列定义 */
static void typeofFunc(
  sqlite3_context *context,
  int NotUsed,
  sqlite3_value **argv
){
  static const char *azType[] = { "integer", "real", "text", "blob", "null" };
  int i = sqlite3_value_type(argv[0]) - 1;   /* SQLITE_INTEGER=1 ... SQLITE_NULL=5 */
  ...
  sqlite3_result_text(context, azType[i], -1, SQLITE_STATIC);
}
```

注意这个函数体里**没有任何"列"的概念**——它只看传入的值(`argv[0]`)是什么类型。这就是"值定类型"在源码里的铁证:`typeof()` 永远返回值真实的存储类型,哪怕你这一列声明的是 `INTEGER`、值是 `'hello'`,`typeof()` 也老老实实告诉你这是 `'text'`。

> **钉死这件事**:`typeof(X)` 是 SQLite 类型系统最诚实的探针——它告诉你"这个值客观上是什么类型",与列声明、与 affinity 都无关。P3-09 那章讲过,这个存储类型最终序列化成一个 serial type(0=NULL, 1-6/8/9=INTEGER, 7=REAL, 奇=TEXT, 偶=BLOB)写进 Record 的 body 里。**值的存储类型是物理事实,affinity 是语义偏好,两者正交**。

### 第二套:列的 affinity(7 种,柔性偏好)

而你在 `CREATE TABLE` 里写的列类型名(`INTEGER`/`VARCHAR(100)`/`DOUBLE`...),**不直接是类型**,而是被推导成一个 **affinity(亲和性)**。affinity 是列的一个**属性**,它表示"这一列偏好什么类型,存值时会试一下转过去"。源码里 affinity 是几个 ASCII 字符常量([sqliteInt.h:2338-2345](../sqlite/src/sqliteInt.h#L2338-L2345)):

```c
/* sqliteInt.h:2338-2345  七种亲和性(3.54 新增 FLEXNUM) */
#define SQLITE_AFF_NONE     0x40  /* '@'  不转换 */
#define SQLITE_AFF_BLOB     0x41  /* 'A'  BLOB/NONE 亲和性(几乎不转) */
#define SQLITE_AFF_TEXT     0x42  /* 'B'  TEXT 亲和性 */
#define SQLITE_AFF_NUMERIC  0x43  /* 'C'  NUMERIC 亲和性 */
#define SQLITE_AFF_INTEGER  0x44  /* 'D'  INTEGER 亲和性 */
#define SQLITE_AFF_REAL     0x45  /* 'E'  REAL 亲和性 */
#define SQLITE_AFF_FLEXNUM  0x46  /* 'F'  FLEXNUM 亲和性(3.54 新增) */
#define SQLITE_AFF_DEFER    0x58  /* 'X'  延迟计算(内部用) */
```

> **易翻车坑**:亲和性的常量值是**连续的 ASCII 字符** `@ A B C D E F`。这不是装饰——SQLite 把"一行的 affinity 串"压成一个 C 字符串(每列一个字符),塞进 `OP_MakeRecord`/`OP_Affinity` 的 P4 参数里。这样既省空间(P4 是变长字符串),又能直接 `strcmp` 比较。还有一个妙用:判断"是不是数值类亲和性"只要一句 `>=SQLITE_AFF_NUMERIC`([sqliteInt.h:2347](../sqlite/src/sqliteInt.h#L2347)),因为 `NUMERIC(0x43) ≤ INTEGER(0x44) ≤ REAL(0x45) ≤ FLEXNUM(0x46)`,数值类的全排在一起,一个 `>=` 一刀切。这种"用值的连续性简化判断"贯穿整个 affinity 系统。

affinity **7 种**(注意是 7 种,不是老资料说的 5 种——`NONE` 和 `BLOB` 几乎等价但都存在,3.54 又加了 `FLEXNUM`):

| affinity | 值 | 含义 | 列类型名怎么推导出这个 |
|---|---|---|---|
| **NONE** | `'@'` | 完全不转(几乎只出现在"无列背景"的表达式里) | 内部用,普通列推不出 |
| **BLOB** | `'A'` | 不转(BLOB 和 NONE 行为等价) | 类型名不含 INT/CHAR/TEXT/CLOB/REAL/FLOA/DOUB,或不写类型 |
| **TEXT** | `'B'` | 偏好文本,收到数会转成文本 | 类型名含 `CHAR`/`CLOB`/`TEXT`(如 `VARCHAR`) |
| **NUMERIC** | `'C'` | 偏好数值,文本是数就转 | 类型名不含上述任何关键字(如 `NUMERIC`/`DATE`/`DECIMAL`) |
| **INTEGER** | `'D'` | 偏好整数 | 类型名含 `INT`(如 `INTEGER`/`BIGINT`) |
| **REAL** | `'E'` | 偏好实数 | 类型名含 `REAL`/`FLOA`/`DOUB` |
| **FLEXNUM** | `'F'` | 灵活数值(3.54 新),数优先,转不成不强求 | 3.54 内部用 |

### 两套体系的根本区别:谁决定"值最终存什么"

这就是 SQLite 和 MySQL/PG 在类型系统上最根本的分歧:

| 维度 | **SQLite(动态)** | **MySQL/PG(强类型)** |
|---|---|---|
| 谁决定值最终存什么 | **值本身**(序列化时挑 serial type) | **列定义**(schema 定死) |
| 列声明的类型 | 推导成 affinity,**柔性偏好** | 强类型,**强制约束** |
| 插入不匹配类型 | 试转一次,转不成**保留原值不报错** | 隐式转换或**报错/截断** |
| 同一列能否存不同类型 | **能**(每行值独立) | **不能**(列类型定死) |

举个反直觉的例子,看 SQLite 的动态类型有多"放飞":

```sql
CREATE TABLE t(x);          -- 无类型列,推 BLOB affinity
INSERT INTO t VALUES(42);         -- 存整数
INSERT INTO t VALUES('hello');    -- 存文本
INSERT INTO t VALUES(3.14);       -- 存浮点
INSERT INTO t VALUES(x'0102');    -- 存 BLOB
INSERT INTO t VALUES(NULL);       -- 存 NULL
SELECT typeof(x), x FROM t;
--  integer   42
--  text      hello
--  real      3.14
--  blob      (二进制)
--  null      NULL
```

**同一个列 `x`,五行存了五种不同的存储类型**——这在 MySQL/PG 里直接报错(列类型不匹配)。SQLite 不仅不报错,而且每行的 `typeof(x)` 还老老实实告诉你这一行存的到底是什么。这就是"值定类型"的极致体现。

> **钉死这件事**:理解 SQLite 类型系统的钥匙,是认清楚"存储类型(值级,5 种)"和"affinity(列级,7 种)"是两套正交的概念。值存什么类型,**永远由值本身决定**;列声明的类型名只是被推导成一个 affinity,在值"存入"或"参与比较"时温柔地推一把。**MySQL 是"列管值"(列类型强制值的类型),SQLite 是"值管自己,列只建议"**——这是两种设计哲学的根本对立。

---

## 二、为什么 SQLite 敢不要强类型:动态类型的动机

讲清了两套体系,接下来回答最根本的问题:**SQLite 凭什么敢"不要强类型"?** 这是一个设计决策,动机值得讲透——因为它直接决定了 SQLite 适合什么场景、不适合什么场景。

### 强类型服务的是"MySQL/PG 那种场景"

MySQL/PG 要强类型,是因为它们的使用场景有几个硬约束:

1. **C/S + 多应用共享**:一个数据库服务器后面挂着好几个应用,应用 A 写的表应用 B 也可能读。强类型是"应用之间的契约"——A 写进 `age` 列的必须是整数,B 才能放心 `SUM(age)`。没有强类型,A 不小心塞了个 `'twenty'` 进去,B 的聚合查询就崩了。
2. **查询优化器要类型**:MySQL 的优化器算索引基数、估代价,要靠"列是 `INT` 还是 `VARCHAR`"这种强类型信息。类型不确定,统计和代价模型都没法建。
3. **存储紧凑 + 解析快**:强类型下,`INT` 永远 4 字节、`VARCHAR(255)` 长度记在行头,解析行不用扫类型,直接按 schema 偏移取列——快。

> **承接《MySQL·InnoDB》**:MySQL 的强类型 + 复杂行格式(REDUNDANT/COMPACT/DYNAMIC/COMPRESSED),在《MySQL·InnoDB》那本讲 InnoDB 行格式(P3 存储)那里拆透了。本章只一句话点出对照——**MySQL 强类型服务 C/S 多应用场景,SQLite 动态类型服务嵌入式单应用场景,两种都对,各对各的**。不重复讲 MySQL 行格式。

### 嵌入式场景反过来:强类型是负担

SQLite 是嵌入式——数据库链接进你的 App,数据来源就是**你自己的代码**。这个定位下,强类型的三个价值都打折扣:

1. **没有"多应用共享"**:数据来源是你自己的程序,你自己写错了类型是你的 bug,不是数据库的事。强类型契约在这里价值低。
2. **schemaless 友好**:很多嵌入式场景(配置存储、缓存、日志、CSV 导入、JSON 解析)数据结构会变,提前定死 schema 反而是负担。SQLite 允许 `CREATE TABLE t(a, b, c)`(无类型列),甚至允许一列里混存不同类型——这对"我不想提前设计 schema"的场景极友好。
3. **动态类型其实更省空间**:P3-09 讲过,SQLite 按值的实际大小挑最小的 serial type——存个状态码 `1` 用 0 字节(serial type 9),存个时间戳用 4 字节,存大数才用 8 字节。而 MySQL 的 `BIGINT` 永远 8 字节。在值分布偏小的嵌入式场景(布尔标志、小整数状态码占绝大多数),动态类型平均省一半空间。

> **不这样会怎样**:如果 SQLite 学 MySQL 用强类型,会发生什么?① 你往 `INTEGER` 列插字符串 `'456'`,直接报错——可嵌入式场景很多时候数据来源杂(CSV 一列里有数有字符串、JSON 解析出来的字段类型运行时才确定),报错体验差;② 每列定死类型,失去"按值大小挑 serial type"的省空间优势(整列要么全 8 字节要么全 1 字节);③ schema 演进困难(改个列类型要 `ALTER TABLE` 全表重写)。SQLite 选"动态类型 + 亲和性",是用"放弃强约束"换"灵活 + 紧凑"——这对嵌入式是对的,对 C/S 多应用共享是错的。

> **钉死这件事**:**强类型 vs 动态类型,不是谁更先进,而是不同场景的取舍**。MySQL/PG 的强类型服务于"C/S + 多应用 + 查询优化",SQLite 的动态类型服务于"嵌入式 + 灵活 + 紧凑"。理解了这个动机,你就理解了 SQLite 一切"类型宽松"行为的根源——它不是"功能弱",是"换了个赛道"。

### 但完全不要约束也不行:affinity 的折中

可是,纯动态类型有个现实问题:如果**完全不**转换,那 `SELECT * FROM t WHERE score = 99`,在 `score` 列存的是文本 `'99'` 时,会和整数 `99` 比较吗?如果不比较,用户会困惑("明明存了 99,为什么查不出来");如果硬比,文本 `'99'` 和整数 `99` 怎么比?

这就是 affinity 存在的意义——**它在值"存入表"和"参与比较"时,按列定义的偏好,温柔地推一把,尽量把值转成"期望"的类型,让比较和查询结果符合直觉**。affinity 不是强类型(转不成不报错),也不是"完全不转"(那查询结果会很反直觉),而是中间的折中:一个**柔性偏好 + 温柔试转**的机制。

接下来三节,我们拆 affinity 的三件事:① 列类型名怎么推成 affinity(本节);② affinity 转换矩阵怎么落地(下节);③ 比较时 affinity 怎么对齐(再下节)。

### 列类型名怎么推成 affinity:sqlite3AffinityType

你写 `CREATE TABLE t(a VARCHAR(100), b BIGINT, c DOUBLE, d BLOB)`,SQLite 怎么把这些类型名推导成 affinity?核心是 `sqlite3AffinityType`([build.c:1670-1736 sqlite3AffinityType](../sqlite/src/build.c#L1670-L1736))。它的策略是**扫描类型名字符串,按出现的关键字判断**,优先级很讲究:

```c
/* build.c:1670-1736  把列类型名推导成 affinity(简化,保留关键规则) */
char sqlite3AffinityType(const char *zIn, Column *pCol){
  u32 h = 0;                         /* 滚动 hash */
  char aff = SQLITE_AFF_NUMERIC;     /* 默认 NUMERIC */
  const char *zChar = 0;

  while( zIn[0] ){
    u8 x = *(u8*)zIn;
    h = (h<<8) + sqlite3UpperToLower[x];   /* 滚动累积 4 字节 hash */
    zIn++;
    if( h==(('c'<<24)+('h'<<16)+('a'<<8)+'r') ){           /* CHAR → TEXT */
      aff = SQLITE_AFF_TEXT;  zChar = zIn;
    }else if( h==(('c'<<24)+('l'<<16)+('o'<<8)+'b') ){     /* CLOB → TEXT */
      aff = SQLITE_AFF_TEXT;
    }else if( h==(('t'<<24)+('e'<<16)+('x'<<8)+'t') ){     /* TEXT → TEXT */
      aff = SQLITE_AFF_TEXT;
    }else if( h==(('b'<<24)+('l'<<16)+('o'<<8)+'b')        /* BLOB → BLOB */
        && (aff==SQLITE_AFF_NUMERIC || aff==SQLITE_AFF_REAL) ){
      aff = SQLITE_AFF_BLOB;
      ...
    }else if( h==(('r'<<24)+('e'<<16)+('a'<<8)+'l')        /* REAL → REAL */
        && aff==SQLITE_AFF_NUMERIC ){
      aff = SQLITE_AFF_REAL;
    }else if( h==(('f'<<24)+('l'<<16)+('o'<<8)+'a')        /* FLOA → REAL */
        && aff==SQLITE_AFF_NUMERIC ){
      aff = SQLITE_AFF_REAL;
    }else if( h==(('d'<<24)+('o'<<16)+('u'<<8)+'b')        /* DOUB → REAL */
        && aff==SQLITE_AFF_NUMERIC ){
      aff = SQLITE_AFF_REAL;
    }else if( (h&0x00FFFFFF)==(('i'<<16)+('n'<<8)+'t') ){  /* INT → INTEGER, 立即 break */
      aff = SQLITE_AFF_INTEGER;
      break;
    }
  }
  return aff;
}
```

把这个函数翻译成**人话规则**(注意优先级和顺序):

| 类型名里包含 | 推导出的 affinity | 关键点 |
|---|---|---|
| `INT`(如 `INTEGER`/`BIGINT`/`SMALLINT`/`TINYINT`) | **INTEGER** | 一旦命中 `INT`,**立即 break**,优先级最高 |
| `CHAR`/`CLOB`/`TEXT`(如 `VARCHAR`/`CHARACTER`) | **TEXT** | 命中会记住 `zChar`(为后面算长度估计) |
| `BLOB`(或没声明类型) | **BLOB** | 只在还没被 REAL 抢走时才生效 |
| `REAL`/`FLOA`/`DOUB`(如 `REAL`/`FLOAT`/`DOUBLE`) | **REAL** | 只在还是默认 NUMERIC 时才抢 |
| 其他(如 `DATE`/`NUMERIC`/`DECIMAL`) | **NUMERIC** | 默认值 |

> **易翻车坑**:这些规则的**优先级和顺序很反直觉**。比如 `VARCHAR(100)` 包含 `CHAR` → TEXT affinity;`BIGINT` 包含 `INT` → INTEGER affinity;`DOUBLE PRECISION` 包含 `DOUB` → REAL affinity。但如果你写个奇奇怪怪的类型名比如 `BLOBINT`,扫描时**先**命中 `BLOB`(aff=BLOB),**再**命中 `INT`(aff=INTEGER 并 break)——最终是 INTEGER!这是因为 `INT` 一旦命中就 break,而其他关键字是"后命中覆盖前命中"。所以**类型名推导是"按字符出现顺序匹配,INT 一旦命中立即 break,其他按最后命中的为准"**。这个规则在 SQLite 官方文档 "Datatypes In SQLite" 里有说明,但源码是唯一权威——尤其注意 `INT` 的 break 行为,这是最容易讲错的点。

特别地,**不写类型名**(`CREATE TABLE t(a, b)`)的列,推导出 **BLOB affinity**(等价于 NONE,不转换)。这是 SQLite "schemaless" 的入口——你完全可以建一个"无类型"表,每列存什么都行,这正是上一节那个"一行存五种类型"例子的基础。

---

## 三、affinity 转换矩阵:每种 affinity 收到值后温柔地试转

affinity 推导出来后,**什么时候应用**?在两个时机:① **值存入表**的时候(`OP_MakeRecord` 的第一步);② **值参与比较**的时候(`OP_Eq`/`OP_Lt` 等比较 opcode 前)。两个时机用的都是同一个核心函数 `applyAffinity`([vdbe.c:397-428 applyAffinity](../sqlite/src/vdbe.c#L397-L428))(注:本节聚焦"存入"这个时机;比较时的应用下下节讲):

```c
/* vdbe.c:397-428  应用亲和性(简化,对照源码注释看) */
static void applyAffinity(Mem *pRec, char affinity, u8 enc){
  if( affinity>=SQLITE_AFF_NUMERIC ){
    /* NUMERIC / INTEGER / REAL / FLEXNUM 这一族(因为它们 ASCII 连续,>=NUMERIC 一刀切) */
    assert( affinity==SQLITE_AFF_INTEGER || affinity==SQLITE_AFF_REAL
         || affinity==SQLITE_AFF_NUMERIC || affinity==SQLITE_AFF_FLEXNUM );
    if( (pRec->flags & MEM_Int)==0 ){
      if( (pRec->flags & (MEM_Real|MEM_IntReal))==0 ){
        if( pRec->flags & MEM_Str )
          applyNumericAffinity(pRec,1);   /* TEXT → 尝试解析成数 */
      }else if( affinity<=SQLITE_AFF_REAL ){
        sqlite3VdbeIntegerAffinity(pRec);  /* 已经是 REAL,看能不能无损转回 INT */
      }
    }
  }else if( affinity==SQLITE_AFF_TEXT ){
    /* TEXT:把 INTEGER/REAL 转成 TEXT(字符串化) */
    if( 0==(pRec->flags&MEM_Str) ){
      if( (pRec->flags&(MEM_Real|MEM_Int|MEM_IntReal)) ){
        sqlite3VdbeMemStringify(pRec, enc, 1);   /* 数字 → 字符串 */
      }
    }
    pRec->flags &= ~(MEM_Real|MEM_Int|MEM_IntReal);   /* 清掉数值标志 */
  }
  /* SQLITE_AFF_BLOB / NONE:什么都不做 */
}
```

把这段代码翻译成**完整的 affinity 转换矩阵**——这是本章的核心交付物,务必钉死:

| 列 affinity | 收到 **NULL** | 收到 **INTEGER** | 收到 **REAL** | 收到 **TEXT** | 收到 **BLOB** |
|---|---|---|---|---|---|
| **NONE / BLOB**(`@`/`A`) | NULL(不转) | INTEGER(不转) | REAL(不转) | TEXT(不转) | BLOB(不转) |
| **TEXT**(`B`) | NULL(不转) | **TEXT**(数字 stringify) | **TEXT**(数字 stringify) | TEXT(不转) | **BLOB**(不转!) |
| **NUMERIC**(`C`) | NULL(不转) | INTEGER(不转) | REAL(不转) | **能精确转 INT→INT,有小数点→REAL,都不行→保留 TEXT** | BLOB(不转) |
| **INTEGER**(`D`) | NULL(不转) | INTEGER(不转) | **能无损转 INT→INT,否则保留 REAL** | **能精确转 INT→INT,都不行→保留 TEXT** | BLOB(不转) |
| **REAL**(`E`) | NULL(不转) | **INTEGER**(不转!存成整数省空间,标 MEM_IntReal) | REAL(不转) | **能转 REAL→REAL(优先),能转 INT→INT,都不行→保留 TEXT** | BLOB(不转) |
| **FLEXNUM**(`F`,3.54 新) | NULL(不转) | INTEGER(不转) | REAL(不转) | **尝试转数,转不成保留 TEXT** | BLOB(不转) |

这张矩阵有几个**反直觉但极其重要**的细节,逐个钉死:

### 细节一:TEXT affinity 收到 BLOB 不转

很多人以为"TEXT affinity 就是把什么都转成文本"。**错。** 看矩阵第 2 行第 6 列——TEXT affinity 收到 BLOB,值还是 BLOB。为什么?因为 BLOB 是"原始字节,神圣不可侵犯",affinity 的哲学是"只动数字和文本之间的转换,绝不碰 BLOB 的字节"。源码里 `applyAffinity` 的 TEXT 分支只检查 `MEM_Real|MEM_Int|MEM_IntReal` 这几个数值标志,`MEM_Blob` 不在转换范围内。

> **钉死这件事**:affinity 的转换规则有一条铁律——**BLOB 永远不被任何 affinity 转换**(除了显式 `CAST` 强制)。这是 SQLite 对"二进制数据完整性"的尊重:你存进去 `x'0102'`,无论这列是什么 affinity,读出来字节顺序一个都不变。这条铁律也意味着,如果你想用 SQLite 存"绝不能被改写"的二进制(图片、序列化数据、加密块),放心用 BLOB 列,affinity 不会偷偷动它。

### 细节二:REAL affinity 收到整数,不转成 REAL——为了省空间

这是最容易踩的坑。看矩阵第 5 行第 3 列——REAL affinity 收到一个整数,**不**把它转成 8 字节浮点,而是**保留整数存储**(标记成 `MEM_IntReal`,意思是"语义上是实数,但存储按整数")。源码在 `OP_Affinity`([vdbe.c:3451-3468 OP_Affinity](../sqlite/src/vdbe.c#L3451-L3468))里特意这么处理:

```c
/* vdbe.c:3451-3468  OP_Affinity:REAL affinity 收到整数的特殊处理 */
if( zAffinity[0]==SQLITE_AFF_REAL && (pIn1->flags & MEM_Int)!=0 ){
  /* 当 REAL affinity 收到一个整数,如果它装得进 6 字节(48 位),
  ** 标记成 MEM_IntReal(语义是实数,但存储按整数),为了省空间。
  ** 整数 1 字节、浮点 8 字节,差 8 倍。*/
  if( pIn1->u.i<=140737488355327LL && pIn1->u.i>=-140737488355328LL ){
    pIn1->flags |= MEM_IntReal;
    pIn1->flags &= ~MEM_Int;
  }else{
    pIn1->u.r = (double)pIn1->u.i;   /* 装不下,转成真浮点 */
    pIn1->flags |= MEM_Real;
    pIn1->flags &= ~(MEM_Int|MEM_Str);
  }
}
```

为什么?因为**整数比浮点省空间**——`1` 存成整数可能只 1 字节(serial type 1)甚至 0 字节(serial type 9),存成浮点永远 8 字节(serial type 7)。一个 REAL 列里如果存的全是"恰好是整数"的值(如 `1.0`/`2.0`/`100.0`),按整数存能省 80% 空间。所以 SQLite 宁可"语义上是实数、存储上是整数",也要省这笔空间。读出来 `typeof()` 仍是 `'integer'`(因为存储类型确实是整数),但参与数值比较时按实数比(`1` 和 `1.0` 相等)。

> **不这样会怎样**:如果 REAL affinity 朴素地把所有整数都转成 8 字节浮点,那一个存了 1 亿行"整数状实数"(如价格 `100.0`、计数 `5.0`)的 REAL 列,每行多占 7 字节,1 亿行就是 700 MB 的浪费。SQLite 用 `MEM_IntReal` 这个标志,把"存储紧凑"和"语义正确"两个矛盾目标都兼顾了——这是"为嵌入式抠空间"哲学的又一次体现,和 P3-09 讲的 serial type 8/9(0 字节存 0/1)是同一种执念。

### 细节三:NUMERIC/INTEGER affinity 收到 TEXT,转不成也不报错

看矩阵第 3、4 行第 5 列——NUMERIC 或 INTEGER affinity 收到一个文本,会调用 `applyNumericAffinity`([vdbe.c:354-372 applyNumericAffinity](../sqlite/src/vdbe.c#L354-L372))尝试解析这个文本是不是数:能精确转成整数就转 INTEGER,有小数点或指数就转 REAL,**都转不成(如 `'hello'`)就保留 TEXT,不报错**。

这和 MySQL 的强类型行为**根本不同**。MySQL 的 `INTEGER` 列收到 `'hello'`,要么报错要么转成 `0`(看 sql_mode);SQLite 的 `INTEGER` 列收到 `'hello'`,**保留 `'hello'` 原样存进去**(只是 affinity 试转失败而已)。这就是"温柔试转 vs 强制约束"的本质区别:

```sql
-- SQLite:
CREATE TABLE t(a INTEGER);          -- INTEGER affinity
INSERT INTO t VALUES('hello');      -- 不报错!存成 text
SELECT typeof(a), a FROM t;         -- text | hello

-- MySQL:
CREATE TABLE t(a INTEGER);
INSERT INTO t VALUES('hello');      -- 报错或存成 0(看 sql_mode)
```

> **钉死这件事**:affinity 的本质是——**存入时温柔地试一次转换,转不成就算了,绝不报错**。这和 MySQL 的强类型(转不成报错或截断)是根本区别,也是 SQLite "动态类型"最直接的体现。affinity 只动 INTEGER/REAL/TEXT 三者之间的转换,从不碰 BLOB(BLOB 是"原始字节,神圣不可侵犯"),NULL 也不被转(NULL 就是 NULL)。理解了"温柔 vs 强制"、"何时触发",你就不会被 SQLite 的类型行为绕晕。

---

## 四、值的比较:类型顺序、OP_Eq、affinity 对齐

讲完了 affinity 在"存入"时的应用,接下来是 affinity 的第二个战场——**值与值怎么比较**。这是动态类型系统里最 tricky 的部分:两个值,如果类型不同,怎么比?

### 类型顺序:NULL < INTEGER/REAL < TEXT < BLOB

SQLite 的比较规则有一条明确的**类型顺序**,当两个值类型不同时,按这个顺序决定谁大谁小(而不是按数值):

```
   SQLite 比较的类型顺序(类型不同时):
   ┌──────────────────────────────────────────────────────┐
   │   NULL  <  INTEGER/REAL  <  TEXT  <  BLOB            │
   │   (最小)     (数字一族)        (文本)    (最大)       │
   └──────────────────────────────────────────────────────┘

   具体规则:
   1. NULL 与任何值比:结果总是 NULL(SQL 三值逻辑,NULL 不参与大小)
   2. 两个数(INTEGER/REAL)比:按数值比(1 == 1.0,2 < 3)
   3. 两个 TEXT 比:按 collation 比(默认 BINARY = memcmp 字节比)
   4. 两个 BLOB 比:memcmp 字节比
   5. 数 vs TEXT:数 < TEXT(无论数值和文本内容是什么)
   6. 数 vs BLOB:数 < BLOB
   7. TEXT vs BLOB:TEXT < BLOB
```

这条规则有几个反直觉的点,务必钉死:

- **数字总是小于文本**:`SELECT 999999 < 'a';` 返回 **1**(真)。无论数字多大、文本多小,数 < TEXT。这是因为 SQLite 把"类型"的优先级看得比"数值"高。
- **INTEGER 和 REAL 之间按数值比**:`1 = 1.0` 返回真。SQLite 不区分整数和浮点的"类型"差异,只看数值相等。
- **NULL 比较永远是 NULL**:`NULL < 1` 的结果是 NULL(不是真也不是假),所以 `WHERE x < 1` 会过滤掉 `x IS NULL` 的行(这是 SQL 三值逻辑,所有数据库都这样)。

> **不这样会怎样**:如果不定类型顺序,那"数 `100` 和文本 `'hello'` 怎么比"就没答案——要么硬转(转成什么?),要么报错。SQLite 选了"按类型固定顺序"这条简单粗暴但确定的规则:类型不同时,**永远按 `数 < TEXT < BLOB` 排**,不试图跨类型"智能转换"。这让比较行为可预测(你知道数一定小于文本),也避免了"该转成什么类型比"的歧义。代价是有些结果反直觉(`999999 < 'a'` 是真),但**可预测的反直觉**比**不可预测的智能**安全得多。

### 比较的源码:OP_Eq 和它的兄弟们

比较操作在 VDBE 里是几个 opcode 实现的:`OP_Eq`(等于)/`OP_Ne`(不等于)/`OP_Lt`(小于)/`OP_Le`(小于等于)/`OP_Gt`(大于)/`OP_Ge`(大于等于)。这六个 opcode 是**连续的整数**([vdbe.c:2428-2429](../sqlite/src/vdbe.c#L2428-L2429) 的 assert 钉死了这点:`OP_Eq==OP_Ne+1`, `OP_Gt==OP_Ne+2`, `OP_Le==OP_Ne+3`, `OP_Lt==OP_Ne+4`, `OP_Ge==OP_Ne+5`),这样可以用查表(`sqlite3aGTb[]`/`sqlite3aLTb[]`/`sqlite3aEQb[]`)根据 opcode 索引直接拿到比较结果,不用 switch-case。

这六个 opcode 共用一段比较逻辑([vdbe.c:2308-2451 OP_Eq..OP_Ge](../sqlite/src/vdbe.c#L2308-L2451)),核心三步:

**第一步,整数快速路径**:如果两个值都是 `MEM_Int`(整数),直接比 `u.i` 字段,O(1) 不进任何转换:

```c
/* vdbe.c:2323-2349  整数 vs 整数的极速比较路径 */
if( (flags1 & flags3 & MEM_Int)!=0 ){
  /* Common case of comparison of two integers */
  if( pIn3->u.i > pIn1->u.i ){
    if( sqlite3aGTb[pOp->opcode] ) goto jump_to_p2;   /* opcode 是 GT/GE/NE → 跳 */
    iCompare = +1;
  }else if( pIn3->u.i < pIn1->u.i ){
    if( sqlite3aLTb[pOp->opcode] ) goto jump_to_p2;   /* opcode 是 LT/LE/NE → 跳 */
    iCompare = -1;
  }else{
    if( sqlite3aEQb[pOp->opcode] ) goto jump_to_p2;   /* opcode 是 EQ/GE/LE → 跳 */
    iCompare = 0;
  }
  break;
}
```

**第二步,处理 NULL**:如果有任一操作数是 NULL,结果按 SQL 三值逻辑(默认 NULL,除非 `SQLITE_NULLEQ` 标志)。

**第三步,通用路径——affinity 对齐 + sqlite3MemCompare**:这是核心。两个非整数、非 NULL 的值比较时,先按 P5 里的 affinity 对两边做一次转换,然后调用 `sqlite3MemCompare`([vdbe.h:303](../sqlite/src/vdbe.h#L303) 声明)做实际比较:

```c
/* vdbe.c:2380-2419  比较的通用路径:affinity 对齐 + sqlite3MemCompare */
}else{
  /* Neither operand is NULL and we couldn't do the special high-speed
  ** integer comparison case.  So do a general-case comparison. */
  affinity = pOp->p5 & SQLITE_AFF_MASK;
  if( affinity>=SQLITE_AFF_NUMERIC ){
    /* NUMERIC affinity:把任一边的 TEXT 尝试转成数 */
    if( (flags1 | flags3)&MEM_Str ){
      if( (flags1 & (MEM_Int|MEM_IntReal|MEM_Real|MEM_Str))==MEM_Str ){
        applyNumericAffinity(pIn1,0);
      }
      if( (flags3 & (MEM_Int|MEM_IntReal|MEM_Real|MEM_Str))==MEM_Str ){
        applyNumericAffinity(pIn3,0);
      }
    }
  }else if( affinity==SQLITE_AFF_TEXT && ((flags1 | flags3) & MEM_Str)!=0 ){
    /* TEXT affinity:把任一边的数 stringify 成文本 */
    if( (flags1 & MEM_Str)!=0 ){
      pIn1->flags &= ~(MEM_Int|MEM_Real|MEM_IntReal);
    }else if( (flags1&(MEM_Int|MEM_Real|MEM_IntReal))!=0 ){
      sqlite3VdbeMemStringify(pIn1, encoding, 1);
    }
    /* pIn3 同理 */
  }
  assert( pOp->p4type==P4_COLLSEQ || pOp->p4.pColl==0 );
  res = sqlite3MemCompare(pIn3, pIn1, pOp->p4.pColl);   /* P4 是 collation */
}
```

注意这段代码的两个关键设计:

1. **比较前 affinity 对齐**:比较的两个值,先按 P5 指定的 affinity 做一次"对齐转换"(NUMERIC affinity 把文本转数,TEXT affinity 把数转文本),让两边尽量"类型一致"后再比。这个 affinity 是**编译期由 code generator 根据 SQL 表达式推导出来的**(下节讲)。
2. **比较本身交给 `sqlite3MemCompare`**:它实现上面那张"类型顺序 + 同类型比较规则"的逻辑。注意它接收一个 `CollSeq*` 参数(P4),用来比 TEXT——不同 collation(BINARY/NOCASE/RTRIM)比法不同。下下节讲 collation。

> **钉死这件事**:SQLite 的比较不是"读出值直接比",而是**"affinity 对齐 + sqlite3MemCompare"两步走**。affinity 对齐让两边尽量变成同类型(减少跨类型比较的反直觉),`sqlite3MemCompare` 实现固定的类型顺序规则(`数 < TEXT < BLOB`)。这是动态类型系统在"比较"这件事上的完整闭环。理解了这,你就理解了为什么 `WHERE x = 1` 在 `x` 列存的是 `'1'`(文本)时也能命中——因为比较前 affinity 把 `'1'` 转成了数 `1`。

### 比较的 affinity 从哪来:comparisonAffinity

那比较时用的那个 affinity(P5),是谁、怎么决定的?这是 code generator 在编译 SQL 表达式时算出来的。核心函数 `comparisonAffinity`([expr.c:366-381 comparisonAffinity](../sqlite/src/expr.c#L366-L381))和 `sqlite3CompareAffinity`([expr.c:344-360 sqlite3CompareAffinity](../sqlite/src/expr.c#L344-L360)):

```c
/* expr.c:344-360  两个操作数 affinity 合并规则 */
char sqlite3CompareAffinity(const Expr *pExpr, char aff2){
  char aff1 = sqlite3ExprAffinity(pExpr);
  if( aff1>SQLITE_AFF_NONE && aff2>SQLITE_AFF_NONE ){
    /* 两边都是列(都有 affinity):任一是数值→NUMERIC,否则 NONE(BLOB) */
    if( sqlite3IsNumericAffinity(aff1) || sqlite3IsNumericAffinity(aff2) ){
      return SQLITE_AFF_NUMERIC;
    }else{
      return SQLITE_AFF_BLOB;
    }
  }else{
    /* 一边是列、一边不是(如字面量):用列的 affinity */
    return (aff1<=SQLITE_AFF_NONE ? aff2 : aff1) | SQLITE_AFF_NONE;
  }
}
```

这段代码定义了**比较时 affinity 的合并规则**,翻译成人话:

- **列 vs 列**(`a = b`,两列都有 affinity):如果任一是数值类(NUMERIC/INTEGER/REAL),用 NUMERIC 比较两边;否则用 NONE(不转)。
- **列 vs 字面量**(`a = 1` 或 `a = 'x'`,一边是列一边是常量):**用列的 affinity**。所以 `a = 1`(a 是 TEXT affinity)会把 `1` 当 TEXT 比;`a = '1'`(a 是 INTEGER affinity)会把 `'1'` 转成数 `1` 比。

这个规则保证了:**查询条件里的字面量,会被"列的偏好"拉着走**,而不是字面量自己定类型。这正是为什么 `WHERE score = 99`(score 是 NUMERIC/REAL 列)能查到 `score` 存 `'99'`(文本)的行——比较前 affinity 把 `'99'` 转成了数 `99`。

> **钉死这件事**:比较的 affinity 是**编译期**由 code generator 推导的(不是运行期),它根据"两边表达式各是什么 affinity"合并出一个统一的比较 affinity,塞进 `OP_Eq` 的 P5。这让 SQLite 在**不损失动态类型灵活性**的前提下,让比较结果尽量符合直觉(列偏好什么,字面量就被往那边拉)。这是一个"用编译期分析换运行期正确性"的精妙设计——和 P1-04 code generator 把 SQL 编译成 opcode 时做的各种优化是同一种思路。

### 排序:OP_Compare + OP_Jump

排序(`ORDER BY`/`GROUP BY`)和比较同源,但走的是另一条 opcode 路径:`OP_Compare`([vdbe.c:2525-2586 OP_Compare](../sqlite/src/vdbe.c#L2525-L2586)) + `OP_Jump`([vdbe.c:2596-2607 OP_Jump](../sqlite/src/vdbe.c#L2596-L2607))。`OP_Compare` 拿两个寄存器向量(P1 和 P2,各 n 个寄存器,对应排序键的各列),逐列调用 `sqlite3MemCompare` 比较,第一列不相等就出结果(经典的"多关键字排序"逻辑):

```c
/* vdbe.c:2563-2583  OP_Compare:多列排序键逐列比(简化) */
for(i=0; i<n; i++){
  pColl = pKeyInfo->aColl[i];                          /* 这一列用哪个 collation */
  bRev = (pKeyInfo->aSortFlags[i] & KEYINFO_ORDER_DESC);
  iCompare = sqlite3MemCompare(&aMem[p1+idx], &aMem[p2+idx], pColl);
  if( iCompare ){
    if( (pKeyInfo->aSortFlags[i] & KEYINFO_ORDER_BIGNULL)   /* NULL 排序控制 */
     && ((aMem[p1+idx].flags & MEM_Null) || (aMem[p2+idx].flags & MEM_Null))
    ){
      iCompare = -iCompare;
    }
    if( bRev ) iCompare = -iCompare;                    /* DESC 反转 */
    break;                                              /* 第一列不相等就出结果 */
  }
}
```

注意排序里两个排序标志([sqliteInt.h:2697-2698](../sqlite/src/sqliteInt.h#L2697-L2698)):

- `KEYINFO_ORDER_DESC`(0x01):**降序**。比较结果取反。
- `KEYINFO_ORDER_BIGNULL`(0x02):**NULL 排在最后**(big null)。默认(NULL 不开这个标志),SQLite 的 NULL 排在 ASC 排序的**最前面**(因为 NULL < 一切);开了 `BIGNULL`,NULL 排在**最后**(因为 NULL > 一切)。这个标志对应 SQL 的 `ORDER BY x DESC NULLS LAST` 之类语法。

`OP_Compare` 之后紧跟一个 `OP_Jump`,根据 `iCompare` 的符号(<0/=0/>0)跳到三个不同的地址,实现排序算法的分支(用于 sorter 归并)。这是 VDBE 实现排序的核心 opcode 对。

> **承接《Lua》**:`OP_Compare` + `OP_Jump` 这种"比较出符号,再根据符号跳转"的模式,和《Lua》虚拟机里 `OP_LT`/`OP_LE` 直接产出布尔/跳转的设计是同构的——都是"把比较结果编码成跳转目标",区别只在于 Lua VM 是单值比较、SQLite VDBE 是多列向量比较。这是字节码虚拟机实现"比较 + 跳转"的通用范式,承接《Lua》那本讲的 VM 基础,本书不重复。

---

## 五、collation:改写 TEXT 比较的规则

讲完了比较和排序,还有一个关键问题:**两个 TEXT 怎么比?** 默认是 `memcmp` 字节比(BINARY),但很多时候用户想要"大小写不敏感"(NOCASE)或"忽略尾部空格"(RTRIM)。这就是 **collation(比较序列)** 的作用——它是一个**可替换的函数指针**,决定两个 TEXT 怎么比。

### collation 是什么:一个函数指针

源码里 collation 是一个 `CollSeq` 结构([sqliteInt.h:2309-2315 CollSeq](../sqlite/src/sqliteInt.h#L2309-L2315)):

```c
/* sqliteInt.h:2309-2315  CollSeq:一个可替换的比较函数 */
struct CollSeq {
  char *zName;          /* collation 的名字,如 "BINARY"/"NOCASE"/"RTRIM" */
  u8 enc;               /* 文本编码(UTF-8/UTF-16le/UTF-16be) */
  void *pUser;          /* 传给 xCmp 的第一个参数 */
  int (*xCmp)(void*,int, const void*, int, const void*);   /* 真正的比较函数 */
  void (*xDel)(void*);  /* pUser 的析构函数 */
};
```

`xCmp` 是个函数指针——给它两个字节串(及其长度),它返回负/零/正(和 `memcmp` 同语义)。SQLite 比 TEXT 时,就是调这个 `xCmp`。换 collation = 换 `xCmp` 指向的函数。这是一个典型的"策略模式":比较逻辑做成可替换的函数,数据库内核只负责调用,不关心具体怎么比。

`sqlite3MemCompare` 在比两个 TEXT 时,如果传进来的 `CollSeq*` 不为空,就调它的 `xCmp`;如果为空,默认用 `memcmp`(相当于 BINARY)。

### 三个内置 collation:BINARY / NOCASE / RTRIM

SQLite 启动时,在 `openDatabase` 里注册三个内置 collation([main.c:3516-3520 createCollation](../sqlite/src/main.c#L3516-L3520)):

```c
/* main.c:3516-3520  注册三个内置 collation */
createCollation(db, sqlite3StrBINARY, SQLITE_UTF8,  0, binCollFunc, 0);
createCollation(db, sqlite3StrBINARY, SQLITE_UTF16BE, 0, binCollFunc, 0);
createCollation(db, sqlite3StrBINARY, SQLITEUTF16LE, 0, binCollFunc, 0);
createCollation(db, "NOCASE", SQLITE_UTF8, 0, nocaseCollatingFunc, 0);
createCollation(db, "RTRIM",  SQLITE_UTF8, 0, rtrimCollFunc, 0);
```

注意 BINARY 注册了**三种编码**(UTF-8/UTF-16BE/UTF-16LE),因为 BINARY 是字节比,三种编码都能用同一个 `binCollFunc`;而 NOCASE/RTRIM 只注册了 UTF-8(它们需要理解字符,Unicode 支持有限,只做 UTF-8 实现)。三个比较函数的实现都极简:

**BINARY**([main.c:1044-1061 binCollFunc](../sqlite/src/main.c#L1044-L1061))——直接 `memcmp`,短的算小:

```c
/* main.c:1044-1061  BINARY:逐字节 memcmp */
static int binCollFunc(
  void *NotUsed,
  int nKey1, const void *pKey1,
  int nKey2, const void *pKey2
){
  int rc, n;
  n = nKey1<nKey2 ? nKey1 : nKey2;       /* 取较短长度 */
  rc = memcmp(pKey1, pKey2, n);          /* 逐字节比 */
  if( rc==0 ){
    rc = nKey1 - nKey2;                  /* 前缀相同,短的算小 */
  }
  return rc;
}
```

**NOCASE**([main.c:1096-1108 nocaseCollatingFunc](../sqlite/src/main.c#L1096-L1108))——ASCII 大小写不敏感,调 `sqlite3StrNICmp`:

```c
/* main.c:1096-1108  NOCASE:ASCII 大小写不敏感比较 */
static int nocaseCollatingFunc(
  void *NotUsed,
  int nKey1, const void *pKey1,
  int nKey2, const void *pKey2
){
  int r = sqlite3StrNICmp(               /* 不区分大小写的字节比 */
      (const char *)pKey1, (const char *)pKey2, (nKey1<nKey2)?nKey1:nKey2);
  if( 0==r ){
    r = nKey1-nKey2;
  }
  return r;
}
```

**RTRIM**([main.c:1067-1077 rtrimCollFunc](../sqlite/src/main.c#L1067-L1077))——先各自砍掉尾部空格,再 BINARY 比:

```c
/* main.c:1067-1077  RTRIM:忽略尾部空格 */
static int rtrimCollFunc(
  void *pUser,
  int nKey1, const void *pKey1,
  int nKey2, const void *pKey2
){
  const u8 *pK1 = (const u8*)pKey1;
  const u8 *pK2 = (const u8*)pKey2;
  while( nKey1 && pK1[nKey1-1]==' ' ) nKey1--;   /* 砍掉尾部空格 */
  while( nKey2 && pK2[nKey2-1]==' ' ) nKey2--;
  return binCollFunc(pUser, nKey1, pKey1, nKey2, pKey2);   /* 再 BINARY 比 */
}
```

三个函数加起来不到 40 行,极简。但有几个**关键事实**要钉死:

### 事实一:NOCASE 只懂 26 个英文字母

`sqlite3StrNICmp` 的"不区分大小写"**只对 ASCII 的 A-Z/a-z 有效**,不懂 Unicode。源码注释([main.c:1087-1095](../sqlite/src/main.c#L1087-L1095))明确说:"SQLite's knowledge of upper and lower case equivalents extends only to the 26 characters used in the English language."(SQLite 对大小写等价的知识,仅限于英语的 26 个字符。)

所以 `'É'`(带重音的 E)和 `'é'` 在 NOCASE 下**不相等**——NOCASE 把它们当不同的字节。如果你要 Unicode 大小写不敏感,得自己注册一个 collation(用 `sqlite3_create_collation`),SQLite 内置不提供。这是一个**有意的简化**:Unicode 大小写规则极复杂(土耳其语的 i/İ、德语 ß=ss、阿拉伯语没有大小写…),内置一个"几乎肯定有人会用错"的 Unicode NOCASE 不如只做 ASCII,把复杂情况交给应用层。

> **不这样会怎样**:如果 SQLite 内置一个"完整的 Unicode NOCASE",要么引入 ICU 库(体积大,违背嵌入式极小原则),要么自己实现(几乎肯定有 bug,会被各种语言的用户骂)。SQLite 选"只做 ASCII + 允许用户自己注册",是把"正确性责任"和"体积"都交给用户——这是嵌入式数据库的典型哲学:**内核只做最小正确的事,复杂的事留给应用**。承接《LevelDB》那本讲的"嵌入式 = 内核极简 + 应用补全"思路。

### 事实二:collation 决定索引能否加速查询

collation 不只是"比较规则",它还**直接影响索引能否用**。因为索引是按某个 collation 排序的 B-tree——如果索引建在 `name COLLATE NOCASE` 上,那 `WHERE name = 'ALICE'` 能用这个索引(NOCASE 下 `'ALICE'='alice'`);但如果索引建在默认 BINARY 上,`WHERE name = 'ALICE' COLLATE NOCASE` **不能用这个索引**(collation 不匹配,索引顺序和查询需要的顺序不一致)。

SQLite 在 code generator 里用 `sqlite3IndexAffinityOk`([expr.c:389-398 sqlite3IndexAffinityOk](../sqlite/src/expr.c#L389-L398))和一系列 collation 匹配检查来决定走不走索引。这是个优化器话题(P3-10 讲过索引选择),这里只点出:**collation 是类型系统的一部分,它和索引、查询优化深度耦合**,不是孤立的"比较函数"。

### 事实三:每个 TEXT 列默认有一个 collation

建表时,每个 TEXT 列默认继承 BINARY collation。你可以显式指定:`CREATE TABLE t(name TEXT COLLATE NOCASE)`,这样这一列的所有比较(包括索引)默认用 NOCASE。SQL 表达式里也可以临时指定:`WHERE name = 'alice' COLLATE RTRIM`。

collation 的选择规则(code generator 在 `sqlite3BinaryCompareCollSeq` 里实现):左操作数有 `COLLATE` 就用左边的;否则用右边的;都没有就用默认 BINARY。这是 SQL 标准的 collation 强制规则(coercion rules),SQLite 严格遵循。

> **钉死这件事**:collation 是 SQLite 类型系统里"TEXT 比较的规则层",它把"两个文本怎么比"从内核里抽出来,做成可替换的函数指针(`CollSeq.xCmp`)。三个内置 collation(BINARY/NOCASE/RTRIM)覆盖了最常见的三种需求,复杂的 Unicode 需求交给应用自己注册。collation 和索引、查询优化深度耦合——**选错 collation,索引就用不上,查询就慢**。理解了 collation,你就理解了为什么 SQLite 的 `WHERE name='ALICE'` 在不同 collation 下结果不同、能不能走索引也不同。

---

## 六、CAST 和 typeof:强制转换 vs 类型探针

讲完了 affinity(温柔试转)和比较/collation(语义规则),还有两个工具函数值得单独拆清:`CAST(X AS type)`(强制转换)和 `typeof(X)`(类型探针)。这俩和 affinity 是**三条独立路径**,经常被混为一谈,必须分清。

### typeof:值存储类型的诚实探针

`typeof(X)` 前面讲过——返回值 `X` 的**存储类型**(5 种之一),与列定义、与 affinity 都无关。源码 [func.c:79-98 typeofFunc](../sqlite/src/func.c#L79-L98) 就是查 `sqlite3_value_type()`。它是 SQLite 类型系统里最诚实的探针:**它告诉你这个值客观上是什么**,不被任何偏好影响。

typeof 最有用的场景是**调试动态类型的反直觉行为**:

```sql
CREATE TABLE t(a INTEGER);            -- INTEGER affinity
INSERT INTO t VALUES(123);            -- INTEGER affinity 不转,存 integer
INSERT INTO t VALUES('hello');        -- INTEGER affinity 试转 'hello' 失败,存 text
INSERT INTO t VALUES('456');          -- INTEGER affinity 试转 '456' 成功,存 integer
INSERT INTO t VALUES(3.14);           -- INTEGER affinity 试转 3.14 失败(有小数),存 real

SELECT typeof(a), a FROM t;
--  integer  123
--  text     hello      ← 反直觉!INTEGER 列里存了 text
--  integer  456        ← '456' 被转成了 integer
--  real     3.14       ← 3.14 没被转成 integer,存成 real
```

这张结果表把 INTEGER affinity 的行为展现得淋漓尽致:`123` 不转(本来就是整数)、`'hello'` 转不成保留 text(不报错!)、`'456'` 转成 integer、`3.14` 没法无损转整数所以保留 real。**typeof 是你理解 SQLite 类型行为的眼睛**——遇到任何"这怎么存进去了"的困惑,第一反应就是 `SELECT typeof(x), x`。

### CAST:强制转换,失败给默认值

`CAST(X AS type)` 是 SQL 标准的强制转换操作符。它和 affinity 有两点根本不同:

1. **强制 vs 温柔**:affinity 转不成保留原值;CAST 转不成**给个默认值**(转整数给 0、转实数给 0.0)。
2. **结果类型确定 vs 不确定**:affinity 应用后值类型可能变也可能不变(看转成没);CAST 后**值类型一定变成目标类型**(强制)。

源码里 CAST 走 `OP_Cast` opcode([vdbe.c:2197-2213 OP_Cast](../sqlite/src/vdbe.c#L2197-L2213)),它调 `sqlite3VdbeMemCast`([vdbemem.c:926-966 sqlite3VdbeMemCast](../sqlite/src/vdbemem.c#L926-L966)):

```c
/* vdbe.c:2197-2213  OP_Cast:强制类型转换 */
case OP_Cast: {                  /* in1 */
  assert( pOp->p2>=SQLITE_AFF_BLOB && pOp->p2<=SQLITE_AFF_REAL );
  pIn1 = &aMem[pOp->p1];
  ...
  rc = sqlite3VdbeMemCast(pIn1, pOp->p2, encoding);   /* 强制转换 */
  ...
}
```

```c
/* vdbemem.c:926-966  强制 cast(简化) */
int sqlite3VdbeMemCast(Mem *pMem, u8 aff, u8 encoding){
  if( pMem->flags & MEM_Null ) return SQLITE_OK;   /* NULL 不变 */
  switch( aff ){
    case SQLITE_AFF_BLOB:   /* 强制 BLOB:先 stringify 再改标志 */
      ...
    case SQLITE_AFF_NUMERIC:   /* 强制 NUMERIC:能整则整,否则实 */
      sqlite3VdbeMemNumerify(pMem);  break;
    case SQLITE_AFF_INTEGER:   /* 强制整数:截断成整数 */
      sqlite3VdbeMemIntegerify(pMem);  break;
    case SQLITE_AFF_REAL:     /* 强制实数 */
      sqlite3VdbeMemRealify(pMem);  break;
    default:  /* SQLITE_AFF_TEXT:强制 stringify */
      ...
  }
  return SQLITE_OK;
}
```

`sqlite3VdbeMemCast` 的几个分支调的是 `*ify` 系列函数(`Numerify`/`Integerify`/`Realify`),它们是**强制转换**——比如 `Integerify` 把 `'hello'` 转成 `0`(用 `atoi` 语义),把 `3.14` 截断成 `3`。这和 affinity 的"温柔试转"完全不同。

CAST 的几个典型行为:

```sql
SELECT CAST('hello' AS INTEGER);   -- 0(转不成,默认值)
SELECT CAST('123abc' AS INTEGER);  -- 123(前缀能解析的部分)
SELECT CAST(3.14 AS INTEGER);      -- 3(截断)
SELECT CAST(123 AS REAL);          -- 123.0
SELECT CAST(x'4142' AS TEXT);      -- 'AB'(BLOB 按 UTF-8 解码)
SELECT CAST('AB' AS BLOB);         -- x'4142'(TEXT 按字节转 BLOB)
```

> **钉死这件事**:`applyAffinity`(温柔,存入时/比较时)和 `sqlite3VdbeMemCast`(强制,CAST 时)是**两条独立的路径**——前者失败保留原值,后者失败给默认值。CAST 是 SQL 标准的强制转换,affinity 是 SQLite 独创的柔性偏好。理解了"温柔 vs 强制"、"何时触发",你就不会被 SQLite 的类型行为绕晕。`typeof` 是探针(只读,返回真实类型),CAST 是改写(强制,改变类型),affinity 是偏好(温柔,试转)——三者各司其职,组合起来构成 SQLite 类型系统的完整工具箱。

### CAST 怎么编译成 OP_Cast

CAST 在 SQL 里是 `CAST(expr AS typename)` 语法,Parser 把它建成 `TK_CAST` 节点。code generator 遇到 `TK_CAST`([expr.c:5176-5182 TK_CAST](../sqlite/src/expr.c#L5176-L5182)),先算 `expr` 的值放进寄存器,然后发一条 `OP_Cast`,P2 是把 `typename` 用 `sqlite3AffinityType` 推导出的 affinity:

```c
/* expr.c:5176-5182  TK_CAST 编译成 OP_Cast */
case TK_CAST: {
  /* Expressions of the form:   CAST(pLeft AS token) */
  sqlite3ExprCode(pParse, pExpr->pLeft, target);   /* 先算左操作数 */
  sqlite3VdbeAddOp2(v, OP_Cast, target,
                    sqlite3AffinityType(pExpr->u.zToken, 0));   /* 再 cast */
  return inReg;
}
```

注意 `OP_Cast` 的 P2 范围被 assert 限制在 `SQLITE_AFF_BLOB .. SQLITE_AFF_REAL`([vdbe.c:2198](../sqlite/src/vdbe.c#L2198))——也就是说 CAST 只支持 5 种基本类型(BLOB/NUMERIC/INTEGER/REAL/TEXT),不支持 NONE/FLEXNUM/DEFER 这些内部 affinity。这是合理的:CAST 是 SQL 标准操作,只暴露标准类型;内部的 affinity(NONE/FLEXNUM)是 SQLite 实现细节,不对 SQL 层开放。

---

## 七、STRICT 表(3.37+):给动态类型补一层可选强类型

讲到这里,你已经理解了 SQLite 的动态类型系统——值定类型 + 列 affinity 温柔试转。但有些场合,用户就是想要 MySQL/PG 那样的强类型(防止"我明明声明 INTEGER,却存进了 hello"这种事)。**3.37.0(2021-11-27)** 引入的 **STRICT 表**就是给这个需求的一条退路。

### STRICT 是什么:可选强类型,不是替代动态类型

STRICT 表是用 `CREATE TABLE ... STRICT` 语法声明的表:

```sql
CREATE TABLE t(a INTEGER, b TEXT, c REAL) STRICT;
INSERT INTO t VALUES(1, 'hello', 3.14);      -- OK
INSERT INTO t VALUES('hello', 1, 3.14);      -- 报错!cannot store text value in integer column t.a
```

STRICT 表的规则:**列声明什么类型,就必须存什么类型,违者报错**(`SQLITE_CONSTRAINT_DATATYPE`)。这是 SQLite 在动态类型基础上**叠加**的一层强类型——不是放弃动态类型(普通表还是动态的),而是给那些"我就要强类型"的场合一个开关。

> **版本交代**:STRICT 关键字是 **3.37.0(2021-11-27)** 加的。注意:STRICT 表的数据库文件**不能用 3.37 之前的 SQLite 打开**(因为 STRICT 标志写进了 schema,老版本不认识会拒绝)。这是一个**前向不兼容**的特性,迁移时要注意。这是 SQLite 演进里少见的"破坏老版本兼容"的特性,值得讲清版本。

### STRICT 表的限制:只允许 5 种基本类型名

STRICT 表的列,**类型名必须是 5 种基本类型之一**(源码 [build.c:2696-2732](../sqlite/src/build.c#L2696-L2732)):

```c
/* build.c:2696-2732  STRICT 表的列类型限制(简化) */
if( tabOpts & TF_Strict ){
  p->tabFlags |= TF_Strict;
  for(ii=0; ii<p->nCol; ii++){
    Column *pCol = &p->aCol[ii];
    if( pCol->eCType==COLTYPE_CUSTOM ){
      /* 自定义类型(如 VARCHAR/DECIMAL/DATE)在 STRICT 表里不允许 */
      if( pCol->colFlags & COLFLAG_HASTYPE ){
        sqlite3ErrorMsg(pParse,
          "unknown datatype for %s.%s: \"%s\"",
          p->zName, pCol->zCnName, sqlite3ColumnType(pCol, ""));
      }else{
        sqlite3ErrorMsg(pParse, "missing datatype for %s.%s",
                        p->zName, pCol->zCnName);
      }
      return;
    }else if( pCol->eCType==COLTYPE_ANY ){
      pCol->affinity = SQLITE_AFF_BLOB;   /* ANY 列接受任何类型 */
    }
    /* 非 INTEGER PRIMARY KEY 的主键列强制 NOT NULL */
    if( (pCol->colFlags & COLFLAG_PRIMKEY)!=0
     && p->iPKey!=ii && pCol->notNull == OE_None ){
      pCol->notNull = OE_Abort;
    }
  }
}
```

也就是说,STRICT 表的列类型名只能是 `INT`/`INTEGER`/`REAL`/`TEXT`/`BLOB`(和 `ANY`)这 6 个**标准类型名**(对应 `eCType` 的 `COLTYPE_INT`/`COLTYPE_INTEGER`/`COLTYPE_REAL`/`COLTYPE_TEXT`/`COLTYPE_BLOB`/`COLTYPE_ANY`,定义在 [sqliteInt.h:2270-2276](../sqlite/src/sqliteInt.h#L2270-L2276))。你写 `VARCHAR(100)`、`DECIMAL(10,2)`、`DATE` 这些**自定义类型名**(`COLTYPE_CUSTOM`),STRICT 表会直接报错"unknown datatype"。

这 6 个标准类型名是怎么识别的?在 `sqlite3AddColumn`([build.c:1544-1560](../sqlite/src/build.c#L1544-L1560))里,SQLite 把类型名和 `sqlite3StdType[]` 数组([global.c:404-411](../sqlite/src/global.c#L404-L411))逐字比——这个数组就是 6 个标准名:

```c
/* global.c:404-411  6 个标准类型名(顺序对应 COLTYPE_ANY/BLOB/INT/INTEGER/REAL/TEXT) */
const char *sqlite3StdType[] = {
  "ANY",     /* COLTYPE_ANY    —— 接受任何类型 */
  "BLOB",    /* COLTYPE_BLOB   —— 必须 BLOB */
  "INT",     /* COLTYPE_INT    —— 必须整数 */
  "INTEGER", /* COLTYPE_INTEGER —— 整数,且可当 rowid 别名 */
  "REAL",    /* COLTYPE_REAL   —— 必须实数(或能转实数的整数) */
  "TEXT"     /* COLTYPE_TEXT   —— 必须 TEXT */
};
```

这 6 个名字的 affinity 映射在 `sqlite3StdTypeAffinity[]`([global.c:396-403](../sqlite/src/global.c#L396-L403)):`ANY`→NUMERIC(实际存什么看值)、`BLOB`→BLOB、`INT/INTEGER`→INTEGER、`REAL`→REAL、`TEXT`→TEXT。

### STRICT 表怎么强制类型:OP_TypeCheck

STRICT 表的强制类型检查,靠一个专门的 opcode:`OP_TypeCheck`([vdbe.c:3340-3428 OP_TypeCheck](../sqlite/src/vdbe.c#L3340-L3428))。这个 opcode 在 INSERT/UPDATE 时,被 code generator **插在 `OP_MakeRecord` 之前**——先做严格的类型检查,通过才允许造 Record 写入。

`OP_TypeCheck` 的逻辑([vdbe.c:3363-3418](../sqlite/src/vdbe.c#L3363-L3418)):对每一列,先 `applyAffinity`(应用列的 affinity,温柔试转),然后检查转换后的值类型**是否匹配列的 `eCType`**——不匹配就跳 `vdbe_type_error`:

```c
/* vdbe.c:3363-3418  OP_TypeCheck:严格类型检查(简化,保留关键逻辑) */
for(; i<nCol; i++){
  ...
  applyAffinity(pIn1, aCol[i].affinity, encoding);   /* 先温柔试转 */
  if( (pIn1->flags & MEM_Null)==0 ){
    switch( aCol[i].eCType ){
      case COLTYPE_BLOB: {
        if( (pIn1->flags & MEM_Blob)==0 ) goto vdbe_type_error;   /* 不是 BLOB 报错 */
        break;
      }
      case COLTYPE_INTEGER:
      case COLTYPE_INT: {
        if( (pIn1->flags & MEM_Int)==0 ) goto vdbe_type_error;    /* 不是整数报错 */
        break;
      }
      case COLTYPE_TEXT: {
        if( (pIn1->flags & MEM_Str)==0 ) goto vdbe_type_error;    /* 不是 TEXT 报错 */
        break;
      }
      case COLTYPE_REAL: {
        /* REAL 列:整数也接受,但标成 IntReal(为了省空间) */
        if( pIn1->flags & MEM_Int ){
          if( pIn1->u.i<=140737488355327LL && pIn1->u.i>=-140737488355328LL){
            pIn1->flags |= MEM_IntReal;   /* 整数按整数存,但语义是实数 */
          }else{
            pIn1->u.r = (double)pIn1->u.i;   /* 太大,转真浮点 */
          }
        }else if( (pIn1->flags & (MEM_Real|MEM_IntReal))==0 ){
          goto vdbe_type_error;            /* 既不是整数也不是浮点,报错 */
        }
        break;
      }
      default: { /* COLTYPE_ANY:接受任何类型 */ break; }
    }
  }
  pIn1++;
}
...
vdbe_type_error:
  sqlite3VdbeError(p, "cannot store %s value in %s column %s.%s",
     vdbeMemTypeName(pIn1), sqlite3StdType[aCol[i].eCType-1],
     pTab->zName, aCol[i].zCnName);
  rc = SQLITE_CONSTRAINT_DATATYPE;   /* 类型违反,报这个错 */
  goto abort_due_to_error;
```

注意 STRICT 表的检查逻辑有几个**精妙**之处:

1. **先 affinity 试转,再严格检查**:STRICT 不是"类型必须一开始就匹配",而是"先按 affinity 试转,转完后看类型对不对"。所以 STRICT 表的 `INTEGER` 列,你插 `'456'`(文本)是**可以的**——因为 INTEGER affinity 先把 `'456'` 转成了整数 `456`,检查时是 `MEM_Int`,通过。但插 `'hello'` 不行——affinity 转不成,还是 `MEM_Str`,检查 `COLTYPE_INTEGER` 时报错。这让 STRICT 表"既严格又不过分死板"(允许合理的字符串到数的转换)。

2. **REAL 列接受整数,标成 IntReal**:STRICT 的 REAL 列,你插整数 `5` 不会报错——它会被标成 `MEM_IntReal`(语义实数,存储整数),为了省空间。这和普通表的 REAL affinity 行为一致,体现了 SQLite "省空间"哲学贯穿 STRICT 模式。

3. **ANY 列接受任何类型**:`ANY` 是 STRICT 表里的"逃逸舱"——如果你某一列就是想存混合类型(动态),用 `ANY` 类型,STRICT 也不限制它。这让 STRICT 表能"局部动态"(大部分列严格,个别列 ANY)。

> **钉死这件事**:STRICT 表不是"放弃动态类型",而是"在动态类型基础上,加一个可选的强类型检查层"。它的实现是 `OP_TypeCheck` opcode——在 INSERT/UPDATE 时插在 `OP_MakeRecord` 之前,先 affinity 试转、再严格检查类型匹配,不匹配报 `SQLITE_CONSTRAINT_DATATYPE`。**STRICT = 动态类型 + 一道严格关卡**,不是推倒重来。这是 SQLite 演进温和的体现:它没有为了"强类型需求"重构整个类型系统,而是加了一个 opcode + 一个表标志(`TF_Strict`),就把强类型作为"可选项"叠加上了——向后兼容(老表还是动态),向前出新(新表可 STRICT)。

### STRICT vs 普通表:一张对照表

| 维度 | **普通表(动态)** | **STRICT 表(3.37+)** |
|---|---|---|
| 声明语法 | `CREATE TABLE t(...)` | `CREATE TABLE t(...) STRICT` |
| 列类型名 | 任意(推导成 affinity) | 只能是 INT/INTEGER/REAL/TEXT/BLOB/ANY |
| 插入不匹配类型 | 试转,转不成**保留原值不报错** | 试转,转不成**报 `SQLITE_CONSTRAINT_DATATYPE`** |
| 同一列混存类型 | **能** | **不能**(除 ANY 列) |
| `typeof()` 行为 | 反映值真实类型(可能 ≠ 列类型) | 反映值真实类型(强制等于列类型) |
| 实现机制 | 只有 affinity(`OP_Affinity`/`OP_MakeRecord`) | 多一道 `OP_TypeCheck` 严格关卡 |
| 兼容性 | 所有版本 | 3.37.0+,老版本打不开含 STRICT 表的库 |

> **承接《MySQL·InnoDB》**:STRICT 表是 SQLite 给"MySQL/PG 那种强类型需求"开的退路。但即便开了 STRICT,SQLite 的强类型和 MySQL 还是有差别——MySQL 的强类型是**全局默认**的(所有表都强类型),SQLite 的 STRICT 是**逐表可选**的(你想强类型才加 STRICT 关键字)。而且 SQLite 的 STRICT 只检查 5 种基本类型,不支持 MySQL 那么丰富的类型(`DECIMAL`/`ENUM`/`SET`/`JSON`/`TIMESTAMP`…)——STRICT 是"够用的强类型",不是"MySQL 完整强类型"。这是嵌入式哲学的延续:**内核只做最小必要,复杂需求留给应用或不用**。

### 什么时候该用 STRICT

STRICT 不是默认,是个选项。什么时候该开?经验法则:

- **数据来源可控、schema 稳定**(如应用自己的配置表、业务核心表):**开 STRICT**。防止 bug 把错误类型数据塞进去,提早暴露问题。
- **数据来源杂、schema 灵活**(如 CSV 导入、JSON 缓存、原型期):**别开 STRICT**。动态类型的灵活性更重要。
- **混合**(核心表严格,边缘表灵活):**部分开 STRICT**。SQLite 允许同一库里有的表 STRICT、有的表动态,按需选。

> **钉死这件事**:STRICT 是 SQLite 类型系统的"安全带"——平时(动态类型)不系,允许灵活;需要严谨时(核心数据)系上,防止类型错误。它体现了 SQLite 一贯的设计哲学:**给用户选择权,而不是替用户做决定**。你可以全程用动态类型(像 Python),也可以全程用 STRICT(像 Rust),还可以混用——SQLite 都支持,不强制。

---

## 八、技巧精解:两个最硬核的设计

本章正文讲完。技巧精解环节,挑两个最硬核、最值得单独钉死的技巧拆透——它们是 SQLite 类型系统的两块基石。

### 技巧一:用 ASCII 连续字符编码 affinity——一个 `>=` 切分数值类

第一个硬核技巧,是 affinity 常量值的设计——它不是随便取的整数,而是**精心选择的连续 ASCII 字符**(`@ A B C D E F`)。这个看似不起眼的设计,带来了三个工程上的妙处。

**朴素做法会怎样**:如果 affinity 用普通整数枚举(`NONE=0, BLOB=1, TEXT=2, NUMERIC=3, INTEGER=4, REAL=5, FLEXNUM=6`),那判断"是不是数值类 affinity"要写成:

```c
/* 朴素写法:判断数值类 affinity 要列举 */
if( affinity==SQLITE_AFF_NUMERIC || affinity==SQLITE_AFF_INTEGER
 || affinity==SQLITE_AFF_REAL || affinity==SQLITE_AFF_FLEXNUM ){
  ...
}
```

四个 `==` 或起来,啰嗦且容易漏(加新 affinity 时要记得改这里)。

**SQLite 的巧招**:它把 affinity 编成连续 ASCII 字符,且**数值类(NUMERIC/INTEGER/REAL/FLEXNUM)全排在一起**(0x43-0x46),于是判断数值类只要一句:

```c
/* sqliteInt.h:2347  一个 >= 切分数值类 */
#define sqlite3IsNumericAffinity(X)  ((X)>=SQLITE_AFF_NUMERIC)
```

这一个 `>=` 替代了四个 `==`——**而且对未来扩展免疫**(如果以后加 `FLEXNUM2`=`0x47`,只要它排在 REAL 后面,这个判断自动包含它,不用改代码)。这是"用值的连续性简化判断"的经典操作。

**第二个妙处**:把"一行的 affinity 串"压成 C 字符串。`OP_MakeRecord`/`OP_Affinity` 的 P4 参数是一个字符串,每个字符是一列的 affinity。因为 affinity 是 ASCII 可打印字符,这个字符串可以直接 `strcmp` 比较、可以直接 printf 调试、可以用 C 字符串函数处理。如果 affinity 是 0-6 的整数,P4 就得是 `int` 数组,处理起来麻烦得多。

**第三个妙处**:`SQLITE_AFF_MASK`(0x47)可以用来从 P5 里"掩码提取" affinity。比较 opcode 的 P5 既有 affinity(低 7 位)又有其他标志(`SQLITE_NULLEQ`/`SQLITE_JUMPIFNULL` 等,高位),用 `P5 & SQLITE_AFF_MASK` 一次提取出 affinity 字符。这也是因为 affinity 用连续 ASCII(都在 0x40-0x47 范围),才能用一个掩码搞定。

**为什么 sound(正确性)**:这套设计正确的前提是"affinity 的编码值满足两个偏序关系":① 数值类连续(>=NUMERIC 一刀切);② 都在 0x40-0x47 范围(掩码提取)。SQLite 通过精心选择常量值保证了这两点,代码里到处是 `assert(affinity>=SQLITE_AFF_BLOB && ...)` 之类的断言守护这个不变量。一旦加新 affinity,必须维护这个偏序——这是"用编码约定换代码简洁"的代价,但收益(简洁 + 可扩展)远大于代价。

> **钉死这件事**:affinity 用连续 ASCII 字符编码,是一个"看似随意、实则精心"的设计。它用"数值类连续排列"这一个约定,换来了"判断数值类一个 `>=`、P4 压成字符串、P5 掩码提取"三个工程便利,而且对未来扩展免疫。这是 C 语言"用编码代替逻辑"的典范——和 P3-09 讲的"serial type 用奇偶区分 BLOB/TEXT"是同一种智慧:**观察数据的本质规律,把它编码进表示层,让逻辑层自然简化**。

### 技巧二:STRICT 用一个 opcode 叠加强类型——不改类型系统骨架

第二个硬核技巧,是 STRICT 表的实现策略——**它没有重构 SQLite 的类型系统骨架,而是用一个 opcode(`OP_TypeCheck`)+ 一个表标志(`TF_Strict`)就把强类型"叠加"上了**。这是一个"用最小侵入实现新特性"的教科书级设计。

**朴素做法会怎样**:如果让你给一个"原本动态类型"的数据库加"可选强类型"功能,朴素做法可能是——给 `Column` 结构加一个"strict mode"标志,然后在所有写路径(INSERT/UPDATE/导入)都加上类型检查分支:

```c
/* 朴素写法:每个写路径都加 STRICT 检查分支 */
if( pTab->isStrict ){
  if( !checkColumnType(pCol, value) ) return error;
}
... 写入 Record ...
```

这意味着要**侵入所有写路径**(INSERT 的 code generator、UPDATE 的 code generator、`INSERT INTO ... SELECT` 的 code generator、导入工具……),每个地方都加分支。代码分散、容易漏(某个写路径忘了加检查就成了漏洞)、维护负担重。

**SQLite 的巧招**:它观察到——**所有写路径最终都要经过 `OP_MakeRecord`**(因为不管哪条路径写,最后都要把值拼成 Record 写进 B-tree)。那么,只需要在 `OP_MakeRecord` **之前**插一个 `OP_TypeCheck`,就一次性覆盖了所有写路径!

具体实现在 `sqlite3TableAffinity`([insert.c:179-203 sqlite3TableAffinity](../sqlite/src/insert.c#L179-L203)):

```c
/* insert.c:179-203  STRICT 表:把 OP_TypeCheck 插在 OP_MakeRecord 前 */
void sqlite3TableAffinity(Vdbe *v, Table *pTab, int iReg){
  if( pTab->tabFlags & TF_Strict ){
    if( iReg==0 ){
      /* 已经生成了 OP_MakeRecord,把它"往后挪一格",
      ** 在它原来的位置插入 OP_TypeCheck(用同一个寄存器组) */
      VdbeOp *pPrev;
      sqlite3VdbeAppendP4(v, pTab, P4_TABLE);
      pPrev = sqlite3VdbeGetLastOp(v);
      assert( pPrev->opcode==OP_MakeRecord ... );
      pPrev->opcode = OP_TypeCheck;          /* 原 MakeRecord 改名 TypeCheck */
      pPrev->p3 = 0;
      sqlite3VdbeAddOp3(v, OP_MakeRecord, pPrev->p1, pPrev->p2, p3);  /* 再补一个 MakeRecord */
    }else{
      /* 还没生成 MakeRecord,单独插一个 TypeCheck */
      sqlite3VdbeAddOp2(v, OP_TypeCheck, iReg, pTab->nNVCol);
      sqlite3VdbeAppendP4(v, pTab, P4_TABLE);
    }
    return;
  }
  /* 普通表:走 affinity 路径(生成 OP_Affinity 或 MakeRecord 带 P4) */
  ...
}
```

这段代码有个极巧的细节——**当 code generator 已经发出了 `OP_MakeRecord`**,它**就地把它改名成 `OP_TypeCheck`**,然后在后面**补一个真正的 `OP_MakeRecord`**。这样原本的 `MakeRecord` 操作数(P1/P2 寄存器组)被 `TypeCheck` 复用(检查同一组寄存器),检查通过后才执行后面那个真正的 `MakeRecord`。这是"在已有的 opcode 流里无损插入一道关卡"的优雅操作——不改变寄存器分配,不改变后续 opcode,只在中间多一道检查。

**为什么 sound(正确性)**:这个机制正确,前提是"所有写路径都经过 `OP_MakeRecord`"。SQLite 的设计保证了这一点——不管 INSERT/UPDATE/`INSERT SELECT`/导入,任何把值写进表的操作,最后都要把值拼成 Record(因为 B-tree cell 的 payload 就是 Record)。所以只要在 `OP_MakeRecord` 前插 `OP_TypeCheck`,就覆盖了所有写路径,无遗漏。这是一个"找到系统的瓶颈点(所有写都经过的汇合点),在那里加一道关卡"的架构智慧——和 P3-08 讲的"B-tree 是所有表/索引存储的汇合点"是同一种"找汇合点"的思路。

> **不这样会怎样**:如果 SQLite 朴素地"侵入每个写路径加 STRICT 分支",代码会分散在 insert.c/update.c/select.c(INSERT SELECT)/各种导入工具里,维护噩梦,而且容易漏(某条新加的写路径忘了检查就成了类型安全漏洞)。用一个 opcode 在汇合点加关卡,代码集中(就 `OP_TypeCheck` 一个 case)、覆盖完整(所有写都过 MakeRecord)、易维护(改 STRICT 逻辑只改 `OP_TypeCheck` 一处)。这是"最小侵入实现新特性"的典范,体现了 SQLite 代码的高度模块化——opcode 是天然的扩展点。

> **钉死这件事**:STRICT 的实现策略是"**opcode 层的扩展点**"——用一个新 opcode(`OP_TypeCheck`)+ 一个表标志(`TF_Strict`),在不改类型系统骨架的前提下,把强类型作为可选项叠加。这背后是一个深刻的架构洞察:**所有写路径都汇合于 `OP_MakeRecord`,在那里加关卡就覆盖全部**。这种"找汇合点、加扩展点"的设计,让 SQLite 能温和地演进(加 STRICT 这种大特性,只动一个 opcode,不重构)——这正是 SQLite "几十年演进无大重构"的工程秘诀之一。

---

## 九、章末小结

### 回扣主线

本章服务**存储与事务**这一面——具体说,是"数据在 SQL 语义层怎么被类型化对待"。从 P5-15(VFS,怎么跨平台读写文件)接过来,我们往上抬一层,看**值的类型语义**:affinity 怎么决定值能不能存、值之间怎么比、怎么排、collation 怎么改写比较、STRICT 怎么补强类型。本章和 P3-09(Record 字节格式)合起来,才是 SQLite 类型系统的完整画像——P3-09 是"值在磁盘上的字节布局",本章是"值在 SQL 语义上的类型规则"。两章正交互补:

1. **两套类型体系**:存储类型(5 种,值级,由值定)+ affinity(7 种,列级,柔性偏好),正交互补。
2. **affinity 转换矩阵**:每种 affinity 收到不同类型值的温柔试转规则,转不成保留原值不报错(BLOB 永不转,REAL 收整数标 IntReal 省空间)。
3. **比较与排序的类型顺序**:`NULL < INTEGER/REAL < TEXT < BLOB`,跨类型按固定顺序比,同类型按数值/collation/memcmp 比;`OP_Eq`/`OP_Compare` 在比较前先 affinity 对齐。
4. **collation**:`BINARY`(memcmp)/`NOCASE`(ASCII 大小写不敏感)/`RTRIM`(忽略尾部空格),做成可替换函数指针,和索引/查询优化深度耦合。
5. **STRICT 表(3.37+)**:`CREATE TABLE ... STRICT` 给动态类型补一层可选强类型,实现是 `OP_TypeCheck` opcode 叠加在 `OP_MakeRecord` 前,向后兼容。
6. **CAST/typeof/affinity 三条路径**:CAST 强制转换(失败给默认值)、typeof 返回真实存储类型、affinity 温柔试转——各司其职。

### 五个为什么

1. **为什么 SQLite 把"类型"拆成存储类型(值级)和 affinity(列级)两套?**——存储类型服务"值在磁盘上怎么紧凑编码"(P3-09 的 serial type),affinity 服务"值在 SQL 语义上怎么被对待"(本章的转换/比较)。两套正交,让 SQLite 既能按值大小省空间(动态),又能让列有偏好(亲和),兼顾灵活和直觉。
2. **为什么 affinity 是"温柔试转"而不是"强制转换"?**——嵌入式场景数据来源杂(CSV/JSON),强制类型会报错体验差;温柔试转转不成保留原值,既符合直觉(列偏好什么就尽量转)又不破坏数据(转不成不丢)。这是"灵活优先于严格"的嵌入式取舍。
3. **为什么比较的类型顺序是 `数 < TEXT < BLOB`,而不是跨类型"智能转换"?**——智能转换(该转成什么比)有歧义且不可预测;固定顺序(数永远小于文本)虽然有时反直觉(`999999 < 'a'`)但**可预测**。可预测的反直觉比不可预测的智能安全得多——这是类型系统"确定性优先"的原则。
4. **为什么 NOCASE 只懂 26 个英文字母,不懂 Unicode?**——Unicode 大小写规则极复杂(土耳其语 i/İ、德语 ß=ss…),内置一个"几乎肯定有人会用错"的 Unicode NOCASE 不如只做 ASCII,把复杂情况交给应用层(用户可自己注册 collation)。这是"内核只做最小正确的事,复杂留给应用"的嵌入式哲学。
5. **为什么 STRICT 用一个 opcode(`OP_TypeCheck`)实现,而不是侵入每个写路径?**——因为所有写路径都汇合于 `OP_MakeRecord`(任何写最后都要拼 Record),在那里前插一道关卡就覆盖全部。这是"找汇合点加扩展点"的架构智慧,让 SQLite 能温和演进(加强类型这种大特性,只动一个 opcode,不重构),代码集中、覆盖完整、易维护。

### 想继续深入往哪钻

- **想看官方权威**:读 SQLite 官方文档 "Datatypes In SQLite"(亲和性规则权威说明,含 3.54 的 FLEXNUM)、"Collating Sequences"(collation 注册和使用)、"STRICT Tables"(3.37 STRICT 表的官方说明)。这些是本章源码背后的语义权威。
- **想看源码**:本章引用的核心文件——`src/sqliteInt.h`(affinity 常量定义 `SQLITE_AFF_*`、CollSeq 结构)、`src/build.c`(`sqlite3AffinityType` 推导 affinity、STRICT 表列类型限制)、`src/vdbe.c`(`applyAffinity` 温柔转换、`OP_Cast` 强制转换、`OP_Eq`/`OP_Compare` 比较、`OP_TypeCheck` STRICT 检查、`OP_Affinity` 批量应用)、`src/vdbemem.c`(`sqlite3VdbeMemCast` 强制转换实现)、`src/expr.c`(`comparisonAffinity`/`sqlite3CompareAffinity` 比较 affinity 推导)、`src/func.c`(`typeofFunc` 类型探针)、`src/main.c`(三个内置 collation `binCollFunc`/`nocaseCollatingFunc`/`rtrimCollFunc`)、`src/global.c`(`sqlite3StdType[]` 6 个标准类型名)、`src/insert.c`(`sqlite3TableAffinity` 把 OP_TypeCheck 插在 MakeRecord 前)。按"类型定义 → affinity 推导 → 转换/比较/collation → STRICT"的顺序读,闭环。
- **想动手感受**:`sqlite3` CLI 起个库,建表插各种类型的值,用 `SELECT typeof(x), x` 看每行真实类型;用 `EXPLAIN` 看 `WHERE x = 1` 编译出的 `OP_Eq` 的 P5(affinity);建一个 STRICT 表和不带 STRICT 的同名表,对比插 `'hello'` 进 INTEGER 列的行为差异。这些实验能把本章的所有规则在字节码层验证一遍。

### 引出下一章

我们搞清了 SQLite 类型系统的语义——值定类型 + 列 affinity + 比较/collation/STRICT。但有个问题一直没碰:**多个连接/线程同时操作同一个 `.db` 文件时,SQLite 怎么协调?** 前面所有章节,我们隐含假设"只有一个连接在操作数据库"。可现实中,一个 App 可能有多个线程、甚至多个进程同时打开同一个 `.db` 文件——这时候怎么防止"两个写同时改同一页、互相覆盖"?SQLite 没有像 MySQL/InnoDB 那样的行锁(它是嵌入式、单文件),它用的是**文件级锁(database lock)**——一个 5 态的锁状态机(unlocked/shared/reserved/pending/exclusive)。下一章 P5-17,我们讲 SQLite 的**并发模型**:文件锁怎么工作、为什么 SQLite 的并发写比 MySQL 弱(单写者)、WAL 模式怎么部分缓解这个限制。类型系统是"值的语义",并发锁是"多个操作者的协调"——从语义层转向并发层。

> **下一章**:[P5-17 · 并发模型:database lock](P5-17-并发模型-database-lock.md)

---

> **本章承接**:
> - **承《MySQL·InnoDB》强类型对照**:本章一句话点出 MySQL 列强类型 vs SQLite 动态类型的根本差异(列管值 vs 值管自己),指路《MySQL·InnoDB》P3 讲 InnoDB 行格式处不重复。STRICT 表是 SQLite 给"MySQL 式强类型需求"开的退路,但和 MySQL 的全局强类型仍有差别(逐表可选 vs 全局默认)。
> - **承《Lua》**:本章 `OP_Compare` + `OP_Jump` 的"比较出符号再跳转"模式,和《Lua》虚拟机 `OP_LT`/`OP_LE` 同构,承接《Lua》VM 基础不重复。`applyAffinity`/`sqlite3VdbeMemCast` 这种"类型转换做成显式函数"的风格,和 Lua VM 的 `luaT_trybinTM` 类型元方法处理是不同语言的同款思路。
> - **承《LevelDB》**:SQLite 动态类型 + 内置 NOCASE 只做 ASCII(复杂留给应用)的哲学,和《LevelDB》"嵌入式 = 内核极简 + 应用补全"一脉相承。STRICT 表"可选强类型"也呼应 LevelDB"把决策权交给用户"的设计取向。
> - **承《PG/MySQL》C/S 对照**:本章一句话点出 SQLite 类型系统比 C/S 数据库灵活(动态 + 可选 STRICT),代价是少了强类型契约和优化器类型信息——这是嵌入式单应用场景的取舍。
> - **承接 P3-09**:P3-09 讲 Record 字节格式 + serial type(存储侧),本章讲 affinity/比较/collation/STRICT(语义侧),两章分工不重复,合起来是 SQLite 类型系统完整画像。本章开头已明确声明分工。
