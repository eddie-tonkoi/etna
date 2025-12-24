# etna

A small collection of personal scripts for text checking / editorial workflows.

This repo is primarily for my own reference. If you find it useful, feel free to fork and adapt.

## What this is

- Practical scripts for analysing text (grammar, spelling, consistency checks, etc.)
- Glue code that ties together a few Python libraries and separately-installed tools

## What this is not

- A polished end-user application
- A supported library with a stable API

## Prerequisites

- Python 3.10+ (3.11/3.12 should be fine)
- A virtual environment (`venv`)

Optional external tools (used by some scripts):

- **LanguageTool** running as a local server (HTTP)
- **Hunspell** (plus dictionaries — dictionary licences vary)

## Setup

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
```

This project uses a `pip-tools` workflow:

- `requirements.in` is the hand-edited list of top-level dependencies
- `requirements.txt` is generated (pinned) from `requirements.in`

Install tooling and sync dependencies:

```bash
python -m pip install pip-tools
pip-compile requirements.in -o requirements.txt
pip-sync requirements.txt
```

## Optional dependency (GPL)

Some scripts can use the **GPLv3** Python wrapper `language_tool_python` to talk to a separately running LanguageTool server.

It is **not installed by default**.

If you want to use scripts that import `language_tool_python` (for example `07_grammar_check.py`), opt in with:

```bash
pip install -r requirements-gpl.txt
```

If you prefer to avoid GPL-licensed Python dependencies, simply don’t install `requirements-gpl.txt` and ignore those scripts.

## LanguageTool

These scripts are intended to call a **separately running** LanguageTool server over HTTP (for example `http://localhost:8081`).

How you run the server (jar, Docker, etc.) is up to you; this repo does not bundle LanguageTool.

## spaCy model

This project pins the English small model via a direct URL in `requirements.in`:

- `en_core_web_sm` (downloaded during install)

## Usage

Run scripts directly, for example:

```bash
python path/to/script.py --help
```

Each script has its own assumptions and arguments.

## Licence

MIT. See `LICENSE`.

## Third-party licences

See `THIRD_PARTY_LICENSES.md` for an inventory of Python dependencies and their licences.

External tools (LanguageTool, Hunspell, dictionaries) are not bundled here and retain their own licences.