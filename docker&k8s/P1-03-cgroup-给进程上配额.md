# 第 3 章 · cgroup:给进程上配额

> **前置**:你需要先读过 [第 2 章 · namespace:集装箱的铁皮舱壁](P1-02-namespace-集装箱的铁皮舱壁.md)——那一章给集装箱围上了 8 块**看不见别人的铁皮舱壁**(各自的进程表、各自的网卡、各自的主机名)。但那章结尾留了一个钩子:**舱壁只挡住了"视线",没挡住"手"**。一个集装箱照样能把货轮的 CPU 跑满、把内存吃光、把磁盘 IO 占死,把别的集装箱活活挤死。这一章就来补这个洞:怎么给每个集装箱**限量**。

> **核心问题**:光隔离(看不见)还不够——一个容器还能把宿主的 CPU/内存吃光。怎么限量?
>
> 这一章拆第二块基石——**cgroup(control group,控制组)**。namespace 管"看见什么",cgroup 管"用多少"。它们是内核给容器准备的两件外套里,管"配额"的那一件。这一章要回答:cgroup 凭什么能限制一个进程组的资源?为什么它有两代(v1/v2)、为什么要有 v2?cpu / memory / io 这三个最常用的控制器各怎么计量、怎么限额?以及那个最实战的问题——一个容器内存超限被杀的"局部 OOM",底下到底发生了什么?
>
> **读完本章你会明白**:
> - 为什么 namespace **不管资源**(它只动"视图",不动"用量")——这是它和 cgroup 划清的边界,也是为什么容器必须**两件外套都穿**。
> - cgroup v1(每个控制器一棵独立层级树)和 v2(单一统一层级)的差别,以及为什么要有 v2——v1 多棵树导致"同一个进程在不同控制器里归属不一致",难管;v2 把所有控制器挂到同一棵树上,归属统一。
> - cpu / memory / io 三个最常用控制器各怎么计量、怎么限额(`cpu.max`/`cpu.weight`、`memory.max`、`io.max`),以及它们在 runc 源码里**就是往文件里写几个字符串**。
> - 为什么容器有"**局部 OOM**":`memory.max` 超限只杀这个容器(内核 OOM killer 把搜杀范围限定在这个 cgroup 内),不波及宿主——这底下,是内核系列的 OOM 章。
> - cgroup 在文件系统里长什么样(`/sys/fs/cgroup/`),为什么"写一个 PID 到 `cgroup.procs`"就是把进程塞进 cgroup。

> **如果一读觉得太难**:先只记住三件事——① namespace 管"看见什么",cgroup 管"用多少",两者正交、必须同时用;② v2 比 v1 强在"一棵树挂所有控制器",归属统一好管;③ 容器的 OOM 是"局部 OOM"——内核 OOM killer 只在超限的那个 cgroup 内选受害者。这三句话撑起整章。

---

## 章首·从 namespace 的边界说起

第 2 章做完,我们的集装箱已经有了 8 块功能各异的舱壁——容器里的进程看不见别人的进程表、用不了别人的网卡、改不了别人的主机名。读者到这里,很容易觉得"隔离做完了呀,这容器够封闭了"。

不够。远远不够。

> **比喻**:你给集装箱装上了不透光的铁皮舱壁(进程表隔离)。可舱里的货一旦开始干活——搬货的搬货、烧油的烧油——它该用多少 CPU、吃多少内存、占多少货轮的动力,舱壁**一个字都没说**。一个流氓集装箱可以把货轮的全部动力抽干,把别的正经集装箱饿死。**舱壁挡住了"视线",挡不住"胃口"**。

落到技术上,验证起来非常简单。在一个没配 cgroup 的容器里跑这段:

```sh
# fork 炸弹——疯狂制造子进程,把 CPU 跑满
:(){ :|:& };:
# 或者疯狂分配内存,把宿主内存吃光
while true; do dd if=/dev/zero of=/dev/null bs=1G count=100; done
```

**如果没有任何资源限制,这一段代码能把整台宿主机拖死**,同机所有别的容器跟着陪葬。这跟"一个普通进程能干啥"完全一样——因为第 1 章我们已经立死了:**容器就是个普通进程**,namespace 没改变这一点。

namespace 的边界在哪?它只动"**进程看见的视图**",不动"**进程用的资源**"。视图(world view)和用量(usage)是**两个正交的维度**——一个管"看到什么",一个管"用掉多少"。namespace 把第一个维度做满了,可第二个维度一行代码都没写。

补上第二个维度的,就是 **cgroup**。

> **再扣一次航运比喻**:namespace 是集装箱的**铁皮舱壁**(隔视线),cgroup 是集装箱的**载重 / 体积 / 温控配额**(限用量)。一艘货轮上装 100 个集装箱,光有舱壁不够——还得给每个箱定个"最多装多少吨、最多占多大体积、最多制冷多少度",否则一个超载箱能把船压沉。这个"给每个箱上配额"的活儿,在内核里叫 cgroup。

---

## 一、cgroup 的最小心智模型:把进程分组,按组计量、按组限额

理解 cgroup,先忘掉那些复杂的层级树和控制器名词,只记一个最朴素的事实:

> **cgroup 干的事就一句话:把若干进程归为一组(给这个组起个名字,叫一个 cgroup),内核 thereafter 按这个组来统计它用了多少资源、限制它最多能用多少资源。**

就这么朴素。"control group"这个名字本身,就是"控制(资源)的一组进程"。

### 它在文件系统里长什么样

cgroup 在用户态的接口,**全是文件**。它挂载在 `/sys/fs/cgroup/` 这个目录下(现代 Linux 默认就挂好了)。`cd` 进去看一眼(cgroup v2 模式下):

```sh
$ ls /sys/fs/cgroup/
cgroup.controllers      cgroup.stat
cgroup.max.depth        cgroup.subtree_control
cgroup.max.descendants  cgroup.threads
cgroup.procs            cpu.stat
cpu.max                 io.stat
cpu.weight              memory.current
cpu.pressure            memory.events
cpu.stat                memory.max
docker/                 memory.pressure
kubepods/               ...
```

读这个目录,信息量很大,有几个关键点:

1. **`cgroup.procs`**:这个文件里存着"当前这个 cgroup 里所有进程的 PID"。**写一个 PID 进去,就把这个进程塞进了这个 cgroup**。这就是"把进程归组"的操作——往一个文件里写个数字。
2. **`cpu.max` / `memory.max` / `io.max`**:这三个文件就是"配额设定文件"。写一个数字进 `cpu.max`,就限了这个 cgroup 的 CPU;写一个数字进 `memory.max`,就限了内存。**配额 = 往文件里写数字**。
3. **`memory.current` / `cpu.stat` / `io.stat`**:这三个是"用量统计文件",**读**它们就知道这个 cgroup 当前用了多少资源。
4. **子目录**:`docker/`、`kubepods/` 这些子目录,每一个都是一个**子 cgroup**。cgroup 是**树状**的——根下面挂子 cgroup,子 cgroup 下面还可以再挂孙 cgroup。

把这几条合起来,你就理解了 cgroup 的全部用户态模型:**它就是一棵目录树,每个目录是一个 cgroup;`cgroup.procs` 决定哪些进程在这个组里;`cpu.max`/`memory.max`/... 决定这个组能用多少资源。** 不需要任何新 API,**全是文件操作**——这正是 Linux 内核"一切皆文件"哲学在资源管理上的体现。

### 进程怎么"挂"到一个 cgroup 上:一个 PID 写进 `cgroup.procs`

把"进程和 cgroup 的关系"画成图,就是:

```
   进程 (task_struct)
      │  task->cgroups 指针
      ▼
   css_set  ───────────┐
      │                │  (一个 css_set 引用一组 css)
      ▼                ▼
   cgroup (一个目录)   cgroup_subsys_state (css)
   /sys/fs/cgroup/     cpu  memory  io  pids ...
   └── docker/             ▲    ▲     ▲
       └── <container-id>/ │    │     │
           ├── cgroup.procs│    │     │  ← 每个控制器一个 css,
           ├── cpu.max   ──┘    │     │    挂在同一个 cgroup 上
           ├── memory.max ─────┘     │
           └── io.max ───────────────┘
```

读这张图:**一个进程通过 `task->cgroups`(指向 `css_set` 结构)关联到一组 cgroup;每个 cgroup 是文件系统里的一个目录;目录里 `cgroup.procs` 列着这个组里的 PID,`cpu.max` 等文件设置这个组的配额。** 用户态的操作(把进程加进组、设配额)全是读写这些文件。

> 这个"往文件里写 PID/写数字"的接口,**是 cgroup v2 的标志**——所有控制器共用同一棵目录树、同一套文件接口。v1 长得不一样(v1 是每个控制器一棵独立树),下面专门一节讲它们的差别。

---

## 二、为什么会有 v1 和 v2 两代:cgroup 的演化史

cgroup 有个让初学者困惑的事:它有**两代**——v1 和 v2。一台现代 Linux 机器(内核 5.x 以上)默认用 v2,但很多老系统还在用 v1。**runc / docker / k8s 都得同时支持两套**。为什么会有两代?为什么内核要重做一遍?

答案藏在 cgroup 的演化史里,而这演化史,本身就是"工程演进"的一个绝佳样本。

### v1:每个控制器一棵独立层级树

cgroup v1 是 2007 年进内核的(那会儿容器还叫 LXC,还没 Docker)。它的设计是:**每种资源一个"控制器(controller)"也叫"子系统(subsystem)",每个控制器有自己一棵独立的层级树**。

比如 memory 控制器有一棵树,cpu 控制器有另一棵树,blkio(块设备 IO)控制器又有第三棵树。这些树互相独立、可以长得完全不一样:

```
   v1 的世界(每个控制器一棵树,树与树彼此独立):

   memory 树             cpu 树                blkio 树
   /sys/fs/cgroup/       /sys/fs/cgroup/       /sys/fs/cgroup/
   └── memory/           └── cpu/              └── blkio/
       ├── webapp/           ├── webapp/           ├── webapp/
       └── db/               └── batch/           └── db/
```

每棵树独立挂载,一个进程可以**在不同的控制器树里挂在不同位置**——比如 `webapp` 进程在 memory 树的 `webapp/` 子树里,在 cpu 树的 `batch/` 子树里(因为它的 CPU 调度按 batch 分组),在 blkio 树的另一个位置。

### 不这样会怎样:v1 的三个痛

"每个控制器一棵独立树"听起来挺灵活,实践起来简直是灾难。三个要命的痛:

**痛点一:进程归属不一致。** 同一个进程在 memory 树和 cpu 树里挂在不同位置——那它到底属于"webapp 组"还是"batch 组"?运维想"看 webapp 这个组用了多少资源",得**跨好几棵树去查**。一个容器明明是一个逻辑实体,在 cgroup 里却**散在好几棵树上**,定位、统计、限额都麻烦。

**痛点二:控制器组合受限。** v1 规定:**两个控制器,如果挂到同一棵树上,就不能再挂到别的树上**(一个控制器只能属于一棵树)。可你常常想"用一棵树同时管 memory + cpu + io"(因为一个容器的这三样是绑在一起的)。结果就是你要么把所有控制器挤进一棵树(丧失按控制器独立分组的灵活性),要么把它们分散到多棵树(然后陷入痛点一)。**两难**。

**痛点三:接口不一致、坑多。** v1 各控制器的文件名、单位、语义各干各的——cpu 用 `cpu.cfs_quota_us`/`cpu.cfs_period_us`(微秒),memory 用 `memory.limit_in_bytes`(字节),blkio 用 `blkio.throttle.read_bps_device`(还要带设备号)。每来一个新控制器,都新发明一套字段名,没有统一约定。运维记不住、工具实现起来又臭又长。

### 所以这样设计:v2——一棵统一层级树

2016 年(内核 4.5),cgroup v2 进了内核,核心改动一句话:

> **所有控制器,挂到同一棵层级树上。** 一个 cgroup(目录)同时承载 cpu / memory / io / pids 所有控制器的配额——一个进程在一棵树上只有一个位置,**归属唯一**。

```
   v2 的世界(单一统一层级树,所有控制器都在同一棵树上):

   /sys/fs/cgroup/   ← 唯一的根(挂载点)
   ├── cgroup.procs
   ├── cgroup.subtree_control   ← 在这里声明"这个子树启用哪些控制器"
   ├── cpu.max       ← 同一个目录,同时承载
   ├── memory.max    ←   cpu、memory、io、pids...
   ├── io.max        ←   的所有配额
   ├── webapp/
   │   ├── cgroup.procs    ← webapp 组的进程 PID
   │   ├── cpu.max
   │   ├── memory.max
   │   └── io.max
   └── db/
       └── ...
```

这棵树里,一个 cgroup(一个目录)**同时挂载着所有控制器的配额文件**。`webapp` 这个组,它的 CPU、内存、IO 配额全在 `webapp/` 这一个目录里——**归属唯一、一眼看清、管理方便**。痛点一(归属不一致)直接消失。

**控制器怎么启用?** v2 引入了 `cgroup.subtree_control`:在父目录写一行,声明"我下面的子目录启用哪些控制器":

```sh
# 在根 cgroup 上声明:子树启用 cpu、memory、io 控制器
$ echo "+cpu +memory +io" > /sys/fs/cgroup/cgroup.subtree_control
```

这个设计的关键约束是:**子节点能用的控制器,必须由父节点授权**(写到父节点的 `cgroup.subtree_control` 里)。所以 v2 是"自顶向下授权控制器"的模式,而 v1 是"每个控制器自顾自挂一棵树"。**v2 用层级授权换来了归属统一**。

### v2 的代价(为什么没立刻替代 v1)

v2 不是白来的,它有代价:

- **某些控制器在 v2 里来得很晚**。比如 devices 控制器(控制容器能访问哪些设备)直到 4.15 才进 v2,cpuset 直到 5.0,hugepage 直到 5.6。在过渡期(2016~2020),v2 缺控制器,系统还得"v1 + v2 混用"(所谓 hybrid 模式)。
- **老工具不认 v2**。Docker 早期只支持 v1,k8s 也晚一拍(1.x 之后才默认 v2)。所以到现在(2024+),很多生产集群还在 v1 模式。

但**趋势是 v2 一统天下**——内核主线不再给 v1 加新功能,v2 是默认推荐。**runc 现在两套都完整支持**,启动时根据宿主是 v1 还是 v2 自动选。

### runc 怎么选 v1 还是 v2:一行 `IsCgroup2UnifiedMode()`

runc 启动容器时,**第一步**就是判断宿主跑在 cgroup v1 还是 v2。看它的 cgroup manager 工厂函数([runc/vendor/github.com/opencontainers/cgroups/manager/new.go](../runc/vendor/github.com/opencontainers/cgroups/manager/new.go#L29-L55)):

```go
// NewWithPaths is similar to New, and can be used in case cgroup paths
// are already well known, which can save some resources.
//
// For cgroup v1, the keys are controller/subsystem name, and the values
// are absolute filesystem paths to the appropriate cgroups.
//
// For cgroup v2, the only key allowed is "" (empty string), and the value
// is the unified cgroup path.
func NewWithPaths(config *cgroups.Cgroup, paths map[string]string) (cgroups.Manager, error) {
    // ... 省略配置校验 ...

    // Cgroup v2 aka unified hierarchy.
    if cgroups.IsCgroup2UnifiedMode() {
        path, err := getUnifiedPath(paths)
        if err != nil {
            return nil, fmt.Errorf("manager.NewWithPaths: inconsistent paths: %w", err)
        }
        if config.Systemd {
            return systemd.NewUnifiedManager(config, path)
        }
        return fs2.NewManager(config, path)        // ← v2 走 fs2
    }

    // Cgroup v1.
    if config.Systemd {
        return systemd.NewLegacyManager(config, paths)
    }

    return fs.NewManager(config, paths)            // ← v1 走 fs
}
```

读这个函数,runc 的"v1/v2 适配策略"清清楚楚:

1. **`cgroups.IsCgroup2UnifiedMode()`** 是个判别函数(它内部检测 `/sys/fs/cgroup/cgroup.controllers` 是否存在等特征)。**一行判断,二选一**。
2. **v2 走 `fs2.NewManager`**,路径是"单一统一路径"(注释明说 "the only key allowed is `""` (empty string), and the value is the unified cgroup path")。这正是 v2"一棵树"在代码里的体现——只有一条路径。
3. **v1 走 `fs.NewManager`**,路径是"per-subsystem 路径 map"(注释:"the keys are controller/subsystem name, and the values are absolute filesystem paths")。这正是 v1"每个控制器一棵树"的体现——一个 map 存多条路径。
4. 两边都还支持 systemd 模式(让 systemd 帮你管 cgroup,这是 k8s 默认配置之一)。

**这个分支点,就是 v1/v2 差别的源码级总开关。** 同一份 runc 代码,在 v2 宿主上走 `fs2` 那一套,在 v1 宿主上走 `fs` 那一套——但对外接口(`Manager` 抽象)完全一样,所以上层(containerd、docker、k8s)不用关心底下是哪代。

> v1 在 runc 里的实现,对应代码里那 15 个子系统,见 [fs/fs.go](../runc/vendor/github.com/opencontainers/cgroups/fs/fs.go#L17-L33) 的 `subsystems` 列表——cpuset、devices、memory、cpu、cpuacct、pids、blkio、hugetlb、net_cls、net_prio、perf_event、freezer、rdma……每行就是一个独立控制器。v1 的"多控制器"复杂性,在这个 15 行列表里就一览无余。

---

## 三、三个最常用控制器:cpu / memory / io 各怎么计量、怎么限额

讲清 v1/v2 之后,我们来拆三个最常用的控制器——**cpu**(限 CPU)、**memory**(限内存)、**io**(限磁盘 IO)。它们三个加起来,覆盖了 95% 的容器资源限制需求。

我们以 **cgroup v2** 为主讲(v2 是趋势、接口干净),v1 的差别在最后点出。

### 1. cpu 控制器:用 `cpu.max` 限上限,用 `cpu.weight` 分权重

cpu 控制器干两件事:**给一个 cgroup 设 CPU 用量上限**(硬限制),以及**在多个 cgroup 之间按权重分配空闲 CPU**(软分配)。

**`cpu.max`:设上限。** 格式是两个数字:`$QUOTA $PERIOD`。语义:**每 `PERIOD` 微秒的时间里,这个 cgroup 里的进程最多能跑 `QUOTA` 微秒的 CPU**。

```
# 例 1:限制这个容器最多用 1 个 CPU 的算力
$ echo "100000 100000" > /sys/fs/cgroup/.../cpu.max
#                ^周期 100ms(100000 微秒),配额也是 100ms
#                → 100ms 周期内能用 100ms CPU = 1 个核

# 例 2:限制这个容器最多用 2.5 个 CPU
$ echo "250000 100000" > /sys/fs/cgroup/.../cpu.max
#                ^配额 250ms,周期 100ms → 2.5 个核

# 例 3:不限制(无限)
$ echo "max 100000" > /sys/fs/cgroup/.../cpu.max
```

这是"硬限制"——配额用完,内核就把这个 cgroup 的进程**冻结到下个周期**,这就是容器的 CPU 限流(throttling)。`cpu.stat` 里的 `nr_throttled`、`throttled_usec` 就是统计被限流了多少次、累计多久。

**`cpu.weight`:分权重。** 范围 1~10000,默认 100。语义:**当多个 cgroup 抢同一个 CPU 时,按 weight 比例分**。比如 A 的 weight 是 100、B 是 200,那空闲 CPU 时 A 拿 1/3、B 拿 2/3。**它不是上限**(weight 高的也能用满 CPU,只要没别的 cgroup 抢),只是"谁更重要"的相对权重。

**这两套配合**:`cpu.max` 防"流氓容器吃光 CPU",`cpu.weight` 让"重要容器抢到更多 CPU"。docker / k8s 里 `--cpus=2.5`(设上限)对应 `cpu.max`,`--cpu-shares`(设权重)对应 `cpu.weight`。

### 2. memory 控制器:用 `memory.max` 设硬上限

memory 控制器核心就一个文件:**`memory.max`**——这个 cgroup 最多能用多少字节内存(含进程的匿名内存、page cache 等)。

```
# 限制这个容器最多用 512MB 内存
$ echo "536870912" > /sys/fs/cgroup/.../memory.max      # 512 * 1024 * 1024

# 不限制
$ echo "max" > /sys/fs/cgroup/.../memory.max

# 看当前用量
$ cat /sys/fs/cgroup/.../memory.current
421238784    # 当前用了约 402MB

# 看历史峰值
$ cat /sys/fs/cgroup/.../memory.peak
```

`memory.current` 是实时用量,`memory.peak` 是历史峰值。除了硬上限,还有几个常用配套文件:

- **`memory.high`**:软上限。**超过 `memory.high` 但未到 `memory.max`**,内核会**放慢**这个 cgroup 的分配(让回收压力增大),但不杀进程。`memory.high` 是"开始施压",`memory.max` 是"动手杀人"。
- **`memory.swap.max`**:swap(交换分区)用量上限。设成 0 = 不允许这个容器用 swap(把内存压力转成"立刻 OOM"而不是"swap 拖慢")。
- **`memory.low`**:保护线。**只要这个 cgroup 用量在 `memory.low` 以下,内核就不会为了别人的回收来抢它的内存**——给"重要容器"一个"保底不被骚扰"的额度。
- **`memory.events`**:事件计数器,关键的有 `oom`(触发 OOM 次数)、`oom_kill`(实际杀死进程次数)、`oom`(还是 OOM 计数)。这是判断"容器是不是被 OOM 过"的窗口。

`memory.max` 一旦超过,会触发什么?**这个 cgroup 的 OOM killer 被唤醒,挑一个进程杀掉**——这就是下一节专门讲的"局部 OOM"。

### 3. io 控制器:用 `io.max` 限带宽/IOPS,用 `io.weight` 分权重

io 控制器(对应 v1 的 blkio)管**块设备(磁盘)IO**。和 cpu 类似,分上限和权重两条路。

**`io.max`:设上限**,但格式比 `cpu.max` 多一截——**要指明是哪块磁盘、限的是哪种指标**:

```
# 限制这个容器对 /dev/sda(8:0)的读取带宽最多 100MB/s
$ echo "8:0 rbps=104857600" > /sys/fs/cgroup/.../io.max
#        ^major:minor 设备号
#                 ^read bytes per second = 100MB

# 限制写 IOPS(每秒写次数)最多 1000
$ echo "8:0 wiops=1000" > /sys/fs/cgroup/.../io.max

# 四个维度:rbps(读带宽)、wbps(写带宽)、riops(读IOPS)、wiops(写IOPS)
```

**`io.weight`:分权重**,范围 1~10000,默认 100。语义和 `cpu.weight` 类似——多 cgroup 抢同一块磁盘时,按 weight 比例分。它还有个变体 `io.bfq.weight`(用 BFQ 调度器时,对交互式负载更友好)。

io 控制器在 v2 里来得很早(4.5 就有),但有个**限制**:它只能管**直接块设备 IO**(走 `read`/`write`/`fsync` 落盘的),管不了**缓冲 IO**(page cache 那部分,因为那算 memory)。所以一个容器的磁盘 IO 限制,常常要配合 memory 限制(限制 page cache 大小)才准。

### 三个控制器的 v1 对照

v1 里这三个控制器的文件名都不一样,初学者最容易混淆。对照表:

| 控制器 | cgroup v2 文件 | cgroup v1 文件 |
|---|---|---|
| CPU 上限 | `cpu.max`(`$QUOTA $PERIOD`) | `cpu.cfs_quota_us` + `cpu.cfs_period_us` 两个文件 |
| CPU 权重 | `cpu.weight`(1~10000) | `cpu.shares`(2~262144) |
| 内存上限 | `memory.max` | `memory.limit_in_bytes` |
| 内存+swap 上限 | `memory.swap.max` | `memory.memsw.limit_in_bytes` |
| IO 上限 | `io.max`(`MAJ:MIN rbps=...`) | `blkio.throttle.read_bps_device` 等(每个指标一个文件) |
| IO 权重 | `io.weight` | `blkio.weight` |

**v2 的命名约定干净一致**(`X.max` 表上限、`X.weight` 表权重),v1 一片混乱——这正是上一节讲的"v2 为什么要重做"的具体体现。**runc 的 v1/v2 两套代码,本质就是在翻译这两套字段名到同一个内部 `Resources` 结构**。

---

## 四、为什么有"局部 OOM":cgroup memory 超限只杀容器,不波及宿主

讲完三个控制器,有一个现象必须专门讲——因为它是运维容器时**最常踩的坑**:容器跑着跑着突然死了,日志里写着 `Killed`、`OutOfMemoryError`。这就是**容器局部 OOM**。

这个现象背后的机制,是 cgroup + 内核 OOM killer 的精妙配合。

### 不这样会怎样:没有局部 OOM,一个容器能拖死全机

想象没有 cgroup 的世界:一个进程内存用爆了,触发的是**全机 OOM**——内核的 OOM killer 在**整个机器**范围内挑一个"分最高的进程"杀掉。这个受害者**可能是你的数据库、你的 SSH 会话、别人的容器**——内核不管,谁分高杀谁。一个流氓进程能让全机上的无辜进程陪着死。这就是第 1 章讲的"裸进程没有限额"的灾难场景之一。

有了 cgroup + `memory.max`,情况完全不同。

### 所以这样设计:cgroup 把 OOM 的"搜杀范围"圈起来

当一个 cgroup 的内存用量**超过 `memory.max`**,会发生这样一连串事:

1. 这个 cgroup 里某个进程尝试分配内存(比如 `malloc` 或读文件进 page cache)。
2. cgroup memory 控制器发现:**这次分配会让这个 cgroup 的内存超过 `memory.max`**。
3. 控制器**先尝试回收**(把 page cache 写回磁盘、换出到 swap),尽量腾出空间——这叫 reclaim。
4. 如果回收也凑不够(都是匿名内存,没法回收),控制器**触发这个 cgroup 的 OOM**。
5. **关键**:这个 OOM 调用的,是**内核那个 OOM killer**(`out_of_memory()` 函数),但参数里带了一个限定——**只在当前这个 cgroup 内选受害者**。
6. OOM killer 在这个 cgroup 里挑一个进程(通常是占内存最多的那个),杀掉它,释放内存。
7. 杀完之后,这个 cgroup 的内存降回 `memory.max` 以下,系统继续转。

**关键在于第 5 步——"只在当前 cgroup 内选受害者"。** 这是 cgroup 给 OOM killer 加的一道"围栏":OOM 的范围被**圈在这个超限的 cgroup 里**,宿主上别的进程、别的容器**完全不受影响**。这就是"局部 OOM"的语义——OOM 还是那个 OOM(底下用的就是内核的 OOM killer),但作用域被 cgroup 收窄了。

> **回扣内核系列**:这底下,正是内核 OOM killer 机制——cgroup 没有发明新的"杀进程算法",它复用了内核 `mm/oom_kill.c` 里那套"按 oom_score 排序、挑分最高的杀"的逻辑,只是把"搜杀范围"从"整个机器"缩小到"这个 cgroup"。**这就是容器"组合内核已有能力"的又一个铁证**——OOM 杀手是内核的,cgroup 只负责"圈范围"。

### 内核代码:从 `memory.max` 到 `out_of_memory()` 的完整路径

我们去内核源码里走一遍这条路径,看清"局部 OOM"是怎么落地的。

**入口:`memory.max` 的写处理函数**。当用户态往 `memory.max` 写一个值(比如 runc 给容器设 512MB),内核里触发的处理函数叫 `memory_max_write`([mm/memcontrol.c](https://github.com/torvalds/linux/blob/master/mm/memcontrol.c),约 L4700-L4749)。这个函数干的事:把新限制写进去,然后**如果发现新限制比当前用量还低**,会尝试回收内存;**回收失败就触发 OOM**:

```c
/* mm/memcontrol.c, memory_max_write 关键路径(简化示意,非逐字源码) */
static ssize_t memory_max_write(struct kernfs_open_file *of, char *buf, size_t nbytes, loff_t off)
{
    /* ... 把 buf 解析成新的 max 值,写入 memcg->memory.max ... */

    if (memcg->memory.max > old_max /* 把上限调高了,没事 */)
        return nbytes;

    /* 把上限调低了 —— 看当前用量是否超过新上限 */
    /* ... 尝试 reclaim 内存 ... */
    if (/* reclaim 没凑够 */) {
        mem_cgroup_out_of_memory(memcg, GFP_KERNEL, 0);   /* ← 触发这个 cgroup 的 OOM */
    }
    return nbytes;
}
```

(注:`memory_max_write` 真实位置在 [mm/memcontrol.c](https://github.com/torvalds/linux/blob/master/mm/memcontrol.c#L4700-L4749),细节随内核版本微调,这里只展示主干逻辑。)

**核心:`mem_cgroup_out_of_memory` 把范围圈起来**。这个函数是"局部 OOM"的灵魂([mm/memcontrol.c](https://github.com/torvalds/linux/blob/master/mm/memcontrol.c#L1876-L1903)):

```c
static bool mem_cgroup_out_of_memory(struct mem_cgroup *memcg, gfp_t gfp_mask, int order)
{
    struct oom_control oc = {
        .zonelist = NULL,
        .nodemask = NULL,
        .memcg = memcg,         /* ← 关键:把搜杀范围限定在这个 memcg 里 */
        .gfp_mask = gfp_mask,
        .order = order,
    };
    bool ret = true;

    if (mutex_lock_killable(&oom_lock))
        return true;
    if (mem_cgroup_margin(memcg) >= (1 << order))
        goto unlock;

    ret = out_of_memory(&oc);   /* ← 调用 mm/oom_kill.c 的内核 OOM killer */
unlock:
    mutex_unlock(&oom_lock);
    return ret;
}
```

读这个函数,核心就一行——**`struct oom_control oc = { ..., .memcg = memcg, ... };`**。

**`.memcg = memcg` 这个字段,就是"局部 OOM"的全部秘密。** 它告诉内核 OOM killer:"你这次选受害者,**只在 `memcg` 这个 cgroup 的进程里挑**,不要看整个机器"。内核的 `out_of_memory(&oc)`(在 [mm/oom_kill.c](https://github.com/torvalds/linux/blob/master/mm/oom_kill.c))收到这个 `oc`,它的 `select_bad_process()` 遍历候选进程时,会**用 `oc->memcg` 过滤**——只挑属于这个 cgroup 的进程打分。

**这一行代码,把"全机 OOM"变成了"局部 OOM"。** 杀的进程,100% 在这个超限的 cgroup 里——别的人、别的容器,完全安全。

### 两个触发路径

注意 `mem_cgroup_out_of_memory` 有两个调用路径,分别对应两种场景:

1. **进程分配内存时,实时发现超限** —— 走 `try_charge_memcg`([mm/memcontrol.c](https://github.com/torvalds/linux/blob/master/mm/memcontrol.c#L2558-L2752)),这是常态路径:进程每次 `malloc`/读文件,内核都要给这次分配"记账(charge)"到所在 cgroup;账超了就先 reclaim,reclaim 失败就调 `mem_cgroup_out_of_memory`。
2. **管理员调小 `memory.max`,新上限低于当前用量** —— 走 `memory_max_write`,直接调 `mem_cgroup_out_of_memory`(就是上面那段)。这是少见但会发生的场景(比如 k8s 在线调整 Pod 的内存限制)。

两条路径,最后都汇聚到 `mem_cgroup_out_of_memory` —— **统一由它去触发"局部 OOM"**。

> **一句话回扣**:"容器局部 OOM"不是 cgroup 发明的新机制,而是 **cgroup 给内核 OOM killer 加了个 `.memcg` 字段,把搜杀范围圈了起来**。OOM killer 还是那个 OOM killer(内核系列的 OOM 章),只是这次它**只在犯事的那个容器里挑受害者**。宿主、别的容器,毫发无伤。这就是为什么"一个容器爆内存,死的只是这个容器自己"——这是 cgroup 给容器世界提供的一道关键隔离。

---

## 关键源码精读:runc 是怎么"给容器上配额"的

讲了这么多机制,现在去看 runc 源码——真正造容器的那个"起重机",是怎么把"用户要限多少 CPU/内存"翻译成"内核 cgroup 文件里的几个数字"的。这一节是本章的源码高潮。

### 第 1 步:配置——runc 描述一个容器的资源限制

OCI runtime-spec(JSON)里有个 `linux.resources` 字段,长这样(简化):

```json
{
  "linux": {
    "resources": {
      "memory": { "limit": 536870912 },
      "cpu": { "quota": 100000, "period": 100000, "shares": 1024 },
      "blockIO": { "weight": 500 }
    }
  }
}
```

每一项就是一类配额。runc 读这份 spec,转成内部一个 `cgroups.Resources` 结构体(在 [runc/libcontainer/configs/config.go](../runc/libcontainer/configs/config.go#L150-L152) 里,`Config.Cgroups` 字段就指向它)。**这个结构体是 runc 资源管理的中心数据结构**——cpu/memory/io 的所有字段都挂在它身上,跨 v1/v2 通用。

### 第 2 步:把容器进程挂进 cgroup——`Apply(pid)`

容器启动时,runc 干的第一件 cgroup 相关的事:**建好 cgroup 目录,把容器进程的 PID 写进去**。这是 [runc/libcontainer/process_linux.go](../runc/libcontainer/process_linux.go#L825) 的调用点:

```go
// Do this before syncing with child so that no children can escape the
// cgroup. We don't need to worry about not doing this and not being root
// because we'd be using the rootless cgroup manager in that case.
if err := p.manager.Apply(p.pid()); err != nil {
    // ... 错误处理 ...
}
```

**注意那行注释:`Do this before syncing with child so that no children can escape the cgroup.`**(在同步子进程之前做这件事,免得子进程逃出 cgroup)。这揭示了 cgroup 应用的**时机**——必须在容器业务进程真正开跑之前把它"关进" cgroup,否则它能在被关进去之前先抢一把资源。这是安全考虑。

`Apply(pid)` 在 v2 模式下走 [fs2/fs2.go](../runc/vendor/github.com/opencontainers/cgroups/fs2/fs2.go#L65-L85):

```go
func (m *Manager) Apply(pid int) error {
    if err := CreateCgroupPath(m.dirPath, m.config); err != nil {   // ← ① 建 cgroup 目录
        // ... rootless 错误处理 ...
        return err
    }
    if err := cgroups.WriteCgroupProc(m.dirPath, pid); err != nil { // ← ② 把 PID 写进 cgroup.procs
        return err
    }
    return nil
}
```

两件事:**`CreateCgroupPath` 建好目录(也就是创建一个新 cgroup),`WriteCgroupProc` 把 PID 写进 `cgroup.procs`**。第二件事就是"挂进程进 cgroup"的全部操作,看 [utils.go](../runc/vendor/github.com/opencontainers/cgroups/utils.go#L380-L415):

```go
// WriteCgroupProc writes the specified pid into the cgroup's cgroup.procs file
func WriteCgroupProc(dir string, pid int) error {
    if dir == "" {
        return fmt.Errorf("no such directory for %s", CgroupProcesses)
    }
    if pid == -1 {
        return nil
    }

    file, err := OpenFile(dir, CgroupProcesses, os.O_WRONLY)  // ← 打开 cgroup.procs
    if err != nil {
        return fmt.Errorf("failed to write %v: %w", pid, err)
    }
    defer file.Close()

    for range 5 {
        _, err = file.WriteString(strconv.Itoa(pid))           // ← 把 PID 字符串写进去
        if err == nil {
            return nil
        }
        // EINVAL might mean that the task being added to cgroup.procs is in state
        // TASK_NEW. We should attempt to do so again.
        if errors.Is(err, unix.EINVAL) {
            time.Sleep(30 * time.Millisecond)
            continue
        }
        return fmt.Errorf("failed to write %v: %w", pid, err)
    }
    return err
}
```

**就这么几行**——打开 `cgroup.procs` 文件,把 PID 字符串写进去,**完事**。容器进程从此就被"关"进了这个 cgroup。注释里 `CgroupProcesses = "cgroup.procs"`([utils.go L22](../runc/vendor/github.com/opencontainers/cgroups/utils.go#L22)),那个重试逻辑是应对子进程还处在 `TASK_NEW` 状态(刚 fork 还没完全就绪)的特殊情况——内核此时会返回 EINVAL,runc 等 30ms 重试。

**这一段把"挂进程进 cgroup"这件事,还原到了最朴素**:就是写一个数字进一个文件。没有任何神秘 API,内核在 `cgroup.procs` 的写处理函数(`cgroup_procs_write` → `cgroup_attach_task`,见 [kernel/cgroup/cgroup.c](https://github.com/torvalds/linux/blob/master/kernel/cgroup/cgroup.c),`cgroup_procs_write` 约 L5427、`cgroup_attach_task` 约 L3015-L3043)里把这个 PID 真正关联到 cgroup。

### 第 3 步:写配额——`Set(Resources)` 是配额设定的总入口

PID 挂进 cgroup 后,第二件事:**把 cpu / memory / io 的限制值,写到对应的 cgroup 文件里**。这是 `Manager.Set()` 的活。看 v2 版本([fs2/fs2.go](../runc/vendor/github.com/opencontainers/cgroups/fs2/fs2.go#L177-L231)):

```go
func (m *Manager) Set(r *cgroups.Resources) error {
    if r == nil {
        return nil
    }
    if err := m.getControllers(); err != nil {
        return err
    }
    // pids (since kernel 4.5)
    if err := setPids(m.dirPath, r); err != nil {
        return err
    }
    // memory (since kernel 4.5)
    if err := setMemory(m.dirPath, r); err != nil {       // ← 设 memory.max 等
        return err
    }
    // io (since kernel 4.5)
    if err := setIo(m.dirPath, r); err != nil {           // ← 设 io.max 等
        return err
    }
    // cpu (since kernel 4.15)
    if err := setCPU(m.dirPath, r); err != nil {          // ← 设 cpu.max / cpu.weight
        return err
    }
    // devices、cpuset、hugetlb、rdma、freezer 等略 ...
    if err := m.setUnified(r.Unified); err != nil {       // ← 用户自定义的任意 v2 文件
        return err
    }
    m.config.Resources = r
    return nil
}
```

读这个函数,信息量很大:

1. **`Set` 就是挨个控制器调用一遍 `setXxx`**——`setPids`、`setMemory`、`setIo`、`setCPU`、`setDevices`、`setCpuset`、`setHugeTlb`、`setFreezer`……每个控制器的设置函数各自管自己那一摊。
2. **注释里标了"since kernel X.X"**——这告诉我们**每个控制器在 v2 里都是某个内核版本才加入的**(memory/io 在 4.5,cpu 在 4.15,cpuset 在 5.0……)。这是上一节讲的"v2 控制器来得有先后"的代码级证据。
3. **最后那个 `m.setUnified(r.Unified)`** 是个逃生口——它允许用户写**任意** cgroup v2 文件(键值对),不限于 runc 已经封装好的那些。这是给新控制器、新参数留的扩展点。

每个 `setXxx` 函数,干的事都是同一个套路:**读 `Resources` 结构体里自己那几个字段 → 转成字符串 → 写到对应的 cgroup 文件**。下面挨个看三个核心的。

### 第 4 步:`setMemory`——往 `memory.max` 写数字

看 v2 的 `setMemory`([fs2/memory.go](../runc/vendor/github.com/opencontainers/cgroups/fs2/memory.go#L35-L78)):

```go
func setMemory(dirPath string, r *cgroups.Resources) error {
    if !isMemorySet(r) {
        return nil
    }

    if err := CheckMemoryUsage(dirPath, r); err != nil {   // ← 检查:新上限是不是比当前用量还低
        return err
    }

    swap, err := cgroups.ConvertMemorySwapToCgroupV2Value(r.MemorySwap, r.Memory)
    if err != nil {
        return err
    }
    swapStr := numToStr(swap)
    if swapStr == "" && swap == 0 && r.MemorySwap > 0 {
        swapStr = "0"
    }
    // never write empty string to `memory.swap.max`, it means set to 0.
    if swapStr != "" {
        if err := cgroups.WriteFile(dirPath, "memory.swap.max", swapStr); err != nil {  // ← 设 swap 上限
            // ... 容错 ...
        }
    }

    if val := numToStr(r.Memory); val != "" {
        if err := cgroups.WriteFile(dirPath, "memory.max", val); err != nil {          // ← 设内存上限
            return err
        }
    }

    if val := numToStr(r.MemoryReservation); val != "" {
        if err := cgroups.WriteFile(dirPath, "memory.low", val); err != nil {          // ← 设保护线
            return err
        }
    }

    return nil
}
```

逐行读,`setMemory` 干三件事:**写 `memory.swap.max`(swap 上限)、写 `memory.max`(内存上限)、写 `memory.low`(保护线)**。**每一个都是一次 `cgroups.WriteFile`——就是往文件里写字符串。** 注意 `numToStr` 函数([fs2/memory.go](../runc/vendor/github.com/opencontainers/cgroups/fs2/memory.go#L21-L29))把 `0` 转成空串(不设置)、把 `-1` 转成 `"max"`(不限制)——这是从 v1 沿用下来的约定。

**开头的 `CheckMemoryUsage` 是个保护机制**(看 [fs2/fs2.go](../runc/vendor/github.com/opencontainers/cgroups/fs2/fs2.go#L299-L329)):如果用户给的新上限**比这个 cgroup 当前用量还低**,直接报错——因为这种情况下,设上限会立刻触发 OOM(内存已经超了),往往不是用户想要的。这是 runc 给运维加的"防呆"。

**v1 对照**:v1 的 `setMemory` 干的是一模一样的事,只是写的是不同文件名(`memory.limit_in_bytes` 而不是 `memory.max`)。看 [fs/memory.go](../runc/vendor/github.com/opencontainers/cgroups/fs/memory.go#L36-L58):

```go
func setMemory(path string, val int64) error {
    if val == 0 {
        return nil
    }
    err := cgroups.WriteFile(path, cgroupMemoryLimit, strconv.FormatInt(val, 10))
    // cgroupMemoryLimit = "memory.limit_in_bytes"   ← v1 的文件名
    // ...
}
```

`cgroupMemoryLimit` 常量定义在 [fs/memory.go L21](../runc/vendor/github.com/opencontainers/cgroups/fs/memory.go#L21):

```go
cgroupMemoryLimit = "memory.limit_in_bytes"   // ← v1 的内存上限文件名
```

**同一个"设内存上限"的语义,v1 写到 `memory.limit_in_bytes`,v2 写到 `memory.max`——runc 的两套代码,翻译了两套字段名到同一个 `Resources.Memory` 字段**。这就是 v1/v2 差别在代码层面的具体体现。

### 第 5 步:`setCPU`——往 `cpu.max` 和 `cpu.weight` 写

看 v2 的 `setCPU`([fs2/cpu.go](../runc/vendor/github.com/opencontainers/cgroups/fs2/cpu.go#L19-L77))主干:

```go
func setCPU(dirPath string, r *cgroups.Resources) error {
    if !isCPUSet(r) {
        return nil
    }
    // ... cpu.idle 略 ...

    // NOTE: .CpuShares is not used here. Conversion is the caller's responsibility.
    if r.CpuWeight != 0 {
        if err := cgroups.WriteFile(dirPath, "cpu.weight",
            strconv.FormatUint(r.CpuWeight, 10)); err != nil {       // ← 设 cpu.weight(权重)
            return err
        }
    }
    // ... cpu.max.burst 略 ...

    if r.CpuQuota != 0 || r.CpuPeriod != 0 {
        str := "max"
        if r.CpuQuota > 0 {
            str = strconv.FormatInt(r.CpuQuota, 10)
        }
        period := r.CpuPeriod
        if period == 0 {
            // 默认值,见 kernel 文档 admin-guide/cgroup-v2.html
            period = 100000
        }
        str += " " + strconv.FormatUint(period, 10)
        if err := cgroups.WriteFile(dirPath, "cpu.max", str); err != nil {   // ← 设 cpu.max(上限)
            return err
        }
        // ...
    }

    return nil
}
```

读这段,`setCPU` 干两件事:**写 `cpu.weight`(权重,默认 100)、写 `cpu.max`(上限)**。

注意 `cpu.max` 的字符串拼装——它要拼成 `"$QUOTA $PERIOD"` 格式(比如 `"100000 100000"`),`str += " " + period`。**Quota 默认是 `"max"`(不限制),Period 默认 100000 微秒(100ms)**——这俩默认值,内核文档里写明了,代码里也照搬。

还有一处细节:`// NOTE: .CpuShares is not used here. Conversion is the caller's responsibility.`——**v1 的 `CpuShares` 不在这里直接用**,因为 v2 用的是 `cpu.weight`(1~10000),v1 用的是 `cpu.shares`(2~262144),两者单位不一样。转换由调用者做(`ConvertCPUSharesToCgroupV2Value`,在 [utils.go L425](../runc/vendor/github.com/opencontainers/cgroups/utils.go#L425) 附近)。**这又是 v1/v2 差别的一个微观体现:连权重都换了一套单位**。

### 第 6 步:`setIo`——往 `io.max` 写每设备每指标的配额

最后看 v2 的 `setIo`([fs2/io.go](../runc/vendor/github.com/opencontainers/cgroups/fs2/io.go#L39-L98)),它的格式比前两个复杂——**每块磁盘、每个指标各写一行**:

```go
func setIo(dirPath string, r *cgroups.Resources) error {
    if !isIoSet(r) {
        return nil
    }

    // 如果有 BFQ 调度器,优先用它管权重
    var bfq *os.File
    if r.BlkioWeight != 0 || len(r.BlkioWeightDevice) > 0 {
        bfq, err = cgroups.OpenFile(dirPath, "io.bfq.weight", os.O_RDWR)
        // ...
    }

    if r.BlkioWeight != 0 {
        if bfq != nil {
            // 用 BFQ
            // ...
        } else {
            // 回退到 io.weight,要转换权重值
            v := cgroups.ConvertBlkIOToIOWeightValue(r.BlkioWeight)
            if err := cgroups.WriteFile(dirPath, "io.weight",
                strconv.FormatUint(v, 10)); err != nil {                  // ← 设 io.weight
                return err
            }
        }
    }
    // ... 设备级权重略 ...

    for _, td := range r.BlkioThrottleReadBpsDevice {
        if err := cgroups.WriteFile(dirPath, "io.max", td.StringName("rbps")); err != nil {  // ← 读带宽
            return err
        }
    }
    for _, td := range r.BlkioThrottleWriteBpsDevice {
        if err := cgroups.WriteFile(dirPath, "io.max", td.StringName("wbps")); err != nil {  // ← 写带宽
            return err
        }
    }
    for _, td := range r.BlkioThrottleReadIOPSDevice {
        if err := cgroups.WriteFile(dirPath, "io.max", td.StringName("riops")); err != nil { // ← 读 IOPS
            return err
        }
    }
    for _, td := range r.BlkioThrottleWriteIOPSDevice {
        if err := cgroups.WriteFile(dirPath, "io.max", td.StringName("wiops")); err != nil { // ← 写 IOPS
            return err
        }
    }

    return nil
}
```

读这段,`setIo` 干两类事:**设权重(`io.weight` 或 `io.bfq.weight`)、设上限(每块磁盘每个指标写一行到 `io.max`)**。注意那个 **BFQ 优先** 的逻辑——如果内核启用了 BFQ IO 调度器(对交互式负载友好),runc 优先用 `io.bfq.weight`;否则回退到通用 `io.weight`。

四个 `io.max` 写入循环,对应四个维度(rbps/wbps/riops/wiops),每个循环遍历用户配置的"每设备限速列表",逐个写入。**每个写入都是一行 `"$MAJ:$MIN rbps=$VAL"` 格式**——比如 `"8:0 rbps=104857600"`(限 `/dev/sda` 读带宽 100MB/s)。`td.StringName("rbps")` 就是把设备号和指标名拼成这行字符串。

### 串起来:从配置到 cgroup 文件

把六步合起来,runc 给容器上配额的完整链路:

```
   docker run --cpus=2.5 --memory=512m --device-read-bps /dev/sda:100mb ...
              │
              ▼ docker/containerd 把意图翻译成 OCI runtime-spec (JSON)
   linux.resources = { cpu:{quota:250000, period:100000}, memory:{limit:536870912}, ... }
              │
              ▼ runc 读 spec,转成 cgroups.Resources 结构体
              │
              ▼ manager.New(config.Cgroups) → 判 v1/v2 → 选 fs 或 fs2 manager
              │
              ▼ Apply(pid):建 cgroup 目录 + 写 PID 到 cgroup.procs
              │              ↓ (内核 cgroup_attach_task 把进程真正关联到 cgroup)
              ▼ Set(Resources):
                  ├─ setMemory → 写 memory.max、memory.swap.max、memory.low
                  ├─ setCPU    → 写 cpu.weight、cpu.max("250000 100000")
                  └─ setIo     → 写 io.weight、io.max("8:0 rbps=104857600")
              │
              ▼ 内核此后按这些 cgroup 文件,对这个容器计量 + 限额
```

**这条链路里没有任何"虚拟化"**。每一步都是普通的文件读写:`OpenFile` + `WriteString`。容器的资源限制,从用户角度看是 `--cpus`、`--memory` 这些命令行参数,从内核角度看就是几个 cgroup 文件里的几个数字——runc 的工作,就是把前者**忠实地翻译**成后者。

> **再扣一次第一性原理**:容器没有任何神秘力量。它的资源隔离,来自**往内核 cgroup 文件系统里的几个文件,写了几个数字**。docker / k8s 做的所有"资源管理"产品化,都是盖在这套朴素的文件接口之上。

---

## 章末小结

### 用航运比喻回顾本章

回到港口。第 2 章我们给集装箱装上了**不透光的铁皮舱壁**(namespace)——舱里的货看不见、碰不到别的箱。但本章一开头就戳破:**舱壁只挡视线,不挡胃口**——一个集装箱照样能把货轮的动力(CPU)、燃料舱(内存)、装卸设备(磁盘 IO)吃光榨干,把别的箱饿死。

这一章干的,就是给每个集装箱**配上载重 / 体积 / 温控配额**——这就是 cgroup。

- **cgroup 干的最朴素的事**:把若干进程归为一组(起个名,叫一个 cgroup),内核按这个组**计量它用了多少**(读 `memory.current` / `cpu.stat` / `io.stat`)、**限制它最多能用多少**(写 `memory.max` / `cpu.max` / `io.max`)。**全是文件读写**——`cgroup.procs` 写 PID 就是"归组",`X.max` 写数字就是"限额"。

- **两代演进**:v1(每个控制器一棵独立层级树)太乱——进程归属不一致、控制器组合受限、字段名各干各的。v2(2016,4.5)统一成**一棵树挂所有控制器**——一个进程在一棵树上只有一个位置、归属唯一、字段名约定一致(`X.max` 表上限、`X.weight` 表权重)。**v2 是趋势,v1 在退场**,runc 现在两套都支持,启动时自动选(就靠 [manager/new.go](../runc/vendor/github.com/opencontainers/cgroups/manager/new.go#L29-L55) 那一行 `IsCgroup2UnifiedMode()`)。

- **三个最常用控制器**:cpu(`cpu.max` 限上限、`cpu.weight` 分权重)、memory(`memory.max` 限硬上限、`memory.high` 软上限、`memory.low` 保护线)、io(`io.max` 按设备按指标限上限、`io.weight` 分权重)。三个加起来覆盖 95% 的容器资源限制需求。

- **"局部 OOM"的真相**:容器内存超 `memory.max` 触发的 OOM,**底下就是内核 OOM killer**(`mm/oom_kill.c` 的 `out_of_memory()`)——只是 cgroup 给它加了个 `.memcg` 字段,把**搜杀范围圈在这个超限的容器内**。死的只有这个容器的进程,宿主、别的容器毫发无伤。这是 cgroup 复用内核机制的经典一例——**它没发明 OOM 算法,只圈了范围**。

最后,runc 这个"起重机"程序,把"用户要限多少"翻译成"cgroup 文件里的几个数字",核心代码就是**挨个控制器调 `setXxx` 函数,每个函数都是 `OpenFile` + `WriteString`**。**没有任何神秘 API**——这又一次印证全书第一性原理:容器 = 内核能力的组合,而 cgroup 这块基石的组合方式,朴素到就是"写文件"。

### 本章在全书主线中的位置

记住全书的二分法:**打包隔离 vs 调度编排**。

这一章,我们补上了**"打包隔离"这半边的第二块基石**:

- 第 2 章 namespace(舱壁)→ 隔离"进程**看见**的世界"。
- 第 3 章 cgroup(配额)→ 隔离"进程**能用**的资源"。
- 第 4 章 rootfs(小甲板)→ 隔离"进程**站在**哪个文件系统上"。

namespace 和 cgroup **是正交的两件外套**——一个管"看见什么",一个管"用多少",两者必须同时穿,缺一不可。第 1 章那句话——**容器 = 普通进程 + namespace + cgroup**——到这里,namespce 和 cgroup 这两个核心名词,你都已经在源码层面见过了。

从内核视角看,这三块基石**全是内核早已有的能力**——namespace、cgroup、`pivot_root`/`chroot`——的组合。这再次印证第 1 章的铁证:**容器没有发明新东西,它只是把内核早就摆在那儿的零件,按"造一个隔离世界"的需求拼起来**。

而**有了配额,容器才真正"可调度"**——这正是 k8s 半本(第 5~6 篇)的基础。一个 Pod 要多少 CPU、多少内存(`requests`/`limits`),调度器才能据此决定"这箱货能不能上这艘船";节点资源是否够,也靠 cgroup 的用量统计上报。**没有 cgroup,k8s 的调度编排根本无从谈起**——这会在第 18 章(调度器)和第 19 章(kubelet)回扣。

### 五个"为什么"清单

如果你只能记五件事,记这五件:

1. **为什么 namespace 不管资源**:namespace 只动"进程看见的视图",不动"进程用的资源"——视图和用量是两个正交的维度。一个全 namespace 装备的容器,照样能把宿主 CPU/内存/IO 吃光。必须再用 cgroup 补上"用量"这一维——这就是为什么容器必须**两件外套都穿**。

2. **为什么要有 v2**:v1 每个控制器一棵独立树,导致进程归属不一致(同一进程在不同控制器里挂不同位置)、控制器组合受限、字段名混乱。v2 把所有控制器挂到**同一棵统一层级树**,归属唯一、字段名约定一致(`X.max`/`X.weight`)。v2 是 2016 进内核的趋势,v1 在退场,runc 两套都支持,启动时靠 `IsCgroup2UnifiedMode()` 自动选。

3. **`cpu.max` / `memory.max` / `io.max` 各怎么计量限额**:cpu 用"每周期配额"(`cpu.max` = `"$QUOTA $PERIOD"`,微秒),配额用完就 throttling;memory 用字节(`memory.max`),超限触发局部 OOM;io 按设备按指标(`io.max` = `"$MAJ:$MIN rbps=... wbps=... riops=... wiops=..."`)。三个都配套有权重文件(`cpu.weight`/`io.weight`)做软分配。**设置方式全是往文件里写数字**。

4. **"局部 OOM"的真相**:容器内存超 `memory.max`,触发的是内核 OOM killer(`mm/oom_kill.c` 的 `out_of_memory()`),但 cgroup 通过 `struct oom_control` 里的 `.memcg` 字段,**把搜杀范围限定在这个超限的 cgroup 内**(见 [mm/memcontrol.c L1876-L1903](https://github.com/torvalds/linux/blob/master/mm/memcontrol.c#L1876-L1903) 的 `mem_cgroup_out_of_memory`)。死的是这个容器自己的进程,宿主和别的容器毫发无伤。**cgroup 没发明 OOM 算法,只圈了范围**——这是"复用内核机制"的经典一例。

5. **runc 怎么给容器上配额**:`manager.Apply(pid)` 建 cgroup 目录 + 写 PID 到 `cgroup.procs`(挂进程进组);`manager.Set(Resources)` 挨个控制器调 `setMemory`/`setCPU`/`setIo`,**每个函数都是 `OpenFile` + `WriteString`**,往 `memory.max` / `cpu.max` / `io.max` 等文件里写几个数字。v1 和 v2 的差别,本质就是**同一语义翻译到不同的文件名**(v1 `memory.limit_in_bytes` vs v2 `memory.max`)。

### 想继续深入,该往哪钻

- **亲手观测 cgroup**:在跑着容器的 Linux 上,`cat /sys/fs/cgroup/<container-id>/memory.max`、`memory.current`、`cpu.max`、`cpu.stat`——你会亲眼看到 `nr_throttled`(CPU 被限流次数)、`memory.events` 里的 `oom_kill`(被 OOM 次数)。这是最直接的实证。
- **看 runc 的 v1/v2 切换**:[manager/new.go](../runc/vendor/github.com/opencontainers/cgroups/manager/new.go) 的 `NewWithPaths`(L29)是 v1/v2 总开关;[fs/fs.go](../runc/vendor/github.com/opencontainers/cgroups/fs/fs.go#L17-L33) 的 `subsystems` 列表是 v1 的 15 个控制器;[fs2/fs2.go](../runc/vendor/github.com/opencontainers/cgroups/fs2/fs2.go#L177-L231) 的 `Set` 是 v2 的统一设置入口。
- **看 runc 怎么写 memory.max / cpu.max / io.max**:本章引用的 [fs2/memory.go](../runc/vendor/github.com/opencontainers/cgroups/fs2/memory.go#L35-L78) 的 `setMemory`、[fs2/cpu.go](../runc/vendor/github.com/opencontainers/cgroups/fs2/cpu.go#L19-L77) 的 `setCPU`、[fs2/io.go](../runc/vendor/github.com/opencontainers/cgroups/fs2/io.go#L39-L98) 的 `setIo`——三个加起来不到 200 行,就是容器资源限制的全部核心逻辑。
- **看内核的"局部 OOM"路径**:[mm/memcontrol.c](https://github.com/torvalds/linux/blob/master/mm/memcontrol.c) 的 `mem_cgroup_out_of_memory`(L1876,核心是 `.memcg = memcg` 这一行)+ `try_charge_memcg`(L2558,实时记账触发 OOM 的入口)+ `memory_max_write`(L4700,调小 max 时触发 OOM)。再回扣内核系列的 OOM 章,你会看清"局部 OOM"完全复用了内核 OOM killer,只是圈了范围。
- **看内核 cgroup 核心**:[kernel/cgroup/cgroup.c](https://github.com/torvalds/linux/blob/master/kernel/cgroup/cgroup.c) 的 `cgroup_attach_task`(L3015,把进程关联到 cgroup)、`cgroup_procs_write`(L5427,`cgroup.procs` 文件的写处理)、`cgroup_create`(L5868,创建新 cgroup 节点)。v1 的挂载逻辑在 [kernel/cgroup/cgroup-v1.c](https://github.com/torvalds/linux/blob/master/kernel/cgroup/cgroup-v1.c)。

---

> 舱壁装好了,配额上好了。集装箱里的货,看不见别人、也吃不垮货轮了。**但还有个洞敞着**:舱壁围住的这块空间,**脚下踩的是哪一片甲板?** 一个 `ls /` 下去,看到的还是货轮的公用大甲板——`/etc/shadow` 能读、`/root` 能翻。namespace 隔开了"看见谁",cgroup 隔开了"用多少",可"**站在哪个文件系统上**"还没隔开。下一章,我们把集装箱连根拔起,挪到它自己的小甲板上——翻开 **第 4 章 · rootfs 与 pivot_root:换个根**。
