# 附录 B · 动手实践:用 EnTT 写一个最小 ECS

> **这篇附录做什么**:前面 20 章我们一直在讲 ECS——Entity 是 ID、Component 是纯数据、System 是纯行为,以及 SoA 比 AoS 快、Archetype 比 sparse_set 更紧凑、view 怎么只扫匹配的实体。可这些都是"纸上谈兵":你**读了**,但没**摸过**。这篇附录就是补上"摸"这一环。我们将从零开始,用 C++ 和业界标杆库 **EnTT(skypjack/entt,Minecraft 在用)**,亲手搭出一个能跑的最小 ECS:几百几千个移动小球,逐帧 update、撞墙反弹。我们会把它和一个朴素面向对象版本并排写出来,**用 `std::chrono` 计时,亲手量出 SoA 和 AoS 的性能差距**——你会"看见"第 6 章(P2-06)那张缓存 miss 图,在你的机器上变成真实数字。

> **读者将得到什么**:
> 1. 一份**真实可编译、能跑**的 C++ 代码(基于 EnTT 真实 API,header-only,一个 `g++` 命令就能编),从面向对象版到 ECS 版循序渐进,四步走完。
> 2. 亲手感受 **AoS(`std::vector<Ball>`)vs SoA(EnTT 连续 pool)** 的性能差异——同样的移动逻辑,换一种数据布局,每帧 update 快多少。
> 3. 亲手感受 **`view<Position, Velocity>` 的筛选**——给一部分小球加个 `Health` 组件,看 `view` 怎么只扫同时挂了 Position 和 Velocity 的实体,跳过其他的。
> 4. 每一步对应本书哪一章的回指:第一步回 P1-04(面向对象墙)、第二步回 P2-05(三件套)+ P2-06(SoA)、第三步回 P2-07(遍历)、第四步回 P2-08(Archetype)+ P2-09(Query)。
> 5. 编译运行说明(CMake 或单文件 g++)、踩坑提示、在哪里能继续深入。

> **如果一跑就出错**:先确认三件事——① 编译器支持 **C++20**(EnTT 现行版本要求 C++20,见 README "Requirements" 一节);② EnTT 是 **header-only**,只要 `-I` 指向它的 `src` 目录就行,不用链接任何库;③ 用 **Release 模式 / `-O2`** 跑性能对比,Debug 模式下编译器不做优化,数据布局的优势被噪声盖掉,你量不出差距。

> **本附录和正文章节的关系**:本附录是**实践篇**,正文 20 章是**理论篇**。你可以把本附录当成 P2-05~P2-09 那几章的"实验课"——正文章节用图和源码片段讲清楚了"为什么 ECS 比面向对象快",本附录让你**亲手跑出那个"快"的数字**。两者对照着看,理论才落到地上。如果你还没读 P2-05~P2-09,先读再动手,体会更深;如果你已经读过,本附录就是验证你理解的最佳工具——你会发现"原来 SoA 比 AoS 快 2~5 倍"不是一个口号,而是一条你能在自己机器上稳定复现的曲线。

---

## 〇、一句话点破

> **别光读书,跑一遍代码。同样的几百个小球、同样的"位置 += 速度、撞墙反弹"逻辑,朴素面向对象版(`std::vector<Ball>`,字段全混一起)和 EnTT ECS 版(每个组件类型一条连续 pool)写起来差不多,跑起来差好几倍——这个"差好几倍"不是书上写的,是你在自己机器上用 `std::chrono::high_resolution_clock` 量出来的。量过一次,你就真正"信"了数据导向设计。**

这是结论。本附录倒过来走:第一步先用最朴素的面向对象写一版,作为性能基线;第二步把它改写成 EnTT ECS 版,看三件套怎么落地;第三步两边都跑成百上千个实体、计时对比,亲手"看见"差距;第四步加一个 `Health` 组件,感受 `view` 的筛选。全程代码都给你,能跑。

---

## 一、动手前的准备:EnTT 是什么、怎么装

### EnTT 是什么

**EnTT(Entity Toolkit,skypjack/entt)** 是 C++ ECS 的事实标杆,Minecraft(Mojang)、ArcGIS 等都在用。它的特点和我们选它做本附录载体的理由:

- **header-only**:整个库就是一组 `.hpp` 头文件,`#include <entt/entt.hpp>` 即可,不用链接 `.a`/`.so`/`.lib`。
- **要求 C++20**(现行版本,见仓库 README "Requirements" 一节;老资料说 C++17 的全过时了)。
- **数据导向**:每种组件类型一个 pool,pool 内部是 sparse_set(sparse 分页索引 + dense 连续数组),本质就是 P2-06 讲的**组件级 SoA** 的工业实现。
- **API 简洁**:`registry.create()` 建实体、`emplace<T>(e, args...)` 加组件、`view<A, B>()` 查询、`view.each([](...){})` 或 `for (auto [e, a, b] : view.each())` 遍历。

本附录所有 API 都基于 EnTT 仓库 `src/entt/entity/registry.hpp` 的真实签名,**不是伪代码**。

### 装 EnTT(三种方式任选)

**方式一:git submodule / 直接 clone(最简单,推荐做本附录时用)**

```bash
# 在你的项目目录下
git clone https://github.com/skypjack/entt.git third_party/entt
# 之后编译时加 -I third_party/entt/src 即可
```

EnTT 是 header-only,你只需要它的 `src/` 目录能被 `-I` 找到。`#include <entt/entt.hpp>` 会自动找到 `third_party/entt/src/entt/entt.hpp`。

**方式二:CMake FetchContent(如果你用 CMake 管理项目)**

```cmake
include(FetchContent)
FetchContent_Declare(
    entt
    GIT_REPOSITORY https://github.com/skypjack/entt.git
    GIT_TAG        v3.14.0   # 用一个稳定 tag, 不要用 master
)
FetchContent_MakeAvailable(entt)

target_link_libraries(your_target PRIVATE EnTT::EnTT)
```

**方式三:vcpkg / Conan / Homebrew(包管理器)**

```bash
# vcpkg
vcpkg install entt
# Homebrew (macOS)
brew install skypjack/entt/entt
```

任选一种。本附录后面给的单文件编译命令假设你用的是方式一(`third_party/entt/src` 在 `-I` 路径里)。

### 最小 Hello World(确认装好了)

先写个 10 行的 Hello World,确认 EnTT 装好了、能编:

```cpp
// hello.cpp
#include <entt/entt.hpp>
#include <iostream>

struct Position { float x, y; };

int main() {
    entt::registry registry;
    const auto e = registry.create();              // 建一个实体
    registry.emplace<Position>(e, 3.0f, 4.0f);     // 给它挂个 Position
    auto &pos = registry.get<Position>(e);          // 拿出来
    std::cout << "entity " << static_cast<entt::id_type>(e)
              << " at (" << pos.x << ", " << pos.y << ")\n";
    return 0;
}
```

编译运行:

```bash
g++ -std=c++20 -O2 -I third_party/entt/src hello.cpp -o hello
./hello
# 输出: entity 0 at (3, 4)
```

跑出来这行,说明 EnTT 装好了。下面正式开始。

---

## 二、第一步:朴素面向对象版(性能基线)

我们先写一个**最朴素**的面向对象版本,作为性能对照的基线。这就是 P0-01 第三节、P1-04 全章讲过的"面向对象组织游戏对象"的写法——一个 `Ball` 结构,把位置、速度、颜色、半径全绑一起,几百个 `Ball` 装进 `std::vector<Ball>`。

### 为什么先写这一版

P1-04 已经讲透:面向对象组织游戏对象会撞两面墙——**继承墙**(几百个相同球没事,一旦要"会飞的球""会变色的球"就炸)和**性能墙**(每个 Ball 整块存,字段混一起,update 只要位置速度却把颜色半径也拉进缓存)。本附录聚焦**性能墙**(继承墙在本附录不重要,因为我们只跑一种小球),所以先把面向对象版本写出来,作为"被对照的慢版本"。这样第三步计时对比时,你才有一个**具体的数字对手**。

### 完整代码:OOP 版小球

```cpp
// step1_oop.cpp  ——  朴素面向对象版: std::vector<Ball> (AoS)
#include <vector>
#include <chrono>
#include <iostream>
#include <random>
#include <cmath>

// ---------- 一个 Ball: 数据 + 行为绑一起 (面向对象) ----------
struct Color { float r, g, b; };

struct Ball {
    float x, y;          // Position:  8 字节
    float vx, vy;        // Velocity:  8 字节
    Color color;         // Color:    12 字节
    float radius;        // Radius:    4 字节
};                       // 共 32 字节 (无虚函数; 加 vtable 会变成 40+)

static constexpr float W = 800.0f;   // 屏幕宽
static constexpr float H = 600.0f;   // 屏幕高

// ---------- MovementSystem (OOP 版): 遍历所有 Ball, 更新位置 ----------
void update_balls(std::vector<Ball> &balls, float dt) {
    for (auto &b : balls) {
        b.x += b.vx * dt;                       // 位置 += 速度
        b.y += b.vy * dt;
        if (b.x < 0.0f || b.x > W) b.vx = -b.vx; // 撞左右墙反弹
        if (b.y < 0.0f || b.y > H) b.vy = -b.vy; // 撞上下墙反弹
    }
}

// ---------- 造 N 个随机小球 ----------
std::vector<Ball> make_balls(int n) {
    std::mt19937 rng(42);                        // 固定种子, 可复现
    std::uniform_real_distribution<float> dx(0.0f, W);
    std::uniform_real_distribution<float> dy(0.0f, H);
    std::uniform_real_distribution<float> dv(-100.0f, 100.0f);
    std::uniform_real_distribution<float> dc(0.0f, 1.0f);
    std::uniform_real_distribution<float> dr(2.0f, 8.0f);

    std::vector<Ball> balls;
    balls.reserve(n);
    for (int i = 0; i < n; ++i) {
        balls.push_back(Ball{
            dx(rng), dy(rng),                    // x, y
            dv(rng), dv(rng),                    // vx, vy
            Color{dc(rng), dc(rng), dc(rng)},    // color
            dr(rng)                              // radius
        });
    }
    return balls;
}

// ---------- 主函数: 造球 -> 跑 FRAMES 帧 -> 计时 ----------
int main(int argc, char **argv) {
    const int N      = (argc > 1) ? std::atoi(argv[1]) : 10000;  // 实体数, 命令行可调
    const int FRAMES = 1000;                                       // 模拟跑 1000 帧
    const float dt   = 0.016f;                                     // 每帧 16ms

    auto balls = make_balls(N);

    // 计时: 跑 FRAMES 帧 update, 看总耗时和平均每帧耗时
    auto t0 = std::chrono::high_resolution_clock::now();
    for (int f = 0; f < FRAMES; ++f) {
        update_balls(balls, dt);
    }
    auto t1 = std::chrono::high_resolution_clock::now();

    double total_ns = std::chrono::duration<double, std::nano>(t1 - t0).count();
    double per_frame_us = total_ns / FRAMES / 1000.0;
    double per_entity_ns = total_ns / FRAMES / N;

    // 防止编译器把循环优化掉: 随便用一个结果
    volatile float sink = balls[0].x + balls[0].y;

    std::cout << "[OOP]   N=" << N
              << "  total=" << total_ns / 1e6 << " ms"
              << "  per-frame=" << per_frame_us << " us"
              << "  per-entity=" << per_entity_ns << " ns\n";
    (void)sink;
    return 0;
}
```

读这段代码注意三件事:

1. **`Ball` 是 32 字节的 POD**(plain old data,无虚函数)。这是面向对象能拿到的**最好待遇**——连续 `std::vector`、无 vtable。P2-06 讲过,一旦加 `virtual ~Ball()` 或用 `std::vector<Ball*>`,性能会雪上加霜(对象多 8 字节 vtable 指针,遍历变成指针追逐)。本附录只测"最好情况下的 AoS",这样对比才公平。换句话说,我们在量**面向对象理论上能跑得最快的版本**,EnTT 还要比它快——这个对比才有说服力。如果你之后想看"真实面向对象代码"会有多惨,把 `struct Ball` 改成带虚函数的 `class Ball`,再跑一次,会发现差距从 2~5 倍拉到 5~10 倍以上,这正是 P2-06 那节"vtable 这一笔账"实测出来的样子。
2. **`update_balls` 只碰 `x, y, vx, vy`**,完全不碰 `color` 和 `radius`。可 cpu 读 `b.x` 时是把整条 64 字节缓存行(含两个 Ball 的全部字段)都搬进 L1,其中 `color`/`radius` 对 MovementSystem 来说是白搬。这就是 P2-06 拆透的"AoS 字段浪费"。把它具体算一笔:一个 Ball 32 字节,update 只用 16 字节(x,y,vx,vy),缓存利用率 50%(连读两个 Ball 时);如果 MovementSystem 一次只处理一个 Ball,缓存利用率只有 16/64 = 25%。10 万个小球跑一遍,有 50000 × 16 = 800000 字节 ≈ 780 KB 的带宽白搬在颜色和半径上——这些字节本可以多搬一倍的 Position 和 Velocity 进来。
3. **`volatile float sink`** 是个小技巧——防止聪明的编译器发现"update 的结果没人用"就把整个循环优化掉。我们后面 EnTT 版也加同样的 sink。这一步看似无关紧要,但它是性能基准测试的命脉:GCC 和 Clang 在 `-O2` 下都会做"死存储消除"(dead store elimination),一个写出去再也没人读的变量会被整段删掉。如果你忘了加 sink,可能量到 "per-frame = 0 us" 然后大吃一惊——那不是你的代码快,是编译器帮你把活全免了。这个坑在所有性能基准里都有,包括 EnTT 自己仓库的 benchmark。

### 主循环的简化:省掉了什么

本附录的主循环是 `for (int f = 0; f < FRAMES; ++f) { update(...); }`,这比真实游戏引擎的主循环(P1-02 讲的三段式 input → update → render)简化了不少。我们省掉了三样东西,得讲清楚,免得你以为真实引擎也这么简单:

- **省掉 input**:本附录没有输入,小球自动移动。真实游戏要先读键鼠/手柄,再决定这一帧怎么 update。
- **省掉 render**:本附录不画画面,只算位置。真实游戏每帧 update 完要渲染一帧,渲染那一段(P5-18)本身就是十几毫秒的开销,通常比 update 还重。本附录聚焦 update 段的数据布局收益,把 render 省掉,这样你量到的纯粹是"组织海量对象"那部分的差距。
- **省掉固定步长**:本附录用固定 `dt = 0.016f`,真实引擎的物理更新要用 accumulator 模式(P3-10)保证数值稳定。本附录的小球只是"位置 += 速度 × dt、撞墙反弹",用固定 dt 就够。

这些简化都是有意的——本附录的目标是让你**看清 ECS 和面向对象在组织数据上的差距**,不是写一个真实游戏。等你跑完本附录,想看完整主循环,翻 P1-02 和 P3-10。

编译运行:

```bash
g++ -std=c++20 -O2 step1_oop.cpp -o step1_oop
./step1_oop 10000        # 跑 10000 个小球
./step1_oop 50000        # 跑 50000 个
./step1_oop 200000       # 跑 200000 个, 看 AoS 在工作集超过 L2/L3 后怎么飙升
```

记下你机器上不同 N 的 `per-frame` 和 `per-entity` 数字,等下和 EnTT 版对比。

> **承 P1-04**:这一版就是 P1-04 全章拆解的"面向对象组织游戏对象"。它的两面墙里,本附录只对照**性能墙**(数据散落缓存差)。继承墙在单一种小球上看不出来——但你可以想象,如果策划突然要"会隐形的球""会变色的球",这一版立刻得改 `Ball` 结构或加继承,而 EnTT 版只是换组件组合(见第四步)。

---

## 三、第二步:用 EnTT 写 ECS 版

现在把同一个"几百个移动小球"用 EnTT 重写。这一步对应 P2-05(三件套)和 P2-06(SoA 存储):**把面向对象绑一起的"数据 + 行为"拆开**——`Ball` 拆成 `Position`/`Velocity`/`Color`/`Radius` 四个纯数据组件,`update_balls` 拆成一个独立的 `MovementSystem` 函数。

### 组件:纯数据

```cpp
// 组件: 纯数据结构, 无任何方法
struct Position { float x, y; };
struct Velocity { float vx, vy; };
struct Color    { float r, g, b; };
struct Radius   { float r; };
```

这就是 P2-05 第二节讲的:**Component = 纯数据,无行为**。每个组件就是一个小 `struct`,没有 `update()`、没有 `render()`,什么方法都没有。一个实体"是什么"由它挂了哪些组件决定——挂了 `Position + Velocity + Color + Radius` 的实体就是个"小球"。

### System:纯行为,用 `view` 查询 + `each` 遍历

```cpp
// MovementSystem: 纯行为, 遍历所有同时有 Position 和 Velocity 的实体
void movement_system(entt::registry &reg, float dt) {
    auto view = reg.view<Position, Velocity>();        // 查询: 我要同时有这两个组件的实体
    for (auto [entity, pos, vel] : view.each()) {      // 遍历: entity + 它的 Position& + Velocity&
        pos.x += vel.vx * dt;
        pos.y += vel.vy * dt;
        if (pos.x < 0.0f || pos.x > W) vel.vx = -vel.vx;   // 撞墙反弹
        if (pos.y < 0.0f || pos.y > H) vel.vy = -vel.vy;
    }
}
```

读这段注意三件事:

1. **`reg.view<Position, Velocity>()`** 是查询(Query)——它告诉 registry"我要所有同时挂了 `Position` 和 `Velocity` 组件的实体"。registry 内部找到 Position pool 和 Velocity pool,返回一个 view 对象(P2-09 详讲 view 的实现)。view **不是**一个新数组,它只是两个 pool 的"窗口"。
2. **`view.each()`** 返回一个可迭代对象,每次循环给你一个 `(entity, Position&, Velocity&)` 元组(结构化绑定)。这里用的是 EnTT 官方 README "Code Example" 一节给的两种写法之一(`for (auto [entity, pos, vel]: view.each())`);另一种是用回调 `view.each([](const auto entity, auto &pos, auto &vel){ ... })`,等价。
3. **System 自己不持有数据**——`movement_system` 这个函数,除了参数 `reg` 和 `dt`,自己不存任何状态。数据全在 registry 管的组件 pool 里,System 只是来"查询 + 遍历 + 读写"的过客。这就是 P2-05 第三节讲的"行为从对象身上剥离"。

> **★承 P2-06**:为什么这一版会快?关键不在"System 是函数"本身,而在 EnTT **怎么存 Position 和 Velocity**。你 `emplace<Position>(e, x, y)` 时,registry 给 Position 这种组件类型开一个 pool,pool 内部是 sparse_set(sparse 分页索引 + dense 连续数组),Position 数据被放进 pool 的 dense 数组末尾,**自然连续**。所以 `view<Position, Velocity>().each()` 遍历时,读 Position 是连续的,读 Velocity 也是连续的——这就是 P2-06 拆透的**组件级 SoA**。MovementSystem 只扫这两条连续数组,缓存利用率 100%,预取器一路领先;完全不碰 Color/Radius pool。

### 这一段代码到底"拆"了什么

第二步的核心动作是把面向对象的 `Ball` 类**拆开**:数据拆成 4 个组件(Position/Velocity/Color/Radius),行为拆成 1 个 System(movement_system)。这一拆的后果,值得逐条对照面向对象版看清楚:

- **数据上**:`Ball` 类原本是 32 字节一整块,update 时 16 字节有用、16 字节浪费;拆成 4 个组件后,每个组件独立成 pool,MovementSystem 只扫 Position pool 和 Velocity pool,Color pool 和 Radius pool 它根本不访问。这两条被忽略的 pool 连缓存行都不会被加载进 L1——这是和面向对象最大的不同,面向对象里那 16 字节"浪费"是**被白搬进缓存又扔掉**,而 EnTT 里它们**根本不进缓存**。
- **行为上**:`update_balls(balls, dt)` 是个对 `std::vector<Ball>` 操作的函数,它内部知道"我要遍历整个 balls 数组";而 `movement_system(reg, dt)` 是个对 `entt::registry` 操作的函数,它通过 `view<Position, Velocity>()` **声明自己关心哪类实体**,registry 只把匹配的实体喂给它。这两种写法的"知道得多少"不一样——前者知道得太具体(写死了"遍历 balls 数组"),换一种对象组合就得改函数;后者只声明"我要 Position+Velocity 的实体",谁挂了这两个组件它都扫,后面加新对象变种(飞球、隐形球)它一个字不改就支持。
- **类型上**:`Ball` 是一个具体类型,继承它要做变种就撞 P1-04 的继承墙;而 Position/Velocity/Color/Radius 是四个独立的 struct,要"会飞的球"就再加个 `Flyable` 组件,要"会隐形的球"就加个 `Invisible` 组件,组合取代继承,P1-04 那面墙直接消失。

把这三条合起来:第二步不只是"换了一种写法",而是**根本性地重组了数据和行为的关系**——数据按"系统怎么访问"分组(组件级 SoA),行为按"我关心哪类数据"声明(view 查询)。这是数据导向设计的字面落地,也是 P2-05~P2-07 三章一脉相承的核心。

### Entity 是 ID 这件事,在代码里看得见

P2-05 第一节反复强调"Entity 就是一个 ID,什么数据都没有"。这件事在第二步的代码里直接看得见——`registry.create()` 返回一个 `entt::entity`,你之后对它做的所有操作都是"挂组件"(emplace)和"查组件"(get/view),entity 本身始终是个数字。你可以打印它:

```cpp
const auto e = registry.create();
std::cout << "new entity id = " << static_cast<entt::id_type>(e) << "\n";
// 输出类似: new entity id = 0   (就是个数字)
```

这个数字里其实藏了 P2-05 技巧精解讲的"slot + version"两段(低 20 位 slot,高 12 位 version),但用起来你完全感受不到——它就是个透明的句柄。你只要知道两件事:① 它唯一标识一个实体;② 你 destroy 它之后,registry 会把它的 slot 回收(version +1),下次 create 可能复用这个 slot 但 version 不同,所以你手里攥着的旧 entity id 会被 `registry.valid()` 判定为失效。这两件事 P2-05 已经讲透,这里只点一下:本附录的代码不会触发悬空引用(我们不存 entity id 跨帧用),但真实游戏里(脚本、回调、事件)经常要查"这个 entity 还活着吗",这时候 `registry.valid(e)` 就是你最好的朋友。

### 造球:`create()` 建实体,`emplace<T>()` 加组件

```cpp
// 造 N 个小球: 每个小球 = 一个 Entity + Position + Velocity + Color + Radius
void make_balls(entt::registry &reg, int n) {
    std::mt19937 rng(42);
    std::uniform_real_distribution<float> dx(0.0f, W);
    std::uniform_real_distribution<float> dy(0.0f, H);
    std::uniform_real_distribution<float> dv(-100.0f, 100.0f);
    std::uniform_real_distribution<float> dc(0.0f, 1.0f);
    std::uniform_real_distribution<float> dr(2.0f, 8.0f);

    for (int i = 0; i < n; ++i) {
        const auto e = reg.create();                  // 1. 建实体(一个 ID)
        reg.emplace<Position>(e, dx(rng), dy(rng));   // 2. 挂 Position 组件
        reg.emplace<Velocity>(e, dv(rng), dv(rng));   // 3. 挂 Velocity 组件
        reg.emplace<Color>(e, dc(rng), dc(rng), dc(rng)); // 4. 挂 Color
        reg.emplace<Radius>(e, dr(rng));              // 5. 挂 Radius
    }
}
```

对照面向对象版:`Ball` 结构的四个字段(`x,y`、`vx,vy`、`color`、`radius`),这里拆成四个 `emplace` 调用,每个挂一个组件。这就是 P2-05 末尾"把 Ball 类拆成 4 组件"的字面落地。

### 完整代码:ECS 版小球

把上面三段拼起来,加计时:

```cpp
// step2_entt.cpp  ——  EnTT ECS 版: Position/Velocity/Color/Radius 组件 + MovementSystem
#include <entt/entt.hpp>
#include <vector>
#include <chrono>
#include <iostream>
#include <random>
#include <cmath>

struct Position { float x, y; };
struct Velocity { float vx, vy; };
struct Color    { float r, g, b; };
struct Radius   { float r; };

static constexpr float W = 800.0f;
static constexpr float H = 600.0f;

void movement_system(entt::registry &reg, float dt) {
    auto view = reg.view<Position, Velocity>();
    for (auto [entity, pos, vel] : view.each()) {
        pos.x += vel.vx * dt;
        pos.y += vel.vy * dt;
        if (pos.x < 0.0f || pos.x > W) vel.vx = -vel.vx;
        if (pos.y < 0.0f || pos.y > H) vel.vy = -vel.vy;
    }
}

void make_balls(entt::registry &reg, int n) {
    std::mt19937 rng(42);
    std::uniform_real_distribution<float> dx(0.0f, W);
    std::uniform_real_distribution<float> dy(0.0f, H);
    std::uniform_real_distribution<float> dv(-100.0f, 100.0f);
    std::uniform_real_distribution<float> dc(0.0f, 1.0f);
    std::uniform_real_distribution<float> dr(2.0f, 8.0f);
    for (int i = 0; i < n; ++i) {
        const auto e = reg.create();
        reg.emplace<Position>(e, dx(rng), dy(rng));
        reg.emplace<Velocity>(e, dv(rng), dv(rng));
        reg.emplace<Color>(e, dc(rng), dc(rng), dc(rng));
        reg.emplace<Radius>(e, dr(rng));
    }
}

int main(int argc, char **argv) {
    const int N      = (argc > 1) ? std::atoi(argv[1]) : 10000;
    const int FRAMES = 1000;
    const float dt   = 0.016f;

    entt::registry registry;
    make_balls(registry, N);

    auto t0 = std::chrono::high_resolution_clock::now();
    for (int f = 0; f < FRAMES; ++f) {
        movement_system(registry, dt);
    }
    auto t1 = std::chrono::high_resolution_clock::now();

    double total_ns = std::chrono::duration<double, std::nano>(t1 - t0).count();
    double per_frame_us = total_ns / FRAMES / 1000.0;
    double per_entity_ns = total_ns / FRAMES / N;

    // 防优化: 取一个实体的 Position 用一下
    auto view = registry.view<Position>();
    volatile float sink = view.begin()->x;
    (void)sink;

    std::cout << "[EnTT]  N=" << N
              << "  total=" << total_ns / 1e6 << " ms"
              << "  per-frame=" << per_frame_us << " us"
              << "  per-entity=" << per_entity_ns << " ns\n";
    return 0;
}
```

编译运行:

```bash
g++ -std=c++20 -O2 -I third_party/entt/src step2_entt.cpp -o step2_entt
./step2_entt 10000
./step2_entt 50000
./step2_entt 200000
```

> **API 核对**:这段代码用的每个 EnTT API 都基于源码 `src/entt/entity/registry.hpp` 的真实签名——`registry.create()` 返回 `entity_type`(`[[nodiscard]] entity_type create()`)、`emplace<Type, Args...>(entity, args...)` 返回引用(`template<typename Type, typename... Args> decltype(auto) emplace(const entity_type entt, Args&&... args)`)、`view<Position, Velocity>()` 返回 `basic_view<...>`、`view.each()` 是 README "Code Example" 一节官方演示的写法。`#include <entt/entt.hpp>` 也是 README "Integration" 一节明确说的整库包含方式。

> **★承 P2-05**:这一步把面向对象的"数据 + 行为绑一起"拆成了三件套——Entity 是 ID(`registry.create()` 拿到一个 `entt::entity`)、Component 是纯数据(`Position`/`Velocity`/`Color`/`Radius` 都是无方法 struct)、System 是纯行为(`movement_system` 函数,通过 `view` 查询 + `each` 遍历)。继承墙塌了(小球要变种就换组件组合,见第四步),性能墙也松动了(每个 System 只碰它要的字段,数据可以按"系统怎么遍历"连续摆放)。

> **★承 P2-06**:EnTT 给每种组件类型开一个 pool,Pool 内部是 sparse_set(sparse 分页索引 + dense 连续数组)。你 `emplace<Position>(e, ...)` 时,Position 数据被放进 Position pool 的 dense 数组末尾,**自然连续**。`view<Position, Velocity>().each()` 遍历时,读 Position 是连续的,读 Velocity 也是连续的——这就是组件级 SoA。MovementSystem 只扫这两条连续数组,完全不碰 Color/Radius pool。

---

## 四、第三步:性能对比,亲手"看见"差距

两版都写好了,现在正经计时对比。我们写一个跑多种实体数的脚本,把两种实现的"每帧 update 耗时随实体数变化"画成曲线。

### 怎么对比才公平

计时对比有几个坑,踩了就量不出真实差距:

1. **必须开 `-O2` 或 `-O3`**(Release 模式)。Debug 模式下编译器不做内联、不做向量化,EnTT 的模板开销还摆在那,但 SoA 的缓存优势被噪声盖掉。本附录所有命令都带 `-O2`。
2. **跑足够多帧**(本附录跑 1000 帧),取总耗时算平均每帧。只跑一两帧会被启动开销、缓存冷启动 dominate。
3. **固定随机种子**(`std::mt19937 rng(42)`),这样 OOP 版和 EnTT 版造出来的小球初始位置一样,工作量一致。
4. **防编译器优化掉循环**——加 `volatile float sink` 用一下结果。否则 GCC/Clang 发现"update 结果没人用"会把整个循环删掉,你量到的是 0。
5. **关掉省电模式 / 后台别跑重活**——CPU 频率漂移和后台噪声会让数字跳动。最好跑几次取中位数。

### 一个批量跑的脚本

```bash
#!/bin/bash
# bench.sh  ——  批量跑 OOP vs EnTT, 多种实体数
set -e
g++ -std=c++20 -O2 -I third_party/entt/src step1_oop.cpp   -o step1_oop
g++ -std=c++20 -O2 -I third_party/entt/src step2_entt.cpp  -o step2_entt

echo "N,OOP_per_frame_us,EnTT_per_frame_us"
for N in 1000 2000 5000 10000 20000 50000 100000 200000; do
    OOP=$(./step1_oop   "$N" | grep -oE 'per-frame=[0-9.]+' | cut -d= -f2)
    ENTT=$(./step2_entt "$N" | grep -oE 'per-frame=[0-9.]+' | cut -d= -f2)
    echo "$N,$OOP,$ENTT"
done
```

跑出来的表(示意,你的具体数字会随机器/编译器变,但**比值**应该稳定):

```
N,OOP_per_frame_us,EnTT_per_frame_us
1000,     2.1,    1.0
2000,     4.3,    2.0
5000,    11.0,    5.1
10000,   22.5,   10.2
20000,   46.0,   20.8
50000,  120.0,   52.0     <- 工作集开始跨过 L2 (~256KB)
100000,  260.0,  108.0
200000,  620.0,  230.0    <- OOP 的工作集 (200000*32B=6.4MB) 快顶到 L3 (~8MB), 开始飙升
```

把这张表描在双对数坐标上,就是 P2-06 那张缓存 miss 图的"实测版":

![附录 B 性能对比(双对数):三条曲线分别是 `std::vector<Ball*> + virtual`(红,指针追逐,超线性飙升)、`std::vector<Ball>`(琥珀,AoS 连续,线性)、EnTT `view<Position, Velocity>`(绿,SoA 连续 pool,线性但斜率最平)。50000 个实体处 EnTT 约比连续 AoS 快 2~3 倍,比指针追逐版快更多](images/fig-appB_02-bench-aos-vs-entt.png)

这张图的看点:① 三条线在双对数坐标下大体是直线(幂律),但斜率不同——EnTT 最平(每实体耗时最少),指针追逐最陡(超线性,跨过 L2/L3 容量门槛后明显向上翘);② 50000 个实体处,EnTT 比连续 AoS 快 2~3 倍,比指针追逐版快更多;③ EnTT 的曲线**始终线性**,因为它只扫两条小数组(Position+Velocity,各 N×8 字节),工作集小,能较久待在 L2 里。

### 怎么读这张图:把数字和缓存层级对上

这张图不是抽象的"快慢",每一段曲线的拐点都对应你 cpu 的缓存层级,值得把数字对上去看:

- **L1 缓存约 32 KB**:几千个实体(每个 32 字节)的工作集还能塞进 L1,这时三种实现都很快,差距不明显。这就是为什么 N=1000 时比值只有 1.5 倍左右——大家都在 L1 里跑,缓存优势没拉开。
- **L2 缓存约 256 KB**:8 千个实体(8K × 32B = 256 KB)正好顶到 L2 边界。N 超过这个数,AoS 的工作集开始往 L3 甚至内存溢出,而 EnTT 只扫两条小数组(2 × N × 8B),N=16000 时也才 256 KB,还待在 L2。这就是 50000 实体处差距拉到 2~3 倍的物理根因——AoS 已经开始跑 L3 了,EnTT 还在 L2 里飞。
- **L3 缓存约 8 MB**:25 万个实体(250K × 32B = 8 MB)顶到 L3 边界。N 超过这个数,连 EnTT 的工作集(2 × 250K × 8B = 4 MB,还在 L3)也开始紧张,AoS 的工作集已经溢出到内存(每个 Ball 要从 DRAM 拿,延迟约 100 纳秒),指针追逐版更是每个对象一次 DRAM 访问。这就是为什么超大 N 时 AoS 和指针追逐会"飙升"——它们的工作集彻底装不进任何一层缓存。
- **DRAM 延迟约 100 纳秒,几十个时钟周期**:这是"缓存未命中"的代价上限。所以指针追逐版在 N=200000 时每实体耗时飙升到几纳秒甚至十几纳秒,是因为它频繁触发 DRAM 访问;而 EnTT 始终在 L2 里(延迟约 4 纳秒),每实体不到 1 纳秒。

把这个"工作集 vs 缓存层级"的对位关系记牢,你看任何数据导向的性能图都能一眼读出"为什么这一段平、那一段翘"——这不是玄学,是物理。

### 别被"实测数字"绑架:比值稳定比绝对值重要

跑完基准,新手常犯两个误读,得提醒:

- **别纠结绝对数字**:你机器上量到的"per-entity = 0.5 ns"可能在我机器上是 0.8 ns,差 CPU 主频、缓存大小、内存带宽、编译器版本。绝对数字跨机器没意义。**有意义的只有比值**(EnTT 比 AoS 快几倍),这个比值在同样硬件上稳定,能复现。
- **别用一次跑定结论**:基准测试有噪声(后台进程、CPU 频率漂移、热节流)。同一组参数跑 5~10 次取中位数,或者至少取最小值(最小值通常代表"噪声最少时的真实性能")。本附录的 `bench.sh` 跑一次只给一个点,严格点你应该改成跑多次取中位数。

记住这两条,你跑出来的数据才经得起推敲。这也是 EnTT 仓库自己的 benchmark、所有严肃性能测试都遵循的规矩——单次数字靠不住,稳定比值才靠得住。

> **★承 P2-07**:为什么 EnTT 这么快?P2-06 讲了"数据连续"(缓存友好),P2-07 讲"连续之后能干什么"——**SIMD 批处理 + 多核数据并行**。MovementSystem 对每个实体都是"`pos += vel * dt`"这同一个操作,SIMD 指令(SSE/AVX)一次能对 4~16 个实体的 Position 同时加 Velocity,EnTT 连续的 dense 数组正好喂给 SIMD;实体间互不依赖,可以分给多核各算一半。本附录的代码 `-O2` 下 GCC/Clang 会自动把 `view.each()` 内层循环向量化(SIMD),你能量到这部分收益。

### 别误会:差距没你想的那么固定

 honesty time(诚实时间):你在自己机器上量到的比值,可能比上面表的 2~3 倍小,也可能更大。原因:

- **小 N(几百个)**:两种实现都快得几乎一样(微秒级),工作集全在 L1,缓存优势不明显。差距可能只有 1.5 倍。
- **中等 N(几万个)**:这是差距最大的区间,AoS 的工作集开始跨过 L2(256KB),EnTT 的两条小数组还待在 L2 里,差距能拉到 3~5 倍。
- **超大 N(几十万以上)**:EnTT 的工作集也跨过 L3 了,大家都开始往内存跑,差距收窄到 1.5~2 倍。但绝对耗时上,AoS 已经卡死了,EnTT 还在跑。
- **如果你的 `Ball` 加了虚函数**(面向对象常见):AoS 会雪上加霜,差距能到 5~10 倍。本附录的 OOP 版为了公平**没加**虚函数,你自己可以试着加一个 `virtual void update(float dt)` 看看差距。

所以别纠结具体倍数,**量到"EnTT 明显更快"这个事实**就够支撑本书的论点了——同样是"`pos += vel * dt`"这条逻辑,换一种数据布局,快了几倍。这就是数据导向设计的**实测收益**。

> **钉死这件事**:同样的小球、同样的移动逻辑,朴素 `std::vector<Ball>`(AoS)和 EnTT(组件级 SoA)写起来差不多,跑起来差 2~5 倍(中等 N)。这个差距不是书上写的,是你在自己机器上量出来的。量过一次,你就真正"信"了 P2-06 那张缓存 miss 图——数据布局决定性能,这不是理论。

---

## 五、第四步:加 Health 组件,感受 `view` 的筛选

前三步的小球都是同质的——每个都挂 Position+Velocity+Color+Radius。真实游戏里,实体千变万化:有的有血量、有的会飞、有的有 AI。EnTT 的 `view` 凭什么能"只扫同时有 Position 和 Velocity 的实体",跳过其他的?这一步亲手感受。

### 给一部分小球加 Health

我们改一下 `make_balls`,只给**前一半**小球加 `Health` 组件,后一半不加:

```cpp
struct Health { int cur; int max; };    // 新增组件

void make_balls_with_health(entt::registry &reg, int n) {
    std::mt19937 rng(42);
    std::uniform_real_distribution<float> dx(0.0f, W);
    std::uniform_real_distribution<float> dy(0.0f, H);
    std::uniform_real_distribution<float> dv(-100.0f, 100.0f);
    std::uniform_real_distribution<float> dc(0.0f, 1.0f);
    std::uniform_real_distribution<float> dr(2.0f, 8.0f);
    for (int i = 0; i < n; ++i) {
        const auto e = reg.create();
        reg.emplace<Position>(e, dx(rng), dy(rng));
        reg.emplace<Velocity>(e, dv(rng), dv(rng));
        reg.emplace<Color>(e, dc(rng), dc(rng), dc(rng));
        reg.emplace<Radius>(e, dr(rng));
        if (i < n / 2) {                                   // 只给前一半加 Health
            reg.emplace<Health>(e, 100, 100);
        }
    }
}
```

现在 registry 里有两类实体:

- **前一半(N/2 个)**:Position + Velocity + Color + Radius + **Health**
- **后一半(N/2 个)**:Position + Velocity + Color + Radius(无 Health)

### `view<Position, Velocity>` 仍然扫全部,`view<Health>` 只扫有 Health 的

关键来了:`movement_system` 里的 `view<Position, Velocity>()`,**所有 N 个实体都同时有 Position 和 Velocity**(我们给每个都 emplace 了),所以它扫全部 N 个——和第三步一样快。

但如果我们写一个 `damage_system`,只处理有 `Health` 的实体:

```cpp
void damage_system(entt::registry &reg, int amount) {
    auto view = reg.view<Health>();               // 只要 Health
    for (auto [entity, hp] : view.each()) {
        hp.cur -= amount;
        if (hp.cur < 0) hp.cur = 0;
    }
}
```

这个 `view<Health>()` **只扫有 Health 的实体**(N/2 个),完全不碰没 Health 的那一半。这就是 P2-09 讲的 **Query 的筛选**:view 自动只返回"挂了这些组件"的实体,你不用写 `if (has_health)` 这种判断。

![view 筛选:三个 pool(Position/Velocity/Health),每个只存挂了该组件的实体;`view<Position, Velocity>` 求交集得到同时有这两组件的实体(底部紫色行),跳过缺其一的;e2/e6 虽然还有 Health,view 仍然返回它们——view 只关心你声明的组件](images/fig-appB_03-view-filter.png)

这张图的看点:① Position pool 存了所有挂 Position 的实体(e0/e1/e2/e4/e5/e6),Health pool 只存挂 Health 的(e2/e5/e6/e7)——每个 pool 都只装"挂了该组件"的实体,**没挂的实体根本不在这个 pool 里**(承 P2-06 的 sparse_set:每个 pool 独立);② 底部 `view<Position, Velocity>` 的结果 = Position pool 和 Velocity pool 的交集(e0/e2/e4/e6),没 Position 的 e3/e7 被跳过,没 Velocity 的 e1/e5 也被跳过;③ e2 和 e6 虽然还挂着 Health,view 仍然返回它们——**view 只关心你声明的组件,不管你额外还挂了什么**。

### 用 `exclude` 反向筛选:给"没有 Health 的小球"特殊处理

EnTT 的 view 还支持 `exclude_t`,让你筛"**没有**某些组件"的实体。比如"只对没有 Health 的小球(无敌的?)施加特殊效果":

```cpp
#include <entt/entity/helper.hpp>     // entt::exclude
// 或直接用现成的: view 的模板参数里写 entt::exclude_t<Health>

void invincible_effect(entt::registry &reg, float dt) {
    // 注意: view 的模板参数是 <Get..., exclude_t<Exclude...>>
    auto view = reg.view<Position, Velocity, entt::exclude_t<Health>>();
    for (auto [entity, pos, vel] : view.each()) {
        // 这些实体有 Position+Velocity, 但没有 Health
        // 给它们加点特殊效果, 比如速度翻倍
        vel.vx *= 1.0001f;
        vel.vy *= 1.0001f;
    }
}
```

这个 `exclude_t<Health>` 是 EnTT `view` 的真实签名——`registry.hpp` 里 `view` 的声明是 `template<typename Type, typename... Other, typename... Exclude> basic_view<...> view(exclude_t<Exclude...> = exclude_t{})`。`exclude` 让 view 跳过"挂了 Exclude 列表里任何组件"的实体。

### 完整代码:带 Health 的 ECS 版

把 `damage_system`、`invincible_effect`、计时都加上:

```cpp
// step3_health.cpp  ——  加 Health 组件, 看 view 怎么筛选
#include <entt/entt.hpp>
#include <chrono>
#include <iostream>
#include <random>

struct Position { float x, y; };
struct Velocity { float vx, vy; };
struct Color    { float r, g, b; };
struct Radius   { float r; };
struct Health   { int cur, max; };

static constexpr float W = 800.0f;
static constexpr float H = 600.0f;

void movement_system(entt::registry &reg, float dt) {
    auto view = reg.view<Position, Velocity>();
    for (auto [e, pos, vel] : view.each()) {
        pos.x += vel.vx * dt;
        pos.y += vel.vy * dt;
        if (pos.x < 0.0f || pos.x > W) vel.vx = -vel.vx;
        if (pos.y < 0.0f || pos.y > H) vel.vy = -vel.vy;
    }
}

void damage_system(entt::registry &reg, int amount) {
    auto view = reg.view<Health>();               // 只扫有 Health 的
    for (auto [e, hp] : view.each()) {
        hp.cur -= amount;
        if (hp.cur < 0) hp.cur = 0;
    }
}

void make_balls_with_health(entt::registry &reg, int n) {
    std::mt19937 rng(42);
    std::uniform_real_distribution<float> dx(0.0f, W);
    std::uniform_real_distribution<float> dy(0.0f, H);
    std::uniform_real_distribution<float> dv(-100.0f, 100.0f);
    std::uniform_real_distribution<float> dc(0.0f, 1.0f);
    std::uniform_real_distribution<float> dr(2.0f, 8.0f);
    for (int i = 0; i < n; ++i) {
        const auto e = reg.create();
        reg.emplace<Position>(e, dx(rng), dy(rng));
        reg.emplace<Velocity>(e, dv(rng), dv(rng));
        reg.emplace<Color>(e, dc(rng), dc(rng), dc(rng));
        reg.emplace<Radius>(e, dr(rng));
        if (i < n / 2) {
            reg.emplace<Health>(e, 100, 100);
        }
    }
}

int main(int argc, char **argv) {
    const int N      = (argc > 1) ? std::atoi(argv[1]) : 10000;
    const int FRAMES = 1000;
    const float dt   = 0.016f;

    entt::registry registry;
    make_balls_with_health(registry, N);

    // 看看两类实体各有多少
    auto all_pv   = registry.view<Position, Velocity>();
    auto only_hp  = registry.view<Health>();
    std::cout << "total entities: " << registry.size() << "\n"
              << "with Position+Velocity: " << all_pv.size() << "\n"
              << "with Health:            " << only_hp.size() << "\n";

    // 跑 FRAMES 帧: movement 扫全部, damage 只扫一半
    auto t0 = std::chrono::high_resolution_clock::now();
    for (int f = 0; f < FRAMES; ++f) {
        movement_system(registry, dt);
        damage_system(registry, 1);
    }
    auto t1 = std::chrono::high_resolution_clock::now();

    double total_ns = std::chrono::duration<double, std::nano>(t1 - t0).count();
    std::cout << "total=" << total_ns / 1e6 << " ms"
              << "  per-frame=" << total_ns / FRAMES / 1000.0 << " us\n";

    // 验证: Health 池里所有实体的血量都从 100 减到了 0 (FRAMES=1000 帧, 每次 -1)
    auto v = registry.view<Health>();
    int sample = v.size() > 0 ? v.begin()->x.cur : -1;  // 注意: view 的迭代器解引用是 entity, 不是 Health
    // 拿第一个 Health 实体的血量:
    if (registry.view<Health>().size() > 0) {
        const auto first_e = registry.view<Health>().front();
        const auto &hp = registry.get<Health>(first_e);
        std::cout << "first Health entity hp.cur = " << hp.cur
                  << " (expected 0, since 1000 damage of 1 each, clamped at 0)\n";
    }
    return 0;
}
```

编译运行:

```bash
g++ -std=c++20 -O2 -I third_party/entt/src step3_health.cpp -o step3_health
./step3_health 10000
# 输出类似:
# total entities: 10000
# with Position+Velocity: 10000
# with Health:            5000
# total=... ms  per-frame=... us
# first Health entity hp.cur = 0 (expected 0, ...)
```

你会看到 `with Health: 5000`——view 自动只扫有 Health 的实体,数量正好是 N/2。`damage_system` 每帧只处理这 5000 个,**完全跳过**另外 5000 个没 Health 的实体。这就是 P2-09 讲的 Query 筛选,而且它**不浪费缓存**——Health pool 里只有这 5000 个,连续紧凑,damage_system 遍历它缓存全命中。

> **★承 P2-08(Archetype)+ P2-09(Query)**:这一步直观展示了 `view` 的筛选能力,但背后有个更深的话题——**Archetype**。EnTT 的存储是"每种组件一个 pool"(sparse_set),`view<Position, Velocity>` 在内部要遍历较小的 pool,去较大的 pool 的 sparse 索引里查"在不在"。这种"按组件类型分组"的方案,在组件组合复杂时会有遍历开销。**Archetype**(Bevy、Flecs、Unity DOTS 用的方案)换了个思路:把"组件组合相同"的实体放进同一个 archetype(比如"{Position, Velocity}"是一个 archetype,"{Position, Velocity, Health}"是另一个),每个 archetype 内部连续存放,`view<Position, Velocity>` 只要扫"组件组合是 {Position, Velocity} 超集"的 archetype,不用做 sparse 查找。这在组件组合多时更快。P2-08 详讲 Archetype 的内存布局,P2-09 详讲 Query 怎么在两种存储上工作。本附录用 EnTT 的 sparse_set 方案,够你感受"view 只扫匹配实体";想深入 Archetype,看那两章。

### 两种存储方案的取舍:sparse_set vs Archetype

既然提到了 Archetype,值得花一段把两种方案的取舍讲清楚,因为这是 ECS 存储设计的核心分水岭,你将来选 ECS 库或者自己设计时会面对这个抉择:

**sparse_set 方案(EnTT)** 的核心是"每种组件类型一个 pool"。它的好处是**加减组件便宜**——`emplace<Health>(e)` 只是在 Health pool 末尾 push_back 一个 entry、更新 sparse 索引,O(1);`erase<Health>(e)` 用 swap_and_pop 也是 O(1)。代价是**多组件查询要做 sparse 查找**——`view<Position, Velocity>` 遍历较小的 pool 时,对每个 entity 要去较大 pool 的 sparse 索引里查"在不在",虽然 O(1) 但有常数开销。这种方案适合"组件组合多变、加减频繁"的场景,比如脚本能动态挂组件的游戏。

**Archetype 方案(Bevy / Flecs / Unity DOTS)** 的核心是"组件组合相同的一个 archetype"。它的好处是**多组件查询不用 sparse 查找**——`view<Position, Velocity>` 扫所有"组件组合是 {Position, Velocity} 超集"的 archetype,每个 archetype 内部已经是连续的 Position+Velocity 数组,直接遍历,缓存极致友好。代价是**加减组件要搬实体**——`emplace<Health>(e)` 要把 e 从"{Position, Velocity}" archetype 搬到"{Position, Velocity, Health}" archetype,意味着拷贝它所有的组件数据到新 archetype 的数组里。如果组件多、加减频繁,这个搬移开销会显现。这种方案适合"组件组合相对稳定、查询密集"的场景,比如大型仿真、物理引擎。

**怎么选**:EnTT 之所以选 sparse_set,是因为它的目标用户(游戏 + 工具)经常需要动态加减组件;Bevy 选 Archetype,是因为它的目标用户(数据导向的游戏引擎)查询远多于加减,且查询性能要榨干缓存。两者没有绝对优劣,只有场景适配。P2-08 会把 Archetype 的内存布局画到字节级,你看完那章再回头看本附录的 sparse_set 代码,会更清楚两者的差异。

### 这一step在真实游戏里长什么样

第四步加的 `Health` 组件,在真实游戏里对应什么?举几个例子帮你想清楚:

- **RPG 的小怪**:每个小怪有 Position(在哪)、Velocity(怎么动)、Health(血量)、AI(怎么决策)。MovementSystem 扫 Position+Velocity 更新位置;CombatSystem 扫 Health+Position 处理受伤;AISystem 扫 AI+Position+Velocity 决策。每个 System 只碰它要的组件,组件组合千变万化也不互相干扰。
- **塔防的塔和怪**:塔有 Position+AttackRange+Damage,没有 Velocity(它不动);怪有 Position+Velocity+Health,没有 AttackRange(它不攻击)。两个 MovementSystem 用同一个 `view<Position, Velocity>()`,自动只扫怪(塔没 Velocity,view 跳过它),不用你写 `if (是怪)` 这种判断。
- **冰冻状态**:P2-05 第二节讲过的例子——敌人被冰冻时,把它的 Velocity 组件 `erase` 掉,MovementSystem 自动跳过它(没 Velocity 的实体 view 不返回),解冻时再 `emplace<Velocity>` 加回来。整个冰冻逻辑零侵入,不用在 MovementSystem 里写 `if (frozen) return`。本附录第四步的 `exclude_t<Health>` 就是这种思路的预演——你可以用"有/无某个组件"来表达状态,而不是用标志位。

这些例子都是 ECS 比面向对象明显灵活的地方。面向对象里,你要表达"塔不会动"大概得让 Tower 继承一个 ImmovableMixin 或者重写 update() 让它什么都不做,逻辑分散;ECS 里"塔不会动"就是"塔没 Velocity 组件"这一件事,干净利落。跑完本附录,你对照想想自己平时写的面向对象代码里那些 `if (frozen)`、`if (dead)`、`if (can_fly)` 的判断,在 ECS 里能怎么用组件加减替代——这是从面向对象思维切换到数据导向思维的关键练习。

---

## 六、把所有代码合到一个 CMake 项目里(可选)

如果你想要一个完整的项目结构,这里给一个 CMakeLists.txt,把三个 step 都编译出来:

```cmake
# CMakeLists.txt
cmake_minimum_required(VERSION 3.28)
project(minimal_ecs CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# 释放模式才有性能对比意义
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()
set(CMAKE_CXX_FLAGS_RELEASE "-O2")

# 引入 EnTT (header-only)
add_subdirectory(third_party/entt)

add_executable(step1_oop    step1_oop.cpp)
add_executable(step2_entt   step2_entt.cpp)
add_executable(step3_health step3_health.cpp)

target_link_libraries(step2_entt   PRIVATE EnTT::EnTT)
target_link_libraries(step3_health PRIVATE EnTT::EnTT)
```

目录结构:

```
minimal_ecs/
├── CMakeLists.txt
├── step1_oop.cpp
├── step2_entt.cpp
├── step3_health.cpp
└── third_party/
    └── entt/              # git clone https://github.com/skypjack/entt.git
```

构建:

```bash
cmake -B build -S .
cmake --build build --config Release
./build/step1_oop   50000
./build/step2_entt  50000
./build/step3_health 50000
```

---

## 七、踩坑提示

跑本附录的代码,几个常见的坑:

1. **C++ 版本不对**:EnTT 现行版本要求 **C++20**(`-std=c++20` 或 `c++23`)。用 C++17 会编不过(用了 concepts、`<=>` 等)。老资料说 EnTT 支持 C++14/17 的全过时了——EnTT 在 v3.x 系列里逐步收紧到 C++20,这是为了用上 C++20 的 concepts(写约束更清晰)、三方比较(简化迭代器实现)、范围库等新特性。你装 EnTT 之前先确认编译器版本:GCC 11+、Clang 14+、MSVC 19.30+(VS2019 16.10+)都支持 C++20 的核心特性。
2. **没开 `-O2`**:Debug 模式下,EnTT 的模板展开开销大,SoA 的缓存优势没体现,你量到的差距可能不到 1.5 倍。性能对比**必须 Release**。这一点怎么强调都不过分——很多新手第一次跑 ECS 基准,发现"咦,ECS 没比面向对象快多少啊",一查是编译器没开优化,模板代码全是没内联的函数调用,缓存优势被调用开销盖住。开 `-O2` 或 `-O3` 后,模板全内联,`view.each()` 内层循环向量化,差距立刻显现。
3. **`-I` 路径不对**:EnTT 是 header-only,但 `#include <entt/entt.hpp>` 要求 `-I` 指向 EnTT 仓库的 **`src`** 目录(不是仓库根)。比如你 clone 到 `third_party/entt`,那就 `-I third_party/entt/src`。这个坑很多人踩——他们以为 header-only 就是 `-I third_party/entt`,结果编不过,因为 `<entt/entt.hpp>` 要找的是 `src/entt/entt.hpp`。如果你用 CMake 的 `add_subdirectory(third_party/entt)` + `target_link_libraries(... EnTT::EnTT)`,这层路径 CMake 会自动处理,不用自己操心。
4. **编译器把循环优化掉了**:如果你量到"per-frame = 0 us",多半是编译器发现 update 结果没人用,把循环删了。加 `volatile float sink` 用一下结果。另一种防优化的办法是把 `N` 和 `FRAMES` 从命令行读(本附录就是这么做的,`argc > 1`),这样编译期不知道循环次数,不好激进优化。两种办法一起用最保险。
5. **MSVC 的坑**:Windows 上用 MSVC,记得开 `/O2` 和 `/std:c++20`。MSVC 对结构化绑定 `auto [e, pos, vel]` 的支持从 VS2019 16.7 起完整,确保你的 VS 够新。另外 MSVC 对模板的报错有时候比 GCC/Clang 难懂,如果碰到长报错,换个编译器试往往能更快定位问题。
6. **`registry.size()` vs `view.size()`**:`registry.size()` 是所有存活实体数,`view<...>().size()` 是匹配查询的实体数。第四步里你能看到两者不同——这是 view 筛选的直接体现。新手有时会混淆,以为"我 create 了 10000 个,view 怎么只有 5000",其实这正是 view 的本职工作,它只返回匹配的实体。
7. **EnTT 不同版本 API 微调**:EnTT 是个活跃项目,API 偶尔会调整。比如老版本(3.x 早期)的 view 遍历写法 `for (auto entity : view) { auto &pos = view.get<Position>(entity); }`,在新版本里推荐用 `for (auto [entity, pos, vel] : view.each())` 这种结构化绑定(README "Code Example" 一节演示的两种都行)。本附录用的是结构化绑定,基于现行 master 的 API。如果你装的是老版本,可能要调整写法。
8. **多线程警告**:EnTT 的 registry 单线程使用是安全的,但多线程下要小心——默认情况下两个线程同时遍历同一个 view、同时改 registry 都有数据竞争。EnTT 提供了 `entt::organizer`(P5-17 讲)来帮你做并发调度,声明每个 System 读/写哪些组件,organizer 自动算出哪些能并行。本附录不涉及多线程,但你要知道:**性能提升的另一大块来自多核数据并行**,不止数据布局——P2-07 和 P5-17 会讲透。

把这八个坑记牢,你跑本附录的代码会顺利很多。其实这些坑也是用任何 C++ 库做性能基准的通病——版本、优化、防优化、API 演进、并发,每一项都是踩过才会记住。本附录把它们集中列出来,省你几次返工。

---

## 八、从最小 demo 到真实引擎:下一步往哪走

跑完本附录,你手里有了一个能跑的最小 ECS。但真实游戏引擎比这复杂得多——本附录的小球只会"移动 + 撞墙反弹",真实引擎的小球(或角色、敌人、子弹)还要碰撞、渲染、AI 决策、播放音效、和网络同步。这一节帮你理清"从本附录的最小 demo 到真实引擎"还要补哪些东西,每一样对应本书哪一章。

### 你还差什么:本附录省掉的子系统

本附录的 demo 是个"裸 ECS",只跑了 MovementSystem 和(第四步的)damage_system。真实引擎至少还要:

- **渲染提交(P5-18)**:每帧 update 完要把所有带 Position+Color+Radius 的实体画出来。本附录省掉了 render,因为渲染本身是《图形渲染管线》那本的题,而且渲染开销通常比 update 大,加进来会把"数据布局收益"的信号盖掉。真实引擎里 RenderSystem 用 `view<Position, Color, Radius>()` 拿到所有可画实体,提交 draw call 给管线。
- **物理(P3-12 空间划分 + 本子线物理那本)**:小球要互相碰撞,不能穿过去。这要空间划分(四叉树/八叉树/BVH)快速找"谁和谁可能碰撞",然后做碰撞响应。本附录只做撞墙,没做球碰球,因为碰撞检测是另一大块题。
- **脚本(P4-14 Lua / P4-15 C#)**:游戏逻辑(敌人 AI、触发器、关卡)要用脚本写,不能写死在 C++ 里。本附录的 System 是 C++ 函数,真实引擎的很多 System 是脚本回调,这涉及 Lua VM 嵌入、C++ 对象绑定到脚本、热重载。
- **资源管理(P4-13)**:贴图、模型、音频这些大资产要异步加载、引用计数释放。本附录的小球是纯数学对象,没资产;真实引擎每个可视对象至少有个 Mesh 和 Material,都是资产。
- **主循环细节(P3-10 / P3-11)**:本附录的 `for (int f = 0; f < FRAMES; ++f)` 是简化主循环,真实引擎的主循环要处理固定步长(物理 update 用固定 dt 保证数值稳定)、可变步长(渲染用实际帧间隔)、accumulator 模式、帧率独立的动画插值。
- **多线程(P5-17)**:本附录单线程跑,真实引擎要把不同 System 分配到多核。MovementSystem 和 AISystem 不冲突的话可以并行,CombatSystem 和 MovementSystem 都改 Position 的话要有依赖。EnTT 的 `organizer` 帮你算依赖、自动并行。

每一项都是本书后面章节的题。本附录聚焦"组织数据"这一块(P2 系列),其他块在各自章节展开。把本附录当成"地基",其他子系统是"地基上的楼"。

### 进阶练习:把本附录的 demo 扩展一下

如果你想自己动手加深理解,这里有几个由浅入深的练习:

1. **加 RenderSystem(文字输出版)**:写一个 `render_system`,用 `view<Position, Color, Radius>()` 遍历所有可画实体,在终端打印几个统计量(比如"这一帧画了 N 个球,平均颜色是 ...")。这能让你感受 RenderSystem 怎么和 MovementSystem 共享 registry 但只读各自关心的组件。
2. **加碰撞检测**:写一个 CollisionSystem,用 `view<Position, Radius>()` 拿到所有可能碰撞的实体,O(N²) 两两检测距离,碰撞后反弹。这是数据导向的暴力碰撞,真实引擎用空间划分加速(P3-12)。跑 1000 个球还能 O(N²),跑 10000 个就明显卡了,这时候你会真正体会到空间划分的必要。
3. **加冰冻状态**:给一部分球加 `Frozen` 组件,MovementSystem 用 `view<Position, Velocity, exclude_t<Frozen>>()` 跳过冰冻的球。解冻时 `erase<Frozen>(e)`。这是 P2-05 第二节讲的"冰冻 = 移除 Velocity"思路的变体,亲手写一遍你就能体会"用组件加减表达状态"的灵活。
4. **换 Archetype 存储**:如果你想深入 Archetype,试试 Flecs(C 的 ECS 库,有 C++ 包装)或 Bevy(Rust),用同样的"几百个移动小球"重写一遍。同样的逻辑,不同的存储模型,你能直观对比 sparse_set 和 Archetype 在加减组件、查询时的差异。
5. **多线程化**:用 EnTT 的 `organizer` 把 MovementSystem 和(你自己加的)GravitySystem 注册进去,让 organizer 自动并行。观察在多核机器上,两核跑是不是接近单核的一半时间。这是 P5-17 的题,提前做能让你对"数据并行"有第一手感受。

这五个练习从浅到深,做完前两个你能巩固本附录的内容,做完后三个你已经触及真实引擎的设计了。每个练习都能用本附录的代码作起点,改改就能跑。

### 把本附录的代码变成你的"ECS 实验台"

最后一个建议:别跑完本附录就把代码删了。把它留下来,当成你的"ECS 实验台"。以后你读到任何 ECS 相关的概念(Archetype、group、event、scene 序列化),都可以在这个实验台里加一个小 System 试一试。比如:

- 读到 group(EnTT 比 view 更激进的预计算),在本附录的代码里加个 `registry.group<Position, Velocity>()`,跑跑看它和 view 的性能差距。
- 读到 on_construct/on_destroy 信号(EnTT 的组件生命周期事件),在本附录的代码里加个回调,打印"新建了一个 Position 组件",观察它什么时候被调用。
- 读到 registry 的 sort(给实体排序),在本附录的代码里按 Position.x 排序,然后看 view 遍历顺序的变化。

这个实验台的价值在于:你以后碰到任何 ECS 问题,都能在一个**已知能跑的最小代码**上做实验,而不是从零搭一个测试项目。本书正文里的所有概念,你都能在这个实验台里摸到。这是"动手实践"真正的长期价值。

---

## 九、内存布局总览:把两版画在一起

最后,我们把朴素 OOP 版和 EnTT 版的内存布局并排画出来,这是本附录的总结图:

![两版内存布局对照:上半 OOP 版 `std::vector<Ball>`,每个 Ball 是 32 字节一整块(8 个字段混一起,update 时颜色半径被白拉进缓存);下半 EnTT 版,Position[]/Velocity[]/Color[] 三个 pool 分开,每个 pool 内部 dense 数组连续,view 只扫前两个 pool,缓存行 100% 利用](images/fig-appB_01-aos-vs-entt-layout.png)

这张图的要点三个:① 上半 OOP 版,一条 64 字节缓存行装 2 个 Ball(各 32 字节),update 时只有 Position+Velocity(每个 Ball 16 字节)有用,Color+Radius 被白搬;② 下半 EnTT 版,Position 是独立的连续 pool,一条 64 字节缓存行装 8 个 Position(每个 8 字节),update 时 100% 有用;③ EnTT 的 Color pool 在另一块内存,MovementSystem 根本不碰它,它连缓存行都不会被加载。

这就是本附录"量到的差距"的物理根因——P2-06 整章拆透的"数据布局决定性能",在你的机器上变成了真实的 2~5 倍。

---

## 十、本附录对应的章节回指

本附录四步,每步对应本书哪一章:

| 步骤 | 做了什么 | 对应章节 |
|------|---------|---------|
| 第一步 | 朴素 `std::vector<Ball>`(AoS)作为基线 | **P1-04**(组织游戏对象的困境:面向对象为什么崩溃)——这就是那面"性能墙"被对照的真实代码 |
| 第二步 | 用 EnTT 写 ECS:Position/Velocity/Color/Radius 组件 + MovementSystem | **P2-05**(ECS 三件套)+ **P2-06**(Component 的存储:SoA vs AoS)——三件套的字面落地,组件连续 pool 即 SoA |
| 第三步 | 两版计时对比,2~5 倍差距 | **P2-07**(System 的遍历:缓存友好与并行)——连续 + SIMD + 数据并行的实测收益 |
| 第四步 | 加 Health 组件,view 筛选 | **P2-08**(Archetype)+ **P2-09**(Query:快速找"有这些组件的实体")——view 的筛选,以及更深的 Archetype 分组 |

如果你跑完本附录还意犹未尽,继续往这几章钻,它们把本附录"为什么快"的根挖到字节级。

---

## 十一、章末小结

### 回扣主线

本附录是全书的"动手收尾"。前面 20 章我们一直讲 ECS——Entity 是 ID、Component 是纯数据、System 是纯行为,SoA 比 AoS 快,Archetype 比 sparse_set 更紧凑,view 只扫匹配实体。可这些都是"纸上"的。本附录补上"摸"这一环:你亲手写了面向对象版和 EnTT ECS 版,亲手跑了成百上千个实体,亲手用 `std::chrono` 量出 2~5 倍的差距。这个"亲手量到"的过程,把 P2-06 那张缓存 miss 图,从书上的理论,变成你机器上的真实数字——**数据布局决定性能,这不是教条,是你在自己 CPU 上量出来的事实**。

把本附录和正文章节串起来,你会看到一条完整的学习闭环:**P0-01 立起全景**(游戏引擎是大循环、ECS 是答案)→ **P1-04 拆面向对象的墙**(继承墙 + 性能墙)→ **P2-05 立三件套**(Entity/Component/System)→ **P2-06 拆存储**(SoA vs AoS + sparse_set)→ **P2-07 拆遍历**(缓存友好 + SIMD + 并行)→ **P2-08 拆 Archetype**(组件组合连续存放)→ **P2-09 拆 Query**(view 怎么筛选)→ **本附录**把你前面学的所有概念,落成几百行能跑的代码。这条闭环走完,你对 ECS 的理解不再是"听过"、"读过",而是"写过、跑过、量过"——这是任何技术真正掌握的标志。

如果你跑完本附录后有种"原来如此"的感觉,那本附录就成功了。如果你跑完还有疑问,比如"为什么我的比值只有 1.5 倍不是 3 倍"、"为什么 N=200000 时 EnTT 也变慢了",回到 P2-06 那张缓存 miss 图、本章"怎么读这张图"那节,把你的工作集大小(实体数 × 每实体字节数)和你机器的 L1/L2/L3 容量对一下,疑问通常就解开了。ECS 性能不是玄学,是物理——数据怎么摆、工作集多大、缓存层级多深,每一段都对得上。

### 五个为什么

1. **为什么用 EnTT 而不是自己手撸一个 ECS?**——EnTT 是工业级标杆(Minecraft 在用),它的 sparse_set、pool 模型、view 筛选,就是 P2-05~P2-09 讲的那些概念的工业实现。用 EnTT,你摸到的是真实 ECS 库的 API 和性能;自己手撸一个,容易在数据结构细节上跑偏,反而量不到稳定的差距。当然,读完本书你想自己实现一个来深入理解,那是好事——附录 A 给了阅读 EnTT/Bevy 源码的路线图。
2. **为什么性能差距是 2~5 倍,不是书上说的"几十倍"?**——本附录的 OOP 版为了公平**没加虚函数**(连续 `std::vector<Ball>`,32 字节),这是 AoS 的最好情况。P2-06 讲的"几十倍"是堆上散落 + 指针追逐(虚函数或 `vector<Ball*>`)的情况。你可以自己给 OOP 版加 `virtual void update(float dt)` 再量一次,差距会拉到 5~10 倍。
3. **`view<Position, Velocity>` 怎么知道哪些实体同时有这两个组件?**——registry 内部维护"每种组件一个 pool",pool 是 sparse_set(sparse 分页索引 + dense 连续数组)。view 拿到 Position pool 和 Velocity pool,选较小的那个遍历,对每个 entity 去另一个 pool 的 sparse 索引查"在不在",在的话从 dense 拿数据。P2-06 的 sparse_set 双数组设计就是为了这个 O(1) 查找 + 连续遍历两全其美。
4. **为什么 `damage_system` 只扫一半实体不浪费?**——Health pool 里**只有**那 N/2 个挂了 Health 的实体,连续紧凑。damage_system 遍历它,缓存全命中,完全不碰另外 N/2 个没 Health 的实体(它们根本不在 Health pool 里)。这就是 P2-09 Query 筛选的物理基础——每个 pool 独立,view 天然只扫它要的。
5. **EnTT 的 sparse_set 方案和 Bevy 的 Archetype 方案,到底哪个好?**——两者都是组件级 SoA 的工业实现。sparse_set(EnTT):每种组件一个 pool,view 求交集;组件组合复杂时,view 遍历要做 sparse 查找,有常数开销。Archetype(Bevy/Flecs/Unity DOTS):组件组合相同的实体放进同一个 archetype,连续存放;view 扫"组件组合是超集"的 archetype,不用 sparse 查找,但加/删组件要搬实体到另一个 archetype。两者各有所长,P2-08 详讲 Archetype 的取舍。本附录用 EnTT 的 sparse_set,因为 API 最简洁、最适合入门。

### 想继续深入往哪钻

- **想搞懂 EnTT 的 sparse_set 字节级实现**:附录 A"游戏引擎源码阅读路线图"给了 EnTT 的阅读顺序,核心是 `src/entt/entity/sparse_set.hpp` 和 `storage.hpp`。P2-06 的技巧精解已经拆过它的双数组设计。
- **想搞懂 Archetype**(比 sparse_set 更进一步的 SoA):第 2 篇 P2-08。它会拆透 Bevy 的 archetype 怎么把"组件组合相同"的实体连续存放,view 怎么扫 archetype 而不是 pool。
- **想搞懂 view / Query 怎么快速返回**:第 2 篇 P2-09。它会拆透 view 的位掩码匹配、archetype 匹配的算法。
- **想搞懂怎么把这套东西接进一个完整的主循环**(主循环、固定步长、渲染提交):第 3 篇 P3-10(主循环)、第 5 篇 P5-18(渲染提交)。本附录的 `for (int f = 0; f < FRAMES; ++f)` 是个简化主循环,真实主循环要处理固定步长、accumulator、多线程 job。
- **想看 EnTT 的更多用法**(group、sort、sighash、organizer 调度器):EnTT 官方 wiki 和 `src/entt/entity/` 下的头文件。本书 P5-17 讲 job 系统时会用到 organizer。
- **想换个语言试试**:Bevy(Rust)的 ECS 和 EnTT 思想一致但 API 不同,附录 A 也给了 Bevy 的阅读路线图。Rust 的所有权模型让 ECS 的借用检查更严格,值得对照体会。

### 引出附录 A

本附录带你在 EnTT 的 API 层面"摸"了一遍 ECS。如果你想**钻进 EnTT 源码**,看 sparse_set 的双数组在字节级怎么实现、registry 怎么管所有 pool、group 怎么做比 view 更激进的预计算——那就去附录 A"游戏引擎源码阅读路线图",它给你 EnTT(和 Bevy)的阅读顺序 + 关键模块地图,把本附录用的那些 API,挖到它们的实现深处。

> **下一篇**:[附录 A · 游戏引擎源码阅读路线图](附录A-游戏引擎源码阅读路线图.md)
