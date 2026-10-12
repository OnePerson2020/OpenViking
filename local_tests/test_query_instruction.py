"""embedding.dense.query_param reaches Volcengine as a prefix on text queries only."""
from types import SimpleNamespace

from openviking.models.embedder.volcengine_embedders import VolcengineDenseEmbedder
from openviking_cli.utils.config.embedding_config import EmbeddingConfig

PREFIX = "Target_modality: text.\nInstruction:根据问题检索相关的记忆笔记\nQuery:"


def _capture(embedder):
    sent = []

    def create(input, model, extra_headers):
        sent.append(input)
        return SimpleNamespace(data=SimpleNamespace(embedding=[1.0, 0.0]), usage=None)

    embedder.client = SimpleNamespace(multimodal_embeddings=SimpleNamespace(create=create))
    return sent


def test_prefix_on_text_queries_only():
    e = VolcengineDenseEmbedder("m", api_key="k", dimension=2, query_param=PREFIX)
    sent = _capture(e)
    image = [{"type": "image_url", "image_url": {"url": "http://x/a.png"}}]
    e.embed("哪个方案", is_query=True)
    e.embed("document text")
    e.embed(image, is_query=True)
    assert sent == [
        [{"type": "text", "text": PREFIX + "哪个方案"}],
        [{"type": "text", "text": "document text"}],
        image,
    ]


def test_factory_passes_query_param():
    cfg = EmbeddingConfig(dense={"provider": "volcengine", "model": "m", "api_key": "k",
                                 "dimension": 2, "query_param": PREFIX})
    assert cfg.get_embedder().query_param == PREFIX
