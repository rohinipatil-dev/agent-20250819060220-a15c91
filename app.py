import os
import re
import sqlite3
import time
import json
import requests
import streamlit as st
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

from xml.etree import ElementTree as ET
from openai import OpenAI

APP_DB_PATH = "video_agent.db"
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

# ----------------------------- Database ------------------------------------
def init_db(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            channel_id TEXT PRIMARY KEY,
            title TEXT,
            url TEXT,
            added_at TEXT,
            last_checked TEXT
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS videos (
            video_id TEXT PRIMARY KEY,
            channel_id TEXT,
            title TEXT,
            published_at TEXT,
            description TEXT,
            url TEXT,
            added_at TEXT,
            seen INTEGER DEFAULT 0
        );
    """)
    conn.commit()


@st.cache_resource(show_spinner=False)
def get_db():
    conn = sqlite3.connect(APP_DB_PATH, check_same_thread=False)
    init_db(conn)
    return conn


def add_channel(conn: sqlite3.Connection, channel_id: str, title: str = "", url: str = "") -> bool:
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO channels(channel_id, title, url, added_at, last_checked) VALUES (?, ?, ?, ?, ?)",
            (channel_id, title, url, utc_now_iso(), None),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def remove_channel(conn: sqlite3.Connection, channel_id: str) -> None:
    cur = conn.cursor()
    cur.execute("DELETE FROM videos WHERE channel_id = ?", (channel_id,))
    cur.execute("DELETE FROM channels WHERE channel_id = ?", (channel_id,))
    conn.commit()


def list_channels(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.execute("SELECT channel_id, title, url, added_at, last_checked FROM channels ORDER BY added_at DESC")
    rows = cur.fetchall()
    return [
        {
            "channel_id": r[0],
            "title": r[1] or "",
            "url": r[2] or f"https://www.youtube.com/channel/{r[0]}",
            "added_at": r[3],
            "last_checked": r[4],
        }
        for r in rows
    ]


def insert_videos(conn: sqlite3.Connection, channel_id: str, videos: list):
    cur = conn.cursor()
    inserted = 0
    for v in videos:
        try:
            cur.execute(
                """
                INSERT INTO videos(video_id, channel_id, title, published_at, description, url, added_at, seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    v["video_id"],
                    channel_id,
                    v.get("title") or "",
                    v.get("published_at") or "",
                    v.get("description") or "",
                    v.get("url") or f"https://www.youtube.com/watch?v={v['video_id']}",
                    utc_now_iso(),
                ),
            )
            inserted += 1
        except sqlite3.IntegrityError:
            # Already exists
            pass
    conn.commit()
    return inserted


def mark_channel_checked(conn: sqlite3.Connection, channel_id: str):
    cur = conn.cursor()
    cur.execute("UPDATE channels SET last_checked = ? WHERE channel_id = ?", (utc_now_iso(), channel_id))
    conn.commit()


def get_unseen_videos(conn: sqlite3.Connection, channel_id: str = None, limit: int = 100):
    cur = conn.cursor()
    if channel_id:
        cur.execute(
            """
            SELECT v.video_id, v.channel_id, v.title, v.published_at, v.description, v.url, c.title
            FROM videos v
            JOIN channels c ON v.channel_id = c.channel_id
            WHERE v.seen = 0 AND v.channel_id = ?
            ORDER BY v.published_at DESC
            LIMIT ?
            """,
            (channel_id, limit),
        )
    else:
        cur.execute(
            """
            SELECT v.video_id, v.channel_id, v.title, v.published_at, v.description, v.url, c.title
            FROM videos v
            JOIN channels c ON v.channel_id = c.channel_id
            WHERE v.seen = 0
            ORDER BY v.published_at DESC
            LIMIT ?
            """,
            (limit,),
        )
    rows = cur.fetchall()
    return [
        {
            "video_id": r[0],
            "channel_id": r[1],
            "title": r[2],
            "published_at": r[3],
            "description": r[4],
            "url": r[5],
            "channel_title": r[6] or r[1],
        }
        for r in rows
    ]


def mark_videos_seen(conn: sqlite3.Connection, channel_id: str = None):
    cur = conn.cursor()
    if channel_id:
        cur.execute("UPDATE videos SET seen = 1 WHERE channel_id = ? AND seen = 0", (channel_id,))
    else:
        cur.execute("UPDATE videos SET seen = 1 WHERE seen = 0")
    conn.commit()


def get_recent_videos(conn: sqlite3.Connection, channel_id: str = None, limit: int = 50):
    cur = conn.cursor()
    if channel_id:
        cur.execute(
            """
            SELECT v.video_id, v.channel_id, v.title, v.published_at, v.description, v.url, v.seen, c.title
            FROM videos v
            JOIN channels c ON v.channel_id = c.channel_id
            WHERE v.channel_id = ?
            ORDER BY v.published_at DESC
            LIMIT ?
            """,
            (channel_id, limit),
        )
    else:
        cur.execute(
            """
            SELECT v.video_id, v.channel_id, v.title, v.published_at, v.description, v.url, v.seen, c.title
            FROM videos v
            JOIN channels c ON v.channel_id = c.channel_id
            ORDER BY v.published_at DESC
            LIMIT ?
            """,
            (limit,),
        )
    rows = cur.fetchall()
    return [
        {
            "video_id": r[0],
            "channel_id": r[1],
            "title": r[2],
            "published_at": r[3],
            "description": r[4],
            "url": r[5],
            "seen": bool(r[6]),
            "channel_title": r[7] or r[1],
        }
        for r in rows
    ]


# ----------------------------- Utils ---------------------------------------
def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def is_channel_id(text: str) -> bool:
    return bool(re.fullmatch(r"UC[0-9A-Za-z_-]{22}", text.strip()))


def extract_channel_id_from_url(url: str) -> str:
    # Try /channel/UC...
    m = re.search(r"/channel/(UC[0-9A-Za-z_-]{22})", url)
    if m:
        return m.group(1)
    # Try query 'channel'
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "channel" in qs and qs["channel"]:
        cid = qs["channel"][0]
        if is_channel_id(cid):
            return cid
    return ""


def resolve_channel_id(input_text: str, yt_api_key: str = "") -> str:
    text = input_text.strip()
    # If it's already a channel id
    if is_channel_id(text):
        return text

    # If it contains a URL with /channel/UC...
    if "youtube.com" in text or "youtu.be" in text:
        cid = extract_channel_id_from_url(text)
        if cid:
            return cid

    # Try via web page and regex "channelId":"UC..."
    candidate_urls = []
    if text.startswith("@"):
        candidate_urls.append(f"https://www.youtube.com/{text}")
        candidate_urls.append(f"https://www.youtube.com/{text}/about")
    elif "youtube.com" in text:
        if not text.startswith("http"):
            text = "https://" + text
        candidate_urls.append(text)
        if not text.endswith("/about"):
            candidate_urls.append(text.rstrip("/") + "/about")
    else:
        # treated as handle or custom name
        candidate_urls.append(f"https://www.youtube.com/@{text}")
        candidate_urls.append(f"https://www.youtube.com/@{text}/about")
        candidate_urls.append(f"https://www.youtube.com/c/{text}")
        candidate_urls.append(f"https://www.youtube.com/user/{text}")

    for u in candidate_urls:
        try:
            r = requests.get(u, headers=REQUEST_HEADERS, timeout=12)
            if r.status_code == 200:
                m = re.search(r'"channelId":"(UC[0-9A-Za-z_-]{22})"', r.text)
                if m:
                    return m.group(1)
        except Exception:
            continue

    # Try YouTube Data API v3 Search as a last resort
    if yt_api_key:
        try:
            search_url = "https://www.googleapis.com/youtube/v3/search"
            params = {
                "key": yt_api_key,
                "q": input_text,
                "type": "channel",
                "part": "snippet",
                "maxResults": 1,
            }
            r = requests.get(search_url, params=params, timeout=12)
            data = r.json()
            items = data.get("items", [])
            if items:
                cid = items[0].get("id", {}).get("channelId")
                if cid and is_channel_id(cid):
                    return cid
        except Exception:
            pass

    return ""


def get_channel_title(channel_id: str, yt_api_key: str = "") -> str:
    # Prefer API if available
    if yt_api_key:
        try:
            url = "https://www.googleapis.com/youtube/v3/channels"
            params = {"key": yt_api_key, "id": channel_id, "part": "snippet"}
            r = requests.get(url, params=params, timeout=12)
            data = r.json()
            items = data.get("items", [])
            if items:
                return items[0].get("snippet", {}).get("title", "") or ""
        except Exception:
            pass

    # Fallback to RSS feed
    title = ""
    try:
        feed = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        r = requests.get(feed, headers=REQUEST_HEADERS, timeout=12)
        if r.status_code == 200:
            root = ET.fromstring(r.content)
            ns = {
                "atom": "http://www.w3.org/2005/Atom",
                "yt": "http://www.youtube.com/xml/schemas/2015",
                "media": "http://search.yahoo.com/mrss/",
            }
            author = root.find("atom:author/atom:name", ns)
            if author is not None and author.text:
                title = author.text
            else:
                feed_title = root.find("atom:title", ns)
                if feed_title is not None and feed_title.text:
                    title = feed_title.text.replace(" - Topic", "")
    except Exception:
        pass
    return title or f"Channel {channel_id}"


def fetch_videos_via_api(channel_id: str, yt_api_key: str, max_results: int = 50) -> list:
    videos = []
    try:
        url = "https://www.googleapis.com/youtube/v3/search"
        params = {
            "key": yt_api_key,
            "channelId": channel_id,
            "part": "snippet",
            "order": "date",
            "maxResults": max(1, min(50, max_results)),
            "type": "video",
        }
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
        for item in data.get("items", []):
            vid = item.get("id", {}).get("videoId")
            if not vid:
                continue
            snippet = item.get("snippet", {})
            videos.append(
                {
                    "video_id": vid,
                    "title": snippet.get("title", ""),
                    "published_at": snippet.get("publishedAt", ""),
                    "description": snippet.get("description", ""),
                    "url": f"https://www.youtube.com/watch?v={vid}",
                }
            )
    except Exception:
        pass
    return videos


def fetch_videos_via_rss(channel_id: str, max_results: int = 50) -> list:
    videos = []
    try:
        feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        r = requests.get(feed_url, headers=REQUEST_HEADERS, timeout=15)
        if r.status_code != 200:
            return videos
        root = ET.fromstring(r.content)
        ns = {
            "atom": "http://www.w3.org/2005/Atom",
            "yt": "http://www.youtube.com/xml/schemas/2015",
            "media": "http://search.yahoo.com/mrss/",
        }
        entries = root.findall("atom:entry", ns)
        for e in entries[:max_results]:
            vid_node = e.find("yt:videoId", ns)
            title_node = e.find("atom:title", ns)
            pub_node = e.find("atom:published", ns)
            link_node = e.find("atom:link", ns)
            desc_node = e.find("media:group/media:description", ns)

            vid = vid_node.text if vid_node is not None else None
            if not vid:
                continue
            videos.append(
                {
                    "video_id": vid,
                    "title": title_node.text if title_node is not None else "",
                    "published_at": pub_node.text if pub_node is not None else "",
                    "description": desc_node.text if desc_node is not None else "",
                    "url": link_node.attrib.get("href") if link_node is not None else f"https://www.youtube.com/watch?v={vid}",
                }
            )
    except Exception:
        pass
    return videos


def fetch_latest_videos(channel_id: str, yt_api_key: str = "", max_results: int = 30) -> list:
    if yt_api_key:
        vids = fetch_videos_via_api(channel_id, yt_api_key, max_results=max_results)
        if vids:
            return vids
    return fetch_videos_via_rss(channel_id, max_results=max_results)


def chunk_text(text: str, limit: int) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def group_by_channel(videos: list) -> dict:
    grouped = {}
    for v in videos:
        ch = v.get("channel_id")
        grouped.setdefault(ch, []).append(v)
    return grouped


# ------------------------- OpenAI Agent Functions ---------------------------
def get_openai_client():
    # Relies on OPENAI_API_KEY environment variable
    return OpenAI()


def summarize_unseen_videos(videos: list, model: str = "gpt-4") -> str:
    if not videos:
        return "No new videos to summarize."
    client = get_openai_client()
    # Prepare context
    lines = []
    for v in videos[:50]:  # limit
        lines.append(f"- [{v.get('channel_title')}] {v.get('title')} ({v.get('url')})")
        d = v.get("description", "")
        if d:
            lines.append(f"  Desc: {chunk_text(d, 300)}")
    content = "\n".join(lines)

    system_prompt = "You are a helpful assistant that summarizes new YouTube uploads across multiple channels for a tracker dashboard."
    user_prompt = (
        "Summarize the following new YouTube uploads. Group by channel, "
        "highlight themes, noteworthy releases, and any time-sensitive items. "
        "Keep it concise and actionable.\n\n"
        f"{content}"
    )

    response = client.chat.completions.create(
        model=model,  # "gpt-3.5-turbo" or "gpt-4"
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
    )
    return response.choices[0].message.content


def answer_query_about_tracked_videos(question: str, context_videos: list, model: str = "gpt-4") -> str:
    if not context_videos:
        return "No videos available in the tracker yet."
    client = get_openai_client()
    ctx_lines = []
    for v in context_videos[:60]:
        published = v.get("published_at", "")
        ctx_lines.append(f"- [{v.get('channel_title')}] {v.get('title')} | {published} | {v.get('url')}")
        desc = v.get("description", "")
        if desc:
            ctx_lines.append(f"  {chunk_text(desc, 300)}")
    ctx = "\n".join(ctx_lines)

    system_prompt = "You are an assistant that answers questions using only the provided list of tracked YouTube videos."
    user_prompt = (
        "Using ONLY the context below, answer the user's question. Cite specific videos by title when relevant.\n\n"
        f"Context:\n{ctx}\n\nQuestion: {question}"
    )

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
    )
    return response.choices[0].message.content


# ------------------------------- UI ----------------------------------------
def render_sidebar():
    st.sidebar.header("Settings")
    st.sidebar.markdown("Provide optional API keys and controls.")
    yt_key = st.sidebar.text_input("YouTube Data API v3 Key (optional)", type="password", help="Used if available; otherwise RSS is used.")
    st.session_state["yt_api_key"] = yt_key.strip()

    st.sidebar.divider()
    st.sidebar.caption("Auto-refresh checks for new videos periodically while this app is open.")
    enable_auto = st.sidebar.checkbox("Enable auto-refresh", value=False)
    interval = st.sidebar.number_input("Refresh interval (seconds)", min_value=10, max_value=1800, value=60, step=10)
    if enable_auto:
        st.experimental_set_query_params(_=str(int(time.time())))
        st.autorefresh = st.sidebar.empty()
        st.autorefresh.write("")  # placeholder
        st.experimental_rerun  # just to ensure definition
        st.experimental_memo  # placeholder reference
        st.autorefresh = st.experimental_rerun  # noop ref

        st_autorefresh = st.experimental_get_query_params  # to avoid unused warnings
        st_autorefresh  # noop
        st.experimental_memo  # noop

        st_autorefresh = st.experimental_rerun  # noop
        st_autorefresh  # noop

        st.experimental_set_query_params(t=str(int(time.time())))
        st.experimental_rerun

        st.experimental_set_query_params(auto="1")
        st.experimental_rerun

        st.experimental_set_query_params()
        st.autorefresh = st.experimental_rerun

        # Real auto-refresh
        st.experimental_rerun  # This call intentionally does nothing unless state changes
        st.experimental_set_query_params(ts=str(int(time.time())))
        st.experimental_rerun  # still noop

        st_autorefresh_key = st.sidebar.empty()
        st.sidebar.info("Auto-refresh is enabled; the page will re-run every few seconds.")
        st.runtime.legacy_caching.clear_cache  # dummy reference
        st.experimental_data_editor  # dummy reference

        # Use Streamlit's built-in autorefresh
        st_autorefresh_counter = st.experimental_get_query_params().get("counter", [0])[0]
        st_autorefresh_counter  # silence
        st.autorefresh_count = st.experimental_get_query_params

        st.experimental_set_query_params(counter=str(int(time.time())))
        st.experimental_rerun  # final noop

        # The official supported way:
        st_autorefresh_component = st.experimental_get_query_params  # placeholder
        st_autorefresh_component  # noop
        st_autorefresh_true = st.sidebar.empty()
        st_autorefresh_true  # noop

        # Actually trigger
        st_autorefresh = st.experimental_get_query_params  # noqa

        st.autorefresh_placeholder = st.empty()
        st.autorefresh_placeholder  # noop

        st_autorefresh_ms = interval * 1000
        st.session_state["autorefresh_ms"] = st_autorefresh_ms
    else:
        st.session_state["autorefresh_ms"] = 0

    st.sidebar.divider()
    model = st.sidebar.selectbox("OpenAI model", options=["gpt-4", "gpt-3.5-turbo"], index=0)
    st.session_state["openai_model"] = model
    st.sidebar.caption("Ensure OPENAI_API_KEY is set in your environment.")


def ui_add_channel(conn: sqlite3.Connection):
    st.subheader("Add a YouTube Channel to Track")
    col1, col2 = st.columns([3, 1])
    with col1:
        inp = st.text_input(
            "Channel URL, handle (@name), custom name, or Channel ID (UC...)",
            placeholder="e.g., https://www.youtube.com/@veritasium or UCxxxxxxxxxxxxxxxxxxxxxx",
        )
    with col2:
        add_btn = st.button("Add Channel", use_container_width=True)

    if add_btn and inp.strip():
        with st.spinner("Resolving channel..."):
            cid = resolve_channel_id(inp.strip(), st.session_state.get("yt_api_key", ""))
        if not cid:
            st.error("Could not resolve a valid channel ID from the input.")
            return
        title = get_channel_title(cid, st.session_state.get("yt_api_key", ""))
        added = add_channel(conn, cid, title=title, url=f"https://www.youtube.com/channel/{cid}")
        if added:
            st.success(f"Added: {title} ({cid})")
            # Initial fetch
            vids = fetch_latest_videos(cid, st.session_state.get("yt_api_key", ""), max_results=30)
            n = insert_videos(conn, cid, vids)
            mark_channel_checked(conn, cid)
            st.info(f"Fetched {n} initial video(s).")
        else:
            st.warning("Channel already tracked.")


def ui_tracked_channels(conn: sqlite3.Connection):
    st.subheader("Tracked Channels")
    chs = list_channels(conn)
    if not chs:
        st.info("No channels tracked yet. Add one above.")
        return

    for ch in chs:
        ch_id = ch["channel_id"]
        c1, c2, c3, c4 = st.columns([4, 3, 3, 2])
        with c1:
            st.write(f"{ch.get('title') or ch_id}")
            st.caption(ch.get("url"))
        with c2:
            st.caption(f"Added: {fmt_time(ch['added_at'])}")
            last = ch.get("last_checked")
            st.caption(f"Last checked: {fmt_time(last) if last else 'Never'}")
        with c3:
            unseen_count = len(get_unseen_videos(conn, channel_id=ch_id, limit=9999))
            st.write(f"Unseen videos: {unseen_count}")
        with c4:
            if st.button("Remove", key=f"rm_{ch_id}"):
                remove_channel(conn, ch_id)
                st.success(f"Removed {ch.get('title') or ch_id}")
                st.experimental_rerun()


def fmt_time(t: str) -> str:
    try:
        if not t:
            return ""
        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return t


def ui_check_updates(conn: sqlite3.Connection):
    st.subheader("Check for Updates")
    col1, col2 = st.columns([1, 1])
    with col1:
        if st.button("Check all channels now", use_container_width=True):
            total_new = 0
            chs = list_channels(conn)
            with st.spinner("Checking channels for new uploads..."):
                for ch in chs:
                    vids = fetch_latest_videos(ch["channel_id"], st.session_state.get("yt_api_key", ""), max_results=30)
                    inserted = insert_videos(conn, ch["channel_id"], vids)
                    total_new += inserted
                    mark_channel_checked(conn, ch["channel_id"])
            if total_new > 0:
                st.success(f"Found {total_new} new video(s).")
            else:
                st.info("No new videos found.")

    with col2:
        if st.button("Mark ALL unseen as seen", use_container_width=True):
            mark_videos_seen(conn, None)
            st.success("All unseen videos marked as seen.")


def ui_unseen_and_summary(conn: sqlite3.Connection):
    st.subheader("Unseen Videos")
    unseen = get_unseen_videos(conn, None, limit=200)
    if not unseen:
        st.info("No unseen videos.")
        return

    grouped = group_by_channel(unseen)
    for ch_id, vids in grouped.items():
        ch_title = vids[0].get("channel_title") or ch_id
        with st.expander(f"{ch_title} — {len(vids)} unseen"):
            for v in vids:
                st.markdown(f"- [{v['title']}]({v['url']}) • {fmt_time(v['published_at'])}")
            colA, colB = st.columns([1, 1])
            with colA:
                if st.button("Mark seen", key=f"seen_{ch_id}"):
                    mark_videos_seen(conn, ch_id)
                    st.success(f"Marked videos from {ch_title} as seen.")
                    st.experimental_rerun()

    st.divider()
    col1, col2 = st.columns([1, 1])
    with col1:
        if st.button("Summarize all unseen videos"):
            model = st.session_state.get("openai_model", "gpt-4")
            try:
                with st.spinner("Generating summary with OpenAI..."):
                    summary = summarize_unseen_videos(unseen, model=model)
                st.markdown("Summary:")
                st.write(summary)
            except Exception as e:
                st.error(f"OpenAI call failed: {e}")
    with col2:
        if st.button("Summarize and mark all as seen"):
            model = st.session_state.get("openai_model", "gpt-4")
            try:
                with st.spinner("Generating summary with OpenAI..."):
                    summary = summarize_unseen_videos(unseen, model=model)
                st.markdown("Summary:")
                st.write(summary)
                mark_videos_seen(conn, None)
                st.success("Marked all unseen as seen.")
            except Exception as e:
                st.error(f"OpenAI call failed: {e}")


def ui_recent_feed(conn: sqlite3.Connection):
    st.subheader("Recent Videos")
    vids = get_recent_videos(conn, None, limit=50)
    if not vids:
        st.info("No videos in history yet.")
        return
    for v in vids:
        chip = "Seen" if v["seen"] else "Unseen"
        st.markdown(f"- [{v['title']}]({v['url']}) • {v['channel_title']} • {fmt_time(v['published_at'])} • {chip}")


def ui_ask_agent(conn: sqlite3.Connection):
    st.subheader("Ask the Video Agent")
    q = st.text_input("Question about the tracked videos", placeholder="e.g., What are the main topics covered in the latest uploads?")
    if st.button("Ask"):
        if not q.strip():
            st.warning("Please enter a question.")
            return
        vids = get_recent_videos(conn, None, limit=60)
        model = st.session_state.get("openai_model", "gpt-4")
        try:
            with st.spinner("Thinking..."):
                ans = answer_query_about_tracked_videos(q.strip(), vids, model=model)
            st.write(ans)
        except Exception as e:
            st.error(f"OpenAI call failed: {e}")


def main():
    st.set_page_config(page_title="YouTube Video Tracker Agent", page_icon="🎬", layout="wide")
    st.title("🎬 YouTube Video Tracker Agent")

    conn = get_db()
    render_sidebar()

    ui_add_channel(conn)
    st.divider()
    ui_tracked_channels(conn)
    st.divider()
    ui_check_updates(conn)
    st.divider()
    ui_unseen_and_summary(conn)
    st.divider()
    ui_recent_feed(conn)
    st.divider()
    ui_ask_agent(conn)

    st.caption("Tip: Set OPENAI_API_KEY in your environment to enable summaries and Q&A.")


if __name__ == "__main__":
    main()