"""Temporary check: show what the Substack feeds return."""
import re
import urllib.error
import urllib.request

from substack_watcher import USER_AGENT

for url in ("https://openrss.org/substack.com/@chamath", "https://openrss.org/chamath.substack.com"):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            text = response.read(2_000_000).decode("utf-8", "replace")
            print("PROBE", url, response.status, response.headers.get("Content-Type"), len(text))
    except urllib.error.HTTPError as error:
        text = error.read(3000).decode("utf-8", "replace")
        print("PROBE", url, "HTTP", error.code, dict(error.headers).get("Retry-After"))
    print("PROBE tags", {t: len(re.findall(f"<{t}[ >]", text)) for t in ("item", "entry", "title", "guid")})
    print("PROBE head", " ".join(text[:1400].split()))
    first = re.search(r"<(item|entry)[ >].{0,1500}", text, re.S)
    print("PROBE first", " ".join(first.group(0).split()) if first else "none")
