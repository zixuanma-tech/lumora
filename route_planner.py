"""第二算子：兴趣评分、展厅打包、拓扑搜索、裁剪与顺路国宝补偿。

数据源、兴趣解析器和向量服务由调用方注入；本模块不持有数据库连接。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from math import fsum
from typing import Any

from route_graph import SpatialGraph, WalkLeg
from route_models import (
    InterestParser, InterestProfile, MuseumDataSource, MuseumSnapshot,
    PlannerConfig, VectorService, _vector,
)
from route_scoring import ExhibitScore, GalleryBundle, aggregate_galleries, score_exhibits
from session_state import RouteStop, SessionState

EPS = 1e-9


@dataclass(frozen=True, slots=True)
class _Candidate:
    sequence: tuple[int, ...]
    legs: tuple[WalkLeg, ...]
    retained: tuple[tuple[ExhibitScore, ...], ...]
    dropped: tuple[ExhibitScore, ...]
    nominal_min: float
    total_min: float
    gain: float
    floor_changes: int


def _dwell(score: ExhibitScore, pace: str) -> float:
    return score.exhibit.dwell_time_map[pace] * score.exhibit.crowd_factor


def _gain(scores: Sequence[ExhibitScore], config: PlannerConfig) -> float:
    return fsum(config.fatigue_decay ** rank * item.total_score for rank, item in enumerate(scores))


class _Optimizer:
    def __init__(self, snapshot, bundles, graph, start, exit_node, pace, budget, config):
        self.snapshot, self.bundles, self.graph = snapshot, bundles, graph
        self.start, self.exit = start, exit_node
        self.pace, self.budget, self.config = pace, budget, config
        self.gallery_nodes = {g.node_id: g for g in snapshot.galleries}
        self.nodes = tuple(b.gallery.node_id for b in bundles)
        relevant = tuple(dict.fromkeys((start, *self.nodes, exit_node)))
        self.paths = {(a, b): graph.shortest(a, b) for a in relevant for b in relevant}

    def endpoint_pass(self, sequence: tuple[int, ...]) -> float:
        """起终点按空间出现位置计费，不能因该厅在稍后停留就免除起点穿行。"""
        if not sequence:
            endpoints = tuple(dict.fromkeys((self.start, self.exit)))
        else:
            endpoints = (() if self.start == self.nodes[sequence[0]] else (self.start,))
            endpoints += (() if self.exit == self.nodes[sequence[-1]] else (self.exit,))
        return fsum(self.gallery_nodes[n].base_pass_min for n in endpoints if n in self.gallery_nodes)

    def _evaluate(self, sequence: tuple[int, ...]) -> _Candidate | None:
        waypoints = (self.start, *(self.nodes[i] for i in sequence), self.exit)
        legs = tuple(self.paths[(a, b)] for a, b in zip(waypoints, waypoints[1:]))
        if any(leg is None for leg in legs):
            return None
        changes = sum(leg.floor_changes for leg in legs)
        penalty = max(0, changes - self.config.floor_change_allowance) * self.config.floor_change_penalty_min
        nominal = (fsum(leg.total_min for leg in legs) + self.endpoint_pass(sequence) + penalty
                   + fsum(self.bundles[i].stay_time_min for i in sequence))
        if nominal > self.budget + self.config.trim_overrun_min + EPS:
            return None
        retained = [list(self.bundles[i].exhibits) for i in sequence]
        total, dropped = nominal, []
        # 只从各厅得分排序的尾部删除，再在所有尾部中选最低分。此顺序仅由
        # 展厅集合决定，与访问顺序无关，是精确 DP 支配性成立的依据。
        while total > self.budget + EPS:
            tails = [(items[-1].total_score, items[-1].exhibit.exhibit_id, pos)
                     for pos, items in enumerate(retained) if items]
            if not tails:
                return None
            _, _, pos = min(tails)
            item = retained[pos].pop()
            dropped.append(item)
            total -= _dwell(item, self.pace)
        gain = fsum(_gain(items, self.config) for items in retained)
        return _Candidate(sequence, legs, tuple(tuple(items) for items in retained),
                          tuple(dropped), nominal, max(0.0, total), gain, changes)

    @staticmethod
    def better(candidate: _Candidate | None, incumbent: _Candidate | None) -> bool:
        if candidate is None:
            return False
        if incumbent is None:
            return True
        if abs(candidate.gain - incumbent.gain) > EPS:
            return candidate.gain > incumbent.gain
        if abs(candidate.total_min - incumbent.total_min) > EPS:
            return candidate.total_min < incumbent.total_min
        return (len(candidate.dropped), candidate.sequence) < (len(incumbent.dropped), incumbent.sequence)

    def exact(self) -> _Candidate | None:
        """Held–Karp DP，精确到既定 Top-K、最短通路和尾部剔除规则。

        同一 (展厅集合、末厅、已用免费跨层次数) 只保留最短的名义耗时。
        集合相同时满配收益和删除次序相同，耗时更短的路径不会多删展品；
        因而删后收益也不会更低。无需枚举所有展厅排列或任意展品子集。
        """
        n, allowance = len(self.bundles), self.config.floor_change_allowance
        # key -> (名义耗时（暂不含出口腿）、访问序列)
        states: dict[tuple[int, int, int], tuple[float, tuple[int, ...]]] = {}
        best = self._evaluate(())
        for i, bundle in enumerate(self.bundles):
            leg = self.paths[(self.start, self.nodes[i])]
            if leg is None:
                continue
            time = leg.total_min + bundle.stay_time_min + max(0, leg.floor_changes - allowance) * self.config.floor_change_penalty_min
            if self.start in self.gallery_nodes and self.start != self.nodes[i]:
                time += self.gallery_nodes[self.start].base_pass_min
            if time <= self.budget + self.config.trim_overrun_min + EPS:
                states[(1 << i, i, min(allowance, leg.floor_changes))] = (time, (i,))
        # mask 严格增加，按 mask 数值顺序访问即可完成拓扑 DP。
        by_mask: dict[int, list[tuple[int, int]]] = {}
        for mask, last, used in states:
            by_mask.setdefault(mask, []).append((last, used))
        for mask in range(1, 1 << n):
            for last, used in tuple(by_mask.get(mask, ())):
                time, sequence = states[(mask, last, used)]
                candidate = self._evaluate(sequence)
                if self.better(candidate, best):
                    best = candidate
                for nxt, bundle in enumerate(self.bundles):
                    if mask & (1 << nxt):
                        continue
                    leg = self.paths[(self.nodes[last], self.nodes[nxt])]
                    if leg is None:
                        continue
                    next_time = time + leg.total_min + bundle.stay_time_min
                    next_time += max(0, used + leg.floor_changes - allowance) * self.config.floor_change_penalty_min
                    if next_time > self.budget + self.config.trim_overrun_min + EPS:
                        continue
                    key = (mask | (1 << nxt), nxt, min(allowance, used + leg.floor_changes))
                    value = (next_time, sequence + (nxt,))
                    old = states.get(key)
                    if old is None:
                        states[key] = value
                        by_mask.setdefault(key[0], []).append(key[1:])
                    elif value < old:
                        states[key] = value
        return best

    def heuristic(self) -> _Candidate | None:
        """按增益/新增时间插入，随后进行有向图可行性检查的 2-opt。"""
        best = self._evaluate(())
        sequence: tuple[int, ...] = ()
        unused = set(range(len(self.bundles)))
        # 空序列若不可达，单厅序列仍可能可行（例如指定起厅的停留计费）。
        while unused:
            choices = []
            for nxt in sorted(unused, key=lambda i: (-self.bundles[i].density, self.nodes[i])):
                for pos in range(len(sequence) + 1):
                    order = sequence[:pos] + (nxt,) + sequence[pos:]
                    candidate = self._evaluate(order)
                    if candidate is None or best is not None and candidate.gain <= best.gain + EPS:
                        continue
                    base_gain, base_time = (best.gain, best.total_min) if best else (0.0, 0.0)
                    ratio = (candidate.gain - base_gain) / max(EPS, candidate.total_min - base_time)
                    choices.append((ratio, candidate.gain, -candidate.total_min, order, candidate))
            if not choices:
                break
            choice = max(choices, key=lambda item: item[:4])[-1]
            sequence, best = choice.sequence, choice
            unused.difference_update(sequence)
        # 反转片段后重算每条有向通路；不沿用无向图的边替换公式。
        improved = True
        while improved and best is not None:
            improved = False
            for i in range(len(sequence)):
                for j in range(i + 2, len(sequence) + 1):
                    order = sequence[:i] + tuple(reversed(sequence[i:j])) + sequence[j:]
                    candidate = self._evaluate(order)
                    if self.better(candidate, best):
                        sequence, best, improved = order, candidate, True
                        break
                if improved:
                    break
        return best


def _score_details(item: ExhibitScore) -> dict[str, Any]:
    return {
        "total": item.total_score, "tag": item.tag_score,
        "vector": item.vector_score, "base": item.base_score,
        "cosine_similarity": item.cosine_similarity,
        "dimension_matches": dict(item.dimension_matches),
        "conjunction_multiplier": item.conjunction_multiplier,
    }


def _ordered(scores: Sequence[ExhibitScore], entry_coords=(0.0, 0.0)) -> tuple[ExhibitScore, ...]:
    """最近邻，从展厅入口坐标开始；同距按 ID 保证结果可复现。"""
    remaining, result, current = list(scores), [], entry_coords
    while remaining:
        item = min(remaining, key=lambda score: (
            (score.exhibit.coords[0] - current[0]) ** 2 + (score.exhibit.coords[1] - current[1]) ** 2,
            score.exhibit.exhibit_id,
        ))
        remaining.remove(item)
        result.append(item)
        current = item.exhibit.coords
    return tuple(result)


def _render_result(candidate, optimizer, scores, total_minutes, interests, config, method):
    """按实际空间出现顺序输出通行节点，在纯路过厅中就地插入国宝。"""
    snapshot, bundles = optimizer.snapshot, optimizer.bundles
    core_items = {bundles[i].gallery.node_id: candidate.retained[pos]
                  for pos, i in enumerate(candidate.sequence) if candidate.retained[pos]}
    # 空腿（起点等于首厅或末厅等于出口）只生成一个物理节点。
    path, stop_occurrences = [optimizer.start], {}
    for pos, leg in enumerate(candidate.legs):
        path.extend(leg.nodes[1:])
        if pos < len(candidate.sequence):
            stop_occurrences[len(path) - 1] = bundles[candidate.sequence[pos]].gallery.node_id
    gallery_by_node = {g.node_id: g for g in snapshot.galleries}
    selected_ids = {s.exhibit.exhibit_id for items in core_items.values() for s in items}
    treasure_by_gallery: dict[str, list[ExhibitScore]] = {}
    for item in scores:
        if item.exhibit.importance_level == 5 and item.exhibit.exhibit_id not in selected_ids:
            treasure_by_gallery.setdefault(item.exhibit.gallery_id, []).append(item)
    for items in treasure_by_gallery.values():
        items.sort(key=lambda s: (-s.total_score, s.exhibit.exhibit_id))
    route, gallery_visits, pending = [], [], []
    treasure_ids, treasure_time = set(), 0.0
    node_details = []
    for occurrence, node in enumerate(path):
        gallery = gallery_by_node.get(node)
        is_core = occurrence in stop_occurrences and bool(core_items.get(node))
        selected = core_items.get(node, ()) if is_core else ()
        is_treasure_stop = False
        if gallery and not selected and node not in core_items:
            treasures = []
            for item in treasure_by_gallery.get(gallery.gallery_id, ()):
                if item.exhibit.exhibit_id in treasure_ids:
                    continue
                if candidate.total_min + treasure_time + config.treasure_dwell_min <= total_minutes + EPS:
                    treasures.append(item)
                    treasure_ids.add(item.exhibit.exhibit_id)
                    treasure_time += config.treasure_dwell_min
            selected = tuple(treasures)
            is_treasure_stop = bool(selected)
        node_details.append({"node_id": node, "gallery_id": gallery.gallery_id if gallery else None,
                             "floor": snapshot.node_floors[node],
                             "kind": "core" if is_core else "treasure" if is_treasure_stop else "pass"})
        if not gallery:
            continue
        items_out = []
        for item in _ordered(selected, gallery.entry_coords):
            labels = list(item.matched_labels)
            if is_treasure_stop:
                labels.insert(0, "顺路必看·镇馆之宝")
            dwell = config.treasure_dwell_min if is_treasure_stop else _dwell(item, optimizer.pace)
            row = {"exhibit_id": item.exhibit.exhibit_id, "relic_id": item.exhibit.exhibit_id,
                   "name": item.exhibit.name, "gallery_id": gallery.gallery_id,
                   "coords": list(item.exhibit.coords), "dwell_time_min": dwell,
                   "recommendation_tags": labels, "recommendation_reason": "；".join(labels),
                   "is_pass_through_treasure": is_treasure_stop, "score": _score_details(item)}
            items_out.append(row)
            route.append(row)
            pending.append(RouteStop(item.exhibit.exhibit_id, row["recommendation_reason"]))
        gallery_visits.append({"gallery_id": gallery.gallery_id, "node_id": node,
                               "name": gallery.name, "floor": gallery.floor,
                               "kind": node_details[-1]["kind"], "exhibits": items_out,
                               "base_pass_min": gallery.base_pass_min,
                               "dwell_time_min": fsum(item["dwell_time_min"] for item in items_out)})
    dropped = [{"exhibit_id": item.exhibit.exhibit_id, "name": item.exhibit.name,
                "gallery_id": item.exhibit.gallery_id, "score": item.total_score,
                "reason": "为满足净时间预算剔除展厅末位展品",
                "recalled_as_treasure": item.exhibit.exhibit_id in treasure_ids}
               for item in candidate.dropped]
    walk = fsum(leg.walk_min for leg in candidate.legs)
    # 每个空间出现位置的展厅基础时间均计一次，起终点等同节点只计一次。
    passed = fsum(gallery_by_node[node].base_pass_min for node in path if node in gallery_by_node)
    core_dwell = fsum(_dwell(item, optimizer.pace) for items in candidate.retained for item in items)
    floor_penalty = max(0, candidate.floor_changes - config.floor_change_allowance) * config.floor_change_penalty_min
    total = walk + passed + core_dwell + treasure_time + floor_penalty
    if abs(total - (candidate.total_min + treasure_time)) > 1e-6:
        raise RuntimeError("路线账单与搜索成本不一致")
    result = {
        "status": "planned" if route else "no_matching_stops",
        "search_method": method,
        "optimality_scope": "在固定最短通路、单厅Top-K和最低分尾部剔除规则内精确最优" if method == "exact_dp" else "性价比插入贪心与有向2-opt，不保证全局最优",
        "start_node_id": optimizer.start, "exit_node_id": optimizer.exit, "pace": optimizer.pace,
        "interest_profile": asdict(interests),
        "path_nodes": path, "nodes": node_details, "galleries": gallery_visits, "exhibits": route,
        "walk_legs": [{"source": leg.source, "target": leg.target, "path_nodes": list(leg.nodes),
                       "walk_min": leg.walk_min, "pass_min": leg.pass_min,
                       "total_min": leg.total_min, "floor_changes": leg.floor_changes}
                      for leg in candidate.legs],
        "core_gallery_ids": [bundles[i].gallery.gallery_id for pos, i in enumerate(candidate.sequence) if candidate.retained[pos]],
        "core_gain": candidate.gain,
        "treasure_bonus_score": fsum(s.total_score for s in scores if s.exhibit.exhibit_id in treasure_ids),
        "dropped_highlights": dropped,
        "time_bill": {
            "available_min": total_minutes, "net_budget_min": optimizer.budget,
            "reserved_buffer_min": total_minutes - optimizer.budget,
            "walk_min": walk, "gallery_pass_min": passed, "core_dwell_min": core_dwell,
            "treasure_dwell_min": treasure_time, "floor_changes": candidate.floor_changes,
            "floor_penalty_min": floor_penalty, "nominal_core_route_min": candidate.nominal_min,
            "core_route_min": candidate.total_min, "total_min": total,
            "trimmed_dwell_min": max(0.0, candidate.nominal_min - candidate.total_min),
            "buffer_used_min": max(0.0, total - optimizer.budget), "remaining_min": max(0.0, total_minutes - total),
        },
        "warnings": [],
    }
    return result, pending


async def op_plan_route(
    state: SessionState,
    *,
    start_node_id: str,
    exit_node_id: str,
    snapshot: MuseumSnapshot | None = None,
    data_source: MuseumDataSource | None = None,
    interests: InterestProfile | None = None,
    interest_parser: InterestParser | None = None,
    query_vector: Sequence[float] | None = None,
    vector_service: VectorService | None = None,
    pace: str = "standard",
    config: PlannerConfig | None = None,
) -> dict[str, Any]:
    """生成完整 JSON 结果，成功后原子替换展品级 pending_queue。

    直接传 snapshot/interests/query_vector 适合测试或上游已有缓存；否则分别
    await data_source.load()、interest_parser(visitor_info=...) 和
    vector_service.embed_query(text)。馆藏向量由数据源提供，且必须与查询使用
    同一编码模型。模型或数据缺失不使用假标签或零分语义回退。
    remaining_minutes 为本次剩余预算，不再扣减历史参观时间；规划不扣时间。
    起点由 Router 的实际位置提供，不能用最近生成讲解的展品推测。
    """
    if not isinstance(state, SessionState):
        raise ValueError("state 必须是 SessionState")
    if state.status != "active":
        raise RuntimeError("游览已结束，不能规划路线")
    config = config or PlannerConfig()
    if not isinstance(config, PlannerConfig):
        raise ValueError("config 必须是 PlannerConfig")
    if pace not in config.top_k:
        raise ValueError("pace 必须是 quick、standard 或 deep")
    for node in (start_node_id, exit_node_id):
        if not isinstance(node, str) or not node.strip() or node != node.strip():
            raise ValueError("起终点必须为非空且无首尾空格的节点 ID")
    minutes = state.visitor_info.remaining_minutes
    if minutes is None:
        raise ValueError("请先提供 remaining_minutes 再规划路线")
    if minutes == 0:
        result = {"status": "time_exhausted", "pending_queue_updated": True, "start_node_id": start_node_id,
                  "exit_node_id": exit_node_id, "path_nodes": [], "nodes": [], "galleries": [],
                  "exhibits": [], "core_gallery_ids": [], "dropped_highlights": [],
                  "time_bill": {"available_min": 0.0, "total_min": 0.0, "remaining_min": 0.0},
                  "warnings": ["剩余时间已用尽；空路线不表示当前位置已到出口"]}
        state.replace_pending_queue(())
        return result
    if not state.visitor_info.content_interests:
        raise ValueError("请先提供内容兴趣")
    # 同一会话仍需调用方串行化；不向外部适配器传递可变会话对象。
    visitor_info = state.visitor_info
    visited = state.visited_relic_ids
    if snapshot is not None and data_source is not None:
        raise ValueError("snapshot 和 data_source 只能提供一个")
    if snapshot is None:
        if data_source is None:
            raise NotImplementedError("请提供馆藏及空间拓扑快照或 MuseumDataSource")
        snapshot = await data_source.load()
    if not isinstance(snapshot, MuseumSnapshot):
        raise ValueError("数据源必须返回 MuseumSnapshot")
    graph = SpatialGraph(snapshot)
    if start_node_id not in snapshot.node_floors or exit_node_id not in snapshot.node_floors:
        raise ValueError("起终点必须存在于 node_floors 或展厅节点中")
    if interests is None:
        if interest_parser is None:
            raise NotImplementedError("请提供结构化兴趣或 InterestParser")
        interests = await interest_parser(visitor_info=visitor_info)
    if not isinstance(interests, InterestProfile) or not interests.has_targets:
        raise ValueError("兴趣解析器必须返回至少含一个维度的 InterestProfile")
    if query_vector is None:
        if vector_service is None:
            raise NotImplementedError("请提供 query_vector 或 VectorService")
        query_vector = await vector_service.embed_query("；".join(visitor_info.content_interests))
    query_vector = _vector(query_vector, "query_vector")
    scores = score_exhibits(snapshot.exhibits, interests, query_vector,
                            visited_exhibit_ids=visited, config=config)
    bundles = tuple(sorted(aggregate_galleries(snapshot.galleries, scores, pace=pace, config=config),
                           key=lambda bundle: bundle.gallery.gallery_id))
    optimizer = _Optimizer(snapshot, bundles, graph, start_node_id, exit_node_id,
                           pace, float(minutes) * config.net_budget_ratio, config)
    method = "exact_dp" if len(bundles) <= config.exact_gallery_limit else "greedy_2opt"
    candidate = optimizer.exact() if method == "exact_dp" else optimizer.heuristic()
    if candidate is None:
        direct = graph.shortest(start_node_id, exit_node_id)
        return {"status": "no_feasible_route", "search_method": method,
                "start_node_id": start_node_id, "exit_node_id": exit_node_id,
                "path_nodes": [], "nodes": [], "galleries": [], "exhibits": [],
                "dropped_highlights": [], "time_bill": {"available_min": float(minutes),
                "net_budget_min": optimizer.budget, "total_min": None},
                "warnings": ["起点无法到达出口" if direct is None else "通行及必要穿行时间超过净预算"],
                "pending_queue_updated": False}
    result, pending = _render_result(candidate, optimizer, scores, float(minutes), interests, config, method)
    # 外部依赖及全部计算成功后才提交；历史轨迹和游客信息均保持原快照。
    if state.visitor_info != visitor_info or state.visited_relic_ids != visited:
        raise RuntimeError("规划期间会话已变化，请串行调度后重新规划")
    state.replace_pending_queue(pending)
    result["pending_queue_updated"] = True
    return result
