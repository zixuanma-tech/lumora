"""演示适配与 HTTP 边界回归；模型替身只用于测试，不发网络请求。"""
import json
from threading import Thread
from urllib.error import HTTPError
from urllib.request import urlopen, Request
import unittest

from demo_museum import ENTRANCE, EXIT, build_demo_services, museum_metadata
from main import create_server
from router import Router


async def model(**kwargs):
    payload = json.loads(kwargs['user_prompt'])
    name = kwargs['response_format']['name']
    if name == 'input_analysis':
        return {'visitor_info_changes': {} if '纹样' in payload.get('user_text', '') else {'content_interests': ['日常生活'], 'remaining_minutes': 45},
                'question': '肩部是什么纹样？' if '纹样' in payload.get('user_text', '') else None, 'replan_requested': False, 'finish_requested': False}
    if name == 'interest_profile':
        return {'target_eras': [], 'target_regions': [], 'target_categories': [], 'target_craft_types': [],
                'target_themes': [['日常生活']], 'target_aesthetics': [], 'target_cultural_ip': [],
                'target_audience': [], 'query_text': '日常生活'}
    if name == 'guide_draft':
        material = payload['selection']['materials'][0]
        return {'relic_id': payload['request']['relic_id'], 'segments': [material['text'], '以上都是虚构教学设定。'],
                'used_material_ids': [material['material_id']]}
    if name == 'summary_review':
        return {'approved': True, 'issues': []}
    if name == 'summary_draft':
        request = payload['request']
        record = request['guide_records'][0]
        return {'title': '演示探索手记', 'introduction': '本次虚构场馆的探索记录。',
                'trail': [{'relic_id': item['relic_id'], 'recap': '观察了虚构器物的外形。'} for item in request['visited_relics']],
                'discoveries': [{'title': '器物形状', 'text': '讲解介绍了器物的演示设定。',
                                 'relic_ids': [record['relic_id']], 'guide_ids': [record['guide_id']]}],
                'connections': [], 'questions': [], 'next_exploration': None}
    raise AssertionError(name)


class DemoHTTPTests(unittest.TestCase):
    def setUp(self):
        self.model_calls = []

        async def recording_model(**kwargs):
            self.model_calls.append(kwargs['response_format']['name'])
            if kwargs['response_format']['name'] == 'guide_draft':
                self.guide_targets.append(json.loads(kwargs['user_prompt'])['request']['relic_id'])
            return await model(**kwargs)

        self.guide_targets = []
        # 原 HTTP 流程继续精确核对前台调用；预生成另有默认开启的专门用例。
        self.router = Router(build_demo_services(model_call=recording_model), prefetch_enabled=False)
        self.server = create_server(router=self.router, port=0)
        # 测试显式装配时复用公开元信息，不触发真实模型配置。
        self.server.museum = museum_metadata()
        self.worker = Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.worker.join()
        self.server.server_close()

    def call(self, path, payload=None):
        req = Request(self.base + path, data=None if payload is None else json.dumps(payload).encode(),
                      headers={'Content-Type': 'application/json'})
        with urlopen(req, timeout=5) as response:
            return json.load(response)

    def test_full_demo_flow_and_authoritative_resume_after_reload(self):
        museum = self.call('/api/museum')
        self.assertEqual(museum['mode'], 'demo')
        self.assertGreaterEqual(len(museum['recommended_routes']), 3)
        result = self.call('/api/session', {})
        session_id = result['latest_state']['session_id']
        self.assertEqual(result['reply_content']['current_node_id'], ENTRANCE)
        self.assertEqual(result['reply_content']['exit_node_id'], EXIT)
        result = self.call('/api/chat', {'session_id': session_id, 'text': '想看日常生活，有45分钟'})
        self.assertEqual(result['reply_content']['status'], 'planned')
        queue = result['latest_state']['pending_queue']
        self.assertGreater(len(queue), 0)
        self.call('/api/event', {'session_id': session_id, 'action': 'skip', 'payload': {'relic_id': queue[0]['relic_id']}})
        next_id = queue[1]['relic_id']
        calls_before_guide = len(self.model_calls)
        result = self.call('/api/event', {'session_id': session_id, 'action': 'start_guide', 'payload': {'relic_id': next_id}})
        self.assertEqual(result['reply_content']['status'], 'guide_ready')
        self.assertEqual(self.model_calls[calls_before_guide:], ['guide_draft'])
        context = result['reply_content']['playback']['current']
        self.assertEqual(context['completed_segments'], 0)  # 生成不表示已播放
        restored = self.call('/api/state?session_id=' + session_id)
        self.assertEqual(restored['reply_content']['playback']['current'], context)
        calls_before_question = len(self.model_calls)
        result = self.call('/api/chat', {'session_id': session_id, 'text': '肩部是什么纹样？',
                           'playback': {'guide_id': context['guide_id'], 'completed_segments': 1}})
        self.assertEqual(result['reply_content']['status'], 'answer_ready')
        self.assertEqual(self.model_calls[calls_before_question:], ['input_analysis', 'guide_draft'])
        answer = result['reply_content']['playback']['current']
        self.assertEqual(len(result['reply_content']['playback']['resume_stack']), 1)
        result = self.call('/api/event', {'session_id': session_id, 'action': 'resume_guide',
                           'payload': {'guide_id': answer['guide_id'], 'completed_segments': len(answer['segments'])}})
        self.assertEqual(result['reply_content']['playback']['current']['guide_id'], context['guide_id'])
        self.assertEqual(result['reply_content']['playback']['current']['completed_segments'], 1)
        self.assertEqual(len(result['latest_state']['visited_relics']), 1)
        result = self.call('/api/event', {'session_id': session_id, 'action': 'finish',
                           'payload': {'feedback': {'favorite_relic_id': next_id, 'reflection': '观察到了形状差异。'}}})
        self.assertEqual(result['reply_content']['status'], 'summary_ready')
        self.assertEqual(result['latest_state']['status'], 'finished')
        self.assertEqual(len(result['reply_content']['summary']['trail']), 1)
        self.assertEqual(result['reply_content']['summary']['feedback']['favorite_relic_id'], next_id)
        self.assertEqual(self.model_calls, [
            'input_analysis', 'interest_profile', 'guide_draft',
            'input_analysis', 'guide_draft', 'summary_draft', 'summary_review',
        ])

    def test_static_allowlist_blocks_configuration_sources_and_private_pages(self):
        for path in ['/llm_config.json', '/main.py', '/data/relics.json', '/%2e%2e/llm_config.json', '/架构示意图.html']:
            with self.subTest(path=path):
                # urllib 请求路径须 ASCII，最后一项按编码的实际 URL 请求。
                from urllib.parse import quote
                with self.assertRaises(HTTPError) as caught:
                    urlopen(self.base + quote(path, safe='/%'), timeout=5)
                self.assertEqual(caught.exception.code, 404)
        for path in ['/', '/style.css', '/app.js']:
            with urlopen(self.base + path, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIsNone(response.headers.get('Access-Control-Allow-Origin'))

    def test_prefetch_is_private_and_refresh_then_start_reuses_prepared_guide(self):
        self.router._prefetch_enabled = True
        created = self.call('/api/session', {})
        session_id = created['latest_state']['session_id']
        planned = self.call('/api/chat', {'session_id': session_id, 'text': '想看日常生活，有45分钟'})
        queue = planned['latest_state']['pending_queue']
        self.assertGreaterEqual(len(queue), 2)
        first_id, next_id = queue[0]['relic_id'], queue[1]['relic_id']
        started = self.call('/api/event', {'session_id': session_id, 'action': 'start_guide',
                            'payload': {'relic_id': first_id}})
        for _ in range(20):
            restored = self.call('/api/state?session_id=' + session_id)
            if restored['reply_content']['guide_prefetch']['status'] == 'ready':
                break
        self.assertEqual(restored['reply_content']['guide_prefetch'], {'status': 'ready', 'relic_id': next_id})
        self.assertEqual(len(restored['latest_state']['visited_relics']), 1)
        self.assertEqual(len(restored['latest_state']['guide_records']), 1)
        self.assertEqual(restored['latest_state']['pending_queue'][0]['relic_id'], next_id)
        self.assertEqual(restored['reply_content']['playback']['current'], started['reply_content']['playback']['current'])
        consumed = self.call('/api/event', {'session_id': session_id, 'action': 'start_guide',
                             'payload': {'relic_id': next_id}})
        self.assertEqual(consumed['reply_content']['status'], 'guide_ready')
        self.assertEqual(len(consumed['latest_state']['visited_relics']), 2)
        self.assertEqual(len(consumed['latest_state']['guide_records']), 2)
        self.assertEqual(self.guide_targets.count(next_id), 1)

    def test_unknown_session_and_invalid_body(self):
        with self.assertRaises(HTTPError) as caught:
            self.call('/api/state?session_id=missing')
        self.assertEqual(caught.exception.code, 404)
        with self.assertRaises(HTTPError) as caught:
            self.call('/api/session', {'unsupported': True})
        self.assertEqual(caught.exception.code, 400)


if __name__ == '__main__':
    unittest.main()
