from fastapi import APIRouter, HTTPException

from app import db
from app.kb_search import kb_search_engine
from app.schemas import KBArticleCreate, KBArticleUpdate

router = APIRouter(prefix="/kb", tags=["knowledge-base"])


@router.get("")
def list_articles():
    return db.kb_list()


@router.get("/{article_id}")
def get_article(article_id: str):
    article = db.kb_get(article_id)
    if not article:
        raise HTTPException(404, "Artikel tidak ditemukan")
    return article


@router.post("", status_code=201)
def create_article(payload: KBArticleCreate):
    article = db.kb_create(
        title=payload.title, content=payload.content,
        tags=payload.tags, category=payload.category,
    )
    kb_search_engine.refresh()
    return article


@router.put("/{article_id}")
def update_article(article_id: str, payload: KBArticleUpdate):
    article = db.kb_update(article_id, **payload.model_dump(exclude_unset=True))
    if not article:
        raise HTTPException(404, "Artikel tidak ditemukan")
    kb_search_engine.refresh()
    return article


@router.delete("/{article_id}", status_code=204)
def delete_article(article_id: str):
    ok = db.kb_delete(article_id)
    if not ok:
        raise HTTPException(404, "Artikel tidak ditemukan")
    kb_search_engine.refresh()


@router.get("/search/query")
def search_kb(q: str, top_k: int = 5):
    """Endpoint bantu untuk uji coba pencarian KB langsung dari browser/Postman."""
    return kb_search_engine.search(q, top_k=top_k)
