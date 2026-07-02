# 物理引擎系列 · Box2D v3.2 源码事实锚点(写作 agent 必读)

> 本文件由主控基于**本地已 clone 的真实源码**(`../box2d/`,Box2D **v3.2.0**,commit `56edae79f2949d86142b03450d5d60f63bcf5a6f`)逐条 Grep/Read 核实后写下。**所有写作 agent 动笔前必读本文件**,并以此为准——它修正了总纲/P0-01 里若干"老 v3 印象"。引用源码一律以本锚点 + 现场再 Grep/Read 为准,**严禁凭记忆写函数名/行号**。
>
> 本系列一贯要求"诚实标注版本演进"(同 go1.27 / Box2D-v3 之于老 v2 资料)。下面标 ★ 的是**与总纲/P0-01 简化模型有出入、必须按真实源码写并诚实标注**的事实。

---

## 0. 版本与定位

- **Box2D v3.2.0**(major=3, minor=2, revision=0),commit `56edae79`,`src/core.c:101 b2GetVersion`。
- ★**这是 v3.2,不是总纲默认假设的"早期 v3"**。v3.2 相比早期 v3,加入了**并行求解架构**(solver sets / constraint graph / island / scheduler / parallel_for)、**子步进(soft constraint / TGS 风味)求解器**、**mover 系统(CCD 新形态)**。总纲/P0-01 用"Sequential Impulse / PGS"这个**历史算法名**做概念主线是对的(Erin Catto 的 Sequential Impulse 就是这个算法的祖师),但**讲源码时必须展示 v3.2 真实的分阶段并行实现**,并诚实标注"v3.2 把 SI 用约束图着色并行化了、加了 warm start + soft constraint + speculative contacts"。
- 公共 API 仍是 **C 句柄 API**:`b2WorldId` / `b2CreateWorld` / `b2World_Step` / `b2CreateBody` / `b2CreateShape` …(见 `include/box2d/box2d.h`)。★**不是 v2 的 C++ 类**(`b2World::Step` 等已过时)。v3 用句柄 `b2WorldId` 解引用到内部 `b2World*`。
- ★`#define b2CreateWorld b2CreateWorldDoublePrecision`(`box2d.h:17`):v3.2 有**单/双精度切换**,默认走双精度实现。

---

## 1. 一个时间步的真相:`b2World_Step` 与子步进 ★

入口:`void b2World_Step( b2WorldId worldId, float timeStep, int subStepCount )`,[src/physics_world.c:828](../box2d/src/physics_world.c#L828)。

关键(逐行已核):
- `context.dt = timeStep;`
- `context.subStepCount = max(1, subStepCount);`(`physics_world.c:866` 附近)
- `context.h = timeStep / subStepCount;` —— **子步步长**(物理积分真正用的步长)
- `context.inv_dt`, `context.inv_h`
- `context.contactSoftness = b2MakeSoft(contactHertz, dampingRatio, h);` —— 接触**软约束**(hertz + 阻尼比)
- `context.staticSoftness = b2MakeSoft(2.0f * contactHertz, dampingRatio, h);` —— **静态接触更硬**(2 倍 hertz)
- `b2Solve(world, &context);`([src/solver.c:1272](../box2d/src/solver.c#L1272))

> ★**钉死**:用户调一次 `b2World_Step(dt=1/60, subStepCount=4)`,内部真正积分用的是 `h = dt/4`。总纲/老资料说"固定步长 dt"是概念主线(对的),但源码层要讲清**subStepCount 把一个大步切成 N 个 h 子步**,每个子步跑一遍约束求解迭代。这是 v3.2 的**子步进软约束求解器**。

---

## 2. 求解器:`b2Solve` 的分阶段并行流水线 ★(P5-16 核心)

`b2Solve(world, stepContext)`,[src/solver.c:1272](../box2d/src/solver.c#L1272)。求解分**阶段(stage)**,精确枚举见 [src/solver.h:77-85](../box2d/src/solver.h#L77-L85) 的 `b2StageType`(**共 9 个阶段**,已逐行核):

```
b2_stagePrepareJoints      →  准备关节约束
b2_stagePrepareContacts    →  准备接触约束(算 effective mass / bias / softness)
b2_stageIntegrateVelocities →  ① 积分速度: v += h * (invMass*force + gravityScale*gravity)   [symplectic Euler!]
b2_stageWarmStart          →  warm start:用上一步的累积冲量初值(接触 + 关节)
  ↻ [迭代 subStepCount×iterationCount 轮]
b2_stageSolve              →  ② 顺序/并行解约束(Sequential Impulse 主体)
b2_stageIntegratePositions →  ③ 积分位置: x += h * v;  rotation += IntegrateRotation(...)
b2_stageRelax              →  放松(TGS soft 尾调)
b2_stageRestitution        →  恢复系数(反弹)后处理
b2_stageStoreImpulses      →  存累积冲量供下帧 warm start
```
(执行 dispatch 见 [src/solver.c:852](../box2d/src/solver.c#L852) 的 switch;主循环 `b2SolverTask` @ solver.c:1007;阶段顺序注释 @ solver.c:1039-1046。)

- **速度积分(半隐式/辛欧拉)**在 [src/solver.c:100-102](../box2d/src/solver.c#L100-L102):
  - `linearVelocityDelta = h * invMass * force + h * gravityScale * gravity;`
  - `angularVelocityDelta = h * invInertia * torque;`
  - 位置积分 `deltaRotation = b2IntegrateRotation(deltaRotation, h * angularVelocity);`(solver.c:158)
  - ★**这正是 P2-07 要讲的"半隐式欧拉"**:先用加速度更新速度、再用新速度更新位置。Box2D 用的就是它,源码铁证。
- **约束图着色并行**(`src/constraint_graph.c`):把接触/关节按"是否共享物体"建图、**图着色**,同色的约束互不相邻 → **可并行解**。`b2SolverBlock.colorIndex` 决定块归属。★这是 v3.2 把"顺序冲量"并行化的核心数据结构,讲 P5-16 必须点到。
- **warm start**:`b2WarmStartContactsTask`(contact_solver.c:1811)、`b2WarmStartJointsTask` —— 用上一帧累积冲量做迭代初值,显著加速收敛。`world->enableWarmStarting`(contact_solver.c:44)。
- **soft constraint(TGS 风味)**:`b2Softness{ massScale, impulseScale, biasRate }`(contact_solver.c:283-314),把"刚性"冲量变成有 hertz/阻尼比的软弹簧,避免堆叠抖动。`b2MakeSoft(hertz, dampingRatio, h)`。
- **speculative contacts(防穿透)**:contact_solver.c:1924-1931,`specBias` —— 用未来一帧的相对速度预判穿透,配合 CCD 让高速物体不 tunnel。P5-18(CCD)点到这里。
- 求解器还有**溢出回退路径**:`b2PrepareContacts_Overflow`(contact_solver.c:24)、`b2WarmStartContacts_Overflow`(162)——并行任务超容时串行兜底。

> ★**给 P5-16 写作 agent**:概念主线仍讲透 **Sequential Impulse / PGS**(Erin Catto 算法:每个约束按顺序施加冲量、多轮迭代收敛、本质解 LCP/MLCP),这是物理引擎教科书级核心,读者必须懂。然后诚实标注 + 展示 **v3.2 的真实实现**是"分阶段(stage)+ 约束图着色并行 + warm start + soft constraint + speculative"。**两者不矛盾**:SI 是算法,v3.2 是它的高性能工程化。

---

## 3. 积分器(symplectic Euler)与刚体动力学

- 速度/位置积分:`src/solver.c`(b2IntegrateVelocitiesTask @66, b2IntegratePositionsTask @114)。**半隐式欧拉**,见上节。
- **刚体质量/惯性**:`b2CreateBody` [src/body.c:175](../box2d/src/body.c#L175);`b2ComputeShapeMass`(body.c:587)累加每个 shape 的 `b2MassData`;`invMass = 1.0f/mass`(body.c:598);**平行轴定理**:`inertia = massData.rotationalInertia + massData.mass * b2Dot(offset, offset)`(body.c:613)。★P2-05 讲惯性张量/转动惯量,这里是用**平行轴定理**把各 shape 惯性平移累加到质心——真实源码佐证。
- Body/Shape 公共 API 在 `include/box2d/box2d.h`:`b2CreateBody` / `b2CreateCircleShape` / `b2CreatePolygonShape` / `b2CreateCapsule` / `b2CreateSegment`。★v3 形状是 **circle / polygon / capsule / segment** 四类(不是 v2 的 b2PolygonShape 类)。

---

## 4. 检测侧:宽相 + 窄相 + 接触流形

### 4a. 宽相:动态 AABB 树(每物体类型一棵)★
- `b2BroadPhase_CreateProxy`(broad_phase.c:104)→ `b2DynamicTree_CreateProxy(bp->trees + proxyType, ...)`。★**broad phase 内部按 body type(static/kinematic/dynamic)各维护一棵动态树**(`bp->trees[]`),不是单一一棵树。配对查询时跨树。
- `b2DynamicTree_CreateProxy` [src/dynamic_tree.c:744](../box2d/src/dynamic_tree.c#L744),`_DestroyProxy`(765),`_MoveProxy`(782),`_EnlargeProxy`(798),`_Rebuild`(1879),`_GetHeight`(870)。
- ★**树的重平衡/重建**:`b2DynamicTree_Rebuild`(dynamic_tree.c:1879)——物体运动导致树质量下降时批量重建(不是每次 MoveProxy 都旋转)。讲 P3-11 动态树增量更新,讲清 **MoveProxy(移动)+ 定期 Rebuild(重建)** 的搭配。
- AABB 工具(★核实修正):AABB 相交/包含/合并/中心/范围是 `B2_INLINE` 定义在 [include/box2d/math_functions.h:749-789](../box2d/include/box2d/math_functions.h#L749-L789)(如 `b2AABB_Overlaps`@785、`b2AABB_Contains`@749、`b2AABB_Union`@774);**`src/aabb.c` 只有 `b2IsValidAABB`@10 和 `b2AABB_RayCast`@19(slab 法,Ericson RTCD p179),没有两 AABB 相交判断函数**(别去 aabb.c 找 Overlaps)。

### 4b. 窄相:GJK + SAT(写在 per-shape-pair collide 函数里)
- **GJK**:`b2ShapeDistance`(src/distance.c:424,注释挂 Erin Catto GJK GDC2010 pdf),用 `b2Simplex`(v1/v2/v3 顶点)+ simplex cache(`b2SimplexCache`,distance.c:206)。★**GJK 算两凸形状的距离/最近点**,用于"没碰时算多近"。P4-13 主角。
- **SAT**:**不是**一个独立 `b2SAT()` 函数,而是融在每个 `b2CollideXxx` 里(找"最小分离边/轴")。见 `src/manifold.c`:
  - `b2CollideCircles`(manifold.c:36)、`b2CollideCapsules`(237,注释 "find reference edge using SAT" @327)、`b2CollidePolygonAndCircle`(127,"Find the min separating edge" @138)、`b2CollidePolygonAndCapsule`(504)…
  - ★**SAT 在 Box2D 里以"找参考边(reference edge)/ 最小分离轴"的形式实现在 per-shape-pair 函数中**。讲 P4-12 先讲透 SAT 定理(凸多边形→投影到每条法线轴→看能否分离),再点 manifold.c 里 "min separating edge" 就是它的落地。
- **接触流形(manifold)**:`b2LocalManifold`(manifold.c)+ `b2LocalManifoldPoint`(法线方向、穿透深度、接触点)。`src/manifold.c` 一族 `b2CollideXxx` 的输出就是接触流形。P4-14 主角。

### 4c. 距离/形状几何
- `src/distance.c`(GJK 距离)、`src/geometry.c`(几何原语)、`src/hull.c`(凸包,多边形顶点排序)、`src/math_functions.c`(数学)。写作时按需 Grep。

---

## 5. 关节约束(P5-17)

每类关节一个文件:`src/revolute_joint.c`(旋转/铰链)、`src/prismatic_joint.c`(平移/滑轨)、`src/distance_joint.c`(距离)、`src/weld_joint.c`(焊接)、`src/wheel_joint.c`(轮)、`src/motor_joint.c`(电机)。公共 `src/joint.c`。★关节和接触**同样进 Sequential Impulse 求解**(`b2WarmStartJointsTask` + solve 阶段),区别只是约束方程不同。P5-17 主角。

---

## 6. 休眠 / CCD / 稳定性(P5-18)

- **休眠(sleeping)**:solver sets 里有 `b2_awakeSet`(physics_world.c:597 等)和睡眠集合;`b2World_EnableSleeping`(box2d.h)。静止物体移入睡眠集合,不参与求解。
- **★CCD(连续碰撞检测)—— 重大更正(写作 agent 核实源码后修正)**:★**`src/mover.c` + `b2World_CastMover` / `b2World_CollideMover` 不是刚体 CCD,而是角色控制器(character controller)**——用 `b2Capsule` 作 mover 收集碰撞平面、`b2SolvePlanes`(mover.c:7)做平面求解,供 kinematic 角色手动移动/滑墙。**刚体 CCD 的真实主体是两层叠加**:
  - ① **speculative contacts(默认常开)**:contact_solver.c:1924-1931 的 `specBias = s·inv_h` + mask blend,用未来一帧相对速度预判穿透;`B2_SPECULATIVE_DISTANCE = 4×B2_LINEAR_SLOP = 2cm`(constants.h:55)。廉价广覆盖。
  - ② **TOI sweep(按需,对 `b2_isFast` 高速物体)**:`b2SolveContinuous`(solver.c:386,Tracy zone `ccd`)调 `b2TimeOfImpact`(distance.c:1143,保守前进 conservative advancement,内部复用 GJK),算出碰撞时刻 TOI,把物体截停在 TOI(`B2_CORE_FRACTION=0.25` solver.c:180)。精确窄打击。
  - `b2World_EnableContinuous` / `b2World_IsContinuousEnabled`(box2d.h)总开关。
  - ★**老资料讲 CCD 多是 "sub-stepping / swept shape" 通用说法;v3.2 落地为 "speculative 廉价广覆盖 + TOI 精确窄打击" 的工程化分层**。讲 P5-18 以这两层为主;**mover 单独点明是角色控制器,别和刚体 CCD 混**(这是本锚点原文的错,已更正)。
- **休眠(sleeping)**:solver sets 里有 `b2_awakeSet`(physics_world.c:215 钉死下标)和睡眠集合;静止物体**整岛迁移**进睡眠集合(solver_set.c `b2TrySleepIsland`@155、`b2WakeSolverSet`@35,memcpy + swap-remove)。`B2_TIME_TO_SLEEP=0.5f`(constants.h:73),速度低于 `sleepVelocity` 累积 `sleepTime` 达阈值才睡。`b2World_EnableSleeping`(box2d.h)。
- **restitution(恢复系数)阈值**:`b2World_SetRestitutionThreshold`(box2d.h,physics_world.c:1782)—— 相对速度低于阈值不算反弹(`relativeVelocity > -threshold`,contact_solver.c:422),且 `totalNormalImpulse==0` 跳过 speculative 未真撞接触。默认 1.0 m/s(types.c:17)。
- **速度封顶(稳定性兜底)**:`maxLinearSpeed` / `maxAngularSpeed` 钳制 + `b2_isSpeedCapped`(solver.c:139-153),防数值爆炸物体飞掉。

---

## 7. 给写作 agent 的铁律提醒(基于真实源码)

1. **认准 v3.2 的 C API**(`b2WorldId`/`b2World_Step`/`b2CreateBody`/`b2CreateCircleShape`…),**绝不**写成 v2 的 `b2World::Step` / `b2Body` 类 / `b2PolygonShape` 类。
2. **求解器讲法**:概念讲透 Sequential Impulse / PGS / LCP,源码展示 v3.2 分阶段(stage)+ 约束图着色并行 + warm start + soft constraint + speculative。**诚实标注 v3.2 把 SI 并行化了**。
3. **SAT 讲法**:讲透定理,落地指 manifold.c 的 `b2CollideXxx` + "min separating edge / reference edge",**不要**编造独立 `b2SAT()` 函数。
4. **积分器**:`solver.c:100-102` 是半隐式欧拉,直接贴。显式欧拉的不稳定用 numpy 模拟画图佐证(P2-06)。
5. 每个引用**现场 Grep/Read 核行号**;不确定**只标文件不标行号**;简化代码标"(简化示意)"。
6. **★鼓励质疑/修正总纲**:发现总纲/P0-01 与真实源码出入(本文件已列多处),按真实源码写 + 诚实标注,并在章末"技巧精解"或注释里点一句"v3.2 相比早期 v3 的演进"。这是高质量信号。

---

> 本锚点会随写作推进补充修正。agent 若 Grep 到新的非显然事实(尤其与总纲出入),回报主控,主控回写本文件 + 最终存入 memory(`physics-engine-source-facts`)。
