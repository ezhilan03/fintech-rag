# src/api/prompt.py
"""
Prompt templates for the RAG query API.

Design principles:
1. Ground the LLM strictly in retrieved context — no hallucination
2. Force explicit "I don't know" when context is insufficient
3. Require conflict flagging when sources disagree
4. Require source citation so answers are auditable
5. Enforce ACH domain framing — operational decisions need precision

Why prompt design matters:
Perfect retrieval with a bad prompt still produces bad answers.
The prompt is the contract between the retrieval system and the LLM.
"""


SYSTEM_PROMPT = """You are an ACH payment operations expert assistant.
You help payment operations teams understand Nacha return codes, 
return windows, retry rules, and compliance requirements.

STRICT RULES:
1. Answer ONLY using the provided source chunks. Never use outside knowledge.
2. If the answer is not in the provided chunks, say exactly:
   "I don't have enough information in my sources to answer this accurately."
3. If sources CONTRADICT each other on a specific fact, explicitly flag it:
   "Note: Sources disagree on this point — [source A says X, source B says Y].
    Verify against your Nacha operating rules before making operational decisions."
4. Always cite which source you used at the end of your answer.
5. For retry decisions especially — be precise. Wrong retry advice 
   can cause Nacha violations and financial penalties.
6. Keep answers concise and operational — these users make real-time 
   payment decisions."""


def build_query_prompt(
    question: str,
    chunks: list[dict],
) -> str:
    """
    Builds the user-turn prompt with retrieved chunks injected.

    Each chunk includes its source document so the LLM can cite it
    and detect when two sources say different things about the same fact.

    Args:
        question: The user's raw question
        chunks:   List of dicts with 'content' and 'source_doc' keys
    """
    # Format each chunk with its source label
    # Numbered so the LLM can reference them in citations
    context_blocks = []
    for i, chunk in enumerate(chunks, 1):
        source = chunk.get("source_doc", "unknown").replace("_", " ").replace(".txt", "")
        context_blocks.append(
            f"[Source {i} — {source}]\n{chunk['content']}"
        )

    context = "\n\n---\n\n".join(context_blocks)

    return f"""Here are the relevant sources for your question:

{context}

---

Question: {question}

Answer based strictly on the sources above. 
If sources contradict each other, flag it explicitly.
Cite which source(s) you used at the end."""


def build_no_results_prompt(question: str) -> str:
    """
    Used when retriever returns empty results or all below threshold.
    Tells the LLM why we have no context rather than leaving it to guess.
    """
    return f"""No relevant sources were found for this question.

Question: {question}

Respond with: "I don't have enough information in my sources to answer 
this accurately. This may be because the topic falls outside ACH return 
code documentation, or the specific code or scenario you're asking about 
isn't covered in my current knowledge base."

Do not attempt to answer from general knowledge."""