"""Парсер CSV Content Engine: детект формата + строки (C1-3)."""

from __future__ import annotations

from domain.content_engine import parse_content_csv


def test_detect_direct():
    csv = b"link,anchor,text\nhttps://nawal.mx/,Nawal,<p>hi</p>\n"
    r = parse_content_csv(csv)
    assert r.fmt == "direct"
    assert r.rows == [{"link": "https://nawal.mx/", "anchor": "Nawal", "text": "<p>hi</p>"}]


def test_detect_campaign_with_aliases():
    # counts/links алиасы + count парсится + язык/keyword
    csv = b"anchor,links,counts,keyword,language\nNawal,https://nawal.mx/,5,casino,en\n"
    r = parse_content_csv(csv)
    assert r.fmt == "campaign"
    row = r.rows[0]
    assert row["count"] == 5 and row["link"] == "https://nawal.mx/"
    assert row["keyword"] == "casino" and row["language"] == "en"


def test_campaign_content_parametrs_json():
    csv = b'anchor,link,count,content_parametrs\nN,https://x.com/,2,"{""brand"":""Acme""}"\n'
    r = parse_content_csv(csv)
    assert r.fmt == "campaign"
    assert r.rows[0]["content_parametrs"] == {"brand": "Acme"}


def test_skips_bad_rows():
    csv = b"anchor,link,count\nNawal,https://nawal.mx/,3\n,,\nX,https://y.com/,1\n"
    r = parse_content_csv(csv)
    assert len(r.rows) == 2 and r.skipped == 1


def test_header_aliases_singular_and_plural():
    # keywords/url/lang — алиасы к keyword/link/language
    csv = b"anchor,url,counts,keywords,lang\nNawal,https://nawal.mx/,4,casino,en\n"
    r = parse_content_csv(csv)
    assert r.fmt == "campaign"
    row = r.rows[0]
    assert row["link"] == "https://nawal.mx/" and row["count"] == 4
    assert row["keyword"] == "casino" and row["language"] == "en"


def test_unknown_format():
    r = parse_content_csv(b"foo,bar\n1,2\n")
    assert r.fmt == "" and r.error


def test_parse_xlsx_campaign_real_layout():
    """XLSX как из LibreOffice: links,anchor,counts,type,keyword + числовые counts."""
    import io

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["links", "anchor", "counts", "type", "keyword"])
    ws.append(["https://1go-slots.com/", "https://1go-slots.com/", 90, "anchor", "1Go Casino"])
    ws.append(["https://1go-slots.com/", "1Go", 80, "anchor", "1Go"])
    ws.append(["https://1go-slots.com/", "1go bonus 2026", 10, "anchor", "1Go"])
    buf = io.BytesIO()
    wb.save(buf)

    r = parse_content_csv(buf.getvalue())
    assert r.fmt == "campaign"
    assert len(r.rows) == 3
    # counts (числовые ячейки) → int, type игнорируется, keyword читается
    assert r.rows[0]["count"] == 90 and r.rows[0]["link"] == "https://1go-slots.com/"
    assert r.rows[0]["anchor"] == "https://1go-slots.com/"
    assert r.rows[0]["keyword"] == "1Go Casino"
    assert sum(x["count"] for x in r.rows) == 180


def test_parse_site_filter():
    from api.admin.schemas.postings import parse_site_filter
    assert parse_site_filter("en, FR ,de") == ["en", "fr", "de"]
    assert parse_site_filter("us,uk,au") == ["us", "uk", "au"]
    assert parse_site_filter("") == []
    assert parse_site_filter(None) == []
