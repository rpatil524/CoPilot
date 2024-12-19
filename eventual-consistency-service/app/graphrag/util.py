import asyncio
import base64
import logging
import re
import traceback
from glob import glob

import httpx
from graphrag import reusable_channel, workers
from pyTigerGraph import TigerGraphConnection

from common.config import (
    doc_processing_config,
    embedding_service,
    get_llm_service,
    llm_config,
    milvus_config,
)
from common.embeddings.milvus_embedding_store import MilvusEmbeddingStore
from common.extractors import GraphExtractor, LLMEntityRelationshipExtractor
from common.extractors.BaseExtractor import BaseExtractor
from common.logs.logwriter import LogWriter

logger = logging.getLogger(__name__)
http_timeout = httpx.Timeout(15.0)

tg_sem = asyncio.Semaphore(2)
load_q = reusable_channel.ReuseableChannel()

# will pause workers until the event is false
loading_event = asyncio.Event()
loading_event.set() # set the event to true to allow the workers to run

async def install_queries(
    requried_queries: list[str],
    conn: TigerGraphConnection,
):
    # queries that are currently installed
    installed_queries = [q.split("/")[-1] for q in conn.getEndpoints(dynamic=True)]

    # doesn't need to be parallel since tg only does it one at a time
    for q in requried_queries:
        # only install n queries at a time (n=n_workers)
        q_name = q.split("/")[-1]
        # if the query is not installed, install it
        if q_name not in installed_queries:
            res = await workers.install_query(conn, q)
            # stop system if a required query doesn't install
            if res["error"]:
                raise Exception(res["message"])


async def init_embedding_index(s: MilvusEmbeddingStore, vertex_field: str):
    content = "init"
    vec = embedding_service.embed_query(content)
    await s.aadd_embeddings([(content, vec)], [{vertex_field: content}])
    s.remove_embeddings(expr=f"{vertex_field} in ['{content}']")


async def init(
    conn: TigerGraphConnection,
) -> tuple[BaseExtractor, dict[str, MilvusEmbeddingStore]]:
    # install requried queries
    requried_queries = [
        "common/gsql/graphRAG/StreamIds",
        "common/gsql/graphRAG/StreamDocContent",
        "common/gsql/graphRAG/SetEpochProcessing",
        "common/gsql/graphRAG/ResolveRelationships",
        "common/gsql/graphRAG/get_community_children",
        "common/gsql/graphRAG/entities_have_resolution",
        "common/gsql/graphRAG/communities_have_desc",
        "common/gsql/graphRAG/louvain/graphrag_louvain_init",
        "common/gsql/graphRAG/louvain/graphrag_louvain_communities",
        "common/gsql/graphRAG/louvain/modularity",
        "common/gsql/graphRAG/louvain/stream_community",
        "common/gsql/supportai/create_entity_type_relationships"
    ]
    # add louvain to queries
    q = [x.split(".gsql")[0] for x in glob("common/gsql/graphRAG/louvain/*")]
    requried_queries.extend(q)
    await install_queries(requried_queries, conn)

    # extractor
    if doc_processing_config.get("extractor") == "graphrag":
        extractor = GraphExtractor()
    elif doc_processing_config.get("extractor") == "llm":
        extractor = LLMEntityRelationshipExtractor(get_llm_service(llm_config))
    else:
        raise ValueError("Invalid extractor type")
    vertex_field = milvus_config.get("vertex_field", "vertex_id")
    index_names = milvus_config.get(
        "indexes",
        [
            "Document",
            "DocumentChunk",
            "Entity",
            "Relationship",
            "Community",
        ],
    )
    index_stores = {}
    async with asyncio.TaskGroup() as tg:
        for index_name in index_names:
            name = conn.graphname + "_" + index_name
            s = MilvusEmbeddingStore(
                embedding_service,
                host=milvus_config["host"],
                port=milvus_config["port"],
                support_ai_instance=True,
                collection_name=name,
                username=milvus_config.get("username", ""),
                password=milvus_config.get("password", ""),
                vector_field=milvus_config.get("vector_field", "document_vector"),
                text_field=milvus_config.get("text_field", "document_content"),
                vertex_field=vertex_field,
                drop_old=False,
            )

            LogWriter.info(f"Initializing {name}")
            # init collection if it doesn't exist
            if not s.check_collection_exists():
                tg.create_task(init_embedding_index(s, vertex_field))

            index_stores[name] = s

    return extractor, index_stores


def make_headers(conn: TigerGraphConnection):
    if conn.apiToken is None or conn.apiToken == "":
        tkn = base64.b64encode(f"{conn.username}:{conn.password}".encode()).decode()
        headers = {"Authorization": f"Basic {tkn}"}
    else:
        headers = {"Authorization": f"Bearer {conn.apiToken}"}

    return headers


async def stream_ids(
    conn: TigerGraphConnection, v_type: str, current_batch: int, ttl_batches: int
) -> dict[str, str | list[str]]:
    headers = make_headers(conn)

    try:
        async with httpx.AsyncClient(timeout=http_timeout) as client:
            async with tg_sem:
                res = await client.post(
                    f"{conn.restppUrl}/query/{conn.graphname}/StreamIds",
                    params={
                        "current_batch": current_batch,
                        "ttl_batches": ttl_batches,
                        "v_type": v_type,
                    },
                    headers=headers,
                )
        ids = res.json()["results"][0]["@@ids"]
        return {"error": False, "ids": ids}

    except Exception as e:
        exc = traceback.format_exc()
        LogWriter.error(f"/{conn.graphname}/query/StreamIds\nException Trace:\n{exc}")

        return {"error": True, "message": str(e)}


def map_attrs(attributes: dict):
    # map attrs
    attrs = {}
    for k, v in attributes.items():
        if isinstance(v, tuple):
            attrs[k] = {"value": v[0], "op": v[1]}
        elif isinstance(v, dict):
            attrs[k] = {
                "value": {"keylist": list(v.keys()), "valuelist": list(v.values())}
            }
        else:
            attrs[k] = {"value": v}
    return attrs


def process_id(v_id: str):
    v_id = v_id.replace(" ", "_").replace("/", "").replace("%", "percent")

    has_func = re.compile(r"(.*)\(").findall(v_id)
    if len(has_func) > 0:
        v_id = has_func[0]
    if v_id == "''" or v_id == '""':
        return ""
    v_id = v_id.replace("(", "").replace(")", "")

    return v_id


async def upsert_vertex(
    conn: TigerGraphConnection,
    vertex_type: str,
    vertex_id: str,
    attributes: dict,
):
    logger.info(f"Upsert vertex: {vertex_type} {vertex_id}")
    vertex_id = vertex_id.replace(" ", "_")
    attrs = map_attrs(attributes)
    await load_q.put(("vertices", (vertex_type, vertex_id, attrs)))


async def upsert_batch(conn: TigerGraphConnection, data: str):
    headers = make_headers(conn)
    async with httpx.AsyncClient(timeout=http_timeout) as client:
        async with tg_sem:
            try:
                res = await client.post(
                    f"{conn.restppUrl}/graph/{conn.graphname}", data=data, headers=headers
                )
            except Exception as e:
                err = traceback.format_exc()
                logger.error(f"Upsert err:\n{err}")
        res.raise_for_status()


async def check_vertex_exists(conn, v_id: str):
    headers = make_headers(conn)
    async with httpx.AsyncClient(timeout=http_timeout) as client:
        async with tg_sem:
            try:
                res = await client.get(
                    f"{conn.restppUrl}/graph/{conn.graphname}/vertices/Entity/{v_id}",
                    headers=headers,
                )

            except Exception as e:
                err = traceback.format_exc()
                logger.error(f"Check err:\n{err}")
        try:
            res.raise_for_status()
            return res.json()
        except Exception as e:
            logger.error(f"Check err:\n{e}\n{e}")
            return {"error": True, "message": e}


async def upsert_edge(
    conn: TigerGraphConnection,
    src_v_type: str,
    src_v_id: str,
    edge_type: str,
    tgt_v_type: str,
    tgt_v_id: str,
    attributes: dict = None,
):
    if attributes is None:
        attrs = {}
    else:
        attrs = map_attrs(attributes)
    src_v_id = src_v_id.replace(" ", "_")
    tgt_v_id = tgt_v_id.replace(" ", "_")
    await load_q.put(
        (
            "edges",
            (
                src_v_type,
                src_v_id,
                edge_type,
                tgt_v_type,
                tgt_v_id,
                attrs,
            ),
        )
    )


async def get_commuinty_children(conn, i: int, c: str):
    headers = make_headers(conn)
    async with httpx.AsyncClient(timeout=None) as client:
        async with tg_sem:
            try:
                resp = await client.get(
                    f"{conn.restppUrl}/query/{conn.graphname}/get_community_children",
                    params={"comm": c, "iter": i},
                    headers=headers,
                )
            except:
                logger.error(f"Get Children err:\n{traceback.format_exc()}")
        try:
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"Get Children err:\n{e}")
    descrs = []
    try:
        res = resp.json()["results"][0]["children"]
    except Exception as e:
        logger.error(f"Get Children err:\n{e}")
        res = []
    for d in res:
        desc = d["attributes"]["description"]
        # if it's the entity iteration
        if i == 1:
            # filter out empty strings
            desc = list(filter(lambda x: len(x) > 0, desc))
            # if there are no descriptions, make it the v_id
            if len(desc) == 0:
                desc.append(d["v_id"])
            descrs.extend(desc)
        else:
            descrs.append(desc)

    return descrs


async def check_all_ents_resolved(conn):
    headers = make_headers(conn)
    async with httpx.AsyncClient(timeout=None) as client:
        async with tg_sem:
            resp = await client.get(
                f"{conn.restppUrl}/query/{conn.graphname}/entities_have_resolution",
                headers=headers,
            )
        try:
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"Check Vert Desc err:\n{e}")

    res = resp.json()["results"][0]["all_resolved"]
    logger.info(resp.json()["results"])

    return res

async def add_rels_between_types(conn):
    headers = make_headers(conn)
    async with httpx.AsyncClient(timeout=None) as client:
        async with tg_sem:
            resp = await client.get(
                f"{conn.restppUrl}/query/{conn.graphname}/create_entity_type_relationships",
                headers=headers,
            )
        try:
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"Check Vert EntityType err:\n{e}")

    if resp.status_code != 200:
        logger.error(f"Check Vert EntityType err:\n{resp.text}")
        return {"error": True, "message": resp.text}
    else:
        res = resp.json()
        logger.info(resp.json())
        return res

async def check_vertex_has_desc(conn, i: int):
    headers = make_headers(conn)
    async with httpx.AsyncClient(timeout=None) as client:
        async with tg_sem:
            resp = await client.get(
                f"{conn.restppUrl}/query/{conn.graphname}/communities_have_desc",
                params={"iter": i},
                headers=headers,
            )
        try:
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"Check Vert Desc err:\n{e}")

    res = resp.json()["results"][0]["all_have_desc"]
    logger.info(resp.json()["results"])

    return res