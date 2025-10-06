# agent_system.py
import os
import uuid
import ollama
import qdrant_client
from qdrant_client.http import models
from fastembed import TextEmbedding
from typing import List, Dict, Any
from rich.console import Console

try:
    # When called via: python -m src.main
    from .data_models import FinalReport
    from .financial_config import get_financial_context, get_role_based_prompt
except ImportError:
    # When called via: python src/main.py
    from data_models import FinalReport
    from financial_config import get_financial_context, get_role_based_prompt

class AgentWorkflow:
    def __init__(self, collection_name: str, 
                 qdrant_host: str, 
                 ollama_host: str,
                 ollama_model=None, 
                 embedding_model="BAAI/bge-small-en-v1.5"):
        
        self.collection_name = collection_name
        # Prefer env override; default to a model tag that exists in your manifests
        self.llm_model = os.getenv("OLLAMA_MODEL", ollama_model or "mistral:7b-instruct-q4_0")
        self.console = Console()
        
        self.console.print(f"Connecting to Qdrant: {qdrant_host}:6333")
        self.console.print(f"Connecting to Ollama: {ollama_host}")
        self.console.print(f"Using LLM model: {self.llm_model}")

        self.qdrant_client = qdrant_client.QdrantClient(host=qdrant_host, port=6333)
        self.ollama_client = ollama.Client(host=ollama_host)
        self.embedding_model = TextEmbedding(model_name=embedding_model)
        
        self._ensure_collection_exists()

    def _ensure_collection_exists(self):
        """Check if Qdrant collection exists, create it if it doesn't."""
        try:
            self.qdrant_client.get_collection(collection_name=self.collection_name)
        except Exception:
            self.console.print(f"Collection '{self.collection_name}' does not exist, creating...")
            
            # The embed method returns a generator, which must be converted to a list
            # before we can access its elements by index.
            dummy_embedding = list(self.embedding_model.embed("test"))[0]
            vector_size = len(dummy_embedding)
            self.console.print(f"Detected vector dimension: {vector_size}")

            self.qdrant_client.recreate_collection(
                collection_name=self.collection_name,
                vectors_config=models.VectorParams(
                    size=vector_size,
                    distance=models.Distance.COSINE,
                ),
            )

    def embed_and_store(self, documents: List[Dict[str, Any]]):
        """Vectorize documents and store them in Qdrant."""
        texts = [doc["page_content"] for doc in documents]
        metadatas = [doc["metadata"] for doc in documents]
        
        self.console.print(f"Vectorizing and storing {len(texts)} documents...")
        
        # The embed method can return a generator, so we convert it to a list
        embeddings = list(self.embedding_model.embed(texts))
        
        points = [
            models.PointStruct(
                id=str(uuid.uuid4()), 
                vector=embedding.tolist(), 
                payload={**metadata, "page_content": text}
            )
            for text, metadata, embedding in zip(texts, metadatas, embeddings)
        ]

        self.qdrant_client.upsert(
            collection_name=self.collection_name,
            points=points,
            wait=True
        )
        self.console.print(f"Successfully stored {len(points)} documents in knowledge base '{self.collection_name}'.")

    def _retrieve_context(self, query: str, top_k: int = 5) -> List[str]:
        """Retrieve relevant context from Qdrant based on user query."""
        # Convert the generator to a list to access the first element
        query_embedding = list(self.embedding_model.embed([query]))[0]
        
        search_result = self.qdrant_client.search(
            collection_name=self.collection_name,
            query_vector=query_embedding.tolist(),
            limit=top_k,
        )
        
        context = [hit.payload["page_content"] for hit in search_result]
        return context

    def _run_agent(self, agent_name: str, prompt: str) -> str:
        """Run a single LLM Agent and return its response."""
        self.console.print(f"\n[bold yellow]>> Calling {agent_name}...[/bold yellow]")
        try:
            response = self.ollama_client.chat(
                model=self.llm_model,
                messages=[{'role': 'user', 'content': prompt}]
            )
            content = response['message']['content']
            self.console.print(f"[italic gray]{content}[/italic gray]")
            return content
        except Exception as e:
            self.console.print(f"[bold red]Error calling Ollama ({agent_name}): {e}[/bold red]")
            return f"Error: {agent_name} execution failed."

    def run(self, user_query: str) -> FinalReport:
        self.console.print(f"\n[cyan]🔍 Retrieving relevant context for query: '{user_query}'[/cyan]")
        context = self._retrieve_context(user_query)
        context_str = "\n\n---\n\n".join(context)
        
        if not context:
            self.console.print("[orange]Warning: No relevant context retrieved. Analysis will be based on general knowledge only.[/orange]")

        # Get financial professional context and role-based prompts
        financial_context = get_financial_context(user_query)
        analyst_role_prompt = get_role_based_prompt("analyst")

        analyst_prompt = f"""
        {analyst_role_prompt}
        
        [Financial Professional Background Knowledge]
        {financial_context}
        
        [Retrieved Relevant Data]
        {context_str}

        [User Question]
        {user_query}

        Please generate your professional analysis report draft, including:
        1. Core viewpoints and investment recommendations
        2. Key data and indicator analysis
        3. Risk factor assessment
        4. Time frame and expectations
        """
        analyst_report = self._run_agent("Analyst", analyst_prompt)

        checker_prompt = f"""
        As a fact-checker, please review the following report draft written by the analyst.
        Your tasks are:
        1. Identify any contradictions or logical inconsistencies in the report.
        2. Point out which key arguments most need data support (even if current data is insufficient).
        3. Evaluate the objectivity of the report.

        [Analyst Report Draft]
        {analyst_report}

        Please provide your fact-checking feedback:
        """
        checker_feedback = self._run_agent("Fact Checker", checker_prompt)

        critic_prompt = f"""
        As a professional adversarial critic (red team role), please read the following analysis report and its fact-checking feedback.
        Your task is to present strong counter-arguments. Challenge the core assumptions of the report, point out potential risks, unconsidered factors, or alternative explanations.
        Your goal is to ensure that all possible negative scenarios are considered in the final decision.

        [Analyst Report Draft]
        {analyst_report}

        [Fact-Checking Feedback]
        {checker_feedback}

        Please present your adversarial viewpoints and key risk warnings:
        """
        critic_critique = self._run_agent("Adversarial Critic", critic_prompt)

        synthesis_prompt = f"""
        As the final report writer, please synthesize all the following information to generate a structured final report.
        The report must include:
        1.  `summary`: Core answer summary to the user's question (2-3 sentences).
        2.  `key_findings`: List of main findings.
        3.  `counter_arguments`: Main counter-arguments or risks proposed by the adversarial critic.
        4.  `confidence_score`: Your overall confidence score for this report's conclusions (0.0 to 1.0).
        5.  `uncertainty_notes`: Explanation of the confidence score reasoning and what uncertainties exist in the report.

        [Original Question]: {user_query}
        [Analyst Report]: {analyst_report}
        [Fact-Checking Feedback]: {checker_feedback}
        [Adversarial Viewpoints]: {critic_critique}

        Please output your final report strictly in the following JSON format, without adding any additional explanations or text:
        {{
          "summary": "...",
          "key_findings": ["...", "..."],
          "counter_arguments": ["...", "..."],
          "confidence_score": 0.0,
          "uncertainty_notes": "..."
        }}
        """
        final_report_json_str = self._run_agent("Report Synthesizer", synthesis_prompt)

        try:
            if "```json" in final_report_json_str:
                final_report_json_str = final_report_json_str.split("```json\n")[1].split("```")[0]
            
            report_data = FinalReport.model_validate_json(final_report_json_str)
            return report_data
        except Exception as e:
            self.console.print(f"[bold red]Failed to parse final report JSON: {e}[/bold red]")
            return FinalReport(
                summary="System encountered a parsing error while generating the final report.",
                key_findings=[],
                counter_arguments=[],
                confidence_score=0.1,
                uncertainty_notes=f"Unable to parse LLM output. Raw output: {final_report_json_str}"
            )

