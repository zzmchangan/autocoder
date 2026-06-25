# 第 21 章 · 存储:Volume / PV / PVC

> **前置**:你需要先读过 [第 5 章《联合文件系统 overlayfs:分层镜像为什么这么设计》](P1-05-联合文件系统overlayfs-分层镜像为什么这么设计.md) 和 [第 17 章《声明式 API 与 reconcile:k8s 的灵魂》](P6-17-声明式API与reconcile-k8s的灵魂.md)。第 5 章结尾留了一颗钉子——集装箱那块小甲板的内容是分层叠出来的,公共层(lower)大家共用,改动都落在本集装箱独有那块**可写托盘(upper)**上;可它还顺嘴说了一句要命的话:"容器一删,upper 一清,世界恢复如初"。这句话的好处是镜像永远干净,坏处是——**容器一删,数据也没了**。第 17 章给了我们看懂本章的工具:声明式 API + controller 的 watch-reconcile 循环。本章的存储抽象,正是建立在这两样东西之上。

> **核心问题**:容器文件系统是临时的(容器一死数据就没了),怎么给容器接持久化存储?而且在一个有几百个节点、几十种后端磁盘/网络存储的集群里,怎么做到"用的人不知道存储具体在哪、给存储的人不知道谁在用"?
>
> 这是第 7 篇(进阶与边界)的开篇。前面 6 篇我们一路讲下来,集装箱怎么造、怎么装上船、怎么被航运中心调度,数据这条线一直悬着没解决。这一章把它收口:让一个会随船沉的数据舱,变成一个**独立于任何一艘船、随时可以重新接到别的船上的"岸上仓库"**。
>
> **读完本章你会明白**:
> - 为什么容器文件系统是**临时**的——回扣第 5 章 overlayfs 的 upper 层,容器删除即丢,这是整个持久化存储问题的源头。
> - 为什么**不能"直接给容器挂个目录"了事**——在一个有几百节点、几十种存储后端的集群里,直接挂会把"用存储"和"给存储"死死焊在一起。PVC/PV 的解耦就是为了切开这层焊点。
> - **PVC / PV / StorageClass 三层抽象**各自是什么、为什么这样切:谁声明需求、谁描述供给、谁负责按需自动造。静态绑定 vs 动态供给差在哪。
> - **CSI(Container Storage Interface)** 为什么是必须的:存储也像网络(CNI)、运行时(CRI)一样做成可插拔标准,否则 k8s 会被某家存储厂商绑死。

> **如果一读觉得太难**:先只记住三件事——① 容器的 upper 层是临时的,数据要持久就得挂一块"集装箱之外的仓库"(Volume);② 集群里这块仓库分两层声明:PVC 是"我要一块仓库"(应用方),PV 是"集群里真有这么一块仓库"(供给方),StorageClass 是"没有就按这个模板自动造一块"(运维模板);③ 真正把存储后端接进来的是 CSI 插件,k8s 只负责把"要"和"有"撮合到一起(bind),不管造仓和挂仓的具体活儿。这三句撑起整章。

---

## 章首 · 容器一死,数据去了哪

把第 5 章的结论再念一遍:

> 镜像 = 一摞只读 lower 层;容器实例 = 这摞 lower + 一块**可写 upper**。容器所有的改动(写日志、落库、改配置)都落在 upper 里。`docker rm` 一删,upper 一清,镜像(lower)毫发无损——下次还能从同一个镜像起一个全新干净的容器。

这句话对"无状态"应用是天堂(每次重启都是干净状态,行为可预测),对"有状态"应用是地狱:

- 一个 MySQL 容器,所有表数据写在 upper 里。运维 `kubectl delete pod mysql-0`,Pod 重建,upper 没了——**整个数据库凭空消失**。
- 一个 Redis 容器做了 RDB 持久化,快照文件写在 upper 的 `/var/lib/redis/dump.rdb`。Pod 一重启,快照没了,**几小时前的数据全丢**。
- 一个 Elasticsearch,索引数据写在 upper。节点故障,Pod 被调度到另一台机器,新机器上**空空如也**,从头索引。

这不是"配置没配对"的小问题,这是容器模型在"数据"这一项上的**根本缺陷**:upper 的生命周期和容器绑死,而数据需要的生命周期是"跨越容器的生死"。一个数据库的命,得比任何一个具体的容器实例长。

> **比喻**:回到航运。集装箱里那块可写托盘(upper),是**跟着集装箱走的**——集装箱被吊下船、扔进海里,托盘上的货也跟着没了。这对快消品(无状态服务,每次重新装就好)没问题;但对**珠宝、账本、合同**(数据库、用户上传、交易流水)这种不能丢的东西,你绝不敢放集装箱自带的托盘里。你需要的,是**码头边上那座独立的岸上仓库**——它不跟着任何一艘船走,船沉了仓还在;这艘船走了,下艘船来了,把仓库的门重新接到新船上,货还在原处。

这座"岸上仓库",在容器世界里叫 **Volume(存储卷)**;在一个有几百艘船、几十种仓库的航运网络里,怎么把"我要一座仓库"和"岸上有这么一座仓库"撮合起来、还互不认识对方,就是本章后半段 PV/PVC/StorageClass 要解决的事。

我们先从最小的"怎么挂一座仓库"讲起,再一层层往上抽象。

---

## 一、Volume:在容器之外开一块地

最朴素的需求:**给容器一块容器之外的存储,它的生命周期独立于容器**。这块存储可以是一个目录、一块磁盘、一个网络存储,但关键是——容器死,它不死。

k8s 在 Pod 这个层级给了这个抽象,叫 **Volume**。一个 Pod 可以声明若干个 Volume,然后把它们挂(mount)到容器里的某个路径。容器读写这个路径,实际读写的是 Volume 指向的那块外部存储;容器死了,Volume 还在,同一个 Pod 里重启的新容器、或者下个 Pod 实例,还能挂同一块 Volume 接着用。

### 不这样会怎样:容器自带存储的两个坑

如果只有 overlayfs 那块 upper,没有 Volume,有两个坑填不上:

**坑一:容器一删,数据没了**(前面讲过的)。

**坑二:同一个 Pod 里的多个容器没法共享文件**。一个 Pod 里跑了 web 容器和日志收集 sidecar,web 要把自己写的访问日志给 sidecar 读。两个容器各有各的 upper(文件系统是隔离的),互相看不见。怎么办?需要一个**两个容器都能挂上去**的地方。

Volume 把这两个坑一起填了:它是 Pod 级别的资源(不属于任何一个容器),Pod 里所有容器都能把它挂到自己的某个路径,从而共享同一块存储;它的生命周期跟 Pod(在最简单的情况下)而不是单个容器绑。

### 所以这样设计:Pod 声明 Volume,容器挂 Volume

一个 Pod spec 长这样(简化):

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: web-with-log
spec:
  volumes:                       # ← Pod 级别声明:这个 Pod 要两块存储卷
    - name: html
      emptyDir: {}               # 类型一:临时空目录
    - name: data
      hostPath:                  # 类型二:宿主机上一个路径
        path: /srv/data
  containers:
    - name: web
      image: nginx
      volumeMounts:              # ← 容器把卷挂到自己的某个路径
        - name: html
          mountPath: /usr/share/nginx/html
    - name: log-collector
      image: fluentd
      volumeMounts:
        - name: html
          mountPath: /var/log/nginx      # 两个容器挂同一块卷 → 共享
```

这里有两件值得品的事:

- **Volume 的"类型"极多**。上面出现了 `emptyDir`(Pod 一删就没了的临时空目录,本质是宿主机上一块临时盘)、`hostPath`(直接指宿主机某个路径)。k8s 还内置了几十种:`nfs`、`cephfs`、`awsElasticBlockStore`、`gcePersistentDisk`、`configMap`、`secret`……每种对应一种后端。**Volume 的 `volumes` 字段,本质上是个"后端类型 + 参数"的声明。**
- **Volume 是 Pod 局部的**。`emptyDir`、`hostPath` 这种,都是"跟着这个 Pod 走、跟着这个节点走"的,**Pod 没了、节点换了,这块卷也就没了或换地方了**。这解决了一些共享问题,但**没解决"数据要跨 Pod、跨节点长期存活"的问题**——`hostPath` 还把 Pod 钉死在某个节点上(换台机器就找不到那个路径了)。

> 这里要敲一下黑板:`emptyDir` 名字带"Dir",**它不是持久化的**——Pod 删除它就没了,只是比容器活得更长一点(能撑过容器重启)。真正能让数据"跨 Pod 跨节点长期存活"的 Volume 类型,是后面要讲的 **PV(PersistentVolume)**。`emptyDir` 更像"集装箱里多放一块共享托盘",而不是"岸上仓库"。

### Volume 的局限:Pod 级别,描述了"怎么挂",没描述"在哪、谁的"

到这里,Volume 解决了"容器之外有块地"这个最基本的问题。但放在一个**几百节点、几十种存储后端**的真实集群里,直接用 Volume 类型会立刻撞墙:

- 应用开发者要在 Pod spec 里写 `awsElasticBlockStore: { volumeID: vol-xxx }`——他得**知道这块 EBS 的 ID**。可应用开发者凭什么要懂 AWS?他凭什么要知道存储具体在哪台机器、用什么介质?
- 运维/存储团队造了一块 NFS,想给某个团队用,得告诉对方"挂在 `nfs.example.com:/export/data`,参数是这样"——**存储的具体细节全暴露给了使用方**。哪天运维想换存储厂商,所有 Pod spec 全得改。
- 更要命的是,**应用开发者根本不该关心存储是怎么来的**。他只想说"给我 10GB 能读写、跨节点只读共享的存储",至于这块存储是 NFS、Ceph、EBS 还是本地盘,跟他无关。

这撞墙的本质是:**Volume 把"用存储的人"和"给存储的人"焊死在了一起**。在一个小集群、一个团队里这还能凑合;在跨团队、跨厂商的大集群里,这种焊点就是灾难。下一节,我们就来看 k8s 怎么用 **PVC / PV 这对解耦**切开这个焊点。

---

## 二、为什么不能直接挂:用的人不该知道,给的人不该认识

这是理解整个 PV/PVC 体系最关键的"为什么"。我们先把这个"焊死"的灾难画清楚,再看解耦。

### 不这样会怎样:四种角色被搅成一锅粥

设想一个真实场景:一个公司有 500 个 k8s 节点,跑着几百个应用,存储后端五花八门——开发环境用本地盘、测试用 NFS、生产用 Ceph RBD、关键业务用云厂商的块存储(EBS/PV disk)。涉及四类角色:

1. **应用开发者**:写业务代码的人。他关心的是"我的 MySQL 要 50GB、读写、挂单个节点"。
2. **运维/SRE**:管集群的人。他关心的是"这块 50GB 给哪个应用用了、要不要做快照、回收策略是啥"。
3. **存储管理员**:管存储后端的人。他关心的是"Ceph 集群还有多少空间、哪些卷是给生产的、哪天要扩容"。
4. **存储厂商**:写 CSI 驱动的人。他关心的是"我的驱动怎么和 k8s 对接"。

如果全用裸 Volume(直接在 Pod spec 里写后端类型和参数),这四类角色被搅成一锅:

- 应用开发者被迫学懂"我现在要写 cephfs 还是 awsElasticBlockStore"——**业务代码被存储后端污染**。
- 存储管理员不知道"现在集群里到底有多少块存储、谁在用"——因为存储信息散落在几百个 Pod spec 里,**没有统一台账**。
- 想换存储厂商?几百个 Pod spec 一个个改,**没有抽象层做缓冲**。

### 所以这样设计:声明需求的(PVC)和描述供给的(PV)分开

k8s 的答案是:**把"我要一块存储"和"我有一块存储"拆成两个独立的 API 对象**,让它们只通过"撮合"耦合,互不直接引用对方的具体细节。

> **比喻**:回到航运。一个贸易商(应用开发者)要发一批巧克力,他到航运中心填一张单子:"**我要一座 100 平米、恒温 18 度、能在 A 港和 B 港之间调度的岸上仓库**"——他**不知道**这座仓库具体在哪个港口、是钢筋的还是砖混的、归哪家仓储公司管。与此同时,各港口的仓储公司在航运中心的另一本台账上登记:"**我在 A 港有 200 平米、恒温 18 度的仓库一座,编号 W-007**"——它**不知道**这座仓库最终会租给哪个贸易商。航运中心有个撮合员(controller),盯着这两本台账,把"要"和"有"按规则匹配上,然后在两张单子上都盖上对方的章:**绑定(bind)**。

这两张单子,就是 k8s 里的:

- **PVC(PersistentVolumeClaim,持久卷声明)**——"我要一块存储"。应用开发者写的,声明**需求**:多大、什么访问模式(读写 / 只读、单节点 / 多节点)、要哪个档次的存储(StorageClass)。**PVC 不描述后端细节。**
- **PV(PersistentVolume,持久卷)**——"集群里有一块存储"。集群范围的资源(不属于任何 namespace),描述**供给**:这块存储的容量、访问模式、后端类型和参数(NFS 地址、Ceph monitor、云盘 ID)、回收策略。**PV 不描述谁在用。**

撮合员就是 k8s 控制制器里的 **PV 控制器(persistentvolume controller)**,它盯着 PVC 和 PV 两本台账,把匹配的绑定到一起。下面我们就拆这三层抽象。

---

## 三、三层抽象:PVC / PV / StorageClass

这是本章的技术核心。我们一层层来,每一层都先问"为什么还需要再抽象一层"。

### 第一层:PV —— 集群里"已经存在"的一块存储

PV 是最先把"存储"从 Pod spec 里剥离出来的对象。它描述的是:**集群里此刻真实存在的一块持久化存储**。

一个 PV 长这样(简化):

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: pv-nfs-001
spec:
  capacity:
    storage: 50Gi
  accessModes: ["ReadWriteOnce"]    # 单节点读写
  persistentVolumeReclaimPolicy: Retain   # 回收策略:删 PVC 后 PV 保留
  nfs:
    server: nfs.example.com
    path: /export/data
```

几个关键字段:

- **`capacity`**:这块存储多大。注意 PV 自己声明,撮合时按这个匹配 PVC 的需求。
- **`accessModes`(访问模式)**:这块存储支持怎么被访问。k8s 定义了三种:
  - `ReadWriteOnce`(RWO):**单个节点**读写。块存储(EBS、Ceph RBD)大多是这样,因为块设备不能并发挂载写。
  - `ReadOnlyMany`(ROX):多个节点只读。
  - `ReadWriteMany`(RWX):多个节点读写。NFS、CephFS 这类共享文件系统才支持,块存储做不到。
- **`persistentVolumeReclaimPolicy`(回收策略)**:绑定的 PVC 被删后,这块 PV 怎么处理:
  - `Retain`:保留(数据还在,人工清理)。
  - `Delete`:删除(连同后端的真实卷一起删)。
  - `Recycle`:(已废弃,基本不用)。
- **后端类型(`nfs` / `cephfs` / `csi` / ...)**:这块存储到底是什么。**这个字段就是给"存储管理员"看的——他知道这块 PV 背后是哪台 NFS。**

PV 有个 **phase(阶段)** 状态机:`Available`(没人用)→ `Bound`(绑定了某个 PVC)→ `Released`(绑过的 PVC 删了,但数据还在,还没被回收)→ `Failed`(出问题)。这个状态机是 controller 撮合的依据。

### 第二层:PVC —— 应用方"我要一块这样的存储"

PV 把存储剥离出来了,但应用开发者**不应该直接去挑哪个 PV**——那等于又把"用的人"和"给的人"焊上了。PVC 就是给应用方用的:**声明需求,不指定具体哪一块**。

一个 PVC 长这样:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: mysql-data
  namespace: prod
spec:
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: 50Gi
  storageClassName: standard        # 要哪个档次的存储(下一节讲)
```

注意几件事:

- **PVC 生活在某个 namespace 里**(因为它属于某个应用),而 **PV 是集群范围的**(没有 namespace,因为存储是全局资源)。这个不对称是有意的:需求是应用级别的(有命名空间),供给是集群级别的(全局台账)。
- PVC **不写后端类型**(`nfs`、`cephfs` 都不写)。它只说"我要 50GB、RWO、standard 这个档次的"。
- 应用在 Pod spec 里不再写 `volumes: [{name: ..., nfs: ...}]`,而是写 `volumes: [{name: ..., persistentVolumeClaim: {claimName: mysql-data}}]`——**挂的是 PVC,不是 PV**。

然后 Pod 拿着这个 PVC,k8s 的 PV 控制器去撮合:在所有 `Available` 的 PV 里找一个"容量够、访问模式兼容、StorageClass 匹配"的,把它和这个 PVC bind 起来。bind 之后,PVC 的 `spec.volumeName` 指向那个 PV,PV 的 phase 变成 `Bound`,两边都记下对方的引用。

> **绑定是双向的**:PV 的 `spec.claimRef` 指回 PVC,PVC 的 `spec.volumeName` 指向 PV。这就像两张单子互相盖了章——撮合完成后,任何一方拿着自己的单子都能找到对方。

### 静态供给:管理员先造好 PV,等着 PVC 来匹配

上面这种"管理员提前手动造好一堆 PV,等 PVC 来撮合"的模式,叫 **静态供给(static provisioning)**。它的逻辑是:

1. 存储管理员手工创建一批 PV(或在云控制台买好磁盘,然后导入成 PV)。
2. 应用开发者创建 PVC。
3. PV 控制器 watch 到新 PVC,在所有 `Available` 的 PV 里找最合适的(大小够、访问模式匹配、StorageClass 对得上),bind 上。

这够用,但有个明显短板:**管理员得提前猜"会有多少 PVC、要多大"**。猜少了,PVC 一直 Pending(没合适的 PV);猜多了,买一堆没人用的磁盘,白花钱。在一个动态的、按需的云环境里,这种"先备货后等客"的模式太笨重。

### 第三层:StorageClass —— 按需自动造 PV 的模板

于是有了第三层抽象,**StorageClass(存储类)**。它不是"已经存在的存储",而是"**怎么造一块存储**的模板"。

一个 StorageClass 长这样:

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: standard
provisioner: driver.csi.example.com    # 谁来造(CSI 驱动的名字)
parameters:                            # 造的时候传什么参数
  type: gp3
  fsType: ext4
reclaimPolicy: Delete                  # 造出来的 PV 默认回收策略
volumeBindingMode: WaitForFirstConsumer   # 绑定时机
allowVolumeExpansion: true             # 允许扩容
```

关键字段:

- **`provisioner`(供给者)**:谁负责真的去造这块存储。在现代 k8s 里,这几乎都是某个 **CSI 驱动**的名字(下一节细讲)。StorageClass 通过这个字段告诉系统:"造这种存储,去找这个驱动"。
- **`parameters`**:造的时候传给驱动的参数(磁盘类型、文件系统、冗余级别……)。这些参数完全是驱动自定义的,k8s 本身看不懂,只是透传。
- **`reclaimPolicy`**:动态造出来的 PV,删 PVC 时怎么处理(默认 `Delete`——因为这块卷是按需造的,用完就扔)。
- **`volumeBindingMode`**:什么时候绑定/造卷。两种:
  - `Immediate`:PVC 一创建就立刻造卷、立刻绑定。问题是——此刻还不知道 Pod 会调度到哪个节点,可能造出来的卷和 Pod 不在同一个可用区,Pod 调度失败。
  - `WaitForFirstConsumer`(**生产推荐**):**等第一个用这个 PVC 的 Pod 被调度了、确定了节点,再根据节点所在的可用区/拓扑去造卷**。这避免了"造好的卷和 Pod 跨区"的问题,对拓扑敏感的存储(云盘、本地盘)至关重要。

### 动态供给:PVC + StorageClass 触发自动造 PV

有了 StorageClass,**动态供给(dynamic provisioning)** 就能跑起来了,这是现代 k8s 的主流模式:

1. 应用开发者创建 PVC,`storageClassName: standard`。
2. PV 控制器 watch 到这个 PVC,发现没现成的 PV 能匹配,但 PVC 指定了 StorageClass。
3. 控制器(通过 CSI 驱动)**真的去后端造一块存储**(比如在 AWS 调 API 买一块 EBS),造好后自动生成一个对应的 PV,把这块 PV 和 PVC bind 起来。
4. 应用挂这个 PVC,实际用的就是刚造好的那块存储。

整个过程,应用开发者**完全不知道**这块存储是哪个厂商、什么介质、在哪个可用区——他只要 50GB、RWO。存储管理员也**不用提前备货**——按需造,用完按回收策略自动清理。这就是三层抽象带来的彻底解耦。

> **三层各管一摊**:
> - **PVC**(应用视角):"**我要**什么样的存储。"
> - **PV**(集群视角):"集群里**有**一块什么样的存储。"
> - **StorageClass**(供给模板):"**怎么造**一块这样的存储。"
>
> 静态供给:PVC 找现成 PV;动态供给:PVC + StorageClass 现造一个 PV。两者最终都收敛到"PV 和 PVC bind 上"这一个状态。

### 谁绑定谁、什么时候造:一张状态流转图

把这套机制的状态流转画清楚:

```
   应用方创建 PVC(spec.storageClassName = standard)
                  │
                  ▼
       ┌──────────────────────┐
       │  PV 控制器 reconcile  │  ← watch 到新 PVC
       └──────────────────────┘
                  │
        ┌─────────┴─────────┐
        ▼                   ▼
  有匹配的 Available PV?    没有,但有 StorageClass?
        │                   │
        ▼                   ▼
   静态 bind            动态供给:调用 CSI provisioner
   (PV ←→ PVC)         造好卷 → 生成新 PV → bind
        │                   │
        └─────────┬─────────┘
                  ▼
         PV phase=Bound, PVC phase=Bound
         PVC.spec.volumeName → PV
         PV.spec.claimRef    → PVC
                  │
                  ▼
         Pod 挂这个 PVC → 调度 → kubelet 真正 mount
```

这张图里藏着一个我们要在"关键源码精读"里去核实的细节:**撮合、bind、触发动态供给,全是在 PV 控制器的 reconcile 循环里发生的**。它不是某个一次性的脚本,而是一个持续盯着的 watch-reconcile 闭环——PVC 或 PV 任何一边有变化,都会触发重新 reconcile,直到两边都进入 Bound 状态。

---

## 四、CSI:存储也做成可插拔标准

三层抽象解决了"用的人和给的人解耦",但还剩最后一个问题:**PV 描述了后端是 NFS、Ceph 还是云盘,可真正去后端造卷、挂卷、扩卷的代码,放在哪儿?**

### 不这样会怎样:k8s 被某家存储绑死

最朴素的方案是:**把所有存储后端的驱动代码,直接编译进 k8s 自己**。k8s 早期确实这么干过——你看 PV spec 里那些 `awsElasticBlockStore`、`gcePersistentDisk`、`azureDisk`、`cephRBD`、`glusterfs`……每一个对应一段写在 k8s 源码树里(in-tree)的驱动逻辑。

这个方案叫 **in-tree volume plugin**,它有几个要命的毛病:

- **k8s 被各家存储绑死**。每出一个新厂商,都得给 k8s 提 PR、等 review、等发版——厂商等不起,k8s 维护者也扛不住(几百个厂商,代码爆炸)。
- **k8s 发版和存储驱动发版强耦合**。某个存储驱动修了个 bug,得等 k8s 下个大版本才能上线。而存储驱动本来就是各家厂商自己的事,凭什么卡在 k8s 发版节奏上?
- **安全边界模糊**。各家厂商的驱动代码跑在 k8s 核心进程里,一个驱动的崩溃/漏洞可能拖垮整个控制平面。

这个问题,k8s 在网络(CNI)和运行时(CRI)上都遇到过,答案是同一个:**抽一个标准接口出来,让驱动作为独立进程(插件)跑,k8s 只通过标准接口和它对话**。

### 所以这样设计:CSI——Container Storage Interface

**CSI(Container Storage Interface)** 就是存储这个标准接口。它的定位和 CNI、CRI 完全对等:

| 接口 | 管什么 | 谁实现 | 谁调用 |
|------|--------|--------|--------|
| **CNI** | 网络(IP 分配、路由、策略) | calico / flannel / … | kubelet 在创建 Pod 时调 |
| **CRI** | 运行时(起容器、管镜像) | containerd / cri-o / … | kubelet 调 |
| **CSI** | 存储(造卷、挂卷、扩卷、快照) | 各存储厂商的 CSI 驱动 | k8s 组件 + sidecar 调 |

CSI 是一个 **gRPC 接口规范**,定义了一组存储相关的 RPC,核心有这么几类(简化):

- **Identity 服务**:`GetPluginInfo`、`GetPluginCapabilities`——驱动自报家门、说自己能干啥。
- **Controller 服务**:`CreateVolume`、`DeleteVolume`、`ControllerPublishVolume`(attach)、`ControllerUnpublishVolume`(detach)、`CreateSnapshot`……——这些是**集群级别**的操作,通常在某个控制节点上执行(造一块云盘、把云盘 attach 到某台虚拟机)。
- **Node 服务**:`NodeStageVolume`(全局挂,一次)、`NodePublishVolume`(挂到具体 Pod 路径)、`NodeUnpublishVolume`、`NodeExpandVolume`……——这些是**节点级别**的操作,在每台要挂这个卷的机器上执行(把 attach 过来的块设备格式化、mount 到容器路径)。

有了 CSI,**k8s 自己不再懂任何具体存储后端**。PV spec 里的 `nfs`、`cephRBD` 那些 in-tree 字段被逐步废弃(经过一个叫 "CSI migration" 的过渡期,k8s 内部把它们翻译成 CSI 调用),最终所有的持久化存储都通过 CSI 这一个口子进出。

> **比喻**:航运中心不再自己雇码头工人去各家仓库干活。它定了一套**标准作业流程**(CSI):"造一座仓(CreateVolume)、把仓库的门拉到某艘船的舷窗边(ControllerPublishVolume)、在舷窗上接一根管子通到集装箱(NodePublishVolume)"。每家仓储公司(CSI 驱动)只要派一个能听懂这套流程的工人(独立的 CSI 插件进程)驻场,航运中心按这套流程发指令就行。**流程是标准的,工人是各家自己的。**

### CSI 的 sidecar 架构:把 k8s 的活和驱动的活切开

CSI 驱动是个独立进程,它**不直接懂 k8s**(它只是个 gRPC 服务,不知道 PVC、PV 这些 k8s 概念)。那它怎么和 k8s 配合?答案是 **sidecar 容器**——一组 k8s 官方提供的小容器,跑在 CSI 驱动旁边,负责"翻译":

- **external-provisioner**:watch PVC,发现有动态供给需求时,调 CSI 驱动的 `CreateVolume`。造好后,它(通过 PV 控制器)生成对应的 PV 对象。
- **external-attacher**:watch `VolumeAttachment` 对象,调驱动 `ControllerPublishVolume`/`ControllerUnpublishVolume`。
- **external-resizer**:watch PVC 扩容请求,调 `ControllerExpandVolume`。
- **external-snapshotter**:watch `VolumeSnapshot`,调 `CreateSnapshot`。
- **node-driver-registrar**:跑在每个节点上,把 CSI 驱动注册给 kubelet(kubelet 才知道这个节点上有这么个 CSI 驱动、能调它的 Node 服务)。
- **liveness-probe**:给 kubelet 报驱动是否活着。

这套架构的精妙之处在于:**CSI 驱动本身只懂 gRPC + 存储,完全不懂 k8s**;k8s 那边通过这些 sidecar 把"k8s 的世界(PVC/PV/VolumeAttachment 对象的变化)"翻译成"CSI 的世界(gRPC 调用)"。两边各管一摊,通过标准接口耦合。任何一个存储厂商,只要实现 CSI 这套 gRPC,就能接入 k8s——不需要给 k8s 提一行代码。

### 一个 PV 从造到挂的完整旅程(走 CSI)

把前面所有抽象合起来,看一个动态供给的 PV 从无到有、最终被 Pod 挂上的完整流程:

```
1. 应用创建 PVC(storageClassName=standard,大小 50Gi)
       │
2. PV 控制器 reconcile:发现没现成 PV,触发动态供给
   → 在 PVC 上打 annotation: volume.kubernetes.io/storage-provisioner = <CSI 驱动名>
   → 等待外部 provisioner 干活(控制器自己不造卷!)
       │
3. external-provisioner sidecar watch 到带这个 annotation 的 PVC
   → 调 CSI 驱动的 CreateVolume(参数来自 StorageClass.parameters + PVC 的大小)
   → 驱动在后端真的造了一块卷(比如调云 API 买了块 EBS),返回卷 ID
   → sidecar 用这个卷的信息生成一个 PV 对象,提交给 apiserver
       │
4. PV 控制器看到新 PV,把它和 PVC bind(两边互相指)
       │
5. 调度器把用了这个 PVC 的 Pod 调度到节点 N(volumeBindingMode=WaitForFirstConsumer 在这里起作用)
       │
6. attach-detach 控制器在节点 N 上创建一个 VolumeAttachment 对象
   → external-attacher sidecar watch 到,调 CSI ControllerPublishVolume
   → 驱动把那块卷 attach 到节点 N(对块存储就是把云盘挂到那台虚拟机)
       │
7. kubelet 在节点 N 上调 CSI NodeStageVolume(全局挂一次,格式化块设备、mount 到一个全局目录)
       → 再调 CSI NodePublishVolume(从全局目录 bind mount 到容器内的目标路径)
       │
8. 容器启动,在 /var/lib/mysql 读写,实际穿透到 CSI 驱动 mount 好的那块后端卷
```

这 8 步牵涉了 PV 控制器、调度器、attach-detach 控制器、4 个 sidecar、kubelet、CSI 驱动本身——极其复杂。但每一块都只做自己的事、通过对象/接口和下一块解耦。这种"分层 + 标准接口 + watch-reconcile"的组合,是 k8s 把"持久化存储"这种天生复杂的东西,做到可扩展、可替换、不绑死任何一家的根本手段。

---

## 关键源码精读:PV 控制器的 reconcile

讲完原理,我们去 k8s 源码里看撮合到底怎么发生。整个 PV/PVC 体系的大脑是 **`persistentvolume` controller**,代码在 [kubernetes/kubernetes 的 pkg/controller/volume/persistentvolume/](https://github.com/kubernetes/kubernetes/tree/master/pkg/controller/volume/persistentvolume)。它的骨架是一个标准的 watch-reconcile 循环(第 17 章讲过的范式),我们重点看三个函数:`syncClaim`(撮合 PVC 侧)、`bind`(真正绑定)、`provisionClaim`(触发动态供给)。

### reconcile 的骨架:两个 worker,两条队列

PV 控制器同时盯两类对象(PV 和 PVC),所以它有**两条独立的 work queue**和**两个 worker 循环**,一个处理 volume 事件,一个处理 claim 事件。这在 [pv_controller_base.go](https://github.com/kubernetes/kubernetes/blob/master/pkg/controller/volume/persistentvolume/pv_controller_base.go) 里:

```go
// pkg/controller/volume/persistentvolume/pv_controller_base.go
// (Run 方法节选,启动三个 goroutine:周期 resync + volume worker + claim worker)
func (ctrl *PersistentVolumeController) Run(ctx context.Context) {
    // ... 启动事件广播、等缓存同步 ...
    ctrl.initializeCaches(logger, ctrl.volumeLister, ctrl.claimLister)

    wg.Go(func() { wait.Until(func() { ctrl.resync(ctx) }, ctrl.resyncPeriod, ctx.Done()) })
    wg.Go(func() { wait.UntilWithContext(ctx, ctrl.volumeWorker, time.Second) })
    wg.Go(func() { wait.UntilWithContext(ctx, ctrl.claimWorker, time.Second) })
    <-ctx.Done()
}
```

这段对应第 17 章讲的 controller 标准结构:informer 监听对象变化 → 把变化的事件塞进 work queue → worker 循环从队列里取、调 reconcile。这里 PV 和 PVC 各一条队列,意味着**两个方向的撮合是独立的**:PVC 变化触发 `claimWorker` 去找匹配的 PV;PV 变化(比如新造好的)触发 `volumeWorker` 去看能不能绑给某个 Pending 的 PVC。无论哪边先变,reconcile 都会把系统往"两边 Bound"的期望状态拉。

### syncClaim:撮合的主分发

PVC 那侧的入口是 `syncClaim`,在 [pv_controller.go](https://github.com/kubernetes/kubernetes/blob/master/pkg/controller/volume/persistentvolume/pv_controller.go#L238):

```go
// pkg/controller/volume/persistentvolume/pv_controller.go L238
func (ctrl *PersistentVolumeController) syncClaim(ctx context.Context, claim *v1.PersistentVolumeClaim) error {
    logger := klog.FromContext(ctx)
    logger.V(4).Info("Synchronizing PersistentVolumeClaim", "PVC", klog.KObj(claim), ...)

    // 处理 CSI 迁移相关的 annotation(无关细节,跳过)
    newClaim, err := ctrl.updateClaimMigrationAnnotations(ctx, claim)
    if err != nil { return err }
    claim = newClaim

    if !metav1.HasAnnotation(claim.ObjectMeta, storagehelpers.AnnBindCompleted) {
        return ctrl.syncUnboundClaim(ctx, claim)     // ← 还没绑过,去撮合/造卷
    } else {
        return ctrl.syncBoundClaim(ctx, claim)        // ← 已经绑过,保证 PV 还在、双向引用没断
    }
}
```

注意它怎么分流的:用一个 annotation(`pv.kubernetes.io/bind-completed`)标记"这个 PVC 是不是已经撮合过了"。没绑过 → `syncUnboundClaim`;绑过 → `syncBoundClaim`(主要做一致性检查:绑过的 PV 还在不在、双向引用有没有被破坏,坏了就尝试修复)。

真正的撮合逻辑在 `syncUnboundClaim`,这是整章最值得读的函数:

```go
// pkg/controller/volume/persistentvolume/pv_controller.go L332
func (ctrl *PersistentVolumeController) syncUnboundClaim(ctx context.Context, claim *v1.PersistentVolumeClaim) error {
    logger := klog.FromContext(ctx)
    if claim.Spec.VolumeName == "" {
        // 用户没指定具体哪块 PV —— "给我随便一块够格的就行"
        delayBinding, err := storagehelpers.IsDelayBindingMode(claim, ctrl.classLister)  // WaitForFirstConsumer?
        if err != nil { return err }

        volume, err := ctrl.volumes.findBestMatchForClaim(claim, delayBinding)   // ← 在所有 Available PV 里找最合适的
        if err != nil { return ... }

        if volume == nil {
            // 现成的 PV 没找到
            ctrl.assignDefaultStorageClass(ctx, claim)   // 没指定 storageClass 就补一个默认的
            // ... 根据情况:要么等延迟绑定、要么触发动态供给、要么报"没卷可用"
            switch {
            case delayBinding && !storagehelpers.IsDelayBindingProvisioning(claim):
                // WaitForFirstConsumer 且 Pod 还没调度 → 等
            case storagehelpers.GetPersistentVolumeClaimClass(claim) != "":
                if err = ctrl.provisionClaim(ctx, claim); err != nil { return err }   // ← 动态供给!
                return nil
            default:
                ctrl.eventRecorder.Event(claim, v1.EventTypeNormal, events.FailedBinding,
                    "no persistent volumes available for this claim and no storage class is set")
            }
            // 标记 Pending,下个 reconcile 周期再试
            ctrl.updateClaimStatus(ctx, claim, v1.ClaimPending, nil)
            return nil
        } else {
            // 找到匹配的 PV 了 → bind
            if err = ctrl.bind(ctx, volume, claim); err != nil { return err }
            return nil
        }
    } else {
        // 用户指定了具体 PV(claim.Spec.VolumeName != "") —— 直接找那块去 bind
        // ... 校验那块 PV 满不满足 claim 的需求,满足就 bind,不满足报错 ...
    }
}
```

这个函数读完,你应该能看清前面那张状态图的代码实现。三个分支把"撮合"的所有可能性都覆盖了:

1. **有现成的、匹配的 PV**(`volume != nil`)→ 直接 `bind`。
2. **没现成的,但有 StorageClass** → `provisionClaim` 触发动态供给。
3. **既没现成的、也没 StorageClass**(或者 WaitForFirstConsumer 还在等 Pod 调度)→ 标记 Pending,等下一轮。

特别注意 `findBestMatchForClaim` 那个调用——它就是在所有 `Available` 的 PV 里,按"容量够不够、accessModes 兼不兼容、storageClassName 对不对、volumeMode 一致不一致"这几个维度,挑一块最合适的。挑选的规则在 [checkVolumeSatisfyClaim](https://github.com/kubernetes/kubernetes/blob/master/pkg/controller/volume/persistentvolume/pv_controller.go#L260) 里:

```go
// pkg/controller/volume/persistentvolume/pv_controller.go L260
func checkVolumeSatisfyClaim(volume *v1.PersistentVolume, claim *v1.PersistentVolumeClaim) error {
    requestedQty := claim.Spec.Resources.Requests[v1.ResourceName(v1.ResourceStorage)]
    requestedSize := requestedQty.Value()
    if volume.ObjectMeta.DeletionTimestamp != nil {
        return fmt.Errorf("the volume is marked for deletion %q", volume.Name)
    }
    volumeQty := volume.Spec.Capacity[v1.ResourceStorage]
    if volumeQty.Value() < requestedSize {
        return fmt.Errorf("requested PV is too small")         // 容量不够
    }
    if storagehelpers.GetPersistentVolumeClass(volume) != storagehelpers.GetPersistentVolumeClaimClass(claim) {
        return fmt.Errorf("storageClassName does not match")    // 类不匹配
    }
    if storagehelpers.CheckVolumeModeMismatches(&claim.Spec, &volume.Spec) {
        return fmt.Errorf("incompatible volumeMode")            // 模式(Filesystem/Block)不一致
    }
    if !storagehelpers.CheckAccessModes(claim, volume) {
        return fmt.Errorf("incompatible accessMode")            // 访问模式不兼容
    }
    return nil
}
```

这正是前面讲的"撮合规则"的源码化身:容量、类、模式、访问模式四个维度,任何一个不满足就拒绝 bind。撮合不是"随便配",而是"严格校验"。

### bind:双向盖章,四步原子化

撮合上了,就进 `bind`。这是个看似简单、其实设计很讲究的函数,在 [pv_controller.go:1095](https://github.com/kubernetes/kubernetes/blob/master/pkg/controller/volume/persistentvolume/pv_controller.go#L1095):

```go
// pkg/controller/volume/persistentvolume/pv_controller.go L1095
func (ctrl *PersistentVolumeController) bind(ctx context.Context, volume *v1.PersistentVolume, claim *v1.PersistentVolumeClaim) error {
    var updatedClaim *v1.PersistentVolumeClaim
    var updatedVolume *v1.PersistentVolume

    if updatedVolume, err = ctrl.bindVolumeToClaim(ctx, volume, claim); err != nil { return err }     // ① PV.Spec.ClaimRef → PVC
    volume = updatedVolume
    if updatedVolume, err = ctrl.updateVolumePhase(ctx, volume, v1.VolumeBound, ""); err != nil { return err }  // ② PV phase=Bound
    volume = updatedVolume
    if updatedClaim, err = ctrl.bindClaimToVolume(ctx, claim, volume); err != nil { return err }      // ③ PVC.Spec.VolumeName → PV
    claim = updatedClaim
    if updatedClaim, err = ctrl.updateClaimStatus(ctx, claim, v1.ClaimBound, volume); err != nil { return err }  // ④ PVC phase=Bound
    return nil
}
```

读这段要抓住一个设计要点:**bind 不是一次原子操作,是四次独立的 API 调用**(两次改 PV、两次改 PVC)。这看起来"不优雅"——为什么不一次性原子地绑?因为 k8s 的 API 模型里,PV 和 PVC 是两个独立的对象,apiserver 不支持"跨对象的事务"。所以 bind 必然是一个多步过程,**中间任何一步都可能失败**(网络抖动、别人改了对象、版本冲突)。

控制器怎么应对这种"非原子的多步 bind"?答案是第 17 章讲过的:**reconcile 的幂等性**。`bind` 每一步都设计成可重入的——`bindVolumeToClaim` 里先判断"这个 PV 是不是已经指向这个 claim 了"(看 `AnnBoundByController` annotation),已经指了就跳过;`updateVolumePhase` 如果 phase 已经是 Bound 也跳过。所以**哪怕 bind 在第 ② 步崩了,下次 reconcile 会从第 ③ 步接着干**,最终系统收敛到"两边都 Bound"。这就是声明式 + 幂等 reconcile 的威力:**用"可重入的多步"模拟出"看起来像原子"的最终一致性**。

### provisionClaim:控制器其实不造卷

最后看动态供给的入口 `provisionClaim`,这段代码纠正了一个常见的误解:

```go
// pkg/controller/volume/persistentvolume/pv_controller.go L1561
func (ctrl *PersistentVolumeController) provisionClaim(ctx context.Context, claim *v1.PersistentVolumeClaim) error {
    if !ctrl.enableDynamicProvisioning { return nil }
    plugin, storageClass, err := ctrl.findProvisionablePlugin(claim)   // 找 in-tree 插件 或 标记走外部 CSI
    if err != nil { ... return nil }
    ctrl.scheduleOperation(logger, opName, func() error {
        if plugin == nil {
            _, err = ctrl.provisionClaimOperationExternal(ctx, claim, storageClass)   // ← 现代 CSI 走这条!
        } else {
            _, err = ctrl.provisionClaimOperation(ctx, claim, plugin, storageClass)    // in-tree 老插件走这条(已废弃)
        }
        return err
    })
    return nil
}
```

关键的真相在 `provisionClaimOperationExternal`,在 [pv_controller.go:1807](https://github.com/kubernetes/kubernetes/blob/master/pkg/controller/volume/persistentvolume/pv_controller.go#L1807):

```go
// pkg/controller/volume/persistentvolume/pv_controller.go L1807
func (ctrl *PersistentVolumeController) provisionClaimOperationExternal(
    ctx context.Context, claim *v1.PersistentVolumeClaim, storageClass *storage.StorageClass) (string, error) {
    provisionerName := storageClass.Provisioner
    // ... CSI 迁移相关 ...
    // 关键:只是给 PVC 打一个 annotation,告诉外部 provisioner "该你干活了"
    newClaim, err := ctrl.setClaimProvisioner(ctx, claim, provisionerName)
    if err != nil { ... }
    claim = newClaim
    msg := fmt.Sprintf("Waiting for a volume to be created either by the external provisioner '%s' ...", provisionerName)
    ctrl.eventRecorder.Event(claim, v1.EventTypeNormal, events.ExternalProvisioning, msg)
    // 然后就返回了!不造卷!
    return provisionerName, nil
}
```

读到这里你会发现一件反直觉的事:**PV 控制器自己根本不造卷**。在 CSI 时代,它干的事只是:

1. 看 PVC 指定了哪个 StorageClass,拿到 `provisioner` 名字。
2. 在 PVC 上打一个 annotation:`volume.kubernetes.io/storage-provisioner = <provisioner 名字>`。
3. 发个 "ExternalProvisioning" 事件,然后就返回了。

**真正造卷的是那个独立的 `external-provisioner` sidecar**——它 watch 到带这个 annotation 的 PVC,去调 CSI 驱动的 `CreateVolume`,造好后**自己生成一个 PV 对象**提交给 apiserver。然后 PV 控制器在下一轮 reconcile 里看到这个新 PV,才把它和 PVC bind 起来。

这个分工是 CSI 架构的精髓:**k8s 内核只负责"撮合"和"记账",造卷/挂卷/扩卷这些和具体后端打交道的活,全部外包给独立的 CSI 驱动 + sidecar**。k8s 源码树里没有任何一家厂商的存储驱动代码——它只认 CSI 这个 gRPC 标准。这就是为什么 k8s 能支持几百种存储后端,而自己的代码库不爆炸。

> 这个 `setClaimProvisioner` 打的 annotation,是 PV 控制器和 external-provisioner sidecar 之间的"暗号"。控制器不直接调 sidecar,sidecar 也不直接调控制器——它们通过 **PVC 这个共享对象上的 annotation** 异步通信。这正是第 17 章讲的"声明式系统里,组件之间通过共享状态协调,而不是直接互调"的又一个活生生的例子。

---

## 章末小结

### 用航运比喻回顾本章

回到那片港口。前 20 章我们讲了怎么造集装箱、怎么装上船、怎么调度,但有一件事一直悬着:**集装箱自带的货架(upper)是跟着船走的,船沉货没**。这一章我们给"会沉的货"找了一个不沉的地方。

- **Volume**——在集装箱之外**多放一块共享托盘**,Pod 里的几个容器都能往上面放东西。但这是"集装箱级"的扩展,Pod 没了它也没了,还解决不了跨 Pod 跨节点的长期存活。
- **PV(岸上仓库)**——**码头边那座独立的仓库**,不跟任何一艘船走。航运中心有一本全局台账,登记着"现在岸上有这么些仓库,各多大、什么条件"。
- **PVC(要仓库的单子)**——贸易商填的需求单:"我要一座 100 平米、恒温、能在 A/B 港之间调度的仓库"。**不指定具体哪一座**,只描述需求。
- **PV 控制器(撮合员)**——盯着台账和需求单两本本子,按"够大、条件匹配"把合适的仓库和需求撮合到一起,在两张单子上**互相盖章(bind)**。
- **StorageClass(造仓模板)**——台账里没有合适的现成仓库?拿这张模板去**现造一座**:谁去造(provisioner)、造的时候带什么参数、造好了用完是拆还是留。
- **CSI(标准作业流程 + 各家驻场工人)**——航运中心不自己雇工人,定了一套标准流程(CreateVolume / PublishVolume…),各家仓储公司派自己的工人(CSI 驱动 + sidecar)驻场,按流程接单干活。**流程标准,工人各家自己的。**

这套设计的本质是**层层解耦**:用的人不知道后端、给的人不知道用户、k8s 不知道厂商——每一层只通过声明(对象 + annotation)和标准接口(CSI)和邻层耦合。这让"持久化存储"这种天生极其复杂的东西,在几百节点、几十后端的大集群里仍然可扩展、可替换、不被任何一方绑架。

### 本章在全书的二分法里站哪边

全书的二分法是:**打包隔离 vs 调度编排**。

这一章,**两边都沾**,但更偏**编排**这一侧:

- "容器文件系统是临时的、要持久化"——这是**打包隔离**(第 1 篇)留下的一个副产品:overlayfs 为了让镜像不被污染,把所有改动都丢进临时的 upper。本章是来给它"打补丁"的:对**无状态**应用,upper 的临时性是优点;对**有状态**应用,我们用 Volume/PV 在隔离世界之外**凿一个通向持久存储的口子**。
- "PV/PVC/StorageClass 的撮合、CSI 的可插拔"——这是**调度编排**(第 5~7 篇)的标准套路:声明式 API + reconcile 闭环 + 分层标准接口。本章的 PV 控制器,就是第 17 章那个 watch-reconcile 范式在存储领域的具体实现;CSI 之于存储,正是 CNI 之于网络(第 13 章)、CRI 之于运行时(第 10 章)的同构翻版。

从内核视角回扣一句:Volume 最终落到节点上,本质还是 **mount**——`NodePublishVolume` 那一步,kubelet(经 CSI 驱动)做的事,和第 4 章讲的 `pivot_root`、第 5 章讲的 overlay mount,是同一个 `mount(2)` 系统调用家族。**容器存储的"持久化",底子里仍是内核挂载机制**——k8s/Docker 的全部精妙,在于怎么把"在哪个节点、挂哪块后端、什么时候挂、挂失败怎么办"这套复杂状态机,编排成上面那 8 步旅程。

### 五个"为什么"清单

如果你只能从这一章带走五件事:

1. **为什么容器文件系统是临时的**:回扣第 5 章——所有改动落在 overlay 的 upper 层,upper 跟着容器走,容器一删就没了。这是无状态应用的优点、有状态应用的灾难。数据要持久,得用 Volume 把存储接到"容器之外"。
2. **为什么不直接挂个目录**:在一个有几百节点、几十种后端的集群里,直接在 Pod spec 里写后端类型和参数,会把"用存储的人"和"给存储的人"焊死——应用开发者被迫懂存储后端,存储团队没法统一管台账,换厂商要改所有 Pod。PVC/PV 的解耦就是为了切开这个焊点。
3. **三层抽象各管什么**:PVC(应用视角,"我要")/ PV(集群视角,"我有")/ StorageClass(供给模板,"怎么造")。静态供给是 PVC 找现成 PV,动态供给是 PVC + StorageClass 现造一个 PV;两者都收敛到"PV 和 PVC bind 上"这一个状态。
4. **bind 为什么是多步而非原子**:因为 PV 和 PVC 是两个独立对象,apiserver 不支持跨对象事务。bind 是四次独立 API 调用,靠 reconcile 的幂等性(每步可重入)保证最终两边都 Bound——失败重试从断点接着干。这是声明式系统"用可重入多步模拟原子"的标准手法。
5. **CSI 为什么必须存在**:in-tree 插件会把 k8s 和各家存储绑死(发版耦合、代码爆炸、安全边界模糊)。CSI 是个 gRPC 标准接口,让存储驱动作为独立进程跑,k8s 通过 sidecar 翻译 PVC/PV 的变化成 CSI 调用。**PV 控制器自己不造卷——它只打 annotation,真正的造卷是 external-provisioner sidecar 调 CSI 驱动干的。**

### 想继续深入,该往哪钻

- **看 PV 控制器的完整 reconcile**:[kubernetes/kubernetes 的 pkg/controller/volume/persistentvolume/pv_controller.go](https://github.com/kubernetes/kubernetes/blob/master/pkg/controller/volume/persistentvolume/pv_controller.go)。重点读 `syncClaim`(L238)、`syncUnboundClaim`(L332)、`bind`(L1095)、`provisionClaimOperationExternal`(L1807)。配套读 [pv_controller_base.go](https://github.com/kubernetes/kubernetes/blob/master/pkg/controller/volume/persistentvolume/pv_controller_base.go) 看双队列 + 双 worker 的骨架。
- **看 attach-detach 控制器**:[pkg/controller/volume/attachadaptch/](https://github.com/kubernetes/kubernetes/tree/master/pkg/controller/volume/attachdetach)。它负责把已 bind 的卷 attach 到具体节点,是动态供给流程第 6 步的主角。
- **看 CSI 规范本身**:[container-storage-interface/spec](https://github.com/container-storage-interface/spec)。三个服务(Identity / Controller / Node)的 RPC 定义全在那儿。读完你会理解为什么 CSI 是个"接口"而不是个"实现"。
- **看一个真实的 CSI 驱动 + sidecar**:[kubernetes-csi/external-provisioner](https://github.com/kubernetes-csi/external-provisioner)(watch PVC 调 CreateVolume)、[kubernetes-csi/external-attacher](https://github.com/kubernetes-csi/external-attacher)(watch VolumeAttachment 调 ControllerPublishVolume)。看它们怎么把"k8s 世界"翻译成"CSI 世界"。
- **亲手玩一下动态供给**:用 kind/minikube 起一个本地集群(自带一个 `standard` StorageClass 和 mock CSI 驱动),写一个不指定 PV 的 PVC,`kubectl get pvc` 看它从 Pending 变 Bound,`kubectl get pv` 看自动冒出来的 PV,`kubectl describe pvc` 看 Events 里那条 "ExternalProvisioning"——你会亲眼看到上面那 8 步旅程的真实痕迹。

---

> 有了这一章,数据这条线终于落了地——会沉的货有了不沉的仓库。但还有一个问题没解决:我们让容器跑了起来、给它挂了持久存储、让它和别的容器通信,这一切都默认容器是个"老实人"。可**容器默认常常以 root 跑**,在一个共享内核的环境里,这有多危险?容器里那个 root,是真 root 还是假 root?如果它通过某个内核漏洞或配置失误"逃"出了 namespace,会不会把宿主机也搞垮?下一章我们就来收紧这道口子——翻开 **第 22 章 · 安全:Seccomp / AppArmor / capabilities / 容器逃逸**。
