# 附录 B · 源码阅读路线与工具

> **前置**:这是全书的**实用参考篇**。你刚刚走完了从"一个裸进程"到"几万个 Pod 在几百个节点上调谐"的整段旅程(第 1~23 章),正文里反复出现"看这段源码""看这个函数"。这一篇不教新概念,只给你**一把钥匙**:想自己钻进 runc / containerd / kubernetes 的源码,该从哪进门;想观测一台真实跑着容器的机器,该用什么工具、敲什么命令。
>
> 这一篇的存在理由只有一句话:**容器这门技术,所有"魔法"都在源码里、所有"黑箱"都能用工具撬开。** 不读源码、不亲手观测,你永远在"背命令";读了、撬了,你才真正"看懂了它"。本篇就是把正文的每一处引用,收拢成一张可查的地图。
>
> **读完本附录你会明白**:
> - runc / containerd / kubernetes 三大仓库各自**该按什么顺序读**、入口文件在哪、容易踩什么坑。
> - 面对 kernel 这种上 GB 的超大仓库,**怎么不 clone 全量也能在线读、在线跳转、在线搜**。
> - 正文反复提到的 `docker inspect` / `ctr` / `crictl` / `kubectl` / `nsenter` / `/proc` / `/sys/fs/cgroup` 这些观测工具,**一句话用途 + 真实命令**长什么样。
> - 每个工具、每个源码入口,**对应回本书哪一章**讲过它。

---

## 一、为什么要有这篇附录

正文 23 章里,我们反复在做一件事:**把一个抽象概念,钉到一段具体源码上**。比如讲 namespace,我们引 kernel 的 `CLONE_NEW*` 定义;讲 runc 的双进程模型,我们引 `nsenter/nsexec.c`;讲 reconcile,我们引 client-go 的 `SharedInformer`。这些引用散落在各章,读者读的时候是"顺着主线走",不一定回头查。

但等你合上书,真要自己动手时,问题就变成了另一类:

- "我想看看 runc 到底怎么把容器进程拉起来的,先读哪个文件?"
- "k8s 那么大,我只想搞懂调度器,要不要把整个仓库 clone 下来?"
- "我机器上一个容器 OOM 了,怎么确认它是不是被 cgroup 杀的?"
- "`docker run` 背后到底发生了什么,有没有办法看到真实的系统调用?"

这些问题,正文不会专门讲(正文要讲"为什么",不是"怎么查")。本篇就是回答它们的。它分四块:

1. **三大仓库怎么读**(runc / containerd / kubernetes,每个给入口、路线、坑)。
2. **在线读大仓库的方法**(kernel、k8s 太大,本地 clone 不现实时怎么办)。
3. **观测工具速查表**(docker / containerd / k8s / 内核 / 网络 / 进阶)。
4. **"对应章节"索引**(每个工具/入口,对应本书哪一章)。

> **比喻**:正文是带你走完整片港口、讲清每座码头为什么这么建;本篇是把港口检查员的**眼镜、台账、探伤仪**一股脑塞给你,再附上一张"哪件工具查哪座码头"的对照表。带着它,你就能自己回去复检任何一座。

---

## 二、三大仓库怎么读

容器的源码分散在三个体量悬殊的仓库里。体量决定了读法:**小仓库(runc)可以通读,中仓库(containerd)挑模块读,大仓库(k8s)只读你要的那一块**。

| 仓库 | 体量 | 本地? | 读法 | 本书主角章 |
|------|------|-------|------|-----------|
| **runc** | 小(~21MB) | 本地 `../runc/` | 可通读主线 | 第 1~3、9 章 |
| **containerd** | 中 | 本地 `../containerd/` | 挑 shim / snapshot / CRI 读 | 第 10 章 |
| **kubernetes** | 极大(~1GB+) | 仅在线 | 分块,只读目标子目录 | 第 5~7 篇 |

下面逐个讲。

### 1. runc:真正把容器跑起来的那个程序

**仓库定位**:runc 是 OCI runtime-spec 的参考实现,是"起重机"——docker / containerd / k8s 最后都是通过它把容器真正砌出来的。它**小而精**,是读"容器底层到底干了什么"的最佳入口。本书第 9 章整章讲它。

**入口与推荐阅读顺序**(本地路径,可直接点开):

1. **命令行入口** [`main.go`](../runc/main.go) → 它注册了 `run`/`create`/`start`/`init` 等子命令(都在仓库根目录,注意 runc 的命令文件是**平铺在根目录**的 `run.go`、`create.go`、`init.go` 等,不是放在 `cmd/` 子目录下)。
   - [`run.go`](../runc/run.go#L11-L89):`runc run` = create + start 一气呵成。
   - [`create.go`](../runc/create.go):只建沙箱、不启动业务(第 9 章讲为什么分开)。
   - [`start.go`](../runc/start.go):打开那个"放行"FIFO,让 runc init 真正 execve 业务进程。
2. **真正的总调度** [`utils_linux.go` 的 `startContainer`](../runc/utils_linux.go#L381-L432):它在 `run`/`create` 之后被调用,负责"装好一个容器"的全流程编排。**这是读 runc 主线的最佳中枢**——顺着它往下,每一站都接得上。
3. **parent 侧(派生 runc init)** [`libcontainer/container_linux.go`](../runc/libcontainer/container_linux.go):
   - [`Start` / `Run`](../runc/libcontainer/container_linux.go#L205-L225)(注意:当前版本这两个方法挂在 `*Container` 上,老资料常写的 `*linuxContainer` 在新版里已**改名**)。
   - [`newParentProcess`](../runc/libcontainer/container_linux.go#L516) 派生出 runc init 子进程。
4. **child 侧(runc init 真正建沙箱)** [`libcontainer/standard_init_linux.go` 的 `Init()`](../runc/libcontainer/standard_init_linux.go#L51):这是"建 namespace、设 cgroup、pivot_root、exec 业务"的集大成函数,本书第 6 章和第 9 章都对照过它。
5. **双进程的关键 C 代码** [`libcontainer/nsenter/nsexec.c`](../runc/libcontainer/nsenter/nsexec.c):runc init 在 Go 代码跑起来**之前**,先执行这段 C(通过 [`init.go`](../runc/init.go) 的 `init()` 触发),完成"在多个 namespace 间跳转"的脏活。**这是 runc 双进程模型最绕、也最关键的一段**。

**容易踩的坑**(基于本书写作时对该版本的核查):

- **命令文件平铺在根目录**,不是 `cmd/run.go`。沿用其它 Go 项目习惯去 `cmd/` 下找会扑空。
- **`linuxContainer` 已改名 `Container`**,接收者是 `(c *Container)`。博客和老书里满篇 `linuxContainer.Start()`,在新版源码里搜不到——别以为自己看错了。
- **cgroups 逻辑不在 `libcontainer/cgroups/` 下**。本版本把 cgroup manager 抽成了独立的外部库 `github.com/opencontainers/cgroups`(见 [`container_linux.go` 的 import](../runc/libcontainer/container_linux.go#L22)),`libcontainer/` 下**没有** `cgroups/` 目录。如果你按老资料去找 `libcontainer/cgroups/fs/`,会完全找不到——得去 `vendor/github.com/opencontainers/cgroups/` 看。
- **`runc init` 不是 init 系统**。它只是 runc 内部的一个隐藏子命令,名字极其误导(容易和 PID 1 的 init 混淆)。读 [`init.go`](../runc/init.go) 时记住:它做的是"在被 clone 出来的新 namespace 里完成容器初始化"。

**推荐路线**:先读第 9 章,它会带你走一遍 `main.go → utils_linux.go → container_linux.go → standard_init_linux.go → nsexec.c` 的主线;读完第 9 章再回头通读源码,效率最高。

### 2. containerd:高层运行时管什么

**仓库定位**:containerd 是 docker 抽出来的"港口管理公司",管镜像拉取、快照、容器生命周期、shim 进程。它在 runc 之上,在 docker / k8s 之下。本书第 10 章整章讲它。它体量中等,不要通读,**按模块读**。

**入口与推荐阅读顺序**(本地路径):

1. **daemon 入口** [`cmd/containerd/main.go`](../containerd/cmd/containerd/main.go#L28-L34):极简,只启动一个 CLI app。
2. **真正的装配** [`cmd/containerd/command/main.go`](../containerd/cmd/containerd/command/main.go#L83-L93):这里把所有"部门"(server、plugin 注册)装配起来。
3. **插件加载** [`cmd/containerd/server/server.go`](../containerd/cmd/containerd/server/server.go#L112-L126):containerd 是**插件化架构**,所有功能(snapshotter、runtime、CRI)都是插件,在 daemon 启动时被注册。理解插件机制是理解 containerd 的钥匙。
4. **shim 机制**(containerd 最有特色的设计):
   - 启动 shim:[`core/runtime/v2/binary.go` 的 `Start`](../containerd/core/runtime/v2/binary.go#L66-L85)——它负责把 shim 二进制拉起来。
   - shim 自身入口:[`cmd/containerd-shim-runc-v2/main.go`](../containerd/cmd/containerd-shim-runc-v2/main.go#L29-L31)。
   - shim 内部调 runc:[`cmd/containerd-shim-runc-v2/process/init.go`](../containerd/cmd/containerd-shim-runc-v2/process/init.go#L86-L94)。
   - **shim 为什么存在**(让容器不绑死在 daemon 上)→ 读第 10 章。
5. **快照/分层存储** [`core/snapshots/snapshotter.go` 的 `Snapshotter` 接口](../containerd/core/snapshots/snapshotter.go#L265-L309):镜像分层是怎么落地的,看这个接口。
6. **CRI 插件**(k8s 通过它跑容器):
   - 注册:[`plugins/cri/cri.go`](../containerd/plugins/cri/cri.go#L45-L65)——注意 CRI 在 containerd 里**只是一个插件**,编译时可用 `no_cri` tag 去掉([`cmd/containerd/builtins/cri.go`](../containerd/cmd/containerd/builtins/cri.go))。
   - 实现:`internal/cri/server/` 下按 CRI 方法一个文件一个文件铺开(`container_create.go`、`container_start.go`、`container_remove.go`……)。想看"k8s 让 containerd 起一个容器"的完整链路,从 `container_create.go` + `container_start.go` 进。

**容易踩的坑**:

- **containerd 经历过大版本迁移**。仓库里同时有 v1 和 v2 风格的代码(`containerd/v2/` 的 import 路径)。读源码先认准 import path,别在新老之间跳来跳去。
- **shim 是独立二进制,不是 containerd 进程的一部分**。它由 containerd 拉起,但跑在自己的进程里(这就是它能让容器脱离 daemon 存活的原因)。调试时要在 shim 进程上看,不是在 containerd 主进程上看。
- **`ctr` 命令行是 containerd 原生的,不是 CRI 标准的**。它绕过 CRI 插件直接调 containerd API,所以 `ctr` 能跑的容器,k8s 不一定能跑(反之亦然)。观测 k8s 视角的容器要用 `crictl`,见后文。

**推荐路线**:先读第 10 章理清"daemon / shim / CRI"三层,再按上面的顺序进源码。重点放在 shim 机制和 CRI 插件这两块——它们是 containerd 区别于 runc 的核心价值。

### 3. kubernetes:极大仓库,分块读

**仓库定位**:k8s 是"航运调度中心",体量极大(GB 级),**不建议本地 clone 全量**(下文第三节讲怎么在线读、或 sparse checkout 子目录)。它内部是一个个独立的二进制(apiserver / controller-manager / scheduler / kubelet / kube-proxy),每个对应本书的不同章节。**按你想搞懂的组件,只读那一个子目录**。

| 想搞懂 | 读哪里 | 本书章节 |
|--------|--------|---------|
| 控制平面 / apiserver | `cmd/kube-apiserver/` + `staging/src/k8s.io/apiserver/` | 第 15 章 |
| 控制器(informer/reconcile) | `pkg/controller/` + `staging/src/k8s.io/client-go/tools/cache/` | 第 17 章 |
| 调度器 | `pkg/scheduler/` + `staging/src/k8s.io/kube-scheduler/` | 第 18 章 |
| 节点(kubelet) | `pkg/kubelet/` | 第 19 章 |
| 网络(kube-proxy) | `pkg/proxy/` | 第 20 章 |
| 存储(PV/PVC) | `pkg/controller/volume/` + `pkg/volume/` | 第 21 章 |
| 客户端库 | `staging/src/k8s.io/client-go/` | 第 15、17 章 |

**关键入口(在线 GitHub 链接,均可点开)**:

- **apiserver 入口**:[`cmd/kube-apiserver/app/server.go`](https://github.com/kubernetes/kubernetes/blob/master/cmd/kube-apiserver/app/server.go) 里的 `CreateServerChain`——它把三个 API server(extensions / core / aggregator)用**委托链**串起来。这是理解 apiserver"为什么是唯一入口"的起点。配套读 [`kubernetes/apiserver` 的 ARCHITECTURE.md](https://github.com/kubernetes/apiserver/blob/master/ARCHITECTURE.md),它反过来引用了这个函数,讲清了委托链的设计。
- **watch/informer 机制**:[`staging/src/k8s.io/client-go/tools/cache/`](https://github.com/kubernetes/kubernetes/tree/master/staging/src/k8s.io/client-go/tools/cache)——这是"所有控制器共用的事件引擎"。三件套必读:
  - [`shared_informer.go`](https://github.com/kubernetes/kubernetes/blob/master/staging/src/k8s.io/client-go/tools/cache/shared_informer.go)(门面)
  - [`delta_fifo.go`](https://github.com/kubernetes/kubernetes/blob/master/staging/src/k8s.io/client-go/tools/cache/delta_fifo.go)(事件队列)
  - [`controller.go`](https://github.com/kubernetes/kubernetes/blob/master/staging/src/k8s.io/client-go/tools/cache/controller.go)(取事件、回调的循环)
  - **理解这三件套,就理解了 k8s 一半的设计**(第 17 章整章讲它)。
- **调度器**:在 [`pkg/scheduler/`](https://github.com/kubernetes/kubernetes/tree/master/pkg/scheduler) 下。**注意:调度器源码近年大改过**。2020~2022 年的老资料反复引用 `pkg/scheduler/core/generic_scheduler.go` 和 `genericScheduler.Schedule`、`FindNodesThatFit`、`PrioritizeNodes` 三个函数——**这三个在 master 上已经不存在了**,`core/` 子目录也没了。整个"两阶段通用调度器"被重构进了**调度框架(scheduling framework)**:接口在 [`staging/src/k8s.io/kube-scheduler/framework/interface.go`](https://github.com/kubernetes/kubernetes/blob/master/staging/src/k8s.io/kube-scheduler/framework/interface.go)(权威源在 staging,pkg 下是镜像),filter/score 都变成了可插拔的插件(`pkg/scheduler/framework/plugins/` 下按插件名分子目录)。**读调度器前,务必先确认你看的资料是不是 2023 年以后的**。
- **kubelet**:[`pkg/kubelet/kubelet.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/kubelet.go) 的 `syncLoop` / `syncLoopIteration`——这是 kubelet "不停把期望状态往实际拉"的主循环。`pkg/kubelet/cri/` 下是它调 CRI 的代码,`pkg/kubelet/kuberuntime/kuberuntime_manager.go` 是"k8s Pod 语义 → CRI 调用"的翻译层。
- **kube-proxy**:[`pkg/proxy/iptables/proxier.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/proxy/iptables/proxier.go)(iptables 模式)、[`pkg/proxy/ipvs/proxier.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/proxy/ipvs/proxier.go)(ipvs 模式)。

**容易踩的坑**:

- **`staging/` 是权威源,`pkg/` 部分是镜像**。k8s 把可独立发布的子项目(client-go、kube-scheduler、apiserver 等)放在 `staging/src/k8s.io/` 下,构建时再同步到各自独立仓库。看接口/类型定义,**优先看 staging 版**(`pkg/` 下的是同步过去的副本,内容一致但 staging 是上游)。
- **老资料全面过时**。k8s 演进极快,调度器、APIService、CRI、volume 几乎每隔一两个大版本就重构。任何 2022 年前的博客/书,读之前都要在 master 上**核对文件还在不在**。
- **k8s 仓库不要 clone 全量**。下文专门讲。

**推荐路线**:别想"通读 k8s",它太大了。**先确定你要搞懂哪个组件**(一般是调度器或 kubelet,因为它们最有"机制"可讲),只读那一个子目录 + 它依赖的 client-go informer。

---

## 三、在线读大仓库的方法

kernel(~1.5GB)和 k8s(~1GB+)这俩仓库,**本地 clone 全量既慢又占地方**,绝大多数时候你只想看几个文件。下面这套方法,能让你**不 clone 也能高效读源码**。

### 1. zread:专门为"读代码"优化的在线阅读器

**zread**(以及类似工具)是读大仓库的首选:它把 GitHub 仓库做成可在线浏览、可跳转、可搜索的代码库,体验接近本地 IDE。它的几个杀手锏:

- **`get_repo_structure`**:看一个仓库/目录的结构,不用 clone。
- **`read_file`**:读单个文件,带行号。
- **跳转**:点函数名能跳到定义(类似 IDE 的 Go to Definition)。

> **本书怎么用 zread 的**:正文里凡是引 kernel / k8s 的链接,作者都是在 zread 上**先把行号核实过**才写的。你照着链接点进去,看到的就是作者当时看的内容。

### 2. GitHub 网页直接跳转

不装任何工具,GitHub 网页本身就能读:

- **blob 链接可点**:`https://github.com/<org>/<repo>/blob/<ref>/<path>#L行号` 直接定位到某一行。本书所有在线引用都是这个格式。
- **raw 文件**:把 `blob` 换成 `raw`(`raw.githubusercontent.com`),拿到文件原始内容,适合复制或喂给其它工具。
- **Code Search**:GitHub 顶部的搜索框,支持按仓库、按语言、按路径过滤。比如搜 `CreateServerChain repo:kubernetes/kubernetes` 直接定位。
- **Go to symbol**:在 blob 页面,点函数名/类型名,能看到"哪些地方调用了它""定义在哪"。

### 3. shallow / sparse checkout:只要一部分

如果你确实需要本地有一份(比如要跑 `grep`、要改改试),又不想拉全量:

```bash
# 只拉最近一次提交,不要历史(--depth 1)
git clone --depth 1 https://github.com/kubernetes/kubernetes.git

# 更进一步:sparse checkout,只拉某个子目录
git clone --depth 1 --filter=blob:none --sparse https://github.com/kubernetes/kubernetes.git
cd kubernetes
git sparse-checkout set pkg/scheduler staging/src/k8s.io/kube-scheduler
# 现在你的工作区里只有 scheduler 这一块,其它都不占空间
```

**这条对读 k8s 尤其有用**:你只想要 `pkg/scheduler`,就只拉它,几百 MB 不到。同理读 kubelet 就 `set pkg/kubelet`。

> **本书源码策略的来历**:正文的"源码策略"就是这套——runc / containerd 小,本地全 clone(`../runc/`、`../containerd/`);kernel / k8s 大,在线用 zread / GitHub 链接,不本地 clone。所以你在正文里看到的两类链接格式不一样:本地仓库是 `../runc/...` 相对路径(可离线点跳),在线仓库是 `https://github.com/...` 完整链接。

### 4. elixir.bootlin.com:读内核的利器

本书第 1 篇(基石)大量引用 kernel,但 kernel 没本地 clone。除了 GitHub,还有个**更适合读内核**的在线工具:**[elixir.bootlin.com](https://elixir.bootlin.com)**。它的优势:

- **跨版本**:可以选具体内核版本(本书用的是 master / 6.x 系),不同版本对比着看。
- **跳转强**:点函数、宏、结构体,直接跳定义,比 GitHub 网页好用。
- **带"xref"**(交叉引用):一个函数被哪些地方调用,一页列全。

读 kernel 的 namespace / cgroup / overlayfs 定义,强烈推荐用 elixir 替代 GitHub。本书引用 kernel 时给了 GitHub 链接(为了一致),但你自己读的时候用 elixir 体验更好。

---

## 四、观测工具速查表

源码告诉你"它怎么写的",工具告诉你"它在真实机器上怎么跑的"。这一节是**正文反复提到、但没集中讲**的所有观测工具,按"从上到下"的层次组织:docker → containerd → k8s → 内核观测点 → 网络 → 进阶。

每条都给:**一句话用途 + 典型命令 + 对应章节**。

### A. docker 侧(用户视角)

| 工具 | 一句话用途 | 典型命令 | 对应章 |
|------|-----------|---------|--------|
| `docker run` | 拉镜像 + 起容器(本书无数次用) | `docker run -d --name web -p 8080:80 nginx` | 第 1、11 章 |
| `docker inspect` | 看一个容器的完整配置(namespace、cgroup、rootfs、网络、PID 映射) | `docker inspect <容器名>` | 第 2、3、11 章 |
| `docker history` | 看镜像分层历史(每一层是哪条 Dockerfile 指令建的) | `docker history nginx` | 第 5、7 章 |
| `docker ps` | 列出正在跑的容器 | `docker ps -a`(含已停止) | 第 11 章 |
| `docker exec` | 进到正在跑的容器里执行命令 | `docker exec -it <容器> sh` | 第 2 章(进 namespace) |
| `docker stats` | 实时看每个容器的 CPU/内存/网络/IO 用量 | `docker stats` | 第 3 章(cgroup 计量) |

> **`docker inspect` 是神器**。它会输出一段巨大的 JSON,里面有 `State.Pid`(容器进程在宿主上的 PID,有了它你就能用 `nsenter` 进去看)、`HostConfig` 下的资源限制(对应 cgroup)、`NetworkSettings`(对应 network namespace)。本书第 2、3 章都让你 `docker inspect` 后去 `/proc/<那个 PID>/` 验证容器就是个进程。

### B. containerd 侧(底层视角)

| 工具 | 一句话用途 | 典型命令 | 对应章 |
|------|-----------|---------|--------|
| `ctr` | containerd 原生命令行,绕过 CRI 直接管镜像/容器/快照/task | `ctr images pull docker.io/library/nginx:alpine` | 第 10 章 |
| `crictl` | CRI 标准命令行,**k8s 视角**看容器和 Pod | `crictl ps`、`crictl pods` | 第 10、19 章 |

**`ctr` 常用子命令**:

```bash
ctr namespaces ls                      # containerd 用 namespace 隔离不同客户端(k8s 的在 k8s.io 这个 ns 下)
ctr -n k8s.io images ls                # 列 k8s 拉的镜像
ctr -n k8s.io containers ls            # 列容器(注意:这里叫 container,对应一个 runc 容器)
ctr -n k8s.io tasks ls                 # 列正在跑的 task(对应一个跑着的容器进程)
ctr -n k8s.io snapshots ls             # 列快照(对应镜像分层,第 10 章)
```

**`crictl` 常用**(注意它和 `ctr` 的视角差异):

```bash
crictl pods                            # 列 Pod(CRI 视角,Pod 是一等公民;ctr 没有 Pod 概念)
crictl ps                              # 列容器(每个容器都属于某个 Pod)
crictl inspect <容器ID>                # 看容器详情,类似 docker inspect 但走 CRI
crictl logs <容器ID>                   # 看容器日志
crictl stats                           # 看容器资源用量
```

> **`ctr` vs `crictl` 怎么选**:你想看"containerd 自己怎么管这些东西",用 `ctr`(它是 containerd 原生 API);你想看"k8s 眼里这台节点上有什么",用 `crictl`(它走 CRI 标准接口)。**在 k8s 节点上,优先用 `crictl`**——它看到的就是 kubelet 看到的。

### C. k8s 侧(集群视角)

| 工具 | 一句话用途 | 典型命令 | 对应章 |
|------|-----------|---------|--------|
| `kubectl get` | 列资源(Pod/Node/Service/Deployment…) | `kubectl get pods -o wide` | 第 5、6 篇 |
| `kubectl describe` | 看一个资源的完整详情(含 events,reconcile 的痕迹) | `kubectl describe pod <名字>` | 第 17、19 章 |
| `kubectl logs` | 看 Pod 日志 | `kubectl logs <pod> -c <容器>` | 第 19 章 |
| `kubectl exec` | 进到 Pod 里执行命令 | `kubectl exec -it <pod> -- sh` | 第 2、16 章 |
| `kubectl get -o yaml` | 看资源的完整 spec + status(声明式 API 的真身) | `kubectl get pod <名字> -o yaml` | 第 17 章 |
| `kubectl describe node` | 看节点资源(容量/已分配/地址/污点) | `kubectl describe node <名字>` | 第 18 章(调度) |
| `kubectl get events` | 看集群事件(controller 的 reconcile 动作) | `kubectl get events --sort-by=.lastTimestamp` | 第 17 章 |
| `kubectl apply` | 声明式提交期望状态 | `kubectl apply -f deploy.yaml` | 第 17 章 |
| `kubectl debug` | 临时起一个调试容器,挂进目标 Pod 的 namespace | `kubectl debug -it <pod> --image=busybox` | 第 22 章 |

**几个高频组合**:

```bash
# 看 Pod 完整定义(含 spec=期望 / status=实际,正好对应 reconcile 的两半)
kubectl get pod <名字> -o yaml

# 看 Pod 为什么调度到这个节点 / 为什么起不来(describe 的 Events 段是关键)
kubectl describe pod <名字>

# 看节点资源(Allocatable 是给容器用的上限,第 18 章调度器就看这个)
kubectl describe node <名字>

# 实时盯着 events 看(controller 每做一件事都会产生 event)
kubectl get events --watch
```

> **`-o yaml` 和 `describe` 是理解声明式 API 的钥匙**。前者让你看到"期望(spec)+ 实际(status)"并排陈列——reconcile 就是把 status 往 spec 拉;后者的 `Events` 段是 controller 留下的"作业痕迹",Pod 起不来、调度不上,绝大多数时候答案就在 Events 里。第 17、19 章反复让你看这俩。

### D. 直接钻内核观测点(最硬核,也最能"看穿"容器)

这一组工具最关键,因为它们**绕过所有上层封装(docker/k8s),直接在内核层面验证"容器就是个普通进程"**。本书第 1~3 章的铁证都靠它们。

| 工具 / 文件 | 一句话用途 | 典型命令 | 对应章 |
|------------|-----------|---------|--------|
| `ps aux` | 在宿主上看所有进程(含容器里的) | `ps aux \| grep nginx` | 第 1 章(铁证一) |
| `/proc/<pid>/ns/` | 看一个进程所属的所有 namespace inode | `ls -l /proc/<pid>/ns/` | 第 2 章 |
| `/proc/<pid>/cgroup` | 看一个进程在哪些 cgroup 里 | `cat /proc/<pid>/cgroup` | 第 3 章 |
| `/sys/fs/cgroup/` | cgroup v2 的统一层级,限额文件在这里 | `cat /sys/fs/cgroup/.../memory.max` | 第 3 章 |
| `nsenter` | 进入某个进程的 namespace 执行命令 | `nsenter -t <pid> -m -u -i -n -p sh` | 第 2 章 |
| `unshare` | 在新 namespace 里跑命令(自己造容器用) | `unshare --pid --fork --mount-proc sh` | 第 6 章 |

**最有用的几个用法**:

```bash
# 1. 验证容器就是个进程:在宿主上能看到它
docker run -d --name web nginx
docker inspect --format '{{.State.Pid}}' web     # 拿到容器进程在宿主的 PID,假设是 12345
ps aux | grep 12345                              # 它就在宿主进程表里

# 2. 看这个进程的所有 namespace(inode 号;两个进程 inode 相同=同一个 namespace)
ls -l /proc/12345/ns/
# 输出形如:net -> net:[4026532313]  pid -> pid:[4026532315] ...

# 3. 进到它的 namespace 里看世界(nsenter 是最直接的"钻进容器"工具)
nsenter -t 12345 -m -u -i -n -p sh
# 现在你在的 shell,看到的进程表、网卡、挂载点,和容器里一模一样
# 但你其实还在宿主上——这正是 namespace "造幻觉"的本质

# 4. 看它的 cgroup 限额
cat /proc/12345/cgroup                           # 它在哪个 cgroup 路径下
cat /sys/fs/cgroup/.../memory.max                # 读那个路径下的 memory.max,就是它的内存上限
```

> **`nsenter` 和 `/proc/<pid>/ns/` 是本组的灵魂**。正文第 2 章讲 namespace 时,让你做的"在宿主 ls 一下容器的 ns"就是这个。它证明了一件事:**容器的隔离不是"传送到了另一个世界",而是"被关在了一组 inode 标记的房间里"**。你随时能用 nsenter 走进去,也随时能用 `/proc/<pid>/ns/` 看穿它。

### E. 网络观测

容器网络是容器世界最绕的一块(第 12、13、20 章),工具也最杂。下表是排查容器网络问题时的标准组合。

| 工具 | 一句话用途 | 典型命令 | 对应章 |
|------|-----------|---------|--------|
| `ip link` / `ip addr` | 看网卡(veth pair 的一端在宿主,一端在容器) | `ip link`、`ip addr` | 第 12 章 |
| `ip netns` | 操作 network namespace(进、列、跑命令) | `ip netns list`、`ip netns exec <ns> ip addr` | 第 12 章 |
| `bridge link` / `brctl show` | 看 bridge 上挂了哪些 veth | `brctl show`(老)、`bridge link` | 第 12 章 |
| `iptables -t nat -L -n -v` | 看 NAT 规则(容器出网、端口映射都在这) | `iptables -t nat -L -n -v` | 第 12、20 章 |
| `ipvsadm -L -n` | 看 IPVS 规则(kube-proxy ipvs 模式用它) | `ipvsadm -L -n` | 第 20 章 |
| `tcpdump` | 抓包(查"包到底有没有出去/进来") | `tcpdump -i eth0 -nn port 80` | 第 12 章 |
| `conntrack -L` | 看连接跟踪表(NAT 的依据) | `conntrack -L -n \| grep <ip>` | 第 12、20 章 |

**排查"容器访问不到外网/别的容器"的标准流程**:

```bash
# 1. 先确认容器自己的网卡在不在、IP 对不对
ip netns exec <容器ns> ip addr         # 或 nsenter 进去看

# 2. 看宿主上 veth 的一端在不在、bridge 上挂没挂
ip link                                # 找 vethXXXX
brctl show                             # 看 docker0 / cni0 上挂了谁

# 3. 看 NAT 规则(SNAT 让容器出网、DNAT 把端口映射进来)
iptables -t nat -L -n -v

# 4. 抓包确认包走到哪一层没了
tcpdump -i any -nn port 80
```

> 第 12 章讲 veth/bridge/iptables、第 20 章讲 kube-proxy 时,都会让你在真实节点上跑这套命令。**容器网络问题 90% 是"包卡在某一跳",tcpdump 是定位卡在哪一跳的终极武器**。

### F. 进阶:系统调用与 eBPF

最后这一组是"想看穿一切"时的重武器,对应本书第 6 章(手搓容器,看 `clone`/`unshare`/`pivot_root` 真实怎么调)和第 22 章(安全,看 seccomp 卡哪些调用)。

| 工具 | 一句话用途 | 典型命令 | 对应章 |
|------|-----------|---------|--------|
| `strace -f` | 抓一个进程(及其子进程)的所有系统调用 | `strace -f -e trace=clone,unshare,mount,pivot_root docker run ...` | 第 6、22 章 |
| `bpftrace` | 用 eBPF 写一行脚本,观测内核任意点 | `bpftrace -e 'tracepoint:syscalls:sys_enter_clone { @[comm] = count(); }'` | 第 22 章(呼应内核 eBPF 卷) |
| `bcc` 工具集 | 一堆现成的 eBPF 观测工具 | `execsnoop`、`opensnoop`、`tcplife` | 第 22 章 |

**`strace` 是看"容器到底调了哪些系统调用"的最直接工具**:

```bash
# 看 docker run 背后的所有 clone/unshare/mount/pivot_root 调用
strace -f -e trace=clone,unshare,mount,pivot_root,execve \
       -o /tmp/runc.strace docker run --rm alpine echo hello

# 然后看 /tmp/runc.strace,你会看到第 6 章讲的那些调用一字排开:
#   clone(CLONE_NEWNS|CLONE_NEWPID|CLONE_NEWNET|...)  ← 建一堆 namespace
#   unshare(...)                                        ← runc init 自己再切
#   mount(...overlay...)                               ← 挂 overlayfs
#   pivot_root(...)                                     ← 换根
#   execve("/usr/bin/echo", ...)                        ← 跑业务进程
```

跑一遍这条命令,你会亲眼看到本书第 1~6 章讲的每一个机制,在真实机器上就是一行系统调用。**这是把"容器是普通进程"这件事钉死的最后一步**。

---

## 五、"对应章节"索引表

把上面两节(源码入口 + 工具)反过来看:**这本书的每一章,该用哪个工具观测、读哪段源码?** 下表是正反双向的索引。

| 本书章节 | 核心源码入口 | 首选观测工具 |
|---------|------------|-------------|
| 第 1 章 容器不是虚拟机 | runc `CloneFlags()` ([`libcontainer/configs/namespaces_syscall.go`](../runc/libcontainer/configs/namespaces_syscall.go#L11-L33));kernel `CLONE_NEW*` ([`include/uapi/linux/sched.h`](https://github.com/torvalds/linux/blob/master/include/uapi/linux/sched.h)) | `ps aux`、`uname -a`、`docker inspect` |
| 第 2 章 namespace | runc [`libcontainer/configs/namespaces.go`](../runc/libcontainer/configs/namespaces.go);kernel 各 ns 实现(`kernel/pid_namespace.c` 等) | `ls -l /proc/<pid>/ns/`、`nsenter`、`ip netns` |
| 第 3 章 cgroup | runc 经 `github.com/opencontainers/cgroups`;kernel `mm/memcontrol.c` | `cat /proc/<pid>/cgroup`、`/sys/fs/cgroup/`、`docker stats` |
| 第 4 章 rootfs/pivot_root | runc [`libcontainer/rootfs_linux.go`](../runc/libcontainer/rootfs_linux.go) 的 `pivotRoot`;kernel `fs/namespace.c` | `mount`(在容器里)、`strace -e pivot_root` |
| 第 5 章 overlayfs | runc [`libcontainer/mount_linux.go`](../runc/libcontainer/mount_linux.go);kernel `fs/overlayfs/` | `mount \| grep overlay`、`docker history` |
| 第 6 章 手搓容器 | 自写最小实现;对照 runc [`standard_init_linux.go`](../runc/libcontainer/standard_init_linux.go) | `strace -f -e clone,unshare,mount,pivot_root` |
| 第 7 章 镜像本质 | containerd 镜像解构(`images/`、[`core/snapshots/snapshotter.go`](../containerd/core/snapshots/snapshotter.go#L265-L309)) | `docker history`、`ctr images ls` |
| 第 8 章 OCI 标准 | [OCI runtime-spec](https://github.com/opencontainers/runtime-spec);runc 的 `create`/`start` | `docker inspect`(输出接近 runtime-spec) |
| 第 9 章 runc | runc [`utils_linux.go`](../runc/utils_linux.go)、[`container_linux.go`](../runc/libcontainer/container_linux.go)、[`standard_init_linux.go`](../runc/libcontainer/standard_init_linux.go)、[`nsenter/nsexec.c`](../runc/libcontainer/nsenter/nsexec.c) | `strace -f docker run`、`docker inspect --format '{{.State.Pid}}'` |
| 第 10 章 containerd | containerd [`cmd/containerd`](../containerd/cmd/containerd)、[`core/runtime/v2`](../containerd/core/runtime/v2)、[`plugins/cri`](../containerd/plugins/cri) | `ctr`、`crictl` |
| 第 11 章 docker | docker/moby [daemon](https://github.com/moby/moby) | `docker run/ps/inspect/stats` |
| 第 12 章 容器网络基础 | kernel `drivers/net/veth.c`、`net/bridge/`;runc [`network_linux.go`](../runc/libcontainer/network_linux.go) | `ip link/addr`、`brctl show`、`iptables -t nat -L`、`tcpdump` |
| 第 13 章 CNI | [CNI 规范](https://github.com/containernetworking/cni)、[plugins](https://github.com/containernetworking/plugins) | `ip addr`(看容器 IP)、`ip route`(看路由) |
| 第 14 章 编排为何存在 | (概念章) | `docker`(单机的局限)、对比 `kubectl` |
| 第 15 章 k8s 架构 | [`cmd/kube-apiserver/app/server.go`](https://github.com/kubernetes/kubernetes/blob/master/cmd/kube-apiserver/app/server.go)、`cmd/kubelet`、`staging/src/k8s.io/client-go` | `kubectl get nodes`、`kubectl cluster-info` |
| 第 16 章 Pod | [`pkg/kubelet`](https://github.com/kubernetes/kubernetes/tree/master/pkg/kubelet);pause 容器 | `kubectl get pod -o wide`、`crictl pods` |
| 第 17 章 声明式/reconcile | [`staging/src/k8s.io/client-go/tools/cache`](https://github.com/kubernetes/kubernetes/tree/master/staging/src/k8s.io/client-go/tools/cache)、`pkg/controller/` | `kubectl get -o yaml`、`kubectl get events --watch` |
| 第 18 章 调度器 | [`pkg/scheduler/`](https://github.com/kubernetes/kubernetes/tree/master/pkg/scheduler)、`staging/.../kube-scheduler/framework/interface.go` | `kubectl describe node`、`kubectl describe pod`(看调度 Events) |
| 第 19 章 kubelet | [`pkg/kubelet/kubelet.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/kubelet.go) 的 `syncLoop`、`pkg/kubelet/cri` | `kubectl describe pod`、`crictl ps`、节点上 `journalctl -u kubelet` |
| 第 20 章 Service/kube-proxy | [`pkg/proxy/iptables/proxier.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/proxy/iptables/proxier.go)、`pkg/proxy/ipvs/` | `kubectl get svc`、`iptables-save`、`ipvsadm -L` |
| 第 21 章 存储 | `pkg/controller/volume/`、`pkg/volume/`;CSI | `kubectl get pv/pvc/sc`、`kubectl describe pvc` |
| 第 22 章 安全 | runc [`seccomp/`](../runc/libcontainer/seccomp)、[`capabilities/`](../runc/libcontainer/capabilities)、[`apparmor/`](../runc/libcontainer/apparmor);kernel `kernel/seccomp.c` | `strace`(看哪些调用被挡)、`docker inspect` 看 `SecurityOpts` |
| 第 23 章 容器边界 | (概念章)Kata/gVisor | `cat /proc/<pid>/cgroup`、对比 VM 的 `uname` |

> 用法:你重读某一章时,顺手把这一章对应的源码入口点开看一眼、对应的工具在真实机器上敲一遍。**"读源码 + 敲工具"双管齐下,一章就真的吃透了**。这正是本书作者写每一章时做的事。

---

## 六、写在最后:工具会变,底座不变

这一篇列了一大堆工具、一大堆路径。可能有读者会担心:这些工具和路径,过两年是不是就过时了?

会。而且**一定有一部分会过时**——k8s 每个版本都在重构,`ctr`/`crictl` 的命令行会调整,GitHub 的某个文件会被搬走,某个插件会被废弃。这也是为什么本篇的源码链接**只标到目录或文件、不轻易标精确行号**(行号是最容易过时的东西),并且反复叮嘱你"读老资料前先在 master 上核对文件还在不在"。

但有一样东西**不会变**,而且它正是本书 23 章反复立的那个底座:

> **容器底下,永远是内核那几样能力——`clone` 的 `CLONE_NEW*`(namespace)、cgroup 的限额文件、`pivot_root`、`overlayfs`、veth/bridge/iptables。k8s 底下,永远是那个"watch 期望状态 → 把实际往期望拉"的 reconcile 循环。**

工具会换名字、路径会搬家、命令会改 flag,但只要你心里有这个底座:

- 看到任何"新容器运行时"(containerd 换成新的、runc 换成 kata、cri-o 换成别的),你只要问"它底层是不是还在用 namespace + cgroup?"——答案几乎总是"是"。
- 看到任何"新编排系统"(k8s 之上的 operator、nomad、甚至更野的),你只要问"它是不是也在 watch 一个期望状态、不停往实际拉?"——答案几乎总是"是"。
- 看到任何"新观测工具",你只要问"它最终是不是在读 `/proc`、`/sys/fs/cgroup`、iptables 规则、或者 hook 了某个内核 tracepoint?"——答案几乎总是"是"。

**带着这个底座去看任何新工具,你都不会迷路。** 因为它们都是在同一片地基上盖的不同形状的楼,而你已经把地基摸透了。

这就是本书想给你的东西,也是这篇附录作为"钥匙"真正想开的锁:

> **不是教你背下今天这堆命令,而是让你从此看到任何容器/编排工具,都能一眼看穿它底下是哪几样内核能力、哪个 reconcile 循环。**

祝你在这片港口里,自己走得比本书带你的更远。

---

> **下一步**:如果你还没有完整读过正文,推荐从[导言](P0-00-导言-一篇看懂容器全貌.md)或[第 1 章](P0-01-第一性原理-容器不是虚拟机.md)开始,把主线走一遍;读正文时手边开着本附录,随时对照源码和工具。另见 [附录 A · 全景脉络与设计哲学](附录A-全景脉络与设计哲学.md)——它把全书收束成几条哲学,和本篇的"工具地图"正好一内一外。
