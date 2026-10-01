# yofuke-sns

よふけ書店（[@yofukeshoten](https://x.com/yofukeshoten)）のSNS投稿を、
Notion で承認したものだけ Buffer 経由で X と Threads に流す。

```
Notion「SNS投稿ストック（よふけ書店）」
  │  毎夕18:55にタスクが投稿案を書く（ステータス＝ドラフト）
  │
  ├─ 人がNotionで本文を読んで「承認済み」に変える
  │
  ▼
GitHub Actions（毎時5分）
  │  承認済みの行を拾う → 「投稿処理中」にして確保
  ▼
Buffer API（GraphQL）
  ├─ X        6abe398dea19ca0bde41cc0f
  └─ Threads  6abe39b4ea19ca0bde41ccf9
  │
  ▼
Notion を「投稿済み」に戻す
```

**承認済みにしない限り、何も投稿されない。**

## ファイル

| | |
|---|---|
| `post.py` | 本体 |
| `.github/workflows/post.yml` | 毎時5分の実行と、手動実行 |

## Secrets

Settings → Secrets and variables → Actions

| 名前 | 取得元 |
|---|---|
| `NOTION_TOKEN` | https://www.notion.so/my-integrations |
| `BUFFER_API_KEY` | https://publish.buffer.com/settings/api |

Notion のインテグレーションは、作ったあと
**「SNS投稿ストック（よふけ書店）」のページで `•••` → 接続 → 追加**まで必要。
これを忘れると `object_not_found` になる。

## 最初の確認

Actions → SNS投稿 → Run workflow。
**「送らずに内容だけ表示する」にチェックを入れたまま**実行すると、
Buffer には送らず、送信内容がログに出るだけ。

内容を確認してから、チェックを外して本番実行。

## 仕様

- **予定日**が未来なら予約投稿、空か過去なら即時投稿
- **型が「問い」「事実」「棚」なのにリンクが入っていたら投稿しない**（リンク付きは表示が絞られるため）
- **画像URL**を入れると画像付きで投稿する。Bufferが取得できる公開URLであること
- 途中で落ちても、`BufferID_X` / `BufferID_Threads` が入っている側は再送しない

## ローカルで試す

```bash
export NOTION_TOKEN="..."
export BUFFER_API_KEY="..."
DRY_RUN=1 python3 post.py
```
