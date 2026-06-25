# 第 22 章 · 安全:Seccomp / AppArmor / capabilities / 容器逃逸

> **前置**:你需要先读过 [第 1 章《第一性原理:容器不是虚拟机》](P0-01-第一性原理-容器不是虚拟机.md)——记住那句被反复念叨的命门:**"所有容器共享同一个内核"**。第 1 章用它换来了"又轻又快又省",也留下了一句伏笔:"一个内核漏洞理论上能让宿主上所有容器全军覆没"。这一章,我们就来直面这个命门带来的**安全风险**,以及容器世界为了对冲它,准备了哪几道"内锁"。你最好也读过 [第 2 章 namespace](P1-02-namespace-集装箱的铁皮舱壁.md) 和 [第 3 章 cgroup](P1-03-cgroup-给进程上配额.md)——它们是隔离的"外套",本章是在外套之内再加几道锁。

> **核心问题**:容器默认常常以 root 跑,这有多危险?怎么在**不影响功能**的前提下,把这个"会跑业务的 root"手里的权限收紧到最小?
>
> 这是第 7 篇(进阶与边界)的第二章。上一章讲存储(给容器接一块不沉的仓库),本章换一个维度:**容器关进了铁皮箱、上了配额,可箱里的那个 root,手里还攥着多少能伤到船(宿主机)的钥匙?** 我们要把这些钥匙一件件清点,该收的收掉。
>
> **读完本章你会明白**:
> - 为什么"容器隔离"**不等于**"安全"——namespace 和 cgroup 只管"看不见"和"用多少",但容器里那个 root 依然握着大把内核特权,而内核是全船共用的。
> - **capabilities / seccomp / AppArmor(或 SELinux)这三层收紧各管哪一摊**:谁拆 root 的特权、谁卡系统调用、谁管"谁能访问哪个文件"。三件事互不替代,缺一不可。
> - 几个**经典容器逃逸路径**到底怎么走通的:`--privileged` 把宿主能力全开、挂 `/var/run/docker.sock` 等于把港口钥匙交出去、内核 CVE 一旦中招所有容器同归于尽。
> - 为什么 runc 的初始化里,capabilities、seccomp、AppArmor 要按那个**特定顺序**施加,以及 `NoNewPrivileges` 这一位为什么是最后一道保险。

> **逃生阀**:如果一读觉得信息量太大,先抓三句话——① 容器 = 共享内核的铁皮箱,箱隔离只是"看不见",不是"碰不到内核";② **capabilities** 把 root 拆成几十把小钥匙,只留业务需要的;**seccomp** 是系统调用的白名单门卫;**AppArmor** 是文件级强制访问控制,三道锁各管一层。③ **永远别用 `--privileged`**(等于把所有锁全拆了),它几乎是大多数容器逃逸故事的起点。这三句撑起整章。

---

## 章首·共享内核这道门,是双向的

第 1 章我们立了一个铁律:容器就是个普通进程,它和宿主机、和所有其他容器,**跑在同一个内核上**。当时我们把这点写成容器最大的优点——轻、快、省。

可这扇"共享内核"的门是**双向**的:

- **好处那一面**:容器不用自己起内核,启动只要毫秒级,一台机器跑几百个容器,内存开销远小于几十个 VM。
- **坏处那一面**(本章的主角):容器里那个进程,**调用的每一个系统调用,都直接进同一个内核**。而内核,是这艘船上**所有集装箱共用**的轮机舱。

> **比喻**:回到航运。集装箱的铁皮舱壁(namespace)让箱里的货**看不见**别的箱,载重配额(cgroup)让谁也**吃不垮**船的动力。可这两道防线,**都不挡货去碰船的轮机舱**。集装箱不是真空密封的——每件货都要用电、要排水、要靠船的动力系统活着,所以每个箱子里,都有一条**通向轮机舱的管道**(系统调用)。一个心怀恶意的货物,只要顺着这条管道钻进轮机舱,理论上就能搞坏**全船所有集装箱**赖以生存的动力。

这条"通向轮机舱的管道",就是**系统调用**。而本该守门的那个"船员",就是 root。

### 容器默认以 root 跑,这有多危险

绝大多数镜像的默认用户是 root(UID 0)。你 `docker run nginx`,里面跑的 master process 就是 root;你 `docker run alpine sh`,落地就是一个 root shell。问题来了:

> **容器里的 root,是不是真 root?**

答案让人脊背发凉:**在没做任何收紧的情况下,容器里的 root 就是宿主机内核眼中的 root。** namespace 只改了 root "看见的世界"(它看不见宿主的进程表、看不见宿主的网卡),但**没改它在内核那里的特权身份**。内核收到一个来自容器的系统调用,看的是这个进程的 **credentials(凭证)**——而它的 effective UID 是 0、effective capability set 里攥着一堆特权。

这意味着,只要这个 root 能找到**任何一条缝**钻出 namespace(逃逸),它对内核说的话,和宿主机上的 root 说的**分量一样重**。比如:

- 它有 `CAP_SYS_ADMIN`(capability 第 21 号),内核里大量"管理员才准做"的操作——`mount`、`unshare`、改 cgroup、配 namespace——对它全是放行的。
- 它有 `CAP_SYS_MODULE`(第 16 号),理论上可以往内核里**插内核模块**(`init_module`/`finit_module`),那等于直接在内核里执行任意代码——这船的轮机舱被攻陷了。
- 它能调用 `keyctl`、`bpf`、`perf_event_open` 这些历史上爆过无数 CVE 的系统调用,任何一个中招,都可能直接拿到宿主机 root。

namespace 隔离的是"视图",不是"权限"。**这正是为什么"容器隔离 ≠ 安全"**。

### 不这样会怎样:三个真实惨剧的雏形

把上面这些摆在一起,如果不做任何权限收紧,会发生什么?三条经典逃逸路径,本章后半段会逐个拆:

1. **`--privileged` 模式**:`docker run --privileged` 这一个标志,等于把容器的所有 capability 全部打开、把宿主的所有设备文件全挂进去、关掉 seccomp/AppArmor。容器里的 root 顺手 `mount /dev/sda1`,宿主机的整个根文件系统就挂进来了——**逃逸,一行命令**。
2. **挂 `/var/run/docker.sock`**:为了"让容器里能跑 docker 命令",很多人图省事把宿主的 docker socket 挂进容器。可这个 socket 是 docker daemon 的入口,容器里 root 调它,就能让宿主 daemon **起一个任意配置的新容器**——比如一个 `--privileged`、把宿主根目录挂进去的新容器,然后 `chroot` 进去。**等于把港口总钥匙给了集装箱里的货。**
3. **内核 CVE**:容器和宿主共享内核,一个内核漏洞(dirty COW、Dirty Pipe、各类 namespace/bpf/io_uring 漏洞)一旦被容器里的进程利用,直接拿到**宿主内核态**的代码执行——**全船所有集装箱同归于尽**,因为只有一个内核。

这三个惨剧的共同根源都是一句:**共享内核 + 容器内 root 握有太多特权**。本章的解药,就是把"太多特权"这三个字,一刀刀削下去。

> **本章的二分法定位**:全书的二分法是"打包隔离 vs 调度编排"。这一章站在**打包隔离**这一侧,是它的一道"安全收口"——第 1~4 篇讲怎么用 namespace/cgroup/rootfs/overlay 把一个应用关进铁皮箱,本章讲"关进去还不够,得再上几道内锁,把这个箱里的 root 削成一个只能干本职活的最小权限进程"。它和第 23 章(容器的边界)是姐妹篇:本章讲"怎么收紧",第 23 章讲"收紧也有极限,何时该上 VM"。

---

## 一、capabilities:把 root 的特权拆成几十把小钥匙

第一道锁,从"root 握有什么特权"下手。

### 什么是 capabilities:root 不再是"全有或全无"

历史上,Unix 的特权模型极其粗暴:**UID 0(root)啥都能干,非 0 啥特权都没有**。这是"全有或全无"(all-or-nothing)模型——你要让一个程序能绑定 80 端口(特权操作),就得给它 root,可一旦给了 root,它就**同时**获得了改内核、杀任何进程、读任何文件的全部权力。这就像为了让门卫能开门,把整栋楼的万能钥匙都塞给了他。

Linux 从 2.2 开始,把"root 的那一坨特权"拆成了一组细粒度的开关,每个开关叫一个 **capability(能力)**。一个进程要干一件特权操作,内核不再只看"你是不是 UID 0",而是看"你**手里有没有这一项 capability**"。

> **首次出现,一句话解释**:**capabilities** = 把传统"root 啥都能干"这坨特权,拆成几十项彼此独立的小权限(比如"能不能绑定小于 1024 的端口"、"能不能改文件属主"、"能不能插内核模块"),进程各持其需,不必为了一件小事背一整个 root 的特权。

具体拆成多少项?看内核头文件 [include/uapi/linux/capability.h](https://github.com/torvalds/linux/blob/master/include/uapi/linux/capability.h)。截至当前内核,从 `CAP_CHOWN`(0 号)到 `CAP_CHECKPOINT_RESTORE`(40 号)共 **41 项**:

```c
#define CAP_CHOWN            0      /* 改文件属主 */
#define CAP_DAC_OVERRIDE     1      /* 绕过文件的读/写/执行权限检查 */
#define CAP_KILL             5      /* 给别的 UID 的进程发信号 */
#define CAP_SETGID           6      /* 改 GID */
#define CAP_SETUID           7      /* 改 UID */
#define CAP_NET_BIND_SERVICE 10     /* 绑定 <1024 的端口 */
#define CAP_NET_ADMIN        12     /* 改网络配置、改路由表、改网卡 */
#define CAP_NET_RAW          13     /* 用 RAW socket(ping、抓包都要它) */
#define CAP_SYS_MODULE       16     /* 插/删内核模块 —— 最危险之一 */
#define CAP_SYS_RAWIO        17     /* 直接 I/O,ioperm/iopl */
#define CAP_SYS_CHROOT       18     /* 调 chroot */
#define CAP_SYS_PTRACE       19     /* ptrace 任何进程 */
#define CAP_SYS_ADMIN        21     /* 万能钥匙:mount/unshare/配 cgroup/... 最危险的"杂项管理员" */
#define CAP_SYS_BOOT         22     /* reboot */
#define CAP_MKNOD            27     /* mknod 建设备文件 */
#define CAP_AUDIT_WRITE      29
#define CAP_SETFCAP          31     /* 给文件设 capability */
...
#define CAP_PERFMON          38
#define CAP_BPF              39     /* 加载 BPF 程序 */
#define CAP_CHECKPOINT_RESTORE 40
#define CAP_LAST_CAP         CAP_CHECKPOINT_RESTORE
```

> **航运比喻**:传统 root = "港口总经理",一把万能钥匙开所有门。capabilities = 把"总经理"的权力拆成 41 把小钥匙——开某扇仓门的、开某台吊车的、开配电箱的……员工只领**本职需要的那几把**,不必为开一扇门而拿走整串钥匙。`CAP_SYS_ADMIN` 是其中最危险的一把,因为它管的事太杂(mount、namespace、cgroup 全沾),被称为"新的 root"——容器裁权,首要目标就是把它拿掉。

### 不这样会怎样:一个最小 Web 服务的过度授权

设想一个跑在容器里的 nginx。它真正需要的特权其实就那么几样:

- 绑定 80/443 端口(需要 `CAP_NET_BIND_SERVICE`);
- 改自己的 UID(nginx master 先以 root 起来绑定端口,再 `setuid` 到 `nginx` 用户,需要 `CAP_SETUID`/`CAP_SETGID`);
- **仅此而已**。

可如果它以裸 root 跑(没裁 capability),它**同时**握着 `CAP_SYS_MODULE`(能插内核模块)、`CAP_SYS_ADMIN`(能 mount)、`CAP_SYS_PTRACE`(能 ptrace 宿主进程)、`CAP_NET_ADMIN`(能改宿主路由)……这一堆权限,nginx 一辈子都用不上,却全在它兜里。一旦 nginx 被攻陷(RCE 漏洞),攻击者立刻拿到这一整串特权——他可以 ptrace 出 namespace、mount 宿主磁盘、插内核模块常驻。**业务用不到的特权,就是白白送给攻击者的跳板。**

### 所以这样设计:裁掉用不到的 capability,只留需要的

容器的做法是**白名单**:启动时,按镜像/运行时配置,把容器进程**需要**的那几项 capability 留在它的 effective set 里,其余全部**拿掉**。Docker 默认给容器的 capability 集合是一个精挑细选的子集(`CAP_CHOWN`、`CAP_DAC_OVERRIDE`、`CAP_FSETID`、`CAP_KILL`、`CAP_SETGID`、`CAP_SETUID`、`CAP_SETPCAP`、`CAP_NET_BIND_SERVICE`、`CAP_NET_RAW`、`CAP_SYS_CHROOT`、`CAP_MKNOD`、`CAP_AUDIT_WRITE`、`CAP_SETFCAP`),**特意不含** `CAP_SYS_ADMIN`、`CAP_SYS_MODULE`、`CAP_SYS_PTRACE`、`CAP_NET_ADMIN` 这些高危项。想加?用 `--cap-add`;想再砍?用 `--cap-drop`。

这里要展开一个关键概念——capability 不是"一坨",而是分**五个集合**(sets),分别管不同时机。runc 的配置结构 [Capabilities](../runc/libcontainer/configs/config.go#L456-L467) 就是按这五个集合定义的:

```go
type Capabilities struct {
    // Bounding is the set of capabilities checked by the kernel.
    Bounding []string `json:"Bounding,omitempty"`
    // Effective is the set of capabilities checked by the kernel.
    Effective []string `json:"Effective,omitempty"`
    // Inheritable is the capabilities preserved across execve.
    Inheritable []string `json:"Inheritable,omitempty"`
    // Permitted is the limiting superset for effective capabilities.
    Permitted []string `json:"Permitted,omitempty"`
    // Ambient is the ambient set of capabilities that are kept.
    Ambient []string `json:"Ambient,omitempty"`
}
```

五个集合的分工,记住最关键的两个就够入门:

- **Bounding set(边界集)**:**天花板**。一个进程能持有的所有 capability,不能超出这个集合。**裁权最重要的就是先把 bounding set 砍小**——天花板压低了,后面 exec 出来的子进程也甭想再拿到被砍掉的那些。
- **Effective set(有效集)**:**此刻真正生效**的特权。内核检查权限,看的就是这一项。Permitted 是 effective 的"上限池",Inheritable/Ambient 管 exec 后能不能继承——这三项本章不展开。

> **为什么要分边界集和有效集?** 因为 root 进程经常要 `fork`+`exec` 子进程(比如 nginx master exec 出 worker)。如果只有 effective set,你没法保证"exec 之后子进程也别拿到危险能力";有了 bounding set 当天花板,无论 exec 多少代,**后代都翻不出这个天花板**。这正是 runc 在 `setupUser`(切 UID)**之前**先砍 bounding set 的原因——晚了就压不住了。

---

## 二、seccomp:系统调用层的白名单门卫

第二道锁,从"容器进程能调用哪些系统调用"下手。capabilities 卡的是"某项特权操作准不准",seccomp 卡的是更底层的"**这个系统调用本身,能不能进内核**"。

### 什么是 seccomp:在内核门口设个 BPF 门卫

Linux 内核有 400 多个系统调用。对绝大多数业务进程,真正用得到的也就几十个(`read`/`write`/`open`/`close`/`socket`/...),而剩下那一大批——`keyctl`、`ptrace`、`bpf`、`perf_event_open`、`unshare`、`clone3`、`io_uring_setup`……——它一辈子都不会碰,可这些恰恰是历史上 CVE 的"重灾区"。

> **首次出现,一句话解释**:**seccomp**(secure computing mode)= 给单个进程装一个**系统调用过滤器**:在它发出每个系统调用**进入内核之前**,先用一段 BPF 字节码判一下,命中黑名单的(或不在白名单的)直接挡回去,根本不让它进内核。它把"减少攻击面"这件事做到了系统调用这一层。

seccomp 有两种模式:

- **strict 模式(mode 1)**:最早的版本,只允许 `read`/`write`/`exit`/`sigreturn` 四个调用,其余一律杀进程。太死板,几乎没人用。
- **filter 模式(mode 2)**:现代版本,允许用户用 **BPF 程序**自定义过滤规则。这就是容器用的。每条规则可以是"允许这个调用"、"拒绝并返回 errno"、"直接杀进程"、"交给用户态代理裁决(notify)"。

filter 模式的内核入口是 [kernel/seccomp.c](https://github.com/torvalds/linux/blob/master/kernel/seccomp.c) 里的 `seccomp_set_mode_filter`([L1957](https://github.com/torvalds/linux/blob/master/kernel/seccomp.c#L1957))。每次有系统调用发生时,内核在 `__secure_computing`([L1389](https://github.com/torvalds/linux/blob/master/kernel/seccomp.c#L1389))里调 `__seccomp_filter`([L1260](https://github.com/torvalds/linux/blob/master/kernel/seccomp.c#L1260)),用 BPF 跑一遍规则,再根据返回的动作(`SECCOMP_RET_ALLOW` / `SECCOMP_RET_ERRNO` / `SECCOMP_RET_KILL_PROCESS`…)决定放行还是拦截。

### 不这样会怎样:每个系统调用都是一颗潜在的雷

为什么要在系统调用这层设防?因为**内核攻击面 = 系统调用面**。内核漏洞几乎全发生在"处理某个系统调用"的代码路径里:

- `keyctl` 出过多次信息泄漏/提权 CVE;
- `bpf`/`perf_event_open` 因为允许用户态加载代码到内核,是提权的常客;
- `io_uring`、`userfaultfd` 这些"新特性"syscall,近年被反复用来做利用链;
- `unshare`/`clone` 配合 namespace,是逃逸的常用工具。

对容器里一个普通的 web 进程,这些调用它根本用不上,却**全部对它敞开**(默认内核不挡)。一旦容器里的进程被攻陷,攻击者第一件事就是去试这些"高危 syscall"有没有可用的 CVE。**把它们在 seccomp 层直接挡死,等于把这些雷的引信全拆了——内核即使有漏洞,攻击者根本调不到那行代码。**

### 所以这样设计:白名单——默认拒绝,只放业务需要的

Docker / containerd 给容器套的默认 seccomp profile(源在 [moby/profiles/seccomp/default.json](https://github.com/moby/profiles/blob/main/seccomp/default.json))是一份**白名单**:

- `defaultAction` = `SCMP_ACT_ERRNO`:意思是"**任何没被明确点名的系统调用,默认拒绝,返回 EPERM**"。
- `syscalls` 数组里列出一批 `action: SCMP_ACT_ALLOW` 的条目:这些是**放行**的调用(`read`/`write`/`open`/`socket`/`connect`/……几十个常用调用)。
- 少数高危调用(`keyctl`、`ptrace`、`init_module`/`finit_module`、`kexec_load`、`unshare` 的某些参数组合…)被**显式点名拒绝**。

合起来的效果:**默认一个都不让过,只有白名单里点到的才放行**。这把容器的内核攻击面从 400+ 个系统调用,一下子砍到几十个——剩下的几百个(连同它们的 CVE)对容器进程**根本不可达**。

> **为什么是白名单而不是黑名单?** 因为新内核一直在加新系统调用(`clone3`、`openat2`、`io_uring_*`、`landlock_*`…),黑名单(默认放行,只列禁项)永远追不上新调用带来的新攻击面;白名单(默认拒绝,只列允许项)则天然安全——任何新调用出来,容器都拿不到,除非 profile 主动加进去。**默认拒绝,是安全设计的第一性原理**。

### 一条规则的细节:seccomp 能卡到"参数"

seccomp 不止能"按系统调用名卡",还能**按参数卡**。比如 `clone` 这个调用,容器里是**要用**的(`fork` 都走它),但 `clone(CLONE_NEWUSER | CLONE_NEWNET | ...)` 这种"凭空造新 namespace"的用法就该挡。seccomp 规则可以写成:`clone` 允许,但**第一个参数(flags)不能包含某些位**(比如不许带 `CLONE_NEWUSER`)。runc 转译这种参数规则在 [matchCall](../runc/libcontainer/seccomp/seccomp_linux.go#L269) 里——它会检查 `call.Args`,把每个参数的条件(`EqualTo`/`MaskEqualTo`/...)转成 libseccomp 的 `ScmpCondition`。这种"允许调用但限制参数"的能力,让 seccomp 既能放行业务、又能卡死危险用法,精度远高于"全开/全关"。

---

## 三、AppArmor / SELinux:文件级的强制访问控制

第三道锁,从"这个进程能访问哪些文件、能干哪些操作"下手。capabilities 和 seccomp 都偏"系统能力",可一个被攻陷的 root 进程,**就算没了高危 capability,照样能读 `/etc/passwd`、能写容器里所有文件**——文件权限这道门,要靠 MAC 来锁。

### DAC 不够:root 在自己的箱里仍是霸王

Linux 默认的文件权限模型叫 **DAC(Discretionary Access Control,自主访问控制)**——就是 `ls -l` 看到的 `rwxr-xr-x` 那一套:文件属主决定谁能访问。DAC 在容器**内部**够用,但它有个致命缺口:**root 能绕过几乎所有 DAC 检查**(`CAP_DAC_OVERRIDE`)。也就是说,容器里一旦是 root,它对箱里的文件几乎是想读就读、想写就写——哪怕文件权限是 `000`。

更关键的是,DAC **不认识"这个进程是干嘛的"**。它只看 UID/GID。可安全策略往往想问的是:"nginx 这个进程,只准读 `/usr/share/nginx/html`,不准碰 `/etc/shadow`,不准写 `/usr/bin`"——这种**以程序身份为中心**的规则,DAC 表达不了。

### MAC:再加一道"进程身份 vs 资源标签"的强制策略

**MAC(Mandatory Access Control,强制访问控制)** 就是为补这个缺口生的。它不替代 DAC,而是在 DAC 之上**再加一层**由系统管理员定义、进程自己改不了的强制策略。

> **首次出现,一句话解释**:**MAC**(强制访问控制)= 在传统 `rwx` 权限之上,由管理员强制规定"**某类进程**能对**某类资源**做什么",进程自己(哪怕是 root)也改不了这条规则。Linux 上两大实现:**AppArmor**(用"路径名"标注资源,策略可读、上手快,Ubuntu/Debian 默认)和 **SELinux**(用"标签"标注资源,更精细但更复杂,RHEL/CentOS/Android 默认)。

容器里最常用的是 **AppArmor**。一个 AppArmor profile 大致长这样(伪代码):

```
profile docker-default flags=(attach_disconnected,mediate_deleted) {
    #include <tunables/global>
    network,                      # 允许任意网络
    capability,                   # 允许所有 capability(和 capability 裁剪正交,这里不重复卡)
    file,                         # 允许任意文件访问 (默认放行,后面用 deny 收紧)
    deny @{PROC}/* w,             # 禁止写 /proc 下大部分
    deny /sys/[^f]*/** wklx,
    deny /sys/f[^s]*/** wklx,
    deny /sys/fs/[^c]*/** wklx,
    deny /sys/fs/c[^g]*/** wklx,
    deny /sys/fs/cg[^r]*/** wklx,
    ...
    deny mount,                   # 禁止 mount
    deny ptrace (read, trace),
}
```

读这份 profile,你会看到它的思路是**默认放开常见操作、显式 deny 危险路径和危险操作**。它卡的是 DAC 管不到的"哪怕你是 root,也不能 mount、也不能 ptrace、也不能写 /proc 的敏感项"。这一层和 capabilities、seccomp **完全不重叠**:capabilities 管特权操作准不准,seccomp 管系统调用进不进内核,AppArmor 管"这个进程能访问哪个文件/做什么操作"——三道锁各管一摊,叠在一起才是"纵深防御"。

### runc 怎么施加 AppArmor

runc 在容器进程 `exec` 业务程序**之前**,把指定的 AppArmor profile 套到自己身上。代码在 [libcontainer/apparmor/apparmor_linux.go](../runc/libcontainer/apparmor/apparmor_linux.go):

```go
// isEnabled returns true if apparmor is enabled for the host.
var isEnabled = sync.OnceValue(func() bool {
    if _, err := os.Stat("/sys/kernel/security/apparmor"); err != nil {
        return false
    }
    buf, err := os.ReadFile("/sys/module/apparmor/parameters/enabled")
    return err == nil && len(buf) > 0 && buf[0] == 'Y'
})

// applyProfile will apply the profile with the specified name to the process
// after the next exec.
func applyProfile(name string) error {
    if name == "" {
        return nil
    }
    return changeOnExec(name)
}

// changeOnExec reimplements aa_change_onexec from libapparmor in Go.
func changeOnExec(name string) error {
    if err := setProcAttr("exec", "exec "+name); err != nil {
        return fmt.Errorf("apparmor failed to apply profile: %w", err)
    }
    return nil
}
```

注意两点:① runc 先检查宿主有没有开 AppArmor(读 `/sys/module/apparmor/parameters/enabled`),没开就跳过——容器不强求宿主必须装;② 真正的"套 profile"是写 `/proc/thread-self/attr/apparmor/exec`(`setProcAttr` 干的活),内容是 `exec <profile-name>`,意思是"**下一次 execve 时切到这个 profile**"。所以 AppArmor 是和业务进程的 `exec` 绑定的——runc 自己先套上,再 exec 出 nginx,nginx 一落地就活在 profile 的约束里。

---

## 四、三层收紧对照表:各管哪一摊

到这儿,三道锁都讲完了。它们各管一摊、互不替代,缺任何一道都会留下缺口。用一张表收束:

| 锁 | 作用层 | 卡什么 | 挡住什么类型的攻击 | 典型配置 |
|----|--------|--------|---------------------|----------|
| **capabilities** | 进程凭证(cred) | root 特权被拆成的 41 项小钥匙,只留需要的 | "拿到 root 就能为所欲为"——挡 `CAP_SYS_MODULE` 插内核、`CAP_SYS_ADMIN` mount 宿主盘等 | Docker 默认 13 项白名单;高危项全砍 |
| **seccomp** | 系统调用入口 | 哪些 syscall 能进内核(可精确到参数) | 内核 syscall 攻击面——`bpf`/`keyctl`/`perf_event_open`/`io_uring` 等的 CVE,连调用都打不进去 | 白名单,默认 `SCMP_ACT_ERRNO`,放行几十个常用 |
| **AppArmor / SELinux** | 文件/操作(MAC) | "这个进程能访问哪些路径、做什么操作" | root 绕过 DAC 读写敏感文件、非法 mount/ptrace | 默认 profile:`docker-default`,deny 掉 /proc、/sys 写和 mount |

> **航运比喻收一下**:capabilities = 把集装箱里工人手里的工具收一遍,只留本岗位要用的(裁特权钥匙);seccomp = 在箱里那根通往轮机舱的管道上装个阀门,只准过电、不准过别的(裁系统调用);AppArmor = 在箱里贴一张"这个工人只准进这几间舱、不准碰那几台机器"的强制告示(裁文件访问)。**三道全上**,箱里的 root 才从一个"拿着万能钥匙的总经理",被削成一个"只会在本岗位拧螺丝的工人"。

还有一道**非显眼但极关键**的保险,要单独点名:

### NoNewPrivileges:断掉"通过 setuid 提权"的后路

capabilities/seccomp/AppArmor 都管的是"现在这个进程",可 Linux 还有一条古老的提权路径:**setuid 程序**。`su`、`sudo`、`ping`、`passwd` 这些二进制,被设置了 setuid 位——任何进程 exec 它们,运行时就**自动获得它们的属主(通常是 root)特权**。一个被攻陷的容器进程,如果发现箱里有个 setuid-root 的 `su`,就可能 exec 它、借着它把已经被裁掉的特权**全部要回来**。

`NoNewPrivileges`(对应内核的 `PR_SET_NO_NEW_PRIVS`)就是断这条后路的:设上之后,**本进程及所有后代,exec 任何 setuid 程序都不会获得新特权**。runc 在 [standard_init_linux.go](../runc/libcontainer/standard_init_linux.go) 里施加它:

```go
if l.config.NoNewPrivileges {
    if err := unix.Prctl(unix.PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0); err != nil {
        return &os.SyscallError{Syscall: "prctl(SET_NO_NEW_PRIVS)", Err: err}
    }
}
```

注意它在 runc init 流程里出现的位置——和 capabilities/seccomp 是**协同**的。`NoNewPrivileges` 还有一个副作用:**它让"加载 seccomp filter"这个操作本身不再需要特权**(否则装 seccomp 自己就需要 `CAP_SYS_ADMIN`)。这正是 runc 对 seccomp 施加时机分两条路的原因——见下一节。

---

## 五、关键源码精读:runc 怎么按那个顺序把三道锁全上好

讲完三道锁的"是什么、为什么",我们去源码里看 runc 在容器进程 `exec` 业务程序**之前**,到底以什么顺序、怎么把它们施加到位。这一节是本章的"源码高潮",挑 [libcontainer/init_linux.go](../runc/libcontainer/init_linux.go) 的 `finalizeNamespace`、[capabilities.go](../runc/libcontainer/capabilities/capabilities.go) 的施加、以及 [standard_init_linux.go](../runc/libcontainer/standard_init_linux.go) 的 seccomp 时机来逐段读。顺序特别讲究,先看全景再拆。

### 全景:capabilities → seccomp → AppArmor,顺序为什么是这样

把 runc 的 init 主流程(`linuxStandardInit.Init()`)里和安全相关的步骤按时间排开:

```
1. prepareRootfs / pivot_root      # 先换根,否则后面操作的对象是宿主文件系统
2. apparmor.ApplyProfile(...)      # 套 AppArmor profile(在切用户前,此时还是 root)
3. 写 sysctl / readonlyPaths / maskPaths
4. 设 NoNewPrivileges(若配置)      # 断 setuid 提权后路
5. (若 !NoNewPrivileges) 先装 seccomp   # 没 NNP 时,装 seccomp 是特权操作,得在丢特权前做
6. finalizeNamespace:
   6a. ApplyBoundingSet()          # 先砍 capability 边界集(天花板压低)
   6b. setupUser()                 # 切到容器 UID(丢 root 身份)
   6c. ApplyCaps()                 # 再设最终的 effective/permitted/ambient
7. (若 NoNewPrivileges) 后装 seccomp   # 有 NNP 时,把 seccomp 放到 exec 前最后一刻
8. exec 业务进程
```

这个顺序里藏着三条**不可颠倒**的约束,逐条拆:

### 约束一:为什么先砍 bounding set,再切 UID(`setupUser`)

看 `finalizeNamespace`([init_linux.go:300-368](../runc/libcontainer/init_linux.go#L300))的核心三步:

```go
// init_linux.go L336
w, err := capabilities.New(config.Capabilities)
if err != nil {
    return err
}
// drop capabilities in bounding set before changing user
if err := w.ApplyBoundingSet(); err != nil {            // ① 先压天花板
    return fmt.Errorf("unable to apply bounding set: %w", err)
}
// preserve existing capabilities while we change users
if err := system.SetKeepCaps(); err != nil {            // ② 切 UID 时临时保留 cap
    return fmt.Errorf("unable to set keep caps: %w", err)
}
if err := setupUser(config); err != nil {               // ③ 切到容器 UID(丢 root)
    return fmt.Errorf("unable to setup user: %w", err)
}
// ...
if err := system.ClearKeepCaps(); err != nil {
    return fmt.Errorf("unable to clear keep caps: %w", err)
}
if err := w.ApplyCaps(); err != nil {                   // ④ 设最终 effective 集
    return fmt.Errorf("unable to apply caps: %w", err)
}
```

读那段注释——"drop capabilities in bounding set **before** changing user"。为什么必须在切用户前砍 bounding set?

因为 **bounding set 一旦设小,只能在"还能管理 capability"的特权态下做**。如果先 `setupUser` 切到了非 root,你**就不再有权改 bounding set 了**(改它需要 `CAP_SETPCAP`)。所以顺序必须是:**趁还是 root、还握有 `CAP_SETPCAP`,先把 bounding set 这个天花板压到配置允许的范围;切完用户后,再用 `ApplyCaps` 把 effective 集设成最终想要的子集**。

再看施加 bounding set 的实现([capabilities.go:100](../runc/libcontainer/capabilities/capabilities.go#L100)):

```go
// ApplyBoundingSet sets the capability bounding set to those specified in the whitelist.
func (c *Caps) ApplyBoundingSet() error {
    if c.pid == nil {
        return nil
    }
    c.pid.Clear(capability.BOUNDING)                       // 先清空
    c.pid.Set(capability.BOUNDING, c.caps[capability.BOUNDING]...)  # 再只设白名单里这几项
    return c.pid.Apply(capability.BOUNDING)                # 提交到内核
}
```

"清空 → 只设白名单 → 提交"——典型的白名单施加手法。提交后,这个进程及其所有后代的 capability 天花板,就被压到了配置允许的这几项,`CAP_SYS_ADMIN`/`CAP_SYS_MODULE` 这些危险项从此**永远拿不回来**(哪怕后面有 setuid 提权,也受 bounding set 限制)。

### 约束二:为什么 seccomp 分"先装"和"后装"两路

seccomp 的施加时机,在 runc 里有一条很巧的分叉。看 [standard_init_linux.go:190-202](../runc/libcontainer/standard_init_linux.go#L190):

```go
// Without NoNewPrivileges seccomp is a privileged operation, so we need to
// do this before dropping capabilities; otherwise do it as late as possible
// just before execve so as few syscalls take place after it as possible.
if l.config.Config.Seccomp != nil && !l.config.NoNewPrivileges {
    seccompFd, err := seccomp.InitSeccomp(l.config.Config.Seccomp)
    // ...
    if err := syncParentSeccomp(l.pipe, seccompFd); err != nil {
        return err
    }
}
```

这段注释把"为什么"讲透了,值得逐句读:

> "Without NoNewPrivileges seccomp is a privileged operation, so we need to do this **before dropping capabilities**"——没有设 `NoNewPrivileges` 时,装 seccomp filter **本身需要 `CAP_SYS_ADMIN`**。所以这条路必须**在 `finalizeNamespace`(丢特权)之前**先把 seccomp 装上,否则一丢特权,seccomp 就装不上了。

那另一路呢?同一个文件 [L236-250](../runc/libcontainer/standard_init_linux.go#L236):

```go
// Set seccomp as close to execve as possible, so as few syscalls take
// place afterward (reducing the amount of syscalls that users need to
// enable in their seccomp profiles). ...
if l.config.Config.Seccomp != nil && l.config.NoNewPrivileges {
    seccompFd, err := seccomp.InitSeccomp(l.config.Config.Seccomp)
    // ...
}
```

> 设了 `NoNewPrivileges` 时,装 seccomp **不再需要特权**(这是内核给 NNP 的特殊待遇),所以可以(也应该)把它**推迟到 execve 前最后一刻**。为什么越晚越好?因为 seccomp 一装上,后面任何 syscall 都要过过滤器——runc 自己在装完 seccomp 之后、exec 业务之前,还得调几个 syscall(关 pipe 之类)。seccomp 装得越晚,需要"放进白名单"的 syscall 就越少,profile 越紧。

这条分叉的精妙之处:**`NoNewPrivileges` 不只是防 setuid 提权,它还顺带解锁了"把 seccomp 装到最晚"这个能力,让 profile 能做到更小更紧**。两个看似无关的安全机制,在这里协同出了一条更优的路径。

### 约束三:seccomp 的过滤器到底怎么生成

最后看 seccomp 的核心 `InitSeccomp`([seccomp_linux.go:32](../runc/libcontainer/seccomp/seccomp_linux.go#L32)),它把 JSON profile 翻译成内核能加载的 BPF filter:

```go
// InitSeccomp installs the seccomp filters to be used in the container as
// specified in config.
func InitSeccomp(config *configs.Seccomp) (int, error) {
    if config == nil {
        return -1, errors.New("cannot initialize Seccomp - nil config passed")
    }
    defaultAction, err := getAction(config.DefaultAction, config.DefaultErrnoRet)  // ① 默认动作(SCMP_ACT_ERRNO)
    // ...
    filter, err := libseccomp.NewFilter(defaultAction)                            // ② 以默认动作为底建过滤器
    // ...
    for _, arch := range config.Architectures {                                    // ③ 加架构(x86_64/...)
        scmpArch, _ := libseccomp.GetArchFromString(arch)
        filter.AddArch(scmpArch)
    }
    // ...
    for _, call := range config.Syscalls {                                         // ④ 逐条加规则
        if err := matchCall(filter, call, defaultAction); err != nil {
            return -1, err
        }
    }
    seccompFd, err := patchbpf.PatchAndLoad(config, filter)                       // ⑤ 装载到内核
    // ...
    return seccompFd, nil
}
```

读这条流水线:runc 把运行时配置(来自 OCI spec 的 `linux.seccomp`)里的"默认动作、架构列表、每条规则(放行哪些调用、按什么参数)"翻译成 libseccomp 的 filter 对象,最后由 `patchbpf.PatchAndLoad` 把生成的 BPF 字节码**加载进内核**——之后这个进程发的每个系统调用,内核都会在 `__secure_computing`([kernel/seccomp.c:1389](https://github.com/torvalds/linux/blob/master/kernel/seccomp.c#L1389))里跑一遍这段 BPF,命中放行才进内核、命中拒绝就按 `SECCOMP_RET_*` 处理。

这里值得点一下返回值 `seccompFd`:当 profile 里用了 `SCMP_ACT_NOTIFY` 动作时,内核会返回一个文件描述符,容器外的"seccomp agent"可以通过它**拦截并裁决**容器里某些 syscall(这就是 seccomp 的 user-notify 机制,让外部代理做更复杂的判断)。代码里特意检查了一条——"`write` 这个 syscall 不能用 notify"(见 [seccomp_linux.go:62](../runc/libcontainer/seccomp/seccomp_linux.go#L62)),因为 runc 自己还要用 `write` 把这个 fd 传给父进程,如果 `write` 被 notify 卡住,整个初始化就死锁了。这种"和自己的引导过程冲突的 syscall 必须豁免"的细节,是 seccomp 实践里很容易踩的坑。

### 内核侧:系统调用到底怎么被挡下来

最后我们追到内核,看一道 seccomp 规则是怎么把一个 syscall 挡回去的。入口是 [kernel/seccomp.c](https://github.com/torvalds/linux/blob/master/kernel/seccomp.c)。每次进程发系统调用,内核在进 syscall handler 前会调 `__secure_computing`([L1389](https://github.com/torvalds/linux/blob/master/kernel/seccomp.c#L1389)):

```c
int __secure_computing(void)
{
    int mode = current->seccomp.mode;
    int this_syscall;
    // ...
    this_syscall = syscall_get_nr(current, current_pt_regs());

    switch (mode) {
    case SECCOMP_MODE_STRICT:
        __secure_computing_strict(this_syscall);  /* may call do_exit */
        return 0;
    case SECCOMP_MODE_FILTER:
        return __seccomp_filter(this_syscall, false);   // 容器走这条
    // ...
    }
}
```

容器走的是 `SECCOMP_MODE_FILTER`,进 `__seccomp_filter`([L1260](https://github.com/torvalds/linux/blob/master/kernel/seccomp.c#L1260)),它的核心是一个 switch:

```c
static int __seccomp_filter(int this_syscall, const bool recheck_after_trace)
{
    // ...
    filter_ret = seccomp_run_filters(&sd, &match);   // ① 跑 BPF 规则
    action = filter_ret & SECCOMP_RET_ACTION_FULL;   // ② 取出动作

    switch (action) {
    case SECCOMP_RET_ERRNO:
        syscall_set_return_value(current, current_pt_regs(), -data, 0);  // 返回 -EPERM
        goto skip;
    case SECCOMP_RET_TRAP:
        force_sig_seccomp(this_syscall, data, false);                    // 发 SIGSYS
        goto skip;
    // ...
    case SECCOMP_RET_ALLOW:                                              // 放行
        return 0;
    case SECCOMP_RET_KILL_THREAD:
    case SECCOMP_RET_KILL_PROCESS:                                       // 直接杀
        // ...
    }
}
```

`seccomp_run_filters`([L405](https://github.com/torvalds/linux/blob/master/kernel/seccomp.c#L405)) 就是真正跑那段 BPF 字节码的地方——它把当前 syscall 的编号和参数塞进 `struct seccomp_data`,让 BPF 程序判断,返回 `SECCOMP_RET_*` 中的一个。命中 `ERRNO` 就改返回值为 `-EPERM` 然后 `goto skip`(跳过正常 syscall 处理)、命中 `KILL_PROCESS` 就 `do_exit(SIGKILL)`。

> 注意一个关键设计:**seccomp 检查发生在 syscall 真正执行之前**(在 `__secure_computing` 这道门里)。这就是为什么它能"挡掉攻击面"——那个有漏洞的 syscall handler **根本没被调用**,攻击者连触发漏洞的机会都没有。这比"让 syscall 执行、事后再审计"强得多。代价是:seccomp filter 必须写得**正确**——一旦误拦了业务必需的 syscall,容器就会莫名其妙地报 EPERM。

至此,从 runc 的施加顺序,到 libseccomp 的 filter 生成,再到内核的 `__secure_computing` 检查路径,我们走通了一条完整的"系统调用层的门卫"链路。

---

## 六、经典容器逃逸路径:三道锁是怎么被拆掉的

讲完怎么"上锁",我们反向看:这几道锁,到底是怎么被绕过/拆掉的?理解逃逸路径,才能理解为什么前面那些"默认值"如此重要。这一节给三个经典场景,每个都对应一个被拆掉的锁。

### 路径一:`--privileged`,一把拆光所有锁的扳手

`docker run --privileged` 是最容易、也最危险的逃逸入口。这一个标志干的事:

- **把所有 capability 全开**(包括 `CAP_SYS_ADMIN`、`CAP_SYS_MODULE`、`CAP_SYS_PTRACE`……);
- **关掉 seccomp**(默认 profile 不再施加);
- **关掉 AppArmor**(默认 profile 不再施加);
- **把宿主的所有设备文件(`/dev/sda1` 等)全挂进容器**。

三道锁,一击全拆。逃逸随之变得平凡:

```bash
# privileged 容器里:
mkdir /mnt/host
mount /dev/sda1 /mnt/host        # 宿主的整个根文件系统,挂进来了
chroot /mnt host                 # 你现在就是宿主机上的 root
```

**为什么这条路径这么"顺利"?** 因为 mount 一个块设备需要 `CAP_SYS_ADMIN`——这道 capability 正是 Docker 默认裁掉的、而 `--privileged` 又开回来的。capabilities 这道锁一旦被 `--privileged` 拆掉,seccomp 和 AppArmor 也一起没了,容器里的 root 就退化回了"握有全部特权的真 root",加上宿主设备文件被挂进来,namespace 的隔离在 mount 面前形同虚设。

> **结论**:`--privileged` 几乎从来不是必要的。它存在的本意是某些特殊场景(容器内跑 docker、需要访问特定硬件),但绝大多数"图省事"的用法都是错的。需要的只是某一项 capability,就用 `--cap-add NET_ADMIN`(或别的具体那项),别整把钥匙全要。**本章最重要的一条实操建议:永远审计集群里的 `--privileged`。**

### 路径二:挂 `/var/run/docker.sock`,把港口总钥匙交出去

另一个高频踩坑:为了让"容器里能跑 docker 命令"(比如 CI/CD 容器里要 build 镜像),把宿主的 docker socket 挂进去:

```bash
docker run -v /var/run/docker.sock:/var/run/docker.sock ...
```

`/var/run/docker.sock` 是 docker daemon 的 UNIX domain socket,谁连上它、谁就能给 daemon **下发任意 docker 命令**(docker CLI 全走这个 socket)。而 docker daemon 是以宿主 root 跑的。于是容器里的进程只要:

```bash
# 容器里(装了 docker CLI):
docker run -v /:/host alpine chroot /host   # 让宿主 daemon 起一个新容器,
                                            # 把宿主根目录挂进去,再 chroot
```

——这个"新容器"是宿主 daemon 起的,**它在宿主上**,把宿主的 `/` 挂进了自己,然后 chroot 进去。容器里的攻击者,**根本没"逃"出 namespace**,他只是借 docker daemon 这个"宿主上的特权代理",让宿主自己替他开了个口子。

> **比喻**:这等于把**港口调度中心的钥匙**给了集装箱里的一件货。这件货自己出不去箱,但它能打电话给调度中心,让调度中心给它单独开一艘带万能钥匙的船。锁没坏,是有人主动从外面开了门。这条路径提醒我们:**安全不只是"收紧容器",还有"容器和宿主特权服务之间的通道"**——任何宿主特权服务的 socket(`docker.sock`、`kubelet` 的、containerd 的),挂进容器就等于送特权。

### 路径三:内核 CVE,共享内核的终极代价

前两条都是"配置失误",而最致命的逃逸路径是**内核漏洞**——它不需要 `--privileged`,不需要挂 socket,只需要容器里跑的某个程序,触发了一个内核 syscall 的 bug。

历史上有太多例子:

- **Dirty COW(CVE-2016-5195)**:一个"写时复制"竞态,让只读文件可写,绕过 DAC。容器里的进程用它改 `/etc/passwd` 之类的,直接提权。
- **Dirty Pipe(CVE-2022-0847)**:pipe 缓冲区的缺陷,允许任意覆盖只读文件内容。
- 各类 namespace/bpf/io_uring/userfaultfd 的 CVE,常被串成"从容器到宿主内核代码执行"的利用链。

**这些漏洞之所以是"容器逃逸"而不只是"提权",根本原因就是第 1 章那句命门——共享内核。** 容器里的进程触发内核 bug,攻击的是**这艘船上唯一的那个轮机舱**;一旦得手,它在内核态的代码执行,可以横扫宿主上**所有容器**(因为只有一个内核、一套进程表)。这也是为什么 capabilities/seccomp 要拼命裁 syscall:**每裁掉一个高危 syscall,就少一条触发内核 bug 的路径**。

> 三个路径合起来,正好对应三种"锁被拆"的方式:`--privileged` 是**主动拆锁**(配置错误)、挂 docker.sock 是**绕过锁走特权通道**(架构错误)、内核 CVE 是**锁和墙之外的共有地基塌方**(无法靠容器自身收紧完全防住)。前两种靠"正确配置"就能防——本章的三道锁 + 不用 privileged + 不乱挂 socket;第三种则超出了容器自身的能力边界,这正是下一章(第 23 章)要讲的事:什么时候,你得承认容器收紧也有极限,该上 VM 了。

---

## 章末小结

### 用航运比喻回顾本章

回到那片港口。前 21 章我们让集装箱跑了起来,装上了持久仓库,修通了网络通道。这一章我们做了一件朴素但关键的事:**清点箱里那个 root 手里的钥匙,把业务用不到的全收走。**

- **问题源头**:集装箱的铁皮舱壁(namespace)只让货**看不见**别的箱,配额(cgroup)只让谁也**吃不垮**船——可这两道防线都**不挡货去碰船的轮机舱**(内核)。容器里那个 root,顺着"系统调用"这根管道,理论上够得着整艘船的动力系统。而**全船只有一个内核**,一旦被攻陷,所有集装箱同归于尽。
- **capabilities**:把"总经理的万能钥匙"拆成 41 把小钥匙,只发业务要用的几把。先砍 **bounding set**(天花板),让后代也翻不出;高危的 `CAP_SYS_ADMIN`/`CAP_SYS_MODULE` 默认不给。
- **seccomp**:在系统调用这根管道上装个 BPF 阀门,**默认拒绝**,只放行白名单里几十个业务必需的 syscall。每个被挡死的 syscall,连同它身上的 CVE,对容器进程彻底不可达。
- **AppArmor / SELinux**:在文件权限之上再加一层"这个进程能访问哪些资源、做什么操作"的强制策略,堵住 root 绕过 DAC 的缺口。
- **NoNewPrivileges**:断掉"通过 setuid 程序把特权要回来"的后路,顺带解锁"seccomp 装到最晚"。
- **逃逸三路径**:`--privileged`(主动拆锁)、挂 docker.sock(走特权通道)、内核 CVE(共有地基塌方)。前两个靠正确配置防住,第三个超出容器能力。

三道锁 + NNP 是**纵深防御**:它们互不替代,任何一道被绕过,其余的还在挡。这套机制把容器里的 root,从一个"拿着万能钥匙的总经理",削成一个"只会在本岗位拧螺丝的工人"。

### 本章在全书二分法里站哪边

全书的二分法:**打包隔离 vs 调度编排**。

这一章牢牢站在**打包隔离**这一侧,是它的**安全收口**:

- 第 1~4 篇用 namespace/cgroup/rootfs/overlay 把一个应用关进铁皮箱——那是"造箱"。本章是"造完箱之后,再把箱里的权限收紧到最小"——**同一个打包隔离主题的纵深收尾**。
- 它和第 1 章"共享内核"的命门首尾呼应:第 1 章用共享内核换来了轻,**指出代价是隔离不如 VM**;本章具体讲这个代价(逃逸风险)和**怎么用最小权限原则去对冲它**。第 1 章挖的坑,本章填了一半——靠正确配置能填的那一半。
- 从内核视角回扣:capabilities 是改进程的 `struct cred` 里的 capability sets;seccomp 是给进程挂上 `struct seccomp_filter`;AppArmor 是经过 LSM 钩子(linux security module)在文件操作的路径上加策略。**这三样,底下都是内核早已准备好的安全机制**——容器运行时(runc)做的事,依然是"把内核已有的能力组合、施加到容器进程上",和 namespace/cgroup 的故事如出一辙。容器没有发明新安全机制,它只是"把内核的安全旋钮拧到位"。

### 五个"为什么"清单

如果你只能从这一章带走五件事:

1. **为什么"容器隔离"不等于"安全"**:namespace 只改"看见的世界",cgroup 只管"用多少",**都不挡容器里的 root 顺着系统调用够到内核**——而内核是全船共用的。容器里的 root,在没收紧时,对内核的特权重心和宿主 root 一样。所以"关进 namespace"不等于"关进沙箱"。
2. **三道锁各管哪一摊**:capabilities 管"root 的特权拆成 41 项,只留需要的"(裁凭证);seccomp 管"哪些系统调用能进内核"(裁入口,默认拒绝白名单);AppArmor/SELinux 管"这个进程能访问哪些文件/做什么操作"(裁文件,MAC)。三者互不替代,叠起来才是纵深防御。
3. **为什么 seccomp 用白名单而不是黑名单**:新内核一直在加新 syscall,黑名单追不上新攻击面;白名单(默认 `SCMP_ACT_ERRNO`,只放行点名的)天然安全——新调用出来容器默认拿不到。seccomp 还能**卡到参数**(允许 `clone` 但禁带 `CLONE_NEWUSER`),精度远高于全开全关。
4. **runc 施加这三道锁的顺序为什么讲究**:先砍 capability bounding set(趁还是 root,晚了改不了),再切 UID,再设 effective cap;seccomp 在没有 `NoNewPrivileges` 时必须**丢特权前**装(否则装 seccomp 自己就需要特权),有 NNP 时**推迟到 execve 前最后一刻**(profile 能更紧);AppArmor 在切用户前套,绑在 `exec` 上对业务进程生效。
5. **三个经典逃逸路径的根因**:`--privileged` 一次性拆掉所有锁(cap 全开 + seccomp/AppArmor 关掉 + 设备全挂);挂 docker.sock 是**绕过锁走特权通道**(让宿主 daemon 替你开门);内核 CVE 是**共享内核的终极代价**(一个内核被攻陷,所有容器同归于尽)。前两个靠正确配置防住,第三个超出容器能力。

### 想继续深入,该往哪钻

- **看 capabilities 的施加**:runc 的 [libcontainer/capabilities/capabilities.go](../runc/libcontainer/capabilities/capabilities.go)(`New` L50、`ApplyBoundingSet` L100、`ApplyCaps` L110),和调用方 [libcontainer/init_linux.go](../runc/libcontainer/init_linux.go) 的 `finalizeNamespace`(L300-368)。配套读内核 [include/uapi/linux/capability.h](https://github.com/torvalds/linux/blob/master/include/uapi/linux/capability.h) 看 41 项能力的定义和注释。
- **看 seccomp 的施加**:runc 的 [libcontainer/seccomp/seccomp_linux.go](../runc/libcontainer/seccomp/seccomp_linux.go)(`InitSeccomp` L32、`matchCall` L269、`getAction` L201),和施加时机的分叉 [libcontainer/standard_init_linux.go](../runc/libcontainer/standard_init_linux.go) L190 / L236。
- **看内核的 seccomp 检查路径**:[kernel/seccomp.c](https://github.com/torvalds/linux/blob/master/kernel/seccomp.c) 的 `__secure_computing`(L1389)、`__seccomp_filter`(L1260)、`seccomp_run_filters`(L405)、`seccomp_set_mode_filter`(L1957)。重点读那个 switch(L1279-1360),看每种 `SECCOMP_RET_*` 动作怎么处理。
- **看 AppArmor 的施加**:runc 的 [libcontainer/apparmor/apparmor_linux.go](../runc/libcontainer/apparmor/apparmor_linux.go)(`isEnabled`、`setProcAttr`、`changeOnExec`),理解"写 `/proc/thread-self/attr/apparmor/exec` 绑定到下一次 exec"这套机制。
- **看默认 profile 长啥样**:Docker 的默认 seccomp profile 在 [moby/profiles/seccomp/default.json](https://github.com/moby/profiles/blob/main/seccomp/default.json),AppArmor 默认 profile 在 [moby/moby 的 profiles/apparmor/template.go](https://github.com/moby/moby/blob/master/profiles/apparmor/template.go)。读它们你会对"默认挡了哪些 syscall / 哪些路径"有具体感觉。
- **亲手玩一下**:用一个不需要特权的小测试——`docker run --rm alpine sh -c 'unshare -r sh'`(默认 profile 下会被 seccomp 挡,报 EPERM);再用 `--security-opt seccomp=unconfined` 重试,对比差异。再 `docker run --rm --cap-drop ALL --cap-add NET_BIND_SERVICE nginx` 看"裁到只剩一个 cap"业务还能不能跑。

---

> 这一章,我们把箱里 root 手里的钥匙清点了一遍、该收的收了,把通向轮机舱的管道上也装了阀门。可有一个事实,从第 1 章到现在一直悬着:**所有这些锁,都建立在"内核本身是可信的"之上。** 一旦内核本身被攻陷(CVE),所有容器共享同一个轮机舱,锁再多也挡不住地基塌方。这就引出了本书的收尾之问:**容器的隔离,边界到底画在哪里?什么时候再怎么收紧,也不够、必须上 VM?** 翻开 **第 23 章 · 容器的边界:它隔离不了什么**,我们去直面共享内核的终极代价。
