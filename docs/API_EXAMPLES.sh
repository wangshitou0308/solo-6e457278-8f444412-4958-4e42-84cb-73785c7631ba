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

# ================================================================ 时序核验分析
# 前置：python3 scripts/make_timing_sample.py 生成
#   examples/timing-mails.zip      正常多跳 / 时钟偏差 / 传输耗时 / 跳逆序 / 回复早于父邮件
#   examples/timing-chain-{a,b}.zip 同一 Message-ID 传输链不同（案件级并列展示）

# ---------------------------------------------------------------- 14. 上传时序示例包并创建分析
TIMING_JOB=$(curl -s -X POST "$BASE/api/v1/jobs" \
  -F 'file=@examples/timing-mails.zip;type=application/zip' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
while :; do
  S=$(curl -s "$BASE/api/v1/jobs/$TIMING_JOB" | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])")
  [ "$S" = completed ] && break
  [ "$S" = failed ] && { echo "作业失败: $TIMING_JOB"; exit 1; }
  sleep 0.5
done
# 作业树节点带 received 跳点（原顺序保留，UTC + 原始时区）
curl -s "$BASE/api/v1/jobs/$TIMING_JOB/tree" | python3 -c \
  "import json,sys; n=json.load(sys.stdin)['threads'][0]; print(json.dumps(n['received'], ensure_ascii=False, indent=2))"

# 创建分析：自定义时钟偏差与传输耗时阈值（缺省用默认值 120/300 秒）
AN_JSON=$(curl -s -X POST "$BASE/api/v1/analyses" \
  -H 'Content-Type: application/json' \
  -d "{\"target_type\": \"job\", \"target_id\": \"$TIMING_JOB\", \"thresholds\": {\"clock_skew_seconds\": 120, \"max_transit_seconds\": 300}}")
echo "$AN_JSON"
AN_ID=$(python3 -c "import json,sys; print(json.loads(sys.stdin.read())['id'])" <<<"$AN_JSON")

# ---------------------------------------------------------------- 15. 轮询分析进度
while :; do
  AN=$(curl -s "$BASE/api/v1/analyses/$AN_ID")
  STATUS=$(python3 -c "import json,sys; print(json.load(sys.stdin)['status'])" <<<"$AN")
  echo "$AN" | python3 -c "import json,sys; j=json.load(sys.stdin); print(j['status'], j['progress'], j['phase'])"
  if [ "$STATUS" = completed ] || [ "$STATUS" = failed ]; then break; fi
  sleep 0.5
done
echo "$AN" | python3 -m json.tool   # stats.findings_by_type 汇总各类结论数

# ---------------------------------------------------------------- 16. 读取时间线（可筛选）
# 完整时间线（条目按定位时间升序，结论内联在 entries[].findings）
curl -s "$BASE/api/v1/analyses/$AN_ID/timeline" | python3 -m json.tool
# 按异常类型筛选（可选：client_clock_skew / abnormal_transit /
#   hop_time_inversion / reply_before_parent / chain_mismatch）
curl -s "$BASE/api/v1/analyses/$AN_ID/timeline?type=client_clock_skew" | python3 -m json.tool
# 按时间区间筛选（边界必须带时区，缺时区返回 400）
curl -s "$BASE/api/v1/analyses/$AN_ID/timeline?from=2026-09-01T09:00:00%2B00:00&to=2026-09-02T00:00:00%2B00:00" \
  | python3 -m json.tool

# ---------------------------------------------------------------- 17. 下载分析结果 JSON
curl -s -D - "$BASE/api/v1/analyses/$AN_ID/result" -o "/tmp/${AN_ID}.analysis-result.json"
python3 -m json.tool "/tmp/${AN_ID}.analysis-result.json" | head -40 || true

# ---------------------------------------------------------------- 18. 案件级：同一 Message-ID 传输链并列展示
CA=$(curl -s -X POST "$BASE/api/v1/jobs" -F 'file=@examples/timing-chain-a.zip;type=application/zip' | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
CB=$(curl -s -X POST "$BASE/api/v1/jobs" -F 'file=@examples/timing-chain-b.zip;type=application/zip' | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
for J in "$CA" "$CB"; do
  while :; do
    S=$(curl -s "$BASE/api/v1/jobs/$J" | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])")
    [ "$S" = completed ] && break
    [ "$S" = failed ] && { echo "作业失败: $J"; exit 1; }
    sleep 0.5
  done
done
TCASE=$(curl -s -X POST "$BASE/api/v1/cases" -H 'Content-Type: application/json' \
  -d "{\"name\": \"传输链比对\", \"job_ids\": [\"$CA\", \"$CB\"]}" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
while :; do
  S=$(curl -s "$BASE/api/v1/cases/$TCASE" | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])")
  [ "$S" = completed ] && break
  sleep 0.5
done
TAN=$(curl -s -X POST "$BASE/api/v1/analyses" -H 'Content-Type: application/json' \
  -d "{\"target_type\": \"case\", \"target_id\": \"$TCASE\"}" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
while :; do
  S=$(curl -s "$BASE/api/v1/analyses/$TAN" | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])")
  [ "$S" = completed ] && break
  sleep 0.5
done
# 两个来源的传输链在 evidence.variants 中并列展示
curl -s "$BASE/api/v1/analyses/$TAN/timeline?type=chain_mismatch" | python3 -m json.tool

# ---------------------------------------------------------------- 19. 删除源作业不影响已完成的分析
curl -s -X DELETE "$BASE/api/v1/jobs/$CA" > /dev/null
curl -s -X DELETE "$BASE/api/v1/jobs/$CB" > /dev/null
curl -s "$BASE/api/v1/analyses/$TAN" | python3 -c \
  "import json,sys; j=json.load(sys.stdin); print('分析结果仍可读:', j['status'], '结论数:', j['finding_count'])"
