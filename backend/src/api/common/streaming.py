"""Хелперы для StreamingResponse-ов больших выгрузок (см. ADR-011)."""

from __future__ import annotations

import csv
import io
from collections.abc import AsyncIterator, Iterable


async def csv_stream(
    header: Iterable[str],
    rows: AsyncIterator[Iterable[str]],
    chunk_size: int = 100,
) -> AsyncIterator[bytes]:
    """
    Стримит CSV построчно, не материализуя весь dataset в памяти.

    Использование:
        return StreamingResponse(
            csv_stream(["name", "url"], my_async_iterator),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="result.csv"'},
        )
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(list(header))

    yield buf.getvalue().encode("utf-8")
    buf.seek(0)
    buf.truncate(0)

    count = 0
    async for row in rows:
        writer.writerow(list(row))
        count += 1
        if count >= chunk_size:
            yield buf.getvalue().encode("utf-8")
            buf.seek(0)
            buf.truncate(0)
            count = 0

    if count > 0:
        yield buf.getvalue().encode("utf-8")
