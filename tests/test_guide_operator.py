"""第三算子回归测试；只使用本地替身，不需要模型、知识库或网络。"""

import json
import unittest
from unittest.mock import AsyncMock

from guide_generator import op_generate_guide
from guide_models import (
    GuideContext, GuideDraft, GuideMaterial, GuideRequest, GuideReview, GuideSelection,
)
from session_state import GuideRecord, RouteStop, SessionState, VisitorInfo


class GenerateGuideTests(unittest.IsolatedAsyncioTestCase):
    def make_state(self):
        state = SessionState(VisitorInfo(
            content_interests=["制作工艺"], guide_styles=["通俗"], remaining_minutes=45,
        ))
        state.replace_pending_queue([
            "a", "skip", RouteStop("c", "适合工艺兴趣", "这只是规划提示"), "d",
        ])
        state.complete_visit("a", "历史讲稿全文不应进入新文物的请求。")
        state.skip_next(expected_relic_id="skip")
        return state

    def services(self, relic_id="c"):
        selection = GuideSelection(relic_id, (
            GuideMaterial("fact", "经选材服务提供的材料。", ("馆藏来源:1",)),
        ))
        draft = GuideDraft(relic_id, ("第一句。", "第二句。"), ("fact",))
        review = GuideReview(True)
        return (
            AsyncMock(return_value=selection), AsyncMock(return_value=draft),
            AsyncMock(return_value=review),
        )

    async def generate(self, state, services, **kwargs):
        selector, composer, reviewer = services
        return await op_generate_guide(
            state, selector=selector, composer=composer, reviewer=reviewer, **kwargs,
        )

    async def test_success_passes_history_ids_to_all_stages_then_commits_once(self):
        state = self.make_state()
        previous_visits = state.visited_relics
        services = self.services()
        result = await self.generate(state, services, relic_id=" c ")
        selector, composer, reviewer = services
        request = result.request
        self.assertEqual(request.visited_relic_ids, ("a",))
        self.assertEqual(request.connection_candidates, ("a",))
        self.assertIs(request.visitor_info, state.visitor_info)
        self.assertIsNone(request.current_guide_text)
        self.assertIsNone(request.guide_context)
        selector.assert_awaited_once_with(request=request)
        composer.assert_awaited_once_with(request=request, selection=result.selection)
        reviewer.assert_awaited_once_with(
            request=request, selection=result.selection, draft=result.draft,
        )
        self.assertEqual(state.visited_relic_ids, ("a", "c"))
        self.assertIs(state.visited_relics[0], previous_visits[0])
        self.assertEqual(tuple(stop.relic_id for stop in state.pending_queue), ("d",))
        self.assertIs(result.visit_record, state.visited_relics[-1])
        self.assertEqual(result.visit_record.recommendation_reason, "适合工艺兴趣")
        self.assertEqual(result.guide_text, "第一句。第二句。")
        self.assertEqual(result.guide_context.segments, result.draft.segments)
        self.assertEqual(result.guide_context.completed_segments, 0)
        self.assertEqual(result.guide_context.session_id, state.session_id)
        self.assertIs(result.guide_record, state.guide_records[-1])
        self.assertEqual(result.guide_record.guide_id, result.guide_context.guide_id)
        self.assertEqual(result.guide_record.kind, "guide")
        self.assertEqual(result.guide_record.segments, result.draft.segments)
        self.assertIsNone(result.guide_record.question)
        self.assertIsNone(result.resume_context)
        self.assertNotIn("历史讲稿全文", json.dumps(result.to_dict(), ensure_ascii=False, allow_nan=False))

    async def test_first_guide_passes_empty_history(self):
        state = SessionState()
        state.replace_pending_queue(["c"])
        result = await self.generate(state, self.services())
        self.assertEqual(result.request.visited_relic_ids, ())
        self.assertEqual(result.request.connection_candidates, ())
        self.assertEqual(state.visited_relic_ids, ("c",))

    async def test_history_keeps_visit_order_and_excludes_skipped_pending_and_target(self):
        state = SessionState()
        state.replace_pending_queue(["a", "b", "skip", "c", "d"])
        state.complete_visit("a", "A 的讲稿")
        state.complete_visit("b", "B 的讲稿")
        state.skip_next()
        result = await self.generate(state, self.services())
        self.assertEqual(result.request.visited_relic_ids, ("a", "b"))
        self.assertEqual(result.request.connection_candidates, ("b", "a"))
        self.assertEqual(result.request.visited_relic_ids, ("a", "b"))
        self.assertEqual(state.visited_relic_ids, ("a", "b", "c"))

    async def test_interrupted_pending_guide_question_preserves_original_and_resume_context(self):
        state = self.make_state()
        original = state.to_dict()
        context = GuideContext(
            state.session_id, "c", "original-c", ("介绍外观。", "介绍用途。"),
            completed_segments=1,
        )
        result = await self.generate(
            state, self.services(), question=" 有什么用途？ ", guide_context=context,
        )
        self.assertEqual(result.request.relic_id, "c")
        self.assertEqual(result.request.question, "有什么用途？")
        self.assertEqual(result.request.visited_relic_ids, ("a",))
        self.assertIs(result.request.guide_context, context)
        self.assertIsNone(result.request.current_guide_text)
        self.assertIs(result.resume_context, context)
        self.assertIsNone(result.visit_record)
        self.assertNotEqual(result.guide_context.guide_id, context.guide_id)
        self.assertEqual(result.resume_context.remaining_segments, ("介绍用途。",))
        self.assertEqual(result.guide_record.kind, "answer")
        self.assertEqual(result.guide_record.question, "有什么用途？")
        self.assertIs(result.guide_record, state.guide_records[-1])
        updated = state.to_dict()
        self.assertEqual(updated.pop("guide_records")[:-1], original.pop("guide_records"))
        self.assertEqual(updated, original)

    async def test_question_without_playback_context_uses_current_guide_only(self):
        state = self.make_state()
        original = state.to_dict()
        result = await self.generate(state, self.services("a"), question="为什么？")
        self.assertEqual(result.request.relic_id, "a")
        self.assertEqual(result.request.visited_relic_ids, ("a",))
        self.assertEqual(result.request.connection_candidates, ())
        self.assertEqual(result.request.current_guide_text, state.visited_relics[-1].guide_text)
        self.assertIsNone(result.visit_record)
        self.assertEqual(result.guide_record.question, "为什么？")
        updated = state.to_dict()
        self.assertEqual(updated.pop("guide_records")[:-1], original.pop("guide_records"))
        self.assertEqual(updated, original)

    async def test_main_and_repeated_answers_are_preserved_in_generation_order(self):
        state = SessionState()
        state.replace_pending_queue(["c", "d"])
        main = await self.generate(state, self.services())
        original_visit, pending = state.visited_relics, state.pending_queue
        first = await self.generate(
            state, self.services(), question="用途是什么？", guide_context=main.guide_context,
        )
        second = await self.generate(state, self.services(), question="为什么这样制作？")
        self.assertEqual(state.guide_records, (
            main.guide_record, first.guide_record, second.guide_record,
        ))
        self.assertEqual(tuple(record.kind for record in state.guide_records), ("guide", "answer", "answer"))
        self.assertEqual(tuple(record.question for record in state.guide_records), (
            None, "用途是什么？", "为什么这样制作？",
        ))
        self.assertEqual(len({record.guide_id for record in state.guide_records}), 3)
        self.assertIs(state.visited_relics, original_visit)
        self.assertIs(state.pending_queue, pending)
        self.assertEqual(second.request.current_guide_text, main.guide_text)
        self.assertIs(state.get_guide_record(main.guide_context.guide_id), main.guide_record)
        self.assertIs(first.resume_context, main.guide_context)
        encoded = json.dumps(state.to_dict(), ensure_ascii=False, allow_nan=False)
        restored = SessionState.from_dict(json.loads(encoded))
        self.assertEqual(restored.guide_records, state.guide_records)

    async def test_failed_answers_do_not_append_a_record_or_change_visit(self):
        for failing_stage in range(4):
            with self.subTest(failing_stage=failing_stage):
                state = self.make_state()
                original = state.to_dict()
                services = self.services("a")
                if failing_stage < 3:
                    services[failing_stage].side_effect = RuntimeError("回答失败")
                    error = RuntimeError
                else:
                    services[2].return_value = GuideReview(False, ("回答无依据",))
                    error = ValueError
                with self.assertRaises(error):
                    await self.generate(state, services, question="为什么？")
                self.assertEqual(state.to_dict(), original)

    async def test_history_change_during_generation_prevents_stale_commit(self):
        state = self.make_state()
        services = self.services()
        selection = services[0].return_value
        independent = GuideRecord("parallel-answer", "a", "answer", ("独立回答。",), "已有问题？")

        async def append_answer(**kwargs):
            state.record_guide(independent)
            return selection

        services[0].side_effect = append_answer
        visits, pending, history = state.visited_relics, state.pending_queue, state.guide_records
        with self.assertRaisesRegex(RuntimeError, "会话已变化"):
            await self.generate(state, services)
        self.assertEqual(state.guide_records, history + (independent,))
        self.assertIs(state.visited_relics, visits)
        self.assertIs(state.pending_queue, pending)
        services[1].assert_not_awaited()
        services[2].assert_not_awaited()

    async def test_question_does_not_guess_target_from_pending_queue(self):
        state = SessionState()
        state.replace_pending_queue(["c"])
        original = state.to_dict()
        services = self.services()
        with self.assertRaises(ValueError):
            await self.generate(state, services, question="这是什么？")
        for service in services:
            service.assert_not_awaited()
        self.assertEqual(state.to_dict(), original)

    async def test_invalid_targets_and_contexts_are_rejected_before_services(self):
        state = self.make_state()
        original = state.to_dict()
        for kwargs in (
            {"relic_id": "d"}, {"relic_id": " "}, {"question": " "},
            {"question": 1}, {"question": "为什么？", "relic_id": "c"},
            {"guide_context": GuideContext(state.session_id, "c", "c-v1", ("稿件。",))},
            {"question": "为什么？", "guide_context": {"relic_id": "c"}},
            {"question": "为什么？", "guide_context": GuideContext("other", "c", "c-v1", ("稿件。",))},
            {"question": "为什么？", "guide_context": GuideContext(state.session_id, "d", "d-v1", ("稿件。",))},
        ):
            with self.subTest(kwargs=kwargs):
                services = self.services()
                with self.assertRaises(ValueError):
                    await self.generate(state, services, **kwargs)
                for service in services:
                    service.assert_not_awaited()
                self.assertEqual(state.to_dict(), original)

    async def test_service_exceptions_leave_state_unchanged_and_stop_pipeline(self):
        for failing_stage in range(3):
            with self.subTest(failing_stage=failing_stage):
                state = self.make_state()
                original = state.to_dict()
                services = self.services()
                services[failing_stage].side_effect = RuntimeError("服务失败")
                with self.assertRaisesRegex(RuntimeError, "服务失败"):
                    await self.generate(state, services)
                self.assertEqual(state.to_dict(), original)
                for service in services[failing_stage + 1:]:
                    service.assert_not_awaited()

    async def test_invalid_service_responses_do_not_commit_or_call_later_stages(self):
        invalid_outputs = (
            (0, {}),
            (0, GuideSelection("other", (GuideMaterial("fact", "材料", ("来源",)),))),
            (1, {}),
            (1, GuideDraft("other", ("稿件。",), ("fact",))),
            (1, GuideDraft("c", ("稿件。",), ("not-selected",))),
            (2, {"approved": True}),
        )
        for stage, output in invalid_outputs:
            with self.subTest(stage=stage, output=output):
                state = self.make_state()
                original = state.to_dict()
                services = self.services()
                services[stage].return_value = output
                with self.assertRaises(ValueError):
                    await self.generate(state, services)
                self.assertEqual(state.to_dict(), original)
                for service in services[stage + 1:]:
                    service.assert_not_awaited()

    async def test_review_rejection_keeps_history_and_pending_queue(self):
        state = self.make_state()
        original = state.to_dict()
        services = self.services()
        services[2].return_value = GuideReview(False, ("材料不能支持结论",))
        with self.assertRaisesRegex(ValueError, "材料不能支持结论"):
            await self.generate(state, services)
        self.assertEqual(state.to_dict(), original)

    async def test_missing_adapter_does_not_use_a_default_approval(self):
        state = self.make_state()
        original = state.to_dict()
        for missing_stage in range(3):
            with self.subTest(missing_stage=missing_stage):
                services = list(self.services())
                services[missing_stage] = None
                with self.assertRaises(NotImplementedError):
                    await self.generate(state, services)
                for service in services:
                    if service is not None:
                        service.assert_not_awaited()
        self.assertEqual(state.to_dict(), original)

    async def test_empty_queue_and_finished_session_are_rejected_before_services(self):
        for finished in (False, True):
            with self.subTest(finished=finished):
                state = self.make_state() if finished else SessionState()
                if finished:
                    state.finish()
                original = state.to_dict()
                services = self.services()
                with self.assertRaises(RuntimeError if finished else ValueError):
                    await self.generate(state, services)
                for service in services:
                    service.assert_not_awaited()
                self.assertEqual(state.to_dict(), original)

    async def test_state_change_during_any_stage_prevents_stale_commit(self):
        for changing_stage in range(3):
            with self.subTest(changing_stage=changing_stage):
                state = self.make_state()
                previous_visits, pending_queue = state.visited_relics, state.pending_queue
                services = self.services()
                output = services[changing_stage].return_value

                async def change_state(**kwargs):
                    state.update_visitor_info(remaining_minutes=10)
                    return output

                services[changing_stage].side_effect = change_state
                with self.assertRaisesRegex(RuntimeError, "会话已变化"):
                    await self.generate(state, services)
                self.assertIs(state.visited_relics, previous_visits)
                self.assertIs(state.pending_queue, pending_queue)
                self.assertEqual(state.visitor_info.remaining_minutes, 10)
                for service in services[changing_stage + 1:]:
                    service.assert_not_awaited()

    async def test_integrated_generation_commits_without_fabricating_independent_review(self):
        state = self.make_state()
        selector, composer, _ = self.services()
        result = await op_generate_guide(
            state, selector=selector, composer=composer, review_mode="integrated",
        )
        selector.assert_awaited_once_with(request=result.request)
        composer.assert_awaited_once_with(request=result.request, selection=result.selection)
        self.assertIsNone(result.review)
        self.assertEqual(result.review_mode, "integrated")
        self.assertIsNone(result.to_dict()["review"])
        self.assertEqual(result.to_dict()["review_mode"], "integrated")
        self.assertEqual(state.visited_relic_ids, ("a", "c"))
        self.assertEqual(tuple(stop.relic_id for stop in state.pending_queue), ("d",))
        self.assertEqual(state.guide_records[-1], result.guide_record)
        self.assertEqual(result.guide_context.completed_segments, 0)

    async def test_integrated_answer_preserves_main_and_resume_context(self):
        state = self.make_state()
        previous_records = state.guide_records
        selector, composer, _ = self.services()
        main = await op_generate_guide(
            state, selector=selector, composer=composer, review_mode="integrated",
        )
        visits, pending = state.visited_relics, state.pending_queue
        answer_selector, answer_composer, _ = self.services()
        answer = await op_generate_guide(
            state, selector=answer_selector, composer=answer_composer,
            review_mode="integrated", question="有什么用途？", guide_context=main.guide_context,
        )
        self.assertIs(answer.resume_context, main.guide_context)
        self.assertIs(state.visited_relics, visits)
        self.assertIs(state.pending_queue, pending)
        self.assertEqual(state.guide_records, previous_records + (main.guide_record, answer.guide_record))
        self.assertEqual(answer.guide_record.kind, "answer")
        self.assertEqual(answer.guide_record.question, "有什么用途？")
        self.assertIsNone(answer.review)

    async def test_integrated_errors_and_invalid_drafts_do_not_commit(self):
        for failure in ("loader_error", "composer_error", "wrong_target", "unknown_material"):
            with self.subTest(failure=failure):
                state = self.make_state()
                original = state.to_dict()
                selector, composer, _ = self.services()
                if failure == "loader_error":
                    selector.side_effect = RuntimeError("加载失败")
                elif failure == "composer_error":
                    composer.side_effect = RuntimeError("生成失败")
                elif failure == "wrong_target":
                    composer.return_value = GuideDraft("other", ("稿件。",), ("fact",))
                else:
                    composer.return_value = GuideDraft("c", ("稿件。",), ("unknown",))
                expected = RuntimeError if failure.endswith("error") else ValueError
                with self.assertRaises(expected):
                    await op_generate_guide(
                        state, selector=selector, composer=composer, review_mode="integrated",
                    )
                self.assertEqual(state.to_dict(), original)
                if failure == "loader_error":
                    composer.assert_not_awaited()

    async def test_integrated_state_changes_at_each_await_prevent_stale_commit(self):
        for changing_stage in range(2):
            with self.subTest(changing_stage=changing_stage):
                state = self.make_state()
                visits, pending, records = state.visited_relics, state.pending_queue, state.guide_records
                selector, composer, _ = self.services()
                services = (selector, composer)
                output = services[changing_stage].return_value

                async def change_state(**kwargs):
                    state.update_visitor_info(remaining_minutes=10)
                    return output

                services[changing_stage].side_effect = change_state
                with self.assertRaisesRegex(RuntimeError, "会话已变化"):
                    await op_generate_guide(
                        state, selector=selector, composer=composer, review_mode="integrated",
                    )
                self.assertIs(state.visited_relics, visits)
                self.assertIs(state.pending_queue, pending)
                self.assertIs(state.guide_records, records)
                self.assertEqual(state.visitor_info.remaining_minutes, 10)
                if changing_stage == 0:
                    composer.assert_not_awaited()

    async def test_integrated_requires_services_and_rejects_ambiguous_review_configuration(self):
        for configuration in ("missing_loader", "missing_composer", "invalid_mode", "reviewer_conflict"):
            with self.subTest(configuration=configuration):
                state = self.make_state()
                original = state.to_dict()
                selector, composer, reviewer = self.services()
                kwargs = {"selector": selector, "composer": composer, "review_mode": "integrated"}
                if configuration == "missing_loader":
                    kwargs["selector"] = None
                elif configuration == "missing_composer":
                    kwargs["composer"] = None
                elif configuration == "invalid_mode":
                    kwargs["review_mode"] = "unknown"
                else:
                    kwargs["reviewer"] = reviewer
                expected = NotImplementedError if configuration.startswith("missing") else ValueError
                with self.assertRaises(expected):
                    await op_generate_guide(state, **kwargs)
                for service in (selector, composer, reviewer):
                    service.assert_not_awaited()
                self.assertEqual(state.to_dict(), original)


class GuideContractTests(unittest.TestCase):
    def test_request_copies_history_and_prioritizes_other_recent_relics(self):
        ids = ["a", "b", "c"]
        request = GuideRequest("session", "c", VisitorInfo(), ids)
        ids.append("external")
        self.assertEqual(request.visited_relic_ids, ("a", "b", "c"))
        self.assertEqual(request.connection_candidates, ("b", "a"))

    def test_material_requires_sources_and_complete_draft_requires_material_ids(self):
        for create in (
            lambda: GuideMaterial("fact", "材料", ()),
            lambda: GuideMaterial("fact", "材料", (" ",)),
            lambda: GuideSelection("c", ()),
            lambda: GuideDraft("c", ("稿件。",), ()),
            lambda: GuideDraft("c", (), ("fact",)),
            lambda: GuideReview(True, ("未解决问题",)),
            lambda: GuideReview(1),
        ):
            with self.subTest(create=create), self.assertRaises(ValueError):
                create()


if __name__ == "__main__":
    unittest.main()
