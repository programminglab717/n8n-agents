#!/usr/bin/env python3
"""Build the importable n8n workflow JSON files from the prompt files.

Edit the prompts in ../prompts/ (or the settings below), then run:

    python3 scripts/build_workflows.py

and re-import the files in ../workflows/ into n8n.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "prompts"
OUT = ROOT / "workflows"

# ---------------------------------------------------------------------------
# Settings that end up inside the workflows. Everything here can also be
# changed later inside n8n.
# ---------------------------------------------------------------------------
STORE_DOMAIN = "https://hairqueenla.com"  # Shopify storefront (custom domain or *.myshopify.com)
MCP_ENDPOINT = "https://REPLACE-WITH-YOUR-MCP-HOST/mcp"  # ReachInbox MCP, Streamable HTTP
REACHINBOX_API = "https://api.reachinbox.ai"
PINECONE_INDEX = "hairqueenla-kb"
PINECONE_NAMESPACE = "brand"
REPLY_MODEL = "claude-sonnet-5-5"
TRIAGE_MODEL = "claude-haiku-5-5"
EMBEDDING_MODEL = "text-embedding-3-small"  # must match the Pinecone index dimension (1536)

# Read-only ReachInbox tools the agents may call. Sending is done by a plain
# HTTP node after validation, never by the model.
READ_TOOLS = ["list_onebox_threads", "get_onebox_thread"]

CRED = {
    "anthropic": {"anthropicApi": {"id": "", "name": "Anthropic account"}},
    "mcp": {"httpBearerAuth": {"id": "", "name": "ReachInbox MCP gateway key"}},
    "reachinbox": {"httpBearerAuth": {"id": "", "name": "ReachInbox API key"}},
    "pinecone": {"pineconeApi": {"id": "", "name": "Pinecone account"}},
    "openai": {"openAiApi": {"id": "", "name": "OpenAI account (embeddings)"}},
    "sheets": {"googleSheetsOAuth2Api": {"id": "", "name": "Google Sheets account"}},
    "drive": {"googleDriveOAuth2Api": {"id": "", "name": "Google Drive account"}},
}


def nid(workflow: str, name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"hairqueenla/{workflow}/{name}"))


def prompt(name: str) -> str:
    return "=" + (PROMPTS / name).read_text(encoding="utf-8").strip() + "\n"


class Workflow:
    def __init__(self, key: str, name: str):
        self.key = key
        self.name = name
        self.nodes: list[dict] = []
        self.connections: dict = {}

    def node(self, name, type_, version, pos, params, creds=None, **extra):
        n = {
            "parameters": params,
            "id": nid(self.key, name),
            "name": name,
            "type": type_,
            "typeVersion": version,
            "position": list(pos),
        }
        if creds:
            n["credentials"] = creds
        n.update(extra)
        self.nodes.append(n)
        return name

    def link(self, src, dst, kind="main", src_index=0, dst_index=0):
        outs = self.connections.setdefault(src, {}).setdefault(kind, [])
        while len(outs) <= src_index:
            outs.append([])
        outs[src_index].append({"node": dst, "type": kind, "index": dst_index})

    def sticky(self, name, pos, content, width=400, height=300, color=None):
        params = {"content": content, "height": height, "width": width}
        if color:
            params["color"] = color
        self.node(name, "n8n-nodes-base.stickyNote", 1, pos, params)

    def dump(self, filename):
        data = {
            "name": self.name,
            "nodes": self.nodes,
            "connections": self.connections,
            "settings": {"executionOrder": "v1", "saveManualExecutions": True},
            "pinData": {},
            "active": False,
            "tags": [],
        }
        path = OUT / filename
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")


# ---------------------------------------------------------------------------
# Shared sub-node builders
# ---------------------------------------------------------------------------

def anthropic_model(wf, name, pos, model, max_tokens):
    return wf.node(
        name, "@n8n/n8n-nodes-langchain.lmChatAnthropic", 1.6, pos,
        {
            "model": {"__rl": True, "mode": "id", "value": model},
            "options": {"maxTokensToSample": max_tokens, "temperature": 0.3},
        },
        CRED["anthropic"],
    )


def mcp_tool(wf, name, pos):
    return wf.node(
        name, "@n8n/n8n-nodes-langchain.mcpClientTool", 1.2, pos,
        {
            "endpointUrl": MCP_ENDPOINT,
            "serverTransport": "httpStreamable",
            "authentication": "bearerAuth",
            "include": "selected",
            "includeTools": READ_TOOLS,
            "options": {"timeout": 60000},
        },
        CRED["mcp"],
    )


def openai_embeddings(wf, name, pos):
    return wf.node(
        name, "@n8n/n8n-nodes-langchain.embeddingsOpenAi", 1.2, pos,
        {"model": EMBEDDING_MODEL, "options": {}},
        CRED["openai"],
    )


def pinecone_index_param():
    return {"__rl": True, "mode": "id", "value": PINECONE_INDEX}


# ---------------------------------------------------------------------------
# Workflow 1: the reply agent
# ---------------------------------------------------------------------------

VALIDATE_JS = r"""
// Parses the reply agent's JSON, validates it, de-duplicates, and decides
// what happens next: send | draft | escalate | skip | duplicate | error.
const cfg = $('Config').first().json;
const mode = String(cfg.mode || 'draft').toLowerCase() === 'send' ? 'send' : 'draft';
const store = $getWorkflowStaticData('global');
store.handled = store.handled || {};

// Forget entries older than 30 days so static data stays small.
const cutoff = Date.now() - 30 * 24 * 3600 * 1000;
for (const [k, t] of Object.entries(store.handled)) if (t < cutoff) delete store.handled[k];

function parse(raw) {
  if (raw && typeof raw === 'object') return raw;
  const text = String(raw ?? '').replace(/```(?:json)?/gi, '');
  const start = text.indexOf('{');
  const end = text.lastIndexOf('}');
  if (start === -1 || end <= start) throw new Error('no JSON object in agent output');
  return JSON.parse(text.slice(start, end + 1));
}

const isEmail = (s) => /^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$/.test(String(s || '').trim());
const results = [];

for (const item of $input.all()) {
  const source = $('Task from webhook').isExecuted ? 'webhook' : 'sweep';
  let r;
  try {
    r = parse(item.json.output);
  } catch (e) {
    results.push({ json: { status: 'error', error: `Could not parse agent output: ${e.message}`, raw_output: String(item.json.output ?? '').slice(0, 2000), source } });
    continue;
  }

  const decision = String(r.decision || '').toLowerCase();
  const refs = Array.isArray(r.references) ? r.references.filter(Boolean).map(String)
    : String(r.references || '').split(/\s+/).filter(Boolean);
  const out = {
    ...r,
    decision,
    references: refs,
    lead_email: String(r.lead_email || '').trim(),
    account: String(r.account || '').trim(),
    source,
    error: '',
  };

  let status = decision;
  if (decision === 'reply') {
    const problems = [];
    if (!isEmail(out.lead_email)) problems.push('lead_email');
    if (!isEmail(out.account)) problems.push('account');
    if (!out.in_reply_to) problems.push('in_reply_to');
    if (!out.original_message_id) problems.push('original_message_id');
    if (String(out.reply_html || '').replace(/<[^>]+>/g, '').trim().length < 20) problems.push('reply_html');
    if (/\{\{|REPLACE|lorem ipsum/i.test(String(out.reply_html || ''))) problems.push('reply_html contains placeholder text');
    if (out.lead_email.toLowerCase() === out.account.toLowerCase()) problems.push('lead_email equals our account');

    if (problems.length) {
      status = 'error';
      out.error = `Reply blocked, missing or invalid: ${problems.join(', ')}`;
    } else {
      const key = String(out.original_message_id || `${out.thread_id}:${out.in_reply_to}`);
      const draftKey = `draft:${key}`;
      if (store.handled[key] || (mode === 'draft' && store.handled[draftKey])) {
        status = 'duplicate';
      } else if (mode === 'send') {
        status = 'send';
        store.handled[key] = Date.now();
      } else {
        status = 'draft';
        store.handled[draftKey] = Date.now();
      }
      out.dedupe_key = key;
    }
  } else if (!['skip', 'escalate'].includes(decision)) {
    status = 'error';
    out.error = `Unknown decision "${decision}"`;
  }

  out.status = status;
  out.subject = out.subject || 'Re: your message';
  out.reply_text = String(out.reply_html || '').replace(/<\/p>/gi, '\n\n').replace(/<br\s*\/?>/gi, '\n')
    .replace(/<a [^>]*href="([^"]+)"[^>]*>(.*?)<\/a>/gi, '$2 ($1)').replace(/<[^>]+>/g, '').trim();
  out.emaildata = JSON.stringify({
    from: out.account,
    to: [out.lead_email],
    cc: [],
    bcc: [],
    subject: out.subject,
    body: out.reply_html,
    references: out.references,
    inReplyTo: out.in_reply_to,
    originalMessageId: String(out.original_message_id || ''),
  });
  results.push({ json: out });
}
return results;
""".strip()


PARSE_TRIAGE_JS = r"""
// Turns the triage agent's JSON into one item per thread.
const max = Number($('Config').first().json.max_threads_per_run) || 15;
const raw = String($json.output ?? '').replace(/```(?:json)?/gi, '');
const start = raw.indexOf('{');
const end = raw.lastIndexOf('}');
if (start === -1 || end <= start) throw new Error('Triage agent returned no JSON: ' + raw.slice(0, 500));
const threads = (JSON.parse(raw.slice(start, end + 1)).threads || [])
  .filter(t => t && t.thread_id && t.account)
  .slice(0, max);
return threads.map(t => ({ json: t }));
""".strip()


def build_reply_agent():
    wf = Workflow("reply-agent", "Hair Queen LA – ReachInbox Reply Agent")

    wf.sticky(
        "Note: Overview", (-1180, -520),
        "## Hair Queen LA · ReachInbox reply agent\n"
        "Two ways in:\n"
        "1. **ReachInbox webhook** (real time): set it to fire on *reply received*.\n"
        "2. **Schedule sweep** every 15 min: catches anything the webhook missed.\n\n"
        "The agents can only **read** ReachInbox (MCP, read-only tools). The reply is checked "
        "by *Validate & dedupe* and sent by a plain HTTP call, and only when `mode` in **Config** is `send`.\n\n"
        "Start with `mode = draft`, review the drafts in the Google Sheet, then switch to `send`.",
        width=520, height=300, color=4,
    )
    wf.sticky(
        "Note: Config", (-460, -520),
        "### Edit Config first\n"
        "- `mode`: `draft` (log only) or `send`\n"
        "- `booking_url`: salon booking link\n"
        "- `store_url`, `signature_name`\n"
        "- `lookback_hours`, `max_threads_per_run`",
        width=300, height=220, color=6,
    )

    wh = wf.node(
        "ReachInbox webhook", "n8n-nodes-base.webhook", 2, (-1100, -100),
        {
            "httpMethod": "POST",
            "path": "hairqueenla-reachinbox-reply-7f3c9a",
            "responseMode": "onReceived",
            "options": {},
        },
        webhookId=nid("reply-agent", "webhook-id"),
    )
    sched = wf.node(
        "Every 15 minutes", "n8n-nodes-base.scheduleTrigger", 1.2, (-1100, 140),
        {"rule": {"interval": [{"field": "minutes", "minutesInterval": 15}]}},
    )

    config = wf.node(
        "Config", "n8n-nodes-base.set", 3.4, (-860, 20),
        {
            "mode": "manual",
            "includeOtherFields": False,
            "assignments": {"assignments": [
                {"id": nid("reply-agent", "cfg-mode"), "name": "mode", "type": "string", "value": "draft"},
                {"id": nid("reply-agent", "cfg-brand"), "name": "brand_name", "type": "string", "value": "Hair Queen LA"},
                {"id": nid("reply-agent", "cfg-booking"), "name": "booking_url", "type": "string", "value": "https://REPLACE-WITH-YOUR-BOOKING-LINK"},
                {"id": nid("reply-agent", "cfg-store"), "name": "store_url", "type": "string", "value": STORE_DOMAIN},
                {"id": nid("reply-agent", "cfg-sig"), "name": "signature_name", "type": "string", "value": "The Hair Queen LA Team"},
                {"id": nid("reply-agent", "cfg-lookback"), "name": "lookback_hours", "type": "number", "value": 48},
                {"id": nid("reply-agent", "cfg-max"), "name": "max_threads_per_run", "type": "number", "value": 15},
            ]},
            "options": {},
        },
    )
    wf.link(wh, config)
    wf.link(sched, config)

    route = wf.node(
        "Started by webhook?", "n8n-nodes-base.if", 2.2, (-640, 20),
        {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
                "conditions": [{
                    "id": nid("reply-agent", "if-webhook"),
                    "leftValue": "={{ $('ReachInbox webhook').isExecuted }}",
                    "rightValue": "",
                    "operator": {"type": "boolean", "operation": "true", "singleValue": True},
                }],
                "combinator": "and",
            },
            "options": {},
        },
    )
    wf.link(config, route)

    # Webhook branch -------------------------------------------------------
    wh_task = wf.node(
        "Task from webhook", "n8n-nodes-base.set", 3.4, (-400, -120),
        {
            "mode": "manual",
            "includeOtherFields": False,
            "assignments": {"assignments": [
                {"id": nid("reply-agent", "wh-source"), "name": "source", "type": "string", "value": "webhook"},
                {"id": nid("reply-agent", "wh-task"), "name": "task", "type": "string",
                 "value": "=ReachInbox sent a webhook event. If it is not about a lead replying, return decision \"skip\". "
                          "Otherwise find the thread (use the account and thread id if the payload has them; otherwise "
                          "search list_onebox_threads with the lead's email), read the whole thread, and prepare our reply.\n\n"
                          "Webhook payload:\n{{ JSON.stringify($('ReachInbox webhook').first().json.body, null, 2) }}"},
            ]},
            "options": {},
        },
    )
    wf.link(route, wh_task, src_index=0)

    # Schedule branch ------------------------------------------------------
    triage = wf.node(
        "Triage agent (find threads to answer)", "@n8n/n8n-nodes-langchain.agent", 2.2, (-400, 200),
        {
            "promptType": "define",
            "text": "=Check the Onebox now and list every thread that is waiting for our reply. Return the JSON only.",
            "options": {"systemMessage": prompt("triage-agent.system.md"), "maxIterations": 40},
        },
    )
    wf.link(route, triage, src_index=1)
    wf.link(anthropic_model(wf, "Claude (triage)", (-480, 440), TRIAGE_MODEL, 2048), triage, "ai_languageModel")
    wf.link(mcp_tool(wf, "ReachInbox MCP (triage, read-only)", (-300, 440)), triage, "ai_tool")

    parse_triage = wf.node(
        "One item per thread", "n8n-nodes-base.code", 2, (-40, 200),
        {"jsCode": PARSE_TRIAGE_JS},
    )
    wf.link(triage, parse_triage)

    sweep_task = wf.node(
        "Task from sweep", "n8n-nodes-base.set", 3.4, (180, 200),
        {
            "mode": "manual",
            "includeOtherFields": False,
            "assignments": {"assignments": [
                {"id": nid("reply-agent", "sw-source"), "name": "source", "type": "string", "value": "sweep"},
                {"id": nid("reply-agent", "sw-task"), "name": "task", "type": "string",
                 "value": "=Prepare our reply for this ReachInbox thread.\n"
                          "account: {{ $json.account }}\nthread_id: {{ $json.thread_id }}\n"
                          "lead_email: {{ $json.lead_email }}\nnote from triage: {{ $json.note }}"},
            ]},
            "options": {},
        },
    )
    wf.link(parse_triage, sweep_task)

    # Reply agent ----------------------------------------------------------
    agent = wf.node(
        "Reply agent", "@n8n/n8n-nodes-langchain.agent", 2.2, (480, 20),
        {
            "promptType": "define",
            "text": "={{ $json.task }}",
            "options": {"systemMessage": prompt("reply-agent.system.md"), "maxIterations": 15},
        },
        onError="continueRegularOutput",
    )
    wf.link(wh_task, agent)
    wf.link(sweep_task, agent)
    wf.link(anthropic_model(wf, "Claude (replies)", (300, 300), REPLY_MODEL, 4096), agent, "ai_languageModel")
    wf.link(mcp_tool(wf, "ReachInbox MCP (read-only)", (440, 300)), agent, "ai_tool")

    kb = wf.node(
        "search_knowledge_base", "@n8n/n8n-nodes-langchain.vectorStorePinecone", 1.3, (580, 300),
        {
            "mode": "retrieve-as-tool",
            "toolName": "search_knowledge_base",
            "toolDescription": "Search the Hair Queen LA knowledge base: services, prices, policies, FAQs, "
                               "hours, location, stylists, wig and hair care. Input a short search query.",
            "pineconeIndex": pinecone_index_param(),
            "topK": 6,
            "includeDocumentMetadata": True,
            "options": {"pineconeNamespace": PINECONE_NAMESPACE},
        },
        CRED["pinecone"],
    )
    wf.link(kb, agent, "ai_tool")
    wf.link(openai_embeddings(wf, "Embeddings (OpenAI)", (580, 500)), kb, "ai_embedding")

    store_search = wf.node(
        "search_store", "@n8n/n8n-nodes-langchain.toolHttpRequest", 1.1, (740, 300),
        {
            "toolDescription": "Live search of the Hair Queen LA Shopify store. Use it to find products, "
                               "collections and pages with their URLs, prices and availability. "
                               "Input: q = a short search phrase such as 'body wave lace front wig' or 'edge control'.",
            "method": "GET",
            "url": f"{STORE_DOMAIN}/search/suggest.json",
            "sendQuery": True,
            "specifyQuery": "keypair",
            "parametersQuery": {"values": [
                {"name": "q", "valueProvider": "modelRequired"},
                {"name": "resources[type]", "valueProvider": "fieldValue", "value": "product,collection,page"},
                {"name": "resources[limit]", "valueProvider": "fieldValue", "value": "6"},
                {"name": "resources[options][unavailable_products]", "valueProvider": "fieldValue", "value": "last"},
            ]},
            "placeholderDefinitions": {"values": [
                {"name": "q", "description": "Short product search phrase", "type": "string"},
            ]},
        },
    )
    wf.link(store_search, agent, "ai_tool")

    product = wf.node(
        "get_product_details", "@n8n/n8n-nodes-langchain.toolHttpRequest", 1.1, (900, 300),
        {
            "toolDescription": "Get full details of one product from the Hair Queen LA store: description, "
                               "variants (length, density, colour), prices and availability. Input: handle = the "
                               "product handle, the last part of a product URL from search_store (e.g. 'body-wave-wig').",
            "method": "GET",
            "url": f"{STORE_DOMAIN}/products/{{handle}}.js",
            "placeholderDefinitions": {"values": [
                {"name": "handle", "description": "Shopify product handle", "type": "string"},
            ]},
        },
    )
    wf.link(product, agent, "ai_tool")

    # Validation, routing, sending, logging -------------------------------
    validate = wf.node(
        "Validate & dedupe", "n8n-nodes-base.code", 2, (840, 20),
        {"jsCode": VALIDATE_JS},
    )
    wf.link(agent, validate)

    def rule(key, value):
        return {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
                "conditions": [{
                    "id": nid("reply-agent", f"sw-{key}"),
                    "leftValue": "={{ $json.status }}",
                    "rightValue": value,
                    "operator": {"type": "string", "operation": "equals"},
                }],
                "combinator": "and",
            },
            "renameOutput": True,
            "outputKey": key,
        }

    switch = wf.node(
        "Route by status", "n8n-nodes-base.switch", 3.2, (1060, 20),
        {
            "rules": {"values": [rule("send", "send")]},
            "options": {"fallbackOutput": "extra", "renameFallbackOutput": "log only"},
        },
    )
    wf.link(validate, switch)

    send = wf.node(
        "Send reply via ReachInbox", "n8n-nodes-base.httpRequest", 4.2, (1300, -100),
        {
            "method": "POST",
            "url": f"{REACHINBOX_API}/api/v1/onebox/send",
            "authentication": "genericCredentialType",
            "genericAuthType": "httpBearerAuth",
            "sendBody": True,
            "contentType": "multipart-form-data",
            "bodyParameters": {"parameters": [
                {"parameterType": "formData", "name": "emaildata", "value": "={{ $json.emaildata }}"},
            ]},
            "options": {"response": {"response": {"fullResponse": True, "neverError": True}}},
        },
        CRED["reachinbox"],
    )
    wf.link(switch, send, src_index=0)

    def log_row(name, pos, status_expr, http_expr):
        v = "$('Validate & dedupe').item.json"
        fields = [
            ("logged_at", "={{ $now.setZone('America/Los_Angeles').toISO() }}"),
            ("status", status_expr),
            ("source", f"={{{{ {v}.source }}}}"),
            ("decision", f"={{{{ {v}.decision }}}}"),
            ("category", f"={{{{ {v}.category }}}}"),
            ("lead_email", f"={{{{ {v}.lead_email }}}}"),
            ("lead_name", f"={{{{ {v}.lead_name }}}}"),
            ("account", f"={{{{ {v}.account }}}}"),
            ("thread_id", f"={{{{ {v}.thread_id }}}}"),
            ("subject", f"={{{{ {v}.subject }}}}"),
            ("reply_text", f"={{{{ {v}.reply_text }}}}"),
            ("reason", f"={{{{ {v}.reason }}}}"),
            ("summary_for_team", f"={{{{ {v}.summary_for_team }}}}"),
            ("error", f"={{{{ {v}.error }}}}"),
            ("http_status", http_expr),
        ]
        return wf.node(
            name, "n8n-nodes-base.set", 3.4, pos,
            {
                "mode": "manual",
                "includeOtherFields": False,
                "assignments": {"assignments": [
                    {"id": nid("reply-agent", f"{name}-{k}"), "name": k, "type": "string", "value": val}
                    for k, val in fields
                ]},
                "options": {},
            },
        )

    sent_row = log_row(
        "Log row (sent)", (1520, -100),
        "={{ $json.statusCode >= 200 && $json.statusCode < 300 ? 'sent' : 'send_failed' }}",
        "={{ $json.statusCode }} {{ $json.statusCode >= 300 ? JSON.stringify($json.body).slice(0, 300) : '' }}",
    )
    other_row = log_row("Log row (draft/skip/escalate)", (1300, 140), "={{ $json.status }}", "")
    wf.link(send, sent_row)
    wf.link(switch, other_row, src_index=1)

    sheet = wf.node(
        "Append to Reply Log sheet", "n8n-nodes-base.googleSheets", 4.5, (1760, 20),
        {
            "operation": "append",
            "documentId": {"__rl": True, "mode": "url", "value": "https://docs.google.com/spreadsheets/d/REPLACE-WITH-YOUR-SHEET-ID/edit"},
            "sheetName": {"__rl": True, "mode": "name", "value": "Reply Log"},
            "columns": {"mappingMode": "autoMapInputData", "value": {}, "matchingColumns": [], "schema": []},
            "options": {},
        },
        CRED["sheets"],
    )
    wf.link(sent_row, sheet)
    wf.link(other_row, sheet)

    wf.dump("hairqueenla-reachinbox-reply-agent.json")


# ---------------------------------------------------------------------------
# Workflow 2: knowledge base ingestion into Pinecone
# ---------------------------------------------------------------------------

def build_ingestion():
    wf = Workflow("kb-ingest", "Hair Queen LA – Knowledge Base → Pinecone")

    wf.sticky(
        "Note: Overview", (-700, -440),
        "## Load the knowledge base into Pinecone\n"
        "Run manually whenever the knowledge base changes.\n\n"
        "**A. Google Drive folder**: put your GHL exports (table CSVs, uploaded files, FAQ doc) in one folder. "
        "Google Docs are exported as text and Sheets as CSV.\n\n"
        "**B. Website pages**: list the pages your GHL bot crawled (FAQ, services, policies, about…).\n\n"
        "To rebuild from scratch, set *Clear Namespace* on the first Pinecone node, run, then switch it off.\n"
        "Products are **not** indexed: the agent searches the live Shopify store instead.",
        width=560, height=330, color=4,
    )

    trigger = wf.node("Run ingestion", "n8n-nodes-base.manualTrigger", 1, (-660, 0), {})

    # A: Google Drive -------------------------------------------------------
    list_files = wf.node(
        "List files in KB folder", "n8n-nodes-base.googleDrive", 3, (-420, -100),
        {
            "resource": "fileFolder",
            "returnAll": True,
            "filter": {
                "folderId": {"__rl": True, "mode": "url", "value": "https://drive.google.com/drive/folders/REPLACE-WITH-FOLDER-ID"},
                "whatToSearch": "files",
            },
            "options": {"fields": ["id", "name", "mimeType"]},
        },
        CRED["drive"],
    )
    wf.link(trigger, list_files)

    download = wf.node(
        "Download file", "n8n-nodes-base.googleDrive", 3, (-200, -100),
        {
            "operation": "download",
            "fileId": {"__rl": True, "mode": "id", "value": "={{ $json.id }}"},
            "options": {
                "googleFileConversion": {"conversion": {
                    "docsToFormat": "text/plain",
                    "sheetsToFormat": "text/csv",
                    "slidesToFormat": "application/pdf",
                }},
            },
        },
        CRED["drive"],
    )
    wf.link(list_files, download)

    insert_files = wf.node(
        "Pinecone: insert files", "@n8n/n8n-nodes-langchain.vectorStorePinecone", 1.3, (60, -100),
        {
            "mode": "insert",
            "pineconeIndex": pinecone_index_param(),
            "embeddingBatchSize": 100,
            "options": {"pineconeNamespace": PINECONE_NAMESPACE, "clearNamespace": False},
        },
        CRED["pinecone"],
    )
    wf.link(download, insert_files)
    wf.link(openai_embeddings(wf, "Embeddings (files)", (0, 140)), insert_files, "ai_embedding")
    loader_files = wf.node(
        "Loader (files)", "@n8n/n8n-nodes-langchain.documentDefaultDataLoader", 1.1, (160, 140),
        {
            "dataType": "binary",
            "binaryMode": "allInputData",
            "loader": "auto",
            "textSplittingMode": "custom",
            "options": {"metadata": {"metadataValues": [
                {"name": "source", "value": "={{ $('List files in KB folder').item.json.name }}"},
                {"name": "kind", "value": "file"},
            ]}},
        },
    )
    wf.link(loader_files, insert_files, "ai_document")
    split_files = wf.node(
        "Splitter (files)", "@n8n/n8n-nodes-langchain.textSplitterRecursiveCharacterTextSplitter", 1, (160, 320),
        {"chunkSize": 1000, "chunkOverlap": 150, "options": {}},
    )
    wf.link(split_files, loader_files, "ai_textSplitter")

    # B: Website pages ------------------------------------------------------
    urls = wf.node(
        "Website pages to index", "n8n-nodes-base.set", 3.4, (-420, 480),
        {
            "mode": "raw",
            "jsonOutput": json.dumps({"urls": [
                f"{STORE_DOMAIN}/pages/about-us",
                f"{STORE_DOMAIN}/pages/faq",
                f"{STORE_DOMAIN}/pages/contact",
                f"{STORE_DOMAIN}/policies/shipping-policy",
                f"{STORE_DOMAIN}/policies/refund-policy",
            ]}, indent=2),
            "options": {},
        },
    )
    wf.link(trigger, urls)
    split_urls = wf.node(
        "One item per URL", "n8n-nodes-base.splitOut", 1, (-200, 480),
        {"fieldToSplitOut": "urls", "options": {"destinationFieldName": "url"}},
    )
    wf.link(urls, split_urls)
    fetch = wf.node(
        "Fetch page", "n8n-nodes-base.httpRequest", 4.2, (20, 480),
        {
            "url": "={{ $json.url }}",
            "options": {"response": {"response": {"responseFormat": "text", "outputPropertyName": "html"}}},
        },
        onError="continueRegularOutput",
    )
    wf.link(split_urls, fetch)
    md = wf.node(
        "HTML → Markdown", "n8n-nodes-base.markdown", 1, (240, 480),
        {"html": "={{ $json.html }}", "destinationKey": "text", "options": {"ignore": "script,style,nav,header,footer,form,svg,noscript"}},
    )
    wf.link(fetch, md)
    insert_pages = wf.node(
        "Pinecone: insert pages", "@n8n/n8n-nodes-langchain.vectorStorePinecone", 1.3, (480, 480),
        {
            "mode": "insert",
            "pineconeIndex": pinecone_index_param(),
            "embeddingBatchSize": 100,
            "options": {"pineconeNamespace": PINECONE_NAMESPACE},
        },
        CRED["pinecone"],
    )
    wf.link(md, insert_pages)
    wf.link(openai_embeddings(wf, "Embeddings (pages)", (420, 720)), insert_pages, "ai_embedding")
    loader_pages = wf.node(
        "Loader (pages)", "@n8n/n8n-nodes-langchain.documentDefaultDataLoader", 1.1, (580, 720),
        {
            "dataType": "json",
            "jsonMode": "expressionData",
            "jsonData": "={{ $json.text }}",
            "textSplittingMode": "custom",
            "options": {"metadata": {"metadataValues": [
                {"name": "source", "value": "={{ $('One item per URL').item.json.url }}"},
                {"name": "kind", "value": "web_page"},
            ]}},
        },
    )
    wf.link(loader_pages, insert_pages, "ai_document")
    split_pages = wf.node(
        "Splitter (pages)", "@n8n/n8n-nodes-langchain.textSplitterRecursiveCharacterTextSplitter", 1, (580, 900),
        {"chunkSize": 1000, "chunkOverlap": 150, "options": {}},
    )
    wf.link(split_pages, loader_pages, "ai_textSplitter")

    wf.dump("hairqueenla-knowledge-base-ingestion.json")


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    build_reply_agent()
    build_ingestion()
