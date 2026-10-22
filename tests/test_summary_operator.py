"""手记的取材范围、反馈、输出引用及异步边界测试；不调用真实模型。"""

import json
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock

from operators import op_generate_summary
from session_state import GuideRecord, SessionState, VisitorInfo
from summary_models import (
    SUMMARY_QUESTIONS, SummaryDraft, SummaryFeedback, SummaryInsight,
    SummaryQuestion, SummaryRelic, SummaryRequest, SummaryReview, SummaryStop,
)


class SummaryOperatorTests(unittest.IsolatedAsyncioTestCase):
    def make_state(self):
        state = SessionState(VisitorInfo(
            content_interests=["制作工艺"], guide_styles=["通俗"],
            guide_focus=["材料"], remaining_minutes=45,
        ), session_id="visit")
        state.replace_pending_queue(["a", "skip", "b", "pending"])
        state.record_guide(GuideRecord("a-main", "a", "guide", ("A 的工艺。", "A 的用途。")))
        state.record_guide(GuideRecord("a-qa", "a", "answer", ("A 的回答。",), "A 怎么制作？"))
        state.skip_next()
        state.record_guide(GuideRecord("b-main", "b", "guide", ("B 的工艺。",)))
        state.record_guide(GuideRecord("b-qa", "b", "answer", ("B 的回答。",), "B 为什么这样制作？"))
        return state

    def services(self):
        source = type("Source", (), {})()
        # 故意逆序返回资料，算子必须恢复为本次请求的顺序。
        source.load_relics = AsyncMock(return_value=(
            SummaryRelic("b", "展品乙"), SummaryRelic("a", "展品甲", "唐", "工艺厅"),
        ))
        draft = SummaryDraft(
            "从材料读懂工艺", "这次围绕工艺展开探索。",
            (SummaryStop("a", "甲的回顾。"), SummaryStop("b", "乙的回顾。")),
            (SummaryInsight("工艺发现", "讲解中的工艺。", ("a",), ("a-main", "a-qa")),),
            (SummaryInsight("共同线索", "甲乙之间的联系。", ("a", "b"), ("a-main", "b-main")),),
            (SummaryQuestion("b-qa", "简短回答。"),), "下次继续了解材料。",
        )
        return source, AsyncMock(return_value=draft), AsyncMock(return_value=SummaryReview(True))

    async def generate(self, state, services, **kwargs):
        source, composer, reviewer = services
        return await op_generate_summary(
            state, data_source=source, composer=composer, reviewer=reviewer, **kwargs,
        )

    async def test_full_history_and_feedback_produce_json_without_mutating_state(self):
        state, services = self.make_state(), self.services()
        before = state.to_dict()
        feedback = SummaryFeedback(" a ", "  原文感想\n第二行。  ")
        result = await self.generate(state, services, feedback=feedback)
        request = result.request
        self.assertEqual(state.to_dict(), before)
        self.assertEqual(result.status, "generated")
        self.assertEqual(request.visited_relic_ids, ("a", "b"))
        self.assertEqual(request.guide_records, state.guide_records)
        self.assertIs(request.visitor_info, state.visitor_info)
        self.assertEqual(tuple(item.relic_id for item in request.relics), ("a", "b"))
        services[0].load_relics.assert_awaited_once_with(("a", "b"))
        services[1].assert_awaited_once_with(request=request)
        services[2].assert_awaited_once_with(request=request, draft=result.draft)
        output = json.loads(json.dumps(result.to_dict(), ensure_ascii=False, allow_nan=False))
        self.assertEqual(output["feedback"], {
            "favorite_relic_id": "a", "reflection": "  原文感想\n第二行。  ",
        })
        self.assertEqual(output["trail"][0]["name"], "展品甲")
        self.assertEqual(output["questions"][0], {
            "guide_id": "b-qa", "relic_id": "b", "relic_name": "展品乙",
            "question": "B 为什么这样制作？", "answer_summary": "简短回答。",
        })
        self.assertNotIn("request", output)
        self.assertNotIn("review", output)

    async def test_finished_session_allows_repeated_generation(self):
        state = self.make_state()
        state.finish()
        before = state.to_dict()
        for _ in range(2):
            result = await self.generate(state, self.services())
            self.assertEqual(result.status, "generated")
        self.assertEqual(state.to_dict(), before)

    async def test_both_optional_answers_can_be_skipped(self):
        result = await self.generate(self.make_state(), self.services())
        self.assertEqual(result.request.feedback, SummaryFeedback())
        self.assertEqual(SummaryFeedback(reflection=" \n ").reflection, None)
        self.assertEqual(len(SUMMARY_QUESTIONS), 2)
        self.assertIn("感想", SUMMARY_QUESTIONS[1])

    async def test_empty_session_returns_empty_without_any_services(self):
        for finished in (False, True):
            state = SessionState()
            state.replace_pending_queue(["pending"])
            if finished:
                state.finish()
            before = state.to_dict()
            services = self.services()
            result = await self.generate(state, services, feedback=SummaryFeedback(reflection="一次感想。"))
            self.assertEqual(result.status, "empty")
            self.assertIsNone(result.draft)
            self.assertEqual(result.to_dict()["trail"], [])
            self.assertEqual(result.to_dict()["feedback"]["reflection"], "一次感想。")
            self.assertEqual(state.to_dict(), before)
            for service in (services[0].load_relics, *services[1:]):
                service.assert_not_awaited()
        self.assertEqual((await op_generate_summary(SessionState())).status, "empty")

    async def test_pending_question_is_available_without_becoming_a_visit(self):
        state = self.make_state()
        state.record_guide(GuideRecord("pending-qa", "pending", "answer", ("已生成回答。",), "队首问题？"))
        services = self.services()
        services[0].load_relics.return_value += (SummaryRelic("pending", "待看展品"),)
        services[1].return_value = replace(
            services[1].return_value, questions=(SummaryQuestion("pending-qa", "回答摘要。"),),
        )
        result = await self.generate(state, services)
        self.assertEqual(result.request.guide_records, state.guide_records)
        services[0].load_relics.assert_awaited_once_with(("a", "b", "pending"))
        self.assertEqual(tuple(item.relic_id for item in result.draft.trail), ("a", "b"))
        self.assertEqual(result.to_dict()["questions"][0]["question"], "队首问题？")
        self.assertEqual(result.to_dict()["relics"][-1]["name"], "待看展品")

    async def test_question_only_session_can_be_summarized(self):
        state = SessionState()
        state.replace_pending_queue(["a"])
        state.record_guide(GuideRecord("qa-only", "a", "answer", ("回答。",), "这是什么？"))
        services = self.services()
        services[0].load_relics.return_value = (SummaryRelic("a", "甲"),)
        services[1].return_value = SummaryDraft(
            "一次小探索", "从问题开始。", (),
            (SummaryInsight("发现", "回答的提炼。", ("a",), ("qa-only",)),),
            questions=(SummaryQuestion("qa-only", "摘要。"),),
        )
        result = await self.generate(state, services)
        self.assertEqual(result.status, "generated")
        self.assertEqual(result.to_dict()["trail"], [])

    async def test_invalid_feedback_is_rejected_before_services(self):
        state = self.make_state()
        before = state.to_dict()
        for feedback in ({}, SummaryFeedback("pending"), SummaryFeedback("skip"), SummaryFeedback("unknown")):
            with self.subTest(feedback=feedback):
                services = self.services()
                with self.assertRaises(ValueError):
                    await self.generate(state, services, feedback=feedback)
                services[0].load_relics.assert_not_awaited()
                services[1].assert_not_awaited()
        self.assertEqual(state.to_dict(), before)

    async def test_missing_or_invalid_dependencies_are_rejected(self):
        state = self.make_state()
        before = state.to_dict()
        for name in ("composer", "reviewer"):
            services = self.services()
            params = dict(data_source=services[0], composer=services[1], reviewer=services[2])
            params[name] = None
            with self.subTest(name=name), self.assertRaises(NotImplementedError):
                await op_generate_summary(state, **params)
            services[0].load_relics.assert_not_awaited()
        for params in ({"composer": 1, "reviewer": AsyncMock()},
                       {"data_source": object(), "composer": AsyncMock(), "reviewer": AsyncMock()}):
            with self.assertRaises(ValueError):
                await op_generate_summary(state, **params)
        self.assertEqual(state.to_dict(), before)

    async def test_bad_metadata_stops_before_composition(self):
        for metadata in (
            {}, "text", ({} ,), (SummaryRelic("a", "甲"),),
            (SummaryRelic("a", "甲"), SummaryRelic("a", "重复")),
            (SummaryRelic("a", "甲"), SummaryRelic("b", "乙"), SummaryRelic("extra", "额外")),
        ):
            with self.subTest(metadata=metadata):
                state, services = self.make_state(), self.services()
                before = state.to_dict()
                services[0].load_relics.return_value = metadata
                with self.assertRaises(ValueError):
                    await self.generate(state, services)
                services[1].assert_not_awaited()
                services[2].assert_not_awaited()
                self.assertEqual(state.to_dict(), before)

    async def test_illegal_draft_references_and_trails_are_rejected(self):
        good = self.services()[1].return_value
        bad_drafts = (
            {}, replace(good, trail=good.trail[::-1]), replace(good, trail=good.trail[:1]),
            replace(good, trail=(good.trail[0], good.trail[0])),
            replace(good, trail=good.trail + (SummaryStop("pending", "待看"),)),
            replace(good, discoveries=(SummaryInsight("标题", "内容", ("skip",), ("a-main",)),)),
            replace(good, discoveries=(SummaryInsight("标题", "内容", ("a",), ("unknown",)),)),
            replace(good, discoveries=(SummaryInsight("标题", "内容", ("b",), ("a-main",)),)),
            replace(good, discoveries=(SummaryInsight("标题", "内容", ("a", "b"), ("a-main",)),)),
            replace(good, connections=(SummaryInsight("标题", "内容", ("a",), ("a-main",)),)),
            replace(good, questions=(SummaryQuestion("a-main", "主讲冒充回答"),)),
            replace(good, questions=(SummaryQuestion("unknown", "未知回答"),)),
            replace(good, questions=good.questions * 2),
        )
        for draft in bad_drafts:
            with self.subTest(draft=draft):
                state, services = self.make_state(), self.services()
                before = state.to_dict()
                services[1].return_value = draft
                with self.assertRaises(ValueError):
                    await self.generate(state, services)
                services[2].assert_not_awaited()
                self.assertEqual(state.to_dict(), before)

    async def test_no_questions_or_connections_are_required(self):
        services = self.services()
        services[1].return_value = replace(services[1].return_value, connections=(), questions=())
        result = await self.generate(self.make_state(), services)
        self.assertEqual(result.to_dict()["questions"], [])
        self.assertEqual(result.to_dict()["connections"], [])

    async def test_service_failures_leave_state_unchanged_and_stop_pipeline(self):
        for stage in range(3):
            state, services = self.make_state(), self.services()
            before = state.to_dict()
            mocks = (services[0].load_relics, *services[1:])
            mocks[stage].side_effect = RuntimeError("服务失败")
            with self.subTest(stage=stage), self.assertRaisesRegex(RuntimeError, "服务失败"):
                await self.generate(state, services)
            self.assertEqual(state.to_dict(), before)
            for later in mocks[stage + 1:]:
                later.assert_not_awaited()

    async def test_rejected_or_invalid_review_never_returns_a_handbook(self):
        for review in ({"approved": True}, SummaryReview(False, ("出现了讲解中没有的结论",)), SummaryReview(False)):
            state, services = self.make_state(), self.services()
            before = state.to_dict()
            services[2].return_value = review
            with self.subTest(review=review), self.assertRaises(ValueError):
                await self.generate(state, services)
            self.assertEqual(state.to_dict(), before)

    async def test_state_changes_during_every_stage_reject_stale_results(self):
        for stage in range(3):
            state, services = self.make_state(), self.services()
            mocks = (services[0].load_relics, *services[1:])
            output = mocks[stage].return_value
            visits, records, pending = state.visited_relics, state.guide_records, state.pending_queue

            async def change_state(*args, **kwargs):
                state.update_visitor_info(remaining_minutes=10)
                return output

            mocks[stage].side_effect = change_state
            with self.subTest(stage=stage), self.assertRaisesRegex(RuntimeError, "会话已变化"):
                await self.generate(state, services)
            self.assertIs(state.visited_relics, visits)
            self.assertIs(state.guide_records, records)
            self.assertIs(state.pending_queue, pending)
            self.assertEqual(state.visitor_info.remaining_minutes, 10)
            for later in mocks[stage + 1:]:
                later.assert_not_awaited()

    async def test_guide_log_changes_are_also_detected(self):
        state, services = self.make_state(), self.services()
        output = services[1].return_value

        async def append_answer(**kwargs):
            state.record_guide(GuideRecord("new-qa", "b", "answer", ("新回答。",), "新问题？"))
            return output

        services[1].side_effect = append_answer
        with self.assertRaisesRegex(RuntimeError, "会话已变化"):
            await self.generate(state, services)
        self.assertEqual(state.guide_records[-1].guide_id, "new-qa")
        services[2].assert_not_awaited()


class SummaryContractTests(unittest.TestCase):
    def test_request_and_draft_copy_external_lists(self):
        state = SessionState()
        state.replace_pending_queue(["a"])
        state.complete_visit("a", "主讲内容。")
        visits, records, relics = list(state.visited_relics), list(state.guide_records), [SummaryRelic("a", "甲")]
        request = SummaryRequest(state.session_id, state.visitor_info, visits, records, relics)
        visits.clear()
        records.clear()
        relics.clear()
        self.assertEqual(request.visited_relic_ids, ("a",))
        self.assertEqual(len(request.guide_records), 1)
        insight_ids, guide_ids = ["a"], [request.guide_records[0].guide_id]
        insight = SummaryInsight("发现", "内容", insight_ids, guide_ids)
        insight_ids.clear()
        guide_ids.clear()
        self.assertEqual(insight.relic_ids, ("a",))
        self.assertEqual(len(insight.guide_ids), 1)

    def test_invalid_contracts_fail_before_model_use(self):
        for create in (
            lambda: SummaryFeedback(reflection=3), lambda: SummaryFeedback(" "),
            lambda: SummaryRelic("a", " "), lambda: SummaryRelic("a", "甲", era=[]),
            lambda: SummaryInsight("发现", "内容", (), ("g",)),
            lambda: SummaryInsight("发现", "内容", ("a", "a"), ("g",)),
            lambda: SummaryDraft("标题", "开篇", (), ()),
            lambda: SummaryReview(1), lambda: SummaryReview(True, ("未解决",)),
        ):
            with self.subTest(create=create), self.assertRaises(ValueError):
                create()


if __name__ == "__main__":
    unittest.main()
