"""讲解记录的保存、恢复与参观更新边界；无需模型或网络。"""

from dataclasses import FrozenInstanceError
import json
import unittest

from session_state import GuideRecord, RouteStop, SessionState


class SessionGuideHistoryTests(unittest.TestCase):
    def make_state(self):
        state = SessionState(session_id="session")
        state.replace_pending_queue([RouteStop("a", "推荐理由", "路线提示"), "b", "c"])
        return state

    def test_record_copies_segments_without_changing_text_or_boundaries(self):
        segments = [" 第一段。 ", "第二段。"]
        record = GuideRecord(" version-1 ", " a ", "guide", segments)
        segments[0] = "外部修改"
        self.assertEqual(record.guide_id, "version-1")
        self.assertEqual(record.relic_id, "a")
        self.assertEqual(record.segments, (" 第一段。 ", "第二段。"))
        self.assertEqual(record.guide_text, " 第一段。 第二段。")
        with self.assertRaises(FrozenInstanceError):
            record.question = "外部修改"

    def test_main_commits_log_and_visit_together_then_answer_only_appends_log(self):
        state = self.make_state()
        main = GuideRecord("main", "a", "guide", ("原稿一。", "原稿二。"))
        visit = state.record_guide(main)
        self.assertEqual(state.guide_records, (main,))
        self.assertEqual(visit.guide_text, main.guide_text)
        self.assertEqual(visit.recommendation_reason, "推荐理由")
        self.assertEqual(visit.connection_from_previous, "路线提示")
        self.assertEqual(state.visited_relic_ids, ("a",))
        visits, queue = state.visited_relics, state.pending_queue
        answer = GuideRecord("answer", "a", "answer", ("回答。",), " 为什么？ ")
        self.assertIsNone(state.record_guide(answer))
        self.assertEqual(state.guide_records, (main, answer))
        self.assertEqual(answer.question, "为什么？")
        self.assertIs(state.visited_relics, visits)
        self.assertIs(state.pending_queue, queue)
        self.assertEqual(state.current_relic_id, "a")
        self.assertIs(state.get_guide_record(" main "), main)
        with self.assertRaises(KeyError):
            state.get_guide_record("unknown")

    def test_answer_for_interrupted_head_can_precede_main_without_advancing_route(self):
        state = self.make_state()
        original_queue = state.pending_queue
        answer = GuideRecord("answer-first", "a", "answer", ("回答。",), "用途？")
        self.assertIsNone(state.record_guide(answer))
        self.assertIs(state.pending_queue, original_queue)
        self.assertEqual(state.visited_relics, ())
        state.record_guide(GuideRecord("main", "a", "guide", ("主讲。",)))
        self.assertEqual(tuple(record.kind for record in state.guide_records), ("answer", "guide"))
        restored = SessionState.from_dict(state.to_dict())
        self.assertEqual(restored.to_dict(), state.to_dict())

    def test_invalid_commit_and_duplicate_version_leave_every_field_unchanged(self):
        state = self.make_state()
        main = GuideRecord("main", "a", "guide", ("原稿。",))
        state.record_guide(main)
        original = state.to_dict()
        for record in (
            main,
            GuideRecord("main", "a", "answer", ("新回答。",), "重复 ID？"),
            GuideRecord("wrong-main", "c", "guide", ("非队首主讲。",)),
            GuideRecord("wrong-answer", "c", "answer", ("不相关回答。",), "问题？"),
            {},
        ):
            with self.subTest(record=record), self.assertRaises(ValueError):
                state.record_guide(record)
            self.assertEqual(state.to_dict(), original)
        state.finish()
        finished = state.to_dict()
        with self.assertRaises(RuntimeError):
            state.record_guide(GuideRecord("late", "a", "answer", ("回答。",), "问题？"))
        self.assertEqual(state.to_dict(), finished)

    def test_json_snapshot_roundtrip_is_independent_and_preserves_records_after_finish(self):
        state = self.make_state()
        state.record_guide(GuideRecord("main", "a", "guide", ("第一段。", "第二段。")))
        state.record_guide(GuideRecord("answer", "a", "answer", ("解答。",), "为什么？"))
        state.finish()
        snapshot = json.loads(json.dumps(state.to_dict(), ensure_ascii=False, allow_nan=False))
        restored = SessionState.from_dict(snapshot)
        self.assertEqual(restored.to_dict(), state.to_dict())
        snapshot["guide_records"][0]["segments"][0] = "外部修改"
        self.assertEqual(restored.guide_records[0].segments, ("第一段。", "第二段。"))
        self.assertEqual(restored.status, "finished")

    def test_legacy_snapshot_restores_main_text_without_inventing_answers(self):
        state = self.make_state()
        state.complete_visit("a", "旧主讲。")
        snapshot = state.to_dict()
        snapshot.pop("guide_records")
        restored = SessionState.from_dict(snapshot)
        self.assertEqual(restored.visited_relics, state.visited_relics)
        self.assertEqual(len(restored.guide_records), 1)
        self.assertEqual(restored.guide_records[0].kind, "guide")
        self.assertEqual(restored.guide_records[0].segments, ("旧主讲。",))
        self.assertIsNone(restored.guide_records[0].question)

    def test_invalid_record_shape_or_question_is_rejected(self):
        for changes in (
            {"guide_id": " "}, {"relic_id": " "}, {"kind": "other"},
            {"segments": ()}, {"segments": "正文"}, {"segments": [" "]},
            {"segments": [1]}, {"kind": "answer"},
            {"kind": "answer", "question": " "},
            {"kind": "answer", "question": 1}, {"question": "不应出现在主讲"},
        ):
            values = dict(guide_id="id", relic_id="a", kind="guide", segments=("正文。",))
            values.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                GuideRecord(**values)

    def test_snapshot_rejects_duplicate_versions_or_main_visit_disagreement(self):
        state = self.make_state()
        state.complete_visit("a", "主讲。")
        base = state.to_dict()
        for mutate in (
            lambda snapshot: snapshot["guide_records"].append(dict(snapshot["guide_records"][0])),
            lambda snapshot: snapshot["guide_records"][0].update(relic_id="other"),
            lambda snapshot: snapshot["guide_records"][0].update(segments=["篡改正文。"]),
            lambda snapshot: snapshot.update(guide_records=[]),
        ):
            snapshot = json.loads(json.dumps(base))
            mutate(snapshot)
            with self.subTest(snapshot=snapshot), self.assertRaises(ValueError):
                SessionState.from_dict(snapshot)


if __name__ == "__main__":
    unittest.main()
