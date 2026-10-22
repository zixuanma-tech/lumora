"""JSON 展品基础资料读取测试，测试数据只存在于临时目录。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from session_state import SessionState
from summary_data import JsonSummaryDataSource
from summary_generator import op_generate_summary
from summary_models import SummaryRelic


class SummaryDataTests(unittest.IsolatedAsyncioTestCase):
    async def load(self, data, ids=("a", "b")):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "relics.json"
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8-sig")
            return await JsonSummaryDataSource(path).load_relics(ids)

    async def test_lookup_uses_ids_preserves_order_and_ignores_extra_fields(self):
        result = await self.load({"relics": [
            {"relic_id": "b", "name": "乙", "unneeded": {"anything": True}},
            {"relic_id": "unused", "name": "未参观"},
            {"relic_id": "a", "name": "甲", "era": "唐", "gallery_name": "一厅"},
        ]})
        self.assertEqual(result, (SummaryRelic("a", "甲", "唐", "一厅"), SummaryRelic("b", "乙")))

    async def test_missing_duplicate_and_invalid_data_fail_explicitly(self):
        for data in (
            [], {}, {"relics": {}}, {"relics": ["text"]},
            {"relics": [{"name": "无ID"}]},
            {"relics": []}, {"relics": [{"relic_id": "a", "name": "甲"}]},
            {"relics": [{"relic_id": "a", "name": "甲"}, {"relic_id": "a", "name": "重复"}]},
            {"relics": [{"relic_id": "a", "name": " "}]},
        ):
            with self.subTest(data=data), self.assertRaises(ValueError):
                await self.load(data)

    async def test_missing_file_is_not_replaced_with_fictional_data(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "absent.json"
            with self.assertRaises(FileNotFoundError):
                await JsonSummaryDataSource(path).load_relics(("a",))
            self.assertFalse(path.exists())

    async def test_default_source_uses_project_data_and_stops_before_model_if_empty(self):
        expected = Path(__file__).resolve().parents[1] / "data" / "relics.json"
        self.assertEqual(JsonSummaryDataSource().path, expected)
        state = SessionState()
        state.replace_pending_queue(["missing-summary-test-id"])
        state.complete_visit("missing-summary-test-id", "讲解。")
        before = state.to_dict()
        composer, reviewer = AsyncMock(), AsyncMock()
        with self.assertRaisesRegex(ValueError, "缺少展品基础资料"):
            await op_generate_summary(state, composer=composer, reviewer=reviewer)
        composer.assert_not_awaited()
        reviewer.assert_not_awaited()
        self.assertEqual(state.to_dict(), before)


if __name__ == "__main__":
    unittest.main()
