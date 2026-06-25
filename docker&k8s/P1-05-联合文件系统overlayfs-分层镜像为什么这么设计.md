# 第 5 章 · 联合文件系统 overlayfs:分层镜像为什么这么设计

> **前置**:你需要先读过 [第 4 章《rootfs 与 pivot_root:换个根》](P1-04-rootfs与pivot_root-换个根.md)。那章结尾留了一个钉子——我们用 `pivot_root` 把集装箱连根拔起、挪到了它自己那块**独立的小甲板**(rootfs)上,把旧甲板整个吊离了现场。但当时刻意没回答一个马上会冒出来的问题:**这块换上去的小甲板,它的内容是哪来的?** 是每次开容器都从零拷贝一整个文件系统吗?如果是那样,一个几百 MB 的 Ubuntu 镜像,凭什么能秒级启动、几十个容器共用大部分内容还不占空间?这一章就来回答它——小甲板不是一块整板,而是**一层一层叠起来的**,公共层大家共用,改动层各写各的。这层"叠甲板"的机制,就是 **overlayfs**。

> **核心问题**:一个几百 MB 的镜像,凭什么能秒级启动、还能几十个容器共用大部分内容不占空间?
>
> 这一章拆第 1 篇的最后一块基石——**联合文件系统(overlayfs)**。前三章(namespace、cgroup、rootfs/pivot_root)解决了"隔离进程看见的世界、限资源、换根"。但 rootfs 这块根,如果每次开容器都原样复制一份,那 docker 的"秒级起、省空间"就全是空话。这一章要回答:**镜像为什么必须分层、overlay 怎么把多层叠成一个统一视图、为什么容器改文件不会污染镜像、只读层凭什么能被几十个容器共用**。
>
> **读完本章你会明白**:
> - 为什么镜像**必须分层**(不分层会怎样:每个容器拷一整份,空间爆炸、拉取爆炸);分层叠放让公共层复用,这是 docker "轻"的根源之一。
> - overlayfs 的 **lower / upper / merged** 三层是怎么叠的:多个只读 lower 叠最下、一个可写 upper 在上、merged 是统一视图——它**不是一个真目录,是内核现算出来的视图**。
> - 为什么**只读层能被几十个容器共享**(因为只读,挂多少次都安全),以及 copy-up 怎么保证"改文件不污染镜像"。
> - **写时复制(copy-up)** 的精确语义:容器第一次写某文件时,内核才把它从 lower 复制到 upper;lower 里的原文件被 upper 的副本"遮盖",镜像毫发无损。
> - **删除(whiteout)** 怎么实现:overlay 不真删 lower 的文件,而是在 upper 放一个"白板"把它遮住。

> **如果一读觉得太难**:先只记住三件事——① 镜像分层 = 公共货架层共用,改动单独一层,省空间省传输;② overlay = 多个只读 lower 叠下面 + 一个可写 upper 在上,叠出一个 merged 统一视图;③ 容器改文件时触发 copy-up,改的是 upper 副本,lower(镜像)永远不被污染。这三句撑起整章。

---

## 章首·那块小甲板,内容哪来的

第 4 章最后,我们用 `pivot_root` 把容器进程脚下的根换掉了——它现在站在自己那块独立的小甲板上。可读者只要稍微往回退一步想,一个麻烦就出现了:

> **这块小甲板,到底是块什么板?它的内容是哪来的?**

最朴素的答案:**它是一个目录,里面装着容器要跑的那个系统的文件**——比如 Ubuntu 的 `/bin /etc /usr /lib`,加上你的应用。听起来合理。但马上翻车:

- **翻车一:一个 Ubuntu 镜像几百 MB,几十个容器各开一份,就是几十 GB。** 你在一台机器上跑 50 个 nginx 容器,它们 99% 的文件(`/usr/sbin/nginx`、`/lib/x86_64-linux-gnu/libc.so.6`、`/etc/ssl/...`)是一模一样的——可如果每个容器都拷一整份,这 50 份文件加起来就是 50 倍的空间。**这不叫容器,这叫"用磁盘换隔离"**。
- **翻车二:`docker pull ubuntu` 要拉几百 MB,改一行配置再打一个镜像,难道又拉几百 MB?** 如果镜像是一整块板,你 `apt update && apt install -y curl` 改了几个文件,新镜像和旧镜像**几乎完全一样**,只差那几个被改动的文件——但传输时却要传一整个几百 MB。这在任何正经 CI/CD 里都是灾难。

这两个翻车场景,逼出一个设计:**镜像不能是一整块板,它必须是"一层一层叠起来的"。** 公共的内容(基础系统、公共库)大家共用同一层;每个改动(装个包、改个配置)单独成一层,只有改动的那一点是新增的。开容器时,把需要的几层叠在一起,就是完整的文件系统。

> **比喻**:回到航运。一个集装箱里的货架,**不是一块整板削出来的**,而是**一层一层叠的托盘**:最底下那层托盘是基础系统(Ubuntu/Alpine 的文件),上面再叠一层"装了 Python"的托盘,再叠一层"装了我的应用"的托盘。**多个集装箱如果都用"基础系统 + Python"这两层,它们共用最底下那两块托盘**(因为只读,放一起没问题);只有最上面那层"我的应用"是每个集装箱独有的。**托盘共用,空间和装卸成本都省了。**

这层"叠托盘"的机制,在容器世界叫**联合文件系统(union filesystem)**。Linux 内核里那个用得最广的实现,叫 **overlayfs**。这一章,我们就把 overlayfs 拆开:它怎么叠、怎么共享、怎么做到"改文件不污染镜像"。

---

## 一、为什么镜像必须分层:不分层的两个灾难

先把"为什么分层"这个最根本的"为什么"钉死。讲机制之前,先看不分层会怎样。

### 不这样会怎样:镜像是一整块板的灾难

假设我们退回最朴素的模型:**一个镜像就是一个完整的文件系统目录,每次改动都重新打包成一个新的整目录。** 听起来简单,但在任何真实场景里,它会在两个地方同时崩盘。

**灾难一:存储爆炸。** 你有一台机器,跑了 30 个容器,其中 20 个是 nginx、10 个是 redis。如果每个容器都拷一整份镜像文件系统:

- 20 份 nginx(每份假设 150MB)= 3GB;
- 10 份 redis(每份假设 120MB)= 1.2GB;
- 合计 4.2GB,就为了存 30 份**几乎完全相同**的内容。

可真相是:这 20 个 nginx 容器,它们 99% 的文件(`/usr/sbin/nginx`、`/etc/nginx/nginx.conf`、`/usr/lib/...` 一大堆公共库)**是一模一样的**;它们唯一的区别,只是各自运行时改动的那一点点(`/var/log/nginx/` 的日志、被改过的配置)。**为这 1% 的差异,付出 20 倍的存储,这笔账怎么算都亏。**

**灾难二:传输/拉取爆炸。** 你用 `docker pull ubuntu:22.04` 拉了一个镜像(假设 80MB),然后基于它装了个 curl,打成新镜像 `myubuntu:with-curl`。再 push 到仓库。下次另一台机器 `docker pull myubuntu:with-curl` 时:

- 如果镜像是一整块板,这台机器要重新下载整个镜像——哪怕 `myubuntu` 和原版 `ubuntu:22.04` 只差几个 curl 的二进制(几 MB),也得拉一整个 80MB+。
- 一天 CI/CD 跑几百次,每次改一行就传几百 MB,带宽和时间的浪费是天文数字。

### 所以这样设计:分层,公共层复用

这两个灾难,用一个设计同时化解:**把镜像切成一层一层的 diff,公共层只存一份、只传一次。**

具体怎么做:

- **每一层(layer)是一组文件的"变化"**:新增了哪些文件、修改了哪些文件、删除了哪些文件。注意,不是一整份文件系统,而是**相对上一层的增量**。
- **公共层只存一份。** 20 个 nginx 容器,它们的"基础系统层 + nginx 安装层"是同一份,在磁盘上只占一个 150MB,被这 20 个容器共用。
- **公共层也只传一次。** `docker pull myubuntu:with-curl` 时,docker 先算每一层的指纹(hash),发现"基础系统层"这台机器已经有了(之前 pull `ubuntu:22.04` 时拉过),**就跳过不拉**,只拉那个新增的"装了 curl"的层——几 MB 而已。

> **一组数字感受一下**:一台机器跑 20 个 nginx 容器。不分层:20 × 150MB = 3GB。分层:1 份基础 + 1 份 nginx = ~150MB,加上 20 份各几 MB 的"运行时改动层",总共可能也就 200MB 出头。**节省了一个数量级。** 这就是为什么 docker 能"一台机器跑几百个容器"——磁盘没爆,是因为绝大部分内容大家共用。

### 分层的代价:需要一个能"叠层"的文件系统

但分层引入一个新问题:**这些一层一层的 diff,怎么"叠"成一个完整的、容器进程能直接用的文件系统?**

容器进程可不管你分了多少层——它执行 `cat /etc/nginx/nginx.conf`,内核必须返回一个**完整的、最新的文件内容**,就好像 `/etc/nginx/nginx.conf` 真的躺在一个完整目录里一样。可实际上这个文件可能躺在第 3 层(基础系统层),也可能被第 5 层(配置改动层)覆盖过。

把多层 diff 叠成一个统一视图,这件事必须由**内核里的文件系统**来做——因为只有文件系统能拦截每一次 `open`/`read`/`write`/`readdir`,现算出"这个路径在多层叠加后到底长什么样"。

这个"能叠层的文件系统",就是 **overlayfs**。从下一节开始,我们拆它怎么叠。

> **铺垫一句**:这一节讲的"layer(层)",就是第 7 章《镜像的本质》要讲的镜像 layer。一个镜像,本质就是一组 layer 的有序叠加;这一章讲的是**这些 layer 在运行时怎么被叠成 rootfs**。第 7 章会反过来讲**这些 layer 在镜像里怎么打包、怎么传输**。两章是镜像这一枚硬币的两面。

---

## 二、overlayfs 的三层:lower / upper / merged

overlayfs 把"叠层"这件事,抽象成三种角色。这是理解整个机制的核心,务必先记住这三个词:

> **lower(下层,只读)**:一个或多个只读目录,叠在最底下。镜像的内容就放在这里——基础系统层在下、改动层依次往上叠。lower 可以有**多个**(因为镜像通常有很多层),它们从下到上、由旧到新依次叠加。
>
> **upper(上层,可写)**:一个**可写**的目录,叠在所有 lower 之上。容器运行时所有的改动(新建文件、修改文件、删除文件)**都写到这里**,lower 一个字节都不会动。每个容器有自己的 upper,互不干扰。
>
> **merged(合并视图)**:这不是一个真目录,而是 overlayfs 挂载出来的**统一视图**。容器进程看到的就是 merged——它把 lower + upper 叠在一起,**上层遮盖下层**(同名文件,upper 的赢),呈现给进程一个"完整文件系统"的假象。

用一个图把这三层摆清楚:

```
                  容器进程看到的(rootfs / merged 视图)
                 ┌──────────────────────────────────┐
                 │  /etc/nginx/nginx.conf           │  ← 容器改过,来自 upper
                 │  /var/log/nginx/access.log       │  ← 运行时新增,在 upper
                 │  /usr/sbin/nginx                 │  ← 没改过,来自 lower1
                 │  /usr/lib/x86_64-linux-gnu/...   │  ← 没改过,来自 lower0
                 │  /bin /etc/hostname ...          │
                 └──────────────────────────────────┘
                                    ▲
                          overlayfs 内核现算的统一视图
                                    ▲
        ┌───────────────────┬───────┴───────┬───────────────────┐
        ▼                   ▼               ▼                   ▼
  ┌──────────┐        ┌──────────┐    ┌──────────┐        ┌──────────┐
  │  upper   │  可写   │ (workdir)│    │ lower1   │  只读   │ lower0   │ 只读
  │ (本容器  │  ←───── │ 内核用   │    │ 改动层   │  ←───── │ 基础系统 │
  │  专有)   │         │ 临时目录 │    │ (装了nginx)│       │ (Ubuntu) │
  └──────────┘        └──────────┘    └──────────┘        └──────────┘
       ▲                                                    ▲
       │ 本容器独有                                          │ 多个容器共用
       │ (写时复制到这里)                                     │ (只读,挂多少次都安全)
```

这张图要传达三件事:

1. **merged 不是真目录,是内核"算"出来的**:进程访问 merged 下的任何路径,overlayfs 都要去 lower/upper 里现查"这个文件实际在哪一层",再把内容返回。进程完全感觉不到分层。
2. **upper 是本容器独有的、可写的**:容器所有的改动落在这里,所以多个容器可以共用同一组 lower,但各有各的 upper,互不污染。
3. **lower 是只读的、可共享的**:因为 lower 只读,同一份 lower 可以被几十个 overlay 挂载同时引用,内核层面保证它们看到的都是同一份数据——这就是"几十个容器共用大部分内容"的技术基础。

还有一个角色藏在图里没强调:**workdir(工作目录)**。它是 overlayfs 内核自己用的临时目录,做 copy-up 时需要一个原子性的"中转站"(在 workdir 里建临时文件、拷数据、再原子 rename 到 upper)。用户从不直接碰它,但挂载 overlay 时必须给它一个路径。

### 挂一个 overlay 长什么样:三个参数

把这套抽象落到命令上,挂一个 overlay 文件系统是这样:

```bash
mount -t overlay overlay \
  -o lowerdir=/lower0:/lower1,upperdir=/upper,workdir=/work \
  /merged
```

- `lowerdir=/lower0:/lower1`:多个只读 lower,用**冒号 `:`** 分隔,**从左到右、从上到下**叠加(`/lower1` 在上、`/lower0` 在下;上面的遮盖下面的)。注意顺序——冒号左边是"更上层",这和直觉有点反,容易踩坑。
- `upperdir=/upper`:可写 upper,容器改动落这里。
- `workdir=/work`:内核做 copy-up 的临时目录,必须和 upper 在**同一个文件系统**上。
- 最后的 `/merged` 是挂载点——进程从这里看到统一视图。

这三个参数(lowerdir/upperdir/workdir),正是 kernel overlayfs 的挂载选项。我们待会儿去内核源码里看它们是怎么被解析的。

> **只读 overlay(没有 upper)**:也可以只挂 `lowerdir`、不给 `upperdir`——这就是一个纯只读的 overlay。CD-ROM、只读镜像视图、k8s 里 init container 用的 image volume 都是这种。只读 overlay 没有 copy-up(反正也没地方写),纯粹是把多层叠成一个视图。

### 为什么"上层遮盖下层"就够用了

merged 视图的核心规则极简:**对同一个路径,如果有多层都有,最上面的那层赢。**

- 读 `/etc/nginx/nginx.conf`:overlayfs 从上往下查(先 upper、再 lower1、再 lower0),找到的第一个就是答案。如果 upper 有,读 upper 的;如果 upper 没有,读 lower1 的;以此类推。
- 列目录 `/etc`:overlayfs 把所有层里 `/etc` 下的条目**合并去重**——同名条目只显示最上面那层的版本,其余被遮盖。

就这一条规则,就足以让多层叠出一个自洽的、进程感觉不到分层的完整文件系统。后续的 copy-up、whiteout,都是在这条规则上打补丁,让"写"和"删"也能自洽。

> **比喻再落一下**:集装箱里的货架是分层托盘叠的。merged 就是"你从上往下看这摞托盘,能看到的全部货物"——上面托盘挡住的下面托盘的货,你看不到(被遮盖);没被挡住的,层层叠叠全露在外面。你伸手去拿"最上面那个 nginx.conf",拿到的永远是**最上层露出来的那一份**。

---

## 三、只读层怎么被多个容器共享

讲了三层结构,现在回答一个关键问题:**凭什么同一份 lower 能被几十个容器共用,还不出乱子?**

答案就一句话:**因为 lower 是只读的。**

### 不这样会怎样:如果 lower 可写

假设 lower 是可写的——容器 A 改了 lower 里的 `/etc/nginx/nginx.conf`,容器 B、C、D 立刻也看到了这个改动(因为它们共用同一份 lower)。这就乱套了:

- 容器 A 是个测试版,把配置改坏了;容器 B、C、D 是生产版,**被 A 的错误改动连累,一起崩**。
- 几十个容器同时写同一个 lower 里的同一个文件,**没有锁、没有隔离**,互相覆盖。

这等于把"容器之间互相隔离"的整个前提给打破了。所以 **lower 必须只读,这是共享的前提**。

### 所以这样设计:lower 永远只读,改动落 upper

overlayfs 从根上把这件事锁死:**lower 目录挂进 overlay 之后,对它的所有写操作都会被重定向——要么失败(纯只读 lower),要么 copy-up 到 upper(带 upper 的 overlay)。** 内核层面,lower 的文件**永远不会被 overlay 的写操作改动**。

这带来一个直接后果:**同一份 lower 可以被 N 个 overlay 挂载同时引用,完全安全。**

- 容器 A、B、C 都用同一个"Ubuntu 基础系统"lower 目录。它们各自挂一个 overlay:`lowerdir=/ubuntu-base, upperdir=/upperA` / `/upperB` / `/upperC`,merged 各自不同。
- 三个容器读 `/usr/sbin/nginx` 时,都从同一份 lower 读——**内核里的页缓存(page cache)也是同一份**,连内存都省了。
- 三个容器写各自的东西时,都写到各自的 upper——**互不污染**。

> **比喻**:那块"基础系统"托盘是**密封的只读托盘**,货放进去了就再也改不了。多个集装箱都把这块托盘摆在自己货架最底下——它们各自能读上面的货(基础系统文件),但谁也改不动它。每个集装箱要改东西?改到自己那块"可写托盘"(upper)上,公共托盘永远原样。**只读 = 可安全共享**,这是整个分层镜像能省空间的命门。

### 一个真实数字:省了多少

具体感受一下共享的威力。假设一台机器跑了 50 个基于 `ubuntu:22.04` 的容器:

- **不分层**:50 × 80MB = 4GB 磁盘 + 50 次完整解压。
- **分层**:1 份 ubuntu base layer(80MB)+ 50 份各自的小 upper(假设每个平均 5MB)= 80 + 250 = 330MB。

**省了将近 13 倍。** 这就是为什么 docker 在一台普通服务器上能轻松跑几百个容器——不是磁盘有多大,是**绝大部分内容大家共用同一份只读层**。

---

## 四、copy-up(写时复制):改文件为什么不污染镜像

讲了共享,现在讲最精妙的那个机制——**容器改文件时,overlayfs 怎么做到"改了不污染镜像"**。

答案叫 **copy-up,写时复制**。它的语义可以一句话说清:

> **容器第一次"写"某个文件时(修改、截断、以写模式打开等),overlayfs 才把这个文件从 lower 复制(copy up)到 upper。复制完之后,这次写操作落到 upper 的副本上——lower 里的原文件被 upper 的副本"遮盖"(因为 upper 在上面),从此容器再读这个文件,读到的就是 upper 的版本。lower 一个字节都没动。**

注意"第一次"——如果文件已经在 upper 里了(之前 copy-up 过),那再写就直接写 upper 的副本,不会再 copy-up 一次。**copy-up 只发生一次,之后就稳定在 upper 上。**

### 不这样会怎样:如果不 copy-up

如果不做 copy-up,容器要改 lower 的文件,只有两种选择,都不行:

1. **直接改 lower**:破坏了"只读层共享"——前面讲过,这会让所有共用这个 lower 的容器都被污染。
2. **拒绝写**:那容器根本没法工作(改不了配置、写不了日志)。

所以必须有一条中间路:**既不碰 lower,又能让容器"觉得"自己改成功了。** 这条路就是 copy-up——把要改的文件**先复制一份到 upper**,然后在 upper 上改。lower 不动,容器也满意。

### copy-up 的精确过程

把 copy-up 的过程拆细,它分几步:

1. **触发**:容器进程对某文件发起一个"会修改"的操作。最典型的是以 `O_WRONLY`/`O_RDWR` 模式 `open()` 一个文件,或 `O_TRUNC`、或 rename/unlink 它。
2. **判定**:overlayfs 检查这个文件当前在不在 upper。如果不在(只在 lower),触发 copy-up。
3. **复制**:在 workdir 里建一个临时文件,把 lower 的文件**数据**拷过来(用 `splice` 逐块拷,内核里是 1MB 一块);再把 lower 的**元数据**(属主、权限、时间戳、xattr)也拷过来。
4. **原子 rename**:把 workdir 里的临时文件 `rename` 到 upper 对应位置——**这一步是原子的**,要么成功(upper 有了副本),要么失败(upper 没动),不会出现"复制一半"的中间态。
5. **遮盖**:从此,这个路径在 upper 有了文件。由于"上层遮盖下层"规则,容器再读这个路径,读到的是 upper 的副本;lower 的原文件被遮住,**但它还在那儿,毫发无损**。
6. **完成原操作**:copy-up 完成后,overlayfs 才把容器最初发起的那个写操作(比如 write)重定向到 upper 的副本上,真正执行。

> **关键洞察:copy-up 是"惰性"的——只在第一次写时发生,而且只复制那一个文件,不是整个层。** 容器启动时,overlayfs **不会**把 lower 全部复制到 upper(那就和不分层一样了)。它复制的是"被实际改动的文件",绝大多数文件一辈子都不会被 copy-up——它们始终待在 lower 里,被容器只读地共享着。**这就是 overlayfs 省 space 的精髓:不改不复制,改一个复制一个。**

### copy-up 的代价:第一次写会慢

copy-up 不是免费的。一个文件第一次被写时,要把它**整个**从 lower 拷到 upper——如果是个 2GB 的数据库文件,容器第一次写它一个字节,就得先拷 2GB。这是 overlayfs 一个有名的性能坑。

工程上怎么缓解:

- **不 copy-up 的特殊情况**:对某些"特殊文件"(设备文件、FIFO、套接字),overlayfs 默认不做 copy-up,直接透传。内核里有一段判定逻辑(下面看源码时会见到 `special_file` 检查)。
- **业务侧**:如果知道某个大文件要被频繁写,要么把它放在 upper(挂个 volume 直接写可写层之外),要么用专门的存储卷(第 21 章 Volume 会讲)。

但绝大多数容器的真实负载是:**99% 的文件一辈子不被写,只读**。对这种负载,copy-up 的总开销趋近于零——这正是 overlayfs 适合容器的原因。

---

## 五、删除怎么实现:whiteout,不是真删

还有最后一种"改动"要讲:**删除文件**。容器 `rm /etc/hostname`,会发生什么?

直觉上你会想:把文件删了呗。但等等——这个文件如果在 lower 里(镜像的一部分),lower 是**只读**的,删不了。那容器怎么"觉得"它删掉了?

overlayfs 的办法叫 **whiteout(白板/白出)**:

> **删除一个文件,overlayfs 不去碰 lower 里的原文件,而是在 upper 里放一个"白板"标记,告诉 merged 视图:"这个路径被遮掉了,当作不存在。"**

具体实现上,这个"白板"在传统的 trusted xattr 模式下,是 upper 里一个**主次设备号都是 0 的字符设备文件**(char device 0/0);在较新的 user xattr 模式下,则是带特定 xattr 的隐藏条目。无论哪种实现,**merged 视图看到白板,就把这个路径从视图里抹掉**——容器 `ls /etc` 时,hostname 不见了;`cat /etc/hostname` 报 `No such file`。

> **比喻**:集装箱的货架是叠的。你嫌最底层托盘上某件货碍眼,想"扔掉"它。但那块托盘是密封的只读托盘,你拿不出来。怎么办?**在你自己那块可写托盘(upper)的对应位置,贴一张"此处无货"的白纸(whiteout)。** 你从上往下看货架时,看到白纸,就知道"哦,这件货被当作不存在了"——底层托盘上的货其实还在,只是被白纸遮住了。别的集装箱没有这张白纸,它们的货架上,这件货照旧好好地摆着。

对**目录**的删除有个特殊情况:如果容器要删一整个目录(比如 `rm -rf /var/log`),upper 里不可能给目录下每个文件都放一张白板(太多)。overlayfs 用一个叫 **opaque(不透明)** 的标记:在 upper 的对应目录上打一个 xattr(`trusted.overlay.opaque=y`),告诉 merged 视图:**"这个目录是'不透明'的,请忽略它下面所有 lower 层的内容"**。于是 `ls /var/log` 就只能看到 upper 里(如果有的话)的内容,lower 的全被屏蔽。

### copy-up + whiteout 合起来:镜像永远不被污染

把 copy-up 和 whiteout 合起来看,你就彻底理解了"容器怎么改都污染不了镜像":

- **修改/新建** → copy-up 到 upper,lower 原文件被遮盖。
- **删除** → upper 放 whiteout,lower 原文件被抹掉视图。

两种操作的共同点:**lower(镜像)自始至终一个字节都没动。** 镜像永远干干净净,可以被无数个容器反复共用。容器的所有改动,全在它自己那块 upper 里——容器一删,upper 一清,世界恢复如初。

> 这就是为什么 `docker rm` 一个容器后,你可以立刻用同一个镜像再起一个全新的、干净的容器——因为镜像(lower)从来没被动过,容器的所有"脏东西"都在 upper 里,跟着容器一起被删了。**镜像 = 只读层,容器实例 = 镜像 + 一块可写 upper**,这个二分是整个容器模型的核心。

---

## 六、这套机制谁搭的:containerd 拼 mount,runc 挂上去

讲完原理,我们去看代码里这套机制怎么落地。先回答一个读者可能已经有的疑问:**overlay 这么个挂载,到底是哪个程序搭起来的?**

答案是分工:**containerd(高层运行时)负责拼 mount 参数,runc(底层运行时)负责执行 mount。**

- **containerd** 有一个叫 **snapshotter(快照器)** 的子系统,专门管"镜像的每一层在磁盘上怎么存、怎么叠"。镜像拉下来后,每一层被解压成磁盘上的一个目录(`<snapshot-root>/snapshots/<id>/fs`);containerd 的 overlay snapshotter 负责,**给定一个容器的层链(从底层基础系统到最上层应用),把 lowerdir/upperdir/workdir 这三个参数拼成一个 mount 配置**。
- **runc** 拿到这个 mount 配置(它在 runtime-spec 里就是一个 `{Type: "overlay", Source: "overlay", Options: ["lowerdir=...", "upperdir=...", "workdir=..."]}` 的结构),当作**一个普通 mount** 执行——它并不特殊对待 overlay,只是把 `mount(2)` 系统调用发出去,内核的 overlayfs 驱动接管。

这个分工很关键,因为它揭示了:**overlay 的"分层"逻辑(containerd 管)和"叠层"机制(内核 overlayfs 管)是分开的**。containerd 决定"哪几层叠、什么顺序";overlayfs 决定"叠好之后怎么呈现、怎么 copy-up"。两者通过一个标准的 mount 配置衔接。

### containerd 拼 mount:`mounts()` 方法

看 containerd 的 overlay snapshotter 怎么拼 mount 参数。这段代码在 [plugins/snapshots/overlay/overlay.go](https://github.com/containerd/containerd/blob/master/plugins/snapshots/overlay/overlay.go#L413-L465) 的 `mounts()` 方法里:

```go
func (o *snapshotter) mounts(s storage.Snapshot, info snapshots.Info) []mount.Mount {
	var options []string
	// ... uidmap/gidmap 处理(无关,省略)...

	if len(s.ParentIDs) == 0 {
		// 没有任何父层 → 不能用 overlay(overlay 至少要一个 lower),
		// 退化成一个 bind mount。
		roFlag := "rw"
		if s.Kind == snapshots.KindView {
			roFlag = "ro"
		}
		return []mount.Mount{
			{
				Source: o.upperPath(s.ID),
				Type:   "bind",
				Options: append(options, roFlag, "rbind"),
			},
		}
	}

	if s.Kind == snapshots.KindActive {
		// 可写快照:加上 workdir 和 upperdir
		options = append(options,
			fmt.Sprintf("workdir=%s", o.workPath(s.ID)),
			fmt.Sprintf("upperdir=%s", o.upperPath(s.ID)),
		)
	} else if len(s.ParentIDs) == 1 {
		// 只读视图且只有一个父层 → 退化成 bind mount(没必要 overlay)
		return []mount.Mount{
			{
				Source: o.upperPath(s.ParentIDs[0]),
				Type:   "bind",
				Options: append(options, "ro", "rbind"),
			},
		}
	}

	// 把所有父层拼成 lowerdir,用冒号分隔(从上到下,从左到右)
	parentPaths := make([]string, len(s.ParentIDs))
	for i := range s.ParentIDs {
		parentPaths[i] = o.upperPath(s.ParentIDs[i])
	}
	options = append(options, fmt.Sprintf("lowerdir=%s", strings.Join(parentPaths, ":")))
	options = append(options, o.options...)

	return []mount.Mount{
		{
			Type:    "overlay",
			Source:  "overlay",
			Options: options,
		},
	}
}
```

读这段,几个细节值得品:

- **`s.ParentIDs` 就是层链**——从底层基础系统到最上层应用,每个父层在磁盘上是个目录(`<root>/snapshots/<id>/fs`)。containerd 把它们用 `o.upperPath()` 拼成绝对路径。
- **`strings.Join(parentPaths, ":")`**——这正好对应内核 overlayfs 的"lowerdir 用冒号分隔"语法。`parentPaths[0]` 是最上层(冒号左边),`parentPaths[N-1]` 是最底层。
- **没有父层就退化成 bind**——overlayfs 至少需要一个 lower(否则没法叠),所以单层时 containerd 直接用 bind mount,不走 overlay。这是个务实的退化。
- **只读视图 + 单父层也退化成 bind**——这种场景没必要 overlay(没 copy-up 需求),bind 更轻。

containerd 拼好这个 mount 配置后,通过 OCI runtime-spec 传给 runc。runc 接到的是一组 `Mount` 结构,其中一个的 `Type` 是 `"overlay"`,这就是容器的根(rootfs)。

### runc 挂上去:当作普通 mount

runc 那边呢?它**根本不认识 overlay 的特殊性**。在 runc 的 `mountToRootfs` 函数([libcontainer/rootfs_linux.go](../runc/libcontainer/rootfs_linux.go#L610-L660))里,有一个 `switch m.Device` 的分支,处理了 `proc`、`sysfs`、`tmpfs`、`bind`、`mqueue`、cgroup——但**没有 `overlay` 的专门分支**。overlay 走 default 路径,落到 `m.mountPropagate()`:

```go
// runc/libcontainer/rootfs_linux.go L1480-L1525
func (m *mountEntry) mountPropagate(rootFd *os.File, mountLabel string) error {
	var (
		data  = label.FormatMountLabel(m.Data, mountLabel)
		flags = m.Flags
	)
	// ... tmpfs/dev 的只读延迟处理(无关) ...

	if err := utils.WithProcfdFile(m.dstFile, func(dstFd string) error {
		return mountViaFds(m.Source, m.srcFile, m.Destination, dstFd, m.Device, uintptr(flags), data)
	}); err != nil {
		return err
	}
	// ... 后续 reopen、propagation flags 处理 ...
}
```

而 `mountViaFds`([libcontainer/mount_linux.go](../runc/libcontainer/mount_linux.go#L158-L209))最终落到一个朴素的 `unix.Mount`:

```go
// runc/libcontainer/mount_linux.go L192-L195
} else {
	op = "mount"
	err = unix.Mount(src, dst, fstype, flags, data)
}
```

注意这里的参数:`fstype` 就是容器配置里给的 `"overlay"`,`data` 就是 containerd 拼好的 `lowerdir=...,upperdir=...,workdir=...` 那一串。**runc 把这一坨原样传给内核的 `mount(2)` 系统调用,内核的 overlayfs 驱动接管。**

> 这个分工揭示了一个重要事实:**runc 自己不解析 `lowerdir`/`upperdir`/`workdir`,它甚至不知道这是 overlay**。它只是个"老老实实执行 mount"的起重机。**"分哪几层、怎么叠"是 containerd 的决策;"叠好之后怎么呈现"是内核 overlayfs 的事**。runc 在中间是个透明管道。这也是为什么 runc 这么小、这么稳定——它不掺和上层业务逻辑,只做最底层的系统调用封装。

---

## 关键源码精读:内核 overlayfs 的 copy-up 与打开重定向

把 containerd 和 runc 都看过一遍后,最后去内核里看 overlayfs 自己——它是怎么实现"叠层视图"和"copy-up"的。这一节是本章的源码高潮,代码全是 torvalds/linux 的 `fs/overlayfs/` 里真实摘出来的。

### 挂载参数:lowerdir/upperdir/workdir 怎么解析

先看挂载时那三个参数怎么被内核解析。overlayfs 的挂载参数定义在一个叫 `ovl_parameter_spec` 的表里,在 [fs/overlayfs/params.c](https://github.com/torvalds/linux/blob/master/fs/overlayfs/params.c):

```c
const struct fs_parameter_spec ovl_parameter_spec[] = {
	fsparam_string_empty("lowerdir",    Opt_lowerdir),
	fsparam_file_or_string("lowerdir+", Opt_lowerdir_add),
	fsparam_file_or_string("datadir+",  Opt_datadir_add),
	fsparam_file_or_string("upperdir",  Opt_upperdir),
	fsparam_file_or_string("workdir",   Opt_workdir),
	fsparam_flag("default_permissions", Opt_default_permissions),
	fsparam_enum("redirect_dir",        Opt_redirect_dir, ovl_parameter_redirect_dir),
	// ... index, uuid, nfs_export, xino, metacopy, verity, fsync, volatile ...
	{}
};
```

这就是 overlayfs 的"挂载参数说明书":`lowerdir`、`upperdir`、`workdir` 三个核心参数,以及一堆高级选项(redirect_dir、index、metacopy 等是为了一些高级特性,容器场景大多用不到)。

多个 lowerdir 用冒号分隔的语法,在 [fs/overlayfs/params.c](https://github.com/torvalds/linux/blob/master/fs/overlayfs/params.c) 的 `ovl_parse_param_split_lowerdirs` 函数里实现——它按 `:` 把 lowerdir 字符串切成多个路径,统计层数。注意一个细节:overlayfs 还支持"双冒号 `::`"来表示 data-only layer(只提供数据、不参与目录合并,用于某些场景),这是较新的特性。

挂载完成后,内核会调 `ovl_fill_super`(在 [fs/overlayfs/super.c](https://github.com/torvalds/linux/blob/master/fs/overlayfs/super.c))把这些参数变成一个真实的 superblock——一个 overlayfs 实例的"身份证"。从此 merged 视图的所有访问,都通过这个 superblock 路由。

### 打开文件时的重定向:ovl_open 第一步就 copy-up

现在看最精彩的部分——**容器进程打开一个文件时,overlayfs 是怎么"现算"出真实文件、并在需要时触发 copy-up 的**。

文件操作的入口在 [fs/overlayfs/file.c](https://github.com/torvalds/linux/blob/master/fs/overlayfs/file.c) 的 `ovl_open`。这个函数是 overlayfs 文件操作表 `ovl_file_operations` 的 `.open` 回调——**容器进程每次 `open()` 一个文件,最终都走到这里**。看它的开头:

```c
// fs/overlayfs/file.c
static int ovl_open(struct inode *inode, struct file *file)
{
	struct dentry *dentry = file_dentry(file);
	struct file *realfile;
	struct path realpath;
	struct ovl_file *of;
	int err;

	/* lazy lookup and verify of lowerdata */
	err = ovl_verify_lowerdata(dentry);
	if (err)
		return err;

	err = ovl_maybe_copy_up(dentry, file->f_flags);    // ← 关键!按需 copy-up
	if (err)
		return err;

	/* No longer need these flags, so don't pass them on to underlying fs */
	file->f_flags &= ~(O_CREAT | O_EXCL | O_NOCTTY | O_TRUNC);

	ovl_path_realdata(dentry, &realpath);              // ← 解析出真实路径(copy-up 后在 upper)
	if (!realpath.dentry)
		return -EIO;

	realfile = ovl_open_realfile(file, &realpath);     // ← 打开底层真实文件
	if (IS_ERR(realfile))
		return PTR_ERR(realfile);

	of = ovl_file_alloc(realfile);
	if (!of) {
		fput(realfile);
		return -ENOMEM;
	}

	file->private_data = of;
	return 0;
}
```

读这个函数,注意三件事:

1. **`ovl_maybe_copy_up(dentry, file->f_flags)`**:这是 copy-up 的触发点。容器一 `open()` 文件,overlayfs 第一件事就是判断"要不要 copy-up"——判断的依据是 `file->f_flags`(打开标志)。如果打开模式是写(`O_WRONLY`/`O_RDWR`)、或带 `O_TRUNC`(截断)、或要新建(`O_CREAT`),且文件当前只在 lower,就触发 copy-up。**这就是"打开时按需 copy-up"的代码所在。**

2. **`ovl_path_realdata(dentry, &realpath)`**:copy-up 完成后(或者本来就在 upper),这个函数解析出文件的"真实路径"——也就是文件**实际躺在哪一层**。如果 copy-up 过了,真实路径在 upper;没 copy-up,真实路径在 lower。

3. **`ovl_open_realfile(file, &realpath)`**:overlayfs 自己**不直接读文件内容**。它打开的是**底层真实文件**(upper 或 lower 里的那个真家伙),然后把这次打开的 `struct file *` 存进 `file->private_data`。后续所有的 `read`/`write`,overlayfs 都是把请求**转发给这个真实文件**。

这就是 overlayfs 的核心戏法——**它不存储数据,它只做"重定向"**:把容器对 merged 视图的每一次操作,重定向到正确的底层真实文件上。

### 读写的重定向:ovl_read_iter / ovl_write_iter

看看读和写是怎么重定向的。读操作在 [fs/overlayfs/file.c](https://github.com/torvalds/linux/blob/master/fs/overlayfs/file.c) 的 `ovl_read_iter`:

```c
static ssize_t ovl_read_iter(struct kiocb *iocb, struct iov_iter *iter)
{
	struct file *file = iocb->ki_filp;
	struct file *realfile;
	struct backing_file_ctx ctx = {
		.cred = ovl_creds(file_inode(file)->i_sb),
		.accessed = ovl_file_accessed,
	};

	if (!iov_iter_count(iter))
		return 0;

	realfile = ovl_real_file(file);              // ← 拿到对应的真实文件
	if (IS_ERR(realfile))
		return PTR_ERR(realfile);

	return backing_file_read_iter(realfile, iter, iocb, iocb->ki_flags, &ctx);  // ← 委托给真实文件读
}
```

**三行核心**:`ovl_real_file(file)` 拿到真实文件,`backing_file_read_iter(...)` 把读请求转发过去。overlayfs 在这里几乎不做事,就是个"代理"。

写操作 `ovl_write_iter` 几乎是镜像的——拿真实文件、转发写请求。**因为 copy-up 已经在 `open()` 时做过了**,所以写的时候,真实文件一定在 upper(可写),写下去没问题。

> **设计精髓**:overlayfs 的文件操作表(`ovl_file_operations`)里,`.open`/`.read_iter`/`.write_iter`/`.fsync`/`.mmap` 等等,**全是"重定向"的实现**——拿到真实文件,委托底层文件系统干活。overlayfs 自己**只管"哪一层赢、何时 copy-up"**,数据读写完全透传。这就是为什么 overlayfs 能那么薄——它不需要重新实现磁盘 IO,它只是 VFS 层的一个"路由器"。

### copy-up 的判定:special_file 不 copy-up

回到 copy-up 本身,看判定逻辑。在 [fs/overlayfs/copy_up.c](https://github.com/torvalds/linux/blob/master/fs/overlayfs/copy_up.c),入口是 `ovl_maybe_copy_up`,它先调 `ovl_open_need_copy_up` 判断要不要 copy-up:

```c
// fs/overlayfs/copy_up.c
static bool ovl_open_need_copy_up(struct dentry *dentry, int flags)
{
	/* Copy up of disconnected dentry does not set upper alias */
	if (ovl_already_copied_up(dentry, flags))     // ← 已经 copy-up 过了,不用
		return false;

	if (special_file(d_inode(dentry)->i_mode))   // ← 特殊文件(设备/FIFO/套接字)不 copy-up
		return false;

	if (!ovl_open_flags_need_copy_up(flags))     // ← 打开标志不需要 copy-up(纯读)的不 copy-up
		return false;

	return true;
}

int ovl_maybe_copy_up(struct dentry *dentry, int flags)
{
	if (!ovl_open_need_copy_up(dentry, flags))
		return 0;

	return ovl_copy_up_flags(dentry, flags);
}
```

三个判定条件,每一条都对应一个设计决策:

- **`ovl_already_copied_up`**:已经在 upper 了,不用再 copy-up。这就是"copy-up 只发生一次"的保证。
- **`special_file`**:**特殊文件不 copy-up**。这是为什么 `/dev/null`、`/proc` 里的文件这些不会触发 copy-up——它们不是普通文件,没有"数据"可拷,overlayfs 直接透传。这个判定避开了无数奇怪的边界情况。
- **`ovl_open_flags_need_copy_up`**:**纯读打开不 copy-up**。只有带写意图的打开(写、读写、截断、新建)才需要。这保证了"只读访问完全零开销"——你 `cat` 一个文件,overlayfs 连一个字节都不复制,直接从 lower 读。

### copy-up 的执行:逐块拷数据 + 拷元数据 + 原子 rename

真正干 copy-up 活的,是 `ovl_copy_up_flags` → `ovl_copy_up_one` → `ovl_do_copy_up` 这条调用链。我们看其中两个关键环节。

**拷数据**——`ovl_copy_up_data` 调 `ovl_copy_up_file`,在 [fs/overlayfs/copy_up.c](https://github.com/torvalds/linux/blob/master/fs/overlayfs/copy_up.c):

```c
// fs/overlayfs/copy_up.c (ovl_copy_up_file 节选)
#define OVL_COPY_UP_CHUNK_SIZE (1 << 20)   // ← 1MB 一块

// ...
while (len) {
	size_t this_len = OVL_COPY_UP_CHUNK_SIZE;
	ssize_t bytes;

	if (len < this_len)
		this_len = len;

	if (signal_pending_state(TASK_KILLABLE, current)) {
		error = -EINTR;
		break;
	}

	// ... hole 跳过优化 ...

	error = ovl_verify_area(old_pos, new_pos, this_len, len);
	if (error)
		break;

	bytes = do_splice_direct(old_file, &old_pos,        // ← 从 lower 读一块
				 new_file, &new_pos,            // ← 写到 upper(临时文件)
				 this_len, SPLICE_F_MOVE);      // ← splice 零拷贝(能 splice 就不进用户态)
	if (bytes <= 0) {
		error = bytes;
		break;
	}
	WARN_ON(old_pos != new_pos);

	len -= bytes;
}
```

几个细节:

- **`OVL_COPY_UP_CHUNK_SIZE = 1 << 20`**:1MB 一块。不是一次性读完整个文件,是分块——这样**大文件 copy-up 也能被信号打断**(每块之间检查 `signal_pending`),避免一个 10GB 文件 copy-up 时进程卡死无法 kill。
- **`do_splice_direct`**:用 `splice` 系统调用在内核态直接搬运数据(从一个文件的 page cache 到另一个),**不经过用户态缓冲区**。如果底层文件系统支持,还会先尝试 `vfs_clone_file_range`(文件系统级的 reflink/clone,真正零拷贝),失败再退回 splice。**这是 copy-up 性能优化的关键**——能 splice 不 read/write,能 clone 不 splice。
- **hole 跳过**:lower 文件里的"洞"(sparse file 的空洞)不拷贝,直接 seek 跳过。省时间和空间。

**拷元数据 + 原子 rename**——`ovl_copy_up_metadata` 把 lower 的 xattr、属主、权限、时间戳都拷到 upper 的临时文件;然后 `ovl_copy_up_workdir` 或 `ovl_copy_up_tmpfile` 在 workdir 里建临时文件,把数据和元数据都准备好,最后用一次 **`ovl_do_rename_rd`(原子 rename)** 把临时文件挪到 upper 的最终位置:

```c
// fs/overlayfs/copy_up.c (ovl_copy_up_workdir 节选)
err = ovl_copy_up_metadata(c, temp);
if (!err)
	err = ovl_do_rename_rd(&rd);   // ← 原子 rename:workdir/临时文件 → upper/最终位置
end_renaming(&rd);
```

**这一步 rename 是原子的**——内核里 `rename` 系统调用对同一文件系统内的操作是原子的(要么成功,要么文件系统状态不变)。所以 copy-up 的最后一步"把准备好的临时文件挪到 upper"是**全有或全无**的:成功则 upper 多了文件(此后容器看到 upper 版本),失败则 upper 没动(容器下次 open 还会再试一次 copy-up)。**绝不会出现"upper 里有个复制一半的损坏文件"**。

这就是 copy-up 的完整内核实现——惰性触发、按需复制、分块可中断、splice 零拷贝、原子落地。读到这里,"容器改文件为什么不会污染镜像"不再是一句口号,而是 `ovl_open` → `ovl_maybe_copy_up` → `ovl_do_copy_up` → `do_splice_direct` + `ovl_do_rename_rd` 这条具体代码路径的产物。

### whiteout:删除的"遮板"标记

最后补一句 whiteout 的实现位置。overlayfs 里,创建 whiteout 的逻辑在 [fs/overlayfs/dir.c](https://github.com/torvalds/linux/blob/master/fs/overlayfs/dir.c)(目录操作所在的文件)里,涉及 `ovl_cleanup_and_whiteout`、`ovl_create_or_link` 等函数。

简化讲,删除一个文件时,overlayfs 不去碰 lower 的原文件,而是在 upper 里创建一个特殊的"whiteout 条目"(传统模式是主次设备号都是 0 的字符设备文件 `c 0 0`;user xattr 模式是带 `overlay.whiteout` xattr 的隐藏条目)。merged 视图在列目录、查找路径时,如果碰到 whiteout 条目,就**把这个路径从视图里抹掉**——容器看到的效果就是"文件被删了"。

目录的 opaque 标记同理:在 upper 的目录上设 `overlay.opaque=y` xattr,merged 视图看到这个标记,就**忽略 lower 层在这个目录下的所有内容**。于是 `rm -rf /var/log` 后,`ls /var/log` 看不到 lower 的日志文件——它们被 opaque 标记屏蔽了。

(whiteout 的具体函数实现随内核版本变化较大,这里只标文件 [fs/overlayfs/dir.c](https://github.com/torvalds/linux/blob/master/fs/overlayfs/dir.c) 不标具体行号,避免过时。)

---

## 章末小结

### 用航运比喻回顾本章

回到那片港口。第 4 章结束时,我们的集装箱被 `pivot_root` 连根拔起,挪到了自己那块独立的小甲板上。可那块小甲板,**内容哪来的**?这一章回答了它:

那块小甲板**不是一块整板削出来的,是一层一层托盘叠出来的**——

- 最底下那层,是**基础系统托盘**(lower0:Ubuntu/Alpine 的文件),**只读、密封**。
- 往上叠一层**改动托盘**(lower1:装了 nginx),同样只读、密封。
- 再往上,是**本集装箱自己那块可写托盘**(upper)——集装箱运行时要改东西、写日志,全写在这块上。
- 你从上往下看这摞托盘,看到的就是 merged 视图:**上面托盘挡住的下面托盘的货,你看不见;没被挡住的,层层全露在外面**。

这套叠托盘的设计,解决了两个要命的工程问题:

1. **省空间**:几十个集装箱都用同一摞"基础系统 + nginx"的只读托盘,这些公共托盘在仓库里**只放一份**,被所有集装箱共用——因为只读,挂多少次都安全。每个集装箱独占的,只有自己那块可写 upper。
2. **省传输**:从仓库拉一摞托盘时,如果某层托盘本地已经有了(按指纹/hash 比对),**就跳过不拉**——只拉本地没有的那几层。

而"改东西不污染公共托盘"的魔法,叫 **copy-up(写时复制)**:集装箱第一次伸手改某件货时,码头工人先把这件货**从只读托盘复制一份到集装箱自己的可写托盘**,然后才让你改。从此这件货的"最新版"在可写托盘上,只读托盘里的原版**被遮盖**(被上面挡住了),但它**毫发无损**——别的集装箱没有这次复制,它们的货架上,这件货照旧是只读托盘里的原版。

删除叫 **whiteout**:集装箱 `rm` 一件货,码头工人不碰只读托盘(动不了),只在可写托盘的对应位置**贴一张"此处无货"的白纸**。你从上往下看,看到白纸,这件货就从你的视图里消失了——但只读托盘上它还好好的。

### 本章在第 1 篇中的位置

记住全书的二分法:**打包隔离 vs 调度编排。**

这一章,我们补上了第 1 篇"打包隔离"这半边的**第四块、也是最后一块基石**:

- 第 2 章 **namespace**(舱壁)→ 隔离"进程看见的世界"。
- 第 3 章 **cgroup**(配额)→ 隔离"进程能用的资源"。
- 第 4 章 **rootfs + pivot_root**(小甲板)→ 隔离"进程踩在哪个文件系统根上"。
- 第 5 章 **overlayfs**(叠托盘)→ **解决"那块小甲板的内容怎么高效组织、怎么共享、怎么改了不污染"**。

四块基石凑齐,容器**隔离 + 高效打包**这半边就齐了。从内核视角看,这四件事**全是内核早已有的能力**(namespace、cgroup、`pivot_root`/`chroot` 系统调用、overlayfs 文件系统驱动)的组合——再次印证第一性原理:**容器没有发明新东西,它只是把内核早就摆在那儿的零件,按"造一个隔离又高效的世界"的需求拼起来**。

> **顺带一句**:这一章讲的 "layer(层)",就是第 7 章《镜像的本质》的主角。一个镜像,本质就是一组 layer 的有序叠加 + 一份 manifest(层清单)+ 一份 config(启动参数)。本章讲的是 **这些 layer 在运行时怎么被 overlayfs 叠成 rootfs**;第 7 章会反过来讲 **这些 layer 在镜像文件里怎么打包、怎么用 hash 寻址去重、怎么从仓库拉到本地**。两章是镜像这枚硬币的两面——一个管"叠",一个管"包"。

### 五个"为什么"清单

如果你只能从这一章带走五件事:

1. **为什么镜像必须分层**:不分层会导致两个灾难——存储爆炸(几十个容器各拷一整份,99% 重复内容)和传输爆炸(改一行配置要重新传几百 MB)。分层让公共层只存一份、只传一次,这是 docker "轻"的根源之一。
2. **overlayfs 的三层是什么**:多个只读 lower 叠最下(镜像层,公共可共享)、一个可写 upper 在上(本容器独有,改动落这里)、merged 是内核现算的统一视图(上层遮盖下层)。merged 不是真目录,是 overlayfs 拦截每次访问现算出来的。
3. **只读层为什么能被多个容器共享**:因为 lower 只读——overlayfs 从根上保证 lower 永不被写操作改动。同一份 lower 挂多少次都安全,内核页缓存都是同一份。**只读 = 可安全共享**,这是省空间的命门。
4. **copy-up 为什么不污染镜像**:容器第一次写某文件时,overlayfs 才把它从 lower 惰性复制到 upper(只复制被改的文件,不是整层);从此 upper 副本遮盖 lower 原文件,所有写落到 upper,lower 一个字节没动。**copy-up 是惰性的、单次的、只针对被改文件的。**
5. **whiteout 怎么实现删除**:删除不是真删 lower(删不动),而是在 upper 放一个 whiteout 标记(字符设备 0/0 或 xattr),merged 视图看到它就把路径抹掉。目录删除用 opaque xattr 屏蔽整个 lower 子树。**镜像永远不被污染**,这是容器可反复干净重启的根基。

### 想继续深入,该往哪钻

- **亲手玩一下 overlay**:在 Linux 虚拟机上,`mkdir lower upper work merged`,放几个文件到 lower,然后 `mount -t overlay overlay -o lowerdir=lower,upperdir=upper,workdir=work merged`。在 merged 里改个文件,去 upper 里看(文件出现了!),去 lower 里看(原版还在!)。这一组动手实验顶读十篇文章。
- **看内核 overlayfs 的打开重定向**:[torvalds/linux 的 fs/overlayfs/file.c](https://github.com/torvalds/linux/blob/master/fs/overlayfs/file.c) 的 `ovl_open`、`ovl_read_iter`、`ovl_write_iter`。重点体会"overlayfs 不存数据,只做重定向"这个设计——它的文件操作表全是"拿真实文件 → 委托底层"的代理模式。
- **看内核的 copy-up 全过程**:[fs/overlayfs/copy_up.c](https://github.com/torvalds/linux/blob/master/fs/overlayfs/copy_up.c) 的 `ovl_maybe_copy_up` → `ovl_copy_up_flags` → `ovl_copy_up_one` → `ovl_do_copy_up`。重点看 `ovl_copy_up_file` 里 1MB 分块 + splice 零拷贝 + 原子 rename 这套组合拳——它回答了"copy-up 又慢又安全怎么权衡"。
- **看 containerd 的 overlay snapshotter 怎么拼 mount**:[containerd 的 plugins/snapshots/overlay/overlay.go](https://github.com/containerd/containerd/blob/master/plugins/snapshots/overlay/overlay.go) 的 `mounts()` 方法。这是"containerd 决策分层,runc 挂载"分工中,containerd 这一侧的真实代码。
- **理解 layer 在镜像里怎么打包**:这是下一章的事——但如果你想提前看,翻到第 7 章《镜像的本质:layer + manifest + config》,看一组 layer 怎么用 hash 寻址、怎么天然去重。

---

> 第 1 篇的四块基石——namespace(舱壁)、cgroup(配额)、rootfs/pivot_root(小甲板)、overlayfs(叠托盘)——到此全部摆齐。你已经看清了集装箱的全部构造。但还有一件事没做:**亲手把这几块零件装在一起,造一个能跑的集装箱。** 第 1 篇四章一直是分开讲的,你可能还心存怀疑:"这几样真的拼起来就能跑容器吗?" 下一章,我们就把 namespace + cgroup + pivot_root + overlayfs 拼到一起,用几十行 Go **亲手造一个能跑的容器**——把第 1 篇彻底收口。翻开 **第 6 章 · 动手:不用 docker,手搓一个容器**。
