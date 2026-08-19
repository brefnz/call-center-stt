"""
KB Search Engine.

Implementasi awal pakai TF-IDF + cosine similarity (scikit-learn) supaya
ringan, tanpa perlu download model embedding tambahan (cocok untuk desain
"sementara" sebelum data KB dipindah ke CRM/vector DB yang sesungguhnya).

Cara upgrade ke semantic search nanti: ganti `TfidfVectorizer` dengan model
embedding open-source (mis. sentence-transformers) dan simpan embedding_vector
per artikel (lihat kolom di PRD Bab 11), lalu index pakai FAISS/pgvector.
"""
import threading

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from app import db
from app.config import settings


class KBSearchEngine:
    def __init__(self):
        self._lock = threading.Lock()
        self._vectorizer: TfidfVectorizer | None = None
        self._matrix = None
        self._articles: list[dict] = []
        self.refresh()

    def refresh(self):
        """Rebuild index dari database. Panggil setiap ada perubahan KB."""
        with self._lock:
            self._articles = db.kb_list()
            corpus = [
                f"{a['title']} {a['content']} {' '.join(_tags(a))}"
                for a in self._articles
            ]
            if not corpus:
                self._vectorizer = None
                self._matrix = None
                return
            self._vectorizer = TfidfVectorizer(
                lowercase=True,
                ngram_range=(1, 2),
                max_df=0.9,
            )
            self._matrix = self._vectorizer.fit_transform(corpus)

    def search(self, query: str, top_k: int | None = None, min_score: float | None = None):
        top_k = top_k or settings.KB_TOP_K
        min_score = min_score if min_score is not None else settings.KB_MIN_SCORE

        with self._lock:
            if not self._articles or self._vectorizer is None:
                return []
            query_vec = self._vectorizer.transform([query])
            sims = cosine_similarity(query_vec, self._matrix)[0]

        ranked = sorted(
            zip(self._articles, sims), key=lambda x: x[1], reverse=True
        )
        results = []
        for article, score in ranked[:top_k]:
            if score < min_score:
                continue
            results.append({
                "id": article["id"],
                "title": article["title"],
                "content": article["content"],
                "score": round(float(score), 4),
            })
        return results


def _tags(article: dict) -> list[str]:
    import json
    try:
        return json.loads(article.get("tags") or "[]")
    except json.JSONDecodeError:
        return []


# Singleton dipakai di seluruh aplikasi
kb_search_engine = KBSearchEngine()
