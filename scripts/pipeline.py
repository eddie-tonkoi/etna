import os
import re
import subprocess
import sys
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


def _extract_status_line(line: str) -> Optional[str]:
    """Return the status one-liner if this line looks like one; else None."""
    s = (line or "").strip()
    if not s:
        return None
    return s if STATUS_LINE_RE.match(s) else None

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
                print(f"{i}. {script.name}")
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

                            if status:
                                summaries.append((script.name, status))
                                if status.startswith("❌"):
                                    any_bad = True
                                elif status.startswith("⚠️"):
                                    any_needs_review = True
                            else:
                                fallback = (
                                    f"⚠️  {script.name} finished — no status one-liner detected "
                                    "(open its report/output if unsure)"
                                )
                                summaries.append((script.name, fallback))
                                any_needs_review = True

                            if rc != 0:
                                any_bad = True

                            print("")

                        print("\n— Summary —")
                        for name, line in summaries:
                            print(f"- {name}: {line}")

                        if not any_bad and not any_needs_review:
                            print("\n🎉 All scripts reported clean results.")
                        elif not any_bad and any_needs_review:
                            print("\n⚠️  Some checks suggest opening at least one report.")
                        else:
                            print("\n❌ One or more scripts failed — see output above.")

                        input("\n⏎  Press Enter to return to the script menu...")
                    else:
                        run_script(selected, working_dir)
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
