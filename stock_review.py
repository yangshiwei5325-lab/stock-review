#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
A股每日复盘一键脚本  (stock_review.py)
=========================================================
一键完成：大盘行情 -> 市场宽度/情绪 -> 资金流向 -> 板块结构
        -> 个股榜单 -> 消息面 -> 外围市场 -> 均线位置 -> 明日展望
输出：Markdown 复盘报告（默认存到 ./reports/ 目录）+ 控制台摘要

用法：
    python stock_review.py                     # 全量生成
    python stock_review.py --push              # 生成并推送到飞书
    python stock_review.py --no-news           # 跳过新闻抓取（更快）
    python stock_review.py --live              # 强制抓实时数据（盘中默认已开启）
    python stock_review.py --no-live           # 盘中也不吃缓存，走本地快照
    python stock_review.py --date 20260911     # 离线复盘（只读缓存重算，不联网）
    python stock_review.py --out D:/x.md       # 指定输出文件

缓存策略：
    交易时段（周一~周五 09:10-15:05）自动绕过缓存抓实时数据，且盘中快照不写回缓存；
    收盘后走缓存，秒级完成且与收盘结果一致。

兼容 Python 2.7 与 Python 3.x。
数据来源：东方财富 / 新浪财经 公开行情接口。
"""

from __future__ import print_function, unicode_literals, division

import os
import sys
import json
import math
import time
import codecs
import numbers
import argparse
import datetime
import threading
import collections
import heapq
import re

# ---------------------------------------------------------------- py2/py3 兼容
PY3 = sys.version_info[0] >= 3
NUM_TYPES = (int, float, long) if not PY3 else (int, float)
if PY3:
    from urllib.request import Request, urlopen
else:
    from urllib2 import Request, urlopen
    try:
        # 让 print 支持中文。这里必须用自定义标记做幂等，不能靠 .encoding：
        # codecs.StreamWriter 会把未知属性代理给内层流，读到的永远是原始流的
        # encoding（重定向时常为 None），判断必然失败并被重复包装；二次包装后
        # 输出中文会在内层 encode 时按 ascii 解码，抛 UnicodeDecodeError。
        # 与 feishu_push.ensure_utf8_stdout() 共用 '_utf8_wrapped' 标记。
        _so = sys.stdout
        if not getattr(_so, '_utf8_wrapped', False) and \
                'utf' not in (getattr(_so, 'encoding', '') or '').lower():
            _so = codecs.getwriter('utf-8')(_so)
            _so._utf8_wrapped = True
            sys.stdout = _so
    except Exception:
        pass

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36'
EM_UT = 'fa5fd1943c7b386f172d6893dbfba10b'
# 东财行情主机（按顺序轮询，前一个失败自动换下一个）
EM_HOSTS = [
    'push2delay.eastmoney.com',
    'push2.eastmoney.com',
    '1.push2.eastmoney.com',
    '2.push2.eastmoney.com',
    '82.push2.eastmoney.com',
]
HIS_HOSTS = ['push2his.eastmoney.com']

# 指数清单：(新浪代码, 展示名, 东财secid用于取K线)
INDICES = [
    ('sh000001', '上证指数', '1.000001'),
    ('sz399001', '深证成指', '0.399001'),
    ('sz399006', '创业板指', '0.399006'),
    ('sh000688', '科创50', '1.000688'),
    ('sh000300', '沪深300', '1.000300'),
    ('sh000016', '上证50', '1.000016'),
    ('sh000905', '中证500', '1.000905'),
    ('sh000852', '中证1000', None),
    ('sz399303', '国证2000', None),
    ('bj899050', '北证50', '0.899050'),
    ('sh000922', '中证红利', None),
]

# 外围指数：东财 secid
GLOBAL_SECIDS = [
    ('100.DJIA', '道琼斯'),
    ('100.SPX', '标普500'),
    ('100.NDX', '纳斯达克'),
    ('100.N225', '日经225'),
    ('100.KS11', '韩国KOSPI'),
    ('100.TWII', '台湾加权'),
    ('100.HSI', '恒生指数'),
    ('124.HSTECH', '恒生科技'),
    ('100.FTSE', '英国富时100'),
    ('100.GDAXI', '德国DAX'),
    ('100.UDI', '美元指数'),
    ('100.BDI', '波罗的海BDI'),
]

# 行业/概念板块过滤串
FS_INDUSTRY = 'm:90+t:2+f:!50'
FS_CONCEPT = 'm:90+t:3'
# 全A股（沪深主板+创业板+科创板+北交所）
FS_ALL_A = 'm:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048'

NEWS_KEYWORDS = [
    '收评', '沪指', '成交额', '主力资金', '净流出', '净流入', '北向', '涨停', '跌停',
    '板块', '医药', '创新药', '半导体', '存储', 'PCB', '军工', '农业', '粮食',
    '美联储', '加息', '降息', '央行', '政策', '盘面', '异动', '大宗', '油价',
]

ERRORS = []          # 记录失败的数据源，最后写进报告的"数据缺口"
OFFLINE = [False]    # True = 只读本地缓存，不发起网络请求（离线复盘）
_IO_LOCK = threading.Lock()   # 并发抓取时保证控制台输出不串行错乱


def log(msg):
    with _IO_LOCK:
        print('[%s] %s' % (datetime.datetime.now().strftime('%H:%M:%S'), msg))


def is_trading_session(now):
    """是否处于 A 股连续竞价时段（周一~周五 09:10-15:05，含集合竞价与收盘缓冲）。"""
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return (9 * 60 + 10) <= mins <= (15 * 60 + 5)


def warn(msg):
    ERRORS.append(msg)
    with _IO_LOCK:
        print('[!] %s' % msg)


# ================================================================ HTTP 基础
def http_get(url, referer=None, timeout=25, retries=4, encoding='utf-8'):
    """带重试的 GET，返回解码后的文本（最后一次失败后不再无谓等待）。"""
    headers = {'User-Agent': UA, 'Accept': '*/*'}
    if referer:
        headers['Referer'] = referer
    last = None
    for i in range(retries):
        try:
            resp = urlopen(Request(url, headers=headers), timeout=timeout)
            try:
                raw = resp.read()
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
            return raw.decode(encoding, 'ignore')
        except Exception as e:
            last = e
            if i < retries - 1:          # 最后一次失败直接抛出，不再空等
                time.sleep(0.5 * (i + 1))
    raise last


def http_json(url, referer=None, timeout=25, retries=4):
    return json.loads(http_get(url, referer, timeout, retries))


class _JsonFetcher(object):
    """带磁盘缓存的 JSON 抓取器基类。

    - 成功抓取后把原始 JSON 落盘，重跑时直接命中缓存（秒级完成，也便于断点续跑）
    - refresh=True 时忽略缓存强制重抓
    """

    def __init__(self, hosts=None, cache_dir=None, referer='https://quote.eastmoney.com/',
                 timeout=20, retries=4, pace=0.15, budget=45):
        self.hosts = list(hosts or [])
        self.good = None
        self.cache_dir = cache_dir
        self.referer = referer
        self.timeout = timeout
        self.retries = retries
        self.pace = pace            # 每次网络请求前的最小间隔，避免把代理打挂
        self.budget = budget        # 单次 json() 的总时间预算（秒），超出即放弃
        self.refresh = False
        self.no_write = False       # True = 只抓不写缓存（盘中实时模式）

    # ---- 缓存 ----
    def _path(self, key):
        if not (self.cache_dir and key):
            return None
        return os.path.join(self.cache_dir, key + '.json')

    def _load(self, key):
        if self.refresh:
            return None
        p = self._path(key)
        if p and os.path.isfile(p):
            try:
                with codecs.open(p, 'r', 'utf-8') as fp:
                    return json.load(fp)
            except Exception:
                return None
        return None

    def _save(self, key, obj):
        if self.no_write:      # 盘中快照不落盘，避免污染"收盘定稿"的缓存
            return
        p = self._path(key)
        if not p:
            return
        try:
            d = os.path.dirname(p)
            if d and not os.path.isdir(d):
                os.makedirs(d)
            with codecs.open(p, 'w', 'utf-8') as fp:
                fp.write(json.dumps(obj))
        except Exception:
            pass

    # ---- 请求 ----
    def json(self, path, key=None, referer=None, timeout=None, tries=None, budget=None):
        cached = self._load(key)
        if cached is not None:
            return cached
        if OFFLINE[0]:
            raise Exception('离线模式下缺少缓存: %s' % (key or path[:60]))
        timeout = timeout or self.timeout
        tries = tries or self.retries
        budget = budget or self.budget
        deadline = time.time() + budget
        state = {'err': None, 'timeout': False}

        def attempt(hosts):
            """依次尝试 hosts，每个主机最多 tries 次。成功返回 JSON，全失败返回 None。"""
            for h in hosts:
                url = ('https://%s%s' % (h, path)) if self.hosts else path
                for i in range(tries):
                    if time.time() > deadline:
                        state['timeout'] = True
                        return None
                    time.sleep(self.pace)
                    try:
                        j = http_json(url, referer=referer or self.referer,
                                      timeout=timeout, retries=1)
                        if j is not None:
                            if self.hosts:
                                self.good = h
                            self._save(key, j)
                            return j
                        state['err'] = Exception('空响应: %s' % url[:70])
                    except Exception as e:
                        state['err'] = e
                        if i < tries - 1:
                            time.sleep(0.8 * (i + 1))
            return None

        r = None
        if self.hosts and self.good:            # 先用上次成功的主机，通常一次命中
            r = attempt([self.good])
            if r is None:
                self.good = None                # 该主机失效，回到全量探测
        if r is None:
            r = attempt(list(self.hosts) if self.hosts else [None])
        if r is not None:
            return r
        if state['timeout']:
            raise Exception('超出单次请求时间预算 %ds' % budget)
        if state['err']:
            raise state['err']
        raise Exception('请求失败: %s' % path[:80])


# 行情客户端实例（cache_dir / refresh 在 main 里按交易日设置）
EM = _JsonFetcher(hosts=EM_HOSTS)                                        # 东财行情主机
EM_HIS = _JsonFetcher(hosts=HIS_HOSTS)                                   # 东财日K主机
HTTP = _JsonFetcher(hosts=None, referer='https://data.eastmoney.com/')   # 涨跌停池/两融/快讯


def em_json(path, hosts=None, referer='https://quote.eastmoney.com/', timeout=25, key=None):
    """请求东财接口。hosts=HIS_HOSTS 时走日K主机。"""
    if hosts == HIS_HOSTS:
        return EM_HIS.json(path, key=key, referer=referer, timeout=timeout)
    return EM.json(path, key=key, referer=referer, timeout=timeout)


def retry_call(fn, attempts=3, delay=4.0, label=''):
    """对整批操作做几次尝试，用来扛过代理/接口的瞬时不可用。"""
    last = None
    for i in range(attempts):
        try:
            r = fn()
            if r is not None:
                return r
        except Exception as e:
            last = e
        if i < attempts - 1:
            log('  %s 第 %d 次失败，%.0f 秒后重试 ...' % (label or '请求', i + 1, delay))
            time.sleep(delay)
    if last:
        warn('%s 重试 %d 次仍失败: %r' % (label or '请求', attempts, last))
    return None


def parallel_map(fn, items, workers=6):
    """并发执行 fn(item)，结果与 items 等长（单项异常记为 None）。"""
    items = list(items)
    results = [None] * len(items)
    if not items:
        return results
    lock = threading.Lock()
    todo = collections.deque(range(len(items)))

    def worker():
        while True:
            with lock:
                if not todo:
                    return
                i = todo.popleft()
            try:
                results[i] = fn(items[i])
            except Exception:
                results[i] = None

    nw = max(1, min(workers, len(items)))
    ts = [threading.Thread(target=worker) for _ in range(nw)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return results


def fetch_all_sources(jobs, workers=6):
    """并发执行一批互不依赖的取数任务，结果按名字返回。

    jobs: [(名字, 无参可调用), ...]
    返回 {名字: 结果}；单项失败记为 None 并 warn（含任务名与异常）。
    """
    def run(job):
        _name, fn = job
        try:
            return fn(), None
        except Exception as e:
            return None, e

    results = parallel_map(run, jobs, workers)
    out = {}
    for (name, _fn), res in zip(jobs, results):
        # parallel_map 把 worker 内的意外异常记为 None；这里统一还原成错误
        value, err = res if res is not None else (None, Exception('未知异常'))
        if err is not None:
            warn('%s 抓取失败: %r' % (name, err))
        out[name] = value
    return out


def fnum(v, default=0.0):
    """安全转 float（'-' / None / '' 都当默认值）。"""
    try:
        if v is None or v == '-' or v == '':
            return default
        return float(v)
    except Exception:
        return default


# ================================================================ 数据抓取
def sina_text(key, codes, timeout=25):
    """新浪接口的文本级缓存（原始返回 GBK，统一转 UTF-8 落盘）。"""
    p = os.path.join(HTTP.cache_dir, key) if HTTP.cache_dir else None
    if p and os.path.isfile(p) and not HTTP.refresh:
        try:
            with codecs.open(p, 'r', 'utf-8') as fp:
                return fp.read()
        except Exception:
            pass
    if OFFLINE[0]:
        raise Exception('离线模式下缺少缓存: %s' % key)
    txt = http_get('https://hq.sinajs.cn/list=' + codes,
                   referer='https://finance.sina.com.cn', encoding='gbk', timeout=timeout)
    if p and not HTTP.no_write:      # 盘中快照不落盘
        try:
            d = os.path.dirname(p)
            if d and not os.path.isdir(d):
                os.makedirs(d)
            with codecs.open(p, 'w', 'utf-8') as fp:
                fp.write(txt)
        except Exception:
            pass
    return txt


# 预编译：新浪行情文本 hq_str_xxx="..."
_SINA_HQ_RE = re.compile(r'hq_str_([^=]+)="([^"]*)"')


def parse_sina_lines(txt):
    """解析新浪 hq_str_xxx="..." 文本为 {code: [字段列表]}。"""
    out = {}
    for code, body in _SINA_HQ_RE.findall(txt):
        out[code] = body.split(',')
    return out


def fetch_indices(raw_text=None):
    """新浪行情一次取回全部指数。返回 ({code: {...}}, 原始文本)。"""
    codes = ','.join([c for c, _, _ in INDICES])
    codes_set = set(codes.split(','))
    if raw_text is None:
        raw_text = sina_text('sina_indices.txt', codes)
    parsed = parse_sina_lines(raw_text)
    out = {}
    for code, f in parsed.items():
        if code not in codes_set or len(f) < 10:
            continue
        prev = fnum(f[2])
        price = fnum(f[3])
        if prev <= 0 or price <= 0:
            continue
        out[code] = {
            'name': f[0],
            'open': fnum(f[1]),
            'prev': prev,
            'price': price,
            'high': fnum(f[4]),
            'low': fnum(f[5]),
            'amount': fnum(f[9]),       # 元；对 sh000001/sz399001 即该市场总成交额
            'pct': (price / prev - 1) * 100,
            'chg': price - prev,
            'date': f[30] if len(f) > 30 else '',
            'time': f[31] if len(f) > 31 else '',
        }
    return out, raw_text


def fetch_global():
    secids = ','.join([s for s, _ in GLOBAL_SECIDS])
    path = ('/api/qt/ulist.np/get?fltt=2&secids=%s&fields=f2,f3,f4,f12,f14,f124&ut=%s'
            % (secids, EM_UT))
    j = em_json(path, key='global')
    out = []
    name_map = dict(GLOBAL_SECIDS)
    for d in (j.get('data') or {}).get('diff') or []:
        sid = '%s.%s' % (d.get('f13', ''), d.get('f12', ''))
        out.append({
            'name': name_map.get(sid) or d.get('f14'),
            'price': fnum(d.get('f2')),
            'pct': fnum(d.get('f3')),
            'chg': fnum(d.get('f4')),
            'ts': d.get('f124') or 0,
        })
    return out


def fetch_commodity_fx():
    """新浪外盘期货 / 汇率。"""
    txt = sina_text('sina_commodity.txt', 'hf_GC,hf_CL,hf_SI,USDCNY')
    out = {}
    spec = {'hf_GC': ('纽约黄金', 'USD'), 'hf_CL': ('纽约原油', 'USD'),
            'hf_SI': ('纽约白银', 'USD'), 'USDCNY': ('美元/人民币', None)}
    for code, f in parse_sina_lines(txt).items():
        if code not in spec or len(f) < 9:
            continue
        nm, _ = spec[code]
        if code == 'USDCNY':
            out[code] = {'name': nm, 'price': fnum(f[1]), 'pct': None}
        else:
            cur, prev = fnum(f[0]), fnum(f[7])
            out[code] = {
                'name': nm, 'price': cur, 'prev': prev,
                'open': fnum(f[8]), 'high': fnum(f[4]), 'low': fnum(f[5]),
                'pct': ((cur / prev - 1) * 100) if prev else None,
            }
    return out


def fetch_breadth(max_pages=70, page_size=100, workers=8):
    """全A股逐页抓取（东财单页上限100）。并发 + 逐页落盘缓存，失败页可下次续跑。"""

    def page_path(pn):
        return ('/api/qt/clist/get?pn=%d&pz=%d&po=1&np=1&fltt=2&invt=2&fid=f3'
                '&fs=%s&fields=f2,f3,f6,f12,f14&ut=%s' % (pn, page_size, FS_ALL_A, EM_UT))

    def get_page(pn):
        j = EM.json(page_path(pn), key='breadth_p%03d' % pn, timeout=25, budget=90)
        d = (j.get('data') or {})
        return (d.get('diff') or []), d.get('total')

    # 第 1 页用于拿到总数，据此决定要翻多少页。它是整段抓取的闸门，多试几次。
    first, total = None, None
    for attempt in range(4):
        try:
            first, total = get_page(1)
            if first:
                break
        except Exception as e:
            warn('市场宽度第1页抓取失败（第 %d/4 次）: %r' % (attempt + 1, e))
        if attempt < 3:
            time.sleep(3)
    if not first:
        warn('市场宽度第1页始终失败，本次跳过该章节（直接重跑即可续抓）')
        return [], (total or 0), 0

    need = int(math.ceil((total or len(first)) / float(page_size)))
    need = max(1, min(need, max_pages))
    results = {1: first}

    def run_pass(pages, nworkers):
        """并发跑一轮抓取；返回本轮仍失败的页码（复用 parallel_map，不重复造线程池）。"""
        pages = list(pages)
        if not pages:
            return []

        def grab(pn):
            return get_page(pn)[0]

        got = parallel_map(grab, pages, nworkers)
        bad = []
        for pn, rows in zip(pages, got):
            if rows is None:
                bad.append(pn)          # 单项异常被 parallel_map 记为 None
            else:
                results[pn] = rows
        return bad

    failed = run_pass(list(range(2, need + 1)), workers)

    # 第二轮补抓：降低并发、放慢节奏，专治瞬时抖动（代理/限流）
    if failed:
        log('  首轮有 %d 页失败，开始第二轮补抓 ...' % len(failed))
        time.sleep(2)
        failed = run_pass(failed, max(1, min(3, workers // 2)))

    if failed:
        warn('市场宽度有 %d/%d 页最终失败（页码 %s）；再跑一次即可只补这些页'
             % (len(failed), need, ','.join(str(x) for x in sorted(failed)[:12])))

    out = []
    for pn in sorted(results):
        out.extend(results[pn])
    rows, suspended = [], 0
    for d in out:
        pct = d.get('f3')
        if not isinstance(pct, NUM_TYPES):   # 停牌 / 无报价，f3 为 '-'
            suspended += 1
            continue
        rows.append({
            'code': d.get('f12'), 'name': d.get('f14'),
            'price': fnum(d.get('f2')), 'pct': float(pct),
            'amount': fnum(d.get('f6')),
        })
    got_pages = len(results)
    if got_pages < need or failed:
        warn('市场宽度：应取 %d 页，实际成功 %d 页；缺失页可直接重跑续抓'
             % (need, got_pages))
    return rows, (total or len(rows)), suspended


def fetch_board(kind, po=1, size=15):
    """行业/概念板块排行。kind: 'industry' | 'concept'"""
    fs = FS_INDUSTRY if kind == 'industry' else FS_CONCEPT
    path = ('/api/qt/clist/get?pn=1&pz=%d&po=%d&np=1&fltt=2&invt=2&fid=f3&fs=%s'
            '&fields=f3,f12,f14,f62,f104,f105,f128&ut=%s' % (size, po, fs, EM_UT))
    j = em_json(path, key='board_%s_%s_%d' % (kind, 'up' if po else 'dn', size))
    rows = []
    for d in (j.get('data') or {}).get('diff') or []:
        rows.append({
            'name': d.get('f14'), 'pct': fnum(d.get('f3')),
            'flow': fnum(d.get('f62')),          # 主力净额（元）
            'lead': d.get('f128') or '',
            'up': d.get('f104'), 'down': d.get('f105'),
        })
    return rows


def fetch_board_flow(kind, po=1, size=10):
    """板块主力资金净额排行。"""
    fs = FS_INDUSTRY if kind == 'industry' else FS_CONCEPT
    path = ('/api/qt/clist/get?pn=1&pz=%d&po=%d&np=1&fltt=2&invt=2&fid=f62&fs=%s'
            '&fields=f3,f14,f62&ut=%s' % (size, po, fs, EM_UT))
    j = em_json(path, key='flow_%s_%s_%d' % (kind, 'in' if po else 'out', size))
    rows = []
    for d in (j.get('data') or {}).get('diff') or []:
        rows.append({'name': d.get('f14'), 'pct': fnum(d.get('f3')), 'flow': fnum(d.get('f62'))})
    return rows


def fetch_market_flow():
    """沪深两市主力资金（主力/小单/中单/大单/超大单，单位元）。"""
    total = {'main': 0.0, 'small': 0.0, 'mid': 0.0, 'big': 0.0, 'xbig': 0.0}
    ok = 0
    for secid in ('1.000001', '0.399001'):
        path = ('/api/qt/stock/fflow/kline/get?lmt=1&klt=1&secid=%s'
                '&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56'
                '&ut=b2884a393a59ad64002292a3e90d46a5' % secid)
        try:
            j = em_json(path, key='marketflow_%s' % secid)
            k = ((j.get('data') or {}).get('klines') or [])
            if not k:
                continue
            p = k[0].split(',')
            total['main'] += fnum(p[1])
            total['small'] += fnum(p[2])
            total['mid'] += fnum(p[3])
            total['big'] += fnum(p[4])
            total['xbig'] += fnum(p[5])
            ok += 1
        except Exception as e:
            warn('主力资金(%s)抓取失败: %s' % (secid, e))
    if ok == 1:
        warn('主力资金只取到单边市场（应为沪深两市），该数值不完整')
    return total if ok else None


def fetch_pools(trade_date):
    """涨停/跌停/炸板家数。trade_date: 'YYYYMMDD'"""
    conf = [('zt', 'getTopicZTPool', 'fbt:asc'),
            ('dt', 'getTopicDTPool', 'fund:asc'),
            ('zb', 'getTopicZBPool', 'fbt:asc')]

    def get_pool(item):
        key, api, sort = item
        url = ('https://push2ex.eastmoney.com/%s?ut=7eea3edcaed734bea9cbfc24409ed989'
               '&dpt=wz.ztzt&Pageindex=0&pagesize=1&sort=%s&date=%s' % (api, sort, trade_date))
        try:
            j = HTTP.json(url, key='pool_%s_%s' % (key, trade_date),
                          referer='https://quote.eastmoney.com/')
            return (j.get('data') or {}).get('tc'), None
        except Exception as e:
            return None, e

    # 三个池互不依赖，并发抓取
    vals = parallel_map(get_pool, conf, workers=len(conf))
    res = {}
    for (key, _, _), (tc, err) in zip(conf, vals):
        res[key] = tc
        if err is not None:
            warn('%s 池抓取失败: %s' % (key, err))
        elif tc is None:
            warn('%s 池抓取失败（接口未返回 tc 字段）' % key)
    return res


def fetch_margin(days=6):
    """两融余额（东财数据中心）。"""
    url = ('https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPTA_RZRQ_LSHJ'
           '&columns=ALL&source=WEB&client=WEB&sortColumns=DIM_DATE&sortTypes=-1'
           '&pageSize=%d&pageNumber=1' % days)
    j = HTTP.json(url, key='margin', referer='https://data.eastmoney.com/')
    rows = []
    for r in ((j.get('result') or {}).get('data') or []):
        rows.append({
            'date': (r.get('DIM_DATE') or '')[:10],
            'total': fnum(r.get('RZRQYE')),
            'rz': fnum(r.get('RZYE')),
            'rq': fnum(r.get('RQYE')),
        })
    return rows


def fetch_kline(secid, lmt=70):
    """日K收盘序列，用于均线。返回 [(date, close)]"""
    path = ('/api/qt/stock/kline/get?secid=%s&fields1=f1,f2&fields2=f51,f53'
            '&klt=101&fqt=1&end=20500101&lmt=%d&ut=%s' % (secid, lmt, EM_UT))
    j = em_json(path, hosts=HIS_HOSTS, key='kline_%s_%d' % (secid.replace('.', '_'), lmt))
    rows = []
    for r in (j.get('data') or {}).get('klines') or []:
        p = r.split(',')
        rows.append((p[0], fnum(p[1])))
    return rows


def fetch_turnover_history(secid, lmt=10):
    """指数日K的成交额序列 [(date, close, amount)]"""
    path = ('/api/qt/stock/kline/get?secid=%s&fields1=f1,f2&fields2=f51,f53,f57'
            '&klt=101&fqt=1&end=20500101&lmt=%d&ut=%s' % (secid, lmt, EM_UT))
    j = em_json(path, hosts=HIS_HOSTS, key='turnover_%s_%d' % (secid.replace('.', '_'), lmt))
    rows = []
    for r in (j.get('data') or {}).get('klines') or []:
        p = r.split(',')
        rows.append((p[0], fnum(p[1]), fnum(p[2])))
    return rows


def fetch_news(max_pages=3, page_size=100):
    """东财 7x24 快讯（各页并发抓取，单页失败只丢该页，不再整段放弃）。"""
    def get_page(pi):
        url = ('https://np-listapi.eastmoney.com/comm/web/getFastNewsList?client=web'
               '&biz=web_724&fastColumn=102&sortEnd=&pageSize=%d&req_trace=1&page_index=%d'
               % (page_size, pi))
        try:
            j = HTTP.json(url, key='news_p%d' % pi, tries=3,
                          referer='https://kuaixun.eastmoney.com/')
            return (((j or {}).get('data') or {}).get('fastNewsList') or []), None
        except Exception as e:
            return [], e

    pages = list(range(1, max_pages + 1))
    results = parallel_map(get_page, pages, workers=max(1, min(len(pages), 4)))
    out = []
    for pi, (lst, err) in zip(pages, results):
        if err is not None:
            warn('新闻第%d页失败: %s' % (pi, err))
            continue
        out.extend(lst)

    seen, rows = set(), []
    for it in out:
        t = (it.get('title') or '').strip()
        if not t or t in seen:
            continue
        seen.add(t)
        rows.append({
            'time': (it.get('showTime') or '')[:16],
            'title': t,
            'summary': (it.get('summary') or '').strip(),
        })
    return rows


# ================================================================ 分析计算
def analyze(breadth, indices, market_flow, margin, pools, kl):
    a = {}
    n = len(breadth)
    a['n'] = n

    # 一次遍历完成涨跌家数、分布等统计，避免对 5000+ 只股票多次扫描
    up = down = flat = 0
    limit_up = limit_dn = big_up = big_dn = 0
    pcts = []
    # 分布桶顺序与展示一致
    dist_counts = [0] * 10
    for x in breadth:
        p = x['pct']
        pcts.append(p)
        if p > 0:
            up += 1
        elif p < 0:
            down += 1
        else:
            flat += 1
        if p >= 9.8:
            limit_up += 1
        if p <= -9.8:
            limit_dn += 1
        if p >= 5:
            big_up += 1
        if p <= -5:
            big_dn += 1
        # 按区间累加
        if p >= 9.8:
            dist_counts[0] += 1
        elif p >= 7:
            dist_counts[1] += 1
        elif p >= 5:
            dist_counts[2] += 1
        elif p >= 2:
            dist_counts[3] += 1
        elif p >= 0:
            dist_counts[4] += 1
        elif p >= -2:
            dist_counts[5] += 1
        elif p >= -5:
            dist_counts[6] += 1
        elif p >= -7:
            dist_counts[7] += 1
        elif p > -9.8:
            dist_counts[8] += 1
        else:
            dist_counts[9] += 1

    bucket_names = ['>=9.8%', '7~9.8%', '5~7%', '2~5%', '0~2%',
                    '-2~0%', '-5~-2%', '-7~-5%', '-9.8~-7%', '<=-9.8%']

    a['up'] = up
    a['down'] = down
    a['flat'] = flat
    a['limit_up'] = limit_up
    a['limit_dn'] = limit_dn
    a['big_up'] = big_up
    a['big_dn'] = big_dn
    pcts.sort()
    a['median'] = pcts[n // 2] if n else 0
    a['mean'] = (sum(pcts) / float(n)) if n else 0
    # 用堆取 TOP-N，避免对全量股票做完整排序
    a['top_gainers'] = heapq.nlargest(15, breadth, key=lambda x: x['pct'])
    a['top_losers'] = heapq.nsmallest(15, breadth, key=lambda x: x['pct'])
    a['top_turnover'] = heapq.nlargest(20, breadth, key=lambda x: x['amount'])
    a['dist'] = list(zip(bucket_names, dist_counts))   # py3 下 zip 是迭代器，这里固化成列表

    # 两市成交额（沪+深）
    sh = indices.get('sh000001') or {}
    sz = indices.get('sz399001') or {}
    a['amount_sh'] = sh.get('amount', 0)
    a['amount_sz'] = sz.get('amount', 0)
    a['amount_2m'] = a['amount_sh'] + a['amount_sz']

    # 均线
    a['ma'] = {}
    for _, nm, secid in INDICES:
        if not secid or secid not in kl:
            continue
        cl = [c for _, c in kl[secid]]
        if len(cl) < 20:
            continue
        rec = {'close': cl[-1]}
        for p in (5, 10, 20, 60):
            rec['ma%d' % p] = (sum(cl[-p:]) / float(p)) if len(cl) >= p else None
        a['ma'][nm] = rec

    a['flow'] = market_flow
    a['margin'] = margin
    a['pools'] = pools
    # 炸板率
    zt, zb = pools.get('zt'), pools.get('zb')
    a['zbl'] = (zb / float(zt + zb) * 100) if (zt is not None and zb is not None and (zt + zb) > 0) else None
    return a


def build_signals(a, idx, turnover_hist, glob):
    """规则化多空信号。阈值写死在下面，便于按自己习惯调整。"""
    sup, pre = [], []
    sh = idx.get('sh000001') or {}
    close = sh.get('price', 0)
    m = a['ma'].get('上证指数') or {}

    # 1) 均线结构
    mas = [m.get('ma5'), m.get('ma10'), m.get('ma20'), m.get('ma60')]
    mas = [x for x in mas if x]
    if close and mas and close < min(mas):
        sup_lo, sup_hi = min(mas), max(mas)
        pre.append('均线空头排列：上证收于 MA5/MA10/MA20/MA60 全部下方，上方 %.0f–%.0f 为密集压力区' % (sup_lo, sup_hi))
    elif mas and close > max(mas):
        sup.append('均线多头排列：上证站上全部主要均线，趋势偏强')
    else:
        sup.append('上证位于部分均线之上，短期结构中性')

    # 2) 量能
    if len(turnover_hist) >= 2:
        cur_amt = turnover_hist[-1][2]
        prev_amt = turnover_hist[-2][2]
        if prev_amt > 0:
            delta = (cur_amt / prev_amt - 1) * 100
            if delta <= -8:
                sup.append('缩量 %.1f%%（沪市 %.0f亿 vs 前日 %.0f亿），杀跌动能衰减' % (delta, cur_amt / 1e8, prev_amt / 1e8))
            elif delta >= 12:
                if sh.get('pct', 0) < 0:
                    pre.append('放量下跌 %.1f%%（沪市 %.0f亿），抛压明显' % (delta, cur_amt / 1e8))
                else:
                    sup.append('放量上涨 %.1f%%（沪市 %.0f亿），资金进场' % (delta, cur_amt / 1e8))

    # 3) 指数与个股背离
    if a['n']:
        ratio = a['up'] / float(a['n']) * 100
        if ratio >= 50 and sh.get('pct', 0) < 0:
            sup.append('指数与个股背离：上涨 %d 家 / 下跌 %d 家（中位数 %+.2f%%），指数跌但个股普涨，非全面避险'
                       % (a['up'], a['down'], a['median']))
        elif ratio <= 30:
            pre.append('个股普跌：仅 %d 家上涨 / %d 家下跌，赚钱效应差' % (a['up'], a['down']))

    # 4) 主力资金
    if a.get('flow'):
        main = a['flow']['main']
        if main < 0:
            pre.append('主力资金净流出 %.0f 亿，增量资金不足' % (abs(main) / 1e8))
        else:
            sup.append('主力资金净流入 %.0f 亿' % (main / 1e8))

    # 5) 情绪
    if a.get('zbl') is not None:
        if a['zbl'] >= 30:
            pre.append('炸板率约 %.0f%%，高位承接一般，不宜追高连板' % a['zbl'])
        else:
            sup.append('炸板率约 %.0f%%，情绪承接尚可' % a['zbl'])

    # 6) 两融趋势
    mg = a.get('margin') or []
    if len(mg) >= 3:
        if mg[0]['total'] < mg[2]['total']:
            pre.append('两融余额连续回落（最新 %.0f 亿），杠杆资金退潮' % (mg[0]['total'] / 1e8))
        else:
            sup.append('两融余额回升（最新 %.0f 亿）' % (mg[0]['total'] / 1e8))

    # 7) 外围
    gmap = dict((g['name'], g) for g in glob)
    ks = gmap.get('韩国KOSPI')
    nk = gmap.get('日经225')
    if ks and ks['pct'] <= -2:
        pre.append('韩国KOSPI %+.2f%%（半导体权重高），外围科技情绪偏弱，关注对A股半导体/存储的传导' % ks['pct'])
    if nk and nk['pct'] <= -1:
        pre.append('日经225 %+.2f%%，亚太风险偏好走弱' % nk['pct'])
    us = [gmap.get(x) for x in ('道琼斯', '标普500', '纳斯达克') if gmap.get(x)]
    if us and all(g['pct'] > 0 for g in us):
        sup.append('隔夜美股三大指数收涨（道指 %+.2f%% / 标普 %+.2f%% / 纳指 %+.2f%%）'
                   % (gmap['道琼斯']['pct'], gmap['标普500']['pct'], gmap['纳斯达克']['pct']))

    # 8) 关键点位
    levels = {}
    if sh:
        lo = sh.get('low', 0)
        levels['support'] = [round(lo, 0)]
        if lo:
            levels['support'].append(round(lo - 20, 0))
        if mas:
            levels['resistance'] = [round(min(mas), 0), round(max(mas), 0)]
        levels['low'] = lo
        levels['high'] = sh.get('high', 0)
    return sup, pre, levels


# ================================================================ 摘要数据
def build_summary(a, idx, boards, sup, pre, lv, trade_date, report_path):
    """把关键盘面数据抽成结构化摘要，供飞书推送 / 二次分析使用。"""
    def g(code):
        d = idx.get(code)
        if not d:
            return None
        return {'name': d['name'], 'close': round(d['price'], 2),
                'pct': round(d['pct'], 2), 'amount': d['amount']}

    def top(key, n=4):
        rows = boards.get(key) or []
        return [{'name': x['name'], 'pct': round(x['pct'], 2),
                 'flow_yi': round(x['flow'] / 1e8, 1)} for x in rows[:n]]

    f = a.get('flow') or {}
    mg = (a.get('margin') or [{}])[0]
    s = {
        'trade_date': trade_date,
        'report_path': report_path,
        'indices': {
            'sh': g('sh000001'), 'sz': g('sz399001'), 'cyb': g('sz399006'),
            'kc50': g('sh000688'), 'hs300': g('sh000300'), 'zz500': g('sh000905'),
            'zz1000': g('sh000852'), 'gz2000': g('sz399303'), 'bj50': g('bj899050'),
        },
        'turnover_yi': round(a['amount_2m'] / 1e8, 0),
        'turnover_sh_yi': round(a['amount_sh'] / 1e8, 0),
        'breadth': {
            'total': a.get('total_all', a['n']), 'quoted': a['n'],
            'up': a['up'], 'down': a['down'], 'flat': a['flat'],
            'median_pct': round(a['median'], 2),
            'limit_up': a['pools'].get('zt'), 'limit_dn': a['pools'].get('dt'),
            'broken': a['pools'].get('zb'),
            'broken_rate': round(a['zbl'], 0) if a.get('zbl') is not None else None,
        },
        'fund': {
            'main_yi': round(f.get('main', 0) / 1e8, 1) if f else None,
            'xbig_yi': round(f.get('xbig', 0) / 1e8, 1) if f else None,
            'small_yi': round(f.get('small', 0) / 1e8, 1) if f else None,
            'margin_date': mg.get('date'), 
            'margin_yi': round(mg.get('total', 0) / 1e8, 0) if mg.get('total') else None,
        },
        'leaders': {'industry_up': top('ind_up'), 'industry_dn': top('ind_dn'),
                    'concept_up': top('con_up'), 'concept_dn': top('con_dn')},
        'signals': {'support': sup, 'pressure': pre},
        'levels': lv,
    }
    return s


# ================================================================ 报告渲染
def yi(v):
    return '%.1f亿' % (v / 1e8) if abs(v) >= 1e8 else '%.0f万' % (v / 1e4)


def render(a, idx, boards, flows, news, glob, comm, turnover_hist, trade_date,
           signals=None):
    L = []
    add = L.append
    sh = idx.get('sh000001') or {}
    sh_time = ('%s %s' % (sh.get('date', ''), sh.get('time', ''))).strip()

    add('# A股市场复盘与明日展望')
    add('')
    add('**交易日：%s**' % trade_date)
    add('')
    add('**数据口径**：A股为收盘数据（行情时间 %s）；外围为最新可得数据。' % sh_time)
    add('数据来源：东方财富 / 新浪财经公开行情接口。本报告由 `stock_review.py` 自动生成。')
    add('')
    add('---')
    add('')

    # ---------------- 一、大盘
    add('## 一、大盘概览')
    add('')
    add('| 指数 | 收盘 | 涨跌幅 | 涨跌点 | 成交额 |')
    add('|---|---|---|---|---|')
    for code, nm, _ in INDICES:
        d = idx.get(code)
        if not d:
            continue
        add('| %s | %.2f | %+.2f%% | %+.2f | %s |'
            % (nm, d['price'], d['pct'], d['chg'],
               ('%.0f亿' % (d['amount'] / 1e8)) if d['amount'] > 0 else '—'))
    add('')
    add('**两市成交额：%.0f 亿元**（沪市 %.0f 亿 + 深市 %.0f 亿）'
        % (a['amount_2m'] / 1e8, a['amount_sh'] / 1e8, a['amount_sz'] / 1e8))
    if len(turnover_hist) >= 2:
        prev = turnover_hist[-2]
        add('前一交易日（%s）沪市成交 %.0f 亿，今日 %s'
            % (prev[0], prev[2] / 1e8,
               ('缩量 %.0f 亿（%.1f%%）' % (prev[2] / 1e8 - a['amount_sh'] / 1e8,
                                        (a['amount_sh'] / prev[2] - 1) * 100))
               if prev[2] else '—'))
    add('')

    # ---------------- 二、宽度与情绪
    add('## 二、市场宽度与情绪')
    add('')
    add('| 指标 | 数值 |')
    add('|---|---|')
    add('| 有效报价 / 全市场 | %d / %s 只（其余 %s 只停牌或无报价） |'
        % (a['n'], a.get('total_all', a['n']), a.get('suspended', 0)))
    add('| 上涨 / 下跌 / 平盘 | **%d / %d / %d** |' % (a['up'], a['down'], a['flat']))
    add('| 涨停 / 跌停（涨停池口径） | %s / %s |' % (a['pools'].get('zt', '—'), a['pools'].get('dt', '—')))
    add('| 炸板家数 / 炸板率 | %s / %s |'
        % (a['pools'].get('zb', '—'), ('%.0f%%' % a['zbl']) if a.get('zbl') is not None else '—'))
    add('| 涨幅≥9.8%% / 跌幅≤-9.8%% | %d / %d |' % (a['limit_up'], a['limit_dn']))
    add('| 涨超5%% / 跌超5%% | %d / %d |' % (a['big_up'], a['big_dn']))
    add('| 中位涨跌幅 / 平均涨跌幅 | %+.2f%% / %+.2f%% |' % (a['median'], a['mean']))
    add('')
    add('**涨跌幅分布**')
    add('')
    add('```')
    for nm, cnt in a['dist']:
        add('  %-9s %5d  %s' % (nm, cnt, '#' * int(cnt / 50)))
    add('```')
    add('')

    # ---------------- 三、资金面
    add('## 三、资金面')
    add('')
    if a.get('flow'):
        f = a['flow']
        add('- 主力净额 **%s**（超大单 %s、大单 %s、中单 %s），小单 %s'
            % (yi(f['main']), yi(f['xbig']), yi(f['big']), yi(f['mid']), yi(f['small'])))
    if a.get('margin'):
        m0 = a['margin'][0]
        add('- 两融余额（%s）：**%.0f 亿元**（融资 %.0f 亿、融券 %.0f 亿）'
            % (m0['date'], m0['total'] / 1e8, m0['rz'] / 1e8, m0['rq'] / 1e8))
        if len(a['margin']) >= 3:
            hist = list(reversed(a['margin']))     # 按时间正序展示
            add('- 近 %d 个交易日两融余额：%s'
                % (len(hist), ' → '.join('%.0f亿' % (x['total'] / 1e8) for x in hist)))
    add('')
    if flows.get('concept_in') or flows.get('concept_out'):
        add('**概念资金净流入 TOP**')
        add('')
        add('| 概念 | 涨跌幅 | 主力净额 |')
        add('|---|---|---|')
        for x in flows.get('concept_in') or []:
            add('| %s | %+.2f%% | %s |' % (x['name'], x['pct'], yi(x['flow'])))
        add('')
        add('**概念资金净流出 TOP**')
        add('')
        add('| 概念 | 涨跌幅 | 主力净额 |')
        add('|---|---|---|')
        for x in flows.get('concept_out') or []:
            add('| %s | %+.2f%% | %s |' % (x['name'], x['pct'], yi(x['flow'])))
        add('')
    if flows.get('industry_in') or flows.get('industry_out'):
        add('**行业资金净流入 TOP**：%s'
            % '、'.join('%s %s' % (x['name'], yi(x['flow'])) for x in flows.get('industry_in') or []))
        add('')
        add('**行业资金净流出 TOP**：%s'
            % '、'.join('%s %s' % (x['name'], yi(x['flow'])) for x in flows.get('industry_out') or []))
        add('')

    # ---------------- 四、板块结构
    add('## 四、板块结构')
    add('')
    add('### 行业板块')
    add('')
    add('**领涨**：' + '、'.join('%s %+.2f%%' % (x['name'], x['pct']) for x in boards.get('ind_up') or []))
    add('')
    add('**领跌**：' + '、'.join('%s %+.2f%%' % (x['name'], x['pct']) for x in boards.get('ind_dn') or []))
    add('')
    add('### 概念板块')
    add('')
    add('**领涨**：' + '、'.join('%s %+.2f%%' % (x['name'], x['pct']) for x in boards.get('con_up') or []))
    add('')
    add('**领跌**：' + '、'.join('%s %+.2f%%' % (x['name'], x['pct']) for x in boards.get('con_dn') or []))
    add('')

    # ---------------- 五、个股
    add('## 五、个股榜单')
    add('')
    stock_tables = [('涨幅榜 TOP15', a['top_gainers']),
                    ('跌幅榜 TOP15', a['top_losers']),
                    ('成交额榜 TOP20', a['top_turnover'])]
    for title, rows in stock_tables:
        add('**%s**' % title)
        add('')
        add('| 代码 | 名称 | 涨跌幅 | 成交额 |')
        add('|---|---|---|---|')
        for x in rows:
            add('| %s | %s | %+.2f%% | %.2f亿 |'
                % (x['code'], x['name'], x['pct'], x['amount'] / 1e8))
        add('')

    # ---------------- 六、消息面
    if news:
        add('## 六、市场消息（自动筛选）')
        add('')
        hits = [n for n in news if any(k in n['title'] for k in NEWS_KEYWORDS)]
        today_hits = [n for n in hits if n['time'].startswith(trade_date)]
        use = today_hits if today_hits else hits
        for n in use[:40]:
            add('- `%s` %s' % (n['time'][11:] or n['time'], n['title']))
        if len(use) > 40:
            add('- …（另有 %d 条，详见原始快讯）' % (len(use) - 40))
        add('')

    # ---------------- 七、外围
    add('## 七、外围市场')
    add('')
    add('| 市场 | 最新 | 涨跌幅 |')
    add('|---|---|---|')
    for g in glob:
        add('| %s | %s | %s |'
            % (g['name'], ('%.2f' % g['price']) if g['price'] else '—',
               ('%+.2f%%' % g['pct']) if g['pct'] else '—'))
    add('')
    if comm:
        add('| 商品 / 汇率 | 最新 | 涨跌幅 |')
        add('|---|---|---|')
        for k in ('hf_CL', 'hf_GC', 'hf_SI', 'USDCNY'):
            c = comm.get(k)
            if not c:
                continue
            add('| %s | %.4f | %s |'
                % (c['name'], c['price'],
                   ('%+.2f%%' % c['pct']) if c.get('pct') is not None else '—'))
        add('')

    # ---------------- 八、技术位置
    add('## 八、技术位置（均线）')
    add('')
    add('| 指数 | 收盘 | MA5 | MA10 | MA20 | MA60 | 距MA20 |')
    add('|---|---|---|---|---|---|---|')
    for _, nm, _ in INDICES:
        r = a['ma'].get(nm)
        if not r:
            continue
        ma20 = r.get('ma20')
        add('| %s | %.2f | %s | %s | %s | %s | %s |'
            % (nm, r['close'],
               '%.0f' % r['ma5'] if r.get('ma5') else '—',
               '%.0f' % r['ma10'] if r.get('ma10') else '—',
               '%.0f' % ma20 if ma20 else '—',
               '%.0f' % r['ma60'] if r.get('ma60') else '—',
               ('%+.2f%%' % ((r['close'] / ma20 - 1) * 100)) if ma20 else '—'))
    add('')

    # ---------------- 九、明日展望
    sup, pre, lv = signals if signals else build_signals(a, idx, turnover_hist, glob)
    add('## 九、下一交易日展望（规则化输出）')
    add('')
    add('> 以下为**按固定阈值自动生成的多空信号**，用于快速定位当日的矛盾点；')
    add('> 具体仓位与买卖决策需结合政策面、事件日历与个人风险偏好人工复核。')
    add('')
    add('### 支持企稳 / 偏多的信号')
    add('')
    if sup:
        for s in sup:
            add('- %s' % s)
    else:
        add('- （无）')
    add('')
    add('### 压制反弹 / 偏空的信号')
    add('')
    if pre:
        for s in pre:
            add('- %s' % s)
    else:
        add('- （无）')
    add('')
    if lv:
        add('### 关键点位（上证指数）')
        add('')
        if lv.get('resistance'):
            add('- **压力**：%.0f – %.0f（均线密集区）' % (lv['resistance'][0], lv['resistance'][1]))
        if lv.get('support'):
            add('- **支撑**：%s' % ' → '.join('%.0f' % x for x in lv['support']))
        add('- 今日振幅区间：%.2f – %.2f' % (lv.get('low', 0), lv.get('high', 0)))
        add('')

    # ---------------- 十、缺口与声明
    if ERRORS:
        add('## 十、数据缺口')
        add('')
        for e in ERRORS:
            add('- %s' % e)
        add('')
    add('---')
    add('')
    add('*本报告由脚本自动抓取公开行情数据生成，仅作数据整理与逻辑推演，不构成任何投资建议。市场有风险，决策需谨慎。*')
    return '\n'.join(L)


# ================================================================ 主流程
def main():
    ap = argparse.ArgumentParser(description='A股每日复盘一键脚本')
    ap.add_argument('--out', default=None, help='输出 Markdown 路径')
    ap.add_argument('--date', default=None,
                    help='离线复盘指定交易日（YYYYMMDD），只读本地缓存、不联网')
    ap.add_argument('--dir', default=None, help='输出目录（默认脚本同级 reports/）')
    ap.add_argument('--pages', type=int, default=70, help='市场宽度最大分页数（默认70）')
    ap.add_argument('--news-pages', type=int, default=3, help='新闻抓取页数（默认3，每页100条）')
    ap.add_argument('--no-news', action='store_true', help='跳过新闻抓取')
    ap.add_argument('--no-breadth', action='store_true', help='跳过全市场宽度抓取（最快，仅指数与板块）')
    ap.add_argument('--top', type=int, default=15, help='板块榜单条数')
    ap.add_argument('--quiet', action='store_true', help='只写文件，不打印摘要')
    ap.add_argument('--workers', type=int, default=8, help='并发线程数（默认8）')
    ap.add_argument('--refresh', action='store_true', help='忽略本地缓存，强制重新抓取')
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--live', dest='live', action='store_true',
                   help='强制抓实时数据（盘中默认已自动开启）')
    g.add_argument('--no-live', dest='live', action='store_false',
                   help='即使处在交易时段也走缓存，不强制实时')
    ap.set_defaults(live=None)
    ap.add_argument('--cache-dir', default=None, help='缓存目录（默认脚本同级 .cache/<交易日>/）')
    ap.add_argument('--push', action='store_true',
                    help='生成后把摘要推送到飞书（需先配置 push_config.json）')
    ap.add_argument('--push-dry-run', action='store_true',
                    help='只打印将要推送的卡片，不真正发送')
    ap.add_argument('--push-config', default=None,
                    help='推送配置文件路径（默认脚本同级 push_config.json）')
    ap.add_argument('--push-provider', default=None,
                    help='覆盖配置文件里的渠道：feishu / feishu_app / custom')
    args = ap.parse_args()

    t0 = time.time()
    log('开始抓取数据 ...')

    base = os.path.dirname(os.path.abspath(__file__))
    want_date = args.date
    d = None

    # 0) 盘中实时模式：A 股交易时段内默认绕过缓存，
    #    这样一天里多次运行/多次推送拿到的是各自时点的最新快照，而不是早盘的旧数据。
    #    同时盘中数据不写回缓存，保证缓存里始终是"收盘定稿"的那一份。
    now = datetime.datetime.now()
    intraday = False
    if not want_date:
        in_session = is_trading_session(now)
        live = in_session if args.live is None else args.live
        intraday = bool(live and in_session)
        if live:
            args.refresh = True
            if args.live:
                log('--live：本次强制抓取实时数据（不吃缓存）')
            else:
                log('当前 %s 处于 A 股交易时段，自动改为抓取实时数据（不吃缓存）；'
                    '如需走缓存加 --no-live' % now.strftime('%H:%M'))
            if intraday:
                log('盘中快照不写回缓存，避免覆盖收盘定稿数据')

    # 1) 离线复盘：先把缓存目录定下来，再从缓存读指数
    if want_date:
        d = want_date
        if len(d) == 8 and '-' not in d:
            d = '%s-%s-%s' % (d[:4], d[4:6], d[6:])
        cachedir = args.cache_dir or os.path.join(base, '.cache', d)
        if not os.path.isdir(cachedir):
            print('离线复盘需要该交易日的缓存目录，但不存在：%s' % cachedir)
            print('（请先联网正常跑一次该交易日，或用 --cache-dir 指定缓存位置）')
            return 2
        OFFLINE[0] = True
        for c in (EM, EM_HIS, HTTP):
            c.cache_dir = cachedir
            c.refresh = False
        log('离线复盘模式：只读缓存 %s' % cachedir)

    # 2) 指数
    try:
        idx, sina_raw = fetch_indices()
    except Exception as e:
        print('指数抓取失败，无法继续：%s' % e)
        return 2
    sh = idx.get('sh000001') or {}
    trade_date = sh.get('date') or datetime.date.today().strftime('%Y-%m-%d')
    log('交易日 = %s，行情时间 %s' % (trade_date, sh.get('time', '')))
    trade_compact = trade_date.replace('-', '')

    if want_date and trade_date != d:
        warn('缓存的行情日期是 %s，与请求的 %s 不一致，请确认缓存目录是否正确' % (trade_date, d))

    if not OFFLINE[0]:
        today = datetime.date.today().strftime('%Y-%m-%d')
        if trade_date != today:
            log('提示：行情日期(%s) 与系统日期(%s) 不一致，可能为非交易日或数据未更新。'
                % (trade_date, today))
        # 缓存目录：按交易日隔离。抓取成功即落盘，重跑秒级完成，缺页可续跑。
        cachedir = args.cache_dir or os.path.join(base, '.cache', trade_date)
        if not os.path.isdir(cachedir):
            os.makedirs(cachedir)
        for c in (EM, EM_HIS, HTTP):
            c.cache_dir = cachedir
            c.refresh = args.refresh
            c.no_write = intraday
        log('缓存目录 = %s%s' % (cachedir, '（--refresh：忽略缓存）' if args.refresh else ''))
        if not intraday:
            try:                   # 保存指数原始文本，供日后离线复盘
                with codecs.open(os.path.join(cachedir, 'sina_indices.txt'), 'w', 'utf-8') as fp:
                    fp.write(sina_raw)
            except Exception:
                pass

    # 3) 轻量预热：只探首号主机一次，失败也不阻塞
    #    （离线模式跳过；缓存已就绪时也无需探测，可直接省下一次往返）
    try:
        n_cached = len(os.listdir(cachedir)) if os.path.isdir(cachedir) else 0
    except Exception:
        n_cached = 0
    if not OFFLINE[0] and (args.refresh or n_cached < 5):
        try:
            probe = ('https://%s/api/qt/clist/get?pn=1&pz=1&po=1&np=1&fltt=2&invt=2'
                     '&fid=f3&fs=m:1+t:2&fields=f3,f14&ut=%s' % (EM_HOSTS[0], EM_UT))
            http_json(probe, referer='https://quote.eastmoney.com/', timeout=8, retries=1)
            EM.good = EM_HOSTS[0]
            log('行情主机 = %s' % EM.good)
        except Exception:
            log('行情主机预热未命中（不阻塞，后续按需自动切换）')
    elif not OFFLINE[0]:
        log('缓存已就绪（%d 个文件），跳过行情主机预热' % n_cached)

    # 4) 板块（并发抓取，批次级重试）
    boards, flows = {}, {}
    bspec = [('ind_up', 'industry', 1), ('ind_dn', 'industry', 0),
             ('con_up', 'concept', 1), ('con_dn', 'concept', 0)]
    def _fetch_boards():
        res = parallel_map(lambda sp: fetch_board(sp[1], po=sp[2], size=args.top),
                           bspec, args.workers)
        return res if any(r is not None for r in res) else None
    bres = retry_call(_fetch_boards, attempts=3, delay=4.0, label='板块涨跌') or [None] * len(bspec)
    for (key, kind, po), r in zip(bspec, bres):
        if r is not None:
            boards[key] = r
        else:
            warn('板块涨跌(%s)抓取失败' % key)
    fspec = [('industry_in', 'industry', 1), ('industry_out', 'industry', 0),
             ('concept_in', 'concept', 1), ('concept_out', 'concept', 0)]
    def _fetch_flows():
        res = parallel_map(lambda sp: fetch_board_flow(sp[1], po=sp[2], size=8),
                           fspec, args.workers)
        return res if any(r is not None for r in res) else None
    fres = retry_call(_fetch_flows, attempts=3, delay=4.0, label='板块资金') or [None] * len(fspec)
    for (key, kind, po), r in zip(fspec, fres):
        if r is not None:
            flows[key] = r
        else:
            warn('板块资金(%s)抓取失败' % key)

    # 5) 主力资金 / 两融 / 涨跌停池 / 外围 / 商品：彼此独立，一次并发抓完
    #    （原先这 5 个源是串行请求，是整条链路上最大的一块可压缩等待时间）
    src = fetch_all_sources([
        ('主力资金', fetch_market_flow),
        ('两融数据', fetch_margin),
        ('涨跌停池', lambda: fetch_pools(trade_compact)),
        ('外围市场', fetch_global),
        ('商品/汇率', fetch_commodity_fx),
    ], args.workers)
    market_flow = src.get('主力资金')
    margin = src.get('两融数据') or []
    pools = src.get('涨跌停池') or {}
    glob = src.get('外围市场') or []
    comm = src.get('商品/汇率') or {}

    # 6) 均线 + 成交额历史（并发抓取）
    kl, turnover_hist = {}, []
    kspec = [(nm, secid) for _, nm, secid in INDICES if secid]
    kres = parallel_map(lambda sp: fetch_kline(sp[1], lmt=70), kspec, args.workers)
    for (nm, secid), r in zip(kspec, kres):
        if r:
            kl[secid] = r
        else:
            warn('%s K线抓取失败' % nm)
    try:
        turnover_hist = fetch_turnover_history('1.000001', lmt=10)
    except Exception as e:
        warn('成交额历史抓取失败: %r' % (e,))

    # 8) 市场宽度（最重，约 60 页，放在最后）
    breadth, total_all, suspended = [], 0, 0
    if not args.no_breadth:
        log('抓取全市场个股（最多 %d 页，已缓存的可跳过）...' % args.pages)
        try:
            breadth, total_all, suspended = fetch_breadth(max_pages=args.pages,
                                                          workers=args.workers)
            log('  可取报价 %d 只 / 全市场 %s 只（停牌无报价 %d 只）'
                % (len(breadth), total_all, suspended))
        except Exception as e:
            warn('市场宽度抓取失败: %r' % (e,))

    # 9) 新闻
    news = []
    if not args.no_news:
        log('抓取快讯 ...')
        try:
            news = fetch_news(max_pages=args.news_pages)
            log('  取到 %d 条' % len(news))
        except Exception as e:
            warn('新闻抓取失败: %s' % e)

    # 10) 分析 + 渲染
    log('计算指标并生成报告 ...')
    if not breadth:
        # 无宽度数据时给个空壳，保证报告仍能生成
        breadth = []
    a = analyze(breadth, idx, market_flow, margin, pools, kl)
    a['total_all'] = total_all
    a['suspended'] = suspended
    sup, pre, lv = build_signals(a, idx, turnover_hist, glob)
    md = render(a, idx, boards, flows, news, glob, comm, turnover_hist, trade_date,
                signals=(sup, pre, lv))

    outdir = args.dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'reports')
    if not os.path.isdir(outdir):
        os.makedirs(outdir)
    outpath = args.out or os.path.join(outdir, 'A股复盘_%s.md' % trade_date)
    with codecs.open(outpath, 'w', 'utf-8') as fp:
        fp.write(md)

    # 结构化摘要（供推送 / 二次分析使用，避免去解析 Markdown）
    summary = build_summary(a, idx, boards, sup, pre, lv, trade_date, outpath)
    jsonpath = os.path.splitext(outpath)[0] + '.summary.json'
    try:
        with codecs.open(jsonpath, 'w', 'utf-8') as fp:
            fp.write(json.dumps(summary, ensure_ascii=False, indent=2))
    except Exception as e:
        warn('摘要 JSON 写入失败: %r' % (e,))

    log('完成，用时 %.1f 秒' % (time.time() - t0))
    print('报告已生成: %s' % outpath)

    if not args.quiet:
        print('')
        print('=' * 60)
        print('  盘面速览  %s' % trade_date)
        print('=' * 60)
        for code, nm, _ in INDICES[:7]:
            d = idx.get(code)
            if d:
                print('  %-8s %10.2f  %+7.2f%%' % (nm, d['price'], d['pct']))
        if a['n']:
            print('  上涨/下跌      %d / %d   中位数 %+.2f%%' % (a['up'], a['down'], a['median']))
        print('  两市成交额     %.0f 亿' % (a['amount_2m'] / 1e8))
        if a.get('flow'):
            print('  主力净额       %+.0f 亿' % (a['flow']['main'] / 1e8))
        if a.get('zbl') is not None:
            print('  炸板率         %.0f%%' % a['zbl'])
        print('=' * 60)
        print('  报告: %s' % outpath)

    # 飞书推送
    if args.push or args.push_dry_run:
        try:
            import feishu_push
        except ImportError:
            warn('未找到 feishu_push.py，跳过推送')
        else:
            rc = feishu_push.push_file(
                jsonpath,
                config_path=args.push_config,
                dry_run=args.push_dry_run,
                provider=args.push_provider,
            )
            if rc != 0 and not args.push_dry_run:
                warn('飞书推送未成功（返回码 %s）' % rc)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\n已中断')
        sys.exit(130)
