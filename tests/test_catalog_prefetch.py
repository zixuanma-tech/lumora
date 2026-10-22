"""真实汝窑数据的预生成回归；模型替身不发出网络请求。"""

import asyncio
from collections import Counter
from dataclasses import replace
import json
import unittest

from catalog_data import JsonMuseumDataSource, build_catalog_services
from router import Router


class Clock:
    def __init__(self):
        self.seconds = 1000.0

    def __call__(self):
        return self.seconds

    def advance(self, seconds):
        self.seconds += seconds


class CatalogPrefetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_ru_ware_survives_budget_replan_without_model_retry(self):
        calls = Counter()
        guide_targets = Counter()

        async def model(**kwargs):
            name = kwargs["response_format"]["name"]
            calls[name] += 1
            if name == "interest_profile":
                if calls[name] > 1:
                    raise TimeoutError("时间重算不应重新请求兴趣模型")
                return {
                    "target_eras": [], "target_regions": [],
                    "target_categories": ["瓷"], "target_craft_types": [],
                    "target_themes": [["工艺技术"]], "target_aesthetics": [],
                    "target_cultural_ip": [], "target_audience": [],
                    "query_text": "瓷器制作工艺",
                }
            if name == "guide_draft":
                payload = json.loads(kwargs["user_prompt"])
                target = payload["request"]["relic_id"]
                guide_targets[target] += 1
                material = payload["selection"]["materials"][0]
                return {
                    "relic_id": target, "segments": [material["text"]],
                    "used_material_ids": [material["material_id"]],
                }
            raise AssertionError(f"意外的模型职责：{name}")

        services = build_catalog_services(model_call=model)
        snapshot = await JsonMuseumDataSource().load()
        first_id, ru_ware_id = "NMC-CQ-105", "NMC-CQ-107"
        # 缩小候选以固定队列；仍使用交付数据中的展品、材料、展厅和空间通路。
        snapshot = replace(snapshot, exhibits=tuple(
            item for item in snapshot.exhibits
            if item.exhibit_id in (first_id, ru_ware_id)
        ))
        clock = Clock()
        router = Router(replace(services, snapshot=snapshot, data_source=None), clock=clock)
        self.addAsyncCleanup(router.aclose)
        router.create_session(
            {"content_interests": ["瓷器制作工艺"], "remaining_minutes": 60},
            session_id="ru-ware", current_node_id="entrance", exit_node_id="exit",
        )
        planned = await router.dispatch_action("ru-ware", "replan")
        self.assertEqual(planned["reply_content"]["status"], "planned", planned["reply_content"])
        self.assertEqual(
            [item["relic_id"] for item in planned["latest_state"]["pending_queue"]],
            [first_id, ru_ware_id],
        )
        first = await router.dispatch_action("ru-ware", "start_guide", {"relic_id": first_id})
        self.assertEqual(first["reply_content"]["status"], "guide_ready")
        for _ in range(100):
            ready = await router.dispatch_action("ru-ware", "get_state")
            if ready["reply_content"]["guide_prefetch"] == {"status": "ready", "relic_id": ru_ware_id}:
                break
            await asyncio.sleep(0)
        else:
            self.fail("汝窑天青釉盘讲解未预生成完成")
        self.assertEqual(guide_targets[ru_ware_id], 1)
        self.assertEqual(len(ready["latest_state"]["guide_records"]), 1)

        route = ready["reply_content"]["route"]
        first_dwell = next(item["dwell_time_min"] for item in route["exhibits"]
                           if item["relic_id"] == first_id)
        estimate = route["time_bill"]["total_min"] - first_dwell
        # 触发开始前的预算重算；实际路线仍可保留汝窑这件顺路重点展品。
        remaining = estimate - 0.1
        self.assertGreater(remaining, 0)
        clock.advance((60 - remaining) * 60)

        consumed = await router.dispatch_action("ru-ware", "start_guide", {"relic_id": ru_ware_id})
        self.assertEqual(consumed["reply_content"]["status"], "guide_ready", consumed["reply_content"])
        self.assertIn("route_planned", consumed["reply_content"]["steps"])
        self.assertEqual(calls["interest_profile"], 1)
        self.assertEqual(guide_targets[ru_ware_id], 1)
        self.assertEqual(len(consumed["latest_state"]["guide_records"]), 2)
        self.assertIn("天青", consumed["reply_content"]["guide"]["guide_text"])


if __name__ == "__main__":
    unittest.main()
