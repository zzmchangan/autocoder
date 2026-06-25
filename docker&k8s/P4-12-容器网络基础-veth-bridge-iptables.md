# 第 12 章 · 容器网络基础:veth / bridge / iptables

> **前置**:你需要先读过[第 11 章《docker:让所有人都会用容器》](P3-11-docker-让所有人都会用容器.md)。那一章收尾时,我们说 docker 这半本已经讲到了顶——单机上,从镜像到运行时,docker 已经把"把一个应用装箱、隔离、跑起来"做成了开箱即用的一条命令。可一旦你真的敲下 `docker run nginx`,大概率会顺手再敲一句 `curl http://localhost:8080`,然后心里冒出一个新问题:**这个跑在容器里的 nginx,网卡是哪儿来的?它的 IP 谁给的?我浏览器敲的 `localhost:8080` 凭什么能打到容器里?** 这些问题 docker 那半本没回答——因为它底下是另一片天地:**容器网络**。从这一章起,我们进入第 4 篇。

> **核心问题**:容器怎么有自己的 IP 和网卡?它和宿主、和其他容器怎么通信?
>
> 顺带要回答三个相伴的子问题:
> - 容器明明和宿主共享同一个内核,凭什么它有自己的"网卡"、自己的 IP、自己的路由表?
> - 一台宿主上跑几十个容器,它们彼此怎么通信?又怎么访问外网?
> - `docker run -p 8080:80` 这个最常见的"端口映射",背后到底干了什么?
>
> **读完本章你会明白**:
> - **network namespace 是一座孤岛**:它给容器独立的协议栈和网卡,但默认谁也连不上——必须有人"架线"把它接出去。
> - **veth pair(一对虚拟网线)**怎么把容器接到宿主,**bridge(虚拟交换机)**怎么把多个容器连成一个局域网——这俩是 Linux 内核早就有、本就是给虚拟化用的网络积木。
> - **iptables NAT** 怎么让容器用宿主的 IP 上网(MASQUERADE/SNAT),又怎么把宿主端口"映射"进容器(DNAT)——`-p` 背后全是它。
> - **这一切没有一项是 docker 发明的**——容器网络没有造新硬件,它只是把内核里早就摆好的几块积木(network namespace、veth、bridge、iptables)拼成了一条路。

> **如果一读觉得太难**:先只记住三件事——① network namespace 给每个容器一座"孤岛协议栈",但孤岛默认不通外面;② veth 是一根虚拟网线、bridge 是一台虚拟交换机,把孤岛连起来就是个小局域网;③ 容器要上外网,得靠 iptables 在宿主这层做"地址翻译"(NAT),`-p` 端口映射也是 iptables 干的。本章信息密度大,看不懂细节时,这三句话兜底。

---

## 章首·一句话点破

第 1 章我们立下全书第一性原理:容器就是个普通进程,套上 namespace + cgroup 两件外套。其中 **network namespace** 是 8 种 namespace 里的一种(回扣第 2 章),它的作用,是把"网络"这件事整个隔离开——容器里看到的网卡、IP 地址、路由表、iptables 规则、端口占用,全是它自己的一套,和宿主互不相干。

可问题来了:**如果容器的网络是完全独立的一套,那它默认就是一座孤岛——它有自己的网卡,但那网卡连着什么都没有**。它 ping 不通宿主、ping 不通别的容器、更 ping 不通外网。

> **比喻**:回到我们的港口。第 1~3 篇我们在干一件事——把货物(应用)装进标准集装箱、用铁皮舱壁(namespace)把每个箱密封起来、装上船通电运作。可你想过没有:**每个集装箱被铁皮严严实实焊死后,里面的货物怎么和外头交流?** 一个完全密封、不接外面任何通道的集装箱,装在船上也是死的——它运货进来可以,但它内部的应用要连数据库、要被外部访问,根本无路可走。
>
> 所以集装箱之间、集装箱和码头之间,必须修**转运通道**。在航运里那是叉车、传送带、管道;在容器世界里,那就是 **veth(网线)+ bridge(交换机)+ iptables(地址翻译)**。第 4 篇的主题,就是"怎么给密封的集装箱修路"。

这一章先讲清楚单机上这条"路"是怎么修出来的——它是 CNI(下一章)的地基。我们先从孤岛说起。

---

## 一、network namespace:一座完全隔离的协议栈

### 不这样会怎样:不隔离网络的灾难

先回想一下"不隔离"有多糟。假设你直接在一个普通进程里监听 80 端口,而宿主上的 nginx 也在用 80——直接冲突,谁都起不来。再假设你跑了 20 个容器,每个都想用 80 端口(它们的程序就是这么写的),如果共享同一个网络栈,**第 2 个就启动不了**:端口已经被第 1 个占了。

更要命的是隔离性:共享网络栈意味着容器能看到宿主的所有网卡(`ip addr` 列出宿主的 eth0、docker0……)、看到宿主的连接表、改宿主的路由——一个容器网络配错,可能把整台宿主的网络搞瘫。

### 所以这样:network namespace 给每个容器一份独立的协议栈

network namespace 解决了这一切。一旦一个进程被 `CLONE_NEWNET` 标志位隔离进自己的 network namespace(回扣第 2 章和第 1 章的 `CloneFlags()`),它在网络世界里就成了一个"平行宇宙":

- **独立的网卡列表**:它只看到自己 namespace 里的网卡,看不到宿主的 eth0。
- **独立的 IP 地址**:它有自己的 IP,和宿主 IP 毫无关系。
- **独立的路由表**:它有自己的默认网关、自己的路由规则。
- **独立的端口空间**:它能监听 80 端口,和宿主或其他容器监听 80 互不冲突——因为"端口"是在各自的协议栈里记账的。
- **独立的 iptables 规则、独立的连接跟踪(conntrack)表**:它在自己的 namespace 里写的防火墙规则,只对自己生效。

这一层隔离,对应内核里的 [net/core/net_namespace.c](https://github.com/torvalds/linux/blob/master/net/core/net_namespace.c)——每个 network namespace 在内核里是一个 `struct net`,里面挂着属于它的网卡、路由表、规则表……所有"网络相关的状态"都按 namespace 分开记账。

> **回扣**:还记得第 2 章那张 `/proc/<pid>/ns/` 的图吗?里面那个 `net -> net:[4026532008]` 就是 network namespace 的 inode 号。两个进程的 `net` inode 相同,就在同一个网络世界里;不同,就互相看不见彼此的网卡。容器就是利用这一点,让每个容器活在自己的网络宇宙里。

### 但是:新建的 network namespace,默认是一座孤岛

隔离有了,代价也来了。**一个新建的、空的 network namespace,除了一个 `lo`(loopback,回环)网卡之外,什么都没有。**

- `lo` 只能和自己说话(`ping 127.0.0.1` 在容器内是通的,因为 lo 在);
- 但它没有连向宿主的网卡,没有连向外网的通路;
- 它甚至没有 IP(除了 `127.0.0.1`)。

这就是"孤岛"的处境:**容器内部自洽,但和外界完全断开**。你在容器里 `curl http://baidu.com`,直接报"网络不可达"。

> **比喻**:network namespace 就像一个把铁皮舱壁焊死、又没留任何出入口的集装箱。货物在箱里好好的,但箱里要发快递出不去、外面要送货进来也进不来。**修路,是接下来所有容器网络工作的核心。**

那么,怎么把这座孤岛接到外面?答案的第一块积木,叫 **veth**。

---

## 二、veth pair:一根打了个结的虚拟网线

### 它是什么:一对总是成对出现的虚拟网卡

veth(Virtual Ethernet)是 Linux 内核里一种特殊的虚拟网卡设备,定义在 [drivers/net/veth.c](https://github.com/torvalds/linux/blob/master/drivers/net/veth.c)。它的核心特性,一句话讲清:**veth 永远成对出现,从一端塞进去的数据包,会从另一端原样吐出来**。

你把它想成一根真实网线——但网线的两头,各自是一张"网卡"。你把这两张网卡分别放进两个 network namespace,这两个 namespace 就被这根"网线"连了起来。容器这边有一张网卡(常见名字 `eth0`),宿主那边有它的对端(一张对应的网卡),数据从容器 `eth0` 出去,就从宿主那张网卡进来。

### 为什么非得是"成对"的:不这样会怎样

为什么不直接给容器一张普通虚拟网卡,让它自己发数据?**因为"网卡"和"网线"是两回事**。一张网卡本身不会让数据凭空出现在另一个地方——你得有物理介质把数据搬过去。在真实世界里,那是双绞线/光纤;在内核里,veth 就是这根"虚拟的双绞线"。它的两头各是一张网卡(各自有自己的 MAC 地址、可以各自配 IP),但内核保证:**一边 send,另一边 receive**。

你只用一张 veth 是没意义的——它没有"另一端"接出去,数据发出去就石沉大海。所以 veth 必须是一对:一张在容器里,一张留在宿主(或别的地方),配成对才有意义。

### 看内核怎么实现"一端进、另一端出"

veth 的实现极其精炼,核心就在 [`veth_xmit`](https://github.com/torvalds/linux/blob/master/drivers/net/veth.c#L351-L422) 这个发送函数里——当容器往自己的 `eth0`(其实就是 veth 的一端)发数据包时,内核最终调用它:

```c
static netdev_tx_t veth_xmit(struct sk_buff *skb, struct net_device *dev)
{
	struct veth_priv *rcv_priv, *priv = netdev_priv(dev);
	struct veth_rq *rq = NULL;
	struct netdev_queue *txq;
	struct net_device *rcv;
	int length = skb->len;
	...
	rcu_read_lock();
	rcv = rcu_dereference(priv->peer);          // ← 关键:找到"对端"那张网卡
	if (unlikely(!rcv) || !pskb_may_pull(skb, ETH_HLEN)) {
		kfree_skb(skb);
		goto drop;
	}
	...
	ret = veth_forward_skb(rcv, skb, rq, use_napi);  // ← 把包递给对端网卡
	...
}
```

读这几行,注意两个点,它们把"成对网线"的精髓讲透了:

1. **`rcv = rcu_dereference(priv->peer)`**——veth 网卡的私有数据 `struct veth_priv` 里,有一个指针 `peer`,指向它的**对端**那张网卡([`struct veth_priv`](https://github.com/torvalds/linux/blob/master/drivers/net/veth.c#L78-L84))。这一行就是"顺着这根虚拟网线,找到另一头"。
2. **`veth_forward_skb(rcv, skb, ...)`**——拿到对端后,把数据包 `skb` 直接转交给对端网卡处理。**没有走任何真实的物理介质**——它就是把内核里的一个数据包对象,从这张网卡的发送队列,挪到了对端网卡的接收队列。在内核眼里,"网线传输"就是这么一次指针搬运。

> 这就是虚拟化的本质:**它没有模拟物理网线的电信号,它只是用内核数据结构的一个指针,达成了"一端进、另一端出"的语义**。从应用程序的角度看,跟真实网线没区别——数据发出去了,对端收到了;但对内核来说,这只是一次函数调用。veth 的高明,在于它用最朴素的方式(一个 peer 指针 + 一次 forward),复现了"网线"的全部行为。

### veth 怎么把容器接出去:一端进容器,一端留宿主

理解了"成对",容器联网的第一步就清晰了。给一个新容器接网络,通常这么干(这是 docker/CNI 在背后替你做的事):

1. 在宿主上创建一对 veth:`veth0` 和 `veth1`(它们是一对 peer)。
2. 把 `veth1` 这张网卡**移进**容器的 network namespace,改名为 `eth0`,给它配个 IP(比如 `172.17.0.2`)。
3. `veth0` 留在宿主上,作为容器对外的"接头"。

从这一刻起,容器就有了一条通向宿主的网线:容器往 `eth0` 发数据,数据就出现在宿主的 `veth0` 上;宿主往 `veth0` 发数据(路由合适的话),数据就出现在容器的 `eth0` 里。

> **把网卡"移进 namespace"这件事,代码上长什么样?** 真正的容器运行时 runc 在它的 `network_linux.go` 里有一个函数 [`devChangeNetNamespace`](../runc/libcontainer/network_linux.go#L108-L232),专门干"把一张网卡从一个 namespace 搬到另一个 namespace"的活。它通过 netlink 发一个 `RTM_NEWLINK` 消息,带上 `IFLA_NET_NS_FD`(目标 namespace 的文件描述符)和 `IFLA_IFNAME`(新名字),让内核原子地把网卡"过户"到目标 namespace,并在过户时顺便重命名。读完这段你会发现:**连"移动网卡"这么"虚拟化味儿"的操作,底下也是一次 netlink 系统调用**——容器没有魔法。

### 但是,只拉一根 veth 还不够

到这里容器有了"网线",但这根网线的宿主端 `veth0` 漏在宿主的网络栈里,谁也没和它连。**它只是一根悬空的线头**——容器的数据能到 `veth0`,但从 `veth0` 往外去哪儿?默认哪儿也不去(除非你手动配路由)。

更关键的是,一台宿主要跑几十个容器,你得拉几十根这样的 veth,宿主上就有几十个悬空的 `vethX` 网卡。这些线头要怎么组织、怎么让它们彼此互通?

答案的第二块积木,叫 **bridge**。

---

## 三、bridge:一台把所有容器连起来的虚拟交换机

### 它是什么:软件实现的二层交换机

bridge(网桥)是 Linux 内核里另一个早就存在的网络设备,源码在 [net/bridge/](https://github.com/torvalds/linux/tree/master/net/bridge)。你可以把它理解成**一台用软件模拟的二层交换机(switch)**。

真实的交换机干两件事:**学习**(记住每个端口后面是哪个 MAC 地址)、**转发**(收到包,看目标 MAC,送到对应端口;不认识就广播)。bridge 一模一样:它是一张虚拟网卡(有自己的 MAC 和 IP),但你还可以把别的网卡"插"到它上面(成为它的 port),bridge 就负责在所有 port 之间做 MAC 学习和转发。

### 不这样会怎样:没有 bridge 的混乱

假设没有 bridge,你有 10 个容器,每个容器的 veth 宿主端 `veth0`~`veth9` 都散在宿主的网络栈里。要让这 10 个容器互通,你得在宿主上**手动给每两个 veth 之间配路由或转发规则**——组合爆炸(45 条两两关系)。更要命的是,每来一个新容器,你得改所有已有容器的路由。

bridge 把这件事彻底简化:**把所有容器的 veth 宿主端,统统"插"到一个 bridge 上**。bridge 自己来做"哪个 MAC 在哪个端口"的学习和转发。容器之间通信,根本不用知道彼此在哪个 veth 上——它们只要把包丢给"默认网关"(就是 bridge 的 IP),bridge 自己搞定剩下的事。

> **比喻**:veth 是一根根"网线",一头插在容器上、另一头悬在宿主上。bridge 就是宿主上那台"虚拟交换机"——你把所有悬着的网线头,统统插进这台交换机。于是所有容器就处在同一个二层局域网里,可以彼此 ping 通。docker 默认网络就是这么干的:那台"虚拟交换机"在宿主上叫 `docker0`,所有容器的 veth 宿主端都插在它上面。

### 看内核怎么决定"转发还是广播"

bridge 作为一台交换机,最核心的逻辑是:**收到一个包,该往哪个端口送?** 这个决策在 [`br_handle_frame_finish`](https://github.com/torvalds/linux/blob/master/net/bridge/br_input.c) 里,关键几行:

```c
	...
	case BR_PKT_UNICAST:
		dst = br_fdb_find_rcu(br, eth_hdr(skb)->h_dest, vid);   // ← 查转发表(FDB):这个 MAC 在哪个端口?
		...
		break;
	...

	if (dst) {
		...
		br_forward(dst->dst, skb, local_rcv, false);             // ← 查到了:只往那一个端口送
	} else {
		if (!mcast_hit)
			br_flood(br, skb, pkt_type, local_rcv, false, vid);  // ← 没查到:广播给所有端口(flood)
		else
			br_multicast_flood(mdst, skb, brmctx, local_rcv, false);
	}
```

读这段,交换机的两大动作一清二楚:

1. **`br_fdb_find_rcu`**——查转发表(FDB,Forwarding DataBase)。bridge 会"学习":每当某个 port 上来了一个包,它就把"这个包的源 MAC ↔ 这个 port"记进 FDB。下次要找这个 MAC,直接查表就知道在哪个 port。这就是 `dst = br_fdb_find_rcu(...)` 这一行干的——**单播包先查表,查到了就精确转发**。
2. **查到了 `br_forward`,查不到 `br_flood`**——查到了,调 [`br_forward`](https://github.com/torvalds/linux/blob/master/net/bridge/br_forward.c)(把包只送给那一个目标 port);查不到,调 [`br_flood`](https://github.com/torvalds/linux/blob/master/net/bridge/br_forward.c)(把包复制一份,发给所有 port)。**这和真实交换机处理"未知单播"的行为完全一致**——不知道目标在哪,就先全广播一遍,总有一个 port 能命中。

> 这又一次印证全书主线:**bridge 没有发明任何新东西**。它就是用软件把一台真实二层交换机的"学习 + 转发/广播"两件事复现出来。`br_forward.c` 里那个 `br_flood` 函数,名字直白得可爱——它就是一个 `for` 循环,遍历 bridge 上所有 port,每个 port 都 `deliver_clone` 一份包。交换机的广播,在内核里就是这么一个 for 循环。

### docker0:默认 bridge 网络的全貌

把 veth 和 bridge 串起来,docker 的默认网络(`docker run` 不加 `--network` 时)就一目了然了:

```
                        宿主机 (host network namespace)
   ┌─────────────────────────────────────────────────────────────┐
   │                                                              │
   │   eth0 (真实网卡, IP 比如 192.168.1.10, 连外网)               │
   │       │                                                      │
   │   ┌───┴─────────────── docker0 (bridge, IP 172.17.0.1) ────┐ │
   │   │   (虚拟交换机)                                          │ │
   │   │     │          │          │                            │ │
   │   │   veth0a     veth0b     veth0c   ← 容器 veth 的宿主端   │ │
   │   └─────┼──────────┼──────────┼──────────────────────────-─┘ │
   │         │          │          │                              │
   └─────────┼──────────┼──────────┼──────────────────────────────┘
             │          │          │
        ╔════╧═══╗ ╔═══╧════╗ ╔═══╧════╗   ← network namespace 边界
        ║ 容器 A  ║ ║ 容器 B  ║ ║ 容器 C  ║
        ║ eth0   ║ ║ eth0   ║ ║ eth0   ║
        ║.17.0.2 ║ ║.17.0.3 ║ ║.17.0.4 ║
        ╚════════╝ ╚════════╝ ╚════════╝
```

- 三个容器,各自在独立的 network namespace 里,各有自己的 `eth0` 和 IP(`172.17.0.2/3/4`)。
- 每个容器的 `eth0` 都是一根 veth 的容器端;对应的宿主端 `veth0a/b/c` 全部"插"在 `docker0` 这台虚拟交换机上。
- **容器之间通信**:容器 A ping 容器 B,包从 A 的 eth0 出 → 宿主 veth0a 进 → docker0 查 FDB 发现目标是 veth0b → 转发到 veth0b → 容器 B 的 eth0 收到。**全程二层,不需要 NAT。**
- 容器 A 把 `docker0` 的 IP(`172.17.0.1`)配成自己的默认网关。

现在容器之间通了。可最后一个问题还没解决:**容器怎么访问外网?外部怎么访问容器?** 这就轮到 iptables NAT 上场。

---

## 四、iptables NAT:让孤岛和外界互通

### 不这样会怎样:容器 IP 出不了宿主

容器 A 想访问百度(`110.x.x.x`)。它发了个包,源 IP `172.17.0.2`,目标 IP `110.x.x.x`。这个包顺着默认网关到了 `docker0`,再被宿主内核往外路由,从宿主真实网卡 `eth0` 出去。

**问题来了:源 IP 是 `172.17.0.2`**。这是一个**私网 IP**,只在 docker0 这个虚拟局域网里有意义。一旦这个包到了宿主外面的真实网络,路由器看到"一个 `172.17.0.2` 主动来连我",它**根本不知道怎么回包**——因为 `172.17.0.2` 是 docker 私造的,公网上没人认。回包要么被路由器直接丢掉,要么就算回了,也找不到 `172.17.0.2` 在哪台宿主后面。

### 所以这样:MASQUERADE——出网时把源 IP 改成宿主 IP

解决办法叫 **SNAT(Source NAT,源地址翻译)**——在包从宿主 `eth0` 出去**之前**,把它的源 IP 从 `172.17.0.2` 改成宿主的 IP(`192.168.1.10`)。这样外面的世界看到的,就是"宿主这台机器在访问我",回包自然回到宿主;宿主再悄悄把它翻译回 `172.17.0.2`,送给容器。容器完全无感知,它以为自己是直接和外面通的。

docker 用的是 SNAT 的一种特殊形式,叫 **MASQUERADE**(伪装)。它的特别之处在于:**"改成哪个 IP"不是写死的,而是根据出口网卡动态决定**——这对动态 IP 的宿主(比如 DHCP 拿到 IP 的笔记本)特别友好。

这套机制是 Linux 内核的 **netfilter/iptables** 子系统提供的,源码在 [net/ipv4/netfilter/](https://github.com/torvalds/linux/tree/master/net/ipv4/netfilter) 和 [net/netfilter/](https://github.com/torvalds/linux/tree/master/net/netfilter)。它的核心是一个叫"hook"的机制:**内核在网络包流经的几个关键位置(进、出、转发等),埋了钩子,允许你注册回调函数去改包**。NAT 就是注册在这些钩子上的回调。

看 iptables 的 nat 表是怎么挂到 hook 上的,源码在 [net/ipv4/netfilter/iptable_nat.c](https://github.com/torvalds/linux/blob/master/net/ipv4/netfilter/iptable_nat.c):

```c
static const struct nf_hook_ops nf_nat_ipv4_ops[] = {
	{
		.hook		= ipt_do_table,
		.pf		= NFPROTO_IPV4,
		.hooknum	= NF_INET_PRE_ROUTING,      // ← 进来的包(此处做 DNAT)
		.priority	= NF_IP_PRI_NAT_DST,
	},
	{
		.hook		= ipt_do_table,
		.pf		= NFPROTO_IPV4,
		.hooknum	= NF_INET_POST_ROUTING,     // ← 出去的包(此处做 SNAT/MASQUERADE)
		.priority	= NF_IP_PRI_NAT_SRC,
	},
	...
};
```

读这个数组,关键看 `hooknum` 字段。nat 表的规则,挂在**两个时机**:

- **`NF_INET_POST_ROUTING`(包即将从网卡出去前)**:这是 **SNAT/MASQUERADE** 的位置——包马上要出门了,临走前把源 IP 改掉。容器出网就是走这里。
- **`NF_INET_PRE_ROUTING`(包刚进来、还没决定怎么路由前)**:这是 **DNAT** 的位置——包刚进门,趁早把目标 IP 改掉。下面讲 `-p` 端口映射时会再见到它。

而 MASQUERADE 这个 target,明确声明自己**只挂在 POST_ROUTING**:

```c
static struct xt_target masquerade_tg_reg[] __read_mostly = {
	{
		.name		= "MASQUERADE",
		.family		= NFPROTO_IPV4,
		.target		= masquerade_tg,
		...
		.table		= "nat",
		.hooks		= 1 << NF_INET_POST_ROUTING,    // ← 只在"包出去前"这个时机触发
		...
	},
};
```

(见 [net/netfilter/xt_MASQUERADE.c](https://github.com/torvalds/linux/blob/master/net/netfilter/xt_MASQUERADE.c)。)

读 `.hooks = 1 << NF_INET_POST_ROUTING` 这一行,你就明白:**MASQUERADE 的本质,就是"在包出门前的那一刻,把源 IP 翻译成出口网卡的 IP"**。容器出网,全靠这一行规则在 POST_ROUTING 钩子上"临门一脚"。

> **回扣"组合而非发明"**:iptables/netfilter 是 Linux 防火墙和网络地址翻译的核心子系统,1999 年就有了(比 docker 早十几年)。容器网络用 MASQUERADE 让容器上网,**用的就是这套早就摆在那儿的防火墙工具**,一条规则的事,没有任何"容器专用"的新机制。你 `iptables -t nat -L` 就能看到 docker 替你写好的那条 MASQUERADE 规则。

### 反过来:外部怎么访问容器?——DNAT 与端口映射

容器要访问外网(SNAT)解决了。可 `docker run -p 8080:80 nginx` 是反过来的需求:**外部想访问容器**。你浏览器敲 `http://宿主IP:8080`,这个请求打到的是**宿主**的 8080 端口——但 nginx 跑在容器里(容器 IP `172.17.0.2`,监听 80),宿主自己根本没在 8080 上起服务。这怎么打通?

答案是 **DNAT(Destination NAT,目标地址翻译)**。docker 在宿主的 iptables nat 表里写了一条规则,大致意思是:**"凡是从外面进来、目标是宿主的 8080 端口的包,把目标地址改成容器的 IP + 80 端口"**。这条规则挂在 `NF_INET_PRE_ROUTING` 钩子上(包刚进宿主、还没决定路由时),[`iptable_nat.c`](https://github.com/torvalds/linux/blob/master/net/ipv4/netfilter/iptable_nat.c) 那个数组里 `NF_INET_PRE_ROUTING` 对应的优先级 `NF_IP_PRI_NAT_DST` 就是干这个的。

于是数据流的旅程是这样的:

```
   你浏览器 → 宿主IP:8080 → 包进宿主 eth0
                                  │
                                  ▼  PRE_ROUTING 钩子,DNAT 规则触发:
                                  │  目标 IP: 宿主IP → 172.17.0.2
                                  │  目标端口: 8080 → 80
                                  ▼
                              内核按新目标路由 → 进 docker0
                                  │
                                  ▼
                              veth0a → 容器 eth0:80 → nginx 收到
                                  │
                              回包原路返回,POST_ROUTING 再把源地址翻译回去
                                  ▼
                              你看到 nginx 的页面
```

**`-p 8080:80` 背后,就是一条 DNAT 规则**。docker 帮你写好,你只管敲命令。

> **比喻**:DNAT 就像港口的"转运登记台"。外人不知道哪个集装箱在哪儿、内部编号是多少,他们只知道"我要找港口 X 号门头的货"。转运台把"X 号门头"翻译成"3 号堆场 5 号集装箱",外人照着走就行。回程时,转运台再把"3 号堆场 5 号集装箱"翻译回"X 号门头"发出去。外人全程只和门头打交道,集装箱内部编号对他是透明的——这正是 NAT 的精髓:**用一层地址翻译,把内部网络的细节藏在宿主身后**。

### 三块积木拼成的"路"

把这一章的三块积木拼起来,容器网络的完整图景就出来了:

| 积木 | 作用 | 比喻 | 在内核里 |
|---|---|---|---|
| **network namespace** | 给容器独立的协议栈(IP、路由、端口) | 密封的集装箱 | `net/core/net_namespace.c` |
| **veth pair** | 一对虚拟网线,把容器和宿主连起来 | 集装箱到码头的传输管道 | `drivers/net/veth.c` |
| **bridge** | 虚拟交换机,把所有容器连成局域网 | 码头上的分拨中心 | `net/bridge/` |
| **iptables NAT(MASQUERADE/DNAT)** | 地址翻译,让容器出网、让外部访问容器 | 转运登记台(改门头↔内部编号) | `net/ipv4/netfilter/`、`net/netfilter/` |

**没有一项是 docker 发明的。** veth、bridge、iptables,全都是 Linux 内核早就有的网络能力(它们本是为虚拟机网络、防火墙设计的)。docker / 容器网络做的事情,就是**把这四块积木按"给容器联网"的需求拼起来**——创一个 network namespace、拉一对 veth、把容器端塞进 namespace、把宿主端插到 bridge 上、写几条 iptables 规则。仅此而已。

> 这正是全书反复回扣的那条主线:**容器的全部精妙,在于把内核已有的能力组合、封装、产品化**。容器网络没有造新硬件,它造的是"组合方式"。

---

## 关键源码精读:veth 一根网线的全部秘密

讲完原理,我们把这一章最核心的一块积木——**veth**——在源码层面拆透。veth 是理解容器网络的最小单元:bridge 是它的"集合",iptables 是它的"邻居",但 veth 是那一根真正连接两个世界的线。看懂 veth,容器网络的地基就稳了。

### 1. veth 的"私事":对端指针就藏在 `struct veth_priv` 里

每张 veth 网卡,在内核里都是一个 `struct net_device`(它和 eth0、lo 这些网卡是同一套数据结构)。但 veth 有自己的"私事"——它要知道自己的**对端是谁**。这部分私有数据,挂在一个叫 `struct veth_priv` 的结构体里([drivers/net/veth.c](https://github.com/torvalds/linux/blob/master/drivers/net/veth.c#L78-L84)):

```c
struct veth_priv {
	struct net_device __rcu	*peer;       // ← 指向对端那张网卡
	atomic64_t		dropped;
	struct bpf_prog		*_xdp_prog;
	struct veth_rq		*rq;
	unsigned int		requested_headroom;
};
```

整个 veth 的"成对"机制,就建立在 `peer` 这一个指针上。**一张 veth 知道它的对端是谁,这就是"一对虚拟网线"在内核里的全部表示**——没有复杂的连接状态、没有协议握手,就一个指针。

### 2. veth 怎么被注册成一张"网卡":net_device_ops

veth 既然是网卡,它就得实现网卡该有的一套操作(打开、关闭、发送、统计……)。Linux 用一个 `struct net_device_ops` 来描述"一张网卡能干什么"。veth 的这一套操作,定义在 [`veth_netdev_ops`](https://github.com/torvalds/linux/blob/master/drivers/net/veth.c#L1705-L1724):

```c
static const struct net_device_ops veth_netdev_ops = {
	.ndo_init            = veth_dev_init,
	.ndo_open            = veth_open,
	.ndo_stop            = veth_close,
	.ndo_start_xmit      = veth_xmit,          // ← 关键:这张网卡的"发送"函数
	.ndo_get_stats64     = veth_get_stats64,
	.ndo_set_rx_mode     = veth_set_multicast_list,
	.ndo_set_mac_address = eth_mac_addr,
	...
};
```

最关键的一行是 `.ndo_start_xmit = veth_xmit`——这是**当有人往这张网卡发包时,内核会调用的发送函数**。容器里那个 `eth0`(其实是 veth 的一端)发包时,内核最终走到的就是这个 `veth_xmit`。其余几项都是常规网卡该有的(开关、统计、改 MAC),veth 复用了内核通用的实现。

### 3. 一根网线的灵魂:`veth_xmit` 怎么把包"送"到对端

现在看本章最核心的函数 [`veth_xmit`](https://github.com/torvalds/linux/blob/master/drivers/net/veth.c#L351-L422)——**它就是"虚拟网线"的物理定律**。我们把它精简到骨干:

```c
static netdev_tx_t veth_xmit(struct sk_buff *skb, struct net_device *dev)
{
	struct veth_priv *rcv_priv, *priv = netdev_priv(dev);
	struct net_device *rcv;
	...
	rcu_read_lock();
	rcv = rcu_dereference(priv->peer);              // ① 找到对端网卡
	if (unlikely(!rcv) || ...) {
		kfree_skb(skb);
		goto drop;
	}
	rcv_priv = netdev_priv(rcv);
	...
	ret = veth_forward_skb(rcv, skb, rq, use_napi); // ② 把包递给对端去"接收"
	switch (ret) {
	case NET_RX_SUCCESS:
		...
		break;
	...
	}
	rcu_read_unlock();
	return ret;
}
```

就两步,每一步都极其朴素:

**① `rcv = rcu_dereference(priv->peer)`——找到对端。** 用 RCU(读-复制-更新,内核的一种无锁读机制,回扣内核系列同步原语章)安全地读出 `peer` 指针,拿到对端那张网卡。这是"顺着虚拟网线找到另一头"。

**② `veth_forward_skb(rcv, skb, ...)`——把包递给对端。** `skb` 就是那个数据包(`sk_buff`,Linux 网络栈里数据包的统一表示)。这一步把 `skb` 交给对端网卡的接收路径处理——**对端就"收到"了这个包**,就像真实网线那头真的有信号进来一样。

> 你看,**"网线传输"在内核里,就是从一张网卡的发送函数,直接调到对端网卡的接收函数**。没有电信号、没有介质、没有握手——一次函数调用,数据包就从这边到了那边。这正是虚拟网络设备的高效之处:**它跳过了所有物理细节,直接在内核内存里搬运指针**。所以 veth 的性能可以非常高(没有真实硬件的开销,只有一次跨 CPU 的缓存同步)。
>
> 而且注意 `rcu_read_lock`/`rcu_dereference`——对端关系是可能动态变化的(比如容器销毁时 peer 被置空),用 RCU 保证读端无锁、写端安全。**虚拟网线的"物理定律",也要遵守内核的并发规则**。

### 4. veth 怎么成对创建:`veth_newlink`

最后看一眼"怎么造出一对 veth"。veth 不是用普通的网卡注册接口造的,它是通过 netlink 的 `rtnl_link` 机制创建的,入口是 [`veth_newlink`](https://github.com/torvalds/linux/blob/master/drivers/net/veth.c#L1815):

当你敲 `ip link add veth0 type veth peer name veth1` 时,内核走到的就是这个函数。它会一次性创建两张网卡(`dev` 和 `peer`),然后把它们的 `priv->peer` **互指对方**——这一步"互指",就是"成对"的真正落地点。两张网卡的 peer 指针对指,从此它们就是一根虚拟网线的两端。

读完 veth 这四段源码,容器网络的最小单元就彻底通透了:**veth = 两张网卡 + 两个互指的 peer 指针 + 一个把包从这边递到那边的 xmit 函数**。就这么简单,这么强大。

---

## 章末小结

### 用航运比喻回顾本章

回到港口。这一章我们做的事情,是给密封的集装箱**修转运通道**。

1. **network namespace 是密封的集装箱本身**——铁皮舱壁把货物的网络世界(网卡、IP、端口)整个焊死在里面,容器有自己的"网络宇宙"。但**一个焊死的箱,默认是孤岛**,内外不通。
2. **veth pair 是集装箱到码头的传输管道**——一头插在集装箱里(容器的 `eth0`),一头悬在码头(宿主的 `vethX`)。数据从一头进,另一头出,中间没有物理介质,就是内核里一次函数调用。
3. **bridge 是码头上的分拨中心**——把所有悬着的管道头都接进来,它自己学着"哪个管道后面是哪个集装箱"(MAC 学习),该转发的转发(`br_forward`),不认识的广播(`br_flood`)。于是所有集装箱处在同一个局域网里。
4. **iptables NAT 是转运登记台**——MASQUERADE 让集装箱往外发货时代签名(改成码头名,DNAT 让外面送货进来时改地址(门头号 ↔ 集装箱内部编号)。一层地址翻译,把集装箱网络的细节藏在码头身后。

四块积木拼起来,密封的集装箱就成了一个能内外通信的网络节点。**这四块积木,没一块是航运公司(容器)新发明的**——veth、bridge、iptables 都是码头(内核)早就有的设施,航运公司只是把它们组合成了一套标准的转运流程。

### 本章在全书主线中的位置

回到全书的二分法:**打包隔离 vs 调度编排**。

- 本章属于**打包隔离**这一侧——它是"把一个应用装箱、隔绝、限好量"之后,补上的最后一块:**让箱里箱外能通信**。没有网络,容器就是个自说自话的孤岛,装在船上也没用。
- 同时,本章为**调度编排**(k8s 半本)埋下了地基——k8s 编排几万个容器,这些容器要互相发现、互相访问,底层全靠这一章讲的网络原语。第 20 章讲 Service、kube-proxy 时,你会看到 iptables/ipvs 规则在更大的尺度上被批量编写——但它的根,还是这一章的 NAT 机制。

### 关键的"为什么"清单

如果你只能记五件事,记这五件:

1. **为什么 network namespace 默认是孤岛**:它给容器独立的协议栈,但新建的 namespace 除了 `lo` 什么都没有,必须有人"架线"把它接出去。
2. **为什么用 veth 而不是普通虚拟网卡**:网卡本身不会让数据出现在别处,得有"网线"。veth 是成对的虚拟网线,一端进另一端出,本质是内核里 peer 指针 + 一次 forward。
3. **为什么需要 bridge**:几十个容器的 veth 宿主端散在宿主上,两两互联组合爆炸。bridge 把它们全插到一台虚拟交换机上,用 MAC 学习 + 转发/广播自动搞定互通。
4. **容器为什么能上外网**:容器 IP 是私网地址,公网不认。MASQUERADE(SNAT)在包出门前把源 IP 改成宿主 IP,回包再翻译回去。**用的就是内核早就有的 iptables/netfilter**。
5. **`-p 端口映射`背后是什么**:DNAT——一条挂在 PRE_ROUTING 钩子上的 iptables 规则,把"宿主端口"翻译成"容器 IP + 端口"。docker 帮你写好,你只管敲命令。
6. **贯穿一切的主线**:容器网络没有发明任何新硬件。namespace、veth、bridge、iptables,全是 Linux 内核早有的能力(本为虚拟机网络和防火墙设计)。容器网络的高明,在于**组合**。

### 想继续深入,该往哪钻

- **亲手验证"四块积木"**:在装了 docker 的机器上 `docker run -d --name test nginx`,然后:
  - `docker inspect test` 看 `NetworkSettings.IPAddress`(就是容器 IP,大概率 `172.17.0.x`)。
  - `ip link` 在宿主上看,会看到一个 `vethXXXXXX` 网卡(宿主端),`docker0` 就是 bridge。
  - `iptables -t nat -L -n -v` 看 docker 写好的 MASQUERADE 和 DNAT 规则——本章讲的全在这儿。
  - `ip netns`(配合 `docker inspect` 拿到容器进程号,`/proc/<pid>/ns/net`)能看到容器的 network namespace inode,和宿主不同。
- **看 veth 源码**:[drivers/net/veth.c](https://github.com/torvalds/linux/blob/master/drivers/net/veth.c) 的 `veth_xmit`(本章精读的函数)、`veth_newlink`(创建一对 veth)。文件不长,是理解"虚拟网络设备"的最佳入门。
- **看 bridge 源码**:[net/bridge/br_input.c](https://github.com/torvalds/linux/blob/master/net/bridge/br_input.c) 的 `br_handle_frame_finish`(转发决策:查 FDB 还是 flood)、[net/bridge/br_forward.c](https://github.com/torvalds/linux/blob/master/net/bridge/br_forward.c) 的 `br_forward` 和 `br_flood`(单播转发 vs 广播)。
- **看 NAT 怎么挂 hook**:[net/ipv4/netfilter/iptable_nat.c](https://github.com/torvalds/linux/blob/master/net/ipv4/netfilter/iptable_nat.c) 看 nat 表挂在 PRE_ROUTING/POST_ROUTING;[net/netfilter/xt_MASQUERADE.c](https://github.com/torvalds/linux/blob/master/net/netfilter/xt_MASQUERADE.c) 看 MASQUERADE target 只挂 POST_ROUTING。
- **看 runc 怎么移动网卡**:[libcontainer/network_linux.go](../runc/libcontainer/network_linux.go) 的 `devChangeNetNamespace`——"把一张网卡从一个 namespace 搬到另一个"这件事的 netlink 实现。
- **想看内核网络栈全貌**:翻内核系列的《Linux 网络》卷,那里的 netfilter、bridge、net_namespace 是本章的根。本书只点了容器用到的部分。

---

> 单机上一个容器的网络,我们用 namespace + veth + bridge + iptables 拼通了。但真实的集群里,容器可能分布在几百台机器上,跨机的容器怎么通信?不同公司对网络的需求千差万别(有的要简单 bridge、有的要 BGP 路由、有的要 overlay 隧道)——总不能每换一种需求就重写一遍这套配置逻辑。于是容器网络被**标准化成了一个插件接口**。下一章,我们看 **CNI:容器网络接口**——它怎么把这四块积木封装成"插上就能用"的标准插件。翻开 **第 13 章 · CNI:容器网络接口**。
