# app.py — CalDAV bridge for iCloud (FastAPI)
# Exposes simple JSON endpoints that forward real WebDAV (PROPFIND/REPORT/PUT/DELETE) to iCloud.
# Auth to this bridge via X-Api-Key header; to iCloud via APPLE_ID + APPLE_APP_PASSWORD env vars.

from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
import os, uuid, datetime as dt, requests
import xml.etree.ElementTree as ET
from typing import Optional, Dict, Any, List
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

app = FastAPI(title="CalDAV Bridge", version="1.0.0")

# ----- Config from environment -----
BASE = os.environ.get("BASE_URL", "https://caldav.icloud.com").rstrip("/")
APPLE_ID = os.environ.get("APPLE_ID")
APPLE_PW = os.environ.get("APPLE_APP_PASSWORD")
API_KEY = os.environ.get("API_KEY")  # optional; if set, clients must send X-Api-Key
TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "30"))

# DAV/CalDAV namespaces
NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav"}

def ensure_env():
    if not APPLE_ID or not APPLE_PW:
        raise HTTPException(500, "Server misconfigured: APPLE_ID or APPLE_APP_PASSWORD missing")

def require_key(x_api_key: Optional[str]):
    # FastAPI maps the header via alias below
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(401, "Invalid API key")

def dav(method: str, url: str, headers: Optional[Dict[str, str]] = None,
        body: Optional[str | bytes] = None, content_type_xml: bool = True) -> requests.Response:
    """Make a WebDAV/CalDAV request with sane defaults."""
    ensure_env()
    h = {}
    if content_type_xml:
        h["Content-Type"] = "application/xml; charset=utf-8"
    if headers:
        h.update(headers)
    try:
        r = requests.request(
            method=method, url=url, headers=h, data=body,
            auth=(APPLE_ID, APPLE_PW), allow_redirects=True, timeout=TIMEOUT
        )
    except requests.RequestException as e:
        raise HTTPException(502, f"Upstream request failed: {e!s}")
    if r.status_code >= 400:
        # Surface upstream error details
        raise HTTPException(r.status_code, r.text or r.reason)
    return r

def parse_xml(xml: str) -> ET.Element:
    try:
        return ET.fromstring(xml)
    except ET.ParseError:
        raise HTTPException(502, "Failed to parse XML from iCloud")

def find_text(root: ET.Element, xpath: str) -> Optional[str]:
    el = root.find(xpath, NS)
    return (el.text or "").strip() if el is not None and el.text else None

def extract_principal_href(xml: str) -> Optional[str]:
    root = parse_xml(xml)
    # Try several known props
    for xp in (
        ".//d:current-user-principal/d:href",
        ".//d:principal-URL/d:href",
        ".//{DAV:}current-user-principal/{DAV:}href",
        ".//{DAV:}principal-URL/{DAV:}href",
    ):
        val = find_text(root, xp)
        if val:
            return val
    return None

# ---------- Schemas ----------
class TimeRange(BaseModel):
    calendar_href: str
    start_z: str  # YYYYMMDDThhmmssZ
    end_z: str    # YYYYMMDDThhmmssZ

class CreateEvent(BaseModel):
    calendar_href: str
    summary: str
    dtstart_z: str
    dtend_z: str
    description: Optional[str] = None
    uid: Optional[str] = None

class DeleteEvent(BaseModel):
    href: str  # full href returned previously (relative path under caldav host)

# ---------- Routes ----------
@app.get("/health")
def health(x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    require_key(x_api_key)
    return {"ok": True}

@app.get("/principal")
def principal(x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    require_key(x_api_key)
    body = """<?xml version="1.0"?>
<D:propfind xmlns:D="DAV:">
  <D:prop><D:current-user-principal/></D:prop>
</D:propfind>"""
    # Try well-known then root
    for path in ("/.well-known/caldav", "/"):
        res = dav("PROPFIND", f"{BASE}{path}", {"Depth": "0"}, body).text
        href = extract_principal_href(res)
        if href:
            return {"principalHref": href}
    # Fallback: principal-URL
    body2 = """<?xml version="1.0"?>
<D:propfind xmlns:D="DAV:">
  <D:prop><D:principal-URL/></D:prop>
</D:propfind>"""
    res2 = dav("PROPFIND", f"{BASE}/", {"Depth": "0"}, body2).text
    href = extract_principal_href(res2)
    if href:
        return {"principalHref": href}
    raise HTTPException(500, "Could not parse principal href")

@app.get("/debug/principal-xml", response_class=PlainTextResponse)
def principal_xml(x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    require_key(x_api_key)
    body = """<?xml version="1.0"?>
<D:propfind xmlns:D="DAV:">
  <D:prop><D:current-user-principal/></D:prop>
</D:propfind>"""
    r = dav("PROPFIND", f"{BASE}/", {"Depth": "0"}, body)
    return r.text

@app.get("/home")
def home(x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    require_key(x_api_key)
    p = principal(x_api_key=x_api_key)["principalHref"]
    body = """<?xml version="1.0"?>
<D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop><C:calendar-home-set/></D:prop>
</D:propfind>"""
    res = dav("PROPFIND", f"{BASE}{p}", {"Depth": "0"}, body).text
    root = parse_xml(res)
    href = find_text(root, ".//c:calendar-home-set/d:href") or \
           find_text(root, ".//{urn:ietf:params:xml:ns:caldav}calendar-home-set/{DAV:}href")
    if not href:
        raise HTTPException(500, "Could not parse calendar-home-set")
    return {"calendarHome": href}

@app.get("/calendars")
def calendars(x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    require_key(x_api_key)
    home_href = home(x_api_key=x_api_key)["calendarHome"]
    body = """<?xml version="1.0"?>
<D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:ietf:params:xml:ns:caldav">
  <D:prop>
    <D:displayname/>
    <C:supported-calendar-component-set/>
    <D:resourcetype/>
  </D:prop>
</D:propfind>"""
    # Note: Some iCloud stacks tolerate the typo'd namespace; to be safe we query twice if needed
    def do_list(ns_ok: bool):
        xml = body if ns_ok else body.replace("urn:ietf:ietf:params:xml:ns:caldav", "urn:ietf:params:xml:ns:caldav")
        # Check if home_href is already a full URL
        url = home_href if home_href.startswith(('http://', 'https://')) else f"{BASE}{home_href}"
        return dav("PROPFIND", url, {"Depth": "1"}, xml).text

    try:
        res = do_list(True)
    except HTTPException:
        res = do_list(False)

    root = parse_xml(res)
    items: List[Dict[str, Any]] = []
    for resp in root.findall(".//d:response", NS):
        href_el = resp.find("d:href", NS)
        name_el = resp.find(".//d:displayname", NS)
        if href_el is None or name_el is None:
            continue
        href = (href_el.text or "").strip()
        name = (name_el.text or "").strip()
        if href:
            items.append({"href": href, "displayname": name})
    return {"home": home_href, "items": items}

@app.post("/events")
def events(tr: TimeRange, x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    require_key(x_api_key)
    # REPORT: VEVENTs in a time range
    body = f"""<?xml version="1.0"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:getetag/><c:calendar-data/></d:prop>
  <c:filter><c:comp-filter name="VCALENDAR">
    <c:comp-filter name="VEVENT">
      <c:time-range start="{tr.start_z}" end="{tr.end_z}"/>
    </c:comp-filter>
  </c:comp-filter></c:filter>
</c:calendar-query>"""
    # Check if calendar_href is already a full URL
    url = tr.calendar_href if tr.calendar_href.startswith(('http://', 'https://')) else f"{BASE}{tr.calendar_href}"
    res = dav("REPORT", url, {"Depth": "1"}, body).text
    root = parse_xml(res)
    items: List[Dict[str, str]] = []
    for resp in root.findall(".//d:response", NS):
        href_el = resp.find("d:href", NS)
        data_el = resp.find(".//c:calendar-data", NS)
        if href_el is None or data_el is None:
            continue
        href = (href_el.text or "").strip()
        ics  = (data_el.text or "").strip()
        if href and ics:
            items.append({"href": href, "ics": ics})
    return {"items": items}

@app.post("/create")
def create(ev: CreateEvent, x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    require_key(x_api_key)
    uid = (ev.uid or uuid.uuid4().hex).upper()
    dtstamp = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    description = (ev.description or '').replace('\n', '\\n')
    # ICS requires CRLF line endings; build with \r\n
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Clockwork//Study//EN",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{uid}@clockwork",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART:{ev.dtstart_z}",
        f"DTEND:{ev.dtend_z}",
        f"SUMMARY:{ev.summary}",
        f"DESCRIPTION:{description}",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ]
    ics = "\r\n".join(lines)
    # Check if calendar_href is already a full URL
    calendar_url = ev.calendar_href if ev.calendar_href.startswith(('http://', 'https://')) else f"{BASE}{ev.calendar_href}"
    url = f"{calendar_url}{uid}.ics"
    try:
        r = requests.put(
            url, data=ics.encode("utf-8"),
            headers={"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"},
            auth=(APPLE_ID, APPLE_PW), allow_redirects=True, timeout=TIMEOUT
        )
    except requests.RequestException as e:
        raise HTTPException(502, f"Upstream PUT failed: {e!s}")
    if r.status_code not in (200, 201, 204):
        raise HTTPException(r.status_code, r.text or r.reason)
    return {"ok": True, "uid": uid, "href": f"{ev.calendar_href}{uid}.ics"}

@app.post("/delete")
def delete(d: DeleteEvent, x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    require_key(x_api_key)
    try:
        # Check if href is already a full URL
        url = d.href if d.href.startswith(('http://', 'https://')) else f"{BASE}{d.href}"
        r = requests.delete(url, auth=(APPLE_ID, APPLE_PW), allow_redirects=True, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise HTTPException(502, f"Upstream DELETE failed: {e!s}")
    if r.status_code not in (200, 202, 204):
        raise HTTPException(r.status_code, r.text or r.reason)
    return {"ok": True}
