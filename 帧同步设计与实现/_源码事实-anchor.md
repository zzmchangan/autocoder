# _源码事实锚点(帧同步系列)

> 写章节时直接引用的源码事实清单。所有结论均经 Grep/Read 真实 `.cs` 文件核对(不照抄 docs/)。
> **版本策略:跟最新,不锁快照**。本次核对基准 HEAD = `0ea90d7`。项目在加固期,行号会漂移,**引用前必须重新 Grep/Read 核实**。
> 源码根:`C:/Users/86133/Desktop/Program/LockstepSdk/`。下面简写 `…/`。

---

## 0. 文档与代码的出入(写书必守,以源码为准)

| 项 | 文档说 | 源码真实 | 出处 |
|---|---|---|---|
| Handler 数量 | 7 个 | **9 个** | LockstepServer.cs:101-114 |
| 协议版本 | 1.0 | **1.1**(Version=65537) | ProtocolVersion.cs:22-54 |
| RTTVAR beta | 0.125(隐含) | **0.25** | NetworkClock.cs:43 |
| KCP | 暗示已实现 | **stub**(Update/SetConfig 空方法体) | KcpClient.cs:605-613 |
| `LFloat.One` 类型 | 早期 `const int`(bug) | **现在 `const long`**(已修) | LFloat.cs:19 |
| GameEnd | 有枚举值 | **无 Message 类、无 Parser case**(占位) | MessageType.cs / Messages.cs |

---

## 1. 定点数学(我亲读,LFloat 261行 / LMath 431行 / LInt128 151行)

### LFloat(`…/src/Lockstep.Core/Math/LFloat.cs`)
- `readonly struct`,`public readonly long RawValue`(:14,:23)。注释自报"Q48.16"——实际是 **Q48.16 用 `long` 存**(64 位总数 / 16 位小数 / 47 位整数 + 1 符号位)。范围 ±140,737,488,355,327,精度 1/65536 ≈ 0.000015。
- `Shift = 16`(:16),`Precision = One = 1L << 16 = 65536`(:18-19,**`const long`**),`Half = 32768`(:20)。
- **`One` vs `OneVal` 区分(历史 bug 现状)**:`One` 是 `const long` 65536(标量),`OneVal` 才是 `LFloat`(:27)。`LFloat.One / 2` = 32768(long 除法),不是 LFloat。issues/README 里"LFloat.One 是 int"是**早期版本** bug(已修 int→long),但"One 不是 LFloat 类型"的语义陷阱仍在。
- 构造:`LFloat(bool isRaw, long rawValue)`(:40,私有约定),`FromRaw/FromInt/FromFloat/FromDouble`(:43/46/49/62)。
- `FromFloat`(:49-59):DEBUG 校验 `IsFinite` + 范围 `MaxSafe = long.MaxValue >> Shift`(:54-56);实际转换 `(long)(value * One)`(:58)。
- **隐式 `int → LFloat`**(:98),显式转出 `LFloat → int/long/float/double`(:94-97)。
- 算术:`+/-` 直接 RawValue 加减(:103/106);`*` 调 `LMath.MulShiftFast`(:110);`/` 调 `LMath.DivShiftFast` + 除零 throw(:141-145);`%` RawValue 取模(:151-155)。
- **`MulFallback`/`DivFallback`(:117/131)**:`internal`,`#if NET8` 用硬件 Int128,`#else` 用 LMath 软件版。注释明说"保留供 MathEquivalenceTests 双路径逐位比对"(:113-115)。**这是"性能+正确性"双全的测试驱动设计**。
- **Floor/Ceil/Round 用位掩码非除法**(:219-229):`RawValue & ~(One-1)` 清小数位。Round 先加 `Half` 再掩码。`[MethodImpl(AggressiveInlining)]`。
- `Lerp`(:232-235):`a + (b-a)*t`,经一次 MulShiftFast。
- Sqrt/Sin/Cos/Tan/Atan2 代理到 LMath(:240-248)。
- `MinValue = long.MinValue`(:29),`MaxValue = long.MaxValue`(:30),`Epsilon = RawValue 1`(:31)。

### LMath(`…/src/Lockstep.Core/Math/LMath.cs`,partial class 主文件)
- **常量 PI/PIHalf/PI2**(:196-198):PI=205887,PIHalf=102943,PI2=411775(都是 Q16.16 RawValue;π×65536≈205887 ✓,2π×65536≈411775 ✓)。`Rad2Deg = 180/PI`,`Deg2Rad = PI/180`(:199-200)。
- **MulShift(非NET8 定点乘核心)**(:21-25):`LInt128.Mul(a,b).ArithmeticShiftToLong(shift)`。注释(:14-18)详述 **P0 跨 TFM 修复**:原 abs-then-negate(truncate toward zero)对负数非整除乘积与 NET8 有符号移位(floor toward -inf)差 1,**影响所有 LFloat 乘法跨 TFM desync**。
- **★Mul2SumShift/Mul3SumShift/Mul4SumShift**(:34-60):多乘积"累加后统一右移"。注释(:27-30)极关键:"必须累加后右移而非各项各自右移再求和,否则与 NET8 的 ((Int128)p0+p1+...)>>Shift 在低 16 位非零时差 1-2 RawValue(P0-1 跨 TFM 分叉)"。用于 LMatrix/LQuaternion。**减法项传取反操作数**(因为 -g*h == (-g)*h,g 取反不溢出)。
- **MulShiftFast(乘法统一入口)**(:70-80):
  - `#if NET8_0_OR_GREATER`:`FastLimit = 1L<<31`,若 `|a|,|b| < 2^31` 则 `|乘积| < 2^62 < 2^63` 走 long 快速路径 `(a*b)>>Shift`(注释"游戏值命中率 >99%");否则 `(long)(((Int128)a*b)>>Shift)`。
  - `#else`:退化为 `MulShift(a,b,Shift)`。
  - 注释(:67)"阈值逻辑只此一处"。
- **DivShiftFast**(:88-98):NET8 `|a| < 2^47`(=long.MaxValue>>Shift)走 long `(a<<Shift)/b`,否则 Int128。注释(:84)解释 2^47 阈值。
- **DivShift(非NET8)**(:104-130):取绝对值;快速路径 `ua <= ulong.MaxValue>>shift` 走 64 位除法;慢路径 `high=ua>>(64-shift), low=ua<<shift`,若 `high>=ub` 溢出返回 MaxValue,否则调 Div128By64。
- **★Div128By64(Knuth TAOCP Vol.2 §4.3.1 Algorithm D, base 2^32)**(:137-178):归一化 `s=Clz64(divisor)` 消除试商误差 → q1 试商 + 至多 2 次修正 → 部分余数 → q0 同。注释"~2 次原生除法 + 修正,替代 64 次逐位迭代""前置 high<divisor"。**这是 128/64 除法的硬核点**。
- **Clz64**(:182-193):手写 64 位前导零(二分)。
- **Atan2**(:205-263):象限翻转(四个象限分别处理 x,y 正负)+ `LUTAtan2.table[num5*DIM+num4]` 查表。特殊点:x=0/y=0 处理(:212-221)。
- **Acos/Asin**(:266-281):`num = val.RawValue*HALF_COUNT>>Shift + HALF_COUNT`,Clamp 到 [0,COUNT],查表。
- **Sin/Cos**(:284-295):`LUTSinCos.getIndex(radians)` 查 sin_table/cos_table。
- **SinCos**(:298-303):一次 getIndex 取 sin+cos(避免两次索引计算)。
- **Tan**(:306-311):SinCos 后 `s/c`,c=0 返回 MaxValue。
- **★Sqrt(long) 牛顿迭代**(:314-330):`a<=0 return 0; a<4 return 1`;初始猜测 `x = 1L<<((64-clz+1)>>1)`(:322,注释(:319-321)说原 `64-(clz>>1)` 给过大猜测导致 30+ 次迭代,已修);`x1=(x+a/x)>>1; while(x1<x){x=x1; x1=(x+a/x)>>1;}` 收敛到 floor(sqrt)。
- **Sqrt(LFloat)**(:351-380):
  - `#if NET8`:`a.RawValue < FastMaxRaw(=2^47)` 走 64 位牛顿 `Sqrt(a.RawValue<<Shift)`(:358-360);否则 `SqrtFallback`。注释(:356-357)"此前 NET8 路径无脑走 Int128,比非 NET8 快速路径还慢——本修复补齐"。
  - `#else`:`val<=ulong.MaxValue>>Shift` 走快速;否则浮点 sqrt 初猜 + 三轮 DivShift 迭代(:370-378),注释"三轮确保 bit-level 精度"。
- **SqrtFallback**(:385,NET8 internal):Int128 版供等价性测试。
- **InternalLeadingZeroCount**(:333-348 long 版 / :406-411 Int128 版):NET8 用 `BitOperations.LeadingZeroCount`,非 NET8 手写二分。

### LInt128(`…/src/Lockstep.Core/Math/LInt128.cs`)
- `struct`(非 readonly),`ulong Low; long High;`(:13-14,**High 是有符号 long**)。
- **Mul**(:28-68):`#if NET8` 走硬件 `Int128`;`#else` 4 路 32×32→64 分块乘法(al/ah × bl/bh)+ 进位累加 + 负数二补码取反加一(:59-65)。
- Add/Sub(:71-86):带进位/借位。
- Sign(:88):High<0→-1。
- **ToShiftedLong(truncate toward zero)**(:94-110):绝对值移位再取负。有溢出检查 `absV.High >= 1<<(shift-1)`。
- **★ArithmeticShiftToLong(floor toward -inf)**(:122-126):`(long)((Low>>shift) | (ulong)High<<(64-shift))`——二补码算术右移。注释(:113-119)详述:**与 NET8 的 `(long)(int128>>shift)` 逐位等价**;与 ToShiftedLong 的区别就是对负数非整除乘积差 1(floor vs truncate),**这是跨 TFM desync 根因**。约定 shift∈[1,63]。
- Negate(:128-134):二补码取反(~+1)。

### LUT 查表(`…/src/Lockstep.Core/Math/LUT/`)
- **LUTSinCos.cs(8222 行巨型文件!)**:COUNT=4096(12 位),MASK=4095。getIndex = `(radians.RawValue * 4096L / 411775L) & MASK`(411775 = PI2 的 RawValue)。表值范围 ±65536(±1.0 in Q16.16),峰值附近几个值=65536。**读此文件必须 offset 分页,否则 25k token 截断**。
- LUTAtan2.cs:DIM=64(64×64 二维表)。
- LUTAcos.cs:COUNT=1024,HALF_COUNT=512。
- LUTAsin.cs:同 Acos 结构。
- **所有三角函数 = 查表 + 定点,零 IEEE 浮点参与**。精度:sin/cos ~12 位,atan2/acos/asin 6-10 位。

### LRandom(`…/src/Lockstep.Core/Math/LRandom.cs`)
- **Xorshift128+**(非 LCG/xorshift64):`ulong _state0, _state1`(:13-14)。
- NextUInt64(:83-94):标准 Vigna 算法 —— `result=s0+s1; s1^=s0; _state0=rotl(s0,24)^s1^(s1<<16); _state1=rotl(s1,37)`。
- SetSeed(:45-62):**splitmix64**(常数 0xBF58476D1CE4E5B9 / 0x94D049BB133111EB)从单 seed 扩两状态。零状态强制 `_state0=1`(:36-39,58-61)。
- **序列化 = 两 ulong**:`State => (_state0,_state1)`(:19),`RestoreState(s0,s1)`(:68)。**回滚就是存/恢复这两个 ulong,零成本**。

### SIMD(LMathSIMD.cs / Advanced/LVector2SIMD.cs)
- `BatchAdd`/`BatchSub`:`#if NET8` 用 `Sse2.LoadVector128/Add/Store`,2 个 long 一组,剩余标量兜底。**SIMD 不用于单个 LFloat 乘法(那是 MulShiftFast),只用于批量数组运算**。

---

## 2. ECS(World / ComponentPool / UnsafeComponentPool)

- **双引擎真实区别**:
  - **SafeECS = `ComponentPool<T>`**(`…/src/Lockstep.Core/ECS/ComponentPool.cs`):`where T : struct, IComponent`。平铺数组按 entityId 索引(`T[] _components` + `bool[] _active` + `List<int> _activeEntities`)。**删除 = 标记 + 延迟回收**(Remove 立即 `_active[entityId]=false` + Reset,遍历中则进 `_pendingRemovals` 队列,遍历结束 FlushPendingRemovals 才移除)。回滚安全靠 `_activeEntities` 用 BinarySearch **保序插入**,序列化按 entityId 顺序写。**不是 swap-and-pop**。
  - **UnsafeECS = `UnsafeComponentPool<T>`**(`…/src/Lockstep.Core/ECS/Unsafe/UnsafeComponentPool.cs`):`where T : unmanaged, IComponent`(更严格)。**Sparse Set** 存储(`int[] _sparse` + `int[] _dense` + `T[] _components`)。**删除 = 真正 swap-and-pop**(:133-157,把最后元素搬被删位,--count)。**序列化是裸内存 `fixed` 指针拷贝**(:159-229,`writer.WriteBytes((byte*)pDense, _count*sizeof(int))` + 组件数组整体拷贝)。**极速快照**。
  - **World 只用 SafeECS**(World.cs ComponentPoolWrapper<T> 包装 ComponentPool<T>),UnsafeComponentPool 是独立高级 API,**未接入 World.SaveState**(写书诚实标注:这是"高级逃生舱/半成品")。
- **ComponentPool 序列化三路径缓存**(:374-440):EnableCaching && !IsDirty → 吐缓存;!EnableCaching → 直写;脏 → 借 BitWriterPool 生成缓存。`HighFrequencyComponentAttribute` 标记的组件默认 EnableCaching=false(为 Transform 这类每帧必变组件省内存拷贝)。
- **World 的 System 执行顺序 = 稳定插入排序**(World.cs `SortSystemsStable`):手写插入排序,注释明说 List.Sort 非稳定会破坏确定性。System 按 Priority 升序(推荐范围:0-99 输入/100-199 物理/200-299 逻辑/300+ 清理)。
- **★SystemStateValidator 实际拦截清单(写第 24 章红线清单必读,P2-05 agent 核实)**:`[Conditional("DEBUG")]`(非 `#if DEBUG`),实际拦截的是 **Dictionary/HashSet/Hashtable、Task/Thread/Timer/CancellationToken、System.Random、World/Entity 引用缓存、Guid/Stopwatch、委托/事件、静态字段(除 static readonly 委托例外)、危险命名模式(lastframe/cachedEntity 等)**。**注意:Validator 实际不拦 `float`/`double` 字段**——锚点旧述"禁 float"是任务预期非源码实际。float/double 泄露当前无自动体检(呼应 DG-4:System + Component 层 float 体检均未做),靠人工 review。
- **World.SaveState/LoadState 格式**(World.cs:829-1115):① VersionMagic=0x4C534550("LSEP")(:824,881)② **SerializationVersion=2**(:823,当前值)③ CurrentFrame(int32)④ 随机状态 _state0+_state1(u64×2)⑤ _incrementalHash(u32)⑥ 实体管理(_nextEntityId + _generations + _activeEntities + _recycledIds)⑦ 组件池按**类型 FullName 字典序 Ordinal 排序**写。
- **加载完整性校验补丁**(World.cs:1089-1114):重算全量组件哈希与快照内嵌增量哈希比对。注释解释隐蔽 bug:ComputeHash 在 Disabled 模式 O(1) 返回 baseHash*31+_incrementalHash,而 _incrementalHash 本身从快照恢复 → 校验恒真,无法发现位翻转。LoadState 重算全量是补丁。
- **实体代数 Generation**(World.cs:129-141,270-399):_generations 每槽一代数。CreateEntity 优先从 _recycledIds 复用,DestroyEntity 时 _generations[Id]++ + 入队。注释承认 int 溢出需 21.47 亿次同槽销毁(约 25 天),不处理。
- **增量哈希 + 双轨**(World.cs:1134-1229):组件变更 XOR 到 _incrementalHash(O(1))。DualTrackMode 三档:Disabled(生产)/Periodic(每 N 帧)/FullValidation(每帧)。HashDriftRecoveryPolicy:Continue 覆盖 / Throw 抛 HashDriftException。
- Entity 是轻量 readonly struct 句柄(ECS/Entity.cs):int Id + int Generation,不存组件引用。

---

## 3. 同步核心(LockstepController / RingBuffer / Snapshot)

- **LockstepController.DoUpdate 两阶段**(Sync/LockstepController.cs:307-340):
  1. **ConfirmServerFrames**(:348-469):动态追帧限速(tickGap>100 用 100,>20 用 50,否则 20);回滚检测(:378-399,tick<=PredictedTick 且 CompareInputs 不一致 → IsReplaying=true, RollbackTo(tick-1));命中预测(:400-422);**C-6 修复**(:356,464-468,try/finally 包裹保证 IsReplaying 任意出口复位 false)。
  2. **PredictAhead**(:498-525):基于本地输入向前预测,**绝对不限速**(保证本地操作即时生效),受 _maxPredictionFrames + deadline 约束。
- MaxSimulationMsPerFrame = 50(:27,每帧最多模拟 50ms 防卡死)。
- **RollbackTo**(:623-688):tick==-1 加载 _initialState 校验 _initialStateHash;否则 FindSnapshot(:771-782,从 targetTick 往回扫最多 _frameHistorySize 帧),加载快照**立即校验**哈希==snapshot.Hash(:659-664)。
- **RingBuffer**(Sync/RingBuffer.cs):构造时 `_capacity=1; while(<capacity) <<=1; _mask=_capacity-1`(:34-38,强制 2 的幂);`GetIndex = index & _mask`(:43-57,纯位与无分支)。★**时效性契约 C-5**(:11-22):纯槽数组无 Count/头尾,越界 index 静默环绕到陈旧槽,靠调用方 `payload.Frame==tick` 自校验。默认 frameHistorySize=2000(LockstepController.cs:98)。
- **Snapshot**(Sync/Snapshot.cs):int Frame + uint Hash + byte[]? _data(BufferPool.Rent) + string? DebugState。Dispose 幂等(:54-62)。**SnapshotInterval 默认=1**(LockstepController.cs:96,112,**每帧都存快照**,性能/回滚速度权衡点)。
- **IInputPredictor 三实现**(Sync/Predictors/):LastInputPredictor(默认,0 阶保持,返回副本防污染历史帧)/NeutralInputPredictor(空输入,格斗 ACT)/TrendInputPredictor<TInput>(抽象基类,分析 t-2/t-1 算趋势,业务层必须继承实现 Deserialize/Serialize/CalculateTrend)。
- 输入掩码隔离(LockstepController.cs:574-609):本地玩家用 _localInputProvider.GetInput() 实时注入,非本地玩家严格走 _inputPredictor。
- FrameData(Sync/FrameData.cs):MaxPlayerCount=256;`[ThreadStatic] static BitReader? _sharedReader` 省 GC。序列化 [Frame:int32][PlayerCount:int32] + 每玩家 [length:int32][bytes]。

---

## 4. 序列化与池化

- **BitWriter**(Serialization/BitWriter.cs):BufferPool.Rent 默认 256;全 `BinaryPrimitives.WriteXxxLittleEndian`(**小端序**,跨平台一致);OverwriteInt32 回填长度(:79-86);WriteBytes null 写 -1(:230-241);WriteString UTF-8 两阶段(占 4 字节 length 位写完回填,:270-289);**WriteDictionarySorted 强制 Key 排序**(:397-429,租 ArrayPool 排序,仅 int/string key,保证确定性);ComputeHash FNV-1a(:215-228);DEBUG _pooledInUse 防双倍归还(:24-26)。
- **BitReader**(Serialization/BitReader.cs):PeekInt32 不移指针预读(:67-72);ReadBytes -1=null 其他负 fail-fast(:225-239);**溢出安全 P1-SEC**(:325-331,350-356,count 来自不可信流,count*4 回绕校验,防 OOM DoS);ThrowBufferOverflow [NoInlining] 保热路径内联(:77-84)。
- **BufferPool**(Pooling/BufferPool.cs):静态封装 ArrayPool<byte>.Shared。**DEBUG 用 ConditionalWeakTable<byte[], LeaseMarker> 检测双倍租出/归还**(:34-43,56-62,75-82)——★帧同步静默损坏最阴险来源(同一数组分发两方)。RentedBuffer(:121-168,readonly struct + IDisposable,支持 using)。

---

## 5. 网络/驱动层(agent2 核对)

### LockstepDriver(`…/src/Lockstep.Network/Client/Core/LockstepDriver.cs`,1082 行)
- 封装 _simulation + _client(INetworkClient) + _controller + _clock + _recorder + InputPredictor + 限流器 + 重连状态 + 跨线程命令队列。**Driver 拥有 Controller**(非反过来)。
- Update(dt) 主循环(:492-565):① 消费跨线程命令队列(:495)② 守卫(:497)③ 重连分支(:499-503)④ 在途 StateRequest 超时检测(:508-526)⑤ 帧累加器钳制爆发(:529-533)⑥ 模式分支本地/联机(:535-545)⑦ **_controller.DoUpdate(_inputTick)**(:549)⑧ OnLogicStepComplete(:553)⑨ 渲染插值(:557-563)⑩ OnUpdate(:564)。
- 输入预发送:Driver 自己不算,透传 NetworkClock。`PreSendCount => _clock?.PreSendCount ?? 2`(:62)。RTT 入口收 Pong:`ping = _client?.Ping ?? 50; _clock?.UpdateFromPong(ping, pong.ServerTimestamp)`(:695-702)。**ping 取自 INetworkClient.Ping(传输层维护),不是从 PongMessage 算**。
- 网络消息处理(:683-687):所有网络回调**先入 _commandQueue**,下一帧 Update 顶部主线程消费(帧同步线程安全要求,Controller DEBUG 有 CheckThreadAffinity)。
- 丢帧请求双层:Controller ConfirmServerFrames 发现空/帧号不符 → OnNeedMissFrame(:363-367);Driver OnNeedMissFrame(:937-950)去重+限流(MissFrameRequestRatePerSec=2)后 RequestMissFrames。**_consecutiveSyncFailures>=3 三处终止游戏**(:515,793,819)。
- 停止=Dispose(:1019-1070):幂等守卫、_ioCts.Cancel()、解绑事件、_controller.Dispose()、_client.Dispose()(Driver 接管传输层生命周期)。

### NetworkClock(`…/src/Lockstep.Network/Client/Core/NetworkClock.cs`,343 行)
- **Jacobson 真实系数**(:42-43):`RttAlpha=0.125f, RttBeta=0.25f`。`RTTVAR=(1-beta)*RTTVAR+beta*|SRTT-RTT|`(:300),`SRTT=(1-alpha)*SRTT+alpha*RTT`(:302)。**beta=0.25 不是 0.125**(RFC6298 经典)。首次样本 _srtt=rtt,_rttVar=rtt/2(:292-295)。**不算 RTO,只用 SRTT+4×RTTVAR 做 jitter buffer**。
- RTT 测量:Clock 不自测,消费 INetworkClient.Ping(传输层 Pong 回调算 now-pong.ClientTimestamp)。UpdateFromPong(:130-144):UpdateRtt(ping) + `localNow-(serverTimestamp+ping/2)` 估单向延迟(**假设对称链路 ping/2**)。
- **★硬边界 GetTargetTick**(:195-215):`elapsedMs=localNow-_gameStartTimestampMs+_clockOffsetMs`(防回拨 Max(0,...));`physicalTick=elapsedMs/_frameIntervalMs`;`smoothedTarget=physicalTick+PreSendCount`;`hardMin=serverConfirmedTick+PreSendCount`;`hardMax=serverConfirmedTick+maxPredictionFrames`;`return Clamp(smoothedTarget,hardMin,hardMax)`。**平滑只在区间内生效,触界放弃平滑**。
- **ClockOffset 三档 EWMA**(:32-37,260-282):OffsetSmoothingFactor=0.1(常规)/OffsetForceCorrectThresholdMs=150/ColdStartThreshold=5/ColdStartFactor=0.5(冷启动)/OffsetForceCorrectFactor=0.3(强修正)。主源 Pong(高精度),ServerFrame 安全网(>150ms 才介入,isForceCorrect)。
- **PreSendCount 动态**(:309-333):`targetDelayMs=SRTT/2 + 4*RTTVAR`;`targetCount=targetDelayMs/frameIntervalMs+1`;变差立即增深(:322),变好 0.98/0.02 缓慢衰减(:327);Clamp [_minPreSendCount=3, _maxPreSendCount=30]。**不对称迟滞**。PredictCountHelper.cs 是旧副本(文件头注释自承被 NetworkClock.UpdatePreSendCount 取代)。

### 服务器(LockstepServer.cs 449行 / GameRoom.cs 957行 / RoomManager.cs 200行)
- **LockstepServer**:传输层无关,持 _transport/_roomManager/_dispatcher/_logger + 有界 Channel _msgChannel(容量 2000, DropOldest)。**自身不做游戏逻辑**。MainLoopAsync(:217-268):**100Hz 调度**(tickInterval=10ms),每 tick 先消费最多 MaxMessagesPerTick=512 条消息再 _roomManager.DoUpdate()。512 是公平性预算(原版排空整个 channel,单房洪泛饿死全局)。并发:专用线程 + SingleThreadSynchronizationContext,所有 await continuation 回该线程,保证 World 线程亲和(:180-193)。
- **9 个 Handler**(LockstepServer.cs:101-114,grep 核验):Join/Leave/Input/HashReport/Ping/Reconnect/MissFrameRequest/MissFrameAck/State。MissFrameHandler.cs 含两个类(Request+Ack)。手动 new 注册非反射。
- **GameRoom**:① 玩家生命周期 ② 帧聚合广播 ③ 历史帧存储补帧 ④(可选)服务器端权威 sim + 哈希校验。输入聚合 _tickInputs:Dictionary<int,byte[][]>(:108,key=tick,value=每玩家槽)。OnPlayerInput(:747-760)只填缓存不广播,防御:tick<_currentTick 弃 / tick>_currentTick+16 弃 / input.Length>256 弃。
- **帧推进=时间驱动不等人**(DoUpdate :208-243):`while(_currentTick<=TickSinceGameStart && loops<10){BroadcastTick; _currentTick++;}`。TickSinceGameStart(:177-186)由**单调钟 Stopwatch**驱动。★**P1 修复**(:113-119):原版用 DateTimeOffset.UtcNow 算 tick,NTP 后跳让差值变负→房间永久冻结且超时检测同时失效。改 Stopwatch 免疫。默认 frameRate=20(50ms/tick=20Hz)。每帧最多 catch-up 10 帧。
- **迟到输入用 nullInput 填**(BroadcastTick :252-258):`for i: if(inputs[i]==null) inputs[i]=_nullInputFactory()`。
- **冗余帧抗丢包**(BroadcastTick :330-391):RedundancyCount(默认 2)从 _historyBuffer 取 tick-1/tick-2 塞 msg.RedundantFrames。**writer.ToArray() 复制独立 byte[] 再归还**(:379,P1-ROB-8:原用 AsMemory 传入后台被池回收读脏=静默 desync)。clientIds.ToArray() 快照(:384)。帧存环形 _historyBuffer[tick%3600](3 分钟)。
- **HashReport 防作弊**(OnHashReport :850-921):_frameHashes:Dictionary<int,uint[]>,每帧 uint[_requiredPlayers] 初值 MaxValue。**全部玩家到齐才比对**(:862-870 手写循环避 LINQ),不符广播 HashMismatchMessage。权威 sim 每 SnapshotInterval(默认 60)帧存 _snapshotHashes+_snapshots。
- **MissFrame**(GetMissFrames :803-839):startTick<_minRetainedTick→isExpired;否则 _historyBuffer[idx%cap] 取并校验 frame.Frame==idx 防错位,上限 MaxMissFramesPerRequest=600。
- **★顶号重连 token(P0-2)**(TryJoin :467-547,:496-518):原版仅同名+不同 ClientId 就重分配槽位→攻击者知玩家名即可踢人+身份劫持。加 token 校验:不匹配(含空)必拒,JoinHandler 退化为新建房间。
- **★中毒快照熔断** _serverSimFaulted(:141-149,777-780):权威 sim Tick 抛异常→World 半修改不一致→置位停用 sim + 清空旧快照 + 房间不崩溃继续转发帧,重连强制走 miss-frame。
- Ping/Pong(PingHandler :14-33):回 PongMessage{ClientTimestamp, ServerTimestamp}。**不带 ackTick/currentTick**。Ping 计算 now-pong.ClientTimestamp(墙钟差,非 NTP)。
- ReconnectHandler(ReconnectHandler.cs:14-63 + HandleReconnect :552-575):校验 playerId + token 比对 → 更新 ClientId → 回 ReconnectResponseMessage{Success,CurrentFrame} + 补发 GameStartMessage。**服务器在 ReconnectHandler 不发快照不发增量帧**,只发 CurrentTick+GameStart;快照/增量由客户端后续 RequestState/MissFrameRequest 拉取。
- RoomManager:Dictionary<int,GameRoom> _rooms,_nextRoomId 从 1 自增。DoUpdate 遍历所有房间(单房异常 try-catch 隔离)。配置 LockstepServerConfig:Port=9999,FrameRate=20,MaxRooms=100,HeartbeatTimeoutMs=5000,HistoryBufferCapacity=3600,SnapshotInterval=60,RedundancyCount=2。
- RateLimiter(RateLimiter.cs):标准 Token Bucket。**关键:服务器侧 8 个 Handler 都没用它——它是客户端侧限流重型请求的工具类**。服务器侧限流靠:① MaxMessagesPerTick=512 全局预算 ② IMessageInterceptor 拦截器(Priority 排序,BeforeHandle 返回 false 拦)。

### 传输层
- INetworkClient/IServerTransport 抽象。TransportType 枚举 Udp=0/Tcp=1/Kcp=2/WebSocket=3。
- 四客户端全 .NET BCL 无第三方 nuget:TCP(TcpClient+NetworkStream)/UDP(UdpClient)/WS(ClientWebSocket)/KCP(UdpClient+SimpleKcpCore)。
- **★KCP 是 stub**(KcpClient.cs):类注释(:51-55)"需引入 KCP 库";CreateKcpCore(:365-370)默认 new SimpleKcpCore;**SimpleKcpCore.Update(:605-608)与 SetConfig(:610-613)都是 `// 简化版无需实现` 空方法体**;Send(:570-583)只做"4 字节 conv 头 + 数据"送 UDP,无重传无滑窗;默认行为=UDP+4字节 conv 会话过滤(:590-591)。IKcpCore 接口+KcpConfig 是预留替换面。
- UDP 无可靠性无顺序(UdpClient.cs):SendAsync 直接发,ReceiveLoop 收到即 Parse 无去重重排。**可靠性靠协议层冗余历史帧**。
- 分帧两派:流式(TCP/WS)自做分帧——TCP 手写**大端序 4 字节长度前缀**(TcpClient.cs:276-284 写/:317-348 读,含 1MB 上限校验:331);WS 原生消息边界。数据报(UDP/KCP)借传输边界。
- WebSocket 默认 ws://(明文)端口 9999 路径 /ws(NetworkClientConfig.cs:16,25,49-51),useSsl=false 默认。服务器 WebSocketServerTransport 无 TLS 配置,wss 需反代。

### 断线重连
- ReconnectCredential 4 字段(ReconnectCredentialStore.cs:12-29):PlayerName/PlayerId/RoomId/ReconnectToken。**无 lastAckTick**。IsValid = token 非空 && PlayerId>=0 && RoomId>0。
- FileReconnectCredentialStore(:22-109):5 行纯文本,字符串字段 Base64。Save File.WriteAllLines,Load 校验行数+版本+IsValid。**全程 try-catch 吞异常 + lock**,损坏返回 null 降级全新加入。**默认 NullReconnectCredentialStore(空实现),必须宿主显式注入才启用进程级重连**。
- 重连双级解耦:① 传输层(TcpClient 为例)心跳超时 5000ms→Disconnect→ReconnectCoreAsync(发 ReconnectRequestMessage)。**进程重启后 ConnectAsync(:99-113):reconnectToken 空则先 Load 凭证,IsValid 走重连,失败 Clear+降级全新 Join**(对调用方透明)。② 应用层 Driver 订阅 OnReconnected,Start(isReconnect=true):413-416 `if(isReconnect) RequestMissFrames(0)`。
- **从断点续 vs 跳到现在**:服务器帧历史环形缓冲 3600 帧(3 分钟 @20fps),快照缓存上限 10。决策树(Driver :855-875):重连→RequestMissFrames(0);0 帧在窗口(<3600)→增量帧→PushServerFrames 追帧(从断点续);>3600 帧 0 帧已覆盖→isExpired→转 RequestState→拉最近快照→LoadState 跳到快照点再续追(跳到现在)。MissFrameResponse.MaxFrames=600 硬上限。

### 协议
- **MessageType 枚举**(:byte,Mes``sageType.cs:6-37,18 值,P4-16 agent 核实纠正 anchor 旧述"17"):1 JoinRequest/2 JoinResponse/3 LeaveNotify/10 GameStart/11 GameEnd(占位无实现)/20 ClientInput/21 ServerFrame/22 HashReport/23 HashMismatch/30 StateRequest/31 StateResponse/40 Ping/41 Pong/50 ReconnectRequest/51 ReconnectResponse/52 MissFrameRequest/53 MissFrameResponse/54 MissFrameAck。按十进制分段。
- **ProtocolVersion**(ProtocolVersion.cs:22-54):Major=1,Minor=1,Version=(1<<16)|1=65537,VersionString="1.1"。IsCompatible 只查 Major 相等(:49-54),Minor 不查=向前兼容。不匹配=握手拒绝(JoinResponseMessage.Success=false)。MinVersionWithJoinReconnectToken=65537。
- **SerializationVersion**(ECS/World.cs:823):当前值 **2**。V1 无版本头,V2 加版本头+组件池缓存。与 ProtocolVersion 独立(双轨)。
- FrameData(FrameData.cs:10-132):int Frame / int PlayerCount(校验 0..256) / byte[][] PlayerInputs。**无 playerId 字段**(身份由数组下标隐含)、**无 hash 字段**(hash 在 HashReportMessage 单独传)。
- ClientInputMessage(Messages.cs:157-200,C→S):Frame/PlayerId/InputData/RedundantInputs(最多 255)。ServerFrameMessage(:202-240):FrameData Frame/FrameData[] RedundantFrames。HashReportMessage(:242-280):Frame/Hash/RedundantHashes。
- 序列化手写 BitWriter(非 protobuf):每消息先 1 字节 MessageType 再子类 Serialize。整数小端序,定点数写 Int64(RawValue)。

---

## 5.5 可观测性与确定性护栏·当前状态(2026-07-01 据源码核实,非审计旧快照)

> ⚠️ **写书必读**:`FRAMEWORK_QUALITY_AUDIT.md` 是"发现问题时"的快照,项目加固期大多数 P0/P1 **已修但审计文档没更新**。写 bug 案例章(第 25 章)和可观测性章(第 22 章)时,每条必须核实当前源码,写成"发现→根因→修复→现状",绝不能写成"现在还有这 bug"。下面是已核实状态:

| 审计曾列为问题 | 当前源码真实状态 | 核实依据 |
|---|---|---|
| 35 处裸 Console.WriteLine 绕过日志抽象 | ✅ **已清理**(35→个位,且都在日志设施内部:Log.cs/ILockstepLogger.cs/ComponentPool) | grep Console.WriteLine 全 src |
| 双轨哈希"漂移即覆盖"静默吞掉 desync | ✅ **已修**:漂移触发 `OnHashDrift` 事件(无论策略)+ `HashDriftRecoveryPolicy`(Continue 默认向后兼容 / Throw 立即抛 HashDriftException)+ 详细日志(打印 Incremental/Full/Delta XOR) | World.cs:189-198, 868, 1102, 1160-1177 |
| 32 位哈希无法定位到字段 | ✅ **已支持,三级下钻**:`GetPerTypeHashes()`(类型级,:1334)+ `Diff(World)`(Entity 级,:1351)+ `GetDebugString(entityId)`(字段级,输出组件完整字段值文本)。`:1165` TODO 仅指"漂移点自动接入 Diff 落盘",**能力本身已完整** | World.cs:1334, 1351-1416 |
| Core Log(static) vs Network ILockstepLogger 两套 | ⚠️ **有意保留**(零依赖:Core 不能引用 Network 接口)+ 提供桥接(Log.Sink 委托签名 `(LogLevel,string)` 便于 ILockstepLogger 桥接)。设计合理,非债 | Log.cs:30-72 |
| 客户端默认 NullLogger 生产静默 | ✅ **已修**:客户端默认 ConsoleLogger(错误默认可见);Core Log 默认 ConsoleSink。服务器仍 NullLogger(合理) | NetworkClientConfig.cs:54, Log.cs:38, LockstepServerBuilder.cs:251 |
| World lock/[ThreadStatic] 假线程安全 | ✅ **已修**:移除 lock 与 [ThreadStatic],改纯单线程模型(World.cs:1208 注释"原 [ThreadStatic] 在单线程 World 里是误导") | grep World.cs 无 lock/( |
| KCP 是 stub | ❌ **未变**(KcpClient.cs:605-613 仍空方法体。可能有意——冗余帧够用,KCP 预留) | KcpClient.cs:605-613 |

**结论**:审计报告的最大 P0(可观测性地基)**当前已基本就位**,不是"地基缺失"。剩余真实债轻:① Diff 漂移点自动接入(TODO:1165);② KCP 真实现;③ 巨型方法/E2E(待核实)。

---

## 6. 写书时易翻车的坑

1. **LUTSinCos.cs 8222 行**:Read 必 offset 分页,否则截断。
2. **Handler 9 非 7、版本 1.1 非 1.0、RTTVAR beta 0.25 非 0.125**:文档全错,以源码为准。
3. **KCP 是 stub**:不能写成"已实现 KCP ARQ"。
4. **`LFloat.One` 现在是 long**:README/issues 说的"int"是历史 bug,已修。但语义陷阱(One 不是 LFloat)仍在。
5. **GameEnd 占位**:有枚举无实现。
6. **行号会漂移**:项目加固期,引用前重 Grep/Read。注释里 P0/P1/A-X/C-X 是修复批次编号,对应 issues_found.md。
7. **UnsafeECS 未接入 World.SaveState**:写"双引擎"时诚实标注这是高级逃生舱。
8. **IsReplaying/IsPredicting 复位路径(C-6)**:已修 bug,不能只看 happy path。
