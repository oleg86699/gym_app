"""csv-direct парсер (link, anchor, text)."""

from __future__ import annotations

import io

import pytest

from domain.csv_inputs import parse_campaign, parse_link_anchor_text


def test_csv_basic():
    csv = b"link,anchor,text\nhttps://nawal.mx/,Nawal,\xd1\x82\xd0\xb5\xd0\xba\xd1\x81\xd1\x82 1\nhttps://x.com/,X,body2\n"
    rows = parse_link_anchor_text(csv, "in.csv")
    assert len(rows) == 2
    assert rows[0]["link"] == "https://nawal.mx/"
    assert rows[0]["anchor"] == "Nawal"
    assert rows[1]["text"] == "body2"


def test_csv_header_case_insensitive_and_extra_cols():
    csv = b"LINK,Text,Anchor,extra\nhttps://a.com/,hello,anc,zzz\n"
    rows = parse_link_anchor_text(csv, "x.csv")
    assert rows == [{"link": "https://a.com/", "anchor": "anc", "text": "hello"}]


def test_csv_skips_rows_without_link_or_text():
    csv = b"link,anchor,text\n,A,body\nhttps://a.com/,A,\nhttps://b.com/,A,ok\n"
    rows = parse_link_anchor_text(csv, "x.csv")
    assert len(rows) == 1 and rows[0]["link"] == "https://b.com/"


def test_csv_missing_required_columns():
    with pytest.raises(ValueError):
        parse_link_anchor_text(b"foo,bar\n1,2\n", "x.csv")


def test_unsupported_format():
    with pytest.raises(ValueError):
        parse_link_anchor_text(b"x", "file.txt")


def test_campaign_basic():
    # формат как на скрине: links, anchor, counts, type (type игнорим)
    csv = (b"links,anchor,counts,type\n"
           b"https://nawal.mx/,Nawal,5,anchor\n"
           b"https://nawal.mx/,Sitio Nawal,6,anchor\n")
    rows = parse_campaign(csv, "camp.csv")
    assert rows == [
        {"link": "https://nawal.mx/", "anchor": "Nawal", "count": 5},
        {"link": "https://nawal.mx/", "anchor": "Sitio Nawal", "count": 6},
    ]


def test_campaign_count_defaults_and_singular_headers():
    csv = b"link,anchor,count\nhttps://a.com/,A,\nhttps://b.com/,B,3\n,skip,9\n"
    rows = parse_campaign(csv, "x.csv")
    assert rows == [
        {"link": "https://a.com/", "anchor": "A", "count": 1},
        {"link": "https://b.com/", "anchor": "B", "count": 3},
    ]


def test_campaign_missing_links_column():
    with pytest.raises(ValueError):
        parse_campaign(b"anchor,counts\nA,5\n", "x.csv")


def test_xlsx_basic():
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["link", "anchor", "text"])
    ws.append(["https://nawal.mx/", "Nawal", "тело"])
    ws.append(["", "skip", "no link"])
    buf = io.BytesIO()
    wb.save(buf)
    rows = parse_link_anchor_text(buf.getvalue(), "in.xlsx")
    assert len(rows) == 1
    assert rows[0]["link"] == "https://nawal.mx/" and rows[0]["text"] == "тело"
