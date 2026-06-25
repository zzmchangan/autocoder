# 第 6 章 · 动手:不用 docker,手搓一个容器

> **前置**:你需要先读过[第 5 章《联合文件系统 overlayfs:分层镜像为什么这么设计》](P1-05-联合文件系统overlayfs-分层镜像为什么这么设计.md)。前面四章我们一块一块拆了集装箱的三块舱壁——**namespace(隔离)**、**cgroup(限资源)**、**rootfs/overlayfs(换根)**。但它们一直是分开讲的,你可能还心存怀疑:"这几样真的拼起来就能跑容器吗?"这一章,我们把这三块拼到一起,**亲手用一个普通进程造出第一个能跑的容器**,把第 1 篇彻底收口。

> **核心问题**:剥掉 docker 所有包装,"容器"的最小实现到底有多简单?
>
> 这一章我们不再讲新机制,而是干一件最朴素也最有力的事——**用几十行 Go,自己写一个能跑的容器**。从 `clone` 建隔离世界,到往 `/sys/fs/cgroup` 写文件设配额,到 `pivot_root` 换根,到 `exec /bin/sh`。跑起来之后,你会亲手验证:容器里 `ps` 只看得到自己、`hostname` 独立、`free` 看到的是被限过的内存。**这一刻,容器所有的"神秘感"会彻底瓦解。**
>
> **读完本章你会明白**:
> - 为什么"容器"在源码层面就是 `clone(CLONE_NEW*)` + 写 cgroup 文件 + `pivot_root` + `exec` 这四个系统调用的组合,**根本没有第二套魔法**。
> - 怎么**亲手写**一个能跑 busybox 的最小容器,代码不到 100 行,每一步对应第 2~5 章哪个机制。
> - 工业级运行时 runc 在我们手搓版的几十行之上,**补了哪些工业级细节**(用户/uid_map、网络、信号收割、AppArmor、seccomp、CVE 防护……),以及**这些细节为什么必须有**。
> - docker 到底替我们省了哪些麻烦(镜像分发、CLI、生命周期、日志),从而自然引出第 2 篇——**真正的"集装箱"是怎么打包分发的**。

> **逃生阀**:如果代码读得头大,你只需要记住三件事——① 容器 = 普通进程 + 几个 `CLONE_NEW*` 标志 + 写 cgroup 文件 + `pivot_root`;② 我们手搓的几十行和 runc 的工业版,差在"边界情况处理",不在"核心机制";③ "手搓能跑"这件事本身,就是第 1 篇整篇基石的验证。

---

## 章首·把三块基石拼起来

第 1 章我们立了第一性原理:**容器就是一个普通进程,套了 namespace 和 cgroup 两件外套**。第 2、3、4、5 章我们逐块拆了外套的不同部位:

- 第 2 章 **namespace**——让进程看到完全不同的世界(进程表、网卡、挂载点、主机名)。
- 第 3 章 **cgroup**——给这个进程设配额(CPU、内存、IO)。
- 第 4 章 **rootfs + pivot_root**——换掉它的根目录,让它看到"自己的文件系统"。
- 第 5 章 **overlayfs**——分层堆叠 rootfs,公共层复用。

我们一直在分开讲这几块,像一个工程师在桌上摊开一堆零件,挨个介绍"这是舱壁、这是配额秤、这是地基板、这是分层货架"。但容器之所以是容器,**是这几块零件装在一起、变成一艘能跑的集装箱**。

> **比喻**:想象一个学徒集装箱工程师站在空荡荡的甲板上。前面四章师傅讲了"铁皮怎么打、配额秤怎么调、地基怎么铺、货架怎么叠"。今天师傅说:"行了,理论够了。给你一卷铁皮、一台秤、一块地基板——**自己装一个能跑的箱子出来**。"

装出来的东西会告诉你一个朴素而震撼的事实:**原来只要把这四块零件装在一起,集装箱就能跑。没有什么"集装箱引擎",没有什么"虚拟化内核模块",就是这四个系统调用的组合。**

这一章,我们就来装这个箱子。

---

## 一、目标:造一个什么样的容器

先把目标定清楚,免得读者预期错位。

我们要造的容器,功能极简:

1. **隔离**:进程进自己的 PID/mount/network/UTS/IPC namespace——里面 `ps` 只看得到自己、`hostname` 是独立的、网卡是空的 `lo`。
2. **限资源**:给它限 100MB 内存、半个 CPU(往 cgroup v2 文件里写值)。
3. **换根**:把根目录换成一个解压好的 busybox 目录,进去看到的是 `/bin /etc /usr`,不是宿主的 `/`。
4. **跑业务**:在里面跑一个 `/bin/sh`,你可以在里面敲 `ls`、`ps`、`free`,验证"我确实被关进了一个小盒子"。

**不要**期待它有这些功能(都是 docker 的功劳,后面章节讲):镜像分发、网络联网(能 ping 宿主/外网)、卷挂载、生命周期管理、日志收集、配置文件……这些都先没有。我们要的就是**最小可验证的隔离沙箱**。

> **不这样(不先定一个最小目标)会怎样**:很多人写"手搓容器"教程,一上来就堆 500 行 Go,把网络、卷、tty、信号都加上,读者读到一半就迷失在细节里,反而错过了"容器核心就这么简单"这个最重要的认知。所以我们**刻意把目标砍到最小**——只要能证明"隔离 + 限资源 + 换根 = 容器"这件事就够了。

接下来,我们一步步把它写出来。

---

## 二、零件清单:四个系统调用

在写代码之前,先把"用到的零件"摆出来。**整个手搓容器,只用四个内核系统调用 + 一个写文件操作**。

| 步骤 | 系统调用 / 操作 | 对应章节 | 干什么 |
|------|---------------|---------|--------|
| ① 建隔离进程 | `clone` / `unshare` + `CLONE_NEW*` 标志 | 第 2 章 | 起子进程并把它塞进新的 namespace |
| ② 设资源配额 | 往 `/sys/fs/cgroup/.../memory.max` 等文件写值 | 第 3 章 | 限内存/CPU |
| ③ 换根 | `pivot_root`(或 `chroot`) | 第 4 章 | 把根目录换成 rootfs |
| ④ 跑业务 | `execve` | (内核进程篇) | 把当前进程的镜像换成 `/bin/sh` |

**就这四件。** 你可以在脑子里先预演一遍:一个父进程,`clone` 出一个带了 `CLONE_NEW*` 标志的子进程;子进程一进来发现自己在一个新世界(进程表是空的、网卡是 `lo`),然后它先往 cgroup 文件里写值把自己限好量、`pivot_root` 把自己换到 busybox 目录、最后 `exec /bin/sh` 接管自己。**一个能跑的容器,就这四步。**

> 这个清单本身就是第 1 篇四章的"复习提纲"。如果你读到这一行,觉得每一步都"嗯,我懂"——那说明第 1 篇的地基你已经踩实了。下面我们只是把它写下来。

---

## 三、运行它之前:rootfs 怎么准备

在写代码前,先把 rootfs 准备好——这是容器跑起来的"地基板"。

> **比喻**:集装箱里得有货架(文件系统),才能放货(应用)。我们用一个**最小货架**:busybox——它一个二进制顶几十个常用命令(`ls`、`ps`、`free`、`sh`、`cat`……都链接到同一个 busybox),体积才 2MB,是手搓容器的标配。

```bash
# 1. 在工作目录下建个文件夹当容器根
mkdir -p ~/mini-container/rootfs && cd ~/mini-container

# 2. 下载静态编译的 busybox(不依赖宿主的 glibc,免得进去后跑不起来)
curl -L -o busybox.tar.xz \
  https://busybox.net/downloads/binaries/1.35.0-x86_64-linux-musl/busybox.tar.xz

# 3. 解压到 rootfs
mkdir -p rootfs/bin
tar -xf busybox.tar.xz -C rootfs/bin

# 4. 装 busybox 的"软链接":sh、ls、ps、free、mount …… 都指向 busybox
#    (busybox 根据自己被叫什么名字,执行不同的命令)
cd rootfs/bin && ln -sf busybox sh && ln -sf busybox ls && \
  ln -sf busybox ps && ln -sf busybox free && ln -sf busybox mount && \
  ln -sf busybox hostname && ln -sf busybox cat && cd ../..

# 5. 给 rootfs 凑齐 /proc 和 /sys 占位(代码里会真挂)
mkdir -p rootfs/proc rootfs/sys
```

完成后,`rootfs/` 应该长这样:

```
rootfs/
├── bin/
│   ├── busybox
│   ├── sh -> busybox
│   ├── ls -> busybox
│   └── ...
├── proc/   (空,运行时 mount procfs)
└── sys/    (空,运行时挂 sysfs)
```

> **不这样会怎样**:如果你不给 rootfs 准备 `/bin/sh`,容器进程换完根、`exec` 的时候就找不到目标程序,直接报 `no such file`;如果不用**静态编译**的 busybox,它会找不到 `libc.so.6`(因为你换根后,宿主的 `/lib` 看不到了),`exec` 同样失败。这两条是新手手搓容器最容易踩的坑。**这个坑也顺手证明了第 4 章换根的彻底性:换完根,宿主的 `/lib`、`/usr` 一切都真的看不到了。**

好,零件齐了。开始写代码。

---

## 四、最小实现:100 行 Go 造一个容器

> **说明:以下为本章编写的最小示意实现(非真实源码摘录)**。代码目标只有一个——**可读、可跑、每一步对应前面某一章**。它不是 runc 的简化版,而是为讲清"容器 = 四个系统调用组合"专门写的教学代码。运行需要 Linux + cgroup v2 + root 权限。

### 4.1 总体结构

我们的程序分**父进程**和**子进程**两段,用一个 Go 程序里 `os.Args[1]` 区分:

- **不带参数 / 调用方式 `./mini-container`**:父进程——负责 `clone` 出带 namespace 的子进程、给子进程建 cgroup、把子进程的 PID 写进 cgroup。
- **带参数 `__child__` / 调用方式 `./mini-container __child__`**:子进程——它已经在新的 namespace 里了,负责往 cgroup 写配额、`pivot_root` 换根、`exec /bin/sh`。

> **为什么子进程的工作要"自己往自己的 cgroup 写配额"而不是父进程代劳?** 因为配额一旦写进 cgroup,**这个 cgroup 里所有的进程都受约束**。我们希望"配额"作用在容器进程(以及它派生的所有子进程)上,所以让子进程**先把自己加进 cgroup 再 exec**——这样它和它未来 fork 出来的所有进程都在同一个 cgroup 里,谁也别想逃。runc 的做法略有不同(父进程提前 `Apply`),但原理一致:让目标进程活在 cgroup 里。

### 4.2 父进程:克隆一个隔离世界

```go
// 文件: main.go —— 本章编写的最小示意实现
package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"syscall"
)

// ===== 步骤 1:namespace 标志位 =====
// 对应第 2 章。把"要哪些 namespace"翻译成 clone 的 CLONE_NEW* 位掩码。
// 这里一口气要 5 个:PID、mount、network、UTS、IPC。
// 注意:不建 user namespace,所以这个容器必须以 root 跑。
const cloneFlags = syscall.CLONE_NEWPID |
	syscall.CLONE_NEWNS |
	syscall.CLONE_NEWNET |
	syscall.CLONE_NEWUTS |
	syscall.CLONE_NEWIPC

func main() {
	if len(os.Args) > 1 && os.Args[1] == "__child__" {
		// 我已经是子进程了,跑子进程逻辑(在隔离世界里)
		child()
		return
	}
	parent()
}

func parent() {
	// ===== 步骤 2:用 exec.Command + SysProcAttr.Cloneflags 起子进程 =====
	// 对应第 2 章 clone(CLONE_NEW*)。
	// Go 的 exec 包在 Linux 上会把 SysProcAttr.Cloneflags 原样传给 clone。
	cmd := exec.Command("/proc/self/exe", "__child__") // 复制自己,以 __child__ 模式跑
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr

	cmd.SysProcAttr = &syscall.SysProcAttr{
		Cloneflags: uintptr(cloneFlags),
		// 告诉子进程"你的根目录在这"(子进程自己 pivot_root)
		Setpgid: true,
	}

	if err := cmd.Start(); err != nil {
		fmt.Printf("clone 失败: %v\n", err)
		os.Exit(1)
	}

	// ===== 步骤 3:用子进程的 PID 建 cgroup v2 目录 =====
	// 对应第 3 章。cgroup v2 是统一层级,/sys/fs/cgroup 是唯一根。
	// 我们在它底下建一个子目录,用容器名命名。
	pid := cmd.Process.Pid
	cgPath := "/sys/fs/cgroup/mini_" + strconv.Itoa(pid)
	if err := os.Mkdir(cgPath, 0755); err != nil {
		fmt.Printf("建 cgroup 失败(确认你以 root 跑、cgroup v2 已挂): %v\n", err)
		os.Exit(1)
	}

	// 把子进程的 PID 写进 cgroup.procs —— 这一步"把子进程钉进 cgroup"。
	// 对应第 3 章:写完之后,这个 cgroup 的配额就开始作用在子进程上。
	if err := os.WriteFile(filepath.Join(cgPath, "cgroup.procs"),
		[]byte(strconv.Itoa(pid)), 0644); err != nil {
		fmt.Printf("加进程进 cgroup 失败: %v\n", err)
		os.Exit(1)
	}

	// 等子进程退出(子进程退出后我们顺手清掉 cgroup)
	_ = cmd.Wait()
	_ = os.RemoveAll(cgPath)
}
```

读父进程这段,把它和第 2、3 章对上:

- **第 35 行** `const cloneFlags = ...`:就是第 2 章那张 namespace 表的位或——P0-01 引的 runc `CloneFlags()` 函数([libcontainer/configs/namespaces_syscall.go#L24-L33](../runc/libcontainer/configs/namespaces_syscall.go#L24-L33))做的就是同样的事,只不过 runc 那张表是从配置读的,我们这里是写死的 5 个。
- **第 52-54 行** `Cloneflags: uintptr(cloneFlags)`:Go 把它传给底层 `clone`,**新子进程一出生就已经在 5 个新 namespace 里**。没有任何"先创建再切"的中间态。
- **第 67-71 行** `os.Mkdir("/sys/fs/cgroup/mini_<pid>")`:这就是 cgroup v2 的玩法——**建目录 = 建 cgroup**。每建一个目录,内核就在 cgroup 层级里挂上一个新节点。
- **第 77-79 行** `os.WriteFile(..., "cgroup.procs", pid)`:**写 PID = 把进程钉进 cgroup**。从这一刻起,这个子进程受这个 cgroup 的配额约束。

> 注意:父进程只做了"建 cgroup 目录"和"加 PID",但**配额(memory.max、cpu.max)还没设**。配额留给子进程自己写——这样代码读起来更清晰:父进程负责"造隔离世界",子进程负责"在自己世界里布置一切"。

### 4.3 子进程:在隔离世界里装货

```go
// 文件: main.go(续)——本章编写的最小示意实现
func child() {
	// ===== 步骤 4:写配额(memory.max / cpu.max)=====
	// 对应第 3 章。
	// 子进程一进来,它的 PID 已经被父进程钉进了 /sys/fs/cgroup/mini_<pid>。
	// 这里我们往那个目录里的 memory.max、cpu.max 写值,完成限资源。
	cgPath := "/sys/fs/cgroup/mini_" + strconv.Itoa(os.Getppid())
	// 注:这里用 Getppid 是教学简化;真实 runc 是父进程把 cgPath 传给子进程的。
	// 限内存 100MB
	must(os.WriteFile(filepath.Join(cgPath, "memory.max"),
		[]byte("104857600"), 0644)) // 100 * 1024 * 1024
	// 限 CPU 50%:cpu.max 格式是 "<quota> <period>",50000/100000 = 50%
	must(os.WriteFile(filepath.Join(cgPath, "cpu.max"),
		[]byte("50000 100000"), 0644))

	// ===== 步骤 5:挂 /proc(在新 mount namespace 里挂自己的 procfs)=====
	// 对应第 2 章 mount namespace + 第 4 章换根前置准备。
	// 不挂 /proc,容器里 ps 就没法用、free 也读不到内存信息。
	must(syscall.Mount("proc", "/proc", "proc", 0, ""))

	// ===== 步骤 6:pivot_root 换根 =====
	// 对应第 4 章。
	// 准备:把 rootfs 重新 bind mount 一下(因为 pivot_root 要求
	//       new_root 必须是个挂载点,不能是普通目录)
	rootfs := os.Getenv("MINI_ROOTFS")
	if rootfs == "" {
		rootfs = "./rootfs"
	}
	must(syscall.Mount(rootfs, rootfs, "bind",
		syscall.MS_BIND|syscall.MS_REC, ""))
	// pivot_root 需要:把 new_root 自己挂成挂载点,再 pivot 到 itself
	// (这里用 pivot_root(".", ".") 的标准技巧)
	must(os.Chdir(rootfs))
	must(syscall.PivotRoot(".", "."))
	// 现在 cwd 还是老 root,umount 掉它
	must(syscall.Unmount(".", syscall.MNT_DETACH))
	must(os.Chdir("/"))

	// ===== 步骤 7:设置主机名(对应 UTS namespace)=====
	// 对应第 2 章。验证容器有自己的"船名"。
	must(syscall.Sethostname([]byte("mini-box")))

	// ===== 步骤 8:exec 业务程序 =====
	// 对应内核进程篇的 execve。
	// 到这里环境完全就绪:在隔离 namespace、配额已限、根已换、主机名已改。
	// 把自己这个进程的镜像换成 /bin/sh —— "容器"就这样跑起来了。
	must(syscall.Exec("/bin/sh", []string{"sh"}, os.Environ()))
}

func must(err error) {
	if err != nil {
		fmt.Printf("子进程错误: %v\n", err)
		os.Exit(1)
	}
}
```

把子进程这段和第 3、4 章对上:

- **第 7-13 行** `os.WriteFile("memory.max", ...)` / `cpu.max`:这就是第 3 章说的"往 cgroup 文件写几个数字"。**写 100MB 内存限额**就是这么一行 `WriteFile`。下文关键源码精读会对照 runc 怎么做这件事——你会发现 runc 在这同一行之上加了一堆预检和兼容处理。
- **第 20 行** `Mount("proc", "/proc", "proc", ...)`:挂自己的 procfs。这是**为什么 `ps`、`free` 在容器里能工作**——它们读 `/proc`,而我们在新 mount namespace 里挂了新的 procfs,**它只看得到当前 PID namespace 里的进程**(所以 `ps` 只看得到自己)。
- **第 28-36 行** `PivotRoot(".", ".")`:第 4 章的主角。注意它用了一个绕路:**先把 rootfs bind mount 成挂载点**(`pivot_root` 要求 new_root 必须是挂载点,普通目录不行),再用 `pivot_root(".", ".")` 技巧(下面 runc 源码精读会看到 runc 也用同一招),最后 `MNT_DETACH` 卸掉老 root。
- **第 42 行** `Sethostname("mini-box")`:UTS namespace 的体现——子进程改主机名,**不会影响宿主**,因为它的 UTS namespace 是新的。
- **第 49 行** `syscall.Exec("/bin/sh", ...)`:execve,**把当前进程的内存镜像换成 /bin/sh**。从这里开始,我们的 Go 进程"死了",`/bin/sh` 接管了它的一切(PID、namespace、cgroup、rootfs 都保留)——**这就是容器进程的诞生**。

> 整段加起来不到 100 行。读到这里,你应该已经能感受到本章开头那个"震撼":**原来造一个能跑的容器,只需要这些。**

### 4.4 跑起来:验证隔离生效

```bash
# 编译
cd ~/mini-container
go mod init mini && go build -o mini-container .

# 跑(必须 root,因为没建 user namespace)
sudo MINI_ROOTFS=$PWD/rootfs ./mini-container
```

如果一切顺利,你会看到一个 shell 提示符(`#` 或 `/ #`)。现在你在"容器"里了。敲这些命令验证:

```bash
# ① 验证 PID namespace:只看得到自己
/ # ps aux
PID   USER     TIME  COMMAND
    1 root      0:00 /bin/sh       ← 我是 1 号进程!宿主上几百个进程全看不见
    5 root      0:00 ps aux

# ② 验证 UTS namespace:主机名是 mini-box,不是宿主名
/ # hostname
mini-box

# ③ 验证 network namespace:网卡是空的,只有一个 lo
/ # ip addr
1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 ...
    inet 127.0.0.1/8 scope host lo

# ④ 验证 cgroup memory.max:free 显示的内存可能远超 100MB,
#    但你只要一超过 100MB,memory cgroup 就会触发 OOM kill。
#    试试 / # head -c 200000000 /dev/zero | tail    → 这条命令会被 OOM 杀掉
```

> 这四条验证,每一条都对应第 1 篇某一章:**PID namespace(第 2 章)、UTS namespace(第 2 章)、network namespace(第 2 章)、memory cgroup(第 3 章)**。如果四条都符合预期,恭喜——**你刚刚亲手造了一个能跑的容器**。
>
> **不这样(不亲手验证)会怎样**:你可以一辈子读"容器是 namespace + cgroup",但只要没亲手跑过一次,这个认知就一直是"听别人说的"。亲手敲 `ps` 看到 PID 是 1、敲 `head -c 200M` 被内核 OOM 杀掉——这两次冲击,顶得上读十篇文章。这就是这一章存在的意义。

---

## 五、回头看:我们到底用了什么

把这个手搓版的步骤列一遍,**每一步对应第 1 篇的某一章**:

| 步骤 | 我们做了什么 | 对应章节 | 用的系统调用 |
|------|------------|---------|-----------|
| 1 | 给子进程要 5 个新 namespace | 第 2 章 | `clone(CLONE_NEWPID\|NEWNS\|NEWNET\|NEWUTS\|NEWIPC)` |
| 2 | 建 cgroup 目录、把子进程加进去 | 第 3 章 | `mkdir /sys/fs/cgroup/...`, `write cgroup.procs` |
| 3 | 写内存/CPU 配额 | 第 3 章 | `write memory.max`, `write cpu.max` |
| 4 | 挂自己的 procfs | 第 2 章 | `mount proc` |
| 5 | 换根 | 第 4 章 | `pivot_root` + `MNT_DETACH` |
| 6 | 设主机名 | 第 2 章 | `sethostname` |
| 7 | 跑业务 | 进程篇 | `execve("/bin/sh")` |

**这张表把第 1 篇四章全部串起来了。** 你会发现:除了 `execve` 是内核进程篇的内容,前面 6 步**全是第 1 篇讲过的东西**。容器没有任何新机制——这就是本章要立的核心认知。

> 换句话说:**如果你能读懂这 100 行,你就读懂了第 1 篇整本书。**

---

## 六、和 docker 比一比:docker 替你省了什么

手搓版的容器能跑,但它简陋得可笑:没有镜像分发(你手动 curl busybox)、没有网络(容器里只有 `lo`,ping 不通宿主)、没有 CLI(参数硬编码)、没有生命周期管理(容器退出 cgroup 就被删了,不能 `stop`/`start`)、没有日志(stdout 直接接到终端)。

> **不这样(不对比)会怎样**:读者会误以为"手搓版 = docker 的内核",从而看轻 docker。真相是反过来的——**手搓版的 100 行是地基,docker 是在这地基上盖的几十层楼**。看清楚 docker 多做了什么,你才明白它为什么值得用。

docker 相比我们这 100 行,至少替你省了这些麻烦:

| 你手搓要自己干的 | docker 帮你做了 | 后续哪章讲 |
|------|------|------|
| 手动 curl busybox、解压、建 rootfs | `docker pull` 自动从镜像仓库拉、自动解压分层 | 第 7 章(镜像本质)、第 11 章(docker) |
| 容器只有 `lo`,联网要自己建 veth pair + bridge | 默认配 docker0 bridge + 自动 NAT | 第 12 章(容器网络) |
| 参数全靠改源码 | `docker run -it --rm --memory=100m ...` 一条命令 | 第 11 章 |
| 容器死了就没了,数据丢了 | volume / 数据卷持久化 | 第 21 章 |
| stdout 直接喷到终端 | `docker logs` 统一收集 | 第 11 章 |
| 没有 OOM、崩溃的自愈 | 配合 k8s 的 Deployment 自动重启 | 第 14、17 章 |
| 配额、安全限制全自己写 | runc 默认带 seccomp、AppArmor、capabilities 裁权 | 第 22 章 |

> **比喻**:我们手搓的,是一个**铁皮笼子**——能关货、能限重、能从甲板抬走,但**没有装卸系统、没有堆场、没有运单、没有保险**。docker 是把这些全包圆了——你只要说"把这箱货从仓库拉过来、限重 100 公斤、装到甲板上、跑了别让它沉",docker 一条命令全办。**集装箱(容器进程)还是那个集装箱,docker 的价值在"集装箱之外的一切"。**

这一对比,我们也就知道了接下来几篇要讲什么:**怎么把这个能跑的铁皮笼子,变成可搬运、可分发、可规模化的标准集装箱。** 那就是**第 2 篇:镜像与运行时标准**。

但在这之前,我们先看一件让你震撼的事:**我们这 100 行的"工业版"——runc——到底比我们多写了多少行?**

---

## 关键源码精读:我们手搓的 100 行,runc 写了多少

> 这是本章的高潮。我们挑 runc 里**和我们手搓版一一对应**的几段真实源码,逐段对照——**核心机制一样,但每一步 runc 都补了工业级细节**。看完你会明白:"容器运行时"这个工种,真正的难点不在"造出能跑的容器",而在"造出在所有奇怪环境下都安全、都正确、都不漏的容器"。

### 对照 1:CloneFlags —— 我们写死 5 个,runc 从配置读

我们手搓版第 35 行的 `const cloneFlags = ...`,在 runc 里长这样([libcontainer/configs/namespaces_syscall.go#L11-L33](../runc/libcontainer/configs/namespaces_syscall.go#L11-L33)):

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
			continue
		}
		flag |= namespaceInfo[v.Type]
	}
	return uintptr(flag)
}
```

**和我们的对比**:

- 我们写死 5 个 namespace。runc 把它做成一张表 + 一个 for 循环——**配置说要哪些,就拼哪些**。多支持了 user/cgroup/time 三种 namespace。
- 注意 `if v.Path != ""` 这一行(P0-01 也讲过):**带 Path 的 namespace 不参与 clone flag,而是稍后用 `setns` 加入已有的**。这一行是 k8s Pod(几个容器共享 network namespace)的根基——第 16 章会再见到它。**我们手搓版没有这个能力**(我们只会新建,不会加入)。

runc 在哪用 `CloneFlags()`?在 [libcontainer/container_linux.go 的 `newInitProcess`](../runc/libcontainer/container_linux.go)(约 L645-L671):

```go
func (c *Container) newInitProcess(p *Process, cmd *exec.Cmd, comm *processComm) (*initProcess, error) {
	cmd.Env = append(cmd.Env, "_LIBCONTAINER_INITTYPE="+string(initStandard))
	nsMaps := make(map[configs.NamespaceType]string)
	for _, ns := range c.config.Namespaces {
		if ns.Path != "" {
			nsMaps[ns.Type] = ns.Path
		}
	}
	data, err := c.bootstrapData(c.config.Namespaces.CloneFlags(), nsMaps)
	if err != nil {
		return nil, err
	}
	// ... 把 bootstrapData 挂到 initProcess 上,后续通过管道喂给 runc init 子进程
}
```

`c.config.Namespaces.CloneFlags()` 这一行就是 runc 调我们前面那段函数的地方。注意它把结果塞进 `bootstrapData`——**runc 不是简单 `cmd.SysProcAttr.Cloneflags = ...`,而是先把位图 + namespace path 表打包成一段数据,通过管道交给 runc init 子进程(C 写的)消费**。为什么要这么绕?因为 runc 在 user namespace + uid_map 这件事上有大量边界处理,Go 的 `exec` 包应付不了——这部分我们手搓版**完全没碰**(我们没建 user namespace)。

### 对照 2:加进程进 cgroup —— 我们 mkdir+write,runc 多了安全预检

我们手搓版的第 67-79 行(建 cgroup 目录 + 写 PID),runc 在 [libcontainer/process_linux.go 的 `initProcess.start()`](../runc/libcontainer/process_linux.go)(约 L777-L820):

```go
func (p *initProcess) start() (retErr error) {
	defer p.comm.closeParent()
	err := p.cmd.Start()
	p.process.ops = p
	p.comm.closeChild()
	if err != nil {
		p.process.ops = nil
		return fmt.Errorf("unable to start init: %w", err)
	}

	defer func() {
		if retErr != nil {
			// 失败时:统计是否 OOM 杀的、terminate 子进程、Destroy cgroup
			oom, err := p.manager.OOMKillCount()
			// ...
			if err := ignoreTerminateErrors(p.terminate()); err != nil {
				logrus.WithError(err).Warn("unable to terminate initProcess")
			}
			_ = p.manager.Destroy()
		}
	}()

	// Do this before syncing with child so that no children can escape the
	// cgroup. We don't need to worry about not doing this and not being root
	// because we'd be using the rootless cgroup manager in that case.
	if err := p.manager.Apply(p.pid()); err != nil {
		if errors.Is(err, cgroups.ErrRootless) {
			// ...
		} else {
			return fmt.Errorf("unable to apply cgroup configuration: %w", err)
		}
	}
	// Reset the CPU affinity after cgroups are configured ...
	tryResetCPUAffinity(p.pid())
	// ...
}
```

**和我们的对比**:

- 我们只写成功路径(建目录、加 PID)。runc 的 `manager.Apply(pid)` 之下做了**事务式保护**——失败时(OOM 归因 + terminate + Destroy cgroup),它要清干净自己留下的痕迹。我们手搓版失败时 cgroup 目录就**泄漏**了。
- 注释里那句 "Do this before syncing with child so that no children can escape the cgroup" —— **"先 Apply 再 sync,免得子进程逃出 cgroup"**。这是个安全要点:子进程在 Apply 之前是不受限的,如果它在那段时间 fork 出孙子进程,孙子可能就不在这个 cgroup 里了。runc 在这里加了同步顺序保证。**我们手搓版完全没考虑这个**。
- `tryResetCPUAffinity(p.pid())`:设完 cgroup 还要重设 CPU affinity,确保和 cpuset 一致。我们手搓版没碰。

### 对照 3:写配额文件 —— 我们一行 echo,runc 做预检+兼容

我们手搓版第 11-13 行的 `os.WriteFile("memory.max", ...)`。runc 的最底层"写 cgroup 文件"在 [vendor/github.com/opencontainers/cgroups/file.go](../runc/vendor/github.com/opencontainers/cgroups/file.go)(L23-L57):

```go
// WriteFile writes data to a cgroup file in dir.
// It is supposed to be used for cgroup files only.
func WriteFile(dir, file string, data string) error {
	fd, err := OpenFile(dir, file, unix.O_WRONLY)
	if err != nil {
		return err
	}
	defer fd.Close()
	if _, err := fd.WriteString(data); err != nil {
		// Having data in the error message helps in debugging.
		return fmt.Errorf("failed to write %q: %w", data, err)
	}
	return nil
}
```

读到这里你会想"这不就是和我们一样,`OpenFile` + `WriteString`?"——表面一样,但 `OpenFile` 内部用的是 `openat2` 系统调用,会**校验路径必须落在 cgroupfs 上**,防止路径被符号链接劫持到别的文件系统(逃逸风险)。我们用 `os.WriteFile`,默认是裸 `open`,**没有这个校验**。

更复杂的逻辑在 [fs2/memory.go 的 `setMemory`](../runc/vendor/github.com/opencontainers/cgroups/fs2/memory.go)(L35-L67):

```go
func setMemory(dirPath string, r *cgroups.Resources) error {
	if !isMemorySet(r) {
		return nil
	}

	if err := CheckMemoryUsage(dirPath, r); err != nil {
		return err
	}

	swap, err := cgroups.ConvertMemorySwapToCgroupV2Value(r.MemorySwap, r.Memory)
	if err != nil {
		return err
	}
	swapStr := numToStr(swap)
	if swapStr == "" && swap == 0 && r.MemorySwap > 0 {
		// memory and memorySwap set to the same value -- disable swap
		swapStr = "0"
	}
	// never write empty string to `memory.swap.max`, it means set to 0.
	if swapStr != "" {
		if err := cgroups.WriteFile(dirPath, "memory.swap.max", swapStr); err != nil {
			// If swap is not enabled, silently ignore setting to max or disabling it.
			if !(errors.Is(err, os.ErrNotExist) && (swapStr == "max" || swapStr == "0")) {
				return err
			}
		}
	}

	if val := numToStr(r.Memory); val != "" {
		if err := cgroups.WriteFile(dirPath, "memory.max", val); err != nil {
			return err
		}
	}
	// ...
}
```

**和我们的对比**:

- `CheckMemoryUsage(dirPath, r)`:写 `memory.max` 之前先**预检**——比如你要设的限额比当前已用内存还小,它会直接报错,避免"设完瞬间 OOM 全杀"。我们手搓版没这个,你想写多少写多少,后果自负。
- `ConvertMemorySwapToCgroupV2Value`:**v1 → v2 兼容换算**。cgroup v1 里 memory 和 swap 是分开两个文件,v2 里 swap.max 是"额外可用的 swap"(不含 memory),要换算。我们手搓版只在 v2 上跑,没这个兼容。
- `swapStr == "max" || swapStr == "0"` 时 swap 不启用就**静默忽略**——内核某些配置不支持 swap,如果硬报错会让用户莫名其妙。这是"工业级容错":**能设就设,不能设的合理情况就当没事**。

`cpu.max` 同理,在 [fs2/cpu.go 的 `setCPU`](../runc/vendor/github.com/opencontainers/cgroups/fs2/cpu.go)(L19-L74)里,runc 把 `quota period` 拼成 `"50000 100000"` 这种格式(我们手搓版第 13 行直接写死的那个串),还处理了 `cpu.weight`、`cpu.idle`、`cpu.max.burst`、EINVAL 重试等一堆边角。

> **一句话**:我们写一行 `WriteFile`,runc 在这一行外面包了**预检、换算、兼容、容错、安全校验**。这是"100 行能跑"和"工业级运行时"的差距所在——**核心机制一模一样,差距全在边角处理**。

### 对照 4:pivot_root —— 我们 bind + PivotRoot,runc 用同一招但更稳

我们手搓版的第 28-39 行(换根)。runc 的对应函数在 [libcontainer/rootfs_linux.go 的 `pivotRoot`](../runc/libcontainer/rootfs_linux.go)(约 L1144-L1195):

```go
// pivotRoot will call pivot_root such that rootfs becomes the new root
// filesystem, and everything else is cleaned up.
func pivotRoot(root *os.File) error {
	// ... 注释解释 pivot_root(".", ".") 的原理 ...
	oldroot, err := linux.Open("/", unix.O_DIRECTORY|unix.O_RDONLY|unix.O_PATH, 0)
	if err != nil {
		return err
	}
	defer unix.Close(oldroot)

	// Change to the new root so that the pivot_root actually acts on it.
	if err := unix.Fchdir(int(root.Fd())); err != nil {
		return &os.PathError{Op: "chdir", Path: root.Name(), Err: err}
	}

	if err := unix.PivotRoot(".", "."); err != nil {
		return &os.PathError{Op: "pivot_root", Path: ".", Err: err}
	}

	// Currently our "." is oldroot ...
	if err := unix.Fchdir(oldroot); err != nil {
		return &os.PathError{Op: "fchdir", Path: "fd " + strconv.Itoa(oldroot), Err: err}
	}

	// Make oldroot rslave to make sure our unmounts don't propagate to the
	// host (and thus bork the machine).
	if err := mount("", ".", "", unix.MS_SLAVE|unix.MS_REC, ""); err != nil {
		return err
	}
	// Perform the unmount. MNT_DETACH allows us to unmount /proc/self/cwd.
	if err := unmount(".", unix.MNT_DETACH); err != nil {
		return err
	}

	// Switch back to our shiny new root.
	if err := unix.Chdir("/"); err != nil {
		return &os.PathError{Op: "chdir", Path: "/", Err: err}
	}
	return nil
}
```

**和我们的对比**:

- runc 也用 `pivot_root(".", ".")` 这个技巧——和我们手搓版**思路一致**。但它用 `O_PATH` 打开老 root(更省 fd,也更安全),pivot 后 `fchdir(oldroot)` 回到老 root,**再 mount 一个 `MS_SLAVE|MS_REC` 防止 umount 传播到宿主**(注释里那句 "don't bork the machine" 是血泪教训——历史上 runc 的 umount 真的因为没设 rslave 而把宿主的文件系统一起 umount 掉过),最后 `MNT_DETACH` 卸老 root、`chdir("/")` 回新 root。
- **我们手搓版缺了 `MS_SLAVE|MS_REC` 这一步**——在生产环境里,这意味着我们的容器 umount 老 root 时可能把宿主的挂载一起 umount 掉,**让宿主整机崩溃**。这是手搓版的硬伤,但教学场景下无伤大雅(我们没多少 mount 要传播)。

更上面,在 `prepareRootfs` 里,runc 还做了"三种切根方式的选择"——[rootfs_linux.go L234-L256](../runc/libcontainer/rootfs_linux.go):

```go
	if config.NoPivotRoot {
		err = msMoveRoot(config.Rootfs)
	} else if config.Namespaces.Contains(configs.NEWNS) {
		err = pivotRoot(rootFd)
	} else {
		err = chroot()
	}
```

默认走 `pivotRoot`;`--no-pivot`(ramdisk 等不能 pivot 的场景)走 `msMoveRoot`(`MS_MOVE` + `chroot`);没有 mount namespace 时退化成裸 `chroot`。**runc 永远在为"容器可能跑在什么样的环境里"做适配**。

### 对照 5:容器进程进来之后——我们啥都不做,runc 做了一长串

这里差距最大。我们手搓版子进程一进来,挂 proc → pivot → sethostname → exec,**干净利落**。但 runc 的容器 init 进程(就是容器里的 PID 1)进来之后,做的事情列出来会让你咂舌——看 [libcontainer/standard_init_linux.go 的 `Init()`](../runc/libcontainer/standard_init_linux.go)(L51-L107):

```go
func (l *linuxStandardInit) Init() error {
	if !l.config.Config.NoNewKeyring {
		if l.config.ProcessLabel != "" {
			if err := selinux.SetKeyLabel(l.config.ProcessLabel); err != nil {
				return err
			}
			defer selinux.SetKeyLabel("")
		}
		ringname, keepperms, newperms := l.getSessionRingParams()
		// ... 建会话密钥环,不继承父进程的 ...
	}

	if err := setupNetwork(l.config); err != nil {
		return err
	}
	if err := setupRoute(l.config.Config); err != nil {
		return err
	}

	selinux.GetEnabled()

	err := prepareRootfs(l.pipe, l.config)
	if err != nil {
		return err
	}

	if l.config.CreateConsole {
		if err := setupConsole(l.consoleSocket, l.config, true); err != nil {
			return err
		}
		if err := system.Setctty(); err != nil {
			return &os.SyscallError{Syscall: "ioctl(setctty)", Err: err}
		}
	}
	// ... Init 函数还有后半段 ...
}
```

接着后半段(L122-L184),容器 init 进程还依次做了:

- `Sethostname` —— 我们也做了。
- `apparmor.ApplyProfile` —— **应用 AppArmor 强制访问控制**。我们没碰。
- `WriteSysctls` —— **应用 sysctl 配置**(比如 `net.ipv4.ip_forward`)。我们没碰。
- 遍历 `ReadonlyPaths` 调 `readonlyPath` —— **把指定路径(如 `/proc/sys`)改成只读**。我们没碰。
- `maskPaths` —— **把指定路径(如 `/proc/kcore`)用 `/dev/null` 遮掉**,防止容器读宿主内核信息。我们没碰。
- `GetParentDeathSignal` + 后面 `pdeath.Restore()` —— **父死信号**:runc 进程如果被杀,容器 init 要收到信号自杀,免得变孤儿。我们没碰。
- `PR_SET_NO_NEW_PRIVS` —— **禁止子进程提权**(防止 setuid 程序被利用)。我们没碰。
- `setupScheduler` / `setupIOPriority` / `setupMemoryPolicy` —— **调度策略 / IO 优先级 / NUMA 内存策略**。我们一个都没碰。
- `syncParentReady(l.pipe)` —— **和父进程握手**,告诉它"我准备好了"。这个握手实现"先建好沙箱,再启动业务"的两段式(`runc create` + `runc start` 分开)。我们没碰(我们是 create 即 start)。
- `InitSeccomp` —— **装 seccomp 过滤器,只允许白名单系统调用**。这是容器安全的关键防线(第 22 章细讲)。我们没碰。
- `finalizeNamespace` —— **应用 uid/gid 映射、切用户**。我们没建 user namespace,所以这步不存在。

最后,在真正 `exec` 之前,Init 函数的结尾(L252-L305)还有:

```go
	// Close the pipe to signal that we have completed our init.
	_ = l.pipe.Close()
	if err := l.logPipe.Close(); err != nil {
		return fmt.Errorf("close log pipe: %w", err)
	}

	// Wait for the FIFO to be opened on the other side before exec-ing ...
	fifoFile, err := pathrs.Reopen(l.fifoFile, unix.O_WRONLY|unix.O_CLOEXEC)
	if err != nil {
		return fmt.Errorf("reopen exec fifo: %w", err)
	}
	defer fifoFile.Close()
	if _, err := fifoFile.Write([]byte("0")); err != nil {
		return &os.PathError{Op: "write exec fifo", Path: fifoFile.Name(), Err: err}
	}
	// ...
	// Close all file descriptors we are not passing to the container.
	// See CVE-2024-21626 for more information as to why this protection is necessary.
	if err := utils.UnsafeCloseFrom(l.config.PassedFilesCount + 3); err != nil {
		return err
	}
	return linux.Exec(name, l.config.Args, l.config.Env)
}
```

最后这两步特别值得说:

- **等 FIFO 才 exec**:这一步实现 `runc create` 和 `runc start` 的两段式——容器 init 把所有沙箱都建好后,**不立刻 exec**,而是阻塞在一个 FIFO 上,等外部 `runc start` 来打开这个 FIFO 才放行。这样用户可以"先建好容器、配好环境、再启动业务"。**我们手搓版是 create 即 start**,做不到两段式。
- **`UnsafeCloseFrom` + CVE-2024-21626 注释**:这是 2024 年最严重的 runc 漏洞——容器进程通过 `exec /proc/self/fd/<n>` 之类的技巧,能让 runc 把宿主文件泄漏到容器里。修复就是**在 exec 之前关掉所有不该传给容器的 fd**。这种漏洞的存在本身就告诉我们:**"容器隔离"不是免费的午餐,边角细节漏一处,就是逃逸**。我们手搓版完全没有这层防护。

### 对照 6:PID 1 的命运——信号转发 + 子进程收割

最后一个对照,也是我们手搓版完全没碰的一块:**容器里 PID 1 的特殊性**。

内核有个规则:**PID 1 进程如果没有注册信号处理器,大部分信号(包括 SIGTERM)对它无效**。这是为了防止 init 进程被随便一个信号杀掉。但这带来一个麻烦——**容器里的 PID 1(我们的 `/bin/sh`)如果不处理 SIGCHLD,它派生的子进程会变僵尸**;用户 `docker stop` 发的 SIGTERM 也不会被转发给业务进程。

runc 怎么解决?在 [signals.go 的 `forward`](../runc/signals.go)(L53-L127):

```go
func (h *signalHandler) forward(process *libcontainer.Process, tty *tty) (int, error) {
	pid1, err := process.Pid()
	if err != nil {
		return -1, err
	}
	_ = tty.resize()
	// Handle and forward signals.
	for s := range h.signals {
		switch s {
		case unix.SIGWINCH:
			_ = tty.resize()
		case unix.SIGCHLD:
			exits, err := h.reap()
			if err != nil {
				logrus.Error(err)
			}
			for _, e := range exits {
				if e.pid == pid1 {
					_, _ = process.Wait()
					return e.status, nil
				}
			}
		case unix.SIGURG:
			// SIGURG is used by go runtime for async preemptive scheduling ...
		default:
			us := s.(unix.Signal)
			if err := process.Signal(s); err != nil {
				logrus.Error(err)
			}
		}
	}
	return -1, nil
}
```

`reap()`(同文件下方)调 `Wait4(-1, WNOHANG)` **循环收所有子进程的尸**——这是 subreaper(子进程收割者)机制。runc 在启动时把自己设成 subreaper([utils_linux.go 的 runner.run](../runc/utils_linux.go),约 L264-L273):

```go
	if r.enableSubreaper {
		// set us as the subreaper before registering the signal handler for the container
		if err := system.SetSubreaper(1); err != nil {
			logrus.Warn(err)
		}
	}
```

**和我们的对比**:

- 我们手搓版的 `/bin/sh` 是 PID 1,如果你在容器里跑 `sleep 100` 然后 Ctrl-C,会发生奇怪的事(信号处理不一致);如果你 `sh` fork 出子进程然后退出,子进程会变僵尸。
- runc 走的是另一条路——**runc 自己(在容器外面)做 subreaper + 信号转发**,把外部信号(SIGTERM/SIGINT)转发给容器 init,把僵尸子进程收割掉。`SIGURG` 那个 case 特别有意思:Go runtime 用 SIGURG 做协程抢占,所以**必须丢弃,不能转发**,否则会扰乱 Go runtime。这种细节,**只有写过生产级运行时的人才会想到**。

> 这一段对照下来,你应该明白本章开头的判断了:**容器核心机制,100 行能写出来;但要让它在生产环境里安全、稳定、可控,得加几千行边角处理。** runc 不是"更复杂的我们",而是"我们 + 一层又一层的工业级防护"。
>
> 这也是为什么后面第 2 篇要专门讲 runc 的标准(OCI runtime-spec)、第 22 章要专门讲安全——**这些边角处理,正是工业级容器运行时存在的全部意义**。

---

## 章末小结

### 用航运比喻回顾本章

回到那片港口。前四章师傅讲了铁皮、配额秤、地基板、货架怎么造,今天师傅把你扔到甲板上,说:**"装一个能跑的箱子出来。"**

你装出来的,是一个**最朴素的集装箱**——铁皮一围(namespace)、配额秤一挂(cgroup)、地基一铺(pivot_root)、里面摆上货架(busybox 当 rootfs)、通上电(`/bin/sh` 跑起来)。**100 行代码,它真的能跑。**

然后你看着旁边 runc 工程队造的同款集装箱——人家在铁皮上焊了 AppArmor 防破坏板、在配额秤上加了预检防止超载断裂、在地基上加了 mount propagation 防止震动传给甲板、在门口装了 seccomp 门禁只放行熟人、还配了一个守门员(subreaper)专门收尸和转发信号。**核心零件一模一样,但每块零件都加了工业级强化。**

最后你回头看了一眼远处的港口(docker)——人家不光造箱子,还管**装卸(镜像拉取)、堆场(存储)、运单(生命周期)、保险(卷)**——你说"一箱货从仓库到甲板",docker 一条命令全办。**集装箱还是那个集装箱,docker 的价值在集装箱之外。**

### 本章在第 1 篇中的收口位置

这一章是**第 1 篇(容器的三块基石)的综合收口**。第 1 篇五章一起讲了一件事:

> **容器 = 普通进程 + namespace(隔离) + cgroup(限资源) + rootfs/pivot_root(换根) + overlayfs(分层 rootfs)。这五件事都是内核已有的能力,容器把它们组合起来。**

我们这一章用 100 行代码,亲手验证了这个组合——**它确实能跑出容器**。从此,你对"容器是什么"的认知,不再是"听别人说的",而是"我亲手造过"。

回到全书二分法(**打包隔离 vs 调度编排**):第 1 篇整篇都属于**"打包隔离"**这一侧,我们讲的是"怎么把一个应用隔离、限资源、换根、装成能跑的箱子"。接下来要进入的,是更上层的"打包与产品化"——**怎么把这种"手搓的箱子"变成可搬运、可分发、可规模化的标准集装箱**。

### 五个"为什么"清单

如果你只能从这一章带走五件事:

1. **为什么容器没有神秘力量**:因为它就是 `clone(CLONE_NEW*)` + 写 cgroup 文件 + `pivot_root` + `exec` 这四个系统调用的组合。这四个东西都是第 1 篇前四章讲过的,**容器没有任何新机制**。
2. **为什么本章要动手写**:因为"亲手验证一次"顶得上读十篇文章。当你敲 `ps` 看到 PID 是 1、敲 `head -c 200M` 被内核 OOM 杀掉,容器的所有抽象才会从"听别人说"变成"我知道"。
3. **为什么我们手搓的 100 行 ≠ docker**:因为 docker 在这 100 行之外,还做了镜像分发、网络、CLI、生命周期、日志——这些是后面几篇的主角。**集装箱还是那个集装箱,docker 的价值在集装箱之外**。
4. **为什么 runc 是工业版**:因为同一个核心机制,在边角上加了**预检、兼容、容错、安全、信号收割、CVE 防护**。"能跑"和"安全稳定地跑",差了几千行边角处理。
5. **为什么 runc 还要做 AppArmor/seccomp/pdeath/fd 关闭**:因为容器共享内核,**边角漏一处就是逃逸**(CVE-2024-21626 就是 fd 泄漏导致的逃逸)。这是第 22、23 章的主角。

### 想继续深入,该往哪钻

- **把我们的手搓版跑起来**:照着第三、四节敲一遍,自己改一改——加一个 `CLONE_NEWUSER` 让它不靠 root 也能跑(挑战大,要写 uid_map)、给它配一个 veth pair 接到宿主 bridge 让它能联网(第 12 章会讲)。
- **读 runc 的 `Init()` 全函数**:[libcontainer/standard_init_linux.go](../runc/libcontainer/standard_init_linux.go)。把每一行对应到它"补的工业细节"——你会看到本章对照 5 里列的那一长串,逐一在代码里能找到。
- **追 CVE-2024-21626**:这个 runc 漏洞是理解"为什么 fd 关闭那么重要"的最好材料,也能让你体会"共享内核的代价"。
- **看 runc 命令行入口怎么把这一切串起来**:[cmd/run.go](../runc/cmd/run.go) + [utils_linux.go](../runc/utils_linux.go) 的 `startContainer` / `runner.run`。你会看到 `runc run` 这一条命令底下,调了多少我们本章用过的零件。

---

> 第 1 篇的基石,至此收口。你已经亲手造过容器——它就是几个系统调用的组合,毫无神秘。但有一个问题一直在角落里等着:**我们手搓的容器,用的是本地一个解压好的 busybox 目录当根。可真正的镜像(image),是怎么打包、怎么带版本、怎么从仓库拉到本地的?** 这就是第 2 篇的开篇要回答的事。翻开 **第 7 章 · 镜像的本质:layer + manifest + config**。
