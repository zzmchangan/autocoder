# 第 17 章 · 声明式 API 与 reconcile:k8s 的灵魂

> **前置**:你需要先读完[第 14 章《编排为什么必须存在》](P5-14-第一性原理-编排为什么必须存在.md)和[第 15 章《k8s 架构:控制平面与节点》](P5-15-k8s架构-控制平面与节点.md)。第 14 章用一段伪代码,把 k8s 半本的第一性原理立住了——**编排 = 声明期望状态 + 控制器自动调谐(reconcile)**,并且明说"这套范式在源码里长什么样,第 17 章会逐行拆";第 15 章又把架构骨架搭好,正式引出了 informer 三件套(SharedInformer / DeltaFIFO / Reflector),并说"第 17 章会钻进 informer 的源码,把每一行讲透"。**这一章,就是来兑现这两个承诺的。**

> **核心问题**:为什么 k8s 是"声明我想要 3 个副本"而不是"命令你去启动 3 个"?这套范式为什么这么强大,以至于成了整个云原生世界的根基?
>
> 第 14 章我们讲清了"为什么必须换范式",第 15 章讲了"reconcile 循环跑在哪个组件里",但一直把那套循环当成一个抽象的口号。这一章,我们把它**钉死在源码上**。你会发现,整个 k8s 的灵魂——所有控制器、所有 Operator、所有自愈和扩缩容的魔力——底下就是一条朴实到惊人的事件流水线:**Reflector(list+watch apiserver)→ DeltaFIFO(把变化排成事件队列)→ SharedInformer(分发事件,更新本地缓存,触发回调)→ workqueue(控制器串行取 key)→ reconcile 函数(读期望、读实际、写回)**。把这条链路走通,你就拿到了理解 k8s 一切魔力的钥匙。
>
> **读完本章你会明白**:
> - 命令式和声明式的本质区别,不是"换个写法",而是**换了一个数学结构**——声明式把"状态"从一堆散落的命令里解放出来,变成一个可比较、可收敛、天然幂等的目标。
> - 为什么 k8s 的所有控制器(不管是内置的 Deployment 控制器,还是你写的 Operator)都长着**同一副骨架**——informer + workqueue + reconcile。这套骨架是 k8s 把"分布式系统的难题"降维成"写一个 switch 语句"的关键。
> - informer 五件套每一件各解决了什么问题——为什么 list 之后要 watch、为什么需要一个 FIFO 队列、为什么要有本地缓存(而不是每次回 apiserver 读)、为什么 reconcile 要从 workqueue 取 key 而不是直接在回调里干。
> - 为什么这个范式能无限扩展——**CRD + 写个新 controller,就能让 k8s 管一种它从未见过的新资源**(一个 MySQL 集群、一个证书、一条流水线)。这就是 Operator 模式,也是云原生生态爆炸性繁荣的根源。

> **如果一读觉得太难**:这一章源码密度很大,informer 那条链路有五个环节,很容易把人绕晕。先只记住这条流水线的一句话版本——
> ① **list+watch**(盯着 apiserver,把资源变化拉下来)→ ② **排队**(把变化排成事件队列)→ ③ **分发**(更新本地缓存,通知关心这个变化的人)→ ④ **workqueue**(关心的人把"该处理哪个对象"塞进自己的工作队列)→ ⑤ **reconcile**(从队列取出对象,读期望、读实际、写回)。
> 这五步钉死,中间的源码细节可以慢慢填。本章结尾会再用一张图把这条流水线收束一遍。

---

## 章首·一句话点破

如果你对 k8s 的印象是"一个用 YAML 配置集群的工具",或者"一个比 docker 多了调度功能的运行时",请把这种印象先放一边。这是理解 k8s 时最容易绊倒的一个误解,它会把你的注意力引向"这些字段怎么填",而不是真正改变世界的那件事。

这一章要做的第一件事,是把下面这句话连根钉死:

> **k8s 的灵魂,不是它的组件、不是它的 API、不是它的 YAML,而是一个反复运转的 reconcile 循环。这个循环的范式如此干净,以至于整个云原生世界——从内置的 Deployment 控制器,到 Prometheus Operator、Cert-Manager、ArgoCD——底下都是同一副骨架。理解了这套骨架,你就拿到了一把万能钥匙,能打开云原生世界几乎每一个项目的源码。**

我们一块一块拆。先回到第 14 章那个还没被钉死的直觉:命令式和声明式,到底差在哪?

---

## 一、命令式 vs 声明式:差的不是写法,是数学结构

第 14 章我们用一张对比表,讲过命令式(脚本)和声明式(k8s)在表象上的差别——一个说"去启动 3 个",一个说"我要 3 个"。这一章,我们要把这个差别**追问到它的数学根上**,因为只有看清了根,你才会真正认同"为什么必须换范式",而不是觉得"这只是两种风格,无所谓"。

### 不这样会怎样:命令式的根本病根,是"状态"被动作淹没了

先看一段最朴素的命令式脚本(回忆第 14 章那段):

```bash
# 朴素的扩容脚本:发现 web 不够,就再起几个
docker run -d web
docker run -d web
docker run -d web
```

这段脚本的**信息含量**是什么?是三个动作:`docker run`、`docker run`、`docker run`。注意,这三个动作里**没有一个字**告诉你"我最终想要几个 web 容器"——你只能通过"数动作的次数"反推"大概是想要 3 个"。

这就是命令式的根本病根:**它把"目标(我想要什么)"和"动作(我怎么得到它)"焊死在了一起,而且只留下了动作,丢掉了目标。** 一旦动作被执行,目标就消失了——你没法从"已经跑了 3 次 docker run"反推出"作者本意是想要 3 个,还是想要 6 个但只跑了一半"。

这个病根会衍生出第 14 章讲的那四个灾难,但更根本的是,它让系统**失去了"被比较"的能力**:

- 你没法问"现在的状态,是不是我想要的状态?"——因为"我想要的状态"从来没被写下来过,只藏在脚本的语义里。
- 你没法问"如果现在不是,差几个?"——同样因为"想要几个"无从得知。
- 你更没法问"该怎么把它拉回来?"——拉回哪个目标?

**一个无法被比较的系统,是无法被 reconcile 的。** 这就是为什么"写更多脚本"永远走不通——不是脚本写得不够聪明,是命令式这种范式,从根上就抹掉了"可被 reconcile 的目标"。

### 所以这样设计:声明式把"目标"和"动作"彻底解耦

声明式做了一件极其克制的事:**它只让你写"目标",完全不让你写"动作"。**

```yaml
# 一份 Deployment:你只声明"我要什么",完全不写"怎么做"
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
spec:
  replicas: 3            # ← 我要 3 个副本(目标)
  selector:
    matchLabels: { app: web }
  template:               # ← 每个副本长什么样(也是目标的一部分)
    spec:
      containers:
        - name: web
          image: nginx:1.25
```

读这份 YAML,注意两件事:

1. **它没有一条"动作"**。没有"启动""停止""重启""扩容""缩容"——这些动词一个都没有。它只描述了**世界应该是什么样子**:"应该有 3 个副本,每个跑 nginx:1.25"。
2. **它是一个可以被精确比较的数据结构**。`replicas: 3` 是一个整数,`template` 是一个对象——它们都能被读出来、能和"实际状态"做差、能算出"差几个、该怎么补"。**目标变成了一个可计算的东西,而不是藏在脚本语义里的隐含知识。**

> **比喻**:这是从"给港口经理一张操作清单(命令式)"升级到"给全球调度中心一张目标单(声明式)"——第 14 章已经用过这个对比,这里再深化一层。
>
> 操作清单的本质问题是:**它是为"一次性的、确定的世界"写的**。它假设"3 号船还在""5 号船有空位""B 码头的起重机没坏"——一旦任何一个假设崩了,整张清单作废。而目标单的本质优势是:**它不为任何具体的世界状态背书,它只声明终点**。3 号船沉了?调度中心自己换船。起重机坏了?它换码头。**不管世界怎么变,目标不变,调度中心盯着目标,见招拆招。**
>
> 这就是声明式的力量:**把"目标"从"动作"里解放出来,让它成为一个独立的、可比较的、不会被中间扰动改变的量。** 一旦目标独立了,reconcile 才有了"被拉向"的对象。

### 声明式的三个"白送"特性:幂等、自愈、可收敛

一旦"目标"独立了,下面三个特性几乎是**白送的**——它们不是 k8s 努力写出来的,而是从这个范式里**自然长出来的**:

- **天然幂等**。你把同一份 `replicas: 3` apply 一百次,世界最终都是"3 个副本"。因为每一轮 reconcile 都是"读目标 → 读实际 → 算差 → 补/杀",输入(目标=3)不变,输出永远收敛到 3。**幂等性是"盯着目标"的副产品,不是额外的工作。**
- **天然自愈**。一个容器挂了,"实际"变成 2,但"目标"还是 3(存在 etcd 里,挂容器不会改目标)。下一轮 reconcile 检测到"实际 < 目标",自动补 1 个。**你不需要写"故障重启脚本"——reconcile 循环本身就是故障重启。**
- **天然可收敛**。不管世界现在多乱(人为多起了几个、缩容没缩干净、部分失败卡在中间),只要"目标"不再变,reconcile 会一直跑,直到"实际 = 目标"。**任何扰动都被吸收,而不是被放大。**

第 14 章已经讲过这三点,这里再强调一次,是因为接下来我们要钻进源码,**亲眼看这三点是怎么被一段段朴素的代码实现的**。你会发现,实现它们的代码,朴素到会让你怀疑人生——因为难的不是"怎么写",而是"想到该这么写"。

---

## 二、controller 的 watch-reconcile 循环:k8s 一切魔力的根源

目标独立了,接下来要有一套机制,**不停地把"实际"往"目标"拉**。这套机制,就是 controller(控制器)的核心:**watch-reconcile 循环**。

我们先用一张总览图把它立起来,然后逐个环节拆:

```
        ┌─────────────────────────────────────────────────────────────┐
        │                    apiserver (背后是 etcd)                   │
        │   存着所有"期望状态"(Deployment.spec.replicas=3)             │
        │   和"实际状态"(现在有几个 Pod、分别在哪)                     │
        └──────────────────────────┬──────────────────────────────────┘
                                   │ ① list(全量拉一次)+ watch(订阅增量)
                                   ▼
        ┌─────────────────────────────────────────────────────────────┐
        │  Reflector  (reflector.go)                                   │
        │  负责"感知世界":list 一次建基线,然后 watch 增量              │
        │  把每个变化翻译成一个 Delta{Type, Object}                    │
        └──────────────────────────┬──────────────────────────────────┘
                                   │ ② 把 Delta 塞进队列
                                   ▼
        ┌─────────────────────────────────────────────────────────────┐
        │  DeltaFIFO  (delta_fifo.go)                                  │
        │  一个"事件队列":按对象 key 聚合 Delta,先进先出              │
        │  解决"同一个对象短时间多次变化"的合并问题                      │
        └──────────────────────────┬──────────────────────────────────┘
                                   │ ③ controller.processLoop 从队列 Pop
                                   ▼
        ┌─────────────────────────────────────────────────────────────┐
        │  SharedInformer.handleDeltas  (shared_informer.go)           │
        │  对每个 Delta 做两件事:                                       │
        │    a. 更新本地缓存(Indexer),让它和世界一致                  │
        │    b. 分发事件给所有注册的 handler(OnAdd/OnUpdate/OnDelete) │
        └──────────────────────────┬──────────────────────────────────┘
                                   │ ④ handler 收到事件,把"对象 key"塞进 workqueue
                                   ▼
        ┌─────────────────────────────────────────────────────────────┐
        │  workqueue  (util/workqueue)                                 │
        │  控制器自己的工作队列:去重、延时、限速、重试                  │
        │  里面存的是"该 reconcile 哪个 key"                            │
        └──────────────────────────┬──────────────────────────────────┘
                                   │ ⑤ worker 从队列取 key
                                   ▼
        ┌─────────────────────────────────────────────────────────────┐
        │  reconcile 函数  (如 deployment_controller.go 的 syncDeployment)│
        │  从本地缓存读"期望"(Deployment.spec.replicas)                │
        │  从本地缓存读"实际"(Lister 列出关联的 Pod/ReplicaSet)        │
        │  算偏差,采取行动(创建/删除 Pod,写回 apiserver)              │
        │  —— 这就是一轮 reconcile 的完整化身                            │
        └─────────────────────────────────────────────────────────────┘
```

> **比喻**:这是航运调度中心里一条完整的"情报 → 决策"流水线。
> - **Reflector** 是情报员:盯着港口台账(etcd 经 apiserver),有新货到了、有货转船了,他第一个知道。
> - **DeltaFIFO** 是情报篮子:情报员把每条变化丢进篮子,同一个运单号的多次变化会被攒在一起,避免重复处理。
> - **SharedInformer** 是调度中心的公告板:篮子里的情报被取出,先更新公告板(本地缓存,让所有人看到的都是最新状态),再广播给所有关心这类情报的督办(handler)。
> - **workqueue** 是每个督办桌上的待办盒:督办收到广播后,不立刻动手,而是把"该处理哪个运单号"写进待办盒(去重、限速,防止被同一件事淹没)。
> - **reconcile** 是督办真正动手:从待办盒取出一个运单号,查台账读"这单要运几箱"、读"现在实际运了几箱",少了补、多了杀。

这张图你先有个整体印象。接下来我们一个环节一个环节拆,每一节都回答两个问题:**这个环节解决什么不解决就不行的问题?它的源码长什么样?**

---

## 三、informer 五件套之一:Reflector——感知世界的眼睛

流水线的第一环,是 **Reflector**。它解决的问题是:**控制器怎么知道"世界变了"?**

### 不这样会怎样:几百个组件一起轮询,apiserver 当场去世

第 15 章已经讲过这个灾难:如果每个组件都"每隔 2 秒去 apiserver 问一次'有变化吗'",几百个组件 × 几秒一次 × 全量拉取,apiserver 和它背后的 etcd 会被这种无意义的重复查询活活打死。99% 的轮询都是浪费——大部分时间里,资源根本没变化。

所以 k8s 的答案是 **list 一次 + watch 增量**:启动时 list 一次全量,建立基线;之后只 watch 增量变化,有变化才被通知。这个"list + watch"的逻辑,就封装在 Reflector 里。

### 源码:ListAndWatch,Reflector 的主循环

Reflector 的核心方法叫 `ListAndWatch`(以及它的 context 版本 `ListAndWatchWithContext`),定义在 [reflector.go](https://github.com/kubernetes/kubernetes/blob/master/staging/src/k8s.io/client-go/tools/cache/reflector.go)。我们看它的真实签名:

```go
// staging/src/k8s.io/client-go/tools/cache/reflector.go
func (r *Reflector) ListAndWatchWithContext(ctx context.Context) error {
    // ...
}
```

这个方法的整体逻辑(简化叙述,不逐字摘录):

1. **list 一次全量**。调用 apiserver 的 list 接口,把它关心的那批资源(比如"所有 Deployment")全部拉下来。这一步建立"世界当前长什么样"的基线。
2. **拿这次的 resourceVersion**。list 返回的资源列表里,带一个全局递增的版本号(resourceVersion)。Reflector 记下这个版本号——它就是"我已经看到了哪儿"的书签。
3. **带着这个版本号,发起 watch**。告诉 apiserver:"从版本 X 开始,这个资源有任何变化,都推给我"。apiserver 维护一个 watch 流,把后续的所有 Added/Modified/Deleted 事件推过来。
4. **收到事件,翻译成 Delta,塞进 DeltaFIFO**。每收到一个 watch 事件(比如"Pod A 被新建了"),Reflector 调用 store 的 Add/Update/Delete,把变化写进下游的 DeltaFIFO。

> **resourceVersion 的妙处**:这个版本号是 k8s 处理"watch 断了怎么办"的关键。如果 watch 的长连接断了,Reflector 重连时会带上"我上次看到版本 X",apiserver 就把"从 X 到现在的所有变化"补发给它。如果 X 太老、已经被 etcd 压缩掉了,apiserver 返回错误,Reflector 触发一次重新 list。**这种"带版本的增量订阅 + 必要时全量重建",是分布式状态同步的通用解**(git 的 commit hash、etcd 自己的 watch,都是同一个套路)。

### 一个老资料的坑:watchHandler 已经被重构了

这里要插一个**源码核实细节**,因为很多老资料(和老博客)讲的 Reflector,都提到一个叫 `watchHandler` 的方法——但 master 分支已经把它**重构掉了**。

现在 Reflector 处理 watch 事件的,是几个**包级函数**(不再是 Reflector 的方法),定义在同一个 [reflector.go](https://github.com/kubernetes/kubernetes/blob/master/staging/src/k8s.io/client-go/tools/cache/reflector.go) 里:

```go
// staging/src/k8s.io/client-go/tools/cache/reflector.go
func handleListWatch(...) (bool, error) { ... }   // 处理"list 模式"的 watch
func handleWatch(...)   (bool, error) { ... }     // 处理普通 watch
func handleAnyWatch(...) (bool, error) { ... }     // 底层统一实现,上面两个都委托给它
```

`handleAnyWatch` 是真正干活的:它在一个 `for` 循环里 `select` watch 的事件 channel(`w.ResultChan()`),根据 `event.Type` 分发——`watch.Added` 就调 `store.Add`,`watch.Modified` 就调 `store.Update`,`watch.Deleted` 就调 `store.Delete`。**这三个 store 调用,最终都把变化塞进了下游的 DeltaFIFO**。

> 看老资料遇到 `watchHandler` 不要慌,知道它被拆成了 `handleWatch` / `handleListWatch` / `handleAnyWatch` 三个包级函数即可。这是 k8s 源码持续重构的常态——也是为什么本书坚持"用 WebFetch 核实 master 分支"的原因:**凭记忆写 k8s 源码,十有八九会写错**。

---

## 四、informer 五件套之二:DeltaFIFO——把变化排成事件队列

Reflector 把变化塞进了一个队列。这个队列叫 **DeltaFIFO**,定义在 [delta_fifo.go](https://github.com/kubernetes/kubernetes/blob/master/staging/src/k8s.io/client-go/tools/cache/delta_fifo.go)。它解决的问题是:**怎么把"散落的、可能乱序的、同一个对象可能连续多次的变化",整理成一条有序的、可消费的事件流?**

### 为什么需要一个专门的队列,不能直接调 handler?

你可能会想:Reflector 收到 watch 事件,为什么不直接调用控制器的 handler(`OnAdd`/`OnUpdate`/`OnDelete`),而要先塞进一个队列?

**不这样会怎样**:假设 Reflector 直接调 handler。考虑这个场景——同一个 Pod,在 10 毫秒内被改了三次(Update、Update、Update)。如果 Reflector 收到一个就同步调一次 handler,那么:

- handler 会被连着调三次,每次都触发一轮 reconcile。**三轮 reconcile 干的几乎是同一件事**(因为三次 Update 之间世界没怎么变),纯属浪费。
- 更糟的是,如果 handler 处理得慢(一轮 reconcile 要几十毫秒),Reflector 的 watch 循环会被**阻塞**在 handler 上——后续的 watch 事件没人收,event channel 会积压,甚至被 apiserver 断连。**生产环境里,控制器变慢会反噬 watch,这是灾难性的。**

所以需要一个**缓冲队列**:Reflector 只管往里塞(快),消费者(控制器)按自己的节奏从里头取(慢)。**生产者和消费者解耦,这是队列的经典价值。**

### DeltaType:变化有几种?

DeltaFIFO 存的"变化"叫 **Delta**,每个 Delta 有一个类型(DeltaType)和一个对象(Object)。看真实的类型定义,在 [delta_fifo.go](https://github.com/kubernetes/kubernetes/blob/master/staging/src/k8s.io/client-go/tools/cache/delta_fifo.go):

```go
// staging/src/k8s.io/client-go/tools/cache/delta_fifo.go
const (
    Added   DeltaType = "Added"
    Updated DeltaType = "Updated"
    Deleted DeltaType = "Deleted"
    // Replaced is emitted when we encountered watch errors and had to do a
    // relist, or on initial listing of objects.
    Replaced DeltaType = "Replaced"
    // ReplacedAll ... emitted instead when the FIFO supports atomic replacement.
    ReplacedAll DeltaType = "ReplacedAll"
    // Sync is for synthetic events during a periodic resync.
    Sync DeltaType = "Sync"
    // SyncAll indicates all known objects should be reprocessed.
    SyncAll DeltaType = "SyncAll"
    // Bookmark is emitted on Bookmark calls and Replace calls to pass resource
    // version information to the consumer.
    Bookmark DeltaType = "Bookmark"
)
```

注意这里有个**老资料的坑**:很多讲 informer 的资料只提"4 种 DeltaType"(Added/Updated/Deleted/Sync),但 master 分支实际有 **8 种**。多出来的几种各有用途:

- **Replaced / ReplacedAll**:watch 断了重新 list 时,用这两个类型表示"这是一次全量替换,不是单个对象的变化"。ReplacedAll 是新版支持"原子批量替换"时用的。
- **Sync / SyncAll**:周期性 resync(定时全量重新对账)时用。SyncAll 表示"把所有已知对象都重新处理一遍"。
- **Bookmark**:不携带具体对象,只携带 resourceVersion,用来"在消费者之间传递版本号进度",是一种轻量的心跳。

> **核心还是经典的三种**:Added、Updated、Deleted。理解了这三个,其他几种都是它们的"批量版"或"控制信号"。本章后面讲 `processDeltas` 时,你会看到这三 种怎么被分发。

### DeltaFIFO 的精妙:按 key 聚合 Delta

DeltaFIFO 最精妙的设计,是它**不是把每个 Delta 当成队列里独立的一项,而是按对象的 key 聚合**。看它核心字段的简化示意(标注"非源码逐字摘录",但字段名和结构是真实的):

```go
// (以下为简化示意,基于真实结构,非源码逐字摘录)
type DeltaFIFO struct {
    lock sync.Mutex
    cond sync.Cond
    items map[string]Deltas   // 按 key 聚合:同一个对象的多个 Delta 攒在一起
    queue []string             // 待处理的 key 顺序(每个 key 只出现一次)
    // ...
}
```

注意 `items` 是个 `map[string]Deltas`——**同一个对象的多次变化,会被追加到同一个 key 的 Deltas 列表里**,而 `queue` 里这个 key 只出现一次。

这意味着什么?如果同一个 Pod 在 10 毫秒内被 Update 了三次:

- `items["default/my-pod"]` = `[Update(v1), Update(v2), Update(v3)]`(三次变化攒在一起)
- `queue` 里 `"default/my-pod"` 只出现一次。

当消费者 Pop 这个 key 时,它一次性拿到**这个对象的完整变化序列**(从 v1 到 v3),而不是被叫三次、每次拿一个中间态。**这天然合并了"同一个对象的短时间多次变化",避免了基于中间态的无效 reconcile。** 这是 DeltaFIFO 区别于普通队列的关键一笔。

### Pop:阻塞地取,空了就等

DeltaFIFO 的核心消费方法是 `Pop`,定义在 [delta_fifo.go](https://github.com/kubernetes/kubernetes/blob/master/staging/src/k8s.io/client-go/tools/cache/delta_fifo.go):

```go
// staging/src/k8s.io/client-go/tools/cache/delta_fifo.go
func (f *DeltaFIFO) Pop(process PopProcessFunc) (interface{}, error) {
    f.lock.Lock()
    defer f.lock.Unlock()
    for {
        for len(f.queue) == 0 {
            // 队列空,挂起等待;Close() 时会被唤醒并返回
            if f.closed {
                return nil, ErrFIFOClosed
            }
            f.cond.Wait()
        }
        isInInitialList := !f.hasSynced_locked()
        id := f.queue[0]
        f.queue = f.queue[1:]
        // ... 省略 trace 和 initialPopulationCount 处理
        item, ok := f.items[id]
        // ...
        delete(f.items, id)
        err := process(item, isInInitialList)   // 调用处理函数,处理这一组 Delta
        return item, err
    }
}
```

读这段真实源码,注意三个细节:

1. **队列空时 `f.cond.Wait()` 挂起**——这是"阻塞等待",而不是"轮询 + sleep"。没有空转的 CPU 开销,有事件进来被 `cond.Broadcast()` 唤醒。**事件驱动系统的标准姿态**。
2. **`process(item, isInInitialList)`**——Pop 出一组 Delta 后,把它交给一个处理函数 `process`。这个 `process` 是谁?下一节揭晓,它就是 SharedInformer 的 `handleDeltas`。
3. **`isInInitialList` 标志**——告诉处理函数"这组 Delta 是不是初次全量 list 阶段产生的"。控制器可以用这个标志区分"初次同步"和"后续增量",做一些特殊处理(比如初次同步时不打"新建"的事件日志)。

---

## 五、informer 五件套之三:SharedInformer——分发事件,更新本地缓存

DeltaFIFO 被 Pop 出来的 Delta,交给谁处理?这就是 **SharedInformer** 的活。它定义在 [shared_informer.go](https://github.com/kubernetes/kubernetes/blob/master/staging/src/k8s.io/client-go/tools/cache/shared_informer.go),是整个 informer 体系的**门面(facade)**——对外暴露统一接口,对内协调 Reflector、DeltaFIFO、本地缓存、事件分发。

### SharedInformer 干的两件事

DeltaFIFO Pop 出一组 Delta 后,会调用 SharedInformer 的 `handleDeltas` 方法。这个方法干**两件事**:

1. **更新本地缓存(Indexer)**:把 Delta 应用到本地缓存上——Added 就加进去,Updated 就替换,deleted 就删掉。**让本地缓存始终和"世界当前的样子"一致。**
2. **分发事件给所有注册的 handler**:调用每个 `AddEventHandler` 注册进来的回调,告诉它们"这个对象被 Add/Update/Delete 了"。**这就是 reconcile 循环被触发的地方。**

我们看真实的 `handleDeltas` 源码:

```go
// staging/src/k8s.io/client-go/tools/cache/shared_informer.go
func (s *sharedIndexInformer) handleDeltas(logger klog.Logger, obj interface{}, isInInitialList bool) error {
    s.blockDeltas.Lock()
    defer s.blockDeltas.Unlock()

    if deltas, ok := obj.(Deltas); ok {
        return processDeltas(logger, s, s.indexer, deltas, isInInitialList, s.keyFunc)
    }
    return errors.New("object given as Process argument is not Deltas")
}
```

注意两件事:

- **方法名是小写 `handleDeltas`**(h 小写)。这是 master 分支的真实方法名。但有意思的是,**这个文件顶部的文档注释里,写的还是大写 `HandleDeltas`**——见 shared_informer.go 第 590 行附近的注释 `processing them with sharedIndexInformer::HandleDeltas`。**这是 k8s 源码里一个陈旧注释的活样本**:代码早就重构改成小写了,注释还停留在旧名字上。看老资料遇到大写 `HandleDeltas` 不要怀疑,知道代码里是小写即可。
- `handleDeltas` 本身只加了把锁(`blockDeltas`,保证 Delta 处理的串行化),真正的活委托给了 `processDeltas`。**`processDeltas` 才是真正"更新缓存 + 分发事件"的地方。**

### processDeltas:一个 switch,把 Delta 变成"缓存更新 + 事件分发"

`processDeltas` 是整个 informer 体系的**心脏**——它把"一个 Delta"翻译成"一次缓存操作 + 一次 handler 回调"。它定义在 [controller.go](https://github.com/kubernetes/kubernetes/blob/master/staging/src/k8s.io/client-go/tools/cache/controller.go)(注意,虽然名字叫 controller.go,但 `processDeltas` 是个包级函数,不是某个 controller 的方法):

```go
// staging/src/k8s.io/client-go/tools/cache/controller.go
func processDeltas(
    logger klog.Logger,
    handler ResourceEventHandler,   // 接收事件通知的对象(就是 SharedInformer 自己)
    clientState Store,              // 本地缓存(Indexer)
    deltas Deltas,
    isInInitialList bool,
    keyFunc KeyFunc,
) error {
    // from oldest to newest
    for _, d := range deltas {
        obj := d.Object
        switch d.Type {
        // ... 省略 ReplacedAll / SyncAll / Bookmark 等批量类型
        case Sync, Replaced, Added, Updated:
            if old, exists, err := clientState.Get(obj); err == nil && exists {
                if err := clientState.Update(obj); err != nil {   // ① 更新本地缓存
                    return err
                }
                handler.OnUpdate(old, obj)                         // ② 分发 Update 事件
            } else {
                if err := clientState.Add(obj); err != nil {       // ① 加进本地缓存
                    return err
                }
                handler.OnAdd(obj, isInInitialList)                // ② 分发 Add 事件
            }
        case Deleted:
            if err := clientState.Delete(obj); err != nil {        // ① 从本地缓存删
                return err
            }
            handler.OnDelete(obj)                                  // ② 分发 Delete 事件
        // ...
        }
    }
    return nil
}
```

读这段真实源码,你会发现它**朴素到惊人**——整个 informer 体系的心脏,就是一个 `for` 循环 + 一个 `switch`:

- 对每个 Delta,根据类型(Sync/Replaced/Added/Updated 归一类,Deleted 单独一类),走两条路之一。
- 每条路都干**完全相同的两件事**:**先动本地缓存(`clientState.Add/Update/Delete`),再通知 handler(`handler.OnAdd/OnUpdate/OnDelete`)**。
- 顺序很关键:**先更新缓存,再通知 handler**。这样 handler 被调用时,本地缓存已经是最新状态——它去缓存里读"期望"和"实际",读到的都是最新值。

> **这就是"本地缓存"存在的根本原因**。第 15 章我们说 informer 用"本地缓存"避免了每次回 apiserver 读——这里你看到这个设计的落点:**handler(reconcile)读的所有状态,都来自本地缓存,而不是 apiserver**。本地缓存是 informer 替控制器维护的"世界快照",它和 etcd 最终一致(靠 watch 增量追平),但读它**不打扰 apiserver**。这是 k8s 能扛大规模的根基之一。

### OnAdd/OnUpdate/OnDelete:SharedInformer 自己的回调,负责广播

注意 `processDeltas` 里的 `handler`,其实就是 SharedInformer 自己(它实现了 `ResourceEventHandler` 接口)。它的 `OnAdd/OnUpdate/OnDelete` 方法,干的是**广播**——把事件分发给所有通过 `AddEventHandler` 注册的"外部 handler"(也就是真正的控制器)。看真实的实现:

```go
// staging/src/k8s.io/client-go/tools/cache/shared_informer.go
// Conforms to ResourceEventHandler
func (s *sharedIndexInformer) OnAdd(obj interface{}, isInInitialList bool) {
    s.cacheMutationDetector.AddObject(obj)
    s.processor.distribute(addNotification{newObj: obj, isInInitialList: isInInitialList}, false)
}

func (s *sharedIndexInformer) OnUpdate(old, new interface{}) {
    isSync := false
    // ... 判断 isSync(resourceVersion 没变,就是 resync 事件)
    s.cacheMutationDetector.AddObject(new)
    s.processor.distribute(updateNotification{oldObj: old, newObj: new}, isSync)
}

func (s *sharedIndexInformer) OnDelete(old interface{}) {
    s.processor.distribute(deleteNotification{oldObj: old}, false)
}
```

核心是 `s.processor.distribute(...)`——`processor` 是 SharedInformer 内部的一个事件分发器,它维护着所有注册的 handler,`distribute` 把一个 notification(Add/Update/Delete)发给每一个 handler。**这就是"Shared"的含义:同一个 informer 实例,可以被多个 handler 共享**——三个组件都关心 Pod 变化,它们不需要各自起三个 informer、各自 list+watch 一遍,而是共享一个 informer,各自注册自己的 handler。informer 内部只做一次 list+watch,然后把事件分发给所有 handler。**几百个关心 Pod 的组件,在 apiserver 看来只是一个 watch 客户端**——这是把 watch 负载摊到最小的设计。

---

## 六、informer 五件套之四:Indexer——本地缓存,带索引的世界快照

`processDeltas` 里那个 `clientState`(类型是 `Store`,实际实现是 `Indexer`),就是**本地缓存**。它定义在 [thread_safe_store.go](https://github.com/kubernetes/kubernetes/blob/master/staging/src/k8s.io/client-go/tools/cache/thread_safe_store.go)。这一节我们快速过一下它,因为它相对直观,但有一个常被忽略的精妙之处:**它不只是个 map,还带索引**。

### 不这样会怎样:控制器每次 reconcile 都要遍历全部对象

假设本地缓存只是个普通的 `map[string]obj`。Deployment 控制器要 reconcile "web 这个 Deployment",它需要知道"现在有几个属于 web 的 Pod"。

如果只有 map,它只能 `for key, obj := range store { if belongsToWeb(obj) { count++ } }`——**遍历整个集群的所有 Pod**。一个 10000 Pod 的集群,每一轮 reconcile 都遍历 10000 个对象?这会让控制器慢到不可用。

所以 Indexer 在 map 之上,加了一层**索引(index)**:你可以注册一个索引函数(比如"按 label `app` 的值建索引"),之后查"所有 app=web 的 Pod"时,直接走索引,O(1) 拿到结果,不用遍历。看真实的 `ByIndex` 方法签名:

```go
// staging/src/k8s.io/client-go/tools/cache/thread_safe_store.go
func (c *threadSafeMap) ByIndex(indexName, indexedValue string) ([]interface{}, error)
```

给它一个索引名和一个索引值,它返回所有匹配的对象。Deployment 控制器就是靠这个,瞬间拿到"web 这个 Deployment 关联的所有 Pod/ReplicaSet",不用遍历。**这是控制器能在海量对象里快速 reconcile的关键。**

### Add/Update/Delete:维护 map 和索引

Indexer 的 Add/Update/Delete 方法,除了动 map,还要维护索引。看真实的 `Add`:

```go
// staging/src/k8s.io/client-go/tools/cache/thread_safe_store.go
func (c *threadSafeMap) Add(key string, obj interface{}) {
    c.lock.Lock()
    defer c.lock.Unlock()
    oldObject := c.items[key]
    c.items[key] = obj
    c.index.updateIndices(oldObject, obj, key)   // 维护索引
}
```

朴素的几行:加锁、写 map、更新索引。**本地缓存的全部秘密,就是一个带锁的 map + 一层索引**。没有黑魔法。

---

## 七、informer 五件套之五:workqueue——控制器自己的工作队列

到这里,事件已经被 SharedInformer 分发到了控制器的 handler。但你可能注意到一个**反直觉的设计**:handler 收到事件后,**并没有直接 reconcile**,而是把"该处理哪个对象"塞进了一个**工作队列(workqueue)**,然后由另一个 worker 循环从队列里取出来 reconcile。

为什么要多此一举?这是 informer 体系里最容易被忽略、却最关键的一笔。

### 不这样会怎样:在 handler 里直接 reconcile 的三个灾难

假设我们让 handler 直接 reconcile——收到"OnUpdate deployment/web",立刻读缓存、算偏差、创建/删除 Pod。会发生什么?

**灾难一:handler 阻塞会反噬整个 informer。** 回忆 `processDeltas`——它是串行处理 Delta 的,调完 `handler.OnUpdate` 才处理下一个 Delta。如果 handler 里直接 reconcile(一轮可能要几十毫秒,还要回 apiserver 写),那么**整个 informer 的事件处理会被这一个慢 reconcile 卡住**,后面的 Delta 全部排队等待,本地缓存迟迟不能更新,最终和 etcd 严重脱节。**一个慢控制器,会把整个 informer 拖死。**

**灾难二:同一个对象短时间多次变化,会触发多次重复 reconcile。** 用户快速改了 Deployment 三次(改 replicas、改 image、又改回来),informer 会收到三个 Update 事件,handler 被调三次。如果每次都 reconcile,三次 reconcile 干的几乎是同一件事(最终态都是"3 个副本,新 image"),纯属浪费。

**灾难三:reconcile 失败了怎么办?** 如果 handler 直接 reconcile,失败了一次就过去了——下一轮要等到下一次"有变化"才会被重新触发。可是"下一次变化"可能要很久才来,期间系统一直处于不一致状态。**reconcile 失败了,得有人负责重试,handler 自己不擅长干这个。**

### 所以这样设计:handler 只入队,worker 出队才 reconcile

k8s 的解法是**生产者-消费者解耦**:

- **handler(生产者)**:收到事件,只做一件极快的事——算出"该处理哪个对象的 key",把它塞进 workqueue。然后立刻返回,不阻塞 informer。
- **worker(消费者)**:另一个独立的循环,不停地从 workqueue 取 key,调用 reconcile 函数。reconcile 慢没关系,它和 informer 的事件处理是分开的两条线程。

这样一拆,上面三个灾难全部化解:

- handler 极快(就是 `queue.Add(key)`),不会阻塞 informer。
- workqueue **天然去重**——同一个 key 被塞多次,队列里只保留一份。用户改三次 Deployment,handler 入队三次"web"这个 key,但队列里只有一个"web",worker 只 reconcile 一次。**多次变化被合并成一次 reconcile,而且是基于最新状态(因为本地缓存已经追到最新)。**
- workqueue **支持重试**——reconcile 失败了,worker 调 `queue.AddRateLimited(key)` 把 key 重新入队(带退避延时),稍后再试。失败不会丢,会一直重试到成功(或超过 maxRetries 次放弃)。

> **比喻**:这是督办收到广播后,**先在待办盒上贴个标签(入队),不立刻动手**。等手头的活干完,再从待办盒里抽一张标签(出队),按标签上的运单号去处理。这样:① 督办不会被一个慢单子卡死(标签贴上就返回,继续收下一条广播);② 同一个运单号被广播十次,待办盒里只有一张标签(去重);③ 这个单子处理砸了,把标签重新扔回盒子(重试),不会丢。

### workqueue 的四件套:Get / Done / Add / AddRateLimited

workqueue 的核心接口(简化,真实接口在 [util/workqueue](https://github.com/kubernetes/kubernetes/tree/master/staging/src/k8s.io/client-go/util/workqueue)):

- `Get()`:从队列取出一个 key,标记为"处理中"。**注意,取出来不等于处理完**——这个 key 此刻被标记成"processing",同一个 key 在处理期间不会再被取出(防止并发处理同一个对象)。
- `Done(key)`:告诉队列"这个 key 处理完了"。处理完之后,如果这个 key 在处理期间又被 Add 过(说明有新变化),它会被重新放回队列。
- `Add(key)`:塞一个 key 进队列。如果 key 已经在队列里(或正在处理中),这次 Add 会被合并,不重复。
- `AddRateLimited(key)`:带退避延时的入队——reconcile 失败时用,延时时间随失败次数指数增长(防止疯狂重试打爆 apiserver)。

这四个方法合起来,实现了 **去重 + 串行处理 + 失败重试 + 限速**——这是分布式控制器工作队列的全部需求。

---

## 八、一个真实 controller 的 reconcile:deployment_controller 逐行看

五件套讲完了,我们用一个**真实的控制器**把它们串起来。最好的例子是 k8s 内置的 **Deployment 控制器**——它管的就是第 14 章那段伪代码的真实版:"我要 N 个副本"。它的源码在 [pkg/controller/deployment/deployment_controller.go](https://github.com/kubernetes/kubernetes/blob/master/pkg/controller/deployment/deployment_controller.go)。

### 装配:NewDeploymentController,把五件套接起来

先看控制器是怎么被"装配"起来的——`NewDeploymentController` 干的就是"接好 informer + workqueue + reconcile"这几根线:

```go
// pkg/controller/deployment/deployment_controller.go
func NewDeploymentController(ctx context.Context, dInformer appsinformers.DeploymentInformer,
    rsInformer appsinformers.ReplicaSetInformer, podInformer coreinformers.PodInformer,
    client clientset.Interface) (*DeploymentController, error) {
    // ...
    dc := &DeploymentController{
        // ...
        queue: workqueue.NewTypedRateLimitingQueueWithConfig(   // ① 创建 workqueue
            workqueue.DefaultTypedControllerRateLimiter[string](),
            workqueue.TypedRateLimitingQueueConfig[string]{Name: "deployment"},
        ),
    }
    // ...

    // ② 给三个 informer 注册 handler(Deployment / ReplicaSet / Pod 各一个)
    dInformer.Informer().AddEventHandler(cache.ResourceEventHandlerFuncs{
        AddFunc: func(obj interface{}) { dc.addDeployment(logger, obj) },
        UpdateFunc: func(oldObj, newObj interface{}) { dc.updateDeployment(logger, oldObj, newObj) },
        DeleteFunc: func(obj interface{}) { dc.deleteDeployment(logger, obj) },
    })
    rsInformer.Informer().AddEventHandler(cache.ResourceEventHandlerFuncs{
        AddFunc: func(obj interface{}) { dc.addReplicaSet(logger, obj) },
        // ...
    })
    podInformer.Informer().AddEventHandler(cache.ResourceEventHandlerFuncs{
        DeleteFunc: func(obj interface{}) { dc.deletePod(logger, obj) },
        // ...
    })

    // ③ 把 reconcile 函数指针赋给 syncHandler(后面 worker 会调它)
    dc.syncHandler = dc.syncDeployment
    dc.enqueueDeployment = dc.enqueue

    // ④ 拿到三个 Lister(从本地缓存读对象的接口)
    dc.dLister = dInformer.Lister()
    dc.rsLister = rsInformer.Lister()
    dc.podLister = podInformer.Lister()
    // ...
    return dc, nil
}
```

读这段装配代码,把五件套对应起来:

- **① workqueue**:控制器自己持有一个 `TypedRateLimitingInterface`(带限速的工作队列)。
- **② 三个 informer 的 handler**:Deployment 控制器关心三种资源的变化——Deployment(用户改了期望)、ReplicaSet(Deployment 底下管副本的)、Pod(副本挂了)。每种资源注册一个 handler,handler 里干的事都是"把这个对象对应的 key 入队"。
- **③ syncHandler**:reconcile 函数(`syncDeployment`)被赋给 `dc.syncHandler`。后面 worker 取出 key 后,调的就是它。
- **④ Lister**:三个 Lister 是"从本地缓存读对象"的接口。reconcile 时,控制器不回 apiserver,而是从 Lister 读——读 Deployment 期望、读 ReplicaSet/Pod 实际,都走本地缓存。

> **注意一个细节**:Deployment 控制器**不止关心 Deployment 这一种资源**。它还 watch ReplicaSet 和 Pod——因为"实际有几个副本"这个信息,藏在 ReplicaSet 和 Pod 里。用户没改 Deployment,但一个 Pod 挂了,控制器也得知道、也得 reconcile。**这就是为什么一个控制器往往要 watch 多种资源**——它需要的"实际状态",分散在多种资源里。

### 入队:handler 怎么把对象变成 key

handler(`addDeployment` / `updateDeployment` 等)干的事极其简单——把对象转成 key,塞进队列。看入队的核心函数 `enqueue`:

```go
// pkg/controller/deployment/deployment_controller.go
func (dc *DeploymentController) enqueue(deployment *apps.Deployment) {
    key, err := controller.KeyFunc(deployment)   // 算出 key(通常是 "namespace/name")
    if err != nil {
        utilruntime.HandleError(fmt.Errorf("couldn't get key for object %#v: %v", deployment, err))
        return
    }
    dc.queue.Add(key)   // 入队
}
```

就两行:算 key(`controller.KeyFunc`,通常是 `"namespace/name"` 格式)、入队(`dc.queue.Add(key)`)。**handler 极快,不阻塞 informer。** 注意,handler 入队的是"对象的 key",而不是对象本身——后续 worker 拿着这个 key,再去本地缓存里读最新的对象。**这保证了 reconcile 读到的永远是最新状态**,而不是 handler 收到事件那一刻的旧状态(期间可能又有变化)。

### worker:经典 reconcile worker 骨架

worker 是控制器的"消费者"循环。看真实的 `worker` 和 `processNextWorkItem`:

```go
// pkg/controller/deployment/deployment_controller.go
func (dc *DeploymentController) worker(ctx context.Context) {
    for dc.processNextWorkItem(ctx) {
    }
}

func (dc *DeploymentController) processNextWorkItem(ctx context.Context) bool {
    key, quit := dc.queue.Get()        // ① 从队列取 key(阻塞,空了就等)
    if quit {
        return false
    }
    defer dc.queue.Done(key)           // ④ 处理完(无论成败),标记 Done

    err := dc.syncHandler(ctx, key)    // ② 调 reconcile 函数(syncDeployment)
    dc.handleErr(ctx, err, key)        // ③ 处理错误(重试或放弃)

    return true
}
```

这四行(`Get → syncHandler → handleErr → Done`),是**k8s 所有控制器的 worker 骨架**——不止 Deployment,ReplicaSet、DaemonSet、Job、StatefulSet,乃至于你写的 Operator,worker 长的都是这副模样。**记住这四行,你就记住了 k8s 控制器的消费侧全貌。**

再看错误处理 `handleErr`,它是"失败重试"逻辑的化身:

```go
// pkg/controller/deployment/deployment_controller.go
func (dc *DeploymentController) handleErr(ctx context.Context, err error, key string) {
    if err == nil || errors.HasStatusCause(err, v1.NamespaceTerminatingCause) {
        dc.queue.Forget(key)           // 没错(或 namespace 在删除),forget 这个 key
        return
    }
    // ...
    if dc.queue.NumRequeues(key) < maxRetries {   // 重试次数没超上限
        dc.queue.AddRateLimited(key)              // 带退避延时,重新入队
        return
    }
    // 重试超上限,放弃这个 key(记录错误日志)
    utilruntime.HandleError(err)
    dc.queue.Forget(key)
}
```

读这段,注意 reconcile 的**自愈式重试逻辑**:

- reconcile 成功(`err == nil`)→ `Forget`(忘记这个 key 的重试历史),完事。
- reconcile 失败,但重试次数 < `maxRetries`(定义在文件第 54 行,通常是个常数)→ `AddRateLimited`,带退避延时重新入队,稍后再试。
- 重试次数超上限 → 放弃,`Forget`。

**这个"失败自动重试 + 退避 + 超限放弃"的逻辑,是 reconcile 自愈特性的源码落点。** 第 14 章说"reconcile 对部分失败免疫",免疫从哪儿来?就从这个 `handleErr` 来——一轮 reconcile 跑挂了,worker 自动把它扔回队列,下一轮重新算,直到成功(或彻底放弃)。**你不需要写"重试脚本",workqueue + handleErr 替你写好了。**

### syncDeployment:reconcile 函数的真实面目

最后是真正的 reconcile 函数——`syncDeployment`。它是第 14 章那段伪代码的真实版。我们看它的核心结构(省略部分分支,保留主干):

```go
// pkg/controller/deployment/deployment_controller.go
func (dc *DeploymentController) syncDeployment(ctx context.Context, key string) error {
    namespace, name, err := cache.SplitMetaNamespaceKey(key)   // 解析 key
    // ...

    // ① 读"期望":从本地缓存拿这个 Deployment
    deployment, err := dc.dLister.Deployments(namespace).Get(name)
    if errors.IsNotFound(err) {
        // Deployment 被删了,什么都不做(级联删除由别的机制处理)
        return nil
    }
    d := deployment.DeepCopy()   // 深拷贝,避免改到缓存里的对象

    // ② 读"实际":列出这个 Deployment 拥有的所有 ReplicaSet
    rsList, err := dc.getReplicaSetsForDeployment(ctx, d)

    // ③ 如果正在被删除,只同步状态
    if d.DeletionTimestamp != nil {
        return dc.syncStatusOnly(ctx, d, rsList)
    }

    // ④ 各种分支决策:暂停?回滚?扩缩容?滚动更新?
    if d.Spec.Paused {
        return dc.sync(ctx, d, rsList)
    }
    if getRollbackTo(d) != nil {
        return dc.rollback(ctx, d, rsList)
    }
    scalingEvent, err := dc.isScalingEvent(ctx, d, rsList)
    if scalingEvent {
        return dc.sync(ctx, d, rsList)   // ← 这里会真正调整副本数(补/杀)
    }

    // ⑤ 根据更新策略,做滚动更新或重建
    switch d.Spec.Strategy.Type {
    case apps.RecreateDeploymentStrategyType:
        return dc.rolloutRecreate(ctx, d, rsList, podMap)
    case apps.RollingUpdateDeploymentStrategyType:
        return dc.rolloutRolling(ctx, d, rsList)
    }
    return fmt.Errorf("unexpected deployment strategy type: %s", d.Spec.Strategy.Type)
}
```

读这段真实的 reconcile,把它和第 14 章那段伪代码对照:

- **① 读期望**:`dc.dLister.Deployments(namespace).Get(name)`——从本地缓存读 Deployment,拿到 `d.Spec.Replicas`(期望副本数)。**对应伪代码的 `desired := etcd.Get(deploymentName).Replicas`**。
- **② 读实际**:`dc.getReplicaSetsForDeployment` 列出关联的 ReplicaSet,再往下能数出实际的 Pod 数。**对应伪代码的 `actual := len(etcd.ListPods(...))`**。
- **③④⑤ 算偏差,采取行动**:根据 Deployment 当前的状态(被删?暂停?要回滚?要扩缩容?要滚动更新?),走不同的分支,调用 `sync`/`rollback`/`rolloutRecreate`/`rolloutRolling` 等子函数,真正去创建/删除 Pod。

> **两个关键认知**:
> 1. **reconcile 读的所有状态,都来自本地缓存(Lister),不是 apiserver**。这是 informer 的本地缓存放点——reconcile 高频运行,如果每次回 apiserver 读,apiserver 会被打死。
> 2. **reconcile 函数本身是"无状态"的**。它只做"读期望、读实际、算偏差、采取行动",不记任何中间状态。失败重来,从头算一遍。**这就是第 14 章说的"对部分失败免疫"——没有中间状态,所以没有"接着上次跑"的负担。**

`syncDeployment` 里调用的 `sync`、`rolloutRolling` 等子函数,会进一步调用 apiserver 创建/删除 Pod(经 ReplicaSet),把决策写回 etcd。写回之后,新的变化又触发新一轮事件流,informer 收到、入队、worker 取出、reconcile……**整个 k8s,就是由无数条这样的事件流驱动的循环。**

---

## 九、CRD + Operator:这个范式为什么能无限扩展

讲完了内置的 Deployment 控制器,你可能会想:这套东西确实强大,但它管的是 k8s 内置的资源(Deployment、Service……)。**如果我想管一种 k8s 从来没见过的新资源——比如"一个 MySQL 集群",怎么办?**

这一节,我们回答本章的最后一个"为什么":**为什么这个范式能无限扩展,以至于催生了整个云原生生态。**

### 不这样会怎样:每种新需求都要改 k8s 核心代码

假设没有扩展机制。你想让 k8s 管 MySQL 集群,只能去改 kubernetes 的源码,加一种新资源(MySQLCluster),加一个内置控制器,然后重新编译、重新发布整个 k8s。这条路有几个致命问题:

- **每加一种资源都要改核心**——几十种数据库、消息队列、缓存……每种都改核心,k8s 会变成一个无法维护的怪物。
- **发布周期被核心绑架**——你的 MySQL 控制器有个 bug,得等 k8s 下一个大版本才能修。可生产环境的数据库等不起。
- **厂商锁定**——你写的 MySQL 控制器进了核心,别人写的 PostgreSQL 控制器也想进,谁来裁决?核心会变成各方利益博弈的战场。

### 所以这样设计:CRD + 自定义 controller = Operator

k8s 的解法极其优雅,叫 **CRD(CustomResourceDefinition)+ 自定义 controller**,合起来叫 **Operator 模式**。思路两步:

1. **CRD:让 apiserver 认识一种新资源**。你向 k8s 声明"我要一种新资源,叫 MySQLCluster,它的 spec 长这些字段"。apiserver 收到后,就把这种新资源**登记在册**——从此你 `kubectl apply` 一份 MySQLCluster 的 YAML,apiserver 会像对待 Deployment 一样,校验它、存进 etcd、提供 list/watch 接口。**apiserver 不需要懂"MySQL 集群是什么",它只负责把这种新资源当成一个通用的"对象"存起来、推出去。**
2. **自定义 controller:写一个 reconcile 循环管它**。你写一个普通的 Go 程序,用 client-go(就是前面讲的 informer 那套库)watch MySQLCluster 这种资源。收到变化,你的 reconcile 函数读"MySQL 集群该长什么样(spec 里写的:3 个副本、版本 8.0、备份策略……)"、读"实际长什么样(相关的 Pod、Service、PVC……)",算偏差,采取行动(创建 Pod、配置主从、触发备份)。**这个 controller 的骨架,和 Deployment 控制器一模一样——informer + workqueue + reconcile。**

> **比喻**:这是航运调度中心开放了"自定义运单类型"的接口。原来总部只认识"标准集装箱(Deployment)""冷藏集装箱(DaemonSet)"几种内置运单;现在你注册一种新运单"MySQL 集群运单"(CRD),总部就照单全收,帮你记账、帮你广播变化。至于"这种运单该怎么处理",你自己派一个督办(自定义 controller)驻在总部旁边,盯着这种运单的变化,按你写的规则 reconcile。**总部不操心你的业务逻辑,只提供"记账 + 广播"的基础设施。**

### Operator 的力量:把领域知识编码进 reconcile

Operator 模式的真正威力,在于它**把人类运维专家的知识,编码进了一个 reconcile 循环**。

一个熟练的 DBA,知道"MySQL 主库挂了,要把一个从库提升为主,然后重建其他从库"。这个知识,传统上是装在 DBA 脑子里的——DBA 写文档、带新人、出事故时手动操作。**Operator 把这个知识写进了 reconcile 函数**:控制器检测到"主库 Pod 挂了"(实际状态变化),reconcile 函数里写好的逻辑触发"提升从库、重建其他从库",自动执行。**DBA 的知识,从一个会走路的专家脑子里,变成了一个 7x24 小时运转的循环。**

这就是为什么云原生世界里有那么多 Operator:

- **Prometheus Operator**:管 Prometheus 监控实例的部署、扩缩容、配置更新。
- **Cert-Manager**:管 TLS 证书的签发、自动续期(证书快过期了,reconcile 检测到,自动申请新的)。
- **ArgoCD**:管"Git 仓库里的 YAML"和"集群里的实际状态"的一致性(Git 里改了,reconcile 把集群拉齐)。
- 各大数据库厂商(MySQL、PostgreSQL、MongoDB、Redis、Kafka……)几乎都有自己的官方 Operator。

**这些 Operator,底下都是同一副骨架——informer + workqueue + reconcile。** 你理解了本章讲的 Deployment 控制器,就理解了这些 Operator 的 90%——剩下的 10%,只是它们 reconcile 的资源不同、领域逻辑不同。

> **这就是声明式 + reconcile 范式的终极威力**:它不仅统一了 k8s 内置的所有控制器,还提供了一个**无限可扩展的框架**。任何人,只要会写 reconcile 循环,就能让 k8s 管一种新资源。**整个云原生生态的爆炸性繁荣,根源就在这里——一个统一的、可复制的范式,降低了"让 k8s 管新东西"的门槛到"写一个 Go 程序"的程度。**

---

## 关键源码精读:把 informer 的心脏钉死

理论讲完了,这一节我们把本章最核心的两段源码——informer 的事件分发心脏(`processDeltas`)和 Deployment 控制器的 worker 骨架(`processNextWorkItem` + `handleErr`)——并在一起,逐段对应前面讲的设计。这两段加起来不到 50 行,却是整个 k8s 的灵魂。

### 第一段:processDeltas,informer 的心脏

[controller.go 的 `processDeltas`](https://github.com/kubernetes/kubernetes/blob/master/staging/src/k8s.io/client-go/tools/cache/controller.go) —— DeltaFIFO Pop 出一组 Delta 后调用的处理函数:

```go
func processDeltas(
    logger klog.Logger,
    handler ResourceEventHandler,
    clientState Store,
    deltas Deltas,
    isInInitialList bool,
    keyFunc KeyFunc,
) error {
    for _, d := range deltas {                    // ① 遍历这组 Delta(从旧到新)
        obj := d.Object
        switch d.Type {
        case Sync, Replaced, Added, Updated:      // ② Add/Update 类:缓存里有就 Update,没有就 Add
            if old, exists, err := clientState.Get(obj); err == nil && exists {
                if err := clientState.Update(obj); err != nil {
                    return err
                }
                handler.OnUpdate(old, obj)
            } else {
                if err := clientState.Add(obj); err != nil {
                    return err
                }
                handler.OnAdd(obj, isInInitialList)
            }
        case Deleted:                              // ③ Delete 类:从缓存删
            if err := clientState.Delete(obj); err != nil {
                return err
            }
            handler.OnDelete(obj)
        // 省略 ReplacedAll / SyncAll / Bookmark 等批量类型
        }
    }
    return nil
}
```

逐段对应前面讲的设计:

- **① `for _, d := range deltas`**:对应第四节讲的"DeltaFIFO 按 key 聚合"——同一对象的多次变化攒成一组,这里一次性处理完。**避免了基于中间态的无效 reconcile。**
- **② Add/Update 合并处理**:`clientState.Get` 判断缓存里有没有——有就走 Update 路径(替换缓存 + 通知 OnUpdate),没有就走 Add 路径(加进缓存 + 通知 OnAdd)。**注意,这里的 OnAdd/OnUpdate 是 SharedInformer 自己的方法,它内部会广播给所有注册的外部 handler。**
- **③ Delete 单独处理**:从缓存删 + 通知 OnDelete。
- **顺序永远是"先动缓存,再通知 handler"**:保证 handler 被调用时,缓存已是最新。reconcile 从缓存读,读到的就是最新状态。

> **这一段为什么是"心脏"?** 因为它把"一个原始的 Delta(资源变化)"翻译成了"一次缓存更新 + 一次事件分发"——前者维持了本地世界快照,后者触发了 reconcile。**整个 informer 体系的价值,就浓缩在这个 switch 里**——它朴素到只是一个 for + switch,却撑起了 k8s 几万个容器的事件驱动。

### 第二段:processNextWorkItem + handleErr,worker 的骨架

[deployment_controller.go 的 worker](https://github.com/kubernetes/kubernetes/blob/master/pkg/controller/deployment/deployment_controller.go) —— 控制器消费 workqueue 的标准循环:

```go
func (dc *DeploymentController) processNextWorkItem(ctx context.Context) bool {
    key, quit := dc.queue.Get()        // ① 阻塞地从队列取 key
    if quit {
        return false
    }
    defer dc.queue.Done(key)           // ④ 标记处理完

    err := dc.syncHandler(ctx, key)    // ② 调 reconcile(syncDeployment)
    dc.handleErr(ctx, err, key)        // ③ 错误处理(重试/放弃)
    return true
}

func (dc *DeploymentController) handleErr(ctx context.Context, err error, key string) {
    if err == nil || errors.HasStatusCause(err, v1.NamespaceTerminatingCause) {
        dc.queue.Forget(key)                                   // 没错,忘掉重试历史
        return
    }
    if dc.queue.NumRequeues(key) < maxRetries {               // 重试没超限
        dc.queue.AddRateLimited(key)                          // 带退避延时,重新入队
        return
    }
    utilruntime.HandleError(err)                              // 超限,放弃
    dc.queue.Forget(key)
}
```

逐段对应前面讲的设计:

- **① `dc.queue.Get()`**:阻塞取——队列空就挂起等待,有 key 进来被唤醒。这是事件驱动的标准姿态,不空转。
- **② `dc.syncHandler(ctx, key)`**:调用 reconcile 函数(`syncDeployment`)。**这是"读期望、读实际、写回"真正发生的地方**——worker 的全部职责就是"取 key、调 reconcile"。
- **③ `dc.handleErr(ctx, err, key)`**:reconcile 的自愈式重试。成功就 Forget,失败且没超限就 AddRateLimited(带退避延时重新入队),超限就放弃。**这是"reconcile 对部分失败免疫"的源码落点。**
- **④ `defer dc.queue.Done(key)`**:标记处理完。处理期间如果这个 key 又被 Add 过(有新变化),Done 之后它会被重新放回队列,触发新一轮 reconcile。**保证"处理期间来的新变化不丢"。**

> **这两段合起来,就是 k8s 一切控制器的完整骨架。** 你打开任何一个内置控制器(ReplicaSet、DaemonSet、Job、StatefulSet……),或者任何一个 Operator,worker 都长这两段的模样——Get → syncHandler → handleErr → Done。**区别只在 syncHandler 里读什么资源、算什么偏差、采取什么行动。** 理解了这两段,你就拿到了打开云原生世界所有控制器源码的钥匙。

---

## 章末小结

### 用航运比喻回顾本章:调度中心的"情报 → 决策"流水线

回到港口。这一章我们做了一件事:**把第 14 章立的那个抽象的 reconcile 循环,钉死在源码上**。

答案是一条完整的"情报 → 决策"流水线,五个环节,每个环节都解决一个不解决就不行的问题:

```
   apiserver(台账)
        │
        │ ① list+watch
        ▼
   Reflector(情报员)──── 盯着台账,有变化第一个知道
        │
        │ ② 入队
        ▼
   DeltaFIFO(情报篮子)──── 按 key 聚合,合并短时间多次变化
        │
        │ ③ Pop
        ▼
   SharedInformer.handleDeltas(公告板)──── 更新本地缓存,广播给督办
        │
        │ ④ handler 入队
        ▼
   workqueue(待办盒)──── 去重、限速、重试
        │
        │ ⑤ worker 取 key
        ▼
   reconcile(督办动手)──── 读期望、读实际、写回
```

五个环节,五句话讲清:

1. **声明式把"目标"从"动作"里解放出来**——你只写"我要 3 个",不写"怎么得到 3 个"。目标变成了一个可比较、可收敛的数据结构,reconcile 才有了被拉向的对象。
2. **Reflector 用 list+watch 感知世界**——list 一次建基线,watch 增量订阅变化,resourceVersion 保证不丢。避免了"几百个组件轮询打死 apiserver"。
3. **DeltaFIFO 按 key 聚合 Delta**——同一个对象的多次变化攒成一组,避免基于中间态的无效 reconcile。生产者(Reflector)和消费者(controller)解耦。
4. **SharedInformer 更新本地缓存 + 广播事件**——本地缓存是 informer 替控制器维护的"世界快照",reconcile 读它不打扰 apiserver;广播让多个 handler 共享一个 informer(几百个组件在 apiserver 看来只是一个 watch 客户端)。
5. **workqueue + worker 把"被通知"和"动手 reconcile"解耦**——handler 极快地入队(不阻塞 informer),worker 按自己节奏取 key、调 reconcile、失败自动重试。**Get → syncHandler → handleErr → Done 是所有控制器的共同骨架。**

### 本章在全书主线中的位置:reconcile 范式的源码落点

记住全书的二分法:**打包隔离 vs 调度编排。**

这一章,我们把**调度编排这一侧的灵魂**钉死了:

- 第 14 章立了第一性原理(编排 = 声明 + reconcile),但只给了伪代码。
- 第 15 章搭了架构骨架(控制平面 + 节点 + informer 三件套),但 informer 还是个黑盒。
- **这一章,兑现了前两章的承诺**——把 reconcile 循环从一句口号,变成了一条五环节的流水线、一段段真实的源码。从此,"reconcile"对你不再是一个抽象的词,而是 `processDeltas` 里那个 switch、`processNextWorkItem` 里那四行 Get/syncHandler/handleErr/Done。

后面三章,都是在这套骨架之上展开的具体细节:

- **第 18 章 · 调度器**:本章 reconcile 里"补到哪个节点"这一步,具体怎么决策(filter + score 两阶段)。调度器本身也是一个"watch pending Pod + reconcile(给它找个节点)"的控制器,只是它的 reconcile 产物是 bind。
- **第 19 章 · kubelet**:本章 reconcile 决定"这个节点上要有这个 Pod"之后,谁真正把它跑起来。kubelet 在节点上做着和本章一模一样的事——watch 分配到自己的 Pod,经 CRI 调 containerd 启动容器,反向同步状态。
- **第 20 章 · Service 与 kube-proxy**:服务发现这件事,也是同一个 reconcile 范式——kube-proxy watch Service 和 Pod 的变化,reconcile 出本地的 iptables/ipvs 规则。

每一章,你都会听到本章那条流水线的回响——**list+watch → 排队 → 分发 → workqueue → reconcile**。这是 k8s 的"宪法",后面所有组件都是在这部宪法之下展开的具体法律。

### 五个"为什么"清单

如果你只能记五件事,记这五件:

1. **为什么是声明式不是命令式**:命令式把"目标"和"动作"焊死,只留下动作、丢掉目标,系统失去了"被比较"的能力,无法 reconcile。声明式只让你写"目标",目标变成一个可比较、可收敛的数据结构——幂等、自愈、可收敛这三个特性是**白送的**,从这个范式里自然长出来。
2. **informer 五件套各解决什么问题**:Reflector 解决"怎么感知世界"(list+watch,避免轮询打死 apiserver);DeltaFIFO 解决"怎么整理散落变化"(按 key 聚合,合并短时间多次变化);SharedInformer 解决"怎么让多个组件共享一份世界快照"(本地缓存 + 广播);Indexer 解决"怎么在海量对象里快速查"(带索引的本地缓存);workqueue 解决"怎么不被慢 reconcile 拖死 + 失败重试"(handler 只入队,worker 出队才 reconcile)。
3. **为什么 handler 不直接 reconcile**:handler 直接 reconcile 会阻塞 informer、触发重复 reconcile、失败没人重试。解耦成"handler 入队 + worker 出队",三个问题全解——handler 极快不阻塞、workqueue 去重合并、worker 失败自动 AddRateLimited 重试。
4. **所有控制器的共同骨架是什么**:`Get → syncHandler → handleErr → Done`。从内置的 Deployment/ReplicaSet/DaemonSet,到 Prometheus Operator/Cert-Manager/ArgoCD,worker 都长这副模样。区别只在 syncHandler 读什么、算什么、做什么。
5. **为什么这个范式能无限扩展(CRD + Operator)**:CRD 让 apiserver 认识新资源(只记账广播,不操心业务),自定义 controller 用同一套 informer 骨架管这种新资源。**任何人会写 reconcile 循环,就能让 k8s 管一种新东西**——这是云原生生态爆炸性繁荣的根源。

### 想继续深入,该往哪钻

- **亲手感受 informer**:用 `kubectl get pods -w`(那个 `-w` 就是 watch)感受事件流。再试试 `kubectl get --raw "/apis/apps/v1/deployments?watch=true&resourceVersion=0"`,你能看到 watch 返回的原始事件流——本章 Reflector 收到的就是这种东西。
- **顺着本章的源码走一遍**:打开 [staging/src/k8s.io/client-go/tools/cache](https://github.com/kubernetes/kubernetes/tree/master/staging/src/k8s.io/client-go/tools/cache),按本章第五节的流水线图,从 `shared_informer.go` 的 `Run` → `handleDeltas`(953 行,小写)→ `controller.go` 的 `processLoop`(236 行)→ `processDeltas`(607 行)→ `delta_fifo.go` 的 `Pop`(562 行)→ `reflector.go` 的 `ListAndWatchWithContext`(470 行)/ `handleAnyWatch`(972 行),顺一遍。这是理解 informer 的最短路径。
- **读一个真实 controller 的全貌**:打开 [pkg/controller/deployment/deployment_controller.go](https://github.com/kubernetes/kubernetes/blob/master/pkg/controller/deployment/deployment_controller.go),从 `NewDeploymentController`(104 行,装配)→ `Run`(171 行,启动 worker)→ `worker`(481 行)→ `processNextWorkItem`(486 行)→ `handleErr`(499 行)→ `syncDeployment`(574 行,reconcile),把本章讲的骨架在源码里走通。
- **写一个最小 Operator**:用 [kubebuilder](https://book.kubebuilder.io/) 或 Operator SDK,生成一个最小 controller 的脚手架。你会发现生成的代码里,informer + workqueue + reconcile 的骨架和本章讲的 Deployment 控制器一模一样——**你只需要填 reconcile 函数的领域逻辑,剩下的基础设施 client-go 全替你写好了**。这是体会"Operator 模式为什么门槛低"的最好方式。
- **想理解 workqueue 的限速和重试细节**:打开 [staging/src/k8s.io/client-go/util/workqueue](https://github.com/kubernetes/kubernetes/tree/master/staging/src/k8s.io/client-go/util/workqueue),看 `rate_limiting_queue.go` 和 `default_rate_limiters.go`——指数退避、令牌桶限速的实现都在里面。本章 `handleErr` 里调的 `AddRateLimited`,底下就是这些。

---

> reconcile 的源码骨架立住了:**声明目标 → list+watch 感知世界 → 排队分发 → workqueue 串行处理 → reconcile 读期望读实际写回 → 失败自动重试**。这套骨架是 k8s 一切控制器的共同DNA,也是云原生生态爆炸性繁荣的根基。但有个问题本章一直绕着走——**reconcile 里"补到哪个节点"这一步,具体是怎么决策的?** 几百个节点里挑一个最合适的,这是另一个精妙的子系统。下一章,我们拆开调度器,看它的 filter + score 两阶段是怎么把"这个 Pod 该放哪"算出来的。翻开 **第 18 章 · 调度器:Pod 放到哪个节点**。
