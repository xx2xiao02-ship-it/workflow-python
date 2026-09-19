"""Explicit local-only import from the audited console; never import old modules."""
from __future__ import annotations

import argparse
import ast
import base64
import copy
import json
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from topic_migration.config_service import FIXED_SETTINGS, SERVICE_CATALOG, ConfigService, _dpapi


def literal_tables(source: Path) -> dict:
    warnings.filterwarnings("ignore", category=SyntaxWarning)
    constants = {}
    for relative in ("src/workflow_1256/ark_tts_transport.py", "src/workflow_1256/sound_effect_production.py"):
        tree = ast.parse((source / relative).read_text(encoding="utf-8-sig"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                try:
                    constants[node.targets[0].id] = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    pass

    class Resolve(ast.NodeTransformer):
        def visit_Name(self, node):
            if node.id in constants:
                return ast.copy_location(ast.Constant(constants[node.id]), node)
            return node

    requested = {"API_MANAGEMENT_SPECS", "API_MANAGEMENT_GROUP_RUNTIME", "API_MANAGEMENT_SECRET_RUNTIME"}
    result = {}
    tree = ast.parse((source / "tools/video_production_console.py").read_text(encoding="utf-8-sig"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id in requested:
            result[node.targets[0].id] = ast.literal_eval(Resolve().visit(copy.deepcopy(node.value)))
    if set(result) != requested:
        raise ValueError("source_mapping_missing")
    return result


def read_legacy_secrets(path: Path) -> dict:
    sealed = base64.b64decode(path.read_text(encoding="ascii").strip(), validate=True)
    if sealed.startswith((b"M1:", b"U1:")):
        sealed = sealed[3:]
    raw = json.loads(_dpapi(sealed, protect=False).decode("utf-8"))
    if not isinstance(raw.get("channels"), dict):
        raise ValueError("source_secret_format_invalid")
    return raw["channels"]


def prepare_import(source: Path, config: dict, secrets: dict) -> tuple[dict, dict, list]:
    tables = literal_tables(source)
    specs = {s["id"]: s for s in tables["API_MANAGEMENT_SPECS"]}
    aliases = {"story-writing": "story-model", "visual-guidance": "directors-v2",
               "image-generation": "image2", "video-generation": "seedance", "digital-human": "runninghub",
               "tts": "ark-tts", "sound-effect": "seed-audio", "bgm-audio": "ark-bgm", "audio-storage": "tts-object-storage"}
    groups = config.get("groups", {})
    channels = list(dict.fromkeys([*SERVICE_CATALOG, *groups, *secrets]))
    result, audit = {}, []
    for channel in channels:
        base = channel.split("__custom_", 1)[0]
        if base not in SERVICE_CATALOG or (base == "tts" and channel != base):
            audit.append({"channel": channel, "status": "historical_excluded"})
            continue
        spec = specs[aliases.get(base, base)]
        settings = copy.deepcopy(groups.get(channel, {}))
        fields = tables["API_MANAGEMENT_GROUP_RUNTIME"].get(base, {})
        if channel == base:
            settings.setdefault("endpoint", spec.get("default_url", ""))
            if spec.get("default_region"):
                settings.setdefault("region", spec["default_region"])
            if spec.get("default_model") and not settings.get("model_slots"):
                settings.setdefault("primary_model", spec["default_model"])
        if base in FIXED_SETTINGS:
            for field in ("endpoint", "region", "app_id", "primary_model", "backup_model", "model_slots", "model_names", "model_order"):
                settings.pop(field, None)
            settings.update(FIXED_SETTINGS[base])
        keys = copy.deepcopy(secrets.get(channel, {}))
        for field, value in list(keys.items()):
            if isinstance(value, str) and "://" in value:
                keys.pop(field)
                audit.append({"channel": channel, "field": field, "status": "invalid_credential_url_skipped", "length": len(value)})
        # Preserve explicit credential inheritance as a reference in the target snapshot.
        result[channel] = {"settings": settings, "secrets": keys}
        audit.append({"channel": channel, "settings_fields": sorted(settings),
                      "transport_mapping": fields,
                      "credential_mapping": tables["API_MANAGEMENT_SECRET_RUNTIME"].get(base, {}),
                      "source": "page_config_and_encrypted_channels", "connection_verified": False})
    routing = {field: copy.deepcopy(config.get(field, {})) for field in ("api_groups", "api_functions", "api_channels", "removed_channels")}
    return result, routing, audit


def run(args) -> dict:
    source = Path(args.source_project).resolve()
    legacy = Path(args.source_runtime).resolve()
    config = json.loads((legacy / "api_management.json").read_text(encoding="utf-8"))
    secrets = read_legacy_secrets(legacy / "api_management_secrets.bin")
    channels, routing, audit = prepare_import(source, config, secrets)
    target = ConfigService(args.target_root)
    if target.root.resolve().is_relative_to(source) or any((root / "service.lock").exists() for root in (target.root, target.root.parent)):
        raise ValueError("target_not_independent_or_service_locked")
    for channel, payload in channels.items():
        target._validated_channel(channel, payload)
    if not args.apply:
        return {"status": "dry_run", "channels": list(channels), "audit": audit, "external_requests": False}
    merged = target.merge_import(channels, routing=routing)
    # A second independent instance verifies the persisted settings and masked states.
    restarted = ConfigService(target.root).public_snapshot()
    expected = {g["channel_id"]: g for g in merged["snapshot"]["groups"]}
    for group in restarted["groups"]:
        prior = expected[group["channel_id"]]
        if any(group[key] != prior[key] for key in ("settings", "credential_status", "credential_fields")):
            raise ValueError("restart_comparison_failed")
    return {"status": "imported", "restart_instance_verified": True,
            "source_issues": [item for item in audit if "status" in item],
            "conflicts": merged["conflicts"], "external_requests": False,
            "services": [{k: g[k] for k in ("channel_id", "provider", "config_status", "credential_status", "business_status", "connection_status")} for g in restarted["groups"]]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-project", required=True)
    parser.add_argument("--source-runtime", required=True)
    parser.add_argument("--target-root", required=True)
    parser.add_argument("--apply", action="store_true")
    try:
        result = run(parser.parse_args())
    except Exception as exc:
        # Exception values and tracebacks may contain credential-bearing inputs.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "credentials_printed": False}))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
