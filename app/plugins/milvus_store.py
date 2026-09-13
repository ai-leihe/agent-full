"""vector_store 槽位插件：把切片与向量落库到 Milvus，并对外提供相似度检索。

与 ``memory_store`` 同槽位、同协议：只要实现 :class:`SearchableIndex` 的
``search()``，上游（切片 / 向量化）与下游（``/api/search`` 等检索接口）都无需改动。

数据模型
--------
一个 Milvus 集合（collection）可以容纳**多次上传**的切片，靠 ``run_id`` 字段区分；
检索时按 ``run_id`` 过滤，因此每次上传的索引彼此隔离，不会串库。
集合的 ``vector`` 字段维度在首次创建时固定，后续写入维度不一致时直接拒绝——
这正是文档要求的「embedding 模型 / 维度不匹配就拒绝检索」。

依赖
----
``pymilvus`` 属于可选依赖（``pip install pymilvus``），未安装时插件仍会注册，
只在真正执行时给出明确报错。本地部署见 ``deploy/milvus/docker-compose.yml``。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..core.context import CHUNKS, EMBEDDINGS, VECTOR_INDEX, Chunk, PipelineContext
from ..core.db import from_json
from ..core.skill import Skill, skill
from ..core.warehouse import warehouse

DEFAULT_URI = "http://localhost:19530"

#: Milvus 集合命名规则：字母或下划线开头，仅含字母、数字、下划线
_COLLECTION_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Milvus VARCHAR 的 max_length 以 UTF-8 字节计，这里按字节安全截断
_TEXT_BYTES = 65535
_ID_BYTES = 128
_RUN_ID_BYTES = 64
_FILENAME_BYTES = 512

#: 向量字段名（协议不关心字段名，这里固定下来便于按名取维度）
_VECTOR_FIELD = "vector"

#: 只接受「分数越高越相似」的度量，才能与 memory_store 的余弦分数语义保持一致
_METRICS = ("COSINE", "IP")


def _pymilvus():
    """延迟导入可选依赖，未安装时给出可执行的修复建议。"""
    try:
        import pymilvus  # type: ignore
    except ImportError:  # pragma: no cover - 取决于运行环境
        raise RuntimeError("milvus_store 需要额外依赖，请执行：pip install pymilvus") from None
    return pymilvus


def _truncate(text: str, max_bytes: int) -> str:
    """按 UTF-8 字节数截断，避免多字节字符被截成半个字。"""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _jsonable(meta: dict[str, Any]) -> dict[str, Any]:
    """Chunk.meta 要写进 Milvus 的 JSON 字段，非 JSON 值降级为字符串。"""
    try:
        json.dumps(meta, ensure_ascii=False)
    except (TypeError, ValueError):
        return {str(key): str(value) for key, value in meta.items()}
    return meta


def _literal(value: str) -> str:
    """拼进过滤表达式前转义引号与反斜杠。"""
    return value.replace("\\", "\\\\").replace('"', '\\"')


class MilvusIndex:
    """Milvus 上的一个 run 级索引，满足 :class:`SearchableIndex` 协议。"""

    backend = "milvus"

    def __init__(
        self,
        *,
        uri: str,
        collection: str,
        run_id: str,
        embedder: str,
        dimension: int,
        size: int,
        metric_type: str = "COSINE",
        filename: str = "",
        token: str = "",
    ) -> None:
        self.uri = uri
        self.collection = collection
        self.run_id = run_id
        self.embedder = embedder
        self.dimension = int(dimension)
        self.metric_type = metric_type
        self.filename = filename
        self._size = int(size)
        self._token = token
        self._client: Any = None

    @property
    def size(self) -> int:
        """本索引包含的切片数（建索引时确定，供仓库列表展示）。"""
        return self._size

    # -------------------------------------------------- 连接

    def _connect(self):
        if self._client is None:
            kwargs: dict[str, Any] = {"uri": self.uri}
            if self._token:
                kwargs["token"] = self._token
            self._client = _pymilvus().MilvusClient(**kwargs)
        return self._client

    def _run_filter(self) -> str:
        return f'run_id == "{_literal(self.run_id)}"'

    def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响主流程
                pass

    # -------------------------------------------------- 检索

    def search(self, query_vector: list[float], top_k: int = 5) -> list[dict[str, Any]]:
        if top_k <= 0:
            return []
        if not query_vector:
            raise ValueError("查询向量为空，无法检索")
        if len(query_vector) != self.dimension:
            raise ValueError(
                f"查询向量维度 {len(query_vector)} 与索引维度 {self.dimension} 不一致，"
                f"请使用建索引时相同的 embedder（当前索引绑定 '{self.embedder}'）"
            )

        client = self._connect()
        # 集合可能因服务重启而处于未加载状态，检索前确保已加载
        client.load_collection(self.collection)
        rows = client.search(
            collection_name=self.collection,
            data=[[float(value) for value in query_vector]],
            filter=self._run_filter(),
            limit=int(top_k),
            output_fields=["chunk_index", "text", "meta"],
            # 强制强一致：刚写入的切片立即可检索，否则默认 Bounded 会「上传完搜不到」
            consistency_level="Strong",
        )
        hits: list[dict[str, Any]] = []
        for item in rows[0] if rows else []:
            entity = item.get("entity") or {}
            hits.append(
                {
                    "score": round(float(item.get("distance", 0.0)), 4),
                    "chunk_id": item.get("id"),
                    "index": entity.get("chunk_index"),
                    "text": entity.get("text", ""),
                    "meta": entity.get("meta") or {},
                }
            )
        return hits

    # -------------------------------------------------- 清理

    def drop(self) -> None:
        """删除本 run 在 Milvus 中的全部切片。

        集合可能被其它 run 共用，因此只按 ``run_id`` 删实体，绝不整表 drop。
        """
        client = self._connect()
        client.delete(
            collection_name=self.collection,
            filter=self._run_filter(),
            consistency_level="Strong",
        )
        self._size = 0


@skill
class MilvusStore(Skill):
    """Milvus 向量库：把切片与向量写入 Milvus，并按 run 隔离检索。"""

    name = "milvus_store"
    slot = "vector_store"
    optional = True
    description = "写入 Milvus 集合（本地 Standalone 或集群），按 run 隔离的相似度检索。"
    consumes = (CHUNKS, EMBEDDINGS)
    produces = (VECTOR_INDEX,)
    param_schema = {
        "uri": {
            "type": "str",
            "default": DEFAULT_URI,
            "label": "Milvus 地址",
            "help": "本地部署 default：http://localhost:19530（先执行 deploy/milvus 下的 docker compose up -d）",
        },
        "collection": {"type": "str", "default": "default", "label": "集合名称"},
        "metric_type": {
            "type": "str",
            "default": "COSINE",
            "label": "相似度度量",
            "choices": list(_METRICS),
            "help": "COSINE / IP 均为分数越高越相似，与 memory_store 口径一致",
        },
        "token": {
            "type": "str",
            "default": "",
            "label": "鉴权 Token",
            "help": "形如 user:password；本地无鉴权留空。建议用 ${MILVUS_TOKEN} 从 .env 注入",
        },
    }

    def configure(self, options: dict) -> None:
        self.uri = str(options.get("uri") or DEFAULT_URI).strip()
        self.collection = str(options.get("collection") or "default").strip()
        self.metric_type = str(options.get("metric_type") or "COSINE").upper()
        self.token = str(options.get("token") or "")
        if not _COLLECTION_RE.match(self.collection):
            raise ValueError(
                f"集合名称 '{self.collection}' 不合法：必须以字母或下划线开头，且只能包含字母、数字、下划线"
            )
        if self.metric_type not in _METRICS:
            raise ValueError(f"metric_type 仅支持 {' / '.join(_METRICS)}")
        self._client = None

    # -------------------------------------------------- 连接与建表

    def _connect(self):
        if self._client is None:
            kwargs: dict[str, Any] = {"uri": self.uri}
            if self.token:
                kwargs["token"] = self.token
            self._client = _pymilvus().MilvusClient(**kwargs)
        return self._client

    def _vector_dim(self, client, name: str) -> int | None:
        for field in client.describe_collection(name).get("fields", []):
            if field.get("name") == _VECTOR_FIELD:
                dim = (field.get("params") or {}).get("dim")
                return int(dim) if dim else None
        return None

    def _index_metric(self, client, name: str) -> str:
        for index_name in client.list_indexes(name):
            metric = str((client.describe_index(name, index_name) or {}).get("metric_type") or "")
            if metric:
                return metric.upper()
        return ""

    def _ensure_collection(self, client, dimension: int) -> None:
        """集合不存在则建表建索引；已存在则校验维度与度量是否兼容。"""
        if client.has_collection(self.collection):
            existing_dim = self._vector_dim(client, self.collection)
            if existing_dim is not None and existing_dim != dimension:
                raise ValueError(
                    f"Milvus 集合 '{self.collection}' 已存在，向量维度为 {existing_dim}，"
                    f"与本次向量维度 {dimension} 不一致；请更换集合名称或调整 embedder 维度"
                )
            existing_metric = self._index_metric(client, self.collection)
            if existing_metric and existing_metric != self.metric_type:
                raise ValueError(
                    f"Milvus 集合 '{self.collection}' 已用 {existing_metric} 度量建索引，"
                    f"与本次配置的 {self.metric_type} 不一致；请更换集合名称"
                )
            return

        pymilvus = _pymilvus()
        data_type = pymilvus.DataType
        schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", data_type.VARCHAR, max_length=_ID_BYTES, is_primary=True)
        schema.add_field("run_id", data_type.VARCHAR, max_length=_RUN_ID_BYTES)
        schema.add_field("chunk_index", data_type.INT64)
        schema.add_field("filename", data_type.VARCHAR, max_length=_FILENAME_BYTES)
        schema.add_field("text", data_type.VARCHAR, max_length=_TEXT_BYTES)
        schema.add_field("meta", data_type.JSON)
        schema.add_field(_VECTOR_FIELD, data_type.FLOAT_VECTOR, dim=dimension)

        index_params = client.prepare_index_params()
        index_params.add_index(field_name=_VECTOR_FIELD, index_type="AUTOINDEX", metric_type=self.metric_type)
        client.create_collection(
            self.collection,
            schema=schema,
            index_params=index_params,
            # 上传后立刻检索是平台的默认用法，集合直接建成强一致，避免「搜不到刚上传的内容」
            consistency_level="Strong",
        )

    # -------------------------------------------------- 执行

    def run(self, ctx: PipelineContext) -> None:
        chunks: list[Chunk] = ctx.require(CHUNKS)
        vectors: list[list[float]] = ctx.require(EMBEDDINGS)
        if len(chunks) != len(vectors):
            raise ValueError("切片数与向量数不一致，无法写入 Milvus")
        if not chunks:
            raise ValueError("切片为空，无法写入 Milvus")

        dimension = len(vectors[0])
        for position, vector in enumerate(vectors):
            if len(vector) != dimension:
                raise ValueError(
                    f"第 {position} 个向量维度 {len(vector)} 与首个向量 {dimension} 不一致，无法建索引"
                )
        if dimension <= 0:
            raise ValueError("向量维度为 0，无法建立 Milvus 索引")

        embedder = ctx.artifact_meta(EMBEDDINGS).get("embedder", "")
        client = self._connect()
        self._ensure_collection(client, dimension)

        rows = [
            {
                "id": _truncate(str(chunk.id), _ID_BYTES),
                "run_id": _truncate(ctx.run_id, _RUN_ID_BYTES),
                "chunk_index": int(chunk.index),
                "filename": _truncate(ctx.filename, _FILENAME_BYTES),
                "text": _truncate(chunk.text, _TEXT_BYTES),
                "meta": _jsonable(chunk.meta),
                "vector": [float(value) for value in vector],
            }
            for chunk, vector in zip(chunks, vectors)
        ]
        client.insert(collection_name=self.collection, data=rows)

        index = MilvusIndex(
            uri=self.uri,
            collection=self.collection,
            run_id=ctx.run_id,
            embedder=embedder,
            dimension=dimension,
            size=len(rows),
            metric_type=self.metric_type,
            filename=ctx.filename,
            token=self.token,
        )
        ctx.put(
            VECTOR_INDEX,
            index,
            producer=self.name,
            collection=self.collection,
            size=index.size,
            dimension=dimension,
            embedder=embedder,
            backend=index.backend,
            uri=self.uri,
        )
        warehouse.put(
            ctx.run_id,
            ctx.filename,
            index,
            collection=self.collection,
            backend=index.backend,
            uri=self.uri,
            metric_type=self.metric_type,
        )


def _rebuild_milvus_index(row: dict[str, Any]) -> Any:
    """进程重启后按数据库里的元数据重建 Milvus 索引句柄。

    向量本身留在 Milvus，不需要重复落库，这里只恢复「怎么连、连哪个集合」。
    鉴权 Token 不会写进数据库，因此重建出的句柄不带 Token，适用于
    ``deploy/milvus`` 那套无鉴权的本地 Standalone；需要鉴权时请在
    网络层放行应用，或自行扩展本函数从环境变量取 Token。
    """
    meta = from_json(row.get("meta"), {}) or {}
    return MilvusIndex(
        uri=str(meta.get("uri") or DEFAULT_URI),
        collection=str(row.get("collection") or "default"),
        run_id=str(row.get("run_id") or ""),
        embedder=str(row.get("embedder") or ""),
        dimension=int(row.get("dimension") or 0),
        size=int(row.get("vec_size") or 0),
        metric_type=str(meta.get("metric_type") or "COSINE").upper(),
        filename=str(row.get("filename") or ""),
        token=str(meta.get("token") or ""),
    )


#: 注册重建工厂：``Warehouse`` 重启后能按元数据把 Milvus 索引接回来
warehouse.register_rebuilder("milvus", _rebuild_milvus_index)
