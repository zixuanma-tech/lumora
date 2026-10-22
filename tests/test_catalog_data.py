"""Delivered 60-relic package adapters."""

import unittest
from unittest.mock import AsyncMock

from catalog_data import (
    CatalogInterestParser,
    JsonGuideDataSource,
    JsonMuseumDataSource,
    catalog_metadata,
    catalog_taxonomy,
)
from route_models import InterestProfile
from session_state import VisitorInfo


class CatalogDataTests(unittest.IsolatedAsyncioTestCase):
    async def test_guide_materials_are_loaded_by_relic_id(self):
        source = JsonGuideDataSource()
        materials = await source.load_materials("NMC-CQ-104")
        self.assertEqual(len(materials), 1)
        self.assertEqual(materials[0].material_id, "NMC-CQ-104-m1")
        self.assertTrue(materials[0].source_refs)

    async def test_missing_official_material_is_explicit(self):
        with self.assertRaisesRegex(ValueError, "没有可用讲解史料"):
            await JsonGuideDataSource().load_materials("NMC-TJ-015")

    async def test_map_builds_snapshot_for_all_data_halls(self):
        snapshot = await JsonMuseumDataSource().load()
        self.assertEqual(len(snapshot.exhibits), 60)
        self.assertEqual({gallery.gallery_id for gallery in snapshot.galleries}, {
            "NMC-EXH-CQ", "NMC-EXH-YQ", "NMC-EXH-FS", "NMC-EXH-TJ",
        })
        self.assertEqual(len(snapshot.edges), 10)
        self.assertEqual(snapshot.edges[0].source, "entrance")
        self.assertEqual(snapshot.edges[0].target, "NMC-EXH-CQ")

    def test_metadata_and_taxonomy_are_from_delivered_package(self):
        metadata = catalog_metadata()
        self.assertEqual(metadata["mode"], "catalog")
        self.assertEqual(len(metadata["relics"]), 60)
        taxonomy = catalog_taxonomy()
        self.assertTrue(taxonomy["craft_types"])
        self.assertIn("龙", taxonomy["aesthetics"])

    async def test_explicit_catalog_labels_skip_model_interest_parsing(self):
        fallback = AsyncMock()
        parser = CatalogInterestParser(catalog_taxonomy(), fallback)
        profile = await parser(visitor_info=VisitorInfo(
            content_interests=["日常生活", "瓷器制作工艺"],
            guide_focus=["龙纹"],
        ))

        self.assertTrue(profile.has_targets)
        self.assertIn(("生活美学",), profile.target_themes)
        self.assertIn(("工艺技术",), profile.target_themes)
        self.assertTrue(profile.target_craft_types or profile.target_categories)
        self.assertIn(("龙",), profile.target_aesthetics)
        fallback.assert_not_awaited()

    async def test_ambiguous_interest_falls_back_to_model_parser(self):
        expected = InterestProfile(target_themes=["模型解析结果"])
        fallback = AsyncMock(return_value=expected)
        parser = CatalogInterestParser(catalog_taxonomy(), fallback)
        info = VisitorInfo(content_interests=["随便看看未知主题"])

        self.assertIs(await parser(visitor_info=info), expected)
        fallback.assert_awaited_once_with(visitor_info=info)


if __name__ == "__main__":
    unittest.main()
