"""真实 Prompt 与算子通过部署装配层运行；模型调用使用本地替身。"""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from guide_models import GuideContext, GuideMaterial, GuideRequest, GuideSelection
from guide_prompts import (
    GUIDE_COMPOSER_SYSTEM_PROMPT, GUIDE_COMPOSER_WEB_DATA_MARKER,
    GUIDE_COMPOSER_WEB_SYSTEM_PROMPT,
)
from input_interpreter_prompt import INPUT_INTERPRETER_JSON_SCHEMA
from model_api import ChatCompletionsClient, LLMConfig
from model_services import build_model_services, load_llm_config, make_input_interpreter
from route_models import Exhibit, ExhibitTags, Gallery, MuseumSnapshot, SpatialEdge
from router import Router
from session_state import VisitorInfo
from summary_models import SummaryRelic


class ModelServicesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []
        self.reject_review = False
        self.guide_response = None
        self.guide_error = None
        self.material = GuideMaterial("m1", "这是一段离线测试材料。", ("test-source",))

        async def model(**kwargs):
            self.calls.append(copy.deepcopy(kwargs))
            name = kwargs["response_format"]["name"]
            payload = json.loads(kwargs["user_prompt"])
            if name == "input_analysis":
                return {
                    "visitor_info_changes": {},
                    "question": "这件展品有什么特点？" if "特点" in payload["user_text"] else None,
                    "replan_requested": False, "finish_requested": False,
                }
            if name == "interest_profile":
                return {
                    "target_eras": [], "target_regions": [],
                    "target_categories": ["测试品类"], "target_craft_types": [],
                    "target_themes": [], "target_aesthetics": [],
                    "target_cultural_ip": [], "target_audience": [],
                    "query_text": "测试品类",
                }
            if name == "guide_draft":
                if self.guide_error is not None:
                    raise self.guide_error
                if self.guide_response is not None:
                    return self.guide_response
                return {
                    "relic_id": payload["request"]["relic_id"],
                    "segments": [self.material.text], "used_material_ids": ["m1"],
                }
            if name == "summary_review":
                if self.reject_review:
                    return {"approved": False, "issues": ["离线测试拒绝该稿件"]}
                return {"approved": True, "issues": []}
            if name == "summary_draft":
                request = payload["request"]
                first = request["guide_records"][0]
                return {
                    "title": "离线测试手记", "introduction": "回顾本次测试记录。",
                    "trail": [{"relic_id": item["relic_id"], "recap": "回顾测试材料。"}
                              for item in request["visited_relics"]],
                    "discoveries": [{
                        "title": "测试发现", "text": self.material.text,
                        "relic_ids": [first["relic_id"]], "guide_ids": [first["guide_id"]],
                    }],
                    "connections": [], "questions": [], "next_exploration": None,
                }
            raise AssertionError("出现未预期的模型职责：" + name)

        self.model = model
        self.snapshot = MuseumSnapshot(
            [Gallery("g", "测试厅", base_pass_min=1)],
            [Exhibit("r1", "测试展品", "g", tags=ExhibitTags(category_main=["测试品类"]),
                     vector=[1, 0], importance_level=5)],
            [SpatialEdge("entrance", "g", 1), SpatialEdge("g", "exit", 1)],
            {"entrance": 1, "exit": 1},
        )
        self.vector = AsyncMock()
        self.vector.embed_query.return_value = [1, 0]
        self.source = AsyncMock()
        self.source.load_relics.return_value = (SummaryRelic("r1", "测试展品"),)

    def services(self, **changes):
        values = {
            "model_call": self.model, "candidates": [self.material],
            "snapshot": self.snapshot, "vector_service": self.vector,
            "summary_data_source": self.source,
        }
        values.update(changes)
        return build_model_services(**values)

    def router(self, **changes):
        router = Router(self.services(**changes), clock=lambda: 1000.0)
        router.create_session(
            {"content_interests": ["测试品类"], "remaining_minutes": 60},
            session_id="s", current_node_id="entrance", exit_node_id="exit",
        )
        return router

    async def test_router_uses_five_model_roles_and_keeps_independent_summary_review(self):
        http_calls = []

        async def transport(**kwargs):
            http_calls.append(copy.deepcopy(kwargs))
            payload = kwargs["payload"]
            schema = payload["response_format"]["json_schema"]
            result = await self.model(
                system_prompt=payload["messages"][0]["content"],
                user_prompt=payload["messages"][1]["content"],
                response_format={"type": "json_schema", **schema},
            )
            return {"choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)},
                                 "finish_reason": "stop"}]}

        client = ChatCompletionsClient(
            LLMConfig(api_key="offline-key", response_mode="json_schema"),
            transport=transport,
        )
        router = self.router(model_call=client)
        self.assertEqual(self.calls, [])
        response = await router.dispatch_chat("s", "你好")
        self.assertEqual(response["reply_content"]["status"], "ok")
        response = await router.dispatch_action("s", "replan")
        self.assertEqual(response["reply_content"]["status"], "planned")
        response = await router.dispatch_action("s", "start_guide", {"relic_id": "r1"})
        self.assertEqual(response["reply_content"]["status"], "guide_ready")
        self.assertEqual(len(response["latest_state"]["guide_records"]), 1)
        response = await router.dispatch_action("s", "finish")
        self.assertEqual(response["reply_content"]["status"], "summary_ready")
        self.assertEqual(response["reply_content"]["summary"]["status"], "generated")
        self.assertEqual(response["latest_state"]["status"], "finished")
        names = [call["response_format"]["name"] for call in self.calls]
        self.assertEqual(names, [
            "input_analysis", "interest_profile", "guide_draft",
            "summary_draft", "summary_review",
        ])
        self.assertEqual(len(http_calls), 5)
        for call in http_calls:
            self.assertEqual(call["url"], "https://llmapi.paratera.com/chat/completions")
            self.assertEqual(call["headers"]["Authorization"], "Bearer offline-key")
            self.assertEqual(call["payload"]["model"], "GLM-5.3-Flash")
            self.assertFalse(call["payload"]["stream"])
        self.assertNotEqual(self.calls[3]["system_prompt"], self.calls[4]["system_prompt"])
        self.vector.embed_query.assert_awaited_once()
        self.source.load_relics.assert_awaited_once_with(("r1",))
        json.dumps(response, ensure_ascii=False, allow_nan=False)

    async def test_main_guide_loads_all_candidates_and_calls_model_once(self):
        second = GuideMaterial("m2", "第二段完整材料。", ("second-source",), ("r2",))
        candidates = [self.material, second]
        services = self.services(candidates=candidates)
        candidates.clear()
        self.assertIsNone(services.reviewer)
        self.assertEqual(services.guide_review_mode, "integrated")
        router = Router(services, clock=lambda: 1000.0)
        router.create_session(
            {"content_interests": ["测试品类"], "remaining_minutes": 60},
            session_id="s", current_node_id="entrance", exit_node_id="exit",
        )
        await router.dispatch_action("s", "replan")
        self.calls.clear()
        response = await router.dispatch_action("s", "start_guide", {"relic_id": "r1"})
        self.assertEqual(response["reply_content"]["status"], "guide_ready")
        self.assertEqual([call["response_format"]["name"] for call in self.calls], ["guide_draft"])
        payload = json.loads(self.calls[0]["user_prompt"])
        self.assertEqual(payload["selection"]["materials"], [
            {"material_id": "m1", "text": self.material.text,
             "source_refs": ["test-source"], "related_relic_ids": []},
            {"material_id": "m2", "text": second.text,
             "source_refs": ["second-source"], "related_relic_ids": ["r2"]},
        ])
        self.assertIsNone(response["reply_content"]["guide"]["review"])

    async def test_followup_uses_one_guide_call_and_preserves_main_guide(self):
        router = self.router()
        await router.dispatch_action("s", "replan")
        response = await router.dispatch_action("s", "start_guide", {"relic_id": "r1"})
        original = response["latest_state"]["guide_records"][0]
        context = response["reply_content"]["playback"]["current"]
        self.calls.clear()
        response = await router.dispatch_chat("s", "这件展品有什么特点？", playback={
            "guide_id": context["guide_id"], "completed_segments": 0,
        })
        self.assertEqual(response["reply_content"]["status"], "answer_ready")
        self.assertEqual([call["response_format"]["name"] for call in self.calls],
                         ["input_analysis", "guide_draft"])
        self.assertEqual(response["latest_state"]["guide_records"][0], original)
        self.assertEqual(len(response["latest_state"]["visited_relics"]), 1)
        self.assertEqual(len(response["latest_state"]["guide_records"]), 2)
        self.assertEqual(response["latest_state"]["guide_records"][1]["kind"], "answer")
        self.assertEqual(response["reply_content"]["playback"]["resume_stack"], [context])

    async def test_invalid_guide_or_model_failure_does_not_commit_or_retry(self):
        invalid_outputs = [
            {"relic_id": "r1", "segments": ["无效引用。"], "used_material_ids": ["unknown"]},
            {"relic_id": "other", "segments": ["错误目标。"], "used_material_ids": ["m1"]},
            {"relic_id": "r1", "segments": [], "used_material_ids": ["m1"]},
            "模型输出不是 JSON",
        ]
        for output in invalid_outputs + [TimeoutError("离线超时")]:
            with self.subTest(output=output):
                router = self.router()
                planned = await router.dispatch_action("s", "replan")
                self.calls.clear()
                self.guide_response = None if isinstance(output, Exception) else output
                self.guide_error = output if isinstance(output, Exception) else None
                response = await router.dispatch_action("s", "start_guide", {"relic_id": "r1"})
                self.assertEqual(response["reply_content"]["status"], "error")
                self.assertEqual(response["latest_state"], planned["latest_state"])
                self.assertEqual([call["response_format"]["name"] for call in self.calls],
                                 ["guide_draft"])

    async def test_async_candidate_loader_is_checked_before_model_call(self):
        for materials in ([], [self.material, self.material]):
            with self.subTest(materials=materials):
                loader = AsyncMock(return_value=materials)
                services = self.services(candidates=loader)
                request = GuideRequest("s", "r1", VisitorInfo(), ())
                with self.assertRaises(ValueError):
                    await services.selector(request=request)
                loader.assert_awaited_once_with(request=request)
                self.assertEqual(self.calls, [])

    async def test_guide_web_search_selects_policy_and_tool_only_for_guide_draft(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                http_calls = []

                async def transport(**kwargs):
                    http_calls.append(copy.deepcopy(kwargs))
                    name = kwargs["payload"]["response_format"]["json_schema"]["name"]
                    if name == "input_analysis":
                        content = {"visitor_info_changes": {}, "question": None,
                                   "replan_requested": False, "finish_requested": False}
                    else:
                        content = {"relic_id": "r1", "segments": [self.material.text],
                                   "used_material_ids": ["m1"]}
                    return {"choices": [{"message": {
                        "content": json.dumps(content, ensure_ascii=False)},
                        "finish_reason": "stop"}]}

                services = build_model_services(
                    config=LLMConfig(api_key="offline-key", guide_web_search=enabled),
                    transport=transport, candidates=[self.material],
                )
                await services.interpreter("你好", visitor_info=VisitorInfo(),
                                           current_visit=None, guide_context=None)
                requests = (
                    GuideRequest("s", "r1", VisitorInfo(), ()),
                    GuideRequest("s", "r1", VisitorInfo(), (), question="特点？",
                                 guide_context=GuideContext("s", "r1", "g1", ("已播。", "未播。"), 1),
                                 current_guide_text="原稿。"),
                )
                for request in requests:
                    draft = await services.composer(
                        request=request, selection=GuideSelection("r1", (self.material,)),
                    )
                    self.assertEqual(draft.used_material_ids, ("m1",))
                names = [call["payload"]["response_format"]["json_schema"]["name"]
                         for call in http_calls]
                self.assertEqual(names, ["input_analysis", "guide_draft", "guide_draft"])
                interpreter_payload = http_calls[0]["payload"]
                self.assertNotIn("tools", interpreter_payload)
                self.assertNotIn("联网搜索", interpreter_payload["messages"][0]["content"])
                for call, request in zip(http_calls[1:], requests):
                    composer_payload = call["payload"]
                    user_text = composer_payload["messages"][1]["content"]
                    if enabled:
                        self.assertEqual(composer_payload["tools"], [
                            {"type": "web_search",
                             "web_search": {"enable": True, "search_result": True}}])
                        self.assertEqual(composer_payload["messages"][0]["content"],
                                         GUIDE_COMPOSER_WEB_SYSTEM_PROMPT)
                        prefix, user_text = user_text.split(GUIDE_COMPOSER_WEB_DATA_MARKER, 1)
                        self.assertIn("web_search", prefix)
                    else:
                        self.assertNotIn("tools", composer_payload)
                        self.assertEqual(composer_payload["messages"][0]["content"],
                                         GUIDE_COMPOSER_SYSTEM_PROMPT)
                    data = json.loads(user_text)
                    self.assertEqual(data["request"]["question"], request.question)
                    self.assertEqual(data["request"]["current_guide_text"], request.current_guide_text)
                    self.assertEqual(data["selection"]["materials"][0]["text"], self.material.text)
                    self.assertEqual(set(composer_payload["response_format"]["json_schema"]["schema"]["required"]),
                                     {"relic_id", "segments", "used_material_ids"})
                    if request.guide_context is not None:
                        self.assertEqual(data["request"]["guide_context"]["played_segments"], ["已播。"])
                        self.assertEqual(data["request"]["guide_context"]["remaining_segments"], ["未播。"])

    async def test_summary_rejection_is_still_a_separate_model_call(self):
        router = self.router()
        await router.dispatch_action("s", "replan")
        await router.dispatch_action("s", "start_guide", {"relic_id": "r1"})
        self.reject_review = True
        self.calls.clear()
        response = await router.dispatch_action("s", "finish")
        self.assertEqual(response["reply_content"]["status"], "error")
        self.assertEqual(response["latest_state"]["status"], "finished")
        self.assertEqual(len(response["latest_state"]["visited_relics"]), 1)
        self.assertEqual(len(response["latest_state"]["guide_records"]), 1)
        self.assertIsNone(response["reply_content"]["summary"])
        self.assertEqual([call["response_format"]["name"] for call in self.calls],
                         ["summary_draft", "summary_review"])

    async def test_missing_candidate_materials_fails_before_model_call(self):
        services = self.services(candidates=None)
        with self.assertRaises(NotImplementedError):
            await services.selector(request=GuideRequest("s", "r1", VisitorInfo(), ()))
        self.assertEqual(self.calls, [])

    async def test_interpreter_keeps_local_schema_independent_of_client_mutation(self):
        original = copy.deepcopy(INPUT_INTERPRETER_JSON_SCHEMA)

        async def mutate_model(**kwargs):
            kwargs["response_format"]["schema"].clear()
            return {"visitor_info_changes": {}, "question": None,
                    "replan_requested": False, "finish_requested": False}

        interpreter = make_input_interpreter(mutate_model)
        await interpreter("你好", visitor_info=VisitorInfo(), current_visit=None, guide_context=None)
        self.assertEqual(INPUT_INTERPRETER_JSON_SCHEMA, original)

    async def test_interpreter_invalid_json_is_rejected_by_existing_parser(self):
        interpreter = make_input_interpreter(AsyncMock(return_value={"question": "缺字段"}))
        with self.assertRaises(ValueError):
            await interpreter("你好", visitor_info=VisitorInfo(), current_visit=None, guide_context=None)

    def test_conflicting_injected_dependencies_are_rejected(self):
        with self.assertRaises(ValueError):
            self.services(data_source=AsyncMock())
        with self.assertRaises(ValueError):
            self.services(transport=AsyncMock())
        with self.assertRaises(ValueError):
            self.services(config=LLMConfig())


class ModelConfigLoadingTests(unittest.TestCase):
    def config_file(self, text):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "llm_config.json"
        path.write_text(text, encoding="utf-8")
        return path

    def test_checked_in_config_matches_provider_with_placeholder_key(self):
        config = load_llm_config(environ={})
        self.assertEqual(config.base_url, "https://llmapi.paratera.com")
        self.assertEqual(config.model, "GLM-5.3-Flash")
        self.assertEqual(config.chat_path, "/chat/completions")
        self.assertEqual(config.response_mode, "json_object")
        self.assertEqual(config.embedding_model, "GLM-Embedding-3")
        self.assertFalse(config.guide_web_search)
        self.assertNotIn(config.api_key, repr(config))

    def test_environment_overrides_file_without_writing_secret(self):
        path = self.config_file(json.dumps({"model": "file-model", "embedding_model": "file-embed"}))
        before = path.read_text(encoding="utf-8")
        config = load_llm_config(path, environ={
            "LUMORA_LLM_BASE_URL": "https://example.com/v1/",
            "LUMORA_LLM_API_KEY": "local-test-key",
            "LUMORA_LLM_MODEL": "env-model", "LUMORA_LLM_TIMEOUT": "12.5",
            "LUMORA_LLM_RESPONSE_MODE": "json_object", "LUMORA_LLM_EMBEDDING_MODEL": "",
            "LUMORA_LLM_GUIDE_WEB_SEARCH": "false",
        })
        self.assertEqual(config.base_url, "https://example.com/v1")
        self.assertEqual(config.model, "env-model")
        self.assertEqual(config.timeout, 12.5)
        self.assertEqual(config.response_mode, "json_object")
        self.assertIsNone(config.embedding_model)
        self.assertFalse(config.guide_web_search)
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertNotIn("local-test-key", repr(config))

    def test_invalid_config_json_and_unknown_fields_fail(self):
        for text in ('[]', '{"unknown":true}', '{"model":"a","model":"b"}',
                     '{"timeout":NaN}', '{"timeout":Infinity}', '{',
                     '{"guide_web_search":"true"}', '{"guide_web_search":1}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                load_llm_config(self.config_file(text), environ={})
        with self.assertRaises(ValueError):
            load_llm_config(self.config_file('{}'), environ={"LUMORA_LLM_TIMEOUT": "bad"})
        for value in ("yes", "", "1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                load_llm_config(self.config_file('{}'),
                                environ={"LUMORA_LLM_GUIDE_WEB_SEARCH": value})

    def test_factory_is_lazy_and_only_injects_configured_embedding(self):
        transport = AsyncMock()
        services = build_model_services(config=LLMConfig(), transport=transport)
        self.assertIsNone(services.vector_service)
        services = build_model_services(
            config=LLMConfig(embedding_model="explicit-embedding"), transport=transport,
        )
        self.assertTrue(callable(services.vector_service.embed_query))
        transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()


