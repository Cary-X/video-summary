"""Bounded public webpage downloads and article/table extraction."""
import hashlib
import ipaddress
import socket
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from . import store, tasks, uploads


def validate_url(url):
    url = url.strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Enter a public http:// or https:// webpage URL without credentials.")
    if any(ch.isspace() for ch in url):
        raise ValueError("The URL contains whitespace.")
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("Only public internet webpages can be imported.")
    return url


def enqueue(db, url, language, route):
    url = validate_url(url)
    route.register()
    previous = db.execute("SELECT id FROM videos WHERE url=?", (url,)).fetchone()
    if previous and db.execute("SELECT 1 FROM uploaded_sources WHERE video_id=? AND kind='webpage'",
                               (previous["id"],)).fetchone() is None:
        raise ValueError("This URL is already in the video library. Open its existing task or listing.")
    video_id = store.add_video(db, url, title=urlsplit(url).hostname, target_language=language)
    existing = db.execute("SELECT id FROM processing_tasks WHERE video_id=? AND status IN ('queued','running')",
                          (video_id,)).fetchone()
    if existing:
        return existing["id"]
    with db:
        db.execute("INSERT INTO uploaded_sources(video_id,path,kind) VALUES(?,?,?) ON CONFLICT(video_id) DO NOTHING",
                   (video_id, "", "webpage"))
    return tasks.enqueue_processing_video(db, video_id, summary_route=route)


def html_blocks(content, url, *, images=None):
    import trafilatura
    from bs4 import BeautifulSoup
    from .document_reader import table_text
    soup = BeautifulSoup(content, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else urlsplit(url).hostname
    if title.strip().casefold() in {"just a moment...", "access denied", "attention required! | cloudflare"}:
        raise ValueError("The website requires browser verification. Its protection is not bypassed.")
    for tag in soup(["script", "style", "nav", "footer", "header", "form", "noscript"]):
        tag.decompose()
    main = soup.find("article") or soup.find("main") or soup
    if images is not None:
        seen = set()
        for img in main.find_all("img"):
            source = img.get("data-src") or img.get("src")
            if not source:
                continue
            link = urljoin(url, source)
            figure = img.find_parent("figure")
            caption = figure.find("figcaption") if figure else None
            label = caption.get_text(" ", strip=True) if caption else img.get("alt", "").strip()
            if link in seen or urlsplit(link).scheme not in {"http", "https"}:
                continue
            if label.casefold() in {"logo", "icon", "avatar", "advertisement", "spacer"}:
                continue
            seen.add(link)
            images.append((link, label[:300]))
        images.sort(key=lambda item: not bool(item[1]))
    # Preserve row/column association separately from article text.
    tables = []
    for table in main.find_all("table"):
        if table.parent is None:
            continue
        rows = []
        occupied = {}
        for row_index, row in enumerate(table.find_all("tr")):
            if row.find_parent("table") is not table:
                continue
            cells, column = [], 0
            for cell in row.find_all(["th", "td"], recursive=False):
                while (row_index, column) in occupied:
                    cells.append(occupied[row_index, column])
                    column += 1
                value = cell.get_text(" ", strip=True)
                def span(name):
                    try:
                        return max(1, min(int(cell.get(name, 1)), 100))
                    except (TypeError, ValueError):
                        return 1
                width, height = span("colspan"), span("rowspan")
                for offset in range(width):
                    cells.append(value if offset == 0 else "(merged)")
                    for down in range(1, height):
                        occupied[row_index + down, column + offset] = value if offset == 0 else "(merged)"
                column += width
            while (row_index, column) in occupied:
                cells.append(occupied[row_index, column])
                column += 1
            if cells:
                rows.append(cells)
        if any(any(cell.strip() for cell in row) for row in rows):
            tables.append(f"Table {len(tables) + 1} · {url}\n" + table_text(rows))
        table.decompose()
    text = trafilatura.extract(str(main), url=url, include_comments=False, include_tables=False)
    if not text:
        text = main.get_text("\n", strip=True)
    if not text.strip() and not tables and not images:
        raise ValueError("No readable article found. This page may require login or JavaScript.")
    return title, ["Source: " + url + "\nTitle: " + title + "\n" + text] + tables


def _image_blocks(session, path, images, deadline, on_progress, on_image):
    from io import BytesIO
    from PIL import Image
    import requests
    from .document_reader import read_visual_block

    blocks, seen = [], set()
    for link, label in images[:12]:
        if time.monotonic() >= deadline:
            break
        if on_progress:
            on_progress(f"Checking webpage image {len(blocks) + 1}")
        try:
            validate_url(link)
            with session.get(link, allow_redirects=False, stream=True, timeout=(5, 8)) as response:
                if response.status_code != 200:
                    continue
                data = bytearray()
                for chunk in response.iter_content(65536):
                    data.extend(chunk)
                    if len(data) > 3_000_000 or time.monotonic() >= deadline:
                        break
                else:
                    digest = hashlib.sha256(data).digest()
                    if digest in seen:
                        continue
                    with Image.open(BytesIO(data)) as image:
                        if min(image.size) < 120 or max(image.size) > 8 * min(image.size):
                            continue
                        image.thumbnail((2400, 2400))
                        saved = path.with_name(f"{path.stem}-image-{len(blocks) + 1}.png")
                        image.convert("RGB").save(saved)
                    seen.add(digest)
                    heading = f"Web image {len(blocks) + 1}" + (f" · {label}" if label else "")
                    blocks.append(read_visual_block(saved, heading + "\nSource: " + link, on_progress, on_image))
        except (ValueError, OSError, requests.RequestException, Image.DecompressionBombError):
            continue
    return blocks


def extract(url, folder: Path, *, on_progress=None, on_image=None, on_source=None):
    import requests
    folder.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 90
    with requests.Session() as session:
        session.trust_env = False
        for _ in range(6):
            url = validate_url(url)
            if on_progress:
                on_progress("Downloading webpage")
            with session.get(url, timeout=(10, 20), allow_redirects=False, stream=True,
                             headers={"User-Agent": "RaccoonAgent/1.0 (personal document reader)"}) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    url = urljoin(url, response.headers.get("Location", ""))
                    continue
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_content(65536):
                    if on_progress:
                        on_progress("Downloading webpage content")
                    data.extend(chunk)
                    if len(data) > 20_000_000 or time.monotonic() > deadline:
                        raise ValueError("Web content exceeded the 20 MB or 90-second import limit.")
                mime = response.headers.get("Content-Type", "").lower()
                suffix = ".pdf" if bytes(data[:5]) == b"%PDF-" else ".html"
                if suffix != ".pdf" and mime and not any(t in mime for t in ("text/html", "application/xhtml", "text/plain")):
                    raise ValueError("This URL is not a readable webpage or PDF.")
                path = folder / (hashlib.sha256(url.encode()).hexdigest() + suffix)
                path.write_bytes(data)
                if on_source:
                    on_source(path)
                if suffix == ".pdf":
                    return path, urlsplit(url).path.rsplit("/", 1)[-1], uploads.document_segments(
                        path, on_progress=on_progress, on_image=on_image)
                images = [] if on_image else None
                title, blocks = html_blocks(bytes(data), url, images=images)
                if images:
                    blocks.extend(_image_blocks(session, path, images, deadline, on_progress, on_image))
                return path, title, uploads.text_segments(blocks)
    raise ValueError("The webpage redirected too many times.")
