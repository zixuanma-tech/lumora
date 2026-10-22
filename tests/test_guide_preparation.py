"""准备稿与正式提交的边界；预生成不能提前形成参观记录。"""

from dataclasses import FrozenInstanceError, replace
import unittest
from unittest.mock import AsyncMock

from guide_generator import build_guide_request, commit_prepared_guide, prepare_guide
from guide_models import GuideDraft, GuideMaterial, GuideReview, GuideSelection, PreparedGuide
from session_state import GuideRecord, SessionState, VisitorInfo


class GuidePreparationTests(unittest.IsolatedAsyncioTestCase):
    def state(self, session_id="s"):
        state = SessionState(VisitorInfo(
            content_interests=("青铜器",), guide_styles=("通俗",), remaining_minutes=30,
        ), session_id=session_id)
        state.replace_pending_queue(("a", "b"))
        return state

    async def prepare(self, state, **changes):
        request = build_guide_request(state)
        selection = GuideSelection(request.relic_id, (
            GuideMaterial("m", "有来源的材料。", ("src",)),
        ))
        options = {
            "selector": AsyncMock(return_value=selection),
            "composer": AsyncMock(return_value=GuideDraft(request.relic_id, ("讲解内容。",), ("m",))),
            "review_mode": "integrated",
        }
        options.update(changes)
        return await prepare_guide(request, **options)

    async def test_preparation_is_immutable_and_does_not_commit_any_state(self):
        state = self.state()
        original = state.to_dict()
        prepared = await self.prepare(state)
        self.assertEqual(state.to_dict(), original)
        self.assertIsInstance(prepared, PreparedGuide)
        self.assertIsNone(prepared.review)
        self.assertFalse(hasattr(prepared, "guide_id"))
        self.assertFalse(hasattr(prepared, "guide_record"))
        with self.assertRaises(FrozenInstanceError):
            prepared.review_mode = "independent"

    async def test_commit_allows_natural_time_change_and_submits_exactly_once(self):
        state = self.state()
        prepared = await self.prepare(state)
        state.update_visitor_info(remaining_minutes=29.5)
        result = commit_prepared_guide(state, prepared)
        self.assertEqual(state.visited_relic_ids, ("a",))
        self.assertEqual(tuple(stop.relic_id for stop in state.pending_queue), ("b",))
        self.assertEqual(state.guide_records, (result.guide_record,))
        self.assertEqual(result.guide_context.completed_segments, 0)
        self.assertEqual(result.guide_context.guide_id, result.guide_record.guide_id)
        self.assertIs(result.request, prepared.request)
        self.assertEqual(state.visitor_info.remaining_minutes, 29.5)
        after_commit = state.to_dict()
        with self.assertRaises(ValueError):
            commit_prepared_guide(state, prepared)
        self.assertEqual(state.to_dict(), after_commit)

    async def test_invalid_commit_leaves_current_state_unchanged(self):
        for change in ("finished", "target", "history", "interest", "style", "focus", "needs", "session"):
            with self.subTest(change=change):
                state = self.state()
                prepared = await self.prepare(state)
                if change == "finished":
                    state.finish()
                elif change == "target":
                    state.skip_next()
                elif change == "history":
                    state.replace_pending_queue(("previous", "a", "b"))
                    state.complete_visit("previous", "先前讲解。")
                elif change == "session":
                    state = self.state("other-session")
                else:
                    name = {"interest": "content_interests", "style": "guide_styles",
                            "focus": "guide_focus", "needs": "other_needs"}[change]
                    state.update_visitor_info(**{name: ("已改变",)})
                before_commit = state.to_dict()
                with self.assertRaises(RuntimeError if change == "finished" else ValueError):
                    commit_prepared_guide(state, prepared)
                self.assertEqual(state.to_dict(), before_commit)

    async def test_pure_question_record_does_not_invalidate_next_prepared_guide(self):
        state = self.state()
        first = commit_prepared_guide(state, await self.prepare(state))
        prepared = await self.prepare(state)
        answer = GuideRecord("answer-a", "a", "answer", ("对当前文物的回答。",), "有什么特点？")
        state.record_guide(answer)
        second = commit_prepared_guide(state, prepared)
        self.assertEqual(state.visited_relic_ids, ("a", "b"))
        self.assertEqual(state.guide_records, (first.guide_record, answer, second.guide_record))

    async def test_optional_guard_runs_after_each_external_await(self):
        state = self.state()
        guards = []
        prepared = await self.prepare(
            state, review_mode="independent", reviewer=AsyncMock(return_value=GuideReview(True)),
            ensure_unchanged=lambda: guards.append("checked"),
        )
        self.assertEqual(guards, ["checked", "checked", "checked"])
        self.assertTrue(prepared.review.approved)
        original = state.to_dict()
        composer = AsyncMock()

        def stale():
            raise RuntimeError("过期准备请求")

        with self.assertRaisesRegex(RuntimeError, "过期准备请求"):
            await self.prepare(state, composer=composer, ensure_unchanged=stale)
        composer.assert_not_awaited()
        self.assertEqual(state.to_dict(), original)

    async def test_prepared_contract_rejects_invalid_target_references_or_review(self):
        prepared = await self.prepare(self.state())
        invalid_changes = (
            {"selection": GuideSelection("other", prepared.selection.materials)},
            {"draft": GuideDraft("other", ("稿件。",), ("m",))},
            {"draft": GuideDraft("a", ("稿件。",), ("unknown",))},
            {"review_mode": "independent"},
            {"review_mode": "independent", "review": GuideReview(False, ("无依据",))},
            {"review": GuideReview(True)},
        )
        for changes in invalid_changes:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(prepared, **changes)


if __name__ == "__main__":
    unittest.main()
