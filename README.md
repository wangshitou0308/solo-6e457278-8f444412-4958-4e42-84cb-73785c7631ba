# mailrecon — 本地历史邮件包会话重建 API

供法务与支持团队整理历史邮件包的**纯本地、零外部依赖**服务：接收含 `.eml`
文件的 ZIP，安全校验后解析 MIME，按 `Message-ID` / `In-Reply-To` /
`References` 引用链重建会话树，并通过 HTTP 接口查询与下载 JSON 结果。
案件级多包合并可把多个已完成作业拼成一棵跨包会话森林：重复邮件按
SHA-256 归并、Message-ID 冲突并列保留、跨包补齐缺失父邮件。
时序核验模块按原顺序保留每封邮件的全部 `Received` 头（逐跳时间、
发送/接收主机，统一换算 UTC 并保留原始时区），可对已完成作业或案件
创建后台分析：按可配置阈值标出客户端时钟偏差、异常传输耗时、回复
早于父邮件，同一 Message-ID 在不同来源中的传输链不一致时并列展示；
缺时区、无法解析、相邻跳逆序等情况一律只标注证据、不做猜测。
声明身份核验模块离线解析 From / Sender / Reply-To / Return-Path /
Message-ID 与 DKIM-Signature（保留重复头与原始值，提取地址、域名及
`d`/`s`/`i`/`h` 标签并记录异常），可对已完成作业或案件创建后台核验：
按邮件和会话汇总 From 与 Sender/Return-Path 域不一致、回复链身份变化、
Message-ID 域漂移、DKIM 的 `h` 未覆盖 From。**不查 DNS、不验证签名
真伪**；转发、邮件列表与字段缺失只记为待复核证据，证据不足标无法核验，
绝不判定伪造；每项发现都附来源文件、头字段与依据。
收件人流转分析模块沿会话父子回复边逐条比较 **From/To/Cc 可见地址集合**
（忽略显示名，域名转小写、local-part 保持原样），可对已完成作业或案件
创建后台分析：记录新增地址、未继续列入收件人、To/Cc 角色变化，以及相对
父邮件参与者的 reply-all 遗漏；每条事件附父子邮件、会话根、涉及字段、
地址与集合差异。邮件列表/群组迹象、畸形地址、父链缺失、引用冲突时差异
只列为待复核；**绝不根据 Bcc 或 SMTP 信封猜测实际送达对象，不改写现有
会话树**。

* 仅使用 Python 标准库：`http.server`、`email`、`zipfile`、`sqlite3`；
* 不修改、不回传任何原始邮件，附件只导出元数据（名称/类型/大小/SHA-256）；
* 问题只标注、不阻断：重复 Message-ID、父邮件缺失、引用成环、正文无法解码
  等情况都会写入节点的 `issues`；压缩包级别的安全问题才拒绝整包。

## 目录结构

```
mailrecon/            服务源码
  config.py           上限/路径配置（环境变量可覆盖）
  zipguard.py         ZIP 安全校验与受控解压
  mailparser.py       单封 .eml 解析（MIME/字符集/正文/附件/引用头/Received/身份头）
  received.py         Received 头逐跳解析（UTC 换算 + 原始时区保留，不猜测）
  idheaders.py        声明身份头解析（六头保留重复值/原始值，DKIM d/s/i/h，不验签）
  threads.py          会话树重建（缺失/成环/重复 ID 处理，迭代式）
  casemerge.py        案件级多包合并（去重/冲突/补链，迭代式）
  timing.py           传输时序核验引擎（五类结论，记录所用字段与阈值）
  identity.py         声明身份核验引擎（四类发现 + 待复核证据/无法核验）
  recipientflow.py    收件人流转引擎（四类事件 + 列表/畸形/父链/引用待复核）
  jsonio.py           迭代式 JSON 解析/序列化（深引用链不受递归深度限制）
  storage.py          SQLite 元数据 + 作业/案件/分析/核验/流转落盘文件管理
  processor.py        后台作业流水线（单线程顺序处理）
  caseproc.py         后台案件合并流水线（单线程顺序处理）
  timeproc.py         后台时序核验流水线（单线程顺序处理，重启续跑）
  identityproc.py     后台声明身份核验流水线（单线程顺序处理，重启续跑）
  recflowproc.py      后台收件人流转流水线（单线程顺序处理，重启续跑）
  server.py           HTTP API（http.server）
  __main__.py         启动入口
scripts/
  make_sample.py      生成正常示例包 examples/sample-mails.zip
  make_case_sample.py 生成案件合并示例包 examples/case-pack-{1,2}.zip
  make_timing_sample.py 生成时序核验示例包 examples/timing-*.zip
  make_identity_sample.py 生成身份核验示例包 examples/identity-*.zip
  make_recipient_sample.py 生成收件人流转示例包 examples/recipient-*.zip
  make_evil.py        生成应被拒绝的恶意/超限 ZIP
tests/                245 个 unittest 用例
examples/             生成产物（正常包 + 案件/时序/身份/收件人示例包 + evil/ 恶意包）
docs/API.md           接口文档
docs/API_EXAMPLES.sh  curl 调用示例
```

## 快速开始

```bash
# Python 3.10+，无需安装任何依赖
python3 -m mailrecon                 # 默认监听 127.0.0.1:8080

# 生成示例邮件包（正常会话 + 各类问题场景）
python3 scripts/make_sample.py

# 提交作业
curl -X POST http://127.0.0.1:8080/api/v1/jobs \
     -H 'Idempotency-Key: legal-case-2026-09-001' \
     -F 'file=@examples/sample-mails.zip;type=application/zip'
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MAILRECON_HOST` | `127.0.0.1` | 监听地址（仅建议本机访问） |
| `MAILRECON_PORT` | `8080` | 监听端口 |
| `MAILRECON_DATA_DIR` | `./data` | SQLite 与作业文件目录 |
| `MAILRECON_MAX_UPLOAD_BYTES` | `524288000` (500 MiB) | HTTP 请求体上限 |
| `MAILRECON_MAX_ENTRIES` | `10000` | ZIP 条目数上限 |
| `MAILRECON_MAX_TOTAL_UNCOMPRESSED` | `1073741824` (1 GiB) | 解压总体积上限 |
| `MAILRECON_MAX_ENTRY_SIZE` | `104857600` (100 MiB) | 单条目解压体积上限 |
| `MAILRECON_MAX_COMPRESSION_RATIO` | `200` | 压缩比上限（ZIP 炸弹特征） |
| `MAILRECON_MAX_CASE_JOBS` | `100` | 单个案件可合并的作业数上限 |
| `MAILRECON_MAX_CASE_EMAILS` | `100000` | 单个案件邮件总量上限 |
| `MAILRECON_DEFAULT_CLOCK_SKEW_SECONDS` | `120` | 时序核验默认客户端时钟偏差容差（秒） |
| `MAILRECON_DEFAULT_MAX_TRANSIT_SECONDS` | `300` | 时序核验默认单跳传输耗时上限（秒） |
| `MAILRECON_MAX_THRESHOLD_SECONDS` | `604800` | 时序核验用户可配阈值的上限（秒） |

## 安全策略摘要

提交的 ZIP 会在**解压前**依据中央目录拒绝以下情况（作业标记 `failed`，
错误信息见 `error` 字段）：

* 路径穿越条目：`../`、绝对路径、盘符、反斜杠、Windows 保留名；
* 符号链接/设备等非普通文件条目、目录条目、加密条目、重名条目；
* 条目数、单条目体积、解压总体积、压缩比超限；非 ZIP / 损坏 ZIP。

解压时会再次以硬上限逐块读取，并核对实际落盘体积与中央目录声明是否一致。

## 深引用链

线程父子解析、树复制、紧凑视图与 JSON 读写均为显式栈实现，不使用
Python 递归，因此即使整个包是一条上万封邮件的回复链（远超默认递归深度
1000），作业也能正常完成，`/tree` 与 `/result` 可正常返回。嵌套超过 500
层时接口自动输出紧凑 JSON（结构不变），落盘结果文件始终为紧凑 JSON，
避免缩进格式在纯链上 O(深度²) 的体积膨胀。

## 问题数据（只标注、不改原文件）

每封邮件节点带 `issues` 列表，覆盖：

* `missing_message_id` — 无 Message-ID，作为独立根；
* `duplicate_message_id` — 同一 ID 被多封邮件使用（区分“重复副本”与
  “内容不同的 ID 冲突”），重复节点不参与挂载，独立成根；
* `missing_parent` — 引用的父邮件不在包内（会沿 References 上溯最近祖先）；
* `reference_cycle` / `self_reference` — 引用链成环/自引用，断开成环边；
* `undecodable` — 字符集声明无效或正文无法按声明解码，自动降级
  （UTF-8 → GB18030 → Latin-1）并记录问题；
* 其余解析缺陷（畸形日期/头、附件解码失败等）计入 `other`。

## 案件级多包合并

`POST /api/v1/cases` 把多个**已完成**作业合并为一个案件，后台重建跨包
会话森林（接口详见 `docs/API.md` 第 8–11 节）：

* **去重**：SHA-256 相同的邮件合并为一个节点，`sources` 保留全部来源
  作业与文件名；
* **冲突**：Message-ID 相同但内容不同的邮件并列保留并标注
  （`merge_info.conflict`），引用该 ID 时**不猜测父节点**，可沿
  References 上溯无歧义祖先；
* **补链**：父邮件在某作业包内缺失、但在案件其他作业中找到时跨包挂载，
  依据记入 `merge_info.relinked_parent`；
* **贡献汇总**：`stats.job_contributions` 按作业统计唯一节点、被合并
  重复、冲突参与、补链与根节点数；
* 源作业未完成 / 已删除 / 在同一案件中重复提交时，创建即被拒绝并说明
  原因；案件结果独立落盘，完成后删除源作业不影响读取，服务重启自动
  恢复未完成的案件。

```bash
python3 scripts/make_case_sample.py   # 生成两个有关联的示例包
# 分别上传为作业后：
curl -X POST http://127.0.0.1:8080/api/v1/cases \
     -H 'Content-Type: application/json' \
     -d '{"name": "合同谈判合并", "job_ids": ["<job1>", "<job2>"]}'
```

## 传输时序核验

每封邮件的全部 `Received` 头在解析期按**原始出现顺序**保留（`index` 0 =
最上方 = 传输路径最后一跳），逐跳提取发送主机、接收主机与时间：时间统一
换算 UTC（`time_utc`），同时保留原始时区标记（`timezone`）与原始时区下的
时间（`time_original`）。**缺时区、无法解析时不做猜测**——对应字段为
`null`，原因写入该跳的 `issues`。

`POST /api/v1/analyses` 对已完成的作业或案件创建后台时序核验（接口详见
`docs/API.md` 第 12–15 节），阈值可配（`clock_skew_seconds` /
`max_transit_seconds`），结论类型：

* `client_clock_skew` — `Date` 与首跳接收时间差超过时钟偏差阈值；
* `abnormal_transit` — 相邻两跳间隔超过传输耗时阈值；
* `hop_time_inversion` — 相邻跳时间逆序（不猜测原因，两跳证据并列）；
* `reply_before_parent` — 回复的 `Date` 早于父邮件超过时钟偏差容差；
* `chain_mismatch` — 同一 Message-ID 在不同来源中的传输链不一致，
  各版本传输链在 `evidence.variants` 中并列展示，不做取舍。

每条结论都记录 `fields`（所用字段）、`thresholds`（本次判定阈值）与
`evidence`（具体取值）；时间线接口支持按时间区间（边界须带时区）或
异常类型筛选；分析结果独立落盘，删除源作业/案件不影响读取，服务重启
自动恢复未完成的分析。

```bash
python3 scripts/make_timing_sample.py   # 生成时序核验示例包
# 上传 examples/timing-mails.zip 为作业后：
curl -X POST http://127.0.0.1:8080/api/v1/analyses \
     -H 'Content-Type: application/json' \
     -d '{"target_type": "job", "target_id": "<job_id>",
          "thresholds": {"clock_skew_seconds": 120, "max_transit_seconds": 300}}'
```

## 邮件声明身份核验

每个作业/案件节点都带 `identity` 头块：解析期用 `email.message` 的
`raw_items()` 按原始出现顺序保留 From / Sender / Reply-To /
Return-Path / Message-ID / DKIM-Signature 的**全部重复头与逐字原始值**
（折叠换行也保留），提取地址、域名，以及 DKIM 的 `d`/`s`/`i`/`h` 标签；
畸形头、缺失/重复标签等只写入该头的 `anomalies`，不阻断解析。

`POST /api/v1/identity-checks` 对已完成的作业或案件创建后台核验
（接口详见 `docs/API.md` 第 16–20 节），**全程离线**：

* `from_domain_mismatch` — From 与任一 Sender / Return-Path 值
  （**重复头的每个值都参与**）域不一致；
* `reply_identity_change` — 回复链上相邻两封邮件 From 域变化
  （显示名相同却换域等形态），证据并列、不判伪造；
* `message_id_domain_drift` — 邮件级 Message-ID 标识域 ≠ From 域；
  会话级同一发件人在同一会话内标识域漂移；
* `dkim_from_not_covered` — DKIM-Signature 的 `h=` 未列出 from
  （只看覆盖关系，`b=` 签名字段绝不验证，不查 DNS）。

结论三态：`observed`（客观差异）、`needs_review`（有转发/列表/多重签名
等背景，待人工复核）、`inconclusive`（证据不足，无法核验）。转发
（`Fwd:` 主题）、邮件列表迹象、字段缺失与头解析异常另列为
`review_flags` 待复核证据；**缺 From 时既不判 From 域不一致，也不判
DKIM“未覆盖 From”**，缺 DKIM 不代表无签名即伪造。每项发现都附
`sources`（来源文件/作业）、`headers`（依据头字段）、`basis` 与
`evidence`。支持进度查询、按类型/状态/域名筛选（`/findings`、`/emails`、
`/threads`）与 JSON 下载；结果独立落盘，删除源作业/案件后仍可读，
服务重启自动续跑未完成的核验。

```bash
python3 scripts/make_identity_sample.py   # 生成身份核验示例包
# 上传 examples/identity-mails.zip 为作业后：
curl -X POST http://127.0.0.1:8080/api/v1/identity-checks \
     -H 'Content-Type: application/json' \
     -d '{"target_type": "job", "target_id": "<job_id>"}'
# 按类型筛选 / 按域名筛选 / 下载
curl '.../identity-checks/<check_id>/findings?type=dkim_from_not_covered'
curl '.../identity-checks/<check_id>/findings?domain=mailer.net'
curl -OJ '.../identity-checks/<check_id>/result'
```

## 收件人流转分析

`POST /api/v1/recipient-flows` 对已完成的作业或案件创建后台收件人流转
分析（接口详见 `docs/API.md` 第 21–25 节）。分析沿会话树的每一条父子
回复边，逐条比较两封邮件的 **From / To / Cc 可见地址集合**：

* **匹配口径**：忽略显示名；只把**域名转小写**，**local-part 保持原样**
  （`a@X.com` = `a@x.com`，而 `John@x.com` ≠ `john@x.com`，不猜测
  local-part 大小写语义）；
* `added` — 相对父邮件参与者集合新增的可见地址；
* `dropped` — 父邮件原 To/Cc 收件人未继续列入子邮件 To/Cc，且不是
  子邮件发件人（父发件人在回复中“变成我”不算遗漏）；
* `role_changed` — 同一地址 To↔Cc 角色变化（证据带 from/to_role）；
* `reply_all_omitted` — 相对父邮件全部参与者（From∪To∪Cc），子邮件
  可见集合遗漏的地址（疑似未全部回复）。

**只看可见头，绝不推断实际送达**：不读取或推断 Bcc，不使用
Return-Path/Sender/Received 等 SMTP 信封信息猜测谁真正收到，不查 DNS；
分析只读结果 JSON，**绝不改写现有会话树**。

下列背景下的父子边差异**只列为待复核（`reviews`），不生成客观事件**，
但仍附完整集合差异快照供人工判断：

* 邮件列表/群组迹象（多地址 From、RFC 群组语法、与 From 不同域的
  Sender/Reply-To、列表形态地址如 `*-owners`/`*-request`/`list-*`）；
* 畸形地址（无法可靠归一化的 From/To/Cc 地址，该地址不参与自动比较）；
* 父链缺失（父邮件不在范围内或挂载到上溯祖先）；
* 引用冲突（引用边断开、Message-ID 对应多封内容不同的邮件）。

每条事件都附父子邮件（uid/Message-ID/时间/来源）、会话根、涉及字段
`fields`、主地址、判定依据 `basis` 与集合差异快照（父子各自 from/to/cc
及四类差异清单）。支持进度查询，按会话（`thread`）、地址（`address`）、
事件类型（`type`）/待复核类型（`kind`）筛选，提供按会话汇总、地址台账
与完整 JSON 下载。结果独立落盘，删除源作业/案件后仍可读，服务重启
自动续跑未完成的分析。

```bash
python3 scripts/make_recipient_sample.py   # 生成收件人流转示例包
# 上传 examples/recipient-mails.zip 为作业后：
curl -X POST http://127.0.0.1:8080/api/v1/recipient-flows \
     -H 'Content-Type: application/json' \
     -d '{"target_type": "job", "target_id": "<job_id>"}'
# 按地址 / 事件类型 / 待复核类型筛选
curl '.../recipient-flows/<flow_id>/events?address=carl@example.org'
curl '.../recipient-flows/<flow_id>/events?type=reply_all_omitted'
curl '.../recipient-flows/<flow_id>/events?kind=possible_mailing_list'
# 按会话汇总 / 地址台账 / 下载
curl '.../recipient-flows/<flow_id>/threads'
curl '.../recipient-flows/<flow_id>/addresses'
curl -OJ '.../recipient-flows/<flow_id>/result'
```

## 作业落盘与删除

```
DATA_DIR/mailrecon.db               SQLite 元数据（作业 + 案件 + 分析 + 核验）
DATA_DIR/jobs/<job_id>/upload.bin   收到的原始 ZIP（字节不动）
DATA_DIR/jobs/<job_id>/result.json  结果 JSON
DATA_DIR/jobs/<job_id>/extract/     处理期间临时解压目录，完成后立即删除
DATA_DIR/cases/<case_id>/result.json 案件合并结果（与源作业目录独立）
DATA_DIR/analyses/<analysis_id>/result.json 时序核验结果（与源目标目录独立）
DATA_DIR/identity_checks/<check_id>/result.json 身份核验结果（与源目标目录独立）
DATA_DIR/recipient_flows/<flow_id>/result.json 收件人流转结果（与源目标目录独立）
```

`DELETE /api/v1/jobs/{id}` 会删除元数据并 `rmtree` 整个作业目录；
服务重启后，上次未完成的作业、案件、时序核验分析、声明身份核验与
收件人流转分析会自动重新入队处理。

## 测试

```bash
python3 -m unittest discover -s tests -v     # 245 个用例
```

## 更多

* 接口字段、状态码与错误码：[`docs/API.md`](docs/API.md)
* 调用示例：[`docs/API_EXAMPLES.sh`](docs/API_EXAMPLES.sh)
* 恶意包生成：`python3 scripts/make_evil.py --help`
