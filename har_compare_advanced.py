#!/usr/bin/env python3
from __future__ import annotations
def render_header_table(headers: dict) -> str:
    if not headers:
        return '<div class="td" style="color:var(--muted)">None</div>'
    rows = [f'<tr><th>Header</th><th>Value</th></tr>']
    for k, v in headers.items():
        rows.append(f'<tr><td>{html.escape(str(k))}</td><td style="word-break:break-word">{html.escape(str(v))}</td></tr>')
    return '<table class="header-table">' + ''.join(rows) + '</table>'
"""
Advanced HAR Comparison Tool
- Robust request pairing (endpoint + method + parameters). GraphQL pairs by operationName + normalized query
- SQLite storage of requests/responses with headers/meta and timestamps
- Light theme UI with: Tabs (Added/Removed, Changed), domain checkbox filtering (persistent), search box (live),
  detailed rows with headers and GraphQL details, and GraphQL query name in brackets for identification

Standard library only.
"""
import argparse
import difflib
import json
import html
import os
import sqlite3
from typing import Any, Dict, List, Tuple, Optional
from urllib.parse import urlparse, parse_qsl
from collections import defaultdict
from time import time

# ----------------------------- Utilities -----------------------------

def safe_get(d: Dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and k in d:
            d = d[k]
        else:
            return default
    return d


def list_to_kv_map(items: List[Dict[str, Any]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for it in items or []:
        n = it.get("name", "").lower()
        v = it.get("value", "")
        if n:
            out[n] = v
    return out


def normalize_url(u: str) -> Tuple[str, str, str]:
    try:
        p = urlparse(u)
        host = (p.netloc or "").lower()
        path = p.path or "/"
        return f"{p.scheme}://{host}{path}", host, path
    except Exception:
        return u, "", u


def canonicalize_json_str(s: Any) -> str:
    if s is None or s == "":
        return ""
    try:
        obj = s if isinstance(s, (dict, list)) else json.loads(s)
        return json.dumps(obj, sort_keys=True, separators=(",", ":"))
    except Exception:
        return str(s)


def normalize_graphql_query(q: Optional[str]) -> str:
    if not q:
        return ""
    # remove whitespace for structural match
    return "".join(q.split())


def query_params_signature(url: str) -> str:
    try:
        p = urlparse(url)
        pairs = parse_qsl(p.query, keep_blank_values=True)
        pairs.sort()
        return json.dumps(pairs, separators=(",", ":"))
    except Exception:
        return ""


# ----------------------------- HAR loading -----------------------------

def detect_graphql(req: Dict[str, Any]) -> bool:
    mime = safe_get(req, "postData", "mimeType") or ""
    if "graphql" in (mime or "").lower():
        return True
    # Heuristic: JSON body with keys query/operationName
    try:
        txt = safe_get(req, "postData", "text")
        if not txt:
            return False
        obj = json.loads(txt)
        return isinstance(obj, dict) and ("query" in obj or "operationName" in obj)
    except Exception:
        return False


def load_har(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    entries = safe_get(data, "log", "entries", default=[]) or []
    out: List[Dict[str, Any]] = []
    for e in entries:
        req = e.get("request", {})
        res = e.get("response", {})
        method = req.get("method", "GET")
        url = req.get("url", "")
        url_no_q, domain, endpoint = normalize_url(url)
        status = res.get("status")
        time_ms = e.get("time")
        req_headers = list_to_kv_map(req.get("headers"))
        res_headers = list_to_kv_map(res.get("headers"))
        req_body_text = safe_get(req, "postData", "text")
        res_body_text = safe_get(res, "content", "text")
        started = safe_get(e, "startedDateTime")

        item: Dict[str, Any] = {
            "type": "graphql" if detect_graphql(req) else "rest",
            "method": method,
            "url": url,
            "url_no_q": url_no_q,
            "domain": domain,
            "endpoint": endpoint,
            "status": status,
            "time": time_ms,
            "req_headers": req_headers,
            "res_headers": res_headers,
            "req_body": req_body_text,
            "res_body": res_body_text,
            "started_at": started,
        }
        # parameters signature for REST (query + JSON body if applicable)
        if item["type"] == "rest":
            params_sig = query_params_signature(url)
            json_body_sig = ""
            mime = (safe_get(req, "postData", "mimeType") or "").lower()
            if mime.startswith("application/json") and req_body_text:
                json_body_sig = canonicalize_json_str(req_body_text)
            item["param_signature"] = json.dumps({"query": params_sig, "json": json_body_sig}, sort_keys=True)
        else:
            # GraphQL fields
            gql_op = None
            gql_query_raw = None
            gql_vars = None
            if req_body_text:
                try:
                    pd = json.loads(req_body_text)
                    if isinstance(pd, dict):
                        gql_op = pd.get("operationName")
                        gql_query_raw = pd.get("query")
                        gql_vars = pd.get("variables")
                except Exception:
                    pass
            item["gql_operation"] = gql_op
            item["gql_query"] = gql_query_raw
            item["gql_query_norm"] = normalize_graphql_query(gql_query_raw)
            item["gql_variables"] = gql_vars
        out.append(item)
    return out


# ----------------------------- SQLite storage -----------------------------

def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT,
            file TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS requests(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER,
            type TEXT,
            method TEXT,
            url TEXT,
            url_no_q TEXT,
            domain TEXT,
            endpoint TEXT,
            status INTEGER,
            time_ms REAL,
            req_headers TEXT,
            res_headers TEXT,
            req_body BLOB,
            res_body BLOB,
            gql_operation TEXT,
            gql_query TEXT,
            gql_query_norm TEXT,
            gql_variables TEXT,
            started_at TEXT,
            FOREIGN KEY(run_id) REFERENCES runs(id)
        )
        """
    )
    return conn


def insert_run(conn: sqlite3.Connection, label: str, file: str) -> int:
    cur = conn.cursor()
    cur.execute("INSERT INTO runs(label, file) VALUES (?, ?)", (label, file))
    conn.commit()
    return cur.lastrowid


def insert_requests(conn: sqlite3.Connection, run_id: int, entries: List[Dict[str, Any]]):
    cur = conn.cursor()
    for it in entries:
        cur.execute(
            """
            INSERT INTO requests(
                run_id, type, method, url, url_no_q, domain, endpoint, status, time_ms,
                req_headers, res_headers, req_body, res_body, gql_operation, gql_query,
                gql_query_norm, gql_variables, started_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id, it.get("type"), it.get("method"), it.get("url"), it.get("url_no_q"), it.get("domain"), it.get("endpoint"),
                it.get("status"), it.get("time"), json.dumps(it.get("req_headers")), json.dumps(it.get("res_headers")),
                (it.get("req_body") or "").encode("utf-8"), (it.get("res_body") or "").encode("utf-8"),
                it.get("gql_operation"), it.get("gql_query"), it.get("gql_query_norm"), json.dumps(it.get("gql_variables")),
                it.get("started_at"),
            ),
        )
    conn.commit()


# ----------------------------- Comparators -----------------------------

class BaseComparator:
    def key(self, e: Dict[str, Any]) -> str:
        raise NotImplementedError

    def name(self, e: Dict[str, Any]) -> str:
        # default display name per request
        return f"{e.get('method','')} {e.get('endpoint','')}"


class GraphQLComparator(BaseComparator):
    def key(self, e: Dict[str, Any]) -> str:
        # Pair by endpoint + method + operationName + normalized query
        return f"{e.get('method')} {e.get('endpoint')} | op={e.get('gql_operation') or ''} | q={e.get('gql_query_norm') or ''}"

    def name(self, e: Dict[str, Any]) -> str:
        op = e.get("gql_operation") or ""
        # Display bracketed name as requested
        return f"[{op}] {e.get('method')} {e.get('endpoint')}" if op else f"{e.get('method')} {e.get('endpoint')}"


class RestComparator(BaseComparator):
    def key(self, e: Dict[str, Any]) -> str:
        # Pair by endpoint + method + parameter signature (query + json body signature if JSON)
        sig = e.get("param_signature") or ""
        return f"{e.get('method')} {e.get('endpoint')} | p={sig}"


# ----------------------------- Pairing and diff -----------------------------

def pair_entries_by_type(a: List[Dict[str, Any]], b: List[Dict[str, Any]]):
    gql_cmp = GraphQLComparator()
    rest_cmp = RestComparator()
    def group(items: List[Dict[str, Any]], cmp: BaseComparator):
        d: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for it in items:
            d[cmp.key(it)].append(it)
        return d

    a_gql = [x for x in a if x["type"] == "graphql"]
    b_gql = [x for x in b if x["type"] == "graphql"]
    a_rest = [x for x in a if x["type"] == "rest"]
    b_rest = [x for x in b if x["type"] == "rest"]

    added, removed, pairs = [], [], []
    for (a_items, b_items, cmp) in [ (a_gql, b_gql, gql_cmp), (a_rest, b_rest, rest_cmp) ]:
        ga, gb = group(a_items, cmp), group(b_items, cmp)
        keys = set(ga.keys()) | set(gb.keys())
        for k in keys:
            la, lb = ga.get(k, []), gb.get(k, [])
            n = max(len(la), len(lb))
            for i in range(n):
                xa = la[i] if i < len(la) else None
                xb = lb[i] if i < len(lb) else None
                if xa and not xb:
                    removed.append(xa)
                elif xb and not xa:
                    added.append(xb)
                else:
                    pairs.append((xa, xb))
    return added, removed, pairs


def dict_diff(old: Dict[str, str], new: Dict[str, str]) -> Dict[str, Any]:
    ok, nk = set(old.keys()), set(new.keys())
    added = {k: new[k] for k in nk - ok}
    removed = {k: old[k] for k in ok - nk}
    changed = {k: {"old": old[k], "new": new[k]} for k in ok & nk if old[k] != new[k]}
    return {"added": added, "removed": removed, "changed": changed}


def build_changed_rows(pairs: List[Tuple[Dict, Dict]]):
    rows = []
    domains = set()
    for a, b in pairs:
        domain = b.get("domain") or a.get("domain")
        domains.add(domain)
        req_hdr = dict_diff(a.get("req_headers", {}), b.get("req_headers", {}))
        res_hdr = dict_diff(a.get("res_headers", {}), b.get("res_headers", {}))
        # GraphQL diffs
        aq, bq = normalize_graphql_query(a.get("gql_query")), normalize_graphql_query(b.get("gql_query"))
        av, bv = canonicalize_json_str(a.get("gql_variables")), canonicalize_json_str(b.get("gql_variables"))
        op_a, op_b = a.get("gql_operation"), b.get("gql_operation")
        gql_query_changed = (aq != bq)
        gql_vars_changed = (av != bv)
        status_changed = a.get("status") != b.get("status")
        time_changed = None
        try:
            ta, tb = (a.get("time") or 0), (b.get("time") or 0)
            time_changed = abs((tb or 0) - (ta or 0)) > 100
        except Exception:
            time_changed = False
        headers_changed = any([req_hdr["added"], req_hdr["removed"], req_hdr["changed"], res_hdr["added"], res_hdr["removed"], res_hdr["changed"]])
        any_changed = any([status_changed, time_changed, headers_changed, gql_query_changed, gql_vars_changed])
        rows.append({
            "type": b.get("type") or a.get("type"),
            "domain": domain,
            "method": b.get("method") or a.get("method"),
            "endpoint": b.get("endpoint") or a.get("endpoint"),
            "url": b.get("url") or a.get("url"),
            "name": (f"[{op_b}] {b.get('method')} {b.get('endpoint')}" if op_b else f"{b.get('method')} {b.get('endpoint')}") if (b and b.get('type')=='graphql') else (f"[{op_a}] {a.get('method')} {a.get('endpoint')}" if a.get('type')=='graphql' and op_a else f"{a.get('method')} {a.get('endpoint')}"),
            "status_a": a.get("status"),
            "status_b": b.get("status"),
            "time_a": a.get("time"),
            "time_b": b.get("time"),
            "req_hdr": req_hdr,
            "res_hdr": res_hdr,
            "gql": {
                "op_a": op_a, "op_b": op_b,
                "query_a": a.get("gql_query"),
                "query_b": b.get("gql_query"),
                "vars_a": a.get("gql_variables"),
                "vars_b": b.get("gql_variables"),
                "query_changed": gql_query_changed,
                "vars_changed": gql_vars_changed,
            },
            "badges": {
                "status": status_changed,
                "time": time_changed,
                "headers": headers_changed,
                "gql_query": gql_query_changed,
                "gql_vars": gql_vars_changed,
            },
            "any_changed": any_changed,
        })
    return rows, sorted(domains)


# ----------------------------- HTML rendering (light theme) -----------------------------

PRIMARY = "#2563eb"

CSS_TEMPLATE = """
:root{--primary:__PRIMARY__;--bg:#f9fafb;--panel:#ffffff;--muted:#6b7280;--ok:#16a34a;--warn:#ea580c;--bad:#dc2626;--chip:#eef2ff;--text:#111827;--border:#e5e7eb}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,"Helvetica Neue",Arial,sans-serif}a{color:inherit;text-decoration:none}
.container{max-width:1200px;margin:24px auto;padding:0 16px}
.h1{font-size:22px;margin:0 0 12px;font-weight:700}
.toolbar{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;margin:12px 0}
.tabs{display:flex;gap:8px}
.tab{padding:8px 12px;border-radius:8px;background:#e5e7eb;cursor:pointer;color:#374151}.tab.active{background:var(--primary);color:#fff}
.filters{display:flex;flex-wrap:wrap;gap:12px;align-items:center}
.checkbox-list label{margin-right:10px;font-size:13px}
input[type='search']{padding:8px 10px;border:1px solid var(--border);border-radius:8px;min-width:260px}
.table{width:100%;border-collapse:separate;border-spacing:0 8px}
.tr{background:var(--panel);border:1px solid var(--border);border-radius:10px;overflow:hidden}
.td{padding:10px 12px;vertical-align:top;font-size:13px;color:#374151}
.url{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:560px;display:inline-block}
.badge{display:inline-block;padding:2px 6px;border-radius:999px;font-size:11px;margin-right:6px;background:var(--chip);color:#3730a3;border:1px solid #c7d2fe}
.badge.good{background:#ecfdf5;color:#065f46;border-color:#a7f3d0}
.badge.warn{background:#fff7ed;color:#9a3412;border-color:#fed7aa}
.badge.bad{background:#fef2f2;color:#7f1d1d;border-color:#fecaca}
.code{background:#f3f4f6;border:1px solid var(--border);border-radius:8px;padding:8px;max-height:280px;white-space:pre-wrap;overflow-x:auto;word-break:break-word;overflow-y:auto}
.header-table{width:100%;border-collapse:collapse;margin:6px 0 0 0;table-layout:fixed}
.header-table th,.header-table td{padding:4px 8px;font-size:13px;text-align:left;vertical-align:top;word-break:break-word;max-width:320px}
.header-table th{background:#f3f4f6;color:var(--muted);font-weight:600;border-bottom:1px solid var(--border)}
.header-table td{background:#fff}
.section-title{color:#1d4ed8;margin:6px 0 4px 0;font-weight:600}
.expand{cursor:pointer}
.details{display:none;padding:8px 12px 12px 12px;border-top:1px solid var(--border);background:#fafafa}
.diff .old{background:#fee2e2;color:#7f1d1d;padding:0 3px;border-radius:4px}
.diff .new{background:#dcfce7;color:#065f46;padding:0 3px;border-radius:4px}
.checkbox-list{display:flex;flex-wrap:wrap;gap:8px}
"""
CSS = CSS_TEMPLATE.replace("__PRIMARY__", PRIMARY)

JS = """
function showTab(name){document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));document.querySelectorAll('.panel').forEach(p=>p.style.display='none');document.getElementById('panel-'+name).style.display='block';document.getElementById('tab-'+name).classList.add('active');filterRows()}
function toggleDetails(id){const el=document.getElementById(id);el.style.display=(el.style.display==='table-row'?'none':'table-row')}
function getCheckedDomains(){return Array.from(document.querySelectorAll('.domain-checkbox')).filter(c=>c.checked).map(c=>c.value)}
function savePrefs(){const prefs={domains:getCheckedDomains(),search:document.getElementById('searchBox').value,headerKey:document.getElementById('headerKeyBox').value,headerVal:document.getElementById('headerValBox').value};localStorage.setItem('harComparePrefs',JSON.stringify(prefs))}
function loadPrefs(){try{const p=JSON.parse(localStorage.getItem('harComparePrefs')||'{}');if(p.search!==undefined){document.getElementById('searchBox').value=p.search}if(p.domains&&p.domains.length){document.querySelectorAll('.domain-checkbox').forEach(c=>{c.checked=p.domains.includes(c.value)})}if(p.headerKey!==undefined){document.getElementById('headerKeyBox').value=p.headerKey}if(p.headerVal!==undefined){document.getElementById('headerValBox').value=p.headerVal}}catch(e){} }
function onFilterChanged(){savePrefs();filterRows()}
function filterRows(){
    const s=(document.getElementById('searchBox').value||'').toLowerCase();
    const ds=new Set(getCheckedDomains());
    const headerKey=(document.getElementById('headerKeyBox').value||'').toLowerCase();
    const headerVal=(document.getElementById('headerValBox').value||'').toLowerCase();
    const matchType=(document.getElementById('headerMatchType')||{value:'contains'}).value;
    document.querySelectorAll('[data-row="req"]').forEach(r=>{
        const domain=r.getAttribute('data-domain');
        const name=(r.getAttribute('data-name')||'').toLowerCase();
        let domOk=ds.size===0||ds.has(domain);
        let sOk=s===''||name.includes(s);
        let headerOk=true;
        if(headerKey||headerVal){
            let found=false;
            // Try to find header in data-req-headers and data-res-headers
            let reqHeaders=JSON.parse(r.getAttribute('data-req-headers')||'{}');
            let resHeaders=JSON.parse(r.getAttribute('data-res-headers')||'{}');
            [reqHeaders,resHeaders].forEach(hs=>{
                Object.keys(hs).forEach(k=>{
                    let keyMatch = !headerKey || k.toLowerCase().includes(headerKey);
                    let valMatch = false;
                    if(!headerVal) valMatch = true;
                    else if(matchType==='exact') valMatch = ((''+hs[k]).toLowerCase() === headerVal);
                    else valMatch = ((''+hs[k]).toLowerCase().includes(headerVal));
                    if(keyMatch && valMatch){found=true;}
                })
            })
            headerOk=found;
        }
        r.style.display=(domOk&&sOk&&headerOk)?'table-row':'none';
        const det=document.getElementById(r.getAttribute('data-detail-id'));
        if(det){det.style.display='none'}
    })
}
function selectAllDomains(checked){document.querySelectorAll('.domain-checkbox').forEach(c=>c.checked=checked);onFilterChanged()}
window.addEventListener('DOMContentLoaded',()=>{loadPrefs();filterRows()});
"""

HTML_HEAD = """
<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>HAR Compare - Advanced</title>
<style>__CSS__</style>
</head><body>
<div class=\"container\">
  <div class=\"h1\">HAR Compare - Advanced</div>
  <div class=\"toolbar\">
    <div class=\"tabs\">
      <div id=\"tab-added\" class=\"tab active\" onclick=\"showTab('added')\">Added/Removed</div>
      <div id=\"tab-changed\" class=\"tab\" onclick=\"showTab('changed')\">Changed</div>
    </div>
    <div class=\"filters\">
      <div class=\"checkbox-list\">__DOMAIN_CHECKBOXES__ <button onclick=\"selectAllDomains(true)\" type=\"button\">All</button> <button onclick=\"selectAllDomains(false)\" type=\"button\">None</button></div>
      <input id=\"searchBox\" type=\"search\" placeholder=\"Search request name...\" oninput=\"onFilterChanged()\"/>
            <input id=\"headerKeyBox\" type=\"search\" placeholder=\"Header key...\" style=\"min-width:120px\" oninput=\"onFilterChanged()\"/>
            <input id=\"headerValBox\" type=\"search\" placeholder=\"Header value...\" style=\"min-width:120px\" oninput=\"onFilterChanged()\"/>
            <select id="headerMatchType" style="min-width:100px" onchange="onFilterChanged()">
                <option value="contains">contains</option>
                <option value="exact">exact match</option>
            </select>
  </div>
"""

HTML_FOOT = """
</div>
<script>__JS__</script>
</body></html>
"""


def render_header_diff(title: str, diff: Dict[str, Any]) -> str:
    parts = [f'<div class="section-title">{html.escape(title)}</div>']
    if diff["added"] or diff["removed"] or diff["changed"]:
        parts.append('<table class="header-table">')
        parts.append('<tr><th>Header</th><th>Old Value</th><th>New Value</th></tr>')
        # Changed
        for k, ch in diff["changed"].items():
            parts.append(f'<tr><td>{html.escape(k)}</td><td style="background:#fee2e2">{html.escape(str(ch["old"]))}</td><td style="background:#dcfce7">{html.escape(str(ch["new"]))}</td></tr>')
        # Added
        for k, v in diff["added"].items():
            parts.append(f'<tr><td>{html.escape(k)}</td><td></td><td style="background:#ecfdf5">{html.escape(v)}</td></tr>')
        # Removed
        for k, v in diff["removed"].items():
            parts.append(f'<tr><td>{html.escape(k)}</td><td style="background:#fef2f2">{html.escape(v)}</td><td></td></tr>')
        parts.append('</table>')
    else:
        parts.append('<div class="td" style="color:var(--muted)">No changes</div>')
    return "".join(parts)


def render_graphql_details(gql: Dict[str, Any]) -> str:
    parts = []
    op_a = gql.get("op_a")
    op_b = gql.get("op_b")
    if op_a or op_b:
        left = html.escape(str(op_a or ""))
        right = html.escape(str(op_b or ""))
        changed = ' <span class="badge warn">changed</span>' if left!=right else ''
        title = f'GraphQL Operation {changed}'
        parts.append(f'<div class="section-title">{title}</div>')
        parts.append(f'<div class="td">[{left}] → [{right}]</div>')
    if gql.get("query_a") or gql.get("query_b"):
        q_changed = gql.get("query_changed")
        change_badge = '<span class="badge warn">changed</span>' if q_changed else ''
        qa = str(gql.get("query_a") or "")
        qb = str(gql.get("query_b") or "")
        parts.append(f'<div class="section-title">Query {change_badge}</div>')
        parts.append('<div style="display:flex;gap:16px;flex-wrap:wrap">')
        parts.append('<div style="flex:1;min-width:240px"><div style="color:var(--muted);font-size:12px">Before</div><div class="code">'+diff_text(qa, qb)+'</div></div>')
        parts.append('<div style="flex:1;min-width:240px"><div style="color:var(--muted);font-size:12px">After</div><div class="code">'+diff_text(qb, qa)+'</div></div>')
        parts.append('</div>')
    if gql.get("vars_a") is not None or gql.get("vars_b") is not None:
        v_changed = gql.get("vars_changed")
        vars_badge = '<span class="badge warn">changed</span>' if v_changed else ''
        va = json.dumps(gql.get("vars_a"), indent=2, ensure_ascii=False) if gql.get("vars_a") is not None else ''
        vb = json.dumps(gql.get("vars_b"), indent=2, ensure_ascii=False) if gql.get("vars_b") is not None else ''
        parts.append(f'<div class="section-title">Variables {vars_badge}</div>')
        parts.append('<div style="display:flex;gap:16px;flex-wrap:wrap">')
        parts.append('<div style="flex:1;min-width:240px"><div style="color:var(--muted);font-size:12px">Before</div><pre class="code">'+diff_text(va, vb)+'</pre></div>')
        parts.append('<div style="flex:1;min-width:240px"><div style="color:var(--muted);font-size:12px">After</div><pre class="code">'+diff_text(vb, va)+'</pre></div>')
        parts.append('</div>')
    return "".join(parts)


def escape(s: Any) -> str:
    return html.escape(str(s))

def diff_text(a: str, b: str) -> str:
    """Return HTML with word-level diff highlighting between a and b."""
    if a is None: a = ""
    if b is None: b = ""
    a_lines = a.splitlines()
    b_lines = b.splitlines()
    # If multiline, do line diff; else, do word diff
    if len(a_lines) > 1 or len(b_lines) > 1:
        sm = difflib.SequenceMatcher(None, a_lines, b_lines)
        out = []
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == 'equal':
                for line in a_lines[i1:i2]:
                    out.append(html.escape(line))
            elif tag == 'replace':
                for line in a_lines[i1:i2]:
                    out.append('<span class="diff old">'+html.escape(line)+'</span>')
                for line in b_lines[j1:j2]:
                    out.append('<span class="diff new">'+html.escape(line)+'</span>')
            elif tag == 'delete':
                for line in a_lines[i1:i2]:
                    out.append('<span class="diff old">'+html.escape(line)+'</span>')
            elif tag == 'insert':
                for line in b_lines[j1:j2]:
                    out.append('<span class="diff new">'+html.escape(line)+'</span>')
        return '\n'.join(out)
    else:
        # word-level diff
        sm = difflib.SequenceMatcher(None, a, b)
        out = []
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == 'equal':
                out.append(html.escape(a[i1:i2]))
            elif tag == 'replace':
                if i1 != i2:
                    out.append('<span class="diff old">'+html.escape(a[i1:i2])+'</span>')
                if j1 != j2:
                    out.append('<span class="diff new">'+html.escape(b[j1:j2])+'</span>')
            elif tag == 'delete':
                out.append('<span class="diff old">'+html.escape(a[i1:i2])+'</span>')
            elif tag == 'insert':
                out.append('<span class="diff new">'+html.escape(b[j1:j2])+'</span>')
        return ''.join(out)


def generate_html(added: List[Dict], removed: List[Dict], changed_rows: List[Dict], domains: List[str]) -> str:
    css = CSS
    domain_checks = [f'<label><input type="checkbox" class="domain-checkbox" value="{escape(d)}" checked onchange="onFilterChanged()"> {escape(d)}</label>' for d in domains]
    head = HTML_HEAD.replace("__CSS__", css).replace("__DOMAIN_CHECKBOXES__", "".join(domain_checks))

    def row_badges(b):
        parts = []
        if b.get("status"): parts.append('<span class="badge warn">status</span>')
        if b.get("time"): parts.append('<span class="badge warn">time</span>')
        if b.get("headers"): parts.append('<span class="badge warn">headers</span>')
        if b.get("gql_query"): parts.append('<span class="badge warn">gql:query</span>')
        if b.get("gql_vars"): parts.append('<span class="badge warn">gql:variables</span>')
        return "".join(parts) or '<span class="badge">no-change</span>'

    # Added/Removed Panel
    html_added = ['<div id="panel-added" class="panel" style="display:block">']
    # New Requests section
    html_added.append('<h3 class="section-title">New Requests</h3>')
    html_added.append('<table class="table">')
    for i,x in enumerate(added):
        rid = f"add-{i}"
        if x.get('type') == 'graphql' and x.get('gql_operation'):
            display_name = f"[{escape(x.get('gql_operation'))}] {escape(x['method'])} {escape(x['endpoint'])}"
        else:
            display_name = f"{escape(x['method'])} {escape(x['endpoint'])}"
        html_added.append('<tr class="tr expand" data-row="req" onclick="toggleDetails(\'%s\')" data-detail-id="%s" data-domain="%s" data-name="%s" data-req-headers="%s" data-res-headers="%s">'%(
            rid, rid, escape(x['domain']), display_name,
            html.escape(json.dumps(x.get('req_headers') or {})),
            html.escape(json.dumps(x.get('res_headers') or {}))
        ))
        html_added.append('<td class="td">%s</td>'%escape(x['method']))
        html_added.append('<td class="td"><span class="url">%s</span><div style="color:var(--muted);font-size:12px">%s</div></td>'%(escape(x['url']), display_name))
        html_added.append('<td class="td"><span class="badge good">added</span></td>')
        html_added.append('</tr>')
        # details row
        html_added.append('<tr id="%s" class="details"><td class="td" colspan="3">'%rid)
        # show headers and GraphQL content if present
        html_added.append('<div class="section-title">Request Headers</div>')
        html_added.append(render_header_table(x.get('req_headers') or {}))
        html_added.append('<div class="section-title">Response Headers</div>')
        html_added.append(render_header_table(x.get('res_headers') or {}))
        if x.get('type')=='graphql':
            html_added.append(render_graphql_details({
                'op_a': None,'op_b': x.get('gql_operation'),
                'query_a': None,'query_b': x.get('gql_query'),
                'vars_a': None,'vars_b': x.get('gql_variables'),
                'query_changed': True if x.get('gql_query') else False,
                'vars_changed': True if x.get('gql_variables') else False,
            }))
        html_added.append('</td></tr>')
    html_added.append('</table>')

    # Missing Requests section
    html_added.append('<h3 class="section-title">Missing Requests</h3>')
    html_added.append('<table class="table">')
    for i,x in enumerate(removed):
        rid = f"rem-{i}"
        if x.get('type') == 'graphql' and x.get('gql_operation'):
            display_name = f"[{escape(x.get('gql_operation'))}] {escape(x['method'])} {escape(x['endpoint'])}"
        else:
            display_name = f"{escape(x['method'])} {escape(x['endpoint'])}"
        html_added.append('<tr class="tr expand" data-row="req" onclick="toggleDetails(\'%s\')" data-detail-id="%s" data-domain="%s" data-name="%s" data-req-headers="%s" data-res-headers="%s">'%(
            rid, rid, escape(x['domain']), display_name,
            html.escape(json.dumps(x.get('req_headers') or {})),
            html.escape(json.dumps(x.get('res_headers') or {}))
        ))
        html_added.append('<td class="td">%s</td>'%escape(x['method']))
        html_added.append('<td class="td"><span class="url">%s</span><div style="color:var(--muted);font-size:12px">%s</div></td>'%(escape(x['url']), display_name))
        html_added.append('<td class="td"><span class="badge bad">removed</span></td>')
        html_added.append('</tr>')
        html_added.append('<tr id="%s" class="details"><td class="td" colspan="3">'%rid)
        html_added.append('<div class="section-title">Request Headers</div>')
        html_added.append(render_header_table(x.get('req_headers') or {}))
        html_added.append('<div class="section-title">Response Headers</div>')
        html_added.append(render_header_table(x.get('res_headers') or {}))
        if x.get('type')=='graphql':
            html_added.append(render_graphql_details({
                'op_a': x.get('gql_operation'),'op_b': None,
                'query_a': x.get('gql_query'),'query_b': None,
                'vars_a': x.get('gql_variables'),'vars_b': None,
                'query_changed': True if x.get('gql_query') else False,
                'vars_changed': True if x.get('gql_variables') else False,
            }))
        html_added.append('</td></tr>')
    html_added.append('</table></div>')

    # Changed Panel
    html_changed = ['<div id="panel-changed" class="panel" style="display:none">']
    html_changed.append('<table class="table">')
    for i,row in enumerate(changed_rows):
        rid = f"chg-{i}"
        status_val = f"<span class='diff'><span class='old'>{escape(row['status_a'])}</span> → <span class='new'>{escape(row['status_b'])}</span></span>" if row['badges']['status'] else escape(row.get('status_b'))
        time_val = f"<span class='diff'><span class='old'>{escape(row['time_a'])}ms</span> → <span class='new'>{escape(row['time_b'])}ms</span></span>" if row['badges']['time'] else f"{escape(row.get('time_b'))}ms"
        name = escape(row.get('name') or '')
        html_changed.append('<tr class="tr expand" onclick="toggleDetails(\'%s\')" data-row="req" data-detail-id="%s" data-domain="%s" data-name="%s" data-req-headers="%s" data-res-headers="%s">'%(
            rid, rid, escape(row['domain']), name,
            html.escape(json.dumps(row.get('req_hdr',{}).get('old',{}) or {})),
            html.escape(json.dumps(row.get('res_hdr',{}).get('old',{}) or {}))
        ))
        html_changed.append('<td class="td">%s</td>'%escape(row['method']))
        html_changed.append('<td class="td"><span class="url">%s</span><div style="color:var(--muted);font-size:12px">%s</div></td>'%(escape(row['url']), name))
        html_changed.append('<td class="td">%s</td>'%status_val)
        html_changed.append('<td class="td">%s</td>'%time_val)
        html_changed.append('<td class="td">%s</td>'%row_badges(row['badges']))
        html_changed.append('</tr>')
        html_changed.append('<tr id="%s" class="details"><td class="td" colspan="5">'%rid)
        html_changed.append(render_header_diff("Request Headers", row['req_hdr']))
        html_changed.append(render_header_diff("Response Headers", row['res_hdr']))
        html_changed.append(render_graphql_details(row['gql']))
        html_changed.append('</td></tr>')
    html_changed.append('</table></div>')

    return head + "".join(html_added+html_changed) + HTML_FOOT.replace("__JS__", JS)


# ----------------------------- Main -----------------------------

def main():
    ap = argparse.ArgumentParser(description="Advanced HAR Compare with pairing, SQLite, and light UI")
    ap.add_argument('har_a', help='Old/baseline HAR file')
    ap.add_argument('har_b', help='New/comparison HAR file')
    ap.add_argument('-o','--output', default='compare_advanced.html', help='Output HTML file')
    ap.add_argument('--db', default='har_compare.db', help='SQLite database file to store requests')
    args = ap.parse_args()

    a = load_har(args.har_a)
    b = load_har(args.har_b)

    # Save to SQLite
    conn = init_db(args.db)
    run_a = insert_run(conn, 'old', os.path.abspath(args.har_a))
    insert_requests(conn, run_a, a)
    run_b = insert_run(conn, 'new', os.path.abspath(args.har_b))
    insert_requests(conn, run_b, b)
    conn.close()

    added, removed, pairs = pair_entries_by_type(a, b)
    changed_rows, domains = build_changed_rows(pairs)

    html_out = generate_html(added, removed, changed_rows, domains)
    with open(args.output, 'w', encoding='utf-8') as f:
        f.write(html_out)
    print(f"Report written to {args.output}")


if __name__ == '__main__':
    main()