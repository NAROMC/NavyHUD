# NavyHUD

Minecraft などのゲーム向けの**非公式**オーバーレイHUDツールです (Windows)。
画面の上に CPS / FPS / Ping / キーストローク / マイクミュート表示を重ねて表示します。

> **Unofficial overlay HUD for Windows games (e.g. Minecraft). Shows CPS, FPS, ping, keystrokes and a mic-mute indicator.**
> Not affiliated with Mojang, Microsoft or any game server.

## 機能
- **CPS** … 左/右クリックの毎秒クリック数
- **FPS** … 実フレームレート (PresentMon使用)。1% Low / 0.1% Low / 平均も表示可
- **Ping** … 指定サーバーまでの応答時間
- **キーストローク** … WASD / スペース / マウスボタン / ダッシュ / スプリント
- **マイクミュート** … `Ctrl+Shift+M` でミュート/解除。赤●(ミュート)・緑●(解除)の表示、枠、フェードアウト、切り替え音
- 色・大きさ・文字・背景・枠・位置の細かい設定、プリセット保存、日本語/English
- HUDは `Ctrl + 左ドラッグ` で移動

## ダウンロードして使う (おすすめ)
1. [Releases](../../releases) から `NavyHUD.exe` をダウンロード
2. ダブルクリックで起動 (展開やインストールは不要です)

### 起動時の注意
- **管理者権限の確認(UAC)が出ます。** FPS測定(Windowsのイベントトレース)に必要です。「はい」を選んでください。
- **Windows SmartScreen の警告が出ることがあります。** 署名のないexeのためです。「詳細情報」→「実行」で起動できます。
  不安な場合は、このリポジトリのソースを確認し、自分でビルドすることもできます。
- ウイルス対策ソフトが誤検知することがあります (PyInstaller製のexeでは起こりやすい現象です)。

## 自分でビルドする
1. このリポジトリをダウンロード (Code → Download ZIP) して展開
2. `build.bat` をダブルクリック (Pythonが無い場合は自動で案内します)
3. 同じフォルダに `NavyHUD.exe` ができます

すぐ試すだけなら `run.bat` (ビルドなしで起動) が使えます。詳しい使い方は [README.txt](README.txt) を参照してください。

## プライバシー
- キーやマウスは「押されているか」を端末内で確認するだけで、**内容の記録・保存・送信はしません。**
- 通信するのは、Ping測定で**あなたが指定したサーバー**へ送る応答確認だけです。
- 設定は `%APPDATA%\NavyHUD` に保存されます。

## 免責事項 / Disclaimer
- 本ソフトは**無保証**で提供されます。使用は**自己責任**でお願いします。
- 非公式のファンメイドツールで、Mojang / Microsoft / 各ゲームサーバーとは**一切関係ありません**。
  Minecraft などの名称は各権利者の商標です。
- ゲームやサーバーによっては、オーバーレイやキー入力の表示を禁止・制限している場合があります。
  利用前に各ゲーム・サーバーの規約を確認してください。本ソフトの使用によるアカウント制限等について、作者は責任を負いません。
- The software is provided "as is", without warranty of any kind. Use at your own risk.

## ライセンス
[MIT License](LICENSE)。使用しているサードパーティ製ソフトについては [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES) を参照してください。
