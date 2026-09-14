import json
from types import SimpleNamespace as NS

import pytest
from eval.grounding import BudgetedMessages, BudgetExceeded, FIXTURES, evaluate, score


def test_replay_is_explicitly_not_model_quality(tmp_path):
    report = evaluate(output_dir=tmp_path)
    assert report['checks_passed'] and report['completed_cases'] == 8
    assert report['mode'] == 'harness_replay_NOT_model_quality'
    assert report['model'] is None and report['usage'] == []
    assert report['semantic_review'] == 'not_applicable'
    assert report['reserved_cost_usd'] == 0
    assert len(list(tmp_path.glob('*.json'))) == 1


def test_score_rejects_plausible_but_wrong_facts_and_missing_citations():
    case = json.loads(FIXTURES.read_text())['cases'][0]
    failures = score(case, 200, {'status':'answered', 'answer':'Two additional retries. [S1]', 'cited_source_ids':[]})
    assert any(f.startswith('missing_required_pattern') for f in failures)
    assert any(f.startswith('forbidden_pattern') for f in failures)
    assert 'missing_required_citations' in failures


def test_live_requires_credentials_before_call(monkeypatch, tmp_path):
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    with pytest.raises(ValueError, match='requires configured'):
        evaluate(live=True,output_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


class ProviderMessages:
    def __init__(self):
        self.generated = 0
        self.failed = False
    def count_tokens(self, **kwargs):
        return NS(input_tokens=2000)
    def create(self, **kwargs):
        self.generated += 1
        if self.failed:
            raise RuntimeError('uncertain network result')
        return NS(usage=NS(input_tokens=2000,output_tokens=100),model='existing-model')


def request():
    return dict(model='existing-model',system='test',messages=[],tools=[],max_tokens=1600)


def test_budget_rejects_generation_before_call():
    provider = ProviderMessages()
    bounded = BudgetedMessages(NS(messages=provider), 0.009)
    with pytest.raises(BudgetExceeded):
        bounded.create(**request())
    assert provider.generated == 0


def test_budget_keeps_reservation_after_uncertain_failure():
    provider = ProviderMessages()
    bounded = BudgetedMessages(NS(messages=provider), 0.015)
    provider.failed = True
    with pytest.raises(RuntimeError):
        bounded.create(**request())
    assert bounded.reserved_cost == pytest.approx(0.010)
    with pytest.raises(BudgetExceeded):
        bounded.create(**request())
    assert provider.generated == 1


def test_usage_is_recorded_separately_from_maximum_reservation():
    provider = ProviderMessages()
    bounded = BudgetedMessages(NS(messages=provider), 0.015)
    bounded.create(**request())
    assert bounded.usage[0]['estimated_cost_usd'] == pytest.approx(0.0025)
    assert bounded.reserved_cost == pytest.approx(0.010)


@pytest.mark.parametrize('budget', [0,-1,0.26,float('nan')])
def test_invalid_budgets_rejected(tmp_path, budget):
    with pytest.raises(ValueError):
        evaluate(max_cost=budget,output_dir=tmp_path)
