# 第 5 篇 · 第 19 章 · 安全:TLS / mTLS / ALTS / tsi

> **核心问题**:一条 gRPC 连接从客户端穿到服务端,中间可能是公网、可能是不可信的机房网络——怎么保证字节不被窃听、不被篡改、对端真的是它声称的那个?更深一层:gRPC 要跑在公网(用 TLS)、跑在 Google 内部(用 ALTS)、跑在同一台机器(用 local 免加密),**底层安全机制完全不同**,上层怎么不被这些差异淹没?还有,认证"机器"(这台服务器是合法的)和认证"调用者"(这次调用是哪个用户 / 应用发的)是两件事,gRPC 怎么把它们分开管、又能叠加用?这一章拆 gRPC 的 tsi 抽象、ALTS、以及 channel creds vs call creds 两层分离的设计。

> **读完本章你会明白**:
> 1. 为什么 gRPC 要造一个 tsi(Transport Security Interface)抽象层,把 SSL/ALTS/local/fake 这些底层安全后端统一起来;tsi 的 handshaker / frame_protector / peer 三件套各管什么。
> 2. ALTS(Application Layer Transport Security)是什么——Google 内部用的 mTLS 替代品,凭什么基于"服务账号"而非"证书"做认证,为什么它比 TLS 更适合数据中心内部。
> 3. 连接建立后,握手(handshake)是怎么编进 gRPC 的通用 handshaker 框架的——先握手再传业务字节,握手完用 frame protector 加解密。
> 4. ★为什么 channel creds(连接级 TLS/mTLS)和 call creds(调用级 OAuth/JWT)必须分两层,又能用 composite creds 叠加;这背后"机器身份 vs 调用者身份"的精巧切分。

> **如果一读觉得太难**:先只记住三件事——① tsi 是个抽象接口,屏蔽 SSL/ALTS/local 等底层,上层只面对统一的 handshaker;② channel creds 管连接级(认证机器,整条连接复用),call creds 管调用级(认证人/应用,每次调用注入 metadata);③ 两者能用 composite 叠加(mTLS 认机器 + OAuth 认人),这是生产里最常见的搭配。

---

## 〇、一句话点破

> **gRPC 的安全设计有两根支柱:tsi 抽象层把"底层是 SSL 还是 ALTS 还是 local"的差异藏起来,上层只面对统一的 handshaker 接口;channel/call creds 两层分离把"认证机器"(整条连接一次)和"认证调用者"(每次调用一次)切开,各自独立、又能 composite 叠加。前者解决"可插拔",后者解决"两种身份不混层"。**

这是结论,不是理由。本章倒过来拆:先讲清跨网络通信要解决的三件事(加密、完整、认证)和 tsi 怎么统一抽象它们;再讲 SSL/TLS、ALTS、local 三种后端各凭什么;然后讲握手怎么编进连接建立;最后技巧精解拆 channel/call creds 分层的根因。

> **本章范围说明**:TLS 握手的协议细节(ClientHello / 证书链验证 / cipher suite 协商)本章不深挖——那是 SSL/TLS 协议本身的事,有 RFC 8446(TLS 1.3)和大量专著。本章聚焦 gRPC 自己的设计:**tsi 抽象、ALTS 的特殊之处、creds 分层**——这些是"gRPC 凭什么这么设计"的部分,不是 SSL 教科书。

---

## 一、跨网络要解决的三件事

跨网络发字节,你不加保护时会面对三个威胁:

1. **窃听**:中间人(运营商、攻击者、不可信的机房)读到你的字节。RPC 调用里的请求参数、响应数据、认证 token 全是明文,泄露。
2. **篡改**:中间人改你的字节。把请求里的金额从 100 改成 10000,把响应里的余额改小,而两端还以为通信正常。
3. **冒充**:攻击者伪装成服务端(骗客户端的 token),或者伪装成客户端(骗服务端接活 / 越权)。

对应的三个防御:

| 威胁 | 防御 | tsi 概念 |
|------|------|------|
| 窃听 | **加密**:字节变成密文,中间人读到也看不懂 | privacy |
| 篡改 | **完整性**:每帧带 MAC / AEAD 标签,改一个 bit 就验不过 | integrity |
| 冒充 | **认证**:握手时验证对端身份(证书 / 服务账号) | peer(对端属性) |

注意 tsi 的 `tsi_security_level` 枚举([`transport_security_interface.h:53-59`](../grpc/src/core/tsi/transport_security_interface.h#L53-L59))就对应这三个层次:

```c
typedef enum {
  TSI_SECURITY_NONE,           // 不防护(明文)
  TSI_INTEGRITY_ONLY,          // 只防篡改,不防窃听
  TSI_PRIVACY_AND_INTEGRITY,   // 既加密又防篡改
} tsi_security_level;
```

`INTEGRITY_ONLY` 这档(只防篡改不加密)看似奇怪,实则有场景:数据中心内部已经物理隔离(没人能窃听),但仍要防"一个进程冒充另一个进程发伪造数据"——这时只验完整性就够,省掉加密的 CPU。这是 tsi 把"安全等级"显式建模的根。

> **钉死这件事**:跨网络安全的本质是三件事——加密(防窃听)、完整性(防篡改)、认证(防冒充)。tsi 把这三件事统一抽象成 handshaker(协商密钥 + 认证对端) + frame_protector(用协商出的密钥加解密每帧) + peer(对端的身份属性)三件套。这套抽象不预设具体用什么协议实现,所以 SSL、ALTS、local 都能塞进来。

---

## 二、tsi 抽象:屏蔽底层安全后端

### 为什么需要抽象

gRPC 要跑在多种环境,每种环境用的安全机制不一样:

- **公网 / 跨数据中心**:用标准 TLS(SSL)。证书由 CA 签发,握手时验证证书链。
- **Google 内部**:用 ALTS(Application Layer Transport Security)。基于服务账号(service identity)而非证书,握手靠一个独立的 ALTS handshaker 服务。
- **同一台机器(unix socket / loopback)**:用 local。同机进程间通信,内核已经保证只有本机进程能连,不需要加密,只验证 process_id。
- **测试 / 不安全的开发环境**:用 fake。完全不加密,只占位。

如果上层(channel、call、HTTP/2 transport)直接面对这四种实现,代码会变成满地的 `if (ssl) ... else if (alts) ...`,每加一种新机制(比如未来的量子安全协议)就要改一堆地方。这是个典型的"**可插拔**"需求——抽象一个统一接口,让上层只面对接口,底层实现自由替换。

> **不这样会怎样**:如果没有抽象层,gRPC 的安全代码会和具体协议(TLS / ALTS)深度耦合。想换 TLS 库(从 OpenSSL 换 BoringSSL)、想加新协议(后量子 KEM)、想支持新环境,都得改上层。抽象层让上层"不知道底层是啥",各后端独立演进——这是软件工程里"依赖倒置"在安全模块上的标准应用。

### tsi 的三件套

tsi(Transport Security Interface)定义在 [`src/core/tsi/transport_security_interface.h`](../grpc/src/core/tsi/transport_security_interface.h)。核心是三个抽象对象:

**1. `tsi_handshaker`:握手器。** 负责连接刚建立时的握手——双方协商出加密密钥、互相认证身份。接口核心是 `tsi_handshaker_next`([`transport_security_interface.h:494-501`](../grpc/src/core/tsi/transport_security_interface.h#L494-L501)):

```c
tsi_result tsi_handshaker_next(
    tsi_handshaker* self,
    const unsigned char* received_bytes, size_t received_bytes_size,  // 从对端收到的字节
    const unsigned char** bytes_to_send, size_t* bytes_to_send_size,  // 要发给对端的字节
    tsi_handshaker_result** handshaker_result,                        // 握手结果(完成时非 NULL)
    tsi_handshaker_on_next_done_cb cb, void* user_data,               // 异步回调
    std::string* error);
```

这个函数是**握手状态机的单步推进器**:你喂给它"从对端刚收到的字节"(比如 TLS 的 ServerHello + Certificate),它吐出"要发给对端的字节"(比如 ClientKeyExchange),以及一个状态(`TSI_INCOMPLETE_DATA` 表示还要更多字节、`TSI_OK` + result 表示握手完成、`TSI_ASYNC` 表示异步还在算)。握手就是反复调它,直到 result 非 NULL。

**2. `tsi_frame_protector`:帧保护器。** 握手完成后,用 `tsi_handshaker_result_create_frame_protector` 从握手结果造出来。它提供 `protect`(明文 → 密文帧)和 `unprotect`(密文帧 → 明文)两个核心操作([`transport_security_interface.h:167-215`](../grpc/src/core/tsi/transport_security_interface.h#L167-L215))。**握手之后,这条连接上所有业务字节都经过它加解密**。

**3. `tsi_peer`:对端属性。** 握手完成后,`tsi_handshaker_result_extract_peer` 抽取出对端的身份属性(证书里的 CN、SAN、service account 等)——上层用这些属性做授权决策("只接受 service X 的连接")。

这三件套的关系是:**handshaker 用一次(握完手就丢)→ 产出 handshaker_result → 从 result 抽出 peer(认证)+ 创建 frame_protector(后续加解密)**。后续连接的生命周期里,handshaker 不再参与,只剩 frame_protector 在每帧上加解密。

```
  连接生命周期里 tsi 三件套的参与
  ┌────────────────────────────────────────────────────────────┐
  │ 阶段1: 建连(TCP 三次握手)                                  │
  ├────────────────────────────────────────────────────────────┤
  │ 阶段2: 安全握手(handshaker 主场)                          │
  │   while (没握完):                                          │
  │     收对端字节 → tsi_handshaker_next → 发对端字节           │
  │   握完 → handshaker_result                                 │
  │        ├→ extract_peer  → 上层认证对端                      │
  │        └→ create_frame_protector → 后续用                   │
  ├────────────────────────────────────────────────────────────┤
  │ 阶段3: 业务传输(frame_protector 主场)                     │
  │   每发一帧: protect(明文) → 密文过线                       │
  │   每收一帧: 密文 → unprotect → 明文                         │
  └────────────────────────────────────────────────────────────┘
```

> **钉死这件事**:tsi 的三件套分工——handshaker 管一次性的握手(认证 + 密钥协商),frame_protector 管持续的字节加解密,peer 是认证结果供上层授权。这三件套把"安全连接"这件事拆成了**握手期**和**传输期**两个清晰的阶段,以及认证、加密、完整性三个正交的维度。SSL、ALTS、local 三种后端都按这套抽象实现,上层代码完全一致。

### 各种后端怎么填这套抽象

`src/core/tsi/` 下每种后端一个目录:

- **SSL/TLS**(`ssl_transport_security.cc`):最常见。client 端 `tsi_ssl_client_handshaker_factory_create_handshaker`([`ssl_transport_security.h:256`](../grpc/src/core/tsi/ssl_transport_security.h#L256)),server 端 `tsi_ssl_server_handshaker_factory_create_handshaker`。底层用 BoringSSL(Google 维护的 OpenSSL 分支)。握手就是标准 TLS 1.2 / 1.3,frame protector 用 TLS record layer 的 AEAD 加解密。
- **ALTS**(`alts/`):Google 内部用,下节详述。
- **local**(`local_transport_security.cc`):不加密,只验证 process_id。注释直接说([`local_transport_security.h:39-43`](../grpc/src/core/tsi/local_transport_security.h#L39-L43)):"This handshaker is also being used as a minimalist handshaker for insecure security connector"——local 实际上是个"占位握手器",握手流程跑一下(为了和其他后端接口一致),但不真的加解密。
- **fake**(`fake_transport_security.cc`):测试用,完全不安全,只占位。

这四种后端都实现同一套 `tsi_handshaker` 接口,上层 `SecurityHandshaker` 不关心具体是哪种——这就是抽象的价值。

---

## 三、ALTS:Google 内部为什么不用 TLS

ALTS(Application Layer Transport Security)是这套抽象里最特别的一个后端,值得单独讲。它是 Google 内部用的安全机制,解决一个 TLS 在超大规模数据中心里不擅长的问题。

### TLS 在数据中心内部的痛点

标准 TLS 在公网几乎完美,但在 Google 这种"几百万台机器、每秒上百亿次内部 RPC"的数据中心内部,有几个痛点:

1. **证书管理爆炸**:TLS 要给每台机器 / 每个服务发证书,证书有过期、要轮换、要 CRL/OCSP 验证。在百万台机器规模下,证书分发、轮换、撤销的运维负担极重。
2. **握手开销**:TLS 握手(尤其 1.2)是多 RTT 的,即便 1.3 也要 1 RTT。数据中心内部 RPC 极其频繁、连接极多,握手开销累积可观。
3. **机器身份 vs 证书身份不匹配**:数据中心内部真正想认证的是"**这是哪个服务账号(service account)在发**"(比如 `payments-prod@goog`,而不是"这是哪台机器"。TLS 证书的 CN/SAN 字段虽然也能塞 service account,但语义不自然——证书的本职是认证"主机/域名"。

### ALTS 的做法

ALTS 的核心思路:**直接在协议层用"服务账号"作为身份**,不走证书体系。它的握手不是两端直接协商(像 TLS),而是**通过一个独立的 ALTS handshaker 服务**做中介——两端都信任这个 handshaker,handshaker 验证双方的 service account,然后给两端下发一个会话密钥。

协议定义在 [`src/proto/grpc/gcp/handshaker.proto`](../grpc/src/proto/grpc/gcp/handshaker.proto):

```protobuf
enum HandshakeProtocol {
  HANDSHAKE_PROTOCOL_UNSPECIFIED = 0;
  TLS = 1;
  ALTS = 2;   // ALTS 是其中一种握手协议
}

message Identity {
  oneof identity_oneof {
    ServiceAccount service_account = 1;   // 服务账号(核心身份)
    Hostname hostname = 2;
  }
}
```

注意 `Identity` 里 `service_account` 是一等身份——这正是 ALTS "认证服务账号而非机器"的体现。`altscontext.proto` 里的 `AltsContext` 记录握手后的安全上下文(peer 的 service account、使用的算法等),应用层能从 auth context 拿到。

ALTS 的优势在数据中心内部很显著:

- **不用证书**:service account 由 Google 的基础设施统一管理(不是 PKI 证书),没有证书过期 / 轮换 / 撤销的运维。
- **身份语义自然**:"这是 `payments-prod`" 直接表达,不用塞进 CN 字段。
- **更适合超大规模**:handshaker 服务可水平扩展,握手集中处理。

> **不这样会怎样**:如果 Google 内部硬用 TLS,光证书管理就是一场噩梦——百万台机器每台一个证书,每天换一大批(证书有效期短是安全最佳实践),CRL 流量、OCSP 查询、证书分发,都会成为基础设施的沉重负担。ALTS 把"数据中心内部安全"重新设计,代价是它绑死在 Google 的基础设施上(那个 handshaker 服务是 Google 内部的)。这就是为什么 ALTS 出了 Google 几乎没人用——它不是个通用方案,而是为超大规模数据中心内部 RPC 量身定的。

> **钉死这件事**:ALTS 是个"**为特定环境重新设计**"的范例。TLS 是通用方案(任何环境都能用),但在 Google 这种极端规模下,通用方案的运维成本反而成了主成本。ALTS 用"放弃通用性换运维简洁"的取舍,换来了数据中心内部的高效安全。tsi 抽象让 gRPC 能同时支持 TLS(公网)和 ALTS(Google 内部),上层代码无感切换——这就是抽象的价值在真实场景里的体现。gRPC 的 `google_default` credentials 会**自动选**:在 Google 内部(检测到 ALTS 可用)用 ALTS,在外部用 TLS。

---

## 四、握手怎么编进连接建立

讲完抽象和后端,现在看握手是怎么接入 gRPC 的连接建立流程的。这关系到 tsi 在整个 gRPC 架构里的位置。

### 通用 handshaker 框架

gRPC 有一个通用的 handshaker 框架([`src/core/handshaker/`](../grpc/src/core/handshaker/)),不只服务于安全。任何"连接建立后、传业务字节前要做的事"都可以做成一个 handshaker——比如 HTTP CONNECT 代理协商(`http_connect/`)、TCP 层的连接信息交换(`tcp_connect/`)、还有安全握手(`security/`)。这些 handshaker 通过 `handshaker_registry` 注册,连接建立时按顺序跑一遍。

安全握手的具体实现是 `SecurityHandshaker`([`src/core/handshaker/security/security_handshaker.cc:74-108`](../grpc/src/core/handshaker/security/security_handshaker.cc#L74-L108))。它是个适配器,把 tsi 的 `tsi_handshaker` 接口适配进 gRPC 的通用 `Handshaker` 接口:

```cpp
class SecurityHandshaker : public Handshaker {
 public:
  SecurityHandshaker(tsi_handshaker* handshaker,
                     grpc_security_connector* connector,
                     const ChannelArgs& args);
  void DoHandshake(HandshakerArgs* args,
                   absl::AnyInvocable<void(absl::Status)> on_handshake_done) override;
  void Shutdown(absl::Status error) override;
 private:
  grpc_error_handle DoHandshakerNextLocked(...);
  grpc_error_handle OnHandshakeNextDoneLocked(...);
  void HandshakeFailedLocked(absl::Status error);
  grpc_error_handle CheckPeerLocked();
  // ...
};
```

它的 `DoHandshake` 启动握手流程,内部反复调 `tsi_handshaker_next`,通过 `OnHandshakeDataReceivedFromPeerFn` / `OnHandshakeDataSentToPeerFn` 这些回调处理字节收发。握手完成后:

1. `CheckPeerLocked()` 调 `tsi_handshaker_result_extract_peer` 拿到对端属性,交给 `security_connector` 验证(证书链是否可信、peer 是否符合预期)。
2. 用 `tsi_handshaker_result_create_frame_protector` 创建 frame protector。
3. **用 `secure_endpoint` 包装底层 endpoint**(`src/core/handshaker/security/secure_endpoint.cc`)——此后这条连接上所有的读写都经过 frame protector 加解密。
4. 调 `on_handshake_done`,把包装好的 endpoint 交给上层(进入 HTTP/2 transport 阶段)。

### ALPN:TLS 里的应用层协议协商

顺带讲一个握手期的细节:ALPN(Application-Layer Protocol Negotiation)。当用 TLS 时,握手期双方要协商"这条 TLS 连接里跑什么应用协议"。gRPC 的 HTTP/2 只支持一个 ALPN 标识:`h2`([`src/core/ext/transport/chttp2/alpn/alpn.cc:27`](../grpc/src/core/ext/transport/chttp2/alpn/alpn.cc#L27)):

```cpp
// in order of preference
static const char* const supported_versions[] = {"h2"};
```

客户端在 TLS ClientHello 里带 `h2`,服务端如果也支持 h2 就在 ServerHello 里回 h2,握手完双方都知道这是 HTTP/2。这是个 TLS 扩展,避免"握手完了再发现协议不一致"的尴尬。

> **钉死这件事**:gRPC 的安全不是悬空的——它通过通用 handshaker 框架,**织进了连接建立的标准流程**:TCP 握手 → (可能的)代理 handshaker → **安全 handshaker** → HTTP/2 transport。安全 handshaker 跑在 HTTP/2 之前,所以 HTTP/2 的所有字节(SETTINGS、HEADERS、DATA)都已经是被加密保护的。这套"先握手、后业务"的顺序,是所有"加密 + 应用协议"设计的基石(HTTPS、HTTP/3 都是这个套路)。

---

## 五、channel creds vs call creds:两层身份

现在到本章最关键的设计——**两种 creds(凭证)的分层**。这是 gRPC 安全设计最容易被忽视、却最核心的洞察。

### 两种身份:机器 vs 调用者

跨网络 RPC 里,需要认证的身份其实是**两种**:

1. **机器 / 服务身份**:这条连接的对端是哪台机器、哪个服务?这条 TLS 连接建好之后,身份是**连接级**的、对所有在这条连接上的调用共享。
2. **调用者身份**:这一次具体的 RPC 调用,是哪个用户 / 应用发的?不同的调用(即便在同一连接上)可能来自不同的用户,**身份是调用级的**、每次调用各自带。

举个具体例子:你用一个 gRPC channel 连到订单服务(这个 channel 用 mTLS 认证了订单服务这个"机器")。然后,你通过这个 channel 发了三次调用:

- 调用 A:用户 Alice 查她的订单。
- 调用 B:用户 Bob 查他的订单。
- 调用 C:一个内部 cron job 在批量补数据。

这三次调用,**机器身份都是同一个**(都通过同一条 mTLS 连接、都认证了"订单服务")。但**调用者身份各不相同**(Alice / Bob / cron job),每个调用需要带各自的 OAuth token,订单服务根据 token 决定能看哪个用户的订单。

> **不这样会怎样**:如果把两种身份混在一层(比如"每次调用都重做一次完整 TLS 握手 + 用户认证"),会撞两堵墙:① **性能墙**——每次调用重新 TLS 握手是多 RTT,海量调用下握手开销爆炸;而且本来 HTTP/2 多路复用省下的连接开销,被"每调用一握手"全赔回去。② **语义墙**——机器身份和用户身份的生命周期不同:机器身份是"这条连接的属性",只要连接在就稳定;用户身份是"这次调用的属性",每次调用可能不同。混在一起,两者都被对方的生命周期绑架。

### gRPC 的解法:两类 creds

gRPC 把这两种身份切成**两类 creds**(凭证):

**1. `grpc_channel_credentials`(channel creds,连接级)。** 定义在 [`src/core/credentials/transport/transport_credentials.h`](../grpc/src/core/credentials/transport/transport_credentials.h)。它管"**整条 channel 的连接安全**":

- 典型实现:TLS / mTLS、ALTS、local、insecure。
- 生命周期:channel 级——channel 创建时配置,这条 channel 上所有 SubChannel、所有调用共享。
- 核心方法:`create_security_connector`([`transport_credentials.h:70-73`](../grpc/src/core/credentials/transport/transport_credentials.h#L70-L73))——创建一个 `grpc_channel_security_connector`,后者负责具体的握手、证书验证。

```cpp
struct grpc_channel_credentials : grpc_core::RefCounted<grpc_channel_credentials> {
  virtual grpc_core::RefCountedPtr<grpc_channel_security_connector>
  create_security_connector(
      grpc_core::RefCountedPtr<grpc_call_credentials> call_creds,  // ★ 接受 call creds 参数
      const char* target, grpc_core::ChannelArgs* args) = 0;
  // ...
};
```

**注意那个 `call_creds` 参数**——channel creds 创建 security_connector 时可以接受一个 call creds 一起带进去。这就是两类 creds 叠加的接口入口。

**2. `grpc_call_credentials`(call creds,调用级)。** 定义在 [`src/core/credentials/call/call_credentials.h`](../grpc/src/core/credentials/call/call_credentials.h)。它管"**每次调用注入的认证 metadata**":

- 典型实现:OAuth 2.0 token(`oauth2/`)、JWT(`jwt/`)、IAM(`iam/`)、自定义 plugin(`plugin/`)。
- 生命周期:调用级——每次 RPC 调用时,异步地把认证 metadata(如 `authorization: Bearer <token>`)注入这次调用的 initial metadata。
- 核心方法:`GetRequestMetadata`([`call_credentials.h:120-123`](../grpc/src/core/credentials/call/call_credentials.h#L120-L123)):

```cpp
virtual grpc_core::ArenaPromise<
    absl::StatusOr<grpc_core::ClientMetadataHandle>>
GetRequestMetadata(grpc_core::ClientMetadataHandle initial_metadata,
                   const GetRequestMetadataArgs* args) = 0;
```

——返回一个 Promise,resolve 时给出"加好认证 metadata 的 initial metadata"。这是个**异步**过程(OAuth token 可能要刷新、JWT 可能要现签),所以用 Promise 表达。每次调用发起时,client_call 会先 await 这个 Promise,拿到带 token 的 metadata,再发出去。

> **钉死这件事**:两类 creds 的切分,本质是"**两种身份的生命周期不同,所以分开管**"。channel creds 是连接级的、握一次手长期复用(TLS 加密 + 机器认证);call creds 是调用级的、每次调用现取 token(用户 / 应用认证)。这个切分让两类身份各自优化:channel creds 享受 HTTP/2 多路复用的连接复用红利(不每调用握手),call creds 享受每次调用的灵活性(不同用户、不同权限)。混在一起,两边都享受不到。

### composite creds:两层叠加

光切开还不够——生产场景里你常常**同时需要两种**:既要 mTLS 认机器,又要 OAuth 认人。gRPC 的解法是 composite credentials。实现是 `grpc_composite_channel_credentials`([`src/core/credentials/transport/composite/composite_channel_credentials.h:42-87`](../grpc/src/core/credentials/transport/composite/composite_channel_credentials.h#L42-L87)):

```cpp
class grpc_composite_channel_credentials : public grpc_channel_credentials {
 public:
  grpc_composite_channel_credentials(
      grpc_core::RefCountedPtr<grpc_channel_credentials> channel_creds,
      grpc_core::RefCountedPtr<grpc_call_credentials> call_creds)
      : inner_creds_(std::move(channel_creds)),
        call_creds_(std::move(call_creds)) {}
  // ...
  grpc_core::RefCountedPtr<grpc_channel_security_connector>
  create_security_connector(
      grpc_core::RefCountedPtr<grpc_call_credentials> call_creds,
      const char* target, grpc_core::ChannelArgs* args) override;
 private:
  grpc_core::RefCountedPtr<grpc_channel_credentials> inner_creds_;  // TLS / ALTS
  grpc_core::RefCountedPtr<grpc_call_credentials> call_creds_;       // OAuth / JWT
};
```

它内部同时持有一个 channel creds(TLS / ALTS)+ 一个 call creds(OAuth / JWT)。用户这样用:

```cpp
auto channel_creds = grpc::SslCredentials(opts);            // TLS 认机器
auto call_creds   = grpc::GoogleOAuth2Credentials(...);    // OAuth 认人
auto composite    = grpc::CompositeChannelCredentials(channel_creds, call_creds);
auto channel = grpc::CreateChannel(target, composite);
```

`create_security_connector` 被调时,composite 会把自己的 `call_creds_` 和外面传进来的 `call_creds` 参数**再组合**(call creds 也能 composite,多个 call creds 串起来),最终一起传给底层 `inner_creds_->create_security_connector(...)`。底层 security_connector 拿到这俩,知道:

- 用 inner_creds(TLS)做连接级安全。
- 用 call_creds(OAuth)在每次调用时把 token 注入 metadata。

> **不这样会怎样**:如果没有 composite,你想"TLS + OAuth"就得自己写一个"又管连接又管调用"的复合 creds,本质上把两类身份的复杂性又揉回一起。composite 让你**像搭积木一样**组合 creds:TLS + OAuth、TLS + JWT、ALTS + IAM、TLS + OAuth + 自定义 plugin……任意叠加,底层机制统一。这是关注点分离在凭证管理上的落地。

### 安全等级的最低保证

`grpc_call_credentials` 有个 `min_security_level_`([`call_credentials.h:114-116`](../grpc/src/core/credentials/call/credentials.h#L114-L116)),默认是 `GRPC_PRIVACY_AND_INTEGRITY`(最高)。它的作用是:**某些 call creds(OAuth token)绝不能在不安全的连接上传**——否则 token 会被窃听。如果 channel 用了 insecure creds(`TSI_SECURITY_NONE`),而 call creds 要求 `PRIVACY_AND_INTEGRITY`,gRPC 会拒绝发这个调用。这是个安全护栏,防止"配错了导致 token 泄露"。

---

## 六、技巧精解:channel/call creds 分层的根因

这一节单独拆透本章最硬的设计——**为什么 channel creds 和 call creds 必须分两层,而不能合成一个"超级 creds"**。这是分布式系统身份管理里一个反复出现的智慧,值得深挖。

### 朴素方案:一个 creds 全管会撞什么墙

假设你是个新框架设计者,面对"既要认证机器又要认证用户",最朴素的方案是:

> **定义一个 `Credentials` 类,既管连接握手(机器认证)又管每次调用的 token(用户认证),一把抓。**

听起来简洁——一个抽象,一次配置。撞上去试试:

**墙一:生命周期冲突。** 机器认证是连接级的——一条 TLS 连接握一次手,只要连接在,认证就有效(直到证书过期 / 连接断)。用户认证是调用级的——每个调用可能来自不同用户,token 可能随时过期刷新。一个 creds 同时管两种生命周期,内部状态机极其复杂:握手状态是"长期稳定"的,token 状态是"频繁变化"的,两者步调完全不一致。结果是要么握手状态被 token 刷新逻辑误改,要么 token 刷新被握手状态卡住。

**墙二:连接复用被绑架。** HTTP/2 多路复用(P2-05)的核心红利是:一条连接跑海量调用、握一次手。如果 creds 把"机器认证"和"用户认证"绑在一起,那"换用户"就得"换连接"——因为一个 creds 实例 = 一个连接 = 一组绑定身份。于是 1000 个用户调同一个后端,你得开 1000 条 TLS 连接(每条绑一个用户身份),多路复用红利全废。**机器身份应该是"这条连接的属性",1000 个用户共享一条 TLS 连接、各自带自己的 token**——这只有把两种身份切开才能做到。

**墙三:跨场景复用被堵死。** 有些场景只要机器认证不要用户认证(服务到服务的内部调用,mTLS 就够);有些场景只要用户认证不要机器认证(同一台机器 unix socket 通信,local creds 认机器、OAuth 认人)。一个超级 creds 把两者焊死,要么"过度安全"(内部调用也强制带用户 token),要么"不足安全"(为了灵活放弃机器认证)。分开管,才能按需组合。

**墙四:演进困难。** TLS 在演进(1.2 → 1.3 → 后量子),OAuth 也在演进(OAuth 2.0 → 2.1 / DPoP)。两类机制各自演化,如果焊在一起,改一边可能弄坏另一边。分开,各自独立演进、独立测试。

### gRPC 的解法:两层 + composite

gRPC 的设计精确破解了这四堵墙:

1. **生命周期分离**:channel creds(`RefCounted`,channel 级)握一次手长期复用;call creds(`DualRefCounted`,调用级)每次调用异步 `GetRequestMetadata`。两者状态机完全独立,互不干扰。破解**墙一**。
2. **连接复用保住**:一条 TLS 连接握一次手(channel creds 完事),上面跑的每个调用各自注入自己的 token(call creds)。1000 个用户共享一条 TLS 连接,各自带 token。破解**墙二**——这是 HTTP/2 多路复用红利能落到安全 RPC 上的根。
3. **按需组合**:channel creds 可选 TLS / mTLS / ALTS / local / insecure,call creds 可选 OAuth / JWT / IAM / plugin,任意 composite。破解**墙三**。
4. **独立演进**:TLS 升 1.3 不影响 OAuth;OAuth 升 DPoP 不影响 TLS。破解**墙四**。

| 维度 | channel creds | call creds |
|------|------|------|
| 认证对象 | 机器 / 服务 | 用户 / 应用 |
| 生命周期 | channel 级(连接共享) | 调用级(每次新取) |
| 典型实现 | TLS / mTLS / ALTS / local | OAuth / JWT / IAM / plugin |
| 接口核心 | `create_security_connector` | `GetRequestMetadata` (Promise) |
| 开销 | 握手一次性 | 每调用一次(轻量,注入 metadata) |
| 失败影响 | 整条连接不可用 | 这次调用失败 |

这张表就是两类 creds 的全部设计意图——**两种身份、两种生命周期、两种开销模型,分开管、能叠加**。

> **钉死这件事**:channel/call creds 的分层,本质是"**两种生命周期不同的东西,绝不能焊在一起**"这条工程智慧的落地。它在很多地方都有回响:Linux 里"内存的页映射(进程级)vs 文件的页缓存(系统级)"分开;数据库里"连接的认证(连接级)vs 事务的隔离(事务级)"分开;Kubernetes 里"节点的身份(节点级)vs Pod 的 ServiceAccount(Pod 级)"分开。gRPC 把这条智慧用在了 RPC 安全上:**机器身份属于连接,用户身份属于调用,各归各位**。

---

## 七、配置实战:几种典型搭配

把原理落到生产配置上。下面是几种常见组合:

**场景一:公网 + mTLS 双向认证(零信任网络)**。client 和 server 互相验证书:

```cpp
// server: 要求并验证 client 证书
grpc::SslServerCredentialsOptions opts(
    GRPC_SSL_REQUEST_AND_REQUIRE_CLIENT_CERTIFICATE_AND_VERIFY);
opts.pem_root_certs = client_ca_pem;        // 信任的 client CA
opts.pem_key_cert_pairs = {{server_key, server_cert}};
auto server_creds = grpc::SslServerCredentials(opts);

// client: 验证 server 证书 + 出示自己的 client 证书
grpc::SslCredentialsOptions c_opts;
c_opts.pem_root_certs = server_ca_pem;
c_opts.pem_private_key = client_key;
c_opts.pem_cert_chain = client_cert;
auto channel_creds = grpc::SslCredentials(c_opts);
```

**场景二:mTLS + OAuth(最常见的服务网格 / 微服务)**。机器用 mTLS、调用用 OAuth:

```cpp
auto channel_creds = grpc::SslCredentials(c_opts);          // mTLS
auto call_creds   = grpc::GoogleOAuth2Credentials(...);    // OAuth
auto composite    = grpc::CompositeChannelCredentials(channel_creds, call_creds);
auto channel = grpc::CreateChannel(target, composite);
```

**场景三:同机 unix socket(免加密,local)**。loopback / unix domain socket,内核已保证只有本机进程能连,不需要加密:

```cpp
auto channel = grpc::CreateChannel("unix:///tmp/foo.sock", grpc::LocalCredentials(LOCAL));
```

`LocalCredentials` 走的是 tsi 的 local handshaker——只验证 process_id、不加密。这在同机服务间通信(比如 sidecar ↔ app)很常见,省 CPU。

**场景四:Google Cloud 内部(google_default,自动选 ALTS 或 TLS)**:

```cpp
auto channel_creds = grpc::GoogleDefaultCredentials();   // 自动:内部用 ALTS,外部用 TLS
```

`google_default` creds(`src/core/credentials/transport/google_default/`)会探测环境:在 GCE / GKE / Bare Metal Solution 里检测到 ALTS 可用就用 ALTS,否则回退到 TLS + Compute Engine metadata 服务取的 OAuth token。这就是 tsi 抽象让上层"无感切换"的真实落地。

> **钉死这件事**:配置 gRPC 安全的核心是"**按场景选 channel creds,按需叠加 call creds**"。零信任用 mTLS、同机用 local、Google 内部用 google_default,各自配 channel creds;需要认证用户时,用 composite 叠一个 OAuth/JWT。这套组合的自由度,来自 channel/call creds 分层 + composite 这两个设计。

---

## 八、章末小结

### 回扣主线

安全横跨二分法的**协议层和框架层**——tsi / frame_protector / handshaker 属于协议层(它们决定字节怎么加解密过线,是在 HTTP/2 字节流之外的另一层字节变换);channel/call creds 属于框架层(它们决定"谁被允许调用、用什么身份",是调用治理的一部分)。两者衔接点是 `SecurityHandshaker`:它在 HTTP/2 transport 启动之前跑,把裸 TCP endpoint 包装成 secure endpoint,让后续所有 HTTP/2 字节都自动加解密。这是"协议层保护 + 框架层治理"的标准组合。

### 五个为什么

1. **为什么 gRPC 要造 tsi 抽象层?**——gRPC 要跑在公网(TLS)、Google 内部(ALTS)、同机(local)等多种环境,安全后端完全不同;抽象一个统一的 handshaker/frame_protector/peer 三件套,上层只面对接口,底层实现自由替换,可插拔、可独立演进。
2. **为什么 Google 内部用 ALTS 而不是 TLS?**——TLS 在百万台机器规模下证书管理(分发 / 轮换 / 撤销)运维爆炸,且证书的"主机/域名"身份语义不适合数据中心的"服务账号"需求;ALTS 用 service account 作身份、靠独立 handshaker 服务握手,运维简洁、身份语义自然,代价是绑死 Google 基础设施。
3. **为什么 channel creds 和 call creds 必须分两层?**——机器身份是连接级(握一次手长期复用),用户身份是调用级(每次调用各取 token);生命周期不同,焊在一起会让连接复用被绑架(换用户就得换连接,HTTP/2 多路复用红利全废)、状态机互相干扰、跨场景复用堵死、演进互相阻塞。
4. **为什么 composite 能叠加两层 creds?**——`grpc_composite_channel_credentials` 内部同时持有一个 channel creds(TLS/ALTS)+ 一个 call creds(OAuth/JWT),`create_security_connector` 把两者一起传给底层,各自管各自的生命周期;这让你像搭积木一样组合(TLS+OAuth、ALTS+IAM、TLS+OAuth+plugin)。
5. **为什么 call creds 的 `GetRequestMetadata` 是 Promise(异步)?**——OAuth token 可能要刷新(过期前重取)、JWT 可能要现签、自定义 plugin 可能要外部调用;这些都不能阻塞调用线程,所以用 Promise 异步表达"拿到带 token 的 metadata"这个过程。

### 想继续深入往哪钻

- 想看 tsi 的完整接口:读 [`transport_security_interface.h`](../grpc/src/core/tsi/transport_security_interface.h),重点是 `tsi_handshaker`/`tsi_frame_protector`/`tsi_peer` 三个抽象、以及头注释里那段"typical usage"伪代码(把同步 / 异步握手流程说清了)。
- 想看 SSL/TLS 怎么填这套抽象:读 [`ssl_transport_security.cc`](../grpc/src/core/tsi/ssl_transport_security.cc) 和 [`ssl_transport_security.h`](../grpc/src/core/tsi/ssl_transport_security.h)(client/server handshaker factory、frame protector)。
- 想看 ALTS 的协议和实现:读 [`src/proto/grpc/gcp/handshaker.proto`](../grpc/src/proto/grpc/gcp/handshaker.proto)(协议)和 [`src/core/tsi/alts/`](../grpc/src/core/tsi/alts/) 下各子目录(handshaker / frame_protector / crypt)。
- 想看握手怎么接入 gRPC 连接建立:读 [`src/core/handshaker/security/security_handshaker.cc`](../grpc/src/core/handshaker/security/security_handshaker.cc) 的 `SecurityHandshaker` 类,以及通用 handshaker 框架 [`src/core/handshaker/handshaker.h`](../grpc/src/core/handshaker/handshaker.h)。
- 想看 composite creds 怎么叠加:读 [`composite_channel_credentials.h`](../grpc/src/core/credentials/transport/composite/composite_channel_credentials.h) 和对应 `.cc`,关注 `create_security_connector` 如何把内外两层 call creds 组合。
- 想学 TLS 协议本身:读 RFC 8446(TLS 1.3)、RFC 5246(TLS 1.2),以及 BoringSSL / OpenSSL 文档。

### 引出下一章

第 5 篇"生产可用"三件套——keepalive(探活)、健康检查(探健康)、安全(加密认证)——到这里讲完了。一条 gRPC 连接,现在能被探活、能被探健康、能被加密认证,具备了上线生产的全部基础。但生产可用还有另一面:**可观测**。一次调用失败了,trace-id 怎么传?一条 channel 卡住了,运行时怎么诊断?百万 QPS 下怎么出指标?下一章 P6-20,我们讲 gRPC 的元数据传播、channelz 诊断树、OpenTelemetry 集成——把"看不见"的 gRPC 变成"看得见"的。

> **下一章**:[P6-20 · 元数据、channelz、stats / otel](P6-20-元数据-channelz-stats-otel.md)
