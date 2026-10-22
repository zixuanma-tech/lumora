"""中央路由集成测试：真实算子配合本地数据和服务替身，无模型或网络。"""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from guide_models import GuideDraft, GuideMaterial, GuideReview, GuideSelection
from operators import InputAnalysis
from route_models import Exhibit, ExhibitTags, Gallery, InterestProfile, MuseumSnapshot, SpatialEdge
from router import Router, RouterServices, dispatch_action, dispatch_chat


class Clock:
    def __init__(self):
        self.seconds = 1000.0

    def __call__(self):
        return self.seconds

    def advance(self, seconds):
        self.seconds += seconds


class RouterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Clock()
        self.calls = []
        self.interpreter = AsyncMock(return_value=InputAnalysis())

        async def parse(*, visitor_info):
            self.calls.append("plan")
            return InterestProfile(target_categories=["陶瓷"])

        async def select(*, request):
            self.calls.append("answer" if request.question else "guide")
            return GuideSelection(request.relic_id, (
                GuideMaterial("fact", "本地测试材料。", ("馆藏来源:1",)),
            ))

        async def compose(*, request, selection):
            prefix = "回答" if request.question else "主讲"
            return GuideDraft(request.relic_id, (prefix + "第一句。", prefix + "第二句。"), ("fact",))

        self.parser = AsyncMock(side_effect=parse)
        self.vector = AsyncMock()
        self.vector.embed_query.return_value = [1, 0]
        self.selector = AsyncMock(side_effect=select)
        self.composer = AsyncMock(side_effect=compose)
        self.reviewer = AsyncMock(return_value=GuideReview(True))
        self.snapshot = MuseumSnapshot(
            [Gallery("g", "陶瓷厅", base_pass_min=1)],
            [Exhibit(name, name, "g", tags=ExhibitTags(category_main=["陶瓷"]),
                     vector=[1, 0], importance_level=4, coords=[index, 0])
             for index, name in enumerate(("a", "b", "c"), 1)],
            [SpatialEdge("entrance", "g", 1), SpatialEdge("g", "exit", 1)],
            {"entrance": 1, "exit": 1},
        )
        self.router = Router(RouterServices(
            interpreter=self.interpreter, snapshot=self.snapshot,
            interest_parser=self.parser, vector_service=self.vector,
            selector=self.selector, composer=self.composer, reviewer=self.reviewer,
        ), clock=self.clock, prefetch_enabled=False)

    async def asyncTearDown(self):
        await self.router.aclose()

    def create(self, session_id="s", *, minutes=60, locations=True):
        return self.router.create_session(
            {"content_interests": ["陶瓷"], "remaining_minutes": minutes},
            session_id=session_id,
            current_node_id="entrance" if locations else None,
            exit_node_id="exit" if locations else None,
        )

    async def planned(self, session_id="s", *, minutes=60):
        self.create(session_id, minutes=minutes)
        response = await self.router.dispatch_action(session_id, "replan")
        self.assertEqual(response["reply_content"]["status"], "planned")
        return response

    async def start(self, relic_id="a", session_id="s"):
        response = await self.router.dispatch_action(session_id, "start_guide", {"relic_id": relic_id})
        self.assertEqual(response["reply_content"]["status"], "guide_ready")
        return response

    def current(self, response):
        return response["reply_content"]["playback"]["current"]

    def assert_error(self, response, code):
        self.assertEqual(response["reply_content"]["status"], "error")
        self.assertEqual(response["reply_content"]["error"]["code"], code)

    async def test_structured_buttons_bypass_interpreter_and_return_serializable_snapshots(self):
        self.create(minutes=None)
        response = await dispatch_action(self.router, "s", "submit_preferences", {
            "visitor_info": {"remaining_minutes": 30, "guide_styles": ["通俗"]},
        })
        self.assertEqual(set(response), {"reply_content", "latest_state"})
        self.assertEqual(response["reply_content"]["steps"], ["visitor_info_updated", "route_planned"])
        self.assertEqual([row["relic_id"] for row in response["latest_state"]["pending_queue"]], ["a", "b", "c"])
        self.interpreter.assert_not_awaited()
        json.dumps(response, ensure_ascii=False, allow_nan=False)

    async def test_real_elapsed_time_is_deducted_once_and_explicit_budget_restarts_clock(self):
        await self.planned()
        self.clock.advance(120)
        response = await self.router.dispatch_action("s", "get_state")
        self.assertEqual(response["latest_state"]["visitor_info"]["remaining_minutes"], 58)
        response = await self.router.dispatch_action("s", "get_state")
        self.assertEqual(response["latest_state"]["visitor_info"]["remaining_minutes"], 58)
        self.clock.advance(60)
        response = await self.router.dispatch_action("s", "update_visitor_info", {
            "visitor_info": {"remaining_minutes": 20},
        })
        self.assertEqual(response["latest_state"]["visitor_info"]["remaining_minutes"], 20)
        self.clock.advance(90)
        response = await self.router.dispatch_action("s", "get_state")
        self.assertEqual(response["latest_state"]["visitor_info"]["remaining_minutes"], 18.5)

    async def test_unknown_time_stays_unknown_until_budget_is_provided(self):
        self.create(minutes=None)
        self.clock.advance(600)
        response = await self.router.dispatch_action("s", "get_state")
        self.assertIsNone(response["latest_state"]["visitor_info"]["remaining_minutes"])
        response = await self.router.dispatch_action("s", "update_visitor_info", {
            "visitor_info": {"remaining_minutes": 20},
        })
        self.assertEqual(response["latest_state"]["visitor_info"]["remaining_minutes"], 20)

    async def test_processing_elapsed_time_is_counted_but_new_budget_starts_after_input(self):
        await self.planned()

        async def slow_input(*args, **kwargs):
            self.clock.advance(120)
            return InputAnalysis(visitor_info_changes={"remaining_minutes": 30})

        self.interpreter.side_effect = slow_input
        response = await self.router.dispatch_chat("s", "现在还有半小时")
        self.assertEqual(response["latest_state"]["visitor_info"]["remaining_minutes"], 30)
        self.interpreter.side_effect = None
        self.interpreter.return_value = InputAnalysis()
        self.clock.advance(60)
        response = await self.router.dispatch_chat("s", "你好")
        self.assertEqual(response["latest_state"]["visitor_info"]["remaining_minutes"], 29)

    async def test_natural_language_reasserting_same_budget_restarts_time_after_analysis(self):
        await self.planned()

        async def slow_input(*args, **kwargs):
            self.clock.advance(120)
            return InputAnalysis(visitor_info_changes={"remaining_minutes": 60})

        self.interpreter.side_effect = slow_input
        response = await self.router.dispatch_chat("s", "从现在起我还有一小时")
        self.assertEqual(response["reply_content"]["input_result"]["changed_fields"], ())
        self.assertEqual(response["reply_content"]["input_result"]["updated_fields"], ("remaining_minutes",))
        self.assertEqual(response["latest_state"]["visitor_info"]["remaining_minutes"], 60)
        self.clock.advance(60)
        response = await self.router.dispatch_action("s", "get_state")
        self.assertEqual(response["latest_state"]["visitor_info"]["remaining_minutes"], 59)

    async def test_guide_processing_time_is_deducted_without_inferred_dwell_charge(self):
        await self.planned()
        original_compose = self.composer.side_effect

        async def slow_compose(*, request, selection):
            self.clock.advance(90)
            return await original_compose(request=request, selection=selection)

        self.composer.side_effect = slow_compose
        response = await self.start()
        self.assertEqual(response["latest_state"]["visitor_info"]["remaining_minutes"], 58.5)

    async def test_budget_expiry_clears_pending_without_finishing_or_moving_location(self):
        await self.planned(minutes=20)
        self.clock.advance(1200)
        response = await self.router.dispatch_action("s", "get_state")
        self.assertEqual(response["latest_state"]["visitor_info"]["remaining_minutes"], 0)
        self.assertEqual(response["latest_state"]["pending_queue"], [])
        self.assertEqual(response["latest_state"]["status"], "active")
        self.assertEqual(response["reply_content"]["current_node_id"], "entrance")
        self.assertEqual(response["reply_content"]["route"]["status"], "time_exhausted")

    async def test_missing_planning_inputs_are_reported_without_services(self):
        self.router.create_session(session_id="s")
        response = await self.router.dispatch_action("s", "replan")
        self.assertEqual(response["reply_content"]["status"], "needs_input")
        self.assertEqual(set(response["reply_content"]["missing_fields"]), {
            "remaining_minutes", "content_interests", "current_node_id", "exit_node_id",
        })
        self.parser.assert_not_awaited()

    async def test_mixed_chat_updates_then_answers_then_replans_with_current_history(self):
        await self.planned()
        initial = await self.start()
        self.calls.clear()
        self.interpreter.return_value = InputAnalysis(
            visitor_info_changes={"content_interests": ["制作工艺"], "guide_styles": ["简洁"]},
            question="它有什么用途？",
        )
        response = await dispatch_chat(self.router, "s", "讲短一点，也想看工艺，它有什么用途？", playback={
            "guide_id": self.current(initial)["guide_id"], "completed_segments": 1,
        })
        self.assertEqual(self.calls, ["answer", "plan"])
        self.assertEqual(response["reply_content"]["steps"], ["visitor_info_updated", "answered", "route_planned"])
        request = self.selector.call_args.kwargs["request"]
        self.assertEqual(request.visitor_info.guide_styles, ("简洁",))
        self.assertEqual(request.visitor_info.content_interests, ("制作工艺",))
        self.assertEqual(request.visited_relic_ids, ("a",))
        self.assertEqual(request.guide_context.completed_segments, 1)
        self.assertEqual([row["relic_id"] for row in response["latest_state"]["visited_relics"]], ["a"])
        self.assertEqual([row["kind"] for row in response["latest_state"]["guide_records"]], ["guide", "answer"])

    async def test_style_only_change_does_not_replan_and_question_without_current_is_retained(self):
        await self.planned()
        self.parser.reset_mock()
        response = await self.router.dispatch_action("s", "update_visitor_info", {
            "visitor_info": {"guide_styles": ["简洁"], "guide_focus": ["工艺"]},
        })
        self.assertFalse(response["reply_content"]["route_dirty"])
        self.parser.assert_not_awaited()
        self.interpreter.return_value = InputAnalysis(question="上面是什么？")
        response = await self.router.dispatch_chat("s", "上面是什么？")
        self.assertEqual(response["reply_content"]["status"], "needs_input")
        self.assertTrue(response["reply_content"]["needs_current_relic"])
        self.assertEqual(response["reply_content"]["question"], "上面是什么？")
        self.assertEqual(response["latest_state"]["guide_records"], [])
        self.selector.assert_not_awaited()

    async def test_stale_relic_button_is_rejected_and_skip_does_not_create_a_visit(self):
        await self.planned()
        response = await self.router.dispatch_action("s", "start_guide", {"relic_id": "b"})
        self.assert_error(response, "invalid_request")
        self.selector.assert_not_awaited()
        response = await self.router.dispatch_action("s", "skip", {"relic_id": "a"})
        self.assertEqual(response["latest_state"]["skipped_relics"], ["a"])
        self.assertEqual(response["latest_state"]["visited_relics"], [])
        response = await self.router.dispatch_action("s", "skip", {"relic_id": "a"})
        self.assert_error(response, "invalid_request")
        self.assertEqual(response["latest_state"]["pending_queue"][0]["relic_id"], "b")

    async def test_replan_uses_updated_actual_location_not_latest_generated_guide(self):
        await self.planned()
        await self.start()
        response = await self.router.dispatch_action("s", "get_state")
        self.assertEqual(response["reply_content"]["current_node_id"], "entrance")
        response = await self.router.dispatch_action("s", "update_location", {"current_node_id": "g"})
        self.assertEqual(response["reply_content"]["current_node_id"], "g")
        response = await self.router.dispatch_action("s", "replan")
        self.assertEqual(response["reply_content"]["route"]["start_node_id"], "g")
        self.assertFalse(response["reply_content"]["route_dirty"])
        self.assertEqual([row["relic_id"] for row in response["latest_state"]["visited_relics"]], ["a"])

    async def test_no_feasible_route_retains_old_queue_and_marks_it_unusable(self):
        planned = await self.planned()
        old_queue = planned["latest_state"]["pending_queue"]
        await self.router.dispatch_action("s", "update_location", {"current_node_id": "exit"})
        # From the exit there is no directed path back into the only gallery.
        # A zero-length exit route is valid and may contain no matching stops;
        # make the exit unreachable instead by using the entrance as destination.
        await self.router.dispatch_action("s", "update_location", {
            "current_node_id": "exit", "exit_node_id": "entrance",
        })
        response = await self.router.dispatch_action("s", "replan")
        self.assertEqual(response["reply_content"]["status"], "no_feasible_route")
        self.assertFalse(response["reply_content"]["plan_result"]["pending_queue_updated"])
        self.assertTrue(response["reply_content"]["route_dirty"])
        self.assertEqual(response["latest_state"]["pending_queue"], old_queue)
        response = await self.router.dispatch_action("s", "start_guide", {"relic_id": "a"})
        self.assert_error(response, "route_needs_replan")

    async def test_planning_failure_preserves_committed_preferences_and_old_queue(self):
        initial = await self.planned()
        self.parser.side_effect = RuntimeError("解析服务失败")
        response = await self.router.dispatch_action("s", "update_visitor_info", {
            "visitor_info": {"content_interests": ["制作工艺"]},
        })
        self.assert_error(response, "operator_failed")
        self.assertEqual(response["latest_state"]["visitor_info"]["content_interests"], ["制作工艺"])
        self.assertEqual(response["latest_state"]["pending_queue"], initial["latest_state"]["pending_queue"])
        self.assertTrue(response["reply_content"]["route_dirty"])
        self.assertEqual(response["reply_content"]["plan_result"]["status"], "error")

    async def test_playback_uses_server_script_and_rejects_unknown_backward_or_invalid_progress(self):
        await self.planned()
        response = await self.start()
        guide = self.current(response)
        response = await self.router.dispatch_action("s", "playback_progress", {
            "guide_id": guide["guide_id"], "completed_segments": 1,
        })
        self.assertEqual(self.current(response)["completed_segments"], 1)
        for values, code in (
            ({"guide_id": "old-version", "completed_segments": 2}, "stale_guide"),
            ({"guide_id": guide["guide_id"], "completed_segments": 0}, "invalid_request"),
            ({"guide_id": guide["guide_id"], "completed_segments": 3}, "invalid_request"),
            ({"guide_id": guide["guide_id"], "completed_segments": True}, "invalid_request"),
            ({"guide_id": guide["guide_id"], "completed_segments": 2, "segments": ["伪造原稿"]}, "invalid_request"),
        ):
            with self.subTest(values=values):
                response = await self.router.dispatch_action("s", "playback_progress", values)
                self.assert_error(response, code)
                self.assertEqual(self.current(response)["completed_segments"], 1)
                self.assertEqual(self.current(response)["segments"], guide["segments"])

    async def test_answer_completion_resumes_original_at_confirmed_progress_and_rejects_late_main_report(self):
        await self.planned()
        initial = await self.start()
        main = self.current(initial)
        self.interpreter.return_value = InputAnalysis(question="为什么？")
        response = await self.router.dispatch_chat("s", "为什么？", playback={
            "guide_id": main["guide_id"], "completed_segments": 1,
        })
        answer = self.current(response)
        self.assertNotEqual(answer["guide_id"], main["guide_id"])
        self.assertEqual(response["reply_content"]["playback"]["resume_stack"][0]["completed_segments"], 1)
        response = await self.router.dispatch_action("s", "playback_progress", {
            "guide_id": main["guide_id"], "completed_segments": 2,
        })
        self.assert_error(response, "stale_guide")
        response = await self.router.dispatch_action("s", "resume_guide", {
            "guide_id": answer["guide_id"], "completed_segments": 1,
        })
        self.assert_error(response, "answer_not_finished")
        response = await self.router.dispatch_action("s", "resume_guide", {
            "guide_id": answer["guide_id"], "completed_segments": 2,
        })
        self.assertEqual(self.current(response)["guide_id"], main["guide_id"])
        self.assertEqual(self.current(response)["completed_segments"], 1)
        self.assertEqual(self.current(response)["segments"], main["segments"])
        self.assertEqual(response["reply_content"]["playback"]["resume_stack"], [])
        self.assertEqual(len(response["latest_state"]["visited_relics"]), 1)

    async def test_nested_questions_restore_each_interrupted_version_in_order(self):
        await self.planned()
        main = self.current(await self.start())
        self.interpreter.return_value = InputAnalysis(question="为什么？")
        first = self.current(await self.router.dispatch_chat("s", "为什么？"))
        second = self.current(await self.router.dispatch_chat("s", "再解释一下？", playback={
            "guide_id": first["guide_id"], "completed_segments": 1,
        }))
        response = await self.router.dispatch_action("s", "resume_guide", {
            "guide_id": second["guide_id"], "completed_segments": 2,
        })
        self.assertEqual(self.current(response)["guide_id"], first["guide_id"])
        self.assertEqual(self.current(response)["completed_segments"], 1)
        response = await self.router.dispatch_action("s", "resume_guide", {
            "guide_id": first["guide_id"], "completed_segments": 2,
        })
        self.assertEqual(self.current(response)["guide_id"], main["guide_id"])
        self.assertEqual(len(response["latest_state"]["guide_records"]), 3)

    async def test_chat_failure_keeps_original_guide_paused_and_does_not_append_answer(self):
        await self.planned()
        initial = await self.start()
        self.interpreter.return_value = InputAnalysis(question="为什么？")
        self.reviewer.return_value = GuideReview(False, ("依据不足",))
        response = await self.router.dispatch_chat("s", "为什么？")
        self.assert_error(response, "invalid_request")
        self.assertEqual(self.current(response)["guide_id"], self.current(initial)["guide_id"])
        self.assertTrue(response["reply_content"]["playback"]["paused"])
        self.assertEqual(len(response["latest_state"]["guide_records"]), 1)

    async def test_finish_is_idempotent_freezes_budget_and_does_not_generate_summary(self):
        await self.planned()
        self.clock.advance(120)
        response = await self.router.dispatch_action("s", "finish")
        self.assertEqual(response["reply_content"]["status"], "finished")
        self.assertEqual(response["latest_state"]["visitor_info"]["remaining_minutes"], 58)
        self.clock.advance(600)
        response = await self.router.dispatch_action("s", "finish")
        self.assertEqual(response["latest_state"]["visitor_info"]["remaining_minutes"], 58)
        self.assertTrue(response["latest_state"]["pending_queue"])
        response = await self.router.dispatch_chat("s", "继续")
        self.assert_error(response, "session_finished")
        self.selector.assert_not_awaited()
        self.interpreter.assert_not_awaited()

    async def test_explicit_finish_intent_takes_priority_over_question_and_replan(self):
        await self.planned()
        await self.start()
        self.calls.clear()
        self.interpreter.return_value = InputAnalysis(
            question="为什么？", replan_requested=True, finish_requested=True,
        )
        response = await self.router.dispatch_chat("s", "最后这个问题，算了今天到这里")
        self.assertEqual(response["latest_state"]["status"], "finished")
        self.assertEqual(self.calls, [])
        self.assertEqual(len(response["latest_state"]["guide_records"]), 1)

    async def test_response_mutation_cannot_override_state_route_or_server_script(self):
        response = await self.planned()
        response["latest_state"]["pending_queue"].clear()
        response["reply_content"]["route"]["path_nodes"].clear()
        response = await self.start()
        original = self.current(response)["guide_id"]
        response["reply_content"]["playback"]["current"]["segments"] = ["伪造"]
        response["latest_state"]["guide_records"][0]["segments"][0] = "伪造"
        response = await self.router.dispatch_action("s", "get_state")
        self.assertEqual(self.current(response)["guide_id"], original)
        self.assertEqual(self.current(response)["segments"], ("主讲第一句。", "主讲第二句。"))
        self.assertEqual(response["latest_state"]["guide_records"][0]["segments"][0], "主讲第一句。")
        self.assertEqual(response["reply_content"]["route"]["path_nodes"], ["entrance", "g", "exit"])

    async def test_invalid_payload_and_missing_services_are_explicit(self):
        await self.planned()
        for action, payload in (
            ("start_guide", {"relic_id": "a", "state": {}}),
            ("update_visitor_info", {"visitor_info": {"remaining_minutes": -1}}),
            ("update_location", {"current_node_id": "unknown"}),
            ("unknown_action", {}),
        ):
            with self.subTest(action=action):
                response = await self.router.dispatch_action("s", action, payload)
                self.assert_error(response, "invalid_request")
                self.assertEqual(len(response["latest_state"]["pending_queue"]), 3)
        unavailable = Router(clock=self.clock)
        unavailable.create_session(session_id="u")
        response = await unavailable.dispatch_chat("u", "讲短一点")
        self.assert_error(response, "service_unavailable")

    async def test_sessions_are_isolated_and_unknown_or_duplicate_ids_are_rejected(self):
        await self.planned("one")
        self.create("two", minutes=30)
        await self.start(session_id="one")
        response = await self.router.dispatch_action("two", "get_state")
        self.assertEqual(response["latest_state"]["session_id"], "two")
        self.assertEqual(response["latest_state"]["guide_records"], [])
        self.assertEqual(response["latest_state"]["pending_queue"], [])
        self.assertIsNone(self.current(response))
        with self.assertRaises(ValueError):
            self.create("one")
        with self.assertRaises(KeyError):
            await self.router.dispatch_action("missing", "get_state")

    async def test_same_session_serializes_while_other_session_can_progress(self):
        await self.planned("one")
        self.create("two")
        entered, release = asyncio.Event(), asyncio.Event()
        original_compose = self.composer.side_effect

        async def blocking_compose(*, request, selection):
            entered.set()
            await release.wait()
            return await original_compose(request=request, selection=selection)

        self.composer.side_effect = blocking_compose
        first = asyncio.create_task(self.router.dispatch_action("one", "start_guide", {"relic_id": "a"}))
        await asyncio.wait_for(entered.wait(), 1)
        duplicate = asyncio.create_task(self.router.dispatch_action("one", "start_guide", {"relic_id": "a"}))
        other = await asyncio.wait_for(self.router.dispatch_action("two", "replan"), 1)
        self.assertEqual(other["reply_content"]["status"], "planned")
        self.assertFalse(duplicate.done())
        release.set()
        first_response, duplicate_response = await asyncio.gather(first, duplicate)
        self.assertEqual(first_response["reply_content"]["status"], "guide_ready")
        self.assert_error(duplicate_response, "invalid_request")
        self.assertEqual(len(duplicate_response["latest_state"]["guide_records"]), 1)


if __name__ == "__main__":
    unittest.main()
