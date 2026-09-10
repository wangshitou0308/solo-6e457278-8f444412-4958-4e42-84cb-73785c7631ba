# mailrecon — 本地历史邮件包会话重建 API

供法务与支持团队整理历史邮件包的**纯本地、零外部依赖**服务：接收含 `.eml`
文件的 ZIP，安全校验后解析 MIME，按 `Message-ID` / `In-Reply-To` /
`References` 引用链重建会话树，并通过 HTTP 接口查询与下载 JSON 结果。

* 仅使用 Python 标准库：`http.server`、`email`、`zipfile`、`sqlite3`；
* 不修改、不回传任何原始邮件，附件只导出元数据（名称/类型/大小/SHA-256）；
* 问题只标注、不阻断：重复 Message-ID、父邮件缺失、引用成环、正文无法解码
  等情况都会写入节点的 `issues`；压缩包级别的安全问题才拒绝整包。

## 目录结构

```
mailrecon/            服务源码
  config.py           上限/路径配置（环境变量可覆盖）
  zipguard.py         ZIP 安全校验与受控解压
  mailparser.py       单封 .eml 解析（MIME/字符集/正文/附件/引用头）
  threads.py          会话树重建（缺失/成环/重复 ID 处理，迭代式）
  jsonio.py           迭代式 JSON 解析/序列化（深引用链不受递归深度限制）
  storage.py          SQLite 元数据 + 作业落盘文件管理
  processor.py        后台作业流水线（单线程顺序处理）
  server.py           HTTP API（http.server）
  __main__.py         启动入口
scripts/
  make_sample.py      生成正常示例包 examples/sample-mails.zip
  make_evil.py        生成应被拒绝的恶意/超限 ZIP
tests/                65 个 unittest 用例
examples/             生成产物（正常包 + evil/ 恶意包）
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

## 作业落盘与删除

```
DATA_DIR/mailrecon.db              SQLite 元数据
DATA_DIR/jobs/<job_id>/upload.bin  收到的原始 ZIP（字节不动）
DATA_DIR/jobs/<job_id>/result.json 结果 JSON
DATA_DIR/jobs/<job_id>/extract/    处理期间临时解压目录，完成后立即删除
```

`DELETE /api/v1/jobs/{id}` 会删除元数据并 `rmtree` 整个作业目录；
服务重启后，上次未完成的作业会自动重新入队处理。

## 测试

```bash
python3 -m unittest discover -s tests -v     # 65 个用例
```

## 更多

* 接口字段、状态码与错误码：[`docs/API.md`](docs/API.md)
* 调用示例：[`docs/API_EXAMPLES.sh`](docs/API_EXAMPLES.sh)
* 恶意包生成：`python3 scripts/make_evil.py --help`
