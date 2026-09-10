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

# ================================================================ 案件合并（多包）
# 前置：python3 scripts/make_case_sample.py 生成 examples/case-pack-1.zip
# 与 examples/case-pack-2.zip（跨包补链 / 重复合并 / ID 冲突三类场景）

# ---------------------------------------------------------------- 8. 上传两个包并等待完成
JOB1=$(curl -s -X POST "$BASE/api/v1/jobs" \
  -F 'file=@examples/case-pack-1.zip;type=application/zip' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
JOB2=$(curl -s -X POST "$BASE/api/v1/jobs" \
  -F 'file=@examples/case-pack-2.zip;type=application/zip' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
for J in "$JOB1" "$JOB2"; do
  while :; do
    S=$(curl -s "$BASE/api/v1/jobs/$J" | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])")
    [ "$S" = completed ] && break
    [ "$S" = failed ] && { echo "作业失败: $J"; exit 1; }
    sleep 0.5
  done
done

# ---------------------------------------------------------------- 9. 创建案件并轮询
CASE_JSON=$(curl -s -X POST "$BASE/api/v1/cases" \
  -H 'Content-Type: application/json' \
  -d "{\"name\": \"合同谈判合并\", \"job_ids\": [\"$JOB1\", \"$JOB2\"]}")
echo "$CASE_JSON"
CASE_ID=$(python3 -c "import json,sys; print(json.loads(sys.stdin.read())['id'])" <<<"$CASE_JSON")
while :; do
  CASE=$(curl -s "$BASE/api/v1/cases/$CASE_ID")
  STATUS=$(python3 -c "import json,sys; print(json.load(sys.stdin)['status'])" <<<"$CASE")
  echo "$CASE" | python3 -c "import json,sys; j=json.load(sys.stdin); print(j['status'], j['progress'], j['phase'])"
  if [ "$STATUS" = completed ] || [ "$STATUS" = failed ]; then break; fi
  sleep 0.5
done
# 各作业贡献 / 重复 / 冲突 / 补链统计
echo "$CASE" | python3 -m json.tool

# ---------------------------------------------------------------- 10. 读取合并树
# 完整树（sources 保留全部来源；merge_info 记录重复/冲突/补链依据）
curl -s "$BASE/api/v1/cases/$CASE_ID/tree" | python3 -m json.tool
# 紧凑视图
curl -s "$BASE/api/v1/cases/$CASE_ID/tree?view=compact" | python3 -m json.tool

# ---------------------------------------------------------------- 11. 下载案件结果 JSON
curl -s -D - "$BASE/api/v1/cases/$CASE_ID/result" -o "/tmp/${CASE_ID}.case-result.json"
python3 -m json.tool "/tmp/${CASE_ID}.case-result.json" | head -40 || true

# ---------------------------------------------------------------- 12. 删除源作业不影响案件结果
curl -s -X DELETE "$BASE/api/v1/jobs/$JOB1"; echo
curl -s -X DELETE "$BASE/api/v1/jobs/$JOB2"; echo
curl -s "$BASE/api/v1/cases/$CASE_ID/tree" | python3 -c \
  "import json,sys; print('案件结果仍可读，根节点数:', len(json.load(sys.stdin)['threads']))"

# ---------------------------------------------------------------- 13. 创建案件的拒绝场景
# 源作业不存在：404 job_not_found
curl -s -X POST "$BASE/api/v1/cases" -H 'Content-Type: application/json' \
  -d '{"job_ids": ["00000000-0000-0000-0000-000000000000"]}'; echo
# 同一作业重复提交：409 duplicate_job（先重新上传一个作业）
# J=$(curl -s -X POST "$BASE/api/v1/jobs" -F 'file=@examples/case-pack-1.zip;type=application/zip' | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
# curl -s -X POST "$BASE/api/v1/cases" -H 'Content-Type: application/json' \
#   -d "{\"job_ids\": [\"$J\", \"$J\"]}"
