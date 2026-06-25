# 第 11 章 · docker:让所有人都会用容器

> **前置**:你需要先读完[第 10 章《containerd:高层运行时管什么》](P3-10-containerd-高层运行时管什么.md)。那一章结尾,我们看清了"港口管理公司"(containerd)的全部家当:常驻的 daemon、给每个容器派的 shim、给 k8s 开的 CRI 业务窗口、管镜像的冷库(content)和堆场(snapshotter)。它已经是一套能日常运转的系统。可你真去敲 `docker run nginx` 时,用的不是 `ctr`、也不是直接调 containerd——你用的是 **docker**。这一章就回答最后一层:**docker 在 containerd 之上,又加了什么?为什么有了 containerd,我们还要 docker?**

> **核心问题**:`docker run nginx` 这一串简单命令背后,是怎样一长串调用链?docker 相比直接用 containerd 多做了什么?
>
> **读完本章你会明白**:
> - `docker run nginx` 这一条命令,实际上穿过了 **docker CLI → dockerd → containerd → containerd-shim → runc → 内核** 这么长一串;每一层各加了什么价值,为什么缺一不可。
> - 为什么 docker 把容器体验做到了"一条命令开箱即用"——它在 containerd 之上补的,主要是**用户体验**:镜像仓库(registry)、网络、卷(volume)、Dockerfile 构建、Compose。
> - docker 的两个历史包袱——**daemon 模式**和**默认 root 权限**——是怎么来的,以及 docker 为什么从 2017 年起,一步一步把底层让给 containerd(把 containerd、runc 捐出去,自己只管上层)。
> - 一个反直觉的事实:**今天的 dockerd 是"自动拉起一个 containerd"再连上去的**——docker 早就不再是"自己直接管容器"了,它现在只是 containerd 上面那个最好用的"前台"。

> **如果一读觉得太难**:先只记住三件事——① `docker run` = 一条命令穿过五层调用链,最底下还是第 1 篇讲的 namespace/cgroup;② docker 之于 containerd,主要是把"系统级接口"包装成了"小白也能用的一条命令",还附带镜像仓库、网络、卷、Compose;③ docker 有两个老毛病(daemon 模式、root 权限),所以后来它自己把 containerd/runc 捐了出去,只保留上层。

---

## 章首·一句话点破

第 10 章我们认识了一家设备齐全、运转高效的"港口管理公司"(containerd)。它什么都能干:拉镜像、存内容、叠快照、管 shim、给 k8s 开窗口。看起来,直接用 `ctr`(containerd 自带的命令行)就能跑容器了——那为什么真实世界几乎人人都敲 `docker run`,而不是 `ctr run`?

答案不在"docker 比 containerd 多了什么底层魔法",而在一个朴素得多的东西:**体验**。

> **比喻**:containerd 是一家**设备专业、流程严谨**的港口管理公司,但它对外的接口是一套**给工程师用的 gRPC**(相当于公司的"内部 OA 系统",要用就得知道字段、知道调用顺序)。普通货主(开发者)想用它,得先学会这套 OA——太劝退。
>
> docker 是开在这家管理公司门脸上的**一站式服务中心**:你只要走到前台说一句"我要一箱 nginx",它就替你填好所有单子、办好所有手续、把集装箱吊上船、连日志和账单都给你打印好。**它没有造任何新的港口设备,它只是把那套专业设备,包装成了"谁都会用"的前台服务**——这个包装本身,就是 docker 的核心价值。

这一章我们就拆这个"前台":它内部是怎么把一句 `docker run nginx` 翻译成一长串调用、它比 containerd 多做了哪几件让人省心的事、它又背着哪些历史包袱。最后,你会看清 docker 在整个生态里**今天的真实位置**——它已经不再是"包打天下"的老大哥,而是 containerd 上面那个最受欢迎的"壳"。

---

## 一、先看清调用链:`docker run nginx` 到底穿过了几层

要讲清楚 docker 加了什么,最直接的办法,是把 `docker run nginx` 这条命令从敲下去到 nginx 真正跑起来,**一层一层拆开**。先给你整张图,再逐层解释。

### 整条调用链一览

```
   你在终端敲: docker run nginx
        │
        ▼
   ① docker CLI        (docker/cli 仓库,你机器上的 /usr/bin/docker)
      解析参数、组装请求,通过 HTTP 发给 dockerd
        │   POST /containers/create  +  POST /containers/{id}/start
        ▼
   ② dockerd           (moby/moby 仓库,常驻 daemon,root 跑)
      管镜像/网络/卷/构建,把请求翻译成对 containerd 的调用
        │  (dockerd 启动时已经自动拉起一个 containerd,并连上它)
        ▼
   ③ containerd        (containerd 仓库,常驻 daemon)
      管镜像、内容、快照、容器清单;fork 一个 shim
        │
        ▼
   ④ containerd-shim   (每个容器一个,真正持有容器进程)
      通过 runc 客户端 exec runc
        │
        ▼
   ⑤ runc              (opencontainers/runc,一次性施工)
      建 namespace、设 cgroup、pivot_root、exec
        │
        ▼
   ⑥ 内核:clone(CLONE_NEW*)、写 cgroup、mount overlay
        │
        ▼
   nginx 容器进程跑起来了(父进程是 shim)
```

**整整五层(①~⑤),底下还压着内核(⑥)**。你敲的一行命令,实际触发了这一长串。这五层每一层各加了什么价值?我们一层层看。

### 第 ① 层:docker CLI——把"人话"翻译成"HTTP 请求"

`docker` 这个命令本身,其实是个**很薄的客户端程序**。它常被误以为就是 docker 全部——其实它只是个"前台接待员"。

> 一个重要的源码事实:今天的 `docker` 命令,根本不在 moby/moby 仓库里。**2017 年,随着 Moby Project 重构,docker CLI 被拆到了独立的 `docker/cli` 仓库**——`moby/moby` 只保留了 `dockerd`(daemon)和 `docker-proxy`。你机器上那个 `/usr/bin/docker`,是 `docker/cli` 编译出来的。

CLI 干的事很纯粹:解析你敲的 `run nginx -p 80:80` 这些参数,把它们组装成一个 HTTP 请求,发给本地的 dockerd(默认 socket 是 `/var/run/docker.sock`)。看 `docker run` 命令的入口([docker/cli 的 `cli/command/container/run.go`](https://github.com/docker/cli/blob/master/cli/command/container/run.go#L87-L117)):

```go
func runRun(ctx context.Context, dockerCLI command.Cli, flags *pflag.FlagSet, ropts *runOptions, copts *containerOptions) error {
    ...
    containerCfg, err := parse(flags, copts, serverInfo.OSType)
    ...
    return runContainer(ctx, dockerCLI, ropts, copts, containerCfg)
}
```

`runRun` 拿到参数后,调 `runContainer`——而 `runContainer` 内部最关键的两步,是先 `createContainer`(创建,拿到容器 ID),再 `ContainerStart`(启动)([docker/cli `cli/command/container/run.go`](https://github.com/docker/cli/blob/master/cli/command/container/run.go#L151)):

```go
containerID, err := createContainer(ctx, dockerCli, containerCfg, &runOpts.createOptions)
...
if _, err := apiClient.ContainerStart(ctx, containerID, client.ContainerStartOptions{}); err != nil {
```

注意这两步——**`docker run` 在底层被拆成了 `create` + `start` 两步**。这不是偶然,它和第 9 章讲的 runc "`runc create` 先建沙箱、`runc start` 再启动业务"是一脉相承的:**整个生态都遵循"先准备好环境、再启动进程"这个范式**,从最底层的 runc 一路贯彻到最上层的 docker CLI。

`createContainer` 内部最终调的是客户端库的 `ContainerCreate`([moby/moby `client/container_create.go`](https://github.com/moby/moby/blob/master/client/container_create.go#L18-L62)):

```go
func (cli *Client) ContainerCreate(ctx context.Context, options ContainerCreateOptions) (ContainerCreateResult, error) {
    ...
    resp, err := cli.post(ctx, "/containers/create", query, body, nil)
    ...
}
```

`cli.post` 是 HTTP 调用,目标地址是 dockerd 监听的 socket。**所以 docker CLI 和 dockerd 之间的协议,是 plain HTTP/JSON over unix socket,不是 gRPC**——这是和 containerd 的 gRPC、CRI 的 gRPC 都不一样的一笔。docker 比 containerd 出现得早,沿用了它自己那套 RESTful 风格的 API。

> **比喻**:CLI 是港口服务中心的**前台接待员**。你跟他说"我要一箱 nginx",他不懂怎么吊集装箱,但他会把你的需求填成一张标准单子(`POST /containers/create`),递进后面的办公室(dockerd)。前台本身不干活,它只翻译。

### 第 ② 层:dockerd——把"HTTP 请求"翻译成"对 containerd 的调用"

请求到了 dockerd。dockerd 是一个常驻的 Go daemon(守护进程),**这才是 docker 的本体**。它才是"港口服务中心"真正干活的后台。

dockerd 收到 `POST /containers/create` 这个 HTTP 请求,靠的是它内嵌的一套路由器。看路由注册([moby/moby `daemon/server/router/container/container.go`](https://github.com/moby/moby/blob/master/daemon/server/router/container/container.go)):

```go
router.NewPostRoute("/containers/create", c.postContainersCreate),
...
router.NewPostRoute("/containers/{name:.*}/start", c.postContainersStart),
```

这两条路由,正好对应 CLI 发出的两个请求。处理 `start` 的 handler 内部,会调用一个叫 `backend` 的接口([moby/moby `daemon/server/router/container/container_routes.go`](https://github.com/moby/moby/blob/master/daemon/server/router/container/container_routes.go#L283-L303)):

```go
func (c *containerRouter) postContainersStart(ctx context.Context, w http.ResponseWriter, r *http.Request, vars map[string]string) error {
    ...
    if err := c.backend.ContainerStart(ctx, vars["name"], r.Form.Get("checkpoint"), r.Form.Get("checkpoint-dir")); err != nil {
        return err
    }
    w.WriteHeader(http.StatusNoContent)
    return nil
}
```

这个 `c.backend.ContainerStart` 是个接口方法,它的实现就是 dockerd 的 `Daemon.ContainerStart`([moby/moby `daemon/start.go`](https://github.com/moby/moby/blob/master/daemon/start.go#L49))。这里有个设计上的小心思:**HTTP handler 不直接调 Daemon,而是先过一层 `backend` 接口**。为什么?这样可以把"HTTP 协议层"和"daemon 业务层"解耦——daemon 的核心逻辑不绑死 HTTP,未来换成别的协议(比如直接 gRPC)也不用大改业务代码。这是 moby 内部一个典型的接口分层。

到了 `Daemon.ContainerStart` 这一层,dockerd 干的事就很多了:校验容器状态、解析网络配置、设置 cgroup、生成 OCI spec(就是第 8 章讲的那份 runtime-spec)……做完这些,它要**真正启动容器进程**了——这一步,它不再自己 fork 进程,而是**调 containerd**。看 `Daemon.containerStart` 里关键的那一行([moby/moby `daemon/start.go`](https://github.com/moby/moby/blob/master/daemon/start.go#L197)):

```go
ctr, err := libcontainerd.ReplaceContainer(ctx, daemon.containerd, container.ID, spec, shim, createOptions, func(ctx context.Context, client *containerd.Client, c *containers.Container) error {
    ...
```

`daemon.containerd` 是 Daemon 持有的一个 containerd 客户端句柄。`libcontainerd.ReplaceContainer` 这个调用,就是 dockerd 把"启动这个容器"的活儿**外包给 containerd** 的入口。从这里往下,就进入了第 10 章熟悉的地盘:containerd 收到请求,fork 一个 shim,shim 调 runc,runc 建 namespace、设 cgroup、换根、exec——一路打通到内核。

> **这一层是全书调用链的"翻译枢纽"**。dockerd 上接 HTTP(给 CLI 用),下接 containerd 的 gRPC(给真正干活的用),它自己夹在中间,做的是"把 docker 那套以人为本的 API,翻译成 containerd 那套以机器为对象的 API"。它还顺手管了一堆 containerd 不管的事(网络、卷、构建、镜像仓库认证)——这些我们下一节细讲。

### 第 ③④⑤⑥ 层:containerd → shim → runc → 内核

这四层,正是第 10 章、第 9 章、第 2~5 章已经讲透的内容。这里只一句带过,不重复:

- **containerd**(第 10 章):管镜像、内容、快照、容器清单;给每个容器 fork 一个 shim。
- **shim**(第 10 章):真正持有容器进程,收日志、等退出码,让 daemon 可重启。
- **runc**(第 9 章):一次性施工队,建 namespace、设 cgroup、pivot_root、exec。
- **内核**(第 1~5 章):`clone(CLONE_NEW*)` 造隔离、写 cgroup 文件限资源、mount overlay 给 rootfs。

把整条链拉直了看,你会发现一件发人深省的事:**docker CLI 只占了最上面那一层,真正"把容器砌出来"的全部脏活累活,都在 containerd、shim、runc、内核手里**。docker 干的,主要是"翻译"和"包装"。

那这个"包装"到底包了什么?为什么这层包装如此重要,以至于几乎所有开发者都离不开 docker?下一节我们就来数 docker 比 containerd 多做的那些事。

---

## 二、docker 比 containerd 多做了什么:把"能用"变成"好用"

containerd 的接口(无论是 gRPC 还是自带的 `ctr` 命令)是**面向系统工程师**的:它的方法粒度很细(拉镜像是一个方法、创建容器是另一个、启动又是另一个),你要自己把这些方法串起来。直接用 containerd 跑容器,你得:

- 自己知道去哪个 registry 拉镜像、自己处理认证;
- 自己配网络(给容器接 veth、分 IP、设路由——第 12 章会讲这有多麻烦);
- 自己挂卷(数据写到哪、怎么持久化);
- 自己写一份 OCI runtime-spec(几十个字段,错一个就起不来)。

**containerd 给你的是零件,不是产品。** docker 的高明,就在于它把这些零件**组装成了一台开箱即用的整机**。下面是 docker 相对 containerd,补的最关键的几块。

### 1. 一条命令开箱即用:`docker run` 的"全自动"

你敲 `docker run nginx`,docker 默认会:

1. **自动去默认 registry(Docker Hub)拉镜像**——你不用指定 registry 地址、不用先 `pull` 再 `run`。
2. **自动配网络**——默认给容器接一个 bridge(`docker0`),自动分 IP、自动 NAT(第 12 章细讲)。
3. **自动准备 rootfs**——基于镜像叠好 overlayfs,你完全不用关心 lower/upper/merged。
4. **自动生成 OCI spec**——你 `-e ENV=xxx`、`-v /data`、`-p 80:80` 这些人性化的参数,docker 帮你翻译成 runtime-spec 的几十个字段。

对比一下 `ctr`:`ctr run docker.io/library/nginx:alpine mynginx /`——光是命令就长一截,默认还不拉镜像(你得先 `ctr image pull`),默认没网络,默认没日志收集。**`ctr` 是给开发者调试 containerd 用的,不是给用户跑业务的**。这之间的体验鸿沟,就是 docker 的价值。

> **比喻**:containerd 是一家**只对内营业**的港口公司,你得自己带起重机手、自己带网络工、自己填一堆专业表格。docker 是它门口的**一站式服务中心**:你只要说"一箱 nginx",剩下的从订货、运输、入关、上架、通电、记账,它全替你办了。

### 2. Dockerfile:把"怎么装箱"写成可版本化的脚本

containerd 不管"镜像怎么造出来"——它只管"已经有了镜像,怎么存、怎么跑"。**镜像的构建(build),是 docker 的一大发明。**

docker 引入了 **Dockerfile**——一份纯文本的"装箱说明书":

```dockerfile
FROM node:20-alpine
WORKDIR /app
COPY package.json .
RUN npm install
COPY . .
EXPOSE 3000
CMD ["node", "server.js"]
```

这份说明书里每一行指令(`FROM`、`COPY`、`RUN`……),docker 都会执行,并把**每一步的结果做成一个镜像层**(layer)。这正是第 5 章讲的 overlayfs 分层的源头——**每条 Dockerfile 指令,对应镜像里的一层**。这种"声明式 + 分层缓存"的构建方式,让镜像可复现、可缓存、可版本化(把 Dockerfile 跟代码一起放 git)。

> containerd 后来也有了构建能力(通过 buildkit 集成),但 Dockerfile 这套语法、这套"指令=层"的心智模型,是 docker 立下的标准,全行业沿用。

### 3. 镜像仓库生态:Docker Hub 与 registry 标准

光会构建镜像还不够,**得能搬运**——把镜像从这台机器运到那台、从开发运到生产。这就需要一个"集装箱集散中心":**registry**。

docker 干的第一件奠基性的事,是建了 **Docker Hub**(世界上第一个公共容器镜像仓库),并定义了 **registry 的 HTTP API 标准**。今天你 `docker pull nginx`,默认就是去 Docker Hub 拉;你 `docker pull myregistry.com/myapp:v1`,就是去自建 registry 拉。整个生态——containerd、k8s、podman、所有云厂商的镜像服务——都**遵循 docker 定义的这套 registry 协议**。

> **比喻**:docker 不仅造了港口服务中心,还顺手定义了"全球集装箱统一编号、统一货运单"的**行业标准**。哪家港口(哪个 registry)、哪种集装箱(哪个镜像)、怎么验真伪(content hash),全都按 docker 当年立的规矩来。containerd 也得遵守这套规矩才能和 docker 互通。

### 4. Volume(卷)与网络插件:把"临时容器"变成"有状态服务"

containerd 默认不管存储和网络——这些它都留给了上层(k8s 有自己的 PV/PVC 体系,网络有 CNI 插件)。docker 在单机层面,自己实现了两套好用的子系统:

- **volume**:`docker volume create mydata`、`docker run -v mydata:/data`。docker 管一块宿主上独立的目录,挂进容器——容器删了,数据还在。这让"数据库容器""有状态应用"成为可能。
- **network**:`docker network create mynet`、`docker run --network mynet`。docker 自带 bridge、host、overlay(跨主机)几种网络驱动,让多个容器能互通、能跨主机通信。

这两套子系统,是 docker 把"容器"从"一次性玩具"推进到"能跑真实业务"的关键。第 12 章我们会钻进容器网络的内核原理(veth/bridge/iptables),那里你会看到 docker 的 bridge 网络底下到底是什么。

### 5. Compose:把"一组容器"写成一份编排文件

最后,docker 还贡献了 **Compose**(`docker-compose` / `docker compose`)——一份 YAML 文件,描述一组相互关联的容器(比如 web + redis + db),一条 `docker compose up` 全部拉起来。

```yaml
services:
  web:
    image: nginx
    ports: ["80:80"]
  redis:
    image: redis
```

Compose 解决的是"一个应用往往是多个容器协同"这个真实痛点。它不是 k8s 那种大规模编排(那是第 5、6 篇的事),但在"本地开发、单机小服务"这个场景下,它的简单好用无可替代。

> 顺带一个容易混淆的点:**Compose 不在 moby/moby 仓库里**,它在独立的 `docker/compose` 仓库。moby/moby 只管 dockerd 和 docker-proxy;docker CLI 在 `docker/cli`;Compose 在 `docker/compose`——**docker 这个"产品",其实是横跨好几个仓库的一个生态集合**,这是它历史上一次次拆分的产物。

把这五块加在一起,你就明白了 docker 之于 containerd 的全部价值:**containerd 给的是零件,docker 给的是产品**。docker 没有造任何新的底层能力(底下还是 namespace/cgroup/overlayfs,运行时还是 containerd/runc),它做的是**封装、整合、产品化**——把"能让工程师用"的零件,组装成"让所有人都会用"的整机。这正是本章标题那句话:**docker 让所有人都会用容器**。

---

## 三、docker 的两个历史包袱:daemon 模式与 root 权限

上一节讲的全是 docker 的功劳。但 docker 也背着两个**沉重的历史包袱**——这俩包袱直接催生了后来 containerd 的独立、CRI 的诞生、甚至 k8s 一度"弃用 docker"的决策。理解它们,你才能理解 docker 今天为什么是"退居上层"的姿态。

### 包袱一:daemon 模式——所有容器都曾是 dockerd 的"亲儿子"

docker 一开始的设计是:**dockerd 这个常驻 daemon,直接 fork 每一个容器进程**。也就是说,每个容器都是 dockerd 的**直接子进程**。

这个设计在早期没什么问题,但随着容器规模上去,它暴露出三个灾难——这三个灾难,正是第 10 章讲 containerd 时反复提到的:

1. **dockerd 一升级,所有容器跟着死**:要升级 docker,就得重启 dockerd;dockerd 一重启,它所有的子进程要么跟着死、要么变孤儿。生产环境跑着几百个业务容器,**升级 docker = 全线重启业务**,这在生产环境根本不可接受。
2. **dockerd 一崩,所有容器失控**:dockerd 是个 Go 程序,会 panic、会 OOM、会被误 kill。dockerd 一挂,所有容器同时失去管理——没人收退出码、没人转发信号。
3. **dockerd 是个单点**:它是 root 权限的常驻进程,管着所有容器、所有镜像、所有网络——它一旦被攻破或出 bug,**影响面是整台机器上所有容器**。

> **这正是第 10 章 containerd 用 shim 解耦的根本原因**。containerd 吸取了 docker 早期的这个教训:不让 daemon 当容器的爹,而是给每个容器派一个独立的 shim 当爹,daemon 和容器之间隔着 shim 这层缓冲。**daemon 可重启、可升级,容器不受影响**——这个能力,是 containerd 相对早期 docker 的关键进步,也是后来 docker 把底层让给 containerd 的核心动因之一。

> 今天你装的新版 docker,底层已经走 containerd+shim 了(下一节细讲),所以"daemon 升级容器跟着死"这个问题已经基本解决。但"dockerd 是个 root 权限的单点常驻进程"这个**架构姿态**没变,它仍然是 docker 最被诟病的安全特征之一。

### 包袱二:默认 root 权限——那个让运维夜不能寐的 docker.sock

dockerd 默认以 **root** 身份运行。这不是 docker 独有的问题(任何要操作 namespace、cgroup、mount 的程序都得有特权),但 docker 把它放大成了一个系统级的攻击面:

- **`/var/run/docker.sock`** 这个 socket,默认只有 root 和 docker 组能读写。但**谁能访问这个 socket,谁就能以 root 身份在机器上起任意容器、挂任意目录**——`docker run -v /:/host alpine chroot /host` 一条命令,就把整个宿主文件系统交了出去。
- 历史上无数"容器逃逸"事故,根源都是 docker.sock 被不当暴露(比如挂进了一个不信任的容器,或者暴露给了 CI/CD 流水线里的恶意代码)。第 22 章(安全)会专门讲这个。
- **rootless mode** 是后来才补上的(让 docker 在普通用户权限下也能跑,靠 user namespace),但它是后加的,默认不开,配置也麻烦。

> **比喻**:dockerd 像一个**掌握整座港口总钥匙的超级管理员**。它必须有这么大权限(不这样的话,它没法建 namespace、没法 mount),但这把总钥匙一旦落到不该拿的人手里,整个港口就沦陷了。这是 docker"好用"换来的代价。

这两个包袱——daemon 单点 + root 权限——是 docker 从 2013 年那个"为了好用先这么设计"的决定里继承下来的。它们不致命,但它们让 docker 在**生产环境、大规模、多租户**的场景下显得力不从心。这也正是为什么,后来整个生态(尤其 k8s)一步一步把底层从 docker 手里拿走,交给了更专业、更解耦的 containerd。

---

## 四、docker 的自我瘦身:为什么它把底层让给了 containerd

理解了上面的包袱,你就能理解 docker 这十年来最重要的一次战略转身:**它一步步把自己的底层剥离出去,捐给了开源社区,自己只保留上层**。这个转身,直接塑造了今天我们看到的容器生态。

### 拆分时间线

| 时间 | 发生了什么 | 为什么 |
|---|---|---|
| **2015** | docker 把 **runc** 捐给刚成立的 **OCI(Open Container Initiative)**,作为 runtime-spec 的参考实现 | 让"怎么把容器砌出来"成为中立标准,避免厂商锁定(详见第 8 章) |
| **2017/03** | docker 把 **containerd** 捐给 **CNCF**(云原生计算基金会) | 让"高层运行时"也中立化;让 k8s 等其他编排系统能直接用 containerd,不再依赖 docker |
| **2017/04** | **Moby Project** 重构:`docker/docker` 更名为 `moby/moby`,docker CLI 拆到独立的 `docker/cli` | 把"docker 产品"和"moby 上游开源项目"分开,允许其他厂商基于 moby 构建自己的 docker 衍生版 |
| **2017** 起 | k8s 引入 **CRI**(Container Runtime Interface),1.24(2022)正式**移除对 dockershim 的支持** | k8s 不再"为了迁就 docker 而特殊对待它",所有运行时一视同仁走 CRI;docker 在 k8s 里被 containerd 取代 |
| **2019/02** | containerd 从 CNCF **毕业**(graduated,最高成熟度) | 标志着 containerd 已是生产级、社区主导的核心基础设施,docker 不再是唯一主导者 |

### 为什么 docker 要"自废武功"捐出去?

听起来 docker 是在割肉——把辛苦做出来的 containerd、runc 都捐出去。但这是一步精明的棋,背后的逻辑是:

1. **生态比产品值钱**。如果底层只有 docker 一家做,那别的厂商(AWS、Google、Red Hat)就不敢押注容器——他们会担心被 docker 锁定。docker 把底层捐成中立标准,等于**把整个行业拉上自己的船**:大家都基于同一套 runc+containerd,而 docker 仍然是这套生态里最受欢迎的上层产品。**标准是 docker 立的,蛋糕是大家一起做的,蛋糕做大 docker 吃得最多**。
2. **k8s 的崛起逼着 docker 选择**。2015 年之后 k8s 势不可挡,而 k8s 早期要跑容器只能调 docker。如果 docker 不开放底层,k8s 社区会另起炉灶造一个"非 docker 的运行时"(后来果然有了 CRI-O)。与其被绕开,不如主动把 containerd 捐出去、让 k8s 直接用——**这样 docker 至少还是底层的一部分**。
3. **docker 自己也受益于解耦**。捐出 containerd 后,docker 自己的 dockerd 也不用再维护那一大坨底层代码了——它现在内嵌一个 containerd,把脏活外包出去,自己专注上层体验。代码更轻、更稳定。

### "k8s 弃用 docker"到底弃用了什么

很多人听过"k8s 1.24 弃用 docker",以为"docker 要完蛋了"。这是个误解。准确地说:

- k8s 弃用的是 **dockershim**——一个 k8s 为了"让 docker 适配 CRI"而专门写的适配层。docker 早期没有原生实现 CRI,k8s 只好在 kubelet 里塞一段代码,把 CRI 调用翻译成 docker 的 API。这段代码叫 dockershim。
- 弃用 dockershim 后,**k8s 节点上不再装 docker,改装 containerd**(containerd 原生实现 CRI,见第 10 章)。所以"k8s 弃用 docker" = "k8s 节点上的运行时从 docker 换成了 containerd"。
- **但 docker 作为开发者工具一点没受影响**:你本地开发该 `docker build` 还 `docker build`,该 `docker run` 还 `docker run`。受影响的只是"k8s 集群里每个节点上跑的是哪个运行时"。

> **比喻**:航运调度中心(k8s)以前规定"每个港口必须用 docker 牌的管理公司"。后来它发现,docker 牌管理公司其实底下也是用 containerd,与其让 docker 在中间多转一手,不如直接用 containerd。所以调度中心改了规矩:"港口底层管理公司必须是符合 CRI 标准的,我推荐 containerd。" **docker 这个品牌没倒,它只是从"k8s 节点上的默认运行时"这个位置上退了下来,回到"开发者本地最爱用的容器工具"这个它本来的强项。**

---

## 五、一个反直觉的事实:今天的 dockerd 自己拉起一个 containerd

讲到这里,有一个很多人不知道、但对理解 docker 现状至关重要的事实:

> **今天你装完 docker 跑 `dockerd`,它启动时会自动再拉起一个 containerd 进程,然后连上去。docker 早就不"自己直接管容器"了——它现在只是 containerd 上面那一层。**

你可以在装了 docker 的机器上 `ps -ef | grep containerd` 验证:你会看到**两个** containerd 相关进程——一个是 dockerd 自己拉起的(叫 "managed containerd"),另一个可能是各 shim。这个"dockerd 自动管理一个 containerd"的行为,在源码里写得清清楚楚。

看 dockerd 启动时初始化 containerd 的逻辑([moby/moby `daemon/command/daemon.go`](https://github.com/moby/moby/blob/master/daemon/command/daemon.go#L1145-L1170)):

```go
func (cli *daemonCLI) initializeContainerd(ctx context.Context) (func(time.Duration) error, error) {
    systemContainerdAddr, ok, err := systemContainerdRunning(honorXDG)
    if err != nil {
        return nil, errors.Wrap(err, "could not determine whether the system containerd is running")
    }
    if ok {
        // detected a system containerd at the given address.
        cli.Config.ContainerdAddr = systemContainerdAddr
        return nil, nil
    }

    log.G(ctx).Info("containerd not running, starting managed containerd")
    opts, err := getContainerdDaemonOpts(cli.Config)
    ...
    r, err := supervisor.Start(ctx, filepath.Join(cli.Config.Root, "containerd"), filepath.Join(cli.Config.ExecRoot, "containerd"), opts...)
    ...
    cli.Config.ContainerdAddr = r.Address()
    return r.WaitTimeout, nil
}
```

读这段,它讲了一个清晰的三段决策:

1. **先看系统里有没有已经在跑的 containerd**(`systemContainerdRunning`)。如果有,**直接连它**——docker 不重复造轮子。
2. **如果没有,就自己拉起一个**——注意那行日志 `"containerd not running, starting managed containerd"`,这是 dockerd 亲手 fork 一个 containerd 进程出来的铁证。
3. **把这个 managed containerd 的地址记下来**(`cli.Config.ContainerdAddr = r.Address()`),后面 `NewDaemon` 就用这个地址去连。

`supervisor.Start` 内部最终会 `exec.Command` 调起 containerd 二进制([moby/moby `daemon/internal/libcontainerd/supervisor/remote_daemon.go`](https://github.com/moby/moby/blob/master/daemon/internal/libcontainerd/supervisor/remote_daemon.go#L146-L198)):

```go
cmd := exec.Command(r.daemonPath, "--config", cfgFile)
// redirect containerd logs to docker logs
cmd.Stdout = os.Stdout
cmd.Stderr = os.Stderr
...
```

`r.daemonPath` 就是 containerd 二进制的路径——dockerd 把它当子进程拉起来。所以从进程树看,**containerd 是 dockerd 的子进程**;但从职责看,**containerd 才是真正干底层活的那一层**,dockerd 只是它的"前台"。

> 这就回答了本章开头那个反直觉的事实。**docker 今天的真实架构是:`dockerd`(前台,管体验)→ `containerd`(后台,管底层)→ `shim`(每容器一个)→ `runc`(施工)**。docker 早已不是 2013 年那个"包打天下"的单体——它瘦身成了"containerd 上面那个最好用的壳"。这也是为什么第 10 章我们花那么大篇幅讲 containerd:在今天,理解 containerd 才是理解 docker 运行时本质的钥匙。

---

## 关键源码精读:从 `docker run nginx` 的一个 HTTP 请求,到 dockerd 调 containerd

把前面散见的源码串成一条完整的链路,我们走一遍"`docker run nginx` 真正启动容器那一刻"在源码里的样子。**这一节是全章的源码高潮**——你会看见前面讲的每一层设计,在代码里如何对应。

### 第一站:`Daemon` 这个结构体——dockerd 的"内脏清单"

要看懂 dockerd 干了什么,先看它持有哪些"部门"。`Daemon` 结构体的定义在 [moby/moby `daemon/daemon.go`](https://github.com/moby/moby/blob/master/daemon/daemon.go#L100-L158),关键字段有:

```go
type Daemon struct {
    ...
    registryService   *registry.Service         // ① 镜像仓库认证服务
    ...
    containerdClient  *containerd.Client        // ② 直连 containerd 的 gRPC 客户端
    containerd        libcontainerdtypes.Client // ③ docker 对 containerd 的封装层
    ...
}
```

注意这三个字段,它们正好对应 docker 的三层能力:

- **`registryService`**(111 行):docker 自己管的"镜像仓库服务"——负责去 Docker Hub/自建 registry 拉镜像、处理登录认证。这是 docker 比 containerd 多管的一块(第 10 章讲过,containerd 也管镜像拉取,但 docker 在上层做了认证、仓库选择的封装)。
- **`containerdClient`**(125 行):一个**原始的 `*containerd.Client`**——直接来自 `github.com/containerd/containerd/v2/client`。dockerd 在需要直接和 containerd 的镜像服务打交道时(比如把镜像存到 containerd 的 content store),用这个。
- **`containerd`**(126 行):**docker 自己封装的一层**(`libcontainerdtypes.Client`)。dockerd 在管"容器生命周期"时(创建、启动、停止容器)用这个——它把 docker 的概念翻译成 containerd 的调用。

> 读这三个字段,你就读懂了 dockerd 的"内脏":它既是 containerd 的客户端(`containerdClient`/`containerd`),又自己管着 containerd 不管的事(`registryService`)。**dockerd 不是一个"翻译器",它是一个"翻译器 + 一堆 containerd 没有的子系统"**。

### 第二站:`NewDaemon`——dockerd 启动时怎么连上 containerd

`Daemon` 是怎么被造出来的?看 `NewDaemon`([moby/moby `daemon/daemon.go`](https://github.com/moby/moby/blob/master/daemon/daemon.go#L849)):

```go
func NewDaemon(ctx context.Context, config *config.Config, pluginStore *plugin.Store, authzMiddleware *authorization.Middleware) (_ *Daemon, retErr error) {
    registryService, err := registry.NewService(config.ServiceOptions)
    if err != nil {
        return nil, err
    }
    ...
```

它第一件事就是建 `registryService`(镜像仓库服务)。然后在 999 行把它存进 `d.registryService`:

```go
d.registryService = registryService
```

再往下,到了 1006-1028 行,是 dockerd **连 containerd** 的核心逻辑:

```go
const connTimeout = 60 * time.Second

gopts := []grpc.DialOption{
    grpc.WithStatsHandler(tracing.ClientStatsHandler(otelgrpc.WithTracerProvider(otel.GetTracerProvider()))),
    grpc.WithUnaryInterceptor(grpcerrors.UnaryClientInterceptor),
    grpc.WithStreamInterceptor(grpcerrors.StreamClientInterceptor),
}
if cfgStore.ContainerdAddr != "" {
    log.G(ctx).WithFields(log.Fields{
        "address": cfgStore.ContainerdAddr,
        "timeout": connTimeout,
    }).Info("Creating a containerd client")
    d.containerdClient, err = containerd.New(
        cfgStore.ContainerdAddr,
        containerd.WithDefaultNamespace(cfgStore.ContainerdNamespace),
        containerd.WithExtraDialOpts(gopts),
        containerd.WithTimeout(connTimeout),
    )
    if err != nil {
        return nil, errors.Wrapf(err, "failed to dial %q", cfgStore.ContainerdAddr)
    }
}
```

读这段,关键信息:

- **`cfgStore.ContainerdAddr`**——这就是前文 `initializeContainerd` 填进去的那个地址(要么是系统 containerd 的地址,要么是 dockerd 自己拉起的 managed containerd 的地址)。
- **`containerd.New(...)`**——这是 containerd 官方客户端库的构造函数。dockerd 用它**拨号连上 containerd 的 gRPC socket**。
- 注意 import:`containerd "github.com/containerd/containerd/v2/client"`——docker 用的是 **containerd v2** 的客户端。docker 和 containerd 的版本是配套演进的。

**到这一步,dockerd 启动完成,它左手握着 registryService(自己管镜像仓库),右手握着 containerdClient(连上了 containerd)**。后面任何 `docker run`,都是这两个客户端协同干活。

### 第三站:`ContainerStart` 收到请求,转发给 containerd

当用户敲 `docker run`,CLI 发 `POST /containers/{id}/start`,router 转到 `postContainersStart`,它调 `c.backend.ContainerStart`,落到 `Daemon.ContainerStart`([moby/moby `daemon/start.go`](https://github.com/moby/moby/blob/master/daemon/start.go#L49))。`Daemon.ContainerStart` 做完前置检查(容器状态、网络、cgroup),最终在内部 `containerStart` 方法里,**把启动这个容器的活儿外包给 containerd**([moby/moby `daemon/start.go`](https://github.com/moby/moby/blob/master/daemon/start.go#L197)):

```go
ctr, err := libcontainerd.ReplaceContainer(ctx, daemon.containerd, container.ID, spec, shim, createOptions, func(ctx context.Context, client *containerd.Client, c *containers.Container) error {
    // Only set the image if we are using containerd for image storage.
    // This is for metadata purposes only.
    // Other lower-level components may make use of this information.
    is, ok := daemon.imageService.(*mobyc8dstore.ImageService)
    if !ok {
        return nil
    }
    img, err := is.ResolveImage(ctx, container.Config.Image)
    ...
```

读这一行——`libcontainerd.ReplaceContainer(ctx, daemon.containerd, ...)`:

- **`daemon.containerd`**:就是前面 Daemon 结构体 126 行那个封装层(不是原始的 `containerdClient`)。docker 用自己的封装层调 containerd,这一层会进一步把请求翻译成 containerd v2 的 gRPC 调用。
- **`spec`**:这是 docker 根据用户参数(命令、环境变量、cgroup 限制、挂载……)生成的那份 OCI runtime-spec——第 8 章讲过,这是整套生态的"标准集装箱图纸"。docker 在这一步把它交给 containerd,containerd 再交给 shim,shim 再交给 runc,runc 照着它砌容器。
- **那个回调函数**(`func(ctx, client *containerd.Client, c *containers.Container) error`):这是 docker 在 containerd 创建容器记录时,顺手往里塞"这个容器用的是哪个镜像"的元数据。注意那段注释 "Only set the image if we are using containerd for image storage"——这暴露了 docker 一个正在进行的演进:**docker 的镜像存储正从"自己那套(graphdriver)"迁移到"用 containerd 的 content store"**。这也是 docker 进一步瘦身、把更多底层职责让给 containerd 的又一证据。

> **从这一行往下,就是第 10 章的全部内容**:`daemon.containerd` 把请求送到 containerd daemon → containerd fork 一个 shim → shim 通过 runc 客户端 exec runc → runc 建 namespace、设 cgroup、pivot_root、exec → 内核 `clone(CLONE_NEW*)` → nginx 进程跑起来,父进程是 shim。**docker 在这条链上,只是最上面那一层"前台",它把请求递进去,就退场了**。

### 第四站:把整条链画出来

把这一节四站串起来,`docker run nginx` 启动容器那一刻的源码路径是这样的:

```
docker CLI: ContainerStart()           [docker/cli client]
    │ HTTP POST /containers/{id}/start
    ▼
dockerd router: postContainersStart     [moby/moby daemon/server/router]
    │ c.backend.ContainerStart(...)
    ▼
Daemon.ContainerStart → containerStart  [moby/moby daemon/start.go:49,76]
    │ 前置检查、生成 OCI spec
    │ libcontainerd.ReplaceContainer(ctx, daemon.containerd, ...)
    ▼
containerd daemon 收到 gRPC 调用        [第 10 章主讲]
    │ fork shim
    ▼
containerd-shim                          [第 10 章主讲]
    │ exec runc
    ▼
runc create + start                      [第 9 章主讲]
    │ clone(CLONE_NEW*)、cgroup、pivot_root、exec
    ▼
内核 + nginx 进程                        [第 1~5 章主讲]
```

**每一行代码都对应一个明确的设计决策**:HTTP 解耦、backend 接口分层、containerd 封装层、OCI spec 标准化、shim 解耦 daemon、runc 一次性施工……没有一行是冗余的。这就是 docker 这套"五层调用链"的工程美感。

---

## 章末小结

### 用航运比喻回顾本章

回到港口。这一章我们认识了**港口服务中心**(docker)——它不是一台新设备,而是开在港口管理公司(containerd)门脸上的一站式服务中心。

1. **它的前台是 docker CLI**:你说一句"我要一箱 nginx",前台(CLI)把你的话填成标准单子(`POST /containers/create`),递进后面的办公室(dockerd)。前台不干活,只翻译。
2. **它的后台是 dockerd**:夹在 HTTP(给 CLI)和 containerd gRPC(给底层)之间,做"翻译枢纽"。它上接人性化的 API,下接机器化的 API;它还顺手管了 containerd 不管的一堆事——镜像仓库认证、网络、卷、Dockerfile 构建、Compose——正是这些把 containerd 的"零件"组装成了 docker 的"整机"。
3. **它有两个老毛病**:daemon 模式(dockerd 是单点常驻,早期容器还是它的子进程)和默认 root 权限(那把危险的 `/var/run/docker.sock`)。这俩包袱是 docker"好用"换来的代价,也是后来 containerd 独立、CRI 诞生、k8s 弃用 dockershim 的导火索。
4. **它今天已经瘦身成 containerd 的壳**:docker 把 runc(2015 捐 OCI)、containerd(2017 捐 CNCF)一层层捐了出去;今天你装的 dockerd,启动时会自动拉起一个 managed containerd 再连上去。**docker 早已不是 2013 年那个包打天下的单体,它是 containerd 上面那个最受欢迎的"前台"**。

### 本章在全书主线中的位置

回到全书的二分法:**打包隔离 vs 调度编排**。

- 这一章是**"打包隔离"这半本(docker 半本)的收尾章**。从第 1 章的"容器就是个进程",到第 2~5 章的三块基石(namespace/cgroup/rootfs/overlayfs),到第 6 章手搓容器,到第 7~9 章的镜像和 runc,到第 10 章 containerd,再到这一章 docker——**我们终于把"一个应用怎么被打包、隔离、跑起来、被管理"这一整条旅程走到了头**。docker 是这条旅程最上面那个把所有零件包装成产品的"前台",它是普通开发者接触容器的入口。
- **底下,它仍然回扣内核**:docker 五层调用链的最底下,还是第 1~5 章讲的 namespace(`clone` 标志位)、cgroup(写文件)、rootfs(pivot_root)、overlayfs(分层挂载)。docker 一个零件都没造,它造的是"怎么把这些零件组装好用"。
- **它也指明了后半本的方向**:docker 的两个包袱(daemon 单点、root 权限)和它"单机"的天花板,正是 k8s 出现的理由。当容器从"一台机器上跑几个"变成"几百台机器上跑几万个",docker 这套"前台式"管理就力不从心了——需要的是"声明期望状态 + 自动调度 + 自愈"的全新范式。**第 14 章《编排为什么必须存在》会从这里接着讲**。

而在去 k8s 之前,还有一件 docker 留下的、我们一直没拆开的事——**容器网络**。`docker run` 默认就给容器接好了网、分好了 IP,这底下到底发生了什么?第 12 章我们就钻进那个"小区之间的路"。

### 五个"为什么"清单

如果你只能记五件事,记这五件:

1. **`docker run nginx` 穿过了几层**:整整五层——docker CLI → dockerd → containerd → containerd-shim → runc → 内核。docker CLI 只是前台,dockerd 是翻译枢纽,真正砌容器的是底下的 containerd/shim/runc/内核。**docker 自己一个底层零件都没造**。

2. **docker 比 containerd 多做了什么**:主要是**用户体验**——一键开箱(`docker run` 自动拉镜像/配网络/生成 OCI spec)、Dockerfile 构建(指令=层)、镜像仓库生态(Docker Hub + registry 标准)、volume 和网络子系统、Compose。**containerd 给零件,docker 给整机**。

3. **docker 的两个历史包袱是什么**:**daemon 模式**(dockerd 是单点常驻,早期容器是它子进程,升级 docker = 重启业务)和**默认 root 权限**(`/var/run/docker.sock` 一旦泄露,等于把宿主 root 交出去)。这俩包袱是 docker"好用"的代价,也是 containerd 独立、CRI 诞生、k8s 弃用 dockershim 的导火索。

4. **docker 为什么把底层捐出去**:生态比产品值钱。docker 把 runc(2015 捐 OCI)、containerd(2017 捐 CNCF)捐成中立标准,把全行业拉上自己的船;自己则瘦身成"containerd 上面最好用的壳"。**"k8s 弃用 docker"弃用的只是 dockershim,不是 docker 产品本身**——docker 在开发者本地依然是容器工具之王。

5. **今天 dockerd 和 containerd 是什么关系**:dockerd 启动时会自动拉起一个 managed containerd(源码里那句 "starting managed containerd" 是铁证),再连上去。**docker 今天早就不"自己直接管容器"了,它只是 containerd 上面那一层**。理解 containerd,才是理解 docker 运行时本质的钥匙。

### 想继续深入,该往哪钻

- **亲手观测 docker 的运行时结构**:在装了 docker 的机器上,`ps -ef | grep -E 'dockerd|containerd|containerd-shim'`——你会看到 dockerd、它拉起的 managed containerd、以及每个容器一个的 shim。`pstree -p <dockerd_pid>` 能看清它们的父子关系(dockerd 是 containerd 的父进程,但容器进程的父进程是 shim 而非 dockerd)。
- **看 dockerd 怎么连/拉起 containerd**:[moby/moby `daemon/command/daemon.go`](https://github.com/moby/moby/blob/master/daemon/command/daemon.go#L1145-L1170)(`initializeContainerd`,本章引用的"starting managed containerd"那段)、[moby/moby `daemon/internal/libcontainerd/supervisor/remote_daemon.go`](https://github.com/moby/moby/blob/master/daemon/internal/libcontainerd/supervisor/remote_daemon.go#L146-L198)(`exec.Command` 拉起 containerd 二进制)。
- **看 dockerd 怎么把请求转发给 containerd**:[moby/moby `daemon/start.go`](https://github.com/moby/moby/blob/master/daemon/start.go#L49-L205)(`Daemon.ContainerStart` → `containerStart` → `libcontainerd.ReplaceContainer`),顺着 `daemon.containerd` 字段往下,看 docker 的 libcontainerd 封装层怎么翻译成 containerd v2 的 gRPC 调用。
- **看 docker CLI 怎么发 HTTP 请求**:[docker/cli `cli/command/container/run.go`](https://github.com/docker/cli/blob/master/cli/command/container/run.go#L87-L151)(`runRun` → `runContainer` → `createContainer` + `ContainerStart`)、[moby/moby `client/container_create.go`](https://github.com/moby/moby/blob/master/client/container_create.go#L18-L62)(`ContainerCreate` → `POST /containers/create`)。
- **想理解 docker 的演进和"为什么捐出去"**:可以读 CNCF 当年 containerd 接收公告(2017/03)、OCI 成立公告(2015)、以及 k8s 1.24 release notes 里"移除 dockershim"的说明——把这些时间点串起来,你会看清 docker 这十年战略转身的全貌。

---

> 港口服务中心(docker)讲完了:它有前台(CLI)、有后台(dockerd)、有镜像仓库生态、有 volume 和网络、有 Dockerfile 和 Compose;它背着 daemon 单点和 root 权限两个老包袱;它今天瘦身成了 containerd 上面那个最好用的壳。**docker 半本到此收尾**——一个应用怎么被装箱、隔离、跑起来、被管理,这条旅程我们走到了头。接下来,在去 k8s 之前,还有一件 docker 默默替你做了、但底下大有乾坤的事:**容器怎么有网?** 翻开 **第 12 章 · 容器网络基础:veth / bridge / iptables**。
