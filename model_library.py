"""Where model profiles live: one folder per currency under config/.

    config/
        DOGE-USD/                 the DOGE-USD models, one profile per model
            discard/              models a ranking set aside, with discard_log.csv
            regimes/              models with market regimes (experimental; see regimes.py)
            best_model.json       the simulator's pick: the best DOGE-USD model it has tested
        BTC-USD/ ...
        *.json                    profiles from before currency folders: still read, and moved
                                  into their folder by organize()
        .example/                 public templates, never read as models

A folder is named after the product its models trade (`product_id`), such as DOGE-USD.
Every tool reads all of them: the simulator lists them, the model search learns from them and
writes into its currency's folder, and the assessor ranks and discards within one.
"""

import csv
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from file_lock import file_lock

CONFIG_DIR = Path(__file__).parent / "config"
DISCARD = "discard"
DISCARD_LOG = "discard_log.csv"
# Models with "regimes" (experimental) live in each currency's regimes/ folder. Branches without regime support
# list only the top of a currency folder, so they never show these models and silently trade their base rules.
REGIME_FOLDER = "regimes"
BEST_FILE = "best_model.json"
# Held while model files are added to, moved within or named in a model library, by any process.
LOCK_NAME = ".models.lock"


def library_root(path):
    """The library a folder belongs to: config/ for config/ and its currency folders, else the folder itself."""
    path = Path(path).resolve()
    root = CONFIG_DIR.resolve()
    return CONFIG_DIR if path == root or root in path.parents else path


def models_lock(path):
    """The lock that serializes changes to the library `path` belongs to."""
    return file_lock(library_root(path) / LOCK_NAME)


def currency_folder(product, config_dir=CONFIG_DIR):
    return Path(config_dir) / product


def currency_folders(config_dir=CONFIG_DIR):
    """Every currency folder in the library (any subfolder but discard/ and hidden ones such as .example/)."""
    config_dir = Path(config_dir)
    if not config_dir.is_dir():
        return []
    return [path for path in sorted(config_dir.iterdir())
            if path.is_dir() and path.name != DISCARD and not path.name.startswith(".")]


def profile_paths(config_dir=CONFIG_DIR):
    """Every model profile in the library: config/*.json, then each currency folder's *.json and its regimes/*.json."""
    config_dir = Path(config_dir)
    paths = sorted(config_dir.glob("*.json")) if config_dir.is_dir() else []
    for folder in currency_folders(config_dir):
        paths += [path for path in sorted(folder.glob("*.json")) if path.name != BEST_FILE]
        paths += sorted((folder / REGIME_FOLDER).glob("*.json"))
    return paths


def discard_folders(config_dir=CONFIG_DIR):
    """Every discard folder: config/discard/ (from before currency folders) and each currency's."""
    folders = [Path(config_dir) / DISCARD] + [folder / DISCARD for folder in currency_folders(config_dir)]
    return [folder for folder in folders if folder.is_dir()]


def discard_folder(product, config_dir=CONFIG_DIR):
    return currency_folder(product, config_dir) / DISCARD


def read_profile(path):
    """The profile at `path`, or None if it isn't a readable model profile."""
    try:
        profile = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return profile if isinstance(profile, dict) and "strategy" in profile else None


def organize(config_dir=CONFIG_DIR):
    """Move profiles at the top of the library, and in config/discard/, into their currency's folders.

    Returns (moved, skipped): lists of (name, destination) and (name, reason). A profile with
    no `product_id`, or whose name is already taken in its destination, stays where it is.
    """
    config_dir = Path(config_dir)
    moved, skipped = [], []
    with models_lock(config_dir):
        sources = [(path, None) for path in sorted(config_dir.glob("*.json"))]
        sources += [(path, DISCARD) for path in sorted((config_dir / DISCARD).glob("*.json"))]
        for path, kind in sources:
            profile = read_profile(path)
            product = (profile or {}).get("product_id")
            if not product:
                skipped.append((path.stem, "no product_id"))
                continue
            folder = currency_folder(product, config_dir) / (kind or "")
            target = folder / path.name
            if target.exists():
                skipped.append((path.stem, f"{target} already exists"))
                continue
            folder.mkdir(parents=True, exist_ok=True)
            shutil.move(path, target)
            moved.append((path.stem, target))
    return moved, skipped


# --- each currency's best model --------------------------------------------------------

def read_best(config_dir=CONFIG_DIR):
    """{product: record} for every currency with a recorded best model whose profile still exists.

    A record's "path" is the profile's location (relative to the repository for files in it).
    """
    best = {}
    for folder in currency_folders(config_dir):
        try:
            record = json.loads((folder / BEST_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        path = resolve_path(record.get("path") or folder / f"{record.get('model')}.json")
        if path.exists():
            best[folder.name] = {**record, "path": path}
    return best


def resolve_path(path):
    """A record's profile path as an absolute path (relative ones are from the repository)."""
    path = Path(path)
    return path if path.is_absolute() else CONFIG_DIR.parent / path


def portable_path(path):
    """`path` relative to the repository when it is inside it, so records survive moving the repository."""
    path = Path(path).resolve()
    try:
        return path.relative_to(CONFIG_DIR.parent.resolve()).as_posix()
    except ValueError:
        return str(path)


def update_best(product, model, path, total_return, config_dir=CONFIG_DIR, now=None):
    """Record the model as the currency's best if it beats the recorded best's return (or none is
    recorded, or the recorded model is this one). Returns True if the record changed."""
    current = read_best(config_dir).get(product)
    if current is not None and current.get("model") != model and current.get("total_return", -1e9) >= total_return:
        return False
    write_best(product, {"model": model, "path": portable_path(path), "total_return": total_return},
               config_dir, now)
    return True


def write_best(product, record, config_dir=CONFIG_DIR, now=None):
    """Record `record` (with at least "model") as the currency's best model."""
    folder = currency_folder(product, config_dir)
    folder.mkdir(parents=True, exist_ok=True)
    record = {**record, "product": product,
              "selected_utc": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S UTC")}
    temporary = folder / (BEST_FILE + ".tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    temporary.replace(folder / BEST_FILE)
    return record


def append_discard_log(folder, rows):
    """Append rows (dicts) to the discard folder's log, writing its header first if it is new."""
    columns = ["discarded_utc", "model", "product", "rank", "ranked", "passes", "goals_met", "goals_set", "score",
               "missed", "file"]
    log = Path(folder) / DISCARD_LOG
    new = not log.exists()
    with log.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        if new:
            writer.writeheader()
        writer.writerows(rows)
