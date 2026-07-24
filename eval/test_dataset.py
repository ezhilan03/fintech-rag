# eval/test_dataset.py
"""
Ground truth test dataset for RAGAS evaluation.

These questions represent real operational scenarios from ACH payment processing.
Ground truth answers are written by a domain expert (EZ) based on Nacha rules.

Dataset design principles:
  - Cover different query types: exact code, semantic, comparison, threshold
  - Include edge cases: non-existent code, ambiguous scenarios
  - Ground truth answers are concise and factually precise
  - ~15 questions — enough for meaningful scores, not so many it takes forever
"""

TEST_CASES = [
    # ── Retry eligibility (most operationally critical) ───────────────────
    {
        "question": "Can I retry an R01 return?",
        "reference": (
            "Yes. R01 (Insufficient Funds) can be retried up to 2 additional "
            "times within 180 calendar days of the original settlement date. "
            "Wait a few days before retrying to allow the account holder time "
            "to replenish funds."
        ),
    },
    {
        "question": "Can I retry an R29 return?",
        "reference": (
            "No. R29 (Corporate Customer Advises Not Authorized) cannot be "
            "retried without new written authorization from the receiver. "
            "Retrying without new authorization is a Nacha violation."
        ),
    },
    {
        "question": "Can I retry an R10 return?",
        "reference": (
            "No. R10 (Customer Advises Originator Not Known or Not Authorized) "
            "cannot be retried without resolving the authorization issue and "
            "obtaining a new written authorization. "
            "R10 carries a 60 calendar day return window."
        ),
    },
    {
        "question": "Can I retry an R09 return?",
        "reference": (
            "Yes. R09 (Uncollected Funds) can be retried. "
            "Wait 2-3 business days for the pending deposit to clear before "
            "retrying. Maximum 2 additional attempts."
        ),
    },

    # ── Return windows ────────────────────────────────────────────────────
    {
        "question": "How long does a bank have to return an R10?",
        "reference": (
            "The RDFI has 60 calendar days from the settlement date to return "
            "an R10 entry. This extended window applies because R10 is an "
            "unauthorized return code."
        ),
    },
    {
        "question": "What is the return window for R02?",
        "reference": (
            "R02 (Account Closed) has a 2 banking day return window from the "
            "settlement date. Do not retry — contact the customer for updated "
            "account information."
        ),
    },
    {
        "question": "What is the difference between the return window for R01 and R07?",
        "reference": (
            "R01 (Insufficient Funds) has a 2 banking day return window. "
            "R07 (Authorization Revoked) has a 60 calendar day return window "
            "because it is an unauthorized return code. "
            "Unauthorized codes have extended windows to allow consumers time "
            "to identify and dispute unauthorized transactions."
        ),
    },

    # ── Semantic queries (no exact code) ──────────────────────────────────
    {
        "question": "What happens when a customer says they never authorized a payment?",
        "reference": (
            "When a consumer claims they never authorized a payment, the RDFI "
            "returns the entry with R10 (Customer Advises Originator Not Known "
            "or Not Authorized). The originator must not retry without obtaining "
            "new written authorization. A Written Statement of Unauthorized "
            "Debit is required. The return window is 60 calendar days."
        ),
    },
    {
        "question": "What return code is used when a corporate account says a debit was not authorized?",
        "reference": (
            "R29 (Corporate Customer Advises Not Authorized) is used when a "
            "corporate account holder notifies their bank that an ACH debit "
            "was not authorized. R29 has a 2 banking day return window, "
            "shorter than the 60-day window for consumer unauthorized codes."
        ),
    },
    {
        "question": "What should I do when I receive an R02?",
        "reference": (
            "When you receive an R02 (Account Closed), do not retry the payment. "
            "The account has been permanently closed. Contact the customer to "
            "obtain new valid bank account information before attempting "
            "another transaction."
        ),
    },

    # ── Nacha thresholds ──────────────────────────────────────────────────
    {
        "question": "What are Nacha's return rate thresholds?",
        "reference": (
            "Nacha enforces three return rate thresholds: "
            "Overall returns must stay below 15%. "
            "Administrative returns (R02, R03, R04) must stay below 3%. "
            "Unauthorized returns (R05, R07, R10, R29, R51) must stay below 0.5%. "
            "All thresholds are calculated over the preceding 60 days."
        ),
    },
    {
        "question": "What is the unauthorized return rate threshold?",
        "reference": (
            "Unauthorized returns must stay below 0.5%. "
            "This threshold covers return codes R05, R07, R10, R29, and R51. "
            "Exceeding this threshold triggers Nacha enforcement actions "
            "which can include fines and suspension of ACH origination privileges."
        ),
    },

    # ── Edge cases ────────────────────────────────────────────────────────
    {
        "question": "What is the difference between R07 and R10?",
        "reference": (
            "R07 (Authorization Revoked) means the customer previously gave "
            "authorization but later revoked it — typically for a recurring "
            "payment they cancelled. "
            "R10 (Not Authorized) means the customer says they never authorized "
            "the transaction in the first place, or the originator is not known. "
            "Both carry a 60 calendar day return window and cannot be retried "
            "without new written authorization."
        ),
    },
    {
        "question": "What does R11 mean and when was it updated?",
        "reference": (
            "R11 (Entry Not In Accordance with Terms of Authorization) means "
            "the payment was initiated in error — wrong amount, wrong date, or "
            "outside the terms of an existing authorization. Unlike R10, a valid "
            "authorization exists but this specific entry did not conform to it. "
            "Nacha repurposed R11 to specifically cover this scenario. "
            "R11 can be retried once after correcting the error, without "
            "needing new authorization. Return window is 60 calendar days."
        ),
    },
    {
        "question": "What does R99 mean?",
        "reference": (
            "R99 is not a valid Nacha ACH return code. "
            "Valid return codes range from R01 to R85. "
            "If you received an R99, verify the return code with your "
            "payment processor as it may be a proprietary code or a data error."
        ),
    },
]