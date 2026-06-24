# 第 3 章 · SQL 解析器:从字符串到解析树

> **前置**:你需要先读过[第 2 章《连接与会话》](P1-02-连接与会话-客户端怎么进门.md)——它讲了客户端怎么"进门"、backend 进程怎么就位。这一章是旅程的**第二站**:门进了、backend 接管了,客户端发来的那句 SQL 已经躺在 backend 手里——但它还只是一串字符串,backend 看不懂。本章就回答:这串字符串怎么变成数据库能处理的东西。

> **核心问题**:你敲下的 `SELECT id, name FROM users WHERE id = 1`,到了 backend 这里就是一个字符数组 `'S','E','L','E','C','T',' ','i','d',...`。数据库怎么"读懂"它?为什么必须先把它翻译成一棵**解析树(parse tree)**,而不是边读字符串边执行?词法分析、语法分析、语义分析这**三步**各自在干什么、为什么不能合并成一步?
>
> **读完本章你会明白**:一句 SQL 从字符串到可被优化的结构,要经历**三个阶段**,每阶段产出不同——词法切出 token、语法拼成 raw parse tree、语义查表把 raw tree 变成 `Query` 树;为什么是"树"而不是"边读边执行";为什么语义分析必须**访问系统目录**(查表存不存在、列存不存在)而前两步不能;以及 `SELECT id, name FROM users WHERE id = 1` 这一整条链路在 PG 源码里是怎么走的。

> **如果一读觉得太难**:先记三件事——① SQL 先被切成 token,再按语法拼成一棵树,这棵树**只懂语法、不懂语义**(不知道表存不存在);② 树比"边读边执行"强,因为树能被检查、重写、优化;③ 第三步语义分析才查系统目录,把"语法正确但语义可能错"的 raw tree 升级成带类型、带表 OID 的 `Query` 树。三阶段的分界线,你只要记住"前两步纯看字符串、第三步才开始查表"就够了。

---

## 一、字符串不是结构:backend 拿到的是一坨字节

第 2 章末尾,backend 进程在 `PostgresMain` 主循环里阻塞在 `ReadCommand`,等客户端发 SQL。一旦客户端发来一条(简单查询协议下就是一个 `'Q'` 消息),backend 读到的本质上就是**一个以 null 结尾的 C 字符串**:

```
"SELECT id, name FROM users WHERE id = 1"
```

对 backend 而言,这只是 38 个字节躺在内存里。它知道 `S` 后面是 `E`、`E` 后面是 `L`,但它**不知道**:

- `SELECT` 是个关键字、`id` 是个列名、`users` 是个表名、`1` 是个整数常量;
- 这串字符的"结构"是什么——哪部分是要查的列、哪部分是数据来源、哪部分是过滤条件;
- `users` 这个表到底存不存在、`id` 这列存不存在、`id = 1` 里的 `1` 能不能和 `id` 的类型比较。

如果让执行器直接对着这串字节干活,它无从下手——执行器需要的是"扫描哪张表、取哪些列、按什么条件过滤"这样**结构化的指令**,不是一团文本。

> **所以,字符串和执行器之间存在一道鸿沟:字符串是人写的、是线性的;执行器要的是结构化的、可逐节点处理的指令。** 跨过这道鸿沟,就是解析器的全部职责。

### 不"翻译"会怎样:为什么不能边读字符串边执行

一个自然的疑问:何必搞这么复杂?为什么不能让 backend 一边读字符、一边就地执行?读到一个 `SELECT` 就开始准备查表,读到 `FROM users` 就去开 users 表……

> **不这样会怎样**:设想一个"边读边执行"的解析器。它读到 `SELECT id` 就去 users 表拿 id 列——可是它**还没读到 `FROM users`**(FROM 在 SELECT 后面)!它根本不知道该去哪张表拿。即便调整顺序先找 FROM,真正的灾难在后头:

1. **没法做任何检查**。`SELECT id FROM users WHERE id = 1`,如果 users 表里根本没有 id 列?如果是 `SELECT id FORM users`(FORM 拼错了)?如果是 `SELECT * FROM users JOIN orders ON users.id = orders.uid`,两个表的 id 列重名该取哪个?这些都需要在执行**之前**就判定,否则要么跑到一半崩溃,要么悄悄返回错的数据。而"边读边执行"意味着读到哪执行到哪,出错时可能已经改了一半数据。
2. **没法优化**。同一句 `SELECT id FROM users WHERE id = 1`,可以全表扫描,也可以走索引——选哪条路更快,取决于表有多大、有没有索引、数据分布。这个决策需要在**完整理解整句 SQL 之后**才能做。边读边执行,读到一半就开工,根本没有"通盘考虑"的机会。
3. **没法重写**。数据库有"查询重写"机制:视图要被展开成底层表、规则要被应用。这些重写操作的对象,是一个**完整的、结构化的查询表示**,不是一行行字符。

所以,SQL 必须先被**完整地、整体地**翻译成一个结构化的中间表示——一棵**树**——然后才能进入后续的检查、优化、执行。这棵树,就是**解析树(parse tree)**。

### 为什么是"树",不是别的结构

SQL 天然是**嵌套**的。`SELECT id FROM (SELECT * FROM users) sub WHERE id = 1`——外层 SELECT 的 FROM 是一个子查询,子查询里又是一个 SELECT。`WHERE id = 1 AND name = 'a'`——AND 连起两个条件,每个条件又是一棵表达式子树。线性结构(数组、链表)表达不了这种嵌套,而**树**天然擅长:每个节点是"做什么",它的子节点是"对谁做"。

> 一句话:**树是"声明式语句"最自然的载体**——一个 `SELECT` 节点挂着它的目标列、数据来源(FROM)、过滤条件(WHERE),过滤条件里又嵌套着表达式树。后续的优化器、执行器,都是**遍历这棵树**来工作的。把字符串翻译成树,就是把"线性的人话"翻译成"机器能逐节点处理的指令"。

---

## 二、三步走:词法 → 语法 → 语义

把"字符串→树"拆开,会发现它其实是**三个阶段**,每阶段职责不同、输出不同。这三个阶段不是随手分的,而是被两个硬约束逼出来的:

1. **纯字符处理和需要查表的逻辑必须分开**。前两个阶段(词法、语法)只盯着字符串本身——切词、组装结构,不需要访问数据库的任何表。第三阶段(语义)才需要查系统目录(表存不存在、列存不存在、类型匹不匹配)。
2. **为什么必须分开?** 因为查表这件事**依赖事务状态、依赖权限、很慢**。把"纯字符的解析"和"需要查表的语义分析"分开,意味着:即便事务已经 abort、即便你没有权限,数据库仍然能**把 SQL 解析成 raw parse tree**——这非常有用,比如在一个已经失败的事务里,你发 `ROLLBACK` 或 `COMMIT`,数据库得先解析出这是什么命令,才能处理它。如果解析和语义混在一起,abort 状态下连命令都读不懂。

PG 源码里把这三步分得很干净。我们顺着调用链看:

> backend 主循环读到 SQL 后,简单查询协议下进入 [`exec_simple_query`](../postgresql-17.0/src/backend/tcop/postgres.c#L1017)。它做的第一件实质性的事,就是**只做纯字符解析**:[`pg_parse_query`](../postgresql-17.0/src/backend/tcop/postgres.c#L615)。

[src/backend/tcop/postgres.c:624](../postgresql-17.0/src/backend/tcop/postgres.c#L624)

```c
	raw_parsetree_list = raw_parser(query_string, RAW_PARSE_DEFAULT);
```

注意它顶上的注释(L605-613)有一句关键的话:**"it is important to keep this routine separate from the analysis & rewrite stages. Analysis and rewriting cannot be done in an aborted transaction, since they require access to database tables."**(把这个例程和分析、重写阶段分开很重要——分析和重写需要访问表,不能在已 abort 的事务里做)。这正是上面说的"为什么分开"的源码佐证。

`raw_parser` 返回一个**原始解析树列表**(`raw_parsetree_list`,因为一条字符串里可能有多条用分号隔开的 SQL)。之后 `exec_simple_query` 才对每棵 raw tree 做 [`pg_analyze_and_rewrite`](../postgresql-17.0/src/backend/tcop/postgres.c#L675)(语义分析 + 重写)。

下面三节,我们把每一步拆开讲透。

---

## 三、第一步:词法分析——把字符串切成 token

### 这一步在干什么

字符串是一团连续的字节。词法分析器(lexer/scanner)的任务,是把它切成一个个有意义的**词法单元(token)**:关键字、标识符、数字常量、字符串常量、操作符、标点。

以 `SELECT id, name FROM users WHERE id = 1` 为例(以下为**示意**,非真实输出;真实的 token 内部还带位置信息):

```text
SELECT  id  ,  name  FROM  users  WHERE  id  =  1
关键字  标识符 逗号 标识符 关键字 标识符 关键字 标识符 等号 整数常量
```

切完之后,语法分析器拿到的就不再是"字符流",而是"token 流"——每个 token 自带类型(它是关键字还是标识符?是整数还是字符串?)和值。

### 为什么词法要和语法分开

> **不这样会怎样**:如果不切 token,语法分析器就得一边识别"SELECT 是一个词"、一边判断"它后面该跟什么"。这两件事耦合在一起,语法规则会极其难写、极易错。词法规则(什么是标识符、什么是数字)和语法规则(一个 SELECT 语句长什么样)是**两种完全不同性质**的东西:词法用**正则**就能描述(`identifier = [A-Za-z_][A-Za-z0-9_]*`),语法需要**上下文无关文法**。把它们交给两个专门工具各司其职,远比揉在一起清晰。

PG 正是这么干的:词法用 **flex**(一个正则扫描器生成器),语法用 **bison**(一个 LALR(1) 语法分析器生成器)。词法规则在 [`src/backend/parser/scan.l`](../postgresql-17.0/src/backend/parser/scan.l)(`.l` 是 flex 的输入),语法规则在 [`src/backend/parser/gram.y`](../postgresql-17.0/src/backend/parser/gram.y)(`.y` 是 bison 的输入)。

### 词法规则长什么样:标识符的切分

看 scan.l 里切"标识符"这条核心规则。先定义什么是标识符的字符集:

[src/backend/parser/scan.l:346-349](../postgresql-17.0/src/backend/parser/scan.l#L346-L349)

```c
ident_start		[A-Za-z\200-\377_]
ident_cont		[A-Za-z\200-\377_0-9\$]

identifier		{ident_start}{ident_cont}*
```

`ident_start` 是"标识符能以什么开头":字母、下划线、以及 `\200-\377`(非 ASCII 字符,支持 Unicode 标识符)。`ident_cont` 是"后面能跟什么":再加数字和 `$`。`identifier` 就是"开头字符 + 任意多个续字符"。一条正则,就把"什么是标识符"说清了。

然后是用这条规则的动作(切到一个标识符后干什么):

[src/backend/parser/scan.l:1080-1103](../postgresql-17.0/src/backend/parser/scan.l#L1080-L1103)

```c
{identifier}	{
					int			kwnum;
					char	   *ident;

					SET_YYLLOC();

					/* Is it a keyword? */
					kwnum = ScanKeywordLookup(yytext,
											  yyextra->keywordlist);
					if (kwnum >= 0)
					{
						yylval->keyword = GetScanKeyword(kwnum,
														 yyextra->keywordlist);
						return yyextra->keyword_tokens[kwnum];
					}

					/*
					 * No.  Convert the identifier to lower case, and truncate
					 * if necessary.
					 */
					ident = downcase_truncate_identifier(yytext, yyeng, true);
					yylval->str = ident;
					return IDENT;
				}
```

这段逻辑非常关键,它揭示了 SQL 词法的一个微妙之处:**关键字和标识符是用同一条正则切出来的**。`SELECT`、`FROM`、`id`、`users` 都匹配 `{identifier}`。切出来之后,词法器回头问一句:这个词**在不在关键字表里**(`ScanKeywordLookup`)?在——比如 `SELECT`——就返回对应的关键字 token(如 `SELECT`);不在——比如 `id`、`users`——就当成普通标识符 `IDENT` 返回,并顺手**转成小写**(SQL 里未加引号的标识符大小写不敏感,统一转小写处理)。

> 这就是为什么 `select` 和 `SELECT` 等价(都是关键字),而 `Id` 和 `id` 也等价(都转成小写 `id`)。但 `"Id"`(双引号包裹)不同——双引号标识符在另一个规则 [`{xdstop}`](../postgresql-17.0/src/backend/parser/scan.l#L813-L824) 里处理,**不做大小写转换**,所以 `"Id"` 和 `Id` 是两个不同的列名。这个区分,就是在词法层完成的。

数字常量(`ICONST`/`FCONST`)、字符串常量(`SCONST`)、操作符(`=`, `,`, 等)各有各的规则,原理相同:正则匹配 + 返回对应 token 类型。这里不逐条展开,只要记住:**词法器的全部产出,就是一个 token 流**——每个 token 带着(类型, 值, 位置)。

---

## 四、第二步:语法分析——把 token 拼成一棵 raw parse tree

### 这一步在干什么

语法分析器(parser)拿着 token 流,按**语法规则**(文法)把它们组装成一棵**原始解析树(raw parse tree)**。这棵树此时只反映"SQL 的语法结构",还**不包含任何语义信息**——它不知道 users 表存不存在,只知道"这里有个叫 users 的标识符"。

文法的核心是**产生式(production)**,形如"一个 SELECT 语句由这些部分组成"。看 bison 里 `simple_select` 的产生式:

[src/backend/parser/gram.y:12790-12806](../postgresql-17.0/src/backend/parser/gram.y#L12790-L12806)

```c
simple_select:
			SELECT opt_all_clause opt_target_list
			into_clause from_clause where_clause
			group_clause having_clause window_clause
				{
					SelectStmt *n = makeNode(SelectStmt);

					n->targetList = $3;
					n->intoClause = $4;
					n->fromClause = $5;
					n->whereClause = $6;
					n->groupClause = ($7)->list;
					n->groupDistinct = ($7)->distinct;
					n->havingClause = $8;
					n->windowClause = $9;
					$$ = (Node *) n;
				}
```

读法:左边的 `simple_select` 可以被右边那一串替换——`SELECT` 关键字、可选的 ALL/DISTINCT、目标列(`opt_target_list`)、INTO 子句、FROM 子句、WHERE 子句、GROUP BY、HAVING、WINDOW。当 bison 在 token 流里匹配上这一整串,就执行大括号里的 **C 动作**:`makeNode(SelectStmt)` 建一个 `SelectStmt` 节点,把 `$3`、`$5`、`$6` 这些(分别对应第 3、5、6 个成分的语义值)填进它的字段,然后 `$$`(这个产生式的返回值)指向这个新节点。

> `$3`、`$5` 这种是 bison 的语法:`$n` 代表右边第 n 个成分经各自动作处理后返回的值。比如 `opt_target_list` 那个位置返回的是一个 `ResTarget` 链表(目标列),它就被赋给 `n->targetList`。子成分又是各自的产生式递归处理出来的——`from_clause` 产生式处理 FROM,`where_clause` 产生式处理 WHERE。整棵树就是这样**自底向上**拼起来的。

### 目标列怎么变成节点:ResTarget

`SELECT id, name` 里的 `id, name` 是目标列。它由 `target_list` 和 `target_el` 产生式处理:

[src/backend/parser/gram.y:17088-17111](../postgresql-17.0/src/backend/parser/gram.y#L17088-L17111)

```c
target_el:	a_expr AS ColLabel
				{
					$$ = makeNode(ResTarget);
					$$->name = $3;
					$$->indirection = NIL;
					$$->val = (Node *) $1;
					$$->location = @1;
				}
			| a_expr BareColLabel
				{
					$$ = makeNode(ResTarget);
					$$->name = $2;
					$$->indirection = NIL;
					$$->val = (Node *) $1;
					$$->location = @1;
				}
			| a_expr
				{
					$$ = makeNode(ResTarget);
					$$->name = NULL;
					$$->indirection = NIL;
					$$->val = (Node *) $1;
					$$->location = @1;
				}
```

三个分支对应三种写法:`a_expr AS 别名`、`a_expr 别名`(不加 AS)、`a_expr`(不加别名)。每种的产出都是一个 `ResTarget` 节点——`val` 是表达式本身(比如 `ColumnRef` 列引用),`name` 是输出列名(没起别名就 NULL)。`SELECT id, name` 走第三个分支,产出两个 `ResTarget`,`val` 分别指向表示 `id` 和 `name` 的 `ColumnRef` 节点。

### WHERE 怎么挂:where_clause

[src/backend/parser/gram.y:13909-13912](../postgresql-17.0/src/backend/parser/gram.y#L13909-L13912)

```c
where_clause:
			WHERE a_expr							{ $$ = $2; }
			| /*EMPTY*/								{ $$ = NULL; }
		;
```

`WHERE` 后面跟一个表达式 `a_expr`(这个表达式本身又是一大堆产生式递归定义出来的,处理 `=`、`AND`、`OR`、函数调用等所有 SQL 表达式)。没有 WHERE 就返回 NULL。这个表达式节点会被塞进 `SelectStmt` 的 `whereClause` 字段。

### 表名怎么变成节点:RangeVar

`FROM users` 里的 `users` 走 `qualified_name` 产生式:

[src/backend/parser/gram.y:17146-17155](../postgresql-17.0/src/backend/parser/gram.y#L17146-L17155)

```c
qualified_name:
			ColId
				{
					$$ = makeRangeVar(NULL, $1, @1);
				}
			| ColId indirection
				{
					$$ = makeRangeVarFromQualifiedName($1, $2, @1, yyscanner);
				}
		;
```

`FROM users`(单层名字)走第一个分支,`makeRangeVar(NULL, "users", ...)` 造一个 `RangeVar`(范围变量)节点——schema 名为 NULL,表名为 `users`。如果是 `FROM public.users`(带 schema),走第二个分支。注意:**此时 `RangeVar` 只记着"表名是 users 这个字符串",并不知道这个表的真实 OID、它有几列**。那些要等语义分析去查。

### 顶层怎么包:RawStmt

最外层,`stmtmulti` 产生式把每条语句包成一个 `RawStmt`:

[src/backend/parser/gram.y:982-988](../postgresql-17.0/src/backend/parser/gram.y#L982-L988)

```c
			| toplevel_stmt
				{
					if ($1 != NULL)
						$$ = list_make1(makeRawStmt($1, 0));
					else
						$$ = NIL;
				}
```

`RawStmt` 是个薄壳,里面 `stmt` 指针指向真正的语句节点(如 `SelectStmt`),外加这条语句在原字符串里的位置(`stmt_location`、`stmt_len`)——这个位置信息后面报错时用来精确指出"错在第几行第几列"。

### raw parse tree 的形状示意

把 `SELECT id, name FROM users WHERE id = 1` 走完整条语法分析后的 raw tree(以下为**示意**,真实节点见源码):

```text
RawStmt
 └─ stmt: SelectStmt
     ├─ targetList: [ResTarget(val=ColumnRef("id")), ResTarget(val=ColumnRef("name"))]
     ├─ fromClause: [RangeVar(catalog=NULL, schema=NULL, relname="users")]
     └─ whereClause: A_Expr(op='=', lexpr=ColumnRef("id"), rexpr=A_Const(ival=1))
```

注意此刻这棵树的特征:**全是字符串和结构,没有任何"真相"**。`ColumnRef("id")` 只是说"引用了一个叫 id 的东西",但 id 是 users 表的列,还是某个别名,还是个函数?raw tree 不知道。`RangeVar("users")` 只是说"引用了一个叫 users 的表",但这个表存不存在?raw tree 也不知道。**这些"对不对"的问题,留给第三步。**

---

## 五、第三步:语义分析——查表,把 raw tree 升级成 Query 树

### 这一步在干什么

raw parse tree 的问题:它语法正确,但**语义可能错**。`SELECT nonexistent_col FROM nonexistent_table`——语法完全合法(符合 `simple_select` 产生式),但它引用的列和表都不存在。这一步要做的,就是把"名字"落实成"真实对象":查系统目录,确认每个表名对应一张真实存在的表(拿到 OID)、每个列名对应真实存在的列(拿到类型),把表达式里的类型推导清楚,把 `*` 展开成具体列。

完成这一步后,raw tree 里的 `SelectStmt`(和它那些只有字符串名字的子节点)被**变换(transform)**成 `Query` 树——里面的节点类型也变了:`ColumnRef` 变成了带表 OID 和列号的 `Var`,`A_Const` 仍然是常量但带上了类型,`RangeVar` 变成了 `RangeTblEntry`(范围表项,记录这张表的 OID、列定义、权限等)。

### 为什么这一步必须查表,且不能和前两步合并

> **不这样会怎样**:如果不查表就优化、执行,会发生什么?优化器要估算"扫 users 表多少行",可是它连 users 表存不存在、多大都不知道。执行器要去磁盘取 users.id,可是它不知道 id 列在表的第几个位置、什么类型。更糟的是 SQL 里到处是**歧义**:`SELECT id FROM users JOIN orders ON ...`,如果 users 和 orders 都有 id 列,`SELECT id` 里的 id 是哪个?这个歧义只有在**知道两张表各有哪些列**之后才能消解——而"知道各有哪些列"必须查系统目录。

所以语义分析这一步的本质是:**用系统目录里的真相,给 raw tree 的每个名字配上真实的身份**。

入口是 `parse_analyze`(PG 17 提供了三个变体,简单查询走 [`parse_analyze_fixedparams`](../postgresql-17.0/src/backend/parser/analyze.c#L103))。它的核心调用是 `transformTopLevelStmt` → `transformStmt`:

[src/backend/parser/analyze.c:248-259](../postgresql-17.0/src/backend/parser/analyze.c#L248-L259)

```c
transformTopLevelStmt(ParseState *pstate, RawStmt *parseTree)
{
	Query	   *result;

	/* We're at top level, so allow SELECT INTO */
	result = transformOptionalSelectInto(pstate, parseTree->stmt);

	result->stmt_location = parseTree->stmt_location;
	result->stmt_len = parseTree->stmt_len;

	return result;
}
```

`transformStmt` 按语句类型分发:

[src/backend/parser/analyze.c:340-372](../postgresql-17.0/src/backend/parser/analyze.c#L340-L372)

```c
	switch (nodeTag(parseTree))
	{
			/*
			 * Optimizable statements
			 */
		case T_InsertStmt:
			result = transformInsertStmt(pstate, (InsertStmt *) parseTree);
			break;
		...
		case T_SelectStmt:
			{
				SelectStmt *n = (SelectStmt *) parseTree;

				if (n->valuesLists)
					result = transformValuesClause(pstate, n);
				else if (n->op == SETOP_NONE)
					result = transformSelectStmt(pstate, n);
				else
					result = transformSetOperationStmt(pstate, n);
			}
			break;
```

注意 `T_InsertStmt`/`T_DeleteStmt`/`T_UpdateStmt`/`T_SelectStmt` 这几条被注释称为 **"Optimizable statements"**(可优化的语句)——它们会被 transform 成一棵能交给优化器的 `Query` 树;而 `CREATE TABLE` 这类 utility 语句不在此列,它们只是被包进一个 `CMD_UTILITY` 的 Query,后续走另一条执行路径。这正是查询引擎(可优化的 DML)和命令处理(DDL)的分界。

### transformSelectStmt:把 SELECT 树的每一部分都"落实"

`transformSelectStmt` 是 SELECT 语义分析的主函数。它的骨架是一连串"依次 transform 各子句":

[src/backend/parser/analyze.c:1337-1383](../postgresql-17.0/src/backend/parser/analyze.c#L1337-L1383)

```c
transformSelectStmt(ParseState *pstate, SelectStmt *stmt)
{
	Query	   *qry = makeNode(Query);
	Node	   *qual;
	ListCell   *l;

	qry->commandType = CMD_SELECT;

	/* process the WITH clause independently of all else */
	if (stmt->withClause)
	{
		qry->hasRecursive = stmt->withClause->recursive;
		qry->cteList = transformWithClause(pstate, stmt->withClause);
		qry->hasModifyingCTE = pstate->p_hasModifyingCTE;
	}
	...
	/* process the FROM clause */
	transformFromClause(pstate, stmt->fromClause);

	/* transform targetlist */
	qry->targetList = transformTargetList(pstate, stmt->targetList,
										  EXPR_KIND_SELECT_TARGET);

	/* mark column origins */
	markTargetListOrigins(pstate, qry->targetList);

	/* transform WHERE */
	qual = transformWhereClause(pstate, stmt->whereClause,
								EXPR_KIND_WHERE, "WHERE");

	/* initial processing of HAVING clause is much like WHERE clause */
	qry->havingQual = transformWhereClause(pstate, stmt->havingClause,
										   EXPR_KIND_HAVING, "HAVING");
	...
```

读这段代码,你会发现 transform 的顺序很有讲究,它对应一个关键的设计决策:

1. **先 FROM**:`transformFromClause` 把 `RangeVar`("users" 这个名字)变成 `RangeTblEntry`(查系统目录,拿到 users 表的 OID、列定义、权限),并把它加进 `ParseState` 的范围表。**只有先把表落实了,后面才能知道列长什么样。**
2. **再目标列**:有了 FROM 的范围表,`transformTargetList` 才能把 `ColumnRef("id")` 解析成 `Var`(引用 users 表第几列、什么类型)。如果先于 FROM,根本无从知道 id 属于哪张表。
3. **再 WHERE**:同样依赖范围表,把 `id = 1` 里的 `ColumnRef("id")` 解析成 `Var`,并推导整个表达式的类型(还要检查 `=` 能不能用在 int 和 int 之间——类型兼容性检查)。

> **为什么是这个顺序?** 因为语义依赖关系是单向的:列引用依赖表引用、表达式类型依赖列类型。先解析被依赖的部分,是唯一可行的顺序。这看似是常识,但它是"为什么不能边读字符串边执行"的另一个角度:**语义分析需要全局视野(先知道有哪些表,才能解析列),而线性地读字符串做不到这一点。**

### raw tree 和 Query 树的对照

经过 transform 后,前文的 raw tree 升级成 `Query` 树(示意):

```text
Query(commandType=CMD_SELECT)
 ├─ rtable: [RangeTblEntry(relid=users的OID, eref列出users的所有列和类型, ...)]
 ├─ targetList: [TargetEntry(expr=Var(users, attno=id列号), ...),
 │                TargetEntry(expr=Var(users, attno=name列号), ...)]
 └─ jointree: FromExpr(fromlist=[RangeTblRef(rtindex=1)])
     └─ quals: OpExpr(op='=', args=[Var(users,id), Const(int4,1)])
```

关键变化:

- `RangeVar("users")` → `RangeTblEntry`(带真实 `relid`,即 `pg_class` 里那张表的 OID);
- `ColumnRef("id")` → `Var`(带 `varno`=范围表索引、`varattno`=列在表里的物理列号、`vartype`=列的 OID);
- `A_Const(1)` → `Const`(类型已确定为 int4);
- `A_Expr('=',...)` → `OpExpr`(操作符 `=` 的 OID 已从系统目录查到,且做过类型兼容检查)。

**这棵 `Query` 树,就是优化器的输入。** 从这一刻起,"users 表"不再是个字符串名字,而是一个有 OID、有列定义、有统计信息的真实对象;"`id = 1`"不再是一串字符,而是一个类型正确、操作符已确定的表达式。优化器可以基于这棵树开始估算代价、选执行计划了。

---

## 六、三个阶段为什么不能合并:一张总账

把三步并排,看它们各自的输入、输出、是否查表:

| 阶段 | 输入 | 输出 | 查系统目录? | 用的工具 |
|------|------|------|------------|---------|
| 词法 (scan.l) | 字符串 | token 流 | 否 | flex(正则) |
| 语法 (gram.y) | token 流 | raw parse tree(`SelectStmt` 等) | 否 | bison(LALR 文法) |
| 语义 (analyze.c) | raw parse tree | `Query` 树 | **是** | 手写 C(transform 函数族) |

**为什么不能合并前两步?** 词法用正则、语法用上下文无关文法,是两种表达力不同的形式系统,各自由专门工具(flex/bison)生成最高效的代码;硬揉在一起会让规则难写、难维护。而且分开后,bison 可以专心处理"结构",把"切词"的脏活(转小写、处理 Unicode、跳注释、识别多字节)全丢给 flex。

**为什么语义分析必须独立出来?** 因为只有它**需要查表**。查表意味着:依赖事务(要能看到一致的系统目录快照)、依赖权限(你得有读 users 表的权限)、慢(要访问 `pg_class`/`pg_attribute` 等系统表)。把这件重活和纯字符解析分开,使得**即便在事务 abort 状态下,纯解析仍然能工作**——这正是 `pg_parse_query` 注释里那句"analysis and rewriting cannot be done in an aborted transaction"的根因。这个分离不是洁癖,是**正确性的需要**。

---

## 关键源码精读:从字符串到 Query 树的完整调用链

我们把这条链路上几个最核心的结构和函数,连起来看一遍。

### 1. 入口:`raw_parser` 把词法和语法串起来

[src/backend/parser/parser.c:41-86](../postgresql-17.0/src/backend/parser/parser.c#L41-L86)

```c
List *
raw_parser(const char *str, RawParseMode mode)
{
	core_yyscan_t yyscanner;
	base_yy_extra_type yyextra;
	int			yyresult;

	/* initialize the flex scanner */
	yyscanner = scanner_init(str, &yyextra.core_yy_extra,
							 &ScanKeywords, ScanKeywordTokens);
	...
	/* initialize the bison parser */
	parser_init(&yyextra);

	/* Parse! */
	yyresult = base_yyparse(yyscanner);

	/* Clean up (release memory) */
	scanner_finish(yyscanner);

	if (yyresult)				/* error */
		return NIL;

	return yyextra.parsetree;
}
```

三个关键点:

- `scanner_init` 初始化 flex 词法器,把 SQL 字符串交给它;
- `base_yyparse` 是 bison 生成的语法分析主函数——它会**反复调用词法器**要下一个 token(`base_yylex`),一边拿 token 一边按文法规则归约、执行动作、拼节点;
- 最终的 raw parse tree 列表存在 `yyextra.parsetree` 里返回。

> 这个函数顶上的注释(L34-40)一句话点明了它的职责:"Given a query in string form, do lexical and grammatical analysis. Returns a list of raw (un-analyzed) parse trees."——**词法 + 语法,产出 raw、未分析的树**。语义分析不在这里,是 `parse_analyze` 的事。职责边界清清楚楚。

### 2. raw parse tree 的根:`SelectStmt`

语法分析产出的语句节点,SELECT 对应 `SelectStmt`。它的字段几乎一一对应 SQL 的各个子句:

[src/include/nodes/parsenodes.h:2116-2163](../postgresql-17.0/src/include/nodes/parsenodes.h#L2116-L2163)

```c
typedef struct SelectStmt
{
	NodeTag		type;

	/*
	 * These fields are used only in "leaf" SelectStmts.
	 */
	List	   *distinctClause;
	IntoClause *intoClause;
	List	   *targetList;		/* the target list (of ResTarget) */
	List	   *fromClause;
	Node	   *whereClause;
	List	   *groupClause;
	bool		groupDistinct;
	Node	   *havingClause;
	List	   *windowClause;
	...
	/*
	 * These fields are used only in upper-level SelectStmts.
	 */
	SetOperation op;			/* type of set op */
	bool		all;			/* ALL specified? */
	struct SelectStmt *larg;	/* left child */
	struct SelectStmt *rarg;	/* right child */
} SelectStmt;
```

注意注释把字段分成三组:**叶子 SELECT 用的**(targetList/fromClause/whereClause...)、**VALUES 用的**(valuesLists)、**上层集合操作用的**(UNION/INTERSECT/EXCEPT 的 op/larg/rarg)。一个 `SELECT a UNION SELECT b`,顶层 `SelectStmt` 的 `op=SETOP_UNION`,`larg`/`rarg` 各指向一个叶子 SELECT;叶子 SELECT 的 `op=SETOP_NONE`。`transformStmt` 里正是靠 `n->op == SETOP_NONE` 区分"普通 SELECT"还是"集合操作"(见前文 L367-370)。

> 这个设计揭示了一件事:**同一棵 raw tree,既是叶子又是上层节点的载体**。`SelectStmt` 既是 `SELECT id FROM users` 这种简单查询的节点,也是 `A UNION B` 这种集合操作的节点——通过 `op` 字段和 `larg/rarg` 子树来表达。这种"一个结构体身兼两职"是 raw tree 层的常见手法,目的是让语法规则更紧凑。

### 3. 目标列元素:`ResTarget` 和列引用 `ColumnRef`

[src/include/nodes/parsenodes.h:514-521](../postgresql-17.0/src/include/nodes/parsenodes.h#L514-L521)

```c
typedef struct ResTarget
{
	NodeTag		type;
	char	   *name;			/* column name or NULL */
	List	   *indirection;	/* subscripts, field names, and '*', or NIL */
	Node	   *val;			/* the value expression to compute or assign */
	ParseLoc	location;		/* token location, or -1 if unknown */
} ResTarget;
```

[src/include/nodes/parsenodes.h:291-296](../postgresql-17.0/src/include/nodes/parsenodes.h#L291-L296)

```c
typedef struct ColumnRef
{
	NodeTag		type;
	List	   *fields;			/* field names (String nodes) or A_Star */
	ParseLoc	location;		/* token location, or -1 if unknown */
} ColumnRef;
```

`ResTarget.val` 指向一个表达式节点——对 `SELECT id` 来说,这个表达式是 `ColumnRef`。`ColumnRef.fields` 是个 List:对 `id` 是 `["id"]`,对 `users.id` 是 `["users","id"]`,对 `users.*` 是 `["users", A_Star]`。注意 `fields` 全是**字符串节点**,没有任何类型信息——这就是 raw tree 的特征:**它精确记录了"写了什么",但不知道"这指的是什么"**。

### 4. 表引用:`RangeVar`

[src/include/nodes/primnodes.h:71-95](../postgresql-17.0/src/include/nodes/primnodes.h#L71-L95)

```c
typedef struct RangeVar
{
	NodeTag		type;

	/* the catalog (database) name, or NULL */
	char	   *catalogname;

	/* the schema name, or NULL */
	char	   *schemaname;

	/* the relation/sequence name */
	char	   *relname;

	/* expand rel by inheritance? recursively act on children? */
	bool		inh;

	/* see RELPERSISTENCE_* in pg_class.h */
	char		relpersistence;

	/* table alias & optional column aliases */
	Alias	   *alias;

	/* token location, or -1 if unknown */
	ParseLoc	location;
} RangeVar;
```

`RangeVar` 记录一个表引用的三段式名字(数据库.模式.表),外加继承是否展开(`inh`)、临时/持久(`relpersistence`)、表别名(`alias`)。`FROM users u` 里,`catalogname`/`schemaname` 都是 NULL,`relname="users"`,`alias` 指向别名 `u`。

> 注意 `RangeVar` 里**没有任何 OID、没有任何列信息**。它的 `relname` 只是个字符串。只有当语义分析的 `transformFromClause` 拿着这个字符串去 `pg_class` 里查,得到这张表的 OID,并据此构造出 `RangeTblEntry`,这个表引用才"落地"成真实对象。raw tree 的 `RangeVar` 和语义分析后的 `RangeTblEntry`,就是这个"落地"前后两个阶段的对照。

### 5. 整条链的鸟瞰

把 `SELECT id, name FROM users WHERE id = 1` 这一句,在源码里走一遍:

```text
exec_simple_query (postgres.c:1017)
 │
 │ ① 字符串 → token → raw tree(纯解析,不查表)
 ├─ pg_parse_query (postgres.c:615)
 │    └─ raw_parser (parser.c:42)
 │         ├─ scanner_init + flex(scan.l)  → token 流
 │         └─ base_yyparse + bison(gram.y) → [RawStmt → SelectStmt]
 │
 │ ② raw tree → Query 树(查系统目录,落实语义)
 ├─ pg_analyze_and_rewrite_fixedparams (postgres.c:675)
 │    └─ parse_analyze_fixedparams (analyze.c:103)
 │         └─ transformTopLevelStmt (analyze.c:248)
 │              └─ transformStmt (analyze.c:311)
 │                   └─ transformSelectStmt (analyze.c:1337)
 │                        ├─ transformFromClause   : RangeVar → RangeTblEntry(查 pg_class)
 │                        ├─ transformTargetList    : ColumnRef → Var(查 pg_attribute)
 │                        └─ transformWhereClause   : A_Expr → OpExpr(查操作符,类型检查)
 │
 └─ 得到 Query 树 → 交给优化器(下一章)
```

这条链上每一步都只做自己的事:词法只切词、语法只拼结构、语义只查表落实。每一步的输出都是下一步的输入,职责边界由"是否需要查系统目录"这条线划得清清楚楚。

---

## 章末小结

### 一句话回顾

一句 SQL 是字符串,backend 看不懂;解析器分**三步**把它变成机器能处理的树:

1. **词法分析**(scan.l + flex):字符串 → token 流。识别关键字、标识符、常量、操作符。
2. **语法分析**(gram.y + bison):token 流 → **raw parse tree**(`SelectStmt` 等节点)。这棵树只懂语法结构,不懂语义——不知道表存不存在、列存不存在。
3. **语义分析**(analyze.c):raw parse tree → **`Query` 树**。查系统目录,把每个名字落实成真实对象(表→OID、列→列号和类型、操作符→OID 并做类型检查)。

最终产出的 `Query` 树,是优化器的输入。

### 三个关键"为什么"

- **为什么是树不是边读边执行?** 因为执行前要做检查(表/列是否存在)、要做优化(选哪条执行计划)、要做重写(视图展开)。这些都需要"完整理解整句 SQL 之后"才能做,而"完整理解"的载体就是一棵结构化的树。
- **为什么分三步?** 因为词法(正则)、语法(上下文无关文法)、语义(需要查表)是三种不同性质的逻辑,各自由专门工具处理最高效;更关键的是,**纯字符解析必须和"需要查表"的语义分析分开**,否则在事务 abort 状态下连 `ROLLBACK` 都解析不了。
- **为什么语义分析在最后?** 因为语义依赖关系是单向的:列引用依赖表引用、表达式类型依赖列类型。必须先把 FROM(表)落实,才能解析目标列和 WHERE;这种全局依赖,线性读字符串做不到。

### 回扣主线

这一章主要服务的是数据的第三个本性——**"用户说要什么,机器只会按位置读"**。SQL 是"要什么"(`SELECT id FROM users WHERE id=1`),它和"怎么取"(扫哪张表的哪个页、哪个列)之间有巨大的鸿沟。解析器的工作,就是把这句"要什么"从一团字符串,变成一棵**结构化、可检查、可优化**的树——这是跨越那道鸿沟的**第一步**(后续由优化器完成"怎么取"的翻译)。

它偏向主线二分法的哪一侧?**偏"快"**——解析器本身不直接追求快(它的开销通常不大),但它产出的 `Query` 树是优化器(追求快)的地基;没有一棵干净、类型正确、对象落实的树,优化器无从估算代价、无从选计划。同时,语义分析查表落实对象,也守了一道**"不乱"**的关卡:语法对但语义错(表不存在、列不存在、类型不匹配)的 SQL,在这里被拦下,不会带着错误往下走。

### 想继续深入,该往哪钻

- **看词法全集**:[src/backend/parser/scan.l](../postgresql-17.0/src/backend/parser/scan.l)。重点是 `{identifier}` 规则(L1080)和字符串常量、数字常量的规则;还有 `base_yylex` 那个"filter 层"([parser.c:111](../postgresql-17.0/src/backend/parser/parser.c#L111))——它处理多 token 前瞻,把一些情况压回单 token 前瞻以保持文法 LALR(1)。
- **看语法全集**:[src/backend/parser/gram.y](../postgresql-17.0/src/backend/parser/gram.y)。从 `stmtmulti`(L970)和 `simple_select`(L12790)入手;`a_expr` 那一大块(L13000+ 起)是表达式文法,理解了它就理解了 SQL 表达式怎么解析。
- **看语义分析全集**:[src/backend/parser/analyze.c](../postgresql-17.0/src/backend/parser/analyze.c) 的 `transformSelectStmt`(L1337);以及它调用的 `transformFromClause`、`transformTargetList`(分别在 [parse_clause.c](../postgresql-17.0/src/backend/parser/parse_clause.c) 和 [parse_target.c](../postgresql-17.0/src/backend/parser/parse_target.c))、列解析的 [parse_relation.c](../postgresql-17.0/src/backend/parser/parse_relation.c)。
- **看节点定义**:[src/include/nodes/parsenodes.h](../postgresql-17.0/src/include/nodes/parsenodes.h) 的 `SelectStmt`(L2116)、`RawStmt`(L2017)、`Query`(L117);以及 [primnodes.h](../postgresql-17.0/src/include/nodes/primnodes.h) 的 `RangeVar`(L71)、`Var`、`Const`、`OpExpr`。

---

> 字符串已经变成了一棵类型正确、对象落实的 `Query` 树。但这棵树只说"要干什么"(查 id=1 的行),没说"怎么干"——是扫全表,还是走索引?哪条路更快?这正是**优化器**要回答的问题。而且同一个 `Query` 树,可能对应好几条执行计划,优化器得基于统计信息估算每条的代价,选出最省的那一条。翻开 **第 4 章 · 查询优化器:怎么决定"用哪条路最省"**。
