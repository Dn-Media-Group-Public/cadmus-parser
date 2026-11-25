# Plan: Figure extraction from scanned pages

> **Status: implemented.** Depends on [processor-handoff](../implemented-plans/01-processor-handoff.md) (synthetic members), [sidecar-usage-reporting](../implemented-plans/03-sidecar-usage-reporting.md),
> [caption](../implemented-plans/05-caption.md) and the existing OCR (`server/cadmus_parser_server/ocr.py`); shares `common/upstream_auth.py` and the profile wiring with the other sidecars.

## Problem / Goal

A scanned page (a page image, or a scanned PDF page) contains text and pictures. OCR turns the whole page into one flat string, so a photo or illustration is lost
or turned into garbage text, and a consumer cannot embed or caption it on its own.

Goal: detect the picture regions on a page, crop each to its own image file that goes through the normal image pipeline (thumbnail, metadata, caption), and record where on the page it was.
Detection engines vary a lot in cost (a CPU model at a few hundred ms per page, a GPU layout model, a remote vision LLM billed per token), so the choice is a **deploy-time profile**, like `ocr` and `caption`.

## Design

### Feature and profiles

- New feature `layout` in the wrapper's `FEATURES` (`LAYOUT_PROFILES`: name -> `{url, token}`), set up by the overlay only when configured, with its own internal network `parse-layout`. With no `layout` profile configured, nothing changes.
- **A job asks for an ordered chain** of profiles: `features: {"layout": ["cheap", "docling", "gemini"]}` (a plain string is a chain of one). The wrapper hands the parser `LAYOUT_CHAIN` = JSON `[{url, token}, ...]` for exactly the profiles named, nothing else. (The existing features take one profile; list support in `wrapper.py` is new.)
- **Escalation:** a page goes to the next profile when a profile's answer is *unreliable*, not when it has no pictures (a page without pictures is a normal result): the profile returned no box of any label, or its confidence is below the profile's `escalate_below` (the answer's page-level `score` if the engine gives one, else the mean score of its boxes); also on any error or 503 (waited out like caption). The last profile's answer stands. **The threshold is per profile only** (no job option): scores are not comparable across engines. Each engine has a default used when the profile sets none: `yolo-onnx` 0.5, `docling-serve` 0.5 (against the page's `mean_score`; untuned: a trivial one-line page scored 0.80 in the experiment), `openai-vlm` none (a VLM gives no confidence, so it escalates only on an empty, unparseable or failed answer).
- **Who decides the chain.** The operator decides which profiles exist (`LAYOUT_PROFILES`: a local `yolo-onnx` sidecar, a `docling-serve` profile pointing at a local container or at a remote host, an `openai-vlm` profile) and may set `LAYOUT_DEFAULT_CHAIN` (profile names, used when a job asks for `layout` without naming profiles) and `LAYOUT_ALLOWED` (the profiles a job may name; unset = all). The job picks its chain among those, so all of these are expressible: **only Docling** (`["docling"]`: Docling is required, with no silent fallback to a cheaper model; if it fails the page keeps its OCR text and a note says so), **cheap with Docling as fallback** (`["cheap", "docling"]`), or **only cheap** (`["cheap"]`). A job naming a profile that does not exist or is not allowed is rejected when it is posted, like an unknown feature today. `page-ocr` follows the chain exactly: it never calls a profile that is not in it. There is deliberately no operator override that forces a chain: an operator who wants one path restricts `LAYOUT_ALLOWED` (a one-profile list is a forced profile); a job may ask for a shorter chain than the operator's default, which only reduces cost.

### layout-sidecar

A new sidecar `layout-sidecar/`, same protocol as the others ([sidecar-protocol](../sidecar-protocol.md)): `POST /process` with a PNG/JPEG page image as the body and `Authorization: Bearer <profile token>`;
the token selects the profile; no URL or path from the request; usage reporting through `common/usage.py`; egress pinned through `common/egress_rules.py`; `GET /health`.

`LAYOUT_PROFILES`: `{name: {token, engine, ...engine fields..., auth, ...}}`. Engines:

| `engine` | Where it runs | Profile fields | Notes |
|---|---|---|---|
| `yolo-onnx` | in the sidecar, CPU | `model_path` (weights file baked into or mounted in the image), `score_min` | cheapest; onnxruntime only, no torch; DocLayout-YOLO or PP-DocLayout weights exported to ONNX |
| `docling-serve` | a separate `docling-serve` service the sidecar calls, **directly or behind LiteLLM's HTTP pass-through** (then `base_url` is the LiteLLM route, e.g. `http://litellm:4000/docling`, and `auth: bearer` carries the LiteLLM key; see [Verified](#verified-in-an-experiment)) | `base_url`, `auth` (`upstream_auth`), `mode` | called as `POST <base_url>/v1/convert/file` (multipart: `files`, `do_ocr`, `do_table_structure`, `to_formats=json`); heavy; GPU where the admin deploys it. `mode: layout` (default): OCR and table structure switched off, only pictures and their boxes are used; our own OCR stays the text engine. `mode: full`: see [Full-page profiles](#full-page-profiles-docling-as-ocr-and-layout) |
| `openai-vlm` | through LiteLLM (or any OpenAI-compatible endpoint) | `base_url`, `model`, `auth`, `prompt?` | chat completions with an `image_url` data URI like `caption-sidecar`; the model is asked for JSON boxes (or, for `granite-docling` under vLLM, its layout tags); per-token usage reported |

Which of these exist in a deployment is the admin's choice: a profile is available only if it is in `LAYOUT_PROFILES`; `yolo-onnx` profiles need the sidecar service to run, the other two only need their upstream reachable.
The engine-specific parsing (class ids, Docling JSON, VLM output) stays in the sidecar.

**Common response**, identical for every engine (JSON body): `{"engine": "...", "model": "...", "width": W, "height": H, "boxes": [{"label": "picture|table|text|title|other", "bbox": [x0, y0, x1, y1], "score": 0.0-1.0}]}`;
`bbox` is in pixels of the image that was sent. An optional top-level `"score"` (0..1) is the engine's own page-level confidence (`docling-serve`: `confidence.mean_score`); `yolo-onnx` leaves it out and its box scores are used. Invalid or unparseable upstream output is a `502`, not an empty list (so it escalates instead of reading as "no pictures").
A `mode: full` profile adds to each box an optional `markdown` (a text block, a heading, a table as a markdown table; absent for pictures) and returns the boxes **in reading order**, plus a top-level `"full": true`.

### Full-page profiles (Docling as OCR and layout)

A `docling-serve` profile with `mode: full` runs Docling's whole pipeline with its own OCR and table structure on (TableFormer needs cell text, which a scan only gets from OCR; the OCR engine inside `docling-serve` is the admin's choice). It is an **alternative to the Tesseract-plus-`ocr` path**, not a layout step in front of it:

- The sidecar converts the `DoclingDocument` JSON (`document.json_content`) to the common response: each item's `prov[0].bbox` (`l`, `t`, `r`, `b` in PDF points, `coord_origin: BOTTOMLEFT`, so `t > b`; the page size is in `pages[n]`) converted to pixels of the image sent (scale by sent width / page width, flip y), per-item markdown (`export_to_markdown` per text and table item), pictures without markdown, all in reading order.
- When the profile that answers for a page is `full`, the page's text is the concatenation of its boxes' markdown, with a link `![](page-0003/fig-1.png)` in place of each picture box, and **replaces** that page's Tesseract/`ocr` result in `parsed/<rel>.md`. Pictures still become members and tables are also written as members (`page-0003/fig-1.png`, `page-0003/table-1.md`; see [Parser side](#parser-side-scanned-pages-as-page-units)).
- A `layout`-mode profile never changes the page text; it only adds members. So the admin chooses per job: `["cheap"]` (figures only, our OCR for text), `["cheap", "docling-full"]` (escalate to full-page when the cheap detector is unsure), or `["docling-full"]` alone.
- Table markdown is available only from a `full` profile (or a `openai-vlm` profile whose prompt asks for it); a `layout`-mode or `yolo-onnx` answer reports `table` boxes without markdown, and they are recorded in `figures.json` but not extracted.

### Parser side: scanned pages as page units

Scanned PDF pages are not handed to the image pipeline (`media-decode` and `media-markdown` skip synthetic members, and a page copy per page would clutter the output). They follow the idiom `asr-split` and `caption-split` use: the parent writes **synthetic units**, a dedicated processor accepts only those and leaves its result in scratch, and the parent writes the only real outputs. **No page image is ever an output.**

- **Which pages.** A PDF is decided per page, as `ocr._pdf` does today: a page whose text layer has at least `OCR_MIN_CHARS` characters keeps its pdfium text; any other page is scanned. A standalone image file is one page. Everything is bounded by `OCR_MAX_PAGES` (50) as today.
- **`documents` (PDF).** Renders each scanned page (`OCR_DPI`) to a scratch PNG plus a unit manifest `page-NNNN.json`, and calls `ctx.recurse_many(..., synthetic=True)`. After the last page it reads each page's result with `member.dir("page-ocr")` and writes `parsed/<pdf>.md` in page order (pdfium text for text-layer pages, `<!-- page N (ocr) -->` sections for scanned ones, as today) and `figures/<rel>/figures.json`. `ocr._pdf`'s OCR loop is removed.
- **`page-ocr` (new processor).** Accepts only page units; claims them. It runs the shared function `ocr.ocr_page(image)`, which is the single place where per-page work happens:
  1. Tesseract (`ocr_engine`, unchanged thresholds); the result is also the **gate**: layout runs only if the job asked for `layout` and Tesseract found at least `figures.min_text_chars` (default 200) characters (a photo with no text is `caption`'s job; a page with a text layer has its embedded images left alone, out of scope).
  2. The layout chain (a small client module `layout.py`, the sidecar protocol, escalation), on the page downscaled to `max_side` (default 2000).
  3. If the answering profile is `full`, its markdown is the page text and Tesseract/remote OCR is not used for the page; otherwise the text is Tesseract's, or the remote `ocr` sidecar's when Tesseract is below the thresholds, exactly as today.
  4. It keeps boxes labelled `picture` with `score >= score_min`, side at least `min_side` (64 px), area at least `min_area_frac` (0.01) of the page, at most `max_figures` (30) per page, and crops them from the **full-resolution** page into scratch files named `member-NNNN.png` (the name archive extraction uses, so that `members` also keeps each crop as `original/<rel>`).
  Its result (text, how, boxes, the crop files) is written to `page.json` in its `ctx.dir()`. It creates no member itself.
- **Standalone images.** `media-markdown` (the only remaining caller of the image OCR path) calls the same `ocr.ocr_page` and treats the image as page 1.
- **Figure crops are real members of the real file, grouped by page directory, created by the parent.** After assembly, `documents` (or `media-markdown` for an image) calls `ctx.recurse(path, "page-0003/fig-1.png")` for each crop in page order, so the creation order, and the entry order in `index.json`, is the page order, independent of the threads that ran the pages. The name contains `/` like any archive member name (`page-0001/...` for a standalone image). The address is `[pdf, "page-0003/fig-1.png"]`: no page entry exists, so no ghost entry is created and **no worker change is needed**. The crop then runs the normal chain (`media-decode` thumbnail, `metadata`, `caption` if requested, OCR) and appears in `index.json` as a nested entry of the PDF or image, e.g. `doc.pdf.d/page-0003/fig-1.png`.
- **Output** `figures/<file.rel>/figures.json`, written by the parent: `[{"page": 3, "member": "page-0003/fig-1.png", "bbox": [..], "page_size": [W, H], "score": 0.93, "profile": "cheap", "engine": "yolo-onnx"}]`, plus a note per page naming the profile that answered and any escalation (`page 3: layout docling (escalated from cheap)`).
- **Bound on crops:** besides the job's `max_figures` and `max_pages`, an **operator cap on figure members per file**, `max_figure_members`, set in the profile of the engine that answered the page and defaulting per engine (proposed: `yolo-onnx` 2000, `docling-serve` 1000, `openai-vlm` 200: the more a profile costs per page, and the more its crops will be captioned remotely, the lower). A job's options can only tighten it. It is counted per file under a lock (pages run in parallel); when it is reached, the remaining figures are not extracted and a note says so. It also stays within what is left of the wrapper's index entry cap (`max_entries`), past which entries are not listed.
- **Failure never fails the parse**: an exhausted chain, a down sidecar or a timeout is a note, and the page keeps its Tesseract/OCR text as today.
- **Page text and tables:** only when the answering profile is `full` (see above): the page markdown with reading order, the figure links (`![](page-0003/fig-1.png)`) and the tables replace the page's OCR text; tables are also written as `page-0003/table-<n>.md` members. With any other profile the page text is unchanged and a figure appears only as a member.
- **Scope kept out of the first version:** inline figure links and reading order for pages answered by a non-`full` profile (would need masking figure regions out of OCR), and table markdown from `yolo-onnx`.

### Weights

- **`yolo-onnx` weights are baked into the sidecar image at build time** (`layout-sidecar/Dockerfile`): the weights file is fetched in a build stage from a pinned URL given as a build argument and checked against a pinned SHA-256, and the image records the model name and licence (a label and `/models/README`). `model_path` in a profile names a file inside the image; a different model means a different image tag. Nothing is downloaded at runtime.
### OCR languages: available, selected, default

- **Available languages are set at deploy time.** Tesseract: `OCR_LANGS` names the packs installed in the parser image at build time (default `nor+eng`; a code without an installed pack is dropped with a note, as today). `docling-serve`: the OCR engine and its models installed in that deployment. VLM profiles: nothing to install. The available set is what a job may select from.
- **Selected per job; the deployment may set a default.** A job gives `options["ocr"] = {"langs": ["nb", "en"]}` (**IETF BCP 47** language tags, as consumers already hold them in HTML `lang` and document metadata, with script and region where they matter: `nb`, `nn`, `zh-Hans`, `de-Latf`; shared by `page-ocr`, `frame-ocr` and the image path). The operator may set `OCR_DEFAULT_LANGS` (a subset of the available languages, also BCP 47). **The job overrides the deployment default**; there are no further levels in the parser. A language that is not available is dropped with a note; if none is left the job behaves as if unset.
- **Default when neither job nor deployment sets a language: whatever the engine can detect.** Tesseract cannot auto-detect a language, so unset means all installed packs together (slower and less exact, which is why selecting is better); a VLM profile and Docling get no hint and use their own detection or configured defaults.
- **How the selection reaches each engine.** Tesseract: its `lang` argument per call, per job (today `LANGS` is read once at import), through a built-in table from BCP 47 to Tesseract's pack names (`en`→`eng`, `nb`/`nn`/`no`→`nor`, `zh-Hans`→`chi_sim`, `de-Latf`→`Fraktur`, ...); `OCR_LANGS` stays in Tesseract's native names because it names installed packs; a tag with no mapping or no installed pack is dropped with a note. `ocr-sidecar` and `layout-sidecar`: the existing protocol option header `X-Language: nor,eng` (the sidecar adds it to the VLM prompt; `docling-serve` gets it as its OCR language option through the profile's `lang_map` (BCP 47 tag -> the engine's own code), because its codes depend on its OCR engine, e.g. EasyOCR `no`/`en`; `yolo-onnx` ignores it).
- **Documented once, for every OCR engine**, in a new section of the deployment docs (`docs/parser-image.md` and `docs/quickstart.md` link to it): where the available set and the default are configured per engine, how a job selects and overrides the default, and the language codes per `docling-serve` OCR engine.

### Verified in an experiment

A throwaway stack (LiteLLM `main-stable` and `docling-serve-cpu`, both with arm64 builds, on one overlay network) showed:

- LiteLLM's generic pass-through proxies `docling-serve` unchanged, multipart upload included, with this proxy config (`include_subpath` is needed for `/v1/convert/file`):

  ```yaml
  general_settings:
    master_key: sk-...
    pass_through_endpoints:
      - path: "/docling"
        target: "http://docling:5001"
        include_subpath: true
        auth: true
  ```

  `POST http://litellm:4000/docling/v1/convert/file` with `Authorization: Bearer <key>` gave the same 200 and body as calling `docling-serve` directly.
- `auth: true` is enforced on this image: no key gives 401. A key other than the master key is looked up in LiteLLM's database; without a database it gives 400 `No connected db`, so virtual keys need the usual LiteLLM database (not tested).
- `docling-serve` returns `document.json_content` with `texts`, `pictures` and `tables` items whose `prov` carry `bbox` (PDF points, `BOTTOMLEFT`) and `page_no`, `pages[n]` sizes, and `confidence` with page-level and overall `layout_score`, `ocr_score`, `table_score`, `mean_score`, `mean_grade`. A ruled box in a test PDF came back as a `picture` item.
- Not tested: image uploads (PNG/JPEG pages, the sidecar's actual input), `do_ocr` with a language option, and tables.

## Open questions

1. Check that a crop written as `member-NNNN.png` is kept by `members` as `original/<rel>` when the parent recurses it from a real file that is not an archive (the `members` processor accepts scratch files named `member*`); adjust the naming or the processor if not. A test covers it (step 5); no decision is needed.
2. `docling-serve`, to be settled by testing against the target deployment at the start of implementation step 3, not decided here: the OCR language option and the per-engine codes behind `lang_map`, image uploads (the sidecar's actual input), and table cell text on a scanned page with OCR on. The [experiment](#verified-in-an-experiment) covered none of them; the engine is adjusted to what is found.

## Implementation order

1. Wrapper: list-valued features, `layout` in `FEATURES`, `LAYOUT_CHAIN` in the parser env; overlay and `docker-compose.parse.yml` (`LAYOUT_PROFILES`, `parse-layout` network, optional service); tests.
2. `layout-sidecar` with the common response and the `yolo-onnx` engine, weights baked in at build (pinned URL and checksum build args); tests with a stub model (as `test_caption_sidecar.py`).
3. `docling-serve` (first run the checks in open question 2 against the target deployment; both modes, including the coordinate conversion and per-item markdown) and `openai-vlm` engines with stubbed upstreams; Dockerfile and egress rules.
4. Parser: `layout.py` (chain client, escalation), `ocr.ocr_page` and the `page-ocr` processor (gate, layout, `full` markdown, crops, `max_figure_members`), `documents` (page units for scanned PDF pages, assembly of `parsed/<pdf>.md` and `figures.json`, `ocr._pdf` OCR loop removed), `media-markdown` (images through `ocr.ocr_page`); register the processor.
5. Tests: gate (text vs photo), box filtering, escalation (low score, empty, 502), crop coordinates at full resolution, crops in `index.json` as `page-NNNN/fig-N.png` with no page image in the outputs, mixed PDF (text-layer and scanned pages in order), the `max_figure_members` cap under parallel pages, `full` profile (reading order, table member, figure links, replaces OCR text, Tesseract skipped), coordinate conversion from PDF points, no `layout` profile configured = unchanged output, existing OCR tests for PDFs still pass through the page units.
6. Docs; move this plan to `docs/implemented-plans/` and update both indexes.

## Files to change

| File | Change |
|---|---|
| `layout-sidecar/` (new: `app.py`, `start.py`, `Dockerfile`, `tests/`) | the sidecar and its three engines |
| `server/cadmus_parser_server/layout.py` (new) | layout chain client and escalation |
| `server/cadmus_parser_server/ocr.py` | per-job languages (`options["ocr"]["langs"]` against `OCR_LANGS`, `X-Language`), `ocr_page` (Tesseract, gate, layout, `full` markdown, crops), `max_figure_members`; the PDF OCR loop (`_pdf`) removed |
| `server/cadmus_parser_server/page_ocr.py` (new) | the `page-ocr` processor for page units |
| `server/cadmus_parser_server/parse.py` | `documents`: page units for scanned PDF pages, assembly of `parsed/<pdf>.md` and `figures.json` |
| `server/cadmus_parser_server/media_markdown.py` | images through `ocr.ocr_page` |
| `server/cadmus_parser_server/wrapper.py` | `layout` feature, list-valued profiles, `LAYOUT_CHAIN`, `LAYOUT_DEFAULT_CHAIN` / `LAYOUT_ALLOWED`, rejecting unknown or disallowed profiles at POST |
| `ocr-sidecar/app.py` | `X-Language` header into the prompt |
| `server/cadmus_parser_server/overlay.py`, `docker-compose.parse.yml` | optional `layout-sidecar`, `parse-layout` network, `LAYOUT_PROFILES` |
| `server/pyproject.toml` | register `page-ocr` |
| `common/` | none expected (reuses `upstream_auth`, `usage`, `egress_rules`) |
| `server/tests/`, `layout-sidecar/tests/` | cases in step 5 |

## Docs to update

`docs/parser-image.md` and `docs/quickstart.md` (a new "OCR languages" section for every OCR engine: available, `OCR_DEFAULT_LANGS`, selected per job, default; linked from both; the sidecar image's baked-in weights), `docs/sidecar-protocol.md` (the new sidecar, the LiteLLM pass-through example with its `include_subpath`/`auth` settings and the database note for virtual keys, the common box response, `full` profiles), `docs/processors.md` (`page-ocr`, page units, `documents` and `media-markdown` changes, crops created by the parent as members of a real file), `docs/reference.md` (feature `layout`, options `options["page-ocr"]`, `figures.json`), `docs/security.md` (a new sidecar and egress), `docs/quickstart.md` (profiles), `docs/plans/index.md`, `docs/implemented-plans/index.md`.

## Implementation notes (where the code settled differently from the text above)

- **Chain entries carry the profile name.** `LAYOUT_CHAIN` is `[{name, url, token}]` (the plan said `{url, token}`): the parser needs the name for `figures.json` and the escalation notes. One usage record is registered per profile of the chain (`<job>.layout.<profile>`).
- **`escalate_below` and `max_figure_members` come with the answer.** The sidecar's response carries the profile's effective values (its own or the engine default); the parser keeps no copy of the sidecar's table.
- **`max_figure_members` is counted by the parent**, serially while it recurses the crops in page order, not under a lock in `page-ocr`: which figures survive is then deterministic (the first ones in page order). It does not look at the wrapper's `max_entries`: the wrapper stops listing past its own cap.
- **Options** live in `options["page-ocr"]` (`min_text_chars` included, not `figures.min_text_chars`); `max_pages` is `OCR_MAX_PAGES` as before.
- **Tables** from a non-`full` answer are recorded in `figures.json` with `member: null`; a figure member (`page-NNNN/fig-N.png`) never goes through layout again.
- **`documents` still OCRs an image** that media-decode did not take (text only, no layout) through `page_ocr.plain_image_text`; `media-markdown` is the only caller that makes page units of images.
- **Not verified here** (open question 2, needs the target deployment): `docling-serve` image uploads, its OCR language option and the codes behind `lang_map`, table cell text on a scanned page. The conversion was tested against a hand-written `DoclingDocument`, not a live server; the `yolo-onnx` engine against a stub model, not real DocLayout-YOLO weights (output layouts `(1, N, 6)` and `(1, 4+nc, N)` are handled).
