import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.style import Style
from rich.table import Table
from rich.text import Text


def _human_size(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def _find_domain_dirs(download_root: Path) -> list[Path]:
    if not download_root.exists():
        return []
    return sorted(
        d for d in download_root.iterdir()
        if d.is_dir() and (d / "states").is_dir()
    )


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _safe_name(raw: str, width: int = 60) -> str:
    return raw if len(raw) <= width else raw[:width - 3] + "..."

_EXT = (".pdf", ".xlsx", ".xls", ".xlsm", ".xlsb")


def _is_file_url(url: str) -> bool:
    return any(url.lower().endswith(e) for e in _EXT)


def _format_dt(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return iso or "—"


def _merged_urls(runs: list[tuple[str, dict]]) -> dict[str, dict]:
    merged: dict[str, dict] = {}
    for _name, data in runs:
        for url, info in data.items():
            if url not in merged:
                merged[url] = info
    return merged


def _count_run_urls(run_data: dict) -> tuple[int, int, int, int]:
    total = len(run_data)
    files = sum(1 for u in run_data if _is_file_url(u))
    pages = total - files
    pending = sum(1 for v in run_data.values() if v.get("is_scrapped") is False)
    return total, pages, files, pending


def _read_all_run_files(domain_dir: Path) -> list[tuple[str, dict]]:
    state_dir = domain_dir / "states"
    if not state_dir.exists():
        return []
    runs = []
    for f in sorted(state_dir.iterdir()):
        if f.name.startswith("run_") and f.name.endswith(".json"):
            data = _read_json(f)
            if data:
                runs.append((f.name, data))
    return runs


def _read_all_url_files(domain_dir: Path) -> dict[str, dict]:
    merged: dict[str, dict] = {}
    state_dir = domain_dir / "states"
    if not state_dir.exists():
        return merged
    for f in state_dir.iterdir():
        if f.name.startswith("urls_") and f.name.endswith(".json"):
            data = _read_json(f)
            if data:
                merged.update(data)
    return merged


def _read_page_metas(domain_dir: Path) -> list[dict]:
    metas = []
    cache_dir = domain_dir / "caches"
    if cache_dir.exists():
        for p in cache_dir.rglob("*.meta.json"):
            m = _read_json(p)
            if m:
                metas.append(m)
    return metas


def _read_file_metas(domain_dir: Path) -> list[dict]:
    metas = []
    files_dir = domain_dir / "files"
    if files_dir.exists():
        for p in files_dir.rglob("*.meta.json"):
            m = _read_json(p)
            if m:
                metas.append(m)
    return metas


def _pick_domain(download_root: Path, console: Console) -> Optional[Path]:
    dirs = _find_domain_dirs(download_root)
    if not dirs:
        console.print("\n[bold red]✗[/] No domain directories found.")
        return None

    if len(dirs) == 1:
        return dirs[0]

    console.print()
    console.print("[bold cyan]Select a domain:[/]")
    for i, d in enumerate(dirs, 1):
        state = _read_json(d / "states" / "state.json")
        domain = state.get("domain", d.name) if state else d.name
        pages = len(list((d / "caches").rglob("*.meta.json"))) if (d / "caches").exists() else 0
        files = len(list((d / "files").rglob("*.meta.json"))) if (d / "files").exists() else 0
        last = _format_dt(state.get("last_crawl")) if state else "—"
        console.print(
            f"  [bold cyan]{i}.[/] [white]{domain}[/]  "
            f"([green]{pages}[/] pages, [yellow]{files}[/] files, "
            f"last: [dim]{last}[/])"
        )
    console.print()

    choice = Prompt.ask("[bold]Enter number[/]", default="1")
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(dirs):
            return dirs[idx]
    except (ValueError, IndexError):
        pass
    console.print("[bold red]Invalid choice.[/]")
    return None


def _render_overview(domain_dir: Path, state: dict, pages: list[dict], files: list[dict], runs: list) -> Panel:
    domain = state.get("domain", domain_dir.name) if state else domain_dir.name
    last = _format_dt(state.get("last_crawl")) if state else "—"
    max_depth = state.get("max_depth", "—") if state else "—"
    root_urls = state.get("root_urls", []) if state else []

    total_page_size = sum(m.get("size", 0) or 0 for m in pages)
    total_file_size = sum(m.get("size", 0) or 0 for m in files)

    status_counts = Counter(m.get("status_code", 200) for m in pages)
    status_parts = []
    for code, count in sorted(status_counts.items()):
        s = "green" if code < 400 else ("yellow" if code < 500 else "red")
        status_parts.append(f"[{s}]{code}[/]: {count}")
    status_summary = ", ".join(status_parts)

    merged = _merged_urls(runs)
    pending_count = sum(1 for v in merged.values() if v.get("is_scrapped") is False)
    total_discovered = len(merged)

    info = [
        f"[bold cyan]Domain:[/] [white]{domain}[/]",
        f"[bold cyan]Root URLs:[/] {len(root_urls)}",
        f"[bold cyan]Last crawl:[/] [dim]{last}[/]",
        f"[bold cyan]Max depth:[/] {max_depth}",
        f"[bold cyan]Runs:[/] [magenta]{len(runs)}[/]",
        f"[bold cyan]Total discovered:[/] {total_discovered}",
        f"[bold cyan]  └ Pending:[/] [bold yellow]{pending_count}[/]",
        f"[bold cyan]Cached pages:[/] [green]{len(pages)}[/] ({_human_size(total_page_size)})",
        f"[bold cyan]Downloaded files:[/] [yellow]{len(files)}[/] ({_human_size(total_file_size)})",
        f"[bold cyan]Statuses:[/] {status_summary}",
    ]
    return Panel("\n".join(info), title="[bold cyan]Domain Overview[/]", border_style="cyan")


def _render_discovered_urls(runs: list[tuple[str, dict]]) -> Table:
    merged = _merged_urls(runs)
    total = len(merged)
    table = Table(
        title=f"[bold]All Discovered URLs[/] [[cyan]{total}[/]]",
        title_style="bold",
        border_style="blue",
        header_style="bold cyan",
    )
    table.add_column("URL", style="cyan", no_wrap=True)
    table.add_column("Depth", justify="right")
    table.add_column("Type")
    table.add_column("Status")
    table.add_column("Discovered At")

    for url in sorted(merged.keys()):
        info = merged[url]
        is_file = _is_file_url(url)
        ftype = "[bold green]FILE[/]" if is_file else "[bold blue]PAGE[/]"
        scrapped = info.get("is_scrapped")
        if scrapped is True:
            status = "[green]✓ DONE[/]"
        elif scrapped is False:
            status = "[bold yellow]○ PENDING[/]"
        else:
            status = "[dim]—[/]"
        d = info.get("depth")
        d_str = f"[magenta]{d}[/]" if d is not None else "[dim]—[/]"
        table.add_row(
            _safe_name(url, 80),
            d_str,
            ftype,
            status,
            _format_dt(info.get("discovered_at")),
        )
    return table


def _render_file_types(files: list[dict]) -> Table:
    by_type: dict[str, list[int]] = defaultdict(list)
    for m in files:
        ext = m.get("extension", "unknown").lower()
        s = m.get("size", 0) or 0
        by_type[ext].append(s)

    ext_colors = {"pdf": "red", "xlsx": "green", "xls": "green", "xlsm": "green", "xlsb": "green"}

    table = Table(
        title=f"[bold]Files by Type[/] [[yellow]{len(files)}[/] total]",
        title_style="bold",
        border_style="yellow",
        header_style="bold yellow",
    )
    table.add_column("Extension", style="green")
    table.add_column("Count", justify="right")
    table.add_column("Total Size", justify="right")
    table.add_column("Average Size", justify="right")
    table.add_column("Min Size", justify="right")
    table.add_column("Max Size", justify="right")

    for ext in sorted(by_type.keys()):
        sizes = by_type[ext]
        color = ext_colors.get(ext, "white")
        table.add_row(
            f"[bold {color}].{ext}[/]",
            str(len(sizes)),
            _human_size(sum(sizes)),
            _human_size(sum(sizes) // len(sizes)),
            _human_size(min(sizes)),
            _human_size(max(sizes)),
        )
    return table


def _render_file_details(files: list[dict]) -> Table:
    ext_colors = {"pdf": "red", "xlsx": "green", "xls": "green", "xlsm": "green", "xlsb": "green"}

    table = Table(
        title="[bold]Cached Files[/]",
        title_style="bold",
        border_style="green",
        header_style="bold green",
    )
    table.add_column("Name", style="cyan")
    table.add_column("Type")
    table.add_column("Size", justify="right")
    table.add_column("Depth", justify="right")
    table.add_column("URL")

    for m in sorted(files, key=lambda x: x.get("name", "")):
        name = m.get("orig_name") or m.get("name", "—")
        ext = m.get("extension", "?")
        color = ext_colors.get(ext, "white")
        d = m.get("depth")
        d_str = f"[magenta]{d}[/]" if d is not None else "[dim]—[/]"
        table.add_row(
            _safe_name(name, 40),
            f"[{color}].{ext}[/]",
            _human_size(m.get("size", 0) or 0),
            d_str,
            _safe_name(m.get("url", ""), 70),
        )
    return table


def _render_pending(runs: list[tuple[str, dict]]) -> Table:
    pending: dict[str, dict] = {}
    for _name, data in runs:
        for url, info in data.items():
            if info.get("is_scrapped") is False:
                if url not in pending:
                    pending[url] = info

    count = len(pending)
    color = "green" if count == 0 else ("yellow" if count < 10 else "bold red")

    table = Table(
        title=f"[bold]Pending URLs[/] [[{color}]{count}[/]]",
        title_style="bold",
        border_style="yellow",
        header_style="bold yellow",
    )
    table.add_column("URL", style="yellow", no_wrap=True)
    table.add_column("Depth", justify="right")
    table.add_column("Discovered At")

    for url in sorted(pending.keys()):
        info = pending[url]
        d = info.get("depth")
        d_str = f"[magenta]{d}[/]" if d is not None else "[dim]—[/]"
        table.add_row(
            _safe_name(url, 80),
            d_str,
            _format_dt(info.get("discovered_at")),
        )
    return table


def _render_last_run(runs: list[tuple[str, dict]], url_data: dict) -> Table:
    if not runs:
        return Table(title="[bold]Last Run[/]", title_style="bold")

    name, data = runs[-1]
    total = len(data)
    pending = sum(1 for v in data.values() if v.get("is_scrapped") is False)
    completed_count = sum(1 for v in data.values() if v.get("is_scrapped") is True)
    files_count = sum(1 for u in data if _is_file_url(u))
    pages = total - files_count
    in_url_index = sum(1 for u in data if u in url_data)

    pending_color = "green" if pending == 0 else ("yellow" if pending < 10 else "bold red")

    table = Table(
        title=f"[bold]Last Run[/] [[cyan]{name}[/]]",
        title_style="bold",
        border_style="cyan",
        header_style="bold cyan",
    )
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")

    table.add_row("Total URLs", f"[bold]{total}[/]")
    table.add_row("Pages", f"[blue]{pages}[/]")
    table.add_row("Files", f"[yellow]{files_count}[/]")
    table.add_row("Pending", f"[{pending_color}]{pending}[/]")
    table.add_row("Completed", f"[green]{completed_count}[/]")
    table.add_row("In URL index", f"[dim]{in_url_index}[/]")
    return table


def _render_depth_distribution(runs: list[tuple[str, dict]]) -> Table:
    merged = _merged_urls(runs)
    if not merged:
        return Table(title="[bold]URL Depth Distribution[/]", title_style="bold")

    counts: Counter = Counter()
    for v in merged.values():
        d = v.get("depth")
        if d is not None:
            counts[d] += 1

    depth_colors = ["cyan", "blue", "green", "yellow", "magenta", "red"]

    table = Table(
        title="[bold]URL Depth Distribution[/]",
        title_style="bold",
        border_style="purple",
        header_style="bold magenta",
    )
    table.add_column("Depth", justify="right", style="cyan")
    table.add_column("Count", justify="right")
    table.add_column("Bar")

    total = sum(counts.values())
    bar_width = 30
    for depth in sorted(counts.keys()):
        count = counts[depth]
        bar_len = max(1, int(count / total * bar_width)) if total > 0 else 0
        color = depth_colors[depth % len(depth_colors)]
        bar = "█" * bar_len
        table.add_row(
            f"[bold]{depth}[/]",
            str(count),
            f"[{color}]{bar} [dim]{count}/{total}[/][/]",
        )
    return table


def _render_status_distribution(pages: list[dict]) -> Table:
    counts: Counter[int] = Counter()
    for m in pages:
        counts[m.get("status_code", 200)] += 1

    if not counts:
        return Table(title="[bold]Page Status Distribution[/]", title_style="bold")

    table = Table(
        title="[bold]Page Status Distribution[/]",
        title_style="bold",
        border_style="green",
        header_style="bold green",
    )
    table.add_column("Status Code", justify="right", style="cyan")
    table.add_column("Count", justify="right")
    table.add_column("Bar")

    total = sum(counts.values())
    bar_width = 30
    for code in sorted(counts.keys()):
        count = counts[code]
        bar_len = max(1, int(count / total * bar_width)) if total > 0 else 0
        bar = "█" * bar_len
        style = "green" if code < 400 else ("yellow" if code < 500 else "bold red")
        table.add_row(str(code), str(count), f"[{style}]{bar} [dim]{count}[/][/]")
    return table


def _last_activity(data: dict) -> str:
    latest = None
    for info in data.values():
        dt = info.get("discovered_at")
        if dt:
            if not latest or dt > latest:
                latest = dt
    return _format_dt(latest)


def _render_timeline(runs: list[tuple[str, dict]]) -> Table:
    if not runs:
        return Table(title="[bold]Crawl Timeline[/]", title_style="bold")

    table = Table(
        title="[bold]Crawl Timeline[/]",
        title_style="bold",
        border_style="dim",
        header_style="bold white",
    )
    table.add_column("Run File", style="cyan")
    table.add_column("Date")
    table.add_column("Total URLs", justify="right")
    table.add_column("Pages", justify="right")
    table.add_column("Files", justify="right")
    table.add_column("Pending", justify="right")
    table.add_column("Last Activity")

    for name, data in runs:
        total, pages, files, pending = _count_run_urls(data)
        date_str = name.replace("run_", "").replace(".json", "")
        pending_str = f"[yellow]{pending}[/]" if pending > 0 else f"[green]{pending}[/]"
        last = _last_activity(data)
        table.add_row(name, date_str, str(total), str(pages), str(files), pending_str, last)
    return table


def _render_summary(domain_dir: Path, pages: list[dict], files: list[dict]) -> None:
    if not (domain_dir / "states" / "state.json").exists():
        return
    state = _read_json(domain_dir / "states" / "state.json")
    if not state:
        return
    runs = _read_all_run_files(domain_dir)
    url_data = _read_all_url_files(domain_dir)

    tables = [
        _render_overview(domain_dir, state, pages, files, runs),
        _render_file_types(files),
        _render_last_run(runs, url_data),
        _render_depth_distribution(runs),
        _render_status_distribution(pages),
        _render_timeline(runs),
    ]

    console = Console()
    for t in tables:
        console.print()
        console.print(t)
    console.print()


def run_stat(download_root: Path) -> None:
    console = Console()
    domain_dir = _pick_domain(download_root, console)
    if not domain_dir:
        return

    pages = _read_page_metas(domain_dir)
    files = _read_file_metas(domain_dir)
    _render_summary(domain_dir, pages, files)
