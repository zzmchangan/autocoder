# 第十章 · redisObject 与 8.0 的 keymeta 层:一个值容器和它背后的取舍

> 篇:P3 内存治理
> 主轴呼应:这一章是**取向②(内存即数据库)+ 取向③(编码自适应)的双根**。`redisObject` 把"值"统一封装成 16 字节小盒子、底层可换编码;8.0 新增的 `mstr`(keymeta 抽象)把"字符串 + 可选元数据"压成一次分配。读完本章你会明白:Redis 凭什么让同一个 `SET k v` 在 v 短的时候是 embstr、长的时候是 raw、是数字的时候是 int,而这一切上层命令完全无感。

---

## 读完本章你会明白

1. **为什么 `redisObject` 恰好是 16 字节,不是 8、不是 32**——四个位段(`type:4 + encoding:4 + lru:24`)拼成 32 bit 头、`refcount` 和 `ptr` 各占一字,这个布局是半个缓存行的精算。
2. **为什么短字符串要做成 embstr(robj 和 sds 头挤在同一个分配),而一旦被 `APPEND`/`SETRANGE`/`INCR` 修改就必须转成 raw**——连续内存的好处是省一次 `malloc` 和少一次 cache miss,代价是它"不可改",改一次就崩,所以修改前必须重新分配。
3. **为什么 0 到 9999 这一万个整数要预先做成共享对象,大整数却不共享**——命中率 + 节省的是 robj + sds 头开销,大整数命中率太低、做了反而费内存。
4. **为什么 `refcount` 要有一个 `INT_MAX` 魔法值,而 `decrRefCount` 第一件事就是特判它**——共享对象永不可变、永不释放,`incrRefCount`/`decrRefCount` 对它必须 no-op,这个不变量靠一个魔法值钉死。
5. **为什么 8.0 要发明一个新东西叫 mstr(immutable string with metadata),而不是扩展 sds**——sds 是可变字符串,API 极宽(split/join/cat/catprintf……),把元数据逻辑塞进去会非常脆弱;另起炉灶做"不可变 + 有限 API + 可挂元数据",是为 hash 字段 TTL 也是为未来 keymeta 统一抽象铺路。
6. **为什么 `lru` 字段的 24 bit 在 LRU 模式下是分钟级时钟、在 LFU 模式下却是"高 16 位衰减时间 + 低 8 位对数频率"**——一个 if 切换两种语义,省掉一个 union,但读源码时必须时刻警惕 `o->lru` 该怎么解释。

---

> **如果一读觉得太难:先只记住三件事**——
> ① robj 是 Redis 一切"值"的统一容器,16 字节,装着 `type`(用户视角是什么)+ `encoding`(底层怎么存)+ `lru`(访问热度)+ `refcount`(引用计数)+ `ptr`(数据指针);
> ② 短字符串是 embstr(robj 和 sds 一次连续分配,不可改)、长字符串是 raw(两次分配,可改)、纯数字是 int(`ptr` 直接存值,零分配);任何修改类命令都会把 embstr/int 转成 raw;
> ③ 8.0 的 mstr 是"不可变字符串 + 前挂元数据"的单分配布局,当前唯一在役场景是 hash 字段 TTL,它是未来"key 自带 TTL/LRU/next 指针"统一抽象的雏形。
> 这三件事,就是本章的全部。

---

> **一句话点破:redisObject 的本质不是"封装",而是"把五个语义压成一个固定 16 字节头,用 type/encoding 分离换编码自适应、用 refcount 魔法值换共享对象免锁、用 lru 24 bit 复用换 LRU/LFU 双模";mstr 的本质不是"新字符串",而是"为未来 key 元信息统一收编做的抽象预演——一次分配、按需挂载、不可变换简单换紧凑"。**

前九章我们钻完了五种底层结构——SDS、dict、listpack/intset、quicklist、skiplist、rax。可是上层命令看到的从来不是这些:一条 `SET k v` 进来,Redis 内部既不直接给你 sds、也不给你 skiplist,而是给你一个叫 `redisObject` 的统一盒子,盒子里的 `ptr` 才指向真正的底层结构。**这层盒子是什么、为什么必须有它、它的 16 字节里塞了哪些取舍**,就是本章前半段的主线。后半段我们看 8.0 新引入的 `mstr`——它不是第六种数据结构,而是为"key 元信息"做的底层抽象预演,当前只在 hash 字段 TTL 上服役,却是 Redis 团队在源码注释里直接宣告的"未来 keyspace key 也将变成一种 mstring"的方向。

## 10.1 这块要解决什么:值的形态多样 + 同一逻辑值底层可换

先把问题摆到桌面上。Redis 是个内存数据库,它的"值"形态极其多样:一个字符串、一个列表、一张哈希表、一个集合、一个有序集合,甚至是一个流。如果每种类型各写一套增删改查、各写一套序列化、各写一套内存释放,代码会立刻裂成五条互不相交的河流。

更麻烦的是,**同一份"逻辑值"在底层可以有不同表示**。一个 list 既可以是一条 listpack(元素少时省内存、连续紧凑),也可以是 quicklist(元素多时省操作、分块易改)。一个 hash 既可以 listpack(小)、又可以 ht(大)。一个 zset 既可以 listpack(小)、又可以 dict+skiplist(大)。**上层命令看到的永远是"一个 list"、"一个 hash"、"一个 zset",底层却在悄悄换挡**——而这一切对命令代码必须完全透明。

这两个诉求合起来,逼出了一个统一的值容器——`redisObject`(代码里简称 `robj`)。它的职责很纯粹:**把"这是什么类型"(type)、"底层怎么存的"(encoding)、"还有谁在引用我"(refcount)、"我多久没被访问了"(lru)打包成一个固定大小的小盒子,盒子里那个 `void *ptr` 指向真正的数据**。上层只认 `robj`,下层编码随便换。这就是取向③(编码自适应)能成立的物理基础——**没有 robj 这层中间抽象,编码切换要么裂到每条命令里、要么干脆做不了**。

> **不这样会怎样**:假设不用统一容器,每个命令自己判断"这个值现在是 sds 还是 listpack 还是 hashtable"、自己 dispatch 到对应的处理函数。第一,代码会**到处出现 type/encoding 的 if-else 链**,新增一种编码就要改所有命令;第二,**编码切换无法对上层透明**——一个 hash 在小→大时从 listpack 转 ht,如果上层直接拿着 listpack 指针操作,切换瞬间指针就失效了;第三,**统一引用计数、LRU、序列化都无从谈起**——每条命令自己管内存,bug 满地。robj 这层中间层用一次间接(指针指向真正的底层结构),换来了上层代码的干净和编码切换的透明。

解决了"值的统一"之后,8.0 又把刀对准了"key 的元信息"。在旧版 Redis 里,key 这个字符串和它附带的状态(过期时间、淘汰时钟、引用计数、类型编码)是**散落**的:过期时间塞在 `dictEntry` 里,淘汰时钟塞在 `robj->lru` 里,类型编码塞在 `robj->type/encoding` 里。当一个 hash 字段也想拥有自己的 TTL(hash field expiration,7.4 引入的特性)时,这套"挂在 dictEntry 上"的机制就捉襟见肘了——**hash 字段不是 key,它没有独立的 dictEntry 可挂**。

于是 8.0 引入了一个底层抽象:`mstr`(m-string,immutable string with optional metadata attached)。它是一段**不可变字符串,前面可以按需挂若干块定长元数据,全部在单次内存分配里完成**。Redis 团队在源码注释里直接点明了它的野心([mstr.h:70-100](../../redis-8.0.2/src/mstr.h#L70)):未来 keyspace 的 key 也可以是一种 mstring,把 TTL、LRU、甚至 `dictEntry` 的 next 指针都"内嵌"进去。这就是本章标题里"keymeta 层"的真正落点:**不是把元信息从一个结构搬到另一个结构,而是发明一种"字符串+可选元数据"的单分配紧凑布局,让任何需要携带状态的字符串对象都能复用它**。8.0.2 里它已经实打实地在 hash 字段上服役了。

## 10.2 redisObject:四个位段加一个指针,共 16 字节

先看结构本身,它定义在 `server.h`([server.h:1001-1009](../../redis-8.0.2/src/server.h#L1001)):

```c
/* server.h:1001-1009 */
struct redisObject {
    unsigned type:4;                       /* 类型,4 bit                  */
    unsigned encoding:4;                   /* 编码,4 bit                  */
    unsigned lru:LRU_BITS;                 /* LRU/LFU,24 bit,LRU_BITS=24 */
    int refcount;                          /* 引用计数                     */
    void *ptr;                              /* 真实数据指针                 */
};
```

短短五行,信息量却很密。逐字段拆开看。

`type` 只用 4 bit,因为 Redis 的对象类型屈指可数:`OBJ_STRING`/`OBJ_LIST`/`OBJ_SET`/`OBJ_ZSET`/`OBJ_HASH`(外加 `OBJ_STREAM`、`OBJ_MODULE`),8 种以内,4 bit 够装。

`encoding` 也只用 4 bit,因为它要枚举的是底层编码([server.h:980-992](../../redis-8.0.2/src/server.h#L980)):

```c
/* server.h:980-992 */
#define OBJ_ENCODING_RAW 0        /* robj 和 sds 分开两次分配          */
#define OBJ_ENCODING_INT 1        /* ptr 直接存 long,零分配           */
#define OBJ_ENCODING_HT 2         /* 哈希表                            */
#define OBJ_ENCODING_ZIPMAP 3     /* 已废弃                            */
#define OBJ_ENCODING_LINKEDLIST 4 /* 已废弃                            */
#define OBJ_ENCODING_ZIPLIST 5    /* 已废弃(改名 listpack)            */
#define OBJ_ENCODING_INTSET 6     /* 紧凑整数数组                      */
#define OBJ_ENCODING_SKIPLIST 7   /* dict + skiplist                   */
#define OBJ_ENCODING_EMBSTR 8     /* robj 和 sds 头挤在同一次分配       */
#define OBJ_ENCODING_QUICKLIST 9  /* listpack 组成的链                 */
#define OBJ_ENCODING_STREAM 10    /* rax of listpacks                  */
#define OBJ_ENCODING_LISTPACK 11  /* listpack                          */
#define OBJ_ENCODING_LISTPACK_EX 12 /* listpack + metadata 扩展         */
```

`type` 和 `encoding` 分开,是这一层最关键的设计:**type 回答"用户眼里的它是什么",encoding 回答"内存里的它怎么摆"**。一个 `OBJ_HASH` 在元素少时是 `OBJ_ENCODING_LISTPACK`(连续紧凑布局,缓存友好、指针开销为零),元素多了自动转 `OBJ_ENCODING_HT`(哈希表,查找 O(1) 但每个 entry 都要单独分配)。转换对上层完全透明,命令代码只判断 type,不关心 encoding。这就是取向③的工程落地。

三个位段拼起来:`type:4 + encoding:4 + lru:24` = 32 bit,正好对齐到一个 int 边界;后面 `refcount`(int,4 字节)和 `ptr`(指针,8 字节)各占一个机器字。**整个 robj 在 64 位系统上恰好 16 字节**,这不是巧合,是刻意安排的——16 字节是半个 cache line(64 字节),遍历 dict 时一次 cache line 读能装下两个 robj 的元信息,命中率极高。

```text
redisObject 的 16 字节布局(64 位)
┌────────────────┬────────────────┬─────────────────┬──────────┬────────┐
│  type:4 (bit)  │ encoding:4(bit)│   lru:24 (bit)  │ refcount │  ptr   │
│   类型枚举      │   编码枚举      │  LRU/LFU 双模   │   int    │ void*  │
│   4 bit        │   4 bit        │   24 bit        │  4 byte  │ 8 byte │
└────────────────┴────────────────┴─────────────────┴──────────┴────────┘
←────── 头 4 字节(三种语义压一处)──────→←── 各 1 字 ──→
←────────────────────────── 共 16 字节(半个 cache line)───────────────────→
```

> **钉死这件事**:robj 的 16 字节布局不是随便定的——`type:4 + encoding:4 + lru:24` 拼成 32 bit 头,`refcount` 和 `ptr` 各占一个机器字,整个结构在 64 位下恰为 16 字节(半个 cache line)。位段位宽是抠出来的:type 和 encoding 各 4 bit 因为枚举都 ≤16 种,lru 24 bit 是 LRU_BITS 常量([server.h:994](../../redis-8.0.2/src/server.h#L994))。**少一个字段会破坏对齐、多一个字节会浪费内存——这是取向②(内存即数据库)在结构体层的微观体现,百亿级 key 时每省一个字节都是省一台机器。**

## 10.3 createObject:robj 的通用构造器

来看 robj 是怎么造出来的。所有 robj 的"祖构造器"是 `createObject`([object.c:23](../../redis-8.0.2/src/object.c#L23)):

```c
/* object.c:23-31 */
robj *createObject(int type, void *ptr) {
    robj *o = zmalloc(sizeof(*o));    /* 一次 16 字节分配 */
    o->type = type;
    o->encoding = OBJ_ENCODING_RAW;   /* 默认 RAW,调用方按需改写 */
    o->ptr = ptr;
    o->refcount = 1;                  /* 出生态:被调用方持有 */
    o->lru = 0;                       /* LRU/LFU 由 initObjectLRUOrLFU 填 */
    return o;
}
```

注意几个细节:① 默认 encoding 是 `OBJ_ENCODING_RAW`,调用方若想要别的(如 `OBJ_ENCODING_LISTPACK`),自己再赋值;② `refcount` 置 1(出生态被调用方"持有");③ `lru` 置 0,但 0 不是有效值——真正的 LRU/LFU 初值在 `initObjectLRUOrLFU` 里填([object.c:33-44](../../redis-8.0.2/src/object.c#L33)),而那个函数会在把对象塞进 dict 之前/共享池里被调一次。

`createObject` 是"祖构造器",但它直接产出的 encoding 永远是 RAW。短字符串要走专门的 `createEmbeddedStringObject`(embstr)、整数要走 `createStringObjectFromLongLongWithOptions`(int),它们是更精细的子构造器,下一节详细讲。

## 10.4 embstr 与 raw:为什么短字符串要挤在同一次分配里

字符串是 Redis 里最频繁的对象类型,因此它的编码被压榨得最厉害。一个字符串 robj 有三种可能的 encoding:

- **`OBJ_ENCODING_RAW`**([object.c:65-67](../../redis-8.0.2/src/object.c#L65)):robj 和它指向的 sds 是**两次独立分配**。robj 是 `zmalloc(sizeof(robj))`,sds 是另一次 `sdsnewlen`。两次分配 = 两次 malloc 头 + 两次 cache miss。

```c
/* object.c:65-67 */
robj *createRawStringObject(const char *ptr, size_t len) {
    return createObject(OBJ_STRING, sdsnewlen(ptr,len));  /* 两次分配 */
}
```

- **`OBJ_ENCODING_EMBSTR`**([object.c:72-94](../../redis-8.0.2/src/object.c#L72)):robj 和 sds 头**挤在同一次分配里**,布局是 `[robj | sdshdr8 | buf]` 连续内存。

```c
/* object.c:72-94,关键行 */
robj *createEmbeddedStringObject(const char *ptr, size_t len) {
    robj *o = zmalloc(sizeof(robj)+sizeof(struct sdshdr8)+len+1);  /* 一次分配 */
    struct sdshdr8 *sh = (void*)(o+1);    /* sdshdr8 紧跟在 robj 后面 */
    o->type = OBJ_STRING;
    o->encoding = OBJ_ENCODING_EMBSTR;
    o->ptr = sh+1;                         /* ptr 指向 buf 起点 */
    o->refcount = 1;
    o->lru = 0;
    sh->len = len;
    sh->alloc = len;
    sh->flags = SDS_TYPE_8;                /* 写死 sdshdr8 */
    /* memcpy(buf) ... */
    return o;
}
```

注意 `createEmbeddedStringObject` 把 sds 类型**写死成 sdshdr8**([object.c:84](../../redis-8.0.2/src/object.c#L84))——`sh->flags = SDS_TYPE_8`。为什么?因为 embstr 只用于短字符串(≤44 字节,见下),sdshdr8(用 1 字节存 len、1 字节存 alloc、1 字节 flags,共 3 字节头)正好够装 44 这个长度。不需要 sdshdr16/32/64。

- **`OBJ_ENCODING_INT`**:字符串值是整数时,`ptr` 不指向任何分配,**直接把 long 值塞进 `ptr` 这个 void*** ([object.c:135-138](../../redis-8.0.2/src/object.c#L135))。零额外分配。

```c
/* object.c:135-138 */
if ((value >= LONG_MIN && value <= LONG_MAX) && flag != LL2STROBJ_NO_INT_ENC) {
    o = createObject(OBJ_STRING, NULL);
    o->encoding = OBJ_ENCODING_INT;
    o->ptr = (void*)((long)value);   /* 把整数直接塞进指针变量 */
}
```

那么选哪种编码?入口是 `createStringObject`([object.c:103-108](../../redis-8.0.2/src/object.c#L103)),按长度分路:

```c
/* object.c:102-108 */
#define OBJ_ENCODING_EMBSTR_SIZE_LIMIT 44   /* 注释见下 */
robj *createStringObject(const char *ptr, size_t len) {
    if (len <= OBJ_ENCODING_EMBSTR_SIZE_LIMIT)
        return createEmbeddedStringObject(ptr,len);  /* 短 → embstr */
    else
        return createRawStringObject(ptr,len);       /* 长 → raw */
}
```

那个 44 是怎么定的?注释 [object.c:100-101](../../redis-8.0.2/src/object.c#L100) 直说:"The current limit of 44 is chosen so that the biggest string object we allocate as EMBSTR will still fit into the 64 byte arena of jemalloc."(44 这个限制是为了让 embstr 的最大分配刚好装进 jemalloc 的 64 字节 arena)。

拆账给你看:

```text
embstr 一次分配的总字节 = robj(16) + sdshdr8(3) + len + 1(NUL)
                          = 20 + len

要装进 64 字节 arena:20 + len ≤ 64  →  len ≤ 44
```

embstr 的精算账:robj 16 字节 + sdshdr8 头 3 字节 + 字符串内容 + 1 字节 NUL。当 len=44 时总分配正好 64 字节,刚好占满 jemalloc 一个 64 字节小内存块,**没有任何浪费**。len=45 就要进 jemalloc 的 80 字节 arena,这时 embstr 相对 raw 的优势(省一次 malloc、cache 友好)就被多出来的 16 字节空洞抵消了——所以超过 44 就直接用 raw。

embstr 比 raw 的好处有两个:**① 一次分配 vs 两次分配**(省一次 malloc/free,在 jemalloc 下还能精确命中 64 字节小内存块,内存碎片更小);**② cache 友好**(robj 头和 sds 数据在同一 cache line,访问 sds 时不再多一次 cache miss)。

但 embstr 有一个致命代价:**它不可改**。robj 和 sds 头和 buf 是一块连续内存,要修改 buf(变长、变短、改内容)必须 `zrealloc`,而 realloc 可能整体搬迁——robj 头、sds 头、buf 都会变位置,robj 指针(在 dict 里被很多地方引用)就失效了。所以 embstr 写入后**永远不能原地改**,任何修改类命令碰到它,必须先转成 raw(两次分配,sds 和 robj 各自独立,改 sds 时 realloc 只影响 sds 不动 robj)。

> **钉死这件事**:embstr 是"短字符串一次连续分配、不可改",raw 是"长字符串或可改字符串两次独立分配"。**embstr 的存在理由是省一次 malloc + cache 友好,代价是任何修改都必须先转 raw**。44 这个分水岭是 jemalloc 64 字节 arena 的精算结果:`robj(16) + sdshdr8(3) + len + 1 = 20 + len ≤ 64 → len ≤ 44`。这不是拍脑袋的数字,是工程上把内存碎片也一并考虑后的最优解。

## 10.5 embstr → raw 的转换时机:任何修改类命令的必经之路

这是讲 embstr 时绕不开的反面:**它什么时候会被迫转 raw?**

答案是:任何会改变字符串内容(变长、变短、改字符)的命令,在动手前都会调 `dbUnshareStringValueWithDictEntry`([db.c:562-571](../../redis-8.0.2/src/db.c#L562))——它的名字叫"unshare",但实际职责更宽:**只要对象的 refcount 不是 1、或 encoding 不是 RAW,就把它复制成一个新的 RAW 对象,替换 dict 里原值**。

```c
/* db.c:562-571 */
robj *dbUnshareStringValueWithDictEntry(redisDb *db, robj *key, robj *o, dictEntry *de) {
    serverAssert(o->type == OBJ_STRING);
    if (o->refcount != 1 || o->encoding != OBJ_ENCODING_RAW) {
        robj *decoded = getDecodedObject(o);                    /* INT → sds */
        o = createRawStringObject(decoded->ptr, sdslen(decoded->ptr));  /* 新 RAW */
        decrRefCount(decoded);
        dbReplaceValueWithDictEntry(db,key,o,de);                /* 替换 dict 里的值 */
    }
    return o;
}
```

注意那个 if 条件 `o->refcount != 1 || o->encoding != OBJ_ENCODING_RAW`——它一网打尽三种情况:

- **`refcount > 1`(共享对象,如 `shared.integers[42]`)**:复制一份独立副本,避免改了共享对象污染所有引用方。
- **`encoding == OBJ_ENCODING_EMBSTR`**(embstr):复制成新的 RAW。因为接下来要 `sdscatlen`/`sdsgrowzero` 修改 sds,embstr 的连续内存不允许原地 realloc。
- **`encoding == OBJ_ENCODING_INT`**(int 编码,如 `SET k 42` 之后 `k` 的值是 int):`getDecodedObject` 先把 int 转成 sds 字符串(临时对象),再 `createRawStringObject` 把它变成 RAW robj。

哪些命令触发这条路径?所有修改类字符串命令。看 `APPEND`([t_string.c:470](../../redis-8.0.2/src/t_string.c#L470)):

```c
/* t_string.c:470 */
o = dbUnshareStringValueWithDictEntry(c->db,c->argv[1],o,de);
```

看 `SETRANGE`([t_string.c:707](../../redis-8.0.2/src/t_string.c#L707)):

```c
/* t_string.c:707 */
o = dbUnshareStringValueWithDictEntry(c->db,c->argv[1],o,de);
```

`INCRBY`/`INCR` 走的是 `t_string.c:609` 的 `createStringObjectFromLongLongForValue(value)`,它绕开了 embstr(算完新值直接生成 INT 编码或共享对象),但同样绕不开"embstr 不可改"这个根本约束——算新值前必然已经把原值解码成数字,等于隐式做了一次转换。

所以一个完整的 embstr → raw 转换场景是这样的:`SET k hello`(k 是 embstr,因为 len=5 ≤ 44)→ `APPEND k world` → `dbUnshareStringValueWithDictEntry` 发现 `o->encoding == OBJ_ENCODING_EMBSTR`(且 refcount=1),触发 `createRawStringObject(decoded->ptr, sdslen(decoded->ptr))` 生成新的 RAW 对象,把字典里 k 的值替换成它,然后 `sdscatlen` 把 "world" 追加到这个 RAW 对象的 sds 上。

转换的代价是什么?**一次新的 zmalloc(robj 16 字节)+ 一次新的 sdsnewlen(3+len 字节)+ 一次 memcpy(拷贝原字符串)+ 一次 decrRefCount(释放旧 embstr 一次 64 字节 zfree)**。在 embstr → raw 这一次命令上,用户会感受到比 `SET` 时多一倍的小内存分配开销。这是 embstr 用"不可改"换"省一次分配 + cache 友好"的代价摊销。

> **不这样会怎样**:假设 embstr 允许原地改,会发生什么?改短还好(realloc 可能原地缩),但改长会触发 realloc 扩容——realloc 可能整体搬迁,robj 头会移动位置,而 dict 里、客户端输出缓冲里、复制 backlog 里都可能持有这个 robj 指针,一搬迁就全部 use-after-free。所以 embstr 的"不可改"是结构性的硬约束,**宁可转 raw 多花一次分配,也不能让 realloc 搬迁 robj 头**。raw 的设计就是"robj 头永远不动,只动它指向的 sds",sds 的 realloc 不影响 robj 指针,这才让修改类命令安全。

> **钉死这件事**:embstr 一旦被 `APPEND`/`SETRANGE`/`INCR`/`SET`(修改已有值)碰到,第一件事就是走 `dbUnshareStringValueWithDictEntry`([db.c:562](../../redis-8.0.2/src/db.c#L562))转成 raw。**这是 embstr 用"不可改"换"省分配 + cache 友好"的代价摊销点**——单次 `SET` 短字符串比 raw 省,但任何一次修改就额外付出一次 16+3+len 字节的新分配 + memcpy + 旧 embstr 的释放。如果你知道某个 key 会被反复 APPEND,初始就 `SET` 一个长字符串(>44)让它一开始就是 raw,反而更划算。

## 10.6 共享对象:把"高频出现的值"做成全局单例

Redis 启动时会预先造好一大批共享对象,逻辑在 `createSharedObjects`([server.c:1934](../../redis-8.0.2/src/server.c#L1934))。最重要的一类是**共享整数**,在 [server.c:2079-2083](../../redis-8.0.2/src/server.c#L2079):

```c
/* server.c:2079-2083 */
for (j = 0; j < OBJ_SHARED_INTEGERS; j++) {
    shared.integers[j] =
        makeObjectShared(createObject(OBJ_STRING,(void*)(long)j));
    initObjectLRUOrLFU(shared.integers[j]);
    shared.integers[j]->encoding = OBJ_ENCODING_INT;
}
```

`OBJ_SHARED_INTEGERS` 默认 10000([server.h:107](../../redis-8.0.2/src/server.h#L107))。也就是说,**0 到 9999 这一万个整数,全 Redis 进程里只有一份 robj**,谁要用就拿到同一个指针。想一想这是多大的节省:任何一个 list、hash、zset、string 里只要出现一个 `42`,它指向的都是同一块内存。`makeObjectShared` 把 refcount 钉死成 `OBJ_SHARED_REFCOUNT = INT_MAX`([object.c:57-61](../../redis-8.0.2/src/object.c#L57)):

```c
/* object.c:57-61 */
robj *makeObjectShared(robj *o) {
    serverAssert(o->refcount == 1);
    o->refcount = OBJ_SHARED_REFCOUNT;
    return o;
}
```

`createStringObjectFromLongLongWithOptions` 是这条优化的受益者([object.c:129-146](../../redis-8.0.2/src/object.c#L129)):

```c
/* object.c:129-146 */
#define LL2STROBJ_AUTO 0       /* 自动选最优 */
#define LL2STROBJ_NO_SHARED 1  /* 禁用共享 */
#define LL2STROBJ_NO_INT_ENC 2 /* 禁用 int 编码 */
robj *createStringObjectFromLongLongWithOptions(long long value, int flag) {
    robj *o;
    if (value >= 0 && value < OBJ_SHARED_INTEGERS && flag == LL2STROBJ_AUTO) {
        o = shared.integers[value];              /* 零分配! */
    } else {
        if ((value >= LONG_MIN && value <= LONG_MAX) && flag != LL2STROBJ_NO_INT_ENC) {
            o = createObject(OBJ_STRING, NULL);
            o->encoding = OBJ_ENCODING_INT;
            o->ptr = (void*)((long)value);       /* 直接塞进 ptr */
        } else {
            char buf[LONG_STR_SIZE];
            int len = ll2string(buf, sizeof(buf), value);
            o = createStringObject(buf, len);    /* 大整数回退成 embstr/raw */
        }
    }
    return o;
}
```

注意三个分支:**① [0,10000) 走共享(零分配)**;② 超出共享范围但在 long 范围内走 int 编码(分配 16 字节 robj,ptr 直接存值);③ 超出 long 范围(比如 20+ 位的大整数)只能转成字符串(embstr 或 raw)。**没有一个分支会为 0-9999 之间的整数新分配内存**。

但这里有一个关键限制:并不是所有场景都能用共享整数。看 `createStringObjectFromLongLongForValue`([object.c:159-166](../../redis-8.0.2/src/object.c#L159)):

```c
/* object.c:159-166 */
robj *createStringObjectFromLongLongForValue(long long value) {
    if (server.maxmemory == 0 || !(server.maxmemory_policy & MAXMEMORY_FLAG_NO_SHARED_INTEGERS)) {
        return createStringObjectFromLongLongWithOptions(value, LL2STROBJ_AUTO);
    } else {
        return createStringObjectFromLongLongWithOptions(value, LL2STROBJ_NO_SHARED);
    }
}
```

如果配置了 `maxmemory` 且淘汰策略是 LRU/LFU(`MAXMEMORY_FLAG_NO_SHARED_INTEGERS = MAXMEMORY_FLAG_LRU|MAXMEMORY_FLAG_LFU`,[server.h:621-622](../../redis-8.0.2/src/server.h#L621))),那么**即使是 0-9999 的整数也不共享**——为什么?因为共享对象的 `lru` 字段不携带任何访问时间信息(`initObjectLRUOrLFU` 第一件事就是 `if (o->refcount == OBJ_SHARED_REFCOUNT) return;` 跳过共享对象,见 [object.c:34-35](../../redis-8.0.2/src/object.c#L34)))。在 LRU/LFU 淘汰下,每个 key 都需要独立的访问热度记录——如果所有等于 42 的 key 共享同一个 robj,它们的 lru 就被串成同一个值,LRU/LFU 算法就废了。所以"用淘汰策略时禁用共享整数"是一个**正确性优先于内存节省**的取舍。

那么为什么共享范围是 [0,10000)?这是命中率 + 节省目标的权衡。**命中率**:小整数在真实负载里出现频率极高(计数器、状态码、年龄、score、list index 都是 0-9999 的范围内),命中率足够高,共享才有意义。**节省目标**:共享节省的不是"那个数字本身",而是"robj(16字节)+ sdshdr8(3字节)+ NUL(1字节)" = 20 字节的对象头开销。一万种共享对象 = 预先分配 10000×20 ≈ 200KB 的常驻内存。如果共享范围扩到 100 万,预先开销就是 20MB,而百万级整数的命中率提升边际很小——大整数(用户 ID、时间戳、金额)取值分散,基本不会重复出现,共享它们命中率极低,做了反而费内存。**10000 是命中率和预分配开销的 sweet spot**。

`shared.bulkhdr[j]`、`shared.mbulkhdr[j]`、`shared.maphdr[j]`、`shared.sethdr[j]` 是另一类共享对象([server.c:2085-2092](../../redis-8.0.2/src/server.c#L2085))——它们是协议回复的固定前缀(`*3\r\n`、`$4\r\n`、`%2\r\n`、`~3\r\n` 之类),每个连接、每条回复都要用到,共享掉就省掉了海量的小字符串分配。

> **钉死这件事**:共享整数 [0,10000) 是 Redis 启动时一次性预分配的常驻内存(约 200KB),换来全进程任何地方出现这个范围的整数都零分配。但有两个限制:① 共享对象的 `lru` 字段恒不更新(`initObjectLRUOrLFU` 第一行特判跳过,见 [object.c:34-35](../../redis-8.0.2/src/object.c#L34))),所以 LRU/LFU 淘汰策略下禁用共享(`MAXMEMORY_FLAG_NO_SHARED_INTEGERS`,[server.h:621](../../redis-8.0.2/src/server.h#L621)));② 共享对象的 refcount 被 `makeObjectShared` 钉死成 `INT_MAX`,**永远不能修改、永远不能释放**——这是它"可被多个持有者无锁引用"的物理前提。

## 10.7 refcount 与引用计数垃圾回收:为什么不用真正的 GC

`refcount` 是个朴素的引用计数器,没有分代、没有标记、没有根集扫描。`incrRefCount`/`decrRefCount` 的实现简单到令人发指([object.c:350-384](../../redis-8.0.2/src/object.c#L350)):

```c
/* object.c:350-360 */
void incrRefCount(robj *o) {
    if (o->refcount < OBJ_FIRST_SPECIAL_REFCOUNT) {
        o->refcount++;                                   /* 普通对象:直接 ++ */
    } else {
        if (o->refcount == OBJ_SHARED_REFCOUNT) {
            /* 共享对象:no-op,引用计数永不变 */
        } else if (o->refcount == OBJ_STATIC_REFCOUNT) {
            serverPanic("You tried to retain an object allocated in the stack");
        }
    }
}

/* object.c:362-384 */
void decrRefCount(robj *o) {
    if (o->refcount == OBJ_SHARED_REFCOUNT)
        return;                              /* 共享对象:第一道特判,永不释放 */
    if (unlikely(o->refcount <= 0)) {
        serverPanic("illegal decrRefCount for object with: type %u, encoding %u, refcount %d",
            o->type, o->encoding, o->refcount);
    }
    if (--(o->refcount) == 0) {              /* 引用归零:释放 */
        switch(o->type) {
        case OBJ_STRING: freeStringObject(o); break;
        case OBJ_LIST:   freeListObject(o);  break;
        case OBJ_SET:    freeSetObject(o);   break;
        case OBJ_ZSET:   freeZsetObject(o);  break;
        case OBJ_HASH:   freeHashObject(o);  break;
        case OBJ_MODULE: freeModuleObject(o); break;
        case OBJ_STREAM: freeStreamObject(o); break;
        default: serverPanic("Unknown object type"); break;
        }
        zfree(o);                            /* 最后释放 robj 自身 */
    }
}
```

这里有几个"魔法值",都定义在 [server.h:998-1000](../../redis-8.0.2/src/server.h#L998):

```c
/* server.h:998-1000 */
#define OBJ_SHARED_REFCOUNT INT_MAX     /* 全局共享,永不可变、永不释放 */
#define OBJ_STATIC_REFCOUNT (INT_MAX-1) /* 栈上分配,禁止增减引用 */
#define OBJ_FIRST_SPECIAL_REFCOUNT OBJ_STATIC_REFCOUNT  /* 特殊值门槛 */
```

`OBJ_SHARED_REFCOUNT = INT_MAX`:代表"全局共享、永不可变"。所有 shared.integers、shared.bulkhdr 等的 refcount 都是这个值。`incrRefCount`/`decrRefCount` 对它直接 no-op——这个不变量让它能被多线程(主线程、bio 线程、模块线程)无锁引用。

`OBJ_STATIC_REFCOUNT = INT_MAX-1`:代表"栈上分配、禁止增减引用"。这种对象通常是用 `ALCREATE_STACK_OBJECT` 宏在栈上构造的临时对象(不占堆),一旦 `incrRefCount` 它就 panic([object.c:357](../../redis-8.0.2/src/object.c#L357))——栈对象生命周期由栈帧决定,引用计数无意义。

`OBJ_FIRST_SPECIAL_REFCOUNT = OBJ_STATIC_REFCOUNT`:`incrRefCount` 用它做"是否需要特殊处理"的门槛([object.c:351](../../redis-8.0.2/src/object.c#L351))。普通对象的 refcount 从 1 开始递增,远远小于 INT_MAX-1,直接 `++` 不需要每次都做两次比较;只有当 refcount 涨到接近 INT_MAX 时才进入特殊分支——这是为普通路径做的常数优化。

`freeStringObject`([object.c:289-293](../../redis-8.0.2/src/object.c#L289))里藏着一个细节:

```c
/* object.c:289-293 */
void freeStringObject(robj *o) {
    if (o->encoding == OBJ_ENCODING_RAW) {
        sdsfree(o->ptr);          /* 只有 RAW 才有独立 sds 要释放 */
    }
    /* embstr 的 sds 和 robj 在一次分配里,zfree(o) 一次搞定 */
    /* int 编码的 ptr 不指向任何分配,什么都不做 */
}
```

只有 RAW 编码需要单独 `sdsfree`——embstr 的 sds 和 robj 是连续分配,`zfree(o)`(由 `decrRefCount` 调用)一次就释放整块;int 编码的 ptr 是直接存的 long 值,不指向任何堆分配,不需要释放。**编码不仅影响分配方式,也影响释放方式**。

为什么用引用计数而不是 Boehm 之类真正的 GC?答案落在**取向④(简单优先)和取向①(单线程)**:单线程模型下不存在并发修改 refcount,朴素的 `++/--` 就够了,既没有 stop-the-world 的停顿,也没有写屏障的开销。代价是**无法回收循环引用**——但 Redis 的对象图天然是树状的(key→value,container→element),不存在环,所以这个代价根本不存在。

> **钉死这件事**:refcount 是引用计数 GC,没有分代、没有标记、没有写屏障。`decrRefCount` 的释放路径有两道特判:**① 共享对象(INT_MAX)直接 return**,**② 引用归零才按 type 分发到 `freeStringObject`/`freeListObject`/...**。这套机制能成立的前提是"对象图无环"——Redis 的 key→value、container→element 天然是树状,所以朴素引用计数够用。**这是取向④(简单优先)的招牌:不为不存在的问题(并发、环)付出复杂度代价。**

## 10.8 lru 字段:LRU 与 LFU 两种淘汰策略如何共用 24 bit

`lru` 字段是 24 bit(`LRU_BITS = 24`,[server.h:994](../../redis-8.0.2/src/server.h#L994))。这 24 bit 在两种淘汰策略下含义完全不同([server.h:1004-1006](../../redis-8.0.2/src/server.h#L1004) 注释写得明白):

- **LRU 模式**:整个 24 bit 存的是"上次访问时间"(分钟级时钟,相对全局 `lru_clock`)。
- **LFU 模式**:**低 8 bit** 存访问频率的对数计数器(LOG_C),**高 16 bit** 存上次衰减时间(LDT,分钟级 Unix time 的低 16 位)。

切换由 `initObjectLRUOrLFU` 完成([object.c:33-44](../../redis-8.0.2/src/object.c#L33)):

```c
/* object.c:33-44 */
void initObjectLRUOrLFU(robj *o) {
    if (o->refcount == OBJ_SHARED_REFCOUNT)
        return;                              /* 共享对象不参与淘汰 */
    if (server.maxmemory_policy & MAXMEMORY_FLAG_LFU) {
        o->lru = (LFUGetTimeInMinutes() << 8) | LFU_INIT_VAL;   /* 高16位LDT | 低8位频率初值5 */
    } else {
        o->lru = LRU_CLOCK();                /* 整24位都是 LRU 时钟 */
    }
}
```

注意第一行的关键判断:**共享对象跳过淘汰初始化**。这正是 `shared.integers` 永远不会被 LRU/LFU 误淘汰的原因——它们根本不携带访问时间,等于免疫淘汰。

LFU 的两个常量:`LFU_INIT_VAL = 5`([server.h:3759](../../redis-8.0.2/src/server.h#L3759)),是个细节。注释 [evict.c:252-257](../../redis-8.0.2/src/evict.c#L252) 解释:"New keys don't start at zero, in order to have the ability to collect some accesses before being trashed away, so they start at LFU_INIT_VAL."(新 key 不从 0 开始,是为了让它们有机会积累几次访问再被淘汰,所以初始频率是 5)。如果新 key 一进来频率是 0,LFU 衰减逻辑立刻就会把它当垃圾淘汰掉,这正是 5 这个非零初值的用处。

LFU 的频率计数器是对数增长的——`LFULogIncr`([evict.c:282-290](../../redis-8.0.2/src/evict.c#L282)):

```c
/* evict.c:282-290 */
uint8_t LFULogIncr(uint8_t counter) {
    if (counter == 255) return 255;             /* 饱和 */
    double r = (double)rand()/RAND_MAX;
    double baseval = counter - LFU_INIT_VAL;
    if (baseval < 0) baseval = 0;
    double p = 1.0/(baseval*server.lfu_log_factor+1);   /* counter 越大,p 越小 */
    if (r < p) counter++;                                /* 概率性 +1 */
    return counter;
}
```

这是**概率性对数计数**:counter 越大,下一次访问真正 +1 的概率越低(`p = 1/(counter×log_factor+1)`)。这样 8 bit(0-255)能表示的"逻辑访问次数"远超 256——配合 `lfu_log_factor`(默认 10),counter 到 255 大约对应一亿次访问。低 8 bit 频率 + 高 16 位衰减时间,合在一起让 LFU 既能表达"高频",也能表达"很久没访问的曾经高频"(靠衰减)。

衰减逻辑在 `LFUDecrAndReturn`([evict.c:302-308](../../redis-8.0.2/src/evict.c#L302)):

```c
/* evict.c:302-308 */
unsigned long LFUDecrAndReturn(robj *o) {
    unsigned long ldt = o->lru >> 8;            /* 高 16 位 = LDT */
    unsigned long counter = o->lru & 255;       /* 低 8 位 = 频率 */
    /* 如果过了 lfu_decay_time 分钟,counter 减 1 */
    /* ... */
}
```

把两种语义塞进同一个字段,是典型的"以一个 if 换 4 字节"——按全局百亿级 key 量算,省下的内存极其可观。代价是阅读源码时要时刻警惕 `o->lru` 到底该怎么解释,得结合 `maxmemory_policy` 判断。这个 if 在整个进程生命周期里基本不变(LRU/LFU 切换要重启),分支预测器会把它预测成几乎免费。

> **钉死这件事**:`lru` 字段的 24 bit 在 LRU 模式下是分钟级时钟(整 24 bit),在 LFU 模式下是"高 16 位 LDT(衰减时间)+ 低 8 位 LOG_C(对数频率)"。一个 `if (server.maxmemory_policy & MAXMEMORY_FLAG_LFU)` 切换两种语义,省掉结构体里加一个 union 的 4 字节。这是字段语义复用的招牌——**两种策略都需要"每个值一个访问热度标记",共用一个字段是最经济的做法**。代价是读源码时要时刻警惕 `o->lru` 该怎么解释,得结合 `maxmemory_policy` 判断——本章第十三、十四章会深入讲 expire/evict 的全流程。

## 10.9 tryObjectEncoding:命令执行后自动编码降级

讲完三种编码,还有一个常被忽略的入口:`tryObjectEncodingEx`([object.c:608-683](../../redis-8.0.2/src/object.c#L608))。它在 `SET` 命令最终落库前被调,职责是"看一个字符串 robj 能不能压成更省的编码"。完整逻辑分三段:

```c
/* object.c:608-683,精简 */
robj *tryObjectEncodingEx(robj *o, int try_trim) {
    sds s = o->ptr;
    long value;
    size_t len;

    serverAssertWithInfo(NULL,o,o->type == OBJ_STRING);

    /* 第一道:只对 RAW/EMBSTR 编码尝试压缩,其它编码(int)不动 */
    if (!sdsEncodedObject(o)) return o;

    /* 第二道:共享对象和被多次引用的对象不压(避免副作用) */
    if (o->refcount > 1) return o;

    len = sdslen(s);

    /* 第三道:如果能解析成 long 且 ≤ 20 字符,试转 INT */
    if (len <= 20 && string2l(s,len,&value)) {
        /* 共享范围内走共享对象 */
        if ((server.maxmemory == 0 ||
            !(server.maxmemory_policy & MAXMEMORY_FLAG_NO_SHARED_INTEGERS)) &&
            value >= 0 &&
            value < OBJ_SHARED_INTEGERS)
        {
            decrRefCount(o);
            return shared.integers[value];           /* 零分配 */
        } else {
            /* 否则就地转 INT 编码 */
            if (o->encoding == OBJ_ENCODING_RAW) {
                sdsfree(o->ptr);
                o->encoding = OBJ_ENCODING_INT;
                o->ptr = (void*) value;
                return o;
            } else if (o->encoding == OBJ_ENCODING_EMBSTR) {
                decrRefCount(o);
                return createStringObjectFromLongLongForValue(value);
            }
        }
    }

    /* 第四道:短字符串且仍是 RAW,试转 EMBSTR(更省) */
    if (len <= OBJ_ENCODING_EMBSTR_SIZE_LIMIT) {
        robj *emb;
        if (o->encoding == OBJ_ENCODING_EMBSTR) return o;  /* 已经是 embstr 不动 */
        emb = createEmbeddedStringObject(s,sdslen(s));
        decrRefCount(o);
        return emb;
    }

    /* 第五道:都不是,保留 RAW,trim 一下 sds */
    if (try_trim)
        trimStringObjectIfNeeded(o, 0);
    return o;
}
```

这段代码体现了一个核心思想:**编码不是创建时一次定型的,而是每次 SET 都会尝试压到最优**。一条 `SET k 12345` 走完这段,`k` 的值会从临时的 embstr(创建时走 `createStringObject` 因为 "12345" 长度 5 ≤ 44)被压成共享对象 `shared.integers[12345]`——零分配、零 sds、零 zmalloc。

注意第二道 `if (o->refcount > 1) return o`——**共享对象和被多次引用的对象不做编码压缩**,因为压缩会替换对象指针(如 `decrRefCount(o); return shared.integers[value]`),如果原对象被多处引用,替换会让其它引用方持有悬垂指针。

> **钉死这件事**:`tryObjectEncodingEx`([object.c:608](../../redis-8.0.2/src/object.c#L608))是 Redis"编码自适应"的发动机——每条 `SET` 命令落库前都会调它,自动把字符串压成最优编码(数字 → INT/共享、短串 → EMBSTR、长串 → RAW)。它体现了编码切换的三个不变量:**① 只在 refcount==1 时压**(避免悬垂引用);**② 共享对象跳过**(避免破坏 lru);**③ 编码压缩总是"创建新对象 + 替换 dict 里原值 + decrRefCount 旧对象"**。

## 10.10 技巧精解①:robj 16 字节布局的精算

把前面几节关于 robj 16 字节的精算汇成一张总表:

| 设计选择 | 取舍 | 体现的取向 |
|---------|------|-----------|
| `type:4 + encoding:4` 共 1 字节 | 类型 ≤16 种、编码 ≤16 种,各 4 bit 够装 | 取向②(省内存) |
| `lru:24` 与前 8 bit 拼成 4 字节头 | 24 bit 够装分钟级 LRU 时钟(约 194 天)或 LFU 双模 | 取向②(省内存) |
| 头 4 字节 + refcount(4) + ptr(8) = 16 字节 | 半个 cache line,遍历 dict 时命中率高 | 取向② + 缓存友好 |
| `type` 与 `encoding` 分离 | 上层只判 type、底层可换 encoding,编码切换对命令透明 | 取向③(编码自适应) |
| `refcount` 是朴素 int 不是 atomic | 单线程无并发,朴素 ++/-- 够用 | 取向①(单线程) |
| `ptr` 是 `void*` 不是 union | 同一个字段既能存指针(RAW/EMBSTR)、又能存 long(INT 编码) | 取向② + 类型擦除 |

embstr 的精算(对应 10.4 节):

| 长度 len | encoding | 一次分配字节 | jemalloc arena | 备注 |
|---------|---------|------------|---------------|------|
| 0-44 | EMBSTR | 20 + len ≤ 64 | 64 字节 | 刚好填满小内存块 |
| 45-? | RAW | 16 + (3+len+1) | 80 字节起 | 两次分配,可改 |

int 编码的精算:

| 值范围 | encoding | 内存占用 | 备注 |
|--------|---------|---------|------|
| [0, 10000) 且无淘汰 | 共享对象 | 0(共享池里) | 零分配 |
| 在 long 范围内、非共享 | INT | 16 字节(robj only) | ptr 直接存值 |
| 超出 long | EMBSTR/RAW | 20+len 或 16+(4+len) | 走字符串路径 |

> **钉死这件事**:robj 的 16 字节、embstr 的 44 字节分水岭、共享整数的 10000 上限、int 编码的 16 字节节省——**每一个数字都不是拍脑袋的,是精算后的 sweet spot**。44 是 jemalloc 64 字节 arena 的精确填满,10000 是命中率 vs 预分配开销的平衡,int 是"ptr 这个 8 字节字段直接装下 long 值"的零额外分配。这是取向②(内存即数据库)在结构体层的微观体现——百亿级 key 时每省一个字节都是省一台机器。

## 10.11 技巧精解②:refcount 魔法值与共享对象的免锁访问

refcount 的设计藏着两个层次的精算。

**第一层:朴素 ++/-- 而不是原子操作。** 看 `incrRefCount`([object.c:350-360](../../redis-8.0.2/src/object.c#L350))和 `decrRefCount`([object.c:362-384](../../redis-8.0.2/src/object.c#L362)),它们对 `o->refcount` 的修改都是朴素的 `++`/`--`,没有任何 `__atomic_add_fetch` 或 `__sync_synchronize`。为什么能这么大胆?**因为 Redis 主线程是单线程**(取向①),所有命令路径串行执行,不存在两个线程同时 `incrRefCount` 同一个对象的竞争。bio 后台线程、模块线程虽然存在,但它们只通过特定接口(如 `incrRefCount` 共享对象)与主线程交互,且共享对象的 refcount 是 `INT_MAX` 永不变,所以"多线程访问共享对象"实际不修改 refcount,无需加锁。

**第二层:魔法值 INT_MAX 做共享对象免锁标记。** `OBJ_SHARED_REFCOUNT = INT_MAX` 这个值的选择不是随意的:

- `incrRefCount` 的判断 `if (o->refcount < OBJ_FIRST_SPECIAL_REFCOUNT)` 用 `OBJ_FIRST_SPECIAL_REFCOUNT = INT_MAX-1` 做门槛([server.h:1000](../../redis-8.0.2/src/server.h#L1000))),普通对象的 refcount 远远小于这个值(1,2,3...),走快速路径 `++`;只有 refcount 接近 INT_MAX 才走特殊分支——这是常数优化。
- `decrRefCount` 第一行 `if (o->refcount == OBJ_SHARED_REFCOUNT) return`([object.c:363](../../redis-8.0.2/src/object.c#L363)))——共享对象永不释放,这一道特判保证了 `shared.integers[42]` 即使被 decrRefCount 一亿次也不会被 free。
- `initObjectLRUOrLFU` 第一行 `if (o->refcount == OBJ_SHARED_REFCOUNT) return`([object.c:34](../../redis-8.0.2/src/object.c#L34)))——共享对象不携带 LRU/LFU 信息,这道特判保证了它们不会被淘汰算法误杀。

**这三个特判合起来,让共享对象"可被任何线程无锁引用、永不变更、永不释放"**。这是一个跨层不变量:refcount 字段 + `incrRefCount`/`decrRefCount`/`initObjectLRUOrLFU` 三个 API + 共享对象全局池 `shared.integers` 三者协同,才能让"零分配 + 零锁 + 零 GC"同时成立。

> **钉死这件事**:refcount 的魔法值 INT_MAX 不是装饰,是**让共享对象免锁访问的物理基础**。朴素 `++/--`(无原子操作)能成立是因为主线程单线程;魔法值 INT_MAX 能让共享对象"永不释放、永不变更"是因为 `incrRefCount`/`decrRefCount`/`initObjectLRUOrLFU` 三处特判。**这是取向①(单线程)+ 取向②(省内存)+ 取向④(简单优先)三个取向在 refcount 字段上的交汇**——单线程省了原子操作、共享对象省了分配、朴素计数省了 GC 复杂度。

## 10.12 8.0 的 keymeta 抽象:mstr(m-string)

讲完 robj,进入本章真正的 8.0 新东西。先纠正一个可能由旧版资料带来的预期:**8.0.2 源码里并没有 `keymeta.c`/`keymeta.h` 这两个文件**。Redis 团队为"key 元信息"做的底层基础设施,在 8.0 里实际叫 `mstr`(文件 `mstr.h`/`mstr.c`)。它的设计意图在文件头注释里写得明明白白([mstr.h:9-11](../../redis-8.0.2/src/mstr.h#L9)):

> "mstr stands for immutable string with optional metadata attached."
> (mstr = 带可选元数据附加的不可变字符串)

为什么需要它?注释 [mstr.h:13-24](../../redis-8.0.2/src/mstr.h#L13) 给了"为什么不"的两条路:

> "One thought might be, why not to extend sds to support metadata. The answer is that sds is mutable string in its nature, with wide API (split, join, etc.). Pushing metadata logic into sds will make it very fragile, and complex to maintain. Another idea involved using a simple struct with flags and a dynamic buf[] at the end. While this could be viable, it introduces considerable complexity and would need maintenance across different contexts."

翻译过来:**① 为什么不扩展 sds?** 因为 sds 是可变字符串,有 split/join 等大量 API,把元数据逻辑塞进去会非常脆弱。**② 为什么不用一个带 flags + 动态 buf[] 的 struct?** 那样维护成本太高。于是另起炉灶,做一种**不可变、API 受限、可挂元数据**的字符串类型。

它的内存布局是这一层最精妙的部分。无元数据时的基本形态([mstr.h:29-40](../../redis-8.0.2/src/mstr.h#L29)):

```text
+----------------------------------------------+
| mstrhdr8                       | c-string |  |
+--------------------------------+-------------+
|8b   |2b     |1b      |5b       |?bytes    |8b|
| Len | Type  |m-bit=0 | Unused  | String   |\0|
+----------------------------------------------+
```

`mstrNew()` 返回的指针直接指向字符串首字节,头信息藏在它前面——这套"指针指向数据、头在前"的把戏和 sds 同源(返回的是 `char*`,可直接当 C 字符串传给 `strcmp`/`memcmp`)。注意 `Type` 用 2 bit 区分 `mstrhdr5/8/16/64` 四种头([mstr.h:139-144](../../redis-8.0.2/src/mstr.h#L139))),和 sds 的 sdshdr5/8/16/32/64 一脉相承——短串用 5 bit 存长度的 hdr5,长串用更大的头。

`m-bit`(meta bit)是关键:它为 0 表示无元数据,为 1 表示字符串前面还跟着 16 bit 的元数据标志位 `mFlags`。带元数据时的布局([mstr.h:48-60](../../redis-8.0.2/src/mstr.h#L48)):

```text
+-------------------------------------------------------------------------------+
| METADATA FIELDS       | mflags | mstrhdr8                       | c-string |  |
+-----------------------+--------+--------------------------------+-------------+
|?bytes |?bytes |?bytes |16b     |8b   |2b     |1b      |5b       |?bytes    |8b|
| Meta3 | Meta2 | Meta0 | 0x1101 | Len | Type  |m-bit=1 | Unused  | String   |\0|
+-------------------------------------------------------------------------------+
```

`mFlags` 是 16 bit(`typedef uint16_t mstrFlags`,[mstr.h:160](../../redis-8.0.2/src/mstr.h#L160))),每一位对应一种元数据;bit i 置位,则元数据 i 被附加。**元数据在内存里按枚举倒序排列**(Meta0 离字符串最近,Meta3 最远),这样可以通过 `mstrMetaRef(s, kind, flagIdx)` 用一个偏移计算直接定位。

元数据的种类和大小由一个 `mstrKind` 结构定义([mstr.h:186-189](../../redis-8.0.2/src/mstr.h#L186)):

```c
/* mstr.h:186-189 */
typedef struct mstrKind {
    const char *name;
    int metaSize[NUM_MSTR_FLAGS];            /* 每种元数据的字节数,0 表示该位不用 */
} mstrKind;
```

`NUM_MSTR_FLAGS = sizeof(mstrFlags)*8 = 16`([mstr.h:183](../../redis-8.0.2/src/mstr.h#L183))),即一个 mstrKind 最多定义 16 种元数据。

四种头结构 [mstr.h:162-181](../../redis-8.0.2/src/mstr.h#L162) 都是 `__attribute__ ((__packed__))` 紧凑打包。注意 `mstrhdr8` 故意有一个 `unused` 字段([mstr.h:167](../../redis-8.0.2/src/mstr.h#L167)):

```c
/* mstr.h:166-171 */
struct __attribute__ ((__packed__)) mstrhdr8 {
    uint8_t unused;  /* To achieve odd size header (See comment above) */
    uint8_t len;
    unsigned char info; /* 2 lsb of type, 6 unused bits */
    char buf[];
};
```

那个注释一语道破:`unused` 字段存在的唯一目的就是**让 mstrhdr8 的字节数变成奇数**。下文 10.14 会详细讲为什么必须是奇数。

## 10.13 mstr 的架构野心:未来 keymeta 的雏形

这一层的野心在注释里的 `HkeyMetaFlags` 假想例子一览无余([mstr.h:80-100](../../redis-8.0.2/src/mstr.h#L80)):

```c
/* mstr.h:80-100,假想代码(注释里的示例,不是已启用代码) */
typedef enum HkeyMetaFlags {
    HKEY_META_VAL_REF_COUNT    = 0,  // refcount
    HKEY_META_VAL_REF          = 1,  // Val referenced
    HKEY_META_EXPIRE           = 2,  // TTL and more
    HKEY_META_TYPE_ENC_LRU     = 3,  // TYPE + LRU + ENC
    HKEY_META_DICT_ENT_NEXT    = 4,  // Next dict entry
    // Following two must be together and in this order
    HKEY_META_VAL_EMBED8       = 5,  // Val embedded, max 7 bytes
    HKEY_META_VAL_EMBED16      = 6,  // Val embedded, max 15 bytes (23 with EMBED8)
} HkeyMetaFlags;

mstrKind hkeyKind = {
    .name = "hkey",
    .metaSize[HKEY_META_VAL_REF_COUNT] = 4,
    .metaSize[HKEY_META_VAL_REF]       = 8,
    .metaSize[HKEY_META_EXPIRE]        = sizeof(ExpireMeta),
    .metaSize[HKEY_META_TYPE_ENC_LRU]  = 8,
    .metaSize[HKEY_META_DICT_ENT_NEXT] = 8,
    .metaSize[HKEY_META_VAL_EMBED8]    = 8,
    .metaSize[HKEY_META_VAL_EMBED16]   = 16,
};
```

这段不是 8.0.2 已启用的代码,而是团队在**宣告架构方向**:未来 keyspace 的 key 可以收敛成一种叫 "hkey" 的 mstring kind,把 refcount、val 引用、TTL(`ExpireMeta`)、TYPE+ENC+LRU、`dictEntry` 的 next 指针、甚至"嵌入 7/15/23 字节的小值"全部定义成可选元数据。

把这段和现在的 robj + dictEntry + redisDb 对比一下:

| 信息 | 当前位置 | 未来 hkey 中的位置 |
|------|---------|------------------|
| type/encoding | robj->type/encoding | HKEY_META_TYPE_ENC_LRU(8 字节,合并存) |
| LRU/LFU | robj->lru | HKEY_META_TYPE_ENC_LRU(同上) |
| TTL | dictEntry 的 dictEntryMetadata | HKEY_META_EXPIRE(sizeof(ExpireMeta)) |
| next 指针 | dictEntry->next | HKEY_META_DICT_ENT_NEXT(8 字节) |
| val 引用 | dictEntry->v.val | HKEY_META_VAL_REF(8 字节) |
| refcount | (当前 key 级无 refcount) | HKEY_META_VAL_REF_COUNT(4 字节) |
| 短值直接嵌入 | 无(总是独立 val robj) | HKEY_META_VAL_EMBED8/16(7/15/23 字节) |

**核心革命**:把现在散落在 `dictEntry`/`redisDb`/`robj` 三个结构上的 key 级状态,统一收编到一次内存分配里。每个 key 只需要一次 zmalloc,而不是现在的"dictEntry + robj + sds"三次独立分配。

最后一项 `HKEY_META_VAL_EMBED8/16` 尤其有意思:**它允许把 7-23 字节的短值直接嵌进 key 的 mstring 里**,不再需要独立的 val robj。这是把 embstr 的"连续分配"思想推到极致——key 和 value 在同一个分配里。

这就是本章标题"keymeta 层"的真正含义——**不是某个叫 keymeta.c 的文件,而是为 key 元信息建立的、可组合、可单分配、可按需挂载的元数据框架**。

## 10.14 mstr 在 8.0.2 的实际落地:hash 字段带 TTL

8.0.2 里 mstr 已经投入生产使用,落点是 **hash 字段的过期时间**(hash field expiration)。在此之前,过期只能挂在 key 上;hash 的某个 field 想单独过期,就得想办法把 TTL 信息附在 field 字符串上。`t_hash.c` 直接把 field 从 sds 换成了 mstr([t_hash.c:137-155](../../redis-8.0.2/src/t_hash.c#L137)):

```c
/* t_hash.c:137-155 */
/* The implementation of hashes by dict was modified from storing fields as sds
 * strings to store "mstr" (Immutable string with metadata) in order to be able
 * to attach TTL (ExpireMeta) to the hash-field. This usage of mstr opens up
 * the opportunity for future features to attach additional metadata by need
 * to the fields. */

typedef enum HfieldMetaFlags {
    HFIELD_META_EXPIRE = 0,
} HfieldMetaFlags;

mstrKind mstrFieldKind = {
    .name = "hField",
    /* Taking care that all metaSize[*] values are even ensures that all
     * addresses of hfield instances will be odd. */
    .metaSize[HFIELD_META_EXPIRE] = sizeof(ExpireMeta),
};
static_assert(sizeof(struct ExpireMeta) % 2 == 0, "must be even!");
```

`HFIELD_META_EXPIRE` 是目前 hfield 唯一一种元数据,但框架已经为未来(比如 hash 字段的 LRU、引用计数)留好了扩展位——**只要在 enum 里加一个 flag、在 `metaSize[]` 里填上字节数,内存布局和读写 API 全部自动就位**。这就是抽象的力量:新增一种元数据不需要改 mstr 的任何核心代码。

注释里那句 `Taking care that all metaSize[*] values are even ensures that all addresses of hfield instances will be odd.`(确保所有 metaSize 值都是偶数,从而所有 hfield 实例地址都是奇数)牵出一个跨层的硬约束,见下一节。

## 10.15 技巧精解③:奇地址保证——为什么 mstr 必须是奇地址

mstr 注释里反复强调一件事([mstr.h:119-125](../../redis-8.0.2/src/mstr.h#L119)):

> "Few optimizations in Redis rely on the fact that sds address is always an odd pointer. We can achieve the same with a little effort. It was already taken care that all headers of type mstrhdrX has odd size. With that in mind, if a new kind of mstr is required to be limited to odd addresses, then we must make sure that sizes of all related metadatas that are defined in mstrKind are even in size."

翻译过来:**Redis 里有些优化依赖"sds 地址永远是奇数"这个事实**。mstr 要保持同样的性质,就要求所有 mstrhdrX 头的字节数必须是奇数,且附加的元数据字节数必须是偶数——这样"奇数头 + 偶数元数据"加起来仍是奇数,mstr 的字符串首字节地址就是奇数。

源码用 `static_assert` 在编译期锁死这个不变量([mstr.h:217-221](../../redis-8.0.2/src/mstr.h#L217)):

```c
/* mstr.h:217-221 */
static_assert(sizeof(struct mstrhdr5 ) % 2 == 1, "must be odd");
static_assert(sizeof(struct mstrhdr8 ) % 2 == 1, "must be odd");
static_assert(sizeof(struct mstrhdr16) % 2 == 1, "must be odd");
static_assert(sizeof(struct mstrhdr64) % 2 == 1, "must be odd");
static_assert(sizeof(mstrFlags ) % 2 == 0, "must be even to keep mstr pointer odd");
```

为什么 mstr 必须是奇地址?这要回到第五章 dict 的渐进式 rehash——dict 在 rehash 时用指针的**最低位**做标记(渐进式迁移时区分"在老表还是新表")。指针的最低位平时是 0(因为 malloc 返回的地址至少按 2 字节对齐,通常按 8/16 字节对齐),所以最低位可以"白拿"用来做标记。但前提是:**这个指针天然就是奇数,或者特殊设计成可以低位编码**。

sds 通过"头在前、指针指向数据"的布局,让返回的指针指向 buf 起点——sdshdr5/8/16/32/64 的字节数设计成奇数,加上 buf 内容对齐保证,使 sds 指针天然是奇数。**mstr 沿用了同样的设计**——这就是 mstrhdr8 故意有个 `unused` 字段的原因([mstr.h:167](../../redis-8.0.2/src/mstr.h#L167)))。`unused` 让 mstrhdr8 的字节数从 2(len+info)变成 3,从而是奇数;再加上偶数字节的元数据,最终的 mstr 指针就是奇数。

这是一个跨层的硬约束:**数据结构层(dict 的 rehash 用低位编码)、内存分配层(jemalloc 返回偶对齐地址)、字符串层(sds/mstr 的头设计成奇字节)**,三者协同才能让"指针低位白拿做标记"成立。任何一层破坏约定,另外两层都会出 bug。

> **钉死这件事**:mstr 的所有头结构(mstrhdr5/8/16/64)都设计成奇数字节,且要求附加元数据的字节数必须是偶数——这样"奇头 + 偶元数据 + 偶 mFlags"加起来仍是奇地址。**这是为了让 dict 的渐进式 rehash 能用指针最低位做标记**(第五章讲过)。mstrhdr8 里那个 `unused` 字段不是浪费,是为保证头字节数为奇数而**刻意保留的填充字节**——少它一字节,整个 dict 的渐进式 rehash 优化就崩了。

## 10.16 技巧精解④:取舍——mstr 用"不可变"换"简单 + 紧凑"

mstr 的最大代价是它**不可变**。任何修改字符串内容的操作都必须新分配一个 mstr——因为元数据 + 头 + 字符串是一次连续分配,realloc 可能整体搬迁,破坏 mstr 指针的奇地址不变量。

对于 hash 字段这种"一旦写入 key 名基本不改"的场景,这个代价几乎为零;但如果哪天有人想用 mstr 存可变字符串,就会撞上 sds 当年绕开的同一个问题。Redis 选择把 mstr 的 API 严格限定在"创建 + 挂元数据 + 读取"([mstr.h:191-205](../../redis-8.0.2/src/mstr.h#L191))),不提供 split/join/append——明确地用功能换简单、换内存紧凑。

注释 [mstr.h:25-29](../../redis-8.0.2/src/mstr.h#L25) 直白:"we introduce a new implementation of immutable strings, with limited API, and with the option to attach metadata. The representation of the string, without any metadata, in its basic form, resembles SDS but without the API to manipulate the string."(我们引入一种新实现:不可变字符串,API 受限,可挂元数据。它的基本形态像 sds,但**没有操纵字符串的 API**)。

这正是取向④(简单优先)在 mstr 上的体现:**宁可放弃可变字符串的灵活性,也不让元数据 API 滑向 sds 那种"什么都能做"的复杂度**。这是 Redis 团队从 sds 的成功和复杂度里学到的——sds 之所以好用是因为它专门为"可变字符串"优化;如果要为"不可变 + 元数据"也做一次,就另起炉灶,不污染 sds。

## 10.17 散点:EMBSTR 不可改的边界、refcount 归零的回收细节

**`freeStringObject` 只对 RAW 释放 sds。** 看 [object.c:289-293](../../redis-8.0.2/src/object.c#L289):

```c
/* object.c:289-293 */
void freeStringObject(robj *o) {
    if (o->encoding == OBJ_ENCODING_RAW) {
        sdsfree(o->ptr);     /* 只有 RAW 才有独立 sds 要释放 */
    }
    /* embstr 的 sds 和 robj 在一次分配里,zfree(o) 一次搞定 */
    /* int 编码的 ptr 不指向任何分配,什么都不做 */
}
```

三种编码各有各的释放路径:RAW 要单独 `sdsfree(o->ptr)` 再 `zfree(o)`;EMBSTR 只要 `zfree(o)`(整块释放);INT 编码啥都不用做(只释放 robj)。**编码不仅决定分配方式,也决定释放方式**——这是 `decrRefCount` 里 `switch(o->type)` 分发的根本理由。

**`getDecodedObject` 的临时对象技巧。** 当一个 INT 编码的对象需要被当字符串用时(比如 `APPEND` 数字后追加字符串),`getDecodedObject`([object.c:703-718](../../redis-8.0.2/src/object.c#L703))会在栈上用 `ll2string` 转成 sds,再 `createStringObject` 生成临时 EMBSTR/RAW 对象。这是"按需解码、用完即弃"的模式,避免常驻 INT 对象总是带一份 sds 浪费内存。

**`MAXMEMORY_FLAG_NO_SHARED_INTEGERS` 的意义。** [server.h:621-622](../../redis-8.0.2/src/server.h#L621):

```c
/* server.h:621-622 */
#define MAXMEMORY_FLAG_NO_SHARED_INTEGERS \
    (MAXMEMORY_FLAG_LRU|MAXMEMORY_FLAG_LFU)
```

即"配置了 LRU 或 LFU 淘汰策略时,禁用共享整数"。原因前面讲过——共享对象的 lru 字段是免疫的,如果所有等于 42 的 key 共享一个对象,它们的访问热度就串成一个值,LRU/LFU 算法失效。**这是正确性优先于内存节省的取舍**。

**`makeObjectShared` 只能 refcount==1 时调。** [object.c:57-61](../../redis-8.0.2/src/object.c#L57):

```c
/* object.c:57-61 */
robj *makeObjectShared(robj *o) {
    serverAssert(o->refcount == 1);     /* 必须是独占的才能共享化 */
    o->refcount = OBJ_SHARED_REFCOUNT;
    return o;
}
```

`serverAssert(o->refcount == 1)` 是个 sanity check——只能把"刚创建、未被引用"的对象共享化,不能把"已经被引用了"的对象直接钉死成 INT_MAX(那会让其它持有者的 decrRefCount 漏释放)。

## 章末:回扣、五个为什么、往哪钻

### 主线回扣

把这一章的五条取向对一遍。**取向②(内存即数据库)** 贯穿始终:robj 压到 16 字节、lru 用 24 bit 复用、共享一万个整数、mstr 把元数据和字符串塞进一次分配——每一个设计都在抠字节,因为内存就是数据库本身,省一个字节乘以百亿 key 就是省一台机器。**取向③(编码自适应)** 是 robj 的存在理由:type 与 encoding 分离,让同一逻辑值在不同规模下用不同物理表示(embstr/raw/int/listpack/ht/...),上层无感。**取向④(简单优先)** 体现在三处:引用计数朴素到没有 GC,换来单线程下零开销;mstr 宁可放弃可变字符串的灵活性,也不让元数据 API 滑向 sds 那种"什么都能做"的复杂度;`createObject` 默认 encoding=RAW 而不是自动选最优,把"挑编码"这件事交给 `tryObjectEncodingEx` 显式做。**取向①(单线程 + 事件循环)** 是 refcount 不加锁的前提,也是共享对象能"零锁零 GC"的物理基础。**取向⑤(可靠性)** 在 `decrRefCount` 的 panic 检查(`refcount <= 0` 直接 panic,见 [object.c:366-369](../../redis-8.0.2/src/object.c#L366)))、`makeObjectShared` 的 `serverAssert`、mstr 的 `static_assert` 编译期约束里闪光——非法状态宁可立刻崩溃,也不默默腐化。

robj 和 mstr 一起,回答了同一个根问题的两个侧面:**"如何把多样的值统一封装,又让底层可换"**(robj),以及**"如何把 key 的元信息统一封装,又让扩展可加"**(mstr)。两者都用"一次连续分配 + 头在前 + 数据在后"的布局,都不是巧合——这是 Redis 团队从 sds 的成功里抽象出的通用模式,在 mstr 上推到了"还能挂元数据"的极致。

### 五个为什么

**Q1:为什么 embstr 的长度上限是 44,不是 32 或 64?**

44 是 jemalloc 64 字节 arena 的精算结果。embstr 一次分配字节 = robj(16) + sdshdr8 头(3) + len + 1(NUL) = 20 + len。要装进 64 字节 arena,20 + len ≤ 64,即 len ≤ 44。len=45 就要进 80 字节 arena,留下 16 字节空洞,embstr 相对 raw 的省分配优势就被抵消,所以转 raw。这是 [object.c:100-102](../../redis-8.0.2/src/object.c#L100) 注释直说的依据。

**Q2:共享整数为什么上限是 10000,不是 1000 或 100000?**

10000 是命中率 vs 预分配开销的 sweet spot。命中率方面:0-9999 的整数在真实负载里出现频率极高(计数器、状态码、年龄、score、list index 都是这个范围)。预分配方面:共享池常驻 10000 × (16 robj + 3 sdshdr8 + 1 NUL) ≈ 200KB,可以接受。扩到 100000 就要预存 2MB,而大整数的命中率提升边际很小;缩到 1000 又损失大量命中机会。10000 是实测后的经验值。

**Q3:embstr 被修改时为什么要转 raw,不能就地 realloc 吗?**

embstr 的 robj 头和 sds 头和 buf 是一块连续内存。realloc 改长度可能整体搬迁(扩容超过原 arena 大小时),robj 头会移动位置,而 dict 里、客户端输出缓冲里、复制 backlog 里都可能持有这个 robj 指针,一搬迁就全部 use-after-free。raw 的设计是"robj 头永远不动,只动它指向的 sds"——sds 的 realloc 不影响 robj 指针,这才让修改类命令安全。所以宁可转 raw 多花一次分配,也不能让 realloc 搬迁 robj 头。

**Q4:refcount 为什么不用真正的 GC(Boehm 之类)?**

两个原因:① 单线程模型下不存在并发修改 refcount,朴素的 ++/-- 就够了,既没有 stop-the-world 停顿,也没有写屏障开销(取向① + 取向④);② Redis 的对象图天然是树状(key→value,container→element),不存在环,引用计数能正常工作。真正的 GC 是为"对象图可能有环、可能并发修改"准备的复杂工具,Redis 不需要。

**Q5:mstr 为什么不可变,而不是像 sds 那样可变?**

因为 mstr 的设计目标是"单分配紧凑布局 + 可挂元数据"。一旦允许变长 realloc,元数据 + 头 + 字符串的整体搬迁会破坏"指针指向数据、头在前"的不变量,也会破坏"指针必须是奇地址"的硬约束(第十五章讲过)。所以 mstr 选择"创建后不可变"——牺牲 split/join/cat 等 API,换来的是元数据挂载的简单性和内存紧凑性。对于 hash 字段这种"key 名一旦写入基本不改"的场景,这个代价几乎为零。这是取向④(简单优先)的体现。

### 想继续深入往哪钻

- 想看 embstr/raw/int 三种编码的真实切换:读 [object.c](../../redis-8.0.2/src/object.c) 的 `tryObjectEncodingEx`(608-683)、`createStringObject`(103-108)、`createEmbeddedStringObject`(72-94),它们是字符串编码自适应的发动机。
- 想看共享对象的完整列表:读 [server.c](../../redis-8.0.2/src/server.c) 的 `createSharedObjects`(1934 起),里面有 shared.integers、shared.bulkhdr、shared.mbulkhdr、shared.maphdr、shared.sethdr、shared.minstring、shared.maxstring、各种 OK/ERR/-1/-2 回复,共数百个全局共享对象。
- 想看 hash field TTL 的完整实现:读 [t_hash.c](../../redis-8.0.2/src/t_hash.c) 的 `hashTypeSetExInit`/`hashTypeSetEx`/`hashTypeSetExDone`(224-229)、`hfieldGetExpireMeta`(50),它们是 mstr 在生产环境的第一个落地。
- 想看 LFU 的衰减算法细节:读 [evict.c](../../redis-8.0.2/src/evict.c) 的 `LFUDecrAndReturn`(302-308)、`LFULogIncr`(282-290)、`LFUGetTimeInMinutes`(266-268),第十三章 expire/evict 会深入讲这套机制。

### 引出下一章

至此你已经看清"值"的统一容器(robj)和"key 元信息"的抽象框架(mstr)。但 Redis 真正的 keyspace 不只有一个大 dict——为了支持渐进式 rehash、多 DB、cluster slot 分片,8.0 把"key→value"的存储层做了一次大手术,引入了 **kvstore**(一组可分片的 dict 集合)。mstr 给 key 附加元数据,kvstore 给整个 keyspace 附加结构。下一章我们走进这层存储骨架,看 Redis 如何在不阻塞主线程的前提下,把一个可能上亿 entry 的哈希表平滑扩容。

---

## 验证物:如何亲手确认本章的设计

> 说明:本书写作环境为 Windows,无法直接运行 redis-server。以下 (1) gdb 断点脚本 (2) 源码常量锚点 (3) OBJECT ENCODING 观察项 均为可复现的精确指引,供读者在 Linux 环境对 redis-8.0.2 源码 `make no-opt` 编译后自行验证。**本书不附编造的运行输出**——凡未实跑的,只给脚本与预期观察变量,不写具体数值。

### 1. gdb 断点脚本

编译:`cd redis-8.0.2 && make no-opt`(带 -g)
启动:`gdb ./src/redis-server`

```gdb
(gdb) break createObject                    # 通用构造器,object.c:23
(gdb) break createEmbeddedStringObject      # embstr 构造器,object.c:72
(gdb) break createRawStringObject           # raw 构造器,object.c:65
(gdb) break createStringObject              # 按 len 分路,object.c:103
(gdb) break tryObjectEncodingEx             # 编码自适应发动机,object.c:608
(gdb) break dbUnshareStringValueWithDictEntry  # embstr/int→raw 转换,db.c:562
(gdb) break incrRefCount                    # 引用计数 ++,object.c:350
(gdb) break decrRefCount                    # 引用计数 --,object.c:362
(gdb) break makeObjectShared                # 共享对象化,object.c:57
(gdb) break initObjectLRUOrLFU              # LRU/LFU 初始化,object.c:33
(gdb) run --port 6379

# 另开终端 redis-cli 发命令,gdb 会在断点停下:

# 场景一:SET k hello (len=5 ≤ 44,走 embstr)
# redis-cli: SET k hello
(gdb) continue                              # 命中 createStringObject
(gdb) print len                             # 预期:5(<= 44 走 embstr)
(gdb) continue                              # 命中 createEmbeddedStringObject
(gdb) print len                             # 预期:5
(gdb) print o->encoding                     # 预期:8(OBJ_ENCODING_EMBSTR)

# 场景二:SET k2 12345 (数字,触发共享/INT 编码)
# redis-cli: SET k2 12345
(gdb) continue                              # 命中 tryObjectEncodingEx
(gdb) print value                           # 预期:12345(string2l 解析成功)
# 若 maxmemory_policy 不是 LRU/LFU:
(gdb) next                                  # 走 shared.integers[12345] 分支
(gdb) print o                               # 预期:等于 shared.integers[12345]
(gdb) print o->refcount                     # 预期:INT_MAX(OBJ_SHARED_REFCOUNT)
(gdb) print o->encoding                     # 预期:1(OBJ_ENCODING_INT)

# 场景三:APPEND k world (embstr → raw 转换)
# redis-cli: APPEND k world
(gdb) continue                              # 命中 dbUnshareStringValueWithDictEntry
(gdb) print o->encoding                     # 预期:8(EMBSTR,触发 unshare)
(gdb) print o->refcount                     # 预期:1
(gdb) next                                  # 进入 createRawStringObject
(gdb) continue                              # 命中 createRawStringObject
# 后续 o 在 dict 里被替换成 RAW 对象

# 场景四:decrRefCount(共享对象永不释放)
# redis-cli: DEL k
(gdb) continue                              # 命中 decrRefCount
(gdb) print o->refcount                     # 若是共享对象,预期:INT_MAX(直接 return)
```

**预期观察**(基于源码 [object.c:103-108](../../redis-8.0.2/src/object.c#L103) 的分路逻辑、[object.c:608-683](../../redis-8.0.2/src/object.c#L608) 的编码自适应、[db.c:562-571](../../redis-8.0.2/src/db.c#L562) 的 unshare 路径,本书未实跑):短字符串(≤44)走 embstr 一次连续分配;数字走 INT 或共享;APPEND/SETRANGE 触发 embstr→raw 转换;共享对象 refcount 恒为 INT_MAX 不释放。

### 2. 源码常量锚点(带行号,从 redis-8.0.2 源码 Grep 核实)

| 常量/字段 | 位置 | 值/说明 |
|----------|------|---------|
| `redisObject` 结构体 | server.h:1001-1009 | type:4 + encoding:4 + lru:24 + refcount + ptr,共 16 字节 |
| `LRU_BITS` | server.h:994 | 24(lru 字段位宽) |
| `OBJ_SHARED_REFCOUNT` | server.h:998 | INT_MAX(共享对象,永不释放) |
| `OBJ_STATIC_REFCOUNT` | server.h:999 | INT_MAX-1(栈对象,禁止改 refcount) |
| `OBJ_FIRST_SPECIAL_REFCOUNT` | server.h:1000 | OBJ_STATIC_REFCOUNT(特判门槛) |
| `OBJ_SHARED_INTEGERS` | server.h:107 | 10000(共享整数范围 [0,10000)) |
| `OBJ_ENCODING_EMBSTR_SIZE_LIMIT` | object.c:102 | 44(embstr 长度上限,填满 jemalloc 64 字节 arena) |
| `LFU_INIT_VAL` | server.h:3759 | 5(LFU 新 key 频率初值) |
| `MAXMEMORY_FLAG_NO_SHARED_INTEGERS` | server.h:621-622 | LRU|LFU(用淘汰策略时禁用共享) |
| `OBJ_ENCODING_*` 枚举 | server.h:980-992 | RAW=0/INT=1/HT=2/.../EMBSTR=8/LISTPACK=11 |
| `createObject` | object.c:23-31 | 通用构造器,默认 RAW,refcount=1 |
| `createEmbeddedStringObject` | object.c:72-94 | embstr 一次连续分配,写死 sdshdr8(@84) |
| `createStringObject` | object.c:103-108 | 按 len ≤ 44 分路 embstr/raw |
| `tryObjectEncodingEx` | object.c:608-683 | 编码自适应:数字→INT/共享、短串→EMBSTR |
| `dbUnshareStringValueWithDictEntry` | db.c:562-571 | embstr/int/shared → raw 转换 |
| `incrRefCount` | object.c:350-360 | 普通对象 ++,共享 no-op |
| `decrRefCount` | object.c:362-384 | 共享特判 + 引用归零按 type free |
| `makeObjectShared` | object.c:57-61 | 钉死 refcount=INT_MAX(serverAssert==1) |
| `initObjectLRUOrLFU` | object.c:33-44 | 共享跳过;LFU=高16LDT+低8counter;LRU=整24时钟 |
| `LFUGetTimeInMinutes` | evict.c:266-268 | (unixtime/60) & 65535(低16位) |
| `LFULogIncr` | evict.c:282-290 | 概率性对数 +1,p=1/(counter×log_factor+1) |
| `shared.integers` 创建 | server.c:2079-2083 | for [0,10000) makeObjectShared + INT 编码 |
| `struct redisObject` (含位段) | server.h:1001-1009 | type:4/encoding:4/lru:24 拼成 32 bit 头 |

mstr 相关:

| 常量/字段 | 位置 | 值/说明 |
|----------|------|---------|
| mstr 设计注释 | mstr.h:9-30 | "immutable string with optional metadata attached" |
| `MSTR_TYPE_*` 四种头类型 | mstr.h:139-144 | 5/8/16/64(对应 sdshdr 系列) |
| `mstrhdr8.unused` 字段 | mstr.h:167 | "To achieve odd size header"(奇字节填充) |
| `mstrKind` 结构 | mstr.h:186-189 | name + metaSize[16](16 位 mFlags 对应 16 种元数据) |
| `NUM_MSTR_FLAGS` | mstr.h:183 | sizeof(mstrFlags)×8 = 16 |
| `HkeyMetaFlags` 假想例子 | mstr.h:80-100 | 未来 keymeta 的方向(refcount/expire/next/embed) |
| 奇地址保证注释 | mstr.h:119-125 | "sds address is always an odd pointer" |
| `static_assert` 头奇字节 | mstr.h:217-220 | mstrhdr5/8/16/64 % 2 == 1 |
| `static_assert` mFlags 偶字节 | mstr.h:221 | mstrFlags % 2 == 0(保 mstr 指针奇) |
| `HfieldMetaFlags` 实际使用 | t_hash.c:144-146 | HFIELD_META_EXPIRE=0(唯一元数据) |
| `mstrFieldKind` 定义 | t_hash.c:148-154 | metaSize[0]=sizeof(ExpireMeta),要求偶数 |

### 3. OBJECT ENCODING 观察项(需本地 redis-server)

> 以下操作需在 Linux 本地启动 redis-server 后用 redis-cli 执行。本书未实跑,仅列观察方法与预期切换点(常量来自源码,可 `CONFIG GET` 确认相关配置)。

```text
# 观察一:embstr → raw 切换(embstr 上限 44 字节)
127.0.0.1:6379> SET shortk hello                         # len=5,预期 embstr
127.0.0.1:6379> OBJECT ENCODING shortk                   # 预期:embstr
127.0.0.1:6379> SET midk <44 字节字符串>                  # len=44,仍 embstr
127.0.0.1:6379> OBJECT ENCODING midk                     # 预期:embstr
127.0.0.1:6379> SET longk <45 字节字符串>                 # len=45,转 raw
127.0.0.1:6379> OBJECT ENCODING longk                    # 预期:raw

# 观察二:数字走 INT 编码
127.0.0.1:6379> SET numk 12345                           # 数字,走 INT
127.0.0.1:6379> OBJECT ENCODING numk                     # 预期:int
127.0.0.1:6379> SET bignumk 99999999999999999999         # 超 long 范围(20+ 位)
127.0.0.1:6379> OBJECT ENCODING bignumk                  # 预期:embstr 或 raw(超 long 走字符串)

# 观察三:APPEND 触发 embstr → raw 转换
127.0.0.1:6379> SET apndk hi                             # len=2,embstr
127.0.0.1:6379> OBJECT ENCODING apndk                    # 预期:embstr
127.0.0.1:6379> APPEND apndk there                       # 触发 dbUnshareStringValue
127.0.0.1:6379> OBJECT ENCODING apndk                    # 预期:raw(转换后)

# 观察四:共享整数在 LRU/LFU 下被禁用
127.0.0.1:6379> CONFIG SET maxmemory-policy allkeys-lru  # 设 LRU 淘汰
127.0.0.1:6379> SET shrk 42                              # 应禁用共享(LRU 模式)
127.0.0.1:6379> OBJECT ENCODING shrk                     # 预期:int(独占 INT 编码,非共享)
# 用 OBJECT FREQ / OBJECT IDLETIME 验证它有独立 lru:
127.0.0.1:6379> OBJECT IDLETIME shrk                     # 应返回非零数字(独立 lru 字段)
127.0.0.1:6379> CONFIG SET maxmemory-policy noeviction   # 复原

# 观察五:hash 字段 TTL(8.0 mstr 的实际落地)
127.0.0.1:6379> HSET myhash f1 v1                        # 普通 field(可能不带 TTL)
127.0.0.1:6379> HEXPIRE myhash 100 FIELDS 1 f1           # 给 field 设 TTL(8.0 新命令)
127.0.0.1:6379> HPTTL myhash FIELDS 1 f1                 # 预期:非负数(剩余 TTL 毫秒)
```

标注:以上预期基于源码常量([object.c:102](../../redis-8.0.2/src/object.c#L102) 的 44 字节上限、[server.h:107](../../redis-8.0.2/src/server.h#L107) 的 OBJ_SHARED_INTEGERS=10000、[server.h:621-622](../../redis-8.0.2/src/server.h#L621) 的 LRU/LFU 禁用共享、[t_hash.c:148-154](../../redis-8.0.2/src/t_hash.c#L148) 的 hfield mstr 定义)与 [config.c](../../redis-8.0.2/src/config.c) 默认配置推导,本书未在本地实跑;若你的 redis 版本/配置不同,切换点可能偏移,以 `CONFIG GET` 实际值为准。共享整数是否启用,取决于 `maxmemory-policy` 是否含 LRU/LFU 标志位(`MAXMEMORY_FLAG_NO_SHARED_INTEGERS`,见 [server.h:621](../../redis-8.0.2/src/server.h#L621)))。
