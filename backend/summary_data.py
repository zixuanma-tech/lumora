"""手记基础资料读取；默认按展品 ID 查询项目 data/relics.json。"""

import json
from collections.abc import Mapping
from pathlib import Path

from summary_models import SummaryRelic


class JsonSummaryDataSource:
    """最小 JSON 契约：{relics: [{relic_id, name, era?, gallery_name?}]}。

    只选取请求的展品，按请求顺序返回；馆藏最终结构确定后可替换此适配器。
    不从模型知识补名称，不创建虚构馆藏，不修改源文件。
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else Path(__file__).parent / "data" / "relics.json"

    async def load_relics(self, relic_ids: tuple[str, ...]) -> tuple[SummaryRelic, ...]:
        with self.path.open(encoding="utf-8-sig") as stream:
            data = json.load(stream)
        if not isinstance(data, Mapping) or not isinstance(data.get("relics"), list):
            raise ValueError("展品数据必须包含 relics 列表")
        requested = set(relic_ids)
        found = {}
        for item in data["relics"]:
            if not isinstance(item, Mapping):
                raise ValueError("relics 的每项必须是展品字段字典")
            relic_id = item.get("relic_id")
            if not isinstance(relic_id, str) or not relic_id.strip():
                raise ValueError("展品数据缺少有效的 relic_id")
            relic_id = relic_id.strip()
            if relic_id not in requested:
                continue
            if relic_id in found:
                raise ValueError(f"展品数据包含重复 ID：{relic_id}")
            found[relic_id] = SummaryRelic(
                relic_id, item.get("name"), item.get("era", ""), item.get("gallery_name", ""),
            )
        missing = [relic_id for relic_id in relic_ids if relic_id not in found]
        if missing:
            raise ValueError(f"缺少展品基础资料：{'、'.join(missing)}")
        return tuple(found[relic_id] for relic_id in relic_ids)
