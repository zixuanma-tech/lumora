import asyncio
import json
import unittest

from interest_prompts import (
    INTEREST_PARSER_SYSTEM_PROMPT,
    INTEREST_PROFILE_JSON_SCHEMA,
    build_interest_parser_messages,
    make_interest_parser,
    parse_interest_profile,
)
from route_models import InterestProfile
from session_state import VisitorInfo


class InterestPromptTests(unittest.TestCase):
    def setUp(self):
        self.info = VisitorInfo(
            content_interests=["唐代陶瓷", "动物题材"],
            guide_styles=["通俗"],
            guide_focus=["制作工艺"],
            remaining_minutes=40,
            other_needs=["亲子同行"],
        )

    def test_prompt_and_schema_describe_dimensions(self):
        for name in (
            "target_eras", "target_regions", "target_categories", "target_craft_types",
            "target_themes", "target_aesthetics", "target_cultural_ip", "target_audience",
            "query_text",
        ):
            self.assertIn(name, INTEREST_PARSER_SYSTEM_PROMPT)
            self.assertIn(name, INTEREST_PROFILE_JSON_SCHEMA["properties"])
        self.assertFalse(INTEREST_PROFILE_JSON_SCHEMA["additionalProperties"])

    def test_messages_keep_visitor_info_and_optional_taxonomy_as_data(self):
        messages = build_interest_parser_messages(
            visitor_info=self.info,
            taxonomy={
                "eras": ["唐", "宋"],
                "categories": ["陶瓷"],
                "themes": ["动物", "礼制"],
            },
        )
        self.assertEqual([item["role"] for item in messages], ["system", "user"])
        payload = json.loads(messages[1]["content"])
        self.assertEqual(payload["current_visitor_info"]["remaining_minutes"], 40)
        self.assertEqual(payload["catalog_taxonomy"]["target_eras"], ["唐", "宋"])
        self.assertEqual(payload["catalog_taxonomy"]["target_categories"], ["陶瓷"])

    def test_parser_preserves_flat_targets_and_or_groups(self):
        profile = parse_interest_profile({
            "target_eras": ["唐", "唐"],
            "target_regions": [],
            "target_categories": ["陶瓷"],
            "target_craft_types": [],
            "target_themes": [["动物", "神话瑞兽"], ["动物", "神话瑞兽"]],
            "target_aesthetics": [],
            "target_cultural_ip": [],
            "target_audience": [["儿童"]],
            "query_text": "唐代陶瓷和动物题材，关注制作工艺",
        })
        self.assertIsInstance(profile, InterestProfile)
        self.assertEqual(profile.target_eras, ("唐",))
        self.assertEqual(profile.target_themes, (("动物", "神话瑞兽"),))
        self.assertEqual(profile.target_audience, (("儿童",),))

    def test_parser_rejects_unknown_fields_and_empty_targets(self):
        base = {
            "target_eras": [], "target_regions": [], "target_categories": [],
            "target_craft_types": [], "target_themes": [], "target_aesthetics": [],
            "target_cultural_ip": [], "target_audience": [], "query_text": "",
        }
        with self.assertRaises(ValueError):
            parse_interest_profile({**base, "unexpected": []})
        with self.assertRaisesRegex(ValueError, "至少需要"):
            parse_interest_profile(base)

    def test_factory_passes_prompt_and_returns_interest_profile(self):
        calls = []

        async def fake_model(**kwargs):
            calls.append(kwargs)
            return {
                "target_eras": ["唐"], "target_regions": [],
                "target_categories": ["陶瓷"], "target_craft_types": [],
                "target_themes": [["动物"]], "target_aesthetics": [],
                "target_cultural_ip": [], "target_audience": [],
                "query_text": "唐代陶瓷，动物题材",
            }

        parser = make_interest_parser(
            fake_model, taxonomy={"eras": ["唐"], "categories": ["陶瓷"]},
        )
        result = asyncio.run(parser(visitor_info=self.info))
        self.assertEqual(result.target_eras, ("唐",))
        self.assertEqual(result.target_categories, ("陶瓷",))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["response_format"]["name"], "interest_profile")


if __name__ == "__main__":
    unittest.main()
