# 第 4 章 · rootfs 与 pivot_root:换个根

> **前置**:你需要先读过 [第 3 章 · cgroup:给进程上配额](P1-03-cgroup-给进程上配额.md)(理由一句话)——前两章用 namespace 给集装箱围上了**铁皮舱壁**(进程表/网卡/主机名各看各的),又用 cgroup 给它配了**载重/体积配额**(限 CPU、限内存)。但有个洞还敞着:**舱壁围住的,到底是哪一片"甲板"?** 这一章就来回答:怎么让一个集装箱里的货,**站到自己那块独立的小甲板**(独立文件系统)上,而不是踩在整艘货轮的公用大甲板(宿主根文件系统)上。

> **核心问题**:容器里的进程,凭什么看到的是"自己的文件系统"而不是宿主的?
>
> 这一章拆解最后一块基石——**文件系统的根**。前几章的隔离都是"进程看不见别的进程",可要是进程一抬眼,`/` 还是宿主那块根,它照样能 `cat /etc/shadow`、能翻你的家目录。所以必须把根换掉。但"换根"这事,内核给了不止一把工具:有古老的 `chroot`,也有更年轻的 `pivot_root`。这章要回答:**它们各自怎么换根、为什么容器偏偏要选 `pivot_root`、`chroot` 到底漏在哪**。
>
> **读完本章你会明白**:
> - 为什么**光有 mount namespace 还不够**——它只换了"挂载点视图",没换 `/` 这个根,进程照样能 `cd /` 看见宿主一切。
> - `chroot` 漏在哪儿:它只改了"路径从哪里开始解析",**特权进程手里攥着的旧根文件描述符,是它逃出伪根的后门**——这是几十年来各种"chroot 越狱"的祖传手法。
> - `pivot_root` 凭什么更安全:它**整体替换根挂载点**,把旧根挪到别处并卸载掉,**内核层面**保证进程再也够不到旧根。
> - runc 真正调用的是哪个、它在源码里怎么写这个换根动作、什么情况下会退回 `chroot`。
> - 为什么"换根"是容器"看起来像个独立小系统"的**最后一块拼图**——拼上它,容器里的进程才真正"住进了一个自己的世界"。

> **如果一读觉得太难**:先只记住三件事——① mount namespace 只换"看见哪些挂载点",`/` 根没动,所以不够;② `chroot` 是"换路径起点",留有逃逸后门;③ `pivot_root` 是"换根挂载点+卸掉旧根",是容器默认用的更安全的换根方式。这三句话撑起整章。

---

## 章首·先把问题钉死

前两章做完,我们的集装箱已经有了**看不见别人的舱壁**(namespace)和**限量配额**(cgroup)。读者到这里,可能已经形成了一个错觉:"隔离做完了呀,进程看不见别人的进程表、用不了别人的网卡、吃不了别人的内存——这不就够了?"

不够。还差一个最要命的洞。

> **比喻**:你给集装箱装上了不透光的铁皮舱壁(进程表隔离)、装上了限重限量器(cgroup)。可舱里的货一站稳,低头一看——**脚下踩的,还是整艘货轮那块公用的、光秃秃的大甲板**(宿主的根文件系统)。它往四周一看,虽然看不见别的集装箱里的货(那是 namespace 的功劳),但它**能低头看见甲板上的一切**:别的货物堆放区、轮机舱的钥匙、船长的航海日志。

落到技术上:容器里的进程 `ls /`,列出来的还是宿主那个 `/`——`/etc`、`/root`、`/home`、`/var`,全是宿主的。它能 `cat /etc/shadow` 读密码文件、能 `cd /root` 翻管理员家目录、能 `rm -rf /lib` 把宿主系统搞瘫。**前面的 namespace 把"进程的世界"隔开了,可"世界本身(文件系统)"还没换。** 这个洞不补,容器就是个筛子。

所以这一章,要干的事就一件:**把根换掉**。让容器里的进程踩在自己的、独立的一小块文件系统上——这块"小甲板"就是 **rootfs**(root file system,根文件系统)。

但内核给了两把换根的工具,故事从它们的差别开始。

---

## 一、为什么光有 mount namespace 不够

讲 `chroot` 和 `pivot_root` 之前,得先把一个**很容易踩的坑**说清楚:很多人以为"容器隔离文件系统,靠的是 **mount namespace**"。

**对,但只对了一半。**

mount namespace(第 2 章那 8 种 namespace 之一,`CLONE_NEWNS`)干的事是:**给这个进程(以及它的子孙)一份独立的"挂载点视图"**。也就是说,进程 A 在 mount namespace 里 `mount` 了一个 U 盘,进程 B(不在那个 namespace)看不见。每个 mount namespace 看到的"哪些东西挂在哪里"可以不一样。

> **比喻**:mount namespace 像是给集装箱开了一张**私有的"甲板平面图"**——只有这个箱知道自己在哪几个位置摆了什么货架。别的箱看不到这张图。

### 不这样会怎样

但 mount namespace 有一个要命的**默认行为**:**它不换根,只是复制了一份当前的挂载点视图,然后让你在里面折腾。** 进程一进新的 mount namespace,抬头一看,`/` 还是原来那个 `/`,宿主的整个文件系统原封不动地摆在那儿。

你在新的 mount namespace 里可以 `mount` 新东西、`umount` 旧东西——但你**没有动到那个 `/`**。进程照样能:

```sh
# 进了 mount namespace 的进程,执行:
ls /            # 看到的还是宿主整个根目录树
cat /etc/passwd # 读到的是宿主的密码文件
cd /home        # 翻进宿主的用户家目录
```

所以 mount namespace 解决的是"**哪些挂载点可见**",而不是"**根是哪一个**"。它造了一个私有的挂载表,但表里**根那一项(`/`)默认指向的还是宿主的根**。要让容器真正踩在自己的文件系统上,你必须**主动把 `/` 这一项换掉**——这就是换根。

换根,内核提供两条路:`chroot`(老)和 `pivot_root`(新)。下面挨个拆,重点拆它们**为什么一个安全、一个不安全**。

---

## 二、chroot:古老而漏风的"换路径起点"

`chroot` 是 Unix 老爷爷辈的系统调用(1979 年进 Unix V7),名字直译就是"change root"——换根。它的做法听起来很直接:**给定一个目录,告诉内核"以后从今天起,路径解析的起点(`/`)就从这个目录开始"**。

举个例子,宿主上有这么个目录 `/opt/myrootfs`,里面长这样(就是一个迷你 Linux 文件系统的样子):

```
/opt/myrootfs/
├── bin/sh
├── lib/...
└── etc/passwd
```

调用 `chroot("/opt/myrootfs")` 之后,这个进程(及其后续 `fork` 出来的子进程)**对路径的解析,全部以 `/opt/myrootfs` 为 `/`**。它执行 `ls /bin`,看到的是 `/opt/myrootfs/bin`;它执行 `cat /etc/passwd`,读到的是 `/opt/myrootfs/etc/passwd`。宿主真正的 `/etc/passwd` 它够不着了——至少,看起来够不着。

> **比喻**:`chroot` 像是给集装箱的货物发了一张**伪造的"舱内地图"**,地图上把"舱内某个角落"标注成"整艘船的根"。货照着这张地图走,以为世界就这么大。但地图是假的——甲板还是那块大甲板,只是货以为走到边了。

听起来挺好,容器用它换根不就行了?Docker 早期还真就这么干过。但 `chroot` 有一个**致命的安全漏洞**,让它在"隔离不可信代码"这个场景下形同虚设。

### 不这样会怎样:chroot 的越狱祖传手法

`chroot` 的根本问题在于,它**只动了"路径解析的起点"这一个变量**,而**进程手里攥着的、指向旧根的文件描述符(fd),它一个都没动**。

这意味着:**只要进程在 `chroot` 之前,手里拿着一个指向旧根(或旧根某层目录)的 fd,它就能用这个 fd 当"跳板",逃出伪根。** 这是 Unix 安全圈几十年的祖传越狱手法,核心就两步:

1. 在 `chroot` 之前,先 `open(".")`(打开当前目录),拿到一个指向旧根下某处的 fd——记作 `fd_old`。
2. `chroot` 之后,进程对路径的解析确实被锁进了伪根;但 `fd_old` **还是指向旧根那个真实位置**。进程只要对 `fd_old` 调一次 `fchdir(fd_old)`(切换当前目录到那个 fd 指向的地方),再 `chroot(".")` 把伪根换到"当前目录"——**它就从伪根里跳出来,站到了旧根的真身上**。然后 `mkdir escape; chroot escape; ..` 一通操作,就能读到宿主真正的 `/etc/shadow`。

这套手法在 CVE 数据库里能搜出一长串。内核为了堵这个洞,加了不少补丁(比如要求 `chroot` 调用者有 `CAP_SYS_CHROOT` 能力、`chroot` 后必须 `chdir` 才生效),但**只要"换根只换路径解析、不换 fd"这个根本设计不变,就总能在特权进程身上找到逃逸路径**。

### 所以这样设计:换个彻底的

把上面这套越狱手法看明白,你就理解了**为什么容器不能只靠 `chroot`**:

> `chroot` 是个"软隔离"——它骗的是路径解析,但进程手里所有指向旧世界的"锚"(文件描述符、当前工作目录、挂载点),它一个都没回收。只要进程足够"特权"、只要它手里攥着一个旧世界的锚,它就能顺着锚爬回去。

容器要隔离的,经常是**不可信的、可能以 root 跑**的代码。对这种对手,"软隔离"等于没隔离。容器需要一个**硬隔离**:换根的时候,不仅换路径解析,**整个旧根挂载点都要从进程的视野里彻底消失**——不是"假装看不见",是"内核层面真的够不着了"。

这个硬隔离,就是 `pivot_root`。

> **一句提示**:`chroot` 并没有被淘汰,它在一些**不需要对抗特权对手**的场景里仍然好用——比如构建系统(在一个干净目录里编译软件,避免污染宿主)、跑可信的测试套件。`chroot` 的"软"在这些场景里是优点(轻、灵活)。但在"隔离一个可能作恶的容器"这种场景里,它的软就是致命伤。**工具没有好坏,只有合不合适;本章讲的是"容器为什么不能用 chroot",不是"chroot 该进垃圾堆"。**

---

## 三、pivot_root:把旧根整块挪走、再卸掉

`pivot_root` 是 Linux 2.3.41(2000 年)引入的系统调用,名字是"pivot"(枢轴、转动)——它干的事,比 `chroot"转动根的枢轴"狠得多。

它的语义是:**把当前进程的根文件系统整体"转"一下——新的根挂载点顶上去成为 `/`,旧的根被挪到一个指定目录下,然后你可以把旧根整个 `umount` 掉**。

### 它怎么转的:三个角色

`pivot_root(new_root, put_old)` 需要两个参数,加上"当前的根",一共三个角色:

- **当前根**(old `/`):进程现在踩着的根,也就是宿主那个真根。
- **`new_root`**:你想换上去的新根——通常就是你准备好的那块 rootfs。**它必须是一个挂载点**(不能随便是个普通目录,得是 `mount` 挂上去的)。
- **`put_old`**:旧根被挪去的位置——**它必须在 `new_root` 之下**(是 `new_root` 的子目录),且它自己也得是个挂载点(或可以挂的地方)。

`pivot_root` 一旦执行,内核做三件事:

1. 把 `new_root` **顶到 `/`** 的位置——从此进程的 `/` 指向它。
2. 把原来的旧根,挪到 `put_old` 这个位置——旧根现在挂在新根下面某个角落,成了新根里的一个子目录。
3. 把进程的根(`root`)和当前目录(`pwd`)凡是指向旧根的,统统更新到新根——**这一步是关键**,它意味着进程的"当前所在",被整体搬到了新根上,没有给旧根留任何"锚"。

做完这三步,你再 `umount(put_old)`——**旧根就彻底从进程的挂载树里消失了**。进程再怎么折腾,也碰不到旧根了,因为旧根已经不在它的世界里了。

> **比喻**:`pivot_root` 不是"给货物发张假地图",而是**真把整块甲板换掉了**。起重机把货物抬起来,底下塞进一块全新的、独立的小甲板(new_root),然后把原来那块大甲板(old root)整个抽走、吊离现场(挪到 put_old 再 umount)。货物重新落地时,脚下踩的、四周接触的,全是新甲板——**旧甲板已经不在它的物理世界里了**,不是它"看不见",是它**够不着**。

### 不这样会怎样:为什么这些"约束"是必需的

注意 `pivot_root` 那一堆**约束**:

- `new_root` 和 `put_old` **都得是挂载点**(所以你得先 `mount --bind` 把 rootfs 弄成挂载点);
- `put_old` 必须在 `new_root` 之下;
- `new_root` 不能是当前根本身,也不能是当前根的挂载点的某个怪异子树;
- 当前根、`new_root`、`put_old` 三者涉及的挂载点,**都不能是 shared(共享传播)的**。

这些约束看起来啰嗦,但每一条都在堵洞:

- **"必须是挂载点"**:因为 `pivot_root` 操作的是**挂载点**,不是普通目录。它要在挂载树(内核里那棵 mount tree)上做手术,只能在节点(挂载点)之间搬。一个普通目录不是挂载树的节点,没法 pivot。
- **"`put_old` 在 `new_root` 之下"**:因为旧根要挪过去,它得有个落脚点;而这个落脚点必须在未来的新世界里,否则旧根挪完就"飘"在挂载树外面了。
- **"不能 shared"**:shared 传播意味着这个挂载点的变化会**传播到别的 mount namespace**。如果根是 shared 的,你一 pivot,宿主的根也跟着乱套了。所以必须先把相关挂载点设成 `private` 或 `slave`,把容器这个 namespace 的挂载树和宿主**断开传播关系**。

这些约束不是 `pivot_root` 的毛病,恰恰是它**安全性的来源**——它要保证这次"换根手术"是**自洽的、不波及宿主的、不留锚的**。相比之下,`chroot` 一个约束都没有(几乎谁都能调),也就一点安全性都不保证。

### pivot_root vs chroot:一句话的语义差别

把两个并列看,差别一目了然:

| 维度 | `chroot` | `pivot_root` |
|---|---|---|
| 换的是什么 | 路径解析的**起点**(一个变量) | **根挂载点**(挂载树上的一个节点) |
| 旧根的命运 | 还在那儿,进程手里的 fd 还指它 | 被挪到别处,可整体 umount 掉 |
| 进程的"锚" | 不动(fd、pwd 仍指旧根) | 全部更新到新根(不留锚) |
| 调用门槛 | `CAP_SYS_CHROOT`,约束少 | `CAP_SYS_ADMIN`,一堆约束 |
| 隔离强度 | 软(可被特权进程逃逸) | 硬(旧根消失,无处可逃) |

一句话:**`chroot` 换的是"进程以为的根",`pivot_root` 换的是"挂载树上真正的根"。** 前者是骗进程,后者是改造世界。

> **为什么不是"二选一淘汰 chroot"**:在已经 `pivot_root` + `umount` 旧根之后,容器里其实**还会再用一次 `chroot`**——有些场景(比如 `docker exec` 进一个已经在跑的容器)没法再 pivot(因为约束太多),这时在已经隔离好的世界里用 `chroot` 是安全的(旧根早没了,逃也逃不到哪去)。所以你会在 runc 源码里看到两者**并存**:主流程用 `pivot_root`,特定 fallback 用 `chroot`。下一节就去看。

---

## 四、runc 是怎么做的:三条路、一个主路

讲清楚 `chroot` 和 `pivot_root` 的差别后,我们去看真正造容器的那个程序——`runc`——它在源码里到底怎么换根。这一节是本章的"源码高潮",代码全是 runc 仓库里真实摘出来的。

`runc` 在准备容器文件系统、最后换根这一步,走的是一个**三选一的分支**。这个分支藏在一个叫 `prepareRootfs` 的函数里。

### 入口:`prepareRootfs` 拢起整套文件系统准备

`prepareRootfs` 是 runc 容器初始化时,准备文件系统的总入口。它的活儿很多:挂 rootfs、挂 `/proc`、挂 `/dev`、配各种 bind mount……但**最后那一下,才是换根**。看它的关键片段([runc/libcontainer/rootfs_linux.go](../runc/libcontainer/rootfs_linux.go) L170 起,换根分支在 L234-L243):

```go
func prepareRootfs(pipe *syncSocket, iConfig *initConfig) (err error) {
	config := iConfig.Config
	if err := prepareRoot(config); err != nil {           // ← ① 先把 rootfs 做成一个"挂载点"
		return fmt.Errorf("error preparing rootfs: %w", err)
	}
	// ... 中间一大段:循环挂 /proc、/dev、各种 bind mount 到 rootfs 上 ...
	// 这些都挂在"将来要成为新根"的那块 rootfs 里面

	// ② 换根:三条路二选一/三选一
	if config.NoPivotRoot {
		err = msMoveRoot(config.Rootfs)                  // 路A:--no-pivot,用 MS_MOVE + chroot
	} else if config.Namespaces.Contains(configs.NEWNS) {
		err = pivotRoot(rootFd)                          // 路B(主路):有 mount namespace → pivot_root
	} else {
		err = chroot()                                   // 路C:没 mount namespace → 裸 chroot
	}
	if err != nil {
		return fmt.Errorf("error jailing process inside rootfs: %w", err)
	}
	// ... 之后的应用根挂载传播标志、cwd 准备 ...
}
```

读这个三分支,三个条件各对应一种场景,**优先级和安全强度从高到低**:

- **路 B(主路,`pivotRoot`)**:容器**新建了 mount namespace**(`configs.NEWNS`)。这是绝大多数容器的标准配置,走最安全的 `pivot_root`。**容器默认走这条。**
- **路 A(`msMoveRoot`)**:用户显式传了 `--no-pivot`(对应 `NoPivotRoot=true`)。这是给某些特殊环境(比如某些嵌套容器、或者 pivot 因内核配置/权限受限的场景)留的后门,用 `MS_MOVE` 把 rootfs 挪到 `/` 上,再 `chroot`。安全强度次于 pivot。
- **路 C(`chroot`)**:容器**根本没有 mount namespace**(罕见,通常是特殊用途)。没有独立的挂载树可 pivot,只能退回最原始的 `chroot`。

注意 runc 的注释把这个动作叫 "jailing process inside rootfs"(把进程关进 rootfs 这个牢里)——"jail"(牢)这个词很传神,正是换根要达到的效果。

### 路B 主路:`pivotRoot` 的源码

主路 `pivotRoot` 是最值得精读的。它的实现有个**很巧妙的技巧**:不创建任何临时目录,而是用 `pivot_root(".", ".")` 这种看起来诡异的调用。看真实代码([runc/libcontainer/rootfs_linux.go](../runc/libcontainer/rootfs_linux.go) L1144-L1195):

```go
// pivotRoot will call pivot_root such that rootfs becomes the new root
// filesystem, and everything else is cleaned up.
func pivotRoot(root *os.File) error {
	// While the documentation may claim otherwise, pivot_root(".", ".") is
	// actually valid. ... this allows us to pivot without creating directories in
	// the rootfs. Shout-outs to the LXC developers for giving us this idea.

	oldroot, err := linux.Open("/", unix.O_DIRECTORY|unix.O_RDONLY|unix.O_PATH, 0)  // ← 先攥住旧根的 fd
	if err != nil {
		return err
	}
	defer unix.Close(oldroot)

	// Change to the new root so that the pivot_root actually acts on it.
	if err := unix.Fchdir(int(root.Fd())); err != nil {                            // ← cwd 切到新根
		return &os.PathError{Op: "chdir", Path: root.Name(), Err: err}
	}

	if err := unix.PivotRoot(".", "."); err != nil {                                // ← 关键一击
		return &os.PathError{Op: "pivot_root", Path: ".", Err: err}
	}

	// Currently our "." is oldroot (according to the current kernel code).
	// ... fchdir(oldroot) since there isn't really any guarantee ...
	if err := unix.Fchdir(oldroot); err != nil {                                   // ← 切到旧根(现在挂在某处)
		return &os.PathError{Op: "fchdir", Path: "fd " + strconv.Itoa(oldroot), Err: err}
	}

	// Make oldroot rslave to make sure our unmounts don't propagate to the host
	if err := mount("", ".", "", unix.MS_SLAVE|unix.MS_REC, ""); err != nil {      // ← 断开传播
		return err
	}
	// Perform the unmount. MNT_DETACH allows us to unmount /proc/self/cwd.
	if err := unmount(".", unix.MNT_DETACH); err != nil {                          // ← 卸掉旧根!
		return err
	}

	// Switch back to our shiny new root.
	if err := unix.Chdir("/"); err != nil {                                        // ← 回到新根
		return &os.PathError{Op: "chdir", Path: "/", Err: err}
	}
	return nil
}
```

逐行读,这函数干的事,正好把我们前面讲的 `pivot_root` 三步**操作化**了:

1. **`linux.Open("/", ...)` 拿到 `oldroot` 这个 fd**:这就是"攥住旧根的锚"。但注意——runc 这里攥旧根 fd **不是为了逃逸**,恰恰相反,**是为了能切过去把它卸掉**。`pivot_root` 之后,进程没法再用路径访问旧根了(路径已经被锁进新根),只能靠这个 fd 才能再够到旧根、对它执行 `umount`。**同一把"fd 锚",在攻击者手里是逃逸后门,在 runc 手里是清理旧根的扳手。**
2. **`Fchdir(root)` 把 cwd 切到新根**:`pivot_root` 要求调用时进程的 cwd 在 new_root 之下(否则内核拒绝),所以先切过去。
3. **`unix.PivotRoot(".", ".")`**:关键一击。两个参数都是 `"."`——这就是 LXC 那帮人发现的技巧。正常 `pivot_root(new_root, put_old)` 要求 `put_old` 在 `new_root` 下且都得是挂载点,按字面规矩得在 rootfs 里先 `mkdir` 一个目录当 `put_old`。但内核实际上允许 `new_root` 和 `put_old` 指向**同一个挂载点**(`.`),结果是:新根顶上 `/`,旧根被挪到当前 cwd(此时仍是旧根自己的视图)。**省了创建目录的麻烦,也省了一次额外 mount。** 函数开头那段长注释就是在解释这个"看起来违规、其实合法"的用法。
4. **`Fchdir(oldroot)` + `mount(..., MS_SLAVE|MS_REC)` + `unmount(".", MNT_DETACH)`**:切到旧根(现在它被挪到某处了),把它和宿主的挂载传播断开(rslave,防止我们 umount 旧根的动作传回宿主搞坏宿主),然后**把旧根 `MNT_DETACH` 卸掉**。`MNT_DETACH` 是"懒惰卸载",允许在还有进程引用时也能卸(此时 cwd 还指着它),这正是这里需要的——马上要切走,先标记卸载。
5. **`Chdir("/")` 切回新根**:此时 `/` 已经是新根了。大功告成,进程干净地站在了自己的 rootfs 上,旧根已从挂载树里消失。

读完这个函数,你对 `pivot_root` "整体换根、卸掉旧根、不留锚" 的安全性,应该有了源码级的实感。

### 路 C:fallback 的 `chroot`

再看 fallback 那条最简单的路([runc/libcontainer/rootfs_linux.go](../runc/libcontainer/rootfs_linux.go) L1260-L1268):

```go
func chroot() error {
	if err := unix.Chroot("."); err != nil {
		return &os.PathError{Op: "chroot", Path: ".", Err: err}
	}
	if err := unix.Chdir("/"); err != nil {
		return &os.PathError{Op: "chdir", Path: "/", Err: err}
	}
	return nil
}
```

就两行:`Chroot(".")` 换根、`Chdir("/")` 切到新根的 `/`。**没有攥旧根 fd、没有 umount 旧根**——因为它走的场景(没有 mount namespace)本来就不指望硬隔离,只是个最小限度的"路径起点切换"。

这正是本章反复强调的那个对比的**代码实证**:`chroot` 路径短小轻薄(2 行),`pivotRoot` 路径繁复严谨(50 行 + 一堆约束)。**短小意味着留洞,繁复意味着堵洞。** 容器默认选繁复的那条,是有道理的。

### 还有一个前置:`prepareRoot` 把 rootfs 弄成挂载点

回看 `prepareRootfs` 开头第一行 `prepareRoot(config)`,它干的事也很关键——**`pivot_root` 要求 `new_root` 必须是挂载点**,而 rootfs 通常一开始只是个普通目录(比如 `/var/lib/docker/overlay2/<id>/merged`)。所以 `prepareRoot` 要先把它**bind mount 自己到自己**,变成一个挂载点([runc/libcontainer/rootfs_linux.go](../runc/libcontainer/rootfs_linux.go) L1106-L1120):

```go
func prepareRoot(config *configs.Config) error {
	flag := unix.MS_SLAVE | unix.MS_REC
	if config.RootPropagation != 0 {
		flag = config.RootPropagation
	}
	if err := mount("", "/", "", uintptr(flag), ""); err != nil {        // ← 把当前根设成 rslave,断开与宿主的传播
		return err
	}

	if err := rootfsParentMountPropagation(config.Rootfs, config.RootPropagation); err != nil {
		return err
	}

	return mount(config.Rootfs, config.Rootfs, "bind", unix.MS_BIND|unix.MS_REC, "")  // ← rootfs bind 到自己 → 变挂载点
}
```

最后一行 `mount(config.Rootfs, config.Rootfs, "bind", MS_BIND|MS_REC, "")` 就是经典手法:**把一个目录 bind 挂到自己身上,它就从一个普通目录变成了一个挂载点**——这样后面的 `pivot_root` 才认它。这一步看似多此一举,却是 `pivot_root` 那条约束("new_root 必须是挂载点")的直接产物。

> 三条路 + 一个前置准备,合起来就是 runc 的"换根"全套。**没有任何神秘力量**——就是把"内核早就有的 `pivot_root` / `chroot` 系统调用,按容器的要求组合起来用"。这再次印证全书第一性原理:容器 = 内核能力的组合。

---

## 关键源码精读:内核里 `pivot_root` 到底干了什么

讲了 runc 怎么调,最后去内核里看一眼 `pivot_root` 这个系统调用**自己**长什么样——看看它"挪挂载点"这个动作,在内核代码里是怎么落地的。这一节是本章"源码精读"的压轴。

内核里 `pivot_root` 的实现分两层:**系统调用入口 `SYSCALL_DEFINE2(pivot_root)`**(做参数解析)和**真正干活的 `path_pivot_root()`**(做挂载树手术)。两者都在 [fs/namespace.c](https://github.com/torvalds/linux/blob/master/fs/namespace.c)(`path_pivot_root` 在 L4661 起,`SYSCALL_DEFINE2(pivot_root)` 在 L4757 起)。

### 入口:系统调用只是个"参数解析壳"

系统调用入口很薄,只把用户态传来的两个路径字符串(`new_root`、`put_old`)解析成内核的 `struct path`,然后丢给 `path_pivot_root`([fs/namespace.c](https://github.com/torvalds/linux/blob/master/fs/namespace.c#L4757-L4775)):

```c
SYSCALL_DEFINE2(pivot_root, const char __user *, new_root,
		const char __user *, put_old)
{
	struct path new __free(path_put) = {};
	struct path old __free(path_put) = {};
	int error;

	error = user_path_at(AT_FDCWD, new_root,
			     LOOKUP_FOLLOW | LOOKUP_DIRECTORY, &new);
	if (error)
		return error;

	error = user_path_at(AT_FDCWD, put_old,
			     LOOKUP_FOLLOW | LOOKUP_DIRECTORY, &old);
	if (error)
		return error;

	return path_pivot_root(&new, &old);
}
```

注意 `LOOKUP_DIRECTORY`——它要求 `new_root` 和 `put_old` 解析出来必须是**目录**(不能是文件),这是 `pivot_root` 约束的第一道关卡,在入口处就挡掉了。

### 核心:`path_pivot_root` 的"一堆约束检查 + 挂载树手术"

真正的重头戏是 `path_pivot_root`。这个函数前半段全是**约束检查**(对应我们前面讲的那些"约束"),后半段才是**真正改挂载树**。先看约束检查([fs/namespace.c](https://github.com/torvalds/linux/blob/master/fs/namespace.c#L4661-L4710)):

```c
int path_pivot_root(struct path *new, struct path *old)
{
	struct path root __free(path_put) = {};
	struct mount *new_mnt, *root_mnt, *old_mnt, *root_parent, *ex_parent;
	int error;

	if (!may_mount())
		return -EPERM;                              // ← 必须有挂载权限
	error = security_sb_pivotroot(old, new);       // ← LSM 安全钩子
	if (error)
		return error;

	get_fs_root(current->fs, &root);               // ← 取出当前根

	LOCK_MOUNT(old_mp, old);
	old_mnt = old_mp.parent;
	// ...
	new_mnt = real_mount(new->mnt);
	root_mnt = real_mount(root.mnt);
	ex_parent = new_mnt->mnt_parent;
	root_parent = root_mnt->mnt_parent;
	if (IS_MNT_SHARED(old_mnt) ||                  // ← 约束:涉及的挂载点都不能是 shared
	    IS_MNT_SHARED(ex_parent) ||
	    IS_MNT_SHARED(root_parent))
		return -EINVAL;
	if (!check_mnt(root_mnt) || !check_mnt(new_mnt))  // ← 约束:得在当前 mnt namespace 里
		return -EINVAL;
	if (new_mnt->mnt.mnt_flags & MNT_LOCKED)          // ← 约束:不能是锁定的挂载点
		return -EINVAL;
	if (d_unlinked(new->dentry))
		return -ENOENT;
	if (new_mnt == root_mnt || old_mnt == root_mnt)
		return -EBUSY;                             // ← 约束:new/old 不能就是当前根
	if (!path_mounted(&root))
		return -EINVAL; /* not a mountpoint */     // ← 约束:当前根必须是挂载点
	if (!mnt_has_parent(root_mnt))
		return -EINVAL; /* absolute root */
	if (!path_mounted(new))
		return -EINVAL; /* not a mountpoint */     // ← 约束:new_root 必须是挂载点!
	if (!mnt_has_parent(new_mnt))
		return -EINVAL; /* absolute root */
	/* make sure we can reach put_old from new_root */
	if (!is_path_reachable(old_mnt, old_mp.mp->m_dentry, new))
		return -EINVAL;                            // ← 约束:put_old 必须能从 new_root 到达(即 put_old 在 new_root 下)
	/* make certain new is below the root */
	if (!is_path_reachable(new_mnt, new->dentry, &root))
		return -EINVAL;
	// ... 接下来是真正的挂载树手术 ...
```

把这段逐条读下来,你会发现——**前面讲的 `pivot_root` 那一堆约束,全在这里一条一条地硬检查**:

- "new_root 必须是挂载点" → `path_mounted(new)` 返回 false 就 `-EINVAL`;
- "put_old 必须在 new_root 之下" → `is_path_reachable(old_mnt, ..., new)` 检查从 new 能不能走到 old;
- "不能 shared" → `IS_MNT_SHARED(...)` 三连查;
- "要有权限" → `may_mount()` 和 `security_sb_pivotroot`。

**任何一个约束不满足,直接返回错误,手术根本不做。** 这就是为什么 `pivot_root` 比 `chroot"难用"**——它不是难用,是它**先确认这次换根是安全的、自洽的,才肯动手**。`chroot` 没这些检查,所以谁都能调、调完就生效,代价是留下逃逸口。

### 手术:三步把根挪走

约束全过了,才是真正的挂载树手术([fs/namespace.c](https://github.com/torvalds/linux/blob/master/fs/namespace.c#L4711-L4729)):

```c
	lock_mount_hash();
	umount_mnt(new_mnt);                                  // 先把 new 从当前位置摘下来
	if (root_mnt->mnt.mnt_flags & MNT_LOCKED) {
		new_mnt->mnt.mnt_flags |= MNT_LOCKED;
		root_mnt->mnt.mnt_flags &= ~MNT_LOCKED;
	}
	/* mount new_root on / */
	attach_mnt(new_mnt, root_parent, root_mnt->mnt_mp);   // ← ① 把 new_root 顶到原来根的位置
	umount_mnt(root_mnt);
	/* mount old root on put_old */
	attach_mnt(root_mnt, old_mnt, old_mp.mp);             // ← ② 把旧根挪到 put_old 位置
	touch_mnt_namespace(current->nsproxy->mnt_ns);
	/* A moved mount should not expire automatically */
	list_del_init(&new_mnt->mnt_expire);
	unlock_mount_hash();
	mnt_notify_add(root_mnt);
	mnt_notify_add(new_mnt);
	chroot_fs_refs(&root, new);                           // ← ③ 更新所有进程的 root/pwd 引用
	return 0;
}
```

这三步,精确对应我们前面讲的"pivot_root 三件事":

1. **`attach_mnt(new_mnt, root_parent, root_mnt->mnt_mp)`**:`new_root` 被挂到原来根在挂载树里的那个位置(`root_parent` 是旧根的父挂载点,`root_mnt->mnt_mp` 是旧根占据的挂载点)。**从此挂载树里那个"根的位置",装的是 `new_root`。**
2. **`attach_mnt(root_mnt, old_mnt, old_mp.mp)`**:旧根(`root_mnt`)被摘下来,挂到 `put_old`(`old_mnt` 上的 `old_mp`)那里。**旧根没消失,它被挪到 new_root 下方的 put_old 位置了**——所以进程还能(短暂地)够到它,准备卸载。
3. **`chroot_fs_refs(&root, new)`**:遍历系统里所有进程,凡是 `root`(根)或 `pwd`(当前目录)还指向旧根的,**统统更新到新根**。**这一步是 `pivot_root` 比 `chroot"安全"的内核级保证**——`chroot` 不动这些引用,所以旧根的"锚"还在;`pivot_root` 主动把所有锚搬到新根上,进程再无借口回到旧根。

读完这段内核代码,"pivot_root 整体换根、不留锚"就不再是抽象口号,而是 `attach_mnt` + `chroot_fs_refs` 这几个函数的具体动作。做完这三步,进程的挂载树里,根已经是 new_root,旧根被晾在 put_old——接下来用户态(比如 runc)只要 `umount(put_old)`,旧根就从这棵挂载树里彻底蒸发。

> **顺带一提 `chroot` 的内核实现**:对比着看,内核里 `chroot` 系统调用([fs/open.c](https://github.com/torvalds/linux/blob/master/fs/open.c) 里的 `SYSCALL_DEFINE1(chroot)`)就薄得多——检查一下 `CAP_SYS_CHROOT` 权限和路径合法性,然后调 `set_fs_root()` **只改当前进程的 `fs->root` 这一个字段**。**没有 `attach_mnt`、没有 `chroot_fs_refs`、不动挂载树、不碰别的进程的引用。** 这一对比,你就能从源码层面看清:`chroot` 换的是"进程私有的一根指针",`pivot_root` 换的是"挂载树上一整个节点"——安全强度的差距,根子上就在这儿。
>
> (注:`chroot` 系统调用在 [fs/open.c](https://github.com/torvalds/linux/blob/master/fs/open.c) 里,行号随内核版本变动较大,此处只标文件,不标具体行。)

---

## 章末小结

### 用航运比喻回顾本章

回到港口。前两章,我们给集装箱装上了**不透光的铁皮舱壁**(namespace)和**载重限量器**(cgroup)。但本章一开头就戳破一个洞:**舱壁围住的,到底是哪一片甲板?** 如果不处理,舱里的货一低头,踩的还是货轮那块公用大甲板——它能读到船长航海日志(`/etc/shadow`)、能翻别的货物的存放区(宿主的家目录)。

这一章干的,就是**把集装箱连根拔起来,挪到它自己那块独立的小甲板上**——这就是 rootfs + 换根。

两把工具,两种挪法:

- **`chroot`(老挪法)**:给货物发一张**伪造的"舱内地图"**,地图上把舱内某角落标成"船的根"。货照着地图走,以为世界就这么大。但地图是假的——**大甲板还在那儿**,只是货以为走到边了。手里攥着旧地图的锚(文件描述符)的特权货,能顺着锚爬回大甲板。
- **`pivot_root`(新挪法,容器默认)**:**真把大甲板抽走、塞进一块全新的小甲板**。起重机(new_root)顶上去,旧大甲板被吊到角落(put_old)、然后**整个吊离现场**(umount)。货重新落地时,四周接触的全是新甲板——**旧甲板已经不在它的物理世界里了**,不是"看不见",是"够不着"。

runc 这个"起重机"程序,默认走 `pivot_root` 这条主路;只有在用户显式说"别 pivot"(`--no-pivot`)、或者容器压根没 mount namespace 时,才退回 `chroot`。**这不是 runc 偷懒,是它根据"对手有多强"选工具:对可能作恶的容器,用硬隔离;对可信的特殊场景,用软隔离。**

### 本章在全书主线中的位置

记住全书的二分法:**打包隔离 vs 调度编排**。

这一章,我们补上了**"打包隔离"这半边的最后一块基石**:

- 第 2 章 namespace(舱壁)→ 隔离"进程看见的世界"。
- 第 3 章 cgroup(配额)→ 隔离"进程能用的资源"。
- 第 4 章 rootfs + pivot_root(小甲板)→ **隔离"进程踩在哪个文件系统上"**。

三块基石凑齐,容器里的进程才真正"住进了一个自己的、与宿主隔绝的小世界"——它有自己看到的进程表(namespace)、自己的资源限额(cgroup)、**自己的文件系统根(rootfs + 换根)**。从内核视角看,这三件事**全是内核早已有的能力**(namespace、cgroup、`pivot_root`/`chroot` 系统调用)的组合——再次印证第一性原理:**容器没有发明新东西,它只是把内核早就摆在那儿的零件,按"造一个隔离世界"的需求拼起来**。

拼完这三块,容器**隔离**这半边就齐了。接下来第 5 章开始,进入**打包**这半边——但有一个马上就会冒出来的问题:**这块换上去的小甲板(rootfs),它的内容是哪儿来的?是每次都从零拷贝一整个文件系统吗?** 当然不是。下一章,我们就去看 rootfs 背后那个让镜像秒级启动、几十个容器共用大部分内容的机制——**联合文件系统 overlayfs**。

### 五个"为什么"清单

如果你只能记五件事,记这五件:

1. **为什么光有 mount namespace 不够**:mount namespace 只换了"哪些挂载点可见"的**视图**,没换 `/` 这个**根**。进程照样能 `cd /` 看到宿主整个文件系统。必须主动把根换掉。
2. **`chroot` 为什么不安全**:它只改"路径解析的起点"这一个变量,**进程手里指向旧根的文件描述符,它一个都没动**。特权进程攥着旧根的 fd 当跳板,`fchdir` + `chroot` 一通操作就能逃出伪根——这是 Unix 安全圈几十年的祖传越狱手法。
3. **`pivot_root` 为什么更安全**:它**整体替换根挂载点**——new_root 顶到 `/`,旧根挪到 put_old,`chroot_fs_refs` 把所有进程的 root/pwd 引用更新到新根,然后旧根可整体 umount 掉。**内核层面保证进程再也够不到旧根**。
4. **`pivot_root` 那一堆约束是干嘛的**:new_root/put_old 必须是挂载点、put_old 在 new_root 之下、不能 shared……每条都在堵洞、保证换根手术自洽且不波及宿主。**约束多 = 安全性高**;`chroot` 没约束 = 没安全性。
5. **runc 怎么选**:默认走 `pivot_root`(主路);`--no-pivot` 或无 mount namespace 时退回 `chroot`(fallback)。**容器默认选硬隔离,只在可信特殊场景用软隔离。**

### 想继续深入,该往哪钻

- **亲手对比两把工具**:在 Linux 虚拟机上,分别用 `chroot` 和写个小程序调 `pivot_root`,体会两者差别。注意 `pivot_root` 要先 `mount --bind` 把目标目录弄成挂载点(对照本章 `prepareRoot` 那行 `mount(rootfs, rootfs, "bind", MS_BIND|MS_REC, "")`)。
- **看 runc 的换根全套**:[runc/libcontainer/rootfs_linux.go](../runc/libcontainer/rootfs_linux.go) 的 `prepareRootfs`(L170)、`prepareRoot`(L1106)、`pivotRoot`(L1144)、`chroot`(L1260)、`msMoveRoot`(L1197)。重点读 `pivotRoot` 那段 `pivot_root(".", ".")` 的长注释——它解释了一个"看似违规、实则合法"的内核技巧。
- **看内核的 `pivot_root` 实现**:[torvalds/linux 的 fs/namespace.c](https://github.com/torvalds/linux/blob/master/fs/namespace.c),`path_pivot_root`(L4661 起)和 `SYSCALL_DEFINE2(pivot_root)`(L4757 起)。对照本章"约束检查 + 三步手术"的拆解去读,会非常顺畅。再看 `chroot` 在 [fs/open.c](https://github.com/torvalds/linux/blob/master/fs/open.c) 里薄得多的实现,对比"换挂载点"vs"换路径指针"。
- **理解"换根之后,rootfs 的内容从哪来"**:这正是下一章 overlayfs 要讲的——换上去的这块小甲板,是用分层文件系统"叠"出来的,公共层复用、改动层独立。翻开下一章,你会看到容器秒级启动、几十实例共用大部分内容的秘密。

---

> 三块基石凑齐了:舱壁(namespace)、配额(cgroup)、小甲板(rootfs + pivot_root)。容器**隔离**这半边,到此齐活。但那块换上去的小甲板,**它的内容是哪来的**?为什么一个几百 MB 的镜像能秒级起、几十个容器共用大部分内容不占空间?——下一章,我们去看 rootfs 背后的**联合文件系统**。翻开 **第 5 章 · 联合文件系统 overlayfs:分层镜像为什么这么设计**。
