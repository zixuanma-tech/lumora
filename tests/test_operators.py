"""输入算子的分流、校验和状态边界测试；无需模型或网络。"""

import unittest
from unittest.mock import AsyncMock

from operators import GuideContext, InputAnalysis, op_process_input
from session_state import SessionState, VisitorInfo


class ProcessInputTests(unittest.IsolatedAsyncioTestCase):
    def make_state(self):
        state = SessionState(VisitorInfo(
            content_interests=["青铜器"], guide_styles=["通俗"], remaining_minutes=45,
        ))
        state.replace_pending_queue(["a", "b", "c"])
        state.complete_visit("a", "这是一件青铜器，表面有铭文。")
        return state

    async def test_structured_input_updates_only_provided_fields_without_interpreter(self):
        state = self.make_state()
        interpreter = AsyncMock()
        visits, queue = state.visited_relics, state.pending_queue
        result = await op_process_input(state, {
            "guide_styles": [" 简洁 ", "简洁"], "remaining_minutes": 20,
        }, interpreter=interpreter)
        interpreter.assert_not_awaited()
        self.assertEqual(state.visitor_info.content_interests, ("青铜器",))
        self.assertEqual(state.visitor_info.guide_styles, ("简洁",))
        self.assertEqual(result.changed_fields, ("guide_styles", "remaining_minutes"))
        self.assertEqual(result.visitor_info, state.visitor_info)
        self.assertIs(state.visited_relics, visits)
        self.assertIs(state.pending_queue, queue)

    async def test_explicit_clear_and_zero_time_keep_distinct_meanings(self):
        state = self.make_state()
        await op_process_input(state, {"remaining_minutes": 0})
        self.assertEqual(state.visitor_info.remaining_minutes, 0)
        await op_process_input(state, {"content_interests": [], "remaining_minutes": None})
        self.assertEqual(state.visitor_info.content_interests, ())
        self.assertIsNone(state.visitor_info.remaining_minutes)
        self.assertEqual(state.visitor_info.guide_styles, ("通俗",))

    async def test_natural_requirements_receive_context_and_preserve_other_fields(self):
        state = self.make_state()
        original_info = state.visitor_info
        interpreter = AsyncMock(return_value=InputAnalysis(
            visitor_info_changes={"content_interests": ["青铜器", "陶瓷"]},
        ))
        result = await op_process_input(state, "  也想看看陶瓷  ", interpreter=interpreter)
        interpreter.assert_awaited_once_with(
            "也想看看陶瓷", visitor_info=original_info, current_visit=state.visited_relics[-1],
            guide_context=None,
        )
        self.assertEqual(state.visitor_info.content_interests, ("青铜器", "陶瓷"))
        self.assertEqual(state.visitor_info.remaining_minutes, 45)
        self.assertEqual(result.changed_fields, ("content_interests",))
        self.assertIsNone(result.question)

    async def test_current_question_does_not_update_visitor_info_or_route(self):
        state = self.make_state()
        original = state.to_dict()
        interpreter = AsyncMock(return_value=InputAnalysis(question=" 铭文写的是什么？ "))
        result = await op_process_input(state, "上面的字写的是什么？", interpreter=interpreter)
        self.assertEqual(result.question, "铭文写的是什么？")
        self.assertEqual(result.current_relic_id, "a")
        self.assertFalse(result.needs_current_relic)
        self.assertEqual(result.changed_fields, ())
        self.assertEqual(state.to_dict(), original)

    async def test_mixed_input_updates_info_and_preserves_question(self):
        state = self.make_state()
        visits, queue = state.visited_relics, state.pending_queue
        interpreter = AsyncMock(return_value=InputAnalysis(
            visitor_info_changes={"guide_styles": ["简洁"]},
            question="这件文物的铭文是什么意思？",
        ))
        result = await op_process_input(
            state, "以后讲短一点，这件文物上的字是什么意思？", interpreter=interpreter,
        )
        self.assertEqual(result.visitor_info.guide_styles, ("简洁",))
        self.assertEqual(result.changed_fields, ("guide_styles",))
        self.assertEqual(result.question, "这件文物的铭文是什么意思？")
        self.assertEqual(result.current_relic_id, "a")
        self.assertEqual(state.visitor_info.content_interests, ("青铜器",))
        self.assertIs(state.visited_relics, visits)
        self.assertIs(state.pending_queue, queue)

    async def test_question_without_current_relic_is_retained_for_clarification(self):
        state = SessionState()
        state.replace_pending_queue(["a"])
        original = state.to_dict()
        interpreter = AsyncMock(return_value=InputAnalysis(question="上面的字是什么？"))
        result = await op_process_input(state, "上面的字是什么？", interpreter=interpreter)
        self.assertTrue(result.needs_current_relic)
        self.assertIsNone(result.current_relic_id)
        self.assertEqual(result.question, "上面的字是什么？")
        self.assertEqual(state.to_dict(), original)
        self.assertIsNone(interpreter.call_args.kwargs["current_visit"])

    async def test_explicit_replan_works_without_information_changes(self):
        state = self.make_state()
        original = state.to_dict()
        interpreter = AsyncMock(return_value=InputAnalysis(replan_requested=True))
        result = await op_process_input(state, "换一条路线", interpreter=interpreter)
        self.assertTrue(result.replan_requested)
        self.assertEqual(result.changed_fields, ())
        self.assertEqual(state.to_dict(), original)

    async def test_finish_intent_is_returned_without_finishing_state(self):
        state = self.make_state()
        original = state.to_dict()
        interpreter = AsyncMock(return_value=InputAnalysis(finish_requested=True))
        result = await op_process_input(state, "今天先逛到这里", interpreter=interpreter)
        self.assertTrue(result.finish_requested)
        self.assertEqual(state.to_dict(), original)

    async def test_invalid_analysis_does_not_partially_update_state(self):
        invalid_results = [
            {},
            InputAnalysis(visitor_info_changes={"unknown": 1}),
            InputAnalysis(visitor_info_changes={"remaining_minutes": -1}),
            InputAnalysis(visitor_info_changes={"guide_styles": "简洁"}),
            InputAnalysis(visitor_info_changes={"guide_styles": ["简洁"]}, question=" "),
            InputAnalysis(visitor_info_changes={"guide_styles": ["简洁"]}, replan_requested=1),
            InputAnalysis(visitor_info_changes={"guide_styles": ["简洁"]}, finish_requested="是"),
        ]
        for analysis in invalid_results:
            with self.subTest(analysis=analysis):
                state = self.make_state()
                original = state.to_dict()
                with self.assertRaises(ValueError):
                    await op_process_input(state, "输入", interpreter=AsyncMock(return_value=analysis))
                self.assertEqual(state.to_dict(), original)

    async def test_invalid_structured_input_is_atomic(self):
        for changes in (
            {"guide_styles": ["简洁"], "remaining_minutes": float("nan")},
            {"guide_styles": ["简洁"], "remaining_minutes": True},
            {"guide_styles": ["简洁"], "action": "skip"},
        ):
            with self.subTest(changes=changes):
                state = self.make_state()
                original = state.to_dict()
                with self.assertRaises(ValueError):
                    await op_process_input(state, changes)
                self.assertEqual(state.to_dict(), original)

    async def test_interpreter_failure_or_missing_implementation_does_not_update_state(self):
        state = self.make_state()
        original = state.to_dict()
        with self.assertRaises(NotImplementedError):
            await op_process_input(state, "讲短一点")
        with self.assertRaises(RuntimeError):
            await op_process_input(state, "讲短一点", interpreter=AsyncMock(side_effect=RuntimeError("失败")))
        self.assertEqual(state.to_dict(), original)

    async def test_same_information_and_unrecognized_text_can_return_no_actions(self):
        state = self.make_state()
        result = await op_process_input(state, {"guide_styles": [" 通俗 ", "通俗"]})
        self.assertEqual(result.changed_fields, ())
        result = await op_process_input(state, "你好", interpreter=AsyncMock(return_value=InputAnalysis()))
        self.assertEqual(result.changed_fields, ())
        self.assertIsNone(result.question)
        self.assertFalse(result.replan_requested)
        self.assertFalse(result.finish_requested)

    async def test_empty_unsupported_and_finished_inputs_are_rejected_before_interpretation(self):
        state = self.make_state()
        interpreter = AsyncMock()
        original = state.to_dict()
        for user_input in (" ", None, 20, ["青铜器"]):
            with self.subTest(user_input=user_input), self.assertRaises(ValueError):
                await op_process_input(state, user_input, interpreter=interpreter)
        self.assertEqual(state.to_dict(), original)
        state.finish()
        finished = state.to_dict()
        with self.assertRaises(RuntimeError):
            await op_process_input(state, "讲短一点", interpreter=interpreter)
        with self.assertRaises(RuntimeError):
            await op_process_input(state, {"guide_styles": ["简洁"]})
        self.assertEqual(state.to_dict(), finished)
        interpreter.assert_not_awaited()


class GuideContextTests(unittest.TestCase):
    def test_playback_cursor_splits_confirmed_prefix_and_remaining_script(self):
        segments = ["第一句。", "第二句。", "第三句。"]
        context = GuideContext("session", "a", "guide-v1", segments, completed_segments=1)
        segments[0] = "外部修改"
        self.assertEqual(context.segments, ("第一句。", "第二句。", "第三句。"))
        self.assertEqual(context.played_segments, ("第一句。",))
        self.assertEqual(context.remaining_segments, ("第二句。", "第三句。"))

    def test_playback_can_be_at_start_or_end_without_assuming_generation_finished(self):
        context = GuideContext("session", "a", "guide-v1", ("第一句。",))
        self.assertEqual(context.played_segments, ())
        self.assertEqual(context.remaining_segments, ("第一句。",))
        context = GuideContext("session", "a", "guide-v1", ("第一句。",), completed_segments=1)
        self.assertEqual(context.remaining_segments, ())
        context = GuideContext(
            "session", "a", "guide-v1", ("第一句。",),
            completed_segments=1, generation_complete=False,
        )
        self.assertEqual(context.remaining_segments, ())
        self.assertFalse(context.generation_complete)
        context = GuideContext("session", "a", "guide-v1", (), generation_complete=False)
        self.assertEqual(context.segments, ())

    def test_invalid_script_and_cursor_are_rejected(self):
        for changes in (
            {"session_id": " "}, {"relic_id": ""}, {"guide_id": ""},
            {"segments": "第一句。"}, {"segments": [""]}, {"segments": [1]},
            {"segments": ()}, {"completed_segments": -1}, {"completed_segments": 2},
            {"completed_segments": True}, {"completed_segments": 0.5},
            {"generation_complete": 1},
        ):
            with self.subTest(changes=changes):
                values = dict(session_id="session", relic_id="a", guide_id="guide-v1", segments=("第一句。",))
                values.update(changes)
                with self.assertRaises(ValueError):
                    GuideContext(**values)


class InterruptedGuideInputTests(unittest.IsolatedAsyncioTestCase):
    def make_state(self):
        state = SessionState()
        state.replace_pending_queue(["a", "b", "c"])
        state.complete_visit("a", "上一件展品的完整讲解。")
        return state

    async def test_interrupted_pending_stop_is_question_target_instead_of_previous_visit(self):
        state = self.make_state()
        original = state.to_dict()
        context = GuideContext(
            state.session_id, "b", "guide-b-v1", ("介绍外观。", "介绍铭文。", "介绍用途。"),
            completed_segments=1,
        )
        interpreter = AsyncMock(return_value=InputAnalysis(question="上面的字是什么？"))
        result = await op_process_input(state, "上面的字是什么？", interpreter=interpreter, guide_context=context)
        self.assertEqual(result.current_relic_id, "b")
        self.assertFalse(result.needs_current_relic)
        self.assertIs(result.guide_context, context)
        self.assertEqual(result.guide_context.remaining_segments, ("介绍铭文。", "介绍用途。"))
        interpreter.assert_awaited_once_with(
            "上面的字是什么？", visitor_info=state.visitor_info, current_visit=None, guide_context=context,
        )
        self.assertEqual(state.to_dict(), original)

    async def test_first_stop_can_be_interrupted_before_any_visit_record_exists(self):
        state = SessionState()
        state.replace_pending_queue(["a"])
        context = GuideContext(state.session_id, "a", "guide-a-v1", ("介绍。",))
        interpreter = AsyncMock(return_value=InputAnalysis(question="它有什么用途？"))
        result = await op_process_input(state, "它有什么用途？", interpreter=interpreter, guide_context=context)
        self.assertEqual(result.current_relic_id, "a")
        self.assertFalse(result.needs_current_relic)
        self.assertEqual(state.visited_relics, ())

    async def test_generated_and_recorded_guide_can_still_be_partly_played(self):
        state = self.make_state()
        context = GuideContext(
            state.session_id, "a", "guide-a-v1", ("第一句。", "第二句。"), completed_segments=1,
        )
        interpreter = AsyncMock(return_value=InputAnalysis(question="为什么？"))
        result = await op_process_input(state, "为什么？", interpreter=interpreter, guide_context=context)
        self.assertEqual(result.current_relic_id, "a")
        self.assertEqual(result.guide_context.remaining_segments, ("第二句。",))
        self.assertIs(interpreter.call_args.kwargs["current_visit"], state.visited_relics[-1])

    async def test_mixed_input_updates_info_without_altering_interrupted_script(self):
        state = self.make_state()
        context = GuideContext(state.session_id, "b", "guide-b-v1", ("第一句。", "第二句。"), completed_segments=1)
        interpreter = AsyncMock(return_value=InputAnalysis(
            visitor_info_changes={"guide_styles": ["简洁"]}, question="铭文是什么意思？",
        ))
        result = await op_process_input(
            state, "以后讲短一点，铭文是什么意思？", interpreter=interpreter, guide_context=context,
        )
        self.assertEqual(result.visitor_info.guide_styles, ("简洁",))
        self.assertEqual(result.changed_fields, ("guide_styles",))
        self.assertIs(result.guide_context, context)
        self.assertEqual(context.completed_segments, 1)
        self.assertEqual(context.segments, ("第一句。", "第二句。"))
        self.assertEqual(state.visited_relic_ids, ("a",))
        self.assertEqual(state.pending_queue[0].relic_id, "b")

    async def test_context_is_forwarded_even_for_information_change_without_question(self):
        state = self.make_state()
        context = GuideContext(state.session_id, "b", "guide-b-v1", ("第一句。",))
        result = await op_process_input(state, {"guide_styles": ["简洁"]}, guide_context=context)
        self.assertIsNone(result.question)
        self.assertIs(result.guide_context, context)

    async def test_unrelated_context_is_rejected_before_analysis_or_information_update(self):
        state = self.make_state()
        original = state.to_dict()
        interpreter = AsyncMock(return_value=InputAnalysis(visitor_info_changes={"guide_styles": ["简洁"]}))
        for context in (
            GuideContext("another-session", "b", "guide-b-v1", ("第一句。",)),
            GuideContext(state.session_id, "c", "guide-c-v1", ("第一句。",)),
            {"relic_id": "b"},
        ):
            with self.subTest(context=context), self.assertRaises(ValueError):
                await op_process_input(state, "讲短一点", interpreter=interpreter, guide_context=context)
            with self.subTest(context=context), self.assertRaises(ValueError):
                await op_process_input(state, {"guide_styles": ["简洁"]}, guide_context=context)
        interpreter.assert_not_awaited()
        self.assertEqual(state.to_dict(), original)


if __name__ == "__main__":
    unittest.main()
