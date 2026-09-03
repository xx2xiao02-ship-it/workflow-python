"""Obsidian Markdown 知识库仓库。

仓库只读写 Vault 内的 Markdown/YAML，不依赖 Obsidian 插件。写入采用
同目录临时文件替换，且所有相对路径都会经过 Vault 边界校验。
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping, TypeVar

from .contracts import CognitionCard, GradeCard, KnowledgeContractError, WorkCard, _validate_relative_ref, utc_now
from .evidence import DistillationJob, EvidenceSet, RawSource


T = TypeVar("T", WorkCard, CognitionCard, GradeCard)


class ObsidianRepositoryError(RuntimeError):
    pass


class VaultConfigError(ObsidianRepositoryError):
    pass


class KnowledgeConflictError(ObsidianRepositoryError):
    pass


_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _frontmatter_enabled(value: Any) -> bool:
    """把 YAML/JSON 中常见的布尔表示统一成治理状态。"""

    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off", "disabled"}
    return bool(value)


def _yaml_dump(value: Mapping[str, Any]) -> str:
    """优先输出 Obsidian 友好的 YAML；无 PyYAML 时回退到合法 YAML JSON。"""

    try:
        import yaml  # type: ignore

        return yaml.safe_dump(dict(value), allow_unicode=True, sort_keys=False, default_flow_style=False).rstrip()
    except ImportError:
        return json.dumps(dict(value), ensure_ascii=False, indent=2)


def _yaml_load(value: str) -> dict[str, Any]:
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(value)
    except ImportError:
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ObsidianRepositoryError("当前环境缺少 PyYAML，且知识卡片 frontmatter 不是 JSON") from exc
    except Exception as exc:  # yaml parser errors are implementation-specific
        raise ObsidianRepositoryError("Obsidian frontmatter 解析失败") from exc
    if not isinstance(loaded, Mapping):
        raise ObsidianRepositoryError("Obsidian frontmatter 必须是对象")
    return dict(loaded)


def _render_note(frontmatter: Mapping[str, Any], body: str = "") -> str:
    clean_body = str(body or "").strip("\n")
    rendered = "---\n" + _yaml_dump(frontmatter) + "\n---\n"
    return rendered + ((clean_body + "\n") if clean_body else "")


def _parse_note(text: str) -> tuple[dict[str, Any], str]:
    normalized = str(text or "")
    if not normalized.startswith("---\n"):
        raise ObsidianRepositoryError("知识笔记缺少 YAML frontmatter")
    marker = normalized.find("\n---", 4)
    if marker < 0:
        raise ObsidianRepositoryError("知识笔记 frontmatter 未闭合")
    frontmatter = _yaml_load(normalized[4:marker])
    body = normalized[marker + 4 :].lstrip("\n")
    return frontmatter, body


class ObsidianRepository:
    """Vault 内的账号知识资产仓库。"""

    ACCOUNT_DNA_NOTE_FILENAMES = (
        "语言DNA.md",
        "文章结构模板.md",
        "写作视角与认知框架.md",
        "视觉风格指南.md",
        "Writing-DNA.md",
    )
    ACCOUNT_DNA_HISTORY_FILENAME = "account_dna_versions.json"

    def __init__(self, vault_path: Path | str, *, create: bool = True) -> None:
        raw = Path(vault_path).expanduser()
        if not str(raw).strip():
            raise VaultConfigError("ACCOUNT_KNOWLEDGE_VAULT_PATH 不能为空")
        self.vault_path = raw.resolve()
        if create:
            self.vault_path.mkdir(parents=True, exist_ok=True)
        elif not self.vault_path.is_dir():
            raise VaultConfigError(f"Vault 不存在：{self.vault_path}")

    @classmethod
    def from_env(cls, *, env_name: str = "ACCOUNT_KNOWLEDGE_VAULT_PATH", create: bool = True) -> "ObsidianRepository":
        value = os.environ.get(env_name, "").strip()
        if not value:
            raise VaultConfigError(f"未配置 {env_name}，不会猜测或写死本机路径")
        return cls(value, create=create)

    @staticmethod
    def _validate_account_id(account_id: str) -> str:
        value = str(account_id or "").strip()
        if not _ACCOUNT_ID_RE.fullmatch(value):
            raise ObsidianRepositoryError("account_id 必须是安全的字母数字/下划线/短横线编号")
        return value

    @staticmethod
    def _validate_filename(filename: str) -> str:
        value = str(filename or "").strip()
        invalid_chars = set('\\/:*?"<>|') | {chr(code) for code in range(32)}
        if (
            not value.lower().endswith(".md")
            or not value
            or Path(value).name != value
            or ".." in Path(value).parts
            or any(char in invalid_chars for char in value)
        ):
            raise ObsidianRepositoryError("知识笔记文件名不合法")
        return value

    def _inside_vault(self, path: Path) -> Path:
        candidate = path.resolve()
        try:
            candidate.relative_to(self.vault_path)
        except ValueError as exc:
            raise ObsidianRepositoryError("目标路径超出 Obsidian Vault 边界") from exc
        return candidate

    def account_dir(self, account_id: str) -> Path:
        account = self._validate_account_id(account_id)
        return self._inside_vault(self.vault_path / "账号知识库" / "accounts" / account)

    def delete_account(self, account_id: str) -> dict[str, Any]:
        """彻底删除一个账号在 Vault 内的全部知识资产。"""
        root = self.account_dir(account_id)
        if not root.is_dir():
            raise ObsidianRepositoryError("找不到指定账号知识库")
        # account_dir 已经过 Vault 边界校验；禁止删除 accounts 根目录。
        if root == (self.vault_path / "账号知识库" / "accounts").resolve():
            raise ObsidianRepositoryError("拒绝删除账号知识库根目录")
        file_count = sum(1 for item in root.rglob("*") if item.is_file())
        shutil.rmtree(root)
        return {"account_id": str(account_id), "deleted_files": file_count}

    def initialize_account(self, account_id: str, *, account_name: str = "", overwrite_account_note: bool = False) -> Path:
        """建立账号知识库目录；只有显式要求才写入账号说明笔记。"""

        account = self._validate_account_id(account_id)
        root = self.account_dir(account)
        for directory in (
            "raw_works",
            "derived_works",
            "work_cards",
            "cognition_cards",
            "negative_cases",
            "grade_cards",
        ):
            (root / directory).mkdir(parents=True, exist_ok=True)
        generated = self._inside_vault(self.vault_path / "账号知识库" / "generated")
        for directory in ("approved", "published"):
            (generated / directory).mkdir(parents=True, exist_ok=True)
        existing_name = str(account_name or account).strip() or account
        account_note = self._inside_vault(root / "account.md")
        if account_note.is_file() and not overwrite_account_note:
            return account_note
        return self.write_account_note(
            account,
            filename="account.md",
            frontmatter={"schema_version": 1, "account_id": account, "account_name": existing_name},
            body=f"# {existing_name}\n\n此笔记由账号知识库底座创建，可由人工补充账号定位。",
            overwrite=overwrite_account_note,
        )

    @staticmethod
    def _save_json_atomic(path: Path, payload: Mapping[str, Any], *, overwrite: bool = False) -> Path:
        """Write a small versioned JSON asset with idempotent conflict handling."""

        if path.exists() and not overwrite:
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ObsidianRepositoryError(f"无法读取已有 JSON 资产：{path.name}") from exc
            if existing == dict(payload):
                return path
            raise KnowledgeConflictError(f"JSON 资产已存在且内容不同：{path.name}")
        ObsidianRepository._write_atomic(path, json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n")
        return path

    def save_raw_source_record(self, record: RawSource, *, overwrite: bool = False) -> Path:
        """保存一篇原始作品的来源记录；记录键按 work_id 去重。"""

        record.validate()
        path = self._inside_vault(self.account_dir(record.account_id) / "_meta" / "raw_sources" / f"{record.work_id}.json")
        return self._save_json_atomic(path, record.to_dict(), overwrite=overwrite)

    def load_raw_source_record(self, account_id: str, work_id: str) -> RawSource:
        path = self._inside_vault(self.account_dir(account_id) / "_meta" / "raw_sources" / f"{str(work_id).strip()}.json")
        try:
            return RawSource.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise ObsidianRepositoryError(f"无法读取原始来源记录：{path.name}") from exc

    def list_raw_source_records(self, account_id: str) -> list[RawSource]:
        directory = self.account_dir(account_id) / "_meta" / "raw_sources"
        if not directory.is_dir():
            return []
        result: list[RawSource] = []
        for path in sorted(directory.glob("*.json")):
            try:
                result.append(RawSource.from_dict(json.loads(self._inside_vault(path).read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                raise ObsidianRepositoryError(f"原始来源记录损坏：{path.name}") from exc
        return result

    def save_evidence_set(self, evidence: EvidenceSet, *, overwrite: bool = False) -> Path:
        evidence.validate()
        path = self._inside_vault(self.account_dir(evidence.account_id) / "_meta" / "evidence_sets" / f"{evidence.evidence_set_id}.json")
        return self._save_json_atomic(path, evidence.to_dict(), overwrite=overwrite)

    def load_evidence_set(self, account_id: str, evidence_set_id: str) -> EvidenceSet:
        path = self._inside_vault(self.account_dir(account_id) / "_meta" / "evidence_sets" / f"{str(evidence_set_id).strip()}.json")
        try:
            return EvidenceSet.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise ObsidianRepositoryError(f"无法读取证据集：{path.name}") from exc

    def list_evidence_sets(self, account_id: str) -> list[EvidenceSet]:
        directory = self.account_dir(account_id) / "_meta" / "evidence_sets"
        if not directory.is_dir():
            return []
        result: list[EvidenceSet] = []
        for path in sorted(directory.glob("*.json")):
            try:
                result.append(EvidenceSet.from_dict(json.loads(self._inside_vault(path).read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                raise ObsidianRepositoryError(f"证据集损坏：{path.name}") from exc
        return result

    def save_distillation_job(self, job: DistillationJob, *, overwrite: bool = False) -> Path:
        job.validate()
        path = self._inside_vault(self.vault_path / "账号知识库" / "_jobs" / f"{job.job_id}.json")
        return self._save_json_atomic(path, job.to_dict(), overwrite=overwrite)

    def load_distillation_job(self, job_id: str) -> DistillationJob:
        safe_id = str(job_id or "").strip()
        if not safe_id or "/" in safe_id or "\\" in safe_id or ".." in safe_id:
            raise ObsidianRepositoryError("DistillationJob 编号不合法")
        path = self._inside_vault(self.vault_path / "账号知识库" / "_jobs" / f"{safe_id}.json")
        try:
            return DistillationJob.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise ObsidianRepositoryError(f"无法读取采集任务：{path.name}") from exc

    def list_distillation_jobs(self, *, account_id: str = "") -> list[DistillationJob]:
        directory = self._inside_vault(self.vault_path / "账号知识库" / "_jobs")
        if not directory.is_dir():
            return []
        requested = str(account_id or "").strip()
        result: list[DistillationJob] = []
        for path in sorted(directory.glob("*.json")):
            try:
                job = DistillationJob.from_dict(json.loads(self._inside_vault(path).read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                raise ObsidianRepositoryError(f"采集任务损坏：{path.name}") from exc
            if not requested or job.account_id == requested:
                result.append(job)
        return result

    def _card_path(self, account_id: str, collection: str, identifier: str) -> Path:
        if collection not in {"work_cards", "cognition_cards", "grade_cards", "negative_cases"}:
            raise ObsidianRepositoryError("不支持的知识卡片目录")
        account_dir = self.account_dir(account_id)
        safe_id = str(identifier or "").strip()
        if not safe_id or "/" in safe_id or "\\" in safe_id or ".." in safe_id:
            raise ObsidianRepositoryError("知识卡片编号不合法")
        return self._inside_vault(account_dir / collection / f"{safe_id}.md")

    @staticmethod
    def _write_atomic(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(text, encoding="utf-8", newline="\n")
            temporary.replace(path)
        except OSError as exc:
            raise ObsidianRepositoryError(f"无法写入知识笔记：{path.name}") from exc
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _read(path: Path) -> tuple[dict[str, Any], str]:
        try:
            return _parse_note(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ObsidianRepositoryError(f"无法读取知识笔记：{path.name}") from exc

    def _save_card(self, *, path: Path, payload: Mapping[str, Any], body: str = "", overwrite: bool = False) -> Path:
        if path.exists() and not overwrite:
            existing, _ = self._read(path)
            existing_id = str(existing.get("work_id") or existing.get("cognition_id") or existing.get("grade_id") or "")
            payload_id = str(payload.get("work_id") or payload.get("cognition_id") or payload.get("grade_id") or "")
            if existing_id == payload_id:
                if existing == dict(payload):
                    # 保留人工可能已经补充的正文；重复导入只复用现有编号。
                    return path
                raise KnowledgeConflictError(f"知识卡片已存在，需显式 overwrite 才能更新：{path.name}")
            raise KnowledgeConflictError(f"知识卡片已存在且编号冲突：{path.name}")
        self._write_atomic(path, _render_note(payload, body))
        return path

    def save_work_card(self, card: WorkCard, *, body: str = "", overwrite: bool = False) -> Path:
        card.validate()
        path = self._card_path(card.account_id, "work_cards", card.work_id)
        return self._save_card(path=path, payload=card.to_dict(), body=body, overwrite=overwrite)

    def load_work_card(self, account_id: str, work_id: str) -> tuple[WorkCard, str]:
        path = self._card_path(account_id, "work_cards", work_id)
        if not path.is_file():
            raise ObsidianRepositoryError(f"找不到作品卡：{work_id}")
        frontmatter, body = self._read(path)
        return WorkCard.from_dict(frontmatter), body

    def save_cognition_card(self, card: CognitionCard, *, body: str = "", overwrite: bool = False) -> Path:
        card.validate()
        path = self._card_path(card.account_id, "cognition_cards", card.cognition_id)
        return self._save_card(path=path, payload=card.to_dict(), body=body, overwrite=overwrite)

    def load_cognition_card(self, account_id: str, cognition_id: str) -> tuple[CognitionCard, str]:
        path = self._card_path(account_id, "cognition_cards", cognition_id)
        if not path.is_file():
            raise ObsidianRepositoryError(f"找不到认知卡：{cognition_id}")
        frontmatter, body = self._read(path)
        return CognitionCard.from_dict(frontmatter), body

    def save_grade_card(self, card: GradeCard, *, body: str = "", overwrite: bool = False) -> Path:
        card.validate()
        path = self._card_path(card.account_id, "grade_cards", card.grade_id)
        return self._save_card(path=path, payload=card.to_dict(), body=body, overwrite=overwrite)

    def list_work_cards(self, account_id: str) -> list[WorkCard]:
        directory = self.account_dir(account_id) / "work_cards"
        if not directory.is_dir():
            return []
        result: list[WorkCard] = []
        for path in sorted(directory.glob("*.md")):
            frontmatter, _ = self._read(self._inside_vault(path))
            result.append(WorkCard.from_dict(frontmatter))
        return result

    def list_cognition_cards(self, account_id: str) -> list[CognitionCard]:
        directory = self.account_dir(account_id) / "cognition_cards"
        if not directory.is_dir():
            return []
        result: list[CognitionCard] = []
        for path in sorted(directory.glob("*.md")):
            frontmatter, _ = self._read(self._inside_vault(path))
            result.append(CognitionCard.from_dict(frontmatter))
        return result

    def list_account_ids(self) -> list[str]:
        root = self._inside_vault(self.vault_path / "账号知识库" / "accounts")
        if not root.is_dir():
            return []
        return sorted(
            path.name
            for path in root.iterdir()
            if path.is_dir() and _ACCOUNT_ID_RE.fullmatch(path.name)
        )

    def write_account_note(self, account_id: str, *, filename: str, frontmatter: Mapping[str, Any], body: str = "", overwrite: bool = False) -> Path:
        path = self._inside_vault(self.account_dir(account_id) / self._validate_filename(filename))
        if path.exists() and not overwrite:
            raise KnowledgeConflictError(f"账号笔记已存在：{filename}")
        self._write_atomic(path, _render_note(frontmatter, body))
        return path

    def read_account_note(self, account_id: str, *, filename: str) -> tuple[dict[str, Any], str]:
        path = self._inside_vault(self.account_dir(account_id) / self._validate_filename(filename))
        if not path.is_file():
            raise ObsidianRepositoryError(f"找不到账号笔记：{filename}")
        return self._read(path)

    def load_account_dna_context(
        self,
        account_id: str,
        *,
        include_pending: bool = False,
        max_chars_per_note: int = 8000,
    ) -> dict[str, Any]:
        """读取五份账号 DNA 笔记的受控上下文。

        默认只允许全部标记为 ``APPROVED``/``PUBLISHED`` 的笔记进入文案
        provider。候选版和待审核版只返回状态，不会被静默当成生产规则。
        ``include_pending`` 必须由调用方显式开启，且返回上下文仍保留
        ``REVIEW_PENDING`` 标记。
        """

        account = self._validate_account_id(account_id)
        documents: dict[str, dict[str, Any]] = {}
        missing: list[str] = []
        for filename in self.ACCOUNT_DNA_NOTE_FILENAMES:
            path = self._inside_vault(self.account_dir(account) / self._validate_filename(filename))
            if not path.is_file():
                missing.append(filename)
                continue
            frontmatter, body = self._read(path)
            if str(frontmatter.get("account_id") or "").strip() != account:
                raise ObsidianRepositoryError(f"账号笔记 account_id 不一致：{filename}")
            documents[filename] = {"frontmatter": frontmatter, "body": body}
        if missing:
            return {
                "available": False,
                "knowledge_status": "MISSING",
                "distillation_status": "MISSING",
                "missing_files": missing,
                "sample_count": 0,
                "evidence_work_ids": [],
                "knowledge_source_ids": [],
                "knowledge_source_versions": {},
            }

        statuses = {
            str(item["frontmatter"].get("knowledge_status") or "REVIEW_PENDING").strip().upper()
            for item in documents.values()
        }
        distillation_statuses = {
            str(item["frontmatter"].get("distillation_status") or "").strip().upper()
            for item in documents.values()
        }
        sample_counts = [int(item["frontmatter"].get("sample_count") or 0) for item in documents.values()]
        evidence_sets = [
            [str(value).strip() for value in (item["frontmatter"].get("evidence_work_ids") or []) if str(value).strip()]
            for item in documents.values()
        ]
        source_id_sets = [
            [str(value).strip() for value in (item["frontmatter"].get("knowledge_source_ids") or []) if str(value).strip()]
            for item in documents.values()
        ]
        source_versions = {}
        for item in documents.values():
            value = item["frontmatter"].get("knowledge_source_versions")
            if isinstance(value, Mapping):
                source_versions.update({str(key): str(version or "source-v1") for key, version in value.items() if str(key).strip()})
        knowledge_status = "APPROVED" if statuses and statuses.issubset({"APPROVED", "PUBLISHED"}) else "REVIEW_PENDING"
        enabled_values = {
            _frontmatter_enabled(item["frontmatter"].get("enabled", True))
            for item in documents.values()
        }
        enabled = knowledge_status == "APPROVED" and enabled_values == {True}
        distillation_status = next(iter(distillation_statuses), "") if len(distillation_statuses) == 1 else "MIXED"
        base = {
            "available": (knowledge_status == "APPROVED" and enabled) or include_pending,
            "knowledge_status": knowledge_status,
            "distillation_status": distillation_status,
            "enabled": enabled,
            "sample_count": min(sample_counts) if sample_counts else 0,
            "evidence_work_ids": sorted(set(evidence_sets[0])) if evidence_sets else [],
            "knowledge_source_ids": sorted({value for values in source_id_sets for value in values}),
            "knowledge_source_versions": source_versions,
            "missing_files": [],
        }
        if not base["available"]:
            base["reason"] = (
                "账号思维包已停用"
                if knowledge_status == "APPROVED" and not enabled
                else "五份账号笔记尚未全部人工审核通过"
            )
            return base

        notes: dict[str, Any] = {}
        for filename, item in documents.items():
            frontmatter = item["frontmatter"]
            payload = frontmatter.get("content")
            rendered = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if len(rendered) > max_chars_per_note:
                payload = {
                    "truncated": True,
                    "preview": rendered[:max_chars_per_note],
                    "note": "上下文已按运行时长度限制截断；应回到 Obsidian 原笔记复核。",
                }
            notes[filename] = {
                "layer": str(frontmatter.get("layer") or ""),
                "content": payload,
            }
        base["notes"] = notes
        base["source_policy"] = "账号 DNA 仅提供抽象表达/结构/认知线索，不替代本次新闻事实来源。"
        return base

    def approve_account_dna(self, account_id: str, *, reviewer: str, notes: str = "") -> list[Path]:
        """显式人工批准深度账号 DNA；离线候选版永远不能直接批准。"""

        account = self._validate_account_id(account_id)
        reviewer_text = str(reviewer or "").strip()
        if not reviewer_text:
            raise ObsidianRepositoryError("批准账号 DNA 必须记录 reviewer")
        context = self.load_account_dna_context(account, include_pending=True)
        if context.get("missing_files"):
            raise ObsidianRepositoryError("五份账号 DNA 笔记不完整，不能批准")
        if context.get("knowledge_status") == "APPROVED":
            self._ensure_account_dna_history(account, reviewer=reviewer_text, notes=notes, persist=True)
            return [self._inside_vault(self.account_dir(account) / filename) for filename in self.ACCOUNT_DNA_NOTE_FILENAMES]
        if context.get("distillation_status") != "READY_FOR_REVIEW":
            raise ObsidianRepositoryError("只有 READY_FOR_REVIEW 的深度账号 DNA 才能批准；PROVISIONAL_OFFLINE 不可直接批准")
        if int(context.get("sample_count") or 0) < 30:
            raise ObsidianRepositoryError("账号 DNA 样本少于 30 篇，不能批准为正式规则")
        approved_at = utc_now()
        paths: list[Path] = []
        for filename in self.ACCOUNT_DNA_NOTE_FILENAMES:
            frontmatter, body = self.read_account_note(account, filename=filename)
            frontmatter.update(
                {
                    "knowledge_status": "APPROVED",
                    "enabled": True,
                    "approved_by": reviewer_text,
                    "approved_at": approved_at,
                }
            )
            if str(notes or "").strip():
                frontmatter["approval_notes"] = str(notes).strip()
            paths.append(self.write_account_note(account, filename=filename, frontmatter=frontmatter, body=body, overwrite=True))
        self._ensure_account_dna_history(account, reviewer=reviewer_text, notes=notes, persist=True, reason="approve")
        return paths

    def _account_dna_history_path(self, account_id: str) -> Path:
        return self._inside_vault(self.account_dir(account_id) / "_meta" / self.ACCOUNT_DNA_HISTORY_FILENAME)

    def _read_account_dna_history(self, account_id: str) -> dict[str, Any]:
        path = self._account_dna_history_path(account_id)
        if not path.is_file():
            return {
                "schema_version": "account-dna-history-v1",
                "account_id": self._validate_account_id(account_id),
                "current_version": "",
                "versions": [],
            }
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ObsidianRepositoryError("账号 DNA 版本历史损坏") from exc
        if not isinstance(value, dict):
            raise ObsidianRepositoryError("账号 DNA 版本历史格式错误")
        value.setdefault("schema_version", "account-dna-history-v1")
        value.setdefault("account_id", self._validate_account_id(account_id))
        value.setdefault("versions", [])
        if not isinstance(value.get("versions"), list):
            raise ObsidianRepositoryError("账号 DNA 版本历史必须是数组")
        return value

    def _write_account_dna_history(self, account_id: str, value: Mapping[str, Any]) -> Path:
        path = self._account_dna_history_path(account_id)
        payload = dict(value)
        payload["schema_version"] = "account-dna-history-v1"
        payload["account_id"] = self._validate_account_id(account_id)
        payload["updated_at"] = utc_now()
        versions = payload.get("versions") if isinstance(payload.get("versions"), list) else []
        payload["versions"] = versions[-50:]
        self._write_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return path

    def _account_dna_documents(self, account_id: str) -> dict[str, dict[str, Any]]:
        account = self._validate_account_id(account_id)
        documents: dict[str, dict[str, Any]] = {}
        for filename in self.ACCOUNT_DNA_NOTE_FILENAMES:
            path = self._inside_vault(self.account_dir(account) / self._validate_filename(filename))
            if not path.is_file():
                raise ObsidianRepositoryError(f"账号 DNA 笔记不完整：{filename}")
            frontmatter, body = self._read(path)
            documents[filename] = {"frontmatter": dict(frontmatter), "body": body}
        return documents

    @staticmethod
    def _account_dna_digest(documents: Mapping[str, Mapping[str, Any]]) -> str:
        raw = json.dumps(documents, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _ensure_account_dna_history(
        self,
        account_id: str,
        *,
        reviewer: str = "",
        notes: str = "",
        persist: bool = False,
        reason: str = "snapshot",
    ) -> dict[str, Any]:
        account = self._validate_account_id(account_id)
        documents = self._account_dna_documents(account)
        history = self._read_account_dna_history(account)
        digest = self._account_dna_digest(documents)
        current = str(history.get("current_version") or "")
        versions = history.get("versions") if isinstance(history.get("versions"), list) else []
        current_entry = next(
            (item for item in versions if isinstance(item, Mapping) and item.get("version_id") == current),
            None,
        )
        if current_entry is not None:
            previous_snapshot = current_entry.get("snapshot") if isinstance(current_entry, Mapping) else None
            if isinstance(previous_snapshot, Mapping) and self._account_dna_digest(previous_snapshot) == digest:
                return history
        version_number = max(
            [int(item.get("version_number") or 0) for item in versions if isinstance(item, Mapping)] or [0]
        ) + 1
        version_id = f"account-dna-v{version_number}-{digest}"
        statuses = {
            str(item["frontmatter"].get("knowledge_status") or "REVIEW_PENDING").upper()
            for item in documents.values()
        }
        knowledge_status = "APPROVED" if statuses.issubset({"APPROVED", "PUBLISHED"}) else "REVIEW_PENDING"
        enabled = knowledge_status == "APPROVED" and all(
            _frontmatter_enabled(item["frontmatter"].get("enabled", True)) for item in documents.values()
        )
        first = next(iter(documents.values()))["frontmatter"]
        entry = {
            "version_id": version_id,
            "version_number": version_number,
            "created_at": utc_now(),
            "reason": reason,
            "reviewer": str(reviewer or "").strip(),
            "notes": str(notes or "").strip(),
            "knowledge_status": knowledge_status,
            "distillation_status": str(first.get("distillation_status") or ""),
            "enabled": enabled,
            "sample_count": int(first.get("sample_count") or 0),
            "evidence_work_ids": list(first.get("evidence_work_ids") or []),
            "knowledge_source_ids": list(first.get("knowledge_source_ids") or []),
            "knowledge_source_versions": dict(first.get("knowledge_source_versions") or {}) if isinstance(first.get("knowledge_source_versions"), Mapping) else {},
            "snapshot": documents,
        }
        versions.append(entry)
        history["current_version"] = version_id
        history["versions"] = versions
        if persist:
            for filename, item in documents.items():
                frontmatter = dict(item["frontmatter"])
                frontmatter.setdefault("asset_type", "account_thinking_package")
                frontmatter["asset_version"] = version_id
                frontmatter.setdefault("enabled", enabled)
                self.write_account_note(
                    account,
                    filename=filename,
                    frontmatter=frontmatter,
                    body=str(item["body"] or ""),
                    overwrite=True,
                )
            # Re-read after the asset_version fields are written so the
            # persisted snapshot is exactly what the runtime consumes.
            documents = self._account_dna_documents(account)
            entry["snapshot"] = documents
            history["versions"][-1] = entry
            self._write_account_dna_history(account, history)
        return history

    def list_account_dna_versions(self, account_id: str) -> list[dict[str, Any]]:
        """列出账号思维包版本摘要，不返回五份笔记正文。"""

        account = self._validate_account_id(account_id)
        try:
            history = self._ensure_account_dna_history(account, persist=False)
        except ObsidianRepositoryError:
            return []
        versions = history.get("versions") if isinstance(history.get("versions"), list) else []
        current = str(history.get("current_version") or "")
        result: list[dict[str, Any]] = []
        for item in reversed(versions):
            if not isinstance(item, Mapping):
                continue
            result.append({
                key: item.get(key)
                for key in (
                    "version_id", "version_number", "created_at", "reason", "reviewer", "notes",
                    "knowledge_status", "distillation_status", "enabled", "sample_count", "evidence_work_ids",
                    "knowledge_source_ids", "knowledge_source_versions",
                )
            } | {"current": str(item.get("version_id") or "") == current})
        return result

    def set_account_dna_enabled(self, account_id: str, *, enabled: bool, reviewer: str, notes: str = "") -> dict[str, Any]:
        """启用/停用已审核账号思维包，并记录新的可回滚版本。"""

        account = self._validate_account_id(account_id)
        reviewer_text = str(reviewer or "").strip()
        if not reviewer_text:
            raise ObsidianRepositoryError("启用或停用账号思维包必须记录 reviewer")
        context = self.load_account_dna_context(account, include_pending=True)
        if context.get("missing_files"):
            raise ObsidianRepositoryError("五份账号 DNA 笔记不完整")
        if context.get("knowledge_status") != "APPROVED":
            raise ObsidianRepositoryError("只有已审核账号思维包才能启用或停用")
        documents = self._account_dna_documents(account)
        for filename, item in documents.items():
            frontmatter = dict(item["frontmatter"])
            frontmatter["enabled"] = bool(enabled)
            frontmatter["enabled_by"] = reviewer_text
            frontmatter["enabled_at"] = utc_now()
            self.write_account_note(account, filename=filename, frontmatter=frontmatter, body=item["body"], overwrite=True)
        history = self._ensure_account_dna_history(account, reviewer=reviewer_text, notes=notes, persist=True, reason="enable" if enabled else "disable")
        return {
            "account_id": account,
            "enabled": bool(enabled),
            "current_version": history.get("current_version", ""),
            "versions": self.list_account_dna_versions(account),
        }

    def rollback_account_dna(self, account_id: str, version_id: str, *, reviewer: str, notes: str = "") -> dict[str, Any]:
        """将账号 DNA 恢复到历史快照，并生成新的回滚版本记录。"""

        account = self._validate_account_id(account_id)
        reviewer_text = str(reviewer or "").strip()
        if not reviewer_text:
            raise ObsidianRepositoryError("回滚账号思维包必须记录 reviewer")
        requested = str(version_id or "").strip()
        if not requested or "/" in requested or "\\" in requested:
            raise ObsidianRepositoryError("账号 DNA 版本编号不合法")
        history = self._ensure_account_dna_history(account, persist=False)
        versions = history.get("versions") if isinstance(history.get("versions"), list) else []
        target = next((item for item in versions if isinstance(item, Mapping) and item.get("version_id") == requested), None)
        if not isinstance(target, Mapping):
            raise ObsidianRepositoryError("找不到账号 DNA 历史版本")
        snapshot = target.get("snapshot")
        if not isinstance(snapshot, Mapping):
            raise ObsidianRepositoryError("账号 DNA 历史版本缺少快照")
        for filename in self.ACCOUNT_DNA_NOTE_FILENAMES:
            item = snapshot.get(filename)
            if not isinstance(item, Mapping):
                raise ObsidianRepositoryError(f"账号 DNA 历史版本缺少 {filename}")
            frontmatter = dict(item.get("frontmatter") or {})
            body = str(item.get("body") or "")
            # 回滚到未审核快照时默认保持停用，避免旧候选版直接进入生产。
            if str(frontmatter.get("knowledge_status") or "").upper() != "APPROVED":
                frontmatter["enabled"] = False
            frontmatter["rollback_of"] = requested
            frontmatter["rolled_back_by"] = reviewer_text
            frontmatter["rolled_back_at"] = utc_now()
            self.write_account_note(account, filename=filename, frontmatter=frontmatter, body=body, overwrite=True)
        result_history = self._ensure_account_dna_history(account, reviewer=reviewer_text, notes=notes, persist=True, reason=f"rollback:{requested}")
        return {
            "account_id": account,
            "rollback_of": requested,
            "current_version": result_history.get("current_version", ""),
            "versions": self.list_account_dna_versions(account),
        }

    def save_approved_copy_note(self, payload: Mapping[str, Any], *, body: str = "", overwrite: bool = False) -> Path:
        """只接收显式人工批准的 V2 handoff，并写入 Vault 的 generated/approved。"""

        if not isinstance(payload, Mapping) or payload.get("handoff_type") != "v2_manual_approval":
            raise ObsidianRepositoryError("只有 v2_manual_approval 才能写入 approved 作品目录")
        account_id = self._validate_account_id(str(payload.get("account_id") or ""))
        approved_copy = str(payload.get("approved_copy") or "").strip()
        draft_id = str(payload.get("draft_id") or "").strip()
        if not approved_copy or not draft_id or "/" in draft_id or "\\" in draft_id or ".." in draft_id:
            raise ObsidianRepositoryError("approved copy 缺少安全的 draft_id 或正文")
        path = self._inside_vault(self.vault_path / "账号知识库" / "generated" / "approved" / f"{draft_id}.md")
        frontmatter = {**dict(payload), "account_id": account_id, "knowledge_status": "APPROVED"}
        if path.exists() and not overwrite:
            existing, _ = self._read(path)
            if existing == frontmatter:
                return path
            raise KnowledgeConflictError(f"approved 作品已存在，需显式 overwrite 才能更新：{path.name}")
        self._write_atomic(path, _render_note(frontmatter, body or approved_copy))
        return path

    def save_raw_source(self, account_id: str, source_ref: str, source: bytes, *, overwrite: bool = False) -> Path:
        """保存账号原始作品，source_ref 必须位于该账号的 raw_works/ 下。"""

        account_root = self.account_dir(account_id)
        relative = str(source_ref or "").strip().replace("\\", "/")
        if not relative.startswith("raw_works/"):
            raise ObsidianRepositoryError("原始作品 source_ref 必须位于 raw_works/ 下")
        safe_relative = _validate_relative_ref(relative)
        path = self._inside_vault(account_root / safe_relative)
        if path.exists() and not overwrite:
            try:
                if path.read_bytes() == bytes(source):
                    return path
            except OSError as exc:
                raise ObsidianRepositoryError(f"无法读取已有原始作品：{path.name}") from exc
            raise KnowledgeConflictError(f"原始作品已存在且内容不同：{path.name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(bytes(source))
            temporary.replace(path)
        except OSError as exc:
            raise ObsidianRepositoryError(f"无法写入原始作品：{path.name}") from exc
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return path

    def save_professional_material(
        self,
        *,
        material_id: str,
        metadata: Mapping[str, Any],
        body: str = "",
        source_bytes: bytes | None = None,
        source_suffix: str = "",
        overwrite: bool = False,
    ) -> dict[str, str]:
        """写入公共专业知识库的待处理资料。

        资料先以 ``专业知识库/待处理`` 中的 Markdown 和可选原始附件落盘，
        不会自动把未经审核的内容当作生产知识。调用方仍需显式触发后续
        解析/蒸馏任务。
        """

        material = str(material_id or "").strip()
        if not _ACCOUNT_ID_RE.fullmatch(material):
            raise ObsidianRepositoryError("material_id 必须是安全的字母数字/下划线/短横线编号")
        root = self._inside_vault(self.vault_path / "专业知识库" / "待处理")
        note_path = self._inside_vault(root / f"{material}.md")
        payload = {
            "schema_version": 1,
            "material_id": material,
            "knowledge_status": "RAW",
            **dict(metadata),
        }
        if note_path.exists() and not overwrite:
            existing, _ = self._read(note_path)
            if existing == payload:
                result = {"material_path": str(note_path)}
                if source_bytes is not None:
                    suffix = str(source_suffix or "").strip().lower()
                    if suffix and not suffix.startswith("."):
                        suffix = "." + suffix
                    attachment = self._inside_vault(root / f"{material}.source{suffix}")
                    if attachment.is_file():
                        result["attachment_path"] = str(attachment)
                return result
            raise KnowledgeConflictError(f"专业知识资料已存在且内容不同：{note_path.name}")
        self._write_atomic(note_path, _render_note(payload, body))
        result = {"material_path": str(note_path)}
        if source_bytes is not None:
            suffix = str(source_suffix or "").strip().lower()
            if suffix and not suffix.startswith("."):
                suffix = "." + suffix
            if suffix and not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
                raise ObsidianRepositoryError("专业知识资料附件后缀不合法")
            attachment = self._inside_vault(root / f"{material}.source{suffix}")
            temporary = attachment.with_name(f".{attachment.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
            try:
                attachment.parent.mkdir(parents=True, exist_ok=True)
                temporary.write_bytes(bytes(source_bytes))
                temporary.replace(attachment)
            except OSError as exc:
                raise ObsidianRepositoryError(f"无法写入专业知识资料附件：{attachment.name}") from exc
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            result["attachment_path"] = str(attachment)
        return result

    def read_professional_material(self, material_id: str) -> tuple[dict[str, Any], str]:
        """读取 ``专业知识库/待处理`` 中的单份资料及其正文。"""

        material = str(material_id or "").strip()
        if not _ACCOUNT_ID_RE.fullmatch(material):
            raise ObsidianRepositoryError("material_id 必须是安全的字母数字/下划线/短横线编号")
        path = self._inside_vault(self.vault_path / "专业知识库" / "待处理" / f"{material}.md")
        if not path.is_file():
            raise ObsidianRepositoryError(f"找不到专业知识资料：{material}")
        return self._read(path)

    def update_professional_material(
        self,
        material_id: str,
        metadata: Mapping[str, Any],
        *,
        body: str | None = None,
    ) -> Path:
        """更新专业资料 frontmatter，保留正文和原始附件。

        知识源状态、人工确认的平台/类型和使用范围都要同步到 Vault；该
        方法只改目标资料，不会重建目录或覆盖其它笔记。
        """

        if not isinstance(metadata, Mapping):
            raise ObsidianRepositoryError("专业知识资料 metadata 必须是对象")
        material = str(material_id or "").strip()
        if not _ACCOUNT_ID_RE.fullmatch(material):
            raise ObsidianRepositoryError("material_id 必须是安全的字母数字/下划线/短横线编号")
        path = self._inside_vault(self.vault_path / "专业知识库" / "待处理" / f"{material}.md")
        if not path.is_file():
            raise ObsidianRepositoryError(f"找不到专业知识资料：{material}")
        frontmatter, existing_body = self._read(path)
        updated = dict(frontmatter)
        updated.update(dict(metadata))
        updated["material_id"] = material
        updated["updated_at"] = utc_now()
        self._write_atomic(path, _render_note(updated, existing_body if body is None else body))
        return path

    def load_raw_source(self, account_id: str, source_ref: str) -> str:
        """读取账号目录中的原始作品，供显式蒸馏任务使用。

        读取同样经过 Vault 边界和 ``raw_works/`` 前缀校验，避免把任意本机
        路径交给模型 provider。
        """

        account_root = self.account_dir(account_id)
        relative = str(source_ref or "").strip().replace("\\", "/")
        if not relative.startswith("raw_works/"):
            raise ObsidianRepositoryError("原始作品 source_ref 必须位于 raw_works/ 下")
        safe_relative = _validate_relative_ref(relative)
        path = self._inside_vault(account_root / safe_relative)
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ObsidianRepositoryError(f"无法读取原始作品：{path.name}") from exc

    def save_derived_source(self, account_id: str, source_ref: str, source: bytes, *, overwrite: bool = False) -> Path:
        """保存风格包派生作品，明确与 ``raw_works`` 原始作品分离。"""

        account_root = self.account_dir(account_id)
        relative = str(source_ref or "").strip().replace("\\", "/")
        if not relative.startswith("derived_works/"):
            raise ObsidianRepositoryError("派生作品 source_ref 必须位于 derived_works/ 下")
        safe_relative = _validate_relative_ref(relative)
        path = self._inside_vault(account_root / safe_relative)
        if path.exists() and not overwrite:
            try:
                if path.read_bytes() == bytes(source):
                    return path
            except OSError as exc:
                raise ObsidianRepositoryError(f"无法读取已有派生作品：{path.name}") from exc
            raise KnowledgeConflictError(f"派生作品已存在且内容不同：{path.name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(bytes(source))
            temporary.replace(path)
        except OSError as exc:
            raise ObsidianRepositoryError(f"无法写入派生作品：{path.name}") from exc
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return path

    def load_derived_source(self, account_id: str, source_ref: str) -> str:
        """读取风格包派生作品；不会把它伪装成可供真实账号蒸馏的原文。"""

        account_root = self.account_dir(account_id)
        relative = str(source_ref or "").strip().replace("\\", "/")
        if not relative.startswith("derived_works/"):
            raise ObsidianRepositoryError("派生作品 source_ref 必须位于 derived_works/ 下")
        safe_relative = _validate_relative_ref(relative)
        path = self._inside_vault(account_root / safe_relative)
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ObsidianRepositoryError(f"无法读取派生作品：{path.name}") from exc

    def save_work_metadata(self, account_id: str, work_id: str, metadata: Mapping[str, Any], *, overwrite: bool = False) -> Path:
        """保存写作蒸馏器需要的单篇 `_meta` JSON 记录。"""

        account_root = self.account_dir(account_id)
        safe_id = str(work_id or "").strip()
        if not safe_id or "/" in safe_id or "\\" in safe_id or ".." in safe_id:
            raise ObsidianRepositoryError("作品元数据编号不合法")
        path = self._inside_vault(account_root / "_meta" / f"{safe_id}.json")
        if path.exists() and not overwrite:
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ObsidianRepositoryError(f"无法读取已有作品元数据：{path.name}") from exc
            if existing == dict(metadata):
                return path
            raise KnowledgeConflictError(f"作品元数据已存在且内容不同：{path.name}")
        self._write_atomic(path, json.dumps(dict(metadata), ensure_ascii=False, indent=2) + "\n")
        return path

    def load_work_metadata(self, account_id: str, work_id: str) -> dict[str, Any]:
        account_root = self.account_dir(account_id)
        safe_id = str(work_id or "").strip()
        if not safe_id or "/" in safe_id or "\\" in safe_id or ".." in safe_id:
            raise ObsidianRepositoryError("作品元数据编号不合法")
        path = self._inside_vault(account_root / "_meta" / f"{safe_id}.json")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ObsidianRepositoryError(f"无法读取作品元数据：{path.name}") from exc
        if not isinstance(payload, dict):
            raise ObsidianRepositoryError("作品元数据必须是对象")
        return payload

    def iter_note_paths(self, account_id: str | None = None) -> Iterable[Path]:
        root = self.account_dir(account_id) if account_id else self._inside_vault(self.vault_path / "账号知识库")
        if not root.exists():
            return []
        return (self._inside_vault(path) for path in root.rglob("*.md") if path.is_file())


__all__ = [
    "KnowledgeConflictError",
    "ObsidianRepository",
    "ObsidianRepositoryError",
    "VaultConfigError",
]
