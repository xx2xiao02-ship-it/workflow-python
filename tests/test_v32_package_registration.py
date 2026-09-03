import importlib.util
from pathlib import Path
spec=importlib.util.spec_from_file_location("console", Path("tools/video_production_console.py")); mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
def test_v32_registered_and_v31_default_preserved():
    assert mod.DIRECTOR_PACKAGE_SPECS["v3_2"]["version"] == "3.2"
    assert mod.DIRECTOR_PACKAGE_SPECS["v3"]["version"] == "3.1"
    assert mod.DEFAULT_DIRECTOR_PACKAGE == "v3"
