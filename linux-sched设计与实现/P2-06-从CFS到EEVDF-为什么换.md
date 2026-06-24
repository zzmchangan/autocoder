# 第六章 · 从 CFS 到 EEVDF:为什么换

> 篇:第 2 篇 · EEVDF 公平调度:下一个跑谁
> 主线呼应:第一篇立好了账本(`task_struct`/`sched_entity`/`rq`/`cfs_rq`),这一篇要回答全书的中央策略问题——**给定一个 `cfs_rq` 上挂着一堆 `sched_entity`,下一个该让谁跑?** 这是"公平"二字最集中的落点。Linux 从 2.6.23(2007)到 6.5(2023)用了整整 16 年的答案叫 **CFS**(Completely Fair Scheduler,完全公平调度器),它的招牌是"虚拟运行时间 vruntime + 红黑树选最小 vruntime"。但 6.6 起,CFS 被一个新的算法 **EEVDF**(Earliest Eligible Virtual Deadline First)取代。本章不写 EEVDF 的细节(那是下一章的事),只回答两个问题:**CFS 到底做对了什么,又卡在哪里,以至于内核要动这块统治了 16 年的地基?**

## 核心问题

**CFS 用 vruntime(把实际 CPU 时间按权重折算成虚拟时间)+ 红黑树(永远选 vruntime 最小的任务)实现了"按权重成比例分配 CPU"的直觉式公平。它统治了 Linux 16 年。但它对延迟的保障只能靠一个全局旋钮 `sched_latency_ns` 粗糙地撑着、对"体重大"的任务(cgroup 配高权重的组)在边界上不公平、睡眠唤醒后的 vruntime 补偿一直在打补丁。6.6 引入的 EEVDF 保留了 vruntime 这个"按权重折算"的好直觉,但把"选谁"的判据从"最小 vruntime"换成了"lag(欠账)+ eligible(资格)+ virtual deadline(截止)"三件套,一举拿掉了 CFS 的三个老毛病。**

读完本章你会明白:

1. CFS 的核心直觉:实际 CPU 时间按权重折算成虚拟时间(vruntime),永远让 vruntime 最小的任务跑——这是"按权重成比例"最直白的工程化。
2. vruntime 的折算公式 `delta_vruntime = delta_exec * NICE_0_LOAD / weight`,以及为什么这个公式能实现"权重翻倍 = 虚拟时间走半快 = 拿双倍 CPU"。
3. CFS 的三个老毛病:延迟保障只能靠 `sched_latency_ns` 粗调、睡眠唤醒的 vruntime 补偿(几个 feature flag 打补丁)、对体重大任务边界不公平。
4. EEVDF 为什么是"换地基"而不是"打补丁":它保留了 vruntime 折算的好直觉,但把"选谁"的判据彻底换了。
5. ★ 对照第 7 本:CFS/EEVDF 这种"按权重折算虚拟时间"的公平,在 Go runtime 的 GMP 里**根本没有对应物**——goroutine 默认无优先级、无权重,全靠 work-stealing 平衡,这是两种调度哲学的差异。

> **逃生阀**:如果你只想搞懂"EEVDF 怎么挑下一个",可以跳到第 7 章。本章是"为什么要换"的史料和动机,讲清 CFS 的好直觉和它的天花板,为第 7 章的 EEVDF 三件套铺好路。vruntime 这个概念你必须懂,因为它在 EEVDF 里**仍然活着**——EEVDF 没有抛弃 vruntime,只是不再只看它。

---

## 6.1 一句话点破

> **CFS 把"公平"翻译成了一句大白话:让 vruntime 最小的任务跑。vruntime 把实际 CPU 时间按权重折算,谁的 vruntime 落后谁就该跑——简单、直觉、统治了 16 年。但它回答不了"这个任务该多快被调度一次"(延迟保障),也回答不好"睡了一觉回来该补多少"(唤醒补偿)。EEVDF 在 vruntime 之上加了 lag 和 deadline,把"公平"从一维(vruntime)升到了三维(lag/vruntime/deadline),才补上了 CFS 答不出的那几问。**

这是结论,不是理由。本章倒过来拆:先看 CFS 的 vruntime 是怎么把"按权重成比例"变成代码的,再看它在哪里撞墙,最后看 EEVDF 换了什么、留了什么。

---

## 6.2 CFS 的直觉:把"公平"翻译成"vruntime 最小者优先"

### 6.2.1 提出问题:公平到底怎么落到代码里

第一篇讲了,每个 CPU 核有一个 `rq`,`rq` 里挂着 `cfs_rq`(普通任务的公平子队列),`cfs_rq` 上挂着一堆 `sched_entity`(可能是任务,也可能是组,见 P1-02)。现在问题来了:`cfs_rq` 上有 N 个可运行任务,每个任务有权重(`se->load.weight`,由 nice 值查表得到,见第 8 章),**下一个该让谁跑?**

最朴素的公平是"轮流":A 跑一段、B 跑一段、C 跑一段……但任务有权重,nice -20 的任务该比 nice 0 的任务多拿 CPU。怎么把"权重"揉进"轮流"?

### 6.2.2 CFS 的答案:vruntime——把实际时间按权重折算成虚拟时间

CFS(2007 年由 Ingo Molnar 合入)给了一个极其漂亮的工程化答案:**给每个任务一个 `vruntime`(virtual runtime,虚拟运行时间),每次任务实际跑了 `delta_exec` 纳秒,它的 vruntime 就增长 `delta_vruntime`,但增长量按权重折算——权重大的任务 vruntime 走得慢,权重小的任务 vruntime 走得快。然后,调度器永远选 `cfs_rq` 上 vruntime 最小的任务跑。**

折算公式(权重越大,vruntime 走得越慢):

```
              NICE_0_LOAD
delta_vruntime = delta_exec * ------------
                  weight
```

这里 `NICE_0_LOAD` 是 nice 0 任务的权重(在 6.9 源码里是 `1 << SCHED_FIXEDPOINT_SHIFT` = 1024,见 [`sched.h:158`](../linux/kernel/sched/sched.h#L158) 的 `#define NICE_0_LOAD`)。一个 nice 0 任务(权重 1024)跑 1ms,vruntime 涨 1ms;一个 nice -5 任务(权重 3121,查 [`core.c:11518`](../linux/kernel/sched/core.c#L11518) 的 `sched_prio_to_weight`)跑 1ms,vruntime 只涨 `1024/3124 * 1ms ≈ 0.33ms`——它走得更慢,所以会**长期保持较小的 vruntime**,于是调度器会反复选中它,它就拿走了多得多的 CPU。

> **钉死这件事**:vruntime 的妙处是它把"按权重成比例分配 CPU"翻译成了一个单调递增的标量——权重大的走得慢,长期 vruntime 落后,于是反复被选中。这把一个复杂的比例分配问题,变成了一个"找最小值"问题,可以用红黑树 O(log N) 高效维护。

这个折算在 6.9 的源码里仍然活着,函数叫 [`calc_delta_fair`](../linux/kernel/sched/fair.c#L296)([fair.c:296](../linux/kernel/sched/fair.c#L296)),EEVDF 完全沿用:

```c
/*
 * delta /= w
 */
static inline u64 calc_delta_fair(u64 delta, struct sched_entity *se)
{
	if (unlikely(se->load.weight != NICE_0_LOAD))
		delta = __calc_delta(delta, NICE_0_LOAD, &se->load);

	return delta;
}
```

> 注:`calc_delta_fair` 的注释 `delta /= w` 揭示了它的本质——把实际时间 `delta` 除以任务的权重比例(以 NICE_0_LOAD 为基准)。权重正好是 NICE_0_LOAD(1024)时,直接返回原值,是个快速路径。`__calc_delta` 内部用预计算的倒数(`sched_prio_to_wmult[]`,见 [`core.c:11536`](../linux/kernel/sched/core.c#L11536))把除法变成乘法,这是内核里"用查表换除法"的常规手段,第 8 章详讲。

每次时钟 tick 或任务入队/出队,调度器调 [`update_curr`](../linux/kernel/sched/fair.c#L1162)([fair.c:1162](../linux/kernel/sched/fair.c#L1162))更新当前任务的 vruntime:

```c
static void update_curr(struct cfs_rq *cfs_rq)
{
	struct sched_entity *curr = cfs_rq->curr;
	s64 delta_exec;
	...
	delta_exec = update_curr_se(rq_of(cfs_rq), curr);
	if (unlikely(delta_exec <= 0))
		return;

	curr->vruntime += calc_delta_fair(delta_exec, curr);
	update_deadline(cfs_rq, curr);
	update_min_vruntime(cfs_rq);
	...
}
```

`curr->vruntime += calc_delta_fair(delta_exec, curr)` 这一行,就是 CFS 公平的根——每次 tick 把刚跑的实际时间按权重折算,累加到 vruntime 上。这一行在 EEVDF 里**原封不动**留着,因为 vruntime 折算这个直觉是对的。

### 6.2.3 不这样会怎样:为什么不能直接用实际时间

> **不这样会怎样**:如果 `vruntime += delta_exec`(不折算,直接累加实际时间),那所有任务的 vruntime 走得一样快,调度器选最小 vruntime 就退化成"纯轮流"——nice 值完全没用,权重等于摆设。`renice` 改 CPU 占比的命令会失效。vruntime 折算是把"权重"塞进"选最小"判据的唯一办法。

### 6.2.4 选最小 vruntime:红黑树

CFS 把所有可运行任务的 `sched_entity` 挂在一棵红黑树(`cfs_rq->tasks_timeline`,见 [`sched.h:594`](../linux/kernel/sched/sched.h#L594)),按 vruntime 排序。选下一个就是取最左节点(O(log N) 维护,O(1) 取最左指针)。这是 CFS 在"选最小 vruntime"判据下的最优数据结构。

```
 CFS 的红黑树(老资料讲的都是这个,6.9 已不是这样选):

        cfs_rq->tasks_timeline (rb_root_cached)
                    │
                    ▼
              ┌─ vruntime=5 ─┐        ← 最左节点 = vruntime 最小 = 下一个跑
              │   任务 A      │
              └───────────────┘
                    │
        ┌───────────┴───────────┐
        ▼                       ▼
   ┌─ vruntime=12 ─┐      ┌─ vruntime=20 ─┐
   │   任务 B      │      │   任务 C      │
   └───────────────┘      └───────────────┘

  调度器:取最左 = 任务 A,跑一段后 A 的 vruntime 增长,
         重新插入,红黑树自平衡,再取新的最左。
```

**但是——这里有个关键点,所有讲 CFS 红黑树的老资料都过时了**:在 6.9(EEVDF)里,红黑树**仍然存在**,但**排序键变了**:不再是"按 vruntime 排序选最小",而是"按 deadline 排序 + augmented tree 维护子树最小 vruntime 用于剪枝"。6.9 的 [`pick_eevdf`](../linux/kernel/sched/fair.c#L884)(下一章详讲)遍历这棵按 deadline 排序的树,在 eligible(有资格)的节点里找最早 deadline 的——**不是取最左 vruntime 最小的**。这个差异是 CFS 和 EEVDF 在数据结构层面的分水岭,我们留到第 7、10 章拆透。

---

## 6.3 CFS 的三个老毛病

vruntime + 红黑树选最小,在"按权重成比例"这个维度上做得很好,统治了 16 年不是没道理。但它在另外三个维度上一直有毛病,内核社区打了十几年的补丁也没根治,最终促成了 6.6 的换地基。

### 6.3.1 毛病一:延迟保障只能靠一个全局旋钮 `sched_latency_ns`

CFS 的"延迟保障"(一个任务多久内必须被调度到一次)靠一个叫 `sched_latency_ns` 的目标延迟(6.9 之前默认 6ms,因 HZ 和配置而异)撑着:调度器尽量让所有任务在一个 `sched_latency_ns` 窗口内都跑一遍,于是把窗口切成 N 段,每段一个时间片。但这有几个硬伤:

- **它是全局的、被动的**:任务数 N 一变大,每个任务的时间片 = `sched_latency_ns / N` 就被拉薄;当 N 大到 `sched_min_granularity` 撑不住时,实际延迟会突破 `sched_latency_ns`——延迟保障只是"尽力而为",不是保证。
- **它和权重脱节**:一个 nice -20 的重任务和一个 nice 0 的轻任务在同一个 `sched_latency_ns` 窗口里,虽然时间片按权重分了,但"该多久跑一次"这件事和任务的紧急程度没有直接挂钩。延迟敏感的任务(交互式)只能靠唤醒抢占这种 side effect 间接拿到快速响应。
- **时间片是动态算出来的**(老的 `sched_slice` 函数 = `sched_latency_ns / nr_running` 按权重比例分配),任务一进一出,每个人的时间片都在抖动,缓存预热成本不可预期。

> **钉死这件事**:CFS 的延迟保障是"软目标",靠 `sched_latency_ns` 这个旋钮和一堆 sysctl 调参。它回答不了"这个具体任务必须在多长时间内被调度一次"这种硬约束。EEVDF 的 virtual deadline 把这件事变成了算法的一等公民:每个任务每次入队都有一个明确的截止时间,调度器优先调度快到期的——延迟保障从"尽力而为"升级成"算法内置"。

### 6.3.2 毛病二:睡眠唤醒的 vruntime 补偿——打了一堆补丁还是不准

CFS 有个老问题:一个任务睡了很久,醒来后它的 vruntime 还停在睡觉前的值——而别的任务一直在跑,vruntime 涨了一大截。如果直接让睡醒的任务入队,它的 vruntime 会"领先"一大段,要等很久才轮到它(饿死);反过来,如果不补偿,任务可以靠"睡一下醒一下"反复重置自己的 vruntime 到 `min_vruntime`,从而拿到超额 CPU(饿死别人)。

CFS 的修法是在唤醒时把任务的 vruntime 拉到 `min_vruntime - sched_latency_ns` 附近(给一点优待,因为交互式任务常常是睡一下醒一下),并用一堆 feature flag(`GENTLE_FAIR_SLEEPERS`、`START_DEBIT` 等)调这个补偿的力度。这些补丁在大多数情况下工作得还可以,但:

- **补偿量是经验值**,对不同负载(workload)表现不一,有时会让某些任务莫名拿到不公平的份额。
- **feature flag 越打越多**,维护者自己也承认这块是"调出来的"而不是"推出来的"。
- **它本质上是在 vruntime 这个一维标量上塞进两个目标**(公平 + 唤醒响应),互相打架。

EEVDF 的解法是引入 **lag**(任务"欠"多少 CPU 时间,见下一章):睡眠唤醒不再靠"把 vruntime 往前挪一点",而是靠"记住任务欠了多少 CPU,醒来后用 lag 决定它什么时候有资格跑(eligible)"。补偿从 vruntime 的一维挪到了 lag 这个独立维度,两个目标不再打架。

### 6.3.3 毛病三:对"体重大"的任务(cgroup 高权重)边界不公平

vruntime 折算是 `delta_vruntime = delta_exec * NICE_0_LOAD / weight`,这是个**除法**。权重很大的任务,nice -20(权重 88761)对 nice 0(权重 1024),比例 86:1——vruntime 走得极慢。但在 cgroup 场景下(第 19 章详讲),一个 `task_group` 整体作为一个 `sched_entity` 参与上层 `cfs_rq` 的竞争,它的权重由 `cpu.weight` 配置。当组内又有很多任务时,上层的 vruntime 折算精度(64 位整数运算)在极端权重比下会出现"四舍五入"导致的不公平累积——体重大的一方在长时间窗口下拿到的份额会偏离配置。

更本质的:CFS 的 vruntime 是一个**全局单调**的标量,它隐含假设"所有任务在同一个时间轴上比较 vruntime 是公平的"。但当一个任务的权重变化(renice)或者入队/出队时,整个时间轴的"重心"(`avg_vruntime`)会跳,导致别的任务的相对位置被动改变——这正是 EEVDF 引入 `avg_vruntime` 和 `lag` 要解的问题(下一章详讲)。

> **钉死这件事**:CFS 的三个毛病都源于同一个根:**vruntime 是个一维标量,它想同时表达"公平份额"(已分配多少)和"先后顺序"(谁该跑)和"延迟紧急度"(多快该跑)三件事,挤在一根轴上必然互相挤压。** EEVDF 的解法是把这三件事拆开:lag 表达"公平份额"、vruntime 仍是基础折算、deadline 表达"延迟紧急度",eligibility 串起 lag 和 vruntime。这是从一维到三维的升级。

---

## 6.4 EEVDF:换了什么、留了什么

6.6 合入的 EEVDF(Earliest Eligible Virtual Deadline First)是 Intel 的 Peter Zijlstra 和其他人主导的重写。它不是一个新加的调度类,而是**直接替换了 `fair_sched_class` 内部的算法**——`SCHED_NORMAL`/`SCHED_BATCH` 的任务仍然由 `fair_sched_class` 管,只是这个类内部"怎么选下一个"从 CFS 算法变成了 EEVDF 算法。从用户态看几乎没有变化(`nice`/`chrt`/`taskset` 命令都一样),但内核里 `fair.c` 的核心路径被改写。

### 6.4.1 留下的:vruntime 折算

CFS 最对的直觉——**按权重把实际时间折算成虚拟时间**——EEVDF 完全保留。`sched_entity` 里仍有 `vruntime` 字段(见 [`include/linux/sched.h:549`](../linux/include/linux/sched.h#L549) 的 `u64 vruntime;`),`update_curr` 里那行 `curr->vruntime += calc_delta_fair(delta_exec, curr)` 原封不动。权重大的任务 vruntime 仍然走得慢,这是"按权重成比例"的数学根。

EEVDF 保留 vruntime,是因为它**就是按权重折算虚拟时间的正确定义**——CFS 错的不是 vruntime 本身,而是"只看 vruntime"。

### 6.4.2 换掉的:"选谁"的判据

CFS 的判据:**`cfs_rq` 上 vruntime 最小的任务下一个跑**(红黑树取最左)。

EEVDF 的判据(下一章详讲,这里先给轮廓):

1. 每个任务有一个 **lag**(虚拟欠账,`se->vlag` 字段),表示它"该跑的份额"减去"实际跑的份额"的虚拟时间差。lag ≥ 0 表示任务被欠了(该跑而没跑够),lag < 0 表示它跑过头了。
2. 只有 **lag ≥ 0 的任务才有资格(eligible)跑**。lag < 0 的任务被排除在候选之外,直到时间推移、`avg_vruntime` 增长让它的 lag 转正。
3. 每个任务每次入队时算一个 **virtual deadline**(`se->deadline = se->vruntime + calc_delta_fair(se->slice, se)`),表示"这次它该在这之前跑完"。
4. 选下一个 = **在所有 eligible 的任务里,挑 deadline 最早的那个**。这就是名字 "Earliest Eligible Virtual Deadline First"。

红黑树仍然存在,但排序键从 vruntime 变成了 deadline,并且用 augmented tree(每个节点维护子树的 `min_vruntime`)来高效剪枝 eligible 检查。数据结构层面,6.9 的 `pick_eevdf` 是 O(log N) 的搜索,不是 CFS 的 O(1) 取最左——但它能在一次搜索里同时保证"eligible"和"最早 deadline"两个约束,这是 CFS 做不到的。

### 6.4.3 不这样会怎样:为什么必须换地基而不是打补丁

> **不这样会怎样**:如果只在 CFS 上打补丁(继续用"选最小 vruntime",但额外维护一个 deadline 字段,当 vruntime 最小的任务 deadline 还很远时让位给别的任务),会出现判据冲突:vruntime 说"A 该跑"(A 的 vruntime 最小),deadline 说"B 该跑"(B 的 deadline 更早),到底听谁的?判据必须是单一的、自洽的。EEVDF 把判据统一成"eligible 里最早 deadline",eligibility 用 lag 表达公平(谁被欠谁有资格),deadline 用 virtual deadline 表达延迟(谁的截止更近谁先跑),vruntime 退为基础量(算 lag 和 deadline 都要用它)——三维各司其职,自洽。这是换地基的必要性。

EEVDF 的理论根基是 1995 年 Stiliadis 和 Varma 的论文 *Earliest Eligible Virtual Deadline First: A Flexible and Accurate Mechanism for Proportional Share Resource Allocation*——Linux 在 2023 年才把它工程化进内核。理论 28 岁,工程化用了这么久,是因为 EEVDF 在保留 vruntime 折算的同时引入了 lag 和 deadline,实现复杂度远高于 CFS 的"选最小"——它需要维护 `avg_vruntime`、augmented rbtree、eligibility 检查,以及对睡眠唤醒、权重变化、cgroup 层级的一整套新处理(`place_entity` 的 lag 补偿、`reweight_eevdf` 的权重变化调整)。

> **钉死这件事**:CFS → EEVDF 不是"换了个更快的算法",而是"换了个表达能力更强的算法"。CFS 用一维 vruntime 表达公平,EEVDF 用三维(lag/vruntime/deadline)表达公平 + 资格 + 延迟。Linux 社区在 16 年里给 CFS 打了几十个补丁都没根治的三个毛病(延迟保障、唤醒补偿、权重边界公平),EEVDF 在算法层面一并解决。这就是为什么值得动这块统治了 16 年的地基。

---

## 6.5 EEVDF 带来的工程简化:一个常量时间片

CFS 时代,时间片是动态算的:老的 `sched_slice` 函数 = `sched_latency_ns * weight / 总权重`,每个任务的时间片都随队列里任务数和权重分布变化,抖动大。6.9 的 EEVDF 把这件事简化了——**时间片成了一个全局常量** [`sysctl_sched_base_slice`](../linux/kernel/sched/fair.c#L76)([fair.c:76](../linux/kernel/sched/fair.c#L76),默认 750000 纳秒 = 0.75ms):

```c
unsigned int sysctl_sched_base_slice = 750000ULL;
```

每次任务用完一个 slice,在 [`update_deadline`](../linux/kernel/sched/fair.c#L984)([fair.c:984](../linux/kernel/sched/fair.c#L984))里重新设:

```c
static void update_deadline(struct cfs_rq *cfs_rq, struct sched_entity *se)
{
	if ((s64)(se->vruntime - se->deadline) < 0)
		return;

	/*
	 * For EEVDF the virtual time slope is determined by w_i (iow.
	 * nice) while the request time r_i is determined by
	 * sysctl_sched_base_slice.
	 */
	se->slice = sysctl_sched_base_slice;

	/* EEVDF: vd_i = ve_i + r_i / w_i */
	se->deadline = se->vruntime + calc_delta_fair(se->slice, se);

	/* The task has consumed its request, reschedule. */
	if (cfs_rq->nr_running > 1) {
		resched_curr(rq_of(cfs_rq));
		clear_buddies(cfs_rq, se);
	}
}
```

注意一个微妙之处:`se->slice`(实际时间片,0.75ms)是常量,但 `se->deadline`(虚拟截止时间)是按权重折算的:`se->vruntime + calc_delta_fair(se->slice, se)`。体重大(nice 低)的任务,`calc_delta_fair` 把 0.75ms 折算成更小的虚拟增量,所以它的 deadline 更近(在虚拟时间轴上更紧迫),更早被 EEVDF 选中——**权重通过 deadline 的"紧迫度"发挥作用,不再通过"时间片长短"**。这是 EEVDF 和 CFS 在"权重如何起作用"上的一个本质差异(第 8 章详讲)。

> **钉死这件事**:CFS 里权重决定"时间片长短"(权重大的任务一次拿更长实际时间);EEVDF 里权重决定"deadline 紧迫度"(权重大的任务虚拟 deadline 更近,被更频繁地选中,但每次跑的实际时间片 `se->slice` 是固定的 0.75ms)。两种都能实现"权重翻倍 = 双倍 CPU",但路径不同。这是 EEVDF 把"延迟保障"算法内置的代价之一:时间片固定,意味着权重大的任务靠"更频繁被选中"而非"一次跑更久"来拿份额,响应曲线更平滑。

---

## 6.6 技巧精解:CFS vruntime 的直觉与缺陷——为 EEVDF 铺路

这一章我们不讲 EEVDF 的技巧(那是下一章),只拆透 CFS vruntime 这个"对了一半"的直觉:它对在哪里、错在哪里。这是理解 EEVDF 为什么必须换地基的前提。

### 技巧一:vruntime 折算——把比例分配变成"找最小"

CFS 最漂亮的一招,是把"按权重成比例分配 CPU"这个比例问题,转化成了"找一个单调递增标量的最小值"问题。具体怎么做?

定义每个任务的 vruntime 增长速率 `dv/dt = NICE_0_LOAD / weight`。两个任务 A、B,权重比 `w_A : w_B = 2 : 1`。设系统总虚拟时间 V 匀速增长。在任意时刻:

- A 的 vruntime 增长率是 `NICE_0_LOAD / w_A`
- B 的 vruntime 增长率是 `NICE_0_LOAD / w_B`

因为 A 的权重是 B 的两倍,A 的 vruntime 增长率是 B 的一半。调度器选 vruntime 最小的跑,所以会反复选 A(它的 vruntime 落后)——A 拿到的实际 CPU 时间约是 B 的两倍。这正好对应权重比 2:1。

数学上可以证明:在稳态下,各任务实际拿到 CPU 时间的比例严格等于其权重比。vruntime 是这个比例分配的 Lyapunov 函数——它单调地把系统推向公平。

> **反面对比**:如果不用 vruntime 折算,而用"权重大的任务多给几个时间片"这种朴素策略(比如 nice -20 的任务连续跑 3 个时间片再让出),那任务数一多、权重组合一复杂,比例就会失真(三任务权重比 3:5:7 怎么排时间片?)。vruntime 折算是连续的、平滑的,不需要"凑整",任意权重比都能精确实现。这是 CFS 的精髓。

### 技巧二:vruntime 的根本缺陷——一维挤三维

vruntime 这个标量,被 CFS 赋予了三重职责:

1. **表达公平份额**(已分配多少):谁的 vruntime 落后,谁被欠了。
2. **决定先后顺序**(谁该跑):选 vruntime 最小的。
3. **隐含延迟信息**(多快该跑):通过 `sched_latency_ns` 间接表达。

这三件事挤在一根轴上,就出现了第 6.3 节的三个毛病:延迟保障只能全局调、唤醒补偿要在 vruntime 上打补丁、权重边界精度不够。

EEVDF 的解法是把它拆成三个独立字段(都在 [`include/linux/sched.h:536`](../linux/include/linux/sched.h#L536) 的 `struct sched_entity` 里):

| 字段 | EEVDF 的职责 | CFS 里对应物 |
|------|-------------|-------------|
| `vruntime` (u64) | 按权重折算的虚拟时间,基础量 | `vruntime`(同) |
| `vlag` (s64) | 虚拟欠账(lag),决定 eligibility | 无独立字段,隐含在 vruntime 里 |
| `deadline` (u64) | 本次请求的虚拟截止时间 | 无,靠 `sched_latency_ns` 间接 |

加上 `cfs_rq` 里的 `avg_vruntime`/`avg_load`(见 [`sched.h:580`](../linux/kernel/sched/sched.h#L580))用于算 lag 和 eligibility,EEVDF 用三维独立表达公平、资格、延迟。这是从一维到三维的算法升级。

> **反面对比**:如果不引入 lag 和 deadline,只在 vruntime 上继续打补丁(比如唤醒时把 vruntime 拉前更多、权重变化时调 vruntime),补丁会越打越乱——因为 vruntime 同时被"公平份额"和"先后顺序"两个目标拉扯,改一个影响另一个。引入独立字段后,唤醒补偿动 `vlag`(不影响 vruntime 的单调性),延迟紧急度看 `deadline`(不挤占公平份额的判据),三者解耦。这是 EEVDF 工程美学的一面:把耦合的概念拆成独立字段,各管一摊。

> **钉死这件事**:CFS 的 vruntime 是个对了一半的直觉——"按权重折算虚拟时间"对,但"只看 vruntime 选最小"不够。EEVDF 在保留 vruntime 折算的同时,引入 lag(表公平份额)和 deadline(表延迟紧急度),把判据从一维升到三维。这是 CFS 统治 16 年后,内核在公平调度上的算法重写。下一章我们钻进 EEVDF 的三件套,看 lag、eligible、virtual deadline 各自怎么算、怎么用。

---

## 6.7 ★ 对照第 7 本:CFS/EEVDF 的"按权重公平"在 Go 里没有对应物

CFS/EEVDF 的核心是"按权重(`nice` → `sched_prio_to_weight`)成比例分配 CPU"。这个设计**在 Go runtime 的 GMP 里根本不存在**:

- Go 的 goroutine **默认没有优先级、没有权重**——所有 goroutine 在 Go 调度器眼里一视同仁。Go 没有 `nice` 等价物,也没有"权重折算成虚拟时间"的概念。
- Go 的"公平"靠的是 **runnext + 本地队列 FIFO + work-stealing**:每个 P 有个本地 runqueue(256 长度的环形数组)+ 一个 `runnext` 槽(下次一定跑这个),goroutine 按 FIFO 调度,空闲 P 偷别的 P 队列一半。这是一种**结构性公平**(每个 goroutine 进队列的机会均等),不是 CFS/EEVDF 那种**权重比例公平**。
- 如果用户要让某个 goroutine 跑得多,Go 没有原生 API——只能靠业务层(让那个 goroutine 主动 `runtime.Gosched()` 让出少一点,或者干脆 `GOMAXPROCS` 占住一个 P)。这和 Linux `renice`/`chrt` 一条命令改权重的体验完全不同。

这反映两种调度哲学的差异:**Linux 调度器是通用仲裁者,要服务从交互式到批处理的各种负载,必须有权重/优先级;Go runtime 是为 goroutine 这种"轻量协作"设计的,默认全公平,把优先级让给业务层处理。** 第 21 章的对照总表会钉死这组差异。

---

## 章末小结

这一章是"为什么换"的动机章,我们没有写 EEVDF 的算法细节(那是下一章),但立清了三样东西:

1. **CFS 的核心直觉**:vruntime 把实际时间按权重折算,选 vruntime 最小的任务跑——把"按权重比例分配 CPU"工程化成了"找最小值"。`calc_delta_fair` 这个折算 EEVDF 完全沿用。
2. **CFS 的三个老毛病**:延迟保障只能全局调 `sched_latency_ns`、唤醒补偿要在 vruntime 上打补丁、权重边界精度不够——根因是 vruntime 这个一维标量被塞了三件事(公平/先后/延迟)。
3. **EEVDF 换了什么**:判据从"vruntime 最小"换成"eligible 里最早 deadline";引入 `vlag`/`deadline` 两个独立字段,把公平、资格、延迟拆成三维。vruntime 折算保留,红黑树保留但排序键换成 deadline。时间片从动态 `sched_slice` 简化成常量 `sysctl_sched_base_slice`。

本章服务二分法的**策略**面:讲清"给定 `cfs_rq`,下一个跑谁"这个策略问题的算法演进——从 CFS 的"vruntime 最小"到 EEVDF 的"eligible + 最早 deadline"。

### 五个"为什么"清单

1. **CFS 的 vruntime 到底在做什么?** 把任务实际跑的 CPU 时间按权重折算(`delta_vruntime = delta_exec * NICE_0_LOAD / weight`),权重大的走得慢、长期 vruntime 落后被反复选中,从而拿走与其权重成比例的 CPU。
2. **为什么 CFS 选 vruntime 最小?** vruntime 是单调递增的标量,"选最小"等价于"选最被欠的",在稳态下严格实现权重比例分配。红黑树 O(log N) 维护,O(1) 取最左。
3. **CFS 的三个老毛病是什么?** (a)延迟保障只能靠全局 `sched_latency_ns` 软调;(b)睡眠唤醒的 vruntime 补偿靠一堆 feature flag,经验值,边界不准;(c)权重大的任务/cgroup 在精度和边界上累积不公平。根因是 vruntime 一维挤三维。
4. **EEVDF 保留了 vruntime 吗?** 保留了。vruntime 折算(`calc_delta_fair`、`update_curr` 里那行累加)EEVDF 完全沿用。它错在"只看 vruntime",不是 vruntime 本身。
5. **EEVDF 换了什么?** 判据从"vruntime 最小"换成"eligible(vlag ≥ 0)里 virtual deadline 最早"。引入 `se->vlag`/`se->deadline` 两个独立字段,红黑树按 deadline 排序 + augmented 维护子树 min_vruntime 用于剪枝。时间片简化成常量 `sysctl_sched_base_slice`。

### 想继续深入往哪钻

- 源码:`kernel/sched/fair.c` 的 [`calc_delta_fair`](../linux/kernel/sched/fair.c#L296)(L296)、[`update_curr`](../linux/kernel/sched/fair.c#L1162)(L1162)、[`update_deadline`](../linux/kernel/sched/fair.c#L984)(L984)、[`sysctl_sched_base_slice`](../linux/kernel/sched/fair.c#L76)(L76);`kernel/sched/core.c` 的 [`sched_prio_to_weight`](../linux/kernel/sched/core.c#L11518)(L11518);`include/linux/sched.h` 的 [`struct sched_entity`](../linux/include/linux/sched.h#L536)(L536,看 vruntime/vlag/deadline/slice 四字段)。
- 6.6 EEVDF 合入的 commit:`09096f0eed "sched/eevdf: Doc-ify the EEVDF roundup"` 系列(可在 `git log --grep=eevdf` 里翻)。
- `Documentation/scheduler/sched-eevdf.rst`(若你的内核源码树有)有 EEVDF 的设计说明。
- 老资料对比:任何讲 "CFS 红黑树按 vruntime 排序选最小" 的博客/书,在 6.6 之后都过时;本书以 6.9 源码为准。

### 引出下一章

CFS 的 vruntime 和它的三个毛病讲完了。下一章,我们正式钻进 EEVDF 的三件套——**lag(虚拟欠账)、eligibility(资格)、virtual deadline(虚拟截止)**。你会看到 `se->vlag` 怎么算、`avg_vruntime` 怎么维护、`entity_eligible` 怎么判、`pick_eevdf` 怎么在红黑树里找最早 deadline 的 eligible 任务。这是全书最硬核的一章,务必配 ASCII 时间轴把 lag/vruntime/deadline 的几何关系看清楚。
