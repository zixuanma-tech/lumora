"""有向空间图及确定性的最短通行时间计算。"""

from __future__ import annotations

import heapq
from dataclasses import dataclass

from route_models import MuseumSnapshot


@dataclass(frozen=True, slots=True)
class WalkLeg:
    source: str
    target: str
    nodes: tuple[str, ...]
    walk_min: float
    pass_min: float
    floor_changes: int

    @property
    def total_min(self) -> float:
        return self.walk_min + self.pass_min


class SpatialGraph:
    """空间有向图。

    边的 walk_time_min 是地图边本身的时间；路径中的中间展厅（不含起点和
    终点）会增加其 base_pass_min。展厅节点和普通空间节点均可作为端点。
    """

    def __init__(self, snapshot: MuseumSnapshot):
        if not isinstance(snapshot, MuseumSnapshot):
            raise ValueError("snapshot 必须是 MuseumSnapshot")
        self.snapshot = snapshot
        self.floors = dict(snapshot.node_floors)
        self.gallery_by_node = {g.node_id: g for g in snapshot.galleries}
        self.adj: dict[str, list[tuple[str, float]]] = {node: [] for node in self.floors}
        for edge in snapshot.edges:
            if edge.source not in self.adj or edge.target not in self.adj:
                raise ValueError(f"空间边引用未知节点: {edge.source}->{edge.target}")
            self.adj[edge.source].append((edge.target, edge.walk_time_min))
        for values in self.adj.values():
            values.sort(key=lambda item: item[0])
        self._cache: dict[tuple[str, str], WalkLeg | None] = {}

    def _path_cost(self, nodes: tuple[str, ...], edge_sum: float) -> tuple[float, float, int]:
        pass_min = sum(self.gallery_by_node[n].base_pass_min for n in nodes[1:-1] if n in self.gallery_by_node)
        changes = sum(self.floors.get(a) != self.floors.get(b) for a, b in zip(nodes, nodes[1:]))
        return edge_sum + pass_min, pass_min, changes

    def shortest(self, source: str, target: str) -> WalkLeg | None:
        if source not in self.adj or target not in self.adj:
            raise ValueError("最短路径端点不在空间图中")
        key = (source, target)
        if key in self._cache:
            return self._cache[key]
        if source == target:
            leg = WalkLeg(source, target, (source,), 0.0, 0.0, 0)
            self._cache[key] = leg
            return leg
        # Dijkstra 的状态中保留完整路径用于确定性 tie break。图规模应远小于展品数，
        # 这样比额外维护 predecessor 更直观，也能在相同耗时下优先少跨层、少跳数。
        heap: list[tuple[float, int, int, tuple[str, ...], str, float]] = [(0.0, 0, 0, (source,), source, 0.0)]
        best: dict[str, tuple[float, int, int, tuple[str, ...]]] = {
            source: (0.0, 0, 0, (source,)),
        }
        while heap:
            total_cost, changes_hint, hops, path, node, edge_sum = heapq.heappop(heap)
            old = best.get(node)
            if (total_cost, changes_hint, hops, path) != old:
                continue
            if node == target:
                _, pass_min, changes = self._path_cost(path, edge_sum)
                leg = WalkLeg(source, target, path, edge_sum, pass_min, changes)
                self._cache[key] = leg
                return leg
            for nxt, weight in self.adj[node]:
                if nxt in path:  # 正权图中的环永远不会改善路径，且路线本身无需环路。
                    continue
                next_path = path + (nxt,)
                next_changes = sum(self.floors.get(a) != self.floors.get(b) for a, b in zip(next_path, next_path[1:]))
                pass_cost = self.gallery_by_node[node].base_pass_min if node != source and node in self.gallery_by_node else 0.0
                item = (total_cost + weight + pass_cost, next_changes, hops + 1, next_path, nxt, edge_sum + weight)
                prior = best.get(nxt)
                if prior is None or item[:4] < prior:
                    best[nxt] = item[:4]
                    heapq.heappush(heap, item)
        self._cache[key] = None
        return None

    def all_pairs(self, nodes: list[str] | tuple[str, ...] | None = None) -> dict[tuple[str, str], WalkLeg | None]:
        selected = tuple(self.adj) if nodes is None else tuple(nodes)
        return {(a, b): self.shortest(a, b) for a in selected for b in selected if a != b}

