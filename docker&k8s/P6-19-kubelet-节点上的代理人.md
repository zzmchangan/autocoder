# 第 19 章 · kubelet:节点上的代理人

> **前置**:你需要先读完[第 15 章《k8s 架构:控制平面与节点》](P5-15-k8s架构-控制平面与节点.md)——它已经把 kubelet 摆进了整张架构图,并点名它是每艘船(节点)上的"驻港经理",干两件事:把控制平面派下来的期望 Pod 真正跑起来、把节点和 Pod 的真实状态反向同步回去。但那一章对 kubelet 只用一句话带过,留了一连串没回答的问题——**这个驻港经理,内部到底是怎么运转的?它怎么知道"我这艘船上应该有哪些 Pod"?它又是通过什么手段真正把一个 Pod 跑起来的(它自己又不直接管容器)?一个容器在它眼皮底下挂了,它凭什么知道、又凭什么能拉起来?节点上发生了那么多事,它怎么把"真相"源源不断地报回总部的账本(etcd)?** 这一章,我们就钻进 kubelet 的源码,把这些问题一个一个钉死。
>
> 同时本章会和[第 10 章《containerd》](P3-10-containerd-高层运行时管什么.md)(CRI)、[第 13 章《CNI》](P4-13-CNI-容器网络接口.md)(容器网络)直接呼应——kubelet 真正"动手"启动容器的那一步,调的就是第 10 章讲的 CRI;而 Pod 的网络配线,正是第 13 章讲的 CNI。

> **核心问题**:控制平面说"这个节点上要有这个 Pod",谁来真正把它跑起来?节点状态又怎么报回去?

> 这一章我们打开 kubelet 这个黑盒。你会发现,kubelet **本身就是一个 reconcile 循环**——它和第 15 章讲过的 controller-manager 里的那些督办,本质是同一套范式,只不过它 reconcile 的对象从"集群里的 Deployment/ReplicaSet"缩小成了"我这台机器上的容器"。它从 apiserver 订阅"我这台节点该有哪些 Pod"(期望),不停地把"我这台机器上实际跑着的容器"往这个期望拉(多了杀、少了起、挂了重启);它再把自己看到的真相反向写回 etcd,让总部的 reconcile 有依据。**它是控制平面的手,也是节点的嘴。**

> **读完本章你会明白**:
> - 为什么 kubelet **自己也是一个 reconcile 循环**——它不是"被动接命令"的,而是主动订阅"我这台节点该有哪些 Pod",然后不停地把实际容器往期望拉。这和第 14、15 章立的那套"声明期望 + 自动调谐"范式,在节点这一层是同一个套路。
> - 一个期望的 Pod,是怎么被 kubelet 翻译成对 containerd 的 **CRI 调用**、最终变成跑着的容器的——这条链子 `apiserver → kubelet.syncLoop → podWorkers → kubeGenericRuntimeManager.SyncPod → RunPodSandbox/CreateContainer → containerd → runc`,是 k8s 上一个 Pod 真正"落地"的完整路径,也是回扣第 9、10 章的地方。
> - **PLEG(Pod Lifecycle Event Generator)** 凭什么能发现"一个容器挂了"——它不是事件推送,而是 kubelet 每秒主动去问一遍 CRI 运行时"现在每个 Pod 里有哪些容器、什么状态",拿新旧两次快照做 diff。这个"轮询而不是事件"的设计,有个很现实的理由。
> - 节点和 Pod 的真实状态,是怎么**反向同步**回 apiserver 的——Pod 的 `status` 字段(Phase / Conditions / ContainerStatuses)**不是你写的,也不是 apiserver 写的,而是 kubelet 写的**。status manager 拿到本地真相,再 PATCH 回 etcd,这才让总部的 reconcile 有真相可依。
> - 为什么 **Pod 网络由 CRI 运行时(containerd)在 `RunPodSandbox` 时配 CNI**,kubelet 只通过 CRI 触发它、**不直接调 CNI**——回扣第 13 章。

> **如果一读觉得太难**:kubelet 内部组件特别多(podWorkers / statusManager / pleg / probeManager / podManager / runtimeManager / volumeManager / evictionManager / imageGC / containerGC……),很容易把人淹没。先只记住三件事——
> ① **kubelet 是个小 controller**:它订阅"我这台节点该有的 Pod",不停把实际容器往期望拉(多了杀、少了起、挂了重启),和 controller-manager 同一个范式。
> ② **它通过 CRI 让 containerd 干活**:kubelet 自己不直接管容器,它把"起一个 Pod"翻译成 `RunPodSandbox` + 一堆 `CreateContainer`/`StartContainer` 的 gRPC 调用,交给 containerd。
> ③ **它反向同步真相**:PLEG 每秒轮询运行时拿真相,statusManager 把真相 PATCH 回 apiserver。**Pod 的 status 字段是 kubelet 写的**,这是总部 reconcile 的依据来源。
> 这三句话钉死,本章你就抓住了 80%,剩下的细节(各个 manager、各种 channel)可以慢慢填。

---

## 章首·一句话点破

第 15 章那张架构图里,kubelet 是一个画在每艘货轮(节点)圆圈里的小方框,旁边写着"驻港经理"。我们当时只说它"把期望的 Pod 真正跑起来、把状态反向同步",把所有细节都吞进了一个黑盒。

这一章,我们打开这个黑盒。要做的第一件事,是连根拔起一个对 kubelet 最常见的误解:

> **kubelet 不是"被 apiserver 远程调用、然后启动容器"的被动服务员。它是一个自主的常驻进程,自己盯着 apiserver 上分配给自己这台节点的 Pod,自己 reconcile、自己上报。apiserver 从不直接告诉 kubelet "你去启动这个容器"——apiserver 只是把"这个 Pod 绑定到了 node-1"写进 etcd,然后 kubelet 自己 watch 到这个变化,自己决定动手。**

这条认知,是理解 kubelet 全部行为的钥匙。我们一块一块拆。

---

## 一、为什么必须有个 kubelet:控制平面是"动嘴的",节点上得有"动手的"

要理解 kubelet 为什么存在,最直接的办法是问:没有它会怎样?

### 不这样会怎样:控制平面只会写账,不会动手

回忆第 15 章。整个控制平面——apiserver、etcd、scheduler、controller-manager——**没有哪一个真正碰过容器**。它们全在"办公室"里:

- apiserver 收单、校验、记账;
- etcd 存这本账;
- scheduler 决定一个新 Pod 该上哪艘船,把这个决定(bind)写进账;
- controller-manager 里的几十个督办,盯着账本做决策("期望 3 个、实际 2 个,补一个"),但它们补的方式是——**往账本里写一条新的、pending 的 Pod,等 scheduler 分配节点**。

注意:这条链子从头到尾,**没有一个组件真正启动过哪怕一个容器**。它们都只是在"写账、读账、决策"。账本里现在写着"node-1 上应该有 web-xxx 这个 Pod",可 node-1 上**根本没人**去把这个 Pod 真跑起来——除非那台机器上有一个会主动看账、会动手的角色。

> **比喻**:这就好比航运总部的台账上写着"3 号货轮上要有 5 个集装箱"。可台账自己不会吊集装箱。3 号货轮上得有个**长期驻港的经理**——他每天去查台账(经 apiserver),"总部说该有哪几个箱",然后亲自去码头组织吊装、通电、固定;箱子坏了试着重启;每天把"我这船上现在真跑着哪些"报回总部。**这个人,就是 kubelet。**

### 所以这样设计:每艘船上一个常驻代理人

kubelet 就是这个驻港经理。它在**每一台工作节点**上以常驻进程的形式跑着(systemd 起的一个 `kubelet` 进程),干的活儿可以拆成三件:

1. **听令**(watch):它通过 informer(第 15 章讲过)订阅 apiserver 上"分配到我这台节点的 Pod"。一旦总部把一个 Pod 绑定(bind)到我这台节点,我立刻就能知道。
2. **动手**(sync):它把这个期望的 Pod,通过 CRI 翻译成对 containerd 的调用,在本机上**真正**把容器跑起来;容器挂了,它负责按重启策略拉起来;Pod 被删了,它负责把容器优雅杀掉、清理干净。
3. **回报**(status sync):它不停地把"我这台节点上现在真跑着哪些 Pod、它们什么状态、节点本身资源够不够、健康不健康"反向写回 apiserver(最终落进 etcd)。**总部做决策的依据,全部来自这一路上报。**

这三件事里,"动手"是它和别的控制平面组件最不一样的地方——**kubelet 是 k8s 全集群里唯一一个真正会启动容器的组件**(通过 CRI)。它是控制平面那套"嘴上功夫"和真实容器世界之间的桥梁。

> **一个小但关键的细节:apiserver 从不直接调用 kubelet**。你可能以为总部要"命令" kubelet 干活,得有个 RPC。没有。kubelet 是**自己主动去 watch apiserver** 的——它和 Deployment 控制器、scheduler 用的是同一套 informer 机制。总部只负责把"该有的状态"写进 etcd,kubelet 自己感知、自己行动。**这种"没有显式命令、全靠围绕共享状态的 watch-reconcile"的协作方式,是 k8s 整套架构的灵魂**(第 15 章已经反复强调过),kubelet 在节点这一层把它复刻了一遍。

### 那 kubelet 自己怎么知道"我这台节点叫什么"?

一个自然的疑问:kubelet 启动时,它怎么知道自己是哪台节点、该 watch 哪些 Pod?

答案是 kubelet 启动时带了一个参数 `--hostname-override`(或默认用机器主机名),它知道"我是 node-1"。然后它向 apiserver 注册自己:在 etcd 里创建(或更新)一个 Node 对象,声明"node-1 存在,它的 IP 是 X、容量是 Y、装了哪些容器运行时"。之后它的 informer 就只 watch `spec.nodeName == node-1` 的 Pod——别的节点的 Pod,它一概不管。

> **比喻**:驻港经理上岗第一件事,是向总部报到:"我是 3 号货轮的经理,我这船载重多少、烧什么油。" 总部把这条信息记进台账(Node 对象)。之后,凡是台账上写着"这个箱子上 3 号船"的单子,才会推给这位经理。

讲清了"为什么要有 kubelet",接下来拆它的内部。我们先看它最反直觉的一面:**它自己就是一个 reconcile 循环**。

---

## 二、kubelet 自己就是个 reconcile 循环:期望 Pod vs 实际容器

这是本章最重要、也最容易被忽略的一个认知。很多人以为 kubelet 是"收到一条命令、执行一条命令"的执行器。不是的。

> **kubelet 内部跑着一个叫 `syncLoop`(同步循环)的主循环。这个循环干的事,和 controller-manager 里的 Deployment 控制器本质上是一模一样的:盯着"期望状态"(我这台节点该有哪些 Pod),对比"实际状态"(我这台节点上真跑着哪些容器),算出偏差,然后采取行动——该起的起、该杀的杀、该重启的重启。**

### 不这样会怎样:如果 kubelet 是"一次性执行器"

假设 kubelet 设计成"收到一个 ADD 事件,就启动这个 Pod,然后完事"。听起来也够用?可一旦容器世界里发生这些事,它就抓瞎了:

- **容器自己挂了**:Pod 还在期望里(没被删),但容器进程退出了。一次性执行的 kubelet 不会发现,容器就这么死着,直到有人手动重启——回到了第 14 章讲的"故障不自愈"的黑暗时代。
- **容器被人在宿主上手动 `docker kill` 了**:期望里这个 Pod 还在,实际容器没了。kubelet 不主动对比,就发现不了这个偏差。
- **Pod 的配置变了**(比如改了镜像版本):kubelet 得把这个变化翻译成"杀掉旧容器、起新容器"。一次性执行器没有"持续对比"的概念。
- **Pod 被删了**:kubelet 得知道"这个期望没了,我得把对应的容器清掉"。

这四件事,本质上都是同一类问题:**期望和实际会持续偏离,reconcile 必须是一个永不停歇的循环,而不是一次性的动作**。这正是第 14、17 章立的范式——kubelet 在节点这一层把它原样复刻。

### 所以这样设计:syncLoop,一个 select 多路事件的主循环

kubelet 的主循环叫 `syncLoop`,它在 kubelet 启动时被 `Run` 拉起。我们先看它的入口(后面"关键源码精读"会逐行拆):

```go
// pkg/kubelet/kubelet.go
func (kl *Kubelet) syncLoop(ctx context.Context, updates <-chan kubetypes.PodUpdate, handler SyncHandler) {
    // ... 准备几个 ticker ...
    syncTicker := time.NewTicker(time.Second)            // 每秒触发一次兜底同步
    housekeepingTicker := time.NewTicker(housekeepingPeriod)
    plegCh := kl.pleg.Watch()                             // PLEG 的事件通道

    for {
        // 如果运行时不健康,退避等待
        if err := kl.runtimeState.runtimeErrors(); err != nil { ... }

        // 一轮:从五条 channel 之一读事件,分发给 handler
        if !kl.syncLoopIteration(ctx, updates, handler, syncTicker.C, housekeepingTicker.C, plegCh) {
            break
        }
    }
}
```

(参见 [`pkg/kubelet/kubelet.go` 的 `syncLoop`](https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/kubelet.go) 与其调用的 `syncLoopIteration`。)

读这段,先抓住一件事:**`syncLoop` 是一个 `for { ... }` 死循环,每一轮调用一次 `syncLoopIteration`**。而 `syncLoopIteration` 是一个 **`select`**,它同时盯着**五条 channel**,哪条来东西就处理哪条:

```go
func (kl *Kubelet) syncLoopIteration(ctx context.Context, configCh <-chan kubetypes.PodUpdate, handler SyncHandler,
    syncCh <-chan time.Time, housekeepingCh <-chan time.Time, plegCh <-chan *pleg.PodLifecycleEvent) bool {

    select {
    case u, open := <-configCh:        // ① 来自 apiserver 的 Pod 配置变化(ADD/UPDATE/REMOVE/DELETE)
        switch u.Op {
        case kubetypes.ADD:    handler.HandlePodAdditions(ctx, u.Pods)
        case kubetypes.UPDATE: handler.HandlePodUpdates(ctx, u.Pods)
        case kubetypes.REMOVE: handler.HandlePodRemoves(ctx, u.Pods)
        // ...
        }

    case e := <-plegCh:                // ② PLEG 发现的"容器生命周期事件"(某容器死了/起了)
        if pod, ok := kl.podManager.GetPodByUID(e.ID); ok {
            handler.HandlePodSyncs(ctx, []*v1.Pod{pod})   // 重新同步这个 Pod
        }
        if e.Type == pleg.ContainerDied { /* 清理死容器 */ }

    case <-syncCh:                     // ③ 定时兜底:每秒检查"有没有该同步的 Pod"
        handler.HandlePodSyncs(ctx, podsToSync)

    case update := <-kl.livenessManager.Updates():  // ④ 健康探针的结果(某容器 liveness 失败)
        if update.Result == proberesults.Failure { /* 标记容器不健康,触发重启 */ }

    case <-housekeepingCh:             // ⑤ 定时清理(已死容器的回收、日志轮转)
        // ...
    }
    return true
}
```

(参见 [`syncLoopIteration`](https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/kubelet.go)。以上为简化展示,保留 select 五路结构,实际源码细节更多。)

这个 `select` 是理解 kubelet 的核心。它告诉一件很重要的事:**kubelet 的 reconcile,是被五类事件驱动的**——

1. **configCh**(配置变化):总部改了"我这台节点该有的 Pod"(新建、更新、删除)。这是最直接的事件源——你 `kubectl apply` 一个 Deployment,最终 scheduler 把 Pod bind 到这台节点,这个变化经 informer 流进 configCh。
2. **plegCh**(容器生命周期事件):PLEG 发现"一个本来在跑的容器死了"或者"一个新容器起来了"。**这是节点本地真相变化的事件源**——总部没改期望,但容器自己挂了,这个事件也会触发一轮 reconcile,让 kubelet 去按重启策略把它拉回来。下一节专讲 PLEG。
3. **syncCh**(定时兜底):每秒触发一次,检查"有没有长时间没同步的 Pod"。这是个**保险阀**——万一某个事件丢了、某个 reconcile 失败了,这把每秒一次的扫帚会兜底把它重新捡起来。
4. **livenessManager.Updates()**(健康探针):探针发现"这个容器虽然进程还在,但应用已经不响应了(liveness 失败)"。kubelet 据此杀掉并重启这个容器。第六节细讲。
5. **housekeepingCh**(清理):定时做家务——回收已经退出的死容器、轮转日志、清理孤儿。

> **这个 select 的精妙在于**:它把"外部命令"(configCh)、"本地真相变化"(plegCh)、"应用健康度"(livenessManager)、"定时兜底"(syncCh)和"做家务"(housekeepingCh)这**五个完全不同来源的触发**,统一成同一种处理——**都是触发一次 reconcile**。不管是谁喊的"该干活了",kubelet 的回应都是同一套:对比期望和实际,该起起该杀杀。**这正是 reconcile 范式的精髓:不管扰动来自哪里,我的反应永远是"重新对齐期望和实际"。**

### 一个 Pod 触发后,谁来真正动手:podWorkers,每 Pod 一个 goroutine

`syncLoopIteration` 收到事件后调的 `HandlePodAdditions`/`HandlePodSyncs` 等,最后都会调到一个关键方法:[`podWorkers.UpdatePod`](https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/pod_workers.go)。这是 kubelet "把一个 Pod 交给工人去干"的入口。

`UpdatePod` 做的事很精妙:**它给每个 Pod 分配一个独立的 goroutine(叫 pod worker),专门负责这个 Pod 的全部生命周期**。看它最关键的那几行:

```go
// pkg/kubelet/pod_workers.go
func (p *podWorkers) UpdatePod(ctx context.Context, options UpdatePodOptions) {
    // ... 解析 pod UID ...
    status, ok := p.podSyncStatuses[uid]
    if !ok {
        // 这个 Pod 第一次被同步:为它建一个状态记录,并 spawn 一个专属 worker goroutine
        status = &podSyncStatus{ syncedAt: now, fullname: ... }
        p.podSyncStatuses[uid] = status
        // ...
        go func() {
            defer runtime.HandleCrash()
            p.podWorkerLoop(ctx, uid, outCh)   // ← 这个 Pod 的专属循环,跑到 Pod 被彻底销毁
        }()
    }

    // 把这次更新塞进这个 Pod 的待处理队列,通知它的 worker
    status.pendingUpdate = &options
    select {
    case podUpdates <- struct{}{}:    // 唤醒那个 goroutine
    default:
    }
}
```

(参见 [`pod_workers.go` 的 `UpdatePod`](https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/pod_workers.go)。简化展示,保留"首次 spawn goroutine + pendingUpdate + 通知"的核心逻辑。)

读这段,抓住三件事:

1. **每个 Pod 一个 goroutine**。当一个 Pod 第一次进到这台节点,`UpdatePod` 会 `go func() { p.podWorkerLoop(...) }()` 起一个**专属 goroutine**。从这一刻起,这个 Pod 的所有事(创建、重启、终止)都由这个 goroutine 串行处理,直到这个 Pod 被彻底销毁,goroutine 才退出。
2. **pendingUpdate + channel 通知**。新的更新进来,不是立刻执行,而是塞进这个 Pod 的 `pendingUpdate`(注意:如果短时间内来了多次更新,后到的会和 pending 的合并——这避免了对同一个 Pod 的频繁、重复 reconcile)。
3. **串行化**。同一个 Pod 的多次更新,在这个 worker 里是**排队串行处理**的,不会并发。这非常重要——它从根本上避免了"两个 goroutine 同时去启动同一个 Pod 的容器"这种竞争。

那个 goroutine 里跑的 `podWorkerLoop`,核心是反复从 channel 取通知、然后调用真正的 `syncPod`(下面"关键源码精读"会贴)。**真正"动手启动/杀掉容器"的逻辑,就在这个 `syncPod` 里。**

讲到这里,kubelet 作为"小 controller"的骨架已经清楚了:**五路事件 → syncLoopIteration 分发 → HandlePod* → podWorkers.UpdatePod → 每 Pod 一个 goroutine → syncPod 真正动手**。接下来我们看 `syncPod` 到底干了什么——也就是 kubelet 怎么把一个期望的 Pod,真正变成跑着的容器。

---

## 三、怎么真正起容器:回扣第 10 章 CRI 调用链

这一节是和第 9、10 章的回合。kubelet 自己不会"建 namespace、设 cgroup、pivot_root"——这些是 runc 的活(第 9 章)。kubelet 甚至不直接管镜像、不管快照、不直接管容器生命周期——这些是 containerd 的活(第 10 章)。kubelet 干的,是**把"一个期望的 Pod"翻译成对 CRI 运行时的调用**。

### 先回顾:CRI 是什么、为什么需要它

第 10 章讲过,**CRI(Container Runtime Interface)是 k8s 和底层容器运行时之间的一套标准 gRPC 接口**。它存在的理由是解耦:k8s 不想绑死 docker,也不想绑死 containerd——它想能换成 CRI-O、换成别的运行时,而 k8s 自己一行代码都不用改。

CRI 把"运行时该干什么"抽象成两组接口:

- **`RuntimeService`**:管 Pod 沙箱和容器——`RunPodSandbox` / `StopPodSandbox` / `RemovePodSandbox` / `CreateContainer` / `StartContainer` / `StopContainer` / `RemoveContainer` / `ListPodSandbox` / `ListContainers` / `ContainerStatus` / `ExecSync` ……
- **`ImageService`**:管镜像——`ListImages` / `ImageStatus` / `PullImage` / `RemoveImage` / `ImageFsInfo` ……

kubelet 内部有一个叫 [`kubeGenericRuntimeManager`](https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/kuberuntime/kuberuntime_manager.go) 的组件(实现了 kubecontainer.Runtime 接口),它就是把"k8s 的 Pod 语义"翻译成"CRI 的 gRPC 调用"的那一层。**它持有的 `runtimeService`,就是一个连到 containerd 的 CRI gRPC 客户端。**

> **比喻**:驻港经理(kubelet)不会自己开起重机(runc)、也不会自己管堆场(containerd)。他只会对着对讲机喊一套**标准口令**(CRI):"给我建一个 3 号箱的沙箱(RunPodSandbox)""在沙箱里建一个叫 web 的箱子(CreateContainer)""启动这个箱子(StartContainer)""这个箱子里要装 nginx:1.25,先去仓库拉一个(PullImage)"。这套口令是标准的——不管对讲机那头是 containerd 还是 CRI-O,只要听得懂这套口令,经理就能指挥它干活。**这正是 CRI 这层抽象的价值。**

### syncPod 的"八步"流程

`podWorkerLoop` 真正调的那个 `syncPod`,实现在 `kubeGenericRuntimeManager.SyncPod`。这是本章最核心的一段代码——它把"对比期望和实际、决定该干什么、然后通过 CRI 干"的逻辑,浓缩成了一个清晰的八步流程。我们看它的骨架:

```go
// pkg/kubelet/kuberuntime/kuberuntime_manager.go
func (m *kubeGenericRuntimeManager) SyncPod(ctx context.Context, pod *v1.Pod, podStatus *kubecontainer.PodStatus,
    pullSecrets []v1.Secret, backOff *flowcontrol.Backoff, restartAllContainers bool) (result kubecontainer.PodSyncResult) {

    // Step 1: 算出"该干什么"——沙箱要不要重建?哪些容器该杀、哪些该起?
    podContainerChanges := m.computePodActions(ctx, pod, podStatus, restartAllContainers)

    // Step 2: 如果沙箱变了(要重建),先把整个 Pod 杀掉
    if podContainerChanges.KillPod {
        killResult := m.killPodWithSyncResult(...)
        // ...
    } else {
        // Step 3: 否则,只杀掉那些"不该存在"的容器(比如配置变了、版本旧了)
        for containerID, containerInfo := range podContainerChanges.ContainersToKill {
            m.killContainer(ctx, pod, containerID, containerInfo.name, ...)
        }
    }

    // ...

    // Step 4: 如果需要,建一个新的 Pod 沙箱(这一步会真正配 CNI 网络!见下一节)
    podSandboxID := podContainerChanges.SandboxID
    if podContainerChanges.CreateSandbox {
        // ... 调 createPodSandbox → runtimeService.RunPodSandbox ...
        podSandboxID, msg, err = m.createPodSandbox(ctx, pod, podContainerChanges.Attempt)
        // ...
    }

    // Step 5: 启动 init 容器(逐个、串行,前一个成功才下一个)
    // Step 6: 拉镜像(如果还没拉)
    // Step 7: 逐个创建并启动业务容器(create + start)
    // Step 8: 记录结果
    return result
}
```

(参见 [`kuberuntime_manager.go` 的 `SyncPod`](https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/kuberuntime/kuberuntime_manager.go)。源码原始注释里就标着 `Step 1` ~ `Step 8`,这里保留了主要的几个。)

读这段,重点抓两件事:

**一、Step 1 的 `computePodActions` 是整个 reconcile 的"大脑"。** 它拿到"期望的 Pod 配置"(spec)和"实际这个 Pod 现在的容器状态"(podStatus),算出一份"该干的事"清单:

- 沙箱(PodSandbox)还在不在?变了没?要不要重建?
- 每个该有的容器(web、sidecar),现在跑着吗?跑着的版本对不对?(镜像 tag 变了就算"该杀该重建")
- 有没有"不该有的容器"(比如一个已经从 spec 里删掉的旧 sidecar 还赖着)?

这就是 reconcile 的核心动作——**对比期望和实际,算出偏差**。和 Deployment 控制器数"期望 3 个、实际 2 个"是一回事,只是这里精细到了"每个容器"。

**二、Step 4 的 `createPodSandbox` 是 CRI 调用的入口,也是 Pod 网络配线的入口。** 我们重点看它:

```go
// pkg/kubelet/kuberuntime/kuberuntime_sandbox.go
func (m *kubeGenericRuntimeManager) createPodSandbox(ctx context.Context, pod *v1.Pod, attempt uint32) (string, string, error) {
    podSandboxConfig, err := m.generatePodSandboxConfig(ctx, pod, attempt)   // 拼出 CRI 的 PodSandboxConfig
    // ... 建日志目录、解析 RuntimeClass ...

    // 关键这一行:把"建沙箱"翻译成 CRI 的 RunPodSandbox 调用,发给 containerd
    podSandBoxID, err := m.runtimeService.RunPodSandbox(ctx, podSandboxConfig, runtimeHandler)
    // ...
    return podSandBoxID, "", nil
}
```

(参见 [`kuberuntime_sandbox.go` 的 `createPodSandbox`](https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/kuberuntime/kuberuntime_sandbox.go) 第 67 行——`m.runtimeService.RunPodSandbox(ctx, ...)`。)

**就是这一行 `RunPodSandbox`,把一个 Pod 的"沙箱"(pause 容器 + 它占住的共享 network namespace)建了出来**——而这个动作一旦成功,Pod 的网络配线(CNI)就由 containerd 在背后替你配好了。

### 回扣第 13 章:CNI 是 containerd 调的,不是 kubelet 调的

这是一个经常被讲错的点,第 13 章已经核实过,这里再强调一遍——

> **Pod 的 CNI 网络,是 CRI 运行时(containerd)在执行 `RunPodSandbox` 时配的,不是 kubelet 直接调 CNI 插件配的。** kubelet 只通过 CRI 触发了 `RunPodSandbox` 这一步;至于这一步内部是怎么建 pause 容器、怎么创 network namespace、怎么调 CNI 插件给它分配 IP、配路由——**全是 containerd 的内部细节,kubelet 一概不碰。**

> **比喻**:驻港经理对对讲机喊一声"给我建一个 3 号箱的沙箱"(RunPodSandbox),至于这个沙箱内部的水电网络(CNI)是怎么接的——拉哪根线、接到哪个交换机、配什么 IP——那是码头(containerd)自己的事,经理不关心也看不见。**经理只对"沙箱建好了没"这个结果负责。**

这种分工的好处是:**kubelet 不用绑定任何一种 CNI 实现**。你换 calico、换 flannel、换 cilium——kubelet 的代码一行都不用改,因为它压根没参与 CNI 调用。CNI 是 containerd(或 CRI-O)和插件之间的事(第 13 章)。

讲清了"怎么起容器",下一节看 kubelet 怎么发现"容器挂了"——这是它作为 controller 能自愈的前提。

---

## 四、PLEG:怎么发现"一个容器挂了"

reconcile 要能工作,前提是"kubelet 知道实际状态"。但实际状态(这台机器上每个容器在不在跑、是不是刚死了)kubelet 怎么知道?**这就是 PLEG(Pod Lifecycle Event Generator)的活儿。**

### 先看一个 PLEG 出事件的样子

PLEG 是 kubelet 内部的一个组件,它持续地把"容器世界的变化"翻译成一种叫 **PodLifecycleEvent** 的事件,塞进一个 channel(`plegCh`)。前面 `syncLoopIteration` 的 `case e := <-plegCh:` 读的就是它。

事件类型就几种(来自 PLEG 的真实源码):

```go
// 容器相关的事件
return []*PodLifecycleEvent{{ID: podID, Type: ContainerStarted, Data: cid}}   // 一个容器起来了
return []*PodLifecycleEvent{{ID: podID, Type: ContainerDied, Data: cid}}       // 一个容器死了
return []*PodLifecycleEvent{
    {ID: podID, Type: ContainerDied, Data: cid},                                // 死了
    {ID: podID, Type: ContainerRemoved, Data: cid},                            // 然后被清掉了
}
```

(参见 [`pkg/kubelet/pleg/generic.go` 的 `generateEvents`](https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/pleg/generic.go),事件类型 `ContainerStarted`/`ContainerDied`/`ContainerRemoved`/`ContainerChanged`/`PodSync` 都是真实定义。)

当 `plegCh` 里来一个 `ContainerDied` 事件,`syncLoopIteration` 就会去 `HandlePodSyncs`——对这个 Pod 触发一轮 reconcile,然后 `computePodActions` 发现"这个容器该在但死了",按重启策略(`Always`/`OnFailure`/`Never`)决定要不要拉起来。**这就是 k8s 容器自愈的源头:PLEG 发现死亡 → 触发 reconcile → kubeGenericRuntimeManager 重新 CreateContainer/StartContainer。**

### 不这样会怎样:为什么不能用"事件推送"

你可能想:容器死了,运行时(containerd)直接给 kubelet 推个事件不就行了,为什么要专门搞个 PLEG?

这正是 PLEG 设计里最现实的一笔。理论上,容器运行时(尤其是较新的 CRI 实现)确实能流式地推容器状态变化(这叫 **Evented PLEG**,k8s 有这个 feature gate)。但现实中——

- **不是所有运行时都支持流式事件**。CRI 标准里,流式事件是可选的,不是强制的。如果 kubelet 假设运行时一定推事件,那碰到不支持的就抓瞎。
- **流式事件可能丢**。网络抖动、kubelet 重启、运行时重启——任何一个都可能让中间的事件丢掉。丢了就是"该被发现的变化没被发现",reconcile 就漏了。
- **流式事件不能覆盖所有"变化"**。有些"变化"不是单个容器的事件,而是整体状态的偏离(比如一个容器卡在某个中间态),需要定期全量扫描才能发现。

### 所以这样设计:Generic PLEG —— 每秒主动全量"relist",拿新旧快照做 diff

k8s 的兜底方案叫 **Generic PLEG(通用 PLEG)**,它的核心思路朴素得让人安心:**不管运行时支不支持事件推送,我每隔一段固定时间(默认 1 秒)主动去问一遍运行时——"现在每个 Pod 里有哪些容器、什么状态",拿这次快照和上次快照做对比,有变化就生成事件。**

看它的核心方法 `Relist`(re-list,重新列举):

```go
// pkg/kubelet/pleg/generic.go
func (g *GenericPLEG) Relist(ctx context.Context) {
    g.relistLock.Lock()
    defer g.relistLock.Unlock()

    timestamp := g.clock.Now()

    // 关键这一步:主动去问运行时——"现在我这台机器上有哪些 Pod、每个里有哪些容器?"
    podList, err := g.runtime.GetPods(ctx, true)   // 底层就是 CRI 的 ListPodSandbox + ListContainers
    // ...

    // 把这次的快照设为"current",和上次的"old"对比
    g.podRecords.setCurrent(pods)

    // 对每个 Pod,对比 old 和 current,生成事件
    for pid := range g.podRecords {
        g.reconcilePodRecord(ctx, pid)   // ← 这里 diff,有变化就往 eventChannel 塞事件
    }
}
```

(参见 [`generic.go` 的 `Relist`](https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/pleg/generic.go) 与 [`reconcilePodRecord`](https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/pleg/generic.go)。`g.runtime.GetPods` 底层调的就是 CRI 的 `ListContainers`。)

`reconcilePodRecord` 是真正的"diff"逻辑:它对每个 Pod,把上次记录的容器状态(old)和这次查到的容器状态(current)逐个对比,某个容器从 running 变成 exited,就生成一个 `ContainerDied` 事件;从不存在变成 running,就生成 `ContainerStarted` 事件。这些事件被塞进 `eventChannel`,随后被 `syncLoopIteration` 的 `plegCh` 那一路读走。

> **比喻**:PLEG 像一个**每秒巡一次仓库的盘点员**。他不去指望"每个箱子坏了会主动报警"(报警器可能失灵),而是**每秒亲自走一遍,把每个箱子的状态记一遍**,然后和上次的记录对比——"3 号箱上次在亮着,这次灭了,记一条:3 号死了。" 这种"主动定期盘点 + 比对"的方式,慢是慢了点(最多 1 秒延迟),但**极其可靠**——不依赖任何外部承诺,丢了的事件下一轮盘点自然会被重新发现。

### PLEG 还有个 Healthy 探针,它会"卡住"

PLEG 还有一个细节值得点出。它会定期更新一个"上次 relist 时间"。kubelet 的 `runtimeState` 会用这个时间判断 PLEG 健不健康——**如果超过一定阈值(默认 3 分钟)PLEG 没 relist 成功, kubelet 就把本节点标记为 NotReady**(PLEG is not healthy)。这是一种自我保护——PLEG 卡住意味着 kubelet 已经"看不见"容器的真实状态了,这时再让调度器往这台节点派新 Pod 是危险的,所以先把自己标 NotReady,让调度器绕开。**PLEG 卡住是生产中节点 NotReady 最常见的根因之一**,记住这个排查路径。

> **补一句关于 Evented PLEG**:较新的 k8s 有个 feature gate 叫 `EventedPLEG`,它让支持流式事件的运行时(比如新版 containerd)可以走"事件推送"路径,降低延迟和 CPU。但**它始终以 Generic PLEG 做兜底**——一旦流式事件出错或丢失,立刻 fallback 回每秒 relist。这种"乐观用事件、悲观用轮询"的双重保险,是 kubelet 在"性能"和"可靠性"之间典型的工程取舍。

讲清了"怎么发现真相",接下来看 kubelet 怎么把真相**反向同步回总部**——这是它"嘴"的那一面。

---

## 五、反向同步:Pod 的 status 字段是 kubelet 写的

到这一步,我们解决了"kubelet 怎么把容器跑起来、怎么知道它挂了"。但 reconcile 是个双向闭环:控制平面要做决策("期望 3 个、实际几个"),它得知道**实际几个**。这个"实际几个"从哪来?

答案:**从 kubelet 上报来**。

### 不这样会怎样:总部不知道节点真相,reconcile 就是瞎子

第 14 章讲"为什么必须编排"时,反复强调一个痛点——**reconcile 要能收敛,前提是"期望"和"实际"都是可读的、权威的**。期望在 etcd(你 apply 的 YAML);实际呢?

如果实际状态散落在每个节点上、不报回总部,会发生什么:

- Deployment 控制器算"我要 3 个副本,现在有几个"——它只能数 etcd 里 `phase=Running` 的 Pod 有几个。可这些 Pod 是不是真在跑、它们的容器是不是真活着、是不是健康——**etcd 自己根本不知道,它只是个 KV 存储**。
- Node 督办(controller-manager 里那个)判断"这台节点是不是失联了",它得看 Node 对象的 status——这个 status 谁写?etcd 自己不会凭空知道"node-1 现在磁盘快满了、CPU 80%"。
- 调度器决定一个 Pod 该上哪台节点,它得知道每台节点的可用资源——这个"可用资源"也是从 Node 的 status 算出来的。

所有这些"真相",**都必须有人从节点上收集、报回 etcd**。这个人,就是 kubelet。**Pod 的 status 字段(Phase / Conditions / ContainerStatuses)、Node 的 status 字段(容量、已用、Conditions)——都不是 apiserver 算的,也不是你写的,而是 kubelet 写的。**

> **比喻**:驻港经理每天干的最重要的一件事,是**给总部发电报**:"3 号货轮上报:5 号箱还在跑、7 号箱昨晚挂了我重启了、12 号箱一直起不来;我这艘船现在还剩 30% 载重、主机温度正常、1 号吊车在修。" 总部台账上那些"实际状态"的字段,全部来自这些电报。**没有这些电报,总部的 reconcile 就成了瞎子——它知道想要 3 个,但不知道实际几个。**

### 所以这样设计:statusManager,一个专门写回 apiserver 的组件

kubelet 内部有一个叫 **statusManager**(status manager)的组件,专职负责"把本节点的真相写回 apiserver"。它的工作分两步:

**第一步:在本机维护每个 Pod 的最新 status。** kubelet 各个组件会把"我知道的真相"喂给 statusManager:

- `syncPod` 跑完一轮,把"这个 Pod 现在的容器状态、重启次数"更新进去——调用 `statusManager.SetPodStatus`。
- PLEG 每秒 relist 发现容器状态变了,这些变化最终也汇聚到 statusManager 这里。
- 健康探针(liveness/readiness)发现"这个容器虽然活着但应用不响应",会调 `statusManager.SetContainerReadiness` 更新这个容器的 ready 状态。

**第二步:把这些 status 写回 apiserver。** statusManager 启动时跑一个常驻循环:

```go
// pkg/kubelet/status/status_manager.go
func (m *manager) Start(ctx context.Context) {
    syncTicker := time.NewTicker(syncPeriod).C      // 定时全量同步
    go wait.Forever(func() {
        for {
            select {
            case <-m.podStatusChannel:               // 有 status 变了,立刻同步
                m.syncBatch(ctx, false)
            case <-syncTicker:                        // 定时兜底,全量同步
                m.syncBatch(ctx, true)
            }
        }
    }, 0)
}
```

(参见 [`status_manager.go` 的 `Start`](https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/status/status_manager.go)。`podStatusChannel` 一收到信号就 sync,外加定时全量兜底。)

真正把 status 写回 apiserver 的,是 `syncBatch` 里调用的 `syncPod`(注意:这个 `syncPod` 是 statusManager 的,和前面的 `kubeGenericRuntimeManager.SyncPod` 不是一回事):

```go
// pkg/kubelet/status/status_manager.go
func (m *manager) syncPod(ctx context.Context, uid types.UID, status versionedPodStatus) {
    pod, err := m.kubeClient.CoreV1().Pods(status.podNamespace).Get(ctx, status.podName, ...)

    // 合并:本地的新 status + apiserver 上已有的 status → 该写回去的 status
    mergedStatus := mergePodStatus(pod, pod.Status, status.status, ...)

    // 关键:把合并后的 status PATCH 回 apiserver(最终落进 etcd)
    newPod, patchBytes, unchanged, err :=
        statusutil.PatchPodStatus(ctx, m.kubeClient, pod.Namespace, pod.Name, pod.UID, pod.Status, mergedStatus)
    // ...
}
```

(参见 [`status_manager.go` 的 `syncPod`](https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/status/status_manager.go),`PatchPodStatus` 那一行就是真相回写的落点。)

**就是那个 `PatchPodStatus`,把 kubelet 在本机看到的真相,通过一个 PATCH 请求写回了 apiserver,最终落进 etcd。** 从这一刻起,etcd 里这个 Pod 的 `status` 字段——它的 Phase(Running/Pending/Failed)、Conditions(Ready/Initialized)、ContainerStatuses(每个容器的状态、重启次数、退出码)——就是 kubelet 写进去的了。Deployment 控制器、scheduler、你 `kubectl get pod` 看到的,都是这个。

> **这里藏着一个初学者常困惑的点**:`kubectl get pod` 看到的 `STATUS` 列(那个 Running/Pending/ContainerCreating/CrashLoopBackOff),是哪来的?——**不是 apiserver 算的,是 kubelet 写的**。Pod 卡在 ContainerCreating,是 kubelet 还在拉镜像/建沙箱,还没来得及把 status 改成 Running;CrashLoopBackOff,是 kubelet 反复重启一个一直挂的容器,它在 status 里把 reason 标成这个。**Pod 的 status,本质上是 kubelet 对"我这台机器上这个 Pod 的真实状况"的一手陈述。**

### Node 的 status 也一样,而且加了 lease

除了 Pod status,kubelet 还要上报**本节点的 status**——这台机器现在有多少 CPU/内存/磁盘、用了多少、健康不健康(Ready)、有没有压力(MemoryPressure/DiskPressure/PIDPressure)。这部分由 `syncNodeStatus` 负责,逻辑类似但更重一些。

> 一个细节:Node 的"心跳"(我还在不在)现在默认走 **node lease**(Lease 对象),而不是每次都 PATCH 整个 Node 对象。`go kl.nodeLeaseController.Run(...)` 就是干这个的。Lease 是个很小的对象,频繁 renew 它比频繁 PATCH 整个 Node 省 apiserver 负载得多。**Node status 全量上报的频率较低(默认几十秒一次),lease 心跳频率较高(默认 10 秒)——分而治之,既让总部及时知道节点还活着,又不把 apiserver 打死。**

讲到这里,kubelet 的"双向闭环"就完整了——

```
                ┌───────────────────── apiserver / etcd ─────────────────────┐
                │   spec(期望,你 apply 的)        status(实际,kubelet 写的)│
                └─────────┬─────────────────────────────────┬────────────────┘
                          │ ① watch(spec 变化)              ▲ ⑥ PATCH(status 回写)
                          ▼                                  │
   ┌─────────── kubelet(syncLoop 主循环)───────────────────────────────────┐
   │                                                                       │
   │  ② configCh ← informer watch "我这台节点的 Pod"                        │
   │       ↓                                                               │
   │  syncLoopIteration ──┐                                                │
   │   (select 五路)      │                                                │
   │     configCh         │ ③ HandlePodAdditions/Updates → podWorkers      │
   │     plegCh ──────┐   │                            ↓                    │
   │     syncCh       │   │ ④ 每 Pod 一个 goroutine → syncPod              │
   │     livenessMgr──┤   │                            ↓                    │
   │     housekeepingCh   │ ⑤ kubeGenericRuntimeManager.SyncPod            │
   │                    └──┐  → RunPodSandbox/CreateContainer (CRI)        │
   │   ┌──────────────────────┐                                            │
   │   │  PLEG:每秒 relist    │── 容器状态变化 → plegCh                     │
   │   │  statusManager       │─────── 真相汇聚 ──────── PatchPodStatus(⑥) │
   │   │  probeManager        │── liveness/readiness 结果 → livenessManager │
   │   └──────────────────────┘                                            │
   └─────────────────────────────────┬─────────────────────────────────────┘
                                     ▼
                          containerd ──→ runc ──→ 容器进程
                          (CRI;RunPodSandbox 时配 CNI)
```

(示意图,非源码。)

但闭环里还差一环——**"应用到底活着吗"**,这件事光靠"容器进程在不在跑"是答不了的。这就是健康探针的活儿。

---

## 六、健康探针:把"应用活着吗"也上报

PLEG 能告诉你"容器进程在不在跑"。但第 14 章讲过一个微妙的故障模式——**活死(livelock)**:容器进程还活着,但应用已经 hang 住了(死锁、依赖的下游挂了、内部状态崩了)。PLEG 看进程是看不出来的——进程没退出,PLEG 不会产生 `ContainerDied` 事件。

这种"活着但其实没用"的状态,得靠应用层的探针来发现。k8s 的 Pod spec 里可以定义三种探针:

- **livenessProbe(存活探针)**:周期性地问"你活着吗"(发个 HTTP GET、开个 TCP、exec 个命令)。**返回失败 → kubelet 杀掉这个容器,按重启策略重启它。**
- **readinessProbe(就绪探针)**:周期性地问"你准备好了接流量吗"。**返回失败 → kubelet 把这个 Pod 的 ready 条件标成 false,Service 就不会把流量转发给它**(不杀,只是先把它从负载均衡里摘掉)。这是第 20 章 Service 的伏笔。
- **startupProbe(启动探针)**:给慢启动的应用用——在它通过 startupProbe 之前,禁用 liveness/readiness,避免一个还没起完的应用被误判挂掉而杀掉。

### 不这样会怎样:光靠进程退出判断,漏掉"活死"

如果只有 PLEG、没有探针,会发生什么——一个 web 容器因为死锁不再响应任何请求,但它的进程还在跑(jvm 还活着,只是所有线程都卡住了)。PLEG 看进程没退出,不报事件。kubelet 不知情,继续把它当健康 Pod。Service 继续往它转发流量,用户看到一片 502。**容器"活着",但业务"死了",而 k8s 毫不知情。**

这就是为什么要有 livenessProbe——它把"应用到底活没活"的判断,从"看进程"升级到"问应用"。

### 所以这样设计:probeManager + 各种 manager 协作

kubelet 内部有个 **probeManager**(探针管理器),它负责:为每个定义了探针的容器,起一个专属 goroutine,周期性地执行探针(HTTP/TCP/Exec),把结果写进几个 manager:

- 探针结果是"活着/死了",写进 **livenessManager**;
- 探针结果是"就绪/没就绪",写进 **readinessManager**(然后调 statusManager.SetContainerReadiness,改 Pod 的 ready 条件)。

回想前面 `syncLoopIteration` 的 `case update := <-kl.livenessManager.Updates():`——**这就是探针结果进入主循环的入口**。一旦 livenessManager 报"某个容器 liveness 失败",syncLoop 就会对这个 Pod 触发一轮 reconcile,`computePodActions` 看到"容器该被重启",kubelet 就杀掉它、按重启策略拉起来。

> **比喻**:驻港经理不光数箱子在不在(PLEG),还会定期去敲每个箱子的门问"你里面货还正常吗"(探针)。敲半天没回应,经理判定"这箱虽然灯亮着但里面坏了",于是断电重启(liveness 失败 → 重启)。或者,经理只把"敲了不应"的箱子从装卸清单里暂时划掉,先不往里搬新货,但不停它的电(readiness 失败 → 摘流量但不杀)。

讲清了探针,kubelet 的全部主要职责就齐全了。下面把这些零散的部件,用源码钉死在主循环上。

---

## 关键源码精读:syncLoop 的五路 select 与一个 Pod 的旅程

理论讲完了,我们钻进源码,把前面讲的几个组件,在 kubelet 主循环里逐个对应。这一节是本章的"钉钉子"环节。

### 一、入口:Run 怎么把几个核心组件拉起来

kubelet 的总入口是 [`Run`](https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/kubelet.go)(`pkg/kubelet/kubelet.go` 的 `func (kl *Kubelet) Run`)。它做了很多初始化,但对本章最关键的是这几行(简化展示,顺序保留):

```go
func (kl *Kubelet) Run(ctx context.Context, updates <-chan kubetypes.PodUpdate) {
    // ... 各种初始化 ...

    // 节点状态同步:定时把 Node 的 status PATCH 回 apiserver
    go wait.JitterUntil(func() { kl.syncNodeStatus(ctx) }, kl.nodeStatusUpdateFrequency, 0.04, true, wait.NeverStop)

    // node lease 心跳(让总部及时知道我还活着,轻量)
    go kl.nodeLeaseController.Run(context.Background())

    // status manager:把 Pod 的 status 回写 apiserver
    kl.statusManager.Start(ctx)

    // PLEG:开始每秒 relist,产生容器生命周期事件
    kl.pleg.Start(ctx)

    // probe manager:开始周期性执行 liveness/readiness/startup 探针
    // ... (kl.probeManager.AddPod / Start)

    // 最后:主循环!这是 kubelet 真正"干活"的核心
    kl.syncLoop(ctx, updates, kl)
}
```

读这段,注意一个时序细节:**`statusManager.Start`、`pleg.Start` 都在 `syncLoop` 之前被启动**。这很重要——syncLoop 一启动就会立刻去读 `plegCh` 和等 status 回写,所以这些"供料"组件必须先就位。同时,`syncNodeStatus` 和 `nodeLeaseController` 是**独立的 goroutine**,不和 syncLoop 抢资源——它们只管"节点本身的真相上报",和"Pod 级别的 reconcile"是两条并行轨道。

### 二、syncLoopIteration:五路 select 的真实结构

主循环 `syncLoop` 是个死循环,每轮调一次 `syncLoopIteration`。我们已经看过它的骨架,这里再钉一遍它真实的五路 select,并把每一路对应到前面讲的设计(参见源码 `syncLoopIteration`):

```go
func (kl *Kubelet) syncLoopIteration(ctx context.Context, configCh <-chan kubetypes.PodUpdate, handler SyncHandler,
    syncCh <-chan time.Time, housekeepingCh <-chan time.Time, plegCh <-chan *pleg.PodLifecycleEvent) bool {

    select {
    case u, open := <-configCh:        // ① 总部改了"我这台节点的 Pod"
        if !open { return false }
        switch u.Op {
        case kubetypes.ADD:    handler.HandlePodAdditions(ctx, u.Pods)     // 新 Pod 来了
        case kubetypes.UPDATE: handler.HandlePodUpdates(ctx, u.Pods)       // Pod 配置变了
        case kubetypes.REMOVE: handler.HandlePodRemoves(ctx, u.Pods)       // Pod 被删
        case kubetypes.RECONCILE: handler.HandlePodReconcile(ctx, u.Pods)
        case kubetypes.DELETE: handler.HandlePodUpdates(ctx, u.Pods)       // 优雅删除,当 UPDATE
        }
        kl.sourcesReady.AddSource(u.Source)

    case e := <-plegCh:                 // ② PLEG 发现容器生命周期事件(某容器死了/起了)
        if isSyncPodWorthy(e) {
            if pod, ok := kl.podManager.GetPodByUID(e.ID); ok {
                handler.HandlePodSyncs(ctx, []*v1.Pod{pod})    // 触发这个 Pod 的 reconcile
            }
        }
        if e.Type == pleg.ContainerDied {                       // 顺手清理死容器
            if containerID, ok := e.Data.(string); ok {
                kl.cleanUpContainersInPod(ctx, e.ID, containerID)
            }
        }

    case <-syncCh:                      // ③ 每秒兜底:重新同步"长时间没同步的 Pod"
        podsToSync := kl.getPodsToSync()
        if len(podsToSync) == 0 { break }
        handler.HandlePodSyncs(ctx, podsToSync)

    case update := <-kl.livenessManager.Updates():   // ④ liveness 探针结果
        if update.Result == proberesults.Failure {
            // 找到这个容器对应的 Pod,触发 reconcile(reconcile 时会杀掉不健康容器并重启)
            if podUID, ok := kl.podManager.GetPodByUID(...); ok { ... }
        }

    case <-housekeepingCh:              // ⑤ 定时做家务:清理死容器、回收
        // ...
    }
    return true
}
```

源码注释里有一段特别值得摘出来——它解释了为什么是 `select`:

> "Here is an appropriate place to note that despite the syntactical similarity to the switch statement, the case statements in a select are evaluated in a **pseudorandom order** if there are multiple channels ready to read from. In other words, **case statements are evaluated in random order**, and you can not assume that the case statements evaluate in order if multiple channels have events."

(直译:多个 channel 同时有数据时,`select` 的 case 是**伪随机**选一个的,不要假设它按代码顺序处理。)

这个细节不是装饰——它告诉我们一个重要的事实:**这五路事件没有"优先级"**。configCh 和 plegCh 同时来,谁先处理是随机的。这其实是好事——它避免了"某一路饿死另一路"。也正因为没有优先级,kubelet 必须保证"不管哪一路触发,结果都应该让世界更接近期望"——而这,正是 reconcile 语义的核心(reconcile 是幂等的、收敛的,谁先谁后不影响最终状态)。

### 三、一个 Pod 的完整旅程:从 watch 到 RunPodSandbox

把前面几节串起来,我们跟一个新 Pod 从"被 scheduler 绑定到 node-1"到"在 node-1 上跑起来",在 kubelet 内部的完整旅程:

```
   ① apiserver: scheduler 把 Pod bind 到 node-1,写进 etcd
                    │
                    ▼
   ② kubelet 的 informer(PodConfig)watch 到"node-1 多了个 Pod"
                    │  经 PodConfig 转成 kubetypes.PodUpdate{Op: ADD}
                    ▼
   ③ syncLoop 的 configCh 收到 ADD
                    │  syncLoopIteration 的 case <-configCh
                    ▼
   ④ HandlePodAdditions:
      - podManager.AddPod(pod)              // 记进本地的"我应该有哪些 Pod"
      - admission(资源够不够、安全策略)      // 过了才往下
      - kl.podWorkers.UpdatePod(...)         // 把这个 Pod 交给 podWorkers
                    │
                    ▼
   ⑤ podWorkers.UpdatePod:
      - 这是这个 Pod 第一次来 → spawn 一个专属 goroutine:go podWorkerLoop
      - 把这次更新塞进 pendingUpdate,往 podUpdates channel 发信号
                    │
                    ▼
   ⑥ podWorkerLoop 收到信号,取出 pendingUpdate
      - 调 kl.syncPod(...)                  // 这是 Kubelet.SyncPod,真正动手前的封装
                    │
                    ▼
   ⑦ Kubelet.SyncPod:
      - 跑 admission、生成 PodStatus 等
      - 调 kl.containerRuntime.SyncPod(...) // containerRuntime 就是 kubeGenericRuntimeManager
                    │
                    ▼
   ⑧ kubeGenericRuntimeManager.SyncPod(那个八步流程):
      Step1: computePodActions → 这个 Pod 是全新的,沙箱要建,容器要起
      Step4: createPodSandbox → runtimeService.RunPodSandbox(CRI!)
              └─ containerd 收到 RunPodSandbox:
                  ├─ 建 pause 容器(占住共享 network namespace)
                  ├─ 调 CNI 插件给这个 sandbox 配网络(IP、路由)  ← 第 13 章的 CNI
                  └─ 返回 sandboxID
      Step5: 逐个跑 init 容器(CRI: CreateContainer + StartContainer)
      Step6: PullImage(如果镜像没拉过)
      Step7: 逐个跑业务容器(CRI: CreateContainer + StartContainer)
                    │
                    ▼
   ⑨ 容器跑起来。kubeGenericRuntimeManager 返回。
                    │
                    ▼
   ⑩ podWorker 把结果汇报 → statusManager.SetPodStatus(pod, newStatus)
                    │
                    ▼
   ⑪ statusManager 的循环收到 podStatusChannel 信号 → syncBatch → syncPod
      → PatchPodStatus:把这个 Pod 的 status(Phase=Running,Conditions=Ready)
        PATCH 回 apiserver,落进 etcd
                    │
                    ▼
   ⑫ apiserver → 你 kubectl get pod,看到 STATUS: Running
```

(流程示意,非源码;每一步对应的函数名都是真实的,可在前面引用的源码文件里查到。)

**这条链子的精华在于:它没有任何一步是"k8s 直接启动容器"**。每一层都只负责自己那一小块——informer 负责"感知世界",syncLoop 负责"分发事件",podWorkers 负责"每 Pod 串行化",kubeGenericRuntimeManager 负责"对比期望实际 + 翻译成 CRI 调用",containerd/runc 负责"真正建 namespace/cgroup/exec"。**这种层层分工、各管一摊的设计,正是 k8s 能在几万个容器规模上保持清晰和可维护的关键。**

---

## 章末小结

### 用航运比喻回顾本章:驻港经理的一日

回到港口。这一章我们做了一件事:**打开每艘货轮上那个"驻港经理"的黑盒,看清他一天到晚在忙什么**。

驻港经理(kubelet)的一天,可以总结成三件事:

1. **听令 + 动手**:他每天盯着总部台账(经 apiserver watch)上"我这艘船该有哪些箱"。一旦总部说"3 号船要加一个 web 箱",他立刻去码头组织吊装——但他不亲自开起重机(runc),也不亲自管堆场(containerd),他只对着对讲机喊一套标准口令(CRI),让码头(containerd)替他把箱吊到位、通电、固定。这一套喊话的口令,就是 `RunPodSandbox` + `CreateContainer` + `StartContainer`。
2. **盘点**:他**每秒巡一次仓库**,把每个箱的状态记一遍,和上次比对——哪个箱灭了(容器死了)、哪个箱新亮了。这就是 PLEG。一旦发现"3 号箱灭了",他立刻按规则(重启策略)把它重新通电拉起来。这种"主动定期盘点 + 比对"的方式,慢一拍但极其可靠——不靠报警器会不会坏,靠的是"我每秒都去看一遍"。
3. **回报**:他每天给总部发电报,把"我这船上现在真跑着哪些箱、它们状态如何、我这条船还剩多少载重、主机正不正常"汇报回去。这些电报,就是 kubelet 写回 etcd 的 status 字段——**总部台账上那些"实际状态",全部来自这些电报**。没有这些电报,总部的 reconcile 就是瞎子。

而这位经理最反直觉的一点:**他不是被动接命令的服务员,他自己就是个"小调度员"**。他不停地在"台账上该有的箱"和"甲板上真有的箱"之间做对比、补差——多了杀、少了起、挂了重启。**这就是 reconcile 范式在节点这一层的复刻**,和总部那些 Deployment 督办、Node 督办是同一个套路,只是他 reconcile 的对象从"集群"缩小到了"我这台机器"。

### 本章在全书主线中的位置

回到全书的二分法:**打包隔离 vs 调度编排**。

这一章,我们正式填上了第 15 章那张架构图里 kubelet 这个方框的全部血肉,把"调度编排"侧最关键的一个执行者讲透了:

- **第 14 章立了范式**(声明期望 + 自动调谐),**第 15 章画了架构**(控制平面 + 节点),**第 16 章定义了最小调度单位**(Pod),**第 17 章深挖了 informer 和 controller 的源码**,**第 18 章讲了"Pod 上哪艘船"的决策**(调度器)。
- 而这一章(第 19 章)回答的是最后一步:**"这个 Pod 被分配到了我这台节点,谁来真正把它跑起来?真相又怎么报回去?"** ——答案是 kubelet,它通过 CRI 把控制平面的"嘴上功夫"翻译成 containerd/runc 的"动手功夫",再通过 statusManager 把节点真相反向同步回 etcd,让总部的 reconcile 闭环成立。

至此,k8s 那套 reconcile 范式的**完整闭环**就讲透了:

```
   你 apply 期望 → apiserver → etcd
                         ↓ (informer watch)
        ┌────────────────┴────────────────┐
        │                                 │
   controller-manager                 scheduler
   (Deployment 督办)                 (这箱货上哪艘船)
        │                                 │
        │ 补 Pod 到 etcd                   │ bind Pod 到 node-1
        ▼                                 ▼
                  ┌──────── kubelet(node-1)────────┐
                  │  ① watch 到"我有新 Pod"        │  ← 本章
                  │  ② syncLoop reconcile           │
                  │  ③ CRI → containerd → runc      │  容器真正跑起来
                  │  ④ PLEG 盘点                    │
                  │  ⑤ status PATCH 回 apiserver    │  ← 本章
                  └─────────────┬───────────────────┘
                                ▼
                  etcd 的 status 更新 → controller-manager
                  看到"实际副本数够了" → reconcile 收敛
```

**kubelet 是这个闭环里唯一"真正动手"的环节**,也是真相回流的总闸。没有它,前面的 scheduler 决策是空话,后面的 controller-manager reconcile 是瞎猜。

### 五个"为什么"清单

如果你只能记五件事,记这五件:

1. **为什么需要 kubelet**:控制平面全是"动嘴的"(apiserver/etcd/scheduler/controller-manager 都只读写账本,不碰容器),节点上必须有个"动手的"。kubelet 是 k8s 全集群唯一真正启动容器的组件(通过 CRI),它是控制平面和真实容器世界之间的桥梁。
2. **为什么 kubelet 自己也是个 controller**:它会持续 watch "我这台节点该有的 Pod"(期望),对比"这台机器上真跑着的容器"(实际),按需起/杀/重启——和 controller-manager 同一个 reconcile 范式,只是对象缩到了"本机容器"。被五类事件驱动(configCh/plegCh/syncCh/livenessManager/housekeepingCh),统一进 `syncLoop` 的 select。
3. **怎么真正起容器**:kubelet 把期望的 Pod 翻译成对 CRI 的 gRPC 调用——`RunPodSandbox`(建 Pod 沙箱)→ `CreateContainer`/`StartContainer`(起容器)。这条链子 `apiserver → kubelet.syncLoop → podWorkers → kubeGenericRuntimeManager.SyncPod → runtimeService → containerd → runc`,是 k8s 上一个 Pod 落地的完整路径,回扣第 9、10 章。
4. **PLEG 怎么发现容器挂了**:不是事件推送,是 Generic PLEG 每秒主动 `Relist`(调 CRI 的 ListContainers),拿新旧快照做 diff,有变化生成 PodLifecycleEvent。这种"主动轮询 + 比对"慢一拍但极可靠,不依赖运行时支不支持事件。PLEG 卡住会让节点变 NotReady,是生产中常见根因。
5. **状态怎么报回去**:Pod 的 status 字段(Phase/Conditions/ContainerStatuses)、Node 的 status 字段,**都是 kubelet 写的**。statusManager 汇聚本机真相,通过 PatchPodStatus PATCH 回 apiserver,落进 etcd,让总部的 reconcile 有依据。**没有 kubelet 上报,整个 k8s 就是瞎子。**

### 想继续深入,该往哪钻

- **亲手感受 kubelet 的"小 controller"行为**:`kubectl delete pod <某个 Pod>` 之后,盯着这个 Pod,你会看到它被杀、又被自动重建(因为 Deployment 督办发现少了、补了一个新的)。但更狠的实验是 `ssh` 到节点上,直接 `crictl stop`(或 `docker stop`)那个容器——你会发现 kubelet **几十秒内**把它重新拉起来。这就是 PLEG + reconcile 在节点本地的真实体现。
- **看 syncLoop 的源码**:打开 [`pkg/kubelet/kubelet.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/kubelet.go),找 `syncLoop` → `syncLoopIteration`,对着本章那张"五路 select"的图,一路路读过去。然后跟着 `HandlePodAdditions` → `podWorkers.UpdatePod` → `Kubelet.SyncPod` → `kubeGenericRuntimeManager.SyncPod`,把一个 Pod 的旅程走通。
- **看 PLEG 怎么 diff**:打开 [`pkg/kubelet/pleg/generic.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/pleg/generic.go),重点读 `Relist` 和 `reconcilePodRecord`——你会亲眼看到"拿新旧快照对比、生成事件"的代码,这是"轮询 reconcile"最朴素的实现。
- **看 status 怎么写回**:打开 [`pkg/kubelet/status/status_manager.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/status/status_manager.go),读 `Start`(那个 `wait.Forever` 循环)→ `syncBatch` → `syncPod` → `PatchPodStatus`。这是"kubelet 把真相写回 etcd"的落点。
- **看 CRI 调用链**:打开 [`pkg/kubelet/kuberuntime/`](https://github.com/kubernetes/kubernetes/tree/master/pkg/kubelet/kuberuntime),从 `kuberuntime_manager.go` 的 `SyncPod`(那个 Step1~Step8)开始,跟到 `kuberuntime_sandbox.go` 的 `createPodSandbox`,你会看到 `m.runtimeService.RunPodSandbox(...)`——这就是回扣第 10 章 CRI 的那一行。然后去看 containerd 的 CRI 实现(`containerd/pkg/cri/`),看 RunPodSandbox 内部怎么调 CNI——这是回扣第 13 章。

---

> kubelet 这个"驻港经理"立住了:它把控制平面的决策(这个节点该有哪些 Pod)真正落地成跑着的容器,又把节点真相源源不断报回总部的账本,**让 k8s 的 reconcile 闭环从"嘴上"走通了到"手上"**。但这里冒出一个新问题——**Pod 是会死会生的,它的 IP 一直在变**(kubelet 重启它、扩容了变多、缩容了变少),那么别的 Pod 怎么稳定地找到它?总不能每次都查"现在 IP 是几号"。这个问题,需要一个盖在会变的 Pod IP 之上的**稳定门牌**——下一章,我们拆 Service 和 kube-proxy 怎么用 iptables/ipvs 把这个门牌做出来。翻开 **第 20 章 · Service 与 kube-proxy:服务发现与负载均衡**。
