# 第八章 · nice 与权重:从 nice -20..19 到 timeslice

> 篇:第 2 篇 · EEVDF 公平调度:下一个跑谁
> 主线呼应:上一章讲了 EEVDF 的三件套(lag/eligible/deadline),里面反复出现一个量——**权重 `se->load.weight`**。权重决定了 `calc_delta_fair` 的折算比例(权重大的 vruntime 走得慢)、决定了 `vlag` 的限幅幅度、决定了 `deadline` 的紧迫程度(`deadline = vruntime + slice/weight`)。但权重自己从哪来?用户怎么调?这一章回答这个"权重入口"的问题:**nice 值怎么映射成权重、为什么是这个非线性表、renice 之后内核改了什么、SCHED_IDLE 怎么走极端**。

## 核心问题

**用户态的 `nice` 值(-20..19)是 Linux 给普通任务调节 CPU 优先级的唯一接口(实时任务用 `SCHED_FIFO`/`SCHED_RR`,见第 17 章)。nice 不直接决定 CPU 占比,而是先查一张精心设计的非线性表 `sched_prio_to_weight[40]` 得到权重,再由权重驱动 EEVDF 的 vruntime 折算、lag 限幅、deadline 紧迫度。每差一级 nice,权重差约 25%(对应相对 CPU 占比差约 10%),这个非线性设计让 nice 在低端和高端都有意义。`renice` 改的是 `se->load.weight`(以及 `inv_weight`),触发 `reweight_entity`/`reweight_eevdf` 重算 vlag/deadline。SCHED_IDLE 任务权重压到极小(3),几乎拿不到 CPU。**

读完本章你会明白:

1. nice 的范围和语义(-20 最高,19 最低,0 默认)、nice 与 kernel 内部 `static_prio` 的换算(`NICE_TO_PRIO` / `PRIO_TO_NICE`)。
2. `sched_prio_to_weight[40]` 表的设计:nice 0 = 1024,每级差约 25%(1.25 倍),nice -20 = 88761、nice 19 = 15——这张非线性表的设计动机。
3. 权重如何驱动 EEVDF:在 `calc_delta_fair`(vruntime 折算)、`entity_lag`(lag 限幅)、`update_deadline`(deadline 紧迫度)三处的作用,以及"权重翻倍 = 双倍 CPU"是怎么通过这三处协同实现的。
4. `renice` 的内核路径:`set_user_nice` → `set_load_weight` → `reweight_task` → `reweight_entity`/`reweight_eevdf`,以及为什么 renice 时 vlag 要按权重比缩放。
5. ★ 对照第 7 本:Go 的 goroutine 没有 nice/优先级/权重概念,所有 goroutine 一视同仁——这反映了 Go 的协作式并发哲学,与 Linux 通用调度的权重公平是两种设计取向。

---

## 8.1 一句话点破

> **nice 不是直接给 CPU 占比的,它是一个查表的索引——查 `sched_prio_to_weight[40]` 得到权重,再由权重驱动 EEVDF 的折算/限幅/截止。这张表是精心设计的非线性(每级权重差 1.25 倍,对应相对 CPU 差约 10%),让 nice 在低端(nice 19 任务几乎饿不死)和高端(nice -20 任务显著占优)都有意义。如果用线性映射,nice 的差异会要么太陡要么太弱,失去调节价值。**

这是结论,不是理由。本章倒过来拆:先看 nice 的范围和 prio 换算,再看 `sched_prio_to_weight` 表的设计,然后看权重在 EEVDF 三处的具体作用,最后走读 `renice` 的内核路径。

---

## 8.2 nice 的范围与 prio 换算

### 8.2.1 nice 的语义

`nice` 是 Unix 老牌的"让利值":一个进程的 nice 值越高,表示它越"友好"(让出更多 CPU 给别人),自己拿得越少。范围 **-20..19**,默认 0:

- nice -20:最不友好(最贪),拿最多 CPU。
- nice 0:默认,普通任务。
- nice 19:最友好(最让),拿最少 CPU(但不是完全饿死,EEVDF 仍保证按 15:1024 的比例分一点)。

普通用户只能调高 nice(让利),root 可以调低(抢夺)。命令 `nice -n 5 cmd`、`renice -n 5 -p PID`、`top` 里按 `r` 重设——都是改这个值。

### 8.2.2 nice 到 static_prio 的换算

内核内部不直接存 nice,而是存 `static_prio`(见 [`include/linux/sched.h:793`](../linux/include/linux/sched.h#L793) 的 `int static_prio;`)。换算在 [`include/linux/sched/prio.h`](../linux/include/linux/sched/prio.h) 的宏:

```c
#define MAX_RT_PRIO		100              /* RT 任务占 prio 0..99 */
#define MAX_NICE		19
#define NICE_WIDTH		(MAX_NICE - MIN_NICE + 1)   /* 40 */
#define MAX_PRIO		(MAX_RT_PRIO + NICE_WIDTH)  /* 140 */
#define DEFAULT_PRIO		(MAX_RT_PRIO + NICE_WIDTH / 2)  /* 120 */

#define NICE_TO_PRIO(nice)	((nice) + DEFAULT_PRIO)   /* nice 0 → prio 120 */
#define PRIO_TO_NICE(prio)	((prio) - DEFAULT_PRIO)
```

所以 nice 0 对应 `static_prio = 120`,nice -20 对应 100,nice 19 对应 139。RT 任务(第 17 章)占 prio 0..99,普通任务占 100..139。

查表索引是 `prio - MAX_RT_PRIO`,即 `static_prio - 100`:nice 0 → 索引 20,nice -20 → 索引 0,nice 19 → 索引 39。这正是 `sched_prio_to_weight[40]` 的下标范围。`set_load_weight` 里那句 `int prio = p->static_prio - MAX_RT_PRIO;`(见 [`core.c:1329`](../linux/kernel/sched/core.c#L1329))就是这个换算。

> **钉死这件事**:nice 是用户接口(-20..19),`static_prio` 是内核表示(100..139),`prio - MAX_RT_PRIO` 是查表索引(0..39)。三者一一对应,nice 只是 prio 的偏移表达。这套 prio 体系把 RT(0..99)和普通(100..139)统一进一个数字轴,`prio` 小的优先级高。

---

## 8.3 `sched_prio_to_weight[40]`:精心设计的非线性表

### 8.3.1 表的样子

[`kernel/sched/core.c:11518`](../linux/kernel/sched/core.c#L11518)([core.c:11518](../linux/kernel/sched/core.c#L11518))定义了这张表:

```c
/*
 * Nice levels are multiplicative, with a gentle 10% change for every
 * nice level changed. I.e. when a CPU-bound task goes from nice 0 to
 * nice 1, it will get ~10% less CPU time than another CPU-bound task
 * that remained on nice 0.
 *
 * The "10% effect" is relative and cumulative: from _any_ nice level,
 * if you go up 1 level, it's -10% CPU usage, if you go down 1 level
 * it's +10% CPU usage. (to achieve that we use a multiplier of 1.25.
 * If a task goes up by ~10% and another task goes down by ~10% then
 * the relative distance between them is ~25%.)
 */
const int sched_prio_to_weight[40] = {
 /* -20 */     88761,     71755,     56483,     46273,     36291,
 /* -15 */     29154,     23254,     18705,     14949,     11916,
 /* -10 */      9548,      7620,      6100,      4904,      3906,
 /*  -5 */      3121,      2501,      1991,      1586,      1277,
 /*   0 */      1024,       820,       655,       526,       423,
 /*   5 */       335,       272,       215,       172,       137,
 /*  10 */       110,        87,        70,        56,        45,
 /*  15 */        36,        29,        23,        18,        15,
};
```

读法:行标是 nice 起始值,每行 5 个。索引 20(nice 0)= 1024(即 `NICE_0_LOAD / scale_load(1)`,这是基准)。索引 0(nice -20)= 88761,索引 39(nice 19)= 15。极端比 88761 : 15 ≈ 5917 : 1。

### 8.3.2 设计动机:每级差约 10% 相对 CPU

源码注释把设计动机说得很清楚:**每差一级 nice,相对 CPU 占比差约 10%**。怎么实现的?

两个 CPU-bound 任务 A、B,nice 差 1(A 是 nice 0、B 是 nice 1)。它们的权重比 1024 : 820 ≈ 1.249 : 1。在 EEVDF(或 CFS)稳态下,CPU 占比 = 权重比,所以 A : B = 1.249 : 1。

"A 比 B 多多少?"相对 B 看,A 多了 `(1.249 - 1) / 1 = 24.9%`;但注释说 10%,怎么回事?关键在注释那句:"If a task goes up by ~10% and another task goes down by ~10% then the relative distance between them is ~25%"。这是**对称定义**:把 A 的份额看成 1.25x、B 看成 0.8x(因为 nice ±1 各差一级,A 涨 25%、B 跌 20%,几何均值约 1.25),从中间看,上下各偏离约 10-12%,相对距离 25%。每级权重因子 1.25,是对"两边各 10%"的工程化(取 1.25 是为了 4 级累积接近 2 倍:1.25^4 ≈ 2.44)。

> **钉死这件事**:权重比是 1.25 倍一级,这是"相对 CPU 差 10%"在两边对称情况下的折算。每级 nice 在权重量纲上是 1.25 倍,在 CPU 占比量纲上是 10% 级别的相对差异。两个 nice 值差 N 级的任务,CPU 占比 = 1.25^N : 1。差 5 级 = 3.05 倍,差 10 级 = 9.3 倍,差 20 级 = 86.7 倍。

### 8.3.3 不这样会怎样:线性映射会让 nice 没用

> **不这样会怎样**:如果权重和 nice 是线性映射(比如 `weight = 1024 + (20 - nice) * 1000`),会撞两种墙:(a)在低端(nice 18、19),相邻级差对总权重的比例太小,几乎没区别,nice 18 和 19 的任务拿的 CPU 几乎一样,失去调节价值;(b)在高端(nice -20、-19),线性映射要么差太大(让 nice -20 任务直接饿死别人)要么差太小。非线性(乘法 1.25 倍)的关键是**每级的相对差异一致**,无论在表的哪一段,nice 差 1 级 = 相对 CPU 差 10%——这种"对数尺度"的调节在感知上均匀,符合人对优先级的直觉(1 级 nice 就是一档"让一点")。

乘法(非线性)的另一好处:**极端比合理**。nice -20 到 nice 19 是 40 级,1.25^39 ≈ 8450,但表实际取 88761:15 ≈ 5917(末端做了一点裁剪防溢出)。这个比例足够让 nice -20 任务显著占优、nice 19 任务几乎不抢,但又不到完全饿死(nice 19 任务仍按 15:1024 的比例拿到约 1.4% CPU,系统不至于把它彻底挂起)。

> **钉死这件事**:线性映射会让 nice 在低端失效、高端失衡;非线性 1.25 倍乘法让每级 nice 的"调节手感"一致(相对 10% 差异)。这是 Unix 老牌 nice 设计的精髓,Linux 完全继承并用 `sched_prio_to_weight[40]` 把它工程化。表是 `const int`,编译期固定,运行时只查不改——查表 O(1),极快。

---

## 8.4 权重在 EEVDF 三处的驱动作用

权重定了,它怎么影响调度?EEVDF 里权重出现在三个关键位置,协同实现"权重翻倍 = 双倍 CPU"。

### 8.4.1 位置一:`calc_delta_fair`——vruntime 折算

上一章讲过,`update_curr` 里 `curr->vruntime += calc_delta_fair(delta_exec, curr)`。`calc_delta_fair` 的核心(简化):

```
delta_vruntime = delta_exec * NICE_0_LOAD / weight
```

权重大的任务,这一项小——它的 vruntime 走得慢。长期下来,vruntime 落后,被反复选中,拿走更多 CPU。这是 EEVDF(沿用 CFS)实现权重比例的根。

源码 [`calc_delta_fair`](../linux/kernel/sched/fair.c#L296) 和 [`__calc_delta`](../linux/kernel/sched/fair.c#L266)([fair.c:296](../linux/kernel/sched/fair.c#L296), [fair.c:266](../linux/kernel/sched/fair.c#L266)):

```c
static inline u64 calc_delta_fair(u64 delta, struct sched_entity *se)
{
	if (unlikely(se->load.weight != NICE_0_LOAD))
		delta = __calc_delta(delta, NICE_0_LOAD, &se->load);

	return delta;
}
```

`__calc_delta` 用预计算的 `inv_weight`(2^32 / weight,见 [`sched_prio_to_wmult`](../linux/kernel/sched/core.c#L11536))把除法变成乘法:

```c
static u64 __calc_delta(u64 delta_exec, unsigned long weight, struct load_weight *lw)
{
	u64 fact = scale_load_down(weight);
	u32 fact_hi = (u32)(fact >> 32);
	int shift = WMULT_SHIFT;
	...
	__update_inv_weight(lw);
	...
	fact = mul_u32_u32(fact, lw->inv_weight);   /* weight * inv_weight */
	...
	return mul_u64_u32_shr(delta_exec, fact, shift);   /* delta * fact >> shift */
}
```

`inv_weight = 2^32 / weight`,所以 `weight * inv_weight / 2^32 = weight * (2^32/weight) / 2^32 = 1`(归一化)。最终 `__calc_delta(delta, NICE_0_LOAD, &se->load)` 算的是 `delta * NICE_0_LOAD / weight`——正是 vruntime 折算公式。`inv_weight` 的预计算表 `sched_prio_to_wmult[40]` 见 [`core.c:11536`](../linux/kernel/sched/core.c#L11536),和 `sched_prio_to_weight` 一一对应。

> **钉死这件事**:权重在 `calc_delta_fair` 处的作用是"vruntime 折算比例"。权重翻倍 → vruntime 走半快 → 长期 vruntime 落后一倍 → 被选中频率翻倍 → CPU 占比翻倍。这是 EEVDF 实现权重比例的主路径。`inv_weight` 预计算把除法变乘法,是热路径性能优化的典范。

### 8.4.2 位置二:`entity_lag`——lag 限幅

上一章讲过,`entity_lag` 给 lag 限幅:`limit = calc_delta_fair(max_t(u64, 2*se->slice, TICK_NSEC), se)`。注意 limit 也经过 `calc_delta_fair`——所以**权重大(nice 低)的任务,lag 限幅的虚拟量更小**。直观理解:权重大的任务"欠账"的虚拟单位更紧凑(因为它的虚拟时间走得慢),所以同样一段实际欠账折算成虚拟欠账,数值更小。

这个限幅保证:任何任务的 lag 都不会超过"约 2 个 slice 的虚拟量",防止睡醒后霸占。权重大的任务限幅紧(虚拟单位小)、权重小的任务限幅松(虚拟单位大),但折算回实际时间,都是约 2 个 slice——**限幅在虚拟量上随权重变,但物理含义(实际时间)一致**。

### 8.4.3 位置三:`update_deadline`——deadline 紧迫度

上一章讲过,`se->deadline = se->vruntime + calc_delta_fair(se->slice, se)`。`se->slice` 是常量 0.75ms,但折算成虚拟增量时,权重大(nice 低)的任务 `calc_delta_fair(0.75ms)` 更小——它的 deadline 离当前 vruntime 更近(更紧迫)。所以 `pick_eevdf` 会更频繁选中权重大的任务。

```
 同一个 slice = 0.75ms,不同权重的任务算出的"虚拟 deadline 增量":

   nice  0 (weight 1024): vd_incr = 0.75ms * 1024/1024 = 0.75ms      ← 基准
   nice -5 (weight 3121): vd_incr = 0.75ms * 1024/3121 ≈ 0.246ms     ← 更近(更紧迫)
   nice +5 (weight  335): vd_incr = 0.75ms * 1024/335  ≈ 2.296ms     ← 更远(不紧迫)

   → nice -5 的 deadline 每 0.246ms 虚拟时间就到期,被反复选中
   → nice +5 的 deadline 每 2.296ms 虚拟时间才到期,被选中频率低
   → CPU 占比约 3121:335 ≈ 9.3:1,与权重比一致
```

> **钉死这件事**:权重在 EEVDF 里**不是通过"时间片长短"起作用**(时间片 `se->slice` 是常量 0.75ms,所有任务一样),而是通过三处协同:(a) vruntime 折算比例(走得快慢)、(b) lag 限幅虚拟量、(c) deadline 紧迫度。其中 (a) 和 (c) 是主要的——(a) 让权重大的任务 vruntime 落后,(c) 让它的 deadline 更近,两者都让它被更频繁选中,最终 CPU 占比 = 权重比。这是 EEVDF 和 CFS 在"权重如何起作用"上的关键差异:CFS 还部分依赖动态时间片(`sched_slice` 按权重分),EEVDF 时间片固定,权重完全靠 vruntime + deadline 双通道发挥作用。

---

## 8.5 `renice` 的内核路径:改了什么

用户态 `renice` 或 `setpriority` 系统调用,最终调 [`set_user_nice`](../linux/include/linux/sched.h#L1799)(声明),它做几件事:改 `static_prio`、调 `set_load_weight` 重设权重、触发 `reweight_entity` 重算 EEVDF 相关量。

### 8.5.1 `set_load_weight`:查表设权重

[`set_load_weight`](../linux/kernel/sched/core.c#L1327)([core.c:1327](../linux/kernel/sched/core.c#L1327)):

```c
static void set_load_weight(struct task_struct *p, bool update_load)
{
	int prio = p->static_prio - MAX_RT_PRIO;
	struct load_weight *load = &p->se.load;

	/* SCHED_IDLE tasks get minimal weight: */
	if (task_has_idle_policy(p)) {
		load->weight = scale_load(WEIGHT_IDLEPRIO);   /* 3 */
		load->inv_weight = WMULT_IDLEPRIO;             /* 1431655765 */
		return;
	}

	if (update_load && p->sched_class == &fair_sched_class) {
		reweight_task(p, prio);
	} else {
		load->weight = scale_load(sched_prio_to_weight[prio]);
		load->inv_weight = sched_prio_to_wmult[prio];
	}
}
```

三件事:

1. **SCHED_IDLE 特例**:如果任务是 `SCHED_IDLE` 策略(用户 `chrt -i` 或内核 `TASKSCHED_IDLE`),权重压到 3(`WEIGHT_IDLEPRIO`,见 [`sched.h:2201`](../linux/kernel/sched/sched.h#L2201))——比 nice 19 的 15 还小 5 倍,几乎拿不到 CPU,但不完全饿死。这是"让一个任务几乎不占 CPU 但仍存活"的机制。
2. **`scale_load`**:在 64 位平台把权重左移 10 位(`SCHED_FIXEDPOINT_SHIFT`,见 [`include/linux/sched.h:414`](../linux/include/linux/sched.h#L414))。这是为了在 cgroup 组调度场景下提高权重计算精度(见第 19 章)。所以 `se->load.weight` 存的是 `sched_prio_to_weight[prio] << 10`,读取时用 `scale_load_down` 折回来。nice 0 任务 `weight = 1024 << 10 = 1048576 = 1 << 20 = NICE_0_LOAD`(64 位上 `NICE_0_LOAD_SHIFT = 20`)。
3. **`reweight_task`**:如果任务已在 fair 类且要求更新,调 `reweight_task` 做完整重算(包括 EEVDF 的 vlag/deadline)。

### 8.5.2 `reweight_task` 与 `reweight_entity`:重算 EEVDF 量

[`reweight_task`](../linux/kernel/sched/fair.c#L3844)([fair.c:3844](../linux/kernel/sched/fair.c#L3844))调 [`reweight_entity`](../linux/kernel/sched/fair.c#L3791):

```c
void reweight_task(struct task_struct *p, int prio)
{
	struct sched_entity *se = &p->se;
	struct cfs_rq *cfs_rq = cfs_rq_of(se);
	struct load_weight *load = &se->load;
	unsigned long weight = scale_load(sched_prio_to_weight[prio]);

	reweight_entity(cfs_rq, se, weight);
	load->inv_weight = sched_prio_to_wmult[prio];
}
```

`reweight_entity` 做几件事(见 [`fair.c:3791`](../linux/kernel/sched/fair.c#L3791)):

1. `update_curr(cfs_rq)`:先把当前已跑的时间记账到 vruntime。
2. 如果任务在队(`on_rq`),`__dequeue_entity` 摘出、`update_load_sub` 减旧权重。
3. 调 `reweight_eevdf` 重算 vruntime 和 deadline(关键,见下)。
4. 如果不在队,缩放 `vlag`(因为 vlag 是虚拟量,权重变了要按比例换算)。
5. `update_load_set` 设新权重。
6. 如果在队,重新入队。

### 8.5.3 `reweight_eevdf`:为什么 renice 要改 vruntime

最关键的是 `reweight_eevdf`(见 [`fair.c:3685`](../linux/kernel/sched/fair.c#L3685))。源码注释有一段精彩推导(节选):

```
* VRUNTIME
* ========
*
* COROLLARY #1: The vruntime of the entity needs to be adjusted if re-weight
* at !0-lag point.
*
* ... 推导见源码 ...
*
*  ==> v_i' = v_i * (w_i / w_i') + V * (1 - w_i/w_i')
```

意思是:权重从 `w_i` 变成 `w_i'`,如果任务的 vruntime 直接不动,那它的 lag(`= w_i * (V - v_i)`)会因权重变化而失真——因为 lag 在 EEVDF 里要"跨权重变化保留"(否则 renice 就等于重置公平)。所以必须按公式调整 vruntime:

```
v_i' = v_i * (w_i / w_i') + V * (1 - w_i/w_i')
```

这个调整保证:renice 前后,任务的"实际欠账"(lag,以加权 service 算)不变。同时 deadline 也要按比例缩放(见 [`fair.c:3786`](../linux/kernel/sched/fair.c#L3786))。

> **钉死这件事**:`renice` 改的不只是 `se->load.weight`。如果任务当前在队且 lag ≠ 0,它的 `vruntime` 和 `deadline` 都要按公式调整——否则 renice 会顺便重置任务的公平状态(被欠的或欠的清零),破坏公平。这是 EEVDF 工程化的一个非显然细节:CFS 时代 renice 直接改权重就行(vruntime 不用动,因为 CFS 没有 lag 概念),EEVDF 因为引入了 lag,renice 必须协同调整 vruntime/deadline 才能保公平。源码注释里那段推导(COROLLARY #1)是 EEVDF 工程严谨性的体现。

---

## 8.6 技巧精解:`inv_weight` 预计算——把除法变成乘法

这一章最硬核的工程技巧是 `sched_prio_to_wmult[]` 这张**预计算倒数表**。调度器的热路径(`calc_delta_fair` 在每次 tick 都跑)要算 `delta * NICE_0_LOAD / weight`——一次 64 位除法。除法在 CPU 上要几十周期,热路径上千万次调用,开销巨大。怎么消除?

### 8.6.1 预计算倒数

把 `1/weight` 预计算成 `inv_weight = 2^32 / weight`(整数近似),存进 `sched_prio_to_wmult[40]`(和 `sched_prio_to_weight[40]` 一一对应):

```c
const u32 sched_prio_to_wmult[40] = {
 /* -20 */     48388,     59856,     76040,     92818,    118348,
 /* -15 */    147320,    184698,    229616,    287308,    360437,
 /* -10 */    449829,    563644,    704093,    875809,   1099582,
 /*  -5 */   1376151,   1717300,   2157191,   2708050,   3363326,
 /*   0 */   4194304,   5237765,   6557202,   8165337,  10153587,
 /*   5 */  12820798,  15790321,  19976592,  24970740,  31350126,
 /*  10 */  39045157,  49367440,  61356676,  76695844,  95443717,
 /*  15 */ 119304647, 148102320, 186737708, 238609294, 286331153,
};
```

验证:nice 0 的 weight = 1024,inv_weight = 2^32 / 1024 = 4194304 ✓(表里 `/* 0 */` 第一个就是 4194304)。

### 8.6.2 运行时:乘法 + 移位替代除法

[`__calc_delta`](../linux/kernel/sched/fair.c#L266) 的核心是把 `delta / weight` 变成 `delta * inv_weight >> 32`:

```c
fact = scale_load_down(weight);                /* = NICE_0_LOAD(传入) */
...
__update_inv_weight(lw);                       /* 懒加载 inv_weight */
...
fact = mul_u32_u32(fact, lw->inv_weight);      /* fact = weight * inv_weight */
...
return mul_u64_u32_shr(delta_exec, fact, shift);   /* delta * fact >> shift */
```

`weight * inv_weight / 2^32 ≈ weight * (2^32/weight) / 2^32 = 1`,但因为 `inv_weight` 是整数近似,要做一点 shift 调整(`__calc_delta` 里的 `fact_hi` / `fs` 处理)保证精度。最终结果等价于 `delta_exec * weight / lw->weight`,但全程无除法(只有 32 位乘法 + 移位)。

> **反面对比**:如果直接用 64 位除法 `delta / weight`,每次 tick(每核每秒上千次)、每次 `__calc_delta` 调用都做除法,累加开销显著。预计算倒数把除法变成乘法 + 移位,开销降到几周期。这是内核里"用空间换时间、用预处理换热路径性能"的标准操作,和 mm 的 per-cpu pageset、第 8 本《内存分配器》的 per-CPU cache 是同一类思路。代价是表多占 40 * 4 = 160 字节(可忽略),且 inv_weight 的精度有上限(但对 nice 调度精度足够)。

> **钉死这件事**:`sched_prio_to_wmult[]` 是 `sched_prio_to_weight[]` 的预计算倒数(2^32/weight)。调度器热路径算 `delta / weight` 时,用 `delta * inv_weight >> 32` 替代,把 64 位除法变成 32 位乘法 + 移位。这是 EEVDF(也是 CFS)在权重计算上最关键的性能优化,没有它,调度器每秒要多花几百万周期在除法上。

---

## 8.7 ★ 对照第 7 本:nice/权重在 Go 里没有对应物

Linux 的 nice/权重体系在 Go runtime 里**完全没有**:

- Go 的 goroutine 没有 nice、没有 prio、没有 weight。所有 goroutine 在 Go 调度器眼里**完全平等**。
- Go 没有任何 API 让一个 goroutine "比另一个 goroutine 多拿 CPU"。如果业务需要优先级,只能:(a) 业务层手撸——优先级高的 goroutine 多跑、低的主动 `runtime.Gosched()` 让出;(b) 用 `GOMAXPROCS` 给某个线程组隔离一个 P;(c) 用 cgroup(把 Go 进程整体 renice,但这影响所有 goroutine)。这些都不是"goroutine 级优先级"。
- 这反映了 Go 的设计哲学:goroutine 是协作式并发原语,**默认全公平,优先级是业务问题不是调度器问题**。Linux 的 nice 体系是为"一个系统上跑各种互相竞争的进程"设计的(交互式、批处理、后台守护进程混跑),必须有优先级;Go 通常一个进程内跑一种业务(虽然 goroutine 多但目标一致),不需要进程内优先级。

这组差异是第 21 章对照总表的重要一行:**Linux 调度器有权重公平(nice → weight → EEVDF),Go 调度器有结构公平(FIFO + runnext + work-stealing),两者服务的场景不同。**

---

## 章末小结

这一章是 EEVDF 的"权重入口",我们立清了四样东西:

1. **nice 到权重的映射**:nice(-20..19)→ static_prio(100..139)→ 查表索引(0..39)→ `sched_prio_to_weight[40]` 查得权重。nice 0 = 1024(`NICE_0_LOAD` 的基准)。
2. **`sched_prio_to_weight` 表的设计**:每级权重差 1.25 倍,对应相对 CPU 差约 10%。非线性(乘法)让 nice 在低端和高端都有调节价值——线性映射会让 nice 失效。
3. **权重在 EEVDF 三处的驱动**:calc_delta_fair(vruntime 折算比例)、entity_lag(lag 限幅虚拟量)、update_deadline(deadline 紧迫度)。三者协同,权重翻倍 → vruntime 走半快 + deadline 更近 → 被选中频率翻倍 → CPU 占比翻倍。时间片 `se->slice` 是常量,权重不通过时间片长短起作用(这是 EEVDF 和 CFS 的关键差异)。
4. **`renice` 的内核路径**:`set_user_nice` → `set_load_weight`(查表设 weight + inv_weight)→ `reweight_task` → `reweight_entity`/`reweight_eevdf`(重算 vruntime/deadline 以保 lag 不失真)。SCHED_IDLE 任务权重压到 3,几乎拿不到 CPU 但不饿死。

本章还讲了最硬核的工程技巧——**`sched_prio_to_wmult[]` 预计算倒数表**,把热路径的除法变成乘法 + 移位。

本章服务二分法的**策略**面:讲清"用户态 nice 怎么变成 EEVDF 用的权重,权重怎么驱动公平调度"——这是用户接口和 EEVDF 算法之间的桥梁。

### 五个"为什么"清单

1. **nice 的范围和语义?** -20..19,-20 最贪、19 最让、0 默认。nice 高 = 让出更多 CPU。普通用户只能调高,root 可调低。
2. **nice 怎么变成权重?** nice → `static_prio`(120 + nice)→ 查表索引(`prio - 100`)→ `sched_prio_to_weight[40]` 查得权重。nice 0 = 1024。`set_load_weight` 干这事。
3. **为什么权重表是非线性(1.25 倍一级)?** 每级 nice 对应相对 CPU 差约 10%,线性映射会让 nice 在低端失效(相邻级无区别)、高端失衡。非线性(乘法)让每级 nice 的"调节手感"一致。
4. **权重在 EEVDF 里怎么起作用?** 不通过时间片长短(slice 是常量 0.75ms),而是通过三处:vruntime 折算比例(calc_delta_fair)、lag 限幅虚拟量(entity_lag)、deadline 紧迫度(update_deadline)。权重翻倍 → CPU 占比翻倍,通过 vruntime + deadline 双通道实现。
5. **renice 时为什么 vruntime 也要改?** EEVDF 的 lag 要跨权重变化保留(`reweight_eevdf` 的 COROLLARY #1)。如果直接改权重不动 vruntime,lag 会失真,renice 等于重置公平。所以按公式 `v_i' = v_i*(w/w') + V*(1-w/w')` 调整。

### 想继续深入往哪钻

- 源码:`kernel/sched/core.c` 的 [`sched_prio_to_weight`](../linux/kernel/sched/core.c#L11518)(L11518)、[`sched_prio_to_wmult`](../linux/kernel/sched/core.c#L11536)(L11536)、[`set_load_weight`](../linux/kernel/sched/core.c#L1327)(L1327);`kernel/sched/fair.c` 的 [`calc_delta_fair`](../linux/kernel/sched/fair.c#L296)(L296)、[`__calc_delta`](../linux/kernel/sched/fair.c#L266)(L266)、[`reweight_entity`](../linux/kernel/sched/fair.c#L3791)(L3791)、[`reweight_eevdf`](../linux/kernel/sched/fair.c#L3685)(L3685);`include/linux/sched/prio.h` 的 `NICE_TO_PRIO`/`MAX_RT_PRIO`;`kernel/sched/sched.h` 的 [`NICE_0_LOAD`](../linux/kernel/sched/sched.h#L158)(L158)、`scale_load`/`scale_load_down`(L135/L136)。
- 实验:`nice -n 19 burnCPU &; nice -n -20 burnCPU &` 跑两个 CPU hog,`top` 看 CPU 占比,验证约 88761:15 ≈ 5917:1(会被 cgroup 和其他系统任务摊薄,但趋势明显)。
- `renice -n 5 -p PID` 后,`cat /proc/PID/sched | grep -E 'weight|nice|vruntime|deadline'` 看字段变化(需要内核开 `CONFIG_SCHED_DEBUG`)。
- SCHED_IDLE:`chrt -i 0 burnCPU &`,观察它几乎拿不到 CPU 但仍存活。

### 引出下一章

权重讲清楚了,但 EEVDF/负载均衡/CPU 频率调节还需要一个量——**任务"有多忙"**。一个 nice 0 的任务,可能 100% 跑满 CPU,也可能只是偶尔跑一下(平均 10%)。调度器怎么知道?靠 PELT(Per-Entity Load Tracking)——每个 `sched_entity` 维护一个按几何级数衰减的负载/利用率(`load_avg`/`util_avg`),用来驱动负载均衡(第 15 章)和 CPU 频率调节(cpufreq)。下一章,我们钻进 PELT 的几何衰减。
