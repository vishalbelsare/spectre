"""
@author: Heerozh (Zhang Jianhao)
@copyright: Copyright 2019-2020, Heerozh. All rights reserved.
@license: Apache 2.0
@email: heeroz@gmail.com
"""
import io
import os
from struct import unpack, calcsize
import re

import pandas as pd
import numpy as np
import glob
from tqdm.auto import tqdm

from spectre.data.dataloader import DataLoader


class TDXLoader(DataLoader):
    """ China stock daily price loader """

    def __init__(self, tdx_vipdoc_dir: str,
                 universe=('^SH60.*', '^SZ00.*', 'SH000001'),
                 calender_asset='SH000001', align_by_time=False) -> None:
        """
            universe 可以是：
                大盘： ' SZ399001', 'SZ399006', 'SZ399300', 'SZ399905'
                创业板: ^SZ30.*',
        """
        super().__init__(tdx_vipdoc_dir,
                         ohlcv=('open', 'high', 'low', 'close', 'volume'),
                         adjustments=('ex-dividend', 'split_ratio'))
        self._calender = calender_asset
        self.universe = [re.compile(m) for m in universe]
        self._align_by_time = align_by_time

    @property
    def last_modified(self) -> float:
        pattern = os.path.join(self._path, "*.*")
        files = glob.glob(pattern)
        if len(files) == 0:
            raise ValueError("Dir '{}' does not contains any files.".format(self._path))
        return max([os.path.getmtime(fn) for fn in files])

    def _load(self) -> pd.DataFrame:
        def tick_in_universe(_ticker_):
            for m in self.universe:
                if re.match(m, _ticker_):
                    return True
            return False

        def read_daily_price(file_handle):
            data = []
            while True:
                binary = file_handle.read(4 * 5 + 4 * 3)
                if not binary:
                    break
                row = unpack('<IIIIIfII', binary)
                data.append(row)
            ret = pd.DataFrame(data, columns=['date', 'open', 'high', 'low', 'close', 'turnover',
                                              'volume', 'unused'])
            price_cols = ['open', 'high', 'low', 'close']
            ret[price_cols] /= 100
            ret[price_cols] = ret[price_cols].astype(np.float32)
            ret['volume'] = ret['volume'].astype(np.float64)
            ret['date'] = pd.to_datetime(ret['date'].astype(str), format='%Y%m%d')
            ret.set_index('date', inplace=True)
            ret.index = ret.index.tz_localize('Asia/Shanghai')
            ret.drop(columns=['unused'], inplace=True)
            return ret[~ret.index.duplicated(keep='last')]

        def read_adjustment(file_handle):
            file_handle.seek(104, 1)
            date = unpack('<I', file_handle.read(4))
            rows = []
            while date[0] != 0xFFFFFFFF:
                row = unpack('<ffff100s', file_handle.read(0x74))
                rows.append(date + row)

                next_date = file_handle.read(4)
                if not next_date:
                    break
                date = unpack('<I', next_date)
            ret = pd.DataFrame(rows, columns=['date', 'split', 'allotment', 'allotment_price',
                                              'dividend', 'note'])
            ret['date'] = pd.to_datetime(ret['date'], unit='s')
            ret['note'] = ret['note'].str.decode("gbk").str.rstrip('\0')
            ret.set_index('date', inplace=True)
            ret.index = ret.index.tz_localize('Asia/Shanghai')
            return ret

        def read_fundamentals(file_handle):
            mem = file_handle.read()
            header_pack_format = '<1hI1H3L'
            header_size = calcsize(header_pack_format)
            stock_item_size = calcsize("<6s1c1L")
            data_header = mem[0:header_size]
            stock_header = unpack(header_pack_format, data_header)
            max_count = stock_header[2]
            report_date = stock_header[1]
            report_size = stock_header[4]
            report_fields_count = int(report_size / 4)
            report_pack_format = '<{}f'.format(report_fields_count)

            selected = []
            for stock_idx in range(0, max_count):
                cur = header_size + stock_idx * calcsize("<6s1c1L")
                si = mem[cur:cur + stock_item_size]
                stock_item = unpack("<6s1c1L", si)
                code = stock_item[0].decode("utf-8")
                foa = stock_item[2]
                cur = foa

                info_data = mem[cur:cur + calcsize(report_pack_format)]
                cw_info = unpack(report_pack_format, info_data)
                selected.append((report_date, code, cw_info[0], cw_info[3], cw_info[5],
                                 cw_info[237], cw_info[238],))

            ret = pd.DataFrame(selected, columns=['date', 'asset', 'eps', 'bvps', 'roe',
                                                  'outstanding', 'floating'])
            ret['date'] = pd.to_datetime(ret['date'].astype(str), format='%Y%m%d') \
                .dt.tz_localize('Asia/Shanghai')
            ret.asset = ret.asset.str.replace(r'^00', 'SZ00')
            ret.asset = ret.asset.str.replace(r'^60', 'SH60')
            ret.set_index(['date', 'asset'], inplace=True)
            return ret

        print('Loading prices...')

        exchanges = ['sh', 'sz']

        # read all daily prices
        price_dfs = {}
        for exchange in exchanges:
            pattern = os.path.join(self._path, exchange, 'lday', "*.day")
            files = glob.glob(pattern)
            for fn in tqdm(files):
                ticker = os.path.basename(fn)[:-4].upper()
                if not tick_in_universe(ticker):
                    continue
                with io.open(fn, 'rb') as f:
                    df = read_daily_price(f)
                    price_dfs[ticker] = df

        print('Loading adjustments...')

        # read all div splits
        adj_dfs = {}
        for exchange in exchanges:
            filename = os.path.join(self._path, 'full_{}.PWR'.format(exchange))
            with io.open(filename, 'rb') as f:
                f.seek(12)
                while True:
                    ticker = f.read(12)
                    if not ticker:
                        break
                    ticker = ticker.decode("utf-8").rstrip('\0').upper()
                    adj = read_adjustment(f)
                    if tick_in_universe(ticker):
                        adj_dfs[ticker] = adj

        print('Merging prices and adjustments...')

        # merge adjustments to prices
        for ticker in tqdm(price_dfs.keys()):
            df = price_dfs[ticker]
            if ticker not in adj_dfs:
                df['ex-dividend'] = 0
                df['split_ratio'] = 1
                price_dfs[ticker] = df
                continue
            # 配股相当于支付给对方分红，支付price*allotment
            # 注意，这个算法逻辑上问题是不大的，但是会导致涨停板计算错误，好在大股一般没配股的问题
            # 所以涨停板之类，得使用交易所的除权方式自己重新计算，或者用交易量成交/滑点模型实现
            adjustment = adj_dfs[ticker]
            ex_div = adjustment['dividend']
            ex_div -= adjustment['allotment_price'] * adjustment['allotment']
            ex_div = ex_div.reindex(df.index)
            ex_div = ex_div.fillna(0)
            ex_div.name = 'ex-dividend'
            sp_rto = adjustment['split'] + 1 + adjustment['allotment']
            sp_rto = sp_rto.reindex(df.index)
            sp_rto = sp_rto.fillna(1)
            sp_rto.name = 'split_ratio'
            price_dfs[ticker] = pd.concat([df, ex_div, sp_rto], axis=1)

        ret_df = pd.concat(price_dfs, sort=False)
        ret_df = ret_df.rename_axis(['asset', 'date'])

        # formatting
        print('Formatting data...')
        ret_df = ret_df.swaplevel(0, 1).sort_index()
        ret_df = self._format(ret_df, split_ratio_is_inverse=True)
        if self._calender:
            ret_df = self._align_to(ret_df, self._calender, self._align_by_time)

        print('Loading fundamentals...')
        fund_dfs = []
        pattern = os.path.join(self._path, 'cw', "gpcw*.dat")
        files = glob.glob(pattern)
        for fn in tqdm(files):
            with io.open(fn, 'rb') as f:
                df = read_fundamentals(f)
                fund_dfs.append(df)
        fund_df = pd.concat(fund_dfs)
        fund_df = fund_df[~fund_df.index.duplicated(keep='last')]
        fund_df.sort_index(level=0, inplace=True)

        print('Merging prices and fundamentals...')
        ret_df = ret_df.join(fund_df)
        cols = fund_df.columns
        ret_df[cols] = ret_df[cols].groupby(level=1).fillna(method='pad')

        print('Merging prices and Index weights...')
        pattern = os.path.join(self._path, "*_weights.csv")
        files = glob.glob(pattern)
        for fn in files:
            name = os.path.basename(fn)[:-12].upper()
            df = pd.read_csv(fn, parse_dates=['effDate'])
            df.consID = df.consID.str.replace(r'(.*)(.XSHG)', r'SH\1')
            df.consID = df.consID.str.replace(r'(.*)(.XSHE)', r'SZ\1')
            df = df.set_index(['effDate', 'consID'])

            s = df.weight / 100
            # 对于移出的设为0，为了方便之后的pad na
            s = s.unstack().stack(dropna=False).fillna(0)
            s.index = s.index.set_names(['date', 'asset'])
            s.name = name

            ret_df = ret_df.join(s)
            # 填充Nan
            ret_df[name] = ret_df[name].groupby(level=1).fillna(method='pad')
            # 把0权重的设为nan
            ret_df[name] = ret_df[name].replace(to_replace=0, value=np.nan)
            print(name, ' weights OK.')

        print('All ok')

        return ret_df
