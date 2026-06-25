# 第 2 章 · namespace:集装箱的铁皮舱壁

> **前置**:你需要先读过 [第 1 章 · 第一性原理:容器不是虚拟机](P0-01-第一性原理-容器不是虚拟机.md)——它把"容器 = 普通进程 + namespace + cgroup"这句话钉死了,并给你看了 namespace 在源码层面的样子:就是 `clone` 的几个 `CLONE_NEW*` 标志位。本章把那几个标志位**挨个拆开**——内核到底准备了哪几种 namespace、每种隔开了什么、`/proc/<pid>/ns/` 凭什么是观测它们的最佳窗口。这一章是后面 rootfs、网络、安全各章的公共地基。

> **核心问题**:同一个内核,怎么让两个进程"看见完全不同的世界"——各自的进程表、各自的网卡、各自的挂载点、各自的主机名?
>
> 第 1 章结尾留了一个钩子:容器隔离的本质,是创建进程时多带几个 `CLONE_NEW*` 标志位。但"几个"到底是几个?每种隔离的是什么?为什么是"clone 时带标志"这种设计、而不是另起一套"创容器"的 API?还有一个最反直觉的——user namespace 为什么能让"容器里的 root 等于宿主的 nobody"?
>
> **读完本章你会明白**:
> - 为什么需要 **8 种** namespace(只隔离进程表远远不够:网络、挂载点、主机名、IPC、用户、cgroup 视图、时间偏移,各自独立演化),以及它们各隔离了什么。
> - 为什么内核选择"复用 clone 的标志位"来制造隔离,而不是发明一套全新的"创建容器"系统调用——这是 Unix 设计哲学在容器上的体现。
> - `/proc/<pid>/ns/` 凭什么是理解 namespace 的最佳观测点:每种 namespace 一个符号链接,`readlink` 看 inode,同 inode = 同一个 namespace。
> - **user namespace 为什么特殊**:它能把"容器里的 uid 0"映射成"宿主的 uid 100000",是 rootless 容器和容器安全的基石——也是为什么 runc 建 namespace 时**user 必须排第一**。
> - "新建一个 namespace"(`CLONE_NEW*`)和"加入一个已有 namespace"(`setns`)的差别——这个差别正是 k8s Pod 的根基。

> **如果一读觉得太难**:先只记住三件事——① 容器隔离不是"一刀切",而是 8 种 namespace 各管一摊(pid/mount/net/uts/ipc/user/cgroup/time);② 制造隔离的方式就是 `clone` 时带 `CLONE_NEW*` 标志位,**容器没发明新东西**;③ `/proc/<pid>/ns/` 下一组符号链接能让你看清两个进程是否在同一个"舱"里。这三句话撑起整章。

---

## 章首·一个朴素的问题

第 1 章我们立住了一个事实:容器就是个普通进程,它的隔离来自 `clone` 的几个 `CLONE_NEW*` 标志位。但只要你在终端里敲过 `docker run`,就会冒出一个更具体的问题:

> 同一台机器、同一个内核,凭什么容器 A 里的进程 `ps aux` 只看到自己,容器 B 里的进程 `ip a` 看到的是完全不同的网卡,容器 C 里 `hostname` 输出的名字跟宿主不一样?

要回答这个问题,光知道"有几个标志位"还不够,得搞清楚:**这些标志位到底各自关上了哪几扇门。**

> **比喻**:namespace 之于容器,就是**集装箱的铁皮舱壁**。但真实的集装箱不止"四面铁皮"这么简单——它有**密封的舱壁**(挡住视线)、**独立的电源插座**(独立网络)、**独立的铭牌**(独立主机名)、**独立编号系统**(独立进程号)。一个集装箱的"隔离",是一组**功能各异的隔断**叠出来的,不是一刀切。

Linux 内核给"造隔离"准备了**8 种功能各异的隔断**——8 种 namespace。本章就把它们一个一个拆开,看每种挡住了什么、它们怎么组合成一个完整的"容器",以及为什么是这个数量。

---

## 一、先看清单:8 种 namespace 各管一摊

打开内核头文件 [include/uapi/linux/sched.h](https://github.com/torvalds/linux/blob/master/include/uapi/linux/sched.h),在那一堆 `CLONE_*` 标志位里,以 `CLONE_NEW` 开头的——也就是"给新进程新建一份某种隔离"的——正好 **8 个**:

```c
#define CLONE_NEWNS      0x00020000      /* New mount namespace group */
#define CLONE_NEWCGROUP  0x02000000      /* New cgroup namespace */
#define CLONE_NEWUTS     0x04000000      /* New utsname namespace */
#define CLONE_NEWIPC     0x08000000      /* New ipc namespace */
#define CLONE_NEWUSER    0x10000000      /* New user namespace */
#define CLONE_NEWPID     0x20000000      /* New pid namespace */
#define CLONE_NEWNET     0x40000000      /* New network namespace */
/* 注意 CLONE_NEWTIME 单独放在文件末尾,和 CSIGNAL 位段冲突: */
#define CLONE_NEWTIME    0x00000080      /* New time namespace */
```

(来源:[include/uapi/linux/sched.h](https://github.com/torvalds/linux/blob/master/include/uapi/linux/sched.h),前 7 个紧凑排在一起,`CLONE_NEWTIME` 因为位段和 `CSIGNAL` 冲突被单独挪到文件靠后位置。)

把这 8 个标志位翻译成"隔离了什么",就是下面这张**全书最常用的对照表**:

### 8 种 namespace 对照表

| namespace(中文) | `CLONE_NEW*` 标志 | 隔离了什么 | 进了它之后能看到的"假象" | 引入内核版本 |
|---|---|---|---|---|
| **mount**(挂载) | `CLONE_NEWNS` | 挂载点视图 | 自己 mount 的 U 盘,别的进程看不见 | 2002(最早的 namespace) |
| **uts**(主机名) | `CLONE_NEWUTS` | `hostname`、`domainname`、内核版本名 | 容器里改 hostname 不影响宿主 | 2006 |
| **ipc**(进程间通信) | `CLONE_NEWIPC` | System V 信号量、消息队列、共享内存 | 和别的容器的 IPC 互不可见 | 2006 |
| **pid**(进程号) | `CLONE_NEWPID` | 进程号空间 | 容器里的 1 号进程,在宿主上是另一个号 | 2006 |
| **net**(网络) | `CLONE_NEWNET` | 协议栈、网卡、路由表、iptables、端口 | 容器有自己的 `eth0`、自己的 `127.0.0.1` | 2009 |
| **user**(用户) | `CLONE_NEWUSER` | uid/gid、capability | 容器里的 root 在宿主上是个普通用户(见第五节) | 2013(最晚,也最特殊) |
| **cgroup**(cgroup 视图) | `CLONE_NEWCGROUP` | `/proc/self/cgroup` 里看到的 cgroup 路径 | 容器里看到的 cgroup 根,是宿主某个子目录 | 2016(4.6) |
| **time**(时间偏移) | `CLONE_NEWTIME` | `CLOCK_MONOTONIC`/`BOOTTIME` 的偏移 | 容器里"开机多久了"可以和宿主不一样 | 2020(5.6) |

读这张表时请记住:**namespace 隔离的不是"物理资源",而是"进程看到的视图"**——它让两个进程**对同一个内核,看到不同的"事实"**。网卡还是那张物理网卡(在内核里只有一份),但 net namespace 让容器以为自己有独立的网卡;进程还是宿主进程表里的那一行,但 pid namespace 让容器以为自己是 1 号。

> **比喻**:namespace 像是给集装箱里的货物**戴上不同的滤镜**——同一艘货轮(内核)、同一片海,但戴着 pid 滤镜的货只看到自己这边的进程编号,戴着 net 滤镜的货只看到自己的网卡。**世界没变,只是每个箱看到的"世界"不一样。**

### 不这样会怎样:为什么需要 8 种,不能一种搞定?

这是初学者最容易问的:**"为什么不能搞一种'全隔离' namespace,一次到位?"**

答案是,**内核 namespace 不是"一锤子设计"出来的,而是按需演化了 18 年**。从 2002 年第一种(mount)进内核,到 2020 年最后一种(time)进内核,每一种都是**被一个具体需求逼出来的**:

- 2002 年,有人想在 HPC 集群上给不同用户**各自的挂载点视图**(各 mount 各的)→ 诞生 mount namespace。
- 后来发现光隔离挂载不够,**主机名还串着**(改 hostname 全机生效)→ 2006 年加 uts namespace。
- 再后来发现 **System V 信号量还串着**(容器间能互相操作信号量)→ 同年加 ipc namespace。
- 集群环境里,**两个容器都觉得自己是 PID 1**(init 系统惯例)→ pid namespace。
- 网络隔离(每个容器要自己的 IP、自己的端口)→ 2009 年才搞定 net namespace(它最复杂,要隔离一整个协议栈)。
- 最特殊的 user namespace(让容器里的 root 安全)→ 2013 年,前面 6 种都成熟了好几年,它才进来——因为它的安全问题最难。
- cgroup namespace、time namespace 是更晚的精修,解决"容器看到宿主 cgroup 路径泄露""容器迁移后时间单调性"这类边角问题。

**每一种 namespace,都对应着一类"如果不隔离,就会出事"的资源**。把它们合在一起,才凑出了一个真正"自洽"的隔离世界——少了任何一种,都会留一道通向宿主的缝:

- 没有 mount namespace?容器改挂载点会改到宿主。
- 没有 uts namespace?容器改 hostname 改的是宿主的名字。
- 没有 ipc namespace?两个容器能用信号量互相捣乱。
- 没有 pid namespace?容器 `kill 1` 把宿主的 init 杀了。
- 没有 net namespace?两个容器抢同一组端口。
- 没有 user namespace?容器里的 root 就是宿主的 root(第 22 章安全章的重头戏)。

> 所以"8 种"不是拍脑袋的数字,是**18 年里一个一个补出来的**——每补一种,隔离世界就闭合一道缝。Docker 2013 年出来的时候,前 6 种 namespace 已经齐了;user namespace 那年刚进内核,Docker 早期根本不敢用(默认关掉),直到容器安全成为热点才慢慢启用。

---

## 二、为什么是"clone 时带标志位"这种设计

理解了"有哪些",下一个"为什么"更关键:**内核为什么用"clone 时带 CLONE_NEW* 标志位"这种方式来造隔离,而不是发明一套全新的"创建容器"系统调用?**

### 不这样会怎样:另起一套 API 会怎样?

假设内核设计者当初走了另一条路——专门发明一个 `create_container()` 系统调用,接受一堆参数(要不要隔离 pid、要不要隔离 net、要不要……)。这条路看起来"语义清晰",实际上会带来三个麻烦:

1. **API 爆炸**:每种隔离需求都要么单独一个系统调用(`create_pid_container`、`create_net_container`……),要么 `create_container` 的参数表越拉越长。**新加一种 namespace,就得改一次 API。**
2. **和已有的进程创建机制割裂**:`fork`/`clone` 是 Unix 几十年的根基,所有进程都是它生的。如果"容器进程"是另一套系统调用生的,那它和普通进程的关系就成了悬空的事——调度器要不要单独管?信号、退出、wait 怎么处理?**整个内核的进程模型都要为"容器"开一个特例。**
3. **组合性丧失**:你想"只隔离 pid,不隔离 net"(比如某些 sidecar 场景),或者"同时隔离 pid+net+mount 但不隔离 user"——在一套专门的 API 里,这些组合要么得枚举穷尽,要么得搞个复杂的子参数结构。而标志位天然支持任意组合。

### 所以这样设计:复用 clone,用标志位表达"要哪些隔离"

内核实际选择的路,**极其 Unix**:

> **"造一个隔离的进程"和"造一个普通进程",用的是同一个系统调用(`clone`)。区别只在于:你传了哪些 `CLONE_NEW*` 标志位。**

这背后的设计哲学是——**容器进程不是一种新的进程,它是普通进程戴上几个隔离滤镜**。这个认知,在第 1 章已经立过一次,这里再加深一次:

- 不传任何 `CLONE_NEW*`:就是个普通子进程,和父进程共享一切视图。
- 传 `CLONE_NEWPID`:子进程进一个新的 pid namespace,它(及其后代)看到自己从 PID 1 开始。
- 传 `CLONE_NEWPID | CLONE_NEWNET | CLONE_NEWNS | ...`:**一次调用,同时进好几个 namespace**——这就是一个"容器进程"。

为什么能"一次到位"?因为 `CLONE_NEW*` 标志位设计成**互不重叠的位**(`0x00020000`、`0x20000000`、`0x40000000`……),可以用按位或 `|` **任意组合**,再用一次 `clone` 调用传进去:

```c
/* 一个标准容器要同时隔离 mount+uts+ipc+pid+net+cgroup,一次 clone 全带上 */
flags = CLONE_NEWNS | CLONE_NEWUTS | CLONE_NEWIPC
      | CLONE_NEWPID | CLONE_NEWNET | CLONE_NEWCGROUP;
/* 把 flags 传给 clone/clone3,新进程就同时进了这几个 namespace */
```

**这种"用位图表达一组可组合的选项"是 Unix 系统调用一贯的精炼风格**(`open` 的 `O_RDONLY|O_CREAT`、`mmap` 的 `PROT_READ|PROT_WRITE` 都是同款)。它的好处在第 1 章已点过,这里再钉一次:

- **原子性**:一次 `clone` 同时进多个 namespace,避免了"先进 pid ns、还没进 net ns"这种**中间态的不一致**(中间态里进程可能已经能用旧的网络栈干点啥)。
- **可组合**:任何子集都能表达,新加一种 namespace 只需新加一个位,不动 API。
- **零特例**:容器进程在内核调度器、信号系统、wait 机制眼里,和普通进程**完全一样**——没有"容器专用的进程类型"。

### runc 干的事:把"用户要哪些 namespace"翻译成"clone 标志位"

第 1 章我们已经引过 runc 那个翻译函数 `CloneFlags()`,这里再贴一次,但要带上**它的全部上下文**——这次我们看清楚它读的配置长什么样。先看 runc 怎么描述一个 namespace([runc/libcontainer/configs/namespaces_linux.go](../runc/libcontainer/configs/namespaces_linux.go#L88-L95)):

```go
// Namespace defines configuration for each namespace.  It specifies an
// alternate path that is able to be joined via setns.
type Namespace struct {
    Type NamespaceType `json:"type"`
    Path string        `json:"path,omitempty"`
}

func (n *Namespace) GetPath(pid int) string {
    return fmt.Sprintf("/proc/%d/ns/%s", pid, NsName(n.Type))
}
```

读这两个定义,信息量很大:

1. **`Namespace` 只有两个字段**:`Type`(哪种 namespace)和 `Path`。一个 namespace 的配置,在 runc 眼里就这么朴素。
2. **`Path` 字段是关键**——注释明说"an alternate path that is able to be joined via setns"。它的含义:**`Path` 为空 = 这个容器要新建一个这种 namespace;`Path` 非空 = 这个容器要加入 `Path` 指向的那个已经存在的 namespace**。这一行字,埋了第五节"新建 vs 加入"的伏笔,也是 k8s Pod 的根基(第 16 章回扣)。
3. **`GetPath` 函数**拼出来的字符串是 `/proc/<pid>/ns/<name>`——`<name>` 由 `NsName()` 翻译(下面会用到)。这正是下一节"观测点"的来源。

再看 runc 怎么把"用户配置"翻译成"clone 标志位"([runc/libcontainer/configs/namespaces_syscall.go](../runc/libcontainer/configs/namespaces_syscall.go#L11-L33)):

```go
var namespaceInfo = map[NamespaceType]int{
    NEWNET:    unix.CLONE_NEWNET,
    NEWNS:     unix.CLONE_NEWNS,
    NEWUSER:   unix.CLONE_NEWUSER,
    NEWIPC:    unix.CLONE_NEWIPC,
    NEWUTS:    unix.CLONE_NEWUTS,
    NEWPID:    unix.CLONE_NEWPID,
    NEWCGROUP: unix.CLONE_NEWCGROUP,
    NEWTIME:   unix.CLONE_NEWTIME,
}

// CloneFlags parses the container's Namespaces options to set the correct
// flags on clone, unshare. This function returns flags only for new namespaces.
func (n *Namespaces) CloneFlags() uintptr {
    var flag int
    for _, v := range *n {
        if v.Path != "" {
            continue        // ← Path 非空 = 要"加入"已有 ns,不需要 CLONE_NEW*
        }
        flag |= namespaceInfo[v.Type]
    }
    return uintptr(flag)
}
```

左边 `namespaceInfo` 是一张**一一对应**的表:8 种 namespace 类型 ↔ 8 个 `CLONE_NEW*` 标志位(和内核头文件那 8 个 `#define` 完全对应)。右边 `CloneFlags()` 干的事:遍历容器配置,凡是要**新建**的(`Path` 为空),就把对应标志位**按位或**进去;凡是要**加入**已有的(`Path` 非空),跳过(后面用 `setns` 处理)。

> **就这几行**。容器运行时最核心的隔离逻辑,浓缩在一个 `map` 和一个 for 循环里。**没有任何神秘力量**——就是把"用户要哪些隔离"翻译成"clone 的标志位整数",再传给内核。这又一次印证第 1 章的铁证:容器 = 普通进程 + 标志位。

---

## 三、`/proc/<pid>/ns/`:理解 namespace 的最佳观测点

讲完"怎么造",还得讲"怎么看"。namespace 是个内核对象,看不见摸不着,**你怎么知道两个进程是不是在同一个 namespace 里?**

答案是 Linux 给的一个绝佳观测窗口:**`/proc/<pid>/ns/` 目录**。

### 这个目录长什么样

随便找一台 Linux 机器(哪怕没装 docker),敲:

```sh
$ ls -l /proc/self/ns/
total 0
lrwxrwxrwx ... cgroup -> cgroup:[4026531835]
lrwxrwxrwx ... ipc    -> ipc:[4026531839]
lrwxrwxrwx ... mnt    -> mnt:[4026531840]
lrwxrwxrwx ... net    -> net:[4026531992]
lrwxrwxrwx ... pid    -> pid:[4026531836]
lrwxrwxrwx ... pid_for_children -> pid:[4026531836]
lrwxrwxrwx ... time   -> time:[4026531834]
lrwxrwxrwx ... time_for_children -> time:[4026531834]
lrwxrwxrwx ... user   -> user:[4026531837]
lrwxrwxrwx ... uts    -> uts:[4026531838]
```

**每种 namespace 一个符号链接**,链接名就是这种 namespace 的短名(`net`、`mnt`、`pid`、`user`、`uts`、`ipc`、`cgroup`、`time`)。每个链接指向一个 `xxx:[数字]` 格式的"魔法路径"——**方括号里那个数字,就是内核给这个 namespace 实例分配的 inode 号**。

读这个目录,你会发现三件事:

1. **"短名"和"标志位"一一对应**。`net`↔`CLONE_NEWNET`、`mnt`↔`CLONE_NEWNS`……这个对应关系,内核里是固定的;在 runc 里由 `NsName()` 函数([runc/libcontainer/configs/namespaces_linux.go](../runc/libcontainer/configs/namespaces_linux.go#L27-L47))硬编码翻译。
2. **`pid_for_children` 和 `time_for_children` 多出来的两个链接**。这俩有点微妙——pid 和 time 这两种 namespace,对**已经存在的进程**是不生效的(它已经活在原来的 ns 里了),只对**未来 fork 出来的子进程**生效。所以内核额外暴露 `*_for_children`,表示"未来子进程将进入的那个 ns"。这是内核实现细节,初学可以先忽略。
3. **方括号里的数字,是判断"在不在同一个 namespace"的钥匙**。

### 关键技巧:看 inode 号,判断"是否同舱"

**两个进程,只要它们 `/proc/<pid>/ns/net` 的 inode 号相同,它们就在同一个 network namespace 里;不同,就在不同的。** 其他 7 种同理。

这怎么用?举几个实战场景:

**场景一:看一个容器进程都进了哪些 namespace。**

```sh
# 在宿主上找到容器进程的 PID(假设是 12345)
$ ls -l /proc/12345/ns/
# 把它的 ns inode 和宿主自己(/proc/self/ns/)的 inode 对比:
#   net 的 inode 不一样 → 容器进了独立的 network namespace
#   user 的 inode 不一样 → 容器进了独立的 user namespace
#   uts 的 inode 一样    → 容器没隔离 uts(罕见配置)
```

**场景二:验证 k8s Pod 里的容器共享 network namespace。**

Pod 里几个容器(主容器 + sidecar)按设计**共享同一个 net ns**。在 Pod 所在节点上,找到这两个容器的进程 PID,然后:

```sh
$ readlink /proc/<容器A的PID>/ns/net
net:[4026532188]
$ readlink /proc/<容器B的PID>/ns/net
net:[4026532188]    # ← inode 完全相同!说明它俩在同一个 net ns 里
```

**inode 相同 = 同一个内核 ns 对象 = 它俩能"看到"同一组网卡。** 这就是 Pod "共享网络"的真相——第 16 章会展开。

### 这个目录从哪来:内核里的 `proc_ns_operations`

你可能会问:**这些符号链接是哪儿冒出来的?为什么链接名恰好是 `net`/`mnt`/`pid`/...?**

答案是内核里每种 namespace 都注册了一个 `proc_ns_operations` 结构体,它有一个 `.name` 字段——**那个字段就是 `/proc/<pid>/ns/` 下链接的名字**。以 uts namespace 为例,看 [kernel/utsname.c](https://github.com/torvalds/linux/blob/master/kernel/utsname.c) 末尾:

```c
const struct proc_ns_operations utsns_operations = {
    .name       = "uts",       /* ← 这就是 /proc/<pid>/ns/uts 这个链接名的来源 */
    .get        = utsns_get,
    .put        = utsns_put,
    .install    = utsns_install,
    .owner      = utsns_owner,
};
```

这个结构体就是 uts namespace 的"户口"——内核靠它在 `/proc` 文件系统里自动生成 `/proc/<pid>/ns/uts` 这个链接,链接名取 `.name = "uts"`。`.install` 函数(`utsns_install`)则是 `setns` 加入这个 namespace 时内核回调的钩子(第五节会用到)。

8 种 namespace 各注册一个这样的结构体,所以 `/proc/<pid>/ns/` 下有 8(加 2 个 `_for_children`)个链接。**观测点的存在性、链接名的来源,在内核源码里清清楚楚。**

> **顺带一提**:`readlink` 看到的 `net:[4026531992]` 那个数字,是 namespace 对应内核对象的 **inode 号**(`ns_common.inum`)。它由内核统一分配、全局唯一,所以"两个进程的 inode 相等"是判断"同一个 ns"的可靠标准。这也是 `lsns`、`ip netns identify` 等工具背后的原理。

---

## 四、内核怎么"造"一个 namespace:`copy_namespaces`

到目前为止,我们说的都是"用户视角":`CLONE_NEW*` 标志位 → 进了一个新 ns。但**内核收到这个标志位之后,到底干了什么?** 这一节我们去内核里看一眼"造 namespace"的工厂。

### 入口:fork 路径上的 `copy_namespaces`

`clone` 系统调用最终走内核的 `fork` 路径(进程创建),在这条路径上,内核会调一个函数 `copy_namespaces`,它的职责就是**根据传进来的 `CLONE_NEW*` 标志位,决定要不要给新进程造新的 namespace**。它住在 [kernel/nsproxy.c](https://github.com/torvalds/linux/blob/master/kernel/nsproxy.c) 里:

```c
/*
 * called from clone.  This now handles copy for nsproxy and all
 * namespaces therein.
 */
int copy_namespaces(u64 flags, struct task_struct *tsk)
{
    struct nsproxy *old_ns = tsk->nsproxy;
    struct user_namespace *user_ns = task_cred_xxx(tsk, user_ns);
    struct nsproxy *new_ns;

    if (likely(!(flags & (CLONE_NS_ALL & ~CLONE_NEWUSER)))) {
        /* 没带任何 NEW 标志(除 NEWUSER 外)→ 直接复用父进程的 nsproxy */
        if ((flags & CLONE_VM) ||
            likely(old_ns->time_ns_for_children == old_ns->time_ns)) {
            get_nsproxy(old_ns);   /* 引用计数 +1 */
            return 0;
        }
    } else if (!ns_capable(user_ns, CAP_SYS_ADMIN))
        return -EPERM;             /* 带了 NEW 标志但要检查权限:得有 CAP_SYS_ADMIN */

    /* ... 一条针对 CLONE_NEWIPC + CLONE_SYSVSEM 互斥的检查 ... */

    new_ns = create_new_namespaces(flags, tsk, user_ns, tsk->fs);
    if (IS_ERR(new_ns))
        return PTR_ERR(new_ns);

    /* ... */

    nsproxy_ns_active_get(new_ns);
    tsk->nsproxy = new_ns;         /* ← 新进程挂上全新的 nsproxy */
    return 0;
}
```

读这个函数,内核的"造 ns 逻辑"清晰可见:

1. **没带任何 `CLONE_NEW*` 标志位** → 直接 `get_nsproxy(old_ns)` 把父进程的 `nsproxy` 引用计数 +1,新进程和父进程共享同一组 namespace。**这是 99% 的普通 fork 情况。**
2. **带了任何一个 `CLONE_NEW*`** → 先检查权限(`CAP_SYS_ADMIN`,造新 ns 是特权操作),然后调 `create_new_namespaces(flags, ...)` 造一组新的。
3. **造完之后** → `tsk->nsproxy = new_ns`,新进程的 `nsproxy` 指针换成新的。

### 关键数据结构:`nsproxy`——一个进程"当前的所有 namespace"

注意上面代码反复出现的 `nsproxy`——它是理解 namespace 的**核心数据结构**。每个进程的 `task_struct` 里有个 `nsproxy` 指针,指向一个 `struct nsproxy` 对象,**这个对象就是"该进程当前所在的全部 namespace 的集合"**。看它的定义([kernel/nsproxy.c](https://github.com/torvalds/linux/blob/master/kernel/nsproxy.c) 开头的 `init_nsproxy` 暴露了它的字段):

```c
struct nsproxy init_nsproxy = {
    .count              = REFCOUNT_INIT(1),
    .uts_ns             = &init_uts_ns,
    .ipc_ns             = &init_ipc_ns,
    .mnt_ns             = NULL,
    .pid_ns_for_children = &init_pid_ns,
    .net_ns             = &init_net,
    .cgroup_ns          = &init_cgroup_ns,
    .time_ns            = &init_time_ns,
    .time_ns_for_children = &init_time_ns,
};
```

`nsproxy` 就是**8 种 namespace 指针的容器**——一个进程站在哪种 uts、哪种 ipc、哪种 mnt、哪种 net……里,全在这个结构体的字段里。`init_nsproxy` 是内核启动时那个根 nsproxy(所有未隔离的进程共享它)。

> **这里有个重要细节**:`pid_ns_for_children` 和 `time_ns_for_children` 用的是 `_for_children` 后缀,而不是直接 `pid_ns`/`time_ns`。原因是 **pid 和 time 这两种 namespace 对"已经在跑的当前进程"不生效**——你 `clone(CLONE_NEWPID)` 出来的子进程,它**自己**的 PID 视角并不会马上变,只有当它**再 fork 孙进程**时,孙进程才会从 PID 1 开始数。所以 `nsproxy` 里有个"留给未来子进程的" pid ns 槽位。这就是为什么 `/proc/<pid>/ns/` 里多出来 `pid_for_children` 和 `time_for_children` 那两个链接——它们对应的就是这个槽位。

### 真正干活的:`create_new_namespaces` 一口气造 8 种

`copy_namespaces` 把活儿派给 `create_new_namespaces`,**这个函数才真正把 8 种 namespace 一个一个造出来**。看它([kernel/nsproxy.c](https://github.com/torvalds/linux/blob/master/kernel/nsproxy.c) 中段):

```c
static struct nsproxy *create_new_namespaces(u64 flags,
    struct task_struct *tsk, struct user_namespace *user_ns,
    struct fs_struct *new_fs)
{
    struct nsproxy *new_nsp;

    new_nsp = create_nsproxy();                          /* 分配一个空 nsproxy */
    if (!new_nsp)
        return ERR_PTR(-ENOMEM);

    new_nsp->mnt_ns = copy_mnt_ns(flags, ...);           /* ① mount ns */
    new_nsp->uts_ns = copy_utsname(flags, ...);          /* ② uts ns */
    new_nsp->ipc_ns = copy_ipcs(flags, ...);             /* ③ ipc ns */
    new_nsp->pid_ns_for_children = copy_pid_ns(flags, ...); /* ④ pid ns */
    new_nsp->cgroup_ns = copy_cgroup_ns(flags, ...);     /* ⑤ cgroup ns */
    new_nsp->net_ns = copy_net_ns(flags, ...);           /* ⑥ net ns */
    new_nsp->time_ns_for_children = copy_time_ns(flags, ...); /* ⑦ time ns */
    new_nsp->time_ns = get_time_ns(tsk->nsproxy->time_ns);

    /* ... 出错时逐个 put 回去(错误处理标签链) ... */
    return new_nsp;
}
```

**7 行赋值,把 7 种 ns 各自的 `copy_xxx_ns` 调一遍**(user namespace 不在这里,它单独由 cred 机制管,见第五节)。每种 ns 的 `copy_xxx_ns` 函数干的事都差不多——**看 flags 里有没有我这一位,有就 clone 一份新的,没有就引用旧的**。

以 uts namespace 为例,看 [kernel/utsname.c](https://github.com/torvalds/linux/blob/master/kernel/utsname.c) 里 `copy_utsname` 的核心:

```c
struct uts_namespace *copy_utsname(u64 flags,
    struct user_namespace *user_ns, struct uts_namespace *old_ns)
{
    struct uts_namespace *new_ns;

    BUG_ON(!old_ns);
    get_uts_ns(old_ns);

    if (!(flags & CLONE_NEWUTS))     /* ← 没带 CLONE_NEWUTS 标志 */
        return old_ns;               /* ← 就直接复用老的 uts ns */

    new_ns = clone_uts_ns(user_ns, old_ns);  /* ← 带了,才真正 clone 一份 */

    put_uts_ns(old_ns);
    return new_ns;
}
```

**这两行 `if (!(flags & CLONE_NEWUTS)) return old_ns;` 是整个 namespace 机制的精髓**:

- **没有 `CLONE_NEWUTS`** → 返回 `old_ns`,新进程和父进程共享同一个 uts ns(改 hostname 互相能看到)。
- **有 `CLONE_NEWUTS`** → 调 `clone_uts_ns` 造一份全新的(里面 `memcpy` 把老的 hostname 拷过来,但之后改了互不影响)。

`clone_uts_ns` 内部干的事很简单——分配新对象、引用计数 +1、`memcpy(&ns->name, &old_ns->name, sizeof(ns->name))` 把老的 hostname 等字段拷贝过来(所以**刚 clone 出来的 ns,内容和老的完全一样**,这就是为什么容器一开始 hostname 和宿主相同,要主动改才不同)。

> **这就是"标志位开关"在内核里的真实落地**。`CLONE_NEW*` 不是"造一个全新的隔离世界从零开始",而是"把父进程当前的 ns 内容**克隆一份**,从此父子各改各的"。所以**容器一开始看到的世界,是宿主世界的一份拷贝**——只是从这一刻起,它们各自演化,互不干扰。

7 种 ns 的 `copy_xxx_ns` 都是同款套路(`copy_mnt_ns`、`copy_ipcs`、`copy_pid_ns`、`copy_net_ns`、`copy_cgroup_ns`、`copy_time_ns`)。差异只在每种 ns "克隆"的具体内容不同(uts 拷 hostname、net 拷一份空的协议栈、pid 拷一份新的 pid 分配表……)。这个统一套路,就是 8 种 namespace 能用同一套 `CLONE_NEW*` 标志位驱动的根本原因。

---

## 五、user namespace:8 种里最特殊的一个

前面四节讲的 8 种 namespace,user 是其中**最特殊**的一种,值得单独一节。它特殊到:**它是唯一一种让"非 root 用户"也能创建的 namespace**,**它是容器安全(rootless 容器)的基石**,**它在内核里进来的时间最晚(2013,3.8)**,**而且 runc 建 namespace 时它必须排第一个**。

### 它干什么:user namespace 改写"我是谁"

先回顾前 7 种 namespace 干的事:它们隔离的都是**某种"视图"**——pid 视图、mount 视图、net 视图……进了一个新 ns,你看到的"世界"变了。

user namespace 不一样。它隔离的是**身份**:uid(用户 ID)、gid(组 ID)、capability(权限能力)。

> **比喻**:前 7 种 namespace 是给集装箱换"地图"(进程表、网卡、挂载点);**user namespace 是给货物换"身份证"**——同一个货物,在集装箱**外面**看是个普通搬运工(uid 100000),在集装箱**里面**看却是个**船长**(uid 0,root)。

### 不这样会怎样:没有 user namespace 的痛

要理解 user namespace 有多重要,先看**没有它**时的处境:

容器里的进程,常常需要以 **root(uid 0)** 跑——很多软件(nginx、postgres、各种 init 系统)默认假设自己是 root,要绑特权端口(<1024)、要改某些系统文件。但**宿主机上的 root,就是内核眼里最有权的用户**——如果容器里的 root 真的就是宿主的 root,那只要容器里有任何越界(配合一个内核漏洞,或者一个配置失误),它就能对宿主为所欲为:读 `/etc/shadow`、改内核参数、杀别的容器。

这就是**第 1 章埋的那个"共享内核 = 隔离不彻底"的痛**,在"身份"这一维上的具体表现:**容器里的 root,在没 user namespace 时,字面意义上等于宿主的 root。**

所以长期以来,Docker 默认**不敢用 user namespace**(它直到很晚才作为可选项启用)——所有容器都共用宿主的 user 视图,容器里 root = 宿主 root,只能靠"别让容器越界"来硬撑安全。

### 所以这样设计:uid 映射——容器里的 root,是宿主的普通人

user namespace 的解法极其巧妙:**它在容器内外之间,建立一张"uid 映射表"**。

- 在 user namespace **内部**:你是 uid 0(root),拥有所有 capability,看起来无所不能。
- 在 user namespace **外部**(宿主视角):你是 uid 100000(或随便一个映射区间),**就是宿主上一个毫无特权的普通用户**。

这种"内外两张皮"是通过写两个 `/proc` 文件实现的:`/proc/<pid>/uid_map` 和 `/proc/<pid>/gid_map`。这俩文件里写的就是映射规则,格式是三元组 `容器内起始uid  宿主起始uid  长度`,比如:

```
0  100000  65536
```

意思:**容器里的 uid 0~65535,映射到宿主的 uid 100000~165535**。容器里 `whoami` 说自己是 root(uid 0),宿主上 `ps` 看它 uid 是 100000——**同一进程,两个身份,看你在哪个 ns 里看**。

**这带来一个革命性的安全好处**:即便容器里的 root 配合内核漏洞"逃逸"到了宿主,它在宿主上仍然只是个 uid 100000 的普通用户——读不了 `/etc/shadow`、改不了内核参数、杀不了别人的进程。**隔离强度从"信任 root"提升到了"即便 root 逃逸也无害"。**

> 这是 rootless 容器(以非 root 用户运行整个容器引擎,如 Podman、rootless Docker)能存在的根本。第 22 章讲容器安全时,你会看到 user namespace 是整套容器安全的第一道、也是最重要的一道防线。

### runc 怎么写这张映射表

runc 配置 user namespace 时,干两件事:① 算出要新建一个 user ns(`CLONE_NEWUSER` 标志位,和别的 ns 一样);② 把 uid/gid 映射**写进 `/proc/<pid>/uid_map` 和 `/proc/<pid>/gid_map`**。

第二件事的写入点,在 runc 的 C 代码 [runc/libcontainer/nsenter/nsexec.c](../runc/libcontainer/nsenter/nsexec.c) 里(为什么用 C?因为要在 Go runtime 起来之前、刚 clone 出新进程的窗口里写):

```c
write_log(DEBUG, "update /proc/%d/uid_map to '%s'", pid, map);
if (write_file(map, map_len, "/proc/%d/uid_map", pid) < 0) {
    /* ... 出错处理 ... */
}
```

(来源:[runc/libcontainer/nsenter/nsexec.c#L271-L275](../runc/libcontainer/nsenter/nsexec.c#L271-L275)。同一函数稍后还会写 `gid_map`。)

注意这个**写入顺序的微妙**:必须**先**创建 user namespace(`clone(CLONE_NEWUSER)`),**再**写它自己的 `uid_map`/`gid_map`——因为只有"自己创建的 user ns 的父进程"才有资格写这个映射。而且,写完映射之前,新进程里的 root 还**没有真正生效**(它在父 ns 视角下是个 nobody)。

### 为什么 runc 把 user 排在第一位

回头看 runc 的 `NamespaceTypes()` 函数([runc/libcontainer/configs/namespaces_linux.go](../runc/libcontainer/configs/namespaces_linux.go#L73-L84)):

```go
func NamespaceTypes() []NamespaceType {
    return []NamespaceType{
        NEWUSER, // Keep user NS always first, don't move it.
        NEWIPC,
        NEWUTS,
        NEWNET,
        NEWPID,
        NEWNS,
        NEWCGROUP,
        NEWTIME,
    }
}
```

**注释直接写明:`Keep user NS always first, don't move it.`(user ns 永远排第一,别动)。** 为什么?

因为**其他 namespace 的创建,需要"在新 user ns 的身份下"才有正确的所有权**。具体说:你 `clone(CLONE_NEWUSER | CLONE_NEWNET)` 创建新 net ns,这个新 net ns 的**owner** 就是新 user ns。如果顺序搞反(先建 net ns 再建 user ns),net ns 的 owner 会落到父 user ns,导致权限关系错乱。

所以**user ns 必须最先建,其他 7 种在它之后建**,这样它们的 owner 才是新的、低权限的 user ns——这才能保证"容器里的所有 ns,整体归属于容器自己的(低权限)user ns",封堵"容器越权操作 ns"的路径。

> 这就是 user namespace 在 8 种里地位特殊的根子:**它不只是"再多隔离一种资源",它是给整个容器建立一个"低权限身份外壳"**。其他 7 种 ns 都活在这个外壳之下。这是容器安全的基石,第 22 章会再深入。

---

## 六、新建 vs 加入:`CLONE_NEW*` 和 `setns` 的区别

到这里,我们一直讲的是"新建一个 namespace"——容器启动时,从零造几个新 ns 出来。但 namespace 还有**另一种用法**:加入一个**已经存在**的 namespace。这两条路,在内核和 runc 里都是分开的。

### 不这样会怎样:为什么需要"加入"

设想 k8s 的 Pod 场景:一个 Pod 里有**两个容器**(主应用 + sidecar),它们要**共享同一个 network namespace**(这样 sidecar 能直接看到主应用的网卡、能 intercept 它的流量)。

如果只会"新建",怎么办?两个容器各自 `CLONE_NEWNET` 造两个独立的 net ns?那它们就互相看不到了,共享网络无从谈起。

**答案是**:其中一个容器**先**新建一个 net ns(`CLONE_NEWNET`),**另一个容器不新建,而是"加入"第一个容器造的那个 net ns**。

这种"加入已有 namespace"的操作,内核提供 `setns` 系统调用:

```c
int setns(int fd, int nstype);
```

`fd` 是一个**指向已有 namespace 的文件描述符**——还记得第三节说的 `/proc/<pid>/ns/net` 那个符号链接吗?打开它(`open("/proc/12345/ns/net")`)就拿到一个 fd,把这个 fd 传给 `setns`,**当前进程就挤进了 12345 那个进程的 network namespace**。

### 内核的 setns:加入是怎么发生的

`setns` 的内核实现也在 [kernel/nsproxy.c](https://github.com/torvalds/linux/blob/master/kernel/nsproxy.c) 末尾:

```c
SYSCALL_DEFINE2(setns, int, fd, int, flags)
{
    CLASS(fd, f)(fd);
    struct ns_common *ns = NULL;
    struct nsset nsset = {};
    int err = 0;

    if (fd_empty(f))
        return -EBADF;

    if (proc_ns_file(fd_file(f))) {
        ns = get_proc_ns(file_inode(fd_file(f)));  /* ← fd 是 /proc/<pid>/ns/xxx */
        if (flags && (ns->ns_type != flags))
            err = -EINVAL;
        flags = ns->ns_type;
    } else if (!IS_ERR(pidfd_pid(fd_file(f)))) {
        err = check_setns_flags(flags);            /* ← 也支持传 pidfd */
    } else {
        err = -EINVAL;
    }
    /* ... */

    err = prepare_nsset(flags, &nsset);            /* 准备一个新的 nsset 容器 */
    if (err)
        goto out;

    if (proc_ns_file(fd_file(f)))
        err = validate_ns(&nsset, ns);             /* 校验并 install 那个 ns */
    else
        err = validate_nsset(&nsset, pidfd_pid(fd_file(f)));
    if (!err) {
        commit_nsset(&nsset);                      /* ← 提交:切换当前进程的 nsproxy */
        perf_event_namespaces(current);
    }
    put_nsset(&nsset);
out:
    return err;
}
```

读这个函数,`setns` 的逻辑是:**打开 `/proc/<pid>/ns/xxx` 拿到 fd → 内核从 fd 反查出对应的 `ns_common` 对象 → 校验权限(每种 ns 的 `install` 钩子,比如前面见过的 `utsns_install`)→ 校验通过后 `commit_nsset` 把当前进程的 `nsproxy` 整体切换**。

**关键一句**:`commit_nsset` 里调的 `switch_task_namespaces(me, nsset->nsproxy)`——它**直接替换**了当前进程的 `task->nsproxy` 指针。从这一刻起,当前进程看到的"世界"就变了:它进入了目标 namespace。

注意 `setns` 和 `CLONE_NEW*` 的本质差别:

| 维度 | `CLONE_NEW*`(新建) | `setns`(加入) |
|---|---|---|
| 谁触发 | `clone`/`unshare` 时带标志位 | 显式调 `setns(fd, ...)` |
| 干了什么 | **造一个全新的 namespace 对象** | **引用一个已存在的 namespace 对象** |
| 内容来自 | 父进程当前 ns 的克隆 | 别的进程已经造好的那个 ns |
| 典型场景 | 容器**第一次**启动,造全新的世界 | **加入**别人造好的世界(共享网络、`docker exec` 进容器) |

### runc 的两条路:`newInitProcess` vs `newSetnsProcess`

这套"新建 vs 加入"的区分,在 runc 源码里是**两条完全分开的代码路径**。看 [runc/libcontainer/container_linux.go](../runc/libcontainer/container_linux.go#L645-L698):

```go
// 路A:新建——容器第一次启动,需要 clone(CLONE_NEW*) 造全新 ns
func (c *Container) newInitProcess(p *Process, cmd *exec.Cmd, comm *processComm) (*initProcess, error) {
    cmd.Env = append(cmd.Env, "_LIBCONTAINER_INITTYPE="+string(initStandard))
    nsMaps := make(map[configs.NamespaceType]string)
    for _, ns := range c.config.Namespaces {
        if ns.Path != "" {
            nsMaps[ns.Type] = ns.Path    // ← 要"加入"的 ns 记下来(走 setns)
        }
    }
    data, err := c.bootstrapData(c.config.Namespaces.CloneFlags(), nsMaps)
    //                          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    //                          CloneFlags() 算出要"新建"哪些(带 CLONE_NEW*)
    // ...
}

// 路B:加入——容器已经存在,docker exec 进去再跑一个进程
func (c *Container) newSetnsProcess(p *Process, cmd *exec.Cmd, comm *processComm) (*setnsProcess, error) {
    cmd.Env = append(cmd.Env, "_LIBCONTAINER_INITTYPE="+string(initSetns))
    state := c.currentState()
    // for setns process, we don't have to set cloneflags as the process namespaces
    // will only be set via setns syscall
    data, err := c.bootstrapData(0, state.NamespacePaths)
    //                         ^
    //                         注意:flags 传 0!完全不新建任何 ns,全部走 setns 加入
    // ...
}
```

**两条路的差别,在 `bootstrapData` 的第一个参数上赤裸裸地体现**:

- **路 A(新建,`newInitProcess`)**:传 `c.config.Namespaces.CloneFlags()`——第二节那个函数算出来的标志位整数。底层 C 代码拿这个整数去 `clone`,**新建**容器需要的所有 ns。
- **路 B(加入,`newSetnsProcess`)**:**传 `0`**——注释明说"我们不需要 cloneflags,因为进程的 namespace 全部通过 setns 系统调用来设置"。底层 C 代码不 `clone` 任何新 ns,而是**打开 `state.NamespacePaths` 里记录的那些 `/proc/<pid>/ns/xxx` 路径,逐个 `setns` 挤进去**。

> **(注释里那行 `// for setns process, we don't have to set cloneflags ...` 是 runc 源码的一句自白,直接证明了我们这一节讲的"两条路"区分。)**

### 这两条路的现实映射

把这两条路对应到日常操作:

- **`docker run nginx`** → 路 A。容器第一次启动,runc 调 `newInitProcess`,`clone(CLONE_NEWPID|CLONE_NEWNET|...)` 造一组全新的 ns,nginx 进程活在全新的世界里。
- **`docker exec -it <容器> bash`** → 路 B。容器已经在跑了(它的 ns 已经存在),你要再进去跑个 bash,bash 不需要造新 ns,而是 `setns` **加入**容器现有的所有 ns——所以 bash 看到的进程表、网卡、主机名,和容器里 nginx 看到的一模一样(因为它们在**同一组** ns 里)。
- **k8s Pod 的第二个容器(sidecar)** → 也是路 B 的变种。Pod 里第一个容器(通常是 pause 容器)先造好共享的 net ns,后续每个业务容器**不**新建 net ns,而是 `setns` 加入 pause 容器的那个 net ns——这就是 Pod"共享网络"的实现根基。

还记得第二节那张 `Namespace` 结构体的 `Path` 字段吗?它非空,就表示这个容器要 setns 加入那个路径指向的 ns;为空,就表示要 clone 新建。**这一个字段的两种取值,把 runc 推向两条完全不同的代码路径,也把容器世界分成"造世界的"和"进世界的"两类操作。**

> 第 16 章讲 Pod 时,你会再见到这个 `Path` 字段——Pod 里几个容器共享 network namespace,就是通过把这个字段指向同一个 `/proc/<pause容器pid>/ns/net` 实现的。**Pod 的根基,埋在 runc 这一个字段里。**

---

## 关键源码精读:从"标志位"到"8 个 ns 对象"的完整链路

把前面散落的源码点串起来,我们要回答最后一个问题:**用户敲下 `docker run`,从"几个配置项"到"内核里真有 8 个新 ns 对象",这条链路完整长什么样?** 这一节是本章的源码高潮,把内核 + runc 两边的真实代码合起来走一遍。

### 第 1 步:docker/containerd 把意图翻译成 runc 配置

`docker run` 经过 docker daemon → containerd,最终生成一份 OCI runtime-spec(JSON),里面有个 `linux.namespaces` 数组,长这样(简化):

```json
{
  "linux": {
    "namespaces": [
      {"type": "mount"},
      {"type": "uts"},
      {"type": "ipc"},
      {"type": "pid"},
      {"type": "network"},
      {"type": "cgroup"},
      {"type": "user", "path": "/proc/12345/ns/user"}
    ]
  }
}
```

每一项就是一个 namespace 配置——`type` 是哪种,`path`(可选)是要加入的已有 ns 路径。**没 path = 新建,有 path = 加入**。

### 第 2 步:runc 把配置翻译成 `Namespace` 结构体

runc 读这份 spec,转成内部配置。每个 namespace 转成一个 `Namespace` 结构体([runc/libcontainer/configs/namespaces_linux.go#L88-L91](../runc/libcontainer/configs/namespaces_linux.go#L88-L91)):

```go
type Namespace struct {
    Type NamespaceType `json:"type"`
    Path string        `json:"path,omitempty"`
}
```

一个容器的所有 namespace,凑成一个 `Namespaces`(就是 `[]Namespace` 的别名,[runc/libcontainer/configs/namespaces.go](../runc/libcontainer/configs/namespaces.go#L5)):

```go
type Namespaces []Namespace
```

### 第 3 步:`CloneFlags()` 算出要新建的标志位

runc 走"新建"路时(`newInitProcess`),调 `CloneFlags()`([runc/libcontainer/configs/namespaces_syscall.go#L24-L33](../runc/libcontainer/configs/namespaces_syscall.go#L24-L33))把"要新建的"翻译成 clone 标志位整数:

```go
func (n *Namespaces) CloneFlags() uintptr {
    var flag int
    for _, v := range *n {
        if v.Path != "" {
            continue        // 要加入的(Path 非空)跳过,不占标志位
        }
        flag |= namespaceInfo[v.Type]
    }
    return uintptr(flag)
}
```

比如上面那个配置(7 个要新建,user 要加入)→ `flag = CLONE_NEWNS|CLONE_NEWUTS|CLONE_NEWIPC|CLONE_NEWPID|CLONE_NEWNET|CLONE_NEWCGROUP`。

### 第 4 步:标志位传给底层,最终走 `clone`

`CloneFlags()` 返回的整数,经 `bootstrapData()` 序列化([runc/libcontainer/container_linux.go#L653](../runc/libcontainer/container_linux.go#L653))传给底层的 C 入口 `nsexec.c`(Go runtime 起来前执行)。`nsexec.c` 最终调 `clone(clone_flags, ...)`,**这一次调用就把"新建"的几种 ns 同时造出来**。

### 第 5 步:内核 `copy_namespaces` 派活,8 种 ns 各自 clone

内核收到 `clone`,走 fork 路径,调 [kernel/nsproxy.c](https://github.com/torvalds/linux/blob/master/kernel/nsproxy.c) 的 `copy_namespaces`,再派给 `create_new_namespaces`——后者**7 行赋值,7 个 `copy_xxx_ns` 各自检查自己那一位标志**,带了就 clone,没带就复用。每个 `copy_xxx_ns`(比如 uts 的 `copy_utsname`)的核心就两行:

```c
if (!(flags & CLONE_NEWUTS))
    return old_ns;          /* 没带 → 复用老的 */
new_ns = clone_uts_ns(user_ns, old_ns);   /* 带了 → clone 一份新的 */
```

`clone_uts_ns` 内部 `memcpy` 把老 ns 的内容(hostname 等)拷一份。**从此新进程看到的 uts ns 和父进程的相同初始内容、但互不影响**。

user namespace 单独走 cred 路径(因为身份是 cred 管的),由 [kernel/user_namespace.c](https://github.com/torvalds/linux/blob/master/kernel/user_namespace.c) 处理,但思路一致。

### 第 6 步:`/proc/<pid>/ns/` 立刻可观测

新进程起来后,它的 `/proc/<新pid>/ns/` 目录立刻生成(由 8 种 ns 各自注册的 `proc_ns_operations` 驱动,比如 uts 的 `utsns_operations` 里 `.name = "uts"`)。你 `readlink /proc/<新pid>/ns/*`,看到的就是这次刚 clone 出来的新 ns 的 inode 号——和宿主 `/proc/self/ns/*` 的 inode **不一样**,证明新进程确实在独立的世界里。

### 第 7 步(可选):setns 加入那些"要加入"的

如果配置里有 `Path` 非空的 namespace(比如上面例子里的 user),`nsexec.c` 还会**打开那个 Path 拿到 fd,调 `setns(fd, CLONE_NEWUSER)`** 把当前进程挤进去。内核的 `setns`([kernel/nsproxy.c](https://github.com/torvalds/linux/blob/master/kernel/nsproxy.c) 末尾的 `SYSCALL_DEFINE2(setns)`)走 `validate_nsset` → `commit_nsset`,替换当前进程的 `nsproxy` 里那一项。

---

把这条 7 步链路看完,你会发现:**从一条 `docker run` 命令,到内核里实实在在多出来 8 个 namespace 对象,中间没有任何"魔法"**。每一步都是普通的系统调用、普通的数据结构、普通的字段拷贝。**容器的隔离世界,就是这么朴素地搭起来的**——再一次印证第 1 章立的第一性原理。

---

## 章末小结

### 用航运比喻回顾本章

回到港口。第 1 章我们搞清楚了"集装箱(容器)就是个普通进程,套了铁皮箱"。这一章,我们**把铁皮箱拆开**,看清了它由**8 块功能各异的舱壁**拼成:

- **mount(挂载舱壁)**:挡住"别的箱的货架布局",各有各的挂载点视图。
- **uts(铭牌舱壁)**:挡住"船名",各有各的 hostname。
- **ipc(信号舱壁)**:挡住"别的箱的信号灯",各有各的 IPC。
- **pid(编号舱壁)**:挡住"别的箱的货物编号",各有各的 PID 1。
- **net(电路舱壁)**:挡住"别的箱的网卡和插座",各有各的协议栈。
- **user(身份证舱壁)**:挡住"我在船上的真实身份",**最特殊的一块**——让箱里的"船长"(root)在箱外只是个普通搬运工。
- **cgroup(配额牌舱壁)**:挡住"船上真实的配额台账",箱里看到的是自己的配额视图。
- **time(钟表舱壁)**:挡住"船的真实钟",箱里的"开机多久了"可以和船不一样。

这 8 块舱壁不是同时发明的——**18 年里(2002~2020),被一个个具体需求逼着、一道道缝补着,演化出来的**。每补一块,集装箱就闭合一道通向货轮(宿主内核)的缝。

而**造这些舱壁的方式,出奇地朴素**:不就是给造箱子的那台机器(`clone` 系统调用)多拨几个开关(`CLONE_NEW*` 标志位)。**集装箱没有任何专用的"造箱机",它就是用了造普通货物那台机器,多开了几个开关。**

观测这些舱壁也极简单:**每块舱壁在 `/proc/<pid>/ns/` 下都有一个"舱号牌"**(符号链接),`readlink` 看牌号(inode),牌号相同 = 在同一个舱里。**判断两个进程是不是同舱,一行 `readlink` 搞定。**

最特殊的那块舱壁——**user 舱壁——给整个集装箱套了个"低权限身份外壳"**:箱里的 root 在箱外是个 nobody,即便箱被攻破、货物逃到甲板上,它也只是个无权无能的普通货。这是容器安全的基石。

最后,舱壁有两种用法:**新建一块全新的(用 `CLONE_NEW*`)**,或者**挤进一块已经存在的(用 `setns`,通过 `/proc/<pid>/ns/xxx` 找到入口)**。前者是 `docker run` 造新容器,后者是 `docker exec` 进老容器、是 k8s Pod 里多个容器共享同一块网络舱壁。

### 本章在全书主线中的位置

记住全书的二分法:**打包隔离 vs 调度编排**。

这一章把**"打包隔离"这半边最核心的隔离能力**讲透了:

- 容器的隔离 = **8 种 namespace 的组合**,每种隔离一类"视图"(进程号、网卡、挂载点……)。
- 这 8 种 namespace 是**内核 18 年里演化出来的**,不是 Docker 发明,Docker 只是组合。
- 隔离的**机制**是 `clone` 的 `CLONE_NEW*` 标志位(第 1 章立的铁证,本章展开);**观测**靠 `/proc/<pid>/ns/`;**安全基石**是 user namespace;**复用**靠 setns(Pod 根基)。

但 namespace 只管**"看见什么"**,不管**"用多少"**。一个容器进了全套 namespace,它照样能 `while(1) fork()` 把宿主 CPU 吃光、能 `malloc` 直到 OOM。**隔离了视图,却没限资源——这是 namespace 的边界,也是下一章的入口。**

### 五个"为什么"清单

如果你只能记五件事,记这五件:

1. **为什么需要 8 种 namespace**:每种隔离一类"视图"(进程号/网卡/挂载点/主机名/IPC/用户/cgroup视图/时间),一种只挡一道缝。它们是内核 18 年里被具体需求一个个逼出来的,不是一次设计。少了任何一种,都会留通向宿主的缝。
2. **为什么用"clone 带标志位"而不是专门的"创容器"系统调用**:复用 clone 让容器进程在内核眼里和普通进程**完全一样**(调度/信号/wait 都不特例),标志位可任意组合、原子生效、新加一种 ns 只需加一位不动 API。这是 Unix 设计哲学的体现。
3. **`/proc/<pid>/ns/` 为什么是最佳观测点**:每种 ns 一个符号链接,链接名由内核里该 ns 注册的 `proc_ns_operations.name` 决定;`readlink` 看 inode,**同 inode = 同一个 ns 对象 = 在同一个舱里**。判断 Pod 内容器是否共享网络、判断 `docker exec` 是否进对了 ns,都靠它。
4. **user namespace 为什么特殊**:它隔离的是"身份"而非"视图"——容器里的 root(uid 0)在宿主上是 nobody(uid 100000),靠 `/proc/<pid>/uid_map` 写映射实现。这是 rootless 容器和容器安全的基石,也是 runc 为什么把 user ns 永远排在第一位(其他 ns 的 owner 要落在它之下)。
5. **"新建"(`CLONE_NEW*`)vs "加入"(`setns`)的区别**:前者造全新 ns、内容来自父进程 ns 的克隆;后者引用已存在的 ns、靠打开 `/proc/<pid>/ns/xxx` 拿 fd 再调 `setns`。runc 源码里这两条路完全分开(`newInitProcess` 传 CloneFlags / `newSetnsProcess` 传 0)。这个区别是 `docker exec` 和 k8s Pod 共享网络的根基。

### 想继续深入,该往哪钻

- **亲手观测 namespace**:在跑着容器的机器上,`ls -l /proc/<容器进程pid>/ns/`,对比 `/proc/self/ns/` 的 inode,亲眼看到容器进了哪些独立的 ns。再 `readlink` 两个同 Pod 容器的 `ns/net`,验证它们 inode 相同(共享网络)。
- **看内核的 namespace 工厂**:[kernel/nsproxy.c](https://github.com/torvalds/linux/blob/master/kernel/nsproxy.c) 的 `copy_namespaces`、`create_new_namespaces`、`SYSCALL_DEFINE2(setns)` 全在一个文件里——本章引用的核心代码都在这里,顺着读非常顺。再看 [kernel/utsname.c](https://github.com/torvalds/linux/blob/master/kernel/utsname.c) 的 `copy_utsname` + `clone_uts_ns` + `utsns_operations`,理解一种 ns 是怎么"克隆 + 注册观测点"的(其他 6 种套路相同)。
- **看 runc 的两条路**:[runc/libcontainer/container_linux.go#L645-L698](../runc/libcontainer/container_linux.go#L645-L698) 的 `newInitProcess` vs `newSetnsProcess`,以及 [runc/libcontainer/configs/namespaces_syscall.go#L24-L33](../runc/libcontainer/configs/namespaces_syscall.go#L24-L33) 的 `CloneFlags()`——这十几行代码就是"配置到标志位"的全部翻译逻辑。
- **亲手玩 user namespace**:Linux 上 `unshare --user --map-root-user bash`,体会"容器里 root = 宿主 nobody"——这是 rootless 容器的最小复现。

---

> 8 块舱壁装好了,集装箱里的货物再也看不到、碰不到别的箱了。**但有个洞还敞着**:舱壁只挡住了"视线",没挡住"手"——一个集装箱照样能把货轮的 CPU 跑满、把内存吃光,把别的集装箱活活挤死。**隔离了视图,还要限量。** 下一章,我们给集装箱装上**载重配额**——翻开 **第 3 章 · cgroup:给进程上配额**。
