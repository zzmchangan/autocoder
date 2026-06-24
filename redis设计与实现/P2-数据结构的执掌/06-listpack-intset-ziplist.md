# 第六章 · 紧凑编码:listpack、intset 与 ziplist 的演进

> 篇:P2 数据结构的执掌
> 主轴呼应:这一章是**取向②(内存即数据库)+ 取向④(简单优先)的招牌**。Redis 把"小集合"这件事做到极致——不分配指针、不分配对象头,把整个集合压进一块连续内存,每个字节都榨干。读完这章你会明白:listpack 凭什么取代了 ziplist,intset 凭什么比 hash 表还省,以及"为什么 backlen 存的是自己的长度而不是前一个的长度"这一处看似无关紧要的字段差异,直接决定了 Redis 7.0 的一场数据结构大换血。

---

## 读完本章你会明白

1. **为什么 hash/set/zset 在元素少时不走 dict、不走 hashtable,而要走一块连续内存**——答案不是"省一次指针跳转",而是省到极致:零对象头、零对齐填充,一个两字段的 hash 在紧凑编码下能把壳子开销从几百字节压到十几字节。
2. **listpack 凭什么取代 ziplist**——根子不在某个细节优化,而在一个字段:ziplist 的 `prevlen` 记的是"前一个 entry 的长度"(向前看),listpack 的 `backlen` 记的是"自己的长度"(向自己看)。前者让修改会**级联**到所有后继(O(N²) 最坏),后者让修改**只动自己**(O(1))。这是从结构层面消灭病灶,而不是在算法层补补丁。
3. **`lpEncodeIntegerGetType` 的整数编码空间为什么是 7/13/16/24/32/64 这一串"奇怪档位"**——因为每个档位对应一种"小整数高频"的现实分布:0–127 是计数器和状态码的地盘,用 1 字节装下;13 位整数塞进 2 字节,覆盖一批小 ID;-4096~4095 的对称区间则是为了负数也只花 2 字节。这是一份**按现实数据分布画的压缩蓝图**。
4. **`backlen` 为什么"续位标记的方向和常规 varint 相反"**——为了反向遍历。从尾部往前走时,先碰到的字节是 backlen 的末位,续位标记必须设计成"从末位往前读,遇到 0 停"。这个方向反过来,正向遍历的代价不变,反向遍历的代价从 O(可能要回跳)降到 O(backlen 字节数)。
5. **intset 升级编码时为什么要"从后往前搬"**——新编码下每个元素占的字节数变多,从前往后搬会让"大元素"覆盖"还没读的小元素"。这个看似不起眼的循环方向,是"用操作顺序换额外缓冲区"的典范。intset 还做了另一个反直觉的取舍:**只升不降**——把代价留给少见时刻,而不是频繁时刻。

---

> **如果一读觉得太难:先只记住三件事**——
> ① 三个紧凑编码的共同特征是"一块连续内存 + 自描述编码",没有指针、没有对象头;
> ② listpack 取代 ziplist 的根子是:每个 entry 只记**自己的长度**,改一个 entry 不影响别人,消灭了 ziplist 的连锁更新;
> ③ intset 是定长整数数组 + 二分查找,升级编码(2→4→8 字节)时从后往前搬。
> 这三件事,就是本章的全部。

---

> **一句话点破:紧凑编码是用"一块连续内存 + 自描述变长编码"换"零壳子 + 缓存行完美命中",代价是写入 O(N) 和查找 O(N);listpack 取代 ziplist 不是因为它更快,而是因为它把 ziplist 那颗 O(N²) 的连锁更新定时炸弹,从字段定义上直接拆掉了——复杂度不消灭,只转移,listpack 把它从"修改路径"转移到了"读路径",而读路径恰好是 Redis 想要快的路径。**

第五章我们看清了 SDS 字符串,也顺带瞥见了 `dict`、`adlist` 这些基于指针的结构。一个自然的问题是:`hash`、`set`、`zset` 这类上层对象,既然底层不是 `dict` 就是 `hashtable` + 跳表,为什么还要专门搞一套 `listpack`、`intset`?这一章就来回答它。我们会从"为什么不能直接用 dict"的动机讲起,逐一拆开三种紧凑编码的源码,把它们的精妙处和代价摆清楚,最后落回 listpack 取代 ziplist 那场 7.0 起的内部换血。

## 6.1 这块要解决什么:小集合的壳子税

想象你是一个线上 Redis 实例,管着几亿条 key。其中相当一部分是只有三五个字段的小 hash(用户画像:`{name:"alice", age:30, city:"sh"}`)、只有十几个元素的小 set(标签集合、白名单)、只有几十个 entry 的小 zset(每日榜)。

这些小集合的共同点:**元素少,但 key 多**。如果你按"教科书做法"——每个 hash 上一个 `dict`,每个 entry 是一个 `dictEntry` + 一份 `sds` key + 一份 `redisObject` value——光壳子就要吃掉一大块内存。来算一笔账(64 位机器):

- `dictEntry` 三个指针(key/value/next)= 24 字节;
- `sds` 头(至少 SDS_HDR8 = 8 字节起)+ 字符串本体;
- `redisObject` 头 = 16 字节;
- 哈希表本身的两段 `dictht` + 桶指针数组。

一个最简单的小 hash,字段 `{name:"alice"}`——**真正的数据("alice" + "alice" 的 key)加起来 10 字节不到,壳子奔着 200 字节去了**。数据没占多少,壳子占了一大半。更要命的是这些指针指向的内存是**碎片化**的:每个 entry 散落在堆的各个角落,一次 `HGETALL` 要做 N 次随机内存访问,每次都可能触发一次 cache miss,延迟按纳秒堆叠。

> **不这样会怎样**:小集合在 Redis 实例里**数量极多**(几亿 key 里很多是小 hash),壳子税被放大几亿倍。按上面估算,一个 10 字节的真实数据撑出 200 字节的内存占用——内存利用率 5%。对一个**内存即数据库**(取向②)的产品,这是不可接受的。它还有一个隐性代价:指针散落带来的 cache miss,会让 `HGETALL` 这类全字段读取的延迟从"纳秒级"退化到"微秒级"。

Redis 的回答是:**当集合足够小时,把它压成一块连续内存**。整块内存里没有指针、没有对象头,每个元素**自描述**编码和长度,从头到尾贴在一起。这就是本章三位主角——`listpack`、`intset`、以及它取代的 `ziplist`——的共同特征:**紧凑编码(compact encoding)**。

这块连续内存不仅省了壳子税,还顺带解决了缓存问题:一次 `HGETALL` 只需要把这一小块内存顺序扫一遍,**几乎 100% 命中 cache line**。这是取向②(内存即数据库)和取向④(简单优先)联手的硬功夫——别人觉得不值得做的内存优化,Redis 做到极致。

那么什么时候用紧凑编码、什么时候切回指针结构?由一组可配置阈值决定,这就是编码自适应(取向③)的入口,本章末尾给出默认值,把读者顺滑地引向第九章。

> **钉死这件事**:紧凑编码的动机不是"快"(它的查找是 O(N),比 hash 表的 O(1) 慢),而是"省"——在元素少时,把壳子税压到几乎为零,顺带让 cache line 完美命中。"小而紧凑"和"大而专业"是两种截然不同的优化目标,阈值是它们之间的分界线。这是取向②和取向④在数据结构层的联手。

## 6.2 intset:整数集合的三段式升级

三个紧凑编码里,`intset` 最简单也最锋利。它专门服务一种场景:**纯整数集合**。整数定长、可排序、可二分,不需要任何变长编码的元数据。这是把"特殊场景"做到极致的典范——能用一个数组解决的事,绝不引入更复杂的结构(取向④)。

先看它的全貌([intset.h:35](../../redis-8.0.2/src/intset.h#L35)):

```c
/* intset.h:35-39 */
typedef struct intset {
    uint32_t encoding;   // 当前编码:int16 / int32 / int64
    uint32_t length;     // 元素个数
    int8_t contents[];   // 柔性数组,真正的整数们(连续存储、升序排列)
} intset;
```

三个字段,总共 8 字节头 + 一块连续的整数数组。它整体的字节布局长这样:

```text
                            intset 整体布局
┌────────────────────────┬──────────────────────┬────────────────────────────────────┐
│ encoding (4 字节,小端) │ length (4 字节,小端)│ contents[] 连续整数数组(升序)      │
│ INTSET_ENC_INT16/32/64 │       元素个数        │ [int0][int1][int2]...[intN-1]      │
└────────────────────────┴──────────────────────┴────────────────────────────────────┘
       8 字节固定头                                     每个元素定长(encoding 决定)
```

注意 `encoding` 不是"每个元素一个",而是**整个 intset 共用一个编码**——所有元素要么都是 int16、要么都是 int32、要么都是 int64。三种编码是常量,按大小有序排列([intset.c:41](../../redis-8.0.2/src/intset.c#L41)):

```c
/* intset.c:39-43 */
/* Note that these encodings are ordered, so:
 * INTSET_ENC_INT16 < INTSET_ENC_INT32 < INTSET_ENC_INT64. */
#define INTSET_ENC_INT16 (sizeof(int16_t))  // = 2
#define INTSET_ENC_INT32 (sizeof(int32_t))  // = 4
#define INTSET_ENC_INT64 (sizeof(int64_t))  // = 8
```

为什么注释要强调"these encodings are ordered"?因为代码里会直接用 `enc >`、`enc <=` 来判断要不要升级,这依赖三个常量的数值顺序(2 < 4 < 8)。新建 intset 默认从最小编码起步([intset.c:98](../../redis-8.0.2/src/intset.c#L98)):

```c
/* intset.c:97-103 */
/* Create an empty intset. */
intset *intsetNew(void) {
    intset *is = zmalloc(sizeof(intset));
    is->encoding = intrev32ifbe(INTSET_ENC_INT16);  // 默认 int16,最省内存
    is->length = 0;
    return is;
}
```

这里 `intrev32ifbe` 是字节序转换宏(定义在 endianconv.h,不在 intset.h)——intset 里的整数一律**小端存储**,跨平台一致。这一点对持久化(RDB/AOF)至关重要:磁盘上写下的字节序必须确定,不能依赖运行时 CPU 是大端还是小端。小端机器上 `intrev32ifbe` 是 no-op(原样返回),大端机器上做 32 位字节反转。

**升级机制**是 intset 最精巧的一笔。插入一个超出当前编码范围的整数时,触发 `intsetUpgradeAndAdd`([intset.c:159](../../redis-8.0.2/src/intset.c#L159)):

```c
/* intset.c:158-182 */
/* Upgrades the intset to a larger encoding and inserts the given integer. */
static intset *intsetUpgradeAndAdd(intset *is, int64_t value) {
    uint8_t curenc = intrev32ifbe(is->encoding);
    uint8_t newenc = _intsetValueEncoding(value);
    int length = intrev32ifbe(is->length);
    int prepend = value < 0 ? 1 : 0;                  /* 负数塞开头,非负塞末尾 */

    /* First set new encoding and resize */
    is->encoding = intrev32ifbe(newenc);              /* ① 先改编码 */
    is = intsetResize(is,intrev32ifbe(is->length)+1); /* ② 再扩容 +1 元素 */

    /* Upgrade back-to-front so we don't overwrite values.
     * Note that the "prepend" variable is used to make sure we have an empty
     * space at either the beginning or the end of the intset. */
    while(length--)                                   /* ③ 从后往前搬运 */
        _intsetSet(is,length+prepend,_intsetGetEncoded(is,length,curenc));

    /* Set the value at the beginning or the end. */
    if (prepend)                                      /* ④ 写入触发值 */
        _intsetSet(is,0,value);
    else
        _intsetSet(is,intrev32ifbe(is->length),value);
    is->length = intrev32ifbe(intrev32ifbe(is->length)+1);
    return is;
}
```

### 关键细节一:从后往前搬,绕开覆盖

这是用"操作顺序"巧妙绕开"额外缓冲区"的典范。升级意味着每个元素占的字节数变多(比如从 int16 的 2 字节变成 int32 的 4 字节),元素整体往后挪。如果从前往后搬,**新的大元素会覆盖后面还没读的小元素**:

```text
升级前(encoding=int16, 每元素 2 字节,假设 4 个元素):
  偏移:    0    2    4    6
  数据:   [A]  [B]  [C]  [D]      每个字母代表一个 int16

扩容后(encoding 改为 int32, 每元素 4 字节,contents 缓冲区扩到 5×4=20 字节,
        但旧的 4×2=8 字节数据还在原位,下面用小写 a,b,c,d 表示旧字节):
  偏移:    0    2    4    6    8   10   12   14   16   18
  数据:   [a a][b b][c c][d d][????????????????????????]

❌ 如果从前往后搬(把 A 写到偏移 0,占 4 字节):
   写 A 到偏移 0~3 → 覆盖了 a a b b → B 的旧数据被毁 → 后面读不出来

✅ 如果从后往前搬(先搬 D 到偏移 12~15):
   D 读自偏移 6~7(原位),写到 12~15(新位置),两者不重叠 → 安全
   C 读自偏移 4~5,写到 8~11,不重叠 → 安全
   ... 一直搬到 A
```

这就是为什么循环是 `while(length--)`——从最大下标 `length-1` 递减到 0,严格反向。源码注释那一句 `Upgrade back-to-front so we don't overwrite values`(intset.c:169)点破了这件事。

> **钉死这件事**:intset 升级时 `while(length--)` 从后往前搬,是为了避免"新大元素覆盖未读的小元素"。这是用循环顺序(免费)换额外缓冲区(昂贵)的典范。同样的手法你在 C 标准库的 `memmove`(区分 src/dst 重叠方向)里也能看到——**有重叠的批量搬运,方向就是正确性本身**。

### 关键细节二:新值只能在两端,且不查重

升级是由一个越界值触发的——要么比当前所有元素都大(追加到末尾),要么比所有元素都小(塞到开头)。`prepend = value < 0 ? 1 : 0` 就是这个判断。这依赖一个不变式:**intset 始终保持升序**(`intsetAdd` 在非升级路径里靠 `intsetSearch` 找插入位置,见下文)。所以触发升级的值**天然不重复**(它要么大于所有元素,要么小于所有元素),代码里**根本不调 `intsetSearch` 查重**(对照 [intset.c:214-216](../../redis-8.0.2/src/intset.c#L214) 的升级分支直接 return,不走查重分支)。注释 intset.c:211-213 把这件事说得明白:"this value should be either appended (if > 0) or prepended (if < 0), because it lies outside the range of existing values."

升序带来的另一项红利是**二分查找**。`intsetSearch`([intset.c:117](../../redis-8.0.2/src/intset.c#L117))就是一个教科书式的二分,先做两个边界剪枝(比最大值大、比最小值小直接定位插入点),再进 while 循环:

```c
/* intset.c:117-156,精简 */
static uint8_t intsetSearch(intset *is, int64_t value, uint32_t *pos) {
    int min = 0, max = intrev32ifbe(is->length)-1, mid = -1;
    int64_t cur = -1;

    if (intrev32ifbe(is->length) == 0) {                    /* 空集 */
        if (pos) *pos = 0;
        return 0;
    } else {
        if (value > _intsetGet(is,max)) {                    /* 边界剪枝:比最大值大 */
            if (pos) *pos = intrev32ifbe(is->length);
            return 0;
        } else if (value < _intsetGet(is,0)) {               /* 边界剪枝:比最小值小 */
            if (pos) *pos = 0;
            return 0;
        }
    }

    while(max >= min) {
        mid = ((unsigned int)min + (unsigned int)max) >> 1;  /* 强转 unsigned 防溢出 */
        cur = _intsetGet(is,mid);
        if (value > cur) min = mid+1;
        else if (value < cur) max = mid-1;
        else break;
    }

    if (value == cur) { if (pos) *pos = mid; return 1; }
    else { if (pos) *pos = min; return 0; }
}
```

查找 O(log N),插入和删除因为要 `memmove` 搬动数组是 O(N),但 N 受阈值控制(默认上限 512,见 6.5 节),完全可接受。注意 `mid = ((unsigned int)min + (unsigned int)max) >> 1` 这一行的强转——经典防溢出写法,如果 `min+max` 用 int 算,在大 N 下可能整数溢出成负数,导致 `mid` 错乱、数组越界。这是写二分的人最容易踩的坑(Java 标准库也曾因此修 bug),Redis 在 intset 这一处把"标准答案"写对了。

### 关键细节三:只升不降

一个常被忽略的取舍:**intset 只支持升级,不支持降级**。删掉那个触发升级的大数,编码不会回退。看 `intsetRemove`([intset.c:235](../../redis-8.0.2/src/intset.c#L235)):

```c
/* intset.c:235-253 */
intset *intsetRemove(intset *is, int64_t value, int *success) {
    uint8_t valenc = _intsetValueEncoding(value);
    uint32_t pos;
    if (success) *success = 0;

    if (valenc <= intrev32ifbe(is->encoding) && intsetSearch(is,value,&pos)) {
        uint32_t len = intrev32ifbe(is->length);
        if (success) *success = 1;
        /* Overwrite value with tail and update length */
        if (pos < (len-1)) intsetMoveTail(is,pos+1,pos);   /* 前移覆盖 */
        is = intsetResize(is,len-1);                       /* 缩容 */
        is->length = intrev32ifbe(len-1);
    }
    return is;
}
```

从头到尾**没有降级逻辑**——没有重新扫描所有元素找最大值、没有 `intsetDowngrade` 函数。即使把 int64 集合里的大值删光、剩下全都是 int16 范围内的小值,**encoding 仍保持 INT64**,每个元素仍占 8 字节。

为什么这样设计?降级要再次全表搬运(O(N) 的 `memmove`),而编码升级在集合生命周期里通常只发生有限次(int16 → int32 → int64,最多两次)。**为偶尔的升级反复降级得不偿失——把代价留给"少见的升级时刻",而不是"频繁的删除时刻"**。这是复杂度守恒在数据结构层的一个变体:**复杂度不消灭,只转移**,关键是转移到正确的时刻。Redis 选了把它留给"少见且可预测的升级",而不是"频繁的删除"。

> **钉死这件事**:intset **只升不降**是刻意的取舍。降级要全表搬运,代价 O(N);升级在生命周期里通常只发生 1-2 次,代价可预测。把代价留给少见时刻,而不是频繁时刻——这是取向④(简单优先)在删除路径的体现:**不为低频场景付出高频成本**。

把 intset 三段式总结一下:**统一编码换极简**(每个元素定长、可二分、缓存完美命中)+ **从后往前搬避免覆盖**(用顺序换缓冲)+ **只升不降**(把代价留给少见时刻)。三个取舍咬合,成就了一个用不到 200 行 C 代码写出来、却能在小集合场景把 hash 表打趴下的数据结构。

## 6.3 listpack:一块连续内存,每个 entry 自描述

intset 解决了纯整数集合,但 hash、list、zset 的元素是字符串(以及整数 score),长度千差万别,定长编码行不通。`listpack` 就是 Redis 7.0 起用来装载变长元素的紧凑容器,也是 ziplist 的继任者。

先看它的整体布局。头部 6 字节,尾部 1 字节:

```c
/* listpack.c:27 */
#define LP_HDR_SIZE 6       /* 32 bit total len + 16 bit number of elements. */
...
/* listpack.c:76 */
#define LP_EOF 0xFF
```

整体字节布局如下(本书整理,源码无现成注释段):

```text
                            listpack 整体布局
┌──────────────────────┬──────────────────────┬─────────────────────────────────┬───────┐
│ total-bytes (4字节)  │ num-elements (2字节) │ entry[0] entry[1] ... entry[N-1]│ 0xFF  │
│   整块 listpack 的   │   元素个数(<65535)  │ 一连串自描述 entry              │ EOF   │
│   总字节数(小端)    │   (小端;若≥65535    │                                 │       │
│                      │   写 65535 标记未知) │                                 │       │
└──────────────────────┴──────────────────────┴─────────────────────────────────┴───────┘
       6 字节固定头                 中间变长                              1 字节尾

单个 entry 内部:
┌────────────────────────────────────────────┬──────────────────────────┐
│ encoding + data  (正向写入,变长 1~9 字节) │ backlen(反向变长,1~5 字节)│
│   前缀位模式决定后续字节如何解释          │  本 entry 自己的总字节数  │
└────────────────────────────────────────────┴──────────────────────────┘
```

前 4 字节是整块 listpack 的总字节数(小端),后 2 字节是元素个数。如果元素超过 65534 个,2 字节存不下,就写入一个特殊值 `LP_HDR_NUMELE_UNKNOWN`(= 65535),后续取个数只能全表扫描(注释 [listpack.c:27](../../redis-8.0.2/src/listpack.c#L27) 直说)。尾部一个 `0xFF` 标记结束。一个空 listpack 初始就是 7 字节:6 字节头 + 1 字节 EOF。

中间是一连串 entry。**listpack entry 的精妙之处在于"自描述 + 末尾反向回退编码"**:开头是编码字节(可能含数据),末尾是 `backlen`——**本 entry 自己的总字节数**,用变长编码存储。这两件事——前缀编码 + 自描述长度——合在一起,支撑了 listpack 的双向遍历和零壳子布局。

### 6.3.1 整数编码空间:7/13/16/24/32/64 六档

listpack 的整数编码前缀是一张精心设计的"档位表"。这些前缀位模式在文件开头一一列出([listpack.c:34-74](../../redis-8.0.2/src/listpack.c#L34)),整理成表:

| 编码 | 前缀位模式 | 掩码 | 数据范围 | entry 编码部分大小 |
|------|-----------|------|----------|-------------------|
| 7-bit uint | `0xxxxxxx` | 0x80 | 0 ~ 127 | 1 字节(前缀即数据) |
| 6-bit str  | `10xxxxxx` | 0xC0 | 长度 0~63 | 1 字节头 + N 字节 |
| 13-bit int | `110xxxxx` | 0xE0 | -4096 ~ 4095 | 2 字节(前缀 + 高位+低字节) |
| 12-bit str | `1110xxxx` | 0xF0 | 长度 0~4095 | 2 字节头 + N 字节 |
| 16-bit int | `11110001` | 0xFF | -32768 ~ 32767 | 3 字节(前缀 + 2 字节小端) |
| 24-bit int | `11110010` | 0xFF | ±8388607 | 4 字节(前缀 + 3 字节小端) |
| 32-bit int | `11110011` | 0xFF | ±2147483647 | 5 字节(前缀 + 4 字节小端) |
| 64-bit int | `11110100` | 0xFF | 全 int64 | 9 字节(前缀 + 8 字节小端) |
| 32-bit str | `11110000` | 0xFF | 长度 0~2³²-1 | 5 字节头 + N 字节 |

设计思想一句话:**常见小整数用最短编码**。具体看 `lpEncodeIntegerGetType`([listpack.c:251](../../redis-8.0.2/src/listpack.c#L251)):

```c
/* listpack.c:251-298,精简 */
static inline void lpEncodeIntegerGetType(int64_t v, unsigned char *intenc, uint64_t *enclen) {
    if (v >= 0 && v <= 127) {                  /* 7-bit: 0~127 → 1 字节 */
        if (intenc) intenc[0] = v;             /* 编码前缀位 0,数据直接是低 7 位 */
        if (enclen) *enclen = 1;
    } else if (v >= -4096 && v <= 4095) {       /* 13-bit: -4096~4095 → 2 字节 */
        if (v < 0) v = ((int64_t)1<<13)+v;      /* 负数加 2^13 转无符号余数 */
        if (intenc) {
            intenc[0] = (v>>8)|LP_ENCODING_13BIT_INT;  /* 前缀 0xC0 | 高 5 位 */
            intenc[1] = v&0xff;                        /* 低 8 位 */
        }
        if (enclen) *enclen = 2;
    } else if (v >= -32768 && v <= 32767) {     /* 16-bit → 3 字节 */
        ...
        if (enclen) *enclen = 3;
    } else if (v >= -8388608 && v <= 8388607) { /* 24-bit → 4 字节 */
        ...
        if (enclen) *enclen = 4;
    } else if (v >= -2147483648 && v <= 2147483647) { /* 32-bit → 5 字节 */
        ...
        if (enclen) *enclen = 5;
    } else {                                     /* 64-bit → 9 字节 */
        ...
        if (enclen) *enclen = 9;
    }
}
```

每一档都是**对应"小整数高频"的现实分布**:

- **7-bit (0~127,1 字节)**:计数器、状态码、布尔标志、小序号。这是 Redis 里出现频率最高的一类整数。一个 `HSET user:1 login_count 5`——5 这个值,1 字节就装下了,加 1 字节 backlen 总共 2 字节。
- **13-bit (-4096~4095,2 字节)**:一批小 ID、小坐标、温度等带正负的物理量。这一档特别照顾负数——常见的负数(小负偏移、差值)用 2 字节装下,而不是退化到 16-bit 的 3 字节。
- **16/24/32/64-bit**:覆盖更大的整数。每跨一档,前缀标记多 1 字节,数据部分按需扩展。

**算一笔账**。在 listpack 里存一个 `HSET user:1 login_count 5` 的 value(`5`):
- 7-bit 整数编码:1 字节(前缀 + 数据合一);
- backlen:1 字节(因为 entry 总长 = 1);
- **总共 2 字节**。

对比一下用 dict + redisObject + sds 存同一个 5:
- `dictEntry` 三个指针 = 24 字节;
- value 的 `redisObject` 头 = 16 字节;
- `sds` 头(8 字节)+ 字符串 "5" = 9 字节;
- **总共 49 字节**。

2 字节 vs 49 字节,24 倍差距。这就是取向②(内存即数据库)的硬功夫:**每个字节都要榨干**。

字符串编码(`lpEncodeString` 在 [listpack.c:414](../../redis-8.0.2/src/listpack.c#L414))同样的思路:6-bit(长度 < 64)用 1 字节头、12-bit(< 4096)用 2 字节头、32-bit(< 2³²)用 5 字节头。注意:代码里**没有** `lpEncodeStringGetType` 这个函数——字符串和整数的编码判定统一走 `lpEncodeGetType`([listpack.c:323](../../redis-8.0.2/src/listpack.c#L323)),它先用 `lpStringToInt64` 尝试把字符串解析成整数,解析成功走整数分支(更省),否则走字符串分支:

```c
/* listpack.c:323-334 */
static inline int lpEncodeGetType(unsigned char *ele, uint32_t size,
                                  unsigned char *intenc, uint64_t *enclen) {
    int64_t v;
    if (lpStringToInt64((const char*)ele, size, &v)) {
        lpEncodeIntegerGetType(v, intenc, enclen);    /* 能解析成整数 → 整数编码 */
        return LP_ENCODING_INT;
    } else {
        if (size < 64) *enclen = 1+size;              /* 6-bit str */
        else if (size < 4096) *enclen = 2+size;       /* 12-bit str */
        else *enclen = 5+(uint64_t)size;              /* 32-bit str */
        return LP_ENCODING_STRING;
    }
}
```

一个易被忽略的细节:用户写 `SET counter 5`,客户端传过来的是字符串 `"5"`(5 字节 SDS),但 listpack 在 `lpAdd` 路径里会尝试把它解析成整数(int64),成功就按整数编码存。所以同一个 `5`,在 listpack 里是 1 字节的整数编码,而不是 2 字节的字符串编码(1 字节头 + 1 字符)。**这是一次透明、无损的"运行时类型推断"**——客户端以为是字符串,服务端悄悄按整数存,省一半空间。

> **钉死这件事**:listpack 的整数编码空间是一份"按现实数据分布画的压缩蓝图"。0~127 是计数器和状态码的地盘,1 字节装下;13-bit 照顾负数;更大的整数按需扩展。**常见值用最短编码,罕见值才付全价**,这是霍夫曼思想在内存数据结构里的一次落地——不是用通用压缩算法(LZF/LZ4),而是用预设档位,把"了解数据长什么样"的红利直接做进字段定义。

### 6.3.2 backlen:反向变长编码的灵魂

讲完整数编码,来看 listpack 真正的灵魂——`backlen`。每个 entry 末尾的 backlen 用 1~5 字节编码本 entry 自己的总字节数,函数是 `lpEncodeBacklen`([listpack.c:341](../../redis-8.0.2/src/listpack.c#L341)):

```c
/* listpack.c:341-376 */
static inline unsigned long lpEncodeBacklen(unsigned char *buf, uint64_t l) {
    if (l <= 127) {                    /* 1 字节 */
        if (buf) buf[0] = l;           /* 最高位 0,无续位 */
        return 1;
    } else if (l < 16383) {            /* 2 字节 */
        if (buf) {
            buf[0] = l>>7;             /* 高位字节在前(数据),最高位 0 */
            buf[1] = (l&127)|128;      /* 末字节(数据低 7 位),最高位 1 */
        }
        return 2;
    } else if (l < 2097151) {          /* 3 字节 */
        if (buf) {
            buf[0] = l>>14;
            buf[1] = ((l>>7)&127)|128;
            buf[2] = (l&127)|128;
        }
        return 3;
    } else if (l < 268435455) {        /* 4 字节 */
        if (buf) {
            buf[0] = l>>21;
            buf[1] = ((l>>14)&127)|128;
            buf[2] = ((l>>7)&127)|128;
            buf[3] = (l&127)|128;
        }
        return 4;
    } else {                           /* 5 字节 */
        if (buf) {
            buf[0] = l>>28;
            buf[1] = ((l>>21)&127)|128;
            buf[2] = ((l>>14)&127)|128;
            buf[3] = ((l>>7)&127)|128;
            buf[4] = (l&127)|128;
        }
        return 5;
    }
}
```

这里有一个常被讲反的细节:**backlen 的"续位标记方向"和常规 varint 相反**。常规 varint(比如 protobuf 的 varint)是"高位标记在低位字节,从低到高写,遇到最高位 0 停"——也就是说,反向遍历时不知道要读几个字节,要先回头数。而 backlen 的设计是:

```text
以一个 3 字节 backlen 为例(假设编码值 l 的高位字节是 0x05,中位是 0x43,低位是 0x21):
  
  字节顺序(buf[0..2],物理上从左到右):
  ┌────────┬────────┬────────┐
  │ 0x05   │ 0xC3   │ 0xA1   │
  │ 高位   │ 中位   │ 低位   │
  │ 最高位0│ 最高位1│ 最高位1│  ← 注意:第一个字节(高位)最高位 0,末字节最高位 1
  └────────┴────────┴────────┘
     ↑                        ↑
   正向遍历时先读到这里     反向遍历时先碰到这里(末字节)
```

**关键:第一个字节(`buf[0]`,高位数据)的最高位是 0(因为 `buf[0] = l>>N` 没或 0x80),其余字节(中位、低位)的最高位是 1(因为都 `|128`)。** 但反向解码时是从 `buf[n-1]`(末字节,低位)**往前**读的——`lpDecodeBacklen` 在 [listpack.c:397](../../redis-8.0.2/src/listpack.c#L397):

```c
/* listpack.c:395-408 */
/* Decode the backlen and returns it. If the encoding looks invalid (more than
 * 5 bytes are used), UINT64_MAX is returned to report the problem. */
static inline uint64_t lpDecodeBacklen(unsigned char *p) {
    uint64_t val = 0;
    uint64_t shift = 0;
    do {
        val |= (uint64_t)(p[0] & 127) << shift;   /* 取低 7 位,按 shift 累加 */
        if (!(p[0] & 128)) break;                 /* 最高位 0 → 这是 buf[0],结束 */
        shift += 7;
        p--;                                      /* 向左(向前)继续读 */
        if (shift > 28) return UINT64_MAX;        /* 超过 5 字节 → 非法 */
    } while(1);
    return val;
}
```

**仔细看这个循环的方向**:`p` 一开始指向某个 entry 的 backlen **末字节**(`buf[n-1]`,低位数据,最高位 1),取它的低 7 位作为 val 的最低位;然后 `p--` 向前(物理上的左边、逻辑上的高位字节)走一格,读 `buf[n-2]`,以此类推。直到读到最高位为 0 的字节(`buf[0]`,高位数据),停止。

那么"末字节最高位 1、首字节最高位 0"的方向,对反向解码意味着什么?意味着**反向解码时,从末字节开始读,一直读到首字节为止——它总是知道在哪停**(最高位 0 是停止信号)。如果方向反过来(首字节最高位 1,末字节最高位 0),反向解码时**第一个碰到的字节就是末字节,但末字节最高位 0 解码器会立刻以为读完了**,只读到 1 个字节就停,错误。

所以这个"反向 varint"的设计,本质是**为反向遍历量身定制的**:从尾部往前走时,先碰到的字节(backlen 末字节)恰好是"还有后续"的信号(最高位 1),一直读到"没有后续"的字节(backlen 首字节,最高位 0)才停。换一个方向就废了。

> **钉死这件事**:backlen 的续位标记方向是"首字节最高位 0、其余字节最高位 1",和常规 varint 相反。这是为反向遍历量身定制的——`lpDecodeBacklen` 从末字节开始往前读,靠"最高位 0 = 停止"的信号确定 backlen 总字节数。**方向不是随便选的,是为双向遍历(O(1) 跳到前/后 entry)直接做进字段定义里**。

### 6.3.3 双向遍历:lpNext 与 lpPrev

有了整数编码和 backlen,listpack 的双向遍历就顺理成章。先看正向 `lpNext`([listpack.c:494](../../redis-8.0.2/src/listpack.c#L494)):

```c
/* listpack.c:491-500 */
/* If 'p' points to an element of the listpack, calling lpNext() will return
 * the pointer to the next element (the one on the right), or NULL if 'p'
 * already pointed to the last element of the listpack. */
unsigned char *lpNext(unsigned char *lp, unsigned char *p) {
    assert(p);
    p = lpSkip(p);                  /* 跳过当前 entry:lpCurrentEncodedSize + backlen 字节数 */
    if (p[0] == LP_EOF) return NULL;/* 遇到 0xFF → 末尾 */
    lpAssertValidEntry(lp, lpBytes(lp), p);
    return p;
}
```

正向很简单:从当前 entry 起始指针 `p` 出发,先读它的编码前缀确定 encoding+data 占多少字节(`lpCurrentEncodedSizeUnsafe`),再读它的 backlen 确定它本身占多少字节(`lpEncodeBacklenBytes`),两者相加就是 entry 总长,`p += entrylen` 即跳到下一个 entry。如果跳到 `0xFF`,说明到尾了,返回 NULL。

再看反向 `lpPrev`([listpack.c:505](../../redis-8.0.2/src/listpack.c#L505)):

```c
/* listpack.c:502-514 */
/* If 'p' points to an element of the listpack, calling lpPrev() will return
 * the pointer to the previous element (the one on the left), or NULL if 'p'
 * already pointed to the first element of the listpack. */
unsigned char *lpPrev(unsigned char *lp, unsigned char *p) {
    assert(p);
    if (p-lp == LP_HDR_SIZE) return NULL;     /* 已是首元素,无前驱 */
    p--;                                       /* 退到前一 entry 的 backlen 末字节 */
    uint64_t prevlen = lpDecodeBacklen(p);    /* 反向解码出前一 entry 的 encoding+data 长度 */
    prevlen += lpEncodeBacklenBytes(prevlen); /* 加上 backlen 自身所占字节数 */
    p -= prevlen-1;                           /* 跳到前一 entry 的首字节 */
    lpAssertValidEntry(lp, lpBytes(lp), p);
    return p;
}
```

**这是 listpack 设计的精华所在**。要往前走,只需:
1. `p--` 退一格,落到前一个 entry 的 backlen **末字节**;
2. 用 `lpDecodeBacklen` 从这个末字节**往前**读完整 backlen,得到前一 entry 的 encoding+data 总字节数 `prevlen`;
3. `prevlen += lpEncodeBacklenBytes(prevlen)` 再加上 backlen 自身占的字节数;
4. `p -= prevlen-1` 向左偏移,正好落到前一 entry 的首字节。

**全程只依赖当前 entry 自己的字节**——具体说,只依赖"当前 entry 的首字节位置"(用来 `p--`)和"前一个 entry 的 backlen 字段"。**它根本不需要读前一个 entry 的 encoding/data,也不依赖任何外部元数据**。这一条性质,是 listpack 取代 ziplist 的根本原因,下一节会展开。

> **钉死这件事**:listpack 的反向遍历靠"当前 entry 的 backlen"自描述——`p--` 落到前 entry 的 backlen 末字节,反向解码出前 entry 长度,一偏移就到位。**修改一个 entry,只动它自己的字节,前后 entry 的 backlen 不变**,这是它能在结构层面消灭连锁更新的物理基础。

### 6.3.4 LISTPACK_MAX_SAFETY_SIZE:1 GB 的硬上限

最后看一个安全阀。listpack 有一个硬上限 `LISTPACK_MAX_SAFETY_SIZE`([listpack.c:123](../../redis-8.0.2/src/listpack.c#L123)):

```c
/* listpack.c:121-123 */
/* Don't let listpacks grow over 1GB in any case, don't wanna risk overflow in
 * Total Bytes header field */
#define LISTPACK_MAX_SAFETY_SIZE (1<<30)
```

注释直白:**"不让 listpack 长过 1 GB,免得 Total Bytes 头部字段溢出。"** 头部 `total-bytes` 是 4 字节无符号整数,最大可表示 4 GB,但 Redis 选了 1 GB 作硬上限,留了 4 倍裕度。这是一个"防呆设计"——哪怕业务拼命往里塞,也不会让 listpack 长到不可控。

实际生产中,listpack 远到不了这个上限:每条 entry 的最大尺寸由 `list-max-listpack-size` 等阈值控制,集合元素数也被 128/512 等阈值挡住。一旦超阈值,Redis 会把它整体重编码为指针结构(见 6.5 节)。这个硬上限只是最后一道防线,防止极端场景(配置错误、恶意数据)让 listpack 把进程拖垮。

## 6.4 技巧精解①:为什么 listpack 必须取代 ziplist——连锁更新的根因

这是本章的高潮。讲清 listpack 的妙处,必须先讲清它取代的 ziplist 为什么不行。**根子不在某个实现细节,而在一个字段:`prevlen` vs `backlen`。**

ziplist 是 Redis 早期(2.x 起)的紧凑编码方案。它的整体布局和 listpack 几乎一样([ziplist.c:16](../../redis-8.0.2/src/ziplist.c#L16)):

```text
<zlbytes> <zltail> <zllen> <entry> <entry> ... <entry> <zlend>
   4字节    4字节    2字节                              1字节
```

头部 11 字节(4+4+2+1,比 listpack 多一个 `zltail` 末尾偏移,因为 ziplist 反向遍历的代价更高,需要这个偏移快速定位末尾)。

真正的差别在 entry 结构。ziplist 的 entry 是([ziplist.c:47](../../redis-8.0.2/src/ziplist.c#L47)):

```text
<prevlen> <encoding> <entry-data>
```

注意第一个字段 `prevlen`——**前一个 entry 的长度,不是自己的长度**。源码注释 [ziplist.c:41](../../redis-8.0.2/src/ziplist.c#L41) 原文:"the length of the previous entry is stored to be able to traverse the list from back to front."(为了能反向遍历,存储前一个 entry 的长度)。这正是原罪所在。

### 6.4.1 prevlen 的编码规则与定时炸弹

`prevlen` 的编码规则([ziplist.c:55](../../redis-8.0.2/src/ziplist.c#L55),宏在 [ziplist.c:195](../../redis-8.0.2/src/ziplist.c#L195)):

```c
/* ziplist.c:194-200 */
#define ZIP_END 255         /* Special "end of ziplist" entry. */
#define ZIP_BIG_PREVLEN 254 /* ZIP_BIG_PREVLEN - 1 is the max number of bytes of
                               the previous entry, for the "prevlen" field prefixing
                               each entry, to be represented with just a single byte.
                               Otherwise it is represented as FE AA BB CC DD, where
                               AA BB CC DD are a 4 bytes unsigned integer
                               representing the previous entry len. */
```

翻译:
- **前一个 entry 长度 < 254 字节**:用 **1 字节**存;
- **前一个 entry 长度 ≥ 254 字节**:用 **5 字节**(0xFE 前缀 + 4 字节小端整数)存。

看似无害的规则,埋下了一颗定时炸弹。设想这样的场景:ziplist 里有一连串 entry,每个 entry 自身大小都**恰好是 250~253 字节**。每个 entry 的 `prevlen` 字段都是 1 字节(因为前一个 entry < 254)。

现在,在中间某个位置插入或修改一个 entry,让它的长度从 253 变成 254 字节。它后面的那个 entry 的 `prevlen` 必须从 1 字节扩到 5 字节——**entry 自身变长了 4 字节**,从 253 变成 257。于是**再下一个 entry** 的 `prevlen` 又要扩到 5 字节(因为它的前一个 entry 现在 257 ≥ 254)……以此类推,**整条链一路炸到底**:

```text
连锁更新前的 ziplist(每个 entry 253 字节,prevlen 1 字节):
  ┌──────┬──────┬──────┬──────┬──────┐
  │ E[0] │ E[1] │ E[2] │ E[3] │ ...  │   每个 entry = 253 字节,prevlen=1
  └──────┴──────┴──────┴──────┴──────┘

  在 E[1] 处插入新数据,让 E[1] 从 253 变成 257 字节(假设是修改):
  
  ┌──────┬──────────┬──────┬──────┬──────┐
  │ E[0] │ E[1]'    │ E[2] │ E[3] │ ...  │   E[1]' 现在 257 字节
  └──────┴──────────┴──────┴──────┴──────┘
                      ↑
            E[2] 的 prevlen 必须改成 5 字节(因为前一个 entry 现在 257 ≥ 254)
            → E[2] 自身从 253 变成 257
            
  ┌──────┬──────────┬──────────┬──────┬──────┐
  │ E[0] │ E[1]'    │ E[2]'    │ E[3] │ ...  │   E[2]' 也 257 了
  └──────┴──────────┴──────────┴──────┴──────┘
                                ↑
                      E[3] 的 prevlen 也得改 → E[3]' 257 ...
                      
  ......  级联传播到底,整条 ziplist 全部重写
```

这就是 Redis 文档里反复警告的**连锁更新(cascade update)**。源码里的 `__ziplistCascadeUpdate`([ziplist.c:750](../../redis-8.0.2/src/ziplist.c#L750))就是专门用来收拾这个烂摊子的。它前面的注释([ziplist.c:730-749](../../redis-8.0.2/src/ziplist.c#L730))写得明明白白,关键句在 [ziplist.c:736-737](../../redis-8.0.2/src/ziplist.c#L736):

> *"...This effect may cascade throughout the ziplist when there are consecutive entries with a size close to ZIP_BIG_PREVLEN, so we need to check that the prevlen can be encoded in every consecutive entry."*

注释里还有一句格外值得品味([ziplist.c:742-746](../../redis-8.0.2/src/ziplist.c#L742),反向收缩被刻意忽略):

> *"Note that this effect can also happen in reverse, where the bytes required to encode the prevlen field can shrink. This effect is deliberately ignored, because it can cause a 'flapping' effect where a chain prevlen fields is first grown and then shrunk again after consecutive inserts. Rather, the field is allowed to stay larger than necessary..."*

意思是:反过来(prevlen 从 5 字节可以缩回 1 字节)也会发生,但**故意不缩**,因为反复缩放会让链"扑动"(flapping),每次插入都来回调整,代价更大。所以 ziplist 容忍 prevlen 字段"虚胖"——能大不能小。这是一个**反过来印证连锁更新有多烦人**的注释:作者宁可让字段一直占 5 字节,也不愿处理缩回的级联。

最坏情况下的复杂度:**N 个 entry 全部需要重写,每次重写引发一次 `memmove`,总共 O(N²)**。虽然实际触发概率不高(需要精心构造的边界条件——一长串 250~253 字节的连续 entry),但一旦命中,延迟会出现毛刺,对一个号称亚毫秒级的内存数据库是致命的。这就是 Redis 文档反复警告的"cascade update 是 ziplist 的 known pathology"。

### 6.4.2 listpack 的根治:字段定义层面消灭病灶

listpack 的根治办法简单粗暴却彻底:**每个 entry 只记自己的长度(backlen),不依赖前一个 entry 的任何字段**。修改一个 entry 不影响任何其他 entry 的元数据,根本不存在连锁的可能。

把两种 entry 结构放一起对比:

```text
  ziplist entry(向前看):                  listpack entry(向自己看):
  ┌─────────┬──────────┬───────────┐     ┌────────────────┬─────────┐
  │ prevlen │ encoding │ entry-data│     │ encoding+data  │ backlen │
  │(前一    │          │           │     │(自己的编码+数据)│(自己的 │
  │ entry   │          │           │     │                │ 总长度)│
  │ 的长度) │          │           │     │                │         │
  └─────────┴──────────┴───────────┘     └────────────────┴─────────┘
       ↑                                          ↑
   连锁根源:前 entry 变长,         安全:改自己只动自己,
   自己的 prevlen 也得变,          backlen 只描述自己,
   自己变长又让后者的 prevlen变,   不影响任何别的 entry
   级联传播到底
```

**这就是从"向前看"改成"向自己看"的一字之差**。listpack 的 backlen 存的是"我自己的长度",它和前一个 entry 的长度完全无关。所以在 listpack 里改一个 entry:
- 改自己:只动自己的字节(encoding/data/backlen);
- 前一个 entry:它的 backlen 仍正确,因为它描述的是它自己的长度;
- 后一个 entry:它的 backlen 也仍正确,因为它描述的也是它自己的长度。

**没有任何其他 entry 的元数据需要更新**。连锁更新的传播条件(后继 entry 的 prevlen 依赖于前驱的长度)从字段定义上就不存在了。

注意:修改 entry 仍可能引发**单次 memmove**(如果新 entry 长度变了,需要把后续字节整体平移),但**只有这一次 memmove,不会级联**——因为后续 entry 的 backlen 描述的是它们自己的长度,不依赖被改的 entry。复杂度从 O(N²) 退回到 O(N)(单次 memmove 是 O(N),N 是字节数)。**最坏情况被消灭在数据结构设计层面**,而不是靠算法补丁。

> **钉死这件事**:listpack 取代 ziplist,根子是字段定义的一字之差——`prevlen`(向前看)换成 `backlen`(向自己看)。ziplist 改一个 entry 会让后继的 prevlen 失效,级联传播 O(N²);listpack 改一个 entry,后继的 backlen 仍正确,只单次 memmove。**复杂度不消灭,只转移——listpack 把连锁更新从"修改路径"转移到了根本不存在的地方**。这是"用结构换算法"的典范:不是去优化 cascade update 的实现,而是让产生它的条件不存在。

### 6.4.3 演进史:从 ziplist 到 listpack 的内部换血

这场换血发生在 Redis 7.0。hash、set、zset、stream 的小编码全部从 ziplist 切到了 listpack。证据在源码里随处可见——最直接的是 `OBJ_ENCODING_ZIPLIST` 的定义([server.h:985](../../redis-8.0.2/src/server.h#L985)):

```c
/* server.h:980-992 */
#define OBJ_ENCODING_RAW 0     /* Raw representation */
#define OBJ_ENCODING_INT 1     /* Encoded as integer */
#define OBJ_ENCODING_HT 2      /* Encoded as hash table */
#define OBJ_ENCODING_ZIPMAP 3  /* No longer used: old hash encoding. */
#define OBJ_ENCODING_LINKEDLIST 4 /* No longer used: old list encoding. */
#define OBJ_ENCODING_ZIPLIST 5 /* No longer used: old list/hash/zset encoding. */
#define OBJ_ENCODING_INTSET 6  /* Encoded as intset */
#define OBJ_ENCODING_SKIPLIST 7  /* Encoded as skiplist */
#define OBJ_ENCODING_EMBSTR 8  /* Embedded sds string encoding */
#define OBJ_ENCODING_QUICKLIST 9 /* Encoded as linked list of listpacks */
#define OBJ_ENCODING_STREAM 10 /* Encoded as a radix tree of listpacks */
#define OBJ_ENCODING_LISTPACK 11 /* Encoded as a listpack */
#define OBJ_ENCODING_LISTPACK_EX 12 /* Encoded as listpack, extended with metadata */
```

`OBJ_ENCODING_ZIPLIST 5 /* No longer used */` 这一行注释,既是历史标记,也是设计警示。用 Grep 全 `src/` 扫一遍 `OBJ_ENCODING_ZIPLIST`,**全源码树里只有这一处定义,没有任何代码引用它**——彻底废弃。作为对比,`OBJ_ENCODING_LISTPACK 11` 在 aof.c、db.c、debug.c、defrag.c、geo.c、module.c、object.c、t_hash.c、t_list.c、t_zset.c 等大量文件里广泛使用。

更微妙的证据在配置项别名。看 config.c 里这组配置([config.c:3173](../../redis-8.0.2/src/config.c#L3173) 等):

```c
createSizeTConfig("hash-max-listpack-entries", "hash-max-ziplist-entries", ...)
createSizeTConfig("hash-max-listpack-value",   "hash-max-ziplist-value",   ...)
createSizeTConfig("zset-max-listpack-entries", "zset-max-ziplist-entries", ...)
createSizeTConfig("zset-max-listpack-value",   "zset-max-ziplist-value",   ...)
createIntConfig("list-max-listpack-size",      "list-max-ziplist-size",    ...)
```

每个 `*-max-listpack-*` 配置项都保留了一个 `*-max-ziplist-*` **别名**——这是兼容老配置文件的贴心,也是 listpack 是 ziplist 继任者的隐式印记。8.0 还新增了 `OBJ_ENCODING_LISTPACK_EX 12`(带过期元数据的 listpack 变体,用于 hash 的字段级 TTL),说明 listpack 还在继续演化。

诚实标注一点:Redis 8.0.2 的 listpack.c 和 ziplist.c 文件头**都没有写"7.0 引入 listpack 替代 ziplist"的版本演进注释**——上面这些是从编码常量废弃(`No longer used`)、配置别名(`*-max-ziplist-*` 作 `*-max-listpack-*` 的别名)、源码引用分布(ziplist 编码零引用、listpack 大量引用)三处证据交叉印证出来的。具体版本号"7.0"需要查 Redis 官方 changelog,源码注释里没有直接说明。

> **钉死这件事**:listpack 取代 ziplist 不是某个细节优化,而是一场从字段定义(`prevlen`→`backlen`)开始的、覆盖 hash/set/zset/stream 全部小编码的内部换血。`OBJ_ENCODING_ZIPLIST 5 /* No longer used */` 这行注释是这场换血的墓碑。**有些设计债只能靠推倒重来还**——antirez 没有去优化 cascade update 的实现,而是从结构层面消灭它的产生条件,然后用一个新数据结构把老数据结构整个换掉。这是取向⑤(可靠性)在数据结构层的一次硬决断。

## 6.5 技巧精解②:编码切换的阈值表——"小用紧凑,大用专业"

讲完三种结构,把它们的取舍收敛成几条原则,然后看 Redis 用什么阈值划定紧凑编码和指针结构的边界。

### 6.5.1 三种结构的取舍收敛

**第一,intset 用"统一编码 + 升级"换极简。** 整数集合没有变长编码、没有自描述头,每个元素就是定长的几个字节。代价是:小整数也按大编码存,有冗余(一旦升级到 int64,所有元素都 8 字节);但换来的是**连续数组 + 二分查找**,缓存行完美命中,代码也极简。这是"特殊场景做绝"的取舍——纯整数场景足够常见(计数器、ID 集合、白名单),值得专门一套结构。

**第二,listpack 用"变长编码 + backlen 自描述"换通用。** 字符串长度千差万别,listpack 用 6/12/32-bit 三档长度编码 + 7/13/16/24/32/64-bit 六档整数编码,把每个字节都榨干(常见值 1 字节,罕见值付全价)。代价是:查找必须线性扫描(O(N));修改可能引发单次 memmove(O(N))。但 N 受阈值控制,实际很小,线性扫描的总开销低于一次哈希表的多次指针跳转加 cache miss。

**第三,紧凑布局与指针结构的边界由阈值精确划定。** 看 `redis.conf` 的默认值([redis.conf:1959](../../redis-8.0.2/redis.conf#L1959) 起):

```text
hash-max-listpack-entries 512       # 1959
hash-max-listpack-value 64          # 1960
list-max-listpack-size -2           # 1975 (-2 表示每个 list 节点最大 8 Kb)
set-max-intset-entries 512          # 1998
set-max-listpack-entries 128        # 2004
set-max-listpack-value 64           # 2005
zset-max-listpack-entries 128       # 2010
zset-max-listpack-value 64          # 2011
```

config.c 的 Default 定义([config.c:3173](../../redis-8.0.2/src/config.c#L3173) 等)与 redis.conf 一致。每个数字都是经验调优的结果。来读一遍:

- `hash` 允许 512 个元素——hash 通常字段多,放宽一些;
- `set` 和 `zset` 只允许 128 个——它们后续要换更复杂的结构(dict / skiplist),过渡阈值要更保守;
- `value` 上限统一是 64 字节:超过这个长度的单元素本身就不"小"了,继续塞 listpack 收益递减;
- `set-max-intset-entries 512`:纯整数 set 的专属阈值,512 个 int64 = 4 KB,刚好是常见 cache 大小的一两倍,二分查找代价可接受。

这些阈值在源码里的实际触发点(用 `>` 严格大于,即"超过阈值才换"):

- **hash(listpack → hashtable)**:[t_hash.c:901-905](../../redis-8.0.2/src/t_hash.c#L901) 在 `hashTypeSet` 里检查 `sdslen(field) > server.hash_max_listpack_value || sdslen(value) > server.hash_max_listpack_value`,超长就 `hashTypeConvert(o, OBJ_ENCODING_HT, ...)`;[t_hash.c:932-934](../../redis-8.0.2/src/t_hash.c#L932) 写入后检查 `hashTypeLength(o, 0) > server.hash_max_listpack_entries`,超量也转。
- **set(intset → listpack → hashtable)**:[t_set.c:65-66](../../redis-8.0.2/src/t_set.c#L65) `intsetLen > intsetMaxEntries()`(基于 `server.set_max_intset_entries`)时转 hashtable;[t_set.c:147-161](../../redis-8.0.2/src/t_set.c#L147) 在 listpack 编码里检查元素数和单值长度,超限调 `setTypeConvertAndExpand(set, OBJ_ENCODING_HT, ...)`。
- **zset(listpack → skiplist)**:[t_zset.c:1462-1468](../../redis-8.0.2/src/t_zset.c#L1462) 在 `zsetAdd` 里预判 `zzlLength(zobj->ptr)+1 > server.zset_max_listpack_entries || sdslen(ele) > server.zset_max_listpack_value`,超限调 `zsetConvertAndExpand(zobj, OBJ_ENCODING_SKIPLIST, ...)`。
- **list(单 listpack → quicklist)**:list 走的是另一条路——它没有"单 listpack"编码,小 list 直接用 quicklist(每个 quicklist 节点是一个小 listpack)。`list-max-listpack-size` 由 `listTypeTryConvertListpack`([t_list.c:24](../../redis-8.0.2/src/t_list.c#L24))通过 `quicklistNodeExceedsLimit(server.list_max_listpack_size, ...)` 判断单节点是否超限。

注意 8.0 还有一个细节:zset 的 `zsetConvertToListpackIfNeeded`([t_zset.c:1333](../../redis-8.0.2/src/t_zset.c#L1333))会**反向**把 skiplist 缩回 listpack(用于 ZUNIONSTORE 等命令后,如果结果集合又变小了)。这是"编码自适应"双向流动的一个体现——和 intset 的"只升不降"形成有趣对比:listpack/skiplist 的转换是双向的,intset 的转换是单向的。为什么?因为 listpack↔skiplist 是两种成熟结构的互换,代价可控;而 intset↔intset(降级)需要全表扫描找最大值,代价高、收益低。

### 6.5.2 紧凑编码牺牲了什么

必须讲清换取的代价:

- **写入是 O(N)**(intset/listpack):每次插入都要 `realloc` + `memmove`,因为内存连续。指针结构(dict)插入是 O(1)。Redis 接受这个代价,因为紧凑编码只在 N 很小时启用。
- **查找是 O(N)**(listpack)或 O(log N)(intset):hash 表是 O(1)。同样靠阈值兜底——N 上限 128/512,最坏 512 次比较也就微秒级。
- **不支持随机修改的高效路径**:改一个元素可能改变其编码长度(比如把 value 从 "5" 改成 "hello world",从 7-bit 整数编码变成 12-bit 字符串编码),entry 变长,引发后续 memmove。

这些代价被"小集合"这个前提吸收掉了。一旦集合变大、跨越阈值,Redis 会把它整体**重编码**(re-encode)成指针结构,一劳永逸。这就是第九章"编码自适应"要展开的主题。

> **钉死这件事**:紧凑编码的代价是写入/查找都退化为 O(N)/O(log N),靠"N 受阈值控制"吸收。阈值的设定是 Redis 工程师长期调优的经验值——hash 512、set/zset 128、value 64 字节——每个数字背后都是"再大就 O(N) 扛不住、再小就省得不够"的权衡。**这是取向③(编码自适应)的入口:小用紧凑结构省内存,大用专业结构保性能,边界由阈值精确划定**。

## 章末:回扣、五个为什么、往哪钻

### 主线回扣

这一章是**取向②(内存即数据库)+ 取向④(简单优先)+ 取向③(编码自适应)的三方交汇**。

- **取向②(内存即数据库)** 是本章的根。Redis 不像关系库可以把数据堆在磁盘上慢慢捞,每一字节都在内存里、都在计费。所以哪怕是一个只有两个字段的小 hash,也要为它专门设计 listpack 这种"压扁"的容器。小集合的紧凑化是 Redis 在"内存即数据库"这条路上的硬功夫——别人觉得不值得做的内存优化,Redis 做到极致。一个 7-bit 整数 1 字节、backlen 1 字节、总共 2 字节装下一个 entry,这是把每个字节都榨干的执念。

- **取向④(简单优先)** 体现在两处。intset 是典范:能用一个有序数组解决的,绝不引入树;能用一个统一编码的,绝不引入异构编码。listpack 也是:能用变长字节解决的,绝不引入指针。**简单不只是哲学,它带来更少的 bug、更好的缓存行为、更可预测的延迟**。尤其是 listpack 取代 ziplist——antirez 没有去优化 cascade update 的实现,而是从字段定义上消灭它的产生条件。**用结构换算法,用简单换可靠**,这是取向④的最高表达。

- **取向③(编码自适应)** 是本章的出口。紧凑编码不是 Redis 的全部,而是它的一种"形态"。集合小的时候用紧凑编码省内存,大了自动切换到指针结构保性能。阈值的设定、切换的时机、`OBJ_ENCODING_LISTPACK` / `OBJ_ENCODING_INTSET` / `OBJ_ENCODING_HT` / `OBJ_ENCODING_SKIPLIST` 这些编码常量的流转([server.h:986](../../redis-8.0.2/src/server.h#L986)、991 等),构成 Redis 区别于其他数据结构库的独特灵魂。本章给出阈值表,正是为第九章的编码自适应总览埋下锚点。

- **取向⑤(可靠性)** 也在场。ziplist 的连锁更新是个反面教材,但 Redis 选择**彻底重写**而不是修补——从结构层面消灭问题源。listpack 不向后兼容 ziplist 的字节布局(虽然 RDB 加载时仍能识别旧格式),这是为长期可靠性付出的迁移成本。源码里保留 `OBJ_ENCODING_ZIPLIST 5 /* No longer used */` 这一行,既是历史标记,也是设计警示。

### 五个为什么

**Q1:既然 listpack 解决了 ziplist 的连锁更新,为什么 intset 不也用"自描述长度"的设计,反而坚持统一编码?**
因为 intset 是**定长**的(每个元素占的字节数固定,由 encoding 决定),根本没有"前一个 entry 长度变化"的问题——所有 entry 都一样长。统一编码让 intset 可以用纯数组 + 二分查找,缓存完美命中,代码不到 200 行。变长自描述(listpack 那套)是为"长度千差万别"的场景准备的,intset 的场景用不上。**数据结构没有"最好的",只有"最适合场景的"**。

**Q2:13-bit 整数为什么范围是 -4096~4095,不是对称的 -4095~4095?**
因为 13 位有符号整数的可表示范围就是 -4096~4095(2¹² = 4096 个负数 + 4095 个非负数 + 零,或者按二进制补码看:-2¹² ~ 2¹²-1)。listpack 把负数 `v` 转无符号余数的代码 `v = ((int64_t)1<<13)+v`([listpack.c:256](../../redis-8.0.2/src/listpack.c#L256))就是这个转换——负数加 2¹³ 落到正数区间。这不是 Redis 的设计,是补码表示的自然结果。

**Q3:backlen 反向变长编码的"首字节最高位 0、其余字节最高位 1"看起来很绕,为什么不直接用常规 varint?**
因为常规 varint 是为"从前往后读"优化的——首字节最高位 1 表示"还有后续",0 表示"这是最后一字节"。这种方向对正向遍历友好,但反向遍历就麻烦了:从尾部往前读时,第一个碰到的字节是 varint 的末字节,它的最高位可能是 0(如果是单字节 varint)或 1(如果是多字节 varint 的中间字节),解码器**无法立刻判断 backlen 总共有几个字节**,得试探着往前读。listpack 把方向反过来后,**末字节的最高位一定是 1**(多字节情况下)或 0(单字节情况下),反向解码时一读到最高位 0 就知道结束了——逻辑干净。代价是正向遍历如果想读 backlen(实际上不需要,正向靠 `lpSkip` 直接累加 entry 长度),会稍微绕一点点。但 listpack 的双向遍历代价由此完全对称,值。

**Q4:intset"只升不降"会不会导致内存浪费?比如先插一个大数触发 int64 升级,然后删掉它,剩下全是小整数却仍按 int64 存?**
会。这是 intset 已知的代价。但实际触发概率不高:用户主动 `SREM` 删掉那个大数,然后集合就只剩小整数——这种"先大后小"的访问模式在实际业务里少见。更常见的是集合元素单调增长(只增不删,或删的是平均大小的元素)。Redis 选择不为这种少见场景付出"每次删除都要 O(N) 扫描看能否降级"的高频代价。**复杂度守恒:把代价留给少见时刻**。如果业务真的反复插大数又删大数,用户可以手动 `SINTERSTORE` 把集合重写一次(会重新选最小编码),代价可控。

**Q5:listpack 和 LevelDB 的 memtable(跳表 + MemTable)都追求"小而紧凑",它们的设计哲学有什么本质区别?**
两个方向。listpack 是"无指针、自描述、连续内存"——把所有元数据做进编码前缀和 backlen,追求极致的内存利用率,代价是 O(N) 查找。LevelDB 的 memtable 用 skiplist(带指针),追求 O(log N) 查找和并发友好(无锁读),代价是每个节点都有指针开销和缓存不友好。**listpack 是"小到极致的容器"(N 受阈值控制),memtable 是"中等规模的有序索引"(N 可能到几 MB)**。它们的共同点是"为访问模式量身定制"——listpack 为"小集合的全字段扫"优化,memtable 为"中等集合的随机读写"优化。本系列《LevelDB 设计与实现深入浅出》的 SkipList 章会从另一个角度讲同一个家族的取舍。

### 想继续深入往哪钻

- 想看 listpack 的完整 API:`lpNew`/`lpAdd`/`lpInsert`/`lpDelete`/`lpReplace`/`lpFind`/`lpLength`/`lpBytes`,读 [listpack.c](../../redis-8.0.2/src/listpack.c) 的实现。重点看 `lpInsert`([listpack.c:950 附近](../../redis-8.0.2/src/listpack.c#L950))如何处理"新 entry 长度变化引发的 memmove"——你会看到只有一次 memmove,没有级联。
- 想理解 listpack 在 stream 里的特殊用法:读 [t_stream.c](../../redis-8.0.2/src/t_stream.c),stream 用 rax(基数树,见第九章)做外层索引,每个叶子节点挂一个 listpack 存一批 stream entry。这是 listpack "中等规模"用法的代表。
- 想看 ziplist 的完整 cascade update 实现:读 [ziplist.c:730-846](../../redis-8.0.2/src/ziplist.c#L730),重点看两趟循环——第一趟累计 `extra`(总扩容字节数)、第二趟 `memmove` 腾挪 + 反向回填 prevlen。注释里那段"flapping"警告([ziplist.c:742-746](../../redis-8.0.2/src/ziplist.c#L742))格外值得读。
- 想理解为什么 LevelDB 的 skiplist 选了另一条路(无锁并发读,而不是紧凑编码):看本系列《LevelDB 设计与实现深入浅出》的 SkipList 章,对比"内存数据库的小集合容器"和"持久化数据库的内存索引"在设计取向上的根本差异。

### 引出下一章

至此你看清了"单块连续内存"的紧凑编码:listpack(变长字符串/整数)、intset(纯整数)。但 list 类型有个尴尬:它既要支持两端 O(1) 的 PUSH/POP,又可能存海量元素(消息队列场景),单块 listpack 扛不住(`LISTPACK_MAX_SAFETY_SIZE = 1<<30`,约 1 GB,见 [listpack.c:123](../../redis-8.0.2/src/listpack.c#L123),但单块太大每次写入都重 alloc,延迟不可接受)。

Redis 的解法是把多个小 listpack 用双向链表串起来——这就是 `quicklist`。每个 quicklist 节点是一个受 `list-max-listpack-size` 控制的小 listpack,节点之间用指针连接。它融合了 listpack 的内存紧凑(每个节点内部)和链表的两端高效操作(节点级),还支持中间节点按需 LZF 压缩。

紧凑编码到了这里,从"一块"长成了"一串"——这是下一章《quicklist:把 listpack 串成链》要讲的故事。而 quicklist 节点大小的选择、压缩深度的调优,又将再次回扣本章的取舍主题:**连续 vs 指针、紧凑 vs 灵活,永远是数据结构设计的主旋律**。

---

## 验证物:如何亲手确认本章的设计

> 说明:本书写作环境为 Windows,无法直接运行 redis-server(8.0 依赖 fork/epoll 等 Linux 特性)。以下 (1) gdb 断点脚本 (2) 源码常量锚点 (3) OBJECT ENCODING 观察项 均为可复现的精确指引,供读者在 Linux 环境(Ubuntu 22.04 / CentOS 8 等)对 redis-8.0.2 源码 `make no-opt` 编译后自行验证。**本书不附编造的运行输出**——凡未实跑的,只给脚本与预期观察变量,不写具体数值。

### 1. gdb 断点脚本

编译:`cd redis-8.0.2 && make no-opt`(带 -g)
启动:`gdb ./src/redis-server`,另一终端 `redis-cli`。

```gdb
(gdb) break lpEncodeIntegerGetType      # 整数编码判定,listpack.c:251
(gdb) break lpEncodeBacklen             # backlen 编码,listpack.c:341
(gdb) break lpDecodeBacklen             # backlen 反向解码,listpack.c:397
(gdb) break lpPrev                      # 反向遍历,listpack.c:505
(gdb) break lpNext                      # 正向遍历,listpack.c:494
(gdb) break intsetUpgradeAndAdd         # intset 升级,intset.c:159
(gdb) break intsetSearch                # intset 二分查找,intset.c:117
(gdb) break intsetRemove                # 确认无降级,intset.c:235
(gdb) break __ziplistCascadeUpdate      # ziplist 连锁更新(老路径),ziplist.c:750
(gdb) run --port 6379

# 验证 1:HSET 一个小 hash,观察 listpack 编码
# redis-cli: HSET smallhash f1 v1
# (gdb 在 lpEncodeIntegerGetType 不一定停,因为 "v1" 不是整数;改用数字 value)
# redis-cli: HSET smallhash counter 5
(gdb) print v                           # 预期:5(被 lpStringToInt64 解析成整数)
(gdb) print *enclen                     # 预期:1(7-bit 整数编码)
(gdb) continue
(gdb) print intenc[0]                   # 预期:5(前缀位 0 + 数据 5,即 0x05)

# 验证 2:intset 升级
# redis-cli: SADD intset_only 1 2 3      # 全小整数,int16 编码
# redis-cli: OBJECT ENCODING intset_only  # 预期:intset
# redis-cli: SADD intset_only 2147483648 # 触发 int32 升级(超过 int16 范围)
(gdb) 在 intsetUpgradeAndAdd 停下:
(gdb) print curenc                       # 预期:2(INTSET_ENC_INT16)
(gdb) print newenc                       # 预期:4(INTSET_ENC_INT32)
(gdb) print prepend                      # 预期:0(2147483648 是正数,塞末尾)
# 单步进入 while(length--) 循环,观察从后往前搬运

# 验证 3:lpPrev 的反向遍历(需在已有多元素 listpack 上)
# redis-cli: HSET prevtest a 1 b 2 c 3
# redis-cli: HGETALL prevtest            # 内部走 lpNext 正向扫
# 要触发 lpPrev,需用反向遍历命令(如 LRANGE 带 -1/-2 在 quicklist 上,或某些 hash 内部操作)
# 本书未实跑,建议读者自行找触发点
```

**预期观察**(基于源码 [listpack.c:251](../../redis-8.0.2/src/listpack.c#L251) 和 [intset.c:159](../../redis-8.0.2/src/intset.c#L159),本书未实跑):
- `HSET ... counter 5` 时,`lpEncodeIntegerGetType` 收到 `v=5`,返回 `enclen=1`,写入字节 `0x05`(7-bit 整数);
- `SADD ... 2147483648` 时,`intsetUpgradeAndAdd` 的 `curenc=2`、`newenc=4`、`prepend=0`,然后 `while(length--)` 从 `length-1` 递减到 0 严格反向搬。

### 2. 源码常量锚点(带行号,从 redis-8.0.2 源码 Grep 核实)

| 常量/字段 | 位置 | 值/说明 |
|----------|------|---------|
| `LP_HDR_SIZE` | listpack.c:27 | 6(32 bit total len + 16 bit num elements) |
| `LP_EOF` | listpack.c:76 | 0xFF(结束标记) |
| `LP_ENCODING_7BIT_UINT` | listpack.c:34 | 0(前缀位模式 `0xxxxxxx`,范围 0~127) |
| `LP_ENCODING_13BIT_INT` | listpack.c:43 | 0xC0(前缀位模式 `110xxxxx`,范围 -4096~4095) |
| `LP_ENCODING_16BIT_INT` | listpack.c:52 | 0xF1(前缀位模式 `11110001`) |
| `LP_ENCODING_64BIT_INT` | listpack.c:67 | 0xF4(前缀位模式 `11110100`) |
| `LISTPACK_MAX_SAFETY_SIZE` | listpack.c:123 | 1<<30(约 1 GB,listpack 总字节硬上限) |
| `lpEncodeIntegerGetType` | listpack.c:251 | 整数编码判定(7/13/16/24/32/64 六档) |
| `lpEncodeGetType`(非 lpEncodeStringGetType) | listpack.c:323 | 整数+字符串统一判定 |
| `lpEncodeBacklen` | listpack.c:341 | backlen 反向变长编码(1~5 字节) |
| `lpDecodeBacklen` | listpack.c:397 | backlen 反向解码 |
| `lpPrev` | listpack.c:505 | 反向遍历(p--→lpDecodeBacklen→偏移) |
| `lpNext` | listpack.c:494 | 正向遍历(lpSkip 累加 entry 长度) |
| `intset` 结构 | intset.h:35-39 | encoding/length/contents[] 三字段,8 字节头 |
| `INTSET_ENC_INT16/32/64` | intset.c:41-43 | 2/4/8(sizeof(int16/32/64_t)) |
| `intrev32ifbe` 宏 | endianconv.h:33/40 | 小端 no-op,大端字节反转(不在 intset.h) |
| `intsetUpgradeAndAdd` | intset.c:158-182 | 升级编码,从后往前搬,prepend 判定 |
| `intsetSearch` | intset.c:117-156 | 二分查找 + 两端边界剪枝 |
| `intsetRemove` | intset.c:235-253 | **无降级逻辑**(只升不降) |
| ziplist 整体布局注释 | ziplist.c:16 | `<zlbytes><zltail><zllen>...<zlend>`,头部 11 字节 |
| ziplist entry 结构注释 | ziplist.c:47 | `<prevlen><encoding><entry-data>` |
| `ZIP_BIG_PREVLEN` | ziplist.c:195 | 254(prevlen 1 字节 vs 5 字节的阈值) |
| cascade 注释(原句) | ziplist.c:736-737 | "This effect may cascade throughout the ziplist..." |
| flapping 警告 | ziplist.c:742-746 | "deliberately ignored"(反向收缩被刻意忽略) |
| `__ziplistCascadeUpdate` | ziplist.c:750-846 | 连锁更新收拾函数(两趟循环) |
| `OBJ_ENCODING_ZIPLIST` | server.h:985 | 5,`/* No longer used */`,全 src/ 仅此一处定义零引用 |
| `OBJ_ENCODING_INTSET` | server.h:986 | 6 |
| `OBJ_ENCODING_LISTPACK` | server.h:991 | 11 |
| `OBJ_ENCODING_LISTPACK_EX` | server.h:992 | 12(8.0 新增,带过期元数据) |
| 紧凑编码阈值(redis.conf) | redis.conf:1959-2011 | hash 512/64,set-intset 512,set-lp 128/64,zset 128/64,list-size -2 |

### 3. OBJECT ENCODING 观察项(需本地 redis-server)

> 以下操作需在 Linux 本地启动 redis-server 后用 redis-cli 执行。本书未实跑,仅列观察方法与预期切换点(阈值来自 [redis.conf:1959-2011](../../redis-8.0.2/redis.conf#L1959) 默认值,可 `CONFIG GET` 确认)。

```text
# 观察 1:intset 编码(set 全整数时)
127.0.0.1:6379> CONFIG GET set-max-intset-entries      # 预期 512
127.0.0.1:6379> DEL myset
127.0.0.1:6379> SADD myset 1 2 3                        # 全小整数
127.0.0.1:6379> OBJECT ENCODING myset                   # 预期 intset
127.0.0.1:6379> SADD myset "not_a_number"               # 插入非整数
127.0.0.1:6379> OBJECT ENCODING myset                   # 预期 listpack(≤128 元素且 ≤64 字节)

# 观察 2:listpack → hashtable 切换(hash)
127.0.0.1:6379> CONFIG GET hash-max-listpack-entries    # 预期 512
127.0.0.1:6379> DEL myhash
127.0.0.1:6379> HSET myhash f1 v1                       # 1 字段
127.0.0.1:6379> OBJECT ENCODING myhash                  # 预期 listpack
# 循环 HSET 到 513 个字段(超过阈值 512,见 t_hash.c:932-934):
127.0.0.1:6379> OBJECT ENCODING myhash                  # 预期 hashtable

# 观察 3:listpack → skiplist 切换(zset)
127.0.0.1:6379> CONFIG GET zset-max-listpack-entries    # 预期 128
127.0.0.1:6379> DEL myzset
127.0.0.1:6379> ZADD myzset 1 a                         # 1 元素
127.0.0.1:6379> OBJECT ENCODING myzset                  # 预期 listpack
# 循环 ZADD 到 129 元素(超过阈值 128,见 t_zset.c:1462-1468):
127.0.0.1:6379> OBJECT ENCODING myzset                  # 预期 skiplist

# 观察 4:单 value 超长触发切换(hash)
127.0.0.1:6379> DEL bigvalhash
127.0.0.1:6379> HSET bigvalhash k1 $(head -c 100 /dev/urandom | base64)  # value > 64 字节
127.0.0.1:6379> OBJECT ENCODING bigvalhash              # 预期 hashtable(单值超 hash-max-listpack-value=64)
```

标注:以上预期基于源码常量([redis.conf:1959-2011](../../redis-8.0.2/redis.conf#L1959))与 [t_hash.c:901-905](../../redis-8.0.2/src/t_hash.c#L901)、[t_set.c:147-161](../../redis-8.0.2/src/t_set.c#L147)、[t_zset.c:1462-1468](../../redis-8.0.2/src/t_zset.c#L1462) 的转换逻辑推导,本书未在本地实跑;若你的 redis 版本/配置不同,切换点可能偏移,以 `CONFIG GET` 实际值为准。注意 8.0 起 hash 在某些路径下编码可能显示为 `listpack` 而非 `ziplist`(老资料常说 ziplist,已过时)。
