# 新会话接力 prompt(复制下方「开始」到「结束」之间全部内容,粘贴进新会话)

<!-- 开始 -->

你将接手并完成《深入浅出系列》的《Tower 设计与实现深入浅出:为什么把每个请求都抽象成一个可组合的 Future》一书。

**工作目录**:`c:\Users\86133\Desktop\深入浅出系列`
**当前进度**:总纲、写作提示词、目录与导读均已写好;`tower`(tower-rs/tower)源码**已 clone** 到系列根目录(`../tower/`,tag `tower-0.5.2`,短 commit `7dc533e`,版本 0.5.2;`tower-service` 0.3.3 在子目录 `tower-service/`;`tower-layer` 0.3.3 在子目录 `tower-layer/`)。clone 自 gh-proxy 镜像。
**你的目标**:先 clone 源码钉死完整 commit 哈希,然后用多 agent 并行,写完 20 章(P0-01 ~ P7-20)+ 附录 AB,范围是全书第 0~7 篇全部写完。

## 第 0 步:clone 源码并钉死 commit(动笔前必做)
1. 把 `tower-rs/tower` clone 到 `../tower/`(经 gh-proxy 镜像)。
2. `cd ../tower && git checkout tower-0.5.2 && git rev-parse HEAD` 取完整 40 位哈希,记下来(对标 hyper 钉死 `aecf5abf`/1.10.1 的做法)。
3. 核实三个版本号:`tower/Cargo.toml` 的 `version = "0.5.2"`、`tower-service/Cargo.toml` 的 `0.3.3`、`tower-layer/Cargo.toml` 的 `0.3.3`。
4. ★已核实的关键事实(由四件套建设专员经 zread 核实,务必沿用,勿推翻):
   - `tower-service/src/lib.rs`:**Service trait 仍保留 `poll_ready(&mut self)` 和 `call(&mut self, req)`**(都 `&mut self`,★与 hyper 删了 poll_ready 形成对照)。
   - `tower-layer/src/lib.rs`:**Layer trait** `fn layer(&self, inner: S) -> Self::Service`,加 `mod {identity, layer_fn, stack, tuple}`(`Stack<L, Inner>` 类型级洋葱)。
   - **★自 0.4.0 起所有原独立 `tower-*` 中间件 crate(tower-timeout/tower-retry/tower-buffer/tower-limit/tower-balance/tower-load/tower-load-shed/tower-discover/tower-reconnect/tower-hedge/tower-filter/tower-util/tower-make/tower-ready-cache/tower-spawn-ready/tower-steer 等)已全部合并进 `tower` crate,放在 feature flag 后面**。老博客/老资料里的独立 crate 名**已过时**,真实路径是 `tower/src/timeout/`、`tower/src/balance/p2c/`、`tower/src/retry/budget/` 等。
   - `tower/src/` 真实模块:`balance/`(含 `p2c/` 子目录)、`buffer/`、`discover/`、`filter/`、`hedge/`、`limit/`(含 `concurrency/`+`rate/`)、`load/`(`peak_ewma`/`pending_requests`/`constant`/`completion`)、`load_shed/`、`make/`、`ready_cache/`、`reconnect/`、`retry/`(含 `budget/` + `backoff.rs`)、`spawn_ready/`、`steer/`、`timeout/`、`util/`(含 `boxed/`/`call_all/`/`optional/` 子目录 + 一堆组合子)、`builder/`(`ServiceBuilder`)、`layer.rs`、`lib.rs`。
   - 0.5.0 大改:retry `Policy::retry` 改 `&mut req`/`&mut res`、`Budget` 从结构体重构成 trait、Buffer 容量修 off-by-one(#635)、`Either::A/B` 改 `Left/Right` 且要求同 error、`BoxService` 变 Sync(#702);0.5.2 加 `BoxCloneSyncService`/`BoxCloneSyncServiceLayer`(#777/#802)。
   - 鼓励 agent 在写时质疑/修正总纲不准的源码印象,但上面这几条是核实过的硬事实。

## 第一步:必读(重建上下文,按顺序)
1. `Tower设计与实现/全书规划-总纲.md` —— 主线(执行单元 Service vs 组合单元 Layer)、二分法、**承接 Tokio/hyper**、直球为主比喻点睛、分篇分章、源码策略。
2. `Tower设计与实现/_章节写作提示词.md` —— **这是你的执行手册**:写作铁律、四段式 + 技巧精解、源码规范、配图、标点、**承接铁律、附 20 章清单与并行分组**。
3. `Tower设计与实现/目录与导读.md` —— 每章一句话钩子 + 技巧标签 + 二分法归属。
4. `Tower设计与实现/P0-01-第一性原理-为什么Rust异步生态需要Tower.md` —— **风格样板**,所有 agent 一律对齐它的文风 / 结构 / 源码引用。
5. 承接锚点:`[[tokio-source-facts]]`(Future/Poll/mpsc/Semaphore/time)、`[[hyper-source-facts]]`(hyper Service 删 poll_ready、Service 入门)、`[[grpc-source-facts]]`(filter stack 对照)、`[[envoy-source-facts]]`(filter chain 对照)。
6. 已加载的 memory 方法论:`source-code-series-method`(动机 + 技巧双线)、`source-code-book-multi-agent`(多 agent 编排 + 核验三板斧)。

## 铁律(每一章都必须遵守)
- **动机(why) + 技巧(how) 双线**:每章单开"技巧精解"小节,挑最硬核 1~2 个技巧、配真实源码 + 反面对比拆透。**讲不清 poll_ready 背压 / Layer 洋葱 / Buffer worker / Balance P2C / ServiceBuilder 类型级 Stack = 没讲 Tower。**
- **★承接《Tokio》《hyper》铁律**:Tokio 讲透的(Future/Poll/mpsc/Semaphore/time/budget)、hyper 讲透的(Service trait 入门 P1-02、Tower 中间件链 P1-03),**一句带过 + 指路对应 source-facts**,篇幅全留 Tower 独有。★关键对照点贯穿全书:**hyper 的 Service 删了 `poll_ready`,Tower 保留**——讲每章时点到 + 指路,别当新东西重复——违者返工。
- **源码严禁凭记忆**:基于 `../tower/`;每处引用先 Grep / Read 核实行号,不确定就**只标文件不标行号**。简化代码必须标注"(简化示意)"。**外部 crate(tokio/hyper/axum/tonic/hdrhistogram)诚实标注,不当 tower 源码编行号**;`tower-service`/`tower-layer` 是同仓子目录(`../tower/tower-service/`、`../tower/tower-layer/`),算 Tower 源码可编行号。★严禁用废弃的 `tower-xxx` 独立 crate 名当路径(如 `tower-timeout` 当 crate),真实路径是 `tower/src/timeout/` 等。★鼓励 agent 质疑 / 修正总纲不准的源码印象。
- **正确性**:涉及背压/并发/重试/负载均衡的机制,讲清"为什么 sound(不丢背压/不泄漏 worker/permit 不死锁/重试不风暴/P2C 收敛)"。
- **标点**:半角 `,` `:` `()` `?` + 全角 `、` `——` `""` `。`(句号全角),中英文之间留空格。
- **配图**:时序 / 状态 / 流程(请求穿洋葱、Buffer worker 状态、P2C 抽样、Retry Policy 决策、ServiceBuilder Stack 嵌套)用 `mermaid`;内存 / 布局(`Stack<T,L>` 类型嵌套、mpsc 通道、trait object 布局、Budget 桶)用 ASCII 框图。
- **比喻**:直球为主、比喻点睛。洋葱/插座比喻只在 P0-01 点睛,其他章不得沿用做主线。
- **命名**:`Tower设计与实现/P{篇}-{章}-标题.md`;**每章正文约 50000~68000 字符(对齐《hyper》《SQLite》《RocksDB》,招牌章 70000+)**。

## 执行方式:用 Agent 工具多 agent 并行(按提示词附录的分组)
- **第一波(地基,两篇并行)**:第 1 篇 P1-02~04(核心 trait Service/Layer)+ 第 2 篇 P2-05~07(背压类 Buffer/SpawnReady/LoadShed)——两篇互不依赖,篇内顺序。
- **第二波(四篇并行)**:第 3 篇限流超时(P3-08~10)/ 第 4 篇韧性(P4-11~13)/ 第 5 篇路由负载均衡(P5-14~16)/ 第 6 篇工程化(P6-17~19)各开一个 agent;篇内按章序。
- **第三波(收尾)**:P7-20 双对照 + 附录 A 源码路线图 + 附录 B 实践,最后写(需全书成稿后收束)。
- 每个 agent 的任务模板:读"提示词 + 总纲 + 本篇相邻章 + P0-01 样板 + 对应 source-facts" → Grep / Read 核源码 → 写指定章节 → 过自检清单 → 产出 `P{篇}-{章}-标题.md`。
- **并行要点**:同一批要并行的 agent,放在**一条消息里多个 Agent 调用**,才会并发跑。

## 核验三板斧(主控每波收稿时做)
1. **Grep 扫结构**:每章是否有章首核心问题 / 正文四段式 / 技巧精解 / 章末五问。
2. **抽源码行号**:每章抽查 2~3 处源码引用,Read `../tower/tower/src/`(或 `tower-service/`/`tower-layer/`)对应文件,看行号 / 函数名逐字吻合。★抽查是否用了废弃 `tower-xxx` 独立 crate 名(违规则返工改路径)。
3. **承接核查**:每章 Tokio/hyper 是否一句带过 + 指路(没当新东西重复);★hyper 删 poll_ready vs Tower 保留 的对照点是否正确点到;外部 crate 是否诚实标注;agent 是否主动修正源码印象。

## 自主推进授权
你被充分授权**自主推进**:先 clone 源码钉死 commit → 读文件 → 启动 agent → 核验产出(抽查源码行号、技巧是否讲透、承接是否到位、正确性是否 sound、标点是否合规、没用废弃 crate 名) → 继续下一波,直到 20 章 + 附录全部完成。每完成一波向我简短汇报。只在遇到"风格 / 范围 / 深度 / 承接取舍"这类需要拍板的事时才问我,写作细节自己定。

**现在开始**:先 clone 源码钉死 commit,然后读上面文件,然后启动第一波 agent(核心 trait + 背压类并行)。

<!-- 结束 -->
