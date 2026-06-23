# セットアップ手順（GitHub Actions で週次自動更新）

このフォルダのファイルをリポジトリ `kanomegudesign-glitch/kyushu-events` に追加すると、
**毎週火曜の朝（JST）に自動でイベント情報を収集し、README.md を更新してコミット**します。
Claude もトークンも不要で、すべて GitHub 上で完結します。

## 配置するファイル

リポジトリのルートに、次の構成で配置してください。

```
kyushu-events/
├─ scrape.py
├─ requirements.txt
└─ .github/
   └─ workflows/
      └─ update-events.yml
```

## 追加方法（どちらか）

### 方法A：GitHubのWeb画面でアップロード（かんたん）
1. リポジトリの「Add file」→「Upload files」で `scrape.py` と `requirements.txt` をアップロード。
2. ワークフローは「Add file」→「Create new file」で、ファイル名に
   `.github/workflows/update-events.yml` と入力 → 中身を貼り付けてコミット。
   （スラッシュを入力するとフォルダが自動で作られます）

### 方法B：git コマンド
```bash
git clone https://github.com/kanomegudesign-glitch/kyushu-events.git
cd kyushu-events
# このフォルダの3ファイルを同じ構成でコピー
git add scrape.py requirements.txt .github/workflows/update-events.yml
git commit -m "feat: 週次自動更新ワークフローを追加"
git push
```

## 重要：Actions の書き込み権限を有効化（1回だけ）

自動コミットには「書き込み権限」が必要です。
1. リポジトリの **Settings → Actions → General** を開く
2. 一番下の **Workflow permissions** で
   **「Read and write permissions」** を選択 → Save

これを忘れると、収集はできてもコミットで失敗します。

## 動作確認（すぐ試す）
1. リポジトリの **Actions** タブを開く
2. 左の「週次イベント更新」を選択
3. **Run workflow** ボタンで手動実行 → 緑のチェックが付けば成功
4. README.md が更新されていれば完了。

## スケジュール
- 既定は **毎週火曜 08:00 JST**（`update-events.yml` の `cron: "0 23 * * 1"`）
- 変更したい場合はこの cron 値を編集してください（UTC基準）。

## 収集の仕組みと注意点
- 収集元：**筑後いこい**（福岡県南部）、**くまもとガイド**（熊本県北部の県北・荒尾玉名・山鹿）、
  **西日本新聞**（対象エリアのキーワードに合致するもののみ）。
- 抽出条件：今日の3日前〜60日先に重なるイベントを、エリア判定キーワードで
  「福岡県南部 / 熊本県北部 / 佐賀エリア / その他」に振り分けます。
- **久留米ファン**のカレンダー表ページは更新が止まっているため、収集対象に含めていません
  （README のリンク一覧には参考として残しています）。
- 各サイトのHTML構造が変わると、特定サイトの取得が0件になることがあります。
  その場合もワークフローは止まらず、取得できたサイトだけで README を更新します。
  取得が継続的に0件になった場合は `scrape.py` のパーサ調整が必要です（お知らせください）。

## 参加費・駐車場について
一覧ページには参加費・駐車場が載らないことが多く、自動収集では取得していません。
README には「イベント名・日程・場所・詳細・出典」を掲載し、詳細は各公式情報で確認する運用です。
（これらの項目も自動で埋めたい場合は、各イベント詳細ページまで巡回する拡張が必要です。）
