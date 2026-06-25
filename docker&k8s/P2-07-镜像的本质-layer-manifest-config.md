# 第 7 章 · 镜像的本质:layer + manifest + config

> **前置**:你需要先读过[第 6 章《动手:不用 docker,手搓一个容器》](P1-06-动手-不用docker手搓一个容器.md)和[第 5 章《联合文件系统 overlayfs:分层镜像为什么这么设计》](P1-05-联合文件系统overlayfs-分层镜像为什么这么设计.md)。第 6 章结尾留了一个钩子——我们手搓的容器,用的是**本地一个解压好的 busybox 目录当根**;可真正的 docker 镜像,是怎么打包、怎么从仓库拉到本地、怎么变成那个目录的?第 5 章讲了 overlayfs 的"层"(lower/upper/merged),但那只回答了"层怎么叠起来"。本章要回答的是另一面:**这些层,在镜像文件里到底长什么样、叫什么名字、怎么被引用、怎么被搬运**。两章合起来,才是镜像这枚硬币的两面。

> **核心问题**:一个 image 文件,拆开到底由哪几部分组成?为什么是这样切分的?
>
> 第 6 章我们用 `~/mini-container/rootfs` 这个本地目录当根,容器就跑起来了。可 docker 不会让你手动准备目录——它让你 `docker pull nginx`,一个命令就把一个完整的、标准化的"集装箱"拉到本地,自动解压、自动叠成 rootfs、自动开容器。这个"集装箱"到底装了什么?它不是一个 tar 包那么简单。这一章,我们把一个 image 拆开看:**它由三部分组成——一堆 layer(文件 diff)、一个 manifest(层清单)、一个 config(启动参数)。为什么偏偏是这三样、为什么不能再合并、为什么不能再拆**——这就是本章要钉死的事。
>
> **读完本章你会明白**:
> - 一个 image 拆开,是 **layer(层)+ manifest(层清单/索引)+ config(启动参数)** 三件套。三者各管一摊,缺一不可,但也不能合并——**合并了就会丢掉"去重""防篡改""一份层多份配置"这些好处**。
> - 为什么 **config 要和 layer 分开**:同样的层,换一份启动配置就是另一个镜像(`nginx` 改成 `nginx-debug`,层几乎一样,只是 entrypoint 不同);改配置不该重传层。
> - **layer 用内容 SHA256 寻址(digest)** 为什么能同时做到**天然去重**和**天然防篡改**——一个 hash 同时解决两个看似无关的工程问题。
> - **manifest 和 index(manifest list)** 各管什么:manifest 是"单平台镜像的层清单",index 是"多平台镜像的清单的清单"——后者让 `docker pull nginx` 一个命令同时支持 amd64/arm64/ppc64le。
> - 把第 5 章 overlayfs 的"层"和本章的"镜像 layer"打通:**镜像拉下来后,每个 layer 解压成一个 overlayfs 的 lower 目录,叠起来挂成 rootfs**——两章讲的"层"是同一个东西的两面。

> **逃生阀**:如果 JSON 和 hash 读得头大,你只需要记住三件事——① 镜像 = layer(文件 diff)+ manifest(层清单)+ config(启动参数);② 每一层和每份文件都用内容的 SHA256 当名字,内容一样名字就一样,天然去重 + 天然防篡改;③ config 和 layer 分开存,是为了"改配置不重传层"。这三句撑起整章。

---

## 章首·手搓的 rootfs 和真正的镜像,差在哪

第 6 章我们手搓容器时,做了一件很"原始"的事:

```bash
# 从网上下一个 busybox.tar.xz,解压到本地一个目录,拿这个目录当 rootfs
curl -L -o busybox.tar.xz https://busybox.net/.../busybox.tar.xz
mkdir -p rootfs/bin && tar -xf busybox.tar.xz -C rootfs/bin
```

这个 `rootfs/` 目录,就是我们手搓容器的"地基板"。它工作得很好——`pivot_root` 换根、`exec /bin/sh`,容器就跑起来了。

但你只要往真实世界看一眼,就会发现 docker 的玩法完全不一样:

```bash
docker pull nginx          # 一个命令,从 docker.io 拉一个"标准集装箱"
docker run nginx           # 自动起一个容器
```

这里没有"手动 curl + tar 解压"。`docker pull nginx` 这一行,背后发生了什么?**docker 从一个叫 registry(镜像仓库)的远程服务器,拉回来一组标准格式的文件,自动组装成 rootfs,然后开容器。**

问题来了:

1. **拉回来的到底是什么?** 是一个 tar 包?还是一个目录?还是别的什么?
2. **为什么不用 tar 包?** busybox 的 `busybox.tar.xz` 不就是个 tar 包吗,docker 为什么不直接用?
3. **nginx 几百 MB,改一行配置再 push,难道每次都重传几百 MB?** 第 5 章讲过 overlayfs 怎么省空间,但那是在**本地**叠层。镜像在**网络传输**时,怎么复用层?

答案就藏在本章标题里:**镜像不是一整块 tar,它是 layer + manifest + config 三件套,靠内容寻址串起来。** 我们一层一层拆。

---

## 一、镜像不是 tar 包:先看清"货物清单"

先把"镜像是个什么东西"这个最根本的认知立住。讲为什么之前,先看它长什么样。

### 一个 image,拆开是三件套

把 `docker pull nginx` 拉下来的东西摊在桌上,你会看到**三种东西**:

1. **一堆 layer(层)**:每一层是一个 `.tar.gz`(或者 `.tar.zstd`)压缩包,**里面是一组文件的 diff**——这一层相对上一层,新增了什么、改了什么。注意,不是一整个文件系统,是**增量**。nginx 镜像可能有 5~7 层:最底下是 Debian/Alpine 基础系统,往上叠 nginx 的依赖库、nginx 二进制、配置文件……
2. **一份 manifest(清单)**:一个很小的 JSON 文件,**列出这个镜像由哪几层组成**——每一层的 mediaType(是什么格式)、size(多大)、digest(内容的 SHA256,下面细讲)。manifest 就是"这箱货的装箱单"。
3. **一份 config(配置)**:也是一个 JSON 文件,记录**怎么启动这个镜像**——entrypoint(入口命令,比如 `/docker-entrypoint.sh`)、cmd(默认参数)、env(环境变量)、user(以什么用户跑)、workingdir(工作目录)、暴露的端口……config 就是"这箱货的说明书"。

这三样合起来,才是一个完整的 image。三者**互相引用**——manifest 引用 config 和所有 layer,config 自己不引用 layer 的 blob 但记录层的 diffID(下面讲)。

> **比喻**:回到航运。一个**标准集装箱**(image),不是"一坨货塞进铁皮箱"那么粗暴。它配齐三样文书:
> - **货物本身**(layer):一层一层叠的托盘,每块托盘是一组货物 diff。
> - **装箱单 / 提单**(manifest):这张单子写明"本箱共 5 块托盘,第 1 块托盘编号 sha256:xxx、重 30MB,第 2 块……"——照单点货,一块不少。
> - **货物说明书**(config):这张单子写"本箱货物通电后应启动 nginx,环境变量 PORT=80,以 nginx 用户跑"——码头工人(运行时)照这份说明给集装箱通电。
>
> **三样文书缺一不可**:光有货,不知道怎么启动;光有说明书,没有货;有货有说明书但没有装箱单,货都拼不起来(不知道哪块托盘在下、哪块在上)。三样文书分开存,各有各的用处——这就是本章后面要讲的"为什么分开"。

### 三样东西长什么样:真实的 JSON

口说无凭,看一个真实的 manifest 长什么样。下面这份是 OCI/Docker Schema 2 manifest 的官方示例([CNCF Distribution 规范](https://distribution.github.io/distribution/spec/manifest-v2-2/)):

```json
{
  "schemaVersion": 2,
  "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
  "config": {
    "mediaType": "application/vnd.docker.container.image.v1+json",
    "digest": "sha256:b5b2b2c507a0944348e0303114d8d93aaaa081732b86451d9bce1f432a537bc7",
    "size": 7023
  },
  "layers": [
    {
      "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
      "digest": "sha256:e692418e4cbaf90ca69d05a66403747baa33ee08806650b51fab815ad7fc331f",
      "size": 32654
    },
    {
      "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
      "digest": "sha256:3c3a4604a545cdc127456d94e421cd355bca5b528f4a9c1905b15da2eb4a4c6b",
      "size": 16724
    },
    {
      "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
      "digest": "sha256:ec4b8955958665577945c89419d1af06b5f7636b4ac3da7f12184802ad867736",
      "size": 73109
    }
  ]
}
```

读这份 JSON,逐字段对:

- **`schemaVersion: 2`**:manifest 格式的版本号。这是 Docker 第二版(Schema 2),也是 OCI 标准继承的版本(第 8 章细讲 OCI 标准的来龙去脉)。
- **`mediaType`**:这一整份 manifest 自己的 MIME 类型。registry 靠它区分"你拉的是 manifest,还是 manifest list,还是别的"。
- **`config`**:指向 config 这份 JSON 的指针。注意它不内联 config 的内容,只给 `mediaType + digest + size`——**用 digest 引用**。这意味着 config 也是单独存的一个 blob,manifest 只是"指过去"。
- **`layers`**:一个有序数组,**从底到顶**列出每一层(Schema 2 规范原文:"The layer list is ordered starting from the base image")。每一层同样只给 `mediaType + digest + size`,真正的层数据是另外存的 blob。

**关键观察:manifest 里没有一个字节的"实际内容"**。它全是 digest 引用——指向 config 的 digest、指向每一层的 digest。实际内容(config 的 JSON、每层的 tar.gz)都单独躺在仓库里,靠 digest 寻址。这个设计不是偶然,它是后面所有好处的根基(下一节展开)。

config 长什么样?它本身是个更大的 JSON,字段在 OCI image-spec 里有精确定义。我们待会儿看源码,先看它的关键字段:

```json
// 简化示意:一份 image config JSON 的关键字段(结构来自 OCI image-spec,具体值是示意)
{
  "created": "2024-09-01T10:00:00Z",
  "architecture": "amd64",
  "os": "linux",
  "config": {
    "User": "nginx",
    "Env": ["PATH=/usr/local/sbin:...", "NGINX_VERSION=1.27.1"],
    "Entrypoint": ["/docker-entrypoint.sh"],
    "Cmd": ["nginx", "-g", "daemon off;"],
    "ExposedPorts": {"80/tcp": {}, "443/tcp": {}},
    "WorkingDir": "/"
  },
  "rootfs": {
    "type": "layers",
    "diff_ids": [
      "sha256:<layer0 解压后的 hash>",
      "sha256:<layer1 解压后的 hash>",
      "sha256:<layer2 解压后的 hash>"
    ]
  },
  "history": [
    {"created": "...", "created_by": "/bin/sh -c apt-get update && ..."},
    {"created": "...", "created_by": "/bin/sh -c set -x && ..."}
  ]
}
```

读这份 config,注意它**不存任何文件内容**,只存:

- **启动参数**(`config.Entrypoint/Cmd/Env/User/WorkingDir/ExposedPorts`):docker run 时怎么起容器。
- **平台**(`architecture/os`):这镜像能在什么 CPU/OS 上跑。
- **rootfs.diff_ids**:一组 hash,**记录每一层解压后的内容指纹**。注意——不是 manifest 里那组 digest(那是压缩后的),这里是**解压后**的 diffID。这两个 hash 的区别,是本章最精妙的设计之一,待会儿专讲。
- **history**:每一层是怎么构建出来的(`docker history` 看到的就是它),便于追溯。

> **三个字段的小结**:config 存"怎么跑",manifest 存"由哪些层组成 + 配置在哪",layer 存"文件 diff 本身"。三者分工明确,谁也不抢谁的活。

### 一张图把三件套摆清

```
                    image = nginx:1.27
                          │
            ┌─────────────┼─────────────┐
            ▼             ▼             ▼
       ┌─────────┐   ┌─────────┐   ┌──────────────────────┐
       │ manifest│   │ config  │   │  layer 0..N (tar.gz) │
       │  (JSON) │   │ (JSON)  │   │                      │
       └─────────┘   └─────────┘   └──────────────────────┘
            │             │              │
            │ 引用→        │ ←──被 manifest 引用
            │ config+layers│              │
            │ (by digest)  │              │
            │             │              │
            ▼             │              ▼
     ┌──────────────┐    │       ┌─────────────────┐
     │ config:      │    │       │ rootfs.diff_ids │← config 里这组 hash
     │  digest:...  │────┘       │  = 各层解压后   │  是"解压后"的层指纹,
     │  size:7023   │            │  的 SHA256      │  和 manifest 里
     └──────────────┘            └─────────────────┘  (压缩后的 digest)
            │                                          是两个不同的东西
            ▼
     ┌────────────────────────────┐
     │ layers:                    │
     │  [0] digest:sha256:e692... │──→ blob: sha256:e692...(layer 0 的 tar.gz)
     │      size: 32654           │
     │  [1] digest:sha256:3c3a... │──→ blob: sha256:3c3a...(layer 1 的 tar.gz)
     │      size: 16724           │
     │  [2] digest:sha256:ec4b... │──→ blob: sha256:ec4b...(layer 2 的 tar.gz)
     │      size: 73109           │
     └────────────────────────────┘
```

这张图的核心是:**manifest/config/layer 三者,全部靠 digest(内容的 SHA256)互相引用,没有一处"内联内容"**。整个 image 是一张由 digest 串起来的引用图,真正的字节数据躺在仓库的 blob 存储里。**这个设计,是后面所有好处(去重、防篡改、改配置不传层)的根基。**

那么,我们就要问了:**为什么偏偏要这样切**?下面四节,逐个回答"为什么"。

---

## 二、为什么是 layer + manifest + config 三件套,不是一整块

第一个"为什么":**为什么要把镜像切成三样,而不是一整块 tar?** 第 5 章其实已经从"本地省空间"的角度回答过一次,本章从"打包传输"的角度再回答一次——两边的答案合起来,才完整。

### 不这样会怎样:镜像是一整块 tar 的灾难

假设我们退回最朴素的模型:**一个镜像就是一个完整的文件系统 tar 包**。听起来简单,但和第 5 章讲 overlayfs 时那两个灾难一样,它在两个地方同时崩盘:

**灾难一:传输爆炸。** 你 `docker pull ubuntu:22.04` 拉了一个 80MB 的 tar。然后基于它装了 curl,打成 `myubuntu:with-curl`,push 到仓库。下次另一台机器 `docker pull myubuntu:with-curl`——**如果镜像是一整块,这台机器得重新下整个 80MB,哪怕它和 ubuntu:22.04 只差几个 curl 的二进制(几 MB)**。一天 CI/CD 跑几百次,每次改一行就传几百 MB,带宽和时间全是天文数字。

**灾难二:存不下来。** 一个 registry 上挂 10 万个镜像,每个几百 MB,且 99% 的内容相同(都用同一个 Ubuntu base)。如果每个镜像都存一整份 tar,registry 的存储早就爆了。

### 所以这样设计:分层,每层内容寻址,公共层只存一份

切成分层后,加上"**每层用内容的 SHA256 当名字**"这条规则,两个灾难一起化解:

- **每层是文件 diff,不是完整 fs**。改一行配置只新增一个几 KB 的小层,不重传 base。
- **每层用 SHA256 命名**。`docker pull myubuntu:with-curl` 时,docker 先看 manifest,发现"装了 curl"那层的 digest 是 `sha256:xxx`,本地已经有了(因为同一个 Ubuntu base 谁都拉过),**就跳过这层不下载**。只下本地没有的那几个新层。
- **registry 上,同一份 layer 只存一份**。10 万个镜像都用 Ubuntu base,base 那层在 registry 的 blob 存储里**只占一个 80MB**,被 10 万个 manifest 共同引用。

> **一组数字感受一下**:一台机器第一次 `docker pull nginx`(150MB),第二次 `docker pull myapp`(基于 nginx,改了应用代码,新增 5MB)。
> - **不分层**:第二次下 155MB。
> - **分层 + 内容寻址**:第二次只下新的 5MB 那一层(nginx 的层本地有了,manifest 一比对发现 digest 相同,跳过)。**省了 30 倍带宽**。

### 为什么 config 要和 layer 分开

这是本章最容易被忽略、但极其重要的一个设计。先看"不分开会怎样":

**假设 config 和 layer 焊在一起**(manifest 不指向 config,而是把启动参数直接写在每层的元数据里,或者干脆把 config 塞进某一个 layer):

- 你有一个 `nginx` 镜像,跑得很欢。现在你想出个 `nginx-debug` 版本——**入口命令从 `nginx -g 'daemon off;'` 改成 `nginx-debug -g 'daemon off;'`,其他一切不变**(层完全一样)。
- 如果 config 和 layer 焊死,改入口命令就得**重打所有层**(因为 config 绑在其中一层上),整个镜像要重新 push 一遍——**150MB 全传**,只是为了改一行启动命令。
- 同理,`nginx:1.27` 和 `nginx:1.27-alpine` 如果共用部分层,但 config 不同(一个 ENTRYPOINT 是 nginx、一个加 debug),不分开就没法复用层。

config 分开之后:

- `nginx` 和 `nginx-debug` **用完全相同的一组 layer**,只是 config 不同。push `nginx-debug` 时,**layer 一个都不用传**(digest 都一样,registry 已有),只传那份几 KB 的 config JSON。
- `nginx:1.27` 改一个环境变量(比如 `NGINX_VERSION=1.27.2`),只需要重打 config,layer 全复用。

> **一句话**:config 和 layer 分开,是为了让**"同样的内容只存一份、只传一份"**这条原则,不仅适用于 layer 之间,也适用于 config 和 layer 之间。config 改了,layer 不用动;layer 改了,旧 config 仍能引用新 layer 的 digest(只要层链没变)。
>
> 这背后是一个更深的工程哲学:**把"变的部分"和"不变的部分"分开寻址,变的部分改了,不变的部分不用动**。这个哲学,在第 17 章讲 k8s 的"声明式 + reconcile"时你会再次遇到——它贯穿整个云原生世界。

### 为什么还要 manifest 这一层

有人会问:**config 已经存了 `rootfs.diff_ids`(各层的 hash),为什么还要单独搞个 manifest?config 直接列层不就行?**

因为有三个信息,config 给不了,必须 manifest 给:

1. **层是压缩后的 blob**。config 里的 `diff_ids` 是**解压后**的层指纹(下面专讲为什么)。但网络传输的是**压缩后**的 tar.gz,它的 hash 和解压后的 hash **不一样**。manifest 里的 `layers[].digest` 是压缩后的 hash——**只有 manifest 知道"网上那坨压缩 blob 解压开应该是什么样"**。config 知道的是"解压后的内容指纹",manifest 知道的是"压缩 blob 的指纹",两者各管一段。
2. **mediaType(格式信息)**。manifest 告诉拉取方"这层是 `tar+gzip` 还是 `tar+zstd`"——解压器要按这个选。config 不存这个。
3. **size(字节数)**。manifest 给每层标了 size,拉取时可以先分配空间、可以校验下载完整性。config 不存这个。

> 所以三件套的分工精确到:**manifest 管"网上怎么搬"(压缩格式、字节数、blob digest),config 管"本地怎么跑 + 解压后该怎么验证"(启动参数、解压后 diffID),layer 管"文件内容本身"**。三者各司其职,合起来才能"既能高效搬运,又能正确启动,还能安全验证"。

---

## 三、digest:一个 SHA256 同时解决去重和防篡改

manifest 里反复出现的 `sha256:xxx`,有个专门的名字叫 **digest(摘要)**。这一节专门讲它——**它是整个镜像设计的脊梁**。

### digest 是什么:内容的 SHA256 指纹

digest 就是"把这份内容的字节做 SHA256 哈希,得到的 64 位十六进制串"。比如:

```
sha256:e692418e4cbaf90ca69d05a66403747baa33ee08806650b51fab815ad7fc331f
```

SHA256 有两个关键性质,正好被镜像设计拿来用:

1. **确定性**:同样的字节,hash 一定一样;不同的字节(哪怕差一个 bit),hash 完全不同。
2. **不可逆**:从 hash 反推不出原文(计算上不可行)。

把这两个性质用在镜像上,就同时解决两个工程问题。

### 好处一:天然去重

**同样的内容,hash 一样,名字就一样,自然只存一份。**

- nginx 镜像的 base 层(Ubuntu)解压后是某组字节,它的 digest 是 `sha256:e692...`。
- 我的 myapp 镜像也用 Ubuntu base,它的 base 层字节**一模一样**,digest 也是 `sha256:e692...`。
- registry 看到"两个 manifest 都引用 `sha256:e692...` 这个 blob",**它在 blob 存储里只放一份**,被两个 manifest 共享。
- 本地 containerd 也是同样逻辑:`pull myapp` 时,看到 `sha256:e692...` 这层本地有了(上次 pull nginx 时拉过),跳过不下载。

> 这个去重不需要任何"额外的去重逻辑"——它就是 hash 的天然性质。**用内容当名字,内容相同名字就相同,重不重复一眼可见。** 这是镜像设计"内容寻址(content addressing)"这个名字的来源:名字 = 内容的指纹,而不是名字 = 随便起的标签。

### 好处二:天然防篡改

**改了一个字节,digest 就变了,manifest 里的引用就对不上,立刻露馅。**

设想一个攻击场景:有人入侵了 registry,偷偷把 nginx 镜像某一层的 tar.gz 换成"带了后门的版本"。

- 攻击者改了 blob 的内容,这个 blob 的 SHA256 就变了。但 manifest 里写的还是**原来的 digest**(`sha256:e692...`)。
- 你 `docker pull nginx`,docker 拉到那个被篡改的 blob,先算它的 SHA256,得到 `sha256:ev1l...`(因为内容变了)。**和 manifest 里写的 `sha256:e692...` 对不上,docker 立刻报错拒绝**——`failed to register layer: digest mismatch`。
- 你永远不会运行一个被篡改的层。

> **digest 同时是"身份证"(去重)和"防伪标签"(防篡改)**。一个 64 位的 hash,同时解决两个看似无关的工程问题——这就是为什么整个 OCI 镜像规范是建立在 digest 之上的。任何一处引用,manifest→config、manifest→layer、index→manifest,全是 digest 串起来的链条,**任何一环被改,链条立刻断**。
>
> 这个设计,和 Git 用 commit hash 做版本号、和 Bitcoin 用 hash 串区块,是同一种思想——**用 hash 当名字,既去重又防伪**。容器镜像只是把这个思想用在了"软件打包分发"上。

### 不这样(不用内容寻址)会怎样

假设我们用"序号 + 标签"给层命名(layer-0, layer-1, layer-2……),而不是用 digest:

- **去重要手动实现**:得维护一张"哪些层内容相同"的映射表,跨镜像、跨 registry 同步——复杂、易错、中心化(得有个权威表)。
- **防篡改没招**:有人换了 layer-0 的内容,名字还是 layer-0,谁也看不出来——除非另搞一套校验机制(签名、checksum……),又得维护签名表。
- **跨 registry 引用困难**:我的 registry 上的 layer-0 和你的 registry 上的 layer-0 是不是一个东西?不知道,得对内容——可一旦对内容,就又回到 hash 了。

用 digest,以上问题全部消失。**这就是 OCI image-spec 把 digest 作为根基的原因**——它在 `Descriptor` 结构里把 digest 设成必填字段,就是强制所有引用都走内容寻址。

---

## 四、digest 和 diffID:一个层为什么有两个 hash

讲到这里,有一个细节必须澄清——**一个 layer,有两个 hash**。这是初学者最容易绕晕的点,但它恰恰是镜像设计最精妙的一笔。

### 两个 hash 各是什么

回看前面的 JSON:

- **manifest 里 `layers[].digest`**:是**压缩后** blob 的 SHA256(对那坨 `tar.gz` 字节算 hash)。
- **config 里 `rootfs.diff_ids[]`**:是**解压后**的层的 SHA256(对解压出来的 tar 流算 hash,也就是"这一层文件 diff 的原始内容"的 hash)。

**这两个 hash 不一样**,因为压缩后的字节和解压后的字节不是同一份字节。

### 为什么要有两个

为什么不只用一个?**因为两个 hash 各管一件不同的事**:

**digest(压缩后的 hash)管"传输和存储"**:

- registry 上存的是压缩 blob,它的指纹必须是压缩后的 hash,这样 manifest 引用它才对得上。
- 拉取时校验完整性:`docker pull` 下了一个 tar.gz,算它的 SHA256,和 manifest 里写的 digest 比对——**这是"传输没出错、没被篡改"的保证**。这步用的必须是压缩后的 hash。

**diffID(解压后的 hash)管"解压和挂载"**:

- 本地解压后,要校验"解压出来的内容对不对"。这步用的必须是解压后的 hash——因为压缩可能有多种实现(同内容压缩出不同字节),但解压后的内容是唯一的。
- 更重要的:diffID 用来算 **chainID**——一组层叠起来,该叫什么名字。chainID 是把 diffIDs 递归 hash 出来的(下面看源码),它是**本地 snapshot 的名字**。**chainID 必须用解压后的 hash**,否则同一个镜像用不同压缩算法(gzip / zstd),本地就要存两份 snapshot——但解压后内容一样,本来该共享。用 diffID 算 chainID,保证**不管你怎么压缩,解压后内容一样,本地的 snapshot 就一样,天然复用**。

> **一句话总结**:digest 是"压缩 blob 的身份证"(给网络和 registry 看),diffID 是"解压内容的身份证"(给本地文件系统看)。两个身份证各服务一段流水线,缺一不可。containerd 在拉取时会**用 digest 校验下载完整性,再解压算 diffID,和 config 里记录的 diffID 比对**,两个都对上,这一层才算合格。下面看源码会看到这个校验的真实代码。

### 不这样(只用一个 hash)会怎样

假设我们只用 digest(压缩后):

- 一个镜像今天用 gzip 压,明天用 zstd 压——**digest 完全不同**,但内容是一样的。本地得存两份解压后的 snapshot,空间浪费。
- 防篡改的链条也不完整:digest 只能证明"压缩 blob 没被改",但万一解压器有 bug 解压出了错内容呢?得有 diffID 兜底,校验解压结果。

假设我们只用 diffID(解压后):

- registry 没法直接存——它存的是压缩 blob,得知道每个 blob 的指纹才能被 manifest 引用。解压后的 hash 在 registry 那边用不上(它不解压)。
- 拉取时没法校验"下载完整"——你拿到的是压缩流,得解压才能算 hash,但万一下载到一半就断了,解压直接报错,根本到不了算 hash 那一步。

所以两个 hash **必须同时存在**,各管一段。这是镜像设计在"内容寻址"上做得最细致的一笔。

---

## 五、manifest list(index):一个镜像,多套平台

到这里,我们讲的都是"一个 manifest + 一份 config + 一组 layer"——这是一个**单平台**镜像。但现实里,你 `docker pull nginx` 拉到的,往往是另一个东西:**manifest list(OCI 叫 index)**。

### 不这样会怎样:每个 CPU 架构都得换个镜像名

假设没有 manifest list:

- `docker pull nginx` 只能拉一个平台的镜像,比如 amd64。
- 用 ARM 机器(树莓派、Apple Silicon、AWS Graviton)的人,得 `docker pull nginx:arm64`——**用户得自己知道自己的 CPU 架构,自己挑镜像**。
- 应用作者要发布 `myapp`,得手动给每个架构打一个镜像,起不同的名字(`myapp:amd64`、`myapp:arm64`、`myapp:ppc64le`……),用户文档里写一堆"如果你是 X 架构请 pull Y"。

这在 2026 年(ARM 服务器、混合架构集群遍地)完全不可接受。

### 所以这样设计:manifest list——清单的清单

manifest list(OCI 标准里叫 **index**)是"清单的清单":**它本身不指向 layer,而是指向多个 manifest,每个 manifest 对应一个平台**。

看一份真实的 manifest list([CNCF Distribution 规范示例](https://distribution.github.io/distribution/spec/manifest-v2-2/)):

```json
{
  "schemaVersion": 2,
  "mediaType": "application/vnd.docker.distribution.manifest.list.v2+json",
  "manifests": [
    {
      "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
      "digest": "sha256:e692418e4cbaf90ca69d05a66403747baa33ee08806650b51fab815ad7fc331f",
      "size": 7143,
      "platform": {
        "architecture": "ppc64le",
        "os": "linux"
      }
    },
    {
      "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
      "digest": "sha256:5b0bcabd1ed22e9fb1310cf6c2dec7cdef19f0ad69efa1f392e94a4333501270",
      "size": 7682,
      "platform": {
        "architecture": "amd64",
        "os": "linux"
      }
    }
  ]
}
```

注意它和 manifest 的区别:

- **`manifests` 数组**(不是 `layers`):每个元素指向**另一个 manifest**(digest 引用),并标了那个 manifest 对应的 `platform`(architecture + os,可选 variant/version)。
- 它**完全不引用 layer**,只引用 manifest。每个被引用的 manifest,才是上一节讲的"单平台 manifest"。

### 拉取流程:registry 和 client 配合挑平台

`docker pull nginx` 在有多架构镜像时,流程是:

1. docker 向 registry 请求 `nginx:latest` 的 manifest。
2. registry 返回的不是单平台 manifest,而是 **manifest list**(因为 `nginx:latest` 在 Docker Hub 上是多架构的)。
3. docker 看自己机器的平台(比如 `linux/amd64`),**在 manifest list 的 `manifests` 数组里挑出 platform 匹配的那一项**——拿到对应的子 manifest 的 digest。
4. docker 再向 registry 请求那个子 manifest(用它的 digest),拿到一份**单平台 manifest**(config + layers)。
5. 接着按上一节的流程拉 config、拉 layers、组装 rootfs。

用户全程无感——一条 `docker pull nginx`,在 amd64 机器上拉到 amd64 版,在 arm64 机器上拉到 arm64 版。**manifest list 把"多架构支持"做进了镜像格式本身,而不是让用户自己挑。**

> **比喻**:manifest 是"一箱货的装箱单",manifest list 是"**一票货的总运单**"——总运单上写"这票货分成 3 个集装箱,1 号箱给 ARM 码头、2 号箱给 x86 码头、3 号箱给 PowerPC 码头"。每个码头只领自己的那个箱子,但发货人只要填一张总运单。
>
> k8s 集群经常是混合架构(amd64 跑业务、arm64 跑边缘、ppc64le 跑大型机),manifest list 是这种集群能正常 `kubectl apply` 的基础——同一个 `image: nginx` 字段,在每个节点上自动拉到对应架构的版本。第 18 章讲调度器时你会再见到 platform 这个维度。

---

## 六、把第 5 章和本章打通:layer 怎么变成 overlayfs 的 lower

讲了这么多 manifest/config/digest,最后必须把它们和第 5 章的 overlayfs 接起来——否则两章就成了两张皮。这一节回答一个关键问题:

> **镜像拉下来后,manifest 里的那些 layer blob,怎么变成第 5 章讲的 overlayfs 的 lower 目录,最终挂成 rootfs?**

这个转换,是 containerd 的 **snapshotter(快照器)** 干的活。流程是这样的:

```
  manifest.layers[0]: digest=sha256:e692...(tar.gz)        ┐
  manifest.layers[1]: digest=sha256:3c3a...(tar.gz)        │  拉取 + 解压 + 落盘
  manifest.layers[2]: digest=sha256:ec4b...(tar.gz)        ┘
                  │
                  ▼
  ┌─────────────────────────────────────────────────────────┐
  │ containerd content store(存原始 blob)                  │
  │   /var/lib/containerd/io.containerd.content.v1.content/ │
  │     blobs/sha256/e692418e...   ← 压缩的 tar.gz         │
  │     blobs/sha256/3c3a4604...                           │
  │     blobs/sha256/ec4b8955...                           │
  └─────────────────────────────────────────────────────────┘
                  │
                  │  解压 + Apply 到 snapshot
                  ▼
  ┌─────────────────────────────────────────────────────────┐
  │ containerd snapshotter(overlay)                        │
  │   snapshots/<id0>/fs/   ← layer0 解压后的目录(lower)   │
  │   snapshots/<id1>/fs/   ← layer0+layer1 叠加解压(父=id0)│
  │   snapshots/<id2>/fs/   ← 三层全叠(父=id1)             │
  │                                                          │
  │   每一层 = 它自己 + 所有父层的内容叠加                   │
  └─────────────────────────────────────────────────────────┘
                  │
                  │  容器启动时:拼出 mount 配置
                  ▼
  ┌─────────────────────────────────────────────────────────┐
  │ overlay mount(交给 runc 执行)                          │
  │   lowerdir = snap0/fs:snap1/fs:snap2/fs                 │
  │   upperdir = <新容器的可写层>                            │
  │   workdir  = <overlay 内核用>                            │
  │   → 挂到容器的 /                                         │
  └─────────────────────────────────────────────────────────┘
```

这个流程的关键节点:

1. **拉取**:docker/containerd 按 manifest 列的 digest,逐层从 registry 拉 tar.gz blob,存到 **content store**(原始压缩 blob,按 digest 寻址)。
2. **解压成 snapshot**:每拉一层,snapshotter 调用 **Applier**(解压器),把这一层 tar.gz **解压 + 应用到一个新的 snapshot 目录**。注意——snapshot 是**累积**的:`snap2` 是"snap1 的内容 + layer2 的 diff",`snap1` 又是"snap0 + layer1",层层叠加。**每层 snapshot 的名字是 chainID**(下面看源码)。
3. **拼 mount**:容器要启动时,containerd 算出"这个镜像最顶层的 chainID"对应的 snapshot,把它的所有父 snapshot 路径拿出来,**拼成 `lowerdir=path0:path1:path2` 的 overlay mount 配置**(第 5 章见过这个格式),加一个新 upperdir 给容器写。这个 mount 配置交给 runc。
4. **挂载**:runc 当作普通 mount 执行,内核 overlayfs 接管——容器进程看到的就是叠好的 rootfs。

**第 5 章讲的 lower/upper/merged 三层,在这里和镜像 layer 接上了**:

- 第 5 章的 **lower**(只读层,公共可共享)= 本章讲的**镜像 layer 解压后的 snapshot 目录**。多个容器共用同一个镜像,就是共用同一组 lower snapshot——这是第 5 章讲的"几十个容器共用一份只读层"在工程上的落地。
- 第 5 章的 **upper**(本容器可写)= 本章流程里 containerd 给每个新容器**新建**的空目录。容器一删,upper 一清,lower(镜像 snapshot)毫发无损——这就是第 5 章讲的 copy-up + whiteout 不污染镜像的工程保证。

> **两章是镜像这枚硬币的两面**:第 5 章讲"层在内核里怎么叠"(overlayfs 视角),本章讲"层在打包/传输/寻址时怎么组织"(manifest/config/digest 视角)。中间的桥就是 **containerd 的 snapshotter**——它把"manifest 里的 digest 引用"翻译成"磁盘上的 lower 目录",把镜像的世界和文件系统的世界对接起来。下一节看真实代码,你会看到这个翻译的每一行。

---

## 关键源码精读:OCI 规范定义 + containerd 解构镜像

讲完原理,去看代码里这些东西是怎么落地的。本节挑三段最核心的源码:**① OCI image-spec 的结构体定义(规范);② containerd 怎么从 manifest 解构出 config 和 layers(解析);③ containerd 怎么把 layer 解压成 snapshot chain(落地为 overlayfs lower)**。

### 1. OCI image-spec:Manifest / Config / Descriptor / Index 的官方定义

先看规范本身。OCI image-spec 是用 Go 结构体定义的(containerd 把它 vendor 在本地,我们读本地副本),Manifest 长这样([vendor/.../image-spec/specs-go/v1/manifest.go#L20-L41](../containerd/vendor/github.com/opencontainers/image-spec/specs-go/v1/manifest.go#L20-L41)):

```go
// Manifest provides `application/vnd.oci.image.manifest.v1+json` mediatype structure when marshalled to JSON.
type Manifest struct {
	specs.Versioned

	// MediaType specifies the type of this document data structure e.g. `application/vnd.oci.image.manifest.v1+json`
	MediaType string `json:"mediaType,omitempty"`

	// ArtifactType specifies the IANA media type of artifact when the manifest is used for an artifact.
	ArtifactType string `json:"artifactType,omitempty"`

	// Config references a configuration object for a container, by digest.
	// The referenced configuration object is a JSON blob that the runtime uses to set up the container.
	Config Descriptor `json:"config"`

	// Layers is an indexed list of layers referenced by the manifest.
	Layers []Descriptor `json:"layers"`

	// Subject is an optional link from the image manifest to another manifest forming an association between the image manifest and the other manifest.
	Subject *Descriptor `json:"subject,omitempty"`

	// Annotations contains arbitrary metadata for the image manifest.
	Annotations map[string]string `json:"annotations,omitempty"`
}
```

注意几个字段对应前面讲的"为什么":

- **`Config Descriptor`**:config 是一个 `Descriptor`(下面看这个结构),不是内联的 JSON——**用 digest 引用**。这就是"config 和 layer 分开存"在规范层面的体现。
- **`Layers []Descriptor`**:layer 是一个 Descriptor 切片,**有序数组**(从底到顶)。规范没给"无序"的选项,强制有序——因为叠层顺序决定了最终文件系统长什么样。
- **`Subject *Descriptor`**:这是较新的特性(引用关联),允许一个 manifest 指向另一个 manifest(比如签名、SBOM 软件物料清单)。

`Descriptor` 是整个规范的脊梁——**所有"指向别处内容"的引用,都是 Descriptor**([vendor/.../image-spec/specs-go/v1/descriptor.go#L22-L50](../containerd/vendor/github.com/opencontainers/image-spec/specs-go/v1/descriptor.go#L22-L50)):

```go
// Descriptor describes the disposition of targeted content.
type Descriptor struct {
	// MediaType is the media type of the object this schema refers to.
	MediaType string `json:"mediaType"`

	// Digest is the digest of the targeted content.
	Digest digest.Digest `json:"digest"`

	// Size specifies the size in bytes of the blob.
	Size int64 `json:"size"`

	// URLs specifies a list of URLs from which this object MAY be downloaded
	URLs []string `json:"urls,omitempty"`

	// Annotations contains arbitrary metadata relating to the targeted content.
	Annotations map[string]string `json:"annotations,omitempty"`

	// Data is an embedding of the targeted content. ...
	Data []byte `json:"data,omitempty"`

	// Platform describes the platform which the image in the manifest runs on.
	//
	// This should only be used when referring to a manifest.
	Platform *Platform `json:"platform,omitempty"`

	// ArtifactType is the IANA media type of this artifact.
	ArtifactType string `json:"artifactType,omitempty"`
}
```

`Descriptor` 的精髓就三个字段:**`MediaType + Digest + Size`**——"这是什么格式、内容的 hash 是什么、多大"。这三个字段构成了一个**自验证的内容引用**:拉取方知道该用什么解析器(mediaType)、该从哪里取(digest 在 registry 里寻址)、下多大、下完怎么校验(重算 hash 比对 digest)。**整个 OCI 镜像就是由 Descriptor 互相引用串起来的一张图。**

> 注意 `Platform *Platform` 字段——它"只用于指向 manifest 时"(注释明说)。这正是 manifest list 里每个元素带 Platform 的规范依据。在 manifest 引用 config/layer 时,Platform 是空的(它们不跨平台);只有 index 引用 manifest 时,Platform 才填上。

`Image`(也就是 config JSON 对应的结构体)定义了启动参数和层指纹([vendor/.../image-spec/specs-go/v1/config.go#L93-L111](../containerd/vendor/github.com/opencontainers/image-spec/specs-go/v1/config.go#L93-L111)):

```go
// Image is the JSON structure which describes some basic information about the image.
// This provides the `application/vnd.oci.image.config.v1+json` mediatype when marshalled to JSON.
type Image struct {
	// Created is the combined date and time at which the image was created, formatted as defined by RFC 3339, section 5.6.
	Created *time.Time `json:"created,omitempty"`

	// Author defines the name and/or email address of the person or entity which created and is responsible for maintaining the image.
	Author string `json:"author,omitempty"`

	// Platform describes the platform which the image in the manifest runs on.
	Platform

	// Config defines the execution parameters which should be used as a base when running a container using the image.
	Config ImageConfig `json:"config,omitempty"`

	// RootFS references the layer content addresses used by the image.
	RootFS RootFS `json:"rootfs"`

	// History describes the history of each layer.
	History []History `json:"history,omitempty"`
}
```

而启动参数在嵌套的 `ImageConfig` 里([vendor/.../image-spec/specs-go/v1/config.go#L24-L62](../containerd/vendor/github.com/opencontainers/image-spec/specs-go/v1/config.go#L24-L62)):

```go
// ImageConfig defines the execution parameters which should be used as a base when running a container using an image.
type ImageConfig struct {
	// User defines the username or UID which the process in the container should run as.
	User string `json:"User,omitempty"`

	// ExposedPorts a set of ports to expose from a container running this image.
	ExposedPorts map[string]struct{} `json:"ExposedPorts,omitempty"`

	// Env is a list of environment variables to be used in a container.
	Env []string `json:"Env,omitempty"`

	// Entrypoint defines a list of arguments to use as the command to execute when the container starts.
	Entrypoint []string `json:"Entrypoint,omitempty"`

	// Cmd defines the default arguments to the entrypoint of the container.
	Cmd []string `json:"Cmd,omitempty"`

	// Volumes is a set of directories describing where the process is likely write data specific to a container instance.
	Volumes map[string]struct{} `json:"Volumes,omitempty"`

	// WorkingDir sets the current working directory of the entrypoint process in the container.
	WorkingDir string `json:"WorkingDir,omitempty"`
	// ... StopSignal, ArgsEscaped ...
}
```

这些字段,就是 `docker run` 时 docker 读出来当默认启动参数的来源——`Entrypoint` + `Cmd` 决定跑什么命令,`Env` 决定环境变量,`User` 决定以谁的身份跑,`WorkingDir` 决定起在哪个目录。**config 的全部意义,就是把这些"启动参数"从 layer 里剥离出来,让它们可以独立变化。**

`RootFS` 这个字段藏着一个前面讲过的关键设计([vendor/.../image-spec/specs-go/v1/config.go#L65-L71](../containerd/vendor/github.com/opencontainers/image-spec/specs-go/v1/config.go#L65-L71)):

```go
// RootFS describes a layer content addresses
type RootFS struct {
	// Type is the type of the rootfs.
	Type string `json:"type"`

	// DiffIDs is an array of layer content hashes (DiffIDs), in order from bottom-most to top-most.
	DiffIDs []digest.Digest `json:"diff_ids"`
}
```

**注释明说:DiffIDs 是"层内容 hash",从底到顶**。这就是我们前面讲的"config 里存的是解压后的 diffID,不是 manifest 里压缩后的 digest"。规范在这一行的注释里,就把两个 hash 的区别定死了。

最后看 Index(manifest list 的 OCI 名字)([vendor/.../image-spec/specs-go/v1/index.go#L21-L38](../containerd/vendor/github.com/opencontainers/image-spec/specs-go/v1/index.go#L21-L38)):

```go
// Index references manifests for various platforms.
// This structure provides `application/vnd.oci.image.index.v1+json` mediatype when marshalled to JSON.
type Index struct {
	specs.Versioned

	// MediaType specifies the type of this document data structure e.g. `application/vnd.oci.image.index.v1+json`
	MediaType string `json:"mediaType,omitempty"`

	// ArtifactType specifies the IANA media type of artifact when the manifest is used for an artifact.
	ArtifactType string `json:"artifactType,omitempty"`

	// Manifests references platform specific manifests.
	Manifests []Descriptor `json:"manifests"`

	// Subject is an optional link from the image manifest to another manifest ...
	Subject *Descriptor `json:"subject,omitempty"`

	// Annotations contains arbitrary metadata for the image index.
	Annotations map[string]string `json:"annotations,omitempty"`
}
```

`Manifests []Descriptor`——和 Manifest 的 `Layers` 长得几乎一样,但**指向的是 manifest 而不是 layer**。每个 Descriptor 带 `Platform`(在 Descriptor 那个结构里),标明这个子 manifest 是哪个平台的。这就是 manifest list 的全部规范定义——简洁到惊人。

**mediaType 的常量定义**也值得看一眼([vendor/.../image-spec/specs-go/v1/mediatype.go#L17-L48](../containerd/vendor/github.com/opencontainers/image-spec/specs-go/v1/mediatype.go#L17-L48)):

```go
const (
	MediaTypeImageIndex    = "application/vnd.oci.image.index.v1+json"
	MediaTypeImageManifest = "application/vnd.oci.image.manifest.v1+json"
	MediaTypeImageConfig   = "application/vnd.oci.image.config.v1+json"
	// ...
	MediaTypeImageLayer      = "application/vnd.oci.image.layer.v1.tar"
	MediaTypeImageLayerGzip  = "application/vnd.oci.image.layer.v1.tar+gzip"
	MediaTypeImageLayerZstd  = "application/vnd.oci.image.layer.v1.tar+zstd"
)
```

**这些字符串就是"镜像各部分的 MIME 类型"**——manifest、config、index、layer 各有一个,layer 还按压缩格式分了 tar/gzip/zstd 三种。registry 和 client 靠它们区分"这一坨字节是什么"。第 8 章讲 OCI 标准时你会再见到这套 mediaType——它是 OCI 把"镜像格式"标准化的核心抓手。

### 2. containerd 解构镜像:从一个 Descriptor 拿到 config 和 layers

规范定义清楚了,看 containerd 怎么用它。containerd 在内部把一个镜像表示成一个极简的结构([core/images/image.go#L34-L56](../containerd/core/images/image.go#L34-L56)):

```go
// Image provides the model for how containerd views container images.
type Image struct {
	// Name of the image.
	//
	// To be pulled, it must be a reference compatible with resolvers.
	Name string

	// Labels provide runtime decoration for the image record.
	Labels map[string]string

	// Target describes the root content for this image. Typically, this is
	// a manifest, index or manifest list.
	Target ocispec.Descriptor

	CreatedAt, UpdatedAt time.Time
}
```

注意 containerd 的 `Image` 结构**只存一个 `Target Descriptor`**——它不直接持有 manifest 或 layer,而是"指向 manifest 的 digest"。**要拿到真正的 manifest 内容,得通过 content store 用这个 digest 去取。** 这就是内容寻址在 containerd 数据模型里的体现:Image 是一个 Descriptor 的壳,真正的字节在 content store 里按 digest 摊开。

那怎么从 `Target`(一个 Descriptor,可能指向 index,也可能直接指向 manifest)拿到 config 和 layers?看 containerd 的 `Manifest()` 函数,它处理了"Target 可能是 index 也可能是 manifest"两种情况([core/images/image.go#L153-L255](../containerd/core/images/image.go#L153-L255)),关键的 index 处理段:

```go
} else if IsIndexType(desc.MediaType) {
    p, err := content.ReadBlob(ctx, provider, desc)
    if err != nil {
        return nil, err
    }
    // ...
    var idx ocispec.Index
    if err := json.Unmarshal(p, &idx); err != nil {
        return nil, err
    }

    if platform == nil {
        return idx.Manifests, nil
    }

    var descs []ocispec.Descriptor
    for _, d := range idx.Manifests {
        if d.Platform == nil || platform.Match(*d.Platform) {
            descs = append(descs, d)
        }
    }
    // ... 按 platform 排序 ...
    return descs, nil
}
```

读这段,它做的事就是前面讲的"manifest list 挑平台":

- 如果 `Target` 指向的是 index(`IsIndexType`),把它反序列化成 `ocispec.Index`。
- 遍历 `idx.Manifests`(每个是一个 Descriptor,带 Platform),**用 `platform.Match(*d.Platform)` 过滤出当前平台匹配的**。
- 返回这些匹配的子 manifest Descriptor。调用方会再对每个子 manifest 走 `IsManifestType` 分支,拿到真正的单平台 manifest。

注意那个 `d.Platform == nil || platform.Match(*d.Platform)`——**`Platform == nil` 也算匹配**。这是因为有些 manifest 在 index 里没标 platform(老镜像或 atypical 镜像),containerd 把它们当成"可能适用"放进来,让后续校验决定。

拿到单平台 manifest 后,config 怎么取?看 `Config()` 函数([core/images/image.go#L262-L268](../containerd/core/images/image.go#L262-L268)):

```go
func Config(ctx context.Context, provider content.Provider, image ocispec.Descriptor, platform platforms.MatchComparer) (ocispec.Descriptor, error) {
	manifest, err := Manifest(ctx, provider, image, platform)
	if err != nil {
		return ocispec.Descriptor{}, err
	}
	return manifest.Config, nil
}
```

**就是 `manifest.Config` 这一句**——拿 manifest 里的 Config 字段(它是个 Descriptor)。要真正读到 config 的 JSON 内容,调用方再拿这个 Descriptor 去	content store 取。整个链路:**Target(Descriptor) → manifest(JSON) → Config(Descriptor) → config(JSON)**,每一步都是"用 digest 取下一层"。这就是内容寻址的工作方式——**没有"指针解引用",只有"用 hash 取 blob"**。

containerd 还提供了一个 `Children()` 函数,把"manifest 里引用了哪些子节点"抽象出来([core/images/image.go#L337-L377](../containerd/core/images/image.go#L337-L377)):

```go
func Children(ctx context.Context, provider content.Provider, desc ocispec.Descriptor) ([]ocispec.Descriptor, error) {
	if IsManifestType(desc.MediaType) {
		// ... 反序列化成 Manifest ...
		return append([]ocispec.Descriptor{manifest.Config}, manifest.Layers...), nil
	} else if IsIndexType(desc.MediaType) {
		// ... 反序列化成 Index ...
		return append([]ocispec.Descriptor{}, index.Manifests...), nil
	}
	// ...
}
```

**这个函数是整个镜像遍历的基石**。containerd 用它实现"从根 Descriptor 出发,递归遍历整个镜像的所有节点"——拉取时,就是从 Target 出发,不停 `Children()`、对每个 child 调用 fetch handler,把所有 blob 拉到本地 content store。`Children()` 把 manifest/config/index/layer 的引用关系,统一抽象成"一个 Descriptor 有哪些子 Descriptor"——这让上层逻辑(拉取、push、GC)都可以用同一套遍历框架处理,不用关心具体是哪种节点。这是个非常漂亮的设计。

### 3. 拉取流程:fetcher 从 registry 按 digest 取 blob

containerd 的 docker registry 拉取器([core/remotes/docker/fetcher.go](../containerd/core/remotes/docker/fetcher.go))里,`Fetch()` 方法对不同类型的 blob 走不同的 HTTP 端点。看关键分支([core/remotes/docker/fetcher.go#L277-L329](../containerd/core/remotes/docker/fetcher.go#L277-L329)):

```go
// Try manifests endpoints for manifests types
if images.IsManifestType(desc.MediaType) || images.IsIndexType(desc.MediaType) {
    // ...
    for i, host := range r.hosts {
        req := r.request(host, http.MethodGet, "manifests", desc.Digest.String())
        // ...
        rc, _, err := r.open(ctx, req, desc.MediaType, offset, i == len(r.hosts)-1)
        // ...
    }
    // ...
}

// Finally use blobs endpoints
// ...
for i, host := range r.hosts {
    req := r.request(host, http.MethodGet, "blobs", desc.Digest.String())
    // ...
    rc, _, err := r.open(ctx, req, desc.MediaType, offset, i == len(r.hosts)-1)
    // ...
}
```

读这两段,可以看到 registry HTTP API 的设计:

- **manifest 和 index 走 `/manifests/<digest>` 端点**。这个端点支持内容协商(根据 Accept 头返回不同 schema 版本),也支持按 tag 或按 digest 取。**为什么 manifest 单独一个端点?** 因为 manifest 可能被 registry 在传输时做格式转换(老 schema 1 ↔ schema 2),它需要一个专门的端点处理这些转换逻辑。
- **config 和 layer 走 `/blobs/<digest>` 端点**。blob 是"纯字节内容",registry 不做任何转换,原样返回。**这就是内容寻址的 HTTP 体现**:`/blobs/sha256:xxx` 直接用 digest 当 URL,registry 找到那个 blob 原样吐出来。

注意两段都用 `desc.Digest.String()` 当 URL 参数——**digest 既是 manifest 里的引用,又是 registry 上的寻址 key**。整个链条无缝:manifest 引用 digest → fetcher 把 digest 拼进 URL → registry 按 digest 找 blob → 返回的字节重算 hash 校验。**没有一处"翻译",digest 贯穿始终。**

还有一个细节值得品:fetcher 在 `open()` 里支持**并行分块下载**([core/remotes/docker/fetcher.go#L502-L588](../containerd/core/remotes/docker/fetcher.go#L502-L588))——大 layer 被切成 chunk,用多个 HTTP Range 请求并行拉,再拼起来。这是 registry HTTP API 支持 `Range` 头带来的福利,让大镜像拉取不卡在单连接上。chunk 大小由 `ConcurrentLayerFetchBuffer` 控制,默认走并行;小 layer(小于 chunk size)自动退化成单连接。这是工业级运行时在"内容寻址"之上加的传输优化——**核心机制(digest 寻址)不变,优化叠在上面**。

### 4. diffID 怎么算:解压后的 hash 才给 chainID 用

前面讲了 digest(压缩后)和 diffID(解压后)的区别,看 containerd 怎么从压缩 blob 算出 diffID([core/images/diffid.go#L33-L82](../containerd/core/images/diffid.go#L33-L82)):

```go
// GetDiffID gets the diffID of the layer blob descriptor.
func GetDiffID(ctx context.Context, cs content.Store, desc ocispec.Descriptor) (digest.Digest, error) {
	switch desc.MediaType {
	case
		// If the layer is already uncompressed, we can just return its digest
		MediaTypeDockerSchema2Layer,
		ocispec.MediaTypeImageLayer,
		MediaTypeDockerSchema2LayerForeign,
		ocispec.MediaTypeImageLayerNonDistributable:
		return desc.Digest, nil
	}
	info, err := cs.Info(ctx, desc.Digest)
	if err != nil {
		return "", err
	}
	v, ok := info.Labels[labels.LabelUncompressed]
	if ok {
		// Fast path: if the image is already unpacked, we can use the label value
		return digest.Parse(v)
	}
	// if the image is not unpacked, we may not have the label
	ra, err := cs.ReaderAt(ctx, desc)
	if err != nil {
		return "", err
	}
	defer ra.Close()
	r := content.NewReader(ra)
	uR, err := compression.DecompressStream(r)
	if err != nil {
		return "", err
	}
	defer uR.Close()
	digester := digest.Canonical.Digester()
	hashW := digester.Hash()
	if _, err := io.Copy(hashW, uR); err != nil {
		return "", err
	}
	// ...
	digest := digester.Digest()
	// memorize the computed value
	info.Labels[labels.LabelUncompressed] = digest.String()
	// ...
	return digest, nil
}
```

读这段,把"digest vs diffID"彻底看清:

- **开头那个 switch**:如果 layer **本身就是未压缩的**(mediaType 是 `MediaTypeImageLayer` 等,不带 `+gzip`/`+zstd` 后缀),那它的 digest 就等于 diffID——**没压缩,两个 hash 自然相同**。这是个快速路径。
- 否则,**走解压流程**:从 content store 拿到压缩 blob 的 `ReaderAt`,套一层 `DecompressStream`(根据 mediaType 选 gzip/zstd 解压器),得到解压后的字节流,再 `io.Copy` 到一个 SHA256 digester,**算出来的 hash 才是 diffID**。
- **结果缓存到 `LabelUncompressed` 这个 label 里**——下次同一 layer 再问 diffID,直接读 label,不用再解压一遍。这是个工程优化:解压是 CPU 密集操作,缓存结果避免重复劳动。

**这段代码精确地回答了"两个 hash 各是什么"**:

- `desc.Digest`(传入参数):压缩 blob 的 hash,来自 manifest。GetDiffID 的输入。
- 函数返回值:解压后的 hash,即 diffID。它会和 config 里 `rootfs.diff_ids` 比对——**对得上,说明这一层解压后的内容正是镜像作者打包时的内容,没被改、没传错**。

### 5. chainID:把一组 diffID 折叠成"这条层链的名字"

最后看一个最精妙的算法——**chainID**。前面讲过,本地 snapshot 的名字不是单层的 diffID,而是"从底到这层所有 diffID 叠起来"的 hash。这个 hash 怎么算?看 OCI image-spec 的 identity 包([vendor/.../image-spec/identity/chainid.go#L41-L67](../containerd/vendor/github.com/opencontainers/image-spec/identity/chainid.go#L41-L67)):

```go
// ChainIDs calculates the recursively applied chain id for each identifier in
// the slice. ...
//
// As an example, given the chain of ids `[A, B, C]`, the result `[A,
// ChainID(A|B), ChainID(A|B|C)]` will be written back to the slice.
func ChainIDs(dgsts []digest.Digest) []digest.Digest {
	if len(dgsts) < 2 {
		return dgsts
	}

	parent := digest.FromBytes([]byte(dgsts[0] + " " + dgsts[1]))
	next := dgsts[1:]
	next[0] = parent
	ChainIDs(next)

	return dgsts
}
```

短短几行,干的事很深:**把一组 diffID `[A, B, C]` 递归折叠成 `[A, ChainID(A,B), ChainID(A,B,C)]`**。每一项的 chainID,是"它自己的 diffID + 前一项的 chainID"拼起来再 hash。注释里那个例子:`[A, B, C]` → `[A, ChainID(A|B), ChainID(A|B|C)]`。

**为什么这么算?** 因为 chainID 要当 snapshot 的名字。一个 snapshot 代表"从底到这层全部叠起来"的状态——它不仅取决于这一层,还取决于它底下所有层。所以 chainID 必须**包含整条链的信息**。用递归 hash 折叠:

- 只要链上任一层变了,从那一层往上的所有 chainID 全变——**天然区分"不同的层链"**。
- 同样的层链(同样的 diffID 序列),算出的 chainID 一定相同——**天然复用 snapshot**。
- chainID 是 hash,不可逆——但不需要反推,它只当名字用。

**containerd 用 chainID 给 snapshot 命名**。看 unpacker 怎么用([core/unpack/unpacker.go#L368-L404](../containerd/core/unpack/unpacker.go#L368-L404)):

```go
// pre-calculate chain ids for each layer
chainIDs := make([]digest.Digest, len(diffIDs))
copy(chainIDs, diffIDs)
chainIDs = identity.ChainIDs(chainIDs)

topHalf := func(i int, desc ocispec.Descriptor, span *tracing.Span, startAt time.Time) (<-chan *unpackStatus, error) {
	var (
		err     error
		parent  string
		chainID string
	)
	if i > 0 && !parallel {
		parent = chainIDs[i-1].String()
	}
	chainID = chainIDs[i].String()
	// ...
	snapshotLabels[labelSnapshotRef] = chainID
	snapshotLabels[labelSnapshotDiffID] = diffIDs[i].String()
	if i > 0 {
		snapshotLabels[labelSnapshotParent] = chainIDs[i-1].String()
	}
	// ...
	// Prepare snapshot with from parent, label as root
	key = fmt.Sprintf(snapshots.UnpackKeyFormat, uniquePart(), chainID)
	mounts, err = sn.Prepare(ctx, key, parent, opts...)
	// ...
}
```

读这段,看 chainID 怎么落地:

- `identity.ChainIDs(chainIDs)` 把 diffIDs 折叠成 chainIDs——**每一项是"到这层为止的层链指纹"**。
- 对每一层,`parent = chainIDs[i-1].String()`——**父 snapshot 的名字是上一层的 chainID**。这保证 snapshot 的父子关系和层的叠放关系一致。
- `chainID = chainIDs[i].String()`——**这一层的 snapshot 名字是它的 chainID**。
- `sn.Prepare(ctx, key, parent, ...)`——在 snapshotter 里**准备一个新 snapshot,父是 parent(上一层叠完的结果),内容待填**。后续会调用 Applier 把这一层的 tar.gz 解压,应用到这个 snapshot 上。

**chainID 在这里同时是"snapshot 的身份"和"去重的 key"**:

- 同一个镜像(`nginx:1.27`)在两台机器上 pull,层的 diffID 序列一样,算出的 chainID 序列一样,**两台机器上 snapshot 的名字一样**。
- 同一台机器 pull `nginx:1.27` 和 `nginx:1.27-debug`(假设它们 base 层一样),base 层的 chainID 一样,**containerd 发现这个 snapshot 已存在,直接复用,不解压第二遍**。

> **这就是 chainID 把"网络上的 digest 去重"延续到了"本地的 snapshot 去重"**。第 5 章讲的"几十个容器共用一份只读 lower",在工程上的实现就是:**所有共用的层,因为 diffID 相同,算出的 chainID 相同,在 snapshotter 里只存一份,被多个 overlay mount 引用**。chainID 是这张共享网络的"户口本"——它决定了"这个 snapshot 是谁、归谁所有、能不能复用"。

至此,从规范(Descriptor)、到解析(Children/Manifest/Config)、到拉取(fetcher)、到校验(GetDiffID)、到落地(chainID + snapshot),整条链路在源码里全部走通。**一个 `docker pull nginx`,底下就是这么一串函数调用**——没有魔法,全是"用 digest 寻址、用 hash 校验、用 chainID 复用"这套机制的层层叠加。

---

## 章末小结

### 用航运比喻回顾本章

回到那片港口。第 6 章我们手搓的集装箱,用的是一块**手动 curl 下来的 busybox 目录**当货架——土得掉渣。真正的航运公司(docker)用的"标准集装箱",里面装的可不是一坨散货,而是**一套规范的、可搬运的、可追溯的标准件**:

1. **货物本身**(layer):一层一层叠的托盘,每块托盘是一组文件 diff(相对上层的增量),用压缩 tar.gz 包装,贴一张**压缩后的指纹**(digest)。
2. **货物说明书**(config):一份 JSON,写明"这箱货通电后启动 nginx、环境变量 PORT=80、以 nginx 用户跑"——**启动参数和货物分开存**,改说明书不用换货。
3. **装箱单 / 提单**(manifest):一份 JSON,列出"本箱共 5 块托盘,每块托盘的指纹是 sha256:xxx、多大、什么压缩格式"——**用指纹引用每块托盘**,不内联内容。
4. **总运单**(manifest list / index):如果是多架构镜像,再来一份"清单的清单",写明"ppc64le 码头领 1 号箱、amd64 码头领 2 号箱"——**一个镜像名,适配所有 CPU 架构的码头**。

这套标准件最巧妙的地方,是**所有引用都走指纹(SHA256 digest)**:

- **同样的货,指纹一样,全球仓库只存一份**——天然去重。
- **改一个字节的货,指纹就变,装箱单对不上,立刻露馅**——天然防篡改。
- 一个指纹同时解决"去重"和"防伪"两个问题,是整个标准件体系的脊梁。

而最精细的一笔,是**每块托盘有两个指纹**:压缩后的(digest,给网络和仓库看)和解压后的(diffID,给本地文件系统看)。前者校验"下载没出错",后者算出"层链指纹"(chainID)——chainID 决定了本地货架上哪几块托盘可以叠在一起复用。**这就是第 5 章讲的"几十个集装箱共用同一摞只读托盘"在标准件层面的实现**。

最后,码头工人(containerd 的 snapshotter)拿到这套标准件,干的活就是把**装箱单上的指纹引用,翻译成本地货架上的托盘实体**——拉取、解压、按 chainID 落盘、拼成 overlay mount 交给 runc。**第 5 章(overlayfs 怎么叠)和本章(镜像怎么打包)这两枚硬币,在 containerd 的 snapshotter 这里合二为一。**

### 本章在第 2 篇中的位置

记住全书的二分法:**打包隔离 vs 调度编排。**

这一章,我们进入了第 2 篇(镜像与运行时标准)的第一站。第 1 篇(基石)讲的是"**怎么把一个进程隔离、限资源、换根、分层 rootfs**"——全是**内核已有能力的组合**,是"打包隔离"这半边最底层的地基。但那些章节里的"rootfs",要么是本地目录(第 6 章手搓),要么是抽象的"层"(第 5 章 overlayfs),**没回答"这个 rootfs 怎么标准化、怎么搬运、怎么复用"**。

本章就补上了这一块:**镜像 = layer + manifest + config,靠 digest 内容寻址串起来**。从此,"打包一个应用"不再是一个土法手搓的目录,而是一个**标准化的、可版本化的、可内容寻址验证的、天然去重的标准件**。这是"打包隔离"这半边从"能用"走向"可工业化分发"的关键一跃。

> 下一章(第 8 章)会接着讲:**这套镜像格式是怎么变成一个公开标准的(OCI image-spec),以及为什么 Docker 要把镜像格式和运行时都标准化(避免厂商锁定)**。本章讲的是"镜像是什么",第 8 章讲的是"为什么全行业都得按这个格式来"。再下一章(第 9 章)讲 runc——**真正把这套标准镜像跑成容器的那个底层程序**,第 1 篇手搓的 100 行的工业版。三章合起来,是"镜像 → 标准 → 运行时"的完整旅程。

### 五个"为什么"清单

如果你只能从这一章带走五件事:

1. **为什么镜像是三件套**:一个 image 拆开是 layer(文件 diff)+ manifest(层清单)+ config(启动参数)。三样各管一摊:layer 管内容、manifest 管"网上怎么搬"、config 管"本地怎么跑"。合并就会丢失"去重""防篡改""改配置不传层"的好处。
2. **为什么 config 要和 layer 分开**:同样的层,换一份启动配置就是另一个镜像(`nginx` → `nginx-debug`)。分开存,改 config 不用重传 layer;改 layer 也不用动 config(只要层链没变)。这是"把变的部分和不变的部分分开寻址"的工程哲学。
3. **为什么用 digest 内容寻址**:一个 SHA256 同时解决两个问题——天然去重(同内容同 hash,只存一份)和天然防篡改(改一字节 hash 变,manifest 对不上立刻报错)。digest 是整个镜像设计的脊梁,所有引用都走它。
4. **为什么一个层有两个 hash**:digest 是压缩 blob 的指纹(给网络/registry,校验下载),diffID 是解压内容的指纹(给本地文件系统,算 chainID 决定 snapshot 复用)。两个各管一段,缺一不可——只压缩没法本地去重,只解压没法网络寻址。
5. **manifest 和 index 各管什么**:manifest 是单平台镜像的层清单(config + layers),index(manifest list)是多平台镜像的"清单的清单",指向多个不同平台的 manifest。index 让 `docker pull nginx` 一个命令在 amd64/arm64/ppc64le 都拉到对应版本,是混合架构集群的基础。

### 想继续深入,该往哪钻

- **亲手拆一个真实镜像**:找一台有 docker 的机器,`docker pull nginx` 后 `docker image save nginx -o nginx.tar`,然后 `tar xf nginx.tar` 解开。你会看到一个目录,里面有 `manifest.json`(可能是 manifest list)、每个 layer 一个 `<digest>.tar.gz`、一份 config JSON。对照本章讲的字段,逐个对。这是理解镜像结构最快的方法。
- **读 OCI image-spec 规范**:[opencontainers/image-spec](https://github.com/opencontainers/image-spec) 的 `specs-go/v1/` 目录。Manifest/Config/Index/Descriptor 四个结构体加起来不到 200 行 Go,但定义了整个镜像格式。本章引用的本地副本在 [containerd/vendor 里](../containerd/vendor/github.com/opencontainers/image-spec/specs-go/v1/manifest.go)。
- **看 containerd 怎么遍历镜像**:[core/images/image.go](../containerd/core/images/image.go) 的 `Manifest()`、`Config()`、`Children()`、`RootFS()` 四个函数。它们合起来,就是"从一个 Descriptor 解构出整个镜像"的全部逻辑。
- **看 containerd 怎么解压 layer 成 snapshot**:[core/unpack/unpacker.go](../containerd/core/unpack/unpacker.go) 的 `unpack()` 函数 + `identity.ChainIDs`。重点理解"diffID 怎么变成 chainID、chainID 怎么当 snapshot 名字、snapshot 之间怎么父子相承"——这是本章和第 5 章打通的那座桥。
- **看 registry HTTP API**:[CNCF Distribution 规范](https://distribution.github.io/distribution/spec/manifest-v2-2/) 的 Image Manifest V2 Schema 2。本章用的真实 JSON 示例就来自这里。重点看 `/manifests/<digest>` 和 `/blobs/<digest>` 两个端点的区别——前者可能被转换,后者原样返回,这是 registry 设计的精髓。

---

> 镜像这枚硬币,到这一章两面都讲透了:一面是 overlayfs 怎么把层叠起来(第 5 章),一面是 manifest/config/digest 怎么把层打包、寻址、搬运(本章)。但还有一个问题没回答:**这套镜像格式,是怎么从一个"Docker 自家的私有格式",变成全行业都得遵守的公开标准的?为什么 Docker 愿意把自己最核心的东西开放出去?** 这背后是容器生态一次差点分裂又惊险收敛的故事。翻开 **第 8 章 · OCI 标准:为什么要有运行时标准**。
