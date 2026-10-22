"""下一件讲解预生成：用事件控制并发，不调用模型或真实网络。"""

import asyncio
from collections import Counter
from dataclasses import replace
import json
import unittest
from unittest.mock import AsyncMock, patch

from guide_models import GuideDraft, GuideMaterial, GuideSelection
from operators import InputAnalysis
from route_models import Exhibit, ExhibitTags, Gallery, InterestProfile, MuseumSnapshot, SpatialEdge
from router import Router, RouterServices


class Clock:
    def __init__(self):
        self.seconds = 1000.0

    def __call__(self):
        return self.seconds

    def advance(self, seconds):
        self.seconds += seconds


class GuidePrefetchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Clock()
        self.calls = []
        self.counts = Counter()
        self.gates = {}
        self.failures = set()
        self.routers = []
        self.interpreter = AsyncMock(return_value=InputAnalysis())

        async def select(*, request):
            return GuideSelection(request.relic_id, (
                GuideMaterial("fact", "有来源的测试材料。", ("source",)),
            ))

        async def compose(*, request, selection):
            kind = "answer" if request.question else "guide"
            key = (request.session_id, request.relic_id, kind)
            self.counts[key] += 1
            call_key = (*key, self.counts[key])
            self.calls.append((call_key, request))
            gate = self.gates.get(call_key)
            if gate is not None:
                await gate.wait()
            if call_key in self.failures:
                raise RuntimeError("预生成服务失败")
            style = "/".join(request.visitor_info.guide_styles) or "默认"
            return GuideDraft(request.relic_id, (
                f"{kind}:{request.relic_id}:{style}:{call_key[-1]}。", "第二个完整单元。",
            ), ("fact",))

        vector = AsyncMock()
        vector.embed_query.return_value = [1, 0]
        self.snapshot = MuseumSnapshot(
            [Gallery("g", "测试展厅", base_pass_min=1)],
            [Exhibit(item, item, "g", tags=ExhibitTags(category_main=["陶瓷"]),
                     vector=[1, 0], importance_level=4, coords=[index, 0])
             for index, item in enumerate(("a", "b", "c", "d"), 1)],
            [SpatialEdge("entrance", "g", 1), SpatialEdge("g", "exit", 1)],
            {"entrance": 1, "exit": 1},
        )
        self.services = RouterServices(
            interpreter=self.interpreter, selector=select, composer=compose,
            guide_review_mode="integrated", snapshot=self.snapshot,
            interest_parser=AsyncMock(return_value=InterestProfile(target_categories=["陶瓷"])),
            vector_service=vector,
        )
        self.router = self.make_router()

    def make_router(self, **options):
        router = Router(self.services, clock=self.clock, **options)
        self.routers.append(router)
        return router

    async def asyncTearDown(self):
        for gate in self.gates.values():
            gate.set()
        for router in self.routers:
            await router.aclose()

    def create(self, session_id="s", *, router=None, minutes=60, queue=("a", "b", "c", "d")):
        router = router or self.router
        router.create_session(
            {"content_interests": ["陶瓷"], "remaining_minutes": minutes}, session_id=session_id,
            current_node_id="entrance", exit_node_id="exit",
        )
        session = router._sessions[session_id]
        session.state.replace_pending_queue(queue)
        session.route_dirty = False
        return session

    def block(self, target="b", occurrence=1, session_id="s", kind="guide"):
        event = asyncio.Event()
        self.gates[(session_id, target, kind, occurrence)] = event
        return event

    def count(self, target, session_id="s", kind="guide"):
        return self.counts[(session_id, target, kind)]

    async def spin_until(self, condition, message="异步操作未完成"):
        for _ in range(500):
            if condition():
                return
            await asyncio.sleep(0)
        self.fail(message)

    async def state(self, *, router=None, session_id="s"):
        return await (router or self.router).dispatch_action(session_id, "get_state")

    async def wait_status(self, status, target="b", *, router=None, session_id="s"):
        for _ in range(500):
            response = await self.state(router=router, session_id=session_id)
            item = response["reply_content"]["guide_prefetch"]
            if item["status"] == status and (target is None or item.get("relic_id") == target):
                return response
            await asyncio.sleep(0)
        self.fail(f"未等到 {target} 的预生成状态 {status}")

    async def start(self, target="a", *, router=None, session_id="s"):
        response = await (router or self.router).dispatch_action(
            session_id, "start_guide", {"relic_id": target},
        )
        self.assertEqual(response["reply_content"]["status"], "guide_ready", response["reply_content"])
        return response

    async def test_prefetch_is_only_next_and_never_commits_or_exposes_the_draft(self):
        session = self.create()
        gate = self.block()
        main = await self.start()
        await self.spin_until(lambda: self.count("b") == 1)
        response = await self.state()
        self.assertEqual(session.state.visited_relic_ids, ("a",))
        self.assertEqual(len(session.state.guide_records), 1)
        self.assertEqual(tuple(item.relic_id for item in session.state.pending_queue), ("b", "c", "d"))
        self.assertEqual(response["reply_content"]["playback"]["current"],
                         main["reply_content"]["playback"]["current"])
        self.assertEqual(response["reply_content"]["guide_prefetch"]["relic_id"], "b")
        self.assertEqual(response["reply_content"]["guide_prefetch"]["status"], "generating")
        gate.set()
        response = await self.wait_status("ready")
        self.assertEqual(session.state.visited_relic_ids, ("a",))
        self.assertEqual(len(session.state.guide_records), 1)
        self.assertEqual(self.count("c"), 0)
        encoded = json.dumps(response["reply_content"]["guide_prefetch"], ensure_ascii=False)
        self.assertNotIn("segments", encoded)
        self.assertNotIn("guide:b", encoded)

    async def test_ready_cache_commits_on_click_without_another_model_call(self):
        session = self.create()
        await self.start()
        await self.wait_status("ready")
        self.assertEqual(self.count("b"), 1)
        response = await self.start("b")
        self.assertEqual(self.count("b"), 1)
        self.assertEqual(session.state.visited_relic_ids, ("a", "b"))
        self.assertEqual(len(session.state.guide_records), 2)
        self.assertEqual(response["reply_content"]["playback"]["current"]["relic_id"], "b")
        await self.wait_status("ready", "c")
        self.assertEqual(self.count("d"), 0)

    async def test_waiting_for_prefetch_reuses_request_and_does_not_lock_playback(self):
        self.create()
        gate = self.block()
        main = await self.start()
        await self.spin_until(lambda: self.count("b") == 1)
        waiting = asyncio.create_task(self.start("b"))
        await asyncio.sleep(0)
        progress = await asyncio.wait_for(self.router.dispatch_action("s", "playback_progress", {
            "guide_id": main["reply_content"]["playback"]["current"]["guide_id"],
            "completed_segments": 1,
        }), timeout=1)
        self.assertEqual(progress["reply_content"]["status"], "ok")
        self.assertFalse(waiting.done())
        self.assertEqual(self.count("b"), 1)
        gate.set()
        await asyncio.wait_for(waiting, timeout=1)
        self.assertEqual(self.count("b"), 1)

    async def test_pure_question_does_not_wait_for_or_invalidate_prefetch(self):
        session = self.create()
        gate = self.block()
        main = await self.start()
        await self.spin_until(lambda: self.count("b") == 1)
        self.interpreter.return_value = InputAnalysis(question="有什么用途？")
        response = await asyncio.wait_for(self.router.dispatch_chat("s", "有什么用途？", playback={
            "guide_id": main["reply_content"]["playback"]["current"]["guide_id"],
            "completed_segments": 1,
        }), timeout=1)
        self.assertEqual(response["reply_content"]["status"], "answer_ready")
        self.assertEqual(response["reply_content"]["guide_prefetch"]["status"], "generating")
        self.assertEqual(session.state.visited_relic_ids, ("a",))
        self.assertEqual(len(session.state.guide_records), 2)
        self.assertEqual(len(response["reply_content"]["playback"]["resume_stack"]), 1)
        gate.set()
        await self.wait_status("ready")
        await self.start("b")
        self.assertEqual(self.count("b"), 1)

    async def test_style_change_discards_old_result_and_prepares_latest_style(self):
        session = self.create()
        old = self.block()
        newer = self.block(occurrence=2)
        await self.start()
        await self.spin_until(lambda: self.count("b") == 1)
        response = await self.router.dispatch_action("s", "update_visitor_info", {
            "visitor_info": {"guide_styles": ["简洁"]},
        })
        self.assertEqual(response["reply_content"]["status"], "ok")
        self.assertEqual(self.count("b"), 1)
        old.set()
        await self.spin_until(lambda: self.count("b") == 2)
        self.assertEqual(session.state.visited_relic_ids, ("a",))
        newer.set()
        await self.wait_status("ready")
        response = await self.start("b")
        self.assertIn("简洁:2", response["reply_content"]["guide"]["guide_text"])
        self.assertEqual(self.count("b"), 2)

    async def test_same_explicit_time_budget_invalidates_but_natural_time_does_not(self):
        self.create()
        await self.start()
        await self.wait_status("ready")
        self.clock.advance(120)
        response = await self.state()
        self.assertEqual(response["latest_state"]["visitor_info"]["remaining_minutes"], 58)
        self.assertEqual(response["reply_content"]["guide_prefetch"]["status"], "ready")
        self.assertEqual(self.count("b"), 1)
        await self.router.dispatch_action("s", "update_visitor_info", {
            "visitor_info": {"remaining_minutes": 58},
        })
        await self.spin_until(lambda: self.count("b") == 2)
        await self.wait_status("ready")
        await self.start("b")
        self.assertEqual(self.count("b"), 2)

    async def prepare_budget_replan(self, *, block=False):
        session = self.create()
        planned = await self.router.dispatch_action("s", "replan")
        self.assertEqual(planned["reply_content"]["status"], "planned")
        gate = self.block() if block else None
        await self.start()
        if block:
            await self.spin_until(lambda: self.count("b") == 1)
        else:
            await self.wait_status("ready")
        # 正常规划已拿到兴趣和向量；自动预算重算不得再访问这些服务。
        self.services.interest_parser.side_effect = TimeoutError("兴趣服务超时")
        self.services.vector_service.embed_query.side_effect = TimeoutError("向量服务超时")
        return session, gate

    def exceed_cached_route_budget(self, session):
        estimate = self.router._remaining_estimate(session)
        self.clock.advance((session.state.visitor_info.remaining_minutes - estimate + 1) * 60)

    async def test_ready_cache_survives_budget_replan_without_network_calls(self):
        session, _ = await self.prepare_budget_replan()
        self.exceed_cached_route_budget(session)
        response = await self.start("b")
        self.assertIn("route_planned", response["reply_content"]["steps"])
        self.assertEqual(self.count("b"), 1)
        self.assertEqual(session.state.visited_relic_ids, ("a", "b"))
        self.services.interest_parser.assert_awaited_once()
        self.services.vector_service.embed_query.assert_awaited_once()

    async def test_inflight_cache_survives_budget_replan_on_click(self):
        session, gate = await self.prepare_budget_replan(block=True)
        self.exceed_cached_route_budget(session)
        waiting = asyncio.create_task(self.start("b"))
        await self.spin_until(lambda: session.foreground_job is not None)
        self.assertFalse(waiting.done())
        self.assertEqual(self.count("b"), 1)
        gate.set()
        response = await asyncio.wait_for(waiting, 1)
        self.assertIn("route_planned", response["reply_content"]["steps"])
        self.assertEqual(self.count("b"), 1)

    async def test_budget_replan_reuses_loaded_snapshot(self):
        source = AsyncMock()
        source.load.return_value = self.snapshot
        self.services = replace(self.services, snapshot=None, data_source=source)
        self.router = self.make_router()
        session, _ = await self.prepare_budget_replan()
        source.load.side_effect = TimeoutError("数据源服务超时")
        self.exceed_cached_route_budget(session)
        response = await self.start("b")
        source.load.assert_awaited_once()
        self.assertEqual(self.count("b"), 1)
        self.assertNotIn("planning_inputs", response["reply_content"])
        self.assertNotIn("query_vector", response["reply_content"])

    async def test_inflight_cache_survives_budget_replan_after_waiting(self):
        session, gate = await self.prepare_budget_replan(block=True)
        waiting = asyncio.create_task(self.start("b"))
        await self.spin_until(lambda: session.foreground_job is not None)
        self.exceed_cached_route_budget(session)
        gate.set()
        response = await asyncio.wait_for(waiting, 1)
        self.assertIn("route_planned", response["reply_content"]["steps"])
        self.assertEqual(self.count("b"), 1)

    async def test_budget_replan_changed_target_invalidates_ready_cache(self):
        session, _ = await self.prepare_budget_replan()
        old = session.prefetch
        self.exceed_cached_route_budget(session)

        async def change_target(state, **kwargs):
            state.replace_pending_queue(("c", "d"))
            return {"status": "planned", "pending_queue_updated": True}

        with patch("router.op_plan_route", side_effect=change_target):
            response = await self.router.dispatch_action("s", "start_guide", {"relic_id": "b"})
        self.assertTrue(old.invalidated.is_set())
        self.assertEqual(session.state.visited_relic_ids, ("a",))
        self.assertNotEqual(response["reply_content"]["status"], "guide_ready")
        self.assertNotEqual(response["reply_content"]["guide_prefetch"]["relic_id"], "b")

    async def test_budget_replan_failure_invalidates_ready_cache(self):
        for failure in (RuntimeError("本地规划失败"), {
            "status": "no_feasible_route", "pending_queue_updated": False,
        }):
            with self.subTest(failure=failure):
                router = self.make_router()
                self.create(router=router)
                await self.start(router=router)
                await self.wait_status("ready", router=router)
                session = router._sessions["s"]
                old = session.prefetch
                session.route = {"time_bill": {"total_min": 60}, "exhibits": []}
                self.clock.advance(60)
                with patch("router.op_plan_route", side_effect=failure if isinstance(failure, Exception) else None,
                           return_value=failure):
                    response = await router.dispatch_action("s", "start_guide", {"relic_id": "b"})
                self.assertTrue(old.invalidated.is_set())
                self.assertTrue(session.route_dirty)
                self.assertEqual(response["reply_content"]["guide_prefetch"]["status"], "idle")
                self.assertEqual(session.state.visited_relic_ids, ("a",))

    async def test_changed_preferences_do_not_reuse_planning_inputs(self):
        session, _ = await self.prepare_budget_replan()
        self.services.interest_parser.side_effect = None
        self.services.vector_service.embed_query.side_effect = None
        await self.router.dispatch_action("s", "update_visitor_info", {
            "visitor_info": {"guide_focus": ["工艺"]},
        })
        await self.wait_status("ready")
        self.exceed_cached_route_budget(session)
        await self.start("b")
        self.assertEqual(self.services.interest_parser.await_count, 2)
        self.assertEqual(self.services.vector_service.embed_query.await_count, 2)

    async def test_replan_invalidates_even_when_the_next_relic_is_unchanged(self):
        self.create()
        await self.start()
        await self.wait_status("ready")
        response = await self.router.dispatch_action("s", "replan")
        self.assertEqual(response["reply_content"]["status"], "planned")
        self.assertEqual(response["latest_state"]["pending_queue"][0]["relic_id"], "b")
        await self.spin_until(lambda: self.count("b") == 2)
        await self.wait_status("ready")
        await self.start("b")
        self.assertEqual(self.count("b"), 2)

    async def test_location_change_discards_inflight_result_until_route_is_valid(self):
        self.create()
        gate = self.block()
        await self.start()
        await self.spin_until(lambda: self.count("b") == 1)
        response = await self.router.dispatch_action("s", "update_location", {"current_node_id": "g"})
        self.assertTrue(response["reply_content"]["route_dirty"])
        gate.set()
        for _ in range(30):
            await asyncio.sleep(0)
        response = await self.state()
        self.assertNotEqual(response["reply_content"]["guide_prefetch"]["status"], "ready")
        self.assertEqual(self.count("b"), 1)
        await self.router.dispatch_action("s", "replan")
        await self.spin_until(lambda: self.count("b") == 2)
        await self.wait_status("ready")

    async def test_multiple_skips_keep_only_latest_target_without_hidden_parallel_calls(self):
        session = self.create()
        old = self.block()
        await self.start()
        await self.spin_until(lambda: self.count("b") == 1)
        await self.router.dispatch_action("s", "skip", {"relic_id": "b"})
        await self.router.dispatch_action("s", "skip", {"relic_id": "c"})
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertEqual(self.count("c"), 0)
        self.assertEqual(self.count("d"), 0)
        old.set()
        await self.wait_status("ready", "d")
        self.assertEqual(self.count("d"), 1)
        self.assertEqual(self.count("c"), 0)
        self.assertEqual(session.state.visited_relic_ids, ("a",))
        self.assertEqual(session.state.skipped_relics, ("b", "c"))

    async def test_finish_clears_cache_and_late_results_cannot_add_records(self):
        session = self.create()
        gate = self.block()
        await self.start()
        await self.spin_until(lambda: self.count("b") == 1)
        response = await self.router.dispatch_action("s", "finish")
        self.assertEqual(response["latest_state"]["status"], "finished")
        self.assertEqual(response["reply_content"]["guide_prefetch"]["status"], "idle")
        gate.set()
        for _ in range(30):
            await asyncio.sleep(0)
        response = await self.state()
        self.assertEqual(response["reply_content"]["guide_prefetch"]["status"], "idle")
        self.assertEqual(session.state.visited_relic_ids, ("a",))
        self.assertEqual(len(session.state.guide_records), 1)
        self.assertEqual(self.count("c"), 0)

    async def test_expired_budget_clears_pending_and_prefetch_without_finishing_session(self):
        session = self.create(minutes=1)
        gate = self.block()
        await self.start()
        await self.spin_until(lambda: self.count("b") == 1)
        self.clock.advance(60)
        response = await self.state()
        self.assertEqual(response["latest_state"]["status"], "active")
        self.assertEqual(response["latest_state"]["pending_queue"], [])
        self.assertEqual(response["reply_content"]["guide_prefetch"]["status"], "idle")
        gate.set()
        for _ in range(30):
            await asyncio.sleep(0)
        self.assertEqual(session.state.visited_relic_ids, ("a",))
        self.assertEqual(len(session.state.guide_records), 1)
        self.assertEqual(self.count("c"), 0)

    async def test_background_failure_does_not_retry_until_user_starts_next(self):
        session = self.create()
        self.failures.add(("s", "b", "guide", 1))
        await self.start()
        await self.wait_status("failed")
        for _ in range(5):
            await self.state()
        self.assertEqual(self.count("b"), 1)
        self.assertEqual(session.state.visited_relic_ids, ("a",))
        await self.start("b")
        self.assertEqual(self.count("b"), 2)
        self.assertEqual(session.state.visited_relic_ids, ("a", "b"))

    async def test_duplicate_start_waiters_commit_once_and_reuse_same_request(self):
        session = self.create()
        gate = self.block()
        await self.start()
        await self.spin_until(lambda: self.count("b") == 1)
        first = asyncio.create_task(self.router.dispatch_action("s", "start_guide", {"relic_id": "b"}))
        second = asyncio.create_task(self.router.dispatch_action("s", "start_guide", {"relic_id": "b"}))
        await asyncio.sleep(0)
        gate.set()
        responses = await asyncio.wait_for(asyncio.gather(first, second), timeout=1)
        self.assertTrue(any(item["reply_content"]["status"] == "guide_ready" for item in responses))
        self.assertEqual(self.count("b"), 1)
        self.assertEqual(session.state.visited_relic_ids, ("a", "b"))
        self.assertEqual(len(session.state.guide_records), 2)

    async def test_cancelled_waiter_does_not_cancel_shared_background_request(self):
        self.create()
        gate = self.block()
        await self.start()
        await self.spin_until(lambda: self.count("b") == 1)
        waiting = asyncio.create_task(self.router.dispatch_action("s", "start_guide", {"relic_id": "b"}))
        await asyncio.sleep(0)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        gate.set()
        await self.wait_status("ready")
        await self.start("b")
        self.assertEqual(self.count("b"), 1)

    async def test_cancelled_foreground_waiter_keeps_request_for_next_click(self):
        session = self.create()
        gate = self.block("a")
        waiting = asyncio.create_task(self.router.dispatch_action("s", "start_guide", {"relic_id": "a"}))
        await self.spin_until(lambda: self.count("a") == 1)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertEqual(session.state.visited_relic_ids, ())
        self.assertEqual(len(session.state.guide_records), 0)
        following = asyncio.create_task(self.start())
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertFalse(following.done())
        self.assertEqual(self.count("a"), 1)
        gate.set()
        await asyncio.wait_for(following, timeout=1)
        self.assertEqual(self.count("a"), 1)
        self.assertEqual(session.state.visited_relic_ids, ("a",))
        self.assertEqual(len(session.state.guide_records), 1)

    async def test_skip_while_start_waits_prevents_stale_commit(self):
        session = self.create()
        gate = self.block()
        await self.start()
        await self.spin_until(lambda: self.count("b") == 1)
        waiting = asyncio.create_task(self.router.dispatch_action("s", "start_guide", {"relic_id": "b"}))
        await asyncio.sleep(0)
        skipped = await asyncio.wait_for(self.router.dispatch_action("s", "skip", {"relic_id": "b"}), timeout=1)
        self.assertEqual(skipped["reply_content"]["status"], "ok")
        response = await asyncio.wait_for(waiting, timeout=1)
        self.assertEqual(response["reply_content"]["status"], "error")
        self.assertFalse(gate.is_set())
        self.assertEqual(session.state.visited_relic_ids, ("a",))
        self.assertEqual(len(session.state.guide_records), 1)
        gate.set()

    async def test_global_background_limit_does_not_queue_foreground_requests(self):
        router = self.make_router(prefetch_concurrency=1)
        self.create("one", router=router)
        self.create("two", router=router)
        first_gate = self.block(session_id="one")
        second_gate = self.block(session_id="two")
        await self.start(router=router, session_id="one")
        await self.spin_until(lambda: self.count("b", "one") == 1)
        await asyncio.wait_for(self.start(router=router, session_id="two"), timeout=1)
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertEqual(self.count("b", "two"), 0)
        self.interpreter.return_value = InputAnalysis(question="为什么？")
        response = await asyncio.wait_for(router.dispatch_chat("one", "为什么？"), timeout=1)
        self.assertEqual(response["reply_content"]["status"], "answer_ready")
        first_gate.set()
        await self.spin_until(lambda: self.count("b", "two") == 1)
        second_gate.set()
        await self.wait_status("ready", router=router, session_id="two")

    async def test_queued_prefetch_is_promoted_when_user_starts_next(self):
        router = self.make_router(prefetch_concurrency=1)
        self.create("one", router=router)
        second_session = self.create("two", router=router)
        first_gate = self.block(session_id="one")
        second_gate = self.block(session_id="two")
        await self.start(router=router, session_id="one")
        await self.spin_until(lambda: self.count("b", "one") == 1)
        await self.start(router=router, session_id="two")
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertEqual(self.count("b", "two"), 0)
        waiting = asyncio.create_task(self.start("b", router=router, session_id="two"))
        await self.spin_until(lambda: self.count("b", "two") == 1)
        self.assertFalse(first_gate.is_set())
        second_gate.set()
        await asyncio.wait_for(waiting, timeout=1)
        self.assertEqual(second_session.state.visited_relic_ids, ("a", "b"))
        first_gate.set()
        await self.wait_status("ready", router=router, session_id="one")
        self.assertEqual(self.count("b", "two"), 1)

    async def test_changed_exit_invalidates_cache_even_when_current_location_is_unchanged(self):
        self.create()
        await self.start()
        await self.wait_status("ready")
        response = await self.router.dispatch_action("s", "update_location", {
            "current_node_id": "entrance", "exit_node_id": "g",
        })
        self.assertTrue(response["reply_content"]["route_dirty"])
        self.assertEqual(response["reply_content"]["guide_prefetch"]["status"], "idle")
        self.assertEqual(self.count("b"), 1)
        await self.router.dispatch_action("s", "replan")
        await self.wait_status("ready")
        self.assertEqual(self.count("b"), 2)

    async def test_prefetch_can_be_disabled_for_custom_services(self):
        router = self.make_router(prefetch_enabled=False)
        self.create(router=router)
        await self.start(router=router)
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertEqual(self.count("b"), 0)
        response = await self.state(router=router)
        self.assertEqual(response["reply_content"]["guide_prefetch"]["status"], "idle")


if __name__ == "__main__":
    unittest.main()
