"""
build_sections.py
=================
曲のセクションごとに、AI に渡すプロンプト・変換の強さの範囲・オシロの描き方を切り替える。

  sec_table（Table DAT）  セクションの定義（1行=1セクション）
  sec_driver（Execute DAT）毎フレーム、いまのセクションを決める → 変わったら
                           ・sec_state に強さの範囲を書く（ai_driver が読む）
                           ・osc_ctrl の mode（描き方）を替える
                           ・プロンプトを予約する（ai_driver が次の送信枠で送る）
  sec_state（Constant CHOP）index / ta_q / tb_q（無音側）/ ta_l / tb_l（大音量側）
  sec_link（Ableton Link CHOP）既に ablink があればそれを使い、無ければ作る

sec_table の列
--------------
  bars    そのセクションの長さ（小節）
  name    名前（表示用）
  mode    0=XY / 1=波形 / 2=極座標
  gain    振幅→画面の倍率（osc_ctrl の gain）。空欄なら変えない
  quiet   無音のときの t_index（"14,24" の形）。大きいほど元の形に忠実
  loud    大音量のときの t_index（"6,16"）。小さいほど AI の読み替えが強い
  prompt  そのセクションで AI に描かせるもの

どこにいるかの決め方（時計）
----------------------------
  Ableton Link に相手がいれば（numpeers≥1）Link の小節位置、いなければ内部時計
  （BPM 一定）で「cue してから何小節目か」を数える。最後のセクションの次は先頭に戻る。
  Link の小節番号は曲の頭と揃っていない（共有されるのは拍の格子だけ）ので、曲の頭で cue する。

  op('/project1/sec_driver').module.cue()      # いまを 0 小節目にする
  op('/project1/sec_driver').module.go(2)      # 手動でセクション2へ（auto を止める）
  op('/project1/sec_driver').module.auto(True) # 時計で進める状態に戻す（cue もし直す）

  Ableton のシーンに合わせて即興で切り替えるときは、go() を MIDI / OSC の受け口から呼ぶ
  （未配線。S4 の曲は Arrangement を頭から流し、auto(True) で揃える）。

前提: build_oscilloscope.py → build_ai_bridge.py を実行済み。
"""

import td

import importlib, os, sys
try:
    _SD = os.path.dirname(os.path.abspath(__file__))
except NameError:                                   # Textport へのペースト時
    _SD = os.environ.get('TD_OSC_SCRIPTS',
                         '/Users/ryookada/work/td-oscilloscope-ai/scripts')
if _SD not in sys.path:
    sys.path.insert(0, _SD)
import td_build as tdb
importlib.reload(tdb)

PARENT = '/project1'
BPM = 88.0              # Link に相手がいないときの内部時計（前作の自作曲と同じテンポ）
X0, Y0 = -600, 700

# 自作曲（S4、88BPM・各8小節 ≈ 22秒）の構成に合わせたセクション。
#   intro: 左右とも D3。右は位相90°・0.2% 高い → 円 ↔ 斜線を約3.4秒周期で行き来する
#   build: 左 D3 に対して右が2小節ごとに 3:2 → 4:3 → 2:1 → 5:4。XY でリサージュ図形が順に変わる
#   peak : 3:2 を8分で刻む＋ドラム・ベース（中央）。極座標で音の荒さがとげとげの輪になる
#   break: 左右同じ和音のアルペジオ。波形モードで和音の重なりが波の形になる
#   （極座標は 1/60 秒に D3 が2.4周しか入らず輪が閉じないので、純音の区間では使わない）
SECTIONS = [
    # bars, name,    mode, gain, quiet,   loud,    prompt
    (8, 'intro', 0, 2.6, '16,26', '10,20',
     'intricate glowing neon wire sculpture, liquid chrome filaments, dark background, cinematic light, highly detailed'),
    (8, 'build', 0, 2.6, '14,24', '8,18',
     'folded white origami paper sculpture, sharp creases, soft studio light, minimal, highly detailed'),
    (8, 'peak', 2, 1.0, '10,20', '6,16',
     'molten iridescent liquid metal splash, rainbow chrome, macro photography, dramatic light'),
    (8, 'break', 1, 2.2, '16,26', '12,22',
     'sumi-e ink brush stroke on rice paper, black ink, minimal, japanese calligraphy'),
]

DRIVER_SRC = r'''
# sec_driver: いまのセクションを決め、変わったら強さ・描き方・プロンプトを切り替える
import td
TABLE = '/project1/sec_table'
STATE = '/project1/sec_state'
LINK = '%(link)s'
CTRL = '/project1/osc_ctrl'
CB = '/project1/websocket1_callbacks1'
BPM = %(bpm)r

_cue_bar = None          # cue した時点の小節位置
_auto = True
_index = -1
log = []                 # (経過秒, index, name) 切り替えの記録


def _bars_now():
    """いまの小節位置（小数）と、どちらの時計かを返す。"""
    l = op(LINK)
    if l is not None and l['numpeers'] is not None and l['numpeers'].eval() >= 1:
        return l['bar'].eval() + l['rampbar'].eval(), 'link'
    return td.absTime.seconds * BPM / 60.0 / 4.0, 'internal'


def _rows():
    t = op(TABLE)
    out = []
    for r in range(1, t.numRows):
        out.append({c.val: t[r, c.col].val for c in t.row(0)})
    return out


def _pair(s):
    a, b = [int(x) for x in s.replace(' ', '').split(',')]
    return a, b


def cue():
    global _cue_bar
    _cue_bar, _ = _bars_now()


def auto(on=True):
    global _auto
    _auto = bool(on)
    if _auto:
        cue()


def go(i):
    """手動でセクション i へ（時計で進めるのを止める）。"""
    global _auto
    _auto = False
    _apply(int(i))


def _apply(i):
    global _index
    rows = _rows()
    if not rows:
        return
    i %%= len(rows)
    if i == _index:
        return
    _index = i
    r = rows[i]
    qa, qb = _pair(r['quiet'])
    la, lb = _pair(r['loud'])
    s = op(STATE)
    for k, v in (('index', i), ('ta_q', qa), ('tb_q', qb), ('ta_l', la), ('tb_l', lb)):
        setattr(s.par, 'value%%d' %% _STATE_ORDER.index(k), v)
    c = op(CTRL)
    if c is not None:
        c.par.value0 = int(r['mode'])
        if r.get('gain'):
            for k in range(8):
                if getattr(c.par, 'name%%d' %% k).eval() == 'gain':
                    setattr(c.par, 'value%%d' %% k, float(r['gain']))
    cb = op(CB)
    if cb is not None:
        cb.module.request_prompt(r['prompt'])
    log.append((round(td.absTime.seconds, 2), i, r['name']))
    debug('[sections] ->', i, r['name'])


_STATE_ORDER = ['index', 'ta_q', 'tb_q', 'ta_l', 'tb_l']


def current():
    return _index


def onFrameStart(frame):
    if not _auto:
        return
    if _cue_bar is None:
        cue()
    pos, _ = _bars_now()
    rows = _rows()
    total = sum(int(r['bars']) for r in rows)
    if total <= 0:
        return
    b = (pos - _cue_bar) %% total
    acc = 0
    for i, r in enumerate(rows):
        acc += int(r['bars'])
        if b < acc:
            _apply(i)
            return


def onStart():
    return


def onCreate():
    return


def onExit():
    return
'''


def build_sections():
    p = tdb.get_parent(PARENT)
    tdb.require(p, 'osc_ctrl', 'ai_driver', 'websocket1_callbacks1',
                hint='build_oscilloscope.py → build_ai_bridge.py')

    names = ['sec_table', 'sec_state', 'sec_driver']
    if p.op('ablink') is None:
        names.append('sec_link')
    tdb.destroy(p, *names)

    if p.op('ablink') is not None:
        link = p.op('ablink')
    else:
        link = tdb.ensure(p, 'sec_link', 'abletonlinkCHOP', X0, Y0 + 160)

    t = tdb.ensure(p, 'sec_table', 'tableDAT', X0, Y0)
    t.clear()
    t.appendRow(['bars', 'name', 'mode', 'gain', 'quiet', 'loud', 'prompt'])
    for row in SECTIONS:
        t.appendRow([str(v) for v in row])

    st = tdb.ensure(p, 'sec_state', 'constantCHOP', X0 + 160, Y0)
    q0, q1 = [int(x) for x in SECTIONS[0][4].split(',')]
    l0, l1 = [int(x) for x in SECTIONS[0][5].split(',')]
    for i, (k, v) in enumerate([('index', 0), ('ta_q', q0), ('tb_q', q1), ('ta_l', l0), ('tb_l', l1)]):
        tdb.set_pars(st, {f'name{i}': k, f'value{i}': v})

    src = DRIVER_SRC % {'link': link.path, 'bpm': BPM}
    drv = tdb.ensure(p, 'sec_driver', 'executeDAT', X0 + 320, Y0, text=src,
                     pars={'active': True, 'framestart': True})

    print(f'[sections] build complete. {len(SECTIONS)} sections from {t.path}, '
          f'clock: {link.path} if linked else internal {BPM:g} BPM. '
          "cue: op('/project1/sec_driver').module.cue()")
    return drv


if __name__ == '__main__':
    build_sections()
