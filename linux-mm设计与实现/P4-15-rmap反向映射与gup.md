# 第十五章 · rmap 反向映射 + gup

> 篇:第 4 篇 · 用户地址空间:进程内存
> 主线呼应:第 14 章我们立起了**正向映射**——虚拟地址 → 物理页,缺页时内核沿 PGD/P4D/PUD/PMD/PTE 一路建项,最后从 buddy 拿一个物理页填进 PTE。但第 14 章每处 [`folio_add_new_anon_rmap`](../linux/mm/rmap.c#L1406)、[`folio_add_anon_rmap_pte`](../linux/mm/rmap.c#L1361)、[`folio_remove_rmap_pte`](../linux/mm/rmap.c#L1587) 都是在做一件神秘的事——**建立/拆除 rmap 索引**。这一章就把这件事拆透:**给定一个物理页,怎么找出所有映射它的 PTE?**(反向映射 rmap)。再加一个相关话题:**内核自己(DMA/IO/O_DIRECT)要访问用户页,怎么"钉住"它防止被回收?**(`get_user_pages`/gup)。这两件事看似无关,实则同源——都涉及"虚拟↔物理"的映射关系,rmap 是**反向**索引,gup 是**正向**走页表并加锁。

## 核心问题

**给定一个物理页(它可能被多个进程的多个 PTE 同时映射,如 `fork` 后的共享匿名页、共享文件页缓存),内核怎么快速找出所有映射它的 PTE?为什么需要(回收/迁移要解除物理页的所有映射才能换出/搬走)?get_user_pages(gup)怎么让内核"钉住"用户页给 DMA/IO,`pin` 和 `get` 在引用计数语义上有什么区别?**

读完本章你会明白:

1. **正向映射 vs 反向映射**:PTE 是"虚拟→物理"的正向,rmap 是"物理→所有映射它的 PTE 所在的 VMA"的反向;两者对称,服务于不同路径(缺页建正向,回收/迁移走反向)。
2. **匿名页 rmap**:`struct anon_vma`(每个 VMA 一个,`fork` 时父子共享 root)+ `struct anon_vma_chain`(把 anon_vma 和 VMA 多对多串联)+ `anon_vma->rb_root` 区间树(按页偏移索引 VMA)。
3. **文件页 rmap**:`address_space->i_mmap` 区间树(P4-12 提过 VMA 的 `shared.rb` 节点挂进这里),按文件偏移索引所有映射该文件的 VMA——这就是"object-based rmap"。
4. **`try_to_unmap`**:回收/迁移时沿 rmap 走到每个映射的 VMA,逐个解除 PTE(清 PTE + 刷 TLB + 减 mapcount)。
5. **gup/pin**:`get_user_pages`/`pin_user_pages` 沿页表正向走、把物理页"钉住"给内核(DMA/IO);`FOLL_PIN` vs `FOLL_GET` 在引用计数语义上的区别(为什么 DMA 必须用 `pin`,不能只 `get`)。

> **逃生阀**:如果你对"PTE 是虚拟→物理的正向映射"还模糊,先回 [第 14 章 14.5 节](P4-14-缺页中断-从虚拟到物理.md)看一眼 `do_anonymous_page` 怎么 `set_ptes` 建正向映射,以及那里反复出现的 `folio_add_new_anon_rmap`(它就是本章要拆的 rmap 种子)。本章默认你已经知道 PTE 结构(第 13 章)、缺页建正向映射(第 14 章)、VMA 的 `anon_vma` 字段(第 12 章)。

---

## 15.1 一句话点破

> **正向映射(PTE)回答"虚拟地址映射到哪个物理页",反向映射(rmap)回答"物理页被哪些虚拟地址映射"。回收/迁移要把物理页换出去,必须先沿 rmap 找到所有映射它的 PTE 解除——扫描所有进程的所有页表是 O(总页表大小) 不可行,rmap 让它 O(该页的映射数)。gup 则反过来——内核主动沿正向页表走、把用户页钉住给 DMA,引用计数保证回收/迁移不会把它弄走。**

这是结论,不是理由。本章倒过来拆:先看为什么需要反向映射、朴素方案为什么不行,再看匿名页的 `anon_vma` 链、文件页的 `address_space->i_mmap` 区间树分别怎么实现反向索引,然后钻进 `try_to_unmap`(沿 rmap 解除映射)和 `folio_add_new_anon_rmap`(种 rmap 种子),最后拆 gup/pin 的语义,回到全书二分法。

---

## 15.2 为什么需要反向映射

### 正向映射不够用的场景

第 14 章建的正向映射(PTE)回答了"虚拟地址 → 物理页"。这是**分配/访问路径**要的:进程访问虚拟地址,MMU 沿 PTE 翻译到物理页。

但**回收/迁移路径**问的是反过来的问题:**给定一个物理页,谁映射了它?** 为什么需要这个反向查询?看三个场景:

**场景一:页面回收(vmscan)**。物理内存紧张,kswapd 选了一个冷的物理页要换出去(swap)。但这个物理页可能被**多个 PTE** 映射——比如 `fork` 后父子共享的匿名页、被多个进程 mmap 的共享文件页。换出之前,内核必须**找到所有映射它的 PTE,逐个解除**(把 PTE 改成"指向 swap entry"或直接 absent),否则换出后还有 PTE 指向这个物理页,进程一访问就拿到错误数据(物理页已经被别人复用了)。

**场景二:页面迁移(NUMA balancing、compaction)**。NUMA balancing 把一个页从远端 NUMA 节点迁到本地,compaction 把 MOVABLE 页搬走凑连续大页。迁移就是"分配新物理页 → 复制内容 → 把所有指向旧页的 PTE 改成指向新页 → 释放旧页"。这同样需要先找到所有指向旧页的 PTE。

**场景三:KSM(同页合并)**。KSM 把内容相同的匿名页合并成一个共享页省内存。合并后,多个 VMA 的 PTE 都指向同一个物理页——这个页的 rmap 列表里就有多个映射。后续某个进程写这个共享页(COW),又要沿 rmap 找到自己的那个映射。

### 反面对比:扫描所有页表会破产

朴素地想,要"找映射物理页 X 的所有 PTE",就**扫描所有进程的所有页表**:

```
  朴素反查(扫描所有页表):
  for each process p:
      for each PGD entry:
          for each P4D entry:
              ... for each PTE:
                  if (pte_pfn(*pte) == pfn(X)):
                      找到一个映射
```

这个代价是 **O(所有进程的所有页表大小)**。一台 256GB 的机器跑几百个进程,每个进程页表几 MB ~ 几 GB,扫描一遍是 TB 级的内存访问——**完全不可行**。回收一个页要扫描所有页表,回收路径直接瘫痪。

> **不这样会怎样**:如果内核没有 rmap,每回收一个物理页都要扫描所有进程的所有页表,那 kswapd(后台回收线程)每回收一页要花几秒到几分钟——内存稍微紧张系统就卡死。rmap 的存在,就是让这个反查变成 **O(该页的映射数)**——一个页被几个 PTE 映射,rmap 就只走那几个映射,不碰其他页表。

### rmap 的本质:每个物理页维护一份"谁映射了我"的索引

rmap 的思路:**给每个物理页(更准确说是 `struct folio`)维护一份"映射了我的 VMA 列表(及页内偏移)"的索引**。反查时,直接读这份索引,O(映射数) 走完。

但这里有个微妙——rmap 不直接索引到 PTE,而是**索引到 VMA**(再加一个"在 VMA 内的偏移")。然后从 VMA + 偏移,**重新沿正向页表走一次**定位到具体 PTE。这是 Linux rmap 的关键设计:

```
  反查物理页 X 的所有 PTE(rmap 的两级结构):
  ① X 的索引(anon_vma 或 address_space)→ 一组相关 VMA
  ② 对每个 VMA:计算 X 在该 VMA 内的虚拟地址 → 沿正向页表定位 PTE
```

为什么不直接索引到 PTE 指针?因为 PTE 指针会随页表删除/重建而失效(VMA munmap 后整个 PTE 页都没了),维护"PTE 指针列表"的代价大。而 VMA 是稳定的(只要进程不 munmap/exit,VMA 就在 maple tree 里),从 VMA + 偏移沿页表走到 PTE 是 O(页表深度)= O(4~5) 的廉价操作。

> **钉死这件事**:rmap 是"物理页 → VMA(及偏移)"的反向索引,不是"物理页 → PTE 指针"的直接索引。从 VMA 定位到 PTE 靠"正向沿页表走"(PTE 锁保证走到的 PTE 没被并发改)。这个"反查到 VMA、再正向走到 PTE"的混合设计,平衡了索引稳定性(VMA)和定位精度(PTE)。

---

## 15.3 匿名页 rmap:`anon_vma` + `anon_vma_chain`

匿名页(anonymous page,没有文件后端,如 `malloc` 拿到的页)的 rmap 用 `struct anon_vma` 和 `struct anon_vma_chain` 两层结构组织。

### `struct anon_vma`:每个 VMA 一个(可能共享)

[`struct anon_vma`](../linux/include/linux/rmap.h#L31)([rmap.h:31](../linux/include/linux/rmap.h#L31))核心字段:

```c
struct anon_vma {
    struct anon_vma *root;          /* 这棵 anon_vma 树的根(fork 时父子共享 root) */
    struct rw_semaphore rwsem;      /* 修改/遍历用,读写信号量 */
    atomic_t refcount;
    unsigned long num_children;     /* 子 anon_vma 数(用于 fork 时复用决策) */
    unsigned long num_active_vmas;  /* 指向这个 anon_vma 的 VMA 数 */
    struct anon_vma *parent;        /* 父 anon_vma */
    struct rb_root_cached rb_root;  /* ★ 区间树:挂所有相关 VMA 的 anon_vma_chain */
};
```

注意 [`rb_root`](../linux/include/linux/rmap.h#L66)——这是一棵**区间树(interval tree)**,按"VMA 内的页偏移"索引挂进来的 `anon_vma_chain`。这就是反向索引的核心数据结构。

每个 VMA 有一个 `anon_vma` 指针([`vma->anon_vma`](../linux/include/linux/mm_types.h#L647 附近),第 12 章 VMA 卡片里见过)。匿名页缺页时([`do_anonymous_page`](../linux/mm/memory.c#L4383))第一件事就是 [`anon_vma_prepare(vma)`](../linux/mm/rmap.c#L188)——确保这个 VMA 有 anon_vma(没有就分配一个或复用兄弟 VMA 的)。

### `struct anon_vma_chain`:多对多的连接器

光有 anon_vma 不够——`fork` 之后,父子 VMA 共享同一批匿名页,但父和子各有自己的 anon_vma(子进程新建匿名页要挂到自己的 anon_vma 上)。需要一个结构把"一个 VMA"和"一个 anon_vma"多对多连接起来,这就是 [`struct anon_vma_chain`](../linux/include/linux/rmap.h#L82)([rmap.h:82](../linux/include/linux/rmap.h#L82)):

```c
struct anon_vma_chain {
    struct vm_area_struct *vma;     /* 我连接的 VMA */
    struct anon_vma *anon_vma;      /* 我连接的 anon_vma */
    struct list_head same_vma;      /* 同一 VMA 的所有 avc 串成链(按 mmap_lock) */
    struct rb_node rb;              /* 挂进 anon_vma->rb_root 区间树的节点 */
    unsigned long rb_subtree_last;  /* 区间树剪枝用 */
};
```

一个 `anon_vma_chain`(简称 avc)是一个**连接器**:它一头连 VMA,一头连 anon_vma。一个 VMA 可能挂在多个 anon_vma 上(`fork` 后子 VMA 既挂自己的 anon_vma,也通过 avc 挂到父 anon_vma——这样才能反查到父进程的映射),所以 VMA 有一个 `same_vma` 链表把所有相关的 avc 串起来;一个 anon_vma 上也可能挂多个 VMA(父 anon_vma 挂父子两个 VMA),所以 anon_vma 用区间树 `rb_root` 索引所有挂进来的 avc。

### `anon_vma_prepare`:给 VMA 装上 anon_vma

[`__anon_vma_prepare`](../linux/mm/rmap.c#L188)([rmap.c:188](../linux/mm/rmap.c#L188))是缺页路径里反复调用的函数([`do_anonymous_page`](../linux/mm/memory.c#L4383) 开头就调它)。简化主干:

```c
int __anon_vma_prepare(struct vm_area_struct *vma)
{
    struct anon_vma *anon_vma, *allocated;
    struct anon_vma_chain *avc;

    avc = anon_vma_chain_alloc(GFP_KERNEL);       /* 分配一个 avc(连接器) */
    if (!avc) goto out_enomem;

    anon_vma = find_mergeable_anon_vma(vma);      /* 先试着复用兄弟 VMA 的 anon_vma */
    allocated = NULL;
    if (!anon_vma) {
        anon_vma = anon_vma_alloc();              /* 不能复用就新分配一个 */
        anon_vma->num_children++;
        allocated = anon_vma;
    }

    anon_vma_lock_write(anon_vma);                /* 拿 anon_vma 写锁 */
    spin_lock(&mm->page_table_lock);
    if (likely(!vma->anon_vma)) {                 /* ★ 复查:VMA 还没 anon_vma 吗? */
        vma->anon_vma = anon_vma;
        anon_vma_chain_link(vma, avc, anon_vma);  /* 把 avc 挂进 anon_vma 区间树 + VMA 链 */
        anon_vma->num_active_vmas++;
        allocated = NULL;
        avc = NULL;
    }
    spin_unlock(&mm->page_table_lock);
    anon_vma_unlock_write(anon_vma);
    /* 如果我多分配的没被用上,释放掉 */
    if (unlikely(allocated)) put_anon_vma(allocated);
    if (unlikely(avc)) anon_vma_chain_free(avc);
    return 0;
}
```

注意这里又出现了 mm 里反复见的**乐观并发**:分配 avc + anon_vma 在不持锁的情况下做(可能阻塞),拿锁后复查 `!vma->anon_vma`——如果别的线程已经把 anon_vma 装好了,就把自己多分配的释放掉。和第 13 章 `pmd_install` 的"分配 + 拿锁复查 + 回滚"一个模子。

`find_mergeable_anon_vma` 是个优化:**如果这个 VMA 前后有兄弟 VMA 已经有 anon_vma,且满足合并条件,就复用那个 anon_vma**,不新分配。这控制 anon_vma 数量(否则每个 VMA 一个 anon_vma,`fork` 几次就爆炸)。

### `__folio_set_anon`:把 anon_vma 编码进 folio->mapping

给 VMA 装好 anon_vma 之后,缺页路径给物理页(folio)**建立 rmap 索引**——[`folio_add_new_anon_rmap`](../linux/mm/rmap.c#L1406)([rmap.c:1406](../linux/mm/rmap.c#L1406))是入口。它内部调 [`__folio_set_anon`](../linux/mm/rmap.c#L1234)([rmap.c:1234](../linux/mm/rmap.c#L1234)):

```c
static void __folio_set_anon(struct folio *folio, struct vm_area_struct *vma,
        unsigned long address, bool exclusive)
{
    struct anon_vma *anon_vma = vma->anon_vma;

    BUG_ON(!anon_vma);

    /* 如果不是 exclusive(fork 后共享),用 _最老的_ anon_vma(root) */
    if (!exclusive)
        anon_vma = anon_vma->root;

    /*
     * ★ 关键技巧:把 anon_vma 指针 + PAGE_MAPPING_ANON 位,
     *   一起写进 folio->mapping(同一个字段!)。
     *   PAGE_MAPPING_ANON 位用来区分"这是 anon_vma 指针"
     *   还是"这是 address_space 指针"(文件页的 mapping)。
     *   page_idle 做无锁 rmap 扫描,所以这里 WRITE_ONCE 保证
     *   编译器不会把"anon_vma 写"和"位置位"拆成两个 store
     *   (中间状态会让 rmap 把它误认成 address_space 而 crash)。
     */
    anon_vma = (void *) anon_vma + PAGE_MAPPING_ANON;
    WRITE_ONCE(folio->mapping, (struct address_space *) anon_vma);
    folio->index = linear_page_index(vma, address);
}
```

这里有个**精妙的低位编码技巧**——`folio->mapping` 这个字段是**复用**的:

- **匿名页**:`folio->mapping = (anon_vma 指针) | PAGE_MAPPING_ANON`(最低位区置位表示"这是 anon_vma")。
- **文件页**:`folio->mapping = address_space 指针`(指向文件_inode 的 `address_space`,最低位区**不**置位)。

反查时([`folio_anon_vma`](../linux/mm/rmap.c#L500) 等函数),读 `folio->mapping`,看低位是否置位——置位说明是匿名页,`(mapping - PAGE_MAPPING_ANON)` 还原出 anon_vma 指针;不置位说明是文件页,mapping 直接就是 address_space。**一个字段两用**,省了一个字段。

注释还特别提到——`page_idle`(内核的一个特性)会做**无锁的乐观 rmap 扫描**,读 `folio->mapping` 时可能读到中间状态。所以 `__folio_set_anon` 用 `WRITE_ONCE` 保证"anon_vma 指针 + PAGE_MAPPING_ANON 位"作为一个 store 原子地写入——否则编译器可能拆成"先写 anon_vma 指针、再或上低位"两个 store,中间状态(低位还没置位但指针已是 anon_vma)会让并发读者误把它当 address_space 解引用,**crash**。这是 mm 里"无锁读者 + 写者用原子 store"的典范。

> **钉死这件事**:匿名页的 rmap 种子是 [`folio_add_new_anon_rmap`](../linux/mm/rmap.c#L1406) 在缺页这一刻种下的——它把 anon_vma 编码进 `folio->mapping`、初始化 `_mapcount`、设置 `PageAnonExclusive`(第 14 章 COW 复用判断用)。这就是第 14 章每处 `folio_add_new_anon_rmap` 的真正含义——它不只是"加映射",而是**建立反向索引的种子**,让未来回收/迁移能反查到这个映射。

### 匿名页 rmap 的反查路径:`rmap_walk_anon`

回收/迁移要反查一个匿名页的所有映射时,走 [`rmap_walk_anon`](../linux/mm/rmap.c#L2569)([rmap.c:2569](../linux/mm/rmap.c#L2569))。主干:

```c
static void rmap_walk_anon(struct folio *folio,
        struct rmap_walk_control *rwc, bool locked)
{
    struct anon_vma *anon_vma;
    pgoff_t pgoff_start, pgoff_end;
    struct anon_vma_chain *avc;

    anon_vma = rmap_walk_anon_lock(folio, rwc);    /* ★ 从 folio->mapping 解出 anon_vma + 拿读锁 */
    if (!anon_vma) return;

    pgoff_start = folio_pgoff(folio);               /* folio 在 VMA 内的页偏移 */
    pgoff_end = pgoff_start + folio_nr_pages(folio) - 1;

    /* ★ 沿 anon_vma->rb_root 区间树,遍历所有覆盖这个偏移范围的 avc */
    anon_vma_interval_tree_foreach(avc, &anon_vma->rb_root,
                                    pgoff_start, pgoff_end) {
        struct vm_area_struct *vma = avc->vma;
        unsigned long address = vma_address(&folio->page, vma);  /* folio 在 vma 内的虚拟地址 */

        if (rwc->invalid_vma && rwc->invalid_vma(vma, rwc->arg))
            continue;

        if (!rwc->rmap_one(folio, vma, address, rwc->arg))   /* 对每个映射调回调 */
            break;
        if (rwc->done && rwc->done(folio)) break;
    }
    anon_vma_unlock_read(anon_vma);
}
```

这就是"物理页 → 所有映射它的 VMA"的完整反查:

1. **从 `folio->mapping` 解出 anon_vma**(低位置位技巧)+ 拿读锁;
2. **算出 folio 在 VMA 内的页偏移**(`folio_pgoff`);
3. **沿 anon_vma 的区间树 `rb_root`,遍历所有覆盖这个偏移的 avc**——每个 avc 连着一个 VMA,这个 VMA 映射了这个 folio;
4. **对每个 VMA,算出 folio 在它里面的虚拟地址**,调回调 `rmap_one`(如 `try_to_unmap_one`)处理。

注意第 4 步——拿到 VMA + 虚拟地址后,**回调内部会沿正向页表走一次**(`page_vma_mapped_walk`)定位到具体 PTE,然后清 PTE + 刷 TLB + 减 mapcount。这就是前面说的"rmap 反查到 VMA,再正向走到 PTE"。

### `fork` 后的 anon_vma 链:为什么这么绕

`fork` 是 rmap 最绕的场景。父进程 VMA 有 anon_vma A,里面有些匿名页。fork 时:

- 子进程**复制父 VMA**,子 VMA 也要能反查到这些(现在共享的)匿名页。
- 但子进程后续自己 `malloc` 的新匿名页,应该挂到子自己的 anon_vma,不能混进父进程的 rmap。

解决方案:**子 VMA 挂两个 anon_vma**——通过两个 avc。一个连父 anon_vma A(这样父 A 的区间树里有子 VMA,反查共享页能查到子),一个连子新建的 anon_vma B(子新页挂 B)。`fork` 路径的 `anon_vma_fork`/`anon_vma_clone` 干这件事。这样,一个共享匿名页的 rmap 反查,能沿 root anon_vma 的区间树找到父子两个 VMA 的所有映射。

```
  fork 后匿名页 X(物理页)的 rmap 结构(简化):

  父进程 VMA_P ──── avc ───→ anon_vma_A (root)
                              │
                              ├─ rb_root 区间树:
                              │    ├─ avc(VMA_P, pgoff=k)    ← 父进程映射 X
                              │    └─ avc(VMA_C, pgoff=k)    ← 子进程映射 X(共享)
                              │
  子进程 VMA_C ──── avc ───→ anon_vma_B (子自己,新页挂这)
                              │
                              └─ rb_root 区间树:
                                   └─ (子进程 fork 后新 malloc 的页的 avc)
```

X 的 `folio->mapping` 指向 `anon_vma_A | PAGE_MAPPING_ANON`(因为它是 fork 前父进程建的,用 A;fork 后 X 被父子共享,`__folio_set_anon` 时 `exclusive=false`,用 root)。反查 X 时,沿 A 的区间树找到 VMA_P 和 VMA_C 两个映射。这就是 rmap 能找到"所有映射"的机制。

> **钉死这件事**:匿名页 rmap 用 `anon_vma`(每个 VMA 一个,可共享 root)+ `anon_vma_chain`(连接器)+ `anon_vma->rb_root` 区间树(按页偏移索引 VMA)三层结构。`fork` 后的共享匿名页,反查时沿 root anon_vma 的区间树找到所有相关 VMA(父子都在)。这套结构看起来绕,但它让"反查一个物理页的所有映射"变成 O(映射数) 而非 O(所有页表)——这是 rmap 的全部价值。

---

## 15.4 文件页 rmap:`address_space->i_mmap` 区间树

文件页(file-backed page,如 `mmap` 一个文件、可执行文件的代码段)的 rmap 比匿名页简单——**所有映射同一个文件的 VMA,都挂在这个文件的 `address_space` 的 `i_mmap` 区间树里**。这就是"object-based rmap"——文件(inode)就是天然的索引键。

### `address_space->i_mmap`:文件页 rmap 的根

[`struct address_space`](../linux/include/linux/fs.h#L463)([fs.h:463](../linux/include/linux/fs.h#L463))是文件 inode 的"页缓存 + 映射管理"结构,核心字段:

```c
struct address_space {
    struct inode *host;              /* 关联的 inode */
    struct xarray i_pages;           /* 页缓存(page cache):文件偏移 → 物理页 */
    struct rw_semaphore i_mmap_rwsem;
    struct rb_root_cached i_mmap;    /* ★ 区间树:所有映射这个文件的 VMA 挂这里 */
    /* ... */
};
```

[`i_mmap`](../linux/include/linux/fs.h#L473) 是一棵**区间树**,按"VMA 在文件内的偏移 `vm_pgoff`"索引所有挂进来的 VMA。第 12 章我们见过 VMA 的 `shared.rb` 字段——**它就是挂进 `address_space->i_mmap` 的节点**(不是进程内 VMA 索引,那是 maple tree 的事)。

VMA 建立时,如果是文件映射,[`__vma_link_file`](../linux/mm/mmap.c#L384) 把它挂进 `i_mmap`:

```c
static void __vma_link_file(struct vm_area_struct *vma, struct address_space *mapping)
{
    /* ... */
    vma_interval_tree_insert(vma, &mapping->i_mmap);   /* ★ 挂进文件的区间树 */
}
```

([`vma_interval_tree_insert`](../linux/mm/mmap.c#L391),[mmap.c:391](../linux/mm/mmap.c#L391))。这是 P4-12 提过的"文件映射 VMA 的 `shared.rb` 节点挂进 `address_space->i_mmap` 区间树"——现在你看到它的用途了:**这就是文件页 rmap 反查的入口**。

### 文件页反查:`rmap_walk_file`

文件页反查走 [`rmap_walk_file`](../linux/mm/rmap.c#L2618)(逻辑和 `rmap_walk_anon` 平行):

1. **从 `folio->mapping` 直接拿到 address_space**(文件页的 mapping 低位不置位,直接是 address_space);
2. **算出 folio 在文件内的偏移**(`folio->index`);
3. **沿 `address_space->i_mmap` 区间树,遍历所有覆盖这个偏移的 VMA**;
4. **对每个 VMA,算虚拟地址,沿正向页表走,处理 PTE**。

文件页 rmap 比匿名页简单的原因:**文件页天然挂在 page cache 里,page cache 本身就是 `address_space->i_pages`(xarray,按文件偏移索引物理页)**。给定一个文件物理页,它的 `folio->mapping` 就是 address_space,`folio->index` 就是文件内偏移;从 address_space 的 `i_mmap` 区间树找所有映射这个偏移的 VMA,是 O(映射数)。**文件 inode 是天然的、唯一的索引键**,不需要匿名页那种 anon_vma/avc 的多对多结构。

> **钉死这件事**:文件页 rmap 用 `address_space->i_mmap` 区间树(按文件偏移索引 VMA),这是"object-based rmap"——文件本身就是索引键,比匿名页的 anon_vma 结构简单得多。第 12 章 VMA 卡片里的 `shared.rb` 节点,就是为了挂进这棵区间树。反查时沿区间树找到所有映射该文件偏移的 VMA,再正向走页表定位 PTE。

---

## 15.5 `try_to_unmap`:沿 rmap 解除映射

回收/迁移要换出一个物理页时,调用 [`try_to_unmap`](../linux/mm/rmap.c#L1944)([rmap.c:1944](../linux/mm/rmap.c#L1944))。它是 rmap 的"反向遍历 + 解除映射"入口:

```c
void try_to_unmap(struct folio *folio, enum ttu_flags flags)
{
    struct rmap_walk_control rwc = {
        .rmap_one = try_to_unmap_one,             /* ★ 对每个映射调这个 */
        .arg = (void *)flags,
        .done = folio_not_mapped,                 /* 所有映射都解除了就停 */
        .anon_lock = folio_lock_anon_vma_read,    /* 匿名页的锁回调 */
    };

    if (flags & TTU_RMAP_LOCKED)
        rmap_walk_locked(folio, &rwc);
    else
        rmap_walk(folio, &rwc);                    /* 沿 rmap 走 */
}
```

`try_to_unmap` 构造一个 `rmap_walk_control`,设回调 `rmap_one = try_to_unmap_one`,然后 `rmap_walk` 沿 rmap 走(匿名页走 `rmap_walk_anon`、文件页走 `rmap_walk_file`,自动分流)。对每个映射的 VMA,调 [`try_to_unmap_one`](../linux/mm/rmap.c#L1616)([rmap.c:1616](../linux/mm/rmap.c#L1616))。

### `try_to_unmap_one`:解除单个映射

`try_to_unmap_one` 干的是"在一个 VMA 里,把这个 folio 的映射解除"。简化主干:

```c
static bool try_to_unmap_one(struct folio *folio, struct vm_area_struct *vma,
        unsigned long address, void *arg)
{
    struct mm_struct *mm = vma->vm_mm;
    DEFINE_FOLIO_VMA_WALK(pvmw, folio, vma, address, 0);   /* 页表遍历上下文 */
    /* ... mmu_notifier_range 初始化 ... */

    mmu_notifier_invalidate_range_start(&range);

    while (page_vma_mapped_walk(&pvmw)) {       /* ★ 正向沿页表走到每个映射的 PTE */
        /* ... 各种检查(mlock、device、KSM)... */

        /* 拿 PTE 锁,读当前 PTE */
        /* ... */

        if (should_defer_flush) {
            /* 攒进 mmu_gather(第 13 章!),延迟刷 TLB */
        } else {
            ptep_clear_flush(vma, address, pte);   /* 清 PTE + 立刻刷 TLB */
        }

        /* 把 PTE 改成 swap entry(匿名页换出)或 absent(文件页丢弃) */
        /* ... */

        folio_remove_rmap_pte(folio, subpage, vma);  /* ★ 减 mapcount,拆 rmap 关系 */

        /* ... */
    }

    mmu_notifier_invalidate_range_end(&range);
    return true;
}
```

注意 `try_to_unmap_one` 用 [`page_vma_mapped_walk`](../linux/mm/rmap.c)——这是 rmap 的"正向走页表"工具,给定 folio + VMA + 地址,沿页表走到具体 PTE(处理大页、连续 PTE 等情况)。`while` 循环是因为一个大 folio 可能在同一 VMA 里有多个 PTE 映射。

解除映射的关键三步:**① 清 PTE(可能立刻刷 TLB,可能攒进 mmu_gather 延迟刷);② 改 PTE 成 swap entry 或 absent;③ `folio_remove_rmap_pte` 减 mapcount**。其中第 ③ 步就是和第 14 章 `folio_add_new_anon_rmap` 对称的"拆 rmap"——mapcount 减到 0 时,这个 folio 就不再被任何 PTE 映射,可以释放/换出了。

> **钉死这件事**:[`try_to_unmap`](../linux/mm/rmap.c#L1944) 是 rmap 反查的"消费端"——回收/迁移调它,沿 rmap 找到所有映射的 VMA,逐个 `try_to_unmap_one` 清 PTE + 减 mapcount。它和第 14 章 `folio_add_new_anon_rmap`(种 rmap 种子)是对称的——一个建映射(加 mapcount),一个拆映射(减 mapcount)。两者都操作 `_mapcount`,这是 rmap 计数的核心。注意 `try_to_unmap_one` 删 PTE 时也用 mmu_gather(第 13 章)批量刷 TLB——rmap 是回收路径的地基,mmu_gather 是回收路径抗抖动的工具,两者协作。

### `_mapcount`:rmap 的计数核心

`struct folio` 里有几个 mapcount 相关字段(`include/linux/mm_types.h`):

- `_mapcount`:这个 folio(对单页 folio)被多少个 PTE 映射。-1 表示无映射,0 表示 1 个映射,1 表示 2 个映射……(`folio_add_new_anon_rmap` 里 `atomic_set(&folio->_mapcount, 0)` 就是设成"1 个映射")。
- `_nr_pages_mapped`:大 folio(mTHP/THP)的多页映射计数。
- `_entire_mapcount` / `_total_mapcount`:大 folio 整体映射(PMD 大页)的计数。

mapcount 是 rmap 的"引用计数"——它告诉你"这个物理页被几个 PTE 映射"。回收时 `try_to_unmap` 把每个映射的 PTE 清掉、mapcount 减一,减到 0(-1)时这个页就完全无人映射,可以释放/换出。第 14 章 COW 里 `wp_page_copy` "先改 PTE 再减 mapcount"的 ordering,本质就是保证 mapcount 减到能让别人复用之前,所有 CPU 对旧页的 TLB 都已失效——这是 rmap 计数和 TLB 一致性的交叉点。

---

## 15.6 gup:内核钉住用户页给 DMA/IO

rmap 是反向(物理→VMA),gup(get_user_pages)是**正向**——内核(DMA/IO/O_DIRECT/IO_URING)要访问用户进程的虚拟地址对应的物理页,**沿正向页表走、把物理页"钉住"**,防止回收/迁移把它弄走。

### 为什么需要 gup

考虑一个场景:网卡做 **zero-copy 收包**,直接把包 DMA 写到用户进程的缓冲区。网卡是硬件 DMA 引擎,它需要**物理地址**——但用户给的是虚拟地址。内核要:① 把虚拟地址翻译成物理地址(沿页表走);② **保证这个物理页在 DMA 期间不会被回收/迁移**(否则 DMA 写到一半页被换出,DMA 写到错页)。

这就是 gup 干的事——`get_user_pages` / `pin_user_pages` 把用户虚拟地址对应的物理页"钉住"(增加引用计数,让回收/迁移跳过它),返回 `struct page *` 给内核(DMA 用 `page_to_phys` 拿物理地址)。

### `get_user_pages`:沿页表正向走

[`get_user_pages`](../linux/mm/gup.c#L2391)([gup.c:2391](../linux/mm/gup.c#L2391))是 gup 的主入口:

```c
long get_user_pages(unsigned long start, unsigned long nr_pages,
        unsigned int gup_flags, struct page **pages)
{
    int locked = 1;

    if (!is_valid_gup_args(pages, NULL, &gup_flags, FOLL_TOUCH))
        return -EINVAL;

    return __get_user_pages_locked(current->mm, start, nr_pages, pages,
                                    &locked, gup_flags);
}
```

它转 [`__get_user_pages_locked`](../linux/mm/gup.c#L1816) → [`__get_user_pages`](../linux/mm/gup.c#L1186)。`__get_user_pages` 的核心循环:

```c
static long __get_user_pages(struct mm_struct *mm, unsigned long start,
        unsigned long nr_pages, unsigned int gup_flags, struct page **pages, int *locked)
{
    do {
        struct page *page;

        if (!vma || start >= vma->vm_end) {
            vma = gup_vma_lookup(mm, start);        /* maple tree 找 VMA */
            if (!vma) { ret = -EFAULT; goto out; }
            ret = check_vma_flags(vma, gup_flags);  /* VMA 权限检查 */
            if (ret) goto out;
        }

        if (fatal_signal_pending(current)) { ret = -EINTR; goto out; }
        cond_resched();

        page = follow_page_mask(vma, start, foll_flags, &ctx);  /* ★ 沿页表走 */
        /* ... 如果 page 是 NULL 或需要 fault,fault 后重试 ... */
        /* ... 拿到 page,加引用计数(FOLL_GET 走 try_get_page)... */
    } while (/* 还有页要 get */);

    return ret;
}
```

核心是 [`follow_page_mask`](../linux/mm/gup.c#L811)([gup.c:811](../linux/mm/gup.c#L811))——它沿正向页表走:

```c
static struct page *follow_page_mask(struct vm_area_struct *vma,
        unsigned long address, unsigned int flags, struct follow_page_context *ctx)
{
    pgd_t *pgd;
    struct mm_struct *mm = vma->vm_mm;

    if (is_vm_hugetlb_page(vma))
        return hugetlb_follow_page_mask(vma, address, flags, &ctx->page_mask);

    pgd = pgd_offset(mm, address);                  /* ★ 沿 PGD→P4D→PUD→PMD→PTE 走 */
    if (pgd_none(*pgd) || unlikely(pgd_bad(*pgd)))
        return no_page_table(vma, flags);
    /* ... 继续 p4d/pud/pmd/pte 层 ... */
}
```

`follow_page_mask` 用的就是第 13 章的 `pgd_offset`/`pmd_offset`/`pte_offset_map` 系列——**gup 和缺页共享同一套"沿页表走"的基础设施**。区别是:缺页是"PTE 不存在就建",gup 是"PTE 不存在就触发 fault 让缺页路径建,建好再来"。

`follow_page_mask` 返回 `struct page *`(映射到的物理页),`__get_user_pages` 拿到后给它**加引用计数**(`FOLL_GET` 走 `try_get_page`),让回收/迁移看到"这页有人在用"而跳过。最后把 `page *` 填进用户传入的 `pages[]` 数组,DMA 用 `page_to_phys(pages[i])` 拿物理地址。

### `get_user_pages_fast`:无锁快路径

[`get_user_pages_fast`](../linux/mm/gup.c#L3294)([gup.c:3294](../linux/mm/gup.c#L3294))是 gup 的**快路径**——它**不拿 `mmap_lock`**,直接在禁中断的情况下沿页表走(RCU 保护页表页不被释放)。如果快路径失败(页表项不存在、或遇到大页分裂等),退回到慢路径 `get_user_pages`(拿 `mmap_lock`)。

快路径的精妙在于:**禁中断 + RCU** 保护页表页遍历。页表页释放要走 RCU 宽限期(`mmu_gather` 攒页表页,延迟释放),禁中断保证遍历期间不会被打断,RCU 保证遍历到的页表页不会在遍历中途被释放。这是 gup 在高性能 IO(O_DIRECT、IO_URING)上的关键优化——避免每次 IO 都拿 `mmap_lock`。

> **钉死这件事**:gup 是内核访问用户页的标准入口。它沿正向页表走(和缺页共享基础设施),把物理页"钉住"(加引用计数),返回 `struct page *` 给 DMA/IO。`get_user_pages_fast` 是无锁快路径(禁中断 + RCU),`get_user_pages` 是拿 `mmap_lock` 的慢路径。gup 的存在让网卡/显卡/O_DIRECT 能直接操作用户内存,绕开内核拷贝——这是 zero-copy IO 的地基。

---

## 15.7 `FOLL_PIN` vs `FOLL_GET`:为什么 DMA 必须用 pin

gup 有两套语义,靠 `FOLL_PIN` 和 `FOLL_GET` 标志区分([`gup.c`](../linux/mm/gup.c) 顶部注释 L107 起详细解释):

- **`FOLL_GET`**(`get_user_pages`):给物理页的**引用计数(refcount)** +1。这保证页不会被 buddy 回收(回收要 refcount=0),但**不阻止页迁移**(NUMA balancing、compaction)——迁移时 refcount 不变,只是 PTE 改指向新页。
- **`FOLL_PIN`**(`pin_user_pages`):给物理页的 **DMA pin 计数(GUP pin count)** +1(通过 `folio->_pincount` 或 refcount 的特殊增量)。这**同时阻止回收和迁移**——回收/迁移看到 pin 标志就跳过这个页。

### 反面对比:DMA 只 get 不 pin 会怎样

考虑网卡 DMA 收包,直接写到用户缓冲区。如果内核只用了 `get_user_pages`(`FOLL_GET`)拿到物理页 X:

1. 网卡开始 DMA 写 X(物理地址已编程进网卡寄存器);
2. **同时**,NUMA balancing(或 compaction)看到 X 的 refcount 不为 0 但允许迁移,把 X 迁到新页 Y——PTE 改指 Y,内容复制,X 即将被释放;
3. 网卡 DMA 还在往**物理地址 X** 写——但 X 已经不是用户的页了(用户现在通过 PTE 访问 Y)。**网卡写到正在被释放的 X,数据丢失;用户读 Y 拿不到 DMA 写的数据。**

这就是为什么 **DMA 必须用 `pin_user_pages`**(`FOLL_PIN`)——pin 住的页,迁移代码看到 pin 标志会**跳过**(`try_to_unmap` 检查 `folio_test_pinned`),不会迁走。DMA 期间页是"冻结"的,物理地址稳定,DMA 才能正确。

### `pin_user_pages`:多走 longterm 检查

[`pin_user_pages`](../linux/mm/gup.c#L3394)([gup.c:3394](../linux/mm/gup.c#L3394))内部走 [`__gup_longterm_locked`](../linux/mm/gup.c),比 `get_user_pages` 多走 **longterm 检查**:

```c
long pin_user_pages(unsigned long start, unsigned long nr_pages,
        unsigned int gup_flags, struct page **pages)
{
    int locked = 1;

    if (!is_valid_gup_args(pages, NULL, &gup_flags, FOLL_PIN))
        return 0;
    return __gup_longterm_locked(current->mm, start, nr_pages,
                                  pages, &locked, gup_flags);
}
```

`__gup_longterm_locked` 检查"这次 pin 是长期的吗"(`FOLL_LONGTERM`)——长期 pin 的页,如果它在某 NUMA 节点上不能保证可用(比如 ZONE_MOVABLE 的页可能被 compaction 迁走,但 pin 不让迁——死锁),就要拒绝或把页迁到安全区。这是 6.x 为修复 "GUP pin vs page migration" 一族竞态(CVE-2019-5171、CVE-2020-xxxx 等)加的保护。

[`unpin_user_pages`](../linux/mm/gup.c#L469)([gup.c:469](../linux/mm/gup.c#L469))是 `pin_user_pages` 的反向——减 pin 计数,DMA 结束后必须调它释放 pin,否则页永远不能被回收/迁移(内存泄漏)。

> **钉死这件事**:`FOLL_PIN`(pin_user_pages)和 `FOLL_GET`(get_user_pages)的区别是 **DMA 安全性**。DMA 必须用 pin——pin 住的页迁移/回收都跳过,物理地址稳定,DMA 才正确。只 get 不 pin,DMA 期间页可能被迁移,DMA 写到错页。6.x 区分 pin/get + longterm 检查,是为修复历史上 "GUP vs page migration" 的竞态。**这是 mm 里"为什么 sound"的硬核问题之一**——并发正确性靠的是 pin 标志 + 迁移检查的配合,不是简单的锁。

### gup 与 rmap、与缺页的关系

把 gup 放进全局:

- **gup 与缺页**:gup 沿正向页表走(和缺页共享 `pgd_offset`/`pte_offset_map` 基础设施),PTE 不存在时触发 fault 让缺页路径建(第 14 章),建好再来。gup 是"读"页表,缺页是"写"页表。
- **gup 与 rmap**:gup pin 住的页,`try_to_unmap`(沿 rmap 解除映射,用于回收/迁移)会**跳过**——pin 标志告诉回收"这页有人在用,别动"。这是 gup 和 rmap 的交叉点:pin 是 rmap 的"暂停解除"信号。
- **gup 与第 14 章 COW**:gup 读一个 COW 共享的匿名页(父子的只读页)时,如果用 `FOLL_PIN` 且只读,内核会**强制 unshare**——把这个页给当前进程独占一份(触发 [`FAULT_FLAG_UNSHARE`](../linux/include/linux/mm_types.h#L1355) 缺页),避免 pin 住一个 COW 共享页导致后续 COW 逻辑混乱(这是 CVE-2020-29374 一族修复的一部分)。`follow_page_mask` 注释里提到的 `-EMLINK` 返回值就是这个信号。

---

## 15.8 技巧精解:rmap 反向索引 + gup pin 语义

这一章的两个标志性技巧,我们单独拆透。

### 技巧一:rmap 反向索引(物理页 → VMA)

#### 它解决什么问题

回收/迁移要换出/搬走一个物理页,必须先解除它**所有**的 PTE 映射。但物理页没有"我的 PTE 列表"这种东西——PTE 是正向的(虚拟→物理),物理页不知道"谁映射了我"。朴素反查要扫描所有进程的所有页表,代价 O(总页表大小),不可行。

#### 反面对比:扫描所有页表会怎样

> **反面对比**:如果内核没有 rmap,每回收一个物理页都要扫描所有进程的所有页表,会怎样?
>
> 1. 一台 256GB 机器跑几百进程,每个进程页表几 MB~几 GB,扫一遍是 TB 级内存访问。
> 2. kswapd 后台回收每回收一页要几秒~几分钟,内存稍微紧张系统卡死。
> 3. NUMA balancing 周期性迁移页,每次迁移都要反查——扫描所有页表会让周期性迁移变成系统级停顿。
>
> rmap 让反查变成 O(该页的映射数)——一个页被几个 PTE 映射,rmap 只走那几个映射。

#### 实现的精妙:两级结构 + 正反向混合

rmap 的精妙在于**两级结构 + 正反向混合**:

1. **第一级:物理页 → VMA 集合**(反向)。匿名页用 `folio->mapping`(低位编码的 anon_vma 指针)→ `anon_vma->rb_root` 区间树;文件页用 `folio->mapping`(address_space)→ `address_space->i_mmap` 区间树。这一级是**纯反向索引**。
2. **第二级:VMA + 偏移 → PTE**(正向)。拿到 VMA 和 folio 在 VMA 内的偏移后,沿正向页表走一次(`page_vma_mapped_walk`),定位到具体 PTE。

为什么不直接索引到 PTE 指针?前面说过——PTE 指针会失效(VMA munmap 后整个 PTE 页都没了),维护"PTE 指针列表"代价大。VMA 是稳定的,从 VMA 沿页表走到 PTE 是 O(页表深度)= O(4~5) 的廉价操作。这个"反查到 VMA、再正向走到 PTE"的混合设计,是 Linux rmap 的核心权衡。

**`folio->mapping` 的低位置位技巧**也是精妙之处——一个字段两用(匿名页存 anon_vma、文件页存 address_space),低位区分。这和第一章 `struct page` 的紧凑布局(union 复用 + 位段)同源——**海量数据的元数据要省**。

> **钉死这件事**:rmap 是 mm 里"反向索引"的经典。它让"回收/迁移一个物理页"这个本质需要"找所有映射"的操作,从 O(总页表大小) 降到 O(映射数)。两级结构(物理→VMA→PTE)+ 低位编码(anon_vma vs address_space)是它的工程精妙。第 14 章每处 `folio_add_new_anon_rmap` 种下的就是 rmap 种子,本章 `try_to_unmap` 沿 rmap 收割——两个路径对称,共同支撑"页能被换出/搬走"这件事。

### 技巧二:gup 的 pin 语义(DMA 安全)

#### 它解决什么问题

内核(DMA/IO)要访问用户页,需要"冻结"物理页——保证 DMA 期间物理地址稳定,页不被回收/迁移弄走。但"加引用计数"只防回收,不防迁移(NUMA balancing 会迁有引用的页)。需要更强的"pin"语义。

#### 反面对比:DMA 只 get 不 pin 会数据错乱

> **反面对比**:如果 DMA 用 `get_user_pages`(只 get,不 pin)拿到物理页 X,会怎样?
>
> 1. 网卡 DMA 编程写物理地址 X,开始收包。
> 2. NUMA balancing 看到 X 允许迁移(虽然有 refcount),把 X 迁到 Y——PTE 改指 Y,X 待释放。
> 3. 网卡继续往 X 写——**X 已不是用户的页**,数据写到了正在被释放的内存;用户读 Y 拿不到数据。
> 4. 更糟:如果 X 已被 buddy 复用给别人(比如另一个进程的页),网卡 DMA 写 X 会**破坏别人的数据**。
>
> `pin_user_pages`(`FOLL_PIN`)解决这个问题——pin 住的页,迁移代码看到 pin 标志**跳过**(`try_to_unmap` 检查),DMA 期间物理地址稳定。

#### 实现的精妙:pin 计数 + 迁移检查的配合

`pin_user_pages` 不是简单地"加更多 refcount",而是有**独立的 pin 语义**:

- `FOLL_PIN` 给 `folio->_pincount`(或 refcount 的特殊增量)+1,这是一个独立的"DMA pin 计数"。
- 迁移代码(`migrate_page`、NUMA balancing 的 `try_to_unmap`)检查 `folio_test_pinned`(或类似检查),看到 pin 就跳过这个页。
- `unpin_user_pages` 减 pin 计数,DMA 结束后必须调。

这种"**pin 标志 + 迁移检查**"的配合,而不是"一把全局锁",是 mm 并发正确性的典型——回收/迁移路径不需要拿额外的锁来等 DMA 结束,它只是**检查 pin 标志、跳过 pin 的页**;DMA 路径也不需要拿回收锁,只是**设 pin 标志**。两边靠"标志 + 检查"解耦,各自的快路径不受影响。这是 mm 在高并发下的扩展性根基。

longterm 检查(`__gup_longterm_locked`)是另一个精妙——长期 pin 的页,如果它在 ZONE_MOVABLE(compaction 可能想迁,但 pin 不让迁——潜在死锁),就要提前处理(拒绝或迁到安全区)。这是 6.x 为修复 "GUP pin vs memory hotunplug/compaction" 死锁加的保护。

> **钉死这件事**:gup 的 pin 语义是 mm 里"DMA 安全 vs 回收/迁移"的协调机制。pin 不是锁,而是"标志 + 检查"——DMA 设 pin,回收/迁移检查 pin 跳过。两边解耦,高并发下各自快路径不受影响。这是 mm "为什么 sound" 的典范——正确性靠协议(谁检查什么标志),不靠粗粒度锁。第 5 篇讲 vmscan/compaction 时会反复看到"回收/迁移检查 pin/locked 标志跳过"这个模式。

---

## 15.9 把本章放进全局:rmap 与 gup 在分配/回收二分法里的位置

把第 13 章(页表)、第 14 章(缺页)、本章(rmap/gup)接起来,看一次完整的"页映射生命周期":

```
  ① 分配:缺页建正向映射 + 种 rmap(第 14 章 + 本章 15.3)
    用户访问虚拟地址 v → 缺页 → do_anonymous_page
      - alloc_anon_folio:从 buddy 拿物理页 X
      - anon_vma_prepare:给 VMA 装 anon_vma(rmap 根)
      - folio_add_new_anon_rmap(X, vma, v):★ 种 rmap 种子
        └─ __folio_set_anon:X->mapping = anon_vma | PAGE_MAPPING_ANON
        └─ X->_mapcount = 0(1 个映射)
      - set_ptes:写正向 PTE(v → X)
    现在:正向 v→X(在 PTE 里),反向 X→vma(在 rmap 里)

  ② 访问:正常访问(正向查 PTE,TLB 命中)
    后续访问 v → MMU 查 PTE → X,无缺页

  ③ 内核访问:gup 钉住(本章 15.6-15.7)
    网卡 O_DIRECT 收包 → pin_user_pages(vma, v)
      - follow_page_mask:沿正向页表走 v → X
      - FOLL_PIN:X->_pincount++(DMA pin)
      - 返回 page*(X)给网卡
    网卡 DMA 写物理地址 page_to_phys(X),期间 X 不会被回收/迁移

  ④ 回收:沿 rmap 解除映射(本章 15.5 + 第 13 章 mmu_gather)
    内存紧张,kswapd 选 X 要换出 → try_to_unmap(X)
      - rmap_walk_anon:从 X->mapping 解出 anon_vma
      - 沿 anon_vma->rb_root 区间树找所有映射 X 的 VMA
      - 对每个 VMA 调 try_to_unmap_one:
          page_vma_mapped_walk:正向走到 PTE
          ptep_clear_flush + mmu_gather:清 PTE + 批量刷 TLB(第 13 章!)
          X->swap_entry = ...:PTE 改指 swap
          folio_remove_rmap_pte:X->_mapcount--
      - mapcount 减到 -1(无人映射),X 可换出
    现在:正向 PTE 改指 swap entry,反向 rmap 拆除

  ⑤ 再次访问:swap 缺页(第 19 章)
    用户再访问 v → PTE 是 swap entry → do_swap_page → 从 swap 读回 X → 重建映射
```

注意这条生命周期里**本章(rmap/gup)的角色**:

- **rmap 是回收/迁移路径的地基**——`try_to_unmap` 沿 rmap 找所有映射,是"把物理页换出去/搬走"的前提。没有 rmap,回收无法定位映射,换出无从谈起。第 5 篇(vmscan/compaction/swap)全部建立在 rmap 之上。
- **gup 是分配路径的"内核侧延伸"**——内核(DMA/IO)要访问用户页,gup 沿正向页表走、钉住物理页,是 zero-copy IO 的地基。gup pin 的页,回收/迁移要跳过——这是 rmap 和 gup 的交叉点。

> **回扣二分法**:本章是**支撑地基**——rmap 服务回收路径(让回收能找到映射),gup 服务分配路径(让内核能拿到用户页)。两者都不直接给/收物理内存,但都是"映射关系"的管理:rmap 管"物理→虚拟"的反向,gup 管"内核拿到虚拟对应的物理"。第 12~14 章 + 本章合起来,是用户地址空间的完整地基:VMA(账本)+ 页表(正向映射)+ 缺页(按需建映射)+ rmap(反向索引)+ gup(内核访问)。第 5 篇(回收)就建立在这个地基之上——紧张时,沿 rmap 解除映射,把物理页换出去/搬走。

---

## 章末小结

这一章是第 4 篇(用户地址空间)的收尾。我们没碰新的物理内存管理机制(buddy/slab/VMA/页表/缺页都已立),讲了两件支撑性的事:**rmap 反向映射**(物理页 → 所有映射它的 VMA)和 **gup/pin**(内核钉住用户页给 DMA/IO)。

钉死四件事:

1. **rmap 的本质**:物理页 → VMA(及偏移)的反向索引,让回收/迁移反查"谁映射了这页"变成 O(映射数) 而非 O(总页表大小)。从 VMA 沿正向页表走到 PTE 是 O(页表深度),廉价。
2. **匿名页 rmap**:`anon_vma`(每 VMA 一个,fork 后共享 root)+ `anon_vma_chain`(连接器)+ `anon_vma->rb_root` 区间树(按页偏移索引 VMA)。`folio->mapping` 低位置位编码 anon_vma 指针。
3. **文件页 rmap**:`address_space->i_mmap` 区间树(按文件偏移索引 VMA),第 12 章 VMA 的 `shared.rb` 节点挂这里。"object-based rmap",文件是天然索引键。
4. **gup/pin**:`get_user_pages`/`pin_user_pages` 沿正向页表走、钉住物理页给 DMA;`FOLL_PIN` 阻止回收和迁移(`FOLL_GET` 只阻止回收),DMA 必须用 pin;`try_to_unmap` 检查 pin 标志跳过。

三个关键技巧:

- **rmap 反向索引 + 两级结构**:物理→VMA(反向,anon_vma 区间树或 address_space 区间树)→ PTE(正向,沿页表走),把反查从 O(总页表) 降到 O(映射数)(反面对比"扫描所有页表 → kswapd 卡死")。
- **`folio->mapping` 低位置位**:一个字段两用(anon_vma vs address_space),低位区分,省字段;`WRITE_ONCE` 保证无锁读者不读到中间状态(反面对比"两个 store 拆开 → rmap 误认 address_space 而 crash")。
- **gup pin 语义 + 迁移检查配合**:DMA 用 pin(pin 标志 + 计数),回收/迁移检查 pin 跳过——"标志 + 检查"解耦,而非粗粒度锁(反面对比"DMA 只 get 不 pin → 迁移弄走页 → DMA 写错页数据错乱")。

### 五个"为什么"清单

1. **为什么需要 rmap?它解决什么问题?** 回收/迁移要换出/搬走物理页,必须先解除它所有 PTE 映射。朴素扫描所有进程页表是 O(总页表大小),不可行;rmap 让反查变成 O(映射数)。
2. **匿名页和文件页的 rmap 有什么区别?** 匿名页用 `anon_vma` + `anon_vma_chain`(多对多连接器)+ `anon_vma->rb_root` 区间树(按页偏移索引 VMA);文件页直接用 `address_space->i_mmap` 区间树(按文件偏移索引 VMA),因为文件是天然索引键(object-based rmap),结构简单得多。
3. **`folio->mapping` 为什么低位置位?** 一个字段两用——匿名页存 anon_vma 指针(低位 `PAGE_MAPPING_ANON` 置位),文件页存 address_space 指针(低位不置位)。反查时看低位分流。省了一个字段,且 `WRITE_ONCE` 保证无锁读者不读到中间状态。
4. **gup 的 `FOLL_PIN` 和 `FOLL_GET` 有什么区别?为什么 DMA 必须用 pin?** `FOLL_GET` 只加 refcount 防回收,但允许迁移;`FOLL_PIN` 加独立 pin 计数,同时防回收和防迁移。DMA 期间页被迁移会导致 DMA 写错页数据错乱,所以 DMA 必须用 pin。回收/迁移路径检查 pin 标志跳过 pin 的页。
5. **rmap 和缺页、和 gup、和 mmu_gather 分别什么关系?** rmap 和缺页对称——缺页建正向映射 + 种 rmap 种子(`folio_add_new_anon_rmap`),rmap 沿反向索引回收时拆映射(`folio_remove_rmap_pte`)。gup 沿正向页表走(和缺页共享基础设施),pin 的页 rmap 的 `try_to_unmap` 会跳过。rmap 的 `try_to_unmap_one` 删 PTE 时用 mmu_gather(第 13 章)批量刷 TLB——rmap 是回收地基,mmu_gather 是回收抗抖动工具,两者协作。

### 想继续深入往哪钻

- **源码**:
  - [`mm/rmap.c`](../linux/mm/rmap.c):本章主角之一。重点读 `__anon_vma_prepare`(L188)、`__folio_set_anon`(L1234)、`folio_add_new_anon_rmap`(L1406)/`folio_add_anon_rmap_ptes`(L1361)/`folio_remove_rmap_ptes`(L1587)、`try_to_unmap`(L1944)/`try_to_unmap_one`(L1616)、`rmap_walk_anon`(L2569)/`rmap_walk_file`(L2618)。
  - [`include/linux/rmap.h`](../linux/include/linux/rmap.h):`struct anon_vma`(L31)、`struct anon_vma_chain`(L82)、`struct rmap_walk_control`(L714)。
  - [`mm/gup.c`](../linux/mm/gup.c):本章主角之二。重点读 `get_user_pages`(L2391)/`pin_user_pages`(L3394)/`get_user_pages_fast`(L3294)/`get_user_pages_fast_only`(L3260)、`__get_user_pages`(L1186)、`follow_page_mask`(L811)、`unpin_user_pages`(L469)、FOLL_PIN/FOLL_GET 注释(L107-L130)。
  - [`mm/interval_tree.c`](../linux/mm/interval_tree.c):`vma_interval_tree_insert`、`anon_vma_interval_tree_insert`(L75)等区间树操作。
  - [`mm/mmap.c`](../linux/mm/mmap.c):`__vma_link_file`(L384)/`vma_interval_tree_insert`(L391)(文件页 rmap 挂载点)。
  - [`include/linux/fs.h`](../linux/include/linux/fs.h):`struct address_space`(L463)、`i_mmap`(L473)。
  - [`include/linux/mm_types.h`](../linux/include/linux/mm_types.h):`struct folio` 的 `_mapcount`/`_nr_pages_mapped`/`_entire_mapcount`/`_total_mapcount` 字段。
- **观测**:
  - `cat /proc/<pid>/smaps_rollup | grep -E 'Anonymous|Pte|Pmd'`:看进程匿名页量、页表内存。
  - `cat /proc/<pid>/status | grep -E 'VmPTE|VmPMD'`:看页表页内存(rmap 的正向基础设施)。
  - `cat /proc/vmstat | grep -E 'pgmigrate|numa_hint'`:看页迁移次数(NUMA balancing、compaction 触发 rmap + try_to_unmap)。
  - `perf stat -e 'rmap_walk:*',major-faults <cmd>`:如果想观测 rmap 走的次数。
  - 写个程序 `mmap` 大块内存 + 触发 swap(用 `mlock` 反例或调小 `vm.swappiness`),用 `perf trace -e '*mm:*'` 观察 `try_to_unmap`、`folio_remove_rmap_*` 的调用。
  - 想看 gup:写个 O_DIRECT IO 程序(`io_uring` 或 `pread` with `O_DIRECT`),用 `perf stat -e 'gup:*'` 观测 gup 次数——每次 O_DIRECT 都会 pin/unpin 页。
  - 想"看到" rmap 结构:用 `crash` 工具(需要 vmlinux + vmcore),对某个 folio 跑 `struct folio.mapping`、`anon_vma.rb_root`,能直接看到 anon_vma 区间树。或用 `/proc/<pid>/pagemap` + `/proc/kpageflags`(需要 root)看页的映射关系。
- **延伸**:
  - **KSM(Kernel Samepage Merging)**:把内容相同的匿名页合并成一个共享页,省内存(虚拟化场景)。KSM 合并的页 rmap 列表有多个映射(多个 VMA 指向同一个物理页),是 rmap 多映射场景的极致。详见第 20 章。
  - **`page_vma_mapped_walk`**:rmap 反查到 VMA 后,正向走页表定位 PTE 的工具函数(`mm/rmap.c`)。它处理大页(透明大页 PMD-mapped)、连续 PTE、swap entry 等复杂情况,是 rmap "第二级"的核心。
  - **GUP pin vs COW 的竞态**:Linux 5.x 后期有过著名的 CVE-2020-29374 一族"GUP 读 COW 共享页"安全问题。6.x 引入 `PageAnonExclusive`(第 14 章)和 gup 强制 unshare(`FAULT_FLAG_UNSHARE`)是修复的一部分。`tools/testing/selftests/mm/gup_test.c`、`mm/gup_test.c` 有相关测试。
  - **userfaultfd + rmap**:userfaultfd(用户态接管缺页)和 rmap 强相关——`uffd-wp`(write-protect)要在 PTE 上设标记,`try_to_unmap` 要尊重这些标记。QEMU 用 userfaultfd 做虚拟机热迁移。
  - **memcg 与 rmap**:页被加进 rmap 时(`folio_add_new_anon_rmap`)会更新 memcg 的匿名页计数;`try_to_unmap` 解除时减。memcg 限额统计建立在 rmap 之上。
  - **`folio->_mapcount` 的负数语义**:mapcount 是 `atomic_t`,初值 -1(无映射),`folio_add_new_anon_rmap` 设成 0(1 个映射)。减到 -1 表示无人映射。这种"用负数表示空"的计数是 mm 里常见的省字段技巧。

### 引出第 5 篇:回收与规整

本章和第 13 章(页表)、第 14 章(缺页)一起,构成了用户地址空间的**完整地基**:VMA(账本)+ 页表(正向翻译)+ 缺页(按需建映射)+ rmap(反向索引)+ gup(内核访问)。

但有个问题一直悬着:**物理内存是有限的,第 14 章按需给出去的页,什么时候收回来?** 答案是第 5 篇——**回收路径**。

- **第 16 章(watermark + kswapd)**:每个 zone 有水位(high/low/min),空闲页低于 low 时,kswapd 后台线程开始回收——而回收要走本章的 `try_to_unmap` 沿 rmap 解除映射。
- **第 17 章(LRU + workingset + vmscan)**:页按活跃度排 LRU,vmscan 决定回收谁。回收一个 anon 页要走 rmap 解除映射、换到 swap;回收一个文件页要走 rmap 解除映射、丢弃(干净)或回写(脏)。
- **第 18 章(compaction)**:碎片化时把 MOVABLE 页搬走凑连续大页——迁移就是"沿 rmap 解除旧映射 + 建新页 + 沿 rmap 建新映射",全程 rmap。
- **第 19 章(swap + OOM)**:anon 页换出到 swap 设备,实在没了 OOM killer 挑进程杀。swap 换出走 rmap + `try_to_unmap`,swap 换回(第 14 章 `do_swap_page`)重建映射。

**第 5 篇全部建立在第 4 篇的地基上**——回收/迁移要操作映射,就得用 rmap;要删大量 PTE,就得用 mmu_gather;要判断页能不能动,就得检查 gup pin。这就是为什么第 4 篇(P12-P15)是支撑地基——没有它,第 5 篇的回收无从谈起。

> 迷路时回到二分法:本章是**支撑地基**——rmap 服务回收路径(让回收能反查映射),gup 服务分配路径(让内核能钉住用户页)。第 4 篇(用户地址空间:P12 VMA + P13 页表 + P14 缺页 + P15 rmap/gup)是分配路径的虚拟层 + 支撑地基。第 5 篇(回收)站在第 4 篇对面,把按需给出去的页、紧张时再按需收回来——收回时沿 rmap 找映射、用 mmu_gather 批量刷 TLB、检查 gup pin 跳过钉住的页。两篇合起来,才是"内存分出去又收回来"的完整旅程。
