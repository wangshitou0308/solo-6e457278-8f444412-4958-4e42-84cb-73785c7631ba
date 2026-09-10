"""按 Message-ID / In-Reply-To / References 重建会话树。

处理策略：

* 每封邮件得到一个稳定内部 uid；同一 Message-ID 的第一封邮件作为“规范”
  挂链目标，其余重复邮件成为独立根节点并标注问题（内容相同=重复副本，
  内容不同=ID 冲突），绝不静默覆盖。
* 父候选顺序：``In-Reply-To`` → ``References`` 逆序（就近祖先），
  父邮件在包内缺失时沿 References 上溯。
* 引用成环（含自引用）通过 DFS 路径检测，断开成环的那条边，
  环上最先闭环的节点作为该会话根，并在节点问题里说明。
* 所有问题只做标注：原始文件不修改、不删除。
"""

from __future__ import annotations

from typing import Any


def build_threads(records: list[dict[str, Any]]) -> dict[str, Any]:
    """根据解析记录构建线程森林。

    返回 ``{"roots": [...], "messages": [...], "issues_summary": {...}}``。
    节点字典包含 children；messages 为同结构的扁平列表（uid 顺序）。
    """
    nodes: list[dict[str, Any]] = []
    issues_summary = {
        "missing_message_id": 0,
        "duplicate_message_id": 0,
        "missing_parent": 0,
        "reference_cycle": 0,
        "self_reference": 0,
        "undecodable": 0,
        "other": 0,
    }

    # message_id -> 规范节点 uid（重复 id 不覆盖）
    canonical: dict[str, int] = {}

    for rec in records:
        uid = len(nodes)
        node = {
            "uid": uid,
            "source_file": rec["source_file"],
            "raw_sha256": rec["raw_sha256"],
            "message_id": rec["message_id"],
            "in_reply_to": rec["in_reply_to"],
            "references": rec["references"],
            "date": rec["date"],
            "from": rec["from"],
            "to": rec["to"],
            "cc": rec["cc"],
            "subject": rec["subject"],
            "body_text": rec["body_text"],
            "body_html_present": rec.get("body_html_present", False),
            "attachments": rec["attachments"],
            "issues": list(rec["issues"]),
            "children": [],
        }
        nodes.append(node)

        # 粗分解析期问题计数（所有节点都参与）
        for issue in rec["issues"]:
            if any(k in issue for k in ("无法解码", "解码失败", "均无法")):
                issues_summary["undecodable"] += 1
            else:
                issues_summary["other"] += 1

        mid = rec["message_id"]
        if mid is None:
            issues_summary["missing_message_id"] += 1
            node["issues"].append("缺少 Message-ID，无法被其他邮件引用，已作为根节点")
            continue

        if mid in canonical:
            issues_summary["duplicate_message_id"] += 1
            canon_node = nodes[canonical[mid]]
            if canon_node["raw_sha256"] == node["raw_sha256"]:
                detail = "与首次出现的邮件字节完全一致，判定为重复副本"
            else:
                detail = "与首次出现的邮件内容不同，Message-ID 被多封邮件复用"
            node["issues"].append(
                f"Message-ID {mid} 在包内重复：{detail}；"
                "本邮件作为独立根节点展示，不参与引用链挂载"
            )
        else:
            canonical[mid] = uid

    parents: dict[int, int | None] = {}
    resolved: set[int] = set()
    missing_reported: set[tuple[int, str]] = set()

    def candidates(node: dict[str, Any]) -> list[str]:
        """In-Reply-To 优先，其次 References 逆序（就近的父候选在前）。"""
        result: list[str] = []
        if node["in_reply_to"]:
            result.append(node["in_reply_to"])
        for ref in reversed(node["references"]):
            if ref not in result:
                result.append(ref)
        return result

    def resolve(uid: int, stack: set[int]) -> None:
        if uid in resolved:
            return
        node = nodes[uid]
        stack.add(uid)
        try:
            parent_uid: int | None = None
            own_mid = node["message_id"]

            # 重复 Message-ID 的节点强制为根，不参与挂载
            is_duplicate = own_mid is not None and canonical.get(own_mid) != uid
            if not is_duplicate:
                for ref in candidates(node):
                    if own_mid is not None and ref == own_mid:
                        issues_summary["self_reference"] += 1
                        node["issues"].append(
                            f"邮件引用了自身 ({ref})，自引用边已断开"
                        )
                        continue

                    target_uid = canonical.get(ref)
                    if target_uid is None:
                        key = (uid, ref)
                        if key not in missing_reported:
                            missing_reported.add(key)
                            issues_summary["missing_parent"] += 1
                            node["issues"].append(
                                f"引用的父邮件 {ref} 在压缩包内缺失"
                            )
                        continue

                    if target_uid in stack:
                        issues_summary["reference_cycle"] += 1
                        node["issues"].append(
                            f"引用链成环：父邮件 {ref} 已位于当前引用链上，"
                            "该引用边已断开以保证会话树无环"
                        )
                        continue

                    resolve(target_uid, stack)
                    parent_uid = target_uid
                    break
            parents[uid] = parent_uid
            resolved.add(uid)
        finally:
            stack.discard(uid)

    # 固定插入顺序解析，保证结果确定性
    for uid in range(len(nodes)):
        resolve(uid, set())

    roots: list[dict[str, Any]] = []
    for uid, node in enumerate(nodes):
        parent_uid = parents.get(uid)
        if parent_uid is None:
            roots.append(node)
        else:
            nodes[parent_uid]["children"].append(node)

    def sort_tree(level: list[dict[str, Any]]) -> None:
        level.sort(key=lambda n: (n["date"] or "", n["source_file"]))
        for node in level:
            sort_tree(node["children"])

    sort_tree(roots)

    return {
        "roots": roots,
        "messages": nodes,
        "issues_summary": issues_summary,
    }
