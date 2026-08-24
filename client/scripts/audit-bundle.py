"""Bundle audit — prints top contributors to the main chunk sourcemap.

Usage:
    ANALYZE=1 bun run --filter react-flow-client build && cd client && python scripts/audit-bundle.py
"""

from __future__ import annotations

import glob
import json
import os
import sys
from collections import defaultdict


def main() -> int:
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    matches = glob.glob("dist/assets/index-*.js.map")
    if not matches:
        print("no dist/assets/index-*.js.map found; run ANALYZE=1 bun run --filter react-flow-client build first")
        return 1
    # Pick the largest sourcemap — that's the main chunk, not a lazy panel.
    sourcemap_path = max(matches, key=lambda p: os.path.getsize(p))
    print(f"sourcemap: {sourcemap_path} ({os.path.getsize(sourcemap_path):,} bytes)")

    with open(sourcemap_path, encoding="utf-8") as f:
        sm = json.load(f)

    sources = sm.get("sources", [])
    sources_content = sm.get("sourcesContent") or []
    if not sources_content or len(sources_content) != len(sources):
        print("sourceMap has no sourcesContent; rebuild with sourcemap:true")
        return 2

    sized = sorted(
        ((len(c or ""), s) for s, c in zip(sources, sources_content)),
        reverse=True,
    )
    total = sum(n for n, _ in sized)
    print(f"total sources: {len(sources)}")
    print(f"total source bytes: {total:,}")
    print()

    # Per-file top contributors
    print("Top 30 largest source files in the main bundle:")
    print(f'{"bytes":>10}  {"%":>5}  path')
    print("-" * 100)
    for n, s in sized[:30]:
        normalized = s.replace("\\", "/")
        if "/node_modules/" in normalized:
            normalized = "node_modules/" + normalized.split("/node_modules/", 1)[1]
        print(f"{n:>10,}  {100 * n / total:>4.1f}%  {normalized[:90]}")

    # Bucketed by package / src subtree. bun's isolated linker stores deps
    # under a versioned store path like `node_modules/.bun/<pkg>@<ver>/
    # node_modules/<pkg>/...` — we want to collapse on the innermost
    # `node_modules/<pkg>` segment so the bucket is the actual package name.
    buckets: dict[str, int] = defaultdict(int)
    for n, s in sized:
        normalized = s.replace("\\", "/")
        if "/node_modules/" in normalized:
            # Find the LAST `/node_modules/` occurrence so we skip the
            # .bun store wrapper and land on the real package.
            after = normalized.rsplit("/node_modules/", 1)[1]
            parts = after.split("/")
            if parts[0].startswith("@") and len(parts) >= 2:
                bucket = f"{parts[0]}/{parts[1]}"
            else:
                bucket = parts[0]
        elif "/src/" in normalized:
            after = normalized.split("/src/", 1)[1]
            bucket = "src/" + after.split("/")[0]
        else:
            bucket = normalized
        buckets[bucket] += n

    print()
    print("Top 25 buckets by total source contribution:")
    print(f'{"bytes":>10}  {"%":>5}  bucket')
    print("-" * 80)
    for bucket, n in sorted(buckets.items(), key=lambda kv: -kv[1])[:25]:
        print(f"{n:>10,}  {100 * n / total:>4.1f}%  {bucket}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
