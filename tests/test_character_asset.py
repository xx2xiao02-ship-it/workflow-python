from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from workflow_1256.character_asset import build_character_asset_plan, inject_character_asset_refs


class CharacterAssetTests(unittest.TestCase):
    def test_plan_uses_style_refs_once(self) -> None:
        value = build_character_asset_plan(
            {"movie_outline": {"protagonist": "林夏，城市分析师"}},
            style_ref_images=["https://style.example/a.png"],
        )
        self.assertEqual(value["status"], "planned")
        self.assertEqual(value["n"], 1)
        self.assertEqual(value["canvas"]["aspect_ratio"], "9:16")

    def test_plan_uses_selected_landscape_canvas(self) -> None:
        value = build_character_asset_plan(
            {"movie_outline": {"protagonist": "林夏"}},
            style_ref_images=["https://style.example/a.png"], canvas="16:9",
        )
        self.assertEqual(value["canvas"]["width"], 1920)
        self.assertIn("横版 16:9", value["prompt"])

    def test_missing_style_refs_blocks(self) -> None:
        value = build_character_asset_plan(
            {"movie_outline": {"protagonist": "林夏"}}, style_ref_images=[]
        )
        self.assertEqual(value["status"], "blocked")

    def test_local_file_is_registered_for_upload_not_misrepresented_as_url(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "style.png"
            path.write_bytes(b"png")
            value = build_character_asset_plan(
                {"movie_outline": {"protagonist": "林夏"}}, style_ref_images=[str(path)]
            )
        self.assertEqual(value["status"], "planned_local_upload")
        self.assertEqual(value["image_urls"], [])
        self.assertEqual(len(value["local_file_paths"]), 1)

    def test_character_ref_precedes_style_only_for_person_shot(self) -> None:
        self.assertEqual(inject_character_asset_refs(["style"], character_asset_url="character", has_character=True), ["character", "style"])
        self.assertEqual(inject_character_asset_refs(["style"], character_asset_url="character", has_character=False), ["style"])
