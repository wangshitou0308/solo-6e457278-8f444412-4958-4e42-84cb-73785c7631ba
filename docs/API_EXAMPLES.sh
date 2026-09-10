#!/usr/bin/env bash
# mailrecon 调用示例。
# 前置：python3 -m mailrecon --port 8080
# 并已生成示例包：python3 scripts/make_sample.py
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8080}"
ZIP="${ZIP:-examples/sample-mails.zip}"

# ---------------------------------------------------------------- 0. 健康检查
curl -s "$BASE/health"; echo

# ---------------------------------------------------------------- 1. 创建作业
# multipart 方式（推荐），带 Idempotency-Key
JOB_JSON=$(curl -s -X POST "$BASE/api/v1/jobs" \
  -H 'Idempotency-Key: legal-case-2026-09-001' \
  -F "file=@${ZIP};type=application/zip")
echo "$JOB_JSON"
JOB_ID=$(python3 -c "import json,sys; print(json.loads(sys.stdin.read())['id'])" <<<"$JOB_JSON")

# 原始 ZIP 字节方式（无 multipart）
# curl -s -X POST "$BASE/api/v1/jobs" \
#   -H 'Content-Type: application/zip' \
#   -H 'X-Filename: sample-mails.zip' \
#   --data-binary "@${ZIP}"

# ---------------------------------------------------------------- 2. 幂等重放
# 同样的 Key + 同样的字节：200，idempotent_replayed=true，返回同一个作业 ID
curl -s -X POST "$BASE/api/v1/jobs" \
  -H 'Idempotency-Key: legal-case-2026-09-001' \
  -F "file=@${ZIP};type=application/zip"; echo

# 同样的 Key + 不同字节：409 idempotency_conflict
# head -c 100 "$ZIP" > /tmp/other.zip
# curl -s -X POST "$BASE/api/v1/jobs" \
#   -H 'Idempotency-Key: legal-case-2026-09-001' \
#   -F 'file=@/tmp/other.zip;type=application/zip'

# ---------------------------------------------------------------- 3. 轮询进度
while :; do
  JOB=$(curl -s "$BASE/api/v1/jobs/$JOB_ID")
  STATUS=$(python3 -c "import json,sys; j=json.load(sys.stdin); print(j['status'])" <<<"$JOB")
  echo "$JOB" | python3 -c "import json,sys; j=json.load(sys.stdin); print(j['status'], j['progress'], j['phase'])"
  if [ "$STATUS" = completed ] || [ "$STATUS" = failed ]; then break; fi
  sleep 0.5
done
echo "$JOB" | python3 -m json.tool

# ---------------------------------------------------------------- 4. 读取会话树
# 完整树（含正文/附件元数据/issues）
curl -s "$BASE/api/v1/jobs/$JOB_ID/tree" | python3 -m json.tool

# 紧凑视图（仅树形摘要）
curl -s "$BASE/api/v1/jobs/$JOB_ID/tree?view=compact" | python3 -m json.tool

# ---------------------------------------------------------------- 5. 下载结果 JSON
curl -s -D - "$BASE/api/v1/jobs/$JOB_ID/result" -o "/tmp/${JOB_ID}.result.json"
python3 -m json.tool "/tmp/${JOB_ID}.result.json" | head -40 || true

# ---------------------------------------------------------------- 6. 列表 / 删除
curl -s "$BASE/api/v1/jobs" | python3 -m json.tool
curl -s -X DELETE "$BASE/api/v1/jobs/$JOB_ID"; echo

# ---------------------------------------------------------------- 7. 恶意包：被后台拒绝
# python3 scripts/make_evil.py traversal
# curl -s -X POST "$BASE/api/v1/jobs" \
#   -F 'file=@examples/evil/evil-traversal.zip;type=application/zip'
# 随后轮询可见 status=failed，error="压缩包被拒绝: 检测到非法/路径穿越条目: …"
