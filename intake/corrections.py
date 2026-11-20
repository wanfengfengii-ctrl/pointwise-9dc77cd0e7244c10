from intake.observations import current_amount, replace_current_amount
from meter_core import ConflictError


def apply_corrections(connection, batch):
    """Apply corrections and return (changed_count, change_records).

    Each record is ``(position, station, name, before, after)`` using the
    batch's original one-based position; same-value errata are omitted.
    """
    changed = 0
    records = []
    for position, (station, name, litres) in enumerate(batch.rows, start=1):
        old = current_amount(connection, batch.day, station, name)
        if old is None:
            raise ConflictError(f'no observation to correct: {(batch.day, station, name)!r}')
        if old != litres:
            replace_current_amount(connection, batch.day, station, name, litres)
            changed += 1
            records.append((position, station, name, old, litres))
    return changed, records
