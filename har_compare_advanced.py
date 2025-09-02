#!/usr/bin/env python3
"""
Advanced HAR Comparison Tool with Domain Filtering and GraphQL Support
Features tabbed interface, domain filtering, and detailed GraphQL information.
"""

from __future__ import annotations
import argparse
import json
import sqlite3
import sys
import os
import html
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from collections import defaultdict, Counter
from typing import Dict, List, Tuple, Any
from datetime import datetime

import pandas as pd

def normalize_url(raw: str) -> Tuple[str, str, str]:
    try:
        parsed = urlparse(raw)
        host = parsed.netloc.lower()
        path = parsed.path
        
        # Normalize query parameters
        query_params = parse_qsl(parsed.query, keep_blank_values=True)
        query_params.sort()
        query_norm = urlencode(query_params)
        
        # Create normalized URL without query
        url_no_query = urlunparse((parsed.scheme, host, path, '', '', ''))
        
        return url_no_query, host, query_norm
    except Exception:
        return raw, '', ''

def list_to_kv_map(items: List[Dict[str, Any]]) -> Dict[str, str]:
    result = {}
    for item in items:
        name = item.get('name', '')
        value = item.get('value', '')
        if not name:
            continue
        result[name] = value
    return result

def safe_get(d: Dict, *keys, default=None):
    for key in keys:
        if isinstance(d, dict) and key in d:
            d = d[key]
        else:
            return default
    return d

def is_api_or_graphql(url: str, mime_type: str = None) -> bool:
    """
    Determine if a URL represents an API or GraphQL request.
    """
    url_lower = url.lower()
    
    # Check for common API patterns
    api_patterns = [
        '/api/', '/graphql', '/v1/', '/v2/', '/v3/', '/rest/',
        '.json', '/json', '/ajax'
    ]
    
    for pattern in api_patterns:
        if pattern in url_lower:
            return True
    
    # Check MIME type
    if mime_type:
        api_mime_types = [
            'application/json', 'application/graphql',
            'application/xml', 'text/xml'
        ]
        mime_lower = mime_type.lower()
        if any(mime_type in mime_lower for mime_type in api_mime_types):
            return True
    
    return False

def extract_graphql_info(post_data: str) -> Dict[str, Any]:
    """
    Extract GraphQL query and variables from POST data.
    """
    if not post_data:
        return {}
    
    try:
        data = json.loads(post_data)
        result = {}
        
        if 'query' in data:
            result['query'] = data['query']
        
        if 'variables' in data:
            result['variables'] = data['variables']
        
        if 'operationName' in data:
            result['operationName'] = data['operationName']
        
        return result
    except (json.JSONDecodeError, TypeError):
        return {}

def dict_diff(old_dict: Dict[str, str], new_dict: Dict[str, str]) -> Dict[str, Any]:
    """
    Compare two dictionaries and return added, removed, and changed items.
    """
    old_keys = set(old_dict.keys())
    new_keys = set(new_dict.keys())
    
    added = {k: new_dict[k] for k in new_keys - old_keys}
    removed = {k: old_dict[k] for k in old_keys - new_keys}
    changed = {}
    
    for k in old_keys & new_keys:
        if old_dict[k] != new_dict[k]:
            changed[k] = {'old': old_dict[k], 'new': new_dict[k]}
    
    return {'added': added, 'removed': removed, 'changed': changed}

# Database schema and functions
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS dataset (
  id TEXT PRIMARY KEY,
  file_path TEXT NOT NULL,
  loaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dataset_id TEXT NOT NULL,
  idx_in_file INTEGER NOT NULL,
  startedDateTime TEXT,
  method TEXT,
  url TEXT,
  url_no_query TEXT,
  url_norm TEXT,
  host TEXT,
  path TEXT,
  query_norm TEXT,
  status INTEGER,
  statusText TEXT,
  mimeType TEXT,
  bodySize INTEGER,
  time_ms REAL,
  occurrence INTEGER NOT NULL,
  post_json TEXT,
  graphql_query TEXT,
  graphql_variables TEXT,
  graphql_operation_name TEXT,
  FOREIGN KEY(dataset_id) REFERENCES dataset(id)
);

CREATE TABLE IF NOT EXISTS req_headers (
  request_id INTEGER,
  name TEXT,
  value TEXT
);

CREATE TABLE IF NOT EXISTS res_headers (
  request_id INTEGER,
  name TEXT,
  value TEXT
);
"""

def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    return conn

def ingest_har(conn: sqlite3.Connection, dataset_id: str, har_path: str) -> None:
    with open(har_path, 'r', encoding='utf-8') as f:
        har_data = json.load(f)
    
    # Insert dataset record
    conn.execute(
        "INSERT OR REPLACE INTO dataset (id, file_path, loaded_at) VALUES (?, ?, ?)",
        (dataset_id, har_path, datetime.now().isoformat())
    )
    
    entries = har_data.get('log', {}).get('entries', [])
    
    for idx, entry in enumerate(entries):
        request = entry.get('request', {})
        response = entry.get('response', {})
        
        url = request.get('url', '')
        url_no_query, host, query_norm = normalize_url(url)
        
        # Extract timing
        timings = entry.get('timings', {})
        time_ms = sum(v for v in timings.values() if isinstance(v, (int, float)) and v > 0)
        
        # Extract POST data and GraphQL info
        post_data = request.get('postData', {}).get('text', '') if request.get('postData') else ''
        graphql_info = extract_graphql_info(post_data)
        
        # Insert request
        cursor = conn.execute("""
            INSERT INTO requests (
                dataset_id, idx_in_file, startedDateTime, method, url, url_no_query, url_norm,
                host, path, query_norm, status, statusText, mimeType, bodySize, time_ms, occurrence, 
                post_json, graphql_query, graphql_variables, graphql_operation_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            dataset_id, idx,
            entry.get('startedDateTime', ''),
            request.get('method', ''),
            url, url_no_query, url_no_query,
            host, urlparse(url).path, query_norm,
            response.get('status', 0),
            response.get('statusText', ''),
            response.get('content', {}).get('mimeType', ''),
            response.get('bodySize', 0),
            time_ms, 1,
            json.dumps(post_data) if post_data else None,
            graphql_info.get('query'),
            json.dumps(graphql_info.get('variables')) if graphql_info.get('variables') else None,
            graphql_info.get('operationName')
        ))
        
        request_id = cursor.lastrowid
        
        # Insert request headers
        for header in request.get('headers', []):
            conn.execute(
                "INSERT INTO req_headers (request_id, name, value) VALUES (?, ?, ?)",
                (request_id, header.get('name', ''), header.get('value', ''))
            )
        
        # Insert response headers
        for header in response.get('headers', []):
            conn.execute(
                "INSERT INTO res_headers (request_id, name, value) VALUES (?, ?, ?)",
                (request_id, header.get('name', ''), header.get('value', ''))
            )
    
    conn.commit()

def df_from_sql(conn: sqlite3.Connection, dataset_id: str) -> Tuple[pd.DataFrame, Dict, Dict]:
    # Get requests
    query = """
        SELECT * FROM requests WHERE dataset_id = ?
        ORDER BY idx_in_file
    """
    df = pd.read_sql_query(query, conn, params=(dataset_id,))
    
    # Get request headers
    req_headers_query = """
        SELECT r.id, rh.name, rh.value
        FROM requests r
        JOIN req_headers rh ON r.id = rh.request_id
        WHERE r.dataset_id = ?
    """
    req_headers_df = pd.read_sql_query(req_headers_query, conn, params=(dataset_id,))
    req_headers = {}
    for _, row in req_headers_df.iterrows():
        if row['id'] not in req_headers:
            req_headers[row['id']] = {}
        req_headers[row['id']][row['name']] = row['value']
    
    # Get response headers
    res_headers_query = """
        SELECT r.id, rh.name, rh.value
        FROM requests r
        JOIN res_headers rh ON r.id = rh.request_id
        WHERE r.dataset_id = ?
    """
    res_headers_df = pd.read_sql_query(res_headers_query, conn, params=(dataset_id,))
    res_headers = {}
    for _, row in res_headers_df.iterrows():
        if row['id'] not in res_headers:
            res_headers[row['id']] = {}
        res_headers[row['id']][row['name']] = row['value']
    
    return df, req_headers, res_headers

def match_requests(dfA: pd.DataFrame, dfB: pd.DataFrame) -> pd.DataFrame:
    # Enhanced matching by URL, method, and GraphQL operation name
    dfA_match = dfA[['id', 'method', 'url_no_query', 'status', 'time_ms', 'mimeType', 'host', 'graphql_query', 'graphql_variables', 'graphql_operation_name']].copy()
    dfB_match = dfB[['id', 'method', 'url_no_query', 'status', 'time_ms', 'mimeType', 'host', 'graphql_query', 'graphql_variables', 'graphql_operation_name']].copy()
    
    dfA_match.columns = ['id_a', 'method', 'url', 'status_a', 'time_a', 'mime_a', 'host', 'graphql_query_a', 'graphql_variables_a', 'graphql_operation_name_a']
    dfB_match.columns = ['id_b', 'method', 'url', 'status_b', 'time_b', 'mime_b', 'host', 'graphql_query_b', 'graphql_variables_b', 'graphql_operation_name_b']
    
    # First, do a basic merge on method and URL
    matched = pd.merge(dfA_match, dfB_match, on=['method', 'url'], how='outer', indicator=True)
    
    # Now filter out GraphQL requests that have different operation names
    # Only keep matches where:
    # 1. Both have no GraphQL operation name (regular API requests)
    # 2. Both have the same GraphQL operation name
    # 3. One or both are not GraphQL requests
    
    def should_match(row):
        op_a = row.get('graphql_operation_name_a')
        op_b = row.get('graphql_operation_name_b')
        
        # If both are None/NaN, they match (regular API requests)
        if pd.isna(op_a) and pd.isna(op_b):
            return True
        
        # If both have operation names, they must be the same
        if not pd.isna(op_a) and not pd.isna(op_b):
            return str(op_a) == str(op_b)
        
        # If one has operation name and other doesn't, they don't match
        return False
    
    # Apply the filtering only to 'both' matches (not added/removed)
    both_mask = matched['_merge'] == 'both'
    if both_mask.any():
        both_rows = matched[both_mask]
        valid_matches = both_rows[both_rows.apply(should_match, axis=1)]
        
        # Combine valid matches with added/removed
        matched = pd.concat([
            matched[~both_mask],  # Keep added/removed as-is
            valid_matches         # Only valid both matches
        ], ignore_index=True)
    
    return matched

def render_graphql_details(query_a: str, variables_a: str, operation_a: str, 
                          query_b: str, variables_b: str, operation_b: str) -> str:
    """
    Render GraphQL query and variables comparison.
    """
    if not any([query_a, query_b, variables_a, variables_b]):
        return ''
    
    html_parts = ['<div class="graphql-section"><h5><i class="fas fa-code"></i> GraphQL Details</h5>']
    
    # Operation Name
    if operation_a or operation_b:
        html_parts.append('<div class="graphql-operation">')
        html_parts.append('<h6>Operation Name:</h6>')
        if operation_a != operation_b:
            html_parts.append(f'<div class="diff-line"><span class="old-value">{html.escape(operation_a or "None")}</span> → <span class="new-value">{html.escape(operation_b or "None")}</span></div>')
        else:
            html_parts.append(f'<div class="same-value">{html.escape(operation_a or operation_b or "None")}</div>')
        html_parts.append('</div>')
    
    # Query
    if query_a or query_b:
        html_parts.append('<div class="graphql-query">')
        html_parts.append('<h6>Query:</h6>')
        if query_a != query_b:
            html_parts.append('<div class="query-diff">')
            if query_a:
                html_parts.append(f'<div class="old-query"><strong>Old Query:</strong><pre>{html.escape(query_a)}</pre></div>')
            else:
                html_parts.append('<div class="old-query"><strong>Old Query:</strong><pre>None</pre></div>')
            if query_b:
                html_parts.append(f'<div class="new-query"><strong>New Query:</strong><pre>{html.escape(query_b)}</pre></div>')
            else:
                html_parts.append('<div class="new-query"><strong>New Query:</strong><pre>None</pre></div>')
            html_parts.append('</div>')
        else:
            html_parts.append(f'<pre class="same-query">{html.escape(query_a or query_b or "None")}</pre>')
        html_parts.append('</div>')
    
    # Variables
    if variables_a or variables_b:
        html_parts.append('<div class="graphql-variables">')
        html_parts.append('<h6>Variables:</h6>')
        if variables_a != variables_b:
            html_parts.append('<div class="variables-diff">')
            if variables_a:
                html_parts.append(f'<div class="old-variables"><strong>Old Variables:</strong><pre>{html.escape(variables_a)}</pre></div>')
            else:
                html_parts.append('<div class="old-variables"><strong>Old Variables:</strong><pre>None</pre></div>')
            if variables_b:
                html_parts.append(f'<div class="new-variables"><strong>New Variables:</strong><pre>{html.escape(variables_b)}</pre></div>')
            else:
                html_parts.append('<div class="new-variables"><strong>New Variables:</strong><pre>None</pre></div>')
            html_parts.append('</div>')
        else:
            html_parts.append(f'<pre class="same-variables">{html.escape(variables_a or variables_b or "None")}</pre>')
        html_parts.append('</div>')
    
    html_parts.append('</div>')
    return ''.join(html_parts)

def render_header_diff(diff: Dict[str, Any], title: str) -> str:
    """
    Render header differences in a clean format.
    """
    if not any(diff.values()):
        return f'<div class="no-changes">{title}: No changes</div>'
    
    html_parts = [f'<div class="diff-section"><h4 class="diff-title">{title}</h4>']
    
    if diff['added']:
        html_parts.append('<div class="added-headers"><h5>Added Headers:</h5><ul>')
        for name, value in diff['added'].items():
            html_parts.append(f'<li><strong>{html.escape(name)}:</strong> {html.escape(value)}</li>')
        html_parts.append('</ul></div>')
    
    if diff['removed']:
        html_parts.append('<div class="removed-headers"><h5>Removed Headers:</h5><ul>')
        for name, value in diff['removed'].items():
            html_parts.append(f'<li><strong>{html.escape(name)}:</strong> {html.escape(value)}</li>')
        html_parts.append('</ul></div>')
    
    if diff['changed']:
        html_parts.append('<div class="changed-headers"><h5>Changed Headers:</h5><ul>')
        for name, values in diff['changed'].items():
            html_parts.append(f'<li><strong>{html.escape(name)}:</strong> {html.escape(values["old"])} → {html.escape(values["new"])}</li>')
        html_parts.append('</ul></div>')
    
    html_parts.append('</div>')
    return ''.join(html_parts)

def generate_domain_filter_js(domains: List[str]) -> str:
    """
    Generate JavaScript for domain filtering functionality.
    """
    domains_json = json.dumps(sorted(set(domains)))
    
    return f"""
    const availableDomains = {domains_json};
    
    function initDomainFilter() {{
        const filterBtn = document.getElementById('domainFilterBtn');
        const modal = document.getElementById('domainFilterModal');
        const applyBtn = document.getElementById('applyDomainFilter');
        const clearBtn = document.getElementById('clearDomainFilter');
        const checkboxContainer = document.getElementById('domainCheckboxes');
        
        // Populate domain checkboxes
        availableDomains.forEach(domain => {{
            const div = document.createElement('div');
            div.className = 'domain-checkbox-item';
            const domainId = `domain_${{domain.replace(/\\./g, '_')}}`;
            div.innerHTML = `
                <input type="checkbox" value="${{domain}}" id="${{domainId}}" checked>
                <label for="${{domainId}}">${{domain}}</label>
            `;
            
            // Add click handler for the entire item
            div.addEventListener('click', function(e) {{
                if (e.target.type !== 'checkbox') {{
                    const checkbox = this.querySelector('input[type="checkbox"]');
                    checkbox.checked = !checkbox.checked;
                }}
                updateCheckboxItemStyle(this);
            }});
            
            // Add change handler for checkbox
            const checkbox = div.querySelector('input[type="checkbox"]');
            checkbox.addEventListener('change', function() {{
                updateCheckboxItemStyle(div);
            }});
            
            updateCheckboxItemStyle(div);
            checkboxContainer.appendChild(div);
        }});
        
        function updateCheckboxItemStyle(item) {{
            const checkbox = item.querySelector('input[type="checkbox"]');
            if (checkbox.checked) {{
                item.classList.add('checked');
            }} else {{
                item.classList.remove('checked');
            }}
        }}
        
        // Apply filter
        applyBtn.addEventListener('click', function() {{
            const selectedDomains = Array.from(document.querySelectorAll('#domainCheckboxes input:checked')).map(cb => cb.value);
            filterByDomains(selectedDomains);
            bootstrap.Modal.getInstance(modal).hide();
        }});
        
        // Clear filter
        clearBtn.addEventListener('click', function() {{
            document.querySelectorAll('#domainCheckboxes input').forEach(cb => {{
                cb.checked = true;
                updateCheckboxItemStyle(cb.closest('.domain-checkbox-item'));
            }});
            filterByDomains(availableDomains);
            bootstrap.Modal.getInstance(modal).hide();
        }});
    }}
    
    function filterByDomains(selectedDomains) {{
        const rows = document.querySelectorAll('#changed tbody tr');
        let visibleCount = 0;
        
        rows.forEach(row => {{
            if (row.classList.contains('expandable-row')) {{
                const url = row.querySelector('.url-cell').textContent;
                const domain = extractDomain(url);
                
                if (selectedDomains.includes(domain)) {{
                    row.style.display = 'table-row';
                    visibleCount++;
                }} else {{
                    row.style.display = 'none';
                    // Also hide corresponding details row
                    const detailsId = row.getAttribute('data-target');
                    const detailsRow = document.getElementById(detailsId);
                    if (detailsRow) {{
                        detailsRow.style.display = 'none';
                    }}
                }}
            }}
        }});
        
        // Update filter button text
        const filterBtn = document.getElementById('domainFilterBtn');
        if (selectedDomains.length === availableDomains.length) {{
            filterBtn.innerHTML = '<i class="fas fa-filter"></i> Filter by Domain';
        }} else {{
            filterBtn.innerHTML = `<i class="fas fa-filter"></i> Filter by Domain (${{selectedDomains.length}} selected)`;
        }}
        
        // Show/hide no data message
        const noDataRow = document.querySelector('#changed .no-data-filtered');
        if (visibleCount === 0) {{
            if (!noDataRow) {{
                const tbody = document.querySelector('#changed tbody');
                const tr = document.createElement('tr');
                tr.className = 'no-data-filtered';
                tr.innerHTML = '<td colspan="7" class="no-data">No requests match the selected domain filter</td>';
                tbody.appendChild(tr);
            }}
        }} else if (noDataRow) {{
            noDataRow.remove();
        }}
    }}
    
    function extractDomain(url) {{
        try {{
            return new URL(url).hostname;
        }} catch {{
            return 'unknown';
        }}
    }}
    """

def generate_advanced_html(added: List, removed: List, changed: List, har_a: str, har_b: str) -> str:
    """
    Generate advanced HTML with domain filtering and GraphQL support.
    """
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    # Extract domains from changed requests
    domains = set()
    for req in changed:
        try:
            domain = urlparse(req['url']).hostname
            if domain:
                domains.add(domain)
        except:
            pass
    
    # Generate rows for each tab
    added_rows = '\n'.join([
        f'<tr data-domain="{urlparse(req["url"]).hostname or "unknown"}"><td><span class="method-badge method-{req["method"].lower()}">{html.escape(req["method"])}</span></td><td class="url-cell">{html.escape(req["url"])}</td><td><span class="status-badge status-{str(req["status"])[0]}xx">{req["status"]}</span></td></tr>'
        for req in added
    ]) if added else '<tr><td colspan="3" class="no-data">No added requests</td></tr>'
    
    removed_rows = '\n'.join([
        f'<tr data-domain="{urlparse(req["url"]).hostname or "unknown"}"><td><span class="method-badge method-{req["method"].lower()}">{html.escape(req["method"])}</span></td><td class="url-cell">{html.escape(req["url"])}</td><td><span class="status-badge status-{str(req["status"])[0]}xx">{req["status"]}</span></td></tr>'
        for req in removed
    ]) if removed else '<tr><td colspan="3" class="no-data">No removed requests</td></tr>'
    
    changed_rows = '\n'.join([
        f'''
        <tr class="expandable-row" data-target="details-{req["id"]}" data-domain="{urlparse(req["url"]).hostname or "unknown"}">
            <td><span class="method-badge method-{req["method"].lower()}">{html.escape(req["method"])}</span></td>
            <td class="url-cell">{html.escape(req.get("display_url", req["url"]))}</td>
            <td><span class="status-badge status-{str(req["status_a"])[0]}xx">{req["status_a"]}</span></td>
            <td><span class="status-badge status-{str(req["status_b"])[0]}xx">{req["status_b"]}</span></td>
            <td>{req["time_a"]:.1f}ms</td>
            <td>{req["time_b"]:.1f}ms</td>
            <td><i class="fas fa-chevron-down expand-icon"></i></td>
        </tr>
        <tr class="details-row" id="details-{req["id"]}" style="display: none;">
            <td colspan="7">
                <div class="details-container">
                    <div class="details-section">
                        <h4>Request Details</h4>
                        {req["request_details"]}
                    </div>
                    <div class="details-section">
                        <h4>Response Details</h4>
                        {req["response_details"]}
                    </div>
                    {req["graphql_details"]}
                </div>
            </td>
        </tr>
        '''
        for req in changed
    ]) if changed else '<tr><td colspan="7" class="no-data">No changed API/GraphQL requests</td></tr>'
    
    domain_filter_js = generate_domain_filter_js(list(domains))
    
    html_content = f'''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>HAR Comparison Report - Advanced</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <style>
        :root {{
            --primary-color: #2563eb;
            --secondary-color: #64748b;
            --success-color: #059669;
            --warning-color: #d97706;
            --danger-color: #dc2626;
            --light-bg: #f8fafc;
            --card-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
            --border-radius: 12px;
        }}
        
        body {{
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            margin: 0;
            padding: 20px 0;
        }}
        
        .main-container {{
            max-width: 1400px;
            margin: 0 auto;
            background: white;
            border-radius: var(--border-radius);
            box-shadow: var(--card-shadow);
            overflow: hidden;
        }}
        
        .header {{
            background: linear-gradient(135deg, var(--primary-color) 0%, #1e40af 100%);
            color: white;
            padding: 2rem;
            text-align: center;
        }}
        
        .header h1 {{
            margin: 0;
            font-size: 2.5rem;
            font-weight: 700;
            letter-spacing: -0.025em;
        }}
        
        .header p {{
            margin: 0.5rem 0 0 0;
            opacity: 0.9;
            font-size: 1.1rem;
        }}
        
        .file-info {{
            background: var(--light-bg);
            padding: 1.5rem 2rem;
            border-bottom: 1px solid #e2e8f0;
        }}
        
        .file-info-grid {{
            display: grid;
            grid-template-columns: 1fr 1fr 1fr;
            gap: 1rem;
            align-items: center;
        }}
        
        .file-item {{
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }}
        
        .file-item i {{
            color: var(--secondary-color);
        }}
        
        .tabs-container {{
            background: white;
        }}
        
        .nav-tabs {{
            border-bottom: 2px solid #e2e8f0;
            padding: 0 2rem;
        }}
        
        .nav-tabs .nav-link {{
            border: none;
            border-radius: 0;
            color: var(--secondary-color);
            font-weight: 600;
            padding: 1rem 1.5rem;
            margin-bottom: -2px;
            transition: all 0.3s ease;
        }}
        
        .nav-tabs .nav-link:hover {{
            border-color: transparent;
            color: var(--primary-color);
        }}
        
        .nav-tabs .nav-link.active {{
            color: var(--primary-color);
            border-bottom: 2px solid var(--primary-color);
            background: transparent;
        }}
        
        .tab-content {{
            padding: 2rem;
        }}
        
        .tab-controls {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1.5rem;
        }}
        
        .filter-controls {{
            display: flex;
            gap: 1rem;
        }}
        
        .table-container {{
            background: white;
            border-radius: var(--border-radius);
            box-shadow: var(--card-shadow);
            overflow: hidden;
        }}
        
        .table {{
            margin: 0;
        }}
        
        .table thead th {{
            background: var(--light-bg);
            border: none;
            font-weight: 700;
            color: var(--secondary-color);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            font-size: 0.875rem;
            padding: 1rem;
        }}
        
        .table tbody td {{
            border: none;
            padding: 1rem;
            vertical-align: middle;
            border-bottom: 1px solid #f1f5f9;
        }}
        
        .table tbody tr:hover {{
            background: var(--light-bg);
        }}
        
        .expandable-row {{
            cursor: pointer;
            transition: all 0.3s ease;
        }}
        
        .expandable-row:hover {{
            background: var(--light-bg) !important;
        }}
        
        .details-row {{
            background: #fefefe;
        }}
        
        .details-container {{
            padding: 1.5rem;
            background: var(--light-bg);
            border-radius: 8px;
            margin: 0.5rem;
            overflow-x: auto;
            max-width: 100%;
        }}
        
        .details-section {{
            margin-bottom: 2rem;
        }}
        
        .details-section:last-child {{
            margin-bottom: 0;
        }}
        
        .details-section h4 {{
            color: var(--primary-color);
            font-weight: 700;
            margin-bottom: 1rem;
            padding-bottom: 0.5rem;
            border-bottom: 2px solid #e2e8f0;
        }}
        
        .method-badge {{
            padding: 0.375rem 0.75rem;
            border-radius: 6px;
            font-size: 0.75rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}
        
        .method-get {{ background: #dcfce7; color: #166534; }}
        .method-post {{ background: #fef3c7; color: #92400e; }}
        .method-put {{ background: #dbeafe; color: #1e40af; }}
        .method-delete {{ background: #fecaca; color: #991b1b; }}
        .method-patch {{ background: #e0e7ff; color: #3730a3; }}
        
        .status-badge {{
            padding: 0.375rem 0.75rem;
            border-radius: 6px;
            font-size: 0.75rem;
            font-weight: 700;
            min-width: 60px;
            text-align: center;
        }}
        
        .status-2xx {{ background: #dcfce7; color: #166534; }}
        .status-3xx {{ background: #fef3c7; color: #92400e; }}
        .status-4xx {{ background: #fecaca; color: #991b1b; }}
        .status-5xx {{ background: #fde2e8; color: #be185d; }}
        
        .url-cell {{
            font-family: 'JetBrains Mono', 'Fira Code', monospace;
            font-size: 0.875rem;
            max-width: 400px;
            word-break: break-all;
        }}
        
        .expand-icon {{
            transition: transform 0.3s ease;
            color: var(--secondary-color);
        }}
        
        .expanded .expand-icon {{
            transform: rotate(180deg);
        }}
        
        .diff-section {{
            margin-bottom: 1.5rem;
            border-radius: 8px;
            overflow: hidden;
            border: 1px solid #e2e8f0;
        }}
        
        .diff-title {{
            background: var(--light-bg);
            padding: 0.75rem 1rem;
            margin: 0;
            font-weight: 600;
            color: var(--secondary-color);
            border-bottom: 1px solid #e2e8f0;
        }}
        
        .added-headers, .removed-headers, .changed-headers {{
            padding: 1rem;
        }}
        
        .added-headers {{
            background: #f0fdf4;
            border-left: 4px solid var(--success-color);
        }}
        
        .removed-headers {{
            background: #fef2f2;
            border-left: 4px solid var(--danger-color);
        }}
        
        .changed-headers {{
            background: #eff6ff;
            border-left: 4px solid var(--primary-color);
        }}
        
        .added-headers h5 {{ color: var(--success-color); }}
        .removed-headers h5 {{ color: var(--danger-color); }}
        .changed-headers h5 {{ color: var(--primary-color); }}
        
        .graphql-section {{
            margin-top: 2rem;
            padding: 1.5rem;
            background: #f8fafc;
            border-radius: 8px;
            border: 1px solid #e2e8f0;
            overflow-x: auto;
        }}
        
        .graphql-section h5 {{
            color: #7c3aed;
            font-weight: 700;
            margin-bottom: 1rem;
        }}
        
        .graphql-operation, .graphql-query, .graphql-variables {{
            margin-bottom: 1rem;
        }}
        
        .graphql-operation h6, .graphql-query h6, .graphql-variables h6 {{
            color: var(--secondary-color);
            font-weight: 600;
            margin-bottom: 0.5rem;
        }}
        
        .query-diff, .variables-diff {{
            display: grid;
            grid-template-columns: 1fr;
            gap: 1rem;
        }}
        
        .old-query, .new-query, .old-variables, .new-variables {{
            padding: 1rem;
            border-radius: 6px;
            overflow-x: auto;
            margin-bottom: 0.75rem;
        }}
        
        .old-query strong, .new-query strong, .old-variables strong, .new-variables strong {{
            display: block;
            margin-bottom: 0.5rem;
            font-size: 0.9rem;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        
        .graphql-section pre {{
            overflow-x: auto;
            white-space: pre;
            word-wrap: normal;
            max-width: 100%;
        }}
        
        .old-query, .old-variables {{
            background: #fef2f2;
            border-left: 4px solid var(--danger-color);
        }}
        
        .new-query, .new-variables {{
            background: #f0fdf4;
            border-left: 4px solid var(--success-color);
        }}
        
        .same-query, .same-variables {{
            background: #f8fafc;
            padding: 1rem;
            border-radius: 6px;
            border: 1px solid #e2e8f0;
        }}
        
        .diff-line {{
            font-family: 'JetBrains Mono', monospace;
        }}
        
        .old-value {{
            color: var(--danger-color);
            text-decoration: line-through;
        }}
        
        .new-value {{
            color: var(--success-color);
            font-weight: 600;
        }}
        
        .same-value {{
            color: var(--secondary-color);
        }}
        
        .no-changes {{
            text-align: center;
            padding: 2rem;
            color: var(--secondary-color);
            font-style: italic;
        }}
        
        .no-data {{
            text-align: center;
            padding: 3rem;
            color: var(--secondary-color);
            font-style: italic;
        }}
        
        .summary-cards {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 1rem;
            margin-bottom: 2rem;
        }}
        
        .summary-card {{
            background: white;
            padding: 1.5rem;
            border-radius: var(--border-radius);
            box-shadow: var(--card-shadow);
            text-align: center;
        }}
        
        .summary-card h3 {{
            font-size: 2rem;
            font-weight: 700;
            margin: 0;
        }}
        
        .summary-card p {{
            margin: 0.5rem 0 0 0;
            color: var(--secondary-color);
            font-weight: 500;
        }}
        
        .card-added h3 {{ color: var(--success-color); }}
        .card-removed h3 {{ color: var(--danger-color); }}
        .card-changed h3 {{ color: var(--primary-color); }}
        
        .domain-filter-modal .modal-dialog {{
            max-width: 600px;
            width: 90vw;
        }}
        
        .domain-filter-modal .modal-body {{
            max-height: 500px;
            overflow-y: auto;
            padding: 1.5rem;
        }}
        
        .domain-filter-modal .modal-header {{
            background: var(--light-bg);
            border-bottom: 2px solid #e2e8f0;
        }}
        
        .domain-filter-modal .modal-title {{
            color: var(--primary-color);
            font-weight: 700;
        }}
        
        .filter-controls {{
            display: flex;
            align-items: center;
            gap: 1rem;
            margin-bottom: 1rem;
            flex-wrap: wrap;
        }}
        
        .search-filter {{
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }}
        
        .domain-checkboxes {{
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
            max-height: 350px;
            overflow-y: auto;
            padding: 1rem;
            border: 1px solid #e2e8f0;
            border-radius: 8px;
            background: #fafafa;
            margin: 0;
        }}
        
        .domain-checkbox-item {{
            display: flex;
            align-items: center;
            padding: 0.75rem;
            background: white;
            border: 1px solid #e2e8f0;
            border-radius: 6px;
            transition: all 0.2s ease;
            cursor: pointer;
            min-height: 50px;
            width: 100%;
            box-sizing: border-box;
        }}
        
        .domain-checkbox-item:hover {{
            background: var(--light-bg);
            border-color: var(--primary-color);
            transform: translateY(-1px);
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        
        .domain-checkbox-item input[type="checkbox"] {{
            width: 18px;
            height: 18px;
            margin-right: 0.75rem;
            accent-color: var(--primary-color);
            cursor: pointer;
            flex-shrink: 0;
        }}
        
        .domain-checkbox-item label {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.9rem;
            color: var(--secondary-color);
            cursor: pointer;
            margin: 0;
            flex: 1;
            word-break: break-all;
            line-height: 1.4;
        }}
        
        .domain-checkbox-item.checked {{
            background: #f0f9ff;
            border-color: var(--primary-color);
        }}
        
        .domain-checkbox-item.checked label {{
            color: var(--primary-color);
            font-weight: 600;
        }}
        
        .modal-footer {{
            background: var(--light-bg);
            border-top: 2px solid #e2e8f0;
            padding: 1rem 1.5rem;
        }}
        
        .modal-footer .btn {{
            padding: 0.75rem 1.5rem;
            font-weight: 600;
            border-radius: 8px;
            transition: all 0.3s ease;
        }}
        
        .modal-footer .btn:hover {{
            transform: translateY(-1px);
            box-shadow: 0 4px 8px rgba(0,0,0,0.15);
        }}}}
        
        @media (max-width: 768px) {{
            .file-info-grid {{
                grid-template-columns: 1fr;
            }}
            
            .main-container {{
                margin: 0 10px;
            }}
            
            .header, .tab-content {{
                padding: 1rem;
            }}
            
            /* Vertical layout is now default for all screen sizes */
        }}
    </style>
</head>
<body>
    <div class="main-container">
        <div class="header">
            <h1><i class="fas fa-chart-line"></i> HAR Comparison Report</h1>
            <p>Advanced Analysis with Domain Filtering and GraphQL Support</p>
        </div>
        
        <div class="file-info">
            <div class="file-info-grid">
                <div class="file-item">
                    <i class="fas fa-clock"></i>
                    <span><strong>Generated:</strong> {timestamp}</span>
                </div>
                <div class="file-item">
                    <i class="fas fa-file-alt"></i>
                    <span><strong>Old HAR:</strong> {html.escape(os.path.basename(har_a))}</span>
                </div>
                <div class="file-item">
                    <i class="fas fa-file-alt"></i>
                    <span><strong>New HAR:</strong> {html.escape(os.path.basename(har_b))}</span>
                </div>
            </div>
        </div>
        
        <div class="summary-cards" style="padding: 2rem;">
            <div class="summary-card card-added">
                <h3>{len(added)}</h3>
                <p>Added Requests</p>
            </div>
            <div class="summary-card card-removed">
                <h3>{len(removed)}</h3>
                <p>Removed Requests</p>
            </div>
            <div class="summary-card card-changed">
                <h3>{len(changed)}</h3>
                <p>Changed API/GraphQL</p>
            </div>
        </div>
        
        <div class="tabs-container">
            <ul class="nav nav-tabs" id="comparisonTabs" role="tablist">
                <li class="nav-item" role="presentation">
                    <button class="nav-link active" id="added-tab" data-bs-toggle="tab" data-bs-target="#added" type="button" role="tab">
                        <i class="fas fa-plus-circle"></i> Added ({len(added)})
                    </button>
                </li>
                <li class="nav-item" role="presentation">
                    <button class="nav-link" id="removed-tab" data-bs-toggle="tab" data-bs-target="#removed" type="button" role="tab">
                        <i class="fas fa-minus-circle"></i> Removed ({len(removed)})
                    </button>
                </li>
                <li class="nav-item" role="presentation">
                    <button class="nav-link" id="changed-tab" data-bs-toggle="tab" data-bs-target="#changed" type="button" role="tab">
                        <i class="fas fa-exchange-alt"></i> Changed ({len(changed)})
                    </button>
                </li>
            </ul>
            
            <div class="tab-content" id="comparisonTabContent">
                <div class="tab-pane fade show active" id="added" role="tabpanel">
                    <div class="table-container">
                        <table class="table">
                            <thead>
                                <tr>
                                    <th>Method</th>
                                    <th>URL</th>
                                    <th>Status</th>
                                </tr>
                            </thead>
                            <tbody>
                                {added_rows}
                            </tbody>
                        </table>
                    </div>
                </div>
                
                <div class="tab-pane fade" id="removed" role="tabpanel">
                    <div class="table-container">
                        <table class="table">
                            <thead>
                                <tr>
                                    <th>Method</th>
                                    <th>URL</th>
                                    <th>Status</th>
                                </tr>
                            </thead>
                            <tbody>
                                {removed_rows}
                            </tbody>
                        </table>
                    </div>
                </div>
                
                <div class="tab-pane fade" id="changed" role="tabpanel">
                    <div class="tab-controls">
                        <h4>Changed API/GraphQL Requests</h4>
                        <div class="filter-controls">
                            <div class="search-filter">
                                <input type="text" class="form-control" id="requestSearchInput" placeholder="Search requests by name..." style="width: 300px; display: inline-block; margin-right: 10px;">
                                <button type="button" class="btn btn-outline-secondary" id="clearSearchBtn">
                                    <i class="fas fa-times"></i> Clear
                                </button>
                            </div>
                            <button type="button" class="btn btn-outline-primary" id="domainFilterBtn" data-bs-toggle="modal" data-bs-target="#domainFilterModal">
                                <i class="fas fa-filter"></i> Filter by Domain
                            </button>
                        </div>
                    </div>
                    <div class="table-container">
                        <table class="table">
                            <thead>
                                <tr>
                                    <th>Method</th>
                                    <th>URL</th>
                                    <th>Status (Old)</th>
                                    <th>Status (New)</th>
                                    <th>Time (Old)</th>
                                    <th>Time (New)</th>
                                    <th>Details</th>
                                </tr>
                            </thead>
                            <tbody>
                                {changed_rows}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>
    </div>
    
    <!-- Domain Filter Modal -->
    <div class="modal fade domain-filter-modal" id="domainFilterModal" tabindex="-1">
        <div class="modal-dialog">
            <div class="modal-content">
                <div class="modal-header">
                    <h5 class="modal-title"><i class="fas fa-filter"></i> Filter by Domain</h5>
                    <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body">
                    <p class="text-muted mb-3">Select domains to display in the Changed tab:</p>
                    <div id="domainCheckboxes" class="domain-checkboxes"></div>
                </div>
                <div class="modal-footer">
                    <button type="button" class="btn btn-secondary" id="clearDomainFilter">
                        <i class="fas fa-times"></i> Clear Filter
                    </button>
                    <button type="button" class="btn btn-primary" id="applyDomainFilter">
                        <i class="fas fa-check"></i> Apply Filter
                    </button>
                </div>
            </div>
        </div>
    </div>
    
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        {domain_filter_js}
        
        document.addEventListener('DOMContentLoaded', function() {{
            // Initialize domain filter
            initDomainFilter();

            // Handle text search filtering
            const searchInput = document.getElementById('requestSearchInput');
            const clearSearchBtn = document.getElementById('clearSearchBtn');
            
            function filterRequestsBySearch() {{
                const searchTerm = searchInput.value.toLowerCase();
                const changedTab = document.getElementById('changed');
                const rows = changedTab.querySelectorAll('.expandable-row');
                
                rows.forEach(row => {{
                    const urlCell = row.querySelector('.url-cell');
                    const urlText = urlCell.textContent.toLowerCase();
                    const detailsRowId = row.getAttribute('data-target');
                    const detailsRow = document.getElementById(detailsRowId);
                    
                    if (urlText.includes(searchTerm)) {{
                        row.style.display = '';
                        // Keep details row hidden unless it was expanded
                        if (!row.classList.contains('expanded')) {{
                            detailsRow.style.display = 'none';
                        }}
                    }} else {{
                        row.style.display = 'none';
                        detailsRow.style.display = 'none';
                        row.classList.remove('expanded');
                        const icon = row.querySelector('.expand-icon');
                        if (icon) {{
                            icon.style.transform = 'rotate(0deg)';
                        }}
                    }}
                }});
            }}
            
            if (searchInput) {{
                searchInput.addEventListener('input', filterRequestsBySearch);
            }}
            
            if (clearSearchBtn) {{
                clearSearchBtn.addEventListener('click', function() {{
                    searchInput.value = '';
                    filterRequestsBySearch();
                }});
            }}
            
            // Handle expandable rows
            document.querySelectorAll('.expandable-row').forEach(row => {{
                row.addEventListener('click', function() {{
                    const targetId = this.getAttribute('data-target');
                    const detailsRow = document.getElementById(targetId);
                    const icon = this.querySelector('.expand-icon');
                    
                    if (detailsRow.style.display === 'none') {{
                        detailsRow.style.display = 'table-row';
                        icon.style.transform = 'rotate(180deg)';
                        this.classList.add('expanded');
                    }} else {{
                        detailsRow.style.display = 'none';
                        icon.style.transform = 'rotate(0deg)';
                        this.classList.remove('expanded');
                    }}
                }});
            }});
        }});
    </script>
</body>
</html>
    '''
    
    return html_content

def build_advanced_report(har_a: str, har_b: str, out_html: str, db_path: str | None = None) -> None:
    db_path = db_path or ':memory:'
    conn = init_db(db_path)
    
    ingest_har(conn, 'A', har_a)
    ingest_har(conn, 'B', har_b)
    
    dfA, req_headers_A, res_headers_A = df_from_sql(conn, 'A')
    dfB, req_headers_B, res_headers_B = df_from_sql(conn, 'B')
    
    matched = match_requests(dfA, dfB)
    
    added = [
        {'method': r['method'], 'url': r['url'], 'status': r['status_b']}
        for _, r in matched[matched['_merge'] == 'right_only'].iterrows()
    ]
    removed = [
        {'method': r['method'], 'url': r['url'], 'status': r['status_a']}
        for _, r in matched[matched['_merge'] == 'left_only'].iterrows()
    ]
    
    changed_rows = matched[matched['_merge'] == 'both']
    changed_list = []
    
    for idx, r in changed_rows.iterrows():
        url = r['url']
        mime_type = r.get('mime_a') or r.get('mime_b')
        
        # Only include API and GraphQL requests in changed tab
        if not is_api_or_graphql(url, mime_type):
            continue
        
        id_a = r.get('id_a')
        id_b = r.get('id_b')
        
        # Get header differences
        req_headers_diff = dict_diff(
            req_headers_A.get(id_a, {}),
            req_headers_B.get(id_b, {})
        )
        res_headers_diff = dict_diff(
            res_headers_A.get(id_a, {}),
            res_headers_B.get(id_b, {})
        )
        
        # Check if there are any changes
        has_changes = (
            r['status_a'] != r['status_b'] or
            abs(r['time_a'] - r['time_b']) > 100 or
            any(req_headers_diff.values()) or
            any(res_headers_diff.values()) or
            r.get('graphql_query_a') != r.get('graphql_query_b') or
            r.get('graphql_variables_a') != r.get('graphql_variables_b')
        )
        
        if has_changes:
            request_details = render_header_diff(req_headers_diff, 'Request Headers')
            response_details = render_header_diff(res_headers_diff, 'Response Headers')
            
            # Add GraphQL details if available
            graphql_details = render_graphql_details(
                r.get('graphql_query_a'), r.get('graphql_variables_a'), r.get('graphql_operation_name_a'),
                r.get('graphql_query_b'), r.get('graphql_variables_b'), r.get('graphql_operation_name_b')
            )
            
            # Determine display URL with GraphQL operation name if available
            display_url = r['url']
            operation_name = r.get('graphql_operation_name_a') or r.get('graphql_operation_name_b')
            if operation_name and '/graphql' in r['url'].lower():
                display_url = f"{r['url']} ({operation_name})"
            
            changed_list.append({
                'id': len(changed_list) + 1,
                'method': r['method'],
                'url': r['url'],
                'display_url': display_url,
                'operation_name': operation_name,
                'status_a': r['status_a'],
                'status_b': r['status_b'],
                'time_a': r['time_a'],
                'time_b': r['time_b'],
                'request_details': request_details,
                'response_details': response_details,
                'graphql_details': graphql_details
            })
    
    html_content = generate_advanced_html(added, removed, changed_list, har_a, har_b)
    
    with open(out_html, 'w', encoding='utf-8') as f:
        f.write(html_content)
    
    conn.close()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Advanced HAR comparison with domain filtering and GraphQL support.')
    parser.add_argument('har_a', help='Path to the old HAR file')
    parser.add_argument('har_b', help='Path to the new HAR file')
    parser.add_argument('-o', '--output', default='compare_advanced.html', help='Output HTML file path (default: compare_advanced.html)')
    parser.add_argument('--db', help='SQLite database path (default: in-memory)')
    
    args = parser.parse_args()
    
    try:
        build_advanced_report(args.har_a, args.har_b, args.output, args.db)
        print(f"Advanced HTML report generated: {args.output}")
        print("\nFeatures:")
        print("- Tabbed interface (Added/Removed/Changed)")
        print("- Domain filtering for Changed tab")
        print("- GraphQL query and variables display")
        print("- Premium design with clean typography")
        print("- Expandable details for changed requests")
        print("- Header change highlighting")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)