"""独立账号知识库 V2 Lab HTTP 服务。

默认监听 127.0.0.1:8790，与现有 8768 生产控制台完全分离。服务只读写
配置的 Obsidian Vault 和 V2 产物目录；默认只执行本地检索和契约组装。
只有请求显式开启 runtime provider 时才会调用模型；没有注入 provider 时，
文案生成接口返回明确的 424 阻断。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from workflow_1256.account_knowledge import (  # noqa: E402
    AccountKnowledgeModelProvider,
    AccountKnowledgeModelError,
    CopywritingProviderRequired,
    LabArtifactStore,
    ObsidianRepository,
    build_account_distillation,
    build_topic_plan_from_index,
    apply_manual_grade,
    run_copywriting_provider,
    write_account_distillation,
    run_style_package_upgrade,
)
from workflow_1256.account_knowledge.indexer import LocalKnowledgeIndex  # noqa: E402
from workflow_1256.account_knowledge.retrieval import AccountKnowledgeRetriever  # noqa: E402
from workflow_1256.copywriting_knowledge_v2 import promote_v2_draft  # noqa: E402
from workflow_1256.topic_knowledge_v2 import EventCard, RetrievalPolicy  # noqa: E402
from workflow_1256.style_package_store import StylePackageStore  # noqa: E402


MAX_BODY_BYTES = 2 * 1024 * 1024
DEFAULT_PORT = 8790
LAB_UI_PATH = Path(__file__).with_name("account_knowledge_lab_ui.html")


class LabServiceError(RuntimeError):
    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class _InjectedProvider:
    def __init__(self, result: Mapping[str, Any]) -> None:
        self.result = dict(result)

    def draft(self, *, topic_plan, style_profile_id, requirements):
        return self.result


class LabService:
    def __init__(self, *, vault_path: Path | str | None = None, output_path: Path | str | None = None) -> None:
        try:
            self.repository = ObsidianRepository(vault_path) if vault_path else ObsidianRepository.from_env()
        except Exception as exc:
            raise LabServiceError(str(exc), status=503) from exc
        default_output = os.environ.get("ACCOUNT_KNOWLEDGE_LAB_OUTPUT_PATH", "outputs/account_knowledge_lab")
        self.artifacts = LabArtifactStore(output_path or default_output)
        self.index = LocalKnowledgeIndex()
        self.index_ready = False
        self._model_provider: AccountKnowledgeModelProvider | None = None

    def _runtime_provider(self) -> AccountKnowledgeModelProvider:
        """按显式模型路由创建 provider，不在健康检查或索引时触发模型请求。"""

        if self._model_provider is None:
            style_root = Path(
                os.environ.get(
                    "ACCOUNT_KNOWLEDGE_STYLE_PACKAGE_ROOT",
                    str(PROJECT_ROOT / "outputs" / "style_packages"),
                )
            ).expanduser()
            api_config = Path(
                os.environ.get(
                    "API_MANAGEMENT_CONFIG_PATH",
                    str(PROJECT_ROOT / ".runtime-governance-live" / "data" / "VideoProductionConsole" / "api_management.json"),
                )
            ).expanduser()
            style_store = StylePackageStore(style_root)
            try:
                self._model_provider = AccountKnowledgeModelProvider.from_runtime_config(
                    style_profile_loader=style_store.get_profile,
                    api_management_config_path=api_config,
                    use_api_management_runtime=True,
                )
            except Exception as exc:
                raise LabServiceError(
                    f"账号知识库模型 provider 未配置：{type(exc).__name__}", status=503
                ) from exc
        return self._model_provider

    def rebuild_index(self, account_ids: list[str] | None = None) -> dict[str, Any]:
        count = self.index.rebuild_from_repository(self.repository, account_ids=account_ids)
        index_path = self.repository.vault_path / "账号知识库" / "generated" / "knowledge-index.json"
        self.index.dump(index_path)
        self.index_ready = True
        return {"status": "ready", "documents": count, "index_path": str(index_path)}

    def health(self) -> dict[str, Any]:
        return {
            "status": "ready",
            "mode": "offline_lab",
            "production_port": 8768,
            "production_chain_touched": False,
            "index_ready": self.index_ready,
        }

    def topic_plan(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        event_payload = payload.get("event_card")
        if not isinstance(event_payload, Mapping):
            raise LabServiceError("event_card 必须是对象")
        try:
            event = EventCard(**dict(event_payload))
            policy_payload = payload.get("retrieval_policy")
            policy = RetrievalPolicy(**dict(policy_payload)) if isinstance(policy_payload, Mapping) else RetrievalPolicy()
            if not self.index_ready:
                self.rebuild_index()
            raw_reference_ids = payload.get("reference_account_ids") or []
            if not isinstance(raw_reference_ids, (list, tuple)):
                raise LabServiceError("reference_account_ids 必须是数组")
            plan = build_topic_plan_from_index(
                index=self.index,
                event_card=event,
                target_account_id=str(payload.get("target_account_id") or ""),
                reference_account_ids=list(raw_reference_ids),
                retrieval_policy=policy,
            )
            if payload.get("synthesize") is True:
                plan = self._runtime_provider().synthesize_topic(topic_plan=plan)
            path = self.artifacts.save_topic_plan(plan)
            return {
                "status": "ready",
                "topic_plan": plan,
                "artifact_path": str(path),
                "provider_mode": "runtime" if payload.get("synthesize") is True else "local_retrieval",
            }
        except LabServiceError:
            raise
        except Exception as exc:
            raise LabServiceError(str(exc)) from exc

    def negative_search(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """显式避错检索：只返回 C 级作品，不参与正常选题角度生成。"""

        account_id = str(payload.get("account_id") or "").strip()
        query = str(payload.get("query") or "").strip()
        if not account_id or not query:
            raise LabServiceError("account_id、query 不能为空")
        if not self.index_ready:
            self.rebuild_index()
        try:
            results = AccountKnowledgeRetriever(self.index).retrieve_negative_cases(
                account_id=account_id,
                query=query,
                top_k=int(payload.get("top_k") or 5),
                published_after=str(payload.get("published_after") or ""),
            )
        except Exception as exc:
            raise LabServiceError(str(exc)) from exc
        return {
            "status": "ready",
            "account_id": account_id,
            "results": results,
            "provider_mode": "local_retrieval",
        }

    def draft(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        topic_plan = payload.get("topic_plan")
        if not isinstance(topic_plan, Mapping):
            raise LabServiceError("topic_plan 必须是对象")
        style_profile_id = str(payload.get("style_profile_id") or "").strip()
        if not style_profile_id:
            raise LabServiceError("style_profile_id 不能为空")
        provider_result = payload.get("provider_result")
        runtime_mode = str(payload.get("provider_mode") or "").strip().lower() == "runtime"
        provider = (
            self._runtime_provider()
            if runtime_mode
            else _InjectedProvider(provider_result)
            if isinstance(provider_result, Mapping)
            else None
        )
        requirements = dict(payload.get("requirements") if isinstance(payload.get("requirements"), Mapping) else {})
        if isinstance(payload.get("style_profile"), Mapping):
            if runtime_mode and str(payload["style_profile"].get("review_status") or "").strip().upper() != "APPROVED":
                raise LabServiceError("runtime provider 只能使用已审核通过的 style_profile", status=400)
                requirements["style_profile"] = dict(payload["style_profile"])
        account_dna_status = "MISSING"
        account_dna_used = False
        target_account_id = str(topic_plan.get("target_account_id") or "").strip()
        if target_account_id:
            try:
                account_context = self.repository.load_account_dna_context(
                    target_account_id,
                    include_pending=payload.get("include_pending_account_dna") is True,
                )
            except Exception as exc:
                raise LabServiceError(f"账号级 Writing-DNA 读取失败：{type(exc).__name__}", status=409) from exc
            account_dna_status = str(account_context.get("knowledge_status") or "MISSING")
            if account_context.get("available") is True:
                requirements["account_knowledge_context"] = account_context
                account_dna_used = True
        try:
            draft = run_copywriting_provider(
                provider,
                topic_plan=topic_plan,
                style_profile_id=style_profile_id,
                requirements=requirements,
            )
        except CopywritingProviderRequired as exc:
            raise LabServiceError(str(exc), status=424) from exc
        except AccountKnowledgeModelError as exc:
            raise LabServiceError(str(exc), status=502) from exc
        except Exception as exc:
            raise LabServiceError(str(exc)) from exc
        path = self.artifacts.save_draft(draft)
        return {
            "status": "ready",
            "draft": draft,
            "artifact_path": str(path),
            "provider_mode": "runtime" if runtime_mode else "injected_test" if provider else "external",
            "account_knowledge_status": account_dna_status,
            "account_knowledge_context_used": account_dna_used,
        }

    def distill_work(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """显式调用内容分析模型并把单篇作品卡写回 Vault。"""

        account_id = str(payload.get("account_id") or "").strip()
        title = str(payload.get("title") or "").strip()
        source_content = str(payload.get("source_content") or "")
        if not account_id or not title or not source_content.strip():
            raise LabServiceError("account_id、title、source_content 不能为空")
        provider = self._runtime_provider()
        self.repository.initialize_account(account_id)
        try:
            card = provider.distill_work_card(
                account_id=account_id,
                title=title,
                source_content=source_content,
                source_url=str(payload.get("source_url") or ""),
                platform=str(payload.get("platform") or ""),
                published_at=str(payload.get("published_at") or ""),
                source_ref="",
                prompt_version=str(payload.get("prompt_version") or "account-knowledge-v1"),
            )
        except AccountKnowledgeModelError as exc:
            raise LabServiceError(str(exc), status=502) from exc
        except Exception as exc:
            raise LabServiceError(str(exc)) from exc
        source_ref = str(payload.get("source_ref") or f"raw_works/{card.work_id}.txt").strip()
        card.source_ref = source_ref
        card.validate()
        overwrite = payload.get("overwrite") is True
        self.repository.save_raw_source(account_id, source_ref, source_content.encode("utf-8"), overwrite=overwrite)
        card_path = self.repository.save_work_card(card, overwrite=overwrite)
        self.repository.save_work_metadata(
            account_id,
            card.work_id,
            {
                "schema_version": 1,
                "work_id": card.work_id,
                "account_id": account_id,
                "title": card.title,
                "date": card.published_at,
                "platform": card.platform,
                "source_url": card.source_url,
                "source_ref": card.source_ref,
                "content_hash": card.content_hash,
                "article_type": "模型蒸馏待审核",
                "notable": "由内容分析角色模型蒸馏；内容等级和传播等级仍待人工确认",
            },
            overwrite=overwrite,
        )
        return {
            "status": "ready",
            "work_card": card.to_dict(),
            "work_card_path": str(card_path),
            "source_path": str(self.repository.account_dir(account_id) / source_ref),
            "provider_mode": "runtime",
        }

    def distill_account(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """显式调用内容分析模型并把账号级 Writing-DNA 写回 Vault。"""

        account_id = str(payload.get("account_id") or "").strip()
        if not account_id:
            raise LabServiceError("account_id 不能为空")
        self.repository.initialize_account(account_id)
        cards = self.repository.list_work_cards(account_id)
        works: list[dict[str, Any]] = []
        missing_raw: list[str] = []
        for card in cards:
            value = card.to_dict()
            if not card.source_ref:
                missing_raw.append(card.work_id)
                continue
            try:
                value["source_content"] = self.repository.load_raw_source(account_id, card.source_ref)
            except Exception:
                missing_raw.append(card.work_id)
                continue
            works.append(value)
        if missing_raw:
            raise LabServiceError(
                "账号蒸馏要求每篇作品都有可读取的完整原文；缺失作品：" + ", ".join(missing_raw),
                status=409,
            )
        if not works:
            raise LabServiceError("账号没有可蒸馏的作品卡")
        provider = self._runtime_provider()
        try:
            result = provider.distill_account(
                account_id=account_id,
                works=works,
                surface_analysis=payload.get("surface_analysis") if isinstance(payload.get("surface_analysis"), Mapping) else None,
                batch_size=payload.get("batch_size", 8),
            )
            bundle = build_account_distillation(account_id=account_id, works=cards, result=result)
            paths = write_account_distillation(self.repository, bundle, overwrite=payload.get("overwrite") is True)
        except AccountKnowledgeModelError as exc:
            raise LabServiceError(str(exc), status=502) from exc
        except Exception as exc:
            raise LabServiceError(str(exc)) from exc
        return {
            "status": "ready",
            "bundle": bundle,
            "paths": [str(path) for path in paths],
            "provider_mode": "runtime",
        }

    def profile_upgrade(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """离线导入风格包派生作品并生成候选升级包，不调用模型。"""

        account_id = str(payload.get("account_id") or "").strip()
        style_profile_id = str(payload.get("style_profile_id") or "").strip()
        if not account_id or not style_profile_id:
            raise LabServiceError("account_id、style_profile_id 不能为空")
        style_root = Path(
            str(
                payload.get("style_root")
                or os.environ.get(
                    "ACCOUNT_KNOWLEDGE_STYLE_PACKAGE_ROOT",
                    str(PROJECT_ROOT / "outputs" / "style_packages"),
                )
            )
        ).expanduser()
        output_root = Path(
            str(
                payload.get("output_root")
                or os.environ.get("ACCOUNT_KNOWLEDGE_LAB_OUTPUT_PATH", "outputs/account_knowledge_lab")
            )
        ).expanduser()
        try:
            return run_style_package_upgrade(
                self.repository,
                account_id=account_id,
                style_root=style_root,
                style_profile_id=style_profile_id,
                output_root=output_root,
                include_archived=payload.get("include_archived") is not False,
                limit=int(payload["limit"]) if payload.get("limit") is not None else None,
                overwrite=payload.get("overwrite") is True,
            )
        except Exception as exc:
            raise LabServiceError(str(exc)) from exc

    def news_to_copy(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """单入口链路：新闻事实 -> 目标账号知识检索 -> 风格化文案。"""

        event_payload = payload.get("event_card")
        target_account_id = str(payload.get("target_account_id") or "").strip()
        style_profile_id = str(payload.get("style_profile_id") or "").strip()
        if not isinstance(event_payload, Mapping):
            raise LabServiceError("event_card 必须是对象")
        if not target_account_id or not style_profile_id:
            raise LabServiceError("target_account_id、style_profile_id 不能为空")
        try:
            event = EventCard(**dict(event_payload))
            policy_payload = payload.get("retrieval_policy")
            policy = RetrievalPolicy(**dict(policy_payload)) if isinstance(policy_payload, Mapping) else RetrievalPolicy()
            if not self.index_ready:
                self.rebuild_index()
            raw_reference_ids = payload.get("reference_account_ids") or []
            if not isinstance(raw_reference_ids, (list, tuple)):
                raise LabServiceError("reference_account_ids 必须是数组")
            plan = build_topic_plan_from_index(
                index=self.index,
                event_card=event,
                target_account_id=target_account_id,
                reference_account_ids=list(raw_reference_ids),
                retrieval_policy=policy,
            )
            if payload.get("synthesize") is True:
                plan = self._runtime_provider().synthesize_topic(topic_plan=plan)
            topic_artifact_path = self.artifacts.save_topic_plan(plan)
            draft_payload: dict[str, Any] = {
                "topic_plan": plan,
                "style_profile_id": style_profile_id,
                "provider_mode": str(payload.get("provider_mode") or "runtime").strip().lower(),
                "requirements": dict(payload.get("requirements") if isinstance(payload.get("requirements"), Mapping) else {}),
            }
            if payload.get("include_pending_account_dna") is True:
                draft_payload["include_pending_account_dna"] = True
            if isinstance(payload.get("provider_result"), Mapping):
                draft_payload["provider_result"] = dict(payload["provider_result"])
            if isinstance(payload.get("style_profile"), Mapping):
                draft_payload["style_profile"] = dict(payload["style_profile"])
            draft_response = self.draft(draft_payload)
            return {
                "status": "ready",
                "entrypoint": "/v1/news-to-copy",
                "topic_plan": plan,
                "draft": draft_response["draft"],
                "topic_artifact_path": str(topic_artifact_path),
                "artifact_path": draft_response["artifact_path"],
                "provider_mode": draft_response["provider_mode"],
                "account_knowledge_status": draft_response.get("account_knowledge_status", "MISSING"),
                "account_knowledge_context_used": draft_response.get("account_knowledge_context_used", False),
            }
        except LabServiceError:
            raise
        except AccountKnowledgeModelError as exc:
            raise LabServiceError(str(exc), status=502) from exc
        except Exception as exc:
            raise LabServiceError(str(exc)) from exc

    def grade_work(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """人工确认作品等级并写回当前作品卡与评级历史。"""

        account_id = str(payload.get("account_id") or "").strip()
        work_id = str(payload.get("work_id") or "").strip()
        reviewer = str(payload.get("reviewer") or "").strip()
        if not account_id or not work_id or not reviewer:
            raise LabServiceError("account_id、work_id、reviewer 不能为空")
        try:
            card, body = self.repository.load_work_card(account_id, work_id)
            grade = apply_manual_grade(
                card,
                content_grade=str(payload.get("content_grade") or "").strip(),
                performance_grade=str(payload.get("performance_grade") or "UNRATED").strip(),
                reviewer=reviewer,
                notes=str(payload.get("notes") or "").strip(),
            )
            card_path = self.repository.save_work_card(card, body=body, overwrite=True)
            grade_path = self.repository.save_grade_card(grade)
        except Exception as exc:
            raise LabServiceError(str(exc)) from exc
        return {
            "status": "ready",
            "work_card": card.to_dict(),
            "grade_card": grade.to_dict(),
            "work_card_path": str(card_path),
            "grade_card_path": str(grade_path),
        }

    def promote(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        draft = payload.get("draft")
        if not isinstance(draft, Mapping):
            raise LabServiceError("draft 必须是对象")
        try:
            handoff = promote_v2_draft(draft, manual_approval=payload.get("manual_approval") is True)
            artifact_path = self.artifacts.save_handoff(handoff)
            note_path = self.repository.save_approved_copy_note(handoff)
        except Exception as exc:
            raise LabServiceError(str(exc)) from exc
        return {"status": "ready", "handoff": handoff, "artifact_path": str(artifact_path), "approved_note_path": str(note_path)}

    def dispatch(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        method = str(method or "").upper()
        clean_path = str(path or "").split("?", 1)[0]
        try:
            if method == "GET" and clean_path == "/health":
                return 200, self.health()
            if method != "POST":
                return 405, {"status": "blocked", "message": "只支持 GET /health 和指定 POST 路由"}
            body = payload if isinstance(payload, Mapping) else {}
            raw_account_ids = body.get("account_ids") or []
            if not isinstance(raw_account_ids, (list, tuple)):
                raise LabServiceError("account_ids 必须是数组")
            routes = {
                "/v1/index/rebuild": lambda: self.rebuild_index(list(raw_account_ids) or None),
                "/v1/topic-plan": lambda: self.topic_plan(body),
                "/v1/news-to-copy": lambda: self.news_to_copy(body),
                "/v1/negative-cases/search": lambda: self.negative_search(body),
                "/v1/draft": lambda: self.draft(body),
                "/v1/work-distill": lambda: self.distill_work(body),
                "/v1/account-distill": lambda: self.distill_account(body),
                "/v1/profile-upgrade": lambda: self.profile_upgrade(body),
                "/v1/grade-work": lambda: self.grade_work(body),
                "/v1/promote": lambda: self.promote(body),
            }
            handler = routes.get(clean_path)
            if handler is None:
                return 404, {"status": "blocked", "message": "V2 Lab 路由不存在"}
            return 200, handler()
        except LabServiceError as exc:
            return exc.status, {"status": "blocked", "error_type": type(exc).__name__, "message": str(exc)}
        except Exception as exc:
            return 500, {"status": "blocked", "error_type": type(exc).__name__, "message": str(exc)}


class _RequestHandler(BaseHTTPRequestHandler):
    service: LabService

    def _send(self, status: int, payload: Mapping[str, Any]) -> None:
        data = json.dumps(dict(payload), ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, status: int, content: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:  # noqa: N802
        clean_path = str(self.path or "").split("?", 1)[0]
        if clean_path in {"/", "/ui"}:
            try:
                self._send_html(200, LAB_UI_PATH.read_bytes())
            except OSError:
                self._send(500, {"status": "blocked", "message": "Lab UI 文件不可读"})
            return
        if clean_path == "/status":
            self._send(
                200,
                {
                    **self.service.health(),
                    "entrypoint": "POST /v1/news-to-copy",
                    "diagnostic_ui": "/ui",
                },
            )
            return
        status, payload = self.service.dispatch("GET", self.path)
        self._send(status, payload)

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length > MAX_BODY_BYTES:
            self._send(413, {"status": "blocked", "message": "请求体超过 2MB 限制"})
            return
        try:
            raw = self.rfile.read(max(0, length))
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(payload, Mapping):
                raise ValueError("请求 JSON 必须是对象")
        except Exception as exc:
            self._send(400, {"status": "blocked", "error_type": type(exc).__name__, "message": str(exc)})
            return
        status, response = self.service.dispatch("POST", self.path, payload)
        self._send(status, response)

    def log_message(self, format: str, *args: Any) -> None:
        return


def serve(*, host: str, port: int, vault_path: Path | str | None = None, output_path: Path | str | None = None) -> None:
    service = LabService(vault_path=vault_path, output_path=output_path)

    class Handler(_RequestHandler):
        pass

    Handler.service = service
    server = ThreadingHTTPServer((host, port), Handler)
    print(json.dumps({"status": "ready", "host": host, "port": port, "production_port": 8768}, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="独立账号知识库 V2 Lab 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--vault")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        serve(host=args.host, port=args.port, vault_path=args.vault, output_path=args.output)
    except KeyboardInterrupt:
        return 0
    except LabServiceError as exc:
        print(json.dumps({"status": "blocked", "error_type": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
