"""
mock_sd_server.py
=================
GPU サーバーなしで TD 側の送受信を確かめるための、StreamDiffusion サーバーの代わり。
画像には固定の JPEG を IMG_DELAY 秒後に返し、TINDEX / PROMPT には本物と同じ形の OK を返す。

  uv run --with websockets --with pillow python scripts/mock_sd_server.py 0.06 reference/osc_modes.png
  （TD で websocket1 を active 0 → 1 → websocket1_callbacks1.module.stats_reset() → 数十秒後に stats_report()）
"""
import asyncio, base64, io, sys, websockets
from PIL import Image
IMG_DELAY = float(sys.argv[1]) if len(sys.argv) > 1 else 0.06
b = io.BytesIO(); Image.open(sys.argv[2]).convert('RGB').resize((384, 384)).save(b, 'JPEG', quality=50)
JPG = base64.b64encode(b.getvalue()).decode()
async def h(ws):
    async for m in ws:
        if m.startswith('TINDEX:'):
            await asyncio.sleep(0.025); await ws.send('OK:tindex ' + m[7:])
        elif m.startswith('PROMPT:'):
            await asyncio.sleep(0.03); await ws.send('OK:prompt updated')
        else:
            await asyncio.sleep(IMG_DELAY); await ws.send(JPG)
async def main():
    async with websockets.serve(h, '127.0.0.1', 8765, max_size=None):
        print('mock listening', IMG_DELAY, flush=True); await asyncio.Future()
asyncio.run(main())
