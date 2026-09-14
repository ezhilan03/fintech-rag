"""Fixed controlled-context evaluation through the real HTTP query handler.

Live mode calls the existing model once per nonempty case, without LLM judges.
Replay mode checks harness wiring only and MUST NOT be reported as model quality.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from eval.provenance import source_revision, dirty_worktree
from types import SimpleNamespace

import anthropic
from fastapi.testclient import TestClient
from src.api.app import app, MODEL, ABSTENTION
from src.retrieval.retriever import RetrievalResult

FIXTURES = Path(__file__).parent / 'fixtures/grounding.json'
# Standard first-party global Haiku 4.5 rates checked 2026-09-13.
# https://www.anthropic.com/claude/haiku — USD per million tokens, no caching.
INPUT_RATE, OUTPUT_RATE = 1.0, 5.0


class BudgetExceeded(RuntimeError):
    pass


class BudgetedMessages:
    def __init__(self, client, max_cost):
        self.client = client
        self.max_cost = max_cost
        self.reserved_cost = 0.0
        self.usage = []

    def create(self, **kwargs):
        # Count the exact prompt and tools before authorizing generation. Reserve
        # maximum output cost, including on uncertain/failed requests; never retry.
        counted = self.client.messages.count_tokens(**{
            k: kwargs[k] for k in ('model', 'system', 'messages', 'tools')})
        reserve = (counted.input_tokens*INPUT_RATE + kwargs['max_tokens']*OUTPUT_RATE)/1e6
        if self.reserved_cost + reserve > self.max_cost:
            raise BudgetExceeded('Configured evaluation budget would be exceeded')
        self.reserved_cost += reserve
        message = self.client.messages.create(**kwargs)
        usage = message.usage
        self.usage.append({'input_tokens': usage.input_tokens, 'output_tokens': usage.output_tokens,
                           'estimated_cost_usd': (usage.input_tokens*INPUT_RATE + usage.output_tokens*OUTPUT_RATE)/1e6,
                           'response_model': message.model})
        return message


class FixtureRetriever:
    def __init__(self):
        self.case = None

    def retrieve(self, question, **kwargs):
        return [RetrievalResult(
            chunk_id=i, content=text, source_doc=f'{self.case["id"]}-source-{i}.txt',
            return_code=None, return_category=None, return_window=None, can_retry=None,
            max_retries=None, vector_score=1.0, bm25_rank=i, rrf_score=1.0,
            version_id=None, source_sha256=hashlib.sha256(text.encode()).hexdigest(), chunk_index=0)
            for i,text in enumerate(self.case['evidence'], 1)]


class ReplayMessages:
    """Oracle-shaped outputs to test the harness, not an evaluated model."""
    def __init__(self, retriever):
        self.retriever = retriever

    def create(self, **kwargs):
        case = self.retriever.case
        if kwargs['tool_choice']['name'] == 'extract_comparison':
            return SimpleNamespace(stop_reason='tool_use', content=[SimpleNamespace(
                type='tool_use', name='extract_comparison', input={
                    'status':case['expected_status'], 'quotes':[
                        {'source_id':f'S{i}', 'quote':text}
                        for i,text in enumerate(case['evidence'],1)]})])
        return SimpleNamespace(stop_reason='tool_use', content=[SimpleNamespace(
            type='tool_use', name='submit_answer', input={
                'status': case['expected_status'], 'answer': case['example_answer'] or ABSTENTION,
                'cited_source_ids': case['required_citations']})])


def score(case, status_code, body):
    failures = []
    if status_code != 200:
        return ['http_' + str(status_code)]
    if body['status'] != case['expected_status']:
        failures.append('wrong_status')
    answer = body['answer']
    for pattern in case['required']:
        if not re.search(pattern, answer, re.I):
            failures.append('missing_required_pattern:' + pattern)
    for pattern in case['forbidden']:
        if re.search(pattern, answer, re.I):
            failures.append('forbidden_pattern:' + pattern)
    if not set(case['required_citations']) <= set(body['cited_source_ids']):
        failures.append('missing_required_citations')
    if case['expected_status'] == 'insufficient_evidence' and answer != ABSTENTION:
        failures.append('noncanonical_abstention')
    return failures


def evaluate(*, live=False, max_cost=0.15, output_dir=Path('eval/results')):
    if not 0 < max_cost <= 0.25:
        raise ValueError('Evaluation budget must be > 0 and <= $0.25')
    data = json.loads(FIXTURES.read_text())
    retriever = FixtureRetriever()
    provider = None
    if live:
        if not os.getenv('ANTHROPIC_API_KEY'):
            raise ValueError('Live evaluation requires configured ANTHROPIC_API_KEY')
        provider = anthropic.Anthropic(timeout=30, max_retries=0)
        messages = BudgetedMessages(provider, max_cost)
    else:
        messages = ReplayMessages(retriever)
    previous = {key: getattr(app.state, key, None) for key in ('retriever','client')}
    app.state.retriever = retriever
    app.state.client = SimpleNamespace(messages=messages)
    records = []
    try:
        # No lifespan: controlled contexts replace DB retrieval. The same route,
        # prompt, tool schema, provider SDK and citation validator run unchanged.
        client = TestClient(app)
        for case in data['cases']:
            retriever.case = case
            try:
                response = client.post('/query', json={'question':case['question'], 'top_k':5})
                body = response.json()
                failures = score(case, response.status_code, body)
                records.append({'id':case['id'], 'response':body, 'checks_passed':not failures, 'failures':failures})
                if response.status_code != 200:
                    break  # No repeated calls after an upstream/contract failure.
            except Exception as exc:
                records.append({'id':case['id'], 'checks_passed':False, 'failures':[type(exc).__name__]})
                break
    finally:
        for key,value in previous.items():
            setattr(app.state,key,value)
        if provider:
            provider.close()
        if 'client' in locals():
            client.close()
    report = {
        'mode':'live_model' if live else 'harness_replay_NOT_model_quality',
        'scope':data['scope'], 'model':MODEL if live else None,
        'created_at':datetime.now(timezone.utc).isoformat(),
        'fixture_sha256':hashlib.sha256(FIXTURES.read_bytes()).hexdigest(),
        'code_sha256':{name:hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in ['eval/grounding.py','src/api/app.py','src/api/prompt.py']},
        'commit':source_revision(),
        'working_tree_dirty':dirty_worktree(),
        'expected_cases':len(data['cases']), 'completed_cases':len(records),
        'checks_passed':len(records)==len(data['cases']) and all(r['checks_passed'] for r in records),
        'semantic_review':'pending' if live else 'not_applicable',
        'limitations':'Pattern checks detect selected regressions; passing is not proof of semantic faithfulness. Retrieval is controlled, not evaluated.',
        'cost_limit_usd':max_cost if live else 0,
        'reserved_cost_usd':messages.reserved_cost if live else 0,
        'usage':messages.usage if live else [], 'cases':records,
    }
    output_dir.mkdir(parents=True,exist_ok=True)
    target = output_dir / ('grounding_' + ('live_' if live else 'replay_') + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f') + '.json')
    target.write_text(json.dumps(report,indent=2))
    print(json.dumps({'report':str(target), 'mode':report['mode'], 'checks_passed':report['checks_passed']}))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--max-cost-usd',type=float,default=0.15)
    args = parser.parse_args()
    result = evaluate(live=args.live,max_cost=args.max_cost_usd)
    raise SystemExit(0 if result['checks_passed'] else 1)
