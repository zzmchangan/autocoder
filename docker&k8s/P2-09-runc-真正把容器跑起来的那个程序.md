# 第 9 章 · runc:真正把容器跑起来的那个程序

> **前置**:你需要先读过[第 8 章《OCI 标准:为什么要有运行时标准》](P2-08-OCI标准-为什么要有运行时标准.md)。上一章我们讲了 OCI 把容器世界切成两份规范——image-spec(镜像长什么样)和 runtime-spec(怎么把镜像跑成容器),并指出 low-level runtime(runc)和 high-level runtime(containerd)的分工。这一章我们就钻进 runc 的源码,看看它作为"低层运行时",到底是怎么把一份 `config.json` 变成一个真实跑着的容器进程的。

> **核心问题**:`docker run` 拆到底,是哪个程序真正把容器"砌"出来的?它干了哪几步?
>
> 你在键盘上敲下 `docker run nginx`,这条命令背后是一长串调用链——dockerd → containerd → containerd-shim → **runc**。前几层(第 10、11 章细讲)管的是镜像拉取、生命周期、日志,但**真正动手把"集装箱"吊到位、固定、通电**的,只有 runc 一个程序。这一章我们就盯住它,看清楚它从接到一份 `config.json` 到容器进程跑起来,中间到底走了哪几步、为什么这么走。
>
> **读完本章你会明白**:
> - runc 的职责链到底有哪几步:读 runtime-spec 的 `config.json` → 建 namespace → 设 cgroup → pivot_root 换根 → exec 容器进程,以及**这五步为什么是这个顺序**。
> - `runc create` 和 `runc start` 为什么是两条命令而不是一条——先 create 把沙箱/namespace/cgroup 建好但**暂停**,再 start 才放行业务进程。这背后是高层运行时(containerd)要在两步之间插网络配置、卷挂载的现实需求。
> - runc 的**双进程模型**:parent 进程用 `clone` 建好沙箱并管 cgroup/sync,child 进程才是真正的容器 init。这套机制由一段 C 写的 `nsexec` 协调——为什么非 C 不可(在 Go runtime 起来之前就要 setns/clone,Go 的多线程 runtime 会让 setns 失败)。
> - runc 把容器跑起来,用的全是第 1 篇讲过的内核能力(clone/namespace、写 cgroup 文件、pivot_root、execve),**没有任何新机制**——它只是把这套调用工业级地包好、标准化。

> **逃生阀**:如果一读觉得太难,先只记住三件事——① runc 的五步=create→建 ns→设 cgroup→pivot_root→exec;② `runc create` 把沙箱建好但暂停(等 FIFO),`runc start` 打开 FIFO 才放行,这样高层运行时能在两步之间插活;③ runc 跑容器要分两个进程(parent 管 cgroup/网络,child 才是真容器进程),协调它俩的是一段 C 写的 `nsexec`,因为 setns/clone 必须在 Go runtime 起来之前做。

---

## 章首·`docker run` 拆到底是谁干的

第 6 章我们做过一件事:用 100 行 Go,自己 `clone(CLONE_NEW*)` + 写 cgroup 文件 + `pivot_root` + `exec`,亲手搓出了一个能跑 busybox 的容器。那一刻你应该已经确信:**容器没有任何神秘力量,它就是几个系统调用的组合**。

那 docker / containerd / k8s 这一整套生态,干嘛还要这么多层?既然 100 行能搓出容器,工业界为什么不用我们的 100 行?

答案是:**100 行能跑出"能跑的容器",但跑不出"在所有奇怪环境下都安全、都正确、都符合标准、都能被上层调度"的容器。** 工业界需要一个**最小、标准化、只干一件事**的底层运行时——它只负责"按照 OCI runtime-spec 把一个容器跑起来,别的一概不管"。这个程序,就是 runc。

> **比喻**:回到那片港口。前面几章我们手搓过一个简陋的集装箱——能用,但只能在我们这台机器、这个内核、这种 rootfs 上用。工业港口需要的是一台**标准起重机**:不管什么货、什么船、什么调度公司派的单,这台起重机都按同一套规范作业——**把集装箱从堆场吊到甲板指定位置、用锁扣固定、通电检测、然后松钩**。它不关心货是什么、不关心船开去哪、不关心谁来调度——它只关心"按规范把一个集装箱装上船"这件事做到极致。
>
> 这台标准起重机,就是 runc。containerd 是港口管理公司(第 10 章),docker 是码头(第 11 章),k8s 是全球调度中心(第 5~6 篇)——它们都站在 runc 之上,各自加自己那一层的价值。**但真正"吊箱子"的手,只有 runc 一双。**

这一章,我们就拆开这台起重机,看它从接到一份作业单(`config.json`)到松钩(容器进程 exec 起来),内部到底转了几道弯。

---

## 一、先看入口:runc 是个 CLI 程序

runc 是一个独立的可执行文件,用 Go 写的,装上之后命令行就能调。它最常见的三个子命令,正好对应容器的三种使用姿势:

- `runc run <id>` —— 一条命令搞定(create + start,前台跑,退出后清理)。
- `runc create <id>` + `runc start <id>` —— 两步走(先建好沙箱暂停,再启动业务),后台跑。
- `runc exec <id> <cmd>` —— 在已经跑着的容器里再起一个进程(比如 `runc exec mybox ls`)。

我们这一章重点讲前两种,因为它们覆盖了"把一个容器从零跑起来"的完整路径。`runc exec` 走的是另一条分支(setns 而非 clone),机制类似但细节不同,本章末尾会点一句。

### 三个子命令在源码里就是三个 cli.Command

runc 的命令行用 [`urfave/cli`](https://github.com/urfave/cli) 这个库组织。看 runc 仓库根目录的三个文件:

**`runc run`**([run.go](../runc/run.go#L11-L89)):

```go
var runCommand = &cli.Command{
	Name:  "run",
	Usage: "create and run a container",
	// ... 一堆 flags ...
	Action: func(_ context.Context, cmd *cli.Command) error {
		if err := checkArgs(cmd, 1, exactArgs); err != nil {
			return err
		}
		status, err := startContainer(cmd, CT_ACT_RUN, nil)
		if err == nil {
			os.Exit(status)
		}
		return fmt.Errorf("runc run failed: %w", err)
	},
}
```

**`runc create`**([create.go](../runc/create.go#L11-L75)):

```go
var createCommand = &cli.Command{
	Name:  "create",
	Usage: "create a container",
	// ... 一堆 flags ...
	Action: func(_ context.Context, cmd *cli.Command) error {
		if err := checkArgs(cmd, 1, exactArgs); err != nil {
			return err
		}
		status, err := startContainer(cmd, CT_ACT_CREATE, nil)
		// ...
	},
}
```

**`runc start`**([start.go](../runc/start.go#L13-L57)):

```go
var startCommand = &cli.Command{
	Name:  "start",
	Usage: "executes the user defined process in a created container",
	Action: func(_ context.Context, cmd *cli.Command) error {
		if err := checkArgs(cmd, 1, exactArgs); err != nil {
			return err
		}
		container, err := getContainer(cmd)
		// ...
		switch status {
		case libcontainer.Created:
			notifySocket, err := notifySocketStart(cmd, os.Getenv("NOTIFY_SOCKET"), container.ID())
			// ...
			if err := container.Exec(); err != nil {
				return err
			}
			// ...
		case libcontainer.Stopped:
			return errors.New("cannot start a container that has stopped")
		case libcontainer.Running:
			return errors.New("cannot start an already running container")
		}
	},
}
```

读这三段,三件事跳出来:

1. **`run` 和 `create` 共用同一个入口 `startContainer`**,只是传的 action 不同(`CT_ACT_RUN` vs `CT_ACT_CREATE`)。说明它俩底层是同一套机制,区别只在"要不要等用户再敲一次 start"。
2. **`start` 走的是完全另一条路**——它 `getContainer` 把**已经 create 好的容器**从状态文件里加载回来,检查它处于 `Created` 状态,然后调 `container.Exec()` 放行业务进程。注意 `Exec` 这个名字有点反直觉:它**不是** exec 一个新程序,而是"触发那个早就 create 好、正阻塞在 FIFO 上的容器 init 继续往下走"——后面会讲这个 FIFO 机制。
3. **`run` = `create` + `start` 的合体**:在 `startContainer` 内部,`CT_ACT_RUN` 走 `r.container.Run(process)`(create 之后立刻 exec),`CT_ACT_CREATE` 走 `r.container.Start(process)`(create 之后停在 FIFO 等待)。

把这一切串起来的是 [`utils_linux.go` 的 `startContainer`](../runc/utils_linux.go#L381-L432):

```go
func startContainer(cmd *cli.Command, action CtAct, criuOpts *libcontainer.CriuOpts) (int, error) {
	if err := revisePidFile(cmd); err != nil {
		return -1, err
	}
	spec, err := setupSpec(cmd)            // ① 读 config.json(runtime-spec)
	// ...
	id := cmd.Args().First()
	// ...
	container, err := createContainer(cmd, id, spec)   // ② spec → libcontainer 内部 config
	// ...
	r := &runner{
		enableSubreaper: !cmd.Bool("no-subreaper"),
		shouldDestroy:   !cmd.Bool("keep"),
		container:       container,
		// ...
		action:          action,
		init:            true,
	}
	return r.run(spec.Process)             // ③ 真正跑
}
```

读这段,你会看到 runc 接到一条命令后的前三步:**读 spec(`setupSpec`)→ 把 spec 翻译成 libcontainer 内部配置(`createContainer`)→ 交给 `runner.run()` 真正启动**。

> 这里第一次出现了一个**贯穿全章的关键角色**:`config.json`,也就是 OCI runtime-spec 的标准格式文件。`runc create`/`runc run` 都要求你在当前目录(或 `--bundle` 指定的目录)下放一份 `config.json`,它描述了"这个容器要哪些 namespace、限多少内存、跑什么命令、挂哪些卷"。**runc 干的所有事,都是在执行这份 spec。** 第 8 章讲了 spec 的字段含义,本章关心的是:runc 怎么把这份静态的 JSON,变成一个活的容器进程。

接下来我们钻进 `runner.run()` 和它调用的 `container.Start()`,看真正"建沙箱"的过程。

---

## 二、runc 的五步职责链:从 config.json 到容器进程

把 runc 干的事抽象出来,它的"职责链"非常清晰——**五步,每一步都对应第 1 篇某一章讲过的内核能力**:

| 步骤 | runc 干什么 | 底层是哪个内核能力 | 对应章节 |
|------|-----------|------------------|---------|
| ① 读 spec | 读 `config.json`,翻译成内部配置 | (无,纯 Go 解析) | 第 8 章 |
| ② 建 namespace | 用 `clone(CLONE_NEW*)` 把容器进程塞进新隔离世界 | namespace | 第 2 章 |
| ③ 设 cgroup | 给容器进程建 cgroup、写配额、把 PID 钉进去 | cgroup | 第 3 章 |
| ④ pivot_root 换根 | 把根目录换成 rootfs | mount namespace + pivot_root | 第 4、5 章 |
| ⑤ exec 业务进程 | `execve` 把容器进程镜像换成用户指定的命令 | 进程篇 execve | 内核进程篇 |

> **这张表是本章的骨架。** 注意两件事:
>
> 1. **后四步全是第 1 篇讲过的内核机制**——runc 没有发明任何新能力。这又一次印证第 1 章的第一性原理:容器就是内核能力的组合,runc 只是把这套组合标准化、产品化。
> 2. **②③④⑤ 的顺序是有讲究的**(下面每一步都会解释"不这样会怎样")。这个顺序不是 runc 拍脑袋定的,是这套机制本身的依赖关系决定的——比如必须先 `pivot_root` 再 `exec`(否则 exec 找不到 rootfs 里的程序)。

下面把每一步拆开讲。

### 步骤 ②:建 namespace —— clone 出一个隔离世界

容器要隔离,第一件事就是把它的进程"塞进一套新的 namespace"。我们在第 6 章手搓时,是用 Go 的 `exec.Command` + `SysProcAttr.Cloneflags` 把 `CLONE_NEW*` 标志位传给 `clone`。

runc 也一样,但它把这件事拆得更细:它**不是直接 clone 出容器进程,而是先 fork-exec 出一个 "runc init" 子进程,再由这个子进程在内部用 C 代码 clone 出真正的容器进程**。这就是本章标题里说的"双进程模型",我们在第四节专门讲。这里先记住:**parent 进程(runc 主进程)负责 fork-exec 出 "runc init",然后 "runc init" 内部的 C 代码(`nsexec`)再 clone 出真正的容器进程**。

parent 侧 fork-exec "runc init" 的代码在 [`container_linux.go` 的 `newParentProcess`](../runc/libcontainer/container_linux.go#L516-L643):

```go
func (c *Container) newParentProcess(p *Process) (parentProcess, error) {
	comm, err := newProcessComm()
	// ...
	// 关键:runc 用 /proc/self/exe 重新执行自己,参数是 "init"
	cmd := exec.Command(exePath, "init")
	// ...
	cmd.Env = append(cmd.Env, "GOMAXPROCS="+os.Getenv("GOMAXPROCS"))
	// 把一堆 fd 通过 ExtraFiles 塞给 runc init,用环境变量告诉它每个 fd 是几号
	cmd.ExtraFiles = append(cmd.ExtraFiles, comm.initSockChild)
	cmd.Env = append(cmd.Env,
		"_LIBCONTAINER_INITPIPE="+strconv.Itoa(stdioFdCount+len(cmd.ExtraFiles)-1))
	cmd.ExtraFiles = append(cmd.ExtraFiles, comm.syncSockChild.File())
	cmd.Env = append(cmd.Env,
		"_LIBCONTAINER_SYNCPIPE="+strconv.Itoa(stdioFdCount+len(cmd.ExtraFiles)-1))
	// ...
}
```

注意第 550 行那句 `cmd := exec.Command(exePath, "init")`——**runc 主进程把自己(`/proc/self/exe`)重新执行一遍,带一个 `init` 参数**。这个 `init` 子进程不是容器进程,而是"runc 的另一个化身",它的任务是在新的 namespace 里把容器进程跑起来。

> **为什么要 re-exec 自己,而不是直接 clone 出容器进程?** 因为 runc 主进程是个**多线程的 Go 程序**(Go runtime 自带 GC、调度器,天生多线程),而 `setns`(2)在多线程进程里是**不允许的**——内核会拒绝,因为多线程进 namespace 会破坏隔离的一致性。所以 runc 必须 fork-exec 出一个**全新的、单线程的**子进程,在那个子进程里做 namespace 相关的事。这个子进程既然是全新的,最省事的办法就是**重新执行 runc 自己**(它已经链接好了所有需要的代码),带个 `init` 参数让它跑另一条代码路径。后面第四节讲 nsexec 时还会再回到这一点。

### 步骤 ③:设 cgroup —— 给容器进程限资源

namespace 只解决"看不见",没解决"用多少"。第二步是给容器进程建 cgroup、写配额。

这一步在 parent 侧做——[`process_linux.go` 的 `initProcess.start()`](../runc/libcontainer/process_linux.go#L777-L868):

```go
func (p *initProcess) start() (retErr error) {
	defer p.comm.closeParent()
	err := p.cmd.Start()          // ← 这里 fork-exec 出 "runc init" 子进程
	p.process.ops = p
	p.comm.closeChild()
	// ...
	// Do this before syncing with child so that no children can escape the
	// cgroup. We don't need to worry about not doing this and not being root
	// because we'd be using the rootless cgroup manager in that case.
	if err := p.manager.Apply(p.pid()); err != nil {     // ← 把 "runc init" 的 PID 钉进 cgroup
		// ...
	}
	tryResetCPUAffinity(p.pid())
	// ...
	if _, err := io.Copy(p.comm.initSockParent, p.bootstrapData); err != nil {  // ← 把配置塞给 runc init
		return fmt.Errorf("can't copy bootstrap data to pipe: %w", err)
	}
	childPid, err := p.getChildPid()   // ← 等 runc init 把真正容器进程的 PID 报回来
	// ...
	if err := p.waitForChildExit(childPid); err != nil {   // ← 等 "runc init" 的中间进程退出
		// ...
	}
	// ...
}
```

读这段,几个关键点:

- **`p.cmd.Start()`** 才是真正 fork-exec 出 "runc init" 子进程的地方。从这一刻起,系统里多了一个 runc 的化身进程,它在新的 namespace 里(因为 `cmd` 的 `SysProcAttr` 已经设好了 clone flags)。
- **`p.manager.Apply(p.pid())`** 把 "runc init" 的 PID 钉进 cgroup。注释里那句 **"Do this before syncing with child so that no children can escape the cgroup"**(在和孩子同步之前做,免得子进程逃出 cgroup)是个安全要点——"runc init" 还没 fork 出容器进程之前就先把它钉进 cgroup,这样它后续 fork 出来的所有进程(包括真正的容器进程)**天生就在这个 cgroup 里**,谁也别想逃。我们在第 6 章手搓时是先 clone 再加 PID,有个"裸奔窗口";runc 通过"先钉 runc init、再让 runc init fork"消除了这个窗口。
- **`getChildPid()`** 是个有意思的同步点:parent 在这里**阻塞等**,"runc init" 内部的 C 代码(nsexec)在 stage-1 → stage-2 之后,会把真正容器进程的 PID 通过管道报回来。parent 拿到这个 PID 才知道"哦,真正的容器进程是这个号",后面才能管它(转发信号、wait 等)。
- **`waitForChildExit(childPid)`** 等 "runc init" 的**中间 stage 进程**退出——因为双进程模型里有三个进程(stage-0/stage-1/stage-2),最终只有 stage-2(真正的容器进程)留下来,前面两个是过渡。parent 要把 stage-1 的"尸"收掉,然后把 `p.cmd.Process` 替换成 stage-2 的 PID,这样 parent 后续对 `p.cmd.Process` 的操作(信号、wait)实际作用在容器进程上。

> **不这样(不在 sync 之前 Apply cgroup)会怎样**:容器进程在 Apply 之前是不受限的。如果它在这段时间 fork 出孙子进程,孙子可能就不在这个 cgroup 里——一个恶意容器能借此**逃出资源限额**。runc 通过"先钉 runc init、再 fork"保证了**容器进程及其所有后代,从出生的第一纳秒起就在 cgroup 里**。这是工业级运行时和手搓版的本质差距之一。

具体的配额写入(`memory.max`、`cpu.max` 等)在 `manager.Apply` 之下,由 [cgroups/fs2/](../runc/vendor/github.com/opencontainers/cgroups/fs2/) 一堆文件做。这部分第 6 章对照过 runc 的预检和兼容处理(写 `memory.max` 前先 `CheckMemoryUsage`、v1↔v2 换算 swap 等),本章不重复——你只需要记住:**parent 侧的 cgroup 工作就是"建目录 + 加 PID + 写配额文件",全是第 3 章讲过的事**。

### 步骤 ④:pivot_root 换根 —— 让容器看到自己的文件系统

到这一步,容器进程已经在新的 namespace 里、cgroup 也钉好了,但它的**根目录还是宿主的 `/`**——它 `ls /` 看到的还是宿主的文件系统。换根这件事,**必须在容器进程(child)侧做**,因为 parent 不在容器的 mount namespace 里。

child 侧的换根在 [`standard_init_linux.go` 的 `Init()`](../runc/libcontainer/standard_init_linux.go#L51)(我们第 6 章已经对照过这个函数,这里只看和换根相关的脉络):

```go
func (l *linuxStandardInit) Init() error {
	// ... 一堆 keyring / 网络 / SELinux / console 准备 ...

	err := prepareRootfs(l.pipe, l.config)   // ← 准备 rootfs(挂 overlay、bind mount 卷等)
	if err != nil {
		return err
	}

	// ... console、路由、hostname、AppArmor、sysctl、maskPaths、readonlyPaths ...

	// prepareRootfs 内部最终会调 pivotRoot(或 msMoveRoot / chroot)
	// 把根目录换成 l.config.Rootfs
	// ...
}
```

`prepareRootfs` 内部做的事情我们在第 4 章和第 6 章都讲过——它先按 spec 把所有 mount(包括 overlayfs 的 lower/upper/merged、各种 bind mount、/proc、/sys)挂好,最后调 [`rootfs_linux.go` 的 `pivotRoot`](../runc/libcontainer/rootfs_linux.go#L1144-L1195) 把根换成 rootfs。

> **为什么换根必须在 child 侧、在 namespace 里做?** 因为换根会改变整个 mount namespace 的视图。如果 parent(在宿主 mount namespace 里)换根,会影响宿主上所有进程的 `/`——这是灾难。所以必须**先 clone 进新的 mount namespace,再在里面换根**。这也是步骤 ②(建 namespace)必须在 ④(换根)之前的根本原因——**换根是 mount namespace 内的事,没有 mount namespace 就没有"自己的根可换"**。

### 步骤 ⑤:exec 业务进程 —— 容器正式诞生

到这一步,沙箱建好了(namespace、cgroup、rootfs 全就位),但容器进程本身还是 "runc init" 的 stage-2 化身——它没在跑用户的 nginx,它在跑 runc 的初始化代码。最后一步,是把这个进程的镜像换成用户指定的命令:

```go
// standard_init_linux.go 的 Init() 末尾(L266-L305)
fifoFile, err := pathrs.Reopen(l.fifoFile, unix.O_WRONLY|unix.O_CLOEXEC)
// ...
if _, err := fifoFile.Write([]byte("0")); err != nil {   // ← 等 FIFO 被外部打开才能写
	return &os.PathError{Op: "write exec fifo", Path: fifoFile.Name(), Err: err}
}
// ...
if err := utils.UnsafeCloseFrom(l.config.PassedFilesCount + 3); err != nil {   // ← 关掉所有不该传给容器的 fd(CVE-2024-21626 防护)
	return err
}
return linux.Exec(name, l.config.Args, l.config.Env)   // ← 真正的 execve,容器进程诞生
```

最后那行 `linux.Exec(name, l.config.Args, l.config.Env)` 就是 `execve` 系统调用——**把当前进程的内存镜像换成用户在 `config.json` 里指定的命令(比如 `/usr/sbin/nginx`)**。从这一刻起,runc init "死了",nginx 接管了它的一切(PID、namespace、cgroup、rootfs 全部保留),**容器进程正式诞生**。

注意 `fifoFile.Write([]byte("0"))` 这一行——它不是装饰,而是 **`runc create`/`runc start` 两段式的核心机制**。下一节专门讲。

---

## 三、`runc create` 和 `runc start` 为什么分开

如果你用过 docker,你大概只敲过 `docker run`——一条命令开箱即用。但 runc 这个底层运行时,**故意把"跑容器"拆成了两条命令**:`runc create` 和 `runc start`。为什么?

### 不这样会怎样:高层运行时需要"插队的窗口"

想象一个真实场景:containerd 接到 k8s kubelet 的指令"起一个 nginx Pod"。这个 Pod 要:

1. **有自己的 IP**(CNI 插件要给容器配网卡、分 IP、写路由)。
2. **挂载持久化卷**(CSI 插件要把远程存储接到容器里)。
3. **配一堆 iptables 规则**(kube-proxy 要写 service 规则)。

这些事,**都得在容器进程跑起来之前做完**。如果容器进程一上来就 `exec nginx`,nginx 立刻开始监听端口、读配置——可这时候网卡还没配好、卷还没挂上,nginx 要么报错、要么用了一个错误的网络环境跑起来,后面再补救就来不及。

> **不这样(一条命令 create+start 合体)会怎样**:高层运行时(containerd)就会**没有窗口**插手。容器进程要么在网络没配好之前就跑起来(出错),要么高层运行时得**杀掉重来**(慢、不可靠)。所以必须给高层运行时**留一个"沙箱建好但业务没跑"的中间态**,让它在这个中间态里从容地把网络、卷、iptables 都配好,然后再放行业务进程。

这个"中间态",就是 `runc create` 和 `runc start` 之间的窗口。

### 怎么实现:一个 FIFO 把容器进程"卡"在 exec 之前

runc 的做法极其巧妙——它用一个 **FIFO(命名管道)** 把容器 init 进程**卡在 exec 之前**:

1. **`runc create` 时**:parent 在状态目录里建一个 FIFO([`createExecFifo`](../runc/libcontainer/container_linux.go#L467-L491) 用 `mkfifo`),把这个 FIFO 的 fd 通过环境变量 `_LIBCONTAINER_FIFOFD` 传给 runc init。runc init 在 `Init()` 里把所有沙箱都建好之后,**不立刻 exec**,而是 `pathrs.Reopen(l.fifoFile, ...)` 打开这个 FIFO 准备往里写——但 **FIFO 的写端会阻塞,直到有进程打开读端**。于是 runc init 就这么**卡在 `fifoFile.Write` 上,既没死也没跑业务**,等待外部来"放行"。

2. **`runc start` 时**:用户(或 containerd)敲 `runc start`,它走的是 [`start.go` 里的 `container.Exec()`](../runc/start.go#L36-L48) 路径。`Exec` 这个名字(再说一次,反直觉)**不是 exec 一个新程序**,而是去**打开那个 FIFO 的读端**——一旦读端被打开,runc init 那头的 `fifoFile.Write` 立刻解除阻塞,接着往下走,关 fd、execve 业务进程。**容器从此跑起来**。

看 parent 侧 `exec` 的实现([`container_linux.go`](../runc/libcontainer/container_linux.go#L233-L261)):

```go
func (c *Container) exec() error {
	path := filepath.Join(c.stateDir, execFifoFilename)
	if err := handleFifo(path, c.initProcess.pid()); err != nil {
		return err
	}
	return c.postStart()
}

func handleFifo(path string, pid int) error {
	blockingFifoOpenCh := awaitFifoOpen(path)        // ← 阻塞打开 FIFO 读端
	for {
		select {
		case result := <-blockingFifoOpenCh:
			return handleFifoResult(result)
		case <-time.After(time.Millisecond * 100):
			stat, err := system.Stat(pid)            // ← 顺便检查容器 init 是不是已经死了
			if err != nil || stat.State == system.Zombie {
				// ...
			}
		}
	}
}
```

`awaitFifoOpen` 起 goroutine 去 `os.OpenFile(path, os.O_RDONLY, 0)`(阻塞打开)——这一行**就是放行信号**。它一返回,runc init 那头的 Write 就解锁,业务进程跑起来。

### 三个状态:create / created / running

容器在 runc 眼里有三个状态,正好对应这个两段式:

- **`creating`**:`runc create` 在跑,parent 正在建沙箱,child 正在准备 rootfs。
- **`created`**:沙箱建好了,容器 init 卡在 FIFO 上,等放行。**这是高层运行时插手配网络/卷的窗口。**
- **`running`**:`runc start` 打开了 FIFO,容器 init execve 业务进程,正式跑起来。

> `runc run` 就是把这三个状态一口气走完——create 之后立刻自己 exec,不停在 created 状态。看 [utils_linux.go 的 `runner.run`](../runc/utils_linux.go#L223-L327):
>
> ```go
> switch r.action {
> case CT_ACT_CREATE:
>     err = r.container.Start(process)   // ← 只 Start,停在 created
> case CT_ACT_RUN:
>     err = r.container.Run(process)     // ← Run = Start + 立刻 Exec
> // ...
> }
> ```
>
> 而 [`Container.Run`](../runc/libcontainer/container_linux.go#L214-L224) 的实现就是:
>
> ```go
> func (c *Container) Run(process *Process) error {
>     c.m.Lock()
>     defer c.m.Unlock()
>     if err := c.start(process); err != nil {
>         return err
>     }
>     if process.Init {
>         return c.exec()    // ← Start 之后立刻 exec,不等用户敲 start
>     }
>     return nil
> }
> ```

### 谁用两段式,谁不用

- **containerd 一定用两段式**(它要在 created 状态插 CNI/CSI)。
- **docker 也用两段式**(底层走 containerd)。
- **k8s kubelet 间接用**(它通过 CRI 调 containerd,containerd 再调 runc)。
- **`runc run` 自己**(命令行直跑)跳过两段式,因为它是给人 demo 用的,没有"高层运行时要插手"这回事。

> **比喻**:起重机的标准作业流程分两段——**先把集装箱吊到甲板指定位置、用锁扣固定、接上电源检测**(=`runc create`,箱子到位但没通电运行),**然后等港口调度确认"网络、管线、配载都 OK 了",起重机才松钩通电**(=`runc start`,箱子正式运作)。这个"等调度确认"的窗口,就是为了让港口管理公司(containerd)来得及在通电前把所有外围系统接好。**如果起重机一吊到位就立刻通电**,集装箱里的制冷设备可能发现"电源没接对、网络没通",直接报错停机。

---

## 四、双进程模型:为什么跑一个容器要分三个进程

到这儿你可能会困惑:前面老说"双进程模型",可仔细看又出现了 stage-0 / stage-1 / stage-2 三个进程?到底是两个还是三个?

先把概念理清:

- **从 runc 主进程(parent)的角度看,它只直接 fork-exec 出一个 "runc init" 子进程**——所以叫"双进程"(parent + runc init)。
- **但 "runc init" 内部的 C 代码(`nsexec`)为了正确地进 PID namespace 和 user namespace,会再 clone 两次**——所以从内核进程表看,实际上是三个进程:stage-0(原始 runc init)→ stage-1(中间过渡)→ stage-2(真正的容器 init)。

最终的容器进程是 stage-2。stage-0 和 stage-1 在做完各自的协调工作后都退出,只有 stage-2 留下来 exec 业务进程。

### 为什么一个 clone 不够,非要三个进程

读 [`nsexec.c` 里那段著名的吐槽](../runc/libcontainer/nsenter/nsexec.c#L800-L845),你会看到 runc 作者 Aleksa Sarai 自己都写得欲哭无泪("what has my life come to?")。三个进程不是 runc 想搞复杂,是**内核机制本身逼出来的**:

1. **PID namespace 只对孩子生效**——一个进程 `unshare(CLONE_NEWPID)` 之后,它**自己**还是在老的 PID namespace 里,只有它后续 fork 出来的孩子才进新 PID namespace。所以要进 PID ns,**必须 fork 一次**。

2. **user namespace 的 uid_map/gid_map 只能由父进程写**——子进程进了 user ns 之后,它没法给自己写映射(没权限)。所以必须有**一个父进程来给孩子写 `/proc/<child>/uid_map`**。

3. **stage-0 用 `clone_parent` 生的 stage-1 没法 `wait` 自己**——因为 `CLONE_PARENT` 让 stage-1 的父进程变成 stage-0 的父进程(也就是 runc 主进程),所以 stage-0 不能 `wait` stage-1,只能让 runc 主进程来 reap。这就要求 stage-1 再 fork 一个 stage-2(用普通 fork,这样 stage-1 能 wait stage-2),把 stage-2 的 PID 通过 stage-0 转交给 runc 主进程。

这三条加起来,逼出了三进程结构。看 `nsexec.c` 的 stage 切换主框架([`nsexec.c`#L847-L863](../runc/libcontainer/nsenter/nsexec.c#L847-L863)):

```c
switch (setjmp(env)) {
	/*
	 * Stage 0: We're in the parent. Our job is just to create a new child
	 *          (stage 1: STAGE_CHILD) process and write its uid_map and
	 *          gid_map. That process will go on to create a new process, then
	 *          it will send us its PID which we will send to the bootstrap
	 *          process.
	 */
case STAGE_PARENT:{
		// ...
		prctl(PR_SET_NAME, (unsigned long)"runc:[0:PARENT]", 0, 0, 0);
		write_log(DEBUG, "~> nsexec stage-0");

		/* Start the process of getting a container. */
		write_log(DEBUG, "spawn stage-1");
		stage1_pid = clone_parent(&env, STAGE_CHILD);   // ← 生 stage-1
		// ...
```

每个 stage 在 `prctl(PR_SET_NAME, ...)` 里给自己起了个名字(`runc:[0:PARENT]`、`runc:[1:CHILD]`、`runc:[2:INIT]`)——你在宿主上 `ps aux | grep runc:` 能直接看到这三个进程,这就是它们。

### 每个 stage 干什么

**Stage 0(原始 runc init,parent 角色)**([`nsexec.c`#L855-L996](../runc/libcontainer/nsenter/nsexec.c#L855-L996)):

- `clone_parent` 出 stage-1。
- 进入同步循环,**等 stage-1 请求**:stage-1 要写 uid_map/gid_map 时,它发 `SYNC_USERMAP_PLS`,stage-0 帮它写 `/proc/<stage1>/uid_map` 和 `gid_map`;stage-1 把 stage-2 的 PID 报上来时,它发 `SYNC_RECVPID_PLS`,stage-0 通过 `_LIBCONTAINER_INITPIPE` 把 `{"stage1_pid":X,"stage2_pid":Y}` 这个 JSON 发给 runc 主进程(主进程的 `getChildPid()` 就是在读这个)。
- stage-0 完成协调任务后,**自己 exit 退出**。

**Stage 1(第一个 child,过渡角色)**([`nsexec.c`#L1010-L1161](../runc/libcontainer/nsenter/nsexec.c#L1010-L1161)):

- 如果 spec 里有"加入已有 namespace"(比如 k8s Pod 里非 pause 容器加入 pause 的 network namespace),调 `join_namespaces` 用 `setns` 挤进去。
- 处理 user namespace:`unshare(CLONE_NEWUSER)`,然后**请求 stage-0 帮忙写 uid_map**(因为自己没权限写自己的 map)。
- `unshare` 剩下的 namespace(mount/pid/net/uts/ipc/cgroup)。
- `clone_parent` 出 stage-2(真正进 PID ns 必须靠 fork,因为 unshare PID ns 对自己无效)。
- 把 stage-2 的 PID 通过同步管道转给 stage-0,然后 **`exit(0)` 退出**。

**Stage 2(真正的容器 init)**([`nsexec.c`#L1169-L1225](../runc/libcontainer/nsenter/nsexec.c#L1169-L1225)):

- `setsid` 起一个新会话。
- `setuid(0)` / `setgid(0)` / `setgroups(0, NULL)` 完成身份切换。
- 通知 stage-0 "我好了"。
- **`return;` —— 让函数返回,Go runtime 接管**。从这一刻起,这个进程才真正"是"一个 Go 程序,开始跑 `init.go` 里的 Go `init()` 函数,进而跑 `libcontainer.Init()`,最终跑到我们前面讲的 `standard_init_linux.go` 的 `Init()`(准备 rootfs、设 cgroup、pivot_root、exec)。

### 用一张图把三进程 + parent 串起来

```
   runc 主进程 (parent, Go, 多线程)
      │
      │ fork-exec /proc/self/exe init   ← newParentProcess 里 cmd.Start()
      ▼
   "runc init" 进程 (单线程,即将进 namespace)
      │
      │ 进入 C 函数 nsexec() —— 这时 Go runtime 还没起来
      │
      ├──────────────────┬──────────────────┐
      │ Stage 0 (parent) │ Stage 1 (child)  │ Stage 2 (grandchild)
      │ runc:[0:PARENT]  │ runc:[1:CHILD]   │ runc:[2:INIT]   ← 最终容器进程
      │                  │                  │
      │ clone_parent ───►│                  │
      │                  │ clone_parent ───►│
      │                  │                  │
      │ 帮 stage1 写     │ unshare user ns  │ setsid + setuid(0)
      │ uid_map/gid_map  │ unshare 其他 ns  │ return → Go runtime 接管
      │ 接收 stage2 PID  │ 把 stage2 PID    │   ↓
      │ 通过 INITPIPE    │   转给 stage0    │ 跑 Go init() → libcontainer.Init()
      │   报给主进程     │ exit(0)          │   ↓
      │ exit             │                  │ standard_init.Init():
      │                  │                  │   prepareRootfs / pivotRoot /
      │                  │                  │   Sethostname / maskPaths /
      │                  │                  │   seccomp / finalizeNamespace /
      │                  │                  │   等 FIFO → execve(用户命令)
      │                  │                  │   = 容器进程诞生
      ▼                  ▼                  ▼
   getChildPid() 拿到 stage2 的 PID
   waitForChildExit(stage1) 收掉 stage1 的尸
   后续 parent 对 p.cmd.Process 的操作 → 实际作用于 stage2
```

这张图把"runc 主进程 → runc init → nsexec 三 stage → Go runtime → standard_init.Init → execve"整条链路画清楚了。**最终活下来的只有 stage-2,它就是容器里的 PID 1**。

### 为什么 nsexec 必须是 C 写的

整个章节最反直觉的一点:runc 是个 Go 程序,但它的核心初始化逻辑(namespace 协调)是**一段 C 代码**(`nsexec.c`)。为什么?

读 [`libcontainer/nsenter/README.md`](../runc/libcontainer/nsenter/README.md) 开头那几句就讲明白了:

> The `nsenter` package registers a special init constructor that is called before the Go runtime has a chance to boot. This provides us the ability to `setns` on existing namespaces and **avoid the issues that the Go runtime has with multiple threads**.

以及 [`nsenter.go`](../runc/libcontainer/nsenter/nsenter.go#L12-L15) 的注册方式:

```go
extern void nsexec();
void __attribute__((constructor)) init(void) {
	nsexec();
}
```

`__attribute__((constructor))` 是 GCC/Clang 的一个特性——**标记这个函数在 `main()` 之前、但在 C runtime 初始化之后自动执行**。在 cgo 编译的程序里,这个 constructor 会在 Go runtime 完全启动**之前**就跑。所以 runc init 子进程一被 fork-exec 出来,**最先执行的代码就是 `nsexec()`,而不是 Go 的 `main()`**。

为什么要赶在 Go runtime 之前?两个根本原因:

1. **Go runtime 是多线程的**(GC、调度器、网络 poller 都是独立线程)。而 `setns(2)` 在多线程进程里**会失败**——内核要求调用 `setns` 的进程必须是单线程(否则隔离语义无法保证:哪个线程进哪个 ns?)。所以必须在 Go runtime 起多线程之前就把 setns/clone 做完。
2. **`clone(CLONE_NEWPID)` 的语义是"对孩子生效"**,在 Go runtime 已经起来之后再 fork,Go runtime 那一堆线程的状态、信号处理器、tls 都得小心处理,极容易出 bug。在 Go runtime 起来之前用裸 C 的 `clone`(带 `clone_parent` 这种自定义栈和 jump buffer 的玩法),干净利落。

读 `nsexec.c` 里那两个 `clone` 实现的注释,你能感受到作者对"为什么不能用 Go 干这件事"的执念——比如 [`clone_parent` 函数](../runc/libcontainer/nsenter/nsexec.c#L314) 用 `setjmp/longjmp` 在父子进程间跳转(因为 clone 出来的子进程共享父的内存,直接 return 会两边都 return),这种精确到字节的栈控制,Go 这种带 GC 的语言根本做不到。

> [`runc/init.go`](../runc/init.go#L10-L16) 里有一句关键注释,把这个时序点破了:
>
> ```go
> func init() {
>     if len(os.Args) > 1 && os.Args[1] == "init" {
>         // This is the golang entry point for runc init, executed
>         // before main() but after libcontainer/nsenter's nsexec().
>         libcontainer.Init()
>     }
> }
> ```
>
> "before main() but after libcontainer/nsenter's nsexec()"——**Go 的 init() 在 main 之前,但都在 nsexec 之后**。所以执行顺序是:**cgo constructor(nsexec)→ Go init()→ Go main()**。nsexec 在最前面,等它把三 stage 都协调完、stage-2 return 出来之后,Go runtime 才开始跑,stage-2 才进入 Go 的世界去执行 `libcontainer.Init()` → `standard_init.Init()`。

> **比喻**:为什么起重机最底层的"对位传感器"和"锁扣控制器"用的是**老式的机械继电器电路**(C),而不是上层调度系统用的**电脑控制**(Go)?因为机械继电器反应是纳秒级、单线程、不会被 GC 打断——**在最底层的、和物理世界(nsid、pid)直接打交道的环节,你需要这种"绝对单线程、绝对可控"的工具**。等箱子物理上吊到位了,才能交给上层的电脑系统(Go runtime)去做"配载计算、作业记录"这些事。nsexec 是 runc 的机械继电器层。

---

## 关键源码精读:从 `runc create` 一路追到 execve

把前面讲的设计落到代码上,我们完整走一遍 `runc create` 的调用链。建议你打开 runc 源码对照着读。

### 第一段:命令行 → `startContainer`

入口在 [`create.go`#L63-L74](../runc/create.go#L63-L74),它调 [`utils_linux.go` 的 `startContainer`](../runc/utils_linux.go#L381-L432):

```go
func startContainer(cmd *cli.Command, action CtAct, criuOpts *libcontainer.CriuOpts) (int, error) {
	// ...
	spec, err := setupSpec(cmd)                    // ① 读 config.json,解析 runtime-spec
	// ...
	container, err := createContainer(cmd, id, spec)   // ② specconv: spec → libcontainer config
	// ...
	r := &runner{
		// ...
		action: action,
		init:   true,
	}
	return r.run(spec.Process)                      // ③ 真正跑
}
```

`createContainer` 里有一句关键的 [`specconv.CreateLibcontainerConfig`](../runc/utils_linux.go#L189-L200)——它把 OCI runtime-spec 的 JSON 翻译成 libcontainer 内部的 `configs.Config` 结构体。**这是 runc 对外的标准接口(spec)和对内的数据结构(internal config)之间的桥梁**。runtime-spec 是公开标准,libcontainer config 是 runc 内部实现细节,两者解耦——这是 OCI 标准化的价值(第 8 章讲过)。

### 第二段:`runner.run` → `container.Start`

[`utils_linux.go` 的 `runner.run`](../runc/utils_linux.go#L223-L327) 是分发器:

```go
func (r *runner) run(config *specs.Process) (_ int, retErr error) {
	// ...
	process, err := newProcess(config)             // 把 spec.Process 转成 libcontainer.Process
	// ...
	if r.enableSubreaper {
		if err := system.SetSubreaper(1); err != nil {   // ← 把自己设成 subreaper(第 6 章讲过)
			logrus.Warn(err)
		}
	}
	// ...
	switch r.action {
	case CT_ACT_CREATE:
		err = r.container.Start(process)            // ← create 走这里
	case CT_ACT_RUN:
		err = r.container.Run(process)              // ← run 走这里(Start + 立刻 Exec)
	// ...
	}
	// ...
}
```

注意 `system.SetSubreaper(1)`——runc 主进程把自己设成 subreaper,这样三 stage 协调过程中产生的孤儿进程(stage-1 退出后的 stage-2 会被 reparent)能被 runc 收尸。第 6 章对照过这个机制。

### 第三段:`Container.start` → 建 FIFO + fork-exec runc init

[`container_linux.go` 的 `Container.start`](../runc/libcontainer/container_linux.go#L339-L406) 是建沙箱的入口:

```go
func (c *Container) start(process *Process) (retErr error) {
	// ...
	if process.Init {
		if c.initProcessStartTime != 0 {
			return errors.New("container already has init process")
		}
		if err := c.createExecFifo(); err != nil {       // ← 建 FIFO(两段式的核心)
			return err
		}
		// ...
	}

	parent, err := c.newParentProcess(process)            // ← 构造 "runc init" 的 exec.Cmd
	// ...

	// CVE-2024-21626 防护:fork 前 mark 所有非 stdio fd 为 CLOEXEC
	if err := utils.CloseExecFrom(3); err != nil {
		return fmt.Errorf("unable to mark non-stdio fds as cloexec: %w", err)
	}
	// ...
	if err := parent.start(); err != nil {                // ← 这里真正 fork-exec
		return fmt.Errorf("unable to start container process: %w", err)
	}

	if process.Init {
		c.fifo.Close()                                    // ← parent 关掉自己持有的 FIFO fd(留给 runc init 用)
	}
	return nil
}
```

注意 `createExecFifo`([`container_linux.go`#L467-L491](../runc/libcontainer/container_linux.go#L467-L491))——它 `mkfifo` 在状态目录里建一个命名管道。这个 FIFO 是两段式的物理载体。`parent.start()` 走到 `initProcess.start()`(我们第二节贴过),那里 fork-exec 出 "runc init"。

### 第四段:`initProcess.start` → cgroup Apply + 同步

[`process_linux.go`#L777-L868](../runc/libcontainer/process_linux.go#L777-L868) 是 parent 侧的核心(前面贴过,这里只强调同步点):

```go
err := p.cmd.Start()                                     // ① fork-exec runc init
// ...
if err := p.manager.Apply(p.pid()); err != nil {         // ② 把 runc init 的 PID 钉进 cgroup
	// ...
}
// ...
if _, err := io.Copy(p.comm.initSockParent, p.bootstrapData); err != nil {  // ③ 把配置塞给 runc init
	// ...
}
childPid, err := p.getChildPid()                         // ④ 阻塞等 runc init 把真容器 PID 报回来
// ...
if err := p.waitForChildExit(childPid); err != nil {     // ⑤ 等 stage-1 退出,把 cmd.Process 换成 stage-2
	// ...
}
// ...
if err := utils.WriteJSON(p.comm.initSockParent, p.config); err != nil {    // ⑥ 把完整 config 塞给 runc init
	// ...
}
ierr := parseSync(p.comm.syncSockParent, func(sync *syncT) error {          // ⑦ 进入同步循环
	switch sync.Type {
	case procMountPlease:
		// runc init 请求帮忙做带 idmap 的 bind mount(它自己在 user ns 里没权限)
		// ...
	case procSeccomp:
		// runc init 装好 seccomp,把 seccomp fd 转给外部 listener
		// ...
	case procReady:
		// runc init 报告"沙箱建好了"(此时它正卡在 FIFO 上)
		// ...
	}
})
```

这 7 步把 parent 的活全列出来了。最巧妙的是 **③ 和 ⑥** ——parent 把配置**分两段**塞给 runc init:**先塞 `bootstrapData`(namespace paths、clone flags、uid/gid map——这些是 nsexec 在 Go runtime 起来之前就要用的),后塞完整 `config`(rootfs、mounts、cgroup 设置、seccomp 规则——这些是 Go runtime 起来之后 standard_init.Init 才用的)**。为什么分两段?因为 nsexec 在 Go runtime 起来之前就要读 bootstrapData,那时候 Go 的 JSON 解析还用不了,所以 bootstrapData 用了更简单的 netlink 二进制格式([`bootstrapData` 在 `container_linux.go`#L1063](../runc/libcontainer/container_linux.go#L1063));完整 config 用 JSON 是给 Go 代码读的,可以等 Go runtime 起来。

### 第五段:runc init → nsexec 三 stage → Go runtime → Init → execve

runc init 子进程一被 fork 出来,执行顺序是:

1. **cgo constructor 跑 `nsexec()`**(在 Go main 之前)—— [nsexec.c#L725](../runc/libcontainer/nsenter/nsexec.c#L725)。读 `_LIBCONTAINER_INITPIPE` fd,解析 parent 传来的 bootstrapData,然后 `setjmp/clone_parent` 走 stage-0 → stage-1 → stage-2。stage-2 `return` 出来。
2. **Go runtime 起来**,跑 [`init.go`#L10-L16](../runc/init.go#L10-L16) 的 `init()` 函数,它检测到 `os.Args[1] == "init"`,调 `libcontainer.Init()`。
3. `libcontainer.Init()` 根据 `_LIBCONTAINER_INITTYPE` 环境变量(在 `newInitProcess` 里设的,值是 `initStandard`),实例化 `linuxStandardInit`,调它的 [`Init()`](../runc/libcontainer/standard_init_linux.go#L51)。
4. `Init()` 里依次:keyring → 网络 → 路由 → SELinux → **`prepareRootfs`(挂 overlay、bind mount、最终 `pivotRoot`)** → console → hostname → AppArmor → sysctl → readonly/mask paths → pdeath → `PR_SET_NO_NEW_PRIVS` → 调度策略 → **`syncParentReady`(通知 parent "我准备好了")** → seccomp → `finalizeNamespace`(应用 uid/gid 映射)→ **`fifoFile.Write([]byte("0"))`(卡在 FIFO 上等放行)** → `UnsafeCloseFrom`(关 fd,CVE-2024-21626)→ **`linux.Exec(name, args, env)`(execve,容器进程诞生)**。

把这条链路和第二节那张五步职责链表对上:

| 五步 | 在哪里做 |
|------|---------|
| ① 读 spec | parent 侧 `startContainer` 里的 `setupSpec` |
| ② 建 namespace | child 侧 `nsexec` 的 stage-1 unshare + stage-2 clone |
| ③ 设 cgroup | parent 侧 `initProcess.start` 的 `manager.Apply`(把 stage-0 runc init PID 钉进去)|
| ④ pivot_root | child 侧 `standard_init.Init` 的 `prepareRootfs` → `pivotRoot` |
| ⑤ exec 业务 | child 侧 `standard_init.Init` 末尾的 `linux.Exec`(execve)|

> 这一节信息密度很高,但本质就是:**parent 干 parent 能干的事(读 spec、Apply cgroup、同步),child 干 child 必须干的事(进 ns、换根、exec),两者通过管道和 FIFO 协调**。这种"parent/child 分工 + 同步原语协调"的架构,在操作系统里到处都是(fork/exec、init 系统、shell 管道),runc 是这套经典模式在容器场景的精致实现。

---

## 章末小结

### 用航运比喻回顾本章

回到那片港口。这一章我们拆开了**起重机(runc)的内部结构**,看清楚了它从接到一份作业单到松钩的完整流程:

1. **它是一台标准起重机**——不关心货是什么、船开去哪、谁派的单,只按 OCI runtime-spec 这份标准作业单(`config.json`)干活。这是第 8 章 OCI 标准化的落地。
2. **它的标准作业分两段**——**先 create**(把集装箱吊到甲板指定位置、用锁扣固定、接上电源检测,但不通电运行),**等港口调度确认网络/管线/配载都 OK 了,再 start**(松钩通电,集装箱正式运作)。这个两段式不是起重机矫情,是**港口管理公司(containerd)需要在通电前插手配 CNI/CSI**——给它留个窗口。
3. **它的内部是三个齿轮咬合**——主进程(parent)是起重机的电脑控制,它 fork-exec 出 "runc init" 这个执行单元,"runc init" 内部又用 C 写的机械继电器层(`nsexec`)协调出三个 stage(stage-0 写 uid_map、stage-1 进 namespace、stage-2 才是真容器进程)。**为什么三个齿轮?因为内核的 PID namespace 只对孩子生效、user namespace 的 uid_map 只能父写,这些机制逼出来的。为什么底层用 C?因为 setns/clone 必须在 Go 多线程 runtime 起来之前做完。**
4. **它干的活全是第 1 篇讲过的内核能力**——`clone(CLONE_NEW*)` 建 namespace、写 cgroup 文件限资源、`pivot_root` 换根、`execve` 跑业务。**起重机没有发明任何新的物理定律,它只是把第 1 篇那几块基石工业级地组装起来、标准化地封装好。**

### 本章在全书主线中的位置

回到全书二分法:**打包隔离 vs 调度编排**。

这一章属于**打包隔离**这一侧的收尾——我们终于看清了"底层运行时"的完整内部结构。从第 1 章(容器是什么)到第 6 章(手搓容器)到第 8 章(OCI 标准)再到本章(runc 内部),**"怎么把一个应用打包、隔离、装成能跑的标准件"这条线,到 runc 这里彻底走通了**。

runc 的位置极其特殊:它是**整个容器生态的地基**。docker、containerd、k8s kubelet、podman、cri-o——所有这些上层组件,**最终都要调 runc(或 runc 的同类,如 crun、kata-runtime)来真正跑容器**。runc 之下是内核,之上是整个容器世界。**理解了 runc,你就拿到了从"内核机制"通往"容器生态"的那座桥。**

### 五个"为什么"清单

如果你只能从这一章带走五件事:

1. **runc 干的五件事**:读 runtime-spec(config.json)→ 建 namespace → 设 cgroup → pivot_root 换根 → exec 业务进程。**后四件全是第 1 篇讲过的内核能力,runc 没有发明任何新机制。**
2. **为什么 `runc create` 和 `runc start` 分开**:为了给高层运行时(containerd)留一个"沙箱建好但业务没跑"的窗口,让它在这个窗口里配 CNI/CSI。实现靠一个 FIFO——容器 init 卡在 FIFO 的 Write 上,`runc start` 打开 FIFO 读端才放行。
3. **为什么跑一个容器要分三个进程**:因为 PID namespace 只对孩子生效(必须 fork 才能进)、user namespace 的 uid_map 只能父写(必须有父进程帮忙),这些内核机制逼出了 stage-0/1/2 三进程结构。最终活下来的只有 stage-2,它就是容器里的 PID 1。
4. **为什么 nsexec 是 C 写的**:因为 `setns`/`clone` 必须在 Go 多线程 runtime 起来之前做完(否则 setns 会因多线程失败)。runc 用 cgo 的 `__attribute__((constructor))` 让 `nsexec()` 在 Go main 之前自动执行,在 Go runtime 启动前就把 namespace 全部建好。
5. **为什么 runc 不直接 clone 出容器进程,而要 fork-exec 自己**:因为 runc 主进程是 Go 多线程程序,而 setns/clone 进 namespace 要求单线程。所以 fork-exec 出一个全新的单线程 "runc init" 子进程(就是重新执行 runc 自己带 `init` 参数),在它里面做所有 namespace 相关的事。

### 想继续深入,该往哪钻

- **把 runc 跑起来亲自看三 stage**:在有 root 的 Linux 上,`runc spec` 生成默认 config.json,然后 `runc run test` 时在另一个终端 `ps -ef | grep runc:`——你会看到 `runc:[0:PARENT]`、`runc:[1:CHILD]`、`runc:[2:INIT]` 三个进程(转瞬即逝,要快)。这是验证双进程模型最直接的方法。
- **读 nsexec.c 的完整吐槽**: [`libcontainer/nsenter/nsexec.c`#L800-L845](../runc/libcontainer/nsenter/nsexec.c#L800-L845) 那段注释("what has my life come to?")是理解"为什么三进程"的最好材料,作者把每一条内核约束都列了出来。
- **追 FIFO 机制**:`mkfifo` 在 [`container_linux.go`#L467](../runc/libcontainer/container_linux.go#L467),`Reopen + Write` 在 [`standard_init_linux.go`#L266](../runc/libcontainer/standard_init_linux.go#L266),`OpenFile` 读端在 [`container_linux.go`#L242](../runc/libcontainer/container_linux.go#L242)。这三处串起来就是两段式的完整实现。
- **看 containerd 怎么调 runc**(下一章的主题):特别关注 containerd 在 `runc create` 和 `runc start` 之间插了哪些活(CNI、CSI)——你会反过来理解为什么 runc 必须留这个窗口。

---

> 起重机拆完了。我们已经看清:runc 这个底层运行时,**只管"把单个容器按 spec 跑起来"这一件事**——它不拉镜像、不管生命周期、不收日志、不调度。但真实的容器世界里,你要的是"从一个镜像仓库拉镜像、管几百个容器的生死、收集日志、对接 k8s"——这些活 runc 一个都不干。那是谁干的?**港口管理公司(containerd)——它站在起重机之上,管整个港口的运作。** 翻开 **第 10 章 · containerd:高层运行时管什么**。
