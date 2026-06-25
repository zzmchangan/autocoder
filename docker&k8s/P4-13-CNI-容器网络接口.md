# 第 13 章 · CNI:容器网络接口

> **前置**:你需要先读过[第 12 章《容器网络基础:veth / bridge / iptables》](P4-12-容器网络基础-veth-bridge-iptables.md)。那一章我们一块一块拆出了单机容器联网的四块积木——network namespace 把容器焊成孤岛,veth pair 把孤岛接到码头,bridge 把所有网线头汇成局域网,iptables NAT 让集装箱和外界互通。读完你应该有个挥之不去的感觉:**这套拼装虽然不复杂,但全是手工活**。每起一个容器,你都得创 namespace、拉 veth、塞 bridge、配 IP、写路由、加 iptables 规则……一台机器几十个容器,几百台机器上万容器,总不能让人手工一条条敲。更麻烦的是:不同公司对网络的要求天差地别——有的图省事用 bridge、有的要 BGP 路由做大规模三层网络、有的要 VXLAN 隧道跨数据中心打通。如果每换一种需求就得改一遍 docker / containerd 的源码,这生态根本走不到今天。这一章,我们就来看容器世界是怎么把"网络"这件事**标准化成一个谁都能实现、谁都能替换的接口**的。它的名字,叫 **CNI**。

> **核心问题**:不同集群的网络需求千差万别(二层 bridge、三层 BGP、overlay 隧道、底层 SDN),怎么把它做成可插拔的标准件,让运行时(docker / containerd / k8s)和具体的网络实现彻底解耦?

> **读完本章你会明白**:
> - **为什么网络要被标准化成一个插件接口**:运行时不该、也不必关心你装的是 calico 还是 flannel——它只要会"按标准喊一声 ADD",自然有插件替它把活干完。CNI 干的就是把"喊话的口令"定死。
> - **CNI 的契约长什么样**:它简单到让人意外——一个可执行文件 + 几个环境变量 + 一段 stdin JSON + 一段 stdout JSON。ADD/DEL/CHECK/VERSION 几个命令、一个 IPAM 子插件机制,就构成了全部契约。我们会在 skel 骨架里逐行看清它怎么读环境变量、分发命令。
> - **bridge 插件怎么用四块积木落地这套契约**:上一章讲的 veth+bridge+route,iptables,在 bridge 插件里被封装成了一个"插上就能用"的标准件。看它的 `cmdAdd` 五步,你会拍大腿——"原来就是这套"。
> - **三大流派(bridge / calico / flannel)各自的取舍**:为什么有人选 overlay(隧道封装,跨数据中心简单),有人选路由(BGP,性能好但要底层网络支持)——它们的根,都还是上一章那几块积木,只是用了不同的"跨机搬运"策略。

> **如果一读觉得太难**:先只记住三件事——① CNI 是一个**接口标准**,不是一个网络实现;运行时按标准调你的插件,你的插件内部爱怎么实现就怎么实现;② 插件契约极简:**环境变量传命令和参数、stdin 传网络配置、stdout 回结果**,就这三条管道;③ 三大流派的差别只在"跨机的包怎么搬过去"——bridge 不跨机、flannel 用隧道套一层封装、calico 用 BGP 让物理路由器自己会路由。本章协议细节多,看不懂时这三句话兜底。

---

## 章首·一句话点破

第 12 章结尾我们留了个钩子:四块积木拼通了一个容器的网络,可真实的集群里,容器散布在几百台机器上,跨机的容器怎么通信?而且——更要命的是——**每家公司的网络环境都不一样**。有的机房交换机随便玩、能跑 BGP;有的云上动不了底层网络、只能搞 overlay 隧道;有的小团队就一台机器,bridge 凑合用。如果 docker / k8s 想支持这些,难道要把所有可能的网络方案都塞进自己的源码?

这一章我们就来看容器世界给出的答案,一句话讲清:

> **CNI 不是一种网络,而是一套"插口标准"。运行时(docker / containerd / k8s)只管按标准喊"给这个容器配网络(ADD)",至于具体怎么配——是创 bridge、是建 VXLAN 隧道、是下发 BGP 路由——全交给一个可替换的插件二进制去做。**

> **比喻**:回到我们的港口。上一章我们在单个码头内部修路:给每个集装箱拉根 veth 网线、接到 docker0 这台虚拟交换机上、在登记台做 NAT。可现实里,**不同港口的地形千差万别**:有的港口依山而建,得挖隧道才能把集装箱连到外部铁路(overlay);有的港口一马平川,直接铺大路就行(路由);有的小码头,内部 bridge 够用了。如果硬性规定"全世界港口都必须按一种地形修路",那不现实。
>
> CNI 干的事,就是**把"接集装箱的那个标准插口"定死**:不管你港口的地形多特殊,只要这个插口的尺寸、协议、信号约定符合标准,任何船公司的集装箱插上就能用。至于插口后面是隧道、是大路、是悬索桥——CNI 不关心,那是港口(网络厂商)自己的事。**标准化的只是"接口",而不是"实现"。**

这是一道分水岭。读完它,你就理解了为什么容器网络生态能像今天这样百花齐放:calico、flannel、cilium、weave、kube-router……几十种实现,却都无缝地插进同一套运行时。

我们从最朴素的痛点说起。

---

## 一、为什么网络必须标准化:从"每换一次就重写"说起

### 不这样会怎样:运行时和网络实现死死绑死

假设没有 CNI。docker 自己内置了一套网络逻辑(就是上一章那套 veth+bridge+iptables)。现在你接了个新需求:我不要 bridge,我要让不同宿主上的容器直连,得搞 overlay 隧道。你会怎么办?

- **方案一:改 docker 源码**,把内置的 bridge 逻辑换成 VXLAN 逻辑。可每来一种新需求(今天 overlay,明天 BGP,后天 IPVLAN)就改一次 docker?docker 团队会被需求淹没,而且不同需求还互相冲突(有人要 overlay、有人要路由,你内置谁?)。
- **方案二:fork 出来自己改**。结果是 docker 碎成几十个分支,生态彻底分裂,镜像、命令行、API 全跟着分叉。

这个困局不是假设。**docker 早期真的就把网络逻辑死死焊在自己 daemon 里**。2014 年前后,CoreOS 公司做 rkt 容器时,发现自己每搞一种网络就得跟 docker 走完全不同的路;Mesos、Kubernetes 也都各写各的。每家运行时 × 每家网络方案 = 组合爆炸。整个容器网络生态乱成一锅粥。

> **比喻**:这就好比每家航运公司都自己规定"集装箱的插口长什么样"。A 公司的插口是圆的、B 公司是方的,你要把 A 公司的集装箱搬到 B 公司的船上,得专门做个转接头。结果就是集装箱没法通用,物流网根本铺不开。**标准集装箱之所以能改变世界,恰恰是因为它的插口尺寸全球统一**——任何船、任何卡车、任何起重机都能接。容器网络要走出"每家自己造轮子"的泥潭,需要的也是这么一个统一插口。

### 所以这样:把"接口"和"实现"彻底拆开

CNI(Container Network Interface,容器网络接口)就是为这个而生的。它的核心思想,一句话:

> **把"运行时怎么喊"和"插件怎么干"彻底分离。运行时只认一套标准的喊话口令(ADD/DEL/CHECK + JSON 配置);任何按这套口令实现的程序,都是一个合格的网络插件,可以无缝替换。**

这套思路其实不新——它和第 8 章要讲的 OCI 运行时标准(runc)、第 21 章要讲的 CSI 存储接口,是同一招的复用:**把可变的部分(具体实现)用一个稳定的接口包起来,让上下游解耦**。这一招是云原生整个生态能繁荣的根本。CNI 是其中最干净的一个例子,因为它的接口小到出奇。

> **回扣全书主线**:还记得总纲里的那条哲学吗——"分层标准解耦"。OCI 把"镜像怎么跑"标准化(runc 谁都能换);CNI 把"网络怎么配"标准化(calico/flannel 谁都能换);CSI 把"存储怎么接"标准化。**三件大事,一个套路**。理解了 CNI,你就掌握了云原生解耦的范式。

那么,这个"标准插口"具体长什么样?我们进契约里看。

---

## 二、CNI 的契约长什么样:简单到让人意外

很多人第一次接触 CNI,以为它是某种复杂的协议栈、某种 RPC 框架。完全不是。**CNI 的契约简单到可以用三句话讲完**:

> **一个 CNI 插件,就是一个可执行文件(注意:是独立的二进制,不是动态库)。运行时通过三条管道跟它通信:**
> 1. **环境变量(env)**:告诉它"要干什么命令"(`CNI_COMMAND=ADD`)、"给哪个容器干"(`CNI_CONTAINERID`)、"容器的 namespace 在哪"(`CNI_NETNS`)、"网卡叫什么名"(`CNI_IFNAME`)。
> 2. **标准输入(stdin)**:把一段 JSON 格式的**网络配置**喂给它——这个网络叫什么、用什么 IPAM、地址段是多少。
> 3. **标准输出(stdout)**:它干完活,把**结果(IP 配置、路由、DNS)**以 JSON 形式打回 stdout,运行时读回去。

就这么三条。**没有 gRPC、没有 socket、没有长连接**。运行时 fork 出这个插件进程,通过环境变量和 stdin 把活交代清楚,等它干完从 stdout 读结果,然后进程退出。一次 ADD,就是一次进程调用。

> 这个设计的优雅之处:**它把"插件"这个概念降维到了"一个会读 stdin、写 stdout 的命令行程序"**。任何语言都能写(Go、C、Python、Shell 都行),任何运行时都能调(只要会 fork 进程、会设环境变量)。这是 Unix 哲学——"一切皆文件、用文本流通信"——在网络插件领域的完美复刻。**CNI 没有发明新东西,它只是把 Unix 几十年的老传统,套到了容器网络上。**

### 命令:就那几种

CNI 的命令,就是插件要响应的几个动作。到当前规范(1.1.0)为止,一共 6 个,但最常用的是前 3 个:

| 命令 | env `CNI_COMMAND` | 干什么 | 什么时候调 |
|---|---|---|---|
| **ADD** | `ADD` | 给容器配网络(建网卡、分 IP、配路由) | 容器**启动**时 |
| **DEL** | `DEL` | 拆掉容器的网络(删网卡、回收 IP) | 容器**销毁**时 |
| **CHECK** | `CHECK` | 检查容器的网络配置是否还正常 | 健康检查、kubelet 周期巡检 |
| **VERSION** | `VERSION` | 报告自己支持的 CNI 版本 | 运行时探测插件能力 |
| GC | `GC` | 垃圾回收:清理已不存在的容器残留网络 | 1.1.0 新增,周期清理 |
| STATUS | `STATUS` | 报告网络本身的状态(比如 bridge 在不在) | 1.1.0 新增 |

> **注意**:`ADD` 和 `DEL` 是命脉。一个容器的一生,在 CNI 看来就是"ADD 一次、DEL 一次"——出生配网、死亡收网。CHECK 是后来(0.4.0)加的,让运行时能周期性地问一句"这个容器的网络还好吗?",而不是非得等出事。GC 和 STATUS 更新(1.1.0),是为了解决"容器意外死了没来得及 DEL"这种残留垃圾的场景。**CNI 协议在演进,但 ADD/DEL 这对核心从未变过**——它们对应容器生命周期的两端。

### ADD 时插件收到的输入

运行时调一个插件的 ADD,实际上是这么个调用(命令行的视角):

```bash
CNI_COMMAND=ADD \
CNI_CONTAINERID=deadbeef1234... \
CNI_NETNS=/var/run/netns/abc123 \      # 容器 network namespace 的路径
CNI_IFNAME=eth0 \                        # 容器里这张网卡叫什么
CNI_PATH=/opt/cni/bin \                  # 插件二进制在哪个目录
CNI_ARGS="IgnoreUnknown=1,K8S_POD_NAMESPACE=default,K8S_POD_NAME=nginx" \
/opt/cni/bin/bridge < /etc/cni/net.d/10-bridge.conf
```

左边那堆 `CNI_*` 是环境变量(注意:**是 env,不是命令行参数**——这是 CNI 协议一个值得记住的设计选择,我们待会讲 skel 时会看到它为什么这么设计)。`/etc/cni/net.d/10-bridge.conf` 是网络配置文件,内容是个 JSON,通过 stdin 喂给插件。它大致长这样:

```json
{
  "cniVersion": "1.0.0",
  "name": "my-bridge-net",
  "type": "bridge",
  "bridge": "cni0",
  "isGateway": true,
  "ipMasq": true,
  "ipam": {
    "type": "host-local",
    "ranges": [
      [{ "subnet": "10.244.1.0/24" }]
    ],
    "routes": [
      { "dst": "0.0.0.0/0" }
    ]
  }
}
```

读这段 JSON,注意它的结构正好对应"两层":

- **外层是网络插件自己的配置**:`type: bridge`(用 bridge 插件)、`bridge: cni0`(虚拟交换机叫这名)、`isGateway: true`(给 bridge 配个网关 IP)、`ipMasq: true`(出网时做 SNAT)。
- **内层 `ipam` 段是"IP 地址管理"的配置**:`type: host-local`(用 host-local 这个 IPAM 插件)、`ranges` 是地址段、`routes` 是要配的默认路由。

**这个"插件里嵌一个 ipam"的结构,是 CNI 契约里最巧妙的一笔**,我们单独讲。

### IPAM 子插件:把"分 IP"也拆出来

注意上面 JSON 里 `ipam.type` 是 `host-local`——这又是一个**插件名**,但和外面的 `bridge` 插件不是一回事。它是 **IPAM(IP Address Management)插件**,专门管"给容器分哪个 IP"这一件事。

CNI 把网络插件分成了**两层**:

- **主插件(main)**:管"网卡怎么连"。bridge、ipvlan、macvlan、ptp 这些属于这一层。
- **IPAM 插件**:管"IP 怎么分"。host-local(本地文件记账)、dhcp(DHCP 申请)、static(写死)这些属于这一层。

> **为什么不把分 IP 的事也塞进主插件?** 因为这两件事的关注点正交。你用 bridge 组网,IP 可以是 host-local 分配的;你换成 calico(BGP)组网,IP 也可以用同一个 host-local——**组网方式和 IP 分配方式是两个独立的维度**。把它们拆开,就能自由组合:任何 main 插件 × 任何 IPAM 插件。这正是软件工程里"关注点分离"的标准操作,被 CNI 干净利落地落到了网络配置上。

主插件要分 IP 时,会通过一个叫"委托(delegate)"的机制,把 IPAM 插件也调一次(同样走 stdin/stdout 那套契约),拿到分配好的 IP,再继续完成自己的组网工作。待会看 bridge 插件源码时,你会看到那行 `ipam.ExecAdd(...)`——它就是这个委托调用的入口。

### ADD 成功后插件返回的结果

插件干完活,要把结果用 JSON 打到 stdout。结果长这样(简化):

```json
{
  "cniVersion": "1.0.0",
  "interfaces": [
    { "name": "eth0", "sandbox": "/var/run/netns/abc123" }
  ],
  "ips": [
    {
      "interface": 0,
      "address": "10.244.1.5/24",
      "gateway": "10.244.1.1"
    }
  ],
  "routes": [
    { "dst": "0.0.0.0/0", "gw": "10.244.1.1" }
  ],
  "dns": {
    "nameservers": ["169.254.25.10"]
  }
}
```

读这个返回值,你会发现它**精准地回答了运行时关心的所有问题**:这个容器分到了哪个 IP(`ips`)、网关是谁、要配哪些路由(`routes`)、DNS 怎么设。运行时拿到这个 JSON,就知道容器的网络配好了。**一次 ADD 的全部对话,就是 stdin 进一段配置、stdout 回一段结果,中间没有任何握手、没有任何状态**。这种无状态的、一次性的进程调用,是 CNI 契约的全部。

> 这就是 CNI 的全部契约:**6 个命令 + 3 条管道(stdin/stdout/env)+ 两层插件(main + IPAM)+ 1 个 JSON 结果**。没有更多了。任何能读懂这份契约的程序,都是合法的 CNI 插件。这份简洁,是 CNI 能被几十种实现共同遵守的根本原因——**接口越简单,越容易被遵守**。

那么,这套契约在代码里是怎么落地的?我们看 CNI 官方提供的"骨架库"。

---

## 三、skel 骨架:把契约的最枯燥部分替你写好

如果你要写一个 CNI 插件,从零开始按契约敲,会有一堆重复的样板代码:解析环境变量、读 stdin、判断命令、按版本分发、把错误格式化成 JSON 打到 stdout……每个插件都要写一遍,既无聊又容易写错。

CNI 官方库 `containernetworking/cni` 里有一个叫 **skel(skeleton,骨架)** 的包,专门把这部分替你封装好。你只要实现几个业务函数(ADD 干啥、DEL 干啥),剩下的协议解析 skel 全包。看真实源码,在 [pkg/skel/skel.go](https://github.com/containernetworking/cni/blob/master/pkg/skel/skel.go):

```go
// CmdArgs captures all the arguments passed in to the plugin
// via both env vars and stdin
type CmdArgs struct {
	ContainerID   string
	Netns         string
	IfName        string
	Args          string
	Path          string
	NetnsOverride string
	StdinData     []byte
}
```

读这个结构体([pkg/skel/skel.go#L35-L45](https://github.com/containernetworking/cni/blob/master/pkg/skel/skel.go#L35-L45)),它把运行时传给插件的所有输入,统一打包成一个 `CmdArgs`:容器 ID、netns 路径、网卡名、附加参数、插件搜索路径,以及那段 stdin 的原始字节(`StdinData`)。**插件作者拿到的就是这个打包好的对象,再也不用自己去 `os.Getenv("CNI_NETNS")` 了**——skel 已经替你读好了。

主入口函数有两个,新旧两版 API:

```go
// 新版 API(推荐):用一个 CNIFuncs 结构体统一注册所有回调
func PluginMainFuncsWithError(funcs CNIFuncs, versionInfo version.PluginInfo, about string) *types.Error {
	return (&dispatcher{
		Getenv: os.Getenv,
		Stdin:  os.Stdin,
		Stdout: os.Stdout,
		Stderr: os.Stderr,
	}).pluginMain(funcs, versionInfo, about)
}
```

其中 `CNIFuncs` 是一组回调函数的集合([pkg/skel/skel.go](https://github.com/containernetworking/cni/blob/master/pkg/skel/skel.go)):

```go
type CNIFuncs struct {
	Add    func(_ *CmdArgs) error
	Del    func(_ *CmdArgs) error
	Check  func(_ *CmdArgs) error
	GC     func(_ *CmdArgs) error
	Status func(_ *CmdArgs) error
}
```

也就是说,你写一个 CNI 插件,本质上就是**填这么一个结构体**:ADD 时调谁、DEL 时调谁……填好交给 skel,skel 替你处理一切协议细节。这是 Go 库设计的常见套路——"你给回调,我跑框架"。

### skel 怎么分发命令:env 驱动,不是 argv

这里有个**初学者最容易栽跟头**的细节:CNI 插件怎么知道自己该执行 ADD 还是 DEL?

直觉上,你会以为是通过命令行参数,比如 `bridge add` 或 `bridge del`。**错**。CNI 的命令是通过**环境变量** `CNI_COMMAND` 传进来的。看 skel 内部的分发逻辑(在 [pkg/skel/skel.go](https://github.com/containernetworking/cni/blob/master/pkg/skel/skel.go) 的 `pluginMain` 方法里):

```go
switch cmd {
case "ADD":
	err = t.checkVersionAndCall(cmdArgs, versionInfo, funcs.Add)
case "CHECK":
	...
case "DEL":
	err = t.checkVersionAndCall(cmdArgs, versionInfo, funcs.Del)
case "GC":
	...
case "STATUS":
	...
case "VERSION":
	if err := versionInfo.Encode(t.Stdout); err != nil { ... }
default:
	return types.NewError(types.ErrInvalidEnvironmentVariables, fmt.Sprintf("unknown CNI_COMMAND: %v", cmd), "")
}
```

那个 `cmd` 是从哪里来的?是 skel 一开始 `os.Getenv("CNI_COMMAND")` 读出来的([pkg/skel/skel.go](https://github.com/containernetworking/cni/blob/master/pkg/skel/skel.go) 的 `getCmdArgsFromEnv`)。所以,**CNI 协议是 env 驱动,而不是 argv 驱动**。

> **为什么选 env 而不是 argv?** 这是个有讲究的设计选择。argv 的长度有上限(一般几十 KB),而 stdin 可以传任意长的 JSON 配置;更重要的是,**用 env 传"命令 + 一堆元参数",用 stdin 专传"网络配置"**,职责分得很干净——env 是控制平面(干啥、给谁干),stdin 是数据平面(用什么参数干)。而且 env 不依赖参数顺序,扩展新参数时向后兼容性极好(老插件不认新 env 直接忽略即可)。**这是一个成熟的 Unix 程序设计决策,不是随手选的。**

### 错误怎么回:类型化错误,JSON 打到 stdout

最后看一个容易被忽略但很关键的细节:插件干失败了,怎么告诉运行时?

CNI 规定:**错误也要用 JSON 格式,打到 stdout(不是 stderr!),然后进程以非零退出码退出**。错误 JSON 长这样(简化):

```json
{
  "code": 7,
  "msg": "invalid network config",
  "details": "missing 'name' field"
}
```

`code` 是 CNI 定义的错误码(7 表示配置非法、11 表示"稍后再试"……),`msg` 是人能读的简述,`details` 是细节。运行时拿到非零退出码,就去读 stdout 解析这个 JSON,精确知道出了什么类型的错。

这套机制对应 skel 里 `PluginMainFuncs`(非 Error 版本)的行为:拿到 `*types.Error` 后调 `e.Print()` 把它 JSON 化写到 stdout,然后 `os.Exit(1)`([pkg/skel/skel.go](https://github.com/containernetworking/cni/blob/master/pkg/skel/skel.go))。错误类型本身定义在 [pkg/types/types.go](https://github.com/containernetworking/cni/blob/master/pkg/types/types.go):

```go
type Error struct {
	Code    uint   `json:"code"`
	Msg     string `json:"msg"`
	Details string `json:"details,omitempty"`
}
```

> **为什么错误也要这么讲究?** 因为运行时要根据错误类型做不同的处理:遇到"配置非法"(code 7),说明是用户配置错了,别重试了;遇到"稍后再试"(code 11),说明是临时冲突(比如 IP 暴时被占),可以过会儿再来;遇到"IPAM 不可用"(code 11 也用于此类),可能是底层服务没起来,得告警。**类型化的错误,让运行时能做智能的重试和告警决策,而不是只能"重试或放弃"二选一**。这是工程化接口的成熟标志。

读到这里,你应该对 CNI 契约的"骨架"有了完整印象。下一节,我们看一个真实插件是怎么填进这个骨架的——从抽象落到具体。

---

## 四、bridge 插件:四块积木的标准封装

CNI 官方仓库 [containernetworking/plugins](https://github.com/containernetworking/plugins) 里自带了一组参考插件,其中最简单、最经典的就是 **bridge 插件**。它干的活,正是第 12 章我们一笔一画拆出来的那套——veth + bridge + route + iptables。看它怎么把这套手工活封装成一个标准插件,你会对"接口 vs 实现"有切肤的理解。

### 插件的入口:填好 CNIFuncs,交给 skel

先看 bridge 插件的 `main` 函数,在 [plugins/main/bridge/bridge.go#L836-L844](https://github.com/containernetworking/plugins/blob/main/plugins/main/bridge/bridge.go#L836-L844):

```go
func main() {
	skel.PluginMainFuncs(skel.CNIFuncs{
		Add:    cmdAdd,
		Check:  cmdCheck,
		Del:    cmdDel,
		Status: cmdStatus,
		/* FIXME GC */
	}, version.All, bv.BuildString("bridge"))
}
```

读这 8 行,你就看懂了一个 CNI 插件的全部"注册"逻辑:把 `cmdAdd`、`cmdDel`、`cmdCheck`、`cmdStatus` 四个函数,塞进 `skel.CNIFuncs` 结构体,交给 `skel.PluginMainFuncs`。从这一行起,skel 接管——收到 `ADD` 命令就调 `cmdAdd`,收到 `DEL` 就调 `cmdDel`。**插件作者写的"业务代码",就是 `cmdAdd`/`cmdDel` 这几个函数的实现**。CNI 协议的全部复杂度,都被 `skel.PluginMainFuncs` 这一行挡在了门外。

注意那个 `/* FIXME GC */` 注释——bridge 插件目前还没实现 GC 命令(1.1.0 新增的垃圾回收),留了个坑。这印证了我们前面说的:**CNI 协议在演进,但插件可以选择性实现**。GC 和 STATUS 是可选的,ADD/DEL/CHECK 是必选项。

### 读取配置:NetConf 结构体

`cmdAdd` 第一步,是把 stdin 的那段 JSON 配置,反序列化成一个 Go 结构体。这个结构体叫 `NetConf`,定义在 [plugins/main/bridge/bridge.go#L48-L76](https://github.com/containernetworking/plugins/blob/main/plugins/main/bridge/bridge.go#L48-L76):

```go
type NetConf struct {
	types.NetConf
	BrName                    string `json:"bridge"`
	IsGW                      bool   `json:"isGateway"`
	IsDefaultGW               bool   `json:"isDefaultGateway"`
	ForceAddress              bool   `json:"forceAddress"`
	IPMasq                    bool   `json:"ipMasq"`
	IPMasqBackend             *string `json:"ipMasqBackend,omitempty"`
	MTU                       int    `json:"mtu"`
	HairpinMode               bool   `json:"hairpinMode"`
	PromiscMode               bool   `json:"promiscMode"`
	Vlan                      int    `json:"vlan"`
	...
}
```

读这个结构体,你会看到 JSON 配置里每一个字段都对应到这里:`BrName`(bridge 名字,默认 `cni0`)、`IsGW`(要不要给 bridge 配网关 IP)、`IPMasq`(要不要做 SNAT 出网)、`MTU`(网卡的 MTU)、`HairpinMode`(发夹模式,容器访问自己暴露的端口用)……**配置字段就是插件能力的清单**——这个插件支持哪些可调参数,看一眼 `NetConf` 就清楚了。

注意第一行 `types.NetConf`——这是嵌入了 CNI 库提供的**通用配置基类**(在 [pkg/types/types.go#L64-L78](https://github.com/containernetworking/cni/blob/master/pkg/types/types.go#L64-L78)),里面有 `CNIVersion`、`Name`、`Type`、`IPAM`、`DNS` 这些**所有插件通用的字段**。每个插件自己再加特有字段。这是 Go 的结构体嵌入,优雅地复用了公共部分。

> **回扣第 12 章**:看到 `IPMasq` 这个字段了吗?它就是上一章讲的 MASQUERADE 出网规则的开关。配置里写 `ipMasq: true`,bridge 插件就会在 `cmdAdd` 里给 iptables nat 表加一条 MASQUERADE 规则。**CNI 没有发明任何网络机制,它只是把你上一章学的那套 veth/bridge/iptables,用一个结构体 + 一个 `cmdAdd` 函数封装成了标准件。**

### cmdAdd 的五步:把第 12 章的手工活复现一遍

现在到本章的高潮:`cmdAdd` 函数到底干了什么。它在 [plugins/main/bridge/bridge.go#L535-L762](https://github.com/containernetworking/plugins/blob/main/plugins/main/bridge/bridge.go#L535-L762),几百行,但骨架就五步。我们逐步拆:

**第一步:创建/找到那台虚拟交换机。** `cmdAdd` 调 `setupBridge(n)`,内部调 `ensureBridge`——如果 `cni0` 这台 bridge 不存在就创建它,存在就用现成的。**这正是第 12 章我们说的"docker0 是宿主上一台虚拟交换机"——只不过这里它叫 `cni0`,由 bridge 插件来创建,而不是 docker。**

**第二步:打开容器的 netns,创建 veth 对,把宿主端挂到 bridge 上。** 这一步是核心,逐字看([bridge.go#L562-L568](https://github.com/containernetworking/plugins/blob/main/plugins/main/bridge/bridge.go#L562-L568) 附近):

```go
netns, err := ns.GetNS(args.Netns)
...
hostInterface, containerInterface, err := setupVeth(netns, br, args.IfName, n.MTU, ...)
```

`ns.GetNS(args.Netns)` 打开运行时传进来的容器 network namespace(就是上一章那个 `/var/run/netns/abc123`)。`setupVeth` 干的事,正是第 12 章讲的:创建一对 veth,一端移进容器 netns 改名 `eth0`,另一端留在宿主并 `netlink.LinkSetMaster(hostVeth, br)`——把宿主端"插"到 cni0 这台 bridge 上。**底层用的还是上一章的 veth + bridge,一个字母都没变。**

**第三步:委托 IPAM 插件分配 IP。** bridge 插件自己不分 IP,它把这事委托给配置里指定的 IPAM 插件(host-local):

```go
r, err := ipam.ExecAdd(n.IPAM.Type, args.StdinData)
```

这一行([bridge.go#L599](https://github.com/containernetworking/plugins/blob/main/plugins/main/bridge.go#L599))极其关键。`ipam.ExecAdd` 干的事,就是**再去 fork 一次 `/opt/cni/bin/host-local` 这个插件,同样按 CNI 契约(stdin 配置、env 参数、stdout 返回)**,让 host-local 从地址段里挑一个没被用的 IP,返回给 bridge 插件。**IPAM 是一个完整的 CNI 插件,被主插件嵌套调用**——这就是前面说的"委托"机制。

注意失败时还有一行 `defer ipam.ExecDel(...)`([bridge.go#L607](https://github.com/containernetworking/plugins/blob/main/plugins/main/bridge/bridge.go#L607) 附近)——如果后面哪一步出错了,bridge 插件会主动把刚分到的 IP 还回去,避免 IP 泄漏。**这种"配了一半要回滚"的容错,是工程化代码的标志**。

**第四步:把分到的 IP 配到容器的 eth0 上,并加默认路由。** 调 `ipam.ConfigureIface(args.IfName, result)`([bridge.go#L642](https://github.com/containernetworking/plugins/blob/main/plugins/main/bridge/bridge.go#L642))。这一步进到容器的 netns,用 netlink 给 `eth0` 设上 IP、把网卡 up 起来、加默认路由(指向 bridge 的 IP 作为网关)。**第 12 章里我们手工 `ip addr add`、`ip route add` 干的事,这里用 Go 的 netlink 库程序化地干了。**

**第五步:给 bridge 自己配网关 IP,开内核转发,装 IPMasq 规则,返回结果。** `ensureAddr(br, ...)` 给 cni0 配上网关 IP;`enableIPForward(...)` 打开 `net.ipv4.ip_forward`(容器跨宿主通信用得到);如果配置里 `ipMasq: true`,再调 `ip.SetupIPMasqForNetworks(...)` 加 MASQUERADE 规则。最后 `return types.PrintResult(result, cniVersion)` 把整个结果 JSON 化打到 stdout。

> 把这五步并起来看,你会发现:**bridge 插件干的事,和第 12 章我们一笔一画讲的"给容器联网的手工步骤",一字不差**。它没有发明任何新机制,它只是把 veth/bridge/route/iptables 这套手工活,用 Go 代码封装成了一个符合 CNI 契约的可执行文件。**CNI 的意义,不在于它做了什么新东西,而在于它把"已知的、手工的、分散的"网络配置动作,标准化成了一个可插拔、可替换的单元。**

`cmdDel`([bridge.go#L771-L834](https://github.com/containernetworking/plugins/blob/main/plugins/main/bridge/bridge.go#L771-L834))就是 `cmdAdd` 的逆操作:进容器 netns 删 veth、委托 IPAM 回收 IP、拆 IPMasq 规则。一对 ADD/DEL,对应容器的一生。

---

## 五、host-local IPAM:防 IP 重复的最朴素办法

bridge 插件委托的 IPAM 通常是 **host-local**。它的实现极其朴素,值得一看——因为它揭示了"分配 IP 防重复"这件事的本质。

host-local 的分配策略就一句话:**从一个地址段里挑一个没用过的 IP,然后把它"记下来",下次不重复分配**。"记下来"的方式朴素到可爱——用一个**文件**。看真实的分配函数,在 [plugins/ipam/host-local/backend/disk/backend.go](https://github.com/containernetworking/plugins/blob/main/plugins/ipam/host-local/backend/disk/backend.go):

```go
func (s *Store) Reserve(id string, ifname string, ip net.IP, rangeID string) (bool, error) {
	fname := GetEscapedPath(s.dataDir, ip.String())

	f, err := os.OpenFile(fname, os.O_RDWR|os.O_EXCL|os.O_CREATE, 0o600)
	if os.IsExist(err) {
		return false, nil
	}
	...
	if _, err := f.WriteString(strings.TrimSpace(id) + LineBreak + ifname); err != nil { ... }
	...
	return true, nil
}
```

读这段([backend.go#L47 起](https://github.com/containernetworking/plugins/blob/main/plugins/ipam/host-local/backend/disk/backend.go#L47))——**防 IP 重复分配的全部秘密,就是 `os.O_EXCL|os.O_CREATE` 这两个 flag**。它的语义是:原子地创建一个文件,**如果文件已存在则失败**。host-local 用 IP 字符串作为文件名(比如 `10.244.1.5`),放在 `/var/lib/cni/networks/<网络名>/` 目录下。**"这个 IP 有没有被占"="这个文件存不存在"**。一次 `OpenFile` 系统调用,内核保证原子性,完美解决并发分配的竞态。

> 这个设计的高明,在于它**把分布式系统里最头疼的"互斥分配"问题,降维成了文件系统的一个原子操作**。不需要锁服务、不需要数据库、不需要共识协议——一个 `O_EXCL` 标志位搞定。这是 Unix 哲学的又一次胜利:**文件系统就是最可靠的持久化状态存储,能用文件解决的事,别发明新机制。**

host-local 默认从地址段的某个起点开始,**轮询**着分配(每个 range 有个 `last_reserved_ip` 文件记上次分到哪了,下次接着往后挑)。这种分配策略有个明显局限:**它只在本机记账,跨宿主的 IP 唯一性它保证不了**。所以 host-local 通常配合"每台宿主一个不重叠的地址段"来用——比如节点 A 用 `10.244.1.0/24`,节点 B 用 `10.244.2.0/24`,各自在自己的段内 host-local 分配,天然不冲突。

> 跨宿主 IP 协调这事,正是 calico/flannel 这些"大插件"要解决的——它们要么用 etcd/consul 做集中式记账,要么用 BGP 让节点之间互相通告。但**无论上层方案多复杂,落到单机分配这一步,底下往往还是 host-local 这种朴素的"文件 + 原子创建"**。理解了 host-local,你就理解了 IPAM 的最小内核。

---

## 六、三大流派:bridge / calico / flannel 的取舍

讲完单机插件,我们终于可以回答本章标题里的核心问题了:**不同集群的网络需求千差万别,具体差在哪?为什么有人选这种、有人选那种?**

这一节我们不展开每个插件的全部细节(那是几本书的量),只讲**它们各自的"为什么"和"代价"**。理解了取舍,你就知道遇到什么场景该选什么。

### 流派一:bridge——单机或小规模,够用就好

bridge 插件我们刚拆过,它就是上一章四块积木的标准封装。它的特点:**只在单机内连通**——所有容器接在同一个 cni0 上,跨宿主的通信它不管(交给宿主的正常路由 + NAT,每个容器都靠宿主出口)。

- **适合**:单机、小集群、学习环境、CI 测试。docker 默认网络本质上就是 bridge。
- **代价**:跨宿主的容器通信要么走 NAT(性能差、外部看不到容器真实 IP),要么手动配路由(规模一大就管不过来)。**bridge 是"单机方案",不是"集群方案"**。

### 流派二:flannel——overlay 隧道,跨数据中心一把梭

跨机容器通信,flannel 给的第一种主流答案是 **overlay 隧道**(它也支持 host-gw 模式,但最有代表性的是 VXLAN)。思路是:**既然不同宿主的容器处在不同的私网网段,中间的物理网络又不认识这些私网 IP,那就在每个包外面再套一层封装,让物理网络只看到"宿主到宿主"的包,到了对端宿主再拆封,转给目标容器。**

> **比喻**:这套封装就像**国际转运的"外交邮袋"**。你从北京寄一个包裹到纽约的某个集装箱,但国际物流网只认"北京港到纽约港",不认识集装箱内部编号。怎么办?把你的包裹装进一个标准的外交邮袋,邮袋外面只写"北京→纽约"。国际物流照着港口路由邮袋,到了纽约港拆开邮袋,里面的包裹再按内部编号送到具体集装箱。**包裹(原始 IP 包)全程没动过,只是外面套了一层、到了拆一层**。这就是 overlay 隧道的全部精髓。

flannel 的 VXLAN 模式就是这么干的:原始包(源 IP 是容器 A、目标 IP 是容器 B)外面套一个 UDP 头 + VXLAN 头,源/目标 IP 改成两个宿主的物理 IP。物理网络看到的只是"宿主 A 给宿主 B 发了个 UDP 包",正常路由就行。到了宿主 B,内核的 VXLAN 设备拆掉外层封装,把原始包交给容器 B。

- **适合**:跨数据中心、跨云、底层网络不可控(只能走 IP 互通)的场景。**几乎任何环境都能跑**——这是 overlay 最大的优点。
- **代价**:**性能损耗**。每个包都要多一层封装(额外几十字节头部 + 一次封装/拆封的 CPU 开销),MTU 还要相应缩小(否则包超长分片)。对吞吐敏感的场景,overlay 是有成本的。而且**排查问题更难**——网络问题包在隧道里,用 tcpdump 抓到的全是封装后的包,得懂怎么解封装才看得懂。

flannel 还有个 **host-gw 模式**:不封装,而是直接在每台宿主上下发一条静态路由——"要去 10.244.2.0/24 这个网段,下一跳是宿主 B 的物理 IP"。这样原始包不封装,直接靠物理网络路由。性能好,但**要求底层网络是二层互通的**(宿主之间能直接路由彼此的物理 IP,且不挡 IP 转发)。

### 流派三:calico——BGP 路由,性能好但要底层配合

calico 选了第三条路:**不用封装、不用静态路由,而是用真实的路由协议(BGP)让物理网络"学会"怎么路由到容器**。

思路:每台宿主上跑一个 BGP 客户端,把自己上面容器的网段(比如"我这台机器有 10.244.1.0/24")通过 BGP 协议**通告**给其他宿主(甚至通告给物理路由器)。所有宿主(和路由器)的 BGP 表凑起来,就是一个完整的"容器网段 → 该往哪台宿主送"的路由表。原始包完全不用封装,纯靠物理网络的三层路由就能送到目标容器所在的宿主。

> **比喻**:calico 不靠"外交邮袋"这种偷偷摸摸的封装,而是**光明正大地去邮局登记**——告诉整个物流网:"10.244.1.0/24 这片内部编号归北京港管,谁要寄到这片,送到北京港来"。物理物流网学会了这条路由后,包裹走的就是正常投递路径,该转哪里转哪里。**没有封装,没有拆封,纯路由。**

- **适合**:大规模、性能敏感、底层网络可控(能跑 BGP、能配路由)的场景。很多大型生产集群选 calico 就是图它性能好、可观测性强(路由表就是一张完整的网络地图)。
- **代价**:**对底层网络有要求**。云厂商的某些网络环境不允许你跑 BGP(比如一些 VPC 模式),这时 calico 也只能退回去用 IPIP 或 VXLAN 封装(它都支持)。而且 BGP 在超大规模下也有自己的挑战(路由表爆炸、收敛速度),需要做路由反射器(route reflector)来分层。**calico 把复杂度转移到了路由协议层**——好处是不封装,代价是要懂 BGP。

### 三派的共同点:底下都是上一章那几块积木

把三派放一起看,你会发现一个让人踏实的事实:

> **不管上面是 bridge、是 flannel 隧道、是 calico BGP,落到每台宿主内部,容器接出去的那根线,还是上一章讲的 veth;把容器网线头汇起来的那台虚拟交换机,还是 bridge(或它的变种);做地址翻译的,还是 iptables。** 区别只在"跨机的包怎么从这台宿主搬到那台宿主"——这一步,bridge 不管(交给宿主正常路由 + NAT),flannel 用隧道套一层封装,calico 用 BGP 让物理路由器自己会路由。

**跨机网络的本质,就是回答一个问题:"容器 A 发给容器 B 的包,怎么跨越中间的物理网络?"** 三种答案对应三种工程哲学:

| 流派 | 跨机搬运方式 | 比喻 | 代价 |
|---|---|---|---|
| **bridge** | 不搬运(单机内)或靠宿主 NAT | 码头内部路 | 跨机基本靠 NAT,性能和可观测性都差 |
| **flannel(VXLAN)** | 套一层封装,物理网只看宿主间 | 外交邮袋 | 封装开销,排查难 |
| **calico(BGP)** | 让物理网学会路由到容器 | 去邮局登记路由 | 要求底层能跑 BGP |

> **回扣第 12 章**:你看,**CNI 标准化的只是"插件接口",而不是"网络实现"**。它没有规定你怎么跨机——它只规定了你必须响应 ADD/DEL、必须返回 IP 和路由。至于 ADD 内部你是创 bridge、是建隧道、是下发 BGP,那是你的自由。**这正是"接口与实现分离"的力量**:一个稳定的接口,允许多种实现并存,让用户按场景选最合适的。第 8 章讲 OCI 时你会再见到同一招,第 21 章讲 CSI 存储接口时又见一次。

---

## 关键源码精读:bridge 插件的 cmdAdd,从契约到积木

讲完原理和三派取舍,我们把本章最核心的一段——**bridge 插件的 `cmdAdd`**——在源码层面拆透。它是"上一章四块积木"和"本章 CNI 契约"的交汇点。看懂它,你就彻底理解了"标准化的接口,封装已知的积木"这句话的全部含义。

### 1. 注册:CNIFuncs 是契约的全部入口

回到 [bridge.go#L836-L844](https://github.com/containernetworking/plugins/blob/main/plugins/main/bridge/bridge.go#L836-L844) 那个 `main`:

```go
func main() {
	skel.PluginMainFuncs(skel.CNIFuncs{
		Add:    cmdAdd,
		Check:  cmdCheck,
		Del:    cmdDel,
		Status: cmdStatus,
		/* FIXME GC */
	}, version.All, bv.BuildString("bridge"))
}
```

这一行是整个插件"对外契约"的浓缩。`version.All` 是这个插件支持的 CNI 版本列表(从 0.1.0 到 1.1.0 全支持,见 CNI 库的 [pkg/version/version.go](https://github.com/containernetworking/cni/blob/master/pkg/version/version.go)),`bv.BuildString("bridge")` 是版本信息字符串。**skel 拿到这三样东西(回调函数、支持版本、名字),就能完整响应 CNI 协议的一切请求**。插件作者写的"业务",就藏在 `cmdAdd`/`cmdDel` 这几个函数的实现里。

### 2. cmdAdd 的第一步:解析配置 + 准备 bridge

`cmdAdd` 一进来,先把 stdin 的 JSON 反序列化成 `NetConf`,然后调用 `setupBridge(n)`(在 [bridge.go#L514](https://github.com/containernetworking/plugins/blob/main/plugins/main/bridge/bridge.go#L514)),内部走 `ensureBridge`(在 [bridge.go#L332](https://github.com/containernetworking/plugins/blob/main/plugins/main/bridge/bridge.go#L332)):

```go
br, brInterface, err := setupBridge(n)
```

`ensureBridge` 干的事:**用 netlink 创建或找到那台 bridge 设备**(默认名 `cni0`,见常量 `defaultBrName = "cni0"`)。如果 bridge 已存在就用现成的;不存在就 `netlink.LinkAdd` 造一个,再 `LinkSetUp` 把它 up 起来。**这一步对应第 12 章的"创建 docker0 那台虚拟交换机"——只不过这里是 CNI 插件自己造的,叫 cni0。**

### 3. cmdAdd 的第二步:打开 netns + 创建 veth + 挂到 bridge

这是组网的核心,在 [bridge.go#L562-L568](https://github.com/containernetworking/plugins/blob/main/plugins/main/bridge/bridge.go#L562-L568):

```go
netns, err := ns.GetNS(args.Netns)
...
hostInterface, containerInterface, err := setupVeth(netns, br, args.IfName, n.MTU, n.HairpinMode, n.Vlan, n.vlans, n.PreserveDefaultVlan, n.mac, n.PortIsolation)
```

`ns.GetNS(args.Netns)` 打开运行时传来的容器 netns 路径。`setupVeth`(在 [bridge.go#L404-L488](https://github.com/containernetworking/plugins/blob/main/plugins/main/bridge/bridge.go#L404-L488))进到容器 netns 里,调用 [pkg/ip/link_linux.go](https://github.com/containernetworking/plugins/blob/main/pkg/ip/link_linux.go) 的 `ip.SetupVeth`:

```go
// SetupVeth sets up a pair of virtual ethernet devices.
// Call SetupVeth from inside the container netns.
func SetupVeth(contVethName string, mtu int, contVethMac string, hostNS ns.NetNS) (net.Interface, net.Interface, error) {
	return SetupVethWithName(contVethName, "", mtu, contVethMac, hostNS)
}
```

读这段注释——**"Call SetupVeth from inside the container netns"**。这个函数必须在容器 netns 里调用,它会一次创建一对 veth,把对端(`PeerNamespace: netlink.NsFd(int(hostNS.Fd()))`)丢回宿主 netns。回到宿主 netns 后,`setupVeth` 再调 `netlink.LinkSetMaster(hostVeth, br)`——**把宿主端那张网卡"插"到 cni0 这台 bridge 上**。

> **这一步就是第 12 章那张 docker0 拓扑图的程序化复现**。差别只在:docker 用 docker0、bridge 插件用 cni0;docker 在 daemon 里写、bridge 插件在 `cmdAdd` 里写。**底下调用的是完全同一套内核能力——netlink 创建 veth、把网卡 attach 到 bridge。CNI 没有发明任何新硬件。**

### 4. cmdAdd 的第三步:委托 IPAM 分配 IP

到 [bridge.go#L599](https://github.com/containernetworking/plugins/blob/main/plugins/main/bridge/bridge.go#L599) 这一行:

```go
r, err := ipam.ExecAdd(n.IPAM.Type, args.StdinData)
```

`ipam.ExecAdd` 的实现在 [pkg/ipam/ipam.go](https://github.com/containernetworking/plugins/blob/main/pkg/ipam/ipam.go),极其简洁:

```go
func ExecAdd(plugin string, netconf []byte) (types.Result, error) {
	return invoke.DelegateAdd(context.TODO(), plugin, netconf, nil)
}
```

就一行——它**再去按 CNI 契约调用一次 `/opt/cni/bin/<IPAM插件名>`**(比如 host-local),把同样的网络配置 JSON 通过 stdin 传给它,等它返回分配好的 IP 结果。**IPAM 插件和主插件是平等的 CNI 公民,主插件用"委托"机制嵌套调用它们**。这是 CNI 契约"组合性"的体现:任何插件都能调任何 IPAM,自由搭配。

注意紧跟着的回滚保护([bridge.go#L607](https://github.com/containernetworking/plugins/blob/main/plugins/main/bridge/bridge.go#L607) 附近):

```go
defer func() {
	if err != nil {
		ipam.ExecDel(n.IPAM.Type, args.StdinData)
	}
}()
```

**如果后续步骤失败,主动把刚分到的 IP 还回去**。这种"配了一半要回滚"的容错逻辑,在手工配网络时根本顾不上——但封装成标准插件,必须做到滴水不漏。**这也是 CNI 把网络逻辑"产品化"的价值之一:手工活容易漏的边界情况,标准化插件会替你处理好。**

### 5. cmdAdd 的第四、五步:配 IP/路由 + 网关/转发/NAT + 返回

拿到 IP 后,`cmdAdd` 调 `ipam.ConfigureIface(args.IfName, result)`([bridge.go#L642](https://github.com/containernetworking/plugins/blob/main/plugins/main/bridge/bridge.go#L642)),进容器 netns 给 eth0 设上 IP、up 网卡、加默认路由。然后 `ensureAddr(br, ...)` 给 cni0 配网关 IP,`enableIPForward(...)` 开内核转发,可选地 `ip.SetupIPMasqForNetworks(...)` 加 MASQUERADE 规则。最后:

```go
return types.PrintResult(result, cniVersion)
```

把整个结果(IP、路由、DNS)JSON 化打到 stdout,完成 ADD。

> 把这五步串起来,bridge 插件的 `cmdAdd` 就是一份**"第 12 章手工组网步骤"的 Go 代码版**:
>
> 1. `setupBridge` = 创建 docker0/cni0 那台虚拟交换机
> 2. `setupVeth` = 创建 veth 对,容器端塞进 netns,宿主端插 bridge
> 3. `ipam.ExecAdd` = 找人分个 IP
> 4. `ConfigureIface` = `ip addr add` + `ip route add`
> 5. `ensureAddr` + `enableIPForward` + `SetupIPMasq` = 配网关 + 开转发 + 加 NAT
>
> **没有任何一步超出第 12 章讲过的范围**。CNI bridge 插件的全部神秘,就是"把手工命令封装成了符合标准接口的函数"。读完这段源码,你应该彻底相信:**容器网络没有黑科技,只有对内核已有能力的标准化封装**。

---

## 插曲:谁在调 CNI?——一个被改写的事实

讲到这里,有个绕不开的问题:**到底是谁,在什么时候,调用了这些 CNI 插件?**

很多人(包括很多老资料)会告诉你:是 **kubelet** 在调。kubelet 在创建 Pod 时,会找到 `/etc/cni/net.d/` 下的网络配置、`/opt/cni/bin/` 下的插件二进制,然后调 ADD 给 Pod 配网络。

**这个说法在今天(2026 年)已经不准确了**。这里有一个值得专门拎出来讲的架构变化,因为它直接关系到你怎么读源码、怎么排查问题。

### dockershim 时代:kubelet 直接调 CNI

在 Kubernetes 1.23 及更早版本,kubelet 里确实有一个目录 `pkg/kubelet/network/cni/`,里面的 `cni.go` 直接 import 了 `github.com/containernetworking/cni/libcni`,在创建 Pod 时调 `AddNetworkList` 给 Pod 配网络。kubelet 自己有两个标志:`--cni-bin-dir=/opt/cni/bin` 和 `--cni-conf-dir=/etc/cni/net.d`,用来指定插件和配置的位置。

那个时代,CNI 调用链是:

```
kubelet → (内置的 cni 库) → /opt/cni/bin/<插件> ADD
```

### 1.24 之后:CNI 调用下放给 CRI 运行时

这里要先把一个常见混淆说清楚:**Kubernetes 1.24(2022 年)移除的是 dockershim**——kubelet 里那个专门适配 docker 的内置运行时垫片(代码在曾经的 `pkg/kubelet/dockershim/`)。**不是**把 `network/cni/` 整个删了(很多人把这两件事记混了)。不过,正是 dockershim 的移除带动了一次分工重整:**CNI 调用的职责,从 kubelet 下放到了 CRI 运行时**。

那 CNI 现在由谁调?**由 CRI 容器运行时(containerd / CRI-O)来调**。这是容器生态"职责分层"的一次清晰化:kubelet 通过 CRI(Container Runtime Interface,gRPC 接口)跟容器运行时说话,它只说"给我起一个 Pod 沙箱"(RunPodSandbox);至于这个沙箱的网络怎么配,是容器运行时的事,运行时自己带 CNI 库、自己去找插件。dockershim 时代 kubelet 直接 import libcni、亲自驱动 CNI 的那套做法,就此退出了主线。

看 containerd 的真实代码。处理 `RunPodSandbox` 的主流程在 [internal/cri/server/sandbox_run.go](https://github.com/containerd/containerd/blob/main/internal/cri/server/sandbox_run.go),关键调用在两处:

```go
// 第 266 行:在创建好 sandbox 后,发起网络配置
if err := c.setupPodNetwork(ctx, &sandbox); err != nil { ... }

// 第 457 行起:setupPodNetwork 函数
func (c *criService) setupPodNetwork(...) {
	...
	// 第 502、504 行:真正调 CNI 的地方
	if c.config.CniConfig.NetworkPluginSetupSerially {
		result, err = netPlugin.SetupSerially(ctx, id, path, opts...)
	} else {
		result, err = netPlugin.Setup(ctx, id, path, opts...)
	}
	...
}
```

这里的 `netPlugin` 是一个接口,实现来自 containerd 自己的封装库 **go-cni**(`github.com/containerd/go-cni`)。go-cni 内部又 import 了 [containernetworking/cni/libcni](https://github.com/containernetworking/cni/blob/master/pkg/libcni/libcni.go)(看 [vendor/github.com/containerd/go-cni/cni.go](https://github.com/containerd/containerd/blob/main/vendor/github.com/containerd/go-cni/cni.go) 第 26 行的 `cnilibrary "github.com/containernetworking/cni/libcni"`),最终由 libcni 去 fork/exec 那个插件二进制。

所以今天真实的 CNI 调用链是:

```
kubelet
  → (CRI gRPC) RunPodSandbox
    → containerd: internal/cri/server/sandbox_run.go 的 setupPodNetwork
      → containerd 的 go-cni 库: netPlugin.Setup
        → containernetworking/cni 的 libcni
          → fork/exec /opt/cni/bin/<插件> ADD
```

> **为什么要做这次重构?** 因为 dockershim 时代 kubelet 既要调 docker、又要调 CNI、还要管一堆 docker 特有的怪癖,代码又乱又重。把 CNI 调用下放给 CRI 运行时后,**kubelet 只需要懂一种语言(CRI gRPC),网络、存储、容器生命周期这些细节都交给运行时各自处理**。这是"接口分层"的又一次胜利——和 CNI 自己把"网络怎么配"标准化是同一种哲学,只不过这次发生在 kubelet 和容器运行时之间。
>
> 第 10 章讲 containerd 时我们说过它是"高层运行时";第 19 章讲 kubelet 时会再细说这条 CRI 链路。这里你只需要记住:**今天的 CNI 调用者是容器运行时(containerd),不是 kubelet**。这个事实,很多老资料和面试题还在用 1.23 之前的说法,要注意辨别。

### 配置和插件还是那两个目录

虽然调用者变了,但 CNI 的**部署形态**没变。不管谁调,插件二进制还是放在 `/opt/cni/bin/`,网络配置还是放在 `/etc/cni/net.d/`。containerd 默认就认这两个路径(可以在 containerd 的配置文件 `[plugins."io.containerd.grpc.v1.cri".cni]` 段里改)。

`/etc/cni/net.d/` 下的配置文件按**文件名字典序**排序,字典序最小的那个就是**默认网络**。比如你有 `10-bridge.conf` 和 `20-calico.conf`,containerd 会先用 `10-bridge.conf` 配的主网络,再用 `20-calico.conf` 配的附加网络(这叫" chained CNI",多个插件按链式依次执行)。

> **排查 CNI 问题的入口**:遇到 Pod 一直 `ContainerCreating`、网络起不来,第一步去看 `/etc/cni/net.d/` 下的配置对不对、`/opt/cni/bin/` 下的插件在不在、containerd 日志里 setupPodNetwork 报了什么错。**这套排查路径,是今天每个 k8s 运维必备的肌肉记忆。**

---

## 章末小结

### 用航运比喻回顾本章

回到港口。这一章我们做的事情,是给上一章在码头内部修的那套转运通道,**定一个全球统一的标准插口**。

1. **CNI 不是一种网络,而是一套"插口标准"**。它规定了运行时怎么喊话(ADD/DEL/CHECK)、怎么传配置(stdin 的 JSON)、怎么收结果(stdout 的 JSON)。至于插口后面是 bridge、是隧道、是 BGP 路由——CNI 不关心,那是港口(网络厂商)自己的事。**标准化的只是"接口",而不是"实现"。**

2. **契约简单到出奇**。一个可执行文件 + 几个环境变量 + 一段 stdin JSON + 一段 stdout JSON,就是全部。没有 gRPC、没有 socket——把 Unix 几十年的"用文本流通信"老传统,套到了容器网络上。

3. **IPAM 被单独拆出来**。主插件管"网卡怎么连"、IPAM 管"IP 怎么分",两层正交,自由组合。这是"关注点分离"的干净落地。

4. **bridge 插件是第 12 章四块积木的标准封装**。它的 `cmdAdd` 五步——创建 bridge、拉 veth、委托 IPAM 分 IP、配 IP/路由、配网关/NAT——和上一章手工组网的步骤一字不差。**CNI 没有发明任何网络机制,它把已知的、手工的、分散的动作,标准化成了可插拔的单元。**

5. **三大流派的差别只在"跨机的包怎么搬"**:bridge 不搬(单机)、flannel 用隧道套封装、calico 用 BGP 让物理路由器自己会路由。但落到每台宿主内部,容器接出去的还是 veth,汇聚的还是 bridge,做翻译的还是 iptables。**CNI 标准化的只是插件接口,不是网络实现。**

### 本章在全书主线中的位置

回到全书的二分法:**打包隔离 vs 调度编排**。

- 本章和第 12 章一起,完成了**打包隔离**这一侧最后一块拼图:**让装箱隔离后的容器,能和外界、彼此之间标准化地通信**。从"一个容器怎么有网卡"(第 12 章)到"不同集群的网络怎么可插拔"(本章),容器网络这条线到此收束。
- 同时,本章是通往**调度编排**(k8s 半本)的桥梁。k8s 编排几万个容器,这些容器要互相发现、互相访问,底层全靠 CNI 把网络铺好。第 16 章讲 Pod 时你会看到——Pod 里所有容器共享同一个 network namespace,而这个 netns 的网络就是 CNI 在 Pod 创建时配的;第 20 章讲 Service 和 kube-proxy 时,那个"稳定门牌"盖在 Pod IP 之上,而 Pod IP 正是 CNI 分配的。**没有 CNI 这一层标准化,k8s 的网络模型根本立不起来。**

还有一个贯穿的回扣:**CNI 是"分层标准解耦"哲学的典范**。它和第 8 章的 OCI(运行时标准)、第 21 章的 CSI(存储标准)是同一招的三次复用——把可变的部分用一个稳定接口包起来,让上下游解耦。理解了 CNI,你就掌握了云原生生态能百花齐放的根本范式。

### 关键的"为什么"清单

如果你只能记六件事,记这六件:

1. **为什么网络要标准化成 CNI**:不然每换一种网络需求就得改运行时源码,生态会被组合爆炸淹没。标准化接口让运行时和网络实现彻底解耦,任何插件都能无缝替换。
2. **CNI 契约是什么**:一个可执行文件 + 环境变量(传命令和参数)+ stdin(传网络配置 JSON)+ stdout(返回结果 JSON)。ADD/DEL/CHECK/VERSION 是核心命令。没有 gRPC、没有 socket,纯 Unix 文本流。
3. **为什么用环境变量传命令而不是 argv**:env 传控制平面、stdin 传数据平面,职责分离;env 不依赖参数顺序、扩展向后兼容好。
4. **IPAM 为什么单独拆出来**:组网方式和 IP 分配是两个正交维度,拆开能自由组合(任何 main 插件 × 任何 IPAM)。委托机制让主插件嵌套调用 IPAM 插件。
5. **三大流派各自的取舍**:bridge 简单但只能单机;flannel(overlay)跨数据中心一把梭但有封装开销;calico(BGP)性能好但要底层能跑路由协议。它们的差别只在"跨机包怎么搬",落到单机全是上一章的 veth/bridge/iptables。
6. **谁在调 CNI(2026 年的真相)**:不是 kubelet——1.24 起 kubelet 不再直接管 CNI。调用者是 CRI 容器运行时(containerd / CRI-O),在处理 `RunPodSandbox` 时调。插件在 `/opt/cni/bin/`,配置在 `/etc/cni/net.d/`。

### 想继续深入,该往哪钻

- **亲手玩 CNI**:装个 kind 或 minikube,`ls /opt/cni/bin/` 看那一堆插件二进制(bridge、ptp、host-local、loopback、portmap、bandwidth……),`cat /etc/cni/net.d/*.conf` 看集群用的什么网络方案。
- **读 CNI 骨架源码**:[containernetworking/cni 的 pkg/skel/skel.go](https://github.com/containernetworking/cni/blob/master/pkg/skel/skel.go)——`CmdArgs`(L35-L45)、`CNIFuncs`、`pluginMain` 的 env 分发。文件不长,是理解 CNI 协议的最佳入口。
- **读 bridge 插件源码**:[containernetworking/plugins 的 plugins/main/bridge/bridge.go](https://github.com/containernetworking/plugins/blob/main/plugins/main/bridge/bridge.go)——`main`(L836-L844)、`cmdAdd`(L535-L762)、`cmdDel`(L771-L834)。配合 [pkg/ip/link_linux.go](https://github.com/containernetworking/plugins/blob/main/pkg/ip/link_linux.go) 的 `SetupVeth`(L172)看 veth 是怎么创建的。
- **读 host-local IPAM**:[plugins/ipam/host-local/backend/disk/backend.go](https://github.com/containernetworking/plugins/blob/main/plugins/ipam/host-local/backend/disk/backend.go) 的 `Reserve`(L47)——看 `os.O_EXCL|os.O_CREATE` 怎么用文件原子创建防 IP 重复。
- **读 containerd 怎么调 CNI**:[internal/cri/server/sandbox_run.go](https://github.com/containerd/containerd/blob/main/internal/cri/server/sandbox_run.go) 的 `setupPodNetwork`(L457)、`netPlugin.Setup`(L504);[vendor/github.com/containerd/go-cni/cni.go](https://github.com/containerd/containerd/blob/main/vendor/github.com/containerd/go-cni/cni.go) 看 go-cni 怎么 import libcni。
- **想看 calico/flannel 的跨机实现**:calico 的 BGP 模式可以去 [projectcalico/calico](https://github.com/projectcalico/calico) 看 `node` 里的 BGP 客户端;flannel 的 VXLAN 可以去 [flannel-io/flannel](https://github.com/flannel-io/flannel) 看 `backend/vxlan/`。它们都是合法的 CNI 插件,只是 `cmdAdd` 内部干的事比 bridge 复杂得多(建隧道、下发路由、跑协议)。

---

> 单机的网络积木我们封装成了标准插件,跨机的几种搬运策略也讲清了取舍。到这里,容器生态的"打包隔离"这一半——从 namespace/cgroup/rootfs,到镜像/运行时,再到网络——已经全部讲完。但故事还有另一半:当这种集装箱多到几万个、散布在几百台机器上,单机 docker 根本管不过来——故障自愈、扩缩容、跨机调度、服务发现,这些 docker 一个都解决不了。**旅程的下一个转折点到来了**:我们需要一套全新的范式,去编排几万个容器。翻开 **第 14 章 · 第一性原理:编排为什么必须存在**。
