# 附录B 源码阅读路线

> **本书主线**:把动态执行安全变成机器码。本附录给读者一份读 LuaJIT 2.1.ROLLING 源码的导航:文件地图、阅读顺序、结构体速查、函数速查、动手实验、阅读技巧。源码为 `LuaJIT/LuaJIT` 仓库 2.1.ROLLING(commit 7ff8551),clone 于 `深入浅出系列\luajit2`。所有行号已用 Grep/Read 逐条核实。

LuaJIT 是一个"小到能完整读懂"的 trace JIT:核心 C 代码约 4.9 万行,外加手写汇编 VM(`vm_*.dasc`)、离线生成器(`host/buildvm`)和动态汇编器(`dynasm`)。它比 V8、HotSpot 小一到两个量级,是学 JIT 的最佳教学样本。但"小"不等于"好读":它宏密集、C 和汇编混编、机器码是反向生成(先调用 emit 再执行)、还靠 buildvm 离线生成大量表。本附录的目的,是给你一张地图,让你不被这些障碍吓退,一条最省力的路线读进去。

---

## §1 整体地图:luajit2/src 的文件分类

LuaJIT 源码分十大类。下表按"职责簇"组织,每行给文件、职责、实际行数、对应本书哪一篇。C/H 文件在 `src/` 下,汇编模板 `vm_*.dasc` 也在 `src/`,离线工具在 `src/host/`,动态汇编器 `dynasm/` 在仓库根。

### 1.1 前端编译器(源码/字节码 → 字节码)

| 文件 | 行数 | 职责 | 对应篇 |
|---|---|---|---|
| `lj_lex.c`/`lj_lex.h` | 414/175 | 词法分析,把源码切成 token | P1 |
| `lj_parse.c` | 2735 | 语法分析+语义分析,源码→栈式字节码 | P1-02 |
| `lj_bcread.c` | 390 | 读预编译字节码文件(`.luac`) | P1-02 |
| `lj_bcwrite.c` | 270 | 写字节码文件 | P1-02 |
| `lj_bc.c`/`lj_bc.h` | 126/270 | 字节码定义、操作码表、解码 | P1-02 |

`lj_parse.c` 是前端的大头(2735 行),但它不属于本书重点——本书主线是 JIT,前端只在 P1-02 略讲。要读前端,从 `lj_lex.c` 的 `lj_lex_next` 入手,再到 `lj_parse.c` 的 `lj_parse_body`。

### 1.2 解释器(JIT 的基座)

| 文件 | 行数 | 职责 | 对应篇 |
|---|---|---|---|
| `vm_x86.dasc` | 5947 | x86 手写汇编解释器主循环 + exit stub | P1-02/P5-16 |
| `vm_x64.dasc` | 5073 | x64 版(同上) | P1-02/P5-16 |
| `vm_arm64.dasc` 等 | — | ARM/ARM64/MIPS/PPC 版(多后端) | P1-02 |
| `lj_dispatch.c`/`lj_dispatch.h` | 569/164 | dispatch 表 + 热点计数触发 | P1-03 |
| `lj_bc.c` | 126 | 字节码辅助 | P1-02 |

`vm_*.dasc` 是 dynasm 模板(不是纯汇编,也不是纯 C),预处理后才生成 `.s`。读解释器主循环,直接读 `vm_x64.dasc`(64 位主流),从 `->BC_INS` 标签后的 dispatch 开始。

### 1.3 数据结构(值、Table、状态)

| 文件 | 行数 | 职责 | 对应篇 |
|---|---|---|---|
| `lj_obj.h` | 1089 | TValue/GCobj/NaN-boxing/GC 头 | P1-04 |
| `lj_obj.c` | 60 | 对象辅助 | P1-04 |
| `lj_tab.c`/`lj_tab.h` | 687/95 | Table(数组+哈希段) | P1-04 |
| `lj_str.c`/`lj_str.h` | 240/90 | 字符串(内化+哈希) | P1-04 |
| `lj_state.c`/`lj_state.h` | 498/38 | lua_State 生命周期 | P1-04 |
| `lj_func.c`/`lj_func.h` | 223/73 | 函数/原型/闭包 | P1 |
| `lj_frame.h` | 206 | 栈帧布局 | P1-02 |
| `lj_buf.c`/`lj_buf.h` | 235/96 | 动态缓冲区 | 工具 |

`lj_obj.h` 是全书的"地基文件"——TValue 和 GCobj 两个 union 在这里定义,几乎每个别的文件都引用它们。务必先读这个文件。

### 1.4 JIT 核心(trace 生命周期 + 录制 + IR)

| 文件 | 行数 | 职责 | 对应篇 |
|---|---|---|---|
| `lj_jit.h` | 529 | GCtrace/jit_State/SnapShot/TraceLink | P2-05 |
| `lj_jit.c` | 364 | JIT 初始化/释放 | P2-05 |
| `lj_trace.c` | 1013 | trace 主控(生命周期) | P2-05/P5-18 |
| `lj_record.c` | 2940 | 录制:字节码→IR | P2-06 |
| `lj_ir.h` | 615 | IRIns/IRRef/IR op 枚举/REF_BIAS | P2-07 |
| `lj_ir.c` | 487 | IR 生成/常量管理 | P2-07 |
| `lj_iropt.h` | 178 | 优化 pass 声明 | P3 |
| `lj_ircall.h` | 432 | IR 调用表(纯函数桩) | P3-09 |
| `lj_ffrecord.c` | 1603 | 快速函数(library call)录制 | P2-06 |

`lj_trace.c` 是 JIT 的"心脏",`lj_record.c` 是"大脑"。这两个文件加上 `lj_ir.h`,构成了 trace JIT 的全部核心逻辑。

### 1.5 优化 pass

| 文件 | 行数 | 职责 | 对应篇 |
|---|---|---|---|
| `lj_opt_narrow.c` | 614 | 类型窄化(number→int/double) | P2-08 |
| `lj_opt_fold.c` | 2655 | 常量折叠(最大 pass,规则驱动) | P3-09 |
| `lj_opt_mem.c` | 992 | 内存优化/load-store 消除/别名分析 | P3-10 |
| `lj_opt_sink.c` | 384 | 短命分配消除 | P3-11 |
| `lj_opt_dce.c` | 171 | 死代码消除 | P3-11 |
| `lj_opt_loop.c` | 393 | 循环不变量外提 | P3-12 |
| `lj_opt_split.c` | 843 | 指令拆分(NUMBER→INT 等价变换) | P3-12 |

`lj_opt_fold.c` 是优化里最大的文件(2655 行),但真正起作用的是它的规则表——规则由 `host/buildvm_fold.c` 离线生成成 `lj_folddef.h`。

### 1.6 后端(IR → 机器码)

| 文件 | 行数 | 职责 | 对应篇 |
|---|---|---|---|
| `lj_asm.c` | 2643 | 代码生成主控 + 线性扫描寄存器分配 | P4-13/P4-14 |
| `lj_asm.h` | 56 | asm 接口声明 | P4 |
| `lj_asm_x86.h` | 3144 | x86/x64 后端(emit + patchexit) | P4-14 |
| `lj_asm_arm64.h` | 2057 | ARM64 后端 | P4-14 |
| `lj_asm_arm.h`/`lj_asm_mips.h`/`lj_asm_ppc.h` | — | 其他架构后端 | P4-14 |
| `lj_emit_x86.h`/`lj_emit_arm64.h` 等 | — | 底层指令发射宏 | P4-15 |
| `lj_target.h` | 164 | 后端目标抽象(寄存器/RA 可移植层) | P4-15 |
| `lj_target_x86.h` 等 | — | 架构相关寄存器定义 | P4-15 |
| `lj_mcode.c`/`lj_mcode.h` | 458/52 | 可执行内存分配(W^X 保护) | P4-15 |
| `lj_gdbjit.c`/`lj_gdbjit.h` | 268/30 | GDB JIT 接口(调试 trace) | P4-15 |

注意:`lj_asm_patchexit` 不在 `lj_asm.c`,而在各架构头文件(如 `lj_asm_x86.h:3127`),由 `lj_trace.c:531` 调用。这是 LuaJIT"主控与后端分离"的典型结构。

### 1.7 运行时支持(snapshot + side exit)

| 文件 | 行数 | 职责 | 对应篇 |
|---|---|---|---|
| `lj_snap.c`/`lj_snap.h` | 1034/35 | snapshot 编码 + 退出恢复 + side trace 录制入口 | P5-17/P5-18 |

snapshot 的数据结构(SnapShot/SnapEntry)不在这里,而在 `lj_jit.h`(见 §3 速查表)。`lj_snap.c` 只管逻辑:拍快照、编码、退出时恢复解释器状态。

### 1.8 FFI(调 C)

| 文件 | 行数 | 职责 | 对应篇 |
|---|---|---|---|
| `lj_ctype.h`/`lj_ctype.c` | 485/673 | C 类型系统(CType/CTState) | P6-20 |
| `lj_cparse.h`/`lj_cparse.c` | 173/1934 | C 声明语法解析(`ffi.cdef`) | P6-20 |
| `lj_ccall.h`/`lj_ccall.c` | 201/1263 | C 函数调用(ABI 实现) | P6-20 |
| `lj_cconv.h`/`lj_cconv.c` | 113/768 | C/Lua 值转换 | P6-20 |
| `lj_cdata.h`/`lj_cdata.c` | 88/118 | cdata 对象 | P6-20 |
| `lj_clib.h`/`lj_clib.c` | 48/300 | C 库加载(`ffi.C`) | P6-20 |
| `lj_carith.h`/`lj_carith.c` | 26/325 | cdata 算术 | P6-20 |
| `lj_ccallback.h`/`lj_ccallback.c` | 46/453 | C→Lua 回调 | P6-20 |
| `lj_crecord.h`/`lj_crecord.c` | 70/2007 | FFI 录制(JIT 调 C) | P6-21 |

FFI 是 LuaJIT 一大特色,但量很大(ctype+cparse+ccall+cconv+crecord 加起来近 6000 行)。本书 P6 只讲主线,真要读 FFI,先 `lj_ctype.h` 摸清类型表示,再 `lj_crecord.c` 看"JIT 怎么调 C"。

### 1.9 GC + 内存

| 文件 | 行数 | 职责 | 对应篇 |
|---|---|---|---|
| `lj_gc.c`/`lj_gc.h` | 907/93 | 增量三色 GC | P6-21 |
| `lj_alloc.c`/`lj_alloc.h` | 1485/55 | 自带内存分配器(dlmalloc 衍生) | P6-21 |

`lj_alloc.c` 几乎是 dlmalloc 的拷贝,读它对理解 LuaJIT 意义不大,跳过即可。GC 在 `lj_gc.c`,与 JIT 的协作(snapshot 要记录 GC 状态、mcode 也要被 GC 扫)是 P6-21 的重点。

### 1.10 标准库 + 工具

| 文件 | 行数 | 职责 | 对应篇 |
|---|---|---|---|
| `lib_init.c` | 55 | 库注册入口 | — |
| `lib_base.c` | 709 | base 库 | — |
| `lib_jit.c` | 769 | jit 库(`-jdump`/`-joff`/`-jv` 在此挂) | P5 附录 |
| `lib_ffi.c` | 860 | ffi 库 | P6-20 |
| `lib_string.c`/`lib_table.c`/`lib_io.c`/`lib_math.c` 等 | — | 其他标准库 | — |
| `lj_api.c`/`lj_api.h` | 1303/26 | C API(lua_*)实现 | — |
| `lj_debug.c`/`lj_debug.h` | 434/72 | 调试信息 | — |
| `lj_load.c` | 187 | 加载器(loadfile/loadstring) | — |
| `lj_meta.c`/`lj_meta.h` | 350/58 | 元方法分发 | P1 |
| `lj_err.c`/`lj_err.h`/`lj_errmsg.h` | 540/40/203 | 错误处理 + 错误消息表 | — |
| `lj_char.c`/`lj_char.h` | 41/38 | 字符分类表 | — |
| `lj_lib.c`/`lj_lib.h` | 471/159 | 库辅助宏 | — |
| `lj_assert.c` | 18 | 断言(可关) | — |

标准库不是本书重点,但 `lib_jit.c` 必读——所有 `-j` 开关、`jit.*` 表都在这里,是动手实验的入口。

### 1.11 离线工具与动态汇编器

| 文件 | 行数 | 职责 | 对应篇 |
|---|---|---|---|
| `host/buildvm.c` | 529 | 离线生成器主控 | P4-15 |
| `host/buildvm_asm.c` | 355 | 生成 vm_*.s 的字节码 dispatch 表 | P1-02 |
| `host/buildvm_libbc.h` | 42 | 字节码定义 | P1-02 |
| `host/buildvm_fold.c` | 236 | 生成 `lj_folddef.h`(fold 规则状态机) | P3-09 |
| `host/buildvm_lib.c` | 466 | 生成库注册表 | — |
| `host/buildvm_peobj.c` | 473 | Windows PE/COFF 目标 | — |
| `host/minilua.c` | 596 | 极简 Lua(给 buildvm 当工具用) | — |
| `host/genlibbc.lua`/`genminilua.lua`/`genversion.lua` | — | 生成脚本 | — |
| `dynasm/dynasm.lua` | 1095 | dynasm 预处理器主程序 | P4-15 |
| `dynasm/dasm_x86.lua`/`dasm_arm64.lua` 等 | — | 各架构 dynasm 后端 | P4-15 |
| `dynasm/dasm_proto.h` | 83 | dynasm 运行时接口 | P4-15 |
| `dynasm/dasm_x86.h` 等 | — | dynasm 运行时编码器 | P4-15 |

`host/buildvm` 和 `dynasm/` 是两套不同的代码生成机制:buildvm 在**编译期**离线生成静态表(dispatch 表、fold 规则、bcdef);dynasm 在**运行期**动态生成 trace 的机器码。两者都值得了解,但读 trace JIT,先读 dynasm(`dynasm/dynasm.lua` 是它的"汇编器")。

---

## §2 推荐阅读顺序(七阶段,由浅入深)

LuaJIT 不是一本可以从头读到尾的书。它有太多互相依赖的模块,硬读会迷路。下面这套七阶段顺序,是作者推荐的"最小阻力路径":每阶段建立一个小图景,后一阶段建立在前面之上。每阶段给"读哪个文件、看哪些函数、解决什么问题"。

### 第一阶段·数据与解释器:建立"值+解释器+热点"图景

这是地基。看不懂 TValue 和解释器主循环,后面 JIT 的一切都读不动。

- **`lj_obj.h`(读全部)**:先看 `TValue`(行 174)——它是 NaN-boxing 的核心,一个 8 字节 union 把所有 Lua 值(数字、指针、布尔、nil、lightuserdata)塞进 64 位。再看 `GCobj`(行 754),所有可 GC 对象(string/table/function/cdata/trace/...)的通用头。看懂这两个 union,你才看懂 LuaJIT 的"值"。
- **`lj_state.h` + `lj_frame.h`**:lua_State 的字段、栈帧布局。解释器在栈上跑,栈帧格式决定了一切。
- **`lj_bc.h`**:字节码定义。LuaJIT 是栈式字节码(对照官方 Lua 的寄存器式),看 `BCOp` 枚举和 `BCIns` 编码。
- **`vm_x64.dasc`(读主循环部分)**:找 `->BC_INS` 标签,看 dispatch 表怎么跳转、每条字节码怎么执行。重点理解"为什么手写汇编":每条指令的取指/译码/dispatch 都要省到极致。这个文件近 6000 行,不要全读,只读 dispatch + 几条典型指令(ADD/MOV/FOR)。
- **`lj_dispatch.h` + `lj_dispatch.c`**:看 `HotCount`(行 71)、`HOTCOUNT_SIZE`(行 74)、hotcount 数组(行 101)。理解热点检测的机制:`pc>>2` 哈希到一个 64 槽的计数器数组,循环/回填指令每执行一次计数减一,减到 0 触发 trace。

**这一阶段结束时,你应该能回答**:一段 Lua 代码,在解释器里是怎么一条条执行的?热点是怎么被发现的?

### 第二阶段·trace 全景:看一个 trace 的一生

不进细节,先看 trace 从生到死的全貌。

- **`lj_jit.h`(读全部,529 行不大)**:`GCtrace`(行 250)是 trace 对象本身,字段含义丰富(机器码指针/snapshot 数组/链接信息/起始字节码);`jit_State`(行 417)是全局 JIT 状态;`TraceLink`(行 237)枚举 9 种链接类型。这个头文件是 JIT 的"目录"。
- **`lj_trace.c` 的 `lj_trace_hot`(行 781)**:热点触发后第一个被调的函数,从这里开始读 trace 的生命周期。
- **`lj_trace.c` 的主流程函数**:`lj_trace_start`/`lj_trace_record`/`lj_trace_opt`/`lj_trace_asm`/`lj_trace_stop`/`lj_trace_exit`(行 886)。把它们串起来,就是 P2-05 讲的"trace 的一生"。

**这一阶段结束时,你应该能回答**:热点触发后,经过哪些步骤才变成可执行的机器码?退出时又发生什么?

### 第三阶段·录制 + IR:字节码怎么变 IR

进入全书最核心的部分。

- **`lj_record.c` 的 `lj_record_ins`(行 2226)**:录制主循环。每条字节码在这里被翻译成一条或多条 IR。2940 行,不要全读,跟着 `switch(op)` 看几条典型指令(ADD/CALL/FORI)。
- **`lj_ir.h` 的 `IRIns`(行 556)**:IR 指令的 union 表示,op1/op2/prev 三字段。再看 `IRRef`(行 460)和 `REF_BIAS` 偏置编码——理解 SSA 引用的"线性数组"技巧。
- **`lj_ir.c` 的 `lj_ir_emit`(行 117)**:往 IR 数组追加一条指令的底层函数。
- **`lj_opt_narrow.c`**:类型窄化。动态语言的 number 在录制时被推成 int 或 double,这是动态语言能 JIT 的关键。看 `narrow_conv`/`narrow_arith`。

**这一阶段结束时,你应该能回答**:一条字节码,是怎么变成 SSA IR 的?类型信息是怎么"猜"出来的?

### 第四阶段·优化 pass:IR 怎么被改写得更高效

IR 生成后,要过一堆优化 pass。

- **`lj_opt_fold.c` 的 `lj_opt_fold`(行 2526)**:常量折叠入口。它本身是个大 switch,但真正的规则在 `host/buildvm_fold.c` 生成的状态机里。配合 `lj_ircall.h`(纯函数桩表)读。
- **`lj_opt_mem.c`**:内存优化。load/store 消除、别名分析(两个引用是否指向同一对象)。这是 LuaJIT 优化里最有"编译器味"的一块。
- **`lj_opt_sink.c` + `lj_opt_dce.c`**:短命分配消除 + 死代码消除。短小精悍,适合理解"一个 pass 长什么样"。
- **`lj_opt_loop.c` + `lj_opt_split.c`**:循环优化 + 指令拆分。

**这一阶段结束时,你应该能回答**:一条 IR 经过哪些改写才变得更高效?哪些优化是 trace 特有的?

### 第五阶段·后端:IR 怎么变机器码

优化后的 IR 被翻译成机器码。

- **`lj_asm.c` 的 `lj_asm_trace`(行 2471)**:代码生成主入口。先做线性扫描寄存器分配(非图着色),再逐条遍历 IR 发射机器码。2643 行,按"分配→发射→fixup"三段读。
- **`lj_asm_x86.h`**(假设你读 x64):x86 后端。emit 函数(`asm_*`)怎么把 IR 翻译成 x86 指令;guard 怎么变成条件跳转;spill 怎么处理。
- **`lj_target.h` + `lj_target_x86.h`**:后端抽象层。寄存器集、`Reg`/`RA` 可移植接口。
- **`lj_mcode.c`**:可执行内存分配。重点理解 W^X(写时不可执行、执行时不可写)保护——mmap 一段内存,写完机器码后改成只可执行。
- **`dynasm/dynasm.lua`**:动态汇编器。trace 的机器码不是字符串拼接,而是通过 dynasm 的接口编码成字节。

**这一阶段结束时,你应该能回答**:IR 怎么变成机器码?寄存器怎么分配?guard 的检查代码长什么样?

### 第六阶段·运行时:snapshot、exit、side trace

机器码生成后,运行时还要支持它退出和录制新 trace。

- **`lj_snap.c` 的 `lj_snap_restore`(行 940)**:退出时恢复解释器状态的核心。看它怎么把寄存器/栈从机器码状态还原成解释器状态。辅助函数 `snap_restoreval`(行 698)/`snap_restoredata`(行 767)值得细看。
- **`vm_x64.dasc` 的 exit stub**:找 `->vm_exit_handler` 或类似标签。guard 失败时跳到这里,保存寄存器,调 `lj_trace_exit`。
- **`lj_trace.c` 的 `lj_trace_exit`(行 886)**:退出处理的 C 入口。`trace_exit_find`(行 872)根据退出 PC 找到对应的 trace 和 exit 号。
- **`lj_asm_x86.h` 的 `lj_asm_patchexit`(行 3127)**:side trace 编译后,把父 trace 退出点的跳转目标 patch 到新 trace。这是 trace 树生长的关键。

**这一阶段结束时,你应该能回答**:guard 失败时发生了什么?解释器状态怎么恢复?side trace 怎么挂到父 trace 上?

### 第七阶段·FFI + GC:JIT 与外部的协作

最后看 JIT 怎么与 C 世界和 GC 打交道。

- **`lj_ctype.h` + `lj_ctype.c`**:C 类型系统。`CType`(行 143)表示一个 C 类型,CTState 管理类型表。
- **`lj_ccall.c` 的 `lj_ccall_func`(行 1225)**:C 函数调用的运行时实现(各架构 ABI 在 `lj_ccall_*.h`)。
- **`lj_crecord.c` 的 `recff_cdata_call`(行 1335)**:FFI 调用的录制入口。看"JIT 怎么把一次 C 调用编进 trace"。
- **`lj_gc.c`**:增量三色 GC。重点看 GC 与 JIT 的协作点:snapshot 要记录哪些 slot 是 GC 对象(恢复时正确置屏障)、mcode 区域怎么被 GC 管理(`lj_gc.c` 里 mcode 的特殊处理)。

**这一阶段结束时,你应该能回答**:JIT 怎么调 C?GC 怎么不漏扫 trace 引用的对象?

---

## §3 关键结构体速查表

下表给读者一个"打开源码就能定位"的索引。每行一个结构体,一行说明,定位到 file:line(2.1.ROLLING 实测)。

| 结构体 | 一行说明 | 定义位置 |
|---|---|---|
| `TValue` | Lua 值的 NaN-boxing 表示,8 字节 union 装下数字/指针/布尔/nil | `lj_obj.h:174` |
| `GCobj` | 所有可 GC 对象的通用 union(string/table/func/cdata/trace/...) | `lj_obj.h:754` |
| `GCtrace` | 一个 trace 的全部:IR/snapshot/机器码/链接/起始字节码 | `lj_jit.h:250` |
| `jit_State` | 全局 JIT 状态:当前 trace/录制上下文/寄存器分配状态/退出信息 | `lj_jit.h:417` |
| `TraceLink` | trace 链接类型枚举,9 种(NONE/ROOT/LOOP/TAILREC/UPREC/DOWNREC/INTERP/RETURN/STITCH) | `lj_jit.h:237` |
| `IRIns` | 一条 IR 指令的 union 表示,op1/op2/prev 三字段 | `lj_ir.h:556` |
| `IRRef` | IR 引用(uint32_t),用 REF_BIAS 偏置编码正负 | `lj_ir.h:460` |
| `SnapShot` | 一个 guard 的快照元信息(map 偏移/槽位数/对应 IR ref) | `lj_jit.h:180` |
| `SnapEntry` | snapshot map 的一项(uint32_t,编码 slot+IR ref+flags) | `lj_jit.h:193` |
| `CType` | 一个 C 类型的表示(大小/对齐/属性/子类型 id) | `lj_ctype.h:143` |
| `CCallState` | C 调用的运行时上下文(参数槽/ABI 状态/返回值) | `lj_ccall.h:166` |
| `lua_State` | 解释器线程状态(栈/base/top/GC 状态/dispatch 指针) | `lj_obj.h`(搜 `lua_State`) |
| `GCtab` | Table 对象(数组段+哈希段+metatable) | `lj_obj.h`(搜 `GCtab`) |
| `HotCount` | 热点计数器类型(uint16_t) | `lj_dispatch.h:71` |
| `MCode` | 机器码字节/指针类型 | `lj_jit.h`(搜 `MCode`) |

说明:SnapShot 和 SnapEntry 不在 `lj_snap.h`(那里只有函数声明),而在 `lj_jit.h`。这是初读者常踩的坑——snapshot 的"数据"在 jit.h,"逻辑"在 snap.c。

---

## §4 关键函数速查表

按"trace 一生"的顺序排列。每行一个函数,一行职责,定位到 file:line(2.1.ROLLING 实测)。

| 函数 | 一行职责 | 定义位置 |
|---|---|---|
| `lj_dispatch_ins` | 解释器每条指令的分发入口(从汇编主循环进入 C) | `lj_dispatch.c:411` |
| `lj_trace_hot` | 热点计数归零时触发,启动 trace 录制 | `lj_trace.c:781` |
| `lj_record_ins` | 录制主循环,把一条字节码翻译成 IR | `lj_record.c:2226` |
| `lj_ir_emit` | 往 IR 数组追加一条指令(底层) | `lj_ir.c:117` |
| `lj_opt_fold` | 常量折叠入口(最大优化 pass) | `lj_opt_fold.c:2526` |
| `lj_opt_narrow` | 类型窄化(number→int/double) | `lj_opt_narrow.c` |
| `lj_asm_trace` | 代码生成主控(寄存器分配+机器码发射) | `lj_asm.c:2471` |
| `lj_asm_patchexit` | side trace 编译后 patch 父 trace 的退出跳转(各架构实现) | `lj_asm_x86.h:3127` |
| `lj_snap_restore` | guard 退出时恢复解释器寄存器/栈状态 | `lj_snap.c:940` |
| `lj_trace_exit` | 退出处理的 C 入口(被 vm 的 exit stub 调用) | `lj_trace.c:886` |
| `lj_ccall_func` | 运行时执行一次 C 函数调用(FFI) | `lj_ccall.c:1225` |
| `recff_cdata_call` | FFI cdata 调用的录制入口(把 C 调用编进 trace) | `lj_crecord.c:1335` |

补充说明:`lj_asm_patchexit` 是按架构实现的,x64 在 `lj_asm_x86.h:3127`,ARM64 在 `lj_asm_arm64.h:2046`,其余架构类似;调用点统一在 `lj_trace.c:531`。`lj_snap_restore` 内部用 `snap_restoreval`(行 698)和 `snap_restoredata`(行 767)两个辅助函数分别恢复值和数据。

---

## §5 动手实验建议

光读不练,源码读不进去。LuaJIT 给了几个很强的内省工具,务必动手跑。

### 实验 1:`luajit -jdump` 看 trace 全貌

LuaJIT 自带一个 trace dump 工具(`jit/dump.lua`)。准备一个最小热点:

```lua
-- t.lua
local s = 0
for i = 1, 1e8 do s = s + i end
print(s)
```

运行:

```
luajit -jdump=t.lua t.lua        # 只看 trace 概要
luajit -jdump=mr t.lua t.lua     # m=机器码 r=IR 都看
luajit -jdump=ms t.lua t.lua     # s=snapshot 也看
```

输出里你会看到:trace 编号、起始字节码位置、IR 指令列表(带 REF 编号)、寄存器分配结果、guard 检查点、snapshot。把它和本书 P2/P3/P4 的章节对照——纸上讲的 SSA、线性扫描、guard,在 dump 里是活生生的字节。

### 实验 2:`luajit -joff` 对照解释器

关掉 JIT,看纯解释器表现:

```
luajit -joff t.lua
```

对比有 JIT 和没 JIT 的耗时(用 `time` 命令或 `os.clock()`)。差距往往几十倍。这就是本书开头说的"几十倍"的来源——亲手量一次,比读十遍都有感觉。

### 实验 3:用最小 for 循环观察录制全过程

写一个极简循环,故意让它有时是整数、有时是浮点:

```lua
local t = {1, 2, 3, 4.5}    -- 第 4 个是浮点
local s = 0
for i = 1, #t do s = s + t[i] end
```

跑 `-jdump`,你会看到:主 trace 假设 `s` 和 `t[i]` 都是整数(快路径);第 4 次循环 guard 失败,退回解释器;之后可能从退出点长出一条 side trace(假设是浮点)。这个实验串起了 P2-05 到 P5-18 的全部概念:trace、guard、side exit、side trace。

### 实验 4:在 `lj_record_ins` / `lj_asm_trace` 加 printf 打桩

想看清录制每一步,在源码里打桩:

- 在 `lj_record.c:2226` 的 `lj_record_ins` 入口加 `printf("rec op=%d\n", bc_op(*pc))`,重编译,跑一个循环,看每条字节码被录制的过程。
- 在 `lj_asm.c:2471` 的 `lj_asm_trace` 入口加 `printf("asm trace #%d nir=%d\n", T->traceno, T->nins)`,看每个 trace 生成时有多少 IR。

打桩是读源码最有效的"显微镜"。LuaJIT 编译不慢(单文件 `make`),改了重跑很快。

### 实验 5:用 `jit.v` 看实时事件

`jit/v.lua` 是轻量版 dump,只打印事件(录制开始/结束/退出/链接),不打 IR 细节:

```
luajit -jv t.lua
```

适合看 trace 之间的链接关系和退出频率,验证 P5-19 讲的 TraceLink 9 种类型。

### 实验 6:强制 side trace 生长

写一个条件分支在循环里,让某条路径频繁触发:

```lua
local s = 0
for i = 1, 1e8 do
  if i % 100 == 0 then s = s + 1 else s = s + 2 end
end
```

用 `-jdump` 观察:主 trace 录的是 else 分支(更热),if 分支长出 side trace。观察 side trace 怎么 patch 到主 trace 的退出点(对应 `lj_asm_patchexit`)。

---

## §6 源码阅读技巧

LuaJIT 源码有几个"坑",踩过才好读。这里给具体建议。

### 技巧 1:宏密集,先认得这几十个宏

LuaJIT 大量用宏来跨架构、跨 GC 模式、跨字节码。读源码前,先在 `lj_def.h`/`lj_obj.h`/`lj_arch.h` 里认得这些宏家族:

- `gcref`/`gcrefp`/`setgcref`/`gco2*`:GC 引用的读写与类型转换。
- `tvptr`/`setitype`/`itype`/`tvisnil`/`tvisnum`:TValue 的类型检查与设置。
- `LJ_64`/`LJ_GC64`/`LJ_FR2`:编译期架构开关,决定数据布局。
- `BCOp`/`bc_op`/`bc_a`/`bc_b`/`bc_c`/`bc_d`:字节码字段解码。
- `emitir(ot, a, b)`:录制时发 IR 的糖(`lj_ir_set` + `lj_opt_fold` 合并)。

遇到不认识的宏,用 grep 找它的定义,通常就在某个头文件里几行的事。不要跳过——宏是 LuaJIT 的"缩写",不认得就读不懂句子。

### 技巧 2:dynasm 是一层预处理(`.dasc` → `.s`)

`vm_*.dasc` 不是汇编,是 dynasm 模板。语法长这样:

```c
|  mov RA, BASE              // | 开头是 dynasm 指令
|  =>BC_INS                  // => 是跳转到 dynasm 标签
```

构建时 dynasm 预处理器(`dynasm/dynasm.lua`)把它翻成纯 `.s` 汇编。读 `.dasc` 时,把 `|` 后的当成汇编指令即可,但要知道它最终是被预处理的。dynasm 的运行时编码器在 `dynasm/dasm_*.h`(各架构),trace 机器码的生成走的是同一套机制。

### 技巧 3:buildvm 离线生成静态表

很多"看起来该手写"的表,其实是 buildvm 生成的。比如:

- 字节码 dispatch 表(`lj_bcdef.h`/`lj_ffdef.h`):由 `host/buildvm_lib.c` 从 `lib_*.c` 里的 `LJLIB` 宏扫描生成。
- fold 规则状态机(`lj_folddef.h`):由 `host/buildvm_fold.c` 从 `lj_opt_fold.c` 里的规则生成。
- vm 汇编里的字节码跳转表:由 `host/buildvm_asm.c` 生成。

读源码时遇到一个 `.h` 文件找不到、却 `#include` 进来,大概率是 buildvm 生成的,去 `host/buildvm_*.c` 里找它的生成逻辑。

### 技巧 4:机器码是"反向生成"的(先调用 emit,后执行)

这是 LuaJIT 后端最反直觉的一点。生成机器码时,代码长这样:

```c
asm_guardcc(as, CC_NE);     // 先设置一个"guard 失败跳转"的上下文
emitir(...);                 // 然后才发射比较指令
```

为什么反着来?因为 x86 的跳转是"跳到后面某个地址",而那个地址(失败处理代码)此刻还没生成。LuaJIT 的做法是:先把跳转的目标地址记下来(挂到一个 patch 列表),等失败处理代码生成完,再回头 patch 跳转的目标地址。读 `lj_asm.c` 时,要适应这种"先记账、后填地址"的代码生成风格。`lj_mcode.c` 里的 fixup 机制也是为它服务的。

### 技巧 5:C 和汇编混读

trace 的执行横跨 C 和汇编:录制在 C 里(`lj_record.c`),生成的机器码是汇编,退出时从汇编跳回 C(`lj_trace_exit`)。读退出路径,要同时看 `vm_x64.dasc`(exit stub)和 `lj_trace.c`(`lj_trace_exit`)。关键交接点是 `ExitState` 结构(在 `lj_trace.c` 或 `lj_dispatch.h` 附近搜)——汇编把寄存器存进它,C 从它读出来。

### 技巧 6:2.1.ROLLING 与老资料的差异要警惕

网上很多 LuaJIT 资料讲的是 2.0 或老 2.1,与 ROLLING 有出入。常见的坑:

- GC64 模式现在是 x64 默认(TValue 布局变了),老资料的 NaN-boxing 图过时。
- `lj_vm.s` 在 ROLLING 里是构建时由 dynasm 生成的,仓库里不直接提交(老资料可能让你直接看 `.s`)。
- 一些函数改名或重排(本系列各章已标注差异)。

凡是源码与资料冲突,以源码为准。本附录的所有行号都是 2.1.ROLLING commit 7ff8551 实测,但 ROLLING 会滚动更新,你 clone 的版本行号可能有几行出入——遇到对不上,用 Grep 按函数名重新定位即可。

### 技巧 7:从一个热点函数追全链路

读源码最有效的方法不是"逐文件",而是"追一个真实场景"。挑一个你熟悉的 Lua 函数(比如 `string.byte` 或 `table.insert`),从它在 `lib_string.c`/`lib_table.c` 的实现开始,追:它怎么被解释器调用(dispatch)→ 怎么被发现为热点(hotcount)→ 怎么被录制(`recff_*` in `lj_ffrecord.c`)→ IR 长什么样(`-jdump` 看)→ 机器码怎么生成(`lj_asm.c`)。一条链路追下来,你就把全书的主线走了一遍。

---

## §7 一份"周末读完"的最小清单

如果你只有一两个周末,只想读最核心的部分,按这份最小清单:

1. `lj_obj.h` 的 `TValue`(行 174)和 `GCobj`(行 754)——2 小时。
2. `lj_jit.h` 全文——2 小时(JIT 的"目录")。
3. `lj_trace.c` 的 `lj_trace_hot`(行 781)到 `lj_trace_exit`(行 886)——4 小时(trace 一生)。
4. `lj_record.c` 的 `lj_record_ins`(行 2226)加几个典型 case——4 小时。
5. `lj_ir.h` 的 `IRIns`(行 556)——1 小时。
6. `lj_asm.c` 的 `lj_asm_trace`(行 2471)+ `lj_snap.c` 的 `lj_snap_restore`(行 940)——4 小时。
7. 跑一遍 §5 的实验 1、2、3——2 小时。

合计约 20 小时,覆盖了本书 80% 的核心。剩下的优化 pass 细节、FFI、多后端,可以按需深入。

---

## 结语:读源码是这本书的最后一章

这本书的主线是"把动态执行安全变成机器码"。读到这里,你已经知道了 trace、guard、side exit、snapshot、SSA、线性扫描、dynasm 这些机制各自是什么、为什么必须这样。但"知道"和"理解"之间,还差最后一步——亲手在源码里走一遍。

源码是这本书真正的最后一章。当你打开 `lj_record.c`,在 `lj_record_ins` 里看到那条 `switch(bc_op(*pc))`,意识到"一段 Lua 循环,就是在这里一点一点变成机器码的",那一刻,这本书才算读完。

LuaJIT 是一个值得反复读的工程样本。它小,你能读完;它完整,从字节码到机器码一条龙;它精巧,dynasm、NaN-boxing、snapshot 编码、线性扫描寄存器分配,每一处都是"为什么这样设计"的好教材。它不是最快的 JIT(HotSpot、V8 更快),但它是你**能读懂的最快的 JIT**——这正是它作为教学样本的价值所在。

祝你读得愉快。当你下次写一段 Lua、Python、JavaScript,意识到它背后可能有一整座 trace JIT 在工作的时候,你会感谢现在这个捧着源码一行行读的自己。

机器码不会说人话,但读过 LuaJIT 之后,你能听懂它。

——全书完。
