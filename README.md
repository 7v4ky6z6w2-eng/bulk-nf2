# bulk-nf2

Desktop tool with three integrated capabilities between a Firebird-based
ERP database and a WooCommerce store, with a GUI and independently
schedulable recurring jobs for each. See
`/root/.claude/plans/i-need-a-tool-quizzical-lark.md` (or ask for a copy)
for the full design context and rationale.

1. **Product sync** -- `ARTICLE` rows -> WooCommerce products (create/update,
   name cleaning, category derivation, promo pricing, live stock).
2. **Stock sync** -- a separate, lightweight, more-frequent pass that only
   updates `stock_quantity`/`manage_stock` on already-synced products.
3. **Order import** -- WooCommerce orders -> Firebird `PIECE`/`ITEM`
   documents (e.g. Bon de Livraison), idempotent per order.

## Setup (development)

```bash
pip install -r requirements-dev.txt
cp config.example.json config.json   # then edit with your real values
```

## Running

- GUI: `python -m app.main --config config.json`
- One headless product sync: `python -m app.main --sync --config config.json`
- One headless stock-only sync: `python -m app.main --stock-sync --config config.json`
- One headless order import: `python -m app.main --import-orders --config config.json`
- Preview without writing anything: add `--dry-run` to any of the three above.

## First-time setup if your store already has products

The tool tracks which article maps to which WooCommerce product in a local
`sync_state.sqlite3`. If your store was populated by a different/older tool,
run **adopt once** so this tool learns those products' ids (otherwise a full
sync tries to re-create them and hits "SKU already exists" errors, and stock
sync can't find them):

```bash
python -m app.main --adopt --config config.json
```

It enumerates existing WooCommerce products and matches them to DB articles
by SKU. **Important:** if you run a plugin that hides products without
images, it usually also hides them from the REST product listing — disable
it (or set it to frontend-only) just while `--adopt` runs, so every product
is visible. Afterwards, stock sync and full sync work off the stored ids and
never need to enumerate the store again, so the plugin can stay on.

## Before wiring up the real database

Run the schema-discovery diagnostic against your actual `.fdb` file first --
it confirms real column names/content (especially `ARTICLE.PHOTO` and the
stock-quantity table) instead of relying on guesses:

```bash
python -m app.db.schema_discovery --config config.json
```

Note: the sample database inspected during development uses Firebird
on-disk structure (ODS) 11.2, i.e. **Firebird 2.5** -- confirmed by trying to
attach it with a Firebird 3.0 server (`SQLCODE -820: unsupported on-disk
structure ... found 11.2, support 12.2`). This is why the project uses the
`fdb` driver rather than `firebird-driver` (which requires Firebird 3+).

## Tests

```bash
pytest tests/ -q
```

## Building the Windows .exe

1. Get a 64-bit `fbclient.dll` from a Firebird "client-only" install kit at
   firebirdsql.org (matching your Python's bitness) and place it at
   `packaging/fbclient/fbclient.dll` (not committed to this repo).
2. `pip install -r requirements-dev.txt`
3. `pyinstaller packaging/build.spec`

The build must happen on Windows (or a Windows-targeting cross-build setup);
it isn't produced by this repo's CI.
