import asyncio
import json
import unittest

from session_state import GuideRecord, VisitRecord, VisitorInfo
from summary_models import SummaryFeedback, SummaryRelic, SummaryRequest
from summary_prompts import (
    SUMMARY_COMPOSER_JSON_SCHEMA, SUMMARY_COMPOSER_SYSTEM_PROMPT,
    SUMMARY_REVIEWER_JSON_SCHEMA, build_summary_composer_messages,
    make_summary_composer, make_summary_reviewer, parse_summary_draft,
    parse_summary_review,
)


class SummaryPromptTests(unittest.TestCase):
    def setUp(self):
        visits = (VisitRecord("r1", "青铜器主讲。"), VisitRecord("r2", "陶瓷主讲。"))
        records = (
            GuideRecord("g1", "r1", "guide", ("青铜器主讲。",)),
            GuideRecord("a1", "r1", "answer", ("回答纹饰。",), "纹饰？"),
            GuideRecord("g2", "r2", "guide", ("陶瓷主讲。",)),
        )
        self.request = SummaryRequest("s1", VisitorInfo(content_interests=["工艺"]), visits, records,
                                     (SummaryRelic("r1", "青铜器"), SummaryRelic("r2", "陶瓷")),
                                     SummaryFeedback("r1", "很有意思"))
        self.good = {
            "title": "我的手记", "introduction": "这次参观认识了两件展品。",
            "trail": [{"relic_id": "r1", "recap": "青铜器"}, {"relic_id": "r2", "recap": "陶瓷"}],
            "discoveries": [{"title": "工艺", "text": "两件展品都介绍了工艺。", "relic_ids": ["r1"], "guide_ids": ["g1"]}],
            "connections": [{"title": "对照", "text": "两件展品可对照。", "relic_ids": ["r1", "r2"], "guide_ids": ["g1", "g2"]}],
            "questions": [{"guide_id": "a1", "answer_summary": "回答纹饰"}], "next_exploration": "继续了解工艺",
        }

    def test_prompt_schema_and_message_context(self):
        self.assertIn("trail", SUMMARY_COMPOSER_SYSTEM_PROMPT)
        self.assertFalse(SUMMARY_COMPOSER_JSON_SCHEMA["additionalProperties"])
        self.assertFalse(SUMMARY_REVIEWER_JSON_SCHEMA["additionalProperties"])
        payload = json.loads(build_summary_composer_messages(self.request)[1]["content"])
        self.assertEqual([x["relic_id"] for x in payload["request"]["visited_relics"]], ["r1", "r2"])
        self.assertEqual(payload["request"]["feedback"]["reflection"], "很有意思")

    def test_parse_valid_and_reject_scope_or_order_errors(self):
        draft = parse_summary_draft(self.good, request=self.request)
        self.assertEqual(tuple(x.relic_id for x in draft.trail), ("r1", "r2"))
        with self.assertRaises(ValueError):
            parse_summary_draft({**self.good, "trail": list(reversed(self.good["trail"]))}, request=self.request)
        with self.assertRaises(ValueError):
            bad = {**self.good, "questions": [{"guide_id": "g1", "answer_summary": "x"}]}
            parse_summary_draft(bad, request=self.request)
        with self.assertRaises(ValueError):
            bad = {**self.good, "discoveries": [{**self.good["discoveries"][0], "guide_ids": ["fake"]}]}
            parse_summary_draft(bad, request=self.request)

    def test_empty_discoveries_and_review_combination_fail(self):
        with self.assertRaises(ValueError):
            parse_summary_draft({**self.good, "discoveries": []}, request=self.request)
        draft = parse_summary_draft(self.good, request=self.request)
        self.assertFalse(parse_summary_review({"approved": False, "issues": ["事实越界"]}, request=self.request, draft=draft).approved)
        with self.assertRaises(ValueError):
            parse_summary_review({"approved": True, "issues": ["事实越界"]}, request=self.request, draft=draft)

    def test_duplicate_references_and_empty_request_fail(self):
        with self.assertRaises(ValueError):
            parse_summary_draft({**self.good, "discoveries": [{**self.good["discoveries"][0], "relic_ids": ["r1", "r1"]}]}, request=self.request)
        with self.assertRaises(ValueError):
            parse_summary_draft({**self.good, "questions": [{"guide_id": "a1", "answer_summary": "x"}, {"guide_id": "a1", "answer_summary": "y"}]}, request=self.request)
        empty = SummaryRequest("empty", VisitorInfo(), (), (), ())
        with self.assertRaises(ValueError):
            parse_summary_draft(self.good, request=empty)

    def test_async_factories(self):
        async def composer_model(**kwargs):
            return self.good
        async def reviewer_model(**kwargs):
            return {"approved": True, "issues": []}
        async def run():
            draft = await make_summary_composer(composer_model)(request=self.request)
            return await make_summary_reviewer(reviewer_model)(request=self.request, draft=draft)
        self.assertTrue(asyncio.run(run()).approved)


if __name__ == "__main__":
    unittest.main()
