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

## 错误响应格式

所有错误统一为：

```json
{ "error": { "code": "idempotency_conflict",
             "message": "同一 Idempotency-Key 已用于内容不同的上传，请更换 Key 后重试" } }
```

| 状态码 | code | 触发场景 |
|---|---|---|
| 400 | `bad_request` | 请求体为空、multipart 缺 `file`、作业 ID 格式非法、Idempotency-Key 格式非法 |
| 404 | `not_found` | 路径或作业不存在 |
| 409 | `idempotency_conflict` | 同 Key 不同内容 |
| 409 | `not_ready` | 作业未完成时取树/结果 |
| 409 | `job_processing` | 删除正在处理的作业 |
| 411 | `length_required` | 缺 Content-Length |
| 413 | `payload_too_large` | 超过上传体积上限 |
| 415 | `unsupported_media_type` | Content-Type 不是支持的类型 |
| 500 | `internal_error` | 未预期内部错误（作业内部异常会落为作业 `failed`，不会返回 500） |

ZIP 安全/超限问题不使用上述 HTTP 错误码——上传会被受理（201），
后台校验失败后体现在作业的 `status=failed` 与 `error` 文本中，
原因前缀固定为 `压缩包被拒绝:`。
