"""
Unit and Integration Tests for Universal Scraper & Moneycontrol Portal Scraper.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from tools.scraper_tools import (
    _clean_html_to_markdown,
    _extract_tables_from_soup,
    scrape_moneycontrol,
    scrape_url,
)
from bs4 import BeautifulSoup


def test_clean_html_to_markdown():
    sample_html = """
    <html>
      <head><title>Test Article Title</title></head>
      <body>
        <nav><a href="#">Home</a></nav>
        <h1>Main Heading</h1>
        <p>This is the first paragraph describing market momentum.</p>
        <table>
          <thead><tr><th>Metric</th><th>Value</th></tr></thead>
          <tbody>
            <tr><td>P/E Ratio</td><td>24.5</td></tr>
            <tr><td>ROE</td><td>18.2%</td></tr>
          </tbody>
        </table>
        <footer>Copyright 2026</footer>
      </body>
    </html>
    """
    res = _clean_html_to_markdown(sample_html)
    assert res["title"] == "Test Article Title"
    assert "Main Heading" in res["text"]
    assert "first paragraph" in res["text"]
    assert "Copyright" not in res["text"]  # footer stripped
    assert res["tables_count"] == 1
    assert len(res["tables"][0]["rows"]) == 2
    assert res["tables"][0]["rows"][0]["Metric"] == "P/E Ratio"
    assert res["tables"][0]["rows"][0]["Value"] == "24.5"


def test_scrape_url_invalid():
    res = scrape_url("not-a-valid-url")
    assert res["status"] == "error"
    assert "Invalid URL" in res["error"]


def test_scrape_url_mocked_http():
    mock_html = "<html><head><title>Mocked Site</title></head><body><h1>Report</h1><p>Financial details.</p></body></html>"
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "text/html"}
    mock_resp.text = mock_html

    with patch("tools.scraper_tools._fetch_http", return_value=mock_resp):
        res = scrape_url("https://mocked-site.com/report", no_cache=True)
        assert res["status"] == "ok"
        assert res["title"] == "Mocked Site"
        assert "Financial details." in res["text"]
        assert res["domain"] == "mocked-site.com"


def test_scrape_url_json_endpoint():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json.return_value = {"ticker": "TCS", "price": 2300.0}

    with patch("tools.scraper_tools._fetch_http", return_value=mock_resp):
        res = scrape_url("https://api.mocked-finance.com/quote", extract_mode="json", no_cache=True)
        assert res["status"] == "ok"
        assert res["content_type"] == "application/json"
        assert res["json_data"]["ticker"] == "TCS"
        assert res["json_data"]["price"] == 2300.0


def test_scrape_moneycontrol_resolve_and_parse():
    mock_suggest_resp = MagicMock()
    mock_suggest_resp.status_code = 200
    mock_suggest_resp.json.return_value = [
        {
            "name": "Tata Consultancy Services",
            "link_src": "https://www.moneycontrol.com/india/stockpricequote/computers-software/tataconsultancyservices/TCS",
            "sc_id": "TCS",
            "pdt_dis_nm": "Tata Consultancy Services",
        }
    ]

    mock_page_html = """
    <html>
      <head><title>TCS Stock Price</title></head>
      <body>
        <h1>Tata Consultancy Services Ltd.</h1>
        <table>
          <tr><td>Mkt Cap (Rs. Cr.)</td><td>830,785</td></tr>
          <tr><td>VWAP</td><td>2,283.22</td></tr>
          <tr><td>Beta</td><td>0.80</td></tr>
          <tr><td>52 Week High</td><td>3,350.00</td></tr>
          <tr><td>52 Week Low</td><td>1,976.80</td></tr>
          <tr><td>20D Avg Delivery(%)</td><td>49.48</td></tr>
        </table>
      </body>
    </html>
    """
    mock_page_resp = MagicMock()
    mock_page_resp.status_code = 200
    mock_page_resp.headers = {"content-type": "text/html"}
    mock_page_resp.text = mock_page_html

    def fake_fetch(url, *args, **kwargs):
        if "autosuggestion" in url:
            return mock_suggest_resp
        return mock_page_resp

    with patch("tools.scraper_tools._fetch_http", side_effect=fake_fetch):
        res = scrape_moneycontrol("TCS", no_cache=True)
        assert res["status"] == "ok"
        assert res["portal"] == "Moneycontrol"
        overview = res["overview_metrics"]
        assert overview["market_cap_cr"] == "830,785"
        assert overview["vwap"] == "2,283.22"
        assert overview["beta"] == "0.80"
        assert overview["52_week_high"] == "3,350.00"
        assert overview["20d_avg_delivery_pct"] == "49.48"

        # Test with specific fields requested
        res_fields = scrape_moneycontrol("TCS", fields=["beta", "vwap", "delivery"], no_cache=True)
        assert res_fields["status"] == "ok"
        assert res_fields["requested_fields"]["beta"] == "0.80"
        assert res_fields["requested_fields"]["vwap"] == "2,283.22"
        assert res_fields["requested_fields"]["delivery"] == "49.48"
