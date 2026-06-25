# 第 18 章 · 调度器:Pod 放到哪个节点

> **前置**:你需要先读完[第 15 章《k8s 架构:控制平面与节点》](P5-15-k8s架构-控制平面与节点.md)和[第 17 章《声明式 API 与 reconcile》](P6-17-声明式API与reconcile-k8s的灵魂.md)(或至少第 14 章的 reconcile 范式)。第 15 章把 scheduler 摆进了控制平面——它是"那个专职调度员",负责回答"这箱货上哪艘船";而第 14、17 章把 reconcile 循环讲到了 informer 的层面。这一章我们钻进调度员一个人,问一个**被 reconcile 反复触发、却一直没拆开的子问题**:reconcile 循环里那句注释"补到哪个节点,是调度器的事"——它具体是怎么决策的?为什么这样设计?

> **核心问题**:集群里几百个节点,一个新的 Pod 该放哪个?谁决定、怎么决定、为什么这样决定?
>
> 这是 k8s 控制平面里**最像"算法"**的一个组件。其他 controller 们干的活儿,本质都是"看期望和实际的偏差,补或杀"——一个朴素的负反馈循环。唯独 scheduler 不是这样:它要在一个几百维的解空间里(几百个节点 × 几十条约束 × 几十种偏好),找出一个**当下最优**的落点。这件事没法靠"补/杀"解决,它必须**一次性算对**。这一章,我们把这套两阶段决策(filter 过滤 + score 打分)从直觉讲到源码,并回答四个绕不开的"为什么":为什么分两步?为什么把每个判断做成**可插拔的插件**?为什么调度器坚持**无状态、可预测**?以及那些调度维度(requests/limits、亲和反亲和、taint/toleration、优先级抢占)各自到底在解决什么不解决就不行的问题。
>
> **读完本章你会明白**:
> - 为什么调度必须分**过滤 + 打分**两阶段,而不是"一个公式直接算出最优节点"——这两个阶段解决的根本不是同一类问题,硬塞在一起会两头不讨好。
> - 为什么 k8s 把每个判断(资源够不够、污点能不能忍)都做成 **scheduling framework 的插件**,而不是写死在调度器里——这是 k8s 能让你"换个调度策略而不重写整个调度器"的根基。
> - 为什么调度器刻意保持**无状态、可预测**(同样的集群快照,同一个 Pod 永远被调度到同一个节点),以及这件事对调试和扩展意味着什么。
> - requests/limits、nodeAffinity/podAffinity、taint/toleration、priority/preemption 这四组维度,**各解决什么不解决就会出什么乱子**——它们不是 k8s 凑数堆出来的字段,每一个都对应一类真实的调度灾难。

> **如果一读觉得太难**:k8s 的调度维度名词很多(requests、limits、affinity、anti-affinity、taint、toleration、priority、preemption...),很容易把人淹没。先只记住三件事——
> ① **调度 = 两阶段**:先**过滤**(filter)出"哪些节点能放这个 Pod",再在能放的里面**打分**(score),选分最高的。就这么简单。
> ② **每个判断都是一个插件**:资源够不够、污点能不能忍、亲和性满不满足——每一项都是一个**可插拔的插件**(scheduling framework),filter 阶段调 `Filter` 插件,score 阶段调 `Score` 插件。换调度策略就是换插件。
> ③ **调度器自己也是个 controller**:它 watch 那些"还没分到节点"的 pending Pod,算出节点后写回 PodSpec 的 nodeName——这就是它一轮 reconcile 的全部。剩下的四组维度,是它决策时要考虑的"考题"。

---

## 章首·一句话点破

如果你对"k8s 怎么决定 Pod 放哪个节点"的印象是"它有个智能算法,综合考虑各种因素给出最优解",请把这种印象先放一边。它会让你期待一个深不可测的优化引擎,从而错过调度器真正精妙的地方——**它的精妙恰恰不在"算法有多复杂",而在"它把决策拆得有多干净"**。

这一章要做的第一件事,是把这句话连根拔起:

> **k8s 的调度器不是一个"一锤定音"的智能算法,而是一条被拆成两阶段的流水线:先用一组 filter 插件,把"根本放不下"的节点剔除;再用一组 score 插件,在剩下的里面挑最合适的。这两阶段,每个判断都是一个独立、可插拔的插件。整套调度的全部"智能",都被塞进了"插件"这个统一容器里——调度器自己,只是一个老老实实把插件按顺序跑一遍的循环。**

我们一块一块拆。先从最朴素的问题开始:为什么是两阶段,而不是一步到位?

---

## 一、调度器自己也是个 controller:它就是 reconcile 里"补到哪个节点"那一步

在拆两阶段之前,我们要先把调度器在 k8s 整体架构里的**身份**钉死,否则后面所有讨论都会悬在半空。

第 14 章那段 reconcile 伪代码里,有一行关键的注释你可能还记得:

```go
if actual < desired {
    createPods(desired - actual)   // 补到哪个节点,是调度器(scheduler)的事
}
```

Deployment 控制器知道"该补几个 Pod",但它**不知道这每个 Pod 该放哪个节点**——这不是它的职责。它只是把"缺 3 个"翻译成"新建 3 个 Pod 对象",写进 etcd。这 3 个 Pod 一被写进 etcd,它们就处于 `Pending` 状态(还没有 `nodeName`)。**从这一刻起,接力棒交给了 scheduler。**

> **比喻**:这就像航运总部督办处的白领,他看到"3 箱货还没装船",就开出 3 张空白运单(新建 3 个 Pod),扔进"待派单"那一摞。至于这 3 张运单**分别上哪艘船**,他不管——那是**专职调度员**(scheduler)的活儿。调度员每天盯着"待派单"那一摞,看到有新运单就抽出来,翻一遍船队名册,在运单上填好"上 3 号船",然后把单子扔回总部(写回 etcd)。从此这张单子有了归属,3 号船上的驻港经理(kubelet)就会看到"我船上有张新单子",开始真正去码头吊集装箱。

这段比喻,在源码层面是字面成立的。看调度器的入口 `pkg/scheduler/scheduler.go`,它干的第一件事,就是**用 informer 订阅所有非终态的 Pod**(只看还在跑、或还在等调度的 Pod,不看已经成功/失败的):

> **源码位置**:[`pkg/scheduler/scheduler.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/scheduler/scheduler.go) 的 `newPodInformer` 函数。注意它用的 field selector:`status.phase!=Succeeded,status.phase!=Failed`——即只看"非终态"的 Pod。

```go
// (摘自 master,scheduler.go 中的 newPodInformer,简化展示)
func newPodInformer(cs clientset.Interface, resyncPeriod time.Duration) cache.SharedIndexInformer {
	selector := fmt.Sprintf("status.phase!=%v,status.phase!=%v", v1.PodSucceeded, v1.PodFailed)
	tweakListOptions := func(options *metav1.ListOptions) { options.FieldSelector = selector }
	informer := coreinformers.NewFilteredPodInformer(cs, metav1.NamespaceAll, resyncPeriod, cache.Indexers{}, tweakListOptions)
	// ...
}
```

读到这个 field selector,你就懂了 scheduler 的"视野":它**只关心两类 Pod**——一类是已经分了节点、还在跑的(它需要知道这些占着哪些资源),另一类是没分节点、还在 `Pending` 的(这才是它的"待派单")。已经成功结束、或彻底失败的 Pod,它眼不见为净——这些不占资源、也不需要调度。

那些 `Pending` 的 Pod,会被 `addAllEventHandlers`(在 `scheduler.go` 的 `New` 里)推进一个叫 **SchedulingQueue** 的队列。然后调度器的主循环——`Run` 方法——开一个 goroutine,死循环调 `ScheduleOne`:

```go
// (摘自 master,scheduler.go 的 Run,简化展示)
func (sched *Scheduler) Run(ctx context.Context) {
	logger := klog.FromContext(ctx)
	sched.SchedulingQueue.Run(logger)
	// ...
	go wait.UntilWithContext(ctx, sched.ScheduleOne, 0)   // 死循环调 ScheduleOne
	<-ctx.Done()
	// ... 清理
}
```

`wait.UntilWithContext(ctx, sched.ScheduleOne, 0)` 这一句的意思是:**不停地调 `ScheduleOne`,直到 ctx 被取消**。而 `ScheduleOne` 每被调一次,就从队列里弹一个 Pod 出来,算出节点,写回。**这就是一轮 scheduler 自己的 reconcile**——它和 Deployment 控制器是同一个范式(watch → 处理 → 写回),只是它"处理"的内容不是"补/杀 Pod",而是"给 Pod 定个节点"。

> **回扣第 15 章的一句话**:第 15 章把 scheduler 单列成控制平面里一个独立组件,没并进 controller-manager,理由是"调度特别复杂、特别重要、且大家想各搞各的"。这一章你会看到这个理由的代码证据——调度器的内部,被设计成了**高度插件化**的 scheduling framework,你可以不动一行调度器代码、只写几个插件,就改变它的调度行为。**这是 controller-manager 里那些 reconcile 控制器所不具备的可替换性**,也是 scheduler 独立成进程的真正原因。

身份钉死了:scheduler = 一个 watch `Pending` Pod 的 controller,它的 reconcile 动作是"给 Pod 算一个节点,写回 nodeName"。下面我们就钻进这个"算"的过程。

---

## 二、为什么是两阶段:filter + score 的直觉

现在主问题来了:**集群里几百个节点,一个新 Pod 该放哪个?**

最朴素的思路,是"设计一个综合公式,把所有因素(资源、亲和、污点……)揉进去,直接给每个节点算一个分数,选分最高的"。这条路看似优雅,实际上会在三个地方同时崩塌。我们先把"不这样会怎样"讲透。

### 不这样会怎样:一个公式算到底的灾难

假设我们坚持"一个公式"。那这个公式必须同时处理两类**性质完全相反**的约束:

- **硬约束**(这个 Pod **绝对不能**放这种节点):这箱货是冷藏货,而 5 号船根本没有冷藏舱——这种节点,**连考虑的资格都没有**,放进打分公式里给它打低分,是危险的(万一分高的几个全满了,这个低分节点反而"相对最高",被选上,然后货物到了船上发现没冷藏舱,Pod 启动失败)。
- **软偏好**(这个 Pod **更想**放某种节点):这箱货走哪条航线都行,但走 3 号船中转少、更快——这种偏好,是"分越高越好",不是"不满足就出局"。

硬约束要的是"一票否决"(出局),软偏好要的是"加权排序"(打分)。**这两套逻辑,根本不能用同一个公式表达**。硬把它们揉在一起,会出现两类错误:

- **硬约束被软偏好"稀释"**:5 号船没冷藏舱(本该出局),但它在别的偏好项上分高(航线短、装得快),总分反而最高,被选上——结果 Pod 调度到这个节点,启动时发现没冷藏舱,容器起不来。**调度器报一个 `FitError`,把 Pod 扔回队列重排**。这比"一开始就别选它"浪费得多。
- **软偏好被硬约束"放大"**:为了表达"没冷藏舱就出局",你不得不给"有冷藏舱"这个项一个极大权重(比如 10000 分)。可这个巨大权重会**压垮所有真正的软偏好**——一个"有冷藏舱但航线绕远"的节点,会比一个"有冷藏舱且航线短"的节点分还高(因为前者在冷藏这个 10000 分项上拿满了,航线那点小分差翻不起浪)。**软偏好名存实亡**。

更糟的是**性能**。一个几百节点的集群,如果你要给每个节点都跑完那个"综合公式"(包含所有资源计算、所有亲和判断、所有污点检查),那是 O(节点数 × 约束数) 的全量计算。而实际上,**90% 的节点根本放不下这个 Pod**(资源不够、污点不符),给它们算完整公式纯属浪费。

### 所以这样设计:先剔除,再排序

k8s 的解法,是把这两类约束**物理上拆成两个阶段**,各用各的逻辑:

> **阶段一·filter(过滤,旧称 predicate):用硬约束,把"放不下"的节点剔除。** 这一批节点,从此再也不参与考虑——不管它们在软偏好上多优秀,出局就是出局。filter 的输出,是"剩下的、能放这个 Pod 的候选节点集合"。
>
> **阶段二·score(打分,旧称 priority):用软偏好,在候选集合里挑最合适的。** 此时所有节点都已经过了硬约束的筛子,**score 只需要关心"哪个更好",再也不用担心"放不放得下"**。每个 score 插件给每个候选节点打一个 0~100 的分,加权求和,分最高的胜出。

> **比喻**:调度员的工作台,先是**一张"准入清单"**:把船队名册铺开,用红笔划掉所有"装不下这箱货"的船——载重不够的划掉、航线不符的划掉、标着"只接冷藏货但这箱不是冷藏"的划掉。划完一看,500 艘船里只剩 30 艘。然后他**拿出一张"打分表"**:对着这 30 艘,逐艘打分(航线短加分、装得均衡加分、和别的货不冲突加分),选出分最高的那艘,在运单上填好船号。**红笔阶段(filter)负责"能不能",打分阶段(score)负责"好不好"——两件事、两套工具、两个阶段,绝不相混。**

这个"先剔除、再排序"的结构,精确地解决了前面三个崩塌点:

- **硬约束不会被稀释**:filter 阶段直接出局,连打分的资格都没有。5 号船没冷藏舱,在 filter 就被划掉,score 阶段根本看不到它。
- **软偏好不被压垮**:score 阶段所有节点都过了硬约束,软偏好可以放心用 0~100 的小分差来表达"哪个更好",不用再设什么 10000 分的巨权。
- **性能大幅提升**:filter 阶段一旦判断某个节点放不下,立刻跳到下一个,**剩下的 score 计算只跑在少数候选上**。一个 500 节点的集群,可能 470 个在 filter 就被划掉,score 只对 30 个算。**计算量从"500 × 全部约束"降到"500 × 硬约束 + 30 × 软约束"**。

> **一个常被忽略的细节**:filter 阶段有一个**短路优化**——k8s 不会傻乎乎地把所有节点都 filter 一遍。它有一个 `percentageOfNodesToScore`(默认动态算,通常远小于 100%)的机制:一旦找到足够多的可行节点,就停止继续 filter 剩下的。这是大规模集群下调度性能的关键一招——**当可行节点已经够选了,没必要把不可能放下的节点也跑一遍 filter**。

### 拆开后的代价:什么时候 filter 全军覆没

这套两阶段不是没代价。最尴尬的场景,是 **filter 阶段一个节点都没剩下**——所有节点都放不下这个 Pod。

这时,调度器会把这个 Pod 扔回 `SchedulingQueue` 的 **unschedulableQ**(不可调度队列),并附上一个详细的 `FitError`,告诉你"为什么每个节点都不行"(哪个节点因为哪个插件被否决)。Pod 会在那里待一段时间(默认 10 秒,可配),或者等集群状态发生变化(有新节点加入、有 Pod 退出、有 taint 被移除)再被重新拉回 activeQ 重排。

> **filter 全军覆没,有两种性质截然不同的原因**:
> - **真的没地方放**:集群所有节点资源都不够。这时唯一的出路是**扩容节点**(加机器),或者**抢占**(preemption,把别的低优先级 Pod 踢掉腾地方——见后文)。
> - **约束太严**:你给 Pod 设了太多硬约束(nodeSelector 写死了"必须 ssd=true",可全集群只有机械盘),怎么都找不到匹配的。这时出路是**松约束**,或者**给某些节点打对的 label**。
>
> 调度器的 `FitError` 会区分这两种情况,帮助运维定位。这也是为什么调度器坚持"无状态可预测"——见后文。

两阶段的直觉立住了:**filter 管"能不能",score 管"好不好",先剔除再排序**。下面我们要追问一个更深的问题:这些 filter / score 的具体规则(资源够不够、污点能不能忍、亲和满不满足),在代码里是怎么组织的?为什么不是写死在调度器里、而要做成插件?

---

## 三、scheduling framework:为什么把每个判断做成可插拔插件

到此我们知道了"调度分两阶段"。但 k8s 调度器的设计,还有一笔比"两阶段"更深的精妙——**它把 filter 和 score 阶段的每一个具体规则,都做成了独立的、可插拔的"插件(plugin)"**。这套机制有个正式名字:**scheduling framework(调度框架)**。

在讲它之前,我们先问那个永远的"不这样会怎样"。

### 不这样会怎样:规则写死在调度器里的世界

假设 k8s 没有插件化,所有调度规则都写死在调度器的主流程里——一个巨大的 `if` 链:

```go
// (假设的、写死的调度器,非真实源码)
for _, node := range allNodes {
    if 资源不够(pod, node) { continue }                 // 规则 1
    if 污点不容忍(pod, node) { continue }               // 规则 2
    if 亲和不满足(pod, node) { continue }                // 规则 3
    if 端口冲突(pod, node) { continue }                  // 规则 4
    // ... 还有几十条
    feasible = append(feasible, node)
}
```

这个写死的版本,在四个地方会要命:

- **大厂的需求千差万别,不可能在主线满足**。一个跑 AI 训练的集群,需要"GPU 必须放同一台机器做 NVLink 通信";一个跑金融交易的集群,需要"按地理位置亲和,延迟低于 X 毫秒";一个多租户集群,需要"按 tenant 隔离"。这些规则,**没有一个能写进 k8s 主线**——写进去就侵犯了别的用户。可如果不让写进主线,大厂只能 fork 一份调度器源码自己改——**每次 k8s 升级,都得重新 merge,痛苦不堪**。
- **新规则难以加入**。今天你想加一条"避免把同一服务的副本放同一台机器"的规则(PodTopologySpread),明天想加"按镜像大小优先放有缓存的节点"(ImageLocality)。如果都写死在主流程,调度器的代码会**无限膨胀,变成没人能维护的怪物**。
- **规则的顺序、启用与否无法配置**。有的集群要"先看资源再看污点",有的要"先看污点再看资源"——写死了就一条路,无法配。
- **测试和复用困难**。每条规则,理论上应该能独立测试。揉在一个巨型函数里,改一条规则可能踩到另一条。

> **比喻**:这就像一个港口调度员的脑子里,刻着一套**永远不变的调度口诀**——"先看载重、再看航线、再看温度……"。可现实是,运化学品的有化学品的口诀,运生鲜的有生鲜的口诀,运汽车的有汽车的口诀。**一个聪明的港口,不会把所有口诀都刻进每个调度员的脑子里,而是把每种货的"调度规则手册"做成可替换的小册子——调度员脑子里只有一个"读手册、按手册判断"的通用流程,手册本身随时可换。** scheduling framework,就是 k8s 给调度员准备的"通用流程 + 可换手册"。

### 所以这样设计:把每个判断做成插件,scheduling framework 只负责"跑插件"

k8s 的回答,是定义一套**统一的插件接口**,把所有调度规则都实现成插件。scheduling framework 自己**不含任何业务规则**,它只提供两样东西:

1. **一组扩展点(extension point)**:在调度的整个生命周期里,有几个固定的"钩子位置"可以让插件介入。最重要的两个就是 `Filter`(在 filter 阶段调)和 `Score`(在 score 阶段调),但前后还有一串——`PreFilter`(filter 前的预处理)、`PostFilter`(filter 失败后的兜底,抢占就在这里)、`PreScore`、`Reserve`、`PreBind`、`Bind`(最终把 Pod 绑到节点)等。
2. **一个运行时(runtime)**:负责按顺序调这些插件、聚合它们的结果、传递一个在插件间共享的 `CycleState`。

这两样东西,在源码里是分开的:**接口定义在 `staging/src/k8s.io/kube-scheduler/framework/interface.go`,运行时实现在 `pkg/scheduler/framework/runtime/framework.go`**。先看接口——这是整个插件体系的"宪法":

> **源码位置**:[`staging/src/k8s.io/kube-scheduler/framework/interface.go`](https://github.com/kubernetes/kubernetes/blob/master/staging/src/k8s.io/kube-scheduler/framework/interface.go)。注意路径——**这个文件的权威源在 `staging` 子模块里**(k8s 把可独立发布的子项目放在 `staging/src/k8s.io/` 下,`kube-scheduler` 是其中之一,可以单独 `go get`)。`pkg/scheduler/framework/interface.go` 也存在,但它是 staging 的**镜像**(staging 是上游,构建时同步过去),内容一致——看接口以 staging 版为准。老资料常把权威路径写错或只提 pkg 版,留意。

```go
// (摘自 interface.go,verbatim,简化展示)
// Plugin 是所有插件的父类型,只要求一个 Name() 方法
type Plugin interface {
	Name() string
}

// FilterPlugin —— filter 阶段的插件,旧的调度器里叫 "predicate"
type FilterPlugin interface {
	Plugin
	// Filter 在某个节点上调用,返回 Success 表示这个节点放得下,
	// 否则返回 Unschedulable / UnschedulableAndUnresolvable / Error
	Filter(ctx context.Context, state CycleState, pod *v1.Pod, nodeInfo NodeInfo) *Status
}

// ScorePlugin —— score 阶段的插件,旧的调度器里叫 "priority"
type ScorePlugin interface {
	Plugin
	// Score 在每个通过了 filter 的节点上调用,返回 0~MaxNodeScore(100) 的分
	Score(ctx context.Context, state CycleState, p *v1.Pod, nodeInfo NodeInfo) (int64, *Status)
	// ScoreExtensions 返回一个可选的"分数归一化"接口(NormalizeScore)
	ScoreExtensions() ScoreExtensions
}
```

读这三个接口,你会发现 scheduling framework 的"宪法"朴素到极致:

- **所有插件,只要实现一个 `Name() string`**(告诉框架"我叫什么"),就算一个插件。
- **想当 filter 插件?再加一个 `Filter(ctx, state, pod, nodeInfo) *Status`**——给你一个 Pod 和一个节点,你告诉我这个节点行不行。
- **想当 score 插件?再加一个 `Score(...) (int64, *Status)`**——给我一个分,0~100。

**就这些**。没有继承、没有复杂抽象,就是几个一行半的接口。任何一个 Go 开发者,写一个实现了 `Name()` + `Filter()` 的 struct,就是一个 filter 插件。这是 k8s 调度器扩展性最直接的证据——**改调度行为,不需要改调度器,只要写一个插件**。

> **回扣第 15 章"可替换性"**:第 15 章我们说 scheduler 独立成进程,理由是"调度特别复杂、特别重要、且大家想各搞各的"。这一节的 scheduling framework,就是这个理由的代码化身——k8s 把"调度框架"做成了一个**通用容器**,里面装什么插件由你决定。大厂可以注册自己的 GPU 调度插件、租户隔离插件,而 k8s 主线一个都不用知道。

### 插件长什么样:一个真实的 filter 插件 NodeResourcesFit

光看接口还不够,我们要看一个**真实存在的插件**,理解它怎么把"判断逻辑"实现进 `Filter` 方法。最经典的一个,是 `NodeResourcesFit`——它判断"这个节点的资源够不够放这个 Pod"。看它的 `Filter`:

> **源码位置**:[`pkg/scheduler/framework/plugins/noderesources/fit.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/scheduler/framework/plugins/noderesources/fit.go)。

```go
// (摘自 fit.go,verbatim,简化展示)
func (f *Fit) Filter(ctx context.Context, cycleState fwk.CycleState, pod *v1.Pod, nodeInfo fwk.NodeInfo) *fwk.Status {
	s, err := getPreFilterState(cycleState)
	if err != nil {
		return fwk.AsStatus(err)
	}
	// ... 一点检查 ...

	// fitsRequest 是真正的判断逻辑:对比 Pod 要的资源 vs 节点剩余资源
	insufficientResources := fitsRequest(s, nodeInfo, f.ignoredResources, f.ignoredResourceGroups, draManager, opts)
	if len(insufficientResources) != 0 {
		// 不够,返回 Unschedulable,带一句"为什么不够"的说明
		// ... 构造 failureReasons ...
		return fwk.NewStatus(statusCode, failureReasons...)
	}
	return nil   // 够,返回 Success(nil 表示成功)
}
```

而 `fitsRequest` 这个 helper,干的活儿是**逐项对比** Pod 请求的资源(CPU、memory、ephemeral-storage、各种扩展资源如 GPU)和节点的剩余资源(Allocatable − 已被请求的 Requested):

```go
// (摘自 fit.go,verbatim,简化展示)
func fitsRequest(podRequest *preFilterState, nodeInfo fwk.NodeInfo, ...) []InsufficientResource {
	insufficientResources := make([]InsufficientResource, 0, 4)

	// ① Pod 数量上限
	allowedPodNumber := nodeInfo.GetAllocatable().GetAllowedPodNumber()
	if len(nodeInfo.GetPods())+1 > allowedPodNumber {
		insufficientResources = append(insufficientResources, InsufficientResource{ResourceName: v1.ResourcePods, Reason: "Too many pods", ...})
	}
	// ② CPU:Pod 要的 CPU > (节点可分配 CPU - 已被请求的 CPU)?
	if podRequest.MilliCPU > 0 && podRequest.MilliCPU > (nodeInfo.GetAllocatable().GetMilliCPU()-nodeInfo.GetRequested().GetMilliCPU()) {
		insufficientResources = append(insufficientResources, InsufficientResource{v1.ResourceCPU, "Insufficient cpu", ...})
	}
	// ③ Memory:同理
	if podRequest.Memory > 0 && podRequest.Memory > (nodeInfo.GetAllocatable().GetMemory()-nodeInfo.GetRequested().GetMemory()) { /* ... */ }
	// ④ EphemeralStorage、ScalarResources(GPU 等):同理
	// ...
	return insufficientResources
}
```

读这段,你会看到一个 filter 插件的全部秘密,就藏在那个简单的不等式里:

> **节点能不能放下这个 Pod,看的是:`Pod 要的资源 ≤ 节点的(Allocatable − Requested)`。**

`Allocatable` 是这台机器物理上能给的(扣掉系统预留),`Requested` 是这台机器上**所有已有 Pod 的 requests 之和**(而不是它们实际用的)。这个"按 requests 而非实际用量算剩余"的设计极其重要,后面讲 requests/limits 那一节会专门讲为什么。这里先记住:**`NodeResourcesFit` 插件,就是上面那个不等式的 Go 代码化身**。

### 内置插件都注册了什么:看 registry

那么 k8s 默认装了哪些插件?这写在 `registry.go` 里——一个把所有"出厂自带"的插件注册进框架的表:

> **源码位置**:[`pkg/scheduler/framework/plugins/registry.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/scheduler/framework/plugins/registry.go) 的 `NewInTreeRegistry`。

```go
// (摘自 registry.go,verbatim,简化展示)
func NewInTreeRegistry() runtime.Registry {
	fts := plfeature.NewSchedulerFeaturesFromGates(feature.DefaultFeatureGate)
	registry := runtime.Registry{
		// ... 一长串插件 ...
		tainttoleration.Name:                 runtime.FactoryAdapter(fts, tainttoleration.New),
		nodename.Name:                        nodename.New,
		nodeports.Name:                       runtime.FactoryAdapter(fts, nodeports.New),
		nodeaffinity.Name:                    runtime.FactoryAdapter(fts, nodeaffinity.New),
		podtopologyspread.Name:               runtime.FactoryAdapter(fts, podtopologyspread.New),
		nodeunschedulable.Name:               runtime.FactoryAdapter(fts, nodeunschedulable.New),
		noderesources.Name:                   runtime.FactoryAdapter(fts, noderesources.NewFit),               // NodeResourcesFit
		noderesources.BalancedAllocationName: runtime.FactoryAdapter(fts, noderesources.NewBalancedAllocation), // BalancedAllocation
		interpodaffinity.Name:                runtime.FactoryAdapter(fts, interpodaffinity.New),
		defaultbinder.Name:                   defaultbinder.New,
		defaultpreemption.Name:               runtime.FactoryAdapter(fts, defaultpreemption.New),               // 抢占
		// ...
	}
	return registry
}
```

这张表,是 k8s 调度器"出厂自带的插件清单"。它清楚地告诉我们,**k8s 默认的调度策略,本质就是这一堆插件的组合**:

- **`NodeResourcesFit`**(filter):资源够不够放。
- **`nodeports`**(filter):Pod 要的端口在这个节点上有没有冲突。
- **`nodeaffinity`**(filter + score):节点 label 满不满足 Pod 声明的硬性/偏好要求。
- **`tainttoleration`**(filter + score):节点的污点,Pod 能不能忍。
- **`interpodaffinity`**(filter + score):Pod 和别的 Pod 之间的亲和/反亲和。
- **`podtopologyspread`**(filter + score):把同一组 Pod 尽量打散到不同拓扑域(机架、可用区)。
- **`NodeResourcesBalancedAllocation`**(score):让一个节点上 CPU 和内存的占用比例尽量平衡(下面讲)。
- **`defaultpreemption`**(PostFilter):filter 全军覆没时,尝试抢占低优先级 Pod 腾地方。

> **一个老资料常翻车的细节**:`NodeResourcesLeastAllocated`(最少分配)和 `NodeResourcesMostAllocated`(最多分配)**不是一个独立插件**,而是 `NodeResourcesFit` 这个插件的两种**打分策略(scoring strategy)**,通过调度器配置里的 `scoringStrategy` 字段选。老资料和不少博客把 LeastAllocated 当成一个独立 Score 插件来讲,这是错的——它在 master 上是 `NodeResourcesFit` 的内部策略,只注册了 `noderesources.Name`(Fit)和 `noderesources.BalancedAllocationName`(BalancedAllocation)这两个名字。同理,老资料讲的 `volumescheduling` 插件**已经不存在**,master 上拆成了 `volumebinding` + `volumerestrictions` + `volumezone` + `nodevolumelimits` 四个。

scheduling framework 立住了:**接口定义统一、插件独立可换、框架本身不带业务规则**。下面我们花三节的篇幅,把这套框架上跑的几组关键调度维度,一个一个讲清"它们解决什么不解决就不行的问题"。

---

## 四、调度维度之一:requests / limits,为什么调度看的是 requests 而不是实际用量

第一个要讲清的,是最基础的资源维度:**Pod 的 requests 和 limits**。这两个字段你写 YAML 时一定写过:

```yaml
resources:
  requests:
    cpu: "500m"      # 0.5 个 CPU
    memory: "256Mi"
  limits:
    cpu: "1"         # 1 个 CPU
    memory: "512Mi"
```

很多人对这两个字段的印象停留在"requests 是保底,limits 是上限"。这个理解对,但远远不够。我们要追问的是:**调度器在算"节点能不能放下这个 Pod"时,用的是 requests 还是 limits?为什么?**

### 不这样会怎样:按 limits 调度,或按实际用量调度的灾难

假设调度器按 **limits** 算。一个 Pod 声明 `limits.cpu: 1`,意思是"我最多用 1 个 CPU"。可它平时只跑 0.1 个 CPU。如果你按 limits 算,调度器会认为这个 Pod 占了 1 个 CPU,于是只敢在一台 4 核机器上放 4 个这样的 Pod——**机器 90% 的 CPU 闲置,资源利用率极低**。这和虚拟机时代"每个 VM 预留一堆资源不用"的浪费,毫无二致。

再假设调度器按**实际用量**算。听起来很聪明——"它实际用 0.1 个,我就当它占 0.1 个"。可问题是:**实际用量是波动的**。凌晨没流量时这个 Pod 几乎不耗 CPU,调度器看它占用很低,就往这台机器上又塞了 20 个 Pod;到了白天高峰,所有 Pod 同时想用满 CPU——**这台机器瞬间被压垮,所有 Pod 都开始抢 CPU,延迟飙升**。这种"调度时宽松、运行时打架"的状况,会让集群在流量峰值时**集体抖动**。更致命的是,实际用量是**事后**才知道的,调度器在调度那个瞬间根本没法预测这个 Pod 未来会用到多少。

### 所以这样设计:按 requests 调度,按 limits 限流

k8s 的设计,是极其克制地把"调度"和"限流"**分开**:

- **调度**按 **requests** 算——你声明"我至少要 0.5 个 CPU",调度器就**当真**,给你在这台机器上预留 0.5 个 CPU(在它的账本里)。回看上一节 `NodeResourcesFit` 的 `fitsRequest`:它对比的正是 `nodeInfo.GetRequested()`——**所有已有 Pod 的 requests 之和**。**requests 是给调度器看的。**
- **运行时限流**按 **limits** 算——你声明"我最多用 1 个 CPU",kubelet(通过 cgroup)就**真给你卡住**,你超出 1 个 CPU 就被 throttle。**limits 是给 cgroup 看的,不是给调度器看的。**

这两件事分开,带来一个极其优雅的性质:**调度器只信 requests,但集群不会因为"按 requests 预留"而浪费**。因为:

- 一台 4 核机器,可以放 8 个 requests.cpu=0.5 的 Pod(8 × 0.5 = 4),而每个 Pod 的 limits 可以设到 1。**8 个 Pod 平时各用 0.1,机器只用 0.8 个 CPU;但任何一个 Pod 突然需要冲刺,可以冲到 limits 允许的 1 个 CPU**——只要机器总量够,大家相安无事。这就是 **overcommit(超卖)**:requests 之和可以小于 limits 之和,允许"平时都低调、偶尔个别冲刺"。
- 调度器只认 requests,所以**它永远不会把一个机器塞到"按 requests 算放不下"的程度**——也就是保证了"每个 Pod 至少有它声明的那份"。同时 limits 由 cgroup 兜底,保证"再怎么冲刺,也超不过 limits"。

> **比喻**:这就像航运的"配载申报"。每箱货都报一个**保底载重**(requests:"我至少占 500 公斤,你得给我预留")和一个**最大载重**(limits:"我最重不会超过 1000 公斤")。调度员**按保底载重**算船能不能装(8 箱 × 500 = 4 吨,装得下),不按最大载重算(否则 8 箱 × 1000 = 8 吨,船装不下,可实际它们平均才 100 公斤,纯属浪费船舱)。但船长装船时,会**按最大载重**给每箱货预留固定钩位(防止万一某箱真的满了把别的箱挤飞)。**申报(调度)看保底,固定(限流)看最大,两件事分开**。

`NodeResourcesFit` 算的是 requests;limits 由 kubelet 进 cgroup(回扣第 3 章 cgroup 章)。这是 k8s 资源管理的根基。**理解了"调度看 requests",你才能理解为什么 Pod 的 requests 一定要认真填——填小了(实际用量大)Pod 会因为节点超卖被打压,填大了节点塞不下,填 0 则调度器根本不管你这个维度**。

---

## 五、调度维度之二:亲和性与反亲和性,让 Pod 主动选船或躲船

`NodeResourcesFit` 解决了"放得下放不下",但集群里有大量比"资源"更复杂的偏好:**这个 Pod 想和谁靠近(亲和),想躲开谁(反亲和),只想上哪种船(nodeAffinity)**。这些是亲和/反亲和维度要解决的。

### nodeAffinity:Pod 选节点(label 的硬/软约束)

**nodeAffinity** 解决的是:"我这个 Pod,只能/更想放在满足某种 label 的节点上"。比如:

- 硬约束(`requiredDuringScheduling`):"只能放在 `disktype=ssd` 的节点上"。
- 软偏好(`preferredDuringScheduling`):"更想放在 `zone=east` 的节点上,放不下也行"。

> **比喻**:这箱货上面贴了一张**选船条子**——"必须上 SSD 船"(硬),"最好上东区船"(软)。调度员 filter 阶段看硬条子(非 SSD 船直接划掉),score 阶段给满足软条子的船加分。

它的实现就是 `nodeaffinity` 插件,filter 阶段调 `Filter`(检查 `requiredDuringScheduling`),score 阶段调 `Score`(给 `preferred` 项的分加权重)。看它的 `Filter`:

> **源码位置**:[`pkg/scheduler/framework/plugins/nodeaffinity/node_affinity.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/scheduler/framework/plugins/nodeaffinity/node_affinity.go)。

```go
// (摘自 node_affinity.go,verbatim,简化展示)
func (pl *NodeAffinity) Filter(ctx context.Context, state fwk.CycleState, pod *v1.Pod, nodeInfo fwk.NodeInfo) *fwk.Status {
	node := nodeInfo.Node()
	// ... enforcedNodeSelector 检查 ...
	s, err := getPreFilterState(state)
	if err != nil {
		s = &preFilterState{requiredNodeSelectorAndAffinity: nodeaffinity.GetRequiredNodeAffinity(pod)}
	}
	match, _ := s.requiredNodeSelectorAndAffinity.Match(node)
	if !match {
		return fwk.NewStatus(fwk.UnschedulableAndUnresolvable, ErrReasonPod)   // 不匹配,出局
	}
	return nil
}
```

`nodeaffinity.GetRequiredNodeAffinity(pod)` 把 Pod 声明的硬约束取出来,`.Match(node)` 判断这个节点满不满足。不满足,返回 `UnschedulableAndUnresolvable`(`AndUnresolvable` 是个后缀,意思是"即使抢占也解决不了,这个节点永远放不下")——出局。

> **老 nodeSelector 在哪**:你可能见过老的 `nodeSelector: {disktype: ssd}` 语法(简化的硬约束)。k8s 保留它向后兼容,但新的、功能更全的是 `nodeAffinity`。两者底层都走 `nodeaffinity` 插件。

### podAffinity / podAntiAffinity:Pod 选/躲别的 Pod

**podAffinity / podAntiAffinity** 解决的是更微妙的:"我这个 Pod,想和某些别的 Pod 在一起(或不想在一起)"。比如:

- **podAffinity**:"我这个 web Pod,想和 redis Pod 放同一个节点(或同一个可用区)——网络延迟低"。
- **podAntiAffinity**:"我的 3 个 web 副本,**绝对不能**全放在同一个节点——一个节点挂了不能全军覆没"(高可命的根基)。

> **比喻**:这箱货贴了一张**关系条**——"必须和我配套的那批零件货同船"(亲和),"绝不能和另一批易燃货同船"(反亲和)。调度员翻遍船队名册,看哪些船上已经有这些相关货物,据此决定能不能放。

这是高可用部署最常用的维度。一个生产 web 服务,Deployment 写 3 个副本 + 一条 `podAntiAffinity: 同一节点最多放 1 个`,k8s 就会自动把 3 个副本分散到 3 个不同节点——**任何一台挂了,至少还有 2 个副本活着**。这是"用调度器表达高可用意图"的典型用法。

它的实现是 `interpodaffinity` 插件:

> **源码位置**:[`pkg/scheduler/framework/plugins/interpodaffinity/filtering.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/scheduler/framework/plugins/interpodaffinity/filtering.go)。

```go
// (摘自 filtering.go,verbatim,简化展示)
func (pl *InterPodAffinity) Filter(ctx context.Context, cycleState fwk.CycleState, pod *v1.Pod, nodeInfo fwk.NodeInfo) *fwk.Status {
	state, err := getPreFilterState(cycleState)
	if err != nil {
		return fwk.AsStatus(err)
	}
	if !satisfyPodAffinity(state, nodeInfo) {
		return fwk.NewStatus(fwk.UnschedulableAndUnresolvable, ErrReasonAffinityRulesNotMatch)
	}
	if !satisfyPodAntiAffinity(state, nodeInfo) {
		return fwk.NewStatus(fwk.Unschedulable, ErrReasonAntiAffinityRulesNotMatch)
	}
	if !satisfyExistingPodsAntiAffinity(state, nodeInfo) {
		return fwk.NewStatus(fwk.Unschedulable, ErrReasonExistingAntiAffinityRulesNotMatch)
	}
	return nil
}
```

读这段,注意三个 `satisfy*` 函数和一个微妙的区别:

- **亲和不满足**(`satisfyPodAffinity` 返回 false),返回 `UnschedulableAndUnresolvable`——这个节点**永久放不下**(即使抢占也救不了,因为问题是"没满足亲和的 Pod 在这",而不是"资源不够")。
- **反亲和不满足**,返回的是 `Unschedulable`(不带 `AndUnresolvable`)——意思是"现在放不下,但**抢占可能救得了**":如果这个节点上有一个低优先级的、和本 Pod 反亲和的 Pod,抢占它就能腾出位置。

这种**返回码的精细区分**(`Unschedulable` vs `UnschedulableAndUnresolvable`),是抢占逻辑能否工作的关键——抢占只对"非 `AndUnresolvable`"的失败有意义。这是 master 上一个不显眼但很重要的设计点。

---

## 六、调度维度之三:污点与容忍——"这艘船标了只接冷藏货"

第三组维度,是 **taint / toleration**(污点 / 容忍)。这组维度的设计哲学和前面的"Pod 选节点"完全反过来——**它让节点主动声明"我不想接什么货",而不是 Pod 选节点**。

### taint / toleration 解决什么:让节点拒绝不想要的 Pod

想象这些场景:

- 这台机器是 GPU 机器,我**只想**让需要 GPU 的 Pod 上来,别的普通 Pod 别来凑热闹占着 GPU 机器的普通资源。
- 这台机器正在维护(马上要重启),**不想**接任何新 Pod——免得它们刚起来就被我重启打掉。
- 这台机器有点问题(网络抖、磁盘慢),**暂时不想**接新 Pod,等修好再说。

这些场景的共同点是:**节点想主动声明"别往我这塞货"**。光靠 nodeAffinity 不行——nodeAffinity 是 Pod 选节点,普通 Pod 根本不知道自己"不该"上这台 GPU 机器,照样可能被调度上去(只要它没声明 `nodeAffinity`,默认就是"哪都行")。

### 所以这样设计:节点打 taint,Pod 写 toleration

k8s 的设计是**反过来的**:节点给打上一个**污点(taint)**,声明"我不接某些 Pod";Pod 要上这种节点,得在自己的 YAML 里**显式写容忍(toleration)**,表示"我知道这种污点,我能忍"。

```bash
# 给节点打个污点:"专用 GPU 机器,不容忍就走"
kubectl taint nodes gpu-node-1 dedicated=gpu:NoSchedule
```

```yaml
# 只有需要 GPU 的 Pod,才写对应的 toleration
tolerations:
- key: "dedicated"
  operator: "Equal"
  value: "gpu"
  effect: "NoSchedule"
```

- **`NoSchedule`** 效果:调度器**硬拒绝**——没有对应 toleration 的 Pod,filter 阶段直接出局,绝不上这艘船。
- **`PreferNoSchedule`** 效果:**软拒绝**——没 toleration 不会硬出局,但 score 阶段会被扣分(尽量避开,实在没别的地方才放)。
- **`NoExecute`** 效果:更狠——不仅新 Pod 不能调度上来,**已经在跑的、没有 toleration 的 Pod 会被驱逐**(kubelet 把它们踢走)。用于"这台机器要维护了,把上面的货都搬走"。

> **比喻**:这艘船的桅杆上挂了一面**告示旗**——"本船只接冷藏货"(`NoSchedule`)。调度员 filter 阶段看到这面旗,如果你的运单上没盖"我能上冷藏船"的章(toleration),直接把这艘船划掉——你的货别想上。还有更狠的旗——"本船马上要进坞维修,所有在船的货立刻下船"(`NoExecute`),船长(kubelet)会把已经在船上的、没盖章的货全部赶下船。

### 实现:tainttoleration 插件,Filter 算硬、Score 算软

它的实现是 `tainttoleration` 插件,filter 阶段调 `Filter`(看 `NoSchedule` 污点),score 阶段调 `Score`(看 `PreferNoSchedule` 污点)。看 filter:

> **源码位置**:[`pkg/scheduler/framework/plugins/tainttoleration/taint_toleration.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/scheduler/framework/plugins/tainttoleration/taint_toleration.go)。

```go
// (摘自 taint_toleration.go,verbatim,简化展示)
func (pl *TaintToleration) Filter(ctx context.Context, state fwk.CycleState, pod *v1.Pod, nodeInfo fwk.NodeInfo) *fwk.Status {
	logger := klog.FromContext(ctx)
	node := nodeInfo.Node()
	// 找出这个节点上 Pod 不容忍的、NoSchedule 效果的污点
	taint, isUntolerated := v1helper.FindMatchingUntoleratedTaint(logger, node.Spec.Taints, pod.Spec.Tolerations,
		helper.DoNotScheduleTaintsFilterFunc(),
		pl.enableTaintTolerationComparisonOperators)   // ← 注意 master 多了这个第 5 参数(特性开关)
	if !isUntolerated {
		return nil   // 全部污点都能忍,放行
	}
	logger.V(4).Info("node had untolerated taints", "node", klog.KObj(node), "pod", klog.KObj(pod), "untoleratedTaint", taint)
	return fwk.NewStatus(fwk.UnschedulableAndUnresolvable, "node(s) had untolerated taint(s)")
}
```

读这段,关键就一行:`FindMatchingUntoleratedTaint(node.Spec.Taints, pod.Spec.Tolerations, ...)`——拿节点的污点列表,和 Pod 的容忍列表比对,**找出 Pod 不容忍的那个**。找到了,返回 `UnschedulableAndUnresolvable`(注意这个返回码——和亲和一样,抢占救不了,因为这个节点"逻辑上"就不该放这个 Pod,不是"资源不够")。

> **master 上一个不显眼的变化**:`FindMatchingUntoleratedTaint` 这个 helper,**老的资料讲它是 4 个参数**,master 上已经是 **5 个**——多了一个 `enableTaintTolerationComparisonOperators bool`(让 taint 比较支持更复杂的操作符,如 `GreaterThan`/`LessThan` 用于数值型污点)。这是 2024-2025 之间加的特性。你看老博客时遇到"4 参数",知道这是过时的。

### taint 和 affinity 的对偶关系

讲到这里,你应该感受到一个**对偶**:

| 维度 | 谁主动 | 解决的问题 |
|------|--------|-----------|
| **nodeAffinity** | **Pod 选节点** | "我这个 Pod 想去某种节点" |
| **taint/toleration** | **节点拒 Pod** | "我这个节点不想接某种 Pod" |
| **podAffinity** | **Pod 选 Pod** | "我这个 Pod 想和某些 Pod 在一起" |
| **podAntiAffinity** | **Pod 躲 Pod** | "我这个 Pod 想躲开某些 Pod" |

这四者**两两对偶**,合起来覆盖了"Pod 和节点、Pod 和 Pod 之间所有想靠近/想躲开"的需求。**k8s 调度的"表达力",就是靠这四个维度撑起来的**。每一个,都是一类真实部署场景的化身——GPU 专用、高可用分散、主从同节点、跨可用区容灾。

---

## 七、调度维度之四:优先级与抢占——货等船,还是船让货?

最后一组维度,是 **priority / preemption**(优先级 / 抢占)。它回答一个更尖锐的问题:**当一个高优先级的 Pod 来了,但集群所有节点都满了,该谁让位?**

### 不这样会怎样:没有优先级的世界

假设集群里所有 Pod 优先级都一样。这时来一个**至关重要**的核心服务 Pod(比如支付网关),可所有节点资源都满了——它只能去 `unschedulableQ` 排队等。等到什么时候?等到某个低优先级的、跑测试任务的 Pod 自己退出。**关键业务被不重要业务卡住,这在生产环境是不可接受的**。

更糟的是,这种"无优先级"会让集群在压力下**全盘恶化**——所有 Pod 一视同仁,谁也挤不动谁,关键服务和次要任务一起卡。

### 所以这样设计:每个 Pod 有优先级,高优先级可以踢低优先级

k8s 的设计:

- 每个 Pod 在创建时,**必须**有一个 `priorityClassName`,对应一个 `PriorityClass`(0~10 亿的整数,越大越优先)。
- 当一个高优先级 Pod 调度时 filter 全军覆没(所有节点都放不下),调度器不会直接把它扔进 unschedulableQ——它会**触发抢占**:
  - 在每个节点上,**尝试踢掉一些低优先级 Pod**(所谓"牺牲品 victims"),看看踢完之后这个高优先级 Pod 能不能放下。
  - 如果能,选一个"代价最小"的节点(踢的 Pod 数最少、优先级最低、且不违反 PDB——PodDisruptionBudget),执行抢占:把那些低优先级 Pod **驱逐**,高优先级 Pod 占位。
  - 被踢的 Pod 进入 `Pending` 状态重新排队,等调度器给它们找新家(可能在别的节点,也可能触发下一轮抢占)。

> **比喻**:这就像航运里的"优先货等船"。一船关键军火来了,所有码头都满了——可这船不能等。调度员会**强制卸下**几船低优先级的货(比如化肥、日用品),腾出船位给军火。被卸下的货重新进待派单队列,等下一次有空船再装。**优先级高的货,可以"挤掉"优先级低的货**——这是调度器在资源紧张时维持关键服务的最后一招。

### 实现抢占:PostFilter 插件 defaultpreemption

抢占**不是 filter、不是 score**,它是第三个扩展点——`PostFilter`(filter 之后,filter 失败时调)。它的实现是 `defaultpreemption` 插件:

> **源码位置**:[`pkg/scheduler/framework/plugins/defaultpreemption/default_preemption.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/scheduler/framework/plugins/defaultpreemption/default_preemption.go)。

```go
// (摘自 default_preemption.go,verbatim,简化展示)
var _ fwk.PostFilterPlugin = &DefaultPreemption{}    // 它实现 PostFilterPlugin 接口

func (pl *DefaultPreemption) PostFilter(ctx context.Context, state fwk.CycleState, pod *v1.Pod, m fwk.NodeToStatusReader) (*fwk.PostFilterResult, *fwk.Status) {
	defer func() { metrics.PreemptionAttempts.Inc() }()
	// ... workload-aware 抢占的特殊判断 ...
	// 真正的抢占逻辑委托给 Evaluator
	result, status := pl.Evaluator.Preempt(ctx, state, pod, m)
	msg := status.Message()
	if len(msg) > 0 {
		return result, fwk.NewStatus(status.Code(), "preemption: "+msg)
	}
	return result, status
}
```

读到 `PostFilter`,你就明白抢占的位置了——**它在 filter 全军覆没之后才被调**。`PostFilter` 拿到一个 `NodeToStatusReader`(记录了每个节点为什么 filter 失败),在里面找"踢掉某些 Pod 后能不能让本 Pod 通过 filter"的节点。

`pl.Evaluator.Preempt` 内部的核心,是一个叫 `SelectVictimsOnNode` 的过程:在某个节点上,先**把所有比本 Pod 优先级低的、可抢占的 Pod 全部假设移除**,然后跑 filter 看本 Pod 能不能放下;能的话,再**按优先级从低到高、尽量少踢**地往回加(能不踢就不踢),最终确定 victims 列表。

> **PDB(PodDisruptionBudget)的兜底**:抢占不是无脑踢低优先级 Pod。k8s 有一个 `PodDisruptionBudget` 资源,声明"某类 Pod 至少要保持 N 个可用"。抢占在选 victims 时,**不会踢到违反 PDB 的程度**——比如一个 Deployment 设了 PDB "至少 3 个可用",现在有 4 个,抢占最多踢 1 个,不会把可用数打到 2。**这是防止"抢占把某个服务的副本全踢光,导致那个服务自己也挂了"的护栏**。

优先级和抢占,是 k8s 在资源紧张时**维持关键服务**的最后一招。它让集群管理员可以明确表达"哪些货是命脉、哪些货可以让位",调度器在压力下自动执行这种"让位"。

---

## 八、为什么调度器坚持无状态、可预测

讲完四个维度,我们要回答本章最后一个"为什么":**为什么 k8s 的调度器刻意保持"无状态、可预测"——同样的集群快照、同一个 Pod,永远应该被调度到同一个节点?**

### 不这样会怎样:有状态、不可预测的调度灾难

假设调度器是有状态的——它记着"我上次把 web-1 调度到了 node-3",或者用了一个带随机性的算法(每次调度结果可能不同)。这种"有状态/不可预测"会在三个地方要命:

- **调试是噩梦**。一个 Pod 被调度到了一个奇怪的节点(比如放到了资源快满的节点导致 OOM),你复盘时问"它为什么被调度到这?调试器告诉你"因为上次状态 + 随机性"。**你根本没法重现这个问题**——下次同样的 Pod 进来,可能去了完全不同的节点。问题查不清,就只能"再观察观察",直到下一次同样的故障再随机发生。
- **行为不可推理**。运维想给集群做容量规划,问"如果再多 100 个 web Pod,它们会去哪?"。如果调度器不可预测,这个问题没法回答——它们可能去任何地方。**整个集群的行为变成黑盒,任何规划都建立在沙子上**。
- **扩展插件难写**。你想写一个自定义插件,可调度器有状态——你的插件每次跑,上下文都不一样,根本没法独立测试。**插件化的好处(可独立开发、可测试),被"有状态"一笔抹杀**。

### 所以这样设计:调度器无状态,只依赖输入

k8s 调度器刻意做成**无状态**:

- 它**不存**"上一次调度到哪"这种历史。每一次 `ScheduleOne`,都是从当前的集群快照(nodeInfoSnapshot)+ 当前 Pod 出发,**纯粹根据输入算输出**。
- 它**不引入随机性**。打分时,如果两个节点分数相同,**默认按节点名字典序**选(在 `selectHost` / `sortedNodeScores.Pop` 里),保证结果确定。
- 插件也被要求**无副作用、纯函数**——同样的 `(pod, nodeInfo)` 输入,`Filter` / `Score` 必须返回同样的结果。

> **唯一需要状态的地方,用 CycleState 显式传递**。同一次 `ScheduleOne` 内,filter 和 score 阶段可能需要共享一些中间计算(比如 `NodeResourcesFit` 的 PreFilter 算好的 Pod 资源请求)。这个"同一次调度内的状态",用一个叫 [`CycleState`](https://github.com/kubernetes/kubernetes/blob/master/pkg/scheduler/framework/cycle_state.go) 的对象显式传递——它只在一次 `ScheduleOne` 内有效,调度完就丢。**注意它不是跨调度的状态,而是同一次调度内、跨插件的临时状态**。这把"必须的状态"严格限制在最小范围。

> **master 上的一个变化**:老资料讲 `CycleState` 时,经常提到 `Lock()` / `RLock()` 方法(它内部有个 `sync.RWMutex`)。**master 上这个 RWMutex 已经被换成 `sync.Map`**——`CycleState` 的存储字段是 `storage sync.Map`,并发安全靠它,**没有 `Lock()` 方法了**。如果你看老书看到 `state.Lock()`,知道这是过时的。

### 可预测的代价:race condition 与 assume 机制

无状态、可预测这么好,代价是什么?代价是 **race condition(竞态)**:

- 调度器在算"节点 A 能不能放下 Pod X"时,用的是**快照**——某个时间点的集群状态。可在它算的同时,**别的调度器实例**(如果你部署了多个 scheduler,虽然生产一般单实例)、或者**别的 controller** 可能正在改集群状态(比如又来了一个 Pod Y,也被算到节点 A)。
- 如果调度器算完、写回"Pod X → 节点 A"之后,才发现节点 A 其实已经被别的 Pod 占满了,那 Pod X 到了节点 A 会启动失败。

k8s 怎么处理这个?它用了一个叫 **assume(假设)** 的机制:

- 调度器算出"Pod X → 节点 A"后,**立刻在它自己的本地缓存里把这个 Pod "假设"放上去**(`sched.Cache.AssumePod(...)`)——这样它下一次算别的 Pod 时,会把这个"假设已调度"的 Pod 也算进节点 A 的占用。
- 然后**异步地**把绑定写回 apiserver(`bind` 阶段)。apiserver 写成功后,这个 assume 才变成"真的"。
- 如果异步 bind 失败(罕见),调度器把 assume 回滚。

> **这个 assume 机制,是"无状态可预测"和"避免竞态"之间的妥协**。调度器仍然不存"跨调度的持久状态"(assume 只是个临时的本地缓存,可以被重建),但通过"算完立刻假设占位 + 异步确认",把竞态的窗口缩到最小。这是分布式系统里"乐观并发控制"的经典套路——**先乐观假设不冲突,出问题再回滚**。

无状态可预测,是 k8s 调度器**最容易被忽略、却最值钱**的设计原则之一。它让调度行为变得**可推理、可调试、可测试**——这是任何分布式系统能在生产环境长期稳定运行的根基。**理解了这一点,你才理解为什么调度器的所有复杂逻辑(filter、score、抢占)都被严格限制成"纯函数"——它们必须可预测,这是设计宪法**。

---

## 关键源码精读:从 ScheduleOne 到一次完整调度

理论讲完了,我们钻进源码,亲眼看一次完整调度是怎么走的。主角是 `pkg/scheduler/schedule_one.go` 的 `ScheduleOne` 和它调用的 `schedulePod`。

> **重要前提**:k8s 调度器源码近年**大改过**。如果你看过 2020-2022 年的资料或书,会反复看到一个文件 `pkg/scheduler/core/generic_scheduler.go`,里面有一个 `genericScheduler` struct,它的 `Schedule` 方法、`FindNodesThatFit`、`PrioritizeNodes` 三个函数是讲调度的标配。
>
> **这三个函数名,在 master 上全部不存在了。** 我用 GitHub Contents API 实际拉取了 `pkg/scheduler/` 的目录列表——**没有 `core/` 子目录**,`generic_scheduler.go` 这个文件**根本不存在**。整个"两阶段通用调度器"被重构进了 `pkg/scheduler/schedule_one.go`(主流程)+ `pkg/scheduler/framework/runtime/framework.go`(插件运行时)。下面我们看 master 上的真实函数名。

### 一、入口:`ScheduleOne`,一个调度器主循环的轮廓

`ScheduleOne` 是调度器的主循环体,被 `Run` 死循环调用。看它的真身:

> **源码位置**:[`pkg/scheduler/schedule_one.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/scheduler/schedule_one.go) 的 `ScheduleOne`。

```go
// ScheduleOne does the entire scheduling workflow for a single scheduling entity
// (either a pod or a pod group).
// It is serialized on the scheduling algorithm's host fitting.
func (sched *Scheduler) ScheduleOne(ctx context.Context) {
	logger := klog.FromContext(ctx)
	entity, err := sched.NextEntity(logger)   // 从 SchedulingQueue 弹一个待调度实体
	if err != nil {
		utilruntime.HandleErrorWithLogger(logger, err, "Error while retrieving next scheduling entity from scheduling queue")
		return
	}
	if entity == nil {
		return
	}

	switch specificEntity := entity.(type) {
	case *framework.QueuedPodGroupInfo:
		sched.scheduleOnePodGroup(ctx, specificEntity)   // PodGroup(批量调度)
	case *framework.QueuedPodInfo:
		if specificEntity.Pod == nil {
			return
		}
		sched.scheduleOnePod(ctx, specificEntity)        // 单个 Pod —— 本章主角
	default:
		utilruntime.HandleErrorWithLogger(logger, nil, "Unexpected entity", "type", fmt.Sprintf("%T", specificEntity))
	}
}
```

读这段,几个关键点:

- **`sched.NextEntity(logger)`**:从 `SchedulingQueue` 弹一个待调度的"实体"。注意名字是 `NextEntity` 不是 `NextPod`——master 上已经支持 **PodGroup**(一组 Pod 一起调度,用于 AI/批处理场景)。老资料讲 `NextPod`,过时了。普通场景下,弹出来的是一个 `*framework.QueuedPodInfo`,内含一个 Pod。
- **`switch` 分发**:本章我们盯 `scheduleOnePod` 这个分支(普通 Pod 的调度)。PodGroup 是更新的特性,本章不展开。
- 注释里那句 *"It is serialized on the scheduling algorithm's host fitting"* 很关键:**整个 filter 阶段是串行的**——一次只调一个 Pod,不在多个 Pod 之间并行 filter。这是为了避免前面说的 race condition(同时算多个 Pod 会撞车)。

### 二、`schedulePod`:两阶段的真实结构

`scheduleOnePod` 内部,会调一个核心方法 `schedulePod`(注意,这个是 `*Scheduler` 的方法,不是包级函数)。这就是"两阶段"的源码化身:

> **源码位置**:[`pkg/scheduler/schedule_one.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/scheduler/schedule_one.go) 的 `schedulePod`(方法,在 `*Scheduler` 上)。

```go
// schedulePod tries to schedule the given pod to one of the nodes in the node list.
// If it succeeds, it will return the name of the node.
// If it fails, it will return a FitError with reasons.
func (sched *Scheduler) schedulePod(ctx context.Context, fwk framework.Framework, state fwk.CycleState, podInfo *framework.QueuedPodInfo) (result ScheduleResult, err error) {
	pod := podInfo.Pod
	// ...

	if sched.nodeInfoSnapshot.NumNodesInPlacement() == 0 {
		return result, ErrNoNodesAvailable
	}

	// === 阶段一:FILTER ===
	feasibleNodes, diagnosis, nodeHint, err := sched.findNodesThatFitPod(ctx, fwk, state, podInfo)
	if err != nil {
		return result, err
	}
	trace.Step("Computing predicates done")

	if len(feasibleNodes) == 0 {
		return result, &framework.FitError{     // filter 全军覆没,返回 FitError(会触发 PostFilter 抢占)
			Pod:         pod,
			NumAllNodes: sched.nodeInfoSnapshot.NumNodesInPlacement(),
			Diagnosis:   diagnosis,
		}
	}

	// 只有一个可行节点,直接用,跳过 score
	if len(feasibleNodes) == 1 {
		node := feasibleNodes[0].Node().Name
		// ...
		return ScheduleResult{
			SuggestedHost:  node,
			EvaluatedNodes: 1 + diagnosis.NodeToStatus.Len(),
			FeasibleNodes:  1,
		}, nil
	}

	// === 阶段二:SCORE ===
	priorityList, err := prioritizeNodes(ctx, sched.Extenders, fwk, state, pod, feasibleNodes)
	if err != nil {
		return result, err
	}

	sortedPrioritizedNodes := newSortedNodeScores(priorityList)
	node := sortedPrioritizedNodes.Pop()        // 选分最高的
	trace.Step("Prioritizing done")

	// ...
	return ScheduleResult{
		SuggestedHost:  node,
		EvaluatedNodes: len(feasibleNodes) + diagnosis.NodeToStatus.Len(),
		FeasibleNodes:  len(feasibleNodes),
	}, err
}
```

读这段,两阶段的结构赤裸裸地摆出来:

1. **`sched.findNodesThatFitPod(...)`** —— filter 阶段。返回 `feasibleNodes`(可行节点列表)+ `diagnosis`(每个被淘汰节点是被哪个插件淘汰的,用于 FitError 报告)。**这就是"红笔划船"的代码化身**。
2. **`if len(feasibleNodes) == 0`** —— filter 全军覆没,返回 `FitError`。这个 `FitError` 在外层会触发 **PostFilter**(抢占)。
3. **`if len(feasibleNodes) == 1`** —— 只有一个可行节点,跳过 score 直接选(优化,省得算一遍打分)。
4. **`prioritizeNodes(...)`** —— score 阶段。这是个**包级函数**(小写 p),不是方法。返回 `priorityList`(每个节点的加权总分)。
5. **`sortedPrioritizedNodes.Pop()`** —— 在按分排序的堆上 Pop 一个,就是分最高的那个节点。**这就是"选分最高的船"的代码化身**。

> **修正老资料印象**:`findNodesThatFitPod` 这个方法名,有个 `Pod` 后缀;老资料常写的 `FindNodesThatFit`(没后缀、首字母大写)**在 master 上不存在**——它是个**非导出方法**(小写 f,在 `*Scheduler` 上)。同理 `PrioritizeNodes`(大写 P),master 上是**包级函数 `prioritizeNodes`**(小写 p)。`genericScheduler` struct 彻底没了。**你看老书遇到这三个大写名字,统统替换成 master 的小写真实名字**。

### 三、filter 阶段怎么跑插件:`RunFilterPlugins`

`findNodesThatFitPod` 内部,会先调 `RunPreFilterPlugins`(filter 前的预处理,比如 `NodeResourcesFit` 的 PreFilter 算好 Pod 的资源请求),然后调 `findNodesThatPassFilters`,后者对每个节点调 `RunFilterPluginsWithNominatedPods`(带"已被 nominated 的 Pod"的 filter 变体,用于抢占场景)。底层的 `RunFilterPlugins` 在 framework runtime 里:

> **源码位置**:[`pkg/scheduler/framework/runtime/framework.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/scheduler/framework/runtime/framework.go) 的 `RunFilterPlugins`。

```go
// RunFilterPlugins runs the set of configured Filter plugins for pod on the given node.
func (f *frameworkImpl) RunFilterPlugins(
	ctx context.Context,
	state fwk.CycleState,
	pod *v1.Pod,
	nodeInfo fwk.NodeInfo,
) *fwk.Status {
	logger := klog.FromContext(ctx)
	verboseLogs := logger.V(4).Enabled()
	if verboseLogs {
		logger = klog.LoggerWithName(logger, "Filter")
	}

	for _, pl := range f.filterPlugins {                    // 遍历所有注册的 filter 插件
		if state.GetSkipFilterPlugins().Has(pl.Name()) {    // CycleState 可以指定跳过某些插件
			continue
		}
		// ... 设置 logger ...
		if status := f.runFilterPlugin(ctx, pl, state, pod, nodeInfo); !status.IsSuccess() {
			if !status.IsRejected() {
				status = fwk.AsStatus(fmt.Errorf("running %q filter plugin: %w", pl.Name(), status.AsError()))
			}
			status.SetPlugin(pl.Name())                    // 记下是哪个插件 reject 的
			return status                                   // 任何一个插件 reject,这个节点出局
		}
	}

	return nil   // 所有插件都通过,这个节点放得下
}
```

读这段,你会看到一个 filter 插件循环的全部秘密:

- **`for _, pl := range f.filterPlugins`**:遍历所有注册的 filter 插件。注意 `f.filterPlugins` 是 framework 在初始化时根据调度器配置(启用哪些插件、禁用哪些)预填好的切片。
- **`f.runFilterPlugin(...)`**:调单个插件的 `Filter` 方法(其实就是 `pl.Filter(ctx, state, pod, nodeInfo)`,外加一层 metrics 计时)。
- **`!status.IsSuccess()` → 立刻 return**:任何一个 filter 插件 reject,这个节点立刻出局——**这是"一票否决"的代码化身**。filter 是 AND 关系,所有插件都得过。
- **`status.SetPlugin(pl.Name())`**:记下是哪个插件 reject 的——这个信息最终会进 `FitError`,告诉运维"这个节点因为 `NodeResourcesFit` 失败,因为 CPU 不够"。**这是 filter 阶段可观测性的根基**。

> **一个细节**:`state.GetSkipFilterPlugins().Has(pl.Name())`——`CycleState` 可以指定"这次调度跳过某些 filter 插件"。这是一个高级特性,允许某些 Pod 在特定情况下绕过部分 filter(比如内部特权 Pod)。这也是 scheduling framework 灵活性的体现。

### 四、score 阶段怎么算分:`RunScorePlugins`

score 阶段的 `RunScorePlugins` 比 filter 复杂一些——它要并行算、要归一化、要加权。看它的核心(分三步):

> **源码位置**:[`pkg/scheduler/framework/runtime/framework.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/scheduler/framework/runtime/framework.go) 的 `RunScorePlugins`。

```go
// RunScorePlugins runs the set of configured scoring plugins.
// It returns a list that stores scores from each plugin and total score for each Node.
func (f *frameworkImpl) RunScorePlugins(ctx context.Context, state fwk.CycleState, pod *v1.Pod, nodes []fwk.NodeInfo) (ns []fwk.NodePluginScores, status *fwk.Status) {
	// ... 第一步:并行调每个 Score 插件的 Score 方法,给每个节点打原始分 ...
	// (略,省略第一段并行 Score 调用)
	// ... 第二步:并行调每个 Score 插件的 NormalizeScore(归一化到 0~100)...
	// (略)

	// 第三步:加权求和(这段最关键)
	// Apply score weight for each ScorePlugin in parallel,
	// and then, build allNodePluginScores.
	f.Parallelizer().Until(ctx, len(nodes), func(index int) {
		nodePluginScores := fwk.NodePluginScores{
			Name:   nodes[index].Node().Name,
			Scores: make([]fwk.PluginScore, len(plugins)),
		}

		for i, pl := range plugins {
			weight := f.scorePluginWeight[pl.Name()]              // 这个插件的权重(来自调度器配置)
			nodeScoreList := pluginToNodeScores[pl.Name()]
			score := nodeScoreList[index].Score                   // 这个插件给这个节点的归一化分

			if score > fwk.MaxNodeScore || score < fwk.MinNodeScore {   // 0~100 范围检查
				err := fmt.Errorf("plugin %q returns an invalid score %v, ...", pl.Name(), score, fwk.MinNodeScore, fwk.MaxNodeScore)
				errCh.SendWithCancel(err, cancel)
				return
			}
			weightedScore := score * int64(weight)                // 加权:分 × 权重
			nodePluginScores.Scores[i] = fwk.PluginScore{
				Name:  pl.Name(),
				Score: weightedScore,
			}
			nodePluginScores.TotalScore += weightedScore          // 累加到这个节点的总分
		}
		allNodePluginScores[index] = nodePluginScores
	}, metrics.Score)

	// ...
}
```

读这段,score 的"加权求和"清清楚楚:

- **每个 Score 插件,给每个节点打一个 0~100 的分**(在第一步并行算)。
- **每个插件有一个权重**(`f.scorePluginWeight[pl.Name()]`,来自调度器配置——比如 `NodeResourcesBalancedAllocation` 默认权重 1,`InterPodAffinity` 默认权重 1,`PodTopologySpread` 默认权重 2)。
- **最终每个节点的总分 = 所有插件(原始分 × 权重)之和**:`weightedScore := score * int64(weight)`,然后 `TotalScore += weightedScore`。
- **分数范围强制 0~100**(`fwk.MaxNodeScore`/`MinNodeScore`),超范围直接报错——保证插件不会"作弊"打巨分稀释别人。

最终,`schedulePod` 里的 `sortedPrioritizedNodes.Pop()` 选出 `TotalScore` 最高的节点,作为 `SuggestedHost`。这就是一次调度的全部决策。

### 五、写回:bind,完成一轮 reconcile

选出节点后,`scheduleOnePod` 的后半段(在 `schedulePod` 返回后)做两件事:

1. **assume**:`sched.assume(...)`——在本地缓存里把这个 Pod "假设"放到选中的节点(`sched.Cache.AssumePod`),避免下一次调度撞车。
2. **异步 bind**:开一个 goroutine 跑 `bindingCycle`,里面调 `RunPreBindPlugins` → `sched.bind`(`sched.bind` 内部调 `schedFramework.RunBindPlugins`,最终通过 apiserver 写一个 `Binding` 对象,把 `pod.Spec.NodeName` 设成选中的节点)。

bind 写回 apiserver 成功后,这个 Pod 就有了 `nodeName`,从 `Pending` → 进入 `ContainerCreating` 状态。**这一刻,接力棒从 scheduler 传给 kubelet**——kubelet watch 到"我这个节点上有一个新 Pod",开始真正去启动它(那是第 19 章的事)。

> **完整的一轮 scheduler reconcile,串起来**:informer 看到 Pod `Pending` → 推进 SchedulingQueue → `ScheduleOne` 弹出 → `findNodesThatFitPod`(filter 插件,红笔划船)→ `prioritizeNodes`(`RunScorePlugins`,打分选船)→ 选最高分节点 → assume 占位 → 异步 bind 写回 nodeName → Pod 离开 scheduler 视野,交给 kubelet。**这一轮的全部"智能",都封装在那些可插拔的 filter/score 插件里;scheduler 主循环,只是一个老老实实跑插件、聚合结果、写回决策的管道**。

---

## 章末小结

### 用航运比喻回顾本章:调度员的工作台

回到港口。这一章我们做了一件事:**钻进"全球航运调度中心"里那个专职调度员的办公室,看清他怎么决定"这箱货上哪艘船"**。

```
              调度员的工作台(scheduler)
              ┌─────────────────────────────────────────────────┐
              │                                                 │
   待派单      │  ① 抽一张运单(pending Pod) ←─ SchedulingQueue  │
   ──────►    │                                                 │
              │  ② 红笔阶段·FILTER(用硬约束划船)              │
              │     ┌──────────────────────────────────────┐    │
              │     │ NodeResourcesFit 资源够不够?          │    │
              │     │ nodeports       端口冲突吗?           │    │
              │     │ nodeaffinity    label 满足吗?         │    │
              │     │ tainttoleration 污点能忍吗?           │    │
              │     │ interpodaffinity 亲和/反亲和满足吗?   │    │  ───► 每个判断都是一个插件
              │     │ podtopologyspread 拓扑打散满足吗?     │    │       (scheduling framework)
              │     │ ...                                  │    │
              │     └──────────────────────────────────────┘    │
              │     任何一个出局 → 整艘船划掉(一票否决)        │
              │                                                 │
              │  ③ 打分阶段·SCORE(在剩下的船里选最优)         │
              │     ┌──────────────────────────────────────┐    │
              │     │ NodeResourcesBalancedAllocation 平衡 │    │
              │     │ NodeResourcesFit(LeastAllocated) ... │    │
              │     │ interpodaffinity      亲和加分       │    │
              │     │ tainttoleration       少污点加分     │    │
              │     │ podtopologyspread     打散加分       │    │
              │     │ ...                                  │    │
              │     └──────────────────────────────────────┘    │
              │     每个插件给 0~100 分 × 权重,总分最高胜出    │
              │                                                 │
              │  ④ 都放不下?→ PostFilter(defaultpreemption)   │
              │     踢掉低优先级货腾地方(抢占)                │
              │                                                 │
              │  ⑤ assume 占位 → 异步 bind 写回 nodeName        │
              │     ──────────────────────────────────► 交给 kubelet
              └─────────────────────────────────────────────────┘
```

把这张图钉在脑子里,我们用三句话总结调度员的工作原则:

1. **两阶段,先剔除再排序**:filter 用硬约束一票否决(放不下就出局),score 用软偏好加权排序(放得下的里面挑最好)。两阶段物理分开,既不互相稀释,又性能高效。
2. **每个判断都是可插拔插件**:scheduling framework 把"资源够不够""污点能不能忍""亲和满不满足"全部做成独立的 filter/score 插件,框架本身不带任何业务规则。换调度策略,就是换插件——这是大厂能定制调度、k8s 主线却不用改的根基。
3. **无状态、可预测**:调度器不存历史、不引入随机,同样的输入永远同样的输出。这让调度行为可推理、可调试、可测试,是生产稳定的根基。CycleState 只在同一次调度内传递临时状态,assume 机制在"无状态"和"避免竞态"之间做最小妥协。

### 本章在全书主线中的位置:reconcile 里"补到哪个节点"那一步

第 14 章那段 reconcile 伪代码,有一行注释:

```go
if actual < desired {
    createPods(desired - actual)   // 补到哪个节点,是调度器(scheduler)的事
}
```

这一章,我们把"补到哪个节点"这一步,从直觉到源码钉死了:

- **reconcile 循环算出"该补几个 Pod"**(Deployment 控制器的活),然后把这几个 Pod 写进 etcd(`Pending`)。
- **scheduler 接力**,watch 到这些 `Pending` Pod,逐个 filter + score,算出节点,bind 写回。
- **kubelet 再接力**,watch 到"我这个节点有新 Pod",真正启动容器(下一章)。

**scheduler 是 reconcile 范式里最特殊的一个 controller**——别的 controller 干的是"看偏差、补或杀"的负反馈,scheduler 干的是"在几百维解空间里一次性算最优"的决策。但**它的外壳仍是同一个范式**:watch 待处理对象 → 处理 → 写回 apiserver。这正是第 14、15 章说的"所有组件的共同骨架"——scheduler 也不例外,只是它的"处理"特别复杂,所以才被独立成进程、做成插件化框架。

### 五个"为什么"清单

如果你只能记五件事,记这五件:

1. **为什么调度分两阶段**:硬约束(能不能放)要"一票否决",软偏好(哪个更好)要"加权排序",两种性质相反的逻辑硬塞一个公式会两头不讨好(硬约束被软偏好稀释、软偏好被硬约束放大、性能浪费)。filter 先剔除、score 再排序,各用各的逻辑。
2. **为什么把判断做成插件**:写死的调度器无法满足大厂定制需求(GPU 调度、租户隔离、地理亲和),新规则难加、难测、难配。scheduling framework 用统一插件接口(`Filter`/`Score` 等),让"换调度策略 = 换插件",k8s 主线一个插件都不用知道。**这是 scheduler 独立成进程的真正原因**。
3. **为什么调度看 requests 而不是 limits 或实际用量**:按 limits 调度会严重浪费(机器 90% 闲置),按实际用量调度会峰值打架(平时塞满、高峰抖动)。k8s 把"调度(信 requests)"和"限流(信 limits,由 cgroup 兜底)"分开,既不浪费又不打架,允许超卖(overcommit)。
4. **四组调度维度各解决什么**:`requests/limits` 解决资源维度;`nodeAffinity`(Pod 选节点)和 `taint/toleration`(节点拒 Pod)是一对对偶,解决 Pod-节点关系;`podAffinity`/`podAntiAffinity`(Pod 选/躲 Pod)解决 Pod-Pod 关系(高可用分散、主从同节点);`priority/preemption` 解决资源紧张时"谁让位"——高优先级 Pod 可以踢低优先级 Pod 腾地方(受 PDB 护栏)。
5. **为什么调度器无状态可预测**:有状态/随机会让调试成噩梦(问题不可重现)、规划成黑盒(行为不可推理)、插件难独立测。k8s 把调度器做成纯函数式(同样输入同样输出),状态严格限制在 `CycleState`(同一次调度内的临时状态)。assume 机制是"无状态可预测"和"避免竞态"之间的最小妥协。

### 想继续深入,该往哪钻

- **亲手看调度决策**:`kubectl describe pod <name>`,翻到 `Events` 段——你会看到一行 `Successfully assigned ... to node-X`,以及前面一行 `Scheduled` 之前可能有 `FailedScheduling` + 详细原因(比如 `0/5 nodes are available: 5 Insufficient memory`)。**这个原因字符串,就是 filter 插件返回的 `Unschedulable` 状态聚合出来的——你能直接看到哪个插件 reject 了哪些节点**。
- **看调度器主循环**:打开 [`pkg/scheduler/schedule_one.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/scheduler/schedule_one.go),从 `ScheduleOne` → `scheduleOnePod` → `schedulePod` 顺一遍。注意 `findNodesThatFitPod`(filter)和 `prioritizeNodes`(score)这两个调用点,就是两阶段的入口。
- **看插件接口和运行时**:接口在 [`staging/src/k8s.io/kube-scheduler/framework/interface.go`](https://github.com/kubernetes/kubernetes/blob/master/staging/src/k8s.io/kube-scheduler/framework/interface.go)——`Plugin`/`FilterPlugin`/`ScorePlugin` 等;运行时在 [`pkg/scheduler/framework/runtime/framework.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/scheduler/framework/runtime/framework.go)——`RunFilterPlugins`/`RunScorePlugins`。把这两个文件并起来读,插件体系就通了。
- **看一个真实插件的实现**:[`pkg/scheduler/framework/plugins/noderesources/fit.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/scheduler/framework/plugins/noderesources/fit.go) 的 `Filter` 方法——本章重点拆过。建议再读 `tainttoleration` 和 `interpodaffinity` 两个,你会看到不同维度的 filter 实现都遵循同一个模式(取 Pod 约束、对节点判断、返回 Status)。
- **看抢占逻辑**:[`pkg/scheduler/framework/plugins/defaultpreemption/default_preemption.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/scheduler/framework/plugins/defaultpreemption/default_preemption.go) 的 `PostFilter`,顺到 `SelectVictimsOnNode`——看它怎么选牺牲品、怎么尊重 PDB。
- **想自己写一个调度插件**:k8s 官方有 [scheduler-plugins 仓库](https://github.com/kubernetes-sigs/scheduler-plugins),里面有大量示例(容量感知、网络感知、DrainNode 等)。挑一个最简单的读完,你就能上手写自己的插件——这就是 scheduling framework 的全部用意。

---

> 调度器讲完了:Pod 该放哪个节点,由一个"两阶段 + 插件化 + 无状态"的调度员拍板。可调度器只是**填了运单上的船号**——它自己不吊集装箱、不通电、不起容器。真正拿着这张填好的运单、把集装箱吊上船、固定好、通电运转的,是每艘船上的驻港经理。**调度器说"这个 Pod 在 node-1",node-1 上的 kubelet 怎么知道?知道了之后又怎么真把容器跑起来?它跑起来了,节点状态又怎么报回控制平面?**——下一章,我们钻进 kubelet 这个"节点代理人"。翻开 **第 19 章 · kubelet:节点上的代理人**。
