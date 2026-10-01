#!/usr/bin/env python3
"""
よふけ書店 — Notion で承認した投稿を Buffer 経由で X と Threads へ送る。

流れ:
    Notion の「ステータス = 承認済み」の行を拾う
      → ステータスを「投稿処理中」にして確保する（二重処理の防止）
      → Buffer の createPost で X と Threads に送る
      → 成功した側の BufferID を書き戻す
      → 両方成功したら「投稿済み」、片方でも落ちたら「投稿エラー」

環境変数:
    NOTION_TOKEN      Notion のインテグレーションのトークン（必須）
    BUFFER_API_KEY    Buffer の API キー（必須）
    DRY_RUN           "1" なら Buffer に送らず、送る内容を表示するだけ
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- 設定

NOTION_DB = "93f753ba7fcc4a008f1d5dd8d52293eb"

CHANNELS = {
    "X": os.environ.get("BUFFER_CHANNEL_X", "6abe398dea19ca0bde41cc0f"),
    "Threads": os.environ.get("BUFFER_CHANNEL_THREADS", "6abe39b4ea19ca0bde41ccf9"),
}

# Notion のプロパティ名。Notion 側で名前を変えたらここも変える。
P_STATUS = "ステータス"
P_TEXT = "本文"
P_LINK = "リンク"
P_IMAGE = "画像URL"
P_TYPE = "型"
P_TITLE = "タイトル"
P_SCHEDULED = "予定日"
P_POSTED_AT = "投稿日時"
P_ERROR = "エラー"
P_BUFFER_ID = {"X": "BufferID_X", "Threads": "BufferID_Threads"}

JST = timezone(timedelta(hours=9))
DRY_RUN = os.environ.get("DRY_RUN", "").strip() in ("1", "true", "True")

NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
BUFFER_API_KEY = os.environ.get("BUFFER_API_KEY")


# ---------------------------------------------------------------- 通信

def _request(url, payload, headers):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {e.code} from {url}: {body[:500]}") from None


def notion(path, payload=None, method="POST"):
    url = f"https://api.notion.com/v1/{path}"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    if method == "PATCH":
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="PATCH",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {e.code} from Notion: {body[:500]}") from None
    return _request(url, payload or {}, headers)


def buffer_gql(query, variables=None):
    data = _request(
        "https://api.buffer.com",
        {"query": query, "variables": variables or {}},
        {
            "Authorization": f"Bearer {BUFFER_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    if data.get("errors"):
        msgs = "; ".join(e.get("message", "?") for e in data["errors"])
        raise RuntimeError(f"Buffer GraphQL error: {msgs}")
    return data["data"]


# ---------------------------------------------------------------- Notion 読み書き

def plain(prop):
    """rich_text / title プロパティを素の文字列にする。"""
    if not prop:
        return ""
    items = prop.get("rich_text") or prop.get("title") or []
    return "".join(i.get("plain_text", "") for i in items).strip()


def select_name(prop):
    sel = (prop or {}).get("select")
    return sel["name"] if sel else None


def url_value(prop):
    return ((prop or {}).get("url") or "").strip()


def date_start(prop):
    d = (prop or {}).get("date")
    return d["start"] if d else None


def fetch_approved():
    res = notion(
        f"databases/{NOTION_DB}/query",
        {
            "filter": {"property": P_STATUS, "select": {"equals": "承認済み"}},
            "page_size": 25,
        },
    )
    return res.get("results", [])


def update_page(page_id, props):
    notion(f"pages/{page_id}", {"properties": props}, method="PATCH")


def set_status(page_id, status, **extra):
    props = {P_STATUS: {"select": {"name": status}}}
    props.update(extra)
    update_page(page_id, props)


def rich(text):
    return {"rich_text": [{"text": {"content": text[:2000]}}]}


# ---------------------------------------------------------------- Buffer 投稿

CREATE_POST = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
  }
}
"""


def create_post(channel_id, text, image_url=None, due_at=None):
    """Buffer に1件送る。成功したら __typename を返す。"""
    assets = []
    if image_url:
        assets.append({"image": {"url": image_url}})

    post_input = {
        "channelId": channel_id,
        "text": text,
        "assets": assets,
        "schedulingType": "automatic",   # Buffer が自動で公開する
        "needsApproval": False,          # 承認は Notion 側で済んでいる
    }

    if due_at:
        post_input["mode"] = "customScheduled"
        post_input["dueAt"] = due_at
    else:
        post_input["mode"] = "shareNow"

    if DRY_RUN:
        print("    [DRY RUN] 送信内容:")
        print("    " + json.dumps(post_input, ensure_ascii=False, indent=2).replace("\n", "\n    "))
        return "DryRun"

    data = buffer_gql(CREATE_POST, {"input": post_input})
    result = data.get("createPost") or {}
    typename = result.get("__typename", "")

    # Buffer はエラーも結果の型として返すことがある。名前で弾く。
    if not typename or "Error" in typename or "Failure" in typename:
        raise RuntimeError(f"Buffer が投稿を受け付けませんでした: {typename or result}")

    return typename


# ---------------------------------------------------------------- 本体

def build_text(props):
    """本文を組み立てる。記事告知の回だけ末尾にリンクを付ける。"""
    text = plain(props.get(P_TEXT))
    if not text:
        raise RuntimeError("本文が空です")

    link = url_value(props.get(P_LINK))
    post_type = select_name(props.get(P_TYPE))

    if link:
        if post_type and post_type != "記事告知":
            # 記事告知以外にリンクが入っていたら運用ミス。止める。
            raise RuntimeError(
                f"型が「{post_type}」なのにリンクが入っています。"
                "リンクは記事告知の回だけです"
            )
        text = f"{text}\n\n{link}"

    return text


def due_at_or_none(props):
    """予定日が未来なら ISO 文字列で返す。過去・未設定なら None（即時投稿）。"""
    start = date_start(props.get(P_SCHEDULED))
    if not start:
        return None
    try:
        when = datetime.fromisoformat(start)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=JST)
    if when <= datetime.now(timezone.utc) + timedelta(minutes=5):
        return None
    return when.isoformat()


def handle(page):
    page_id = page["id"]
    props = page["properties"]
    title = plain(props.get(P_TITLE)) or page_id

    print(f"\n▶ {title}")

    try:
        text = build_text(props)
    except RuntimeError as e:
        print(f"  × {e}")
        set_status(page_id, "投稿エラー", **{P_ERROR: rich(str(e))})
        return False

    image_url = url_value(props.get(P_IMAGE)) or None
    due_at = due_at_or_none(props)

    print(f"  {len(text)}字 / 画像: {'あり' if image_url else 'なし'} / "
          f"{'予約 ' + due_at if due_at else '即時'}")

    # すでに送れている側は飛ばす（途中で落ちたときの再実行に耐える）
    done = {}
    for label in CHANNELS:
        existing = plain(props.get(P_BUFFER_ID[label]))
        if existing:
            done[label] = existing
            print(f"  - {label}: 送信済み（{existing}）なので飛ばします")

    if len(done) == len(CHANNELS):
        set_status(page_id, "投稿済み",
                   **{P_POSTED_AT: {"date": {"start": datetime.now(JST).isoformat()}}})
        print("  ✓ すでに両方送信済みでした。投稿済みにしました")
        return True

    # 処理中として確保する。ここで落ちても、次回は「承認済み」を拾わない。
    if not DRY_RUN:
        set_status(page_id, "投稿処理中")

    errors = []
    for label, channel_id in CHANNELS.items():
        if label in done:
            continue
        try:
            marker = create_post(channel_id, text, image_url, due_at)
            done[label] = marker
            if not DRY_RUN:
                update_page(page_id, {P_BUFFER_ID[label]: rich(marker)})
            print(f"  ✓ {label}: 送信しました")
        except Exception as e:
            errors.append(f"{label}: {e}")
            print(f"  × {label}: {e}")

    if DRY_RUN:
        print("  [DRY RUN] Notion は更新していません")
        return not errors

    if errors:
        set_status(page_id, "投稿エラー", **{P_ERROR: rich(" / ".join(errors))})
        return False

    set_status(
        page_id, "投稿済み",
        **{
            P_POSTED_AT: {"date": {"start": datetime.now(JST).isoformat()}},
            P_ERROR: rich(""),
        },
    )
    return True


def main():
    missing = [n for n, v in (("NOTION_TOKEN", NOTION_TOKEN),
                              ("BUFFER_API_KEY", BUFFER_API_KEY)) if not v]
    if missing:
        sys.exit(f"環境変数が足りません: {', '.join(missing)}")

    if DRY_RUN:
        print("=== DRY RUN モード。Buffer には送らず、Notion も更新しません ===")

    pages = fetch_approved()
    if not pages:
        print("承認済みの投稿はありません。")
        return

    print(f"承認済み {len(pages)} 件")

    failed = 0
    for page in pages:
        try:
            if not handle(page):
                failed += 1
        except Exception as e:
            failed += 1
            print(f"  × 想定外のエラー: {e}")
            try:
                set_status(page["id"], "投稿エラー", **{P_ERROR: rich(str(e))})
            except Exception:
                pass

    print(f"\n完了。失敗 {failed} 件")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
