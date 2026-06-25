# 第 1 篇 · 第 2 章 · IDL 与 .proto:语言无关的契约

> **核心问题**:网络两端,一台可能是 Java 写的订单服务,一台可能是 Go 写的用户服务,一台可能是 Python 写的推荐模型——它们没有共同的对象模型,甚至没有共同的类型系统。那么,跨语言调用凭什么不乱套?答案是:必须先有一份**语言无关**的契约,用所有人都同意的方式把"有哪些服务、每个服务有哪些方法、每个方法的请求和响应长什么样"白纸黑字写下来。protobuf 的 `.proto` 就是这份契约。这一章要拆透的不是"`.proto` 怎么写"(那是入门教程的事),而是**为什么 `.proto` 这么设计**——尤其是那个一眼会被新手忽略、却是 protobuf 全部兼容性根基的细节:**用字段号(field number)而非字段名来定位字段**。

> **读完本章你会明白**:
> 1. 为什么跨语言调用**必须**先有一份语言无关的 IDL,口头约定(JSON 字段名)为什么会腐烂、为什么编译期查不出错。
> 2. 为什么 protobuf 用**字段号**而不是字段名或字段位置定位字段——这是"加字段不破坏老代码"的根,也是 protobuf 向前/向后兼容的全部秘密。
> 3. `message` / `service` / `rpc` / `oneof` / `map` / `enum` 这些构件各回答了什么问题;`proto2` / `proto3` / `editions` 的取舍里藏着什么教训。
> 4. 为什么"契约管跨语言"这句话能落到一份**机器可读、版本化、可代码生成**的 `.proto` 上,以及它和后面 P1-03(编码)、P1-04(代码生成)的衔接。

> **如果一读觉得太难**:先只记住三件事——① 跨语言调用必须先有一份语言无关的契约,protobuf 的 `.proto` 就是;② 字段靠**编号**定位,不靠名字,所以加字段不破坏老代码(这是兼容性的根);③ `service` + `rpc` 描述"调什么",`message` 描述"传什么",两者合成一份完整的接口合同。

---

## 〇、一句话点破

> **`.proto` 是一份合同:它用语言无关的方式把"接口"写死,再用字段号而非字段名把"字段"钉死——前者换跨语言,后者换向前/向后兼容。**

这是结论。本章倒过来拆:先讲为什么跨语言一定要合同,再讲这份合同具体怎么写,然后把"字段号"这个最容易被新手略过、却是 protobuf 灵魂的细节单独钉死,最后讲 `oneof` / `map` / `editions` 这些构件各补了什么缺。

本章服务的二分法是**协议层(契约层)**——它定义"接口长什么样、字段怎么编号",但还**不涉及字节怎么过线**(那是 P1-03 的事),也**不涉及调用怎么发起**(那是 P1-04 的事)。契约层和架构演进(callback → Promise)无关,无论是经典还是新 Promise 架构,这份 `.proto` 都是一字不改的稳定地基。

---

## 一、为什么跨语言调用必须先有一份"语言无关"的契约

### 从 P0-01 接过来:三件套的第一件

P0-01 讲了 gRPC 用"**IDL 契约 + HTTP/2 流 + protobuf 编码**"三件套回答"怎么把一次跨网络、跨语言的方法调用做对"。其中第一件就是**契约**:让网络两端,哪怕用的是完全不同的语言、不同的对象模型、不同的类型系统,也能就"接口长什么样"达成一致。这一章,我们就把这件拆透。

### 先看一个真实的小契约

打开 gRPC 仓库里最经典的示例 [`examples/protos/helloworld.proto`](../grpc/examples/protos/helloworld.proto#L24-L42):

```protobuf
package helloworld;

service Greeter {
  rpc SayHello (HelloRequest) returns (HelloReply) {}
  rpc SayHelloStreamReply (HelloRequest) returns (stream HelloReply) {}
  rpc SayHelloBidiStream (stream HelloRequest) returns (stream HelloReply) {}
}

message HelloRequest {
  string name = 1;
}

message HelloReply {
  string message = 1;
}
```

短短十几行,已经把"跨语言 RPC 接口"的全部要素写齐了:

- `package helloworld;` —— 给这份契约一个命名空间,避免和别人的 `Greeter` 撞名。
- `service Greeter { rpc ... }` —— 声明**有哪些可调用的方法**。这就是 P0-01 说的"显式契约":接口不再是藏在某个本地对象背后的小秘密,而是白纸黑字写在文件里。
- `message HelloRequest { string name = 1; }` —— 声明**每个方法的请求和响应长什么样**。注意那个 `= 1`——它是**字段号**,不是字段名。这是本章后半段的主角,先记下它。
- `stream` 关键字 —— 声明这个参数是**一条流**(可以发/收多个),对应 P0-01 讲的四种调用模式。

这一份文件,既不是 C++,也不是 Java,也不是 Go——它是一份**语言无关**的描述。然后 protoc(代码生成器)拿着它,生成出 C++ 的 `Greeter::Stub`、Java 的 `GreeterGrpc.GreeterStub`、Go 的 `GreeterClient`……每种语言拿到的是一个**类型安全**的本地对象,调用方法时编译器能帮你查参数类型。

> **钉死这件事**:`.proto` 不是"给某一种语言用的接口定义",而是**给所有语言用的、共同的接口事实来源(single source of truth)**。这是"跨语言"这四个字能落地的前提。

### 不这样会怎样:口头协议的腐烂

假如没有这份语言无关的契约,跨语言调用就只能靠**口头协议**:两边程序员约好,"我给你发一个 JSON,里面有个字段叫 `userId`,是字符串"。

听起来也能跑,可它会**腐烂**,而且烂得悄无声息:

1. **命名漂移**:今天叫 `userId`,明天 Java 那边习惯驼峰写成 `userId`,Python 那边习惯下划线写成 `user_id`,Go 那边写成 `UserID`。没有机器校验,全靠人记。半年后谁也说不清到底该叫什么。
2. **类型不一致**:说好 `age` 是整数,Java 发了个 `int` 30,Python 那边 `json.loads` 出来是 `float` 30.0,JavaScript 那边算术时悄悄变成字符串拼接 `"30" + 1 = "301"`。运行时才炸,排查到脱发。
3. **加字段不通知**:Java 端加了 `email` 字段自己用,忘了告诉 Go 端;Go 端的代码不知道有这个字段,反序列化时直接丢掉,数据无声丢失。
4. **没有版本,没有兼容性承诺**:谁加了字段、删了字段、改了类型,没有任何记录,也没有任何机制保证"老代码还能跑"。

> **不这样会怎样**:口头协议的本质问题是**它不在编译期里**。所有错误都推迟到运行时,而且推迟到**生产环境真正跑起来的那一刻**才暴露。一份机器可读、编译期校验、版本化的 `.proto` 契约,把这些错误**前移到编译期**:protoc 一跑,类型不匹配当场报错;字段号重复当场报错;service 没有定义对应 message 当场报错。这就是契约的价值——**把分布式系统最容易腐烂的"接口约定",从口头变成机器可校验的合同**。

### 所以 gRPC 选 protobuf 当 IDL

gRPC 三件套里,IDl 这件选了 protobuf 的 `.proto`。这个选择不是偶然:

- **protobuf 本身就是为跨语言设计的**:Google 内部 Stubby(gRPC 的前身)早在 2001 年就要面对 C++ / Java / Python 多语言共存,protobuf 就是为"一份描述,生成多语言"而生的。
- **protobuf 自带编码方案**:它不只是定义接口,还规定了**字段怎么变成字节**(P1-03 拆)。IDL 和编码方案是一套的,不会出现"接口定义和序列化各搞各的、对不上"的尴尬。
- **工具链成熟**:protoc + 各语言插件,生成出的代码类型安全、可用。

> **为什么不是 Thrift / FlatBuffers / JSON Schema**:Thrift 也是优秀的二进制 IDL,FlatBuffers 更省零拷贝,JSON Schema 是文本的。gRPC 选 protobuf,部分是技术原因(protobuf 的 `oneof`/`map`/`Any`/`editions` 体系完善、和 gRPC 同出 Google 深度集成),部分是生态原因(protobuf 早就是 Google 内部的通用 lingua franca,工具链、文档、社区都最成熟)。P0-01 已经说过:**这不是 protobuf 绝对比 Thrift 强,而是生态与工具链的选择**。本书不卷这种选型之争,只拆"它凭什么能扛住跨语言 + 兼容性这两件大事"。

---

## 二、`.proto` 的四大构件:各回答一个根本问题

把 helloworld 那个例子拆开,`.proto` 其实就四大类构件。每一个都不是凭空设计的,而是各回答一个 RPC 接口定义里逃不掉的根本问题。

### 构件一:`package` —— 回答"这是谁的接口"

```protobuf
package helloworld;
```

一个大型系统里,可能有几十个服务,每个服务都有自己的 `User`、自己的 `Request`、自己的 `Greeter`。如果没有命名空间,大家撞名撞到怀疑人生。`package` 就是 protobuf 的命名空间,它会被拼进生成的**全限定名**(fully-qualified name):`helloworld.Greeter`、`helloworld.HelloRequest`。这个全限定名还会变成 gRPC 调用时的**方法路径**(后面 P1-04 会看到,生成出来的方法名是 `/helloworld.Greeter/SayHello`,这个路径就是 HTTP/2 的 `:path` 头)。

> **不这样会怎样**:没有 package,两个团队各写一个 `User` message,生成的代码合并到一起就编译错误。package 把"这是谁的接口"这件事在编译期钉死。

### 构件二:`message` —— 回答"传什么数据"

```protobuf
message HelloRequest {
  string name = 1;
}

message Point {
  int32 latitude = 1;
  int32 longitude = 2;
}

message Rectangle {
  Point lo = 1;   // 嵌套另一个 message
  Point hi = 2;
}
```

`message` 是 protobuf 描述**数据结构**的基本单位。注意三个设计:

1. **每个字段有类型**:`string`、`int32`、`bool`、`Point`(另一个 message,即嵌套)。这是编译期类型安全的基础。
2. **每个字段有字段号**:`name = 1`、`latitude = 1`、`longitude = 2`。这个号是 protobuf 的灵魂,后半节单独拆。
3. **可以嵌套**:`Rectangle` 里嵌套两个 `Point`。这让你能像搭积木一样,把复杂数据结构从简单的 message 拼出来。

看 gRPC 仓库里的 [`examples/protos/route_guide.proto`](../grpc/examples/protos/route_guide.proto#L59-L72),`Rectangle` 就是两个对角的 `Point` 拼出来的,这是"用小 message 组大 message"的典型写法。

`message` 还支持几个高级字段类型:

- **`repeated`**:一个字段可以有多个值(类似数组/列表)。`repeated Point points = 1;` 表示 `points` 是一个 `Point` 数组。
- **`map<K, V>`**:键值对映射(类似 dict/HashMap)。看 gRPC 自己的测试 proto [`src/proto/grpc/testing/messages.proto`](../grpc/src/proto/grpc/testing/messages.proto#L251) 里:
  ```protobuf
  map<string, int32> rpcs_by_peer = 1;
  ```
  这其实是 `repeated MapEntry` 的语法糖,每个 `MapEntry` 是个有两个字段(`key`、`value`)的嵌套 message。**这个"语法糖"的真相很重要**:map 在 wire format(线上字节)层面和 repeated 是等价的,所以老代码(不认识 map)也能解析它(当成 repeated 读出来)。这是兼容性的另一个巧妙之处。
- **`oneof`**:一组字段里,同一时刻**最多只有一个**被设置。看 gRPC 的 server reflection 协议 [`src/proto/grpc/reflection/v1/reflection.proto`](../grpc/src/proto/grpc/reflection/v1/reflection.proto#L44-L56):
  ```protobuf
  oneof message_request {
    string file_by_filename = 3;
    string file_containing_symbol = 4;
    ExtensionRequest file_containing_extension = 5;
    ...
  }
  ```
  这表示一次 reflection 请求,**要么**按文件名查、**要么**按符号名查、**要么**按扩展查——只能选一种。`oneof` 是"互斥字段组"的精确表达,比"定义一堆可选字段、靠人记得只填一个"安全得多(设置 `oneof` 里任意一个字段,会自动清掉同组其他字段)。
- **`enum`**:枚举。
- **`reserved`**:保留字段号或字段名,防止以后复用一个已删的字段号引发兼容性灾难(后面讲字段号时回来看这个)。

### 构件三:`service` + `rpc` —— 回答"调什么方法"

```protobuf
service Greeter {
  rpc SayHello (HelloRequest) returns (HelloReply) {}
  rpc SayHelloStreamReply (HelloRequest) returns (stream HelloReply) {}
}
```

`service` 是一组相关 RPC 方法的集合;`rpc` 声明一个方法,指明它的**请求类型**和**响应类型**。这就是 P0-01 说的"显式契约"的落点:接口不再是隐藏的,而是写在文件里、可读、可校验、可生成代码的。

注意 `stream` 关键字——它标注的是 P0-01 讲的四种调用模式:

| 写法 | 客户端 | 服务端 | 模式 |
|------|--------|--------|------|
| `rpc M(Req) returns (Resp)` | 1 个 | 1 个 | Unary |
| `rpc M(Req) returns (stream Resp)` | 1 个 | N 个 | Server streaming |
| `rpc M(stream Req) returns (Resp)` | N 个 | 1 个 | Client streaming |
| `rpc M(stream Req) returns (stream Resp)` | N 个 | N 个 | Bidirectional |

helloworld.proto 里恰好三种都出现了(`SayHello` unary、`SayHelloStreamReply` server-streaming、`SayHelloBidiStream` bidi),是四种模式的活教材。route_guide.proto 里四种齐了(还多了 client-streaming `RecordRoute`)。

> **钉死这件事**:`service`/`rpc` 描述"**调什么**",`message` 描述"**传什么**"。一份 `.proto` 把这两件事一次性、语言无关地写死,这就是契约的全部。后面 P1-04 会看到,protoc 拿着 `service` 生成客户端 Stub 和服务端 Service 基类,拿着 `message` 生成各语言的序列化/反序列化代码——一份契约,N 种语言,全套类型安全。

### 构件四:`option` —— 回答"语言相关的细节怎么挂"

```protobuf
option java_package = "io.grpc.examples.helloworld";
option java_outer_classname = "HelloWorldProto";
option objc_class_prefix = "HLW";
```

protobuf 是语言无关的,但**生成的代码要落到具体语言**,而每种语言有自己的命名习惯:Java 要包名、Objective-C 要类名前缀、Go 要 package 路径。`option` 就是挂这些"语言相关细节"的地方。它不影响契约本身(接口、字段、类型都不变),只影响生成的代码长什么样。

> **不这样会怎样**:如果要把"Java 包名"这种语言相关的东西塞进 message 定义本身,protobuf 就不再是语言无关的了——你会看到一堆 `java_xxx`、`go_xxx`、`python_xxx` 字段堆在 message 里,乱成一锅。`option` 把"语言无关的接口定义"和"语言相关的代码生成配置"干干净净地分开,这是个漂亮的设计。

---

## 三、字段号:protobuf 兼容性的根(本章灵魂)

前面一直在铺垫"字段号"。现在单独把它钉死,因为**这是整本书里"为什么 protobuf 能做到向前/向后兼容"的唯一答案**。

### 字段号是什么

每个字段后面那个数字:

```protobuf
message HelloRequest {
  string name = 1;   // ← 这个 1
}
```

新手第一眼会以为它是"字段的默认值"或"字段的顺序编号"。都不是。**它是这个字段在 wire format(线上字节)里的唯一身份标识**。当 protobuf 把这个 message 序列化成字节时,它不会写"这里有个字段叫 name",而是写"这里有个字段,编号是 1,类型是 string,值是 xxx"。字段名 `name` **根本不出现在线上字节里**(P1-03 会拆透字节布局)。

### 为什么用字段号,不用字段名?

这是本书最关键的"为什么"之一。回答这个问题,要看"如果用字段名会怎样"。

> **不这样会怎样(用字段名定位)**:如果线上字节里写的是字段名 `"name": "world"`,那会发生什么?
>
> 1. **带宽浪费**:每次都把 `"name"` 这串字符发一遍。一个 `user_id` 字段,发 100 万次调用就发 100 万次 `"user_id"` 这 7 个字节。字段名越长,浪费越狠(很多字段名比值还长)。
> 2. **兼容性脆弱**:今天叫 `name`,明天有人觉得"应该叫 `user_name` 更清晰",改了字段名。老代码反序列化时,看到 `user_name` 不认识,当成 unknown field 丢掉——**数据无声丢失**。改个名字就破坏了所有老代码。
> 3. **解析要查表**:收方拿到字段名,要维护一张"字段名 → 类型"的表才能解析,运行时开销。

用字段号就完全不一样:**编号是一个数字**,在线上又小又紧凑(P1-03 会看到,varint 编码下小编号只占 1 字节);**编号一旦分配,就是一个永久身份**,和字段名解耦——你随便改字段名(把 `name` 改成 `user_name`),只要编号还是 1,线上字节完全不变,老代码照样解析。

> **钉死这件事**:字段号不是"为了省那几个字节"(虽然确实省),**它是为了把"字段的身份"和"字段的名字"解耦**。名字是给人看的(可以随便改),编号是给机器用的(一旦定了就不能动)。这是 protobuf 兼容性的**全部根基**。

### 这个设计换来了什么:向前 / 向后兼容

字段号这个设计,直接换来了分布式系统最梦寐以求的东西:**向前兼容(forward compatible)**和**向后兼容(backward compatible)**。

设想一个真实场景:你的 `User` 服务有 1000 个客户端在跑老版本代码,你想给它**加一个字段** `email`。

```protobuf
// 老版本
message User {
  string user_id = 1;
  string name = 2;
}

// 新版本(加了个字段)
message User {
  string user_id = 1;
  string name = 2;
  string email = 3;   // ← 新加
}
```

字段号设计下,这件事**完全无痛**:

- **新服务端发,老客户端收**:新服务端发的字节里,有编号 1、2、3 三个字段。老客户端只认识 1 和 2,对编号 3 不认识——它会把这个 unknown field **原样保留**(透传),而不是报错或丢弃(这就是 **unknown fields 透传**)。老客户端照样能用 `user_id` 和 `name`,只是看不到 `email`。
- **老客户端发,新服务端收**:老客户端发的字节里只有 1 和 2。新服务端认识 1、2、3,看到没有 3,就给 `email` 一个默认值(proto3 下 string 默认是空串)。一切正常。

> **钉死这件事**:**加字段,只要用一个新的、没用过的字段号,就不破坏任何老代码。** 这就是字段号 + unknown fields 透传换来的。在微服务场景下(成百上千个服务、各自独立部署、根本无法同时升级),这个能力是**生死攸关**的——没有它,每次加字段都要协调所有客户端一起发版,根本不现实。

### 反过来,哪些操作会破坏兼容性?

字段号设计能保住"加字段",但有几个操作是**会出事**的,必须钉死:

1. **复用一个已删的字段号**:你删了 `email = 3`,过几天又加了个 `phone = 3`——灾难。老客户端发的字节里编号 3 是 `email`,新代码当成 `phone` 解析,数据错乱。所以 protobuf 给了一个保护机制:`reserved`。
   ```protobuf
   message User {
     reserved 3;              // 保留编号 3,谁也不许再用
     reserved "email";        // 也可以保留字段名
   }
   ```
   `reserved` 是给"这个号/这个名字以前用过、现在不用了,但谁也不许再碰"的明确警告,编译器会拦住任何复用尝试。
2. **改字段的 wire type(线上类型)**:把 `int32 age = 1` 改成 `string age = 1`——编号没变,但类型变了。老代码按 int32 解析,新代码按 string 写,对不上。**字段号不变,但 wire type 不兼容的改动,等于悄悄改了协议**。正确做法是用新编号。
3. **改字段的编号**:把 `name = 2` 改成 `name = 5`——等于废了老的字段,老代码发的编号 2 现在没人认了。这不是"加字段",是"换字段",会破坏兼容。
4. **proto3 把一个 required 字段删掉**(proto3 其实没有 required,但 proto2 有)——见后面的 editions 讨论。

> **所以这样设计**:字段号 + `reserved` + 不改 wire type,这套组合让"协议演进"变成可控的:加字段无痛、删字段留痕、改字段用新编号。这是 protobuf 在生产里能扛住十几年演进的核心机制。本书后面 P1-03 会拆 unknown fields 透传的字节级实现,这里先记住结论。

---

## 四、`proto2` / `proto3` / `editions`:一段教训满满的选型史

`.proto` 文件开头那行 `syntax = "proto3";`,声明用的是哪个 protobuf 版本。这一节讲清三个版本的取舍——它本身就是"API 设计如何演进"的活教材,理解了它,你就理解了 protobuf 字段语义的本质。

### proto2:required / optional / repeated 三态

最早的 protobuf(proto2)给每个字段三种修饰符:

- `required`:序列化时**必须**填这个字段,没填直接报错。看起来很"严格"、很"安全"。
- `optional`:可填可不填,没填就给默认值。
- `repeated`:可以有多个值。

`required` 听起来美好,实际上是个**陷阱**。设想:你给 `User` 加了个 `required string email = 3`。看起来在强制"每个 User 必须有 email"。可一旦部署,噩梦开始——你想把 `email` 改成 optional(因为后来发现有些场景确实没 email),**改不了**:所有老客户端还在发 required 的 email,新服务端如果把它改成 optional,解析老字节不会报错,但语义对不上;更糟的是,如果哪个老客户端漏发了 email,直接报错,调用失败,而且**没法向后兼容地修复**(因为"required"就是"必须",你想放松它,等于改协议)。

> **不这样会怎样**:`required` 字段把你锁死在"这个字段永远必须有"的承诺上。一旦业务变了,你想放松,就破坏兼容性。Google 内部因为这个坑吃过大亏,最终结论是:**永远不要用 required**。

### proto3:全部 optional,repeated 仍在

吸取教训后,protobuf 3 做了个激进的决定:**彻底删掉 required**。proto3 里,所有非 repeated 字段默认都是 optional(而且连 `optional` 关键字一度都不让写),没有"必须填"这回事。

但 proto3 又走了另一个极端:它一度**连 `optional` 关键字都禁了**(只能写 `string name = 1;`,不能写 `optional string name = 1;`)。这导致一个尴尬:proto3 里,一个字段"没被设置"和"被设置成了默认值(比如空串、0)",**无法区分**——因为没设置时也返回默认值。这对很多业务(比如"用户有没有填 phone"这种判断)是真实痛点。

后来 protobuf 团队在 proto3 里又**把 `optional` 关键字加了回来**(叫 "field presence"),这才解决了"区分未设置和默认值"的问题。

### editions:把"字段语义"变成可选特性

经历了 proto2 的 required 坑、proto3 的 presence 坑,protobuf 团队想通了:**协议版本号(syntax = "proto3")是个糟糕的版本化机制**——你想给 proto3 加个小特性(比如让字段可以显式标 has-presence),都得纠结"这还算不算 proto3"。

于是有了 **editions**(2023 起成为推荐写法):

```protobuf
edition = "2023";
```

editions 不再用"大版本号",而是用**逐项特性(feature)开关**:你可以显式声明"我这个字段要 presence 语义"、"我这个字段要 packed 编码"、"我这个字段是 required"。每个特性可以独立开关,而不是被绑死在一个大版本里。这是 API 设计上的成熟——**从"版本号驱动"转向"特性驱动"**。

> **钉死这件事**:proto2 的 required、proto3 的 presence 缺失,都是 Google 团队用十几年血泪换来的教训。editions 是这些教训的沉淀。**对 gRPC 用户来说,现在的推荐是:新项目用 `edition = "2023"`,老项目维持 proto3,绝对不要用 proto2 的 required**。本书所有示例都用 proto3(因为 gRPC 仓库里的示例都是 proto3,见 [`helloworld.proto`](../grpc/examples/protos/helloworld.proto#L15) 的 `syntax = "proto3";`),但你要知道这些版本背后的故事——它解释了为什么字段语义是现在这个样子。

---

## 五、gRPC core 怎么对待这份契约

讲完了 `.proto` 本身,看 gRPC C++ core 怎么和这份契约打交道——这能帮你建立"契约层和后面的层怎么衔接"的直觉。

### gRPC core 不含 protobuf 库本身

先说一个容易被忽略的事实:**gRPC 仓库里没有 protobuf 库的源码**。protobuf 是一个独立的项目(`google/protobuf`,也叫 Protocol Buffers),gRPC 只是**依赖**它。本书源码根 `../grpc/` 里,你找不到 `third_party/protobuf/`——protobuf 是作为外部依赖链接进来的。

> **诚实标注**:这意味着本章讲的 protobuf 编码规则(varint、tag、wire format),是 **protobuf 的规范**,不是 gRPC 源码里能直接 grep 到的实现。本书的策略是:① 用 protobuf 官方规范讲清"字段号怎么变成字节"(P1-03 拆透);② 对照 gRPC **自己**写的、设计同源的变长整数实现——`src/core/ext/transport/chttp2/transport/varint.cc`(这是 gRPC 自家代码,可以逐行讲),帮你建立"变长编码为什么这么设计"的直觉。

### gRPC 把契约用在哪三个地方

gRPC core 对 protobuf 的依赖,集中在三个地方:

1. **代码生成时**:protoc + `grpc_cpp_plugin`(P1-04 的主角)拿着 `.proto`,生成出各语言的 Stub 和 Service 基类。这是契约变成可调用代码的环节。
2. **运行时序列化/反序列化**:生成的 Stub 在发请求前,调 protobuf 库把 message 编码成字节;收响应后,调 protobuf 库把字节解码回 message。这是 P1-03 讲的编码层。
3. **server reflection(服务反射)**:gRPC 有一个标准服务 [`grpc.reflection.v1.ServerReflection`](../grpc/src/proto/grpc/reflection/v1/reflection.proto#L31-L36),它本身就是用 gRPC + protobuf 定义的,让客户端**运行时**查询"服务端有哪些 service、每个 service 有哪些方法、每个 message 长什么样"。这是契约的**运行时自省**,实现里用了 protobuf 的 descriptor 机制(`src/cpp/ext/proto_server_reflection.cc`)。这又一次体现了"契约是 first-class citizen":它不仅能生成代码,还能在运行时被查询。

> **钉死这件事**:gRPC 和 protobuf 的关系是**深度绑定**——gRPC 用 protobuf 定义接口、用 protobuf 编码消息、用 protobuf descriptor 做反射。这种绑定是 gRPC 选 protobuf(而非 Thrift/JSON)的根:IDL、编码、反射,一套打通,没有接缝。

---

## 六、技巧精解:字段号定位与 `reserved` 的"协议防腐"

按本书惯例,正文后、小结前,挑本章最硬核的技巧单独钉死。本章的技巧精解,钉的就是**字段号定位 + `reserved` 这个组合**——它不是"省几个字节"的小优化,而是"让协议能演化十几年不腐烂"的工程奇迹。

### 朴素地写会怎样:位置定位的灾难

为了感受字段号的妙,先看"朴素地写"会撞什么墙。最朴素的序列化设计是**按字段定义顺序、固定位置**排:

```
// 朴素设计:按顺序,第 1 个字段固定在位置 0,第 2 个在位置 1...
User { user_id, name }
序列化: [user_id 的字节][name 的字节]
```

这个设计看起来简洁,但**加字段就是灾难**:

- 如果你想在 `user_id` 和 `name` 中间插一个 `email`,那 `name` 的位置就往后挪了。老代码按"位置 1 是 name"解析,现在位置 1 是 email,直接错乱。
- 如果你想加字段,只能加在**末尾**(给老字段挪位置就会破坏老代码)。可即便加在末尾,老代码也不知道有这个新字段——它按固定长度读,读到末尾就停,新字段被无声忽略(而且这次不是"透传",是真的丢了,因为位置定位没法表达"这里有个你没期待的额外字段")。
- 删字段更惨:中间删一个,后面所有字段位置全乱。

这就是位置定位(location-based)的致命伤:**它把"字段"和"位置"死死绑在一起,任何插入、删除都会让位置错位**。

### 字段号定位妙在哪

字段号定位(tag-based)彻底解开了这个绑死:

```
// protobuf 设计:每个字段自带编号,编号 = 身份
序列化: [tag=1, type=string, value=user_id 的字节][tag=2, type=string, value=name 的字节]
```

每个字段在线上是**自描述的**——它带着自己的编号和类型。这下:

- **加字段**:新字段给个新编号(比如 3),老代码看到编号 3 不认识,但它知道"这是个我没期待的额外字段",可以**原样保留**(unknown fields 透传),而不是按位置读乱。兼容性保住了。
- **删字段**:`reserved 3` 把编号 3 永久封印,谁也不许再用,防止"老字节里的编号 3 被新代码当成别的字段"。兼容性保住了。
- **字段顺序无关**:线上字段的先后顺序无所谓,收方按编号找字段,不是按位置。这让 protobuf 可以**按字段号/类型重新排列字段**以优化编码(比如把同类型字段挨着放,packed repeated,P1-03 会讲)。

> **不这样会怎样**:位置定位下,协议一旦发布就像浇了水泥,任何字段调整都要等所有客户端一起升级。字段号定位下,协议是**可演化的有机体**:加字段无痛、删字段留痕、改字段用新号。这就是为什么 protobuf 能在 Google 内部、在 gRPC 生态里,扛住十几年、几千个团队、几十亿调用/秒的协议演进。

### `reserved`:字段号的"墓碑"

`reserved` 是字段号设计的点睛之笔。它本质上是给字段号立一块**墓碑**:

```protobuf
message User {
  reserved 3, 4;          // 这两个号曾经用过(可能是 email、phone),现在删了
  reserved 15 to 25;      // 也可以声明一个区间
  reserved "email", "phone";  // 字段名也能保留

  string user_id = 1;
  string name = 2;
}
```

这块墓碑有两个作用:

1. **警告后人**:任何尝试用 `reserved` 过的编号或名字定义新字段,**protoc 编译期直接报错**。这防止了"半年后有人不知道编号 3 用过,给它分配了新字段,结果老字节里的编号 3 被错误解析"这种最难排查的 bug。
2. **文档**:它明确记录了"这个协议历史上曾有过这些字段",给后续维护者一份"协议考古"的线索。

> **钉死这件事**:字段号 + `reserved` 的组合,本质上是把"协议的演化历史"编码进了契约本身。**一个有十年历史的成熟 `.proto`,往往 reserve 了一堆编号**——那不是 bug,那是它扛住了十年演化的勋章。朴素的位置定位协议,根本没有承载这种演化历史的能力。

### 字段号的编号策略:小号省字节,大号留扩展

最后补一个实战细节:字段号**直接影响线上字节数**。P1-03 会拆透,这里先给结论——protobuf 编码里,字段号和类型会打包成一个 tag,字段号越小,tag 越短(编号 1~15 只占 1 字节 tag,编号 16~2047 占 2 字节,编号再大占更多)。

所以实战里有个编号策略:

- **最常用、最频繁出现的字段,给小编号**(1~15),省字节。
- **不常用或将来可能加的字段,留大编号区间**(比如从 100 开始),给"小号资源"留余量。
- **预留给将来扩展的号段**:有些团队约定"1~99 是业务字段,100~199 是基础设施字段,1000+ 是预留",让协议演化有章法。

这是字段号设计的另一个妙处:**它不只是身份标识,还是影响编码效率的旋钮**。编号策略和 P1-03 的 varint 编码紧密相关——小号省字节这件事,只有理解了 varint 才能完全明白为什么。

---

## 七、章末小结

### 回扣主线

本章是全书"**协议层 vs 框架层**"二分法里,协议层的**第一块地基**。它回答的是"跨语言调用凭什么不乱套"——答案是一份语言无关的契约。具体来说:

- `service` / `rpc` 描述**调什么**(接口);
- `message` 描述**传什么**(数据);
- **字段号**给每个字段一个永久的、和名字解耦的身份,换向前/向后兼容;
- `option` 挂语言相关的代码生成细节,保持契约本身的语言无关。

这一章**不涉及字节怎么过线**(那是 P1-03)、也**不涉及调用怎么发起**(那是 P1-04)——它是纯粹的接口语义层,和架构演进(callback → Promise)完全无关,无论新旧架构都站在它上面。

### 五个为什么

1. **为什么跨语言调用必须先有契约?**——口头协议(JSON 字段名)会命名漂移、类型不一致、加字段不通知、错误全推迟到运行时。契约把"接口约定"从口头变成机器可校验、版本化、可代码生成的合同,错误前移到编译期。

2. **为什么 protobuf 用字段号而非字段名定位字段?**——字段号把"字段的身份"和"字段的名字"解耦:名字是给人看的(可随便改),编号是给机器用的(一旦定了不动)。换来了向前/向后兼容:加字段(用新编号)不破坏老代码。字段名在线上还省字节。

3. **为什么 protobuf 加字段无痛、删字段要 reserved?**——加字段用新编号,老代码看到不认识的编号会 unknown fields 透传(原样保留),不报错不丢;删字段若不 reserved,新代码可能复用这个号导致老字节被错误解析,所以 `reserved` 给删掉的号立墓碑,编译期拦住复用。

4. **为什么 proto3 删了 required、又把 optional 加回来?**——proto2 的 required 把你锁死在"字段永远必须有"的承诺上,业务一变就破坏兼容,Google 吃过大亏;proto3 删了 required,但一度连 optional 也禁了导致无法区分"未设置"和"默认值",最后把 optional(presence)加回来。editions 把字段语义改成逐项特性开关,是这些教训的沉淀。

5. **为什么 gRPC core 不含 protobuf 库源码?**——protobuf 是独立项目(`google/protobuf`),gRPC 只是依赖它。gRPC 在三个地方用 protobuf:代码生成(protoc + plugin)、运行时序列化(Stub 调 protobuf 库编解码)、server reflection(用 protobuf descriptor 做运行时自省)。这种深度绑定是 gRPC 选 protobuf 的根。

### 想继续深入往哪钻

- **想看真实 `.proto` 长什么样**:`../grpc/examples/protos/` 下一堆示例,从最简单的 [`helloworld.proto`](../grpc/examples/protos/helloworld.proto) 到四模式齐全的 [`route_guide.proto`](../grpc/examples/protos/route_guide.proto),都是现成教材。
- **想看 `oneof` / `map` 在真实协议里怎么用**:gRPC 自己的 reflection 协议 [`src/proto/grpc/reflection/v1/reflection.proto`](../grpc/src/proto/grpc/reflection/v1/reflection.proto) 用了 `oneof` 表达"三种查询方式选一";测试 proto [`src/proto/grpc/testing/messages.proto`](../grpc/src/proto/grpc/testing/messages.proto#L251) 用了 `map<>`。
- **想深入 protobuf 字段语义**:读 protobuf 官方文档的 "Language Guide (proto3)" 和 "Editions Guide",以及关于 field presence、unknown fields 的语义说明。
- **想看兼容性规则全集**:protobuf 官方有 "Updating A Message Type" 一节,列出了哪些改动安全、哪些不安全,是实战参考。
- **想看 gRPC 怎么把这份契约变成可调用的代码**:就是下一章 P1-04。

### 引出下一章

我们搞清楚了"为什么跨语言要契约"和"字段号凭什么换兼容性"。但字段号还只是一个**抽象的身份**——它怎么变成线上实实在在的字节?为什么 protobuf 编码比 JSON 又小又快?那个"小号省字节"到底省在哪?下一章 P1-03,我们拆 protobuf 的 wire format:varint 变长整数、zigzag 有符号编码、tag-length-value 布局、packed repeated、unknown fields 透传的字节级实现。还会对照 gRPC **自己**写的变长整数实现 [`varint.cc`](../grpc/src/core/ext/transport/chttp2/transport/varint.cc),看看"变长编码"这个设计在 gRPC 自家的 HTTP/2 帧里也出现过——同一套思想,两个地方落地。

> **下一章**:[P1-03 · protobuf 编码:为什么又小又快](P1-03-protobuf编码-为什么又小又快.md)
