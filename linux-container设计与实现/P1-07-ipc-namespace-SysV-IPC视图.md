# 第七章 · ipc namespace:System V IPC 视图

> 篇:P1 namespace 视图隔离
> 主线呼应:上一章我们看了 uts namespace——简单到几乎没有逻辑,只是把 `struct new_utsname` 复制一份,它存在的意义是揭示"namespace 是按**视图维度**切分"的设计哲学。这一章我们走向另一个极端:ipc namespace 内部不是一张小表,而是**三张完整的资源表**(消息队列、信号量、共享内存),每张表里有 idr 树、哈希表、读写信号量、容量计数。为什么"只是切个 IPC"要这么重?因为 SysV IPC 是 Linux 上最古老的一种进程间通信方式,它有一个其他 IPC 没有的特性——**全局可见的 key**:任何进程只要拿到同一个 key,就能找到同一个 IPC 对象。如果不隔离,容器 A 用 key `0x1234` 建的消息队列,容器 B 也拿这个 key 去建,就会撞车。这一章讲的就是:内核怎么给每个 ipc ns 造一组**独立的 IPC 表**,让容器以为自己独占整机的 SysV IPC。

## 核心问题

**System V IPC(消息队列/信号量/共享内存)的 key 是全局的吗?容器 A 用 `msgget(0x1234, IPC_CREAT)` 建的消息队列,容器 B 用同样的 key 去拿,会发生什么?ipc namespace 是怎么把一台机器的 SysV IPC 资源"复制"成多份独立视图的——它真的复制数据了吗,还是只换了指针?为什么 `struct ipc_namespace` 里要放三张 `ids` 表而不是一张?**

读完本章你会明白:

1. SysV IPC 隔离的根本动机不是"安全性",而是**避免 key 冲突**——全局一张表,key 一旦撞车,两个毫无关系的进程会拿到同一个 IPC 对象,行为不可预测。
2. ipc ns 的实现是**"造一组空的资源表"**:不是复制宿主上现有的 IPC 对象,而是 `kzalloc` 一个全新的 `struct ipc_namespace`,里面三张 `ids` 表都是空的,容器从零开始建自己的 IPC。
3. `struct ipc_namespace.ids[3]` 这三张表分别管 SEM/MSG/SHM,每张表里既有 idr(按 id 查)又有 rhashtable(按 key 查),两种索引并行——为什么这么设计是本章技巧精解的重头戏。
4. ipc ns 还管 **POSIX 消息队列(mqueue)** 和**一组 sysctl 参数**(`/proc/sys/kernel/msgmni` 等),它的边界比"SysV IPC"四个字大。
5. ipc ns 的销毁走的是**延迟回收**(work queue + `synchronize_rcu`),而不是同步释放——为什么必须这么做,关系到 mqueuefs 挂载的一个微妙 race。

> **逃生阀**:如果你不熟 SysV IPC,先记住一句话就够——它是 80 年代 Unix 留下的三种老式进程间通信(`msgget`/`semget`/`shmget`),靠一个全局 key 找到对象。本章不要求你写过 SysV IPC 代码,只要知道"用 key 拿对象"这件事就够了。POSIX 消息队列和共享内存的内部细节不在本章范围,我们只看 namespace 怎么把它们装进盒子。

---

## 7.1 一句话点破

> **ipc namespace 不是把 IPC 对象复制一份,而是给容器进程发一组空的 IPC 资源表——`struct ipc_namespace` 里塞了三张 `ids[3]` 表(信号量/消息队列/共享内存各一张),容器进程的所有 `msgget`/`semget`/`shmget` 都只在自己的表里找 key、在自己的表里建对象。换 ns 就是换三张空表,容器就以为自己独占了整机的 SysV IPC。**

这是结论,不是理由。本章倒过来拆:先看为什么全局 key 是个灾难,再看三张表是怎么组织的,然后钻进 `copy_ipcs` 看一个新 ipc ns 怎么从 `kzalloc` 到挂上 `nsproxy`,最后讲延迟回收的微妙之处。

---

## 7.2 为什么必须隔离 SysV IPC:全局 key 的灾难

先把"为什么要隔离 SysV IPC"这件事讲透,它和 pid namespace、net namespace 的动机都不一样。

SysV IPC 是 80 年代 System V Unix 引入的三种进程间通信原语:

| 原语 | 系统调用 | 用途 |
|------|---------|------|
| 消息队列(Message Queue) | `msgget`/`msgsnd`/`msgrcv` | 进程间投递带类型的消息报文 |
| 信号量(Semaphore) | `semget`/`semop` | 进程间同步(经典的 P/V 操作) |
| 共享内存(Shared Memory) | `shmget`/`shmat`/`shmdt` | 进程间映射同一块物理内存 |

它们共享一个**关键设计**——用 `key_t key`(一个 32 位整数)作为对象的"全局名字"。一个进程调 `msgget(0x1234, IPC_CREAT | 0666)` 建一个消息队列,另一个进程(哪怕是完全不同的程序)只要调 `msgget(0x1234, 0)` 就能拿到**同一个队列**,然后双方就能通信。这个设计在 80 年代的单机多用户 Unix 上是合理的——一台机器上就那么几个服务,key 用宏约定一下就够了。

> **不这样会怎样**:在容器场景,这套全局 key 是灾难。想象宿主上跑 100 个容器,每个容器里都有一个 nginx 进程,都用 `key = 0x1234` 去建自己的 IPC 队列(可能是 nginx 启动脚本里硬编码的)。如果 IPC 是全局一张表,会发生什么?

1. **第一个容器建成功**:`msgget(0x1234, IPC_CREAT|IPC_EXCL)` 成功,id = 0。
2. **第二个容器建失败**:`IPC_EXCL` 标志要求"必须新建",但 key 已被第一个容器占用,返回 `EEXIST`。第二个容器的 nginx 启动脚本退出,报"IPC 队列已存在"。
3. **更糟的情况:不传 IPC_EXCL**。第二个容器拿 `msgget(0x1234, 0)` 直接拿到了**第一个容器的队列**!两个容器往同一个队列里塞消息,第一个容器的 nginx 从队列里读出了第二个容器塞的指令——**跨容器数据泄漏 + 行为错乱**。

这个问题的本质是:**SysV IPC 的 key 是一种"全局命名空间"**,和"文件路径"一样需要被隔离。mnt namespace 隔离了路径,ipc namespace 隔离了 IPC key。

pid ns、net ns、mnt ns 的隔离动机相对直观(pid 重号、网卡冲突、路径冲突),而 ipc ns 的隔离动机更隐蔽——它不是"隔离资源用"(那是 cgroup 的事),而是**隔离 key 这个命名空间**。所以 ipc ns 属于本书二分法的"视图"那一面:它改的是"进程能找到哪些 IPC 对象",不改"进程能用多少 IPC 资源"(后者由 sysctl 的 `msgmni`/`shmmni` 等参数和 cgroup 的 `memory.kmem` 共同控制)。

> **所以这样设计**:给每个 ipc namespace 独立的三张 IPC 表(`ids[3]`),每个 ns 内部的 key 查找只查自己的表。容器 A 的 `msgget(0x1234)` 查自己 ns 的 `ids[IPC_MSG_IDS]`,容器 B 查自己 ns 的——两个 key 完全相同但物理上在两张不同的表里,**永远不会撞车**。这就是 ipc ns 的全部核心。

---

## 7.3 `struct ipc_namespace`:三张表的容器

现在看核心数据结构。[`include/linux/ipc_namespace.h:31`](../linux/include/linux/ipc_namespace.h#L31) 定义了 `struct ipc_namespace`:

```c
/* include/linux/ipc_namespace.h:18(简化) */
struct ipc_ids {
    int in_use;                       /* 当前表里有几个对象 */
    unsigned short seq;               /* id 的序列号高位(见 7.5) */
    struct rw_semaphore rwsem;        /* 读写信号量,保护整张表 */
    struct idr ipcs_idr;              /* idr 树:按 id 查对象 */
    int max_idx;                      /* 已分配的最大索引(加速遍历) */
    int last_idx;                     /* 回绕检测 */
    struct rhashtable key_ht;         /* 哈希表:按 key 查对象 */
};

struct ipc_namespace {
    struct ipc_ids ids[3];            /* ★ 三张表:SEM/MSG/SHM */
    int sem_ctls[4];                  /* 信号量的 sysctl 限制 */
    int used_sems;                    /* 当前 ns 已用信号量数 */
    unsigned int msg_ctlmax;          /* 单条消息最大字节数 */
    unsigned int msg_ctlmnb;          /* 单个队列最大字节数 */
    unsigned int msg_ctlmni;          /* 队列数上限 */
    struct percpu_counter percpu_msg_bytes;
    struct percpu_counter percpu_msg_hdrs;
    size_t shm_ctlmax;                /* 单段共享内存最大字节数 */
    size_t shm_ctlall;                /* 总共享内存上限 */
    unsigned long shm_tot;            /* 当前已用页数 */
    int shm_ctlmni;                   /* 段数上限 */
    ...
    struct vfsmount *mq_mnt;          /* POSIX mqueuefs 的挂载点 */
    unsigned int mq_queues_count;     /* 当前 mqueue 数 */
    ...
    struct ctl_table_header *mq_sysctls;   /* mqueue 的 sysctl */
    struct ctl_table_header *ipc_sysctls;  /* SysV IPC 的 sysctl */
    struct user_namespace *user_ns;        /* 拥有此 ipc ns 的 user ns */
    struct ucounts *ucounts;               /* 用户计数(限每个 user 的 ipc ns 数) */
    struct ns_common ns;                   /* ns_common 多态(供 setns 通用路径用) */
};
```

([ipc_namespace.h:18-81](../linux/include/linux/ipc_namespace.h#L18-L81))

这个结构信息密度极高,我们拆成三层来看。

### 第一层:三张 `ids` 表是核心

`ids[3]` 是这个 ns 的全部业务核心。下标 0/1/2 在 [ipc/util.h:123-125](../linux/ipc/util.h#L123-L125) 定义:

```c
#define IPC_SEM_IDS   0
#define IPC_MSG_IDS   1
#define IPC_SHM_IDS   2
```

三种 SysV IPC 各占一张。访问时用一组糖宏包一层(`ipc/sem.c:169`、`ipc/msg.c:100`、`ipc/shm.c:96`):

```c
#define sem_ids(ns)   ((ns)->ids[IPC_SEM_IDS])
#define msg_ids(ns)   ((ns)->ids[IPC_MSG_IDS])
#define shm_ids(ns)   ((ns)->ids[IPC_SHM_IDS])
```

每次 `msgget` 进内核,先通过 `current->nsproxy->ipc_ns` 拿到当前进程的 ipc ns,然后 `msg_ids(ns)` 取出该 ns 的消息队列表,所有后续查找都在**这张表**里发生。容器 A 和容器 B 的 `current->nsproxy->ipc_ns` 指向不同的 `struct ipc_namespace`,所以各自的 `msg_ids(ns)` 是不同的表——这就是隔离的物理基础。

> **钉死这件事**:`struct ipc_namespace.ids[3]` 是 ipc ns 隔离的物理实现。换 ns = 换三张表,容器的 `msgget`/`semget`/`shmget` 全部只在自己的表里找 key 和建对象。

### 第二层:为什么 `ipc_ids` 里既有 idr 又有 rhashtable

这是 ipc ns 最有看点的一个设计。同一张表里维护**两套索引**:

- `ipcs_idr`:idr 树,按 **id**(整数)查 `kern_ipc_perm`。
- `key_ht`:rhashtable,按 **key**(`key_t`)查 `kern_ipc_perm`。

为什么两套都要?因为 SysV IPC 的对象有**两个不同的访问入口**:

1. **`msgget(key, ...)` 用 key 找**——用户传的是 key,内核要在表里找有没有这个 key 对应的对象(`ipcget_public` 路径,走 `key_ht`)。
2. **`msgsnd(id, ...)` 用 id 找**——`msgget` 成功返回一个 id,后续 `msgsnd`/`msgrcv` 都用这个 id,内核要在表里按 id 定位对象(走 `ipcs_idr`)。

如果只有一套索引,另一套访问就得线性扫描整张表,IPC 一多就崩。所以内核干脆两套都建,空间换时间——key 哈希表负责"建/查找"路径,idr 负责"使用"路径,各走各的。

### 第三层:sysctl、mqueue、user_ns、ns_common 都塞进来了

除了三张核心表,`struct ipc_namespace` 里还塞了一堆东西,这揭示了 ipc ns 的**真实边界比名字暗示的大**:

- **sysctl 参数**(`msg_ctlmax`/`msg_ctlmni`/`shm_ctlmax` 等):每个 ipc ns 有自己的一组限制,容器里 `cat /proc/sys/kernel/msgmni` 看到的是自己 ns 的值。
- **POSIX 消息队列**(`mq_mnt`/`mq_queues_count`):POSIX mqueue 通过一个 mqueuefs 挂载实现,每个 ipc ns 有自己的 mqueuefs 实例。
- **`user_ns`** 反向指针:每个 ipc ns 归属一个 user ns(谁创建的),用于权限检查(`ipcns_install` 里 `ns_capable(ns->user_ns, CAP_SYS_ADMIN)`)。
- **`ns_common ns`**:这是所有 namespace 的多态基类,提供 `get`/`put`/`install`/`owner` 等操作(见 `proc_ns_operations`),让 setns 的通用路径能处理任意 ns。

所以"ipc namespace"这个名字其实缩水了,它管的是**"SysV IPC + POSIX mqueue + 相关 sysctl"**这一整套用户可见的 IPC 视图。

---

## 7.4 `copy_ipcs`:造一组空表

接下来看新 ipc ns 是怎么造出来的。入口是 [`copy_ipcs`](../linux/ipc/namespace.c#L107)([ipc/namespace.c:107](../linux/ipc/namespace.c#L107)):

```c
/* ipc/namespace.c:107 */
struct ipc_namespace *copy_ipcs(unsigned long flags,
    struct user_namespace *user_ns, struct ipc_namespace *ns)
{
    if (!(flags & CLONE_NEWIPC))
        return get_ipc_ns(ns);        /* 没要新 ipc ns,共享父的 */
    return create_ipc_ns(user_ns, ns); /* 要新的,造一个 */
}
```

非常简洁——`CLONE_NEWIPC` 标志位驱动([uapi/linux/sched.h:20-44](../linux/include/uapi/linux/sched.h#L20-L44)),没置就 `get_ipc_ns` 共享父亲的(引用计数 +1),置了就 `create_ipc_ns` 造新的。和 uts ns、pid ns 的 `copy_*` 形态完全一致——这就是 namespace 子系统的"标准模板"。

真正干活的是 [`create_ipc_ns`](../linux/ipc/namespace.c#L38)([ipc/namespace.c:38](../linux/ipc/namespace.c#L38)):

```c
/* ipc/namespace.c:38(简化,完整见 L38-L105) */
static struct ipc_namespace *create_ipc_ns(struct user_namespace *user_ns,
                                           struct ipc_namespace *old_ns)
{
    struct ipc_namespace *ns;
    struct ucounts *ucounts;
    int err;

    /* 1) 配额检查:每个 user 的 ipc ns 数有上限(防 fork 炸弹) */
    ucounts = inc_ipc_namespaces(user_ns);
    if (!ucounts) {
        /* 没配额了,但如果有正在异步释放的 ipc ns,先等它释放完再试一次 */
        if (flush_work(&free_ipc_work))
            goto again;
        goto fail;
    }

    /* 2) 分配新 ns 结构(GFP_KERNEL_ACCOUNT:记账到 memcg) */
    ns = kzalloc(sizeof(*ns), GFP_KERNEL_ACCOUNT);

    /* 3) 分配一个 ns inode 号 + 挂上 proc_ns_operations */
    ns_alloc_inum(&ns->ns);
    ns->ns.ops = &ipcns_operations;

    /* 4) 初始化引用计数和归属 */
    refcount_set(&ns->ns.count, 1);
    ns->user_ns = get_user_ns(user_ns);
    ns->ucounts = ucounts;

    /* 5) 初始化 POSIX mqueuefs(mq_init_ns) */
    mq_init_ns(ns);
    setup_mq_sysctls(ns);
    setup_ipc_sysctls(ns);

    /* 6) ★ 初始化三张 ids 表 */
    msg_init_ns(ns);   /* 建 ids[IPC_MSG_IDS] */
    sem_init_ns(ns);   /* 建 ids[IPC_SEM_IDS] */
    shm_init_ns(ns);   /* 建 ids[IPC_SHM_IDS] */

    return ns;

    /* 失败回滚链(略) */
}
```

([namespace.c:38-105](../linux/ipc/namespace.c#L38-L105))

这里有几个不显眼但极重要的细节。

### 细节一:`kzalloc(..., GFP_KERNEL_ACCOUNT)` —— ipc ns 自己也吃 memcg

`GFP_KERNEL_ACCOUNT` 这个标志会让这次分配被**记账到当前进程的 memcg**。也就是说,**容器每新建一个 ipc ns,这个 ns 结构体的内存(几百字节)会算到容器自己的 memcg 头上**。这看起来是细节,其实是内核工程美学的一环:任何可以被容器引发的内核内存分配,都应该被 memcg 记账,否则恶意容器可以疯狂 unshare(CLONE_NEWIPC) 榨干宿主内核内存。这是 namespace 子系统和 cgroup 子系统协同的一个缩影——namespace 创造视图,cgroup 兜底资源。

### 细节二:`inc_ipc_namespaces` 的配额机制

每个 user 在 user ns 里能创建的 ipc ns 数有上限(由 `/proc/sys/user/max_ipc_namespaces` 控制,见 ucounts 机制)。`inc_ipc_namespaces` 在 [`namespace.c:28`](../linux/ipc/namespace.c#L28) 尝试给当前 user 计数 +1,如果超过上限返回 NULL,创建失败。这是另一道防线——即使没有 memcg,ucounts 也能限制 ns 数量。

> **钉死这件事**:ucounts + memcg 是**两道独立的资源防线**。ucounts 在创建时拦(每个 user 多少个 ns),memcg 在使用时记账(每个 ns 占多少内存)。namespace 的"视图隔离"必须配 cgroup 的"资源限额"才有意义——这是本书"视图 vs 资源"二分法在 ipc ns 上的具体落地。

### 细节三:`msg_init_ns` / `sem_init_ns` / `shm_init_ns` 真正建表

三个 init 函数的形态高度一致。看 `msg_init_ns`([ipc/msg.c:1306](../linux/ipc/msg.c#L1306)):

```c
/* ipc/msg.c:1306 */
int msg_init_ns(struct ipc_namespace *ns)
{
    int ret;

    ns->msg_ctlmax = MSGMAX;          /* 默认 sysctl 值 */
    ns->msg_ctlmnb = MSGMNB;
    ns->msg_ctlmni = MSGMNI;

    ret = percpu_counter_init(&ns->percpu_msg_bytes, 0, GFP_KERNEL);
    if (ret) goto fail_msg_bytes;
    ret = percpu_counter_init(&ns->percpu_msg_hdrs, 0, GFP_KERNEL);
    if (ret) goto fail_msg_hdrs;

    ipc_init_ids(&ns->ids[IPC_MSG_IDS]);   /* ★ 建表 */
    return 0;
    ...
}
```

`sem_init_ns`([ipc/sem.c:249](../linux/ipc/sem.c#L249))和 `shm_init_ns`([ipc/shm.c:109](../linux/ipc/shm.c#L109))结构一样——先填 sysctl 默认值,再调 `ipc_init_ids` 建表。三个 init 函数各建一张表,加起来就是 `ids[3]`。

真正"建表"的是 [`ipc_init_ids`](../linux/ipc/util.c#L115)([ipc/util.c:115](../linux/ipc/util.c#L115)):

```c
/* ipc/util.c:115 */
void ipc_init_ids(struct ipc_ids *ids)
{
    ids->in_use = 0;
    ids->seq = 0;
    init_rwsem(&ids->rwsem);
    rhashtable_init(&ids->key_ht, &ipc_kht_params);
    idr_init(&ids->ipcs_idr);
    ids->max_idx = -1;
    ids->last_idx = -1;
}
```

把 `ipc_ids` 的所有字段清零、初始化读写信号量、初始化 idr 树、初始化 key 哈希表。新 ns 拿到的就是**三张完全空的表**——容器从零开始建自己的 IPC,没有任何继承。

> **反面对比**:有些人会想"是不是应该把父 ns 的 IPC 对象复制一份给子 ns?"**绝对不应该**。① 复制 IPC 对象意味着复制消息内容、信号量状态、共享内存页——开销巨大,而且语义模糊(共享内存的页复制后还是同一份物理页吗?信号量的当前值要不要重置?);② 父 ns 的 IPC 对象往往属于父 ns 的不同进程,这些进程对子 ns 不可见,复制过来的对象就成了"没有主人的孤儿"。所以 ipc ns 的语义是**"清空视图"而非"复制视图"**——新 ns 看到的是空表,自己从头建。这和 mnt namespace(复制挂载树)是不同的设计选择,因为 mnt 的"挂载"是结构性的(必须有一棵树才能 `pivot_root`),而 IPC 的"对象"是数据性的(没有就空着)。

---

## 7.5 ID 编码:index + seq 的妙处

讲到这里必须插一段——SysV IPC 的 id 是怎么编码的。这是 ipc ns 内部最古老也最精巧的一个设计,不搞懂它就看不懂 `ipc_addid` 和 `ipcid_to_idx`。

一个 IPC id(比如 `msgget` 返回的那个整数)在内核里**不是单纯的索引**,而是 `index + seq` 的组合:

```c
/* ipc/util.h:29 */
#define IPCMNI_SHIFT   15    /* 默认 index 占 15 位,即 32K */

/* ipc/util.h:127 */
#define ipcid_to_idx(id)  ((id) & IPCMNI_IDX_MASK)   /* 取低 15 位 = index */
#define ipcid_to_seqx(id) ((id) >> ipcmni_seq_shift()) /* 取高位 = sequence */
```

([util.h:29-49](../linux/ipc/util.h#L29-L49)、[util.h:127-129](../linux/ipc/util.h#L127-L129))

一个 32 位 id 拆成两段:低 15 位是 `index`(在 idr 树里的位置),高 17 位是 `seq`(序列号)。为什么要序列号?

考虑这个场景:

1. 进程 A `msgget(IPC_CREAT)` 拿到 id = `0x80000000`(index=0, seq=1)。
2. 进程 A `msgctl(IPC_RMID)` 删除这个队列,id = `0x80000000` 失效。
3. 进程 B `msgget(IPC_CREAT)` 新建一个队列,**idr 复用了 index=0** 这个槽位。
4. 如果没有 seq,新队列的 id 还是 `0x80000000`——和被删的那个一模一样。
5. 进程 A 之前缓存的 id `0x80000000` 现在指向**完全不同的新队列**——如果 A 还用这个老 id 调 `msgsnd`,就会错误地把消息塞进 B 的队列。

序列号解决这个问题:每次复用 index 时,`ids->seq` 递增。所以新队列的 id 变成 `0x80000001`(seq=2)。进程 A 老的 id `0x80000000` 用 `ipcid_to_seqx` 取出 seq=1,和当前 seq=2 不匹配,`ipc_checkid` 返回失败——A 会拿到 `EINVAL`,而不是错误地操作到新对象。

```c
/* ipc/util.h:203 */
static inline int ipc_checkid(struct kern_ipc_perm *ipcp, int id)
{
    return ipcid_to_seqx(id) != ipcp->seq;   /* seq 不匹配 = id 已过期 */
}
```

([util.h:203-206](../linux/ipc/util.h#L203-L206))

这个设计在 80 年代就有了,但它和 namespace 的关系是——**`ids->seq` 是每张表独立的**,因为 `ipc_init_ids` 在每个新 ipc ns 里把 `seq` 清零。这意味着不同 ipc ns 的 id 可以重号但语义不同(它们在不同的表里)。这是一个微妙但重要的点:namespace 隔离的是"表",不是"id 编码规则",所以同一个 id 值在不同 ns 里指向完全不同的对象——这没问题,因为 lookup 永远在当前进程的 `current->nsproxy->ipc_ns` 的表里发生。

---

## 7.6 `ipc_addid`:往表里塞一个对象

为了完整理解 ipc ns 是怎么用的,我们看一次 IPC 对象的创建路径——以 `msgget(IPC_CREAT)` 为例。

用户态 `msgget` 进内核后,最终调到 [`ipcget`](../linux/ipc/util.c#L673)([util.c:673](../linux/ipc/util.c#L673)):

```c
/* ipc/util.c:673 */
int ipcget(struct ipc_namespace *ns, struct ipc_ids *ids,
           const struct ipc_ops *ops, struct ipc_params *params)
{
    if (params->key == IPC_PRIVATE)
        return ipcget_new(ns, ids, ops, params);    /* 私有 key,直接新建 */
    else
        return ipcget_public(ns, ids, ops, params); /* 公共 key,查找或新建 */
}
```

注意第一个参数 `ns` 就是当前进程的 ipc ns——这是隔离的入口。两条路径都在 `current->nsproxy->ipc_ns` 的表里操作。

`ipcget_public`([util.c:397](../linux/ipc/util.c#L397))里有一段关键逻辑:

```c
/* ipc/util.c:397(简化) */
static int ipcget_public(struct ipc_namespace *ns, struct ipc_ids *ids,
                         const struct ipc_ops *ops, struct ipc_params *params)
{
    down_write(&ids->rwsem);                        /* ★ 写锁住整张表 */
    ipcp = ipc_findkey(ids, params->key);           /* 在 key_ht 里找 */
    if (ipcp == NULL) {
        if (!(params->flg & IPC_CREAT))
            err = -ENOENT;                          /* 不存在且不要新建 */
        else
            err = ops->getnew(ns, params);          /* ★ 新建:最终走 ipc_addid */
    } else {
        /* 已存在:检查权限和 IPC_EXCL */
        ...
    }
    up_write(&ids->rwsem);
    return err;
}
```

([util.c:397-437](../linux/ipc/util.c#L397-L437))

`ipc_findkey` 在 `ids->key_ht` 里查,如果没找到且 `IPC_CREAT`,就调 `ops->getnew`(对消息队列是 `newque`)。`newque` 内部最终调 [`ipc_addid`](../linux/ipc/util.c#L278)([util.c:278](../linux/ipc/util.c#L278))把新对象塞进表:

```c
/* ipc/util.c:278(简化) */
int ipc_addid(struct ipc_ids *ids, struct kern_ipc_perm *new, int limit)
{
    refcount_set(&new->refcount, 1);

    if (limit > ipc_mni)
        limit = ipc_mni;
    if (ids->in_use >= limit)
        return -ENOSPC;                            /* 表满了 */

    idr_preload(GFP_KERNEL);
    spin_lock_init(&new->lock);
    rcu_read_lock();
    spin_lock(&new->lock);

    /* 填权限 */
    current_euid_egid(&euid, &egid);
    new->cuid = new->uid = euid;
    new->gid = new->cgid = egid;
    new->deleted = false;

    /* ★ 双索引插入:idr 树 + key 哈希表 */
    idx = ipc_idr_alloc(ids, new);                 /* 先进 idr 树 */
    idr_preload_end();

    if (idx >= 0 && new->key != IPC_PRIVATE) {
        err = rhashtable_insert_fast(&ids->key_ht, &new->khtnode,
                                     ipc_kht_params);  /* 再进 key_ht */
        if (err < 0) {
            idr_remove(&ids->ipcs_idr, idx);       /* key_ht 失败要回滚 idr */
            idx = err;
        }
    }
    if (idx < 0) {
        new->deleted = true;
        spin_unlock(&new->lock);
        rcu_read_unlock();
        return idx;
    }

    ids->in_use++;
    if (idx > ids->max_idx)
        ids->max_idx = idx;
    return idx;
}
```

([util.c:278-327](../linux/ipc/util.c#L278-L327))

这里能看到 7.3 节讲的"双索引"在写入时的体现:**先插 idr,再插 key_ht;key_ht 插失败要回滚 idr**。这是"双索引必须保持一致"的标准写法。注意整个函数**调用者已经持有 `ids->rwsem` 写锁**,所以这里不需要再锁表,只需要锁单个对象(`new->lock`)——这是 SysV IPC 锁分层的设计:`rwsem` 保护整张表(增删对象时),`kern_ipc_perm->lock` 保护单个对象(读写对象内容时)。这种"粗粒度表锁 + 细粒度对象锁"的分层,让大量并发的 `msgsnd`/`msgrcv`(只锁单个对象)不会被一次 `msgget`(要表写锁)阻塞太久。

> **钉死这件事**:ipc ns 内部的并发设计是经典的"两层锁"——`ids->rwsem`(读写信号量)保护表的**结构变化**(增删对象),`kern_ipc_perm->lock`(自旋锁)保护单个对象的**数据读写**。表的读多写少(`msgsnd` 不改表结构),所以用 rwsem 而不是自旋锁。这种锁分层在后面 cgroup 各章会反复出现(`cgroup_mutex` 保护树结构,`css_set_lock` 保护 css_set)。

---

## 7.7 unshare / setns:ipc ns 的运行时切换

`copy_ipcs` 是 fork 路径(子进程从父亲复制/新建 ns)。但运行时还有两条路径:**unshare**(从当前进程剥出新 ns)和 **setns**(加入已有 ns)。这两条路径同样会调到 `copy_ipcs`。

### unshare 路径

[`unshare_nsproxy_namespaces`](../linux/kernel/nsproxy.c#L213)([nsproxy.c:213](../linux/kernel/nsproxy.c#L213))在 unshare 系统调用里被调,它检查 `CLONE_NEWIPC`:

```c
/* nsproxy.c:213(简化) */
int unshare_nsproxy_namespaces(unsigned long unshare_flags,
        struct nsproxy **new_nsp, struct cred *new_cred, struct fs_struct *new_fs)
{
    if (!(unshare_flags & (CLONE_NEWNS | CLONE_NEWUTS | CLONE_NEWIPC |
                           CLONE_NEWNET | CLONE_NEWPID | CLONE_NEWCGROUP |
                           CLONE_NEWTIME)))
        return 0;                                  /* 没要任何新 ns */

    /* 权限检查 */
    if (!ns_capable(user_ns, CAP_SYS_ADMIN))
        return -EPERM;

    *new_nsp = create_new_namespaces(unshare_flags, current, user_ns, ...);
    /* create_new_namespaces 内部对每种 NEW* 标志调对应的 copy_*,
       其中 CLONE_NEWIPC 调 copy_ipcs -> create_ipc_ns */
    ...
}
```

([nsproxy.c:213-237](../linux/kernel/nsproxy.c#L213-L237))

`CLONE_NEWIPC` 在掩码里(219 行),所以 `unshare(CLONE_NEWIPC)` 会让当前进程脱离父 ipc ns,获得一组全新的空 IPC 表。

### setns 路径

setns 通过 `/proc/<pid>/ns/ipc` 这个符号链接加入另一个进程的 ipc ns。底层的 `proc_ns_operations` 实现在 [ipc/namespace.c:251](../linux/ipc/namespace.c#L251):

```c
/* ipc/namespace.c:251 */
const struct proc_ns_operations ipcns_operations = {
    .name     = "ipc",
    .type     = CLONE_NEWIPC,
    .get      = ipcns_get,
    .put      = ipcns_put,
    .install  = ipcns_install,
    .owner    = ipcns_owner,
};
```

([namespace.c:251-258](../linux/ipc/namespace.c#L251-L258))

这是一张**函数指针表**(和 cgroup 的 `struct cgroup_subsys` 是同一种设计模式)——nsproxy 的通用 setns 路径不认识 ipc ns 的内部细节,只通过 `proc_ns_operations` 这组回调操作任意 ns。`ipcns_get`/`ipcns_install` 的实现也很直接:

```c
/* ipc/namespace.c:214 */
static struct ns_common *ipcns_get(struct task_struct *task)
{
    struct ipc_namespace *ns = NULL;
    struct nsproxy *nsproxy;

    task_lock(task);                              /* ★ task_lock 保护读取 */
    nsproxy = task->nsproxy;
    if (nsproxy)
        ns = get_ipc_ns(nsproxy->ipc_ns);         /* 引用计数 +1 */
    task_unlock(task);

    return ns ? &ns->ns : NULL;
}

/* ipc/namespace.c:233 */
static int ipcns_install(struct nsset *nsset, struct ns_common *new)
{
    struct nsproxy *nsproxy = nsset->nsproxy;
    struct ipc_namespace *ns = to_ipc_ns(new);

    if (!ns_capable(ns->user_ns, CAP_SYS_ADMIN) ||
        !ns_capable(nsset->cred->user_ns, CAP_SYS_ADMIN))
        return -EPERM;                            /* 必须对目标 ns 和当前 cred 都有 CAP_SYS_ADMIN */

    put_ipc_ns(nsproxy->ipc_ns);                  /* 释放旧的 */
    nsproxy->ipc_ns = get_ipc_ns(ns);             /* 换成新的 */
    return 0;
}
```

([namespace.c:214-244](../linux/ipc/namespace.c#L214-L244))

这里两个细节值得注意。

**细节一:`task_lock` 保护读取**。`ipcns_get` 用 `task_lock(task)` 而不是全局锁来读 `task->nsproxy`。这是因为 `switch_task_namespaces` 切视图时也只拿 `task_lock`([nsproxy.c:239-248](../linux/kernel/nsproxy.c#L239-L248))——锁的是任务自己的 `alloc_lock`,不是任何全局锁。这让"读某个进程的 nsproxy"和"切某个进程的 nsproxy"之间是互斥的,但和"切其他进程的 nsproxy"完全不冲突。这是 Linux namespace 子系统并发性好的一个关键设计:视图切换的锁粒度是**单个 task**,不是全局。

**细节二:双重权限检查**。`ipcns_install` 要求**同时**对目标 ns 的 `user_ns` 和当前 cred 的 `user_ns` 有 `CAP_SYS_ADMIN`。这是为了堵一个洞:如果只检查一方,容器内的恶意进程可能通过 setns 跳到别的 ipc ns 里。两道 capability 检查确保只有真正有特权的进程才能切 ipc ns。

---

## 7.8 一个微妙的限制:`CLONE_NEWIPC` 与 `CLONE_SYSVSEM` 互斥

[`copy_namespaces`](../linux/kernel/nsproxy.c#L151)([nsproxy.c:151](../linux/kernel/nsproxy.c#L151))里有一段不那么显眼但很重要的检查:

```c
/* nsproxy.c:168-177 */
/*
 * CLONE_NEWIPC must detach from the undolist: after switching
 * to a new ipc namespace, the semaphore arrays from the old
 * namespace are unreachable.  In clone parlance, CLONE_SYSVSEM
 * means share undolist with parent, so we must forbid using
 * it along with CLONE_NEWIPC.
 */
if ((flags & (CLONE_NEWIPC | CLONE_SYSVSEM)) ==
    (CLONE_NEWIPC | CLONE_SYSVSEM))
    return -EINVAL;
```

([nsproxy.c:168-177](../linux/kernel/nsproxy.c#L168-L177))

这段注释非常清楚:`CLONE_SYSVSEM` 让父子进程**共享 SysV 信号量的 undo list**——undo list 是进程退出时自动回滚信号量操作的机制(比如进程拿了信号量没释放就 crash,内核帮它释放)。如果同时 `CLONE_NEWIPC`(子进程进新 ipc ns)又 `CLONE_SYSVSEM`(子进程和父亲共享 undo list),会出现什么?子进程的 undo list 指向**父亲那个 ipc ns 里的信号量**,但子进程在新 ipc ns 里**根本看不到那些信号量**(因为表是隔离的)。等子进程退出时,内核尝试在它的 ipc ns 里回滚父亲 ns 的信号量——要么找不到对象,要么找错对象。这是逻辑矛盾,内核直接禁止这个组合。

> **钉死这件事**:这个 `EINVAL` 检查揭示了 ipc ns 隔离的一个深层约束——**undo list 跨 ns 引用是不可能的**。namespace 的隔离是"表"的隔离,任何"跨表的引用"都必须在创建时被切断。这种"要么完全切断、要么完全共享"的二元选择,是 namespace 设计的一贯原则(类比 mnt ns 的挂载传播:shared 要么真共享、private 要么真私有,不存在"半共享")。

---

## 7.9 技巧精解:ipc ns 的延迟回收 —— 为什么不能同步释放

这一章最值得单独拆透的技巧,不是 `ids` 表的设计(那已经够直球),而是**ipc ns 的销毁路径为什么这么绕**。它展示了 Linux 内核工程里"延迟回收 + RCU + work queue"这套组合拳的一个典型用例。

### 问题:mqueuefs 跨 ns 的引用 race

ipc ns 的销毁入口是 [`put_ipc_ns`](../linux/ipc/namespace.c#L198)([namespace.c:198](../linux/ipc/namespace.c#L198)),当最后一个引用者退出时被调。直觉上,这里应该直接释放 `struct ipc_namespace`——但内核没有这么做。看代码:

```c
/* ipc/namespace.c:198(简化,完整注释见 L182-L207) */
void put_ipc_ns(struct ipc_namespace *ns)
{
    if (refcount_dec_and_lock(&ns->ns.count, &mq_lock)) {
        mq_clear_sbinfo(ns);
        spin_unlock(&mq_lock);

        if (llist_add(&ns->mnt_llist, &free_ipc_list))
            schedule_work(&free_ipc_work);        /* ★ 异步! */
    }
}
```

([namespace.c:198-207](../linux/ipc/namespace.c#L198-L207))

引用计数降到 0 时,**不直接释放**,而是把 ns 挂到一个无锁链表 `free_ipc_list` 上,然后 `schedule_work` 调度一个 work 线程稍后释放。这个 work 是 [`free_ipc`](../linux/ipc/namespace.c#L167)([namespace.c:167](../linux/ipc/namespace.c#L167)):

```c
/* ipc/namespace.c:167 */
static void free_ipc(struct work_struct *unused)
{
    struct llist_node *node = llist_del_all(&free_ipc_list);
    struct ipc_namespace *n, *t;

    llist_for_each_entry_safe(n, t, node, mnt_llist)
        mnt_make_shortterm(n->mq_mnt);            /* 标记 mqueuefs 挂载为短期 */

    /* ★ 等待一个 RCU 宽限期 */
    synchronize_rcu();

    llist_for_each_entry_safe(n, t, node, mnt_llist)
        free_ipc_ns(n);                           /* 真正释放 */
}
```

([namespace.c:167-180](../linux/ipc/namespace.c#L167-L180))

两步:**先标记 mqueuefs 挂载,等一个 RCU 宽限期,然后才真正释放 ns**。为什么这么绕?

[namespace.c:182-197](../linux/ipc/namespace.c#L182-L197) 的注释解释得很清楚:

> If this is the last task in the namespace exiting, and it is dropping the refcount to 0, then it can race with a task in another ipc namespace but in a mounts namespace which has this ipcns's mqueuefs mounted, doing some action with one of the mqueuefs files. That can raise the refcount. So dropping the refcount, and raising the refcount when accessing it through the VFS, are protected with mq_lock.

翻译过来:即使本 ipc ns 里最后一个进程退出了(引用计数降到 0),**别的 ipc ns 里的进程也可能通过 mqueuefs 的挂载访问到这个 ns 的 POSIX 消息队列**(把 mqueuefs 挂到自己的 mounts ns 里)。这种跨 ns 的 VFS 访问会**临时把引用计数加回去**。

### 如果朴素地写:同步释放会撞什么墙

想象一个朴素的实现:

```c
/* 朴素的、糟糕的写法(示意,非源码) */
void put_ipc_ns(struct ipc_namespace *ns)
{
    if (refcount_dec_and_test(&ns->count)) {
        free_ipc_ns(ns);   /* 直接释放! */
    }
}
```

撞墙场景:

1. 时刻 T1:ipc ns A 的最后一个进程退出,`put_ipc_ns` 把引用计数从 1 降到 0,`free_ipc_ns(A)` 立刻释放 A 的内存——包括 `mq_mnt` 指向的 mqueuefs superblock 信息。
2. 时刻 T2(几乎同时):另一个 mounts ns 里的进程 B(它把 A 的 mqueuefs 挂到了自己这边)正要 `mq_open` 一个队列。它走 VFS 路径,经过 mqueuefs 的 superblock,准备 `get_ipc_ns(A)` 把引用计数 +1。
3. **race**:`free_ipc_ns(A)` 已经把 A 释放了,B 拿到的 `ns` 指针指向已释放内存,**use-after-free**。B 接下来对已释放 ns 的访问,要么读到乱码(数据被别人复用),要么直接 panic。

这是经典的"释放 vs 引用计数自增"竞争——本质是"判断是否归零"和"实际释放"之间存在窗口。

### Linux 怎么解决:锁 + work queue + RCU 三件套

内核用三件套组合拳:

**第一件套:`mq_lock` 把"判断归零"和"清空 mqueuefs 状态"做成原子**。`refcount_dec_and_lock` 在引用计数降到 0 时**同时**持有 `mq_lock`,然后 `mq_clear_sbinfo` 在锁内把 mqueuefs 的 superblock 标记为"不允许新打开"。这样,任何想通过 VFS 新访问 mqueuefs 的路径(也要拿 `mq_lock` 才能增加引用计数)要么在 `put_ipc_ns` 之前完成(已经持有引用,后续安全),要么在 `mq_clear_sbinfo` 之后开始(看到 superblock 已标记,直接拒绝)。窗口被关闭。

**第二件套:`schedule_work` 延迟到进程上下文**。为什么不在 `put_ipc_ns` 里直接调 `free_ipc_ns`?因为 `put_ipc_ns` 可能在不允许睡眠的上下文里被调(比如某些 RCU 回调路径),而 `free_ipc_ns` 要做的事——`mntput`(可能触发 mqueuefs unmount)、`synchronize_rcu`(等宽限期)——都是可能睡眠的操作。把它推到 work queue 里,就是把它放到允许睡眠的进程上下文。

**第三件套:`synchronize_rcu` 等"正在执行的访问"全跑完**。即使 `mq_lock` 关闭了"新访问"的窗口,但**已经开始但还没结束**的访问呢?比如某个进程已经 `get_ipc_ns(A)` 成功(引用计数 +1 了),正在 `msgsnd`——它持有了指针但还没来得及 `put_ipc_ns`。等它释放时引用计数又变 1 了,然后它再调 `put_ipc_ns`,引用计数才真归零。但这有个鸡生蛋问题:第一次 `put_ipc_ns` 怎么知道还有人在用?

答案就是 `synchronize_rcu`——它等待**所有在 RCU 读临界区里**的访问都退出。SysV IPC 的对象访问路径(`ipc_obtain_object_idr` 等)都在 `rcu_read_lock()` 内,所以 `synchronize_rcu` 返回后,所有"已经开始但还没退出读临界区"的访问都已经退出了,这时再 `free_ipc_ns` 就安全了。

> **反面对比**:如果不用 work queue + RCU,只靠 `mq_lock`,能保证正确性吗?保证不了——`mq_lock` 是自旋锁,持有期间不能睡眠,但 `free_ipc_ns` 要调 `mntput` 可能触发文件系统 unmount(必睡眠)、要 `kfree`(可以原子但还有其他清理)。所以必须先释放锁、再延迟到 work queue、再用 RCU 等已经在临界区里的访问退出。**锁(关新访问)+ work(允许睡眠)+ RCU(等老访问)**,这三件套分工明确,缺一不可。

> **钉死这件事**:ipc ns 的延迟回收是 Linux 内核"引用计数 + RCU + work queue"组合的一个教科书级用例。**锁解决"新访问"窗口,RCU 解决"进行中访问"窗口,work queue 解决"睡眠上下文"问题**。这种"分阶段、各司其职"的回收设计在 cgroup 子系统里也大量出现(`css_set` 的 `put_css_set` 也是延迟到 work queue 释放),第 9 章 P2-09 会专门拆。

---

## 7.10 `free_ipc_ns`:销毁三张表的最后一步

最后看真正释放时做什么——[`free_ipc_ns`](../linux/ipc/namespace.c#L146)([namespace.c:146](../linux/ipc/namespace.c#L146)):

```c
/* ipc/namespace.c:146(简化) */
static void free_ipc_ns(struct ipc_namespace *ns)
{
    mntput(ns->mq_mnt);                /* 释放 mqueuefs 挂载 */
    sem_exit_ns(ns);                   /* 清空 SEM 表 */
    msg_exit_ns(ns);                   /* 清空 MSG 表 */
    shm_exit_ns(ns);                   /* 清空 SHM 表 */

    retire_mq_sysctls(ns);             /* 注销 mqueue sysctl */
    retire_ipc_sysctls(ns);            /* 注销 ipc sysctl */

    dec_ipc_namespaces(ns->ucounts);   /* user 的 ns 计数 -1 */
    put_user_ns(ns->user_ns);          /* 释放对 user ns 的引用 */
    ns_free_inum(&ns->ns);             /* 释放 ns inode 号 */
    kfree(ns);                         /* 真正释放 struct ipc_namespace */
}
```

([namespace.c:146-164](../linux/ipc/namespace.c#L146-L164))

三个 `*_exit_ns` 函数把对应表里的所有 IPC 对象逐个销毁。以 `msg_exit_ns`([msg.c:1330](../linux/ipc/msg.c#L1330))为例:

```c
/* ipc/msg.c:1330 */
void msg_exit_ns(struct ipc_namespace *ns)
{
    free_ipcs(ns, &msg_ids(ns), freeque);             /* 逐个销毁消息队列 */
    idr_destroy(&ns->ids[IPC_MSG_IDS].ipcs_idr);      /* 销毁 idr 树 */
    rhashtable_destroy(&ns->ids[IPC_MSG_IDS].key_ht); /* 销毁 key 哈希表 */
    percpu_counter_destroy(&ns->percpu_msg_bytes);
    percpu_counter_destroy(&ns->percpu_msg_hdrs);
}
```

([msg.c:1330-1337](../linux/ipc/msg.c#L1330-L1337))

[`free_ipcs`](../linux/ipc/namespace.c#L123)([namespace.c:123](../linux/ipc/namespace.c#L123))是通用的"遍历一张表逐个销毁"函数——拿写锁、用 idr 遍历每个对象、调传入的 `free` 回调(`freeque`/`freeary`/`do_shm_rmid`)。这种"通用遍历 + 类型特定回调"的写法,是 SysV IPC 三种原语共用一套基础设施的体现。

> **钉死这件事**:ipc ns 的销毁是"先销对象、再销表、再销 ns 结构"三层。`free_ipcs` 销对象(拿表写锁),`idr_destroy`/`rhashtable_destroy` 销表(释放索引结构),`kfree(ns)` 销 ns 结构。三层各自独立,顺序不能颠倒——必须先销完对象再销表(否则表里的指针变成野指针),必须先销完表再销 ns(否则 ns 里的字段被释放后表就找不到了)。这种"构造与销毁严格分层"的工程美学,在 Linux 内核里随处可见。

---

## 7.11 视图二分法视角:ipc ns 在哪一面

回到全书二分法:**视图隔离(namespace)vs 资源控制(cgroup)**。ipc ns 毫无疑问属于**视图**那一面——它改的是"进程能找到哪些 IPC 对象",不改"进程能用多少 IPC 资源"。

但要补一个重要的边界说明。ipc ns 内部有一组 sysctl 参数(`msg_ctlmni`/`shm_ctlmax` 等),它们看起来像"资源限制",但它们的本质是**视图级别的默认值**——每个 ipc ns 有自己的一组 sysctl,容器进程看到的 `/proc/sys/kernel/msgmni` 是自己 ns 的值。这些参数确实限制了容器内的 IPC 用量,但它们和 cgroup 的资源控制是**两个层次**:

- **ipc ns 的 sysctl**:粗粒度的"系统级上限",类似 ulimit。每个 ns 独立,但不能动态调整(要改 sysctl)。
- **cgroup 的资源控制**:细粒度的"按 cgroup 记账",动态、可分层、可精确到字节/page/操作。

所以"容器能用多少 IPC 资源"这个问题,真正的答案是"ipc ns 的 sysctl + cgroup 的 memory 子系统共同决定"——前者是 namespace 自带的默认上限,后者是 cgroup 的精确记账。这又一次印证了本书的核心论断:**namespace 和 cgroup 必须一起用,才构成完整的容器**。

> **钉死这件事**:ipc ns 属于"视图"那一面,它隔离 IPC 的**命名空间**(key/id),不直接控制 IPC 的**用量**(那是 sysctl 和 cgroup 的事)。理解这一点,就不会把"ipc ns 的 sysctl 限制"误认为是 cgroup 的资源控制——前者是默认值,后者是精确记账。

---

## 7.12 ipc ns 的边界:它不隔离什么

诚实地讲清楚 ipc ns **不**做什么,和讲它做什么一样重要。

**ipc ns 隔离**:
- SysV IPC(消息队列、信号量、共享内存)的对象表。
- POSIX 消息队列(mqueue)。
- 相关的 sysctl(`/proc/sys/kernel/msgmni`、`/proc/sys/kernel/shmmax` 等)。
- `/proc/sysvipc/`(每个 ipc ns 看到自己的 IPC 列表)。

**ipc ns 不隔离**:
- **网络 socket**——那是 net ns 的事(虽然 socket 和 IPC 都叫"进程间通信",但内核里是两套子系统)。
- **管道(pipe/FIFO)**——管道不归属任何 ns,它通过 fd 传递。
- **eventfd / eventpoll / signalfd** 等"匿名 fd"——同样靠 fd 传递,不靠 key。
- **D-Bus / Unix domain socket 的"逻辑命名"**——这些是应用层的命名,内核不管。
- **System V 共享内存的物理页**——虽然 `shmget` 的 key 被隔离了(不同 ns 看不到对方的 SHM 段),但一旦某段共享内存被 `shmat` 映射到进程地址空间,这块物理内存**仍然受 cgroup 的 memcg 管控**(记到所属 cgroup 账上)。所以"用多少共享内存"不是 ipc ns 说了算。

一个常见的误解是"ipc ns 隔离了所有 IPC"——错。它只隔离**靠 key/id 全局命名的老式 SysV IPC 和 POSIX mqueue**。现代 Linux 应用更多用 socket、管道、eventfd,这些不在 ipc ns 的管辖范围内。

> **钉死这件事**:ipc ns 的隔离边界是"key/id 命名空间"。它管 SysV IPC 和 POSIX mqueue,不管 socket/管道/eventfd。所以如果你写的应用用的是 Unix domain socket 做进程间通信,ipc ns 对你几乎无影响——这也解释了为什么现代容器里 ipc ns 的存在感很弱(很多应用根本不用 SysV IPC)。

---

## 章末小结

这一章我们从"为什么必须隔离 SysV IPC"开始,看清了全局 key 在容器场景下的灾难,然后钻进 `struct ipc_namespace` 看它怎么用三张 `ids` 表实现隔离,接着走完 `copy_ipcs` → `create_ipc_ns` → `ipc_init_ids` 的建表路径,讲清了 ID 编码(index + seq)的妙处,最后拆透了 ipc ns 销毁的延迟回收设计(锁 + work queue + RCU 三件套)。

回到全书二分法:ipc ns 属于**视图**那一面——它隔离 IPC 的命名空间(key/id),不改 IPC 的用量上限(那是 sysctl + cgroup 的事)。它和前面几章的 mnt/pid/net/uts ns 一样,本质是"给 task_struct 的 nsproxy 换一个 ipc_ns 指针",让进程只看见自己盒子里的 IPC 对象。不同之处在于,ipc ns 内部装的不是一个简单结构,而是三张完整的资源表——这让它的"建/销"路径比 uts ns 复杂得多。

### 五个"为什么"清单

1. **为什么必须隔离 SysV IPC?** 因为 SysV IPC 用全局 key 命名对象,不隔离的话容器之间会 key 撞车,甚至跨容器数据泄漏(同一 key 拿到同一对象)。隔离的本质是"给每个 ns 一组独立的 key 空间"。
2. **为什么 `struct ipc_namespace` 里是三张 `ids` 表?** 因为 SysV IPC 有三种原语(信号量/消息队列/共享内存),它们各自的 id 空间是独立的(同一个 id 值可以同时是一个信号量和一个消息队列),所以分三张表。用一个宏 `ids[3]` + `IPC_SEM_IDS/IPC_MSG_IDS/IPC_SHM_IDS` 下标区分。
3. **为什么每张 `ipc_ids` 里有 idr 和 rhashtable 两套索引?** 因为 IPC 对象有两个访问入口——`msgget(key)` 用 key 找(走 key_ht),`msgsnd(id)` 用 id 找(走 ipcs_idr)。两套索引并行,空间换时间,避免某一路径线性扫描。
4. **为什么 ipc ns 的销毁要走 work queue + RCU?** 因为 mqueuefs 可以被别的 mounts ns 跨 ns 挂载,导致引用计数 race。锁(`mq_lock`)关"新访问"窗口,work queue 把销毁放到可睡眠上下文,RCU 等"进行中访问"退出。三件套组合,缺一不可。
5. **为什么 `CLONE_NEWIPC` 和 `CLONE_SYSVSEM` 不能同时用?** 因为 `CLONE_SYSVSEM` 要求父子共享 undo list,但 `CLONE_NEWIPC` 让子进程看不到父亲的信号量表——undo list 跨 ns 引用是逻辑矛盾,内核直接禁止。

### 想继续深入往哪钻

- 源码:[`ipc/namespace.c`](../linux/ipc/namespace.c) 全文不长(259 行),建议通读;[`include/linux/ipc_namespace.h`](../linux/include/linux/ipc_namespace.h) 看 `struct ipc_namespace` 的完整定义;[`ipc/util.c`](../linux/ipc/util.c) 的 `ipc_init_ids`/`ipc_addid`/`ipcget` 是 SysV IPC 的核心基础设施。
- 三种原语各自的 init/exit 函数:[`ipc/sem.c`](../linux/ipc/sem.c) 的 `sem_init_ns`/`sem_exit_ns`、[`ipc/msg.c`](../linux/ipc/msg.c) 的 `msg_init_ns`/`msg_exit_ns`、[`ipc/shm.c`](../linux/ipc/shm.c) 的 `shm_init_ns`/`shm_exit_ns`。
- 观测 IPC:`ipcs -q`(消息队列)、`ipcs -s`(信号量)、`ipcs -m`(共享内存)、`ipcs -q -i <id>`(看单个对象)。这些命令默认读 `/proc/sysvipc/*`,在容器里看到的是当前 ipc ns 的对象。
- 自己造 ipc ns:`unshare -i bash` 然后跑 `ipcs`,你会看到空表——证明新 ipc ns 是"清空视图"而非"复制视图"。
- ID 编码细节:读 [`ipc/util.h`](../linux/ipc/util.h) 的 `IPCMNI_SHIFT`/`ipcid_to_idx`/`ipcid_to_seqx` 注释,看 CONFIG_CHECKPOINT_RESTORE 下的 `IPCMNI_EXTEND_SHIFT`(扩展模式,24 位 index)。
- mqueuefs 跨 ns 挂载的细节涉及 `ipc/mq.c`(本地 sparse 未解压),可在线看 [elixir.bootlin.com/linux/v6.9/source/ipc/mq.c](https://elixir.bootlin.com/linux/v6.9/source/ipc/mq.c) 的 `mq_init_ns` 和 `mq_clear_sbinfo`。

### 引出下一章

讲完 7 种 namespace 里的 5 种(mnt/pid/net/uts/ipc),namespace 视图隔离的拼图还差最后、也是最重要的一块:**user namespace**。前面所有 ns 有一个共同限制——要 `CAP_SYS_ADMIN` 才能创建,所以容器虽然隔离了视图,但容器里的 root 仍然是宿主的 root,一旦容器逃逸就直接接管整机。user namespace 用一张 `uid_map` 映射表,把"容器里的 root"和"宿主的 nobody"解耦——这是容器安全的基石,也是 namespace 子系统设计最精巧的一章。下一章,我们从 `kuid_t` 这个"内核全局 uid"和 `make_kuid`/`from_kuid` 这对转换函数讲起,正式进入 user namespace。
