# n8n agents

## Hair Queen LA · ReachInbox reply agent

An n8n agent that answers people who reply to Hair Queen LA's ReachInbox campaigns. It reads the whole email thread, looks up brand knowledge and live store products, and writes a reply in the brand's voice with the correct booking and product links. It can only reply after the reply passes validation, and only once you switch it from `draft` to `send`.

```
             ┌──────────────────────┐        ┌──────────────────────────────┐
ReachInbox ─▶│ Webhook (reply event)│──┐     │ Reply agent (Claude Sonnet)  │
             └──────────────────────┘  │     │  tools:                      │
             ┌──────────────────────┐  ├────▶│  • ReachInbox MCP (read-only)│──▶ Validate & dedupe ──▶ mode = send?
Every 15 min▶│ Triage agent (Haiku) │──┘     │  • search_knowledge_base     │         │                 │yes        │no / skip / escalate
             │ finds waiting threads│        │    (Pinecone)                │         │                 ▼           ▼
             └──────────────────────┘        │  • search_store /            │         │      ReachInbox send API   │
                                             │    get_product_details       │         │                 │           │
                                             │    (live Shopify)            │         │                 └─────▶ Google Sheet "Reply Log"
                                             └──────────────────────────────┘
```

### What's in this repo

| Path | What it is |
|---|---|
| `workflows/hairqueenla-reachinbox-reply-agent.json` | The agent workflow. Import it into n8n. |
| `workflows/hairqueenla-knowledge-base-ingestion.json` | Loads your knowledge base (GHL exports and website pages) into Pinecone. |
| `prompts/` | The agents' instructions. Edit these, then run the build script. |
| `scripts/build_workflows.py` | Regenerates the workflow JSON from the prompts and settings. |
| `mcp-server/` | Docker setup that runs the ReachInbox MCP server over HTTPS for n8n, with read-only tools only. |
| `docs/knowledge-base.md` | Why Pinecone, how to get your knowledge out of GHL, and how to write it well. |
| `knowledge-base/brand-profile.md` | Starter brand document to fill in, verify and ingest. |

### Key design decisions

- **Claude is the brain.** Replies use Claude Sonnet 5.5 (`claude-sonnet-5-5`). The cheaper triage step uses Claude Haiku 5.5 (`claude-haiku-5-5`). You add your Anthropic API key as an n8n credential; it is never in these files.
- **The agent reads ReachInbox through MCP but cannot send through it.** The MCP gateway only exposes `list_onebox_threads` and `get_onebox_thread`. The agent returns a structured draft, a Code node checks it (valid addresses, threading headers present, no placeholder text, not already answered), and a plain HTTP node sends it through ReachInbox's `/api/v1/onebox/send`. This prevents a confused model or a malicious incoming email from sending anything unexpected, and there is no way for the agent to touch campaigns, leads or deletes.
- **Draft mode first.** With `mode = draft` (the default), nothing is sent. Every decision and draft is written to the Google Sheet so you can review quality. Switch `mode` to `send` in the Config node when you're happy.
- **Whole-thread context.** The reply agent always fetches the full thread, and skips the thread if our message is already the newest one.
- **No double replies.** The webhook and the 15-minute sweep can both see the same reply, so *Validate & dedupe* remembers every message it has answered for 30 days.
- **Products come from the live store, not the vector DB.** Prices, stock and links come from Shopify's public storefront search (`/search/suggest.json`, `/products/<handle>.js`), so they're always current. Pinecone holds the brand knowledge: services, policies, FAQs and voice.
- **It knows when not to reply.** Unsubscribes, "not interested", auto-replies and bounces are skipped. Complaints, order problems, refunds, partnership offers, medical questions, and anything the knowledge base can't answer are escalated, meaning logged for a human with a summary of what's needed.

## Setup

### 1. Run the ReachInbox MCP gateway

ReachInbox doesn't publish an official MCP server. This repo uses the open-source community server [nbhr-maker/reachinbox-mcp](https://github.com/nbhr-maker/reachinbox-mcp), pinned to a reviewed commit, wrapped with [supergateway](https://www.npmjs.com/package/supergateway) so n8n can reach it over Streamable HTTP. The gateway requires an API key on every request and exposes only the read-only Onebox tools.

```bash
cd mcp-server
cp .env.example .env    # add REACHINBOX_API_KEY and a random MCP_GATEWAY_API_KEY
docker compose up -d --build
```

- **Self-hosted n8n** on the same Docker host: MCP endpoint is `http://reachinbox-mcp:8000/mcp` (attach both to one network), or `http://<host>:8000/mcp`.
- **n8n Cloud**: point a DNS name at the server, set `MCP_DOMAIN` in `.env`, run `docker compose --profile https up -d --build`, and use `https://<MCP_DOMAIN>/mcp`.

If you already have a different hosted ReachInbox MCP, you can use its URL instead. It must offer tools that list Onebox threads and read a full thread. If its tool names differ, update `READ_TOOLS` in the build script and the tool names in the prompts.

### 2. Create the knowledge base

1. Create a Pinecone index `hairqueenla-kb` (1536 dimensions, cosine).
2. Export your GHL knowledge base into one Google Drive folder, following [docs/knowledge-base.md](docs/knowledge-base.md).
3. Import `workflows/hairqueenla-knowledge-base-ingestion.json`. Set the Drive folder URL, list the website pages to crawl in *Website pages to index*, attach credentials, and click **Execute workflow**.

### 3. Import the agent workflow

Import `workflows/hairqueenla-reachinbox-reply-agent.json`, then:

1. **Config** node: set `booking_url` (your salon booking link), and check `store_url`, `signature_name`, `lookback_hours` and `max_threads_per_run`. Leave `mode` on `draft`.
2. **Credentials** (create in n8n and select them on the nodes):

   | Credential | Type | Used by |
   |---|---|---|
   | Anthropic account | Anthropic API | both Claude model nodes |
   | ReachInbox MCP gateway key | Bearer Auth, token = `MCP_GATEWAY_API_KEY` | both *ReachInbox MCP* tool nodes (also set their **Endpoint** URL) |
   | ReachInbox API key | Bearer Auth, token = your ReachInbox API key | *Send reply via ReachInbox* |
   | Pinecone account | Pinecone API | *search_knowledge_base* |
   | OpenAI account | OpenAI API (embeddings only) | *Embeddings (OpenAI)* |
   | Google Sheets account | Google Sheets OAuth2 | *Append to Reply Log sheet* |

3. **Reply Log sheet**: create a Google Sheet with a tab named `Reply Log` and this header row, then paste its URL into the sheet node:

   `logged_at, status, source, decision, category, lead_email, lead_name, account, thread_id, subject, reply_text, reason, summary_for_team, error, http_status`

4. If the store isn't on `hairqueenla.com`, change the URL in the `search_store` and `get_product_details` tool nodes (or `STORE_DOMAIN` in the build script).

### 4. Connect the ReachInbox webhook

Activate the workflow and copy the **production URL** of the *ReachInbox webhook* node. In ReachInbox, add a webhook for the reply-received event only, pointing at that URL. The path contains a random suffix; treat the URL as a secret. The 15-minute sweep keeps working even without the webhook.

### 5. Test, then go live

1. With `mode = draft`, reply to one of your own campaign emails from a personal address and watch the execution. Read the draft in the sheet.
2. Let it run in draft mode for a day or two and review the sheet. Fix wrong answers by improving the knowledge base, not the prompt.
3. Set `mode` to `send`.

> Duplicate protection uses n8n's workflow static data, which is only saved for **production** (active, trigger-started) executions, not for manual test runs.

## Changing the prompts

Edit `prompts/*.md`, then:

```bash
python3 scripts/build_workflows.py
```

Re-import the regenerated workflow, or paste the prompt into the agent node's *System Message*. The prompts contain n8n expressions such as `{{ $('Config').first().json.booking_url }}`, which n8n fills in at run time.

## Ideas for v2

- Post escalations to Slack or email the team instead of only logging them.
- One-click approve from Slack in draft mode.
- Tag or label leads in ReachInbox by category (interested, booked, not interested).
- Track bookings coming from email replies (UTM on the booking link).
