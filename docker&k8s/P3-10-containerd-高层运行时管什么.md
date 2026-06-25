# 第 10 章 · containerd:高层运行时管什么

> **前置**:你需要先读过[第 9 章《runc:真正把容器跑起来的那个程序》](P2-09-runc-真正把容器跑起来的那个程序.md)。那一章结尾留了一道扣子——runc 把一个容器"砌"出来后,它的事就结束了:它不是常驻进程,启动完容器进程就退出,既不负责拉镜像,也不负责管这个容器活多久。可问题是,**`docker run nginx` 这一条命令,显然不止"砌一个容器"这么简单**——那个 nginx 镜像从哪儿来?容器挂了谁拉起来?十几个容器同时跑,谁在背后记录它们的清单?这一章就来回答:runc 之上,那一层到底在管什么、为什么必须有它。

> **核心问题**:runc 已经能跑容器了,为什么上面还要一层 containerd?
>
> **读完本章你会明白**:
> - runc 只会"把一个已经准备好的 rootfs 跑起来",**镜像拉取、内容存储、快照、容器生命周期管理**——这些它一概不管。这些活儿,正是 containerd 的职责。
> - containerd 不是一个进程,而是**两个角色**:**一个常驻 daemon**(管全局:镜像、内容、快照、容器清单)+ **每个容器一个 shim 进程**(替 daemon 守着那个容器)。shim 为什么必须存在,是本章最精妙的一笔设计。
> - **shim 把容器和 daemon 解耦**:daemon 重启/升级,正在跑的容器照样跑——因为真正持有容器进程的不是 daemon,而是 shim。
> - k8s 不直接调 containerd 的内部 API,而是通过一套标准接口 **CRI(Container Runtime Interface)**——这套"标准插口"让 k8s 不绑死任何运行时。
> - containerd 的 snapshotter(默认 overlayfs)如何承接第 5 章讲的 overlayfs,把镜像分层**真正落成可挂载的文件系统**。

> **如果一读觉得太难**:先只记住三件事——① runc 是"建一次房就走"的施工队,containerd 是"物业公司"管日常;② 每个容器背后都站着一个 shim 小进程,daemon 挂了也不影响它;③ k8s 通过 CRI 这套"标准插口"找 containerd 干活,换运行时不用改 k8s。

---

## 章首·一句话点破

在第 9 章里,我们看见 runc 把"启动容器"这件事拆成了清晰的几步:读 runtime-spec → 建 namespace → 设 cgroup → pivot_root → exec 容器进程。一套动作下来,容器确实跑起来了。

可你真的去敲 `docker run nginx:alpine` 时,会发现一件怪事:**根本就没有 `runc` 这个命令出现在你的命令行里**。你只是说"我要一个 nginx",镜像就从一个叫 registry 的地方飞过来,文件就摆好了,容器就跑起来了。谁干的?

更关键的是——你过几天再 `docker ps` 看一眼,**那个 nginx 还在跑**。可第 9 章讲过,runc 跑完容器进程就退出了。**那这中间这几天,是谁在替你盯着这个容器?谁在记录"我这里有 5 个容器、分别是这些 ID、用的这些镜像"?又是谁,在容器进程意外退出时,知道该把退出码报给谁?**

> **比喻**:回到我们的港口。runc 是那台**起重机**——它把一个集装箱吊到位、固定、通电,这单活儿干完,起重机就可以去吊下一个了。可一个真正运转的港口,光有起重机远远不够:得有人**管堆场**(镜像在哪儿存、怎么叠)、**管物流**(怎么从别的港口把集装箱运过来)、**管每个通电集装箱的台账**(哪些在跑、用了哪个镜像、谁该被拉走)、**管起重机调度**(这一票货该用哪台起重机)。这个**管整个港口日常运作**的角色,就是**港口管理公司**——对应到容器世界,就是 **containerd**。

这一章,我们就来拆这家"港口管理公司":它内部到底分了几个部门、各管什么、为什么这么分工。

---

## 一、先看清 runc 留下的缺口:它不管的事太多了

要理解 containerd 为什么必须存在,最直接的办法,是把 runc **不管的事**一件件列出来——这些缺口,就是 containerd 存在的理由。

### runc 管什么、不管什么

回忆第 9 章,runc 接受的输入是一个**已经准备好的 bundle 目录**:里面有一个 rootfs(根文件系统)、一个 `config.json`(runtime-spec)。runc 的全部职责,就是照着 `config.json` 把这个 rootfs 跑起来——建 namespace、设 cgroup、换根、exec。**它假设你要的东西都已经摆在那儿了。**

那么"摆在那儿"之前,是谁把东西摆好的?让我们对着一次完整的 `docker run nginx:alpine`,把 runc 不管的事一件件拎出来:

| runc 不管的事 | 具体是什么 | 不这样会怎样 |
|---|---|---|
| **镜像拉取(pull)** | 从 registry(`docker.io` 这种)下载镜像 | 你得手动去 registry 下 tar 包,自己解压,自己摆好 rootfs |
| **镜像解构(layer/manifest/config)** | 把镜像拆成一层层的 diff,识别 manifest 和 config | 镜像在 registry 里不是一整个 tar,是一堆 layer blob + manifest;不解构根本不知道哪个是哪个 |
| **内容存储(content store)** | 把下下来的层内容**按 hash 寻址**存在本地 | 几十个镜像共用同一层(比如都基于 `alpine`),不按 hash 去重,本地磁盘会被几百份重复内容撑爆 |
| **快照管理(snapshot)** | 把层**叠加成可挂载的目录**(overlayfs 的 lower/upper) | runc 要的是一个**已经准备好的 rootfs**,可镜像只是"一堆层",谁来叠?谁来建 upper?谁来 copy-up? |
| **容器清单与生命周期** | 记录"这台机器上有哪些容器、状态如何",管 start/stop/delete | runc 跑完就走了,过几天你根本不知道这机器上跑过什么、哪些还活着 |
| **日志与退出码收集** | 收集容器的 stdout/stderr、记录退出码 | 容器进程的 stdout 默认是丢到 `/dev/null` 的;没东西收集,你 `docker logs` 看到一片空白 |
| **与 k8s 对接** | 给编排系统一个稳定、标准的接口 | k8s 想跑容器,就得知道"该调哪个程序、传什么参数";没有标准接口,每换一个运行时 k8s 都得改一遍 |

### 不这样会怎样:回到"手搓容器"那一章的痛

还记得第 6 章《动手:不用 docker,手搓一个容器》吗?那一章我们用几十行代码,从零造了一个能跑的容器,证明了"容器没有任何神秘力量"。但那一章的最后一节,我们也老实交代了:**这个手搓容器,完全不能用**——

- 你得**自己**去 docker registry 手动下镜像、解压、拼成 rootfs;
- 你得**自己**记下"这个 PID 是我启的容器",因为系统不会替你记;
- 容器一退出,**什么痕迹都没了**,退出码、日志全丢;
- 你想跑第二个容器?**自己**再来一遍全流程。

> **比喻**:第 6 章那台手搓"起重机",技术上完全正确——它确实能把一个箱子吊起来。但作为一台**能运营的港口设备**,它什么都不是:没有堆场、没有物流、没有台账、没有调度。**runc 就是这种状态**——技术上把"砌容器"做到了极致干净标准,但作为"日常跑容器的系统",它缺一大圈。

那么这一大圈缺的活儿,谁来补?**这正是 containerd 存在的全部理由**。

---

## 二、containerd 的两个角色:daemon + shim

containerd 不是一个进程,它在运行时实际表现为**两类进程**。理解这两个角色的分工,是理解 containerd 全部设计的关键。

### 角色 1:containerd daemon(常驻的"港口管理公司总部")

你在一台装了 containerd 的机器上敲 `ps`,会看到一个常驻进程,名字就叫 `containerd`:

```
$ ps aux | grep containerd
root  1234  containerd
```

这个进程就是 containerd 的**核心 daemon**(后台守护进程)。它从开机就启动,一直跑着,管的是**全局的事**:

- **镜像与内容**:谁要从 registry 拉镜像,daemon 负责下载、解构、按 hash 存进 content store;
- **快照**:把镜像层叠成 overlayfs(或其他 snapshotter)的可挂载目录;
- **容器清单**:维护一个 metadata 数据库(boltdb),记录"这台机器上有这些容器、用这些镜像、状态如何";
- **暴露 API**:开一个 gRPC socket(`/run/containerd/containerd.sock`),让外部(比如 k8s 的 kubelet、或者 `ctr` 命令行)能调它;
- **管理 shim**:每启动一个容器,daemon 就 fork 一个 shim 进程出来替它守着。

看 containerd 的入口程序就一目了然。daemon 的 main 函数极其简短([cmd/containerd/main.go](../containerd/cmd/containerd/main.go#L28-L34)):

```go
func main() {
    app := command.App()
    if err := app.Run(os.Args); err != nil {
        fmt.Fprintf(os.Stderr, "containerd: %s\n", err)
        os.Exit(1)
    }
}
```

整个 main 干的就一件事:启动一个 CLI app。真正的活儿在 `command.App()` 里,它装配好所有"部门"再启动 server。看 daemon 启动时的描述就点破了它的定位([cmd/containerd/command/main.go](../containerd/cmd/containerd/command/main.go#L83-L93)):

```go
// App returns a *cli.App instance.
func App() *cli.App {
    app := cli.NewApp()
    app.Name = "containerd"
    app.Version = version.Version
    app.Usage = usage
    app.Description = `
containerd is a high performance container runtime whose daemon can be started
by using this command. If none of the *config*, *publish*, or *help* commands
are specified, the default action of the **containerd** command is to start the
containerd daemon in the foreground.
...`
```

注意那个 banner——

```
                    __        _                     __
  _________  ____  / /_____ _(_)___  ___  _________/ /
 / ___/ __ \/ __ \/ __/ __ `/ / __ \/ _ \/ ___/ __  /
/ /__/ /_/ / / / / /_/ /_/ / / / / /  __/ /  / /_/ /
\___/\____/_/ /_/\__/\__,_/_/_/ /_/\___/_/   \__,_/

high performance container runtime
```

——"high performance container runtime"。**这是 containerd 对自己的定位:一个高性能的容器运行时**(注意,是 high-level runtime,底下还有 runc 这个 low-level runtime)。

### daemon 是个"插件式"架构:各部门都是插件

containerd daemon 内部不是一坨代码,而是**一堆插件(plugin)**拼起来的。镜像服务是一个插件,content store 是一个插件,overlayfs snapshotter 是一个插件,CRI 服务是一个插件……daemon 启动时把它们一个个加载、初始化、串起来。

看 daemon 加载插件的入口([cmd/containerd/server/server.go](../containerd/cmd/containerd/server/server.go#L112-L126)):

```go
// New creates and initializes a new containerd server
func New(ctx context.Context, config *srvconfig.Config) (*Server, error) {
    if err := apply(ctx, config); err != nil {
        return nil, err
    }
    ...
    loaded, err := LoadPlugins(ctx, config)
    if err != nil {
        return nil, err
    }
    ...
```

每个插件用 `init()` 函数向一个全局注册表登记自己的类型、名字、依赖谁、初始化函数是谁。比如 overlayfs 这个 snapshotter 的注册([plugins/snapshots/overlay/plugin/plugin.go](../containerd/plugins/snapshots/overlay/plugin/plugin.go#L56-L109)):

```go
func init() {
    registry.Register(&plugin.Registration{
        Type:   plugins.SnapshotPlugin,
        ID:     "overlayfs",
        Config: &Config{},
        InitFn: func(ic *plugin.InitContext) (any, error) {
            ...
            return overlay.NewSnapshotter(root, oOpts...)
        },
    })
}
```

读这段,你会发现一件很有意思的事:**overlayfs snapshotter 在 containerd 里,就是一个普通插件**。它注册的类型是 `SnapshotPlugin`、ID 是 `overlayfs`、初始化时建一个 `overlay.NewSnapshotter(...)` 实例。换句话说——**"用 overlayfs 还是别的联合文件系统"在 containerd 里是个可替换的选择**(native、btrfs、zfs、devmapper 都有对应的 snapshotter 插件)。

> **比喻**:港口管理公司内部是个**部门制**结构——仓储部(content store)、堆场部(snapshot)、物流部(image pull)、运行部(runtime/shim)、客服部(CRI 接口)。每个部门都是一个插件,**可替换**。你不喜欢 overlayfs 这种堆叠方式,换 native 直堆也行;你不喜欢 runc 这个底层,换 kata(轻量 VM)也行。**这种"部门可插拔"的设计,是 containerd 能容纳各种运行时变体的根基**。

### 角色 2:containerd-shim(每个集装箱一个驻场工人)

光有 daemon 不够。**containerd 最精妙的设计,不在 daemon,而在 shim**。

现在我们到了这一章最关键的问题:**如果 daemon 是常驻的,那容器进程和 daemon 是什么关系?**

最直觉的回答是——"容器进程就是 daemon 的子进程呗,daemon fork 出来的"。可是这个答案会带来一个灾难性的后果,我们先看灾难,再看 containerd 怎么躲开它。

---

## 三、shim 为什么存在:把容器和 daemon 解耦

这一节是整章的"魂"。理解了 shim,你就理解了 containerd 为什么是这个形状。

### 不这样会怎样:如果容器是 daemon 的亲儿子

假设我们让 containerd daemon 直接 fork 容器进程——也就是说,每个容器都是 daemon 的直接子进程。这听起来挺自然,直到你问几个问题:

**灾难 1:daemon 一升级,所有容器跟着死。** 你在生产环境跑着 200 个容器,某天要升级 containerd 到新版本。升级就得重启 daemon。可如果每个容器都是 daemon 的子进程——**daemon 一退出,它的所有子进程要么跟着死,要么变成孤儿被 init 收养、失去管理**。这意味着**升级 containerd = 重启所有业务**。这在生产环境根本不可接受。

**灾难 2:daemon 一崩,所有容器失控。** daemon 是个 Go 程序,Go 程序也会 panic、也会 OOM、也会被运维误 kill。如果容器是 daemon 的亲儿子,**daemon 一挂,200 个容器同时失控**——没人收它们的退出码、没人转发信号、没人收集日志。哪怕容器进程没死,你也不知道它们在干啥。

**灾难 3:容器进程的 stdin/stdout 没人接管。** 容器进程要写日志到 stdout,这些字节往哪儿去?如果让 daemon 直接接管,daemon 重启时这些管道就断了,日志全丢。

把这三个灾难摆在一起,结论就清楚了:

> **如果容器进程绑死在 daemon 上,那 daemon 就成了所有容器的单点故障——daemon 一抖,所有容器跟着抖。这在生产环境绝对不可接受。**

### 所以这样设计:用 shim 把容器"撑"起来,与 daemon 解耦

containerd 的解法极其漂亮:**不让 daemon 当容器的爹,而是给每个容器派一个独立的"小代理"——shim**。

具体流程是这样:

1. 你说"启动一个容器",daemon 不直接 fork 容器进程;
2. daemon 先 fork 一个 **shim 进程**(每个容器一个),这个 shim 才是容器进程的**真正父进程**;
3. shim 调用 runc,让 runc 把容器进程创建出来——**容器进程的父进程是 shim,不是 daemon**;
4. runc 创建完容器就退出了(它本来就是"建一次就走"的);
5. **shim 接管一切**:它持有容器进程、收它的 stdout/stderr、等它的退出码、转发信号;
6. 从此,daemon 和容器进程之间,**隔着 shim 这一层**。

> **比喻**:港口管理公司(daemon)不会自己去守着每一个通电的集装箱——它有的是集装箱。它给每个集装箱**派一个驻场工人(shim)**,这个工人就守在集装箱旁边,盯着它运转、记它的状态、收它的"日志"。管理公司总部(daemon)哪天要装修、要搬迁、要升级,完全不影响这些驻场工人——**工人和集装箱是绑定的,和管理公司总部是松耦合的**。

### shim 在源码里长什么样

shim 是怎么被拉起来的?看 containerd 启动 shim 的代码。当要启动一个容器时,runtime manager 会调 `Start`,进而调 `startShim`,最终落到 `binary.Start`——这个函数负责**把 shim 二进制跑起来**([core/runtime/v2/binary.go](../containerd/core/runtime/v2/binary.go#L66-L85)):

```go
func (b *binary) Start(ctx context.Context, opts *types.Any, onClose func()) (_ *shim, err error) {
    // containerd daemon is the intended caller of client.Command; the deprecation
    // targets external callers.
    cmd, err := client.Command(
        ctx,
        &client.CommandConfig{
            ID:           b.bundle.ID,
            RuntimePath:  b.runtime,        // ← 这就是 shim 二进制的路径
            GRPCAddress:  b.containerdAddress,
            TTRPCAddress: b.containerdTTRPCAddress,
            WorkDir:      b.bundle.Path,
            Opts:         opts,
            ...
            Action:       "start",
            SocketDir:    b.socketDir,
        })
    ...
```

注意 `RuntimePath: b.runtime`——这里的 `b.runtime` 不是 runc 的路径,**而是 shim 二进制的路径**(比如 `/usr/bin/containerd-shim-runc-v2`)。containerd 调用的是 shim,不是 runc。

那 shim 内部又是怎么调 runc 的?看 shim 进程的入口([cmd/containerd-shim-runc-v2/main.go](../containerd/cmd/containerd-shim-runc-v2/main.go#L29-L31)):

```go
func main() {
    shim.RunShim(context.Background(), manager.NewShimManager("io.containerd.runc.v2"))
}
```

`RunShim` 是 containerd 提供的框架,它把 shim 跑成一个常驻进程、监听一个 ttrpc socket(给 daemon 回调用),然后等 daemon 的指令。指令到了——比如"创建一个容器"——shim 才真正去调 runc。看 shim 内部创建容器的那一段([cmd/containerd-shim-runc-v2/process/init.go](../containerd/cmd/containerd-shim-runc-v2/process/init.go#L86-L94,L109-L151)):

```go
// NewRunc returns a new runc instance for a process
func NewRunc(root, path, namespace, runtime string, systemd bool) *runc.Runc {
    if root == "" {
        root = RuncRoot
    }
    return &runc.Runc{
        Command:       runtime,        // ← runc 二进制路径(默认 "runc")
        Log:           filepath.Join(path, "log.json"),
        LogFormat:     runc.JSON,
        PdeathSignal:  unix.SIGKILL,
        Root:          filepath.Join(root, namespace),
        SystemdCgroup: systemd,
    }
}

...

// Create the process with the provided config
func (p *Init) Create(ctx context.Context, r *CreateConfig) (retError error) {
    ...
    opts := &runc.CreateOpts{
        PidFile:      pidFile.Path(),
        NoPivot:      p.NoPivotRoot,
        NoNewKeyring: p.NoNewKeyring,
    }
    ...
    if err := p.runtime.Create(ctx, r.ID, r.Bundle, opts); err != nil {
        return p.runtimeError(err, "OCI runtime create failed")
    }
    ...
```

注意两个细节,它们把整个调用链讲透了:

1. `&runc.Runc{Command: runtime, ...}` —— shim 持有一个 runc 客户端对象,`Command` 字段就是 runc 二进制的名字。**shim 调 runc,是通过这个客户端去 exec runc 二进制**(传 `create` / `start` / `kill` 等子命令)。
2. `p.runtime.Create(ctx, r.ID, r.Bundle, opts)` —— 这一行真正调用 runc,对应 `runc create`(第 9 章讲过,create 先建沙箱、start 再启动业务)。**这一步之后,容器进程就被创建出来了,它的父进程是这个 shim**。

> 把这三段代码串起来,整条调用链就活了:**daemon → fork shim → shim 通过 runc 客户端 exec runc 二进制 → runc 用 clone 建出容器进程 → runc 退出,容器进程留在 shim 名下**。容器和 daemon 之间,从此隔着 shim 这一层缓冲。

### 铁证:daemon 重启后能"认领"回所有 shim

光说"shim 把容器和 daemon 解耦"还不够有说服力。最有力的证据是——**containerd daemon 重启后,会主动去把所有正在跑的 shim 重新"认领"回来**。这个机制在源码里白纸黑字写着。

看 `ShimManager.LoadExistingShims`,它在 daemon 启动时被调用,负责扫描磁盘上所有的 bundle 目录、把对应的 shim 重新连接进 daemon 的管理列表([core/runtime/v2/shim_load.go](../containerd/core/runtime/v2/shim_load.go#L36-L63)):

```go
// LoadExistingShims loads existing shims from the path specified by stateDir
// rootDir is for cleaning up the unused paths of removed shims.
func (m *ShimManager) LoadExistingShims(ctx context.Context, stateDir string, rootDir string) error {
    nsDirs, err := os.ReadDir(stateDir)
    if err != nil {
        return err
    }
    for _, nsd := range nsDirs {
        if !nsd.IsDir() {
            continue
        }
        ns := nsd.Name()
        ...
        if err := m.loadShims(namespaces.WithNamespace(ctx, ns), stateDir); err != nil {
            log.G(ctx).WithField("namespace", ns).WithError(err).Error("loading tasks in namespace")
            continue
        }
        ...
    }
    return nil
}
```

读这段,关键是它怎么"认领":**它扫描 stateDir 下每个 namespace 的目录,把每个 bundle 重新 `loadShim` 一遍**。`loadShim` 不是重新启动 shim(shim 还活着,容器还跑着),而是**重新和那个已经在跑的 shim 进程建立连接**(ttrpc),把它纳入管理列表。

进一步看 `loadShim` 里这一段([core/runtime/v2/shim_load.go](../containerd/core/runtime/v2/shim_load.go#L181-L199)):

```go
// There are 3 possibilities for the loaded shim here:
// 1. It could be a shim that is running a task.
// 2. It could be a sandbox shim.
// 3. Or it could be a shim that was created for running a task but
// something happened (probably a containerd crash) and the task was never
// created. This shim process should be cleaned up here.

_, sgetErr := m.sandboxStore.Get(ctx, id)
pInfo, pidErr := shim.Pids(ctx)
if sgetErr != nil && errors.Is(sgetErr, errdefs.ErrNotFound) && (len(pInfo) == 0 || errors.Is(pidErr, errdefs.ErrNotFound)) {
    log.G(ctx).WithField("id", id).Info("cleaning leaked shim process")
    // We are unable to get Pids from the shim and it's not a sandbox
    // shim. We should clean it up her.
    shim.delete(ctx, false, func(ctx context.Context, id string) {})
} else {
    m.shims.Add(ctx, shim.ShimInstance)
}
```

读这段注释——"It could be a shim that was created for running a task but something happened (**probably a containerd crash**) and the task was never created"——**containerd 自己都承认:daemon 是会 crash 的,我们得处理 daemon crash 之后留下的孤儿 shim**。这就是 shim 解耦设计的直接成果:**daemon crash 不等于容器死,daemon 重启后会重新认领所有还活着的 shim**。

> **比喻**:港口管理公司总部哪天突发火灾(daemon crash),驻场工人们(shim)还在各自的集装箱旁边好好守着,集装箱照常运转。总部重建好(daemon restart)之后,派人挨个走访每个驻场工人,重新登记入册(`LoadExistingShims`),继续指挥它们。**业务一秒都没断**。
>
> 这就是 containerd 用 shim 解耦的核心回报:**daemon 成了"可重启、可升级"的角色,而不再绑死所有容器**。

---

## 四、containerd 的"仓储部":content store 和 snapshotter

理解了 daemon + shim 这两个角色,我们再回头看 containerd 比 runc 多管的那些活儿里,最核心的一块——**镜像在本地是怎么存的**。

这块有两个关键概念,**很容易混淆,必须分清**:

- **content store(内容存储)**:存的是镜像**每一层的原始内容**,按内容的 SHA-256 hash 寻址。它是"未拆包的货物"。
- **snapshotter(快照器)**:存的是**叠加好的、可挂载的目录结构**(用 overlayfs 等联合文件系统)。它是"已经摆上货架、随时可通电的集装箱"。

### 不这样会怎样:为什么 content 和 snapshot 要分开

你可能会问:既然最终要的是 snapshot(可挂载的目录),为什么不直接把镜像层下下来就叠好,还要绕一道 content store?

**因为 content store 解决的是"去重与传输",snapshot 解决的是"使用"**——这两件事需求完全不同:

- **去重**靠的是"按内容 hash 寻址":两个镜像都基于 alpine,它们共享同一份 alpine 层的内容(同一个 hash)。content store 把每一层**按 hash 存一份**,天然去重。100 个 alpine 容器,alpine 那层在本地只占一份空间。
- **使用**靠的是"叠加成可挂载的目录":一个具体容器要跑,得有一个 rootfs。这个 rootfs 由镜像的所有层叠出来——这正是第 5 章讲的 overlayfs 的 lower/upper/merged 结构。

如果合并这两层——比如直接把每个镜像的内容解压成一份目录,那 100 个 alpine 容器就会占用 100 份 alpine 目录,空间爆炸;如果只存内容不叠,那 runc 启动时还得现场叠一遍,慢且复杂。**所以 containerd 把这两件事拆开**:content 存原始内容(去重),snapshot 存叠加好的目录(使用),snapshot 通过引用 content 来构建层。

> **比喻**:港口堆场分两个区——**冷库区(content store)**和**作业区(snapshotter)**。冷库区按"货物的指纹(hash)"存放,同一批货不管哪个客户要,只存一份;作业区把货物按订单叠好、摆成"通电就能用"的样子。冷库解决"别重复存",作业区解决"拿来就能用"。

### snapshotter 接口:这套"堆场规则"是抽象出来的

containerd 把"怎么叠层"抽象成了一个接口——`Snapshotter`。看它的定义([core/snapshots/snapshotter.go](../containerd/core/snapshots/snapshotter.go#L265-L309)):

```go
type Snapshotter interface {
    // Stat returns the info for an active or committed snapshot by name or
    // key.
    Stat(ctx context.Context, key string) (Info, error)

    // Update updates the info for a snapshot.
    Update(ctx context.Context, info Info, fieldpaths ...string) (Info, error)

    // Usage returns the resource usage of an active or committed snapshot
    // excluding the usage of parent snapshots.
    Usage(ctx context.Context, key string) (Usage, error)

    // Mounts returns the mounts for the active snapshot transaction identified
    // by key.
    Mounts(ctx context.Context, key string) ([]mount.Mount, error)

    // Prepare creates an active snapshot identified by key descending from the
    // provided parent.
    Prepare(ctx context.Context, key, parent string, opts ...Opt) ([]mount.Mount, error)

    // View behaves identically to Prepare except the result may not be
    // committed back to the snapshot snapshotter.
    View(ctx context.Context, key, parent string, opts ...Opt) ([]mount.Mount, error)
    ...
```

读这个接口,关键方法是 `Prepare` 和 `Mounts`:

- **`Prepare(key, parent)`**:基于一个"父快照"(已经 commit 的一层),创建一个**新的可写快照**(active)。它返回的是一组 `mount.Mount`——你可以理解为"按这个挂载参数,把这个目录挂出来"。对 overlayfs 来说,这组参数描述的就是 `lowerdir=...,upperdir=...,workdir=...`(第 5 章讲过)。
- **`Mounts(key)`**:对一个已经 Prepare 出来的 active 快照,返回它**当前**的挂载参数。容器要启动时,daemon 就是拿着这组参数去 mount,得到容器的 rootfs。

**这就把第 5 章讲的 overlayfs 和 containerd 串起来了**:containerd 的 overlayfs snapshotter,干的就是"把镜像的每一层 commit 成一个 snapshot,新容器启动时 Prepare 出一个 active snapshot(upperdir 是这个容器专属的写层),把所有这些参数交给 runc,runc 拿去 mount 就得到了 rootfs"。

### overlayfs snapshotter:第 5 章的 overlayfs 在 containerd 里的化身

具体到 overlayfs 这个 snapshotter,它怎么把镜像层变成 lower/upper 的?我们不需要看它的全部实现,关键是它**就是第 5 章讲的那个 overlayfs**——lowerdir 是镜像的各层(从 content store 解出来的、commit 成的 snapshot),upperdir 是当前容器自己的写层,merged 是容器看到的最终文件系统。

回扣第 5 章《联合文件系统 overlayfs》:那里讲过 overlayfs 的三层结构 lower/upper/merged,讲过 copy-up(容器改文件,改的是 upper 层的副本,镜像的 lower 层纹丝不动)。**containerd 的 overlayfs snapshotter,就是这套机制在"产品化"层面的落地**——它替你管好了"哪些层是 lower、每个容器的 upper 放在哪、merged 怎么挂出来",runc 只要拿着 `Mounts()` 返回的参数去 mount 就行。

> 这是本书"回扣内核机制"的又一个典型:第 5 章讲了 overlayfs 的内核原理(`mount -t overlay`),这一章看到 containerd 怎么用一套 Snapshotter 接口把它**封装成产品级的镜像层管理**。底层机制还是那套,上面盖的楼却是一整套仓储系统。

---

## 五、CRI:让 k8s 不绑死任何运行时的"标准插口"

到这里,containerd 已经能管理镜像、管 shim、管容器生命周期了。可它还有一件大事要做——**和 k8s 对接**。

### 不这样会怎样:如果没有标准接口

回想 k8s 的处境:它要在每个节点(node)上跑容器,但**容器运行时是个会演进、会替换的东西**——历史上 docker 是老大,后来 containerd 崛起,再后来有人想用 CRI-O,还有人想用 kata(gVisor/Kata Containers,带 VM 隔离强度的容器)。

如果 k8s 直接调每个运行时的内部 API,那它会面临一个噩梦:**每换一个运行时,k8s 的代码都得改一遍**。更要命的是,运行时升级时改了 API,k8s 也得跟着改。这种"互相绑死"的耦合,会让整个生态寸步难行。

### 所以这样设计:CRI(Container Runtime Interface)

k8s 的解法是经典的软件工程套路——**定义一套标准接口,自己只用接口,不管实现**。这套接口叫 **CRI(Container Runtime Interface)**,它用 gRPC + Protocol Buffers 描述,大致包含两部分:

- **RuntimeService**:管容器/Pod 的生命周期——`RunPodSandbox`、`CreateContainer`、`StartContainer`、`StopContainer`、`RemoveContainer`、`ContainerStatus`……
- **ImageService**:管镜像——`PullImage`、`ListImages`、`ImageStatus`、`RemoveImage`……

k8s 的 kubelet 只调这些标准方法,**完全不知道、也不关心底下是 containerd 还是 CRI-O 还是别的**。每个运行时要被 k8s 用,就得**实现这套 CRI 接口**——这是一个 gRPC server,监听一个 socket,kubelet 连过来调它。

> **比喻**:CRI 是港口管理公司给航运调度中心(k8s)开放的**标准业务窗口**。调度中心不需要知道你这个港口内部用的是什么牌子的起重机、堆场怎么布置——它只通过这个标准窗口递单子(我要跑一个 Pod、我要拉这个镜像、我要看这个容器状态),你这个港口把单子接了就行。**只要窗口的标准一致,调度中心可以随便换港口,港口也可以同时接多个调度中心的活**。

### containerd 实现 CRI:它就是一个插件

在 containerd 里,CRI 实现是一个**插件**。看它的注册代码([plugins/cri/cri.go](../containerd/plugins/cri/cri.go#L45-L65)):

```go
// Register CRI service plugin
func init() {
    defaultConfig := criconfig.DefaultServerConfig()
    registry.Register(&plugin.Registration{
        Type: plugins.GRPCPlugin,
        ID:   "cri",
        Requires: []plugin.Type{
            plugins.CRIServicePlugin,
            plugins.PodSandboxPlugin,
            ...
        },
        Config:          &defaultConfig,
        ConfigMigration: configMigration,
        InitFn:          initCRIService,
    })
}
```

读这段,注意几个要点:

1. **它是一个 `GRPCPlugin`**——也就是说,CRI 实现最终会被注册成 containerd gRPC server 上的一个服务。kubelet 连 containerd 的 socket,调的就是这个 CRI 服务。
2. **它的 ID 叫 `cri`**——和 overlayfs 那个 snapshotter 插件一样,只是众多插件之一。**CRI 不是 containerd 的"特权",只是它默认带的一个插件**。如果你不想要 CRI(比如你只用 `ctr` 命令行手动管理,不接 k8s),编译时可以加 `no_cri` tag 把它去掉([cmd/containerd/builtins/cri.go](../containerd/cmd/containerd/builtins/cri.go#L1))——`//go:build !no_cri` 这个 build tag 就是干这个的。
3. **它 Requires 一堆其他插件**(CRIServicePlugin、PodSandboxPlugin、SandboxControllerPlugin……)——说明 CRI 自己不干活,它是个**适配层**,把 CRI 的标准方法翻译成对 containerd 内部各种服务的调用。

CRI 的入口服务函数,以"启动一个 Pod 沙箱"为例([internal/cri/server/sandbox_run.go](../containerd/internal/cri/server/sandbox_run.go#L52-L54)):

```go
// RunPodSandbox creates and starts a pod-level sandbox. Runtimes should ensure
// the sandbox is in ready state.
func (c *criService) RunPodSandbox(ctx context.Context, r *runtime.RunPodSandboxRequest) (_ *runtime.RunPodSandboxResponse, retErr error) {
```

这就是 kubelet 调过来的入口——`RunPodSandbox`,CRI 标准方法。containerd 在这个函数里干一长串事:拉 pause 镜像、创建沙箱容器、配网络、最终让一个 Pod 沙箱就绪。这些活儿内部又会去调镜像服务、runtime 服务、shim——把整条调用链一路传到 runc。

### 把整条调用链画出来

到这里,我们可以把"k8s 跑一个容器"的完整调用链画清楚了:

```
   用户:kubectl run nginx
        │
        ▼
   kube-apiserver → etcd(声明:我要一个 Pod)        ── k8s 控制平面
        │
        ▼
   kubelet(节点上的驻港经理,看到期望状态)
        │  通过 CRI 标准 gRPC 接口
        ▼
   containerd daemon(CRI 插件接单)
        │  ① ImageService.PullImage  → 拉 nginx 镜像
        │  ② content store 按层 hash 存储
        │  ③ overlayfs snapshotter 把层叠成可挂载目录
        │  ④ RuntimeService.CreateContainer → 起 shim
        ▼
   containerd-shim-runc-v2(每个容器一个 shim)
        │  持有容器进程、收日志、等退出码
        │  通过 runc Go 客户端调 runc
        ▼
   runc(起重机:建 namespace、设 cgroup、pivot_root、exec)
        │  干完活就退出
        ▼
   nginx 容器进程(跑起来了,父进程是 shim)
```

这张图就是这一章的核心回报:**每一层都加了明确的、不可替代的价值**——

- kubelet 把"声明式期望"翻译成对运行时的具体调用;
- CRI 把"k8s 和运行时"解耦,让双方都能独立演进;
- containerd daemon 管全局(镜像、内容、快照、容器清单);
- shim 把"容器进程"和"daemon"解耦,让 daemon 可重启;
- runc 把"内核能力"组合成"砌容器"的标准动作;
- 最底下,是第 1~5 章讲的 namespace、cgroup、rootfs、overlayfs 这些内核机制。

**没有一个层级是冗余的**——每一层都解决了一个"不这样就会出大问题"的痛点。这就是分层架构的回报。

---

## 关键源码精读:从一个 gRPC 调用,到一个跑着的容器

把前面散见的源码串起来,我们走一遍"containerd 处理一个 CreateContainer 请求"的核心路径。**这一节是全章的源码高潮**——你会看见前面讲的每一个设计,在代码里如何对应。

### 第一站:daemon 启动,加载所有插件

containerd daemon 启动时,核心是 `server.New`,它通过 `LoadPlugins` 把所有注册的插件按依赖关系排好序,然后逐个初始化([cmd/containerd/server/server.go](../containerd/cmd/containerd/server/server.go#L149-L210)):

```go
for _, p := range loaded {
    id := p.URI()
    log.G(ctx).WithFields(log.Fields{"id": id, "type": p.Type}).Info("loading plugin")
    ...
    initContext := plugin.NewContext(
        ctx,
        initialized,
        map[string]string{
            plugins.PropertyRootDir:      filepath.Join(config.Root, id),
            plugins.PropertyStateDir:     filepath.Join(config.State, id),
            plugins.PropertyGRPCAddress:  grpcAddress,
            plugins.PropertyTTRPCAddress: ttrpcAddress,
        },
    )
    ...
    result := p.Init(initContext)
    ...
    instance, err := result.Instance()
    ...
    if p.Type == plugins.ServerPlugin {
        srv, ok := instance.(server)
        ...
        s.servers = append(s.servers, srv)
    }
    s.plugins = append(s.plugins, result)
}
```

读这段,关键信息:

- **每个插件被分到独立的 `RootDir` 和 `StateDir`**——比如 overlayfs snapshotter 的数据放在 `config.Root/io.containerd.snapshotter.v1.overlayfs/`,content store 放在 `config.Root/io.containerd.content.v1.content/`。**部门之间数据隔离**。
- 插件按依赖顺序加载(`loaded` 已经是 `registry.Graph` 拓扑排序过的结果)——CRI 插件 Requires 一堆别的插件,所以它会等那些先初始化好。
- 类型是 `ServerPlugin` 的插件会被加进 `s.servers`,daemon 启动时(`Server.Start`)逐个启动它们监听 socket——这就是 containerd gRPC server 怎么"开张"的。

### 第二站:ShimManager.Start——拉起一个 shim

当 CRI 插件收到 CreateContainer,层层往下,最终到 `ShimManager.Start`——这一步负责把 shim 进程拉起来([core/runtime/v2/shim_manager.go](../containerd/core/runtime/v2/shim_manager.go#L299-L323)):

```go
func (m *ShimManager) startShim(ctx context.Context, bundle *Bundle, id string, opts runtime.CreateOpts) (*shim, error) {
    ns, err := namespaces.NamespaceRequired(ctx)
    ...
    runtimePath, err := m.resolveRuntimePath(opts.Runtime)
    ...
    b := shimBinary(bundle, shimBinaryConfig{
        runtime:      runtimePath,
        address:      m.containerdAddress,
        ttrpcAddress: m.containerdTTRPCAddress,
        socketDir:    m.socketDir,
        env:          m.env,
    })
    shim, err := b.Start(ctx, typeurl.MarshalProto(topts), func() {
        log.G(ctx).WithField("id", id).Info("shim disconnected")

        cleanupAfterDeadShim(context.WithoutCancel(ctx), id, m.shims, m.events, b)
        // Remove self from the runtime task list.
        ...
```

注意 `m.resolveRuntimePath(opts.Runtime)`——这一步把"runtime 名字"(比如 `io.containerd.runc.v2`)解析成 shim 二进制的真实路径(比如 `/usr/bin/containerd-shim-runc-v2`)。**这里出现的 runtime 是 shim 的名字,不是 runc 的名字**——这是一个常见的混淆点,要分清:

- `io.containerd.runc.v2` 是 **shim 类型**的名字(表示用 runc-based 的 v2 shim);
- shim 内部才进一步去调真正的 **runc 二进制**(默认就叫 `runc`)。

`resolveRuntimePath` 还有一段对名字格式的校验,从错误信息能直接看到这两个名字的格式约定([core/runtime/v2/shim_manager.go](../containerd/core/runtime/v2/shim_manager.go#L386-L397))——它要求名字要么是 `io.containerd.xxx.vx` 这种"反向域名"格式(会在 PATH 里找 `containerd-shim-xxx-vx` 这个二进制),要么是一个绝对路径。

### 第三站:shim 收到指令,调 runc

shim 拉起来后,daemon 通过 ttrpc 给它发指令。shim 收到 `Create` 指令,落到 `Init.Create`——这一段在前面贴过,核心就是 `p.runtime.Create(ctx, r.ID, r.Bundle, opts)`([cmd/containerd-shim-runc-v2/process/init.go](../containerd/cmd/containerd-shim-runc-v2/process/init.go#L149-L151)):

```go
if err := p.runtime.Create(ctx, r.ID, r.Bundle, opts); err != nil {
    return p.runtimeError(err, "OCI runtime create failed")
}
```

`p.runtime` 是一个 `*runc.Runc`(在 [init.go#L64](../containerd/cmd/containerd-shim-runc-v2/process/init.go#L64) 字段定义),它的 `Create` 方法会去 exec `runc create` 这个子命令——而 `runc create` 干的所有事(建 namespace、设 cgroup、pivot_root),正是第 9 章详细讲的那一套。

**到这里,调用链从 kubelet 一路传到 runc,从 runc 传到内核的 clone/cgroup/pivot_root,容器进程就被创建了**。

### 第四站:daemon 重启,认领回所有 shim

最后,我们再看一遍那个最能体现 shim 价值的场景——daemon 重启。前面贴过 `LoadExistingShims` 的主循环,这里看它内部对每个 bundle 的处理([core/runtime/v2/shim_load.go](../containerd/core/runtime/v2/shim_load.go#L127-L175)):

```go
func (m *ShimManager) loadShim(ctx context.Context, bundle *Bundle) error {
    var (
        runtime string
        id      = bundle.ID
    )

    // If we're on 1.6+ and specified custom path to the runtime binary,
    // path will be saved in 'shim-binary-path' file.
    if data, err := os.ReadFile(filepath.Join(bundle.Path, "shim-binary-path")); err == nil {
        runtime = string(data)
    }
    ...

    runtime, err := m.resolveRuntimePath(runtime)
    ...

    binaryCall := shimBinary(bundle,
        shimBinaryConfig{
            runtime:      runtime,
            ...
        })
    // TODO: It seems we can only call loadShim here if it is a sandbox shim?
    shim, err := loadShimTask(ctx, bundle, func() {
        log.G(ctx).WithField("id", id).Info("shim disconnected")
        ...
    })
    ...
```

读这段,关键是 daemon 重启后,**它不需要重新启动 shim**(因为 shim 还活着、容器还跑着),它做的是:

1. 从 bundle 目录里的 `shim-binary-path` 文件读出"这个容器当时用的是哪个 shim 二进制"(daemon crash 前存的,见 [binary.go#L129-L131](../containerd/core/runtime/v2/binary.go#L129-L131));
2. 从 bundle 目录里的 `bootstrap.json` 读出 shim 的连接地址(也是 daemon crash 前存的,见 [binary.go#L143-L146](../containerd/core/runtime/v2/binary.go#L143-L146));
3. 用这些信息,**重新连接到那个还在跑的 shim 进程**,而不是重新拉起一个。

> 这就是 shim 设计的全部回报:**daemon 是有状态的(metadata 在 boltdb),但容器的运行时状态(进程在跑、stdout 在写)在 shim 手里,不在 daemon 手里**。daemon 重启,丢的只是自己的内存状态,丢不掉容器——因为容器不归它管,归 shim 管。

---

## 章末小结

### 用航运比喻回顾本章

回到港口。这一章我们认识了**港口管理公司**(containerd)——它不是一台起重机,而是一整套运营体系:

1. **它的总部是常驻的 daemon**,内部分成若干部门(插件):仓储部(content store,按货物指纹 hash 存原始层)、堆场部(snapshotter,把层叠成可挂载的目录)、运行部(管理 shim)、客服部(CRI,给航运调度中心 k8s 开的标准业务窗口)。**部门可插拔**——换联合文件系统、换底层运行时,都是换插件。

2. **它给每个通电集装箱派一个驻场工人(shim)**。这个工人是集装箱的真正"看护者":集装箱的进程归它管、日志归它收、退出码归它记。总部(daemon)哪天失火重建,工人和集装箱纹丝不动,总部重建完派人挨个走访工人、重新登记入册(`LoadExistingShims`)。**这就是为什么 containerd 能"daemon 升级不重启容器"**——这个能力在生产环境至关重要,是 containerd 相对早期 docker(容器曾是 dockerd 的子进程)的重大进步。

3. **它通过 CRI 这套标准业务窗口对接航运调度中心(k8s)**。调度中心只递标准单子(跑 Pod、拉镜像、看状态),完全不知道港口内部用啥起重机。这让 k8s 可以同时用 containerd、CRI-O、kata 等多种运行时,也让 containerd 不绑死 k8s(它也能给 docker 当底层、给 `ctr` 命令行直接用)。

### 本章在全书主线中的位置

回到全书的二分法:**打包隔离 vs 调度编排**。

- 这一章是"打包隔离"这半本里的**高层组织者**。它把第 1 篇(基石:namespace/cgroup/rootfs/overlayfs)和第 2 篇(标准:OCI/runc)的能力,**组织成一个能日常运转的系统**:管镜像、管存储、管生命周期、管和编排系统的对接。
- 第 9 章我们看见"砌一个容器"的原子动作(runc);**这一章我们看见"运营一群容器"的完整体系**(containerd)。**runc 是一次性施工,containerd 是长期物业**——这俩的分工,正是 high-level runtime 和 low-level runtime 这套分层标准(第 8 章 OCI 讲过)的具象化。
- 底下,它仍然回扣到内核:overlayfs(第 5 章)在 containerd 里化身 snapshotter;namespace/cgroup(第 2、3 章)由 runc 帮它落地;它自己是个 Go 程序,跑在第 1 章讲的那个共享内核上。

下一章(第 11 章《docker》)会讲这条调用链**最上面那一层**——docker CLI。你会发现,`docker run nginx` 这一条简单命令,实际上穿过了 docker CLI → dockerd → containerd → shim → runc → 内核这一长串;**docker 之于 containerd,加的主要是"用户体验"**——把 containerd 那套"系统级"的接口,包装成"小白也能用的一条命令",并附带镜像仓库、网络、卷、compose 这些开箱即用的体验。docker 的历史包袱(daemon 模式、root 权限、曾经自己包含 containerd 的功能)也会在那里交代清楚。

### 五个"为什么"清单

如果你只能记五件事,记这五件:

1. **为什么 runc 之上还要一层**:runc 只管"把一个已经准备好的 rootfs 跑起来",不管镜像拉取、内容存储、快照、生命周期管理、和编排系统对接——这些活儿全是 containerd 的职责。**runc 是一次性施工,containerd 是长期物业**。

2. **containerd 的两个角色是什么**:一个是常驻的 **daemon**(管全局:镜像、内容、快照、容器清单、对外 API),一个是**每个容器一个的 shim 进程**(替 daemon 守着这个容器,持有容器进程、收日志、等退出码)。

3. **shim 为什么存在**:为了**把容器和 daemon 解耦**。如果容器是 daemon 的子进程,daemon 一升级/一崩溃,所有容器跟着死;有了 shim,容器进程的父进程是 shim 而非 daemon,daemon 重启后通过 `LoadExistingShims` 重新认领所有还活着的 shim,业务不中断。**这是 containerd 相对早期 docker 的关键进步**。

4. **CRI 是什么**:k8s 定义的**容器运行时标准接口**(gRPC),让 k8s 不绑死任何运行时。kubelet 只调 CRI 标准方法(`RunPodSandbox`、`CreateContainer`、`PullImage`...),底下是 containerd、CRI-O、kata 都行。在 containerd 里,CRI 是一个插件(`plugins/cri`),编译时甚至能 `no_cri` 去掉。

5. **content store 和 snapshotter 有什么区别**:content store 按**内容 hash 寻址**存镜像每一层的原始内容(解决去重和传输);snapshotter 把这些层**叠加成可挂载的目录**(overlayfs 的 lower/upper/merged,解决使用)。前者是"冷库",后者是"作业区"。回扣第 5 章——overlayfs snapshotter 就是那套 overlayfs 机制在产品化层面的落地。

### 想继续深入,该往哪钻

- **亲手观测 containerd 的运行时结构**:在装了 containerd 的机器上,`ps -ef | grep containerd`——你会看到那个常驻 daemon;`ps -ef | grep containerd-shim`——你会看到每个容器一个的 shim 进程。再 `pstree -p <containerd_pid>` 看 shim 是不是 daemon 的子进程(答案:不是,shim 是 daemon 通过 `cmd.Start()` 拉起的,但启动后脱离父子关系)。
- **看 daemon 怎么加载插件**:[cmd/containerd/server/server.go](../containerd/cmd/containerd/server/server.go#L112-L221)(本章引用的 `New`),顺着 `LoadPlugins` 看 plugin 注册的依赖图怎么拓扑排序。
- **看 shim 启动和解耦的核心代码**:[core/runtime/v2/binary.go](../containerd/core/runtime/v2/binary.go#L66-L155)(shim 二进制怎么被拉起、bootstrap.json 怎么写)、[core/runtime/v2/shim_load.go](../containerd/core/runtime/v2/shim_load.go#L36-L201)(daemon 重启后怎么认领 shim)。
- **看 CRI 怎么把请求翻译成 containerd 内部调用**:[internal/cri/server/sandbox_run.go](../containerd/internal/cri/server/sandbox_run.go#L52-L54)(`RunPodSandbox` 入口),顺着看它怎么调镜像服务拉 pause 镜像、怎么调 runtime 服务起沙箱容器。
- **想看完整调用链**:在本机跑一个容器,用 `strace -f -p <containerd_pid>` 抓系统调用,你会看到 fork/exec shim、connect ttrpc socket 等一连串动作——把这一章的源码叙述和真实系统调用对照起来。

---

> 港口管理公司(containerd)讲完了:它有常驻的总部(daemon)、有派给每个集装箱的驻场工人(shim)、有给调度中心的标准业务窗口(CRI)、有冷库(content)和堆场(snapshot)。**那"一条命令开箱即用"的体验从哪儿来?**——`docker run nginx` 背后那条调用链最上面那一层,正是 **docker CLI**。下一章,我们看 docker 在 containerd 之上,又加了什么、留了哪些历史包袱。翻开 **第 11 章 · docker:让所有人都会用容器**。
