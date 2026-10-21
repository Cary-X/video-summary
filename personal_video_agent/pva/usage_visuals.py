"""Compact, zero-based usage charts for the local dashboard."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd


LOCAL_ZONE = ZoneInfo("Asia/Shanghai")
STEP_NAMES = {
    "photo identification": "Identify photo",
    "match checking": "Match video",
    "translation": "Translate",
    "frame reading": "Read video frames",
    "summary notes": "Summarize · evidence notes",
    "summary": "Summarize · final brief",
    "classification": "Classify",
}
LOCAL_STEPS = ("Local OCR", "Local ASR")


def prepare_usage(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    numeric = ("prompt_tokens", "completion_tokens", "total_tokens",
               "cache_hit_tokens", "cache_miss_tokens",
               "estimated_cost_low_usd", "estimated_cost_high_usd")
    for name in numeric:
        frame[name] = pd.to_numeric(frame[name], errors="coerce").fillna(0).clip(lower=0)
    local = pd.to_datetime(frame["at"], utc=True).dt.tz_convert(LOCAL_ZONE)
    frame["local_time"] = local.dt.strftime("%Y-%m-%d %H:%M")
    frame["day"] = local.dt.tz_localize(None).dt.normalize()
    purpose = frame["purpose"] if "purpose" in frame else pd.Series(
        ["other"] * len(frame), index=frame.index)
    frame["task_step"] = purpose.map(STEP_NAMES).fillna(purpose)
    return frame


def step_totals(frame: pd.DataFrame) -> pd.DataFrame:
    """Provider-reported usage by AI operation; local steps use no API tokens."""
    totals = (frame.assign(calls=1).groupby("task_step", as_index=False)
              .agg(calls=("calls", "sum"), input_tokens=("prompt_tokens", "sum"),
                   output_tokens=("completion_tokens", "sum"),
                   tokens=("total_tokens", "sum"),
                   cost_low_usd=("estimated_cost_low_usd", "sum"),
                   cost_high_usd=("estimated_cost_high_usd", "sum")))
    missing = [{"task_step": name, "calls": 0, "input_tokens": 0,
                "output_tokens": 0, "tokens": 0, "cost_low_usd": 0.0,
                "cost_high_usd": 0.0} for name in LOCAL_STEPS]
    return pd.concat([totals, pd.DataFrame(missing)], ignore_index=True).sort_values(
        "task_step", ignore_index=True)


def complete_days(frame: pd.DataFrame, columns: list[str], period: str) -> pd.DataFrame:
    today = pd.Timestamp(datetime.now(LOCAL_ZONE).date())
    start = (today - pd.Timedelta(days=6 if period == "7 days" else 29)
             if period != "All time" else min(frame["day"].min(), today))
    days = pd.date_range(start=start, end=today, freq="D")
    return (frame.groupby("day")[columns].sum().reindex(days, fill_value=0)
            .rename_axis("day").reset_index())


def _upper(values: pd.Series) -> float:
    maximum = float(values.max()) if not values.empty else 0
    return maximum * 1.12 if maximum > 0 else 1.0


def daily_bars(frame: pd.DataFrame, columns: dict[str, str], *,
               title: str, colors: list[str], money: bool = False) -> alt.Chart:
    """Use narrow grouped bars and an explicit nonnegative vertical domain."""
    names = list(columns)
    long = frame.melt("day", value_vars=names, var_name="series", value_name="value")
    long["series"] = long["series"].map(columns)
    long["value"] = long["value"].clip(lower=0)
    maximum = _upper(long["value"])
    chart = (alt.Chart(long, title=title).mark_bar(size=5, cornerRadiusTopLeft=2,
                                                    cornerRadiusTopRight=2)
             .encode(
                 x=alt.X("day:T", title=None,
                         axis=alt.Axis(format="%m/%d", labelAngle=0, grid=False)),
                 xOffset=alt.XOffset("series:N", sort=list(columns.values())),
                 y=alt.Y("value:Q", title="USD" if money else None,
                         scale=alt.Scale(domain=[0, maximum]),
                         axis=alt.Axis(format="$.3f" if money else "~s")),
                 color=alt.Color("series:N", title=None,
                                 scale=alt.Scale(domain=list(columns.values()),
                                                 range=colors)),
                 tooltip=[alt.Tooltip("day:T", title="Date", format="%Y-%m-%d"),
                          alt.Tooltip("series:N", title="Type"),
                          alt.Tooltip("value:Q", title="USD" if money else "Count",
                                      format="$.6f" if money else ",")],
             ).properties(height=230))
    return chart.configure_view(stroke=None)


def category_bars(frame: pd.DataFrame, label: str, value: str, *,
                  title: str, color: str = "#60a5fa",
                  money: bool = False) -> alt.Chart:
    data = frame[[label, value]].copy()
    data[value] = pd.to_numeric(data[value], errors="coerce").fillna(0).clip(lower=0)
    return (alt.Chart(data, title=title).mark_bar(size=10, color=color,
                                                 cornerRadiusEnd=3)
            .encode(
                y=alt.Y(f"{label}:N", sort="-x", title=None,
                        axis=alt.Axis(labelLimit=190, grid=False)),
                x=alt.X(f"{value}:Q", title=None,
                        scale=alt.Scale(domain=[0, _upper(data[value])]),
                        axis=alt.Axis(format="$.4f" if money else "~s")),
                tooltip=[alt.Tooltip(f"{label}:N", title="Group"),
                         alt.Tooltip(f"{value}:Q", title="USD" if money else "Tokens",
                                     format="$.6f" if money else ",")],
            ).properties(height=max(130, 28 * len(data))).configure_view(stroke=None))
