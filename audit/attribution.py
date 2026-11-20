"""Evidence preservation and per-number delta attribution.

The evidence tables live in the local warehouse database and are independent of
the V1 source payloads: purging the originals only removes ``source_batches`` /
``source_rows`` while attribution evidence stays intact.  A database recognizes
a single first successful enable boundary; enabling again keeps that boundary.
"""
from dataclasses import dataclass

from close.revisions import get_revision
from dispatch.confirm import confirmation_for_revision
from meter_core import (
    EvidenceError,
    InputError,
    decode_litres,
    encode_litres,
    require_name,
    transaction,
)


@dataclass(frozen=True, slots=True)
class AttributionBoundary:
    day: str
    enable_sequence: int


def init_attribution_schema(connection):
    connection.execute('''
        CREATE TABLE IF NOT EXISTS attribution_state (
            marker INTEGER PRIMARY KEY CHECK (marker = 1)
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS attribution_boundaries (
            day TEXT PRIMARY KEY NOT NULL,
            enable_sequence INTEGER NOT NULL CHECK (enable_sequence >= 0)
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS attribution_batches (
            batch_id TEXT PRIMARY KEY NOT NULL REFERENCES batches (batch_id),
            day TEXT NOT NULL,
            sequence INTEGER NOT NULL
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS attribution_changes (
            batch_id TEXT NOT NULL REFERENCES attribution_batches (batch_id),
            position INTEGER NOT NULL,
            station TEXT NOT NULL,
            name TEXT NOT NULL,
            before_value TEXT,
            after_value TEXT NOT NULL,
            PRIMARY KEY (batch_id, position)
        ) WITHOUT ROWID
    ''')


def is_enabled(connection):
    return connection.execute(
        'SELECT 1 FROM attribution_state LIMIT 1',
    ).fetchone() is not None


def _boundaries(connection):
    rows = connection.execute(
        'SELECT day, enable_sequence FROM attribution_boundaries ORDER BY day',
    )
    return tuple(AttributionBoundary(day, sequence) for day, sequence in rows)


def _complete_prefix_end(connection, day, last_sequence):
    """Return the longest still-replayable 1-based prefix of the day."""
    present = [row[0] for row in connection.execute('''
        SELECT b.sequence
        FROM source_batches AS s JOIN batches AS b ON b.batch_id = s.batch_id
        WHERE b.day = ? ORDER BY b.sequence
    ''', (day,))]
    prefix_end = 0
    for sequence in present:
        if sequence != prefix_end + 1:
            break
        prefix_end = sequence
        if prefix_end == last_sequence:
            break
    return prefix_end


def _snapshot_prefix(connection, day, prefix_end):
    rows = connection.execute('''
        SELECT b.batch_id, b.kind, b.sequence, sr.position, sr.station, sr.name, sr.litres
        FROM source_batches AS sb
        JOIN batches AS b ON b.batch_id = sb.batch_id
        JOIN source_rows AS sr ON sr.batch_id = sb.batch_id
        WHERE b.day = ? AND b.sequence <= ?
        ORDER BY b.sequence, sr.position
    ''', (day, prefix_end)).fetchall()
    current = {}
    current_batch = None
    for batch_id, kind, sequence, position, station, name, after_hex in rows:
        if batch_id != current_batch:
            connection.execute(
                'INSERT INTO attribution_batches VALUES (?, ?, ?)',
                (batch_id, day, sequence),
            )
            current_batch = batch_id
        value = decode_litres(after_hex)
        key = (station, name)
        if kind == 'initial':
            before_hex = None
        else:
            old = current.get(key)
            if old is None or old == value:
                # Same-value errata produce no change; positions stay untouched.
                continue
            before_hex = encode_litres(old)
        connection.execute(
            'INSERT INTO attribution_changes VALUES (?, ?, ?, ?, ?, ?)',
            (batch_id, position, station, name, before_hex, after_hex),
        )
        current[key] = value


def enable_attribution(connection):
    """Enable attribution on a workspace, preserving replayable legacy prefixes."""
    init_attribution_schema(connection)
    with transaction(connection, write=True):
        if is_enabled(connection):
            return _boundaries(connection)
        connection.execute('INSERT INTO attribution_state (marker) VALUES (1)')
        boundaries = []
        days = [row[0] for row in connection.execute(
            'SELECT DISTINCT day FROM batches ORDER BY day',
        )]
        for day in days:
            last_sequence = connection.execute(
                'SELECT MAX(sequence) FROM batches WHERE day = ?', (day,),
            ).fetchone()[0]
            prefix_end = _complete_prefix_end(connection, day, last_sequence)
            if prefix_end:
                _snapshot_prefix(connection, day, prefix_end)
            connection.execute(
                'INSERT INTO attribution_boundaries VALUES (?, ?)',
                (day, last_sequence),
            )
            boundaries.append(AttributionBoundary(day, last_sequence))
        return tuple(boundaries)


def record_accepted_batch(connection, batch, sequence, change_records):
    """Preserve an accepted batch inside the caller's intake transaction."""
    if not is_enabled(connection):
        return
    connection.execute(
        'INSERT INTO attribution_batches VALUES (?, ?, ?)',
        (batch.batch_id, batch.day, sequence),
    )
    connection.executemany(
        'INSERT INTO attribution_changes VALUES (?, ?, ?, ?, ?, ?)',
        (
            (
                batch.batch_id, position, station, name,
                None if before is None else encode_litres(before),
                encode_litres(after),
            )
            for position, station, name, before, after in change_records
        ),
    )


def _load_pair(connection, earlier_seal_id, later_seal_id):
    earlier_seal_id = require_name(earlier_seal_id, 'earlier_seal_id')
    later_seal_id = require_name(later_seal_id, 'later_seal_id')
    earlier = get_revision(connection, earlier_seal_id)
    later = get_revision(connection, later_seal_id)
    if earlier is None or later is None:
        raise InputError('unknown seal id')
    if earlier.day != later.day:
        raise InputError('attribution only accepts two seals of the same day')
    if earlier_seal_id == later_seal_id:
        raise InputError('attribution requires two distinct seal ids')
    if later.sequence <= earlier.sequence:
        raise InputError('the later seal id must follow the earlier seal id')
    if later.sequence != earlier.sequence + 1:
        raise InputError('attribution only accepts adjacent seals of the day')
    return earlier, later


def _missing_evidence(connection, day, low_watermark, high_watermark):
    return connection.execute('''
        SELECT b.batch_id, b.sequence
        FROM batches AS b
        WHERE b.day = ? AND b.sequence > ? AND b.sequence <= ?
          AND NOT EXISTS (
              SELECT 1 FROM attribution_batches AS ab
              WHERE ab.batch_id = b.batch_id
          )
        ORDER BY b.sequence
    ''', (day, low_watermark, high_watermark)).fetchall()


def _load_changes(connection, day, low_watermark, high_watermark):
    rows = connection.execute('''
        SELECT b.batch_id, b.sequence, ac.position, ac.station, ac.name,
               ac.before_value, ac.after_value
        FROM attribution_changes AS ac
        JOIN attribution_batches AS ab ON ab.batch_id = ac.batch_id
        JOIN batches AS b ON b.batch_id = ab.batch_id
        WHERE b.day = ? AND b.sequence > ? AND b.sequence <= ?
        ORDER BY b.sequence, ac.position
    ''', (day, low_watermark, high_watermark)).fetchall()
    changes = []
    for batch_id, _sequence, position, station, name, before_hex, after_hex in rows:
        before = None if before_hex is None else decode_litres(before_hex)
        after = decode_litres(after_hex)
        changes.append({
            'batch_id': batch_id,
            'station': station,
            'name': name,
            'position': position,
            'before': before,
            'after': after,
            'delta': after - (0 if before is None else before),
        })
    return changes


def explain_delta(connection, earlier_seal_id, later_seal_id):
    with transaction(connection):
        earlier, later = _load_pair(connection, earlier_seal_id, later_seal_id)
        receipt = confirmation_for_revision(connection, later.seal_id)
        if receipt is None:
            return {'state': 'pending', 'receipt': None, 'changes': None}

        low_watermark = earlier.source_watermark
        high_watermark = later.source_watermark
        if low_watermark == high_watermark:
            if receipt.delta != 0:
                raise EvidenceError(
                    f'seal {later.seal_id!r} has no input change but its original '
                    f'receipt delta is {receipt.delta}, not zero'
                )
            return {'state': 'confirmed', 'receipt': receipt, 'changes': []}

        missing = _missing_evidence(
            connection, later.day, low_watermark, high_watermark,
        )
        if missing:
            detail = ', '.join(f'{batch_id}(seq={sequence})'
                               for batch_id, sequence in missing)
            raise EvidenceError(
                f'attribution evidence missing for batch(es): {detail}; '
                f'interval {earlier.seal_id!r} -> {later.seal_id!r} cannot be replayed'
            )

        changes = _load_changes(
            connection, later.day, low_watermark, high_watermark,
        )
        total = sum(item['delta'] for item in changes)
        if total != receipt.delta:
            raise EvidenceError(
                f'attribution change total {total} does not match the original '
                f'receipt delta {receipt.delta} for seal {later.seal_id!r}'
            )
        return {'state': 'confirmed', 'receipt': receipt, 'changes': changes}
