# 第 1 篇 · 第 4 章 · 代码生成与 Stub

> **核心问题**:P1-02 定义了语言无关的契约、P1-03 讲了它怎么变成又小又快的字节。可这份字节,怎么变成 C++ 能 `stub->SayHello(req)` 调用、Java 能 `stub.sayHello(req)` 调用、Go 能 `stub.SayHello(ctx, req)` 调用的**本地代码**?一份 `.proto`,怎么一次生成出**五种语言**的 Stub,还给每种语言**一次给齐 sync / async / callback 三套 API**?服务端收到一个调用,又怎么凭"方法名"路由到正确的 handler?这一章拆 protoc + `grpc_cpp_plugin` 的代码生成机制,以及生成出来的 Stub 和 Service 基类怎么把"语言无关的契约"落地成"类型安全的本地调用"。

> **读完本章你会明白**:
> 1. protoc + gRPC plugin 的**两阶段生成**——protoc 先生成 message 类(序列化/反序列化代码),gRPC plugin 再生成 service 类(Stub 和 Service 基类);两阶段解耦,各管一段。
> 2. **sync / async / callback 三套 API** 怎么从同一份 IDL 生成出来,以及它们怎么用 C++ 模板混入(mixin)让用户**显式声明**"我要哪种"——这不是冗余,是三种并发模型各有用场的取舍。
> 3. **GeneratedServiceBase 与 method dispatch**:服务端怎么用一个方法名(如 `/helloworld.Greeter/SayHello`)路由到正确的 handler;生成的 `Service` 构造函数怎么把每个方法注册成 `RpcServiceMethod`,运行时怎么按 `api_type` 分发到 sync 线程池 / async completion queue / callback CQ。
> 4. **Stub 的两种调用形态**:为什么客户端 Stub 既要给"同步阻塞"的便捷方法,也要给"PrepareAsync"的异步原语——这背后是 callback → Promise 重构的前奏。

> **如果一读觉得太难**:先只记住三件事——① protoc 出 message 类,gRPC plugin 出 Stub/Service 类,两阶段;② 一个 RPC 方法在线上就是一个名字 `/包.服务/方法`,它就是 HTTP/2 的 `:path` 头;③ 服务端把每个方法注册成 `RpcServiceMethod`(带 handler 和 api_type),运行时按方法名查 handler、按 api_type 决定走 sync/async/callback 哪条路。

---

## 〇、一句话点破

> **代码生成把"语言无关的契约"翻译成"语言相关的本地类型安全调用":protoc 出消息类管序列化,gRPC plugin 出 Stub/Service 管调用;一个 RPC 方法在线上就是一个 `/包.服务/方法` 的名字,服务端凭这个名字路由到 handler。**

这是结论。本章倒过来拆:先讲代码生成为什么是两阶段、两个插件,再拆生成出来的 Stub 长什么样(三模 API),然后拆服务端的 Service 基类和 method dispatch,最后把"三模 API 的取舍"和后面 P3 的 Promise 重构串起来。

本章服务的二分法是**框架层**——它定义"调用怎么发起、怎么路由"。它承接 P1-02/03 的"契约和编码",讲清"契约怎么变出可调用的 Stub";它给全书后面所有"调用"相关章节(P3 call/CQ、P3 filter stack、P4 client_channel)铺垫——Stub 是旅程的起点,服务端 Service 是旅程的终点。

> **架构演进交代**:本章涉及的 `src/cpp/`(surface API)是经典架构的产物。三模 API 里,**callback API 是新方向的雏形**(更接近后面 Promise 重构的语义),sync/async(sync API + completion queue)是经典形态。本章会点出"三模 API 的演进正是后面 P3-10 Promise 重构的前奏",但不过度展开(留给 P3-10)。本章所有源码引用都是当前 master(`2195e869`)的真实状态。

---

## 一、为什么需要代码生成,且是两阶段

### 从 P1-02/03 接过来

P1-02 讲清了 `.proto` 是一份语言无关的契约,定义了"调什么(service/rpc)"和"传什么(message)";P1-03 讲清了 message 怎么变成又小又快的字节。可程序员最终要写的是 `stub->SayHello(req)` 这种**本地语言**的调用——它要有类型检查(`req` 必须是 `HelloRequest` 类型,错了编译期报错)、要有 IDE 补全、要能 `->` 调用。怎么从 `.proto` 变到这一步?

答案:**代码生成**。protoc 拿着 `.proto`,生成出每种语言对应的本地代码。

### 两阶段、两个插件

代码生成不是一步到位的,而是**两阶段、两个插件**:

```
            ┌─ protoc(主程序,protobuf 项目)
.proto  ───▶│
            │   ├─ 调 protobuf 内置生成器 ─▶ 各语言的 message 类(序列化/反序列化)
            │   └─ 调 --grpc_out 插件   ─▶ 各语言的 Stub + Service(gRPC 调用骨架)
            └─
```

- **第一阶段**:protoc 主程序(来自 protobuf 项目)解析 `.proto`,调用 **protobuf 内置的语言生成器**(如 `--cpp_out`、`--java_out`、`--go_out`),生成出**消息类**(`HelloRequest`、`HelloReply`)。这些类带序列化/反序列化方法(`SerializeToString`、`ParseFromString`),但**不带任何 RPC 调用能力**——它们只是数据结构。

- **第二阶段**:protoc 调用 **gRPC 提供的插件**(命令行参数 `--grpc_out`,插件二进制如 `grpc_cpp_plugin`),生成出 **service 类**(`Greeter::Stub`、`Greeter::Service`)。这些类**复用**第一阶段生成的消息类,提供 RPC 调用能力。

> **钉死这件事**:两阶段、两个插件是**解耦设计**。protobuf 只管"消息怎么定义、怎么序列化",不掺合 RPC;gRPC 只管"RPC 怎么调用",复用 protobuf 生成的消息类。这让 protobuf 和 gRPC 可以独立演进——换一种序列化方案(理论上),gRPC 的 service 类生成不受影响;换一种 RPC 框架(比如用 protobuf 配别的 RPC),protobuf 的消息类生成也不受影响。这是"**契约管数据、框架管调用**"在工具链层面的落地。

### gRPC 的插件长什么样

看 gRPC C++ 插件的入口 [`src/compiler/cpp_plugin.cc`](../grpc/src/compiler/cpp_plugin.cc#L19-L26):

```cpp
// Generates cpp gRPC service interface out of Protobuf IDL.
#include "src/compiler/cpp_plugin.h"

int main(int argc, char* argv[]) {
  CppGrpcGenerator generator;
  return grpc::protobuf::compiler::PluginMain(argc, argv, &generator);
}
```

就这几行。`main` 调用 protobuf 提供的 `PluginMain`,把 `CppGrpcGenerator` 注册进去。protobuf 的插件协议(protoc Plugin protocol)规定:protoc 把解析好的 `.proto`(以 `CodeGeneratorRequest` 的形式)通过 stdin 喂给插件,插件把要生成的代码(以 `CodeGeneratorResponse` 的形式)从 stdout 吐回给 protoc,protoc 再写到磁盘。这个协议让**任何语言**都能写自己的 gRPC 插件——gRPC 仓库里 `src/compiler/` 下有 `cpp_plugin`、`csharp_plugin`、`node_plugin`、`objective_c_plugin`、`php_plugin`、`python_plugin`、`ruby_plugin`,每个都是一个几十行的 `main`,真正的生成逻辑在各自的 `*_generator.cc` 里。

> **不这样会怎样**:如果没有统一的插件协议,每种语言要自己写一套"解析 .proto + 生成代码"的工具,重复造轮子。protoc Plugin protocol 把"解析 .proto"这件复杂的事留给 protobuf 主程序(它已经做得最好),插件只管"我要生成什么代码"。这是工程上的优雅分工——**复杂度集中、接口统一、扩展点开放**。

### 真正的生成逻辑:`cpp_generator.cc`

`cpp_plugin.cc` 是个薄壳,真正的生成逻辑在 [`src/compiler/cpp_generator.cc`](../grpc/src/compiler/cpp_generator.cc)。这个文件约 2500 行,把一份 `.proto` 的 service 定义,翻译成一整套 C++ 类。下面几节,我们逐个拆它生成出什么。

---

## 二、生成出来的客户端:Stub

先看客户端。protoc + grpc_cpp_plugin 给每个 service 生成两个客户端类:**`StubInterface`**(纯虚接口)和 **`Stub`**(实现)。看生成器怎么发号施牌 [`cpp_generator.cc:1403-1457`](../grpc/src/compiler/cpp_generator.cc#L1403-L1457):

```cpp
// (生成器源码,简化示意)
printer->Print(*vars,
               "class $Service$ final {\n"           // 外层 $Service$ 类(Greeter)
               " public:\n");
...
// service_full_name():返回 "/包.服务" 全限定名
printer->Print(*vars,
               "static constexpr char const* service_full_name() {\n"
               "  return \"$Package$$Service$\";\n"   // ← "helloworld.Greeter"
               "}\n");
...
// 客户端 StubInterface
printer->Print("class StubInterface {\n public:\n");
for (每个方法) {
  PrintHeaderClientMethodInterfaces(...);   // 同步方法
}
PrintHeaderClientMethodCallbackInterfacesStart(...);
for (每个方法) {
  PrintHeaderClientMethodCallbackInterfaces(...);  // callback 方法
}
...
// 客户端 Stub(实现)
printer->Print("class Stub final : public StubInterface {\n public:\n");
```

生成出来的 C++ 代码,大致长这样(以 helloworld 为例):

```cpp
// (生成代码示意,非源码原文)
class Greeter final {
 public:
  static constexpr char const* service_full_name() { return "helloworld.Greeter"; }

  class StubInterface {
   public:
    virtual ~StubInterface() {}
    // 同步
    virtual ::grpc::Status SayHello(::grpc::ClientContext* context,
                                    const HelloRequest& request,
                                    HelloReply* response) = 0;
    // async
    virtual std::unique_ptr<::grpc::ClientAsyncResponseReaderInterface<HelloReply>>
        AsyncSayHello(::grpc::ClientContext* context, const HelloRequest& request,
                      ::grpc::CompletionQueue* cq) = 0;
    // callback
    virtual ::grpc::ClientUnaryReactor* SayHello(
        ::grpc::ClientContext* context, const HelloRequest& request,
        ::grpc::ClientUnaryReactor* reactor) = 0;
    ...
  };

  class Stub final : public StubInterface {
   public:
    // 同步、async、callback 三套实现
    ::grpc::Status SayHello(::grpc::ClientContext*, const HelloRequest&, HelloReply*) override;
    ...
  };
};
```

### `service_full_name()`:方法在线上叫什么

注意生成器输出的 `service_full_name()`——它返回 `"$Package$$Service$"`,比如 `helloworld.Greeter`。这只是"服务名"。完整的**方法名**(在线上用于路由)是 `/包.服务/方法`,看生成器 [`cpp_generator.cc:2077-2082`](../grpc/src/compiler/cpp_generator.cc#L2077-L2082):

```cpp
printer->Print(*vars,
               "static const char* $prefix$$Service$_method_names[] = {\n");
for (int i = 0; i < service->method_count(); ++i) {
  (*vars)["Method"] = service->method(i)->name();
  printer->Print(*vars, "  \"/$Package$$Service$/$Method$\",\n");   // ← "/helloworld.Greeter/SayHello"
}
```

生成出来一个 `method_names[]` 数组,每个元素形如 `"/helloworld.Greeter/SayHello"`。**这个名字,就是 gRPC 调用在线上的身份**——它是 HTTP/2 请求的 `:path` 头(本书 P2-05、P2-06 会拆透)。客户端发调用时,Stub 把这个名字塞进 HTTP/2 的 `:path`;服务端收到调用,凭这个名字查 handler 路由(P2-08、P3-12 会拆)。

> **钉死这件事**:一个 RPC 方法,在线上就是一个字符串 `/包.服务/方法`。这个字符串由 `package` + `service` + `rpc` 名字拼出来,代码生成阶段就钉死了。它跨语言一致——C++ 客户端、Java 客户端、Go 客户端,发同一个 `SayHello`,线上都是 `/helloworld.Greeter/SayHello`。这是跨语言互通的根:不管哪种语言生成的 Stub,发出去的方法名都按同一规则拼。

### Stub 怎么持有 channel

看 Stub 的构造函数 [`cpp_generator.cc:2095-2120`](../grpc/src/compiler/cpp_generator.cc#L2095-L2120),生成器为每个方法创建一个 `RpcMethod` 对象,持有方法名和 channel:

```cpp
// (生成代码示意)
Greeter::Stub::Stub(const std::shared_ptr<::grpc::ChannelInterface>& channel,
                    const ::grpc::StubOptions& options)
    : channel_(channel) {
  // 对每个方法:
  rpcmethod_SayHello_ = ::grpc::internal::RpcMethod(
      "/helloworld.Greeter/SayHello", options.suffix_for_stats(),
      ::grpc::internal::RpcMethod::NORMAL_RPC, channel);
  ...
}
```

`RpcMethod` 是 gRPC C++ core 里描述"一个 RPC 方法"的内部类,看 [`include/grpcpp/impl/rpc_method.h`](../grpc/include/grpcpp/impl/rpc_method.h#L29-L43):

```cpp
class RpcMethod {
 public:
  enum RpcType {
    NORMAL_RPC = 0,
    CLIENT_STREAMING,
    SERVER_STREAMING,
    BIDI_STREAMING,
    SESSION_RPC
  };

  RpcMethod(const char* name, RpcType type, const std::shared_ptr<ChannelInterface>& channel)
      : name_(name),
        ...
        channel_tag_(channel->RegisterMethod(name)) {}
  ...
  const char* name() const { return name_; }
  ...
};
```

`RpcMethod` 持有方法名(`name_`)、方法类型(unary/streaming)、以及一个 `channel_tag_`(channel 给这个方法注册的内部标识,用于监控/统计)。Stub 调用方法时,把这个 `RpcMethod` 交给 channel,channel 负责把调用编码成 HTTP/2 流发出去(本书 P2、P3 拆)。

> **钉死这件事**:Stub 本身**不碰网络**。它只是个"方法名 + channel"的容器,把程序员写的 `stub->SayHello(req)` 翻译成"用 channel 发起一个名为 `/helloworld.Greeter/SayHello` 的调用"。真正碰网络的是 channel(以及它底下的 client_channel、SubChannel、chttp2 transport)。Stub 是旅程的起点,channel 是旅程的入口——本书 P3、P4 会拆 channel 怎么把调用送出去。

---

## 三、三模 API:sync / async / callback

gRPC C++ 的客户端,从一份 `.proto` **一次给齐三套 API**:sync(同步阻塞)、async(异步 + completion queue)、callback(回调)。这是 gRPC C++ 区别于其他语言客户端(很多只给一两种)的特色,也是它最让新手困惑的地方——为什么一份接口生成三套?

### 三种并发模型,各有用场

回答"为什么三套",先看三种并发模型各适合什么场景。

**sync(同步阻塞)**:调用 `stub->SayHello(req)` 直接阻塞,等响应回来才返回。代码像写本地函数一样直观:

```cpp
::grpc::ClientContext ctx;
HelloReply reply;
::grpc::Status s = stub->SayHello(&ctx, req, &reply);   // 阻塞到响应回来
if (s.ok()) { use(reply); }
```

适合**简单场景、低并发**。缺点是**阻塞调用线程**——如果一个线程要同时管海量并发调用,sync 模式下得开海量线程,撑不住。

**async(异步 + completion queue)**:调用返回一个 `ClientAsyncResponseReader`,调用立刻返回不阻塞;响应就绪后,通过 **completion queue**(完成队列,一个事件队列)投递一个事件,你从队列里 poll 出来处理:

```cpp
auto reader = stub->PrepareAsyncSayHello(&ctx, req, cq);
reader->StartCall();
reader->Finish(&reply, &status, tag);   // tag 是你给的标识
// ... 干别的活,不阻塞 ...
void* got_tag; bool ok;
cq->Next(&got_tag, &ok);                 // 从队列 poll 事件
if (got_tag == tag && ok) { use(reply); }
```

适合**高并发**:一个线程 poll 一个 completion queue,能服务海量并发调用(本书 P3-10 拆透 CQ)。缺点是代码**反直觉**——你不能"线性"地写调用流程,得拆成"发起"和"完成回调"两段,用 tag 串起来,容易写出 callback hell 的近亲。

**callback(回调)**:调用传一个 reactor(回调对象),响应就绪时 gRPC 自动调你的回调:

```cpp
class MyReactor : public ::grpc::ClientUnaryReactor {
  void OnDone(const ::grpc::Status& s) override {
    if (s.ok()) { use(reply_); }
    delete this;
  }
  HelloReply reply_;
};
MyReactor* r = new MyReactor;
stub->async()->SayHello(&ctx, &req, r);   // 立刻返回,响应来时调 r->OnDone
```

适合**高并发 + 代码相对直观**:比 async 的 tag 模型好写(回调对象封装了状态),又比 sync 高效(不阻塞线程)。这是 gRPC C++ 较新的 API,也是**向 Promise 重构过渡的形态**(callback 的"响应就绪自动回调"和 Promise 的"异步完成通知"语义接近)。

### 生成器怎么一次生成三套

看生成器 [`cpp_generator.cc:202-207`](../grpc/src/compiler/cpp_generator.cc#L202-L207),客户端方法接口的生成有个循环:

```cpp
struct {
  std::string prefix;
  std::string method_params;
  std::string raw_args;
} async_prefixes[] = {{"Async", ", void* tag", ", tag"},
                      {"PrepareAsync", "", ""}};
```

`async_prefixes` 这个数组,让生成器对每个方法**生成两份 async 变体**:`Async`(带 tag,直接发)和 `PrepareAsync`(不带 tag,先准备再发)。这是 async API 的两种用法。

callback API 在另一段生成 [`cpp_generator.cc:1438-1445`](../grpc/src/compiler/cpp_generator.cc#L1438-L1445)(`PrintHeaderClientMethodCallbackInterfaces`)。生成器遍历每个方法,生成一个 callback 版本。

合起来,每个 unary 方法(`SayHello`)在生成的 StubInterface 里**至少有这几份**:

- `SayHello`——sync(返回 `Status`)
- `AsyncSayHello`——async 带 tag(返回 `ClientAsyncResponseReader`)
- `PrepareAsyncSayHello`——async 不带 tag
- callback 版 `SayHello`(返回 `ClientUnaryReactor`)

> **钉死这件事**:三套 API 不是冗余,是**三种并发模型的取舍**。sync 简单但阻塞;async 高并发但反直觉;callback 折中且是新方向。gRPC 让你按场景选——低并发简单服务用 sync,超高并发极致性能用 async + CQ,中高并发要可读性用 callback。**这是 gRPC C++ 把"选择权交给用户"的设计哲学**,不替你决定哪种并发模型最好。

---

## 四、生成出来的服务端:Service 基类与 method dispatch

服务端比客户端复杂:它不光要"能调",还要"能被调"——收到一个调用,凭方法名路由到用户写的 handler。这一节拆服务端的 Service 基类和 method dispatch。

### 生成的 `Service` 类:每个方法变虚函数

看生成器 [`cpp_generator.cc:1495`](../grpc/src/compiler/cpp_generator.cc#L1495) 起的服务端类生成。生成的 `Service` 类(继承自 gRPC core 的 `::grpc::Service` 基类)把每个 RPC 方法变成一个**虚函数**,默认实现返回 `UNIMPLEMENTED` 错误:

```cpp
// (生成代码示意)
class Greeter::Service : public ::grpc::Service {
 public:
  Service();
  virtual ~Service();
  // 每个 rpc 方法变虚函数,默认 UNIMPLEMENTED
  virtual ::grpc::Status SayHello(::grpc::ServerContext* context,
                                  const HelloRequest* request,
                                  HelloReply* response) {
    return ::grpc::Status(::grpc::StatusCode::UNIMPLEMENTED, "");
  }
  ...
};
```

用户继承这个 `Service` 类,**override** 想实现的方法,写业务逻辑:

```cpp
class GreeterImpl : public Greeter::Service {
  ::grpc::Status SayHello(::grpc::ServerContext* ctx,
                         const HelloRequest* req,
                         HelloReply* resp) override {
    resp->set_message("Hello " + req->name());
    return ::grpc::Status::OK;
  }
};
```

不 override 的方法,默认返回 `UNIMPLEMENTED`——客户端调它会得到一个明确的错误码,而不是无声崩溃。

### `Service` 基类的 `methods_` 数组

关键来了:生成的 `Service` 子类构造函数,把每个方法注册成 `RpcServiceMethod`,塞进基类 `::grpc::Service` 的 `methods_` 数组。看生成器 [`cpp_generator.cc:2133-2200`](../grpc/src/compiler/cpp_generator.cc#L2133-L2200):

```cpp
// (生成器源码,简化示意)
printer->Print(*vars, "$ns$$Service$::Service::Service() {\n");
for (int i = 0; i < service->method_count(); ++i) {
  auto method = service->method(i);
  (*vars)["Idx"] = as_string(i);
  (*vars)["Method"] = method->name();
  if (method->NoStreaming()) {   // unary
    printer->Print(*vars,
      "AddMethod(new ::grpc::internal::RpcServiceMethod(\n"
      "    $prefix$$Service$_method_names[$Idx$],\n"                  // 方法名 "/包.服务/方法"
      "    ::grpc::internal::RpcMethod::NORMAL_RPC,\n"                // 方法类型
      "    new ::grpc::internal::RpcMethodHandler<$ns$$Service$::Service, "
      "$Request$, $Response$>(\n"
      "        []($ns$$Service$::Service* service,\n"                 // ← handler 是个 lambda
      "           ::grpc::ServerContext* ctx,\n"
      "           const $Request$* req,\n"
      "           $Response$* resp) {\n"
      "             return service->$Method$(ctx, req, resp);\n"      // ← 转发到虚函数
      "           }, this)));\n");
  } else if (ClientOnlyStreaming(method.get())) {
    // 类似,handler 是 ClientStreamingHandler
  } else if (ServerOnlyStreaming(method.get())) {
    // ServerStreamingHandler
  } else if (method->BidiStreaming()) {
    // BidiStreamingHandler
  }
}
```

生成出来的 `Service` 构造函数,大致长这样:

```cpp
// (生成代码示意)
Greeter::Service::Service() {
  AddMethod(new ::grpc::internal::RpcServiceMethod(
      Greeter_method_names[0],                              // "/helloworld.Greeter/SayHello"
      ::grpc::internal::RpcMethod::NORMAL_RPC,
      new ::grpc::internal::RpcMethodHandler<Greeter::Service, HelloRequest, HelloReply>(
          [](Greeter::Service* service, ::grpc::ServerContext* ctx,
             const HelloRequest* req, HelloReply* resp) {
            return service->SayHello(ctx, req, resp);       // ← 转发到用户 override 的虚函数
          }, this)));
  // SayHelloStreamReply、SayHelloBidiStream 同理...
}
```

这就是 method dispatch 的核心:**每个方法 → 一个 `RpcServiceMethod`,带方法名、类型、handler**。handler 是个 lambda,转发到用户 override 的虚函数。方法按 `Idx`(在 service 里的索引)塞进 `methods_` 数组。

### `RpcServiceMethod`:方法名 + 类型 + handler + api_type

看 gRPC C++ core 的 `RpcServiceMethod` 定义 [`include/grpcpp/impl/rpc_service_method.h`](../grpc/include/grpcpp/impl/rpc_service_method.h#L86-L102):

```cpp
class RpcServiceMethod : public RpcMethod {
 public:
  RpcServiceMethod(const char* name, RpcMethod::RpcType type, MethodHandler* handler)
      : RpcMethod(name, type),
        server_tag_(nullptr),
        api_type_(ApiType::SYNC),         // ← 默认 SYNC
        handler_(handler) {}

  enum class ApiType {
    SYNC,
    ASYNC,
    RAW,
    CALL_BACK,      // not CALLBACK because that is reserved in Windows
    RAW_CALL_BACK,
  };
  ...
  MethodHandler* handler() const { return handler_.get(); }
  ApiType api_type() const { return api_type_; }
  ...
};
```

`RpcServiceMethod` 持有四样东西:

1. **方法名**(`name`,继承自 `RpcMethod`)——用于路由;
2. **方法类型**(`RpcType`:unary/streaming)——决定怎么收发消息;
3. **handler**——业务逻辑的入口(那个 lambda,转发到用户虚函数);
4. **api_type**——这个方法是 sync 还是 async 还是 callback,**决定运行时走哪条路**。

第 4 点是 method dispatch 的关键,下一节拆。

### `::grpc::Service` 基类:AddMethod + MarkMethodAsync/Callback

看 `::grpc::Service` 基类(所有生成的 Service 的父类)[`include/grpcpp/impl/service_type.h`](../grpc/include/grpcpp/impl/service_type.h#L155-L226),它维护一个 `methods_` 数组,并提供 `AddMethod`、`MarkMethodAsync`、`MarkMethodCallback` 等方法:

```cpp
class Service {
 ...
 protected:
  void AddMethod(internal::RpcServiceMethod* method) {
    methods_.emplace_back(method);                  // 加进数组
  }

  void MarkMethodAsync(int index) {
    methods_[index]->SetServerApiType(internal::RpcServiceMethod::ApiType::ASYNC);
  }

  void MarkMethodCallback(int index, internal::MethodHandler* handler) {
    methods_[index]->SetHandler(handler);
    methods_[index]->SetServerApiType(internal::RpcServiceMethod::ApiType::CALL_BACK);
  }
  ...
 private:
  std::vector<std::unique_ptr<internal::RpcServiceMethod>> methods_;
};
```

生成的 Service 构造函数调 `AddMethod`(把方法加进数组)。但方法默认是 `SYNC` 的——怎么变成 async 或 callback?答案在下一节:**模板混入(mixin)**。

---

## 五、三模 API 的服务端:模板混入(mixin)

服务端怎么"同一个方法,既支持 sync、又支持 async、又支持 callback"?gRPC 用了 C++ 的**模板混入(curiously recurring template pattern, mixin)**:生成器为每个方法生成三个 mixin 类(`WithAsyncMethod_X`、`WithCallbackMethod_X`、`WithStreamedMethod_X`),用户继承时**显式声明**"我要哪种",mixin 在构造时调 `MarkMethodAsync`/`MarkMethodCallback` 把方法的 api_type 改过来。

### `WithCallbackMethod_X` mixin 长什么样

看生成器 [`cpp_generator.cc:1043-1105`](../grpc/src/compiler/cpp_generator.cc#L1043-L1105),为每个方法生成一个 callback mixin:

```cpp
// (生成器源码,简化示意)
printer->Print(*vars,
  "template <class BaseClass>\n"
  "class WithCallbackMethod_$Method$ : public BaseClass {\n"   // ← 模板混入
  " public:\n");
printer->Print(*vars, "WithCallbackMethod_$Method$() {\n");
// unary:
printer->Print(*vars,
  "  ::grpc::Service::MarkMethodCallback($Idx$,\n"             // ← 构造时 mark 成 callback
  "      new ::grpc::internal::CallbackUnaryHandler<$RealRequest$, $RealResponse$>(\n"
  "        [this](::grpc::CallbackServerContext* context, "
  "const $RealRequest$* request, $RealResponse$* response) {\n"
  "          return this->$Method$(context, request, response); }));}\n");
```

生成出来(以 `SayHello` 为例):

```cpp
// (生成代码示意)
template <class BaseClass>
class WithCallbackMethod_SayHello : public BaseClass {
 public:
  WithCallbackMethod_SayHello() {
    ::grpc::Service::MarkMethodCallback(0,                    // ← idx=0(SayHello 是第 0 个方法)
        new ::grpc::internal::CallbackUnaryHandler<HelloRequest, HelloReply>(
          [this](::grpc::CallbackServerContext* ctx,
                 const HelloRequest* req, HelloReply* resp) {
            return this->SayHello(ctx, req, resp);            // ← 转发到 callback 版虚函数
          }));
  }
  ...
};
```

用户写 callback 服务时,这样继承(把所有方法的 callback mixin 套上去):

```cpp
class GreeterCallbackServiceImpl final
    : public Greeter::CallbackService {   // Greeter::CallbackService 已经套好所有 WithCallbackMethod_*
 public:
  ::grpc::ServerUnaryReactor* SayHello(
      ::grpc::CallbackServerContext* ctx,
      const HelloRequest* req) override {
    // 业务逻辑
    auto* reactor = new ::grpc::ServerUnaryReactor;
    reactor->Finish(::grpc::Status::OK);
    return reactor;
  }
};
```

`Greeter::CallbackService` 是生成器生成的一个"已经套好所有 callback mixin"的别名(看 [`cpp_generator.cc:1539`](../grpc/src/compiler/cpp_generator.cc#L1539) 的 `WithCallbackMethod_$method_name$<...>` 嵌套),用户直接继承它就行,不用自己一个个套 mixin。同理有 `Greeter::AsyncService`(套好 async mixin)。

### mixin 的妙处:显式声明,无运行时开销

这种 mixin 设计有三个妙处:

1. **显式声明 api_type**:你继承 `Greeter::CallbackService`,编译期就定死了"我用 callback API";继承 `Greeter::AsyncService`,定死了"我用 async API"。**选哪种,类型系统帮你记着**,不会运行时混乱。
2. **同一个底层 `methods_` 数组**:sync/async/callback 三种,底层都是 `RpcServiceMethod` 数组,只是 `api_type` 字段不同。这让运行时分发逻辑统一——下一节看 `RegisterService` 怎么按 `api_type` 分发。
3. **零运行时开销**:mixin 在**构造函数**里就把 api_type mark 好,这是编译期可优化的代码,运行时没有"判断用哪种 API"的分支开销。

> **不这样会怎样**:如果不用 mixin,而是给 sync/async/callback 各生成一套独立的 Service 类(`Greeter::SyncService`、`Greeter::AsyncService`、`Greeter::CallbackService` 互不相干),那三套类的代码大量重复(方法定义、handler 注册逻辑都一样,只差 api_type)。mixin 让三套共享底层结构,只在构造时 mark 不同 api_type——**DRY(Don't Repeat Yourself)在代码生成器里的优雅实践**。

---

## 六、运行时:服务端怎么把调用路由到 handler

生成出来的代码搞清楚了,看运行时怎么串。当服务端收到一个 gRPC 调用(带着方法名 `/helloworld.Greeter/SayHello`),它怎么找到 handler、怎么按 api_type 分发?

### `Server::RegisterService`:把 service 注册进 C core

看 gRPC C++ 的 `Server::RegisterService` [`src/cpp/server/server_cc.cc:1052-1097`](../grpc/src/cpp/server/server_cc.cc#L1052-L1097):

```cpp
bool Server::RegisterService(const std::string* addr, grpc::Service* service) {
  bool has_async_methods = service->has_async_methods();
  ...
  for (const auto& method : service->methods_) {           // 遍历 service 的每个方法
    if (method == nullptr) { continue; }                   // generic 方法跳过

    // 把方法名注册进 C core,拿到一个 tag
    void* method_registration_tag = grpc_server_register_method(
        server_, method->name(), addr ? addr->c_str() : nullptr,
        PayloadHandlingForMethod(method.get()), 0);
    ...

    if (method->handler() == nullptr) {                    // async 方法(handler 为 null)
      method->set_server_tag(method_registration_tag);
    } else if (method->api_type() ==                       // sync 方法
               grpc::internal::RpcServiceMethod::ApiType::SYNC) {
      for (const auto& value : sync_req_mgrs_) {
        value->AddSyncMethod(method.get(), method_registration_tag);   // 注册到 sync 线程池
      }
    } else {                                               // callback 方法
      has_callback_methods_ = true;
      grpc::internal::RpcServiceMethod* method_value = method.get();
      grpc::CompletionQueue* cq = CallbackCQ();            // 用专门的 callback CQ
      grpc_server_register_completion_queue(server_, cq->cq(), nullptr);
      grpc_core::Server::FromC(server_)->SetRegisteredMethodAllocator(
          cq->cq(), method_registration_tag, [this, cq, method_value] {
            ...                                             // 注册 callback 请求分配器
          });
    }
    ...
  }
  ...
}
```

这是 method dispatch 的运行时核心。读这段代码,能看出三件事:

1. **方法名注册进 C core**:`grpc_server_register_method(server_, method->name(), ...)` 把方法名(如 `/helloworld.Greeter/SayHello`)注册进 gRPC 的 C 核心(C core 是 gRPC 的引擎,P3 会拆),返回一个不透明的 `method_registration_tag`。**C core 凭方法名做第一次路由**——收到调用,解析出方法名,查这个注册表,找到 tag。
2. **按 `api_type` 分发**:
   - **async 方法**(`handler() == nullptr`):只设个 tag,等用户自己用 `RequestAsyncUnary` 主动来"领"调用(经典 async 模型,用户驱动)。
   - **sync 方法**:`AddSyncMethod` 注册到 sync 线程池(`sync_req_mgrs_`)——调用来了,线程池自动领走,调 handler,阻塞执行。
   - **callback 方法**:用专门的 `CallbackCQ`(callback completion queue)注册一个"请求分配器"——调用来了,gRPC 自动调 handler,handler 返回 reactor,reactor 在 callback CQ 上完成。
3. **三种 api_type,三套执行路径**:sync 走线程池(阻塞)、async 走用户 poll 的 CQ(用户驱动)、callback 走 callback CQ(自动调)。

> **钉死这件事**:服务端 method dispatch 是**两层路由**——第一层,C core 凭方法名(`/包.服务/方法`)查注册表,找到 `RpcServiceMethod`;第二层,凭 `api_type`(SYNC/ASYNC/CALL_BACK)决定走哪条执行路径(线程池 / 用户 CQ / callback CQ)。这两层分离,让"接口定义(方法名)"和"并发模型(api_type)"解耦——同一份 `.proto`,你可以选 sync、async、callback 任一种来跑,接口不变。

### syncreq_mgr:sync 方法的线程池

补一句 sync 路径。sync API 下,gRPC 用一个**线程池**(`SyncRequestThreadManager`,见 `server_cc.cc` 里的 `SyncRequest`/`CallbackRequest` 类)来处理调用:调用来了,线程池里一个线程领走,调你的 handler,handler 阻塞执行(因为是 sync),执行完归还线程。这就是为什么 sync API 简单但吃线程——每个并发调用占一个线程,海量并发下线程数爆炸。callback/async 则是一个线程 poll CQ 服务海量调用(P3-10 拆透),更省。

---

## 七、技巧精解:方法分发机制与三模 API 的模板实现

本章技巧精解,钉两件事:一是 method dispatch 的"两层路由"为什么这么设计(朴素地写会撞什么墙);二是三模 API 用模板混入实现的设计精妙(为什么不是给每种 API 单独生成一套类)。

### 技巧一:method dispatch 的"两层路由"

回头看 method dispatch 的结构:

```
调用进来(带方法名 "/helloworld.Greeter/SayHello")
   │
   ▼
[C core 第一层路由]:grpc_server_register_method 注册表
   │  凭方法名查,找到 RpcServiceMethod(在 methods_ 数组里)
   ▼
[第二层路由]:RpcServiceMethod::api_type
   │  SYNC     → sync 线程池(阻塞执行 handler)
   │  ASYNC    → 用户 poll 的 CQ(用户 RequestAsyncXxx 主动领)
   │  CALL_BACK→ callback CQ(自动调 handler,handler 返回 reactor)
   ▼
执行 handler(lambda,转发到用户 override 的虚函数)
```

#### 朴素地写会撞什么墙

朴素地写 method dispatch,会怎么做?最直接的:**一个大 switch,按方法名字符串分发**。

```cpp
// 朴素写法(示意,非源码原文)
Status dispatch(const std::string& method, Request req) {
  if (method == "/helloworld.Greeter/SayHello") {
    return sayHelloHandler(req);
  } else if (method == "/helloworld.Greeter/SayHelloStreamReply") {
    return sayHelloStreamReplyHandler(req);
  } else if (...) {
    ...
  } else {
    return Status::UNIMPLEMENTED;
  }
}
```

这个写法的问题:

1. **无法扩展**:每加一个方法,要改这个 switch。代码生成器没法干净地生成——要么生成整个 switch(巨大),要么生成一堆 if-else。gRPC 要支持 reflection(运行时查有哪些方法)、要支持 generic service(不预定义方法名,用 `:path` 当 key),switch 模型做不到。
2. **字符串比较慢**:每次调用都做一堆 `==` 字符串比较,O(N)。方法多了,分发变慢。
3. **api_type 不好挂**:switch 只按方法名分发,怎么表达"这个方法是 sync、那个是 callback"?得再加一层 switch on api_type,代码更乱。

#### gRPC 的两层路由妙在哪

gRPC 的两层路由,干净地解开了这些:

1. **第一层用注册表(C core 的 hash 表)**:`grpc_server_register_method` 把方法名注册成一个不透明 tag,O(1) 查找。注册表是动态的——reflection 能查、generic service 能加,不受 switch 死结构限制。
2. **第二层用 `api_type` 字段**:每个 `RpcServiceMethod` 自带 `api_type`,分发时读这个字段,决定走哪条路。加 api_type 不用改分发代码——`MarkMethodCallback` 把字段一改,分发自然走 callback 路。
3. **handler 是对象(多态),不是 switch 分支**:每个方法的 handler 是个 `MethodHandler` 对象(虚函数 `RunHandler`),分发就是 `handler->RunHandler(param)`——多态分发,O(1),且 handler 可以是任意复杂度的对象(支持 message allocator、reactor 等高级特性)。

> **不这样会怎样**:switch 分发是"硬编码",注册表 + 多态 handler 是"软编码"。gRPC 要支持 N 种语言、reflection、generic service、动态配置,必须用软编码。**method dispatch 的两层路由,本质上是把"分发逻辑"从代码生成器推到运行时,用数据(注册表 + api_type 字段)驱动**。这是可扩展架构的典型手法——本书后面 P3-11 的 filter stack(用数据驱动的责任链)、P4-15 的负载均衡(用数据驱动的 Picker)都是同一思想。

### 技巧二:三模 API 的模板混入 vs 多套独立类

第二个技巧,钉"为什么三模 API 用 mixin,而不是生成三套独立类"。

#### 朴素地写:三套独立类

朴素地写,gRPC 可以给 sync/async/callback 各生成一套独立的 Service 类:

```cpp
// 朴素写法(示意,非源码原文)
class GreeterSyncService { /* sync 版,带 sync handler */ };
class GreeterAsyncService { /* async 版,mark ASYNC */ };
class GreeterCallbackService { /* callback 版,mark CALL_BACK */ };
```

三套类各自独立。这看起来清晰,但有三个问题:

1. **代码重复**:三套类里,方法定义、handler 注册逻辑、方法名数组,大量重复。每改一处生成器逻辑,三套都要同步改。
2. **不能混用**:有时候你想"这个方法用 sync,那个方法用 callback"——朴素三套独立类做不到(一个 service 只能整隶属于一种)。
3. **类型不统一**:三套类没有共同基类(除了 `::grpc::Service`),处理"一个 service 注册进 server"的代码要写三遍。

#### gRPC 的 mixin 妙在哪

gRPC 用模板混入:每个方法一个 `WithAsyncMethod_X`、`WithCallbackMethod_X` mixin,基类是 `Greeter::Service`(默认 sync)。用户继承时,按需套 mixin:

```cpp
// 全 sync(默认,继承 Greeter::Service)
class MySyncGreeter : public Greeter::Service { ... };

// 全 async(继承 Greeter::AsyncService,它套好了所有 WithAsyncMethod_*)
class MyAsyncGreeter : public Greeter::AsyncService { ... };

// 全 callback(继承 Greeter::CallbackService)
class MyCallbackGreeter : public Greeter::CallbackService { ... };

// 混合(SayHello 用 callback,SayHelloStreamReply 用 async)—— 也能做到!
class MyMixedGreeter
    : public WithCallbackMethod_SayHello<
          WithAsyncMethod_SayHelloStreamReply<Greeter::Service>> { ... };
```

妙处:

1. **共享底层**:`methods_` 数组、`AddMethod`、handler 注册逻辑,全部在基类 `::grpc::Service` 里。三模只是构造时 `Mark` 不同的 api_type,底层统一。
2. **可混用**:想方法级混用?套不同的 mixin 就行(上面第 4 个例子)。这在"有些方法阻塞简单、有些要高并发"的混合场景很有用。
3. **类型统一**:不管套了多少 mixin,最终都继承 `::grpc::Service`,`RegisterService` 一套代码通吃。
4. **编译期 mark**:mixin 在构造函数里 mark api_type,编译期可优化,运行时无分支开销。

> **钉死这件事**:三模 API 的 mixin 设计,本质是 **CRTP(Curiously Recurring Template Pattern)+ mixin 的组合应用**——把"sync/async/callback 的差异"封装成可组合的 mixin,共享统一的底层(`RpcServiceMethod` 数组)。这是 C++ 模板元编程在工程里的优雅实践:**用编译期组合换运行时灵活**。本书后面 P3-11 的 filter fusion(把多个 filter 在编译期融合成一条流水线)是同一思想的更高阶应用——模板元编程是 gRPC core 的看家本领。

---

## 八、章末小结

### 回扣主线

本章是框架层的第一站,承接 P1-02/03 的"契约和编码"、讲清"契约怎么变出可调用的 Stub"。核心是**代码生成把语言无关的契约,翻译成语言相关的类型安全调用**:

- **两阶段、两插件**:protoc 出消息类(序列化),gRPC plugin 出 Stub/Service(调用),解耦。
- **方法名 = `/包.服务/方法`**:这是 RPC 在线上的身份,是 HTTP/2 的 `:path`,跨语言一致——这是跨语言互通的根。
- **三模 API(sync/async/callback)**:三种并发模型的取舍,gRPC 让用户按场景选;服务端用模板混入让用户显式声明 api_type。
- **method dispatch 两层路由**:C core 凭方法名查注册表,再凭 api_type 走 sync 线程池 / async CQ / callback CQ。

Stub 是旅程的起点(客户端),Service 是旅程的终点(服务端)——本章把这两端立起来。接下来 P2~P4,就是拆"调用怎么从 Stub 穿过 channel、穿过 filter 栈、穿过 client_channel、被 balancer 选路、被 chttp2 编成 HTTP/2 流、过线、被服务端解析回 handler"的完整旅程。

### 架构演进:三模 API 是 Promise 重构的前奏

本章涉及的 `src/cpp/`(surface API)是经典架构的产物。但三模 API 里,**callback API 是新方向的雏形**:

- sync API(阻塞)和 async API(CQ + tag)是经典架构的标志——sync 简单但吃线程,async 高效但反直觉(callback hell 的近亲)。
- callback API 更接近"响应就绪自动通知"的语义,和后面 **Promise 重构**的"异步步骤线性可组合"目标一致。gRPC core 正在把 callback API 的语义,升级成更纯粹的 Promise 模型(`call_spine` + `filter fusion`)——本书 P3-10 会拆透。

> **钉死这件事**:三模 API 的演进(sync → async → callback → Promise),本身是 gRPC 处理"异步复杂度"的方法论演进。sync 太原始(阻塞)、async 太底层(tag 模型难组合)、callback 是中间态、Promise 是目标态。理解这条演进线,你就理解了为什么 gRPC core 要大费周章搞 Promise 重构——不是为了追新,而是 callback/CQ 在复杂 filter 链下真的扛不住。这是 P3-10 的主线,本章先埋下种子。

### 五个为什么

1. **为什么代码生成是两阶段、两插件?**——protobuf 出消息类(管序列化),gRPC plugin 出 Stub/Service(管调用),解耦。protobuf 不掺合 RPC,gRPC 复用 protobuf 生成的消息类。这让两者独立演进,也让"契约管数据、框架管调用"在工具链层面落地。

2. **为什么一个 RPC 方法在线上叫 `/包.服务/方法`?**——由 `package` + `service` + `rpc` 名字拼出,代码生成阶段钉死。这个字符串是 HTTP/2 请求的 `:path` 头,跨语言一致——C++、Java、Go 客户端发同一个方法,线上都是同一个字符串。这是跨语言互通的根。

3. **为什么 gRPC C++ 一次给齐 sync/async/callback 三套 API?**——三种并发模型各有用场:sync 简单但阻塞、async 高并发但反直觉、callback 折中且是新方向。gRPC 把选择权交给用户,按场景选。服务端用模板混入让用户显式声明 api_type,共享底层 `RpcServiceMethod` 数组。

4. **为什么服务端 method dispatch 是两层路由?**——第一层 C core 凭方法名查注册表(动态、可扩展,支持 reflection/generic service);第二层凭 api_type 走 sync 线程池/async CQ/callback CQ。朴素 switch 分发无法扩展、字符串比较慢、挂不了 api_type;注册表 + 多态 handler 是数据驱动分发,加方法/改 api_type 不改分发代码。

5. **为什么三模 API 用模板混入,不是三套独立类?**——共享底层(避免代码重复)、可方法级混用、类型统一(`RegisterService` 一套通吃)、编译期 mark api_type 零运行时开销。这是 CRTP + mixin 在工程的优雅实践,和后面 P3-11 filter fusion 同一思想(编译期组合换运行时灵活)。

### 想继续深入往哪钻

- **想看生成出来的代码长什么样**:对你自己的 `.proto` 跑 `protoc --cpp_out=. --grpc_out=. --plugin=protoc-gen-grpc=grpc_cpp_plugin`,打开生成的 `.grpc.pb.h` 和 `.grpc.pb.cc`,对照本章讲的 Stub/Service/mixin,逐行看。
- **想看生成器源码**:逐段读 [`src/compiler/cpp_generator.cc`](../grpc/src/compiler/cpp_generator.cc)(约 2500 行),重点看 `GetHeaderServices`(客户端 Stub 生成)和 `PrintSourceServiceMethod`(服务端 handler 注册)。
- **想看 `RpcServiceMethod` 和 api_type**:读 [`include/grpcpp/impl/rpc_service_method.h`](../grpc/include/grpcpp/impl/rpc_service_method.h) 和 [`include/grpcpp/impl/service_type.h`](../grpc/include/grpcpp/impl/service_type.h),看 `methods_` 数组、`AddMethod`、`MarkMethodAsync`/`MarkMethodCallback`。
- **想看服务端注册与分发**:读 [`src/cpp/server/server_cc.cc`](../grpc/src/cpp/server/server_cc.cc) 的 `Server::RegisterService`,看方法名怎么注册进 C core、怎么按 api_type 分发到 sync 线程池/callback CQ。
- **想理解 callback API 和 Promise 的关系**:这是 P3-10 的主线,本章先建立"callback 是 Promise 重构前奏"的直觉。

### 引出下一章

我们走完了第 1 篇(契约):P1-02 讲清了字段号换兼容、P1-03 讲清了 varint 换小快、P1-04 讲清了代码生成换跨语言类型安全调用。现在,Stub 已经能 `stub->SayHello(req)` 了,Service 已经能收调用了。可这个调用,从 Stub 发出去,到 Service 收到,中间要穿过 channel、穿过 filter 栈、被 resolver 解析成地址、被 balancer 挑中一条 SubChannel、被 chttp2 编码成 HTTP/2 流、过线、被服务端解析回方法——这条旅程怎么走通?第 2 篇,我们进入全书最硬的部分:**gRPC 自己用 C 实现的一整套 HTTP/2**。下一章 P2-05,从 HTTP/2 基础开始——为什么 gRPC 放弃 HTTP/1.1 和私有 TCP,选 HTTP/2。

> **下一篇**:[P2-05 · HTTP/2 基础:为什么是 gRPC 的天然底座](P2-05-HTTP2基础-为什么是天然底座.md)
