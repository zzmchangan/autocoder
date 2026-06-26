# 附录 B · Envoy 工具链与实践

> 正篇 23 章把 Envoy 的原理拆透了:一条流量怎么从 listener 一路穿到 cluster,xDS 怎么把配置动态推下去、不停机热更新。但原理懂了,线上真撞上问题时——某个 pod 突然 503 飙升、某条 VirtualService 改了不生效、某次证书轮换后 mTLS 握手失败——你还需要一份**实操与排障手册**:知道该敲哪个命令、看哪个端点、读哪个字段、按什么顺序排查。这本附录就是干这个的。它不是新机制的讲解,而是把正文里讲过的那些机制(stats、circuit breaker、xDS、outlier detection、hot restart、mTLS)落到**具体命令和排障路径**上,给你一份"线上用 Envoy 的工具箱"。

> **定位**:参考材料,实战清单。读完你该能:① 熟练用 envoy admin API 的三四十个端点查任意内部状态;② 在 Istio 环境里用 `istioctl proxy-config` 替代手敲 admin 端点;③ 用 `envoy --mode validate` 在上线前校验配置;④ 动手做一次 hot restart 升级并确认交接;⑤ 撞上 503 / 熔断 / 配置不生效 / 延迟突增 / TLS 失败 / 内存涨时,知道第一步查什么。

---

## 一、envoy admin API:Envoy 自带的"调试后门"

线上跑的每个 Envoy 进程,都内置一个 **admin HTTP server**——一个绑在本地端口、只接受 HTTP 请求的"调试后门"。它不是控制面(不发配置),而是一个**只读 + 少量写**的内省接口:你可以通过它 dump 当前生效的全部配置(`config_dump`)、查所有 cluster 的健康与熔断状态(`clusters`)、查所有 listener(`listeners`)、查所有 stats(`stats`)、动态调日志级别(`logging`)、触发 drain(`drain_listeners`)、甚至直接让进程退出(`quitquitquit`)。

正篇 P6-20 讲 stats、P4-15 讲 circuit breaker、P5-18 讲 listener drain 时,都顺带提过 admin 端点;这一节把它们系统列一遍,每个端点干什么、怎么用、对应正文哪一章。

### 1.1 admin 端点的注册:源码里长什么样

admin 端点不是配置驱动的,而是**编译期硬编码注册**的。所有端点在 `AdminImpl` 构造时,通过 `makeHandler(prefix, help_text, callback, ...)` 一个个塞进 `handlers_` 向量,实现在 [`source/server/admin/admin.cc:128-279`](../envoy/source/server/admin/admin.cc#L128-L279)(commit `df2c77d`,1.39.0-dev,下同)。每条注册长这样:

```cpp
// source/server/admin/admin.cc:131 (简化示意,非源码原文)
makeHandler("/clusters", "upstream cluster status",
            MAKE_ADMIN_HANDLER(clusters_handler_.handlerClusters), false, false,
            {{Admin::ParamDescriptor::Type::String, "filter",
              "Regular expression (Google re2) for filtering clusters by name"}}),
```

四个关键字段:`prefix`(URL 路径,如 `/clusters`)、`help_text`(一句话说明)、`callback`(处理函数)、最后两个布尔(`removable` / `mutates`)——后者标记这个端点会不会改状态(改日志级别、触发 drain、退出进程的端点 `mutates=true`)。`/help` 端点([`admin.cc:186`](../envoy/source/server/admin/admin.cc#L186))就是把这些 `help_text` 全列出来,所以你随时可以 `curl localhost:9901/help` 看当前 Envoy 支持哪些端点。

> **钉死**:端点名以源码 [`admin.cc:128-279`](../envoy/source/server/admin/admin.cc#L128-L279) 的 `makeHandler` 注册列表为准。本附录下面那张表里每一个端点,都是逐条 Grep 核实过的真实端点,不是凭记忆写的。下面所有"端点→源码 handler"的对应,可以自己 `grep makeHandler ../envoy/source/server/admin/admin.cc` 复核。

### 1.2 admin 端点全表(Grep 核实)

下表把 1.39.0-dev 注册的全部端点列出来,分四类:配置/状态查询、stats 与日志、控制(改状态)、调试/profiling。

#### A. 配置与状态查询(排障最常用)

| 端点 | 作用 | 怎么用 / 关键字段 | 承接正文 |
|------|------|------------------|---------|
| `/` | admin 首页,列出所有端点链接 | `curl localhost:9901/` | — |
| `/help` | 列出所有 admin 命令及参数说明 | `curl localhost:9901/help` | — |
| `/config_dump` | **dump 当前 Envoy 全部生效配置**(LDS/RDS/CDS/EDS/SDS/集群 全文) | `curl 'localhost:9901/config_dump'`;可加 `?resource=clusters` 只 dump 某类、`?mask=...` 用 FieldMask 切片、`?name_regex=...` 按名过滤、`?include_eds=true` 带 EDS | P5-16/P5-17 xDS |
| `/clusters` | **dump 所有 upstream cluster 状态**:每个 host 的 health_flags、weight、circuit breaker 计数、outlier success_rate | `curl localhost:9901/clusters`;`?format=json` 出 JSON;`?filter=<re2>` 按名过滤。输出形如 `cluster_name::host::health_flags::0/0/0/0/0/0/0/0`、`::cx_active::`、`::rq_active::`、`::max_connections::`、`::max_requests::` 等(见 1.3) | P4-12/P4-14/P4-15 |
| `/listeners` | dump 所有 listener(name + 监听地址 + 状态) | `curl 'localhost:9901/listeners'`;`?format=json` 出 JSON。可看 listener 是不是 warming / draining | P2-05/P5-18 |
| `/init_dump` | dump 各 init manager 还在等哪些 target 就绪(实验性) | `curl 'localhost:9901/init_dump?mask=listener'` 看 listener warming 卡在哪 | P5-18 warming |
| `/server_info` | 进程版本、状态(PRE_INITIALIZING/INITIALIZING/LIVE/DRAINING)、Uptime、hot restart epoch | `curl localhost:9901/server_info` | P6-21 hot restart |
| `/ready` | 探活:LIVE 返回 200,否则 503。**给 k8s readinessProbe 用** | `curl -i localhost:9901/ready` | — |
| `/hot_restart_version` | 打印 hot restart 兼容版本号(用于确认新旧二进制能交接) | `curl localhost:9901/hot_restart_version` | P6-21 |
| `/certs` | 打印进程当前加载的证书(含 PEM、SAN、过期时间) | `curl localhost:9901/certs` | P6-21 mTLS/SDS |

#### B. stats 与日志

| 端点 | 作用 | 怎么用 | 承接正文 |
|------|------|------|---------|
| `/stats` | **dump 所有 stats**(counter/gauge/histogram)。支持 format、filter、histogram_buckets、type、usedonly 等参数 | `curl 'localhost:9901/stats?format=json&filter=^cluster\.'`;`?histogram_buckets=detailed` 看每桶;`?type=Histograms` 只看延迟分布 | P6-20 |
| `/stats/prometheus` | 同上但输出 Prometheus exposition 格式,**给 Prometheus scrape 用** | `curl localhost:9901/stats/prometheus`;`?usedonly=true` 只导被写过的 | P6-20 |
| `/stats/recentlookups` | 最近 stat 名查找记录(配合 symbol table 调试) | `/stats/recentlookups/enable` 开、`/disable` 关、`/clear` 清 | P6-20 symbol table |
| `/contention` | 互斥锁竞争统计(需编译时开 `envoy.locks`)| `curl localhost:9901/contention` | — |
| `/logging` | **查/改日志级别**(全部 logger 或按名)。改完立即生效,不需重启 | `curl 'localhost:9901/logging?level=debug'`(全开 debug);`?paths=router:debug,upstream:trace`(分级);`?group=router:debug` | P6-20 |
| `/runtime` | dump 当前 runtime 变量(含 override) | `curl localhost:9901/runtime` | P4-15 runtime 覆盖 |
| `/runtime_modify` | **运行时改 runtime 变量**(可调 circuit breaker 上限等,见 1.4) | `curl -X POST 'localhost:9901/runtime_modify?key1=val1'`;空值删除 override | P4-15 |
| `/reset_counters` | 把所有 counter 归零(gauge/histogram 不动) | `curl -X POST localhost:9901/reset_counters` | P6-20 |
| `/reopen_logs` | 重开 access log 文件(logrotate 后用) | `curl -X POST localhost:9901/reopen_logs` | P6-20 |

#### C. 控制(改状态 / 触发动作)

| 端点 | 作用 | 怎么用 | 承接正文 |
|------|------|------|---------|
| `/drain_listeners` | **触发 listener drain**。可 `graceful`(走 drain 周期再关)、`skip_exit`(drain 完不退出)、`inboundonly`(只 drain inbound listener,sidecar 场景) | `curl -X POST 'localhost:9901/drain_listeners?graceful'` | P5-18 drain |
| `/healthcheck/fail` | 让 Envoy 主动对外健康检查失败(摘流) | `curl -X POST localhost:9901/healthcheck/fail` | — |
| `/healthcheck/ok` | 恢复健康检查通过 | `curl -X POST localhost:9901/healthcheck/ok` | — |
| `/quitquitquit` | 让进程退出(优雅:先 drain 再退) | `curl -X POST localhost:9901/quitquitquit` | P6-21 |
| `/memory` | 打印当前 allocation/heap 用量 | `curl localhost:9901/memory` | P4-15 overload |
| `/memory/tcmalloc` | 打印 TCMalloc 详细统计(若启用) | `curl localhost:9901/memory/tcmalloc` | P4-15 |

#### D. 调试 / profiling

| 端点 | 作用 |
|------|------|
| `/cpuprofiler?enable=y\|n` | 开/关 CPU profiler |
| `/heapprofiler?enable=y\|n` | 开/关 heap profiler |
| `/heap_dump` | dump 当前 heap |
| `/peak_heap_dump` | dump peak heap |
| `/allocprofiler?enable=y\|n` | 开/关 allocation profiler |

> **统计**:Grep [`admin.cc:128-279`](../envoy/source/server/admin/admin.cc#L128-L279) 的 `makeHandler` 调用,加上 `stats_handler_.statsHandler(false)`([`stats_handler.cc:214`](../envoy/source/server/admin/stats_handler.cc#L214))动态注册的 `/stats`,**共约 36 个端点**。每个端点的 `help_text` 都能通过 `/help` 在线看到。

### 1.3 `/clusters`:排障头号端点,字段含义

线上 90% 的"503/慢/连不上"问题,第一步都是 `curl localhost:9901/clusters`。这个端点把所有 cluster 的每个 host 状态打出来,文本格式(由 [`clusters_handler.cc:222`](../envoy/source/server/admin/clusters_handler.cc#L222) 的 `writeClustersAsText` 生成),每个 host 一组字段:

```
outbound|8080||api.default.svc.cluster.local::10.0.1.5:8080::health_flags::healthy
outbound|8080||api.default.svc.cluster.local::10.0.1.5:8080::weight::1
outbound|8080||api.default.svc.cluster.local::10.0.1.5:8080::cx_active::0
outbound|8080||api.default.svc.cluster.local::10.0.1.5:8080::cx_total::42
outbound|8080||api.default.svc.cluster.local::10.0.1.5:8080::rq_active::0
outbound|8080||api.default.svc.cluster.local::10.0.1.5:8080::rq_total::1003
outbound|8080||api.default.svc.cluster.local::10.0.1.5:8080::rq_2xx::1000
outbound|8080||api.default.svc.cluster.local::10.0.1.5:8080::rq_5xx::3
outbound|8080||api.default.svc.cluster.local::default_priority::max_connections::1024
outbound|8080||api.default.svc.cluster.local::default_priority::max_pending_requests::1024
outbound|8080||api.default.svc.cluster.local::default_priority::max_requests::1024
outbound|8080||api.default.svc.cluster.local::default_priority::max_retries::3
outbound|8080||api.default.svc.cluster.local::10.0.1.5:8080::success_rate::-1
outbound|8080||api.default.svc.cluster.local::10.0.1.5:8080::outlier::ejections_total::1
```

(字段顺序与实际输出可能略有差异,以 `?format=json` 为准;字段名见 [`clusters_handler.cc:234-289`](../envoy/source/server/admin/clusters_handler.cc#L234-L289))

排障时盯这几个字段:

- **`health_flags`**:`HostUtility::healthFlagsToString` 生成([`clusters_handler.cc:269`](../envoy/source/server/admin/clusters_handler.cc#L269)),一组布尔拼成的字符串(每个 active/passive health check 一个位)。`healthy` = 全过;`unhealthy` = 被主动健康检查判挂;`ejected` = 被 outlier detection 踢出(P4-14);`excluded_via_immediate` = 立即逐出。**这个字段是判断"后端能不能接活"的第一指标**。
- **`cx_active` / `rq_active`**:当前活跃连接数 / 活跃请求数。持续高位不下来,说明请求都在排队等后端(慢后端征兆)。
- **`cx_overflow` / `rq_overflow` / `rq_pending_overflow`**:**这三个一旦 > 0,就是 circuit breaker 在拒**(P4-15)。`cx_overflow` = 建连接被拒,`rq_overflow` = 挂 stream 被拒,`rq_pending_overflow` = 排队被拒。它们对应正文里 `upstream_cx_overflow` / `upstream_rq_pending_overflow` 那些 stat。
- **`max_connections` / `max_requests` / `max_retries`**:`addCircuitBreakerSettingsAsText` 输出([`clusters_handler.cc:18-32`](../envoy/source/server/admin/clusters_handler.cc#L18-L32)),就是当前生效的 circuit breaker 上限。被 runtime override 过的会显示 override 后的值。
- **`success_rate` / `local_origin_success_rate`**:outlier detection 算出的成功率(P4-14)。`-1` 表示样本不足(还没攒够请求),负值之外的低值会被 outlier 判 ejected。
- **`outlier::ejections_total`** / `::success_rate_ejection::...` / `::consecutive_5xx::...`:outlier 的逐出计数与原因。

JSON 格式(`?format=json`)字段更全,字段名见 [`envoy/admin/v3/clusters.proto`](../envoy/api/envoy/admin/v3/clusters.proto)(`ClusterStatus`、`HostStatus`、`circuit_breakers.thresholds`)。

### 1.4 `/runtime_modify`:不停机调 circuit breaker 上限

正文 P4-15 讲过,circuit breaker 的上限可以被 runtime 覆盖([`BasicResourceLimitImpl::max()`](../envoy/source/common/common/basic_resource_impl.h#L32))。线上发现某个 cluster 的熔断阈值不合适,不必改配置重启,直接:

```bash
# 把 cluster "api" 的 DEFAULT 优先级 max_connections 调到 2048
curl -X POST 'localhost:9901/runtime_modify?api.default.circuit_breakers.default.max_connections=2048'

# 调 max_retries
curl -X POST 'localhost:9901/runtime_modify?api.default.circuit_breakers.default.max_retries=10'
```

改完立刻生效(下一次 `canCreate()` 读 `max()` 就拿到新值),`/clusters` 里看到的 `max_connections` 也会变。空值删除 override、回到配置里的原始值。这是线上"调阈值不停机"的标准手法,但要小心:override 只在内存,进程重启就丢——长期方案还是要改配置或 xDS。

### 1.5 admin 端口与安全:默认只绑 localhost

admin 接口能改日志级别、触发 drain、甚至让进程退出,是高权限接口,**绝对不能暴露到公网**。Envoy 默认配置要求显式写 admin 的 `address`,运维惯例是绑到 `127.0.0.1:9901`(loopback):

```yaml
admin:
  address:
    socket_address:
      address: 127.0.0.1
      port_value: 9901
```

在 k8s sidecar 场景里,admin 端口只在 pod 内有效(Istio 默认 15000),外部访问不到。要远程访问某个 pod 的 admin,用 `kubectl exec` 或 `kubectl port-forward`:

```bash
kubectl -n default port-forward pod/api-xxxx 9901:15000
# 然后本地 curl localhost:9901/clusters
```

> **不要**:把 admin `address` 配成 `0.0.0.0` 然后用 Service 暴露——任何能访问该 Service 的人都能让 Envoy 退出或 drain 全部流量。如果非要远程访问,至少套一层 mTLS 或绑到内部网络并加网络策略。

---

## 二、istioctl:Istio 环境下的 admin 端点"易用版"

如果你用 Istio,每个 pod 里被注入的 Envoy sidecar(默认 admin 端口 15000)配的是 Istio 控制面(Istiod)通过 xDS 下发的复杂配置。手敲 `kubectl exec ... curl localhost:15000/config_dump` 太累,而且 dump 出来的 config 巨大(Istio 的配置层次多)。Istio 提供了 **`istioctl`** 这个专用工具,把最常见的 admin 操作封装成易用子命令。

> **定位**:istioctl 是 **Istio 的工具**(外部工具,不在 Envoy 源码里),它内部大部分 `proxy-config` 子命令其实是去调目标 pod 里 Envoy 的 admin 端点(主要 `/config_dump`、`/clusters`、`/listeners`、`/logging`),把结果格式化得更易读。换句话说,**istioctl proxy-config 是 admin API 的"易用包装"**,底层还是那一套。

### 2.1 五个核心子命令

`istioctl proxy-config`(简写 `istioctl pc`)有五个最常用子命令,分别对应 admin 的几个端点:

| 子命令 | 对应 admin 端点 | 干什么 |
|--------|---------------|------|
| `istioctl pc cluster <pod>` | `/clusters` | dump 某 pod 的 Envoy 看到的所有 cluster(upstream) |
| `istioctl pc listener <pod>` | `/listeners` | dump 所有 listener(含 HCM、filter chain) |
| `istioctl pc route <pod>` | `/config_dump`(RDS 部分) | dump 所有 route_config |
| `istioctl pc bootstrap <pod>` | `/config_dump`(bootstrap 部分) | dump Envoy bootstrap 配置(含 xDS server 地址) |
| `istioctl pc log <pod>` | `/logging` | **查/动态改日志级别**(无需进 pod) |

典型用法:

```bash
# 看 api pod 的所有 cluster,只看名为 outbound|8080||api 的
istioctl pc cluster api-xxxx.default --fqdn outbound|8080||api.default.svc.cluster.local

# 看 listener,带 --address 过滤
istioctl pc listener api-xxxx.default --address 0.0.0.0 --port 15006

# 看 HTTP 的路由表
istioctl pc route api-xxxx.default --name 80

# 动态把 router logger 开到 debug(进 pod 调 /logging 的等价物)
istioctl pc log api-xxxx.default --level router:debug

# 看 bootstrap,确认 Istiod xDS 地址对不对
istioctl pc bootstrap api-xxxx.default
```

每个子命令都支持 `-o json` 输出原始 protobuf JSON,字段和 admin `/config_dump` 一致。还有 `istioctl pc endpoint`(看 EDS endpoint)、`istioctl pc secret`(看 SDS 下发的证书)、`istioctl pc cluster <pod> --direction outbound` 等过滤选项。

### 2.2 istioctl vs admin:什么时候用哪个

- **能用 istioctl 就用 istioctl**:不用 `kubectl exec` 进 pod、不用记端点 URL、输出格式化好、能跨 namespace 批量查(`istioctl pc cluster -n default --all` 看 namespace 下所有 pod)。
- **istioctl 不够时回退 admin**:某些 admin 端点 istioctl 没封装(`/memory`、`/init_dump`、`/runtime_modify`、`/drain_listeners`),或者要看原始输出,这时 `kubectl exec <pod> -c istio-proxy -- curl -s localhost:15000/<endpoint>`。
- **istioctl 的局限**:它是 Istio 专用的,Envoy 裸部署(没装 Istio)用不了,只能直接打 admin。

> **小贴士**:Istio 还有个 `istioctl proxy-status`(简写 `istioctl ps`)命令,一眼看出所有 sidecar 和 Istiod 的 xDS 同步状态(STALE / SYNCED / NOT SENT),排查"xDS 没下发"时第一步就敲它。

---

## 三、配置调试:validate + config_dump 对比

正文 P5-16/P5-17 讲 xDS 时反复强调:线上"配置不生效"绝大多数是 xDS 没下发、被 NACK、或还在 warming。这一节给具体的调试步骤。

### 3.1 `envoy --mode validate`:上线前校验配置

Envoy 启动有个 `--mode` 选项([`options_impl.cc:155`](../envoy/source/server/options_impl.cc#L155)),取值 `serve`(默认,校验通过后正常服务)或 `validate`(只校验配置、不启动 worker、校验完退出)。CI/CD 流水里在真正部署前跑一遍 validate,能在上线前抓住配置语法/语义错误:

```bash
envoy --mode validate -c my-envoy.yaml
# 退出码 0 = 配置有效;非 0 = 无效,stderr 打印错误
```

validate 模式会走完整的配置加载(解析 YAML、转 proto、实例化所有 filter/cluster/listener 的工厂),但不监听端口、不连 xDS。它能抓住:YAML 语法错、proto 字段类型错、filter 名拼错、cluster 类型不支持、引用了不存在的资源等。**抓不住**的:运行时才暴露的问题(后端实际不通、证书实际过期、xDS 实际不下发)——那些要靠下面的 config_dump 对比。

Istio 场景下,Istiod 自己有一套 config 校验(`istioctl analyze`),针对 VirtualService/DestinationRule 等 Istio CRD;`envoy --mode validate` 校验的是 Istiod **生成出来**给 Envoy 的最终 xDS 配置。两者层次不同,都有用。

### 3.2 `config_dump`:看 Envoy 真正生效的配置

`curl localhost:9901/config_dump` 输出一个 JSON,顶层是 `configs` 数组,每个元素是一类配置的 dump:

- **`BootstrapConfigDump`**:bootstrap(envoy.yaml 里的静态部分)。
- **`ListenersConfigDump`**:所有 listener(LDS),含 active 和 warming,每个带 `version_info`(LDS 推过来的版本号)。
- **`RoutesConfigDump`**:所有 route_config(RDS)。
- **`ClustersConfigDump`**:所有 cluster(CDS),带 `version_info` 和 last update 时间。
- **`EndpointsConfigDump`**:所有 endpoint(EDS),按 cluster 聚合。
- **`SecretsConfigDump`**:所有 SDS 下发的 Secret(证书),含过期时间。

### 3.3 "配置不生效"的排查路径

最经典的线上投诉:"我改了 VirtualService / envoy.yaml,但流量行为没变"。排查路径:

```
  改了配置 → 行为没变
        │
        ▼
  ① 配置到 Envoy 了吗?(xDS 下发了吗?)
        │   看 config_dump 里对应资源的 version_info / 期望内容
        │   Istio:istioctl ps 看 SYNCED 还是 STALE
        │
        ├─ 没到 → 控制面没下发:看 Istiod 日志、xDS 流是否断了
        │         (config_dump 里压根没这个资源 = LDS/RDS 没推)
        │
        └─ 到了 → ② 被 NACK 了吗?
                  │   看 config_dump 里有没有 error_detail
                  │   Istio:istioctl ps 状态、Istiod 日志搜 "NACK"
                  │
                  ├─ NACK → 配置本身有问题,proto 校验失败,改配置
                  │
                  └─ ACK 了 → ③ 还在 warming 吗?
                              │   /listeners 看 listener 是不是 warming
                              │   /init_dump?mask=listener 看卡在哪个 init target
                              │   (典型:SDS 证书还没拉到,listener 在 warming)
                              │
                              └─ 都正常 → ④ 配置生效了,但路由没匹配上
                                          看 /config_dump 里的 route 规则,
                                          对照实际请求的 host/path/header,
                                          确认 match 条件没写错
```

最常见的三种"不生效":① xDS 没下发(控制面问题,看 Istiod);② 被 NACK(配置 proto 不合法);③ RDS 还没到、HCM 还在用旧 route(等几秒或看 `/listeners` 里 HCM 引用的 route_config 名)。每一种,`config_dump` 的 `version_info` / `error_detail` 都能告诉你答案。

> **字段速查**:`config_dump` 里每个动态资源都有 `version_info`(控制面最后一次成功下发的版本)、`last_updated`(Envoy 收到的时间)。如果某资源的 `last_updated` 是几小时前、而你刚才改了配置,那说明新配置根本没到——直接定位到"控制面没下发"。

---

## 四、与 Istio / gRPC / Kubernetes 集成

正文多次提到 Envoy 不是孤立跑的——它通常嵌在更大的系统里。这一节把三种最常见的集成模式讲清。

### 4.1 与 Istio 集成:sidecar 注入

Istio 是 Envoy 最大的"用户"。Istio 的架构是经典的 control plane / data plane 分离:**Istiod**(控制面)负责把用户写的 CRD(VirtualService、DestinationRule、Gateway、ServiceEntry)翻译成 Envoy 的 xDS 配置(LDS/RDS/CDS/EDS/SDS),通过一条 ADS gRPC 流推给每个数据面的 Envoy;**每个 pod 里的 Envoy sidecar**(数据面)接收并执行。

sidecar 注入流程:pod 创建时,Istio 的 mutating webhook(`istio-sidecar-injector`)拦截,往 pod spec 里注入两个容器——`istio-init`(initContainer,用 iptables 劫持 pod 进出流量到 Envoy)和 `istio-proxy`(跑 Envoy)。注入后 pod 里所有应用容器的网络流量都被 iptables 重定向到 Envoy(出站走 15001、入站走 15006),Envoy 按 Istiod 下发的策略处理(mTLS、负载均衡、重试、熔断、可观测上报)。

对应到正文,Istio 的 CRD 大致这样映射到 Envoy 概念:

| Istio CRD / 字段 | 映射到 Envoy |
|------------------|------------|
| `VirtualService`(HTTP routes) | RDS route_config |
| `DestinationRule`(host + subsets + trafficPolicy) | CDS cluster(subset = 不同 cluster)、circuit breaker、outlier、mTLS |
| `Gateway`(端口 + TLS) | LDS listener |
| `ServiceEntry`(外部服务) | CDS cluster + EDS |
| `ProxyConfig` / mesh config | bootstrap + 全局 xDS server 地址 |

所以"在 Istio 里配 Envoy 行为"= 写 Istio CRD,Istiod 翻译成 xDS,Envoy 执行。你几乎不直接写 envoy.yaml(Istio 自动生成 bootstrap),但用 `istioctl pc` 看 Envoy 真实生效的配置仍然必要——因为 CRD 到 xDS 的翻译可能和你预期不一致(见 3.3)。

### 4.2 与 gRPC 集成:Envoy 作为 gRPC 后端代理

正文 P0-01 和 P5-17 反复强调:**xDS 协议是 Envoy 定义的,gRPC 客户端复用了它**(承接《gRPC》P6-22)。Envoy 和 gRPC 的关系有两个方向:

**方向一:Envoy 作为 gRPC 流量的代理**(最常见)。后端服务用 gRPC 通信,Envoy 在中间做负载均衡、重试、可观测。这里 Envoy 的 HTTP/2 codec(P3-09)负责解析 gRPC 的 HTTP/2 帧,router(P3-11)按 `:authority` 或 `:path` 路由到 cluster。关键配置:listener 的 HCM 用 `http2_protocol_options`,cluster 也开 HTTP/2,这样 Envoy 到后端是 HTTP/2 多路复用(一条连接扛多个并发 stream)。

**方向二:gRPC 客户端内置 xDS client**。gRPC 较新版本(Go/Java/C++ 都支持)在客户端内置了 xDS client,可以直接从一个 xDS server(Istiod、或独立的 xDS management server)拉 LDS/RDS/CDS/EDS,自己当"迷你 Envoy"做负载均衡和故障转移——不必 sidecar。这就是"无 sidecar 的 gRPC 服务网格"路线。这里 Envoy 不是数据面,而是**协议定义者**:xDS 的 proto 来自 Envoy 仓(`envoy/service/discovery/v3`),gRPC client 复用。

排障时注意:gRPC 流量是 HTTP/2,`/stats` 里看 `cluster.xxx.upstream_rq_total` 等,access log 里 `%REQ(:METHOD)%` 是 POST、`%REQ(:PATH)%` 是 `/package.Service/Method`(gRPC method 名)。gRPC 的 status code 在 trailer 里(`grpc-status`),Envoy 的 retry_on 可以配 `cancelled`/`deadline-exceeded`/`resource-exhausted` 等 gRPC status 触发重试。

### 4.3 与 Kubernetes 集成:三种部署模式

Envoy 在 k8s 里有三种典型部署:

1. **Sidecar 模式**(Istio / Linkerd):每个业务 pod 注入一个 Envoy sidecar,劫持该 pod 的全部进出流量。优点:应用无感、策略 per-pod 精细;缺点:资源开销大(每 pod 一个 Envoy,N 个 pod = N 份内存)、运维复杂。这是 Istio 的默认模式。

2. **Ingress / Gateway 模式**:少量 Envoy pod 作为集群入口(对应 Istio `Gateway` + `VirtualService`,或独立的 Envoy Gateway / Contour / Gloo)。所有外部流量先进这些 Envoy,再路由到内部服务。优点:集中、省资源;缺点:只覆盖"进集群"这一跳,内部服务间通信不经过 Envoy(除非配 sidecar)。

3. **Egress 模式**:少量 Envoy pod 作为"出集群"代理,内部服务访问外部 API(第三方、数据库)时统一经过 Egress Envoy,做 TLS 终止、mTLS、可观测、策略。对应 Istio `ServiceEntry` + Egress Gateway。

三种模式用的都是同一个 Envoy 二进制,差别只在配置(哪些 listener、哪些 cluster)。Envoy Gateway(CNCF 项目)是较新的"声明式配 Envoy 做 Gateway"的工具,用 Gateway API 标准配,底层生成 Envoy 配置。

---

## 五、hot restart 实操:零停机升级二进制

正文 P6-21 讲了 hot restart 的原理(新进程通过 fd 传递接管 socket,旧进程 drain 后退出)。这一节给实操步骤。

### 5.1 hot restart 的命令行参数

hot restart 由几个命令行参数控制(见 [`options_impl.cc:128-151`](../envoy/source/server/options_impl.cc#L128-L151)):

- `--restart-epoch <N>`:**hot restart epoch**,从 0 开始,每次升级 +1。新旧进程靠这个区分:新进程 epoch = 旧 epoch + 1。
- `--hot-restart-version`:打印 hot restart 兼容版本(用于校验新旧二进制能不能交接),对应 admin `/hot_restart_version`。
- `--drain-time-s <N>`:drain 持续秒数(旧进程停止接新连接后,给在途请求处理的时间)。
- `--parent-shutdown-time-s <N>`:旧(父)进程在新进程接管后,再活多少秒就退出(给完全收尾留时间)。
- `--base-id <N>` / `--use-dynamic-base-id`:共享内存 base id(新旧进程靠它定位共享内存,hot restart 传 stats、listener 状态)。

实际触发 hot restart 不是直接手敲这些参数,而是用一个叫 **`hot-restarter.py`** 的辅助脚本(在 Envoy 仓的 `examples/` 或独立部署),它 fork 新进程时自动算好 epoch、base-id、parent-shutdown-time 传进去。Istio 场景下,sidecar 的升级由 Istio 的 sidecar injector + k8s rolling update 完成,Envoy 进程本身一般跟着 pod 重建,不靠 hot restart;hot restart 主要用在**裸 Envoy 部署**(Ingress、Egress、独立网关)。

### 5.2 手动触发 hot restart 的流程

裸 Envoy 部署的典型升级流程(简化,实际用 hot-restarter.py 包装):

```bash
# 旧进程在跑,epoch=0
envoy --config-path envoy.yaml --restart-epoch 0 --base-id 0 &

# 升级:启动新进程,epoch=1,base-id 同 0(共享内存)
envoy --config-path envoy.yaml --restart-epoch 1 --base-id 0 \
      --drain-time-s 60 --parent-shutdown-time-s 90 &

# 新进程启动时:
#   1. 通过 base-id 找到旧进程的共享内存
#   2. 从旧进程继承 listener 的 listen socket(fd 传递)
#   3. 继承旧进程的 counter/gauge(通过 StatMerger,见 P6-20 第四节)
#   4. 开始接流量
#   5. 通知旧进程开始 drain
```

### 5.3 确认交接:用 admin 端点验证

升级过程中,用 admin 端点确认新老进程交接正常:

1. **`/hot_restart_version`**:在新进程上敲,确认它和旧进程的 hot restart 版本一致(不一致会拒绝交接)。
2. **`/server_info`**:看 `hot_restart_epoch` 字段——新进程应该是 1,旧进程还是 0;`state` 应该是 `LIVE`(新)和 `DRAINING`(旧)。
3. **`/listeners`**:新进程的 listener 都该是 active;旧进程的 listener 在 drain。
4. **`/stats`**:看 `server.live` = 1、`listener.downstream_cx_active` 正常、没有突降(突降说明在途连接被掐了,drain 没做好)。
5. **旧进程的 `/quitquitquit`** 或等 `--parent-shutdown-time-s` 到点:旧进程退出,新进程独立服务。

> **承 P6-21 的关键点**:hot restart 之所以零停机,核心是 **listen socket fd 通过共享内存/AUnix socket 在新旧进程间传递**——新进程一上来 `bind` 不会 EADDRINUSE(它直接拿到旧进程已 listen 的 fd),旧进程停 accept 后新进程立刻接管,中间没有"端口没人 listen"的窗口。drain 让旧进程的在途请求有序收尾。

---

## 六、★线上问题排查清单(本附录重点)

这一节是本附录实战价值最高的部分。每个问题按 **现象 → 可能原因 → 排查命令 → 修复** 组织。所有命令都基于前面讲的 admin 端点。

### 6.1 503 飙升

**现象**:监控里 `upstream_rq_5xx` 或 `downstream_rq_5xx` 突增,客户端大量收到 503。

**排查决策树**:

```
503 飙升
   │
   ▼
① access log 看 RESPONSE_FLAGS 是什么
   (这是区分"Envoy 拒的"vs"后端返的"的关键)
   │
   ├─ UH (NoHealthyUpstream) ────────► ② cluster 里没有健康 host
   │                                      curl :9901/clusters 看 health_flags
   │                                      全 unhealthy/ejected? → 后端全挂或 outlier 全踢
   │
   ├─ UF (UpstreamFailure) ──────────► ③ 连不上后端(连接失败/拒绝)
   │                                      curl :9901/clusters 看 cx_connect_fail
   │                                      检查后端 pod/service/网络
   │
   ├─ UO (UpstreamOverflow) ─────────► ④ circuit breaker 触发(P4-15)
   │                                      curl :9901/clusters 看 cx_overflow/rq_overflow
   │                                      > 0 → 熔断,见 6.2
   │
   ├─ UT (UpstreamRetryTimeout) ─────► ⑤ 后端慢,per_try_timeout
   │                                      curl :9901/stats 看 upstream_rq_timeout
   │                                      见 6.4 延迟突增
   │
   └─ 没有 flag,纯 503 ─────────────► ⑥ 后端自己返的 503
                                          直接看后端应用日志
```

**RESPONSE_FLAGS 速查**(Envoy 的响应标志,access log 的 `%RESPONSE_FLAGS%` 字段,完整列表见 Envoy docs):

| Flag | 含义 | 对应问题 |
|------|------|---------|
| `UH` | No Healthy Upstream | 后端全挂 / outlier 全踢 / EDS 没下发 endpoint |
| `UF` | Upstream Failure | 连接后端失败 |
| `UO` | Upstream Overflow | circuit breaker 触发 |
| `NR` | No Route Found | route 没匹配上(RDS 问题) |
| `URX` | Upstream Retry Timeout | per_try_timeout 到 |
| `DI` | Downstream Connection Termination | 客户端主动断 |
| `RL` | Rate Limited | 限流命中(local rate limit) |

**修复方向**:

- `UH`:查 `/clusters` 看 endpoint 在不在、`health_flags` 全 unhealthy 的话查后端为什么挂(应用日志、k8s pod status);全 ejected 的话查 outlier 阈值是不是太严(P4-14)。
- `UF`:查网络策略、service 是否存在、后端是否 listen。
- `UO`:见 6.2 熔断。
- `NR`:查 `/config_dump` 的 RDS,route 规则没匹配上。

### 6.2 熔断(circuit breaker 触发)

**现象**:503 带 `UO` flag,或 `/clusters` 里某 cluster 的 overflow 计数在涨。承接 P4-15。

**排查**:

```bash
# 1. 看是哪个 cluster 触发了熔断
curl -s localhost:9901/clusters | grep overflow
# 输出形如:
# api::cx_overflow::0
# api::rq_overflow::1234        ← 这个在涨,是 max_requests 触发
# api::rq_pending_overflow::0

# 2. 看当前上限
curl -s localhost:9901/clusters | grep max_
# api::default_priority::max_connections::1024
# api::default_priority::max_requests::1024
# api::default_priority::max_retries::3

# 3. 看活跃计数 vs 上限
curl -s 'localhost:9901/stats?filter=^cluster\.api\.' | grep -E 'cx_active|rq_active'
# cluster.api.upstream_cx_active::900       ← 接近 max_connections 1024
# cluster.api.upstream_rq_active::1020      ← 接近 max_requests 1024
```

**判断**:`rq_active` 接近 `max_requests` → 每来新请求就 `canCreate()` 返回 false → 返 503 UO。

**修复**:

- **临时(不停机)**:`/runtime_modify` 调高上限,见 1.4。
  ```bash
  curl -X POST 'localhost:9901/runtime_modify?api.default.circuit_breakers.default.max_requests=4096'
  ```
- **长期**:后端真的扛不住 1024 并发,加机器(扩容 endpoint)或优化后端,而不是无脑调高上限(调高了就是雪崩,P4-15 讲过)。改配置里的 `circuit_breakers`(或 Istio `DestinationRule` 的 `trafficPolicy.connectionPool`)。

### 6.3 配置不生效(xDS 没下发 / NACK)

见第三节的排查路径。要点:

- `config_dump` 里资源根本不存在 → xDS 没下发,看控制面。
- `config_dump` 里有 `error_detail` → 被 NACK,看错误信息改配置。
- `config_dump` 里有资源但 `version_info` 是旧的 → 控制面没推新版本。
- Istio:`istioctl ps` 看 STALE / SYNCED / NOT SENT。

### 6.4 延迟突增

**现象**:p99 突然飙升,用户反馈卡。

**排查**:

```bash
# 1. 看延迟分布(p99 等),承接 P6-20 histogram
curl -s 'localhost:9901/stats?format=json&filter=^cluster\..*rq_time$' | jq
# 找 outlier 的 cluster(它的 p99 高)

# 2. 看是不是某个后端慢(outlier 该不该踢它)
curl -s localhost:9901/clusters | grep -E 'success_rate|rq_time'
# success_rate 低的 host 该被 outlier 踢;若没踢,outlier 阈值太松

# 3. 看是不是重试风暴放大了延迟,承接 P4-15 retry_budget
curl -s 'localhost:9901/stats?filter=^cluster\.api\.upstream.*retry'
# upstream_rq_retry_overflow 涨 → retry_budget 在拒(正常)
# upstream_rq_retry 涨很多 → 重试多,可能后端整体慢

# 4. 看 outlier 有没有在工作
curl -s localhost:9901/clusters | grep -E 'outlier|ejection'
```

**修复方向**:

- 某个 host 慢但 outlier 没踢:收紧 outlier 阈值(`consecutive_5xx`、`success_rate` 标准,见 P4-14)。
- 整个 cluster 都慢:后端容量问题,扩容或优化后端。
- 重试放大:检查 `retry_on` 是不是太宽、`num_retries` 是不是太大,retry_budget 是否生效。
- per_try_timeout 太短导致大量重试:适当调长 per_try_timeout,或检查后端为什么慢到超时。

### 6.5 mTLS / TLS 握手失败

**现象**:access log 里 `UF` flag,或客户端报 `handshake_failure`、`certificate required`。承接 P6-21。

**排查**:

```bash
# 1. 看证书在不在、过期没
curl -s localhost:9901/certs
# 检查每个 cert 的 Days to Expiration,过期/快过期是常见原因

# 2. 看证书是不是 SDS 下发的、SDS 有没有推过来
curl -s localhost:9901/config_dump | jq '.configs[] | select(.["@type"]|contains("Secrets"))'
# SecretsConfigDump 为空 → SDS 没下发,看 Istiod / SDS server 日志

# 3. 看 mTLS 配置对不对(PeerAuthentication in Istio)
istioctl pc cluster <pod> | grep -i tls   # 看每个 cluster 的 TLS 策略

# 4. 看 SDS 相关错误
curl -s 'localhost:9901/stats?filter=secret' | grep -i fail
```

**修复方向**:

- 证书过期:轮换证书。Istio 自动轮换(默认 24h 换一次),但若 SDS 控制面挂了会卡住,看 Istiod。
- SDS 没下发:看 Istiod 日志、SDS gRPC 流是否通。
- mTLS 模式不匹配:PeerAuthentication 设了 STRICT 但有 client 不支持 mTLS,改 PERMISSIVE 或让 client 支持。
- SAN / SPIFFE 不匹配:Identity 不符,检查 trust domain 配置。

### 6.6 内存涨

**现象**:Envoy RSS 持续增长,可能 OOM。承接 P4-15 overload manager。

**排查**:

```bash
# 1. 看当前内存
curl -s localhost:9901/memory
# allocated / heap_size / resident

# 2. 看 overload manager 状态(有没有触发)
curl -s localhost:9901/stats | grep -i overload
# server.overload_active 等曲线

# 3. 看是不是连接堆积
curl -s 'localhost:9901/stats?filter=^cluster\..*cx_active$' | sort -t: -k3 -n
# 找 cx_active 异常高的 cluster,可能是慢后端堆积连接

# 4. 看是不是请求堆积
curl -s 'localhost:9901/stats?filter=^cluster\..*rq_active$' | sort -t: -k3 -n

# 5. 看下游连接数
curl -s 'localhost:9901/stats?filter=^listener\..*downstream_cx_active$'
```

**修复方向**:

- 某个 cluster 连接堆积(慢后端):见 6.4,修后端或加 timeout 让请求快点失败释放。
- overload manager 该触发但没触发:检查 `envoy.resource_monitors.fixed_heap` 的阈值配低了(`deviation_threshold` 之类)。
- 内存真泄漏(罕见):开 `/heap_dump` 抓快照,用 pprof 分析。
- 临时缓解:调小连接池 `max_requests` per cluster、缩 `stream_idle_timeout` 让卡死连接早点断。

### 6.7 排查清单总表

把上面六类问题压成一张速查表:

| 现象 | 第一步查 | 最可能原因 | 修复方向 |
|------|---------|----------|---------|
| 503 飙升 | access log 的 RESPONSE_FLAGS | UH/UF/UO/UO/UT 五类,见 6.1 | 对症 |
| 熔断(UO) | `/clusters` 的 overflow 计数 | circuit breaker 上限低或后端慢 | runtime_modify 调高 / 扩容后端 |
| 配置不生效 | `config_dump` 的 version_info / error_detail | xDS 没下发 / NACK / warming | 修控制面 / 改配置 / 等 warming |
| 延迟突增 | `/stats` 的 rq_time p99 + `/clusters` success_rate | 慢后端 / outlier 没踢 / 重试风暴 | 收紧 outlier / 扩容 / 调 retry |
| TLS 失败 | `/certs` + SecretsConfigDump | 证书过期 / SDS 没下发 / mTLS 模式 | 轮换证书 / 修 SDS / 调 PeerAuth |
| 内存涨 | `/memory` + overload stats + cx_active | 连接堆积 / overload 没触发 / 罕见泄漏 | 修慢后端 / 调 overload / heap_dump 分析 |

---

## 七、附:常用命令速查卡

```bash
# === 基本 ===
curl localhost:9901/                              # admin 首页
curl localhost:9901/help                          # 所有端点说明
curl localhost:9901/ready                         # 探活(给 k8s readinessProbe)
curl localhost:9901/server_info                   # 版本/状态/epoch

# === 配置 dump ===
curl localhost:9901/config_dump                   # 全量生效配置
curl 'localhost:9901/config_dump?resource=clusters'
curl 'localhost:9901/config_dump?include_eds=true'

# === cluster / listener ===
curl localhost:9901/clusters                      # cluster 状态(排障头号)
curl 'localhost:9901/clusters?format=json'
curl localhost:9901/listeners                     # listener 状态
curl 'localhost:9901/listeners?format=json'
curl 'localhost:9901/init_dump?mask=listener'     # warming 卡在哪

# === stats ===
curl 'localhost:9901/stats?format=json'
curl 'localhost:9901/stats?filter=^cluster\.api\.'
curl 'localhost:9901/stats?histogram_buckets=detailed&type=Histograms'
curl localhost:9901/stats/prometheus              # 给 Prometheus scrape
curl -X POST localhost:9901/reset_counters        # counter 归零

# === 日志 ===
curl 'localhost:9901/logging?level=debug'         # 全开 debug(慎用,日志爆炸)
curl 'localhost:9901/logging?paths=router:debug,upstream:debug'
curl -X POST localhost:9901/reopen_logs           # logrotate 后重开

# === 运行时调整 ===
curl localhost:9901/runtime                       # 看 runtime 变量
curl -X POST 'localhost:9901/runtime_modify?key=val'   # 改 runtime(如 circuit breaker 上限)

# === 证书 / 内存 ===
curl localhost:9901/certs                         # 看证书及过期
curl localhost:9901/memory                        # 内存使用
curl localhost:9901/memory/tcmalloc               # TCMalloc 详情

# === 控制(慎用)==
curl -X POST 'localhost:9901/drain_listeners?graceful'   # 触发 drain
curl -X POST localhost:9901/healthcheck/fail             # 摘流
curl -X POST localhost:9901/healthcheck/ok               # 恢复
curl -X POST localhost:9901/quitquitquit                  # 退出

# === Istio 场景 ===
istioctl ps                                                # 全网格同步状态
istioctl pc cluster <pod>                                  # 看 cluster
istioctl pc listener <pod>                                 # 看 listener
istioctl pc route <pod>                                    # 看 route
istioctl pc bootstrap <pod>                                # 看 bootstrap
istioctl pc log <pod> --level router:debug                 # 动态调日志
istioctl pc cluster <pod> --fqdn outbound\|8080\|api       # 过滤
```

> **一句话总结这本附录**:线上排障靠四件套——**admin 端点查内部状态、`config_dump` 看配置生效没、access log 的 RESPONSE_FLAGS 定位问题类、`/clusters` 的 health_flags/overflow 看后端**。Istio 环境用 istioctl 包装得更易用。原理在正文 23 章里,这本附录是把原理变成命令的桥梁。

---

> **承接**:本附录和附录 A(源码全景路线图)一起,是正文的实践补充——A 帮你读懂源码,B 帮你驾驭线上。回到全书主线:filter chain + xDS 这两件套撑起了 service mesh,而驾驭它的工具,就是这本附录讲的 admin / istioctl / validate / hot restart 这套工具链。
