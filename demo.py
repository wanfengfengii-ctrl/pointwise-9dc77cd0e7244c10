import json
from pathlib import Path
from tempfile import TemporaryDirectory

from dataclasses import asdict, is_dataclass

from audit.attribution import enable_attribution
from audit.lookup import explain_delta, lookup
from audit.purge import purge
from audit.reconcile import reconcile_posting
from audit.workspace import initialize_workspace, open_workspace
from close.finalize import seal
from dispatch.submit import ReplyLost, advance
from intake.batches import apply
from meter_core import EvidenceError
from receiver.balances import balance
from receiver.receipts import receipt_for_request


def lose_reply(receipt):
    raise ReplyLost(f'reply for {receipt.request_id} lost after receiver commit')


def demonstrate(root, sample):
    day = sample['day']
    other_day = sample['other_day']
    initialize_workspace(root)
    with open_workspace(root) as work:
        apply(work.local, day, 'east-initial', 'initial', sample['initial'])
        first = seal(work.local, day, 'east-1')
        advance(work.local, work.external, day)
        apply(work.local, day, 'east-correction-1', 'correction', [sample['corrections'][0]])
        second = seal(work.local, day, 'east-2')
        lost = advance(work.local, work.external, day, after_post=lose_reply)
        pending = lookup(work.local, second.seal_id)
        received = balance(work.external, day)
        original = receipt_for_request(work.external, received.postings[-1].request_id)
        apply(work.local, day, 'east-correction-2', 'correction', [sample['corrections'][1]])
        third = seal(work.local, day, 'east-3')
        removed = purge(work.local, sample['cutoff']).removed

    with open_workspace(root) as work:
        attempts = [advance(work.local, work.external, day) for _ in range(2)]
        reviewed = [lookup(work.local, revision.seal_id) for revision in (first, second, third)]
        account = balance(work.external, day)
        expected = {
            'sealed': [5, 12, 9], 'pending_status': 'pending', 'pending_delta': None,
            'pending_external_balance': 12, 'attempt_order': ['east-2', 'east-3'],
            'confirmed_deltas': [5, 7, -3], 'same_original_receipt': True,
        }
        actual = {
            'sealed': [item.revision.total if item else None for item in reviewed],
            'pending_status': pending.state, 'pending_delta': pending.delta,
            'pending_external_balance': received.total,
            'attempt_order': [attempt.seal_id for attempt in attempts],
            'confirmed_deltas': [item.delta if item else None for item in reviewed],
            'same_original_receipt': original is not None and attempts[0].receipt == original,
        }
        downstream_expected = {'balance': 9, 'posting_deltas': [5, 7, -3], 'purged_batches': 3}
        downstream_actual = {
            'balance': account.total, 'posting_deltas': [entry.delta for entry in account.postings],
            'purged_batches': len(removed),
        }
        if actual != expected or downstream_actual != downstream_expected:
            raise AssertionError(
                f'DAILY_DELTA_MISMATCH actual={actual!r} downstream={downstream_actual!r} '
                f'expected={expected!r} expected_downstream={downstream_expected!r}'
            )
        if lost.state != 'pending' or not reconcile_posting(work.external, reviewed[-1]).balance_matches:
            raise AssertionError('pending transition or final reconciliation failed')

        unchanged = seal(work.local, day, 'east-4')
        zero = advance(work.local, work.external, day)
        apply(work.local, other_day, 'west-initial', 'initial', sample['other_initial'])
        seal(work.local, other_day, 'west-1')
        west = advance(work.local, work.external, other_day)
        east_account = balance(work.external, day)
        west_account = balance(work.external, other_day)
        secondary_actual = {
            'zero_seal': zero.seal_id, 'zero_delta': zero.receipt.delta,
            'distinct_zero_receipt': zero.receipt != attempts[-1].receipt,
            'last_total': unchanged.total, 'east_balance': east_account.total,
            'east_deltas': [posting.delta for posting in east_account.postings],
            'west_balance': west_account.total, 'west_delta': west.receipt.delta,
        }
        secondary_expected = {
            'zero_seal': 'east-4', 'zero_delta': 0, 'distinct_zero_receipt': True,
            'last_total': 9, 'east_balance': 9, 'east_deltas': [5, 7, -3, 0],
            'west_balance': 4, 'west_delta': 4,
        }
        if secondary_actual != secondary_expected:
            raise AssertionError(f'closing ledger actual={secondary_actual!r} expected={secondary_expected!r}')

    evidence = {
        'case_id': 'v5.lost_reply_with_later_revision', 'input': sample,
        'expected': expected, 'actual': actual,
        'downstream_expected': downstream_expected, 'downstream_actual': downstream_actual,
        'code_paths': [
            'intake/batches.py', 'close/finalize.py', 'dispatch/submit.py',
            'dispatch/confirm.py', 'receiver/requests.py', 'receiver/balances.py',
            'audit/lookup.py', 'audit/purge.py',
        ],
        'oracle': '2+3=5; 2+10=12; 2+7=9; adjacent differences are 5, 7, -3.',
    }
    print('POINTWISE_SCENARIO ' + json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    print(json.dumps(secondary_actual, ensure_ascii=False, sort_keys=True))


ATTRIBUTION_DAY = '2025-03-01'
ATTRIBUTION_CUTOFF = '2025-03-02'


def _json_default(value):
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f'cannot serialize {type(value)!r}')


def build_legacy_pair(work, peak):
    """first -> second with two offsetting corrections, all confirmed."""
    apply(work.local, ATTRIBUTION_DAY, 'boot', 'initial',
          [('东站', '甲', 2), ('东站', '乙', 3)])
    first = seal(work.local, ATTRIBUTION_DAY, 'first')
    advance(work.local, work.external, ATTRIBUTION_DAY)
    apply(work.local, ATTRIBUTION_DAY, 'c1', 'correction',
          [('东站', '乙', peak)])
    apply(work.local, ATTRIBUTION_DAY, 'c2', 'correction',
          [('东站', '乙', 3)])
    second = seal(work.local, ATTRIBUTION_DAY, 'second')
    receipt = advance(work.local, work.external, ATTRIBUTION_DAY).receipt
    return first, second, receipt


def demonstrate_attribution(root):
    # Case 1: complete legacy prefix at enable; purge originals, reopen, re-attribute.
    case_one = root / 'preserved'
    initialize_workspace(case_one)
    with open_workspace(case_one) as work:
        first, second, receipt = build_legacy_pair(work, 10)
        boundaries = enable_attribution(work.local)
        purge(work.local, ATTRIBUTION_CUTOFF)
        remaining = work.local.execute('SELECT COUNT(*) FROM source_rows').fetchone()[0]
    with open_workspace(case_one) as work:
        # The original receipt is still read from the V1 confirmation after reopen.
        review = lookup(work.local, second.seal_id)
        answer = explain_delta(work.local, first.seal_id, second.seal_id)
        preserved = {
            'case_id': 'attribution.preserved_after_purge_reopen',
            'enable_boundaries': [
                {'day': item.day, 'enable_sequence': item.enable_sequence}
                for item in boundaries
            ],
            'original_payload_rows_left': remaining,
            'original_receipt_read_after_reopen': review.receipt,
            'receipt_matches_v1_original': review.receipt == receipt,
            'receipt_delta': review.receipt.delta,
            'changes': answer['changes'],
        }
        if answer['state'] != 'confirmed' or answer['receipt'] != receipt:
            raise AssertionError(f'preserved attribution failed: {answer!r}')
        if remaining != 0:
            raise AssertionError('originals were not purged before reopen')
        expected_changes = [
            {'batch_id': 'c1', 'station': '东站', 'name': '乙', 'position': 1,
             'before': 3, 'after': 10, 'delta': 7},
            {'batch_id': 'c2', 'station': '东站', 'name': '乙', 'position': 1,
             'before': 10, 'after': 3, 'delta': -7},
        ]
        if answer['changes'] != expected_changes:
            raise AssertionError(
                f'ATTRIBUTION_MISMATCH actual={answer["changes"]!r} expected={expected_changes!r}'
            )
        print('POINTWISE_ATTRIBUTION ' + json.dumps(
            preserved, ensure_ascii=False, sort_keys=True, default=_json_default))

        # Case 2: same watermark must verify zero and return an empty change list,
        # even though the day's older legacy evidence is already missing.
        third = seal(work.local, ATTRIBUTION_DAY, 'third')
        zero_receipt = advance(work.local, work.external, ATTRIBUTION_DAY).receipt
        zero_answer = explain_delta(work.local, second.seal_id, third.seal_id)
        zero_payload = {
            'case_id': 'attribution.same_watermark_zero',
            'original_receipt_read_after_reopen': zero_receipt,
            'changes': zero_answer['changes'],
        }
        if zero_answer['changes'] != [] or zero_answer['receipt'] != zero_receipt:
            raise AssertionError(f'zero-watermark attribution failed: {zero_answer!r}')
        print('POINTWISE_ATTRIBUTION ' + json.dumps(
            zero_payload, ensure_ascii=False, sort_keys=True, default=_json_default))

    # Case 3: legacy prefix already expired at first enable -> the old interval
    # is rejected as missing evidence; the post-enable interval still works.
    case_three = root / 'expired'
    initialize_workspace(case_three)
    with open_workspace(case_three) as work:
        first, second, _ = build_legacy_pair(work, 10)
        purge(work.local, ATTRIBUTION_CUTOFF)
        enable_attribution(work.local)
        apply(work.local, ATTRIBUTION_DAY, 'c3', 'correction',
              [('东站', '乙', 4)])
        third = seal(work.local, ATTRIBUTION_DAY, 'third')
        third_receipt = advance(work.local, work.external, ATTRIBUTION_DAY).receipt
    with open_workspace(case_three) as work:
        try:
            explain_delta(work.local, first.seal_id, second.seal_id)
        except EvidenceError as missing:
            missing_payload = {
                'case_id': 'attribution.legacy_evidence_missing',
                'rejected_pair': [first.seal_id, second.seal_id],
                'reason': str(missing),
            }
        else:
            raise AssertionError('missing-evidence interval must be rejected')
        print('POINTWISE_ATTRIBUTION ' + json.dumps(
            missing_payload, ensure_ascii=False, sort_keys=True))
        later = explain_delta(work.local, second.seal_id, third.seal_id)
        later_payload = {
            'case_id': 'attribution.post_enable_interval',
            'original_receipt_read_after_reopen': later['receipt'],
            'receipt_matches_v1_original': later['receipt'] == third_receipt,
            'changes': later['changes'],
        }
        if later['changes'] != [
            {'batch_id': 'c3', 'station': '东站', 'name': '乙', 'position': 1,
             'before': 3, 'after': 4, 'delta': 1},
        ]:
            raise AssertionError(f'post-enable attribution failed: {later["changes"]!r}')
        print('POINTWISE_ATTRIBUTION ' + json.dumps(
            later_payload, ensure_ascii=False, sort_keys=True, default=_json_default))


def main():
    sample_path = Path(__file__).resolve().parent / 'samples' / 'shift.json'
    sample = json.loads(sample_path.read_text(encoding='utf-8'))
    with TemporaryDirectory(prefix='estuary-demo-') as root:
        demonstrate(root, sample)
        demonstrate_attribution(Path(root))


if __name__ == '__main__':
    main()
