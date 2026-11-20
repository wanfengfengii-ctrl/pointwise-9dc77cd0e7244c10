import sqlite3

from audit.attribution import (
    AttributionBoundary,
    enable_attribution,
    is_enabled,
)
from audit.lookup import explain_delta
from audit.purge import purge
from audit.workspace import initialize_workspace, open_workspace
from close.finalize import seal
from dispatch.submit import advance
from intake.batches import apply
from meter_core import EvidenceError, InputError, encode_litres
from tests.support import DAY, NEXT, WorkspaceTestCase, rejected_commit


def confirm_day(work, day):
    while True:
        result = advance(work.local, work.external, day)
        if result.state == 'idle':
            return


def legacy_ledgers(work, peak):
    """The prepare_legacy scenario from ATTRIBUTION.md / attribution_examples.py."""
    apply(work.local, DAY, 'boot', 'initial', [('east', 'a', 2), ('east', 'b', 3)])
    first = seal(work.local, DAY, 'first')
    advance(work.local, work.external, DAY)
    apply(work.local, DAY, 'c1', 'correction', [('east', 'b', peak)])
    apply(work.local, DAY, 'c2', 'correction', [('east', 'b', 3)])
    second = seal(work.local, DAY, 'second')
    receipt = advance(work.local, work.external, DAY).receipt
    return first, second, receipt


def simplified_changes(changes):
    return [(item['batch_id'], item['station'], item['name'], item['position'],
             item['before'], item['after'], item['delta']) for item in changes]


class AttributionTests(WorkspaceTestCase):
    def test_legacy_prefix_survives_purge_and_reopen(self):
        for peak, deltas in ((10, (7, -7)), (8, (5, -5))):
            with self.subTest(peak=peak):
                root = self.root / f'peak-{peak}'
                with self.open_at(root) as work:
                    first, second, receipt = legacy_ledgers(work, peak)
                    boundary = enable_attribution(work.local)
                    self.assertEqual(boundary, (AttributionBoundary(DAY, 3),))
                    # Enabling again keeps the original boundary.
                    self.assertEqual(enable_attribution(work.local), boundary)
                    removed = purge(work.local, NEXT).removed
                    self.assertEqual([item.batch_id for item in removed], ['boot', 'c1', 'c2'])
                with self.open_at(root) as work:
                    answer = explain_delta(work.local, first.seal_id, second.seal_id)
                    self.assertEqual(answer['state'], 'confirmed')
                    self.assertEqual(answer['receipt'], receipt)
                    self.assertEqual(answer['receipt'].delta, 0)
                    self.assertEqual(simplified_changes(answer['changes']), [
                        ('c1', 'east', 'b', 1, 3, peak, deltas[0]),
                        ('c2', 'east', 'b', 1, peak, 3, deltas[1]),
                    ])

    def open_at(self, root):
        initialize_workspace(root)
        return open_workspace(root)

    def test_same_value_only_legacy_interval_is_empty(self):
        with self.open() as work:
            first, second, receipt = legacy_ledgers(work, 3)
            enable_attribution(work.local)
            purge(work.local, NEXT)
        with self.open() as work:
            answer = explain_delta(work.local, 'first', 'second')
            self.assertEqual(answer['changes'], [])
            self.assertEqual(answer['receipt'], receipt)

    def test_legacy_prefix_already_purged_reports_missing(self):
        with self.open() as work:
            first, second, _ = legacy_ledgers(work, 10)
            purge(work.local, NEXT)
            boundary = enable_attribution(work.local)
            self.assertEqual(boundary, (AttributionBoundary(DAY, 3),))
            with self.assertRaises(EvidenceError) as missing:
                explain_delta(work.local, 'first', 'second')
            self.assertIn('missing', str(missing.exception))
            # The post-enable interval starting at the enable watermark still works.
            apply(work.local, DAY, 'c3', 'correction', [('east', 'b', 4)])
            third = seal(work.local, DAY, 'third')
            receipt = advance(work.local, work.external, DAY).receipt
        with self.open() as work:
            answer = explain_delta(work.local, 'second', 'third')
            self.assertEqual(answer['receipt'], receipt)
            self.assertEqual(simplified_changes(answer['changes']), [
                ('c3', 'east', 'b', 1, 3, 4, 1),
            ])
            with self.assertRaises(EvidenceError):
                explain_delta(work.local, 'first', 'second')

    def test_not_enabled_nonempty_interval_reports_missing(self):
        with self.open() as work:
            first, second, _ = legacy_ledgers(work, 10)
            self.assertFalse(is_enabled(work.local))
            with self.assertRaises(EvidenceError) as missing:
                explain_delta(work.local, 'first', 'second')
            self.assertIn('missing', str(missing.exception))

    def test_same_watermark_returns_empty_even_without_enable_or_other_missing(self):
        with self.open() as work:
            first, second, _ = legacy_ledgers(work, 10)
            third = seal(work.local, DAY, 'third')
            advance(work.local, work.external, DAY)
            # No enable anywhere; same-watermark pair still verifies the zero receipt.
            answer = explain_delta(work.local, second.seal_id, third.seal_id)
            self.assertEqual(answer['state'], 'confirmed')
            self.assertEqual(answer['changes'], [])
            self.assertEqual(answer['receipt'].delta, 0)
            # Another day whose legacy evidence is missing must not affect this.
            purge(work.local, NEXT)
            enable_attribution(work.local)
            answer = explain_delta(work.local, second.seal_id, third.seal_id)
            self.assertEqual(answer['changes'], [])

    def test_same_watermark_with_nonzero_receipt_is_mismatch(self):
        with self.open() as work:
            first, second, _ = legacy_ledgers(work, 10)
            third = seal(work.local, DAY, 'third')
            advance(work.local, work.external, DAY)
            # Tamper with the stored receipt delta to simulate inconsistent evidence.
            work.local.execute(
                'UPDATE confirmations SET delta = ? WHERE seal_id = ?',
                (encode_litres(9), third.seal_id),
            )
            with self.assertRaises(EvidenceError) as mismatch:
                explain_delta(work.local, second.seal_id, third.seal_id)
            self.assertIn('not zero', str(mismatch.exception))

    def test_pending_later_seal_returns_pending_payload(self):
        with self.open() as work:
            apply(work.local, DAY, 'boot', 'initial', [('east', 'a', 2), ('east', 'b', 3)])
            first = seal(work.local, DAY, 'first')
            advance(work.local, work.external, DAY)
            enable_attribution(work.local)
            apply(work.local, DAY, 'c1', 'correction', [('east', 'b', 10)])
            second = seal(work.local, DAY, 'second')
            self.assertEqual(
                explain_delta(work.local, 'first', 'second'),
                {'state': 'pending', 'receipt': None, 'changes': None},
            )
            receipt = advance(work.local, work.external, DAY).receipt
            answer = explain_delta(work.local, 'first', 'second')
            self.assertEqual(answer['state'], 'confirmed')
            self.assertEqual(answer['receipt'], receipt)

    def test_first_recording_zero_litres_is_listed(self):
        with self.open() as work:
            first = seal(work.local, DAY, 'first')
            advance(work.local, work.external, DAY)
            enable_attribution(work.local)
            apply(work.local, DAY, 'zero-row', 'initial', [
                ('east', 'empty', 0), ('east', 'full', 5),
            ])
            second = seal(work.local, DAY, 'second')
            advance(work.local, work.external, DAY)
            answer = explain_delta(work.local, first.seal_id, second.seal_id)
            self.assertEqual(simplified_changes(answer['changes']), [
                ('zero-row', 'east', 'empty', 1, None, 0, 0),
                ('zero-row', 'east', 'full', 2, None, 5, 5),
            ])

    def test_same_value_errata_omitted_positions_preserved(self):
        with self.open() as work:
            apply(work.local, DAY, 'boot', 'initial', [('east', 'a', 2), ('east', 'b', 3)])
            first = seal(work.local, DAY, 'first')
            advance(work.local, work.external, DAY)
            enable_attribution(work.local)
            apply(work.local, DAY, 'mix', 'correction', [
                ('east', 'a', 2), ('east', 'b', 10),
            ])
            second = seal(work.local, DAY, 'second')
            advance(work.local, work.external, DAY)
            answer = explain_delta(work.local, first.seal_id, second.seal_id)
            self.assertEqual(simplified_changes(answer['changes']), [
                ('mix', 'east', 'b', 2, 3, 10, 7),
            ])

    def test_ordering_follows_batch_sequence_then_row_position(self):
        with self.open() as work:
            apply(work.local, DAY, 'boot', 'initial', [('east', 'a', 1), ('east', 'b', 1)])
            first = seal(work.local, DAY, 'first')
            advance(work.local, work.external, DAY)
            enable_attribution(work.local)
            apply(work.local, DAY, 'p1', 'correction', [('east', 'a', 4), ('east', 'b', 5)])
            apply(work.local, DAY, 'p2', 'correction', [('east', 'b', 2)])
            second = seal(work.local, DAY, 'second')
            advance(work.local, work.external, DAY)
            answer = explain_delta(work.local, first.seal_id, second.seal_id)
            self.assertEqual(simplified_changes(answer['changes']), [
                ('p1', 'east', 'a', 1, 1, 4, 3),
                ('p1', 'east', 'b', 2, 1, 5, 4),
                ('p2', 'east', 'b', 1, 5, 2, -3),
            ])
            self.assertEqual(sum(item['delta'] for item in answer['changes']), 4)

    def test_tally_mismatch_is_distinct_from_missing(self):
        with self.open() as work:
            first, second, _ = legacy_ledgers(work, 10)
            enable_attribution(work.local)
            work.local.execute(
                'UPDATE attribution_changes SET after_value = ? WHERE batch_id = ? AND position = 1',
                (encode_litres(11), 'c1'),
            )
            with self.assertRaises(EvidenceError) as mismatch:
                explain_delta(work.local, 'first', 'second')
            self.assertIn('does not match', str(mismatch.exception))
            self.assertNotIn('missing', str(mismatch.exception))

    def test_invalid_seal_pairs(self):
        with self.open() as work:
            first, second, _ = legacy_ledgers(work, 10)
            third = seal(work.local, DAY, 'third')
            apply(work.local, NEXT, 'other', 'initial', [('west', 'a', 1)])
            other = seal(work.local, NEXT, 'other-seal')
            cases = [
                ('unknown-a', 'second', 'unknown seal id'),
                ('first', 'unknown-b', 'unknown seal id'),
                ('first', other.seal_id, 'same day'),
                (second.seal_id, first.seal_id, 'follow'),
                ('first', 'first', 'distinct'),
                ('first', third.seal_id, 'adjacent'),
            ]
            for earlier, later, fragment in cases:
                with self.subTest(pair=(earlier, later)):
                    with self.assertRaises(InputError) as error:
                        explain_delta(work.local, earlier, later)
                    self.assertIn(fragment, str(error.exception))
            with self.assertRaises(InputError):
                explain_delta(work.local, '  ', 'second')

    def test_enable_failure_leaves_no_evidence(self):
        with self.open() as work:
            legacy_ledgers(work, 10)
            with rejected_commit(work.local), self.assertRaises(sqlite3.DatabaseError):
                enable_attribution(work.local)
            self.assertFalse(is_enabled(work.local))
            self.assertEqual(
                work.local.execute('SELECT COUNT(*) FROM attribution_state').fetchone(), (0,),
            )
            self.assertEqual(
                work.local.execute('SELECT COUNT(*) FROM attribution_batches').fetchone(), (0,),
            )
            self.assertEqual(
                work.local.execute('SELECT COUNT(*) FROM attribution_boundaries').fetchone(), (0,),
            )
            # A later successful enable still takes the first successful boundary.
            boundary = enable_attribution(work.local)
            self.assertEqual(boundary, (AttributionBoundary(DAY, 3),))
            self.assertTrue(is_enabled(work.local))

    def test_apply_failure_leaves_no_partial_evidence(self):
        with self.open() as work:
            legacy_ledgers(work, 10)
            enable_attribution(work.local)
            with rejected_commit(work.local), self.assertRaises(sqlite3.DatabaseError):
                apply(work.local, DAY, 'broken', 'correction', [('east', 'b', 4)])
            self.assertEqual(
                work.local.execute(
                    "SELECT COUNT(*) FROM attribution_batches WHERE batch_id = 'broken'",
                ).fetchone(),
                (0,),
            )
            self.assertIsNone(
                work.local.execute(
                    "SELECT batch_id FROM batches WHERE batch_id = 'broken'",
                ).fetchone(),
            )
            # Retry after the failed commit succeeds and preserves evidence once.
            result = apply(work.local, DAY, 'broken', 'correction', [('east', 'b', 4)])
            self.assertEqual(result.changed, 1)
            self.assertEqual(
                work.local.execute(
                    "SELECT COUNT(*) FROM attribution_batches WHERE batch_id = 'broken'",
                ).fetchone(),
                (1,),
            )

    def test_purge_failure_keeps_evidence_and_originals(self):
        with self.open() as work:
            first, second, _ = legacy_ledgers(work, 10)
            enable_attribution(work.local)
            with rejected_commit(work.local), self.assertRaises(sqlite3.DatabaseError):
                purge(work.local, NEXT)
            self.assertEqual(
                work.local.execute('SELECT COUNT(*) FROM source_rows').fetchone(), (4,),
            )
            self.assertEqual(
                work.local.execute('SELECT COUNT(*) FROM attribution_changes').fetchone(), (4,),
            )
            purge(work.local, NEXT)
        with self.open() as work:
            answer = explain_delta(work.local, first.seal_id, second.seal_id)
            self.assertEqual(len(answer['changes']), 2)

    def test_identical_replay_adds_no_evidence(self):
        with self.open() as work:
            legacy_ledgers(work, 10)
            enable_attribution(work.local)
            rows = [('east', 'b', 10)]
            original = apply(work.local, DAY, 'c1', 'correction', rows)
            self.assertEqual(apply(work.local, DAY, 'c1', 'correction', rows), original)
            self.assertEqual(
                work.local.execute(
                    'SELECT COUNT(*) FROM attribution_batches WHERE batch_id = ?', ('c1',),
                ).fetchone(),
                (1,),
            )

    def test_multi_day_enable_boundaries(self):
        with self.open() as work:
            apply(work.local, DAY, 'day-one', 'initial', [('east', 'a', 2)])
            seal(work.local, DAY, 'one-seal')
            apply(work.local, NEXT, 'day-two', 'initial', [('west', 'a', 4), ('west', 'b', 6)])
            apply(work.local, NEXT, 'day-two-fix', 'correction', [('west', 'b', 7)])
            seal(work.local, NEXT, 'two-seal')
            self.assertEqual(
                enable_attribution(work.local),
                (AttributionBoundary(DAY, 1), AttributionBoundary(NEXT, 2)),
            )
            purge(work.local, '2025-03-03')
        with self.open() as work:
            evidence = work.local.execute(
                'SELECT batch_id, position, station, name, before_value, after_value '
                'FROM attribution_changes ORDER BY batch_id, position',
            ).fetchall()
            self.assertEqual([row[0] for row in evidence],
                             ['day-one', 'day-two', 'day-two', 'day-two-fix'])
            self.assertIsNone(evidence[1][4])  # first recording before is NULL

    def test_gap_in_legacy_prefix_limits_replayable_segment(self):
        with self.open() as work:
            apply(work.local, DAY, 'b1', 'initial', [('east', 'a', 1)])
            first = seal(work.local, DAY, 'first')
            advance(work.local, work.external, DAY)
            apply(work.local, DAY, 'b2', 'correction', [('east', 'a', 2)])
            apply(work.local, DAY, 'b3', 'correction', [('east', 'a', 3)])
            # Simulate an already-expired middle original while later originals remain.
            work.local.execute('DELETE FROM source_rows WHERE batch_id = ?', ('b2',))
            work.local.execute('DELETE FROM source_batches WHERE batch_id = ?', ('b2',))
            boundary = enable_attribution(work.local)
            self.assertEqual(boundary, (AttributionBoundary(DAY, 3),))
            self.assertEqual(
                [row[0] for row in work.local.execute(
                    'SELECT batch_id FROM attribution_batches ORDER BY sequence')],
                ['b1'],
            )
            apply(work.local, DAY, 'b4', 'correction', [('east', 'a', 4)])
            second = seal(work.local, DAY, 'second')
            advance(work.local, work.external, DAY)
            with self.assertRaises(EvidenceError) as missing:
                explain_delta(work.local, first.seal_id, second.seal_id)
            self.assertIn('b2', str(missing.exception))

    def test_exact_large_integer_attribution(self):
        large = 1 << 18000
        with self.open() as work:
            first = seal(work.local, DAY, 'first')
            advance(work.local, work.external, DAY)
            enable_attribution(work.local)
            apply(work.local, DAY, 'big', 'initial', [('east', 'large', large)])
            second = seal(work.local, DAY, 'second')
            advance(work.local, work.external, DAY)
            answer = explain_delta(work.local, first.seal_id, second.seal_id)
            self.assertEqual(simplified_changes(answer['changes']), [
                ('big', 'east', 'large', 1, None, large, large),
            ])
            purge(work.local, NEXT)
        with self.open() as work:
            answer = explain_delta(work.local, 'first', 'second')
            self.assertEqual(answer['changes'][0]['after'], large)
            self.assertEqual(answer['changes'][0]['delta'], large)
            self.assertEqual(answer['receipt'].delta, large)

    def test_later_intake_and_seals_do_not_change_fixed_interval(self):
        with self.open() as work:
            apply(work.local, DAY, 'boot', 'initial', [('east', 'a', 1)])
            first = seal(work.local, DAY, 'first')
            advance(work.local, work.external, DAY)
            enable_attribution(work.local)
            apply(work.local, DAY, 'c1', 'correction', [('east', 'a', 3)])
            second = seal(work.local, DAY, 'second')
            advance(work.local, work.external, DAY)
            snapshot = explain_delta(work.local, first.seal_id, second.seal_id)
            apply(work.local, DAY, 'c2', 'correction', [('east', 'a', 8)])
            third = seal(work.local, DAY, 'third')
            advance(work.local, work.external, DAY)
            apply(work.local, NEXT, 'other', 'initial', [('west', 'a', 5)])
            seal(work.local, NEXT, 'other-seal')
            advance(work.local, work.external, NEXT)
            self.assertEqual(
                explain_delta(work.local, first.seal_id, second.seal_id),
                snapshot,
            )
            self.assertEqual(simplified_changes(
                explain_delta(work.local, second.seal_id, third.seal_id)['changes']),
                [('c2', 'east', 'a', 1, 3, 8, 5)],
            )

    def test_enable_on_empty_workspace_and_idempotent_schema(self):
        from audit.attribution import init_attribution_schema
        with self.open() as work:
            self.assertEqual(enable_attribution(work.local), ())
            self.assertTrue(is_enabled(work.local))
            init_attribution_schema(work.local)
            self.assertEqual(enable_attribution(work.local), ())
            apply(work.local, DAY, 'later', 'initial', [('east', 'a', 2)])
            first = seal(work.local, DAY, 'first')
            advance(work.local, work.external, DAY)
            second = seal(work.local, DAY, 'second')
            advance(work.local, work.external, DAY)
            answer = explain_delta(work.local, first.seal_id, second.seal_id)
            self.assertEqual(answer['state'], 'confirmed')
            self.assertEqual(answer['changes'], [])
