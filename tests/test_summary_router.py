"""第四算子经中央路由的集成边界测试。"""

import json
import unittest
from unittest.mock import AsyncMock

from router import Router, RouterServices
from session_state import SessionState
from summary_models import (
    SummaryDraft, SummaryInsight, SummaryRelic, SummaryReview, SummaryStop,
)


class SummaryRouterTests(unittest.IsolatedAsyncioTestCase):
    def make_router(self):
        source = type("Source", (), {})()

        async def load_relics(relic_ids):
            return tuple(SummaryRelic(relic_id, f"展品-{relic_id}") for relic_id in relic_ids)

        source.load_relics = load_relics

        async def compose(*, request):
            first = request.visited_relic_ids[0]
            guide_id = next(item.guide_id for item in request.guide_records if item.relic_id == first)
            return SummaryDraft(
                "我的探索手记", "这次游览留下了新的线索。",
                tuple(SummaryStop(item, f"回顾 {item}") for item in request.visited_relic_ids),
                (SummaryInsight("发现", "讲解中的发现。", (first,), (guide_id,)),),
            )

        return Router(RouterServices(
            summary_data_source=source,
            summary_composer=compose,
            summary_reviewer=AsyncMock(return_value=SummaryReview(True)),
        ))

    def add_visit(self, router):
        router.create_session(session_id="s")
        session = router._sessions["s"]
        session.state.replace_pending_queue(["a"])
        session.state.complete_visit("a", "完整讲解。")
        return session

    async def test_generate_summary_after_finish_returns_json_and_preserves_state(self):
        router = self.make_router()
        session = self.add_visit(router)
        session.state.finish()
        before = session.state.to_dict()

        response = await router.dispatch_action("s", "generate_summary", {
            "feedback": {"favorite_relic_id": "a", "reflection": "  很有意思。  "},
        })

        self.assertEqual(response["reply_content"]["status"], "summary_ready")
        self.assertEqual(response["reply_content"]["summary"]["status"], "generated")
        self.assertEqual(response["reply_content"]["summary"]["feedback"]["reflection"], "  很有意思。  ")
        self.assertEqual(session.state.to_dict(), before)
        json.dumps(response, ensure_ascii=False, allow_nan=False)

    async def test_finish_can_generate_summary_and_repeated_finish_does_not_duplicate_it(self):
        router = self.make_router()
        session = self.add_visit(router)

        response = await router.dispatch_action("s", "finish", {
            "feedback": {"favorite_relic_id": "a"},
        })
        self.assertEqual(response["latest_state"]["status"], "finished")
        self.assertEqual(response["reply_content"]["summary"]["status"], "generated")

        response = await router.dispatch_action("s", "finish", {
            "feedback": {"favorite_relic_id": "a"},
        })
        self.assertEqual(response["latest_state"]["status"], "finished")
        self.assertEqual(response["reply_content"]["summary"]["status"], "generated")

    async def test_summary_requires_finished_session(self):
        router = self.make_router()
        self.add_visit(router)
        response = await router.dispatch_action("s", "generate_summary")
        self.assertEqual(response["reply_content"]["status"], "error")
        self.assertEqual(response["reply_content"]["error"]["code"], "session_not_finished")


if __name__ == "__main__":
    unittest.main()
