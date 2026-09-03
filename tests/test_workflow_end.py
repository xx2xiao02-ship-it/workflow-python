from __future__ import annotations

import pytest

from workflow_1256.workflow_end import WorkflowEndValidationError, run_workflow_end


def test_workflow_end_renders_save_draft_url_as_content() -> None:
    result = run_workflow_end({"draft_url": "http://mate.local/draft?draft_id=one"})

    assert result == {"content": "http://mate.local/draft?draft_id=one"}


def test_workflow_end_accepts_nested_input() -> None:
    assert run_workflow_end({"params": {"_input": {"draft_url": "draft://new"}}}) == {
        "content": "draft://new"
    }


@pytest.mark.parametrize("params", [{}, {"draft_url": ""}, {"draft_url": 1}])
def test_workflow_end_rejects_missing_final_draft(params) -> None:
    with pytest.raises(WorkflowEndValidationError, match="draft_url"):
        run_workflow_end(params)
