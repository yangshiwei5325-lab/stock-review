#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
飞书推送模块 / 命令行工具  (feishu_push.py)
=========================================================
把 stock_review.py 生成的 .summary.json 渲染成飞书卡片，
推送到飞书群（自定义机器人）或指定的人（自建应用）。

支持的渠道
----------
  feishu      飞书自定义机器人 Webhook（推荐：免认证、2 分钟搞定、可在定时任务里跑）
  feishu_app  飞书自建应用（需 app_id/app_secret，可发给指定人/群）
  custom      任意 webhook（POST JSON，支持 {{title}} / {{content}} 占位符）

命令行用法
----------
  python feishu_push.py                                  # 推最新一份摘要
  python feishu_push.py --summary reports/xxx.summary.json
  python feishu_push.py --summary xxx.json --dry-run      # 只打印卡片，不发送
  python feishu_push.py --test                            # 发一条测试卡片
  python feishu_push.py --text "自定义消息"
  python feishu_push.py --set-webhook "地址"              # 写配置，免手改 JSON
  python feishu_push.py --set-once-per-day true           # 改成每天只推一次

推送次数：默认**不限次数**，同一天想推几次推几次。
若把配置里的 once_per_day 设为 true，则同一交易日只推一次（--force 可强推）。

配置：同目录 push_config.json（可用 --config 指定），也支持环境变量覆盖：
  FEISHU_WEBHOOK / FEISHU_SECRET / FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_PUSH_PROVIDER
兼容 Python 2.7 与 Python 3.x。
"""

from __future__ import print_function, unicode_literals

import os
import sys
import json
import time
import hmac
import base64
import codecs
import hashlib
import argparse

PY3 = sys.version_info[0] >= 3
if PY3:
    from urllib.request import Request, urlopen
    from urllib.parse import quote_plus
else:
    from urllib2 import Request, urlopen
    from urllib import quote_plus


def ensure_utf8_stdout():
    """Python 2 下让 print 支持中文（幂等，重复调用不会二次包装）。

    注意：不能用 .encoding 判断是否已包装。codecs.StreamWriter 通过 __getattr__
    把未定义的属性代理给内层流，读到的仍是原始流的 encoding（重定向时常常是
    None），于是会被反复包装；二次包装后写中文会在内层 encode 时按 ascii 解码，
    抛 UnicodeDecodeError。这里改用自定义标记做幂等判断。
    """
    if PY3:
        return
    try:
        so = sys.stdout
        if getattr(so, '_utf8_wrapped', False):
            return
        if 'utf' in (getattr(so, 'encoding', '') or '').lower():
            return
        w = codecs.getwriter('utf-8')(so)
        w._utf8_wrapped = True
        sys.stdout = w
    except Exception:
        pass


ensure_utf8_stdout()

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) stock-review-pusher/1.0'
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, 'push_config.json')
STATE_FILE = os.path.join(HERE, '.cache', 'push_state.json')

PROVIDERS = ('feishu', 'feishu_app', 'custom')
FEISHU_HOOK = 'https://open.feishu.cn/open-apis/bot/v2/hook/%s'


def _u(s):
    """命令行/配置里可能拿到字节串，统一解码成 unicode（Python 2 需要）。"""
    if not isinstance(s, bytes):
        return s
    for enc in ('utf-8', sys.getfilesystemencoding(), 'gbk'):
        if not enc:
            continue
        try:
            return s.decode(enc)
        except Exception:
            continue
    return s.decode('utf-8', 'ignore')


# ================================================================ HTTP
def form_body(data):
    """手工做 x-www-form-urlencoded 编码，避免 Python 2 下中文报错。"""
    items = []
    for k, v in (data or {}).items():
        if v is None:
            v = ''
        kb = k if isinstance(k, bytes) else ('%s' % k).encode('utf-8')
        vb = v if isinstance(v, bytes) else ('%s' % v).encode('utf-8')
        items.append('%s=%s' % (quote_plus(kb), quote_plus(vb)))
    return '&'.join(items)


def _ascii(s):
    """把任意字符串压成纯 ASCII，避免异常信息本身在 Python 2 下再次抛编码错。"""
    if isinstance(s, bytes):
        s = s.decode('utf-8', 'replace')
    return s.encode('ascii', 'replace').decode('ascii')


def http_post(url, json_body=None, data=None, headers=None, timeout=20, retries=3):
    """POST，返回 (status, 文本)。网络层失败自动重试。

    注意：JSON body 一律用 ensure_ascii=True 序列化。带中文的原始 UTF-8 body
    在经过某些网络中间层时会被重新序列化并抛 UnicodeDecodeError；用 \\uXXXX
    转义后是纯 ASCII，飞书服务端解析结果完全一致。
    """
    hdrs = {'User-Agent': UA}
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode('utf-8')
        hdrs['Content-Type'] = 'application/json; charset=utf-8'
    elif data is not None:
        body = form_body(data).encode('utf-8')
        hdrs['Content-Type'] = 'application/x-www-form-urlencoded; charset=utf-8'
    if headers:
        hdrs.update(headers)

    last = None
    for i in range(retries):
        try:
            resp = urlopen(Request(url, data=body, headers=hdrs), timeout=timeout)
            return (getattr(resp, 'status', None) or resp.getcode(),
                    resp.read().decode('utf-8', 'ignore'))
        except Exception as e:
            txt = None
            try:                       # HTTP 4xx/5xx 也带响应体，尽量读出来
                txt = e.read().decode('utf-8', 'ignore')
            except Exception:
                pass
            if txt:
                return getattr(e, 'code', 0), txt
            last = e
            time.sleep(1.0 * (i + 1))
    raise Exception('network error after %d retries: %s: %s'
                    % (retries, _ascii(type(last).__name__), _ascii('%s' % (last,))))


def gen_sign(timestamp, secret):
    """飞书自定义机器人「签名校验」：key = timestamp\\nsecret，取 HMAC-SHA256 的 base64。"""
    string_to_sign = '%s\n%s' % (timestamp, secret)
    if not isinstance(string_to_sign, bytes):
        string_to_sign = string_to_sign.encode('utf-8')
    digest = hmac.new(string_to_sign, digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode('utf-8')


# ================================================================ 配置
def load_config(path=None, raw=False):
    """读配置：文件 + 环境变量（环境变量优先）。raw=True 时只读文件，不合并环境变量。"""
    cfg = {}
    p = path or DEFAULT_CONFIG
    if os.path.isfile(p):
        try:
            with codecs.open(p, 'r', 'utf-8') as fp:
                cfg = json.load(fp) or {}
        except Exception as e:
            print('[!] 配置文件解析失败 %s: %s' % (p, e))
    if raw:
        return cfg
    env_map = [
        (('feishu', 'webhook'), 'FEISHU_WEBHOOK'),
        (('feishu', 'secret'), 'FEISHU_SECRET'),
        (('feishu_app', 'app_id'), 'FEISHU_APP_ID'),
        (('feishu_app', 'app_secret'), 'FEISHU_APP_SECRET'),
        (('feishu_app', 'receive_id'), 'FEISHU_RECEIVE_ID'),
        (('custom', 'url'), 'FEISHU_CUSTOM_URL'),
    ]
    for (sec, key), env in env_map:
        v = os.environ.get(env)
        if v:
            cfg.setdefault(sec, {})[key] = v
    if os.environ.get('FEISHU_PUSH_PROVIDER'):
        cfg['provider'] = os.environ['FEISHU_PUSH_PROVIDER']
    return cfg


def save_config(cfg, path=None):
    """写回配置文件（保留已有的注释键与其它渠道配置）。"""
    p = path or DEFAULT_CONFIG
    d = os.path.dirname(p)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with codecs.open(p, 'w', 'utf-8') as fp:
        fp.write(json.dumps(cfg, ensure_ascii=False, indent=2))
        fp.write('\n')
    return p


def mask_secret(v, keep=38):
    v = v or ''
    if not v:
        return '(空)'
    return v if len(v) <= keep + 8 else (v[:keep] + '...' + v[-6:])


def print_config(path=None):
    """打印当前配置（敏感字段打码）。"""
    cfg = load_config(path, raw=True)
    p = path or DEFAULT_CONFIG
    print('配置文件: %s' % p)
    print('  provider = %s' % cfg.get('provider'))
    print('  only_if  = %s' % cfg.get('only_if'))
    print('  once_per_day = %s（%s）'
          % (bool(cfg.get('once_per_day')),
             '每天只推一次' if cfg.get('once_per_day') else '不限次数'))
    for name in ('feishu', 'feishu_app', 'custom'):
        sec = cfg.get(name) or {}
        keys = [k for k in sec.keys() if not k.startswith('_')]
        if not keys:
            continue
        print('  [%s]' % name)
        for k in keys:
            v = sec.get(k)
            show = mask_secret(v) if ('secret' in k or 'webhook' in k or 'token' in k or 'url' in k) else v
            print('    %-16s = %s' % (k, show))


def _placeholder(v):
    s = (v or '').strip()
    return (not s) or ('你的' in s) or s.upper().startswith('YOUR') or s.startswith('xxx')


def config_ready(cfg, provider):
    """检查渠道必填项。返回 (ok, 缺什么)。"""
    c = cfg.get(provider) or {}
    if provider == 'feishu':
        return (not _placeholder(c.get('webhook'))), 'feishu.webhook'
    if provider == 'feishu_app':
        ok = (not _placeholder(c.get('app_id'))) and (not _placeholder(c.get('app_secret')))
        return ok, 'feishu_app.app_id / app_secret'
    if provider == 'custom':
        return (not _placeholder(c.get('url'))), 'custom.url'
    return False, '未知渠道 %s' % provider


# ================================================================ 文案渲染
def _clip(s, max_chars):
    return s if len(s) <= max_chars else s[:max_chars - 8] + '\n…（已截断）'


def build_card(sm, title=None):
    """把摘要渲染成飞书交互卡片（interactive card）。"""
    ix = sm.get('indices') or {}
    br = sm.get('breadth') or {}
    fd = sm.get('fund') or {}
    ld = sm.get('leaders') or {}
    sg = sm.get('signals') or {}
    lv = sm.get('levels') or {}
    date = sm.get('trade_date') or ''

    sh = ix.get('sh') or {}
    sh_pct = sh.get('pct') or 0
    # 国内习惯：红涨绿跌
    color = 'red' if sh_pct > 0 else ('green' if sh_pct < 0 else 'grey')
    title = title or 'A股复盘 %s' % date

    def pc(k):
        d = ix.get(k)
        return ('%s %+.2f%%' % (d['name'], d['pct'])) if d else None

    # --- 区块 1：指数
    l1 = []
    if sh:
        l1.append('**上证 %s　%+.2f%%**' % (sh.get('close'), sh_pct))
    row = [x for x in (pc('sz'), pc('cyb'), pc('kc50')) if x]
    if row:
        l1.append(' | '.join(row))
    small = [x for x in (pc('zz1000'), pc('gz2000')) if x]
    if small:
        l1.append('小盘：' + ' | '.join(small))
    if ix.get('bj50'):
        l1.append('北证50 %+.2f%%' % ix['bj50']['pct'])
    s1 = '\n'.join(l1)

    # --- 区块 2：量能与资金
    l2 = ['**成交** %s亿　**涨跌** %s↑ / %s↓（中位 %+.2f%%）'
          % (sm.get('turnover_yi'), br.get('up'), br.get('down'), br.get('median_pct') or 0),
          '**情绪** 涨停%s　跌停%s　炸板率%s'
          % (br.get('limit_up') if br.get('limit_up') is not None else '—',
             br.get('limit_dn') if br.get('limit_dn') is not None else '—',
             ('%s%%' % br.get('broken_rate')) if br.get('broken_rate') is not None else '—')]
    if fd.get('main_yi') is not None:
        l2.append('**主力** 净%s %s亿'
                  % ('流入' if fd['main_yi'] > 0 else '流出', abs(fd['main_yi'])))
    if fd.get('margin_yi'):
        l2.append('**两融** %s亿（%s）' % (fd['margin_yi'], fd.get('margin_date') or ''))
    s2 = '\n'.join(l2)

    # --- 区块 3：板块
    def brief(key, n=4, sep=' / '):
        rows = ld.get(key) or []
        return sep.join('%s%+.1f%%' % (x['name'], x['pct']) for x in rows[:n])

    l3 = []
    if ld.get('industry_up'):
        l3.append('**领涨** ' + brief('industry_up'))
    if ld.get('industry_dn'):
        l3.append('**领跌** ' + brief('industry_dn'))
    if ld.get('concept_up'):
        l3.append('**热点** ' + brief('concept_up', 5, '、'))
    s3 = '\n'.join(l3)

    # --- 区块 4：多空信号
    sup = [_u(x) for x in (sg.get('support') or [])]
    pre = [_u(x) for x in (sg.get('pressure') or [])]
    l4 = ['**偏多信号**']
    l4 += ['• ' + x for x in sup[:3]] or ['• （无）']
    l4.append('')
    l4.append('**偏空信号**')
    l4 += ['• ' + x for x in pre[:3]] or ['• （无）']
    s4 = '\n'.join(l4)

    elements = [
        {'tag': 'div', 'text': {'tag': 'lark_md', 'content': _clip(s1, 900)}},
    ]
    if s2:
        elements += [{'tag': 'hr'},
                     {'tag': 'div', 'text': {'tag': 'lark_md', 'content': _clip(s2, 900)}}]
    if s3:
        elements += [{'tag': 'hr'},
                     {'tag': 'div', 'text': {'tag': 'lark_md', 'content': _clip(s3, 900)}}]
    if s4.strip():
        elements += [{'tag': 'hr'},
                     {'tag': 'div', 'text': {'tag': 'lark_md', 'content': _clip(s4, 1400)}}]
    if lv.get('resistance') and lv.get('support'):
        elements += [{'tag': 'hr'},
                     {'tag': 'div', 'text': {'tag': 'lark_md',
                      'content': '**关键点位**　压力 %.0f–%.0f　支撑 %.0f'
                                 % (lv['resistance'][0], lv['resistance'][1],
                                    lv['support'][0])}}]
    elements.append({'tag': 'note', 'elements': [
        {'tag': 'plain_text',
         'content': '数据来自公开行情接口，仅供整理与推演，不构成投资建议'}]})

    return {
        'config': {'wide_screen_mode': True, 'enable_forward': True},
        'header': {'template': color,
                   'title': {'tag': 'plain_text', 'content': title}},
        'elements': elements,
    }


def render_markdown(sm):
    """纯文本版（feishu_app 或降级时用）。"""
    card = build_card(sm)
    out = ['**%s**' % card['header']['title']['content'], '']
    for el in card['elements']:
        if el.get('tag') == 'div':
            out.append(el['text']['content'])
            out.append('')
        elif el.get('tag') == 'note':
            out.append('_%s_' % el['elements'][0]['content'])
    return '\n'.join(out)


# ================================================================ 发送
def send_feishu_webhook(webhook, card, secret='', timeout=20):
    """飞书自定义机器人。返回 (ok, 说明)。"""
    body = {'msg_type': 'interactive', 'card': card}
    if secret:
        ts = str(int(time.time()))
        body['timestamp'] = ts
        body['sign'] = gen_sign(ts, secret)
    status, txt = http_post(webhook, json_body=body, timeout=timeout)
    return _parse_feishu(txt, status)


def send_feishu_text(webhook, text, secret='', timeout=20):
    """降级用：纯文本消息。"""
    body = {'msg_type': 'text', 'content': {'text': text}}
    if secret:
        ts = str(int(time.time()))
        body['timestamp'] = ts
        body['sign'] = gen_sign(ts, secret)
    status, txt = http_post(webhook, json_body=body, timeout=timeout)
    return _parse_feishu(txt, status)


def _parse_feishu(txt, status):
    try:
        j = json.loads(txt)
    except Exception:
        return (200 <= (status or 0) < 300), txt[:200]
    code = j.get('code')
    if code == 0:
        return True, 'ok'
    if code == 19001:
        return False, 'webhook token 无效，请检查 push_config.json 里的 feishu.webhook'
    if code == 19021:
        return False, '签名校验失败：请检查 feishu.secret 是否与机器人设置一致'
    if code == 9499:
        return False, '消息发送过于频繁，请降低推送频率'
    if code == 19024:
        return False, '机器人被移出群聊或未在群内'
    return False, 'code=%s msg=%s' % (code, j.get('msg'))


def send_feishu_app(app_id, app_secret, receive_id, card, receive_id_type='open_id',
                    timeout=20, api_base='https://open.feishu.cn'):
    """飞书自建应用：换 tenant_access_token 后发消息。

    api_base 可覆盖，便于私有化部署或本地联调。
    """
    status, txt = http_post(api_base + '/open-apis/auth/v3/tenant_access_token/internal',
                            json_body={'app_id': app_id, 'app_secret': app_secret},
                            timeout=timeout)
    try:
        j = json.loads(txt)
    except Exception:
        return False, '获取 token 失败: %s' % txt[:150]
    token = j.get('tenant_access_token')
    if not token:
        return False, '获取 token 失败: code=%s msg=%s' % (j.get('code'), j.get('msg'))

    url = ('%s/open-apis/im/v1/messages?receive_id_type=%s' % (api_base, receive_id_type))
    body = {'receive_id': receive_id, 'msg_type': 'interactive',
            'content': json.dumps({'card': card})}
    status, txt = http_post(url, json_body=body,
                            headers={'Authorization': 'Bearer %s' % token}, timeout=timeout)
    try:
        j = json.loads(txt)
    except Exception:
        return (200 <= (status or 0) < 300), txt[:200]
    if j.get('code') == 0:
        return True, 'ok'
    return False, 'code=%s msg=%s' % (j.get('code'), j.get('msg'))


def send_custom(sec, title, content, card, timeout=20):
    """通用 webhook：body 里支持 {{title}} / {{content}} 占位符。"""
    url = sec.get('url')
    headers = sec.get('headers') or {}
    tmpl = sec.get('body')
    if isinstance(tmpl, dict):
        raw = json.dumps(tmpl, ensure_ascii=False)
        raw = raw.replace('{{title}}', title)
        raw = raw.replace('{{content}}', content.replace('\\', '\\\\').replace('"', '\\"')
                          .replace('\n', '\\n'))
        try:
            body = json.loads(raw)
        except Exception:
            body = {'title': title, 'content': content}
        status, txt = http_post(url, json_body=body, headers=headers, timeout=timeout)
    else:
        status, txt = http_post(url, data={'title': title, 'content': content},
                                headers=headers, timeout=timeout)
    return (200 <= (status or 0) < 300), txt[:200]


# ================================================================ 调度
def should_push(sm, mode):
    """按 only_if 策略决定是否推送，避免无意义打扰。"""
    if mode in (None, '', 'always'):
        return True, ''
    sg = sm.get('signals') or {}
    ix = (sm.get('indices') or {}).get('sh') or {}
    if mode == 'pressure':
        n = len(sg.get('pressure') or [])
        return n >= 3, ('偏空信号 %d 条（阈值 3）' % n)
    if mode == 'bigmove':
        p = abs(ix.get('pct') or 0)
        return p >= 1.0, ('上证涨跌幅 %.2f%%（阈值 1.0%%）' % p)
    return True, ''


def _state_read():
    if os.path.isfile(STATE_FILE):
        try:
            with codecs.open(STATE_FILE, 'r', 'utf-8') as fp:
                return json.load(fp) or {}
        except Exception:
            return {}
    return {}


def _state_write(st):
    try:
        d = os.path.dirname(STATE_FILE)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        with codecs.open(STATE_FILE, 'w', 'utf-8') as fp:
            fp.write(json.dumps(st, ensure_ascii=False))
    except Exception:
        pass


def push(summary, cfg=None, provider=None, dry_run=False, force=False, timeout=20,
         record_state=True):
    """把摘要推送到飞书。返回 0 表示成功。

    record_state=False 表示这是测试性发送，不写入「当天已推送」标记。
    """
    cfg = cfg if cfg is not None else load_config()
    provider = _u(provider) or cfg.get('provider') or 'feishu'
    if provider not in PROVIDERS:
        print('[!] 未知渠道: %s（可选：%s）' % (provider, ' / '.join(PROVIDERS)))
        return 2

    date = summary.get('trade_date') or ''
    st = _state_read()
    once = bool(cfg.get('once_per_day'))
    pushed_today = st.get('last_date') == date and st.get('provider') == provider
    if once and not force and not dry_run and pushed_today:
        print('[i] once_per_day=true，%s 已推送过，跳过（加 --force 可强制重发）' % date)
        return 0

    ok_cfg, need = config_ready(cfg, provider)
    if not ok_cfg:
        if not dry_run:
            print('[!] 渠道 %s 的 %s 还没配置。请编辑 push_config.json 或设置对应环境变量。'
                  % (provider, need))
            return 3
        print('[i] 提示：%s 尚未配置（--dry-run 仍会展示将要发送的卡片）' % need)

    ok_push, why = should_push(summary, cfg.get('only_if'))
    if not ok_push:
        print('[i] 按 only_if=%s 策略本次不推送：%s' % (cfg.get('only_if'), why))
        return 0

    sec = cfg.get(provider) or {}
    title = _u(sec.get('title')) or ('A股复盘 %s' % date)
    card = build_card(summary, title)

    if dry_run:
        print('=' * 66)
        print('渠道: %s   标题: %s' % (provider, title))
        print('-' * 66)
        print(render_markdown(summary))
        print('-' * 66)
        payload = json.dumps({'msg_type': 'interactive', 'card': card}, ensure_ascii=False)
        print('卡片 JSON 长度: %d 字符 / %.1f KB（飞书上限 30KB）'
              % (len(payload), len(payload.encode('utf-8')) / 1024.0))
        print('=' * 66)
        return 0

    if provider == 'feishu':
        ok, msg = send_feishu_webhook(sec['webhook'], card, sec.get('secret', ''), timeout)
        if not ok and ('无效' in (msg or '') or '19001' in (msg or '')):
            pass
    elif provider == 'feishu_app':
        ok, msg = send_feishu_app(sec['app_id'], sec['app_secret'],
                                  sec.get('receive_id', ''),
                                  card, sec.get('receive_id_type', 'open_id'), timeout,
                                  sec.get('api_base') or 'https://open.feishu.cn')
    else:
        ok, msg = send_custom(sec, title, render_markdown(summary), card, timeout)

    if ok:
        print('[OK] 已推送到 %s：%s' % (provider, msg))
        if date and record_state:
            n = 1
            if st.get('last_date') == date:
                n = int(st.get('count') or 0) + 1
            st['last_date'] = date
            st['count'] = n
            st['provider'] = provider
            st['at'] = time.strftime('%Y-%m-%d %H:%M:%S')
            _state_write(st)
            if not once:
                print('    (%s 当天第 %d 次推送；如需每天只推一次，把 once_per_day 设为 true)' % (date, n))
        return 0
    print('[X] 推送失败（%s）：%s' % (provider, msg))
    return 1


def push_file(json_path, config_path=None, dry_run=False, provider=None, force=False):
    """读取 .summary.json 并推送。供 stock_review.py 调用。"""
    if not os.path.isfile(json_path):
        print('[!] 摘要文件不存在: %s' % json_path)
        return 3
    try:
        with codecs.open(json_path, 'r', 'utf-8') as fp:
            sm = json.load(fp)
    except Exception as e:
        print('[!] 摘要文件解析失败: %s' % e)
        return 3
    return push(sm, load_config(config_path), provider, dry_run, force)


# ================================================================ CLI
def _demo_summary():
    return {
        'trade_date': time.strftime('%Y-%m-%d'),
        'indices': {'sh': {'name': '上证指数', 'close': 3885.33, 'pct': -0.07},
                    'sz': {'name': '深证成指', 'close': 13384.57, 'pct': -0.64},
                    'cyb': {'name': '创业板指', 'close': 3285.58, 'pct': -1.10},
                    'kc50': {'name': '科创50', 'close': 1528.27, 'pct': -1.62}},
        'turnover_yi': 16292,
        'breadth': {'up': 3126, 'down': 2224, 'median_pct': 0.28,
                    'limit_up': 55, 'limit_dn': 16, 'broken_rate': 35},
        'fund': {'main_yi': -165.3},
        'leaders': {'industry_up': [{'name': '医疗研发外包', 'pct': 5.75},
                                    {'name': '医疗服务', 'pct': 4.09}],
                    'industry_dn': [{'name': '教育运营', 'pct': -7.9},
                                    {'name': '其他种植业', 'pct': -6.04}],
                    'concept_up': [{'name': 'CRO', 'pct': 4.56}]},
        'signals': {'support': ['这是一条测试消息，收到即说明飞书推送配置成功。'],
                    'pressure': ['（示例）均线空头排列']},
        'levels': {'resistance': [3920, 3937], 'support': [3867]},
    }


def main():
    ap = argparse.ArgumentParser(description='飞书推送（A股复盘摘要）')
    ap.add_argument('--summary', default=None, help='.summary.json 路径')
    ap.add_argument('--text', default=None, help='直接推送一段自定义文本')
    ap.add_argument('--title', default=None, help='卡片标题')
    ap.add_argument('--config', default=None, help='配置文件路径（默认 push_config.json）')
    ap.add_argument('--provider', default=None, help='渠道：%s' % ' / '.join(PROVIDERS))
    ap.add_argument('--dry-run', action='store_true', help='只打印卡片，不发送')
    ap.add_argument('--force', action='store_true',
                    help='即使 once_per_day=true 也强制重发')
    ap.add_argument('--set-once-per-day', default=None, choices=['true', 'false'],
                    help='设置是否每天只推一次（默认 false = 不限次数）')
    ap.add_argument('--test', action='store_true', help='发送一条测试卡片')
    ap.add_argument('--timeout', type=int, default=20, help='单次请求超时（秒）')
    ap.add_argument('--set-webhook', default=None,
                    help='把飞书机器人 Webhook 地址写入 push_config.json')
    ap.add_argument('--set-secret', default=None,
                    help='把机器人「签名校验」密钥写入 push_config.json')
    ap.add_argument('--set-provider', default=None,
                    help='把默认渠道写入 push_config.json')
    ap.add_argument('--show-config', action='store_true',
                    help='显示当前配置（敏感字段打码）')
    args = ap.parse_args()

    # ---- 配置写入类操作 ----
    if args.show_config:
        print_config(args.config)
        return 0
    if (args.set_webhook is not None or args.set_secret is not None
            or args.set_provider or args.set_once_per_day is not None):
        cfg_file = load_config(args.config, raw=True)
        if args.set_provider:
            cfg_file['provider'] = args.set_provider
        if args.set_webhook is not None:
            cfg_file.setdefault('feishu', {})['webhook'] = args.set_webhook.strip()
        if args.set_secret is not None:
            cfg_file.setdefault('feishu', {})['secret'] = args.set_secret.strip()
        if args.set_once_per_day is not None:
            cfg_file['once_per_day'] = (args.set_once_per_day == 'true')
        p = save_config(cfg_file, args.config)
        print('[OK] 已写入 %s' % p)
        print_config(args.config)
        return 0

    cfg = load_config(args.config)
    provider = _u(args.provider) or cfg.get('provider') or 'feishu'
    args.text = _u(args.text)
    args.title = _u(args.title)
    args.summary = _u(args.summary)

    if args.test:
        sm = _demo_summary()
        if args.title:
            sm['_title'] = args.title
        return push(sm, cfg, provider, args.dry_run, force=True, timeout=args.timeout,
                    record_state=False)

    if args.text:
        sm = _demo_summary()
        sm['trade_date'] = time.strftime('%Y-%m-%d')
        sm['signals'] = {'support': [args.text], 'pressure': []}
        return push(sm, cfg, provider, args.dry_run, force=args.force, timeout=args.timeout)

    if not args.summary:
        rdir = os.path.join(HERE, 'reports')
        cands = []
        if os.path.isdir(rdir):
            cands = sorted([f for f in os.listdir(rdir) if f.endswith('.summary.json')])
        if not cands:
            print('[!] 未找到摘要文件。请先跑 stock_review.py，或用 --summary 指定。')
            return 3
        args.summary = os.path.join(rdir, cands[-1])
        print('[i] 使用最新的摘要: %s' % args.summary)

    return push_file(args.summary, args.config, args.dry_run, provider, args.force)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\n已中断')
        sys.exit(130)
