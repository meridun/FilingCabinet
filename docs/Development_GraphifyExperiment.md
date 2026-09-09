# Development_GraphifyExperiment.md — graphify over OCR text (phase-8 spike)

**Verdict: no-go, provisional.** Building an LLM-extracted knowledge graph over exported OCR text
is not worth productizing as a FilingCabinet feature on the evidence below. The one query class it
wins — transitive "who else is on that account" questions that no single document answers — is
reachable deterministically from metadata this index already stores. The verdict is *provisional*
because it was measured on a synthetic corpus (see [Corpus](#corpus)); the confirmation step is a
re-run against a real index.

Spike, not a feature. Nothing here ships as a product surface: the two scripts under `scripts/`
are throwaway instruments for this experiment, deliberately not wired into
`filingcabinet/cli.py`. Issue #8.

## Purpose and scope

Question asked: does running [graphify](Development_TokenTools.md) over per-document OCR text
exports surface cross-document entity links (the same vendor, person, or account number across
several scans) that `fc find` — FTS5, phase 4 — does not?

Out of scope: any permanent surface (an `export` verb, entity tables, entity-resolution code),
schema changes, and any change to `fc find` or the OCR ladder.

## Corpus

**Synthetic, fabricated end to end.** No real user document was read, exported, or graphed, and no
document content appears in this file.

`scripts/make_demo_corpus.py` builds a migrated index of 15 fabricated documents (2 pages each,
~800 words total): 11 documents that deliberately share entities — 3 vendors, 2 people, 2 account
numbers — plus 4 single-document distractors that share nothing and act as negative controls.
`scripts/export_ocr_text.py` then writes one Markdown file per document (front matter + `## Page n`
sections) to an OS temp directory outside this repo.

A real corpus was preferred and checked for first, per the implementation plan: no `--db`, no
`FC_DB`, and no `config.toml` `[paths].data_dir` resolved to an existing index on the build host,
so the synthetic path was taken. That is the single biggest limitation of this writeup — a corpus
built to contain cross-document links can only tell you whether the tool *finds* links that are
known to be there, never how often it finds links in the wild or invents ones that are not.

Content-leak guards (`docs/Architecture.md` §8): the exporter refuses any `--out` inside the repo
by construction (no override flag), exported filenames are `<document_id>-<sha prefix>.md` so a
scan's own filename never leaks into a name, and the graphify run itself was executed with the
working directory set to the temp directory, so `graphify-out/` (which contains text-derived node
summaries) was never created inside the repo.

## How to reproduce

`fc.exe` is not installed on Windows; use `python -m filingcabinet` (matches `SMOKE_CMD` in
`sdlc/PROFILE.md`). Set `PYTHONPATH` to the checkout when running against a working tree rather
than the installed package.

```bash
TMP=$TEMP/fc-graphify-8            # any directory outside this repo
python scripts/make_demo_corpus.py --db $TMP/demo.db
python scripts/export_ocr_text.py --db $TMP/demo.db --out $TMP/export
cd $TMP                            # never the repo: graphify writes graphify-out/ under the cwd
# then the /graphify skill pipeline over $TMP/export (detect -> extract -> cluster -> report)
graphify query "..." ; graphify path "A" "B" ; graphify explain "X"
```

With no `GEMINI_API_KEY`/`GOOGLE_API_KEY` set, graphify's semantic extraction falls to the host
agent, which is how this run was performed (inline, no subagents).

## graphify run summary

| Measure | Value |
|---|---|
| Corpus | 16 files (15 exported documents + the export manifest), ~797 words |
| Nodes / edges | 31 / 48 |
| Communities | 9 (5 substantive, 4 single-document) |
| Extraction mix | 94% EXTRACTED, 6% INFERRED (3 edges, avg confidence 0.75), 0% AMBIGUOUS |
| God nodes | the two account numbers, the two people, and the three vendors (degree 7-8); documents sit at degree 3 |
| Token cost (`graphify-out/cost.json`) | 3,569 input + 6,896 output for 16 files |
| Graph health | OK — no dangling, missing, collapsed, or self-loop edges |

Node shape that made linking work: one node per document, plus one **shared** node per entity that
every mentioning document points at. Sharing the entity node across documents is what turns
co-occurrence into graph structure; graphify's extraction spec pins node IDs to the source file for
code symbols and does not spell this out for document entities, so it was a modelling choice made
during the run, not something the tool decides for you. That choice is where most of the link
quality comes from — worth knowing before anyone reads the result as "graphify found this".

graphify's own report opened with `Corpus is ~797 words - fits in a single context window. You may
not need a graph.`

Cross-document links found (entity classes only; every value below is fabricated):

- **Vendor across documents** — each of the 3 vendors linked 2-4 documents.
- **Person across documents** — both people linked 3-4 documents each, across different vendors.
- **Account number across documents** — both accounts linked 3 documents each, including one where
  the paying bank and the billing vendor appear in *different* documents.
- **Vendor-to-vendor** — an insurer and a clinic linked EXTRACTED (one document names both), and an
  insurer and a utility likewise.
- **Person-to-person** — linked INFERRED (0.85) only. No document names both people; the edge is
  the extractor's household inference, not evidence.

## FTS5 comparison

Same corpus, same 6 queries. `fc find` counts are documents returned; the graphify column is what
`query` / `path` / `explain` returned.

| Query | `fc find` | graphify | Verdict |
|---|---|---|---|
| person A (`"Alex Marlowe"`) | 4 documents | same 4 via the person node's neighbours | tie |
| account number (`"ACCT-88213604"`) | 3 documents | same 3, plus the vendor, the bank, and both people attached to it | graphify adds context, not documents |
| vendor (`"Cedarpoint Clinic"`) | 3 documents | same 3 | tie |
| two vendors together (`"Harborline AND Cedarpoint"`) | 1 document | 1-hop EXTRACTED edge, same evidence | tie |
| two people together (`"Marlowe AND Ramanathan"`) | **0 documents** | 1-hop INFERRED edge (0.85) | graphify answers, but from inference, not evidence |
| bank to utility (no document names both) | not expressible as one query | **2-hop path via the shared account number** | **graphify wins** |
| negative control (`"Zephyr Bicycle Repair"`) | 1 document | isolated 2-node community, no path to any person | tie (both correctly find nothing else) |

The pattern is consistent: for "which documents mention X", FTS5 is equal, instant, and free. The
only class FTS5 cannot express is the **transitive** one — X and Z are related because both touch Y,
and no single document contains both X and Z. graphify answered exactly one such query here on
evidence (bank → account → utility) and one on inference (person ↔ person).

## Go/no-go

**No-go** on productizing LLM-extracted knowledge graphs over OCR text, for three reasons:

1. **The win is narrow and reproducible without an LLM.** The only evidence-backed win was a 2-hop
   join through a shared account number. Every entity that carried a real link was either already a
   column on `document` (`party`, `doc_type`, `doc_date`) or a pattern-matchable literal (an account
   number). A deterministic entity/co-occurrence table over the existing index would answer the same
   question at zero token cost and with no non-determinism — and would stay true after every
   ingest, which a graph does not.
2. **Cost scales with the corpus and repeats on every change.** ~10.5k tokens for ~800 words means
   re-extraction dominates on a real cabinet of thousands of scanned pages, and every ingest makes
   the graph stale. `fc find` costs nothing and is always current.
3. **The interesting edges are the untrustworthy ones.** The person-to-person link — the single
   result FTS5 could not produce at all — is an INFERRED edge, i.e. LLM output about people's
   relationships derived from their private documents. For a filing cabinet whose posture is
   "tools never move documents unasked" (`docs/Architecture.md` §6) and "no user documents leave the
   instance" (§8), silently materialising inferred links between people is a bad default, and one
   that cannot be validated against ground truth.

No follow-up productization issue is filed (that is owed only on a "go"). The deterministic
alternative named in reason 1 — an entity co-occurrence index built from `party` plus pattern-matched
identifiers, queryable as "which documents share an identifier with this one" — is the thing worth
filing if anyone wants this capability; it is a different feature from this spike and needs its own
intake.

## Limitations

- **Synthetic corpus.** The links were planted, so the run measures recall on known links, not
  precision in the wild. Confirmation step: re-run the same pipeline against a real migrated index
  (≥20 documents with OCR text) and compare the same query classes. Until then this verdict stays
  provisional.
- **INFERRED edges are LLM output, not ground truth.** 3 of 48 edges here; they carry the most
  interesting-looking claims and the least evidence.
- **The host agent was the extractor.** No Gemini key was set, so semantic extraction ran inline in
  the session and the token counts in `cost.json` are host-agent estimates (characters ÷ 4), not
  provider-billed figures. Treat them as order-of-magnitude.
- **Node modelling is the operator's choice.** Shared entity nodes across documents (the thing that
  makes the graph useful here) are not something the extraction spec prescribes for document
  corpora; a different modelling choice yields a much weaker graph from the same corpus.
