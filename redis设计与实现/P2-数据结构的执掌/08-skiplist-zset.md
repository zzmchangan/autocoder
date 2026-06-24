# 第八章 · skiplist:为什么 zset 不用红黑树

> 篇:P2 数据结构的执掌
> 主轴呼应:这一章是**取向④(简单优先)的招牌**——用一个概率平衡的 skiplist,换来比红黑树更简单的实现、更顺滑的范围操作、更紧凑的内存。同时它也是**取向②(内存即数据库)** 的体现:zset 为了让每种操作都打到最擅长的结构,愿意在两个结构里各存一份数据。

---

## 读完本章你会明白

1. **为什么 zset 要同时维护一个 dict 和一个 skiplist,看起来"存了两份"**——因为有序集合要同时满足三类操作,没有任何单一结构能都做好;而"两份"其实只多花了一份指针,字符串本身只有一份。
2. **skiplist 凭什么不用旋转就能保持 O(log N)**——靠的是插入时给新节点掷骰子定层高,用概率平衡代替严格平衡。
3. **那个让无数人卡住的 span 更新公式 `span = update[i].span - (rank[0]-rank[i])` 到底什么意思**——这一章会用几何图把它一行一行推出来,而不是贴了代码就跳过。
4. **删除一个节点时,它的 span 是怎么传给前驱的**——和插入完全对称,span 是节点对前驱"可借贷的资产"。
5. **为什么 antirez 选 skiplist 而不是红黑树**——不是为了在某个维度上赢,而是在"够用"的前提下,把实现简单度和范围操作的顺滑度同时做到最优。

---

> **如果一读觉得太难:先只记住三件事**——
> ① zset = 一个 dict(member→score,O(1) 单点查)+ 一个 skiplist(score 有序,支撑范围/排名),两边共享同一份 member 字符串;
> ② skiplist 是个"多层、概率变稀疏"的链表,查找从最高层粗跳、逐层精修,期望 O(log N),没有任何旋转;
> ③ 每个节点的 forward 指针顺手记一个 `span`(跨过多少底层节点),`ZRANK` 靠累加 span 算排名,O(log N)。
> 这三件事,就是 zset 的全部。

---

> **一句话点破:zset 选 skiplist 不是选了一个"更快"的结构,而是选了一个"更简单、范围扫描更顺滑、且没有显著性能劣势"的结构——它用概率平衡换掉了红黑树的旋转,用底层链表换掉了中序遍历,用空间换掉了单点查找的 O(log N)。**

## 8.1 这块要解决什么:有序集合的三类操作,没有一个结构能独力满足

排行榜,是 zset(有序集合)的招牌场景:你往里塞 `(score, member)`,要按分数从高到低拿前 100 名,要能随时查"某个 member 排第几",还要能"取出分数在 `[100, 200)` 之间的所有 member"。把这些需求拆开,zset 必须同时高效地支持**三类完全不同的操作**:

1. **按 member 单点查 score / 增删**:`ZSCORE`、`ZADD`、`ZREM`。这是按"键"(member)的操作,期望 O(1) 最理想——这正是哈希表的强项。
2. **按 score 全序遍历**:`ZRANGE`、`ZRANGEBYSCORE`。这要求元素按分数排好队,底层最好是一条**顺序链表**,顺着走即可。
3. **按位置(排名)取**:`ZRANK`、`ZRANGE start stop`。这要求结构能快速算出"第 k 个是谁",或"某 member 是第几个"。

> **不这样会怎样**:这三类需求没有任何一个数据结构能独自漂亮地满足。哈希表能做 1(O(1) 单点),但天生无序,2 和 3 全废;平衡树(红黑树)能做 2 和 3(O(log N)),但单点查找是 O(log N) 不是 O(1),而且范围遍历要中序递归,不轻快;纯有序链表能做 2,但查找是 O(N),1 和 3 都不行。**单一结构在这三类操作之间必然顾此失彼。**

Redis 的答案是把两件事拆给两个结构,各干各的强项。看 zset 的类型定义([server.h:1504](../../redis-8.0.2/src/server.h#L1504)):

```c
/* server.h:1504-1507 */
typedef struct zset {
    dict *dict;     // 结构 A:member → score,O(1) 单点查
    zskiplist *zsl; // 结构 B:按 score 有序,支撑范围/排名
} zset;
```

当 zset 从 listpack 编码升级为 skiplist 编码时,会一次性建这两份结构([t_zset.c:1272](../../redis-8.0.2/src/t_zset.c#L1272)):

```c
/* t_zset.c:1272-1274 */
zs = zmalloc(sizeof(*zs));
zs->dict = dictCreate(&zsetDictType);   // member → score
zs->zsl = zslCreate();                   // score 有序
```

`ZSCORE` 走 dict,O(1);`ZRANGE` 走 zsl 的底层链表,顺指针扫;`ZRANK` 走 zsl 带 span 的多层链表,O(log N)。每个操作都打到最擅长的那个结构上。至于"两份"是不是浪费内存——8.7 节会看到,member 字符串其实**只存一份**,两个结构共享同一个指针,所谓"双存"只是多了一份指针和一份 score。

那么"按 score 有序"这个角色,为什么选 skiplist 而不是教科书标配的红黑树?这才是本章真正要回答的问题。

## 8.2 几何直觉:多层快车道

先忘掉公式,画个画面。假设你有一条高速公路,从北京到广州沿途有 N 个出口。如果你只有一条国道(单层链表),从北京到某出口必须一站一站开,最坏开 N 站,O(N)。

现在加一条**快车道**:它只停靠少数几个大站(比如每隔几个普通站设一个大站)。你要去某个出口,先上快车道狂奔,过了目标就下到国道慢慢找。因为大站稀疏,平均每走一站能"跨过"好几个普通站,查找成本降下来了。

再加一层**超快车道**,只停更少的大站,你在它上面能跨过更多站。再加一层……如果每往上一层,站与站的间距大致翻倍,那么从最高层(最稀疏)往下走:最高层跳几大步逼近目标区域 → 下一层精修几步 → …… → 最底层国道精准定位。每层大约只走常数步,总层数是 O(log N),所以查找是 **O(log N)**。

这就是 skiplist 的几何本质:**一个多层的、概率性变稀疏的链表**。

```text
level 3 (最稀疏): header ────────────────────────────────→ E ────→ nil
level 2:          header ────────→ C ────────────→ E ────→ nil
level 1:          header ──→ B ──→ C ──→ D ──────→ E ────→ nil
level 0 (国道):   header → A → B → C → D → E → F → nil
                  (每个 forward 指针上的数字 = span,跨过的底层节点数)
```

举个查找的例子,看这个"多层跨步"怎么走。假设要在上表里找 score 等于 D 的节点:

- 从 **level 3** 出发:header 的 forward 直接指向 E。E 的 score > D,这一步跨太大了,停住不下脚,下降到 level 2。
- **level 2**:header 的 forward 指向 C。C 的 score < D,可以走,前进到 C。C 的 forward 指向 E,E 的 score > D,停住,下降到 level 1。
- **level 1**:C 的 forward 指向 D。D 的 score == D,命中。

总共走了"0 大步 + 1 中步 + 1 小步",定位到目标。换成底层链表要从 header 走 4 步。表越大,这个"高层粗跳、低层精修"的省步效果越明显——每层最多走常数步(因为每层节点数是下一层的 1/4),层数是 O(log N),总步数 O(log N)。注意整个查找**只进不退**——每一层单向链表,走的方向天然朝着目标,从不回溯。

> **钉死这件事**:skiplist 的查找是"从最稀疏的顶层往最密的底层逐层逼近":顶层跨大步快速锁定区间,每下降一层精修几步。它没有任何回溯(不像红黑树中序遍历要回父节点)。这种"只进不退"的几何特性,正是 skiplist 范围扫描比红黑树顺滑的根子——范围扫描就是从起点沿底层链表一路向前。

它和红黑树一样能把查找压到 O(log N),但它**没有任何"旋转"或"重平衡"**——平衡不是靠插入时的复杂调整维护的,而是靠**插入时给新节点随机掷骰子决定它出现在哪几层**。这是 skiplist 和红黑树最根本的分野。

## 8.3 节点结构与两个常量

来看 Redis 里 skiplist 节点长什么样([server.h:1488](../../redis-8.0.2/src/server.h#L1488)):

```c
/* server.h:1488-1496 */
typedef struct zskiplistNode {
    sds ele;                              /* member 字符串               */
    double score;                         /* 分数,排序依据              */
    struct zskiplistNode *backward;       /* 后向指针(便于从尾往前遍历)*/
    struct zskiplistLevel {
        struct zskiplistNode *forward;    /* 该层的下一个节点           */
        unsigned long span;               /* 该层 forward 跨过的底层节点数 */
    } level[];                            /* 柔性数组,层数每个节点不同 */
} zskiplistNode;
```

两个要立刻注意的点:

第一,**`level[]` 是柔性数组**——每个节点的层数可以不一样。一个 3 层的节点,就是"它在第 0/1/2 层都有 forward 指针";一个 1 层的节点,只活在最底层国道。创建节点时按实际层数分配(`zslCreateNode`,[t_zset.c:60](../../redis-8.0.2/src/t_zset.c#L60)):

```c
/* t_zset.c:60-66 */
zskiplistNode *zslCreateNode(int level, double score, sds ele) {
    zskiplistNode *zn =
        zmalloc(sizeof(*zn)+level*sizeof(struct zskiplistLevel));
    zn->score = score;
    zn->ele = ele;
    return zn;
}
```

`sizeof(*zn) + level*sizeof(struct zskiplistLevel)` —— 只为自己那几层分配 level 域,不是固定分配 `MAXLEVEL=32` 层。算一笔账:每个 `zskiplistLevel` 是 forward 指针(8 字节)+ span(8 字节)= 16 字节。如果所有节点都固定分 32 层,每个节点光 level 域就要 32×16 = 512 字节;一个装 100 万元素的 zset,仅 level 域就吃掉 512 MB,还没算 ele 和 score。而按柔性数组 + p=1/4 的概率分布(下一节会算出平均 1.33 层),平均每节点 1.33×16 ≈ 21 字节,100 万元素只要约 21 MB——**省了 96%**。绝大多数矮节点不必为少数高节点的 32 层买单。

> **钉死这件事**:柔性数组 + 概率层高,让 skiplist 的内存占用正比于"平均层高 1.33"而非"最大层高 32"。这是 Redis 敢用 skiplist 存百万级 zset 的前提——若每个节点固定 32 层,内存开销会让它根本不可行。

第二,每个 forward 指针旁挂着一个 `span`。这是 skiplist 最精巧的设计之一,也是 8.5 节的主角。先记住它的定义:**`span` = 这个 forward 指针跨过的底层(level 0)节点数**。底层相邻节点的 span 是 1;高层一次跨一大步的 span 可能是 8、16。

跳表的几何参数由两个宏决定([server.h:576](../../redis-8.0.2/src/server.h#L576)):

```c
/* server.h:576-577 */
#define ZSKIPLIST_MAXLEVEL 32 /* Should be enough for 2^64 elements */
#define ZSKIPLIST_P 0.25      /* Skiplist P = 1/4 */
```

- `ZSKIPLIST_P = 0.25`:一个节点在已有第 i 层的基础上,**以概率 1/4 再往上长一层**。注释直白 `Skiplist P = 1/4`。
- `ZSKIPLIST_MAXLEVEL = 32`:最高 32 层。注释 `Should be enough for 2^64 elements` 解释了为什么是 32——下一节算给你看。

## 8.4 概率平衡:zslRandomLevel 与期望层数

新节点掷骰子的代码是 `zslRandomLevel`([t_zset.c:111](../../redis-8.0.2/src/t_zset.c#L111)):

```c
/* t_zset.c:111-117 */
int zslRandomLevel(void) {
    static const int threshold = ZSKIPLIST_P*RAND_MAX;  /* 0.25 * RAND_MAX */
    int level = 1;
    while (random() < threshold)   /* 每次以 1/4 概率"再长一层" */
        level += 1;
    return (level<ZSKIPLIST_MAXLEVEL) ? level : ZSKIPLIST_MAXLEVEL;
}
```

这段代码的几何含义:新节点**至少是 1 层**(最底层国道一定在)。然后不断掷骰子,掷中(`< threshold`,概率 1/4)就往上升一层,掷不中就停,上限 32 层。

这里要算清一个常被讲错的数:**一个节点的期望层数是多少?**

设 p = 1/4。节点层数 ≥ 1 恒成立(概率 1)。层数 ≥ 2 的概率 = p(第一次掷中)。层数 ≥ k 的概率 = p^(k-1)。于是期望层数:

```text
E[层数] = Σ (k≥1) P(层数 ≥ k)
        = 1 + p + p² + p³ + …
        = 1 / (1 - p)
        = 1 / (1 - 0.25)
        = 1 / 0.75
        = 4/3
        ≈ 1.33 层
```

**所以一个节点平均只有 4/3 ≈ 1.33 层。** 绝大多数节点只有 1~2 层,极少数长得高。这是一个无穷等比级数求和(p<1 收敛),结果是 1/(1-p)=4/3。

> **钉死这件事**:期望层数 = 1+p+p²+… = 1/(1-p) = 4/3 ≈ 1.33。注意 4/3 和 1.33 是同一个数的两种写法——4/3 就是 1.333…。Redis 选 p=1/4 而不是 Pugh 论文原始的 p=1/2(那样期望层数是 2),是为了让每个节点平均只占 1.33 个 level 域而不是 2 个,**用稍高一点的查找常数,换更省的内存**。这是空间-时间的一个 sweet spot。

那么 `MAXLEVEL = 32` 够不够?第 k 层期望有 `N · p^(k-1)` 个节点。要让第 32 层还"非空"(期望至少 1 个节点),需要 `N · p^31 ≥ 1`,即 `N ≥ (1/p)^31 = 4^31 ≈ 2^62`。而 Redis 单个 zset 最多 `2^64` 元素,32 层绰绰有余。注释 `Should be enough for 2^64 elements` 说的就是这个。

> **不这样会怎样**:为什么是概率平衡,而不是红黑树那种严格平衡?很多人以为 skiplist 查找是 O(log N) **平均**、红黑树是 O(log N) **最坏**,所以红黑树"更安全"。这话对,但 Redis 在乎的不是那个最坏差距。skiplist 的"最坏"(某次查找走了很多步)概率随 N 指数下降,工程上可忽略;而红黑树为了那个最坏保证,每次插入删除都要花力气做旋转——**旋转本身才是真正的复杂度和 bug 温床**。Redis 是单线程(取向①),最怕的就是实现复杂导致的不易察觉的 bug,而不是理论上的最坏高度。

## 8.5 技巧精解①:span 字段与排名——zslInsert 的 span 公式几何推导

这一节是整章最硬、也最值得讲透的部分。`ZRANK`(给 member 问排名)是 zset 的高频操作。如果只在底层链表数,是 O(N);skiplist 的妙招是**每个 forward 指针顺手记下"它跨过了多少底层节点"**,即 `span`。找排名时从高层往低层走,边走边把经过的 span **累加**,就是排名。

但 `span` 真正难的不是查找时累加,而是**插入/删除时怎么维护**。看 `zslInsert`([t_zset.c:122](../../redis-8.0.2/src/t_zset.c#L122))里这两行让无数人卡住的代码:

```c
/* t_zset.c:161-162 */
x->level[i].span = update[i]->level[i].span - (rank[0] - rank[i]);
update[i]->level[i].span = (rank[0] - rank[i]) + 1;
```

`update[i]` 和 `rank[i]` 是什么?在插入前的下降搜索里([t_zset.c:129-141](../../redis-8.0.2/src/t_zset.c#L129)),代码从最高层往底层走,每一层记录两个东西:

- `update[i]`:新节点在第 i 层的**前驱**(新节点将插在它后面)。
- `rank[i]`:从 header 到 `update[i]`,沿底层累加的节点数,也就是 `update[i]` 的排名。

注意 `rank[i]` 的继承技巧([t_zset.c:131](../../redis-8.0.2/src/t_zset.c#L131)):`rank[i] = (i==最高层) ? 0 : rank[i+1]`。下一层从上一层已累加的 rank 接着算,不必从头。所以 `rank[i]` 始终是"header 到 update[i] 的底层距离"。

现在推导 span 公式。设插入的新节点叫 x,它在 level i 的前驱是 update[i]。先把几个"排名"理清:

- `update[i]` 的排名 = `rank[i]`(header 排名 0,沿底层数到 update[i])。
- x 紧跟在 `update[0]` 后面(x 的紧邻底层前驱是 update[0]),所以 **x 的排名 = `rank[0] + 1`**。
- `rank[0] - rank[i]` = update[i] 到 update[0] 之间隔了多少底层节点 = **update[i] 到 x 之间隔的底层节点数**。

插入前,在 level i 上,`update[i]` 的 forward 指向它的原后继,原 span = `update[i].level[i].span`(原值)。插入 x 后,这一段被 x 切成两半:

```text
插入前 (level i):  update[i] ──────────span(原)──────────→ 原后继
插入后 (level i):  update[i] ──新span──→ x ──x.span──→ 原后继
```

**前半段:新的 `update[i].level[i].span`(update[i] → x)。** 它跨过的底层节点数 = x 排名 − update[i] 排名:

```text
新 update[i].span = (rank[0]+1) - rank[i] = (rank[0] - rank[i]) + 1   …… 对应 @162
```

**后半段:新的 `x.level[i].span`(x → 原后继)。** 插入 x 后,原后继及它后面的所有节点排名都 +1(因为 x 插在它们前面)。所以原后继的新排名 = (原排名) + 1 = (`rank[i]` + 原 span) + 1。于是:

```text
x.span = 原后继新排名 - x排名
       = (rank[i] + 原 update[i].span + 1) - (rank[0] + 1)
       = 原 update[i].span - (rank[0] - rank[i])              …… 对应 @161
```

两行公式,一行不差地对应 [t_zset.c:161-162](../../redis-8.0.2/src/t_zset.c#L161)。几何含义一句话:**新节点 x 把前驱原本的一条长跨距,从自己脚下切成两段——前一段归前驱(`(rank[0]-rank[i])+1`),后一段归自己(原 span 减掉前一段的长度)**。

> **钉死这件事**:span 公式的几何本质是"插入点把一条长跨距一分为二"。`rank[0]-rank[i]` 是前驱到插入点的底层距离,它决定了切分点。`update[i].span = (rank[0]-rank[i])+1` 是前半段(前驱到 x,含 x 故 +1),`x.span = 原 span - (rank[0]-rank[i])` 是后半段(x 到原后继)。这套维护让 span 始终精确反映"每个 forward 指针跨过的底层节点数",`ZRANK` 才能靠累加 span 算出 O(log N) 排名。

还有一个细节:如果新节点的层高超过了 skiplist 当前最高层([t_zset.c:147-154](../../redis-8.0.2/src/t_zset.c#L147)),那些更高层的"前驱"就是 header 自己,且 header 在这些层的 span 直接设成 `zsl->length`(跨过整个表到底):

```c
/* t_zset.c:148-152 */
for (i = zsl->level; i < level; i++) {
    rank[i] = 0;
    update[i] = zsl->header;
    update[i]->level[i].span = zsl->length;   /* header 直接跨到尾 */
}
```

为什么是 `zsl->length`?因为 header 在这些新建的高层上,forward 暂时指向 NULL(表尾),span 应该 = 跨过所有底层节点 = 表长。等 x 插入后,@161-162 会把它切成正确的两段。

查找排名的反向操作 `zslGetRank`([t_zset.c:493](../../redis-8.0.2/src/t_zset.c#L493))就是这套机制的直接受益者——从高层往低层走,边走边 `rank += x->level[i].span`([t_zset.c:504](../../redis-8.0.2/src/t_zset.c#L504)),停在目标时累加值就是 1-based 排名(注释 [t_zset.c:491-492](../../redis-8.0.2/src/t_zset.c#L491) 明说 "rank is 1-based due to the span of zsl->header to the first element")。`ZRANGE start stop` 的按位置切片则靠 `zslGetElementByRank`([t_zset.c:537](../../redis-8.0.2/src/t_zset.c#L537)),逻辑对称:从高层走,边走边 `traversed += span`([t_zset.c:526](../../redis-8.0.2/src/t_zset.c#L526)),直到 `traversed == rank`。

> **不这样会怎样**:如果没有 span,`ZRANK` 要么在底层链表 O(N) 地数(百万级 zset 一次排名查询几十毫秒,不可接受),要么在每个节点额外维护"子树大小"(像顺序统计树那样,每个节点存以其为根的子树节点数)。后者每次插入删除的旋转/调整都要更新一路的子树大小,代码量和 bug 面都大得多。skiplist 把"排名信息"摊进每条 forward 指针的 span,维护代价和插入本身一样是 O(log N),是更轻量的方案。

## 8.6 技巧精解②:zslDelete 的 span 维护——span 是可借贷的资产

有了 8.5 的推导,删除就好懂了——它和插入**完全对称**。删除节点 x 时,x 占着的那段跨距要"还给"前驱。看 `zslDeleteNode`([t_zset.c:181](../../redis-8.0.2/src/t_zset.c#L181)):

```c
/* t_zset.c:181-199 */
void zslDeleteNode(zskiplist *zsl, zskiplistNode *x, zskiplistNode **update) {
    int i;
    for (i = 0; i < zsl->level; i++) {
        if (update[i]->level[i].forward == x) {            /* 这层 x 是 update[i] 的后继 */
            update[i]->level[i].span += x->level[i].span - 1;  /* x 的 span 连同借给前驱 */
            update[i]->level[i].forward = x->level[i].forward; /* 绕过 x */
        } else {                                           /* 这层 x 没出现,但底层少了一个 */
            update[i]->level[i].span -= 1;
        }
    }
    /* ...backward 指针维护、tail 更新、level 收缩... */
}
```

两种情况,对应 span 的两种变化:

**情况一:`update[i]->level[i].forward == x`**(x 在 level i 出现,update[i] 是它在 level i 的前驱)。删除前,level i 上是 `update[i] → x → 原后继` 两段跨距;删除后合并成 `update[i] → 原后继` 一段。新 span = update[i].span + x.span − 1。**为什么 −1?** 因为 x 自己被删了,它不再算作"跨过的底层节点"。所以:

```text
新 update[i].span = (update[i] → x 跨距) + (x → 原后继 跨距) - 1(扣除 x 本身)
                  = update[i].span + x.span - 1                        …… 对应 @185
```

**情况二:`update[i]->level[i].forward != x`**(x 在 level i 没出现,update[i] 在这层的 forward 跨过了 x 所在的底层位置)。x 被删,底层少了一个节点,所以所有跨过 x 位置的高层 span 都要 −1:

```text
update[i].span -= 1                                                   …… 对应 @188
```

把 8.5 的插入和这里的删除放一起看,会看到一个优美的对称:**插入时,x 从前驱那里"借"走一段 span(前驱 span 变小、x span 继承剩余);删除时,x 把 span"还"给前驱(前驱 span 吸收 x 的 span 再减去 x 本身)**。span 就像是节点对前驱的"可借贷资产"——来的时候借走,走的时候归还。

> **钉死这件事**:skiplist 的 span 维护,插入和删除是镜像对称的。插入:`x.span = 前.span - d`、`前.span = d + 1`(d = rank[0]-rank[i]);删除:`前.span += x.span - 1` 或 `前.span -= 1`。理解了"span 是跨过的底层节点数"和"插入/删除只是把一段跨距切分/合并",这两组公式就不用背,自己推得出来。

注意 `zslDelete`([t_zset.c:209](../../redis-8.0.2/src/t_zset.c#L209))还有一个和双存有关的细节(注释 [t_zset.c:204-208](../../redis-8.0.2/src/t_zset.c#L204)):如果调用方传了 `node` 参数,被删节点**只摘链、不释放**,把节点指针还给调用方复用——包括它引用的 SDS 字符串。这正是 8.7 节双存共享 sds 得以成立的前提之一。

## 8.7 dict + skiplist 双存:一份 sds,两个索引

回到 8.1 的问题:zset 同时有 dict 和 skiplist,member 看起来存了两份,内存不是翻倍吗?**不是——member 字符串物理上只有一份,dict 和 skiplist 共享同一个 sds 指针。** 证据就在 `zsetDictType` 的定义里([server.c:511](../../redis-8.0.2/src/server.c#L511)):

```c
/* server.c:511-519 */
dictType zsetDictType = {
    dictSdsHash,               /* hash function */
    NULL,                      /* key dup */
    NULL,                      /* val dup */
    dictSdsKeyCompare,         /* key compare */
    NULL,                      /* key destructor —— Note: SDS string shared & freed by skiplist */
    NULL,                      /* val destructor */
    NULL,                      /* allow to expand */
};
```

看那个 **key destructor 是 `NULL`**,注释([server.c:516](../../redis-8.0.2/src/server.c#L516))写得明明白白:**"SDS string shared & freed by skiplist"**(SDS 字符串由 skiplist 共享和释放)。

这是什么意思?当 zset 插入一个 `(member, score)`:

- skiplist 节点的 `ele` 字段([server.h:1489](../../redis-8.0.2/src/server.h#L1489))指向这块 sds,**它拥有这块 sds**。
- dict 的 key **也指向同一块 sds**,但不拥有它(所以 dict 的 key destructor 是 NULL——dict 删除 entry 时不会去 free 这个 key)。

```text
                  ┌─────────────────────────┐
zsl 节点  ele ──→ │  sds: "player:42"       │ ←── 物理上只有这一份
                  │  (由 zsl 节点拥有/释放) │
dict key  ─────→  └─────────────────────────┘
```

删除一个 member 时,`zslFreeNode`([t_zset.c:89](../../redis-8.0.2/src/t_zset.c#L89))负责 `sdsfree(node->ele)` 释放这块 sds;dict 那边因为 key destructor 是 NULL,只是把 entry 摘掉,不会重复 free。两边各司其职,零重复释放、零内存浪费。

`zslFreeNode` 的注释([t_zset.c:86-88](../../redis-8.0.2/src/t_zset.c#L86))还留了一个"所有权可转移"的口子:"The referenced SDS string ... is freed too, **unless node->ele is set to NULL before calling this function**."——调用方可以先把 `node->ele` 置 NULL,这样 `zslFreeNode` 就不释放 sds(因为这块 sds 还被别处引用着)。这是双存共享得以安全运作的底层保障。

> **不这样会怎样**:如果 dict 和 skiplist 各存一份 member 字符串的拷贝,会怎样?第一,**内存翻倍**——member 字符串往往是 zset 里占比最大的部分(排行榜的 userId、商品 id 都不短),翻倍不可接受;第二,**一致性维护成本**——每次更新 member 都要同步两边,容易漏。共享一份 sds 指针,既省内存,又天然一致(两边看到的是同一块内存)。代价是生命周期管理要小心:必须明确"谁拥有、谁释放",Redis 用"skiplist 拥有、dict 借用(key destructor=NULL)"这条约定把它管死了。

那么 score 呢?score 是 `double`(8 字节),skiplist 节点的 `score` 字段存一份;dict 的 value 存的是指向 score 的信息,供 `ZSCORE` 走 dict 时 O(1) 拿到。所以 zset 的真实内存开销是:每个 member 一份 sds(共享)+ 一份 score(double,skiplist 节点里)+ dict entry 的指针开销 + skiplist 节点的指针/span 开销(平均 1.33 层)。**字符串本身没有重复**。

> **钉死这件事**:zset 的"双存"不是"member 存两份",而是"一份 member sds 被两个索引(dict 和 skiplist)共享指针"。靠 `zsetDictType` 的 key destructor=NULL 这一个设置 + skiplist 负责释放,实现了零拷贝共享。这是取向②(内存即数据库)在 zset 上的具体落地——用一份指针冗余,换每个操作都打到最擅长的结构。

## 8.8 几个散点:并列、精度、backward、柔性数组

**同 score 时按 member 字典序破并列。** 看 `zslInsert` 的比较条件([t_zset.c:132-135](../../redis-8.0.2/src/t_zset.c#L132)):

```c
/* t_zset.c:133-135 */
(x->level[i].forward->score < score ||
    (x->level[i].forward->score == score &&
    sdscmp(x->level[i].forward->ele,ele) < 0))
```

score 相同时,用 `sdscmp` 比较 member 字符串,字典序小的排前面。这让 zset 在重复 score 下仍是**全序**——`ZRANGEBYSCORE` 的输出确定可重现,不会因为相同 score 的 member 乱序而让结果不稳定。`zslGetRank` 的比较([t_zset.c:502-503](../../redis-8.0.2/src/t_zset.c#L502))用 `<= 0` 而非 `< 0`,也是为了在并列时正确定位。

**score 用 double 的精度。** score 是 `double`([server.h:1490](../../redis-8.0.2/src/server.h#L1490))。浮点比较天然有精度问题,但 Redis 选择"接受它":`zslInsert` 入口先 `serverAssert(!isnan(score))`([t_zset.c:127](../../redis-8.0.2/src/t_zset.c#L127))挡掉 NaN,其余的精度细节交给 IEEE 754 的确定性(同样的 double 比较,所有机器结果一致)。这是取向④(简单优先)的体现——不为精度引入任意精度小数或定点数,用 double 够了。

**backward 指针只在底层。** 看节点结构([server.h:1491](../../redis-8.0.2/src/server.h#L1491)),`backward` 是节点级的一个指针,**不是每层都有**。这让 `ZREVRANGE`(降序范围)可以沿 backward 从 tail 往前扫,O(范围大小)。这是个不对称但经济的设计:降序操作远少于升序,所以只在底层花一个反向指针,而不是每层都搞双向。每层双向意味着 forward 与 backward 指针开销翻倍,对平均 1.33 层的节点是不可接受的成本。

> **钉死这件事**:backward 只在底层维护,是 Redis"按实际需求配精度"的又一例——降序范围操作远少于升序,就只为它付出底层一个指针的成本,绝不为低频场景给每个节点的每一层都配反向指针。`zslInsert` 里 `x->backward = (update[0]==header) ? NULL : update[0]`([t_zset.c:170](../../redis-8.0.2/src/t_zset.c#L170))只在底层维护它。

**柔性数组省内存。** 前面提过,`zskiplistNode` 的 `level[]` 是柔性数组,按实际层数分配([t_zset.c:62](../../redis-8.0.2/src/t_zset.c#L62))。平均每个节点 1.33 层,而不是固定 32 层。如果固定 32 层,每个节点要 32×(8+8)=512 字节的 level 域,百万级 zset 就是几百 MB 纯浪费;柔性数组让它降到平均 ~21 字节。这是按需紧凑布局对抗统一结构体浪费。

## 8.9 skiplist vs 红黑树 vs B+树:三者放一起比

把"按 score 有序"这个角色,换成三种候选,看取舍:

| 维度 | skiplist | 红黑树 | B+ 树 |
|------|----------|--------|------|
| 单点查找 | O(log N) 期望 | O(log N) 最坏 | O(log_b N) |
| 平衡机制 | **概率**(掷骰子定层高) | **严格**(插入/删除旋转重染色) | **严格**(节点分裂/合并) |
| 实现复杂度 | 低(无旋转) | 高(多情形旋转) | 中-高(分裂合并) |
| 范围扫描 | **底层链表顺指针走,O(范围大小),常数极小** | 中序遍历,递归回溯找后继 | 叶子链表(带指针的变体) |
| 排名查询 | **span 累加,O(log N)** | 需额外维护子树大小 | 需额外维护子树大小 |
| 内存局部性 | 差(指针跳来跳去) | 差 | **好**(节点内连续) |
| 调试难度 | 低(结构直观) | 高(旋转情形多) | 中 |

Redis 选 skiplist,**不是为了在某个维度上赢红黑树,而是为了在"够用"的前提下,把实现复杂度和范围操作的顺滑度同时做到最优**。

范围扫描是 skiplist 相对红黑树最实在的优势。`ZRANGEBYSCORE` 拿"分数在 [a,b) 的所有 member":

- **skiplist**:先用 `zslFirstInRange` 在多层链表上 O(log N) 定位到第一个 ≥ a 的节点,然后**沿第 0 层 forward 指针一路向右走**,直到分数 ≥ b 停。这是纯粹的"链表顺序扫描",常数极小,天然按 score 升序输出,完全契合 `ZRANGE` 语义。
- **红黑树**:要中序遍历。中序遍历需要从起始节点不断找"中序后继",涉及"右子树最左节点""回溯到父节点"等逻辑,每个节点访问都要判断走法,常数更大。

> **钉死这件事**:对于"内存里、单线程、范围操作频繁"的场景(就是 zset),skiplist 的"零维护 + 顺滑范围扫描"完胜。最坏情况的概率损失,远小于"省掉所有旋转代码"带来的实现简单性和 bug 减少。这是 antirez 的原话精神——**skiplist 比 balanced tree 更简单,范围操作更自然,而且没有显著的性能劣势**——不是教科书标准答案,而是工程上成熟的取舍判断。

注意:如果是**数据库索引**(磁盘 IO,一次失衡代价极大,且数据量巨大),实际用的是 **B+ 树**,不是红黑树,因为磁盘 IO 更看重"节点内连续、扇出大"的内存局部性。skiplist 和红黑树都是**内存**结构,讨论它们的取舍前提就是"数据在内存里"。

## 章末:回扣、五个为什么、往哪钻

### 主线回扣

本章是**取向④(简单优先)的招牌**:选 skiplist 而非红黑树,本质是"用概率平衡换实现简单、用底层链表换范围顺滑"。Redis 宁可接受 skiplist 极小概率的最坏情况,也不要红黑树那套复杂的旋转重平衡——代码简单 = 少 bug = 好维护,这对一个长期单进程跑的内存数据库,价值远超理论上的最坏保证。同时它也是**取向②(内存即数据库)** 的体现:zset 用 dict + skiplist 双结构,但通过共享 sds 指针(zsetDictType 的 key destructor=NULL),让"双存"只多花指针、不重复字符串,把每个操作都打到最擅长的结构上。至于**取向③(编码自适应)**:zset 不是天生 skiplist,元素少时是紧凑的 listpack(见第六章),长大才升级为 dict+skiplist——"小用简单结构、大用专业结构"。

### 五个为什么

**Q1:期望层数 1.33,那 span 累加算排名真的 O(log N) 吗?**
是。虽然每层期望只走常数步,但查找路径的总步数 = 每层走的步数之和,期望是 O(log N)(因为有效层数是 O(log N),每层期望 O(1) 步)。这是 skiplist 的经典结论。最坏情况理论上可能更高,但概率随 N 指数衰减。

**Q2:span 公式里 `rank[0]-rank[i]` 为什么不是 `rank[0]-rank[i]+1` 或别的?**
因为 `rank[0]-rank[i]` 正好是"前驱 update[i] 到插入点 x 之间隔的底层节点数"(不含 x 自己)。前驱到 x 的跨距要含 x(+1),x 到原后继的跨距不含 x(所以是原 span 减掉前半段)。这个 ±1 全由"x 自己算不算一个跨过的节点"决定,8.5 节的代数推过一遍就清楚了。

**Q3:dict 和 skiplist 双存,删除一个 member 时两边怎么同步?**
`ZREM` 的实现先 `dictDelete`(从 dict 摘掉 entry,但因为 key destructor=NULL 不释放 sds),再 `zslDelete`(从 skiplist 摘节点并 `zslFreeNode` 释放 sds)。顺序很重要:必须先 dict 后 skiplist,因为 sds 的释放权在 skiplist。如果反过来,dict 那边的 entry 可能在 skiplist 已经 free 了 sds 之后还引用着它——use-after-free。

**Q4:p=1/4 和 p=1/2 相比,到底省多少内存?**
p=1/2 期望层数 2,p=1/4 期望层数 4/3≈1.33。每个 level 域是 forward 指针(8字节)+span(8字节)=16 字节。p=1/4 每节点平均 1.33×16≈21 字节,p=1/2 是 2×16=32 字节,省约 1/3。代价是查找常数略高(层数少,平均查找路径略长)。Redis 选 1/4 是空间占优的 sweet spot。

**Q5:为什么 zset 不直接用一个带"子树大小"的红黑树(顺序统计树)?**
那样 `ZRANK` 也能 O(log N)。但红黑树的旋转在每次插入删除都要更新一路的子树大小,代码量和 bug 面都更大;而 skiplist 的 span 维护和插入本身耦合在一起(就是切分/合并跨距),没有额外负担。加上 skiplist 范围扫描走底层链表比红黑树中序遍历顺滑,综合下来 skiplist 在 zset 这个场景全胜。

### 想继续深入往哪钻

- 想看 skiplist 的范围操作实现:读 [t_zset.c](../../redis-8.0.2/src/t_zset.c) 的 `zslFirstInRange`/`zslLastInRange`/`zslIsInRange`(区间定位),以及 `zslFirstInLexRange`(字典序区间,zset 的 `BYLEX` 选项)。
- 想理解 listpack → skiplist 的编码切换阈值:读 `t_zset.c` 里 `zsetAdd` 的转换逻辑,阈值是 `zset-max-listpack-entries`(默认 128)和 `zset-max-listpack-value`(默认 64),见 config 默认值。
- 想看 skiplist 的并发安全分析(虽然 Redis 单线程不需要):对比本系列《LevelDB 设计与实现深入浅出》的 SkipList 章——LevelDB 的 skiplist 是**无锁并发读**的(atomic + release-acquire),那是多线程场景的 skiplist,和 Redis 单线程的 skiplist 是同一结构的两种用法。
- 想了解 skiplist 的概率分析细节:读 William Pugh 的原始论文 "Skip Lists: A Probabilistic Alternative to Balanced Trees"(1990)。

### 引出下一章

至此 zset 的两个结构我们都讲完了:dict(第五章)管单点查,skiplist(本章)管有序和排名,两者共享 sds。但 zset 还有一类操作没覆盖:按**前缀**找 key(比如 stream 的消费者组、或 keyspace 扫描)。这需要的不是"按值有序"或"按 member 查",而是"按字符串前缀共享"——答案是 **rax**(基数树)。下一章我们看 rax 如何用前缀压缩组织字符串集合,并作为 P2 数据结构篇的收口,把"编码自适应"(取向③)的全貌串起来:从 listpack(小而紧凑)到 dict/skiplist(大而专业)再到 rax(前缀有序),Redis 为每种访问模式都配了最合适的数据结构。

---

## 验证物:如何亲手确认本章的设计

> 说明:本书写作环境为 Windows,无法直接运行 redis-server。以下 (1) gdb 断点脚本 (2) 源码常量锚点 (3) OBJECT ENCODING 观察项 均为可复现的精确指引,供读者在 Linux 环境对 redis-8.0.2 源码 `make no-opt` 编译后自行验证。**本书不附编造的运行输出**——凡未实跑的,只给脚本、预期观察变量与推导依据,不写具体数值。

### 1. gdb 断点脚本

编译:`cd redis-8.0.2 && make no-opt`
启动:`gdb ./src/redis-server`,另一终端 `redis-cli`。

```gdb
(gdb) break zslInsert          # 插入主流程,t_zset.c:122
(gdb) break zslRandomLevel     # 掷骰子定层高,t_zset.c:111
(gdb) break zslDeleteNode      # 删除 span 维护,t_zset.c:181
(gdb) break zslGetRank         # 排名累加,t_zset.c:493
(gdb) break zslCreateNode      # 看柔性数组分配,t_zset.c:60
(gdb) run --port 6379

# redis-cli 执行:ZADD myzset 100 alice 200 bob 150 carol
# gdb 在 zslInsert 停下,观察 span 公式的输入:
(gdb) print level              # 预期:zslRandomLevel 返回的随机层高(常 1-3)
(gdb) print rank[0]            # 预期:header 到底层前驱的跨度
(gdb) print update[0]          # 预期:新节点的底层前驱
# 单步到 t_zset.c:161-162,观察 span 切分:
(gdb) print update[i]->level[i].span   # 原始 span(切分前)
(gdb) step                              # 执行 @161 后:
(gdb) print x->level[i].span           # 预期:原 span - (rank[0]-rank[i])
```

**预期观察**(基于 [t_zset.c:161-162](../../redis-8.0.2/src/t_zset.c#L161) 的切分逻辑,本书未实跑):插入第 N 个元素时,`rank[0]-rank[i]` 反映前驱到插入点的底层距离;`x->level[i].span` 与 `update[i]->level[i].span` 之和等于切分前的原 span +1(多出的是 x 自己)。

### 2. 源码常量锚点(带行号,从 redis-8.0.2 源码 Grep 核实)

| 常量/字段 | 位置 | 值/说明 |
|----------|------|---------|
| `ZSKIPLIST_MAXLEVEL` | server.h:576 | 32(足够 2^64 元素) |
| `ZSKIPLIST_P` | server.h:577 | 0.25(p=1/4,期望层数 4/3) |
| `zskiplistNode` | server.h:1488-1496 | ele/score/backward/level[]{forward,span} 柔性数组 |
| `zset` | server.h:1504-1507 | dict + zsl 双结构 |
| `zsetDictType`(key destructor=NULL) | server.c:511-519 | "SDS string shared & freed by skiplist" |
| `zslRandomLevel` | t_zset.c:111-117 | threshold = P×RAND_MAX |
| `zslInsert` span 公式 | t_zset.c:161-162 | 切分跨距的两行 |
| `zslDeleteNode` span 维护 | t_zset.c:185/188 | 借还对称:`+= x.span-1` / `-= 1` |

### 3. OBJECT ENCODING 观察项(需本地 redis-server)

> 以下操作需在 Linux 本地启动 redis-server 后用 redis-cli 执行。本书未实跑,仅列观察方法与预期切换点(阈值来自 config.c 默认值,可 `CONFIG GET` 确认)。

```text
# 观察 zset 编码从 listpack → skiplist 的切换:
127.0.0.1:6379> CONFIG GET zset-max-listpack-entries   # 预期 128
127.0.0.1:6379> CONFIG GET zset-max-listpack-value     # 预期 64
127.0.0.1:6379> DEL myzset
127.0.0.1:6379> ZADD myzset 1 a                        # 1 个元素
127.0.0.1:6379> OBJECT ENCODING myzset                 # 预期 listpack(元素数 < 128)
# 循环 ZADD 到 129 个元素(超过阈值 128,见 config.c 默认):
127.0.0.1:6379> OBJECT ENCODING myzset                 # 预期 skiplist(升级为 dict+skiplist)

# 观察 score 并列时按 member 字典序(zslInsert 的 sdscmp,@t_zset.c:134):
127.0.0.1:6379> DEL col
127.0.0.1:6379> ZADD col 1 bbb
127.0.0.1:6379> ZADD col 1 aaa
127.0.0.1:6379> ZRANGE col 0 -1                       # 预期 aaa 在 bbb 前(同分按字典序)

# 观察双存共享 sds 的间接证据——ZSCORE 走 dict 是 O(1):
127.0.0.1:6379> ZSCORE col aaa                         # 预期 1(走 dict 单点查,不走 skiplist 遍历)
```

标注:以上预期基于源码常量与 [config.c](../../redis-8.0.2/src/config.c) 默认阈值推导,本书未在本地实跑;若你的 redis 版本/配置不同,切换点可能偏移,以 `CONFIG GET` 实际值为准。
