import asyncio
import json
import unittest

from guide_models import GuideContext, GuideMaterial, GuideRequest, GuideSelection
from guide_prompts import (
    GUIDE_COMPOSER_JSON_SCHEMA, GUIDE_COMPOSER_SYSTEM_PROMPT,
    GUIDE_COMPOSER_WEB_DATA_MARKER, GUIDE_COMPOSER_WEB_SYSTEM_PROMPT,
    GUIDE_REVIEWER_JSON_SCHEMA, GUIDE_REVIEWER_SYSTEM_PROMPT,
    GUIDE_SELECTOR_JSON_SCHEMA, GUIDE_SELECTOR_SYSTEM_PROMPT,
    build_guide_composer_messages, build_guide_reviewer_messages,
    build_guide_selector_messages, make_guide_composer, make_guide_reviewer,
    make_guide_material_loader, make_guide_selector, parse_guide_draft,
    parse_guide_review, parse_guide_selection,
)
from session_state import VisitorInfo


class GuidePromptTests(unittest.TestCase):
    def setUp(self):
        self.request = GuideRequest(
            "s1", "r1", VisitorInfo(content_interests=["青铜器"], guide_styles=["通俗"]),
            ("old",), question="纹饰有什么含义？",
            guide_context=GuideContext("s1", "r1", "g0", ("已播放。", "未播放。"), 1),
        )
        self.material = GuideMaterial("m1", "青铜器材料", ("src-1",), ("old",))

    def test_prompts_and_schemas_exist(self):
        self.assertIn("候选材料", GUIDE_SELECTOR_SYSTEM_PROMPT)
        self.assertIn("独立", GUIDE_REVIEWER_SYSTEM_PROMPT)
        self.assertFalse(GUIDE_SELECTOR_JSON_SCHEMA["additionalProperties"])
        material_schema = GUIDE_SELECTOR_JSON_SCHEMA["properties"]["materials"]["items"]
        self.assertFalse(material_schema["additionalProperties"])
        self.assertEqual(set(material_schema["required"]), {
            "material_id", "text", "source_refs", "related_relic_ids",
        })
        self.assertEqual(material_schema["properties"]["source_refs"]["minItems"], 1)
        self.assertIn("completed_segments", GUIDE_COMPOSER_SYSTEM_PROMPT)
        self.assertFalse(GUIDE_COMPOSER_JSON_SCHEMA["additionalProperties"])
        self.assertFalse(GUIDE_REVIEWER_JSON_SCHEMA["additionalProperties"])

    def test_messages_include_context_as_json_data(self):
        messages = build_guide_selector_messages(self.request, [self.material])
        payload = json.loads(messages[1]["content"])
        self.assertEqual(payload["request"]["relic_id"], "r1")
        self.assertEqual(payload["request"]["guide_context"]["completed_segments"], 1)
        self.assertEqual(payload["candidate_materials"][0]["material_id"], "m1")

    def test_web_mode_requires_direct_fact_coverage_before_skipping_search(self):
        messages = build_guide_composer_messages(
            self.request,
            GuideSelection("r1", (self.material,)),
            web_search_enabled=True,
        )
        self.assertEqual(messages[0]["content"], GUIDE_COMPOSER_WEB_SYSTEM_PROMPT)
        prefix, payload_text = messages[1]["content"].split(GUIDE_COMPOSER_WEB_DATA_MARKER, 1)
        self.assertIn("只有候选材料对某项事实有直接陈述才算覆盖", prefix)
        self.assertIn("下一步必须先使用 web_search", prefix)
        self.assertIn("不要先输出草稿或 JSON", prefix)
        self.assertIn("受佛教艺术影响", GUIDE_COMPOSER_WEB_SYSTEM_PROMPT)
        payload = json.loads(payload_text)
        self.assertEqual(payload["request"]["question"], "纹饰有什么含义？")

    def test_selection_requires_exact_candidate_material(self):
        good = {"relic_id": "r1", "materials": [{"material_id": "m1", "text": "青铜器材料", "source_refs": ["src-1"], "related_relic_ids": ["old"]}]}
        result = parse_guide_selection(good, request=self.request, candidates=[self.material])
        self.assertEqual(result.materials[0], self.material)
        with self.assertRaises(ValueError):
            parse_guide_selection({**good, "materials": [{**good["materials"][0], "text": "编造"}]}, request=self.request, candidates=[self.material])
        with self.assertRaises(ValueError):
            parse_guide_selection({"relic_id": "r1", "materials": [{**good["materials"][0], "material_id": "fake"}]}, request=self.request, candidates=[self.material])

    def test_draft_and_review_parse_strictly(self):
        selection = parse_guide_selection({"relic_id": "r1", "materials": [{"material_id": "m1", "text": "青铜器材料", "source_refs": ["src-1"], "related_relic_ids": ["old"]}]}, request=self.request, candidates=[self.material])
        draft = parse_guide_draft("```json\n{\"relic_id\":\"r1\",\"segments\":[\"回答。\"],\"used_material_ids\":[\"m1\"]}\n```", request=self.request, selection=selection)
        self.assertEqual(draft.guide_text, "回答。")
        with self.assertRaises(ValueError):
            parse_guide_draft({"relic_id": "r1", "segments": [], "used_material_ids": ["m1"]}, request=self.request, selection=selection)
        with self.assertRaises(ValueError):
            parse_guide_draft({"relic_id": "r1", "segments": ["回答。"], "used_material_ids": ["m1", "m1"]}, request=self.request, selection=selection)
        review = parse_guide_review({"approved": False, "issues": ["缺少依据"]}, request=self.request, selection=selection, draft=draft)
        self.assertFalse(review.approved)
        with self.assertRaises(ValueError):
            parse_guide_review({"approved": True, "issues": ["仍有问题"]}, request=self.request, selection=selection, draft=draft)

    def test_async_factories_inject_model_call(self):
        async def model(**kwargs):
            name = kwargs["response_format"]["name"]
            if name == "guide_selection":
                return {"relic_id": "r1", "materials": [{"material_id": "m1", "text": "青铜器材料", "source_refs": ["src-1"], "related_relic_ids": ["old"]}]}
            if name == "guide_draft":
                return {"relic_id": "r1", "segments": ["回答。"], "used_material_ids": ["m1"]}
            return {"approved": True, "issues": []}
        async def run():
            selection = await make_guide_selector(model, [self.material])(request=self.request)
            draft = await make_guide_composer(model)(request=self.request, selection=selection)
            return await make_guide_reviewer(model)(request=self.request, selection=selection, draft=draft)
        self.assertTrue(asyncio.run(run()).approved)


class GuideMaterialLoaderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.request = GuideRequest("s", "r1", VisitorInfo(), ())
        self.materials = [
            GuideMaterial("m1", "第一份完整材料。", ("src-1",), ("old",)),
            GuideMaterial("m2", "第二份完整材料。", ("src-2",)),
        ]

    async def test_fixed_candidates_are_copied_and_all_materials_preserved(self):
        original = tuple(self.materials)
        load = make_guide_material_loader(self.materials)
        self.materials.clear()
        selection = await load(request=self.request)
        self.assertEqual(selection.relic_id, "r1")
        self.assertEqual(selection.materials, original)
        for actual, expected in zip(selection.materials, original):
            self.assertIs(actual, expected)

    async def test_async_source_loads_each_target_on_each_request(self):
        calls = []

        async def source(*, request):
            calls.append(request)
            return self.materials if request.relic_id == "r1" else self.materials[1:]

        load = make_guide_material_loader(source)
        second_request = GuideRequest("s", "r2", VisitorInfo(), ())
        first = await load(request=self.request)
        second = await load(request=second_request)
        self.assertEqual(calls, [self.request, second_request])
        self.assertEqual(first.materials, tuple(self.materials))
        self.assertEqual(second.relic_id, "r2")
        self.assertEqual(second.materials, (self.materials[1],))

    async def test_invalid_fixed_and_dynamic_materials_are_rejected(self):
        invalid_candidates = ([], [self.materials[0], self.materials[0]],
                              ["invalid"], "invalid", {"m1": self.materials[0]})
        for candidates in invalid_candidates:
            with self.subTest(candidates=candidates):
                with self.assertRaises(ValueError):
                    make_guide_material_loader(candidates)

                async def source(*, request):
                    return candidates

                with self.assertRaises(ValueError):
                    await make_guide_material_loader(source)(request=self.request)


if __name__ == "__main__":
    unittest.main()
