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

父节点解析与排序全部使用显式栈的迭代算法，不使用 Python 递归，
因此会话链长度不受 ``sys.getrecursionlimit()`` 限制（万级深度同样可建）。
"""

from __future__ import annotations

from typing import Any

# 解析状态
_UNRESOLVED = 0
_RESOLVING = 1
_RESOLVED = 2


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
            "quote_structure": rec.get("quote_structure"),
            "attachments": rec["attachments"],
            "received": rec.get("received", []),
            "identity": rec.get("identity") or _empty_identity(),
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

    parents = _resolve_parents(nodes, canonical, issues_summary)

    roots: list[dict[str, Any]] = []
    for uid, node in enumerate(nodes):
        parent_uid = parents[uid]
        if parent_uid is None:
            roots.append(node)
        else:
            nodes[parent_uid]["children"].append(node)

    _sort_forest(roots)

    return {
        "roots": roots,
        "messages": nodes,
        "issues_summary": issues_summary,
    }


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
    issues_summary: dict[str, int],
) -> list[int | None]:
    """迭代式 DFS 解析每个节点的父节点，行为等价于原递归实现。

    维护两个独立结构：

    * ``stack``：等待中的调用帧（父帧在等待候选子节点定稿）；
    * ``on_path``：当前递归意义上的“引用路径”。下探时把目标压入路径，
      定稿时弹出——父帧在等待子帧期间并不占用路径，与原递归
      ``resolve(target)`` 返回后再判断的语义一致。

    栈帧字段：``uid``、``refs``、``index``、``parent``、``reported``、
    ``waiting_ref``。
    """
    n = len(nodes)
    parents: list[int | None] = [None] * n
    state = [_UNRESOLVED] * n
    stack: list[dict[str, Any]] = []
    on_path: set[int] = set()

    def make_frame(uid: int) -> dict[str, Any]:
        own_mid = nodes[uid]["message_id"]
        is_duplicate = own_mid is not None and canonical.get(own_mid) != uid
        refs = [] if is_duplicate else _candidate_refs(nodes[uid])
        return {
            "uid": uid,
            "refs": refs,
            "index": 0,
            "parent": None,            # 已确定的父 uid
            "reported": set(),         # 本帧已记录过缺失问题的 ref
            "waiting_ref": None,       # 正在等待下探解析的候选 ref
            "waiting_for": None,       # 正在等待的子帧 uid
        }

    def finalize(frame: dict[str, Any]) -> None:
        """定稿一个帧：写父关系、标记完成、离开引用路径并弹栈。"""
        uid = frame["uid"]
        parents[uid] = frame["parent"]
        state[uid] = _RESOLVED
        on_path.discard(uid)
        stack.pop()
        if stack:
            caller = stack[-1]
            # 仅当调用帧正在直接等待“本帧”时才回传父关系，并让其结束
            # 候选尝试（等价原递归 resolve() 返回后的 break）
            if caller["waiting_for"] == uid:
                caller["waiting_for"] = None
                caller["waiting_ref"] = None
                caller["parent"] = uid
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
            own_mid = node["message_id"]

            # 还有未尝试的候选引用
            if frame["index"] < len(frame["refs"]):
                ref = frame["refs"][frame["index"]]
                frame["index"] += 1

                if own_mid is not None and ref == own_mid:
                    issues_summary["self_reference"] += 1
                    node["issues"].append(
                        f"邮件引用了自身 ({ref})，自引用边已断开"
                    )
                    continue

                target_uid = canonical.get(ref)
                if target_uid is None:
                    if ref not in frame["reported"]:
                        frame["reported"].add(ref)
                        issues_summary["missing_parent"] += 1
                        node["issues"].append(
                            f"引用的父邮件 {ref} 在压缩包内缺失"
                        )
                    continue

                target_state = state[target_uid]
                if target_state == _RESOLVING and target_uid in on_path:
                    # target 在当前引用路径上：成环，断开这条边
                    issues_summary["reference_cycle"] += 1
                    node["issues"].append(
                        f"引用链成环：父邮件 {ref} 已位于当前引用链上，"
                        "该引用边已断开以保证会话树无环"
                    )
                    continue

                if target_state == _UNRESOLVED:
                    # 等价于递归调用 resolve(target)：调用帧等待，
                    # 目标进入引用路径并压栈
                    frame["waiting_ref"] = ref
                    frame["waiting_for"] = target_uid
                    on_path.add(target_uid)
                    state[target_uid] = _RESOLVING
                    stack.append(make_frame(target_uid))
                    continue

                # target 已解析完成：采用它并结束候选尝试
                # （等价原递归 resolve() 返回后的 break）
                frame["parent"] = target_uid
                frame["index"] = len(frame["refs"])
            else:
                # 候选耗尽：定稿当前节点
                finalize(frame)

    return parents


def _sort_forest(roots: list[dict[str, Any]]) -> None:
    """迭代式按 (date, source_file) 排序整棵森林。"""
    levels: list[list[dict[str, Any]]] = [roots]
    while levels:
        level = levels.pop()
        level.sort(key=lambda n: (n["date"] or "", n["source_file"]))
        for node in level:
            if node["children"]:
                levels.append(node["children"])
