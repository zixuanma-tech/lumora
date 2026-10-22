"""空间图的成本、方向性与最短路径回归测试。"""

import unittest

from route_graph import SpatialGraph
from route_models import Gallery, MuseumSnapshot, SpatialEdge


class SpatialGraphTests(unittest.TestCase):
    def test_shortest_path_includes_pass_cost_during_search(self):
        """高穿厅成本的短边路径不得挤掉总体更快的替代路线。"""
        graph = SpatialGraph(MuseumSnapshot(
            galleries=(
                Gallery("a", "高穿行成本厅", floor=1, base_pass_min=20),
                Gallery("b", "低穿行成本厅", floor=1, base_pass_min=1),
            ),
            exhibits=(),
            edges=(
                SpatialEdge("s", "a", 1), SpatialEdge("a", "x", 1),
                SpatialEdge("s", "b", 4), SpatialEdge("b", "x", 4),
            ),
            node_floors={"s": 1, "x": 1},
        ))
        leg = graph.shortest("s", "x")
        self.assertEqual(leg.nodes, ("s", "b", "x"))
        self.assertEqual(leg.walk_min, 8)
        self.assertEqual(leg.pass_min, 1)
        self.assertEqual(leg.total_min, 9)

    def test_endpoints_do_not_charge_pass_and_edges_are_directed(self):
        graph = SpatialGraph(MuseumSnapshot(
            galleries=(
                Gallery("a", "A", floor=1, base_pass_min=10),
                Gallery("b", "B", floor=2, base_pass_min=20),
            ), exhibits=(), edges=(SpatialEdge("a", "b", 2),),
        ))
        leg = graph.shortest("a", "b")
        self.assertEqual(leg.total_min, 2)
        self.assertEqual(leg.pass_min, 0)
        self.assertEqual(leg.floor_changes, 1)
        self.assertIsNone(graph.shortest("b", "a"))
        self.assertEqual(graph.shortest("a", "a").total_min, 0)

    def test_equal_times_choose_fewer_floor_changes(self):
        graph = SpatialGraph(MuseumSnapshot(
            galleries=(), exhibits=(),
            edges=(
                SpatialEdge("s", "up", 1), SpatialEdge("up", "x", 1),
                SpatialEdge("s", "flat", 1), SpatialEdge("flat", "x", 1),
            ), node_floors={"s": 1, "x": 1, "up": 2, "flat": 1},
        ))
        self.assertEqual(graph.shortest("s", "x").nodes, ("s", "flat", "x"))

    def test_all_pairs_returns_unreachable_direction(self):
        graph = SpatialGraph(MuseumSnapshot(
            galleries=(), exhibits=(), edges=(SpatialEdge("s", "x", 1),),
            node_floors={"s": 1, "x": 1},
        ))
        pairs = graph.all_pairs()
        self.assertEqual(pairs["s", "x"].total_min, 1)
        self.assertIsNone(pairs["x", "s"])

    def test_unknown_node_is_rejected(self):
        graph = SpatialGraph(MuseumSnapshot(galleries=(), exhibits=(), edges=(), node_floors={"s": 1}))
        with self.assertRaisesRegex(ValueError, "端点"):
            graph.shortest("s", "missing")


if __name__ == "__main__":
    unittest.main()
