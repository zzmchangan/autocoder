# 第 20 章 · Service 与 kube-proxy:服务发现与负载均衡

> **前置**:你需要先读完[第 12 章《容器网络基础:veth/bridge/iptables》](P4-12-容器网络基础-veth-bridge-iptables.md)和[第 15 章《k8s 架构:控制平面与节点》](P5-15-k8s架构-控制平面与节点.md)。前者讲清了一件事:iptables 的 NAT(DNAT 把目标地址翻译、MASQUERADE 把源地址翻译)是容器网络和外界互通的根——这一章你会发现,kube-proxy 干的活,就是**大规模、自动化地往内核里写 DNAT 规则**。后者在最后留了一个钩子:它说 kube-proxy 是每艘船(节点)上的"转运员",负责"配 Service 的网络规则",并特别澄清了"它不是代理服务器、不参与转发,只装规则"——但没讲规则长什么样、怎么负载均衡。这一章就把那个钩子彻底拆开。

> **核心问题**:Pod 是会死会生的(IP 一直在变),别的 Pod 怎么稳定地找到它?
>
> 这是第 6 篇(k8s 核心抽象)的最后一章,也是 k8s 半本"服务发现"这条线的收尾。前面几章我们解决了"Pod 怎么被调度出来、怎么被真正跑起来",但一直绕着一个问题走:**Pod 一旦跑起来,它的 IP 是多少?别的 Pod 怎么找到它?** 而 Pod 这个东西,偏偏是会死会生、IP 一刻不停的在变——重启一次 IP 变、扩容一次 IP 全变、缩容一次一批 IP 消失。在一个不停变化的后端之上,怎么造出一个**稳定的门牌**,让访问者只认门牌、不认 IP?这就是 Service 要解决的问题。而把这个"门牌 → 后端 Pod"的翻译,**在每一台节点上自动配进内核**的,就是 kube-proxy。
>
> **读完本章你会明白**:
> - 为什么不能"直接写 Pod IP 当连接字符串"——Pod IP 会变,直连就全崩;为什么需要一个**稳定门牌(Service)**盖在一组会变的 Pod IP 之上,门牌背后的后端列表由一个 controller 持续 reconcile。
> - **kube-proxy 怎么用 iptables 规则把流量负载均衡到后端 Pod**:KUBE-SERVICES 链匹配 ClusterIP、KUBE-SVC-* 链用 `statistic --mode random --probability` 做概率分发、KUBE-SEP-* 链做 DNAT 到 Pod IP——回扣第 12 章的 DNAT,这里是它的批量自动化版本。
> - 为什么 ipvs 模式在 Service 上万条时比 iptables 模式快(iptables 链是线性遍历 O(n),ipvs 是哈希表 O(1))——以及这条对比在今天 nftables 时代的新格局。
> - Service 的四种类型(ClusterIP/NodePort/LoadBalancer/Headless)各解决什么,服务发现的两种方式(DNS 名、环境变量)各适合什么场景。

> **如果一读觉得太难**:本章链路较长(KUBE-* 链好几层),先只记住三件事——
> ① **Service = 一个稳定的假 IP(ClusterIP)+ 一组会变的真 IP(Pod),中间靠 kube-proxy 在每台节点上写 DNAT 规则把假 IP 翻译成真 IP**;
> ② **负载均衡在 iptables 模式下靠 `statistic --mode random --probability` 概率分发实现**(数学很巧妙:`1/n, 1/(n-1), …, 1/2` 这串数字配出来每个后端恰好等概率);
> ③ **门牌背后的"后端 Pod 列表"由 Endpoints/EndpointSlice controller reconcile 出来,kube-proxy watch 它再 reconcile 内核规则——一切都是第 14 章那套范式**。这三句话钉死,后面慢慢填。

---

## 章首·一句话点破

如果你对 Service 的印象是"k8s 自带的一个负载均衡器",或者对 kube-proxy 的印象是"一个跑在每个节点上、专门转发流量的代理服务器",请把这俩印象先放一边。这是理解这一章时最容易绊倒的两个误解,它们会让你去错误的地方找答案(去找那个"做转发的进程"、去找"哪个组件在四层做 LB")。

这一章要做的第一件事,是把这两个误解连根拔起:

> **Service 不是一个负载均衡器进程。它只是一个"稳定的虚拟 IP(ClusterIP) + 一组后端 Pod 的列表",这两样东西都存在 etcd 里。真正把"对 ClusterIP 的访问"翻译成"对某个 Pod IP 的访问"的,是每个节点内核里的 iptables/ipvs 规则——而那些规则,由每个节点上的 kube-proxy 进程(一个**只写规则、不碰数据包**的进程)批量配上去。**

我们一块一块拆。先从最朴素的问题开始:为什么不直接写 Pod IP。

---

## 一、为什么不能直接写 Pod IP:会变的门牌

回忆前面几章走过的旅程。到第 19 章为止,我们已经能让一个 Pod 被声明出来、被调度器放到某个节点、被 kubelet 通过 CRI 真正跑起来。这个 Pod 跑起来后,它从 Pod 网络里拿到一个 IP(比如 `10.244.1.5`),其他 Pod 可以用这个 IP 访问它。听起来挺好。

可一旦你拿这个 IP 去写连接字符串,灾难就开始了。

### 不这样会怎样:Pod IP 一刻不停在变

Pod 这个东西,和虚拟机、和物理机有本质的不同——**它是临时的、可替换的**。k8s 的整个哲学是"Pod 是会死会生的 cattle,不是养着的 pet":任何一个 Pod 挂了、节点炸了、扩缩容了,都会有新的 Pod 顶上来,但**新的 Pod 拿到的,几乎一定是一个新的 IP**。

把"Pod IP 会变"这件事展开,至少有四种场景会让 IP 变:

- **Pod 重启**:应用 OOM 退出、被 liveness probe 判死、节点资源紧张被驱逐——Pod 被销毁重建,新 Pod 是个全新的网络实体,大概率换 IP。
- **滚动更新**:`kubectl rollout` 升级版本,k8s 不会"原地改",而是一个个起新版 Pod、杀旧版 Pod——一整轮下来,所有 Pod 的 IP 全换。
- **扩容**:`replicas` 从 3 改到 30,新起的 27 个全是新 IP。
- **缩容**:杀掉的那一批 IP 直接消失。
- **节点故障**:节点宕了,上面所有 Pod 被调度器在别处重建——一批 IP 全换。

回扣第 14 章那个"服务发现不够"的场景:你的 web 容器要连数据库,你把连接字符串写成 `mysql:10.244.1.5:3306`。过两天数据库 Pod 被滚动更新,IP 变成 `10.244.3.12`,你的 web 全部连接失败。改连接字符串?集群里几千个 Pod,每个都连一堆别的服务,改一次配置得改几万处。**这不是工程问题,这是物理不可能**。

> **比喻**:回到港口。集装箱(Pod)是会换船的——今天这箱货在 3 号船的 5 号舱,明天可能被转到 7 号船的 2 号舱,后天又转到 1 号船。如果你要寄东西给"这箱货",你每次都得先问"它现在在哪艘船哪个舱"——这在几百艘船、每天几百次转船的船队里,根本追踪不过来。
>
> 现实航运怎么解决的?**每个集装箱有一个统一运单号**。不管这箱货转到哪艘船、换了几次船,**运单号不变**。你寄东西只认运单号,中转站会自己查"这个运单号现在在哪个舱",把货送过去。**运单号,就是稳定门牌。** Pod IP 会变,所以我们需要一个不变的"运单号"盖在它上面——这就是 Service。

### 所以这样设计:Service = 稳定 ClusterIP + 一组会变的 Pod IP

k8s 给出的答案,叫 **Service**。它干的事,用一句话讲清:

> **Service 是一个稳定的虚拟 IP(ClusterIP)+ 一个虚拟端口,它背后挂着一组"会变的 Pod IP"。访问者只认 ClusterIP,Service 负责(在每一台节点上)把对 ClusterIP 的访问,翻译成对某一台真实 Pod IP 的访问。**

注意几个关键词:

- **稳定的**:ClusterIP 一旦分配,只要这个 Service 不被删,它就**永远不变**。哪怕背后所有 Pod 都换了三轮,ClusterIP 还是那个 ClusterIP。访问者写的连接字符串,几年不用改。
- **虚拟的**:ClusterIP 不是任何一张网卡上的真实 IP,**它根本 ping 不通**——你不 `curl` 它、不让内核的 netfilter 拦截的话,这个 IP 是"不存在"的。它只是一个**触发 iptables 规则的标记**。这一点我们后面讲源码时亲眼看见。
- **一组 Pod IP**:Service 背后挂着"现在哪些 Pod 是我的后端"这个列表——这个列表是动态的,Pod 一变它就变。维护这个列表的,是一个 controller(下面专讲)。

> **比喻**:Service 就是那个**统一运单号**。运单号(ClusterIP)稳定不变,你寄件只填运单号;中转站(kube-proxy 写的 iptables 规则)负责"运单号 → 现在哪艘船哪个舱(Pod IP)"的翻译。货转到哪艘船了?那是中转站内部的事,你不用管。

那么,这两个核心问题——**① 谁来维护"运单号背后现在有哪些 Pod"这个列表?② 谁在每个节点上做"运单号 → Pod IP"的翻译?** —— 我们一个一个拆。

---

## 二、Endpoints/EndpointSlice:门牌背后的"后端列表"也是 reconcile

先看第一个问题:**Service 怎么知道背后挂哪些 Pod?**

### 不这样会怎样:把"后端列表"写进 Service 本身

最朴素的设想,是把"后端 Pod IP 列表"直接写进 Service 对象。可一旦这么干,你就把"期望"和"实际"揉进了一个对象——这违反了第 14、15 章立的铁律。更具体地说:

- Service 是你声明的**期望**(我要一个叫 `mysql` 的门牌,选中所有 `app=mysql` 的 Pod),它不该被频繁改写。
- 后端 Pod IP 列表是**实际状态**(现在 `app=mysql` 的 Pod 有哪几个、各自 IP 是啥、健不健康),它每秒都在变。
- 把这俩揉进一个对象,意味着每次任何一个 Pod 重启,Service 对象都要被改写一次。集群有 10000 个 Pod,光改 Service 对象就把 apiserver 和 etcd 活活写死。而且——**期望和实际混在一起,出了偏差你都不知道是谁的责任**。

### 所以这样:Service 只存"期望",后端列表是另一个对象(Endpoints / EndpointSlice)

k8s 把它俩拆开了:

- **Service** 只存"期望":我的名字叫 `mysql`,我选中 `app=mysql` 的 Pod,我的 ClusterIP 是 `10.96.0.10`,端口 3306。**它一个字都不写"现在后端有谁"**。
- **Endpoints**(以及它的升级版 **EndpointSlice**)是**另一个对象**,专门存"实际":这个 Service 现在的后端 Pod 有哪几个、各自的 IP 是啥、Ready 不 Ready。**Service 和 Endpoints 是分开的两个对象,各管各的**。

那么 Endpoints 这个对象谁来填?——**还是一个 controller**。

第 15 章讲 controller-manager 时,我说过它里面有几十个"督办",其中一个叫 **Endpoint / EndpointSlice 督办**。这个督办干的事,完全是第 14 章那套 reconcile 范式的又一个化身:

1. 它**watch**两类资源:所有 Service(看"哪些门牌需要后端")、所有 Pod(看"哪些 Pod 是哪些门牌的后端")。
2. 每次 Service 变了(新建、改 selector、删了)、或 Pod 变了(新建、IP 变了、Ready 状态变了),它都被事件唤醒。
3. 它读出某个 Service 的 `spec.selector`(比如 `app=mysql`),去 Pod 列表里**按标签筛出**所有匹配的 Pod,过滤掉不 Ready 的,得到"现在该挂哪些后端"。
4. 把这个列表和 Endpoints 对象**当前的**内容对比——不一样就**改写** Endpoints 对象(经 apiserver 写回 etcd)。一样就什么都不做。

这就是一轮 reconcile。看真实的源码,这个逻辑在 [`pkg/controller/endpoint/endpoints_controller.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/controller/endpoint/endpoints_controller.go) 里,核心方法叫 `syncService`。它关键的一步,是按 selector 把 Pod 列出来:

```go
// (摘自 kubernetes/pkg/controller/endpoint/endpoints_controller.go,方法 syncService 内)
pods, err := e.podLister.Pods(service.Namespace).List(
    labels.Set(service.Spec.Selector).AsSelectorPreValidated())
if err != nil {
    return err
}
// ... 遍历 pods,过滤掉不该进 endpoints 的(比如未 Ready 的),
//     组装 EndpointSubset,Create/Update Endpoints 对象
```

读这一段,你会立刻认出第 14 章那段伪代码的影子:读期望(selector)、读实际(pods)、算偏差、采取行动(改 Endpoints)。**Service 的后端列表,就是这套 reconcile 范式维护出来的。**

### Endpoints vs EndpointSlice:为什么要拆成片

这里有个值得专门讲的演进。早期 k8s(1.0 开始),后端列表对象叫 **Endpoints**(`v1.Endpoints`),它一个对象就装下整个 Service 的**所有**后端 IP。这在 Service 后端只有几个 Pod 时挺好——一个对象、一份 YAML,清清爽爽。

可一旦某个 Service 后端有 1000 个 Pod,Endpoints 这个对象就膨胀成几 MB 的大块头。更糟的是:**任何一个 Pod 的 IP 变了,整个 Endpoints 对象都要被重新计算、写回 etcd、再全量推送给所有 watch 它的客户端**(包括每个节点的 kube-proxy)。1.0 个 Pod 的变化,引发 1 次 MB 级的对象重写——这种"牵一发而动全身"的放大效应,在万 Pod 集群里是灾难。

于是 k8s 在 1.16 引入、1.21 GA 了一个升级版:**EndpointSlice**([KEP-0752](https://github.com/kubernetes/enhancements/blob/master/keps/sig-network/0752-endpointslices/README.md))。它的核心改动:**把一个 Service 的后端列表,拆成多个小对象(每片默认最多 100 个后端,`--max-endpoints-per-slice` 可配)**。一个 Pod 变了,只需要重写它所在的那一片(最多 100 个后端),其他片不动。**改动的影响面从"整个 Service" 降到"一片",写入和 watch 的成本都降了一个量级**。

> **今天(1.21+)kube-proxy 默认 watch 的是 EndpointSlice,不是老的 Endpoints。** 老的 Endpoints 对象还在(为了向后兼容,有些 controller 还在用),但 kube-proxy 这条数据链已经迁到了 EndpointSlice。我们后面讲源码时,会亲眼看到 kube-proxy 注册的是 EndpointSlice 的 informer。

### 一个细节:Service 怎么"选后端"——靠 label selector

上面提到 controller 按 `spec.selector` 筛 Pod。这背后是 k8s 一个贯穿全局的设计:**label(标签)**。Pod 身上挂着一堆 `key=value` 的小标签(`app=mysql`、`tier=backend`),Service 的 selector 声明"我选带这些标签的 Pod"——**Service 和 Pod 之间的绑定关系,是靠 label 这层松耦合建立的,不是名字、不是 IP**。

这有什么好处?Pod 死了重生、IP 变了、甚至镜像换了,只要新 Pod 还挂着 `app=mysql` 这个标签,Service 自动选中它,后端列表自动更新,门牌背后的内容自动收敛。**这种"基于标签的松耦合",是 k8s 一切服务发现的基础**——它让"会变的 Pod"和"稳定的 Service"之间,有了一道**不被 IP 变化破坏**的连接。

> **小结这一节**:Service 的后端列表,**完全是由一个 controller(Endpoints / EndpointSlice controller)用 reconcile 范式维护的**。这是第 14 章那套范式在"服务发现"这一侧的化身——第 14 章我们说"服务发现也用同一个 reconcile 范式",这一节就是它的具体证明。下一个问题是:这个"后端列表"维护出来了,**谁在每个节点上,用它把"对 ClusterIP 的访问"翻译成"对某个 Pod IP 的访问"?** 这就轮到 kube-proxy。

---

## 三、kube-proxy:把"门牌 → 后端"的翻译,写进每台节点的内核

第二个核心问题:**谁在每个节点上,把对 ClusterIP 的访问翻译成对某个 Pod IP 的访问?**

答案在第 15 章已经剧透过:**kube-proxy**。但第 15 章只说了它"配网络规则",没说规则长什么样。这一节就把它拆透。

### 先澄清最大的误解:kube-proxy 不转发数据包

很多人(包括很多工程师)以为 kube-proxy 是一个"代理服务器"——流量先发给 kube-proxy 进程,kube-proxy 再转发给后端 Pod。**这是错的,而且错得很彻底**。

如果真是这样,那 kube-proxy 这个用户态进程就会成为整台节点所有 Service 流量的瓶颈——每个包都要从内核态拷到用户态、交给 kube-proxy 处理、再拷回内核态发出去。这种**用户态代理**的性能,根本撑不住生产流量。

实际上,kube-proxy 干的事极其"克制":

> **kube-proxy 只负责"配置内核的网络规则"(iptables 或 ipvs 规则),它本身完全不参与数据包的转发。真正的转发,是内核(netfilter / IPVS)根据这些规则,在内核态直接完成的——根本不经过 kube-proxy 进程。**

> **比喻**:kube-proxy 这个名字起得太坑了。它不是"转运货物的转运员",它是"**写转运指南的文员**"。它把"凡运单号 X 的货,送到 Y 舱"这种规则,写进每艘船的本地转运指南(iptables 规则表)。规则写完后,真正搬货的,是船上的叉车(内核 netfilter),它照着指南把货搬走——文员(kube-proxy)只是坐在办公室,偶尔刷新一下指南。**货物从头到尾没经过文员的手**。

这一点理解了,后面就顺了:kube-proxy 是个**控制面进程**,数据面的活全在内核里干。所以它的性能开销极低(平时只在 Service/Endpoint 变化时刷新规则),而真正转发的性能极高(纯内核态,不拷贝到用户态)。

### kube-proxy 也是 reconcile:watch Service/EndpointSlice → 写内核规则

kube-proxy 怎么知道"该写哪些规则"?——**还是 watch**。

每个节点上的 kube-proxy 进程,通过 informer(回扣第 15 章)watch 两类资源:

1. **Service**(经 apiserver):知道"集群里有哪些门牌、各自的 ClusterIP 和端口"。
2. **EndpointSlice**(经 apiserver):知道"每个门牌背后现在挂哪些 Pod IP"。

每次这两类资源有变化(新建一个 Service、删一个 Service、某个 Service 的后端 Pod 列表变了),kube-proxy 都被事件唤醒,跑一轮它自己的 reconcile——这一轮在源码里叫 **`syncProxyRules`**。它干的事是:

1. 读出当前所有 Service 和它们的后端 EndpointSlice(从 informer 的本地缓存读)。
2. 算出"内核里**应该**有什么样的 iptables/ipvs 规则"。
3. 和"内核里**现在**有什么规则"对比(它自己缓存了一份上次的规则快照)。
4. 把差异部分,**批量地**写进内核(iptables 模式下用一次 `iptables-restore`,ipvs 模式下用 netlink 调 IPVS)。

**这又是一轮 reconcile**:读期望(Service + Endpoints)、读实际(内核当前规则)、算偏差、写回去。第 14 章那套范式,在"配网络规则"这件事上,再演一次。

> **这一节先讲到这里,源码细节留到后面"关键源码精读"一节**。我们先搞清楚:kube-proxy 写进内核的规则,长什么样、怎么实现负载均衡。

---

## 四、iptables 模式:KUBE-* 链与概率负载均衡

kube-proxy 默认(也是历史上最经典)的模式,叫 **iptables 模式**。它写进内核的,是一堆以 `KUBE-` 开头的 iptables 链。这一节我们就把这些链拆开,看清"对 ClusterIP 的访问,是怎么一步步被翻译、被负载均衡到一个具体 Pod IP 的"。

### 一条数据包的旅程:从 ClusterIP 到 Pod IP

假设你有一个 Service `mysql`,ClusterIP `10.96.0.10`,端口 3306,背后有 3 个 Pod(IP 假设 `10.244.1.5`、`10.244.2.7`、`10.244.3.9`)。集群里某个 Pod(比如一个 web,IP `10.244.4.20`)要连这个数据库,它 `curl 10.96.0.10:3306`。

这个数据包从 web Pod 发出来,到了**所在节点**的内核(因为 Pod 的网络包都要经过宿主机内核转发),接下来发生的事,全是这一节要讲的。我们把这条数据包的旅程,画成一张图:

```
   web Pod 发出包: src=10.244.4.20 dst=10.96.0.10 dport=3306
                              │
                              ▼  (包进宿主内核,命中 nat 表 PREROUTING 链)
                   ┌─────────────────────┐
                   │   KUBE-SERVICES     │  ← 所有 Service 的"入口登记台"
                   │   匹配 dst=10.96.0.10│     (一条规则对应一个 ClusterIP)
                   │   dport=3306        │
                   └──────────┬──────────┘
                              │ jump
                              ▼
                   ┌─────────────────────┐
                   │  KUBE-SVC-XXXXXXXX  │  ← 这个 Service 的"分发表"
                   │  (用 statistic 模块 │     (N 条概率规则 + 1 条兜底)
                   │   做概率负载均衡)    │
                   └──────────┬──────────┘
              ┌───────────────┼───────────────┐
              │ 1/3 概率       │ 1/2 概率       │ 兜底
              ▼               ▼               ▼
       ┌────────────┐  ┌────────────┐  ┌────────────┐
       │KUBE-SEP-XX1│  │KUBE-SEP-XX2│  │KUBE-SEP-XX3│  ← 每个 Pod 一条
       │ DNAT 到     │  │ DNAT 到     │  │ DNAT 到     │     "DNAT 到 Pod IP"
       │ 10.244.1.5 │  │ 10.244.2.7 │  │ 10.244.3.9 │
       └────────────┘  └────────────┘  └────────────┘
                              │
                              ▼  (包的目标地址被改成某个 Pod IP)
                  内核按新目标路由,把包送给那个 Pod
```

我们一层一层拆这四条链。

### 第一层:KUBE-SERVICES——Service 的"入口登记台"

最外层那条链叫 **`KUBE-SERVICES`**,它是所有 Service 流量的入口。kube-proxy 在 nat 表的 `PREROUTING` 链(进来的包)和 `OUTPUT` 链(本机自己发出的包)上,都挂了一条 `-j KUBE-SERVICES`。所以**任何经过这台节点的包,都会先进 KUBE-SERVICES 这条链过一遍**。

KUBE-SERVICES 里面,长这样(示意,真实规则更复杂):

```
# 每一条规则对应一个 Service 的 ClusterIP
-A KUBE-SERVICES -d 10.96.0.10/32 -p tcp --dport 3306 -j KUBE-SVC-XXXXXXXX   # mysql
-A KUBE-SERVICES -d 10.96.0.20/32 -p tcp --dport 80   -j KUBE-SVC-YYYYYYYY   # web
-A KUBE-SERVICES -d 10.96.0.30/32 -p tcp --dport 6379 -j KUBE-SVC-ZZZZZZZZ   # redis
...
# 末尾还有一条 KUBE-NODEPORTS(用于 NodePort 类型,见后文)
```

读这几行,你就明白了:**KUBE-SERVICES 干的活,就是"按目标 IP + 端口匹配,然后跳到对应 Service 的分发表"**。它把"dst=10.96.0.10 dport=3306"这个组合,路由到 KUBE-SVC-XXXXXXXX 这条子链。**这一步还没动数据包,只是做了"哪个 Service"的分流**。

> 注意链名后缀 `XXXXXXXX`:它不是随便起的,是 kube-proxy 用 `sha256("namespace/svcname:port:proto")` 算个哈希、再 base32 截前 16 个字符生成的。这样保证不同 Service 的链名不撞、而且确定性(同样的 Service 在每个节点算出来都是这个名)。

### 第二层:KUBE-SVC-* ——这个 Service 的"分发表",负载均衡就在这里

跳进 KUBE-SVC-XXXXXXXX 这条子链,真正的"负载均衡"发生了。它的核心,是用 iptables 的 **`statistic` 模块**做概率分发。还是上面那个 3 个后端的例子,KUBE-SVC-XXXXXXXX 里面的规则长这样:

```
-A KUBE-SVC-XXXXXXXX -m statistic --mode random --probability 0.3333333333 -j KUBE-SEP-XX1
-A KUBE-SVC-XXXXXXXX -m statistic --mode random --probability 0.5000000000 -j KUBE-SEP-XX2
-A KUBE-SVC-XXXXXXXX -j KUBE-SEP-XX3
```

注意这串概率,极其巧妙。我们一步一步算每个后端被选中的实际概率:

- **第一条规则**:`--probability 0.3333`(就是 1/3)。包进来后有 1/3 概率命中第一条,跳到 KUBE-SEP-XX1。剩下 2/3 概率不命中,继续往下走。
- **第二条规则**:`--probability 0.5`(就是 1/2)。**注意,这个 1/2 是"在已经没命中第一条的前提下"的 1/2**。所以走到这里的包(剩 2/3),一半命中,一半继续。命中第二条的概率 = 2/3 × 1/2 = **1/3**。
- **第三条规则**:没有 statistic,是个**兜底**——前两条都没命中的包(剩 1/3),全部走这条。概率 = 2/3 × 1/2 × 1 = **1/3**。

三个后端,**每个都是 1/3,完全均匀**。这就是 kube-proxy 在 iptables 模式下做负载均衡的全部秘密——**用一串 `1/n, 1/(n-1), …, 1/2` 的概率,组合出每个后端恰好等概率的分流**。

数学上,这是个"递减概率序列 + 兜底"的设计。把通项写出来:对于 N 个后端,第 i 条规则的概率是 `1/(N-i+1)`(i 从 1 数),最后一条(i = N)是兜底(概率 1)。可以证明每个后端被选中的实际概率都是 1/N。这个证明留给读者拿纸笔算(提示:用条件概率展开,会发现是个望远镜求和)。

> **为什么用概率,而不是轮询(round-robin)?** 因为 iptables 是**无状态**的——它只看每个包的头部做匹配,不记得"上一个包给了谁"。所以它没法做严格的轮询("这次给 A,下次给 B"),只能用"每个包独立地按概率抽"来近似轮询。在大流量下,概率抽样的统计结果和轮询几乎没区别(每个后端分到的包数差不多)。

> **为什么是 `statistic --mode random`,不是别的?** 因为 iptables 的 `statistic` 模块支持两种模式:`random`(每个包独立按概率)和 `nth`(每 N 个包命中一次,这是真轮询)。kube-proxy 选 `random`,是因为 `nth` 在多核并发下有锁竞争(要维护"当前第几个"这个计数器),性能差;`random` 是无状态的,多核并发完全无冲突。

### 第三层:KUBE-SEP-* ——DNAT 到具体的 Pod IP

最后,包跳进了某一条 KUBE-SEP-* 链(比如 KUBE-SEP-XX1,对应 Pod `10.244.1.5`)。这一条链的最后一行,就是真正"改包"的动作——**DNAT**(回扣第 12 章,那里讲过 DNAT 把目标地址翻译):

```
-A KUBE-SEP-XX1 -p tcp -j DNAT --to-destination 10.244.1.5:3306
```

这一行执行完,数据包的**目标地址**就从 `10.96.0.10`(那个根本不存在的虚拟 ClusterIP)被改成了 `10.244.1.5`(一个真实的 Pod IP)。接下来,内核按新的目标地址路由这个包——它顺着 Pod 网络送到那个 Pod 的 veth 网卡(回扣第 12 章),那个 Pod 收到包,处理请求,回包原路返回(回包时反向再做一次 SNAT,把源地址从 Pod IP 改回 ClusterIP,这样访问者完全感知不到背后被改过包)。

> **这就是 kube-proxy 在 iptables 模式下,把"对 ClusterIP 的访问"翻译成"对 Pod IP 的访问"的全部机制**。它没造任何新东西——三层 KUBE-* 链(KUBE-SERVICES / KUBE-SVC-* / KUBE-SEP-*),加上第 12 章早就讲过的 DNAT,加上 `statistic` 模块的概率匹配。**kube-proxy 的高明,不在发明新机制,而在"把这套机制,在每个节点上、为成百上千个 Service,自动化、批量、原子地配进内核"**。

### 顺带:KUBE-MARK-MASQ 和 KUBE-POSTROUTING——SNAT 的另一半

上面只讲了 DNAT(目标地址翻译)。完整的 Service 流量,还有另一半——**SNAT(源地址翻译)**,这俩是配对的。回包时要把源地址从 Pod IP 改回 ClusterIP,访问者才感知不到。

但有一个特殊情况需要 SNAT:**如果访问者和后端 Pod 在同一个节点**(比如 web Pod 和 mysql Pod 都在 node-1 上),web 发给 ClusterIP,内核 DNAT 改成 mysql Pod IP,mysql 收到包一看"源 IP 是和我同一节点的 web Pod"——它回包时直接走本地 loopback 给 web,没经过宿主内核做反向 SNAT。结果 web 收到的回包,源地址是 mysql Pod IP,而它发请求时目标是 ClusterIP——**源和目标对不上,内核直接把这个回包当垃圾丢了**(这是 Linux 的反向路径过滤,反欺骗机制)。

kube-proxy 解决这个问题的办法是:在某些情况下(具体是哪些情况,涉及 `externalTrafficPolicy` 等配置,这里不展开),给包打一个**标记(mark)**,然后在包出去前(`POSTROUTING`)看到这个标记就做 MASQUERADE(把源 IP 改成节点 IP)。这两条链分别叫 **`KUBE-MARK-MASQ`**(打标记,标记值是 `0x4000`,即第 14 位为 1)和 **`KUBE-POSTROUTING`**(看到标记就 SNAT,然后清掉标记)。

这个细节不影响主线理解,记住一句话就够:**KUBE-SEP-* 里除了 DNAT,还有可能挂 KUBE-MARK-MASQ 做标记,配合 KUBE-POSTROUTING 完成 SNAT,处理"同节点访问"这种边界情况**。

---

## 五、ipvs 模式:为什么 Service 上万条时,它比 iptables 快

iptables 模式讲到这里,你会发现它有个**根本性的性能瓶颈**:KUBE-SERVICES 那条链里,有几百上千条规则,**每个进来的包都要从上往下、一条一条地匹配**,直到命中某一条。这就是 iptables 的天性——**规则是线性表,匹配是 O(n)**。

集群里只有几十个 Service 时,这条线性表就几十条,匹配开销可以忽略。可一旦 Service 数量爬到几千、上万,**每个包要在这条链里走几百上千次比较**,网络性能肉眼可见地下降。再加上 Service 变化时,kube-proxy 要重写一大堆规则,`syncProxyRules` 一次跑几秒甚至十几秒——这在大规模集群里是致命的。

### 不这样会怎样:iptables 在大规模下的三个痛点

把 iptables 模式的痛点展开,主要有三条:

1. **规则匹配 O(n)**:包进 KUBE-SERVICES,要从头匹配到尾。Service 越多,每个包的转发延迟越高。这是**数据面**的痛。
2. **规则更新成本高**:iptables 规则是"全表结构",改一条规则往往要重写整个表(kube-proxy 用 `iptables-restore` 批量替换,但仍是全表替换)。Service 频繁变化时,**控制面**的 `syncProxyRules` 跑得很慢,有时跟不上变化速度。
3. **规则数量爆炸**:每个 Service 后端有 N 个 Pod,iptables 模式要生成约 N 条 KUBE-SEP 规则 + N 条 KUBE-SVC 概率规则。一个 5000 节点、2000 Service、每 Service 10 Pod 的集群,**每个节点上至少 20000 条 iptables 规则**——内核里规则表膨胀,内存和 CPU 都吃紧。

> 这是 k8s 早期在大规模集群(几千节点、上万 Service)遇到的真实瓶颈。华为的工程师在 2018 年的[那篇经典博客](https://kubernetes.io/blog/2018/07/09/ipvs-based-in-cluster-load-balancing-deep-dive/)里,把这个数字明明白白写了出来,作为推动 ipvs 模式上线的直接动因。

### 所以这样:ipvs 模式——内核里专门的四层负载均衡器

k8s 给出的解法,是切换到 **ipvs 模式**。IPVS(IP Virtual Server)是 Linux 内核里**早就有的、专门为四层负载均衡设计**的子系统,源码在 [`net/netfilter/ipvs/`](https://github.com/torvalds/linux/tree/master/net/netfilter/ipvs),2000 年前后就进了内核主线(比 docker 早十几年)。它和 iptables 是同辈的 netfilter 子系统,但走的路完全不同。

IPVS 的核心数据结构是**哈希表**——所有"虚拟服务"(Virtual Server,对应一个 Service 的 ClusterIP:port)存在一个哈希表里,**查一个虚拟服务是 O(1)** 的,不管集群里有一万个虚拟服务还是一个。这一点,直接把 iptables 的 O(n) 痛点干掉了。

ipvs 模式下,kube-proxy 写进内核的,不再是几千条 iptables 规则,而是**一条 IPVS 的"虚拟服务 + 后端"记录**:

- 每个 Service,对应 IPVS 里一个 **Virtual Server**(VIP = ClusterIP:port),配上一个**调度算法**(scheduler)。
- 每个 Service 的后端 Pod,对应这个 Virtual Server 下的一个 **Real Server**(RIP = Pod IP:port),可以带权重。

数据包的旅程,也大不一样。iptables 模式下,包要在 KUBE-* 链里一条条比;ipvs 模式下,包一进内核的 PREROUTING,就被 IPVS hook 拦下,**一次哈希查到对应的 Virtual Server,然后用调度算法选一个 Real Server,直接 DNAT 过去**。全程 O(1) 查找 + 一次 DNAT,没有线性规则匹配。

而且 IPVS 自带**一堆成熟的调度算法**,远比 iptables 的 `statistic --mode random` 强大:

- `rr`(round-robin,轮询)——默认。
- `wrr`(weighted round-robin,加权轮询)——后端可以带权重。
- `lc`(least-connections,最少连接)——把请求发给当前连接数最少的后端,适合长连接。
- `wlc`(weighted least-connections)——lc 的加权版。
- `sh`(source hashing)——按源 IP 哈希,同一个客户端总落到同一个后端(会话保持)。
- `dh`、`sed`、`nq`、`mh`……十几种。

这些算法都是内核里写好了的、经过二十多年生产检验的——kube-proxy 只需要选一个(默认 `rr`),IPVS 内核模块替你做剩下的所有事。

> **比喻**:iptables 模式像是"门牌登记台前,排着几千米长的纸条清单,转运员要一条条翻找运单号"——运单多了,翻找慢得不行。ipvs 模式像是"换成了一台电子查询机,运单号一输,秒查出对应的舱位"——再多的运单,查询都是常数时间。这就是数据结构的力量:**线性表换成哈希表,O(n) 变 O(1)**。

### 一个细节:ipvs 模式下,iptables 并没有完全消失

值得说清的一点:切到 ipvs 模式,**iptables 规则不是完全没有**。IPVS 只管"虚拟服务 → 后端"的负载均衡和 DNAT,但有些事它不干,还得靠 iptables:

- **SNAT / MASQUERADE**:某些场景下要做源地址翻译(比如同节点访问的边界情况),IPVS 不直接做,kube-proxy 仍然配 `KUBE-MARK-MASQ` 和 `KUBE-POSTROUTING` 这两条 iptables 链来打标记 + 做 MASQUERADE。
- **包过滤**:有些过滤规则(比如拒绝访问没有后端的 Service)也还是用 iptables 实现(`KUBE-IPVS-FILTER` 等)。

所以 ipvs 模式是"**IPVS 做核心负载均衡 + iptables 做辅助的 SNAT/过滤**",两者协作。但**关键的、决定性能的那部分(虚拟服务查找 + 后端选择)迁到了 IPVS**,iptables 只剩下少量辅助规则——这就是它快的根本原因。

### 一个不能不提的新格局:今天,nftables 来了

讲到这里,有一个**必须诚实交代**的新格局。上面"iptables vs ipvs"的对比,是 k8s 从 1.9 到 1.34 这几年的经典叙事——ipvs 在大规模下完胜 iptables。但事情在演进:

- **iptables 模式自己变快了**:从 k8s 1.28 开始,`syncProxyRules` 引入了"增量同步"(只改变化的部分,不再每次全表重写),iptables 模式的控制面性能大幅改善。
- **nftables 模式登场**:nftables 是 iptables 的现代继任者(同样是 netfilter 家族,但数据结构和语法都重做了,规则匹配更高效),k8s 从 1.26 引入 kube-proxy 的 nftables 模式,1.29 进 beta,**1.33(2026 年初)stable 并成为新集群的推荐默认**。
- **ipvs 反而被 deprecated**:出乎很多人意料,在 k8s 1.35(2026 年中),官方文档把 ipvs 模式标记为 **deprecated(弃用)**。原因是"IPVS 的内核 API 和 k8s Service 的语义匹配得不太好"(比如 IPVS 的会话保持、健康检查语义和 k8s 想要的不完全一致),维护成本高。官方的建议是:**用 nftables 模式替代 iptables 和 ipvs,它的性能比这两者都好**。

> 这意味着,如果你今天(2026 年中)起一个新集群,kube-proxy 的默认模式已经是 nftables,而不是 iptables 或 ipvs。但 iptables 模式的原理(KUBE-* 链、概率负载均衡、DNAT)依然是理解 Service 数据面的最佳入口——nftables 只是换了规则的表达形式,底层的"匹配 → 概率分发 → DNAT"逻辑是一脉相承的。所以本章用 iptables 模式讲清原理,既是对历史的诚实交代,也是理解 nftables/ipvs 的基础。

---

## 六、Service 的四种类型:门牌不同的"曝光方式"

到这里,Service 的核心机制(稳定 ClusterIP + 一组后端 Pod + kube-proxy 写规则做 DNAT)讲完了。但实际用 k8s 时,你会发现 Service 有好几种 `type`,它们的"曝光方式"各不相同。这一节快速过一遍,讲清**每种类型解决什么、为什么不这样会怎样**。

### ClusterIP:默认,集群内部用的门牌

最基础的类型,`type: ClusterIP`(也是默认值)。它做的事,就是前面几节讲的那一套:**分配一个稳定的 ClusterIP(只在集群内部路由可达),kube-proxy 在每个节点上为它写 DNAT 规则**。

- **曝光范围**:只在集群**内部**。集群外的机器 ping 这个 ClusterIP,ping 不通——因为它不在任何真实网卡上,只是个触发 iptables 的标记。
- **解决什么**:集群内部的 Pod 之间互相访问(web → mysql、api → cache)。
- **不这样会怎样**:如果你只想集群内部用,却用了对外曝光的类型(下面三种),等于把内部服务裸奔到公网,安全风险巨大。

绝大多数 Service 都是 ClusterIP 类型。下面三种类型,都是在 ClusterIP 基础上"往外多开一个口子"。

### NodePort:在每台节点上开一个端口,对外曝光

`type: NodePort`。它在 ClusterIP 的基础上,多做一件事:**在集群每一台节点上,开一个固定端口(默认范围 30000-32767),把这个端口收到的流量,转发到后端 Pod**。

- **曝光范围**:集群**外部**——你可以在任何一台节点的公网 IP 上,用那个 NodePort 访问这个 Service。
- **解决什么**:让集群外面的客户端(比如你的浏览器、外部的监控)能访问集群里的服务,不用依赖云厂商的负载均衡器。
- **不这样会怎样**:如果只用 ClusterIP,外部访问不了,你必须自己在节点上跑个 nginx 反代,或者用 `kubectl port-forward`(临时调试用)。NodePort 把这种"对外开口"标准化了。

在 iptables 规则上,NodePort 多了一条专门的链叫 **`KUBE-NODEPORTS`**——它挂在 KUBE-SERVICES 的末尾,匹配"目标是本节点、端口是某个 NodePort"的包,然后跳到对应的 KUBE-SVC-* 链,后面就和 ClusterIP 一样了。

NodePort 的缺点:端口范围有限(只有 30000-32767 这 2768 个)、暴露的是节点 IP(任何一个节点挂了,那个 IP 上的访问就断)、客户端不知道该连哪个节点 IP。所以生产环境通常不在 NodePort 上直接停,而是再叠一层 LoadBalancer。

### LoadBalancer:云厂商给你一个公网负载均衡器

`type: LoadBalancer`。它在 NodePort 的基础上,再多做一件事:**调用底层云厂商(AWS、GCP、Azure 等)的 API,自动创建一个云上的负载均衡器(ELB、GLB 等),把它的流量导向集群节点的 NodePort**。

- **曝光范围**:公网——你拿到一个云负载均衡器的公网 IP/域名,全球都能访问。
- **解决什么**:生产环境对外提供服务。云负载均衡器自带高可用(多个后端节点、健康检查、公网带宽),比裸 NodePort 可靠得多。
- **不这样会怎样**:你要么自己管一堆 NodePort + 外部 LB 的对接(运维噩梦),要么用 NodePort(没有高可用、没有公网 IP)。

LoadBalancer 类型依赖 k8s 的 **Cloud Controller Manager**(云控制器),它会和云厂商 API 通信。在裸机集群(没云厂商)上,这个类型不能直接用——除非装个 [MetalLB](https://metallb.universe.tf/) 这样的裸机 LB 实现。

### Headless:不要 ClusterIP,DNS 直接返回 Pod IP

最特殊的一种。严格说,Headless 不是 `type` 的一个枚举值,而是 `type: ClusterIP` 加上 `clusterIP: None` 这种**特殊配置**——意思是"**我不要 ClusterIP 这个虚拟门牌,我自己处理服务发现**"。

- **不这样会怎样(为什么要 Headless)**:有些场景,客户端**必须**知道后端每个 Pod 的真实 IP,不能让 Service 替它做负载均衡。典型的有:
  - **StatefulSet + 主从复制**:比如 MySQL 主从、Redis 集群、Elasticsearch——客户端要明确连"主节点",不能被随机负载均衡到一个从节点。每个 Pod 有自己的 DNS 名(比如 `mysql-0.mysql.default.svc.cluster.local`),客户端按名字连指定的那个。
  - **自己想用客户端负载均衡**:服务网格(Service Mesh,如 Istio)想自己在客户端做精细的负载均衡策略,不希望内核的 iptables/IPVS 横插一脚。

- **Headless 怎么工作**:kube-proxy **不为 Headless Service 配任何规则**(没有 ClusterIP 可配)。取而代之,当客户端查 DNS(比如 `mysql.default.svc.cluster.local`)时,CoreDNS **直接返回后端所有 Pod 的 IP 列表**(而不是返回一个 ClusterIP)。客户端拿到这个列表,自己决定连哪个。

> **比喻**:Headless Service 是"不要统一运单号,你直接问每箱货在哪艘船,自己挑一艘"。它放弃了"门牌"的便利,换来了对后端的精细控制。

Headless 是个"出格栅"的设计——它默认 k8s 别管我,我自己来。理解它,你就理解了 k8s 在"自动化"和"灵活性"之间,留了一道缝给高级用户。

---

## 七、服务发现的两种姿势:DNS vs 环境变量

最后讲一个贯穿全章的问题:**客户端 Pod,怎么"知道"某个 Service 的门牌(ClusterIP)是多少?** 这叫**服务发现(service discovery)**。k8s 给了两种姿势,各适合不同场景。

### 方式一:DNS 名(推荐)

集群里通常跑着一个 **CoreDNS**(k8s 1.11 起的默认 DNS 服务器,取代了老的 kube-dns)。它给每个 Service 自动建一条 DNS 记录,名字长这样:

```
<service-name>.<namespace>.svc.cluster.local
```

比如 `default` namespace 下的 `mysql` Service,DNS 名是 `mysql.default.svc.cluster.local`。客户端 Pod 要连它,连接字符串写这个 DNS 名就行——CoreDNS 把它解析成 ClusterIP,剩下的 DNAT 由 kube-proxy 的规则接管。

DNS 方式的优点:

- **活的**:Service 后端 Pod 变了,ClusterIP 一般不变;就算 ClusterIP 变了(比如删了重建),DNS 记录会被 controller 自动更新,客户端重新解析就能拿到新 IP。
- **跨 namespace**:DNS 名带 namespace,客户端可以明确连别的 namespace 的 Service(`mysql.prod.svc.cluster.local`)。
- **支持 Headless**:Headless Service 的 DNS 解析直接返回 Pod IP 列表(见上节)。
- **支持 SRV 记录**:命名端口可以用 `_portname._protocol.mysql.default.svc.cluster.local` 这种 SRV 记录查到端口。

**DNS 是 k8s 推荐的服务发现方式**,几乎所有生产集群都用它。

### 方式二:环境变量(历史遗留,有坑)

k8s 还提供了另一种方式:**kubelet 在创建 Pod 时,把集群里所有 Service 的 ClusterIP 和端口,作为环境变量注入到 Pod 里**。注入的格式是:

```
<SERVICE_NAME>_SERVICE_HOST=<ClusterIP>
<SERVICE_NAME>_SERVICE_PORT=<port>
```

名字会被大写、连字符换成下划线。比如 Service `redis-primary`(ClusterIP `10.96.0.11`,端口 6379),kubelet 注入的环境变量是:

```
REDIS_PRIMARY_SERVICE_HOST=10.96.0.11
REDIS_PRIMARY_SERVICE_PORT=6379
```

(顺带一提,集群里那个特殊的 `kubernetes` Service——它就是 apiserver 自己,在 `default` namespace——也会被注入成 `KUBERNETES_SERVICE_HOST` / `KUBERNETES_SERVICE_PORT`,Pod 因此知道怎么连 apiserver。)

环境变量方式**有个致命的坑**:**它只在 Pod 创建时,把"那时已经存在的" Service 注入进去**。如果 Pod 创建之后,集群里新建了一个 Service,这个老 Pod 的环境变量里**没有**这个新 Service——因为它创建时,这个 Service 还不存在。

这就引出一个**强制的启动顺序**:**必须先创建 Service,再创建要访问它的 Pod**。否则 Pod 起来时,环境变量里没有 Service 的 IP,它连不上。这种"启动顺序依赖"在分布式系统里是脆弱的(谁也保证不了哪个先起来),所以环境变量方式现在**基本被 DNS 取代了**,只在一些老应用(改不了连接逻辑的)里还在用。

> **比喻**:DNS 是"每次寄件前查一下当前最新的运单号",环境变量是"寄件员入职时,公司给他发了一张运单号通讯录,之后就照这张表寄件"——通讯录是入职那一刻的快照,之后新开的运单号他都查不到。**前者(DNS)更适应变化,这是它成为推荐方式的原因**。

---

## 关键源码精读:kube-proxy 怎么把规则"刷"进内核

理论讲完了,我们钻进源码,看 kube-proxy 那个核心的 reconcile 循环——`syncProxyRules`——到底长什么样。这一节是本章的源码高潮。

kube-proxy 的两种模式(iptables 和 ipvs),核心循环都叫 `syncProxyRules`,但实现完全不同。我们分别看一眼它们的关键片段。

### iptables 模式:逐个 Service 生成 KUBE-* 链

iptables 模式的核心代码在 [`pkg/proxy/iptables/proxier.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/proxy/iptables/proxier.go) 这个文件里。`Proxier` 这个结构体(就是 iptables 模式的 kube-proxy 实现)的核心字段,你能一眼看出它干的是"攒规则、批量刷"的活:

```go
// (摘自 kubernetes/pkg/proxy/iptables/proxier.go,type Proxier struct)
type Proxier struct {
	// ... 监听 service 和 endpoints 变化的字段
	endpointsChanges *proxy.EndpointsChangeTracker
	serviceChanges   *proxy.ServiceChangeTracker

	mu           sync.Mutex   // 保护下面这些字段
	svcPortMap   proxy.ServicePortMap   // 当前所有 Service
	endpointsMap proxy.EndpointsMap     // 当前所有 Endpoints

	// 关键:这几个 buffer 是"攒规则"的地方
	iptablesData *bytes.Buffer   // 最终要刷给内核的整张表
	filterChains proxyutil.LineBuffer
	filterRules  proxyutil.LineBuffer
	natChains    proxyutil.LineBuffer   // KUBE-* 链的定义
	natRules     proxyutil.LineBuffer   // KUBE-* 链里的规则
	// ...
}
```

读这几个 `LineBuffer` 字段,你就理解了 kube-proxy 的工作方式:**它不一条一条地往内核里 `iptables -A`(那样太慢),而是先把所有要写的链和规则,攒到几个内存 buffer 里,最后一口气 `iptables-restore` 整张表刷进去**。这是它性能能扛住大规模的关键。

核心方法 `syncProxyRules` 长达几百行,我们抓三个关键片段。

**片段一:遍历所有 Service,为每个生成 KUBE-SVC-* 链。** `syncProxyRules` 的主体是一个大循环,遍历 `svcPortMap` 里的每个 Service:

```go
// (摘自 syncProxyRules,简化展示)
for svcName, svc := range proxier.svcPortMap {
    // ... 算出这个 Service 对应的链名(svcChain = KUBE-SVC-XXXXXXXX)
    svcChain := svc.ServicePortChainName

    // 在 natChains buffer 里写: "新建这条链"
    natChains.Write(utiliptables.MakeChainLine(svcChain))

    // 在 natRules buffer 里写 KUBE-SERVICES 里的跳转规则:
    // "凡是 dst=ClusterIP dport=port 的包,jump 到 svcChain"
    natRules.Write("-A", string(kubeServicesChain),
        "-d", svc.ClusterIPString(), "-p", protocol, "--dport", svc.PortString(),
        "-j", string(svcChain))
    // ... 继续生成 KUBE-SEP-* 链
}
```

(注:以上为简化示意,真实代码用 `proxier.natRules.Write(args...)` 这种链式调用,参数构造更繁琐,但逻辑就是上面这几行。)

**片段二:为每个后端 Pod 生成 KUBE-SEP-* 链(含 DNAT)。** 这是真正"改包"的地方——每条 KUBE-SEP 链的最后一行,都是 DNAT 到 Pod IP:

```go
// (摘自 syncProxyRules 内,生成每个 endpoint 的 KUBE-SEP-* 链)
for _, ep := range allLocallyReachableEndpoints {
    epInfo := ep.(*endpointInfo)
    endpointChain := epInfo.ChainName   // KUBE-SEP-YYYYYYYY

    natChains.Write(utiliptables.MakeChainLine(endpointChain))   // 新建这条链

    // 写 DNAT 规则:跳进来的包,目标地址改成 Pod IP:port
    args = append(args[:0], "-A", string(endpointChain))
    args = proxier.appendServiceCommentLocked(args, svcPortNameString)
    // ... 同节点访问的 hairpin/masquerade 标记
    args = append(args, "-m", protocol, "-p", protocol,
        "-j", "DNAT", "--to-destination", epInfo.String())  // ← DNAT!
    natRules.Write(args)
}
```

读这一行 `"-j", "DNAT", "--to-destination", epInfo.String()`,你会发现:**整章讲的"把 ClusterIP 翻译成 Pod IP",在源码里就是这一行 iptables 规则**。`epInfo.String()` 就是那个 Pod 的 `IP:port`。kube-proxy 把它写进 buffer,等会儿一口气刷给内核。

**片段三:概率负载均衡的规则生成。** 那串巧妙的 `1/n, 1/(n-1), ...` 概率,在源码哪里?在 `writeServiceToEndpointRules` 这个辅助方法里:

```go
// (摘自 pkg/proxy/iptables/proxier.go 的 writeServiceToEndpointRules,简化展示)
numEndpoints := len(endpoints)
for i, ep := range endpoints {
    epInfo := ep.(*endpointInfo)
    args = append(args[:0], "-A", string(svcChain))   // 在 KUBE-SVC-* 链里
    if i < (numEndpoints - 1) {
        // 不是最后一条:加概率匹配
        args = append(args, "-m", "statistic",
            "--mode", "random",
            "--probability", proxier.probability(numEndpoints-i))  // ← 1/(n-i)
    }
    // 最后一条(i == numEndpoints-1):不加概率,兜底
    natRules.Write(args, "-j", string(epInfo.ChainName))   // jump 到 KUBE-SEP-*
}
```

读这个循环,那串概率的生成一目了然:`proxier.probability(numEndpoints-i)` 就是 `1/(numEndpoints-i)`,随着 `i` 从 0 涨到 `numEndpoints-2`,概率依次是 `1/n, 1/(n-1), ..., 1/2`;最后一个 `i = numEndpoints-1` 不进 if 分支(因为 `i < numEndpoints-1` 不成立),直接兜底 jump。**前面讲的那个"递减概率 + 兜底"的负载均衡数学,在源码里就是这么一个 for 循环**。

而 `proxier.probability` 这个函数本身极其简单:

```go
// (kubernetes/pkg/proxy/iptables/proxier.go)
func (proxier *Proxier) probability(n int) string {
    return fmt.Sprintf("%0.10f", 1.0/float64(n))
}
```

一行算 `1.0/n`,格式化成 10 位小数。**负载均衡的全部数学,浓缩在一个除法里**。

**片段四:一口气刷给内核。** 所有规则攒到 `iptablesData` 这个 buffer 之后,最后一步,是一次 `iptables-restore`(原子的、批量的):

```go
// (摘自 syncProxyRules 末尾)
proxier.iptablesData.Reset()
proxier.iptablesData.WriteString("*filter\n")
proxier.iptablesData.Write(proxier.filterChains.Bytes())
proxier.iptablesData.Write(proxier.filterRules.Bytes())
proxier.iptablesData.WriteString("COMMIT\n")
proxier.iptablesData.WriteString("*nat\n")
proxier.iptablesData.Write(proxier.natChains.Bytes())
proxier.iptablesData.Write(proxier.natRules.Bytes())
proxier.iptablesData.WriteString("COMMIT\n")

// 一次原子地把整张 filter + nat 表刷进内核
err := proxier.iptables.RestoreAll(proxier.iptablesData.Bytes(),
    utiliptables.NoFlushTables, utiliptables.RestoreCounters)
```

注意这一步的精妙:**整张 `filter` + `nat` 表,在一次 `iptables-restore` 调用里原子地刷进去**。这意味着即使有几千条规则,内核看到的也是"一次完整的、一致的替换",不会出现"刷到一半、规则不一致"的中间态。这种"攒一批 → 原子刷"的设计,是 kube-proxy 在大规模下保持稳定的关键工程技巧。

### ipvs 模式:调内核的 IPVS 接口

切到 ipvs 模式,核心代码在 [`pkg/proxy/ipvs/proxier.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/proxy/ipvs/proxier.go)。它的 `syncProxyRules` 不再生成几千条 iptables 规则,而是直接调内核的 IPVS 接口(通过一个叫 [`utilipvs`](https://github.com/kubernetes/kubernetes/tree/master/pkg/util/ipvs) 的库,底层是 netlink 系统调用),为每个 Service 创建/更新一个 **Virtual Server**,为每个后端 Pod 创建/更新一个 **Real Server**:

```go
// (摘自 ipvs proxier 的 syncProxyRules,简化展示)
for svcPortName, svcPort := range proxier.svcPortMap {
    // 构造一个 IPVS VirtualServer: VIP=ClusterIP, port, protocol, scheduler=rr
    serv := &utilipvs.VirtualServer{
        Address:   svcInfo.ClusterIP(),
        Port:      uint16(svcInfo.Port()),
        Protocol:  string(svcInfo.Protocol()),
        Scheduler: proxier.ipvsScheduler,   // 默认 "rr"
    }
    // ... 处理 session affinity 等

    // 调内核:创建或更新这个 Virtual Server(并把 VIP 绑到 dummy 网卡 kube-ipvs0)
    if err := proxier.syncService(svcPortNameString, serv, true, alreadyBoundAddrs); err == nil {
        // 调内核:为这个 Virtual Server 同步所有 Real Server(后端 Pod)
        if err := proxier.syncEndpoint(svcPortName, internalNodeLocal, serv); err != nil {
            proxier.logger.Error(err, "Failed to sync endpoint for service", ...)
        }
    }
}
```

注意几个关键点:

- `proxier.ipvsScheduler` 默认是字符串 `"rr"`——这就是 ipvs 模式默认用轮询算法的地方(想换成 `lc`、`sh` 等,改 kube-proxy 启动参数 `--ipvs-scheduler` 即可)。
- `proxier.syncService` 内部会调 `proxier.ipvs.AddVirtualServer(vs)` / `UpdateVirtualServer(vs)` / `GetVirtualServer(vs)`——这些最终走 netlink,让内核的 IPVS 模块创建/更新一条虚拟服务记录。
- `proxier.syncEndpoint` 类似,调 `AddRealServer` / `UpdateRealServer`,把每个后端 Pod 注册到这个 Virtual Server 下。
- 还有个细节:`syncService` 的第三个参数 `true` 表示"把这个 VIP 绑到一张叫 `kube-ipvs0` 的 dummy(假)网卡上"。为什么要绑?**因为 IPVS 需要内核"认为"这个 IP 是本地的,才会把包交给 IPVS 处理**——dummy 网卡是个只占坑不发包的虚拟网卡,正好用来挂所有 Service 的 VIP。你 `ip addr` 一台 ipvs 模式的节点,会看到 `kube-ipvs0` 上挂了一堆 ClusterIP,这就是它的"挂 VIP 的地方"。

> **对比两种模式的核心差异**:iptables 模式生成"几千条规则字符串,刷给 netfilter";ipvs 模式生成"几百条结构化的 Virtual Server / Real Server 记录,通过 netlink 塞给 IPVS 内核模块"。前者是表,后者是哈希;前者是字符串匹配,后者是结构化查询。**这就是 ipvs 在大规模下快的源码根源**。

### 整个 kube-proxy 的启动:watch 谁、跑哪个循环

最后,看一眼 kube-proxy 整个进程是怎么起来的——它 watch 什么、那个 `syncProxyRules` 循环谁在驱动。入口在 [`cmd/kube-proxy/app/server.go`](https://github.com/kubernetes/kubernetes/blob/master/cmd/kube-proxy/app/server.go),核心方法 `Run`。关键几行:

```go
// (摘自 cmd/kube-proxy/app/server.go 的 ProxyServer.Run,简化展示)

// 建 informer,watch Service(过滤掉 headless 和自定义 proxy 的)
serviceConfig := config.NewServiceConfig(ctx, serviceInformerFactory.Core().V1().Services(), ...)
serviceConfig.RegisterEventHandler(s.Proxier)   // 把 proxier 注册成 handler
go serviceConfig.Run(ctx.Done())

// 建 informer,watch EndpointSlice(过滤掉 headless 的)
endpointSliceConfig := config.NewEndpointSliceConfig(ctx, endpointSliceInformerFactory.Discovery().V1().EndpointSlices(), ...)
endpointSliceConfig.RegisterEventHandler(s.Proxier)
go endpointSliceConfig.Run(ctx.Done())

// ...
go s.Proxier.SyncLoop()   // ← 启动那个 syncProxyRules 循环
```

读这段,你会看到几个关键事实(和我们前面讲的一一对应):

1. **kube-proxy watch 的是 Service 和 EndpointSlice**(不是老的 Endpoints)。`serviceConfig` 和 `endpointSliceConfig` 就是两个 informer(回扣第 15 章),它们替 kube-proxy 把"集群里 Service 和后端 Pod 的变化"源源不断地推过来。
2. **headless Service 被过滤掉了**——`serviceInformerFactory` 那一行有个 `FieldSelector`:`spec.clusterIP != None`,意思是"不 watch 那些 `clusterIP: None` 的(headless)Service"。为什么?因为 headless Service 不归 kube-proxy 管(它没有 ClusterIP,kube-proxy 无规则可配),它的服务发现走 DNS。**这个过滤,是 headless Service "kube-proxy 不管、CoreDNS 管" 在源码层面的证据**。
3. **`s.Proxier` 是个接口**(`proxy.Provider`),它的具体实现是 iptables Proxier、ipvs Proxier 或 nftables Proxier 之一——由 kube-proxy 启动参数 `--proxy-mode` 决定。三种实现都实现了同一套 `OnServiceAdd/Update/Delete` 和 `OnEndpointSliceAdd/Update/Delete` 回调,所以上层 `Run` 不用关心是哪种模式。**这是面向接口编程的典型——切换代理模式,只换实现,不改骨架**。
4. **`s.Proxier.SyncLoop()` 是那个循环的入口**。它内部用一个叫 `BoundedFrequencyRunner` 的工具(限制最大频率的运行器),在"有事件来"或"定期 resync"时,调一次 `syncProxyRules`。这就是 reconcile 循环的驱动器——不是裸 `for { syncProxyRules(); sleep() }`,而是"事件触发 + 限频 + 定期兜底"的复合驱动,既保证响应及时,又防止规则风暴(短时间内 Service 变化太多,不会把内核刷爆)。

> 把这一节的源码串起来,整个 kube-proxy 的工作流就清楚了:
> ```
>   apiserver 上 Service / EndpointSlice 变了
>              │
>              ▼
>   kube-proxy 的 informer (serviceConfig / endpointSliceConfig) 收到事件
>              │
>              ▼
>   Proxier 的 OnServiceUpdate / OnEndpointSliceUpdate 被回调
>      → 把变化记到 serviceChanges / endpointsChanges(暂存,不立即刷)
>              │
>              ▼
>   SyncLoop 的 BoundedFrequencyRunner 触发一次 syncProxyRules
>              │
>              ▼
>   syncProxyRules:
>     ① 应用暂存的变化,更新 svcPortMap / endpointsMap(本地"应有"状态)
>     ② 遍历所有 Service,生成 KUBE-* 规则(iptables)或 VS/RS 记录(ipvs)
>     ③ 一次 RestoreAll / netlink 批量刷给内核
>              │
>              ▼
>   内核 netfilter / IPVS 用新规则,处理之后进来的包
> ```
> 这条流,从头到尾就是第 15 章那个"事件 → informer → reconcile → 写回"骨架的又一次复刻——只不过这次 reconcile 的对象,是**节点内核里的网络规则**。

---

## 章末小结

### 用航运比喻回顾本章:统一运单号与转运指南

回到港口。这一章我们做了一件事:**解决"集装箱(Pod)会换船、运单(IP)会变,寄件人怎么稳定找到它"这个问题**。

答案是三层,一层比一层接近落地:

1. **统一运单号(Service / ClusterIP)**。每批会换船的货,挂一个稳定不变的运单号。寄件人只认运单号,不管货现在在哪艘船。运单号背后"现在挂哪些箱"的清单,由一个**督办(Endpoints/EndpointSlice controller)用 reconcile 范式持续维护**——箱(Pod)变了,清单自动更新。**这是第 14 章那套范式,在"服务发现"这一侧的化身**。

2. **每艘船上的转运指南(iptables/ipvs 规则)**。运单号要起作用,得有人在每艘船(节点)上,把"运单号 → 现在哪些箱"的对应关系,写进本地的转运指南。这个写指南的文员,就是 **kube-proxy**——它**只写指南、不搬货**(不参与数据转发),真正搬货的是船上的叉车(内核 netfilter/IPVS)。指南里的核心规则是三层:入口登记台(KUBE-SERVICES,按运单号分流)→ 分发表(KUBE-SVC-*,用概率做负载均衡)→ 改地址台(KUBE-SEP-*,DNAT 把运单号对应的虚拟地址改成真实的箱号)。

3. **概率分发的巧妙数学**。分发表里那串 `1/n, 1/(n-1), ..., 1/2` 的概率,组合出每个后端箱恰好等概率的分流——这是用无状态的 iptables 模拟有状态的轮询的精妙办法。在 Service 数量上万时,这套线性规则匹配(O(n))会变慢,于是有了 ipvs 模式——把规则换成内核里的哈希表(O(1)),并把 IPVS 二十多年成熟的调度算法(rr/wrr/lc/sh…)直接拿来用。

把这三层合起来,**"Pod IP 会变、客户端找不到"这个第 14 章留下的"服务发现不够"问题,在 k8s 里被彻底解决了**——而且解法依然是 reconcile 范式(Endpoints controller 维护后端列表、kube-proxy watch 后再 reconcile 内核规则),没有引入任何新范式。

### 本章在全书主线中的位置:调度编排侧的"服务发现"闭环

记住全书的二分法:**打包隔离 vs 调度编排**。

- 本章属于**调度编排**这一侧。它是第 6 篇(k8s 核心抽象)的最后一章,补上了 k8s 半本最后一块拼图:**服务发现**。
- 第 14 章立 k8s 第一性原理时,把"单机 docker 的四个不够"列了出来:故障自愈、扩缩容、跨机调度、**服务发现**。前三件分别由第 17 章(reconcile)、第 18 章(调度器)、第 19 章(kubelet)解决;**第四件——服务发现——就是本章用 Service + kube-proxy 解决的**。至此,第 14 章留下的四个不够,被一一化解。k8s 半本的核心抽象(Pod / 声明式 API / 调度器 / kubelet / Service)全部讲完。

回扣两条本书反复强调的主线:

- **"组合而非发明"**:Service + kube-proxy 没有发明任何新机制。ClusterIP 是个虚拟标记、负载均衡用的是第 12 章早就讲过的 iptables DNAT 加 `statistic` 概率模块、ipvs 用的是内核里 2000 年就在的四层负载均衡器、服务发现用的是 DNS(互联网 80 年代就有)。**k8s 的高明,在于把这些早就摆好的积木,在每一台节点上自动化、批量、原子地配起来**——这是规模化编排的工程力量,不是底层原理的发明。
- **"一切都是 reconcile"**:本章再一次证明,第 14 章那套"声明期望 + 控制器自动调谐"的范式,贯穿 k8s 的每一个角落。Service 的后端列表是 Endpoints controller reconcile 出来的、节点上的网络规则是 kube-proxy reconcile 出来的——**同一个范式,管不同的对象**。这正是 k8s 能在几万个容器规模上保持简洁的根本原因。

### 五个"为什么"清单

如果你只能记五件事,记这五件:

1. **为什么不能直接写 Pod IP**:Pod 是会死会生的,IP 一刻不停在变(重启、滚动更新、扩缩容、节点故障都会让 IP 变)。直连 Pod IP,改一次连接字符串得改几万处,物理不可能。所以需要一个稳定的"门牌"(Service 的 ClusterIP)盖在一组会变的 Pod IP 之上。
2. **Service 是什么**:一个稳定的虚拟 IP(ClusterIP)+ 一个虚拟端口,背后挂着一组"会变的 Pod IP"。ClusterIP 不在任何真实网卡上(它 ping 不通),它只是个触发 iptables 规则的标记。Service 只存"期望"(selector),后端列表由 Endpoints/EndpointSlice controller 用 reconcile 维护。
3. **kube-proxy 干什么(最大的误解澄清)**:它**不是转发流量的代理服务器**,它是"写转运指南的文员"——只配内核的 iptables/ipvs 规则,不碰数据包。真正的转发由内核(netfilter/IPVS)在内核态完成,性能极高。kube-proxy 自己也是 reconcile:watch Service/EndpointSlice → 算规则差异 → 批量刷给内核。
4. **iptables 模式怎么做负载均衡**:三层 KUBE-* 链(KUBE-SERVICES 匹配 ClusterIP、KUBE-SVC-* 用 `statistic --mode random --probability` 做概率分发、KUBE-SEP-* 做 DNAT 到 Pod IP)。概率的数学是 `1/n, 1/(n-1), ..., 1/2` + 兜底,组合出每个后端恰好等概率(1/n)的分流。回扣第 12 章:这里用的 DNAT,就是那章讲的同一个机制,只是被自动化、批量地配。
5. **ipvs 模式为什么在大规模下快**:iptables 是线性规则表,匹配 O(n),Service 上万条时每个包都要遍历几百上千条规则,慢。ipvs 用内核里的哈希表,查虚拟服务 O(1),还自带一堆成熟调度算法(rr/wrr/lc/sh…)。**这是数据结构的胜利:线性表换哈希表**。诚实补充:今天(2026)ipvs 已在 k8s 1.35 被 deprecated,nftables 模式(1.33 起 stable 且默认)是继任者,但 iptables 模式的原理仍是理解一切的入口。

### 想继续深入,该往哪钻

- **亲手看 kube-proxy 写的规则**:在集群里起一个 Service,然后到任意一个节点上 `iptables-save | grep KUBE` 或 `iptables -t nat -L KUBE-SERVICES -n -v`——你会亲眼看到 KUBE-SERVICES、KUBE-SVC-*、KUBE-SEP-* 这几条链,以及那串 `statistic --mode random --probability` 规则。ipvs 模式则用 `ipvsadm -Ln` 看虚拟服务和后端。这是验证本章最直接的办法。
- **看 iptables 模式的源码**:打开 [`pkg/proxy/iptables/proxier.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/proxy/iptables/proxier.go),先看 `Proxier` struct 的那几个 buffer 字段,再看 `syncProxyRules`(几百行的大函数)和 `writeServiceToEndpointRules`(那串概率规则的生成地),最后看末尾的 `RestoreAll`(批量刷)。这是本章源码精读的延伸。
- **看 ipvs 模式的源码**:打开 [`pkg/proxy/ipvs/proxier.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/proxy/ipvs/proxier.go),重点看 `syncService` 和 `syncEndpoint`——它们调 `utilipvs` 库,最终走 netlink 把 Virtual Server / Real Server 写进内核的 IPVS 表。
- **看 Endpoints controller 的源码**:打开 [`pkg/controller/endpoint/endpoints_controller.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/controller/endpoint/endpoints_controller.go),找 `syncService`——按 selector 列 Pod、组 EndpointSubset、Create/Update Endpoints。这是"门牌背后的后端列表是怎么 reconcile 出来的"的真相。EndpointSlice 版本在 [`pkg/controller/endpointslice/`](https://github.com/kubernetes/kubernetes/tree/master/pkg/controller/endpointslice)。
- **看 IPVS 内核实现**:打开内核源码 [`net/netfilter/ipvs/`](https://github.com/torvalds/linux/tree/master/net/netfilter/ipvs),看 IPVS 怎么挂在 netfilter hook 上、怎么用哈希表查虚拟服务。这是 ipvs 模式"O(1) 查找"的根源。
- **想看官方对 iptables/ipvs/nftables 的最新表态**:读 [Virtual IPs and Service Proxies](https://kubernetes.io/docs/reference/networking/virtual-ips/) 这篇官方文档(里面有 IPVS 在 1.35 deprecated、nftables 在 1.33 stable 的明确表态),以及 2018 年那篇经典的 [IPVS 深度博客](https://kubernetes.io/blog/2018/07/09/ipvs-based-in-cluster-load-balancing-deep-dive/)。

---

> 第 6 篇讲完了:k8s 的核心抽象——Pod(为什么不是裸容器)、声明式 API + reconcile(k8s 的灵魂)、调度器(Pod 放哪)、kubelet(把 Pod 真跑起来)、Service + kube-proxy(服务发现)——五块拼图凑齐,k8s 怎么"声明期望状态、自动调谐到期望状态"的全貌已经完整。但还有一个我们一直绕着走的问题:**容器一旦死了,它写的文件就没了**。第 12 章讲 overlayfs 时说过,容器的 rootfs 是临时的——而数据库这种应用,数据必须持久化。k8s 怎么给容器接持久化存储?——下一篇,我们进入第 7 篇(进阶与边界),从 **第 21 章 · 存储:Volume / PV / PVC** 开始,看 k8s 怎么把"用存储的人"和"给存储的人"解耦。翻开它。
