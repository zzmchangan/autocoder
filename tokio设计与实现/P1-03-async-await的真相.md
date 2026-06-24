# 第 3 章 · async/await 的真相

> **核心问题**:编译器到底把一个 `async fn` 变成了什么?`.await` 这一个关键字,在底层做了哪几件事?为什么 `async fn` 生成的 Future **是自引用的**,而 Rust 必须发明 `Pin` 才能安全地持有它?——以及那个最让人摸不着头脑的设计:`Pin` 凭什么把"自引用带来的 unsafe"关进笼子,关在笼子的哪一层?
>
> 这一章我们不碰调度器、不碰 reactor,只盯**一个 `async fn` 从你写下它、到编译器吐出状态机、到运行时把它 poll 起来**这条链路上的语言/标准库真相。
>
> **读完本章你会明白**:
> - 编译器怎么把一个 `async fn` 翻译成"一个 enum(每个 `.await` 是一个状态)+ 一个 `impl Future` 的 `poll`(按状态跳转)"——你写顺序代码的语法,编译器替你扛下"翻状态机"这件脏活,这就是 async/await 的全部魔法。
> - `.await` 在底层做了哪三件事:把内层 Future 拉出来 poll 一次;Pending 就把当前 Future 也挂起(并保证内层未来会唤醒自己);Ready 就拿结果、做状态迁移、继续往下。
> - 为什么 async 状态机会**自引用**:跨 `.await` 活着的局部变量,被借用到另一些局部变量上,于是状态机结构体的某个字段,指向**同一个结构体的另一个字段**——移动它会让旧地址失效、变成野指针 UB。
> - `Pin`/`Unpin`/`PhantomPinned` 这套设计,凭什么把"自引用带来的 unsafe"在类型系统层关进笼子:谁有义务保证"不被移动"、unsafe 边界在哪一层、为什么上层能安全。
>
> **如果一读觉得太难**:先只记住三件事——① `async fn` 会被编译成一个状态机 enum(每个 await 是一个状态);② 这个状态机会**自引用**,所以一旦生成出来就**不能被移动**;③ `Pin` 是个"承诺不可移动"的包装类型,运行时把 Future 堆分配后,用 `Pin` 包住,从此谁也移动不了它。这三条记牢,后面看 tokio 怎么 poll task 就不慌了。Pin 的完整 sound 性证明,看不懂可以先跳过,等读完第 5 章 task 布局再回来。

---

## 章首·一句话点破

> **`async fn` 是一个"语法糖":你写得像同步代码,编译器把它翻成一个状态机;状态机里跨 await 活着的变量,会借来借去、最终自引用;自引用的东西不能移动,所以 Rust 造了个 `Pin` 把它焊死在堆上的某个位置——从此它永远只在那一个地址上被反复 poll。**

这是**结论**。这一章倒过来拆:先看一个真实的 `async fn`,亲手把它"手工展开"成状态机,看清编译器干了什么、`.await` 在底层是哪几个动作;然后追问"翻出来的状态机为什么自引用",从最简单的一段代码里揪出自引用指针;最后讲 `Pin` 凭什么把这件事变得安全,unsafe 关在哪一层、谁负责保证。

第 2 章末尾埋了两个钩子:**"编译器把 async fn 翻译成状态机"** 和 **"Pin 因自引用而必须存在"**。本章一口气回答这两个。

---

## 一、先看一个真实的 async fn:它"看起来"是什么

我们要拆的目标,是一段再普通不过的 async 代码。它读一条"长度前缀"的消息:先读 2 字节长度,再按长度读 N 字节数据,最后解析。这种逻辑在网络协议里铺天盖地(HTTP chunked、TLS record、WebSocket frame、自定义 RPC……几乎都是"先读长度、再读 body"的形状)。

```rust
// 你写的(看起来完全像同步代码)
async fn read_message(sock: &TcpStream) -> Result<Msg, io::Error> {
    let mut len_buf = [0u8; 2];
    sock.read_exact(&mut len_buf).await?;        // .await 点 1
    let len = u16::from_be_bytes(len_buf) as usize;
    let mut data = vec![0u8; len];
    sock.read_exact(&mut data).await?;            // .await 点 2
    Ok(Msg::parse(&data))
}
```

注意这段代码里有几个关键细节,它们正是状态机展开的全部难点:

1. **两个 `.await`**,中间夹着计算(`u16::from_be_bytes`、`vec![0u8; len]`)。意味着这段函数会被切成**三段**:await 点 1 之前、await 点 1 到 2 之间、await 点 2 之后。
2. **跨 await 活着的变量**:`len_buf`(在 await 点 1 被 `read_exact` 借用,要活过 await 点 1);`data`(在 await 点 2 被借用,要活过 await 点 2);`sock`(全程活着)。这些变量**不能像普通函数局部变量那样放栈上**,因为函数返回(Pending)后栈帧就没了,下次 poll 是另一次函数调用,栈帧都换了。
3. **借用关系**:`sock.read_exact(&mut len_buf)` 这一调用,`&mut len_buf` 这个借用,**横跨了 await 点 1**——在 `read_exact` 还没 Ready 之前,这个借用一直有效。这意味着状态机里"那个还没完成的 `read_exact` Future"手里攥着一个指向 `len_buf` 的 `&mut`,而 `len_buf` 就在状态机自己肚子里。

第 1 条是状态机的骨架;第 2、3 条合起来,就是"自引用"的来源——本章后半段会专门拆。

> **比喻回到餐厅**:你把这张订单交给服务员时,写的不是"上菜"两个字,而是一张**分了三步的清单**——"先去厨房问 3 号桌的菜好没好(步骤 1);好了再问 4 号桌的(步骤 2);都好了就一起端出去"。服务员**每次回来,从清单上读到哪步了、接着做**,不用从头重问。这张清单,就是编译器从你的 `async fn` 翻出来的状态机。而清单上"步骤 1 还没拿到菜"时,清单上记着"我手里攥着 3 号桌取餐号的便签"——这张便签指向**清单自己的另一栏**(自引用),挪动整张清单,便签就指错了。

---

## 二、手工展开:编译器把 async fn 翻成了什么

现在,我们**人肉**当一回编译器,把上面的 `read_message` 翻成等价的状态机。理解了这个,你就理解了 `async fn` 的全部魔法。

> **说明**:下面这段代码是**手工编写的等价示意**,不是 rustc 的真实输出(rustc 生成的代码要更复杂、有更多优化)。但它**在语义上等价**——`async fn` 的真实展开就是这个形状。想看 rustc 真实输出,可用 `cargo expand` 或 `RUSTFLAGS="-Z mir-opt-level=0"` 配合 nightly 的 MIR 输出(章末"想继续深入"会给工具)。

### 第一步:把局部变量,变成结构体字段

`async fn` 的局部变量,凡是**跨 await 点活着**的,都得从栈上搬进状态机结构体里——因为函数返回(Pending)后栈帧就没了,只有结构体能活到下次 poll。

```rust
// 简化示意,展开后的等价形态(非编译器真实输出)
struct ReadMessage<'a> {
    sock: &'a TcpStream,        // 函数参数,全程活着
    // 跨 await 点 1 的状态
    len_buf: [u8; 2],            // read_exact 还在用它(&mut),要活过 await 1
    // 跨 await 点 2 的状态
    len: usize,                  // await 1 和 2 之间算出来的
    data: Vec<u8>,               // read_exact 还在用它(&mut),要活过 await 2
    // 嵌套的子 Future:每个 .await 对应一个内层 future 字段
    read_exact_fut: Option<ReadExact<'a>>,   // read_exact 返回的那个 Future
    state: State,                // 当前在哪一步
}

enum State {
    S0,                          // 还没开始读 len_buf
    S1,                          // 正在 await 点 1(读 len_buf)
    S2,                          // 正在 await 点 2(读 data)
    Done,
}
```

注意三个细节:

1. **`len` 这个变量,在 `S0` 阶段还不存在**(它要等 `len_buf` 读出来才能算)。真实编译器会用 enum 变体分别存不同阶段的变量(下面会看到),这里为了清楚先平铺成一个 struct。
2. **`read_exact_fut`**:这是关键——`sock.read_exact(&mut len_buf).await` 这一行,`read_exact` 是个 `async fn`,调用它返回一个 Future,`.await` 会 poll 这个 Future。这个 Future 也要**跨 poll 活着**,所以它也是状态机的一个字段。
3. **`Option<ReadExact>`**:用 Option 是因为不同阶段这个字段可能是空的(还没开始或已经结束),真实编译器用 enum 变体表达得更精确。

### 第二步:把函数体,变成 `poll` 里的状态跳转

现在把函数体翻成 `impl Future` 的 `poll`。核心思路:**`poll` 是个 `match self.state`,每个状态对应原函数里"两段 await 之间"的那段代码**。

```rust
// 简化示意,展开后的等价形态(非编译器真实输出)
impl<'a> Future for ReadMessage<'a> {
    type Output = Result<Msg, io::Error>;

    fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        let this = unsafe { self.get_unchecked_mut() };   // 见后文,这里 unsafe 有讲究

        loop {
            match this.state {
                State::S0 => {
                    // 对应:sock.read_exact(&mut len_buf).await
                    // 第一次进入,造出 read_exact 的 Future
                    let fut = this.sock.read_exact(&mut this.len_buf);
                    this.read_exact_fut = Some(fut);
                    this.state = State::S1;
                    // 落到 S1
                }
                State::S1 => {
                    // 对应:.await 点 1 —— poll 内层 future
                    let fut = this.read_exact_fut.as_pin_mut().unwrap();
                    ready!(fut.poll(cx))?;                 // ① poll 内层;② Pending 就 return Pending;③ Ready 就拿结果、继续
                    this.read_exact_fut = None;

                    // 对应:let len = u16::from_be_bytes(len_buf) as usize;
                    let len = u16::from_be_bytes(this.len_buf) as usize;
                    this.len = len;
                    this.data = vec![0u8; len];

                    // 对应:sock.read_exact(&mut data).await
                    let fut = this.sock.read_exact(&mut this.data);
                    this.read_exact_fut = Some(fut);
                    this.state = State::S2;
                }
                State::S2 => {
                    // 对应:.await 点 2
                    let fut = this.read_exact_fut.as_pin_mut().unwrap();
                    ready!(fut.poll(cx))?;
                    this.read_exact_fut = None;

                    // 对应:Ok(Msg::parse(&data))
                    let msg = Msg::parse(&this.data);
                    this.state = State::Done;
                    return Poll::Ready(Ok(msg));
                }
                State::Done => unreachable!("poll after Ready"),
            }
        }
    }
}
```

把这段 `poll` 和原始 `async fn` 对着看,你会发现 **`.await` 这一个关键字,在底层就做了三件事**:

| `.await` 在底层做的事 | 代码里对应 |
|------|------|
| ① 把内层 Future 拿出来,`poll` 它一次 | `fut.poll(cx)` |
| ② 内层返回 `Pending` → 当前 Future 也立即返回 `Pending`(并保证内层未来唤醒时,会通过 `cx` 的 Waker 把自己也叫醒) | `ready!` 宏展开成 `match fut.poll(cx) { Pending => return Pending, Ready(v) => v }` |
| ③ 内层返回 `Ready(v)` → 拿到结果,执行状态迁移(`state = S2`),`loop` 继续往下走 | `this.state = State::S2;` 然后 loop 回到 match 顶部 |

`ready!` 宏是这三件事里②和③的糖:

```rust
// 标准库风格宏(简化)
macro_rules! ready {
    ($e:expr) => {
        match $e {
            Poll::Pending => return Poll::Pending,   // ② 内层没好,我也没好,挂起
            Poll::Ready(v) => v,                      // ③ 内层好了,拿结果继续
        }
    };
}
```

(标准库的 `std::task::ready!` 见 [`std::task::ready`](https://doc.rust-lang.org/std/task/macro.ready.html))

> **钉死这件事**:`.await` 不是"阻塞等待",也不是"注册回调"。它就是**"poll 内层 Future,没好就我也 return Pending,好了就接着往下"**。一个 `.await` 点,就是一个**状态切换点**:内层 Pending,当前 Future 就停在这个状态;内层 Ready,当前 Future 就迁移到下一个状态。整段 `async fn` 就是被这些 `.await` 点切成了若干段,每段是一个状态。

### 一张状态图,看清跳转

```mermaid
stateDiagram-v2
    [*] --> S0
    S0 --> S1: 构造 read_exact(len_buf)
    S1 --> S1: poll 内层 → Pending(挂起,被叫醒再回)
    S1 --> S2: poll 内层 → Ready,算 len,构造 read_exact(data)
    S2 --> S2: poll 内层 → Pending(挂起)
    S2 --> Done: poll 内层 → Ready,解析 Msg
    Done --> [*]: Poll::Ready(Ok(Msg))
```

每个状态,都是"原函数里两个 await 点之间的一段代码"。`poll` 一次,要么在某个状态里 Pending(停在那),要么迁移到下一个状态。最终在 `Done` 状态返回 `Ready`。

> **对比"手写状态机"和 `async fn`**:第 2 章我们看过 tokio 手写的 `MaybeDone` 状态机(三态)。你现在发现——**`async fn` 干的就是同一件事**,只不过编译器**自动**帮你干了。你写 `async fn read_message`,编译器吐出来的就是类似上面这样一个 `struct + enum State + impl Future`。`async fn` 的全部价值,就是**让你用顺序代码的语法,写出状态机的效果**,把"翻状态机、维护字段、状态迁移"这件容易写错的脏活,甩给编译器。

> **钉死这件事(承接第 2 章)**:第 2 章我们说"Future 必须是状态机",并立了 `MaybeDone` 这个手写样本。这一章把另一半补上:**`async fn` 让你不用手写状态机**。这就是 async/await 在 Rust 里的角色——一个**语法糖**,把人肉维护状态机的痛苦,变成编译器的一个 pass。

---

## 三、为什么状态机会"自引用"——Pin 的根因

到这里你可能会觉得:展开就展开呗,有什么大不了的?**真正的大麻烦,藏在你刚才那段展开代码的 `get_unchecked_mut` 那行 `unsafe` 里**。它背后是一颗 sound 性的炸弹,逼出了 Rust 标准库的 `Pin`。

我们要追问一个非常具体的问题:**这个展开出来的 `ReadMessage` 状态机,为什么不能被移动(move)?**

### 自引用是怎么长出来的

回到这段原始代码:

```rust
let mut len_buf = [0u8; 2];
sock.read_exact(&mut len_buf).await?;
```

`read_exact` 是个 `async fn`,调用它返回一个 Future。这个 Future 内部要**持有 `&mut len_buf` 这个借用**(它得知道往哪个缓冲区写数据)。而 `len_buf` 是谁?**就是状态机 `ReadMessage` 自己的一个字段**。

于是,展开后的状态机里,出现了这样的内存布局:

```
   ReadMessage 状态机(整体在堆上某个地址,比如 0x1000)
   ┌────────────────────────────────────────────────────────┐
   │ sock:       &TcpStream   (指向外部,无自引用问题)        │  ← 0x1000
   │ len_buf:    [u8; 2]                                    │  ← 0x1008
   │ data:       Vec<u8>      (Vec 自身有指针,指向堆)         │  ← 0x1010
   │ read_exact_fut: ReadExact {                            │  ← 0x1028
   │     buf: &mut [u8]  ─────────────────┐  指向哪?        │
   │     ...                              │                 │
   │ }                                    │                 │
   │ state: State                         │                 │
   └──────────────────────────────────────┼─────────────────┘
                                          │
                                          └──→ 指向 0x1008(len_buf 自己的地址)
                                               这就是"自引用"!
```

`read_exact_fut.buf` 这个指针,**指向了同一个状态机里的 `len_buf` 字段**——它指向**自己**(同一个结构体的另一个字段)。这叫**自引用(self-referential)**。

> **不这样会怎样(反面,致命)**:假设我们允许把这个状态机 `move` 到另一个地址(比如 `mem::swap`,或 `Box::new` 之后又 `*Box`,或塞进 `Vec` 触发扩容迁移)。状态机整体搬到了 0x2000,但 **`read_exact_fut.buf` 里存的还是 0x1008 这个旧地址**——因为指针的值是个数字,move 不会去更新它。现在 `buf` 指向的 0x1008 已经是**旧地址、旧内存**(可能已被别人复用)。下一次 poll,`read_exact` 试图往 `buf` 写数据,**写到了野指针指向的地方**——UB(undefined behavior),可能是数据损坏,可能是 segfault,可能是莫名其妙的安全漏洞。
>
> ```
>   move 之前:                              move 之后(状态机搬到 0x2000):
>   ┌──────────────┐ 0x1000                 ┌──────────────┐ 0x2000
>   │ len_buf      │ 0x1008  ←──┐           │ len_buf      │ 0x2008(新地址)
>   │ fut.buf: 0x1008 ──────────┘           │ fut.buf: 0x1008 ──→ 野指针!(旧地址已失效)
>   └──────────────┘                        └──────────────┘
> ```

这就是 Rust 必须解决的核心 sound 性问题:**自引用类型不能被 move,move 会让自指针变成野指针**。

> **比喻回到餐厅**:那张分三步的清单,上面"步骤 1"那一栏,记着"3 号桌取餐号写在**这张清单的左下角**"——这是一个指向**清单自己**的内部引用。现在你**复印了清单、把原件扔了**(`move`),复印件上"步骤 1"那栏还是写着"看左下角"——可原件的左下角已经没了,复印件的左下角是另一份内容。**内部引用失效,清单就废了。** 自引用的东西,不能复印后扔原件。

### 不是所有 async 状态机都自引用——但运行时不能赌

要强调一点:**并非每个 `async fn` 展开后都自引用**。如果你的 async fn 里所有跨 await 的变量都是 `Copy` 的、彼此不借用(比如 `async fn add(a: u32, b: u32) { a + b }`),展开的状态机就没有自指针。

但问题在于:**编译器知道每个 async fn 自不自引用,但 `Future` trait 的签名是统一的**——`poll(self: Pin<&mut Self>, ...)`,它不管你自不自引用,一律按"可能自引用"处理。因为如果 trait 签名分两套(自引用一套、非自引用一套),整个 async 生态的泛型代码会爆炸。Rust 选择**统一按最严格的来**:所有 async fn 生成的 Future,都视为"可能自引用",一律 `Pin` 住。

这就是为什么你随便写个 `async fn`,得到的 Future 类型都是 `impl Future`(具体类型匿名),且**默认 `!Unpin`**——编译器不敢承诺它能 move。

> **钉死这件事**:`async fn` 生成的 Future **可能**自引用(跨 await 的借用是根源)。Rust 不在 trait 层做区分,**一律按"自引用"对待**,统一用 `Pin` 焊死。这就是 `Pin` 存在的全部理由——为自引用类型兜底,顺带给所有 async Future 兜底。

---

## 四、Pin:用一个"不可移动的包装"把自引用关进笼子

现在问题清楚了:自引用的东西不能 move。Rust 的答案是——**造一个包装类型,在类型系统层承诺"被包住的东西不会被 move"**。这个包装类型,就是 `Pin<P>`。

### Pin 的定义:一句话

```rust
// 标准库定义(简化展示,完整定义见 rustdoc)
pub struct Pin<P> {
    pointer: P,
}
```

([`std::pin::Pin` —— 标准库定义](https://doc.rust-lang.org/std/pin/struct.Pin.html))

就这么简单——`Pin<P>` 是个**透明包装**(newtype),里面就一个指针 `P`(通常是 `Pin<&mut T>` 或 `Pin<Box<T>>`)。它**不改变内存布局,不加锁,不加运行时检查**。它的全部力量,在于**类型系统层的契约**:

> **Pin 的核心契约**:一旦某个值被 `Pin` 住(被包进 `Pin<P>` 且再也无法拿出 `&mut T`),**这个值就不会被移动**,直到它被 drop。

"不会移动"是怎么保证的?靠的是 `Pin` **不给你拿到 `&mut T` 的安全途径**。想想看,如果你拿到了 `&mut T`,你就能 `mem::swap` 它、`mem::replace` 它——这些都是 move。`Pin` 把所有"能拿到 `&mut T`"的方法,**要么标 `unsafe`,要么要求 `T: Unpin`**。这就是笼子。

### Unpin:大多数类型的"逃生舱"

光有 `Pin` 不够。Rust 里绝大多数类型(`u32`、`Vec<T>`、`String`、`Box<T>`……)**移动它们完全安全**——它们要么是 `Copy` 的纯数据,要么把真正的数据放在堆上(移动 `Vec` 只是把那三个字段的值拷一份,堆上数据不动,内部指针指向堆,不指向 `Vec` 自己)。对这种类型,"禁止移动"是没必要的过度保护。

所以标准库给了一个 marker trait:**`Unpin`**。

```rust
// 标准库定义
pub auto trait Unpin {}
```

([`std::marker::Unpin` —— 标准库定义](https://doc.rust-lang.org/std/marker/trait.Unpin.html))

`auto trait` 意思是**编译器自动推导**——所有类型默认 `Unpin`,除非它内部含 `PhantomPinned` 这个特殊标记。`Unpin` 的语义是:**这个类型移动起来是安全的,不需要 Pin 保护**。

有了 `Unpin`,`Pin` 就有了"逃生舱":

- **对 `T: Unpin` 的类型**:`Pin<&mut T>` 可以**安全地**变回 `&mut T`(`Pin::into_inner` 这种方法是 safe 的,只要 `T: Unpin`)。换句话说,Pin 对 Unpin 类型**形同虚设**——你想拿 `&mut T` 去 swap 都行,反正它移动安全。
- **对 `T: !Unpin` 的类型**(即自引用类型,主要是 async fn 生成的 Future):`Pin<&mut T>` **不会**给你安全的 `&mut T`。想拿,只能用 `unsafe` 的 `Pin::get_unchecked_mut`,并由**调用者**承担"我保证不真的移动它"的义务。

> **钉死这件事**:`Pin` 不是一刀切禁所有移动,而是**只对真正需要保护的类型(`!Unpin`)保护**。靠的就是 `Unpin` 这个 marker——大多数类型自动 `Unpin`,Pin 对它们不起作用;少数自引用类型 opt-out(用 `PhantomPinned` 标记自己 `!Unpin`),Pin 对它们执行铁律。这种"默认放开、出问题的 opt-out"的设计,既保护了 sound 性,又不给普通类型添麻烦。

### PhantomPinned:自引用类型的"opt-out"开关

那一个类型怎么"opt-out",让自己变成 `!Unpin`?靠 `PhantomPinned`:

```rust
// 标准库定义
pub struct PhantomPinned;
```

([`std::marker::PhantomPinned` —— 标准库定义](https://doc.rust-lang.org/std/marker/struct.PhantomPinned.html))

`PhantomPinned` 是个零大小的标记类型(ZST),它的全部作用是**让它所在的类型自动变成 `!Unpin`**(因为 `PhantomPinned` 自己 `!Unpin`,`auto trait Unpin` 不会给含它的类型推导出 `Unpin`)。

手写一个自引用结构体时,你要显式塞个 `_pinned: PhantomPinned` 字段进去,告诉世界"我是自引用的,别 move 我":

```rust
use std::marker::PhantomPinned;
use std::pin::Pin;

struct SelfReferential {
    data: [u8; 16],                 // 真正的数据
    ptr_to_data: *const u8,         // 指向自己 data 字段的指针(自引用!)
    _pinned: PhantomPinned,         // opt-out:让 SelfReferential 变成 !Unpin
}

impl SelfReferential {
    fn new() -> Pin<Box<SelfReferential>> {
        let mut b = Box::pin(SelfReferential {
            data: [0; 16],
            ptr_to_data: std::ptr::null(),
            _pinned: PhantomPinned,
        });
        // 此时 data 的地址固定了(Box::pin 后再也不会移动)
        let data_ptr = b.data.as_ptr();
        b.as_mut().get_unchecked_mut().ptr_to_data = data_ptr;
        b
    }
}
```

注意:`async fn` 生成的 Future,**编译器自动塞 `PhantomPinned`**(只要它检测到这个状态机可能自引用)。你不用手写。这就是为什么你随便一个 `async fn`,它的 Future 类型都是 `!Unpin`。

### unsafe 边界:谁负责保证 sound

这是 `Pin` 设计里**最精妙、最常被误解**的一点。它的 unsafe 不是均匀洒在各处,而是**精确关在一两个边界上**:

| 操作 | safe / unsafe | 谁承担义务 |
|------|------|------|
| `Pin::new(&mut T)` | **`unsafe`**(`T: !Unpin` 时) | 调用者必须保证:从此这个 `T` 永远不被移动(它的内存地址在 drop 前固定) |
| `Pin::new(&mut T)`(`T: Unpin` 时) | **safe** | 无需义务(`Unpin` 类型移动安全) |
| `Box::pin(T)` / `Pin::into_pin` | **safe** | 标准库保证:堆分配 + `Box` 不动,地址固定 |
| `Pin::get_unchecked_mut` | **`unsafe`** | 调用者必须保证:拿到的 `&mut T` 不被用来移动它(`swap`/`replace` 等) |
| `Pin::as_mut` / `Pin::as_ref` | **safe** | 返回的还是 `Pin<&mut T>`,没破坏契约 |
| `Pin::set`(`T: !Unpin`) | **safe** | 只能整体覆盖,不能拿到 `&mut`,所以不会移动 |
| 通过 `Pin<&mut T>` poll 一个 Future | **safe** | Future 内部就算自引用,也安全,因为它被 Pin 焊死了 |

(完整方法表见 [`Pin` 的 rustdoc](https://doc.rust-lang.org/std/pin/struct.Pin.html))

> **钉死这件事(Pin 的 sound 性架构)**:`Pin` 的 unsafe 关在**两个入口**:① **把一个值"放进 Pin"**(`Pin::new` 对 `!Unpin`);② **从 Pin 里"拿出 `&mut`"**(`get_unchecked_mut`)。这两个入口由**具体的人**承担义务——前者是谁负责"分配这块内存并保证不移动它"(通常是运行时,用 `Box::pin` 把 Future 堆分配,堆地址固定);后者是谁要 `&mut T` 谁负责"不真的移动它"。**只要这两个入口守住了**,中间所有对 `Pin<&mut T>` 的操作(`poll`、`as_mut`、`set`)都是 safe 的——因为它们要么不给你 `&mut T`,要么给你的还是 Pin 住的。**unsafe 被关在边界,内部全 safe**——这是 Rust 把 unsafe 关进笼子的标准手法(和 `Vec`/`Mutex`/`UnsafeCell` 一个套路)。

---

## 五、tokio 源码佐证:task 怎么持有并 poll 一个 Pin 住的 Future

讲了这么多抽象,现在看 tokio 在源码里**真实**怎么落地这套设计。这一节是本章的硬证据:**tokio 全程在 `Pin<&mut ...>` 上操作,且把 unsafe 关在了"堆分配 task"那一个点上**。

### task 怎么存 Future:堆分配 + CoreStage

`tokio::spawn(future)` 之后,这个 Future 被存到哪?答案是——**堆上一个固定的位置**,具体是在一个叫 `Core` 的结构体的 `stage` 字段里:

```rust
// tokio/src/runtime/task/core.rs(摘录,简化注释)
pub(super) struct CoreStage<T: Future> {
    stage: UnsafeCell<Stage<T>>,    // 内部可变性 + Pin 住的 future
}

pub(super) struct Core<T: Future, S> {
    pub(super) scheduler: S,
    pub(super) task_id: Id,
    pub(super) stage: CoreStage<T>,  // future 或 output,看执行阶段
}

pub(super) enum Stage<T: Future> {
    Running(T),                       // 还在跑,future 在这
    Finished(super::Result<T::Output>),
    Consumed,
}
```

([tokio/src/runtime/task/core.rs:137-164](../tokio/tokio/src/runtime/task/core.rs#L137-L164),`Stage` enum 在 [L221-L225](../tokio/tokio/src/runtime/task/core.rs#L221-L225))

注意三个细节:

1. **整个 `Cell`(含 `Header` + `Core` + `Trailer`)是一次性堆分配的**——`spawn` 时申请一块堆内存,把 task 的所有部件塞进去。这块内存的地址,从 spawn 到 drop **永远不变**(堆上不会自己移动)。这就是 Pin 契约的"地址固定"那半边。
2. **`stage: UnsafeCell<Stage<T>>`**——`UnsafeCell` 是 Rust 标准库给的"内部可变性"原语,意思是"这块内存可以被 `&self` 安全地 mutate(在协调好同步的前提下)"。tokio 之所以用 `UnsafeCell` 而不是 `RefCell`/`Mutex`,是因为 task 的"同一时刻只有一个线程在 poll 它"是由**调度器状态机**(task 的 state 字段)保证的,不需要 `RefCell` 的运行时检查。这是性能和 sound 性的精妙平衡(第 5 章细拆 state 位)。
3. **`Stage` 是个 enum**——`Running(T)` / `Finished` / `Consumed`,正是状态机三态。和第 2 章的 `MaybeDone` 同构。

### poll:在 Pin 边界上调用 Future

最关键的代码,是 `Core::poll`。它就是把 `UnsafeCell<Stage<T>>` 里的 Future 拿出来,**用 `Pin::new_unchecked` 包成 Pin**,然后调它的 `poll`:

```rust
// tokio/src/runtime/task/core.rs(摘录)
// 注释原文:"self must also be pinned. This is handled by storing the task on the heap."
pub(super) fn poll(&self, mut cx: Context<'_>) -> Poll<T::Output> {
    let res = {
        self.stage.stage.with_mut(|ptr| {
            // Safety: The caller ensures mutual exclusion to the field.
            let future = match unsafe { &mut *ptr } {
                Stage::Running(future) => future,
                _ => unreachable!("unexpected stage"),
            };

            // Safety: The caller ensures the future is pinned.
            let future = unsafe { Pin::new_unchecked(future) };

            let _guard = TaskIdGuard::enter(self.task_id);
            future.poll(&mut cx)
        })
    };

    if res.is_ready() {
        self.drop_future_or_output();
    }

    res
}
```

([tokio/src/runtime/task/core.rs:362-384](../tokio/tokio/src/runtime/task/core.rs#L362-L384))

这段代码是本章前面讲的"Pin 的 unsafe 边界"的**教科书级落地**。我们逐句拆:

**① `with_mut(|ptr| ...)`**:`UnsafeCell` 给你一个 `*mut Stage<T>` 裸指针。tokio 用裸指针而不是 `&mut`,是因为它要在 `&self`(`poll(&self, ...)`)的前提下 mutate——Rust 借用检查器不允许 `&self` mutate,`UnsafeCell` 是逃生舱。

**② `unsafe { &mut *ptr }`**:把裸指针变回 `&mut Stage<T>`。**这里 unsafe 的义务是"调用者保证对这块内存的独占访问"**(注释 `The caller ensures mutual exclusion to the field`)。tokio 怎么保证独占?靠 task 的 state 字段——一个 task 同一时刻只可能被一个 worker 线程 poll(state 里有 `running` 位,第 5 章详拆)。**没有 data race,这个 unsafe sound**。

**③ `unsafe { Pin::new_unchecked(future) }`**:把 `&mut T` 包成 `Pin<&mut T>`。**这里 unsafe 的义务是"调用者保证这个 future 不会被移动"**(注释 `The caller ensures the future is pinned`)。tokio 怎么保证?**靠"task 在堆上、地址固定"**——这正是上一节的 `Cell` 堆分配。`Pin::new_unchecked` 的义务"从此这块内存不移动",由"堆地址永远不变"来满足。**这个 unsafe 也 sound**。

**④ `future.poll(&mut cx)`**:现在 `future` 是 `Pin<&mut T>`,调 `poll` 是 **safe** 的。Future 内部就算自引用,也安全,因为它被 Pin 焊在堆上那个固定地址,自指针永远指向有效内存。

> **钉死这件事**:看 tokio 这段 `poll`,你会看到 **Pin 的整套设计在源码里精确落地**:unsafe 关在 `Pin::new_unchecked` 这一个点上,义务由"堆分配 + 地址不变"满足;一旦包成 Pin,后续 `future.poll()` 完全 safe。**这就是 Pin 的价值——把"自引用 Future 不能移动"这件危险的事,变成一个"局部 unsafe + 上层 safe"的清晰边界**。tokio 的所有 task 操作,都建立在这个边界之上。

### 一个反向问题:为什么不直接存 `Pin<Box<T>>`,而要 `UnsafeCell<Stage<T>>`?

读者可能问:既然 Future 要 Pin,为什么不直接在 `Stage` 里存 `Pin<Box<T>>`,省掉 `Pin::new_unchecked` 这个 unsafe?

> **不这样会怎样(反面)**:`Pin<Box<T>>` 意味着每个 Future 都额外一次堆分配(`Box`)。可 `Cell` 本身已经堆分配了一次(spawn 时申请整个 task 的内存)。**二次堆分配 = 性能直接翻倍**,对一个目标是"百万并发"的运行时,这是不可接受的开销。tokio 的设计是**一次堆分配搞定整个 task**(Header + Core + Trailer + Future 全在一个连续内存块),Future 不再单独 Box。代价是要用 `UnsafeCell` + `Pin::new_unchecked` 自己管 Pin 的 sound 性。**用一处 unsafe 换掉一次堆分配**——这是 tokio 性能致胜的典型取舍,也是 `unsafe` 在系统级 Rust 里"用得其所"的范例。

---

## 技巧精解:自引用状态机 + Pin 的 sound 性架构

这一节是本章的硬核,把"自引用"和"Pin 为什么 sound"这两件事彻底拆透,配反面对比,让妙处显形。

### 技巧一:为什么 async 状态机会自引用——一个最小的复现

很多人理解"自引用"是抽象的,觉得"我的代码不会写出自引用吧"。事实是——**最普通的 async 代码就会**。我们用一个最小例子,亲手揪出自指针:

```rust
async fn self_ref_demo() -> u32 {
    let mut buf = [0u8; 4];                    // 栈上局部变量
    some_async_read(&mut buf).await;           // 这一行就是自引用的根源
    u32::from_le_bytes(buf)
}
```

展开后(简化示意):

```rust
struct SelfRefDemo {
    buf: [u8; 4],                              // 字段 A
    read_fut: Option<ReadFut>,                 // 字段 B,内含 &mut [u8]
    state: State,
    _pinned: PhantomPinned,                    // 编译器自动加的
}
```

`some_async_read(&mut buf).await` 这一行,`some_async_read` 返回的 `ReadFut` 内部**持有 `&mut buf`**。`buf` 在哪?**在 `SelfRefDemo` 自己的 `buf` 字段里**。所以 `ReadFut.buf_ptr` 指向 `SelfRefDemo.buf`——**字段 B 指向字段 A,自引用**。

> **反面对比**:如果 Rust 允许你这么写——
> ```rust
> // 简化示意,非源码原文:假设允许移动自引用 Future(实际 Rust 禁止)
> let fut = self_ref_demo();           // fut 在栈上,地址 0x7ff0
> let mut fut_pin = unsafe { Pin::new_unchecked(&mut fut) };
> fut_pin.as_mut().poll(cx);           // 进入 S1,read_fut.buf_ptr = 0x7ff0(指向 fut.buf)
>
> // 现在我们作弊:把 fut 移动到堆上
> let boxed = Box::new(fut);           // fut 整体搬到 0x1234_0000(堆)
>                                       // 但 read_fut.buf_ptr 还是 0x7ff0(旧栈地址)!
>                                       // 0x7ff0 现在是悬空栈帧,随时被新函数调用覆盖
> boxed.poll(cx);                      // read_fut 往 0x7ff0 写数据 → 写进别人的栈 → UB
> ```
>
> 这就是 Rust 死活要避免的。**没有 Pin,任何 async fn 都是一颗潜在的 UB 炸弹**——只要有人不小心 move 了 Future。Pin 的全部存在意义,就是把这种"不小心 move"在类型系统层堵死。

### 技巧二:Pin 的 sound 性架构——三层分工

`Pin` 最让人困惑的是:**它有 `unsafe` 函数,但绝大多数使用是 safe 的。unsafe 到底在哪、谁负责?** 把这件事讲清,你就理解了 Pin 的全部精妙。

我把 Pin 的 sound 性拆成**三层分工**:

```
   层级一:地址固定(谁负责?)
   ┌─────────────────────────────────────────────┐
   │ 运行时(spawn 时堆分配 task,地址永不变)      │ ← 用 Box::pin / 一次性堆分配保证
   │ 用户代码(Box::pin(future) 拿到 Pin<Box<F>>) │ ← safe API,标准库兜底
   └─────────────────────────────────────────────┘
                       ↓
   层级二:Pin 包装(谁负责?)
   ┌─────────────────────────────────────────────┐
   │ Pin::new(&mut T)   T: Unpin → safe          │ ← 普通 API
   │ Pin::new(&mut T)   T: !Unpin → unsafe       │ ← 调用者承诺"地址已固定"
   │ Box::pin(T)        → 任何 T 都 safe         │ ← 标准库内部用了 unsafe,但替你担保
   └─────────────────────────────────────────────┘
                       ↓
   层级三:使用(谁负责?)
   ┌─────────────────────────────────────────────┐
   │ Pin<&mut T>.poll(cx)              → safe     │ ← 不给你 &mut T,你 swap 不了
   │ Pin<&mut T>.as_mut()              → safe     │ ← 返回的还是 Pin
   │ Pin<&mut T>.get_unchecked_mut()   → unsafe   │ ← 你拿 &mut T,你得保证不移动它
   │ Pin<&mut T>.set(new_value)        → safe     │ ← 整体覆盖,不暴露 &mut
   └─────────────────────────────────────────────┘
```

**关键洞见**:unsafe 只在"**穿越 Pin 边界**"的两个点出现——**进**(把一个值放进 Pin,对 `!Unpin` 不安全)和**出**(从 Pin 拿出 `&mut T`)。**中间所有操作都是 safe 的**,因为它们要么不暴露 `&mut T`,要么暴露的还是 `Pin<&mut T>`。

这跟 `Mutex` 的设计是同构的:

- `Mutex` 把"内部可变性 + 锁"的 unsafe 关在 `lock()` 上(锁成功后给你 `MutexGuard`),`MutexGuard` 的 `DerefMut` 是 safe 的(因为锁已经持有)。
- `Pin` 把"地址固定"的 unsafe 关在 `Pin::new`/`get_unchecked_mut` 上,`Pin<&mut T>` 的 `poll`/`as_mut` 是 safe 的(因为地址已被承诺固定)。

> **钉死这件事(Pin 的设计精髓)**:`Pin` 不是"运行时检查你有没有移动",它是**纯类型系统的契约**——用 `Unpin` marker 区分"移动安全 vs 不安全",用 `PhantomPinned` 让自引用类型 opt-out,用几个 `unsafe` 函数把"穿越 Pin 边界"的操作标红,**让义务落在具体的人头上**(运行时负责"地址固定",拿 `&mut T` 的人负责"不移动")。**unsafe 关在边界,内部全 safe**——这是 Rust 把危险封进笼子的标准手法。运行时(tokio)拿到一个堆分配的 Future,用 `Box::pin` 或 `Pin::new_unchecked`(配 sound 性证明)包成 Pin,此后整个运行时对所有 task 的 poll 都是 safe 操作,**再也不用担心"不小心 move 了 Future 导致 UB"**。

### 一个常见误解:Pin 不阻止 drop

最后澄清一个常见误解:"Pin 住的东西不能移动,那它能被 drop 吗?"

**能**。drop 不是 move——drop 是"在原地址上销毁这个值",并不把它搬到别处。`Pin<&mut T>` 可以安全地 `drop` 内部的 T(`Pin` 实现了 `Drop`,或者在 `&mut T` 离开作用域时正常析构)。Pin 禁止的是"**移动到另一个地址后旧地址失效**"(swap、replace、memcpy 走),不是"销毁"。这个区别很重要,否则你会误以为 Pin 住的东西永远不能释放——那就内存泄漏了。

tokio 在 task 完成时(`poll` 返回 `Ready`),会 `drop_future_or_output`(就是前面 `core.rs:380` 那行),把 Future 在原地址析构掉,然后把 `Stage` 设成 `Consumed`。**完全安全,完全符合 Pin 契约**。

---

## 六、把抽象落到地面:async fn 的 Future 日常怎么用

讲了这么多编译器展开和 Pin 契约,容易把人绕晕。这一节把镜头拉回日常——你写 `async fn`,得到的 Future 在哪些地方需要关心 Pin?

### 日常用法一:`Box::pin` —— 把 Future 请上堆

最常见的场景:你有一个 `async fn`,想把它存进某个数据结构(比如 trait object、`Vec`),或者跨函数边界传递。这时你得把它**堆分配 + Pin 住**,因为 trait object(如 `Pin<Box<dyn Future>>`)要求固定地址。

```rust
async fn handle(conn: TcpStream) { /* ... */ }

// 把 async fn 的返回值堆分配、Pin 住,得到 Pin<Box<F>>
let fut: Pin<Box<dyn Future<Output = ()>>> = Box::pin(handle(conn));
```

`Box::pin` 是 safe API——它**自己内部**用了 `unsafe`(把堆地址的 `&mut T` 包成 Pin),但义务由"堆地址永远不变"满足,所以**对外是 safe 的**。这就是前面"层级二"里说的"标准库替你担保"——你不需要写任何 `unsafe`。

tokio 的 `spawn` 内部就是这套:接收一个 `Future`(不要求 `Unpin`),内部把它放进堆分配的 `Cell`,用 `Pin::new_unchecked` 包成 Pin。**用户侧零 unsafe**。

### 日常用法二:`tokio::pin!` —— 栈上 Pin,零分配

堆分配有开销。很多时候你只是想在**当前函数栈上** pin 住一个 Future(比如 `select!` 多个分支),没必要堆分配。tokio 提供了 `pin!` 宏:

```rust
let fut = some_async_op();
tokio::pin!(fut);          // fut 现在是 Pin<&mut SomeFut>,栈上,零堆分配

// 此后 fut 是 Pin<&mut _>,可以 .poll、可以放进 select!
```

`pin!` 宏的原理很有意思——它利用了一个 Rust 的语义细节:**shadowing 后,原变量名指向一个被 Pin 住的新绑定,而原来那个可移动的值,被"遮蔽"再也访问不到了**。具体展开大致是:

```rust
// 简化示意,非 tokio 宏真实输出
let mut fut = some_async_op();
let mut fut = unsafe { Pin::new_unchecked(&mut fut) };
//   ^^^^ 新 fut 遮蔽了旧 fut。旧 fut 再也访问不到,所以"不会被 move"的义务被满足
```

这个 `unsafe` sound 吗?sound——**因为旧 fut 被遮蔽、再也拿不到 `&mut` 它的途径**,自然没人能 move 它。`pin!` 宏把"地址固定"的义务,**靠变量遮蔽在编译期就堵死了**。这是 tokio 把 unsafe 关进笼子的又一个范例:不靠运行时检查,靠作用域和遮蔽。

> **钉死这件事**:你日常几乎从不直接写 `Pin::new_unchecked`。两个 safe 入口——`Box::pin`(堆分配,适合长期持有/跨边界)和 `tokio::pin!`(栈分配,适合临时/`select!`)——覆盖了 99% 的场景。**直接写 `Pin::new_unchecked` 的,基本只有运行时内部**(像前面 tokio 的 `Core::poll`),它们有完整的 sound 性证明。

### `.await` 如何把内层的 Waker 传给外层

回到本章第二节"`.await` 做三件事"里的第②件:内层 Pending,外层也 Pending,**且保证内层未来会叫醒自己**。这个"保证"是怎么落地的?它藏在一个常被忽略的细节里——**`Context` 和 `Waker` 的透传**。

`.await` 展开后的代码,大致是这样(简化):

```rust
// 简化示意,非编译器真实输出:.await 的核心
let waker = cx.waker();                        // 外层 poll 进来时带的 Waker
let mut inner_cx = cx;                         // 内层用同一个 Waker 构造 Context
match inner_future.poll(inner_cx) {            // 把外层的 Waker 传给内层
    Poll::Pending => return Poll::Pending,     // 内层没好,我也没好
    Poll::Ready(v) => v,
}
```

关键在 `inner_cx` 用的是**外层那个 Waker**。这意味着:**内层 Future 注册到 reactor 时,登记的"数据好了叫我"用的就是外层的 Waker**。于是:

- 数据来了,reactor 触发内层登记的 Waker → 这个 Waker 就是**外层 task 的 Waker** → 外层 task 被重新塞回调度队列 → 外层 task 再次被 poll → 它的 `poll` 又走到那个 `.await` 点 → 再 poll 一次内层 → 这次内层 Ready。

整条链路:内层的唤醒,**直接就是外层的唤醒**。`.await` 不需要做任何额外的"挂起登记"——**它只是把外层的 Waker 透传给内层,剩下的事内层和 reactor 自己解决**。这就是为什么第 2 章那条契约"`Pending` 必须留 Waker"在多层 `.await` 嵌套下依然成立:**每一层都只把自己的 Waker 往下传,最底层那个真正等 I/O 的 Future,用这个 Waker 去 reactor 注册**。

> **比喻回到餐厅**:服务员手里那张"叫号牌"(Waker),从大堂经理一路传到具体去厨房取菜的那个跑腿小弟手里。小弟跟厨房说"3 号菜好了**就举这张牌**"——厨房一叫号,光信号沿同一张牌从厨房传回大堂经理,经理再派服务员回来。**全程一张牌,不需要层层转告**。这张牌怎么造、怎么传、谁举着,是第 4 章(Waker)的全部戏。本章你只要记住:`.await` 在底层做的事,本质上就是"把我手里的 Waker 往下传一层"。
>
> **钉死这件事(承上启下)**:本章把"`.await` 在底层做了什么"拆到了 Waker 透传这一层。但 Waker 本身——它是个什么数据结构?凭什么一个值能携带"唤醒某个特定 task"的全部信息?reactor 怎么通过它把 task 叫醒?——这些,全是第 4 章的内容。本章到此为止,你只要带着一个认知离开:**`async fn` = 编译器翻的状态机 + 自引用 + Pin 焊死 + Waker 透传**,这套机器就转起来了。

---

## 章末小结

### 用"餐厅服务员"比喻回顾本章

1. **订单是一张分步清单,服务员每次回来从清单上读到哪了接着做**——这就是 `async fn` 被编译器翻成的**状态机**:每个 `.await` 是一个步骤(状态),`poll` 就是"按当前状态接着往下做"。你写顺序代码,编译器替你把状态机和字段维护全扛了。
2. **`.await` 不是"傻等",是"问一下内层,没好就我也挂起,好了就接着往下"**——`.await` 在底层就做三件事:poll 内层 Future;内层 Pending 我也 Pending(且保证内层未来会叫醒我);内层 Ready 拿结果、做状态迁移、继续。
3. **清单上"步骤 1"那栏写着"3 号取餐号看清单自己的左下角"**——这就是**自引用**:跨 `.await` 活着的借用,让状态机字段指向同一个状态机的另一字段。
4. **这张清单复印后扔原件,内部引用就废了**——这就是**自引用类型不能 move**:move 后自指针指向旧地址、变野指针,UB。
5. **餐厅规定:这种"分步清单"一旦写好,就钉在某个固定位置(比如墙上),永远在那**——这就是 `Pin`:用类型系统承诺"地址固定",把自引用的 sound 性炸弹在边界关掉,内部操作全 safe。tokio 把 task 堆分配(地址永远不变),然后用 `Pin::new_unchecked` 包成 Pin,此后整个运行时 poll task 都是 safe 的。

### 本章在全书主线中的位置

记住全书的二分法:**调度执行(让就绪的任务跑) vs 事件唤醒(让等待的任务不空耗、就绪了再叫)**。

这一章服务的是**调度执行**那一面的**地基**——具体说,是**"让用户能用普通的 async 代码,产出可让出的 Future"**这一面:

- 第 2 章我们立了"Future 必须是状态机、poll 契约两态"。但那是**手写**状态机的世界(`MaybeDone`)。真实世界里没人手写,都用 `async fn`。
- 这一章补上了另一半:**编译器把 `async fn` 自动翻成状态机,让"写异步代码"和"写同步代码"一样自然**。这是**协作式调度能成立的群众基础**——如果每个 async 任务都得手写状态机,async Rust 根本推不开,百万并发也无从谈起。
- 而 `Pin` 是这套地基的**安全护盾**:它让"自引用 Future 不能移动"这件危险的事,变成"边界 unsafe + 内部 safe"的清晰契约,使 tokio 这样的运行时能在 `Pin<&mut Task>` 上做所有操作而不踩 UB。

后面第 4 章(Waker)讲"挂起的任务凭什么被叫回来";第 5 章(Task)讲"Future 怎么被包成可调度单元"——你会看到 task 的 `Header`/`Core`/`Stage` 怎么组装、状态位怎么打包,**这一切都建立在 Pin 把 Future 焊死在堆地址之上**。

### 五个"为什么"清单

1. **为什么 `async fn` 是"语法糖"?**:你写得像同步代码,编译器把它翻成"一个状态机 struct + 一个 `impl Future` 的 `poll`",每个 `.await` 是一个状态切换点。状态机字段存的是"跨 await 活着的局部变量 + 内层 Future"。这就是 async/await 的全部魔法——把人肉维护状态机的脏活甩给编译器。
2. **`.await` 在底层做了什么?**:三件事——① poll 内层 Future;② 内层 Pending → 当前 Future 也立即 Pending(`ready!` 宏展开成 `match ... Pending => return Pending`),且保证内层未来会通过 `cx` 的 Waker 叫醒自己;③ 内层 Ready → 拿结果、做状态迁移、继续往下走。
3. **为什么 async 状态机会自引用?**:跨 `.await` 活着的借用(比如 `read_exact(&mut buf).await` 里 `&mut buf` 横跨 await 点),让内层 Future 持有的指针指向**同一个状态机的另一个字段**(buf)。Move 状态机后,这个指针指向旧地址,变野指针,UB。
4. **`Pin`/`Unpin`/`PhantomPinned` 各干什么?**:`Pin<P>` 是"承诺内部值不移动"的包装;`Unpin` 是 marker trait,表示"移动安全"(大多数类型自动 `Unpin`,Pin 对它们不起作用);`PhantomPinned` 是 opt-out 开关,塞进结构体让它变 `!Unpin`(`async fn` 生成的 Future 自动带)。三者合力:只对真正需要保护的类型(`!Unpin` 的自引用 Future)执行铁律,对普通类型不加负担。
5. **Pin 的 sound 性怎么保证?**:unsafe 关在**两个边界**——① 把值放进 Pin(`Pin::new` 对 `!Unpin` 不安全,义务:调用者保证地址固定);② 从 Pin 拿出 `&mut T`(`get_unchecked_mut` 不安全,义务:调用者保证不移动)。中间所有操作(`poll`、`as_mut`、`set`)都 safe。tokio 用"堆分配 task(地址永不变)+ `Pin::new_unchecked`"满足①,之后全链路 safe。**unsafe 关在边界,内部全 safe——Rust 把危险封进笼子的标准手法。**

### 想继续深入,该往哪钻

- **标准库源头(本章引用的法律原文)**:
  - [`std::pin::Pin`](https://doc.rust-lang.org/std/pin/struct.Pin.html) —— Pin 的契约、所有 safe/unsafe 方法的完整说明。**重点读它的 module-level 文档**(`std::pin` 模块),那里把"自引用 + Pin + Unpin"的整套动机和 sound 性证明讲得比任何教程都清楚。
  - [`std::marker::Unpin`](https://doc.rust-lang.org/std/marker/trait.Unpin.html) 和 [`std::marker::PhantomPinned`](https://doc.rust-lang.org/std/marker/struct.PhantomPinned.html) —— opt-out 机制。
  - [`std::task::ready!`](https://doc.rust-lang.org/std/task/macro.ready.html) 宏 —— `.await` 展开里②③两步的糖。
- **亲手看 rustc 把 async fn 翻成了什么**:
  - `cargo install cargo-expand`,然后 `cargo expand` 看宏展开(注意:async fn 的状态机展开在 mir 阶段,`cargo expand` 主要看 proc macro;要看 mir 用 `RUSTFLAGS="-Zunpretty=mir"` 配 nightly)。
  - [Asynchronous Programming in Rust (async book)](https://rust-lang.github.io/async-book/) —— 官方 async 书,有"async fn 展开成状态机"的经典章节,本章思路与之同源。
  - [Pin and suffering](https://fasterthanli.me/articles/pin-and-suffering) (fasterthanli.me) —— 一篇把 Pin 拆得极透的博客,配大量可运行代码,本章的"自引用最小复现"思路与之相通。
- **tokio 源码佐证(本章引用的真实代码)**:
  - [`tokio/src/runtime/task/core.rs`](../tokio/tokio/src/runtime/task/core.rs#L137-L164) —— `CoreStage`/`Core`/`Stage` 定义,task 怎么存 Future(L137-225)。
  - [`tokio/src/runtime/task/core.rs::poll`](../tokio/tokio/src/runtime/task/core.rs#L362-L384) —— 本章的黄金佐证:`Pin::new_unchecked` 怎么用、unsafe 义务怎么落在"堆分配"上。
  - [`tokio/src/runtime/task/harness.rs`](../tokio/tokio/src/runtime/task/harness.rs) —— `poll_future` 怎么从 task state 走到 `Core::poll`(L521 起),第 5 章会详拆。
- **下一站**:状态机有了,Pin 把它焊死了,可它被 `poll` 一次返回 `Pending` 后,**凭什么能被叫回来?** 第 2 章埋的钩子"`Pending` 必须留 Waker"、本章里 `cx: &mut Context` 那个被传来传去的参数——它的内核是个叫 `Waker` 的东西。翻开 **第 4 章 · Waker:谁唤醒一个挂起的任务**——我们看一个 fat pointer 怎么同时携带虚表与数据,把"叫醒某个 task"这件事做到无锁、零分配。
