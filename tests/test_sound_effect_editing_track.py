from workflow_1256.editing_layer import _track_role
from types import SimpleNamespace
def test_sound_effect_has_independent_track():
    assert _track_role(SimpleNamespace(role="sound_effect", asset_type="audio")) == "音效"
