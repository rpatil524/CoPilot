import logging
from langchain_core.output_parsers import StrOutputParser
from langchain.prompts import PromptTemplate
from langchain.tools import BaseTool
from langchain.llms.base import LLM
from common.metrics.tg_proxy import TigerGraphConnectionProxy

logger = logging.getLogger(__name__)


class GenerateCypher(BaseTool):
    """GenerateCypher Tool.
    Tool to generate and execute the appropriate Cypher query for the question.
    """
    name = "GenerateCypher"
    description = "Generates a Cypher query for the question."
    conn: TigerGraphConnectionProxy = None
    llm: LLM = None

    def __init__(self, conn: TigerGraphConnectionProxy, llm):
        """Initialize GenerateCypher.
        Args:
            conn (TigerGraphConnection):
                pyTigerGraph TigerGraphConnection connection to the appropriate database/graph with correct permissions
            llm (LLM_Model):
                LLM_Model class to interact with an external LLM API.
            prompt (str):
                prompt to use with the LLM_Model. Varies depending on LLM service.
        """
        super().__init__()
        self.conn = conn
        self.llm = llm

    def _generate_schema_rep(self):
        verts = self.conn.getVertexTypes()
        edges = self.conn.getEdgeTypes()
        vertex_schema = []
        for vert in verts:
            primary_id = self.conn.getVertexType(vert)["PrimaryId"]["AttributeName"]
            attributes = "\n\t\t".join([attr["AttributeName"] + " of type " + attr["AttributeType"]["Name"] 
                                        for attr in self.conn.getVertexType(vert)["Attributes"]])
            if attributes == "":
                attributes = "No attributes"
            vertex_schema.append(f"{vert}\n\tPrimary Id Attribute: {primary_id}\n\tAttributes: \n\t\t{attributes}")

        edge_schema = []
        for edge in edges:
            from_vertex = self.conn.getEdgeType(edge)["FromVertexTypeName"]
            to_vertex = self.conn.getEdgeType(edge)["ToVertexTypeName"]
            direction = "Directed" if self.conn.getEdgeType(edge)["IsDirected"] else "Undirected"
            #reverse_edge = conn.getEdgeType(edge)["Config"].get("REVERSE_EDGE")
            attributes = "\n\t\t".join([attr["AttributeName"] + " of type " + attr["AttributeType"]["Name"] 
                                        for attr in self.conn.getEdgeType(edge)["Attributes"]])
            if attributes == "":
                attributes = "No attributes"
            if from_vertex == "*" or to_vertex == "*":
                edge_pairs = self.conn.getEdgeType(edge)["EdgePairs"]
                for an_edge in edge_pairs:
                    edge_info = f"""From Vertex: {an_edge["From"]}\n\tTo Vertex: {an_edge["To"]}"""
                    edge_schema.append(f"""{edge}\n\t{edge_info}\n\tEdge direction: {direction}\n\tAttributes: \n\t\t{attributes}""")
            else:
                edge_info = f"""From Vertex: {from_vertex}\n\tTo Vertex: {to_vertex}"""
                edge_schema.append(f"""{edge}\n\t{edge_info}\n\tEdge direction: {direction}\n\tAttributes: \n\t\t{attributes}""")

        schema_rep = f"""The schema of the graph is as follows:
Vertex Types:
{chr(10).join(vertex_schema)}

Edge Types:
{chr(10).join(edge_schema)}
"""
        return schema_rep
        
    def generate_cypher(self, question: str) -> str:
        """Generate Cypher query for the question.
        Args:
            question (str):
                question to generate the Cypher query for.
        Returns:
            str:
                Cypher query for the question.
        """
        PROMPT = PromptTemplate(
            template="""You're an expert in OpenCypher programming. Given the following schema: {schema}, what is the OpenCypher query that retrieves the {question} 
                        Only include attributes that are found in the schema. Never include any attributes that are not found in the schema.
                        Use attributes instead of primary id if attribute name is closer to the keyword type in the question.
                        Use as less vertex type, edge type and attributes as possible. If an attribute is not found in the schema, please exclude it from the query.
                        Do not return attributes that are not explicitly mentioned in the question. If a vertex type is mentioned in the question, only return the vertex.
                        Never use directed edge pattern in the OpenCypher query. Always use and create query using undirected pattern.
                        Always use double quotes for strings instead of single quotes.

                        You cannot use the following clauses:
                        OPTIONAL MATCH
                        CREATE
                        MERGE
                        REMOVE
                        UNION
                        UNION ALL
                        UNWIND
                        SET

                        Make sure to have correct attribute names in the OpenCypher query and not to name result aliases that are vertex or edge types.
                        
                        ONLY write the OpenCypher query in the response. Do not include any other information in the response.""",
            input_variables=[
                "question",
                "schema"
            ]
        )

        schema = self._generate_schema_rep()
    
        logger.info("Prompt to LLM:\n" + PROMPT.invoke({"question": question, "schema": schema}).to_string())

        chain = PROMPT | self.llm.model | StrOutputParser()
        out = chain.invoke({"question": question, "schema": schema}).strip("```cypher").strip("```")
        query_header = "USE GRAPH " + self.conn.graphname + " "+ "\n" + "INTERPRET OPENCYPHER QUERY () {" + "\n"
        query_footer = "\n}"
        return query_header + out + query_footer
    
    def _run(self, question: str):
        """Run the GenerateCypher tool.
        Args:
            question (str):
                question to generate the Cypher query for.
        Returns:
            str:
                Cypher query for the question.
        """
        return self.generate_cypher(question)
    
    def _arun(self, question: str):
        raise NotImplementedError("Asynchronous execution is not supported for this tool.")
