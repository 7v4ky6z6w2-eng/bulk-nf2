# bulk-nf2

Desktop tool that syncs products from a Firebird-based ERP database (the
`ARTICLE` table) into a WooCommerce store, with a GUI and an optional
recurring schedule. See `/root/.claude/plans/i-need-a-tool-quizzical-lark.md`
(or ask for a copy) for the full design context and rationale.

## Setup (development)

```bash
pip install -r requirements-dev.txt
cp config.example.json config.json   # then edit with your real values
```

## Running

- GUI: `python -m app.main --config config.json`
- One headless sync: `python -m app.main --sync --config config.json`
- Preview without writing anything: `python -m app.main --sync --dry-run --config config.json`

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
