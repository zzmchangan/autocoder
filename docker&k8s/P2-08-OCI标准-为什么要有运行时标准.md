# 第 8 章 · OCI 标准:为什么要有运行时标准

> **前置**:你需要先读过[第 7 章《镜像的本质:layer + manifest + config》](P2-07-镜像的本质-layer-manifest-config.md)——它讲清了一个 image 拆开看就是「一堆 layer + 一份 manifest + 一份 config」。本章要回答的是更上一层的问题:**凭什么这一堆东西全世界长一个样、谁都能读?为什么 Docker 愿意把自己最核心的格式和运行时,捐给一个公开标准?**

> **核心问题**:为什么 Docker 要把自己拆成「镜像格式标准 + 运行时标准」两套公开规范?
>
> 这一章我们暂时离开代码细节,问一个看起来"文绉绉"、实则关乎整个容器生态生死的问题:**如果每家厂商自己定义"镜像长什么样、容器怎么跑",世界会怎样?为什么 Docker 在 2015 年做了一个反直觉的决定——把自己最值钱的格式和运行时,白送给一个叫 OCI 的中立组织?**
>
> **读完本章你会明白**:
> - **没标准会怎样**:你的镜像只能用 Docker 跑,想换成 containerd、kata、gVisor 就得重打包;社区分裂成"Docker 镜像 / CoreOS 镜像 / 厂商 A 镜像"几套互不兼容的格式。
> - **OCI(Open Container Initiative)是什么**,Docker 在 2015 年成立它、捐出格式和 runc 的真实动机不是"做慈善",而是"生态要活下去"。
> - **image-spec(镜像长什么样)和 runtime-spec(怎么把镜像跑成容器)各管什么**,为什么必须切成两段、不能合二为一。
> - **low-level runtime(runc)vs high-level runtime(containerd)的分层**为什么这么切——runc 只管"按 spec 把一个容器跑起来",高层管镜像、生命周期、快照。
> - **config.json 是 runc 和 containerd 之间的"合同"**:这份 JSON 把"容器要建哪些 namespace、挂哪些目录、限多少 CPU/内存"一次性描述清楚,任何符合 OCI 的运行时都得照着它干。

> **逃生阀**:如果一读觉得太多名词,先只记住三件事——① OCI = 两套规范:image-spec 管"镜像长什么样",runtime-spec 管"怎么把镜像跑成容器";② runtime-spec 的核心是一份叫 `config.json` 的配置 + 一个叫 `rootfs` 的根目录,合称一个 **bundle**;③ **runc 是 runtime-spec 的参考实现**(它就是 Docker 当年捐出去的那个),它读 config.json、按里面写的去建 namespace、设 cgroup、跑进程。其余细节都是给这三件事做注脚。

---

## 章首 · 一句话点破

上一章我们把一个 image 拆成了 layer + manifest + config 三件套,你大概会觉得"这不就完了——Docker 自己定义这格式,大家照着用呗"。但现实里,这世上**不止 Docker 一家在做容器**。

2013 年 Docker 一炮走红之后,容器生态瞬间冒出一堆玩家:CoreOS(后来被 Red Hat 收购)做了自己的镜像格式和运行时、Google 有自己的容器基础设施、各家云厂商都有自己的私有方案……如果放任不管,**用不了两年,容器世界就会退化成 1990 年代的 Unix 战国时代**——你的镜像在我的运行时上跑不起来,我的容器在你的调度器上认不出来。

Docker 当年做了一个现在回头看极其关键的决定:**别打了,我们把格式和运行时定义捐出来,交给一个中立组织,大家一起定一个公开标准。** 这个组织,就是 OCI。

这一章,我们就来拆:**OCI 为什么必须存在?它把哪两件事标准化了?这份标准是怎么用一份 `config.json` 让 runc、containerd、kata、gVisor 这些完全不同的运行时,能跑同一个镜像的?**

---

## 一、没标准会怎样:厂商锁定与社区分裂

要理解"为什么要有标准",最直接的办法是反过来想:**如果没有标准,会发生什么?**

### 不这样会怎样:三场灾难

> **比喻**:想象航运业没有"标准集装箱"这回事。每家航运公司都用自己尺寸的箱子——马士基的箱长 22 英尺、中远的箱长 25 英尺、DHL 的箱长 18 英尺。后果是什么?**全世界的港口起重机都得做成可调的、码头堆场要为每家单独划区、卡车和火车根本没法通用**。一个货主一旦选了某家公司的箱子,就**绑定**了这一家——换公司等于重新打包。这就是**厂商锁定(vendor lock-in)**。

容器世界在 2014~2015 年正滑向这个深渊。具体会发生三件事:

**第一场灾难:镜像不可移植。** 你用 `docker build` 出来的镜像,格式是 Docker 私有的(虽然它公开了文档,但归 Docker 控制)。如果你哪天想换一个运行时——比如 CoreOS 当时推的 rkt(读作 "rocket"),或者想直接用更轻的 containerd——**你的镜像在那边跑不起来**,或者得经过一套转换。一个 `nginx` 镜像,Docker 的是这样、CoreOS 的是那样,你build 一次,得维护 N 份。

**第二场灾难:运行时只能绑定一家。** 容器"跑起来"这件事,当时全靠 Docker 自家的 `docker daemon`。这个 daemon 既负责镜像管理、又负责网络、又负责把容器真正跑起来——一坨巨型单体。你想要个更轻、更稳、能被 k8s 直接调用的运行时?没有,只能绕道 Docker。Kubernetes 早期不得不和 Docker 强绑定(通过一个叫 `dockershim` 的适配层),Docker 出任何问题,k8s 都得跟着抖。

**第三场灾难:创新被掐死。** 容器的隔离方式其实可以有很多种——有人想用普通 Linux namespace(Docker 这条路),有人想用轻量虚拟机(Kata Containers),有人想在用户态拦系统调用(gVisor)。但如果"运行时接口"被一家垄断,这些创新的运行时就**没有标准接口可对接**——k8s 没法同时支持 runc 和 kata,只能选一个。整个生态会被锁死在"Docker 怎么做,大家就跟着做"的状态。

> **一句话总结**:标准的本质,是**让"镜像"和"运行时"解耦**——同一个镜像,能被任何符合标准的运行时跑;同一个运行时,能跑任何符合标准的镜像。没有标准,这两者就是焊死的,生态没法分裂生长。

### 所以:把格式和运行时定义捐出来

Docker 看清了这件事。**2015 年 6 月**,在 Docker 的主导下,联合 CoreOS、Google、IBM、微软、AWS、Red Hat 等一票厂商,在 **Linux Foundation** 下面成立了一个中立组织:**Open Container Initiative(OCI)**。OCI 当下宣布了它要做的事,用 [OCI 官方说明](https://opencontainers.org/about/overview/)原文的意思转述就是:

> Docker 把它的容器格式和运行时 runC,捐给 OCI,作为这个新标准的基石。

这是一次看起来"自废武功"、实则高明的操作:

- **捐出来的不是"已经没用的东西"**——恰恰是 Docker 当时最核心的两样资产:镜像格式和运行时 runc。
- **换回来的是生态**。一旦格式和运行时是中立标准,**所有厂商都愿意围绕它做工具**:containerd、CRI-O、Podman、Buildah、kata、gVisor……它们可以放心地实现自己的运行时,不必担心"明天 Docker 改了格式,我就废了"。**Docker 捐出格式,换来的是整个生态以它定义的格式为地基继续扩张——它的地位反而更稳了。**

> **比喻**:这就好比某个国家率先把自己的铁路轨距标准捐给国际标准化组织。短期看,它"失去了"对轨距的垄断;长期看,**全世界都按它的轨距造火车、铺铁路**,它成了事实上的中心。Docker 把格式捐出去,赢得了"全世界都按它的格式打包镜像"——后来的 image-spec 几乎就是 Docker 镜像格式的标准化版本。

### OCI 干了三件事

OCI 成立后,把工作分成了三块(对外是两个规范 + 一个参考实现):

| OCI 的产出 | 管什么 | 对应航运比喻 |
|------|--------|--------------|
| **[image-spec](https://github.com/opencontainers/image-spec)**(镜像规范) | 一个 image 文件长什么样:layer 怎么叠、manifest 怎么写、config 怎么存、怎么内容寻址 | **标准集装箱的尺寸与结构规范**——箱体多长多宽、怎么封口、铭牌写什么 |
| **[runtime-spec](https://github.com/opencontainers/runtime-spec)**(运行时规范) | 怎么把一个镜像**跑成**一个容器:一份 `config.json` + 一个 `rootfs`,描述容器要建哪些 namespace、挂哪些目录、限多少资源 | **码头起重机怎么吊一个标准箱、通电、固定**的作业流程规范 |
| **runc**(参考实现) | runtime-spec 的一个**官方参考实现**——你照着规范写一个也行,但 runc 是大家公认、用得最广的那个 | **一台符合规范的起重机实物样机** |

注意一个关键点:**OCI 没有规定"你必须用 runc"**。它只规定了"runtime-spec 长什么样"。runc 是这个 spec 的参考实现,但 kata、gVisor、crun(C 语言版)这些**都实现了同一个 spec**,它们之间理论上能跑同一个 bundle。这正是标准的力量——**实现可以换,接口不动**。

---

## 二、为什么是两套规范,而不是一套

你可能会问:**"镜像长什么样"和"怎么把镜像跑成容器"——这两件事就不能合并成一个规范吗?** 这是个好问题,OCI 把它们拆开,不是随意的。

### 不这样会怎样:合并会怎样

假设只有一个"大一统"规范,既规定镜像格式、又规定运行时行为。看上去简洁,实则带来一个致命问题:**镜像的存储/分发 和 容器的执行,演化节奏完全不同**。

- **镜像侧**(image-spec)关心的是:层怎么压、怎么传、怎么去重、怎么签名。它面向的是**仓库、网络、内容寻址**这些事,跟着存储/网络技术的演进在变。
- **运行时侧**(runtime-spec)关心的是:这个进程要进哪些 namespace、挂哪些 cgroup、跑什么命令。它面向的是**操作系统内核能力**,跟着内核(8 种 namespace、cgroup v2、seccomp)的演进而变。

如果合二为一,任何一个改动(比如 cgroup v2 上线)都得拉动整个镜像规范的版本号,镜像仓库和运行时两边都得跟着升级。**这就把两个本可独立演化的东西焊死了。**

更重要的:**两段规范之间天然有个分界线**——**"打包成镜像"和"把镜像 unpack 成 rootfs"**。一旦镜像被解包成一个 `rootfs` 目录 + 一份运行配置,接下来的事**就和"这个 rootfs 当初是 docker build 出来的还是 buildah build 出来的"完全无关了**。runtime-spec 只认 bundle,不认镜像。

### 所以:image-spec 输出 bundle,runtime-spec 消费 bundle

OCI 的真正高明之处,是定义了一个**清晰的接口边界**:

> **image-spec(镜像)→ unpack → runtime-spec 的 bundle(config.json + rootfs)→ runtime 执行**

```
    ┌──────────── image-spec 的地盘 ────────────┐    ┌──── runtime-spec 的地盘 ────┐
    │                                            │    │                              │
    镜像仓库 (registry)                          │    │                              │
        │ docker pull                            │    │                              │
        ▼                                        │    │                              │
    manifest + 一堆 layer (tar.gz,内容寻址)      │    │                              │
        │ containerd/镜像解包工具 unpack         │    │                              │
        ▼                                        │    │                              │
    ┌─────────────────────┐                      │    │                              │
    │ rootfs 目录          │ ← 用 overlayfs 把层叠成一个目录   │                              │
    │ config.json         │ ← 由镜像里的 config 字段翻译过来   │                              │
    └─────────────────────┘                      │    │                              │
        │                                        │    │                              │
        ▼                                        │    │                              │
    【这就是一个 OCI bundle】─────────────────────┼───▶│                              │
                                                     │  runc / kata / gVisor 读它    │
                                                     │  建 namespace、设 cgroup、跑   │
                                                     │                              │
                                                     └──────────────────────────────┘
```

这条边界至关重要。它意味着:

- **镜像仓库(registry)只认 image-spec**。它存的是 manifest + layer,根本不关心这些层将来被谁跑。
- **运行时只认 runtime-spec 的 bundle**。它不关心 rootfs 当初是哪家的镜像、用什么打的。给它 `config.json` + `rootfs`,它就能跑。

两段规范通过 **bundle 这个产物**解耦。这就是"两套规范"的根本原因——它们解决的是容器生命周期的**两个不同阶段**,各自独立演化,通过 bundle 衔接。

> **航运比喻**:image-spec 是"集装箱的制造规范"——钢铁厂(镜像构建工具)按它造箱子,堆场(registry)按它堆箱子。runtime-spec 是"码头起重机的作业规范"——起重机(runc)按它吊箱、通电、固定。**起重机不在乎这个箱是哪个工厂造的,只在乎它符合尺寸规范**;**钢铁厂也不在乎箱最后被哪台起重机吊,只在乎它造出来的箱符合规范**。中间的衔接,就是"标准箱"这个产物本身。

---

## 三、runtime-spec 的核心:一份 config.json + 一个 rootfs

现在我们钻进 runtime-spec 的内部。它定义的最核心概念叫 **bundle**(文件系统束),用 [runtime-spec 的 bundle.md](https://github.com/opencontainers/runtime-spec/blob/master/bundle.md) 原文说,一个 bundle 就是一个目录,里面**只必须**包含两样东西:

1. **`config.json`**——配置文件,**必须**叫这个名字,**必须**放在 bundle 目录的根。它描述了"这个容器要怎么跑"。
2. **容器的 root filesystem**——一个目录,路径由 `config.json` 里的 `root.path` 指定(通常叫 `rootfs/`)。

> bundle.md 还特别强调了一句:**bundle 目录本身不属于 bundle**——意思是如果你把这个目录打成一个 tar 包,`config.json` 和 `rootfs/` 应该在 tar 的根上,而不是嵌在某个顶层目录里。这个细节在自动化处理 bundle 时很重要。

所以,**runtime-spec 的全部精华,几乎都浓缩在 `config.json` 这一个文件里**。它就是"这个容器长什么样"的完整描述。

### config.json 长什么样:真实的结构

我们不去抄一堆字段表(那是 spec 文档的活),而是看 [runtime-spec 的 specs-go/config.go](https://github.com/opencontainers/runtime-spec/blob/master/specs-go/config.go) 里 `Spec` 这个结构体的真实定义——它就是 `config.json` 反序列化后的 Go 对象。挑最关键的几个字段看:

```go
// Spec is the base configuration for the container.
type Spec struct {
	// Version ... OCI Runtime Spec 版本号
	Version string `json:"ociVersion"`
	// Process configures the container process.   ← 容器要跑什么进程
	Process *Process `json:"process,omitempty"`
	// Root configures the container's root filesystem.   ← rootfs 在哪、是否只读
	Root *Root `json:"root,omitempty"`
	// Hostname ...
	Hostname string `json:"hostname,omitempty"`
	// Mounts configures additional mounts (on top of Root).   ← 要挂哪些目录(/proc /dev ...)
	Mounts []Mount `json:"mounts,omitempty"`
	// Hooks configures callbacks for container lifecycle events.
	Hooks *Hooks `json:"hooks,omitempty"`
	// Annotations contains arbitrary metadata for the container.
	Annotations map[string]string `json:"annotations,omitempty"`

	// Linux is platform-specific configuration for Linux based containers.
	Linux *Linux `json:"linux,omitempty"`     // ← Linux 专属:namespaces/cgroups/seccomp 全在这里
	// ...Windows/Solaris/VM 等其他平台
}
```

注意几个关键点:

1. **`Root`** 字段只管"rootfs 的路径 + 是否只读",不关心 rootfs 怎么来的。
2. **`Process`** 字段描述容器要跑的进程:启动命令 `Args`、环境变量 `Env`、工作目录 `Cwd`、用户、capabilities……
3. **`Linux`** 字段是 Linux 平台专属的大块头——容器隔离与限资源的**全部细节都在这里**:`Namespaces`(要建/加入哪些 namespace)、`Resources`(cgroup 限额)、`Seccomp`、`Devices`、`Sysctl`……

我们再深入看 `Linux` 这个结构体(它承载了容器作为容器的核心):

```go
type Linux struct {
	// UIDMapping / GIDMapping ... user namespace 的 uid/gid 映射
	UIDMappings []LinuxIDMapping `json:"uidMappings,omitempty"`
	GIDMappings []LinuxIDMapping `json:"gidMappings,omitempty"`
	// Sysctl are a set of key value pairs that are set for the container on start
	Sysctl map[string]string `json:"sysctl,omitempty"`
	// Resources contain cgroup information for handling resource constraints
	Resources *LinuxResources `json:"resources,omitempty"`     // ← cgroup 限额
	// CgroupsPath specifies the path to cgroups that are created and/or joined
	CgroupsPath string `json:"cgroupsPath,omitempty"`
	// Namespaces contains the namespaces that are created and/or joined
	Namespaces []LinuxNamespace `json:"namespaces,omitempty"`   // ← 要哪些 namespace
	// Devices ... Seccomp ... MaskedPaths ... ReadonlyPaths ...
}
```

读到这里,你应该有一种**"原来如此"**的感觉:**`config.json` 的 `linux.namespaces` 字段,就是我们第 2 章讲的那 8 种 namespace;`linux.resources` 字段,就是第 3 章讲的 cgroup**。OCI runtime-spec 不是发明了什么新机制——它只是把"内核已有的 namespace + cgroup 这两件外套,该按什么参数套到一个进程上"**用一份 JSON 描述清楚**。

再看一眼 `LinuxNamespace` 这个最朴素的子结构:

```go
// LinuxNamespace is the configuration for a Linux namespace
type LinuxNamespace struct {
	// Type is the type of namespace
	Type LinuxNamespaceType `json:"type"`
	// Path is a path to an existing namespace persisted on disk that can be joined
	// and is of the same type
	Path string `json:"path,omitempty"`
}
```

就两个字段:`Type`(pid/network/mount/uts/ipc/user/cgroup/time 之一)+ `Path`。还记得第 1 章里 runc 的 `CloneFlags()` 那个 `if v.Path != ""` 的判断吗?——**这里 `Path` 为空 = 新建一个 namespace;`Path` 不为空 = 加入一个已有的 namespace**。runtime-spec 用 `Path` 这一个字段,就把"新建 vs 加入"两种语义都表达了。这正是 k8s Pod 共享 network namespace 的根基(第 16 章会用到)。

### 一个真实的 config.json 片段

光看结构体不过瘾,我们看一份**真实生成出来的 `config.json` 片段**。它来自 `runc spec` 命令——这个命令会生成一份默认的 `config.json`,内容定义在 [runc 的 specconv/example.go](../runc/libcontainer/specconv/example.go#L14-L156):

```go
spec := &specs.Spec{
    Version: specs.Version,
    Root: &specs.Root{
        Path:     "rootfs",
        Readonly: true,
    },
    Process: &specs.Process{
        Terminal: true,
        User:     specs.User{},
        Args: []string{
            "sh",      // ← 容器启动时跑的命令
        },
        Env: []string{
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "TERM=xterm",
        },
        Cwd:             "/",
        NoNewPrivileges: true,
        Capabilities: &specs.LinuxCapabilities{
            Bounding: []string{
                "CAP_AUDIT_WRITE",
                "CAP_KILL",
                "CAP_NET_BIND_SERVICE",
            },
            // ... Permitted、Effective 同上
        },
        // ...
    },
    Hostname: "runc",
    Mounts: []specs.Mount{
        { Destination: "/proc",        Type: "proc",   Source: "proc",   Options: nil },
        { Destination: "/dev",         Type: "tmpfs",  Source: "tmpfs",  Options: []string{"nosuid","strictatime","mode=755","size=65536k"} },
        { Destination: "/dev/pts",     Type: "devpts", Source: "devpts", Options: []string{"nosuid","noexec","newinstance","ptmxmode=0666","mode=0620","gid=5"} },
        { Destination: "/dev/shm",     Type: "tmpfs",  Source: "shm",    Options: []string{"nosuid","noexec","nodev","mode=1777","size=65536k"} },
        { Destination: "/dev/mqueue",  Type: "mqueue", Source: "mqueue", Options: []string{"nosuid","noexec","nodev"} },
        { Destination: "/sys",         Type: "sysfs",  Source: "sysfs",  Options: []string{"nosuid","noexec","nodev","ro"} },
        { Destination: "/sys/fs/cgroup", Type: "cgroup", Source: "cgroup", Options: []string{"nosuid","noexec","nodev","relatime","ro"} },
    },
    Linux: &specs.Linux{
        MaskedPaths:    []string{"/proc/acpi", "/proc/kcore", "/proc/keys", /* ... */ "/sys/firmware"},
        ReadonlyPaths:  []string{"/proc/bus", "/proc/fs", "/proc/irq", "/proc/sys", "/proc/sysrq-trigger"},
        Resources: &specs.LinuxResources{
            Devices: []specs.LinuxDeviceCgroup{{ Allow: false, Access: "rwm" }},
        },
        Namespaces: []specs.LinuxNamespace{
            { Type: specs.PIDNamespace },
            { Type: specs.NetworkNamespace },
            { Type: specs.IPCNamespace },
            { Type: specs.UTSNamespace },
            { Type: specs.MountNamespace },
        },
    },
}
```

把这份 Go 结构体 `json.MarshalIndent` 出来,就是一份你能直接给 runc 用的 `config.json`。读它,你会看清 **OCI runtime-spec 把一个容器描述成了什么**:

- **`root`**:rootfs 在 `./rootfs` 目录,只读。
- **`process`**:容器启动跑 `sh`,带几个环境变量,工作目录 `/`,只给 3 个 capability(AUDIT_WRITE/KILL/NET_BIND_SERVICE),`noNewPrivileges` 防止提权。
- **`mounts`**:把 `/proc`、`/dev`、`/sys`、`/sys/fs/cgroup` 这些**伪文件系统**挂进容器——这正是"容器看起来像一个独立系统"的那块拼图(第 4 章 rootfs 讲过)。
- **`linux.namespaces`**:容器要新建 PID/Network/IPC/UTS/Mount 这 5 种 namespace(默认是"新建"——`Path` 都为空)。
- **`linux.maskedPaths`/`readonlyPaths`**:把 `/proc/kcore`、`/sys/firmware` 这些**会泄漏宿主信息**的路径遮起来或设只读——这是容器的安全加固(第 22 章细讲)。

> **注意这份 spec 里"没有"什么**:它没有 `linux.resources.memory` / `linux.resources.cpu` 这些 cgroup 限额字段——`runc spec` 默认生成的 config 不限资源。**真实生产环境的 config.json 会由 containerd 等高层运行时,根据用户在 k8s 里写的 `resources.limits` 自动填进去。** 这也是为什么 OCI 把"限额"设计成可选字段——一个最小容器可以不限资源,但生产容器必须限。

---

## 四、low-level vs high-level:运行时的两层切分

OCI runtime-spec 把"怎么跑容器"标准化了,但如果你真的拿一个 bundle 去找 runc 跑,你会发现 runc **只管跑**——它不管"镜像从哪拉"、"跑完了状态怎么记"、"日志去哪写"、"容器死了要不要重启"。

这就引出了容器运行时生态里的一个重要分层:**low-level runtime(低层运行时) vs high-level runtime(高层运行时)**。

### 不这样会怎样:runc 一个人干完所有事

假设没有这个分层,让 runc 既跑容器、又管镜像、又管生命周期。会发生什么?

- **runc 会变成下一个 docker daemon**——一个巨型单体,什么都在里面。这正是 Docker 早期被诟病的"daemon 太重、出问题全家升天"。
- **runc 没法做到"跑完就退"**。runc 现在的设计哲学是:**它是一个短命的命令行工具**——`runc create` 把容器跑起来,然后 runc 进程**自己退出**,容器继续在后台跑(由一个叫 `runc init` 的子进程扛着)。如果 runc 还要管镜像/快照,它就不能这么轻量地退出。
- **没法演化出不同形态的运行时**。比如你想要一个"每个容器都跑在一个轻量 VM 里"的强隔离运行时(Kata),如果 runc 把所有事都揽了,Kata 就没法复用 runc 的"按 spec 跑"这部分能力。

### 所以:切两层,各管各的

容器社区(主要是 containerd 和 k8s 那帮人)给出的答案是:**把运行时切成两层**。

| 层 | 代表 | 职责 | 对应航运比喻 |
|----|------|------|--------------|
| **low-level runtime(低层运行时)** | **runc** | **只干一件事**:读 OCI bundle,按 `config.json` 把容器跑起来(建 namespace、设 cgroup、pivot_root、exec)。**跑完即退,不管其他。** | **起重机**:只负责把箱吊到位、通电、固定。吊完就熄火。 |
| **high-level runtime(高层运行时)** | **containerd**、CRI-O | 管镜像拉取、解包成 bundle、容器生命周期管理、快照、镜像仓库认证、把容器跑起来这件事**委托给 runc**。 | **港口管理公司**:管整个港口的运作——拉货、堆场、调度,具体吊箱子的事外包给起重机。 |

这个分工的精妙在于:

- **runc 保持极简、极稳**。它只对 OCI runtime-spec 负责,API 就是那份 `config.json`。只要内核的系统调用不变,它几乎不用改。这种"职责单一"的程序最容易做对、做稳。
- **高层运行时可以专心做"产品化"的事**。containerd 不用关心 namespace 怎么建(交给 runc),它专心做:从 registry 拉镜像、把镜像层用 overlayfs 叠成 rootfs、根据用户配置生成 `config.json`、调 runc 跑起来、记录容器状态、容器死了清理资源。
- **整个调用链是可替换的**。k8s 通过 CRI(Container Runtime Interface)调用 containerd;containerd 调用 runc。**任何一层都能换**——你可以把 runc 换成 kata(强隔离),containerd 这层不动;可以把 containerd 换成 CRI-O,k8s 这层不动。

> **航运比喻再补一笔**:这个分层就像航运业里**"港口管理公司"和"起重机"的分工**。港口管理公司(containerd)管整个港口的物流——拉货、堆场、调度、记账;具体的"把箱吊上船"这件重体力活,外包给起重机(runc)。港口管理公司可以换不同的起重机厂商(runc/kata/crun),只要起重机符合作业规范(runtime-spec);起重机也不关心港口是哪家公司在管,只管"给我一个标准箱,我吊上去"。

### runc 的命令:这个分层在 CLI 上长什么样

我们用 runc 自己的命令来印证这个分层。看 [runc 的 create.go](../runc/create.go#L11-L75) 和 [run.go](../runc/run.go#L12-L89),这两个命令的描述里都写了同一句话:

> The bundle is a directory with a specification file named "`config.json`" and a root filesystem.

也就是说,**runc 的入口契约就是一个 bundle**——它不关心这个 bundle 是谁准备的。containerd 准备好 bundle,然后调 `runc create` 或 `runc run`,runc 就照着干活。

特别要注意 runc 把生命周期拆成了两步:**`create` 和 `start`**。

- [`runc create`](../runc/create.go#L11-L75):**只建沙箱,不跑业务进程**。它读 `config.json`,把 namespace、cgroup、rootfs 全部搭好,把容器进程**阻塞在创建完成的态**(状态是 `created`,但 `process.args` 里那个命令还没 exec)。
- [`runc start`](../runc/start.go#L24-L56):**真正启动业务进程**。它对一个已经 `created` 的容器调用 `container.Exec()`,把 `config.json` 里 `process.args` 的那个命令跑起来。

> 看 [start.go](../runc/start.go#L36-L55) 的状态机:它检查容器状态必须是 `Created` 才允许 `start`;`Stopped` 报"已停止不能启动";`Running` 报"已经在跑"。**这个 `create` → `start` 的两步,就是"先把沙箱砌好、再放业务进程进去"的设计**。为什么要拆开?因为高层运行时(containerd)经常需要在"沙箱已建好、业务进程还没跑"的窗口里做点事——比如设置网络(容器进了 network namespace 之后,但业务进程还没占着网卡)、挂载卷、装 cgroup 限额。**如果把这两步合并,这些"在容器跑起来前要做的钩子"就没地方插。** 这个设计第 9 章讲 runc 内部时会再展开。

---

## 五、config.json:运行时之间的"合同"

理解了上面四节,我们终于可以钉死本章最核心的一个认知:

> **`config.json` 是 runc 和 containerd(以及任何 OCI 运行时)之间的"合同"。**

这个比喻极其贴切,我们把它讲透。

### 合同的双方

- **甲方(containerd / 任何准备 bundle 的人)**:负责把镜像 unpack 成 rootfs、把"这个容器要怎么跑"写成一份 `config.json`、把这两样东西放进一个目录(bundle)、把 bundle 交给乙方。
- **乙方(runc / 任何符合 OCI 的运行时)**:负责读 `config.json`,严格按里面写的去建 namespace、设 cgroup、挂 mount、跑 process.args 指定的命令。

**合同的"条款",就是 OCI runtime-spec 这份规范本身**。双方都只对这份规范负责,互不关心对方是谁。

### 不这样会怎样:没有合同会怎样

假设没有这份"合同",containerd 和 runc 之间怎么沟通?要么:

- **containerd 直接调 runc 的内部 Go 函数**——那 runc 任何一次重构都会让 containerd 挂掉,两个项目被焊死。
- **containerd 自己包办所有事,不用 runc**——回到"巨型单体 daemon"的老路。
- **containerd 通过一套私有 RPC 和 runc 通信**——那每换一个运行时(containerd → CRI-O),RPC 协议都得改,运行时不可替换。

有了 `config.json` 这份合同,以上问题全消失:**containerd 和 runc 通过一个文件(或一份等价的 JSON)通信,文件格式由 OCI 公开规范定义**。任何一方换了实现,只要还按合同办事,另一方不用改一行代码。

### 合同的"权威文本"在哪

这份合同的权威文本,就是 [OCI runtime-spec 的 specs-go/config.go](https://github.com/opencontainers/runtime-spec/blob/master/specs-go/config.go)(Go 结构体定义,同时也是 JSON schema 的源头)。runc 自己也依赖这个包——看 [runc 的 main.go](../runc/main.go#L21) 里的 import:

```go
import (
    // ...
    "github.com/opencontainers/runtime-spec/specs-go"
    // ...
)
```

**runc 和 runtime-spec 共用同一份 Go 类型定义**。这意味着:runtime-spec 一旦新增一个字段(比如某种新的 namespace 类型),runc 只要升级这个依赖,就能解析新版 config.json。**规范和实现通过这个共享的 Go 包,紧紧咬合在一起,但又各归各家**——规范归 OCI,实现归 runc 项目。

> **一个小验证**:在装了 runc 的机器上敲 `runc --version`,你会看到输出里有一行 `spec: 1.x.x`——这就是 runc 实现的 OCI runtime-spec 版本号。它来自 [main.go 的 printVersion](../runc/main.go#L41-L55) 里 `fmt.Fprintln(w, "spec:", specs.version)`。**每一个 runc 都公开声明自己实现的是哪个版本的"合同"**——这正是合同精神的体现。

---

## 关键源码精读:从一份 config.json 到一个容器

现在我们把前面讲的所有概念,在源码层面串一遍:**一份 `config.json` 进了 runc,是怎么变成一个跑着的容器的?** 这条路径,就是 OCI runtime-spec 落地的全过程。

### 第 1 步:runc 命令入口,接到一个 bundle

无论用户敲的是 `runc run` 还是 `runc create`,最终都汇到 [utils_linux.go 的 startContainer](../runc/utils_linux.go#L381-L432)。它的开头几行就把"runc 只认 bundle"这件事钉死了:

```go
func startContainer(cmd *cli.Command, action CtAct, criuOpts *libcontainer.CriuOpts) (int, error) {
    if err := revisePidFile(cmd); err != nil {
        return -1, err
    }
    spec, err := setupSpec(cmd)   // ← 读 bundle 里的 config.json
    if err != nil {
        return -1, err
    }
    id := cmd.Args().First()      // ← container-id(用户起的名字)
    // ...
    container, err := createContainer(cmd, id, spec)   // ← 把 spec 翻译成内部配置
    // ...
    r := &runner{ /* 把各种运行参数打包 */ }
    return r.run(spec.Process)    // ← 真正跑起来
}
```

`setupSpec` 在 [utils.go](../runc/utils.go#L72-L84) 里,就干一件事:切到 bundle 目录(`--bundle` 指定的路径),然后调 `loadSpec(specConfig)` 读 `config.json`。`specConfig` 这个常量在 [main.go](../runc/main.go#L57-L58) 里就是字符串 `"config.json"`——**文件名是写死的,这是 OCI 规范的硬性要求**。

### 第 2 步:把 OCI spec 翻译成 runc 内部配置

`loadSpec` 把 JSON 反序列化成 `*specs.Spec`(就是 runtime-spec 那个 Go 结构体)。但 runc 内部用的不是 `specs.Spec`,而是它自己的一套配置结构 `configs.Config`(在 `libcontainer/configs` 包里)。中间需要一个**翻译层**——这就是 [specconv.CreateLibcontainerConfig](../runc/libcontainer/specconv/spec_linux.go#L384-L458) 干的活:

```go
func CreateLibcontainerConfig(opts *CreateOpts) (*configs.Config, error) {
    cwd, err := linux.Getwd()
    // ...
    spec := opts.Spec
    if spec.Root == nil {
        return nil, errors.New("root must be specified")
    }
    rootfsPath := spec.Root.Path
    if !filepath.IsAbs(rootfsPath) {
        rootfsPath = filepath.Join(cwd, rootfsPath)   // ← root.path 相对路径,补成绝对路径
    }
    // ...
    config := &configs.Config{
        Rootfs:          rootfsPath,
        NoPivotRoot:     opts.NoPivotRoot,
        Readonlyfs:      spec.Root.Readonly,
        Hostname:        spec.Hostname,
        // ...
    }
    // 把 spec.Mounts 一条条翻译成 configs.Mount
    for _, m := range spec.Mounts {
        cm, err := createLibcontainerMount(cwd, m)
        // ...
        config.Mounts = append(config.Mounts, cm)
    }
    // ...
    // 把 spec.Linux.Namespaces 翻译成 configs.Namespaces
    for _, ns := range spec.Linux.Namespaces {
        t, exists := namespaceMapping[ns.Type]
        if !exists {
            return nil, fmt.Errorf("namespace %q does not exist", ns)
        }
        if config.Namespaces.Contains(t) {
            return nil, fmt.Errorf("malformed spec file: duplicated ns %q", ns)
        }
        config.Namespaces.Add(t, ns.Path)   // ← Path 非空 = 加入已有;Path 空 = 新建
    }
    // ...
}
```

这段翻译层揭示了两个设计要点:

**要点 1:runc 的内部数据结构和 OCI spec 是分离的。** `specs.Spec`(OCI 公开的)和 `configs.Config`(runc 私有的)是两套结构,中间靠 `specconv` 翻译。为什么?因为 **OCI spec 是面向"所有人"的公开契约,要稳定、要通用;而 `configs.Config` 是 runc 内部的实现细节,可以随重构自由演化**。这层翻译隔离了"公开契约"和"内部实现"——OCI spec 改了,改 specconv;runc 内部重构,也改 specconv。**specconv 就是合同双方之间的"适配层"**。

**要点 2:namespace 的翻译用一张映射表。** 看 [spec_linux.go 的 namespaceMapping](../runc/libcontainer/specconv/spec_linux.go#L51-L60):

```go
namespaceMapping = map[specs.LinuxNamespaceType]configs.NamespaceType{
    specs.PIDNamespace:     configs.NEWPID,
    specs.NetworkNamespace: configs.NEWNET,
    specs.MountNamespace:   configs.NEWNS,
    specs.UserNamespace:    configs.NEWUSER,
    specs.IPCNamespace:     configs.NEWIPC,
    specs.UTSNamespace:     configs.NEWUTS,
    specs.CgroupNamespace:  configs.NEWCGROUP,
    specs.TimeNamespace:    configs.NEWTIME,
}
```

这张表把 **OCI spec 里的 namespace 类型**(公开契约的命名)翻译成 **runc 内部的 namespace 类型**(实现细节的命名)。它和第 1 章里 [`CloneFlags()`](../runc/libcontainer/configs/namespaces_syscall.go#L11-L33) 用到的那张表是**同一个家族**——一个把"namespace 类型"翻译成"clone 标志位",一个把"OCI 命名"翻译成"runc 内部命名"。这两张表合起来,就是从 **`config.json` 里写一句 `"type": "pid"`** 到 **内核 `clone()` 调用里带上 `CLONE_NEWPID`** 的完整翻译链:

```
config.json: linux.namespaces[].type = "pid"
        │  (OCI spec 的字符串)
        ▼  specconv.namespaceMapping 翻译
runc 内部: configs.NEWPID
        │  (runc 的内部枚举)
        ▼  configs.CloneFlags() 翻译
clone(flags): unix.CLONE_NEWPID
        │  (内核的系统调用标志位)
        ▼
内核: 新进程进入一个新的 PID namespace
```

**整条链路上没有任何"神秘机制"**——就是几张映射表,把"用户/规范的语言"一步步翻译成"内核的语言"。这正是容器作为"内核能力组合"的本质在第 8 章的具体体现:OCI runtime-spec 不发明新机制,它只是**给"怎么组合 namespace + cgroup"定了一套统一的描述格式**。

### 第 3 步:交给 libcontainer 真正跑起来

`CreateLibcontainerConfig` 返回的 `*configs.Config`,被 [`libcontainer.Create`](../runc/utils_linux.go#L202-L204) 包装成一个 `*libcontainer.Container` 对象,最后由 [`runner.run(spec.Process)`](../runc/utils_linux.go#L431) 真正启动。

`runner.run` 之后的事——双进程模型(parent 用 clone 建沙箱、child 是容器进程)、pivot_root、exec——这些是**第 9 章《runc》**的主场,本章不展开。本章你只需要记住:**到这一步为止,runc 已经把"OCI spec 描述的容器"完整地翻译成了"内核能理解的系统调用参数",剩下的就是把它们真正执行下去**。

---

## 章末小结

### 用航运比喻回顾本章

回到那片港口。这一章我们做的,是给整个航运业**立规矩**。

1. **没规矩会怎样**:每家航运公司用自己尺寸的箱子、自己牌子的起重机。一个货主一旦选了某家,就被焊死了——换公司等于重新打包。这就是 2014~2015 年容器生态滑向的深渊:Docker 镜像只能 Docker 跑,CoreOS 想做自己的就被排挤,创新被掐死。
2. **所以 Docker 在 2015 年做了个高明的决定**:把自家的镜像格式和运行时 runc,捐给 Linux Foundation 下一个中立组织 OCI,作为公开标准的基石。短期看是"自废武功",长期看换来了"全世界按它的格式打包镜像"的中心地位。
3. **OCI 立了两套规矩**:**image-spec**(标准集装箱的尺寸与结构——镜像长什么样)+ **runtime-spec**(码头起重机的作业流程——怎么把镜像跑成容器)。两套规范通过 **bundle**(config.json + rootfs)这个产物衔接,各自独立演化。
4. **运行时又分两层**:**runc**(起重机,只管吊箱通电、干完即走)vs **containerd**(港口管理公司,管镜像/生命周期/快照,把吊箱的活外包给 runc)。这个分层让每一层都可替换——runc 能换 kata,gVisor;containerd 能换 CRI-O。
5. **`config.json` 是各方之间的"合同"**:它由 OCI runtime-spec 这份公开规范定义,任何运行时都得照着它干活。runc 通过一个翻译层(`specconv`)把这份合同转成内部配置,最终落地成 `clone(CLONE_NEW*)` + 写 cgroup 文件这些内核系统调用。

### 本章在全书主线中的位置

记住全书的二分法:**打包隔离 vs 调度编排。**

这一章服务的是**打包隔离**这一侧,而且是一个承上启下的关键节点:

- **承上**:它给第 7 章的"镜像 = layer + manifest + config"补上了**为什么这套格式全世界统一**的答案——因为它是 OCI image-spec,中立标准。
- **启下**:它给第 9 章《runc》铺好了**接口契约**——runc 干的所有事,都是"读 config.json,按里面写的去做"。下一章你看到 runc 源码里那一堆 namespace/cgroup/pivot_root 操作时,记住它们**全部源自 config.json 里那几行字段的翻译**。
- **再往后**:本章立的"分层 + 标准合同"思想,会在第 10 章(containerd)、第 19 章(kubelet 通过 CRI 调 containerd)反复回响。**k8s 之所以能调度几万个容器、还能换底层运行时,根源就在于 OCI 把"镜像"和"运行时"标准化、解耦了**——没有 OCI,就没有可插拔的容器运行时,也就没有 k8s 的"_runtime 无关"。

### 五个"为什么"清单

如果你只能记五件事,记这五件:

1. **为什么要有 OCI 标准**:没标准就会厂商锁定 + 社区分裂——你的镜像只能一家跑、运行时不可替换、创新被掐死。标准让"镜像"和"运行时"解耦,生态才能分裂生长。
2. **为什么 Docker 愿意捐出格式和 runc**:不是慈善,是生态博弈——捐出格式换来"全世界按它的格式打包",它的中心地位反而更稳。OCI 2015 年 6 月在 Linux Foundation 下成立。
3. **为什么是两套规范**:image-spec 管"镜像长什么样"(打包/分发阶段),runtime-spec 管"怎么把镜像跑成容器"(执行阶段)。两段通过 bundle(config.json + rootfs)衔接,各自独立演化,不焊死。
4. **为什么运行时要分两层**:runc 只管"按 spec 把一个容器跑起来"(轻、稳、可替换),containerd 管"镜像/生命周期/快照"(产品化、可演化)。分层让每层都可换——k8s 通过 CRI 调 containerd,containerd 调 runc,任何一层都能替换实现。
5. **config.json 是什么**:它是 OCI runtime-spec 定义的、运行时之间的"合同"。containerd 写它、runc 读它,双方只对 OCI 公开规范负责。runc 用 `specconv` 翻译层把它转成内部配置,最终落地成 `clone` 的 `CLONE_NEW*` 标志位 + cgroup 文件写入。

### 想继续深入,该往哪钻

- **亲手生成一份 config.json**:在任何 Linux 上装好 runc,`mkdir mybundle && cd mybundle && runc spec`——会生成一份默认的 `config.json`。打开它,对照本章的结构体定义逐字段读。这是理解 OCI runtime-spec 最快的办法。
- **看 runtime-spec 的权威文本**:[opencontainers/runtime-spec](https://github.com/opencontainers/runtime-spec) 的 `specs-go/config.go`(本章引用的 struct 定义)、`bundle.md`(bundle 的定义)、`config.md`(每个字段的语义说明)。
- **看 runc 怎么读 config.json**:从 [runc/utils_linux.go 的 startContainer](../runc/utils_linux.go#L381-L432) 入手,顺着 `setupSpec` → `loadSpec` → `createContainer` → `CreateLibcontainerConfig` 这条链读下去,你就把"config.json 进来,怎么变成内部配置"看完了。
- **看 image-spec 和 runtime-spec 怎么衔接**:containerd 的镜像解包代码(`core/mount`、`images/` 目录)——这是第 10 章《containerd》的主场。
- **想理解"为什么 runc 要 create/start 分两步"**:本章点了原因(给高层运行时留钩子),第 9 章《runc》会从源码层面展开双进程模型。

---

> 标准立住了:一份 `config.json` + 一个 `rootfs`,任何符合 OCI 的运行时都得照着跑。**那么 runc 这个"参考实现",到底是怎么把这份合同落地的?它读了 config.json 之后,具体干了哪几件事?** 翻开 **第 9 章 · runc:真正把容器跑起来的那个程序**——我们去 runc 的双进程模型里,看一个容器是怎么被一行行代码"砌"出来的。
