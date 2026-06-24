# 第三章 · IRQ domain 与中断控制器抽象:irq_chip / irq_domain

> 篇:P1 中断与软中断
> 主线呼应:上一章我们看清了"CPU 看的是向量号"——CPU 每条指令后查中断引脚,被拉进内核时硬件自己查 IDT 表、跳到预设入口。但那一章结尾留了个扣子:CPU 只认 0~255 的向量号,而一个实际系统里,中断来源远不止 256 个——一台 ARM 服务器可能有上千根 GIC 中断线、一张 PCIe 网卡能用 MSI-X 申请几百个中断、再加上虚拟化里每个 vCPU 的 IPI、容器里直通的设备中断……**向量号根本不够分,而且驱动也不该直接看见向量号**。这一章讲 Linux 怎么在"硬件中断控制器五花八门"和"驱动只想要一个干净的 IRQ 号"之间,架起 `irq_chip`(对硬件的操作)和 `irq_domain`(硬件中断号 → Linux IRQ 号的映射)两层抽象。归属:**支撑**——它本身不处理事件,而是把"事件怎么进内核"的入口基础设施搭好。

## 核心问题

**硬件中断控制器五花八门:x86 的 LAPIC/IOAPIC、ARM 的 GIC v2/v3、PCIe 的 MSI/MSI-X、虚拟化的 vGIC、IPI、NMI……Linux 怎么把它们抽象成一套统一接口,让驱动只写 `request_irq(irq, handler, ...)` 而不用关心硬件细节?hwirq(硬件中断号)和 Linux 看到的 IRQ 号凭什么能解耦、还能在多个控制器级联时层层翻译?**

读完本章你会明白:

1. **三件套**:`irq_chip`(对硬件操作的函数指针集)、`irq_domain`(hwirq ↔ Linux IRQ 的映射工厂)、`irq_desc`(每个 IRQ 的描述符,挂驱动 action 链表)——这是 Linux 中断子系统的脊柱。
2. **为什么要解耦 hwirq 和 Linux IRQ**:不同控制器的 hwirq 空间互不重叠(GIC 的 hwirq 32 和 MSI 的 hwirq 32 是两个东西),必须有一层全局唯一的 Linux IRQ 号才能让驱动无感。
3. **三种 revmap 后端**(linear / radix-tree / hierarchy):不同规模的控制器选不同的数据结构,O(1) 查表 vs 稀疏大空间。
4. **hierarchical domain(层级域)**:GIC 当父域、MSI 当子域,一条中断的 hwirq 在各级被翻译,最终落到一个 Linux IRQ——这是 MSI/MSI-X 和虚拟化中断能优雅接入的关键技巧。

> **逃生阀**:如果你已经知道"hwirq 是硬件号、Linux IRQ 是内核全局号"这层关系,可以直接跳到 3.4 节(hierarchical domain 层级映射)和 3.5 节(技巧精解),那里是本章最硬的部分。3.1~3.3 是把三件套立清楚,即使你懂术语也建议扫一眼,因为后面 P1-04~P1-07 的中断上下文、上下半部都建立在这套结构之上。

---

## 3.1 一句话点破

> **驱动看到的"IRQ 号"是个内核全局的虚拟编号,它和硬件的"hwirq 中断号"是两回事——中间隔着一个 `irq_domain` 做翻译、一个 `irq_chip` 做硬件操作。这层解耦让 Linux 能把 GIC、LAPIC、IOAPIC、MSI 这些八竿子打不着的中断控制器,统一塞进同一套 `request_irq` / `handle_irq_event` 框架里,还能让它们级联起来。**

这是结论,不是理由。本章倒过来拆:先看"没有这层抽象会怎样",再把 `irq_chip`/`irq_desc`/`irq_domain` 三件套逐一立清楚,最后钻进最硬核的 hierarchical domain 层级映射。

---

## 3.2 为什么需要这层抽象:没有 `irq_domain` 的世界

先做一个思想实验。假设 Linux 没有 `irq_domain`,驱动要怎么用中断?最朴素的写法,驱动拿到一个"中断号",就直接 `request_irq(那个号, handler, ...)`。这个"那个号"是什么?

在单一中断控制器的简单世界(比如早年只有一颗 8259 PIC 的 PC),这个号就是 8259 的 hwirq(0~15),驱动也确实就这么写——`request_irq(3, serial_handler, ...)` 注册 COM2 的中断。但这个世界在三个方向上立刻崩塌:

**第一,不同控制器的 hwirq 空间冲突。** ARM GIC 的 SPI 中断 hwirq 从 32 开始、一个 PCIe 设备的 MSI 中断在它自己的 MSI 表里 hwirq 从 0 开始、x86 的 IOAPIC 又有自己的 pin 号。如果驱动直接拿 hwirq 当 IRQ 号,你会同时有"GIC 的 hwirq 32"和"某 PCIe 设备的 hwirq 32"两个中断,内核根本分不清该把哪个 handler 调起来。

**第二,控制器级联无法表达。** 一个 GPIO 控制器自己是个中断源(连到 GIC 的某根 hwirq 上),但它内部又分出 32 根 GPIO 中断线。这 32 根线没接到 GIC,而是"经过 GPIO 控制器再上 GIC"。如果只有 hwirq,GPIO 控制器的 32 根线在 GIC 看来只是"一根 hwirq",内核没法把它们区分开。

**第三,设备热插拔和 MSI 动态分配。** PCIe 设备插上来要分配几十个 MSI-X 中断,拔下去要回收;每插拔一次 hwirq 序号都会变,驱动不能假设"我的中断永远是 hwirq 42"。

> **不这样会怎样**:如果没有这层抽象,每个驱动都得自己维护一张"硬件中断号 → 我的处理函数"映射表,而且这张表必须全局唯一(否则 hwirq 冲突),还得在每次控制器配置变化时全系统重排——这在 SMP、热插拔、虚拟化场景下根本管不过来。这就是为什么 Linux 从 2.6 时代起引入 `irq_domain`,把"硬件号 → 内核号"这件事集中到中断控制器驱动里,让普通驱动只见 Linux IRQ、不见 hwirq。

> **所以这样设计**:Linux 给每个中断控制器一个 **`irq_domain`**(翻译域),它的核心职责就是把"这个控制器自己空间内的 hwirq"翻译成"内核全局唯一的 Linux IRQ 号"。控制器不同,domain 不同,翻译规则不同,但驱动只用 Linux IRQ,互相不打架。

---

## 3.3 三件套:`irq_chip` / `irq_desc` / `irq_domain`

把这套抽象铺开,核心是三个结构体。它们的关系先看一张总图,再逐一拆。

```
  Linux 中断子系统的脊柱(简化,仅画一条 IRQ):

   ┌──────────────────────────────────────────────────────────────┐
   │  irq_domain(GIC 的一个 domain,或 MSI domain,或 GPIO domain) │
   │  ┌────────────────────────────────────────────────┐          │
   │  │ ops(map/translate/alloc/free...)              │          │
   │  │ fwnode(固件节点:ACPI/DT/PCIe)               │          │
   │  │ parent ──────┐ (层级域:指向父 domain)        │          │
   │  │ revmap[] / revmap_tree  (hwirq → irq_data)   │          │
   │  └──────────────┼─────────────────────────────────┘          │
   └─────────────────┼────────────────────────────────────────────┘
                     │ (hwirq ↔ Linux virq 的翻译都在这)
                     ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  irq_desc  (每个 Linux IRQ 一个,全局数组/radix 索引)        │
   │  ┌──────────────────────────────────────────────────┐        │
   │  │ irq_common_data (跨 chip 共享:affinity/msi_desc)│        │
   │  │ irq_data ──────────┐                             │        │
   │  │   .irq   = Linux IRQ 号                          │        │
   │  │   .hwirq = 本控制器内的硬件号                    │        │
   │  │   .chip  ──────────┼──► irq_chip(GIC/IOAPIC...) │        │
   │  │   .domain ─────────┼──► 上面的 irq_domain        │        │
   │  │   .parent_data ────┘   (层级域:指向父级 irq_data)│       │
   │  │ handle_irq = 流控函数(handle_level_irq 等)     │        │
   │  │ action ──────────┐   (驱动注册的处理函数链表)   │        │
   │  └──────────────────┼─────────────────────────────┘          │
   └──────────────────────┼───────────────────────────────────────┘
                          ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  irqaction 链表(一个 IRQ 可被多个驱动共享 shared IRQ)       │
   │  ┌────────────┐   ┌────────────┐   ┌────────────┐            │
   │  │ handler    │   │ handler    │   │ handler    │  ...       │
   │  │ dev_id     │   │ dev_id     │   │ dev_id     │            │
   │  │ thread_fn  │   │ thread_fn  │   │ thread_fn  │            │
   │  └────────────┘   └────────────┘   └────────────┘            │
   └──────────────────────────────────────────────────────────────┘

   irq_chip(操作集,每个中断控制器一份):
   ┌──────────────────────────────────────────────────────────────┐
   │ name = "GICv3"                                               │
   │ irq_mask / irq_unmask / irq_ack / irq_eoi / irq_set_affinity │
   │ irq_set_type / irq_compose_msi_msg / irq_nmi_setup ...       │
   └──────────────────────────────────────────────────────────────┘
```

### 3.3.1 `irq_chip`:对硬件的操作集

中断控制器千差万别,但对内核来说,要调用的硬件操作其实就那么几类:**屏蔽(mask)/ 解屏蔽(unmask)/ 应答(ack)/ 结束(eoi)/ 设亲和性(set_affinity)/ 设触发类型(set_type)**。Linux 把这些操作抽成一组函数指针,就是 [`struct irq_chip`](../linux/include/linux/irq.h#L501-L551):

```c
/* include/linux/irq.h:501 (简化,挑关键回调) */
struct irq_chip {
    const char  *name;
    void        (*irq_ack)(struct irq_data *data);
    void        (*irq_mask)(struct irq_data *data);
    void        (*irq_mask_ack)(struct irq_data *data);
    void        (*irq_unmask)(struct irq_data *data);
    void        (*irq_eoi)(struct irq_data *data);
    int         (*irq_set_affinity)(struct irq_data *data,
                                    const struct cpumask *dest, bool force);
    int         (*irq_set_type)(struct irq_data *data, unsigned int flow_type);
    int         (*irq_set_wake)(struct irq_data *data, unsigned int on);
    void        (*irq_compose_msi_msg)(struct irq_data *data, struct msi_msg *msg);
    /* ... 还有 startup/shutdown/nmi_setup/ipi_send 等,合计 30+ 个回调 */
    unsigned long flags;
};
```

> 见 [`include/linux/irq.h:501-551`](../linux/include/linux/irq.h#L501-L551)。这是全部用函数指针的"操作集"——典型的 C 风格多态(和调度器的 `sched_class`、VFS 的 `file_operations` 是同一套路)。

每个中断控制器驱动自己定义一个 `irq_chip` 实例,填上对应的硬件寄存器操作。比如 GIC v3 的 `irq_mask` 就是往 GIC 的 GICD_ICENABLERn 寄存器写一位、`irq_eoi` 就是往 GIC_ICC_EOIR1 写一个号。这些细节内核通用层一点不关心,它只调 `chip->irq_mask(&desc->irq_data)`。

> **钉死这件事**:`irq_chip` 的所有回调第一个参数都是 `struct irq_data *`——这个 `irq_data` 里带着 `chip_data`(每 chip 私有指针,通常是控制器驱动的某个结构体),回调函数靠它找到自己该操作的寄存器。**这是 C 语言"对象 + 方法"的标准写法**:`irq_data` 是对象、`irq_chip` 是方法表、`chip_data` 是对象私有状态。

注意 `irq_chip` 还有一组标志位 [`enum { IRQCHIP_* }`](../linux/include/linux/irq.h#L571-L584),其中 `IRQCHIP_IMMUTABLE`(6.x 引入)尤其值得记住:它声明这个 chip 在运行期不会被通用层修改。这是给那些不能被 generic irq chip 框架偷偷改回调的驱动用的(比如一些被 RT-LINUX 或 RCU 特殊处理的根控制器),写驱动时如果不想被框架改,记得打这个标志。

### 3.3.2 `irq_desc`:每个 IRQ 的描述符

每个 Linux IRQ 号对应一个 [`struct irq_desc`](../linux/include/linux/irqdesc.h#L55-L108),它是这个 IRQ 在内核里的"户口本":挂了哪些驱动 handler、当前屏蔽状态、流控函数、统计数据。关键字段:

```c
/* include/linux/irqdesc.h:55 (简化) */
struct irq_desc {
    struct irq_common_data  irq_common_data;   /* 跨 chip 共享:affinity/msi_desc */
    struct irq_data         irq_data;          /* 本 chip 这一层的数据 */
    irq_flow_handler_t      handle_irq;        /* 流控函数:handle_level_irq 等 */
    struct irqaction        *action;           /* 驱动注册的 handler 链表 */
    unsigned int            depth;             /* 嵌套 disable 计数 */
    raw_spinlock_t          lock;              /* 保护 desc 的原始自旋锁 */
    unsigned int            irq_count;         /* 中断计数(查 spurious 用) */
    /* ... 还有 wake_depth / threads_oneshot / wait_for_threads 等 */
} ____cacheline_internodealigned_in_smp;
```

> 见 [`include/linux/irqdesc.h:55-108`](../linux/include/linux/irqdesc.h#L55-L108)。注意末尾的 `____cacheline_internodealigned_in_smp`——这是把 `irq_desc` 按 CPU 缓存行对齐,避免多核频繁访问不同 IRQ 的 desc 时互相把对方的缓存行踢脏(伪共享 false sharing)。**这是高频数据结构的标配技巧,和上一本《内存分配器》per-cpu cache、调度器 `rq` 的缓存行对齐是同一套思路**。

几个要点:

- **`action` 是个链表**:一个 IRQ 可以被多个驱动共享(`IRQF_SHARED`),每个驱动注册一个 `irqaction` 挂在链表上,中断来了内核挨个调一遍 handler,看谁"认领"了这次中断(返回 `IRQ_HANDLED`)。这就是共享中断的工作方式。
- **`handle_irq` 是流控函数(flow handler)**:它决定这个 IRQ "屏蔽-应答-跑 handler-解屏蔽"的顺序。电平触发(level)和边沿触发(edge)的流控不同,下面 3.3.4 详讲。
- **`irq_data` 嵌在 `irq_desc` 里,不是指针**:`irq_desc` 的 `irq_data` 字段是 inline 的,`irq_data` 反过来通过 `container_of` 也能找到 `desc`(见 [`irqdesc.h:125`](../linux/include/linux/irqdesc.h#L125) `irq_data_to_desc`)。这样既省一次指针跳转(中断是热路径),又保证两者生命周期一致。
- **`irq_desc` 在 `CONFIG_SPARSE_IRQ` 下是 radix-tree 索引**:老内核有个静态数组 `irq_desc[NR_IRQS]`,但 NR_IRQS 动辄几万,稀疏场景浪费巨大。6.x 默认 `CONFIG_SPARSE_IRQ=y`,`irq_to_desc(irq)` 走 radix-tree 查找(见 [`irqdesc.c:424`](../linux/kernel/irq/irqdesc.c#L424)),只为真正用到的 IRQ 分配 desc——这本身就是 per-object 按需分配的技巧。

### 3.3.3 `irq_domain`:hwirq ↔ Linux IRQ 的翻译工厂

这是本章主角。[`struct irq_domain`](../linux/include/linux/irqdomain.h#L150-L178) 的核心职责:**把"本控制器空间内的 hwirq"翻译成"内核全局唯一的 Linux IRQ 号(virq)"**,并维护这张反向映射表(revmap)。

```c
/* include/linux/irqdomain.h:150 (简化) */
struct irq_domain {
    const char              *name;
    const struct irq_domain_ops *ops;     /* map/translate/alloc/free 等回调 */
    void                    *host_data;   /* domain 私有数据 */
    unsigned int            flags;
    struct mutex            mutex;
    struct irq_domain       *root;        /* 层级域:指向根 domain 的锁 */
#ifdef CONFIG_IRQ_DOMAIN_HIERARCHY
    struct irq_domain       *parent;      /* 层级域:父 domain */
#endif
    /* 反向映射(revmap)数据 */
    irq_hw_number_t         hwirq_max;
    unsigned int            revmap_size;          /* linear 表大小 */
    struct radix_tree_root  revmap_tree;          /* radix-tree 后端 */
    struct irq_data __rcu   *revmap[] __counted_by(revmap_size);  /* linear 表 */
};
```

> 见 [`include/linux/irqdomain.h:150-178`](../linux/include/linux/irqdomain.h#L150-L178)。

一个 domain 有三种 revmap 后端,对应三种规模的中断控制器:

| 后端 | 创建接口 | 适用场景 | 查找复杂度 |
|------|---------|---------|-----------|
| **linear(线性表)** | `irq_domain_create_linear(size, ...)` | hwirq 数量小且密集(几十根线,如 GPIO 控制器) | O(1) 数组下标 |
| **radix-tree(基数树)** | `irq_domain_create_tree(...)` | hwirq 空间巨大且稀疏(如 MSI-X,可上千) | O(log n) |
| **hierarchy(层级)** | `irq_domain_create_hierarchy(parent, ...)` | 控制器级联(GIC 父 + MSI 子 / GPIO 父 + 子) | 见 3.4 节 |

linear 和 tree 的选择就藏在 [`__irq_domain_create`](../linux/kernel/irq/irqdomain.c#L130-L231) 的 `size` 参数里(见 [`irqdomain.c:147`](../linux/kernel/irq/irqdomain.c#L147)):

```c
/* kernel/irq/irqdomain.c:147 (简化) */
domain = kzalloc_node(struct_size(domain, revmap, size), ...);
/*                                                                */
/*  size > 0  → linear: struct_size 算上 revmap[size] 的数组尾巴  */
/*  size = 0  → tree:   不分配数组尾巴,查走 radix_tree_lookup    */
```

linear 后端把 `revmap[]` 数组直接挂在 domain 结构体尾巴上(`struct_size` 是个柔性数组技巧),hwirq 当下标用,一步到位。tree 后端不分配数组,改用 [`radix_tree_lookup`](../linux/kernel/irq/irqdomain.c#L969),适合 hwirq 编号稀疏(比如 MSI 的中断号可能跳着用)的场景。

> **为什么不是统一用 tree?** linear 表在 hwirq 密集时既快(一条 `mov` 指令取下标)又省内存(不用树节点开销);tree 在 hwirq 稀疏时省内存(只为用到的 hwirq 存节点)。**为不同规模选不同数据结构**——这是 Linux 内核反复出现的工程取舍,和 mm 的 buddy(页) vs slab(对象) vs rmap(反向映射)是同一思路:没有银弹,按场景选。

查找一条 hwirq 对应的 Linux IRQ,统一走 [`__irq_resolve_mapping`](../linux/kernel/irq/irqdomain.c#L939-L980):

```c
/* kernel/irq/irqdomain.c:964-969 (简化) */
rcu_read_lock();
if (hwirq < domain->revmap_size)
    data = rcu_dereference(domain->revmap[hwirq]);   /* linear */
else
    data = radix_tree_lookup(&domain->revmap_tree, hwirq);  /* tree */
rcu_read_unlock();
```

注意这里用 `rcu_read_lock` + `rcu_dereference`——**反向映射表的读路径是无锁的(用 RCU)**。中断是极热路径(网卡每收一个包都要查 revmap),如果在每次中断里都拿自旋锁查表,多核下锁竞争会成为瓶颈。RCU 让读者零开销、写者(分配/释放 IRQ 时)负责复制和回收。**这是中断子系统能在多核上扩展的关键技巧之一**。

### 3.3.4 流控函数:电平 vs 边沿,为什么是两个 handler

前面提到 `irq_desc->handle_irq` 是流控函数。它**不是驱动 handler**,而是决定"屏蔽-应答-跑驱动 handler-解屏蔽"的**顺序**——这个顺序由中断的触发类型(电平 level / 边沿 edge)和控制器特性决定。

最常用的两个:

- [`handle_level_irq`](../linux/kernel/irq/chip.c#L628-L655)(电平触发):进函数先 `mask_ack_irq`(屏蔽并应答,见 [`chip.c:631`](../linux/kernel/irq/chip.c#L631)),因为电平触发如果不立刻屏蔽,只要设备还拉着电平,中断会一直报;跑完 driver handler 再 `cond_unmask_irq` 解屏蔽(见 [`chip.c:650`](../linux/kernel/irq/chip.c#L650))。
- [`handle_edge_irq`](../linux/kernel/irq/chip.c#L787-L839)(边沿触发):边沿是"跳变",不立刻屏蔽也不会一直报,所以先 `irq_ack` 应答、再进 `do { handle_irq_event } while (PENDING)` 循环——边沿可能在中断处理期间又跳变一次,这个循环就是用来"处理期间又来边沿"的情况,见 [`chip.c:814-834`](../linux/kernel/irq/chip.c#L814-L834)。

```c
/* kernel/irq/chip.c:628 (简化,handle_level_irq 关键流程) */
void handle_level_irq(struct irq_desc *desc)
{
    raw_spin_lock(&desc->lock);
    mask_ack_irq(desc);              /* 1. 屏蔽并应答(电平必须先屏蔽) */
    if (!irq_may_run(desc))          /* 2. 检查是否被 affinity 迁移中 */
        goto out_unlock;
    if (!desc->action || irqd_irq_disabled(&desc->irq_data)) {
        desc->istate |= IRQS_PENDING;  /* 没有 handler:挂着,等注册 */
        goto out_unlock;
    }
    kstat_incr_irqs_this_cpu(desc);
    handle_irq_event(desc);          /* 3. 真正跑驱动 handler(链表) */
    cond_unmask_irq(desc);           /* 4. 处理完再解屏蔽 */
out_unlock:
    raw_spin_unlock(&desc->lock);
}
```

> 见 [`kernel/irq/chip.c:628-655`](../linux/kernel/irq/chip.c#L628-L655)。

为什么这一步要和驱动 handler 分开?因为**触发类型是控制器的属性,驱动 handler 是设备无关的逻辑**。同一个 GIC 既接电平触发的网卡、也接边沿触发的按钮,驱动 handler 写法一样(都是"读设备状态寄存器、处理数据"),但流控(屏蔽顺序)由控制器决定。`irq_chip` 的 `set_type` 设好触发类型后,`irq_set_chip_and_handler_name` 同时把对应的流控函数挂上(见 [`chip.c:1089-1096`](../linux/kernel/irq/chip.c#L1089-L1096)),从此驱动只见 `handle_irq_event` 这一层。

`handle_irq_event` 才是真正挨个调驱动 handler 的地方(见 [`handle.c:202-215`](../linux/kernel/irq/handle.c#L202-L215)),它内部调 [`__handle_irq_event_percpu`](../linux/kernel/irq/handle.c#L139) 遍历 `desc->action` 链表、调每个 `action->handler`(见 [`handle.c:158`](../linux/kernel/irq/handle.c#L158))。这一段我们在 P1-05(上下半部)详讲,这里只要记住:**`handle_irq`(流控)→ `handle_irq_event`(遍历 action)→ `action->handler`(驱动代码)**,这是中断进内核后的三层调用链。

---

## 3.4 层级域(hierarchical domain):GIC + MSI 级联

到这里,linear 和 tree 后端已经能解决"一个控制器内部 hwirq → Linux IRQ"的翻译。但现代系统最大的难题是**控制器级联**:中断要穿过好几层控制器才能到 CPU。这一节是本章最硬的部分。

### 3.4.1 级联长什么样

举两个真实场景:

**场景一:GPIO 控制器挂在 GIC 上。** 一个 SoC 的 GPIO 控制器自己有 32 根中断线(GPIO0~GPIO31),但这 32 根线没直接连 CPU,而是经过 GPIO 控制器汇聚后,接到 GIC 的某一根 hwirq 上(比如 GIC 的 hwirq 96)。那么:

- 从 GIC 看:hwirq 96 触发 → CPU 进内核 → 跑 GIC 的 mask/ack。
- 但 96 这一脚背后其实是 32 根 GPIO 之一,需要 GPIO 控制器再看自己的状态寄存器,确定是 GPIO0 还是 GPIO31 触发的,然后调对应驱动 handler。

**场景二:PCIe MSI-X。** 一张网卡用 MSI-X 申请了 64 个中断(RX 队列 0~63),这些中断在 PCIe 总线上是用"写某个内存地址"来触发的(MSI 的本质是一次 DMA 写),CPU 收到后由 IRQ 重映射硬件(IOMMU)翻译成一条中断给 GIC。所以:

- 网卡的 64 个 MSI 中断,在 MSI domain 里各有自己的 hwirq(0~63)。
- 每条 MSI 中断还要翻译成"写到哪个内存地址",这个地址又对应 IRQ 重映射硬件的一条表项。
- 重映射硬件的输出,才真正喂给 GIC 的某根 hwirq。

可以看到,一条中断从"设备发出"到"CPU 真正处理",可能要穿过 **MSI domain → IRQ 重映射 domain → GIC domain** 三层。每层都有自己的 hwirq 空间、自己的 `irq_chip` 操作。如果朴素地用"一个全局 domain",这三层的翻译规则没法统一表达。

### 3.4.2 `irq_domain` 的层级解法

Linux 的解法是**把每个控制器建成一个 `irq_domain`,再用 `parent` 指针把它们串成一条链**:

```c
/* include/linux/irqdomain.h:166 (层级域支持) */
#ifdef CONFIG_IRQ_DOMAIN_HIERARCHY
    struct irq_domain *parent;   /* 指向父 domain */
#endif
```

创建子域时把父域传进去,见 [`irq_domain_create_hierarchy`](../linux/kernel/irq/irqdomain.c#L1139-L1164):

```c
/* kernel/irq/irqdomain.c:1139 (简化) */
struct irq_domain *irq_domain_create_hierarchy(struct irq_domain *parent,
                                               unsigned int flags, unsigned int size,
                                               struct fwnode_handle *fwnode,
                                               const struct irq_domain_ops *ops,
                                               void *host_data)
{
    domain = __irq_domain_create(fwnode, size, size, 0, ops, host_data);
    if (domain) {
        if (parent)
            domain->root = parent->root;   /* 共用根 domain 的 mutex */
        domain->parent = parent;           /* 串父域 */
        domain->flags |= flags;
        __irq_domain_publish(domain);
    }
    return domain;
}
```

> 见 [`kernel/irq/irqdomain.c:1139-1164`](../linux/kernel/irq/irqdomain.c#L1139-L1164)。

注意一个细节:**层级域里所有 domain 共用根 domain 的 `mutex`**(`domain->root = parent->root`,见 [`irqdomain.c:1155`](../linux/kernel/irq/irqdomain.c#L1155))。这样分配一条 IRQ 时,可以从叶子锁到根,中间不会和另一条 IRQ 的分配死锁。**层级共用一把锁,是层级数据结构避免锁反转的标准技巧**。

每条 IRQ 在每一层都有一个对应的 `irq_data`(挂在各自的 `irq_desc->irq_data.parent_data` 链上),见 [`include/linux/irq.h:186-189`](../linux/include/linux/irq.h#L186-L189):

```c
/* include/linux/irq.h:186 (层级 irq_data 串联) */
#ifdef CONFIG_IRQ_DOMAIN_HIERARCHY
    struct irq_data *parent_data;   /* 指向上一层的 irq_data */
#endif
```

于是,一条 Linux IRQ 在多层 domain 上展开成一条 `irq_data` 链:`virq` 的 desc 里有一个根 `irq_data`(对应子域 MSI),它的 `parent_data` 指向中间层 `irq_data`(IRQ 重映射),再 `parent_data` 指向最底层 `irq_data`(GIC)。每一层的 `irq_data` 各自挂自己的 `chip`(MSI chip / 重映射 chip / GIC chip),`irq_mask` 时通用层会沿着这条链挨个调一遍,让每一层都做自己的屏蔽。

### 3.4.3 分配一条层级 IRQ:自顶向下递归

层级域的核心 API 是 [`__irq_domain_alloc_irqs`](../linux/kernel/irq/irqdomain.c#L1542-L1561),它从叶子 domain 开始,**递归地**向父 domain 申请 hwirq。简化后的核心逻辑见 [`irq_domain_alloc_irqs_locked`](../linux/kernel/irq/irqdomain.c#L1474-L1518):

```c
/* kernel/irq/irqdomain.c:1474 (大幅简化,只留层级递归主干) */
static int irq_domain_alloc_irqs_locked(struct irq_domain *domain, ...)
{
    virq = irq_domain_alloc_descs(...);     /* 1. 分配 Linux IRQ 号(desc) */
    irq_domain_alloc_irq_data(domain, virq, nr_irqs);  /* 2. 给每层建 irq_data */
    ret = irq_domain_alloc_irqs_hierarchy(domain, virq, nr_irqs, arg); /* 3. 递归 */
    for (i = 0; i < nr_irqs; i++)
        irq_domain_insert_irq(virq + i);   /* 4. 插入 revmap 反向映射 */
    return virq;
}
```

第 3 步 [`irq_domain_alloc_irqs_hierarchy`](../linux/kernel/irq/irqdomain.c#L1462-L1472) 调用的是本层 domain 的 `ops->alloc`,而**每一层的 `alloc` 实现里又会调 `irq_domain_alloc_irqs_parent` 向父 domain 申请**(典型实现见 [`irqdomain.c:1786`](../linux/kernel/irq/irqdomain.c#L1786) `return irq_domain_alloc_irqs_hierarchy(domain->parent, ...)`),于是递归地从叶子走到根,每一层分到一个 hwirq、配好自己的 `irq_chip`。整个过程像是**一次"中断的 DNS 解析"**:从设备的 MSI 描述出发,一层层向上问"给我一个 hwirq",每层分配完,叶子拿到一个完整的 Linux IRQ 号。

```mermaid
flowchart TD
    A["驱动请求 MSI 中断<br/>(pci_alloc_irq_vectors)"] --> B["MSI domain.alloc<br/>分 hwirq=0..63"]
    B -->|"irq_domain_alloc_irqs_parent"| C["IRQ 重映射 domain.alloc<br/>分 IRTE 表项"]
    C -->|"irq_domain_alloc_irqs_parent"| D["GIC domain.alloc<br/>分 GIC hwirq (如 32~95)"]
    D --> E["每层配好各自 irq_chip<br/>irq_data 串成 parent_data 链"]
    E --> F["返回唯一 Linux IRQ 号 virq<br/>插入每层 revmap"]
    F --> G["驱动拿到 virq<br/>request_irq(virq, handler, ...)"]

    classDef start fill:#dbeafe,stroke:#2563eb
    classDef layer fill:#fef3c7,stroke:#d97706
    classDef end fill:#dcfce7,stroke:#16a34a
    class A,G end
    class B,C,D,E,F layer
```

这就是为什么 MSI/MSI-X、虚拟化中断、GPIO 级联能在 Linux 里优雅接入:**它们都被建模成"一层 domain + 一个 irq_chip",通过 parent 串接,共用同一套 alloc/translate/activate 接口**。新增一种中断控制器,只要写一个 domain + 一个 chip,接上既有的父域,通用层代码一行都不用改。

---

## 3.5 技巧精解

这一节挑两个最硬核的技巧单独拆透:**(1)revmap 用 RCU 无锁读 + linear/tree 双后端;(2)hierarchical domain 的 `parent_data` 链与递归 alloc**。

### 技巧一:revmap 的 RCU 无锁读 + 双后端

中断是极热路径,每收一个包、每按一次键,内核都要从 hwirq 查到 Linux IRQ。这个查找如果走自旋锁,64 核机器上每核都查、锁竞争会直接吞掉中断子系统的吞吐。

Linux 的做法有两层巧思:

**第一,双后端按规模选**。linear 后端把 `revmap[]` 数组柔性挂在 domain 结构尾巴上(见 [`irqdomain.c:147`](../linux/kernel/irq/irqdomain.c#L147) 的 `struct_size(domain, revmap, size)`),hwirq 当下标一步到位,适合 GPIO 这种几十根线的密集控制器;tree 后端用 radix-tree,适合 MSI-X 这种上千根线且稀疏的场景。`__irq_resolve_mapping` 用一个 `if (hwirq < domain->revmap_size)` 切换两种后端(见 [`irqdomain.c:966-969`](../linux/kernel/irq/irqdomain.c#L966-L969))。

**第二,读路径全程 RCU 无锁**。读者只 `rcu_read_lock` + `rcu_dereference`(见 [`irqdomain.c:964-977`](../linux/kernel/irq/irqdomain.c#L964-L977)),不抢任何锁;写者(分配/释放 IRQ,见 [`irq_domain_set_mapping`](../linux/kernel/irq/irqdomain.c#L523-L537) 用 `rcu_assign_pointer`)负责发布和回收。

```c
/* kernel/irq/irqdomain.c:523 (写者发布,简化) */
static void irq_domain_set_mapping(struct irq_domain *domain,
                                   irq_hw_number_t hwirq,
                                   struct irq_data *irq_data)
{
    /* ... */
    if (hwirq < domain->revmap_size)
        rcu_assign_pointer(domain->revmap[hwirq], irq_data);   /* linear */
    else
        radix_tree_insert(&domain->revmap_tree, hwirq, irq_data);  /* tree */
}
```

> **为什么这套设计 sound**:RCU 保证读者看到的 `irq_data` 在读者读完前不会被释放(写者只能等所有读者退出 grace period 后才回收)。中断是异步的,可能正有一条中断在查 revmap,这时候另一核在释放这个 IRQ——RCU 让这个并发安全,而读者零开销。**没有 RCU,中断子系统的多核扩展性会塌掉一半**。

> **反面对比**:如果用一把全局自旋锁保护 revmap,64 核机器上每核每次中断都要抢这把锁,锁竞争会让中断处理的有效 CPU 时间被锁等待吃掉一大块。RCU + 双后端让读路径变成"一条 `mov` 取下标 + RCU 读临界区",这是"高频读、低频写"场景的标准答案——和上一本 mm 的 `ktime_get`(seqlock 读)、调度器的 `rq` per-CPU 化是同一思路:**把热点路径的锁消灭掉**。

### 技巧二:hierarchical domain 的 `parent_data` 链与递归 alloc

层级域的精妙在于**它把"一条中断穿过多层控制器"这件事,编码成了一条 `irq_data` 链 + 一组递归回调**。具体看几个点:

**1. 每条 Linux IRQ 在每层 domain 各有一个 `irq_data`,用 `parent_data` 串成链**(见 [`irq.h:186-188`](../linux/include/linux/irq.h#L186-L188))。所以 `irq_mask` 不是只调一个 chip,而是沿着这条链挨个调——`mask_irq` 在通用层会调当前 `irq_data->chip->irq_mask`,如果当前层没实现就回退到 `parent_data`(这是 `irq_chip` 的"继承"语义,和 C++ 的虚函数回退类似)。**一条 IRQ 的屏蔽操作会被多层 chip 各自执行一部分**,这就是 MSI 要"屏蔽 MSI 源 + 屏蔽重映射表项 + 屏蔽 GIC 这根线"三层都生效的实现。

**2. 分配走递归 alloc**。叶子 domain 的 `ops->alloc` 调 `irq_domain_alloc_irqs_parent` → 父 domain 的 `ops->alloc` → 再向上,直到根 domain。每一层分一个 hwirq、建一个 `irq_data` 挂到链上,最后 `irq_domain_trim_hierarchy` 把链整理整齐(见 [`irqdomain.c:1503`](../linux/kernel/irq/irqdomain.c#L1503))。这种"自底向上递归申请、每层建一层结构"的模式,和文件系统 VFS 的"逐层 lookup"是同构的。

**3. 共用根 domain 的锁**(见 [`irqdomain.c:1155`](../linux/kernel/irq/irqdomain.c#L1155) `domain->root = parent->root`)。层级域的 `mutex` 实际指向根 domain 的 `mutex`,这样分配一条层级 IRQ 时,从叶子到根一把锁保护全程,不会出现"叶子锁了、父域没锁、另一条 IRQ 在父域改了"的并发破坏。**层级共用根锁**是层级数据结构避免锁反转的标准做法(类似 cgroup 层级、VFS 的 inode 锁)。

> **反面对比**:如果不用层级 domain,每层控制器各自维护自己的 hwirq → handler 表,那么 MSI 这条中断要"先查 MSI 表 → 再查重映射表 → 再查 GIC 表",每层都要单独锁、单独查找,而且没法表达"这条 MSI 在 GIC 上对应哪根线"。更要命的是,新增一层(比如插入一个虚拟化的 vGIC)要改所有层的查找逻辑。**层级 domain 把"多层翻译"抽象成 parent 链 + 递归 alloc,新增一层只需写一个 domain 接进去**——这是 Linux 能优雅支持 GIC v2/v3、ITS、LPI、IRQ 重映射、虚拟化 vGIC、GPIO 级联这些千奇百怪拓扑的根本原因。

> **钉死这件事**:`irq_chip`(操作)+ `irq_domain`(翻译)+ `irq_desc`(描述符)三件套,加上 hierarchical domain 的 `parent`/`parent_data` 链,共同构成了 Linux 中断子系统的"对象模型"。任何新中断控制器进来,都是"实现一个 chip + 注册一个 domain(可选接到父域)",通用层(`handle_level_irq`/`handle_irq_event`/`request_threaded_irq`)一行不改——**这是 C 语言实现多态和可扩展架构的教科书级范例**,和 VFS 的 `file_operations`、调度器的 `sched_class` 是同一类设计。

---

## 章末小结

这一章是中断子系统的**支撑地基**,我们没有讲"事件怎么把 CPU 拉进内核"(那是 P1-02),也没讲"中断处理分上下半部"(P1-05),而是把"硬件中断控制器和内核之间"的那层抽象拆透——它本身不处理事件,但没有它,事件根本进不来。三件套(`irq_chip` / `irq_desc` / `irq_domain`)和层级映射是后面所有中断机制的脊柱:P1-04(中断上下文)的 `preempt_count` 要靠 `irq_data` 进入 hardirq 时计数、P1-05(上下半部)的 `handle_irq_event` 要遍历 `desc->action`、P1-06(softirq)的 `raise_softirq` 要在中断返回前由 `irq_exit` 触发——它们都建立在本章这套结构上。

本章服务的二分法那一面:**支撑**。`irq_chip`/`irq_domain` 既不"把控制权拉进内核",也不"内核主动向外驱动",它是让这两者能发生的**地基设施**。

### 五个"为什么"清单

1. **为什么 Linux 要把 hwirq 和 Linux IRQ 解耦?** 不同控制器的 hwirq 空间会冲突(GIC 的 hwirq 32 和 MSI 的 hwirq 32 是两个东西),而且控制器会级联、设备会热插拔。解耦后驱动只见全局唯一的 Linux IRQ,控制器差异封装在 domain 内部。
2. **`irq_chip` 和 `irq_domain` 各自管什么?** `irq_chip` 是"对硬件的操作集"(mask/unmask/ack/eoi/set_affinity 等函数指针),`irq_domain` 是"hwirq ↔ Linux IRQ 的翻译工厂"(含 ops 回调和 revmap 反向映射)。一个 chip 描述"怎么操作硬件"、一个 domain 描述"硬件号怎么翻译成内核号"。
3. **三种 revmap 后端怎么选?** linear(数组下标,适合几十根线密集的 GPIO)、radix-tree(基数树,适合上千根线稀疏的 MSI-X)、hierarchy(层级链,适合控制器级联)。按规模和稀疏度选不同数据结构,没有银弹。
4. **为什么层级域要共用根 domain 的 mutex?** 分配一条层级 IRQ 要从叶子递归到根,如果每层各锁各的,会出现叶子锁了父域没锁的并发破坏(锁反转)。共用根锁让一次分配全程持锁,简单又 sound。
5. **驱动为什么看不见 hwirq,只见 Linux IRQ?** 见 hwirq 会把控制器细节泄漏给驱动(GIC 的 hwirq 编号规则、MSI 的 hwirq 分配策略都不该是驱动关心的)。让驱动只见 Linux IRQ,中断控制器驱动可以自由调整 hwirq 分配,驱动代码不用改——这是抽象隔离的收益。

### 想继续深入往哪钻

- **源码入口**:从 [`kernel/irq/irqdomain.c`](../linux/kernel/irq/irqdomain.c) 的 [`__irq_domain_alloc_irqs`](../linux/kernel/irq/irqdomain.c#L1542) 顺藤摸瓜,看一条层级 IRQ 怎么从叶子递归到根;从 [`__irq_resolve_mapping`](../linux/kernel/irq/irqdomain.c#L939) 看 RCU 无锁查 revmap。再看 [`chip.c`](../linux/kernel/irq/chip.c) 的 [`handle_level_irq`](../linux/kernel/irq/chip.c#L628) / [`handle_edge_irq`](../linux/kernel/irq/chip.c#L787) 看流控差异;[`handle.c`](../linux/kernel/irq/handle.c) 的 [`__handle_irq_event_percpu`](../linux/kernel/irq/handle.c#L139) 看 action 链表怎么遍历。
- **真实 chip 实例**(arch/x86 未 sparse clone,描述作用):GIC v3 的 `irq_chip` 实现在 `drivers/irqchip/irq-gic-v3.c`(`gic_mask_irq`/`gic_unmask_irq`/`gic_eoi_irq`),MSI 的 domain 在 `kernel/irq/msi.c` 和 `drivers/pci/msi/`,GPIO 级联参考 `drivers/gpio/gpio-xxx.c` 的 `irq_domain_create_hierarchy` 用法。
- **观测**:`cat /proc/interrupts` 每行一个 Linux IRQ,显示计数 + 名称(`IRQ-8`/`eth0-TxRx-0` 等),看不出 hwirq;想看 domain 拓扑,`cat /sys/kernel/debug/irq/domains/hierarchy`(需要 `CONFIG_GENERIC_IRQ_DEBUGFS`)列出所有 domain 和它们的 parent 关系;`cat /sys/kernel/debug/irq/irqs/<N>` 看一条 IRQ 的 desc 细节(action/chip/触发类型/affinity)。
- **延伸**:Thomas Gleixner 写的 hierarchical irqdomain 设计文档在 `Documentation/core-api/genericirq.rst`;RCU 在 revmap 上的用法可对照 `Documentation/RCU/`;想理解 MSI 的本质(一次 DMA 写触发中断),看 PCIe 规范的 MSI/MSI-X 章节。
- **调参**:`irqaffinity=` 启动参数设置默认中断亲和性、`/proc/irq/<N>/smp_affinity` 手动调一条 IRQ 的 CPU 亲和性(写掩码)、`/proc/irq/<N>/smp_affinity_list` 用列表形式调、`irqbalance` 守护进程自动均衡中断到各核——这些操作最终都调 `irq_chip.irq_set_affinity`,落到具体控制器硬件寄存器。

### 引出下一章

我们把"硬件中断控制器怎么抽象、hwirq 怎么翻译成 Linux IRQ"讲清了。但有一个问题还没回答:**当 CPU 真的被中断拉进内核、`handle_arch_irq` 开始跑 `handle_level_irq`/`handle_irq_event` 时,这段代码跑在什么样的"上下文"里?它为什么不能 `sleep`、不能拿 `mutex`、连 `current` 都不是它?** 这个上下文叫**中断上下文(hardirq context)**,它是用 `preempt_count` 的一段 bit 位标记的。下一章我们从 `preempt_count` 的嵌套计数讲起,讲清为什么中断上下文不能睡眠、这背后"没有 task 结构可挂起"的本质——这是后面 softirq/workqueue 为什么要切两段(让能睡眠的工作延后到进程上下文)的根本理由。
