"""Delivered 60-relic package adapters."""

import unittest

from catalog_data import (
    JsonGuideDataSource,
    JsonMuseumDataSource,
    catalog_metadata,
    catalog_taxonomy,
)


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


if __name__ == "__main__":
    unittest.main()
