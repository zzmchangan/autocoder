# 附录 A · 全景脉络

> 一张图看懂全书。本附录把前 20 章缝成**端到端旅程**——任务从 `fork` 到 running、从 running 到阻塞、从被唤醒到再被选中、以及在核间被搬来搬去的全过程。以图为主(mermaid 时序图 + 状态图 + 流程图),配少量说明。如果你读完 20 章后想"在脑子里放映一遍内核调度的全过程",这里是放映厅。

本附录三张图:

- **图 A-1**:任务从 `fork` 到 running 的端到端时序总图(串起 activate_task → enqueue_entity → pick_next_task → context_switch → switch_to → scheduler_tick → resched → 再 schedule)。
- **图 A-2**:SMP 负载均衡全景图(调度域分层 → load_balance → detach/attach)。
- **图 A-3**:抢占/切换状态机(任务的可运行/阻塞/被抢占流转)。

---

## 图 A-1:任务从 fork 到 running 的端到端时序

这张图串起全书最核心的一条路径:一个任务从被 `fork` 创建,到被 EEVDF 选中,到上下文切换切上去跑,到时间片耗尽被抢占,再到被重新选中。涉及章节:P1-05(入队)、P2-07/10(EEVDF 选)、P3-11/12/13(抢占/`__schedule`/`switch_to`)、P1-04(时钟/tick)。

```mermaid
sequenceDiagram
    autonumber
    participant P as 父任务 parent
    participant K as 内核(fork 路径)
    participant RQ as CPU 0 的 rq(本核)
    participant CFS as cfs_rq(EEVDF 红黑树)
    participant CLK as 时钟(tick / hrtick)
    participant SCH as __schedule
    participant CTX as context_switch
    participant SW as switch_to(宏)
    participant T as 新任务 task T

    Note over P,K: ① 创建(P1-05 wake_up_new_task)
    P->>K: fork()/clone()
    K->>K: copy_process 建好 task_struct T
    K->>K: select_task_rq(T, WF_FORK) 选 CPU(优先空闲/同 LLC)
    K->>RQ: wake_up_new_task → activate_task(rq, T)
    RQ->>CFS: enqueue_task_fair → enqueue_entity<br/>(place_entity 算 vlag/deadline,<br/>se->vruntime = V - lag, 挂红黑树按 deadline 排)
    RQ->>RQ: T->on_rq = TASK_ON_RQ_QUEUED
    RQ->>RQ: wakeup_preempt:要不要抢 parent?

    Note over RQ,SCH: ② 选下一个(P2-10 pick_next_task_fair,<br/>P2-07 EEVDF)
    CLK-->>RQ: scheduler_tick / hrtick 到点
    RQ->>RQ: task_tick_fair → update_curr(累 vruntime)<br/>→ update_deadline(vruntime≥deadline?)
    RQ->>RQ: resched_curr: set TIF_NEED_RESCHED on curr
    Note over RQ: curr 走到抢占点(preempt_enable/<br/>中断返回),preempt_count 归零
    RQ->>SCH: preempt_schedule → __schedule(SM_PREEMPT)

    SCH->>SCH: preempt_disable + rq_lock + update_rq_clock
    SCH->>SCH: pick_next_task: for_each_class(stop>dl>rt>fair>idle)
    Note over SCH: fair 类有就绪 → pick_next_task_fair
    SCH->>CFS: pick_next_entity → pick_eevdf<br/>(在 eligible 里选 deadline 最早的 T)
    CFS-->>SCH: 返回 sched_entity T
    SCH->>CFS: set_next_entity: __dequeue_entity(T) 摘出树<br/>se->vlag=se->deadline(HACK 快照)<br/>prev_sum_exec_runtime = sum_exec_runtime

    Note over SCH,SW: ③ 上下文切换(P3-13 switch_to)
    SCH->>CTX: context_switch(prev, next=T)
    CTX->>CTX: prepare_task_switch(perf/rseq/notifier)
    CTX->>CTX: switch_mm(prev->active_mm, T->mm)<br/>(切 CR3 页表,若 T 是内核线程复用 active_mm)
    CTX->>CTX: prepare_lock_switch(rq->lock 持有者改记 T)
    CTX->>SW: switch_to(prev, T, prev)
    SW->>SW: 保存 prev 寄存器到 prev 栈
    SW->>SW: prev->thread.sp = 当前 rsp
    SW->>SW: rsp = T->thread.sp  ← 切栈!控制权交给 T
    Note over SW: prev 暂停,等未来被切回<br/>T 从它当年 switch_to 之后继续

    SW->>T: T 的执行流恢复(barrier 后)
    T->>CTX: finish_task_switch(T 当年的 prev)
    CTX->>CTX: rq = this_rq()(重算,可能换 CPU)<br/>finish_task(prev): prev->on_cpu = 0<br/>finish_lock_switch: 放 rq->lock, 开中断
    Note over T: T 真正在 CPU 上跑了

    Note over T,CLK: ④ 跑期间(P2-07 update_curr,<br/>P2-09 PELT, P2-10 slice)
    T->>T: 在 CPU 上执行业务代码
    CLK-->>RQ: scheduler_tick(HZ 频率)
    RQ->>CFS: task_tick_fair → update_curr<br/>T->se.vruntime += calc_delta_fair(delta_exec)
    RQ->>RQ: update_deadline: vruntime ≥ deadline?<br/>PELT: accumulate_sum 几何衰减累积 load_avg

    Note over T,SCH: ⑤ 时间片耗尽,被抢占(回到 ②)
    RQ->>RQ: hrtick_start_fair 到点 OR<br/>update_deadline 发现 vruntime≥deadline
    RQ->>RQ: resched_curr: set TIF_NEED_RESCHED on T
    Note over T: T 走到抢占点,检查到 flag,preempt_count 归零
    T->>SCH: __schedule(SM_PREEMPT) 选中下一个任务 B
    SCH->>CTX: context_switch(T, B) → switch_to(T, B, T)
    Note over T: T 被切下,B 切上<br/>T 停在 switch_to 内部,等未来被切回

    Note over T,RQ: ⑥ T 阻塞或被均衡(分支)
    alt T 主动 sleep(等条件)
        T->>SCH: set_current_state(TASK_INTERRUPTIBLE) + schedule()
        SCH->>SCH: __schedule 里 deactivate_task(DEQUEUE_SLEEP)<br/>T->on_rq = 0, 出队
        Note over T: T 睡眠,等 try_to_wake_up 唤醒(P1-05)
    else T 被 load_balance 搬到别的核(P4-15)
        RQ->>RQ: detach_tasks: T->on_rq = TASK_ON_RQ_MIGRATING
        RQ->>RQ: set_task_cpu(T, new_cpu)
        Note over RQ: attach_tasks 在新核 rq 上重新 enqueue
    end
```

### 图 A-1 读图要点

1. **(1)→(2) fork 创建到入队**:父任务调 `fork` → 内核 `copy_process` 建好新 `task_struct` → `wake_up_new_task` → `select_task_rq` 选个 CPU(优先 prev_cpu/同 LLC idle 核)→ `activate_task` → `enqueue_task_fair` → `enqueue_entity`。`place_entity` 用 `vlag` 补偿算 `se->vruntime = V - lag` 和 `se->deadline`(第 7 章 EEVDF)。
2. **(2)→(3) 抢占点到 `__schedule`**:时钟 tick 或 hrtick 到点 → `task_tick_fair` 更新 vruntime、检查 deadline → `resched_curr` 设 `TIF_NEED_RESCHED` → curr 走到抢占点(`preempt_enable`/中断返回)且 `preempt_count` 归零 → `preempt_schedule` → `__schedule`(第 11/12 章)。
3. **(3)→(4) EEVDF 选 + 切换**:`pick_next_task` 遍历 `sched_class` 链 → fair 类的 `pick_next_task_fair` → `pick_eevdf` 在 eligible 任务里选 deadline 最早的 → `set_next_entity` 摘出树 → `context_switch` → `switch_to` 切栈切寄存器(第 7/10/13 章)。
4. **(4)→(5) `switch_to` 的两个返回点**:`switch_to(prev, T, prev)` 之后,代码事实上在 **T 的执行流** 上跑(T 当年被切走时的返回点)。`finish_task_switch` 处理 T 当年的 prev,放锁清 `on_cpu`。T 真正在 CPU 上跑(第 13 章)。
5. **(5)→(6) 跑期间记账**:T 执行业务代码。每个 tick,`update_curr` 把刚跑的时间按权重折算累到 `vruntime`,`update_deadline` 检查 deadline,PELT 几何衰减累积 `load_avg`/`util_avg`(第 7/9 章)。
6. **(6)→(2) 时间片耗尽再抢占**:hrtick 到点或 `vruntime ≥ deadline` → `resched_curr` 设 flag → T 走到抢占点 → `__schedule` 重选下一个 → `switch_to(T, B, T)` 把 T 切下。T 停在 `switch_to` 内部,等未来被切回。
7. **(7) 分支:阻塞或被均衡**:T 可能主动 sleep(`deactivate_task(DEQUEUE_SLEEP)` 出队)或被 `load_balance` 搬到别的核(`TASK_ON_RQ_MIGRATING` 状态)(第 5/15 章)。

---

## 图 A-2:SMP 负载均衡全景

这张图串起第 4 篇(P4-14 调度域、P4-15 `load_balance`、P4-16 迁移)的全过程:从硬件拓扑抽象成调度域层次树,到周期/newidle 触发,到 pull 模型三步走,到 detach/attach 摘挂任务。

```mermaid
flowchart TD
    subgraph TOPO["硬件拓扑(物理)"]
        N0["NUMA node 0<br/>(物理 CPU 0: core0 HT0/HT1, core1 HT0/HT1)"]
        N1["NUMA node 1<br/>(物理 CPU 1: ...)"]
        N0 <-.QPI 跨节点.-> N1
    end

    subgraph SD["调度域层次树(P4-14,<br/>每 CPU 一条自底向上的链)"]
        SMT["SMT 域<br/>(imbalance_pct=110,<br/>cache_nice_tries=0,<br/>SD_SHARE_CPUCAPACITY)"]
        MC["MC 域(共享 LLC)<br/>(imbalance_pct=117,<br/>cache_nice_tries=1,<br/>SD_SHARE_LLC)"]
        PKG["PKG 域(物理 CPU)"]
        NUMA["NUMA 域<br/>(imbalance_pct=117,<br/>cache_nice_tries=2,<br/>SD_NUMA, SD_SERIALIZE)"]
        SMT --> MC --> PKG --> NUMA
    end

    TOPO -.抽象成.-> SD

    subgraph TRIG["触发(P4-15)"]
        T1["scheduler_tick(每 HZ tick)"]
        T1 -->|"trigger_load_balance:<br/>到 next_balance 才 raise"| SI["SCHED_SOFTIRQ"]
        SI --> RD["run_rebalance_domains<br/>→ rebalance_domains<br/>(for_each_domain 自底向上)"]
        T2["schedule 即将 idle<br/>(运行队列空)"]
        T2 --> NB["newidle_balance<br/>(avg_idle < max_newidle_lb_cost 跳过)"]
    end

    RD --> LB
    NB --> LB

    subgraph LB["load_balance 主循环(P4-15,<br/>pull 模型三步走)"]
        B1["should_we_balance?<br/>(组里只有 balance CPU 发起,<br/>NEWLY_IDLE 例外)"]
        B1 -->|"是"| B2["find_busiest_group<br/>(遍历 sched_group,<br/>消费 PELT load_avg,<br/>imbalance_pct 阈值)"]
        B2 -->|"找到 busiest_group"| B3["find_busiest_queue<br/>(在 busiest 组里选最忙 rq)"]
        B3 -->|"找到 busiest_rq"| B4["detach_tasks<br/>(持 src_rq->lock,<br/>can_migrate_task:<br/>task_hot/cache_nice_tries/亲和)"]
        B4 --> B5["attach_tasks<br/>(持 dst_rq->lock,<br/>activate_task + wakeup_preempt)"]
        B5 -->|"拉到任务"| DONE["更新 next_balance"]
        B4 -.->|"拉不动 + need_active_balance"| AB
    end

    AB["active_balance(P4-15)<br/>stop_one_cpu_nowait 踢源 CPU<br/>migration/N(stop_sched_class 最高优先级)"]
    AB --> AB2["源 CPU 主动把跑着的<br/>任务推出来(misfit/asym/单任务)"]
    AB2 --> DONE

    B4 -.任务在迁移期间.-> MIG["TASK_ON_RQ_MIGRATING<br/>(全调度器可见,<br/>唤醒路径自旋等迁完)"]
    MIG -.让.-> UNLOCK["detach/attach 中间松 src_rq->lock<br/>(避免双 rq 锁的全局顺序约束)"]

    classDef topo fill:#fee2e2,stroke:#dc2626
    classDef sd fill:#dbeafe,stroke:#2563eb
    classDef trig fill:#fef3c7,stroke:#d97706
    classDef lb fill:#dcfce7,stroke:#16a34a
    classDef ab fill:#f3e8ff,stroke:#9333ea
    class N0,N1 topo
    class SMT,MC,PKG,NUMA sd
    class T1,T2,SI,RD,NB trig
    class B1,B2,B3,B4,B5,DONE lb
    class AB,AB2,UNLOCK ab
```

### 图 A-2 读图要点

1. **硬件拓扑 → 调度域层次树**:SMT(同核超线程,共享 L1/L2)→ MC(共享 LLC)→ PKG(物理 CPU)→ NUMA。每层有自己的 `imbalance_pct`(SMT 110/MC 117)、`cache_nice_tries`(MC 1/NUMA 2)、`balance_interval`。越往上层,容忍度越高、迁移代价越大、均衡频率越低。
2. **两种触发**:周期均衡(`trigger_load_balance` 在 tick 里 raise `SCHED_SOFTIRQ`,按 `balance_interval` 节奏)和 newidle 均衡(schedule 即将 idle 时主动拉,`avg_idle < max_newidle_lb_cost` 成本闸门)。两个入口共享同一个 `load_balance` 主循环,差别在 `env.idle`。
3. **pull 模型三步走**:`should_we_balance`(组里只有 balance CPU 发起,避免惊群)→ `find_busiest_group`(消费 PELT load_avg,imbalance_pct 阈值)→ `find_busiest_queue`(选最忙 CPU)→ `detach_tasks` + `attach_tasks`。
4. **`TASK_ON_RQ_MIGRATING` 状态机替代双锁**:`detach_tasks` 持 `src_rq->lock`、`attach_tasks` 持 `dst_rq->lock`,中间松锁。任务在迁移期间 `on_rq = TASK_ON_RQ_MIGRATING`,全调度器可见,唤醒路径自旋等迁完——避免双 rq 锁的全局顺序约束。
5. **active balance**:pull 模型对"正在跑"的任务无能为力,通过 `stop_one_cpu_nowait` 踢源 CPU 的 migration 线程(最高优先级 `stop_sched_class`),由源 CPU 主动把任务推出来。处理 misfit(异构算力)/asym packing/单任务等硬场景。

---

## 图 A-3:任务状态流转机(可运行/阻塞/被抢占)

这张图把任务在几种状态间的流转画清:可运行(TASK_RUNNING + on_rq=1)、正在跑(rq->curr)、阻塞睡眠(TASK_INTERRUPTIBLE/UNINTERRUPTIBLE + on_rq=0)、被迁移(TRANSIENT)、已死(TASK_DEAD)。涉及章节:P1-05(唤醒/睡眠)、P3-11/12(抢占)、P3-13(切换)、P4-15(迁移)。

```mermaid
stateDiagram-v2
    [*] --> 就绪可运行: fork/wake_up_new_task<br/>activate_task → enqueue<br/>(on_rq=QUEUED, state=RUNNING)

    就绪可运行 --> 正在跑: 被 pick_next_task 选中<br/>set_next_entity → switch_to 切上<br/>(P2-10, P3-13)
    正在跑 --> 就绪可运行: 时间片到/被更高优先级抢占<br/>resched_curr + __schedule<br/>switch_to 切下但仍 on_rq=QUEUED<br/>(P3-11/12, P2-10)

    正在跑 --> 阻塞睡眠: 主动 schedule()<br/>set_current_state(非RUNNING) + schedule<br/>__schedule 里 deactivate_task(DEQUEUE_SLEEP)<br/>on_rq=0<br/>(P1-05, P3-12)
    阻塞睡眠 --> 就绪可运行: try_to_wake_up<br/>选核 + ttwu_queue → activate_task<br/>on_rq=QUEUED, state=RUNNING<br/>wakeup_preempt 判断抢<br/>(P1-05)

    就绪可运行 --> 迁移中: load_balance detach_tasks<br/>on_rq=MIGRATING<br/>(P4-15)
    迁移中 --> 就绪可运行: attach_tasks 在新核 rq 上 activate<br/>on_rq=QUEUED<br/>(P4-15)
    正在跑 --> 迁移中: active_balance 踢 migration 线程<br/>源 CPU 把跑着的任务推出<br/>(P4-15)

    正在跑 --> 已死: do_exit/do_task_dead<br/>finish_task_switch 检测 TASK_DEAD<br/>put_task_struct_rcu_user 回收<br/>(P3-13)
    已死 --> [*]

    note right of 正在跑
        rq->curr 指向它
        se 已从 cfs_rq 红黑树摘出(set_next_entity)
        每个 tick: update_curr 累 vruntime
        hrtick 到点或 vruntime≥deadline:
          resched_curr 设 TIF_NEED_RESCHED
        走到抢占点且 preempt_count 归零:
          __schedule 重选
    end note

    note right of 阻塞睡眠
        D 状态(TASK_UNINTERRUPTIBLE)贡献 load
        S 状态(TASK_INTERRUPTIBLE)可被信号唤醒
        不在 rq 上,不被 pick_next_task 看到
        等 try_to_wake_up 唤醒(条件满足/信号/IO 完成)
    end note

    note right of 迁移中
        TASK_ON_RQ_MIGRATING
        全调度器可见,唤醒路径自旋等迁完
        暂时不在 src_rq 也不在 dst_rq
        load_balance 或 sched_setaffinity 触发
    end note
```

### 图 A-3 读图要点

1. **就绪可运行 ↔ 正在跑**:这是最频繁的流转。任务被 `pick_next_task` 选中(`set_next_entity` 摘出树)+ `switch_to` 切上 → 进入"正在跑";时间片到或被更高优先级任务抢占(`resched_curr` + `__schedule` + `switch_to` 切下)→ 回到"就绪可运行"(仍在 `cfs_rq` 上,`on_rq=QUEUED`)。
2. **正在跑 → 阻塞睡眠**:任务调 `schedule()` 主动让出(等条件/IO/锁)。`__schedule` 检查 `prev->__state` 非 RUNNING,调 `deactivate_task(DEQUEUE_SLEEP)` 出队,`on_rq=0`。注意:阻塞和切换发生在**同一次** `__schedule` 里(第 5 章)。
3. **阻塞睡眠 → 就绪可运行**:`try_to_wake_up` 唤醒——拿 `pi_lock`、`smp_mb__after_spinlock`、`select_task_rq` 选核、`set_task_cpu`、`ttwu_queue` → `ttwu_do_activate`(activate_task 入队 + wakeup_preempt 判断抢)。唤醒路径的内存序(`on_cpu` 的 release/acquire 配对)防丢唤醒和 schedule 中途冲突(第 5 章)。
4. **就绪可运行 ↔ 迁移中**:`load_balance` 的 `detach_tasks` 把任务从源 rq 摘下(`on_rq=MIGRATING`),`attach_tasks` 在目标 rq 上重新 enqueue。`TASK_ON_RQ_MIGRATING` 状态让任务"在迁移中"对全调度器可见,唤醒路径自旋等迁完(第 15 章)。
5. **正在跑 → 已死**:任务 `do_exit`/`do_task_dead`,`finish_task_switch` 检测 `TASK_DEAD`,调 `put_task_struct_rcu_user` 回收 `task_struct`。这是任务最后一次切换——不会再被切回(第 13 章)。

---

## 附:数据结构嵌套速查

把全书反复出现的几个核心数据结构画成一张嵌套图,帮你记住"谁套着谁":

```
 物理 CPU N
   └─ struct rq (每 CPU 一个,DEFINE_PER_CPU_SHARED_ALIGNED)
       ├─ raw_spinlock_t __lock      (本 rq 一致性锁)
       ├─ unsigned int nr_running    (cfs+rt+dl 总和)
       ├─ struct task_struct *curr   (当前在跑)
       ├─ struct task_struct *idle   (idle 线程)
       ├─ struct task_struct *stop   (migration/stop 线程)
       ├─ struct cfs_rq cfs          (公平子队列)
       │    ├─ struct load_weight load       (总权重)
       │    ├─ unsigned int nr_running / h_nr_running
       │    ├─ s64 avg_vruntime              (EEVDF 的 V)
       │    ├─ u64 min_vruntime              (单调基准)
       │    ├─ struct rb_root_cached tasks_timeline  (按 deadline 排的红黑树)
       │    │     └─ 挂着 struct sched_entity (augmented, 每节点带 min_vruntime)
       │    │           ├─ struct load_weight load   (权重)
       │    │           ├─ u64 vruntime / deadline / slice
       │    │           ├─ s64 vlag                (EEVDF 欠账)
       │    │           ├─ struct sched_avg avg     (PELT)
       │    │           └─ struct cfs_rq *my_q      (非 NULL=是组,下钻)
       │    └─ struct sched_entity *curr   (本队列当前在跑的 se)
       ├─ struct rt_rq rt            (实时子队列)
       │    ├─ struct rt_prio_array active
       │    │     ├─ DECLARE_BITMAP(bitmap, 101)   (位图,O(1) 选最高 prio)
       │    │     └─ struct list_head queue[100]   (100 条 prio 链表)
       │    ├─ u64 rt_time / rt_runtime            (throttle 配额)
       │    └─ int rt_throttled
       ├─ struct dl_rq dl            (deadline 子队列)
       │    ├─ struct rb_root_cached root          (按 deadline 排红黑树,EDF)
       │    └─ struct { u64 curr, next; } earliest_dl
       ├─ u64 clock / clock_task / clock_pelt      (三套时钟)
       ├─ struct sched_domain *sd                  (调度域链根, P4-14)
       ├─ struct root_domain *rd                   (根域, 跨核共享忙/闲位图)
       └─ struct hrtimer hrtick_timer              (hrtick 高精度定时器, P1-04)

 struct task_struct (include/linux/sched.h)
   ├─ int on_rq                  (0/QUEUED/MIGRATING)
   ├─ int prio / static_prio / normal_prio
   ├─ unsigned int rt_priority
   ├─ struct sched_entity se     (公平调度实体,内嵌)
   ├─ struct sched_rt_entity rt  (实时调度实体)
   ├─ struct sched_dl_entity dl  (deadline 调度实体)
   ├─ const struct sched_class *sched_class  (指向 stop/dl/rt/fair/idle 之一)
   └─ unsigned int policy        (SCHED_NORMAL/FIFO/RR/DEADLINE/BATCH/IDLE)

 struct sched_class (sched.h:2261, 用 linker section 排序)
   ├─ enqueue_task / dequeue_task / yield_task
   ├─ pick_next_task / put_prev_task / set_next_task
   ├─ task_tick / task_fork / task_dead
   ├─ select_task_rq / migrate_task_rq  (SMP)
   └─ update_curr / wakeup_preempt / ...

 五个 sched_class 实例(按优先级从高到低,for_each_class 遍历):
   stop_sched_class (stop_task.c) > dl_sched_class (deadline.c) >
   rt_sched_class (rt.c) > fair_sched_class (fair.c, EEVDF) >
   idle_sched_class (idle.c)
```

这张嵌套图把全书的核心数据结构——`rq` 套 `cfs_rq`/`rt_rq`/`dl_rq`、`cfs_rq` 套红黑树、树挂 `sched_entity`、`task_struct` 内嵌三个调度实体 + `sched_class` 指针、五个 `sched_class` 实例按优先级排序——一次性钉死。配合前面三张时序/状态/流程图,你应该能在脑子里放映出内核调度的全过程。

---

> 这个附录是"一张图看懂全书"的放映厅。如果你读完正文 20 章后想快速回忆"内核调度到底在干什么",回到这里看三张图 + 一张嵌套图就够了。想看每个步骤底下的源码细节和技巧精解,回到对应章节。
