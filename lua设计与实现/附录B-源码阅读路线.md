# 附录B 源码阅读路线:lua-5.5.0 阅读导航

> 这是《Lua虚拟机设计与实现深入浅出》的**附录B**。全书以 lua-5.5.0 官方 C 实现为唯一主干,这个附录给读者一份读这份 32106 行 C 源码的导航:每个文件干什么、读的顺序、哪些结构体和函数是命脉、怎么动手验证。

源码版本:**lua-5.5.0**(2025 年 PUC-Rio 发布)。**不是老资料讲的 5.3 或 5.4**。5.5 相对 5.4 有几处实质演进,本附录凡涉及处会显式标注差异;凡老资料与 5.5 冲突,以源码为准。所有 `file:line` 引用都已在 5.5.0 核实。

---

## 一、整体地图:lua-5.5.0/src 全文件分类

lua-5.5.0/src 下 50 来个 `.c/.h` 文件,可以分成八组。下面这张表是全量,按"职责 + 行数 + 对应本书哪章"三栏组织。行数用 `wc -l` 实测值(含注释和空行)。

### 1.1 核心 VM 与状态

| 文件 | 行数 | 职责 | 对应章节 |
|---|---|---|---|
| `lobject.h` | 864 | 核心数据结构:`TValue`/`Value`/`Table`/`Node`/`Proto`/`UpVal`/`LClosure`/`CClosure`/`TString`/`Udata`,以及全部类型 tag 宏、`set*value` 写值宏 | P1-02/03/04/05、P4-14 |
| `lobject.c` | 718 | 值操作实现:相等性比较 `luaV_rawequalobj`、类型转换、字符串与数字互转的边界逻辑 | P1-02 |
| `lstate.h` | 451 | `lua_State`(线程状态)、`global_State`(全局状态)、`CallInfo`(调用帧)、`UpVal` 的运行时视图、`stringtable` | P1-02、P4-13、P6-20 |
| `lstate.c` | 420 | VM 实例生命周期:`lua_newstate`/`lua_close`、线程创建 `lua_newthread`、栈分配与重分配 | P1-02、P6-20 |
| `lvm.h` | 136 | VM 辅助宏:`tonumber`/`tointeger`/`cvt2str`/`cvt2num`、`LUA_FLOORN2I` 取整模式 | P3-11 |
| `lvm.c` | 1972 | **VM 主循环 `luaV_execute`**、算术/比较/表访问的指令实现、元方法调用 | P3-11、P3-12、P6-19 |
| `ldo.h` | 99 | 调用栈接口 | P4-13 |
| `ldo.c` | 1164 | 调用栈 `luaD_call`/`luaD_precall`、协程 resume/yield、错误抛出 `luaD_throw`、栈增长 | P4-13/15、P6-20 |
| `ltm.h` | 105 | 元方法枚举 `TMS`、`tmname` | P6-19 |
| `ltm.c` | 364 | 元方法查表 `luaT_gettm`、元方法调用 `luaT_callTM`、`__eq`/`__lt` 等事件入口 | P6-19 |

### 1.2 编译器

| 文件 | 行数 | 职责 | 对应章节 |
|---|---|---|---|
| `lzio.h` / `lzio.c` | 67 / 89 | 字符流缓冲(`ZIO`),给词法分析喂数据的抽象 | P2-06 |
| `lprefix.h` | 45 | 编译器共用前缀宏 | P2 |
| `lctype.h` / `lctype.c` | 101 / 64 | 字符分类(字母/数字/十六进制),查表实现,避免 libc `isdigit` 跨 locale 问题 | P2-06 |
| `llex.h` | 93 | token 定义 `Token`、`LexState` | P2-06 |
| `llex.c` | 604 | **词法分析器**:`llex` 主循环、保留字识别、数字与字符串字面量、长括号 `[[...]]` | P2-06 |
| `lparser.h` | 196 | `FuncState`(编译期函数状态)、`BCLine`、`LocVar`、`Upvaldesc` | P2-07/08 |
| `lparser.c` | 2193 | **语法分析器(递归下降 + 算符优先)**:`luaY_parser` 入口、`mainfunc`、语句与表达式、作用域与 upvalue 静态收集 | P2-07/08 |
| `lcode.h` | 105 | 编译期寄存器分配接口、跳转表 | P2-09 |
| `lcode.c` | 1971 | **代码生成**:`luaK_code*` 系列指令发射、`freereg` 寄存器分配、常量表 `k`、跳转回填 | P2-09 |
| `lopcodes.h` | 439 | **指令格式**:32 位布局、`SIZE_A/B/C/Bx/Ax`、`OpMode` 枚举、`OpCode` 全集、操作数提取宏 `GETARG_*` | P3-10 |
| `lopcodes.c` | 140 | `luaP_opmodes` 操作码属性表(每个 op 的 OpMode/参数类型) | P3-10 |
| `lopnames.h` | 105 | `luaP_opnames` 操作码名字表(用于反汇编/调试) | P3-10/12 |
| `ljumptab.h` | 114 | `luaV_execute` 的 `switch`/goto jumptab 分发表(取指后跳转目标) | P3-11 |

### 1.3 数据结构实现

| 文件 | 行数 | 职责 | 对应章节 |
|---|---|---|---|
| `lstring.h` | 73 | `TString` 接口 | P1-03 |
| `lstring.c` | 353 | **字符串实现**:短串驻留(interning)`luaS_new`、长串惰性哈希、字符串表 `stringtable` 扩容 | P1-03 |
| `ltable.h` | 184 | `Table` 接口 | P1-04/05 |
| `ltable.c` | 1355 | **Table 实现**:数组+哈希合体、开放寻址(5.5 新实现)、`luaH_get`/`luaH_newkey`/`luaH_set`、`rehash` 重切分 | P1-04/05 |
| `lfunc.h` | 65 | 闭包/UpVal/Proto 接口 | P4-14 |
| `lfunc.c` | 314 | **闭包与 upvalue**:`luaF_newproto`/`luaF_newLclosure`/`luaF_newCclosure`、`luaF_findupval` 开合链、upvalue 关闭 `luaF_close` | P4-14/15 |

### 1.4 垃圾回收

| 文件 | 行数 | 职责 | 对应章节 |
|---|---|---|---|
| `lgc.h` | 268 | GC 接口、GC 状态常量、三色宏 | P5-16/17/18 |
| `lgc.c` | 1804 | **增量三色 GC + 分代 GC**:`luaC_step` 主步进、标记/原子/清除三阶段、弱表/ephemeron 表、`entergen` 切分代模式 | P5-16/17/18 |

### 1.5 C API 与辅助库

| 文件 | 行数 | 职责 | 对应章节 |
|---|---|---|---|
| `lua.h` | 547 | **公共 API 契约**:lua_State 不透明指针、`lua_State` 栈式 API 声明、错误码 `LUA_OK`/`LUA_YIELD`、类型常量 `LUA_T*`、reader/writer 回调 | P6-21 |
| `lauxlib.h` | 271 | 辅助库接口:`luaL_*` 系列、`luaL_Buffer`、`luaL_Reg` | P6-21 |
| `lauxlib.c` | 1202 | **辅助库实现**:`luaL_newstate`/`luaL_loadbuffer`/`luaL_error`/`luaL_check*` 参数检查、`luaL_Buffer` 分段缓冲 | P6-21 |
| `lapi.h` | 65 | C API 内部辅助 | P6-21 |
| `lapi.c` | 1473 | **C API 实现**:`lua_pushnumber`/`lua_gettable`/`lua_pcall` 等全部 `lua_*` 函数的实现,本质是"操作 lua_State 的栈" | P6-21 |
| `lmem.h` | 96 | 内存分配接口(`l_alloc`) | P1-02 |
| `lmem.c` | 215 | 内存分配:`luaM_realloc`/`luaM_growaux`、分配失败时触发紧急 GC | P5-16 |
| `lundump.h` / `lundump.c` | 40 / 424 | 反序列化:从预编译字节码加载 `Proto`(`luaU_undump`) | (扩展) |
| `ldump.c` | 307 | 序列化:把 `Proto` 导出为预编译字节码(`luaU_dump`) | (扩展) |

### 1.6 标准库

| 文件 | 行数 | 职责 | 对应章节 |
|---|---|---|---|
| `lualib.h` | 65 | 标准库声明:`luaL_openlibs` 与各 `luaopen_*` | P6-21 |
| `linit.c` | 63 | **库注册总入口**:`luaL_openlibs` 把 10 个 `luaopen_*` 依次注册到 `lua_State` | P6-21 |
| `lbaselib.c` | 559 | `base`/`print`/`pcall`/`error`/`pairs`/`ipairs`/`tonumber`/`tostring`/`assert`/`select`/`rawget`/`rawset`/`setmetatable`/`getmetatable` 等 | P6-19/21 |
| `lcorolib.c` | 225 | `coroutine.create`/`resume`/`yield`/`wrap`/`status`/`isyieldable` | P6-20 |
| `ldblib.c` | 477 | `debug`/`getinfo`/`getlocal`/`setlocal`/`traceback`/`sethook` | (扩展) |
| `liolib.c` | 841 | `io`/`read`/`write`/`open`/`lines`/文件句柄 | (扩展) |
| `loslib.c` | 432 | `os`/`time`/`date`/`clock`/`getenv`/`execute` | (扩展) |
| `lmathlib.c` | 765 | `math`/`sin`/`floor`/`random`/`max`/`min` | (扩展) |
| `lstrlib.c` | 1894 | `string`/`find`/`format`/`gsub`/`gmatch`/`byte`/`char`/`rep`(最大的标准库) | (扩展) |
| `ltablib.c` | 426 | `table`/`insert`/`remove`/`concat`/`sort`/`pack`/`unpack` | (扩展) |
| `lutf8lib.c` | 291 | `utf8`/`char`/`codepoint`/`offset`/`codes` | (扩展) |
| `loadlib.c` | 748 | `package`/`require`/动态加载(`loadlib`) | (扩展) |

### 1.7 工具与可执行入口

| 文件 | 行数 | 职责 | 对应章节 |
|---|---|---|---|
| `lua.c` | 766 | `lua` 解释器可执行入口:`main`、REPL、命令行参数解析、信号处理 | P6-21(嵌入示例) |
| `luac.c` | 733 | `luac` 字节码编译器/反汇编器入口:把 `.lua` 编译为 `.luac`、`-l` 反汇编列表 | P3-12 |
| `luaconf.h` | 745 | **配置开关**:整数/浮点位宽(`LUA_INT_TYPE`)、`LUAI_*` 平台宏、`LUA_PATH`/`LUA_PROMPT`、内存上限 | P1-02、贯穿全书 |

### 1.8 行数小结

八组合计 50 个文件、32106 行 C。其中**真正的内核**(不含标准库和两个可执行入口)约 22000 行,而读懂这门 VM 的命脉集中在前三组——核心 VM、编译器、数据结构实现——加起来不到 13000 行。这是一个内核比 V8/CPython/JVM 都小一个数量级的实现,也是 Lua 适合作为"VM 入门样本"的根本原因。

---

## 二、推荐阅读顺序:七个阶段,由浅入深

Lua 源码的依赖关系是一条相当干净的链:`lobject.h` 是所有人依赖的根基,往上长出"编译器把源码变成字节码"和"VM 跑字节码"两条线,数据结构是两条线共用的一层,GC 和 C API 是较独立的外围。下面按这条依赖链分七个阶段。

### 第一阶段·入门:值与状态的图景

读 Lua 源码第一步不是看 VM 怎么跑,而是先在脑子里建起"一个 Lua 值在 C 里长什么样、一个 VM 实例包含什么"的图景。三个头文件按顺序读:

**1. `lua.h`(547 行,只读声明部分)**

这是 Lua 给宿主的公共契约。先看几样东西:

- `typedef struct lua_State lua_State;`——注意是不透明指针,宿主拿到的只是一个 `lua_State *`,不知道内部结构。这是"嵌入"的边界。
- `LUA_TNONE`/`LUA_TNIL`/`LUA_TBOOLEAN`/`LUA_TLIGHTUSERDATA`/`LUA_TNUMBER`/`LUA_TSTRING`/`LUA_TTABLE`/`LUA_TFUNCTION`/`LUA_TUSERDATA`/`LUA_TTHREAD`——Lua 在 API 层暴露的 9 种基本类型(注意"基本"二字,变体 variant 隐藏在内部)。
- `LUA_OK`/`LUA_YIELD`/`LUA_ERRRUN`/`LUA_ERRMEM`/`LUA_ERRERR`——返回码。
- `lua_pushnil`/`lua_pushnumber`/`lua_pushinteger`/`lua_pushstring`/`lua_gettable`/`lua_settable`/`lua_pcall`——栈式 API 的全部"动词"。这些声明告诉你:宿主和 Lua 交互的所有动作,本质都是在操作 `lua_State` 上的一个值栈。

读这一遍,你就知道"从外面看 Lua 是什么"。但你还不知道"它里面长什么样"。

**2. `lobject.h`(864 行,本书 P1 的根基)**

这是全源码最重要的一个文件,所有内核文件都依赖它。重点读这几段:

- **`Value` 联合(`lobject.h:49`)**:Lua 值的真实载体。一个 `Value` 是五个候选的联合——GC 对象指针 `gc`、light userdata 指针 `p`、light C 函数指针 `f`、整数 `i`、浮点数 `n`。基本类型(数、指针)直接装在联合里,需要 GC 管理的复合类型(TString/Table/闭包等)只装一个 `gc` 指针,真正对象在堆上。
- **`TValue`(`lobject.h:67`)**:就是 `Value` + 一个 `lu_byte tt_` 类型 tag。这是"tagged value"编码——一个 Lua 值 = 一个值 + 一个类型。Lua 的所有局部变量、栈槽、Table 值都是 `TValue`。
- **类型 tag 体系**:`LUA_VNIL`(183)/`LUA_VFALSE`(250)/`LUA_VTRUE`(251)/`LUA_VNUMINT`(336)/`LUA_VNUMFLT`(337)/`LUA_VSHRSTR`(373)等。注意这些是"基本类型 + variant"——比如字符串有短串 variant 和长串 variant,布尔有 false/true 两个 variant。variant 让 GC 和 VM 用一个 tag 字节就能区分更细的情况,不用额外的字段。
- **`Table`(`lobject.h:776`)**:数组部分 `Value *array` + 哈希部分 `Node *node` 同居一个结构。这是"统一"的物理实现(P1-04/05 全章讲它)。
- **`Proto`(`lobject.h:602`)**:一个 Lua 函数被编译后的产物。注意它的字段:`Instruction *code`(字节码指令数组)、`TValue *k`(常量表)、`struct Proto **p`(内嵌函数)、`Upvaldesc *upvalues`(upvalue 描述)、`lu_byte maxstacksize`(这个函数需要几个寄存器)。读完 `Proto`,你就知道"编译器产出的东西长什么样",而 VM 要执行的就是它。
- **`LClosure`/`CClosure`/`Closure`(`lobject.h:706/699/713`)**:Lua 闭包和 C 闭包。Lua 函数运行时是 `LClosure`(指向 `Proto` + upvalue 数组);宿主注册的 C 函数运行时是 `CClosure`(指向 `lua_CFunction` + upvalue 数组)。两者通过 `Closure` 联合统一。
- **`UpVal`(`lobject.h:679`)**:upvalue 的运行时表示。注意它有两种状态——open(指向栈上某个 `TValue *p`)和 closed(把值拷进自己的 `u.value`)。这是 P4-14 闭包机制的核心。
- **写值宏**:`setnilvalue`(211)、`setfltvalue`(351)、`setivalue`(357)、`setobj`(118)。这些是设置 `TValue` 的标准动作,先熟悉它们的形态(都是"写 `value_` + 写 `tt_`"的展开),后面读 `lvm.c`/`lapi.c` 到处都是。

**3. `lstate.h`(451 行)**

读 `lua_State`(`lstate.h:285`)和 `global_State`(`lstate.h:327`)两个结构。它们一起定义了"一个 VM 实例的全部状态":

- `lua_State` 是**线程(协程)状态**:值栈(`stack`/`top`/`stack_last`)、当前调用帧(`ci`)、开 upvalue 链(`openupval`)、to-be-closed 链(`tbclist`)、错误恢复点(`errorJmp`)、第一帧(`base_ci`)。
- `global_State` 是**所有线程共享的全局状态**:内存分配器(`frealloc`)、字符串驻留表(`strt`)、注册表(`l_registry`)、GC 全部状态(`allgc`/`gray`/`grayagain`/`weak`/`ephemeron` 链 + `gcstate`/`gckind`/`GCdebt`)、各基本类型的元表(`mt[]`)、元方法名表(`tmname[]`)。
- `CallInfo`(`lstate.h:187`):一个调用帧。注意它用 union 区分 Lua 调用(存 `savedpc`)和 C 调用(存 continuation `k`),用 `callstatus` 位域标记状态。

> **5.5 差异提示**:栈指针在 5.5 是 `StkIdRel`(相对表示,栈底偏移),不是 5.3/5.4 资料里的 `StkId`(绝对 `TValue *`)。改成相对表示后,栈 `realloc` 搬家不必逐个更新所有指向栈的指针。这是读老资料最容易踩的坑之一。

读完这三个头文件,你应该能在脑子里画出:"一个 Lua 值如何装进一个 `TValue`"、"一个 `lua_State` 持有值栈和调用链"、"全局状态管 GC 和字符串表"。这是后面所有阅读的地基。

### 第二阶段·VM 心脏:字节码怎么跑

地基搭好,先看"执行侧"的心脏——VM 主循环。这一阶段只读两个文件,但读深。

**1. `lopcodes.h`(439 行,本书 P3-10 全章)**

先看指令格式,因为 VM 主循环就是反复地"取指-译码-执行",不懂格式看不懂循环。

- **指令布局(`lopcodes.h:21-26`)**:一条指令 32 位,操作码占低 7 位,剩下 25 位按 6 种 `OpMode` 切分——`iABC`/`ivABC`(带 k 位)/`iABx`/`iAsBx`(带符号)/`iAx`/`isJ`。每种模式操作数位宽不同:`A`/`B`/`C` 各 8 位,`Bx` 17 位,`Ax`/`sJ` 25 位。

  > **5.5 差异提示**:5.5 的 `OpMode` 枚举有 6 个值,新增了 `ivABC`(带 k 位的 iABC 变体)。老资料常只列 5 个(`iABC`/`iABx`/`iAsBx`/`iAx`/`isJ`),漏 `ivABC`。

- **位宽宏**:`SIZE_A`/`SIZE_B`/`SIZE_C`(47/44/42)各 8,`SIZE_Bx`(46)=17,`SIZE_Ax`(48)=25,`SIZE_sJ`(49)=25。对应 `MAXARG_A`/`MAXARG_B`/`MAXARG_C`(106/107/109)各 255。
- **操作码全集(`lopcodes.h:235` 起)**:`OP_MOVE`/`OP_LOADI`/`OP_GETUPVAL`/`OP_GETTABLE`/`OP_CALL`/`OP_TAILCALL`/`OP_RETURN`/`OP_RETURN0`/`OP_RETURN1`/`OP_JMP`……每条后面有注释说明语义,比如 `OP_CALL` 注释为 `R[A], ... ,R[A+C-2] := R[A](R[A+1], ... , R[A+B-1])`。把这些注释读完,你对寄存器式指令"操作数直接是寄存器号"会有具体感受。
- **操作数提取宏**:`GETARG_A`/`GETARG_B`/`GETARG_C`/`GETARG_Bx`(138/142/150/161)。VM 主循环就是用它们从 32 位指令里抠出操作数。

辅助文件:`lopcodes.c`(140 行)里的 `luaP_opmodes` 数组(`lopcodes.c:22`)给每个 op 标了"哪种 OpMode、操作数是常量还是寄存器、是否影响跳转";`lopnames.h`(105 行)的 `luaP_opnames` 是反汇编用的字符串名;`ljumptab.h`(114 行)是 `luaV_execute` 在用 `goto jumptab` 分发时跳转目标的表。

**2. `lvm.c`(1972 行,本书 P3-11 全章)**

读 `luaV_execute`(`lvm.c:1198`),这是整个 VM 的主循环。重点看:

- **入口**:签名 `void luaV_execute(lua_State *L, CallInfo *ci)`。它接收一个 `CallInfo`(当前调用帧),从 `ci->u.l.savedpc` 开始取指。
- **取指**:从指令数组读一条 32 位 `Instruction`,用 `GET_OPCODE` 抠出操作码。
- **分发**:5.5 默认用 `goto` 跳转表(`ljumptab.h`),没有跳转表能力的平台退化为 `switch`。每个 op 对应一个标号。
- **操作数访问宏**:`RA(i)`(`lvm.c:1102`)= `base+GETARG_A(i)`,`RB(i)`(1104)、`RC(i)`(1107)、`RKC(i)`(1110,常量或寄存器二选一)。这几个宏是读懂所有指令实现的前提——它们把"操作数号"翻译成"值栈上的位置"。
- **几条典型指令的实现**:`OP_MOVE`(寄存器间拷贝)、`OP_LOADI`(加载整数常量)、`OP_ADD`(算术,先试整数、试浮点、再试元方法)、`OP_GETTABLE`(表访问,整数键走数组部分、短串键走短串优化路径)、`OP_CALL`(转入 `luaD_call`)、`OP_RETURN`(返回值处理)。读这几条,就抓住了 VM 解释执行的全部套路:取操作数、按类型分支、可能调元方法、写结果回寄存器。

读 `luaV_execute` 时会遇到两类宏:一类是 `RA/RB/RC`(取寄存器位置),一类是 `setobjs2s`/`setobj2n`(`lobject.h:129/135`,写值)。先在 `lobject.h` 把这些宏的定义查出来,再读循环就顺了。

辅助阅读:`lvm.h`(136 行)的 `tonumber`/`tointeger`/`cvt2str`/`cvt2num` 宏,它们定义了"什么时候允许类型自动转换"——比如 `cvt2str` 在默认配置下是 0(`lvm.h:19`),意思是数字不会自动转字符串,要靠 `__tostring` 元方法。这种"是否允许隐式转换"的开关全在这里。

### 第三阶段·编译器:源码怎么变字节码

执行侧看完,回头看编译侧——Lua 源码是怎么变成 `Proto->code` 数组的。读三个文件,按数据流顺序:`llex` → `lparser` → `lcode`。

**1. `llex.c`(604 行,本书 P2-06 全章)**

词法分析,把字符流变成 token 流。读 `llex` 主函数和 `luaX_next`。重点:

- token 定义在 `llex.h`:每个 token 有类型 `int token` 和(对数字/字符串)语义值 `SemInfo`。
- `LexState`(`llex.h`)是词法分析器的全部状态:输入 `ZIO`、当前 token `t`、下一个 token `lookahead`、缓冲 `buff`。
- 主循环识别:保留字(`while`/`if`/`function` 等查表)、标识符、数字(十进制/十六进制/浮点/整数)、字符串(单引号/双引号/长括号 `[[...]]`)、注释(`--` 和 `--[==[` 长注释)、运算符。

Lua 词法分析器写得相当紧凑(全文件 604 行),值得整篇读一遍。它没有用 `lex/yacc`,是手写状态机,因为 Lua token 规则简单到手写更短。

**2. `lparser.c`(2193 行,本书 P2-07/08)**

语法分析器,递归下降 + 算符优先。这是编译器最重的一个文件。读入口和几条主干:

- **入口 `luaY_parser`(`lparser.c:2168`)**:接收一个 `ZIO`(源码字符流),产出 `LClosure`(闭包,内含 `Proto`)。它创建 `LexState`、调 `luaX_next` 预读第一个 token、调 `mainfunc` 解析整个 chunk。
- **`mainfunc`**:解析一个 chunk(主函数体)。每个 function/lua 代码块都走相同的解析路径。
- **语句解析**:`statement` 是语句分发器,根据下一个 token 决定调 `ifstat`/`whilestat`/`forstat`/`localstat`/`exprstat` 等。
- **表达式解析**:`expr` 是表达式入口。算术/比较/逻辑走 `subexpr`,用算符优先表(`Priority` 数组)决定结合性。注意 Lua 不建 AST——边解析边调 `lcode.c` 的函数生成指令。
- **作用域与 upvalue(P2-08)**:`FuncState` 记录当前函数的局部变量表、upvalue 描述表、寄存器分配指针 `freereg`。局部变量进作用域时分配栈槽(其实就是一个寄存器号),出作用域时回收。upvalue 在编译期就静态确定(外层第几个局部变量),写到 `Proto->upvalues`。

读 `lparser.c` 时配合 `lparser.h` 的 `FuncState` 结构,会清楚"编译期函数状态"长什么样:寄存器用到第几个、有多少局部变量、跳转待回填的位置。

**3. `lcode.c`(1971 行,本书 P2-09 全章)**

代码生成。读 `luaK_*` 系列:

- **寄存器分配**:`fs->freereg` 是个线性递增计数器,每分配一个临时变量就 `freereg++`,表达式算完就回收。Lua 用最简单的线性分配——不为生存期做图着色,因为编译器要保持小。
- **指令发射**:`luaK_codeABCk`(`lcode.c:399`)是带 k 位的 iABC 指令发射器,`luaK_codeABx`(418)是 iABx 发射器。它们把操作码 + 操作数拼成 32 位 `Instruction`、追加到 `fs->f->code` 数组、返回指令的位置(用于跳转回填)。
- **常量表**:`luaK_intk`/`luaK_numberk`/`luaK_stringK` 把常量去重后放进 `Proto->k` 数组,返回常量索引。指令通过索引引用常量(`RKC` 宏根据 k 位判断操作数是寄存器还是常量)。
- **跳转回填**:`luaK_jump`/`luaK_patchlist`/`luaK_patchtohere`。Lua 编译时经常先发射一条跳转指令、目标未知,等解析到目标位置再回填。这是 `if`/`while`/`and`/`or` 的实现基础。

读完这三个文件,你能完整跟踪一段 Lua 代码从字符流到 `Proto->code` 的全过程:字符 → token(`llex`)→ 语法树节点(边解析边消化的虚拟节点,`lparser`)→ 字节码指令(`lcode`)。

### 第四阶段·数据结构深入

编译器和 VM 都依赖同一层数据结构实现。这一阶段把三个数据结构文件读透。

**1. `ltable.c`(1355 行,本书 P1-04/05 全章)**

Table 是 Lua 唯一的复合结构,实现也最精巧。读:

- **创建 `luaH_new`(`ltable.c:799`)**:新建空 Table。
- **读 `luaH_get`(`ltable.c:1020`)**:泛型读入口。它按 key 类型分发——整数走 `luaH_getint`(959,走数组部分或哈希部分)、短串走 `luaH_getshortstr`(991,有专门优化)、长串走 `luaH_getstr`(1012)、其他走通用路径。这种按类型分发是 Lua Table 快的一个原因:最常见的情况(整数键、短串键)有快路径。
- **写 `luaH_set`(`ltable.c:1195`)**:泛型写入口。找不到 key 时调 `luaH_newkey`(914)插入。
- **新 key 插入 `luaH_newkey`(`ltable.c:914`)**:开放寻址的探测插入。装填过满时调 `rehash`。
- **rehash `rehash`(`ltable.c:762`)**:Table 扩容的核心。它不只是简单翻倍——而是重新统计数组部分和哈希部分各应该占多大,然后调 `luaH_resize`(716)重新切分。算法是:统计现有 key 中整数键的分布,决定哪些整数键值得进数组部分、哪些留在哈希部分。这是"一个结构同时当好数组和哈希"的关键算法(P1-05 全章讲它)。

> **5.5 差异提示**:5.5 的哈希探测是新的开放寻址实现,与 5.4 的实现有差异(节点结构 `Node` 改为 union,带 `KeyNext` 链)。老资料讲的哈希探测细节可能对不上。

**2. `lstring.c`(353 行,本书 P1-03 全章)**

字符串实现。读:

- **新建 `luaS_new`(`lstring.c:269`)**:API 层入口,把 C 字符串变成 `TString`。
- **驻留 `luaS_newlstr`(`lstring.c:249`)**:真正创建字符串。短字符串(默认 ≤ 40 字节,`LUAI_MAXSHORTLEN` 在 `luaconf.h`)会去全局 `stringtable` 查重——已存在就返回已有指针,不存在才新建。这就是"短串驻留":两个内容相同的短串在内存里是同一个指针,比较相等只需比指针,不必比内容。
- **长串惰性哈希**:长字符串不驻留,且哈希值在第一次需要时才算(惰性),避免创建大字符串时的哈希开销。
- **字符串表 `stringtable`**:全局哈希表,存放所有驻留的短串。在 `global_State->strt`。`luaS_resize` 在表满时扩容。
- **`strcache`**(`global_State->strcache`):C API 里频繁传 C 字符串(如 `lua_getfield(L, -1, "name")`),每次都要找/创建 `TString`。`strcache` 是这层的小缓存,避免重复驻留查找。

**3. `lfunc.c`(314 行,本书 P4-14/15)**

闭包与 upvalue。读:

- **创建**:`luaF_newproto`(新建 Proto 模板,编译期产物)、`luaF_newLclosure`(新建 Lua 闭包)、`luaF_newCclosure`(新建 C 闭包)。
- **upvalue 查找 `luaF_findupval`**:这是闭包机制的核心。当一个 Lua 函数要访问外层局部变量时,在当前线程的开 upvalue 链(`L->openupval`)里找有没有现成的 upvalue 指向那个栈槽——有就复用,没有就新建一个 open upvalue(指向栈上的 `TValue`)、插进链表。
- **upvalue 关闭 `luaF_close`**:当一个局部变量离开作用域(函数返回、block 结束),它对应的 open upvalue 要被"关闭"——把栈上的值拷进 upvalue 自己的 `u.value`,状态从 open 转 closed。之后外层闭包再访问这个 upvalue,读的就是拷出来的值,不再依赖原栈。

读完 `lfunc.c`,你就理解了"Lua 闭包捕获外层变量"的完整物理实现:编译期静态决定 upvalue 是外层第几个变量(`Proto->upvalues`),运行期用 `luaF_findupval` 在栈上建立 open upvalue 链、用 `luaF_close` 在变量死亡时关闭。

### 第五阶段·调用与控制

**`ldo.c`(1164 行,本书 P4-13/15、P6-20)**

调用栈、协程、错误处理,都在这一个文件。读:

- **调用 `luaD_call`(`ldo.c:775`)**:函数调用的总入口。它先调 `luaD_precall`(715)做准备工作——判断是 Lua 函数还是 C 函数、分配新的 `CallInfo` 帧、把参数放到位;然后若是 Lua 函数,进入 `luaV_execute` 执行;若是 C 函数,直接调 `lua_CFunction`。返回时处理多返回值、回收 `CallInfo`。
- **预调用 `luaD_precall`(`ldo.c:715`)**:返回新的 `CallInfo`,设置 `savedpc`/参数个数/期望返回值数。
- **栈增长**:Lua 的值栈会按需扩容。`luaD_growstack`/`luaD_reallocstack`。注意 5.5 改用 `StkIdRel` 相对表示后,扩容 `realloc` 搬家不需要逐个修指针。
- **协程 `lua_resume`(`ldo.c:966`)**:协程的 resume。Lua 协程不是线程,是切同一个 `lua_State` 的栈。`lua_resume` 把目标线程的 `CallInfo` 链接进来、恢复执行。yield 时把当前栈顶值传回 resume 方。
- **错误抛出 `luaD_throw`(`ldo.c:125`)**:用 `longjmp` 跳回最近保护的 `errorJmp`(由 `lua_pcall`/`lua_load` 设置)。这是 Lua 错误处理不走返回值、走 `setjmp/longjmp` 的实现。
- **保护调用 `luaD_pcall`**:包裹 `luaD_call`,出错时清理栈、恢复状态、返回错误码。

`ldo.c` 是连接 VM、C API、协程三处的枢纽,读它能把"调用是怎么发生的、协程是怎么切栈的、错误是怎么传播的"一次串起来。

### 第六阶段·GC

**`lgc.c`(1804 行,本书 P5-16/17/18 全三章)**

Lua 的 GC 是增量三色标记清除,5.x 还支持分代模式。读:

- **步进 `luaC_step`(`lgc.c:1740`)**:GC 主入口。VM 每分配一定内存(由 `GCdebt` 控制),就调一次 `luaC_step` 推进一小步。这一步做多少工作由 `pause`/`stepmul` 参数(在 `gcparams` 数组)决定。增量 GC 的"可中断"就体现在这里:每步只做有限工作,然后返回 VM 继续跑字节码。
- **状态机 `gcstate`**:GC 的阶段——`GCSpause`(初始/一轮结束)→`GCSpropagate`(标记根、传播 gray 链)→`GCSenteratomic`(进原子阶段)→`GCSatomic`(原子阶段,一次性处理弱表/ephemeron/closure upvalue 等需要一致快照的部分)→`GCSswpallgc`(清除)→`GCSswpfinobj`/`GCSswptobefnz`/`GCSswpend`(分阶段清除各链)→ 回 `GCSpause`。
- **三色不变式**:白(未访问/可回收)、灰(已访问但子节点未扫)、黑(已访问且子节点已扫)。`currentwhite` 标记"当前白"。增量 GC 的核心难点是"在标记进行中,黑对象又指向了新白对象"这种突变,要用 write barrier(`luaC_barrierback` 等)把黑对象重新染灰、加回 gray 链。
- **各对象的标记函数**:`traversethread`/`traverseproto`/`traverseLclosure`/`traverseCclosure`/`traverseupvalue`/`traversetable`——每种 GC 对象怎么扫描它的子节点。
- **弱表与 ephemeron 表**:`weak`(weak value 表)、`ephemeron`(weak key 表)、`allweak`(全弱表)三条链。弱表的语义在原子阶段处理:如果 weak key 指向的对象被回收了,对应的 entry 要删掉。
- **分代 GC `entergen`(`lgc.c:1428`)**:切到分代模式。`global_State` 里有完整的分代字段——`survival`(存活过一轮的对象)、`old1`、`reallyold`、`firstold1`,以及对应的 finalizer 链。年轻代对象经历几轮 GC 后晋升为老年代,老年代不每轮都扫。
- **全量回收 `luaC_fullgc`(`lgc.c:1786`)**:强制跑一轮完整 GC,通常在内存紧张(`luaM_growaux` 分配失败触发紧急 GC)时调。

读 `lgc.c` 的策略:先读 `luaC_step` 看主循环怎么走状态机,再读标记阶段(`traverse*`/propagate),再读原子阶段(`atomic`),最后读清除阶段和分代。配合 `lgc.h` 的状态常量和宏。

### 第七阶段·嵌入:C API

**1. `lapi.c`(1473 行,本书 P6-21 全章)**

C API 的全部实现。读几条典型:

- **压值**:`lua_pushnumber`(`lapi.c:522`)/`lua_pushinteger`/`lua_pushnil`/`lua_pushstring`/`lua_pushcfunction`。本质都是"在 `L->top` 写一个 `TValue`、`top++`"。这是栈式 API 的全部秘密:宿主通过往 `lua_State` 的值栈压值来传数据给 Lua。
- **读值**:`lua_tonumber`/`lua_tostring`/`lua_tointeger`/`lua_type`。按栈索引(`-1` 是栈顶)读出 `TValue` 的内容。
- **表操作**:`lua_gettable`/`lua_settable`/`lua_getfield`/`lua_setfield`/`lua_rawget`/`lua_rawset`。封装了对 Table 的读写(走 `luaH_get`/`luaH_set`,raw 版绕开元方法)。
- **调用**:`lua_call`/`lua_pcall`/`lua_callk`。`lua_pcall` 用 `setjmp` 建保护点,出错时 `luaD_throw` 跳回、返回错误码。
- **注册 C 函数**:`lua_pushcfunction` 把一个 `lua_CFunction` 包成 `CClosure` 压栈;`lua_setfield` 把它注册到某个 Table(通常 `_G` 或 `package.loaded`)。

读完 `lapi.c`,你理解了"宿主和 Lua 的全部交互,都是宿主操作 `lua_State` 的值栈":压值、调函数、取返回值、注册 C 函数。

**2. `lauxlib.c`(1202 行)**

`luaL_*` 系列辅助函数,封装 `lua_*` 提供更高层便利:

- **`luaL_newstate`(`lauxlib.c:1184`)**:标准入口。它调 `lua_newstate`(5.5 签名是 `lua_newstate(f, ud, seed)`,三参数,第三参是随机种子——老资料是两参)创建 VM、设置默认 panic、设置默认内存分配器。宿主程序一般从这里开始。
- **加载**:`luaL_loadbuffer`/`luaL_loadfile`/`luaL_loadstring`——把源码或字节码加载成可调用的 Lua 函数(压栈一个 `LClosure`)。
- **错误格式化**:`luaL_error`(用 `printf` 风格格式化错误消息后 `luaD_throw`)。
- **参数检查**:`luaL_check*`(`luaL_checkinteger`/`luaL_checkstring`/`luaL_checkudata`)。C 函数注册给 Lua 后,从栈上取参数时要检查类型,这些函数封装了检查+出错。
- **`luaL_Buffer`**:分段字符串缓冲,避免每次拼接都 realloc。`string.format`/`string.gsub` 等大量用。
- **`luaL_Reg`**:库注册表(`{name, func}` 数组),配合 `luaL_setfuncs` 批量注册。

**3. `linit.c`(63 行)**

`luaL_openlibs`(`linit.c`)的标准库注册总入口。它把 10 个 `luaopen_*`(`linit.c:29-38` 列表)依次调一遍——`luaopen_base`/`luaopen_package`/`luaopen_coroutine`/`luaopen_debug`/`luaopen_io`/`luaopen_math`/`luaopen_os`/`luaopen_string`/`luaopen_table`/`luaopen_utf8`。每个 `luaopen_*` 都是注册一批函数到对应全局表。读这个文件,你就知道"一个开箱即用的 Lua 都自带什么库"。如果宿主要裁剪(比如嵌入式环境不要 `io`/`os`),改这里就行。

读完这三个文件,加上前面的 `lua_State` 结构,你能完整写出一个最小嵌入程序:创建 VM、加载脚本、调函数、取返回值、注册 C 函数。这是 P6-21 的闭环。

---

## 三、关键结构体速查表

下表是阅读 Lua 源码最常打交道的结构体,每个一行说明 + 5.5.0 核实位置。读源码时遇到不认识的结构体,先回这张表查定义。

| 结构体 | 一行说明 | 定义位置(5.5.0 核实) |
|---|---|---|
| `Value` | Lua 值的真实载体,5 候选联合(gc 指针/light userdata/light C 函数/整数/浮点) | `lobject.h:49` |
| `TValue` | tagged value,`Value` + 类型 tag `tt_`,所有局部变量/栈槽/Table 值都是它 | `lobject.h:67` |
| `lua_State` | 线程(协程)状态:值栈、调用链、开 upvalue 链、错误恢复点 | `lstate.h:285` |
| `global_State` | 全局共享状态:分配器、字符串表、注册表、GC 全部状态、元表数组 | `lstate.h:327` |
| `CallInfo` | 一个调用帧,union 区分 Lua(`savedpc`)/C(`k` continuation),`callstatus` 位域 | `lstate.h:187` |
| `Table` | 唯一的复合结构,数组部分 `array` + 哈希部分 `node` 同居 | `lobject.h:776` |
| `Node` | Table 哈希节点,union 带 key/value/next 链(5.5 新结构) | `lobject.h:751` |
| `TString` | 字符串对象,短串驻留/长串惰性哈希,带长度 + hash + tag | `lobject.h`(在 `TString` 定义处) |
| `Udata` | userdata(Lua 持有的宿主数据块),带元表 + 对齐大小 | `lobject.h` |
| `Proto` | Lua 函数编译产物:字节码 `code`、常量 `k`、内嵌函数 `p`、upvalue 描述、`maxstacksize` | `lobject.h:602` |
| `UpVal` | upvalue 运行时表示,open(指向栈 `p`)与 closed(值拷进 `u.value`)两态 | `lobject.h:679` |
| `CClosure` | C 闭包:`lua_CFunction` + upvalue 数组,宿主注册的 C 函数运行时形态 | `lobject.h:699` |
| `LClosure` | Lua 闭包:指向 `Proto` + upvalue 数组,Lua 函数运行时形态 | `lobject.h:706` |
| `Closure` | 闭包联合:`LClosure` 或 `CClosure` 二选一,通过 tag 区分 | `lobject.h:713` |
| `Instruction` | 一条字节码指令,`l_uint32`(32 位无符号) | `lobject.h:542` |
| `OpMode` | 指令编码模式枚举:`iABC`/`ivABC`/`iABx`/`iAsBx`/`iAx`/`isJ`(5.5 共 6 种) | `lopcodes.h:36` |
| `OpCode` | 操作码枚举,从 `OP_MOVE` 起 80+ 条 | `lopcodes.h:235` 起 |
| `LexState` | 词法分析器状态:输入 `ZIO`、当前 token `t`、`lookahead`、缓冲 `buff` | `llex.h` |
| `FuncState` | 编译期函数状态:局部变量表、upvalue 描述、`freereg`、跳转待回填 | `lparser.h` |
| `Upvaldesc` | upvalue 编译期描述:外层第几个变量 + 是否 in stack + 名字 | `lobject.h` |
| `stringtable` | 全局短串驻留表(在 `global_State->strt`) | `lstate.h` |
| `luaL_Buffer` | 辅助库分段字符串缓冲,避免拼接频繁 realloc | `lauxlib.h` |
| `luaL_Reg` | 库注册条目 `{const char *name; lua_CFunction func;}` | `lauxlib.h` |

---

## 四、关键函数速查表

下表是 Lua 内核的命脉函数,每个一行说明 + 5.5.0 核实位置。读源码时按这张表找入口,能少走很多弯路。

| 函数 | 一行说明 | 位置(5.5.0 核实) |
|---|---|---|
| `luaV_execute` | **VM 主循环**:取指-译码-执行-分发,跑当前 `CallInfo` 的字节码 | `lvm.c:1198` |
| `luaD_call` | 函数调用总入口:precall + 进入 Lua/C 函数执行 | `ldo.c:775` |
| `luaD_precall` | 预调用:分配 `CallInfo`、参数入位、设置 `savedpc` | `ldo.c:715` |
| `luaD_callnoyield` | 调用并禁止 yield(C 函数/元方法内调用不能 yield) | `ldo.c:783` |
| `luaD_throw` | 错误抛出,`longjmp` 跳回最近 `errorJmp` | `ldo.c:125` |
| `luaD_pcall` | 保护调用,出错时清理栈、恢复状态、返回错误码 | `ldo.c` |
| `lua_resume` | 协程 resume:切目标线程栈、恢复执行 | `ldo.c:966` |
| `luaH_get` | Table 泛型读:按 key 类型分发到 int/shortstr/str/通用路径 | `ltable.c:1020` |
| `luaH_getint` | Table 整数键读(优先走数组部分) | `ltable.c:959` |
| `luaH_getshortstr` | Table 短串键读(驻留指针直接比较) | `ltable.c:991` |
| `luaH_newkey` | Table 插入新 key(开放寻址探测 + 必要时 rehash) | `ltable.c:914` |
| `luaH_set` | Table 泛型写(找不到 key 时调 `luaH_newkey`) | `ltable.c:1195` |
| `luaH_new` | 新建空 Table | `ltable.c:799` |
| `luaH_resize` | Table 重新切分数组/哈希两部分(用于 rehash) | `ltable.c:716` |
| `rehash` | Table 扩容算法:统计整数键分布、重切数组/哈希、resize | `ltable.c:762` |
| `luaS_new` | 字符串创建 API 入口(C 字符串 → `TString`) | `lstring.c:269` |
| `luaS_newlstr` | 字符串真正创建(短串去驻留表查重) | `lstring.c:249` |
| `luaC_step` | GC 步进主入口:推进一小段 GC 工作,增量核心 | `lgc.c:1740` |
| `luaC_fullgc` | 强制全量回收(内存紧张时触发紧急 GC) | `lgc.c:1786` |
| `entergen` | 切到分代 GC 模式(年轻代/老年代初始化) | `lgc.c:1428` |
| `luaY_parser` | 编译器入口:源码字符流 → `LClosure`(内含 `Proto`) | `lparser.c:2168` |
| `luaK_codeABCk` | iABC 指令发射(带 k 位),追加到 `Proto->code`、返回位置 | `lcode.c:399` |
| `luaK_codeABx` | iABx 指令发射 | `lcode.c:418` |
| `luaK_nil` | 批量置 nil(给一段寄存器写 nil) | `lcode.c:132` |
| `luaF_findupval` | upvalue 查找/创建(开 upvalue 链上找或建) | `lfunc.c` |
| `luaF_close` | upvalue 关闭:open → closed,值拷出栈 | `lfunc.c` |
| `luaT_callTM` | 元方法调用(把元方法 + 操作数压栈、`luaD_callnoyield`) | `ltm.c:103` |
| `luaT_gettm` | 查对象的某个元方法(在 metatable 里找 `__eventname`) | `ltm.c` |
| `lua_pushnumber` | 压浮点值(`L->top` 写 `TValue` + `top++`) | `lapi.c:522` |
| `lua_pcall` | 保护调用(建 `errorJmp`、`luaD_pcall`) | `lapi.c` |
| `lua_gettable` | 按栈上 key 读 Table(走 `luaH_get`,触发 `__index`) | `lapi.c` |
| `lua_settable` | 按栈上 key 写 Table(走 `luaH_set`,触发 `__newindex`) | `lapi.c` |
| `lua_pushcfunction` | 把 C 函数包成 `CClosure` 压栈 | `lapi.c` |
| `lua_newstate` | 创建 VM 实例(5.5 三参数:分配器 + ud + 随机种子) | `lstate.c:336` |
| `luaL_newstate` | 标准入口(设默认分配器、panic、调 `lua_newstate`) | `lauxlib.c:1184` |
| `luaL_openlibs` | 注册全部 10 个标准库 | `linit.c` |
| `luaL_loadbuffer` | 从内存加载源码/字节码为 `LClosure` 压栈 | `lauxlib.c` |
| `luaL_error` | 格式化错误消息并 throw | `lauxlib.c` |
| `luaL_checkstring` | 检查并取字符串参数(C 函数注册给 Lua 后用) | `lauxlib.c` |
| `luaopen_*` | 10 个标准库注册函数(`base`/`coroutine`/`debug`/`io`/`math`/`os`/`string`/`table`/`utf8`/`package`) | `lbaselib.c:547`/`lcorolib.c:221`/`ldblib.c:473`/`liolib.c:832`/`lmathlib.c:752`/`loslib.c:428`/`lstrlib.c:1889`/`ltablib.c:422`/`lutf8lib.c:285`/`loadlib.c:724` |

---

## 五、动手实验建议

光读源码容易停留在"看懂了"的幻觉,实际动手跑一跑、改一改、打打桩,理解会扎实得多。下面四个实验按难度递增。

### 实验 1:反编译一段 Lua 代码看字节码(对应 P3-12)

用 `luac` 把一小段 Lua 代码编译并反汇编,对照 `lopcodes.h` 看每条指令。

```bash
# 写一小段 Lua,比如 sum.lua:
# local s = 0
# for i = 1, 10 do s = s + i end
# return s

luac -l -o sum.luac sum.lua      # -l 打印反汇编列表
```

`luac -l` 输出的格式大致是:

```
main <sum.lua:0,0> (6 instructions at 0x...)
0+ params, 4 slots, 1 upvalue, 1 loop, 2 functions at 0x...
        1       [1]     LOADI     0 0            ; 0
        2       [2]     LOADI     1 1            ; 1
        3       [2]     LOADI     2 10           ; 10
        4       [2]     FORPREP   1 1            ; to 6
        5       [2]     ADD       0 0 1
        6       [2]     FORLOOP   1 2            ; to 5
        7       [3]     RETURN1   0
        8       [3]     RETURN0
```

逐行对照 `lopcodes.h:235` 起的操作码注释:

- `LOADI R0 0`——`R[A] := sBx`,把立即数 0 装进 R0(就是 `s`)。
- `FORPREP`/`FORLOOP`——数值 for 循环的指令对,`FORPREP` 初始化循环变量、`FORLOOP` 每轮自增并判断跳转。
- `ADD R0 R0 R1`——`R[A] := R[B] + R[C]`,纯寄存器式,操作数都是寄存器号。

做完这个实验,再回头看 P3-12 的"★对照 CPython"——同一段代码 CPython 的栈式字节码要 `LOAD_FAST`/`BINARY_OP`/`STORE_FAST` 反复压栈弹栈,你会具体感受到寄存器式为什么指令更少。

进阶:把实验代码改成有闭包、有元表、有协程的版本,分别看 `GETUPVAL`/`SETUPVAL`、`GETTABUP`/`SELF`、`CALL`/`TAILCALL` 这些指令怎么发射。

### 实验 2:观察 GC 行为(对应 P5-16/17/18)

Lua 提供 `collectgarbage` 函数和 C API `lua_gc` 暴露 GC 控制。写一段会频繁产生垃圾的脚本,观察不同 GC 模式下的行为。

```lua
-- gc.lua:观察增量 vs 分代 GC
local function churn()
  for i = 1, 100000 do
    local t = {}          -- 每轮造一个短命 Table
    t[i % 100] = i
  end
end

-- 增量模式(默认)
collectgarbage("setmode", "incremental")
collectgarbage("collect")
local t0 = os.clock()
churn()
print("incremental:", os.clock() - t0, "mem:", collectgarbage("count"))

-- 分代模式
collectgarbage("setmode", "generational")
collectgarbage("collect")
local t1 = os.clock()
churn()
print("generational:", os.clock() - t1, "mem:", collectgarbage("count"))
```

对照 `lgc.c` 的 `luaC_step`(`lgc.c:1740`)和 `entergen`(`lgc.c:1428`)看:增量模式下,GC 在 `churn` 期间会反复被触发(每次分配触发一次 `luaC_step`,做一小步标记/清除);分代模式下,短命对象在年轻代就被清掉,不需要每轮都扫整个堆。

进阶:调 `collectgarbage("setpause", N)` 和 `collectgarbage("setstepmul", N)` 改 `pause`/`stepmul` 参数(对应 `global_State->gcparams`),看停顿频率和总耗时的权衡。把 `pause` 调大,GC 启动晚、单次工作量大、停顿长;调小则相反。

### 实验 3:写一个最小嵌入程序(对应 P6-21)

用 C API 把 Lua 嵌进一个 C 程序,完整跑通"创建 VM → 加载脚本 → 调函数 → 注册 C 函数 → 取返回值"的闭环。

```c
/* host.c:最小 Lua 嵌入 */
#include "lua.h"
#include "lauxlib.h"
#include "lualib.h"

/* 一个要暴露给 Lua 的 C 函数 */
static int c_add(lua_State *L) {
  lua_Integer a = luaL_checkinteger(L, 1);
  lua_Integer b = luaL_checkinteger(L, 2);
  lua_pushinteger(L, a + b);    /* 压返回值 */
  return 1;                     /* 返回值个数 */
}

int main(void) {
  lua_State *L = luaL_newstate();   /* 创建 VM */
  luaL_openlibs(L);                 /* 注册标准库 */

  /* 注册 C 函数到全局 */
  lua_pushcfunction(L, c_add);
  lua_setglobal(L, "c_add");

  /* 加载并运行一段 Lua 脚本 */
  if (luaL_dostring(L, "return c_add(3, 4) * 2") != LUA_OK) {
    fprintf(stderr, "error: %s\n", lua_tostring(L, -1));
    return 1;
  }

  /* 取返回值 */
  lua_Integer result = lua_tointeger(L, -1);
  printf("result = %d\n", result);   /* 14 */
  lua_close(L);
  return 0;
}
```

编译(假设 lua-5.5.0 已编译出静态库):

```bash
cc -I lua-5.5.0/src host.c lua-5.5.0/src/liblua.a -lm -ldl -o host
./host
```

读完 `lapi.c` 再写这个程序,每一步都对应得上:`luaL_newstate` 是 `lauxlib.c:1184` 的入口;`lua_pushcfunction` 把 `c_add` 包成 `CClosure` 压栈;`lua_setglobal` 把它写进 `_G`(本质是个 Table 的 `luaH_set`);`luaL_dostring` 编译+执行;`lua_tointeger` 从栈顶读返回值。整个宿主-脚本的双向桥就这么闭环。

进阶:把这个程序改成支持协程——用 `lua_newthread` 创建协程线程、`lua_resume`/`lua_yield` 切栈(对应 P6-20)。

### 实验 4:在关键函数加 printf 打桩

读源码最大的障碍是"不知道执行时实际走了哪条分支"。在几个命脉函数加 `printf`,跑个小脚本,观察实际控制流。

建议打桩的位置:

- **`luaV_execute`(`lvm.c:1198`)**:在主循环取指后,打印当前 `OpCode` 名(用 `luaP_opnames[GET_OPCODE(i)]`)和 `RA`/`RB`/`RC` 的值。跑 `local a = 1; local b = a + 2`,你会看到 `LOADI`/`ADDI`/`RETURN0` 这几条指令的实际执行轨迹。
- **`luaD_call`(`ldo.c:775`)**:打印被调函数类型(Lua/C)和参数个数。跑 `print(string.format("%d", 1))`,你会看到调用栈怎么从 Lua 函数(`print`)进到 C 函数(`string.format`)再返回。
- **`luaC_step`(`lgc.c:1740`)**:打印进入时的 `gcstate` 和退出时的 `GCdebt`。跑一段产生垃圾的循环,你会看到 GC 状态机怎么在 `propagate`/`atomic`/`swpallgc` 之间推进。
- **`luaH_get`(`ltable.c:1020`)**:打印 key 类型和走了哪条快路径(int/shortstr/通用)。跑 `t[1]`、`t.x`、`t["longstring"]`,你会看到整数键和短串键各走 `luaH_getint`/`luaH_getshortstr` 的快路径。
- **`luaH_newkey`(`ltable.c:914`)和 `rehash`(`ltable.c:762`)**:在 `rehash` 入口打印扩容前后的 `asize`/哈希表大小。跑 `local t = {}; for i=1, 100 do t[i] = i end`,你会看到 Table 在哪些点触发 rehash、数组部分怎么长起来。

打桩的技巧:Lua 源码里很多函数被高频调用(尤其 `luaV_execute` 的循环),直接 `printf` 会刷屏。可以加个全局计数器,只打前 N 次;或者只打特定 op(`if (op == OP_ADD) printf(...)`)。

---

## 六、源码阅读技巧

读完前面五节,你应该对"读什么、按什么顺序读、怎么动手"有了路线图。最后讲几条贯穿全程的阅读技巧,帮你少踩坑。

### 1. 宏密集,先吃透 `lobject.h`/`llimits.h` 的宏

Lua 源码大量用宏,不熟悉宏会处处卡壳。读内核前,先把这两个头文件的宏过一遍:

- **值读写宏**(`lobject.h`):`setnilvalue`(211)、`setfltvalue`(351)、`setivalue`(357)、`setobj`(118)、`setobjs2s`(129)、`setobj2s`(131)、`setobj2n`(135)、`setobj2t`(137)。这些宏都展开成"写 `value_` + 写 `tt_`"两步,但名字后缀告诉你语义差异——`2s` 表示写到栈(`s2v` 取栈值)、`2n` 写到新建对象、`2t` 写到 Table。读完一遍,后面看到 `setobjs2s(L, ra, rb)` 就知道是"把栈上 rb 处的 TValue 拷到 ra 处"。
- **类型判定宏**:`ttisnil`/`ttisboolean`/`ttisnumber`/`ttisstring`/`ttistable`/`ttisfunction`/`ttisclosure`/`ttisLclosure` 等。都是 `checktag(o, LUA_V*)` 的展开。
- **类型 tag 体系**:`rawtt`/`withvariant`/`novariant`/`ttypetag`/`ttype`(`lobject.h:84-91` 附近)。Lua 的 tag 是"基本类型(4 位) + variant(4 位)"组合,variant 用来区分同基本类型下的子类(短串/长串、false/true、int/float 闭包)。读源码时遇到 `novariant(rawtt(o)) == LUA_TSTRING`,意思就是"忽略 variant,基本类型是字符串"。
- **GC 头宏**:`CommonHeader`(展开成 `GCObject *next; lu_byte tt; lu_byte marked;`)——所有可回收对象(TString/Table/Proto/Closure/Udata/Thread/Userdata)开头都是这个三字段头,让 GC 能用统一链表管所有对象。
- **配置宏**(`llimits.h`/`luaconf.h`):`LUA_INTEGER`/`LUA_NUMBER` 的位宽、`l_mem`/`lu_mem`/`l_int32` 等内部整数类型、`MAXSIZE` 等上限。读这些能搞清"Lua 在不同平台上的整数/浮点位宽怎么选"。

### 2. C 风格:手写链表、union 复用、位运算

Lua 是 1993 年起用 ANSI C 写的,刻意保持可移植性和零依赖,所以风格上:

- **手写链表**:GC 对象用单/双向链表串(`allgc`/`gray`/`grayagain`/`weak`/`ephemeron`),不用通用容器库。读 GC 代码要习惯 `obj->next = g->allgc; g->allgc = obj;` 这种手动串链。
- **union 复用**:同一块内存在不同状态下当不同结构用。最典型的是 `UpVal`(`lobject.h:679`)——open 状态下 `u` 装的是 `{next, previous}`(链表节点),closed 状态下装的是 `TValue value`(拷出来的值)。`CallInfo`(`lstate.h:187`)的 `u` union 也是——Lua 调用存 `savedpc`,C 调用存 continuation `k`。读这种结构要时刻注意"当前是哪个 variant"。
- **位运算**:`callstatus`(`lstate.h`)用位域标记多种状态(是否 C 函数、是否 yieldable、是否在 hook 里等),`flags`(`Table` 的)用位标记"哪些元方法不存在"(避免反复查 metatable)。读这些要用 `testbit`/`setbit` 宏。
- **`lu_byte`**:就是 `unsigned char`,全源码大量用,表示小整数(tag/状态/标志)。

### 3. 注释精炼但准确,读注释常能抓住意图

Lua 源码的注释不啰嗦,但几乎每条都对。遇到不懂的代码,先读它上面的注释。几个值得专门读的注释块:

- `lobject.h` 每个结构体定义前的注释块,说明这个结构干什么、关键字段含义。
- `ltable.c:518` 附近的注释,讲开放寻址的实现原理和"为什么节点可能 nil 但不能删除"(否则会破坏探测链)。
- `lgc.c` 各阶段的注释,讲三色不变式、增量步进的正确性论证。
- `lopcodes.h:369-377` 的注释,讲 `OP_CALL`/`OP_RETURN` 的 B/C 操作数特殊语义(B==0 表示用 top、C==0 表示设置 top 给下条 open 指令)。

这些注释是作者把设计意图浓缩进去的地方,比任何二手资料都准。

### 4. 抓住"5.5 vs 老资料"的差异点

很多 Lua 资料讲的是 5.3 或 5.4,和 5.5 有几处实质差异。读源码时要心里有数:

- **栈指针**:5.5 用 `StkIdRel`(相对表示),不是 5.3/5.4 的 `StkId`(绝对 `TValue *`)。影响所有读栈代码的写法。
- **`lua_newstate` 签名**:5.5 是 `lua_newstate(f, ud, seed)`(三参,第三参是随机种子),不是老资料的两参。
- **`OpMode` 枚举**:5.5 有 6 个值,新增 `ivABC`(带 k 位的 iABC 变体)。
- **Table 哈希探测**:5.5 是新的开放寻址实现,`Node` 结构改了。
- **`Proto` 新增 `flag` 字段**(`lobject.h:606`),用位标记函数属性。
- **布尔值写值宏**:5.5 用 `setbfvalue`/`setbtvalue`(263/264),而不是老资料的 `setbvalue`。
- **`global_State` 字段更丰富**:5.5 有完整的分代 GC 字段(survival/old1/reallyold/firstold1 等),老资料常缺。

凡老资料与源码冲突,以源码为准。这是本书反复强调的原则,也是读源码的基本功。

### 5. 用 grep 跟踪一个符号的全貌

读源码时遇到一个宏或函数,想搞清"它在哪些地方被用、怎么用",最有效的工具是 `grep`。比如:

- 想看 `setobjs2s` 都在哪用:`grep -rn setobjs2s src/`。
- 想看某个 op 的处理逻辑:`grep -n "vmcase(OP_ADD)\|vnext(OP_ADD)" lvm.c`(5.5 用跳转表,搜 op 名)。
- 想看一个结构体字段被谁读写:`grep -n "savedpc" src/*.c`。

这种"从一个符号发散到全貌"的读法,比线性从头到尾读更高效。Lua 源码小,`grep` 结果通常可控。

### 6. 配合本书各章读

这个附录给的是"读源码的地图",本书正文 21 章给的是"读源码的导览"。每章都聚焦一两个文件或一个机制,带行号精讲。建议:

- 第一阶段(入门)配合 P1-02/03/04/05 读。
- 第二阶段(VM 心脏)配合 P3-10/11/12 读。
- 第三阶段(编译器)配合 P2-06/07/08/09 读。
- 第四阶段(数据结构)配合 P1-03/04/05 读。
- 第五阶段(调用控制)配合 P4-13/14/15、P6-20 读。
- 第六阶段(GC)配合 P5-16/17/18 读。
- 第七阶段(嵌入)配合 P6-19/21 读。

读到正文某章卡住,回到这个附录查它在全局的位置;读到附录某阶段想深入,去正文对应章节。

---

## 七、结束语:动手读源码,把"统一与精简"看进骨头里

走到这里,你已经拿到了读 lua-5.5.0 的全部装备:一张整体地图、一条七阶段的阅读路线、两张速查表、四个动手实验、六条阅读技巧。

回头看全书的主线——**统一与精简,换小而快**。Lua 用三招化解了"小"和"全/快"的对立:

- 一切复合数据都是 `Table`(`lobject.h:776`),一套数据结构代码服务数组/哈希/对象/模块。
- 寄存器式字节码(`lopcodes.h`),一条指令干更多事,取指译码次数更少。
- 增量三色 GC(`lgc.c`),可中断、不卡宿主,天生适合嵌入。

这三招不是孤立的口号,它们具体地落在那 32106 行 C 源码的每一个文件、每一个结构体、每一个函数里。`TValue` 的 union 编码是"统一"的根基;`Proto->code` 的紧凑指令是"精简"的产物;`luaC_step` 的状态机是"可中断"的实现;`lua_State` 的独立栈是"一个 VM 实例"的全部。

读懂这份源码,你得到的不仅是一门脚本语言的实现细节,更是一个"如何用更少的机制换更多的能力"的范本。这套思路——用统一的数据结构减少代码、用紧凑的指令模型提升执行效率、用可中断的算法适配嵌入式场景——在任何需要"在受限资源里造出强能力"的系统设计中都成立。游戏引擎、嵌入式运行时、数据库内嵌脚本、网络服务的扩展点,都能看到它的影子。

Lua 的源码之所以适合作为 VM 入门样本,正因为它小到能完整读完——32106 行,一个认真的人花几周就能从头到尾走一遍,而它又完整地包含了脚本语言运行时的全部核心部件:词法、语法、代码生成、寄存器分配、解释器循环、调用栈、闭包、协程、增量 GC、分代 GC、元表、C API。V8 太大、CPython 也十几万行、JVM 更是工业级工程,初学者容易在工程细节里迷失。Lua 把这些部件用最精炼的方式实现了一遍,每个机制都看得见、摸得着。

所以,最后的建议只有一个:**打开源码,从 `lobject.h:49` 的 `Value` 联合开始,一行行读下去**。读到不懂的地方,回到这个附录查速查表;读到想验证的地方,动手加个 `printf` 跑一遍。把"统一与精简换小而快"这条主线,从一行行 C 代码里看进骨头里。

你会发现,读懂 Lua 的过程,就是读懂"怎么用更少的机制造出更强的能力"的过程。这是这本书的全部,也是 Lua 留给每一个系统设计者的礼物。

---

*全书完。回到 [附录A 全景脉络](附录A-全景脉络.md) 看主线回扣,或回到 [目录与导读](目录与导读.md) 重读某一章。*
