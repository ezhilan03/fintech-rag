# eval/evaluator.py
"""
Legacy RAGAS exploration harness; not the current API release gate.

The current fixed grounding check is python -m eval.grounding. This legacy
script uses the older free-text prompt and makes extra paid judging calls.

Compares two chunking strategies:
  clause    — one chunk per return code entry (production)
  recursive — fixed-size character splitting (baseline)

Metrics measured:
  faithfulness:      is the answer supported by retrieved chunks?
  answer_relevancy:  does the answer address the question?
  context_precision: are retrieved chunks ranked by relevance?
  context_recall:    did retrieval find everything needed?

Output:
  - Score table per strategy
  - Per-question breakdown
  - Saved to eval/results_{strategy}_{timestamp}.json
"""
from __future__ import annotations
import os
import json
import time
import asyncio
from datetime import datetime
from dotenv import load_dotenv
import anthropic as anthropic_sdk

from ragas import SingleTurnSample
from ragas.metrics import faithfulness, context_precision, context_recall
from langchain_anthropic import ChatAnthropic
from ragas.llms import LangchainLLMWrapper
from ragas.llms import llm_factory
from src.retrieval.retriever import HybridRetriever
from src.ingestion.embedder import Embedder
from eval.test_dataset import TEST_CASES

load_dotenv()


# ── LLM setup ─────────────────────────────────────────────────────────────────

def setup_ragas_metrics():
    """
    Sets up the old ragas.metrics singletons which have single_turn_ascore().
    The new ragas.metrics.collections API doesn't support single_turn_ascore()
    and requires evaluate() which has incompatible metric type requirements.
    The old singleton API is deprecated but functional in 0.4.x.
    """
    llm = ChatAnthropic(
        model="claude-haiku-4-5",
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
    )
    wrapper = LangchainLLMWrapper(llm)

    # Set llm on each singleton via property — not constructor argument
    faithfulness.llm      = wrapper
    context_precision.llm = wrapper
    context_recall.llm    = wrapper

    return {
        "faithfulness":      faithfulness,
        "context_precision": context_precision,
        "context_recall":    context_recall,
    }


# ── Answer generation ─────────────────────────────────────────────────────────

def generate_answer(
    question: str,
    contexts: list[str],
    anthropic_client: anthropic_sdk.Anthropic,
) -> str:
    """
    Generates an answer using Claude with retrieved contexts.
    Same prompt structure as the API — consistent evaluation.
    """
    from src.api.prompt import build_query_prompt, build_no_results_prompt, SYSTEM_PROMPT

    if not contexts:
        user_prompt = build_no_results_prompt(question)
    else:
        chunks_for_prompt = [
            {"content": c, "source_doc": "retrieved"}
            for c in contexts
        ]
        user_prompt = build_query_prompt(question, chunks_for_prompt)

    message = anthropic_client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=512,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}]
    )
    return message.content[0].text


# ── Evaluation pipeline ───────────────────────────────────────────────────────

def run_evaluation(strategy: str = "clause") -> dict:
    """
    Runs full RAGAS evaluation for one chunking strategy.

    Steps:
      1. Load retriever for the strategy
      2. For each test case: retrieve chunks, generate answer
      3. Build RAGAS EvaluationDataset
      4. Score with 4 metrics
      5. Return results dict

    Args:
        strategy: "clause" or "recursive"

    Returns:
        dict with scores, per-question breakdown, and metadata
    """
    print(f"\n{'='*60}")
    print(f"RAGAS EVALUATION — strategy: {strategy}")
    print(f"{'='*60}")
    print(f"Test cases: {len(TEST_CASES)}")

    # Initialize components
    embedder = Embedder()
    retriever = HybridRetriever(embedder=embedder, strategy=strategy)
    anthropic_client = anthropic_sdk.Anthropic(
        api_key=os.getenv("ANTHROPIC_API_KEY")
    )
    ragas_llm = setup_ragas_metrics()

    # Build evaluation dataset
    samples = []
    per_question = []

    for i, case in enumerate(TEST_CASES):
        question = case["question"]
        reference = case["reference"]

        print(f"\n[{i+1}/{len(TEST_CASES)}] {question[:60]}...")

        # Retrieve
        results = retriever.retrieve(question)
        contexts = [r.content for r in results]
        return_codes_found = [r.return_code for r in results if r.return_code]

        print(f"  Retrieved: {len(contexts)} chunks "
              f"| codes: {return_codes_found[:3]}")

        # Generate answer
        answer = generate_answer(question, contexts, anthropic_client)
        print(f"  Answer preview: {answer[:80].strip()}...")

        # Add to RAGAS dataset
        samples.append(SingleTurnSample(
            user_input=question,
            retrieved_contexts=contexts,
            response=answer,
            reference=reference,
        ))

        # Store for per-question analysis
        per_question.append({
            "question":      question,
            "answer":        answer,
            "reference":     reference,
            "contexts_found": len(contexts),
            "codes_found":   return_codes_found,
        })

        # Small delay to avoid API rate limits
        time.sleep(0.5)

    # Run RAGAS scoring using old singleton API
    # ragas.metrics singletons have single_turn_ascore() — collections do not
    print(f"\nScoring with RAGAS metrics...")

    metrics = setup_ragas_metrics()
    all_scores = {key: [] for key in metrics}

    for i, sample in enumerate(samples):
        print(f"  Scoring [{i+1}/{len(samples)}] {sample.user_input[:50]}...")
        for metric_name, metric in metrics.items():
            try:
                score = asyncio.run(metric.single_turn_ascore(sample))
                all_scores[metric_name].append(score)
                per_question[i].setdefault("scores", {})[metric_name] = score
            except Exception as e:
                print(f"    Warning: {metric_name} failed — {e}")
                all_scores[metric_name].append(None)
                per_question[i].setdefault("scores", {})[metric_name] = None

    # Average scores — skip None values
    scores = {}
    for key, vals in all_scores.items():
        valid = [v for v in vals if v is not None]
        scores[key] = float(sum(valid) / len(valid)) if valid else 0.0

    output = {
        "strategy":     strategy,
        "timestamp":    datetime.now().isoformat(),
        "n_questions":  len(TEST_CASES),
        "scores":       scores,
        "per_question": per_question,
    }

    # Save results
    os.makedirs("eval/results", exist_ok=True)
    filename = f"eval/results/{strategy}_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    with open(filename, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nResults saved → {filename}")

    return output


def print_comparison(clause_results: dict, recursive_results: dict):
    """
    Prints a side-by-side comparison table of both strategies.
    This is the table that goes in your portfolio README.
    """
    print(f"\n{'='*60}")
    print("STRATEGY COMPARISON")
    print(f"{'='*60}")
    print(f"{'Metric':<25} {'Clause':>10} {'Recursive':>10} {'Winner':>10}")
    print("-" * 60)

    metrics = [
        ("Faithfulness",      "faithfulness"),
        ("Context Precision", "context_precision"),
        ("Context Recall",    "context_recall"),
    ]

    for label, key in metrics:
        c_score = clause_results["scores"][key]
        r_score = recursive_results["scores"][key]
        winner = "clause" if c_score >= r_score else "recursive"
        print(f"{label:<25} {c_score:>10.3f} {r_score:>10.3f} {winner:>10}")

    print("-" * 60)

    # Overall average
    c_avg = sum(clause_results["scores"].values()) / 3
    r_avg = sum(recursive_results["scores"].values()) / 3
    winner = "clause" if c_avg >= r_avg else "recursive"
    print(f"{'Average':<25} {c_avg:>10.3f} {r_avg:>10.3f} {winner:>10}")


if __name__ == "__main__":
    # Run clause first — our production strategy
    clause_results = run_evaluation(strategy="clause")

    print(f"\n{'='*60}")
    print("Clause evaluation complete. Starting recursive...")
    print("Make sure recursive chunks are ingested first.")
    print(f"{'='*60}")

    # Run recursive — baseline for comparison
    recursive_results = run_evaluation(strategy="recursive")

    # Print comparison
    print_comparison(clause_results, recursive_results)