#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
离线自检 (selftest.py)
=========================================================
不联网，用构造数据跑一遍核心纯计算/渲染路径，验证重构后行为一致：
  - analyze()      统计口径（涨跌家数、分布桶边界、中位数/均值、TOP-N）
  - build_signals() 多空信号 + 关键点位
  - render()        Markdown 报告渲染
  - build_summary() 推送摘要结构
  - parse_sina_lines() 新浪行情文本解析
  - feishu_push    卡片渲染、占位符替换、错误码解析

用法：  python selftest.py
兼容 Python 2.7 与 Python 3.x。
"""

from __future__ import print_function, unicode_literals, division

import os
import sys
import json
import copy
import shutil
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import stock_review as sr
import feishu_push as fp

FAILED = []


def check(name, cond, extra=''):
    if cond:
        print('  [ok]   %s' % name)
    else:
        print('  [FAIL] %s %s' % (name, extra))
        FAILED.append(name)


def eq(name, got, want):
    check(name, got == want, '(got=%r want=%r)' % (got, want))


# ---------------------------------------------------------------- 构造数据
def make_breadth():
    """构造 210 只股票，覆盖每个分布桶的边界值。"""
    pcts = [11.0, 9.8, 9.79, 8.0, 7.0, 6.99, 5.0, 4.99, 2.0, 1.99,
            0.0, -0.01, -2.0, -2.01, -5.0, -5.01, -7.0, -7.01,
            -9.8, -9.81, -10.0]
    rows = []
    for i in range(200):
        rows.append({'code': '%06d' % i, 'name': 'S%d' % i,
                     'price': 10.0 + i, 'pct': pcts[i % len(pcts)],
                     'amount': float(1000 + i)})
    return rows


def make_indices():
    return {
        'sh000001': {'name': '上证指数', 'open': 3880.0, 'prev': 3888.0, 'price': 3885.0,
                     'high': 3900.0, 'low': 3867.0, 'amount': 5.0e11, 'pct': -0.08,
                     'chg': -3.0, 'date': '2026-09-14', 'time': '15:00:00'},
        'sz399001': {'name': '深证成指', 'open': 13380.0, 'prev': 13400.0, 'price': 13384.0,
                     'high': 13450.0, 'low': 13300.0, 'amount': 6.0e11, 'pct': -0.12,
                     'chg': -16.0, 'date': '2026-09-14', 'time': '15:00:00'},
        'sz399006': {'name': '创业板指', 'open': 3300.0, 'prev': 3290.0, 'price': 3285.0,
                     'high': 3310.0, 'low': 3270.0, 'amount': 2.0e11, 'pct': -0.15,
                     'chg': -5.0, 'date': '2026-09-14', 'time': '15:00:00'},
    }


def make_kl(n=70):
    """给上证造一段完全单调递减的收盘序列，便于手算均线。"""
    closes = [(str(20260101 + i), 4000.0 - i) for i in range(n)]
    return {'1.000001': closes}


def main():
    print('=== stock_review 自检 ===')
    breadth = make_breadth()
    idx = make_indices()
    market_flow = {'main': -1.65e10, 'small': 1.0e10, 'mid': 2.0e9,
                   'big': -5.0e9, 'xbig': -1.1e10}
    margin = [{'date': '2026-09-14', 'total': 2.6316e12, 'rz': 2.6e12, 'rq': 3.0e10},
              {'date': '2026-09-11', 'total': 2.65e12, 'rz': 2.62e12, 'rq': 3.1e10},
              {'date': '2026-09-10', 'total': 2.66e12, 'rz': 2.63e12, 'rq': 3.2e10}]
    pools = {'zt': 55, 'dt': 16, 'zb': 30}
    kl = make_kl()

    # ---- analyze ----
    a = sr.analyze(breadth, idx, market_flow, margin, pools, kl)
    eq('analyze: 总数', a['n'], 200)
    eq('analyze: up+down+flat == n', a['up'] + a['down'] + a['flat'], 200)

    # 期望值由原始口径独立算出（每个桶的边界区间照抄原实现）
    def in_bucket(v):
        if v >= 9.8:
            return 0
        if 7 <= v < 9.8:
            return 1
        if 5 <= v < 7:
            return 2
        if 2 <= v < 5:
            return 3
        if 0 <= v < 2:
            return 4
        if -2 <= v < 0:
            return 5
        if -5 <= v < -2:
            return 6
        if -7 <= v < -5:
            return 7
        if -9.8 < v < -7:
            return 8
        return 9

    want_dist = [0] * 10
    for x in breadth:
        want_dist[in_bucket(x['pct'])] += 1
    eq('analyze: 分布桶计数', [c for _, c in a['dist']], want_dist)
    eq('analyze: 分布总数守恒', sum(c for _, c in a['dist']), 200)
    eq('analyze: 桶名顺序',
       [n for n, _ in a['dist']],
       ['>=9.8%', '7~9.8%', '5~7%', '2~5%', '0~2%',
        '-2~0%', '-5~-2%', '-7~-5%', '-9.8~-7%', '<=-9.8%'])

    pcts = sorted(x['pct'] for x in breadth)
    eq('analyze: 中位数', a['median'], pcts[100])
    eq('analyze: 最大值进涨幅榜', a['top_gainers'][0]['pct'], max(pcts))
    eq('analyze: 涨幅榜降序', [x['pct'] for x in a['top_gainers']],
       sorted(pcts, reverse=True)[:15])
    eq('analyze: 跌幅榜升序', [x['pct'] for x in a['top_losers']], pcts[:15])
    eq('analyze: TOP-N 长度', (len(a['top_gainers']), len(a['top_losers']),
                               len(a['top_turnover'])), (15, 15, 20))
    eq('analyze: 炸板率', round(a['zbl'], 2), round(30 / 85.0 * 100, 2))
    eq('analyze: 两市成交额', a['amount_2m'], 1.1e12)
    check('analyze: 均线已计算', '上证指数' in a['ma'])

    # ---- build_signals / render / build_summary ----
    sup, pre, lv = sr.build_signals(a, idx, [('2026-09-11', 1.0, 6.0e11),
                                             ('2026-09-14', 1.0, 5.0e11)], [])
    check('signals: 返回三类结果', isinstance(sup, list) and isinstance(pre, list)
          and isinstance(lv, dict))
    check('signals: 有支撑位', bool(lv.get('support')))

    boards = {'ind_up': [{'name': '医疗', 'pct': 5.0, 'flow': 1.0e9}],
              'ind_dn': [{'name': '教育', 'pct': -3.0, 'flow': -1.0e9}],
              'con_up': [{'name': 'CRO', 'pct': 4.0, 'flow': 8.0e8}],
              'con_dn': [{'name': 'ST', 'pct': -2.0, 'flow': -5.0e8}]}
    flows = {'concept_in': [{'name': 'CRO', 'pct': 4.0, 'flow': 8.0e8}],
             'concept_out': [{'name': 'ST', 'pct': -2.0, 'flow': -5.0e8}],
             'industry_in': [{'name': '医疗', 'pct': 5.0, 'flow': 1.0e9}],
             'industry_out': [{'name': '教育', 'pct': -3.0, 'flow': -1.0e9}]}
    news = [{'time': '2026-09-14 15:01', 'title': '收评：沪指跌 0.08%', 'summary': ''},
            {'time': '2026-09-14 09:00', 'title': '无关消息', 'summary': ''}]
    glob = [{'name': '道琼斯', 'price': 40000.0, 'pct': 0.5, 'chg': 200.0, 'ts': 0},
            {'name': '纳斯达克', 'price': 18000.0, 'pct': 0.8, 'chg': 140.0, 'ts': 0},
            {'name': '标普500', 'price': 5500.0, 'pct': 0.3, 'chg': 16.0, 'ts': 0},
            {'name': '韩国KOSPI', 'price': 2600.0, 'pct': -2.5, 'chg': -66.0, 'ts': 0}]
    comm = {'hf_CL': {'name': '纽约原油', 'price': 70.0, 'pct': 1.2},
            'USDCNY': {'name': '美元/人民币', 'price': 7.1, 'pct': None}}
    a['total_all'] = 5300
    a['suspended'] = 320

    md = sr.render(a, idx, boards, flows, news, glob, comm,
                   [('2026-09-11', 1.0, 6.0e11), ('2026-09-14', 1.0, 5.0e11)],
                   '2026-09-14', signals=(sup, pre, lv))
    check('render: 输出非空', len(md) > 2000)
    for section in ('## 一、大盘概览', '## 二、市场宽度与情绪', '## 三、资金面',
                    '## 四、板块结构', '## 五、个股榜单', '## 七、外围市场',
                    '## 八、技术位置（均线）'):
        check('render: 含章节 %s' % section, section in md)
    check('render: 个股三张榜单齐全',
          md.count('| 代码 | 名称 | 涨跌幅 | 成交额 |') == 3)
    check('render: 新闻命中关键词', '收评：沪指跌 0.08%' in md)
    check('render: 未命中关键词的消息被过滤', '无关消息' not in md)

    sm = sr.build_summary(a, idx, boards, sup, pre, lv, '2026-09-14', 'x.md')
    eq('summary: 交易日', sm['trade_date'], '2026-09-14')
    eq('summary: 成交额(亿)', sm['turnover_yi'], round(1.1e12 / 1e8, 0))
    eq('summary: 宽度', (sm['breadth']['up'], sm['breadth']['down']),
       (a['up'], a['down']))
    eq('summary: 主力资金(亿)', sm['fund']['main_yi'], round(-1.65e10 / 1e8, 1))
    check('summary: 领涨板块', bool(sm['leaders']['industry_up']))
    eq('空值不印成 None', sr.na(None), '—')
    eq('板块名去掉罗马数字后缀', sr._board_base('航空装备Ⅲ'), '航空装备')
    eq('板块名去掉另一后缀', sr._board_base('航空装备Ⅱ'), '航空装备')

    pick = sr.build_pick(
        {'ind_up': [{'name': '燃料电池', 'pct': 6.56, 'flow': 0},
                    {'name': '航空装备Ⅲ', 'pct': 5.98, 'flow': 17.5e8},
                    {'name': '航空装备Ⅱ', 'pct': 5.98, 'flow': 17.5e8}]},
        {'concept_in': [{'name': '半导体概念', 'pct': -0.18, 'flow': 39.8e8},
                        {'name': '军工', 'pct': 2.5, 'flow': 37.2e8},
                        {'name': '低空经济', 'pct': 2.45, 'flow': 33.1e8}],
         'industry_in': [{'name': '半导体', 'pct': 0.2, 'flow': 34.2e8},
                         {'name': '国防军工', 'pct': 4.46, 'flow': 29.3e8}],
         'industry_out': [{'name': '传媒', 'pct': -1.2, 'flow': -36.8e8}],
         'concept_out': [{'name': '富时罗素', 'pct': 0.41, 'flow': -62.4e8}]},
        a, {'support': [3681.0], 'resistance': [3758.0, 3882.0]})
    eq('推荐项是价和资金同时成立的军工', pick['name'], '军工')
    eq('推荐项类别', pick['kind'], '概念')
    eq('推荐项净流入', pick['flow_yi'], 37.2)
    eq('弱市立场是观察而不是追', pick['stance'], '防守观察，不追高')
    pick_text = '\n'.join(pick['lines'])
    check('推荐项说明为何不选半导体', '半导体概念' in pick_text and '不列入推荐' in pick_text)
    check('推荐项说明为何不选燃料电池', '燃料电池' in pick_text)
    check('推荐项写明资金撤出的传媒', '传媒' in pick_text)
    check('推荐项不把指数篮子当方向', '富时罗素' not in pick_text)
    check('推荐项写明失效价', '3681' in pick_text)
    empty_pick = sr.build_pick({}, {}, a, {})
    eq('没有同时成立的板块就不推荐', empty_pick['name'], None)
    eq('空榜立场', empty_pick['stance'], '今天不推荐方向')
    check('render: 含推荐关注', '推荐关注（仅一项）' in md)
    check('render: 推荐用了资金榜而不是只看涨幅', '医疗' in md and '主力净流入' in md)

    # ---- parse_sina_lines ----
    txt = ('var hq_str_sh000001="上证指数,3880.0,3888.0,3885.0,3900.0,3867.0,0,0,0,'
           '500000000000,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-09-14,15:00:00";\n'
           'var hq_str_sz399001="深证成指,13380.0,13400.0,13384.0";')
    parsed = sr.parse_sina_lines(txt)
    eq('parse_sina_lines: 命中代码数', len(parsed), 2)
    check('parse_sina_lines: 字段切分', parsed['sh000001'][0] == '上证指数'
          and parsed['sh000001'][3] == '3885.0')
    eq('parse_sina_lines: 短行也能解析', len(parsed['sz399001']), 4)
    eq('parse_sina_lines: 无匹配返回空', sr.parse_sina_lines('garbage'), {})

    # ---- fetch_all_sources ----
    def boom():
        raise ValueError('boom')

    src = sr.fetch_all_sources([('ok', lambda: 42), ('bad', boom)], workers=2)
    eq('fetch_all_sources: 成功项', src['ok'], 42)
    eq('fetch_all_sources: 失败项为 None', src['bad'], None)
    eq('fetch_all_sources: 失败已记录', len(sr.ERRORS) > 0, True)
    sr.ERRORS[:] = []

    # ---- feishu_push ----
    print('=== feishu_push 自检 ===')
    card = fp.build_card(sm)
    eq('card: 涨跌配色(绿)', card['header']['template'], 'green')
    check('card: 有 note 免责声明',
          any(e.get('tag') == 'note' for e in card['elements']))
    md_text = fp.render_markdown(sm)
    check('render_markdown: 含标题', 'A股复盘 2026-09-14' in md_text)
    payload = fp.json.dumps({'msg_type': 'interactive', 'card': card},
                            ensure_ascii=False)
    check('card: 体积远小于 30KB', len(payload.encode('utf-8')) < 30 * 1024)

    tmpl = {'msg_type': 'interactive',
            'card': {'elements': [{'tag': 'div',
                                   'text': {'content': '{{content}}',
                                            'tag': 'lark_md'}}],
                     'title': '{{title}}'},
            'nested': [{'deep': ['{{title}}']}]}
    snapshot = copy.deepcopy(tmpl)
    out = fp._replace_placeholders(tmpl, 'T"1\n2', 'C\\x "y"\nz')
    eq('占位符: 顶层替换', out['card']['title'], 'T"1\n2')
    eq('占位符: 嵌套列表替换', out['nested'][0]['deep'][0], 'T"1\n2')
    eq('占位符: 正文含引号/反斜杠/换行', out['card']['elements'][0]['text']['content'],
       'C\\x "y"\nz')
    # 关键回归点：模板本身必须原封不动，否则第二次推送会复用上一次的文案
    eq('占位符: 不修改原模板', tmpl, snapshot)
    # 关键性质：替换后仍可无损 json 往返（旧实现在这里容易拼坏 JSON）
    re_parsed = fp.json.loads(fp.json.dumps(out, ensure_ascii=False))
    eq('占位符: JSON 往返无损', re_parsed['card']['elements'][0]['text']['content'],
       'C\\x "y"\nz')
    # 同一份模板连续用两次，结果应各自独立
    first = fp._replace_placeholders(tmpl, 'AAA', '111')
    second = fp._replace_placeholders(tmpl, 'BBB', '222')
    eq('占位符: 二次调用不受首次影响',
       (first['card']['title'], second['card']['title']), ('AAA', 'BBB'))

    # 回归：正文含制表符/控制字符时，模板结构必须保住。
    # 旧实现是「先 json.dumps 再手工转义」，只转义了 \\ " \n，
    # 遇到 \t 或 \x0b 会生成非法 JSON -> json.loads 抛错 -> 静默退化成
    # {'title':..,'content':..}，配置里的 msg_type/card 模板被整块丢掉。
    for label, content in (('制表符', 'a\tb'), ('垂直制表符', 'a\x0bb'),
                           ('退格', 'a\bb'), ('换页', 'a\fb')):
        body = fp._replace_placeholders(tmpl, 'T', content)
        check('占位符: 控制字符 %s 不丢模板' % label,
              body.get('msg_type') == 'interactive' and 'card' in body)
        got = json.loads(json.dumps(body, ensure_ascii=False))
        eq('占位符: 控制字符 %s 正文还原' % label,
           got['card']['elements'][0]['text']['content'], content)

    eq('错误码: 19001', fp._parse_feishu('{"code":19001}', 200)[1],
       fp.FEISHU_ERRORS[19001])
    eq('错误码: 成功', fp._parse_feishu('{"code":0}', 200), (True, 'ok'))
    eq('错误码: 未知码保留原文',
       fp._parse_feishu('{"code":999,"msg":"x"}', 200), (False, 'code=999 msg=x'))
    eq('错误码: 非 JSON 按 HTTP 状态判断',
       fp._parse_feishu('not json', 200)[1][:8], 'not json')

    eq('should_push: always', fp.should_push(sm, 'always'), (True, ''))
    check('should_push: pressure 阈值生效',
          fp.should_push(sm, 'pressure')[0] == (len(sup if False else pre) >= 3))

    # ---- 行情主机：缓存命中不触网；坏主机每台只试一次 ----
    print('=== 抓取耗时相关 ===')
    order = sr._JsonFetcher(hosts=['h1', 'h2', 'h3'])
    order.good = 'h2'
    eq('主机顺序: 优先上次成功', order._ordered_hosts(), ['h2', 'h1', 'h3'])
    order.good = 'nope'
    eq('主机顺序: 未知主机则按原列表', order._ordered_hosts(), ['h1', 'h2', 'h3'])
    eq('主机顺序: 直连 URL', sr._JsonFetcher(hosts=None)._ordered_hosts(), [None])

    tmp = tempfile.mkdtemp(prefix='sr-cache-')
    try:
        fetcher = sr._JsonFetcher(hosts=['no-such-host'], cache_dir=tmp,
                                  timeout=1, budget=1, pace=0, slots=1)
        with open(os.path.join(tmp, 'k.json'), 'wb') as fp_cache:
            fp_cache.write(b'{"ok": 1}')
        eq('缓存命中不触网', fetcher.json('/unused', key='k'), {'ok': 1})
        sr.OFFLINE[0] = True
        try:
            raised = False
            try:
                fetcher.json('/nope', key='missing')
            except Exception:
                raised = True
            check('离线缺缓存立即失败', raised)
        finally:
            sr.OFFLINE[0] = False
    finally:
        shutil.rmtree(tmp)

    calls = []
    real_http_json = sr.http_json

    def _down(url, referer=None, timeout=12, retries=2):
        calls.append(url)
        raise Exception('down')

    sr.http_json = _down
    try:
        multi = sr._JsonFetcher(hosts=['h1.example', 'h2.example'],
                                timeout=1, budget=5, pace=0, slots=1)
        try:
            multi.json('/x', key='no-cache')
        except Exception:
            pass
        eq('多主机每台只试一次', len(calls), 2)
        check('多主机先试列表第一台', calls and 'h1.example' in calls[0])

        calls[:] = []
        single = sr._JsonFetcher(hosts=['only.example'], retries=2,
                                 timeout=1, budget=5, pace=0, slots=1)
        try:
            single.json('/x', key='no-cache-2')
        except Exception:
            pass
        eq('单主机按次数重试', len(calls), 2)
    finally:
        sr.http_json = real_http_json

    # ---- 结果 ----
    print('')
    if FAILED:
        print('[X] 自检未通过：%d 项失败' % len(FAILED))
        for n in FAILED:
            print('    - %s' % n)
        return 1
    print('[OK] 全部自检通过')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\n已中断')
        sys.exit(130)
