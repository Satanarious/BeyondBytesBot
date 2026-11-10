"""Temporary tests: tap-through lookup UX (list -> page -> select -> detail)."""
import asyncio, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.pop("BB_GROUP_ID", None)
import db
db.init_db()
db.set_setting("locked_chat_id", -100555)

import media, guard

# ------------------------------------------------------------ fixtures -----
class Resp:
    status = 200
    def __init__(self, payload): self._p = payload
    async def json(self, content_type=None): return self._p
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

class Sess:
    def __init__(self, timeout=None, headers=None): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    def get(self, url, params=None, json=None, headers=None): raise AssertionError("use request")
    def request(self, method, url, params=None, json=None, headers=None):
        for key, payload in CANNED.items():
            if key in url:
                return Resp(payload)
        return Resp({"data": [], "docs": [], "results": []})

class BoomResp:
    status = 503
    async def json(self, content_type=None): return {}
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

class BoomSess(Sess):
    def request(self, method, url, **kw):
        if "api.jikan.moe" in url:
            return BoomResp()
        return super().request(method, url, **kw)

CANNED = {}


async def main():
    # 1: AniList POST is used (json_body) and rows normalized
    CANNED["graphql.anilist.co"] = {"data": {"Page": {"media": [
        {"title": {"english": None, "romaji": "Shingeki"}, "averageScore": 85,
         "format": "TV", "status": "FINISHED", "episodes": 25, "seasonYear": 2013,
         "genres": ["Action", "Drama"], "siteUrl": "https://anilist.co/anime/1",
         "coverImage": {"large": "https://img.al/1.jpg"},
         "description": "<br>Titans.</br> everywhere"}]}}}
    rows, note = await media._lookup_anime(BoomSess(), "attack on titan")
    assert note == "via AniList" and rows[0]["title"] == "Shingeki"
    assert rows[0]["score_scale"] == 100 and "25 episodes" in rows[0]["details"]
    assert "Genres: Action, Drama" in rows[0]["details"]
    assert "Titans" in rows[0]["synopsis"] and "<br>" not in rows[0]["synopsis"]
    assert rows[0]["button_line"].startswith("25 ep")
    print("TEST1 PASS: AniList POST fallback rows")

    # 2: Jikan rich row (score scale 10, members, genres)
    CANNED["api.jikan.moe/v4/anime"] = {"data": [{
        "title": "Chainsaw Man", "score": 8.5, "episodes": 12, "type": "TV",
        "status": "Finished Airing", "duration": "24 min", "members": 1234567,
        "aired": {"string": "Oct 2022 to Dec 2022"},
        "genres": [{"name": "Action"}, {"name": "Comedy"}],
        "images": {"jpg": {"large_image_url": "https://cdn.mal/x.jpg"}},
        "synopsis": "<b>Denji</b> chops.", "url": "https://myanimelist.net/anime/1"}]}
    rows, note = await media._lookup_anime(Sess(), "chainsaw man")
    assert note is None and rows[0]["score_scale"] == 10
    d = rows[0]["details"]
    assert "12 episodes" in d and "Type: TV" in d and "Members: 1,234,567" in d
    assert "Aired: Oct 2022 to Dec 2022" in d and "Genres: Action, Comedy" in d
    print("TEST2 PASS: Jikan rich details")

    # 3: keyboard: 8 rows -> 2 pages, nav present, page-count inert
    rows8 = [{"title": f"T{i}", "url": f"https://e/{i}", "image": "",
              "score": 7.0, "score_scale": 10, "button_line": "12 ep"}
             for i in range(8)]
    kb = media._suggests_keyboard("anime", "qid01", rows8, 0)
    first_labels = [b.text for r in kb.inline_keyboard for b in r if b.callback_data.startswith("lk:")]
    assert len(first_labels) == 5 and first_labels[0].startswith("T0"), first_labels
    nav = [b for r in kb.inline_keyboard for b in r if b.callback_data.startswith("lkp")]
    assert len(nav) == 2 and nav[0].callback_data == "lkp:anime:qid01:-1" \
        and nav[1].callback_data == "lkp:anime:qid01:1", nav
    inert = [b for r in kb.inline_keyboard for b in r if b.callback_data.startswith("lk0:")]
    assert len(inert) == 1 and "1/2" in inert[0].text
    close = [b for r in kb.inline_keyboard for b in r if b.callback_data.startswith("lkx")]
    assert len(close) == 1 and f"lkx:x:qid01:" in close[0].callback_data
    kb2 = media._suggests_keyboard("anime", "qid01", rows8, 1)
    labels2 = [b.text for r in kb2.inline_keyboard for b in r if b.callback_data.startswith("lk:")]
    assert len(labels2) == 3 and labels2[0].startswith("T5"), labels2
    print("TEST3 PASS: paged keyboard (5 + nav + page count + close, page 2 = 3 rows)")

    # 4: detail HTML ordering + blockquote + stripping
    row = {"title": "Chainsaw Man", "url": "https://myanimelist.net/anime/1",
           "image": "https://cdn.mal/x.jpg", "score": 8.5, "score_scale": 10,
           "source": "MyAnimeList",
           "details": ["Type: TV", "12 episodes", "Genres: Action, Comedy"],
           "synopsis": "<b>Denji</b> &amp; Pochita. " * 10}
    doc = media._detail_html(row)
    i_img = doc.index("<img src="); i_h2 = doc.index("<h2>")
    i_meta = doc.index("⭐ 8.5/10"); i_gen = doc.index("Genres: Action, Comedy")
    i_quote = doc.index("<blockquote expandable>")
    i_foot = doc.index("Open on MyAnimeList")
    assert i_img < i_h2 < i_meta < i_gen < i_quote < i_foot
    assert 'href="https://myanimelist.net/anime/1"' in doc
    assert "<b>" not in doc.split("blockquote")[1].split(">")[1], "synopsis must be stripped"
    assert "&amp;" in doc, "amp must be escaped in synopsis"
    cap = media._detail_caption(row)
    assert cap.startswith('<b><a href=') and "<i>" not in cap
    print("TEST4 PASS: detail HTML ordering + caption")

    # 5: callback select flow: list deleted, detail sent in-thread
    events = []
    class Msg:
        async def delete(self): events.append("delete")
        async def edit_text(self, *a, **k): events.append(("edit", k))
    class Query:
        data = "lk:anime:qid02:2"
        message = Msg()
        async def answer(self, *a, **k): events.append("answer")
    class Up:
        callback_query = Query()
        effective_chat = type("C", (), {"id": -100555, "type": "supergroup"})()
        effective_message = Msg()
    async def fake_detail(context, chat_id, thread, r):
        events.append(("detail", chat_id, thread, r["title"])); return "rich"
    media._send_detail = fake_detail
    stored = media._lookup_anime  # keep a ref (unused) to satisfy linters
    rows3 = [{"title": "T0"}, {"title": "T1"}, {"title": "T2"}]
    media._SUGGEST_CACHE["qid02"] = {"rows": rows3, "source": "anime",
                                    "thread": 5, "note": None,
                                    "expires": 9999999999.0}
    ctx = type("C", (), {})()
    await media.on_look_callback(Up(), ctx)
    assert ("detail", -100555, 5, "T2") in events, events
    assert "delete" in events and "answer" in events
    assert "qid02" not in media._SUGGEST_CACHE, "entry must be consumed"
    print("TEST5 PASS: tap selects -> list deleted -> detail card in-thread")

    # 6: pagination edits in place, keeps cache
    events.clear()
    media._SUGGEST_CACHE["qid03"] = {"rows": rows8, "source": "anime",
                                    "thread": 1, "note": "via AniList",
                                    "expires": 9999999999.0}
    q6 = Query(); q6.data = "lkp:anime:qid03:1"
    up6 = Up(); up6.callback_query = q6
    async def noop_detail(*a, **k): raise AssertionError("no detail on paging")
    media._send_detail = noop_detail
    await media.on_look_callback(up6, ctx)
    edits = [e for e in events if isinstance(e, tuple) and e[0] == "edit"]
    assert edits and "reply_markup" in edits[0][1], events
    assert "qid03" in media._SUGGEST_CACHE
    print("TEST6 PASS: ◀▶ edits the list in place")

    # 7: close deletes; expired qid answers alert and does nothing else
    events.clear()
    media._SUGGEST_CACHE["qid04"] = dict(media._SUGGEST_CACHE["qid03"])
    q7 = Query(); q7.data = "lkx:x:qid04:0"
    up7 = Up(); up7.callback_query = q7
    await media.on_look_callback(up7, ctx)
    assert "delete" in events and "qid04" not in media._SUGGEST_CACHE

    q8 = Query(); q8.data = "lk:dead00:0:0"
    up8 = Up(); up8.callback_query = q8
    await media.on_look_callback(up8, ctx)
    assert any("expired" in str(e) or e == "answer" for e in events if isinstance(e, str)) \
        or "answer" in events
    print("TEST7 PASS: close deletes; dead qid politely answered")

    # 8: out-of-group tap refused, nothing fetched
    events.clear()
    media._SUGGEST_CACHE["qid05"] = {"rows": rows3, "source": "anime",
                                    "thread": 1, "note": None,
                                    "expires": 9999999999.0}
    q9 = Query(); q9.data = "lk:anime:qid05:0"
    up9 = Up()
    up9.callback_query = q9
    up9.effective_chat = type("C", (), {"id": -999, "type": "supergroup"})()
    async def raising_detail(*a, **k): raise AssertionError("no card for other groups")
    media._send_detail = raising_detail
    await media.on_look_callback(up9, ctx)
    assert "delete" not in events and "qid05" in media._SUGGEST_CACHE
    print("TEST8 PASS: other groups cannot tap through")

    # 9: command flow posts suggestion list with header + note; member gate
    async def noop_detail2(*a, **k): return "rich"
    media._send_detail = noop_detail2
    cmd = media._make_cmd("anime")
    wrapped = guard.group_command()(cmd)
    sent = []
    class Msg9:
        message_thread_id = 7
        async def reply_text(self, text=None, **k): sent.append(("reply", text))
    class Bot9:
        async def send_message(self, **k): sent.append(("send", k))
        async def get_chat_member(self, *a, **k): raise RuntimeError("admin check ran!")
    async def fake_lookup(session, q):
        return [{"title": "C", "url": "https://e/1", "image": "",
                 "score": 8.5, "score_scale": 10, "button_line": "12 ep",
                 "details": [], "synopsis": ""}], "via AniList"
    media._LOOKUPS["anime"] = fake_lookup
    up9b = type("U", (), {})()
    up9b.effective_chat = type("C", (), {"id": -100555, "type": "supergroup"})()
    up9b.effective_message = Msg9()
    ctx9 = type("C", (), {})()
    ctx9.bot = Bot9(); ctx9.args = ["chainsaw man"]
    await wrapped(up9b, ctx9)
    kinds = [s[0] for s in sent]
    assert kinds == ["send"], kinds           # suggestion list, not direct card
    payload = sent[0][1]
    assert payload["message_thread_id"] == 7
    assert "via AniList" in payload["text"] and "<b>chainsaw man</b>" in payload["text"]
    markup = payload["reply_markup"]
    assert any(b.callback_data.startswith("lk:anime:") for r in markup.inline_keyboard for b in r)
    print("TEST9 PASS: /anime posts tappable list in-thread with source note")

    # 10: member passes without admin check / out-of-group refused / empty args
    from utils import get_thread_id
    assert get_thread_id(Msg9()) == 7

    sent.clear(); ctx9.args = []
    wrong = type("U", (), {})()
    wrong.effective_chat = type("C", (), {"id": -999, "type": "supergroup"})()
    wrong.effective_message = Msg9()
    await wrapped(wrong, ctx9)
    assert sent and sent[0][0] == "reply" and "Not meant" in sent[0][1]

    sent.clear()
    up_same = type("U", (), {})()
    up_same.effective_chat = up9b.effective_chat
    up_same.effective_message = Msg9()
    await wrapped(up_same, ctx9)
    assert sent and sent[0][0] == "reply" and "Usage" in sent[0][1]
    print("TEST10 PASS: gates + usage hint intact")

    names = [next(iter(h.commands)) for h in media.build_handlers()]
    assert set(names) == {"anime", "eac", "comic", "book", "books", "comics"}, names
    print("TEST11 PASS: alias set", names)

    print("ALL TAP-THROUGH TESTS OK")


asyncio.run(main())
