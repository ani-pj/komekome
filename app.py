#!/usr/bin/env python3
"""
KomeKome
- ライブ配信: YouTube内部API でリアルタイムチャット取得
- 通常動画  : YouTube Data API v3 でコメント取得
"""
import os, datetime, threading, time, re
from flask import Flask, request, jsonify, Response

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, 'static')
LOG_FILE   = os.path.join(BASE_DIR, 'comment_log.txt')

app = Flask(__name__)

# pytchat は使わないため不要
PYTCHAT_OK = True  # 常にTrue（内部API方式のため）

# ── 状態 ──────────────────────────────────────
_comments  = []          # 最新500件
_seen      = set()       # 重複防止 (id or user+text)
_lock      = threading.Lock()
_status    = {
    'mode':       'idle',   # idle / live / vod
    'video_id':   '',
    'title':      '',
    'running':    False,
    'error':      '',
    'count':      0,
    'fetch_thread': None,
}

# ── ルーティング ──────────────────────────────
@app.route('/')
def index():
    with open(os.path.join(STATIC_DIR, 'index.html'), 'rb') as f:
        return Response(f.read(), mimetype='text/html; charset=utf-8')

@app.route('/favicon.ico')
def favicon():
    return Response('', status=204)


# ── 開始 / 停止 API ───────────────────────────
@app.route('/start', methods=['POST'])
def start():
    body = request.get_json(force=True, silent=True) or {}
    video_id = body.get('video_id', '').strip()
    api_key   = body.get('api_key', '').strip()
    mode      = body.get('mode', 'live')   # 'live' or 'vod'

    if not video_id:
        return jsonify({'error': 'video_id が必要です'}), 400

    # 既存スレッドを停止
    _stop_fetch()

    with _lock:
        _status['video_id'] = video_id
        _status['mode']     = mode
        _status['running']  = True
        _status['error']    = ''
        _status['count']    = 0
        _comments.clear()
        _seen.clear()

    if mode == 'live':
        t = threading.Thread(target=_fetch_live, args=(video_id,), daemon=True)
    else:
        if not api_key:
            return jsonify({'error': 'VODモードには YouTube Data API キーが必要です'}), 400
        t = threading.Thread(target=_fetch_vod, args=(video_id, api_key), daemon=True)

    t.start()
    _status['fetch_thread'] = t
    return jsonify({'status': 'started', 'mode': mode, 'video_id': video_id})


@app.route('/stop', methods=['POST'])
def stop():
    _stop_fetch()
    return jsonify({'status': 'stopped'})


def _stop_fetch():
    with _lock:
        _status['running'] = False
        _status['mode']    = 'idle'


# ── ライブチャット取得 (YouTube内部API直接アクセス) ──
def _fetch_live(video_id):
    """
    pytchatのsignal問題を回避するため、
    YouTubeの内部チャットAPIを直接叩く方式で実装。
    """
    import urllib.request, urllib.parse, json as _json, re

    print(f'[LIVE] 開始: {video_id}')

    session = {}  # continuation, apiKey などを保持

    # ── Step1: ライブページからcontinuationトークとAPIキーを取得 ──
    def _init_session():
        url = f'https://www.youtube.com/watch?v={video_id}'
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'ja,en;q=0.9',
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode('utf-8', errors='replace')

        # ytcfg から INNERTUBE_API_KEY を取得
        m = re.search(r'"INNERTUBE_API_KEY"\s*:\s*"([^"]+)"', html)
        if not m:
            raise ValueError('INNERTUBE_API_KEY が見つかりません')
        session['api_key'] = m.group(1)

        # INNERTUBE_CONTEXT を取得
        m2 = re.search(r'"INNERTUBE_CONTEXT"\s*:\s*(\{.+?\})\s*,\s*"INNERTUBE', html)
        if m2:
            try:
                session['context'] = _json.loads(m2.group(1))
            except Exception:
                session['context'] = {"client": {"clientName": "WEB", "clientVersion": "2.20240101"}}
        else:
            session['context'] = {"client": {"clientName": "WEB", "clientVersion": "2.20240101"}}

        # continuationトークン (ライブチャット用)
        m3 = re.search(r'"continuation"\s*:\s*"([^"]+)"', html)
        if not m3:
            # ライブでない場合
            if 'isLive' not in html and 'live_stream' not in html.lower():
                raise ValueError('この動画はライブ配信中ではないようです。ライブ配信のURLを入力してください。')
            raise ValueError('チャットのcontinuationトークンが見つかりません')
        session['continuation'] = m3.group(1)

        # 動画タイトル取得
        mt = re.search(r'"title"\s*:\s*\{"runs"\s*:\s*\[.*?"text"\s*:\s*"([^"]+)"', html)
        if mt:
            with _lock:
                _status['title'] = mt.group(1)

    # ── Step2: チャットメッセージ取得 ──
    def _fetch_messages():
        url = (f'https://www.youtube.com/youtubei/v1/live_chat/get_live_chat'
               f'?key={session["api_key"]}')
        payload = _json.dumps({
            'context':      session['context'],
            'continuation': session['continuation'],
        }).encode('utf-8')
        req = urllib.request.Request(url, data=payload, headers={
            'Content-Type':  'application/json',
            'User-Agent':    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                             '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'X-YouTube-Client-Name':    '1',
            'X-YouTube-Client-Version': '2.20240101',
        }, method='POST')

        with urllib.request.urlopen(req, timeout=15) as r:
            data = _json.loads(r.read())

        messages = []
        cont_data = data.get('continuationContents', {}).get('liveChatContinuation', {})

        # 次のcontinuationトークンを更新
        conts = cont_data.get('continuations', [])
        for c in conts:
            tok = (c.get('invalidationContinuationData', {}).get('continuation') or
                   c.get('timedContinuationData', {}).get('continuation') or
                   c.get('liveChatReplayContinuationData', {}).get('continuation'))
            if tok:
                session['continuation'] = tok
                break

        # メッセージ解析
        for action in cont_data.get('actions', []):
            item = action.get('addChatItemAction', {}).get('item', {})

            # 通常メッセージ
            renderer = (item.get('liveChatTextMessageRenderer') or
                        item.get('liveChatPaidMessageRenderer') or
                        item.get('liveChatMembershipItemRenderer'))
            if not renderer:
                continue

            msg_id = renderer.get('id', '')
            author = renderer.get('authorName', {}).get('simpleText', '???')

            # メッセージテキスト
            runs = renderer.get('message', {}).get('runs', [])
            text = ''.join(r.get('text', '') for r in runs).strip()
            if not text:
                # メンバーシップ等テキストなしの場合
                header = renderer.get('headerSubtext', {}).get('runs', [])
                text = ''.join(r.get('text', '') for r in header).strip() or '[メンバーシップ]'

            # スーパーチャット判定
            is_super = 'liveChatPaidMessageRenderer' in item
            amount   = renderer.get('purchaseAmountText', {}).get('simpleText', '')

            ts_us = int(renderer.get('timestampUsec', 0))
            if ts_us:
                ts = datetime.datetime.fromtimestamp(ts_us / 1e6).strftime('%Y-%m-%d %H:%M:%S')
            else:
                ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            messages.append({
                'id':      msg_id,
                'user':    author,
                'comment': f'[{amount}] {text}' if amount else text,
                'ts':      ts,
                'type':    'superChat' if is_super else 'textMessage',
            })

        return messages

    # ── メインループ ──
    try:
        _init_session()
    except Exception as e:
        with _lock:
            _status['error']   = str(e)
            _status['running'] = False
        print(f'[LIVE] 初期化失敗: {e}')
        return

    print(f'[LIVE] チャット接続成功')
    fail_count = 0

    while True:
        with _lock:
            if not _status['running']:
                break
        try:
            messages = _fetch_messages()
            fail_count = 0

            added = []
            with _lock:
                for entry in messages:
                    key = entry['id'] or (entry['user'] + '\x00' + entry['comment'])
                    if key not in _seen:
                        _seen.add(key)
                        _comments.insert(0, entry)
                        _status['count'] += 1
                        added.append(entry)
                del _comments[500:]

            if added:
                _save_log(added)
                n = len(added)
                print(f'[LIVE] +{n}件  計{_status["count"]}件  {time.strftime("%H:%M:%S")}')

            time.sleep(1.5)

        except Exception as e:
            fail_count += 1
            print(f'[LIVE] 取得エラー ({fail_count}回目): {e}')
            if fail_count >= 5:
                with _lock:
                    _status['error']   = f'チャット取得エラー: {e}'
                    _status['running'] = False
                break
            time.sleep(3)

    print(f'[LIVE] 終了: {video_id}')


# ── VODコメント取得 (YouTube Data API v3) ──────
def _fetch_vod(video_id, api_key):
    import urllib.request, urllib.parse, json as _json

    print(f'[VOD] 開始: {video_id}')
    base = 'https://www.googleapis.com/youtube/v3/commentThreads'
    params = {
        'part':       'snippet',
        'videoId':    video_id,
        'maxResults': '100',
        'order':      'time',
        'key':        api_key,
    }

    # 動画タイトル取得
    try:
        vp = urllib.parse.urlencode({
            'part': 'snippet', 'id': video_id, 'key': api_key
        })
        with urllib.request.urlopen(
            f'https://www.googleapis.com/youtube/v3/videos?{vp}', timeout=10
        ) as r:
            vd = _json.loads(r.read())
            items = vd.get('items', [])
            title = items[0]['snippet']['title'] if items else video_id
            with _lock:
                _status['title'] = title
    except Exception:
        pass

    page_token = None
    fetched_ids = set()

    while True:
        with _lock:
            if not _status['running']:
                break

        p = dict(params)
        if page_token:
            p['pageToken'] = page_token

        try:
            url = base + '?' + urllib.parse.urlencode(p)
            with urllib.request.urlopen(url, timeout=10) as r:
                data = _json.loads(r.read())
        except Exception as e:
            with _lock:
                _status['error'] = f'API エラー: {e}'
                _status['running'] = False
            break

        if 'error' in data:
            msg = data['error'].get('message', str(data['error']))
            with _lock:
                _status['error'] = f'API エラー: {msg}'
                _status['running'] = False
            break

        new_items = []
        for item in data.get('items', []):
            cid = item['id']
            if cid in fetched_ids:
                continue
            fetched_ids.add(cid)
            s = item['snippet']['topLevelComment']['snippet']
            entry = {
                'id':      cid,
                'user':    s['authorDisplayName'],
                'comment': s['textDisplay'],
                'ts':      s['publishedAt'][:19].replace('T', ' '),
                'type':    'text',
            }
            new_items.append(entry)

        with _lock:
            for entry in reversed(new_items):
                key = entry['id']
                if key not in _seen:
                    _seen.add(key)
                    _comments.insert(0, entry)
                    _status['count'] += 1
            del _comments[500:]

        if new_items:
            _save_log(new_items)

        page_token = data.get('nextPageToken')
        if not page_token:
            # 最終ページに到達 → ポーリング間隔を置いて再取得
            print(f'[VOD] {len(fetched_ids)}件取得完了。30秒後に再確認...')
            time.sleep(30)
            page_token = None

    print(f'[VOD] 終了: {video_id}')


def _save_log(items):
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        for c in items:
            f.write(f"[{c['ts']}] {c['user']}: {c['comment']}\n")


# ── フロントエンド API ─────────────────────────
@app.route('/status')
def api_status():
    with _lock:
        return jsonify({
            'mode':     _status['mode'],
            'running':  _status['running'],
            'video_id': _status['video_id'],
            'title':    _status['title'],
            'error':    _status['error'],
            'count':    _status['count'],
        })

@app.route('/comments')
def api_comments():
    since_id = request.args.get('since_id', '')
    with _lock:
        comments = _comments[:100]
        return jsonify({
            'comments': comments,
            'total':    len(_comments),
        })

@app.route('/log')
def api_log():
    if not os.path.exists(LOG_FILE):
        return jsonify({'log': ''})
    with open(LOG_FILE, 'r', encoding='utf-8') as f:
        return jsonify({'log': f.read()})

@app.route('/log/clear', methods=['POST'])
def api_log_clear():
    if os.path.exists(LOG_FILE):
        os.remove(LOG_FILE)
    with _lock:
        _comments.clear()
        _seen.clear()
        _status['count'] = 0
    return jsonify({'status': 'cleared'})


# ── URLからvideo_idを抽出 ──
@app.route('/parse_url', methods=['POST'])
def parse_url():
    body = request.get_json(force=True, silent=True) or {}
    url  = body.get('url', '')
    vid  = _extract_video_id(url)
    if vid:
        return jsonify({'video_id': vid})
    return jsonify({'error': '動画IDを取得できませんでした'}), 400

def _extract_video_id(url):
    """YouTube URLから video_id を抽出"""
    patterns = [
        r'(?:v=|youtu\.be/|embed/|shorts/)([A-Za-z0-9_-]{11})',
        r'^([A-Za-z0-9_-]{11})$',
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


if __name__ == '__main__':
    os.makedirs(STATIC_DIR, exist_ok=True)
    print('=' * 54)
    print('  KomeKome')
    print('=' * 54)
    print('  ブラウザ: http://localhost:5000')
    print('  ライブモード: YouTube内部API')
    print('  VODモード  : 要YouTube Data API v3')
    print('=' * 54)
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
