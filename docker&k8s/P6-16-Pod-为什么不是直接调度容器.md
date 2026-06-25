# 第 16 章 · Pod:为什么不是直接调度容器

> **前置**:你需要先读完[第 15 章《k8s 架构:控制平面与节点》](P5-15-k8s架构-控制平面与节点.md)——它已经把"全球航运调度中心"的内部组织拆清楚了:**一个真相源(etcd)+ 一个唯一入口(apiserver)+ 一堆 watch 的控制器(scheduler / controller-manager / kubelet / kube-proxy)+ 指挥与干活分开**。但那一章里,我们反复用到一个词却始终没解释——**"调度一个 Pod"**。我们一直说"reconcile 的最小单位是 Pod",从没说"调度一个容器"。可是 k8s 明明是管容器的啊!**它的最小调度单位,为什么不是容器、而是 Pod 这个新概念?** 这一章,我们就把这个看似多余、实则精妙的抽象拆开。

> **核心问题**:k8s 明明是管容器的,为什么它的最小调度单位不是容器、而是 Pod?
>
> 第 15 章我们讲清了 k8s 的"部门设置",但有一个最基础的单位一直被我们含糊带过:每次说"调度""reconcile""补一个副本",主语都是 **Pod**,不是容器。如果你用过 docker,你的直觉一定是"我起一个容器,k8s 帮我把几万个容器调度到几百艘船上"——可 k8s 偏偏告诉你:**不,我调度的最小单位不是容器,是 Pod。Pod 里可以装好几个容器。** 这一层抽象看起来多此一举。这一章我们要把这个"多此一举"问到底:**它解决了什么不解决就不行的问题?如果直接调度容器会怎样?Pod 这个壳子底下到底是怎么把几个容器"绑成一组"的?那个神秘的 pause 容器又是干什么的?**
>
> **读完本章你会明白**:
> - 为什么"几个必须紧耦合的容器"(主进程 + sidecar,比如 web 服务器 + 日志收集)如果**分开调度**,会撞出三种灾难(调度到不同节点、生命周期不同步、没法共享 localhost)——**Pod 这层抽象,是为"紧耦合进程组"量身造的原子调度单位**。
> - Pod 里的容器**共享什么**(network namespace、IPC、共享 volume),**不共享什么**(mount namespace / 文件系统、PID 默认不共享、cgroup 各自)——这套"部分共享"的清单,是"既有隔离、又能协作"的精妙平衡。
> - 为什么 Pod 比"裸容器"更适合编排——它是**原子的**:要么整个 Pod 一起调度、一起死、一起重启,不会出现"半个 Pod"这种尴尬中间态。
> - **pause 容器为什么存在**:它先于业务容器启动,占住 Pod 的 network namespace(和 IPC),业务容器加入同一个 net ns;pause 永不退出,保证 Pod 的网络身份不随业务容器的生死而消失——**这是 Pod 共享网络的实现基石**。

> **如果一读觉得太难**:Pod 涉及的细节有点多(sandbox、pause、共享 namespace、CRI),很容易把人淹没。先只记住三件事——
> ① **Pod 是一组必须绑在一起调度的容器**:它们要么全在同一艘船(节点)上、要么全不在,生命周期绑死。
> ② **Pod 里的容器共享 network namespace**:所以它们能用 `localhost` 互相访问,看起来就像同一台机器上的几个进程。这块"共享"的根基,是一个叫 pause 的小容器在占着 net ns。
> ③ **Pod 是"逻辑主机",容器是"逻辑主机上的进程"**:呼应第 1 章"容器就是个普通进程"——在 Pod 里,几个容器共享一台虚拟的"主机",每个容器是这台主机上的一个进程组。
> 这三句话钉死,本章你就抓住了 80%。

---

## 章首·一句话点破

如果你对 Pod 的印象是"一个 Pod = 一个容器"(很多人刚接触 k8s 时都这么以为,因为 `kubectl get pods` 看到的每一行确实常常只跑着一个容器),请把这种印象先放一边。这是理解 Pod 时最容易绊倒、也最致命的一个误解——它会让你觉得 Pod 这层抽象纯属多余,从而错过 k8s 设计里最深刻的一笔。

这一章要做的第一件事,是把这句话连根拔起:

> **Pod 不是"一个容器换了个名字"。它是一组"必须绑在一起"的容器,被当成一个整体来调度、来生死、来重启。Pod 这层抽象,是为"紧耦合进程组"量身造的最小调度单位。**

我们一块一块拆。先从一个最朴素的问题开始:直接调度容器,会怎样?

---

## 一、为什么不直接调度容器:紧耦合容器的三种灾难

要理解 Pod 为什么存在,得先看清"如果 k8s 直接调度容器"是个什么处境。

> **比喻**:回到航运物流。假设你是一个调度员,你的最小调度单位是**单个集装箱**。每一票货(每一个容器),你都单独决定"它上哪艘船"。这对大多数货没问题——每个集装箱是独立的,装哪艘船都行。
>
> 但有一类货特别头疼:**联运拼箱**。比如"一台精密仪器 + 它专用的防震底座 + 电池组"这三件货,是一个整体——它们**必须同船、同舱、同时间装卸**,分开就全废(仪器没底座没法固定、电池组和仪器分开了仪器没法供电)。如果你还按"单个集装箱"来调度,会怎样?

### 灾难一:调度到不同的节点(货被拆散到不同的船)

最直接的灾难。假设你有一个 web 应用,它由两个紧耦合的容器组成:

- **主容器**:跑着 nginx,提供 HTTP 服务;
- **sidecar 容器**:一个日志收集 agent(fluentd),它读取 nginx 写到本地日志文件的内容,转发到中心日志系统。

这两个容器是**紧耦合**的:sidecar 必须能读到主容器写的日志文件。如果 k8s 直接调度容器、把每个容器当独立单位:

- 调度器决定"nginx 容器上 node-1"——OK。
- 调度器决定"fluentd 容器上 node-3"——**灾难**。fluentd 在 node-3 上,根本碰不到 node-1 上 nginx 写的日志文件。两个容器各跑各的,sidecar 形同虚设。

> **比喻**:这就好比调度员把"仪器上 1 号船、防震底座上 3 号船、电池组上 5 号船"。三件货被拆散到三艘不同的船上,**整体功能直接报废**——仪器没底座没法固定,一开船就摔了。

你可能会说:"那调度器聪明点,把紧耦合的容器都调到同一个节点不就行了?"可问题是,**调度器怎么知道哪两个容器是紧耦合的?** 如果你给每个容器单独打调度标签、单独写亲和性规则(nginx 和 fluentd 必须同节点),那等于你在调度器里**手写**了一层"这俩绑在一起"的逻辑——这恰恰是 Pod 这层抽象本该干的事。与其在每个容器上都写一遍"我和谁绑",不如**把绑在一起的容器整体当成一个单位**,从一开始就不分开。

### 灾难二:生命周期不同步(货装卸时间对不上)

第二个灾难更隐蔽。继续 web + sidecar 的例子。假设你侥幸把它们调到了同一个节点,但它们是**两个独立的调度单位**,各自有自己的生命周期:

- nginx 先启动了,开始疯狂写日志;
- fluentd 因为镜像拉取慢,晚了 30 秒才起来。

这 30 秒里,nginx 写的日志**没人收集**——直接丢进黑洞(sidecar 还没起来读)。反过来,如果 fluentd 先起来,nginx 还没起,fluentd 干等着读一个不存在的文件。再极端一点:nginx 崩了重启,fluentd 不知道,还在读旧的文件句柄;或者 fluentd 崩了,nginx 继续写,日志堆满磁盘……

**问题的根源**:这两个容器的生命周期是**绑死的**(必须同生、同死、同重启),但作为独立调度单位,它们各自有自己的状态机,根本没有"绑死"这个概念。

> **比喻**:这就像联运拼箱里,仪器的装卸时间和底座的装卸时间对不上——底座还没固定好,仪器已经被吊上去了,摔了;或者仪器卸走了,底座还孤零零留在舱里,占着位置等下一个永远不来的仪器。

### 灾难三:没法共享 localhost(货之间没法直接接触)

第三个灾难最致命。web 和 sidecar 经常需要**通过本地网络通信**(sidecar 不仅是读日志,还可能是 service mesh 的代理,把 nginx 的进出流量都劫持一层)。两个容器要在本地互相访问,最自然的方式是:**它们共享同一个 network namespace**——这样它们的 `localhost` 是同一个,nginx 监听 `127.0.0.1:8080`,sidecar 直接连 `127.0.0.1:8080` 就能到。

但如果它们是**两个独立调度的容器**,各自有自己的 network namespace(各自有独立的 IP、独立的协议栈),那它们之间通信就得走"容器网络"(veth / bridge / 跨节点 overlay),得知道对方的 IP,得处理 IP 变化——**完全失去了"本地通信"的便利**。

> **比喻**:这就像联运拼箱里,仪器和电池组被装进了两个**完全密封、彼此不通**的集装箱——哪怕它们在同一艘船上,你也得通过船上的转运系统才能让它们"对话",而没法直接靠在一起接上线。

### 这三种灾难合起来,逼出了一个需求

把上面三种灾难合起来,你会发现:有一类容器(web + sidecar、app + 配置 agent、main + init),它们天然是**一个整体**——必须同节点、同生命周期、能本地通信。如果调度器把它们当独立单位,就会在这三件事上同时翻车。

> **于是问题变成了**:能不能把"几个绑在一起的容器"**整体当成一个调度单位**?——要么整体调度到同一个节点、要么整体生死、要么整体重启,不会出现"半个整体"这种尴尬中间态?

答案,就是 Pod。

---

## 二、Pod:为"紧耦合进程组"量身造的原子调度单位

Pod 是 k8s 的**最小调度单位**。用一句话讲它的本质:

> **Pod 是一组绑在一起的容器,被当成一个整体来调度、来生死、来重启。Pod 里的容器,必然在同一艘船(节点)上,共享同一个 network namespace,能通过 localhost 互相访问。**

> **比喻**:Pod 就是航运里的**联运拼箱**——把"必须同船同舱、同时间装卸、能彼此接触"的几件货,打包进一个标准的拼箱单元。调度员不再单独调度每一件货,而是调度**整个拼箱**:拼箱上哪艘船,箱里的所有货就一起上哪艘船;拼箱装卸,箱里的货一起装卸;拼箱里任何一件货坏了,整个拼箱被标记为有问题(甚至整体重启)。**拼箱是调度的原子单位——要么整个一起,要么整个不要,不存在"半个拼箱"。**

### Pod 为什么是"原子"的

"原子"这个词在这里不是修辞,是字面意思:**Pod 是不可分的**。k8s 的一切调度、reconcile、扩缩容,**最小的操作单位都是 Pod**,不是 Pod 里的某个容器。

- **调度**(scheduler)决定的是"这个 Pod 上哪艘船",不是"这个容器上哪艘船"。一个 Pod 里的所有容器,**必然落在同一个节点上**(这是 Pod 的硬性保证)。
- **扩缩容**(Deployment 改 replicas)加减的是 Pod 的数量,不是容器的数量。你说"3 个副本",k8s 起的是 3 个 Pod,每个 Pod 里可能有好几个容器——**副本数 = Pod 数,不是容器数**。
- **重启**(容器挂了)以 Pod 为粒度:虽然单个容器挂了,k8s 会优先单独重启这个容器;但 Pod 作为一个整体,它的网络身份(IP)是稳定的(只要 Pod 还在),不会因为某个容器重启就变。

这种"原子性",正是上一节三种灾难的解药:

- **灾难一(调度到不同节点)**:Pod 强制所有容器在同一节点,永远不会拆散。
- **灾难二(生命周期不同步)**:Pod 里的容器有**严格的启动顺序**(下面 pause 容器会讲),sidecar 和主容器的协作有保障;Pod 整体被视为一个生命周期单位。
- **灾难三(没法共享 localhost)**:Pod 里的容器**共享 network namespace**,所以它们能直接用 `localhost` 通信——这是 Pod 最重要的属性,我们下一节专门讲。

### Pod 里有几个容器

讲到这里,你可能会问:那一个 Pod 到底装几个容器?答案是:**通常一个,但可以多个**。

- **最常见的情况**:一个 Pod 里就一个容器(这是绝大多数业务的样子,也是为什么很多人误以为"Pod = 容器")。这种情况下,Pod 这层抽象看起来确实是多余的——但它是**为了统一**而存在的:k8s 不管你一个 Pod 装几个容器,调度单位永远是 Pod。这样调度器、reconcile、扩缩容的逻辑都只需要针对 Pod 这一种单位写,不用区分"单容器 Pod"和"多容器 Pod"。
- **sidecar 模式**:一个主容器 + 一个或多个 sidecar(日志收集、监控 agent、service mesh proxy……)。这是 Pod 真正发挥价值的地方。
- **ambassador / adapter 模式**:主容器 + 一个代理/适配容器。

> **一个非常重要的认知**:不要因为"一个 Pod 通常只跑一个容器",就觉得 Pod 是多余的。Pod 的价值不在于"它能不能装多个容器",而在于**它给调度器、reconcile、扩缩容提供了一个统一的、原子的单位**。哪怕你每个 Pod 只装一个容器,这个抽象依然是 k8s 一切工作的根基——**它把"调度什么"这个问题,从"容器"提升到了"逻辑主机"的层次**。

### 那些不该用 Pod 多容器的场景(诚实交代边界)

说到这里要诚实一点:**不是所有"几个容器一起跑"的场景,都该塞进一个 Pod**。一个常见的判断标准是:**这几个容器是不是"必须共享网络和生命周期"?**

- **该塞进一个 Pod**:web + sidecar(共享网络、日志)、app + proxy(共享网络)、init 容器 + 主容器(必须先跑完再跑主)。它们**生命周期绑死、必须本地通信**。
- **不该塞进一个 Pod**:web 前端 + 数据库后端。虽然它们"一起跑",但**它们不共享网络**(数据库有自己的 IP、自己的访问控制),**生命周期也独立**(前端重启不该连累数据库)。这种应该用**两个 Pod + Service** 来组织——前端 Pod 通过 Service 找到后端 Pod。

> **判断口诀**:**"能不能一起死、一起重启"**。如果一起死没所谓(甚至该一起死),那是 Pod;如果一起死不行(数据库不能跟着前端一起重启),那不是 Pod。这个判断在 sidecar 设计里反复出现,记住了能省掉很多坑。

---

## 三、Pod 的"共享与隔离"清单:精心画的一条线

上一节我们说 Pod 里的容器"共享 network namespace"。但共享到什么程度、隔离到什么程度,这是一条**精心画的线**。这一节,我们把这条线完整地列出来。

### 共享什么

Pod 里的容器,**默认共享**这几样:

1. **Network namespace(网络命名空间)**:这是最重要的。Pod 里的所有容器**共享同一个 network namespace**——同一个 IP、同一个协议栈、同一组端口空间。后果是:
   - 它们能用 `localhost` 互相访问(nginx 监听 `127.0.0.1:8080`,sidecar 连 `127.0.0.1:8080` 直达)。
   - **它们不能监听同一个端口**(端口冲突)——因为端口空间是共享的。两个容器都监听 80,后起的会失败。
   - 对外,**整个 Pod 只有一个 IP**(不是每个容器一个 IP)。别的 Pod 访问这个 Pod,访问的是这一个 IP,具体到哪个容器,靠端口区分。

2. **IPC namespace(进程间通信命名空间)**:Pod 里的容器共享同一套 System V IPC、POSIX 消息队列。这意味着它们能用 IPC 机制(共享内存、信号量)通信——虽然实际用得少,但能力是有的。

3. **UTS namespace(主机名命名空间)**:Pod 里的容器共享同一个 hostname(默认是 Pod 名)。所以它们 `hostname` 命令看到的都一样。

4. **共享 volume(存储卷)**:Pod 可以挂载共享 volume,Pod 里的所有容器都能读写这个 volume。这是 sidecar 读主容器日志的常见方式——主容器把日志写到共享 volume,sidecar 从同一个 volume 读。

### 不共享什么

Pod 里的容器,**默认不共享**这几样:

1. **Mount namespace(挂载点 / 文件系统)**:每个容器有**自己独立的文件系统**(rootfs)。容器 A 装的 nginx,容器 B 看不到;容器 B 写的文件,容器 A 看不到(除非通过共享 volume)。**这是容器隔离的基本盘,Pod 不破坏它**。

2. **PID namespace(进程号命名空间,默认)**:每个容器默认**看不到别的容器里的进程**。容器 A 里 `ps aux`,只看到自己这个容器里的进程,看不到容器 B 里的。**(注:可以通过 Pod spec 的 `shareProcessNamespace: true` 打开共享,但默认是关的——这是一个有意为之的默认值,下面源码会讲。)**

3. **cgroup(资源配额)**:每个容器有**自己的 cgroup**,自己的 CPU / 内存限额。容器 A 限 1 CPU、容器 B 限 2 CPU,各自独立。**Pod 层面也有一个 cgroup(给整个 Pod 设总量上限),但容器之间是独立的**。

### 为什么这条线要这么画

把这份清单摆在一起,你会发现一个清晰的设计意图:

> **Pod 里的容器,共享"通信和协作"需要的东西(网络、IPC、主机名、可选的共享存储),隔离"各自独立性"需要的东西(文件系统、进程、资源配额)。**

这条线的本质是:**让几个容器能紧密协作(共享网络是协作的根基),同时保持各自的独立性(文件系统、资源不互相污染)**。这是一种"既有隔离、又能协作"的精妙平衡——既不像"每个容器完全独立"那样没法协作,也不像"几个进程塞进同一个容器"那样失去隔离。

> **呼应第 1 章**:还记得第 1 章我们立的那个第一性原理吗?**"容器就是个普通进程"**。Pod 这个抽象,恰好把这个原理推到了一个新的层次:**Pod 是一台"逻辑主机",Pod 里的每个容器,是这台逻辑主机上的一个进程组**。它们共享同一台主机的网络(就像同一台物理机上的几个进程共享同一个网卡)、能互相通信(localhost 就像同一台机器上的进程用 loopback 通信)、但各自有自己的文件系统和资源限额(就像同一台机器上的不同进程,文件系统隔离、各自有 cgroup)。**Pod = 一台虚拟的、共享网络的逻辑主机。** 这个认知,是理解 k8s 所有后续设计(Service 怎么找 Pod、kubelet 怎么管 Pod)的钥匙。

那么,Pod 里的几个容器是怎么"共享同一个 network namespace"的?这就引出了本章最关键的一个角色——**pause 容器**。

---

## 四、pause 容器:Pod 共享网络的基石

这一节是本章的技术高潮。我们要回答一个看起来很神秘的问题:**Pod 里的几个容器,凭什么能共享同一个 network namespace?是谁先创建了这个 net ns,然后让别的容器加进来?**

答案是一个你可能听过、但不一定理解的小角色:**pause 容器**。

### 先有 net ns,还是先有业务容器

想清楚这个问题。假设一个 Pod 里有两个容器:nginx 和 fluentd。它们要共享 network namespace。那么这个 net ns 是谁建的?

- 如果是 nginx 建的(net ns 挂在 nginx 进程上),那 nginx 一旦崩了重启,这个 net ns 就消失了——fluentd 瞬间失去网络,整个 Pod 的网络身份(IP)也变了。
- 如果是 fluentd 建的,同理,fluentd 崩了 net ns 就没了。

**问题的本质**:net ns 必须挂在一个**永不退出的进程**上,否则它的生命周期会被业务容器的生死绑架。但业务容器注定会崩、会重启——你不能把 Pod 的网络根基,绑在一个注定会死的进程上。

> **比喻**:回到联运拼箱。拼箱里那几件货(业务容器),装卸、损坏、替换是家常便饭。但拼箱本身要有一个**稳定的"舱位编号"**(network namespace / Pod IP)——不管货怎么换,舱位编号不能变,否则别的船找不着这票货。这个稳定的舱位编号,得挂在一个**永不挪窝**的东西上。

### pause 容器:那个"永不挪窝的底板"

k8s 的解法极其巧妙:**在每个 Pod 里,先于所有业务容器,启动一个特殊的小容器,叫 pause 容器(也叫 infra 容器 / sandbox 容器)**。它干一件事:

> **pause 容器占住 Pod 的 network namespace(和 IPC namespace),自己永远不退出。所有业务容器,都加入 pause 容器占住的这个 net ns。**

这样:

- **net ns 的生命周期,和 pause 容器绑定**,不和任何业务容器绑定。
- 业务容器随便崩、随便重启——**只要 pause 还在,Pod 的 net ns 就还在,Pod 的 IP 就不变**。
- 业务容器重启后,它重新"加入"pause 占住的那个 net ns,继续用同一个 IP、同一个端口空间。
- **整个 Pod 的网络身份(IP),由 pause 容器承载,极其稳定**——这正是 Service 能盖在 Pod 之上的前提(第 20 章会讲,Service 靠 Pod IP 做负载均衡,Pod IP 越稳定越好)。

> **比喻**:pause 容器就是联运拼箱里那块**"占位的底板 / 托盘"**。装货(起业务容器)之前,先把底板铺好(起 pause),底板上钉好"舱位编号"(占住 net ns)。然后所有货都码在这块底板上(业务容器加入同一个 net ns)。**货可以装卸、可以替换,但底板一直在,舱位编号一直不变**——这就是这票货稳定的"船舱位置 / 网络身份"。底板本身什么货都不装(它的镜像只有几百 KB,里面就一个死循环的 pause 程序),它存在的唯一目的,就是**占着位置**。

### pause 容器长什么样

pause 容器跑的程序极其简单。它的源码(在 `kubernetes-sigs/cri-tools` 或 `kubernetes/kubernetes` 的 `build/pause/` 目录历史里能找到)本质上就是这么几行(以下为简化示意,展示核心逻辑):

```c
// (以下为简化示意,非源码原文,展示 pause 的核心)
int main() {
    // 1. 注册一堆信号 handler,把所有信号都忽略掉
    //    (SIGTERM、SIGINT 等统统忽略,保证 pause 不会被普通信号杀死)
    signal(SIGTERM, SIG_IGN);
    // ...
    // 2. 然后就是无限循环,挂起自己
    for (;;) {
        pause();   // 系统调用,让进程挂起,等信号
    }
}
```

读这段,注意两件事:

1. **它什么都不做**。不跑业务逻辑、不监听端口、不读写文件。它的全部使命就是**占着 net ns,然后挂起自己**(用 `pause()` 系统调用进入睡眠,几乎不耗 CPU)。所以 pause 容器极其轻量——镜像几百 KB,运行时几乎零开销。一个节点上跑几百个 Pod,就有几百个 pause 容器,但它们加起来的资源开销可以忽略不计。

2. **它故意"杀不死"**。pause 把常见的终止信号都忽略了,就是为了**不被误杀**——它要是轻易死了,Pod 的 net ns 就没了,所有业务容器瞬间失联网。只有 kubelet 主动销毁 Pod 时,才会用特殊方式(发 SIGKILL,这个没法忽略)杀掉 pause。

### pause 的启动顺序:为什么它必须先起

这里有一个 Pod 启动的严格顺序,理解它就理解了 Pod 的"生命周期绑死":

1. **kubelet 决定在这台节点上跑一个 Pod**(从 apiserver watch 到分配给自己的 Pod)。
2. **kubelet 先调用 CRI 的 `RunPodSandbox`,创建 Pod 的 sandbox**——这一步在节点上起一个 pause 容器,占住 net ns(和 IPC ns)。**此时 Pod 的网络身份(IP)就确定了**。
3. **然后,kubelet 依次启动业务容器**(nginx、fluentd……),每个业务容器启动时,都配置成"加入 pause 占住的那个 net ns"。
4. 业务容器们就这样共享着 pause 的 net ns,开始协作。

这个顺序保证了:**net ns 永远先于业务容器存在**。业务容器一启动就能用这个 net ns,不会出现"业务容器起了但 net ns 还没建好"的尴尬。反过来,Pod 销毁时,业务容器先停,pause 最后停——net ns 一直在,直到所有业务容器都安全退出。

> **这就是为什么 pause 容器叫"sandbox 容器"**:在 CRI(Container Runtime Interface)的术语里,Pod 的这层共享环境(主要是 net ns)叫 **Pod sandbox**。pause 容器就是 Pod sandbox 的载体。下面源码精读,我们就拆 kubelet 是怎么创建这个 sandbox 的。

### 一个常见的误解:pause 是 k8s 的"魔法"吗

不是。pause 容器没有任何魔法。它就是一个**普通的容器**——用的就是和业务容器一样的 namespace、cgroup 机制(第 2、3 章讲的那两块基石)。它特殊的地方只有两点:**① 它先起;② 它不退出**。除此之外,它和业务容器在内核眼里没有任何区别。

> **回扣第 1 章**:第 1 章我们说"容器就是个普通进程"。pause 容器是这句话的最佳注脚——**Pod 的整套"共享网络"魔法,底下就是一个普通的、永不退出的进程,占住了一个 network namespace 而已**。没有什么"Pod 专用内核机制",全是 namespace 的标准用法(一个进程建 net ns,别的进程用 `setns` 加入)。

那么,kubelet 到底是怎么在源码层面,把这个"先起 pause、再起业务容器、共享 net ns"的过程跑起来的?下面进入本章的源码精读。

---

## 关键源码精读:从一个 Pod 到几个共享网络的容器

理论讲完了,我们钻进 k8s 源码,亲眼看看"kubelet 把一个 Pod 变成几个共享网络的容器"这条链子长什么样。所有引用都来自 `kubernetes/kubernetes` 的 master 分支(在线核实),文件路径相对仓库根。

> **一个写源码章必须先说清的事实**:kubelet 不直接管容器。它通过 **CRI(Container Runtime Interface)** 调用底层的容器运行时(containerd 或 CRI-O),让运行时去真正创建 sandbox、起容器。所以这条链子是:**kubelet(决策)→ CRI gRPC → containerd(执行)→ runc(底层施工)**。我们这里只看 kubelet 这一侧——它怎么把"一个 Pod"翻译成"创建 sandbox + 起几个业务容器"的 CRI 调用。

### 一、SyncPod:整个流程的总入口

当一个 Pod 被分配到这台节点,kubelet 要"同步"它(把它真正跑起来)。这个同步的总入口,在 [`kubeGenericRuntimeManager.SyncPod`](https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/kuberuntime/kuberuntime_manager.go)(文件 `pkg/kubelet/kuberuntime/kuberuntime_manager.go`,函数在 1450 行附近,行号随 master 演进会漂移,以函数名为准):

```go
// (摘自 kuberuntime_manager.go,简化展示,省略错误处理和无关分支)
func (m *kubeGenericRuntimeManager) SyncPod(ctx context.Context, pod *v1.Pod,
    podStatus *kubecontainer.PodStatus, pullSecrets []v1.Secret,
    backOff *flowcontrol.Backoff, restartAllContainers bool) (result kubecontainer.PodSyncResult) {

    // 1. 算出"这个 Pod 现在该变成什么样"——哪些容器要起、哪些要停、哪些要重启
    podContainerChanges := m.computePodActions(ctx, pod, podStatus)

    // 2. 如果 sandbox 还没建,或者坏了,先(重新)创建 sandbox
    if podContainerChanges.CreateSandbox {
        podSandboxID, msg, err = m.createPodSandbox(ctx, pod, podContainerChanges.Attempt)
        // ...
    }

    // 3. 启动 / 重启业务容器(每个容器单独一条 CRI 调用)
    for _, c := range podContainerChanges.ContainersToStart {
        // ... 这里会调用 startContainer,把每个业务容器加进 sandbox 的 net ns
    }
    return
}
```

读这个函数,把它和我们前面讲的设计对应起来:

- **第 1 步 `computePodActions`**:`kubelet` 先算出"偏差"——这个 Pod 期望长什么样、实际长什么样、该怎么补。**这就是 reconcile 的味道**——kubelet 自己也是个 reconcile 循环(第 15 章点过、第 19 章细讲),它不是"按命令起容器",而是"不停把实际往期望拉"。
- **第 2 步 `createPodSandbox`**:**这就是"先起 pause"的那一步**。如果 sandbox 还没有(第一次起这个 Pod),或者 sandbox 坏了(被某种原因干掉了),kubelet 先创建它。这个 sandbox,就是 pause 容器占住的那层共享环境。
- **第 3 步起业务容器**:sandbox 建好之后,才依次起业务容器,每个业务容器都会被配置成"加入这个 sandbox 的 net ns"。

**注意这个顺序的硬性**:`createPodSandbox` 永远在起业务容器之前。这就从源码层面保证了"先有 net ns,再有业务容器"——业务容器永远不会在一个没有 net ns 的真空中启动。

### 二、createPodSandbox:真正发起"起 pause"的 CRI 调用

`SyncPod` 第 2 步调用的 `createPodSandbox`,定义在 [`pkg/kubelet/kuberuntime/kuberuntime_sandbox.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/kuberuntime/kuberuntime_sandbox.go)(函数 `createPodSandbox`,38 行附近):

```go
// (摘自 kuberuntime_sandbox.go,简化展示)
func (m *kubeGenericRuntimeManager) createPodSandbox(ctx context.Context, pod *v1.Pod, attempt uint32) (string, string, error) {
    // 1. 生成 sandbox 的配置(net ns 模式、IPC 模式、hostname、DNS、端口……)
    podSandboxConfig, err := m.generatePodSandboxConfig(ctx, pod, attempt)

    // 2. 调用 CRI 的 RunPodSandbox,让底层运行时真正创建 sandbox(起 pause)
    podSandBoxID, err := m.runtimeService.RunPodSandbox(ctx, podSandboxConfig, runtimeHandler)

    return podSandBoxID, "", nil
}
```

读这个函数,关键就是第 2 步那一行:`m.runtimeService.RunPodSandbox(...)`。这一行,是 kubelet 通过 CRI gRPC,告诉底层的 containerd:"**给这个 Pod 建一个 sandbox**"。containerd 收到这个调用后,会在节点上拉起一个 pause 容器(用配置好的 pause 镜像),让 pause 占住 net ns——这就是 sandbox 的实体。

> **一个必须澄清的细节(避免踩坑)**:很多老资料会说"kubelet 配置了 pause 镜像地址,默认是 `registry.k8s.io/pause`"。**在当前 master 分支的 kubernetes 仓库里,这个说法已经不成立了**——kubelet 根本不持有 pause 镜像的地址,`KubeletConfiguration` 结构里没有 `SandboxImage` 字段,老的 `--pod-infra-container-image` 命令行参数也已经删除。**pause 镜像的地址,是由 CRI 运行时(containerd / CRI-O)自己持有的**——比如 containerd 在它的 `config.toml` 里写 `sandbox_image = "registry.k8s.io/pause:3.10"`。kubelet 只是通过 `RunPodSandbox` 发了一个"建 sandbox"的指令,**用哪个镜像起 pause,是运行时的事,不是 kubelet 的事**。这反映了 k8s 的一个设计哲学:**kubelet 只决策,具体怎么干交给 CRI 运行时**(关注点分离)。

`RunPodSandbox` 这个 CRI 接口本身,定义在 [`staging/src/k8s.io/cri-api/pkg/apis/runtime/v1/api.proto`](https://github.com/kubernetes/kubernetes/blob/master/staging/src/k8s.io/cri-api/pkg/apis/runtime/v1/api.proto)(proto 文件,30 行附近):

```proto
// RunPodSandbox creates and starts a pod-level sandbox. Runtimes must ensure
// the sandbox is in the ready state on success.
rpc RunPodSandbox(RunPodSandboxRequest) returns (RunPodSandboxResponse) {}
```

注意 proto 注释里那句话:**"Runtimes must ensure the sandbox is in the ready state on success"**——运行时必须保证 sandbox 成功后处于"就绪"状态。什么叫就绪?就是 **net ns 已经建好、pause 已经在里面跑着、可以接受业务容器加入了**。kubelet 拿到 sandbox 就绪的信号,才会继续往下起业务容器。

### 三、业务容器怎么加入 sandbox 的 net ns

sandbox 建好(pause 占住了 net ns)之后,kubelet 开始起业务容器。每个业务容器,通过 CRI 的 `CreateContainer` 创建。这个接口的请求里,有一个关键字段——`PodSandboxId`,告诉运行时"**这个容器要加进哪个 sandbox 的 net ns**"。看生成代码 [`api.pb.go`](https://github.com/kubernetes/kubernetes/blob/master/staging/src/k8s.io/cri-api/pkg/apis/runtime/v1/api.pb.go) 里的 `CreateContainerRequest`(`CreateContainerRequest` 结构在 5843 行附近,字段 `PodSandboxId` 在 5846 行附近):

```go
// (摘自 api.pb.go,protobuf 生成代码)
type CreateContainerRequest struct {
    // ...
    // ID of the PodSandbox in which the container should be created.
    PodSandboxId string `protobuf:"bytes,1,opt,name=pod_sandbox_id,json=podSandboxId,proto3" json:"pod_sandbox_id,omitempty"`
    // Config of the container.
    Config *ContainerConfig `protobuf:"bytes,2,opt,name=config,proto3" json:"config,omitempty"`
    // ...
}
```

读注释那行:**"ID of the PodSandbox in which the container should be created"**——业务容器要在哪个 sandbox 里创建。这个 `PodSandboxId`,就是上一步 `RunPodSandbox` 返回的那个 sandbox id。运行时(containerd)拿到这个 id,就知道:**新创建的业务容器,要加入这个 sandbox(也就是 pause)占住的那个 net ns**——于是业务容器和 pause 共享了 net ns,进而 Pod 里所有业务容器都通过"加入同一个 sandbox"共享了同一个 net ns。

kubelet 侧发起这个调用的地方,在 [`pkg/kubelet/kuberuntime/kuberuntime_container.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/kuberuntime/kuberuntime_container.go) 的 `startContainer` 函数里(277 行附近):

```go
// (摘自 kuberuntime_container.go,简化展示)
containerID, err := m.runtimeService.CreateContainer(ctx, podSandboxID, containerConfig, podSandboxConfig)
```

这一行,`podSandboxID` 作为第一个参数传给 `CreateContainer`——**这就是业务容器"加入 sandbox net ns"的源码落点**。注意 kubelet 侧用的是小写的 Go 变量 `podSandboxID`,它会被 CRI 客户端序列化成 protobuf 的 `PodSandboxId` 字段(那个大写字段名只在生成代码里出现)。

### 四、NamespaceOption:共享到什么程度,是精确配置的

讲到这里,你可能会问:Pod 里的容器共享 net ns,那 PID ns 呢?IPC ns 呢?**共享到什么程度,是谁决定的?**

答案藏在 sandbox 配置的 `NamespaceOption` 里。kubelet 在创建 sandbox 时,会根据 Pod 的 spec,告诉运行时"这个 sandbox 的 net / IPC / PID / user ns 各是什么模式"。这个映射,在 [`pkg/kubelet/kuberuntime/util/util.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/kuberuntime/util/util.go) 里(注意路径是 `kuberuntime/util/util.go`,不是某些老资料说的 `kuberuntime_util.go`——那个文件不存在)。最关键的三个函数:

```go
// (摘自 kuberuntime/util/util.go)
func IpcNamespaceForPod(pod *v1.Pod) runtimeapi.NamespaceMode {
    // IPC:要么共享宿主的(NODE),要么整个 Pod 共享一份(POD)
    // ...
}

func NetworkNamespaceForPod(pod *v1.Pod) runtimeapi.NamespaceMode {
    // Network:同上,二选一
    // ...
}

func PidNamespaceForPod(pod *v1.Pod) runtimeapi.NamespaceMode {
    if pod != nil {
        if pod.Spec.HostPID {
            return runtimeapi.NamespaceMode_NODE    // 共享宿主 PID ns
        }
        if pod.Spec.ShareProcessNamespace != nil && *pod.Spec.ShareProcessNamespace {
            return runtimeapi.NamespaceMode_POD     // 整个 Pod 共享一份 PID ns
        }
    }
    // Note that PID does not default to the zero value for v1.Pod
    return runtimeapi.NamespaceMode_CONTAINER       // 默认:每个容器独立 PID ns
}
```

读这三个函数,你会发现一个**非显然、但极其重要**的不对称:

- **IPC 和 Network**:`NODE`(共享宿主)或 `POD`(整个 Pod 共享一份)二选一,**默认是 POD**——也就是 Pod 里的容器默认共享 IPC 和 net ns。这正是我们前面讲的"Pod 共享网络"的源码根基。
- **PID**:却是**三分**——`NODE`(共享宿主)、`POD`(整个 Pod 共享)、`CONTAINER`(每个容器独立),**默认是 CONTAINER**!也就是说,**Pod 里的容器默认不共享 PID namespace**——容器 A 默认看不到容器 B 里的进程。只有当 Pod spec 显式写 `shareProcessNamespace: true` 时,才会切到 POD 模式。

注意 `util.go` 里那行注释:**"Note that PID does not default to the zero value for v1.Pod"**(PID 故意不采用默认零值)。这行注释是 k8s 设计者刻意留下的——**PID 默认不共享,是一个有意为之的决定**,不是疏忽。为什么?因为共享 PID ns 意味着容器之间能看到彼此的进程、甚至能发信号杀彼此的进程,这破坏了容器之间的隔离,只在确实需要时(比如 sidecar 要管理主进程)才打开。**网络必须共享(协作需要),PID 默认不共享(隔离优先)**——这正是上一节那份"共享与隔离清单"在源码里的体现。

### 五、串起来:一次 Pod 启动的完整调用链

把上面几段合起来,一个 Pod 从"被调度到这台节点"到"几个共享网络的容器跑起来",完整的调用链是:

```
   apiserver 上一个 Pod 被绑定到 node-1(scheduler 决定的)
              │
              ▼
   kubelet 通过 informer watch 到"这个 Pod 分给我了"
              │
              ▼
   kubelet 的 podWorkerLoop(pod_workers.go)被触发
   → 调用 SyncPod(podSyncer 接口)
              │
              ▼
   kubeGenericRuntimeManager.SyncPod(kuberuntime_manager.go:1450)
   ① computePodActions:算偏差(该起哪些容器)
   ② 如果 sandbox 没建:createPodSandbox(kuberuntime_sandbox.go:38)
              │
              ▼
   createPodSandbox → CRI: RunPodSandbox(gRPC 调用)
              │
              ▼
   containerd 收到 RunPodSandbox:
   ① 拉 pause 镜像(如 registry.k8s.io/pause:3.10,地址在 containerd 配置里)
   ② 起 pause 容器,占住 net ns + IPC ns
   ③ 返回 sandbox id 给 kubelet
   ——此时 Pod 的网络身份(IP)已确定,pause 在挂着
              │
              ▼
   kubelet 回到 SyncPod 第 3 步,依次起业务容器:
   对每个业务容器:startContainer(kuberuntime_container.go)
   → CRI: CreateContainer(podSandboxID, config, ...)
              │
              ▼
   containerd 收到 CreateContainer:
   ① 创建业务容器,但 net ns 设成"加入 sandbox(pause)占住的那个"
   ② 业务容器和 pause 共享 net ns
   ——于是所有业务容器通过"加入同一个 sandbox"共享了 net ns
              │
              ▼
   Pod 就绪:pause 占着 net ns,业务容器们共享着这个 net ns 协作运行
   Pod 的 IP 稳定(由 pause 承载),不会因业务容器重启而变
```

**整条链子里,没有一个"Pod 专用内核机制"。** Pod 的全部魔法,就是:**① 用 CRI 起一个永不退出的 pause 容器占住 net ns;② 让业务容器加入这个 net ns**。底下用的全是 namespace 的标准能力(一个进程建 net ns,别的进程加入)——回扣第 1 章,容器没有任何神秘力量,Pod 也没有。

---

## 章末小结

### 用航运比喻回顾本章:联运拼箱与那块底板

回到港口。这一章我们做了一件事:**搞清楚 k8s 调度的最小单位——Pod——到底是什么,以及它为什么存在。**

答案分四层,一层比一层接近本质:

1. **直接调度容器,会在三种灾难上翻车**:紧耦合的容器(web + sidecar)被拆到不同节点、生命周期对不上、没法共享 localhost。**Pod 这层抽象,是为"紧耦合进程组"量身造的。**
2. **Pod = 联运拼箱**:把"必须同船同舱、同时间装卸、能彼此接触"的几件货(容器),打包成一个整体来调度。**它是原子的**——要么整个一起调度、一起生死、一起重启,不会出现"半个 Pod"。
3. **Pod 里的容器,精心地共享与隔离**:共享 network / IPC / UTS namespace(协作需要)、可选共享 volume;不共享 mount / 默认不共享 PID / 各自 cgroup(独立性需要)。**这条线让几个容器既能紧密协作,又保持各自的隔离**——既不像完全独立的容器那样没法协作,也不像塞进同一个容器那样失去隔离。**Pod 是一台"逻辑主机",容器是这台主机上的进程**——呼应第 1 章"容器就是个普通进程"。
4. **Pod 共享网络的基石是 pause 容器**:它先于业务容器启动,占住 net ns,永不退出;业务容器加入这个 net ns。**Pod 的网络身份(IP)由 pause 承载,极其稳定,不随业务容器的生死而消失**——这是 Service 能盖在 Pod 之上的前提。pause 就是联运拼箱里那块"占位的底板",先铺好,所有货都码在它上面,货可以装卸但底板一直在。

### 本章在全书主线中的位置:Pod 是"调度编排"侧的最小单位

记住全书的二分法:**打包隔离 vs 调度编排。**

- 前 15 章里,**打包隔离**这一侧的最小单位是**容器**(一个普通进程 + namespace + cgroup);到了 k8s 的**调度编排**这一侧,**最小单位提升到了 Pod**。这不是 k8s 多此一举,而是**编排"紧耦合进程组"这件事,天然需要一个比容器更大的单位**。Pod 是 docker 半本(容器)和 k8s 半本(调度)之间的**衔接抽象**——它把"几个容器"重新打包成"一个调度单位",让 k8s 的一切(scheduler、kubelet、reconcile、扩缩容)都能针对这个统一单位工作。

后面四章,都是在 Pod 这个根基上展开:

- **第 17 章 · 声明式 API 与 reconcile**:本章里 kubelet 那个"算偏差、补容器"的 SyncPod,是 reconcile 在节点上的化身。第 17 章会把 reconcile 这套范式在源码层面逐行钉死(钻进 informer 和 controller)。
- **第 18 章 · 调度器**:本章里"Pod 被绑定到 node-1"那一步,谁决定?怎么决定?第 18 章讲调度器的过滤 + 打分两阶段。
- **第 19 章 · kubelet**:本章里 SyncPod → createPodSandbox → 起业务容器这条链子,第 19 章会更完整地拆,包括 kubelet 的反向同步(把 Pod 真实状态报回 apiserver)。
- **第 20 章 · Service 与 kube-proxy**:本章里"Pod 的 IP 由 pause 承载,很稳定"——但 Pod 还是会死会生,IP 终究会变。第 20 章讲 Service 怎么盖在一组 Pod 之上,提供一个真正稳定的门牌。

### 五个"为什么"清单

如果你只能记五件事,记这五件:

1. **为什么不直接调度容器**:紧耦合的容器(web + sidecar)分开调度会撞三种灾难(拆到不同节点、生命周期不同步、没法共享 localhost)。**Pod 是为"紧耦合进程组"量身造的原子调度单位**——要么整个一起调度、一起生死、一起重启。
2. **Pod 里的容器共享什么、不共享什么**:共享 network / IPC / UTS namespace 和(可选的)共享 volume;不共享 mount / 默认不共享 PID / 各自 cgroup。这条线让容器既能紧密协作,又保持各自隔离。**Pod 是"逻辑主机",容器是这台主机上的进程。**
3. **为什么 Pod 比"裸容器"更适合编排**:因为它是原子的——调度、reconcile、扩缩容都针对 Pod 这个统一单位,不会出现"半个 Pod"的尴尬中间态。哪怕一个 Pod 只装一个容器,这个抽象依然是 k8s 一切工作的根基。
4. **pause 容器为什么存在**:它先于业务容器启动,占住 Pod 的 network namespace(和 IPC),永不退出;业务容器加入这个 net ns。**Pod 的网络身份(IP)由 pause 承载,不随业务容器生死而消失**。pause 就是联运拼箱里那块"占位的底板"。它没有任何魔法,就是一个永不退出的普通容器。
5. **底层没有 Pod 专用机制**:Pod 的全部魔法 = ① CRI 起 pause 占住 net ns;② 业务容器加入这个 net ns。底下用的全是 namespace 的标准能力(回扣第 1 章)。**kubelet 只决策,用哪个 pause 镜像、怎么起容器,都交给 CRI 运行时(containerd)——这是 k8s 的关注点分离。**

### 想继续深入,该往哪钻

- **亲手看一个 Pod 里的 pause 容器**:在跑着 k8s 的节点上(或者用 minikube / kind 起一个本地集群),`kubectl run nginx --image=nginx` 起一个 Pod,然后到节点上用 `crictl ps` 看看——你会看到除了 nginx 容器,还有一个 `pause` 容器,镜像通常是 `registry.k8s.io/pause:3.x`。这就是那块"底板"。再用 `crictl inspect <pause-id>` 看 pause 的网络配置,你会发现它的 net ns 就是整个 Pod 共享的那个。
- **亲手验证"Pod 里的容器共享 localhost"**:写一个两容器的 Pod(主容器 + sidecar),让主容器监听一个端口,sidecar 里 `curl 127.0.0.1:<port>`——能通,因为它们共享 net ns。再试 `shareProcessNamespace: true`,在两个容器里互相 `ps aux` 看看能不能看到对方的进程。
- **看 SyncPod 的源码**:打开 [`pkg/kubelet/kuberuntime/kuberuntime_manager.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/kuberuntime/kuberuntime_manager.go),找 `SyncPod` 函数——亲眼看看"先 createPodSandbox、再起业务容器"这个顺序是怎么在源码里钉死的。
- **看 createPodSandbox 和 RunPodSandbox 的调用**:打开 [`pkg/kubelet/kuberuntime/kuberuntime_sandbox.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/kuberuntime/kuberuntime_sandbox.go),看 `createPodSandbox` 怎么调 CRI 的 `RunPodSandbox`;再看 [`staging/src/k8s.io/cri-api/pkg/apis/runtime/v1/api.proto`](https://github.com/kubernetes/kubernetes/blob/master/staging/src/k8s.io/cri-api/pkg/apis/runtime/v1/api.proto) 里 `RunPodSandbox` 的接口定义——你会理解 kubelet 和 containerd 之间的契约。
- **看 namespace 共享模式怎么决定**:打开 [`pkg/kubelet/kuberuntime/util/util.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/kuberuntime/util/util.go),读 `IpcNamespaceForPod` / `NetworkNamespaceForPod` / `PidNamespaceForPod` 三个函数——注意 PID 默认是 CONTAINER(不是 POD)这个非对称,以及那行刻意留下的注释。

---

> Pod 这个抽象立住了:**它是 k8s 调度的最小单位,是几个紧耦合容器绑成的原子整体,共享 network namespace 的根基是那个永不退出的 pause 容器。** 但这里有一个一直被我们含糊带过的问题——**k8s 是怎么"维持"一个 Pod 的?** 你 `kubectl apply` 一个 Pod,k8s 不是"启动它就完事",而是**不停**地把"实际状态"往"期望状态"拉。这套"声明期望 + 自动调谐(reconcile)"的范式,第 14 章给了直觉、第 15 章看到它在各组件里的化身,但它的源码骨架——**informer 怎么感知世界、controller 怎么根据偏差做决策、为什么这套范式这么强大**——我们一直没逐行拆。下一章,我们就钻进 k8s 的灵魂。翻开 **第 17 章 · 声明式 API 与 reconcile:k8s 的灵魂**。
