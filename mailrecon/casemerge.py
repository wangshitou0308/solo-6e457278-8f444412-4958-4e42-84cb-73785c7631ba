"""案件级多包合并：把多个已完成作业的会话森林合并为一棵跨包森林。

合并规则：

* 字节完全相同（SHA-256 相同）的邮件**合并为一个节点**，全部来源作业
  与文件名保留在节点的 ``sources`` 列表中（含原作业内的 uid）；
* Message-ID 相同但 SHA-256 不同的邮件**并列保留**为独立节点，并在
  ``merge_info`` 中标注冲突；解析引用时遇到这种歧义**绝不猜测父节点**，
  该引用边被跳过（可继续沿 References 上溯无歧义的祖先）；
* 父邮件在原作业包内缺失、但在案件其他作业中找到时，跨包补齐
  missing_parent，补链依据记录在 ``merge_info.relinked_parent``；
* 引用成环/自引用与单作业一致：断开成环边并标注，保证输出严格无环。

节点原始问题（``issues``）保留首个来源作业中的原文，合并期产生的
说明全部写入 ``merge_info.notes``，两者互不覆盖，便于审计。

所有遍历均为显式栈迭代，不使用 Python 递归，跨包拼出的万级深链
同样可建。
"""

from __future__ import annotations

from typing import Any

# 解析状态
_UNRESOLVED = 0
_RESOLVING = 1
_RESOLVED = 2

# canonical 表中的歧义标记：Message-ID 对应多封内容不同的邮件
_AMBIGUOUS = -1


def _empty_identity() -> dict[str, Any]:
    return {
        "from": [],
        "sender": [],
        "reply_to": [],
        "return_path": [],
        "message_id": {"present": False, "headers": []},
        "dkim": [],
        "anomalies": [],
    }

# 从来源节点原样拷贝的内容字段（同一 SHA-256 的字节相同，解析结果一致）
_CONTENT_FIELDS = (
    "message_id", "raw_sha256", "in_reply_to", "references", "date",
    "from", "to", "cc", "subject", "body_text", "body_html_present",
    "attachments", "received", "identity",
)


def build_case_forest(job_results: list[dict[str, Any]]) -> dict[str, Any]:
    """合并多个作业的结果森林。

    ``job_results`` 中每项为
    ``{"job_id": ..., "original_filename": ..., "threads": [...]}``
    （``threads`` 即作业结果 JSON 中的森林，节点带原 ``uid``）。
    调用方保证 job_id 不重复。

    返回 ``{"roots": [...], "nodes": [...], "stats": {...}}``：
    roots 为合并后的森林，nodes 为扁平节点列表（uid 顺序），
    stats 含重复/冲突/补链统计与各作业贡献。
    """
    nodes: list[dict[str, Any]] = []
    by_sha: dict[str, int] = {}
    mid_to_uids: dict[str, list[int]] = {}
    contributions: dict[str, dict[str, Any]] = {}
    job_mids: dict[str, set[str]] = {}

    # ---- 1. 归并：SHA-256 相同 -> 同一个节点 --------------------------
    for job in job_results:
        job_id = job["job_id"]
        contrib = {
            "job_id": job_id,
            "original_filename": job.get("original_filename"),
            "emails": 0,
            "unique_nodes": 0,
            "duplicates_merged": 0,
            "conflict_nodes": 0,
            "relinked_nodes": 0,
            "root_nodes": 0,
        }
        contributions[job_id] = contrib
        mids_in_job = job_mids.setdefault(job_id, set())

        for src in _flatten_forest(job.get("threads", [])):
            contrib["emails"] += 1
            mid = src["message_id"]
            if mid is not None:
                mids_in_job.add(mid)

            existing = by_sha.get(src["raw_sha256"])
            if existing is not None:
                nodes[existing]["sources"].append(_source_entry(job_id, src))
                contrib["duplicates_merged"] += 1
                continue

            uid = len(nodes)
            nodes.append(_new_merged_node(uid, job_id, src))
            by_sha[src["raw_sha256"]] = uid
            if mid is not None:
                mid_to_uids.setdefault(mid, []).append(uid)
            contrib["unique_nodes"] += 1

    # ---- 2. 重复/冲突标注 ---------------------------------------------
    for node in nodes:
        info = node["merge_info"]
        info["source_count"] = len(node["sources"])
        info["duplicates_merged"] = info["source_count"] - 1
        if info["duplicates_merged"]:
            info["notes"].append(
                f"同一邮件（SHA-256 相同）在 {info['source_count']} 个来源中"
                "重复出现，已合并为一个节点；全部来源见 sources"
            )

    canonical: dict[str, int] = {}
    conflict_groups = 0
    conflict_node_count = 0
    for mid, uids in mid_to_uids.items():
        if len(uids) == 1:
            canonical[mid] = uids[0]
            continue
        canonical[mid] = _AMBIGUOUS
        conflict_groups += 1
        conflict_node_count += len(uids)
        for uid in uids:
            info = nodes[uid]["merge_info"]
            info["conflict"] = True
            info["conflict_with"] = sorted(u for u in uids if u != uid)
            info["notes"].append(
                f"Message-ID {mid} 对应 {len(uids)} 封内容不同的邮件"
                "（SHA-256 不同），已并列保留为独立节点；"
                "引用该 ID 的邮件不会猜测父节点"
            )
            for src_job in {s["job_id"] for s in nodes[uid]["sources"]}:
                contributions[src_job]["conflict_nodes"] += 1

    # ---- 3. 跨包父引用解析（歧义不猜测） ------------------------------
    counters = {
        "missing_references": 0,
        "ambiguous_references": 0,
        "reference_cycles": 0,
        "self_references": 0,
    }
    parents, parent_refs = _resolve_parents(nodes, canonical, counters)

    # ---- 4. 补链依据：父邮件在来源作业中缺失、跨包找到 -----------------
    relinked = 0
    for uid, parent in enumerate(parents):
        if parent is None:
            continue
        node = nodes[uid]
        ref = parent_refs[uid]
        via = "in_reply_to" if ref == node["in_reply_to"] else "references"
        source_jobs: list[str] = []
        for s in node["sources"]:
            if s["job_id"] not in source_jobs:
                source_jobs.append(s["job_id"])
        resolved_for = [
            j for j in source_jobs if ref not in job_mids.get(j, ())
        ]
        if resolved_for:
            relinked += 1
            note = (
                f"父邮件 {ref} 在来源作业 {', '.join(resolved_for)} 中缺失，"
                f"跨包合并时经 {via} 从案件内其他作业找到并补齐"
            )
            node["merge_info"]["relinked_parent"] = {
                "message_id": ref,
                "via": via,
                "resolved_for_jobs": resolved_for,
                "note": note,
            }
            node["merge_info"]["notes"].append(note)
            for j in resolved_for:
                contributions[j]["relinked_nodes"] += 1

    # ---- 5. 组森林、排序、汇总 -----------------------------------------
    roots: list[dict[str, Any]] = []
    for uid, node in enumerate(nodes):
        parent = parents[uid]
        if parent is None:
            roots.append(node)
        else:
            nodes[parent]["children"].append(node)
    _sort_forest(roots)
    for root in roots:
        contributions[root["sources"][0]["job_id"]]["root_nodes"] += 1

    source_emails = sum(c["emails"] for c in contributions.values())
    stats = {
        "job_count": len(job_results),
        "source_emails": source_emails,
        "merged_nodes": len(nodes),
        "duplicates_merged": source_emails - len(nodes),
        "conflict_groups": conflict_groups,
        "conflict_nodes": conflict_node_count,
        "relinked_nodes": relinked,
        "ambiguous_references": counters["ambiguous_references"],
        "missing_references": counters["missing_references"],
        "reference_cycles": counters["reference_cycles"],
        "self_references": counters["self_references"],
        "thread_count": len(roots),
        "job_contributions": [
            contributions[job["job_id"]] for job in job_results
        ],
    }
    return {"roots": roots, "nodes": nodes, "stats": stats}


# ---------------------------------------------------------------- 节点构造

def _source_entry(job_id: str, src: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "source_file": src["source_file"],
        "original_uid": src["uid"],
    }


def _new_merged_node(
    uid: int, job_id: str, src: dict[str, Any]
) -> dict[str, Any]:
    node = {field: src[field] for field in _CONTENT_FIELDS if field in src}
    # 旧版本作业结果没有 received/identity 字段，按空结构处理（不猜测）
    node.setdefault("received", [])
    node.setdefault("identity", _empty_identity())
    node["uid"] = uid
    node["sources"] = [_source_entry(job_id, src)]
    # 原始问题保留来源作业中的原文（同一 SHA-256 解析结果一致，
    # 但各作业的线程级问题可能不同，这里取首个来源，合并期说明
    # 一律写入 merge_info.notes）
    node["issues"] = list(src["issues"])
    node["merge_info"] = {
        "source_count": 1,
        "duplicates_merged": 0,
        "conflict": False,
        "conflict_with": [],
        "relinked_parent": None,
        "notes": [],
    }
    node["children"] = []
    return node


def _flatten_forest(roots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """迭代式把作业结果森林摊平，并按原 uid 恢复作业内顺序。"""
    out: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = list(reversed(roots))
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(reversed(node["children"]))
    out.sort(key=lambda n: n["uid"])
    return out


# ---------------------------------------------------------------- 父引用解析

def _candidate_refs(node: dict[str, Any]) -> list[str]:
    """In-Reply-To 优先，其次 References 逆序（就近的父候选在前）。"""
    result: list[str] = []
    if node["in_reply_to"]:
        result.append(node["in_reply_to"])
    for ref in reversed(node["references"]):
        if ref not in result:
            result.append(ref)
    return result


def _resolve_parents(
    nodes: list[dict[str, Any]],
    canonical: dict[str, int],
    counters: dict[str, int],
) -> tuple[list[int | None], list[str | None]]:
    """迭代式 DFS 解析每个节点的父节点（与 threads 同源的栈帧算法）。

    与单作业版的差异：``canonical`` 中可能取到 ``_AMBIGUOUS``——
    Message-ID 对应多封内容不同的邮件。此时**不猜测父节点**，记录
    歧义说明后继续尝试下一个候选（沿 References 上溯无歧义祖先）。

    返回 ``(parents, parent_refs)``：父 uid 与命中的引用 ID。
    """
    n = len(nodes)
    parents: list[int | None] = [None] * n
    parent_refs: list[str | None] = [None] * n
    state = [_UNRESOLVED] * n
    stack: list[dict[str, Any]] = []
    on_path: set[int] = set()

    def make_frame(uid: int) -> dict[str, Any]:
        return {
            "uid": uid,
            "refs": _candidate_refs(nodes[uid]),
            "index": 0,
            "parent": None,            # 已确定的父 uid
            "parent_ref": None,        # 命中父节点时使用的引用 ID
            "reported": set(),         # 本帧已记录过说明的 ref
            "waiting_ref": None,       # 正在等待下探解析的候选 ref
            "waiting_for": None,       # 正在等待的子帧 uid
        }

    def finalize(frame: dict[str, Any]) -> None:
        uid = frame["uid"]
        parents[uid] = frame["parent"]
        parent_refs[uid] = frame["parent_ref"]
        state[uid] = _RESOLVED
        on_path.discard(uid)
        stack.pop()
        if stack:
            caller = stack[-1]
            # 仅当调用帧正在直接等待“本帧”时才回传父关系
            if caller["waiting_for"] == uid:
                caller["parent"] = uid
                caller["parent_ref"] = caller["waiting_ref"]
                caller["waiting_for"] = None
                caller["waiting_ref"] = None
                caller["index"] = len(caller["refs"])

    # 固定插入顺序驱动，保证结果确定性
    for start in range(n):
        if state[start] == _RESOLVED:
            continue
        state[start] = _RESOLVING
        on_path.add(start)
        stack.append(make_frame(start))

        while stack:
            frame = stack[-1]
            uid = frame["uid"]
            node = nodes[uid]
            notes = node["merge_info"]["notes"]
            own_mid = node["message_id"]

            if frame["index"] < len(frame["refs"]):
                ref = frame["refs"][frame["index"]]
                frame["index"] += 1

                if own_mid is not None and ref == own_mid:
                    counters["self_references"] += 1
                    notes.append(f"邮件引用了自身 ({ref})，自引用边已断开")
                    continue

                target = canonical.get(ref)
                if target is None:
                    if ref not in frame["reported"]:
                        frame["reported"].add(ref)
                        counters["missing_references"] += 1
                        notes.append(
                            f"引用的父邮件 {ref} 在案件全部来源作业中仍缺失"
                        )
                    continue

                if target == _AMBIGUOUS:
                    # 同一 Message-ID 多封不同邮件：不猜测父节点
                    if ref not in frame["reported"]:
                        frame["reported"].add(ref)
                        counters["ambiguous_references"] += 1
                        notes.append(
                            f"引用的父邮件 {ref} 存在 Message-ID 冲突"
                            "（同一 ID 对应多封内容不同的邮件），"
                            "为避免猜测父节点，该引用边未使用"
                        )
                    continue

                target_state = state[target]
                if target_state == _RESOLVING and target in on_path:
                    counters["reference_cycles"] += 1
                    notes.append(
                        f"引用链成环：父邮件 {ref} 已位于当前引用链上，"
                        "该引用边已断开以保证会话树无环"
                    )
                    continue

                if target_state == _UNRESOLVED:
                    frame["waiting_ref"] = ref
                    frame["waiting_for"] = target
                    on_path.add(target)
                    state[target] = _RESOLVING
                    stack.append(make_frame(target))
                    continue

                # target 已解析完成：采用它并结束候选尝试
                frame["parent"] = target
                frame["parent_ref"] = ref
                frame["index"] = len(frame["refs"])
            else:
                finalize(frame)

    return parents, parent_refs


def _sort_forest(roots: list[dict[str, Any]]) -> None:
    """迭代式按 (date, 首要来源文件, uid) 排序整棵森林。"""
    levels: list[list[dict[str, Any]]] = [roots]
    while levels:
        level = levels.pop()
        level.sort(
            key=lambda n: (
                n["date"] or "",
                n["sources"][0]["source_file"],
                n["uid"],
            )
        )
        for node in level:
            if node["children"]:
                levels.append(node["children"])
