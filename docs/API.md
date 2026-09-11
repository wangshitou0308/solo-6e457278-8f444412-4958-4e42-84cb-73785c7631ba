# mailrecon 接口文档

- Base URL：`http://127.0.0.1:8080`（默认，可配置）
- 请求/响应：除结果下载外均为 `application/json; charset=utf-8`
- 所有时间为 ISO 8601（含时区）；作业 ID 为 UUIDv4
- 无鉴权层：服务设计为本机/内网使用，仅监听 127.0.0.1，如需跨机请自行
  放在反向代理与访问控制之后
- 深链说明：线程构建、JSON 序列化与解析全部使用显式栈（非递归），
  万级长度的单链引用也可处理，不受 Python 默认递归深度（1000）限制。
  当响应 JSON 嵌套深度超过 500 层时，接口自动由缩进格式切换为紧凑
  JSON（内容结构不变），以避免闭合缩进带来的 O(深度²) 体积膨胀；
  落盘结果文件统一使用紧凑 JSON。
- 时序核验：每封邮件的全部 `Received` 头按原顺序解析保留（见第 4 节
  节点字段），可对已完成的作业/案件创建后台时序核验分析（第 12–15 节）。
- 声明身份核验：每个节点新增 `identity` 头块（见第 4 节），可对已完成的
  作业/案件创建后台声明身份核验（第 16–20 节）。**离线运行：不查 DNS、
  不验证 DKIM 签名真伪**，只客观记录声明差异与待复核证据，证据不足时
  标注无法核验，绝不直接判定伪造。

## 1. 创建作业

```
POST /api/v1/jobs
```

**请求体（两种方式任选其一）**

1. `multipart/form-data`：字段名 `file`，内容为 ZIP；
2. 原始 ZIP 字节：`Content-Type: application/zip`
   （或 `application/x-zip-compressed` / `application/octet-stream`），
   可用 `X-Filename` 头告知原始文件名。

**请求头**

| 头 | 必填 | 说明 |
|---|---|---|
| `Content-Length` | 是 | 不支持 chunked；超过 `MAILRECON_MAX_UPLOAD_BYTES` 返回 413 |
| `Idempotency-Key` | 否 | 8–200 个可打印 ASCII 字符。相同 Key + 相同内容返回同一作业；相同 Key + 不同内容返回 409 |

**响应**

`201 Created`（首次）或 `200 OK`（幂等重放，带 `idempotent_replayed: true`）：

```json
{
  "id": "a6771c39-39a0-4f35-94b7-74268e1e2511",
  "status": "queued",
  "created_at": "2026-09-10T15:31:37+00:00",
  "updated_at": "2026-09-10T15:31:37+00:00",
  "original_filename": "sample-mails.zip",
  "idempotency_key": "legal-case-2026-09-001",
  "progress": 0,
  "phase": null,
  "error": null,
  "email_count": 0,
  "thread_count": 0,
  "stats": null
}
```

上传本身只做受理；ZIP 安全校验在后台进行，被拒绝时作业状态变为
`failed`（见错误结构）。**原始上传字节始终原样保存为 `upload.bin`，
任何阶段都不会修改。**

## 2. 查询作业进度

```
GET /api/v1/jobs/{job_id}
```

`status`：`queued` → `processing` → `completed` | `failed`

`phase` 依次为：`validating_zip`、`parsing_eml`、`building_threads`、
`writing_result`、`completed` / `failed`。`progress` 为 0–100 整数。

`failed` 时 `error` 给出拒绝原因，例如：

```json
{ "status": "failed", "phase": "validating_zip",
  "error": "压缩包被拒绝: 检测到非法/路径穿越条目: '../../../../tmp/pwned.eml'" }
```

完成后 `stats` 结构：

```json
{
  "zip_entries": 12,
  "eml_parsed": 11,
  "non_eml_skipped": 1,
  "thread_count": 8,
  "attachment_count": 2,
  "issues_total": 6,
  "issues_by_type": {
    "missing_message_id": 1,
    "duplicate_message_id": 1,
    "missing_parent": 1,
    "reference_cycle": 1,
    "self_reference": 1,
    "undecodable": 1,
    "other": 0
  }
}
```

## 3. 作业列表

```
GET /api/v1/jobs
```

返回 `{ "jobs": [ ...作业摘要... ] }`，按创建时间倒序，最多 100 条。

## 4. 读取会话树

```
GET /api/v1/jobs/{job_id}/tree
GET /api/v1/jobs/{job_id}/tree?view=compact
```

仅在 `completed` 后可用，否则 `409 not_ready`。

完整视图节点（树由 `children` 递归构成，森林按邮件时间排序）：

```json
{
  "job_id": "...",
  "stats": { ... },
  "skipped_entries": [
    { "source_file": "README.txt", "reason": "不是 .eml 文件，已跳过" }
  ],
  "threads": [
    {
      "uid": 0,
      "source_file": "thread-contract/01-request.eml",
      "raw_sha256": "…整封邮件原始字节的 SHA-256…",
      "message_id": "<...@example.com>",
      "in_reply_to": null,
      "references": [],
      "date": "2026-09-01T09:00:00+00:00",
      "from": { "name": "李雷", "address": "lilei@example.com" },
      "to": [ { "name": "法务部", "address": "legal@example.com" } ],
      "cc": [],
      "subject": "【合同】2026 年度框架服务协议审查",
      "body_text": "各位好，\n\n附件为……",
      "body_html_present": false,
      "attachments": [
        {
          "filename": "框架服务协议-节选.txt",
          "content_type": "text/plain",
          "size": 49,
          "sha256": "b6d5f438…"
        }
      ],
      "received": [
        {
          "index": 0,
          "raw": "from mail.example.com by mx.example.org with ESMTPS; Mon, 01 Sep 2026 09:00:20 +0000",
          "from_host": "mail.example.com",
          "by_host": "mx.example.org",
          "time_utc": "2026-09-01T09:00:20+00:00",
          "time_original": "2026-09-01T09:00:20+00:00",
          "timezone": "+0000",
          "issues": []
        }
      ],
      "identity": {
        "from": [
          {
            "index": 0,
            "raw": "李雷 <lilei@example.com>",
            "count": 1,
            "addresses": [ { "name": "李雷", "address": "lilei@example.com", "domain": "example.com" } ],
            "address": "lilei@example.com",
            "name": "李雷",
            "domain": "example.com",
            "anomalies": []
          }
        ],
        "sender": [],
        "reply_to": [],
        "return_path": [],
        "message_id": {
          "present": true,
          "headers": [
            {
              "index": 0,
              "raw": "<...@example.com>",
              "value": "<...@example.com>",
              "local_part": "...",
              "domain": "example.com",
              "anomalies": []
            }
          ]
        },
        "dkim": [
          {
            "index": 0,
            "raw": "v=1; d=example.com; s=sel; h=From:To; b=...",
            "present": true,
            "d": "example.com",
            "s": "sel",
            "i": null,
            "i_local_part": null,
            "i_domain": null,
            "h": ["from", "to"],
            "covers_from": true,
            "anomalies": []
          }
        ],
        "anomalies": []
      },
      "issues": [],
      "children": [ { } ]
    }
  ]
}
```

约定：

* `body_text` 为纯文本正文；只有 HTML 正文时做极简 HTML→文本转换，
  并置 `body_html_present: true`；
* 附件**永远只有** `filename` / `content_type` / `size` / `sha256`，
  接口不返回任何附件二进制；
* `received` 按**邮件头中的原始出现顺序**保留全部 Received 跳点
  （`index` 0 = 最上方 = 传输路径上的最后一跳；路径方向为 index 从大到小）。
  每跳提取 `from_host`（发送主机）、`by_host`（接收主机）与时间：
  `time_utc` 为换算后的 UTC 时间，`time_original` 为原始时区下的时间，
  `timezone` 为原始时区标记（如 `+0200`、`GMT`）。**缺时区、无法解析时
  不做任何猜测**：对应字段为 `null`，原因写入该跳的 `issues`；
* 节点 `issues` 为该邮件的问题说明（中文人类可读）；
* `compact` 视图省略正文、附件明细与问题明细，只保留
  `issue_count`、`attachment_count` 等摘要，便于浏览大树。

### 线程重建规则

1. 每封邮件有内部 `uid`；同一 `Message-ID` 的第一封作为规范目标，
   其余重复邮件独立成根并标注（字节相同=重复副本，不同=ID 冲突）；
2. 父候选顺序：`In-Reply-To` → `References` 逆序（就近祖先），
   直接父缺失时沿 References 上溯；
3. 引用成环（含自引用）断开闭环边并标注，保证输出严格无环；
4. 无 Message-ID 的邮件无法被引用，独立成根并标注。

## 5. 下载 JSON 结果

```
GET /api/v1/jobs/{job_id}/result
```

`200`，响应头：

```
Content-Type: application/json; charset=utf-8
Content-Disposition: attachment; filename="<job_id>.result.json"
```

响应体为完整结果文件（与 `/tree` 相比多 `original_filename`、
`created_at`、`completed_at`、`status` 顶层字段，结构相同的 `threads`）。
完成前请求返回 `409 not_ready`。

## 6. 删除作业

```
DELETE /api/v1/jobs/{job_id}
```

删除 SQLite 元数据并清理整个作业目录（`upload.bin`、`result.json`、
可能残留的 `extract/`）。返回：

```json
{ "id": "...", "deleted": true }
```

`processing` 状态返回 `409 job_processing`（请等其完成/失败后再删）。

## 7. 健康检查

```
GET /health  →  200 { "status": "ok", "version": "1.0.0" }
```

## 8. 创建案件（多包合并）

```
POST /api/v1/cases
Content-Type: application/json
```

把多个**已完成**的作业合并为一个案件，后台重建跨包会话森林。

**请求体**

```json
{
  "name": "合同谈判合并（可选，≤200 字符）",
  "job_ids": ["<已完成作业ID>", "<已完成作业ID>"]
}
```

**响应** `201 Created`：

```json
{
  "id": "7281c96f-635a-4b7c-938f-580984a2be40",
  "name": "合同谈判合并",
  "status": "queued",
  "created_at": "2026-09-10T20:00:00+00:00",
  "updated_at": "2026-09-10T20:00:00+00:00",
  "job_ids": ["287ba219-...", "752d1ab2-..."],
  "progress": 0,
  "phase": null,
  "error": null,
  "email_count": 0,
  "thread_count": 0,
  "stats": null
}
```

**创建即校验，不满足时拒绝并说明原因**：

| 情形 | 状态码 | code |
|---|---|---|
| 源作业不存在或已删除 | 404 | `job_not_found` |
| 源作业未完成（queued/processing/failed） | 409 | `job_not_completed` |
| 同一作业在同一案件中重复提交 | 409 | `duplicate_job` |
| 作业数超过 `MAILRECON_MAX_CASE_JOBS` | 400 | `bad_request` |
| 邮件总量超过 `MAILRECON_MAX_CASE_EMAILS` | 409 | `case_too_large` |
| 请求体非 JSON / 缺字段 / ID 格式非法 | 400 | `bad_request` |

## 9. 查询案件进度

```
GET /api/v1/cases/{case_id}
```

`status`：`queued` → `processing` → `completed` | `failed`

`phase` 依次为：`loading_results`、`building_forest`、`writing_result`、
`completed` / `failed`。`progress` 为 0–100 整数。

`failed` 时 `error` 给出原因（例如源作业在案件处理前被删除）。

完成后 `stats` 结构：

```json
{
  "job_count": 2,
  "source_emails": 8,
  "merged_nodes": 7,
  "duplicates_merged": 1,
  "conflict_groups": 1,
  "conflict_nodes": 2,
  "relinked_nodes": 2,
  "ambiguous_references": 0,
  "missing_references": 0,
  "reference_cycles": 0,
  "self_references": 0,
  "thread_count": 4,
  "job_contributions": [
    {
      "job_id": "287ba219-...",
      "original_filename": "case-pack-1.zip",
      "emails": 5,
      "unique_nodes": 5,
      "duplicates_merged": 0,
      "conflict_nodes": 1,
      "relinked_nodes": 1,
      "root_nodes": 3
    }
  ]
}
```

各作业贡献口径：`unique_nodes` 为该作业**首次提供**的合并节点数；
`duplicates_merged` 为该作业提供但被合并掉的重复邮件数
（`emails = unique_nodes + duplicates_merged`）；`conflict_nodes` /
`relinked_nodes` 按参与计（同一节点可计入多个作业）；`root_nodes`
按节点的首要来源（`sources[0]`）归属。

## 10. 读取案件合并树

```
GET /api/v1/cases/{case_id}/tree
GET /api/v1/cases/{case_id}/tree?view=compact
```

仅在 `completed` 后可用，否则 `409 not_ready`。

```json
{
  "case_id": "...",
  "name": "合同谈判合并",
  "source_jobs": [
    { "job_id": "...", "original_filename": "case-pack-1.zip", "email_count": 5 }
  ],
  "stats": { ... },
  "threads": [
    {
      "uid": 0,
      "message_id": "<case-demo-quote@example.com>",
      "raw_sha256": "…",
      "in_reply_to": null,
      "references": [],
      "date": "2026-08-20T09:00:00+00:00",
      "from": { "name": "销售部", "address": "sales@example.com" },
      "to": [], "cc": [],
      "subject": "2026 年度服务报价",
      "body_text": "…",
      "body_html_present": false,
      "attachments": [],
      "sources": [
        { "job_id": "287ba219-...", "source_file": "01-quote.eml", "original_uid": 0 },
        { "job_id": "752d1ab2-...", "source_file": "backup/quote.eml", "original_uid": 3 }
      ],
      "issues": [],
      "merge_info": {
        "source_count": 2,
        "duplicates_merged": 1,
        "conflict": false,
        "conflict_with": [],
        "relinked_parent": null,
        "notes": ["同一邮件（SHA-256 相同）在 2 个来源中重复出现，已合并为一个节点；全部来源见 sources"]
      },
      "children": []
    }
  ]
}
```

约定：

* `sources` 保留**全部来源作业与文件名**（含原作业内的 `original_uid`），
  顺序为首次出现顺序；`sources[0]` 为首要来源；
* `issues` 为首个来源作业中该邮件的原始问题（审计留痕）；合并期说明
  一律写入 `merge_info.notes`，两者互不覆盖；
* `merge_info.relinked_parent` 为跨包补链依据：
  `{message_id, via, resolved_for_jobs, note}`，仅当父邮件在某个来源
  作业中缺失、跨包找到时存在；
* `compact` 视图省略正文/附件/问题明细，保留 `sources`、`conflict`、
  `relinked` 等合并标记。

### 跨包合并规则

1. **去重**：SHA-256 相同的邮件合并为一个节点，全部来源保留在
   `sources`；
2. **冲突**：Message-ID 相同但 SHA-256 不同的邮件并列保留为独立节点，
   `merge_info.conflict=true` 且 `conflict_with` 列出冲突方 uid；
   引用该 ID 的邮件**不猜测父节点**——该引用边被跳过并记录说明，
   可继续沿 References 上溯无歧义的祖先；
3. **补链**：父邮件在某来源作业包内缺失、但在案件其他作业中找到时，
   跨包挂载并记录 `relinked_parent`；
4. **成环/自引用**：与单作业一致，断开成环边并标注，输出严格无环；
5. 森林按 `(date, 首要来源文件, uid)` 排序。

## 11. 下载案件合并结果

```
GET /api/v1/cases/{case_id}/result
```

`200`，响应头：

```
Content-Type: application/json; charset=utf-8
Content-Disposition: attachment; filename="<case_id>.case-result.json"
```

响应体为完整结果文件（比 `/tree` 多 `status`、`created_at`、
`completed_at` 顶层字段）。完成前请求返回 `409 not_ready`。

**结果独立落盘**（`DATA_DIR/cases/<case_id>/result.json`）：案件完成后
删除源作业不影响案件结果的读取与下载；服务重启后，未完成的案件会
自动重新入队处理。

## 12. 创建时序核验分析

```
POST /api/v1/analyses
Content-Type: application/json
```

对一个**已完成**的作业或案件做邮件传输时序核验：结合 `Date`、
`Received` 跳点链与会话父子关系，按可配置阈值标出时序异常。

**请求体**

```json
{
  "target_type": "job",
  "target_id": "<已完成作业或案件的UUID>",
  "thresholds": {
    "clock_skew_seconds": 120,
    "max_transit_seconds": 300
  }
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `target_type` | 是 | `"job"` 或 `"case"` |
| `target_id` | 是 | 目标作业/案件 UUID |
| `thresholds` | 否 | 阈值对象，缺省项用默认值；两项均须为 0 到 `MAILRECON_MAX_THRESHOLD_SECONDS`（默认 604800）之间的整数 |
| `thresholds.clock_skew_seconds` | 否 | 客户端时钟偏差容差（秒），默认 `MAILRECON_DEFAULT_CLOCK_SKEW_SECONDS`（120） |
| `thresholds.max_transit_seconds` | 否 | 单跳传输耗时上限（秒），默认 `MAILRECON_DEFAULT_MAX_TRANSIT_SECONDS`（300） |

**响应** `201 Created`：

```json
{
  "id": "59329c0d-99b5-414b-b9c9-a59b5b314402",
  "status": "queued",
  "created_at": "2026-09-10T20:48:11+00:00",
  "updated_at": "2026-09-10T20:48:11+00:00",
  "target_type": "job",
  "target_id": "a0816b74-...",
  "thresholds": { "clock_skew_seconds": 120, "max_transit_seconds": 300 },
  "progress": 0,
  "phase": null,
  "error": null,
  "email_count": 0,
  "finding_count": 0,
  "stats": null
}
```

**创建即校验**：目标不存在或已删除返回 `404 target_not_found`；
目标未完成返回 `409 target_not_completed`；阈值非法返回
`400 bad_request`。

## 13. 查询分析进度 / 分析列表

```
GET /api/v1/analyses/{analysis_id}
GET /api/v1/analyses
```

`status`：`queued` → `processing` → `completed` | `failed`

`phase` 依次为：`loading_target`、`flattening_threads`、
`checking_timing`、`writing_result`、`completed` / `failed`。

`failed` 时 `error` 给出原因（例如目标在分析处理前被删除）。
完成后 `stats` 结构：

```json
{
  "emails": 6,
  "emails_with_received": 5,
  "hops_total": 9,
  "hops_with_issues": 1,
  "findings_total": 5,
  "findings_by_type": {
    "client_clock_skew": 2,
    "abnormal_transit": 1,
    "hop_time_inversion": 1,
    "reply_before_parent": 1,
    "chain_mismatch": 0
  }
}
```

## 14. 读取分析时间线（按时间/异常类型筛选）

```
GET /api/v1/analyses/{analysis_id}/timeline
GET /api/v1/analyses/{analysis_id}/timeline?from=2026-09-01T09:00:00%2B00:00&to=2026-09-02T00:00:00%2B00:00
GET /api/v1/analyses/{analysis_id}/timeline?type=client_clock_skew
```

仅在 `completed` 后可用，否则 `409 not_ready`。查询参数可组合：

| 参数 | 说明 |
|---|---|
| `from` / `to` | 时间区间边界，ISO 8601 且**必须带时区**（缺时区返回 400，不做时区猜测）；无定位时间的条目在给出时间筛选时不返回（无法判断，不猜测） |
| `type` | 异常类型，须为 `client_clock_skew` / `abnormal_transit` / `hop_time_inversion` / `reply_before_parent` / `chain_mismatch` 之一，未知值返回 400 并列出可选值 |

响应：

```json
{
  "analysis_id": "...",
  "target_type": "job",
  "target_id": "...",
  "thresholds": { "clock_skew_seconds": 120, "max_transit_seconds": 300 },
  "filters": { "from": null, "to": null, "type": "client_clock_skew" },
  "entry_count": 1,
  "entries": [
    {
      "node_uid": 1,
      "message_id": "<timing-skew@example.com>",
      "subject": "补充材料（发送端时钟异常）",
      "from": { "name": "李雷", "address": "lilei@example.com" },
      "date": "2026-09-01T09:00:00+00:00",
      "time": "2026-09-01T09:00:00+00:00",
      "time_basis": "date",
      "hop_count": 1,
      "source": { "job_id": "...", "source_file": "02-clock-skew.eml" },
      "finding_ids": ["F0001"],
      "notes": [],
      "findings": [
        {
          "id": "F0001",
          "type": "client_clock_skew",
          "time": "2026-09-01T09:00:00+00:00",
          "node_uids": [1],
          "message_ids": ["<timing-skew@example.com>"],
          "summary": "Date 头与首跳接收时间相差 720 秒，超过时钟偏差阈值 120 秒，疑似客户端时钟偏慢",
          "fields": ["Date", "Received"],
          "thresholds": { "clock_skew_seconds": 120 },
          "evidence": {
            "date_header": "2026-09-01T09:00:00+00:00",
            "first_hop": { "index": 0, "from_host": "client-han", "by_host": "mail.example.com", "time_utc": "2026-09-01T09:12:00+00:00", "timezone": "+0000" },
            "skew_seconds": 720.0,
            "direction": "client_behind"
          }
        }
      ]
    }
  ]
}
```

约定：

* 条目按定位时间 `time` 升序；`time` 优先取 `Date`（时区可用时，
  `time_basis="date"`），否则取最早一跳的 UTC 时间
  （`time_basis="received"`），都不可用时为 `null`（排在最后）；
* `notes` 列出该邮件无法执行的检查及原因（如 Date 缺时区、某跳
  Received 无法解析），与结论一样只说明、不猜测；
* `findings` 为该条目命中的结论（给出 `type` 筛选时只含该类型）；
  每条结论都带 `fields`（判定所用字段）、`thresholds`（本次判定
  使用的阈值，无阈值类结论为 `{}`）与 `evidence`（具体取值）；
* 时间线条目的 `source`：作业分析为 `{job_id, source_file}`，
  案件分析为 `{case_id, sources}`（含全部来源作业）。

### 结论类型与证据结构

| type | 含义 | 关键 evidence |
|---|---|---|
| `client_clock_skew` | `Date` 与首跳接收时间差超过 `clock_skew_seconds` | `date_header`、`first_hop`、`skew_seconds`、`direction`（`client_behind`/`client_ahead`） |
| `abnormal_transit` | 相邻两跳间隔超过 `max_transit_seconds` | `gap_seconds`、`earlier_hop`、`later_hop` |
| `hop_time_inversion` | 相邻两跳时间逆序（不猜测原因） | `gap_seconds`（负值）、`earlier_hop`、`later_hop` |
| `reply_before_parent` | 回复的 `Date` 早于父邮件超过 `clock_skew_seconds` 容差 | `reply`、`parent`、`diff_seconds` |
| `chain_mismatch` | 同一 Message-ID 不同来源的 Received 链不一致 | `variants`：各版本传输链**并列**（`raw_sha256`、`sources`、`hops`），不做取舍 |

跳点编号 `index` 一律按 Received 头在邮件中的出现顺序（0 = 最上方 =
最后一跳）。缺可用时间的跳不参与相邻比较，原因记录在该跳的
`issues` 与条目的 `notes` 中。

## 15. 下载分析结果 JSON

```
GET /api/v1/analyses/{analysis_id}/result
```

`200`，响应头：

```
Content-Type: application/json; charset=utf-8
Content-Disposition: attachment; filename="<analysis_id>.analysis-result.json"
```

响应体为完整结果文件（顶层含 `analysis_id`、`target_type`、
`target_id`、`target`、`thresholds`、`stats`、`findings`、
`timeline`）。完成前请求返回 `409 not_ready`。

**结果独立落盘**（`DATA_DIR/analyses/<analysis_id>/result.json`）：
分析完成后删除源作业/案件不影响结果的读取与下载；服务重启后，
未完成的分析会自动重新入队处理（沿用创建时的阈值）。

## 16. 创建声明身份核验

```
POST /api/v1/identity-checks
Content-Type: application/json
```

对一个**已完成**的作业或案件做邮件声明身份核验。解析期（见第 4 节
`identity` 头块）已离线提取 From / Sender / Reply-To / Return-Path /
Message-ID 的地址与域名，以及 DKIM-Signature 的 `d`/`s`/`i`/`h` 标签
（保留重复头与原始值）。本核验在后台汇总四类声明差异。

**离线边界（重要）**：不查 DNS、不校验 DKIM 签名（`b=`）真伪、不评价
SPF/DMARC 对齐、**不做任何“伪造”定性**。转发（`Fwd:` 主题）、邮件列表
迹象、字段缺失等只作为 `review_flags` 待复核证据；证据不足的检查标
`inconclusive`（无法核验），不直接下结论。

**请求体**

```json
{
  "target_type": "job",
  "target_id": "<已完成作业或案件的UUID>"
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `target_type` | 是 | `"job"` 或 `"case"` |
| `target_id` | 是 | 目标作业/案件 UUID |

无阈值参数；出现未知字段返回 `400 bad_request`。

**响应** `201 Created`：

```json
{
  "id": "6b51f0a2-1c6b-4e94-8b3e-7d0b5f7a9201",
  "status": "queued",
  "created_at": "2026-09-10T21:00:00+00:00",
  "updated_at": "2026-09-10T21:00:00+00:00",
  "target_type": "job",
  "target_id": "a0816b74-...",
  "progress": 0,
  "phase": null,
  "error": null,
  "email_count": 0,
  "finding_count": 0,
  "review_count": 0,
  "stats": null
}
```

**创建即校验**：目标不存在或已删除返回 `404 target_not_found`；
目标未完成返回 `409 target_not_completed`；请求体非 JSON / 缺字段 /
ID 格式非法 / 未知字段返回 `400 bad_request`。

### 发现类型与状态

| type | scope | 含义 |
|---|---|---|
| `from_domain_mismatch` | email | From 与任一 Sender / Return-Path 值（含重复头的每个值）域不一致 |
| `reply_identity_change` | thread | 沿回复链父子边，相邻两封邮件 From 域变化 |
| `message_id_domain_drift` | email / thread | 邮件级：Message-ID 标识域 ≠ From 域；会话级：同一发件人在同一会话内 Message-ID 域漂移 |
| `dkim_from_not_covered` | email | 某 DKIM-Signature 的 `h=` 未列出 from（未查 DNS、未验签） |

每条 `finding` 的 `status`：

* `observed` — 客观观察到声明差异（不代表伪造）；
* `needs_review` — 存在转发 / 邮件列表 / 多重签名等背景，需人工复核；
* `inconclusive` — 证据不足（字段缺失等），无法核验。

每条发现都带 `sources`（来源文件/作业）、`headers`（依据的具体头字段）、
`basis`（判定依据说明）与 `evidence`（具体取值）。待复核背景与字段缺失
另列为 `review_flags`，`kind` 为 `possible_forward` /
`possible_mailing_list` / `missing_header` / `header_parse_anomaly`。

## 17. 查询核验进度 / 核验列表

```
GET /api/v1/identity-checks/{check_id}
GET /api/v1/identity-checks
```

`status`：`queued` → `processing` → `completed` | `failed`

`phase` 依次为：`loading_target`、`flattening_threads`、
`checking_identity`、`writing_result`、`completed` / `failed`。
`progress` 为 0–100 整数；`failed` 时 `error` 给出原因（例如目标在核验
处理前被删除）。完成后 `stats` 结构：

```json
{
  "emails": 9,
  "thread_count": 4,
  "findings_total": 9,
  "findings_by_type": {
    "from_domain_mismatch": 3,
    "reply_identity_change": 1,
    "message_id_domain_drift": 1,
    "dkim_from_not_covered": 4
  },
  "findings_by_status": {
    "observed": 2,
    "needs_review": 7,
    "inconclusive": 0
  },
  "review_flags_total": 8,
  "review_flags_by_kind": {
    "possible_forward": 1,
    "possible_mailing_list": 3,
    "missing_header": 1,
    "header_parse_anomaly": 3
  },
  "inconclusive_checks": {
    "from_domain_mismatch": 0,
    "message_id_domain_drift": 0,
    "dkim_from_not_covered": 1
  }
}
```

## 18. 读取发现 / 按邮件汇总 / 按会话汇总（可筛选）

核验 `completed` 后可用，否则 `409 not_ready`。

### 18.1 发现与待复核证据（按类型 / 状态 / 域名筛选）

```
GET /api/v1/identity-checks/{check_id}/findings
GET /api/v1/identity-checks/{check_id}/findings?type=dkim_from_not_covered
GET /api/v1/identity-checks/{check_id}/findings?status=needs_review
GET /api/v1/identity-checks/{check_id}/findings?domain=mailer.net
```

| 参数 | 说明 |
|---|---|
| `type` | 发现类型，须为 `from_domain_mismatch` / `reply_identity_change` / `message_id_domain_drift` / `dkim_from_not_covered`，未知值返回 400 |
| `status` | `observed` / `needs_review` / `inconclusive`，未知值返回 400 |
| `domain` | 域名（大小写不敏感，含子域）；筛选证据中涉及该域的发现。空值返回 400 |

参数可组合。响应顶层为 `findings`（差异发现）与 `review_flags`
（转发/列表/缺失/解析异常等待复核证据），各带 `finding_count` /
`review_flag_count` 与回显的 `filters`。每条发现示例：

```json
{
  "id": "F0001",
  "type": "from_domain_mismatch",
  "scope": "email",
  "status": "needs_review",
  "node_uids": [1],
  "message_ids": ["<mm@example.com>"],
  "sources": [ { "job_id": "...", "source_file": "mismatch.eml" } ],
  "headers": ["From", "Return-Path"],
  "summary": "From 与 Return-Path#1 域 evil.test ≠ From 域 example.com；仅记录声明域差异，不判定伪造",
  "basis": "比较 From 与全部 Sender/Return-Path 值（含重复头）首个地址的域名（小写精确比较）",
  "evidence": {
    "from": { "address": "a@example.com", "name": "A", "domain": "example.com" },
    "comparisons": [
      { "header": "Return-Path", "index": 0, "present": true, "address": "a@example.com", "domain": "example.com", "match": true },
      { "header": "Return-Path", "index": 1, "present": true, "address": "bounce@evil.test", "domain": "evil.test", "match": false }
    ],
    "mismatched": [ { "header": "Return-Path", "index": 1, "domain": "evil.test", "match": false } ],
    "review_signals": []
  }
}
```

约定：

* 重复 Sender / Return-Path 的**每个值**都参与比较，`comparisons` /
  `mismatched` 单元带 `index`（头出现序号，0 = 最上方）；
* 案件分析的 `sources` 为 `{case_id, sources:[...]}`（含全部来源作业）；
* 会话级发现（`scope=thread`）的 `node_uids` 为回复链上相关节点。

### 18.2 按邮件汇总

```
GET /api/v1/identity-checks/{check_id}/emails
GET /api/v1/identity-checks/{check_id}/emails?type=from_domain_mismatch
GET /api/v1/identity-checks/{check_id}/emails?status=inconclusive
GET /api/v1/identity-checks/{check_id}/emails?domain=example.com
```

每封邮件一项，含 From / Sender / Return-Path / Reply-To 域、Message-ID
域、各 DKIM 签名（`d`/`s`/`i`/`covers_from`）、背景信号 `signals` 与三类
邮件级检查单元 `checks`（每项带 `status` / `reason`，差异类还带
`match: false`）。

* `type` 仅接受邮件级类型：`from_domain_mismatch` /
  `message_id_domain_drift` / `dkim_from_not_covered`；传会话级的
  `reply_identity_change` 返回 400。类型筛选只返回**确有差异/待复核**
  的邮件（一致的正常邮件不返回）；
* 无法核验的邮件用 `status=inconclusive` 筛选（如缺 From / DKIM）。

### 18.3 按会话汇总

```
GET /api/v1/identity-checks/{check_id}/threads
GET /api/v1/identity-checks/{check_id}/threads?type=reply_identity_change
GET /api/v1/identity-checks/{check_id}/threads?domain=example-corp.example
```

每个会话（线程树）一项：`root_uid` / `root_message_id` /
`root_subject` / `email_count` / `node_uids` / `finding_ids` /
`identity_sequences`（链上每封的 From 与标识域序列），并内联该会话命中
的 `findings`。`type` / `domain` 筛选作用于内联发现；无命中的会话不返回。

## 19. 核验的证据与无法核验口径

* **每条发现可溯源**：`sources` 指出来源文件（作业）或全部来源作业
  （案件），`headers` 指出依据的具体头字段，`evidence` 给出具体取值，
  `basis` 给出人类可读判定依据；
* **转发 / 邮件列表 / 字段缺失只记待复核证据**：不直接判为差异，相关
  发现降级为 `needs_review`；
* **证据不足标 `inconclusive`（无法核验），绝不判定伪造**。例如：
  缺 From 时无法核验 From/Sender 域一致性，也无法断言 DKIM `h=`
  “未覆盖 From”（没有可被覆盖的 From）；缺 DKIM-Signature 时
  `dkim_from_not_covered` 为 `inconclusive`（不代表无签名即伪造）；
* DKIM 只检查 `h=` 是否列出 from 这一**覆盖关系**，`b=` 签名字段绝不
  验证；多重签名中部分覆盖、或 `h=` 无法解析时为 `needs_review`。

## 20. 下载核验结果 JSON

```
GET /api/v1/identity-checks/{check_id}/result
```

`200`，响应头：

```
Content-Type: application/json; charset=utf-8
Content-Disposition: attachment; filename="<check_id>.identity-result.json"
```

响应体为完整结果文件（顶层含 `identity_check_id`、`target_type`、
`target_id`、`target`、`stats`、`findings`、`review_flags`、
`email_reports`、`thread_reports`）。完成前请求返回 `409 not_ready`。

**结果独立落盘**（`DATA_DIR/identity_checks/<check_id>/result.json`）：
核验完成后删除源作业/案件不影响结果的读取与下载；服务重启后，未完成的
核验会自动重新入队处理。

## 21. 创建收件人流转分析

```
POST /api/v1/recipient-flows
Content-Type: application/json
```

对一个**已完成**的作业或案件做收件人流转分析。分析沿会话树的每一条
父子回复边，逐条比较两封邮件的 **From / To / Cc 可见地址集合**，在后台
运行并把结果独立落盘。

**匹配规则**：

* **忽略显示名**：只用 `local-part@domain` 比较，显示名（`张三`、
  `Alice` 等）完全不参与；
* **域名转小写，local-part 保持原样**：`a@X.com` 与 `a@x.com` 视为
  同一地址；`John@x.com` 与 `john@x.com` **不**合并（不猜测 local-part
  的大小写语义）；
* 同一封邮件 To/Cc 中重复出现的地址按集合去重，但地址台账保留全部出现。

**离线边界（重要）**：只比较邮件头中**可见**的 From/To/Cc。**绝不读取
或推断 Bcc**，不使用 Sender / Return-Path / Received 等 **SMTP 信封**
信息猜测谁真正收到，不查 DNS；分析只读结果 JSON，**绝不改写现有会话树**。

**请求体**

```json
{
  "target_type": "job",
  "target_id": "<已完成作业或案件的UUID>"
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `target_type` | 是 | `"job"` 或 `"case"` |
| `target_id` | 是 | 目标作业/案件 UUID |

无阈值参数；出现未知字段返回 `400 bad_request`。

**响应** `201 Created`：

```json
{
  "id": "7b86e437-99d0-480d-be4d-860bd6bcdb83",
  "status": "queued",
  "created_at": "2026-09-11T09:00:00+00:00",
  "updated_at": "2026-09-11T09:00:00+00:00",
  "target_type": "job",
  "target_id": "a0816b74-...",
  "progress": 0,
  "phase": null,
  "error": null,
  "email_count": 0,
  "event_count": 0,
  "review_count": 0,
  "stats": null
}
```

**创建即校验**：目标不存在或已删除返回 `404 target_not_found`；目标未
完成返回 `409 target_not_completed`；请求体非 JSON / 缺字段 / ID 格式
非法 / 未知字段返回 `400 bad_request`。

### 事件类型

| type | 含义 |
|---|---|
| `added` | 相对父邮件参与者集合（From∪To∪Cc）新增的可见地址 |
| `dropped` | 父邮件原 To/Cc 收件人未继续列入子邮件 To/Cc，且不是子邮件发件人（真正从收件人名单消失；父发件人“变成我回复”不计入） |
| `role_changed` | 同一地址在父子邮件间 To/Cc 角色变化（To→Cc 或 Cc→To），证据带 `from_role`/`to_role` |
| `reply_all_omitted` | 相对父邮件全部参与者（From∪To∪Cc），子邮件可见集合缺失的地址（疑似未全部回复） |

### 待复核（差异只列待复核，不生成客观事件）

以下背景下的父子边差异**不升为 `events`**，而以 `reviews` 形式给出
（仍附完整集合差异快照供人工判断）：

* `possible_mailing_list` — 邮件列表/群组迹象：多地址 From（含 RFC
  群组语法 `组名: a@x, b@y;`）、与 From 不同域的 Sender/Reply-To、
  Sender/Reply-To 重复头，或可见地址为高置信列表形态
  （`*-owners`、`*-request`、`*-subscribe`、`list-*`、
  `bounces@`、`majordomo@` 等）；
* `malformed_address` — From/To/Cc 中存在无法可靠归一化的畸形地址
  （无 `@`、多个 `@`、含空白、域名标签非法、引号 local-part 等），
  该地址不参与自动比较；
* `missing_parent` — 声称回复但父邮件不在目标范围内，或挂载到
  References 上溯祖先（直接父缺失）；
* `reference_conflict` — 引用边被断开（成环/自引用），或被引用的
  Message-ID 对应多封内容不同的邮件，父节点不确定。

## 22. 查询分析进度 / 分析列表

```
GET /api/v1/recipient-flows/{flow_id}
GET /api/v1/recipient-flows
```

`status`：`queued` → `processing` → `completed` | `failed`

`phase` 依次为：`loading_target`、`flattening_threads`、
`analyzing_recipients`、`writing_result`、`completed` / `failed`。
`progress` 为 0–100 整数；`failed` 时 `error` 给出原因（例如目标在
分析处理前被删除）。完成后 `stats` 结构：

```json
{
  "emails": 9,
  "unique_addresses": 12,
  "malformed_addresses": 1,
  "threads_total": 4,
  "edges_total": 5,
  "edges_compared": 2,
  "edges_with_changes": 2,
  "events_total": 11,
  "events_by_type": {
    "added": 3,
    "dropped": 4,
    "role_changed": 1,
    "reply_all_omitted": 4
  },
  "reviews_total": 6,
  "reviews_by_kind": {
    "possible_mailing_list": 3,
    "malformed_address": 2,
    "missing_parent": 1,
    "reference_conflict": 0
  }
}
```

## 23. 读取事件 / 待复核（按会话、地址、类型筛选）

分析 `completed` 后可用，否则 `409 not_ready`。

```
GET /api/v1/recipient-flows/{flow_id}/events
GET /api/v1/recipient-flows/{flow_id}/events?type=reply_all_omitted
GET /api/v1/recipient-flows/{flow_id}/events?thread=0
GET /api/v1/recipient-flows/{flow_id}/events?address=carl@example.org
GET /api/v1/recipient-flows/{flow_id}/events?kind=possible_mailing_list
```

| 参数 | 说明 |
|---|---|
| `type` | 事件类型，须为 `added` / `dropped` / `role_changed` / `reply_all_omitted`，未知值返回 400；给出 `type` 时只返回事件、不返回待复核项 |
| `kind` | 待复核类型，须为 `possible_mailing_list` / `malformed_address` / `missing_parent` / `reference_conflict`，未知值返回 400 |
| `thread` | 会话根节点 uid（非负整数） |
| `address` | 完整邮箱地址（`local-part@domain`）；与引擎同口径匹配（域名小写、local-part 原样），非法形态返回 400。事件按其**主地址**匹配（即该条差异针对的地址），待复核按差异清单/畸形地址/列表提示中的地址匹配 |

参数可组合。响应顶层同时给出 `events`（客观事件）与 `reviews`
（待复核项），各带 `event_count` / `review_count` 与回显的 `filters`。
每条事件示例：

```json
{
  "id": "E0007",
  "type": "reply_all_omitted",
  "thread_root_uid": 0,
  "thread_root_message_id": "<rf-root@example.com>",
  "email": {
    "uid": 1,
    "message_id": "<rf-reply-1@example.com>",
    "subject": "Re: 合同付款安排",
    "date": "2026-09-07T09:20:00+00:00",
    "source": { "job_id": "...", "source_file": "02-reply-only-sender.eml" }
  },
  "parent_email": {
    "uid": 0,
    "message_id": "<rf-root@example.com>",
    "date": "2026-09-07T09:00:00+00:00"
  },
  "time": "2026-09-07T09:20:00+00:00",
  "address": "carl@example.org",
  "fields": ["From", "To", "Cc"],
  "summary": "地址 carl@example.org 相对父邮件参与者集合被遗漏（疑似未全部回复；仅据可见头，不含 Bcc/信封）",
  "basis": "沿会话父子边比较 From/To/Cc 可见地址集合（忽略显示名，域名转小写、local-part 保持原样）；不使用 Bcc 或 SMTP 信封，不推断实际送达对象",
  "evidence": {
    "parent_participant_count": 4,
    "child_participant_count": 2,
    "sets": {
      "parent": {
        "from": ["legal@example.com"],
        "to": ["bob@example.org", "carl@example.org"],
        "cc": ["dana@example.org"]
      },
      "child": {
        "from": ["bob@example.org"],
        "to": ["legal@example.com"],
        "cc": []
      },
      "differences": {
        "added": [],
        "dropped": ["carl@example.org", "dana@example.org"],
        "role_changed": [],
        "reply_all_omitted": ["carl@example.org", "dana@example.org"]
      }
    }
  }
}
```

约定：

* 每条事件都附父子邮件（`email` / `parent_email`，含 uid、
  Message-ID、时间、来源）、会话根（`thread_root_uid` /
  `thread_root_message_id`）、涉及字段 `fields`、主地址 `address`、
  判定依据 `basis` 与集合差异快照 `evidence.sets`（父子各自的
  from/to/cc 与四类差异清单）；
* `role_changed` 事件额外在 `evidence` 中给出 `from_role`、
  `to_role`、`parent_fields`、`child_fields`；
* `added` 的 `fields` 为该地址在子邮件中出现的字段，`dropped` 的
  `fields` 为其在父邮件中出现的字段；
* 案件分析的 `source` 为 `{case_id, sources:[...]}`（含全部来源作业）。

待复核项（`reviews`）结构与事件同级，带 `kind`、`fields`、`email`、
会话根与 `evidence`：邮件级背景（列表/畸形）挂在对应邮件上；
父子边背景（`evidence.background_kinds`、`evidence.sets`）挂在子邮件上，
其中集合差异快照与事件口径一致，便于人工复核。

## 24. 按会话 / 地址台账查询

### 24.1 按会话汇总

```
GET /api/v1/recipient-flows/{flow_id}/threads
GET /api/v1/recipient-flows/{flow_id}/threads?address=carl@example.org
GET /api/v1/recipient-flows/{flow_id}/threads?kind=missing_parent
```

每个会话（线程树）一项：`root_uid` / `root_message_id` /
`email_count` / `node_uids` / `event_ids` / `review_ids` /
`event_count` / `review_count`，并内联该会话命中的 `events` 与
`reviews`（带 `matched_event_count` / `matched_review_count`）。
支持 `address`、`kind`、`thread` 筛选；无命中的会话不返回。

### 24.2 地址台账

```
GET /api/v1/recipient-flows/{flow_id}/addresses
GET /api/v1/recipient-flows/{flow_id}/addresses?address=dana@X.COM
```

每个唯一（归一化）地址一项：`address`、该地址出现过的全部显示名
`names`、出现字段 `fields`（From/To/Cc）、首次出现位置 `first_seen`
与全部出现记录 `occurrences`（每次所在邮件与字段）。`address` 筛选
按归一化键精确匹配（域名大小写不敏感、local-part 原样）。

## 25. 下载分析结果 JSON

```
GET /api/v1/recipient-flows/{flow_id}/result
```

`200`，响应头：

```
Content-Type: application/json; charset=utf-8
Content-Disposition: attachment; filename="<flow_id>.recipient-flow-result.json"
```

响应体为完整结果文件（顶层含 `recipient_flow_id`、`target_type`、
`target_id`、`target`、`stats`、`events`、`reviews`、`threads`、
`emails`、`addresses`；`emails` 为按邮件的汇总行，含可见地址计数、
是否参与比较、`skip_reason` 与关联的事件/待复核 ID）。完成前请求返回
`409 not_ready`。

**结果独立落盘**（`DATA_DIR/recipient_flows/<flow_id>/result.json`）：
分析完成后删除源作业/案件不影响结果的读取与下载；服务重启后，未完成
的分析会自动重新入队处理。

## 错误响应格式

所有错误统一为：

```json
{ "error": { "code": "idempotency_conflict",
             "message": "同一 Idempotency-Key 已用于内容不同的上传，请更换 Key 后重试" } }
```

| 状态码 | code | 触发场景 |
|---|---|---|
| 400 | `bad_request` | 请求体为空、multipart 缺 `file`、作业/案件/分析/核验/追踪 ID 格式非法、Idempotency-Key 格式非法、案件/分析/核验/追踪请求体非合法 JSON、案件作业数超限、分析阈值非法、时间线/核验/收件人流转筛选参数非法（未知 type/status/kind、空 domain/address、非法 address/thread、邮件端点传会话级类型） |
| 404 | `not_found` | 路径或作业/案件/分析/核验/追踪不存在 |
| 404 | `job_not_found` | 创建案件时引用的源作业不存在或已删除 |
| 404 | `target_not_found` | 创建分析/核验时引用的目标作业/案件不存在或已删除 |
| 409 | `idempotency_conflict` | 同 Key 不同内容 |
| 409 | `not_ready` | 作业/案件/分析/核验未完成时取树/时间线/发现/结果 |
| 409 | `job_processing` | 删除正在处理的作业 |
| 409 | `job_not_completed` | 创建案件时引用的源作业未完成 |
| 409 | `target_not_completed` | 创建分析/核验时引用的目标作业/案件未完成 |
| 409 | `duplicate_job` | 同一作业在同一案件中重复提交 |
| 409 | `case_too_large` | 案件邮件总量超过 `MAILRECON_MAX_CASE_EMAILS` |
| 411 | `length_required` | 缺 Content-Length |
| 413 | `payload_too_large` | 超过上传体积上限 |
| 415 | `unsupported_media_type` | Content-Type 不是支持的类型 |
| 500 | `internal_error` | 未预期内部错误（作业/案件/分析/核验内部异常会落为对应 `failed`，不会返回 500） |

ZIP 安全/超限问题不使用上述 HTTP 错误码——上传会被受理（201），
后台校验失败后体现在作业的 `status=failed` 与 `error` 文本中，
原因前缀固定为 `压缩包被拒绝:`。
