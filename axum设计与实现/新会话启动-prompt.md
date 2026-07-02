# 新会话接力 prompt(复制下方「开始」到「结束」之间全部内容,粘贴进新会话)

<!-- 开始 -->

你将接手并完成《深入浅出系列》的《axum 设计与实现深入浅出:hyper 之上的 Web 框架凭什么这么好写》一书。

**工作目录**:`c:\Users\86133\Desktop\深入浅出系列`
**当前进度**:总纲、写作提示词、目录与导读均已写好;P0-01 样板待写。源码已 clone:`../axum/`(tokio-rs/axum,**`axum-v0.8.9` tag @ commit `c59208c86fded335cd85e388030ad59347b0e5ae`**,版本 0.8.9,axum-core 0.5.6,axum-macros 0.5.1)—— clone 自 gh-proxy 镜像,**注意 checkout `axum-v0.8.9` tag,不是 main 分支**(main 在做 0.9 有 breaking changes,不是 crates.io 版本)。
**你的目标**:用多 agent 并行,写完 21 章(P0-01 ~ P7-21)+ 附录 AB,范围是全书第 0~7 篇全部写完。

## 第一步:必读(重建上下文,按顺序)
1. `axum设计与实现/全书规划-总纲.md` —— 主线(路由与分发 vs 提取与响应)、二分法、**承接 hyper/Tower/Tokio**、直球为主比喻点睛、分篇分章、源码策略。
2. `axum设计与实现/_章节写作提示词.md` —— **这是你的执行手册**:写作铁律、四段式 + 技巧精解、源码规范、配图、标点、**承接铁律、附 21 章清单与并行分组、已核实的源码非显然事实**。
3. `axum设计与实现/目录与导读.md` —— 每章一句话钩子 + 技巧标签 + 二分法归属。
4. `axum设计与实现/P0-01-第一性原理-为什么hyper之上需要axum.md` —— **风格样板**(待写,写完即定锚),所有 agent 一律对齐它的文风 / 结构 / 源码引用。
5. 承接锚点:`[[hyper-source-facts]]`(协议机/Service/连接管理)、`[[tokio-source-facts]]`(运行时)、Tower(Service/Layer,成网后)、`[[grpc-series-project]]`(HTTP/2 + filter chain 对照)。
6. 已加载的 memory 方法论:`source-code-series-method`(动机 + 技巧双线)、`source-code-book-multi-agent`(多 agent 编排 + 核验三板斧)。

## 铁律(每一章都必须遵守)
- **动机(why) + 技巧(how) 双线**:每章单开"技巧精解"小节,挑最硬核 1~2 个技巧、配真实源码 + 反面对比拆透。**讲不清 Handler trait 的 T 参数 / impl_handler! 宏展开 / FromRequestParts vs FromRequest 二元划分 / Router 的 matchit 双层匹配 = 没讲 axum。**
- **★承接《hyper》《Tower》《Tokio》铁律**:hyper 讲透的(协议机/连接管理/Service trait 本身/Body Stream)、Tower 讲透的(Service/Layer/poll_ready/ServiceBuilder,成网后)、Tokio 讲透的(运行时/task/AsyncRead/timer/budget),**一句带过 + 指路对应 source-facts**,篇幅全留 axum 独有。禁止当新东西重复——违者返工。
- **源码严禁凭记忆**:基于 `../axum/`(已 clone 后);每处引用先 Grep / Read 核实行号,不确定就**只标文件不标行号**。简化代码必须标注"(简化示意)"。**外部 crate(hyper/tower/matchit/serde)诚实标注,不当 axum 源码编行号**;axum 内部跨 crate(axum/axum-core/axum-macros/axum-extra)都是同仓,可正常标行号。★鼓励 agent 质疑 / 修正总纲不准的源码印象。
- **★版本严格用 axum 0.8.9**:正文不得用 0.7 的 API(`:foo`/`*foo` 路径参数已改 `{foo}`/`{*foo}`、`Router::route` 已只接 MethodRouter、用 route_service 接任意 Service、`from_request` 是 `impl Future` 非 async-trait)。0.7→0.8 差异专门放 P6-20 演进章讲。**老资料大片过时,以 0.8.9 源码为准。**
- **正确性**:涉及提取器/路由的机制,讲清"为什么 sound(不重复消费 body/编译期保证提取器顺序/不漏路由/merge 不冲突/nest 路径拼接正确)"。
- **标点**:半角 `,` `:` `()` `?` + 全角 `、` `——` `""` `。`(句号全角),中英文之间留空格。
- **配图**:时序 / 状态 / 流程用 `mermaid`;类型关系 / 数据结构布局(RouterInner 套 PathRouter 套 Node 套 matchit、Vec&lt;Endpoint&gt; 索引、Handler T tuple 占位、MethodRouter method 映射、提取器链 parts 顺序)用 ASCII 框图。
- **比喻**:直球为主、比喻点睛。"前台调度员+翻译官"比喻只在 P0-01 点睛,其他章不得沿用做主线。
- **命名**:`axum设计与实现/P{篇}-{章}-标题.md`;**每章正文约 50000~68000 字符(对齐《SQLite》《RocksDB》《hyper》,招牌章 70000+)**。

## 执行方式:用 Agent 工具多 agent 并行(按提示词附录的分组)
- **第一波(地基,两篇并行)**:第 1 篇 P1-02~04(框架地基 全景/Service/State)+ 第 2 篇 P2-05~08(路由 matchit+MethodRouter)——两篇互不依赖,篇内顺序。
- **第二波(三篇并行)**:第 3 篇提取响应(P3-09~13,精华中的精华)/ 第 4 篇中间件(P4-14~16)/ 第 5 篇服务高级(P5-17~19)各开一个 agent;篇内按章序。
- **第三波(收尾)**:P6-20(演进)+ P7-21(收束多对照)+ 附录 A 源码路线图 + 附录 B 实践,最后写(需全书成稿后收束)。
- 每个 agent 的任务模板:读"提示词 + 总纲 + 本篇相邻章 + P0-01 样板 + 对应 source-facts" → Grep / Read 核源码 → 写指定章节 → 过自检清单 → 产出 `P{篇}-{章}-标题.md`。
- **并行要点**:同一批要并行的 agent,放在**一条消息里多个 Agent 调用**,才会并发跑。

## 核验三板斧(主控每波收稿时做)
1. **Grep 扫结构**:每章是否有章首核心问题 / 正文四段式 / 技巧精解 / 章末五问。
2. **抽源码行号**:每章抽查 2~3 处源码引用,Read `../axum/` 对应文件,看行号 / 函数名逐字吻合。**重点核 Handler trait T 参数、impl_handler! 宏、FromRequest/Parts 二元划分、Router 双层结构这些易错点**。
3. **承接核查 + 版本核查**:每章 hyper/Tower/Tokio 是否一句带过 + 指路(没当新东西重复);外部 crate 是否诚实标注;agent 是否主动修正源码印象;**正文是否误用 0.7 API**(路径参数 `:foo`、route 接任意 Service、async-trait)——发现即返工。

## 自主推进授权
你被充分授权**自主推进**:读文件 → clone 源码并 checkout `axum-v0.8.9` tag → 启动 agent → 核验产出(抽查源码行号、技巧是否讲透、承接是否到位、正确性是否 sound、标点是否合规、版本是否 0.8.9) → 继续下一波,直到 21 章 + 附录全部完成。每完成一波向我简短汇报。只在遇到"风格 / 范围 / 深度 / 承接取舍"这类需要拍板的事时才问我,写作细节自己定。

**现在开始**:先读上面文件,然后 clone 源码(`git clone <gh-proxy>/tokio-rs/axum.git ../axum && cd ../axum && git checkout axum-v0.8.9`),然后启动第一波 agent(框架地基 + 路由 matchit 并行)。

<!-- 结束 -->
