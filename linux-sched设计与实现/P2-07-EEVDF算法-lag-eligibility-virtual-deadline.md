# 第七章 · EEVDF 算法:lag、eligibility、virtual deadline

> 篇:第 2 篇 · EEVDF 公平调度:下一个跑谁
> 主线呼应:上一章讲了 CFS 的 vruntime 直觉和它的三个老毛病,以及 EEVDF 为什么必须换地基。这一章是**全书最硬核的一章**,我们钻进 EEVDF 的算法内核——**lag(虚拟欠账)、eligibility(资格)、virtual deadline(虚拟截止)**这三件套到底怎么算、怎么用、`pick_eevdf` 怎么在一棵按 deadline 排序的红黑树里 O(log N) 找出"eligible 且最早 deadline"的任务。读完这一章,你能真正讲清 6.6 之后 `kernel/sched/fair.c` 里那些 `vlag`/`avg_vruntime`/`deadline`/`entity_eligible`/`pick_eevdf` 到底在干什么。

## 核心问题

**EEVDF(Earliest Eligible Virtual Deadline First)用三个量决定"下一个跑谁":每个 `sched_entity` 维护 `vlag`(虚拟欠账,V - v_i,正的表示被欠了)和 `deadline`(本次请求的虚拟截止时间),`cfs_rq` 维护 `avg_vruntime`(所有在队任务的加权平均 vruntime,即 V)。一个任务 eligible(有资格被选中)当且仅当它的 lag ≥ 0(即 vruntime ≤ avg_vruntime)。选下一个 = 在所有 eligible 任务里挑 deadline 最早的。`pick_eevdf` 用 augmented rbtree(按 deadline 排序 + 每节点维护子树 min_vruntime)在 O(log N) 内完成这个搜索。**

读完本章你会明白:

1. EEVDF 的三个核心量:`vlag`(任务"欠"多少虚拟时间)、`avg_vruntime`(全队列的加权平均 vruntime V)、`deadline`(本次请求的虚拟截止)——它们的数学定义和 6.9 源码实现。
2. **eligibility**(资格)的判据:`vlag ≥ 0` ⇔ `vruntime ≤ avg_vruntime`,以及为什么这个判据能保证"被欠的任务先跑"。
3. **virtual deadline** 的算法:`deadline = vruntime + calc_delta_fair(slice, se)`,以及为什么 deadline 表达了"延迟紧急度"——权重大的任务 deadline 更近(被更频繁选中)。
4. `pick_eevdf` 的 O(log N) 搜索:augmented rbtree 按 deadline 排序,每个节点缓存子树 min_vruntime,递归剪枝跳过"整棵子树都不 eligible"的分支。
5. ★ 对照第 7 本:EEVDF 这种"虚拟截止时间"的延迟保障,在 Go GMP 里**完全没有**——Go 的 goroutine 没有 deadline 概念,延迟靠 runnext 槽和 work-stealing 间接保证,这是两种调度范式的本质差异。

> **逃生阀**:这是全书数学密度最高的一章。如果你对"为什么是这三个量"不感兴趣,只想知道代码怎么走,可以直接跳到 7.4 节看 `pick_eevdf` 的源码走读,再回头看前面的定义。但如果你想真正理解 EEVDF(而不是记住函数名),7.2 节的 lag 几何意义和 7.3 节的 eligibility 推导必须啃下来——这是 EEVDF 区别于 CFS 的全部精华。

---

## 7.1 一句话点破

> **EEVDF 把"下一个跑谁"分解成两个独立的问题:(a) 谁有资格跑(eligibility,用 lag ≥ 0 判,保证公平);(b) 在有资格的人里谁最紧急(deadline 最早,保证延迟)。CFS 把这两个问题混在 vruntime 一根轴上答,EEVDF 用 `vlag` 和 `deadline` 两个独立字段分开答,于是公平和延迟各自有了数学上一致的判据。**

这是结论,不是理由。本章倒过来拆:先给出三个量的定义和它们在 6.9 源码里的样子,再用 ASCII 时间轴讲清 lag/vruntime/avg_vruntime/deadline 的几何关系,然后推 eligibility 的等价判据,最后走读 `pick_eevdf` 的搜索过程。

---

## 7.2 三个核心量:定义与源码

### 7.2.1 符号约定(先把数学符号钉死)

EEVDF 文献和源码注释用一套固定符号,先把它们钉死,后面所有公式都用这套:

| 符号 | 含义 | 6.9 源码对应 |
|------|------|-------------|
| `w_i` | 任务 i 的权重 | `se->load.weight`(经 `scale_load_down` 折算) |
| `v_i` | 任务 i 的 vruntime(按权重折算的虚拟时间) | `se->vruntime` |
| `V` | 全队列的加权平均 vruntime | `avg_vruntime(cfs_rq)` |
| `lag_i` | 任务 i 的实际欠账 `= w_i * (V - v_i)` | (不直接存,由 `vlag` 表达) |
| `vl_i` | 任务 i 的虚拟欠账 `= V - v_i` | `se->vlag` |
| `r_i` | 任务 i 本次请求的实际时间(slice) | `se->slice`(=`sysctl_sched_base_slice`,0.75ms) |
| `vd_i` | 任务 i 本次请求的虚拟截止 `= v_i + r_i / w_i` | `se->deadline` |
| `S` | 系统已提供的总服务(加权时间) | (概念量,不直接存) |
| `s_i` | 任务 i 已获得的服务 `= w_i * v_i` | (概念量,不直接存) |

注意源码里存的是**虚拟**量(`vlag` 而非 `lag`,`vruntime` 而非 `service`),因为虚拟量避开了权重因子,公式更简洁。源码注释 [`fair.c:684`](../linux/kernel/sched/fair.c#L684) 明说:"lag_i = S - s_i = w_i * (V - v_i), However... we only track the virtual lag: vl_i = V - v_i"。

### 7.2.2 量一:`se->vruntime`——按权重折算的虚拟时间(沿用 CFS)

EEVDF 完全沿用 CFS 的 vruntime。每次任务实际跑 `delta_exec`,它的 vruntime 涨 `calc_delta_fair(delta_exec, se)`(见上一章 6.2.2)。这个量是 EEVDF 的基础,但它**不再单独决定"选谁"**——它只是算 `vlag` 和 `deadline` 的输入。

字段定义在 [`include/linux/sched.h:549`](../linux/include/linux/sched.h#L549):`u64 vruntime;`。更新在 [`update_curr`](../linux/kernel/sched/fair.c#L1174):`curr->vruntime += calc_delta_fair(delta_exec, curr);`。

### 7.2.3 量二:`se->vlag`——虚拟欠账(EEVDF 新增)

`vlag` 是 EEVDF 的灵魂。它表达"任务被欠了多少 CPU"(虚拟单位)。

定义:`vlag_i = V - v_i`,即"全队列平均 vruntime"减去"我的 vruntime"。

- `vlag > 0`:我的 vruntime 落后于平均(我跑得少,被欠了),我应该有资格跑。
- `vlag < 0`:我的 vruntime 领先于平均(我跑得多,跑过头了),我该歇歇。
- `vlag = 0`:正好公平。

字段定义在 [`include/linux/sched.h:550`](../linux/include/linux/sched.h#L550):`s64 vlag;`。它的更新发生在两个地方:

**(a) 出队时(dequeue/sleep)**——把当前 lag 记下来,等下次入队用。[`update_entity_lag`](../linux/kernel/sched/fair.c#L709)([fair.c:709](../linux/kernel/sched/fair.c#L709)):

```c
static void update_entity_lag(struct cfs_rq *cfs_rq, struct sched_entity *se)
{
	SCHED_WARN_ON(!se->on_rq);

	se->vlag = entity_lag(avg_vruntime(cfs_rq), se);
}
```

它调用 [`entity_lag`](../linux/kernel/sched/fair.c#L699)([fair.c:699](../linux/kernel/sched/fair.c#L699))算当前 lag 并做了**限幅**(clamp):

```c
/*
 * lag_i = S - s_i = w_i * (V - v_i)
 *
 * However, since V is approximated by the weighted average of all entities it
 * is possible -- by addition/removal/reweight to the tree -- to move V around
 * and end up with a larger lag than we started with.
 *
 * Limit this to either double the slice length with a minimum of TICK_NSEC
 * since that is the timing granularity.
 *
 * EEVDF gives the following limit for a steady state system:
 *
 *   -r_max < lag < max(r_max, q)
 */
static s64 entity_lag(u64 avruntime, struct sched_entity *se)
{
	s64 vlag, limit;

	vlag = avruntime - se->vruntime;
	limit = calc_delta_fair(max_t(u64, 2*se->slice, TICK_NSEC), se);

	return clamp(vlag, -limit, limit);
}
```

注意限幅 `clamp(vlag, -limit, limit)`,limit = max(2*slice, TICK_NSEC) 按权重折算。为什么要限幅?源码注释解释:V 是用加权平均近似的,任务的入队/出队/renice 会让 V 跳动,如果不在出队时把 lag 钳住,一个任务可能积累出"天文数字"的 vlag,醒来后长期霸占 CPU,严重破坏公平。限幅是 EEVDF 的一个工程加固(纯理论的 EEVDF 没有这个,是 Linux 工程化时加的)。

**(b) 在队时(in queue)**——`vlag` 在任务排队期间不主动重算,它是"上次出队时冻结的快照"。`avg_vruntime` 会随时间增长(别的任务在跑),但 `se->vlag` 字段不变——eligibility 检查时实时算 `V - v_i`(用当前的 `avg_vruntime`),不读 `se->vlag`。`se->vlag` 字段的用途主要是**入队时的 place 补偿**(下一小节和 7.5 节详讲)。

> **钉死这件事**:`se->vlag` 是个"冻结的 lag 快照",在出队时由 `update_entity_lag` 算出来并限幅,用于下次入队时的放置(place)。eligibility 的实时判据**不直接读** `se->vlag`,而是实时算 `avg_vruntime() - se->vruntime`(等价于 lag)。两个字段(`vlag` 和 `vruntime`)和 `avg_vruntime` 协同表达公平。

### 7.2.4 量三:`se->deadline`——本次请求的虚拟截止(EEVDF 新增)

每个任务入队时算一个 deadline,表示"本次请求(r_i)应该在虚拟时间 vd_i 之前完成"。算法在 [`place_entity`](../linux/kernel/sched/fair.c#L5170) 和 [`update_deadline`](../linux/kernel/sched/fair.c#L984):

```
vd_i = v_i + r_i / w_i
```

其中 `r_i / w_i` 用 `calc_delta_fair(r_i, se)` 计算(把实际时间按权重折算成虚拟时间)。源码 [`update_deadline`](../linux/kernel/sched/fair.c#L999)([fair.c:999](../linux/kernel/sched/fair.c#L999)):

```c
se->slice = sysctl_sched_base_slice;

/* EEVDF: vd_i = ve_i + r_i / w_i */
se->deadline = se->vruntime + calc_delta_fair(se->slice, se);
```

关键洞察:**权重大的任务(w_i 大),r_i / w_i 小,deadline 离当前 vruntime 更近(更紧迫)**。所以 EEVDF 会更频繁地选中权重大的任务——它通过"deadline 紧迫度"让权重发挥作用,而不是像 CFS 那样通过"时间片长短"。

字段定义在 [`include/linux/sched.h:540`](../linux/include/linux/sched.h#L540):`u64 deadline;`。

### 7.2.5 全队列的 V:`avg_vruntime` 怎么维护

`avg_vruntime`(即 V)是 EEVDF 公平判据的核心。它的定义:

```
         \Sum (v_i - min_vruntime) * w_i
V = min_vruntime + ----------------------------
                          \Sum w_i
```

减去 `min_vruntime` 是为了减小数值规模(避免 64 位溢出),`min_vruntime` 是个单调递增的基准。这个加权和由 `cfs_rq` 的两个字段增量维护(见 [`sched.h:580`](../linux/kernel/sched/sched.h#L580)):

- `cfs_rq->avg_vruntime`:`\Sum (v_i - min_vruntime) * w_i`(加权和,分子)
- `cfs_rq->avg_load`:`\Sum w_i`(总权重,分母)

每次任务入队/出队,调 [`avg_vruntime_add`](../linux/kernel/sched/fair.c#L628)/[`avg_vruntime_sub`](../linux/kernel/sched/fair.c#L638) 增量更新;每次 `min_vruntime` 涨,调 [`avg_vruntime_update`](../linux/kernel/sched/fair.c#L648) 补偿(因为分子里的 `(v_i - min_vruntime)` 会变小)。读取 V 用 [`avg_vruntime`](../linux/kernel/sched/fair.c#L660)([fair.c:660](../linux/kernel/sched/fair.c#L660)):

```c
u64 avg_vruntime(struct cfs_rq *cfs_rq)
{
	struct sched_entity *curr = cfs_rq->curr;
	s64 avg = cfs_rq->avg_vruntime;
	long load = cfs_rq->avg_load;

	if (curr && curr->on_rq) {
		unsigned long weight = scale_load_down(curr->load.weight);

		avg += entity_key(cfs_rq, curr) * weight;
		load += weight;
	}

	if (load) {
		/* sign flips effective floor / ceil */
		if (avg < 0)
			avg -= (load - 1);
		avg = div_s64(avg, load);
	}

	return cfs_rq->min_vruntime + avg;
}
```

注意一个细节:`curr`(当前正在跑的任务)不在 rbtree 里(它被 `__dequeue_entity` 摘出来跑了,见 [`set_next_entity`](../linux/kernel/sched/fair.c#L8558) 附近),所以读 V 时要把 `curr` 的贡献临时加回来。还有那个 `if (avg < 0) avg -= (load - 1);`——这是个**向下取整**的小技巧(注释叫 "left bias"),它保证 `avg_vruntime() + 0`(即 vruntime 恰好等于 V 的任务)的 `entity_eligible` 返回 true,这是 eligibility 单调性的需要(见 7.3.2)。

> **钉死这件事**:V(`avg_vruntime`)是 EEVDF 公平的标尺。它不是每个任务各自的量,而是全队列共享的"当前公平水位线"。任务通过与 V 比较得知自己被欠还是跑过头。维护 V 的代价是 O(1) 增量更新(入队/出队/min_vruntime 变动各一次),读取也是 O(1)(加权和除以总权重)。这是 EEVDF 在数据结构上比 CFS(只存 vruntime,不存 V)多出来的成本,但换来的是 eligibility 这个 CFS 没有的判据。

---

## 7.3 eligibility:lag ≥ 0 ⇔ vruntime ≤ V

### 7.3.1 提出问题:谁"有资格"被选中

EEVDF 名字里 "Eligible" 是一等公民。它的含义:不是所有在 `cfs_rq` 上的任务都该参与"选下一个"——跑过头的任务(lag < 0)应该被排除,直到时间推移让它的 lag 转正。

直觉:假设 A、B、C 三个任务,权重都一样。某时刻 A 跑了 3 个 slice、B 跑了 1 个、C 跑了 1 个。A 的 vruntime 领先于 B、C。下一轮选谁?如果按 CFS"vruntime 最小"选 B 或 C(它俩 vruntime 一样小),没问题——这是 EEVDF 也会做的。但设想一个边界:A 跑了 100 个 slice、B、C 各 1 个,然后 B、C 短暂睡眠又醒来——它们的 vruntime 可能被 place 补偿拉到 A 前面。CFS 会无条件让 B、C 跑(因为它们 vruntime 小),但这对 A 不公平(B、C 拿了补偿优待后可能反过来霸占)。EEVDF 的 eligibility 判据排除这种情形:A 虽然跑得多,但如果 B、C 的 vruntime 仍 ≤ V(全队平均),B、C eligible;一旦 B 或 C 的 vruntime > V(比如它跑了一小段后 vruntime 越过平均线),它立刻 ineligible,让位给 vruntime 还在平均线以下的任务。

### 7.3.2 推导:lag ≥ 0 ⇔ v_i ≤ V

源码注释 [`fair.c:716`](../linux/kernel/sched/fair.c#L716) 给了推导:

```
Entity is eligible once it received less service than it ought to have,
eg. lag >= 0.

lag_i = S - s_i = w_i*(V - v_i)

lag_i >= 0 -> V >= v_i
```

也就是说,`lag_i ≥ 0`(被欠)等价于 `V ≥ v_i`(我的 vruntime 不超过全队平均)。 eligibility 判据简化成"我的 vruntime 不超过 V"。源码 [`entity_eligible`](../linux/kernel/sched/fair.c#L749)([fair.c:749](../linux/kernel/sched/fair.c#L749)):

```c
int entity_eligible(struct cfs_rq *cfs_rq, struct sched_entity *se)
{
	return vruntime_eligible(cfs_rq, se->vruntime);
}
```

它调 [`vruntime_eligible`](../linux/kernel/sched/fair.c#L733)([fair.c:733](../linux/kernel/sched/fair.c#L733)):

```c
static int vruntime_eligible(struct cfs_rq *cfs_rq, u64 vruntime)
{
	struct sched_entity *curr = cfs_rq->curr;
	s64 avg = cfs_rq->avg_vruntime;
	long load = cfs_rq->avg_load;

	if (curr && curr->on_rq) {
		unsigned long weight = scale_load_down(curr->load.weight);

		avg += entity_key(cfs_rq, curr) * weight;
		load += weight;
	}

	return avg >= (s64)(vruntime - cfs_rq->min_vruntime) * load;
}
```

最后这一行 `avg >= (vruntime - min_vruntime) * load` 是 `V ≥ v_i` 的去分母形式(把 V 的定义代入,两边乘以 load 消去分母)。这个变形不是装饰——**它避免了除法**。在 eligibility 这种热路径(每次 `pick_eevdf` 都要调多次)上,把除法变成乘法是显著的性能优化。这是内核里"用代数变形消除除法"的典型手段(和 `__calc_delta` 用预计算倒数消除除法是同一种思路)。

### 7.3.3 不这样会怎样:没有 eligibility 会撞什么墙

> **不这样会怎样**:如果没有 eligibility 判据,只看 deadline 最早(纯粹的 EDF,Earliest Deadline First),那一个 vruntime 已经远超 V(跑过头)的任务,只要它的 deadline 还早,就仍会被反复选中——它会继续霸占 CPU,而 vruntime 落后的任务饿死。EDF 单独用是会饿死低权重任务的(EDF 只保证"截止期",不保证"公平份额")。EEVDF 把 EDF 限定在 eligible 子集里,既保截止期(延迟)又保份额(公平),这是 "Eligible" 在名字里的意义。

反过来,如果只看 eligibility(lag ≥ 0)不看 deadline——那退化成"在 vruntime ≤ V 的任务里随便选",延迟没保障。两个判据必须同时用:**先过滤 eligible,再在 eligible 里选最早 deadline**。这就是 `pick_eevdf` 干的事。

---

## 7.4 几何关系:lag/vruntime/avg_vruntime/deadline 的时间轴

这一节用 ASCII 时间轴把三个量的几何关系钉死。这是理解 EEVDF 的关键一图。

```
 EEVDF 三件套的几何关系(虚拟时间轴 v):

   vruntime 轴 ──────────────────────────────────► (v 越大 = 跑得越多)
   
        v_A              v_C        V(avg)       v_B                    v_D
        │← lag_A (>0) →│           │            │                      │
   ─────┼──────────────┼───────────┼────────────┼──────────────────────┼─────
        │              │           │            │                      │
        │              │   lag_C>0 │            lag_B < 0(跑过头)      │
        │              │           │            │                      │
        │              │           │            │                      │
        │◄─deadline_A─►│           │            │                      │
        │  = v_A + r/w │           │            │                      │
        │              │           │            │                      │
        │  A eligible  │  C eligible           ✗ B ineligible          │
        │  (v_A ≤ V)   │  (v_C ≤ V)            (v_B > V)               │
        │              │                                              │
        │              │                                              │
        └─ pick: 在 eligible(A,C)里选 deadline 最早的 ── A 胜出(dealine_A < deadline_C)
```

读图要点:

1. **V(avg_vruntime)是分水岭**:vruntime 在 V 左边的任务 lag > 0(eligible),右边的 lag < 0(ineligible)。
2. **deadline 是任务自己 vruntime 往右的一段距离**(`deadline = vruntime + calc_delta_fair(slice)`),权重大这段短(deadline 更紧迫)。
3. **pick_eevdf 只在 eligible 任务里挑 deadline 最早的**。上图里 B 虽然 deadline 可能最早(如果它权重特别大),但 vruntime > V 不 eligible,被排除;A 和 C 都 eligible,选 deadline 更早的 A。

再看一个动态演化:任务 A 跑了一段后,它的 vruntime 往右移,可能越过 V 变 ineligible;同时 V 也在右移(别的任务贡献加权和)。这种动态竞争就是 EEVDF 的稳态。

```
 时间演化:任务 A 在跑,它的 vruntime 往右移,V 也往右移(但比 A 慢,因为 V 是加权平均):

   t0:    v_A ─── V ─── v_B            (A eligible,A 在跑)
   t1:    ─── v_A ─── V ─── v_B        (A 的 vruntime 追上 V,A 仍 eligible)
   t2:    ─── V ─── v_A ─── v_B        (A 的 vruntime 越过 V,A 变 ineligible!)
                                       → pick_eevdf 转选 B(假设 B eligible)
```

> **钉死这件事**:EEVDF 的稳态是"所有任务的 vruntime 围绕 V 上下波动"。权重大的任务 vruntime 走得慢(calc_delta_fair 折算后增量小),它长期在 V 左边(eligible),且 deadline 更近(更频繁被选);权重小的任务 vruntime 走得快,它经常越过 V 变 ineligible,被排除。结果是:权重大的任务拿到与其权重成比例的 CPU(因为它被选的次数多),权重小的任务拿得少。这正是 EEVDF 实现"按权重成比例分配"的机制——通过 lag/deadline 联动,而不是 CFS 的"vruntime 最小"。

---

## 7.5 `pick_eevdf`:O(log N) 的 augmented tree 搜索

eligibility 和 deadline 都清楚了,现在看 EEVDF 怎么在一棵红黑树里高效完成"eligible 里最早 deadline"的搜索。这是 EEVDF 工程实现的核心。源码 [`pick_eevdf`](../linux/kernel/sched/fair.c#L884)([fair.c:884](../linux/kernel/sched/fair.c#L884)):

```c
/*
 * Earliest Eligible Virtual Deadline First.
 *
 * We can do this in O(log n) time due to an augmented RB-tree. The
 * tree keeps the entries sorted on deadline, but also functions as a
 * heap based on the vruntime by keeping:
 *
 *   se->min_vruntime = min(se->vruntime, se->{left,right}->min_vruntime)
 *
 * Which allows tree pruning through eligibility.
 */
static struct sched_entity *pick_eevdf(struct cfs_rq *cfs_rq)
{
	struct rb_node *node = cfs_rq->tasks_timeline.rb_root.rb_node;
	struct sched_entity *se = __pick_first_entity(cfs_rq);
	struct sched_entity *curr = cfs_rq->curr;
	struct sched_entity *best = NULL;

	/* We can safely skip eligibility check if there is only one entity */
	if (cfs_rq->nr_running == 1)
		return curr && curr->on_rq ? curr : se;

	if (curr && (!curr->on_rq || !entity_eligible(cfs_rq, curr)))
		curr = NULL;

	/*
	 * Once selected, run a task until it either becomes non-eligible or
	 * until it gets a new slice. See the HACK in set_next_entity().
	 */
	if (sched_feat(RUN_TO_PARITY) && curr && curr->vlag == curr->deadline)
		return curr;

	/* Pick the leftmost entity if it's eligible */
	if (se && entity_eligible(cfs_rq, se)) {
		best = se;
		goto found;
	}

	/* Heap search for the EEVD entity */
	while (node) {
		struct rb_node *left = node->rb_left;

		/*
		 * Eligible entities in left subtree are always better
		 * choices, since they have earlier deadlines.
		 */
		if (left && vruntime_eligible(cfs_rq,
					__node_2_se(left)->min_vruntime)) {
			node = left;
			continue;
		}

		se = __node_2_se(node);

		/*
		 * The left subtree either is empty or has no eligible
		 * entity, so check the current node since it is the one
		 * with earliest deadline that might be eligible.
		 */
		if (entity_eligible(cfs_rq, se)) {
			best = se;
			break;
		}

		node = node->rb_right;
	}
found:
	if (!best || (curr && entity_before(curr, best)))
		best = curr;

	return best;
}
```

### 7.5.1 树的排序键:deadline(不是 vruntime!)

第一件要注意的事:红黑树**按 deadline 排序**,不是按 vruntime。排序函数 [`__entity_less`](../linux/kernel/sched/fair.c#L793) → [`entity_before`](../linux/kernel/sched/fair.c#L551):

```c
static inline bool entity_before(const struct sched_entity *a,
				 const struct sched_entity *b)
{
	return (s64)(a->deadline - b->deadline) < 0;
}
```

这是 EEVDF 和 CFS 在数据结构层面的分水岭:**CFS 的 rbtree 按 vruntime 排序(取最左 = vruntime 最小);EEVDF 的 rbtree 按 deadline 排序(取最左 = deadline 最早)**。所有讲 "CFS 红黑树选最小 vruntime" 的老资料在 6.6 之后都过时了。

因为按 deadline 排序,**最左节点(`__pick_first_entity`)就是 deadline 最早的任务**。但它不一定 eligible——如果最左任务的 vruntime > V(它跑过头了),它的 deadline 虽早却没资格。`pick_eevdf` 第一个优化:如果最左 eligible,直接返回它(它 deadline 最早且 eligible,完美);否则进入 heap 搜索。

### 7.5.2 augmented tree:每个节点缓存子树 min_vruntime

按 deadline 排序后,eligibility 检查的难点是:一棵子树里**有没有 eligible 任务**?如果整棵子树都不 eligible,可以整棵剪掉。但 eligibility 依赖 vruntime,而树按 deadline 排——vruntime 在树里是乱序的。怎么办?

**augmented rbtree**:每个节点维护一个额外字段 `se->min_vruntime`,表示"以本节点为根的子树里,所有任务的 vruntime 最小值"(定义在 [`fair.c:810`](../linux/kernel/sched/fair.c#L810) 的 `min_vruntime_update`):

```c
/* se->min_vruntime = min(se->vruntime, {left,right}->min_vruntime) */
static inline bool min_vruntime_update(struct sched_entity *se, bool exit)
{ ... }
```

入队时用 `rb_add_augmented_cached` + `min_vruntime_cb` 自动维护([`__enqueue_entity`](../linux/kernel/sched/fair.c#L830),[fair.c:830](../linux/kernel/sched/fair.c#L830))。于是判断"左子树有没有 eligible 任务"变成:检查左子树根的 `min_vruntime` 是否 ≤ V——如果左子树最小的 vruntime 都 > V,整棵左子树都不 eligible,跳过。

这就是 `pick_eevdf` 搜索循环里那一段的关键:

```c
/* Heap search for the EEVD entity */
while (node) {
    struct rb_node *left = node->rb_left;

    /* Eligible entities in left subtree are always better choices,
       since they have earlier deadlines. */
    if (left && vruntime_eligible(cfs_rq,
                __node_2_se(left)->min_vruntime)) {
        node = left;
        continue;
    }

    se = __node_2_se(node);

    /* The left subtree either is empty or has no eligible entity,
       so check the current node ... */
    if (entity_eligible(cfs_rq, se)) {
        best = se;
        break;
    }

    node = node->rb_right;
}
```

逻辑:从根开始,因为树按 deadline 排序,**左子树的 deadline 都比当前节点早**——所以只要左子树里有 eligible 任务,它一定比当前节点和右子树里的任何 eligible 任务 deadline 都早,优先往左走。只有当左子树整棵不 eligible(用 `min_vruntime` 剪枝判断)时,才检查当前节点;当前节点也不 eligible 时,才走右子树。

第一次在当前节点找到 eligible 任务时,`break`——因为树按 deadline 排序,这个 eligible 节点的 deadline 是所有"它和它的右子树"里最早的(左子树不 eligible 已排除),所以它就是答案。

### 7.5.3 时间复杂度:O(log N)

每次循环要么往左、要么往右,树高 O(log N),所以 `pick_eevdf` 是 O(log N)。比 CFS 的 O(1) 取最左慢一点,但换来的是"在 eligible 子集里选最早 deadline"这个 CFS 做不到的能力。这是 EEVDF 用一点常数时间换表达能力的选择。

> **反面对比**:如果不用 augmented tree,朴素办法是"遍历所有 eligible 任务找 deadline 最早的"——O(N),任务数多时(`cfs_rq` 上几百个任务)性能崩。augmented tree 的精妙在于:**用 O(log N) 的额外维护成本(入队/出队时更新 min_vruntime),把"有没有 eligible 任务"这个本该 O(N) 的查询降到 O(1)**(只看子树根的 min_vruntime)。这是 rbtree 的 augmented 变体在内核里的典范用法(另一个例子是 memcg 的 `page_counter`,但那是不同的 augmented)。

### 7.5.4 RUN_TO_PARITY:让被选中的任务跑到 slice 用完

`pick_eevdf` 开头那段:

```c
if (sched_feat(RUN_TO_PARITY) && curr && curr->vlag == curr->deadline)
    return curr;
```

这是个优化:`curr`(当前在跑的任务)如果 `vlag == deadline`(含义见源码注释和 [`fair.c:905`](../linux/kernel/sched/fair.c#L905) 的注释,即它刚被选中、刚好跑到 lag 归零),让它继续跑到 slice 用完,而不是每 tick 都重选。这减少不必要的切换,由 feature flag `RUN_TO_PARITY` 控制([`features.h:9`](../linux/kernel/sched/features.h#L9),默认 true)。`set_next_entity` 里有个对应的 HACK 配合(下一章 P2-10 讲 `set_next_entity` 时会提到)。

---

## 7.6 技巧精解:augmented rbtree 剪枝 + 无除法 eligibility

这一章挑两个最硬核的技巧拆透:`pick_eevdf` 的 augmented tree 剪枝(怎么 O(log N) 完成"eligible 里最早 deadline"搜索)和 `vruntime_eligible` 的无除法变形(怎么把热路径的除法消掉)。

### 技巧一:augmented rbtree——用 O(log N) 维护换 O(1) 剪枝查询

EEVDF 的核心查询是:在按 deadline 排序的 rbtree 里,找最早的 eligible 任务。难点在于 eligibility 依赖 vruntime,而 vruntime 在 deadline 排序的树里是"乱序"的——不能直接取最左了事。

朴素方案(不 augmented):遍历整棵树,逐个检查 eligibility,O(N)。

augmented 方案:每个节点维护 `min_vruntime = min(自己 vruntime, 左子树 min_vruntime, 右子树 min_vruntime)`。判断"左子树有没有 eligible 任务"变成"左子树的 min_vruntime 是否 ≤ V"——O(1)。于是搜索可以剪枝:左子树整棵不 eligible 就跳过,只走右子树;左子树有 eligible 就往左走(因为左子树 deadline 更早,优先)。总复杂度 O(log N)。

维护成本:每次入队/出队,从插入/删除点到根的路径上所有节点的 min_vruntime 要重算,路径长 O(log N),所以增删是 O(log N)(rbtree 本来就 O(log N) 增删,只是常数翻倍)。

```c
/* 入队时:rb_add_augmented_cached 自动维护 min_vruntime */
static void __enqueue_entity(struct cfs_rq *cfs_rq, struct sched_entity *se)
{
	avg_vruntime_add(cfs_rq, se);
	se->min_vruntime = se->vruntime;
	rb_add_augmented_cached(&se->run_node, &cfs_rq->tasks_timeline,
				__entity_less, &min_vruntime_cb);
}
```

`rb_add_augmented_cached` 是 rbtree 的通用 augmented 接口(`include/linux/rbtree_augmented.h`),插入时调用 `min_vruntime_cb` 回调沿路径更新每个节点的 `min_vruntime`。

> **反面对比**:朴素遍历 O(N),100 个任务每次 pick 都遍历 100 个节点,scheduler 的核心路径每秒跑上千次,开销不可接受。augmented 方案把"剪枝查询"从 O(N) 降到 O(1),代价是增删从 O(log N) 变成 O(log N)(常数翻倍,因为要更新 augmented 字段)——净收益巨大。这是"用维护成本换查询成本"的典范,和 LevelDB 的 rank-augmented skiplist(用 O(log N) 维护换 O(log N) 随机访问)是同一类思路。

### 技巧二:无除法的 eligibility——代数变形消除热路径除法

eligibility 的原始定义是 `V ≥ v_i`,其中 V = `avg_vruntime = min_vruntime + 加权和 / 总权重`。直接实现要先算 V(一次除法),再和 v_i 比——每次 `pick_eevdf` 的循环里都要调,除法是热路径的大敌(几十到上百周期)。

源码 [`vruntime_eligible`](../linux/kernel/sched/fair.c#L746) 的实现是:

```c
return avg >= (s64)(vruntime - cfs_rq->min_vruntime) * load;
```

把 V 的定义代入 `V ≥ v_i`:

```
min_vruntime + avg/load >= vruntime
等价于(两边减 min_vruntime):
avg/load >= vruntime - min_vruntime
等价于(两边乘 load,load > 0):
avg >= (vruntime - min_vruntime) * load
```

一次乘法替代一次除法,代数变形后 `vruntime_eligible` 完全无除法。这是内核里"在热路径上用代数变形消除除法"的标准操作。另一个同类技巧是 `__calc_delta` 用预计算的 `inv_weight`(2^32/weight)把除法变成乘法(第 8 章详讲)。

> **反面对比**:如果保留除法,每次 eligibility 检查(在 `pick_eevdf` 的循环里可能调多次)都做一次 `div_s64`,在 64 位上 div_s64 是几十周期,累加起来显著拖慢调度路径。代数变形把它降到一次乘法(几周期)。这种优化在内核调度器这种"每秒跑千万次的热路径"上是必须的。

> **钉死这件事**:EEVDF 两个最硬的技巧都是"用一点点预处理(augmented 维护 / 代数变形)换热路径的大头开销(查询 / 除法)"。这是内核 C 性能工程的精髓——热路径上的每一条指令都要抠。这两个技巧加上 `avg_vruntime` 的增量维护(入队/出队时 O(1) 更新加权和),共同支撑了 EEVDF 在 O(log N) 内完成"eligible 里最早 deadline"的全套决策。

---

## 7.7 入队与唤醒:`place_entity` 的 lag 补偿

eligibility/deadline/`pick_eevdf` 是 EEVDF 的核心,但还有一个关键环节——**任务入队时,它的 vruntime 和 deadline 怎么定?** 这决定了一个睡醒的任务回来后,从虚拟时间轴的哪里开始竞争。EEVDF 在这里用 `vlag` 做了一个 CFS 做不到的事:**跨睡眠保留 lag**。源码 [`place_entity`](../linux/kernel/sched/fair.c#L5170)([fair.c:5170](../linux/kernel/sched/fair.c#L5170)):

```c
static void
place_entity(struct cfs_rq *cfs_rq, struct sched_entity *se, int flags)
{
	u64 vslice, vruntime = avg_vruntime(cfs_rq);
	s64 lag = 0;

	se->slice = sysctl_sched_base_slice;
	vslice = calc_delta_fair(se->slice, se);

	/* EEVDF: placement strategy #1 / #2 */
	if (sched_feat(PLACE_LAG) && cfs_rq->nr_running) {
		...
		lag = se->vlag;

		/*
		 * If we want to place a task and preserve lag, we have to
		 * consider the effect of the new entity on the weighted
		 * average and compensate for this, otherwise lag can quickly
		 * evaporate.
		 *
		 * ... (推导见源码,最终:)
		 *   vl_i = (W + w_i)*vl'_i / W
		 */
		load = cfs_rq->avg_load;
		if (curr && curr->on_rq)
			load += scale_load_down(curr->load.weight);

		lag *= load + scale_load_down(se->load.weight);
		if (WARN_ON_ONCE(!load))
			load = 1;
		lag = div_s64(lag, load);
	}

	se->vruntime = vruntime - lag;

	if (sched_feat(PLACE_DEADLINE_INITIAL) && (flags & ENQUEUE_INITIAL))
		vslice /= 2;

	/* EEVDF: vd_i = ve_i + r_i / w_i */
	se->deadline = se->vruntime + vslice;
}
```

核心是 `se->vruntime = vruntime - lag` 这一行。`vruntime` 是当前全队列的 V,`lag` 是任务上次出队时存的 `se->vlag`(经过上面的放大补偿)。所以:

- 一个被欠的任务(`vlag > 0`),它的 vruntime 被放到 `V - lag`,即比 V 更靠左(更小)——它一入队就 eligible,且 vruntime 较小,会被优先调度。这是"补偿它被欠的份额"。
- 一个跑过头的任务(`vlag < 0`),它的 vruntime 被放到 `V - lag = V + |lag|`,即比 V 更靠右(更大)——它一入队就 ineligible(vruntime > V),要等到时间推移、V 增长追上它才转 eligible。这是"惩罚它跑过头的部分"。

那个放大补偿 `lag *= (load + w_i) / load` 的数学推导写在源码注释里([`fair.c:5192`](../linux/kernel/sched/fair.c#L5192)–L5243):它的目的是抵消"新任务入队会让 V 移动"的副作用。如果不补偿,任务入队后 V 会被它自己的 vruntime 拉动,lag 会"蒸发"。这个推导很精彩但偏数学,我们只记结论:**lag 补偿让任务的虚拟欠账在睡眠唤醒周期里得到保留,这是 EEVDF 跨睡眠保公平的关键**。

> **钉死这件事**:CFS 唤醒时把 vruntime 拉到 `min_vruntime - sched_latency_ns` 附近(经验值补偿),依赖一堆 feature flag 微调;EEVDF 唤醒时用 `vlag`(出队时存的快照)精确补偿,数学上自洽——lag 跨睡眠保留,公平不因睡眠唤醒而漂移。这是 EEVDF 解决 CFS 第二个老毛病(唤醒补偿)的算法层面方案。

---

## 7.8 ★ 对照第 7 本:EEVDF 的"延迟保障"在 Go 里没有对应物

EEVDF 最核心的创新是 virtual deadline——每个任务每次入队都有一个明确的虚拟截止时间,调度器优先调度快到期的。这个机制**在 Go runtime 的 GMP 里完全没有对应物**:

- Go 的 goroutine **没有 deadline 概念**。每个 P 的本地队列是 FIFO(加一个 `runnext` 槽强制下次跑),goroutine 入队后按先进先出调度,没有"哪个 goroutine 该在多久内被调度"这种判据。
- Go 的延迟响应靠 **runnext 槽**(刚唤醒的 goroutine 放进 runnext,下次一定跑它)和 **work-stealing**(空闲 P 偷别的 P 的 goroutine,均衡负载)间接保证。这是一种**结构性延迟控制**(通过队列位置和均衡),不是 EEVDF 那种**算法化延迟保障**(通过 deadline 数学)。
- 如果一个 goroutine 长期不被调度(比如它的 P 上挤了太多别的 goroutine),Go 没有"它快到期了优先跑它"的机制——只能等 FIFO 轮到它,或者别的 P work-stealing 偷走它。

这反映了两种调度哲学的根本差异:**Linux 调度器要为延迟敏感任务(音频、交互、实时-ish 的网络服务)提供算法层面的延迟保障,所以需要 deadline;Go 的 goroutine 是协作式并发原语,延迟敏感场景靠业务层(用 channel 同步、用 GOMAXPROCS 隔离)解决,调度器本身不做 deadline 保障。** 第 21 章的对照总表会钉死这组差异。

---

## 章末小结

这一章是全书最硬核的一章,我们钻进了 EEVDF 的算法内核,立清了三样东西:

1. **三个核心量**:`vruntime`(按权重折算的虚拟时间,沿用 CFS)、`vlag`(虚拟欠账 V - v_i,EEVDF 新增)、`deadline`(本次请求的虚拟截止 v_i + r_i/w_i,EEVDF 新增);加上全队列共享的 `avg_vruntime`(V,加权平均)。
2. **eligibility 判据**:`lag ≥ 0` ⇔ `v_i ≤ V`,实时由 [`vruntime_eligible`](../linux/kernel/sched/fair.c#L733) 用无除法变形判定。lag 表达"谁被欠",eligible 决定"谁有资格跑"。
3. **`pick_eevdf` 的 O(log N) 搜索**:augmented rbtree 按 deadline 排序(`entity_before` 比 deadline),每节点缓存子树 min_vruntime 用于 eligibility 剪枝。先取最左(deadline 最早),不 eligible 就往右走,左子树用 min_vruntime 整棵剪枝。

本章还讲了 **`place_entity` 的 lag 补偿**:任务唤醒时 `se->vruntime = V - lag`,跨睡眠保留公平;以及两个核心工程技巧——augmented tree 剪枝(O(log N) 维护换 O(1) 剪枝查询)和无除法 eligibility(代数变形消热路径除法)。

本章服务二分法的**策略**面:讲清"给定 `cfs_rq`,EEVDF 怎么决定下一个跑谁"——这是 6.6 之后 Linux 公平调度的算法核心,也是全书最重要的策略章节。

### 五个"为什么"清单

1. **EEVDF 的三个量各表什么?** `vruntime` 表"已分配的虚拟份额"(沿用 CFS);`vlag` 表"虚拟欠账"(被欠还是跑过头);`deadline` 表"本次请求的虚拟截止"(延迟紧急度)。三维各司其职,解耦 CFS 一维 vruntime 的冲突。
2. **为什么 eligibility 是 lag ≥ 0?** lag ≥ 0 ⇔ V ≥ v_i,即"我的 vruntime 不超过全队平均"。被欠的任务 vruntime 落后于平均,所以 eligible;跑过头的 vruntime 领先,所以 ineligible。这个判据保证"被欠的先跑"。
3. **deadline 怎么算?权重大为什么 deadline 更近?** `deadline = vruntime + calc_delta_fair(slice)`,其中 `calc_delta_fair(slice) = slice * NICE_0_LOAD / weight`。权重大 → 这项小 → deadline 离当前 vruntime 更近 → 更紧迫 → 更频繁被选中。权重通过 deadline 紧迫度起作用,不是时间片长短。
4. **`pick_eevdf` 怎么 O(log N) 找答案?** rbtree 按 deadline 排序(最左 deadline 最早),每节点缓存子树 min_vruntime。取最左,eligible 就返回;不 eligible 就走右子树,左子树用 min_vruntime 整棵剪枝判断。树高 O(log N)。
5. **`place_entity` 为什么用 lag 补偿?** 任务出队时把 lag 存进 `se->vlag`(限幅快照),入队时 `se->vruntime = V - lag` 还原。被欠的 lag > 0 → vruntime 比 V 小 → 入队即 eligible;跑过头的 lag < 0 → vruntime 比 V 大 → 入队即 ineligible。lag 跨睡眠保留,公平不漂移。这取代了 CFS 那套经验值的 vruntime 补偿。

### 想继续深入往哪钻

- 源码:`kernel/sched/fair.c` 的 [`avg_vruntime`](../linux/kernel/sched/fair.c#L660)(L660)、[`entity_lag`](../linux/kernel/sched/fair.c#L699)(L699)、[`vruntime_eligible`](../linux/kernel/sched/fair.c#L733)(L733)、[`entity_eligible`](../linux/kernel/sched/fair.c#L749)(L749)、[`pick_eevdf`](../linux/kernel/sched/fair.c#L884)(L884)、[`update_deadline`](../linux/kernel/sched/fair.c#L984)(L984)、[`place_entity`](../linux/kernel/sched/fair.c#L5170)(L5170)、[`update_curr`](../linux/kernel/sched/fair.c#L1162)(L1162);`include/linux/sched.h` 的 [`struct sched_entity`](../linux/include/linux/sched.h#L536)(L536,看 `vruntime`/`vlag`/`deadline`/`slice`/`min_vruntime` 五字段)。
- EEVDF 原论文:Stiliadis & Varma, *Earliest Eligible Virtual Deadline First: A Flexible and Accurate Mechanism for Proportional Share Resource Allocation*, IEEE/ACM Trans. Networking, 1998。(理论 28 年,2023 年才进 Linux 主线)
- 6.6 EEVDF 合入的 mailing list 讨论(LKML 搜 "EEVDF",Peter Zijlstra 的 patch series)。
- 想观测:跑一个多任务负载,`perf sched` 看每个任务的调度延迟分布,对比 6.5(CFS)和 6.6+(EEVDF)的延迟尾部分布——EEVDF 的尾延迟应该更稳定(deadline 内置保障)。

### 引出下一章

EEVDF 的三件套讲完了。但还有个关键的窟窿:**权重到底怎么定?** nice -20 到 nice 19 怎么映射成 `sched_prio_to_weight[]`?为什么是这张表而不是线性映射?下一章,我们钻进 `nice` 与权重——这是 EEVDF(也是 CFS)公平的"权重入口",权重表的每一行都是精心设计的非线性(每级差约 10% CPU),它决定了 `calc_delta_fair` 的折算比例、`vlag` 的大小、`deadline` 的紧迫度。
