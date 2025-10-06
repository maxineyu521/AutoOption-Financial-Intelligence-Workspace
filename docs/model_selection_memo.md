Model Selection Memo
Our production model is options-expert, a lightweight, specialized variant of mistral:7b-instruct-q4_0, deployed via Ollama. This decision balances performance, resource constraints, and domain-specific requirements.

1. Base Model Rationale: mistral:7b-instruct-q4_0

We selected mistral:7b-instruct-q4_0 as our base model, prioritizing execution efficiency over raw parameter count.

Efficiency: While larger models may offer marginal gains in reasoning, the superior inference speed and low resource footprint of mistral:7b-instruct-q4_0 are critical. This prevents performance bottlenecks in our multi-turn agent loop, which runs in a constrained CPU/RAM environment.

Capability: The model’s strong instruction-following and Retrieval-Augmented Generation (RAG) capabilities are sufficient for this educational task. When paired with the high-quality context from our Qdrant knowledge base, it ensures the required citation fidelity and reasoning accuracy.

2. Domain-Specific Customization: options-expert

The options-expert variant was created by applying a lightweight specialization layer on top of the base model using an Ollama Modelfile. This approach avoids the high cost of full fine-tuning by:

Embedding a System Prompt: A custom prompt is included to orient the model toward options trading and risk-averse strategy preferences.

Tuning Inference Parameters: Parameters are adjusted to align outputs with financial domain conventions and formatting.

3. Summary of Advantages

Compared to general-purpose models, the final options-expert model provides significant advantages:

High-Relevance Outputs: It produces more consistent and contextually aware analysis for specialized tasks, such as evaluating Greeks, assessing volatility regimes, and selecting strategies.

Cost-Effective & Secure: The solution is highly cost-effective and supports on-premise deployment, ensuring data privacy and system autonomy.