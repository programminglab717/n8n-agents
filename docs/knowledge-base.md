# Knowledge base: what goes where

The agent gets its knowledge from three places. Each kind of information goes where it stays most accurate.

| Information | Where it lives | Why |
|---|---|---|
| Booking link, store URL, sign-off name | **Config** node in the reply workflow | These never change and must never be wrong, so they go straight into the prompt. |
| Brand story, services, salon prices, policies, FAQs, hours, location, stylists, hair and wig care | **Pinecone** (`hairqueenla-kb` index, `brand` namespace) | Long, unstructured text. The agent searches it with `search_knowledge_base`. |
| Products, collections, product prices, stock, product links | **Live Shopify store**, through the `search_store` and `get_product_details` tools | The catalog changes daily, and a vector copy would go stale. The live store always has the current price, availability and URL. |

## Why Pinecone

- n8n has a native Pinecone node for both inserting and searching (as an agent tool). There's no database to run yourself.
- The free Starter plan is plenty for a brand knowledge base of a few thousand chunks.
- Alternatives work the same way in n8n if you prefer one: Supabase (pgvector) if you already use Supabase, or Qdrant if you self-host. Swap the two Pinecone nodes and the search tool.

**Embeddings.** Claude doesn't produce embeddings, so the workflows use OpenAI `text-embedding-3-small` (1536 dimensions). Embedding a whole knowledge base costs a few cents. You can switch to another embeddings provider, but the same model must be used for inserting and for searching, and the index dimension must match it.

### Create the index

In Pinecone, choose **Create index**:

- Name: `hairqueenla-kb`
- Dimensions: `1536`
- Metric: `cosine`
- Type: serverless (any cloud or region)

## Getting the knowledge out of GoHighLevel

GHL has no single "export knowledge base" button. Its AI agent knowledge base is built from four kinds of source, and each one comes out a different way.

1. **Knowledge Base Tables (structured Q&A or data tables).** GHL supports a direct CSV export. Go to AI Agents → Knowledge Base → **Tables** tab, open each table that shows *ready*, and export it to CSV. Upload the CSVs to the Google Drive knowledge-base folder. (GHL help article: "How to Download Knowledge Base Tables in CSV".)
2. **Uploaded files (PDFs, docs).** You already have the originals. If not, download them from the GHL Media Library. Put them in the Drive folder.
3. **FAQs typed into GHL.** There's no export, so copy them into one Google Doc (for example "Hair Queen LA – FAQs") using the format below. Put it in the Drive folder.
4. **Web crawler sources (URLs GHL crawled).** Don't export these. Copy the URL list from the knowledge base sources screen into the **Website pages to index** node of the ingestion workflow, and the workflow crawls them itself.

FAQ format that retrieves well (one question per block):

```
Q: Do you do wig installs for wigs bought elsewhere?
A: ...

Q: How much is the booking deposit, and is it refundable?
A: ...
```

Also add `knowledge-base/brand-profile.md` from this repo (filled in and checked) to the Drive folder.

## Writing tips that make the agent better

- One topic per paragraph, with the topic named in it ("Our 5x5 HD lace closure wig install takes…", not "It takes…"). Chunks are retrieved on their own, so each should make sense without the rest of the document.
- Put real numbers and real links in the text: prices, deposit amount, hours, address, parking, turnaround times.
- Add a "Things we don't do" section (for example no walk-ins, no installs on Sundays). Stating these explicitly stops the agent from promising them.
- Remove outdated promotions. The agent treats anything in the knowledge base as true.

## Refreshing

Re-run the **Knowledge Base → Pinecone** workflow whenever content changes. To rebuild cleanly, turn on **Clear Namespace** in the *Pinecone: insert files* node for one run, then turn it off. Files are inserted first, so the clear happens before the web pages are added.
