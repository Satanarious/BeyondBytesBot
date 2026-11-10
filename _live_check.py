import asyncio
import os
import sys

sys.path.insert(0, "/opt/bbbot")
os.chdir("/opt/bbbot")
import aiohttp
from dotenv import load_dotenv

import media

load_dotenv("/opt/bbbot/.env")


async def main():
    async with aiohttp.ClientSession(
        timeout=media._HTTP, headers={"User-Agent": media._UA}
    ) as s:
        rows, _note = await media._lookup_book(s, "frankenstein mary shelley")
        r = await media._ol_enrich(rows[0])
        print("BOOK top:", r["title"], "|", media._score_text(r))
        print("  author:", [d for d in r["details"] if d.startswith("By")])
        print("  synopsis:", (r["synopsis"] or "(none)")[:130])
        doc = media._detail_html(r)
        print("  blockquote:", "<blockquote expandable>" in doc)

        crows, cnote = await media._lookup_comic(s, "batman year one")
        print("COMIC note:", cnote)
        c = crows[0]
        print("  top:", c["title"], "|", c["source"])
        print("  details:", " | ".join(c["details"][:3]))
        print("  synopsis:", (c["synopsis"] or "(none)")[:130])
        print("  btn:", c["button_line"][:60])


asyncio.run(main())
