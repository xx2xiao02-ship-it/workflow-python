from src.utils.draft_cache import DRAFT_CACHE
from src.utils import helper


def save_draft(draft_url: str) -> str:
    draft_id = helper.get_url_param(draft_url, "draft_id")
    if (not draft_id) or (draft_id not in DRAFT_CACHE):
        raise ValueError("INVALID_DRAFT_URL")
    DRAFT_CACHE[draft_id].save()
    return draft_url
