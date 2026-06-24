# 第十章 · EEVDF 的 pick_next_fair 与时间片

> 篇:第 2 篇 · EEVDF 公平调度:下一个跑谁
> 主线呼应:第 2 篇的前四章讲了 EEVDF 的算法核心(lag/deadline/eligible,第 7 章)、权重入口(nice,第 8 章)、负载跟踪(PELT,第 9 章)。这一章是第 2 篇的收束——我们把 EEVDF 算法**组装进调度器的选任务主路径**:`pick_next_task_fair` 怎么调 `pick_next_entity` → `pick_eevdf` 选出下一个 entity,怎么调 `set_next_entity` 把它"上架"为当前任务,**动态时间片怎么算**(`se->slice` + `hrtick_start_fair` 精确抢占),以及 EEVDF 的 slice 和 CFS 老的动态 `sched_slice` 的关键区别。读完这一章,你能完整讲清"`__schedule` 进 fair 调度类之后,下一个普通任务怎么被选出来并切上去"。

## 核心问题

**`pick_next_task_fair`(fair 调度类的 `pick_next_task` 实现)的流程:从根 `cfs_rq` 开始,循环调 `pick_next_entity`(默认走 `pick_eevdf`,在 eligible 任务里选最早 deadline 的),处理 cgroup 层级(FAIR_GROUP_SCHED 下要逐层下钻到叶子任务),最后调 `set_next_entity` 把选中的 entity 设为 `cfs_rq->curr` 并 `__dequeue_entity` 摘出树外。**时间片在 EEVDF 下被极大简化:每个任务的 `se->slice = sysctl_sched_base_slice`(常量 0.75ms,不再是 CFS 那套 `sched_latency_ns / nr_running * weight` 的动态计算);任务跑过 slice 由 `update_deadline` 标记重调度,`hrtick_start_fair` 用高精度定时器到点精确抢占(误差亚微秒)。EEVDF 把 CFS 时代一堆 `sched_min_granularity`/`sched_latency`/`sched_wakeup_granularity` 旋钮砍掉,只剩一个 `sched_base_slice`。**

读完本章你会明白:

1. `pick_next_task_fair` 的完整流程:从根 `cfs_rq` 选 entity → 处理 cgroup 层级 → `set_next_entity`,以及 `prev`(上一个任务)怎么 `put_prev_entity` 放回树。
2. `set_next_entity` 的关键操作:`__dequeue_entity` 把当前任务摘出树(所以 `pick_eevdf`/`avg_vruntime` 要特殊处理 `cfs_rq->curr`)、HACK `se->vlag = se->deadline`(为 `RUN_TO_PARITY` 存截止快照)、`prev_sum_exec_runtime` 记录 slice 起点。
3. EEVDF 时间片的极大简化:`se->slice = sysctl_sched_base_slice`(0.75ms 常量),取代 CFS 的 `sched_latency_ns`/`sched_min_granularity`/动态 `sched_slice`。
4. `hrtick_start_fair`:高精度定时器到点抢占,用 `sum_exec_runtime - prev_sum_exec_runtime` 算"已跑多少",和 slice 比较决定何时抢。
5. ★ 对照第 7 本:`pick_next_task_fair` 这种"调度类按优先级遍历"的选任务结构,在 Go 里是"runnext 槽 + 本地队列 FIFO + work-stealing"——结构完全不同,反映了"显式策略算法 vs 结构性公平"的差异。

---

## 10.1 一句话点破

> **`pick_next_task_fair` 把 EEVDF 装进调度器:它从根 `cfs_rq` 调 `pick_next_entity` → `pick_eevdf`(eligible 里最早 deadline),逐层下钻到叶子任务,然后 `set_next_entity` 把选中的任务摘出树、设为 curr。时间片在 EEVDF 下出奇地简单——`se->slice = sysctl_sched_base_slice`(0.75ms 常量),CFS 那套 `sched_latency_ns`/`sched_min_granularity` 全砍了。`hrtick_start_fair` 用高精度定时器到点精确抢占,误差亚微秒。**

这是结论,不是理由。本章倒过来拆:先走读 `pick_next_task_fair` 的完整流程,再讲 `set_next_entity` 的关键操作,然后看 EEVDF 时间片的简化,最后看 `hrtick_start_fair` 怎么精确抢占。

---

## 10.2 `pick_next_task_fair` 的完整流程

[`pick_next_task_fair`](../linux/kernel/sched/fair.c#L8398)([fair.c:8398](../linux/kernel/sched/fair.c#L8398))是 `fair_sched_class` 的 `pick_next_task` 方法(注册在 [`fair.c:13115`](../linux/kernel/sched/fair.c#L13115) 的 `fair_sched_class` 结构体)。当 `__schedule`(第 12 章)遍历调度类链到 fair 类时,调它选下一个普通任务。

### 10.2.1 主干(无 FAIR_GROUP_SCHED 的简化情形)

先看没有 cgroup 层级的简化情形(`CONFIG_FAIR_GROUP_SCHED` 未开,或 prev 不是 fair 类),走 `simple` 标签([fair.c:8485](../linux/kernel/sched/fair.c#L8485)):

```c
simple:
#endif
	if (prev)
		put_prev_task(rq, prev);          /* 把上一个任务放回树 */

	do {
		se = pick_next_entity(cfs_rq);    /* 选下一个 entity(pick_eevdf) */
		set_next_entity(cfs_rq, se);       /* 设为 curr,摘出树 */
		cfs_rq = group_cfs_rq(se);         /* 如果 se 是组,下钻 */
	} while (cfs_rq);

	p = task_of(se);                       /* 最终拿到 task_struct */
```

三步:
1. `put_prev_task`:把上一个在跑的任务(prev)放回 `cfs_rq`(它的 vruntime 已经更新,重新入队参与下一轮竞争)。
2. 循环 `pick_next_entity` + `set_next_entity`:从根 `cfs_rq` 选一个 entity,设为 curr;如果选中的是组 entity(它的 `my_q` 非空),下钻到组内的 `cfs_rq` 再选,直到选到叶子任务(`task_of(se)`)。
3. 返回 `task_struct *p`,交给 `__schedule` 的 `context_switch`(第 13 章)切换。

### 10.2.2 cgroup 层级:逐层下钻(FAIR_GROUP_SCHED)

开 `CONFIG_FAIR_GROUP_SCHED`(几乎所有发行版都开)时,`pick_next_task_fair` 走优化路径(见 [`fair.c:8421`](../linux/kernel/sched/fair.c#L8421) 的 `do { ... } while (cfs_rq);`):从根 `rq->cfs` 开始,每层调 `pick_next_entity` 选一个组 entity,下钻到该组的子 `cfs_rq`,直到选到叶子任务。这是组调度的核心(第 19 章详讲):一个 cgroup 作为一个 `sched_entity` 参与上层竞争,EEVDF 在每层独立运行;选任务时从顶层开始,每层选一个 winner,逐层下钻到具体任务。

```
 pick_next_task_fair 的 cgroup 层级下钻(FAIR_GROUP_SCHED):

   根 cfs_rq (rq->cfs)
       │ pick_next_entity → pick_eevdf
       ▼
   选出:组 entity G1 (cgroup "container A")
       │ 下钻:group_cfs_rq(G1) → G1 的子 cfs_rq
       ▼
   G1 的 cfs_rq
       │ pick_next_entity → pick_eevdf
       ▼
   选出:组 entity G2 (cgroup "user X" 在 G1 下)
       │ 下钻:group_cfs_rq(G2)
       ▼
   G2 的 cfs_rq
       │ pick_next_entity → pick_eevdf
       ▼
   选出:叶子任务 T (task_struct)
       │
       ▼
   返回 task_of(T) → __schedule → context_switch

   每层都是独立的 EEVDF 决策;组调度让父组限制子组的 CPU 占比
```

每一层的 `pick_next_entity` → `pick_eevdf` 都是上一章讲的那个算法——eligible 里选最早 deadline。组层级不影响 EEVDF 算法本身,只是把它分层应用。

### 10.2.3 `pick_next_entity`:薄封装 + NEXT_BUDDY 优化

[`pick_next_entity`](../linux/kernel/sched/fair.c#L5466)([fair.c:5466](../linux/kernel/sched/fair.c#L5466))是 `pick_eevdf` 的薄封装,加一个 NEXT_BUDDY 优化:

```c
static struct sched_entity *
pick_next_entity(struct cfs_rq *cfs_rq)
{
	/*
	 * Enabling NEXT_BUDDY will affect latency but not fairness.
	 */
	if (sched_feat(NEXT_BUDDY) &&
	    cfs_rq->next && entity_eligible(cfs_rq, cfs_rq->next))
		return cfs_rq->next;

	return pick_eevdf(cfs_rq);
}
```

`NEXT_BUDDY`(默认 false,见 [`features.h:16`](../linux/kernel/sched/features.h#L16))是个优化:如果开启了,且 `cfs_rq->next`(上次唤醒时设的"下次优先"指针)是 eligible 的,直接返回它——跳过 `pick_eevdf` 的搜索。注释明说:"Enabling NEXT_BUDDY will affect latency but not fairness"——它只改善延迟(刚唤醒的任务立刻跑,cache 热的数据还能用),不影响公平(因为 still 要求 eligible)。默认关是因为 `pick_eevdf` 本身已经够快,且 NEXT_BUDDY 偶尔会让某些任务被频繁选中(虽然 eligible 但 deadline 不一定最早)。

绝大多数情况 `pick_next_entity` 直接走 `pick_eevdf`(上一章详讲)。

### 10.2.4 `put_prev_task_fair`:把 prev 放回树

选下一个之前,要把上一个在跑的任务(prev)放回 `cfs_rq`。走 [`put_prev_task_fair`](../linux/kernel/sched/fair.c#L8550) → [`put_prev_entity`](../linux/kernel/sched/fair.c#L5480):

```c
static void put_prev_entity(struct cfs_rq *cfs_rq, struct sched_entity *prev)
{
	/* If still on the runqueue then deactivate_task() was not called
	   and update_curr() has to be done: */
	if (prev->on_rq) {
		update_curr(cfs_rq);                /* 记账 prev 已跑的时间 */
		...
		/* Put 'current' back into the tree. */
		__enqueue_entity(cfs_rq, prev);     /* 重新入队 */
		...
	}
	cfs_rq->curr = NULL;
	...
}
```

prev 在跑时被 `set_next_entity` 摘出树(见 10.3),现在要重新入队参与下一轮 EEVDF 竞争。`update_curr` 把 prev 这段时间跑的实际时间记到它的 vruntime——这是公平记账的关键(不能让 prev 白跑这段时间)。

> **钉死这件事**:`pick_next_task_fair` 的核心是"选 + 设 + 下钻"。`pick_next_entity` 用 EEVDF 选(上一章),`set_next_entity` 把选中的摘出树并设为 curr(下一节),`put_prev_entity` 把 prev 放回树并记账。组调度下这三步逐层下钻到叶子任务。整个流程的算法核心(EEVDF)在第 7 章已拆透,这一章关注的是它怎么被组装进调度器主路径。

---

## 10.3 `set_next_entity`:摘出树 + 存截止快照

[`set_next_entity`](../linux/kernel/sched/fair.c#L5416)([fair.c:5416](../linux/kernel/sched/fair.c#L5416))做几件关键的事:

```c
static void
set_next_entity(struct cfs_rq *cfs_rq, struct sched_entity *se)
{
	clear_buddies(cfs_rq, se);

	/* 'current' is not kept within the tree. */
	if (se->on_rq) {
		update_stats_wait_end_fair(cfs_rq, se);
		__dequeue_entity(cfs_rq, se);              /* (1) 摘出树 */
		update_load_avg(cfs_rq, se, UPDATE_TG);
		/*
		 * HACK, stash a copy of deadline at the point of pick in vlag,
		 * which isn't used until dequeue.
		 */
		se->vlag = se->deadline;                    /* (2) 存截止快照 */
	}

	update_stats_curr_start(cfs_rq, se);
	cfs_rq->curr = se;                              /* (3) 设为 curr */
	...
	se->prev_sum_exec_runtime = se->sum_exec_runtime;   /* (4) 记 slice 起点 */
}
```

四个关键操作:

### (1) `__dequeue_entity` 摘出树

注释 "'current' is not kept within the tree." 点明:**当前在跑的任务不在 rbtree 里**。这是为什么 `avg_vruntime`/`vruntime_eligible`/`pick_eevdf` 都要特殊处理 `cfs_rq->curr`(上一章看到 `avg_vruntime` 里 `if (curr && curr->on_rq)` 临时加回 curr 的贡献)——curr 不在树里,但算 V 时要把它的 vruntime 计入。

为什么摘出树?因为 EEVDF 的决策都是"在队任务之间"的,curr 在跑(已分得 CPU),不参与"选下一个"的竞争。把它摘出避免 `pick_eevdf` 误选它(它已经在跑了)。

### (2) HACK:`se->vlag = se->deadline`

注释明说这是个 HACK:把"选中时的 deadline"快照存到 `se->vlag` 里(此时 `se->vlag` 字段空闲——它只在 dequeue/place 时用,curr 在跑期间不用)。这个快照供 `pick_eevdf` 开头的 `RUN_TO_PARITY` 检查用:

```c
if (sched_feat(RUN_TO_PARITY) && curr && curr->vlag == curr->deadline)
    return curr;
```

`curr->vlag == curr->deadline` 判断"当前任务刚被选中、deadline 还没动"(刚 set_next_entity 后,vlag 被设成了 deadline 的快照,二者相等)。`RUN_TO_PARITY` 让被选中的任务跑到 slice 用完,而不是每 tick 重选——减少不必要切换。

### (3) `cfs_rq->curr = se`

把选中的 entity 设为当前。后续 `update_curr`/`avg_vruntime`/`pick_eevdf` 都通过 `cfs_rq->curr` 找到它。

### (4) `se->prev_sum_exec_runtime = se->sum_exec_runtime`

记 slice 起点:`sum_exec_runtime` 是任务的总实际运行时间(单调递增),`prev_sum_exec_runtime` 在每次被选中时记下当时的 `sum_exec_runtime`。两者之差 = 本次 slice 内已跑的时间,`hrtick_start_fair` 用这个差判断"slice 用完没"(见 10.5)。

> **钉死这件事**:`set_next_entity` 的四个操作里,最非显然的是 (1) 摘出树 和 (2) 存截止快照。摘出树让 curr 不参与 EEVDF 决策(避免误选自己),但要求所有 EEVDF 路径特殊处理 curr(加回它的 vruntime 贡献)。存截止快照是为了 `RUN_TO_PARITY`——让任务跑满 slice 而不是每 tick 重选,减少抖动。这两个 HACK 都是 EEVDF 工程化的细节,源码注释明说是 HACK,反映了算法实现和工程优化之间的折中。

---

## 10.4 EEVDF 时间片的极大简化

### 10.4.1 CFS 时代:动态时间片旋钮一堆

CFS 时代,时间片是动态算的,有一堆 sysctl 旋钮:

- `sched_latency_ns`(目标延迟,默认 6ms):调度器尽量让所有任务在这个窗口内都跑一遍。
- `sched_min_granularity_ns`(最小时间片,默认 0.75ms):每个任务至少跑这么久才允许被抢,避免频繁切换。
- `sched_wakeup_granularity_ns`(唤醒抢占粒度,默认 1ms):唤醒的任务 vruntime 要领先 curr 这么多才抢占。
- `sched_batch_wakeup_granularity_ns`、`sched_child_runs_first`…… 一堆。

时间片实际值 = `sched_latency_ns / nr_running`(按权重比例分),任务数多时被 `sched_min_granularity` 兜底,实际延迟会突破 `sched_latency_ns`。这套机制给了运维很多调参空间,但也带来了不确定性:任务数变化时,每个人的时间片都在抖动,缓存预热成本不可预期。

### 10.4.2 EEVDF 时代:一个常量 slice

6.6 起,EEVDF 把这套全砍了。时间片就是一个全局常量 [`sysctl_sched_base_slice`](../linux/kernel/sched/fair.c#L76)([fair.c:76](../linux/kernel/sched/fair.c#L76),默认 750000ns = 0.75ms):

```c
unsigned int sysctl_sched_base_slice = 750000ULL;
```

每个任务的 `se->slice` 设成这个值,在 [`update_deadline`](../linux/kernel/sched/fair.c#L994)([fair.c:994](../linux/kernel/sched/fair.c#L994))和 [`place_entity`](../linux/kernel/sched/fair.c#L5175)([fair.c:5175](../linux/kernel/sched/fair.c#L5175))里:

```c
se->slice = sysctl_sched_base_slice;
```

**所有任务(不管 nice、不管任务数)的 slice 都是 0.75ms**。权重的作用不再通过"slice 长短"发挥(那是 CFS 的做法),而是通过"deadline 紧迫度"——权重大的任务 deadline 更近(被更频繁选中),每次跑的 0.75ms slice 一样,但被选中的次数多,总 CPU 占比高(第 8 章详讲)。

```
 EEVDF vs CFS 时间片对比:

 CFS(6.5 及以前):
   slice_i = sched_latency_ns * weight_i / 总权重   (动态,按权重分)
   → 权重大的 slice 长(一次跑久),权重小的 slice 短
   → 任务数变,slice 抖动;sched_min_granularity 兜底
   → 一堆 sysctl 旋钮

 EEVDF(6.6 起,6.9 现状):
   slice = sysctl_sched_base_slice   (常量,0.75ms,所有任务一样)
   → 权重通过 deadline 紧迫度起作用(权重大的更频繁被选)
   → 每次 slice 固定,切换间隔可预期
   → 只剩 sched_base_slice 一个旋钮
```

### 10.4.3 不这样会怎样:为什么 EEVDF 能砍掉动态 slice

> **不这样会怎样**:CFS 用动态 slice 是因为它的判据只有 vruntime——权重大的任务 vruntime 走得慢,如果再给它和权重小的任务一样长的 slice,那它每次被选中后跑同样久,vruntime 增量却小(折算后),它会长期 vruntime 落后被反复选中,这本身就能实现权重比例。但 CFS 多给了权重大的任务更长 slice(动态计算),部分是为了减少切换次数(权重大就一次跑够),部分是历史包袱。EEVDF 引入 deadline 后,权重大 = deadline 近 = 更频繁被选中,这条路径已经足够实现权重比例,不再需要通过 slice 长短加成。所以 EEVDF 把 slice 砍成常量,简化了旋钮,也让切换间隔可预期(每个任务每次都跑 0.75ms,缓存预热成本一致)。

> **钉死这件事**:EEVDF 把 CFS 的一堆时间片旋钮(`sched_latency_ns`/`sched_min_granularity`/`sched_wakeup_granularity`...)砍成一个 `sysctl_sched_base_slice`(0.75ms)。权重不再通过 slice 长短起作用,改通过 deadline 紧迫度。这是 EEVDF 工程简化的一个重要体现——更少的旋钮、更可预期的行为、更少的运维调参负担。老资料里讲的 `sched_latency_ns`/`sched_min_granularity` 在 6.6+ 都过时了。

---

## 10.5 `hrtick_start_fair`:高精度定时器精确抢占

slice 是 0.75ms,但谁来保证任务跑够 0.75ms 就被抢?有两个机制:

### 10.5.1 机制一:粗粒度 tick(`scheduler_tick`)

每个 CPU 有个周期 tick(默认 HZ=250 即每 4ms 一次,或 HZ=1000 即每 1ms 一次)。tick 时调 `scheduler_tick` → `task_tick_fair` → `entity_tick` → `update_curr` → `update_deadline`。`update_deadline` 检查 `curr->vruntime >= curr->deadline`,如果是,调 `resched_curr` 标记重调度(第 11 章)。

但 tick 是粗粒度的(1-4ms),slice 是 0.75ms——一个任务可能跑过 slice 几毫秒才被 tick 检查到,实际拿到的 CPU 比应得的多。这是 CFS 时代的精度问题,EEVDF 同样存在如果只用 tick。

### 10.5.2 机制二:`hrtick` 高精度定时器(EEVDF 精确抢占)

为了精确,内核提供 `hrtick`(high-resolution tick),一个一次性高精度定时器(hrtimer),在任务被选中时设好"slice 用完的时刻",到点精确触发抢占。注册见 [`fair.c:13115`](../linux/kernel/sched/fair.c#L13115) 的 `fair_sched_class`(实际由 `pick_next_task_fair` 末尾调用)。源码 [`hrtick_start_fair`](../linux/kernel/sched/fair.c#L6630)([fair.c:6630](../linux/kernel/sched/fair.c#L6630)):

```c
static void hrtick_start_fair(struct rq *rq, struct task_struct *p)
{
	struct sched_entity *se = &p->se;

	SCHED_WARN_ON(task_rq(p) != rq);

	if (rq->cfs.h_nr_running > 1) {
		u64 ran = se->sum_exec_runtime - se->prev_sum_exec_runtime;   /* 已跑时间 */
		u64 slice = se->slice;                                         /* 0.75ms */
		s64 delta = slice - ran;

		if (delta < 0) {
			/* 已经跑过 slice,立刻重调度 */
			if (task_current(rq, p))
				resched_curr(rq);
			return;
		}
		hrtick_start(rq, delta);    /* 设一个 delta 纳秒后到期的 hrtimer */
	}
}
```

逻辑:`ran = sum_exec_runtime - prev_sum_exec_runtime` 是本次被选中后已跑的实际时间(第 10.3 节的 (4) 记的起点)。`slice = se->slice`(0.75ms)。`delta = slice - ran` 是"还要跑多久"。如果 delta > 0,设一个 delta 纳秒后到期的 hrtimer,到点触发 `hrtick` 中断,在中断里 `resched_curr` 标记重调度。误差亚微秒(hrtimer 是高精度的,基于 CPU TSC 或 HPET)。

`pick_next_task_fair` 末尾调它(见 [`fair.c:8508`](../linux/kernel/sched/fair.c#L8508)):

```c
if (hrtick_enabled_fair(rq))
    hrtick_start_fair(rq, p);
```

注意 `rq->cfs.h_nr_running > 1` 的判断:只有一个任务时不需要 hrtick(没人抢,任务想跑多久跑多久)。

> **钉死这件事**:`hrtick_start_fair` 是 EEVDF 精确时间片保障的关键。粗 tick(1-4ms)粒度太粗,会让任务跑过 slice 几毫秒才被检查;hrtick 用一次性高精度定时器,在任务被选中时设好"slice 到期时刻",到点精确抢占,误差亚微秒。`ran = sum_exec_runtime - prev_sum_exec_runtime` 这个差就是"本次 slice 已跑时间",`set_next_entity` 里记的 `prev_sum_exec_runtime` 在这里发挥作用。第 4 章(P1-04)详讲 hrtick 机制本身,这里只看它在 fair 类的应用。

---

## 10.6 唤醒抢占:`check_preempt_wakeup_fair`

除了 slice 用完的常规抢占,还有一个重要路径:**唤醒抢占**。一个任务刚被唤醒(try_to_wake_up,P1-05),如果它比当前在跑的任务"更该跑",应该立刻抢占 curr,而不是等 curr 跑完 slice。这由 [`check_preempt_wakeup_fair`](../linux/kernel/sched/fair.c#L8286)([fair.c:8286](../linux/kernel/sched/fair.c#L8286))决定:

```c
static void check_preempt_wakeup_fair(struct rq *rq, struct task_struct *p, int wake_flags)
{
	struct task_struct *curr = rq->curr;
	struct sched_entity *se = &curr->se, *pse = &p->se;
	...
	/* 一系列检查,最后比较 deadline */
	...
	/* 如果唤醒任务的 deadline 比 curr 早足够多,标记重调度 */
	if (sched_feat(WAKEUP_PREEMPTION) && ... entity_eligible(...) ...)
		resched_curr(rq);
}
```

唤醒抢占的判据在 EEVDF 里简化为:唤醒的任务如果 eligible 且 deadline 比 curr 早(它更紧急),就抢占。这比 CFS 的 `sched_wakeup_granularity_ns` 旋钮简洁——EEVDF 的 deadline 天然表达"紧急度",唤醒抢占就是 deadline 比较。这是 EEVDF 把"延迟保障"算法内置的另一面:唤醒响应也通过 deadline 判,不再需要单独的唤醒粒度旋钮。

第 11 章详讲 `resched_curr` 怎么设 `TIF_NEED_RESCHED`、抢占怎么延迟到安全点发生——那是机制层的事。

---

## 10.7 技巧精解:`se->slice` 常量化的代价与收益

这一章的招牌技巧是 EEVDF 把时间片从动态(CFS 的 `sched_latency / weight`)简化成常量(`sysctl_sched_base_slice = 0.75ms`)。这个简化有深刻的算法依据和工程权衡。

### 技巧:为什么常量 slice 够用——deadline 接管了"权重表达"

CFS 的动态 slice 有双重职责:(a) 实现"按权重比例"(权重大的 slice 长,一次拿更多实际 CPU),(b) 控制"延迟/切换频率"(任务数多时 slice 缩短,保证每个任务在 sched_latency 窗口内都跑一遍)。这两个职责通过 `slice_i = sched_latency * weight / 总权重` 耦合在一起,任务数变化时两个职责一起抖动。

EEVDF 把这两个职责拆开:
- **"按权重比例" 由 deadline 接管**:权重大的任务 deadline 近(`calc_delta_fair(slice)` 折算后增量小),被更频繁选中——每次跑固定 0.75ms,但被选中的次数多,总 CPU 比例 = 权重比。这条路径不需要 slice 变化。
- **"延迟/切换频率" 由 slice 本身控制**:0.75ms 的固定 slice 意味着任务最多每 0.75ms 切换一次(单核),延迟上限可预期。任务数变化时 slice 不变,切换频率稳定。

因为职责拆开,常量 slice 够用。这是 EEVDF 比 CFS 简洁的算法根源:**把一个耦合的量(slice)拆成两个独立的量(slice 控延迟、deadline 控权重),各自职责清晰,旋钮减少**。

> **反面对比**:如果 EEVDF 保留动态 slice(像 CFS 那样 `slice_i = base * weight / 总权重`),会出现什么?权重大的任务不仅 deadline 近(频繁被选),还每次 slice 长——双倍加成,CPU 占比会偏离权重比(权重大的一方拿到过多)。而且任务数变化时 slice 抖动,切换频率不稳。常量 slice 避免了这两个问题:权重只通过 deadline 一个通道起作用,比例精确;slice 稳定,切换可预期。

> **钉死这件事**:EEVDF 的常量 slice 不是"简化掉了一个特性",而是"算法上不再需要动态 slice"。CFS 需要动态 slice 是因为它只有一个判据(vruntime),权重表达只能挤进 slice;EEVDF 有 deadline 这个独立判据表达权重,不需要 slice 再掺和。这是从一维(CFS)到三维(EEVDF)的算法升级在时间片设计上的体现。结果是旋钮减少(sched_latency/min_granularity 全砍)、行为可预期、运维负担降低。

---

## 10.8 ★ 对照第 7 本:`pick_next_task_fair` vs Go 的 runnext + FIFO

Linux 的 `pick_next_task_fair`(EEVDF 选下一个)和 Go 的选 goroutine 路径结构完全不同:

- **Linux**:`pick_next_task` 遍历 `sched_class` 链(dl > rt > fair > idle),进 fair 类后调 `pick_next_task_fair` → `pick_eevdf`,用 EEVDF 算法(eligible + deadline)在 `cfs_rq` 的 rbtree 里 O(log N) 选一个。这是一套**显式的策略算法**——有明确的判据(deadline)、有明确的数学(EEVDF)、有明确的旋钮(nice/sched_base_slice)。
- **Go**:每个 P 有个 `runnext` 槽(下次一定跑这个 goroutine)+ 本地队列(256 长 FIFO 环形数组)。`schedule()` 先看 `runnext`,有就直接跑;否则从本地队列头取;本地队列空了就 work-stealing 偷别的 P 队列一半。这是一套**结构性公平**——没有显式策略算法,靠队列位置(FIFO)+ runnext(优先一个)+ work-stealing(均衡)隐式实现公平。

差异反映:**Linux 调度器要服务各种负载(交互/批处理/实时-ish),必须有显式策略(EEVDF + nice + 优先级);Go 的 goroutine 是协作式并发原语,默认全公平,靠 FIFO + work-stealing 就够,不需要显式策略算法。** 第 21 章的对照总表会钉死这组差异。

---

## 章末小结

这一章是第 2 篇的收束,我们把 EEVDF 装配进调度器主路径,立清了四样东西:

1. **`pick_next_task_fair` 的流程**:从根 `cfs_rq` 调 `pick_next_entity`(→`pick_eevdf`)选 entity → `set_next_entity` 设为 curr 并摘出树 → 组调度下逐层下钻到叶子任务。`put_prev_entity` 把 prev 放回树并记账。
2. **`set_next_entity` 的关键操作**:摘出树(curr 不参与 EEVDF 决策,但 `avg_vruntime` 要临时加回它的贡献)、HACK `se->vlag = se->deadline`(为 RUN_TO_PARITY 存截止快照)、`prev_sum_exec_runtime` 记 slice 起点。
3. **EEVDF 时间片的简化**:`se->slice = sysctl_sched_base_slice`(0.75ms 常量),CFS 的 `sched_latency_ns`/`sched_min_granularity`/`sched_wakeup_granularity` 全砍。权重通过 deadline 紧迫度(不是 slice 长短)起作用。
4. **`hrtick_start_fair` 精确抢占**:一次性高精度定时器,在任务被选中时设"slice 到期时刻",到点精确抢占(误差亚微秒),弥补粗 tick(1-4ms)粒度不足。`ran = sum_exec_runtime - prev_sum_exec_runtime` 算已跑时间。

本章还讲了招牌技巧——**EEVDF 常量 slice 的算法依据**(deadline 接管权重表达,slice 只控延迟,职责拆开旋钮减少),以及唤醒抢占(`check_preempt_wakeup_fair` 用 deadline 比较简化)。

本章服务二分法的**策略**面:讲清 EEVDF 策略怎么被 `pick_next_task_fair` 装配进调度器主路径,以及时间片(策略的"跑多久"那一半)在 EEVDF 下的形态。同时它是第 2 篇(策略)和第 3 篇(机制:抢占/切换)的衔接点——`resched_curr` 标记的重调度,要靠第 11 章的抢占点和第 12、13 章的 `__schedule`/`switch_to` 真正落实。

### 五个"为什么"清单

1. **`pick_next_task_fair` 的主干流程?** 从根 `cfs_rq` 调 `pick_next_entity`(→`pick_eevdf` 选 eligible 里最早 deadline 的)→ `set_next_entity`(设为 curr,摘出树)→ 组调度下逐层下钻到叶子任务。`put_prev_task` 把 prev 放回树并记账。
2. **`set_next_entity` 为什么要把 curr 摘出树?** curr 在跑(已分得 CPU),不参与"选下一个"的竞争。摘出避免 `pick_eevdf` 误选它。但算 V(`avg_vruntime`)时要临时加回它的 vruntime 贡献,所以 `avg_vruntime`/`vruntime_eligible`/`pick_eevdf` 都有 `if (curr && curr->on_rq)` 特殊处理。
3. **EEVDF 时间片为什么是常量 0.75ms?** 因为权重不再通过 slice 长短起作用,改通过 deadline 紧迫度。slice 只控延迟/切换频率(0.75ms = 切换间隔上限可预期)。CFS 的动态 slice 把"权重表达"和"延迟控制"耦合在一起,EEVDF 拆开成 slice + deadline 两个独立量,旋钮减少(sched_latency/min_granularity 全砍)。
4. **`hrtick_start_fair` 怎么精确抢占?** 任务被选中时,设一个 `slice - ran` 纳秒后到期的 hrtimer(`ran = sum_exec_runtime - prev_sum_exec_runtime`,本次 slice 已跑时间)。到点触发中断,在中断里 `resched_curr` 标记重调度。hrtimer 高精度,误差亚微秒。粗 tick(1-4ms)粒度太粗,slice 0.75ms 会被 tick 拖到几毫秒才抢。
5. **`se->vlag = se->deadline` 这个 HACK 干嘛?** 在 `set_next_entity` 里把选中时的 deadline 存进 vlag(curr 在跑期间 vlag 字段空闲)。供 `pick_eevdf` 的 `RUN_TO_PARITY` 检查用——如果 curr 的 vlag == deadline(刚被选中、deadline 还没动),让它跑满 slice 不每 tick 重选,减少抖动。

### 想继续深入往哪钻

- 源码:`kernel/sched/fair.c` 的 [`pick_next_task_fair`](../linux/kernel/sched/fair.c#L8398)(L8398)、[`pick_next_entity`](../linux/kernel/sched/fair.c#L5466)(L5466)、[`set_next_entity`](../linux/kernel/sched/fair.c#L5416)(L5416)、[`put_prev_entity`](../linux/kernel/sched/fair.c#L5480)(L5480)、[`hrtick_start_fair`](../linux/kernel/sched/fair.c#L6630)(L6630)、[`check_preempt_wakeup_fair`](../linux/kernel/sched/fair.c#L8286)(L8286)、[`update_deadline`](../linux/kernel/sched/fair.c#L984)(L984)、[`sysctl_sched_base_slice`](../linux/kernel/sched/fair.c#L76)(L76);`kernel/sched/features.h` 的 `NEXT_BUDDY`/`RUN_TO_PARITY`/`WAKEUP_PREEMPTION`(L16/L9/L27);`kernel/sched/core.c` 的 `__schedule`/`pick_next_task`(L6616/L6108,第 12 章详讲)。
- 观测:`perf sched record` 抓调度事件,`perf sched latency` 看每个任务的调度延迟分布(EEVDF 的 deadline 保障应该在尾部更稳定);`/proc/sys/kernel/sched_base_slice`(若可写)看/改 slice 值(注意改了要重启负载才生效)。
- 实验:跑一个 CPU hog + 一个会 sleep/wake 的任务,`trace-cmd record -e sched` 抓调度切换,观察 hrtick 怎么精确触发 slice 用完的重调度。

### 引出下一篇

第 2 篇(EEVDF 公平调度)到此结束。你现在已经能完整讲清"给定一个 `cfs_rq`,EEVDF 怎么选下一个、nice 怎么定权重、PELT 怎么跟踪负载、时间片怎么算、`pick_next_task_fair` 怎么把这一切组装进调度器"。但 `pick_next_task_fair` 只是**决策**——真正把 CPU 切给选中的任务、让被打断的任务之后能恢复,是机制层(抢占/切换)的事。下一篇(第 3 篇),我们钻进 `TIF_NEED_RESCHED` 延迟抢占、`__schedule` 主调度函数、`switch_to` 上下文切换——把"下一个跑谁"落实成"切过去跑"。
