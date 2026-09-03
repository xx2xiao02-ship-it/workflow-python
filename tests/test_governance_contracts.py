from __future__ import annotations

import json
from pathlib import Path
import unittest

from workflow_1256.governance import (
    AssetRecord,
    GovernanceContractError,
    TimeWindow,
    assert_transition,
    build_module_governance_snapshot,
    invalidation_scope,
    validate_director_convergence,
    build_director_submodule_closure,
    DIRECTOR_SUBMODULE_NODE_IDS,
    get_module_nodes,
    validate_node_registry,
)
from tests._current_governance_fixture import build_current_governance_fixture


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas"


class GovernanceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = build_current_governance_fixture()

    def test_current_stt_fixture_has_locked_timeline_and_mapping(self) -> None:
        manifest = self.manifest
        self.assertEqual(manifest.project_id, "current-stt-contract-fixture")
        self.assertEqual(manifest.run_id, "current-stt-run")
        self.assertEqual(manifest.director.status, "DIRECTOR_LOCKED")
        self.assertEqual(manifest.director.total_timeline.to_dict(), {
            "start_us": 0,
            "end_us": 6_000_000,
            "duration_us": 6_000_000,
        })
        self.assertEqual(len(manifest.director.groups), 2)
        self.assertEqual(len(manifest.director.shots), 3)
        self.assertEqual(len(manifest.director.captions), 3)
        self.assertEqual(len(manifest.assets.records), 1)
        self.assertEqual(len(manifest.edit.bindings), 1)
        self.assertEqual(len(manifest.field_mappings), 1)
        self.assertEqual(manifest.field_mappings[0]["ref_node"], "105880")
        self.assertTrue(all(item["source_path"] and item["target_path"] for item in manifest.field_mappings))
        self.assertEqual(
            [group.group_id for group in manifest.director.groups],
            ["g01", "g02"],
        )

    def test_shot_ids_and_captions_preserve_source_order(self) -> None:
        manifest = self.manifest
        shot_ids = [shot.shot_id for shot in manifest.director.shots]
        self.assertEqual(len(shot_ids), len(set(shot_ids)))
        self.assertEqual(shot_ids[0], "g01_s01")
        self.assertEqual(shot_ids[-1], "g02_s01")
        caption_ids = [caption.caption_id for caption in manifest.director.captions]
        self.assertEqual(caption_ids, [f"caption_{index:03d}" for index in range(1, 4)])

    def test_fixture_asset_and_edit_binding_follow_locked_first_shot(self) -> None:
        records = self.manifest.assets.records
        self.assertEqual(len(records), 1)
        record = records[0]
        binding = self.manifest.edit.bindings[0]
        self.assertEqual(record.role, "primary_visual")
        self.assertEqual(record.requirement_id, binding.requirement_id)
        self.assertEqual(record.shot_ids, [binding.shot_id])
        self.assertEqual(record.actual_timeline, binding.edit_timeline)

    def test_schema_files_are_valid_and_validate_manifest_parts(self) -> None:
        import jsonschema

        schemas = {
            "director_locked_manifest.schema.json": self.manifest.director.to_dict(),
            "asset_registry.schema.json": self.manifest.assets.to_dict(),
            "edit_manifest.schema.json": self.manifest.edit.to_dict(),
        }
        for filename, instance in schemas.items():
            schema = json.loads((SCHEMAS / filename).read_text(encoding="utf-8"))
            jsonschema.validate(instance, schema)
        project_schema = json.loads(
            (SCHEMAS / "project_manifest.schema.json").read_text(encoding="utf-8")
        )
        resolver = jsonschema.RefResolver(
            (SCHEMAS / "project_manifest.schema.json").as_uri(),
            project_schema,
            store={
                schema_path.name: json.loads(schema_path.read_text(encoding="utf-8"))
                for schema_path in SCHEMAS.glob("*_manifest.schema.json")
            },
        )
        jsonschema.validate(self.manifest.to_dict(), project_schema, resolver=resolver)

    def test_timeline_and_state_boundaries_are_enforced(self) -> None:
        with self.assertRaises(GovernanceContractError):
            TimeWindow(100, 100)
        with self.assertRaises(GovernanceContractError):
            TimeWindow(200, 100)
        assert_transition("director", "REQUIREMENTS_READY", "DIRECTOR_LOCKED")
        with self.assertRaises(GovernanceContractError):
            assert_transition("director", "DIRECTOR_DRAFT", "DIRECTOR_LOCKED")

    def test_validated_asset_requires_real_locator(self) -> None:
        with self.assertRaises(GovernanceContractError):
            AssetRecord(
                asset_id="asset.invalid",
                requirement_id="g01_s01.video.aigc",
                group_id="g01",
                shot_ids=["g01_s01"],
                asset_type="video",
                role="primary_visual",
                status="VALIDATED",
                source_node="test",
                source_index=0,
                asset_version="v1",
            )

    def test_invalidation_rules_include_downstream_edit(self) -> None:
        changed = invalidation_scope(
            "tts_result_changed",
            group_id="g03",
        )
        self.assertIn("assets", changed["scope"])
        self.assertIn("edit", changed["scope"])
        self.assertEqual(changed["group_id"], "g03")

    def test_node_registry_covers_all_61_executable_nodes_and_three_modules(self) -> None:
        summary = validate_node_registry()
        self.assertEqual(summary["total_nodes"], 61)
        self.assertEqual(summary["module_counts"], {
            "编导层": 15,
            "素材层": 27,
            "剪辑层": 19,
        })
        self.assertEqual(summary["parent_node_count"], 9)

    def test_module_snapshot_exposes_mapping_and_production_gates(self) -> None:
        snapshot = build_module_governance_snapshot(self.manifest)
        self.assertEqual(snapshot["observed_data"], {
            "director_groups": 2,
            "director_shots": 3,
            "captions": 3,
            "asset_records": 1,
            "edit_bindings": 1,
        })
        self.assertTrue(snapshot["gate_results"]["编导层_mapping_gate"])
        self.assertTrue(snapshot["gate_results"]["素材层_mapping_gate"])
        self.assertFalse(snapshot["gate_results"]["素材层_production_gate"])
        self.assertTrue(snapshot["gate_results"]["剪辑层_mapping_gate"])
        self.assertFalse(snapshot["gate_results"]["剪辑层_production_gate"])

    def test_director_convergence_locks_current_stt_fixture_without_model_payload(self) -> None:
        report = validate_director_convergence(self.manifest.director)
        self.assertEqual(report.status, "DIRECTOR_LOCKED")
        self.assertEqual(report.evidence_level, "real_fixture_observed_with_redaction")
        self.assertEqual(report.counts["subplans"], 4)
        self.assertEqual(report.counts["visual_specs"], 3)
        self.assertEqual(report.counts["route_specs"], 3)

    def test_director_convergence_schema_matches_real_projection(self) -> None:
        import jsonschema
        from workflow_1256.governance import DirectorConvergenceInput

        data = DirectorConvergenceInput.from_manifest(self.manifest.director)
        schema = json.loads(
            (SCHEMAS / "director_convergence.schema.json").read_text(encoding="utf-8")
        )
        jsonschema.validate(
            {
                "project_id": data.project_id,
                "run_id": data.run_id,
                "plan_version": data.plan_version,
                "subplans": [item.to_dict() for item in data.subplans],
                "visual_specs": [item.to_dict() for item in data.visual_specs],
                "route_specs": [item.to_dict() for item in data.route_specs],
            },
            schema,
        )

    def test_director_convergence_rejects_prompt_payload(self) -> None:
        from workflow_1256.governance import DirectorConvergenceInput, DirectorVisualSpec

        data = DirectorConvergenceInput.from_manifest(self.manifest.director)
        first = data.visual_specs[0]
        with self.assertRaises(GovernanceContractError):
            DirectorVisualSpec(
                shot_id=first.shot_id,
                group_id=first.group_id,
                shot_index=first.shot_index,
                status="READY",
                fields={"prompt": "不得进入编导锁定对象"},
                observed_fields=first.observed_fields,
                source_nodes=first.source_nodes,
            )

    def test_director_convergence_rejects_version_mismatch_and_order_change(self) -> None:
        from dataclasses import replace
        from workflow_1256.governance import DirectorConvergenceInput, DirectorSubplanReceipt

        data = DirectorConvergenceInput.from_manifest(self.manifest.director)
        bad_subplan = DirectorSubplanReceipt(
            name="timing_subtitles",
            project_id=data.project_id,
            run_id=data.run_id,
            plan_version="wrong-version",
            status="OBSERVED_REDACTED",
            source_nodes=("159953",),
        )
        bad_data = replace(data, subplans=(data.subplans[0], bad_subplan, *data.subplans[2:]))
        with self.assertRaises(GovernanceContractError):
            validate_director_convergence(self.manifest.director, bad_data)

        reordered = replace(data, visual_specs=tuple(reversed(data.visual_specs)))
        with self.assertRaises(GovernanceContractError):
            validate_director_convergence(self.manifest.director, reordered)

    def test_director_submodules_have_node_level_closure_without_changing_manifest(self) -> None:
        before = self.manifest.director.to_dict()
        report = build_director_submodule_closure(self.manifest.director)
        after = self.manifest.director.to_dict()

        self.assertTrue(report.mapping_gate)
        self.assertFalse(report.production_gate)
        self.assertEqual(report.overall_status, "PARTIAL")
        self.assertEqual(report.node_count, 15)
        self.assertEqual(before, after)
        self.assertEqual(
            list(report.blocking_nodes),
            ["129109", "102833", "178142", "159953", "105880", "103964", "197742", "116616", "139488"],
        )
        self.assertEqual(
            [item.name for item in report.submodules],
            list(DIRECTOR_SUBMODULE_NODE_IDS),
        )

    def test_prompt_compiler_nodes_are_material_layer_not_director_closure(self) -> None:
        material_ids = {item.node_id for item in get_module_nodes("素材层")}
        director_ids = {item.node_id for item in get_module_nodes("编导层")}
        self.assertTrue({"195692", "181639", "170263", "113743"}.issubset(material_ids))
        self.assertTrue({"195692", "181639", "170263", "113743"}.isdisjoint(director_ids))

    def test_director_submodule_closure_report_schema_matches_real_case(self) -> None:
        import jsonschema

        schema = json.loads(
            (SCHEMAS / "director_submodule_closure.schema.json").read_text(encoding="utf-8")
        )
        report = build_director_submodule_closure(self.manifest.director)
        jsonschema.validate(report.to_dict(), schema)


if __name__ == "__main__":
    unittest.main()
