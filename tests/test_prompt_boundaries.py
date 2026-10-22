"""两个新 Prompt 模块的严格输出边界及真实算子接入回归测试。"""

import copy
import json
import unittest

from guide_generator import op_generate_guide
from guide_models import GuideContext, GuideDraft, GuideMaterial, GuideRequest, GuideReview, GuideSelection
from guide_prompts import (
    GUIDE_REVIEWER_SYSTEM_PROMPT, build_guide_composer_messages,
    build_guide_reviewer_messages, make_guide_composer, make_guide_reviewer,
    make_guide_selector, parse_guide_draft, parse_guide_review, parse_guide_selection,
)
from session_state import GuideRecord, SessionState, VisitRecord, VisitorInfo
from summary_generator import op_generate_summary
from summary_models import SummaryFeedback, SummaryRelic, SummaryRequest, SummaryReview
from summary_prompts import (
    SUMMARY_COMPOSER_JSON_SCHEMA, SUMMARY_REVIEWER_SYSTEM_PROMPT,
    build_summary_composer_messages, build_summary_reviewer_messages,
    make_summary_composer, make_summary_reviewer, parse_summary_draft, parse_summary_review,
)


class PromptBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.guide_request = GuideRequest(
            "s1", "r1", VisitorInfo(content_interests=["纹饰"], guide_styles=["通俗"]),
            ("old",), question="纹饰？",
            guide_context=GuideContext("s1", "r1", "original", ("已播。", "未播。"), 1),
        )
        self.material = GuideMaterial("m1", "纹饰采用重复图案。", ("src-1",), ("old",))
        self.selection_json = {"relic_id": "r1", "materials": [{
            "material_id": "m1", "text": self.material.text,
            "source_refs": ["src-1"], "related_relic_ids": ["old"],
        }]}
        self.selection = GuideSelection("r1", (self.material,))
        self.guide_json = {"relic_id": "r1", "segments": [self.material.text], "used_material_ids": ["m1"]}
        self.guide_draft = GuideDraft("r1", (self.material.text,), ("m1",))
        self.summary_request = SummaryRequest(
            "s1", VisitorInfo(content_interests=["纹饰"]),
            (VisitRecord("r1", "纹饰主讲。"), VisitRecord("r2", "陶器主讲。")),
            (GuideRecord("g1", "r1", "guide", ("纹饰主讲。",)),
             GuideRecord("a1", "r1", "answer", ("纹饰回答。",), "纹饰？"),
             GuideRecord("g2", "r2", "guide", ("陶器主讲。",))),
            (SummaryRelic("r1", "青铜器"), SummaryRelic("r2", "陶器")),
            SummaryFeedback("r1", "觉得很有意思"),
        )
        self.summary_json = {
            "title": "参观手记", "introduction": "回顾本次参观。",
            "trail": [{"relic_id": "r1", "recap": "纹饰。"}, {"relic_id": "r2", "recap": "陶器。"}],
            "discoveries": [{"title": "纹饰", "text": "回顾纹饰。", "relic_ids": ["r1"], "guide_ids": ["g1"]}],
            "connections": [{"title": "对照", "text": "对照两段介绍。", "relic_ids": ["r1", "r2"], "guide_ids": ["g1", "g2"]}],
            "questions": [{"guide_id": "a1", "answer_summary": "纹饰回答。"}], "next_exploration": None,
        }

    def select(self, value):
        return parse_guide_selection(value, request=self.guide_request, candidates=[self.material])

    def guide(self, value):
        return parse_guide_draft(value, request=self.guide_request, selection=self.selection)

    def guide_review(self, value):
        return parse_guide_review(value, request=self.guide_request, selection=self.selection, draft=self.guide_draft)

    def summary(self, value):
        return parse_summary_draft(value, request=self.summary_request)

    def summary_review(self, value):
        return parse_summary_review(value, request=self.summary_request, draft=self.summary(self.summary_json))

    def parsers(self):
        return ((self.select, self.selection_json), (self.guide, self.guide_json),
                (self.guide_review, {"approved": True, "issues": []}),
                (self.summary, self.summary_json), (self.summary_review, {"approved": True, "issues": []}))

    def test_each_top_level_field_is_required_and_extra_fields_fail(self):
        for parser, good in self.parsers():
            for field in good:
                bad = dict(good)
                del bad[field]
                with self.subTest(parser=parser.__name__, missing=field), self.assertRaises(ValueError):
                    parser(bad)
            with self.subTest(parser=parser.__name__, extra=True), self.assertRaises(ValueError):
                parser({**good, "model_self_check_passed": True})

    def test_nested_fields_are_required_and_cannot_include_extra_data(self):
        for parser, good, arrays in ((self.select, self.selection_json, ("materials",)),
                                      (self.summary, self.summary_json, ("trail", "discoveries", "connections", "questions"))):
            for array in arrays:
                for field in good[array][0]:
                    bad = copy.deepcopy(good)
                    del bad[array][0][field]
                    with self.subTest(array=array, missing=field), self.assertRaises(ValueError):
                        parser(bad)
                bad = copy.deepcopy(good)
                bad[array][0]["extra"] = True
                with self.subTest(array=array, extra=True), self.assertRaises(ValueError):
                    parser(bad)

    def test_json_fences_and_common_model_wrappers_cannot_bypass_validation(self):
        for parser, good in self.parsers():
            encoded = json.dumps(good, ensure_ascii=False)
            self.assertEqual(parser("```json\n" + encoded + "\n```"), parser(good))
            invalid = (
                "```json\n" + json.dumps({**good, "extra": True}) + "\n```",
                {"content": encoded}, {"choices": [{"message": {"content": encoded}}]},
                "说明文字\n" + encoded, encoded + encoded, "[]", "null", "", 3,
            )
            for value in invalid:
                with self.subTest(parser=parser.__name__, value=value), self.assertRaises(ValueError):
                    parser(value)

    def test_duplicate_json_keys_fail_at_root_and_nested_levels(self):
        duplicates = (
            (self.select, json.dumps(self.selection_json).replace('"material_id": "m1"', '"material_id":"fake","material_id":"m1"')),
            (self.guide, json.dumps(self.guide_json).replace('"relic_id": "r1"', '"relic_id":"fake","relic_id":"r1"')),
            (self.guide_review, '{"approved":false,"approved":true,"issues":[]}'),
            (self.summary, json.dumps(self.summary_json).replace('"guide_id": "a1"', '"guide_id":"fake","guide_id":"a1"')),
            (self.summary_review, '{"approved":false,"approved":true,"issues":[]}'),
        )
        for parser, value in duplicates:
            with self.subTest(parser=parser.__name__), self.assertRaises(ValueError):
                parser(value)

    def test_non_json_constants_and_non_json_fences_fail(self):
        for parser, good in self.parsers():
            encoded = json.dumps(good)
            for value in ("```python\n" + encoded + "\n```", "```json\n" + encoded,
                          '{"approved":NaN,"issues":[]}', '{"approved":Infinity,"issues":[]}',
                          '{"approved":-Infinity,"issues":[]}'):
                with self.subTest(parser=parser.__name__, value=value), self.assertRaises(ValueError):
                    parser(value)

    def test_selector_cannot_change_sources_related_relics_or_types(self):
        for change in ({"source_refs": []}, {"source_refs": ["fake-source"]},
                       {"related_relic_ids": ["fake-relic"]}, {"source_refs": "src-1"},
                       {"text": 3}, {"material_id": False}, {"related_relic_ids": [False]}):
            bad = copy.deepcopy(self.selection_json)
            bad["materials"][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.select(bad)
        for value in ("other", 3, True, None):
            with self.subTest(relic_id=value), self.assertRaises(ValueError):
                self.select({**self.selection_json, "relic_id": value})
        for value in ([], "m1", [None], self.selection_json["materials"] * 2):
            with self.subTest(materials=value), self.assertRaises(ValueError):
                self.select({**self.selection_json, "materials": value})

    def test_guide_rejects_invalid_types_empty_segments_and_foreign_materials(self):
        for change in ({"relic_id": "other"}, {"relic_id": True}, {"segments": []},
                       {"segments": [" "]}, {"segments": "一句话。"}, {"segments": [False]},
                       {"used_material_ids": []}, {"used_material_ids": ["fake"]},
                       {"used_material_ids": "m1"}, {"used_material_ids": [1]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.guide({**self.guide_json, **change})

    def test_guide_segments_keep_original_text_and_playback_boundaries(self):
        segments = ["  第一句。\n", "第二句。  "]
        draft = self.guide({**self.guide_json, "segments": segments})
        self.assertEqual(draft.segments, tuple(segments))
        self.assertEqual(draft.guide_text, "".join(segments))

    def test_summary_rejects_wrong_scalar_array_and_reference_types(self):
        for change in ({"title": 1}, {"introduction": []}, {"title": " "}, {"trail": "r1"},
                       {"discoveries": {}}, {"connections": None}, {"questions": "a1"},
                       {"next_exploration": 1}, {"next_exploration": " "},
                       {"trail": [None]}, {"discoveries": [False]}, {"questions": [1]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.summary({**self.summary_json, **change})
        for array, field, value in (("trail", "recap", False), ("discoveries", "text", 3),
                                    ("discoveries", "relic_ids", "r1"), ("discoveries", "guide_ids", [False]),
                                    ("questions", "guide_id", []), ("questions", "answer_summary", 1)):
            bad = copy.deepcopy(self.summary_json)
            bad[array][0][field] = value
            with self.subTest(array=array, field=field), self.assertRaises(ValueError):
                self.summary(bad)

    def test_summary_discovery_scope_and_guide_relic_match_must_be_exact(self):
        for change in ({"relic_ids": ["fake"]}, {"guide_ids": ["fake"]}, {"relic_ids": ["r2"]},
                       {"guide_ids": ["g2"]}, {"relic_ids": ["r1", "r2"]},
                       {"guide_ids": ["g1", "g2"]}, {"relic_ids": []}, {"guide_ids": []},
                       {"relic_ids": ["r1", "r1"]}, {"guide_ids": ["g1", "g1"]}):
            insight = {**self.summary_json["discoveries"][0], **change}
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.summary({**self.summary_json, "discoveries": [insight]})
        insight = {**self.summary_json["discoveries"][0], "guide_ids": ["g1", "a1"]}
        self.assertEqual(self.summary({**self.summary_json, "discoveries": [insight]}).discoveries[0].guide_ids, ("g1", "a1"))

    def test_summary_connections_require_two_distinct_matching_relics(self):
        for relic_ids, guide_ids in ((["r1"], ["g1"]), (["r1", "r1"], ["g1"]),
                                     (["r1", "r2"], ["g1"]), (["r1", "r2"], ["g1", "a1"])):
            connection = {**self.summary_json["connections"][0], "relic_ids": relic_ids, "guide_ids": guide_ids}
            with self.subTest(connection=connection), self.assertRaises(ValueError):
                self.summary({**self.summary_json, "connections": [connection]})
        schema = SUMMARY_COMPOSER_JSON_SCHEMA["properties"]["connections"]["items"]
        self.assertEqual(schema["properties"]["relic_ids"]["minItems"], 2)

    def test_summary_questions_cannot_invent_answers_or_rewrite_original_question(self):
        for guide_id in ("g1", "fake"):
            with self.subTest(guide_id=guide_id), self.assertRaises(ValueError):
                self.summary({**self.summary_json, "questions": [{"guide_id": guide_id, "answer_summary": "摘要"}]})
        for extra in ("question", "original_question", "relic_id"):
            question = {**self.summary_json["questions"][0], extra: "模型改写"}
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.summary({**self.summary_json, "questions": [question]})

    def test_answer_only_summary_does_not_create_a_visit(self):
        request = SummaryRequest(
            "answer-only", VisitorInfo(), (), (self.summary_request.guide_records[1],),
            (self.summary_request.relics[0],),
        )
        answer_draft = {**self.summary_json, "trail": [], "connections": [],
            "discoveries": [{**self.summary_json["discoveries"][0], "guide_ids": ["a1"]}]}
        self.assertEqual(parse_summary_draft(answer_draft, request=request).trail, ())

    def test_reviewer_boolean_and_issues_match_existing_data_contract(self):
        for parser, contract in ((self.guide_review, GuideReview), (self.summary_review, SummaryReview)):
            for approved in (1, 0, "true", None):
                with self.subTest(parser=parser.__name__, approved=approved), self.assertRaises(ValueError):
                    parser({"approved": approved, "issues": []})
            for issues in ("问题", [1], [" "], None):
                with self.subTest(parser=parser.__name__, issues=issues), self.assertRaises(ValueError):
                    parser({"approved": False, "issues": issues})
            with self.subTest(parser=parser.__name__, inconsistent=True), self.assertRaises(ValueError):
                parser({"approved": True, "issues": ["问题尚未修复"]})
            # 数据类保持兼容；模型适配层要求拒绝结果携带可修复的具体问题。
            self.assertEqual(contract(False).issues, ())
            with self.subTest(parser=parser.__name__, missing_details=True), self.assertRaises(ValueError):
                parser({"approved": False, "issues": []})
            self.assertEqual(parser({"approved": False, "issues": ["补充材料依据"]}),
                             contract(False, ("补充材料依据",)))

    def test_composer_and_reviewer_messages_preserve_playback_original_and_evidence(self):
        compose = json.loads(build_guide_composer_messages(self.guide_request, self.selection)[1]["content"])
        review_messages = build_guide_reviewer_messages(self.guide_request, self.selection, self.guide_draft)
        review = json.loads(review_messages[1]["content"])
        self.assertEqual(review_messages[0]["content"], GUIDE_REVIEWER_SYSTEM_PROMPT)
        self.assertEqual(review["request"], compose["request"])
        self.assertEqual(review["selection"], self.selection_json)
        self.assertEqual(review["draft"], self.guide_json)
        self.assertEqual(review["request"]["guide_context"]["remaining_segments"], ["未播。"])
        summary_messages = build_summary_reviewer_messages(self.summary_request, self.summary(self.summary_json))
        summary = json.loads(summary_messages[1]["content"])
        self.assertEqual(summary_messages[0]["content"], SUMMARY_REVIEWER_SYSTEM_PROMPT)
        self.assertEqual(summary["draft"], self.summary_json)
        self.assertEqual(summary["request"]["feedback"]["reflection"], "觉得很有意思")
        self.assertEqual(summary["request"]["guide_records"][1]["question"], "纹饰？")
        self.assertEqual(summary["request"], json.loads(build_summary_composer_messages(self.summary_request)[1]["content"])["request"])


class PromptOperatorIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_selector_loads_candidates_for_each_request_before_model_call(self):
        calls = []

        async def load(*, request):
            calls.append(("load", request.relic_id))
            return [GuideMaterial("m-" + request.relic_id, request.relic_id + " 的材料。", ("source",))]

        async def model(**kwargs):
            payload = json.loads(kwargs["user_prompt"])
            relic_id = payload["request"]["relic_id"]
            calls.append(("model", relic_id))
            return {"relic_id": relic_id, "materials": payload["candidate_materials"]}

        select = make_guide_selector(model, load)
        for relic_id in ("r1", "r2"):
            result = await select(request=GuideRequest("s1", relic_id, VisitorInfo(), ()))
            self.assertEqual(result.materials[0].material_id, "m-" + relic_id)
        self.assertEqual(calls, [("load", "r1"), ("model", "r1"), ("load", "r2"), ("model", "r2")])

    async def test_guide_adapters_work_in_existing_operator_and_reviewer_can_reject(self):
        state = SessionState(session_id="integration")
        state.replace_pending_queue(["r1", "r2"])
        material = GuideMaterial("m1", "纹饰采用重复图案。", ("src-1",))
        calls = []

        async def model(**kwargs):
            name = kwargs["response_format"]["name"]
            calls.append(name)
            self.assertTrue(kwargs["response_format"]["strict"])
            self.assertIn("request", json.loads(kwargs["user_prompt"]))
            if name == "guide_selection":
                return {"relic_id": "r1", "materials": [{"material_id": "m1", "text": material.text,
                    "source_refs": ["src-1"], "related_relic_ids": []}]}
            if name == "guide_draft":
                return {"relic_id": "r1", "segments": [material.text], "used_material_ids": ["m1"]}
            return {"approved": True, "issues": []}

        services = {"selector": make_guide_selector(model, [material]), "composer": make_guide_composer(model),
                    "reviewer": make_guide_reviewer(model)}
        result = await op_generate_guide(state, **services)
        self.assertEqual(calls, ["guide_selection", "guide_draft", "guide_review"])
        self.assertEqual(state.visited_relic_ids, ("r1",))
        self.assertEqual(result.guide_text, material.text)
        snapshot = state.to_dict()

        async def reject(**kwargs):
            return {"approved": False, "issues": ["没有回答纹饰问题"]}

        services["reviewer"] = make_guide_reviewer(reject)
        with self.assertRaisesRegex(ValueError, "没有回答纹饰问题"):
            await op_generate_guide(state, question="纹饰含义？", **services)
        self.assertEqual(state.to_dict(), snapshot)

    async def test_summary_adapters_and_injected_source_work_in_existing_operator(self):
        state = SessionState(session_id="integration")
        state.replace_pending_queue(["r1"])
        state.record_guide(GuideRecord("g1", "r1", "guide", ("纹饰采用重复图案。",)))
        state.record_guide(GuideRecord("a1", "r1", "answer", ("纹饰重复。",), "纹饰？"))
        snapshot, calls = state.to_dict(), []

        class Source:
            async def load_relics(self, relic_ids):
                calls.append(("source", relic_ids))
                return (SummaryRelic("r1", "青铜器"),)

        async def compose(**kwargs):
            calls.append(("composer", kwargs["response_format"]["name"]))
            return {"title": "纹饰手记", "introduction": "回顾纹饰问题。",
                "trail": [{"relic_id": "r1", "recap": "介绍纹饰。"}],
                "discoveries": [{"title": "纹饰", "text": "纹饰采用重复图案。", "relic_ids": ["r1"], "guide_ids": ["g1"]}],
                "connections": [], "questions": [{"guide_id": "a1", "answer_summary": "纹饰重复。"}],
                "next_exploration": None}

        async def approve(**kwargs):
            calls.append(("reviewer", kwargs["response_format"]["name"]))
            return {"approved": True, "issues": []}

        services = {"data_source": Source(), "composer": make_summary_composer(compose), "reviewer": make_summary_reviewer(approve)}
        result = await op_generate_summary(state, **services)
        self.assertEqual(calls, [("source", ("r1",)), ("composer", "summary_draft"), ("reviewer", "summary_review")])
        self.assertEqual(result.to_dict()["questions"][0]["question"], "纹饰？")
        self.assertEqual(state.to_dict(), snapshot)

        async def reject(**kwargs):
            return {"approved": False, "issues": ["摘要超出原回答"]}

        services["reviewer"] = make_summary_reviewer(reject)
        with self.assertRaisesRegex(ValueError, "摘要超出原回答"):
            await op_generate_summary(state, **services)
        self.assertEqual(state.to_dict(), snapshot)

    async def test_invalid_model_output_and_model_failures_propagate(self):
        request = GuideRequest("s1", "r1", VisitorInfo(), ())
        selection = GuideSelection("r1", (GuideMaterial("m1", "材料。", ("来源",)),))

        async def invalid(**kwargs):
            return {"relic_id": "r1", "segments": ["稿件。"], "used_material_ids": ["fake"]}

        with self.assertRaises(ValueError):
            await make_guide_composer(invalid)(request=request, selection=selection)

        async def unavailable(**kwargs):
            raise RuntimeError("模型调用失败")

        with self.assertRaisesRegex(RuntimeError, "模型调用失败"):
            await make_guide_composer(unavailable)(request=request, selection=selection)


if __name__ == "__main__":
    unittest.main()
