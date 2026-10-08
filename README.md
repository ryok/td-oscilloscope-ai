# td-oscilloscope-ai

音のサンプルそのものを線で描くオシロスコープ映像を TouchDesigner で組み、自作の StreamDiffusion で
別の素材にリアルタイムで読み替える作品のリポジトリ。音が形を描き、読み替えの激しさも音が決める。

出発点は uisato の2作:
[Oscilloscopes, Everywhere](https://uisato.studio/tools/oscilloscopes-everywhere)（TD の音×幾何学プリセット集）と
[Oscilloscope Diffusion](https://uisato.studio/oscilloscope-diffusion)（SDXL によるオフラインの video-to-video）。
どちらも中身は使っておらず、XY オシロスコープという公知の技法から自作している。違いは、変換をリアルタイムで回し、
その強さを音で動かすこと。

前作: [td-organic-patterns](https://github.com/ryok/td-organic-patterns)（油膜の映像 × 自作 StreamDiffusion）

## 進み具合

| # | 内容 | 状態 |
|---|---|---|
| S1 | オシロスコープ映像（XY・波形・極座標＋残光） | 済 |
| S2 | 自作 StreamDiffusion に送り、拡散の効き方を調整 | |
| S3 | 曲のセクションごとにプロンプト・強さを切り替える | |
| S4 | スコープ用の曲を作る | |
| S5 | 本番録画と公開 | |

## 使い方

1. TouchDesigner で新規または既存のプロジェクトを開く（`/project1` を使う）
2. Textport で [scripts/build_oscilloscope.py](scripts/build_oscilloscope.py) を実行する
   （`exec(open('/path/to/scripts/build_oscilloscope.py').read())`。他の場所に置いたときは環境変数
   `TD_OSC_SCRIPTS` に scripts のパスを入れる）
3. `/project1/osc_out` を表示する。既定はテスト音（左440Hz・右660Hz）・XY モード
4. 切り替え:
   - `op('/project1/osc_src').par.index = 1` で実音へ（`/project1/aud_in` という Audio Device In CHOP があれば繋がる）
   - `op('/project1/osc_ctrl').par.value0 = 0|1|2` で XY / 波形 / 極座標

![oscilloscope modes](reference/osc_modes.png)

（左から XY・波形・極座標。上段はテスト音 440/660Hz で、XY は 2:3 のリサージュ図形。下段は実曲を
Audio File In CHOP で一時的に流した状態。）

## 設計のポイント（Script TOP + cv2 で折れ線を描く）

```
osc_tone_l (440Hz) ─┐
osc_tone_r (660Hz) ─┴ osc_tone ─┐
aud_in（あれば）─────────────────┴ osc_src ─ osc_lr ─ osc_draw ─┬─ osc_comp ─ osc_glow ─ osc_bloom ─ osc_out
                    (0=テスト音 / 1=実音)  (l, r)  (Script TOP)    │  ↑ max
                                                                 osc_fb ─ osc_decay（残光）
```

- **描き方は3つ**（`osc_ctrl` の `mode`）。XY は x=左・y=右で、ステレオの位相関係がそのまま図形になる。
  波形は x=時間・y=(左+右)/2。極座標は角度=時間・半径=0.55+0.4×振幅。
- **CHOP to SOP → Render ではなく、Script TOP の中で numpy と cv2.polylines で描く**。毎フレーム数百点の
  折れ線だけなので、カメラ・Line MAT を組むよりノードが少なく、線幅（`thick`）も1か所で変えられる。
- **1画面に描くのは 1/60 秒ぶんに固定**。音声系 CHOP はタイムスライスで「前回のクックからのぶん」を出す
  ので、普段は約735点だが、クックが飛ぶと 8820点（0.2秒ぶん）になる。量を固定しないと図形の密度がぶれる。
- **波形は一番急な上向きゼロ交差で揃える**（トリガ）。和音は1周期に上向きのゼロ交差が複数あり、
  最初の交差で揃えると、フレームごとに違う位相で始まって波形が2重3重に見えた。
- **残光は「前フレーム×decay」と今フレームを max で重ねる**（add だと重なりが白く飛ぶ）。そのあと
  ぼかしを add してにじみを足す。
- 操作値は `osc_ctrl`（Constant CHOP）: `mode` / `gain`（振幅→画面の倍率）/ `thick`（線幅px）/ `decay`（残光の残り率）

## 実装上の要点（ハマりどころ）

- **残光ループは 16bit float にする**（`osc_fb` / `osc_decay` / `osc_comp`）。8bit のままだと暗い画素が
  4×0.88=3.52→4 と丸め戻されて消えず、前のモードの線がうっすら残り続けた。
- **表示していないと残光が進まない**。TD は参照されているノードしか計算しない。`osc_out` をどこにも
  表示せずに `get_top_image` などで1枚だけ取ると、数回分しかフィードバックが回っておらず、
  前の図形が重なって見える。確認のときは一時的な Execute DAT で毎フレーム `osc_out` を cook する。
- **Audio File In CHOP の `volume=0` は出力サンプルも0にする**（スピーカーに鳴らさない設定ではない）。
  Audio File In はもともとデバイスに音を出さないので、`volume=1` のままでよい。
- **実曲は XY でほぼ右上がりの斜線になる**。ふつうのミックスは左右がほぼ同じ（モノラル寄り）なので、
  x≒y に集まる。マスタリング済みの曲は `gain=0.85` だと枠の端で切れる。図形として見せるには、
  曲側でステレオの位相を設計するか、`gain` を下げる。

## 動作環境

TouchDesigner 2023.12230（macOS）で確認。
