"""模型 HTTP 参数、错误边界与真实 Prompt 闭包的离线集成测试。"""

import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from input_interpreter_prompt import make_input_interpreter
from model_api import (
    ChatCompletionsClient, ModelServiceError, LLMConfig, PLACEHOLDER_API_KEY,
    _NoRedirect, _json_response, _post_json_sync,
)
from session_state import VisitorInfo


SCHEMA = {"type": "object", "additionalProperties": False,
          "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
FORMAT = {"type": "json_schema", "name": "test_output", "strict": True, "schema": SCHEMA}


def response(content='{"ok":true}', *, finish_reason="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}


class ModelConfigTests(unittest.TestCase):
    def test_defaults_match_supplied_service_and_hide_key_from_repr(self):
        settings = LLMConfig()
        self.assertEqual(settings.base_url, "https://llmapi.paratera.com")
        self.assertEqual(settings.model, "GLM-5.3-Flash")
        self.assertEqual(settings.api_key, PLACEHOLDER_API_KEY)
        self.assertNotIn(PLACEHOLDER_API_KEY, repr(settings))
        self.assertEqual(settings.chat_url, "https://llmapi.paratera.com/chat/completions")

    def test_explicit_version_or_endpoint_are_preserved(self):
        settings = LLMConfig(base_url="http://localhost:3210/v1/", api_key="secret", timeout=12.5)
        self.assertEqual(settings.chat_url, "http://localhost:3210/v1/chat/completions")
        self.assertEqual(settings.timeout, 12.5)
        self.assertNotIn("secret", repr(settings))
        complete = LLMConfig(base_url="https://example.com/api/chat/completions/")
        self.assertEqual(complete.chat_url, "https://example.com/api/chat/completions")

    def test_invalid_settings_fail_before_any_request(self):
        cases = ({"base_url": "file:///tmp/test"}, {"base_url": "https://user:secret@example.com"},
                 {"base_url": "https://example.com?key=secret"}, {"api_key": "x\ny"},
                 {"api_key": ""}, {"api_key": " 中文 "}, {"timeout": 0}, {"timeout": True},
                 {"timeout": float("inf")}, {"response_mode": "automatic"},
                 {"base_url": "https://example.com:invalid"}, {"chat_path": "/"},
                 {"guide_web_search": "true"}, {"guide_web_search": 1},
                 {"guide_web_search": None})
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                LLMConfig(**values)


class ChatCompletionsClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_schema_transport_maps_flat_prompt_schema(self):
        calls = []

        async def transport(**kwargs):
            calls.append(kwargs)
            return response()

        client = ChatCompletionsClient(LLMConfig(
            api_key="test-secret", response_mode="json_schema"), transport=transport)
        self.assertEqual(await client(system_prompt="system", user_prompt="user", response_format=FORMAT),
                         '{"ok":true}')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["headers"]["Authorization"], "Bearer test-secret")
        payload = calls[0]["payload"]
        self.assertEqual(payload["model"], "GLM-5.3-Flash")
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["messages"], [
            {"role": "system", "content": "system"}, {"role": "user", "content": "user"}])
        self.assertEqual(payload["response_format"], {"type": "json_schema", "json_schema": {
            "name": "test_output", "strict": True, "schema": SCHEMA}})

    async def test_compatibility_modes_include_schema_and_never_retry(self):
        for mode in ("json_object",):
            calls = []

            async def transport(**kwargs):
                calls.append(kwargs)
                return response()

            client = ChatCompletionsClient(LLMConfig(response_mode=mode), transport=transport)
            await client(system_prompt="system", user_prompt="user", response_format=FORMAT)
            payload = calls[0]["payload"]
            self.assertIn('"required":["ok"]', payload["messages"][0]["content"])
            self.assertEqual(payload["response_format"], {"type": "json_object"})
            self.assertEqual(len(calls), 1)

        calls = []

        async def failed(**kwargs):
            calls.append(kwargs)
            raise ModelServiceError("无法连接模型服务", code="connection_failed")

        client = ChatCompletionsClient(transport=failed)
        with self.assertRaises(ModelServiceError):
            await client(system_prompt="s", user_prompt="u", response_format=FORMAT)
        self.assertEqual(len(calls), 1)

    async def test_web_search_tool_is_attached_only_when_requested(self):
        calls = []

        async def transport(**kwargs):
            calls.append(kwargs)
            return response()

        client = ChatCompletionsClient(transport=transport)
        await client(system_prompt="s", user_prompt="u", response_format=FORMAT, web_search=True)
        await client(system_prompt="s", user_prompt="u", response_format=FORMAT)
        self.assertEqual(calls[0]["payload"]["tools"], [
            {"type": "web_search", "web_search": {"enable": True, "search_result": True}}])
        self.assertNotIn("tools", calls[1]["payload"])
        with self.assertRaises(ValueError):
            await client(system_prompt="s", user_prompt="u", response_format=FORMAT,
                         web_search="yes")
        self.assertEqual(len(calls), 2)

    async def test_incomplete_filtered_refused_and_invalid_responses_fail(self):
        cases = [None, {}, {"choices": []}, {"choices": [None]},
                 {"choices": [response()["choices"][0]] * 2}, response(finish_reason="length"),
                 response(finish_reason="content_filter"), response(finish_reason=None),
                 response(None), response(" "), {"error": "secret", **response()},
                 {"choices": [{"message": {"content": "{}", "refusal": "cannot comply"},
                               "finish_reason": "stop"}]},
                 {"choices": [{"message": {"content": "{}", "tool_calls": [{"id": "bad"}]},
                               "finish_reason": "stop"}]}]
        for result in cases:
            async def transport(**kwargs):
                return result
            client = ChatCompletionsClient(transport=transport)
            with self.subTest(result=result), self.assertRaises(ModelServiceError):
                await client(system_prompt="s", user_prompt="u", response_format=FORMAT)

    async def test_input_interpreter_factory_uses_http_content_and_existing_parser(self):
        calls = []

        async def transport(**kwargs):
            calls.append(kwargs)
            return response(json.dumps({"visitor_info_changes": {"remaining_minutes": 30},
                                        "question": None, "replan_requested": False,
                                        "finish_requested": False}))

        client = ChatCompletionsClient(transport=transport)
        interpreter = make_input_interpreter(client)
        result = await interpreter("还剩半小时", visitor_info=VisitorInfo())
        self.assertEqual(result.visitor_info_changes, {"remaining_minutes": 30})
        payload = json.loads(calls[0]["payload"]["messages"][1]["content"])
        self.assertEqual(payload["user_text"], "还剩半小时")

        async def invalid(**kwargs):
            return response('{"visitor_info_changes":{},"question":null,"replan_requested":false,'
                            '"finish_requested":false,"extra":true}')

        with self.assertRaisesRegex(ValueError, "未知字段"):
            await make_input_interpreter(ChatCompletionsClient(transport=invalid))(
                "你好", visitor_info=VisitorInfo())


class HttpTransportTests(unittest.TestCase):
    def call(self):
        return _post_json_sync(url="https://example.com/chat/completions",
                               headers={"Authorization": "Bearer secret"},
                               payload={"model": "test", "messages": []}, timeout_seconds=2)

    def test_transport_serializes_post_and_decodes_json(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self):
                return b'{"choices":[]}'

        with patch("model_api.build_opener") as opener:
            open_url = opener.return_value.open
            open_url.return_value = Response()
            self.assertEqual(self.call(), {"choices": []})
        request = open_url.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(json.loads(request.data), {"model": "test", "messages": []})
        self.assertEqual(open_url.call_args.kwargs["timeout"], 2)

    def test_transport_errors_hide_upstream_body_and_do_not_retry(self):
        for status in (302, 307, 401, 403, 429, 500):
            upstream = HTTPError("https://example.com", status, "secret", {}, io.BytesIO(b"secret"))
            with patch("model_api.build_opener") as opener:
                open_url = opener.return_value.open
                open_url.side_effect = upstream
                with self.subTest(status=status), self.assertRaises(ModelServiceError) as caught:
                    self.call()
                self.assertEqual(caught.exception.status_code, status)
                self.assertNotIn("secret", str(caught.exception))
                self.assertEqual(open_url.call_count, 1)
        for error, code in ((TimeoutError(), "timeout"), (URLError(TimeoutError()), "timeout"),
                            (URLError("secret"), "connection_failed"),
                            (OSError("secret"), "connection_failed")):
            with patch("model_api.build_opener") as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaises(ModelServiceError) as caught:
                    self.call()
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn("secret", str(caught.exception))

    def test_redirects_are_not_followed_with_authorization(self):
        handler = _NoRedirect()
        self.assertIsNone(handler.redirect_request(
            None, None, 302, "redirect", {}, "https://other.example/secret"))
        with patch("model_api.build_opener") as opener:
            opener.return_value.open.side_effect = HTTPError(
                "https://example.com", 302, "secret", {"Location": "https://other.example"},
                io.BytesIO(b"secret"))
            with self.assertRaises(ModelServiceError) as caught:
                self.call()
            self.assertEqual(caught.exception.code, "redirect_rejected")
            self.assertEqual(opener.return_value.open.call_count, 1)
            self.assertIsInstance(opener.call_args.args[0], _NoRedirect)

    def test_response_json_rejects_duplicate_keys_constants_and_invalid_encoding(self):
        for raw in (b'{"choices":[],"choices":[]}', b'{"value":NaN}', b'{"value":Infinity}',
                    b'{"value":-Infinity}', b'[]', b'\xff', b'{'):
            with self.subTest(raw=raw), self.assertRaises(ModelServiceError):
                _json_response(raw)


if __name__ == "__main__":
    unittest.main()
