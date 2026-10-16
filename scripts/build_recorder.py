"""
build_recorder.py
=================
オシロ（osc_out）と AI の変換結果（ai_out）を左右に並べ、曲の音と一緒に1本の動画に録る。

  rec_fit_osc / rec_fit_ai（Fit TOP）  それぞれ 640×640 に揃える
  rec_pair（Layout TOP）                左右に並べて 1280×640
  rec_pair_out（Movie File Out TOP）    audiochop に aud_in を指定して音も焼き込む

  録画: op('/project1/rec_pair_out').par.record = True … 曲の終わりで False
  AI だけの版は ffmpeg で右半分を切り出す（crop=640:640:640:0）。

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
SIZE = 640
OUT_FILE = os.path.join(os.path.dirname(_SD), 'reference', 'take_raw.mov')
X0, Y0 = -600, -400


def build_recorder():
    p = tdb.get_parent(PARENT)
    tdb.require(p, 'osc_out', 'ai_out', 'aud_in',
                hint='build_oscilloscope.py → build_ai_bridge.py（aud_in は Audio Device In）')
    tdb.destroy(p, 'rec_fit_osc', 'rec_fit_ai', 'rec_pair', 'rec_pair_out')

    fits = []
    for i, (name, src) in enumerate((('rec_fit_osc', 'osc_out'), ('rec_fit_ai', 'ai_out'))):
        f = tdb.ensure(p, name, 'fitTOP', X0 + 160 * i, Y0)
        f.inputConnectors[0].connect(p.op(src))
        # Layout TOP は既定で入力の解像度のまま並べるので、出力解像度を明示する
        tdb.set_pars(f, {'outputresolution': 'custom', 'resolutionw': SIZE, 'resolutionh': SIZE})
        fits.append(f)

    lay = tdb.ensure(p, 'rec_pair', 'layoutTOP', X0 + 320, Y0)
    for i, f in enumerate(fits):
        lay.inputConnectors[i].connect(f)
    tdb.set_pars(lay, {'outputresolution': 'custom', 'resolutionw': SIZE * 2, 'resolutionh': SIZE})

    out = tdb.ensure(p, 'rec_pair_out', 'moviefileoutTOP', X0 + 480, Y0)
    out.inputConnectors[0].connect(lay)
    tdb.set_pars(out, {'type': 'movie', 'videocodec': 'mpeg4', 'fps': 30,
                       'audiochop': p.op('aud_in').path, 'audiocodec': 'mp3',
                       'file': OUT_FILE})

    print(f'[recorder] build complete. {lay.width}x{lay.height} -> {OUT_FILE}. '
          "record: op('/project1/rec_pair_out').par.record = True")
    return out


if __name__ == '__main__':
    build_recorder()
