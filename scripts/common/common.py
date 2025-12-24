# 📁 shared/common.py
from docx import Document

def find_docx_file(cwd):
    docx_files = list(Path(cwd).glob("*.docx"))
    return docx_files[0] if len(docx_files) == 1 else None

def load_text(cwd):
    book_txt = Path(cwd) / "book.txt"
    docx_file = find_docx_file(cwd)
    if docx_file:
        print(f"📘 Using DOCX: {docx_file.name}")
        doc = Document(docx_file)
        return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
    elif book_txt.exists():
        print("📘 Using TXT fallback: book.txt")
        return book_txt.read_text(encoding="utf-8")
    else:
        raise FileNotFoundError("No .docx file or book.txt found in current directory.")


def load_dictionary(paths, seen_files=None, depth=0, root=True):
    words = set()
    seen_files = seen_files or set()

    for path in paths:
        path = Path(path).resolve()
        indent = "  " * depth

        if path in seen_files:
            print(f"{indent}🔁 Already included: {path}")
            continue

        if not path.exists():
            print(f"{indent}❌ Not found: {path}")
            continue

        seen_files.add(path)
        local_words = set()
        includes = []

        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") and not line.startswith("#include"):
                        continue
                    if line.lower().startswith("#include"):
                        parts = line.split(None, 1)
                        if len(parts) == 2:
                            include_path = (path.parent / parts[1].strip()).resolve()
                            includes.append(include_path)
                        else:
                            print(f"{indent}⚠️ Malformed include in {path.name}: {line}")
                    else:
                        local_words.add(line)
        except Exception as e:
            print(f"{indent}⚠️ Error reading {path.name}: {e}")
            continue

        for include_file in includes:
            words |= load_dictionary([include_file], seen_files, depth + 1, root=False)

        words |= local_words
        print(f"{indent}📘 Loaded {len(local_words):>4} entries from: {path}")

    if root:
        print(f"📦 Total unique dictionary entries loaded: {len(words)}")

    return words


# ---------------- Config loading (config.yaml + optional config.local.yaml) ----------------

def _deep_merge_dicts(base: dict, override: dict) -> dict:
    """Deep-merge override into base (dicts only). Returns a new dict."""
    out = dict(base or {})
    for k, v in (override or {}).items():
        if isinstance(out.get(k), dict) and isinstance(v, dict):
            out[k] = _deep_merge_dicts(out[k], v)
        else:
            out[k] = v
    return out


def config_dir() -> Path:
    """Directory that holds config.yaml (etna/scripts/common)."""
    return Path(__file__).resolve().parent


def config_path() -> Path:
    return config_dir() / "config.yaml"


def local_config_path() -> Path:
    return config_dir() / "config.local.yaml"


def load_config(*, require_base: bool = True) -> dict:
    """Load config.yaml and overlay config.local.yaml if present.

    - config.yaml: committed, portable defaults
    - config.local.yaml: optional machine-specific overrides (should be gitignored)

    If require_base=True, missing config.yaml is an error.
    """
    base_path = config_path()
    local_path = local_config_path()

    if not base_path.exists():
        if require_base:
            raise FileNotFoundError(f"Missing config file: {base_path}")
        return {}

    try:
        import yaml  # type: ignore
    except ImportError as e:
        raise ImportError(
            "PyYAML is required to load scripts/common/config.yaml. Install with: pip install pyyaml"
        ) from e

    with base_path.open("r", encoding="utf-8") as f:
        base = yaml.safe_load(f) or {}

    if local_path.exists():
        with local_path.open("r", encoding="utf-8") as f:
            local = yaml.safe_load(f) or {}
        if isinstance(base, dict) and isinstance(local, dict):
            return _deep_merge_dicts(base, local)
        # If shapes are unexpected, prefer local (explicit override).
        return local

    return base if isinstance(base, dict) else {}


def load_paths(*, require_base: bool = True) -> dict:
    """Convenience accessor for the `paths:` section of the merged config."""
    cfg = load_config(require_base=require_base)
    paths = cfg.get("paths", {}) if isinstance(cfg, dict) else {}
    return paths if isinstance(paths, dict) else {}

