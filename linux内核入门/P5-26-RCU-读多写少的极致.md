# 第 26 章 · RCU:读多写少的极致

> **前置**:你需要先读过第 24 章(并发来源、临界区)、第 25 章(spinlock/mutex,以及"读者也要排队"这个痛点),以及**内存篇第 7 章 · slab/slub**(尤其是 `SLAB_TYPESAFE_BY_RCU` 那一节——本章会直接回扣它)。

读完这一章你会明白:**RCU 不是"另一种锁",而是一种"用空间和延迟换读者极致性能"的设计**。它能做到让海量读者**完全不锁、几乎零开销**地读;代价全部转嫁给极少数的写者。你也会知道它什么时候是神兵利器、什么时候**绝对不能用**。

> **如果一读觉得太难**:先只记住四件事——① RCU 是**读多写少 + 数据靠指针整体替换**时的专用武器;② 读者用 `rcu_read_lock`/`rcu_dereference`,**不锁、几乎零开销**;③ 写者用 `rcu_assign_pointer` 换指针,再 `synchronize_rcu`/`call_rcu` **等一个宽限期**让旧读者退场,然后才回收旧数据;④ 它**不是锁**,不能让写者互斥——写者之间还得自己用 spinlock。其余细节第二遍配合源码再抠。

---

## 一、锁的原罪:读者凭什么要排队?

第 25 章讲了两把锁。但它们都有一个**共同的、无法回避的原罪**:

> **即使你只是"读"数据,你也得拿锁。**

设想一份**路由表**。全城几万个进程、上千个软中断,每秒都在**查**这张表;但只有极少数的配置变更才会**改**它(加一条路由、删一条路由,几分钟才一次)。

如果用 spinlock(哪怕是"读锁"):

- 每个读者来,先抢一次锁(一次原子操作,要碰锁所在的缓存行)。
- 几万个读者挤着碰同一个缓存行 → **缓存行 ping-pong**:这个缓存行在几十个核之间疯狂弹来弹去,总线被打爆。
- 可这份档案明明**只读不改**的时候,大家完全可以**一起看**,根本不需要互相挡——锁在这里纯粹是"防一个根本不会发生的冲突",白白付出巨大代价。

**不这样会怎样?** 性能塌方。这种"读极多、写极少"的数据结构(路由表、进程凭据 `struct cred`、文件系统 dentry、网络协议族的 `struct packet_type` 链表……)在内核里比比皆是。如果每条都用锁,内核跑不快。

所以内核需要一种**完全不同的武器**:让读者**根本不锁**,把代价全部甩给写者。这就是 **RCU(Read-Copy-Update)**。

---

## 二、RCU 的核心思想:读旧版,造新版,等退场

RCU 的设计哲学一句话:

> **读者读旧版;写者不直接改,而是复制一份、改好、把指针原子替换;替换之前已经开读的读者继续读旧版,新读者读新版;等所有旧读者都退场了,才销毁旧版。**

用"档案室"比喻讲一遍,你就一辈子忘不掉了:

1. 档案柜里放着一份档案的**当前版本**,读者通过一个**指针 P** 找到它(`P → 版本A`)。
2. 读者来查档案:**不需要拿钥匙、不需要登记,直接顺着 P 翻版本 A**。爱怎么看怎么看,想看多久看多久(只要不离开"读临界区")。
3. 写者要改:**不直接在版本 A 上动笔**(那样正在读的人会读到半新半旧)。而是:
   - 把版本 A **复印一份**,得到版本 B;
   - 在版本 B 上随便改(没人在看 B,怎么改都行,不用锁);
   - 改好之后,把指针 **P 原子地从版本 A 改指向版本 B**。
4. **关键时刻**:指针一换,这之后**新来的读者**顺着 P 读到的是版本 B。但**在换指针之前就已经开始读**的那些读者,手里还拿着版本 A——他们得**看完**。
5. 写者**耐心等**,等到"所有拿着版本 A 的旧读者全部退出了读临界区"(这个等待就叫一个**宽限期 grace period**)。
6. 宽限期一过,版本 A 再也不会有人读了,**安全销毁**。

> **为什么这么牛?** 看第 2 步:读者**完全不锁、不做原子操作、不碰任何会争用的缓存行**——它就是顺着指针读内存,代价和一个普通指针解引用一样。海量读者各自读各自的,**互不打扰**。读者付出的代价≈0,所有代价(复制、等宽限期)都甩给了本就极少的写者。这就是"读多写少"场景下最完美的分工。

> **代价是什么?** ① **内存占用**:宽限期内,旧版本和新版本同时存在,要多占一份。② **写者延迟**:写者要等一个宽限期(可能几毫秒)才能回收旧版,所以 RCU 的"改"不是即时生效的。③ **限制**:只能保护"指针指向的数据"(因为替换靠换指针);写者之间不能靠 RCU 互斥。所以 RCU **专治"读多写少 + 可整体替换"** 的场景。

---

## 三、读者:几乎零开销的读临界区

先看读者这一侧,体会"零开销"长什么样。

### 3.1 rcu_read_lock / rcu_read_unlock

读者的标准写法:

```c
rcu_read_lock();                  /* 进入读临界区 */
p = rcu_dereference(global_ptr);  /* 读那个被 RCU 保护的指针 */
do_something_with(p->field);      /* 顺着指针用数据 */
rcu_read_unlock();                /* 退出读临界区 */
```

`rcu_read_lock()` 到底干了啥?在非抢占内核里,它几乎是**空的**——出自 [include/linux/rcupdate.h](../linux-6.14/include/linux/rcupdate.h#L845-L852):

```c
static __always_inline void rcu_read_lock(void)
{
	__rcu_read_lock();
	__acquire(RCU);
	rcu_lock_acquire(&rcu_lock_map);
	RCU_LOCKDEP_WARN(!rcu_is_watching(),
			 "rcu_read_lock() used illegally while idle");
}
```

`__rcu_read_lock()` 在非抢占配置下基本是 no-op(抢占配置下它会增加当前任务的 `preempt_count`,防止自己被换下去——因为一旦换下去,这个任务可能很久不回来,宽限期就过不了)。剩下的 `__acquire(RCU)`、`rcu_lock_acquire` 都只是给 lockdep/sparse 做静态检查标注,**运行时没有任何原子指令、没有缓存行争用**。

> **对比一下锁**:哪怕最轻的 spinlock 读锁,也要做一次 `cmpxchg`(碰锁的缓存行)。RCU 读者连这个都省了。这就是"读者零开销"的真相。

### 3.2 rcu_dereference:为什么读指针也要包一层?

读者用 `rcu_dereference(global_ptr)` 而不是直接 `global_ptr`。为什么?

**不这样会怎样?** 因为**编译器和 CPU 会乱序**(第 27 章详讲)。设想你写:

```c
p = global_ptr;          /* (1) 读指针 */
x = p->field;            /* (2) 用指针 */
```

CPU 可能把(2)的读"提前"到(1)之前执行(它觉得(2)不依赖(1)?其实依赖,但弱序架构上指针读的可见性没保证),或者编译器把 `global_ptr` 缓存到寄存器、读到一个"半更新"的指针值。`rcu_dereference` 内部藏了一个 **acquire 屏障**,保证:(1) 真正读到指针的一个**一致的快照**;(2) 用这个指针访问的字段,确实来自你读到的那个版本。

> **这条规矩的精炼版**:RCU 保护的数据,**读者必须用 `rcu_dereference` 读指针,写者必须用 `rcu_assign_pointer` 写指针**。这俩是"配对"的——它们各自藏了内存屏障(第 27 章),保证读者看到的版本是自洽的。**直接用裸指针读写,在弱序 CPU 上就是 bug**,只是 x86(强序)上碰巧没发作。

---

## 四、写者:复制、替换、等退场

写者三步:`rcu_assign_pointer`(换指针)→ `synchronize_rcu` 或 `call_rcu`(等宽限期)→ 释放旧版。

### 4.1 rcu_assign_pointer:藏了 release 屏障的"换指针"

出自 [include/linux/rcupdate.h](../linux-6.14/include/linux/rcupdate.h#L594-L603):

```c
#define rcu_assign_pointer(p, v)					      \
do {									      \
	uintptr_t _r_a_p__v = (uintptr_t)(v);				      \
	rcu_check_sparse(p, __rcu);					      \
									      \
	if (__builtin_constant_p(v) && (_r_a_p__v) == (uintptr_t)NULL)	      \
		WRITE_ONCE((p), (typeof(p))(_r_a_p__v));		      \
	else								      \
		smp_store_release(&p, RCU_INITIALIZER((typeof(p))_r_a_p__v)); \
} while (0)
```

逐行:

- 一般情况,它就是 `smp_store_release(&p, v)`——一个带 **release 屏障**的指针写。
- **为什么用 release 而不是普通写?** 因为写者刚在"版本 B"上改了一堆字段,**必须保证"这些字段的写"全部先于"指针的写"被别的核看见**。release 屏障(第 27 章)正是干这个的:它强制屏障之前的所有写,在本次写之前完成并可见。这样,任何读者一旦通过新指针读到版本 B,版本 B 的所有字段就一定是齐整的——绝不会读到"指针是新的、字段还是旧的"半成品。

> 这是 RCU 正确性的关键一环,也是它和第 27 章内存屏障的交汇点。`rcu_assign_pointer` = `WRITE_ONCE(指针, 新版)` + 一道 release 屏障;`rcu_dereference` = `READ_ONCE(指针)` + 一道 acquire 屏障。**RCU 的"无锁正确性",完全建立在这对 acquire/release 屏障上。**

### 4.2 synchronize_rcu / call_rcu:等一个宽限期

指针换好之后,**旧版本还有人在读**,不能立刻释放。怎么"等所有旧读者退场"?

RCU 提供两种等法:

**(A) 同步等:`synchronize_rcu()`**——阻塞当前执行流,直到一个宽限期过去(所有旧读者退场)。出自 [kernel/rcu/tree.c](../linux-6.14/kernel/rcu/tree.c#L3270-L3284):

```c
void synchronize_rcu(void)
{
	...
	RCU_LOCKDEP_WARN(lock_is_held(&rcu_lock_map) || ...,
			 "Illegal synchronize_rcu() in RCU read-side critical section");
	if (!rcu_blocking_is_gp()) {
		if (rcu_gp_is_expedited())
			synchronize_rcu_expedited();   /* 加速版,用 IPI 逼各核表态 */
		else
			synchronize_rcu_normal();      /* 普通版,睡觉等 */
		return;
	}
	...
}
```

注意第一行的 `RCU_LOCKDEP_WARN`:**你自己不能在持有 RCU 读锁的情况下调 `synchronize_rcu`**——因为那样你就是一个"永远不会退场的旧读者",宽限期永远过不了,自己把自己死锁。lockdep 会抓这种 bug。

`rcu_blocking_is_gp()` 的注释点出一个有趣的边界:在**单核 + 非抢占**的早期启动阶段,根本没有"并发读者"可言,此时一个普通的"空操作"本身就是一个宽限期(见 [kernel/rcu/tree.c](../linux-6.14/kernel/rcu/tree.c#L3212-L3224) 的注释)。这是 RCU 在极端简化配置下的优雅退化。

**(B) 异步等:`call_rcu(old, callback)`**——不阻塞,注册一个回调;宽限期过去后,内核自动调你的回调(通常回调里就是 `kfree(old)`)。出自 [kernel/rcu/tree.c](../linux-6.14/kernel/rcu/tree.c#L3170-L3172):

```c
void call_rcu(struct rcu_head *head, rcu_callback_t func)
{
	__call_rcu_common(head, func, enable_rcu_lazy);
}
```

> **怎么选?** `synchronize_rcu` 简单直观,但会**阻塞**(它其实在等一个可能几毫秒的宽限期),适合"我不着急、写很少"的场景。`call_rcu` 不阻塞,适合"我在中断/不能睡太久"或"想批量回收"的场景。代价是 `call_rcu` 要自己管回调,容易写错(回调里又分配又回收,顺序复杂)。内核里两者都常见。

### 4.3 "宽限期"到底怎么判定?

这是 RCU 最神奇的地方,也是读者能"零开销"的代价所在。

内核怎么知道"所有旧读者都退场了"?思路很巧:

- 内核给每个 CPU 核**记录它经历了几次"静止状态(quiescent state)"**。所谓静止状态,就是这个核**此刻不在任何 RCU 读临界区里**(比如它经历了上下文切换、或者跑到了用户态、或者 CPU 空闲)。
- 一个核一旦经过静止状态,它**在本次宽限期开始之前**持有的所有 RCU 读临界区,就一定已经结束了(因为静止状态意味着它"放下了手里的活")。
- 内核靠一个专门的内核线程 **rcu_gp_kthread**(以及每核的报告)来收集各核的静止状态。当**所有核**都报告了静止状态,这个宽限期就结束了——这意味着:宽限期开始时正在读的所有读者,都已退出。
- 然后内核就可以安全地回收旧版了(对 `call_rcu` 来说,就是触发那些排队的回调)。

宽限期的推进代码在 `rcu_gp_init` 等函数里([kernel/rcu/tree.c](../linux-6.14/kernel/rcu/tree.c#L1775) 附近),实现相当复杂(要处理嵌套、抢占、加速宽限期、多个宽限期并发等),但**对你这个使用者来说,只需要知道一件事:`synchronize_rcu` 返回时(或 `call_rcu` 回调被调时),所有在你换指针之前就开始读的读者,保证已经退场了**。这是 RCU 给你的契约。

> **这就是为什么 RCU 读者不能睡太久(在抢占内核里)`:rcu_read_lock` 会关抢占**,确保读者不会在临界区里被换下去睡大觉——否则这个核迟迟不经过静止状态,整个宽限期被卡住,所有写者都在等,系统性能崩。RCU 读临界区同样要**短**。

---

## 五、回扣内存篇:SLAB_TYPESAFE_BY_RCU

内存篇第 7 章 · slab/slub 提到一个标志 `SLAB_TYPESAFE_BY_RCU`,说它是"为 RCU 量身定做"。现在你能完全看懂它了。

### 5.1 问题:RCU 回收的对象,正好是 slab 对象

很多 RCU 保护的对象(task_struct 相关的、文件系统的 dentry 等)是用 slab 分配的。写者换指针后,要等宽限期再 `kmem_cache_free(old)` 回收。这本来没问题——宽限期保证了没人再持有 old。

但有个性能优化:**能不能在宽限期还没过的时候,就把这个 slab 对象还回 slab 池子复用?** 这样 slab 不用等宽限期就能高效复用内存。

### 5.2 不这样会怎样?

如果直接还回 slab 池子,**宽限期还没过**,可能还有读者正拿着这个内存地址在用!此刻 slab 把这块内存**重新分配给另一个对象**(比如一个新的 dentry),读者读到的就是别人的数据——**内存腐化**。

### 5.3 所以:SLAB_TYPESAFE_BY_RCU + 重新验证

`SLAB_TYPESAFE_BY_RCU` 解决了"提前还池"和"读者安全"的矛盾。它的语义,直接引自 [include/linux/slab.h](../linux-6.14/include/linux/slab.h#L101-L120):

> **This delays freeing the SLAB page by a grace period, it does _NOT_ delay object freeing.** This means that if you do `kmem_cache_free()` that memory location is free to be reused at any time. Thus it may be possible to see another object there in the same RCU grace period.
>
> **This feature only ensures the memory location backing the object stays valid**, the trick to using this is relying on an independent object validation pass.

翻译成人话:它保证的是"**这块内存所在的页**,在宽限期内不会被归还给伙伴系统(地址始终可访问)";但**对象本身可以立刻被 slab 复用**——也就是说,读者顺着指针读到的那块地址,**可能已经变成了另一种对象**!

那读者怎么用?**必须有一套独立的"重新验证"流程**:读到对象后,先尝试"拿引用计数"(try_get_ref),如果失败(说明已被释放/复用)就重读;拿到后还要**再核对一次"这还是我要的那个对象吗"**。官方给的范式([include/linux/slab.h](../linux-6.14/include/linux/slab.h#L107-L120))就是这种 retry:

```c
begin:
	rcu_read_lock();
	obj = lockless_lookup(key);
	if (obj) {
		if (!try_get_ref(obj)) {     /* 可能失败:对象已被释放 */
			rcu_read_unlock();
			goto begin;
		}
		if (obj->key != key) {       /* 再核对:这还是我要的对象吗? */
			put_ref(obj);
			rcu_read_unlock();
			goto begin;
		}
	}
	rcu_read_unlock();
```

> **为什么值得这么麻烦?** 因为它让 slab 的高效复用不被 RCU 的宽限期拖慢——回收内存时不必等宽限期,只有"把整个 slab 页还给伙伴系统"才等。对高频分配回收的对象(比如 dentry),这个优化显著。代价是使用者必须老老实实写"重新验证"——而这块代码也是最容易写错的地方,所以 slab.h 的注释开头就亮出 **WARNING READ THIS!**。

> **回扣内存篇**:这正是内存篇第 7 章埋下的钩子。`SLAB_TYPESAFE_BY_RCU` 把"slab 池化"和"RCU 宽限期回收"两个机制缝在一起,是**内存子系统与并发子系统深度耦合**的典型例子——单看 slab 或单看 RCU 都不能理解它,必须两边一起看。

---

## 六、RCU 不是锁:它不能让写者互斥

一个极易踩的坑:**RCU 不是锁,它不提供写者之间的互斥。**

设想两个写者同时改同一份 RCU 数据:

```
写者 A: 复制版本A → 版本B,改 B,准备换指针
写者 X: 复制版本A → 版本C,改 C,准备换指针
两人都 rcu_assign_pointer(global_ptr, 自己的版本)
```

如果 A 和 X 各自基于"版本A"复制,那**后换指针的那个会覆盖前一个的改动**——X 的版本 C 里没有 A 对 B 的修改,A 白干了。这就是**丢失更新**。

> **所以**:RCU 只解决"**读者 vs 写者**"的并发(让读者不被写者打扰)。**写者 vs 写者**之间的互斥,**还得用 spinlock/mutex**。标准范式是:

```c
spin_lock(&writers_lock);             /* 写者之间互斥 */
new = kmalloc(...);
*new = *old;                            /* 复制 */
modify(new);                            /* 改新版 */
old = rcu_dereference_protected(global_ptr, lockdep_is_held(&writers_lock));
rcu_assign_pointer(global_ptr, new);   /* 原子换指针 */
spin_unlock(&writers_lock);
synchronize_rcu();                      /* 等旧读者退场 */
kfree(old);                             /* 回收旧版 */
```

> **记住**:RCU 是"**读者免费、写者重**"的安排;它**取代的是读锁**,不是写锁。写者那把 spinlock/mutex,**该有还得有**。

---

## 七、什么时候该用 RCU,什么时候绝对别用

### 7.1 适合 RCU 的场景

- **读极多、写极少**(路由表、协议注册表、进程凭据、dentry……)。
- 数据**靠指针整体替换**(改 = 换指针,而不是就地改字段)。
- 你能接受**写者延迟**(等一个宽限期)和**短期多占一份内存**。
- 数据结构的"读"可以容忍"**读到稍旧的版本**"(很多统计/路由场景,旧一点点根本无所谓)。

### 7.2 绝对别用 RCU 的场景

- **写很频繁**——每次写都要复制+等宽限期,写多时灾难。
- 数据**不能靠换指针替换**(比如就是一个计数器,RCU 帮不上,该用原子操作)。
- **必须读到最新值、不能容忍任何延迟**(RCU 读者可能读到旧版)。
- 想用 RCU **替代写者之间的互斥**——它做不到(见上节)。
- 读者要在临界区里**睡很久**(抢占内核里 RCU 读临界区会关抢占,睡太久会卡住宽限期)。

> **一句话总结**:RCU 是"读多写少 + 整体替换 + 容忍旧值"这三种条件**同时成立**时的最优解。少一个,就老老实实用锁/原子操作。

---

## 关键源码精读:rcu_assign_pointer 为什么必须用 release

本章最值得逐行看的,是 `rcu_assign_pointer` 里那一句 `smp_store_release`——它把 RCU 的正确性、内存屏障、读者写者的时序关系全压缩进去了。出自 [include/linux/rcupdate.h](../linux-6.14/include/linux/rcupdate.h#L594-L603):

```c
#define rcu_assign_pointer(p, v)					      \
do {									      \
	uintptr_t _r_a_p__v = (uintptr_t)(v);				      \
	rcu_check_sparse(p, __rcu);					      \
									      \
	if (__builtin_constant_p(v) && (_r_a_p__v) == (uintptr_t)NULL)	      \
		WRITE_ONCE((p), (typeof(p))(_r_a_p__v));		      \
	else								      \
		smp_store_release(&p, RCU_INITIALIZER((typeof(p))_r_a_p__v)); \
} while (0)
```

逐段对应设计:

1. **`rcu_check_sparse(p, __rcu)`**:静态检查(由 sparse 工具做)。被 RCU 保护的指针必须声明成 `__rcu` 类型限定符。这一步在编译期帮你抓"忘了标 `__rcu` 的指针被误用",是 RCU 的静态防线之一。
2. **特判 NULL**:赋 NULL 时用 `WRITE_ONCE`(裸的、可见性保证的写)即可——因为"指针变成 NULL"不涉及"新版本字段要先就绪"的问题(没有新版本)。这是个小优化。
3. **一般情况 `smp_store_release(&p, v)`**:**这是 RCU 正确性的命门**。它的语义(第 27 章详讲)是:"**本次写之前,程序顺序里所有更早的写,都必须在本次写之前、对别的核可见**"。

   为什么这个保证不可或缺?写者的完整动作是:`改新版字段` → `换指针`。如果没有 release 屏障,CPU 可能把它重排成"先换指针、后写新版的某个字段"(弱序架构上完全可能)。那么,一个在换指针之后到来的读者,顺着新指针读到新版——却发现某个字段**还没写进去**!读到半成品。release 屏障强制"改字段"全部排在"换指针"之前完成并可见,杜绝这个灾难。

> **配合读者侧**:读者用 `rcu_dereference(p)`,它内部是 `smp_load_acquire(p)`——**acquire 屏障**保证"读指针之后的所有读,都在读指针之后才进行",确保读者顺着新指针读到的字段,确实是和这个新指针配套的版本。**`rcu_assign_pointer`(release)和 `rcu_dereference`(acquire)是天生一对**,缺了任何一个,RCU 在弱序 CPU 上都会读到半新半旧。这就是为什么第 27 章内存屏障是 RCU 的隐形地基——下一章就把这层地基挖开给你看。

---

## 章末小结

用"档案室"比喻收一下:

- **RCU = 档案室把旧档案复印若干份,读者随便拿一份看(不锁);改档案的人另起一份新稿、改好、把柜子里的"当前版本"指针整体换掉;等所有拿着旧复印件的读者都离开了,才把旧复印件销毁。**
- 读者**不锁、几乎零开销**;代价(复制 + 等宽限期)全甩给写者。
- 三件套:**`rcu_read_lock`/`rcu_dereference`(读)、`rcu_assign_pointer`(换指针)、`synchronize_rcu`/`call_rcu`(等宽限期回收)**。其中 `rcu_assign_pointer` 和 `rcu_dereference` 各藏一道屏障,是 RCU 正确性的隐形地基。
- RCU **不是锁**——它只解决"读者 vs 写者",**写者之间还得 spinlock/mutex**。它**取代读锁**,不取代写锁。
- 回扣内存篇:`SLAB_TYPESAFE_BY_RCU` 是 slab 池化与 RCU 回收的耦合,让 slab 高效复用而不必等宽限期——代价是使用者必须写"重新验证"。

**回扣全书主线**:RCU 属于"管资源"那一侧,但它管的不是"谁能用资源",而是"**读者如何零代价地与写者共存**"。它是 Linux 内核**最具特色、也最巧妙**的同步原语——很多其他操作系统没有的。理解它,你就理解了内核为了"读多写少"这个极常见的负载模式,能下多大功夫。

**想继续深入该往哪钻**:
- 宽限期的完整机制:[kernel/rcu/tree.c](../linux-6.14/kernel/rcu/tree.c) 的 `rcu_gp_init`、`rcu_gp_cleanup`、`rcu_gp_kthread`——看内核怎么一帧一帧推进宽限期、收集各核静止状态。
- RCU 的诸多变体:Tree RCU / Tiny RCU、`rcu_read_lock_bh` / `rcu_read_lock_sched`(不同"读临界区"的定义)。
- **rcutorture**:内核自带的 RCU 折磨测试([kernel/rcu/rcutorture.c](../linux-6.14/kernel/rcu/rcutorture.c))——它在各种极端配置下狂测 RCU 的正确性,是 RCU 能让人放心的根本原因。
- 经典讲解:内核文档 [Documentation/RCU/](../linux-6.14/Documentation/RCU/) 目录(Paul McKenney——RCU 的主要作者——写的系列文章)。

> 下一章,我们把贯穿前两章的"屏障"挖到底。**为什么光有锁和 RCU 还不够,编译器和 CPU 还会"乱序"?内存屏障到底挡住了什么?**
