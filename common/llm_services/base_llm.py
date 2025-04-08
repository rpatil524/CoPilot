class LLM_Model:
    """Base LLM_Model Class

    Used to connect to external LLM API services, and retrieve customized prompts for the tools.
    """

    def __init__(self, config):
        self.llm = None

    def _read_prompt_file(self, path):
        with open(path) as f:
            prompt = f.read()
        return prompt

    @property
    def map_question_schema_prompt(self):
        """Property to get the prompt for the MapQuestionToSchema tool."""
        raise ("map_question_schema_prompt not supported in base class")

    @property
    def generate_function_prompt(self):
        """Property to get the prompt for the GenerateFunction tool."""
        raise ("generate_function_prompt not supported in base class")

    @property
    def hyde_prompt(self):
        """Property to get the prompt for the HyDE tool."""
        return """You are a helpful agent that is writing an example of a document that might answer this question: {question}
                  Answer:"""

    @property
    def entity_relationship_extraction_prompt(self):
        """Property to get the prompt for the EntityRelationshipExtraction tool."""
        raise ("entity_relationship_extraction_prompt not supported in base class")

    @property
    def supportai_response_prompt(self):
        """Property to get the prompt for the SupportAI response."""
        return "Answer this question: {question}\nUse this information: {sources}"

    @property
    def question_expansion_prompt(self):
        """Property to get the prompt for the Question Expension response."""
        return """You are a helpful assistant responsible for generating 10 new questions similar to the original question below to represent its meaning in a more clear way.\nInclude a quality score for the answer, based on how well it represents the meaning of the original question. The quality score should be between 0 (poor) and 100 (excellent).\n\nQuestion: {question}\n\n{format_instructions}\n"""

    @property
    def graphrag_scoring_prompt(self):
        """Property to get the prompt for the GraphRAG Scoring response."""
        return """You are a helpful assistant responsible for generating an answer to the question below using the data provided.\nInclude a quality score for the answer, based on how well it answers the question. The quality score should be between 0 (poor) and 100 (excellent).\n\nQuestion: {question}\nContext: {context}\n\n{format_instructions}\n"""

    @property
    def model(self):
        """Property to get the external LLM model."""
        raise ("model not supported in base class")
