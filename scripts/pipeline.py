import os
import re
import subprocess
import sys
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, Union

import yaml


def _find_config_path() -> Path:
    """Locate common/config.yaml relative to this script."""
    here = Path(__file__).resolve().parent
    candidates = [
        here / "common" / "config.yaml",          # e.g. scripts/common/config.yaml
        here.parent / "common" / "config.yaml",   # e.g. common/config.yaml next to scripts/
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(
        "Could not find common/config.yaml. Looked in: "
        + ", ".join(str(c) for c in candidates)
    )



def _deep_merge_dicts(base: dict, override: dict) -> dict:
    """Deep-merge override into base (dicts only). Returns a new dict."""
    out = dict(base)
    for k, v in (override or {}).items():
        if (
            k in out
            and isinstance(out.get(k), dict)
            and isinstance(v, dict)
        ):
            out[k] = _deep_merge_dicts(out[k], v)
        else:
            out[k] = v
    return out


def _local_config_path(config_path: Path) -> Path:
    """Return the sibling config.local.yaml path for a given config.yaml."""
    return config_path.with_name("config.local.yaml")


def load_config() -> dict:
    """Load config.yaml and overlay config.local.yaml if present.

    - config.yaml: committed, portable defaults
    - config.local.yaml: optional machine-specific overrides (should be gitignored)
    """
    config_path = _find_config_path()
    with config_path.open("r", encoding="utf-8") as f:
        base = yaml.safe_load(f) or {}

    local_path = _local_config_path(config_path)
    if local_path.exists():
        with local_path.open("r", encoding="utf-8") as f:
            local = yaml.safe_load(f) or {}
        if isinstance(base, dict) and isinstance(local, dict):
            return _deep_merge_dicts(base, local)
        # If the shapes are unexpected, prefer local (explicit override).
        return local

    return base


def get_books_root_from_config() -> Path:
    """Return the books_root path from common/config.yaml, resolved to an absolute path."""
    cfg = load_config()
    paths = cfg.get("paths", {}) if isinstance(cfg, dict) else {}
    raw = paths.get("books_root")
    if not raw:
        raise KeyError("Missing required config key: paths.books_root")

    config_path = _find_config_path()
    p = Path(raw)
    if not p.is_absolute():
        # Resolve relative to the config file location for portability.
        p = (config_path.parent / p).resolve()

    # If the configured location doesn't exist (e.g. private repo not present),
    # fall back to local example content so the pipeline still works.
    if p.exists() and p.is_dir():
        return p

    scripts_dir = Path(__file__).resolve().parent
    fallback_candidates = [
        (scripts_dir / "../example").resolve(),
    ]

    for fb in fallback_candidates:
        if fb.exists() and fb.is_dir():
            print("⚠️  Configured books_root not found; using fallback example path instead.")
            print(f"   - configured: {p}")
            print(f"   - fallback:   {fb}")
            return fb

    raise FileNotFoundError(
        "Configured books_root does not exist and no fallback was found. Tried: "
        + ", ".join([str(p)] + [str(x) for x in fallback_candidates])
    )

def clear_terminal():
    os.system("clear" if os.name == "posix" else "cls")


# A “status one-liner” is the final summary line each script prints.
# Convention used across the rule-based scripts: it starts with ✅ (clean) or ⚠️ (needs review) or ❌ (failed).
# Note: ⚠️ is often emitted as two codepoints: U+26A0 (⚠) + U+FE0F (variation selector).
# Match ✅ / ❌ / ⚠ with an optional U+FE0F so we reliably detect status lines.
RUN_ALL_SENTINEL = "__RUN_ALL__"
STATUS_LINE_RE = re.compile(r"^[✅❌⚠]\uFE0F?\s+")

# --- Script status icon store ---
STATUS_STORE_FILENAME = ".etna_script_status.json"
NOT_RUN_ICON = "⏺"  # not yet run in this book folder
INFO_ONLY_ICON = "📝"  # ran successfully, but is informational/non-gating

# Scripts that generate useful reports/follow-up artefacts but do NOT “pass/fail” the manuscript.
# These should not block the correctness certificate.
INFO_ONLY_SCRIPTS = {
    "08_ward_audit.py",
    "10_like_and_crutchwords.py",
    "11_repetition_patterns.py",
}

def _status_icon_from_status_line(status_line: Optional[str]) -> str:
    """Normalise the leading icon from a status line to one of ✅ / ⚠️ / ❌.

    The ⚠️ icon may appear as '⚠' + optional U+FE0F; we normalise to '⚠️'.
    """
    if not status_line:
        return NOT_RUN_ICON
    s = status_line.strip()
    if not s:
        return NOT_RUN_ICON

    ch0 = s[0]
    if ch0 == "✅":
        return "✅"
    if ch0 == "❌":
        return "❌"
    if ch0 == "⚠":
        return "⚠️"
    if ch0 == "📝":
        return "📝"
    return "❔"

def _status_store_path(book_dir: Path) -> Path:
    return book_dir / STATUS_STORE_FILENAME

def load_last_status(book_dir: Path) -> dict[str, str]:
    """Load last-run status icons per script for a given book folder."""
    p = _status_store_path(book_dir)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            # Ensure values are strings.
            return {str(k): str(v) for k, v in data.items()}
    except FileNotFoundError:
        return {}
    except Exception:
        # Corrupt/invalid file: ignore rather than breaking the pipeline.
        return {}
    return {}

def save_last_status(book_dir: Path, status_map: dict[str, str]) -> None:
    """Persist last-run status icons per script for a given book folder."""
    p = _status_store_path(book_dir)
    try:
        p.write_text(
            json.dumps(status_map, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except Exception:
        # Never hard-fail the pipeline just because we can't write status.
        pass

def update_last_status(book_dir: Path, script_name: str, rc: int, status_line: Optional[str]) -> None:
    """Update and persist status icon for one script in this book folder."""
    status_map = load_last_status(book_dir)

    if rc != 0:
        icon = "❌"
    elif is_info_only_script(script_name):
        icon = INFO_ONLY_ICON
    else:
        icon = _status_icon_from_status_line(status_line)

    status_map[script_name] = icon
    save_last_status(book_dir, status_map)


def _extract_status_line(line: str) -> Optional[str]:
    """Return the status one-liner if this line looks like one; else None."""
    s = (line or "").strip()
    if not s:
        return None
    return s if STATUS_LINE_RE.match(s) else None


def is_info_only_script(script_name: str) -> bool:
    return script_name in INFO_ONLY_SCRIPTS


def canonical_status_line(script_path: Path, rc: int, status_line: Optional[str]) -> str:
    """Return a status line suitable for summaries / storage.

    If a script didn’t emit a recognised ✅/⚠️/❌ one-liner, we synthesise a ⚠️ line
    so the run is still recorded and visible.
    """
    if status_line:
        return status_line
    if rc != 0:
        return f"❌ {script_path.name} failed (exit {rc}) — see output above"
    return (
        f"⚠️  {script_path.name} finished — no status one-liner detected "
        "(open its report/output if unsure)"
    )

def find_valid_book_folders(books_dir):
    return sorted([
        f for f in books_dir.iterdir()
        if f.is_dir() and any(doc.suffix == ".docx" for doc in f.rglob("*.docx"))
    ])

def list_scripts(script_dir):
    return sorted([
        f for f in script_dir.iterdir()
        if f.is_file() and f.suffix in {'.sh', '.py'}
    ])


def list_script_folders(root_dir: Path) -> list[Path]:
    """Return subfolders next to pipeline.py that contain runnable scripts."""
    exclude = {"common", ".venv", "__pycache__"}
    folders = [
        d for d in root_dir.iterdir()
        if d.is_dir() and not d.name.startswith(".") and d.name not in exclude
    ]
    return sorted(folders, key=lambda p: p.name.lower())

def run_script(script_path: Path, working_dir: Path) -> Tuple[int, Optional[str]]:
    """Run a script, stream its output, and capture its final ✅/⚠️/❌ summary line.

    Returns:
        (returncode, status_line)
    """
    cmd: list[str]
    cwd: Optional[Path] = None

    if script_path.suffix == ".py":
        python_bin = sys.executable
        cmd = [python_bin, str(script_path), str(working_dir)]
    elif script_path.suffix == ".sh":
        cmd = ["zsh", str(script_path)]
        cwd = working_dir
    else:
        return (1, f"❌ Unsupported script type: {script_path.name}")

    status_line: Optional[str] = None

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=False,  # binary mode: preserve '\r' for in-place progress bars
            bufsize=0,
        )

        assert proc.stdout is not None
        # Stream bytes so progress bars using '\r' update in place.
        out = getattr(sys.stdout, "buffer", None)
        line_buf = bytearray()

        while True:
            chunk = proc.stdout.read(1024)
            if not chunk:
                if proc.poll() is not None:
                    break
                continue

            # Write raw bytes to the terminal (preserves carriage returns).
            if out is not None:
                out.write(chunk)
                out.flush()
            else:
                # Fallback if stdout has no buffer.
                sys.stdout.write(chunk.decode("utf-8", errors="replace"))
                sys.stdout.flush()

            # Track newline-terminated lines for status one-liner detection.
            for b in chunk:
                line_buf.append(b)
                if b == 10:  # '\n'
                    line = line_buf.decode("utf-8", errors="replace")
                    maybe = _extract_status_line(line)
                    if maybe:
                        status_line = maybe
                    line_buf.clear()

        # Catch a final line with no trailing newline.
        if line_buf:
            line = line_buf.decode("utf-8", errors="replace")
            maybe = _extract_status_line(line)
            if maybe:
                status_line = maybe

        proc.wait()
        rc = proc.returncode or 0

        if rc != 0 and not status_line:
            status_line = f"❌ {script_path.name} failed (exit {rc}) — see output above"

        return (rc, status_line)

    except KeyboardInterrupt:
        print("\n🛑 Script interrupted by user (Ctrl+C)")
        return (130, f"❌ {script_path.name} interrupted (Ctrl+C)")
    except Exception as e:
        print(f"❌ Script runner error: {e}")
        return (1, f"❌ {script_path.name} runner error — {e}")


# --- Book title helpers ---
def book_title_for_certificate(book_dir: Path) -> str:
    """Derive the human-friendly book title for certificates.

    Strict behaviour:
    - Expect exactly ONE .docx file directly inside `book_dir`.
    - If there are zero or more than one, raise (hard fail).

    The title is taken from the .docx filename stem (".docx" stripped), with
    underscores/hyphens turned into spaces and converted to Title Case.
    """
    docs = sorted(book_dir.glob("*.docx"), key=lambda p: p.name.lower())

    if not docs:
        raise FileNotFoundError(
            f"Expected exactly one .docx in {book_dir}, but found none. "
            "Place the manuscript .docx directly in the book folder root."
        )

    if len(docs) > 1:
        names = ", ".join(p.name for p in docs)
        raise RuntimeError(
            f"Expected exactly one .docx in {book_dir}, but found {len(docs)}: {names}"
        )

    stem = docs[0].stem  # strips .docx
    cleaned = stem.replace("_", " ").replace("-", " ").strip()
    return cleaned.title()

# --- Certificate writer helper ---
def write_correctness_certificate(book_dir: Path, summaries: list[tuple[str, str]]) -> Optional[Path]:
    """Create a PDF certificate in the book's reports folder.

    If a background design image is available, render it and overlay the dynamic text.
    Otherwise, fall back to a simple, clean certificate.

    Assets (in priority order):
      - Environment variables:
          ETNA_CERT_BG  (path to background image, e.g. PNG)
      - Repo assets folder:
          etna/assets/certificate_bg.png

    Only call this when everything has passed.
    """

    reports_dir = book_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = reports_dir / f"certificate_correctness_{book_dir.name}_{ts}.pdf"

    try:
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.units import mm
        from reportlab.lib import colors
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen import canvas
        from reportlab.pdfbase import pdfmetrics
    except Exception as e:
        print(f"⚠️  Could not generate certificate PDF (missing ReportLab?): {e}")
        return None

    # Resolve optional assets.
    scripts_dir = Path(__file__).resolve().parent
    repo_root = scripts_dir.parent  # .../etna
    assets_dir = repo_root / "assets"

    bg_env = os.environ.get("ETNA_CERT_BG")
    bg_path = Path(bg_env).expanduser() if bg_env else (assets_dir / "certificate_bg.png")

    use_bg = bg_path.exists() and bg_path.is_file()

    pagesize = landscape(A4) if use_bg else A4
    c = canvas.Canvas(str(out_path), pagesize=pagesize)
    width, height = pagesize

    # ---- Background first (if present) ----
    if use_bg:
        try:
            c.drawImage(
                ImageReader(str(bg_path)),
                0,
                0,
                width,
                height,
                preserveAspectRatio=True,
                anchor="c",
            )
        except Exception:
            # If background fails to draw, fall back to plain.
            use_bg = False

    # ---- Fallback border (only when no background) ----
    margin = 18 * mm
    if not use_bg:
        c.setStrokeColor(colors.black)
        c.setLineWidth(1)
        c.rect(margin, margin, width - 2 * margin, height - 2 * margin)


    # ---- Text overlay ----
    # When using the landscape certificate template background, place text into the pre-drawn fields.
    # Co-ordinates below are tuned for `assets/certificate_bg.png` (landscape A4).

    ink = colors.HexColor("#2f3b45")

    def truncate(text: str, font: str, size: int, max_width_pts: float) -> str:
        if pdfmetrics.stringWidth(text, font, size) <= max_width_pts:
            return text
        ell = "…"
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi) // 2
            cand = text[:mid].rstrip() + ell
            if pdfmetrics.stringWidth(cand, font, size) <= max_width_pts:
                lo = mid + 1
            else:
                hi = mid
        return text[: max(0, lo - 1)].rstrip() + ell

    def format_date(dt: datetime) -> str:
        # Stable English month names (avoids locale surprises).
        months = [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ]
        return f"{dt.day:02d} {months[dt.month - 1]} {dt.year}"

    now = datetime.now()

    if use_bg:
        # Template field positions (mm from bottom-left).
        # Tuned for the final `assets/certificate_bg.png` which already includes
        # the heading/logo/seal. Adjust these *_MM values to fine-tune alignment.

        BOOK_X_MM = 44.0
        BOOK_Y_MM = 138.0

        DATE_X_MM = 44.0
        DATE_Y_MM = 111.0

        # Checks list: start just to the right of the bullet dots.
        CHECKS_X_MM = 166.0
        CHECKS_Y0_MM = 131
        CHECKS_STEP_MM = 6.6
        MAX_CHECKS = 12

        book_x = BOOK_X_MM * mm
        book_y = BOOK_Y_MM * mm
        date_x = DATE_X_MM * mm
        date_y = DATE_Y_MM * mm

        checks_x = CHECKS_X_MM * mm
        checks_y0 = CHECKS_Y0_MM * mm
        checks_step = CHECKS_STEP_MM * mm
        max_checks = MAX_CHECKS

        # Maximum widths (points)
        book_max_w = 120 * mm
        date_max_w = 90 * mm
        checks_max_w = (width - (22 * mm)) - checks_x

        # Book title
        book_title = book_title_for_certificate(book_dir)
        c.setFillColor(ink)
        c.setFont("Helvetica-Oblique", 14)
        c.drawString(book_x, book_y, truncate(book_title, "Helvetica-Oblique", 14, book_max_w))

        # Date
        c.setFont("Helvetica", 11)
        c.drawString(date_x, date_y, truncate(format_date(now), "Helvetica", 11, date_max_w))

        # Checks list
        c.setFont("Helvetica", 10)
        y = checks_y0

        # Prefer cleaner names in the checklist.
        items = []
        for (n, _s) in summaries:
            base = n.rsplit(".", 1)[0]
            base = base.replace("_", " ").strip()
            label = "Report generated" if is_info_only_script(n) else "Passed"
            items.append(f"{base}: {label}")

        if len(items) > max_checks:
            # Fit within the template: reserve the last line for an overflow note.
            overflow = len(items) - (max_checks - 1)
            items = items[: max_checks - 1] + [f"…and {overflow} more"]

        for item in items:
            c.drawString(checks_x, y, truncate(item, "Helvetica", 10, checks_max_w))
            y -= checks_step

    else:
        # Plain fallback (portrait A4): keep the older simple certificate layout.
        # Title
        c.setFillColor(colors.black)
        c.setFont("Helvetica-Bold", 28)
        c.drawCentredString(width / 2, height - 45 * mm, "Certificate of Correctness")

        c.setFont("Helvetica", 12)
        c.drawCentredString(width / 2, height - 60 * mm, f"ETNA — Eddie Tonkoi's Narrative Analysis")

        c.setFont("Helvetica", 12)
        c.drawCentredString(width / 2, height - 72 * mm, f"Book: {book_title_for_certificate(book_dir)}")
        c.setFont("Helvetica", 11)
        c.drawCentredString(width / 2, height - 82 * mm, f"Issued: {format_date(now)}")

        c.setFont("Helvetica", 12)
        c.drawCentredString(width / 2, height - 98 * mm, "All automated checks completed with clean results.")

        c.setFont("Helvetica-Bold", 12)
        c.drawString(margin + 10 * mm, height - 120 * mm, "Checks passed:")

        c.setFont("Helvetica", 10)
        y = height - 130 * mm
        line_h = 5.5 * mm
        max_w = width - (2 * margin) - (10 * mm)

        for name, status in summaries:
            text = f"{status}  {name}"
            text = truncate(text, "Helvetica", 10, max_w)
            c.drawString(margin + 12 * mm, y, text)
            y -= line_h
            if y < margin + 25 * mm:
                c.showPage()
                c.setFont("Helvetica", 10)
                y = height - margin - 15 * mm

        # Small “PASS” seal
        c.setStrokeColor(colors.darkgreen)
        c.setLineWidth(2)
        c.circle(width - margin - 18 * mm, margin + 22 * mm, 14 * mm, stroke=1, fill=0)
        c.setFont("Helvetica-Bold", 12)
        c.setFillColor(colors.darkgreen)
        c.drawCentredString(width - margin - 18 * mm, margin + 20 * mm, "PASS")

    c.save()
    return out_path

def show_help(script_path):
    print(f"\n=== Help for {script_path.name} ===")
    try:
        if script_path.suffix == '.py':
            subprocess.run([sys.executable, str(script_path), "--help"], check=False)
        else:
            subprocess.run(["bash", str(script_path), "--help"], check=False)
    except Exception as e:
        print(f"⚠️  Error showing help: {e}")
    print("===================================\n")

def collect_script_groups(scripts_dir: Path) -> dict:
    # Discover scripts inside the selected scripts folder.
    groups = {
        "Scripts": [
            f for f in list_scripts(scripts_dir)
        ]
    }
    return groups

def script_menu(working_dir: Path, scripts_dir: Path, method=None):
    while True:
        clear_terminal()
        last_status = load_last_status(working_dir)
        script_groups = collect_script_groups(scripts_dir)
        emoji_map = {
            "Scripts": "🧰",
        }

        print("?  Show flags;   b  Back to folder menu;   q  Quit")
        print(f"Available scripts for 📂 {working_dir.name} (from 📁 {scripts_dir.name}):")

        scripts = []
        script_indices: dict[int, Union[Path, str]] = {}
        selected_scripts = []

        # Run all option (always available when scripts exist)
        if method:
            print(f"\n🌀 {method}")

        print("0. Run all scripts (sequentially)")

        i = 1
        for group, group_scripts in script_groups.items():
            if method and group != method:
                continue
            emoji = emoji_map.get(group, "📄")
            if not method:
                print(f"\n{emoji} {group}")
            for script in group_scripts:
                icon = last_status.get(script.name, NOT_RUN_ICON)
                print(f"{i}. {icon} {script.name}")
                script_indices[i] = script
                selected_scripts.append(script)
                i += 1

        # Always allow 0 to mean “run all” (as long as there are scripts).
        script_indices[0] = RUN_ALL_SENTINEL

        if not script_indices:
            print(f"\n⚠️  No scripts found in 📁 {scripts_dir.name} (expected .py or .sh).")
            input("\n⏎  Press Enter to return to the script-folder menu...")
            return "back"

        choice = input(f"\nChoose script to run in 📂 {working_dir.name} (0 = all): ").strip()

        if choice.lower() == 'q':
            sys.exit(0)
        elif choice.lower() == 'b':
            return "back"
        elif choice == '?':
            try:
                hnum = int(input("Which script number to show help for? ").strip())
                if hnum in script_indices:
                    target = script_indices[hnum]
                    if isinstance(target, Path):
                        show_help(target)
                    else:
                        print("Invalid number.")
                else:
                    print("Invalid number.")
            except ValueError:
                print("Invalid input.")
        else:
            try:
                index = int(choice)
                if index in script_indices:
                    selected = script_indices[index]
                    clear_terminal()

                    if selected == RUN_ALL_SENTINEL:
                        summaries: list[tuple[str, str]] = []  # (script_name, status_line)
                        any_bad = False
                        any_needs_review = False

                        for script in selected_scripts:
                            print(f"\n🔁 Running {script.name}...")
                            rc, status = run_script(script, working_dir)
                            line = canonical_status_line(script, rc, status)
                            update_last_status(working_dir, script.name, rc, line)

                            summaries.append((script.name, line))

                            # Gate “bad / needs review” only on non-info scripts.
                            if not is_info_only_script(script.name):
                                if line.startswith("❌"):
                                    any_bad = True
                                elif line.startswith("⚠️"):
                                    any_needs_review = True

                            # Still treat any non-zero exit as a hard failure, even for info-only scripts.
                            if rc != 0:
                                any_bad = True

                            print("")

                        print("\n— Summary —")
                        for name, line in summaries:
                            print(f"- {name}: {line}")

                        if not any_bad and not any_needs_review:
                            print("\n🎉 All required checks reported clean results.")

                            cert = write_correctness_certificate(working_dir, summaries)
                            if cert is not None:
                                print(f"📜 Certificate written to {cert}")
                        elif not any_bad and any_needs_review:
                            print("\n⚠️  Some checks suggest opening at least one report.")
                        else:
                            print("\n❌ One or more scripts failed — see output above.")

                        input("\n⏎  Press Enter to return to the script menu...")
                    else:
                        rc, status = run_script(selected, working_dir)
                        line = canonical_status_line(selected, rc, status)
                        update_last_status(working_dir, selected.name, rc, line)
                        input("\n⏎  Press Enter to return to the script menu...")
                else:
                    print("Invalid number.")
            except ValueError:
                print("Invalid input.")

def script_folder_menu(working_dir: Path):
    """Second menu: choose a folder next to pipeline.py, then list scripts inside it."""
    scripts_root = Path(__file__).resolve().parent

    while True:
        clear_terminal()
        print("📁 Choose a script folder:")
        folders = list_script_folders(scripts_root)

        if not folders:
            print("⚠️  No script folders found next to pipeline.py.")
            input("\n⏎  Press Enter to return to the book-folder menu...")
            return "back"

        folder_indices: dict[int, Path] = {}
        for i, folder in enumerate(folders, start=1):
            print(f"{i}. {folder.name}")
            folder_indices[i] = folder

        print("\nb  Back to book folders;   q  Quit")
        choice = input(f"\nSelect a script folder (1-{len(folders)}): ").strip()

        if choice.lower() == "q":
            sys.exit(0)
        if choice.lower() == "b":
            return "back"

        try:
            index = int(choice)
        except ValueError:
            input("Invalid input. Press Enter to continue...")
            continue

        if index in folder_indices:
            selected_scripts_dir = folder_indices[index]
            result = script_menu(working_dir, selected_scripts_dir)
            if result == "back":
                continue
        else:
            input("Invalid number. Press Enter to continue...")

def folder_menu():
    try:
        books_root = get_books_root_from_config()
    except Exception as e:
        print(f"⚠️  Unable to load books_root from common/config.yaml: {e}")
        sys.exit(1)

    while True:
        clear_terminal()
        print("📚 Choose a book folder:")
        valid_folders = find_valid_book_folders(books_root)

        if not valid_folders:
            print("⚠️  No valid book folders found (must contain at least one .docx).")
            sys.exit(1)

        folder_indices: dict[int, Path] = {}
        for i, folder in enumerate(valid_folders, start=1):
            print(f"{i}. {folder.name}")
            folder_indices[i] = folder

        print("\nq  Quit")
        choice = input(f"\nSelect a folder (1-{len(valid_folders)}): ").strip()

        if choice.lower() == 'q':
            sys.exit(0)

        try:
            index = int(choice)
        except ValueError:
            input("Invalid input. Press Enter to continue...")
            continue

        if index in folder_indices:
            working_dir = folder_indices[index]
            result = script_folder_menu(working_dir)
            if result == "back":
                continue
        else:
            input("Invalid number. Press Enter to continue...")

if __name__ == "__main__":
    folder_menu()
