# Changelog

All notable changes to Metasploit API Explorer. Versions follow [Semantic Versioning](https://semver.org).

## [0.1.0] - 2026-10-23

First version, built on the Document Explorer (docrag) design: same agent loop, hybrid retrieval, context
management, notes, settings, web UI and API, specialized for the Metasploit Framework API reference.

### Corpus
- Scraped the full YARD API reference at `docs.metasploit.com/api/` (3,369 class/module/mixin pages) to
  Markdown, one file per class, laid out by namespace, in `metasploit-api-docs/`.
- `scripts/ingest_api_docs.py` indexes the corpus, setting each page's `collection` to its top-level
  namespace (`Msf`, `Rex`, `Msf::Exploit`, …); unchanged files are skipped by SHA-256.

### Agent
- System prompt rewritten for Metasploit module development: Framework conventions (superclass, mixins,
  `update_info`, `register_options`, entry methods), runnable Ruby examples in fenced blocks, and the rule to
  use only methods/options confirmed in the reference, citing the page each came from as `[D<id>]`.
- Tool descriptions and summary/scan prompts made API-aware (methods, signatures, mixins, options).

### Branding
- Renamed package `docrag` → `msfrag`, product "Document Explorer" → "Metasploit API Explorer",
  env prefix `DOCRAG_` → `MSFRAG_`, API-key prefix `drk_` → `msf_`, default port `8100` → `8120`.
- Empty-state prompts, placeholders and API examples rewritten for module writing.
