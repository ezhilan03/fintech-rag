# data/synthetic_nacha.py
"""
Synthetic Nacha return code reference document for testing.
Mirrors the structure of real Nacha operating rules documentation.
"""

NACHA_TEXT = """
ACH Return Codes — Nacha Operating Rules Reference

Section 3 — Return Timeframes and Rules

The RDFI must return entries within the timeframes established by Nacha
operating rules. Standard entries must be returned within two banking days
of the settlement date. Unauthorized entries carry an extended return window
of 60 calendar days for consumer accounts.

An originator may re-initiate a returned entry no more than two times within
180 calendar days of the original settlement date. Unauthorized returns
(R05, R07, R10, R29) may not be re-initiated without new written authorization
from the receiver.

Return rate thresholds: Administrative returns (R02, R03, R04) must stay
below 3%. Unauthorized returns (R05, R07, R10, R29, R51) must stay below
0.5%. Overall returns must stay below 15%, calculated over the preceding
60 days.

R01 — Insufficient Funds
The available and/or cash reserve balance is not sufficient to cover the
dollar value of the debit entry. This return is appropriate when the account
exists and is open but does not have sufficient funds.
Return window: 2 banking days from settlement date.
Re-initiation: Allowed, maximum 2 times within 180 calendar days.
Best practice: Wait 3-5 business days before retrying to allow the account
holder time to replenish funds. Retrying the same day is almost always
ineffective and counts against your allowed attempts.

R02 — Account Closed
A previously active account has been closed by the customer or the RDFI.
The account number is valid but the account is no longer active.
Return window: 2 banking days from settlement date.
Re-initiation: Not allowed. Contact customer to obtain updated account
information before attempting another entry.

R03 — No Account / Unable to Locate Account
The account number does not correspond to an individual account held by
the RDFI. May result from a typo in the account number.
Return window: 2 banking days from settlement date.
Re-initiation: Not allowed without verified account information.

R04 — Invalid Account Number Structure
The account number does not pass a validity check. The format is incorrect.
Return window: 2 banking days from settlement date.
Re-initiation: Not allowed without corrected account information.

R07 — Authorization Revoked by Customer
The receiver has revoked the authorization previously given to the originator.
The debit was initiated after the customer had already revoked authorization.
Return window: 60 calendar days from settlement date.
Re-initiation: Not allowed. Obtain new written authorization before debiting.

R09 — Uncollected Funds
Funds exist in the account but have not yet been collected — for example
a recent deposit that has not yet cleared. Unlike R01, the funds exist
but are temporarily unavailable.
Return window: 2 banking days from settlement date.
Re-initiation: Allowed, maximum 2 times. Wait 2-3 business days for the
deposit to clear before retrying.

R10 — Customer Advises Originator Not Known or Not Authorized
The receiver does not recognize the originator or has not authorized the
originator to debit the account. Nacha narrowed R10's definition in 2020 —
it now specifically covers situations where no valid authorization exists.
Return window: 60 calendar days from settlement date.
Re-initiation: Not allowed. Pull authorization records immediately.
A Written Statement of Unauthorized Debit is required from the receiver.

R11 — Entry Not in Accordance with the Terms of Authorization
The entry was initiated in error — wrong amount, wrong date, or outside
the terms of the authorization. Unlike R10, a valid authorization exists
but this specific entry did not conform to its terms.
Return window: 60 calendar days from settlement date.
Re-initiation: Allowed once after correcting the error, without need for
new authorization from the receiver.

R29 — Corporate Customer Advises Not Authorized
Similar to R10 but applies to corporate accounts rather than consumer.
The corporate receiver advises the originator is not authorized to debit.
Return window: 2 banking days from settlement date — note this is shorter
than the 60-day consumer window for R10.
Re-initiation: Not allowed without new written authorization.
"""