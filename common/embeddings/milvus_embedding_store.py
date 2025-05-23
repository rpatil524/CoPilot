import logging
import traceback
from time import sleep, time
from typing import Iterable, List, Optional, Tuple

import Levenshtein as lev
from asyncer import asyncify
from langchain_community.vectorstores import Milvus
from langchain_core.documents.base import Document
# from langchain_milvus.vectorstores import Milvus
from langchain_community.vectorstores.milvus import Milvus
from pymilvus import MilvusException, connections, utility
from pymilvus.exceptions import MilvusException

from common.embeddings.base_embedding_store import EmbeddingStore
from common.embeddings.embedding_services import EmbeddingModel
from common.logs.log import req_id_cv
from common.logs.logwriter import LogWriter
from common.metrics.prometheus_metrics import metrics

logger = logging.getLogger(__name__)


class MilvusEmbeddingStore(EmbeddingStore):
    def __init__(
        self,
        embedding_service: EmbeddingModel,
        host: str,
        port: str,
        support_ai_instance: bool,
        collection_name: str = "tg_documents",
        metric_type: str = "COSINE",
        vector_field: str = "vector_field",
        text_field: str = "text",
        vertex_field: str = "",
        username: str = "",
        password: str = "",
        alias: str = "default",
        retry_interval: int = 2,
        max_retry_attempts: int = 10,
        drop_old=False,
    ):
        self.embedding_service = embedding_service
        self.vector_field = vector_field
        self.vertex_field = vertex_field
        self.text_field = text_field
        self.support_ai_instance = support_ai_instance
        self.collection_name = collection_name
        self.metric_type = metric_type.upper()
        self.milvus_alias = alias
        self.retry_interval = retry_interval
        self.max_retry_attempts = max_retry_attempts
        self.drop_old = drop_old

        if host.startswith("http"):
            if host.endswith(str(port)):
                uri = host
            else:
                uri = f"{host}:{port}"

            self.milvus_connection = {
                "alias": self.milvus_alias,
                "uri": uri,
                "user": username,
                "password": password,
                "timeout": 30,
            }
        else:
            self.milvus_connection = {
                "alias": self.milvus_alias,
                "host": host,
                "port": port,
                "user": username,
                "password": password,
                "timeout": 30,
            }

        self.connect_to_milvus()

        if not self.support_ai_instance:
            self.load_documents()

    def set_collection_name(self, collection_name: str = "tg_documents", vector_field: str = None, text_field: str = None, vertex_field: str = None):
        self.collection_name = collection_name
        if vector_field:
            self.vector_field = vector_field
        if text_field:
            self.text_field = text_field
        if vertex_field:
            self.vertex_field = vertex_field
        self.connect_to_milvus()

    def connect_to_milvus(self):
        retry_attempt = 0
        while retry_attempt < self.max_retry_attempts:
            try:
                connections.connect(**self.milvus_connection)
                # metrics.milvus_active_connections.labels(self.collection_name).inc
                self.milvus = Milvus(
                    embedding_function=self.embedding_service,
                    collection_name=self.collection_name,
                    index_params={"metric_type": self.metric_type, "index_type": "HNSW", "params": {"M": 64, "efConstruction": 360}},
                    search_params={"metric_type": self.metric_type},
                    connection_args=self.milvus_connection,
                    auto_id=True,
                    drop_old=self.drop_old,
                    text_field=self.text_field,
                    vector_field=self.vector_field,
                )
                LogWriter.info(
                    f"""Initializing Milvus with host={self.milvus_connection.get("host", self.milvus_connection.get("uri", "unknown host"))},
                    port={self.milvus_connection.get('port', 'unknown')}, username={self.milvus_connection.get('user', 'unknown')}, alias={self.milvus_connection.get('alias', 'unknown')}, collection={self.collection_name}, metric_type={self.metric_type}, vector_field={self.vector_field}, text_field={self.text_field}, vertex_field={self.vertex_field}"""
                )
                LogWriter.info(f"Milvus version {utility.get_server_version()}")
                return
            except MilvusException as e:
                retry_attempt += 1
                if retry_attempt >= self.max_retry_attempts:
                    raise e
                else:
                    LogWriter.info(
                        f"Failed to connect to Milvus. Retrying in {self.retry_interval} seconds."
                    )
                    sleep(self.retry_interval)

    def check_collection_exists(self):
        connections.connect(**self.milvus_connection)
        return utility.has_collection(self.collection_name, using=self.milvus_alias)

    def load_documents(self):
        if not self.check_collection_exists():
            from langchain_community.document_loaders import DirectoryLoader, JSONLoader

            def metadata_func(record: dict, metadata: dict) -> dict:
                metadata["function_header"] = record.get("function_header")
                metadata["description"] = record.get("description")
                metadata["param_types"] = record.get("param_types")
                metadata["custom_query"] = record.get("custom_query")
                metadata["graphname"] = "all"
                return metadata

            LogWriter.info("Milvus add initial load documents init()")
            import os

            logger.info(f"*******{os.path.exists('tg_documents')}")
            loader = DirectoryLoader(
                "./common/tg_documents/",
                glob="*.json",
                loader_cls=JSONLoader,
                loader_kwargs={
                    "jq_schema": ".",
                    "content_key": "docstring",
                    "metadata_func": metadata_func,
                },
            )
            docs = loader.load()

            # logger.info(f"docs: {docs}")

            operation_type = "load_upsert"
            metrics.milvus_query_total.labels(
                self.collection_name, operation_type
            ).inc()
            start_time = time()

            self.milvus.upsert(documents=docs)

            duration = time() - start_time
            metrics.milvus_query_duration_seconds.labels(
                self.collection_name, operation_type
            ).observe(duration)
            LogWriter.info("Milvus finish initial load documents init()")

            LogWriter.info("Milvus initialized successfully")
        else:
            LogWriter.info("Milvus already initialized, skipping initial document load")

    def add_embeddings(
        self,
        embeddings: Iterable[Tuple[str, List[float]]],
        metadatas: List[dict] = None,
    ):
        """Add Embeddings.
        Add embeddings to the Embedding store.
        Args:
            embeddings (Iterable[Tuple[str, List[float]]]):
                Iterable of content and embedding of the document.
            metadatas (List[Dict]):
                List of dictionaries containing the metadata for each document.
                The embeddings and metadatas list need to have identical indexing.
        """
        try:
            if metadatas is None:
                metadatas = []

            # add fields required by Milvus if they do not exist
            if self.support_ai_instance:
                for metadata in metadatas:
                    if self.vertex_field not in metadata:
                        metadata[self.vertex_field] = ""
            else:
                for metadata in metadatas:
                    if "seq_num" not in metadata:
                        metadata["seq_num"] = 1
                    if "source" not in metadata:
                        metadata["source"] = ""

            LogWriter.info(
                f"request_id={req_id_cv.get()} Milvus ENTRY add_embeddings()"
            )
            texts = [text for text, _ in embeddings]

            operation_type = "add_texts"
            metrics.milvus_query_total.labels(
                self.collection_name, operation_type
            ).inc()
            start_time = time()

            added = self.milvus.add_texts(texts=texts, metadatas=metadatas)

            duration = time() - start_time
            metrics.milvus_query_duration_seconds.labels(
                self.collection_name, operation_type
            ).observe(duration)

            LogWriter.info(f"request_id={req_id_cv.get()} Milvus EXIT add_embeddings()")

            # Check if registration was successful
            if added:
                success_message = f"Document registered with id: {added[0]}"
                LogWriter.info(success_message)
                return success_message
            else:
                error_message = f"Failed to register document {added}"
                LogWriter.error(error_message)
                raise Exception(error_message)

        except Exception as e:
            error_message = f"An error occurred while registering document: {str(e)}"
            LogWriter.error(error_message)

    async def aadd_embeddings(
        self,
        embeddings: Iterable[Tuple[str, List[float]]],
        metadatas: List[dict] = None,
    ):
        """Async Add Embeddings.
        Add embeddings to the Embedding store.
        Args:
            embeddings (Iterable[Tuple[str, List[float]]]):
                Iterable of content and embedding of the document.
            metadatas (List[Dict]):
                List of dictionaries containing the metadata for each document.
                The embeddings and metadatas list need to have identical indexing.
        """
        try:
            if metadatas is None:
                metadatas = []

            # add fields required by Milvus if they do not exist
            if self.support_ai_instance:
                for metadata in metadatas:
                    if self.vertex_field not in metadata:
                        metadata[self.vertex_field] = ""
            else:
                for metadata in metadatas:
                    if "seq_num" not in metadata:
                        metadata["seq_num"] = 1
                    if "source" not in metadata:
                        metadata["source"] = ""

            LogWriter.info(
                f"request_id={req_id_cv.get()} Milvus ENTRY aadd_embeddings()"
            )
            texts = [text for text, _ in embeddings]

            # operation_type = "add_texts"
            # metrics.milvus_query_total.labels(
            #     self.collection_name, operation_type
            # ).inc()
            # start_time = time()

            added = await self.milvus.aadd_texts(texts=texts, metadatas=metadatas)

            # duration = time() - start_time
            # metrics.milvus_query_duration_seconds.labels(
            #     self.collection_name, operation_type
            # ).observe(duration)

            LogWriter.info(
                f"request_id={req_id_cv.get()} Milvus EXIT aadd_embeddings()"
            )

            # Check if registration was successful
            if added:
                success_message = f"Document registered with id: {added[0]}"
                LogWriter.info(success_message)
                return success_message
            else:
                error_message = f"Failed to register document {added}"
                LogWriter.error(error_message)
                raise Exception(error_message)

        except Exception as e:
            error_message = f"An error occurred while registering document:{metadatas} ({len(texts)},{len(metadatas)})\nErr: {str(e)}"
            LogWriter.error(error_message)
            exc = traceback.format_exc()
            LogWriter.error(exc)
            LogWriter.error(f"{texts}")
            raise e

    def get_pks(
        self,
        expr: str,
    ):
        try:
            LogWriter.info(f"request_id={req_id_cv.get()} Milvus ENTRY get_pks()")

            ids = self.milvus.get_pks(expr=expr)
            if ids:
                return ids
            else:
                return []
        except Exception as e:
            error_message = f"An error occurred while getting pks of document: {str(e)}"
            LogWriter.error(error_message)
            raise e

    def has_embeddings(
        self,
        ids: List[str]
    ):
        return self.get_pks(f"vertex_id in {ids}")

    def upsert_embeddings(
        self,
        id: str,
        embeddings: Iterable[Tuple[str, List[float]]],
        metadatas: Optional[List[dict]] = None,
    ):
        try:
            LogWriter.info(
                f"request_id={req_id_cv.get()} Milvus ENTRY upsert_document()"
            )

            if metadatas is None:
                metadatas = []

            # add fields required by Milvus if they do not exist
            if self.support_ai_instance:
                LogWriter.info(
                    f"This is a SupportAI instance and needs vertex ids stored at {self.vertex_field}"
                )
                for metadata in metadatas:
                    if self.vertex_field not in metadata:
                        metadata[self.vertex_field] = ""
            else:
                for metadata in metadatas:
                    if "seq_num" not in metadata:
                        metadata["seq_num"] = 1
                    if "source" not in metadata:
                        metadata["source"] = ""

            documents = []

            # Iterate over embeddings and metadatas simultaneously
            for (text, embedding), metadata in zip(embeddings, metadatas or []):
                # Create a document with text as page content
                document = Document(page_content=text)

                # Add embedding to metadata
                if metadata is None:
                    metadata = {}
                # metadata["embedding"] = embedding

                # Add metadata to document
                document.metadata = metadata

                # Append document to the list
                documents.append(document)

            # Perform upsert operation
            operation_type = "upsert"
            if id is not None and id.strip():
                LogWriter.info(f"id: {id}")
                LogWriter.info(f"documents: {documents}")

                metrics.milvus_query_total.labels(
                    self.collection_name, operation_type
                ).inc()
                start_time = time()

                upserted = self.milvus.upsert(ids=[int(id)], documents=documents)

                duration = time() - start_time
                metrics.milvus_query_duration_seconds.labels(
                    self.collection_name, operation_type
                ).observe(duration)
            else:
                metrics.milvus_query_total.labels(
                    self.collection_name, operation_type
                ).inc()
                start_time = time()

                LogWriter.info(f"documents: {documents}")
                upserted = self.milvus.upsert(documents=documents)

                duration = time() - start_time
                metrics.milvus_query_duration_seconds.labels(
                    self.collection_name, operation_type
                ).observe(duration)

            LogWriter.info(
                f"request_id={req_id_cv.get()} Milvus EXIT upsert_document()"
            )

            # Check if upsertion was successful
            if upserted:
                success_message = f"Document upserted with id: {upserted[0]}"
                LogWriter.info(success_message)
                return success_message
            else:
                error_message = f"Failed to upsert document {upserted}"
                LogWriter.error(error_message)
                raise Exception(error_message)

        except Exception as e:
            error_message = f"An error occurred while upserting document: {str(e)}"
            LogWriter.error(error_message)
            raise e

    def remove_embeddings(
        self, ids: Optional[List[str]] = None, expr: Optional[str] = None
    ):
        try:
            LogWriter.info(f"request_id={req_id_cv.get()} Milvus ENTRY delete()")

            if not self.check_collection_exists():
                LogWriter.info(
                    f"request_id={req_id_cv.get()} Milvus collection {self.collection_name} does not exist"
                )
                LogWriter.info(f"request_id={req_id_cv.get()} Milvus EXIT delete()")
                return f"Milvus collection {self.collection_name} does not exist"

            # Check if ids or expr are provided
            if ids is None and expr is None:
                raise ValueError("Either id string or expr string must be provided.")

            # Perform deletion based on provided IDs or expression
            if expr:
                # Delete by expression
                start_time = time()
                metrics.milvus_query_total.labels(self.collection_name, "delete").inc()
                deleted = self.milvus.delete(expr=expr)
                end_time = time()
                metrics.milvus_query_duration_seconds.labels(
                    self.collection_name, "delete"
                ).observe(end_time - start_time)
                deleted_message = f"deleted by expression: {expr} {deleted}"
            elif ids:
                ids = [int(x) for x in ids]
                # Delete by ids
                start_time = time()
                metrics.milvus_query_total.labels(self.collection_name, "delete").inc()
                deleted = self.milvus.delete(ids=ids)
                end_time = time()
                metrics.milvus_query_duration_seconds.labels(
                    self.collection_name, "delete"
                ).observe(end_time - start_time)
                deleted_message = f"deleted by id(s): {ids} {deleted}"

            LogWriter.info(f"request_id={req_id_cv.get()} Milvus EXIT delete()")

            # Check if deletion was successful
            if deleted:
                success_message = f"Document(s) {deleted_message}."
                LogWriter.info(success_message)
                return success_message
            else:
                error_message = f"Failed to delete document(s). {deleted_message}"
                LogWriter.error(error_message)
                raise Exception(error_message)

        except Exception as e:
            error_message = f"An error occurred while deleting document(s): {str(e)}"
            LogWriter.error(error_message)
            raise e

    def retrieve_similar(self, query_embedding, top_k=10, filter_expr: str = None):
        res = retrieve_similar_with_score(query_embedding, top_k=top_k, filter_expr=filter_expr)
        similar = [doc[0] for doc in res]
        return similar

    def retrieve_similar_with_score(self, query_embedding, top_k=10, similarity_threshold=0.90, filter_expr: str = None):
        """Retireve Similar.
        Retrieve similar embeddings from the vector store given a query embedding.
        Args:
            query_embedding (List[float]):
                The embedding to search with.
            top_k (int, optional):
                The number of documents to return. Defaults to 10.
        Returns:
            https://api.python.langchain.com/en/latest/documents/langchain_core.documents.base.Document.html#langchain_core.documents.base.Document
            Document results for search.
        """
        try:
            LogWriter.info(
                f"request_id={req_id_cv.get()} Milvus ENTRY similarity_search_by_vector()"
            )

            start_time = time()
            metrics.milvus_query_total.labels(
                self.collection_name, "similarity_search_by_vector"
            ).inc()
            similar = self.milvus.similarity_search_with_score_by_vector(
                embedding=query_embedding, k=top_k*2, expr=filter_expr
            )
            end_time = time()
            metrics.milvus_query_duration_seconds.labels(
                self.collection_name, "similarity_search_by_vector"
            ).observe(end_time - start_time)

            sim_ids = [doc[0].metadata.get("function_header") for doc in similar]
            logger.debug(
                f"request_id={req_id_cv.get()} Milvus similarity_search_by_vector() retrieved={sim_ids}"
            )
            # Convert pk from int to str for each document
            for doc in similar:
                doc[0].metadata["pk"] = str(doc[0].metadata["pk"])
            LogWriter.info(
                f"request_id={req_id_cv.get()} Milvus EXIT similarity_search_by_vector()"
            )

            similar.sort(key=lambda x: x[1], reverse=True)
            i = 0
            for i in range(len(similar)):
                if similar[i][1] < similarity_threshold:
                    break
            if i <= top_k:
                return similar[:top_k]
            else:
                return similar[:i]

        except Exception as e:
            error_message = f"An error occurred while retrieving docuements: {str(e)}"
            LogWriter.error(error_message)
            raise e

    def add_connection_parameters(self, query_params: dict) -> dict:
        """Add Connection Parameters.
        Add connection parameters to the query parameters.
        Args:
            query_params (dict):
                Dictionary containing the parameters for the GSQL query.
        Returns:
            A dictionary containing the connection parameters.
        """
        if self.milvus_connection.get("uri", "") != "":
            if self.milvus_connection.get("user", "") != "":
                user = self.milvus_connection.get("user", "")
                pwd = self.milvus_connection.get("password", "")
                host = self.milvus_connection.get("uri", "")
                # build uri with user and password
                method = host.split(":")[0]
                host = host.split("://")[1]
                query_params["milvus_host"] = f"{method}://{user}:{pwd}@{host}"
            else:
                query_params["milvus_host"] = self.milvus_connection.get("uri", "")
            query_params["milvus_port"] = int(host.split(":")[-1])
        else:
            if self.milvus_connection.get("user", "") != "":
                user = self.milvus_connection.get("user", "")
                pwd = self.milvus_connection.get("password", "")
                host = self.milvus_connection.get("host", "")
                # build uri with user and password
                method = host.split(":")[0]
                host = host.split("://")[1]
                query_params["milvus_host"] = f"{method}://{user}:{pwd}@{host}"
            else:
                query_params["milvus_host"] = self.milvus_connection.get("host", "")
            query_params["milvus_port"] = int(self.milvus_connection.get("port", ""))
        query_params["vector_field_name"] = "document_vector"
        query_params["vertex_id_field_name"] = "vertex_id"
        return query_params

    def list_registered_documents(
        self,
        graphname: str = None,
        only_custom: bool = False,
        output_fields: List[str] = ["*"],
    ):
        if only_custom and graphname:
            res = self.milvus.col.query(
                expr="custom_query == true and graphname == '" + graphname + "'",
                output_fields=output_fields,
            )
        elif only_custom:
            res = self.milvus.col.query(
                expr="custom_query == true", output_fields=output_fields
            )
        elif graphname:
            res = self.milvus.col.query(
                expr="graphname == '" + graphname + "'", output_fields=output_fields
            )
        else:
            res = self.milvus.col.query(
                expr="", limit=5000, output_fields=output_fields
            )
        return res

    def query(self, expr: str, output_fields: List[str]):
        """Get output fields with expression

        Args:
            expr: Expression - E.g: "pk > 0"

        Returns:
            List of output fields' contents
        """

        if self.milvus.col is None:
            LogWriter.info("No existing collection to query.")
            return None

        try:
            query_result = self.milvus.col.query(expr=expr, output_fields=output_fields)
        except MilvusException as exc:
            LogWriter.error(
                f"Failed to get outputs: {self.milvus.collection_name} error: {exc}"
            )
            raise exc

        return query_result

    def edit_dist_check(self, a: str, b: str, edit_dist_threshold: float):
        a = a.lower()
        b = b.lower()
        # if the words are short, they should be the same
        if len(a) < 5 and len(b) < 5:
            return a == b

        # edit_dist_threshold (as a percent) of word must match
        threshold = int(min(len(a), len(b)) * (1 - edit_dist_threshold))
        return lev.distance(a, b) < threshold

    async def aget_k_closest(
        self, v_id: str, k=10, threshold_similarity=0.90, edit_dist_threshold_pct=0.75
    ) -> list[Document]:
        threshold_dist = 1 - threshold_similarity

        # asyncify necessary funcs
        query = asyncify(self.milvus.col.query)
        search = asyncify(self.milvus.similarity_search_with_score_by_vector)

        # Get all vectors with this ID
        verts = await query(
            f'{self.vertex_field} == "{v_id}"',
            output_fields=[self.vertex_field, self.vector_field],
        )
        result = []
        for v in verts:
            # get the k closest verts
            sim = await search(
                v["document_vector"],
                k=k,
            )
            # filter verts using similiarity threshold and leven_dist
            similar_verts = [
                doc.metadata["vertex_id"]
                for doc, dist in sim
                # check semantic similarity
                if dist < threshold_dist
                # check name similarity (won't merge Apple and Google if they're semantically similar)
                and self.edit_dist_check(
                    doc.metadata["vertex_id"],
                    v_id,
                    edit_dist_threshold_pct,
                )
                # don't have to merge verts with the same id (they're the same)
                and doc.metadata["vertex_id"] != v_id
            ]
            result.extend(similar_verts)
        result.append(v_id)
        return set(result)

    def __del__(self):
        metrics.milvus_active_connections.labels(self.collection_name).dec
