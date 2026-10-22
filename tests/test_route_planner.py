"""第二算子端到端测试；服务使用确定性替身，不产生网络请求。"""

import json
from itertools import permutations
import random
import unittest
from unittest.mock import AsyncMock

from route_models import Exhibit, ExhibitTags, Gallery, InterestProfile, MuseumSnapshot, PlannerConfig, SpatialEdge
from route_graph import SpatialGraph
from route_planner import _Optimizer, op_plan_route
from route_scoring import aggregate_galleries, score_exhibits
from session_state import SessionState, VisitorInfo


class PlanRouteTests(unittest.IsolatedAsyncioTestCase):
    def state(self, minutes=60):
        return SessionState(VisitorInfo(content_interests=["陶瓷"], remaining_minutes=minutes))

    def exhibit(self, exhibit_id, gallery_id, **changes):
        data = dict(exhibit_id=exhibit_id, name=exhibit_id, gallery_id=gallery_id,
                    tags=ExhibitTags(category_main=["陶瓷"]), vector=[1, 0], importance_level=4)
        data.update(changes)
        return Exhibit(**data)

    def interests(self):
        return InterestProfile(target_categories=["陶瓷"])

    def linear_snapshot(self, galleries, exhibits):
        nodes = ["start", *(g.node_id for g in galleries), "exit"]
        return MuseumSnapshot(galleries, exhibits,
                              [SpatialEdge(a, b, 1) for a, b in zip(nodes, nodes[1:])],
                              {"start": 1, "exit": galleries[-1].floor if galleries else 1})

    async def plan(self, state, snapshot, **kwargs):
        return await op_plan_route(state, start_node_id="start", exit_node_id="exit", snapshot=snapshot,
                                   interests=self.interests(), query_vector=[1, 0], **kwargs)

    async def test_complete_pipeline_returns_json_and_updates_pending_queue(self):
        snapshot = self.linear_snapshot([Gallery("g", "陶瓷厅", base_pass_min=2)],
                                        [self.exhibit("far", "g", coords=[4, 0]), self.exhibit("near", "g", coords=[1, 0])])
        state = self.state()
        result = await self.plan(state, snapshot)
        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["search_method"], "exact_dp")
        self.assertEqual(result["path_nodes"], ["start", "g", "exit"])
        self.assertEqual([row["exhibit_id"] for row in result["exhibits"]], ["near", "far"])
        self.assertEqual([row.relic_id for row in state.pending_queue], ["near", "far"])
        self.assertEqual(result["time_bill"]["walk_min"], 2)
        self.assertEqual(result["time_bill"]["gallery_pass_min"], 2)
        self.assertEqual(result["time_bill"]["core_dwell_min"], 10)
        self.assertEqual(result["time_bill"]["total_min"], 14)
        json.dumps(result, ensure_ascii=False, allow_nan=False)

    async def test_existing_visits_and_closed_exhibits_are_never_selected(self):
        state = self.state()
        state.replace_pending_queue(["visited"])
        state.complete_visit("visited", "已完成的讲解")
        visits = state.visited_relics
        snapshot = self.linear_snapshot([Gallery("g", "陶瓷厅")], [self.exhibit("visited", "g"),
                    self.exhibit("closed", "g", is_open=False), self.exhibit("new", "g")])
        result = await self.plan(state, snapshot)
        self.assertEqual([row["exhibit_id"] for row in result["exhibits"]], ["new"])
        self.assertIs(state.visited_relics, visits)

    async def test_gallery_entry_coordinates_control_intra_gallery_sequence(self):
        snapshot = self.linear_snapshot([Gallery("g", "陶瓷厅", entry_coords=(10, 0))],
                    [self.exhibit("left", "g", coords=(1, 0)), self.exhibit("right", "g", coords=(9, 0))])
        result = await self.plan(self.state(), snapshot)
        self.assertEqual([row["exhibit_id"] for row in result["exhibits"]], ["right", "left"])

    async def test_fine_grained_trim_drops_lowest_tail_and_recomputes_gain(self):
        snapshot = self.linear_snapshot([Gallery("g", "陶瓷厅", base_pass_min=1)],
                    [self.exhibit("best", "g", importance_level=4), self.exhibit("tail", "g", importance_level=3)])
        result = await self.plan(self.state(10), snapshot)
        self.assertEqual([row["exhibit_id"] for row in result["exhibits"]], ["best"])
        self.assertEqual([row["exhibit_id"] for row in result["dropped_highlights"]], ["tail"])
        self.assertEqual(result["core_gain"], 96)
        self.assertEqual(result["time_bill"]["nominal_core_route_min"], 13)
        self.assertEqual(result["time_bill"]["total_min"], 8)

    async def test_pass_through_treasure_uses_buffer_and_keeps_physical_order(self):
        galleries = [Gallery("pass", "途经展厅", base_pass_min=1), Gallery("core", "陶瓷厅", base_pass_min=1)]
        # 国宝偏离兴趣，综合分 50 不过核心厅的 60 分线，但可在途经时补偿。
        snapshot = self.linear_snapshot(galleries, [self.exhibit("treasure", "pass", tags=ExhibitTags(), importance_level=5),
                                                  self.exhibit("core_item", "core")])
        result = await self.plan(self.state(15), snapshot)
        self.assertEqual([row["exhibit_id"] for row in result["exhibits"]], ["treasure", "core_item"])
        self.assertEqual(result["exhibits"][0]["recommendation_tags"][0], "顺路必看·镇馆之宝")
        self.assertEqual(result["core_gallery_ids"], ["core"])
        self.assertEqual(result["time_bill"]["core_route_min"], 10)
        self.assertEqual(result["time_bill"]["treasure_dwell_min"], 4)
        self.assertEqual(result["time_bill"]["buffer_used_min"], 0.5)
        self.assertEqual(result["time_bill"]["total_min"], 14)

    async def test_treasure_is_omitted_when_whole_budget_cannot_fit(self):
        snapshot = self.linear_snapshot([Gallery("pass", "途经展厅", base_pass_min=1), Gallery("core", "陶瓷厅", base_pass_min=1)],
                     [self.exhibit("treasure", "pass", tags=ExhibitTags(), importance_level=5), self.exhibit("core_item", "core")])
        result = await self.plan(self.state(13), snapshot)
        self.assertEqual([row["exhibit_id"] for row in result["exhibits"]], ["core_item"])

    async def test_floor_change_penalty_starts_with_third_change(self):
        snapshot = self.linear_snapshot([Gallery("g1", "一层", floor=1), Gallery("g2", "二层", floor=2), Gallery("g3", "三层", floor=3)],
                     [self.exhibit("e1", "g1"), self.exhibit("e2", "g2"), self.exhibit("e3", "g3")])
        # 入口设为地下一层，三次跨层累计 penalty=3。
        snapshot = MuseumSnapshot(snapshot.galleries, snapshot.exhibits, snapshot.edges, {"start": 0, "exit": 3})
        result = await self.plan(self.state(), snapshot)
        self.assertEqual(result["time_bill"]["floor_changes"], 3)
        self.assertEqual(result["time_bill"]["floor_penalty_min"], 3)
        self.assertEqual(result["time_bill"]["total_min"], 22)

    async def test_no_feasible_path_preserves_previous_queue(self):
        state = self.state()
        state.replace_pending_queue(["old"])
        snapshot = MuseumSnapshot([Gallery("g", "孤立展厅")], [self.exhibit("e", "g")], [], {"start": 1, "exit": 1})
        result = await self.plan(state, snapshot)
        self.assertEqual(result["status"], "no_feasible_route")
        self.assertFalse(result["pending_queue_updated"])
        self.assertEqual(state.pending_queue[0].relic_id, "old")

    async def test_service_adapters_receive_immutable_visitor_information(self):
        state = self.state()
        snapshot = self.linear_snapshot([Gallery("g", "展厅")], [self.exhibit("e", "g")])
        source, parser, vector = AsyncMock(), AsyncMock(), AsyncMock()
        source.load.return_value = snapshot
        parser.return_value = self.interests()
        vector.embed_query.return_value = [1, 0]
        await op_plan_route(state, start_node_id="start", exit_node_id="exit", data_source=source,
                            interest_parser=parser, vector_service=vector)
        source.load.assert_awaited_once_with()
        parser.assert_awaited_once_with(visitor_info=state.visitor_info)
        vector.embed_query.assert_awaited_once_with("陶瓷")

    async def test_bad_service_response_and_finished_session_do_not_change_queue(self):
        state = self.state()
        state.replace_pending_queue(["old"])
        snapshot = self.linear_snapshot([Gallery("g", "展厅")], [self.exhibit("e", "g", vector=[])])
        with self.assertRaises(ValueError):
            await self.plan(state, snapshot)
        self.assertEqual(state.pending_queue[0].relic_id, "old")
        state.finish()
        with self.assertRaises(RuntimeError):
            await self.plan(state, snapshot)

    async def test_zero_time_clears_queue_without_requiring_services(self):
        state = self.state(0)
        state.replace_pending_queue(["old"])
        result = await op_plan_route(state, start_node_id="start", exit_node_id="exit")
        self.assertEqual(result["status"], "time_exhausted")
        self.assertEqual(state.pending_queue, ())

    async def test_unknown_time_requires_input_and_preserves_existing_queue(self):
        state = self.state(None)
        state.replace_pending_queue(["old"])
        with self.assertRaisesRegex(ValueError, "remaining_minutes"):
            await op_plan_route(state, start_node_id="start", exit_node_id="exit")
        self.assertEqual([item.relic_id for item in state.pending_queue], ["old"])

    async def test_starting_gallery_is_charged_on_later_pass(self):
        snapshot = MuseumSnapshot(
            [Gallery("a", "甲厅", base_pass_min=2), Gallery("b", "乙厅", base_pass_min=1)],
            [self.exhibit("ea", "a"), self.exhibit("eb", "b")],
            [SpatialEdge("a", "b", 1), SpatialEdge("b", "a", 1), SpatialEdge("a", "exit", 1)],
            {"exit": 1},
        )
        state = self.state()
        result = await op_plan_route(state, start_node_id="a", exit_node_id="exit", snapshot=snapshot,
                                     interests=self.interests(), query_vector=[1, 0])
        self.assertEqual(set(item.relic_id for item in state.pending_queue), {"ea", "eb"})
        self.assertEqual(result["time_bill"]["gallery_pass_min"], 5)
        self.assertEqual(result["time_bill"]["total_min"], 18)

    async def test_treasures_in_closed_or_visited_items_are_not_recalled(self):
        state = self.state()
        state.replace_pending_queue(["visited"])
        state.complete_visit("visited", "已看")
        snapshot = self.linear_snapshot(
            [Gallery("pass", "途经厅"), Gallery("core", "核心厅")],
            [self.exhibit("visited", "pass", importance_level=5, tags=ExhibitTags(), vector=[]),
             self.exhibit("closed", "pass", importance_level=5, tags=ExhibitTags(), vector=[], is_open=False),
             self.exhibit("new", "core")],
        )
        result = await self.plan(state, snapshot)
        self.assertEqual([item["exhibit_id"] for item in result["exhibits"]], ["new"])
        self.assertEqual(result["time_bill"]["treasure_dwell_min"], 0)

    async def test_thirteen_candidate_galleries_use_heuristic(self):
        galleries = [Gallery(f"g{i:02}", str(i)) for i in range(13)]
        snapshot = self.linear_snapshot(galleries, [self.exhibit(f"e{i:02}", g.gallery_id) for i, g in enumerate(galleries)])
        result = await self.plan(self.state(100), snapshot)
        self.assertEqual(result["search_method"], "greedy_2opt")
        self.assertEqual(len(result["core_gallery_ids"]), 13)
        self.assertEqual(len(set(result["core_gallery_ids"])), 13)
        self.assertLessEqual(result["time_bill"]["total_min"], 90)


class ExactSearchTests(unittest.TestCase):
    def test_dp_matches_enumeration_with_cropping_and_floor_penalties(self):
        """在独立枚举的所有子集排列中核对 DP，覆盖访问顺序和裁剪的耦合。"""
        rng = random.Random(20)
        config = PlannerConfig()
        interests = InterestProfile(target_categories=["陶瓷"])
        for case in range(6):
            galleries = [Gallery(f"g{i}", str(i), floor=rng.randint(1, 3), base_pass_min=rng.randint(0, 2)) for i in range(5)]
            exhibits = [Exhibit(f"e{i}{j}", str(i), g.gallery_id, tags=ExhibitTags(category_main=["陶瓷"]),
                               vector=[1, 0], importance_level=rng.randint(1, 5),
                               dwell_time_map={"quick": 3, "standard": rng.randint(2, 7), "deep": 8})
                        for i, g in enumerate(galleries) for j in range(2)]
            nodes = ["s", *(g.node_id for g in galleries), "e"]
            edges = [SpatialEdge(a, b, rng.randint(1, 8)) for a in nodes for b in nodes if a != b]
            snapshot = MuseumSnapshot(galleries, exhibits, edges, {"s": 1, "e": 1})
            bundles = aggregate_galleries(galleries, score_exhibits(exhibits, interests, [1, 0]), config=config)
            optimizer = _Optimizer(snapshot, bundles, SpatialGraph(snapshot), "s", "e", "standard", rng.randint(20, 40), config)
            expected = optimizer._evaluate(())
            for length in range(1, len(bundles) + 1):
                for sequence in permutations(range(len(bundles)), length):
                    candidate = optimizer._evaluate(sequence)
                    if optimizer.better(candidate, expected):
                        expected = candidate
            actual = optimizer.exact()
            with self.subTest(case=case):
                self.assertIsNotNone(actual)
                self.assertAlmostEqual(actual.gain, expected.gain)
                self.assertAlmostEqual(actual.total_min, expected.total_min)

    def test_twelve_galleries_remain_in_exact_search(self):
        galleries = [Gallery(f"g{i:02}", str(i)) for i in range(12)]
        exhibits = [Exhibit(f"e{i:02}", str(i), g.gallery_id,
                            tags=ExhibitTags(category_main=["陶瓷"]), vector=[1, 0])
                    for i, g in enumerate(galleries)]
        nodes = ["s", *(g.node_id for g in galleries), "e"]
        snapshot = MuseumSnapshot(galleries, exhibits,
                                  [SpatialEdge(a, b, 1) for a, b in zip(nodes, nodes[1:])],
                                  {"s": 1, "e": 1})
        bundles = aggregate_galleries(galleries, score_exhibits(exhibits, InterestProfile(target_categories=["陶瓷"]), [1, 0]))
        result = _Optimizer(snapshot, bundles, SpatialGraph(snapshot), "s", "e", "standard", 100, PlannerConfig()).exact()
        self.assertEqual(len(result.sequence), 12)
        self.assertEqual(result.total_min, 73)


if __name__ == "__main__":
    unittest.main()
