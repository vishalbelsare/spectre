"""
@author: Heerozh (Zhang Jianhao)
@copyright: Copyright 2019-2020, Heerozh. All rights reserved.
@license: Apache 2.0
@email: heeroz@gmail.com
"""
import numpy as np
import torch
import pandas as pd

from typing import Optional, Sequence, Union
from ..parallel import nanlast
from .factor import BaseFactor, CustomFactor
from ..config import Global


class ColumnDataFactor(BaseFactor):
    def __init__(self, inputs: Optional[Sequence[str]] = None,
                 should_delay=True, dtype=None) -> None:
        super().__init__()
        if inputs:
            self.inputs = inputs
        assert (3 > len(self.inputs) > 0), \
            "ColumnDataFactor's `inputs` can only contains one data column and corresponding " \
            "adjustments column"
        self._data = None
        self._multiplier = None
        self._should_delay = should_delay
        self.dtype = dtype

    @property
    def adjustments(self):
        return self._multiplier

    def get_total_backwards_(self) -> int:
        return 0

    def should_delay(self) -> bool:
        return self._should_delay

    def pre_compute_(self, engine, start, end) -> None:
        super().pre_compute_(engine, start, end)
        if self._data is None:
            self._data = engine.column_to_tensor_(self.inputs[0])
            if self.dtype is not None:
                self._data = self._data.to(self.dtype)
            self._data = engine.group_by_(self._data, self.groupby)
            if len(self.inputs) > 1 and self.inputs[1] in engine.dataframe_:
                self._multiplier = engine.column_to_tensor_(self.inputs[1])
                self._multiplier = engine.group_by_(self._multiplier, self.groupby)
                if self.dtype is not None:
                    self._multiplier = self._multiplier.to(self.dtype)
            else:
                self._multiplier = None
            self._clean_required = True

    def clean_up_(self, force=False) -> None:
        super().clean_up_(force)
        self._data = None
        self._multiplier = None
        self._clean_required = False

    def compute_(self, stream: Union[torch.cuda.Stream, None]) -> torch.Tensor:
        return self._data

    def compute(self, *inputs: Sequence[torch.Tensor]) -> torch.Tensor:
        pass

#     def adjusted_shift(self, periods=1):
#         factor = AdjustedShiftFactor(win=periods, inputs=(self,))
#         return factor
#
#
# class AdjustedShiftFactor(CustomFactor):
#     """ Shift the root datafactor """
#
#     def compute(self, data) -> torch.Tensor:
#         return data.first()


class AdjustedColumnDataFactor(CustomFactor):
    def __init__(self, data: ColumnDataFactor):
        super().__init__(1, (data,))
        self.parent = data

    def compute(self, data) -> torch.Tensor:
        multi = self.parent.adjustments
        if multi is None:
            return data
        return data * multi / nanlast(multi, dim=1)[:, None]


class AssetClassifierDataFactor(BaseFactor):
    """ Dict to categorical output for asset, slow """
    def __init__(self, sector: dict, default: int):
        super().__init__()
        self.sector = sector
        self.default = default
        self._data = None

    def get_total_backwards_(self) -> int:
        return 0

    def should_delay(self) -> bool:
        return False

    def pre_compute_(self, engine, start, end) -> None:
        super().pre_compute_(engine, start, end)
        assets = engine.dataframe_index[1]
        sector = self.sector
        default = self.default
        data = [sector.get(asset, default) for asset in assets]  # slow
        data = torch.tensor(data, device=engine.device, dtype=Global.float_type)
        self._data = engine.group_by_(data, self.groupby)

    def clean_up_(self, force=False) -> None:
        super().clean_up_(force)
        self._data = None

    def compute_(self, stream: Union[torch.cuda.Stream, None]) -> torch.Tensor:
        return self._data

    def compute(self, *inputs: Sequence[torch.Tensor]) -> torch.Tensor:
        pass


class SeriesDataFactor(ColumnDataFactor):
    """ Add series to engine, slow """
    def __init__(self, series: pd.Series, fill_na=None, should_delay=True):
        self.series = series
        self.fill_na = fill_na
        assert series.index.names == ['date', 'asset'], \
            "df.index.names should be ['date', 'asset'] "
        super().__init__(inputs=[str(series.name)], should_delay=should_delay)

    def pre_compute_(self, engine, start, end) -> None:
        if self.series.name not in engine.dataframe_.columns:
            engine._dataframe = engine.dataframe_.join(self.series)
            if self.fill_na is not None:
                engine._dataframe[self.series.name] = engine._dataframe[self.series.name].\
                    groupby(level=1).fillna(method=self.fill_na)
        super().pre_compute_(engine, start, end)


class DatetimeDataFactor(BaseFactor):
    """ Datetime's attr to DataFactor """
    _instance = {}

    def __new__(cls, attr):
        if attr not in cls._instance:
            cls._instance[attr] = super().__new__(cls)
        return cls._instance[attr]

    def __init__(self, attr) -> None:
        super().__init__()
        self._data = None
        self.attr = attr

    def get_total_backwards_(self) -> int:
        return 0

    def should_delay(self) -> bool:
        return False

    def pre_compute_(self, engine, start, end) -> None:
        super().pre_compute_(engine, start, end)
        if self._data is None:
            data = getattr(engine.dataframe_index[0], self.attr)  # slow
            if not isinstance(data, np.ndarray):
                data = data.values
            data = torch.from_numpy(data).to(
                device=engine.device, dtype=Global.float_type, non_blocking=True)
            self._data = engine.group_by_(data, self.groupby)
            self._clean_required = True

    def clean_up_(self, force=False) -> None:
        super().clean_up_(force)
        self._data = None
        self._clean_required = False

    def compute_(self, stream: Union[torch.cuda.Stream, None]) -> torch.Tensor:
        return self._data

    def compute(self, *inputs: Sequence[torch.Tensor]) -> torch.Tensor:
        pass
