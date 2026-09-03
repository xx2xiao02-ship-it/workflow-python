from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from workflow_1256.copywriting_skill_governance import runtime_skill_versions


def test_read_only_audited_upstreams_are_not_reported_as_installed() -> None:
    versions = runtime_skill_versions(
        {
            "skills": [
                {
                    "skill_id": "cangjie",
                    "upstream_version": "v2.5.0",
                    "availability": "audited_read_only_not_installed",
                },
                {
                    "skill_id": "nuwa",
                    "upstream_version": "main",
                    "availability": "audited_read_only_not_installed",
                },
                {
                    "skill_id": "human-writing",
                    "upstream_version": "1.1.0",
                    "availability": "audited_read_only_not_installed",
                },
            ]
        }
    )

    assert versions["cangjie_adapter"] == "adapter-unverified"
    assert versions["nuwa_adapter"] == "adapter-unverified"
    assert versions["human_writing"] == "adapter-unverified"


def test_local_and_project_skills_keep_runtime_versions() -> None:
    versions = runtime_skill_versions(
        {
            "skills": [
                {"skill_id": "writing-dna", "upstream_version": "local", "availability": "local_expected"},
                {"skill_id": "copywriting-orchestrator", "upstream_version": "project-v1", "availability": "local"},
            ]
        }
    )

    assert versions["writing_dna"] == "local-writing-dna-skill"
    assert versions["copywriting_orchestrator"] == "project-v1"
