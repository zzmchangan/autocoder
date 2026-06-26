# 第 3 篇 · 第 9 章 · 记录格式与动态类型(type affinity)

> **核心问题**:上一章 P3-08 我们拆了 B-tree 页——一棵表 B-tree 的叶子页里,密密麻麻排着一个个 cell,每个 cell 装着一"行"。可这一行**在 cell 里到底长什么样**?SQLite 的一个反直觉事实是:它**不像 MySQL 那样给每列定死类型**。你往一个 `TEXT` 列里插整数 `123`,它存进去就是整数;你往一个 `INTEGER` 列里插字符串 `"456"`,它会被转成整数 `456`。列上写的 `TEXT`/`INTEGER` 不是"强类型",而是"**亲和性**(type affinity)"——一种**柔性偏好**,值最终存什么类型由**值本身**决定。这是 SQLite 区别于几乎所有主流数据库的根本设计。这一章讲清两件事:① 一行数据在 B-tree cell 里的字节布局(Record 格式);② SQLite 凭什么敢"不要强类型",亲和性这套柔性机制在源码里怎么落地。

> **读完本章你会明白**:
> 1. SQLite 的一行,在 B-tree cell 里是一个 **Record**:`record header`(一串 serial type 变长整数)+ `record body`(各列实际值),整条 Record 塞在 table leaf cell 的 payload 区。
> 2. **varint 变长整数**(1-9 字节,每字节高位是续位)是 Record 格式的"原子积木"——它同时编码 serial type、rowid、payload 长度、偏移,把"类型 + 长度 + 值"压到极致紧凑。
> 3. **serial type 编码表**:一个变长整数**同时**编码了"值的类型"和"值的字节长度"——这是 SQLite 记录格式最巧的一笔(0=NULL,1-6=定长整数,7=IEEE 浮点,8/9=常量 0/1,N≥12 偶=BLOB,N≥13 奇=TEXT)。
> 4. **动态类型 + type affinity**:SQLite 没有"列强类型",只有 `SQLITE_AFF_BLOB/TEXT/REAL/INTEGER/NUMERIC/NONE/FLEXNUM` 七种亲和性(注意 3.54 新增了 FLEXNUM);值存什么存储类型(NULL/INTEGER/REAL/TEXT/BLOB)由**值本身**的 serial type 决定,亲和性只在**存入时尝试做一次转换**。
> 5. **rowid 是 table B-tree 的 key**:一行在表 B-tree 里靠 `rowid`(64 位整数)定位;`INTEGER PRIMARY KEY` 是 rowid 的**别名**(不另占存储),这是 SQLite 独有的"主键即行号"机制。

> **如果一读觉得太难**:先只记住三件事——① 一行 = 一个 Record = `header(各列 serial type)+ body(各列值)`;② SQLite 列没有强类型,只有"亲和性"(柔性偏好,存入时试转一次);③ `INTEGER PRIMARY KEY` 就是 rowid,不重复存。其余细节(varint 编码、overflow 页、affinity 转换表)都是这三件事的展开。

---

## 〇、一句话点破

> **SQLite 的 Record 格式,用"一个变长整数同时编码类型 + 长度"这一个巧招,把"一行存什么、每个值多大、什么类型"全部压进一串字节;而"列无强类型、只有亲和性",是它对"嵌入式、数据来源杂、schema 要灵活"这个场景的诚实回答——值存什么类型由值本身决定,列只在存入时温柔地推一把。**

这是结论,不是理由。本章倒过来拆:先看一行在 cell 里到底长什么样(Record 格式 + varint),再讲 serial type 这个"类型 + 长度二合一"的编码表为什么这么巧,然后拆动态类型与亲和性为什么这么设计、在源码里怎么落地,最后讲 rowid 这个 table B-tree 的 key 和 `INTEGER PRIMARY KEY` 别名机制。

---

## 一、从 cell 说起:一行在 B-tree 页里长什么样

上一章 P3-08 我们讲清了 B-tree 页的骨架:每个页有一个 page header、一个 cell pointer array、一片 cell content area,cell 从页尾向页头生长。**每个 cell 装着一个 key(以及它附带的数据)**。这一章的问题是:**这个 cell 里,具体装了什么?**

先看结论:SQLite 有 4 种页(由 page header 的 flag byte 决定),对应 **4 种 cell 布局**。表(table)的 B-tree 用 `PTF_LEAFDATA | PTF_INTKEY`(内部页 0x05、叶子页 0x0d),索引(index)的 B-tree 用 `PTF_ZERODATA`(内部页 0x02、叶子页 0x0a)。这 4 种 cell 布局不一样,源码里由 `xCellSize` / `xParseCell` 两个函数指针区分([btree.c:2055 decodeFlags](../sqlite/src/btree.c#L2055-L2111)):

| 页 flag byte | 页类型 | `xCellSize` | `xParseCell` | cell 里装什么 |
|---|---|---|---|---|
| `0x0d` | 表叶子页 | `cellSizePtrTableLeaf` | `btreeParseCellPtr` | `[payload-size][rowid][payload(=Record)]` |
| `0x05` | 表内部页 | `cellSizePtrNoPayload` | `btreeParseCellPtrNoPayload` | `[child-ptr(4B)][rowid]`(不装 payload) |
| `0x0a` | 索引叶子页 | `cellSizePtrIdxLeaf` | `btreeParseCellPtrIndex` | `[payload-size][payload(=Record,含 key)]` |
| `0x02` | 索引内部页 | `cellSizePtr` | `btreeParseCellPtrIndex` | `[child-ptr(4B)][payload-size][payload]` |

> **钉死这件事**:表 B-tree 的 key 是 **rowid(整数)**,所以表 cell 里专门有个 varint 字段存 rowid;数据(整行 Record)只存在**叶子页**——这就是为什么表叶子页 flag 叫 `PTF_LEAFDATA`(数据只在叶子)。而表**内部页**的 cell 只装 `[child-ptr + rowid]`(**没有 payload**),纯粹用来按 rowid 导航。这点和 MySQL InnoDB 的 B+树(所有数据在叶子页、叶子页有链表)不同——SQLite 的表内部页 cell 里那个 rowid 就是用来二分定位的 key。

我们本章只拆**表叶子页的 cell**(0x0d)——这是"一行"真正住的地方。先看它在源码里怎么被解析出来的:

```c
/* btree.c:1286  表叶子页 cell 解析器(table leaf) */
static void btreeParseCellPtr(
  MemPage *pPage,
  u8 *pCell,
  CellInfo *pInfo
){
  u8 *pIter;
  u64 nPayload;
  u64 iKey;
  assert( pPage->intKeyLeaf );
  assert( pPage->childPtrSize==0 );   /* 叶子页没有 child 指针 */
  pIter = pCell;

  /* 第一个 varint:payload 的总字节数(Record 的总长度) */
  nPayload = *pIter;
  if( nPayload>=0x80 ){ /* 多字节 varint ... */
    ...
  }
  pIter++;

  /* 第二个 varint:rowid(这行的整数 key) */
  iKey = *pIter;
  if( iKey>=0x80 ){ /* 多字节 varint,循环展开优化 ... */
    ...
  }

  pInfo->nKey = *(i64*)&iKey;     /* nKey 字段存 rowid */
  pInfo->nPayload = nPayload;     /* payload 字节数 */
  pInfo->pPayload = pIter;        /* 指向 payload 起点 */
  /* 计算 nLocal:本地存多少,超出走 overflow 页 */
  ...
}
```

([btree.c:1286-1373 btreeParseCellPtr](../sqlite/src/btree.c#L1286-L1373))

这段代码告诉我们一个**表叶子页 cell 的精确布局**:

```
   表叶子页 cell(0x0d)布局:
   ┌──────────────┬──────────┬─────────────────────────────┬──────────────┐
   │ payload-size │  rowid   │  payload(= Record,前 nLocal)│ overflow 链  │
   │  (varint)    │ (varint) │     字节存本地,其余存这    │ (若有,4B 指针)│
   └──────────────┴──────────┴─────────────────────────────┴──────────────┘
                  ←——— cell 在页里的边界 ———→
   第 1 个 varint:payload 总字节数(= 这条 Record 的长度)
   第 2 个 varint:rowid(这行的整数 key,也是 B-tree 排序的依据)
   紧接着:payload 的前 nLocal 字节存在本页
   若 payload > maxLocal:最后 4 字节是第一个 overflow 页的页号
```

> **不这样会怎样**:为什么 rowid 要单独存在 cell 里,不塞进 payload(Record)?因为表 B-tree 的**内部页**要靠 rowid 二分查找——内部页 cell 里 `[child-ptr + rowid]` 也没有 payload。如果 rowid 藏在 payload 里,内部页导航时就得解 Record,太慢。把 rowid 提到 cell 头部、用专门的 varint 存,是为了**让 B-tree 的导航(按 rowid 比大小)完全不用碰 payload**。这是"key 和 data 分开编码"的精妙之处。

### payload 太大怎么办:overflow 页

一行如果很长(比如存了个几 MB 的 BLOB),payload 会超过一页能装下的上限(`maxLocal`)。这时 SQLite 把 payload 拆成两部分:**前 `nLocal` 字节存本地 cell**,剩下的存一条 **overflow 页链**(每个 overflow 页存 `usableSize - 4` 字节,最后 4 字节是下一个 overflow 页的页号)。溢出量的分配公式见 [btree.c:1205 btreeParseCellAdjustSizeForOverflow](../sqlite/src/btree.c#L1205-L1234):

```c
/* btree.c:1219-1233  overflow 分配 */
minLocal = pPage->minLocal;
maxLocal = pPage->maxLocal;
surplus = minLocal + (pInfo->nPayload - minLocal)%(pPage->pBt->usableSize-4);
if( surplus <= maxLocal ){
  pInfo->nLocal = (u16)surplus;
}else{
  pInfo->nLocal = (u16)minLocal;   /* 本地只留最少,多溢出 */
}
pInfo->nSize = (u16)(&pInfo->pPayload[pInfo->nLocal] - pCell) + 4;
```

这个公式看着怪,它的目的是**让最后一个 overflow 页尽可能填满**(减少碎片)。读取时,`accessPayload`([btree.c:5148](../sqlite/src/btree.c#L5148-L5260))先从本地读 `nLocal` 字节,不够再沿着 overflow 链一页页读:

```c
/* btree.c:5187-5258  accessPayload 读 payload(简化) */
if( offset < pCur->info.nLocal ){
  /* 先从本地页读 */
  a = min(amt, pCur->info.nLocal - offset);
  copyPayload(&aPayload[offset], pBuf, a, ...);
  offset = 0;  pBuf += a;  amt -= a;
}else{
  offset -= pCur->info.nLocal;
}
if( amt>0 ){
  /* 本地不够,沿着 overflow 链读 */
  const u32 ovflSize = pBt->usableSize - 4;   /* 每个 ovfl 页的有效字节数 */
  nextPage = get4byte(&aPayload[pCur->info.nLocal]);  /* 第一个 ovfl 页号 */
  while( nextPage ){
    ... 读这一页的 ovflSize 字节,nextPage = 这页末尾的 4 字节页号 ...
  }
}
```

([btree.c:5148-5260 accessPayload](../sqlite/src/btree.c#L5148-L5260))

> **钉死这件事**:一行(payload)在 B-tree 里是一个**逻辑上连续、物理上可能跨页**的字节串。短行全在本地 cell;长行的前一段在本地,后段挂在 overflow 页链上。VDBE 的 `OP_Column` 读列时,通过 `sqlite3BtreePayload` / `sqlite3BtreePayloadFetch` 这两个接口拿 payload——前者**拷贝**(跨 overflow 页也能拼回来),后者**直接返回本地指针**(零拷贝,但只对本地部分有效)。这是性能上的两条路径,后文会讲。

好,现在我们知道了:一个表叶子页 cell = `[payload-size varint][rowid varint][payload(= Record,可能溢出)]`。**接下来真正的问题是:payload 里那个"Record",也就是一行数据,内部长什么样?** 这才是本章的主角。

---

## 二、Record 格式:header + body,一行数据的字节布局

### Record 长什么样:一张图说清

SQLite 的一行(在 payload 区里)叫一个 **Record**,它的格式极其简洁——**header + body**:

```
   一条 Record 的字节布局(就是 payload 的内容):
   ┌──────────┬────────┬────────┬─────┬────────┬───────┬───────┬─────┬───────┐
   │ hdr-size │ type 0 │ type 1 │ ... │ type N │ data0  │ data1 │ ... │ dataN │
   │  (varint)│(varint)│(varint)│     │(varint)│        │       │     │       │
   └──────────┴────────┴────────┴─────┴────────┴───────┴───────┴─────┴───────┘
   ├─────── record header ────────┤    ├─────── record body ───────────────┤
                                    │
                      hdr-size = 从 record 起点到 data0 的偏移(含自己)
```

([vdbe.c:3520-3534 OP_MakeRecord 注释](../sqlite/src/vdbe.c#L3520-L3534) 原文给出了这个布局)

这个设计有几个关键点:

1. **header 是一串 varint**:第一个 varint 是 `hdr-size`(整个 header 的字节数,含自己);后面每个 varint 是一列的 **serial type**。
2. **body 紧跟 header**:每个 `data i` 是第 i 列的实际值,长度和类型由 header 里的 `type i` 决定。
3. **header 和 body 分离**:所有列的类型信息(type 0…type N)全堆在前面,实际值全堆在后面。这点很重要——**它和"每列 type 后紧跟 data"的交错布局不同**。

> **不这样会怎样**:为什么要把所有 type 堆在 header、所有 data 堆在 body,而不是 `type0|data0|type1|data1|...` 交错?因为**读第 N 列时,如果 type 和 data 交错,要跳过前 N-1 个 data 才能定位——而每个 data 长度不一,跳过它们得先解析前 N-1 个 type**。SQLite 的布局下,要拿第 N 列,只需要**扫一遍前 N 个 type**(全在 header,紧凑),累加出前 N-1 个 data 的总长度,就能从 body 里偏移定位。header 越紧凑,扫得越快。这是一个为"按列随机访问"优化的布局。

### 一个真实例子:一行 `(42, "alice", 3.14)` 的 Record 字节布局

我们具体算一遍。假设表是 `CREATE TABLE t(id INTEGER, name TEXT, score REAL)`,插一行 `(42, "alice", 3.14)`。这条 Record 长什么样?

**第一步,确定每列的 serial type**(对照后文的 serial type 表):
- 列 0 `id=42`:42 是整数,且 42 ≤ 127 但 42 不是 0/1(8/9 是常量)、`i&1==i` 不成立(42 是偶数但 42≠0),所以走 1 字节整数 → serial type = **1**。
- 列 1 `name="alice"`:5 字节文本,N=5,serial type = `5*2 + 12 + 1 =` **23**。
- 列 2 `score=3.14`:浮点 → serial type = **7**。

**第二步,确定 body 长度**:
- 列 0:1 字节(serial type 1 → 1 字节整数)。
- 列 1:5 字节("alice")。
- 列 2:8 字节(IEEE 双精度)。
- body 总长 = 1 + 5 + 8 = **14 字节**。

**第三步,确定 header**:
- 三个 serial type 各占 1 字节 varint(1、23、7 都 < 128)→ 3 字节。
- 加上 `hdr-size` 自己 1 字节(varint)→ header 总长 4 字节。
- 所以 `hdr-size` 的值 = **4**。

**最终的 Record 字节布局**(十六进制):

```
   偏移:  0    1    2    3    4    5  6 7 8 9    10 11 12 13 14 15 16 17
        ┌────┬────┬────┬────┬────┬──────────────┬─────────────────────────┐
   字节: │ 04 │ 01 │ 17 │ 07 │ 2A │ 61 6C 69 63 65│ 1F 85 EB 51 B8 1E 09 40│
        └────┴────┴────┴────┴────┴──────────────┴─────────────────────────┘
         │    │    │    │    │    │              │
         │    │    │    │    │    └─ "alice"(5B) │
         │    │    │    │    └─ id=42(0x2A,1B)    └─ 3.14 的 IEEE 双精度(8B,小端)
         │    │    │    └─ score 的 serial type = 7(REAL)
         │    │    └─ name 的 serial type = 23(TEXT, 5 字节)
         │    └─ id 的 serial type = 1(1 字节整数)
         └─ hdr-size = 4(整个 header 4 字节)
```

> 注意:整数在 body 里是**大端序**(big-endian),浮点是 **IEEE 754 双精度**(也是大端,除非编译时定义 `SQLITE_MIXED_ENDIAN_64BIT_FLOAT`)。源码里 `serialGet`([vdbeaux.c:4078-4100](../sqlite/src/vdbeaux.c#L4078-L4100))用 `FOUR_BYTE_UINT(buf)` 按大端读 4 字节。这是为了让**字节序比较 = 数值比较**(大端整数的前缀就是高位,逐字节比就能比出大小,B-tree 内部页按 key 排序时直接比字节即可,不用反序列化)。

> **钉死这件事**:SQLite 的 Record 是**自描述的紧凑字节流**——header 里每列一个 varint(serial type)同时告诉你"这列是什么类型 + 占几字节",body 里按序堆实际值。**没有对齐填充、没有冗余长度字段**。这一条 18 字节的 Record,如果用"每列 4 字节定长 type + 8 字节定长 data"的朴素布局,至少 36 字节,翻倍。SQLite 为嵌入式省空间的执念,在这体现得淋漓尽致。

### 怎么造出一条 Record:OP_MakeRecord

VDBE 执行 `INSERT` 时,会把要插入的各列值(在寄存器里)拼成一条 Record,用的就是 `OP_MakeRecord` 这个 opcode([vdbe.c:3504](../sqlite/src/vdbe.c#L3504-L3745))。它的逻辑分三步:

**第一步,应用亲和性**(后文详述):把各列寄存器的值,按列定义的 affinity 做一次柔性转换。

```c
/* vdbe.c:3553-3565  对每个输入寄存器应用 affinity */
if( zAffinity ){
  pRec = pData0;
  do{
    applyAffinity(pRec, zAffinity[0], encoding);
    /* 特殊处理:REAL affinity 收到整数,标记成 IntReal(存时按整数存,
       但语义上是实数,这样省 8 字节) */
    if( zAffinity[0]==SQLITE_AFF_REAL && (pRec->flags & MEM_Int) ){
      pRec->flags |= MEM_IntReal;
      pRec->flags &= ~(MEM_Int);
    }
    zAffinity++;
    pRec++;
  }while( zAffinity[0] );
}
```

**第二步,从后往前扫,算每个值的 serial type 和总长度**(注意是从最后一列往前扫,这是个小优化):

```c
/* vdbe.c:3608-3701  算每列 serial type(简化,保留关键分支) */
pRec = pLast;
do{
  if( pRec->flags & MEM_Null ){
    pRec->uTemp = 0;          /* NULL → serial type 0 */
    nHdr++;
  }else if( pRec->flags & (MEM_Int|MEM_IntReal) ){
    i64 i = pRec->u.i;
    u64 uu = i<0 ? ~i : i;    /* 取绝对值(用 ~ 而非 -,避免 INT64_MIN 溢出) */
    if( uu<=127 ){
      if( (i&1)==i && p->minWriteFileFormat>=4 ){
        pRec->uTemp = 8+(u32)uu;   /* 0→serial 8, 1→serial 9, 0 字节存! */
      }else{
        nData++; pRec->uTemp = 1;  /* 1 字节整数 */
      }
    }else if( uu<=32767 ){ nData+=2; pRec->uTemp = 2; }
    else if( uu<=8388607 ){ nData+=3; pRec->uTemp = 3; }
    else if( uu<=2147483647 ){ nData+=4; pRec->uTemp = 4; }
    else if( uu<=140737488355327LL ){ nData+=6; pRec->uTemp = 5; }
    else { nData+=8; pRec->uTemp = 6; }
    nHdr++;
  }else if( pRec->flags & MEM_Real ){
    nHdr++; nData += 8; pRec->uTemp = 7;   /* IEEE 双精度 */
  }else{  /* Str 或 Blob */
    len = (u32)pRec->n;
    serial_type = (len*2) + 12 + ((pRec->flags & MEM_Str)!=0);
    nData += len;
    nHdr += sqlite3VarintLen(serial_type);
    pRec->uTemp = serial_type;
  }
  if( pRec==pData0 ) break;
  pRec--;
}while(1);
```

([vdbe.c:3608-3701](../sqlite/src/vdbe.c#L3608-L3701))

注意这里有个**极巧的优化**:整数 0 和 1 被编码成 serial type **8 和 9**——**0 字节存储**(值藏在 serial type 里)!这两个 serial type 是 3.3.0(文件格式 4)加的,因为 `0` 和 `1` 在数据库里出现频率极高(布尔标志、状态位),用 0 字节存,等于"省下一个 data 字节,只多一个 header 里的 1 字节 type"——而且如果一列全是 0/1,header 里那个 `8` 或 `9` 也可以和别的列共享。这是 SQLite 抠空间抠到极致的体现。

**第三步,把 header 和 body 写到输出寄存器的 buffer 里**:

```c
/* vdbe.c:3746-3777  写 Record(简化) */
zHdr = (u8 *)pOut->z;
zPayload = zHdr + nHdr;
/* 写 header:先 hdr-size */
if( nHdr<0x80 ){
  *(zHdr++) = nHdr;
}else{
  zHdr += sqlite3PutVarint(zHdr, nHdr);
}
/* 再逐列写 serial type + data */
while( 1 ){
  serial_type = pRec->uTemp;
  if( serial_type<=7 ){
    *(zHdr++) = serial_type;     /* 小 serial type 直接 1 字节 */
    if( serial_type==0 ){ /* NULL,不写 data */ }
    else if( serial_type==7 ){ /* 8 字节浮点,memcpy + 字节序处理 */ }
    else { /* 1/2/3/4/6 字节整数,大端写入 */ }
  }else{
    zHdr += sqlite3PutVarint(zHdr, serial_type);
    /* 写 BLOB / TEXT 的原始字节 */
  }
  ...
}
```

([vdbe.c:3746-3800](../sqlite/src/vdbe.c#L3746-L3800))

> **钉死这件事**:`OP_MakeRecord` 是 Record 的"生产端"。它接收一组寄存器(各列值),先应用 affinity,再为每个值挑最小的 serial type,最后把 header 和 body 拼成一条紧凑字节流,放进输出寄存器(标记成 `MEM_Blob`)。这条字节流随后会被 `OP_Insert` 写进 B-tree cell 的 payload 区。**Record 格式是 SQLite "存"这一面的核心数据结构**——理解了它,你就理解了一行在磁盘上到底占多少字节、为什么。

### 怎么读出一条 Record:OP_Column 反序列化

有"生产"就有"消费"。`OP_Column` 是 VDBE 里最高频的 opcode 之一(读一列),它要从 B-tree cell 的 payload 里取出第 N 列。核心是 `sqlite3VdbeRecordUnpack`([vdbeaux.c:4249-4292](../sqlite/src/vdbeaux.c#L4249-L4292))——它把一条 Record 解成一组 `Mem`(每个 Mem 一列):

```c
/* vdbeaux.c:4249-4292  Record 反序列化(简化) */
void sqlite3VdbeRecordUnpack(
  int nKey, const void *pKey, UnpackedRecord *p
){
  const unsigned char *aKey = (const unsigned char *)pKey;
  u32 d, idx, szHdr;
  Mem *pMem = p->aMem;

  idx = getVarint32(aKey, szHdr);   /* 读 hdr-size */
  d = szHdr;                         /* d = body 起点 */
  while( idx<szHdr && d<=(u32)nKey ){
    u32 serial_type;
    idx += getVarint32(&aKey[idx], serial_type);   /* 读这一列的 serial type */
    sqlite3VdbeSerialGet(&aKey[d], serial_type, pMem);  /* 解出值放进 pMem */
    d += sqlite3VdbeSerialTypeLen(serial_type);         /* 累加 body 偏移 */
    if( (++u)>=p->nField ) break;
    pMem++;
  }
}
```

([vdbeaux.c:4249-4292 sqlite3VdbeRecordUnpack](../sqlite/src/vdbeaux.c#L4249-L4292))

注意 `sqlite3VdbeSerialGet` 这个函数——它根据 serial type,从字节流里解出对应类型的值塞进 `Mem`。比如 serial type 1 读 1 字节有符号整数、serial type 7 读 8 字节 IEEE 浮点、serial type 23 算出 `(23-13)/2 = 5` 字节文本。**serial type 是编解码两端的"共享契约"**——写端用 `OP_MakeRecord` 编,读端用 `sqlite3VdbeSerialGet` 解,中间的字节流完全自描述。

> **钉死这件事**:Record 的"读写对称"是它最优雅的地方。写端逐列挑 serial type → 拼 header+body;读端扫 header 的 serial type → 按 type 长度跳偏移解 body。**两边都不需要外部 schema 就能工作**(schema 只在"挑 serial type"和"affinity 转换"时用)——这也是为什么 SQLite 的 `.db` 文件即使没有 schema 也能被工具部分解析(每个 cell 自描述)。

---

## 三、varint:Record 格式的原子积木

你大概注意到了:Record 里**几乎所有数字**(hdr-size、serial type、payload-size、rowid)**都是 varint**。varint 是 SQLite 记录格式的"原子积木",值得单独拆透。

### varint 怎么编码:续位 + 7 位一组

varint(variable-length integer)的思想很简单:**用一个字节的最高位做"续位标志"**,剩下 7 位存数据。连续读字节,直到遇到最高位为 0 的字节为止。最多 9 字节(因为 64 位 = 8×7 + 8,前 8 字节各贡献 7 位,第 9 字节贡献全部 8 位)。

```
   varint 编码规则:
   - 第 1-8 字节:最高位(bit 7)是"续位"(1=还有后续,0=结束),低 7 位存数据
   - 第 9 字节(若到):全部 8 位都是数据(不再有续位,因为已经到 64 位上限)
   - 数据按"先存高位"排列(大端)

   数值范围与字节数:
   0 - 127              → 1 字节      (0x00 - 0x7F)
   128 - 16383          → 2 字节
   16384 - 2097151      → 3 字节
   ... 每多 1 字节,多 7 位 ...
   2^56 - 2^64-1        → 9 字节      (第 9 字节存满 8 位)
```

编码函数 `sqlite3PutVarint`([util.c:1610-1621](../sqlite/src/util.c#L1610-L1621))对常见的小值做了特判快速路径:

```c
/* util.c:1610  编码 varint */
int sqlite3PutVarint(unsigned char *p, u64 v){
  if( v<=0x7f ){                 /* 1 字节:直接写 */
    p[0] = v&0x7f;
    return 1;
  }
  if( v<=0x3fff ){               /* 2 字节:两段拼 */
    p[0] = ((v>>7)&0x7f)|0x80;   /* 第一字节高位=1(续位),低 7 位是高位数据 */
    p[1] = v&0x7f;                /* 第二字节高位=0(结束),低 7 位是低位数据 */
    return 2;
  }
  return putVarint64(p,v);        /* 3-9 字节:通用路径 */
}
```

大于 2 字节的走 `putVarint64`([util.c:1586-1609](../sqlite/src/util.c#L1586-L1609)),逻辑是:先按 7 位一组切(从低到高),每组成一个字节(高位或上 `0x80` 续位),最后翻成大端序写入。

### 一个例子:rowid 300 怎么编码成 2 字节

rowid 是 64 位整数,在 cell 里用 varint 存。假设某行的 rowid = **300**:

- 300 的二进制 = `0b100101100`(9 位)。
- 按 7 位切(从低到高):低 7 位 = `0101100`(44),高 2 位 = `100`(4)。
- 高字节(先存,大端):`4 | 0x80 = 0x84`(续位置 1)。
- 低字节(后存):`44 = 0x2C`(续位置 0,结束)。
- 最终 2 字节:**`84 2C`**。

解码 `sqlite3GetVarint`([util.c:1640-1670](../sqlite/src/util.c#L1640-L1670))读回来:`(0x84 & 0x7f)<<7 | 0x2C = 4<<7 | 44 = 512+44 = 300`。✓

> **钉死这件事**:varint 把"小数用少字节、大数用多字节"这件事做到了极致。SQLite 里绝大多数 rowid、serial type、payload 长度都是小数(rowid 通常是递增的小整数,serial type 大多是 0-9 的小数,payload 长度对小行也不大)。用 varint,这些小数只占 1 字节——比起定长 8 字节,省了 7/8 的空间。这是一个"用解码复杂度换存储紧凑"的典型工程取舍,在嵌入式场景(存储贵、CPU 富余)极其划算。

### varint 解码的极限优化

varint 解码是 SQLite 的**超级热路径**(每读一列都要解好几个 varint)。源码里对它做了**三层优化**:

1. **单字节快路径**:`sqlite3GetVarint` 第一行 `if( ((signed char*)p)[0]>=0 )` ——如果第一个字节的最高位是 0(即 < 128),直接返回 1 字节、值就是 `p[0]`,根本不进循环([util.c:1643-1646](../sqlite/src/util.c#L1643-L1646))。绝大多数 varint 走这条路径。

2. **双字节快路径**:同理,第二个字节最高位为 0 就返回 2 字节([util.c:1647-1650](../sqlite/src/util.c#L1647-L1650))。

3. **循环展开**:在 B-tree cell 解析(`btreeParseCellPtr`)里,varint 解码被**完全内联 + 循环展开**成 9 层嵌套 `if`([btree.c:1325-1351](../sqlite/src/btree.c#L1325-L1351))——避免函数调用开销、避免循环判断开销。源码注释明确说 `This routine is a high-runner`(高频函数)。

> **不这样会怎样**:如果 varint 解码朴素地写成一个 `while` 循环,每次判断续位、每次移位、每次查表,在热路径上累计开销惊人。SQLite 把它内联展开,让 GCC 能把整段优化成无分支的位运算序列。这是 C 语言"为热路径手写汇编级代码"的典范——承接《Lua》虚拟机里 `luaV_execute` 用 `goto` 跳转避免 switch-case 分支预测失败的同款思路。

---

## 四、serial type:用一个变长整数同时编码类型 + 长度

varint 是积木,serial type 就是这些积木搭出的"类型系统"。**serial type 是 SQLite 记录格式最巧的一笔**——它用**一个变长整数同时编码了"值的类型"和"值的字节长度"**。

### 完整的 serial type 编码表

这是 SQLite 记录格式的"宪法",照源码注释([vdbeaux.c:3877-3896](../sqlite/src/vdbeaux.c#L3877-L3896))逐字核对:

| serial type | 字节数 | 存储类型 | 说明 |
|---|---|---|---|
| **0** | 0 | NULL | 空值 |
| **1** | 1 | INTEGER | 8 位有符号整数(大端) |
| **2** | 2 | INTEGER | 16 位有符号整数(大端) |
| **3** | 3 | INTEGER | 24 位有符号整数(大端) |
| **4** | 4 | INTEGER | 32 位有符号整数(大端) |
| **5** | **6** | INTEGER | 48 位有符号整数(大端,跳过 5 字节) |
| **6** | 8 | INTEGER | 64 位有符号整数(大端) |
| **7** | 8 | REAL | IEEE 754 双精度浮点(大端) |
| **8** | 0 | INTEGER | **常量 0**(值藏在 type 里,0 字节!) |
| **9** | 0 | INTEGER | **常量 1**(值藏在 type 里,0 字节!) |
| 10, 11 | — | (保留) | 内部用(serial type 10 = NULL+Zero,虚拟表 nochange) |
| **N ≥ 12 且偶** | (N-12)/2 | **BLOB** | 二进制,(N-12)/2 字节 |
| **N ≥ 13 且奇** | (N-13)/2 | **TEXT** | 文本,(N-13)/2 字节 |

> **易翻车坑**:**N≥12 偶数 = BLOB,N≥13 奇数 = TEXT**。注意不是"N≥12 是 BLOB、N≥13 是 TEXT"——是看**奇偶**。偶数(12,14,16,...)是 BLOB,奇数(13,15,17,...)是 TEXT。公式见 [vdbeaux.c:3995-4003 sqlite3VdbeSerialTypeLen](../sqlite/src/vdbeaux.c#L3995-L4003):
> ```c
> u32 sqlite3VdbeSerialTypeLen(u32 serial_type){
>   if( serial_type>=128 ){
>     return (serial_type-12)/2;    /* 奇偶都套这个,因为 (奇-12) 和 (偶-13) 都对 */
>   }else{
>     return sqlite3SmallTypeSizes[serial_type];  /* 小值查表 */
>   }
> }
> ```
> 而**编码端**(决定一个值用哪个 serial type)见 `OP_MakeRecord` 里 [vdbe.c:3685](../sqlite/src/vdbe.c#L3685):
> ```c
> serial_type = (len*2) + 12 + ((pRec->flags & MEM_Str)!=0);
> /*  len 字节的 BLOB → 2*len+12(偶)
>     len 字节的 TEXT → 2*len+13(奇)  */
> ```
> 之所以 BLOB 用偶、TEXT 用奇,是因为 `len*2 + 12` 必为偶,`+1` 后必为奇——用"乘 2"这一步保证了 type 的**奇偶性直接区分 BLOB/TEXT**,解码时 `type & 1` 就知道是 TEXT 还是 BLOB,`(type-12)>>1`(或 `(type-13)>>1`)拿到长度。**一个变长整数,既编码了类型(BLOB vs TEXT),又编码了长度**——这是 serial type 设计的精髓。

### serial type 1-6:为什么整数有 5 种长度

注意整数没有"5 字节"的 serial type——1/2/3/4/**6**/8(跳过 5 字节)。为什么跳过 5 字节?因为 4 字节(32 位)能覆盖到 ±21 亿,6 字节(48 位)能覆盖到 ±140 万亿,中间的 5 字节(40 位)覆盖范围(±兆)对实际数据没什么用——**省下一个 serial type 编号,给系统留余地**(虽然现在 10/11 还是保留)。这也让 serial type 的小值段(0-11)更紧凑。

整数长度选择逻辑见 `OP_MakeRecord` [vdbe.c:3627-3676](../sqlite/src/vdbe.c#L3627-L3676) 和 `sqlite3VdbeSerialType` [vdbeaux.c:3911-3969](../sqlite/src/vdbeaux.c#L3911-L3969):根据值的绝对值范围,挑能装下的最小整数类型。一个关键细节是它用 `~i` 而不是 `-i` 取负数的绝对值——避免 `INT64_MIN` 取负溢出(`~INT64_MIN = INT64_MAX`,安全)。

> **钉死这件事**:整数 serial type 的多档(1/2/3/4/6/8 字节),让 SQLite 能**按值的实际大小动态选最省的存储**。存个状态码 `1` 用 serial type 9(0 字节),存个时间戳 `1700000000` 用 serial type 4(4 字节),存个 64 位大整数才用 serial type 6(8 字节)。比起 MySQL 定长 `BIGINT` 永远 8 字节,SQLite 在整数列上平均省 50% 以上空间。这是嵌入式数据库的生存哲学:**每一字节都要挣回来**。

### serial type 8 和 9:0 字节存 0 和 1

最绝的是 serial type 8 和 9——**用 0 字节存常量 0 和 1**。值完全藏在 serial type 里:

```c
/* vdbeaux.c:4078 serialGet 解 serial type 8/9(实际走 default 分支前的小整数处理) */
/* 读端:遇到 serial type 8 → 值是 0;遇到 9 → 值是 1,不读任何 data 字节 */
```

写端的判断见 [vdbe.c:3644-3650](../sqlite/src/vdbe.c#L3644-L3650):

```c
if( uu<=127 ){
  if( (i&1)==i && p->minWriteFileFormat>=4 ){
    pRec->uTemp = 8+(u32)uu;   /* i==0 → serial 8, i==1 → serial 9, 0 字节! */
  }else{
    nData++; pRec->uTemp = 1;  /* 其他 [-127,127] 的整数 → 1 字节 */
  }
}
```

注意 `(i&1)==i` 这个条件——只有 `i==0` 或 `i==1` 时成立(`0&1=0=0`,`1&1=1=1`,其他值不满足)。这是位运算判断"是否是 0 或 1"的极速写法。而且 `8+(u32)uu` 直接算出 serial type(`uu=0→8`,`uu=1→9`),不用 if-else。

> **不这样会怎样**:布尔列、状态标志、是否删除标记……数据库里 `0` 和 `1` 出现的频率高得离谱。如果朴素地用 serial type 1(1 字节整数),每行每个布尔列至少 1 字节 data + 1 字节 header里的 type = 2 字节。用 serial type 8/9,只剩 header 里 1 字节 type,省了一半。这个优化在 3.3.0(2006 年)加进来,十几年证明了对实际工作负载收益巨大。这是"用类型编号的富余空间(0-11 这段小值段)编码高频常量值"的教科书级设计。

### serial type 小值查表:sqlite3SmallTypeSizes

为了极速解码,SQLite 把 serial type 0-127 的字节数**预计算成一张 128 项的查表**——`sqlite3SmallTypeSizes`([vdbeaux.c:3975-3990](../sqlite/src/vdbeaux.c#L3975-L3990)):

```c
const u8 sqlite3SmallTypeSizes[128] = {
        /*  0   1   2   3   4   5   6   7   8   9 */
/*   0 */   0,  1,  2,  3,  4,  6,  8,  8,  0,  0,   /* NULL,1B,2B,3B,4B,6B,8B,FLOAT,0,1 */
/*  10 */   0,  0,  0,  0,  1,  1,  2,  2,  3,  3,   /* 10/11 保留,12/13=0BLOB/0TEXT,14/15=1B... */
/*  20 */   4,  4,  5,  5,  6,  6,  7,  7,  8,  8,
  ...                                                       /* 每 2 项一组,BLOB 和 TEXT 交替 */
};
```

`sqlite3VdbeSerialTypeLen`([vdbeaux.c:3995-4003](../sqlite/src/vdbeaux.c#L3995-L4003))对 < 128 的 serial type 直接查表(O(1) 数组访问),≥ 128 的才走公式 `(serial_type-12)/2`:

```c
u32 sqlite3VdbeSerialTypeLen(u32 serial_type){
  if( serial_type>=128 ){
    return (serial_type-12)/2;
  }else{
    assert( serial_type<12
            || sqlite3SmallTypeSizes[serial_type]==(serial_type-12)/2 );
    return sqlite3SmallTypeSizes[serial_type];   /* 查表 */
  }
}
```

> **钉死这件事**:serial type 设计的**三个巧妙**——① 一个 varint 同时编码类型 + 长度(省了独立的 length 字段);② 奇偶区分 BLOB/TEXT(用 `&1` 一眼区分);③ 小值段预计算查表(热路径 O(1))。这三招合起来,让 SQLite 的 Record 格式在"紧凑"和"解码快"两个矛盾目标上都做到极致。

---

## 五、动态类型:为什么 SQLite 不像 MySQL 那样定死列类型

讲完了 Record 的字节布局,我们回到本章的另一个核心问题:**SQLite 为什么"类型宽松"?**

### 现象:SQLite 的列没有强类型

如果你从 MySQL 过来,SQLite 的类型行为会让你震惊。建表:

```sql
CREATE TABLE t(a INTEGER, b TEXT, c REAL);
INSERT INTO t VALUES(123, 456, 'hello');   -- 往 TEXT 列插数字,往 REAL 列插文本
INSERT INTO t VALUES('xyz', 3.14, 99);
SELECT typeof(a), typeof(b), typeof(c) FROM t;
```

MySQL 会报类型错误(或隐式转换成列类型)。SQLite 不报错,而且 `typeof()` 返回的是**值本身的类型**,不是列定义的类型:

```
第一行:integer, integer, text       -- a 存整数(原样),b 被转成整数,c 是文本(没转成 REAL!)
第二行:text, real, integer          -- a 是文本(没转成 INTEGER!),b 被转成 real,c 是整数
```

> **钉死这件事**:SQLite 的核心事实是——**值存什么存储类型,由值本身决定,不由列定义决定**。一个值插进任何列,SQLite 会根据列的"亲和性"**尝试**做一次转换,但如果转换不合适(比如把 `'hello'` 往 REAL 列插,转不成数字),就**保留原类型**。这是"柔性偏好",不是"强制类型"。SQLite 的存储类型只有 5 种:**NULL / INTEGER / REAL / TEXT / BLOB**——这是值级别的类型,和列定义的类型名(TEXT/INTEGER/REAL/NUMERIC/BLOB/...)是两套东西。

### 五种存储类型 vs 列类型名:两套体系

这里有个极易混淆的点,务必讲清——SQLite 有**两套"类型"概念**:

| 概念 | 归属 | 取值 | 由什么决定 |
|---|---|---|---|
| **存储类型(storage class)** | **值级别** | NULL / INTEGER / REAL / TEXT / BLOB | **值本身**(序列化时挑 serial type) |
| **列类型名(column type)** | **列级别** | TEXT / INTEGER / REAL / NUMERIC / BLOB / (无类型) | `CREATE TABLE` 时声明(但只影响 affinity) |

值在磁盘上,存的是"存储类型"(由 serial type 编码);列定义里写的 `INTEGER`/`TEXT`,实际上只用来推导出一个 **affinity**(亲和性),亲和性再在**值存入时尝试转换**。这两套不能混为一谈——`typeof()` 返回的是前者,`CREATE TABLE` 写的是后者(推导成 affinity)。

> **承接《MySQL·InnoDB》**:MySQL 的列是**强类型**——`VARCHAR(255)` 必须是变长字符串,插整数会隐式转成字符串,长度超了截断。InnoDB 的行格式 REDUNDANT/COMPACT/DYNAMIC,每列的长度和类型由 schema 定死。SQLite 走了完全相反的路:**列只有亲和性,值存什么由值定**——这是嵌入式、数据来源杂、schema 要灵活的取舍。MySQL 那本(P3 讲 InnoDB 行格式处)讲的是强类型那套,本书只在这里一句话点出对照,不重复讲 MySQL 行格式。

### 为什么 SQLite 敢不要强类型

这是一个设计决策,值得讲透动机。MySQL/PG 要强类型,是因为:

1. **C/S + 多应用共享**:多个应用往同一个库写,强类型是"契约",防止一个应用写错格式污染另一个应用。
2. **查询优化需要类型**:优化器要知道列类型才能选索引、估算基数(MySQL 的统计信息按类型分)。
3. **存储紧凑**:定长列(如 `INT`)直接定长存,变长列长度记在行头,解析快。

SQLite 反过来:

1. **嵌入式 + 单应用独占**:数据库就嵌在你的 App 里,数据来源就是你的代码——**你自己的代码写错了类型,是你的事,不是数据库的事**。强类型契约在这里价值不大。
2. **schemaless 友好**:很多场景(配置存储、缓存、日志)数据结构会变,强类型反而是负担。SQLite 允许你 `CREATE TABLE t(a, b, c)`(列没类型,等价于 BLOB affinity),甚至允许一列里混存不同类型——这对"我不想提前设计 schema"的场景极友好。
3. **动态类型其实更省空间**:同一个"金额"列,有的行存整数 100(1 字节 serial type),有的行存浮点 99.99(8 字节 serial type 7)——按值的实际大小选最省的 serial type,比 MySQL 定长 `DECIMAL(10,2)` 永远 5 字节,在值小时反而省。

> **不这样会怎样**:如果 SQLite 学 MySQL 用强类型,会发生什么?① 你往 `INTEGER` 列插字符串,直接报错——但嵌入式场景很多时候数据来源杂(CSV 导入、JSON 解析),报错体验差;② 每列定长或定类型,失去"按值大小挑 serial type"的省空间优势;③ schema 演进困难(加列改类型要 ALTER TABLE 全表重写)。SQLite 选择"动态类型 + 亲和性",是用"放弃强约束"换"灵活 + 紧凑"——这对嵌入式是对的,对 C/S 多应用共享是错的(所以 MySQL/PG 不这么干)。

> **钉死这件事**:**强类型 vs 动态类型,不是谁更先进,而是不同场景的取舍**。MySQL/PG 的强类型服务于"C/S + 多应用 + 查询优化",SQLite 的动态类型服务于"嵌入式 + 灵活 + 紧凑"。理解了这个动机,你就理解了 SQLite 一切"类型宽松"行为的根源。

---

## 六、亲和性(type affinity):柔性转换在源码里怎么落地

"动态类型"听上去很美,但有个现实问题:如果完全不转换,那 `SELECT * FROM t WHERE score = 99` 在 `score` 列存的是 `'99'`(文本)时,会和整数 `99` 比较吗?这就是亲和性的作用——**它在值存入时,按列定义的偏好,温柔地推一把,尽量转成"期望"的类型**。

### 七种亲和性:SQLITE_AFF_*

SQLite 的亲和性,在源码里是几个 `#define` 常量([sqliteInt.h:2338-2345](../sqlite/src/sqliteInt.h#L2338-L2345))。**注意 3.54 新增了 `FLEXNUM`**,这是新版本的坑(老资料只列 5-6 种):

```c
/* sqliteInt.h:2338-2345 */
#define SQLITE_AFF_NONE     0x40  /* '@'  不转换 */
#define SQLITE_AFF_BLOB     0x41  /* 'A'  BLOB 亲和性(不转换,与 NONE 几乎等价) */
#define SQLITE_AFF_TEXT     0x42  /* 'B'  TEXT 亲和性 */
#define SQLITE_AFF_NUMERIC  0x43  /* 'C'  NUMERIC 亲和性 */
#define SQLITE_AFF_INTEGER  0x44  /* 'D'  INTEGER 亲和性 */
#define SQLITE_AFF_REAL     0x45  /* 'E'  REAL 亲和性 */
#define SQLITE_AFF_FLEXNUM  0x46  /* 'F'  FLEXNUM 亲和性(3.54 新增) */
#define SQLITE_AFF_DEFER    0x58  /* 'X'  延迟计算(内部用) */
```

> **易翻车坑**:亲和性常量的值是**连续的 ASCII 字符**(`@ A B C D E F`)。这不是巧合——源码注释 [sqliteInt.h:2327-2328](../sqlite/src/sqliteInt.h#L2327-L2328) 说,以前用助记符(`'i'` for INTEGER, `'t'` for TEXT),后来改成连续字符,**为了在 `OP_MakeRecord` 的 P4 参数里把一串 affinity 压成一个 C 字符串**(每列一个字符),省空间且 `strcmp` 可比。这是一个"用 ASCII 编码压缩"的细节。

一个关键判断:`sqlite3IsNumericAffinity(X)` 定义为 `(X)>=SQLITE_AFF_NUMERIC`([sqliteInt.h:2347](../sqlite/src/sqliteInt.h#L2347))——因为 `NUMERIC(0x43) ≤ INTEGER(0x44) ≤ REAL(0x45) ≤ FLEXNUM(0x46)`,所以一个简单的 `>=` 比较就能判断"是不是数值类亲和性"。这种"用值的连续性简化判断"的设计,贯穿整个 affinity 系统。

### 列类型名怎么推导出亲和性:sqlite3AffinityType

你写 `CREATE TABLE t(a VARCHAR(100), b BIGINT, c DOUBLE, d BLOB)`,SQLite 怎么把这些类型名推导成 affinity?核心是 `sqlite3AffinityType`([build.c:1670-1736](../sqlite/src/build.c#L1670-L1736))。它的规则是**扫描类型名字符串,按出现的关键字判断**,优先级很讲究:

```c
/* build.c:1670-1706  推导 affinity(简化,保留关键规则) */
char sqlite3AffinityType(const char *zIn, Column *pCol){
  u32 h = 0;                         /* 滚动 hash */
  char aff = SQLITE_AFF_NUMERIC;     /* 默认 NUMERIC */
  const char *zChar = 0;

  while( zIn[0] ){
    u8 x = *(u8*)zIn;
    h = (h<<8) + sqlite3UpperToLower[x];   /* 滚动累积 4 字节 hash */
    zIn++;
    if( h==(('c'<<24)+('h'<<16)+('a'<<8)+'r') ){           /* CHAR → TEXT */
      aff = SQLITE_AFF_TEXT;  zChar = zIn;
    }else if( h==(('c'<<24)+('l'<<16)+('o'<<8)+'b') ){     /* CLOB → TEXT */
      aff = SQLITE_AFF_TEXT;
    }else if( h==(('t'<<24)+('e'<<16)+('x'<<8)+'t') ){     /* TEXT → TEXT */
      aff = SQLITE_AFF_TEXT;
    }else if( h==(('b'<<24)+('l'<<16)+('o'<<8)+'b')        /* BLOB → BLOB */
        && (aff==SQLITE_AFF_NUMERIC || aff==SQLITE_AFF_REAL) ){
      aff = SQLITE_AFF_BLOB;
    }else if( h==(('r'<<24)+('e'<<16)+('a'<<8)+'l')        /* REAL → REAL */
        && aff==SQLITE_AFF_NUMERIC ){
      aff = SQLITE_AFF_REAL;
    }else if( h==(('f'<<24)+('l'<<16)+('o'<<8)+'a')        /* FLOA → REAL */
        && aff==SQLITE_AFF_NUMERIC ){
      aff = SQLITE_AFF_REAL;
    }else if( h==(('d'<<24)+('o'<<16)+('u'<<8)+'b')        /* DOUB → REAL */
        && aff==SQLITE_AFF_NUMERIC ){
      aff = SQLITE_AFF_REAL;
    }else if( (h&0x00FFFFFF)==(('i'<<16)+('n'<<8)+'t') ){  /* INT → INTEGER, 立即 break */
      aff = SQLITE_AFF_INTEGER;
      break;
    }
  }
  return aff;
}
```

([build.c:1670-1736 sqlite3AffinityType](../sqlite/src/build.c#L1670-L1736))

把这个函数翻译成**人话规则**(注意优先级):

| 类型名里包含 | 推导出的 affinity | 备注 |
|---|---|---|
| `INT`(任何含 INT 的,如 `INTEGER`/`BIGINT`/`SMALLINT`/`TINYINT`) | **INTEGER** | 一旦命中 INT,**立即 break**,优先级最高 |
| `CHAR` / `CLOB` / `TEXT`(任何含 CHAR/TEXT 的,如 `VARCHAR`/`CHARACTER`) | **TEXT** | 命中 CHAR 会记住 `zChar`(为了后面算长度估计) |
| `BLOB`(或没声明类型) | **BLOB** | 只在还没被 REAL 抢走时才生效 |
| `REAL` / `FLOA` / `DOUB`(如 `REAL`/`FLOAT`/`DOUBLE`) | **REAL** | 只在还是默认 NUMERIC 时才抢 |
| 其他(如 `DATE`/`NUMERIC`/`DECIMAL`) | **NUMERIC** | 默认值 |

> **易翻车坑**:这些规则的**优先级和顺序**很反直觉。比如 `VARCHAR(100)` 包含 `CHAR` → TEXT affinity;`BIGINT` 包含 `INT` → INTEGER affinity;但 `DOUBLE PRECISION` 包含 `DOUB` → REAL affinity。如果你写个奇奇怪怪的类型名比如 `BLOBINT`,它会先命中 `BLOB`(aff=BLOB),再命中 `INT`(aff=INTEGER 并 break)——最终是 INTEGER!所以**类型名的推导是"按字符出现顺序匹配,INT 一旦命中立即 break,其他按最后命中的为准"**。这个规则在 SQLite 官方文档 "Datatypes In SQLite" 里有详细说明,但源码是唯一权威。

特别地,**不写类型名**(`CREATE TABLE t(a, b)`)的列,推导出 **BLOB affinity**(等价于 NONE,不转换)。这是 SQLite "schemaless" 的入口——你完全可以建一个"无类型"表,每列存什么都行。

### affinity 应用:applyAffinity 的转换规则

affinity 推导出来后,什么时候应用?在**值存入表**的时候(`OP_MakeRecord` 第一步)和**值参与比较**的时候(`OP_Compare`/`OP_MakeRecord` 的 affinity 字符串)。核心函数是 `applyAffinity`([vdbe.c:397-428](../sqlite/src/vdbe.c#L397-L428)):

```c
/* vdbe.c:397-428  应用亲和性(简化,对照注释看) */
static void applyAffinity(Mem *pRec, char affinity, u8 enc){
  if( affinity>=SQLITE_AFF_NUMERIC ){
    /* NUMERIC / INTEGER / REAL / FLEXNUM 这一族 */
    assert( affinity==SQLITE_AFF_INTEGER || affinity==SQLITE_AFF_REAL
         || affinity==SQLITE_AFF_NUMERIC || affinity==SQLITE_AFF_FLEXNUM );
    if( (pRec->flags & MEM_Int)==0 ){
      if( (pRec->flags & (MEM_Real|MEM_IntReal))==0 ){
        if( pRec->flags & MEM_Str )
          applyNumericAffinity(pRec,1);   /* TEXT → 尝试转 NUMERIC */
      }else if( affinity<=SQLITE_AFF_REAL ){
        sqlite3VdbeIntegerAffinity(pRec);  /* 已经是 REAL,看能不能转回 INT */
      }
    }
  }else if( affinity==SQLITE_AFF_TEXT ){
    /* TEXT:把 INTEGER/REAL 转成 TEXT */
    if( 0==(pRec->flags&MEM_Str) ){
      if( (pRec->flags&(MEM_Real|MEM_Int|MEM_IntReal)) ){
        sqlite3VdbeMemStringify(pRec, enc, 1);   /* 数字 → 字符串 */
      }
    }
    pRec->flags &= ~(MEM_Real|MEM_Int|MEM_IntReal);   /* 清掉数值标志 */
  }
  /* SQLITE_AFF_BLOB / NONE:什么都不做 */
}
```

([vdbe.c:397-428 applyAffinity](../sqlite/src/vdbe.c#L397-L428))

把这段代码翻译成**完整的 affinity 转换规则表**:

| 列 affinity | 收到 NULL | 收到 INTEGER | 收到 REAL | 收到 TEXT | 收到 BLOB |
|---|---|---|---|---|---|
| **NONE / BLOB** | NULL(不转) | INTEGER(不转) | REAL(不转) | TEXT(不转) | BLOB(不转) |
| **TEXT** | NULL(不转) | **TEXT**(数字 stringify) | **TEXT**(数字 stringify) | TEXT(不转) | BLOB(不转!) |
| **NUMERIC** | NULL(不转) | INTEGER(不转) | REAL(不转) | **能转 INT→INT,能转 REAL→REAL,都不行→TEXT** | BLOB(不转) |
| **INTEGER** | NULL(不转) | INTEGER(不转) | **能转 INT→INT,否则 REAL** | **能转 INT→INT,都不行→TEXT** | BLOB(不转) |
| **REAL** | NULL(不转) | INTEGER(不转!存成整数省空间) | REAL(不转) | **能转 REAL→REAL(优先),能转 INT→INT,都不行→TEXT** | BLOB(不转) |
| **FLEXNUM**(3.54 新) | NULL(不转) | INTEGER(不转) | REAL(不转) | **尝试转数字,转不成保留 TEXT** | BLOB(不转) |

几个关键细节:

1. **TEXT affinity 收到 BLOB 不转**——BLOB 是"二进制,别碰我",affinity 只动数字和文本。
2. **REAL affinity 收到整数,不转成 REAL**——这是个大坑!源码 [vdbe.c:3557-3560](../sqlite/src/vdbe.c#L3557-L3560) 特意把 REAL affinity 收到的整数标记成 `MEM_IntReal`(语义是实数,但存储按整数),**为了省空间**(整数 1 字节,浮点 8 字节)。读出来 `typeof()` 仍是 `'integer'`,但和别的 REAL 比较时按实数比。
3. **NUMERIC/INTEGER affinity 收到 TEXT**,会尝试解析文本是不是数字(`applyNumericAffinity`,[vdbe.c:354-372](../sqlite/src/vdbe.c#L354-L372)):能精确转整数就转 INTEGER,有小数点/指数就转 REAL,都转不成(如 `'hello'`)就**保留 TEXT**(不报错!)。

> **钉死这件事**:亲和性的本质是——**存入时温柔地试一次转换,转不成就算了,不报错**。这和 MySQL 的强类型(转不成报错或截断)是根本区别。亲和性只动 INTEGER/REAL/TEXT 三者之间的转换,从不碰 BLOB(BLOB 是"原始字节,神圣不可侵犯")。NULL 也不被转(NULL 就是 NULL)。

### 强制转换:CAST 函数和 sqlite3VdbeMemCast

affinity 是"温柔的",但 SQL 的 `CAST(x AS INTEGER)` 是"强制的"——必须转,转不了也得给个默认值(如 `CAST('hello' AS INTEGER) = 0`)。这走另一条路:`sqlite3VdbeMemCast`([vdbemem.c:926-966](../sqlite/src/vdbemem.c#L926-L966)):

```c
/* vdbemem.c:926  强制 cast(简化) */
int sqlite3VdbeMemCast(Mem *pMem, u8 aff, u8 encoding){
  if( pMem->flags & MEM_Null ) return SQLITE_OK;
  switch( aff ){
    case SQLITE_AFF_BLOB:   /* 转 BLOB:先 stringify 再改标志 */
      ...
    case SQLITE_AFF_NUMERIC:   /* 转 NUMERIC:能整则整,否则实 */
      sqlite3VdbeMemNumerify(pMem);  break;
    case SQLITE_AFF_INTEGER:  /* 强制整数:截断成整数 */
      sqlite3VdbeMemIntegerify(pMem);  break;
    case SQLITE_AFF_REAL:    /* 强制实数 */
      sqlite3VdbeMemRealify(pMem);  break;
    default:  /* TEXT */
      ...  /* 强制 stringify,转码,清掉数值标志 */
  }
  return SQLITE_OK;
}
```

([vdbemem.c:926-966 sqlite3VdbeMemCast](../sqlite/src/vdbemem.c#L926-L966))

`applyAffinity`(温柔,存入时)和 `sqlite3VdbeMemCast`(强制,CAST 时)是两条独立的路径——前者失败保留原值,后者失败给默认值。这个区分很重要,别混。

> **钉死这件事**:affinity 不是强类型,它只在**两个时机**起作用:① `OP_MakeRecord` 存入时(温柔试转);② 比较两个值时(`OP_Compare` 前,把两边 affinity 对齐)。其他时候,值就是它自己的存储类型。`CAST` 是另一条强制路径,和 affinity 无关。理解了"温柔 vs 强制"、"何时触发",你就不会被 SQLite 的类型行为绕晕。

---

## 七、rowid:table B-tree 的 key,以及 INTEGER PRIMARY KEY 别名

讲完了 Record 和 affinity,我们回到 cell 布局的另一个核心——**rowid**。

### rowid 是什么:每行一个 64 位整数

SQLite 的每张表(除非 `WITHOUT ROWID`)都有一列隐式的 **`rowid`**——一个 64 位有符号整数,是这行在表 B-tree 里的 **key**。表 B-tree 按 rowid 排序存储所有行,内部页 cell 里 `[child-ptr + rowid]` 用来二分导航,叶子页 cell 里 `[payload-size + rowid + payload]` 装着整行。

```
   表 B-tree 按 rowid 排序(叶子页 cell):
   ┌─────────────────────────────────────────────────────────┐
   │  cell: [psz][rowid=1][Record(row1)]                     │
   │  cell: [psz][rowid=2][Record(row2)]                     │
   │  cell: [psz][rowid=5][Record(row5)]   ← rowid 不一定连续 │
   │  cell: [psz][rowid=8][Record(row8)]                     │
   │  ...                                                     │
   └─────────────────────────────────────────────────────────┘
   内部页靠 rowid 二分:rowid < 5 往左子树,5 ≤ rowid < 8 往中子树...
```

rowid 在 `INSERT` 时自动分配(通常是 `max(rowid)+1`,或 `sqlite_sequence` 表记录的 AUTOINCREMENT 值)。你也可以显式指定:`INSERT INTO t(rowid, ...) VALUES(100, ...)`。

> **钉死这件事**:rowid 是 SQLite 表的**物理主键**——表 B-tree 的排序依据、内部页的导航 key、`OP_SeekRowid` 的定位依据。理解 rowid,就理解了 SQLite 表为什么"按整数行号组织",而不是像 MySQL 那样按"聚簇索引的 key"组织。这是 SQLite 表 B-tree 和 InnoDB B+树的又一个根本差异(InnoDB 聚簇索引的 key 是主键值,可以是任意类型;SQLite 表 B-tree 的 key 永远是整数 rowid)。

### INTEGER PRIMARY KEY = rowid 别名

rowid 是隐式的,但有个语法糖:**如果一张表的 PRIMARY KEY 是"单列 + 类型是 INTEGER + 升序"**,SQLite 会把这一列**当作 rowid 的别名**——这一列不另占存储,它就是 rowid。

这个逻辑在 `sqlite3AddPrimaryKey`([build.c:1848-1901](../sqlite/src/build.c#L1848-L1901))里:

```c
/* build.c:1887-1900  判断 INTEGER PRIMARY KEY 是否能当 rowid 别名 */
if( nTerm==1                        /* 单列主键 */
 && pCol
 && pCol->eCType==COLTYPE_INTEGER   /* 类型是 INTEGER(严格,不接受 BIGINT 等) */
 && sortOrder!=SQLITE_SO_DESC       /* 升序 */
){
  pTab->iPKey = iCol;               /* 记录这一列是 rowid 别名 */
  pTab->keyConf = (u8)onError;
  pTab->tabFlags |= autoInc*TF_Autoincrement;
}else{
  /* 不是 INTEGER PRIMARY KEY,创建一个独立的 unique index */
}
```

`pTab->iPKey` 这个字段([sqliteInt.h:2442](../sqlite/src/sqliteInt.h#L2442) `i16 iPKey; /* If not negative, use aCol[iPKey] as the rowid */`)是关键——**非负时,它指向那一列的索引,表示"这一列就是 rowid"**。

那查询时,你 `SELECT id FROM t WHERE id=10`(假设 `id` 是 INTEGER PRIMARY KEY),SQLite 怎么处理?在名字解析阶段([resolve.c:465-466](../sqlite/src/resolve.c#L465-L466)):

```c
/* resolve.c:465-466  把 INTEGER PRIMARY KEY 列引用替换成 rowid(-1) */
/* Substitute the rowid (column -1) for the INTEGER PRIMARY KEY */
pExpr->iColumn = j==pTab->iPKey ? -1 : (i16)j;
```

也就是说,**所有对 INTEGER PRIMARY KEY 列的引用,在编译期就被替换成对 rowid(内部列号 -1)的引用**。这一列在 Record 里**根本不存**——因为它的值就是 cell 头部的 rowid,重复存是浪费。

> **易翻车坑**:`INTEGER PRIMARY KEY` 当 rowid 别名,要求**类型名严格是 INTEGER**。`BIGINT PRIMARY KEY` 不行(推导出 INTEGER affinity 但 `eCType != COLTYPE_INTEGER`),`INT PRIMARY KEY` 也不行(虽然含 INT,但类型名不是完整的 INTEGER)。这是个常见的坑——你以为 `INT PRIMARY KEY` 是 rowid 别名,其实不是(它会建一个独立 unique index)。**只有 `INTEGER PRIMARY KEY`(全大写或全小写都行,但要完整匹配 INTEGER)才是 rowid 别名**。

### 为什么 INTEGER PRIMARY KEY 不重复存

回到 Record 格式:一张 `CREATE TABLE t(id INTEGER PRIMARY KEY, name TEXT)` 的表,插一行 `(1, "alice")`,它的 Record 长什么样?

- 列 0 `id=1`:**不在 Record 里**(它是 rowid 别名,值在 cell 头部)。
- 列 1 `name="alice"`:serial type 23,5 字节。

所以这条 Record 的 header 只有 `[hdr-size=2][type1=23]`,body 只有 `alice`——**总共 7 字节**,加上 cell 头部的 `[payload-size=7][rowid=1]`,整个 cell 才 9 字节。如果 id 重复存,得多 2 字节(id 的 serial type + data)。

> **钉死这件事**:`INTEGER PRIMARY KEY` 是 rowid 别名,是 SQLite 的一个**零存储成本的语法糖**——它让你能像用普通列一样用主键(`SELECT ... WHERE id=?`),但物理上不占一寸 Record 空间。这是"逻辑 schema 与物理存储解耦"的典范:逻辑上 `id` 是一列,物理上它就是 rowid。这种"别名机制"在 MySQL/PG 里没有(它们的主键是真实的聚簇索引 key,占存储),是 SQLite 独有的精巧。

---

## 八、对照与回顾:Record 格式 vs MySQL 行格式

本章最后,用一个对照表把 SQLite Record 和 MySQL InnoDB 行格式的关系说清(承接《MySQL·InnoDB》,一句话点过不重复):

| 维度 | **SQLite Record** | **MySQL InnoDB 行格式** |
|---|---|---|
| 列类型 | **动态**(值定类型,5 种存储类型) | **强类型**(列定类型,schema 定死) |
| 行格式名 | Record(header + body) | REDUNDANT / COMPACT / DYNAMIC / COMPRESSED |
| 变长字段长度 | 藏在 serial type 里(类型+长度二合一) | 记录在行头的"变长字段长度列表" |
| NULL 处理 | serial type 0(0 字节) | NULL 标志位 + 不存数据 |
| 主键 | rowid(整数)+ 可选 INTEGER PK 别名 | 聚簇索引 key(任意类型,占存储) |
| 树结构 | B-tree(叶子+内部都存数据) | B+树(只有叶子存数据) |
| 大字段 | overflow 页链 | off-page(768 字节前缀 + 溢出页) |
| 设计取向 | 嵌入式、紧凑、灵活 | C/S、强约束、优化器友好 |

> **承接《MySQL·InnoDB》**:这些对照,在 MySQL 那本讲 InnoDB 行格式(P3 存储)那里有详细拆解。本书只在这里给一张总表,让你看清"SQLite 动态类型 + Record 紧凑格式"是嵌入式的取舍,MySQL 强类型 + 复杂行格式是 C/S 的取舍——**两种设计都对,服务不同场景**。不重复讲 MySQL 行格式的细节。

---

## 九、技巧精解:两个最硬核的设计

本章正文讲完。技巧精解环节,挑两个最硬核、最值得单独钉死的技巧拆透。

### 技巧一:serial type——用一个 varint 同时编码类型 + 长度

这是 SQLite Record 格式最巧的一笔,值得单独拆。

**朴素做法会怎样**:如果让你设计一个"一行多列、每列不同类型"的二进制格式,朴素做法是——每列存 `[type 字段][length 字段][data]`。比如用一个字节 type(标识 NULL/INT/REAL/TEXT/BLOB)、两个字节 length、再跟 data。一行 3 列的 Record 至少 `3×3 + data = 9 + data` 字节的 overhead。

**SQLite 的巧招**:它观察到——

1. **整数的"类型"和"长度"是绑定的**:1 字节整数就是 serial type 1,8 字节整数就是 serial type 6……长度信息可以**编码进 type 编号**里。
2. **BLOB 和 TEXT 只差一个 bit**:它俩都是"一段字节",区别只在"要不要按文本编码"。可以用 serial type 的**奇偶**来区分(偶=BLOB,奇=TEXT),用 `(type-12)/2` 算长度。
3. **NULL 不需要 length**:serial type 0 就是 NULL,0 字节。
4. **常量 0 和 1 可以把值藏进 type**:serial type 8=0,9=1,0 字节 data。

这四点合起来,SQLite 把"类型 + 长度"压成**一个 varint**(对常见的小值只 1 字节),整个 Record 的 overhead 从朴素的"每列 3 字节"降到"每列 1 字节"。对一行 10 列的 Record,省 20 字节——这在嵌入式(存储贵)是巨大的收益。

**为什么 sound(正确性)**:这个编码能工作,是因为它**无歧义且可逆**。给定一个 serial type,你能唯一确定:① 值的存储类型(NULL/INT/REAL/TEXT/BLOB);② 值的字节长度。反过来,给定一个值,你能挑出唯一的 serial type(挑能装下它的最小的)。编解码完全对称,没有任何信息丢失。源码里 `sqlite3VdbeSerialType`(编)和 `sqlite3VdbeSerialGet`(解)是严格对偶的两个函数,任何一个改动都要同步改另一个。

> **钉死这件事**:serial type 的精髓是——**"类型"和"长度"在 SQLite 的值空间里不是正交的(整数的长度由值决定,BLOB/TEXT 的类型只差一个 bit),所以可以合并编码**。这是一个"观察数据本质特征、合并冗余维度"的典范。它不是凭空设计出来的,是 SQLite 团队几十年对"数据库里值的长尾分布"观察的结果——大多数值是小整数(0/1/小数字),少数是长文本/BLOB,用 varint 让小值用 1 字节、大值用多字节,平均下来极省。

### 技巧二:INTEGER PRIMARY KEY = rowid 别名——零存储主键

第二个硬核技巧,是 `INTEGER PRIMARY KEY` 当 rowid 别名的机制。

**朴素做法会怎样**:如果让你实现主键,朴素做法是——主键是一列,值存在 Record 里,另外建一个 unique index 保证不重复。这是 MySQL/PG 的做法(主键 = 聚簇索引 key + 占存储 + 独立索引结构维护)。

**SQLite 的巧招**:它观察到——表 B-tree 本来就有一个 key(rowid),如果主键恰是整数单列,那**主键值 = rowid,不用重复存**。具体做法:

1. **编译期**(`sqlite3AddPrimaryKey`):判断主键是不是"单列 + INTEGER + 升序",是则 `pTab->iPKey = iCol`,不建独立 index。
2. **名字解析期**(`resolve.c:466`):所有对这一列的引用(`pExpr->iColumn`),替换成 `-1`(rowid 的内部列号)。
3. **存储期**(`OP_MakeRecord`):这一列不参与 Record 编码(因为引用它的地方都变成了 rowid,rowid 在 cell 头部)。
4. **读取期**(`OP_Column` 等):读到 rowid 后,如果用户要的是 INTEGER PK 列,直接返回 rowid。

四个阶段协同,让"逻辑上的主键列"在物理上**完全不占空间**——既不占 Record 字节,也不需要独立 index。这是"用一个已有机制(rowid)免费搭车实现另一个需求(主键)"的极致。

**为什么 sound(正确性)**:这个机制正确,前提是"INTEGER PRIMARY KEY 列的值 = rowid"。SQLite 通过两个约束保证这点:① 建表时类型必须严格 INTEGER(`COLTYPE_INTEGER`),保证值是整数;② 所有写入路径(INSERT/UPDATE)都会把这一列的值同步到 rowid。一旦这两个约束满足,rowid 和 INTEGER PK 列就是同一个东西的两个名字,无论用哪个名,读到的都是同一个值。

> **不这样会怎样**:如果 SQLite 学 MySQL,主键一律建独立 index、值存在 Record 里,那 INTEGER PK 列每行至少多 1-8 字节(看值大小)Record 空间 + 一个完整 index 树(每行多一个 index cell)。对一张 1 亿行的表,这是几 GB 的浪费。SQLite 用"别名机制"免费搭 rowid 的车,把这笔开销完全省掉——对嵌入式(手机、浏览器,存储按 KB 算)是巨大收益。

> **钉死这件事**:`INTEGER PRIMARY KEY = rowid 别名` 是 SQLite 独有的"零存储主键"机制,体现了"观察已有机制、免费搭车"的工程美学。它不是"主键索引"——它根本就没有 index 结构,主键就是 rowid 本身。理解了这点,你就理解了 SQLite 表为什么"默认就有一个高效的主键查找"(直接按 rowid 在 B-tree 里二分),而不需要像 MySQL 那样"建主键 = 建聚簇索引"。

---

## 十、章末小结

### 回扣主线

本章服务**存储与事务**这一面——具体说,是"B-tree 页里每个 cell 装的一行,内部长什么样"。从 P3-08 的"B-tree 页骨架"接过来,我们钻进了页里最小的单位(cell),拆清了:

1. **一行 = 一条 Record**:`header(各列 serial type varint)+ body(各列值)`,整条塞在表叶子 cell 的 payload 区,大行溢出到 overflow 页。
2. **varint 是积木**:1-9 字节变长整数,小数用少字节、大数用多字节,贯穿 Record 格式(hdr-size、serial type、payload-size、rowid 全是 varint)。
3. **serial type 是精髓**:一个 varint 同时编码类型 + 长度,奇偶区分 BLOB/TEXT,小值段查表加速,8/9 编码常量 0/1(0 字节)。
4. **动态类型 + affinity**:列无强类型,只有 7 种亲和性(NONE/BLOB/TEXT/NUMERIC/INTEGER/REAL/FLEXNUM,3.54 新增 FLEXNUM),存入时温柔试转一次,值最终类型由值本身定。
5. **rowid 是表 B-tree 的 key,INTEGER PRIMARY KEY 是 rowid 别名**:零存储主键,逻辑列与物理存储解耦。

这是"存储与事务"这一面的"数据怎么存"——下一章 P3-10 讲"数据怎么查得快"(索引怎么用 B-tree 加速),承接本章的 Record/rowid 概念。

### 五个为什么

1. **为什么 SQLite 的 Record 要把所有 type 堆在 header、所有 data 堆在 body?**——为了按列随机访问快:扫 header 的 type 累加偏移就能定位第 N 列的 data,不用跳过前面的 data。
2. **为什么 serial type 用一个 varint 同时编码类型 + 长度?**——观察数据本质:整数长度由值决定(可编码进 type 编号),BLOB/TEXT 只差一个 bit(可奇偶区分),NULL/常量 0/1 不需要 length——合并编码后每列 overhead 从 3 字节降到 1 字节。
3. **为什么 SQLite 不要强类型,只用亲和性?**——嵌入式 + 单应用独占 + 数据来源杂,强类型契约价值低反而碍事;动态类型 + 亲和性换灵活 + 紧凑,是嵌入式场景的取舍(MySQL/PG 的强类型服务 C/S 多应用场景,各对各的)。
4. **为什么 REAL affinity 收到整数不转成 REAL?**——为了省空间:整数 1 字节、浮点 8 字节,存整数更省;语义上仍按实数比较(标记成 MEM_IntReal)。这是"存储紧凑优先于类型整齐"的取舍。
5. **为什么 INTEGER PRIMARY KEY 不占存储?**——因为它就是 rowid 的别名:编译期把列引用替换成 rowid(-1),存储期不编进 Record,读取期直接返回 rowid。零存储主键,是"搭已有机制的便车"。

### 想继续深入往哪钻

- **想看官方权威**:读 SQLite 官方文档 "Record Format"(文件格式规范,serial type 表的源头)、"Datatypes In SQLite"(亲和性规则权威说明,3.54 版含 FLEXNUM)、"Rowid Tables"(rowid 与 INTEGER PRIMARY KEY)。
- **想看源码**:本章引用的核心文件——`src/vdbe.c`(`OP_MakeRecord` 生产 Record)、`src/vdbeaux.c`(serial type 编解码 + RecordUnpack/Compare)、`src/vdbemem.c`(Mem 结构 + affinity cast)、`src/btree.c`(cell 解析 + accessPayload)、`src/build.c`(affinity 推导 + INTEGER PK 别名)、`src/resolve.c`(列引用替换成 rowid)、`src/util.c`(varint 编解码)。按这个顺序读,从"一行怎么造"到"一行怎么存"到"一行怎么读",闭环。
- **想动手感受**:`sqlite3` CLI 起个库,`INSERT` 几行不同类型的值,然后用 `.dbinfo` 或十六进制 dump 工具看 `.db` 文件的字节布局,对照本章的 Record 格式逐字节解析——你会看到 serial type、varint、rowid 都在字节流里活生生地存在着。

### 引出下一章

我们搞清了"一行在 B-tree cell 里长什么样"(Record 格式)和"SQLite 为什么类型宽松"(动态类型 + affinity)。但表 B-tree 是按 rowid 排序的——如果你要按**非 rowid 列**查(比如 `WHERE name='alice'`),表 B-tree 帮不上忙(它只按 rowid 排)。这时候就需要**索引**——另一棵 B-tree,按索引列排序。下一章 P3-10,我们讲**索引怎么用 B-tree 加速查询**:索引 B-tree 的 cell 长什么样(它的 key 不是 rowid 而是 Record)、SQLite 怎么决定走不走索引、`WHERE` 怎么变成对索引 B-tree 的查找。索引的 Record 格式,正是本章 Record 格式的延伸——只不过它存的是"索引列值 + rowid 指针",而不是整行。

> **下一章**:[P3-10 · 索引与查询:怎么用 B-tree 加速](P3-10-索引与查询-怎么用B-tree加速.md)
